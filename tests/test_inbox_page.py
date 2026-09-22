"""The way in, on the family page: paste, review, save; type, review, save.

Nothing is written when the text is read; the review page says what saving
would do, week by week; the saving reads the text again and writes only
what the record lacks. A form that fails comes back with its fields as they
were. The text is synthetic, in the portal's shapes.
"""

import dataclasses
import html
import pathlib
import re
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.clock import FrozenClock
from blossom.dependencies import STATE_ATTRIBUTE, ApplicationState
from blossom.household import COOKIE
from blossom.intake import NOTE_MAX_LENGTH, TEXT_MAX_LENGTH
from blossom.reconciliation import SourceChannel, SourceRecord
from blossom.routes.inbox import (
    CHOOSE_ONE_TYPE,
    CLAIM_UNREADABLE,
    FAR_DUE_DATE,
    LONG_NOTE,
    LOOK_AGAIN,
    NEEDS_ANSWER,
    NEEDS_COURSE,
    NEEDS_TITLE,
    NOT_A_DUE_DATE,
    NOT_A_KIND,
    NOTHING_PASTED,
    TOO_LONG,
)
from blossom.settings import Settings
from blossom.stores.project_state import Assignment, AssignmentKind, UnreadableClaim
from tests.support import SAME_ORIGIN, fixture_settings

PAGE = {"Accept": "text/html"}
HERS = "quiet mornings and loud music"
THEIRS = "the kitchen table at seven"
SUMMARY = """Homework for Wren

* 09/03/2026 - Thursday
08 Geometry - Assigned: Book Covers: (Due:09/08/2026)
Cover both books with paper.
* 09/04/2026 - Friday
Humanities - Due: Summer Reading - Log:
Spanish - Assigned: Binder, labeled dividers and lined paper check: (Due:09/11/2026)
"""
MOVED = "Homework for Wren\n- 09/10/2026 - Thursday\n08 Geometry - Due: Book Covers:\n"
EMAIL = "Assignments:\n09/09 08 Geometry - A: Homework/Classwork: Book Covers Grade: Missing\n"
SCHOOL_SAYS_MISSING = re.compile(
    r'<strong><a class="assignment-link" href="/student/assignments/[^"]+\?return_to=family" '
    r'aria-label="Book Covers, 08 Geometry">Book Covers</a>: '
    r"the school reports it missing\.</strong>"
)
"""The family page's line for the school's report, its title the link to the
assignment's details."""
WEEKLY = "Homework for Wren\n- 09/07/2026 - Monday\nMath - Due: Weekly practice:\n"
WEEKLY_AGAIN = "Homework for Wren\n- 09/14/2026 - Monday\nMath - Due: Weekly practice:\n"
ENTRY = {
    "course": "Spanish",
    "title": "Vocabulary list, unit two",
    "due_date": "2026-09-11",
    "assigned_on": "",
    "kind": "HOMEWORK",
    "note": "Ten words, both ways.",
}


def settings_in(tmp_path: pathlib.Path, **environ: str) -> Settings:
    """A household with nothing on record, its week pinned to the text's, state in ``tmp_path``."""
    return fixture_settings(
        **{
            "BLOSSOM_TODAY": "2026-09-07",
            "BLOSSOM_FIXTURE_PATH": "",
            "BLOSSOM_DATABASE_PATH": str(tmp_path / "blossom.sqlite3"),
            "BLOSSOM_CHECKPOINT_PATH": str(tmp_path / "checkpoints.sqlite3"),
            "BLOSSOM_TRACE_PATH": str(tmp_path / "traces.sqlite3"),
            **environ,
        }
    )


def state_of(client: TestClient) -> ApplicationState:
    state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
    return state


def article_for(page: str, title: str) -> str:
    """The card on a page that names ``title``, from the title to the card's end."""
    return page.split(title, 1)[1].split("</article>", 1)[0]


def by_due(rows: list[Assignment]) -> list[Assignment]:
    return sorted(rows, key=lambda row: row.due_date or date.min)


def review_form(page: str) -> dict[str, str]:
    """The review form as a browser would send it back untouched: the hidden fields, each
    select's chosen option, and the text, read from the page itself."""
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', page))
    for name, body in re.findall(r'<select name="([^"]+)"[^>]*>(.*?)</select>', page, re.S):
        chosen = re.search(r'<option value="([^"]+)" selected>', body)
        assert chosen is not None, name
        fields[name] = chosen.group(1)
    text = re.search(r'<textarea name="text" hidden>(.*?)</textarea>', page, re.S)
    assert text is not None
    fields["text"] = html.unescape(text.group(1))
    return fields


def test_the_family_page_offers_the_paste_box_and_the_entry_form(tmp_path: pathlib.Path) -> None:
    with TestClient(create_app(settings_in(tmp_path)), headers=SAME_ORIGIN) as client:
        page = client.get("/parent", headers=PAGE).text

    assert '<a class="skip" href="#main">Skip to main content</a>' in page
    assert '<a href="#add-assignments">Add assignments</a>' in page
    assert "<summary>Add assignments</summary>" in page
    assert "<h2>Paste school text</h2>" in page
    assert "<summary>See an example</summary>" in page
    assert '<label for="paste-text">School text</label>' in page
    assert 'action="/parent/inbox/read"' in page
    assert '<textarea id="paste-text" name="text"' in page
    assert ">Preview assignments</button>" in page
    assert '<h2 class="entry-heading">Enter one assignment</h2>' in page
    assert 'action="/parent/inbox/enter"' in page
    assert 'id="entry-course" type="text" name="course"' in page
    assert '<label for="entry-due_date">Due date (optional)</label>' in page
    assert '<label for="entry-assigned_on">Assigned date (optional)</label>' in page
    assert '<select id="entry-kind" name="kind">' in page
    assert f'name="note" rows="3" maxlength="{NOTE_MAX_LENGTH}"' in page
    assert "No open help requests." in page
    assert "No plans need your review." in page


def test_a_paste_is_reviewed_week_by_week_and_saved_only_when_asked(
    tmp_path: pathlib.Path,
) -> None:
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        shown = client.post("/parent/inbox/read", data={"text": SUMMARY})
        nothing_yet = client.get("/student/due-this-week", headers=PAGE).text
        kept = client.post("/parent/inbox/keep", data={"text": SUMMARY})
        family = client.get(kept.headers["location"], headers=PAGE).text
        hers = client.get("/student/due-this-week", headers=PAGE).text
        again = client.post("/parent/inbox/read", data={"text": SUMMARY}).text

    assert shown.status_code == 200
    assert "<h1>Review assignments</h1>" in shown.text
    assert '<li aria-current="step"><strong>2. Review</strong></li>' in shown.text
    assert "3 assignments across 2 weeks:" in shown.text
    assert "3 new, 0 updates, 0 already saved." in shown.text
    assert "Nothing is saved until Save is pressed." in shown.text
    assert "<h2>Week of Monday, August 31, 2026</h2>" in shown.text
    assert "<h2>Week of Monday, September 7, 2026</h2>" in shown.text
    assert shown.text.index("August 31") < shown.text.index("September 7, 2026</h2>")
    assert shown.text.count('<span class="pill">New</span>') == 3
    covers = article_for(shown.text, "<h2>Book Covers</h2>")
    assert "Due Tuesday, September 8, 2026" in covers
    assert '<span class="source">Assigned Thursday, September 3, 2026</span>' in covers
    assert (
        '<p class="effect" data-base="Saved as a new assignment.">Saved as a new assignment.</p>'
        in (covers)
    )
    assert '<option value="TASK" selected>Task</option>' in covers
    assert "From the teacher: <q>Cover both books with paper.</q>" in covers
    assert "school portal (the assignment&#39;s own line): 2026-09-08" in covers
    assert "Read from line 4 of the text." in covers
    assert '<textarea name="text" hidden>' in shown.text
    assert ">Save 3 assignments</button>" in shown.text
    assert 'formaction="/parent/inbox/edit" formnovalidate>Edit</button>' in shown.text
    assert '<a class="cancel" href="/parent">Cancel import</a>' in shown.text
    assert "Book Covers" not in nothing_yet
    assert kept.status_code == 303
    assert kept.headers["location"] == "/parent?added=3&updated=0&unchanged=0"
    assert "3 added, 0 updated, 0 unchanged." in family
    assert "Book Covers" in hers
    assert "From the teacher: <q>Cover both books with paper.</q>" in hers
    assert "Reported status:" not in article_for(hers, "Book Covers")
    assert "Entered by a parent." not in hers
    assert "Binder, labeled dividers and lined paper check" in hers
    assert "Summer Reading - Log" not in hers
    assert again.count('<span class="pill">Already saved</span>') == 3
    assert "0 new, 0 updates, 3 already saved." in again
    assert "Everything here is saved already; there is nothing to save." in again
    assert (
        'class="primary done" disabled aria-disabled="true" data-changed-label="Save changes">'
        "Save 0 assignments" in again
    )


def test_an_entry_by_hand_is_reviewed_then_saved_as_the_familys_own(
    tmp_path: pathlib.Path,
) -> None:
    undated = {**ENTRY, "title": "Song lyrics, memorized", "due_date": "", "note": ""}
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        shown = client.post("/parent/inbox/enter", data=ENTRY)
        kept = client.post("/parent/inbox/keep", data=ENTRY)
        family = client.get(kept.headers["location"], headers=PAGE).text
        hers = client.get("/student/due-this-week", headers=PAGE).text
        twice = client.post("/parent/inbox/keep", data=ENTRY)
        nothing_new = client.get(twice.headers["location"], headers=PAGE).text
        no_date = client.post("/parent/inbox/enter", data=undated)
        kept_undated = client.post("/parent/inbox/keep", data=undated)
        rows = {row.title: row for row in state_of(client).project_state.all_assignments()}

    assert shown.status_code == 200
    assert "1 assignment across 1 week:" in shown.text
    assert "Week of Monday, September 7, 2026" in shown.text
    assert "family entry: 2026-09-11" in shown.text
    assert "Your note: <q>Ten words, both ways.</q>" in shown.text
    assert '<p class="note">Entered by a parent.</p>' in shown.text
    assert '<input type="hidden" name="title" value="Vocabulary list, unit two">' in shown.text
    assert ">Save 1 assignment</button>" in shown.text
    assert kept.headers["location"] == "/parent?added=1&updated=0&unchanged=0"
    assert "1 added, 0 updated, 0 unchanged." in family
    assert "Vocabulary list, unit two" in hers
    assert "A parent wrote: <q>Ten words, both ways.</q>" in hers
    assert '<p class="source">Entered by a parent.</p>' in hers
    assert twice.headers["location"] == "/parent?added=0&updated=0&unchanged=1"
    assert "Nothing to save: everything read was saved already (1 unchanged)." in nothing_new
    assert "<h2>No due date yet</h2>" in no_date.text
    assert "No due date given" in no_date.text
    assert kept_undated.headers["location"] == "/parent?added=1&updated=0&unchanged=0"
    assert rows["Song lyrics, memorized"].due_date is None
    assert rows["Song lyrics, memorized"].origins == {
        "record": SourceChannel.PARENT_ENTRY,
        "kind": SourceChannel.PARENT_ENTRY,
    }


def test_a_form_that_fails_comes_back_filled_with_the_field_named(
    tmp_path: pathlib.Path,
) -> None:
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        empty = client.post("/parent/inbox/read", data={"text": "   "})
        long_text = client.post("/parent/inbox/read", data={"text": "x" * (TEXT_MAX_LENGTH + 1)})
        no_title = client.post(
            "/parent/inbox/enter", data={"course": "Spanish", "note": "keep this note"}
        )
        no_course = client.post("/parent/inbox/enter", data={"title": "Vocabulary list"})
        far = client.post(
            "/parent/inbox/enter",
            data={"course": "Spanish", "title": "Vocabulary list", "due_date": "2030-01-01"},
        )
        bad_date = client.post(
            "/parent/inbox/enter",
            data={"course": "Spanish", "title": "Vocabulary list", "due_date": "next friday"},
        )
        bad_kind = client.post(
            "/parent/inbox/enter",
            data={"course": "Spanish", "title": "Vocabulary list", "kind": "CHORE"},
        )
        long_note = client.post(
            "/parent/inbox/enter",
            data={"course": "Spanish", "title": "Vocabulary list", "note": "n" * 501},
        )
        odd = client.get("/parent?added=%C2%B2", headers=PAGE)
        huge = client.get("/parent?added=" + "9" * 5000, headers=PAGE)
        below_zero = client.get("/parent?added=-1&updated=0&unchanged=0", headers=PAGE)

    assert empty.status_code == 422
    assert NOTHING_PASTED in empty.text
    assert 'id="paste-text" name="text"' in empty.text
    assert 'aria-invalid="true" aria-describedby="problem" autofocus' in empty.text
    assert '<a href="#paste-text">Go to the field.</a>' in empty.text
    assert 'id="add-assignments" open>' in empty.text
    assert TOO_LONG in long_text.text
    assert no_title.status_code == 422
    assert NEEDS_TITLE in no_title.text
    assert 'name="course" maxlength="60" placeholder="As the portal names it" value="Spanish"' in (
        no_title.text
    )
    assert ">keep this note</textarea>" in no_title.text
    assert 'id="entry-title" type="text" name="title"' in no_title.text
    assert 'value="" aria-invalid="true" aria-describedby="problem" autofocus>' in no_title.text
    assert NEEDS_COURSE in no_course.text
    assert FAR_DUE_DATE in far.text
    assert 'value="2030-01-01" aria-invalid="true"' in far.text
    assert NOT_A_DUE_DATE in bad_date.text
    assert NOT_A_KIND in bad_kind.text
    assert LONG_NOTE in long_note.text
    assert odd.status_code == 200
    assert huge.status_code == 200
    assert " added, " not in odd.text
    assert " added, " not in huge.text
    assert below_zero.status_code == 200
    assert "-1 added" not in below_zero.text
    assert " added, " not in below_zero.text


def test_text_that_cannot_be_read_is_listed_by_line_and_can_be_edited(
    tmp_path: pathlib.Path,
) -> None:
    text = "A stray line nobody expected\nMath - Assigned: Worksheet: (Due:TBD)\n"
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        stray = client.post("/parent/inbox/read", data={"text": text})
        back = client.post("/parent/inbox/edit", data={"text": text})
        back_to_entry = client.post("/parent/inbox/edit", data=ENTRY)

    assert stray.status_code == 200
    assert "Nothing in the text was read as an assignment." in stray.text
    assert '<h2 id="needs-review">Text that needs review</h2>' in stray.text
    assert '<span class="line-number">Line 1:</span> <code>A stray line nobody expected</code>' in (
        stray.text
    )
    assert "<code>Math - Assigned: Worksheet: (Due:TBD)</code>" in stray.text
    assert ">Save " not in stray.text
    assert ">Edit the text</button>" in stray.text
    assert back.status_code == 200
    assert "<h1>Family review</h1>" in back.text
    assert 'id="add-assignments" open>' in back.text
    assert ">A stray line nobody expected\nMath - Assigned" in back.text
    assert 'value="Vocabulary list, unit two"' in back_to_entry.text
    assert ">Ten words, both ways.</textarea>" in back_to_entry.text


def test_a_later_date_for_a_saved_assignment_is_shown_beside_it_and_saved_as_evidence(
    tmp_path: pathlib.Path,
) -> None:
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        client.post("/parent/inbox/keep", data={"text": SUMMARY})
        shown = client.post("/parent/inbox/read", data={"text": MOVED}).text
        kept = client.post("/parent/inbox/keep", data={"text": MOVED})
        family = client.get(kept.headers["location"], headers=PAGE).text
        state = state_of(client)
        covers = next(
            row for row in state.project_state.all_assignments() if row.title == "Book Covers"
        )
        claims = state.project_state.deadline_records(covers.assignment_id)

    assert '<span class="pill">Saved; adds a date to review</span>' in shown
    assert '<dl class="compare">' in shown
    assert "<dt>Saved due date</dt>" in shown
    assert "<dd>Tuesday, September 8, 2026</dd>" in shown
    assert "<dt>Pasted due date</dt>" in shown
    assert "Thursday, September 10, 2026 (school portal (the day&#39;s header): 2026-09-10)" in (
        shown
    )
    assert "beside the saved date, which stays" in shown
    assert "Saving adds 0 and updates 1; the other 0 stay as they are." in shown
    assert kept.headers["location"] == "/parent?added=0&updated=1&unchanged=0"
    assert "0 added, 1 updated, 0 unchanged." in family
    assert covers.due_date == date(2026, 9, 8)
    assert [said.asserted_value for said in claims] == ["2026-09-08", "2026-09-10"]


def test_repeated_work_is_a_question_on_the_page_and_the_answer_replayed_changes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        client.post("/parent/inbox/keep", data={"text": WEEKLY})
        asked = client.post("/parent/inbox/read", data={"text": WEEKLY_AGAIN}).text
        unanswered = client.post("/parent/inbox/keep", data={"text": WEEKLY_AGAIN})
        answered = client.post(
            "/parent/inbox/keep", data={"text": WEEKLY_AGAIN, "occurrence-0": "new"}
        )
        rows = state_of(client).project_state.all_assignments()
        replayed = client.post(
            "/parent/inbox/keep", data={"text": WEEKLY_AGAIN, "occurrence-0": "new"}
        )
        rows_after = state_of(client).project_state.all_assignments()

    assert '<span class="pill">Needs your answer</span>' in asked
    assert "1 needs your answer." in asked
    assert "<legend>Which is it?</legend>" in asked
    assert '<input type="radio" name="occurrence-0" value="update">' in asked
    assert '<input type="radio" name="occurrence-0" value="new">' in asked
    assert "<dt>Saved due date</dt>" in asked
    assert "Answer the question above, then save." in asked
    assert ">Save 1 assignment</button>" in asked
    assert unanswered.status_code == 200
    assert LOOK_AGAIN in unanswered.text
    assert answered.headers["location"] == "/parent?added=1&updated=0&unchanged=0"
    assert [row.title for row in rows] == ["Weekly practice", "Weekly practice"]
    assert len({row.assignment_id for row in rows}) == 2
    assert replayed.headers["location"] == "/parent?added=0&updated=0&unchanged=1"
    assert len(rows_after) == 2


def test_the_type_is_the_parents_to_correct_on_the_page_and_on_the_entry_form(
    tmp_path: pathlib.Path,
) -> None:
    """A type chosen on a card is kept as the parent's; a saved row's card still offers the
    choice; and the type typed with an entry corrects a saved row."""
    retyped = {**ENTRY, "kind": "TASK"}
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        corrected = client.post(
            "/parent/inbox/keep", data={"text": SUMMARY, "kind-0": "HOMEWORK", "kind-1": "TASK"}
        )
        again = client.post("/parent/inbox/read", data={"text": SUMMARY}).text
        changed_on_the_page = client.post(
            "/parent/inbox/keep",
            data={"text": SUMMARY, "kind-1": "HOMEWORK", "suggested-1": "TASK"},
        )
        client.post("/parent/inbox/keep", data=ENTRY)
        shown = client.post("/parent/inbox/enter", data=retyped).text
        kept = client.post("/parent/inbox/keep", data=retyped)
        rows = {row.title: row for row in state_of(client).project_state.all_assignments()}

    assert corrected.headers["location"] == "/parent?added=3&updated=0&unchanged=0"
    assert '<select name="kind-0" data-saved="HOMEWORK">' in again
    assert 'data-changed-label="Save changes"' in again
    assert "As saved; change it if it is wrong." in again
    assert changed_on_the_page.headers["location"] == "/parent?added=0&updated=1&unchanged=2"
    assert '<span class="pill">Saved; adds the type</span>' in shown
    assert "The type becomes task, as chosen." in shown
    assert ">Save 1 assignment</button>" in shown
    assert kept.headers["location"] == "/parent?added=0&updated=1&unchanged=0"
    assert rows["Book Covers"].kind is AssignmentKind.HOMEWORK
    assert rows["Book Covers"].origins["kind"] is SourceChannel.PARENT_ENTRY
    assert rows["Summer Reading - Log"].kind is AssignmentKind.HOMEWORK
    assert rows["Binder, labeled dividers and lined paper check"].kind is AssignmentKind.TASK
    assert rows["Binder, labeled dividers and lined paper check"].origins["kind"] is (
        SourceChannel.LMS
    )
    assert rows["Vocabulary list, unit two"].kind is AssignmentKind.TASK
    assert rows["Vocabulary list, unit two"].origins["kind"] is SourceChannel.PARENT_ENTRY


SAVED_WEEK = "Tuesday 9/1/2026\nMath\nDue: Weekly practice:\n"
TWO_WEEKS_OF_PRACTICE = (
    "Tuesday 9/8/2026\nMath\nDue: Weekly practice:\n\n"
    "Tuesday 9/15/2026\nMath\nDue: Weekly practice:\n"
)


def test_a_type_chosen_on_a_folded_card_is_saved_and_shown_the_next_time(
    tmp_path: pathlib.Path,
) -> None:
    """The later of two cards gets Task and "the same assignment"; the one row saved is a
    task, a later preview of that card shows Task selected, and sending the same form again
    changes nothing."""
    form = {
        "text": TWO_WEEKS_OF_PRACTICE,
        "occurrence-1": "update",
        "kind-0": "HOMEWORK",
        "suggested-0": "HOMEWORK",
        "kind-1": "TASK",
        "suggested-1": "HOMEWORK",
    }
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        kept = client.post("/parent/inbox/keep", data=form)
        rows = state_of(client).project_state.all_assignments()
        later = client.post(
            "/parent/inbox/read", data={"text": TWO_WEEKS_OF_PRACTICE.split("\n\n")[1]}
        ).text
        replayed = client.post("/parent/inbox/keep", data=form)
        rows_after = state_of(client).project_state.all_assignments()

    assert kept.headers["location"] == "/parent?added=1&updated=0&unchanged=0"
    assert [(row.due_date, row.kind) for row in rows] == [(date(2026, 9, 15), AssignmentKind.TASK)]
    assert rows[0].origins["kind"] is SourceChannel.PARENT_ENTRY
    card = article_for(later, "<h2>Weekly practice</h2>")
    assert '<span class="pill">Already saved</span>' in card
    assert '<option value="TASK" selected>Task</option>' in card
    assert 'data-saved="TASK"' in card
    assert replayed.headers["location"] == "/parent?added=0&updated=0&unchanged=1"
    assert rows_after == rows


def test_a_type_chosen_on_one_card_about_a_saved_row_is_not_undone_by_the_other(
    tmp_path: pathlib.Path,
) -> None:
    """Both cards say the same assignment moved; the first is changed to Task and the second
    is left as the page showed it. The one row updated is a task, with or without the page's
    own note of what it showed, whichever card carries the change, and the same form sent
    again changes nothing."""
    both = {"text": TWO_WEEKS_OF_PRACTICE, "occurrence-0": "update", "occurrence-1": "update"}
    as_the_probe_sends_it = {**both, "kind-0": "TASK", "kind-1": "HOMEWORK"}
    as_the_page_sends_it = {
        **as_the_probe_sends_it,
        "suggested-0": "HOMEWORK",
        "suggested-1": "HOMEWORK",
    }
    on_the_second = {
        **both,
        "kind-0": "HOMEWORK",
        "kind-1": "TASK",
        "suggested-0": "HOMEWORK",
        "suggested-1": "HOMEWORK",
    }
    outcomes: dict[str, tuple[str, list[Assignment], str]] = {}
    for name, form in {
        "probe": as_the_probe_sends_it,
        "page": as_the_page_sends_it,
        "second": on_the_second,
    }.items():
        with TestClient(
            create_app(settings_in(tmp_path / name)), follow_redirects=False, headers=SAME_ORIGIN
        ) as client:
            client.post("/parent/inbox/keep", data={"text": SAVED_WEEK})
            kept = client.post("/parent/inbox/keep", data=form)
            rows = state_of(client).project_state.all_assignments()
            replayed = client.post("/parent/inbox/keep", data=form)
            outcomes[name] = (kept.headers["location"], rows, replayed.headers["location"])

    for name, (saved_to, rows, replayed_to) in outcomes.items():
        assert saved_to == "/parent?added=0&updated=1&unchanged=0", name
        assert [(row.due_date, row.kind) for row in rows] == [
            (date(2026, 9, 15), AssignmentKind.TASK)
        ], name
        assert rows[0].origins["kind"] is SourceChannel.PARENT_ENTRY, name
        assert replayed_to == "/parent?added=0&updated=0&unchanged=1", name


THREE_WEEKS_OF_PRACTICE = (
    TWO_WEEKS_OF_PRACTICE + "\nTuesday 9/22/2026\nMath\nDue: Weekly practice:\n"
)


def test_a_choice_survives_a_page_returned_for_an_unanswered_question(
    tmp_path: pathlib.Path,
) -> None:
    """The first card is changed to Task and the page is saved with the second card's
    question left open, twice. Each returned page shows Task chosen and knows it was a
    choice; answering the question then saves a task, the next preview shows Task, the
    same form sent again changes nothing, and the row is a task after a restart."""
    settings = settings_in(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN) as client:
        preview = client.post("/parent/inbox/read", data={"text": TWO_WEEKS_OF_PRACTICE}).text
        form = {**review_form(preview), "kind-0": "TASK"}
        returned = client.post("/parent/inbox/keep", data=form)
        again = client.post("/parent/inbox/keep", data=review_form(returned.text))
        answered = {**review_form(again.text), "occurrence-1": "update"}
        saved = client.post("/parent/inbox/keep", data=answered)
        rows = state_of(client).project_state.all_assignments()
        later = client.post(
            "/parent/inbox/read", data={"text": TWO_WEEKS_OF_PRACTICE.split("\n\n")[1]}
        ).text
        replayed = client.post("/parent/inbox/keep", data=answered)
    with TestClient(create_app(settings), headers=SAME_ORIGIN) as restarted:
        after_restart = state_of(restarted).project_state.all_assignments()

    assert review_form(preview)["suggested-0"] == "HOMEWORK"
    assert "asked-1" in review_form(preview)
    for page in (returned, again):
        assert page.status_code == 200
        assert NEEDS_ANSWER in page.text
        assert LOOK_AGAIN not in page.text
        fields = review_form(page.text)
        assert (fields["kind-0"], fields["suggested-0"]) == ("TASK", "HOMEWORK")
        assert "asked-1" in fields
        assert "The type becomes task, as chosen." in article_for(
            page.text, "<h2>Weekly practice</h2>"
        )
    assert saved.headers["location"] == "/parent?added=1&updated=0&unchanged=0"
    assert [(row.due_date, row.kind) for row in rows] == [(date(2026, 9, 15), AssignmentKind.TASK)]
    assert rows[0].origins["kind"] is SourceChannel.PARENT_ENTRY
    assert review_form(later)["kind-0"] == "TASK"
    assert review_form(later)["suggested-0"] == "TASK"
    assert replayed.headers["location"] == "/parent?added=0&updated=0&unchanged=1"
    assert after_restart == rows


def test_a_folded_cards_answers_travel_with_a_returned_page(tmp_path: pathlib.Path) -> None:
    """Three cards a week apart. The second is folded into the first with Task chosen on
    it; the third's question is left open. The returned page carries the fold and the
    choice as hidden fields, and answering the third saves one task with both dates and
    one new row, nothing lost."""
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        preview = client.post("/parent/inbox/read", data={"text": THREE_WEEKS_OF_PRACTICE}).text
        first = {**review_form(preview), "occurrence-1": "update", "kind-1": "TASK"}
        returned = client.post("/parent/inbox/keep", data=first)
        carried = review_form(returned.text)
        saved = client.post("/parent/inbox/keep", data={**carried, "occurrence-2": "new"})
        state = state_of(client)
        rows = by_due(state.project_state.all_assignments())
        claims = state.project_state.deadline_records(rows[0].assignment_id)

    assert {"asked-1", "asked-2"} <= set(review_form(preview))
    assert returned.status_code == 200
    assert NEEDS_ANSWER in returned.text
    assert "1 card folded into the card for the same assignment, as you said." in returned.text
    assert (carried["occurrence-1"], carried["kind-1"], carried["suggested-1"]) == (
        "update",
        "TASK",
        "HOMEWORK",
    )
    assert carried["kind-0"] == "TASK"
    assert "asked-2" in carried
    assert "<h2>Weekly practice</h2>" in returned.text
    assert returned.text.count("<h2>Weekly practice</h2>") == 2
    assert saved.headers["location"] == "/parent?added=2&updated=0&unchanged=0"
    assert [(row.due_date, row.kind) for row in rows] == [
        (date(2026, 9, 15), AssignmentKind.TASK),
        (date(2026, 9, 22), AssignmentKind.HOMEWORK),
    ]
    assert rows[0].origins["kind"] is SourceChannel.PARENT_ENTRY
    assert [said.asserted_value for said in claims] == ["2026-09-08", "2026-09-15"]


def test_a_change_back_to_the_suggested_type_is_a_choice_too(tmp_path: pathlib.Path) -> None:
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        client.post(
            "/parent/inbox/keep",
            data={"text": SAVED_WEEK, "kind-0": "TASK", "suggested-0": "HOMEWORK"},
        )
        preview = client.post("/parent/inbox/read", data={"text": SAVED_WEEK}).text
        back = client.post(
            "/parent/inbox/keep", data={**review_form(preview), "kind-0": "HOMEWORK"}
        )
        row = state_of(client).project_state.all_assignments()[0]

    assert review_form(preview)["suggested-0"] == "TASK"
    assert back.headers["location"] == "/parent?added=0&updated=1&unchanged=0"
    assert row.kind is AssignmentKind.HOMEWORK
    assert row.origins["kind"] is SourceChannel.PARENT_ENTRY


def test_a_question_the_record_raised_since_the_page_is_said_apart(
    tmp_path: pathlib.Path,
) -> None:
    """A page with no question is made; another saving puts the week before on record; the
    page sent back now meets a question it never put, and says the record changed."""
    later_week = TWO_WEEKS_OF_PRACTICE.split("\n\n")[1]
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        preview = client.post("/parent/inbox/read", data={"text": later_week}).text
        client.post("/parent/inbox/keep", data={"text": SAVED_WEEK})
        returned = client.post("/parent/inbox/keep", data=review_form(preview))

    assert "asked-0" not in review_form(preview)
    assert returned.status_code == 200
    assert LOOK_AGAIN in returned.text
    assert NEEDS_ANSWER not in returned.text


def test_a_select_left_as_the_page_showed_it_is_no_answer(tmp_path: pathlib.Path) -> None:
    """A page is made; the row is corrected from elsewhere; the page's form, sent back with
    its select untouched, changes nothing, and the correction stands. The notice for cards
    choosing different types is the page's own words."""
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        client.post("/parent/inbox/keep", data={"text": SAVED_WEEK})
        stale_page = client.post("/parent/inbox/read", data={"text": SAVED_WEEK}).text
        client.post(
            "/parent/inbox/keep",
            data={"text": SAVED_WEEK, "kind-0": "TASK", "suggested-0": "HOMEWORK"},
        )
        stale = client.post("/parent/inbox/keep", data=review_form(stale_page))
        rows = state_of(client).project_state.all_assignments()

    assert review_form(stale_page)["kind-0"] == "HOMEWORK"
    assert review_form(stale_page)["shown-0"] == "HOMEWORK"
    assert stale.headers["location"] == "/parent?added=0&updated=0&unchanged=1"
    assert rows[0].kind is AssignmentKind.TASK
    assert "Pick one type for it" in CHOOSE_ONE_TYPE


FOUR_WEEKS_OF_PRACTICE = (
    THREE_WEEKS_OF_PRACTICE + "\nTuesday 9/29/2026\nMath\nDue: Weekly practice:\n"
)


def test_an_edit_to_the_card_shown_stands_over_a_folded_cards_choice(
    tmp_path: pathlib.Path,
) -> None:
    """Task is chosen on the second card, folded into the first; the page comes back for
    the third card's question, twice, showing the merged card as a task. The parent sets
    that card back to Homework and answers. The row is homework, as the parent's own
    choice; the next preview shows Homework; the same form sent again changes nothing; and
    a restart finds the same rows and claims."""
    settings = settings_in(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN) as client:
        preview = client.post("/parent/inbox/read", data={"text": THREE_WEEKS_OF_PRACTICE}).text
        first = {**review_form(preview), "occurrence-1": "update", "kind-1": "TASK"}
        returned = client.post("/parent/inbox/keep", data=first)
        again = client.post("/parent/inbox/keep", data=review_form(returned.text))
        revised = {**review_form(again.text), "kind-0": "HOMEWORK", "occurrence-2": "new"}
        saved = client.post("/parent/inbox/keep", data=revised)
        state = state_of(client)
        rows = by_due(state.project_state.all_assignments())
        claims = [state.project_state.deadline_records(row.assignment_id) for row in rows]
        later = client.post(
            "/parent/inbox/read", data={"text": THREE_WEEKS_OF_PRACTICE.split("\n\n")[1]}
        ).text
        replayed = client.post("/parent/inbox/keep", data=revised)
    with TestClient(create_app(settings), headers=SAME_ORIGIN) as restarted:
        after_restart = by_due(state_of(restarted).project_state.all_assignments())

    for page in (returned, again):
        fields = review_form(page.text)
        assert (fields["kind-0"], fields["shown-0"], fields["suggested-0"]) == (
            "TASK",
            "TASK",
            "HOMEWORK",
        )
        assert (fields["suggested-1"], fields["folded-1"]) == ("HOMEWORK", "0")
        assert "asked-2" in fields
        assert "1 card folded into the card for the same assignment, as you said." in page.text
    assert saved.headers["location"] == "/parent?added=2&updated=0&unchanged=0"
    assert [(row.due_date, row.kind) for row in rows] == [
        (date(2026, 9, 15), AssignmentKind.HOMEWORK),
        (date(2026, 9, 22), AssignmentKind.HOMEWORK),
    ]
    assert rows[0].origins["kind"] is SourceChannel.PARENT_ENTRY
    assert rows[1].origins["kind"] is SourceChannel.LMS
    assert [[said.asserted_value for said in each] for each in claims] == [
        ["2026-09-08", "2026-09-15"],
        ["2026-09-22"],
    ]
    assert (review_form(later)["kind-0"], review_form(later)["suggested-0"]) == (
        "HOMEWORK",
        "HOMEWORK",
    )
    assert replayed.headers["location"] == "/parent?added=0&updated=0&unchanged=2"
    assert after_restart == rows


def test_a_change_back_to_the_suggestion_stays_the_parents_through_another_return(
    tmp_path: pathlib.Path,
) -> None:
    """Task is chosen on the second card and folded; the page comes back; the merged card
    is set back to Homework and the page comes back again, unanswered; the question is
    answered on that page. The row is homework and the type is the parent's, as it would
    be had the answer come with the change; the second return costs nothing. A card left as
    suggested through the same returns stays the school's."""
    settings = settings_in(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN) as client:
        preview = client.post("/parent/inbox/read", data={"text": THREE_WEEKS_OF_PRACTICE}).text
        first = {**review_form(preview), "occurrence-1": "update", "kind-1": "TASK"}
        returned = client.post("/parent/inbox/keep", data=first)
        reverted = client.post(
            "/parent/inbox/keep", data={**review_form(returned.text), "kind-0": "HOMEWORK"}
        )
        carried = review_form(reverted.text)
        saved = client.post("/parent/inbox/keep", data={**carried, "occurrence-2": "new"})
        rows = by_due(state_of(client).project_state.all_assignments())
    with TestClient(create_app(settings), headers=SAME_ORIGIN) as restarted:
        after_restart = by_due(state_of(restarted).project_state.all_assignments())
    untouched = settings_in(tmp_path / "untouched")
    with TestClient(create_app(untouched), follow_redirects=False, headers=SAME_ORIGIN) as client:
        preview = client.post("/parent/inbox/read", data={"text": THREE_WEEKS_OF_PRACTICE}).text
        once = client.post(
            "/parent/inbox/keep", data={**review_form(preview), "occurrence-1": "update"}
        )
        twice = client.post("/parent/inbox/keep", data=review_form(once.text))
        left = review_form(twice.text)
        saved_untouched = client.post("/parent/inbox/keep", data={**left, "occurrence-2": "new"})
        untouched_rows = by_due(state_of(client).project_state.all_assignments())

    assert reverted.status_code == 200
    assert (carried["kind-0"], carried["shown-0"], carried["suggested-0"]) == (
        "HOMEWORK",
        "HOMEWORK",
        "HOMEWORK",
    )
    assert carried["chosen-0"] == "1"
    assert saved.headers["location"] == "/parent?added=2&updated=0&unchanged=0"
    assert [(row.due_date, row.kind) for row in rows] == [
        (date(2026, 9, 15), AssignmentKind.HOMEWORK),
        (date(2026, 9, 22), AssignmentKind.HOMEWORK),
    ]
    assert rows[0].origins["kind"] is SourceChannel.PARENT_ENTRY
    assert after_restart == rows
    assert "chosen-0" not in left
    assert saved_untouched.headers["location"] == "/parent?added=2&updated=0&unchanged=0"
    assert [row.origins["kind"] for row in untouched_rows] == [
        SourceChannel.LMS,
        SourceChannel.LMS,
    ]


def test_the_card_shown_can_be_set_after_a_fold_left_it_as_suggested(
    tmp_path: pathlib.Path,
) -> None:
    """The second card is folded with its type left as suggested; the page comes back for
    the third card's question; the parent sets the merged card to Task. The row is a task."""
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        preview = client.post("/parent/inbox/read", data={"text": THREE_WEEKS_OF_PRACTICE}).text
        returned = client.post(
            "/parent/inbox/keep", data={**review_form(preview), "occurrence-1": "update"}
        )
        chosen = {**review_form(returned.text), "kind-0": "TASK", "occurrence-2": "new"}
        saved = client.post("/parent/inbox/keep", data=chosen)
        rows = by_due(state_of(client).project_state.all_assignments())

    assert review_form(returned.text)["kind-0"] == "HOMEWORK"
    assert saved.headers["location"] == "/parent?added=2&updated=0&unchanged=0"
    assert [(row.due_date, row.kind) for row in rows] == [
        (date(2026, 9, 15), AssignmentKind.TASK),
        (date(2026, 9, 22), AssignmentKind.HOMEWORK),
    ]
    assert rows[0].origins["kind"] is SourceChannel.PARENT_ENTRY


def test_two_folded_cards_cannot_override_the_card_shown(tmp_path: pathlib.Path) -> None:
    """Four cards a week apart: the second, with Task chosen, and the third are folded into
    the first, and the fourth's question is left open. The page comes back with the merged
    card as a task and both folded cards' answers hidden; the parent sets the merged card
    back to Homework and answers. Homework it is, with every date."""
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        preview = client.post("/parent/inbox/read", data={"text": FOUR_WEEKS_OF_PRACTICE}).text
        first = {
            **review_form(preview),
            "occurrence-1": "update",
            "kind-1": "TASK",
            "occurrence-2": "update",
        }
        returned = client.post("/parent/inbox/keep", data=first)
        carried = review_form(returned.text)
        saved = client.post(
            "/parent/inbox/keep", data={**carried, "kind-0": "HOMEWORK", "occurrence-3": "new"}
        )
        state = state_of(client)
        rows = by_due(state.project_state.all_assignments())
        claims = state.project_state.deadline_records(rows[0].assignment_id)

    assert {"asked-1", "asked-2", "asked-3"} <= set(review_form(preview))
    assert "2 cards folded into the card for the same assignment, as you said." in returned.text
    assert (carried["kind-0"], carried["shown-0"]) == ("TASK", "TASK")
    assert (carried["kind-1"], carried["kind-2"]) == ("TASK", "HOMEWORK")
    assert carried["occurrence-2"] == "update"
    assert saved.headers["location"] == "/parent?added=2&updated=0&unchanged=0"
    assert [(row.due_date, row.kind) for row in rows] == [
        (date(2026, 9, 22), AssignmentKind.HOMEWORK),
        (date(2026, 9, 29), AssignmentKind.HOMEWORK),
    ]
    assert rows[0].origins["kind"] is SourceChannel.PARENT_ENTRY
    assert [said.asserted_value for said in claims] == ["2026-09-08", "2026-09-15", "2026-09-22"]


def test_an_entry_that_leaves_the_type_as_it_is_keeps_a_saved_task_a_task(
    tmp_path: pathlib.Path,
) -> None:
    """The entry form promises that only the course and title are required: an entry that
    adds a note to a saved task, its type left as it is, leaves the task a task."""
    entry = {"course": "Religion", "title": "Syllabus", "note": "Bring it Monday."}
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        family = client.get("/parent", headers=PAGE).text
        client.post(
            "/parent/inbox/keep", data={"text": "Tuesday 9/8/2026\nReligion\nDue: Syllabus:\n"}
        )
        shown = client.post("/parent/inbox/enter", data=entry).text
        kept = client.post("/parent/inbox/keep", data=entry)
        row = state_of(client).project_state.all_assignments()[0]

    assert '<option value="" selected>Leave as it is</option>' in family
    assert '<label for="entry-kind">Type (optional)</label>' in family
    assert '<span class="pill">Saved; adds the note</span>' in shown
    assert 'data-saved="TASK"' in shown
    assert '<option value="TASK" selected>Task</option>' in shown
    assert kept.headers["location"] == "/parent?added=0&updated=1&unchanged=0"
    assert row.kind is AssignmentKind.TASK
    assert row.origins["kind"] is SourceChannel.LMS
    assert row.note == "Bring it Monday."


def test_a_question_answered_rides_along_on_a_page_returned_for_another(
    tmp_path: pathlib.Path,
) -> None:
    """Two saved assignments come round again in one text, two questions. One is answered
    and the page comes back for the other; the answer given is on that page as a hidden
    field, so answering the second saves both as the parent said."""
    saved = "Tuesday 9/1/2026\nMath\nDue: Weekly practice:\nMath\nDue: Reading log:\n"
    again = "Tuesday 9/8/2026\nMath\nDue: Weekly practice:\nMath\nDue: Reading log:\n"
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        client.post("/parent/inbox/keep", data={"text": saved})
        preview = client.post("/parent/inbox/read", data={"text": again}).text
        one_answered = {**review_form(preview), "occurrence-0": "update"}
        returned = client.post("/parent/inbox/keep", data=one_answered)
        carried = review_form(returned.text)
        both = client.post("/parent/inbox/keep", data={**carried, "occurrence-1": "new"})
        rows = by_due(state_of(client).project_state.all_assignments())

    assert {"asked-0", "asked-1"} <= set(review_form(preview))
    assert returned.status_code == 200
    assert NEEDS_ANSWER in returned.text
    assert carried["occurrence-0"] == "update"
    assert "asked-0" not in carried
    assert "asked-1" in carried
    assert '<span class="pill">Saved; adds the due date</span>' in returned.text
    assert both.headers["location"] == "/parent?added=1&updated=1&unchanged=0"
    assert [(row.title, row.due_date) for row in rows] == [
        ("Reading log", date(2026, 9, 1)),
        ("Reading log", date(2026, 9, 8)),
        ("Weekly practice", date(2026, 9, 8)),
    ]


def test_an_entered_date_beside_a_saved_one_is_called_entered(tmp_path: pathlib.Path) -> None:
    entry = {"course": "Math", "title": "Weekly practice", "due_date": "2026-09-03"}
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        client.post("/parent/inbox/keep", data={"text": SAVED_WEEK})
        shown = client.post("/parent/inbox/enter", data=entry).text

    assert "<dt>Saved due date</dt>" in shown
    assert "<dt>Entered due date</dt>" in shown
    assert "Pasted due date" not in shown
    assert "The entered date is added as evidence beside the saved date" in shown


def test_what_the_school_reports_is_shown_on_both_pages_with_its_source_and_day(
    tmp_path: pathlib.Path,
) -> None:
    """The email's "Missing" is kept as a report with the day and the email's own date text,
    shown on both pages as a fact the school reported, and never as a due date."""
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        client.post("/parent/inbox/keep", data={"text": SUMMARY})
        shown = client.post("/parent/inbox/read", data={"text": EMAIL}).text
        kept = client.post("/parent/inbox/keep", data={"text": EMAIL})
        family = client.get(kept.headers["location"], headers=PAGE).text
        hers = client.get("/student/due-this-week", headers=PAGE).text

    assert '<span class="pill">School reported: missing</span>' in shown
    assert "1 assignment with no due date yet:" in shown
    assert '<span class="pill">Saved; adds what the school reports</span>' in shown
    assert "<h2>No due date yet</h2>" in shown
    assert (
        "From the school email, pasted Monday, September 7, 2026. The email writes 09/09 "
        "beside it, which it does not explain." in shown
    )
    assert "What the school reports is saved with the day." in shown
    assert kept.headers["location"] == "/parent?added=0&updated=1&unchanged=0"
    assert "<h2>Assignment updates</h2>" in family
    assert "<h3>School reports</h3>" in family
    assert "Worth checking together" not in family
    assert SCHOOL_SAYS_MISSING.search(family)
    assert "From the school email, pasted Monday, September 7, 2026." in family
    assert "<strong>The school reports this missing.</strong>" in hers
    assert "From the school email, pasted Monday, September 7, 2026." in hers
    assert "Reported status: missing" in hers
    assert "Due Tuesday, September 8\n" in article_for(hers, "Book Covers")


def test_a_text_with_the_email_and_the_page_keeps_the_teachers_words_as_the_teachers(
    tmp_path: pathlib.Path,
) -> None:
    """The email names the assignment first; the portal's card dates it and carries the
    instruction. Her page says the note is the teacher's and never that a parent entered
    the assignment."""
    mixed = (
        "Assignments:\n09/09 Math - A: Homework: Practice Grade: Missing\n\n"
        "Tuesday 9/8/2026\nMath\nAssigned: Practice: (Due:09/10/2026)\nBring the packet.\n"
    )
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        kept = client.post("/parent/inbox/keep", data={"text": mixed})
        hers = client.get("/student/due-this-week", headers=PAGE).text

    assert kept.headers["location"] == "/parent?added=1&updated=0&unchanged=0"
    card = article_for(hers, "<h2>Practice</h2>")
    assert "From the teacher: <q>Bring the packet.</q>" in card
    assert "<strong>The school reports this missing.</strong>" in card
    assert "Due Thursday, September 10" in card
    assert "A parent wrote" not in card
    assert "Entered by a parent." not in card


def test_a_saving_waits_while_a_decision_is_being_recorded(tmp_path: pathlib.Path) -> None:
    """A decision is checked against the week the plan was made from, which holds still
    until the decision lands; a saving during a decision waits the moment it takes."""
    with (
        TestClient(
            create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
        ) as client,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        state = state_of(client)
        portal = client.portal
        assert portal is not None
        portal.call(state.decision_lock.acquire)
        saving = pool.submit(client.post, "/parent/inbox/keep", data=ENTRY)
        _, still_waiting = wait([saving], timeout=0.3)
        rows_while_locked = state.project_state.all_assignments()
        portal.call(state.decision_lock.release)
        saved = saving.result(timeout=10)
        rows_after = state.project_state.all_assignments()

    assert saving in still_waiting
    assert rows_while_locked == []
    assert saved.status_code == 303
    assert saved.headers["location"] == "/parent?added=1&updated=0&unchanged=0"
    assert [row.title for row in rows_after] == ["Vocabulary list, unit two"]


def test_her_page_is_read_as_one_snapshot_of_the_record(tmp_path: pathlib.Path) -> None:
    """Her page reads the week, the reports, and the assignments while it holds the store,
    so a saving cannot land between two of its reads: while the store is held elsewhere,
    her page waits, and comes once it is let go."""
    with (
        TestClient(
            create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
        ) as client,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        client.post("/parent/inbox/keep", data={"text": SUMMARY})
        held = state_of(client).project_state.exclusively()
        held.__enter__()
        try:
            reading = pool.submit(client.get, "/student/due-this-week", headers=PAGE)
            _, still_waiting = wait([reading], timeout=0.3)
        finally:
            held.__exit__(None, None, None)
        page = reading.result(timeout=10)

    assert reading in still_waiting
    assert page.status_code == 200
    assert "Book Covers" in page.text


def test_the_family_page_reads_the_schools_reports_as_one_snapshot(
    tmp_path: pathlib.Path,
) -> None:
    """The family page reads the reports and the rows while it holds the store, as her page
    does: while the store is held elsewhere, the page waits, and comes once it is let go."""
    with (
        TestClient(
            create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
        ) as client,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        client.post("/parent/inbox/keep", data={"text": SUMMARY})
        client.post("/parent/inbox/keep", data={"text": EMAIL})
        held = state_of(client).project_state.exclusively()
        held.__enter__()
        try:
            reading = pool.submit(client.get, "/parent", headers=PAGE)
            _, still_waiting = wait([reading], timeout=0.3)
        finally:
            held.__exit__(None, None, None)
        page = reading.result(timeout=10)

    assert reading in still_waiting
    assert page.status_code == 200
    assert SCHOOL_SAYS_MISSING.search(page.text)


def test_a_review_that_spans_midnight_saves_the_day_the_page_said(
    tmp_path: pathlib.Path,
) -> None:
    """The report's day is the day the email was pasted, as the review page says; a save
    after midnight keeps that day, because the page carries the moment it was first read.
    A form without that moment is read as of now."""
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        client.post("/parent/inbox/keep", data={"text": SUMMARY})
        preview = client.post("/parent/inbox/read", data={"text": EMAIL}).text
        carried = review_form(preview)
        state = state_of(client)
        next_day = FrozenClock(datetime(2026, 9, 8, 20, 0, tzinfo=UTC), state.clock.zone)
        setattr(client.app.state, STATE_ATTRIBUTE, dataclasses.replace(state, clock=next_day))  # type: ignore[attr-defined]
        saved = client.post("/parent/inbox/keep", data=carried)
        family = client.get(saved.headers["location"], headers=PAGE).text
        fresh = client.post("/parent/inbox/read", data={"text": EMAIL}).text
        bare = client.post("/parent/inbox/keep", data={"text": EMAIL})
        covers = next(
            row for row in state.project_state.all_assignments() if row.title == "Book Covers"
        )
        reports = state.project_state.status_reports(covers.assignment_id)

    assert carried["read_on"] == "2026-09-07"
    assert "From the school email, pasted Monday, September 7, 2026." in preview
    assert saved.headers["location"] == "/parent?added=0&updated=1&unchanged=0"
    assert "From the school email, pasted Monday, September 7, 2026." in family
    assert review_form(fresh)["read_on"] == "2026-09-08"
    assert "From the school email, pasted Tuesday, September 8, 2026." in fresh
    assert bare.headers["location"] == "/parent?added=0&updated=1&unchanged=0"
    assert [report.reported_on for report in reports] == [date(2026, 9, 7), date(2026, 9, 8)]


def test_the_way_in_is_a_parents(tmp_path: pathlib.Path) -> None:
    settings = settings_in(
        tmp_path,
        BLOSSOM_STUDENT_PASSPHRASE=HERS,
        BLOSSOM_PARENT_PASSPHRASE=THEIRS,
    )
    with TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN) as client:
        came_in = client.post("/sign-in", data={"passphrase": HERS})
        hers = client.post("/parent/inbox/read", data={"text": SUMMARY}, headers=PAGE)
        her_keep = client.post("/parent/inbox/keep", data={"text": SUMMARY})
        her_edit = client.post("/parent/inbox/edit", data={"text": SUMMARY})

    assert COOKIE in came_in.cookies
    assert hers.status_code == 403
    assert her_keep.status_code == 403
    assert her_edit.status_code == 403


# ------------------------------------------------------------------ a claim that cannot be read


def sent_back(page: str) -> dict[str, str]:
    """The review form as a browser sends it back, for a paste or an entry alike: the hidden
    fields and each select's chosen option, with the page's escaping undone."""
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', page))
    for name, body in re.findall(r'<select name="([^"]+)"[^>]*>(.*?)</select>', page, re.S):
        chosen = re.search(r'<option value="([^"]+)" selected>', body)
        assert chosen is not None, name
        fields[name] = chosen.group(1)
    text = re.search(r'<textarea name="text" hidden>(.*?)</textarea>', page, re.S)
    if text is not None:
        fields["text"] = text.group(1)
    return {name: html.unescape(value) for name, value in fields.items()}


def claim_damaged(client: TestClient, title: str, flag: object = 2) -> Assignment:
    """A claim about the date of the assignment on record under ``title``, its flag made
    into what the store never writes, so the strict readers refuse it."""
    state = state_of(client)
    row = next(item for item in state.project_state.all_assignments() if item.title == title)
    state.project_state.record_claims(
        row.assignment_id,
        [
            SourceRecord(
                channel=SourceChannel.EMAIL,
                asserted_value="2026-09-10",
                observed_at=state.clock.now(),
                confidence=0.8,
                seen_in="day header",
            )
        ],
    )
    connection = state.project_state._connection
    connection.execute(
        "UPDATE date_claims SET active = ? WHERE assignment_id = ?", (flag, row.assignment_id)
    )
    connection.commit()
    return row


def tables(client: TestClient) -> tuple[list[Assignment], list[tuple[object, ...]]]:
    """The assignments on record and every row about a date's claims, as they stand."""
    state = state_of(client)
    claims = state.project_state._connection.execute(
        "SELECT * FROM date_claims ORDER BY rowid"
    ).fetchall()
    return state.project_state.all_assignments(), [tuple(row) for row in claims]


@pytest.mark.parametrize("flag", [2, "broken", 0.5])
@pytest.mark.parametrize("press", ["enter", "keep"])
def test_a_claim_that_cannot_be_read_refuses_the_entry_with_everything_typed_kept(
    tmp_path: pathlib.Path, flag: object, press: str
) -> None:
    """A row about a date's claim that the store never writes refuses the entry's review and
    its save alike. The answer reads no store, says nothing was saved, and keeps every field
    typed, escaped, with one focused alert; the record is as it was."""
    typed = {**ENTRY, "note": "New <b>parent note</b>", "kind": "TASK"}
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        assert client.post("/parent/inbox/keep", data=ENTRY).status_code == 303
        form = dict(typed)
        if press == "keep":
            shown = client.post("/parent/inbox/enter", data=typed)
            assert shown.status_code == 200, shown.text[:300]
            form = sent_back(shown.text)
        claim_damaged(client, ENTRY["title"], flag)
        before = tables(client)
        answer = client.post(f"/parent/inbox/{press}", data=form)
        after = tables(client)
        family = client.get("/parent", headers=PAGE)

    assert answer.status_code == 500, answer.text[:300]
    assert answer.text.count(" autofocus") == 1
    assert CLAIM_UNREADABLE in answer.text
    assert "New &lt;b&gt;parent note&lt;/b&gt;" in answer.text
    for kept in ("Spanish", "Vocabulary list, unit two", "2026-09-11", "Kind, as typed"):
        assert kept in answer.text, kept
    assert "Task" in answer.text.split("Kind, as typed")[1].split("</p>")[0]
    assert "saved." not in answer.text.replace("nothing was saved.", "")
    assert after == before
    assert family.status_code == 200


@pytest.mark.parametrize("press", ["read", "keep"])
def test_a_claim_that_cannot_be_read_refuses_the_paste_with_the_whole_text_kept(
    tmp_path: pathlib.Path, press: str
) -> None:
    """The same refusal for a pasted text, on its review and on its save: the whole text is
    kept as pasted, escaped, and nothing is saved."""
    text = SUMMARY + "Keep <b>these words</b>" + chr(10)
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        assert client.post("/parent/inbox/keep", data={"text": SUMMARY}).status_code == 303
        form = {"text": text}
        if press == "keep":
            shown = client.post("/parent/inbox/read", data=form)
            assert shown.status_code == 200, shown.text[:300]
            form = sent_back(shown.text)
        claim_damaged(client, "Book Covers")
        before = tables(client)
        answer = client.post(f"/parent/inbox/{press}", data=form)
        after = tables(client)

    assert answer.status_code == 500, answer.text[:300]
    assert answer.text.count(" autofocus") == 1
    assert CLAIM_UNREADABLE in answer.text
    shown_back = answer.text.split('<textarea id="kept-paste" rows="8" readonly>')[1]
    assert shown_back.split("</textarea>")[0] == html.escape(text, quote=False)
    assert after == before


def test_the_refusal_keeps_the_answers_given_on_the_cards(tmp_path: pathlib.Path) -> None:
    """The answers made on the review's cards, new work or the same assignment and the type
    chosen, are said back as unsaved input beside the text."""
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        assert client.post("/parent/inbox/keep", data={"text": WEEKLY}).status_code == 303
        shown = client.post("/parent/inbox/read", data={"text": WEEKLY_AGAIN})
        assert shown.status_code == 200, shown.text[:300]
        form = sent_back(shown.text)
        asked = [name for name in form if name.startswith("asked-")]
        assert len(asked) == 1, asked
        key = asked[0].removeprefix("asked-")
        form[f"occurrence-{key}"] = "new"
        form[f"kind-{key}"] = "TASK"
        claim_damaged(client, "Weekly practice")
        answer = client.post("/parent/inbox/keep", data=form)

    assert answer.status_code == 500, answer.text[:300]
    assert f"Card {key}: new work under the same name; type Task." in answer.text
    assert html.escape(WEEKLY_AGAIN, quote=False) in answer.text


def test_a_claim_that_cannot_be_read_inside_the_write_rolls_it_back_and_keeps_the_entry(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal met inside the write's own transaction rolls the transaction back whole
    before the answer is made; the connection is left outside any transaction, the record
    is as it was, and the entry is kept."""
    typed = {**ENTRY, "note": "Transaction <b>words</b>"}
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        assert client.post("/parent/inbox/keep", data=ENTRY).status_code == 303
        shown = client.post("/parent/inbox/enter", data=typed)
        assert shown.status_code == 200, shown.text[:300]
        form = sent_back(shown.text)
        store = state_of(client).project_state
        real = store.deadline_records
        seen: list[bool] = []

        def refusing(assignment_id: str) -> list[SourceRecord]:
            seen.append(store._connection.in_transaction)
            if seen[-1]:
                raise UnreadableClaim(assignment_id)
            return real(assignment_id)

        monkeypatch.setattr(store, "deadline_records", refusing)
        before = tables(client)
        answer = client.post("/parent/inbox/keep", data=form)
        after = tables(client)
        settled = not store._connection.in_transaction

    assert True in seen, seen
    assert settled
    assert answer.status_code == 500, answer.text[:300]
    assert CLAIM_UNREADABLE in answer.text
    assert "Transaction &lt;b&gt;words&lt;/b&gt;" in answer.text
    assert after == before
