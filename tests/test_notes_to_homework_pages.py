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
    NOTE_GONE,
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
    CHOICE_IS_PAST,
    CHOOSE_ABOUT_THESE,
    NOT_SAVED,
    details_date_controls_are_valid,
)
from blossom.routes.runs import plan_graphs
from blossom.routes.student import BAD_FORM, NOT_HERS_TO_UPDATE
from blossom.stores.project_state import Assignment
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


def on_record(client: TestClient, due: date, title: str = "Questions 4-8") -> Assignment:
    row = Assignment(
        assignment_id=intake.identity("Geometry", title, due.isoformat()),
        course="Geometry",
        title=title,
        due_date=due,
        dependencies=[],
        reported_submission_status="not_started",
        origins={"record": SourceChannel.LMS},
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
        joined = add(client, name, {**choices, "candidate": target.assignment_id})
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
        form = {**opened(client, name), **typed(), "candidate": shown.assignment_id}
        too_long = add(client, name, {**form, "note": "n" * 501})
        kept = whole_form(too_long.text, note_add_action(name))
        on_record(client, date(2026, 9, 4))
        put_again = add(client, name, form)
        asked = whole_form(put_again.text, note_add_action(name))

    assert too_long.status_code == 422
    assert kept["candidate"] == shown.assignment_id
    assert put_again.status_code == 409
    assert "candidate" not in asked


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
        answer = add(client, name, {**choices, "candidate": shown.assignment_id})
        after = rows(client)

    assert answer.status_code == 409
    assert escape(CHOOSE_ABOUT_THESE) in answer.text
    assert arrived.assignment_id in answer.text
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


@pytest.mark.parametrize("sent", ["separate", "an-assignment-that-is-not-one-of-them"])
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
        not_among = add(client, other, {**shown, "candidate": "an-assignment-that-is-not-one"})
        after = rows(client)

    assert none_here.status_code == 409
    assert escape(CHOICE_IS_PAST) in none_here.text
    assert escape(CHOOSE_ABOUT_THESE) not in none_here.text
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


def damaged(client: TestClient, name: str) -> None:
    """Make the note one that cannot be read: a flag that is neither 0 nor 1."""
    connection = state_of(client).project_state._connection
    connection.execute("UPDATE homework_captures SET archived = 2 WHERE capture_id = ?", (name,))
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
