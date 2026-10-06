# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Asking for help: one press from her page, a state a parent moves, each step shown to her,
and a parent's updates, added without closing the request."""

import logging
import pathlib
import re
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from pydantic import ValidationError

from blossom.app import create_app
from blossom.clock import FrozenClock
from blossom.dependencies import STATE_ATTRIBUTE, ApplicationState
from blossom.routes import student as student_routes
from blossom.stores import help_requests as help_store
from blossom.stores.help_requests import (
    HELP_RETENTION_DAYS,
    NOTE_MAX_LENGTH,
    HelpRequest,
    HelpRequestsStore,
    RequestClosed,
)
from tests.support import (
    HERS,
    OBSERVED_AT,
    PLAN_DATE,
    SAME_ORIGIN,
    THEIRS,
    ZONE,
    Answer,
    SetClock,
    as_a_browser_sends,
    fixture_clock,
    fixture_settings,
    help_reply,
    help_row,
    help_updates,
    refusing,
    signed_in_household,
    whole_form,
)

PAGE = "/student/due-this-week"
ASK = "/student/actions/ask-for-help"
FORM_TYPE = "application/x-www-form-urlencoded"
CLOSED_ON = r"<strong>A parent closed this request on \w+day, \w+ \d+(, \d{4})?\.</strong>"
"""A closure's words on both pages. The store stamps it by the real clock, so its day varies."""
WAITING = "<strong>Waiting for a parent.</strong>"
HELPING = "<strong>A parent is helping.</strong>"
ALREADY_TAKEN_UP = (
    "This request was already taken up. Your words weren't added; they're below in Add an update."
)
CLOSED_BEFORE_UPDATE = "This request was closed before your update was added."
ALREADY_CLOSED = "This request is already closed."
FORM_ADDED_OTHER_WORDS = (
    "This form already added an update with other words, so nothing was changed. Your words "
    "are below in Add an update."
)
NOT_TAKEN_UP_YET = (
    "This request isn't taken up yet, so your update wasn't added. Your words are below."
)
UPDATE_NEEDS_WORDS = "An update needs some words. Nothing was added."
BAD_FORM = "The form did not arrive whole. Nothing was written."
"""What the family page says for each press on a request it can't make as asked."""


def store_in_memory(clock: FrozenClock | None = None) -> HelpRequestsStore:
    return HelpRequestsStore(
        sqlite3.connect(":memory:", check_same_thread=False), clock or fixture_clock()
    )


def browser() -> TestClient:
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()))
    return TestClient(app, follow_redirects=False, headers=SAME_ORIGIN)


def ask_form(client: TestClient) -> dict[str, str]:
    """The fields her page's Ask for help form sends, a fresh id among them."""
    return whole_form(client.get(PAGE).text, ASK)


# ------------------------------------------------------------------ the store


def test_a_request_starts_as_requested_and_can_be_taken_back_until_a_parent_takes_it_up() -> None:
    store = store_in_memory()

    asked = store.ask(PLAN_DATE, "the essay outline")

    assert asked.state == "requested"
    assert asked.note == "the essay outline"
    assert asked.asked_at == fixture_clock().now()
    assert [item.request_id for item in store.open_requests()] == [asked.request_id]
    assert store.take_back(asked.request_id) is True
    assert store.open_requests() == []
    assert store.take_back(asked.request_id) is False


def test_taking_up_then_resolving_moves_the_state_and_keeps_the_words_back() -> None:
    store = store_in_memory()
    asked = store.ask(PLAN_DATE)

    taken_up = store.accept(asked.request_id, "on my way")
    again = store.accept(asked.request_id)
    with pytest.raises(help_store.AlreadyTakenUp):
        store.accept(asked.request_id, "not added")
    kept = store.get(asked.request_id)
    assert kept is not None
    assert kept.response == "on my way"
    resolved = store.resolve(asked.request_id, "we did the outline together")

    assert taken_up.state == "accepted"
    assert taken_up.accepted_at == fixture_clock().now()
    assert taken_up.response == "on my way"
    assert again == taken_up == kept
    assert resolved.state == "resolved"
    assert resolved.resolved_at == fixture_clock().now()
    assert resolved.response == "we did the outline together"
    assert bodies(resolved) == ["on my way", "we did the outline together"]
    assert store.open_requests() == []
    assert [item.request_id for item in store.recently_resolved()] == [asked.request_id]


def test_a_request_taken_up_cannot_be_taken_back_and_a_resolved_one_cannot_move() -> None:
    store = store_in_memory()
    asked = store.ask(PLAN_DATE)
    store.accept(asked.request_id)

    with pytest.raises(RequestClosed, match="taken back"):
        store.take_back(asked.request_id)
    resolved = store.resolve(asked.request_id)
    with pytest.raises(RequestClosed, match="taken up"):
        store.accept(asked.request_id)
    with pytest.raises(RequestClosed, match="resolved again"):
        store.resolve(asked.request_id, "new words")
    with pytest.raises(RequestClosed, match="given an update"):
        store.add_update(asked.request_id, "new words")
    with pytest.raises(KeyError):
        store.accept("nobody")

    assert store.resolve(asked.request_id) == resolved
    assert resolved.accepted_at is not None


def test_resolving_straight_from_requested_stamps_both_steps() -> None:
    store = store_in_memory()
    asked = store.ask(PLAN_DATE)

    resolved = store.resolve(asked.request_id, "sorted")

    assert resolved.accepted_at == resolved.resolved_at == fixture_clock().now()
    assert [(update.body, update.written_at) for update in resolved.parent_updates] == [
        ("sorted", fixture_clock().now())
    ]


def test_a_resolved_request_is_kept_two_weeks_and_an_open_one_indefinitely() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    then = HelpRequestsStore(connection, FrozenClock(OBSERVED_AT, ZONE))
    resolved = then.ask(PLAN_DATE)
    then.resolve(resolved.request_id, "done")
    still_open = then.ask(PLAN_DATE, "never answered")
    later = OBSERVED_AT + timedelta(days=HELP_RETENTION_DAYS + 1)
    now = HelpRequestsStore(connection, FrozenClock(later, ZONE))

    before_sweep = now.recently_resolved()
    gone_before_sweep = now.get(resolved.request_id)
    with pytest.raises(KeyError):
        now.accept(resolved.request_id)
    swept = now.sweep()

    assert before_sweep == []
    assert gone_before_sweep is None
    assert swept == 1
    assert now.get(resolved.request_id) is None
    assert [item.request_id for item in now.open_requests()] == [still_open.request_id]


def test_her_words_are_capped_in_the_store() -> None:
    with pytest.raises(ValidationError):
        store_in_memory().ask(PLAN_DATE, "w" * (NOTE_MAX_LENGTH + 1))


def test_the_store_offers_no_way_to_read_a_pattern() -> None:
    offered = {name for name in dir(HelpRequestsStore) if not name.startswith("_")}

    assert offered == {
        "ask",
        "ask_once",
        "already_asked",
        "take_back",
        "accept",
        "add_update",
        "resolve",
        "get",
        "open_requests",
        "recently_resolved",
        "retained",
        "sweep",
        "open",
        "close",
        "name",
        "retention_policy",
    }


# ------------------------------------------------------------- a parent's updates, in the store

AT = datetime(2026, 8, 19, 21, 0, tzinfo=UTC)
"""Five in the afternoon in New York on the fixture's Wednesday."""
HOUR = timedelta(hours=1)


def set_store() -> tuple[HelpRequestsStore, SetClock]:
    """A store in memory run by a clock the test moves."""
    clock = SetClock(PLAN_DATE, AT)
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    return HelpRequestsStore(connection, clock), clock


def bodies(request: HelpRequest | None) -> list[str]:
    """A request's updates, their words alone, in the order they were added."""
    assert request is not None
    return [update.body for update in request.parent_updates]


def rows_of(store: HelpRequestsStore) -> list[tuple[object, ...]]:
    found = store._connection.execute("SELECT * FROM help_requests ORDER BY request_id")
    return [tuple(row) for row in found.fetchall()]


def test_taken_up_without_words_a_request_takes_updates_and_stays_open() -> None:
    store, clock = set_store()
    asked = store.ask(PLAN_DATE, "the outline").request_id
    taken = store.accept(asked)
    clock.at = AT + HOUR
    first = store.add_update(asked, "Synthetic first update")
    clock.at = AT + 2 * HOUR
    second = store.add_update(asked, "Synthetic second update")

    assert (taken.state, taken.parent_updates, taken.response) == ("accepted", (), None)
    assert first.state == second.state == "accepted"
    assert [(update.body, update.written_at) for update in second.parent_updates] == [
        ("Synthetic first update", AT + HOUR),
        ("Synthetic second update", AT + 2 * HOUR),
    ]
    assert second.response == "Synthetic second update"
    assert second.accepted_at == AT
    assert store.open_requests() == [second]


@pytest.mark.parametrize("final", [None, "Synthetic final words"], ids=["no words", "words"])
@pytest.mark.parametrize("taken_up", [False, True], ids=["from waiting", "from taken up"])
def test_closing_adds_any_final_words_after_the_updates_and_keeps_them_all(
    final: str | None, taken_up: bool
) -> None:
    store, clock = set_store()
    asked = store.ask(PLAN_DATE).request_id
    earlier: list[str] = []
    if taken_up:
        store.accept(asked, "Synthetic on it")
        clock.at += HOUR
        store.add_update(asked, "Synthetic update")
        clock.at += HOUR
        earlier = ["Synthetic on it", "Synthetic update"]
    closed = store.resolve(asked, final)

    assert closed.state == "resolved"
    assert closed.resolved_at == clock.at
    assert bodies(closed) == [*earlier, *([final] if final else [])]
    assert closed.response == (final or (earlier[-1] if earlier else None))
    if final:
        assert closed.parent_updates[-1].written_at == clock.at


def test_the_same_form_sent_again_changes_nothing_whichever_move_it_made() -> None:
    """Each form carries an id of its own. Sent again with the same words, spaced alike or
    not, it finds what it did and writes nothing, before and after the request closes."""
    store, clock = set_store()
    asked = store.ask(PLAN_DATE).request_id
    helping, adding, closing = (uuid4().hex for _ in range(3))
    taken = store.accept(asked, "Synthetic on it", update_id=helping)
    clock.at += HOUR
    retaken = store.accept(asked, "Synthetic on it", update_id=helping)
    added = store.add_update(asked, "Synthetic update", update_id=adding)
    clock.at += HOUR
    readded = store.add_update(asked, "Synthetic  update ", update_id=adding)
    closed = store.resolve(asked, "Synthetic last", update_id=closing)
    clock.at += HOUR
    before = rows_of(store)
    again = [
        store.resolve(asked, "Synthetic last", update_id=closing),
        store.resolve(asked),
        store.accept(asked, "Synthetic on it", update_id=helping),
        store.add_update(asked, "Synthetic update", update_id=adding),
    ]

    assert retaken == taken
    assert readded == added
    assert again == [closed] * 4
    assert rows_of(store) == before
    assert bodies(closed) == ["Synthetic on it", "Synthetic update", "Synthetic last"]
    assert [update.update_id for update in closed.parent_updates] == [helping, adding, closing]


def test_i_can_help_again_adds_no_words_and_refuses_only_words_not_on_record() -> None:
    store, _ = set_store()
    asked = store.ask(PLAN_DATE).request_id
    taken = store.accept(asked, "Synthetic on it")
    before = rows_of(store)
    quiet = [store.accept(asked, words) for words in (None, "  ", "Synthetic  on it")]
    with pytest.raises(help_store.AlreadyTakenUp) as refused:
        store.accept(asked, "Synthetic new words")

    assert quiet == [taken] * 3
    assert refused.value.request == taken
    assert rows_of(store) == before
    assert not store._connection.in_transaction


def test_an_update_is_refused_with_nothing_written_unless_it_can_be_added() -> None:
    store, _ = set_store()
    waiting = store.ask(PLAN_DATE).request_id
    closed = store.ask(PLAN_DATE).request_id
    store.resolve(closed)
    taken = store.ask(PLAN_DATE).request_id
    store.accept(taken)
    form = uuid4().hex
    store.add_update(taken, "Synthetic update", update_id=form)
    before = rows_of(store)

    with pytest.raises(help_store.NotTakenUp):
        store.add_update(waiting, "Synthetic words")
    with pytest.raises(RequestClosed, match="given an update"):
        store.add_update(closed, "Synthetic words")
    with pytest.raises(help_store.UpdateFormUsed):
        store.add_update(taken, "Synthetic other words", update_id=form)
    with pytest.raises(help_store.UpdateFormUsed):
        store.resolve(taken, "Synthetic other words", update_id=form)
    for blank in (None, "", "  \n "):
        with pytest.raises(help_store.UpdateWithoutWords):
            store.add_update(taken, blank)
        with pytest.raises(KeyError):
            store.add_update("0" * 32, blank)
    with pytest.raises(KeyError):
        store.add_update("0" * 32, "Synthetic words")
    with pytest.raises(help_store.NotARequestId):
        store.add_update(taken, "Synthetic words", update_id="not-an-id")
    with pytest.raises(help_store.UpdateTooLong):
        store.add_update(taken, "w" * (NOTE_MAX_LENGTH + 1))

    assert rows_of(store) == before
    assert not store._connection.in_transaction


@pytest.mark.parametrize("first", ["the update", "the close"])
def test_an_update_and_a_close_interleaved_keep_every_word_whichever_lands_first(
    first: str,
) -> None:
    store, clock = set_store()
    asked = store.ask(PLAN_DATE).request_id
    store.accept(asked, "Synthetic on it")
    clock.at += HOUR
    if first == "the update":
        store.add_update(asked, "Synthetic update")
        clock.at += HOUR
        closed = store.resolve(asked, "Synthetic last")
        assert bodies(closed) == ["Synthetic on it", "Synthetic update", "Synthetic last"]
        return
    store.resolve(asked, "Synthetic last")
    clock.at += HOUR
    before = rows_of(store)
    with pytest.raises(RequestClosed):
        store.add_update(asked, "Synthetic update")
    assert rows_of(store) == before
    assert bodies(store.get(asked)) == ["Synthetic on it", "Synthetic last"]


def test_updates_sent_at_once_through_two_connections_are_all_kept(
    tmp_path: pathlib.Path,
) -> None:
    """Two connections to one file, as two processes would have, each adding updates at the
    same moment: every one is appended, none overwrites another."""
    path = tmp_path / "record.sqlite3"
    stores = [HelpRequestsStore.open(path, fixture_clock()) for _ in range(2)]
    asked = stores[0].ask(PLAN_DATE).request_id
    stores[1].accept(asked)
    start = threading.Barrier(8)
    failed: list[Exception] = []

    def add(number: int) -> None:
        try:
            start.wait()
            stores[number % 2].add_update(asked, f"Synthetic update {number}")
        except Exception as error:
            failed.append(error)

    threads = [threading.Thread(target=add, args=(number,)) for number in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    kept = stores[0].get(asked)
    for store in stores:
        store.close()

    assert failed == []
    assert sorted(bodies(kept)) == sorted(f"Synthetic update {number}" for number in range(8))
    assert kept is not None
    assert len({update.update_id for update in kept.parent_updates}) == 8


def times_follow_the_order(request: HelpRequest) -> bool:
    """Whether each update's time is no earlier than the time of the update kept before it, and
    a close, if any, no earlier than the last."""
    times = []
    for update in request.parent_updates:
        if update.written_at is not None:
            times.append(update.written_at)
    if request.resolved_at is not None:
        times.append(request.resolved_at)
    return times == sorted(times)


class LetsAnotherMoveLand:
    """A clock that, the first time it is read, lets another page's move land first, as
    another thread's could land while a move had read the clock and not yet reserved the
    writer."""

    def __init__(self, at: datetime, lands: Callable[[], None]) -> None:
        self.at = at
        self.lands: Callable[[], None] | None = lands

    @property
    def zone(self) -> ZoneInfo:
        return ZONE

    def now(self) -> datetime:
        lands, self.lands = self.lands, None
        if lands is not None:
            lands()
        return self.at

    def today(self) -> date:
        return PLAN_DATE


@pytest.mark.parametrize("move", ["update", "close"])
def test_update_times_follow_their_order_when_another_page_lands_during_a_move(
    move: str, tmp_path: pathlib.Path
) -> None:
    """Another page's update, an hour later by its clock, is sent while a move reads the clock
    and again after. The move holds the writer by then, so the other lands after it or finds
    the request closed, and every time kept follows the order kept."""
    path = tmp_path / "record.sqlite3"
    setup = HelpRequestsStore.open(path, fixture_clock())
    asked = setup.ask(PLAN_DATE).request_id
    setup.accept(asked)
    setup.close()
    other = HelpRequestsStore(
        sqlite3.connect(path, timeout=0, check_same_thread=False), SetClock(PLAN_DATE, AT + HOUR)
    )
    form = uuid4().hex
    outcomes: list[str] = []

    def lands() -> None:
        try:
            other.add_update(asked, "Synthetic from the other page", update_id=form)
        except sqlite3.OperationalError:
            outcomes.append("waits")
        except RequestClosed:
            outcomes.append("closed")
        else:
            outcomes.append("kept")

    store = HelpRequestsStore.open(path, LetsAnotherMoveLand(AT, lands))
    if move == "update":
        store.add_update(asked, "Synthetic from this page")
    else:
        store.resolve(asked, "Synthetic last words")
    lands()
    kept = store.get(asked)
    store.close()
    other.close()

    assert kept is not None
    assert times_follow_the_order(kept)
    if move == "update":
        assert outcomes == ["waits", "kept"]
        assert [(update.body, update.written_at) for update in kept.parent_updates] == [
            ("Synthetic from this page", AT),
            ("Synthetic from the other page", AT + HOUR),
        ]
        return
    assert outcomes == ["waits", "closed"]
    assert [(update.body, update.written_at) for update in kept.parent_updates] == [
        ("Synthetic last words", AT)
    ]
    assert kept.resolved_at == AT


class NotesTheWriter:
    """A clock that notes, each time it is read, whether its store's writer is reserved."""

    def __init__(self, at: datetime) -> None:
        self.at = at
        self.connection: sqlite3.Connection | None = None
        self.reserved: list[bool] = []

    @property
    def zone(self) -> ZoneInfo:
        return ZONE

    def now(self) -> datetime:
        if self.connection is not None:
            self.reserved.append(self.connection.in_transaction)
        return self.at

    def today(self) -> date:
        return PLAN_DATE


@pytest.mark.parametrize("move", ["accept", "add_update", "resolve"])
def test_every_move_reads_the_clock_only_with_the_writer_reserved(move: str) -> None:
    clock = NotesTheWriter(AT)
    store = HelpRequestsStore(sqlite3.connect(":memory:", check_same_thread=False), clock)
    asked = store.ask(PLAN_DATE).request_id
    if move == "add_update":
        store.accept(asked)
    clock.connection = store._connection
    moved = getattr(store, move)(asked, "Synthetic words")

    assert bodies(moved) == ["Synthetic words"]
    assert clock.reserved
    assert all(clock.reserved)


def test_a_parents_updates_go_with_their_request_when_it_ages_out() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    then = HelpRequestsStore(connection, FrozenClock(OBSERVED_AT, ZONE))
    asked = then.ask(PLAN_DATE).request_id
    then.accept(asked, "Synthetic zebra on it")
    then.add_update(asked, "Synthetic zebra update")
    then.resolve(asked, "Synthetic zebra last")
    past = OBSERVED_AT + timedelta(days=HELP_RETENTION_DAYS, microseconds=1)
    now = HelpRequestsStore(connection, FrozenClock(past, ZONE))

    swept = now.sweep()
    tables = [
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    held = [list(connection.execute(f"SELECT * FROM {table}")) for table in tables]  # noqa: S608

    assert swept == 1
    assert "zebra" not in repr(held)


OLD_TABLE = """
    CREATE TABLE help_requests (
        request_id TEXT PRIMARY KEY,
        evening TEXT NOT NULL,
        asked_at TEXT NOT NULL,
        note TEXT,
        state TEXT NOT NULL,
        accepted_at TEXT,
        resolved_at TEXT,
        response TEXT,
        capture_id TEXT
    )
"""
"""The table as a file from before updates holds it."""


def test_a_file_from_before_gains_the_updates_column_and_its_reply_is_an_earlier_reply(
    tmp_path: pathlib.Path,
) -> None:
    """Additive, and safe to meet again: an old table gets one nullable column, a reply kept
    in it becomes the request's one update, with no time, since none was kept; a request
    with no reply has none; and a second start changes nothing."""
    path = tmp_path / "record.sqlite3"
    stamp = fixture_clock().now().isoformat()
    old = sqlite3.connect(path)
    old.execute(OLD_TABLE)
    for request_id, state, response in (
        ("waiting", "requested", None),
        ("helping", "accepted", "Synthetic reply from before"),
        ("closed", "resolved", "Synthetic closing words from before"),
        ("quiet", "accepted", None),
        ("blank", "accepted", " \n "),
    ):
        old.execute(
            "INSERT INTO help_requests (request_id, evening, asked_at, note, state, "
            "accepted_at, resolved_at, response) VALUES (?, ?, ?, 'Synthetic question', ?, "
            "?, ?, ?)",
            (
                request_id,
                PLAN_DATE.isoformat(),
                stamp,
                state,
                None if state == "requested" else stamp,
                stamp if state == "resolved" else None,
                response,
            ),
        )
    old.commit()
    old.close()

    first = HelpRequestsStore.open(path, fixture_clock())
    kept = {request.request_id: request for request in first.retained().requests}
    first.close()
    second = HelpRequestsStore.open(path, fixture_clock())
    again = {request.request_id: request for request in second.retained().requests}
    columns = [
        str(row[1]) for row in second._connection.execute("PRAGMA table_info(help_requests)")
    ]
    added = second.add_update("helping", "Synthetic update after")

    assert columns.count("parent_updates") == 1
    for request_id, reply in (
        ("helping", "Synthetic reply from before"),
        ("closed", "Synthetic closing words from before"),
    ):
        (earlier,) = kept[request_id].parent_updates
        assert (earlier.body, earlier.written_at) == (reply, None)
        assert re.fullmatch(r"[0-9a-f]{32}", earlier.update_id)
        assert kept[request_id].response == reply
    for request_id in ("waiting", "quiet", "blank"):
        assert kept[request_id].parent_updates == ()
    assert again == kept
    assert [(update.body, update.written_at is None) for update in added.parent_updates] == [
        ("Synthetic reply from before", True),
        ("Synthetic update after", False),
    ]
    assert added.response == "Synthetic update after"


CLOSED_BY_THE_REPLY_ALONE = """
    UPDATE help_requests
    SET state = 'resolved', resolved_at = ?, accepted_at = COALESCE(accepted_at, ?),
        response = COALESCE(?, response)
    WHERE request_id = ?
"""
"""A close as a build that reads only the reply makes it: its words, if any, in that column."""


def test_words_a_build_reading_only_the_reply_saves_show_once_after_the_next_start(
    tmp_path: pathlib.Path,
) -> None:
    """Closes made by a build that reads only the reply: at the next start, new words are the
    latest update, with no time, and still the reply; the latest update's words with other line
    endings or edges, or no words, add nothing, and a second start changes nothing."""
    path = tmp_path / "record.sqlite3"
    store = HelpRequestsStore.open(path, fixture_clock())
    asked = {}
    for case in ("new words", "the same words", "no words"):
        asked[case] = store.ask(PLAN_DATE, "Synthetic question").request_id
        store.accept(asked[case], "Synthetic on it")
        store.add_update(asked[case], "Synthetic update\nover two lines")
    store.close()
    stamp = fixture_clock().now().isoformat()
    elsewhere = sqlite3.connect(path)
    for case, words in (
        ("new words", "Synthetic closing words, saved elsewhere"),
        ("the same words", "\u3000 Synthetic update\r\nover two lines \u00a0\r\n"),
        ("no words", None),
    ):
        elsewhere.execute(CLOSED_BY_THE_REPLY_ALONE, (stamp, stamp, words, asked[case]))
    elsewhere.commit()
    elsewhere.close()

    first = HelpRequestsStore.open(path, fixture_clock())
    kept = {request.request_id: request for request in first.retained().requests}
    held = rows_of(first)
    first.close()
    second = HelpRequestsStore.open(path, fixture_clock())
    held_again = rows_of(second)
    second.close()

    closed = kept[asked["new words"]]
    assert closed.state == "resolved"
    assert [(update.body, update.written_at is None) for update in closed.parent_updates] == [
        ("Synthetic on it", False),
        ("Synthetic update\nover two lines", False),
        ("Synthetic closing words, saved elsewhere", True),
    ]
    assert re.fullmatch(r"[0-9a-f]{32}", closed.parent_updates[-1].update_id)
    assert closed.response == "Synthetic closing words, saved elsewhere"
    for case in ("the same words", "no words"):
        assert bodies(kept[asked[case]]) == ["Synthetic on it", "Synthetic update\nover two lines"]
    assert held_again == held


NO_UPDATE_ID = "0123456789abcdef0123456789abcdef"


def an_update(*fields: tuple[str, str]) -> str:
    """The updates' place holding one update as a file might, each key with its JSON value,
    a key given twice kept twice."""
    return "[{" + ", ".join(f'"{key}": {value}' for key, value in fields) + "}]"


ITS_ID = ("id", f'"{NO_UPDATE_ID}"')
ITS_WORDS = ("body", '"Synthetic zebra other"')
NO_TIME = ("written_at", "null")


@pytest.mark.parametrize(
    "unreadable",
    [
        "Synthetic zebra, no list",
        '[7, {"id": "' + NO_UPDATE_ID + '", "body": "Synthetic zebra other", "written_at": null}]',
        '[{"id": "' + NO_UPDATE_ID + '", "body": "Synthetic zebra other", "written_at": null}, '
        '"Synthetic zebra"]',
        an_update(ITS_WORDS, NO_TIME),
        an_update(("id", "7"), ITS_WORDS, NO_TIME),
        an_update(ITS_ID, ITS_WORDS, ("written_at", '"Synthetic zebra time"')),
        an_update(ITS_ID, ITS_WORDS, ("written_at", '"2026-08-19T17:30:00"')),
        an_update(ITS_ID, ("body", '""'), NO_TIME),
        an_update(ITS_ID, ("body", '"' + "z" * (NOTE_MAX_LENGTH + 1) + '"'), NO_TIME),
        "[" * 100_000 + "]" * 100_000,
    ],
    ids=[
        "no list",
        "an earlier one no update",
        "the latest no update",
        "an update with no id",
        "an id that is no text",
        "a time that is no time",
        "a time with no zone",
        "no words",
        "words past the cap",
        "nested past reading",
    ],
)
def test_a_start_that_meets_updates_it_cannot_read_leaves_them_and_names_no_words(
    unreadable: str, tmp_path: pathlib.Path
) -> None:
    """Updates the pages can't read are never written at a start, even beside a reply that
    differs from every update, and reading them is ``UnreadableHelpRequest`` in names alone."""
    path = tmp_path / "record.sqlite3"
    store = HelpRequestsStore.open(path, fixture_clock())
    asked = store.ask(PLAN_DATE).request_id
    store.accept(asked, "Synthetic zebra words")
    store._connection.execute("UPDATE help_requests SET parent_updates = ?", (unreadable,))
    store._connection.commit()
    store.close()

    again = HelpRequestsStore.open(path, fixture_clock())
    held = again._connection.execute("SELECT parent_updates FROM help_requests").fetchone()[0]
    with pytest.raises(help_store.UnreadableHelpRequest) as refused:
        again.retained()

    assert held == unreadable
    assert "zebra" not in str(refused.value)
    assert asked in str(refused.value)


HOLDS_NO_UTF8 = b"Synthetic zebra \xff words"
"""Words with one byte in them that UTF-8 has no reading for."""


def held_raw(store: HelpRequestsStore, request_id: str) -> tuple[object, ...]:
    """A request's reply and updates as the file holds them, bytes and all."""
    return tuple(
        store._connection.execute(
            "SELECT CAST(response AS BLOB), CAST(parent_updates AS BLOB) FROM help_requests "
            "WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    )


@pytest.mark.parametrize(
    ("column", "held"),
    [
        pytest.param("response", HOLDS_NO_UTF8, id="a reply that is no UTF-8"),
        pytest.param(
            "parent_updates",
            b'[{"id": "' + NO_UPDATE_ID.encode() + b'", "body": "' + HOLDS_NO_UTF8 + b'", '
            b'"written_at": null}]',
            id="updates that are no UTF-8",
        ),
        pytest.param("response", "z" * (NOTE_MAX_LENGTH + 100), id="a reply past the cap"),
    ],
)
def test_a_row_a_start_cannot_read_is_left_as_it_is_and_the_start_goes_on(
    column: str, held: object, tmp_path: pathlib.Path
) -> None:
    """Whatever part of a row the pages can't read, a start adds no update to it, and the
    requests after it still have their replies taken in."""
    path = tmp_path / "record.sqlite3"
    store = HelpRequestsStore.open(path, fixture_clock())
    damaged = store.ask(PLAN_DATE).request_id
    store.accept(damaged, "Synthetic zebra words")
    later = store.ask(PLAN_DATE).request_id
    store.accept(later, "Synthetic on it")
    store._connection.execute(
        f"UPDATE help_requests SET {column} = CAST(? AS TEXT) WHERE request_id = ?",  # noqa: S608
        (held, damaged),
    )
    store._connection.execute(
        "UPDATE help_requests SET response = ? WHERE request_id = ?",
        ("Synthetic words saved elsewhere", later),
    )
    store._connection.commit()
    before = held_raw(store, damaged)
    store.close()

    again = HelpRequestsStore.open(path, fixture_clock())
    after = held_raw(again, damaged)
    taken_in = again.get(later)
    again.close()

    assert after == before
    assert bodies(taken_in) == ["Synthetic on it", "Synthetic words saved elsewhere"]


@pytest.mark.parametrize(
    ("updates", "kept"),
    [
        pytest.param(
            an_update(
                ITS_ID, ("body", '"Synthetic first"'), ("body", '"Synthetic zebra words"'), NO_TIME
            ),
            ["Synthetic zebra words"],
            id="words held twice in one update",
        ),
        pytest.param(
            an_update(ITS_ID, ("body", '"Synthetic on it"'), NO_TIME, ("color", '"teal"')),
            ["Synthetic on it", "Synthetic zebra words"],
            id="a key the pages don't use",
        ),
    ],
)
def test_a_start_reads_the_updates_as_the_pages_read_them(
    updates: str, kept: list[str], tmp_path: pathlib.Path
) -> None:
    """An update that holds its words twice reads as the last of them, on the pages and at a
    start, so a reply that is those words adds nothing; a key the pages don't use is passed
    over, and a reply no update holds becomes the latest, once."""
    path = tmp_path / "record.sqlite3"
    store = HelpRequestsStore.open(path, fixture_clock())
    asked = store.ask(PLAN_DATE).request_id
    store.accept(asked, "Synthetic zebra words")
    store._connection.execute("UPDATE help_requests SET parent_updates = ?", (updates,))
    store._connection.commit()
    store.close()

    first = HelpRequestsStore.open(path, fixture_clock())
    once = first.get(asked)
    first.close()
    second = HelpRequestsStore.open(path, fixture_clock())
    twice = second.get(asked)
    second.close()

    assert bodies(once) == kept
    assert twice == once


def test_the_json_reply_is_the_latest_updates_words_whatever_its_column_holds() -> None:
    """A build that reads only the reply can leave that column holding words no update holds
    as kept, spaces alone after a close with none, say: every JSON answer gives the latest
    update's words, as both pages show them."""
    with browser() as client:
        request_id = client.post("/student/help-requests").json()["request"]["request_id"]
        client.post(f"/parent/help-requests/{request_id}/accept", json={"response": "on it"})
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        with state.help_requests._lock:
            state.help_requests._connection.execute("UPDATE help_requests SET response = '   '")
            state.help_requests._connection.commit()
        theirs = client.get("/parent/help-requests").json()
        hers = client.get("/student/help-requests").json()

    assert [item["response"] for item in theirs] == ["on it"]
    assert [item["response"] for item in hers] == ["on it"]


# ------------------------------------------------------------- the routes


def test_asking_over_json_is_answered_with_the_request_as_kept() -> None:
    with browser() as client:
        response = client.post("/student/help-requests", json={"note": "the essay outline"})
        hers = client.get("/student/help-requests").json()
        parents = client.get("/parent/help-requests").json()

    assert response.status_code == 201
    body = response.json()
    assert body["principal"] == "STUDENT"
    assert body["request"]["state"] == "requested"
    assert body["request"]["note"] == "the essay outline"
    assert body["request"]["evening"] == "2026-08-19"
    assert [item["request_id"] for item in hers] == [body["request"]["request_id"]]
    assert parents == hers


def test_a_parent_takes_it_up_and_resolves_it_and_she_sees_each_step() -> None:
    with browser() as client:
        request_id = client.post("/student/help-requests").json()["request"]["request_id"]
        not_seen = client.get(PAGE).text
        taken_up = client.post(
            f"/parent/help-requests/{request_id}/accept", json={"response": "coming"}
        )
        on_it = client.get(PAGE).text
        resolved = client.post(
            f"/parent/help-requests/{request_id}/resolve", json={"response": "all sorted"}
        )
        after = client.get(PAGE).text
        again = client.post(f"/parent/help-requests/{request_id}/resolve")
        with_words = client.post(
            f"/parent/help-requests/{request_id}/resolve", json={"response": "and more"}
        )

    assert WAITING in not_seen
    assert taken_up.status_code == 200
    assert taken_up.json()["state"] == "accepted"
    assert HELPING in on_it
    assert help_reply(on_it) == "coming"
    assert resolved.json()["state"] == "resolved"
    assert re.search(CLOSED_ON, after)
    assert help_reply(after) == "all sorted"
    assert [said for _, said, _ in help_updates(after)] == ["all sorted", "coming"]
    assert '<a href="#help-updates">Help updates (1)</a>' in after
    assert again.status_code == 200
    assert again.json() == resolved.json()
    assert with_words.status_code == 409
    assert with_words.json()["detail"] == ALREADY_CLOSED


def test_she_can_take_a_request_back_only_while_nobody_has_taken_it_up() -> None:
    with browser() as client:
        first = client.post("/student/help-requests").json()["request"]["request_id"]
        second = client.post("/student/help-requests").json()["request"]["request_id"]
        client.post(f"/parent/help-requests/{second}/accept")
        taken_back = client.delete(f"/student/help-requests/{first}")
        refused = client.delete(f"/student/help-requests/{second}")
        gone = client.delete(f"/student/help-requests/{first}")
        listed = client.get("/student/help-requests").json()

    assert taken_back.status_code == 204
    assert refused.status_code == 409
    assert gone.status_code == 404
    assert [item["request_id"] for item in listed] == [second]


def test_an_unknown_request_and_a_move_that_is_not_one_of_the_three_are_refused() -> None:
    with browser() as client:
        unknown = client.post("/parent/help-requests/nobody/accept")
        unknown_update = client.post(
            "/parent/help-requests/nobody/update", json={"response": "Synthetic"}
        )
        request_id = client.post("/student/help-requests").json()["request"]["request_id"]
        odd_move = client.post(f"/parent/actions/help/{request_id}", data={"step": "shrug"})

    assert unknown.status_code == 404
    assert unknown_update.status_code == 404
    assert odd_move.status_code == 422
    assert "&#39;shrug&#39; is not one of the three moves, accept, update or resolve." in (
        odd_move.text
    )


def test_her_note_is_capped_at_the_boundary() -> None:
    with browser() as client:
        over_json = client.post(
            "/student/help-requests", json={"note": "w" * (NOTE_MAX_LENGTH + 1)}
        )
        over_form = client.post(ASK, data={**ask_form(client), "note": "w" * (NOTE_MAX_LENGTH + 1)})
        at_cap = client.post(ASK, data={**ask_form(client), "note": "w" * NOTE_MAX_LENGTH})
        word_back = client.post(
            "/parent/help-requests/nobody/resolve", json={"response": "w" * (NOTE_MAX_LENGTH + 1)}
        )

    assert over_json.status_code == 422
    assert over_form.status_code == 422
    assert f"A note is at most {NOTE_MAX_LENGTH} characters" in over_form.text
    assert at_cap.status_code == 303
    assert word_back.status_code == 422


def test_a_parents_word_back_is_capped_on_the_form_too() -> None:
    with browser() as client:
        request_id = client.post("/student/help-requests").json()["request"]["request_id"]
        over = client.post(
            f"/parent/actions/help/{request_id}",
            data={"step": "accept", "response": "w" * (NOTE_MAX_LENGTH + 1)},
        )
        still_requested = client.get("/parent/help-requests").json()[0]["state"]
        at_cap = client.post(
            f"/parent/actions/help/{request_id}",
            data={"step": "accept", "response": "w" * NOTE_MAX_LENGTH},
        )

    assert over.status_code == 422
    assert f"A reply is at most {NOTE_MAX_LENGTH} characters" in over.text
    assert "<h1>Family review</h1>" in over.text
    assert still_requested == "requested"
    assert at_cap.status_code == 303


TYPED_REPLY = "Synthetic reply: page 12 has <the steps> & the answers"
KEPT_UNDER_THE_PROBLEM = '<label for="kept-reply">Reply, as typed</label>'


@pytest.mark.parametrize(
    ("case", "status_code"),
    [
        ("a reply too long", 422),
        ("a step no page sends", 422),
        ("a request closed meanwhile", 409),
        ("no such request", 404),
    ],
)
def test_a_refused_reply_comes_back_with_the_family_page(case: str, status_code: int) -> None:
    """A take-up or a close the family page refuses writes nothing and keeps the reply as
    typed: in its own field while the request is still open there, marked and focused when
    the reply is what was refused; and under the problem when the request is closed or gone,
    to copy."""
    typed = TYPED_REPLY + " x" * 250 if case == "a reply too long" else TYPED_REPLY
    with browser() as client:
        request_id = client.post("/student/help-requests").json()["request"]["request_id"]
        client.post(f"/parent/help-requests/{request_id}/accept", json={"response": "Synthetic"})
        if case == "a request closed meanwhile":
            client.post(f"/parent/help-requests/{request_id}/resolve")
        named = "0" * 32 if case == "no such request" else request_id
        step = "sideways" if case == "a step no page sends" else "resolve"
        before = requests_held(client)
        answer = client.post(
            f"/parent/actions/help/{named}", data={"step": step, "response": typed}
        )
        after = requests_held(client)

    page = answer.text
    field = re.search(rf'<textarea id="reply-{request_id}"[^>]*>(.*?)</textarea>', page, re.S)
    problem = re.search(r'<p class="problem" role="alert" id="problem">(.*?)</p>', page, re.S)
    assert answer.status_code == status_code
    assert after == before
    assert "<h1>Family review</h1>" in page
    assert problem is not None
    assert page.count(str(escape(typed))) == 1
    if case in ("a reply too long", "a step no page sends"):
        assert field is not None
        assert field.group(1) == str(escape(typed))
        assert KEPT_UNDER_THE_PROBLEM not in page
        opening = field.group(0).split(">", 1)[0]
        refused_words = case == "a reply too long"
        assert ('aria-invalid="true"' in opening) is refused_words
        assert (" autofocus" in opening) is refused_words
        assert ('aria-describedby="problem' in opening) is refused_words
        go = f'<a href="#reply-{request_id}">Go to the field.</a>'
        assert problem.group(1).endswith(go) is refused_words
    else:
        assert (field is None) is (case == "a request closed meanwhile")
        assert field is None or field.group(1) == ""
        assert problem.group(1) == str(escape(answer_detail(case, named)))
        kept = page.split(problem.group(0), 1)[1]
        assert kept.lstrip().startswith(KEPT_UNDER_THE_PROBLEM)
        assert f'<textarea id="kept-reply" rows="3" readonly>{escape(typed)}</textarea>' in kept


def answer_detail(case: str, named: str) -> str:
    """What the JSON route answers for a refused step, which the family page says as it is."""
    if case == "no such request":
        return f"no help request {named!r}"
    return ALREADY_CLOSED


def test_a_reply_counts_a_line_break_once_as_the_field_does() -> None:
    """A browser sends each line break of a reply as two characters; the field and the cap
    count it as one, and the reply is kept with one kind of line ending."""
    at_cap = "w" * 249 + "\n" + "w" * 250
    over = "w" * 250 + "\n" + "w" * 250
    with browser() as client:
        first = client.post("/student/help-requests").json()["request"]["request_id"]
        second = client.post("/student/help-requests").json()["request"]["request_id"]
        kept = client.post(
            f"/parent/actions/help/{first}",
            data=as_a_browser_sends({"step": "accept", "response": at_cap}),
        )
        refused = client.post(
            f"/parent/actions/help/{second}",
            data=as_a_browser_sends({"step": "accept", "response": over}),
        )
        listed = {item["request_id"]: item for item in client.get(HELP_JSON).json()}

    assert kept.status_code == 303
    assert listed[first]["response"] == at_cap
    assert refused.status_code == 422
    assert f"A reply is at most {NOTE_MAX_LENGTH} characters; this one is 501." in refused.text
    assert listed[second]["state"] == "requested"
    assert listed[second]["response"] is None


# --------------------------------------------------------------- the pages


def test_her_page_offers_the_press_and_then_lists_the_request_with_a_way_back() -> None:
    with browser() as client:
        before = client.get(PAGE).text
        asked = client.post(ASK, data={**ask_form(client), "note": "  the outline  "})
        after = client.get(PAGE).text
        request_id = client.get("/student/help-requests").json()[0]["request_id"]
        taken_back = client.post(f"/student/actions/take-back-help/{request_id}")
        again = client.get(PAGE).text

    yours = '<p class="help-label">Your request</p>'
    assert 'action="/student/actions/ask-for-help"' in before
    assert yours not in before
    assert asked.status_code == 303
    assert asked.headers["location"] == f"{PAGE}?asked={request_id}#help-result"
    assert yours in after
    assert '<q class="authored-text">the outline</q>' in after
    assert WAITING in after
    assert f'action="/student/actions/take-back-help/{request_id}"' in after
    assert 'aria-label="Take it back: your request from ' in after
    assert ">Take it back</button>" in after
    assert 'aria-label="Take back' not in after
    assert taken_back.status_code == 303
    assert yours not in again


def test_the_parents_page_lists_what_is_open_and_moves_it_with_two_buttons() -> None:
    with browser() as client:
        client.post("/student/help-requests", json={"note": "the outline"})
        request_id = client.get("/parent/help-requests").json()[0]["request_id"]
        listed = client.get("/parent").text
        taken_up = client.post(
            f"/parent/actions/help/{request_id}", data={"step": "accept", "response": "coming"}
        )
        on_it = client.get("/parent").text
        resolved = client.post(
            f"/parent/actions/help/{request_id}", data={"step": "resolve", "response": "sorted"}
        )
        after = client.get("/parent").text
        hers = client.get(PAGE).text

    def help_of(page: str) -> str:
        return page.split('<section id="help-she-asked-for"', 1)[1].split("</section>", 1)[0]

    assert "<h2>Help she asked for</h2>" in listed
    assert '<p class="help-label">Student\'s request</p>' in help_of(listed)
    assert '<q class="authored-text">the outline</q>' in help_of(listed)
    assert WAITING in help_of(listed)
    assert "Her page says" not in listed
    assert 'value="accept"' in listed
    assert taken_up.status_code == 303
    assert HELPING in help_of(on_it)
    assert help_reply(help_of(on_it)) == "coming"
    assert "Reply so far" not in on_it
    assert 'value="accept"' not in on_it
    assert resolved.status_code == 303
    assert "No open help requests." in after
    assert "<summary>Closed in the last two weeks</summary>" in help_of(after)
    assert re.search(CLOSED_ON, help_of(after))
    assert help_reply(help_of(after)) == "sorted"
    assert "Resolved" not in help_of(after)
    assert re.search(CLOSED_ON, hers)
    assert help_reply(hers) == "sorted"


def test_taking_back_a_request_that_is_not_there_is_said_on_her_page() -> None:
    with browser() as client:
        response = client.post("/student/actions/take-back-help/nobody")

    assert response.status_code == 404
    assert "That request is not here any more; nothing was changed." in response.text
    assert "<h1>My week</h1>" in response.text


def test_taking_back_a_request_a_parent_has_taken_up_is_said_on_her_page() -> None:
    with browser() as client:
        request_id = client.post("/student/help-requests").json()["request"]["request_id"]
        client.post(f"/parent/help-requests/{request_id}/accept")
        response = client.post(f"/student/actions/take-back-help/{request_id}")

    assert response.status_code == 409
    assert "cannot be taken back" in response.text
    assert "<h1>My week</h1>" in response.text


def test_requests_live_in_the_drafts_file_stamped_by_the_real_clock() -> None:
    with browser() as client:
        client.post("/student/help-requests")
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        held = state.help_requests.open_requests()

    assert len(held) == 1
    assert held[0].evening == PLAN_DATE
    assert held[0].asked_at.date() > PLAN_DATE


# ------------------------------------------------------------------ hers to ask, once per form

ALREADY_SENT = "That request is already saved."
FORM_USED = "This form was already used. Open a new help form to ask again."
FORM_SENT_OTHER_WORDS = "This form already sent a request"


def requests_held(client: TestClient) -> list[tuple[object, ...]]:
    state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
    found = state.help_requests._connection.execute("SELECT * FROM help_requests ORDER BY 1")
    return [tuple(row) for row in found.fetchall()]


def signed_in(tmp_path: pathlib.Path) -> TestClient:
    client = TestClient(
        create_app(signed_in_household(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    )
    client.__enter__()
    client.post("/sign-in", data={"passphrase": HERS})
    return client


def as_a_parent(client: TestClient) -> None:
    client.post("/sign-out")
    client.post("/sign-in", data={"passphrase": THEIRS})


def posted(client: TestClient, fields: list[tuple[str, str]]) -> Answer:
    return client.post(ASK, content=urlencode(fields), headers={"Content-Type": FORM_TYPE})


def test_a_parent_can_not_ask_or_take_back_in_her_name(tmp_path: pathlib.Path) -> None:
    client = signed_in(tmp_path)
    try:
        form = ask_form(client)
        hers = client.post(ASK, data={**ask_form(client), "note": "hers"})
        request_id = client.get("/student/help-requests").json()[0]["request_id"]
        as_a_parent(client)
        before = requests_held(client)
        page = client.get(PAGE).text
        asked = client.post(ASK, data={**form, "note": "from a parent"})
        asked_json = client.post("/student/help-requests", json={"note": "from a parent"})
        taken_back = client.post(f"/student/actions/take-back-help/{request_id}")
        taken_back_json = client.delete(f"/student/help-requests/{request_id}")
        after = requests_held(client)
    finally:
        client.__exit__(None, None, None)

    assert hers.status_code == 303
    for refused in (asked, asked_json, taken_back, taken_back_json):
        assert refused.status_code == 403
    assert after == before
    assert f'action="{ASK}"' not in page
    assert "/student/actions/take-back-help/" not in page
    assert '<q class="authored-text">hers</q>' in page
    assert 'href="/parent#help-she-asked-for"' in page


HELP_JSON = "/student/help-requests"
JSON_TYPE = {"Content-Type": "application/json"}
BROKEN_FORM = {"Content-Type": "multipart/form-data; boundary=synthetic"}
BROKEN_BODY = b"--synthetic\r\nContent-Disposition: form-data\r\n\r\nno end"
OVERLONG = b'{"note": "' + b"x" * (NOTE_MAX_LENGTH + 1) + b'"}'


async def unread(*args: object, **kwargs: object) -> None:
    msg = "the body was read before the reader was known"
    raise AssertionError(msg)


@pytest.mark.parametrize(
    ("where", "body", "headers"),
    [
        (ASK, BROKEN_BODY, BROKEN_FORM),
        (ASK, b"note=from+a+parent&note=twice", {"Content-Type": FORM_TYPE}),
        (ASK, b"", {"Content-Type": FORM_TYPE}),
        (HELP_JSON, b"{not json", JSON_TYPE),
        (HELP_JSON, OVERLONG, JSON_TYPE),
        (HELP_JSON, b'{"request_id": "not-an-id"}', JSON_TYPE),
        (HELP_JSON, b'{"status": "done"}', JSON_TYPE),
        (HELP_JSON, b'{"note": "from a parent"}', {"Content-Type": "text/plain"}),
        (HELP_JSON, b"", {}),
        (HELP_JSON, b"\xff\xfe{", JSON_TYPE),
    ],
    ids=[
        "form broken multipart",
        "form note twice",
        "form empty",
        "json malformed",
        "json overlong",
        "json bad id",
        "json unknown field",
        "json as plain text",
        "json empty",
        "json not utf-8",
    ],
)
def test_a_parent_is_refused_before_the_body_is_read(
    where: str,
    body: bytes,
    headers: dict[str, str],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = signed_in(tmp_path)
    try:
        as_a_parent(client)
        before = requests_held(client)
        monkeypatch.setattr(student_routes, "fields_of", unread)
        refused = client.post(where, content=body, headers=headers)
        monkeypatch.undo()
        after = requests_held(client)
    finally:
        client.__exit__(None, None, None)

    assert refused.status_code == 403
    assert "Sign in as the student to ask for help or take a request back." in refused.text
    assert after == before


@pytest.mark.parametrize(
    ("body", "headers", "status_code", "problem"),
    [
        (b"", {}, 201, None),
        (b"null", JSON_TYPE, 201, None),
        (b'{"note": "hers"}', JSON_TYPE, 201, None),
        (b'{"note": "hers"}', {"Content-Type": "application/json; charset=utf-8"}, 201, None),
        (b'{"note": "hers"}', {"Content-Type": "application/problem+json"}, 201, None),
        (b'{"note": "hers"}', {}, 422, ("model_attributes_type", ["body"])),
        (b"{not json", JSON_TYPE, 422, ("json_invalid", ["body", 1])),
        (OVERLONG, JSON_TYPE, 422, ("string_too_long", ["body", "note"])),
        (b'{"status": "done"}', JSON_TYPE, 422, ("extra_forbidden", ["body", "status"])),
        (
            b'{"request_id": "not-an-id"}',
            JSON_TYPE,
            422,
            ("string_pattern_mismatch", ["body", "request_id"]),
        ),
        (
            b'{"note": "hers"}',
            {"Content-Type": "text/plain"},
            422,
            ("model_attributes_type", ["body"]),
        ),
        (b"\xff\xfe{", JSON_TYPE, 400, None),
    ],
    ids=[
        "empty",
        "null",
        "json",
        "json with charset",
        "a json subtype",
        "no content type",
        "malformed",
        "overlong",
        "unknown field",
        "bad id",
        "plain text",
        "not utf-8",
    ],
)
def test_her_json_ask_reads_its_body_as_the_framework_does(
    body: bytes,
    headers: dict[str, str],
    status_code: int,
    problem: tuple[str, list[str | int]] | None,
    tmp_path: pathlib.Path,
) -> None:
    client = signed_in(tmp_path)
    try:
        answer = client.post(HELP_JSON, content=body, headers=headers)
        held = requests_held(client)
    finally:
        client.__exit__(None, None, None)

    assert answer.status_code == status_code
    assert len(held) == (1 if status_code == 201 else 0)
    if problem is not None:
        first = answer.json()["detail"][0]
        assert (first["type"], first["loc"]) == problem


@pytest.mark.parametrize("state", ["requested", "accepted"])
def test_she_can_not_take_up_update_or_close_her_own_request(
    state: str, tmp_path: pathlib.Path
) -> None:
    client = signed_in(tmp_path)
    try:
        client.post(ASK, data={**ask_form(client), "note": "hers"})
        request_id = client.get("/student/help-requests").json()[0]["request_id"]
        if state == "accepted":
            as_a_parent(client)
            client.post(f"/parent/help-requests/{request_id}/accept", json={"response": "on it"})
            client.post("/sign-out")
            client.post("/sign-in", data={"passphrase": HERS})
        before = requests_held(client)
        words = {"response": "Synthetic words of hers", "update_id": uuid4().hex}
        answers = [
            client.post(f"/parent/actions/help/{request_id}", data={**words, "step": step})
            for step in ("accept", "update", "resolve")
        ] + [
            client.post(f"/parent/help-requests/{request_id}/{step}", json=words)
            for step in ("accept", "update", "resolve")
        ]
        after = requests_held(client)
    finally:
        client.__exit__(None, None, None)

    assert [answer.status_code for answer in answers] == [403] * 6
    assert after == before


FIRST = ("note", "First synthetic words")
NOT_WHOLE_SAID = (
    "That form carried a field twice, left one out, or had one this page doesn't send, "
    "so nothing was sent."
)


@pytest.mark.parametrize(
    "shape",
    ["note twice", "no id", "id twice", "bad id", "unknown field"],
)
def test_an_ask_form_that_is_not_whole_sends_nothing_and_keeps_her_first_words(
    shape: str,
) -> None:
    with browser() as client:
        form_id = ask_form(client)["request_id"]
        fields = {
            "note twice": [FIRST, ("note", "Second synthetic words"), ("request_id", form_id)],
            "no id": [FIRST],
            "id twice": [FIRST, ("request_id", form_id), ("request_id", form_id)],
            "bad id": [FIRST, ("request_id", "not-an-id")],
            "unknown field": [FIRST, ("request_id", form_id), ("channel", "LMS")],
        }[shape]
        answer = posted(client, fields)
        again = whole_form(answer.text, ASK)
        held = requests_held(client)

    assert answer.status_code == 422
    assert held == []
    assert str(escape(NOT_WHOLE_SAID)) in answer.text
    assert again["note"] == "First synthetic words"
    assert "Second synthetic words" not in answer.text
    assert again["request_id"] not in ("", "not-an-id", form_id)


def test_a_note_sent_as_a_file_sends_nothing_and_shows_none_of_it() -> None:
    with browser() as client:
        form = ask_form(client)
        answer = client.post(
            ASK,
            data={"request_id": form["request_id"]},
            files={"note": ("note.txt", b"file words", "text/plain")},
        )
        held = requests_held(client)

    assert answer.status_code == 422
    assert "file words" not in answer.text
    assert str(escape(NOT_WHOLE_SAID)) in answer.text
    assert held == []


def test_an_overlong_note_keeps_her_words_by_the_field() -> None:
    words = "Zebra quartz " * 40 + "violin"
    with browser() as client:
        answer = client.post(ASK, data={**ask_form(client), "note": words})
        held = requests_held(client)
    start = answer.text.index('id="help-note"')
    field = answer.text[answer.text.rindex("<input", 0, start) : answer.text.index(">", start)]

    assert answer.status_code == 422
    assert held == []
    assert f'value="{words}"' in field
    assert 'aria-invalid="true"' in field
    assert "help-problem" in field
    assert "autofocus" in field
    assert f"A note is at most {NOTE_MAX_LENGTH} characters" in answer.text


def test_a_send_the_file_refuses_keeps_her_words(monkeypatch: pytest.MonkeyPatch) -> None:
    with browser() as client:
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]

        def refuses(*_: object, **__: object) -> None:
            msg = "disk I/O error"
            raise sqlite3.OperationalError(msg)

        form = ask_form(client)
        monkeypatch.setattr(state.help_requests, "ask_once", refuses)
        answer = client.post(ASK, data={**form, "note": "Zebra quartz violin"})
        monkeypatch.undo()
        held = requests_held(client)

    assert answer.status_code == 500
    assert 'value="Zebra quartz violin"' in answer.text
    assert "could not be sent" in answer.text
    assert held == []


def test_the_same_form_sent_again_makes_one_request() -> None:
    with browser() as client:
        form = {**ask_form(client), "note": "the outline"}
        first = client.post(ASK, data=form)
        again = client.post(ASK, data=form)
        landed = client.get(again.headers["location"]).text
        held = requests_held(client)

    assert first.status_code == 303
    assert again.status_code == 303
    assert len(held) == 1
    assert landed.count(ALREADY_SENT) == 1


def test_the_same_form_after_a_parent_took_it_up_shows_it_as_it_stands() -> None:
    with browser() as client:
        form = {**ask_form(client), "note": "the outline"}
        client.post(ASK, data=form)
        request_id = client.get("/student/help-requests").json()[0]["request_id"]
        client.post(f"/parent/help-requests/{request_id}/accept", json={"response": "After dinner"})
        before = requests_held(client)
        again = client.post(ASK, data=form)
        landed = client.get(again.headers["location"]).text
        after = requests_held(client)

    assert again.status_code == 303
    assert after == before
    assert ALREADY_SENT in landed
    assert HELPING in landed


def test_the_same_form_with_other_words_is_refused_and_keeps_them() -> None:
    with browser() as client:
        form = ask_form(client)
        client.post(ASK, data={**form, "note": "the outline"})
        refused = client.post(ASK, data={**form, "note": "the conclusion too"})
        again = whole_form(refused.text, ASK)
        held = requests_held(client)

    assert refused.status_code == 409
    assert FORM_SENT_OTHER_WORDS in refused.text
    assert again["note"] == "the conclusion too"
    assert again["request_id"] != form["request_id"]
    assert len(held) == 1


def test_a_form_whose_request_was_taken_back_asks_nothing_again() -> None:
    with browser() as client:
        form = {**ask_form(client), "note": "the outline"}
        client.post(ASK, data=form)
        request_id = client.get("/student/help-requests").json()[0]["request_id"]
        client.post(f"/student/actions/take-back-help/{request_id}")
        replayed = client.post(ASK, data=form)
        again = whole_form(replayed.text, ASK)
        held = requests_held(client)

    assert replayed.status_code == 409
    assert FORM_USED in replayed.text
    assert again["note"] == "the outline"
    assert again["request_id"] != form["request_id"]
    assert held == []


def test_a_fresh_form_with_the_same_words_asks_again() -> None:
    with browser() as client:
        client.post(ASK, data={**ask_form(client), "note": "the outline"})
        client.post(ASK, data={**ask_form(client), "note": "the outline"})
        held = requests_held(client)

    assert len(held) == 2


def test_json_with_an_id_asks_once_and_without_one_asks_every_time() -> None:
    with browser() as client:
        form_id = ask_form(client)["request_id"]
        made = client.post("/student/help-requests", json={"note": "json", "request_id": form_id})
        same = client.post("/student/help-requests", json={"note": "json", "request_id": form_id})
        other = client.post("/student/help-requests", json={"note": "else", "request_id": form_id})
        malformed = client.post("/student/help-requests", json={"request_id": "not-an-id"})
        client.delete(f"/student/help-requests/{form_id}")
        spent = client.post("/student/help-requests", json={"note": "json", "request_id": form_id})
        plain = [client.post("/student/help-requests", json={"note": "again"}) for _ in range(2)]
        held = requests_held(client)

    assert (made.status_code, same.status_code) == (201, 200)
    assert same.json()["request"]["request_id"] == form_id
    assert (other.status_code, malformed.status_code, spent.status_code) == (409, 422, 409)
    assert [answer.status_code for answer in plain] == [201, 201]
    assert len(held) == 2


# ------------------------------------------------------------------ a parent's updates, on the page

FAMILY = "/parent"
PROBLEM_LINE = re.compile(r'<p class="problem" role="alert" id="problem">(.*?)</p>', re.S)


def family_form(page: str, request_id: str) -> dict[str, str]:
    """What the family page's form for one request sends with no button pressed: the words
    in its box and the one-time id the page gave it."""
    return whole_form(page, f"/parent/actions/help/{request_id}")


def press(client: TestClient, page: str, request_id: str, step: str, words: str = "") -> Answer:
    """One button under a request, pressed on ``page`` with ``words`` in its box."""
    fields = {**family_form(page, request_id), "step": step, "response": words}
    return client.post(f"/parent/actions/help/{request_id}", data=fields)


def asked_for(client: TestClient, note: str = "Synthetic question") -> str:
    made = client.post("/student/help-requests", json={"note": note}).json()
    return str(made["request"]["request_id"])


def listed(client: TestClient) -> dict[str, dict[str, Any]]:
    """Her requests as the family's JSON route lists them, by id."""
    return {item["request_id"]: item for item in client.get("/parent/help-requests").json()}


def updates_of(client: TestClient, request_id: str) -> list[str]:
    return [update["body"] for update in listed(client)[request_id]["updates"]]


def field_of(page: str, request_id: str) -> tuple[str, str]:
    """A request's box on the family page: its opening tag, and the words in it."""
    found = re.search(rf'(<textarea id="reply-{request_id}"[^>]*>)(.*?)</textarea>', page, re.S)
    assert found is not None, request_id
    return found.group(1), found.group(2)


def test_a_parent_takes_a_request_up_adds_updates_and_closes_it_from_the_page() -> None:
    """Taken up without words, two updates, and a close with final words: each a press of its
    own, the request open until the close, every message kept, and her week showing the
    latest with the earlier ones folded under it."""
    with browser() as client:
        request_id = asked_for(client)
        waiting = client.get(FAMILY).text
        taken = press(client, waiting, request_id, "accept")
        first_page = client.get(FAMILY).text
        first = press(client, first_page, request_id, "update", "Synthetic first update")
        second_page = client.get(FAMILY).text
        second = press(client, second_page, request_id, "update", "Synthetic second update")
        open_still = listed(client)[request_id]["state"]
        closing_page = client.get(FAMILY).text
        closed = press(client, closing_page, request_id, "resolve", "Synthetic last words")
        kept = listed(client)[request_id]
        hers = help_row(client.get(PAGE).text, request_id)

    ids = {
        family_form(page, request_id)["update_id"]
        for page in (waiting, first_page, second_page, closing_page)
    }
    assert [answer.status_code for answer in (taken, first, second, closed)] == [303] * 4
    assert open_still == "accepted"
    assert kept["state"] == "resolved"
    assert [update["body"] for update in kept["updates"]] == [
        "Synthetic first update",
        "Synthetic second update",
        "Synthetic last words",
    ]
    assert kept["response"] == "Synthetic last words"
    assert len(ids) == 4
    assert all(re.fullmatch(r"[0-9a-f]{32}", form_id) for form_id in ids)
    assert [said for _, said, _ in help_updates(hers)] == [
        "Synthetic last words",
        "Synthetic first update",
        "Synthetic second update",
    ]
    assert "<summary>Earlier updates (2)</summary>" in hers


@pytest.mark.parametrize("step", ["accept", "update", "resolve"])
def test_the_same_form_sent_again_from_the_page_changes_nothing(step: str) -> None:
    with browser() as client:
        request_id = asked_for(client)
        if step == "update":
            client.post(f"/parent/help-requests/{request_id}/accept", json={})
        page = client.get(FAMILY).text
        first = press(client, page, request_id, step, "Synthetic words")
        before = requests_held(client)
        again = press(client, page, request_id, step, "Synthetic words")
        after = requests_held(client)

    assert (first.status_code, again.status_code) == (303, 303)
    assert again.headers["location"] == first.headers["location"]
    assert after == before


@pytest.mark.parametrize(
    "words", ["", "Synthetic on it", "Synthetic new words"], ids=["none", "on record", "new"]
)
def test_i_can_help_from_another_page_left_open_adds_nothing_and_keeps_new_words(
    words: str,
) -> None:
    """A second I can help never adds a message. Words already on record, or none, change
    nothing and say nothing; new words are kept in the request's Add an update box, a fresh
    form's, with the reason at the top and a way to the box."""
    with browser() as client:
        request_id = asked_for(client)
        mine, theirs = client.get(FAMILY).text, client.get(FAMILY).text
        assert press(client, mine, request_id, "accept", "Synthetic on it").status_code == 303
        before = requests_held(client)
        answer = press(client, theirs, request_id, "accept", words)
        after = requests_held(client)
        hers = help_row(client.get(PAGE).text, request_id)

    assert after == before
    assert [said for _, said, _ in help_updates(hers)] == ["Synthetic on it"]
    if words != "Synthetic new words":
        assert answer.status_code == 303
        return
    page = answer.text
    problem = PROBLEM_LINE.search(page)
    opening, inside = field_of(page, request_id)
    assert answer.status_code == 409
    assert problem is not None
    assert problem.group(1) == (
        f'{escape(ALREADY_TAKEN_UP)} <a href="#reply-{request_id}">Go to the field.</a>'
    )
    assert inside == str(escape(words))
    assert page.count(str(escape(words))) == 1
    assert f'<label for="reply-{request_id}">Add an update</label>' in page
    assert 'aria-describedby="problem"' in opening
    assert "aria-invalid" not in opening
    assert " autofocus" not in opening
    fresh = family_form(page, request_id)["update_id"]
    used = {family_form(earlier, request_id)["update_id"] for earlier in (mine, theirs)}
    assert fresh not in used


def test_updates_from_two_pages_left_open_are_both_kept_in_the_order_they_came() -> None:
    with browser() as client:
        request_id = asked_for(client)
        client.post(f"/parent/help-requests/{request_id}/accept", json={})
        first_page, second_page = client.get(FAMILY).text, client.get(FAMILY).text
        second = press(client, second_page, request_id, "update", "Synthetic from the second")
        first = press(client, first_page, request_id, "update", "Synthetic from the first")
        kept = updates_of(client, request_id)

    assert (second.status_code, first.status_code) == (303, 303)
    assert kept == ["Synthetic from the second", "Synthetic from the first"]


def test_a_close_from_a_page_left_open_keeps_the_update_sent_since() -> None:
    with browser() as client:
        request_id = asked_for(client)
        client.post(f"/parent/help-requests/{request_id}/accept", json={"response": "Synthetic"})
        left_open, newer = client.get(FAMILY).text, client.get(FAMILY).text
        added = press(client, newer, request_id, "update", "Synthetic update")
        closed = press(client, left_open, request_id, "resolve", "Synthetic last")
        kept = listed(client)[request_id]

    assert (added.status_code, closed.status_code) == (303, 303)
    assert kept["state"] == "resolved"
    assert [update["body"] for update in kept["updates"]] == [
        "Synthetic",
        "Synthetic update",
        "Synthetic last",
    ]


REFUSED_ON_THE_PAGE = {
    "an update to a request closed meanwhile": (
        "update",
        409,
        CLOSED_BEFORE_UPDATE,
        "under the problem",
    ),
    "I can help on a request closed meanwhile": (
        "accept",
        409,
        ALREADY_CLOSED,
        "under the problem",
    ),
    "a close with words of a request closed meanwhile": (
        "resolve",
        409,
        ALREADY_CLOSED,
        "under the problem",
    ),
    "I can help again with new words": ("accept", 409, ALREADY_TAKEN_UP, "below"),
    "an update form sent again with other words": (
        "update",
        409,
        FORM_ADDED_OTHER_WORDS,
        "below",
    ),
    "a close from a form that added other words": (
        "resolve",
        409,
        FORM_ADDED_OTHER_WORDS,
        "below",
    ),
    "an update to a request nobody has taken up": ("update", 409, NOT_TAKEN_UP_YET, "below"),
    "an update with no words": ("update", 422, UPDATE_NEEDS_WORDS, "marked"),
    "a form whose id is not one": ("accept", 422, BAD_FORM, "in the field"),
}
"""Each refusal: the button pressed, the status, what the page says, and where the words go."""


@pytest.mark.parametrize("case", list(REFUSED_ON_THE_PAGE))
def test_every_refusal_of_a_parents_words_writes_nothing_and_keeps_them(case: str) -> None:
    """Whatever the family page refuses, it writes nothing and keeps the words as typed: in
    the request's own box while the request is open there, with a way to it when the reason
    is about them, and under the reason when the request is closed. A blank update marks its
    empty box."""
    step, status_code, said, where = REFUSED_ON_THE_PAGE[case]
    typed = "" if case == "an update with no words" else TYPED_REPLY
    with browser() as client:
        request_id = asked_for(client)
        page = client.get(FAMILY).text
        if case != "an update to a request nobody has taken up":
            assert press(client, page, request_id, "accept", "Synthetic on it").status_code == 303
            page = client.get(FAMILY).text
        if "meanwhile" in case:
            client.post(f"/parent/help-requests/{request_id}/resolve")
        if said == FORM_ADDED_OTHER_WORDS:
            assert press(client, page, request_id, "update", "Synthetic update").status_code == 303
        fields = {**family_form(page, request_id), "step": step, "response": typed}
        if case == "a form whose id is not one":
            fields["update_id"] = "not-an-id"
        before = requests_held(client)
        answer = client.post(f"/parent/actions/help/{request_id}", data=fields)
        after = requests_held(client)

    text = answer.text
    problem = PROBLEM_LINE.search(text)
    assert answer.status_code == status_code
    assert after == before
    assert "<h1>Family review</h1>" in text
    assert problem is not None
    go = f' <a href="#reply-{request_id}">Go to the field.</a>'
    pointed = where in ("below", "marked")
    assert problem.group(1) == f"{escape(said)}{go if pointed else ''}"
    if where == "under the problem":
        assert f'id="reply-{request_id}"' not in text
        kept = text.split(problem.group(0), 1)[1]
        assert kept.lstrip().startswith(KEPT_UNDER_THE_PROBLEM)
        assert f'<textarea id="kept-reply" rows="3" readonly>{escape(typed)}</textarea>' in kept
        return
    opening, inside = field_of(text, request_id)
    assert inside == str(escape(typed))
    assert KEPT_UNDER_THE_PROBLEM not in text
    assert ('aria-describedby="problem"' in opening) is pointed
    assert ('aria-invalid="true"' in opening) is (where == "marked")
    assert (" autofocus" in opening) is (where == "marked")
    assert family_form(text, request_id)["update_id"] != fields["update_id"]
    if typed:
        assert text.count(str(escape(typed))) == 1


@pytest.mark.parametrize("step", ["update", "resolve"])
def test_an_update_or_closing_words_count_a_line_break_once_as_the_field_does(step: str) -> None:
    at_cap = "w" * 249 + "\n" + "w" * 250
    over = "w" * 250 + "\n" + "w" * 250
    with browser() as client:
        kept_id, refused_id = asked_for(client), asked_for(client)
        for request_id in (kept_id, refused_id):
            client.post(f"/parent/help-requests/{request_id}/accept", json={})
        page = client.get(FAMILY).text
        kept = client.post(
            f"/parent/actions/help/{kept_id}",
            data=as_a_browser_sends(
                {**family_form(page, kept_id), "step": step, "response": at_cap}
            ),
        )
        refused = client.post(
            f"/parent/actions/help/{refused_id}",
            data=as_a_browser_sends(
                {**family_form(page, refused_id), "step": step, "response": over}
            ),
        )
        held = listed(client)

    assert kept.status_code == 303
    assert [update["body"] for update in held[kept_id]["updates"]] == [at_cap]
    assert refused.status_code == 422
    assert f"A reply is at most {NOTE_MAX_LENGTH} characters; this one is 501." in refused.text
    assert held[refused_id]["updates"] == []
    assert held[refused_id]["state"] == "accepted"


@pytest.mark.parametrize("step", ["accept", "update", "resolve"])
def test_words_sent_as_json_are_kept_as_the_family_page_keeps_them(step: str) -> None:
    """Words at the cap, sent with a carriage return in each line break and spacing at the
    edges, are kept alike as JSON and from the family page, trimmed with one kind of line
    ending; one character more is refused over JSON too, with nothing written."""
    at_cap = "w" * 249 + "\n" + "w" * 250
    over = "w" * 250 + "\n" + "w" * 250

    def padded(words: str) -> str:
        return "  " + words.replace("\n", "\r\n") + " \r\n"

    with browser() as client:
        by_json, by_form, refused_id = (asked_for(client) for _ in range(3))
        if step == "update":
            for request_id in (by_json, by_form, refused_id):
                client.post(f"/parent/help-requests/{request_id}/accept", json={})
        sent = client.post(
            f"/parent/help-requests/{by_json}/{step}", json={"response": padded(at_cap)}
        )
        pressed = client.post(
            f"/parent/actions/help/{by_form}", data={"step": step, "response": padded(at_cap)}
        )
        before = requests_held(client)
        refused = client.post(
            f"/parent/help-requests/{refused_id}/{step}", json={"response": padded(over)}
        )
        after = requests_held(client)
        held = listed(client)

    assert (sent.status_code, pressed.status_code) == (200, 303)
    for request_id in (by_json, by_form):
        assert [update["body"] for update in held[request_id]["updates"]] == [at_cap]
        assert held[request_id]["response"] == at_cap
    assert held[by_json]["state"] == held[by_form]["state"]
    assert refused.status_code == 422
    assert refused.json()["detail"] == (
        f"A reply is at most {NOTE_MAX_LENGTH} characters; this one is 501."
    )
    assert after == before


def test_an_update_over_json_is_added_once_for_its_id_and_its_refusals_are_said() -> None:
    with browser() as client:
        request_id = asked_for(client)
        route = f"/parent/help-requests/{request_id}/update"
        early = client.post(route, json={"response": "Synthetic early"})
        client.post(f"/parent/help-requests/{request_id}/accept", json={})
        form_id = uuid4().hex
        made = client.post(route, json={"response": "Synthetic update", "update_id": form_id})
        same = client.post(route, json={"response": "Synthetic update", "update_id": form_id})
        other = client.post(route, json={"response": "Synthetic other", "update_id": form_id})
        blank = client.post(route, json={"response": "  "})
        malformed = client.post(route, json={"response": "Synthetic", "update_id": "not-an-id"})
        client.post(f"/parent/help-requests/{request_id}/resolve", json={})
        late = client.post(route, json={"response": "Synthetic late"})
        kept = listed(client)[request_id]

    assert (early.status_code, early.json()["detail"]) == (409, NOT_TAKEN_UP_YET)
    assert made.status_code == same.status_code == 200
    assert same.json() == made.json()
    assert made.json()["state"] == "accepted"
    assert (other.status_code, other.json()["detail"]) == (409, FORM_ADDED_OTHER_WORDS)
    assert (blank.status_code, blank.json()["detail"]) == (422, UPDATE_NEEDS_WORDS)
    assert malformed.status_code == 422
    assert (late.status_code, late.json()["detail"]) == (409, CLOSED_BEFORE_UPDATE)
    assert [(update["update_id"], update["body"]) for update in kept["updates"]] == [
        (form_id, "Synthetic update")
    ]
    written = datetime.fromisoformat(kept["updates"][0]["written_local"])
    assert written.utcoffset() == datetime.fromisoformat(kept["asked_local"]).utcoffset()


@pytest.mark.parametrize("route", ["JSON", "form"])
@pytest.mark.parametrize(
    ("case", "status_code"), [("missing", 404), ("expired", 404), ("taken up", 422)]
)
def test_a_blank_update_to_a_request_not_kept_is_unknown_before_it_is_blank(
    route: str, case: str, status_code: int
) -> None:
    """A request that is not kept, never asked or closed past retention, is 404 for an update
    whatever its words, as for every move; a blank update to one that is kept is 422. Nothing
    is written either way."""
    with browser() as client:
        request_id = asked_for(client)
        client.post(f"/parent/help-requests/{request_id}/accept", json={})
        if case == "expired":
            client.post(f"/parent/help-requests/{request_id}/resolve", json={})
            state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
            past = datetime.now(UTC) - timedelta(days=HELP_RETENTION_DAYS, hours=1)
            state.help_requests._connection.execute(
                "UPDATE help_requests SET resolved_at = ?", (past.isoformat(),)
            )
            state.help_requests._connection.commit()
        named = "0" * 32 if case == "missing" else request_id
        before = requests_held(client)
        if route == "JSON":
            answer = client.post(f"/parent/help-requests/{named}/update", json={"response": " \n "})
            said = str(answer.json()["detail"])
        else:
            answer = client.post(
                f"/parent/actions/help/{named}", data={"step": "update", "response": " \n "}
            )
            problem = PROBLEM_LINE.search(answer.text)
            assert problem is not None
            said = problem.group(1)
        after = requests_held(client)

    assert answer.status_code == status_code
    assert after == before
    expected = f"no help request {named!r}" if status_code == 404 else UPDATE_NEEDS_WORDS
    if route == "JSON":
        assert said == expected
    elif status_code == 404:
        assert said == str(escape(expected))
    else:
        assert said == f'{escape(expected)} <a href="#reply-{request_id}">Go to the field.</a>'


def test_no_words_of_a_parents_update_reach_the_log(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every press, accepted, refused, or refused by the file, logs nothing of its words."""
    caplog.set_level(logging.DEBUG)
    with browser() as client:
        request_id = asked_for(client)
        mine, theirs = client.get(FAMILY).text, client.get(FAMILY).text
        press(client, mine, request_id, "accept", "Synthetic zebra one")
        press(client, theirs, request_id, "accept", "Synthetic zebra two")
        page = client.get(FAMILY).text
        press(client, page, request_id, "update", "Synthetic zebra three")
        press(client, page, request_id, "update", "Synthetic zebra four")
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        monkeypatch.setattr(state.help_requests, "add_update", refusing())
        failed = press(client, client.get(FAMILY).text, request_id, "update", "Synthetic zebra 5")
        monkeypatch.undo()
        press(client, client.get(FAMILY).text, request_id, "resolve", "Synthetic zebra six")
        press(client, page, request_id, "update", "Synthetic zebra seven")
        client.get(PAGE)
        client.get(FAMILY)

    assert failed.status_code == 500
    assert "Synthetic zebra 5" in failed.text
    assert "zebra" not in caplog.text


@pytest.mark.parametrize("step", ["update", "resolve"])
def test_a_move_on_a_row_that_can_not_be_read_keeps_the_words_and_writes_nothing(
    step: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A request whose updates turn unreadable after the page was made is refused when the move
    reads it: the family page's stand-in keeps the words as typed, nothing is written, and the
    log names the kind of failure alone."""
    with browser() as client:
        request_id = asked_for(client)
        client.post(f"/parent/help-requests/{request_id}/accept", json={})
        page = client.get(FAMILY).text
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        state.help_requests._connection.execute(
            "UPDATE help_requests SET parent_updates = 'Synthetic zebra, no list'"
        )
        state.help_requests._connection.commit()
        before = requests_held(client)
        answer = press(client, page, request_id, step, "Synthetic zebra words")
        after = requests_held(client)

    assert answer.status_code == 500
    assert "That could not be saved, and nothing was changed. Your reply is below." in answer.text
    assert '<textarea id="kept-reply" rows="3" readonly>Synthetic zebra words</textarea>' in (
        answer.text
    )
    assert after == before
    kind = "UnreadableHelpRequest"
    assert f"a parent's move on her request could not be saved: {kind}" in caplog.text
    assert "zebra" not in caplog.text
