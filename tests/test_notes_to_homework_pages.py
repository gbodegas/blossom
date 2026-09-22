"""Adding details to a homework note and adding the note to homework, through the pages.

She may do both from her tree and a parent from the family's; the tree a press
comes through is what says whose way in it was. One page holds her words, the
details, and two buttons: save the details, or add the note to homework. With
homework of the same class and title already on record the second button asks
for a choice first, and a choice made on a page that is behind is put again.
"""

import pathlib
import re
import sqlite3
from datetime import date
from html import unescape

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom import intake
from blossom.app import create_app
from blossom.captures import (
    HOUSEHOLD,
    PARENT,
    STUDENT,
    derived_assignment_id,
    new_capture_id,
)
from blossom.reconciliation import SourceChannel
from blossom.routes.captures import (
    ADDED_TO_HOMEWORK,
    ALREADY_ADDED,
    DETAILS_SAVED,
    JOINED_TO_HOMEWORK,
    NOTE_ALREADY_SAVED,
    NOTE_CHANGED,
    NOTE_EDITED,
    NOTE_EDITED_IN_HOMEWORK,
    NOTE_GONE,
    NOTE_RESTORED_IN_HOMEWORK,
    NOTE_SAVED_EARLIER,
    NOTE_UNREADABLE,
    OUT_OF_THE_WINDOW,
)
from blossom.routes.navigation import (
    ADDED_NOTES_PAGE,
    NEW_NOTE_PAGE,
    NOTE_ACTIONS,
    NOTES_PAGE,
    note_action,
    note_add_action,
    note_add_href,
    note_details_action,
    note_href,
)
from blossom.routes.note_details import (
    ALREADY_IN_HOMEWORK,
    CHOICE_IS_PAST,
    CHOOSE_ABOUT_THESE,
    CLASS_NOT_OFFERED,
    CLASS_TYPED_AND_CHOSEN,
    NOT_SAVED,
    SEPARATE,
    choice_from,
    choice_value,
    details_date_controls_are_valid,
)
from blossom.routes.runs import plan_graphs
from blossom.routes.student import BAD_FORM, NOT_HERS_TO_UPDATE
from blossom.stores.project_state import Assignment, AssignmentKind, Saved, StatusReport
from tests.support import (
    FIXTURE_WEEK,
    HER_PAGE,
    HERS,
    PAGE_HEADERS,
    SAME_ORIGIN,
    THEIRS,
    Answer,
    Scripted,
    accepting,
    browser,
    fixture_week_plan,
    form_fields,
    scripted_graphs,
    signed_in_household,
    state_of,
    whole_form,
)

FAMILY = "/parent"
WORDS = "Geometry questions 4-8, heard from a classmate"
OTHER = "__another__"


def save_note(client: TestClient, text: str = WORDS) -> str:
    fields = form_fields(client.get(NEW_NOTE_PAGE).text, NOTE_ACTIONS)
    answer = client.post(
        NOTE_ACTIONS,
        data={**fields, "text": text, "course": "", "due_date": ""},
        headers=PAGE_HEADERS,
    )
    assert answer.status_code == 303, answer.text
    return fields["capture_id"]


def opened(client: TestClient, name: str, *, family: bool = False) -> dict[str, str]:
    page = client.get(note_add_href(name, family=family))
    assert page.status_code == 200, page.text
    return whole_form(page.text, note_add_action(name, family=family))


def typed(**given: str) -> dict[str, str]:
    return {
        "course_choice": OTHER,
        "course_other": "Geometry",
        "title": "Questions 4-8",
        "due_date": "",
        "kind": "HOMEWORK",
        "note": "",
        **given,
    }


def add(client: TestClient, name: str, form: dict[str, str], *, family: bool = False) -> Answer:
    return client.post(note_add_action(name, family=family), data=form, headers=PAGE_HEADERS)


def save_details(
    client: TestClient, name: str, form: dict[str, str], *, family: bool = False
) -> Answer:
    """The second button of the one form: a browser sends every field and changes only where
    the form goes."""
    return client.post(note_details_action(name, family=family), data=form, headers=PAGE_HEADERS)


def rows(client: TestClient) -> tuple[list[object], list[object], list[object]]:
    connection = state_of(client).project_state._connection
    return (
        connection.execute("SELECT * FROM assignments ORDER BY assignment_id").fetchall(),
        connection.execute("SELECT * FROM date_claims ORDER BY rowid").fetchall(),
        connection.execute("SELECT * FROM capture_events ORDER BY sequence").fetchall(),
    )


def radios(page: str) -> list[str]:
    """The value of every choice the page offers about homework already on record, as a
    browser would send it, in the order shown."""
    return [unescape(value) for value in re.findall(r'name="candidate" value="([^"]*)"', page)]


def spoken(day: date) -> str:
    return f"{day.strftime('%B')} {day.day}, {day.year}"


def on_record(
    client: TestClient,
    due: date,
    title: str = "Questions 4-8",
    named: str | None = None,
    kind: AssignmentKind = AssignmentKind.HOMEWORK,
) -> Assignment:
    row = Assignment(
        assignment_id=named or intake.identity("Geometry", title, due.isoformat()),
        course="Geometry",
        title=title,
        due_date=due,
        dependencies=[],
        reported_submission_status="not_started",
        origins={"record": SourceChannel.LMS},
        kind=kind,
    )
    state_of(client).project_state.put_on_record([row], {})
    return row


# ------------------------------------------------------------------ the way in, and the page


def test_the_notes_page_offers_adding_it_and_only_what_has_a_route() -> None:
    with browser() as client:
        name = save_note(client)
        page = client.get(note_href(name)).text

    assert f'href="{note_add_href(name)}"' in page
    assert ">Add it to homework</a>" in page
    assert "Link to homework already here" not in page
    assert "Search homework" not in page


def test_opening_the_page_writes_nothing_and_proposes_only_what_she_wrote() -> None:
    with browser() as client:
        before = rows(client)
        short = save_note(client, "Read pages 12 to 14")
        long = save_note(client, "First line" + chr(10) + "second line")
        first = client.get(note_add_href(short)).text
        second = client.get(note_add_href(long)).text
        after = rows(client)

    assert whole_form(first, note_add_action(short))["title"] == "Read pages 12 to 14"
    assert whole_form(second, note_add_action(long))["title"] == ""
    assert "First line" in second
    for page in (first, second):
        assert re.search(r'<option value="HOMEWORK"[^>]* selected', page)
        assert f'<option value="{OTHER}"' in page
        assert "These homework details can be used in making a plan." in page
        assert ">Add to homework</button>" in page
        assert ">Save details</button>" in page
    assert len(after[2]) == len(before[2]) + 2
    assert after[:2] == before[:2]


# ------------------------------------------------------------------ details


def test_details_are_saved_as_hers_and_her_words_are_as_they_were() -> None:
    with browser() as client:
        store = state_of(client).project_state
        name = save_note(client)
        answer = save_details(
            client, name, {**opened(client, name), **typed(due_date="2026-08-21")}
        )
        landed = client.get(answer.headers["location"]).text
        note = store.capture(name)

    assert answer.status_code == 303
    assert escape(DETAILS_SAVED) in landed
    assert note is not None
    assert (note.text, note.course, note.title, note.due_date) == (
        WORDS,
        "Geometry",
        "Questions 4-8",
        date(2026, 8, 21),
    )
    assert {by.authored_by for by in note.attribution.values()} == {HOUSEHOLD}
    assert {by.channel for by in note.attribution.values()} == {SourceChannel.STUDENT_REPORT}
    assert note.outstanding


def test_the_same_details_again_are_already_saved_and_write_nothing() -> None:
    with browser() as client:
        name = save_note(client)
        first = save_details(client, name, {**opened(client, name), **typed()})
        once = rows(client)
        again = save_details(client, name, {**opened(client, name), **typed()})
        landed = client.get(again.headers["location"]).text
        behind = save_details(client, name, {**opened(client, name), **typed(), "revision": "1"})
        after = rows(client)

    assert (first.status_code, again.status_code, behind.status_code) == (303, 303, 303)
    assert escape(NOTE_ALREADY_SAVED) in landed
    assert after == once


@pytest.mark.parametrize("button", ["add", "details"])
@pytest.mark.parametrize("readable", [True, False])
def test_a_press_through_the_other_persons_tree_writes_nothing_and_keeps_what_was_typed(
    readable: bool, button: str, tmp_path: pathlib.Path
) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client)
        her_form = opened(client, name)
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        form = {
            **her_form,
            **typed(title="Typed <i>by a parent</i>", due_date="2026-08-21", kind="TASK"),
        }
        if not readable:
            damaged(client, name)
        before = rows(client)
        send = add if button == "add" else save_details
        answer = send(client, name, form)
        after = rows(client)

    assert answer.status_code == 403
    assert answer.text.count(" autofocus") == 1
    assert escape(NOT_HERS_TO_UPDATE) in answer.text
    for kept in ("Typed &lt;i&gt;by a parent&lt;/i&gt;", "2026-08-21", "Kind, as chosen: Task"):
        assert kept in answer.text, kept
    assert ">Add to homework</button>" not in answer.text
    assert after == before


def test_a_parent_adds_a_detail_through_the_familys_tree_and_it_says_so(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client)
        save_details(client, name, {**opened(client, name), **typed()})
        her_form = opened(client, name)
        refused_there = add(client, name, her_form, family=True)
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        refused_here = add(client, name, her_form)
        form = {**opened(client, name, family=True), **typed(due_date="2026-08-21")}
        answer = save_details(client, name, form, family=True)
        note = state_of(client).project_state.capture(name)
        page = client.get(note_href(name)).text

    assert refused_there.status_code in (303, 401, 403)
    assert refused_here.status_code == 403
    assert escape(NOT_HERS_TO_UPDATE) in refused_here.text
    assert answer.status_code == 303
    assert note is not None
    assert (note.attribution["title"].authored_by, note.attribution["title"].channel) == (
        STUDENT,
        SourceChannel.STUDENT_REPORT,
    )
    assert (note.attribution["due_date"].authored_by, note.attribution["due_date"].channel) == (
        PARENT,
        SourceChannel.PARENT_ENTRY,
    )
    assert note.text == WORDS
    assert "added by a parent" in page
    by_the_parent = history_of(page).split("<li>")[-1]
    assert "Details saved" in by_the_parent
    assert "by a parent" in by_the_parent
    assert by_the_parent.count("set in this change") == 1
    assert "Due date given: August 21, 2026 <span" in " ".join(by_the_parent.split())


# ------------------------------------------------------------------ adding it to homework


def test_adding_it_makes_the_assignment_once_and_says_so_where_she_lands() -> None:
    with browser() as client:
        store = state_of(client).project_state
        name = save_note(client)
        form = {**opened(client, name), **typed(due_date="2026-08-21", note="Show the working.")}
        answer = add(client, name, form)
        landed = client.get(answer.headers["location"]).text
        again = add(client, name, form)
        landed_again = client.get(again.headers["location"]).text
        made = derived_assignment_id(name)
        kept = [item for item in store.all_assignments() if item.assignment_id == made]
        week = client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text
        details = client.get(f"/student/assignments/{made}").text

    assert answer.status_code == 303
    assert escape(ADDED_TO_HOMEWORK) in landed
    assert f'href="/student/assignments/{made}' in landed
    assert again.status_code == 303
    assert escape(ALREADY_ADDED) in landed_again
    assert len(kept) == 1
    assignment = kept[0]
    assert (assignment.course, assignment.title, assignment.note) == (
        "Geometry",
        "Questions 4-8",
        "Show the working.",
    )
    assert assignment.note_by == "student"
    assert WORDS not in (assignment.note or "")
    assert 'id="homework-notes"' not in week
    assert "You wrote: <q>Show the working.</q>" in details
    assert "From your homework note" in details
    assert escape(WORDS) in details
    assert f'href="{note_href(name)}"' in details


def test_an_assignment_due_outside_the_window_says_it_is_saved_and_not_in_tonights_plan() -> None:
    with browser() as client:
        name = save_note(client)
        answer = add(client, name, {**opened(client, name), **typed(due_date="2027-03-05")})
        landed = client.get(answer.headers["location"]).text

    assert escape(ADDED_TO_HOMEWORK) in landed
    assert escape(OUT_OF_THE_WINDOW) in landed


@pytest.mark.parametrize(
    ("given", "field"),
    [
        ({"course_other": "c" * 61}, "course_other"),
        ({"course_other": ""}, "course_other"),
        ({"course_choice": "", "course_other": ""}, "course"),
        ({"course_choice": "A class that is on no record", "course_other": ""}, "course"),
        ({"title": "t" * 201}, "title"),
        ({"title": ""}, "title"),
        ({"title": "two" + chr(10) + "lines"}, "title"),
        ({"note": "n" * 501}, "note"),
        ({"kind": "ESSAY"}, "kind"),
        ({"due_date": "next friday"}, "due_date"),
    ],
)
def test_a_detail_the_record_will_not_keep_is_said_beside_its_field_and_nothing_is_written(
    given: dict[str, str], field: str
) -> None:
    with browser() as client:
        name = save_note(client)
        before = rows(client)
        form = {**opened(client, name), **typed(note="Kept <b>note</b>"), **given}
        answer = add(client, name, form)
        after = rows(client)
        kept = whole_form(answer.text, note_add_action(name))

    assert answer.status_code == 422
    assert answer.text.count(" autofocus") == 1
    assert re.search(rf'<a href="#details-{field.replace("_", "-")}">', answer.text)
    assert after == before
    if field != "note":
        assert kept["note"] == "Kept <b>note</b>"
    if field == "due_date":
        assert kept["due_date"] == ""
        assert "next friday" in answer.text
        assert kept["date_pending"] == "1"


def test_a_day_that_was_refused_is_left_out_only_when_she_says_so() -> None:
    with browser() as client:
        store = state_of(client).project_state
        name = save_note(client)
        refused = add(client, name, {**opened(client, name), **typed(due_date="next friday")})
        marked = whole_form(refused.text, note_add_action(name))
        plain = add(client, name, marked)
        nothing = store.capture(name)
        chosen = add(
            client, name, {**whole_form(plain.text, note_add_action(name)), "without_date": "1"}
        )
        made = [
            item
            for item in store.all_assignments()
            if item.assignment_id == derived_assignment_id(name)
        ]

    assert plain.status_code == 422
    assert nothing is not None
    assert nothing.outstanding
    assert chosen.status_code == 303
    assert [item.due_date for item in made] == [None]


def test_the_date_fields_of_this_form_are_valid_only_as_a_page_renders_them() -> None:
    marked = {"date_pending": "1"}
    for rendered in (
        {},
        marked,
        {**marked, "date_refused": "next friday"},
        {**marked, "without_date": "1"},
        {**marked, "date_refused": "next friday", "without_date": "1"},
    ):
        assert details_date_controls_are_valid(rendered), rendered
    for never in (
        {"without_date": "1"},
        {"date_refused": "next friday"},
        {**marked, "without_date": "yes"},
        {"date_pending": "yes"},
        {**marked, "date_refused": "2026-08-21"},
        {**marked, "date_refused": " padded "},
    ):
        assert not details_date_controls_are_valid(never), never


@pytest.mark.parametrize("button", ["add", "details"])
@pytest.mark.parametrize("fault", ["unknown", "twice", "revision", "basis"])
def test_a_form_these_pages_did_not_make_writes_nothing_and_keeps_what_she_typed(
    fault: str, button: str
) -> None:
    with browser() as client:
        name = save_note(client)
        before = rows(client)
        form = {
            **opened(client, name),
            **typed(title="Kept title", due_date="2026-08-21", kind="TASK", note="Kept note"),
        }
        pairs = list(form.items())
        if fault == "unknown":
            pairs.append(("role", "parent"))
        elif fault == "twice":
            pairs.append(("title", "another"))
        elif fault == "basis":
            pairs = [(key, "no-digest" if key == "basis" else value) for key, value in pairs]
        else:
            pairs = [(key, "0" if key == "revision" else value) for key, value in pairs]
        action = note_add_action(name) if button == "add" else note_details_action(name)
        answer = client.post(
            action,
            content="&".join(f"{key}={value}".replace(" ", "+") for key, value in pairs),
            headers={**PAGE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
        after = rows(client)

    assert answer.status_code == 422
    assert escape(BAD_FORM) in answer.text
    kept = whole_form(answer.text, note_add_action(name))
    assert (kept["title"], kept["due_date"], kept["kind"], kept["note"]) == (
        "Kept title",
        "2026-08-21",
        "TASK",
        "Kept note",
    )
    assert (kept["course_choice"], kept["course_other"]) == (OTHER, "Geometry")
    assert after == before


@pytest.mark.parametrize("button", ["add", "details"])
@pytest.mark.parametrize("spelled", ["2026-08-25", "20260825", "2026-W35-2"])
def test_the_form_a_refusal_returns_saves_the_day_that_was_picked(
    spelled: str, button: str
) -> None:
    """The second request is the returned page's own form, with nothing typed again."""
    with browser() as client:
        store = state_of(client).project_state
        name = save_note(client)
        send = add if button == "add" else save_details
        sent = {**opened(client, name), **typed(due_date=spelled), "role": "parent"}
        refused = send(client, name, sent)
        returned = whole_form(refused.text, note_add_action(name))
        saved = send(client, name, returned)
        note = store.capture(name)

    assert refused.status_code == 422
    assert returned["due_date"] == "2026-08-25"
    assert "role" not in returned
    assert saved.status_code == 303
    assert note is not None
    assert note.due_date == date(2026, 8, 25)


@pytest.mark.parametrize("button", ["add", "details"])
def test_leaving_the_day_out_counts_only_in_the_save_it_was_ticked_for(button: str) -> None:
    with browser() as client:
        store = state_of(client).project_state
        name = save_note(client)
        send = add if button == "add" else save_details
        refused = send(client, name, {**opened(client, name), **typed(due_date="after my lesson")})
        marked = whole_form(refused.text, note_add_action(name))
        other = send(client, name, {**marked, "without_date": "1", "note": "n" * 501})
        carried = whole_form(other.text, note_add_action(name))
        unticked = send(client, name, {**carried, "note": "A note that fits"})
        asked = whole_form(unticked.text, note_add_action(name))
        saved = send(client, name, {**asked, "without_date": "1"})
        note = store.capture(name)

    assert (refused.status_code, other.status_code, unticked.status_code) == (422, 422, 422)
    assert carried["date_refused"] == "after my lesson"
    assert "without_date" not in carried
    assert "after my lesson" in unticked.text
    assert saved.status_code == 303
    assert note is not None
    assert note.due_date is None


def test_a_form_refused_whole_says_back_a_day_it_could_not_read() -> None:
    with browser() as client:
        name = save_note(client)
        form = {**opened(client, name), **typed(due_date="next friday"), "role": "parent"}
        answer = add(client, name, form)
        kept = whole_form(answer.text, note_add_action(name))

    assert answer.status_code == 422
    assert escape(BAD_FORM) in answer.text
    assert "next friday" in answer.text
    assert (kept["due_date"], kept["date_pending"], kept["date_refused"]) == (
        "",
        "1",
        "next friday",
    )


# ------------------------------------------------------------------ homework already on record


def test_homework_of_that_class_and_title_is_a_choice_she_makes_and_same_changes_none_of_it() -> (
    None
):
    with browser() as client:
        store = state_of(client).project_state
        target = on_record(client, date(2026, 8, 28))
        name = save_note(client)
        before = rows(client)
        unasked = add(client, name, {**opened(client, name), **typed(due_date="2026-08-21")})
        choices = whole_form(unasked.text, note_add_action(name))
        nothing = rows(client)
        assignments = rows(client)[0]
        joined = add(client, name, {**choices, "candidate": choice_value(target.assignment_id)})
        landed = client.get(joined.headers["location"]).text
        note = store.capture(name)
        afterwards = rows(client)

    assert unasked.status_code == 409
    assert unasked.text.count(" autofocus") == 1
    assert escape(CHOOSE_ABOUT_THESE) in unasked.text
    assert ">Same homework" in unasked.text
    assert ">Keep as a separate assignment" in unasked.text
    assert (choices["title"], choices["due_date"]) == ("Questions 4-8", "2026-08-21")
    assert nothing == before
    assert joined.status_code == 303
    assert escape(JOINED_TO_HOMEWORK) in landed
    assert afterwards[0] == assignments
    assert len(afterwards[1]) == len(nothing[1]) + 1
    assert note is not None
    assert note.assignment_id == target.assignment_id


def test_a_choice_is_kept_through_a_refusal_about_something_else_and_not_when_put_again() -> None:
    with browser() as client:
        shown = on_record(client, date(2026, 8, 28))
        name = save_note(client)
        asked = add(client, name, {**opened(client, name), **typed()})
        form = {
            **whole_form(asked.text, note_add_action(name)),
            "candidate": choice_value(shown.assignment_id),
        }
        too_long = add(client, name, {**form, "note": "n" * 501})
        kept = whole_form(too_long.text, note_add_action(name))
        on_record(client, date(2026, 9, 4))
        put_again = add(client, name, form)
        asked_again = whole_form(put_again.text, note_add_action(name))

    assert asked.status_code == 409
    assert too_long.status_code == 422
    assert kept["candidate"] == choice_value(shown.assignment_id)
    assert put_again.status_code == 409
    assert "candidate" not in asked_again
    assert "The choice made before was not saved" in put_again.text


# ------------------------------------------------------------------ the press that was accepted


ACCEPTED_HEADING = '<h3 class="update-heading">What it was added with</h3>'
DIFFERENTLY = [
    ("course_other", "Physics", 'Class, as typed: <span class="authored-text">Physics'),
    ("title", "Different work", "Different work"),
    ("due_date", "2026-08-26", "2026-08-26"),
    ("kind", "TASK", "Kind, as chosen: Task"),
    ("note", "Different instructions", "Different instructions"),
]


@pytest.mark.parametrize("family", [False, True])
@pytest.mark.parametrize(("field", "value", "kept"), DIFFERENTLY)
def test_a_second_form_with_anything_else_in_it_is_refused_with_both_shown(
    field: str, value: str, kept: str, family: bool
) -> None:
    with browser() as client:
        name = save_note(client)
        form = {
            **opened(client, name, family=family),
            **typed(due_date="2026-08-25", note="Accepted instructions"),
        }
        first = add(client, name, form, family=family)
        before = rows(client)
        other = add(client, name, {**form, field: value}, family=family)
        again = add(client, name, form, family=family)
        after = rows(client)

    assert first.status_code == 303
    assert other.status_code == 409, other.headers.get("location")
    assert other.text.count(" autofocus") == 1
    assert escape(ALREADY_IN_HOMEWORK) in other.text
    accepted = other.text.split(ACCEPTED_HEADING)[1].split("Your unsaved details")[0]
    for shown in ("Geometry", "Questions 4-8", "August 25, 2026", "Kind: Homework"):
        assert shown in accepted, shown
    assert "Accepted instructions" in accepted
    assert f'href="/student/assignments/{derived_assignment_id(name)}' in other.text
    assert kept in other.text.split("Your unsaved details")[1]
    assert ">Add to homework</button>" not in other.text
    assert again.status_code == 303
    assert "said=already" in again.headers["location"]
    assert after == before


def test_joining_again_with_another_note_about_the_work_is_refused_with_both_shown() -> None:
    with browser() as client:
        target = on_record(client, date(2026, 8, 28))
        name = save_note(client)
        shown = add(client, name, {**opened(client, name), **typed(note="Accepted instructions")})
        form = {
            **whole_form(shown.text, note_add_action(name)),
            "candidate": choice_value(target.assignment_id),
        }
        first = add(client, name, form)
        before = rows(client)
        other = add(client, name, {**form, "note": "A different second-device instruction"})
        as_separate = add(client, name, {**form, "candidate": SEPARATE})
        again = add(client, name, form)
        after = rows(client)

    assert (shown.status_code, first.status_code) == (409, 303)
    for refused in (other, as_separate):
        assert refused.status_code == 409
        assert escape(ALREADY_IN_HOMEWORK) in refused.text
        assert "Accepted instructions" in refused.text.split(ACCEPTED_HEADING)[1]
    assert "A different second-device instruction" in other.text
    assert "said=already" in again.headers["location"]
    assert after == before


# ------------------------------------------------------------------ what a choice is sent as


AWKWARD_IDS = [
    "separate",
    "same:separate",
    "with:a:colon",
    "unit/3 part?b#c",
    "caf\N{LATIN SMALL LETTER E WITH ACUTE} \N{SPARKLES}",
    "x" * 200,
]


@pytest.mark.parametrize("family", [False, True])
@pytest.mark.parametrize("named", AWKWARD_IDS)
def test_same_homework_names_the_assignment_whatever_its_id_is(named: str, family: bool) -> None:
    with browser() as client:
        store = state_of(client).project_state
        target = on_record(client, date(2026, 8, 28), named=named)
        name = save_note(client)
        shown = add(client, name, {**opened(client, name, family=family), **typed()}, family=family)
        form = whole_form(shown.text, note_add_action(name, family=family))
        offered = radios(shown.text)
        before = rows(client)[0]
        joined = add(client, name, {**form, "candidate": offered[0]}, family=family)
        note = store.capture(name)
        after = rows(client)[0]

    assert shown.status_code == 409
    assert offered == [choice_value(target.assignment_id), SEPARATE]
    assert choice_from(offered[0]) == ("same", target.assignment_id)
    assert joined.status_code == 303, joined.text
    assert "said=joined" in joined.headers["location"]
    assert note is not None
    assert note.assignment_id == named
    assert after == before


def test_keep_separate_beside_an_assignment_named_separate_makes_the_notes_own() -> None:
    with browser() as client:
        store = state_of(client).project_state
        on_record(client, date(2026, 8, 28), named="separate")
        name = save_note(client)
        shown = add(client, name, {**opened(client, name), **typed()})
        form = whole_form(shown.text, note_add_action(name))
        made = add(client, name, {**form, "candidate": radios(shown.text)[1]})
        note = store.capture(name)

    assert made.status_code == 303
    assert "said=added" in made.headers["location"]
    assert note is not None
    assert note.assignment_id == derived_assignment_id(name)


@pytest.mark.parametrize("button", ["add", "details"])
@pytest.mark.parametrize(
    "forged",
    ["a-bare-id", " same:padded", "same:padded ", "same:", "same:" + "x" * 201, "Same:x", "other"],
)
def test_a_choice_in_any_other_spelling_is_a_form_these_pages_did_not_make(
    forged: str, button: str
) -> None:
    with browser() as client:
        on_record(client, date(2026, 8, 28))
        name = save_note(client)
        form = {**opened(client, name), **typed(), "candidate": forged}
        before = rows(client)
        send = add if button == "add" else save_details
        answer = send(client, name, form)
        after = rows(client)

    assert choice_from(forged) is None
    assert answer.status_code == 422
    assert escape(BAD_FORM) in answer.text
    assert after == before


# ------------------------------------------------------------------ what is shown of a candidate


def school_said(client: TestClient, target: Assignment, status: str, day: date) -> None:
    state = state_of(client)
    state.project_state.record_status_reports(
        target.assignment_id,
        [
            StatusReport(
                status=status,
                channel=SourceChannel.LMS,
                reported_on=day,
                dated_by="the day it was pasted",
                observed_at=state.clock.now(),
            )
        ],
    )


def she_said(client: TestClient, target: Assignment, status: str) -> None:
    """Her update on the chain as it stands, so a second one lands after the first."""
    state = state_of(client)
    chain = state.project_state.student_report_chains([target.assignment_id])
    said = chain.get(target.assignment_id, [])
    saved = state.project_state.report_status(
        target.assignment_id,
        status,  # type: ignore[arg-type]
        None,
        expected_head=said[-1].report_id if said else None,
        now=state.clock.now(),
        today=state.clock.today(),
    )
    assert isinstance(saved, Saved), saved


def choice_section(page: str) -> str:
    return page.split('id="details-candidate"')[1].split("</fieldset>")[0]


@pytest.mark.parametrize("family", [False, True])
def test_a_candidate_says_what_she_and_the_school_currently_say_apart(family: bool) -> None:
    with browser() as client:
        today = state_of(client).clock.today()
        quiet = on_record(client, date(2026, 8, 28))
        busy = on_record(client, date(2026, 9, 4), kind=AssignmentKind.TASK)
        she_said(client, busy, "done")
        school_said(client, busy, "missing", today)
        name = save_note(client)
        shown = add(client, name, {**opened(client, name, family=family), **typed()}, family=family)
        rows_shown = choice_section(shown.text).split("<label>")[1:]
        she_said(client, busy, "not_yet")
        kept = {**opened(client, name, family=family), **typed()}
        assert save_details(client, name, kept, family=family).status_code == 303
        later = choice_section(client.get(note_add_href(name, family=family)).text)

    who = "She" if family else "You"
    assert shown.status_code == 409
    assert f"{who} said Not yet on {spoken(today)}" in later
    assert "said Done" not in later
    about = {unescape(row.split('value="')[1].split('"')[0]): row for row in rows_shown}
    said = about[choice_value(busy.assignment_id)]
    silent = about[choice_value(quiet.assignment_id)]
    assert f"{who} said Done on {spoken(today)}" in said
    assert f"The school portal reported missing on {spoken(today)}" in said
    assert "From the school portal" in said
    assert f"No update from {'her' if family else 'you'}" in silent
    assert "said Done" not in silent
    assert "Recorded status: not started" in silent
    assert said.count("Kind: Task.") == 1
    assert silent.count("Kind: Homework.") == 1


def test_two_candidates_alike_in_all_but_kind_are_told_apart_by_it() -> None:
    with browser() as client:
        on_record(client, date(2026, 8, 28), named="alike-homework")
        on_record(client, date(2026, 8, 28), named="alike-task", kind=AssignmentKind.TASK)
        name = save_note(client)
        shown = add(client, name, {**opened(client, name), **typed()})
        section = choice_section(shown.text)

    assert shown.status_code == 409
    assert radios(shown.text) == [
        choice_value("alike-homework"),
        choice_value("alike-task"),
        SEPARATE,
    ]
    facts = re.findall(r'<p class="note candidate-facts"[^>]*>(.*?)</p>', section)
    assert [line.split(".")[0] for line in facts] == ["Kind: Homework", "Kind: Task"]


CHANGES = ["she says done", "school says missing", "record source", "kind"]


@pytest.mark.parametrize("family", [False, True])
@pytest.mark.parametrize("change", CHANGES)
def test_a_fact_shown_about_a_candidate_that_changed_is_put_again_with_what_stands(
    change: str, family: bool
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        today = state_of(client).clock.today()
        target = on_record(client, date(2026, 8, 28))
        name = save_note(client)
        shown = add(client, name, {**opened(client, name, family=family), **typed()}, family=family)
        form = {
            **whole_form(shown.text, note_add_action(name, family=family)),
            "title": "Questions 4-8",
            "note": "Typed before it changed",
            "candidate": choice_value(target.assignment_id),
        }
        if change == "she says done":
            she_said(client, target, "done")
        elif change == "school says missing":
            school_said(client, target, "missing", today)
        elif change == "record source":
            moved = target.model_copy(update={"origins": {"record": SourceChannel.PARENT_ENTRY}})
            store.upsert_assignments([moved])
        else:
            store.upsert_assignments([target.model_copy(update={"kind": AssignmentKind.TASK})])
        before = rows(client)
        answer = add(client, name, form, family=family)
        after = rows(client)
        returned = whole_form(answer.text, note_add_action(name, family=family))
        joined = add(
            client,
            name,
            {**returned, "candidate": choice_value(target.assignment_id)},
            family=family,
        )

    now_shown = {
        "she says done": f"said Done on {spoken(today)}",
        "school says missing": f"reported missing on {spoken(today)}",
        "record source": "From the family entry",
        "kind": "Kind: Task.",
    }[change]
    assert answer.status_code == 409, answer.headers.get("location")
    assert escape(CHOOSE_ABOUT_THESE) in answer.text
    assert now_shown in choice_section(answer.text)
    assert returned["note"] == "Typed before it changed"
    assert "candidate" not in returned
    assert "The choice made before was not saved" in answer.text
    assert after == before
    assert joined.status_code == 303


@pytest.mark.parametrize("family", [False, True])
@pytest.mark.parametrize("button", ["add", "details"])
@pytest.mark.parametrize("which", ["same", "separate"])
def test_a_choice_made_is_still_made_after_a_refusal_about_something_else(
    which: str, button: str, family: bool
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        target = on_record(client, date(2026, 8, 28))
        name = save_note(client)
        fresh = client.get(note_add_href(name, family=family)).text
        shown = add(client, name, {**opened(client, name, family=family), **typed()}, family=family)
        chosen = radios(shown.text)[0 if which == "same" else 1]
        form = {**whole_form(shown.text, note_add_action(name, family=family)), "candidate": chosen}
        send = add if button == "add" else save_details
        refused = send(client, name, {**form, "note": "n" * 501}, family=family)
        returned = whole_form(refused.text, note_add_action(name, family=family))
        done = add(client, name, {**returned, "note": "A note that fits"}, family=family)
        note = store.capture(name)

    assert " checked" not in fresh
    assert refused.status_code == 422
    assert refused.text.count(" autofocus") == 1
    assert '<a href="#details-note">' in refused.text
    assert returned["candidate"] == chosen
    assert refused.text.count(" checked") == 1
    assert done.status_code == 303, done.text
    assert note is not None
    expected = target.assignment_id if which == "same" else derived_assignment_id(name)
    assert note.assignment_id == expected


@pytest.mark.parametrize("button", ["add", "details"])
@pytest.mark.parametrize("refusal", ["note", "revision", "basis"])
@pytest.mark.parametrize("which", ["same", "separate"])
def test_a_refusal_about_something_else_clears_a_choice_whose_facts_changed_and_says_so(
    which: str, refusal: str, button: str
) -> None:
    """A fact shown about a candidate changes after the page is opened. Whatever the press is
    then refused for, the choice made on the old facts is not kept chosen beside the rows as
    they stand: it is said back as unsaved, and the corrected press is asked for a choice."""
    with browser() as client:
        today = state_of(client).clock.today()
        target = on_record(client, date(2026, 8, 28))
        name = save_note(client)
        shown = add(client, name, {**opened(client, name), **typed()})
        form = whole_form(shown.text, note_add_action(name))
        chosen = radios(shown.text)[0 if which == "same" else 1]
        sent = {**form, "candidate": chosen}
        if refusal == "note":
            sent["note"] = "n" * 501
        elif refusal == "revision":
            sent["revision"] = "invalid"
        else:
            sent["basis"] = "invalid"
        she_said(client, target, "done")
        send = add if button == "add" else save_details
        refused = send(client, name, sent)
        returned = whole_form(refused.text, note_add_action(name))
        before = rows(client)
        corrected = add(client, name, {**returned, "note": "A note that fits"})
        after = rows(client)
        again = whole_form(corrected.text, note_add_action(name))
        chosen_again = add(client, name, {**again, "candidate": chosen, "note": "A note that fits"})

    assert shown.status_code == 409
    assert refused.status_code == 422
    assert " checked" not in refused.text
    assert "candidate" not in returned
    # The refused page carries the fingerprint of the rows as they stand, which is what the
    # store's own answer carries when it puts the choice again.
    assert returned["basis"] != form["basis"]
    assert returned["basis"] == again["basis"]
    assert "The choice made before was not saved" in refused.text
    assert f"said Done on {spoken(today)}" in choice_section(refused.text)
    assert corrected.status_code == 409, corrected.headers.get("location")
    assert escape(CHOOSE_ABOUT_THESE) in corrected.text
    assert after == before
    assert chosen_again.status_code == 303, chosen_again.text


def test_a_form_refused_whole_for_its_fingerprint_offers_a_fresh_choice() -> None:
    with browser() as client:
        target = on_record(client, date(2026, 8, 28))
        name = save_note(client)
        shown = add(client, name, {**opened(client, name), **typed()})
        form = whole_form(shown.text, note_add_action(name))
        forged = {**form, "candidate": choice_value(target.assignment_id), "basis": "no-digest"}
        before = rows(client)
        answer = add(client, name, forged)
        returned = whole_form(answer.text, note_add_action(name))
        after = rows(client)

    assert answer.status_code == 422
    assert escape(BAD_FORM) in answer.text
    assert returned["basis"] == form["basis"]
    assert "candidate" not in returned
    assert " checked" not in answer.text
    assert "The choice made before was not saved" in answer.text
    assert after == before


# ------------------------------------------------------------------ a class that left the list


@pytest.mark.parametrize("family", [False, True])
@pytest.mark.parametrize("button", ["add", "details"])
def test_a_class_chosen_from_the_list_that_left_the_record_stays_chosen_until_another_is(
    button: str, family: bool
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        target = on_record(client, date(2026, 8, 28), title="Another title")
        name = save_note(client)
        page = opened(client, name, family=family)
        sent = {**page, **typed(course_choice="Geometry", course_other="")}
        store.upsert_assignments([target.model_copy(update={"course": "Renamed class"})])
        before = rows(client)
        send = add if button == "add" else save_details
        refused = send(client, name, sent, family=family)
        returned = whole_form(refused.text, note_add_action(name, family=family))
        again = send(client, name, returned, family=family)
        still = rows(client)
        typed_in = send(
            client,
            name,
            {**returned, "course_choice": OTHER, "course_other": "Geometry"},
            family=family,
        )
        note = store.capture(name)

    assert refused.status_code == 422
    assert escape(CLASS_NOT_OFFERED) in refused.text
    assert '<a href="#details-course">' in refused.text
    assert refused.text.count(" autofocus") == 1
    assert returned["course_choice"] == "Geometry"
    assert "not in the list now" in refused.text
    assert '<option value="Renamed class"' in refused.text
    assert again.status_code == 422
    assert still == before
    assert typed_in.status_code == 303, typed_in.text
    assert note is not None
    assert note.course == "Geometry"


def test_the_class_chosen_before_is_kept_as_written_beside_a_class_typed() -> None:
    awkward = ('<b>Art</b> & "design" ' + "x" * 60)[:60]
    with browser() as client:
        store = state_of(client).project_state
        target = on_record(client, date(2026, 8, 28), title="Another title")
        store.upsert_assignments([target.model_copy(update={"course": awkward})])
        name = save_note(client)
        sent = {**opened(client, name), **typed(course_choice=awkward, course_other="Typed too")}
        store.upsert_assignments([target.model_copy(update={"course": "Renamed class"})])
        refused = add(client, name, sent)
        returned = whole_form(refused.text, note_add_action(name))

    assert len(awkward) == 60
    assert refused.status_code == 422
    assert escape(CLASS_TYPED_AND_CHOSEN) in refused.text
    assert returned["course_choice"] == awkward
    assert returned["course_other"] == "Typed too"
    assert "<b>Art</b>" not in refused.text
    assert str(escape(awkward)) in refused.text


def test_a_class_sent_that_could_not_be_offered_is_shown_as_words() -> None:
    with browser() as client:
        name = save_note(client)
        sent = {**opened(client, name), **typed(course_choice="y" * 61, course_other="")}
        refused = add(client, name, sent)
        returned = whole_form(refused.text, note_add_action(name))

    assert refused.status_code == 422
    assert "could not be offered as a choice" in refused.text
    assert "y" * 61 in refused.text
    assert returned["course_choice"] == ""


def test_keep_separate_makes_the_notes_own_assignment_beside_the_one_on_record() -> None:
    with browser() as client:
        store = state_of(client).project_state
        on_record(client, date(2026, 8, 28))
        name = save_note(client)
        unasked = add(client, name, {**opened(client, name), **typed()})
        choices = whole_form(unasked.text, note_add_action(name))
        separate = add(client, name, {**choices, "candidate": "separate"})
        ids = {item.assignment_id for item in store.all_assignments()}

    assert separate.status_code == 303
    assert derived_assignment_id(name) in ids


def test_homework_that_arrived_after_the_choices_were_shown_is_put_to_her_again() -> None:
    with browser() as client:
        shown = on_record(client, date(2026, 8, 28))
        name = save_note(client)
        unasked = add(client, name, {**opened(client, name), **typed()})
        choices = whole_form(unasked.text, note_add_action(name))
        arrived = on_record(client, date(2026, 9, 4))
        before = rows(client)
        answer = add(client, name, {**choices, "candidate": choice_value(shown.assignment_id)})
        after = rows(client)

    assert answer.status_code == 409
    assert escape(CHOOSE_ABOUT_THESE) in answer.text
    assert choice_value(arrived.assignment_id) in radios(answer.text)
    assert after == before


def test_a_page_that_is_behind_the_note_is_refused_with_what_she_typed() -> None:
    with browser() as client:
        name = save_note(client)
        form = {**opened(client, name), **typed(title="Typed on an old page")}
        save_details(client, name, {**opened(client, name), **typed(title="Saved elsewhere")})
        answer = add(client, name, form)

    assert answer.status_code == 409
    assert escape(NOTE_CHANGED) in answer.text
    assert whole_form(answer.text, note_add_action(name))["title"] == "Typed on an old page"
    assert "Saved on the note now" in answer.text
    assert "Saved elsewhere" in answer.text
    assert "Your unsaved details" in answer.text
    saved_now = answer.text.index("Saved on the note now")
    assert saved_now < answer.text.index("Your unsaved details")


def test_a_page_behind_a_change_of_kind_alone_is_shown_the_kind_that_stands() -> None:
    with browser() as client:
        name = save_note(client)
        only_kind = {"course_choice": "", "course_other": "", "title": "", "kind": "TASK"}
        stale = {**opened(client, name), **typed(kind="HOMEWORK")}
        saved = save_details(client, name, {**opened(client, name), **typed(**only_kind)})
        note = state_of(client).project_state.capture(name)
        answer = add(client, name, stale)
        shown = answer.text[: answer.text.index("Your unsaved details")]

    assert saved.status_code == 303
    assert note is not None
    assert (note.course, note.title, note.kind) == (None, None, "TASK")
    assert answer.status_code == 409
    assert "Saved on the note now" in shown
    assert "Kind: Task" in shown
    assert whole_form(answer.text, note_add_action(name))["kind"] == "HOMEWORK"


@pytest.mark.parametrize("sent", ["separate", "same:an-assignment-that-is-not-one-of-them"])
def test_a_choice_about_homework_that_is_not_there_adds_nothing_and_says_which_it_is(
    sent: str,
) -> None:
    with browser() as client:
        name = save_note(client)
        other = save_note(client, "A second note")
        on_record(client, date(2026, 8, 28), title="Another title")
        before = rows(client)
        none_here = add(client, name, {**opened(client, name), **typed(), "candidate": sent})
        shown = {**opened(client, other), **typed(title="Another title")}
        not_among = add(client, other, {**shown, "candidate": "same:an-assignment-that-is-not-one"})
        after = rows(client)

    assert none_here.status_code == 409
    assert escape(CHOICE_IS_PAST) in none_here.text
    assert escape(CHOOSE_ABOUT_THESE) not in none_here.text
    assert "The choice made before was not saved" in none_here.text
    assert "is not on record now" in none_here.text
    assert 'id="details-candidate"' not in none_here.text
    assert whole_form(none_here.text, note_add_action(name))["title"] == "Questions 4-8"
    assert not_among.status_code == 409
    assert escape(CHOOSE_ABOUT_THESE) in not_among.text
    assert ">Same homework" in not_among.text
    assert after == before


def test_a_note_put_away_while_its_details_were_typed_keeps_them_to_copy() -> None:
    with browser() as client:
        name = save_note(client)
        form = {**opened(client, name), **typed(title="Typed <i>before</i> it was archived")}
        put_away = client.post(
            note_action(name, "archive"), data={"revision": form["revision"]}, headers=PAGE_HEADERS
        )
        before = rows(client)
        answers = [add(client, name, form), save_details(client, name, form)]
        after = rows(client)

    assert put_away.status_code == 303
    for answer in answers:
        assert answer.status_code == 409
        assert answer.text.count(" autofocus") == 1
        assert escape(NOTE_CHANGED) in answer.text
        assert "Typed &lt;i&gt;before&lt;/i&gt; it was archived" in answer.text
        assert ">Add to homework</button>" not in answer.text
    assert after == before


def test_with_the_file_unreadable_the_plain_page_keeps_every_detail_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        name = save_note(client)
        form = {
            **opened(client, name),
            **typed(
                title="Typed <i>title</i>",
                note="Typed <b>note</b>",
                due_date="2026-08-21",
                kind="TASK",
            ),
        }
        before = rows(client)

        def refuses(*args: object, **kwargs: object) -> None:
            msg = "the file refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "_upsert_assignments_locked", refuses)
        monkeypatch.setattr(store, "sound_capture_history", refuses)
        answer = add(client, name, form)
        monkeypatch.undo()
        after = rows(client)

    assert answer.status_code == 500
    assert answer.text.count(" autofocus") == 1
    assert escape(NOT_SAVED) in answer.text
    for kept in ("Geometry", "Typed &lt;i&gt;title&lt;/i&gt;", "Typed &lt;b&gt;note&lt;/b&gt;"):
        assert kept in answer.text
    assert "2026-08-21" in answer.text
    assert "Kind, as chosen: Task" in answer.text
    assert f'href="{note_href(name)}"' in answer.text
    assert after == before


def damaged(client: TestClient, name: str, flag: object = 2) -> None:
    """Make the note one that cannot be read: a flag that is neither 0 nor 1."""
    connection = state_of(client).project_state._connection
    connection.execute(
        "UPDATE homework_captures SET archived = ? WHERE capture_id = ?", (flag, name)
    )
    connection.commit()


KEPT = ("Typed &lt;i&gt;title&lt;/i&gt;", "Typed &lt;b&gt;note&lt;/b&gt;", "2026-08-21", "Geometry")


@pytest.mark.parametrize("button", ["add", "details"])
@pytest.mark.parametrize("whole", [True, False])
def test_a_refused_form_for_a_note_that_cannot_be_read_keeps_every_detail(
    whole: bool, button: str
) -> None:
    with browser() as client:
        name = save_note(client)
        form = {
            **opened(client, name),
            **typed(
                title="Typed <i>title</i>",
                note="Typed <b>note</b>",
                due_date="2026-08-21",
                kind="TASK",
            ),
        }
        if not whole:
            form["role"] = "parent"
        damaged(client, name)
        before = rows(client)
        send = add if button == "add" else save_details
        answer = send(client, name, form)
        after = rows(client)

    assert answer.status_code == (500 if whole else 422)
    assert answer.text.count(" autofocus") == 1
    assert escape(NOTE_UNREADABLE) in answer.text
    for kept in (*KEPT, "Kind, as chosen: Task"):
        assert kept in answer.text, kept
    assert after == before


@pytest.mark.parametrize("button", ["add", "details"])
@pytest.mark.parametrize("whole", [True, False])
def test_a_refused_form_for_a_note_that_is_not_on_record_keeps_every_detail(
    whole: bool, button: str
) -> None:
    with browser() as client:
        name = save_note(client)
        form = {
            **opened(client, name),
            **typed(
                title="Typed <i>title</i>",
                note="Typed <b>note</b>",
                due_date="2026-08-21",
                kind="TASK",
            ),
        }
        if not whole:
            form["role"] = "parent"
        missing = new_capture_id()
        before = rows(client)
        send = add if button == "add" else save_details
        answer = send(client, missing, form)
        after = rows(client)

    assert answer.status_code == (404 if whole else 422)
    assert answer.text.count(" autofocus") == 1
    assert escape(NOTE_GONE) in answer.text
    for kept in (*KEPT, "Kind, as chosen: Task"):
        assert kept in answer.text, kept
    assert f'href="{note_href(missing)}"' not in answer.text
    assert after == before


@pytest.mark.parametrize("button", ["add", "details"])
def test_with_the_classes_unreadable_a_refusal_still_keeps_every_detail(
    button: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page that answers reads no store: with the first read refused, the record's
    connection runs no statement at all for the whole request."""
    with browser() as client:
        store = state_of(client).project_state
        name = save_note(client)
        form = {**opened(client, name), **typed(title="Typed <i>title</i>", kind="TASK")}
        before = rows(client)
        statements: list[str] = []

        def refuses(*args: object, **kwargs: object) -> None:
            msg = "the file refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "all_assignments", refuses)
        store._connection.set_trace_callback(statements.append)
        send = add if button == "add" else save_details
        answer = send(client, name, form)
        store._connection.set_trace_callback(None)
        monkeypatch.undo()
        after = rows(client)

    assert statements == []
    assert answer.status_code == 500
    assert answer.text.count(" autofocus") == 1
    assert escape(NOT_SAVED) in answer.text
    assert "Typed &lt;i&gt;title&lt;/i&gt;" in answer.text
    assert "Kind, as chosen: Task" in answer.text
    assert after == before


LONG = "W" * 150
AUTHORED = {
    "title": f"<b>bold</b> & \N{SPARKLES} {LONG}",
    "note": "First <i>line</i> \N{SPARKLES}" + chr(10) + "second & last line",
    "course_other": "<u>Art</u> & design \N{ARTIST PALETTE}",
}


@pytest.mark.parametrize("button", ["add", "details"])
def test_a_write_refused_and_a_page_unreadable_try_the_write_once_and_keep_what_was_authored(
    button: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        name = save_note(client)
        form = {**opened(client, name), **typed(**AUTHORED, due_date="2026-08-21", kind="TASK")}
        before = rows(client)
        tried: list[str] = []
        reading = store.sound_capture_history

        def refused_write(*args: object, **kwargs: object) -> None:
            tried.append("write")
            msg = "the file refused"
            raise sqlite3.OperationalError(msg)

        def reads_until_the_write(capture_id: str) -> object:
            if tried:
                msg = "the file refused"
                raise sqlite3.OperationalError(msg)
            return reading(capture_id)

        monkeypatch.setattr(store, "_change_locked", refused_write)
        monkeypatch.setattr(store, "sound_capture_history", reads_until_the_write)
        send = add if button == "add" else save_details
        answer = send(client, name, form)
        monkeypatch.undo()
        after = rows(client)

    assert tried == ["write"]
    assert answer.status_code == 500
    assert answer.text.count(" autofocus") == 1
    assert 'id="problem-summary"' in answer.text
    assert escape(NOT_SAVED) in answer.text
    assert str(escape(AUTHORED["title"])) in answer.text
    assert str(escape(AUTHORED["course_other"])) in answer.text
    kept_note = answer.text.split('id="kept-details-note"')[1].split("</textarea>")[0]
    assert str(escape("First <i>line</i>")) in kept_note
    assert chr(10) + "second &amp; last line" in kept_note
    assert "Kind, as chosen: Task" in answer.text
    assert "2026-08-21" in answer.text
    assert "<b>bold</b>" not in answer.text
    assert after == before


def test_the_kept_details_show_a_class_typed_beside_a_class_chosen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        on_record(client, date(2026, 8, 28), title="Another title")
        name = save_note(client)
        form = {
            **opened(client, name),
            **typed(course_choice="Geometry", course_other="Typed <i>class</i>"),
        }

        def refuses(*args: object, **kwargs: object) -> None:
            msg = "the file refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "sound_capture_history", refuses)
        answer = add(client, name, form)
        monkeypatch.undo()

    assert answer.status_code == 422
    assert "Class, as chosen: <span" in answer.text
    assert "Typed &lt;i&gt;class&lt;/i&gt;" in answer.text


def test_with_the_sign_in_off_the_familys_tree_records_a_family_entry_by_the_household() -> None:
    with browser() as client:
        store = state_of(client).project_state
        name = save_note(client)
        form = {**opened(client, name, family=True), **typed(due_date="2026-08-21")}
        answer = add(client, name, form, family=True)
        note = store.capture(name)
        made = [
            item
            for item in store.all_assignments()
            if item.assignment_id == derived_assignment_id(name)
        ]
        claims = store.deadline_records(derived_assignment_id(name))

    assert answer.status_code == 303
    assert note is not None
    assert {by.authored_by for by in note.attribution.values()} == {HOUSEHOLD}
    assert {by.channel for by in note.attribution.values()} == {SourceChannel.PARENT_ENTRY}
    assert [item.origins["record"] for item in made] == [SourceChannel.PARENT_ENTRY]
    assert [claim.channel for claim in claims] == [SourceChannel.PARENT_ENTRY]
    assert note.text == WORDS


def test_a_note_in_homework_leaves_the_waiting_list_and_stays_reachable_with_its_history() -> None:
    with browser() as client:
        name = save_note(client)
        waiting_note = save_note(client, "Still only a note")
        assert add(client, name, {**opened(client, name), **typed()}).status_code == 303
        waiting = client.get(NOTES_PAGE).text
        added = client.get(ADDED_NOTES_PAGE).text
        page = client.get(note_href(name)).text
        again = client.get(note_add_href(name)).text

    assert f'href="{note_href(name)}"' not in waiting
    assert f'href="{note_href(waiting_note)}"' in waiting
    assert f'href="{ADDED_NOTES_PAGE}"' in waiting
    assert f'href="{note_href(name)}"' in added
    assert f'href="{note_href(waiting_note)}"' not in added
    assert "In homework" in page
    assert "Added to homework" in page
    assert ">Add it to homework</a>" not in page
    assert ">Add to homework</button>" not in again
    assert "already in homework" in again


# ------------------------------------------------------------------ the details, wherever shown


@pytest.mark.parametrize("family", [False, True])
def test_a_kind_saved_alone_is_shown_on_the_note(family: bool) -> None:
    with browser() as client:
        name = save_note(client, "A remembered task that still needs details")
        only_kind = {"course_choice": "", "course_other": "", "title": "", "kind": "TASK"}
        form = {**opened(client, name, family=family), **typed(**only_kind)}
        saved = save_details(client, name, form, family=family)
        page = client.get(saved.headers["location"]).text

    assert saved.status_code == 303
    assert "Kind: Task" in page.split("History of this note")[0]


def history_of(page: str) -> str:
    return page.split('<details class="steps history">')[1].split("</details>")[0]


def test_the_history_keeps_every_detail_of_every_change_and_says_what_each_change_did() -> None:
    with browser() as client:
        name = save_note(client)
        first = {**opened(client, name), **typed(note="Earlier unique instruction", kind="TASK")}
        assert save_details(client, name, first).status_code == 303
        second = {
            **opened(client, name),
            **typed(note="Later unique instruction", kind="HOMEWORK", title=""),
        }
        assert save_details(client, name, second).status_code == 303
        third = {**opened(client, name, family=True), **typed(note="Later unique instruction")}
        by_family = save_details(client, name, {**third, "due_date": "2026-08-21"}, family=True)
        assert by_family.status_code == 303
        added = add(client, name, opened(client, name))
        page = client.get(added.headers["location"]).text

    entries = history_of(page).split("<li>")[1:]
    assert added.status_code == 303
    assert [entry.strip().split(chr(10))[0].strip() for entry in entries] == [
        "Saved",
        "Details saved",
        "Details saved",
        "Details saved",
        "Added to homework",
    ]
    assert "Earlier unique instruction" in entries[1]
    assert "Kind: Task" in entries[1]
    assert "Later unique instruction" in entries[2]
    assert "Kind: Homework" in entries[2]
    assert "Title removed in this change" in entries[2]
    assert "Earlier unique instruction" not in entries[2]
    assert entries[3].count("set in this change") == 2
    assert "Due date given: August 21, 2026" in entries[3]
    for kept in ("Geometry", "Questions 4-8", "Later unique instruction", "Kind: Homework"):
        assert kept in entries[4], kept


@pytest.mark.parametrize("flag", [2, "broken", 0.5])
def test_a_note_in_homework_whose_flag_is_damaged_is_listed_as_one_that_cannot_be_read(
    flag: object,
) -> None:
    """A damaged flag takes a note off no list: a note in homework is read with the notes
    added to homework, where it is named as one that cannot be read, beside the good ones,
    and every note on record is on exactly one list."""
    with browser() as client:
        store = state_of(client).project_state
        name = save_note(client)
        sound = save_note(client, "A second note, also added")
        waiting_note = save_note(client, "A third note, still waiting")
        assert add(client, name, {**opened(client, name), **typed()}).status_code == 303
        assert (
            add(client, sound, {**opened(client, sound), **typed(title="Other")}).status_code == 303
        )
        damaged(client, name, flag)
        added = store.added_captures()
        waiting = store.outstanding_captures()
        archived = store.archived_captures()
        pages = {
            which: client.get(where).text
            for which, where in (("waiting", NOTES_PAGE), ("added", ADDED_NOTES_PAGE))
        }
        pages["archived"] = client.get(NOTES_PAGE + "/archived").text

    assert ([note.capture_id for note in added.notes], added.unreadable) == ([sound], [name])
    assert ([note.capture_id for note in waiting.notes], waiting.unreadable) == ([waiting_note], [])
    assert (archived.notes, archived.unreadable) == ([], [])
    warned = [which for which, page in pages.items() if "cannot be read right now" in page]
    assert warned == ["added"]
    assert "Homework notes added to homework (2)" in pages["added"]
    assert f'href="{note_href(sound)}"' in pages["added"]
    assert "No homework notes have been added to homework." not in pages["added"]


def test_a_parent_signed_in_reads_the_damaged_note_among_the_notes_added(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client)
        assert add(client, name, {**opened(client, name), **typed()}).status_code == 303
        damaged(client, name, "broken")
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        added = client.get(ADDED_NOTES_PAGE)
        waiting = client.get(NOTES_PAGE)

    assert added.status_code == 200
    assert "1 homework note cannot be read right now" in added.text
    assert "cannot be read" not in waiting.text


# ------------------------------------------------------------------ results on a note in homework


@pytest.mark.parametrize("linked", ["its own", "on record"])
def test_a_note_in_homework_says_the_assignment_is_unchanged_never_that_it_is_in_no_plan(
    linked: str,
) -> None:
    with browser() as client:
        target = on_record(client, date(2026, 8, 28)) if linked == "on record" else None
        name = save_note(client)
        plain = save_note(client, "A note that stays a note")
        shown = add(client, name, {**opened(client, name), **typed()})
        if target is not None:
            form = whole_form(shown.text, note_add_action(name))
            shown = add(client, name, {**form, "candidate": choice_value(target.assignment_id)})
        assert shown.status_code == 303
        assignments = rows(client)[0]

        def change(note: str, step: str, **sent: str) -> Answer:
            page = client.get(note_href(note), params={"edit": "1"} if step == "edit" else {}).text
            fields = whole_form(page, note_action(note, step))
            return client.post(
                note_action(note, step), data={**fields, **sent}, headers=PAGE_HEADERS
            )

        edited = change(name, "edit", text="Changed the quoted words")
        edited_page = client.get(edited.headers["location"]).text
        assert change(name, "archive").status_code == 303
        restored = change(name, "restore")
        restored_page = client.get(restored.headers["location"]).text
        earlier = client.get(edited.headers["location"]).text
        unlinked = change(plain, "edit", text="Changed words of a plain note")
        unlinked_page = client.get(unlinked.headers["location"]).text
        added_list = client.get(ADDED_NOTES_PAGE).text
        waiting_list = client.get(NOTES_PAGE).text
        after = rows(client)[0]

    def result(page: str) -> str:
        return page.split('id="note-result"')[1].split("</p>")[0]

    assert escape(NOTE_EDITED_IN_HOMEWORK) in result(edited_page)
    assert escape(NOTE_RESTORED_IN_HOMEWORK) in result(restored_page)
    for page in (edited_page, restored_page):
        assert "not in a plan yet" not in result(page)
        assert f'href="{ADDED_NOTES_PAGE}"' in page
    assert escape(NOTE_SAVED_EARLIER) in result(earlier)
    assert escape(NOTE_EDITED) in result(unlinked_page)
    assert f'href="{note_href(name)}"' in added_list
    assert f'href="{note_href(name)}"' not in waiting_list
    assert after == assignments


# ------------------------------------------------------------------ failure, a plan, the family


def test_a_write_the_file_refuses_keeps_everything_she_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        name = save_note(client)
        form = {**opened(client, name), **typed(title="Typed <i>title</i>")}
        before = rows(client)

        def refuses(*args: object, **kwargs: object) -> None:
            msg = "the file refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "_upsert_assignments_locked", refuses)
        answer = add(client, name, form)
        monkeypatch.undo()
        after = rows(client)

    assert answer.status_code == 500
    assert answer.text.count(" autofocus") == 1
    assert "Typed &lt;i&gt;title&lt;/i&gt;" in answer.text
    assert after == before


def test_the_planner_is_told_the_note_about_the_work_as_hers_and_never_her_notes_own_words() -> (
    None
):
    planners: list[Scripted] = []  # type: ignore[type-arg]
    critics: list[Scripted] = []  # type: ignore[type-arg]
    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [fixture_week_plan()],
            lambda: [accepting()],
            planners=planners,
            critics=critics,
        )
        name = save_note(client, "ZEBRA-WORDS heard from a classmate")
        unfinished = save_note(client, "ZEBRA-WAITING still only a note")
        form = {**opened(client, name), **typed(due_date="2026-08-21", note="Show the working.")}
        assert add(client, name, form).status_code == 303
        client.post("/student/actions/plan")
        assert unfinished

    sent = " ".join(
        str(message.content)
        for asked in (*planners, *critics)
        for brief in asked.briefs
        for message in brief
    )
    assert 'student_noted="Show the working."' in sent
    assert "ZEBRA" not in sent
    assert derived_assignment_id(name) in sent


def test_the_family_page_says_what_a_note_still_needs_and_leads_to_the_familys_form() -> None:
    with browser() as client:
        bare = save_note(client, "Only her words so far")
        ready = save_note(client, "Words with details")
        save_details(client, ready, {**opened(client, ready), **typed()})
        family = client.get(FAMILY).text
        week = client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text
        shared = week

    assert f'href="{note_add_href(bare, family=True)}"' in family
    assert "It needs a class and a title" in family
    assert "Ready to add to homework" in family
    assert "Add the class and title to put this in a plan" in week
    assert "Ready to add to homework" in week
    assert "once a note is added to homework" in shared
