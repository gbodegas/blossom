"""A request for help about one homework note: the reference, and what becomes of it.

The help store has a connection of its own to the record's file. A request that names a
note is checked and written in one transaction on that connection, begun before the
note is looked for, and nothing else about asking for help changes.
"""

import pathlib
import sqlite3
from datetime import UTC, date, datetime

import pytest

from blossom.captures import STUDENT, CaptureChanged, CaptureCreated, NotACaptureId, new_capture_id
from blossom.reconciliation import SourceChannel
from blossom.stores.help_requests import HelpRequestsStore, UnknownCaptureReference
from blossom.stores.project_state import ProjectStateStore
from tests.support import PLAN_DATE, fixture_clock, practice_store

AT = datetime(2026, 9, 14, 21, 0, tzinfo=UTC)
MONDAY = date(2026, 9, 14)


def note_in(store: ProjectStateStore, text: str = "Geometry questions 4-8") -> str:
    name = new_capture_id()
    outcome = store.create_capture(
        name,
        text,
        None,
        None,
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=AT,
        today=MONDAY,
    )
    assert isinstance(outcome, CaptureCreated)
    return name


def rows_of(help_store: HelpRequestsStore) -> list[tuple[object, ...]]:
    found = help_store._connection.execute("SELECT * FROM help_requests ORDER BY request_id")
    return [tuple(row) for row in found.fetchall()]


@pytest.fixture
def stores(tmp_path: pathlib.Path) -> tuple[ProjectStateStore, HelpRequestsStore]:
    path = tmp_path / "record.sqlite3"
    return practice_store(path), HelpRequestsStore.open(path, fixture_clock())


# ------------------------------------------------------------------ the file


def test_a_file_from_before_gains_the_column_and_keeps_every_request(
    tmp_path: pathlib.Path,
) -> None:
    """Additive, and safe to meet again: an old table gets one nullable column, every request
    in it reads as about no note, and a second start changes nothing."""
    path = tmp_path / "record.sqlite3"
    old = sqlite3.connect(path)
    old.execute(
        """
        CREATE TABLE help_requests (
            request_id TEXT PRIMARY KEY,
            evening TEXT NOT NULL,
            asked_at TEXT NOT NULL,
            note TEXT,
            state TEXT NOT NULL,
            accepted_at TEXT,
            resolved_at TEXT,
            response TEXT
        )
        """
    )
    old.execute(
        "INSERT INTO help_requests (request_id, evening, asked_at, note, state) "
        "VALUES ('from-before', ?, ?, 'the essay outline', 'requested')",
        (PLAN_DATE.isoformat(), fixture_clock().now().isoformat()),
    )
    old.commit()
    old.close()

    first = HelpRequestsStore.open(path, fixture_clock())
    kept = first.open_requests()
    first.close()
    second = HelpRequestsStore.open(path, fixture_clock())
    columns = [
        str(row[1]) for row in second._connection.execute("PRAGMA table_info(help_requests)")
    ]

    assert [(item.request_id, item.note, item.capture_id) for item in kept] == [
        ("from-before", "the essay outline", None)
    ]
    assert columns.count("capture_id") == 1
    assert second.open_requests() == kept
    assert second.ask(PLAN_DATE, "another").capture_id is None


# ------------------------------------------------------------- the reference


def test_a_request_about_a_note_names_it_and_copies_none_of_its_words(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    record, help_store = stores
    name = note_in(record, "Geometry questions 4-8, heard from a classmate")

    asked = help_store.ask(PLAN_DATE, None, capture_id=name)
    with_words = help_store.ask(PLAN_DATE, "which questions?", capture_id=name)

    assert (asked.capture_id, asked.note) == (name, None)
    assert (with_words.capture_id, with_words.note) == (name, "which questions?")
    assert [item.capture_id for item in help_store.open_requests()] == [name, name]
    assert help_store.get(asked.request_id) == asked


def test_a_name_that_is_no_note_of_this_record_writes_no_request(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    record, help_store = stores
    note_in(record)
    before = rows_of(help_store)

    with pytest.raises(UnknownCaptureReference):
        help_store.ask(PLAN_DATE, "about nothing", capture_id=new_capture_id())
    with pytest.raises(NotACaptureId):
        help_store.ask(PLAN_DATE, "about nothing", capture_id="assignment-canal-essay")
    with pytest.raises(NotACaptureId):
        help_store.ask(PLAN_DATE, "about nothing", capture_id="")

    assert rows_of(help_store) == before
    assert not help_store._connection.in_transaction
    assert help_store.ask(PLAN_DATE, "and then an ordinary one").capture_id is None


def test_a_file_with_no_notes_in_it_has_no_note_to_name() -> None:
    alone = HelpRequestsStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())

    with pytest.raises(UnknownCaptureReference):
        alone.ask(PLAN_DATE, None, capture_id=new_capture_id())

    assert alone.open_requests() == []
    assert not alone._connection.in_transaction


def test_the_note_is_looked_for_inside_the_transaction_that_writes_the_request(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    """The writer is reserved first, on the help store's own connection, then the note is
    read, then the request is written, and only then is it all committed. A connection's
    own scope would begin nothing until the insert, leaving the read outside."""
    record, help_store = stores
    name = note_in(record)
    seen: list[str] = []
    help_store._connection.set_trace_callback(seen.append)

    help_store.ask(PLAN_DATE, "which questions?", capture_id=name)
    help_store._connection.set_trace_callback(None)

    said = [" ".join(statement.split()) for statement in seen]
    assert said[0] == "BEGIN IMMEDIATE"
    assert said[-1] == "COMMIT"
    read = next(
        index for index, statement in enumerate(said) if "FROM homework_captures" in statement
    )
    wrote = next(index for index, statement in enumerate(said) if statement.startswith("INSERT"))
    assert 0 < read < wrote < len(said) - 1
    assert not help_store._connection.in_transaction


def test_a_request_refused_by_the_file_leaves_nothing_and_no_transaction_open(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    record, help_store = stores
    name = note_in(record)
    help_store._connection.execute(
        "CREATE TRIGGER refuse_requests BEFORE INSERT ON help_requests "
        "BEGIN SELECT RAISE(ABORT, 'the file refused'); END"
    )
    help_store._connection.commit()

    with pytest.raises(sqlite3.DatabaseError):
        help_store.ask(PLAN_DATE, None, capture_id=name)

    assert rows_of(help_store) == []
    assert not help_store._connection.in_transaction


def test_putting_a_note_away_keeps_the_reference_and_a_damaged_note_loses_no_request(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    record, help_store = stores
    name = note_in(record)
    asked = help_store.ask(PLAN_DATE, "stuck on 6", capture_id=name)

    archived = record.archive_capture(
        name, expected_revision=1, authored_by=STUDENT, now=AT, today=MONDAY
    )
    about_an_archived_note = help_store.ask(PLAN_DATE, None, capture_id=name)
    record._connection.execute("UPDATE homework_captures SET due_date = 'next week'")
    record._connection.commit()

    assert isinstance(archived, CaptureChanged)
    assert about_an_archived_note.capture_id == name
    assert sorted(item.request_id for item in help_store.open_requests()) == sorted(
        [asked.request_id, about_an_archived_note.request_id]
    )
    assert help_store.accept(asked.request_id, "on it").capture_id == name
    assert help_store.resolve(asked.request_id).capture_id == name


@pytest.mark.parametrize(
    "held",
    [b"INJECTED reference", "INJECTED words", "CAPITALS", 7, 1.5],
)
def test_a_reference_that_is_no_id_as_the_store_writes_it_is_kept_as_unreadable_and_not_as_text(
    tmp_path: pathlib.Path, held: object
) -> None:
    """The store writes a note's id one way. Anything else in that column is no reference it
    wrote: the request is still read, it says its reference cannot be read, and nothing of
    what the column holds leaves the store."""
    path = tmp_path / "record.sqlite3"
    record = practice_store(path)
    name = new_capture_id()
    record.create_capture(
        name,
        "Geometry questions 4-8",
        None,
        None,
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=AT,
        today=MONDAY,
    )
    requests = HelpRequestsStore(sqlite3.connect(path, check_same_thread=False), fixture_clock())
    asked = requests.ask(MONDAY, "which part?", capture_id=name)
    value = name.upper() if held == "CAPITALS" else held
    requests._connection.execute("UPDATE help_requests SET capture_id = ?", (value,))
    requests._connection.commit()

    read = requests.get(asked.request_id)
    listed = requests.open_requests()

    assert read is not None
    assert (read.capture_id, read.capture_reference_unreadable) == (None, True)
    assert read.note == "which part?"
    assert [item.request_id for item in listed] == [asked.request_id]
    assert "INJECTED" not in read.model_dump_json()
    assert name.upper() not in read.model_dump_json()
    sound = requests.ask(MONDAY, None, capture_id=name)
    assert (sound.capture_id, sound.capture_reference_unreadable) == (name, False)
    plain = requests.ask(MONDAY, "about no note")
    assert (plain.capture_id, plain.capture_reference_unreadable) == (None, False)
