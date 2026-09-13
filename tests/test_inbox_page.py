"""The way in, on the family page: paste, review, save; type, review, save.

Nothing is written when the text is read; the review page says what saving
would do, week by week; the saving reads the text again and writes only
what the record lacks. A form that fails comes back with its fields as they
were. The text is synthetic, in the portal's shapes.
"""

import pathlib
from datetime import date

from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.dependencies import STATE_ATTRIBUTE, ApplicationState
from blossom.household import COOKIE
from blossom.intake import NOTE_MAX_LENGTH, TEXT_MAX_LENGTH
from blossom.reconciliation import SourceChannel
from blossom.routes.inbox import (
    FAR_DUE_DATE,
    LONG_NOTE,
    LOOK_AGAIN,
    NEEDS_COURSE,
    NEEDS_TITLE,
    NOT_A_DUE_DATE,
    NOT_A_KIND,
    NOTHING_PASTED,
    TOO_LONG,
)
from blossom.settings import Settings
from blossom.stores.project_state import AssignmentKind
from tests.support import fixture_settings

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


def test_the_family_page_offers_the_paste_box_and_the_entry_form(tmp_path: pathlib.Path) -> None:
    with TestClient(create_app(settings_in(tmp_path))) as client:
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
    with TestClient(create_app(settings_in(tmp_path)), follow_redirects=False) as client:
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
    assert '<p class="effect">Saved as a new assignment.</p>' in covers
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
    assert 'class="primary done" disabled aria-disabled="true">Save 0 assignments' in again


def test_an_entry_by_hand_is_reviewed_then_saved_as_the_familys_own(
    tmp_path: pathlib.Path,
) -> None:
    undated = {**ENTRY, "title": "Song lyrics, memorized", "due_date": "", "note": ""}
    with TestClient(create_app(settings_in(tmp_path)), follow_redirects=False) as client:
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
    with TestClient(create_app(settings_in(tmp_path)), follow_redirects=False) as client:
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

    assert empty.status_code == 422
    assert NOTHING_PASTED in empty.text
    assert 'id="paste-text" name="text"' in empty.text
    assert 'aria-invalid="true" aria-describedby="problem" autofocus' in empty.text
    assert '<a href="#entry-text">Go to the field.</a>' in empty.text
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


def test_text_that_cannot_be_read_is_listed_by_line_and_can_be_edited(
    tmp_path: pathlib.Path,
) -> None:
    text = "A stray line nobody expected\nMath - Assigned: Worksheet: (Due:TBD)\n"
    with TestClient(create_app(settings_in(tmp_path)), follow_redirects=False) as client:
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
    with TestClient(create_app(settings_in(tmp_path)), follow_redirects=False) as client:
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


def test_repeated_work_is_a_question_on_the_page_and_the_type_can_be_corrected(
    tmp_path: pathlib.Path,
) -> None:
    with TestClient(create_app(settings_in(tmp_path)), follow_redirects=False) as client:
        client.post("/parent/inbox/keep", data={"text": WEEKLY})
        asked = client.post("/parent/inbox/read", data={"text": WEEKLY_AGAIN}).text
        unanswered = client.post("/parent/inbox/keep", data={"text": WEEKLY_AGAIN})
        answered = client.post(
            "/parent/inbox/keep", data={"text": WEEKLY_AGAIN, "occurrence-0": "new"}
        )
        corrected = client.post(
            "/parent/inbox/keep", data={"text": SUMMARY, "kind-0": "HOMEWORK", "kind-1": "TASK"}
        )
        rows = {row.title: row for row in state_of(client).project_state.all_assignments()}

    assert '<span class="pill">Needs your answer</span>' in asked
    assert "1 needs your answer." in asked
    assert "<legend>Which is it?</legend>" in asked
    assert '<input type="radio" name="occurrence-0" value="update">' in asked
    assert '<input type="radio" name="occurrence-0" value="new">' in asked
    assert "Answer the question above, then save." in asked
    assert unanswered.status_code == 200
    assert LOOK_AGAIN in unanswered.text
    assert answered.headers["location"] == "/parent?added=1&updated=0&unchanged=0"
    assert len([title for title in rows if title == "Weekly practice"]) == 1
    assert corrected.headers["location"] == "/parent?added=3&updated=0&unchanged=0"
    assert rows["Book Covers"].kind is AssignmentKind.HOMEWORK
    assert rows["Book Covers"].origins["kind"] is SourceChannel.PARENT_ENTRY
    assert rows["Summer Reading - Log"].kind is AssignmentKind.TASK
    assert rows["Binder, labeled dividers and lined paper check"].kind is AssignmentKind.TASK
    assert rows["Binder, labeled dividers and lined paper check"].origins["kind"] is (
        SourceChannel.LMS
    )


def test_what_the_school_reports_is_shown_on_both_pages_with_its_source_and_day(
    tmp_path: pathlib.Path,
) -> None:
    """The email's "Missing" is kept as a report with the day and the email's own date text,
    shown on both pages as a fact the school reported, and never as a due date."""
    with TestClient(create_app(settings_in(tmp_path)), follow_redirects=False) as client:
        client.post("/parent/inbox/keep", data={"text": SUMMARY})
        shown = client.post("/parent/inbox/read", data={"text": EMAIL}).text
        kept = client.post("/parent/inbox/keep", data={"text": EMAIL})
        family = client.get(kept.headers["location"], headers=PAGE).text
        hers = client.get("/student/due-this-week", headers=PAGE).text

    assert '<span class="pill">School reported: missing</span>' in shown
    assert '<span class="pill">Saved; adds what the school reports</span>' in shown
    assert "<h2>No due date yet</h2>" in shown
    assert (
        "From the school email, pasted Monday, September 7, 2026. The email writes 09/09 "
        "beside it, which it does not explain." in shown
    )
    assert "What the school reports is saved with the day." in shown
    assert kept.headers["location"] == "/parent?added=0&updated=1&unchanged=0"
    assert "<h2>Reported by the school</h2>" in family
    assert "<strong>Book Covers: the school reports it missing.</strong>" in family
    assert "From the school email, pasted Monday, September 7, 2026." in family
    assert "<strong>The school reports this missing.</strong>" in hers
    assert "From the school email, pasted Monday, September 7, 2026." in hers
    assert "Reported status: missing" in hers
    assert "Due Tuesday, September 8\n" in article_for(hers, "Book Covers")


def test_the_way_in_is_a_parents(tmp_path: pathlib.Path) -> None:
    settings = settings_in(
        tmp_path,
        BLOSSOM_STUDENT_PASSPHRASE=HERS,
        BLOSSOM_PARENT_PASSPHRASE=THEIRS,
    )
    with TestClient(create_app(settings), follow_redirects=False) as client:
        came_in = client.post("/sign-in", data={"passphrase": HERS})
        hers = client.post("/parent/inbox/read", data={"text": SUMMARY}, headers=PAGE)
        her_keep = client.post("/parent/inbox/keep", data={"text": SUMMARY})
        her_edit = client.post("/parent/inbox/edit", data={"text": SUMMARY})

    assert COOKIE in came_in.cookies
    assert hers.status_code == 403
    assert her_keep.status_code == 403
    assert her_edit.status_code == 403
