# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A transaction no scope of a store owns, found on the store's own connection: every writer
that would join it or commit it, and the record's read block, refuses with
``TransactionLeftOpen`` and touches nothing. The transaction stays open with what it holds, a
connection of its own to the same file reads nothing new, and the log names the connection
once, at error level. A healthy store nests its scopes and commits as it does anywhere else,
and every way out of a scope gives the store's count of its open scopes back and frees its
lock. The state is forced: a transaction begun by hand on the store's connection, holding
one change.
"""

import ast
import logging
import pathlib
import sqlite3
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, _GeneratorContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import pytest

from blossom.grades.identity import name_form_key
from blossom.reconciliation import SourceChannel
from blossom.settings import REPOSITORY_ROOT
from blossom.stores.gradebook import NameFormAdded, NameFormNotSaved
from blossom.stores.help_requests import HelpRequestsStore
from blossom.stores.homework_saves import TransactionLeftOpen
from blossom.stores.project_state import CouldNotSave, ProjectStateStore, Seed
from blossom.stores.traces import TracedRun, TraceStore
from blossom.stores.workload_signals import WorkloadSignalsStore
from tests.support import (
    PRACTICE,
    PRACTICE_LOG,
    SAID_AT,
    SAID_ON,
    a_row,
    every_row,
    fixture_clock,
    practice_store,
    record,
    school_missing,
)

LEFT_OPEN: Final = "A transaction was found left open on "
ON_THE_RECORD: Final = f"{LEFT_OPEN}the record's connection"
SCOPES: Final = "_scopes"
"""The record's store's count of its own open scopes, named here and nowhere else."""
ADDED: Final = "assignment-added"
INSIDE: Final = "assignment-inside"
HELD: Final = "held and never committed"
HOLD_A_TITLE: Final = "UPDATE assignments SET title = ? WHERE assignment_id = ?"
IDS: Final = "SELECT assignment_id FROM assignments"
KEY: Final = name_form_key(b"5" * 64)
WREN: Final = "Bramble, Wren"

Rows = dict[str, list[tuple[object, ...]]]


def rows_through(connection: sqlite3.Connection) -> Rows:
    """Every table and its rows as ``connection`` reads them, with whatever a transaction
    open on it holds."""
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return {
        str(name): [tuple(row) for row in connection.execute(f'SELECT * FROM "{name}"')]  # noqa: S608
        for (name,) in tables
    }


def read_fresh(path: pathlib.Path, sql: str) -> list[object]:
    """The first column of every row ``sql`` reads, through a connection of its own."""
    connection = sqlite3.connect(path)
    try:
        return [row[0] for row in connection.execute(sql)]
    finally:
        connection.close()


def held_open(connection: sqlite3.Connection, change: str, values: tuple[object, ...]) -> Rows:
    """Begin a transaction by hand on ``connection`` and leave it open holding ``change``.
    What the connection reads with it held."""
    connection.execute("BEGIN")
    connection.execute(change, values)
    return rows_through(connection)


def refusal_in(error: BaseException) -> bool:
    """Whether ``error`` is the refusal, or a store's own failure raised from it."""
    return isinstance(error, TransactionLeftOpen) or isinstance(
        error.__cause__, TransactionLeftOpen
    )


def errors_logged(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    """Every line logged at error level or above, with its level."""
    return [
        (entry.levelno, entry.getMessage())
        for entry in caplog.records
        if entry.levelno >= logging.ERROR
    ]


def scopes_of(store: ProjectStateStore) -> int:
    """The store's count of its own open scopes."""
    count = getattr(store, SCOPES, None)
    assert isinstance(count, int), f"the record's store keeps no count named {SCOPES}"
    return count


def lock_free(store: ProjectStateStore) -> bool:
    """Whether another thread can take the store's lock now."""
    taken: list[bool] = []

    def take() -> None:
        got = store._lock.acquire(blocking=False)
        taken.append(got)
        if got:
            store._lock.release()

    other = threading.Thread(target=take)
    other.start()
    other.join()
    return taken == [True]


def put(store: ProjectStateStore, assignment_id: str = ADDED) -> None:
    """One more assignment, through the write scope every page's write goes through."""
    store.put_on_record([a_row(assignment_id, "Added later")], {})


def committed(store: ProjectStateStore, path: pathlib.Path, assignment_id: str) -> bool:
    """Whether the store's transaction has ended with ``assignment_id`` on the file."""
    return not store._connection.in_transaction and assignment_id in read_fresh(path, IDS)


@pytest.fixture
def path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "record.sqlite3"


@pytest.fixture
def store(path: pathlib.Path) -> Iterator[ProjectStateStore]:
    opened = practice_store(path)
    yield opened
    opened.close()


# ------------------------------------------------------------- every site that refuses


def compared_and_put(store: ProjectStateStore) -> None:
    with store.comparing_and_writing():
        put(store)


def read_block(store: ProjectStateStore) -> None:
    with store.reading():
        store.all_assignments()


RECORD_SITES: Final[dict[str, tuple[type[Exception], Callable[[ProjectStateStore], object]]]] = {
    "a write scope": (TransactionLeftOpen, put),
    "her update, in a write scope": (
        CouldNotSave,
        lambda store: store.report_status(
            PRACTICE, "done", None, expected_head=None, now=SAID_AT, today=SAID_ON
        ),
    ),
    "a grade write, with its savepoint": (
        NameFormNotSaved,
        lambda store: store.add_name_form(KEY, WREN, "parent"),
    ),
    "comparing_and_writing": (TransactionLeftOpen, compared_and_put),
    "a read block": (TransactionLeftOpen, read_block),
    "upsert_assignments": (
        TransactionLeftOpen,
        lambda store: store.upsert_assignments([a_row(ADDED, "Added later")]),
    ),
    "record_status_reports": (
        TransactionLeftOpen,
        lambda store: store.record_status_reports(PRACTICE, [school_missing(SAID_ON)]),
    ),
    "record_claims": (
        TransactionLeftOpen,
        lambda store: store.record_claims(PRACTICE, [record(SourceChannel.LMS, "2026-10-06")]),
    ),
}
"""Each place the record's store checks on its own, by a call that reaches it and what that
call raises: the refusal itself, or the store's own failure raised from it."""


@pytest.mark.parametrize("site", list(RECORD_SITES))
def test_the_record_s_store_refuses_at_each_site_and_touches_nothing(
    site: str,
    store: ProjectStateStore,
    path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raised, call = RECORD_SITES[site]
    before = every_row(path)
    held = held_open(store._connection, HOLD_A_TITLE, (HELD, PRACTICE))
    assert any(HELD in row for row in held["assignments"])

    with pytest.raises(raised) as refused:
        call(store)

    assert refusal_in(refused.value)
    assert store._connection.in_transaction
    assert rows_through(store._connection) == held
    assert every_row(path) == before
    assert errors_logged(caplog) == [(logging.ERROR, ON_THE_RECORD)]


def a_run(run_id: str) -> TracedRun:
    return TracedRun(
        run_id=run_id,
        trace_id=run_id,
        parent_run_id=None,
        thread_id="plan:x",
        name="LangGraph",
        run_type="chain",
        started_at=datetime(2026, 8, 19, 22, 0, tzinfo=UTC),
        ended_at=None,
        inputs="{}",
        outputs=None,
        error=None,
    )


@dataclass(frozen=True)
class Others:
    """The three other stores with a writer that enters its connection's context, each on a
    file of its own named for it, holding one request, one run and one signal."""

    help_requests: HelpRequestsStore
    traces: TraceStore
    workload_signals: WorkloadSignalsStore
    asked: str
    signal: str


@pytest.fixture
def others(tmp_path: pathlib.Path) -> Iterator[Others]:
    clock = fixture_clock()
    help_requests = HelpRequestsStore.open(tmp_path / "help_requests.sqlite3", clock)
    traces = TraceStore.open(tmp_path / "traces.sqlite3", clock)
    workload_signals = WorkloadSignalsStore.open(tmp_path / "workload_signals.sqlite3", clock)
    traces.record(a_run("kept"))
    asked = help_requests.ask(clock.today(), "which part?").request_id
    signal = workload_signals.record(clock.today()).signal_id
    yield Others(help_requests, traces, workload_signals, asked, signal)
    help_requests.close()
    traces.close()
    workload_signals.close()


OTHER_SITES: Final[dict[str, tuple[str, str, Callable[[Others], object]]]] = {
    "help requests, take_back": (
        "help_requests",
        "UPDATE help_requests SET note = ?",
        lambda kept: kept.help_requests.take_back(kept.asked),
    ),
    "help requests, sweep": (
        "help_requests",
        "UPDATE help_requests SET note = ?",
        lambda kept: kept.help_requests.sweep(),
    ),
    "traces, record": (
        "traces",
        "UPDATE trace_runs SET name = ?",
        lambda kept: kept.traces.record(a_run("later")),
    ),
    "traces, sweep": (
        "traces",
        "UPDATE trace_runs SET name = ?",
        lambda kept: kept.traces.sweep(),
    ),
    "workload signals, withdraw": (
        "workload_signals",
        "UPDATE workload_signals SET detail = ?",
        lambda kept: kept.workload_signals.withdraw(kept.signal),
    ),
}
"""Each other writer: its store, the change the transaction left open holds, and the call."""


@pytest.mark.parametrize("site", list(OTHER_SITES))
def test_each_other_store_s_writer_refuses_and_touches_nothing(
    site: str,
    others: Others,
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    name, change, call = OTHER_SITES[site]
    store: HelpRequestsStore | TraceStore | WorkloadSignalsStore = getattr(others, name)
    path = tmp_path / f"{name}.sqlite3"
    before = every_row(path)
    held = held_open(store._connection, change, (HELD,))
    assert any(HELD in row for rows in held.values() for row in rows)

    with pytest.raises(TransactionLeftOpen):
        call(others)

    assert store._connection.in_transaction
    assert rows_through(store._connection) == held
    assert every_row(path) == before
    connection = f"the {name.replace('_', ' ')} connection"
    assert errors_logged(caplog) == [(logging.ERROR, f"{LEFT_OPEN}{connection}")]


# ------------------------------------------------------------- a healthy store nests and commits


def test_a_write_scope_inside_a_write_scope_leaves_the_end_to_the_outer_one(
    store: ProjectStateStore, path: pathlib.Path
) -> None:
    with store._writing():
        put(store, INSIDE)
        assert store.transaction_open()
        assert INSIDE not in read_fresh(path, IDS)

    assert committed(store, path, INSIDE)


def test_a_write_scope_inside_comparing_and_writing_commits_with_it(
    store: ProjectStateStore, path: pathlib.Path
) -> None:
    with store.comparing_and_writing():
        known = {item.assignment_id for item in store.all_assignments()}
        put(store, INSIDE)
        assert INSIDE not in read_fresh(path, IDS)

    assert known == {PRACTICE, PRACTICE_LOG}
    assert committed(store, path, INSIDE)


def test_a_read_block_inside_a_write_scope_joins_it_and_ends_nothing(
    store: ProjectStateStore, path: pathlib.Path
) -> None:
    with store._writing():
        with store.reading():
            seen = store.all_assignments()
        assert store.transaction_open()
        put(store, INSIDE)

    assert len(seen) == 2
    assert committed(store, path, INSIDE)


def test_a_read_block_inside_a_read_block_ends_once_and_a_write_follows(
    store: ProjectStateStore, path: pathlib.Path
) -> None:
    with store.reading():
        with store.reading():
            inner = store.all_assignments()
        assert store.transaction_open()
        outer = store.all_assignments()

    assert inner == outer
    assert not store.transaction_open()
    put(store)
    assert committed(store, path, ADDED)


def test_a_write_inside_a_read_block_commits_when_the_block_ends(
    store: ProjectStateStore, path: pathlib.Path
) -> None:
    with store.reading():
        put(store, INSIDE)
        assert store.transaction_open()
        assert INSIDE not in read_fresh(path, IDS)

    assert committed(store, path, INSIDE)


def test_a_grade_write_inside_a_write_scope_keeps_its_savepoint_and_commits_with_it(
    store: ProjectStateStore, path: pathlib.Path
) -> None:
    forms = "SELECT COUNT(*) FROM grade_name_forms"
    with store._writing():
        outcome = store.add_name_form(KEY, WREN, "parent")
        assert store.transaction_open()
        assert read_fresh(path, forms) == [0]

    assert isinstance(outcome, NameFormAdded)
    assert not store.transaction_open()
    assert read_fresh(path, forms) == [1]


def test_initialize_on_a_blank_file_commits_its_tables_its_seed_and_its_notes_together(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "blank.sqlite3"

    def seed() -> Seed:
        return Seed([a_row(PRACTICE, "Weekly practice")], {})

    def notes(planted: ProjectStateStore) -> None:
        put(planted, INSIDE)

    store = ProjectStateStore.initialize(path, fixture_clock(), seed, notes)
    try:
        assert not store.transaction_open()
        assert set(read_fresh(path, IDS)) == {PRACTICE, INSIDE}
        put(store)
        assert committed(store, path, ADDED)
    finally:
        store.close()


def test_opening_an_existing_file_adds_a_missing_table_and_commits_it(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "record.sqlite3"
    practice_store(path).close()
    by_hand = sqlite3.connect(path)
    by_hand.execute("DROP TABLE hand_in_events")
    by_hand.commit()
    by_hand.close()

    store = ProjectStateStore.open(path, fixture_clock())
    try:
        assert not store.transaction_open()
        assert "hand_in_events" in read_fresh(path, "SELECT name FROM sqlite_master")
        assert set(read_fresh(path, IDS)) == {PRACTICE, PRACTICE_LOG}
        put(store)
        assert committed(store, path, ADDED)
    finally:
        store.close()


def test_a_caller_holding_the_store_exclusively_reads_and_writes_like_any_other(
    store: ProjectStateStore, path: pathlib.Path
) -> None:
    with store.exclusively():
        with store.reading():
            known = {item.assignment_id for item in store.all_assignments()}
        put(store)
        said = store.report_status(
            PRACTICE, "done", None, expected_head=None, now=SAID_AT, today=SAID_ON
        )

    assert known == {PRACTICE, PRACTICE_LOG}
    assert committed(store, path, ADDED)
    assert read_fresh(path, "SELECT status FROM student_reports") == ["done"], said


# ------------------------------------------------------------- every way out of a scope


class Stopped(Exception):
    """What a block raises to leave its scope by an exception."""


class Refusing:
    """The record's connection with its begin, its commit or its rollback refused, as a file
    that cannot be written refuses it. Anything else goes through."""

    def __init__(self, connection: sqlite3.Connection, refused: str) -> None:
        self.connection = connection
        self.refused = refused

    def _refuse(self, step: str) -> None:
        if step == self.refused:
            msg = "refused"
            raise sqlite3.OperationalError(msg)

    def execute(self, statement: str, given: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if statement.startswith("BEGIN"):
            self._refuse("begin")
        return self.connection.execute(statement, given)

    def commit(self) -> None:
        self._refuse("commit")
        self.connection.commit()

    def rollback(self) -> None:
        self._refuse("rollback")
        self.connection.rollback()

    def __getattr__(self, name: str) -> object:
        return getattr(self.connection, name)


OPENERS: Final[dict[str, Callable[[ProjectStateStore], AbstractContextManager[None]]]] = {
    "a write scope": lambda store: store._writing(),
    "a read block": lambda store: store.reading(),
}


@dataclass(frozen=True)
class Exit:
    """One way out of a scope: the step of the connection that is refused, whether the block
    raises, and the scopes it leaves a transaction open after, which no scope then owns."""

    refused: str | None
    raises_inside: bool
    leaves_open: frozenset[str] = frozenset()


EXITS: Final = {
    "a failed begin": Exit("begin", raises_inside=False),
    "an exception in the block": Exit(None, raises_inside=True),
    "a refused commit": Exit(
        "commit", raises_inside=False, leaves_open=frozenset({"a read block"})
    ),
    "a refused rollback": Exit("rollback", raises_inside=True, leaves_open=frozenset(OPENERS)),
    "a generator closed early": Exit(None, raises_inside=False),
}
"""A write scope rolls a refused commit back, so only a refused rollback leaves its transaction
open. A read block commits after its block has ended, so a refused commit leaves its too."""


def left_by(
    store: ProjectStateStore, monkeypatch: pytest.MonkeyPatch, scope: str, way: str
) -> None:
    """Enter one scope of the store, write inside it, and leave it by ``way``."""
    leaving = EXITS[way]
    if way == "a generator closed early":
        entered = OPENERS[scope](store)
        assert isinstance(entered, _GeneratorContextManager)
        entered.__enter__()
        put(store, INSIDE)
        entered.gen.close()
        return

    def through() -> None:
        with OPENERS[scope](store):
            put(store, INSIDE)
            if leaving.raises_inside:
                raise Stopped

    if leaving.refused is None:
        with pytest.raises(Stopped):
            through()
        return
    real = store._connection
    monkeypatch.setattr(store, "_connection", Refusing(real, leaving.refused))
    with pytest.raises(sqlite3.OperationalError, match="^refused$"):
        through()
    monkeypatch.setattr(store, "_connection", real)


@pytest.mark.parametrize("way", list(EXITS))
@pytest.mark.parametrize("scope", list(OPENERS))
def test_every_way_out_of_a_scope_gives_its_count_back_and_the_next_writer_meets_the_state(
    scope: str,
    way: str,
    store: ProjectStateStore,
    path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = every_row(path)

    left_by(store, monkeypatch, scope, way)

    left_open = scope in EXITS[way].leaves_open
    assert store._connection.in_transaction == left_open
    assert every_row(path) == before
    assert scopes_of(store) == 0
    assert lock_free(store)
    if left_open:
        with pytest.raises(TransactionLeftOpen):
            put(store)
        assert store._connection.in_transaction
        assert every_row(path) == before
    else:
        put(store)
        assert committed(store, path, ADDED)
        assert INSIDE not in read_fresh(path, IDS)


def stopped_inside(store: ProjectStateStore, outer: str, inner: str) -> None:
    """One scope of the store entered inside another, and left by an exception."""
    with OPENERS[outer](store), OPENERS[inner](store):
        raise Stopped


@pytest.mark.parametrize("inner", list(OPENERS))
@pytest.mark.parametrize("outer", list(OPENERS))
def test_a_scope_that_joined_another_and_raises_gives_its_count_back(
    outer: str, inner: str, store: ProjectStateStore, path: pathlib.Path
) -> None:
    """A scope that joined an outer one counts until it ends, by an exception too. Were its
    count kept, a transaction left open afterward would pass for one a scope still owns."""
    before = every_row(path)

    with pytest.raises(Stopped):
        stopped_inside(store, outer, inner)

    still_open = store._connection.in_transaction
    assert not still_open
    assert scopes_of(store) == 0
    assert lock_free(store)
    held_open(store._connection, HOLD_A_TITLE, (HELD, PRACTICE))
    with pytest.raises(TransactionLeftOpen):
        put(store)
    assert store._connection.in_transaction
    assert every_row(path) == before


def test_a_write_scope_holds_the_store_s_lock_whoever_enters_it(store: ProjectStateStore) -> None:
    """The write scope takes the lock itself, so its check and its count are never read or
    changed by a caller that came without it."""
    assert lock_free(store)

    with store._writing():
        assert not lock_free(store)

    assert lock_free(store)


# ------------------------------------------------------------- the closed list of sites

CONTEXTS: Final = ("self._connection", "connection")
OTHER_USES: Final = ("all_or_none(self._connection)",)
STATEMENTS: Final = ("BEGIN", "BEGIN DEFERRED", "BEGIN IMMEDIATE", "SAVEPOINT grade_save")
PINNED: Final = {
    "drafts.py": {"BEGIN": 2, "BEGIN IMMEDIATE": 1},
    "gradebook.py": {"SAVEPOINT grade_save": 1},
    "help_requests.py": {"BEGIN IMMEDIATE": 3, "with self._connection": 2},
    "import_drafts.py": {"BEGIN": 1, "BEGIN IMMEDIATE": 2},
    "project_state.py": {
        "BEGIN": 1,
        "BEGIN DEFERRED": 1,
        "BEGIN IMMEDIATE": 1,
        "with connection": 1,
        "with self._connection": 3,
    },
    "traces.py": {"with self._connection": 2},
    "workload_signals.py": {"BEGIN": 1, "BEGIN IMMEDIATE": 2, "with self._connection": 1},
}
"""Every connection used as a context and every transaction or savepoint begun by hand under
``blossom/stores``, by file and exact shape. One added or removed fails here, and so does a
context whose expression names a connection another way and a statement that starts with
``BEGIN`` or ``SAVEPOINT`` and is not one of those counted. It is then classified: a site that
refuses, or one that needs no check. A context on a name that does not say ``connection`` is
not seen."""


def transaction_shapes(source: str) -> Counter[str]:
    """Each connection context and each by-hand begin in ``source``, counted by its exact
    shape. A context that names a connection another way, a statement that starts with
    ``BEGIN`` or ``SAVEPOINT`` and is not one of those counted, and ``executescript`` are
    refused unread."""
    found: Counter[str] = Counter()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                held = ast.unparse(item.context_expr)
                if held in CONTEXTS:
                    found[f"with {held}"] += 1
                else:
                    assert "connection" not in held or held in OTHER_USES, held
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            statement = node.value.strip()
            if statement in STATEMENTS:
                found[statement] += 1
            else:
                assert not statement.startswith(("BEGIN", "SAVEPOINT")), statement
        elif isinstance(node, ast.Attribute):
            assert node.attr != "executescript", ast.unparse(node)
    return found


def test_the_connection_contexts_and_by_hand_begins_under_the_stores_are_the_pinned_ones() -> None:
    stores = REPOSITORY_ROOT / "blossom" / "stores"
    found = {
        file.relative_to(stores).as_posix(): dict(transaction_shapes(file.read_text("utf-8")))
        for file in sorted(stores.rglob("*.py"))
    }

    assert {name: shapes for name, shapes in found.items() if shapes} == PINNED
