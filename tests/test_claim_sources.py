"""Where a claim about a due date came from, and whether it still counts.

A claim made from a homework note names the note and its revision, so the same
claim sent again is one claim, and it can be withdrawn later without touching
what the school said. The school's claims carry no such name and always count.
What a plan, a digest, and a card read is the claims that count; an
assignment's details keep the rest as history.
"""

import pathlib
import re
import sqlite3
from datetime import UTC, date, datetime

import pytest

from blossom.captures import new_capture_id
from blossom.noticing import planning_digest, read_everything, week_from
from blossom.reconciliation import SourceChannel, SourceRecord
from blossom.stores import project_state
from blossom.stores.project_state import ClaimOnRecord, ProjectStateStore
from tests.support import PRACTICE, fixture_clock, practice_store

AT = datetime(2026, 9, 14, 21, 0, tzinfo=UTC)
MONDAY = date(2026, 9, 14)
ADDED = ("capture_id", "capture_revision", "active", "withdrawn_at")


def claim(value: str, channel: SourceChannel = SourceChannel.STUDENT_REPORT) -> SourceRecord:
    return SourceRecord(
        channel=channel, asserted_value=value, observed_at=AT, confidence=0.8, seen_in=None
    )


def columns_of(path: pathlib.Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return [str(row[1]) for row in connection.execute("PRAGMA table_info(date_claims)")]
    finally:
        connection.close()


def file_from_before(path: pathlib.Path) -> list[tuple[object, ...]]:
    """A record whose claims table is as it was before this: six columns and no index on a
    note. Returns its claim rows as that file holds them."""
    store = practice_store(path)
    store.record_claims(PRACTICE, [claim("2026-09-18", SourceChannel.LMS)])
    for statement in (
        "DROP INDEX date_claims_capture_once",
        "ALTER TABLE date_claims DROP COLUMN withdrawn_at",
        "ALTER TABLE date_claims DROP COLUMN active",
        "ALTER TABLE date_claims DROP COLUMN capture_revision",
        "ALTER TABLE date_claims DROP COLUMN capture_id",
    ):
        store._connection.execute(statement)
    store._connection.commit()
    rows = store._connection.execute("SELECT * FROM date_claims ORDER BY rowid").fetchall()
    store.close()
    assert not set(ADDED) & set(columns_of(path))
    return rows


def test_a_file_from_before_gains_the_columns_and_keeps_every_claim_as_one_that_counts(
    tmp_path: pathlib.Path,
) -> None:
    """Additive, and safe to meet again: the school's rows name no note, count, and read as
    they did; a second start changes nothing; a claim is still written after it."""
    path = tmp_path / "record.sqlite3"
    before = file_from_before(path)

    store = ProjectStateStore.open(path, fixture_clock())
    read = store.deadline_records(PRACTICE)
    store.close()
    again = ProjectStateStore.open(path, fixture_clock())
    again.record_claims(PRACTICE, [claim("2026-09-19", SourceChannel.PARENT_ENTRY)])
    history = again.claim_history(PRACTICE)

    assert set(ADDED) <= set(columns_of(path))
    assert len(read) == len(before)
    assert [item.record.asserted_value for item in history][-1] == "2026-09-19"
    assert all(
        (item.capture_id, item.capture_revision, item.active, item.withdrawn_at)
        == (None, None, True, None)
        for item in history
    )
    assert len(history) == len(before) + 1


def test_the_columns_and_their_index_arrive_together_or_not_at_all(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.sqlite3"
    before = file_from_before(path)
    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    def no_index(action: int, first: str | None, *rest: object) -> int:
        refused = action == sqlite3.SQLITE_CREATE_INDEX and first == "date_claims_capture_once"
        return sqlite3.SQLITE_DENY if refused else sqlite3.SQLITE_OK

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)  # type: ignore[call-overload]
        connection.set_authorizer(no_index)
        opened.append(connection)
        return connection  # type: ignore[no-any-return]

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(sqlite3.DatabaseError):
        ProjectStateStore.open(path, fixture_clock())
    for connection in opened:
        connection.close()
    monkeypatch.undo()

    assert not set(ADDED) & set(columns_of(path))
    store = ProjectStateStore.open(path, fixture_clock())
    assert set(ADDED) <= set(columns_of(path))
    assert len(store.deadline_records(PRACTICE)) == len(before)


def test_every_claim_is_written_by_naming_its_columns() -> None:
    """A positional insert breaks, or worse misfiles, the day a column is added."""
    source = pathlib.Path(project_state.__file__).read_text(encoding="utf-8")

    assert not re.search(r"INSERT\s+INTO\s+date_claims\s+VALUES", source)
    assert len(re.findall(r"INSERT\s+INTO\s+date_claims\s*\(", source)) >= 1


def test_a_claim_from_a_note_is_one_claim_however_often_it_is_sent(tmp_path: pathlib.Path) -> None:
    store = practice_store(tmp_path / "record.sqlite3")
    note = new_capture_id()
    for _ in range(2):
        with store._lock, store._writing():
            store._record_capture_claim_locked(
                PRACTICE, claim("2026-09-18"), capture_id=note, capture_revision=3
            )
    with store._lock, store._writing():
        store._record_capture_claim_locked(
            PRACTICE, claim("2026-09-19"), capture_id=note, capture_revision=4
        )

    history = [item for item in store.claim_history(PRACTICE) if item.capture_id == note]

    assert [(item.capture_revision, item.record.asserted_value) for item in history] == [
        (3, "2026-09-18"),
        (4, "2026-09-19"),
    ]
    assert all(isinstance(item, ClaimOnRecord) and item.active for item in history)


def test_a_claim_that_was_withdrawn_is_read_by_nothing_but_the_history(
    tmp_path: pathlib.Path,
) -> None:
    """Withdrawn, a note's claim leaves reconciliation, the week, and the digest, and is
    still there to read as history. The school's equal claim is untouched."""
    store = practice_store(tmp_path / "record.sqlite3")
    note = new_capture_id()
    store.record_claims(PRACTICE, [claim("2026-09-25", SourceChannel.LMS)])
    without = planning_digest(week_from(read_everything(store, store), MONDAY))
    with store._lock, store._writing():
        store._record_capture_claim_locked(
            PRACTICE, claim("2026-09-25"), capture_id=note, capture_revision=1
        )
    counted = planning_digest(week_from(read_everything(store, store), MONDAY))
    store._connection.execute(
        "UPDATE date_claims SET active = 0, withdrawn_at = ? WHERE capture_id = ?",
        (AT.isoformat(), note),
    )
    store._connection.commit()

    single = store.deadline_records(PRACTICE)
    bulk = store.deadline_records_by_assignment([PRACTICE])[PRACTICE]
    everything = store.deadline_records_by_assignment()[PRACTICE]
    history = store.claim_history(PRACTICE)

    assert counted != without
    assert planning_digest(week_from(read_everything(store, store), MONDAY)) == without
    for read in (single, bulk, everything):
        assert SourceChannel.STUDENT_REPORT not in {item.channel for item in read}
        assert SourceChannel.LMS in {item.channel for item in read}
    withdrawn = [item for item in history if item.capture_id == note]
    assert [(item.active, item.withdrawn_at) for item in withdrawn] == [(False, AT)]
    assert [item.active for item in history if item.capture_id is None] == [True] * (
        len(history) - 1
    )


@pytest.mark.parametrize(
    "damage",
    [
        "UPDATE date_claims SET active = 2 WHERE capture_id IS NOT NULL",
        "UPDATE date_claims SET capture_revision = 0 WHERE capture_id IS NOT NULL",
        "UPDATE date_claims SET capture_id = 'NOT-AN-ID' WHERE capture_id IS NOT NULL",
        "UPDATE date_claims SET withdrawn_at = 'then' WHERE capture_id IS NOT NULL",
    ],
)
def test_what_names_a_note_is_read_as_the_store_writes_it_or_the_history_says_so(
    tmp_path: pathlib.Path, damage: str
) -> None:
    store = practice_store(tmp_path / "record.sqlite3")
    with store._lock, store._writing():
        store._record_capture_claim_locked(
            PRACTICE, claim("2026-09-18"), capture_id=new_capture_id(), capture_revision=1
        )
    store._connection.execute(damage)
    store._connection.commit()

    with pytest.raises(project_state.UnreadableClaim):
        store.claim_history(PRACTICE)
