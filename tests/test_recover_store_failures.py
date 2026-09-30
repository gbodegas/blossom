"""A press or a page whose read of the household's file fails: a page that reads no store and
says what happened, with the status the press or the page would have had.

A refusal writes nothing, so its page is tried once and, when a read for it fails, the page
that reads no store keeps the refusal's own status, says it in words that stay true without
the page, and keeps what was typed. A write the file refuses is rolled back, so its page may
say nothing was changed and offer to try again. A page that is only read answers 503, says
what cannot be shown, and offers the same address again. The fixture week through the app, a
pinned clock, and a failure made at the store call, or a second connection holding the file.
"""

import pathlib
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.hand_in import NEEDS_HAND_IN, TURNED_IN, HandInSaved, HandInState
from blossom.reconciliation import SourceChannel, SourceRecord
from blossom.routes import hand_in as hand_in_routes
from blossom.routes import parent as parent_routes
from blossom.routes import runs as run_routes
from blossom.routes import student as student_routes
from blossom.routes.navigation import details_href, week_href
from tests.support import (
    ESSAY_ID,
    ESSAY_TITLE,
    FIXTURE_WEEK,
    HER_PAGE,
    MISSING_EMAIL,
    PAGE_HEADERS,
    PLAN_DATE,
    HeldByAnother,
    Statements,
    after_the_failure,
    browser,
    card_for,
    database_of,
    every_row,
    fixture_settings,
    form_fields,
    hidden,
    household_client,
    main_of,
    planned,
    quiet_client,
    refusing,
    report,
    rules_named,
    sign_in_as,
    state_of,
    store_free_page,
    walkthrough,
    ways_back_of,
)

REPORT = f"/student/actions/assignments/{ESSAY_ID}/report"
UNDO = f"/student/actions/assignments/{ESSAY_ID}/undo-report"
HAND_IN = f"/student/actions/assignments/{ESSAY_ID}/hand-in"
UNDO_HAND_IN = f"/student/actions/assignments/{ESSAY_ID}/undo-hand-in"
DETAILS = f"/student/assignments/{ESSAY_ID}"
TO_TURN_IN = "/student/to-turn-in"
GONE_ID = "assignment-not-here"
TYPED = "Kept <b>words</b>\nand a second line"
LONG = "n" * 501
YOUR_WEEK = "Your week can't be shown right now."
HER_WEEK = "Her week can't be shown right now."
THIS_ASSIGNMENT = "This assignment can't be shown right now."
YOUR_LIST = "Your To turn in list can't be shown right now."
FAMILY = "Family review can't be shown right now."
TRIED = [
    ("the first read", "latest_for", sqlite3.OperationalError),
    ("a later read", "read_everything", sqlite3.DatabaseError),
]
"""Where her week fails as the page for a press is made: the drafts, which the page reads
first, or the record read after them, and the two kinds of failure a read meets."""


def fail_week(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    place: str,
    kind: type[Exception],
    marks: list[str],
) -> None:
    """Make her week's ``place`` read fail once the press has been made."""
    if place == "latest_for":
        monkeypatch.setattr(state_of(client).drafts, "latest_for", refusing(kind, marks))
    else:
        monkeypatch.setattr(student_routes, "read_everything", refusing(kind, marks))


# ------------------------------------------------------------- a refusal on her week


@dataclass(frozen=True)
class Press:
    """A press on her week that is refused, what it answers, and what it keeps."""

    status: int
    said: str
    choice: str | None
    note: str | None
    week: str | None = FIXTURE_WEEK
    about: str = ESSAY_ID


WEEK_PRESSES = {
    "nothing chosen": Press(
        422, "Nothing was saved, because neither Done nor Not yet was chosen.", None, TYPED
    ),
    "a note too long": Press(
        422, "Nothing was saved, because the note is longer than 500 characters.", "Done", LONG
    ),
    "a field the page does not send": Press(
        422,
        "That form carried a field twice, or one this page does not send, so nothing was saved.",
        None,
        TYPED,
    ),
    "a week no page makes": Press(
        422,
        "The form named a page to go back to that these pages do not make. Nothing was saved.",
        "Done",
        TYPED,
        week=None,
    ),
    "saved on another device": Press(
        409, "An update was saved on another device, so yours was not saved.", "Not yet", TYPED
    ),
    "not this card's update": Press(
        422,
        "That form names an update this assignment does not have, so nothing was saved.",
        "Done",
        TYPED,
    ),
    "undo of a changed update": Press(
        409, "Your update has changed, so it cannot be undone from that page.", None, None
    ),
    "undo already undone": Press(409, "That update was already undone.", None, None),
    "undo of homework not on record": Press(
        404, "That assignment is not on record, so nothing was changed.", None, None, about=GONE_ID
    ),
}


def the_card(client: TestClient, opened: str = "change") -> str:
    """The essay's card on the fixture week, its form open or, with ``show``, as it stands."""
    page = client.get(
        HER_PAGE, params={"week": FIXTURE_WEEK, opened: ESSAY_ID}, headers=PAGE_HEADERS
    ).text
    return card_for(page, ESSAY_ID)


def week_press(client: TestClient, case: str) -> tuple[str, dict[str, str]]:
    """Make what the press needs, and the press itself as her card sends it."""
    fields = form_fields(the_card(client), REPORT)
    if case == "nothing chosen":
        return REPORT, {**fields, "note": TYPED}
    if case == "a note too long":
        return REPORT, {**fields, "status": "done", "note": LONG}
    if case == "a field the page does not send":
        return REPORT, {**fields, "status": "done", "note": TYPED, "hand_in_view": "list"}
    if case == "a week no page makes":
        return REPORT, {**fields, "status": "done", "note": TYPED, "week": "someday"}
    if case == "saved on another device":
        report(client, ESSAY_ID, "done")
        return REPORT, {**fields, "status": "not_yet", "note": TYPED}
    if case == "not this card's update":
        return REPORT, {**fields, "status": "done", "note": TYPED, "expected_report_id": "nope"}
    if case == "undo of homework not on record":
        return (
            f"/student/actions/assignments/{GONE_ID}/undo-report",
            {"report_id": "any", "week": FIXTURE_WEEK},
        )
    report(client, ESSAY_ID, "done")
    first = form_fields(the_card(client, "show"), UNDO)
    if case == "undo of a changed update":
        report(client, ESSAY_ID, "not_yet")
    else:
        assert client.post(UNDO, data=first).status_code == 303
    return UNDO, first


@pytest.mark.parametrize(("where", "place", "kind"), TRIED)
@pytest.mark.parametrize("reader", ["her", "open"])
@pytest.mark.parametrize("case", list(WEEK_PRESSES))
def test_a_refusal_on_her_week_is_said_without_the_week_when_the_week_cannot_be_read(
    case: str,
    reader: str,
    where: str,
    place: str,
    kind: type[Exception],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del where
    press = WEEK_PRESSES[case]
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        path, data = week_press(client, case)
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            fail_week(monkeypatch, client, place, kind, seen)
            answer = client.post(path, data=data, headers=PAGE_HEADERS)
        monkeypatch.undo()
        after = every_row(database_of(client))

    main = store_free_page(
        answer, status=press.status, heading="Update not saved", alert=f"{press.said} {YOUR_WEEK}"
    )
    shown = "Done" if press.choice == "Done" else press.choice
    assert (f"<p>Your choice: {shown}.</p>" in main) == (press.choice is not None)
    if press.note is None:
        assert "<textarea" not in main
    else:
        assert f"readonly>{escape(press.note)}</textarea>" in main
    back = week_href(
        None if press.week is None else date.fromisoformat(press.week),
        press.about,
        show=press.about,
    )
    assert ways_back_of(main) == [(str(escape(back)), "Back to the week")]
    assert after_the_failure(seen) == []
    assert after == before


def test_a_refusal_on_her_week_is_the_week_as_before_when_the_week_reads(
    tmp_path: pathlib.Path,
) -> None:
    with household_client("her", tmp_path) as client:
        sign_in_as(client, "her")
        path, data = week_press(client, "nothing chosen")
        answer = client.post(path, data=data, headers=PAGE_HEADERS)

    assert answer.status_code == 422
    assert "<h1>Update not saved</h1>" not in answer.text
    assert "Choose Done or Not yet." in answer.text
    assert YOUR_WEEK not in answer.text


OTHER_FAILURES = {
    "her week after a refusal": ("drafts", "latest_for", "week"),
    "the details after a refusal": ("project_state", "one_assignment", "details"),
    "the list after a refusal": ("routes", "read_everything", "list"),
    "family review after a refusal": ("drafts", "review_snapshot", "family"),
    "the details": ("project_state", "one_assignment", "get details"),
    "her notes": ("project_state", "outstanding_captures", "get notes"),
    "her To turn in list": ("routes", "read_everything", "get list"),
    "taking a request back": ("help_requests", "take_back", "take back"),
    "saying today is too much": ("workload_signals", "record", "too much"),
    "a parent taking her request up": ("help_requests", "accept", "take up"),
}


@pytest.mark.parametrize("kind", [RuntimeError, ValueError])
@pytest.mark.parametrize("case", list(OTHER_FAILURES))
def test_another_kind_of_failure_is_not_taken_for_a_file_that_cannot_be_read(
    case: str, kind: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a failed read or write of the file gets the page that reads no store; anything
    else at the same call is still an error of the server's."""
    store, name, press = OTHER_FAILURES[case]
    app_client = quiet_client(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()))
    with app_client as client:
        state = state_of(client)
        asked = state.help_requests.ask(PLAN_DATE, "which part?").request_id
        path, data = week_press(client, "nothing chosen")
        if press == "details":
            data = {**data, "report_view": "detail", "return_to": "", "plan_id": ""}
        if press == "list":
            hand_in_saved(client, NEEDS_HAND_IN, None)
            path, data = HAND_IN, {"state": TURNED_IN, "note": "typed", "hand_in_view": "list"}
        if press == "family":
            path, data = "/parent/actions/plan", {"plan_date": "someday"}
        target = hand_in_routes if store == "routes" else getattr(state, store)
        monkeypatch.setattr(target, name, refusing(kind))
        if press.startswith("get "):
            where = {
                "get details": DETAILS,
                "get notes": "/student/homework-notes",
                "get list": "/student/to-turn-in",
            }[press]
            answer = client.get(where, headers=PAGE_HEADERS)
        elif press == "take back":
            answer = client.post(f"/student/actions/take-back-help/{asked}")
        elif press == "too much":
            answer = client.post("/student/actions/too-much")
        elif press == "take up":
            answer = client.post(
                f"/parent/actions/help/{asked}", data={"step": "accept", "response": TYPED}
            )
        else:
            answer = client.post(path, data=data, headers=PAGE_HEADERS)

    assert answer.status_code == 500
    assert answer.text == "Internal Server Error"


# ------------------------------------------------------------- a parent's press on her week


@dataclass(frozen=True)
class ParentPress:
    path: str
    heading: str
    said: str
    back: str


NOT_HERS_TO_UPDATE = "Sign in as the student to update."
NOT_HERS_TO_SIGNAL = "Sign in as the student to say today is too much or take it back."
NOT_HERS_TO_ASK = "Sign in as the student to ask for help or take a request back."
PARENT_PRESSES = {
    "her update": ParentPress(REPORT, "Update not saved", NOT_HERS_TO_UPDATE, HER_PAGE),
    "its Undo": ParentPress(UNDO, "Update not saved", NOT_HERS_TO_UPDATE, HER_PAGE),
    "her hand-in": ParentPress(HAND_IN, "Update not saved", NOT_HERS_TO_UPDATE, HER_PAGE),
    "its hand-in Undo": ParentPress(UNDO_HAND_IN, "Update not saved", NOT_HERS_TO_UPDATE, HER_PAGE),
    "too much right now": ParentPress(
        "/student/actions/too-much", "Not saved", NOT_HERS_TO_SIGNAL, HER_PAGE
    ),
    "taking a signal back": ParentPress(
        "/student/actions/take-back/any-signal", "Not saved", NOT_HERS_TO_SIGNAL, HER_PAGE
    ),
    "asking for help": ParentPress(
        "/student/actions/ask-for-help", "Request not sent", NOT_HERS_TO_ASK, f"{HER_PAGE}#help"
    ),
    "taking a request back": ParentPress(
        "/student/actions/take-back-help/" + "0" * 32,
        "Request not taken back",
        NOT_HERS_TO_ASK,
        f"{HER_PAGE}#help",
    ),
}


@pytest.mark.parametrize(("where", "place", "kind"), TRIED)
@pytest.mark.parametrize("case", list(PARENT_PRESSES))
def test_a_parents_press_on_her_week_keeps_its_403_when_her_week_cannot_be_read(
    case: str,
    where: str,
    place: str,
    kind: type[Exception],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del where
    press = PARENT_PRESSES[case]
    with household_client("parent", tmp_path) as client:
        sign_in_as(client, "parent")
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            fail_week(monkeypatch, client, place, kind, seen)
            answer = client.post(
                press.path, data={"status": "done", "note": TYPED}, headers=PAGE_HEADERS
            )
        monkeypatch.undo()
        after = every_row(database_of(client))

    main = store_free_page(
        answer, status=403, heading=press.heading, alert=f"{press.said} {HER_WEEK}"
    )
    assert ways_back_of(main) == [(press.back, "Back to her week")]
    assert "Kept" not in main
    assert "<textarea" not in main
    assert after_the_failure(seen) == []
    assert after == before


# ------------------------------------------------------------- a refusal on the details


DETAILS_PRESSES = {
    "an update with nothing chosen": (
        422,
        "Nothing was saved, because neither Done nor Not yet was chosen.",
    ),
    "an update saved on another device": (
        409,
        "An update was saved on another device, so yours was not saved.",
    ),
    "a hand-in with no choice": (
        422,
        "Nothing was saved, because no choice about turning it in was made.",
    ),
    "a next step too long": (
        422,
        "Nothing was saved, because the next step is longer than 200 characters.",
    ),
    "a next step on two lines": (
        422,
        "Nothing was saved, because the next step is on more than one line.",
    ),
    "a hand-in note too long": (
        422,
        "Nothing was saved, because the note is longer than 500 characters.",
    ),
    "a character it cannot keep": (
        422,
        "Nothing was saved, because it has a character Blossom cannot keep.",
    ),
    "a hand-in that changed": (409, "That changed while you were away, so nothing was saved."),
    "a hand-in this assignment does not have": (
        422,
        "That form names a hand-in update this assignment does not have, so nothing was saved.",
    ),
    "a hand-in form with a field it does not send": (
        422,
        "That form carried a field twice, or one this page does not send, so nothing was saved.",
    ),
    "an Undo of a hand-in that changed": (
        409,
        "Your hand-in update has changed, so it cannot be undone from that page.",
    ),
    "an Undo of a hand-in already undone": (409, "That update was already undone."),
}


def hand_in_saved(
    client: TestClient, state: HandInState, head: str | None, action: str | None = None
) -> str:
    store = state_of(client).project_state
    at = datetime(2026, 8, 19, 21, 0, tzinfo=UTC)
    kept = store.record_hand_in(
        ESSAY_ID, state, action, None, expected_head=head, now=at, today=date(2026, 8, 19)
    )
    assert isinstance(kept, HandInSaved), kept
    return kept.event.event_id


def details_press(client: TestClient, case: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """The press, as the details send it, and what the page that reads no store keeps."""
    update = form_fields(
        client.get(DETAILS, params={"change": "1"}, headers=PAGE_HEADERS).text, REPORT
    )
    hand_in = form_fields(
        client.get(DETAILS, params={"hand_in": "change"}, headers=PAGE_HEADERS).text, HAND_IN
    )
    blank = {"next_action": "", "note": ""}
    if case == "an update with nothing chosen":
        return REPORT, {**update, "note": TYPED}, {"note": TYPED}
    if case == "an update saved on another device":
        report(client, ESSAY_ID, "done")
        kept = {"choice": "Not yet", "note": TYPED}
        return REPORT, {**update, "status": "not_yet", "note": TYPED}, kept
    if case == "a hand-in with no choice":
        return HAND_IN, {**hand_in, **blank, "note": TYPED}, {"hand-in note": TYPED}
    if case == "a next step too long":
        step = "s" * 201
        data = {**hand_in, "state": NEEDS_HAND_IN, "next_action": step, "note": ""}
        return HAND_IN, data, {"next step": step, "state": "Still to turn in"}
    if case == "a next step on two lines":
        step = "first\nsecond"
        data = {**hand_in, "state": NEEDS_HAND_IN, "next_action": step, "note": ""}
        return HAND_IN, data, {"next step": step}
    if case == "a hand-in note too long":
        data = {**hand_in, "state": TURNED_IN, "next_action": "", "note": LONG}
        return HAND_IN, data, {"hand-in note": LONG, "state": "Turned in"}
    if case == "a character it cannot keep":
        note = "bell \x07 here"
        data = {**hand_in, "state": TURNED_IN, "next_action": "", "note": note}
        return HAND_IN, data, {"hand-in note": note}
    if case == "a hand-in that changed":
        hand_in_saved(client, TURNED_IN, None)
        data = {**hand_in, "state": "not_required", **blank, "note": TYPED}
        return HAND_IN, data, {"state": "Nothing to turn in", "hand-in note": TYPED}
    if case == "a hand-in this assignment does not have":
        data = {**hand_in, "state": TURNED_IN, **blank, "expected_hand_in_id": "nope"}
        return HAND_IN, data, {"state": "Turned in"}
    if case == "a hand-in form with a field it does not send":
        data = {**hand_in, "state": TURNED_IN, **blank, "status": "done"}
        return HAND_IN, data, {"state": "Turned in"}
    first = hand_in_saved(client, TURNED_IN, None)
    if case == "an Undo of a hand-in that changed":
        hand_in_saved(client, NEEDS_HAND_IN, first)
    else:
        undone = client.post(
            UNDO_HAND_IN,
            data={"hand_in_id": first, "return_to": "", "week": "", "plan_id": ""},
        )
        assert undone.status_code == 303, undone.text[:300]
    return UNDO_HAND_IN, {"hand_in_id": first, "return_to": "", "week": "", "plan_id": ""}, {}


@pytest.mark.parametrize(
    ("place", "kind"),
    [
        ("one_assignment", sqlite3.OperationalError),
        ("captures_of_assignment", sqlite3.DatabaseError),
    ],
)
@pytest.mark.parametrize("reader", ["her", "open"])
@pytest.mark.parametrize("case", list(DETAILS_PRESSES))
def test_a_refusal_on_the_details_is_said_without_them_when_they_cannot_be_read(
    case: str,
    reader: str,
    place: str,
    kind: type[Exception],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, said = DETAILS_PRESSES[case]
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        path, data, kept = details_press(client, case)
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            store = state_of(client).project_state
            monkeypatch.setattr(store, place, refusing(kind, seen))
            answer = client.post(path, data=data, headers=PAGE_HEADERS)
        monkeypatch.undo()
        after = every_row(database_of(client))

    main = store_free_page(
        answer, status=status, heading="Update not saved", alert=f"{said} {THIS_ASSIGNMENT}"
    )
    if "note" in kept:
        assert (
            f'<textarea id="kept-note" rows="4" readonly>{escape(kept["note"])}</textarea>' in main
        )
    if "choice" in kept:
        assert f"<p>Your choice: {kept['choice']}.</p>" in main
    if "state" in kept:
        assert f"Your choice for turning it in: {kept['state']}. It was not saved." in main
    if "next step" in kept:
        assert f"readonly>{escape(kept['next step'])}</textarea>" in main
    if "hand-in note" in kept:
        assert f"readonly>{escape(kept['hand-in note'])}</textarea>" in main
    assert ways_back_of(main) == [
        (str(escape(details_href(ESSAY_ID, return_to="week"))), "Back to the assignment"),
        (str(escape(week_href(None, ESSAY_ID, show=ESSAY_ID))), "Back to the week"),
    ]
    assert after_the_failure(seen) == []
    assert after == before


# ------------------------------------------------------------- a refusal on the To turn in list


LIST_PRESSES = {
    "a note on the list's press": (
        422,
        "That form carried a field twice, or a field or a value this list does not send, so "
        "nothing was saved.",
    ),
    "a row that changed": (409, "That changed while you were away, so nothing was saved."),
    "homework not on record": (
        404,
        "That assignment is not on record now, so nothing was changed.",
    ),
    "a hand-in the row does not have": (
        422,
        "That form names a hand-in update this assignment does not have, so nothing was saved.",
    ),
    "an Undo already undone": (409, "That update was already undone."),
    "a field the list does not send": (
        422,
        "That form carried a field twice, or a field or a value this list does not send, so "
        "nothing was saved.",
    ),
}


def list_press(client: TestClient, case: str, view: str) -> tuple[str, dict[str, str], str]:
    first = hand_in_saved(client, NEEDS_HAND_IN, None)
    row = {
        "state": TURNED_IN,
        "next_action": "",
        "note": "",
        "expected_hand_in_id": first,
        "hand_in_view": view,
    }
    if case == "a note on the list's press":
        return HAND_IN, {**row, "note": "typed"}, ESSAY_ID
    if case == "a row that changed":
        hand_in_saved(client, NEEDS_HAND_IN, first, "Print it first")
        return HAND_IN, row, ESSAY_ID
    if case == "homework not on record":
        return f"/student/actions/assignments/{GONE_ID}/hand-in", row, GONE_ID
    if case == "a hand-in the row does not have":
        return HAND_IN, {**row, "expected_hand_in_id": "nope"}, ESSAY_ID
    if case == "a field the list does not send":
        return HAND_IN, {**row, "status": "done"}, ESSAY_ID
    pressed = client.post(HAND_IN, data=row, headers=PAGE_HEADERS)
    assert pressed.status_code == 303, pressed.text[:300]
    made = state_of(client).project_state.hand_in_chains([ESSAY_ID])[ESSAY_ID][-1].event_id
    undo = {"hand_in_id": made, "hand_in_view": view}
    assert client.post(UNDO_HAND_IN, data=undo).status_code == 303
    return UNDO_HAND_IN, undo, ESSAY_ID


@pytest.mark.parametrize("view", ["list", "week"])
@pytest.mark.parametrize("reader", ["her", "open"])
@pytest.mark.parametrize("case", list(LIST_PRESSES))
def test_a_refusal_on_the_list_is_said_without_it_when_it_cannot_be_read(
    case: str, reader: str, view: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, said = LIST_PRESSES[case]
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        path, data, about = list_press(client, case, view)
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            if view == "list":
                monkeypatch.setattr(
                    hand_in_routes, "read_everything", refusing(sqlite3.OperationalError, seen)
                )
            else:
                monkeypatch.setattr(
                    state_of(client).drafts, "latest_for", refusing(sqlite3.DatabaseError, seen)
                )
            answer = client.post(path, data=data, headers=PAGE_HEADERS)
        monkeypatch.undo()
        after = every_row(database_of(client))

    line = YOUR_LIST if view == "list" else YOUR_WEEK
    main = store_free_page(
        answer, status=status, heading="Update not saved", alert=f"{said} {line}"
    )
    first = (
        ("/student/to-turn-in#to-turn-in", "Back to To turn in")
        if view == "list"
        else (HER_PAGE, "Back to my week")
    )
    opened = details_href(
        about, fragment="turning-it-in", return_to="to_turn_in" if view == "list" else "week"
    )
    assert ways_back_of(main) == [first, (str(escape(opened)), "Open this assignment")]
    assert after_the_failure(seen) == []
    assert after == before


# ------------------------------------------------------------- a refusal on Family review


@dataclass(frozen=True)
class FamilyPress:
    status: int
    said: str
    kept: tuple[str, str] | None = None


FAMILY_PRESSES = {
    "a check note too long": FamilyPress(
        422,
        "Nothing was written, because the note is longer than 500 characters.",
        ("Your note, as typed", LONG),
    ),
    "a check form not whole": FamilyPress(
        422,
        "The form did not arrive whole. Nothing was written.",
        ("Your note, as typed", TYPED),
    ),
    "a check made from another device": FamilyPress(
        409,
        "This row was marked checked or reopened from another device since this page was made. "
        "Nothing was written.",
        ("Your note, as typed", TYPED),
    ),
    "a check of homework not on record": FamilyPress(
        404, "No assignment with that id is on record. Nothing was written."
    ),
    "a plan date that is not a date": FamilyPress(
        422, "'someday' is not a date. Use the form YYYY-MM-DD."
    ),
    "a plan date that has passed": FamilyPress(
        422,
        "The evening of 2026-08-18 has passed. Plans are for today or a later evening.",
    ),
    "a plan with no model": FamilyPress(
        503, "no model can be constructed: ANTHROPIC_API_KEY is not set."
    ),
    "a decision no button makes": FamilyPress(
        422,
        "'sideways' is not one of the two buttons, approve or refuse.",
        ("Reason, as typed", TYPED),
    ),
    "a reason too long": FamilyPress(
        422,
        "A reason is at most 500 characters; this one is 501.",
        ("Reason, as typed", LONG),
    ),
    "a decision on no draft": FamilyPress(
        404, "no draft 'draft:none'.", ("Reason, as typed", TYPED)
    ),
    "a reply too long": FamilyPress(
        422,
        "A reply is at most 500 characters; this one is 501.",
        ("Reply, as typed", LONG),
    ),
    "a reply to a request already closed": FamilyPress(409, "", ("Reply, as typed", TYPED)),
    "a reply to no request": FamilyPress(
        404, "no help request '" + "0" * 32 + "'.", ("Reply, as typed", TYPED)
    ),
    "a step no page sends": FamilyPress(
        422,
        "'sideways' is not one of the two moves, accept or resolve.",
        ("Reply, as typed", TYPED),
    ),
}


def the_row(page: str) -> str:
    start = page.index(f'id="update-{ESSAY_ID}"')
    return page[start : page.index("</section>", start)]


def family_press(client: TestClient, case: str, reader: str) -> tuple[str, dict[str, str], str]:
    """Make what the press needs; the press as the family page sends it, and anything the
    expected sentence needs that only the setup knows. Ends signed in as ``reader``."""
    sign_in_as(client, reader)
    told = client.post("/parent/inbox/keep", data={"text": MISSING_EMAIL})
    assert told.status_code == 303
    if reader == "parent":
        sign_in_as(client, "her")
    report(client, ESSAY_ID, "done", "Handed in Tuesday.")
    sign_in_as(client, reader)
    row = the_row(client.get("/parent", headers=PAGE_HEADERS).text)
    basis = hidden(row, "basis")
    mark = f"/parent/actions/checks/{ESSAY_ID}/mark"
    asked = state_of(client).help_requests.ask(PLAN_DATE, "which part?").request_id
    if case == "a check note too long":
        return mark, {"basis": basis, "expected_check_id": "", "note": LONG}, ""
    if case == "a check form not whole":
        return mark, {"basis": basis, "note": TYPED}, ""
    if case == "a check made from another device":
        first = {"basis": basis, "expected_check_id": "", "note": "Seen."}
        assert client.post(mark, data=first).status_code == 303
        return mark, {"basis": basis, "expected_check_id": "", "note": TYPED}, ""
    if case == "a check of homework not on record":
        return (
            f"/parent/actions/checks/{GONE_ID}/mark",
            {"basis": GONE_ID, "expected_check_id": "", "note": ""},
            "",
        )
    if case == "a plan date that is not a date":
        return "/parent/actions/plan", {"plan_date": "someday"}, ""
    if case == "a plan date that has passed":
        return "/parent/actions/plan", {"plan_date": "2026-08-18"}, ""
    if case == "a plan with no model":
        return "/parent/actions/plan", {"plan_date": ""}, ""
    if case == "a decision no button makes":
        return "/parent/actions/decide/draft:none", {"decision": "sideways", "reason": TYPED}, ""
    if case == "a reason too long":
        return "/parent/actions/decide/draft:none", {"decision": "refuse", "reason": LONG}, ""
    if case == "a decision on no draft":
        return "/parent/actions/decide/draft:none", {"decision": "refuse", "reason": TYPED}, ""
    if case == "a reply too long":
        return f"/parent/actions/help/{asked}", {"step": "accept", "response": LONG}, ""
    if case == "a reply to a request already closed":
        state_of(client).help_requests.resolve(asked, None)
        said = f"request '{asked}' is resolved, so it cannot be taken up."
        return f"/parent/actions/help/{asked}", {"step": "accept", "response": TYPED}, said
    if case == "a reply to no request":
        return "/parent/actions/help/" + "0" * 32, {"step": "accept", "response": TYPED}, ""
    return f"/parent/actions/help/{asked}", {"step": "sideways", "response": TYPED}, ""


@pytest.mark.parametrize(
    ("place", "kind"),
    [("review_snapshot", sqlite3.OperationalError), ("read_everything", sqlite3.DatabaseError)],
)
@pytest.mark.parametrize("reader", ["parent", "open"])
@pytest.mark.parametrize("case", list(FAMILY_PRESSES))
def test_a_refusal_on_family_review_is_said_without_it_when_it_cannot_be_read(
    case: str,
    reader: str,
    place: str,
    kind: type[Exception],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    press = FAMILY_PRESSES[case]
    with household_client(reader, tmp_path) as client:
        path, data, said = family_press(client, case, reader)
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            if place == "review_snapshot":
                monkeypatch.setattr(state_of(client).drafts, place, refusing(kind, seen))
            else:
                monkeypatch.setattr(parent_routes, place, refusing(kind, seen))
            answer = client.post(path, data=data, headers=PAGE_HEADERS)
        monkeypatch.undo()
        after = every_row(database_of(client))

    main = store_free_page(
        answer,
        status=press.status,
        heading="Family review",
        alert=f"{said or press.said} {FAMILY}",
    )
    if press.kept is None:
        assert "<textarea" not in main
    else:
        label, typed = press.kept
        assert f">{label}</label>" in main
        assert f"readonly>{escape(typed)}</textarea>" in main
    assert ways_back_of(main) == [("/parent", "Return to Family review")]
    assert after_the_failure(seen) == []
    assert after == before


def test_a_decision_on_a_decided_draft_keeps_its_409_and_the_reason_without_the_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser(key=True) as client:
        walkthrough(client)
        made = planned(client)
        decide = f"/parent/actions/decide/{made.draft_id}"
        assert client.post(decide, data={"decision": "approve", "reason": ""}).status_code == 303
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).drafts,
                "review_snapshot",
                refusing(sqlite3.OperationalError, seen),
            )
            answer = client.post(decide, data={"decision": "refuse", "reason": TYPED})
        monkeypatch.undo()
        after = every_row(database_of(client))

    main = store_free_page(
        answer,
        status=409,
        heading="Family review",
        alert=f"draft '{made.draft_id}' was already approved. {FAMILY}",
    )
    assert f"readonly>{escape(TYPED)}</textarea>" in main
    assert after_the_failure(seen) == []
    assert after == before


@pytest.mark.parametrize("case", ["a request already closed", "no such request"])
@pytest.mark.parametrize("reader", ["parent", "open"])
def test_a_request_named_in_a_refusal_is_named_whole_on_family_review_and_its_stand_in(
    case: str, reader: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step on a request that is closed, or not on record, is refused with the request's
    whole id, on the family page and on its stand-in alike: nothing of it is cut."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        state = state_of(client)
        asked = state.help_requests.ask(PLAN_DATE, "which part?").request_id
        if case == "a request already closed":
            state.help_requests.resolve(asked, None)
            named, said = asked, f"request '{asked}' is resolved, so it cannot be taken up"
        else:
            named = uuid4().hex
            said = f"no help request '{named}'"
        path = f"/parent/actions/help/{named}"
        page = client.post(path, data={"step": "accept", "response": ""}, headers=PAGE_HEADERS)
        monkeypatch.setattr(state.drafts, "review_snapshot", refusing(sqlite3.OperationalError))
        stand_in = client.post(path, data={"step": "accept", "response": ""}, headers=PAGE_HEADERS)

    status = 409 if case == "a request already closed" else 404
    assert page.status_code == status
    assert len(named) == 32
    assert f'<p class="problem" role="alert" id="problem">{escape(said)}</p>' in main_of(page.text)
    store_free_page(stand_in, status=status, heading="Family review", alert=f"{said}. {FAMILY}")


def test_family_reviews_alerts_break_a_long_word_where_they_must() -> None:
    """A refusal on Family review can name a request by its id, one word of 32 characters,
    wider than a phone's line: the family page's alert and its stand-in's break such a word
    where they must, so the page never scrolls sideways and nothing is cut, and a problem
    anywhere else keeps its own rules. The browser shows what this gives; here the rule is
    pinned."""
    for selector in ("#problem", "#problem-summary"):
        assert any("overflow-wrap: anywhere;" in inside for inside in rules_named(selector)), (
            selector
        )
    assert not any("overflow-wrap" in inside for inside in rules_named(".problem"))


# ------------------------------------------------------------- a plan that could not be made


def test_her_plan_that_failed_on_the_way_is_said_without_her_week_when_it_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser(key=True) as client:
        walkthrough(client)
        monkeypatch.setattr(run_routes, "read_week", refusing(sqlite3.OperationalError))
        readable = client.post("/student/actions/plan", headers=PAGE_HEADERS)
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).drafts, "latest_for", refusing(sqlite3.OperationalError, seen)
            )
            answer = client.post("/student/actions/plan", headers=PAGE_HEADERS)
        monkeypatch.undo()
        after = every_row(database_of(client))

    assert readable.status_code == 500
    assert (
        "Blossom could not make a plan: something went wrong on the way. The plan already "
        "here, if any, is unchanged."
    ) in readable.text
    main = store_free_page(
        answer,
        status=500,
        heading="Plan not made",
        alert=f"Blossom could not make a plan: something went wrong on the way. {YOUR_WEEK}",
    )
    assert "already here" not in main
    assert ways_back_of(main) == [(HER_PAGE, "Back to my week")]
    assert after_the_failure(seen) == []
    assert after == before


def test_her_plan_refused_before_a_run_keeps_its_status_without_her_week(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with household_client("parent", tmp_path) as client:
        sign_in_as(client, "parent")
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).drafts, "latest_for", refusing(sqlite3.DatabaseError, seen)
            )
            answer = client.post("/student/actions/plan", headers=PAGE_HEADERS)
        monkeypatch.undo()

    main = store_free_page(
        answer,
        status=503,
        heading="Plan not made",
        alert=(
            "Blossom could not make a plan: no model can be constructed: ANTHROPIC_API_KEY is "
            f"not set. {HER_WEEK}"
        ),
    )
    assert ways_back_of(main) == [(HER_PAGE, "Back to her week")]
    assert after_the_failure(seen) == []


def test_a_family_plan_that_failed_on_the_way_is_said_without_family_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser(key=True) as client:
        walkthrough(client)
        monkeypatch.setattr(run_routes, "read_week", refusing(sqlite3.OperationalError))
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).drafts,
                "review_snapshot",
                refusing(sqlite3.OperationalError, seen),
            )
            answer = client.post("/parent/actions/plan", data={"plan_date": ""})
        monkeypatch.undo()
        after = every_row(database_of(client))

    main = store_free_page(
        answer,
        status=500,
        heading="Family review",
        alert=f"The plan could not be made: something went wrong on the way. {FAMILY}",
    )
    assert "waiting below" not in main
    assert after_the_failure(seen) == []
    assert after == before


# ------------------------------------------------------------- taking a request back


TAKE_BACK_FAILED = "Your request could not be taken back, and nothing was changed. Try again."


@pytest.mark.parametrize("kind", [sqlite3.OperationalError, sqlite3.DatabaseError])
@pytest.mark.parametrize("reader", ["her", "open"])
def test_a_request_the_file_would_not_take_back_is_said_in_help_and_can_be_taken_back_once(
    reader: str, kind: type[Exception], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        help_requests = state_of(client).help_requests
        asked = help_requests.ask(PLAN_DATE, "which part?").request_id
        path = f"/student/actions/take-back-help/{asked}"
        monkeypatch.setattr(help_requests, "take_back", refusing(kind))
        refused = client.post(path, headers=PAGE_HEADERS)
        still = [item.request_id for item in help_requests.open_requests()]
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).drafts, "latest_for", refusing(sqlite3.OperationalError, seen)
            )
            unread = client.post(path, headers=PAGE_HEADERS)
        monkeypatch.undo()
        again = client.post(path, headers=PAGE_HEADERS)
        twice = client.post(path, headers=PAGE_HEADERS)
        left = help_requests.open_requests()

    assert refused.status_code == 500
    assert (
        '<p class="problem" role="alert" id="help-problem" tabindex="-1" autofocus>'
        f'{TAKE_BACK_FAILED} <a href="#help-{asked}">Go to the request.</a></p>'
    ) in refused.text
    assert refused.text.count("autofocus") == 1
    assert still == [asked]
    main = store_free_page(
        unread,
        status=500,
        heading="Request not taken back",
        alert=f"{TAKE_BACK_FAILED} {YOUR_WEEK}",
    )
    assert ways_back_of(main) == [(f"{HER_PAGE}#help", "Back to my week")]
    assert after_the_failure(seen) == []
    assert again.status_code == 303
    assert twice.status_code == 404
    assert left == []


@pytest.mark.parametrize(
    ("case", "status", "said"),
    [
        (
            "taken up",
            409,
            "A parent is already responding to this request, so it cannot be taken back. "
            "Nothing was changed.",
        ),
        (
            "resolved",
            409,
            "This request is already closed, so it cannot be taken back. Nothing was changed.",
        ),
        ("gone", 404, "That request is not here any more; nothing was changed."),
    ],
)
def test_a_refused_take_back_keeps_its_status_and_sentence_without_her_week(
    case: str, status: int, said: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser() as client:
        help_requests = state_of(client).help_requests
        asked = help_requests.ask(PLAN_DATE, "which part?").request_id
        if case == "taken up":
            help_requests.accept(asked, None)
        elif case == "resolved":
            help_requests.resolve(asked, None)
        else:
            asked = "0" * 32
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                student_routes, "read_everything", refusing(sqlite3.DatabaseError, seen)
            )
            answer = client.post(f"/student/actions/take-back-help/{asked}")
        monkeypatch.undo()
        after = every_row(database_of(client))

    store_free_page(
        answer, status=status, heading="Request not taken back", alert=f"{said} {YOUR_WEEK}"
    )
    assert after_the_failure(seen) == []
    assert after == before


def test_a_take_back_whose_commit_the_file_refuses_is_rolled_back_and_said_in_help(
    tmp_path: pathlib.Path,
) -> None:
    """Another program reads the file through the commit: the delete is refused at commit,
    rolled back, and said so; the page itself still reads."""
    with household_client("her", tmp_path) as client:
        sign_in_as(client, "her")
        help_requests = state_of(client).help_requests
        asked = help_requests.ask(PLAN_DATE, "which part?").request_id
        path = f"/student/actions/take-back-help/{asked}"
        with HeldByAnother(database_of(client), reading=True):
            refused = client.post(path, headers=PAGE_HEADERS)
        still = [item.request_id for item in help_requests.open_requests()]
        again = client.post(path, headers=PAGE_HEADERS)
        left = help_requests.open_requests()

    assert refused.status_code == 500
    assert TAKE_BACK_FAILED in refused.text
    assert still == [asked]
    assert again.status_code == 303
    assert left == []


# ------------------------------------------------------------- her workload signal


SIGNAL_FAILED = "That could not be saved, and nothing was changed. Try again."


@pytest.mark.parametrize("kind", [sqlite3.OperationalError, sqlite3.DatabaseError])
@pytest.mark.parametrize("press", ["too much", "take it back"])
def test_a_signal_the_file_would_not_keep_is_said_at_the_top_and_kept_once_after(
    press: str, kind: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser() as client:
        signals = state_of(client).workload_signals
        held = [] if press == "too much" else [signals.record(PLAN_DATE, None).signal_id]
        path = (
            "/student/actions/too-much"
            if press == "too much"
            else f"/student/actions/take-back/{held[0]}"
        )
        monkeypatch.setattr(
            signals, "record" if press == "too much" else "withdraw", refusing(kind)
        )
        refused = client.post(path, headers=PAGE_HEADERS)
        kept = [signal.signal_id for signal in signals.held()]
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).drafts, "latest_for", refusing(sqlite3.OperationalError, seen)
            )
            unread = client.post(path, headers=PAGE_HEADERS)
        monkeypatch.undo()
        again = client.post(path, headers=PAGE_HEADERS)
        left = signals.held()

    assert refused.status_code == 500
    assert (
        f'<p class="problem week-problem" role="alert" tabindex="-1" autofocus>{SIGNAL_FAILED}</p>'
        in refused.text
    )
    assert kept == held
    main = store_free_page(
        unread, status=500, heading="Not saved", alert=f"{SIGNAL_FAILED} {YOUR_WEEK}"
    )
    assert ways_back_of(main) == [(HER_PAGE, "Back to my week")]
    assert after_the_failure(seen) == []
    assert again.status_code == 303
    assert len(left) == (1 if press == "too much" else 0)


# ------------------------------------------------------------- a parent's reply to her request


HELP_STEP_FAILED = (
    "That could not be saved, and nothing was changed. Your reply is below. Try again."
)


@pytest.mark.parametrize("kind", [sqlite3.OperationalError, sqlite3.DatabaseError])
@pytest.mark.parametrize("step", ["accept", "resolve"])
@pytest.mark.parametrize("reader", ["parent", "open"])
def test_a_reply_the_file_would_not_keep_is_kept_on_the_page_that_reads_no_store(
    reader: str,
    step: str,
    kind: type[Exception],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: Counter[str] = Counter()
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        state = state_of(client)
        asked = state.help_requests.ask(PLAN_DATE, "which part?").request_id
        path = f"/parent/actions/help/{asked}"
        real: Callable[..., object] = state.drafts.review_snapshot

        def counted(*given: object, **named: object) -> object:
            calls["review_snapshot"] += 1
            return real(*given, **named)

        monkeypatch.setattr(state.drafts, "review_snapshot", counted)
        before = every_row(database_of(client))
        with Statements(state) as seen:
            monkeypatch.setattr(state.help_requests, step, refusing(kind, seen))
            answer = client.post(path, data={"step": step, "response": TYPED})
        monkeypatch.undo()
        after = every_row(database_of(client))
        again = client.post(path, data={"step": step, "response": TYPED})
        moved = state.help_requests.get(asked)

    main = store_free_page(answer, status=500, heading="Family review", alert=HELP_STEP_FAILED)
    assert '<label for="kept-reply">Reply, as typed</label>' in main
    assert f'<textarea id="kept-reply" rows="3" readonly>{escape(TYPED)}</textarea>' in main
    assert ways_back_of(main) == [("/parent", "Return to Family review")]
    assert calls["review_snapshot"] == 0
    assert after_the_failure(seen) == []
    assert after == before
    assert again.status_code == 303
    assert moved is not None
    assert moved.response == TYPED
    assert moved.state == ("accepted" if step == "accept" else "resolved")


def test_a_step_with_no_reply_that_the_file_would_not_keep_promises_no_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        help_requests = state_of(client).help_requests
        asked = help_requests.ask(PLAN_DATE, "which part?").request_id
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(help_requests, "resolve", refusing(sqlite3.OperationalError, seen))
            answer = client.post(
                f"/parent/actions/help/{asked}", data={"step": "resolve", "response": "  "}
            )

    main = store_free_page(
        answer,
        status=500,
        heading="Family review",
        alert="That could not be saved, and nothing was changed. Try again.",
    )
    assert "<textarea" not in main
    assert "reply" not in main.lower()
    assert after_the_failure(seen) == []


STEP_FAILED = "That could not be saved, and nothing was changed. Try again."
REPLIES = {"typed": TYPED, "blank": "", "spaces": "   "}
STEP_REFUSALS = {
    "a request already closed": (409, "accept"),
    "no such request": (404, "accept"),
    "a step no page sends": (422, "sideways"),
}


@pytest.mark.parametrize(
    ("outcome", "reply"),
    [
        *[(step, reply) for step in ("accept", "resolve") for reply in REPLIES],
        *[(refusal, reply) for refusal in STEP_REFUSALS for reply in ("typed", "blank")],
    ],
)
@pytest.mark.parametrize("reader", ["parent", "open"])
def test_a_reply_is_said_to_be_below_only_when_one_was_typed(
    reader: str,
    outcome: str,
    reply: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A take-up or a resolve the file refused says the reply is below only when one was
    typed, and shows it there; a refusal whose family page can't be read shows a typed reply
    and never says it is below. With no reply typed, neither the sentence nor the box."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        state = state_of(client)
        asked = state.help_requests.ask(PLAN_DATE, "which part?").request_id
        named = "0" * 32 if outcome == "no such request" else asked
        if outcome == "a request already closed":
            state.help_requests.resolve(asked, None)
        step = STEP_REFUSALS[outcome][1] if outcome in STEP_REFUSALS else outcome
        before = every_row(database_of(client))
        with Statements(state) as seen:
            if outcome in STEP_REFUSALS:
                monkeypatch.setattr(
                    state.drafts, "review_snapshot", refusing(sqlite3.DatabaseError, seen)
                )
            else:
                monkeypatch.setattr(
                    state.help_requests, outcome, refusing(sqlite3.OperationalError, seen)
                )
            answer = client.post(
                f"/parent/actions/help/{named}",
                data={"step": step, "response": REPLIES[reply]},
                headers=PAGE_HEADERS,
            )
        monkeypatch.undo()
        after = every_row(database_of(client))

    refused = {
        "a request already closed": f"request '{asked}' is resolved, so it cannot be taken up.",
        "no such request": f"no help request '{named}'.",
        "a step no page sends": "'sideways' is not one of the two moves, accept or resolve.",
    }
    if outcome in STEP_REFUSALS:
        status, said = STEP_REFUSALS[outcome][0], f"{refused[outcome]} {FAMILY}"
    else:
        status, said = 500, HELP_STEP_FAILED if reply == "typed" else STEP_FAILED
    main = store_free_page(answer, status=status, heading="Family review", alert=said)
    below = reply == "typed" and outcome not in STEP_REFUSALS
    assert ("Your reply is below." in main) is below
    if reply == "typed":
        assert '<label for="kept-reply">Reply, as typed</label>' in main
        assert f'<textarea id="kept-reply" rows="3" readonly>{escape(TYPED)}</textarea>' in main
    else:
        assert "<textarea" not in main
        assert "reply" not in main.lower()
    assert after_the_failure(seen) == []
    assert after == before


# ------------------------------------------------------------- a page that is only read


def notes_alert(reader: str) -> str:
    whose = "Her" if reader == "parent" else "Your"
    return f"{whose} homework notes can't be shown right now. Try again in a moment. Try again"


def a_deleted_note(client: TestClient) -> str:
    """A note written and deleted from her pages; its id."""
    page = client.get("/student/homework-notes/new", headers=PAGE_HEADERS).text
    fields = form_fields(page, "/student/actions/homework-notes")
    made = client.post(
        "/student/actions/homework-notes",
        data={**fields, "text": "Geometry 4-8", "course": "", "due_date": ""},
    )
    assert made.status_code == 303, made.text[:300]
    name = fields["capture_id"]
    gone = client.post(f"/student/actions/homework-notes/{name}/delete", data={"revision": "1"})
    assert gone.status_code == 303, gone.text[:300]
    return name


@pytest.mark.parametrize(
    ("which", "heading", "read"),
    [
        ("", "Homework notes", "outstanding_captures"),
        ("/added", "Homework notes added to homework", "added_captures"),
        ("/archived", "Archived homework notes", "archived_captures"),
    ],
)
@pytest.mark.parametrize("reader", ["her", "parent", "open"])
def test_her_notes_that_cannot_be_read_are_said_to_be_so_with_a_way_to_ask_again(
    which: str,
    heading: str,
    read: str,
    reader: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = f"/student/homework-notes{which}"
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).project_state, read, refusing(sqlite3.OperationalError, seen)
            )
            answer = client.get(path + "?b=2&a=", headers=PAGE_HEADERS)
        monkeypatch.undo()
        readable = client.get(path, headers=PAGE_HEADERS)

    main = store_free_page(answer, status=503, heading=heading, alert=notes_alert(reader))
    assert f'<a href="{path}?b=2&amp;a=">Try again</a>' in main
    week = "Back to her week" if reader == "parent" else "Back to my week"
    assert ways_back_of(main) == [(HER_PAGE, week)]
    assert "Nothing was" not in main
    assert after_the_failure(seen) == []
    assert readable.status_code == 200


HAND_TYPED_NOTE = "00000000-feed-4bad-8bad-000000000000"


@pytest.mark.parametrize("marker", ["deleted", "already"])
@pytest.mark.parametrize("how", ["after a delete", "typed by hand"])
@pytest.mark.parametrize("read", ["capture_deleted", "outstanding_captures"])
def test_her_notes_after_a_delete_that_cannot_be_read_neither_say_nor_deny_the_delete(
    marker: str, how: str, read: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The address names a note; the page that cannot read the record says it can't check
    whether that note was deleted, whatever the address says, and offers nothing that
    deletes. Pressed again once the record reads, the delete says it was done already."""
    with browser() as client:
        name = a_deleted_note(client) if how == "after a delete" else HAND_TYPED_NOTE
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).project_state, read, refusing(sqlite3.DatabaseError, seen)
            )
            answer = client.get(f"/student/homework-notes?{marker}={name}", headers=PAGE_HEADERS)
        monkeypatch.undo()
        after = every_row(database_of(client))
        if how == "after a delete":
            pressed = client.post(
                f"/student/actions/homework-notes/{name}/delete", data={"revision": "1"}
            )
            assert pressed.status_code == 303
            landed = client.get(pressed.headers["location"], headers=PAGE_HEADERS)
            assert "That note was already deleted." in landed.text
            assert every_row(database_of(client)) == after

    main = store_free_page(
        answer,
        status=503,
        heading="Homework notes",
        alert=(
            "Your homework notes can't be shown right now, so whether that note was deleted "
            "can't be checked yet. Try again in a moment. Try again"
        ),
    )
    assert f'<a href="/student/homework-notes?{marker}={name}">Try again</a>' in main
    assert "Note deleted" not in main
    assert "already deleted" not in main
    assert after_the_failure(seen) == []
    assert after == before


@pytest.mark.parametrize("marker", ["deleted", "already"])
@pytest.mark.parametrize("reader", ["her", "open", "parent"])
def test_her_notes_that_cannot_be_read_after_a_delete_name_it_to_her_alone(
    marker: str, reader: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only she deletes, so a delete the address names is hers: a parent who opens such an
    address while the record can't be read reads that her notes can't be shown, and nothing
    of a delete."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).project_state,
                "outstanding_captures",
                refusing(sqlite3.OperationalError, seen),
            )
            answer = client.get(
                f"/student/homework-notes?{marker}={HAND_TYPED_NOTE}", headers=PAGE_HEADERS
            )
        monkeypatch.undo()

    if reader == "parent":
        alert = notes_alert(reader)
    else:
        alert = (
            "Your homework notes can't be shown right now, so whether that note was deleted "
            "can't be checked yet. Try again in a moment. Try again"
        )
    main = store_free_page(answer, status=503, heading="Homework notes", alert=alert)
    if reader == "parent":
        assert "was deleted" not in main
    assert after_the_failure(seen) == []


@pytest.mark.parametrize("given", ["", "x" * 201, "not-a-note", HAND_TYPED_NOTE + "0"])
def test_a_marker_the_list_would_not_check_is_not_said_on_the_page_that_cannot_read(
    given: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser() as client:
        monkeypatch.setattr(
            state_of(client).project_state,
            "outstanding_captures",
            refusing(sqlite3.OperationalError),
        )
        answer = client.get(f"/student/homework-notes?deleted={given}", headers=PAGE_HEADERS)

    store_free_page(answer, status=503, heading="Homework notes", alert=notes_alert("her"))


DETAIL_RETURNS = [
    ({}, "her", (week_href(None, ESSAY_ID, show=ESSAY_ID), "Back to the week")),
    (
        {"return_to": "week", "week": "2026-08-10"},
        "her",
        (week_href(date(2026, 8, 10), ESSAY_ID, show=ESSAY_ID), "Back to the week"),
    ),
    ({"return_to": "today"}, "open", ("/student/due-this-week#today", "Back to Today")),
    ({"return_to": "to_turn_in"}, "her", ("/student/to-turn-in#to-turn-in", "Back to To turn in")),
    (
        {},
        "parent",
        (f"/parent?focus={ESSAY_ID}#update-{ESSAY_ID}", "Back to family review"),
    ),
    (
        {"return_to": "family", "plan_id": "draft:a"},
        "parent",
        ("/parent?plan=draft%3Aa#plan-draft-a", "Back to family review"),
    ),
]


@pytest.mark.parametrize(
    ("read", "kind"),
    [
        ("one_assignment", sqlite3.OperationalError),
        ("captures_of_assignment", sqlite3.DatabaseError),
        ("claim_history", sqlite3.OperationalError),
    ],
)
@pytest.mark.parametrize(("query", "reader", "back"), DETAIL_RETURNS)
def test_the_details_that_cannot_be_read_offer_the_way_back_the_address_names(
    query: dict[str, str],
    reader: str,
    back: tuple[str, str],
    read: str,
    kind: type[Exception],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(state_of(client).project_state, read, refusing(kind, seen))
            answer = client.get(DETAILS, params=query, headers=PAGE_HEADERS)
        monkeypatch.undo()

    main = store_free_page(
        answer,
        status=503,
        heading="Assignment",
        alert="This assignment can't be shown right now. Try again in a moment. Try again",
    )
    again = details_href(ESSAY_ID, **query) if query else DETAILS
    assert f'<a href="{escape(again)}">Try again</a>' in main
    assert ways_back_of(main) == [(str(escape(back[0])), back[1])]
    assert after_the_failure(seen) == []


@pytest.mark.parametrize("said", ["saved", "undone", "anything"])
def test_the_details_after_a_save_that_cannot_be_read_say_nothing_of_the_save(
    said: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser() as client:
        if said != "anything":
            report(client, ESSAY_ID, "done")
        monkeypatch.setattr(
            state_of(client).project_state, "one_assignment", refusing(sqlite3.OperationalError)
        )
        answer = client.get(DETAILS, params={"said": said, "hand_in": "saved"})

    main = store_free_page(
        answer,
        status=503,
        heading="Assignment",
        alert="This assignment can't be shown right now. Try again in a moment. Try again",
    )
    assert f'href="{DETAILS}?said={said}&amp;hand_in=saved">Try again</a>' in main


def test_the_details_way_back_to_today_reads_nothing_when_the_plan_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).drafts, "latest_for", refusing(sqlite3.OperationalError, seen)
            )
            answer = client.get(DETAILS, params={"return_to": "today"}, headers=PAGE_HEADERS)
        monkeypatch.undo()

    main = store_free_page(
        answer,
        status=503,
        heading="Assignment",
        alert="This assignment can't be shown right now. Try again in a moment. Try again",
    )
    assert ways_back_of(main) == [("/student/due-this-week#today", "Back to Today")]
    assert after_the_failure(seen) == []


@pytest.mark.parametrize("reader", ["her", "parent", "open"])
def test_her_to_turn_in_list_that_cannot_be_read_says_so_with_a_way_to_ask_again(
    reader: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                hand_in_routes, "read_everything", refusing(sqlite3.OperationalError, seen)
            )
            answer = client.get(
                "/student/to-turn-in?hand_in_said=turned_in&about=x&hand_in_event=y",
                headers=PAGE_HEADERS,
            )
        monkeypatch.undo()

    whose = "Her" if reader == "parent" else "Your"
    main = store_free_page(
        answer,
        status=503,
        heading="To turn in",
        alert=f"{whose} To turn in list can't be shown right now. Try again in a moment. Try again",
    )
    assert (
        '<a href="/student/to-turn-in?hand_in_said=turned_in&amp;about=x&amp;hand_in_event=y">'
        "Try again</a>"
    ) in main
    week = "Back to her week" if reader == "parent" else "Back to my week"
    assert ways_back_of(main) == [(HER_PAGE, week)]
    assert "turned in" not in main.lower().replace("to turn in", "")
    assert after_the_failure(seen) == []


def test_a_damaged_note_row_keeps_the_list_at_200() -> None:
    with browser() as client:
        page = client.get("/student/homework-notes/new", headers=PAGE_HEADERS).text
        fields = form_fields(page, "/student/actions/homework-notes")
        client.post(
            "/student/actions/homework-notes",
            data={**fields, "text": "Reading log", "course": "", "due_date": ""},
        )
        store = state_of(client).project_state
        store._connection.execute("UPDATE homework_captures SET attribution = 'no json'")
        store._connection.commit()
        answer = client.get("/student/homework-notes", headers=PAGE_HEADERS)

    assert answer.status_code == 200
    assert "1 homework note cannot be read right now." in answer.text


RECORD_PAGES = {
    "her hand-in record on the list": [TO_TURN_IN, f"{HER_PAGE}?week={FIXTURE_WEEK}"],
    "a claim about the date": [f"{HER_PAGE}?week={FIXTURE_WEEK}", DETAILS],
    "her hand-in record on the details": [DETAILS],
}


@pytest.mark.parametrize("reader", ["her", "parent", "open"])
@pytest.mark.parametrize("record", list(RECORD_PAGES))
def test_a_record_that_cannot_be_read_is_said_on_a_page_that_stands_with_no_word_of_a_change(
    record: str, reader: str, tmp_path: pathlib.Path
) -> None:
    """A record that can't be read is said where it would be shown: which record, what is
    left out for it, and what can't be saved until it can be read. A page that is only read
    says nothing about a change, since nothing was pressed."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        state = state_of(client)
        store = state.project_state
        if record == "a claim about the date":
            store.record_claims(
                ESSAY_ID,
                [
                    SourceRecord(
                        channel=SourceChannel.EMAIL,
                        asserted_value="2026-08-28",
                        observed_at=state.clock.now(),
                        confidence=0.8,
                        seen_in="day header",
                    )
                ],
            )
            store._connection.execute(
                "UPDATE date_claims SET active = 2 WHERE assignment_id = ?", (ESSAY_ID,)
            )
        else:
            hand_in_saved(client, NEEDS_HAND_IN, None)
            store._connection.execute(
                "UPDATE hand_in_events SET state = 'invalid-state' WHERE assignment_id = ?",
                (ESSAY_ID,),
            )
        store._connection.commit()
        before = every_row(database_of(client))
        answers = [client.get(page, headers=PAGE_HEADERS) for page in RECORD_PAGES[record]]
        after = every_row(database_of(client))

    whose = "Her" if reader == "parent" else "Your"
    for answer in answers:
        assert answer.status_code == 200
        main = main_of(answer.text)
        assert "Nothing was changed" not in main
        if record == "a claim about the date":
            assert (
                '<p class="source">A claim about this date cannot be read right now. The date is '
                "shown without it.</p>"
            ) in main
        elif record == "her hand-in record on the list":
            found = re.search(
                rf'<p class="problem">{whose} hand-in record cannot be read right now for: .*?</p>',
                main,
                re.S,
            )
            assert found is not None
            assert ESSAY_TITLE in found.group()
            assert found.group().endswith(". It may belong on this list.</p>")
        else:
            assert (
                f'<p class="problem">{whose} hand-in record for this assignment cannot be read '
                "right now, so it is not shown. Nothing can be saved here until it can be read.</p>"
            ) in main
    assert after == before


# ------------------------------------------------------------- the file held by another program


def test_her_save_with_nothing_chosen_while_the_file_is_held_waits_once_and_keeps_her_words(
    tmp_path: pathlib.Path,
) -> None:
    with household_client("her", tmp_path) as client:
        sign_in_as(client, "her")
        path, data = week_press(client, "nothing chosen")
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen, HeldByAnother(database_of(client)):
            started = time.monotonic()
            answer = client.post(path, data=data, headers=PAGE_HEADERS)
            took = time.monotonic() - started
        after = every_row(database_of(client))

    main = store_free_page(
        answer,
        status=422,
        heading="Update not saved",
        alert=f"Nothing was saved, because neither Done nor Not yet was chosen. {YOUR_WEEK}",
    )
    assert f"readonly>{escape(TYPED)}</textarea>" in main
    assert took < 9
    ran = [line for line in seen if line.strip().upper() not in ("ROLLBACK", "BEGIN DEFERRED")]
    assert len(ran) == 1
    assert "drafts" in ran[0]
    assert after == before


def test_a_parents_reply_while_the_file_is_held_is_kept_and_lands_once_after(
    tmp_path: pathlib.Path,
) -> None:
    with household_client("parent", tmp_path) as client:
        sign_in_as(client, "parent")
        state = state_of(client)
        asked = state.help_requests.ask(PLAN_DATE, "which part?").request_id
        path = f"/parent/actions/help/{asked}"
        before = every_row(database_of(client))
        with Statements(state) as seen, HeldByAnother(database_of(client)):
            started = time.monotonic()
            answer = client.post(path, data={"step": "accept", "response": TYPED})
            took = time.monotonic() - started
        after = every_row(database_of(client))
        again = client.post(path, data={"step": "accept", "response": TYPED})
        moved = state.help_requests.get(asked)

    main = store_free_page(answer, status=500, heading="Family review", alert=HELP_STEP_FAILED)
    assert f"readonly>{escape(TYPED)}</textarea>" in main
    assert took < 9
    ran = [line for line in seen if line.strip().upper() != "ROLLBACK"]
    assert len(ran) == 1
    assert "help_requests" in ran[0]
    assert after == before
    assert again.status_code == 303
    assert moved is not None
    assert (moved.state, moved.response) == ("accepted", TYPED)


def test_her_notes_while_the_file_is_held_say_they_cannot_be_shown(
    tmp_path: pathlib.Path,
) -> None:
    with household_client("her", tmp_path) as client:
        sign_in_as(client, "her")
        with HeldByAnother(database_of(client)):
            answer = client.get("/student/homework-notes", headers=PAGE_HEADERS)
        readable = client.get("/student/homework-notes", headers=PAGE_HEADERS)

    store_free_page(answer, status=503, heading="Homework notes", alert=notes_alert("her"))
    assert readable.status_code == 200
