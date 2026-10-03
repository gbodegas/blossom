# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A homework note's pages when a read of the household's file fails, and the refusals that
meet a note that can't be read.

A note's page that is only read answers 503 when the file can't be read, says what can't be
shown, and offers the same address again; a note whose row can't be decoded keeps its 200 and
its own sentence. The note page reads what it needs first and asks whether a note can be
deleted last, so a check that fails alone hides Delete and says so on a page that stands. A
refusal whose page can't be read keeps its status and says itself without that page. The
fixture week through the app, notes made through the store, and a failure made at the store
call or a second connection holding the file.
"""

import pathlib
import re
import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.captures import (
    STUDENT,
    CaptureCreated,
    CaptureNotSaved,
    UnknownCapture,
    UnreadableCapture,
)
from blossom.hand_in import NEEDS_HAND_IN
from blossom.reconciliation import SourceChannel
from blossom.routes import note_details as details_routes
from blossom.routes import note_links as link_routes
from blossom.routes.navigation import (
    note_action,
    note_add_action,
    note_add_href,
    note_delete_href,
    note_details_action,
    note_help_href,
    note_href,
    note_link_action,
    note_search_href,
    note_unlink_action,
    week_href,
)
from tests.support import (
    NOTE_AT,
    NOTE_DAY,
    PAGE_HEADERS,
    Answer,
    HeldByAnother,
    Statements,
    after_the_failure,
    browser,
    database_of,
    every_row,
    form_fields,
    household_client,
    link_note,
    main_of,
    promote_note,
    refusing,
    rules_named,
    sign_in_as,
    spy_on_stores,
    state_of,
    store_free_page,
    ways_back_of,
)

NOTE_UNAVAILABLE = "This homework note can't be shown right now. Try again in a moment. Try again"
REQUEST_UNCHECKED = (
    "This homework note can't be shown right now, so that request can't be checked here. "
    "Check your requests on your week before you ask again. Try again"
)
DELETE_UNCHECKED = "Whether this note can be deleted can't be checked right now."
DELETE_UNAVAILABLE = (
    "Whether this note can be deleted can't be checked right now, so it can't be deleted yet. "
    "Try again in a moment. Try again"
)
NOTE_CANNOT_BE_READ = "This homework note cannot be read right now."
NOTE_UNREADABLE = "This homework note cannot be read right now. Nothing was changed."
NOTE_NOT_SHOWN = "This homework note can't be shown right now."
FORM_NOT_SHOWN = "The form can't be shown right now. What was typed is still here."
SEARCH_NOT_SHOWN = "The search can't be shown right now. Your search words are still here."
NOT_SAVED = "That could not be saved, and nothing was changed. What was typed is still here."
NOT_A_PARENTS_PRESS = "Sign in as a parent to add details here. Nothing was saved."
TYPED = "Kept <b>words</b>\nand a second line"
READS = [("OperationalError", sqlite3.OperationalError), ("DatabaseError", sqlite3.DatabaseError)]


def a_note(client: TestClient, text: str = "Geometry 4-8") -> str:
    """A note of hers that waits, made through the store; its id."""
    name = str(uuid4())
    made = state_of(client).project_state.create_capture(
        name,
        text,
        None,
        None,
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=NOTE_AT,
        today=NOTE_DAY,
    )
    assert isinstance(made, CaptureCreated)
    return name


def in_homework(client: TestClient) -> str:
    """A note of hers added to homework as homework of its own; its id."""
    name = a_note(client)
    promote_note(state_of(client).project_state, name)
    return name


def revision_of(client: TestClient, name: str) -> int:
    found = state_of(client).project_state.sound_capture_history(name)
    assert found is not None
    return found[0].revision


def damage(client: TestClient, name: str) -> None:
    """Leave a note's row as nothing the store writes, so it can't be decoded."""
    store = state_of(client).project_state
    store._connection.execute(
        "UPDATE homework_captures SET attribution = 'no json' WHERE capture_id = ?", (name,)
    )
    store._connection.commit()


def small_page(answer: Answer, *, status: int, alert: str) -> str:
    """The small note page, which reads no store; its main part."""
    return store_free_page(
        answer, status=status, heading="Homework note", alert=alert, alert_id="note-problem"
    )


def try_again(main: str) -> str:
    found = re.search(r'<a href="([^"]*)">Try again</a>', main)
    assert found is not None
    return found.group(1)


def week_link(reader: str, fragment: str = "") -> tuple[str, str]:
    where = "/student/due-this-week" + (f"#{fragment}" if fragment else "")
    return where, "Back to her week" if reader == "parent" else "Back to my week"


# ------------------------------------------------------------- the note page, only read


@pytest.mark.parametrize(("kind_name", "kind"), READS)
@pytest.mark.parametrize("reader", ["her", "parent", "open"])
def test_a_note_in_homework_whose_homework_cannot_be_read_says_so_with_a_way_to_ask_again(
    reader: str,
    kind_name: str,
    kind: type[Exception],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del kind_name
    calls: Counter[str] = Counter()
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, "her" if reader == "parent" else reader)
        name = in_homework(client)
        sign_in_as(client, reader)
        store = state_of(client).project_state
        real: Callable[..., object] = store.capture_use

        def counted(*given: object, **named: object) -> object:
            calls["capture_use"] += 1
            return real(*given, **named)

        monkeypatch.setattr(store, "capture_use", counted)
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(store, "one_assignment", refusing(kind, seen))
            answer = client.get(note_href(name) + "?said=anything&x=", headers=PAGE_HEADERS)
        monkeypatch.undo()
        readable = client.get(note_href(name), headers=PAGE_HEADERS)

    main = small_page(answer, status=503, alert=NOTE_UNAVAILABLE)
    assert try_again(main) == escape(note_href(name) + "?said=anything&x=")
    assert ways_back_of(main) == [
        ("/student/homework-notes/added", "Back to notes added to homework"),
        week_link(reader),
    ]
    assert calls["capture_use"] == 0
    assert after_the_failure(seen) == []
    assert readable.status_code == 200


@pytest.mark.parametrize(("kind_name", "kind"), READS)
@pytest.mark.parametrize("reader", ["her", "parent", "open"])
def test_a_note_that_cannot_be_read_is_503_and_a_note_that_cannot_be_decoded_keeps_its_200(
    reader: str,
    kind_name: str,
    kind: type[Exception],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del kind_name
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, "her" if reader == "parent" else reader)
        name = a_note(client)
        sign_in_as(client, reader)
        store = state_of(client).project_state
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(store, "sound_capture_history", refusing(kind, seen))
            unread = client.get(note_href(name), headers=PAGE_HEADERS)
        monkeypatch.setattr(store, "sound_capture_history", refusing(UnreadableCapture))
        damaged = client.get(note_href(name), headers=PAGE_HEADERS)

    main = small_page(unread, status=503, alert=NOTE_UNAVAILABLE)
    assert try_again(main) == note_href(name)
    assert after_the_failure(seen) == []
    small_page(damaged, status=200, alert=NOTE_CANNOT_BE_READ)
    assert "Try again" not in main_of(damaged.text)


def test_a_request_the_note_page_cannot_check_sends_her_to_help_and_asks_nothing_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """She asks for help about a note; the page the ask lands on can't read the request. The
    page says the request can't be checked and sends her to Help on her week; the same form
    sent again lands on the request it made, and no second request is written."""
    with browser() as client:
        name = a_note(client)
        page = client.get(note_help_href(name), headers=PAGE_HEADERS).text
        fields = form_fields(page, note_action(name, "ask-for-help"))
        asked = client.post(note_action(name, "ask-for-help"), data={**fields, "note": "which?"})
        assert asked.status_code == 303
        landed = asked.headers["location"]
        help_requests = state_of(client).help_requests
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(help_requests, "get", refusing(sqlite3.OperationalError, seen))
            answer = client.get(landed, headers=PAGE_HEADERS)
        monkeypatch.undo()
        again = client.post(note_action(name, "ask-for-help"), data={**fields, "note": "which?"})
        requests = help_requests.open_requests()

    main = small_page(answer, status=503, alert=REQUEST_UNCHECKED)
    assert try_again(main) == escape(landed.split("#", 1)[0])
    assert ways_back_of(main) == [
        ("/student/homework-notes", "Back to Homework notes"),
        week_link("her", "help"),
    ]
    assert "You asked for help" not in main
    assert after_the_failure(seen) == []
    assert again.status_code == 303
    assert "asked_again=" in again.headers["location"]
    assert len(requests) == 1


def test_an_address_naming_a_request_typed_by_hand_is_answered_as_after_a_real_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        name = a_note(client)
        monkeypatch.setattr(state_of(client).help_requests, "get", refusing(sqlite3.DatabaseError))
        answer = client.get(note_href(name) + "?asked=" + "0" * 32, headers=PAGE_HEADERS)

    small_page(answer, status=503, alert=REQUEST_UNCHECKED)


# ------------------------------------------------------------- the Delete check, last and alone


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        ("OperationalError", sqlite3.OperationalError),
        ("DatabaseError", sqlite3.DatabaseError),
        ("UnreadableCapture", UnreadableCapture),
    ],
)
@pytest.mark.parametrize("note_kind", ["waiting", "in homework", "archived"])
@pytest.mark.parametrize("reader", ["her", "open"])
def test_when_only_the_delete_check_fails_the_page_stands_without_delete_and_says_so(
    reader: str,
    note_kind: str,
    failure: str,
    kind: type[Exception],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del failure
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        name = in_homework(client) if note_kind == "in homework" else a_note(client)
        if note_kind == "archived":
            archived = client.post(note_action(name, "archive"), data={"revision": "1"})
            assert archived.status_code == 303
        store = state_of(client).project_state
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(store, "capture_use", refusing(kind, seen))
            answer = client.get(note_href(name), headers=PAGE_HEADERS)
        monkeypatch.undo()
        readable = client.get(note_href(name), headers=PAGE_HEADERS)

    assert answer.status_code == 200
    main = main_of(answer.text)
    assert "Delete this note" not in main
    assert note_delete_href(name) not in main
    assert main.count(DELETE_UNCHECKED.replace("'", "&#39;")) == 1
    assert "History of this note" in main
    assert "autofocus" not in main
    assert after_the_failure(seen) == []
    assert DELETE_UNCHECKED.replace("'", "&#39;") not in readable.text
    assert ("Delete this note" in readable.text) == (note_kind != "in homework")


def test_a_parent_is_never_told_whether_her_note_can_be_deleted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: Counter[str] = Counter()
    with household_client("parent", tmp_path) as client:
        sign_in_as(client, "her")
        name = a_note(client)
        sign_in_as(client, "parent")
        store = state_of(client).project_state

        def counted(*_: object, **__: object) -> None:
            calls["capture_use"] += 1
            msg = "database is locked"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "capture_use", counted)
        answer = client.get(note_href(name), headers=PAGE_HEADERS)

    assert answer.status_code == 200
    assert DELETE_UNCHECKED.replace("'", "&#39;") not in answer.text
    assert "Delete this note" not in answer.text
    assert calls["capture_use"] == 0


def test_a_note_used_before_or_saved_before_deleting_existed_is_never_offered_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        legacy = a_note(client)
        store._connection.execute(
            "INSERT INTO captures_of_unknown_use (capture_id) VALUES (?)", (legacy,)
        )
        store._connection.commit()
        used = in_homework(client)
        pages = {}
        for name in (legacy, used):
            monkeypatch.setattr(store, "capture_use", refusing(sqlite3.OperationalError))
            pages[name] = client.get(note_href(name), headers=PAGE_HEADERS)
            monkeypatch.undo()

    for page in pages.values():
        assert page.status_code == 200
        assert "Delete this note" not in page.text
        assert DELETE_UNCHECKED.replace("'", "&#39;") in page.text


def test_a_note_deleted_in_another_tab_before_the_check_is_gone_with_none_of_its_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        name = a_note(client, "Only the words of this note")
        store = state_of(client).project_state

        def deleted_meanwhile(*_: object, **__: object) -> None:
            raise UnknownCapture(name)

        monkeypatch.setattr(store, "capture_use", deleted_meanwhile)
        answer = client.get(note_href(name), headers=PAGE_HEADERS)

    assert answer.status_code == 404
    assert "Only the words of this note" not in answer.text


# ------------------------------------------------------------- the delete page and the help page


@pytest.mark.parametrize(
    ("read", "kind", "status", "alert"),
    [
        ("capture_use", sqlite3.OperationalError, 503, DELETE_UNAVAILABLE),
        ("capture_use", sqlite3.DatabaseError, 503, DELETE_UNAVAILABLE),
        ("capture_use", UnreadableCapture, 200, NOTE_CANNOT_BE_READ),
        ("sound_capture_history", sqlite3.OperationalError, 503, NOTE_UNAVAILABLE),
        ("sound_capture_history", UnreadableCapture, 200, NOTE_CANNOT_BE_READ),
    ],
)
def test_the_page_that_asks_before_deleting_is_503_only_when_the_file_cannot_be_read(
    read: str, kind: type[Exception], status: int, alert: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser() as client:
        name = a_note(client)
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(state_of(client).project_state, read, refusing(kind, seen))
            answer = client.get(note_delete_href(name), headers=PAGE_HEADERS)

    main = small_page(answer, status=status, alert=alert)
    if status == 503:
        assert try_again(main) == note_delete_href(name)
    assert "Delete this note?" not in main
    assert after_the_failure(seen) == []


@pytest.mark.parametrize(
    ("kind", "status", "alert"),
    [
        (sqlite3.OperationalError, 503, NOTE_UNAVAILABLE),
        (UnreadableCapture, 200, NOTE_CANNOT_BE_READ),
    ],
)
def test_the_help_page_of_a_note_is_503_only_when_the_file_cannot_be_read(
    kind: type[Exception], status: int, alert: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser() as client:
        name = a_note(client)
        monkeypatch.setattr(state_of(client).project_state, "sound_capture_history", refusing(kind))
        answer = client.get(note_help_href(name), headers=PAGE_HEADERS)

    main = small_page(answer, status=status, alert=alert)
    if status == 503:
        assert try_again(main) == note_help_href(name)


# ------------------------------------------------------------- the details and search pages, read


@pytest.mark.parametrize(
    ("read", "kind"),
    [("all_assignments", sqlite3.OperationalError), ("candidates", sqlite3.DatabaseError)],
)
@pytest.mark.parametrize(
    ("family", "reader"), [(False, "her"), (False, "open"), (True, "parent"), (True, "open")]
)
def test_the_details_page_that_cannot_be_read_is_503_with_a_way_to_ask_again(
    family: bool,
    reader: str,
    read: str,
    kind: type[Exception],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, "her" if reader == "parent" else reader)
        name = a_note(client)
        sign_in_as(client, reader)
        with Statements(state_of(client)) as seen:
            if read == "candidates":
                monkeypatch.setattr(details_routes, "candidate_readings", refusing(kind, seen))
            else:
                monkeypatch.setattr(state_of(client).project_state, read, refusing(kind, seen))
            answer = client.get(note_add_href(name, family=family) + "?a=1", headers=PAGE_HEADERS)
        monkeypatch.undo()

    main = store_free_page(
        answer,
        status=503,
        heading="Add a note to homework",
        alert="This page can't be shown right now. Try again in a moment. Try again",
    )
    assert try_again(main) == note_add_href(name, family=family) + "?a=1"
    assert ways_back_of(main)[-1] == (note_href(name), "Back to the note")
    current = "/parent" if family else "/student/due-this-week"
    assert f'<a href="{current}" aria-current="page">' in answer.text
    assert after_the_failure(seen) == []


@pytest.mark.parametrize(
    ("read", "kind", "joined"),
    [
        ("all_assignments", sqlite3.OperationalError, False),
        ("one_assignment", sqlite3.DatabaseError, True),
    ],
)
@pytest.mark.parametrize(("family", "reader"), [(False, "her"), (True, "parent"), (True, "open")])
def test_the_search_page_that_cannot_be_read_is_503_with_its_words_asked_again(
    family: bool,
    reader: str,
    read: str,
    kind: type[Exception],
    joined: bool,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, "her" if reader == "parent" else reader)
        name = a_note(client)
        if joined:
            link_note(state_of(client).project_state, name)
        sign_in_as(client, reader)
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(state_of(client).project_state, read, refusing(kind, seen))
            answer = client.get(
                note_search_href(name, family=family) + "?q=essay&page=1", headers=PAGE_HEADERS
            )
        monkeypatch.undo()

    main = store_free_page(
        answer,
        status=503,
        heading="Find homework for this note",
        alert="This search can't be shown right now. Try again in a moment. Try again",
    )
    assert try_again(main) == escape(note_search_href(name, family=family) + "?q=essay&page=1")
    assert ways_back_of(main)[0] == (note_href(name), "Back to the note")
    current = "/parent" if family else "/student/due-this-week"
    assert f'<a href="{current}" aria-current="page">' in answer.text
    assert after_the_failure(seen) == []


@pytest.mark.parametrize("page", ["add", "search"])
@pytest.mark.parametrize(
    ("kind", "status", "alert"),
    [
        (sqlite3.OperationalError, 503, NOTE_UNAVAILABLE),
        (UnreadableCapture, 200, NOTE_CANNOT_BE_READ),
    ],
)
def test_the_details_and_search_pages_of_a_note_that_cannot_be_read_are_the_small_page(
    page: str, kind: type[Exception], status: int, alert: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser() as client:
        name = a_note(client)
        monkeypatch.setattr(state_of(client).project_state, "sound_capture_history", refusing(kind))
        where = note_add_href(name) if page == "add" else note_search_href(name)
        answer = client.get(where, headers=PAGE_HEADERS)

    main = small_page(answer, status=status, alert=alert)
    if status == 503:
        assert try_again(main) == where


def asked_page(page: str, name: str, *, family: bool) -> str:
    """The add or search page's address as the reader asked for it."""
    if page == "add":
        return note_add_href(name, family=family)
    return note_search_href(name, family=family) + "?q=essay"


def masthead(answer: Answer) -> tuple[str, str]:
    """The page the masthead marks as current, and the words of its link to her week."""
    head = answer.text.split('<main id="main">', 1)[0]
    current = re.findall(r'<a href="([^"]*)" aria-current="page">', head)
    week = re.search(r'<a href="/student/due-this-week"[^>]*>([^<]*)</a>', head)
    assert len(current) == 1
    assert week is not None
    return current[0], week.group(1)


@pytest.mark.parametrize("page", ["add", "search"])
@pytest.mark.parametrize("kind", [sqlite3.OperationalError, sqlite3.DatabaseError])
@pytest.mark.parametrize(
    ("family", "reader"),
    [(False, "her"), (False, "parent"), (False, "open"), (True, "parent"), (True, "open")],
)
def test_the_small_page_for_a_note_that_cannot_be_read_stays_in_the_tree_asked_for(
    page: str,
    kind: type[Exception],
    family: bool,
    reader: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, "her" if reader == "parent" else reader)
        name = a_note(client)
        sign_in_as(client, reader)
        where = asked_page(page, name, family=family)
        monkeypatch.setattr(state_of(client).project_state, "sound_capture_history", refusing(kind))
        answers = [client.get(where, headers=PAGE_HEADERS) for _ in range(2)]
        monkeypatch.undo()
        healthy = client.get(where, headers=PAGE_HEADERS)

    parent = reader == "parent" or (reader == "open" and family)
    for answer in answers:
        main = small_page(answer, status=503, alert=NOTE_UNAVAILABLE)
        assert try_again(main) == where
        assert ways_back_of(main)[-1] == week_link("parent" if parent else "her")
        current = "/parent" if family else "/student/due-this-week"
        assert masthead(answer) == (current, "Student week" if parent else "My week")
    assert healthy.status_code == 200


@pytest.mark.parametrize("href", [note_href, note_help_href, note_delete_href])
@pytest.mark.parametrize("reader", ["her", "open"])
def test_the_small_page_of_her_own_note_pages_stays_in_her_tree(
    href: Callable[[str], str], reader: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        name = a_note(client)
        refused = refusing(sqlite3.OperationalError)
        monkeypatch.setattr(state_of(client).project_state, "sound_capture_history", refused)
        answer = client.get(href(name), headers=PAGE_HEADERS)
        monkeypatch.undo()

    main = small_page(answer, status=503, alert=NOTE_UNAVAILABLE)
    assert try_again(main) == href(name)
    assert masthead(answer) == ("/student/due-this-week", "My week")


@pytest.mark.parametrize("page", ["add", "search"])
@pytest.mark.parametrize("kind", [sqlite3.OperationalError, sqlite3.DatabaseError])
@pytest.mark.parametrize(("family", "reader"), [(False, "her"), (True, "parent")])
def test_a_note_read_that_fails_is_rolled_back_before_the_small_page_is_made(
    page: str,
    kind: type[Exception],
    family: bool,
    reader: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, "her" if reader == "parent" else reader)
        name = a_note(client)
        sign_in_as(client, reader)
        store = state_of(client).project_state
        routes = details_routes if page == "add" else link_routes
        made = routes.note_unavailable
        reading: list[bool] = []

        def made_after(*args: object, **kwargs: object) -> object:
            reading.append(store._connection.in_transaction)
            return made(*args, **kwargs)

        monkeypatch.setattr(routes, "note_unavailable", made_after)
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(store, "sound_capture_history", refusing(kind, seen))
            answer = client.get(asked_page(page, name, family=family), headers=PAGE_HEADERS)
        monkeypatch.undo()

    small_page(answer, status=503, alert=NOTE_UNAVAILABLE)
    assert seen[seen.index("FAILED HERE") + 1 :] == ([] if page == "add" else ["ROLLBACK"])
    assert reading == [False]


@pytest.mark.parametrize("page", ["add", "search"])
@pytest.mark.parametrize(("family", "reader"), [(False, "her"), (True, "parent")])
def test_the_add_and_search_pages_while_the_file_is_held_commit_nothing(
    page: str, family: bool, reader: str, tmp_path: pathlib.Path
) -> None:
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, "her" if reader == "parent" else reader)
        name = a_note(client)
        sign_in_as(client, reader)
        where = asked_page(page, name, family=family)
        with Statements(state_of(client)) as seen, HeldByAnother(database_of(client)):
            answer = client.get(where, headers=PAGE_HEADERS)
        healthy = client.get(where, headers=PAGE_HEADERS)

    small_page(answer, status=503, alert=NOTE_UNAVAILABLE)
    assert masthead(answer)[0] == ("/parent" if family else "/student/due-this-week")
    ran = [line.strip().upper() for line in seen]
    assert "COMMIT" not in ran
    assert ran[-1] == "ROLLBACK"
    assert len([line for line in ran if line not in ("ROLLBACK", "BEGIN DEFERRED")]) == 1
    assert healthy.status_code == 200


# ------------------------------------------------------------- a refusal on the note page


NOTE_REFUSALS = {
    "an edit from a page that is behind": (
        409,
        "This note changed while you were away, so nothing was saved.",
    ),
    "an archive form that is not whole": (
        422,
        "That form carried a field twice, or one this page does not send, so nothing was saved.",
    ),
    "a delete of a note in homework": (
        409,
        "It was just added to homework, so it can't be deleted.",
    ),
    "an edit with no words": (
        422,
        "Nothing was saved, because the note has no words.",
    ),
}


@pytest.mark.parametrize(("kind_name", "kind"), READS)
@pytest.mark.parametrize("reader", ["her", "open"])
@pytest.mark.parametrize("case", list(NOTE_REFUSALS))
def test_a_refusal_on_a_note_whose_homework_cannot_be_read_is_said_without_the_page(
    case: str,
    reader: str,
    kind_name: str,
    kind: type[Exception],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del kind_name
    status, said = NOTE_REFUSALS[case]
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        name = in_homework(client)
        now = revision_of(client, name)
        fields = {"text": TYPED, "course": "", "due_date": ""}
        press = {
            "an edit from a page that is behind": (
                note_action(name, "edit"),
                {**fields, "revision": str(now - 1)},
            ),
            "an archive form that is not whole": (note_action(name, "archive"), {}),
            "a delete of a note in homework": (
                note_action(name, "delete"),
                {"revision": str(now)},
            ),
            "an edit with no words": (
                note_action(name, "edit"),
                {**fields, "text": "", "revision": str(now)},
            ),
        }[case]
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).project_state, "one_assignment", refusing(kind, seen)
            )
            answer = client.post(press[0], data=press[1], headers=PAGE_HEADERS)
        monkeypatch.undo()
        after = every_row(database_of(client))

    main = store_free_page(
        answer, status=status, heading="Update not saved", alert=f"{said} {NOTE_NOT_SHOWN}"
    )
    if "edit" in case:
        assert f"readonly>{escape(TYPED if 'behind' in case else '')}</textarea>" in main
    assert after_the_failure(seen) == []
    assert after == before


# ------------------------------------------------------------- a refusal on the details or search


DETAILS_REFUSALS = {
    "a form that is not whole": (
        422,
        "That form carried a field twice, or one this page does not send, so nothing was saved.",
    ),
    "a title left out": (422, "Nothing was added, because the homework needs a title."),
    "a class not chosen": (422, "Nothing was saved, because no class was chosen."),
    "a title too long": (
        422,
        "Nothing was saved, because the title is longer than 200 characters.",
    ),
    "a kind no page offers": (
        422,
        "Nothing was saved, because neither Homework nor Task was chosen.",
    ),
    "a page that is behind": (
        409,
        "This note changed while you were away, so nothing was saved.",
    ),
    "homework of this class and title": (
        409,
        "Homework with this class and title is already here, so nothing was added yet.",
    ),
}


def details_fields(client: TestClient, name: str) -> dict[str, str]:
    page = client.get(note_add_href(name), headers=PAGE_HEADERS).text
    return form_fields(page, note_add_action(name))


DETAILS_FAILURES = [
    (case, failing)
    for case in DETAILS_REFUSALS
    for failing in ("the note", "the homework")
    if not (case == "a title too long" and failing == "the homework")
]
"""Each refusal with each read of the page that follows it: the note, then the homework of
its class and title, which the page doesn't read for a title the rules won't keep."""


@pytest.mark.parametrize(("case", "failing"), DETAILS_FAILURES)
def test_a_refusal_on_the_details_is_said_without_them_when_they_cannot_be_read(
    case: str, failing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, said = DETAILS_REFUSALS[case]
    with browser() as client:
        name = a_note(client)
        form = {
            **details_fields(client, name),
            "course_choice": "__another__",
            "course_other": "Geometry",
            "title": "Questions 4-8",
            "due_date": "",
            "kind": "HOMEWORK",
            "note": TYPED,
        }
        press = note_add_action(name)
        if case == "homework of this class and title":
            form = {
                **form,
                "course_choice": "World History",
                "course_other": "",
                "title": "Canal Era comparison essay",
            }
        elif case == "a form that is not whole":
            form = {**form, "status": "done"}
        elif case == "a title left out":
            form = {**form, "title": ""}
        elif case == "a class not chosen":
            form = {**form, "course_choice": "", "course_other": ""}
        elif case == "a title too long":
            form = {**form, "title": "t" * 201}
        elif case == "a kind no page offers":
            form = {**form, "kind": "ESSAY"}
        elif case == "a page that is behind":
            form = {**form, "revision": "7"}
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            if failing == "the note":
                monkeypatch.setattr(
                    state_of(client).project_state,
                    "sound_capture_history",
                    refusing(sqlite3.OperationalError, seen),
                )
            else:
                monkeypatch.setattr(
                    details_routes, "candidate_readings", refusing(sqlite3.DatabaseError, seen)
                )
            answer = client.post(press, data=form, headers=PAGE_HEADERS)
        monkeypatch.undo()
        after = every_row(database_of(client))

    main = store_free_page(
        answer, status=status, heading="Update not saved", alert=f"{said} {FORM_NOT_SHOWN}"
    )
    assert f"readonly>{escape(TYPED)}</textarea>" in main
    assert after_the_failure(seen) == []
    assert after == before


SEARCH_REFUSALS = {
    "a form that is not whole": (
        422,
        "That form carried a field twice, or one this page does not send, so nothing was saved.",
    ),
    "no homework chosen": (422, "Nothing was changed, because no homework was chosen."),
    "a page that is behind": (
        409,
        "This note changed while you were away, so nothing was saved.",
    ),
    "homework that changed": (
        409,
        "That homework changed since this page was made, so nothing was changed.",
    ),
}


@pytest.mark.parametrize("case", list(SEARCH_REFUSALS))
def test_a_refusal_on_the_search_is_said_without_it_when_it_cannot_be_read(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, said = SEARCH_REFUSALS[case]
    with browser() as client:
        name = a_note(client)
        page = client.get(note_search_href(name) + "?q=essay", headers=PAGE_HEADERS).text
        action = note_link_action(name)
        row = page[page.index(f'action="{action}"') :]
        form = {**form_fields(row, action), "q": "essay", "page": "1"}
        if case == "a form that is not whole":
            form = {**form, "status": "done"}
        elif case == "no homework chosen":
            form = {**form, "target": ""}
        elif case == "a page that is behind":
            form = {**form, "revision": "7"}
        elif case == "homework that changed":
            form = {**form, "basis": "0" * 64}
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).project_state,
                "sound_capture_history",
                refusing(sqlite3.OperationalError, seen),
            )
            answer = client.post(action, data=form, headers=PAGE_HEADERS)
        monkeypatch.undo()
        after = every_row(database_of(client))

    main = store_free_page(
        answer, status=status, heading="Nothing was saved", alert=f"{said} {SEARCH_NOT_SHOWN}"
    )
    assert 'Search words, as typed: <span class="authored-text">essay</span>' in main
    assert after_the_failure(seen) == []
    assert after == before


def test_a_link_to_homework_gone_from_the_record_is_said_as_before_when_the_search_is_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal that is true without the page is said on the page that reads no store as it
    always was, with no line about the search."""
    with browser() as client:
        name = a_note(client)
        page = client.get(note_search_href(name) + "?q=essay", headers=PAGE_HEADERS).text
        action = note_link_action(name)
        row = page[page.index(f'action="{action}"') :]
        form = {**form_fields(row, action), "q": "essay", "page": "1"}
        form = {**form, "target": "assignment-not-here"}
        monkeypatch.setattr(
            state_of(client).project_state,
            "sound_capture_history",
            refusing(sqlite3.OperationalError),
        )
        answer = client.post(action, data=form, headers=PAGE_HEADERS)

    store_free_page(
        answer,
        status=409,
        heading="Nothing was saved",
        alert="That homework is not on record now. Nothing was changed.",
    )


# ------------------------------------------------------------- an unlink whose note can't be shown


UNLINKS = {
    "a form that is not whole": (
        422,
        "That form carried a field twice, or one this page does not send, so nothing was saved.",
    ),
    "a page that is behind": (409, "This note changed while you were away, so nothing was saved."),
    "a note not joined to homework": (
        409,
        "This note is not joined to homework that was here before it, so there is no link to "
        "change or to unlink. Nothing was changed.",
    ),
    "a note not on record": (404, NOT_SAVED),
    "a write the file refuses": (500, NOT_SAVED),
}
"""Each way an unlink is refused or not saved, its status, and its words where its note's
details are not shown."""
READS_AGAIN = [
    ("sound_capture_history", sqlite3.OperationalError),
    ("all_assignments", sqlite3.DatabaseError),
]
"""Where an unlink's details fail as they are read again for its answer: the note's own read,
or a later read of the page, and the two kinds of failure a read meets. A note not on record
has no page to read after its own read, so it meets only the first."""
UNLINK_TREES = [("her", False), ("parent", True), ("open", False), ("open", True)]
"""Who presses an unlink, and through which tree: her own tree is hers, the family's a
parent's, and both are open with the sign-in off."""


def unlink_press(
    client: TestClient, case: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, dict[str, str], str]:
    """A note joined to homework, and the unlink press ``case`` makes: the name it is made on,
    the form, and the homework the form names."""
    store = state_of(client).project_state
    name = a_note(client)
    homework = link_note(store, name).assignment_id
    now = revision_of(client, name)
    form = {"revision": str(now), "from": homework}
    if case == "a form that is not whole":
        form = {**form, "status": "done"}
    elif case == "a page that is behind":
        form = {**form, "revision": str(now - 1)}
    elif case == "a note not joined to homework":
        name = a_note(client, "Reading log")
        form = {**form, "revision": "1"}
    elif case == "a note not on record":
        name = str(uuid4())
    elif case == "a write the file refuses":

        def refused(capture_id: str, **_: object) -> None:
            raise CaptureNotSaved(capture_id, sqlite3.OperationalError("database is locked"))

        monkeypatch.setattr(store, "unlink_capture", refused)
    return name, form, homework


@pytest.mark.parametrize(
    ("case", "place", "kind"),
    [
        (case, place, kind)
        for case in UNLINKS
        for place, kind in READS_AGAIN
        if case != "a note not on record" or place == "sound_capture_history"
    ],
)
@pytest.mark.parametrize(("reader", "family"), UNLINK_TREES)
def test_an_unlink_whose_details_cannot_be_read_says_after_its_words_that_the_note_cannot_be_shown(
    case: str,
    place: str,
    kind: type[Exception],
    reader: str,
    family: bool,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unlink refused, or one the file would not save, whose details then can't be read
    is said on the page that reads no store: its own words and status, then once that the
    note can't be shown, and nothing about search words or typing the press never had."""
    status, said = UNLINKS[case]
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        name, form, homework = unlink_press(client, case, monkeypatch)
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(state_of(client).project_state, place, refusing(kind, seen))
            answer = client.post(
                note_unlink_action(name, family=family), data=form, headers=PAGE_HEADERS
            )
        monkeypatch.undo()
        after = every_row(database_of(client))

    main = store_free_page(
        answer, status=status, heading="Nothing was saved", alert=f"{said} {NOTE_NOT_SHOWN}"
    )
    assert main.count(str(escape(NOTE_NOT_SHOWN))) == 1
    assert f'Link shown on your form: <span class="authored-text">{homework}</span>' in main
    assert "Search words" not in main
    assert "Homework chosen" not in main
    assert after_the_failure(seen) == []
    assert after == before


@pytest.mark.parametrize(("reader", "family"), [("her", False), ("parent", True)])
@pytest.mark.parametrize("case", ["a form that is not whole", "a write the file refuses"])
def test_an_unlink_while_the_file_is_held_says_the_note_cannot_be_shown_and_lands_once_after(
    case: str, reader: str, family: bool, tmp_path: pathlib.Path
) -> None:
    """With the file held by another program, an unlink that is not whole is refused and a
    whole one is not saved, and either way its details can't be read back: the page says so
    after the unlink's own words. Once the file is free, the whole press unlinks once."""
    status, said = UNLINKS[case]
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        store = state_of(client).project_state
        name = a_note(client)
        homework = link_note(store, name).assignment_id
        whole = {"revision": str(revision_of(client, name)), "from": homework}
        form = {**whole, "status": "done"} if case == "a form that is not whole" else whole
        action = note_unlink_action(name, family=family)
        before = every_row(database_of(client))
        with HeldByAnother(database_of(client)):
            answer = client.post(action, data=form, headers=PAGE_HEADERS)
        after = every_row(database_of(client))
        again = client.post(action, data=whole, headers=PAGE_HEADERS)
        found = store.sound_capture_history(name)

    store_free_page(
        answer, status=status, heading="Nothing was saved", alert=f"{said} {NOTE_NOT_SHOWN}"
    )
    assert after == before
    assert again.status_code == 303
    assert found is not None
    assert found[0].assignment_id is None


@pytest.mark.parametrize(
    ("case", "status", "said"),
    [
        ("her press in the family's tree", 403, NOT_A_PARENTS_PRESS),
        ("a name that is no note's", 404, "This homework note is not on record."),
        ("a note not on record, its later reads failing", 404, NOT_SAVED),
    ],
)
def test_an_unlink_whose_details_are_never_read_adds_no_line_about_the_note(
    case: str,
    status: int,
    said: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unlink answered before its details are read, or whose note is not on record, meets
    no page that fails, so its words stand alone."""
    with household_client("her", tmp_path) as client:
        sign_in_as(client, "her")
        name = a_note(client)
        homework = link_note(state_of(client).project_state, name).assignment_id
        form = {"revision": str(revision_of(client, name)), "from": homework}
        action = note_unlink_action(name)
        if case == "her press in the family's tree":
            action = note_unlink_action(name, family=True)
        elif case == "a name that is no note's":
            action = note_unlink_action("not-a-note")
        else:
            action = note_unlink_action(str(uuid4()))
        with Statements(state_of(client)) as seen:
            for place in ("sound_capture_history", "all_assignments"):
                monkeypatch.setattr(
                    state_of(client).project_state, place, refusing(sqlite3.OperationalError, seen)
                )
            if case == "a note not on record, its later reads failing":
                monkeypatch.setattr(
                    state_of(client).project_state, "sound_capture_history", lambda _: None
                )
            answer = client.post(action, data=form, headers=PAGE_HEADERS)

    main = store_free_page(answer, status=status, heading="Nothing was saved", alert=said)
    assert str(escape(NOTE_NOT_SHOWN)) not in main
    assert "FAILED HERE" not in seen


# ------------------------------------------------------------- a note that can't be decoded


@pytest.mark.parametrize("press", ["save details", "add", "link"])
def test_a_write_refused_on_a_damaged_note_keeps_its_own_message(press: str) -> None:
    with browser() as client:
        name = a_note(client)
        if press == "link":
            page = client.get(note_search_href(name) + "?q=essay", headers=PAGE_HEADERS).text
            action = note_link_action(name)
            row = page[page.index(f'action="{action}"') :]
            form = {**form_fields(row, action), "q": "essay", "page": "1"}
        else:
            action = note_add_action(name) if press == "add" else note_details_action(name)
            form = {
                **details_fields(client, name),
                "course_choice": "__another__",
                "course_other": "Geometry",
                "title": "Questions 4-8",
                "due_date": "",
                "kind": "HOMEWORK",
                "note": TYPED,
            }
        damage(client, name)
        before = every_row(database_of(client))
        answer = client.post(action, data=form, headers=PAGE_HEADERS)
        after = every_row(database_of(client))

    heading = "Nothing was saved" if press == "link" else "Update not saved"
    store_free_page(answer, status=500, heading=heading, alert=NOT_SAVED)
    assert after == before


@pytest.mark.parametrize(
    ("case", "status", "said"),
    [
        (
            "a form that is not whole",
            422,
            "That form carried a field twice, or one this page does not send, so nothing was "
            "saved.",
        ),
        (
            "a page that is behind",
            409,
            "This note changed while you were away, so nothing was saved.",
        ),
    ],
)
def test_an_unlink_refused_whose_details_cannot_be_read_is_said_without_them(
    case: str, status: int, said: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser() as client:
        name = a_note(client)
        homework = link_note(state_of(client).project_state, name)
        now = revision_of(client, name)
        form = {"revision": str(now), "from": homework.assignment_id}
        if case == "a form that is not whole":
            form = {**form, "status": "done"}
        else:
            form = {**form, "revision": str(now - 1)}
        before = every_row(database_of(client))
        with Statements(state_of(client)) as seen:
            monkeypatch.setattr(
                state_of(client).project_state,
                "all_assignments",
                refusing(sqlite3.OperationalError, seen),
            )
            answer = client.post(note_unlink_action(name), data=form, headers=PAGE_HEADERS)
        monkeypatch.undo()
        after = every_row(database_of(client))

    main = store_free_page(
        answer, status=status, heading="Nothing was saved", alert=f"{said} {NOTE_NOT_SHOWN}"
    )
    assert homework.assignment_id in main
    assert after_the_failure(seen) == []
    assert after == before


@pytest.mark.parametrize("case", ["a form that is not whole", "a write the store refuses"])
def test_an_unlink_refused_on_a_damaged_note_says_the_note_cannot_be_read(case: str) -> None:
    with browser() as client:
        name = a_note(client)
        link_note(state_of(client).project_state, name)
        now = revision_of(client, name)
        homework = state_of(client).project_state.sound_capture_history(name)
        assert homework is not None
        leaving = homework[0].assignment_id or ""
        form = {"revision": str(now), "from": leaving}
        if case == "a form that is not whole":
            form = {**form, "status": "done"}
        damage(client, name)
        answer = client.post(note_unlink_action(name), data=form, headers=PAGE_HEADERS)

    if case == "a form that is not whole":
        store_free_page(answer, status=422, heading="Nothing was saved", alert=NOTE_UNREADABLE)
    else:
        store_free_page(answer, status=500, heading="Nothing was saved", alert=NOT_SAVED)


@pytest.mark.parametrize("marker", ["clarified", "made up by hand"])
def test_the_note_page_after_a_save_that_cannot_be_read_says_nothing_of_the_save(
    marker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser() as client:
        name = a_note(client)
        if marker == "clarified":
            form = {
                **details_fields(client, name),
                "course_choice": "__another__",
                "course_other": "Geometry",
                "title": "Questions 4-8",
                "due_date": "",
                "kind": "HOMEWORK",
                "note": "",
            }
            saved = client.post(note_details_action(name), data=form, headers=PAGE_HEADERS)
            assert saved.status_code == 303
            landed = saved.headers["location"].split("#", 1)[0]
        else:
            landed = note_href(name) + "?said=clarified&event=" + "e" * 20
        monkeypatch.setattr(
            state_of(client).project_state,
            "sound_capture_history",
            refusing(sqlite3.OperationalError),
        )
        unread = client.get(landed, headers=PAGE_HEADERS)
        monkeypatch.undo()
        damage(client, name)
        damaged = client.get(landed, headers=PAGE_HEADERS)

    main = small_page(unread, status=503, alert=NOTE_UNAVAILABLE)
    assert try_again(main) == escape(landed)
    assert "Details saved" not in unread.text
    small_page(damaged, status=200, alert=NOTE_CANNOT_BE_READ)
    assert "Details saved" not in damaged.text


# ------------------------------------------------------------- asking for help about a note


@pytest.mark.parametrize("kind", [sqlite3.OperationalError, UnreadableCapture])
@pytest.mark.parametrize("case", ["not whole", "too long"])
def test_a_help_form_refused_on_a_note_that_cannot_be_read_says_the_form_first(
    case: str, kind: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser() as client:
        name = a_note(client)
        page = client.get(note_help_href(name), headers=PAGE_HEADERS).text
        fields = form_fields(page, note_action(name, "ask-for-help"))
        question = TYPED if case == "not whole" else "q" * 501
        form = {**fields, "note": question}
        if case == "not whole":
            form = {**form, "extra": "x"}
        before = every_row(database_of(client))
        monkeypatch.setattr(state_of(client).project_state, "sound_capture_history", refusing(kind))
        answer = client.post(note_action(name, "ask-for-help"), data=form, headers=PAGE_HEADERS)
        monkeypatch.undo()
        after = every_row(database_of(client))

    said = (
        "That form carried a field twice, left one out, or had one this page doesn't send, so "
        "nothing was sent."
        if case == "not whole"
        else "Nothing was sent, because the question is longer than 500 characters."
    )
    main = store_free_page(
        answer,
        status=422,
        heading="Request not sent",
        alert=f"{said} This homework note can't be shown right now. Your words are below.",
    )
    assert (
        f'<textarea id="kept-help-question" rows="3" readonly>{escape(question)}</textarea>' in main
    )
    assert '<a href="/student/due-this-week#ask-for-help">Open a new help form</a>' in main
    assert after == before


def test_a_parent_asking_about_her_note_that_cannot_be_read_is_still_refused_first(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with household_client("parent", tmp_path) as client:
        sign_in_as(client, "her")
        name = a_note(client)
        sign_in_as(client, "parent")
        monkeypatch.setattr(
            state_of(client).project_state,
            "sound_capture_history",
            refusing(sqlite3.OperationalError),
        )
        answer = client.post(note_action(name, "ask-for-help"), data={"note": "x"})

    assert answer.status_code == 403
    assert "Sign in as the student to ask for help or take a request back." in answer.text


# ------------------------------------------------------------- the unreadable-note lines


@pytest.mark.parametrize("where", ["her week", "the notes list", "family review"])
def test_a_note_that_cannot_be_decoded_is_counted_without_a_word_about_a_change(
    where: str,
) -> None:
    with browser() as client:
        name = a_note(client)
        damage(client, name)
        path = {
            "her week": "/student/due-this-week",
            "the notes list": "/student/homework-notes",
            "family review": "/parent",
        }[where]
        answer = client.get(path, headers=PAGE_HEADERS)

    assert answer.status_code == 200
    assert "1 homework note cannot be read right now.</p>" in answer.text
    assert "homework note cannot be read right now. Nothing was changed." not in answer.text


# ------------------------------------------------------------- the gone page's way back


def test_a_save_on_homework_gone_from_the_record_keeps_her_words_when_today_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client, Statements(state_of(client)) as seen:
        monkeypatch.setattr(
            state_of(client).drafts, "latest_for", refusing(sqlite3.OperationalError, seen)
        )
        answer = client.post(
            "/student/actions/assignments/assignment-not-here/report",
            data={
                "status": "done",
                "note": TYPED,
                "expected_report_id": "",
                "report_view": "detail",
                "return_to": "today",
                "week": "",
                "plan_id": "",
            },
        )

    assert answer.status_code == 404
    main = main_of(answer.text)
    assert "This assignment is not on record now." in main
    assert f"readonly>{escape(TYPED)}</textarea>" in main
    assert ways_back_of(main) == [("/student/due-this-week#today", "Back to Today")]
    assert after_the_failure(seen) == []


GONE = "assignment-not-here"
WEEK_DEFAULT = (week_href(None, GONE, show=GONE), "Back to the week")
FAMILY_DEFAULT = (f"/parent?focus={GONE}#update-{GONE}", "Back to family review")
CHECKED_BACKS = [
    (
        {"return_to": "week", "week": "2026-08-10"},
        (week_href(date(2026, 8, 10), GONE, show=GONE), "Back to the week"),
        False,
    ),
    ({"return_to": "to_turn_in"}, ("/student/to-turn-in#to-turn-in", "Back to To turn in"), False),
    ({"return_to": "today"}, ("/student/due-this-week#today", "Back to Today"), True),
    ({"return_to": "family"}, FAMILY_DEFAULT, False),
    (
        {"return_to": "family", "plan_id": "draft:a"},
        ("/parent?plan=draft%3Aa#plan-draft-a", "Back to family review"),
        True,
    ),
]
"""Each way back these pages write, where the gone page's link leads while no plan can be
read, and whether the link tries to read one on the way. Her own device never writes the
family page's."""
UNCHECKED_BACKS = [
    {},
    {"return_to": "month"},
    {"return_to": "WEEK"},
    {"return_to": "week", "week": "2026-13-01"},
    {"return_to": "week", "week": "0001-01-03"},
    {"return_to": "today", "week": "2026-08-17"},
    {"return_to": "week", "plan_id": "draft:a"},
    {"return_to": "family", "plan_id": "p" * 201},
    {"week": "2026-08-17"},
    {"plan_id": "draft:a"},
]
"""A way back left out, and ways back no page of these writes: a place that is not one of
the four, a week that is no day or none her week can show, a week or a plan beside a place
that takes neither, a plan too long to be one, and a week or a plan with no place."""
FAMILY_FOR_HER = [{"return_to": "family"}, {"return_to": "family", "plan_id": "draft:a"}]
"""The family page named by her own signed-in device, which no page of hers writes."""
GONE_PRESSES = [
    ("details", "her"),
    ("details", "parent"),
    ("details", "open"),
    *[
        (press, reader)
        for press in ("update", "undo", "hand-in", "hand-in undo")
        for reader in ("her", "open")
    ],
]
"""What reaches the gone page, and who: the details asked for by anyone, and each press the
details make, by anyone they are open to."""


def gone_press(client: TestClient, press: str, back: dict[str, str]) -> Answer:
    """``press`` on work gone from the record, from its details, carrying ``back`` as its way
    back."""
    if press == "details":
        return client.get(f"/student/assignments/{GONE}", params=back, headers=PAGE_HEADERS)
    route, fields = {
        "update": (
            "report",
            {"status": "done", "note": TYPED, "expected_report_id": "", "report_view": "detail"},
        ),
        "undo": ("undo-report", {"report_id": "report-not-here", "report_view": "detail"}),
        "hand-in": (
            "hand-in",
            {"state": NEEDS_HAND_IN, "next_action": "", "note": TYPED, "expected_hand_in_id": ""},
        ),
        "hand-in undo": ("undo-hand-in", {"hand_in_id": "hand-in-not-here"}),
    }[press]
    return client.post(
        f"/student/actions/assignments/{GONE}/{route}",
        data={**fields, "return_to": "", "week": "", "plan_id": "", **back},
        headers=PAGE_HEADERS,
    )


def fail_the_plans(monkeypatch: pytest.MonkeyPatch, client: TestClient, calls: list[str]) -> None:
    """Mark every store call in ``calls``, and make both reads of a plan a way back can make
    fail, marked where they fail."""
    state = state_of(client)
    spy_on_stores(monkeypatch, state, calls)
    monkeypatch.setattr(state.drafts, "latest_for", refusing(sqlite3.OperationalError, calls))
    monkeypatch.setattr(state.drafts, "get", refusing(sqlite3.DatabaseError, calls))


@pytest.mark.parametrize(("press", "reader"), GONE_PRESSES)
def test_the_gone_pages_way_back_follows_the_checked_way_back_with_no_call_after_a_failure(
    press: str, reader: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Work gone from the record is said on its small page with the way back the address or
    the form carried, checked. When the plan that way back looks up can't be read, the link
    is made from the checked values alone, and nothing is asked of any store after the
    failure."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        for back, (href, label), reads in CHECKED_BACKS:
            if reader == "her" and back in FAMILY_FOR_HER:
                continue
            with Statements(state_of(client)) as seen:
                fail_the_plans(monkeypatch, client, seen)
                answer = gone_press(client, press, back)
            monkeypatch.undo()

            assert answer.status_code == 404, (back, answer.text[:300])
            main = main_of(answer.text)
            assert "This assignment is not on record now." in main
            assert ways_back_of(main) == [(str(escape(href)), label)], back
            assert ("FAILED HERE" in seen) is reads, back
            if reads:
                assert after_the_failure(seen) == [], back


@pytest.mark.parametrize(("press", "reader"), GONE_PRESSES)
def test_the_gone_pages_way_back_is_the_safe_default_when_none_these_pages_make_is_named(
    press: str, reader: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A way back left out, or one no page of these writes, gives the reader's safe default,
    the family page at the row for a parent and her week for anyone else, and looks no plan
    up, so a plan that can't be read changes nothing."""
    href, label = FAMILY_DEFAULT if reader == "parent" else WEEK_DEFAULT
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        for back in UNCHECKED_BACKS + (FAMILY_FOR_HER if reader == "her" else []):
            calls: list[str] = []
            fail_the_plans(monkeypatch, client, calls)
            answer = gone_press(client, press, back)
            monkeypatch.undo()

            assert answer.status_code == 404, (back, answer.text[:300])
            main = main_of(answer.text)
            assert "This assignment is not on record now." in main
            assert ways_back_of(main) == [(str(escape(href)), label)], back
            assert "FAILED HERE" not in calls, back
            assert not [call for call in calls if call.startswith("drafts.")], (back, calls)


# ------------------------------------------------------------- the small page's Try again


def test_the_small_pages_explanation_is_outlined_and_its_try_again_is_as_tall_as_a_control() -> (
    None
):
    """The small page's focused explanation takes the outline every other focused explanation
    has, drawn around the sentence alone, and the link inside it keeps its place in the line
    and takes the height of a control. The browser shows what they give; here the rules are
    pinned."""
    assert any(
        "outline: 2px solid var(--blue-action);" in inside
        for inside in rules_named("#note-problem:focus")
    )
    assert any(
        "display: inline-block;" in inside
        and "padding: 0.8rem 0;" in inside
        and "margin: -0.8rem 0;" in inside
        for inside in rules_named("#note-problem a")
    )


# ------------------------------------------------------------- the file held by another program


def test_a_note_page_while_the_file_is_held_is_503_and_reads_once(tmp_path: pathlib.Path) -> None:
    with household_client("her", tmp_path) as client:
        sign_in_as(client, "her")
        name = in_homework(client)
        with Statements(state_of(client)) as seen, HeldByAnother(database_of(client)):
            answer = client.get(note_href(name), headers=PAGE_HEADERS)
        readable = client.get(note_href(name), headers=PAGE_HEADERS)

    small_page(answer, status=503, alert=NOTE_UNAVAILABLE)
    ran = [line for line in seen if line.strip().upper() not in ("ROLLBACK", "BEGIN DEFERRED")]
    assert len(ran) == 1
    assert readable.status_code == 200
