# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
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
import re
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final, Literal, cast, get_args

from blossom.captures import Author
from blossom.reconciliation import SourceChannel

DecisionKind = Literal["same", "different", "which", "report_placed", "renamed"]
"""What a parent said about a school row: the same homework as hers, different homework
with the same title, which of several it is, which one a single report is about, or
homework already here under another name."""
DECISION_KINDS: Final = ("same", "different", "which", "report_placed", "renamed")
CREATION: Final = re.compile(r"[0-9a-f]{32}")
"""A creation token as the review page makes it."""
FINGERPRINT: Final = re.compile(r"[0-9a-f]{64}")
"""A fingerprint of what a question showed, as ``identity_basis`` makes it."""
PLACEMENT_FIELDS: Final = frozenset({"channel", "status", "day", "source_date_text"})

CREATE_INTAKE_DECISIONS: Final = """
CREATE TABLE IF NOT EXISTS intake_decisions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('same', 'different', 'which', 'report_placed', 'renamed')),
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


def _day(text: object) -> date:
    """A day exactly as the store writes one."""
    day = date.fromisoformat(cast(str, text))
    if day.isoformat() != text:
        raise ValueError(text)
    return day


def _placement(text: object) -> dict[str, str | None]:
    """A report's placement as the store writes it: the report's channel, status, day, and
    the date written beside the work, if any."""
    placed = json.loads(cast(str, text))
    if not isinstance(placed, dict) or placed.keys() != PLACEMENT_FIELDS:
        raise ValueError(text)
    SourceChannel(placed["channel"])
    _day(placed["day"])
    beside = placed["source_date_text"]
    if type(placed["status"]) is not str or not (beside is None or type(beside) is str):
        raise ValueError(text)
    return placed


def _checked(sequence: int, fields: tuple[object, ...]) -> IntakeDecision:
    """The columns after the sequence as the store writes them, or ``ValueError`` (or
    ``TypeError``): each in its format, and the ones its kind needs in agreement. An answer
    lands on homework its question showed, or on the one a Different's token makes."""
    (
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
        decided_at,
        decided_on,
    ) = fields
    words = (course, title, lands_on, shown, basis, authored_by, channel, decided_at)
    if kind not in DECISION_KINDS or not all(type(value) is str for value in words):
        raise ValueError(kind)
    named = json.loads(cast(str, shown))
    if (
        not isinstance(named, list)
        or not named
        or not all(type(item) is str and item for item in named)
        or len(set(named)) != len(named)
    ):
        raise ValueError(shown)
    if FINGERPRINT.fullmatch(cast(str, basis)) is None:
        raise ValueError(basis)
    if authored_by not in get_args(Author):
        raise ValueError(authored_by)
    SourceChannel(cast(str, channel))
    moment = datetime.fromisoformat(cast(str, decided_at))
    if moment.utcoffset() != timedelta(0) or moment.isoformat() != decided_at:
        raise ValueError(decided_at)
    _day(decided_on)
    if not (report is None or type(report) is str):
        raise ValueError(report)
    if kind == "different":
        if creation is None or CREATION.fullmatch(cast(str, creation)) is None:
            raise ValueError(creation)
        if lands_on != f"assignment-{creation}":
            raise ValueError(lands_on)
    elif creation is not None or lands_on not in named:
        raise ValueError(lands_on)
    if kind == "renamed" and named != [lands_on]:
        raise ValueError(shown)
    placed = None if report is None else _placement(report)
    due_date = None if due is None else _day(due)
    if (kind == "report_placed") != (placed is not None) or (placed and due_date is not None):
        raise ValueError(report)
    return IntakeDecision(
        sequence=sequence,
        kind=cast(DecisionKind, kind),
        course=cast(str, course),
        title=cast(str, title),
        due_date=due_date,
        lands_on=cast(str, lands_on),
        shown=tuple(named),
        basis=cast(str, basis),
        creation=cast(str | None, creation),
        report=placed,
        authored_by=cast(str, authored_by),
        channel=cast(str, channel),
    )


def _decision_of(row: tuple[object, ...]) -> IntakeDecision:
    """One row as the store wrote it, or ``UnreadableDecision``."""
    try:
        sequence, *fields = row
        if type(sequence) is not int:
            raise ValueError(sequence)
        return _checked(sequence, tuple(fields))
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
        """The table and its index, in the caller's transaction. A table from before
        ``renamed`` answers is made again with every row as it was: SQLite can't change
        a table's check in place."""
        made = self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'intake_decisions'"
        ).fetchone()
        if made is not None and "'renamed'" not in str(made[0]):
            self._remake_for_renamed()
        self._connection.execute(CREATE_INTAKE_DECISIONS)
        self._connection.execute(INDEX_INTAKE_DECISIONS)

    def _remake_for_renamed(self) -> None:
        """Copy every answer, its sequence kept, into a table that takes ``renamed`` ones,
        and put it in the old table's place, with the counter the old table had. In the
        caller's transaction, so a refused step leaves the old one as it was."""
        self._connection.execute(
            CREATE_INTAKE_DECISIONS.replace(
                "IF NOT EXISTS intake_decisions", "intake_decisions_next"
            )
        )
        self._connection.execute(
            "INSERT INTO intake_decisions_next SELECT * FROM intake_decisions ORDER BY sequence"
        )
        counted = self._connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'intake_decisions'"
        ).fetchone()
        self._connection.execute("DROP TABLE intake_decisions")
        self._connection.execute("ALTER TABLE intake_decisions_next RENAME TO intake_decisions")
        if counted is not None:
            self._connection.execute(
                "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'intake_decisions'",
                (counted[0],),
            )

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
        """Keep one answer, in the caller's write transaction. An answer the store couldn't
        read back is refused before anything is written, and a second decision with the same
        creation token is refused by the table."""
        fields = (
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
        )
        try:
            _checked(0, fields)
        except (TypeError, ValueError) as error:
            msg = "this answer about which homework a row is about cannot be kept"
            raise ValueError(msg) from error
        with self._lock, self._writing():
            self._connection.execute(INSERT_DECISION, fields)
