"""The part of the record's store that keeps what a parent said about which homework a school
row is about.

Mixed into the store of the record, which supplies the connection, the lock, and
the transaction that reserves the writer before it reads. A decision is written
by the paste's own save, in the same transaction as the rows, claims, reports,
and instructions it goes with, and read inside that transaction. Nothing here
chooses: it keeps what was answered, with the homework the card showed and a
fingerprint of what it showed, so a later paste can tell whether the answer
still covers what is on record.
"""

import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final, Literal, cast

from blossom.captures import Author
from blossom.reconciliation import SourceChannel

DecisionKind = Literal["same", "different", "which", "report_placed"]
"""What a parent said about a school row: the same homework as hers, different homework
with the same title, which of several it is, or which one a single report is about."""
DECISION_KINDS: Final = ("same", "different", "which", "report_placed")

CREATE_INTAKE_DECISIONS: Final = """
CREATE TABLE IF NOT EXISTS intake_decisions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('same', 'different', 'which', 'report_placed')),
    course TEXT NOT NULL,
    title TEXT NOT NULL,
    due_date TEXT,
    lands_on TEXT NOT NULL,
    shown TEXT NOT NULL,
    basis TEXT NOT NULL,
    creation TEXT UNIQUE,
    report TEXT,
    authored_by TEXT NOT NULL,
    channel TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    decided_on TEXT NOT NULL
)
"""
INDEX_INTAKE_DECISIONS: Final = (
    "CREATE INDEX IF NOT EXISTS intake_decisions_by_name ON intake_decisions (course, title)"
)
DECISIONS_NAMED: Final = """
SELECT sequence, kind, course, title, due_date, lands_on, shown, basis, creation, report,
       authored_by, channel, decided_at, decided_on
FROM intake_decisions
WHERE (course, title) IN (
    SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]') FROM json_each(?)
)
ORDER BY sequence
"""
INSERT_DECISION: Final = """
INSERT INTO intake_decisions (
    kind, course, title, due_date, lands_on, shown, basis, creation, report,
    authored_by, channel, decided_at, decided_on
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class UnreadableDecision(RuntimeError):
    """A decision on record that cannot be read as one the store writes: nothing that depends
    on it is decided, and the paste is refused whole."""


@dataclass(frozen=True)
class IntakeDecision:
    """One answer kept: what it was, the name and due date of the row it was about, where
    that row landed, the homework the card showed, and the fingerprint of what it showed."""

    sequence: int
    kind: DecisionKind
    course: str
    title: str
    due_date: date | None
    lands_on: str
    shown: tuple[str, ...]
    basis: str
    creation: str | None
    report: Mapping[str, str | None] | None
    authored_by: str
    channel: str


@dataclass(frozen=True)
class DecisionToKeep:
    """An answer the save is about to keep, before the store numbers it."""

    kind: DecisionKind
    course: str
    title: str
    due_date: date | None
    lands_on: str
    shown: tuple[str, ...]
    basis: str
    creation: str | None = None
    report: Mapping[str, str | None] | None = None


def _decision_of(row: tuple[object, ...]) -> IntakeDecision:
    """One row as the store wrote it, or ``UnreadableDecision``."""
    try:
        (
            sequence,
            kind,
            course,
            title,
            due,
            lands_on,
            shown,
            basis,
            creation,
            report,
            authored_by,
            channel,
            _,
            _,
        ) = row
        if kind not in DECISION_KINDS or type(sequence) is not int:
            raise ValueError(kind)
        if not all(type(value) is str for value in (course, title, lands_on, basis, channel)):
            raise ValueError(lands_on)
        named = json.loads(cast(str, shown))
        if not isinstance(named, list) or not all(type(item) is str for item in named):
            raise ValueError(shown)
        placed = None if report is None else json.loads(cast(str, report))
        if placed is not None and not isinstance(placed, dict):
            raise ValueError(report)
        return IntakeDecision(
            sequence=sequence,
            kind=cast(DecisionKind, kind),
            course=cast(str, course),
            title=cast(str, title),
            due_date=None if due is None else date.fromisoformat(cast(str, due)),
            lands_on=cast(str, lands_on),
            shown=tuple(named),
            basis=cast(str, basis),
            creation=None if creation is None else str(creation),
            report=placed,
            authored_by=str(authored_by),
            channel=cast(str, channel),
        )
    except (TypeError, ValueError) as error:
        msg = "a saved answer about which homework a row is about cannot be read"
        raise UnreadableDecision(msg) from error


class IntakeDecisionRecords:
    """The part of the record's store that keeps the answers about which homework a school
    row is about."""

    _connection: sqlite3.Connection
    _lock: "threading.RLock"

    def _writing(self) -> AbstractContextManager[None]:
        raise NotImplementedError

    def _create_intake_decision_table(self) -> None:
        """The table and its index, in the caller's transaction."""
        self._connection.execute(CREATE_INTAKE_DECISIONS)
        self._connection.execute(INDEX_INTAKE_DECISIONS)

    def intake_decisions(
        self, pairs: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], tuple[IntakeDecision, ...]]:
        """Every answer kept for each class and title, oldest first, in one statement however
        many are asked about. Read in the caller's transaction, so a save reads what it will
        be judged against."""
        wanted = list(dict.fromkeys(pairs))
        with self._lock:
            rows = self._connection.execute(
                DECISIONS_NAMED, (json.dumps([list(name) for name in wanted]),)
            ).fetchall()
        found: dict[tuple[str, str], list[IntakeDecision]] = {name: [] for name in wanted}
        for row in rows:
            decision = _decision_of(row)
            found[(decision.course, decision.title)].append(decision)
        return {name: tuple(decisions) for name, decisions in found.items()}

    def record_intake_decision(
        self,
        decision: DecisionToKeep,
        *,
        authored_by: Author | None,
        channel: SourceChannel,
        now: datetime,
        today: date,
    ) -> None:
        """Keep one answer, in the caller's write transaction; a second decision with the
        same creation token is refused by the table."""
        with self._lock, self._writing():
            self._connection.execute(
                INSERT_DECISION,
                (
                    decision.kind,
                    decision.course,
                    decision.title,
                    None if decision.due_date is None else decision.due_date.isoformat(),
                    decision.lands_on,
                    json.dumps(list(decision.shown)),
                    decision.basis,
                    decision.creation,
                    None if decision.report is None else json.dumps(dict(decision.report)),
                    authored_by or "household",
                    channel.value,
                    now.astimezone(UTC).isoformat(),
                    today.isoformat(),
                ),
            )
