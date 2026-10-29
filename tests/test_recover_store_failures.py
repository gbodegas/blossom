# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A press or a page whose read of the household's file fails: a page that reads no store and
says what happened, with the status the press or the page would have had.

A refusal writes nothing, so its page is tried once and, when a read for it fails, the page
that reads no store keeps the refusal's own status, says it in words that stay true without
the page, and keeps what was typed. A write the file refuses is rolled back, so its page may
say nothing was changed and offer to try again. A page that is only read answers 503, says
what cannot be shown, and offers the same address again. The fixture week through the app, a
pinned clock, and a failure made at the store call, or a second connection holding the file.
"""

import asyncio
import contextlib
import html
import io
import pathlib
import re
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import partial
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from PIL import Image

from blossom import screenshot_reading
from blossom.agent.runs import Unfinished
from blossom.anthropic_client import ScreenshotAnswer
from blossom.dependencies import STATE_ATTRIBUTE
from blossom.drafts import Draft
from blossom.grades.draft import capture_key
from blossom.grades.identity import name_form_key
from blossom.grades.review import GradeReportSaved
from blossom.grades.text_reader import read_grade_report
from blossom.hand_in import NEEDS_HAND_IN, TURNED_IN, HandInSaved, HandInState
from blossom.household import secret_beside
from blossom.reconciliation import SourceChannel, SourceRecord
from blossom.routes import grades as grade_routes
from blossom.routes import hand_in as hand_in_routes
from blossom.routes import imports as import_routes
from blossom.routes import parent as parent_routes
from blossom.routes import student as student_routes
from blossom.routes.forms import fields_of
from blossom.routes.navigation import (
    NEW_NOTE_PAGE,
    NOTE_ACTIONS,
    details_href,
    note_action,
    note_add_action,
    note_add_href,
    note_details_action,
    note_help_href,
    note_link_action,
    note_search_href,
    note_unlink_action,
    week_href,
)
from blossom.routes.runs import Unconfirmed
from blossom.school_instructions import InstructionChoice, InstructionSeen
from blossom.stores.drafts import STORE_WAIT_SECONDS, StoreBusy, WriterBusy
from blossom.stores.help_requests import new_request_id
from blossom.stores.import_drafts import new_creation_token
from blossom.stores.paths import SECRET_NAME
from tests.support import (
    DETAILS,
    ENDED_REASONS,
    ESSAY_ID,
    ESSAY_TITLE,
    FAMILY_ASKED_FOR_AUGUST_19,
    FAMILY_DATE_NOT_READ,
    FAMILY_FOR_A_NEW_PLAN,
    FAMILY_FORM_EXPIRED,
    FAMILY_FORM_NOT_WHOLE,
    FAMILY_NEWER_PLAN_AUGUST_19,
    FIXTURE_WEEK,
    FIXTURES,
    HANG_LIMIT_SECONDS,
    HER_PAGE,
    HER_UPDATES_SAVED,
    LOOP_CYCLES,
    MISSING_EMAIL,
    NOTHING_FOR_AUGUST_20,
    ORIGIN,
    PAGE_HEADERS,
    PLAN_DATE,
    REPORT,
    SAME_ORIGIN,
    UNDO,
    AskedWaits,
    HangWatch,
    HeldByAnother,
    LockHeldByAnother,
    OnTheLoop,
    Parked,
    Statements,
    WaitBeside,
    a_discrepancy,
    action_of,
    after_the_failure,
    browser,
    card_for,
    counted_waits,
    database_of,
    due,
    ended_as,
    ended_run,
    every_row,
    family_line_focused,
    family_page,
    family_plan,
    first_write_to,
    fixture_settings,
    form_fields,
    form_values,
    fresh_plan_fields,
    fresh_signal_fields,
    grade_answers,
    hidden,
    homework_answer,
    homework_from_a_note,
    household_client,
    link_note,
    main_of,
    page_of,
    plan_form,
    planned,
    pressed_form,
    quiet_client,
    refusing,
    report,
    reported,
    row_for,
    rules_named,
    runs_recorded,
    save_grade,
    settled_run,
    sign_in_as,
    state_of,
    store_free_page,
    unwrapped,
    walkthrough,
    ways_back_of,
    whole_form,
    words,
)

HAND_IN = f"/student/actions/assignments/{ESSAY_ID}/hand-in"
UNDO_HAND_IN = f"/student/actions/assignments/{ESSAY_ID}/undo-hand-in"
TURNED_IN_PRESS = f"/student/actions/assignments/{ESSAY_ID}/turned-in"
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
    "I turned it in from a card that fell behind": Press(
        409, "That changed while you were away, so nothing was saved.", None, TYPED
    ),
    "Undo of a turn-in already undone": Press(409, "That update was already undone.", None, TYPED),
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
    if case == "I turned it in from a card that fell behind":
        action, sent = pressed_form(the_card(client), "hand_in_head")
        hand_in_saved(client, NEEDS_HAND_IN, None)
        return action, {**sent, "note": TYPED}
    if case == "Undo of a turn-in already undone":
        action, sent = pressed_form(the_card(client), "hand_in_head")
        pressed = client.post(action, data=sent, headers=PAGE_HEADERS)
        assert pressed.status_code == 303, pressed.text[:300]
        page = client.get(pressed.headers["location"], headers=PAGE_HEADERS).text
        action, sent = pressed_form(card_for(page, ESSAY_ID), "undo_hand_in")
        assert client.post(action, data=sent, headers=PAGE_HEADERS).status_code == 303
        return action, {**sent, "note": TYPED}
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
        assert f"readonly>\n{escape(press.note)}</textarea>" in main
    back = week_href(
        None if press.week is None else date.fromisoformat(press.week),
        press.about,
        show=press.about,
        in_place=data.get("in_place"),
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
    "her week": ("drafts", "latest_for", "get week"),
    "family review": ("drafts", "review_snapshot", "get family"),
    "taking a request back": ("help_requests", "take_back", "take back"),
    "saying today is too much": ("workload_signals", "record_once", "too much"),
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
                "get week": HER_PAGE,
                "get family": "/parent",
            }[press]
            answer = client.get(where, headers=PAGE_HEADERS)
        elif press == "take back":
            answer = client.post(f"/student/actions/take-back-help/{asked}")
        elif press == "too much":
            answer = client.post("/student/actions/too-much", data=fresh_signal_fields(client))
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
NOT_HERS_TO_SIGNAL = (
    "Sign in as the student to press Too much right now or Undo 'Too much right now'."
)
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
    "I turned it in from details that fell behind": (
        409,
        "That changed while you were away, so nothing was saved.",
    ),
    "Undo of a turn-in from the details already undone": (409, "That update was already undone."),
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
    if case == "I turned it in from details that fell behind":
        page = client.get(DETAILS, headers=PAGE_HEADERS).text
        action, sent = pressed_form(page, "hand_in_head")
        hand_in_saved(client, NEEDS_HAND_IN, None)
        return action, {**sent, "note": TYPED}, {"note": TYPED}
    if case == "Undo of a turn-in from the details already undone":
        page = client.get(DETAILS, headers=PAGE_HEADERS).text
        action, sent = pressed_form(page, "hand_in_head")
        pressed = client.post(action, data=sent, headers=PAGE_HEADERS)
        assert pressed.status_code == 303, pressed.text[:300]
        page = client.get(pressed.headers["location"], headers=PAGE_HEADERS).text
        action, sent = pressed_form(page, "undo_hand_in")
        assert client.post(action, data=sent, headers=PAGE_HEADERS).status_code == 303
        return action, {**sent, "note": TYPED}, {"note": TYPED}
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
        typed = escape(kept["note"])
        assert f'<textarea id="kept-note" rows="4" readonly>\n{typed}</textarea>' in main
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
        ("Note about this plan (optional)", TYPED),
    ),
    "a reason too long": FamilyPress(
        422,
        "A reason is at most 500 characters; this one is 501.",
        ("Note about this plan (optional)", LONG),
    ),
    "a decision on no draft": FamilyPress(
        404, "no draft 'draft:none'.", ("Note about this plan (optional)", TYPED)
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
        "'sideways' is not one of the three moves, accept, update or resolve.",
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
    # With no model the family page offers no plan button: each press is a fresh form.
    if case == "a plan date that is not a date":
        return "/parent/actions/plan", fresh_plan_fields(client, plan_date="someday"), ""
    if case == "a plan date that has passed":
        return "/parent/actions/plan", fresh_plan_fields(client, plan_date="2026-08-18"), ""
    if case == "a plan with no model":
        return "/parent/actions/plan", fresh_plan_fields(client, plan_date=""), ""
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
        said = "This request is already closed."
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
        opening=path == "/parent/actions/plan",
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
def test_a_refused_step_reads_the_same_on_family_review_and_its_stand_in(
    case: str, reader: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step on a request that is closed is refused as already closed, and one on a request
    not on record names that request by its whole id: the same words on the family page and
    on its stand-in, nothing of them cut."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        state = state_of(client)
        asked = state.help_requests.ask(PLAN_DATE, "which part?").request_id
        if case == "a request already closed":
            state.help_requests.resolve(asked, None)
            named, said = asked, "This request is already closed."
        else:
            named = uuid4().hex
            said = f"no help request '{named}'"
        path = f"/parent/actions/help/{named}"
        page = client.post(path, data={"step": "accept", "response": ""}, headers=PAGE_HEADERS)
        monkeypatch.setattr(state.drafts, "review_snapshot", refusing(sqlite3.OperationalError))
        stand_in = client.post(path, data={"step": "accept", "response": ""}, headers=PAGE_HEADERS)

    status = 409 if case == "a request already closed" else 404
    ended = said if said.endswith(".") else f"{said}."
    assert page.status_code == status
    assert len(named) == 32
    assert f'<p class="problem" role="alert" id="problem">{escape(said)}</p>' in main_of(page.text)
    store_free_page(stand_in, status=status, heading="Family review", alert=f"{ended} {FAMILY}")


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
        monkeypatch.setattr(student_routes, "make_plan", refusing(sqlite3.OperationalError))
        readable = client.post(
            "/student/actions/plan", data=plan_form(client), headers=PAGE_HEADERS
        )
        before = every_row(database_of(client))
        form = plan_form(client)
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).drafts, "latest_for", refusing(sqlite3.OperationalError, seen)
            )
            answer = client.post("/student/actions/plan", data=form, headers=PAGE_HEADERS)
        monkeypatch.undo()
        after = every_row(database_of(client))

    assert readable.status_code == 409
    assert (
        "Blossom couldn&#39;t finish a reliable plan this time. Your homework updates are saved."
    ) in unwrapped(readable.text)
    assert "went wrong" not in readable.text
    main = store_free_page(
        answer,
        status=409,
        heading="Plan not made",
        alert=(
            "Blossom couldn't finish a reliable plan this time. Your homework updates are "
            f"saved. {YOUR_WEEK}"
        ),
        opening=True,
    )
    assert "went wrong" not in main
    assert ways_back_of(main) == [(HER_PAGE, "Back to my week")]
    assert after_the_failure(seen) == []
    assert after == before


def test_her_plan_refused_before_a_run_keeps_its_status_without_her_week(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with household_client("parent", tmp_path) as client:
        sign_in_as(client, "parent")
        # With no model her week offers no button: the press carries a fresh form.
        form = fresh_plan_fields(client)
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).drafts, "latest_for", refusing(sqlite3.DatabaseError, seen)
            )
            answer = client.post("/student/actions/plan", data=form, headers=PAGE_HEADERS)
        monkeypatch.undo()

    main = store_free_page(
        answer,
        status=503,
        heading="Plan not made",
        alert=(
            "Blossom could not make a plan: no model can be constructed: ANTHROPIC_API_KEY is "
            f"not set. {HER_WEEK}"
        ),
        opening=True,
    )
    assert ways_back_of(main) == [(HER_PAGE, "Back to her week")]
    assert after_the_failure(seen) == []


def test_a_family_plan_that_failed_on_the_way_is_said_without_family_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser(key=True) as client:
        walkthrough(client)
        monkeypatch.setattr(parent_routes, "make_plan", refusing(sqlite3.OperationalError))
        before = every_row(database_of(client))
        form = family_plan(client, "")
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).drafts,
                "review_snapshot",
                refusing(sqlite3.OperationalError, seen),
            )
            answer = client.post("/parent/actions/plan", data=form)
        monkeypatch.undo()
        after = every_row(database_of(client))

    main = store_free_page(
        answer,
        status=409,
        heading="Family review",
        alert=(
            "Blossom couldn't finish a reliable plan this time. Her homework updates are "
            f"saved. {FAMILY}"
        ),
        opening=True,
    )
    assert "Family review shows what happened" not in main
    assert after_the_failure(seen) == []
    assert after == before


FAMILY_LINE = re.compile(r'<p class="problem" role="alert" id="problem"[^>]*>(.*?)</p>', re.S)


def behind_a_newer_plan(client: TestClient) -> dict[str, str]:
    """A family form from a page opened before a second form made a newer plan for its evening."""
    stale = family_plan(client, "")
    assert client.post("/parent/actions/plan", data=family_plan(client, "")).is_redirect
    return stale


def too_slow(*_: object, **__: object) -> None:
    """A store read whose wait ended before it returned."""
    raise Unfinished(cast("asyncio.Future[Any]", Future()))


@pytest.mark.parametrize("failure", ["store error", "too slow"])
def test_a_family_press_behind_a_newer_plan_it_cannot_place_is_still_refused(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the newer plan can't be read to say where the page shows it, the press is still
    refused as one behind a newer plan: no run started, and no place on the page named."""
    with browser(key=True) as client:
        stale = behind_a_newer_plan(client)
        before = runs_recorded(client)
        failing = refusing(sqlite3.OperationalError) if failure == "store error" else too_slow
        monkeypatch.setattr(state_of(client).drafts, "latest_for", failing)
        answer = client.post("/parent/actions/plan", data=stale)
        monkeypatch.undo()
        after = runs_recorded(client)

    assert answer.status_code == 409
    line = FAMILY_LINE.search(answer.text)
    assert line is not None
    assert words(line.group(1)) == f"{FAMILY_NEWER_PLAN_AUGUST_19} {FAMILY_FOR_A_NEW_PLAN}"
    assert family_line_focused(answer.text)
    assert 'action="/parent/actions/plan"' in answer.text
    assert after == before


def test_a_family_press_behind_a_newer_plan_is_said_without_family_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stand-in says a newer plan was made without naming a place on a page it doesn't
    show or a button it doesn't have."""
    with browser(key=True) as client:
        stale = behind_a_newer_plan(client)
        before = runs_recorded(client)
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).drafts,
                "review_snapshot",
                refusing(sqlite3.OperationalError, seen),
            )
            answer = client.post("/parent/actions/plan", data=stale)
        monkeypatch.undo()
        after = runs_recorded(client)

    store_free_page(
        answer,
        status=409,
        heading="Family review",
        alert=f"{FAMILY_NEWER_PLAN_AUGUST_19} {FAMILY}",
        opening=True,
    )
    assert after_the_failure(seen) == []
    assert after == before


ON_THE_STAND_IN = {
    "W-3s": (422, FAMILY_FORM_NOT_WHOLE),
    "W-4s": (409, FAMILY_FORM_EXPIRED),
    "W-5s": (409, f"{FAMILY_ASKED_FOR_AUGUST_19} That plan was made. {NOTHING_FOR_AUGUST_20}"),
    "W-5s, ended": (
        409,
        f"{FAMILY_ASKED_FOR_AUGUST_19} {ENDED_REASONS['checks_failed']} {HER_UPDATES_SAVED} "
        f"{NOTHING_FOR_AUGUST_20}",
    ),
    "W-5us": (409, f"{FAMILY_ASKED_FOR_AUGUST_19} That plan was made. {FAMILY_DATE_NOT_READ}"),
    "W-6s": (
        202,
        "Blossom couldn't confirm that the new plan was saved. Her homework updates are saved.",
    ),
}


async def unconfirmed(*_: object, **__: object) -> object:
    """A run whose plan may have been published, as the route hears of one."""
    raise Unconfirmed("a" * 32, PLAN_DATE)


@pytest.mark.parametrize("line", list(ON_THE_STAND_IN))
def test_a_family_plan_answer_on_the_stand_in_names_nothing_it_does_not_show(
    line: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each answer about the plan form, said where the family page can't be shown: what
    happened and that nothing was started, with no date kept below, no plan shown above, and
    no Plan it to press."""
    status, said = ON_THE_STAND_IN[line]
    with browser(key=True) as client:
        form = family_plan(client, PLAN_DATE.isoformat())
        if line == "W-3s":
            form = {**form, "evening": "tonight"}
        elif line == "W-4s":
            form = {**form, "issued_at": "20260101T000000Z"}
        elif line == "W-5s, ended":
            ended_run(
                state_of(client).drafts,
                thread_id=form["run_id"],
                plan_date=PLAN_DATE,
                outcome="interrupted",
            )
            form = {**form, "plan_date": "2026-08-20"}
        elif line in {"W-5s", "W-5us"}:
            assert client.post("/parent/actions/plan", data=form).is_redirect
            form = {**form, "plan_date": "2026-08-20" if line == "W-5s" else "next tuesday"}
        else:
            monkeypatch.setattr(parent_routes, "make_plan", unconfirmed)
        before = runs_recorded(client)
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).drafts,
                "review_snapshot",
                refusing(sqlite3.OperationalError, seen),
            )
            answer = client.post("/parent/actions/plan", data=form)
        monkeypatch.undo()
        after = runs_recorded(client)

    store_free_page(
        answer, status=status, heading="Family review", alert=f"{said} {FAMILY}", opening=True
    )
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
        form = fresh_signal_fields(client) if press == "too much" else None
        monkeypatch.setattr(
            signals, "record_once" if press == "too much" else "withdraw", refusing(kind)
        )
        refused = client.post(path, data=form, headers=PAGE_HEADERS)
        kept = [signal.signal_id for signal in signals.held()]
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).drafts, "latest_for", refusing(sqlite3.OperationalError, seen)
            )
            unread = client.post(path, data=form, headers=PAGE_HEADERS)
        monkeypatch.undo()
        again = client.post(path, data=form, headers=PAGE_HEADERS)
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


MOVES = {"accept": "accept", "update": "add_update", "resolve": "resolve"}
"""Each button under a request, by the step it sends, and the store call it makes."""


@pytest.mark.parametrize("kind", [sqlite3.OperationalError, sqlite3.DatabaseError])
@pytest.mark.parametrize("step", list(MOVES))
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
        if step == "update":
            state.help_requests.accept(asked)
        path = f"/parent/actions/help/{asked}"
        real: Callable[..., object] = state.drafts.review_snapshot

        def counted(*given: object, **named: object) -> object:
            calls["review_snapshot"] += 1
            return real(*given, **named)

        monkeypatch.setattr(state.drafts, "review_snapshot", counted)
        before = every_row(database_of(client))
        with Statements(state) as seen:
            monkeypatch.setattr(state.help_requests, MOVES[step], refusing(kind, seen))
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
    assert [update.body for update in moved.parent_updates] == [TYPED]
    assert moved.state == ("resolved" if step == "resolve" else "accepted")


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
        ("update", "typed"),
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
                    state.help_requests, MOVES[outcome], refusing(sqlite3.OperationalError, seen)
                )
            answer = client.post(
                f"/parent/actions/help/{named}",
                data={"step": step, "response": REPLIES[reply]},
                headers=PAGE_HEADERS,
            )
        monkeypatch.undo()
        after = every_row(database_of(client))

    refused = {
        "a request already closed": "This request is already closed.",
        "no such request": f"no help request '{named}'.",
        "a step no page sends": (
            "'sideways' is not one of the three moves, accept, update or resolve."
        ),
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
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """While another connection holds the file, her press with neither Done nor Not yet chosen
    is refused on the page that reads no store, with her words and nothing written. It reads the
    drafts store with a single statement, in a call given 5 seconds, under a busy timeout of at
    most 5000 ms set to the time left in them, and lasts within 2.5 seconds of another
    connection's read of the held file under ``STORE_WAIT_SECONDS``, timed beside the press so
    a delay to the process while both wait reaches both. The press and the wait beside it each
    have ``HANG_LIMIT_SECONDS`` before they count as hung."""
    with HangWatch(capsys) as watch, household_client("her", tmp_path) as client:
        sign_in_as(client, "her")
        path, data = week_press(client, "nothing chosen")
        before = every_row(database_of(client))
        held = HeldByAnother(database_of(client))
        with (
            Statements(state_of(client)) as seen,
            AskedWaits(state_of(client).drafts) as asked,
            held,
            WaitBeside(held, STORE_WAIT_SECONDS, "SELECT COUNT(*) FROM drafts", watch) as beside,
        ):
            answer, took = watch.press(lambda: client.post(path, data=data, headers=PAGE_HEADERS))
        after = every_row(database_of(client))

    main = store_free_page(
        answer,
        status=422,
        heading="Update not saved",
        alert=f"Nothing was saved, because neither Done nor Not yet was chosen. {YOUR_WEEK}",
    )
    assert f"readonly>\n{escape(TYPED)}</textarea>" in main
    ran = [
        line
        for line in seen
        if line.strip().upper() not in ("ROLLBACK", "BEGIN DEFERRED")
        and not line.strip().upper().startswith("PRAGMA BUSY_TIMEOUT=")
    ]
    assert len(ran) == 1
    assert "drafts" in ran[0]
    assert after == before
    timeouts = [
        int(line.split("=")[1])
        for line in seen[: seen.index(ran[0])]
        if line.strip().upper().startswith("PRAGMA BUSY_TIMEOUT=")
    ]
    assert asked.waits == [5.0]
    assert len(asked.called) == 1
    assert len(asked.deadlines) == 1
    ends, set_from, set_by = asked.deadlines[0]
    # The store read its clock for the deadline between ``called`` and ``began``, and for the
    # time left between ``set_from`` and ``set_by``, so these hold wherever a pause falls: the
    # deadline is 5 seconds from the call's start, and the busy timeout the time left to it.
    assert ends - asked.began[0] <= 5.0 + 1e-6
    assert ends - asked.called[0] >= 5.0 - 1e-6
    assert timeouts
    assert (ends - set_by) * 1000 - 1 < timeouts[-1] <= max(0.0, (ends - set_from) * 1000)
    assert timeouts[-1] <= 5000
    assert beside.refused, (took, beside.took)
    # Its time against the wait beside it: within half the 5 second wait pinned above.
    assert -2.5 < took - beside.took < 2.5, (took, beside.took)


def test_a_parents_reply_while_the_file_is_held_is_kept_and_lands_once_after(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """While another connection holds the file, a parent's reply is refused on the page that
    reads no store, with the words, nothing written and no transaction left open, and the same
    press lands once after. It asks once for the file's writer, under the help-requests store's
    busy timeout of 5000 ms, and lasts within 2.5 seconds of another connection's
    ``BEGIN IMMEDIATE`` on the held file under 5 seconds, timed beside the press so a delay to
    the process while both wait reaches both. Each press, and the wait beside the first, has
    ``HANG_LIMIT_SECONDS`` before it counts as hung."""
    with HangWatch(capsys) as watch, household_client("parent", tmp_path) as client:
        sign_in_as(client, "parent")
        state = state_of(client)
        asked = state.help_requests.ask(PLAN_DATE, "which part?").request_id
        path = f"/parent/actions/help/{asked}"
        before = every_row(database_of(client))
        held = HeldByAnother(database_of(client))
        with (
            Statements(state) as seen,
            held,
            WaitBeside(held, 5.0, "BEGIN IMMEDIATE", watch) as beside,
        ):
            answer, took = watch.press(
                lambda: client.post(path, data={"step": "accept", "response": TYPED})
            )
        idle = not state.help_requests._connection.in_transaction
        (timeout,) = state.help_requests._connection.execute("PRAGMA busy_timeout").fetchone()
        after = every_row(database_of(client))
        again, _ = watch.press(
            lambda: client.post(path, data={"step": "accept", "response": TYPED})
        )
        moved = state.help_requests.get(asked)

    main = store_free_page(answer, status=500, heading="Family review", alert=HELP_STEP_FAILED)
    assert f"readonly>{escape(TYPED)}</textarea>" in main
    ran = [line for line in seen if line.strip().upper() != "ROLLBACK"]
    assert ran == ["BEGIN IMMEDIATE"]
    assert idle
    assert after == before
    assert again.status_code == 303
    assert moved is not None
    assert (moved.state, moved.response) == ("accepted", TYPED)
    assert [update.body for update in moved.parent_updates] == [TYPED]
    assert timeout == 5000
    assert beside.refused, (took, beside.took)
    # Its time against the wait beside it: within half the 5 second wait pinned above.
    assert -2.5 < took - beside.took < 2.5, (took, beside.took)


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


# ------------------------------------------------------- the event loop while she presses

TURNED_IN_UNDO = f"/student/actions/assignments/{ESSAY_ID}/undo-turned-in"
EARLIER_WORK = f"/student/actions/assignments/{ESSAY_ID}/earlier-work"
TOO_MUCH = "/student/actions/too-much"
ASK_FOR_HELP = "/student/actions/ask-for-help"
SIGNALS = "/student/workload-signals"
STATIC = "/static/blossom.css"
NOTE_WORDS = {"text": "Geometry 4-8", "course": "", "due_date": ""}
"""What she types on a new note: words alone, with no class and no day."""
A_NOTE_ID = "6f1c2b9e-4d3a-4f8e-9b7a-2c5d8e1f0a43"
"""A note's id as the new note form gives one, for a press made before any page is read."""


@dataclass(frozen=True)
class Sent:
    """One request as a test sends it: its method, its address, and a form or a JSON body."""

    method: str
    path: str
    data: dict[str, str] | None = None
    json: dict[str, str] | None = None


def answer_to(client: TestClient, request: Sent) -> Any:  # noqa: ANN401
    """What ``client`` is answered for ``request``, sent as a page or a script sends it."""
    headers = PAGE_HEADERS if request.json is None else {}
    return client.request(
        request.method, request.path, data=request.data, json=request.json, headers=headers
    )


def a_hand_in(_: TestClient) -> Sent:
    return Sent("POST", HAND_IN, {**HAND_IN_PRESS})


HAND_IN_PRESS = {"state": TURNED_IN, "next_action": "", "note": "", "expected_hand_in_id": ""}
REPORT_PRESS = {
    "status": "done",
    "note": "",
    "expected_report_id": "",
    "week": FIXTURE_WEEK,
    "in_place": "",
}


def a_hand_in_undo(client: TestClient) -> Sent:
    assert client.post(HAND_IN, data=HAND_IN_PRESS).status_code == 303
    made = state_of(client).project_state.hand_in_chains([ESSAY_ID])[ESSAY_ID][-1].event_id
    blank = {"return_to": "", "week": "", "plan_id": ""}
    return Sent("POST", UNDO_HAND_IN, {"hand_in_id": made, **blank})


def a_card_turn_in(client: TestClient) -> Sent:
    page = client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text
    action, fields = pressed_form(card_for(page, ESSAY_ID), "hand_in_head")
    return Sent("POST", action, fields)


def a_card_undo(client: TestClient) -> Sent:
    pressed = answer_to(client, a_card_turn_in(client))
    assert pressed.status_code == 303, pressed.text[:300]
    page = client.get(pressed.headers["location"]).text
    action, fields = pressed_form(card_for(page, ESSAY_ID), "undo_hand_in")
    return Sent("POST", action, fields)


def a_report(_: TestClient) -> Sent:
    return Sent("POST", REPORT, {**REPORT_PRESS})


def a_report_undo(client: TestClient) -> Sent:
    reported(state_of(client).project_state, "done")
    return Sent("POST", UNDO, form_fields(card_for(page_of(client), ESSAY_ID), UNDO))


def a_signal(client: TestClient) -> Sent:
    return Sent("POST", TOO_MUCH, fresh_signal_fields(client))


def a_signal_taken_back(client: TestClient) -> Sent:
    signal = state_of(client).workload_signals.record(PLAN_DATE, None)
    return Sent("POST", f"/student/actions/take-back/{signal.signal_id}")


def a_signal_with_other_words(client: TestClient) -> Sent:
    """The JSON press sent again with other words than the signal it recorded: refused, 409,
    once the store has read what that id kept."""
    fields = fresh_signal_fields(client)
    assert client.post(SIGNALS, json={**fields, "detail": "the first words"}).status_code == 201
    return Sent("POST", SIGNALS, json={**fields, "detail": "other words"})


def a_signal_sent(client: TestClient) -> Sent:
    return Sent("POST", SIGNALS, json=fresh_signal_fields(client))


def a_signal_withdrawn(client: TestClient) -> Sent:
    signal = state_of(client).workload_signals.record(PLAN_DATE, None)
    return Sent("DELETE", f"{SIGNALS}/{signal.signal_id}")


def a_request_for_help(client: TestClient) -> Sent:
    fields = form_fields(client.get(HER_PAGE).text, ASK_FOR_HELP)
    return Sent("POST", ASK_FOR_HELP, {**fields, "note": "the outline"})


EARLIER = due("earlier-aug-18", "Fractions practice", date(2026, 8, 18))
"""Homework due the day before the fixture's day, which her week lists as earlier work."""


def an_earlier_choice(client: TestClient) -> Sent:
    state_of(client).project_state.put_on_record([EARLIER], {})
    action = student_routes.earlier_action(EARLIER.assignment_id)
    return Sent("POST", action, form_fields(client.get(HER_PAGE).text, action))


UNKNOWN_SIGNAL = "0" * 32
ON_THE_LOOP_CASES = [
    pytest.param(lambda _: Sent("POST", HAND_IN, {}), 422, id="hand-in refused"),
    pytest.param(a_hand_in, 303, id="hand-in"),
    pytest.param(lambda _: Sent("POST", UNDO_HAND_IN, {}), 422, id="hand-in undo refused"),
    pytest.param(a_hand_in_undo, 303, id="hand-in undo"),
    pytest.param(lambda _: Sent("POST", TURNED_IN_PRESS, {}), 422, id="card turn-in refused"),
    pytest.param(a_card_turn_in, 303, id="card turn-in"),
    pytest.param(lambda _: Sent("POST", TURNED_IN_UNDO, {}), 422, id="card undo refused"),
    pytest.param(a_card_undo, 303, id="card undo"),
    pytest.param(lambda _: Sent("POST", REPORT, {}), 422, id="report refused"),
    pytest.param(a_report, 303, id="report"),
    pytest.param(lambda _: Sent("POST", UNDO, {}), 422, id="report undo refused"),
    pytest.param(a_report_undo, 303, id="report undo"),
    pytest.param(lambda _: Sent("POST", EARLIER_WORK, {}), 422, id="earlier work refused"),
    pytest.param(an_earlier_choice, 303, id="earlier work"),
    pytest.param(lambda _: Sent("POST", TOO_MUCH, {}), 422, id="too much refused"),
    pytest.param(a_signal, 303, id="too much"),
    pytest.param(
        lambda _: Sent("POST", f"/student/actions/take-back/{UNKNOWN_SIGNAL}"),
        303,
        id="take back, no such signal",
    ),
    pytest.param(a_signal_taken_back, 303, id="take back"),
    pytest.param(a_signal_with_other_words, 409, id="signal refused"),
    pytest.param(a_signal_sent, 201, id="signal"),
    pytest.param(
        lambda _: Sent("DELETE", f"{SIGNALS}/{UNKNOWN_SIGNAL}"), 404, id="withdraw, no such signal"
    ),
    pytest.param(a_signal_withdrawn, 204, id="withdraw"),
    pytest.param(lambda _: Sent("POST", ASK_FOR_HELP, {}), 422, id="ask for help refused"),
    pytest.param(a_request_for_help, 303, id="ask for help"),
]
"""Each of her presses on the hand-in and student routes, refused and made."""


@pytest.mark.parametrize(("made", "code"), ON_THE_LOOP_CASES)
def test_her_press_runs_no_statement_on_the_event_loop(
    made: Callable[[TestClient], Sent], code: int
) -> None:
    """Every store read and write a press makes, and the page it answers with, runs on a
    worker thread: a wait on the file holds that thread, never the loop. Each press reads a
    store, so the trace sees statements on a worker, which shows it saw the route."""
    with browser() as client:
        request = made(client)
        with OnTheLoop(state_of(client)) as seen:
            answer = answer_to(client, request)

    assert answer.status_code == code, answer.text[:300]
    assert seen.loop == []
    assert seen.workers != []


def held_in_place(state: Any, where: str) -> Parked:  # noqa: ANN401
    """Her press held inside its store call: at the hand-in's or the update's write, or at the
    first statement of a page that only reads."""
    connection = state.project_state._connection
    if where == "page":
        return Parked(connection, lambda _: True)
    return Parked(connection, first_write_to(where))


async def a_static_file_beside(app: Any, press: Sent, where: str) -> tuple[list[str], Parked, int]:  # noqa: ANN401
    """Her press held in its store call, and a request for a static file sent meanwhile, on
    one event loop: the order they were answered in, the hold, and the press's status. The
    test sets the release only once the static file is answered."""
    order: list[str] = []
    async with app.router.lifespan_context(app):
        state = getattr(app.state, STATE_ATTRIBUTE)
        transport = httpx.ASGITransport(app=app)
        headers = {**SAME_ORIGIN, **PAGE_HEADERS}
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN, headers=headers) as http:
            with held_in_place(state, where) as parked:

                async def pressed() -> int:
                    answer = await http.request(press.method, press.path, data=press.data)
                    order.append("press answered")
                    return answer.status_code

                pressing = asyncio.create_task(pressed())
                await asyncio.to_thread(parked.entered.wait, HANG_LIMIT_SECONDS)
                static = await asyncio.wait_for(http.get(STATIC), HANG_LIMIT_SECONDS)
                order.append(f"static file answered {static.status_code}")
                parked.release.set()
                code = await asyncio.wait_for(pressing, HANG_LIMIT_SECONDS)
    return order, parked, code


@pytest.mark.parametrize(
    ("press", "where", "code"),
    [
        pytest.param(Sent("POST", HAND_IN, HAND_IN_PRESS), "hand_in_events", 303, id="hand-in"),
        pytest.param(Sent("POST", UNDO_HAND_IN, {}), "page", 422, id="hand-in undo refused"),
        pytest.param(
            Sent("POST", NOTE_ACTIONS, {"capture_id": A_NOTE_ID, **NOTE_WORDS}),
            "homework_captures",
            303,
            id="new note",
        ),
        pytest.param(
            Sent("POST", note_action(A_NOTE_ID, "edit"), {}), "page", 422, id="note edit refused"
        ),
        pytest.param(
            Sent("POST", "/parent/inbox/keep", {"text": MISSING_EMAIL}),
            "status_reports",
            303,
            id="paste save",
        ),
        pytest.param(Sent("POST", "/parent/inbox/read", {}), "page", 422, id="paste read refused"),
    ],
)
def test_a_static_file_is_answered_while_her_press_is_held_in_its_store_call(
    press: Sent, where: str, code: int
) -> None:
    """While her press waits inside its store call, a request that needs neither the file nor
    the store's lock is answered first, and the press then ends as it would have. A static
    file: never a page that reads the record, which waits for the same lock."""
    with browser() as client:
        app = client.app
    order, parked, answered = asyncio.run(a_static_file_beside(app, press, where))

    assert parked.on_the_loop is False
    assert not parked.hung
    assert order == ["static file answered 200", "press answered"]
    assert answered == code


@dataclass
class Canceled:
    """What a press canceled while its store call was held left behind."""

    calls: list[str]
    """In order: each time a press took the decision lock, and each store call's entry and
    return."""
    parked: Parked
    reached: bool
    """Whether the cancel found the request, or the handler's task, still running."""
    waits_while_held: int
    ended: str
    """How the canceled request ended: canceled, raised, or answered with a status and words."""
    waiting: bool | None
    """Whether the press made again had asked for the decision lock before the cancel."""
    second: int | None


@dataclass(frozen=True)
class Held:
    """A press made with the decision lock held, and where its write is held: the store, its
    method, and the table the write goes to."""

    made: Callable[[TestClient], Sent]
    store: str
    method: str
    table: str
    again: int
    """The status the same press made again is answered with, as a repeat: a save already
    saved, or an Undo already undone."""
    remade: bool = False
    """Whether the form is signed with the key drawn at each start, and so is made again from
    the page as this start shows it."""
    at_the_call: bool = False
    """Whether the write is held as its method is called, before the store's own lock, rather
    than at its first write: for a press that reads that store before it asks for the decision
    lock, so the same press made again can reach the lock while the first is held."""


async def a_press_canceled(
    app: Any,  # noqa: ANN401
    press: Sent,
    held: Held,
    kind: str,
    *,
    second: bool,
    waits: list[int],
) -> Canceled:
    """A press held on a worker, at its write or as its write is called, and canceled, as
    uvicorn cancels a request through the household gate's task group or as a plain cancel
    of the handler's own task; with ``second``, the same press made again meanwhile, waiting
    for the lock."""
    calls: list[str] = []
    method = held.method
    async with app.router.lifespan_context(app):
        state = getattr(app.state, STATE_ATTRIBUTE)
        store = getattr(state, held.store)
        made = getattr(store, method)

        def noted(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            if held.at_the_call:
                parked.hold("")
            calls.append("store call entered")
            try:
                return made(*args, **kwargs)
            finally:
                calls.append("store call returned")

        lock = state.decision_lock
        acquire = lock.acquire
        holders: list[asyncio.Task[Any] | None] = []
        asked_again = asyncio.Event()

        async def taken() -> bool:
            holders.append(asyncio.current_task())
            if len(holders) > 1:
                asked_again.set()
            took = bool(await acquire())
            # Before any store lock: a section entered early shows here even when its first
            # store call then waits for a lock the held write keeps.
            calls.append("lock taken")
            return took

        setattr(store, method, noted)
        setattr(lock, "acquire", taken)  # noqa: B010
        transport = httpx.ASGITransport(app=app)
        headers = {**SAME_ORIGIN, **PAGE_HEADERS}
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url=ORIGIN, headers=headers
            ) as http:
                if held.remade:
                    shown = await asyncio.wait_for(http.get(HER_PAGE), HANG_LIMIT_SECONDS)
                    press = Sent(press.method, press.path, form_fields(shown.text, press.path))
                # Held at the call, the hold is the call's own, with no statement: a trace
                # never gives an empty one.
                where = (lambda run: not run) if held.at_the_call else first_write_to(held.table)
                with Parked(store._connection, where, loop_parks=False) as parked:
                    first = asyncio.create_task(sent_on(http, press))
                    await asyncio.to_thread(parked.entered.wait, HANG_LIMIT_SECONDS)
                    again = asyncio.create_task(sent_on(http, press)) if second else None
                    waiting = None
                    if again is not None:
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(asked_again.wait(), HANG_LIMIT_SECONDS)
                        waiting = asked_again.is_set()
                    await asyncio.sleep(0)
                    if kind == "through the gate":
                        reached = first.cancel()
                    else:
                        reached = bool(holders) and holders[0] is not None and holders[0].cancel()
                    before = waits[0]
                    for _ in range(LOOP_CYCLES):
                        await asyncio.sleep(0)
                    waits_while_held = waits[0] - before
                    parked.release.set()
                    done, _ = await asyncio.wait({first}, timeout=HANG_LIMIT_SECONDS)
                    ended = ended_as(first) if first in done else "still running: hung"
                    answered = None
                    if again is not None:
                        answered = (await asyncio.wait_for(again, HANG_LIMIT_SECONDS)).status_code
        finally:
            delattr(store, method)
            delattr(lock, "acquire")
    return Canceled(calls, parked, reached, waits_while_held, ended, waiting, answered)


async def sent_on(http: httpx.AsyncClient, request: Sent) -> httpx.Response:
    """``request`` sent on ``http`` as a page or a script sends it."""
    if request.json is not None:
        return await http.request(
            request.method, request.path, json=request.json, headers={"Accept": "*/*"}
        )
    return await http.request(request.method, request.path, data=request.data)


HAND_IN_HELD = Held(a_hand_in, "project_state", "record_hand_in", "hand_in_events", 303)
PRESSES_HELD = [
    pytest.param(HAND_IN_HELD, id="hand-in"),
    pytest.param(
        Held(a_hand_in_undo, "project_state", "undo_hand_in", "hand_in_events", 409),
        id="hand-in undo",
    ),
    pytest.param(
        Held(a_card_turn_in, "project_state", "record_hand_in", "hand_in_events", 303),
        id="card turn-in",
    ),
    pytest.param(
        Held(a_card_undo, "project_state", "undo_hand_in", "hand_in_events", 409), id="card undo"
    ),
    pytest.param(
        Held(a_report, "project_state", "report_status", "student_reports", 303), id="report"
    ),
    pytest.param(
        Held(a_report_undo, "project_state", "undo_report", "student_reports", 409),
        id="report undo",
    ),
    pytest.param(
        Held(an_earlier_choice, "project_state", "choose_catch_up", "catch_up_choices", 303, True),
        id="earlier work",
    ),
    pytest.param(
        Held(a_signal, "workload_signals", "record_once", "workload_signals", 303), id="too much"
    ),
    pytest.param(
        Held(a_signal_taken_back, "workload_signals", "withdraw", "workload_signals", 303),
        id="take back",
    ),
    pytest.param(
        Held(a_signal_sent, "workload_signals", "record_once", "workload_signals", 200),
        id="signal",
    ),
    pytest.param(
        Held(a_signal_withdrawn, "workload_signals", "withdraw", "workload_signals", 404),
        id="withdraw",
    ),
]


def a_note_saved(client: TestClient) -> str:
    """A note of hers saved through the new note form; its id."""
    press = a_new_note(client)
    pressed = answer_to(client, press)
    assert pressed.status_code == 303, pressed.text[:300]
    assert press.data is not None
    return press.data["capture_id"]


def a_new_note(client: TestClient) -> Sent:
    fields = form_fields(client.get(NEW_NOTE_PAGE, headers=PAGE_HEADERS).text, NOTE_ACTIONS)
    return Sent("POST", NOTE_ACTIONS, {**fields, **NOTE_WORDS})


def a_note_with_other_words(client: TestClient) -> Sent:
    """The new note form sent again with other words than the note it saved: refused, 409,
    once the store has read what that id holds."""
    press = a_new_note(client)
    assert answer_to(client, press).status_code == 303
    assert press.data is not None
    return Sent("POST", NOTE_ACTIONS, {**press.data, "text": "Other words"})


def a_note_edit(client: TestClient) -> Sent:
    name = a_note_saved(client)
    return Sent("POST", note_action(name, "edit"), {**NOTE_WORDS, "revision": "1", "text": "4-9"})


def a_note_archived(client: TestClient) -> Sent:
    return Sent("POST", note_action(a_note_saved(client), "archive"), {"revision": "1"})


def a_note_restored(client: TestClient) -> Sent:
    archived = a_note_archived(client)
    assert answer_to(client, archived).status_code == 303
    restore = archived.path.removesuffix("archive") + "restore"
    return Sent("POST", restore, {"revision": "2"})


def a_note_deleted(client: TestClient) -> Sent:
    return Sent("POST", note_action(a_note_saved(client), "delete"), {"revision": "1"})


def a_note_asked_about(client: TestClient) -> Sent:
    name = a_note_saved(client)
    action = note_action(name, "ask-for-help")
    fields = form_fields(client.get(note_help_href(name), headers=PAGE_HEADERS).text, action)
    return Sent("POST", action, {**fields, "note": "the outline"})


def a_note_linked(client: TestClient, *, family: bool = False) -> Sent:
    """A note joined to the essay as the search for it shows it, in her tree or the family's."""
    name = a_note_saved(client)
    action = note_link_action(name, family=family)
    page = client.get(note_search_href(name, family=family, q="essay"), headers=PAGE_HEADERS)
    row = page.text[page.text.index(f'action="{action}"') :]
    return Sent("POST", action, {**form_fields(row, action), "q": "essay", "page": "1"})


def a_note_unlinked(client: TestClient, *, family: bool = False) -> Sent:
    name = a_note_saved(client)
    homework = link_note(state_of(client).project_state, name).assignment_id
    form = {"revision": "2", "from": homework}
    return Sent("POST", note_unlink_action(name, family=family), form)


NOTE_DETAILS = {
    "course_choice": "__another__",
    "course_other": "Geometry",
    "title": "Questions 4-8",
    "due_date": "",
    "kind": "HOMEWORK",
    "note": "",
}
"""The details she types for a note: a class of her own and a title."""


def a_note_detailed(client: TestClient, *, family: bool = False, add: bool = False) -> Sent:
    """The details form of a note with its details typed, sent to save them or, with ``add``,
    to add the note to homework, in her tree or the family's."""
    name = a_note_saved(client)
    page = client.get(note_add_href(name, family=family), headers=PAGE_HEADERS).text
    form = {**whole_form(page, note_add_action(name, family=family)), **NOTE_DETAILS}
    action = (note_add_action if add else note_details_action)(name, family=family)
    return Sent("POST", action, form)


def an_add_with_a_class_left_behind(client: TestClient) -> Sent:
    """Her add with the essay's class chosen from the list, then every homework of that class
    given another class: the list doesn't hold it, so the route reads the homework of that class
    and title before it asks for the lock, to tell a change from a stale choice."""
    press = a_note_detailed(client, add=True)
    store = state_of(client).project_state
    assignments = store.all_assignments()
    (course,) = {item.course for item in assignments if item.assignment_id == ESSAY_ID}
    renamed = [item for item in assignments if item.course == course]
    store.upsert_assignments(
        [item.model_copy(update={"course": "Renamed class"}) for item in renamed]
    )
    assert press.data is not None
    return Sent(
        press.method, press.path, {**press.data, "course_choice": course, "course_other": ""}
    )


def refused_on_a_note(step: Callable[[str], str]) -> Callable[[TestClient], Sent]:
    """A press with an empty form on a note of hers, to the address ``step`` names: refused
    before the store is asked, on a page that reads the note."""
    return lambda client: Sent("POST", step(a_note_saved(client)), {})


NOTE_PRESSES = {
    "new note": Held(a_new_note, "project_state", "create_capture", "homework_captures", 303),
    "note edit": Held(a_note_edit, "project_state", "edit_capture", "capture_events", 303),
    "note archive": Held(
        a_note_archived, "project_state", "archive_capture", "capture_events", 303
    ),
    "note restore": Held(
        a_note_restored, "project_state", "restore_capture", "capture_events", 303
    ),
    "note delete": Held(a_note_deleted, "project_state", "delete_capture", "deleted_captures", 303),
    "help about a note": Held(
        a_note_asked_about, "help_requests", "ask_once", "help_requests", 303, at_the_call=True
    ),
    "her link": Held(a_note_linked, "project_state", "link_capture", "capture_events", 303),
    "family link": Held(
        partial(a_note_linked, family=True), "project_state", "link_capture", "capture_events", 303
    ),
    "her unlink": Held(a_note_unlinked, "project_state", "unlink_capture", "capture_events", 303),
    "family unlink": Held(
        partial(a_note_unlinked, family=True),
        "project_state",
        "unlink_capture",
        "capture_events",
        303,
    ),
    "her details": Held(
        a_note_detailed, "project_state", "clarify_capture", "capture_events", 303, at_the_call=True
    ),
    "family details": Held(
        partial(a_note_detailed, family=True),
        "project_state",
        "clarify_capture",
        "capture_events",
        303,
        at_the_call=True,
    ),
    "her add": Held(
        partial(a_note_detailed, add=True),
        "project_state",
        "promote_capture",
        "capture_events",
        303,
        at_the_call=True,
    ),
    "family add": Held(
        partial(a_note_detailed, family=True, add=True),
        "project_state",
        "promote_capture",
        "capture_events",
        303,
        at_the_call=True,
    ),
}
"""Each press on a note made with the decision lock held. The request for help and the two
details presses read the store their write holds before they ask for the lock, so they are
held as the write is called."""
NOTE_PRESSES_HELD = [pytest.param(held, id=named) for named, held in NOTE_PRESSES.items()]


INSTRUCTIONS = f"/parent/actions/school-instructions/{ESSAY_ID}"
INSTRUCTIONS_PAGE = f"/parent/school-instructions/{ESSAY_ID}"
CHECK = f"/parent/actions/checks/{ESSAY_ID}"
KEEP = "/parent/inbox/keep"
FIND = "/parent/inbox/find"
PLAN_KEPT = "draft:plan:kept"
OUTLINE = "Outline three causes before drafting."
CITE = "Cite the map handout."
ASSIGNED_ELSEWHERE = (
    "Homework for Wren\n- 08/24/2026 - Monday\n"
    "08 Algebra - Assigned: Q1 Check 3: (Due:08/27/2026)\n"
)
"""A paste of homework the school names otherwise than the record does: its card offers a
search."""


def an_instruction_chosen(client: TestClient) -> Sent:
    """Both of the essay's instructions chosen from its review as the page shows it: the
    first applies, and the second was read later and kept apart."""
    settle = partial(
        state_of(client).project_state.settle_school_instructions,
        ESSAY_ID,
        authored_by="parent",
        now=datetime(2026, 8, 19, 20, 0, tzinfo=UTC),
        today=PLAN_DATE,
    )
    settle([InstructionSeen(OUTLINE, SourceChannel.LMS, "assigned", date(2026, 8, 17))], None)
    later = InstructionSeen(CITE, SourceChannel.LMS, "due", date(2026, 8, 21))
    settle([later], InstructionChoice(1, (OUTLINE, CITE), frozenset({OUTLINE})))
    page = client.get(INSTRUCTIONS_PAGE, headers=PAGE_HEADERS).text
    both = {"apply-0": "1", "apply-1": "1"}
    return Sent("POST", INSTRUCTIONS, {**whole_form(page, INSTRUCTIONS), **both})


def a_row_never_kept(_: TestClient) -> Sent:
    """A choice of a kept row the essay doesn't keep: refused once the rows kept are read."""
    return Sent("POST", INSTRUCTIONS, {"revision": "0", "row-0": "7", "apply-0": "1"})


def a_paste_saved(_: TestClient) -> Sent:
    return Sent("POST", KEEP, {"text": MISSING_EMAIL})


def a_search_chosen(client: TestClient) -> Sent:
    """Choose, on a card that offers a search, homework the search found: the choice reads
    that homework before the page is made."""
    target = homework_from_a_note(
        state_of(client).project_state,
        course="08 Algebra",
        title="Patterns quiz",
        due_date=date(2026, 8, 27),
    )
    read = client.post("/parent/inbox/read", data={"text": ASSIGNED_ELSEWHERE})
    assert read.status_code == 200, read.text[:300]
    looked = client.post(
        FIND, data={**whole_form(read.text, KEEP), "find": "0", "search-0": "patterns"}
    )
    assert looked.status_code == 200, looked.text[:300]
    assert f'name="choose" value="0:{target}"' in looked.text
    return Sent("POST", FIND, {**whole_form(looked.text, KEEP), "choose": f"0:{target}"})


def a_check_marked(client: TestClient) -> Sent:
    a_discrepancy(client)
    row = row_for(family_page(client), ESSAY_ID)
    fields = {"basis": hidden(row, "basis"), "expected_check_id": "", "note": "Seen."}
    return Sent("POST", action_of(row, "mark"), fields)


def a_check_reopened(client: TestClient) -> Sent:
    assert answer_to(client, a_check_marked(client)).status_code == 303
    row = row_for(family_page(client), ESSAY_ID)
    fields = {"check_id": hidden(row, "check_id"), "basis": hidden(row, "basis")}
    return Sent("POST", action_of(row, "again"), fields)


def a_plan_the_saver_lost(client: TestClient) -> None:
    """Today's plan, waiting, settled without a graph: the saver holds no review step for it."""
    settled_run(
        state_of(client).drafts,
        Draft(
            draft_id="draft:plan:kept",
            body="Plan",
            created_at=datetime(2026, 8, 19, 22, 0, tzinfo=UTC),
        ),
        thread_id="plan:kept",
        plan_date=PLAN_DATE,
    )


def a_plan_it_cannot_resume(client: TestClient) -> Sent:
    """Looks good on a waiting plan whose saved review step the saver doesn't hold: refused,
    409, on the family page."""
    a_plan_the_saver_lost(client)
    return Sent("POST", f"/parent/actions/decide/{PLAN_KEPT}", {"decision": "approve"})


REVIEW_PRESSES = {
    "instructions": Held(
        an_instruction_chosen,
        "project_state",
        "settle_school_instructions",
        "school_instructions",
        303,
    ),
    "paste save": Held(
        a_paste_saved,
        "project_state",
        "comparing_and_writing",
        "status_reports",
        303,
        at_the_call=True,
    ),
    "mark checked": Held(a_check_marked, "project_state", "mark_checked", "family_checks", 303),
    "check again": Held(a_check_reopened, "project_state", "check_again", "family_checks", 409),
}
"""Each press on the family's review pages made with the decision lock held. A paste save
reads the record before it asks for the lock, so it is held as its write is called."""
REVIEW_PRESSES_HELD = [pytest.param(held, id=named) for named, held in REVIEW_PRESSES.items()]
CANCELS = {
    "through the gate": "canceled",
    "of the handler's own task": "raised RuntimeError",
}
"""Each way a handler is canceled, and how the request then ends, with no response passed on
either way: through the household gate's task group, as uvicorn cancels a request at a
shutdown, the request is canceled; a plain cancel of the handler's own task leaves the gate
with no response, and it raises."""


@pytest.mark.parametrize("kind", CANCELS)
@pytest.mark.parametrize("held", PRESSES_HELD + NOTE_PRESSES_HELD + REVIEW_PRESSES_HELD)
def test_a_canceled_press_keeps_the_decision_lock_until_its_store_call_returns(
    held: Held, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A press canceled while its write is held on a worker keeps the decision lock until that
    write has returned: the same press made again meanwhile enters its own store call only
    after, and is answered as a repeat. The wait does not spin while the write is held."""
    with browser() as client:
        app = client.app
        press = held.made(client)
    waits = counted_waits(monkeypatch)
    canceled = asyncio.run(a_press_canceled(app, press, held, kind, second=True, waits=waits))

    assert canceled.parked.on_the_loop is False
    assert not canceled.parked.hung
    assert canceled.waiting
    assert canceled.reached
    assert canceled.ended == CANCELS[kind]
    assert canceled.calls == [
        "lock taken",
        "store call entered",
        "store call returned",
        "lock taken",
        "store call entered",
        "store call returned",
    ]
    assert canceled.waits_while_held <= 2, canceled.waits_while_held
    assert canceled.second == held.again


REPORT_HELD = Held(a_report, "project_state", "report_status", "student_reports", 303)


def head_shown(client: TestClient, held: Held) -> tuple[str, str | None]:
    """The head a reload's form carries for the essay beside the newest row the record holds,
    or ``None`` when it holds none: a hand-in on the details, an update on her week."""
    connection = state_of(client).project_state._connection
    if held is HAND_IN_HELD:
        page = client.get(DETAILS, params={"hand_in": "change"}, headers=PAGE_HEADERS).text
        shown = form_fields(page, HAND_IN)["expected_hand_in_id"]
        newest = "SELECT event_id FROM hand_in_events ORDER BY rowid DESC LIMIT 1"
    else:
        page = client.get(HER_PAGE, params={"week": FIXTURE_WEEK, "change": ESSAY_ID}).text
        shown = hidden(card_for(page, ESSAY_ID), "expected_report_id")
        newest = "SELECT report_id FROM student_reports ORDER BY rowid DESC LIMIT 1"
    row = connection.execute(newest).fetchone()
    return shown, None if row is None else str(row[0])


REPEATED = {"hand-in": "hand_in=same", "report": f"same={ESSAY_ID}"}
"""What the address a repeat of each press is answered with says."""


@pytest.mark.parametrize("kind", CANCELS)
@pytest.mark.parametrize("fails", [False, True], ids=["committed", "refused by the file"])
@pytest.mark.parametrize(
    ("held", "named"), [(HAND_IN_HELD, "hand-in"), (REPORT_HELD, "report")], ids=REPEATED
)
def test_a_canceled_press_is_whole_or_absent_and_said_truly_after(
    held: Held, named: str, kind: str, fails: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A press canceled while its write is held sends no answer. When the write then lands, a
    reload's form carries it as the head and the same press again is already saved; when the
    file refuses it, the record holds nothing of it and pressing again says so again."""
    with browser() as client:
        app = client.app
        press = held.made(client)
        if fails:
            store = state_of(client).project_state
            store._connection.execute(
                f"CREATE TRIGGER refuse_writes BEFORE INSERT ON {held.table} "
                "BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )
            store._connection.commit()
    waits = counted_waits(monkeypatch)
    canceled = asyncio.run(a_press_canceled(app, press, held, kind, second=False, waits=waits))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        shown, newest = head_shown(client, held)
        again = answer_to(client, press)

    assert canceled.parked.on_the_loop is False
    assert canceled.reached
    assert canceled.ended == CANCELS[kind]
    assert canceled.calls == ["lock taken", "store call entered", "store call returned"]
    if fails:
        assert newest is None
        assert again.status_code == 500
    else:
        assert newest is not None
        assert shown == newest
        assert again.status_code == 303
        assert REPEATED[named] in again.headers["location"]


async def a_request_canceled(
    app: Any,  # noqa: ANN401
    press: Sent,
    kind: str,
    waits: list[int],
    readers: list[asyncio.Task[Any] | None],
) -> Canceled:
    """Her request for help, which takes no decision lock, held at its write on a worker and
    canceled as ``a_press_canceled`` cancels a press. The handler's own task is the first in
    ``readers``, the tasks that read a form."""
    calls: list[str] = []
    async with app.router.lifespan_context(app):
        store = getattr(app.state, STATE_ATTRIBUTE).help_requests
        made = store.ask_once

        def noted(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            calls.append("store call entered")
            try:
                return made(*args, **kwargs)
            finally:
                calls.append("store call returned")

        store.ask_once = noted
        transport = httpx.ASGITransport(app=app)
        headers = {**SAME_ORIGIN, **PAGE_HEADERS}
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url=ORIGIN, headers=headers
            ) as http:
                where = first_write_to("help_requests")
                with Parked(store._connection, where, loop_parks=False) as parked:
                    first = asyncio.create_task(sent_on(http, press))
                    first.add_done_callback(lambda _: calls.append("request ended"))
                    await asyncio.to_thread(parked.entered.wait, HANG_LIMIT_SECONDS)
                    if kind == "through the gate":
                        reached = first.cancel()
                    else:
                        reached = bool(readers) and readers[0] is not None and readers[0].cancel()
                    before = waits[0]
                    for _ in range(LOOP_CYCLES):
                        await asyncio.sleep(0)
                    waits_while_held = waits[0] - before
                    parked.release.set()
                    done, _ = await asyncio.wait({first}, timeout=HANG_LIMIT_SECONDS)
                    ended = ended_as(first) if first in done else "still running: hung"
        finally:
            del store.ask_once
    return Canceled(calls, parked, reached, waits_while_held, ended, None, None)


@pytest.mark.parametrize("kind", CANCELS)
def test_a_canceled_request_for_help_ends_only_after_its_write_and_is_kept_once(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Her request for help takes no decision lock, yet a cancel while its write is held on a
    worker ends the request only once that write has returned, with no answer sent. The
    request is then kept once, and the same press again is answered as one already sent."""
    with browser() as client:
        app = client.app
        press = a_request_for_help(client)
    readers: list[asyncio.Task[Any] | None] = []

    async def reading(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        readers.append(asyncio.current_task())
        return await fields_of(*args, **kwargs)

    monkeypatch.setattr(student_routes, "fields_of", reading)
    waits = counted_waits(monkeypatch)
    canceled = asyncio.run(a_request_canceled(app, press, kind, waits, readers))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        again = answer_to(client, press)
        kept = client.get("/student/help-requests").json()

    assert canceled.parked.on_the_loop is False
    assert not canceled.parked.hung
    assert canceled.reached
    assert canceled.ended == CANCELS[kind]
    assert canceled.calls == ["store call entered", "store call returned", "request ended"]
    assert canceled.waits_while_held <= 2, canceled.waits_while_held
    assert again.status_code == 303
    assert press.data is not None
    assert [asked["request_id"] for asked in kept] == [press.data["request_id"]]


NOTE_ON_THE_LOOP_CASES = [
    pytest.param(a_note_with_other_words, 409, id="new note refused"),
    pytest.param(a_new_note, 303, id="new note"),
    pytest.param(refused_on_a_note(lambda name: note_action(name, "edit")), 422, id="edit refused"),
    pytest.param(a_note_edit, 303, id="edit"),
    pytest.param(
        refused_on_a_note(lambda name: note_action(name, "archive")), 422, id="archive refused"
    ),
    pytest.param(a_note_archived, 303, id="archive"),
    pytest.param(
        refused_on_a_note(lambda name: note_action(name, "restore")), 422, id="restore refused"
    ),
    pytest.param(a_note_restored, 303, id="restore"),
    pytest.param(
        refused_on_a_note(lambda name: note_action(name, "delete")), 422, id="delete refused"
    ),
    pytest.param(a_note_deleted, 303, id="delete"),
    pytest.param(
        refused_on_a_note(lambda name: note_action(name, "ask-for-help")),
        422,
        id="help about a note refused",
    ),
    pytest.param(a_note_asked_about, 303, id="help about a note"),
    *[
        param
        for family, tree in [(False, "her"), (True, "family")]
        for param in [
            pytest.param(
                refused_on_a_note(partial(note_link_action, family=family)),
                422,
                id=f"{tree} link refused",
            ),
            pytest.param(partial(a_note_linked, family=family), 303, id=f"{tree} link"),
            pytest.param(
                refused_on_a_note(partial(note_unlink_action, family=family)),
                422,
                id=f"{tree} unlink refused",
            ),
            pytest.param(partial(a_note_unlinked, family=family), 303, id=f"{tree} unlink"),
            pytest.param(
                refused_on_a_note(partial(note_details_action, family=family)),
                422,
                id=f"{tree} details refused",
            ),
            pytest.param(partial(a_note_detailed, family=family), 303, id=f"{tree} details"),
            pytest.param(
                refused_on_a_note(partial(note_add_action, family=family)),
                422,
                id=f"{tree} add refused",
            ),
            pytest.param(partial(a_note_detailed, family=family, add=True), 303, id=f"{tree} add"),
        ]
    ],
    pytest.param(an_add_with_a_class_left_behind, 422, id="her add, class left behind"),
]
"""Each press on a note, refused and made, in her tree and the family's where it has both,
and her add with a class the list doesn't hold, which reads the store before the lock."""


@pytest.mark.parametrize(("made", "code"), NOTE_ON_THE_LOOP_CASES)
def test_a_note_press_runs_no_statement_on_the_event_loop(
    made: Callable[[TestClient], Sent], code: int
) -> None:
    """Every store read and write a press on a note makes, and the page it answers with, runs
    on a worker thread. Each press reads a store, so the trace sees statements on a worker,
    which shows it saw the route."""
    with browser() as client:
        request = made(client)
        with OnTheLoop(state_of(client)) as seen:
            answer = answer_to(client, request)

    assert answer.status_code == code, answer.text[:300]
    assert seen.loop == []
    assert seen.workers != []


NOTE_REPEATS = {
    "new note": "said=same",
    "note edit": "said=same",
    "note archive": "said=same",
    "note restore": "said=same",
    "note delete": "already=",
    "help about a note": "asked_again=",
    "her link": "said=already",
    "family link": "said=already",
    "her unlink": "said=unlinked",
    "family unlink": "said=unlinked",
    "her details": "said=unchanged",
    "family details": "said=unchanged",
    "her add": "said=already",
    "family add": "said=already",
}
"""What the address a repeat of each press on a note is answered with says."""
ALREADY_SAVED = "Already saved. This is the note as it stands now."
"""The note's page for ``said=same`` or ``said=unchanged`` naming the change that stands:
``NOTE_ALREADY_SAVED`` in ``blossom/routes/captures.py``, through ``SAID``."""
ALREADY_ADDED = "Already added to homework. This is the note as it stands now."
"""The note's page for ``said=already``: ``ALREADY_ADDED`` in ``captures.py``."""
UNLINKED_STANDS = (
    "Unlinked. This note is back in Homework notes, and the homework it was joined to is unchanged."
)
"""The note's page for ``said=unlinked``: ``UNLINKED`` in ``captures.py``."""
ALREADY_DELETED = "That note was already deleted."
"""Her notes for ``already=``: ``NOTE_ALREADY_DELETED`` in ``captures.py``, through
``deleted_result``."""
REQUEST_ALREADY_SAVED = "That request is already saved."
"""Her week for ``asked_again=``: ``ALREADY_SENT`` in ``blossom/routes/student.py``, through
``help_result_for``."""
NOTE_STANDING = {
    "new note": (ALREADY_SAVED, True),
    "note edit": (ALREADY_SAVED, True),
    "note archive": (ALREADY_SAVED, True),
    "note restore": (ALREADY_SAVED, True),
    "note delete": (ALREADY_DELETED, True),
    "help about a note": (REQUEST_ALREADY_SAVED, True),
    "her link": (ALREADY_ADDED, False),
    "family link": (ALREADY_ADDED, False),
    "her unlink": (UNLINKED_STANDS, True),
    "family unlink": (UNLINKED_STANDS, True),
    "her details": (ALREADY_SAVED, True),
    "family details": (ALREADY_SAVED, True),
    "her add": (ALREADY_ADDED, False),
    "family add": (ALREADY_ADDED, False),
}
"""The sentence the page a repeat lands on opens its result line with, and whether it is the
whole line. A note in homework adds to it, after a space, whether the homework is in today's
planning window and a link to the assignment."""


def standing_line(sentence: str, alone: bool, location: str) -> str:
    """The opening of the result line ``location`` lands on, as a page writes it when that
    line says ``sentence``: the element the address's fragment names, then the sentence, then
    the line's end when the sentence is the whole line, or the space before what follows."""
    fragment = location.rsplit("#", 1)[1]
    return f'id="{fragment}" tabindex="-1">{escape(sentence)}' + ("</p>" if alone else " ")


def note_of(press: Sent) -> str:
    """The note a press is about: the id its form gives a new note, or the one its address
    names."""
    if press.path == NOTE_ACTIONS:
        assert press.data is not None
        return press.data["capture_id"]
    return press.path.split("/")[-2]


def rows_in(client: TestClient, held: Held) -> int:
    """How many rows the table a press writes to holds."""
    connection = getattr(state_of(client), held.store)._connection
    counted = f"SELECT count(*) FROM {held.table}"  # noqa: S608
    return int(connection.execute(counted).fetchone()[0])


def newest_change(client: TestClient, name: str) -> str | None:
    """The id of the newest change the record holds for a note, or ``None`` with none."""
    row = (
        state_of(client)
        .project_state._connection.execute(
            "SELECT event_id FROM capture_events WHERE capture_id = ? "
            "ORDER BY sequence DESC LIMIT 1",
            (name,),
        )
        .fetchone()
    )
    return None if row is None else str(row[0])


@pytest.mark.parametrize("kind", CANCELS)
@pytest.mark.parametrize("fails", [False, True], ids=["committed", "refused by the file"])
@pytest.mark.parametrize("named", NOTE_PRESSES)
def test_a_canceled_note_press_is_whole_or_absent_and_said_truly_after(
    named: str, kind: str, fails: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A press on a note canceled while its write is held sends no answer. When the write
    then lands, the record holds one more row of it, and the same press again is answered as
    a repeat whose address names the deleted note, the request kept, or the newest change the
    record holds, and whose page says that save stands; when the file refuses it, the record
    holds nothing of it and the same press again is refused again, 500."""
    held = NOTE_PRESSES[named]
    with browser() as client:
        app = client.app
        press = held.made(client)
        before = rows_in(client, held)
        if fails:
            connection = getattr(state_of(client), held.store)._connection
            connection.execute(
                f"CREATE TRIGGER refuse_writes BEFORE INSERT ON {held.table} "
                "BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )
            connection.commit()
    waits = counted_waits(monkeypatch)
    canceled = asyncio.run(a_press_canceled(app, press, held, kind, second=False, waits=waits))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        after = rows_in(client, held)
        again = answer_to(client, press)
        newest = newest_change(client, note_of(press))
        opened = None if fails else client.get(again.headers["location"], headers=PAGE_HEADERS)

    assert canceled.parked.on_the_loop is False
    assert canceled.reached
    assert canceled.ended == CANCELS[kind]
    assert canceled.calls == ["lock taken", "store call entered", "store call returned"]
    if fails:
        assert after == before
        assert again.status_code == 500
    else:
        assert after == before + 1
        assert again.status_code == held.again
        location = again.headers["location"]
        assert NOTE_REPEATS[named] in location
        if named == "note delete":
            assert f"already={note_of(press)}" in location
        elif named == "help about a note":
            assert press.data is not None
            assert f"asked_again={press.data['request_id']}" in location
        else:
            assert f"event={newest}" in location
        assert opened is not None
        assert opened.status_code == 200
        assert standing_line(*NOTE_STANDING[named], location) in opened.text


REVIEW_ON_THE_LOOP_CASES = [
    pytest.param(lambda _: Sent("POST", INSTRUCTIONS, {}), 422, id="instructions refused"),
    pytest.param(a_row_never_kept, 422, id="instructions, a row never kept"),
    pytest.param(an_instruction_chosen, 303, id="instructions"),
    pytest.param(lambda _: Sent("POST", "/parent/inbox/read", {}), 422, id="paste read refused"),
    pytest.param(
        lambda _: Sent("POST", "/parent/inbox/read", {"text": MISSING_EMAIL}), 200, id="paste read"
    ),
    pytest.param(lambda _: Sent("POST", "/parent/inbox/enter", {}), 422, id="entry refused"),
    pytest.param(
        lambda _: Sent(
            "POST",
            "/parent/inbox/enter",
            {"course": "08 Algebra", "title": "Patterns quiz", "due_date": "2026-08-27"},
        ),
        200,
        id="entry",
    ),
    pytest.param(lambda _: Sent("POST", "/parent/inbox/edit", {}), 200, id="paste edit"),
    pytest.param(lambda _: Sent("POST", FIND, {}), 422, id="review press refused"),
    pytest.param(a_search_chosen, 200, id="review search chosen"),
    pytest.param(lambda _: Sent("POST", KEEP, {}), 422, id="paste save refused"),
    pytest.param(a_paste_saved, 303, id="paste save"),
    pytest.param(lambda _: Sent("POST", f"{CHECK}/mark", {}), 422, id="mark checked refused"),
    pytest.param(a_check_marked, 303, id="mark checked"),
    pytest.param(lambda _: Sent("POST", f"{CHECK}/again", {}), 422, id="check again refused"),
    pytest.param(a_check_reopened, 303, id="check again"),
    pytest.param(
        lambda _: Sent("POST", "/parent/actions/decide/draft:none", {"decision": "sideways"}),
        422,
        id="decision refused",
    ),
    pytest.param(a_plan_it_cannot_resume, 409, id="decision on a plan it can't resume"),
]
"""Each press on the school's instructions, the paste review and the family's checks and
decisions, refused and made, and the reads some make before the decision lock: the kept rows a
choice names, and the homework a search's choice names."""


@pytest.mark.parametrize(("made", "code"), REVIEW_ON_THE_LOOP_CASES)
def test_a_family_review_press_runs_no_statement_on_the_event_loop(
    made: Callable[[TestClient], Sent], code: int
) -> None:
    """Every store read and write a press on the family's review pages makes, and the page it
    answers with, runs on a worker thread. Each press reads a store, so the trace sees
    statements on a worker, which shows it saw the route."""
    with browser() as client:
        request = made(client)
        with OnTheLoop(state_of(client)) as seen:
            answer = answer_to(client, request)

    assert answer.status_code == code, answer.text[:300]
    assert seen.loop == []
    assert seen.workers != []


@pytest.mark.parametrize("page", [True, False], ids=["from the family page", "as JSON"])
def test_a_decision_at_the_gate_runs_no_statement_on_the_event_loop(page: bool) -> None:
    """Looks good on today's plan waiting at the gate, from the family page or as JSON: the
    record's reads and the graph's write of the decision run on worker threads."""
    with browser(key=True) as client:
        draft_id = planned(client).draft_id
        with OnTheLoop(state_of(client)) as seen:
            if page:
                answer = client.post(
                    f"/parent/actions/decide/{draft_id}",
                    data={"decision": "approve"},
                    headers=PAGE_HEADERS,
                )
            else:
                answer = client.post(f"/parent/approvals/{draft_id}", json={"approved": True})

    assert answer.status_code == (303 if page else 200), answer.text[:300]
    assert seen.loop == []
    assert seen.workers != []


def rows_of(client: TestClient, held: Held) -> list[tuple[object, ...]]:
    """Every row of the table a press writes to, in the order they were written."""
    connection = getattr(state_of(client), held.store)._connection
    every = f"SELECT * FROM {held.table} ORDER BY rowid"  # noqa: S608
    return list(connection.execute(every).fetchall())


INSTRUCTIONS_STOOD = "Nothing changed: the school's instructions already stood as chosen."
"""``ALREADY_STOOD``, ``blossom/routes/school_instructions.py``, on the review's result line."""
NOTHING_NEW = "Nothing to save: everything read was saved already (1 unchanged)."
"""What the family page says of a paste saved already, ``parent_review.html``."""
CHECKED_ALREADY = (
    "Already marked checked, from this device or another, so nothing was written. The note "
    "on record is the one shown."
)
"""``CHECK_ALREADY``, ``blossom/routes/parent.py``."""
REOPENED_ELSEWHERE = (
    "This row was marked checked or reopened from another device since this page was made. "
    "Nothing was written; the row shows what stands now."
)
"""``CHECK_MOVED_ON``, ``blossom/routes/parent.py``: a check reopened already is not
the check the form names."""
REVIEW_STANDING = {
    "instructions": f'id="result" tabindex="-1">{escape(INSTRUCTIONS_STOOD)} ',
    "paste save": str(escape(NOTHING_NEW)),
    "mark checked": str(escape(CHECKED_ALREADY)),
    "check again": str(escape(REOPENED_ELSEWHERE)),
}
"""What the page a repeat of each press lands on says: the instructions' result line as it
opens, and the family page's sentence for the rest."""


@pytest.mark.parametrize("kind", CANCELS)
@pytest.mark.parametrize("fails", [False, True], ids=["committed", "refused by the file"])
@pytest.mark.parametrize("named", REVIEW_PRESSES)
def test_a_canceled_review_press_is_whole_or_absent_and_said_truly_after(
    named: str, kind: str, fails: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A press on the family's review pages canceled while its write is held sends no answer.
    When the write then lands, the table holds it, and the same press again is answered as a
    repeat whose page says what stands; when the file refuses it, the table is as it was and
    the same press again is refused again, 500."""
    held = REVIEW_PRESSES[named]
    with browser() as client:
        app = client.app
        press = held.made(client)
        before = rows_of(client, held)
        if fails:
            connection = getattr(state_of(client), held.store)._connection
            for change in ("INSERT", "UPDATE"):
                connection.execute(
                    f"CREATE TRIGGER refuse_{change.lower()} BEFORE {change} ON {held.table} "
                    "BEGIN SELECT RAISE(ABORT, 'refused'); END"
                )
            connection.commit()
    waits = counted_waits(monkeypatch)
    canceled = asyncio.run(a_press_canceled(app, press, held, kind, second=False, waits=waits))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        after = rows_of(client, held)
        again = answer_to(client, press)
        opened = again
        if again.is_redirect:
            opened = client.get(again.headers["location"], headers=PAGE_HEADERS)

    assert canceled.parked.on_the_loop is False
    assert canceled.reached
    assert canceled.ended == CANCELS[kind]
    assert canceled.calls == ["lock taken", "store call entered", "store call returned"]
    if fails:
        assert after == before
        assert again.status_code == 500
    else:
        assert after != before
        assert again.status_code == held.again
        assert REVIEW_STANDING[named] in opened.text


def decision_form_of(page: str, draft_id: str) -> bool:
    """Whether a page offers the two buttons for this plan."""
    return f'action="/parent/actions/decide/{draft_id}"' in page


@pytest.mark.parametrize(
    "press",
    [
        pytest.param(Sent("POST", f"{CHECK}/mark", {}), id="mark checked refused"),
        pytest.param(Sent("POST", "/parent/inbox/read", {}), id="paste read refused"),
    ],
)
def test_a_press_page_marks_a_plan_it_cannot_resume_as_the_family_page_does(press: Sent) -> None:
    """A waiting plan whose saved review step the saver doesn't hold: the family page says it
    can't be approved or refused here, in place of the two buttons, and a press answered with
    the family page, built on a worker, says the same in the same words."""
    with browser() as client:
        a_plan_the_saver_lost(client)
        page = family_page(client)
        pressed = answer_to(client, press)
    said = f'<p class="note">{escape(parent_routes.unresumable(PLAN_DATE, PLAN_DATE))}</p>'

    assert pressed.status_code == 422
    assert said in page
    assert said in pressed.text
    assert not decision_form_of(page, PLAN_KEPT)
    assert not decision_form_of(pressed.text, PLAN_KEPT)


def a_decision_at_the_gate(client: TestClient) -> Sent:
    """Looks good on today's plan, made from her page and waiting at the gate."""
    record = planned(client)
    return Sent("POST", f"/parent/actions/decide/{record.draft_id}", {"decision": "approve"})


DECISION_HELD = Held(
    a_decision_at_the_gate, "drafts", "record_decision", "drafts", 409, at_the_call=True
)
"""The decision's write, held as the graph's last step calls it, on the graph's own worker."""


@pytest.mark.parametrize("kind", CANCELS)
def test_a_canceled_decision_keeps_the_decision_lock_until_the_graph_has_written_it(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decision canceled while the graph's write of it is held keeps the decision lock until
    that write has returned: the same decision made again meanwhile takes the lock only after,
    finds the plan decided, and is refused as a decided plan is, 409; the record holds the one
    decision. The wait does not spin while the write is held."""
    with browser(key=True) as client:
        app = client.app
        press = DECISION_HELD.made(client)
    draft_id = press.path.rsplit("/", 1)[1]
    waits = counted_waits(monkeypatch)
    canceled = asyncio.run(
        a_press_canceled(app, press, DECISION_HELD, kind, second=True, waits=waits)
    )
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        record = state_of(client).drafts.get(draft_id)

    assert canceled.parked.on_the_loop is False
    assert not canceled.parked.hung
    assert canceled.waiting
    assert canceled.reached
    assert canceled.ended == CANCELS[kind]
    assert canceled.calls == [
        "lock taken",
        "store call entered",
        "store call returned",
        "lock taken",
    ]
    assert canceled.waits_while_held <= 2, canceled.waits_while_held
    assert canceled.second == DECISION_HELD.again
    assert record is not None
    assert (record.waiting, record.decision) == (False, "approved")


# ----------------------------------------------- the event loop while the family adds grades

GRADE_REPORT = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")
"""The synthetic grade report the grade pages are tested with."""
GRADE_REVIEW = "/parent/grades/add/review"
GRADE_CHECK = "/parent/grades/add/check"
GRADE_SAVE = "/parent/grades/add/save"
NO_SUCH_RESULT = "result-" + "0" * 32
NO_SUCH_REVIEW = "0" * 32
"""A result's and a screenshot review's id, each of the shape the store gives, naming none."""
IMPORTS = "/parent/imports"


def grades_open(*, key: bool = False) -> TestClient:
    """Her page on the pinned day with screenshot import on, scripted models when ``key`` is
    set, and the grade and screenshot routes open to the test's client as they are to a browser
    on this computer: the rule that decides who may use them is not what these tests are
    about."""
    client = browser(key=key, BLOSSOM_SCREENSHOT_IMPORT="1")
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    overrides[grade_routes.on_this_computer] = lambda: None
    overrides[import_routes.screenshots_here] = lambda: None
    return client


def a_grade_review(_: TestClient) -> Sent:
    return Sent("POST", GRADE_REVIEW, {"report_text": GRADE_REPORT})


def a_grade_form(client: TestClient, *, answered: bool = True) -> dict[str, str]:
    """The review's form for the report as its page sends it, with "Yes, this is her name"
    chosen, or with the name's question left unanswered."""
    page = answer_to(client, a_grade_review(client))
    assert page.status_code == 200, page.text[:300]
    form = whole_form(page.text, GRADE_SAVE)
    return {**form, "identity": "hers"} if answered else form


def a_grade_save(client: TestClient) -> Sent:
    return Sent("POST", GRADE_SAVE, a_grade_form(client))


def a_grade_check(client: TestClient) -> Sent:
    return Sent("POST", GRADE_CHECK, a_grade_form(client))


def unanswered(path: str) -> Callable[[TestClient], Sent]:
    """The review's form sent to ``path`` with the name's question left unanswered: refused
    once the review is read again."""
    return lambda client: Sent("POST", path, a_grade_form(client, answered=False))


def a_term_followed(path: str) -> Callable[[TestClient], Sent]:
    return lambda _: Sent("POST", path, {"view": grade_routes.FOLLOW_CURRENT})


def a_term_made_current(_: TestClient) -> Sent:
    """A current term chosen while none is on record: the record refuses it in its write."""
    return Sent(
        "POST", "/parent/grades/current-term", {"shown": "2026-2027 T1", "term": "2026-2027 T2"}
    )


def a_class_connected(_: TestClient) -> Sent:
    """A homework class name connected to a class no record holds: the record refuses it in its
    write."""
    fields = {"year": "2026-2027", "name": "08 Algebra", "shown": "unanswered"}
    return Sent("POST", "/parent/grades/homework-classes", {**fields, "class": "class-" + "0" * 32})


def a_link_decided(_: TestClient) -> Sent:
    """Link a result no record holds to the essay: its result is read before the decision lock
    and again in the write, which finds none."""
    fields = {"homework": ESSAY_ID, "decision": "link", "revision": "0"}
    return Sent("POST", f"/parent/grades/results/{NO_SUCH_RESULT}/homework", fields)


def a_question_on_no_result(_: TestClient) -> Sent:
    """Her question about a result no record holds: the form's request is read first."""
    fields = {"note": "Why is this one low?", "request_id": new_request_id()}
    return Sent("POST", f"/student/grades/results/{NO_SUCH_RESULT}/ask", fields)


def a_result_asked_about(client: TestClient) -> Sent:
    """Her question about a result of the report saved whole, from its own page: the form's
    request and the result are read before the decision lock."""
    assert answer_to(client, a_grade_save(client)).status_code == 303
    store = state_of(client).project_state
    for (result_id,) in store._connection.execute("SELECT result_id FROM grade_results"):
        address = f"/student/grades/results/{result_id}/ask"
        page = client.get(address, headers=PAGE_HEADERS)
        if page.status_code == 200:
            return Sent("POST", address, {**form_fields(page.text, address), "note": "Why?"})
    msg = "no result of hers to ask about"
    raise AssertionError(msg)


def a_class_connected_on_record(client: TestClient) -> Sent:
    """The first homework class name on its page connected to the class the page offers, once
    the report is saved whole: the names on record are read after the write."""
    assert answer_to(client, a_grade_save(client)).status_code == 303
    address = "/parent/grades/homework-classes"
    values = form_values(client.get(address, headers=PAGE_HEADERS).text, address)
    form: dict[str, str] = {}
    for _, name, value in values:
        if name == "class" and value:
            return Sent("POST", address, {**form, "class": value})
        if name != "class":
            form[name] = value
    msg = "no class to connect"
    raise AssertionError(msg)


SECOND_TERM_REPORT = GRADE_REPORT.replace("**T1**", "**T2**")
"""The same report for the second term of the school year."""


def two_terms(client: TestClient) -> None:
    """The report saved whole for T1 from its page, then the same report for T2 through the
    store under the household's key, as the grade pages' tests seed one: T1 stays current."""
    assert answer_to(client, a_grade_save(client)).status_code == 303
    store = state_of(client).project_state
    key = client.app.state.grade_name_key  # type: ignore[attr-defined]
    draft = read_grade_report(SECOND_TERM_REPORT).draft
    assert draft is not None
    review = store.review_grade_report(draft, capture_key(draft), key=key)
    saved = save_grade(store, draft, key=key, review=review, answers=grade_answers(review))
    assert isinstance(saved, GradeReportSaved), saved


def a_term_chosen(path: str) -> Callable[[TestClient], Sent]:
    """T2 chosen as the term Grades opens on, with T1 current."""

    def made(client: TestClient) -> Sent:
        two_terms(client)
        return Sent("POST", path, {"view": "2026-2027 T2"})

    return made


def a_term_made_current_on_record(client: TestClient) -> Sent:
    """T2 made the current term from the family's Grades while it shows T1, the term chosen
    there, so the page after says which term is current."""
    two_terms(client)
    chose = answer_to(client, Sent("POST", "/parent/grades/view", {"view": "2026-2027 T1"}))
    assert chose.status_code == 303, chose.text[:300]
    fields = {"shown": "2026-2027 T1", "term": "2026-2027 T2"}
    return Sent("POST", "/parent/grades/current-term", fields)


def a_link_kept_apart(client: TestClient) -> Sent:
    """The essay kept apart from a result of the report saved whole, at the revision of the
    result's class and term: no class name needs to be connected for that."""
    assert answer_to(client, a_grade_save(client)).status_code == 303
    connection = state_of(client).project_state._connection
    result_id, class_id, term = connection.execute(
        "SELECT result_id, class_id, term_label FROM grade_results ORDER BY rowid LIMIT 1"
    ).fetchone()
    (revision,) = connection.execute(
        "SELECT revision FROM grade_scope_revisions WHERE class_id = ? AND term_label = ?",
        (class_id, term),
    ).fetchone()
    fields = {"homework": ESSAY_ID, "decision": "apart", "revision": str(revision)}
    return Sent("POST", f"/parent/grades/results/{result_id}/homework", fields)


def on_no_review(step: str = "") -> Callable[[TestClient], Sent]:
    """A press on a screenshot review no record holds, at ``step`` under its address."""
    return lambda _: Sent("POST", f"{IMPORTS}/{NO_SUCH_REVIEW}{step}", {})


GRADE_ON_THE_LOOP_CASES = [
    pytest.param(lambda _: Sent("POST", "/student/grades/view", {}), 422, id="her term refused"),
    pytest.param(a_term_followed("/student/grades/view"), 303, id="her term"),
    pytest.param(lambda _: Sent("POST", "/parent/grades/view", {}), 422, id="family term refused"),
    pytest.param(a_term_followed("/parent/grades/view"), 303, id="family term"),
    pytest.param(
        lambda _: Sent("POST", "/parent/grades/current-term", {}), 422, id="current term refused"
    ),
    pytest.param(a_term_made_current, 422, id="current term, none on record"),
    pytest.param(
        lambda _: Sent("POST", "/parent/grades/homework-classes", {}),
        422,
        id="homework class refused",
    ),
    pytest.param(a_class_connected, 422, id="homework class, no class on record"),
    pytest.param(a_class_connected_on_record, 303, id="homework class"),
    pytest.param(a_grade_review, 200, id="grade review"),
    pytest.param(unanswered(GRADE_CHECK), 422, id="grade check refused"),
    pytest.param(a_grade_check, 200, id="grade check"),
    pytest.param(unanswered(GRADE_SAVE), 422, id="grade save refused"),
    pytest.param(a_grade_save, 303, id="grade save"),
    pytest.param(
        lambda _: Sent("POST", f"/student/grades/results/{NO_SUCH_RESULT}/ask", {}),
        404,
        id="question refused",
    ),
    pytest.param(a_question_on_no_result, 404, id="question on no result"),
    pytest.param(a_result_asked_about, 303, id="question"),
    pytest.param(
        lambda _: Sent("POST", f"/parent/grades/results/{NO_SUCH_RESULT}/homework", {}),
        404,
        id="link refused",
    ),
    pytest.param(a_link_decided, 404, id="link on no result"),
    pytest.param(
        lambda _: Sent("POST", f"/parent/grades/results/{NO_SUCH_RESULT}/homework/search", {}),
        404,
        id="search refused",
    ),
    pytest.param(
        lambda _: Sent(
            "POST",
            f"/parent/grades/results/{NO_SUCH_RESULT}/homework/search",
            {"words": "essay", "page": "1"},
        ),
        404,
        id="search on no result",
    ),
    pytest.param(on_no_review("/images"), 404, id="more screenshots"),
    pytest.param(on_no_review(f"/images/{NO_SUCH_REVIEW}/remove"), 404, id="screenshot removed"),
    pytest.param(
        lambda _: Sent("GET", f"{IMPORTS}/{NO_SUCH_REVIEW}/images/{NO_SUCH_REVIEW}"),
        404,
        id="screenshot shown",
    ),
    pytest.param(lambda _: Sent("GET", f"{IMPORTS}/{NO_SUCH_REVIEW}"), 404, id="screenshot review"),
    pytest.param(on_no_review("/read"), 404, id="screenshots read"),
    pytest.param(on_no_review("/review"), 404, id="screenshot review press"),
    pytest.param(on_no_review("/find"), 404, id="screenshot find"),
    pytest.param(on_no_review("/set-aside"), 404, id="screenshots set aside"),
    pytest.param(on_no_review("/finish"), 404, id="screenshots finished"),
    pytest.param(on_no_review("/discard"), 404, id="screenshots discarded"),
    pytest.param(on_no_review("/not-hers"), 404, id="screenshots not hers"),
]
"""Each press on the grade pages, refused and made, with the reads some make before the
decision lock: her request's form and the result a question or a link names; and each press on
a screenshot review, whose first read is her student ID."""


@pytest.mark.parametrize(("made", "code"), GRADE_ON_THE_LOOP_CASES)
def test_a_grade_or_screenshot_press_runs_no_statement_on_the_event_loop(
    made: Callable[[TestClient], Sent], code: int
) -> None:
    """Every store read and write a press on the grade pages or a screenshot review makes, and
    the page it answers with, runs on a worker thread. Each press reads a store, so the trace
    sees statements on a worker, which shows it saw the route."""
    with grades_open() as client:
        request = made(client)
        with OnTheLoop(state_of(client)) as seen:
            answer = answer_to(client, request)

    assert answer.status_code == code, answer.text[:300]
    assert seen.loop == []
    assert seen.workers != []


@pytest.mark.parametrize(
    ("press", "where", "code"),
    [
        pytest.param(
            Sent("POST", "/parent/grades/view", {"view": grade_routes.FOLLOW_CURRENT}),
            "grade_view_choices",
            303,
            id="family term",
        ),
        pytest.param(
            Sent("POST", f"/parent/grades/results/{NO_SUCH_RESULT}/homework/search", {}),
            "page",
            404,
            id="search refused",
        ),
        pytest.param(
            Sent("GET", f"{IMPORTS}/{NO_SUCH_REVIEW}"), "page", 404, id="screenshot review"
        ),
    ],
)
def test_a_static_file_is_answered_while_a_grade_or_screenshot_press_is_held(
    press: Sent, where: str, code: int
) -> None:
    """While a press on the grade pages or a screenshot review waits inside its store call, a
    static file is answered first, and the press then ends as it would have: a term followed
    held at its write, and a search and a review held at the first statement they read."""
    with grades_open() as client:
        app = client.app
    order, parked, answered = asyncio.run(a_static_file_beside(app, press, where))

    assert parked.on_the_loop is False
    assert not parked.hung
    assert order == ["static file answered 200", "press answered"]
    assert answered == code


GRADE_PRESSES = {
    "her term": Held(
        a_term_followed("/student/grades/view"),
        "project_state",
        "choose_view",
        "grade_view_choices",
        303,
        at_the_call=True,
    ),
    "family term": Held(
        a_term_followed("/parent/grades/view"),
        "project_state",
        "choose_view",
        "grade_view_choices",
        303,
        at_the_call=True,
    ),
    "current term": Held(
        a_term_made_current,
        "project_state",
        "set_current_context",
        "grade_context",
        422,
        at_the_call=True,
    ),
    "homework class": Held(
        a_class_connected,
        "project_state",
        "connect_homework_class",
        "grade_homework_classes",
        422,
        at_the_call=True,
    ),
    "current term on record": Held(
        a_term_made_current_on_record,
        "project_state",
        "set_current_context",
        "grade_context",
        303,
        at_the_call=True,
    ),
    "homework class connected": Held(
        a_class_connected_on_record,
        "project_state",
        "connect_homework_class",
        "grade_homework_classes",
        303,
        at_the_call=True,
    ),
    "question": Held(
        a_result_asked_about, "help_requests", "ask_once", "help_requests", 303, at_the_call=True
    ),
    "grade save": Held(
        a_grade_save, "project_state", "save_grade_report", "grade_reports", 303, at_the_call=True
    ),
    "link": Held(
        a_link_decided,
        "project_state",
        "decide_homework_link",
        "grade_homework_links",
        404,
        at_the_call=True,
    ),
    "link kept apart": Held(
        a_link_kept_apart,
        "project_state",
        "decide_homework_link",
        "grade_homework_links",
        303,
        at_the_call=True,
    ),
}
"""Each press on the grade pages made with the decision lock held, held as its write is called:
a question and a link read the record before they ask for the lock."""
KEY_READ_BEFORE = {"grade save"}
"""The presses that, as the first since the app started, would read the key name forms are made
under with the decision lock held before they ask for it again for their write. The test reads
the key before the press instead, so the press made again is the one seen waiting."""


def key_read_before(monkeypatch: pytest.MonkeyPatch, state: Any) -> None:  # noqa: ANN401
    """The key name forms are made under, read from the household's secret before the press,
    as a first review already made since the app started would hold it."""
    key = name_form_key(secret_beside(state.settings.database_path))

    async def held(*_: object) -> bytes:
        return key

    monkeypatch.setattr(grade_routes, "grade_name_key", held)


@pytest.mark.parametrize("kind", CANCELS)
@pytest.mark.parametrize("named", GRADE_PRESSES)
def test_a_canceled_grade_press_keeps_the_decision_lock_until_its_store_call_returns(
    named: str, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A press on the grade pages canceled while its write is held on a worker keeps the
    decision lock until that write has returned: the same press made again meanwhile enters
    its own store call only after, and is answered as it was the first time or as a repeat.
    The wait does not spin while the write is held."""
    held = GRADE_PRESSES[named]
    with grades_open() as client:
        app = client.app
        press = held.made(client)
        if named in KEY_READ_BEFORE:
            key_read_before(monkeypatch, state_of(client))
    waits = counted_waits(monkeypatch)
    canceled = asyncio.run(a_press_canceled(app, press, held, kind, second=True, waits=waits))

    assert canceled.parked.on_the_loop is False
    assert not canceled.parked.hung
    assert canceled.waiting
    assert canceled.reached
    assert canceled.ended == CANCELS[kind]
    assert canceled.calls == [
        "lock taken",
        "store call entered",
        "store call returned",
        "lock taken",
        "store call entered",
        "store call returned",
    ]
    assert canceled.waits_while_held <= 2, canceled.waits_while_held
    assert canceled.second == held.again


async def a_first_review_canceled(
    app: Any,  # noqa: ANN401
    press: Sent,
    kind: str,
    waits: list[int],
    parked: Parked,
    calls: list[str],
) -> Canceled:
    """The first grade review since the app started, held as it reads the household's secret
    on a worker, and canceled as ``a_press_canceled`` cancels a press, with a second first
    review made meanwhile, waiting for the decision lock."""
    async with app.router.lifespan_context(app):
        lock = getattr(app.state, STATE_ATTRIBUTE).decision_lock
        acquire = lock.acquire
        holders: list[asyncio.Task[Any] | None] = []
        asked_again = asyncio.Event()

        async def taken() -> bool:
            holders.append(asyncio.current_task())
            if len(holders) > 1:
                asked_again.set()
            took = bool(await acquire())
            calls.append("lock taken")
            return took

        setattr(lock, "acquire", taken)  # noqa: B010
        transport = httpx.ASGITransport(app=app)
        headers = {**SAME_ORIGIN, **PAGE_HEADERS}
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url=ORIGIN, headers=headers
            ) as http:
                first = asyncio.create_task(sent_on(http, press))
                await asyncio.to_thread(parked.entered.wait, HANG_LIMIT_SECONDS)
                again = asyncio.create_task(sent_on(http, press))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asked_again.wait(), HANG_LIMIT_SECONDS)
                waiting = asked_again.is_set()
                await asyncio.sleep(0)
                if kind == "through the gate":
                    reached = first.cancel()
                else:
                    reached = bool(holders) and holders[0] is not None and holders[0].cancel()
                before = waits[0]
                for _ in range(LOOP_CYCLES):
                    await asyncio.sleep(0)
                waits_while_held = waits[0] - before
                parked.release.set()
                done, _ = await asyncio.wait({first}, timeout=HANG_LIMIT_SECONDS)
                ended = ended_as(first) if first in done else "still running: hung"
                answered = (await asyncio.wait_for(again, HANG_LIMIT_SECONDS)).status_code
        finally:
            delattr(lock, "acquire")
    return Canceled(calls, parked, reached, waits_while_held, ended, waiting, answered)


READ_AGAIN = {
    "through the gate": [],
    "of the handler's own task": ["secret read entered", "secret read returned"],
}
"""What the second first review reads under the lock after each cancel: through the gate the
first keeps the key it read before the cancel reaches it at its next wait, and the second finds
it; a plain cancel ends the first as its read returns, and the second reads the secret again."""


@pytest.mark.parametrize("kind", CANCELS)
def test_a_canceled_first_review_keeps_the_decision_lock_until_the_secret_is_read(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first grade review, canceled while it reads the household's secret on a worker,
    keeps the decision lock until that read has returned: a second first review made meanwhile
    takes the lock only after, reads the secret again only where the first kept no key, and is
    answered with the review. One secret is made. The wait does not spin while the read is
    held."""
    with grades_open() as client:
        app = client.app
        folder = pathlib.Path(state_of(client).settings.database_path).parent
    calls: list[str] = []
    parked = Parked(loop_parks=False)

    def noted(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        parked.hold("")
        calls.append("secret read entered")
        try:
            return secret_beside(*args, **kwargs)
        finally:
            calls.append("secret read returned")

    monkeypatch.setattr(grade_routes, "secret_beside", noted)
    waits = counted_waits(monkeypatch)
    with parked:
        canceled = asyncio.run(
            a_first_review_canceled(app, a_grade_review(client), kind, waits, parked, calls)
        )

    assert canceled.parked.on_the_loop is False
    assert not canceled.parked.hung
    assert canceled.waiting
    assert canceled.reached
    assert canceled.ended == CANCELS[kind]
    assert canceled.calls == [
        "lock taken",
        "secret read entered",
        "secret read returned",
        "lock taken",
        *READ_AGAIN[kind],
    ]
    assert canceled.waits_while_held <= 2, canceled.waits_while_held
    assert canceled.second == 200
    assert sorted(path.name for path in folder.glob(f"{SECRET_NAME}*")) == [SECRET_NAME]


GRADE_COMMITS = {
    "her term": Held(
        a_term_chosen("/student/grades/view"),
        "project_state",
        "choose_view",
        "grade_view_choices",
        303,
        at_the_call=True,
    ),
    "family term": Held(
        a_term_chosen("/parent/grades/view"),
        "project_state",
        "choose_view",
        "grade_view_choices",
        303,
        at_the_call=True,
    ),
    "current term": GRADE_PRESSES["current term on record"],
    "homework class": GRADE_PRESSES["homework class connected"],
    "grade save": GRADE_PRESSES["grade save"],
    "question": GRADE_PRESSES["question"],
    "link": GRADE_PRESSES["link kept apart"],
}
"""Each locked write of the grade pages, made where it lands, held as its write is called."""
SHOWING_T2 = "Showing T2 · 2026-2027. Your household's current term in Blossom is T1 · 2026-2027."
SHOWING_T1 = "Showing T1 · 2026-2027. Your household's current term in Blossom is T2 · 2026-2027."
"""Grades opening on a term other than the current one, ``blossom/templates/grades.html``."""
CONNECTED = "Connected to Biology."
"""``CONNECTED_TO``, ``blossom/routes/grades.py``, on the name's line."""
SAVED_NINE = "Saved. 9 added."
"""The first line of a save's outcome, ``outcome_lines`` in ``blossom/routes/grades.py``."""
KEPT_APART = f"Kept apart from {ESSAY_TITLE}. Blossom won't match these two by itself."
"""``KEPT_APART_FROM``, ``blossom/routes/grades.py``, on the essay's row."""
GRADE_STANDING = {
    "her term": SHOWING_T2,
    "family term": SHOWING_T2,
    "current term": SHOWING_T1,
    "homework class": str(escape(CONNECTED)),
    "grade save": str(escape(SAVED_NINE)),
    "question": str(escape(REQUEST_ALREADY_SAVED)),
    "link": str(escape(KEPT_APART)),
}
"""What the page a repeat of each press lands on says of the write that stands, as its HTML
carries it: the template's own words as written, a sentence the route passes in escaped."""
ROWS_ADDED = {"current term": 0}
"""The rows each write adds to its table, one unless named: a current term changes its row."""


@pytest.mark.parametrize("kind", CANCELS)
@pytest.mark.parametrize("fails", [False, True], ids=["committed", "refused by the file"])
@pytest.mark.parametrize("named", GRADE_COMMITS)
def test_a_canceled_grade_press_is_whole_or_absent_and_said_truly_after(
    named: str, kind: str, fails: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A press on the grade pages canceled while its write is held sends no answer. When the
    write then lands, its table holds it once, and the same press again lands on a page that
    says what stands; when the file refuses it, the table is as it was and the same press again
    is refused again, 500."""
    held = GRADE_COMMITS[named]
    with grades_open() as client:
        app = client.app
        press = held.made(client)
        before = rows_of(client, held)
        if named in KEY_READ_BEFORE:
            key_read_before(monkeypatch, state_of(client))
        if fails:
            connection = getattr(state_of(client), held.store)._connection
            for change in ("INSERT", "UPDATE"):
                connection.execute(
                    f"CREATE TRIGGER refuse_{change.lower()} BEFORE {change} ON {held.table} "
                    "BEGIN SELECT RAISE(ABORT, 'refused'); END"
                )
            connection.commit()
    waits = counted_waits(monkeypatch)
    canceled = asyncio.run(a_press_canceled(app, press, held, kind, second=False, waits=waits))
    with TestClient(
        app, follow_redirects=False, headers=SAME_ORIGIN, raise_server_exceptions=False
    ) as client:
        after = rows_of(client, held)
        again = answer_to(client, press)
        opened = again
        if again.is_redirect:
            opened = client.get(again.headers["location"], headers=PAGE_HEADERS)

    assert canceled.parked.on_the_loop is False
    assert canceled.reached
    assert canceled.ended == CANCELS[kind]
    assert canceled.calls == ["lock taken", "store call entered", "store call returned"]
    if fails:
        assert after == before
        assert again.status_code == 500
    else:
        assert after != before
        assert len(after) == len(before) + ROWS_ADDED.get(named, 1)
        assert again.status_code == held.again
        assert GRADE_STANDING[named] in opened.text


# ------------------------------------------- the event loop while a screenshot review is used


def a_screenshot() -> bytes:
    """A small synthetic PNG, as a phone's screenshot is uploaded."""
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 40, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


def read_as_homework(*_: object, **__: object) -> ScreenshotAnswer:
    """A fake reader: every screenshot read as her homework list, with no call made."""
    return ScreenshotAnswer(homework_answer(), None, 10, 5)


def uploading(client: TestClient) -> Callable[[], Any]:
    """A first upload of one screenshot, from a form made now."""
    form = {"token": new_creation_token(), "issued": state_of(client).real_clock.now().isoformat()}
    files = [("screenshots", ("shot-0.png", a_screenshot(), "image/png"))]
    return lambda: client.post(IMPORTS, data=form, files=files)


def uploaded(client: TestClient) -> tuple[str, str]:
    """A review made by a first upload: its address, and its page."""
    sent = uploading(client)()
    assert sent.status_code == 303, sent.text[:300]
    address = sent.headers["location"]
    return address, client.get(address, headers=PAGE_HEADERS).text


def fields_of_form(page: str, action: str) -> dict[str, str]:
    """The hidden fields of the first form on the page with this action."""
    found = re.search(rf'<form [^>]*action="{re.escape(action)}".*?</form>', page, re.S)
    assert found is not None, action
    pairs = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', found.group(0))
    return {html.unescape(name): html.unescape(value) for name, value in pairs}


def read_through(client: TestClient) -> tuple[str, str]:
    """A review whose one screenshot is read as her homework list: its address and page."""
    address, page = uploaded(client)
    action = f"{address}/read"
    pressed = client.post(action, data=fields_of_form(page, action), headers=PAGE_HEADERS)
    assert pressed.status_code == 200, pressed.text[:300]
    return address, client.get(address, headers=PAGE_HEADERS).text


def reading(client: TestClient) -> Callable[[], Any]:
    address, page = uploaded(client)
    action = f"{address}/read"
    fields = fields_of_form(page, action)
    return lambda: client.post(action, data=fields, headers=PAGE_HEADERS)


def reviewing(client: TestClient) -> Callable[[], Any]:
    address, _ = read_through(client)
    return lambda: client.get(address, headers=PAGE_HEADERS)


def pressed_on_review(
    press: str, *, ticked: bool = False
) -> Callable[[TestClient], Callable[[], Any]]:
    """A press of the review form as its page shows it, her name answered as hers, and the
    first group ticked when ``ticked``."""

    def made(client: TestClient) -> Callable[[], Any]:
        address, page = read_through(client)
        action = f"{address}/review"
        if ticked:
            kept = client.post(action, data=review_fields(page, action, "keep"))
            assert kept.status_code == 200, kept.text[:300]
            page = client.get(address, headers=PAGE_HEADERS).text
        fields = review_fields(page, action, press)
        if ticked:
            fields["unit"] = html.unescape(re.findall(r'name="unit" value="([^"]+)"', page)[0])
        return lambda: client.post(action, data=fields, headers=PAGE_HEADERS)

    return made


def review_fields(page: str, action: str, press: str) -> dict[str, str]:
    """The review form as its page sends it with ``press``: her name answered as hers, and
    each other question still open given the first answer it offers."""
    fields = {**whole_form(page, action), "press": press}
    for tag in re.findall(r'<input [^>]*type="radio"[^>]*>', page):
        name = html.unescape(re.findall(r'name="([^"]+)"', tag)[0])
        value = html.unescape(re.findall(r'value="([^"]*)"', tag)[0])
        if name.startswith("answer:name:"):
            fields[name] = "hers"
        elif name.startswith("answer:") and not fields.get(name):
            fields[name] = value
    return fields


def saved_again(client: TestClient) -> Callable[[], Any]:
    """The same save sent again: the outcome recorded under its acceptance id is shown."""
    send = pressed_on_review("save", ticked=True)(client)
    first = send()
    assert first.status_code == 200, first.text[:300]
    return send


def pressed_after_discard(client: TestClient) -> Callable[[], Any]:
    """A press of the review form after the review was discarded: the press is looked for
    among the saves recorded before it is refused."""
    address, page = read_through(client)
    action = f"{address}/review"
    fields = review_fields(page, action, "keep")
    discard = f"{address}/discard"
    gone = client.post(discard, data=fields_of_form(page, discard), headers=PAGE_HEADERS)
    assert gone.status_code == 200, gone.text[:300]
    return lambda: client.post(action, data=fields, headers=PAGE_HEADERS)


def small_press(step: str) -> Callable[[TestClient], Callable[[], Any]]:
    """The review's own small form for ``step``, as its page signs it."""

    def made(client: TestClient) -> Callable[[], Any]:
        address, page = read_through(client)
        action = f"{address}/{step}"
        fields = fields_of_form(page, action)
        return lambda: client.post(action, data=fields, headers=PAGE_HEADERS)

    return made


SCREENSHOT_STEPS = [
    pytest.param(uploading, 303, id="first upload"),
    pytest.param(reading, 200, id="read"),
    pytest.param(reviewing, 200, id="review read"),
    pytest.param(pressed_on_review("keep"), 200, id="keep"),
    pytest.param(pressed_on_review("later"), 303, id="later"),
    pytest.param(pressed_on_review("save", ticked=True), 200, id="save"),
    pytest.param(saved_again, 200, id="save again"),
    pytest.param(pressed_after_discard, 200, id="press after discard"),
    pytest.param(small_press("set-aside"), 303, id="set aside"),
    pytest.param(small_press("finish"), 200, id="finish"),
    pytest.param(small_press("discard"), 200, id="discard"),
    pytest.param(small_press("not-hers"), 200, id="not hers"),
]
"""Each step of a screenshot review that reads her student ID or her name's forms: a first
upload, a Read press, the review read, its form's presses and its small forms."""


@pytest.mark.parametrize(("step", "code"), SCREENSHOT_STEPS)
def test_a_screenshot_review_step_runs_no_statement_on_the_event_loop(
    step: Callable[[TestClient], Callable[[], Any]], code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every store read and write a step of a screenshot review makes, her student ID and her
    name's forms included, and the page it answers with, runs on a worker thread."""
    monkeypatch.setattr(screenshot_reading, "read_screenshot", read_as_homework)
    with grades_open(key=True) as client:
        send = step(client)
        with OnTheLoop(state_of(client)) as seen:
            answer = send()

    assert answer.status_code == code, answer.text[:300]
    assert seen.loop == []
    assert seen.workers != []


# ------------------------------------------------------------- the drafts store busy


WEEK_UNREADABLE = "This week cannot be shown right now"
WEEK_NOT_READ = (
    "The record cannot be read right now, so the week, its updates and current date "
    "information are not shown. Try again in a moment. Try again"
)
FAMILY_NOT_READ = (
    "The record cannot be read right now, so reviews, updates and current date information "
    "are not shown. Try again in a moment. Try again"
)
NOT_ON_RECORD = "This assignment is not on record now."
TO_TODAY = {"return_to": "today"}
TO_A_PLAN = {"return_to": "family", "plan_id": "draft:a"}
WAYS_BACK = {
    "today": [("/student/due-this-week#today", "Back to Today")],
    "family": [("/parent?plan=draft%3Aa#plan-draft-a", "Back to family review")],
}


@dataclass(frozen=True)
class BusyPage:
    """A page, or a press's answer, made inside a catch around a drafts read: the read to
    refuse, and the status, heading and explanation of the page it falls back to."""

    read: str
    status: int
    heading: str
    alert: str


BUSY_PAGES = {
    "her week": BusyPage("latest_for", 503, WEEK_UNREADABLE, WEEK_NOT_READ),
    "family review": BusyPage("review_snapshot", 503, "Family review", FAMILY_NOT_READ),
    "a check refused on family review": BusyPage(
        "review_snapshot",
        404,
        "Family review",
        f"No assignment with that id is on record. Nothing was written. {FAMILY}",
    ),
    "an update refused on her week": BusyPage(
        "latest_for",
        422,
        "Update not saved",
        f"Nothing was saved, because neither Done nor Not yet was chosen. {YOUR_WEEK}",
    ),
    "work gone from the record": BusyPage("latest_for", 404, "Assignment", NOT_ON_RECORD),
    "the details": BusyPage(
        "latest_for", 503, "Assignment", f"{THIS_ASSIGNMENT} Try again in a moment. Try again"
    ),
}
ANSWERED_AS_BEFORE = {
    "her week": 200,
    "family review": 200,
    "a check refused on family review": 404,
    "an update refused on her week": 422,
    "work gone from the record": 404,
    "the details": 200,
}


def unheld() -> TestClient:
    """The fixture week with the sign-in off, a failure no page catches answered as a server
    answers it."""
    return quiet_client(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()))


def quiet(client: TestClient) -> TestClient:
    """The application ``client`` reaches, with a failure no page catches answered as a
    server answers it."""
    return TestClient(
        client.app, follow_redirects=False, headers=SAME_ORIGIN, raise_server_exceptions=False
    )


def busy_request(client: TestClient, case: str, back: dict[str, str]) -> Sent:
    """Make what ``case`` needs; the request itself, with ``back`` as the way back where the
    page offers one."""
    if case == "her week":
        return Sent("GET", HER_PAGE)
    if case == "family review":
        return Sent("GET", "/parent")
    if case == "a check refused on family review":
        path, data, _ = family_press(client, "a check of homework not on record", "open")
        return Sent("POST", path, data)
    if case == "an update refused on her week":
        path, data = week_press(client, "nothing chosen")
        return Sent("POST", path, data)
    where = f"/student/assignments/{GONE_ID}" if case == "work gone from the record" else DETAILS
    return Sent("GET", f"{where}?{httpx.QueryParams(back)}")


def busy_answer(answer: Any, page: BusyPage, back: dict[str, str]) -> None:  # noqa: ANN401
    """``answer`` is the page ``page`` falls back to, never the server's own error."""
    assert answer.text != "Internal Server Error"
    if page.alert == NOT_ON_RECORD:
        assert answer.status_code == page.status, answer.text[:300]
        main = main_of(answer.text)
        assert NOT_ON_RECORD in main
    else:
        main = store_free_page(answer, status=page.status, heading=page.heading, alert=page.alert)
    if page.heading == "Assignment":
        assert ways_back_of(main) == WAYS_BACK[back["return_to"]]


def busy_refusals(monkeypatch: pytest.MonkeyPatch, store: Any) -> list[str]:  # noqa: ANN401
    """Each call of ``store`` refused as busy, by kind, as it is refused."""
    refused: list[str] = []
    session = store._session

    @contextlib.contextmanager
    def watched(*args: object, **kwargs: object) -> Iterator[object]:
        try:
            with session(*args, **kwargs) as held:
                yield held
        except (StoreBusy, WriterBusy) as error:
            refused.append(type(error).__name__)
            raise

    monkeypatch.setattr(store, "_session", watched)
    return refused


BUSY_KINDS = [
    *[pytest.param(case, StoreBusy, id=case) for case in BUSY_PAGES],
    pytest.param("family review", WriterBusy, id="family review, its writer busy"),
]


@pytest.mark.parametrize(("case", "kind"), BUSY_KINDS)
def test_a_page_whose_drafts_are_refused_as_busy_falls_back_and_writes_nothing(
    case: str, kind: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drafts read refused as busy, inside the catch a page or a press is made in, gets
    the page that catch already falls back to, the same as a file that can't be read."""
    page = BUSY_PAGES[case]
    with unheld() as client:
        sent = busy_request(client, case, TO_TODAY)
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(state_of(client).drafts, page.read, refusing(kind, seen))
            answer = answer_to(client, sent)
        monkeypatch.undo()
        after = every_row(database_of(client))

    busy_answer(answer, page, TO_TODAY)
    assert after_the_failure(seen) == []
    assert after == before


@pytest.mark.parametrize("case", list(BUSY_PAGES))
def test_a_page_whose_drafts_another_caller_holds_falls_back_and_writes_nothing(
    case: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The drafts store's own lock held past its wait, so each drafts read is refused as
    busy in earnest: the page that catch falls back to, and nothing written."""
    page = BUSY_PAGES[case]
    with HangWatch(capsys) as watch, unheld() as client:
        sent = busy_request(client, case, TO_A_PLAN)
        before = every_row(database_of(client))
        with LockHeldByAnother(state_of(client).drafts):
            answer, _ = watch.press(lambda: answer_to(client, sent))
        after = every_row(database_of(client))

    busy_answer(answer, page, TO_A_PLAN)
    assert after == before


@pytest.mark.parametrize("case", list(BUSY_PAGES))
def test_a_page_whose_drafts_are_free_answers_as_before(case: str) -> None:
    with unheld() as client:
        answer = answer_to(client, busy_request(client, case, TO_A_PLAN))

    assert answer.status_code == ANSWERED_AS_BEFORE[case], answer.text[:300]
    assert "can't be shown right now" not in answer.text
    assert "cannot be read right now" not in answer.text


def test_family_review_whose_drafts_another_caller_holds_is_not_kept_signed_in(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with HangWatch(capsys) as watch, quiet(household_client("parent", tmp_path)) as client:
        sign_in_as(client, "parent")
        with LockHeldByAnother(state_of(client).drafts):
            answer, _ = watch.press(lambda: client.get("/parent", headers=PAGE_HEADERS))

    store_free_page(answer, status=503, heading="Family review", alert=FAMILY_NOT_READ)
    assert answer.headers["cache-control"] == "no-store"


def test_three_pages_at_once_on_a_held_file_each_fall_back(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three pages asked for 30 ms apart while another program holds the file: each drafts
    read holds the store's lock through its file wait, so a later one can be refused as busy
    first. A run in which none was is inconclusive; the held lock above is the sure case."""
    addresses = ["/parent", HER_PAGE, "/parent"]
    answers: dict[int, Any] = {}
    with HangWatch(capsys) as watch, unheld() as client:
        for address in addresses[:2]:
            assert client.get(address, headers=PAGE_HEADERS).status_code == 200
        refused = busy_refusals(monkeypatch, state_of(client).drafts)

        def ask(i: int) -> None:
            answers[i] = client.get(addresses[i], headers=PAGE_HEADERS)

        def three_at_once() -> None:
            asking = [threading.Thread(target=ask, args=(i,), daemon=True) for i in range(3)]
            for thread in asking:
                thread.start()
                time.sleep(0.03)
            for thread in asking:
                thread.join(HANG_LIMIT_SECONDS)

        with HeldByAnother(database_of(client)):
            watch.press(three_at_once)
        monkeypatch.undo()

    assert sorted(answers) == [0, 1, 2]
    for i, address in enumerate(addresses):
        if address == "/parent":
            store_free_page(answers[i], status=503, heading="Family review", alert=FAMILY_NOT_READ)
        else:
            store_free_page(answers[i], status=503, heading=WEEK_UNREADABLE, alert=WEEK_NOT_READ)
    if not refused:
        pytest.skip("no drafts read was refused as busy in this run, so it shows nothing of them")


TURNED_IN_WORDS = {"note": "Outline finished, essay next", "expected_report_id": ""}
TURNED_IN_SAID = "You reported this turned in."
NOT_SAVED_ON_A_TURN_IN = (
    hand_in_routes.HAND_IN_NOT_SAVED,
    hand_in_routes.HAND_IN_NOT_UNDONE,
    hand_in_routes.LIST_NOT_SAVED,
    hand_in_routes.LIST_TURN_IN_FAILED,
    hand_in_routes.LIST_UNDO_FAILED,
    "Update not saved",
)


def turn_ins(client: TestClient) -> int:
    """How many turn-in events the file holds, read through a connection of its own."""
    connection = sqlite3.connect(database_of(client))
    try:
        return connection.execute("SELECT COUNT(*) FROM hand_in_events").fetchone()[0]
    finally:
        connection.close()


@pytest.mark.parametrize("busy", ["refused", "held by another caller"])
def test_a_turn_in_with_her_words_saved_before_her_week_is_busy_says_it_was_saved(
    busy: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """I turned it in with her update's words is saved first; her week, which its answer
    is, then can't read the drafts. The answer says what was saved, keeps her words, and
    says nothing of a turn-in not saved."""
    with HangWatch(capsys) as watch, unheld() as client:
        press = a_card_turn_in(client)
        sent = Sent(press.method, press.path, {**(press.data or {}), **TURNED_IN_WORDS})
        before = turn_ins(client)
        if busy == "refused":
            drafts = state_of(client).drafts
            monkeypatch.setattr(drafts, "newest_published", refusing(StoreBusy))
            answer, _ = watch.press(lambda: answer_to(client, sent))
            monkeypatch.undo()
        else:
            with LockHeldByAnother(state_of(client).drafts):
                answer, _ = watch.press(lambda: answer_to(client, sent))
        after = turn_ins(client)

    main = store_free_page(
        answer,
        status=503,
        heading="Turning it in",
        alert=f"{TURNED_IN_SAID} {YOUR_WEEK}",
    )
    assert after == before + 1
    assert TURNED_IN_WORDS["note"] in main
    for never in NOT_SAVED_ON_A_TURN_IN:
        assert never not in main, never


def test_a_turn_in_with_her_words_is_her_week_as_before_when_the_drafts_are_free() -> None:
    with unheld() as client:
        press = a_card_turn_in(client)
        sent = Sent(press.method, press.path, {**(press.data or {}), **TURNED_IN_WORDS})
        before = turn_ins(client)
        answer = answer_to(client, sent)
        after = turn_ins(client)

    assert answer.status_code == 200, answer.text[:300]
    assert TURNED_IN_SAID in answer.text
    assert after == before + 1


def test_a_decision_on_a_decided_draft_keeps_its_409_when_the_drafts_are_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with quiet(browser(key=True)) as client:
        walkthrough(client)
        made = planned(client)
        decide = f"/parent/actions/decide/{made.draft_id}"
        assert client.post(decide, data={"decision": "approve", "reason": ""}).status_code == 303
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).drafts, "review_snapshot", refusing(StoreBusy, seen)
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


def test_family_review_after_a_saved_keep_whose_drafts_another_caller_holds_falls_back(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A paste kept is saved and redirects to family review; that page then can't read the
    drafts. It falls back, and what the keep saved stays saved."""
    with HangWatch(capsys) as watch, unheld() as client:
        before = every_row(database_of(client))
        with LockHeldByAnother(state_of(client).drafts):
            kept, _ = watch.press(lambda: answer_to(client, a_paste_saved(client)))
            assert kept.status_code == 303, kept.text[:300]
            where = kept.headers["location"]
            answer, _ = watch.press(lambda: client.get(where, headers=PAGE_HEADERS))
        after = every_row(database_of(client))

    store_free_page(answer, status=503, heading="Family review", alert=FAMILY_NOT_READ)
    assert set(after) - set(before)
