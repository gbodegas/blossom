"""Her writes are hers: a parent's press on any of them is refused before anything is read.

Thirteen presses under her tree change her record: her update and its Undo, her hand-in
update and its Undo, a new note, a note's edit, archive, restore and delete, and a note's
details, adding it to homework, linking it to homework found and unlinking it. A parent
signed in is answered 403 from the sign-in and the route alone: no form is read, no id the
press names is looked up, the decision lock is not taken, and nothing is written. A
parent's update, hand-in or Undo lands on her current week, whatever page the form came
from. The four note presses keep a bounded copy of what that same request typed, read only
after the refusal is fixed and never past 16 KiB, on a page that reads no store.

The family's tree has the same four note presses. She may reach them only to be refused
the same way; a parent pressing them is the family's own workflow and is answered as it
was. Her own presses, and every press with the sign-in off, are answered as they were.
The fixture week through the app, a pinned day, synthetic words, and no model.
"""

import asyncio
import logging
import pathlib
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from starlette.requests import Request
from starlette.types import Message, Scope

from blossom.app import create_app
from blossom.captures import PARENT, STUDENT, CaptureCreated, new_capture_id
from blossom.hand_in import NEEDS_HAND_IN, HandInSaved
from blossom.household import COOKIE, SESSION_SECONDS, issue
from blossom.principals import Principal
from blossom.reconciliation import SourceChannel
from blossom.routes import captures as capture_routes
from blossom.routes import forms as form_routes
from blossom.routes import hand_in as hand_in_routes
from blossom.routes import note_details, note_links
from blossom.routes import student as student_routes
from blossom.routes.navigation import (
    NEW_NOTE_PAGE,
    NOTE_ACTIONS,
    TO_TURN_IN_PAGE,
    details_href,
    note_action,
    note_add_action,
    note_add_href,
    note_delete_href,
    note_details_action,
    note_href,
    note_link_action,
    note_search_href,
    note_unlink_action,
)
from blossom.stores.project_state import ProjectStateStore, Saved
from tests.support import (
    ESSAY_ID,
    FIXTURE_WEEK,
    HER_PAGE,
    HERS,
    PAGE_HEADERS,
    PLAN_DATE,
    SAME_ORIGIN,
    THEIRS,
    Answer,
    card_for,
    fixture_settings,
    link_note,
    main_of,
    signed_in,
    signed_in_household,
    state_of,
    store_of,
    whole_form,
    words,
)

Reader = Literal["her", "parent", "open"]

NOW: Final = datetime(2026, 8, 19, 13, 0, tzinfo=UTC)
NOT_HERS: Final = "Sign in as the student to update."
NOT_A_PARENTS: Final = "Sign in as a parent to add details here. Nothing was saved."
WEEK_TOP: Final = f'<p class="problem" role="alert">{NOT_HERS}</p>'
UNSAVED: Final = '<h2 class="update-heading">Your unsaved details</h2>'
NOT_SAVED_SENTENCE: Final = "These details were not saved. Everything typed is here to copy."
NOWHERE: Final = "assignment-nowhere"
NOT_A_NOTE: Final = "not-a-note"
URL_ENCODED: Final = "application/x-www-form-urlencoded"
BROKEN_MULTIPART: Final = (
    b"--synthetic\r\nContent-Disposition: form-data\r\n\r\nno end",
    "multipart/form-data; boundary=synthetic",
)
LIMIT: Final = 16 * 1024

ASSIGNMENT_PRESSES: Final = ("report", "undo-report", "hand-in", "undo-hand-in")
NOTE_CHANGES: Final = ("edit", "archive", "restore", "delete")
NOTE_PRESSES: Final = ("details", "add", "link", "unlink")
"""The four presses that keep a bounded copy of what the refused request typed."""
EVERY_PRESS: Final = (*ASSIGNMENT_PRESSES, "create", *NOTE_CHANGES, *NOTE_PRESSES)
ORIGINS: Final[dict[str, tuple[str, ...]]] = {
    "report": ("week", "details"),
    "undo-report": ("week", "details"),
    "hand-in": ("details", "list", "week list"),
    "undo-hand-in": ("details", "list", "week list"),
}
SHOWN: Final[dict[str, str]] = {
    "report": "note",
    "undo-report": "report_id",
    "hand-in": "note",
    "undo-hand-in": "hand_in_id",
    "create": "text",
    "edit": "text",
    "archive": "revision",
    "restore": "revision",
    "delete": "revision",
    "details": "title",
    "add": "title",
    "link": "q",
    "unlink": "from",
}
"""The field of each press whose words the tests look for, and the one sent twice."""
TYPED: Final[dict[str, str]] = {
    "details": "Wren's <b>title</b>",
    "add": "Wren's <b>title</b>",
    "link": "Wren's <b>search</b>",
    "unlink": "Wren's <b>link</b>",
}
BY_ID_READS: Final = (
    "one_assignment",
    "capture",
    "capture_history",
    "sound_capture_history",
    "capture_deleted",
    "capture_use",
    "captures_of_assignment",
    "claim_history",
)
"""The store's reads of one record by the id a caller names."""
ROUTE_MODULES: Final = {
    "student": student_routes,
    "hand_in": hand_in_routes,
    "captures": capture_routes,
    "note_details": note_details,
    "note_links": note_links,
}


# ------------------------------------------------------------------ the household


@dataclass(frozen=True)
class Seeded:
    """Her record as the presses find it: an update and a hand-in update on the essay, a
    note that waits, one put away, and one joined to homework the school lists."""

    report: str
    hand_in: str
    waiting: str
    archived: str
    joined: str
    homework: str


def a_note(store: ProjectStateStore, text: str) -> str:
    name = new_capture_id()
    made = store.create_capture(
        name,
        text,
        None,
        None,
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=NOW,
        today=PLAN_DATE,
    )
    assert isinstance(made, CaptureCreated)
    return name


def seed(client: TestClient) -> Seeded:
    store = store_of(client)
    saved = store.report_status(
        ESSAY_ID, "done", None, expected_head=None, now=NOW, today=PLAN_DATE
    )
    assert isinstance(saved, Saved)
    handed = store.record_hand_in(
        ESSAY_ID, NEEDS_HAND_IN, "Print it", None, expected_head=None, now=NOW, today=PLAN_DATE
    )
    assert isinstance(handed, HandInSaved)
    waiting = a_note(store, "Questions 4-8, heard in class")
    archived = a_note(store, "An old note, put away")
    store.archive_capture(
        archived, expected_revision=1, authored_by=STUDENT, now=NOW, today=PLAN_DATE
    )
    joined = a_note(store, "Summer reading, joined")
    homework = link_note(store, joined)
    return Seeded(
        report=saved.report.report_id,
        hand_in=handed.event.event_id,
        waiting=waiting,
        archived=archived,
        joined=joined,
        homework=homework.assignment_id,
    )


@contextmanager
def household(tmp_path: pathlib.Path, reader: Reader) -> Iterator[TestClient]:
    """The pinned day read by one reader: her, a parent, or anyone with the sign-in off."""
    if reader == "open":
        settings = fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat())
    else:
        settings = signed_in_household(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN) as client:
        if reader != "open":
            signed_in(client, HERS if reader == "her" else THEIRS)
        yield client


def as_a_parent(client: TestClient) -> None:
    client.post("/sign-out")
    signed_in(client, THEIRS)


def everything_kept(client: TestClient) -> dict[str, list[str]]:
    """Every row of every table in the household's files, read through a connection of its
    own so the reading is no part of what a press is watched doing."""
    settings = state_of(client).settings
    kept: dict[str, list[str]] = {}
    for path in (settings.database_path, settings.trace_path):
        with closing(sqlite3.connect(path)) as connection:
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            ]
            for table in tables:
                rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()  # noqa: S608
                kept[f"{path.name}:{table}"] = sorted(repr(row) for row in rows)
    return kept


# ------------------------------------------------------------------ watching a press


@dataclass
class Watch:
    """What a press did while it was watched."""

    read: list[str] = field(default_factory=list)
    """Every form reader and body reader called, by name."""
    looked_up: list[str] = field(default_factory=list)
    """Every read of one record by an id."""
    statements: list[str] = field(default_factory=list)
    """Every statement run on any of the household's connections, with its values."""
    locked: int = 0
    copied: list[int] = field(default_factory=list)
    """The length of each body handed to the copy's parser."""


Spied = Callable[..., Awaitable[object]]


def recorded(original: Spied, name: str, watch: Watch) -> Spied:
    """A reader that notes its name each time it is called, then reads as it did."""

    async def spy(*args: object, **kwargs: object) -> object:
        watch.read.append(name)
        return await original(*args, **kwargs)

    return spy


@contextmanager
def watched(client: TestClient) -> Iterator[Watch]:
    """Watch every form reader, every body reader, every by-id read, every statement and
    the decision lock while the block runs, and put everything back after."""
    watch = Watch()
    state = state_of(client)
    store = state.project_state
    connections = [
        store._connection,
        state.drafts._connection,
        state.help_requests._connection,
        state.workload_signals._connection,
        state.traces._connection,
    ]
    with pytest.MonkeyPatch.context() as patch:
        for name, module in ROUTE_MODULES.items():
            patch.setattr(
                module, "fields_of", recorded(module.fields_of, f"{name}.fields_of", watch)
            )
        for module in (note_details, note_links):
            kept = getattr(module, "kept_fields_of", None)
            if kept is not None:
                patch.setattr(module, "kept_fields_of", recorded(kept, "kept_fields_of", watch))
        parse = getattr(form_routes, "fields_in", None)
        if parse is not None:

            async def parsed(body: bytes, *args: object, **kwargs: object) -> object:
                watch.copied.append(len(body))
                return await parse(body, *args, **kwargs)

            patch.setattr(form_routes, "fields_in", parsed)
        original_form: Callable[..., object] = Request.form
        original_body = Request.body

        def form(self: Request, *args: object, **kwargs: object) -> object:
            watch.read.append("Request.form")
            return original_form(self, *args, **kwargs)

        async def body(self: Request) -> bytes:
            watch.read.append("Request.body")
            return await original_body(self)

        patch.setattr(Request, "form", form)
        patch.setattr(Request, "body", body)
        for name in BY_ID_READS:
            original: Callable[..., object] = getattr(store, name)

            def looked_up(
                *args: object,
                _name: str = name,
                _read: Callable[..., object] = original,
                **kwargs: object,
            ) -> object:
                watch.looked_up.append(_name)
                return _read(*args, **kwargs)

            patch.setattr(store, name, looked_up)
        lock = state.decision_lock
        acquire = lock.acquire

        async def acquired() -> bool:
            watch.locked += 1
            return await acquire()

        patch.setattr(lock, "acquire", acquired)
        for connection in connections:
            connection.set_trace_callback(watch.statements.append)
        try:
            yield watch
        finally:
            for connection in connections:
                connection.set_trace_callback(None)


# ------------------------------------------------------------------ the presses


def press_path(press: str, subject: str, *, family: bool = False) -> str:
    """Where a press goes, for the assignment or the note it names."""
    if press in ASSIGNMENT_PRESSES:
        return f"/student/actions/assignments/{subject}/{press}"
    if press == "create":
        return NOTE_ACTIONS
    if press in NOTE_CHANGES:
        return note_action(subject, press)
    return {
        "details": note_details_action,
        "add": note_add_action,
        "link": note_link_action,
        "unlink": note_unlink_action,
    }[press](subject, family=family)


def subject_of(press: str, kind: str, seeded: Seeded) -> str:
    """The id a press names: one on record, one that names nothing, or one of no id's shape."""
    if press in ASSIGNMENT_PRESSES:
        return ESSAY_ID if kind == "known" else NOWHERE
    if kind == "unknown":
        return new_capture_id()
    if kind == "misshapen":
        return NOT_A_NOTE
    return {"restore": seeded.archived, "unlink": seeded.joined}.get(press, seeded.waiting)


def valid_form(press: str, origin: str, seeded: Seeded) -> list[tuple[str, str]]:
    """The form a page of hers sends for a press, as she left it: every field, once."""
    week_return = [("return_to", "week"), ("week", FIXTURE_WEEK), ("plan_id", "")]
    match press, origin:
        case "report", "week":
            return [
                ("status", "done"),
                ("note", "Wren's <b>update</b>"),
                ("expected_report_id", seeded.report),
                ("week", FIXTURE_WEEK),
            ]
        case "report", _:
            return [
                ("status", "done"),
                ("note", "Wren's <b>update</b>"),
                ("expected_report_id", seeded.report),
                ("report_view", "detail"),
                *week_return,
            ]
        case "undo-report", "week":
            return [("report_id", seeded.report), ("week", FIXTURE_WEEK)]
        case "undo-report", _:
            return [("report_id", seeded.report), ("report_view", "detail"), *week_return]
        case "hand-in", _:
            fields = [
                ("state", "turned_in"),
                ("next_action", ""),
                ("note", "" if origin != "details" else "Wren's <b>hand-in</b>"),
                ("expected_hand_in_id", seeded.hand_in),
            ]
        case "undo-hand-in", _:
            fields = [("hand_in_id", seeded.hand_in)]
        case "create", _:
            return [
                ("capture_id", new_capture_id()),
                ("text", "Wren's <b>words</b>"),
                ("course", ""),
                ("due_date", ""),
                ("choice", "save"),
            ]
        case "edit", _:
            return [
                ("text", "Wren's <b>words</b>"),
                ("course", ""),
                ("due_date", ""),
                ("revision", "1"),
                ("choice", "save"),
            ]
        case "archive" | "restore" | "delete", _:
            return [("revision", "2" if press == "restore" else "1")]
        case "details" | "add", _:
            return [
                ("revision", "1"),
                ("basis", "0" * 64),
                ("course_choice", "__another__"),
                ("course_other", "Geometry"),
                ("title", TYPED[press]),
                ("due_date", "2026-08-21"),
                ("kind", "TASK"),
                ("note", "Typed <i>note</i>"),
            ]
        case "link", _:
            return [
                ("revision", "1"),
                ("target", seeded.homework),
                ("basis", "0" * 64),
                ("q", TYPED["link"]),
                ("page", "1"),
            ]
        case _:
            return [("revision", "2"), ("from", TYPED["unlink"])]
    if origin == "details":
        return [*fields, *week_return]
    return [*fields, ("hand_in_view", "list" if origin == "list" else "week")]


def bodies(press: str, form: list[tuple[str, str]]) -> dict[str, tuple[bytes, str]]:
    """The bodies a press is sent with: her stale form, the form with its words sent twice,
    a broken multipart body, 1001 fields, and one field over 1 MiB."""
    shown = SHOWN[press]
    return {
        "her stale form": (urlencode(form).encode(), URL_ENCODED),
        "a field twice": (urlencode([*form, (shown, "second <i>value</i>")]).encode(), URL_ENCODED),
        "broken multipart": BROKEN_MULTIPART,
        "1001 fields": (urlencode([(f"f{n}", "1") for n in range(1001)]).encode(), URL_ENCODED),
        "over 1 MiB": (
            urlencode([*form, (shown, "x" * (1024 * 1024 + 1))]).encode(),
            URL_ENCODED,
        ),
    }


def sent(client: TestClient, path: str, body: bytes, kind: str) -> Answer:
    return client.post(path, content=body, headers={**PAGE_HEADERS, "Content-Type": kind})


def copy_made(press: str, body: str) -> bool:
    """Whether a refused note press is to show a copy: only a readable form within 16 KiB."""
    return press in NOTE_PRESSES and body in ("her stale form", "a field twice")


def check_refused(
    press: str, answer: Answer, *, subject: str, typed: str | None, family: bool = False
) -> None:
    """The fixed refusal of a known forbidden caller: 403, the page that says whose press
    it is and the ways back, and the typed words only as a copy under its heading."""
    assert answer.status_code == 403, (press, answer.status_code, answer.text[:300])
    page = main_of(answer.text)
    refusal = NOT_A_PARENTS if family else NOT_HERS
    assert str(escape(refusal)) in page
    if press in ASSIGNMENT_PRESSES:
        assert "<h1>Student week</h1>" in page
        assert WEEK_TOP in page
        assert "August 17 to August 23, 2026" in words(page)
        assert " autofocus" not in answer.text
        assert "Wren" not in answer.text
        return
    assert page.count(" autofocus") == 1
    assert "<form" not in page
    assert "<button" not in page
    if press == "create" or press in NOTE_CHANGES:
        assert "Wren" not in page
    if press == "create":
        assert "<h1>Write down homework</h1>" in page
        assert "A homework note is hers to write." in page
        return
    back = f'<a href="{note_href(subject)}">Back to the note</a>'
    assert (back in page) is (subject != NOT_A_NOTE), subject
    if press in NOTE_CHANGES:
        assert "<h1>Note not changed</h1>" in page
        assert '<a href="/student/homework-notes">Back to Homework notes</a>' in page
        assert ("Back to her week" if not family else "Back to my week") in page
        return
    heading = "Update not saved" if press in ("details", "add") else "Nothing was saved"
    assert f"<h1>{heading}</h1>" in page
    for tag in ("<input", "<textarea"):
        for found in page.split(tag)[1:]:
            assert " readonly" in found.split(">", 1)[0], found[:120]
    if typed is None:
        assert UNSAVED not in page
        assert "Wren" not in page
        assert "second" not in page
    else:
        assert UNSAVED in page
        assert NOT_SAVED_SENTENCE in page
        assert str(escape(typed)) in page
        assert "second" not in page


def check_untouched(watch: Watch, press: str, subject: str) -> None:
    """No form parsed, no body buffered, no id looked up, no lock taken; the four note presses
    read at most the bounded copy, and the note presses run no statement at all."""
    assert watch.read == (["kept_fields_of"] if press in NOTE_PRESSES else []), watch.read
    assert watch.looked_up == []
    assert watch.locked == 0
    assert all(length <= LIMIT for length in watch.copied), watch.copied
    if press in ASSIGNMENT_PRESSES:
        assert [line for line in watch.statements if f"= '{subject}'" in line] == []
        if subject == NOWHERE:
            assert [line for line in watch.statements if subject in line] == []
    else:
        assert watch.statements == [], watch.statements[:3]


# ------------------------------------------------------------------ 1. the matrix


MATRIX: Final = [
    *[
        (press, origin, kind)
        for press in ASSIGNMENT_PRESSES
        for origin in ORIGINS[press]
        for kind in ("known", "unknown")
    ],
    ("create", "page", "none"),
    *[
        (press, "page", kind)
        for press in (*NOTE_CHANGES, *NOTE_PRESSES)
        for kind in ("known", "unknown", "misshapen")
    ],
]


@pytest.mark.parametrize(("press", "origin", "kind"), MATRIX)
def test_a_parents_press_is_refused_before_the_form_the_id_or_the_lock(
    press: str,
    origin: str,
    kind: str,
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every body and every id gets the same 403: her stale form, a field twice, a broken
    multipart body, 1001 fields and a field over 1 MiB, for a known id, an unknown one and,
    for a note, one of no id's shape. Nothing is read but the four note presses' bounded
    copy, nothing is looked up, the lock is not taken, nothing is logged from the body, and
    no row of any table changes."""
    caplog.set_level(logging.DEBUG)
    with household(tmp_path, "parent") as client:
        seeded = seed(client)
        subject = subject_of(press, kind, seeded)
        path = press_path(press, subject)
        form = valid_form(press, origin, seeded)
        before = everything_kept(client)
        for body_name, (body, content_type) in bodies(press, form).items():
            with watched(client) as watch:
                answer = sent(client, path, body, content_type)
            typed = dict(form)[SHOWN[press]] if copy_made(press, body_name) else None
            check_refused(press, answer, subject=subject, typed=typed)
            check_untouched(watch, press, subject)
        after = everything_kept(client)

    assert after == before
    assert [record for record in caplog.records if "Wren" in record.getMessage()] == []
    assert [record for record in caplog.records if "xxxxxxxx" in record.getMessage()] == []


# ------------------------------------------------------------------ 2. the decision lock


def test_a_parent_is_answered_at_once_while_a_decision_holds_the_lock(
    tmp_path: pathlib.Path,
) -> None:
    """Her presses wait for a decision to land; a parent's refusal waits for nothing and
    never takes the lock, on all thirteen presses."""
    with household(tmp_path, "parent") as client, ThreadPoolExecutor(max_workers=13) as pool:
        seeded = seed(client)
        state = state_of(client)
        portal = client.portal
        assert portal is not None
        before = everything_kept(client)
        presses = {
            press: (
                press_path(press, subject_of(press, "known", seeded)),
                urlencode(valid_form(press, ORIGINS.get(press, ("page",))[0], seeded)).encode(),
            )
            for press in EVERY_PRESS
        }
        portal.call(state.decision_lock.acquire)
        try:
            answers = {
                press: pool.submit(sent, client, path, body, URL_ENCODED)
                for press, (path, body) in presses.items()
            }
            _, still_waiting = wait(answers.values(), timeout=5)
        finally:
            portal.call(state.decision_lock.release)
        after = everything_kept(client)

    assert still_waiting == set()
    assert {
        press: answer.result().status_code for press, answer in answers.items()
    } == dict.fromkeys(EVERY_PRESS, 403)
    assert after == before


def test_her_press_on_the_family_tree_is_answered_at_once_while_a_decision_holds_the_lock(
    tmp_path: pathlib.Path,
) -> None:
    with household(tmp_path, "her") as client, ThreadPoolExecutor(max_workers=4) as pool:
        seeded = seed(client)
        state = state_of(client)
        portal = client.portal
        assert portal is not None
        before = everything_kept(client)
        portal.call(state.decision_lock.acquire)
        try:
            answers = [
                pool.submit(
                    sent,
                    client,
                    press_path(press, subject_of(press, "known", seeded), family=True),
                    urlencode(valid_form(press, "page", seeded)).encode(),
                    URL_ENCODED,
                )
                for press in NOTE_PRESSES
            ]
            _, still_waiting = wait(answers, timeout=5)
        finally:
            portal.call(state.decision_lock.release)
        after = everything_kept(client)

    assert still_waiting == set()
    assert [answer.result().status_code for answer in answers] == [403, 403, 403, 403]
    assert after == before


@pytest.mark.parametrize("reader", ["her", "open"])
def test_her_press_still_waits_while_a_decision_holds_the_lock(
    reader: Reader, tmp_path: pathlib.Path
) -> None:
    with household(tmp_path, reader) as client, ThreadPoolExecutor(max_workers=2) as pool:
        seeded = seed(client)
        state = state_of(client)
        portal = client.portal
        assert portal is not None
        form = dict(valid_form("report", "week", seeded))
        portal.call(state.decision_lock.acquire)
        try:
            pressed = pool.submit(
                sent,
                client,
                press_path("report", ESSAY_ID),
                urlencode({**form, "status": "not_yet"}).encode(),
                URL_ENCODED,
            )
            _, still_waiting = wait([pressed], timeout=0.3)
        finally:
            portal.call(state.decision_lock.release)
        answer = pressed.result(timeout=10)

    assert still_waiting == {pressed}
    assert answer.status_code == 303


# ------------------------------------------------------------------ 3. a shared device


def her_stale_forms(client: TestClient, seeded: Seeded) -> dict[str, tuple[str, dict[str, str]]]:
    """Every one of the thirteen forms as her own pages drew them, read while she was signed
    in, with the choice her browser would add when she pressed."""
    week = client.get(HER_PAGE, params={"change": ESSAY_ID}, headers=PAGE_HEADERS).text
    card = card_for(week, ESSAY_ID)
    details = client.get(details_href(ESSAY_ID, return_to="week"), headers=PAGE_HEADERS).text
    changing = client.get(
        details_href(ESSAY_ID, return_to="week", change="1"), headers=PAGE_HEADERS
    ).text
    opened = client.get(
        details_href(ESSAY_ID, return_to="week", hand_in="change"), headers=PAGE_HEADERS
    ).text
    listed = client.get(TO_TURN_IN_PAGE, headers=PAGE_HEADERS).text
    new = client.get(NEW_NOTE_PAGE, headers=PAGE_HEADERS).text
    editing = client.get(note_href(seeded.waiting, edit="1"), headers=PAGE_HEADERS).text
    waiting = client.get(note_href(seeded.waiting), headers=PAGE_HEADERS).text
    archived = client.get(note_href(seeded.archived), headers=PAGE_HEADERS).text
    deleting = client.get(note_delete_href(seeded.waiting), headers=PAGE_HEADERS).text
    adding = client.get(note_add_href(seeded.waiting), headers=PAGE_HEADERS).text
    searched = client.get(note_search_href(seeded.waiting, q="canal"), headers=PAGE_HEADERS).text
    joined = client.get(note_add_href(seeded.joined), headers=PAGE_HEADERS).text
    report = press_path("report", ESSAY_ID)
    undo = press_path("undo-report", ESSAY_ID)
    hand_in = press_path("hand-in", ESSAY_ID)
    undo_hand_in = press_path("undo-hand-in", ESSAY_ID)
    add = note_add_action(seeded.waiting)
    link = note_link_action(seeded.waiting)
    return {
        "report from her week": (report, {**whole_form(card, report), "status": "done"}),
        "report from the details": (report, {**whole_form(changing, report), "status": "done"}),
        "undo from the details": (undo, whole_form(details, undo)),
        "hand-in from the details": (
            hand_in,
            {**whole_form(opened, hand_in), "state": "turned_in"},
        ),
        "hand-in from To turn in": (hand_in, whole_form(listed, hand_in)),
        "undo hand-in from the details": (undo_hand_in, whole_form(details, undo_hand_in)),
        "create": (NOTE_ACTIONS, {**whole_form(new, NOTE_ACTIONS), "text": "Wren's words"}),
        "edit": (
            note_action(seeded.waiting, "edit"),
            whole_form(editing, note_action(seeded.waiting, "edit")),
        ),
        "archive": (
            note_action(seeded.waiting, "archive"),
            whole_form(waiting, note_action(seeded.waiting, "archive")),
        ),
        "restore": (
            note_action(seeded.archived, "restore"),
            whole_form(archived, note_action(seeded.archived, "restore")),
        ),
        "delete": (
            note_action(seeded.waiting, "delete"),
            whole_form(deleting, note_action(seeded.waiting, "delete")),
        ),
        "details": (
            note_details_action(seeded.waiting),
            {**whole_form(adding, add), "title": "Wren's <b>title</b>"},
        ),
        "add": (add, {**whole_form(adding, add), "title": "Wren's <b>title</b>"}),
        "link": (link, first_link_form(searched, link)),
        "unlink": (
            note_unlink_action(seeded.joined),
            whole_form(joined, note_unlink_action(seeded.joined)),
        ),
    }


def first_link_form(page: str, action: str) -> dict[str, str]:
    """The first result's link press on the search page, which draws one form per result."""
    start = page.index(f'action="{action}"')
    one = page[page.rindex("<form", 0, start) : page.index("</form>", start) + len("</form>")]
    return whole_form(one, action)


def test_her_pages_left_open_when_a_parent_signs_in_refuse_every_press(
    tmp_path: pathlib.Path,
) -> None:
    """On one device: her pages open with their forms, a parent signs in, and every press
    on the pages she left is refused with the sentence that says what to do and the ways
    back, and nothing is written."""
    with household(tmp_path, "her") as client:
        seeded = seed(client)
        forms = her_stale_forms(client, seeded)
        as_a_parent(client)
        before = everything_kept(client)
        answers = {
            name: client.post(path, data=form, headers=PAGE_HEADERS)
            for name, (path, form) in forms.items()
        }
        after = everything_kept(client)

    assert after == before
    for name, answer in answers.items():
        assert answer.status_code == 403, (name, answer.text[:300])
        page = main_of(answer.text)
        assert NOT_HERS in page, name
        assert '<a href="' in page, name
    for name in ("report from her week", "report from the details", "undo from the details"):
        assert "<h1>Student week</h1>" in answers[name].text
    for name in ("details", "add"):
        assert str(escape("Wren's <b>title</b>")) in answers[name].text
        assert UNSAVED in answers[name].text


# ------------------------------------------------------------------ 4. the bounded copy


@dataclass(frozen=True)
class Streamed:
    """What the application answered a body sent in chunks, and how much of it it read."""

    status_code: int
    text: str
    headers: dict[str, str]
    pulled: int


def streamed(
    client: TestClient,
    path: str,
    chunks: list[bytes],
    headers: list[tuple[str, str | bytes]],
    *,
    then: Literal["end", "disconnect"] = "end",
) -> Streamed:
    """Send a press straight to the application, its body in these chunks, then the end of
    the body or the client going away; count the body bytes the application asked for."""
    cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"origin", b"http://testserver"),
            (b"accept", b"text/html"),
            (b"cookie", cookie.encode("latin-1")),
            *[(name.lower().encode("latin-1"), raw(value)) for name, value in headers],
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    last = then == "end"
    messages: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": not (last and at == len(chunks) - 1)}
        for at, chunk in enumerate(chunks or [b""])
    ]

    async def run() -> Streamed:
        pulled = 0
        status = 0
        said: dict[str, str] = {}
        answer = bytearray()

        async def receive() -> Message:
            nonlocal pulled
            if messages:
                message = messages.pop(0)
                pulled += len(message["body"])
                return message
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                said.update(
                    (name.decode("latin-1"), value.decode("latin-1"))
                    for name, value in message.get("headers", [])
                )
            elif message["type"] == "http.response.body":
                answer.extend(message.get("body", b""))

        await client.app(scope, receive, send)
        return Streamed(status, answer.decode("utf-8"), said, pulled)

    portal = client.portal
    assert portal is not None
    return portal.call(run)


def raw(value: str | bytes) -> bytes:
    """A header's value as it goes on the wire: text as UTF-8, bytes as they are."""
    return value if isinstance(value, bytes) else value.encode("utf-8")


def sized(press: str, seeded: Seeded, size: int, typed: str | None = None) -> bytes:
    """Her stale form for a press, made exactly ``size`` bytes long by a field no page sends,
    which is not kept."""
    form = [
        (name, typed if typed is not None and name == SHOWN[press] else value)
        for name, value in valid_form(press, "page", seeded)
    ]
    bare = urlencode([*form, ("padding", "")]).encode()
    assert len(bare) <= size, (press, len(bare))
    return urlencode([*form, ("padding", "x" * (size - len(bare)))]).encode()


def in_pieces(body: bytes, count: int) -> list[bytes]:
    step = -(-len(body) // count)
    return [body[at : at + step] for at in range(0, len(body), step)]


FORM_HEADER: Final[tuple[str, str | bytes]] = ("content-type", URL_ENCODED)


def stream_cases(
    press: str, seeded: Seeded
) -> dict[str, tuple[list[bytes], list[tuple[str, str | bytes]], str, str | None]]:
    """Each way a body can arrive: its chunks, its headers, how the stream ends, and the
    words the copy is to show, or ``None`` when no copy is to be made."""
    typed = TYPED[press]
    whole = sized(press, seeded, LIMIT)
    over = sized(press, seeded, LIMIT + 1)
    small = sized(press, seeded, 600)
    length = [FORM_HEADER, ("content-length", str(LIMIT))]
    return {
        "exactly 16 KiB": ([whole], length, "end", typed),
        "exactly 16 KiB in 16 chunks": (in_pieces(whole, 16), length, "end", typed),
        "exactly 16 KiB in 7 uneven chunks": (in_pieces(whole, 7), length, "end", typed),
        "one byte over, declared": (
            in_pieces(over, 4),
            [FORM_HEADER, ("content-length", str(LIMIT + 1))],
            "end",
            None,
        ),
        "one byte over, streamed past a declared 16 KiB": (in_pieces(over, 4), length, "end", None),
        "understated length": (
            [small],
            [FORM_HEADER, ("content-length", str(len(small) - 1))],
            "end",
            None,
        ),
        "a stream that never ends": ([whole[:1024]] * 64, length, "disconnect", None),
        "no length": (in_pieces(small, 3), [FORM_HEADER], "end", None),
        "a length of letters": (
            [small],
            [FORM_HEADER, ("content-length", "six hundred")],
            "end",
            None,
        ),
        "a negative length": ([small], [FORM_HEADER, ("content-length", "-600")], "end", None),
        "a signed length": ([small], [FORM_HEADER, ("content-length", "+600")], "end", None),
        "a length with space": ([small], [FORM_HEADER, ("content-length", " 600")], "end", None),
        "a hexadecimal length": ([small], [FORM_HEADER, ("content-length", "0x258")], "end", None),
        "a length of another script's digits": (
            [small],
            [FORM_HEADER, ("content-length", "\u0666\u0660\u0660")],
            "end",
            None,
        ),
        "a length of superscript digits": (
            [small],
            [FORM_HEADER, ("content-length", b"\xb2\xb3")],
            "end",
            None,
        ),
        "a length of five thousand digits": (
            [small],
            [FORM_HEADER, ("content-length", "9" * 5000)],
            "end",
            None,
        ),
        "two lengths": (
            [small],
            [FORM_HEADER, ("content-length", str(len(small))), ("content-length", str(len(small)))],
            "end",
            None,
        ),
        "a list of lengths": (
            [small],
            [FORM_HEADER, ("content-length", f"{len(small)}, {len(small)}")],
            "end",
            None,
        ),
        "a length beside a transfer coding": (
            [small],
            [FORM_HEADER, ("content-length", str(len(small))), ("transfer-encoding", "chunked")],
            "end",
            None,
        ),
        "a length beside a content coding": (
            [small],
            [FORM_HEADER, ("content-length", str(len(small))), ("content-encoding", "gzip")],
            "end",
            None,
        ),
        "overstated length, then the end": (
            [small],
            [FORM_HEADER, ("content-length", str(len(small) + 10))],
            "end",
            None,
        ),
        "the client goes away": (
            in_pieces(whole, 4)[:2],
            length,
            "disconnect",
            None,
        ),
        "multipart": (
            [BROKEN_MULTIPART[0]],
            [
                ("content-type", BROKEN_MULTIPART[1]),
                ("content-length", str(len(BROKEN_MULTIPART[0]))),
            ],
            "end",
            None,
        ),
        "plain text": (
            [small],
            [("content-type", "text/plain"), ("content-length", str(len(small)))],
            "end",
            None,
        ),
        "two content types": (
            [small],
            [FORM_HEADER, FORM_HEADER, ("content-length", str(len(small)))],
            "end",
            None,
        ),
        "1001 fields within the bound": (
            [urlencode([(f"f{n}", "1") for n in range(1001)]).encode()],
            [
                FORM_HEADER,
                ("content-length", str(len(urlencode([(f"f{n}", "1") for n in range(1001)])))),
            ],
            "end",
            None,
        ),
        "nothing": ([b""], [FORM_HEADER, ("content-length", "0")], "end", None),
        "a charset beside the type": (
            [small],
            [
                ("content-type", f"{URL_ENCODED}; charset=UTF-8"),
                ("content-length", str(len(small))),
            ],
            "end",
            typed,
        ),
    }


@pytest.mark.parametrize("press", NOTE_PRESSES)
@pytest.mark.parametrize("tree", ["hers, by a parent", "the family's, by her"])
def test_the_refusal_copy_is_bounded_by_what_is_read(
    tree: str, press: str, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The copy is made only from a form body of one declared length within 16 KiB, read
    whole and consistent with that length, and never past 16 KiB however much is sent. Every
    other way a body arrives gives the same 403 without a copy. No body reader but the
    bounded one runs, no store is read, the lock is not taken, and nothing is logged."""
    caplog.set_level(logging.DEBUG)
    family = tree == "the family's, by her"
    with household(tmp_path, "her" if family else "parent") as client:
        seeded = seed(client)
        subject = subject_of(press, "known", seeded)
        path = press_path(press, subject, family=family)
        before = everything_kept(client)
        results: dict[str, tuple[Streamed, Watch]] = {}
        for name, (chunks, headers, then, typed) in stream_cases(press, seeded).items():
            with watched(client) as watch:
                answer = streamed(client, path, chunks, headers, then=then)  # type: ignore[arg-type]
            results[name] = (answer, watch)
            check_refused(press, answer, subject=subject, typed=typed, family=family)
            check_untouched(watch, press, subject)
        after = everything_kept(client)

    assert after == before
    for name in (
        "one byte over, declared",
        "no length",
        "a length of letters",
        "a negative length",
        "a signed length",
        "a length with space",
        "a hexadecimal length",
        "a length of another script's digits",
        "a length of superscript digits",
        "a length of five thousand digits",
        "two lengths",
        "a list of lengths",
        "a length beside a transfer coding",
        "a length beside a content coding",
        "multipart",
        "plain text",
        "two content types",
    ):
        answer, watch = results[name]
        assert answer.pulled == 0, name
        assert watch.copied == [], name
    for name in ("one byte over, streamed past a declared 16 KiB", "a stream that never ends"):
        answer, watch = results[name]
        assert answer.pulled <= LIMIT + 1024 + 1, (name, answer.pulled)
        assert watch.copied == [], name
    assert results["exactly 16 KiB"][1].copied == [LIMIT]
    assert results["exactly 16 KiB in 16 chunks"][1].copied == [LIMIT]
    assert [record for record in caplog.records if "Wren" in record.getMessage()] == []


@pytest.mark.parametrize("press", NOTE_PRESSES)
def test_the_copy_keeps_the_first_value_and_says_undecodable_bytes_as_a_replacement(
    press: str, tmp_path: pathlib.Path
) -> None:
    with household(tmp_path, "parent") as client:
        seeded = seed(client)
        subject = subject_of(press, "known", seeded)
        form = valid_form(press, "page", seeded)
        shown = SHOWN[press]
        twice = urlencode([*form, (shown, "second <i>value</i>")])
        mangled = urlencode([(name, value) for name, value in form if name != shown])
        mangled += f"&{shown}=Wren%FF%FEs+words"
        first = sent(client, press_path(press, subject), twice.encode(), URL_ENCODED)
        replaced = sent(client, press_path(press, subject), mangled.encode(), URL_ENCODED)

    check_refused(press, first, subject=subject, typed=TYPED[press])
    check_refused(press, replaced, subject=subject, typed="Wren\ufffd\ufffds words")


COPIED_DAYS: Final[dict[str, tuple[str, list[tuple[str, str]]]]] = {
    "a day as a page writes one": ("2026-08-21", []),
    "a day in another spelling": ("20260819", []),
    "a day in another spelling beside the tick": (
        "20260819",
        [("date_pending", "1"), ("without_date", "1")],
    ),
    "words that are no day": ("next Wednesday", []),
    "words past the length a page says back": ("x" * 240 + "TAIL", []),
    "a day with spaces around it": ("  2026-08-21 ", []),
    "words on two lines": ("Friday\nor Monday", []),
    "markup": ("<b>Friday</b> & Wren's", []),
}
"""Due dates a refused press may send, each with the fields sent beside it."""
TREES: Final = ("hers, by a parent", "the family's, by her")


def refused_copy(
    tmp_path: pathlib.Path, tree: str, press: str, form: list[tuple[str, str]]
) -> tuple[Answer, str]:
    """A refused details or add press with this form, and the id it named."""
    family = tree == TREES[1]
    with household(tmp_path, "her" if family else "parent") as client:
        seeded = seed(client)
        subject = subject_of(press, "known", seeded)
        path = press_path(press, subject, family=family)
        return sent(client, path, urlencode(form).encode(), URL_ENCODED), subject


@pytest.mark.parametrize("given", list(COPIED_DAYS))
@pytest.mark.parametrize("press", ["details", "add"])
@pytest.mark.parametrize("tree", TREES)
def test_the_copy_keeps_the_due_date_as_it_was_sent(
    tree: str, press: str, given: str, tmp_path: pathlib.Path
) -> None:
    due, beside = COPIED_DAYS[given]
    form = [("title", "Copy test"), ("due_date", due), *beside]
    answer, subject = refused_copy(tmp_path, tree, press, form)

    check_refused(press, answer, subject=subject, typed="Copy test", family=tree == TREES[1])
    page = main_of(answer.text)
    assert f'Due date, as given: <span class="authored-text">{escape(due)}</span>' in page
    assert page.count("Due date, as") == 1


@pytest.mark.parametrize(
    "carried",
    ["next Wednesday", "x" * 240 + "TAIL", "2026-08-21", "Friday\nor Monday"],
    ids=["words", "words past the length a page says back", "a day", "words on two lines"],
)
@pytest.mark.parametrize("press", ["details", "add"])
@pytest.mark.parametrize("tree", TREES)
def test_the_copy_keeps_the_day_words_a_page_carried_as_they_were_sent(
    tree: str, press: str, carried: str, tmp_path: pathlib.Path
) -> None:
    form = [
        ("title", "Copy test"),
        ("date_pending", "1"),
        ("date_refused", carried),
        ("due_date", "2026-08-22"),
    ]
    answer, subject = refused_copy(tmp_path, tree, press, form)

    check_refused(press, answer, subject=subject, typed="Copy test", family=tree == TREES[1])
    page = main_of(answer.text)
    assert f'Due date, as typed: <q class="authored-text">{escape(carried)}</q>.</p>' in page
    assert 'Due date, as given: <span class="authored-text">2026-08-22</span>' in page
    assert "which is not a date" not in page


@pytest.mark.parametrize("press", ["details", "add"])
@pytest.mark.parametrize("tree", TREES)
def test_the_copy_keeps_every_typed_value_as_it_was_sent(
    tree: str, press: str, tmp_path: pathlib.Path
) -> None:
    typed = {
        "course_choice": " Art ",
        "course_other": " " + "c" * 80 + " ",
        "title": "  " + "t" * 250 + "  ",
        "note": " " + "n" * 600 + "\n\tlast\n",
    }
    chosen = [("kind", "HOMEWORK"), ("candidate", "separate")]
    answer, subject = refused_copy(tmp_path, tree, press, [*typed.items(), *chosen])

    check_refused(press, answer, subject=subject, typed=typed["title"], family=tree == TREES[1])
    page = main_of(answer.text)
    assert f'Class, as chosen: <span class="authored-text">{typed["course_choice"]}</span>' in page
    assert f'Class, as typed: <span class="authored-text">{typed["course_other"]}</span>' in page
    assert f'value="{typed["title"]}" readonly' in page
    assert f"readonly>{typed['note']}</textarea>" in page
    assert "Kind, as chosen: Homework" in page
    assert "Choice, as made: keep it as a separate assignment." in page


# ------------------------------------------------------------------ 5. her, and nobody known


def pressed(client: TestClient, path: str, form: list[tuple[str, str]]) -> Answer:
    return sent(client, path, urlencode(form).encode(), URL_ENCODED)


@pytest.mark.parametrize("reader", ["her", "open"])
def test_her_presses_and_the_open_households_keep_their_answers(
    reader: Reader, tmp_path: pathlib.Path
) -> None:
    """A field twice, an id that names nothing and an id of no id's shape keep the answers
    they had, the form read first; a misshapen note id is answered 404 before the form is
    judged; her words are kept on 422 and 409; a save goes back where the form came from."""
    with household(tmp_path, reader) as client:
        seeded = seed(client)
        week_report = valid_form("report", "week", seeded)
        details_report = valid_form("report", "details", seeded)
        with watched(client) as watch:
            twice = pressed(
                client, press_path("report", ESSAY_ID), [*week_report, ("note", "second")]
            )
        gone = pressed(client, press_path("report", NOWHERE), details_report)
        misshapen_edit = pressed(
            client,
            press_path("edit", NOT_A_NOTE),
            [*valid_form("edit", "page", seeded), ("text", "second")],
        )
        misshapen_details = pressed(
            client, press_path("details", NOT_A_NOTE), valid_form("details", "page", seeded)
        )
        misshapen_link = pressed(
            client, press_path("link", NOT_A_NOTE), valid_form("link", "page", seeded)
        )
        unknown_archive = pressed(
            client, press_path("archive", new_capture_id()), [("revision", "1")]
        )
        stale = pressed(
            client,
            press_path("report", ESSAY_ID),
            [(name, "" if name == "expected_report_id" else value) for name, value in week_report],
        )
        from_details = pressed(
            client,
            press_path("report", ESSAY_ID),
            [(name, value if name != "status" else "not_yet") for name, value in details_report],
        )
        head = store_of(client).student_reports(ESSAY_ID)[-1].report_id
        from_week = pressed(
            client,
            press_path("report", ESSAY_ID),
            [
                (name, head if name == "expected_report_id" else value)
                for name, value in week_report
            ],
        )
        handed = store_of(client).hand_in_readings([ESSAY_ID]).readable[ESSAY_ID].head_id
        from_list = pressed(
            client,
            press_path("hand-in", ESSAY_ID),
            [
                ("state", "turned_in"),
                ("next_action", ""),
                ("note", ""),
                ("expected_hand_in_id", handed or ""),
                ("hand_in_view", "list"),
            ],
        )

    assert "student.fields_of" in watch.read
    assert twice.status_code == 422
    assert "That form carried a field twice" in twice.text
    assert gone.status_code == 404
    assert "This assignment is not on record now." in gone.text
    assert misshapen_edit.status_code == 404
    assert "This homework note is not on record." in misshapen_edit.text
    assert misshapen_details.status_code == 404
    assert misshapen_link.status_code == 404
    assert unknown_archive.status_code == 404
    assert stale.status_code == 409
    assert str(escape("Wren's <b>update</b>")) in stale.text
    assert from_details.status_code == 303
    assert from_details.headers["location"].startswith(f"/student/assignments/{ESSAY_ID}")
    assert from_week.status_code == 303
    assert from_week.headers["location"].startswith("/student/due-this-week")
    assert from_list.status_code == 303
    assert from_list.headers["location"].startswith(TO_TURN_IN_PAGE)


@pytest.mark.parametrize("reader", ["her", "open"])
@pytest.mark.parametrize("press", EVERY_PRESS)
def test_her_press_and_the_open_households_read_the_form_first(
    reader: Reader, press: str, tmp_path: pathlib.Path
) -> None:
    """For her and with the sign-in off every press reads its whole form, as it did, and a
    form with a field twice is answered 422 or, for a note named by no id, 404."""
    with household(tmp_path, reader) as client:
        seeded = seed(client)
        form = valid_form(press, ORIGINS.get(press, ("page",))[0], seeded)
        with watched(client) as watch:
            answer = pressed(
                client,
                press_path(press, subject_of(press, "known", seeded)),
                [*form, (SHOWN[press], "second")],
            )

    assert [name for name in watch.read if name.endswith(".fields_of")] != []
    assert "kept_fields_of" not in watch.read
    assert answer.status_code in (404, 422), (press, answer.status_code)


# ------------------------------------------------------------------ 6. the gate


@pytest.mark.parametrize("press", EVERY_PRESS)
@pytest.mark.parametrize("sign_in", ["absent", "aged out", "another origin"])
def test_the_gate_answers_before_any_route_and_never_reaches_the_copy(
    sign_in: str, press: str, tmp_path: pathlib.Path
) -> None:
    """A press with no sign-in, or one that aged out, is the gate's: a page is sent to the
    sign-in and a call is answered 401. A press from another origin is the gate's 403 in
    plain text. No route runs, nothing is read and nothing is written."""
    with household(tmp_path, "parent") as client:
        seeded = seed(client)
        path = press_path(press, subject_of(press, "known", seeded))
        body = urlencode(valid_form(press, ORIGINS.get(press, ("page",))[0], seeded)).encode()
        headers = {"Content-Type": URL_ENCODED}
        if sign_in != "another origin":
            client.cookies.clear()
        if sign_in == "aged out":
            keys = client.app.state.household_keys  # type: ignore[attr-defined]
            then = datetime.now(UTC) - timedelta(seconds=SESSION_SECONDS + 60)
            headers["Cookie"] = f"{COOKIE}={issue(Principal.PARENT, keys[Principal.PARENT], then)}"
        if sign_in == "another origin":
            headers["Origin"] = "http://elsewhere.example"
        before = everything_kept(client)
        with watched(client) as watch:
            page = client.post(path, content=body, headers={**PAGE_HEADERS, **headers})
            call = client.post(path, content=body, headers=headers)
        after = everything_kept(client)

    assert watch.read == []
    assert watch.statements == []
    assert after == before
    if sign_in == "another origin":
        for answer in (page, call):
            assert answer.status_code == 403
            assert answer.text == "This request must come from the same origin."
        return
    assert page.status_code == 303
    assert page.headers["location"] == "/sign-in"
    assert call.status_code == 401


@pytest.mark.parametrize("press", NOTE_PRESSES)
def test_her_press_on_the_family_tree_from_another_origin_is_the_gates(
    press: str, tmp_path: pathlib.Path
) -> None:
    with household(tmp_path, "her") as client:
        seeded = seed(client)
        path = press_path(press, subject_of(press, "known", seeded), family=True)
        body = urlencode(valid_form(press, "page", seeded)).encode()
        with watched(client) as watch:
            answer = client.post(
                path,
                content=body,
                headers={
                    **PAGE_HEADERS,
                    "Content-Type": URL_ENCODED,
                    "Origin": "http://elsewhere.example",
                },
            )

    assert answer.status_code == 403
    assert answer.text == "This request must come from the same origin."
    assert watch.read == []


# ------------------------------------------------------------------ 7. the family's tree


@pytest.mark.parametrize("press", NOTE_PRESSES)
@pytest.mark.parametrize("kind", ["known", "unknown", "misshapen"])
def test_her_press_on_the_family_tree_is_refused_as_a_parents_is_on_hers(
    kind: str, press: str, tmp_path: pathlib.Path
) -> None:
    with household(tmp_path, "her") as client:
        seeded = seed(client)
        subject = subject_of(press, kind, seeded)
        path = press_path(press, subject, family=True)
        form = valid_form(press, "page", seeded)
        before = everything_kept(client)
        for body_name, (body, content_type) in bodies(press, form).items():
            with watched(client) as watch:
                answer = sent(client, path, body, content_type)
            typed = dict(form)[SHOWN[press]] if copy_made(press, body_name) else None
            check_refused(press, answer, subject=subject, typed=typed, family=True)
            check_untouched(watch, press, subject)
            assert "Back to my week" in answer.text
        after = everything_kept(client)

    assert after == before


def attribution_of(client: TestClient, name: str) -> dict[str, tuple[str, str]]:
    note = store_of(client).capture(name)
    assert note is not None
    return {
        detail: (source.authored_by, source.channel.value)
        for detail, source in note.attribution.items()
    }


@pytest.mark.parametrize("reader", ["parent", "open"])
def test_the_family_tree_keeps_its_own_workflow_for_a_parent_and_the_open_household(
    reader: Reader, tmp_path: pathlib.Path
) -> None:
    """A parent's press on the family's tree is the family's clarification: the whole form
    first, the note by its id, a field twice 422, a note that names nothing 404, one of no
    id's shape 404, and a save recorded as a family entry by the one who pressed."""
    with household(tmp_path, reader) as client:
        seeded = seed(client)
        details = valid_form("details", "page", seeded)
        with watched(client) as watch:
            twice = pressed(
                client,
                press_path("details", seeded.waiting, family=True),
                [*details, ("title", "second")],
            )
        unknown = pressed(client, press_path("details", new_capture_id(), family=True), details)
        misshapen = pressed(client, press_path("add", NOT_A_NOTE, family=True), details)
        misshapen_unlink = pressed(
            client,
            press_path("unlink", NOT_A_NOTE, family=True),
            [("revision", "2"), ("from", "x")],
        )
        saved = pressed(
            client,
            press_path("details", seeded.waiting, family=True),
            [(name, "Questions 4-8" if name == "title" else value) for name, value in details],
        )
        attribution = attribution_of(client, seeded.waiting)
        unlinked = pressed(
            client,
            press_path("unlink", seeded.joined, family=True),
            [("revision", "2"), ("from", seeded.homework)],
        )
        still_joined = store_of(client).capture(seeded.joined)

    assert "note_details.fields_of" in watch.read
    assert "kept_fields_of" not in watch.read
    assert watch.looked_up != []
    assert twice.status_code == 422
    assert "That form carried a field twice" in twice.text
    assert unknown.status_code == 404
    assert misshapen.status_code == 404
    assert misshapen_unlink.status_code == 404
    assert saved.status_code == 303, saved.text[:300]
    who = PARENT if reader == "parent" else "household"
    assert attribution["title"] == (who, SourceChannel.PARENT_ENTRY.value)
    assert unlinked.status_code == 303, unlinked.text[:300]
    assert still_joined is not None
    assert still_joined.assignment_id is None


@pytest.mark.parametrize("reader", ["her", "open"])
def test_her_tree_keeps_her_workflow_for_her_and_the_open_household(
    reader: Reader, tmp_path: pathlib.Path
) -> None:
    with household(tmp_path, reader) as client:
        seeded = seed(client)
        details = valid_form("details", "page", seeded)
        saved = pressed(
            client,
            press_path("details", seeded.waiting),
            [(name, "Questions 4-8" if name == "title" else value) for name, value in details],
        )
        attribution = attribution_of(client, seeded.waiting)

    who = STUDENT if reader == "her" else "household"
    assert saved.status_code == 303, saved.text[:300]
    assert attribution["title"] == (who, SourceChannel.STUDENT_REPORT.value)


# ------------------------------------------------------------------ 8. her current week


def gone(client: TestClient) -> None:
    connection = store_of(client)._connection
    connection.execute("DELETE FROM assignments WHERE assignment_id = ?", (ESSAY_ID,))
    connection.commit()


ELSEWHERE_RETURNS: Final[dict[str, list[tuple[str, str]]]] = {
    "the family page": [("return_to", "family"), ("week", ""), ("plan_id", "plan-elsewhere")],
    "another week": [("return_to", "week"), ("week", "2026-09-14"), ("plan_id", "")],
    "To turn in": [("return_to", "to_turn_in"), ("week", ""), ("plan_id", "")],
    "a page no one makes": [
        ("return_to", "https://elsewhere.example"),
        ("week", "x"),
        ("plan_id", ""),
    ],
}


@pytest.mark.parametrize("press", ASSIGNMENT_PRESSES)
@pytest.mark.parametrize("kind", ["known", "unknown", "gone"])
@pytest.mark.parametrize("pointing", list(ELSEWHERE_RETURNS))
def test_a_parents_update_hand_in_or_undo_lands_on_her_current_week(
    pointing: str, kind: str, press: str, tmp_path: pathlib.Path
) -> None:
    """Whatever page the form came from and whatever way back it names, a parent's press
    lands on her current week with 403 and the sentence that says who may update. No way
    back is read and the assignment it names is not looked up, on record, not on record, or
    taken off since."""
    with household(tmp_path, "parent") as client:
        seeded = seed(client)
        subject = ESSAY_ID if kind != "unknown" else NOWHERE
        if kind == "gone":
            gone(client)
        base = [
            (name, value)
            for name, value in valid_form(press, ORIGINS[press][0], seeded)
            if name not in ("return_to", "week", "plan_id")
        ]
        form = [*base, *ELSEWHERE_RETURNS[pointing]]
        if press in ("report", "undo-report"):
            form.append(("report_view", "detail"))
        before = everything_kept(client)
        with watched(client) as watch:
            answer = pressed(client, press_path(press, subject), form)
        after = everything_kept(client)

    check_refused(press, answer, subject=subject, typed=None)
    check_untouched(watch, press, subject)
    assert "Week of September 14" not in answer.text
    assert after == before


# ------------------------------------------------------------------ 5. the copy's deadline


SHORT_DEADLINE: Final = 0.4
"""The deadline these tests give the copy, so a body that stalls is seen out quickly. The
application's own is pinned below as it is, and a real server is timed against it apart."""
CLOCK_TICK: Final = 0.05
"""How early an event loop may run a timer: it runs every timer due within its clock's
resolution, which is about 16 milliseconds on Windows."""


@dataclass(frozen=True)
class Paced:
    """What the application answered a body that arrived at its own pace."""

    status_code: int
    text: str
    headers: dict[str, str]
    answered_after: float
    """Seconds from the press to the start of the answer."""
    reads_left_waiting: int
    """Reads of the body still waiting when the application finished: none, when nothing
    goes on reading after the answer."""
    said_gone: bool
    """Whether the client was ever said to have gone."""


def press_scope(client: TestClient, path: str, headers: list[tuple[str, str | bytes]]) -> Scope:
    """A signed-in press on this client, as a server hands it to the application."""
    cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"origin", b"http://testserver"),
            (b"accept", b"text/html"),
            (b"cookie", cookie.encode("latin-1")),
            *[(name.lower().encode("latin-1"), raw(value)) for name, value in headers],
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }


def paced(
    client: TestClient,
    path: str,
    steps: list[tuple[float, bytes]],
    headers: list[tuple[str, str | bytes]],
    *,
    then: Literal["end", "stay", "disconnect"],
) -> Paced:
    """Send a press straight to the application, each chunk of its body after its own pause,
    then the end of the body, nothing more with the connection kept open, or the client
    going away. Time the answer, and count the reads still waiting when it is done."""
    scope = press_scope(client, path, headers)
    queue = list(steps)

    async def run() -> Paced:
        waiting = 0
        gone = False
        status = 0
        said: dict[str, str] = {}
        answer = bytearray()
        started = time.monotonic()
        answered = -1.0

        async def receive() -> Message:
            nonlocal waiting, gone
            waiting += 1
            try:
                if queue:
                    pause, chunk = queue.pop(0)
                    await asyncio.sleep(pause)
                    last = not queue and then == "end"
                    return {"type": "http.request", "body": chunk, "more_body": not last}
                if then == "disconnect":
                    gone = True
                    return {"type": "http.disconnect"}
                await asyncio.Event().wait()
                raise AssertionError  # pragma: no cover
            finally:
                waiting -= 1

        async def send(message: Message) -> None:
            nonlocal status, answered
            if message["type"] == "http.response.start":
                status = message["status"]
                answered = time.monotonic() - started
                said.update(
                    (name.decode("latin-1"), value.decode("latin-1"))
                    for name, value in message.get("headers", [])
                )
            elif message["type"] == "http.response.body":
                answer.extend(message.get("body", b""))

        await client.app(scope, receive, send)
        return Paced(status, answer.decode("utf-8"), said, answered, waiting, gone)

    portal = client.portal
    assert portal is not None
    return portal.call(run)


def trickled(body: bytes, pieces: int, pause: float) -> list[tuple[float, bytes]]:
    return [(pause, piece) for piece in in_pieces(body, pieces)]


def deadline_cases(
    press: str, seeded: Seeded
) -> dict[str, tuple[list[tuple[float, bytes]], str, str | None]]:
    """Each pace a body of 600 bytes can come at, how the connection ends, and the words the
    copy is to show, or ``None`` when no copy is to be made."""
    small = sized(press, seeded, 600)
    return {
        "no body arrives": ([], "stay", None),
        "a part arrives, then nothing": ([(0.0, small[:300])], "stay", None),
        "it trickles in past the deadline": (trickled(small, 6, SHORT_DEADLINE / 3), "end", None),
        "it arrives whole within the deadline": (
            trickled(small, 3, SHORT_DEADLINE / 8),
            "end",
            TYPED[press],
        ),
        "the client goes away": ([(0.0, small[:300])], "disconnect", None),
    }


@pytest.mark.parametrize("press", NOTE_PRESSES)
@pytest.mark.parametrize("tree", ["hers, by a parent", "the family's, by her"])
def test_the_refusal_copy_is_given_up_at_its_deadline(
    tree: str, press: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy has one deadline, counted from the moment its body starts being read and
    never restarted by a chunk that arrives. A body that never comes, stops partway, or is
    still arriving when the deadline passes gives the same 403 without a copy, answered at
    the deadline while the client is still there, with nothing left reading. A body whole
    within the deadline keeps its copy. No store is read and the lock is not taken."""
    monkeypatch.setattr(form_routes, "COPY_DEADLINE", SHORT_DEADLINE)
    family = tree == "the family's, by her"
    with household(tmp_path, "her" if family else "parent") as client:
        seeded = seed(client)
        subject = subject_of(press, "known", seeded)
        path = press_path(press, subject, family=family)
        small = sized(press, seeded, 600)
        headers = [FORM_HEADER, ("content-length", str(len(small)))]
        before = everything_kept(client)
        results: dict[str, Paced] = {}
        for name, (steps, then, typed) in deadline_cases(press, seeded).items():
            with watched(client) as watch:
                answer = paced(client, path, steps, headers, then=then)  # type: ignore[arg-type]
            results[name] = answer
            check_refused(press, answer, subject=subject, typed=typed, family=family)
            check_untouched(watch, press, subject)
            assert answer.reads_left_waiting == 0, name
            assert watch.copied == ([] if typed is None else [600]), (name, watch.copied)
        after = everything_kept(client)

    assert after == before
    for name in (
        "no body arrives",
        "a part arrives, then nothing",
        "it trickles in past the deadline",
    ):
        answer = results[name]
        assert not answer.said_gone, name
        assert SHORT_DEADLINE - CLOCK_TICK <= answer.answered_after < SHORT_DEADLINE + 1.0, (
            name,
            answer.answered_after,
        )
    assert (
        results["it arrives whole within the deadline"].answered_after < SHORT_DEADLINE - CLOCK_TICK
    )
    assert results["the client goes away"].said_gone
    assert results["the client goes away"].answered_after < SHORT_DEADLINE


def test_the_copy_deadline_is_five_seconds() -> None:
    assert form_routes.COPY_DEADLINE == 5.0


def test_a_press_canceled_while_its_copy_is_read_stays_canceled(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline gives up the copy on its own time and nothing else: when the request
    itself is canceled from outside while the copy is being read, it stays canceled: it
    ends by the cancel it was given, a ``BaseException`` and no ``Exception``, and no answer
    is started for it."""
    monkeypatch.setattr(form_routes, "COPY_DEADLINE", 30.0)
    with household(tmp_path, "parent") as client:
        seeded = seed(client)
        subject = subject_of("details", "known", seeded)
        scope = press_scope(
            client, press_path("details", subject), [FORM_HEADER, ("content-length", "600")]
        )

        async def run() -> tuple[object, list[str]]:
            sent: list[str] = []
            reading = asyncio.Event()

            async def receive() -> Message:
                reading.set()
                await asyncio.Event().wait()
                raise AssertionError  # pragma: no cover

            async def send(message: Message) -> None:
                sent.append(message["type"])

            task = asyncio.ensure_future(client.app(scope, receive, send))
            await asyncio.wait_for(reading.wait(), 5)
            task.cancel()
            (ended,) = await asyncio.gather(task, return_exceptions=True)
            return ended, sent

        portal = client.portal
        assert portal is not None
        ended, sent = portal.call(run)

    assert isinstance(ended, BaseException)
    assert not isinstance(ended, Exception)
    assert sent == []
