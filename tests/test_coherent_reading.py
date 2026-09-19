"""One reading of the record costs a fixed number of statements and is one snapshot.

The snapshot cases use two connections to one file, as the application's
stores do. A second connection that cannot commit gives up after a fraction of
a second here, a timeout of this file's own; the application's is untouched.
"""

import pathlib
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from blossom import noticing
from blossom.app import create_app
from blossom.noticing import read_everything
from blossom.reconciliation import SourceChannel
from blossom.settings import FIXTURE_PATH_VARIABLE, TODAY_VARIABLE
from blossom.sources import FixtureSource
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    FIXTURES,
    SAME_ORIGIN,
    a_row,
    fixture_clock,
    fixture_settings,
    record,
    state_of,
)

SHORT_WAIT_SECONDS = 0.2


def names(count: int) -> list[str]:
    return [f"math-{number:03d}" for number in range(count)]


def store_of(path: pathlib.Path, count: int) -> ProjectStateStore:
    """A record of ``count`` assignments; every other one has two claims, the rest none."""
    store = ProjectStateStore.open(path, fixture_clock())
    claims = {
        name: [record(SourceChannel.LMS, "2026-09-18"), record(SourceChannel.EMAIL, "2026-09-19")]
        for name in names(count)[::2]
    }
    store.put_on_record([a_row(name, f"Set {name}") for name in names(count)], claims)
    return store


@contextmanager
def statements_of(store: ProjectStateStore) -> Iterator[list[str]]:
    """Every statement the store's connection runs inside the block, as SQLite saw it."""
    seen: list[str] = []
    store._connection.set_trace_callback(lambda statement: seen.append(" ".join(statement.split())))
    try:
        yield seen
    finally:
        store._connection.set_trace_callback(None)


def titles(path: pathlib.Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("SELECT title FROM assignments ORDER BY assignment_id")
        return [str(row[0]) for row in rows]
    finally:
        connection.close()


def second_connection(path: pathlib.Path, wait: float) -> sqlite3.Connection:
    """Another connection to the same file, which begins and ends its own transactions."""
    return sqlite3.connect(path, timeout=wait, isolation_level=None, check_same_thread=False)


@pytest.mark.parametrize("count", [1, 20, 200])
def test_a_reading_costs_the_same_statements_whatever_the_record_holds(
    count: int, tmp_path: pathlib.Path
) -> None:
    store = store_of(tmp_path / "record.sqlite3", count)

    with statements_of(store) as seen:
        everything = read_everything(store, store)

    assert len(everything.assignments) == count
    assert seen[0] == "BEGIN DEFERRED"
    assert seen[-1] == "COMMIT"
    assert len(seen) == 7, seen
    assert sum("FROM date_claims" in statement for statement in seen) == 1


@pytest.mark.parametrize("page", ["/student/due-this-week", "/parent"])
def test_a_page_costs_the_same_statements_whatever_the_record_holds(page: str) -> None:
    """Her page with a card for each, and the family page: no read is made once per card."""
    costs = []
    for count in (1, 20, 200):
        settings = fixture_settings(**{TODAY_VARIABLE: "2026-09-16", FIXTURE_PATH_VARIABLE: ""})
        with TestClient(create_app(settings), headers=SAME_ORIGIN) as client:
            store = state_of(client).project_state
            store.put_on_record(
                [a_row(name, f"Set {name}") for name in names(count)],
                {name: [record(SourceChannel.LMS, "2026-09-18")] for name in names(count)[::2]},
            )
            with statements_of(store) as seen:
                shown = client.get(page)
        assert shown.status_code == 200
        if page.startswith("/student"):
            assert shown.text.count("Set math-") >= count
        assert sum("FROM date_claims" in statement for statement in seen) == 1
        costs.append(len(seen))

    assert costs == [7, 7, 7]


def test_the_reading_is_made_of_plain_lists_and_holds_nothing_open(
    tmp_path: pathlib.Path,
) -> None:
    store = store_of(tmp_path / "record.sqlite3", 3)

    everything = read_everything(store, store)

    assert type(everything.assignments) is list
    assert type(everything.records) is dict
    assert all(type(claims) is list for claims in everything.records.values())
    assert not store._connection.in_transaction


def test_every_assignment_has_its_claims_in_the_order_made_and_an_empty_list_for_none(
    tmp_path: pathlib.Path,
) -> None:
    store = store_of(tmp_path / "record.sqlite3", 4)
    store.record_claims("math-000", [record(SourceChannel.LMS, "2026-09-20")])

    everything = read_everything(store, store)

    assert list(everything.records) == names(4)
    assert [claim.asserted_value for claim in everything.records["math-000"]] == [
        "2026-09-18",
        "2026-09-19",
        "2026-09-20",
    ]
    assert everything.records["math-001"] == []
    for name in names(4):
        assert everything.records[name] == store.deadline_records(name)


def test_the_bulk_read_names_only_what_has_claims_and_only_what_was_asked(
    tmp_path: pathlib.Path,
) -> None:
    store = store_of(tmp_path / "record.sqlite3", 4)

    assert list(store.deadline_records_by_assignment()) == ["math-000", "math-002"]
    assert list(store.deadline_records_by_assignment(["math-002", "math-003", "unknown"])) == [
        "math-002"
    ]
    with statements_of(store) as seen:
        assert store.deadline_records_by_assignment([]) == {}
    assert seen == []


def test_a_fixture_answers_in_bulk_as_it_answers_one_at_a_time() -> None:
    fixture = FixtureSource(FIXTURES)
    ids = [item.assignment_id for item in fixture.assignments()]

    together = fixture.deadline_records_by_assignment(ids)

    assert any(together.values())
    for assignment_id in ids:
        assert list(together.get(assignment_id, [])) == fixture.deadline_records(assignment_id)


def test_a_writer_cannot_commit_into_a_reading_and_the_reading_stays_one_snapshot(
    tmp_path: pathlib.Path,
) -> None:
    """The reader has made its first read; a second connection then tries to commit."""
    path = tmp_path / "record.sqlite3"
    store = store_of(path, 3)
    writer = second_connection(path, SHORT_WAIT_SECONDS)

    with store.reading():
        before = store.all_assignments()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE assignments SET title = 'changed'")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            writer.execute("COMMIT")
        assert store.all_assignments() == before
        writer.execute("ROLLBACK")
        assert store.all_assignments() == before

    assert "changed" not in titles(path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE assignments SET title = 'changed'")
    writer.execute("COMMIT")
    writer.close()
    assert titles(path) == ["changed"] * 3


def test_a_writer_that_waits_commits_once_the_reading_is_released(
    tmp_path: pathlib.Path,
) -> None:
    """The commit cannot finish while the reading is held, and finishes when it ends."""
    path = tmp_path / "record.sqlite3"
    store = store_of(path, 3)
    writer = second_connection(path, 30)
    about_to_commit = threading.Event()
    finished = threading.Event()
    failures: list[BaseException] = []

    def write() -> None:
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE assignments SET title = 'changed'")
            about_to_commit.set()
            writer.execute("COMMIT")
        except BaseException as error:
            failures.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=write)
    with store.reading():
        before = store.all_assignments()
        thread.start()
        assert about_to_commit.wait(30)
        assert store.all_assignments() == before
        assert not finished.is_set()

    assert finished.wait(30)
    thread.join()
    writer.close()
    assert failures == []
    assert titles(path) == ["changed"] * 3


def test_a_reading_inside_a_callers_transaction_neither_commits_nor_rolls_it_back(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "record.sqlite3"
    store = store_of(path, 3)
    connection = store._connection

    with store.exclusively():
        connection.execute("UPDATE assignments SET title = 'pending'")
        assert connection.in_transaction

        with statements_of(store) as seen:
            everything = read_everything(store, store)
        assert [item.title for item in everything.assignments] == ["pending"] * 3
        assert "BEGIN DEFERRED" not in seen
        assert "COMMIT" not in seen
        assert connection.in_transaction
        assert "pending" not in titles(path)

        failed = RuntimeError("the caller's reading failed")
        with pytest.raises(RuntimeError, match="reading failed"), store.reading():
            raise failed
        assert connection.in_transaction
        assert [item.title for item in store.all_assignments()] == ["pending"] * 3

        connection.rollback()

    assert "pending" not in titles(path)


def test_a_reading_that_fails_ends_its_own_transaction_and_returns_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_of(tmp_path / "record.sqlite3", 3)

    def failing(*args: object, **kwargs: object) -> None:
        msg = "her reports could not be read"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(noticing, "statuses_for", failing)

    with statements_of(store) as seen, pytest.raises(sqlite3.OperationalError):
        read_everything(store, store)

    assert seen[0] == "BEGIN DEFERRED"
    assert seen[-1] == "ROLLBACK"
    assert not store._connection.in_transaction
