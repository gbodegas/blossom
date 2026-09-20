"""Store one: structured project state, queried exactly.

Assignments, due dates, dependencies and reported submission status. Small,
structured, and always asked about by exact criteria, which is why this is
SQLite and not a vector index: retrieving a due date by similarity would return
the most similar assignment rather than the correct one.

Retention is the academic year, then archive. Noticing that a project entered
eleven days ago has no progress against it needs that history.

The store is the household's file at ``BLOSSOM_DATABASE_PATH``, shared with the
drafts, so what the family enters outlives a restart. Beside the assignments it
keeps every channel's claim about an assignment's due date, as the claim was
made, which is what the reconciliation reads. A fixture, when one is named,
is read only into a blank file, in one transaction with the file's own
tables, so a start cut short leaves nothing that the next start would take
for the household's record; a file with anything in it is that record,
whatever it holds, and is left alone.

The field is named ``reported_submission_status`` on purpose: a submission flag
confirms a file was uploaded, not that the assignment was finished, that the
right file went up, or that the teacher considers it done. Code reading it must
not treat it as completion.
"""

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal, NamedTuple, Self, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator, model_validator

from blossom.authored_text import multiline, single_line
from blossom.clock import Clock
from blossom.hand_in import (
    HAND_IN_NOTE_MAX_LENGTH,
    NEEDS_HAND_IN,
    NEXT_ACTION_MAX_LENGTH,
    HandInAlreadySaved,
    HandInConflict,
    HandInEvent,
    HandInProjection,
    HandInSaved,
    HandInState,
    HandInUndone,
    project,
)
from blossom.reconciliation import SourceChannel, SourceRecord
from blossom.retrieval import RetrievalResult
from blossom.stores.paths import refuse_unsafe_path

DUE_THIS_WEEK_KEY = "due_this_week"
logger = logging.getLogger(__name__)

DUE_THIS_WEEK_SPAN = timedelta(days=6)
EVERY_STUDENT_REPORT: Final = """
    SELECT report_id, assignment_id, operation, status, note, reported_at, reported_on,
        previous_report_id, undoes_report_id
    FROM student_reports
    ORDER BY rowid
"""
STUDENT_REPORTS_NAMED: Final = """
    SELECT report_id, assignment_id, operation, status, note, reported_at, reported_on,
        previous_report_id, undoes_report_id
    FROM student_reports
    WHERE assignment_id IN (SELECT value FROM json_each(?))
    ORDER BY rowid
"""
EVERY_DATE_CLAIM: Final = """
    SELECT assignment_id, channel, asserted_value, observed_at, confidence, seen_in
    FROM date_claims
    ORDER BY rowid
"""
DATE_CLAIMS_NAMED: Final = """
    SELECT assignment_id, channel, asserted_value, observed_at, confidence, seen_in
    FROM date_claims
    WHERE assignment_id IN (SELECT value FROM json_each(?))
    ORDER BY rowid
"""
EVERY_HAND_IN_EVENT: Final = """
    SELECT event_id, assignment_id, operation, state, next_action, note, cue_at_utc,
        reported_at_utc, reported_on, previous_event_id, undone_event_id, sequence
    FROM hand_in_events
    ORDER BY sequence
"""
HAND_IN_EVENTS_NAMED: Final = """
    SELECT event_id, assignment_id, operation, state, next_action, note, cue_at_utc,
        reported_at_utc, reported_on, previous_event_id, undone_event_id, sequence
    FROM hand_in_events
    WHERE assignment_id IN (SELECT value FROM json_each(?))
    ORDER BY sequence
"""
HAND_IN_CHAIN: Final = """
    SELECT event_id, assignment_id, operation, state, next_action, note, cue_at_utc,
        reported_at_utc, reported_on, previous_event_id, undone_event_id, sequence
    FROM hand_in_events
    WHERE assignment_id = ? ORDER BY sequence
"""
HAND_IN_HEAD: Final = """
    SELECT event_id, assignment_id, operation, state, next_action, note, cue_at_utc,
        reported_at_utc, reported_on, previous_event_id, undone_event_id, sequence
    FROM hand_in_events
    WHERE assignment_id = ? ORDER BY sequence DESC LIMIT 1
"""
HAND_IN_EVENT_NAMED: Final = """
    SELECT event_id, assignment_id, operation, state, next_action, note, cue_at_utc,
        reported_at_utc, reported_on, previous_event_id, undone_event_id, sequence
    FROM hand_in_events
    WHERE event_id = ?
"""
EVERY_FAMILY_CHECK: Final = """
    SELECT check_id, assignment_id, operation, basis, note, checked_at, checked_on,
        previous_check_id
    FROM family_checks
    ORDER BY rowid
"""
FAMILY_CHECKS_NAMED: Final = """
    SELECT check_id, assignment_id, operation, basis, note, checked_at, checked_on,
        previous_check_id
    FROM family_checks
    WHERE assignment_id IN (SELECT value FROM json_each(?))
    ORDER BY rowid
"""
"""The reads by assignment, each a statement written once and whole. The names go in as one
bound value, a JSON list the statement unpacks, so no statement is ever put together from
its pieces, however many names there are."""


StudentStatus = Literal["done", "not_yet"]
"""What she can say about her part of an assignment: finished, or not yet."""
DONE: Final = "done"
NOT_YET: Final = "not_yet"
REPORT: Final = "report"
UNDO: Final = "undo"
NOTE_MAX_LENGTH: Final = 500
"""How long her note may be, in code points, once its edges and line endings are normalized.
A parent's note with a check is held to the same length."""
CHECKED: Final = "checked"
REOPENED: Final = "reopened"


def normalize_note(text: str | None) -> str | None:
    """Her note as it is kept: line endings as one kind, edges trimmed, blank as none.

    The words inside stay as she typed them, line breaks included; the same
    note typed on two devices reads the same, so a repeat is a repeat. Every
    way a note comes to be kept goes through here: her page, the store's own
    callers, and a seed read from a file.
    """
    if text is None:
        return None
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return cleaned or None


class AssignmentKind(StrEnum):
    """What sort of work an item is, so a planner can size it.

    A school portal lists a signed syllabus beside an essay. Both have to be
    accounted for, but one is minutes of paperwork and the other is a sitting,
    and a plan that gives each an hour is wrong about one of them.
    """

    HOMEWORK = "HOMEWORK"
    TASK = "TASK"


class Assignment(BaseModel):
    """One assignment as structured state, with dependencies and reported status.

    ``due_date`` may be ``None``. A source can list an item and give it no date,
    and an undated item still occupies the week; hiding it would be the system
    deciding it does not matter. ``assigned_on`` is when the work became
    available, which a portal shows as a separate row from the due date; the
    two are different facts about one item, and an item seen under both is
    one assignment, not two.
    """

    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    course: str
    title: str
    due_date: date | None
    dependencies: list[str]
    reported_submission_status: str
    assigned_on: date | None = None
    kind: AssignmentKind = AssignmentKind.HOMEWORK
    note: str | None = None
    """What the teacher wrote under the card, as the portal shows it, or what a parent
    typed with the assignment: an instruction about the work, kept with the assignment
    because it is part of it. ``origins`` says which."""
    origins: dict[str, SourceChannel] = {}
    """Where each of the row's facts came from, by field: ``record`` for the row
    itself, then ``note``, ``kind``, ``due_date``, and ``assigned_on`` when a channel
    supplied them. A parent's entry keeps its origin through a later paste, and a
    parent's correction of a type or a note is told from the school's text."""

    @field_validator("assignment_id")
    @classmethod
    def _can_be_named_in_an_address(cls, assignment_id: str) -> str:
        """An id of one dot or two is refused. A browser reads either as a step in a path
        and resolves it before it sends the address, escaped or not, so no page could link
        to the assignment or send a form about it. Any other id is kept as it is."""
        if assignment_id in {".", ".."}:
            msg = f"the id {assignment_id!r} reads as a step in a path, so no address can name it"
            raise ValueError(msg)
        return assignment_id


class StatusReport(BaseModel):
    """What a school channel reported about an assignment's status, as of a day.

    A fact of another kind than a date claim: not a value to reconcile but a
    statement the school made, kept with who made it and when, so that
    everyone reading it sees a fact reported by the school and the day it
    was reported. ``dated_by`` says where the day came from: the email's own
    date line when the paste carried one, or the day it was pasted.
    """

    model_config = ConfigDict(extra="forbid")

    status: str
    channel: SourceChannel
    reported_on: date
    dated_by: str
    observed_at: AwareDatetime
    source_date_text: str | None = None
    """The date as the source wrote it beside the assignment, ``09/09`` in the school's
    email, kept as text: what it means is not established, so it is shown, not read."""


def kept_note(note: str | None) -> str | None:
    """A note as every event holds it: normalized, and within the limit, or a ``ValueError``.

    An event made from a form, by a caller of the store, or from a seed file
    holds the same note for the same words, so the comparison that finds an
    update already saved never turns on a line ending, and nothing longer
    than the limit is ever kept, whoever made the event.
    """
    kept = normalize_note(note)
    if kept is not None and len(kept) > NOTE_MAX_LENGTH:
        msg = f"a note is at most {NOTE_MAX_LENGTH} characters; this one is {len(kept)}"
        raise ValueError(msg)
    return kept


class StudentReport(BaseModel):
    """One thing she said about an assignment, as of a moment: an event in a chain.

    Her reports are the record of her own account of her work, kept apart
    from what the school reports and from the record's dates. Each event
    carries the state that stands after it: for a report, the status and
    note she gave; for an undo, the status and note restored, or none. The
    chain runs through ``previous_report_id``, and the day is the
    household's day the event was accepted, not a claim about when the
    work was done.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: str
    assignment_id: str
    operation: Literal["report", "undo"]
    status: StudentStatus | None
    """What stands after this event: ``None`` only when an undo restores no report."""
    note: str | None = None
    reported_at: AwareDatetime
    reported_on: date
    previous_report_id: str | None = None
    """The event before this one under the same assignment; ``None`` for the first."""
    undoes_report_id: str | None = None
    """For an undo, the report it takes back; ``None`` for a report."""

    @field_validator("note")
    @classmethod
    def _is_kept_as_it_is_compared(cls, note: str | None) -> str | None:
        """The note as every event holds it: normalized, and within the limit."""
        return kept_note(note)

    @model_validator(mode="after")
    def _is_a_whole_event(self) -> Self:
        """An event has one of two shapes, and anything else is refused where it is read.

        A report says done or not yet and takes nothing back. An undo takes
        back the event before it, so it names that event twice, as what it
        follows and as what it undoes; it can never be first. A note stands
        only with a status. What the chain adds, that the event named is the
        head, is a report, and that an undo restores what stood before it,
        is the store's to check as it writes.
        """
        if self.operation == REPORT:
            if self.status is None:
                msg = f"report {self.report_id!r} says neither done nor not yet"
                raise ValueError(msg)
            if self.undoes_report_id is not None:
                msg = f"report {self.report_id!r} names a report to take back, as only an undo does"
                raise ValueError(msg)
        elif self.undoes_report_id is None or self.previous_report_id != self.undoes_report_id:
            msg = f"undo {self.report_id!r} must follow the report it takes back, and name it"
            raise ValueError(msg)
        if self.status is None and self.note is not None:
            msg = f"event {self.report_id!r} carries a note with no status for it to stand with"
            raise ValueError(msg)
        return self


class FamilyCheck(BaseModel):
    """One event of the family's about a discrepancy on an assignment: that a parent checked
    it with her, or that it is open to check again.

    A check is the household's own record, kept apart from her reports and
    the school's: it changes neither account and sets no status. Each check
    names what it was made against by its basis, the assignment, the report
    that began her Done, and each school statement of missing then current,
    so a row is checked only while those are the facts, and open again when
    they are not. The chain runs through ``previous_check_id``; a reopening
    follows the check it reopens and carries its basis. The day is the
    household's day the event was accepted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    check_id: str
    assignment_id: str
    operation: Literal["checked", "reopened"]
    basis: str
    """What the check was made against, as the page carried it: the assignment id, the id of
    the report that began the Done standing, and each current statement of missing, sorted."""
    note: str | None = None
    """A parent's words with the check, which her card shows; none with a reopening."""
    checked_at: AwareDatetime
    checked_on: date
    previous_check_id: str | None = None
    """The check event before this one under the same assignment; ``None`` for the first."""

    @field_validator("note")
    @classmethod
    def _is_kept_as_it_is_shown(cls, note: str | None) -> str | None:
        """The note as every event holds it: normalized, and within the limit."""
        return kept_note(note)

    @model_validator(mode="after")
    def _is_a_whole_check(self) -> Self:
        """A check event has one of two shapes, and anything else is refused where it is read.

        Both name a basis. A check may carry a note. A reopening carries
        none and follows the check it reopens, so it is never first. What
        the chain adds, that the event followed is the head and that a
        reopening reopens a check, is the store's to check as it writes.
        """
        if not self.basis.strip():
            msg = f"check {self.check_id!r} names no basis"
            raise ValueError(msg)
        if self.operation == REOPENED:
            if self.note is not None:
                msg = f"reopening {self.check_id!r} carries a note, as only a check does"
                raise ValueError(msg)
            if self.previous_check_id is None:
                msg = f"reopening {self.check_id!r} must follow the check it reopens"
                raise ValueError(msg)
        return self


class Seed(NamedTuple):
    """What a blank file is seeded with: assignments, the claims about each one's date, and
    any reports she is taken to have made, which only the sample supplies."""

    assignments: Sequence[Assignment]
    claims: Mapping[str, Sequence[SourceRecord]]
    student_reports: Sequence[StudentReport] = ()


class UnknownReport(LookupError):
    """A form named an update that is not one of the assignment's: no such event, or an
    event under another assignment. Such a name proves nothing about the page it came
    from, so it is refused before anything is compared or written."""


@dataclass(frozen=True)
class HandInReadings:
    """What a page reads about turning work in: a reading for each assignment whose
    record can be read, and the ones whose record cannot, named apart. An assignment
    in the second is never shown as having said nothing."""

    readable: dict[str, HandInProjection]
    unreadable: frozenset[str]


class UnknownHandIn(LookupError):
    """A form named a hand-in event that is not one of the assignment's: no such event, or
    one under another assignment. Such a name proves nothing about the page it came
    from, so it is refused before anything is compared or written."""


class UnknownCheck(LookupError):
    """A form named a check that is not one of the assignment's: no such event, or an event
    under another assignment. Such a name proves nothing about the page it came from, so
    it is refused before anything is compared or written."""


class NoteTooLong(ValueError):
    """A note past the limit reached the store. The pages say so before it gets this far;
    this is the same rule for every other caller, and nothing is written."""

    def __init__(self, length: int) -> None:
        super().__init__(f"a note is at most {NOTE_MAX_LENGTH} characters; this one is {length}")


class CouldNotSave(RuntimeError):
    """The file refused a write, hers or the family's, or the chain failed its checks while
    writing.

    Whatever was begun was rolled back with it, so nothing of the event is
    kept; the page that catches this says so and keeps the words typed.
    """

    def __init__(
        self, assignment_id: str, cause: BaseException, *, what: str = "the update"
    ) -> None:
        super().__init__(f"{what} on {assignment_id!r} could not be saved: {cause}")


class UnknownAssignment(LookupError):
    """A report was made about an assignment the record does not have."""


@dataclass(frozen=True)
class Saved:
    """A report was appended."""

    report: StudentReport


@dataclass(frozen=True)
class AlreadySaved:
    """What she sent is what stands already; nothing was written."""

    head: StudentReport


@dataclass(frozen=True)
class Conflict:
    """The chain moved on since her page was made; nothing was written."""

    head: StudentReport | None


@dataclass(frozen=True)
class Undone:
    """An undo was appended, restoring what stood before the report it takes back."""

    report: StudentReport


@dataclass(frozen=True)
class Checked:
    """A check was appended: a parent checked the discrepancy with her."""

    check: FamilyCheck


@dataclass(frozen=True)
class AlreadyChecked:
    """The same check stands already, against the facts as they are; nothing was written."""

    check: FamilyCheck


@dataclass(frozen=True)
class CheckConflict:
    """What the page rested on moved since it was made, the facts or the family's own record;
    nothing was written."""

    head: FamilyCheck | None
    """The last check event now, or ``None`` when there is none."""


@dataclass(frozen=True)
class Reopened:
    """A reopening was appended; the check it reopens stays in the record."""

    check: FamilyCheck


class ProjectStateStore:
    """SQLite-backed project state, opened once and shared across worker threads.

    The connection is created at application startup rather than per request,
    so every statement is serialized behind a lock. ``sqlite3`` refuses a
    connection used from a thread other than the one that created it, and
    FastAPI runs synchronous handlers in a thread pool.
    """

    name = "project_state"
    retention_policy = "Keep structured assignment state for the academic year, then archive."

    def __init__(
        self, connection: sqlite3.Connection, clock: Clock, *, tables: bool = True
    ) -> None:
        self._connection = connection
        # Required, not defaulted: a clock needs the household's zone, and this
        # store has no business choosing one.
        self._clock = clock
        # Shared across FastAPI's handler threads; see blossom/dependencies.py.
        # Re-entrant, so a caller holding the store through ``exclusively``
        # can read and write through the same methods everyone else uses.
        self._lock = threading.RLock()
        self.path: Path | None = None
        """The file, when the store was opened on one; a connection handed in has none."""
        self.created = False
        """Whether the file was blank when this start opened it; only such a file is seeded."""
        if tables:
            self._create_tables()

    def _create_tables(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS assignments (
                assignment_id TEXT PRIMARY KEY,
                course TEXT NOT NULL,
                title TEXT NOT NULL,
                due_date TEXT,
                dependencies TEXT NOT NULL,
                reported_submission_status TEXT NOT NULL,
                assigned_on TEXT,
                kind TEXT NOT NULL,
                note TEXT,
                origins TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS status_reports (
                assignment_id TEXT NOT NULL,
                status TEXT NOT NULL,
                channel TEXT NOT NULL,
                reported_on TEXT NOT NULL,
                dated_by TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                source_date_text TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS status_reports_by_assignment
            ON status_reports (assignment_id)
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS date_claims (
                assignment_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                asserted_value TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                confidence REAL NOT NULL,
                seen_in TEXT
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS date_claims_by_assignment ON date_claims (assignment_id)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS student_reports (
                report_id TEXT PRIMARY KEY,
                assignment_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                status TEXT,
                note TEXT,
                reported_at TEXT NOT NULL,
                reported_on TEXT NOT NULL,
                previous_report_id TEXT,
                undoes_report_id TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS student_reports_by_assignment
            ON student_reports (assignment_id)
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS family_checks (
                check_id TEXT PRIMARY KEY,
                assignment_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                basis TEXT NOT NULL,
                note TEXT,
                checked_at TEXT NOT NULL,
                checked_on TEXT NOT NULL,
                previous_check_id TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS family_checks_by_assignment
            ON family_checks (assignment_id)
            """
        )
        # Her account of turning work in arrives whole or not at all: on a file
        # from before, a start that is refused the index leaves no table behind
        # it. Inside a first start's transaction this joins it and ends nothing.
        with self._writing():
            self._create_hand_in_tables()
        self._upgrade()

    def _create_hand_in_tables(self) -> None:
        """The hand-in table and its index, in that order, in the caller's transaction.

        The order of events is the order the file gave them, a number of its
        own that is never reused, and an event is named by its id, never by
        that number. That each event belongs to an assignment on record, and
        follows the head of that assignment's chain, is checked in the
        transaction that writes it; the file has no rule of its own for it.
        """
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS hand_in_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                assignment_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                state TEXT,
                next_action TEXT,
                note TEXT,
                cue_at_utc TEXT,
                reported_at_utc TEXT NOT NULL,
                reported_on TEXT NOT NULL,
                previous_event_id TEXT,
                undone_event_id TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS hand_in_events_by_assignment
            ON hand_in_events (assignment_id, sequence)
            """
        )

    def _upgrade(self) -> None:
        """Bring a file from before up to this schema, by adding, and keep what it holds.

        Columns a file lacks are added; no row is dropped, folded, or
        rewritten. Every claim a file holds is kept, two observations of the
        same value at different times included: they are the history of what
        each channel said and when. Pasting the same page twice adds nothing
        twice, but that is the import's doing, which compares before it
        writes, and not this table's. The one thing taken away is an index a
        version between made, which held a claim once and so refused that
        history; a file that carries it loses the index and keeps its rows.
        Reports are the other way about: one per channel, status, and day is
        the promise, kept by an index, and a file from a version without it
        keeps the first of any duplicates.
        """
        self._connection.execute("DROP INDEX IF EXISTS date_claims_once")
        indexes = {
            str(row[1]) for row in self._connection.execute("PRAGMA index_list(status_reports)")
        }
        if "status_reports_once" not in indexes:
            # A report is one per channel, status, and day, as promised; a
            # file from a version that kept no index on that is folded to
            # one, the first kept, before the index is made. The fold is a
            # write, so it is committed here unless a start's own transaction
            # is open around it, which then commits it with the tables.
            outer = self._connection.in_transaction
            self._connection.execute(
                """
                DELETE FROM status_reports WHERE rowid NOT IN (
                    SELECT MIN(rowid) FROM status_reports
                    GROUP BY assignment_id, channel, status, reported_on
                )
                """
            )
            self._connection.execute(
                """
                CREATE UNIQUE INDEX status_reports_once
                ON status_reports (assignment_id, channel, status, reported_on)
                """
            )
            if not outer and self._connection.in_transaction:
                self._connection.commit()
        columns = {
            str(row[1]) for row in self._connection.execute("PRAGMA table_info(assignments)")
        }
        if "note" not in columns:
            self._connection.execute("ALTER TABLE assignments ADD COLUMN note TEXT")
        if "origins" not in columns:
            self._connection.execute("ALTER TABLE assignments ADD COLUMN origins TEXT")
        reports = {
            str(row[1]) for row in self._connection.execute("PRAGMA table_info(status_reports)")
        }
        if "source_date_text" not in reports:
            self._connection.execute("ALTER TABLE status_reports ADD COLUMN source_date_text TEXT")

    @classmethod
    def open(cls, path: Path, clock: Clock) -> "ProjectStateStore":
        """Open the household's file, refusing the places the saved-state store refuses.

        The tables are made and kept at once. Whether the file was blank is
        noted for the caller, after SQLite has finished with whatever an
        earlier start left in the file's journal.
        """
        connection, safe, blank = cls._connect(path)
        store = cls(connection, clock)
        store.path = safe
        store.created = blank
        return store

    @classmethod
    def initialize(
        cls, path: Path, clock: Clock, seed: Callable[[], Seed] | None = None
    ) -> "ProjectStateStore":
        """Open the household's file for the application, seeding a blank one.

        A blank file is this start's: its tables and, when ``seed`` is given,
        what it returns are written in one transaction, so a start cut short
        by anything, an error or the process ending, leaves nothing the next
        start would take for the household's record; that start finds the
        file blank again, after SQLite has rolled the journal back, and begins
        afresh. A file with anything in it is the household's record, whatever
        it holds, and is left alone, its tables added when missing. A seed that
        cannot be read stops the start, and the file this start made goes
        with it.
        """
        connection, safe, blank = cls._connect(path)
        store = cls(connection, clock, tables=not blank)
        store.path = safe
        store.created = blank
        if not blank:
            return store
        try:
            with store._lock, connection:
                # Begun by hand, since the tables would otherwise be kept on
                # their own, before the seed, and a file with tables is judged
                # to be the household's.
                connection.execute("BEGIN")
                store._create_tables()
                if seed is not None:
                    given = seed()
                    store._upsert_assignments_locked(given.assignments)
                    for assignment_id, records in given.claims.items():
                        store._record_claims_locked(assignment_id, records)
                    for report in given.student_reports:
                        store._append_student_report_locked(report)
        except BaseException:
            store.discard_if_new()
            raise
        return store

    @staticmethod
    def _connect(path: Path) -> tuple[sqlite3.Connection, Path, bool]:
        """Connect, and say whether the file is blank: no tables at all.

        The first thing done is a read, so SQLite rolls back whatever a start
        cut short left in the journal before the file is judged.
        """
        safe = refuse_unsafe_path(path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(safe, check_same_thread=False)
        connection.execute("PRAGMA secure_delete=ON")
        tables = connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        return connection, safe, int(tables) == 0

    def exclusively(self) -> AbstractContextManager[object]:
        """Hold the store for one caller, so reads and a write between them see one record.

        The lock is re-entrant: inside the block the caller reads and writes
        through the same methods as anyone else, and no one else gets between
        its comparison and its write.
        """
        return self._lock

    @contextmanager
    def reading(self) -> Iterator[None]:
        """Hold the store and read one snapshot of the file, however many reads it takes.

        The lock keeps this process's other callers out, but the drafts, her
        signals, and her requests write the same file through connections of
        their own, and the lock is nothing to them. A read transaction is:
        from the first read inside the block until its end, SQLite holds the
        file as it was, and another connection's commit waits.

        A transaction is begun only when none is active, and only the one begun
        here is ended here, committed when the block succeeds and rolled back
        when it fails. Inside a caller's own transaction the block joins it and
        ends nothing, since committing or rolling back there would decide the
        caller's writes for it. Keep the block to the reads themselves and
        finish before rendering, a model call, or another store: a writer
        elsewhere waits for as long as this is held.
        """
        with self._lock:
            if self._connection.in_transaction:
                yield
                return
            self._connection.execute("BEGIN DEFERRED")
            try:
                yield
            except BaseException:
                self._connection.rollback()
                raise
            self._connection.commit()

    def discard_if_new(self) -> None:
        """Close, and remove the file if it was blank when this start opened it, so a
        start that fails partway leaves the file as it was: absent."""
        self.close()
        if self.created and self.path is not None:
            self.path.unlink(missing_ok=True)

    def close(self) -> None:
        """Close the underlying connection. Called when the application shuts down."""
        with self._lock:
            self._connection.close()

    def upsert_assignments(self, assignments: Iterable[Assignment]) -> None:
        """Insert or update each assignment, keyed by ``assignment_id``, all or none.

        A batch that fails partway, in the database or in what it is given,
        is rolled back whole, so nothing of it shows, nothing of it is kept
        by a later write, and the file is free for the other stores at once.
        """
        with self._lock, self._connection:
            self._upsert_assignments_locked(assignments)

    def put_on_record(
        self,
        assignments: Iterable[Assignment],
        claims: Mapping[str, Iterable[SourceRecord]],
        reports: Mapping[str, Iterable[StatusReport]] | None = None,
    ) -> None:
        """Keep assignments, the claims about their dates, and what the school reports, in
        one transaction, all or none, like the writes above."""
        with self._lock, self._connection:
            self._upsert_assignments_locked(assignments)
            for assignment_id, records in claims.items():
                self._record_claims_locked(assignment_id, records)
            for assignment_id, said in (reports or {}).items():
                self._record_status_reports_locked(assignment_id, said)

    def record_status_reports(self, assignment_id: str, reports: Iterable[StatusReport]) -> None:
        """Keep what a school channel reported about one assignment, once per day and status,
        and set the row's reported status from the latest report, in one transaction."""
        with self._lock, self._connection:
            self._record_status_reports_locked(assignment_id, reports)

    def _record_status_reports_locked(
        self, assignment_id: str, reports: Iterable[StatusReport]
    ) -> None:
        """Keep each report once per channel, status, and day, whoever writes it: the index
        the file keeps refuses a second, and only that conflict is passed over. The row's
        reported status then follows the latest report, by the day reported and the order
        kept, so what the pages and the planner read of the row never lags the reports."""
        self._connection.executemany(
            """
            INSERT INTO status_reports VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (assignment_id, channel, status, reported_on) DO NOTHING
            """,
            [
                (
                    assignment_id,
                    report.status,
                    report.channel.value,
                    report.reported_on.isoformat(),
                    report.dated_by,
                    report.observed_at.isoformat(),
                    report.source_date_text,
                )
                for report in reports
            ],
        )
        self._connection.execute(
            """
            UPDATE assignments SET reported_submission_status = (
                SELECT status FROM status_reports
                WHERE status_reports.assignment_id = assignments.assignment_id
                ORDER BY reported_on DESC, rowid DESC LIMIT 1
            )
            WHERE assignment_id = ? AND EXISTS (
                SELECT 1 FROM status_reports WHERE status_reports.assignment_id = ?
            )
            """,
            (assignment_id, assignment_id),
        )

    def status_reports(self, assignment_id: str) -> list[StatusReport]:
        """Everything the school reported about one assignment, in the order kept."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT status, channel, reported_on, dated_by, observed_at, source_date_text
                FROM status_reports
                WHERE assignment_id = ?
                ORDER BY rowid
                """,
                (assignment_id,),
            ).fetchall()
        return [report_from(row) for row in rows]

    def status_reports_by_assignment(self) -> dict[str, list[StatusReport]]:
        """Every report the school has made, by assignment, by the day reported and then the
        order kept, in one read. The last report of a channel in that order is what the
        channel says now; the rest is the school's history, kept apart from hers."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT assignment_id, status, channel, reported_on, dated_by, observed_at,
                       source_date_text
                FROM status_reports
                ORDER BY reported_on, rowid
                """
            ).fetchall()
        reports: dict[str, list[StatusReport]] = {}
        for row in rows:
            reports.setdefault(str(row[0]), []).append(report_from(row[1:]))
        return reports

    def student_report_heads(self) -> dict[str, StudentReport]:
        """The last event under each assignment, by stored order: what stands now."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT report_id, assignment_id, operation, status, note, reported_at,
                    reported_on, previous_report_id, undoes_report_id
                FROM student_reports AS latest
                WHERE rowid = (
                    SELECT MAX(rowid) FROM student_reports
                    WHERE assignment_id = latest.assignment_id
                )
                """
            ).fetchall()
        return {str(row[1]): student_report_from(row) for row in rows}

    def student_reports(self, assignment_id: str) -> list[StudentReport]:
        """Everything she said about one assignment, in the order accepted."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT report_id, assignment_id, operation, status, note, reported_at,
                    reported_on, previous_report_id, undoes_report_id
                FROM student_reports
                WHERE assignment_id = ?
                ORDER BY rowid
                """,
                (assignment_id,),
            ).fetchall()
        return [student_report_from(row) for row in rows]

    def student_report_chains(
        self, assignment_ids: Iterable[str] | None = None
    ) -> dict[str, list[StudentReport]]:
        """Every event under each assignment named, in stored order, in one read.

        With no names given, the whole table. The names are bound as one
        value, whatever their number.
        """
        wanted = None if assignment_ids is None else set(assignment_ids)
        if wanted is not None and not wanted:
            return {}
        with self._lock:
            if wanted is None:
                rows = self._connection.execute(EVERY_STUDENT_REPORT).fetchall()
            else:
                rows = self._connection.execute(
                    STUDENT_REPORTS_NAMED, (json.dumps(sorted(wanted)),)
                ).fetchall()
        chains: dict[str, list[StudentReport]] = {}
        for row in rows:
            if wanted is None or str(row[1]) in wanted:
                chains.setdefault(str(row[1]), []).append(student_report_from(row))
        return chains

    def family_check_chains(
        self, assignment_ids: Iterable[str] | None = None
    ) -> dict[str, list[FamilyCheck]]:
        """Every check event under each assignment named, in stored order, in one read.

        Read as her chains are: with no names given, the whole table, and
        the names bound as one value.
        """
        wanted = None if assignment_ids is None else set(assignment_ids)
        if wanted is not None and not wanted:
            return {}
        with self._lock:
            if wanted is None:
                rows = self._connection.execute(EVERY_FAMILY_CHECK).fetchall()
            else:
                rows = self._connection.execute(
                    FAMILY_CHECKS_NAMED, (json.dumps(sorted(wanted)),)
                ).fetchall()
        chains: dict[str, list[FamilyCheck]] = {}
        for row in rows:
            if wanted is None or str(row[1]) in wanted:
                chains.setdefault(str(row[1]), []).append(family_check_from(row))
        return chains

    def family_checks(self, assignment_id: str) -> list[FamilyCheck]:
        """Every check event under one assignment, in the order accepted."""
        return self.family_check_chains([assignment_id]).get(assignment_id, [])

    def report_status(
        self,
        assignment_id: str,
        status: StudentStatus,
        note: str | None,
        *,
        expected_head: str | None,
        now: datetime,
        today: date,
    ) -> Saved | AlreadySaved | Conflict:
        """Keep what she says about an assignment, once, as of now.

        Under the store's lock and one transaction, in this order. An update
        the form names must be one of this assignment's events, or the form
        proves nothing and is refused, ``UnknownReport``, whatever it says.
        Then what she sent is compared with what stands, and the same status
        and note is already saved, whatever page of hers it came from.
        Otherwise the head her page showed must be the head now, or the
        chain moved on since and nothing is written. Otherwise the report is
        appended after the head. The writer is reserved before the read, in
        the one helper that also scopes the transaction, so another
        connection cannot slip a write between the comparison and the
        append. A write the file refuses, or a chain that fails its checks,
        is rolled back whole and raised as ``CouldNotSave``. The note
        is normalized here, whatever the caller did with it, so what is
        compared is what is kept; one past the limit is ``NoteTooLong``, with
        nothing read or written.
        """
        note = normalize_note(note)
        if note is not None and len(note) > NOTE_MAX_LENGTH:
            raise NoteTooLong(len(note))
        try:
            with self._lock, self._writing():
                self._require_assignment_locked(assignment_id)
                if expected_head is not None:
                    self._require_report_locked(assignment_id, expected_head)
                head = self._head_locked(assignment_id)
                standing = (head.status, head.note) if head is not None else (None, None)
                if standing == (status, note):
                    assert head is not None  # noqa: S101  (a report never stands as none)
                    return AlreadySaved(head)
                if (None if head is None else head.report_id) != expected_head:
                    return Conflict(head)
                report = StudentReport(
                    report_id=new_report_id(),
                    assignment_id=assignment_id,
                    operation=REPORT,
                    status=status,
                    note=note,
                    reported_at=now,
                    reported_on=today,
                    previous_report_id=None if head is None else head.report_id,
                )
                self._append_student_report_locked(report)
                return Saved(report)
        except (sqlite3.Error, RuntimeError, ValueError) as error:
            raise CouldNotSave(assignment_id, error) from error

    def undo_report(
        self, assignment_id: str, report_id: str, *, now: datetime, today: date
    ) -> Undone | Conflict:
        """Take back her current report, restoring what stood before it.

        The update the button names must be one of this assignment's events,
        or it is refused, ``UnknownReport``. Only the head can be undone, and
        only when it is a report: a button that names any other event of
        hers finds the chain moved on, and there is no exception for a
        repeat. What is restored is read from the chain, never from the
        page. A refused write is rolled back whole and raised as
        ``CouldNotSave``.
        """
        try:
            with self._lock, self._writing():
                self._require_assignment_locked(assignment_id)
                self._require_report_locked(assignment_id, report_id)
                head = self._head_locked(assignment_id)
                if head is None or head.report_id != report_id or head.operation != REPORT:
                    return Conflict(head)
                restored_status: StudentStatus | None = None
                restored_note: str | None = None
                if head.previous_report_id is not None:
                    before = self._report_locked(head.previous_report_id)
                    if before is not None:
                        restored_status, restored_note = before.status, before.note
                undo = StudentReport(
                    report_id=new_report_id(),
                    assignment_id=assignment_id,
                    operation=UNDO,
                    status=restored_status,
                    note=restored_note,
                    reported_at=now,
                    reported_on=today,
                    previous_report_id=head.report_id,
                    undoes_report_id=head.report_id,
                )
                self._append_student_report_locked(undo)
                return Undone(undo)
        except (sqlite3.Error, RuntimeError, ValueError) as error:
            raise CouldNotSave(assignment_id, error) from error

    def _require_report_locked(self, assignment_id: str, report_id: str) -> None:
        """Refuse a name that is not one of this assignment's events."""
        named = self._report_locked(report_id) if report_id else None
        if named is None or named.assignment_id != assignment_id:
            raise UnknownReport(report_id)

    @contextmanager
    def _writing(self) -> Iterator[None]:
        """One transaction that reserves the writer first, for a write that depends on a read.

        The writer is reserved with ``BEGIN IMMEDIATE`` before anything is
        read, so no other connection can write between the read and the
        write that follows from it; the transaction is committed when the
        block ends and rolled back when it raises. The commit is inside the
        same scope: a commit the file refuses, another connection holding a
        read open through it, leaves SQLite's transaction open with the
        event in it, and the next write to commit would carry that event
        along, so a refused commit is rolled back like any other failure
        and nothing said to be unsaved can land afterward. The reservation
        and the scope are one helper, so no caller can open the scope first
        and reserve second, which would reserve nothing. Inside a
        transaction already open, a first start's, the block joins it and
        leaves its end to the opener.
        """
        if self._connection.in_transaction:
            yield
            return
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def _require_assignment_locked(self, assignment_id: str) -> None:
        row = self._connection.execute(
            "SELECT 1 FROM assignments WHERE assignment_id = ?", (assignment_id,)
        ).fetchone()
        if row is None:
            msg = f"no assignment {assignment_id!r} is on record"
            raise UnknownAssignment(msg)

    def _head_locked(self, assignment_id: str) -> StudentReport | None:
        row = self._connection.execute(
            """
            SELECT report_id, assignment_id, operation, status, note, reported_at,
                reported_on, previous_report_id, undoes_report_id
            FROM student_reports
            WHERE assignment_id = ? ORDER BY rowid DESC LIMIT 1
            """,
            (assignment_id,),
        ).fetchone()
        return None if row is None else student_report_from(row)

    def _report_locked(self, report_id: str) -> StudentReport | None:
        row = self._connection.execute(
            """
            SELECT report_id, assignment_id, operation, status, note, reported_at,
                reported_on, previous_report_id, undoes_report_id
            FROM student_reports
            WHERE report_id = ?
            """,
            (report_id,),
        ).fetchone()
        return None if row is None else student_report_from(row)

    def _append_student_report_locked(self, report: StudentReport) -> None:
        """Append one event after the head, checking the chain as it is written.

        The event's predecessor must be the head now and belong to the same
        assignment. An undo must take back that head, the head must be a
        report, and the status and note the undo carries must be what stood
        before that report, read from the chain; a seeded first report has no
        predecessor, and an undo is never first. The seed goes through the
        same checks as her own saves. A chain that would fork is refused, so a write
        that gets this far and still fails leaves nothing, the transaction
        rolling the event back with it.
        """
        self._require_assignment_locked(report.assignment_id)
        head = self._head_locked(report.assignment_id)
        head_id = None if head is None else head.report_id
        if report.previous_report_id != head_id:
            msg = (
                f"report {report.report_id!r} does not follow the head of {report.assignment_id!r}"
            )
            raise ValueError(msg)
        if report.operation == UNDO:
            if head is None or head.operation != REPORT or report.undoes_report_id != head_id:
                msg = (
                    f"undo {report.report_id!r} does not take back a report at the head of "
                    f"{report.assignment_id!r}"
                )
                raise ValueError(msg)
            before = (
                None
                if head.previous_report_id is None
                else self._report_locked(head.previous_report_id)
            )
            stood = (None, None) if before is None else (before.status, before.note)
            if (report.status, report.note) != stood:
                msg = (
                    f"undo {report.report_id!r} does not restore what stood before "
                    f"{head.report_id!r}"
                )
                raise ValueError(msg)
        self._connection.execute(
            """
            INSERT INTO student_reports (
                report_id, assignment_id, operation, status, note, reported_at,
                reported_on, previous_report_id, undoes_report_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.report_id,
                report.assignment_id,
                report.operation,
                report.status,
                report.note,
                report.reported_at.isoformat(),
                report.reported_on.isoformat(),
                report.previous_report_id,
                report.undoes_report_id,
            ),
        )
        self._confirm_head_locked(report)

    def _confirm_head_locked(self, report: StudentReport) -> None:
        """Read the head back: the event just written, or the write is refused whole."""
        head = self._head_locked(report.assignment_id)
        if head is None or head.report_id != report.report_id:
            msg = f"the chain of {report.assignment_id!r} forked while writing {report.report_id!r}"
            raise RuntimeError(msg)

    def hand_in_chains(
        self, assignment_ids: Iterable[str] | None = None
    ) -> dict[str, list[HandInEvent]]:
        """Every hand-in event under each assignment named, in stored order, in one read.

        Read as her work reports are: with no names given, the whole table,
        and the names bound as one value. An assignment she has said nothing
        about has no entry.
        """
        wanted = None if assignment_ids is None else set(assignment_ids)
        if wanted is not None and not wanted:
            return {}
        with self._lock:
            if wanted is None:
                rows = self._connection.execute(EVERY_HAND_IN_EVENT).fetchall()
            else:
                rows = self._connection.execute(
                    HAND_IN_EVENTS_NAMED, (json.dumps(sorted(wanted)),)
                ).fetchall()
        chains: dict[str, list[HandInEvent]] = {}
        for row in rows:
            chains.setdefault(str(row[1]), []).append(hand_in_event_from(row))
        return chains

    def hand_in_readings(self, assignment_ids: Iterable[str]) -> HandInReadings:
        """What stands about turning each assignment named in, for a page: the readings that
        can be made, and the assignments whose record cannot be read.

        One read for every assignment named, as ``hand_in_chains`` makes. The
        rows are then taken one assignment at a time: a row that cannot be
        decoded, a state that is none of the four, a day that is no day, words
        past the limit, makes that whole assignment unreadable, and so does a
        chain that does not hold together. Such an assignment is named apart
        and never read from what is left of it, the rest read as usual, and
        one with no rows has a reading that says nothing. Nothing is
        repaired, and a failed read of the file is raised, never turned into
        an empty answer. What is logged names the assignment and the kind of
        fault, never her words. The writers do not come through here: they
        read one chain strictly, inside their own transaction.
        """
        wanted = list(dict.fromkeys(assignment_ids))
        if not wanted:
            return HandInReadings({}, frozenset())
        with self._lock:
            rows = self._connection.execute(
                HAND_IN_EVENTS_NAMED, (json.dumps(sorted(wanted)),)
            ).fetchall()
        grouped: dict[str, list[tuple[object, ...]]] = {name: [] for name in wanted}
        for row in rows:
            grouped.setdefault(str(row[1]), []).append(row)
        readable: dict[str, HandInProjection] = {}
        unreadable: set[str] = set()
        for assignment_id in wanted:
            try:
                chain = [hand_in_event_from(row) for row in grouped[assignment_id]]
                readable[assignment_id] = project(assignment_id, chain)
            except (ValueError, TypeError) as fault:
                logger.warning(
                    "the hand-in record of %s cannot be read (%s)",
                    assignment_id,
                    type(fault).__name__,
                )
                unreadable.add(assignment_id)
        return HandInReadings(readable, frozenset(unreadable))

    def record_hand_in(
        self,
        assignment_id: str,
        state: HandInState,
        next_action: str | None,
        note: str | None,
        *,
        expected_head: str | None,
        now: datetime,
        today: date,
    ) -> HandInSaved | HandInAlreadySaved | HandInConflict:
        """Keep what she says about turning an assignment in, once, as of now.

        The order is her work report's. The words are made what is kept
        first, a next action only with still to turn in and dropped with any
        other state, and words the record will not keep are ``TextRefused``
        with nothing read or written. Then, under the store's lock and one
        transaction that reserves the writer before it reads: the assignment
        must be on record; an event the form names must be one of this
        assignment's, or ``UnknownHandIn``; the assignment's whole chain is
        read and must hold together, since a head that looks whole proves
        nothing about what is behind it; the same state, action, and note as
        what stands is already saved, whatever head the page held, a response
        lost on the way included; otherwise the head the page showed must be
        the head now, a blank one meaning no event at all, or nothing is
        written and the head as read here is handed back; otherwise the
        report is appended. Her work reports are not read and not touched.

        A chain that does not hold is ``BrokenChain``, raised as
        ``CouldNotSave`` like any other refused write: nothing is called
        already saved, stale, or new on it, nothing is written, and nothing
        in it is repaired.

        A save says nothing about a calendar cue, so a cue is no part of the
        comparison and a save never removes one by saying nothing: an edit
        that stays in still to turn in carries the standing cue along, and
        any other state keeps none, as that state never does.
        """
        next_action = (
            single_line(next_action, NEXT_ACTION_MAX_LENGTH) if state == NEEDS_HAND_IN else None
        )
        note = multiline(note, HAND_IN_NOTE_MAX_LENGTH)
        try:
            with self._lock, self._writing():
                self._require_assignment_locked(assignment_id)
                if expected_head is not None:
                    self._require_hand_in_locked(assignment_id, expected_head)
                reading = self._hand_in_reading_locked(assignment_id)
                head = reading.head
                standing = (reading.state, reading.next_action, reading.note)
                if head is not None and standing == (state, next_action, note):
                    return HandInAlreadySaved(head)
                if reading.head_id != expected_head:
                    return HandInConflict(head)
                stays = reading.source is not None and reading.state == state == NEEDS_HAND_IN
                report = HandInEvent(
                    event_id=new_hand_in_id(),
                    assignment_id=assignment_id,
                    operation=REPORT,
                    state=state,
                    next_action=next_action,
                    note=note,
                    cue_at_utc=reading.words[3] if stays else None,
                    reported_at=now,
                    reported_on=today,
                    previous_event_id=reading.head_id,
                )
                return HandInSaved(self._append_hand_in_locked(report, reading))
        except (sqlite3.Error, RuntimeError, ValueError) as error:
            raise CouldNotSave(assignment_id, error) from error

    def undo_hand_in(
        self, assignment_id: str, event_id: str, *, now: datetime, today: date
    ) -> HandInUndone | HandInConflict:
        """Take back her current hand-in report, restoring what stood before it.

        The event the button names must be one of this assignment's, or
        ``UnknownHandIn``, and the whole chain must hold together before
        anything is decided on it, or ``CouldNotSave``. Only the head can be
        undone, and only when it is a report; any other event of hers finds
        the chain moved on, and a repeat is refused like any other, with the
        head as this transaction read it, which is how a page tells an event
        already taken back from a change. What is restored is what the chain
        says stood before the head, worked out from the events before it,
        never looked up by a link alone and never taken from the page.
        """
        try:
            with self._lock, self._writing():
                self._require_assignment_locked(assignment_id)
                self._require_hand_in_locked(assignment_id, event_id)
                reading = self._hand_in_reading_locked(assignment_id)
                head = reading.head
                if head is None or head.event_id != event_id or head.operation != REPORT:
                    return HandInConflict(head)
                restored_state, restored_action, restored_note, restored_cue = (
                    reading.words_before_head
                )
                undo = HandInEvent(
                    event_id=new_hand_in_id(),
                    assignment_id=assignment_id,
                    operation=UNDO,
                    state=restored_state,
                    next_action=restored_action,
                    note=restored_note,
                    cue_at_utc=restored_cue,
                    reported_at=now,
                    reported_on=today,
                    previous_event_id=head.event_id,
                    undone_event_id=head.event_id,
                )
                return HandInUndone(self._append_hand_in_locked(undo, reading))
        except (sqlite3.Error, RuntimeError, ValueError) as error:
            raise CouldNotSave(assignment_id, error) from error

    def _require_hand_in_locked(self, assignment_id: str, event_id: str) -> None:
        """Refuse a name that is not one of this assignment's hand-in events."""
        named = self._hand_in_event_locked(event_id) if event_id else None
        if named is None or named.assignment_id != assignment_id:
            raise UnknownHandIn(event_id)

    def _hand_in_reading_locked(self, assignment_id: str) -> HandInProjection:
        """One assignment's whole chain, read through this connection and held to the
        chain's rules, or ``BrokenChain``. For use inside the transaction that writes."""
        rows = self._connection.execute(HAND_IN_CHAIN, (assignment_id,)).fetchall()
        return project(assignment_id, [hand_in_event_from(row) for row in rows])

    def _hand_in_head_locked(self, assignment_id: str) -> HandInEvent | None:
        row = self._connection.execute(HAND_IN_HEAD, (assignment_id,)).fetchone()
        return None if row is None else hand_in_event_from(row)

    def _hand_in_event_locked(self, event_id: str) -> HandInEvent | None:
        row = self._connection.execute(HAND_IN_EVENT_NAMED, (event_id,)).fetchone()
        return None if row is None else hand_in_event_from(row)

    def _append_hand_in_locked(
        self, event: HandInEvent, reading: HandInProjection | None = None
    ) -> HandInEvent:
        """Append one hand-in event, held to the chain's rules together with its history.

        The assignment must be on record. The history the event joins is
        ``reading`` when the caller has read it in this transaction, and is
        read here otherwise; the event is then read as the next of that
        chain, by the same pass every reader uses, so it follows the head,
        an undo takes back the report just before it and carries what stood
        before that, and the history behind it holds too. Whatever calls
        this, a save, an undo, or a seed, is held to the same, and a write
        that fails here leaves nothing, the transaction rolling it back. The
        event comes back with the place the file gave it.
        """
        self._require_assignment_locked(event.assignment_id)
        joined = reading or self._hand_in_reading_locked(event.assignment_id)
        project(event.assignment_id, [*(row.event for row in joined.history), event])
        self._connection.execute(
            """
            INSERT INTO hand_in_events (
                event_id, assignment_id, operation, state, next_action, note, cue_at_utc,
                reported_at_utc, reported_on, previous_event_id, undone_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.assignment_id,
                event.operation,
                event.state,
                event.next_action,
                event.note,
                None if event.cue_at_utc is None else event.cue_at_utc.isoformat(),
                event.reported_at.isoformat(),
                event.reported_on.isoformat(),
                event.previous_event_id,
                event.undone_event_id,
            ),
        )
        return self._confirm_hand_in_head_locked(event)

    def _confirm_hand_in_head_locked(self, event: HandInEvent) -> HandInEvent:
        """Read the head back: the event just written, or the write is refused whole."""
        head = self._hand_in_head_locked(event.assignment_id)
        if head is None or head.event_id != event.event_id:
            msg = (
                f"the hand-in chain of {event.assignment_id!r} forked while writing "
                f"{event.event_id!r}"
            )
            raise RuntimeError(msg)
        return head

    def mark_checked(
        self,
        assignment_id: str,
        basis: str,
        note: str | None,
        *,
        expected_check: str | None,
        basis_now: Callable[[], str | None],
        now: datetime,
        today: date,
    ) -> Checked | AlreadyChecked | CheckConflict:
        """Record that a parent checked the discrepancy on an assignment with her, once, as of now.

        Under the store's lock and one transaction that reserves the writer
        before it reads, in this order. A check the form names must be one
        of this assignment's events, or the form proves nothing and is
        refused, ``UnknownCheck``. Then the basis the page was made against
        is compared with ``basis_now``, worked out inside the transaction
        from her events and the school's reports as they stand, and with
        the last check event. The same check standing already, that basis,
        the basis now, and that note, is already made, and nothing is
        written. A check standing with any other note is another parent's:
        a conflict, so the words typed are never dropped for a check that
        says something else. Otherwise the basis must be the one now and
        the check the page showed must be the head now, or what the page
        rested on moved since and nothing is written. Otherwise the check
        is appended. A write the file refuses is rolled back whole and
        raised as ``CouldNotSave``. The note is normalized here, so a
        repeat is a repeat whatever its line endings, and one past the
        limit is ``NoteTooLong``, with nothing read or written. Nothing
        here writes her events or the school's reports.
        """
        note = normalize_note(note)
        if note is not None and len(note) > NOTE_MAX_LENGTH:
            raise NoteTooLong(len(note))
        try:
            with self._lock, self._writing():
                self._require_assignment_locked(assignment_id)
                if expected_check is not None:
                    self._require_check_locked(assignment_id, expected_check)
                head = self._check_head_locked(assignment_id)
                current = basis_now()
                stands = (
                    head
                    if head is not None and head.operation == CHECKED and head.basis == current
                    else None
                )
                if stands is not None and basis == current and stands.note == note:
                    return AlreadyChecked(stands)
                head_id = None if head is None else head.check_id
                if stands is not None or current != basis or head_id != expected_check:
                    return CheckConflict(head)
                check = FamilyCheck(
                    check_id=new_check_id(),
                    assignment_id=assignment_id,
                    operation=CHECKED,
                    basis=basis,
                    note=note,
                    checked_at=now,
                    checked_on=today,
                    previous_check_id=head_id,
                )
                self._append_family_check_locked(check)
                return Checked(check)
        except (sqlite3.Error, RuntimeError, ValueError) as error:
            raise CouldNotSave(assignment_id, error, what="the check") from error

    def check_again(
        self,
        assignment_id: str,
        check_id: str,
        *,
        expected_basis: str | None,
        basis_now: Callable[[], str | None],
        now: datetime,
        today: date,
    ) -> Reopened | CheckConflict:
        """Reopen the family's check on an assignment, and nothing else.

        The check the button names must be one of this assignment's events,
        or it is refused, ``UnknownCheck``. Only the head can be reopened,
        and only when it marks the row checked: a button that names any
        other event finds the record moved on. The facts are held to the
        page as well: ``expected_basis`` is the basis the page showed, none
        when it showed nothing to check, and it must be ``basis_now``,
        worked out inside the transaction, or her update or the school's
        report moved since the page was made and nothing is written. What
        is compared is the page's basis, never the basis of the check
        reopened, so a check made against facts that have moved can still
        be reopened from a page that shows them as they are. The reopening
        carries the basis of the check it reopens, and that check stays in
        the record. A refused write is rolled back whole and raised as
        ``CouldNotSave``.
        """
        try:
            with self._lock, self._writing():
                self._require_assignment_locked(assignment_id)
                self._require_check_locked(assignment_id, check_id)
                head = self._check_head_locked(assignment_id)
                current = basis_now()
                if (
                    head is None
                    or head.check_id != check_id
                    or head.operation != CHECKED
                    or current != expected_basis
                ):
                    return CheckConflict(head)
                reopened = FamilyCheck(
                    check_id=new_check_id(),
                    assignment_id=assignment_id,
                    operation=REOPENED,
                    basis=head.basis,
                    checked_at=now,
                    checked_on=today,
                    previous_check_id=head.check_id,
                )
                self._append_family_check_locked(reopened)
                return Reopened(reopened)
        except (sqlite3.Error, RuntimeError, ValueError) as error:
            raise CouldNotSave(assignment_id, error, what="the check") from error

    def _require_check_locked(self, assignment_id: str, check_id: str) -> None:
        """Refuse a name that is not one of this assignment's check events."""
        named = self._check_locked(check_id) if check_id else None
        if named is None or named.assignment_id != assignment_id:
            raise UnknownCheck(check_id)

    def _check_head_locked(self, assignment_id: str) -> FamilyCheck | None:
        row = self._connection.execute(
            """
            SELECT check_id, assignment_id, operation, basis, note, checked_at, checked_on,
                previous_check_id
            FROM family_checks
            WHERE assignment_id = ? ORDER BY rowid DESC LIMIT 1
            """,
            (assignment_id,),
        ).fetchone()
        return None if row is None else family_check_from(row)

    def _check_locked(self, check_id: str) -> FamilyCheck | None:
        row = self._connection.execute(
            """
            SELECT check_id, assignment_id, operation, basis, note, checked_at, checked_on,
                previous_check_id
            FROM family_checks
            WHERE check_id = ?
            """,
            (check_id,),
        ).fetchone()
        return None if row is None else family_check_from(row)

    def _append_family_check_locked(self, check: FamilyCheck) -> None:
        """Append one check event after the head, checking the chain as it is written.

        The event's predecessor must be the head now. A reopening must
        follow a check that marks the row checked and carry its basis. A
        chain that would fork is refused, so a write that gets this far and
        still fails leaves nothing, the transaction rolling the event back
        with it.
        """
        self._require_assignment_locked(check.assignment_id)
        head = self._check_head_locked(check.assignment_id)
        head_id = None if head is None else head.check_id
        if check.previous_check_id != head_id:
            msg = (
                f"check {check.check_id!r} does not follow the last check on "
                f"{check.assignment_id!r}"
            )
            raise ValueError(msg)
        if check.operation == REOPENED and (
            head is None or head.operation != CHECKED or head.basis != check.basis
        ):
            msg = (
                f"reopening {check.check_id!r} does not reopen a check standing on "
                f"{check.assignment_id!r}"
            )
            raise ValueError(msg)
        self._connection.execute(
            """
            INSERT INTO family_checks (
                check_id, assignment_id, operation, basis, note, checked_at, checked_on,
                previous_check_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                check.check_id,
                check.assignment_id,
                check.operation,
                check.basis,
                check.note,
                check.checked_at.isoformat(),
                check.checked_on.isoformat(),
                check.previous_check_id,
            ),
        )
        written = self._check_head_locked(check.assignment_id)
        if written is None or written.check_id != check.check_id:
            msg = f"the checks on {check.assignment_id!r} forked while writing {check.check_id!r}"
            raise RuntimeError(msg)

    def latest_status_reports(self) -> dict[str, StatusReport]:
        """The latest report per assignment, by the day reported and then the order kept."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT assignment_id, status, channel, reported_on, dated_by, observed_at,
                       source_date_text
                FROM status_reports
                ORDER BY reported_on, rowid
                """
            ).fetchall()
        return {str(row[0]): report_from(row[1:]) for row in rows}

    def _upsert_assignments_locked(self, assignments: Iterable[Assignment]) -> None:
        """Write each row, over the row with its id when there is one. A note or origins
        the new row lacks leave the saved ones standing: a row written without them, a
        set read from a file among them, never erases what a parent or a paste put there."""
        for assignment in assignments:
            self._connection.execute(
                """
                INSERT INTO assignments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(assignment_id) DO UPDATE SET
                    course=excluded.course,
                    title=excluded.title,
                    due_date=excluded.due_date,
                    dependencies=excluded.dependencies,
                    reported_submission_status=excluded.reported_submission_status,
                    assigned_on=excluded.assigned_on,
                    kind=excluded.kind,
                    note=COALESCE(excluded.note, assignments.note),
                    origins=COALESCE(excluded.origins, assignments.origins)
                """,
                (
                    assignment.assignment_id,
                    assignment.course,
                    assignment.title,
                    None if assignment.due_date is None else assignment.due_date.isoformat(),
                    ",".join(assignment.dependencies),
                    assignment.reported_submission_status,
                    None if assignment.assigned_on is None else assignment.assigned_on.isoformat(),
                    assignment.kind.value,
                    assignment.note,
                    json.dumps(
                        {field: channel.value for field, channel in assignment.origins.items()}
                    )
                    if assignment.origins
                    else None,
                ),
            )

    def is_empty(self) -> bool:
        """Whether nothing is on record yet: no assignment, and no claim about one."""
        with self._lock:
            row = self._connection.execute(
                "SELECT (SELECT COUNT(*) FROM assignments) + (SELECT COUNT(*) FROM date_claims)"
            ).fetchone()
        return int(row[0]) == 0

    def record_claims(self, assignment_id: str, records: Iterable[SourceRecord]) -> None:
        """Keep each channel's claim about one assignment's due date, as made, all or none.

        Every claim is kept, the same value said again at another time
        included: the table is the history of what each channel said and
        when. Not making a claim twice is the import's job, before it writes.
        """
        with self._lock, self._connection:
            self._record_claims_locked(assignment_id, records)

    def _record_claims_locked(self, assignment_id: str, records: Iterable[SourceRecord]) -> None:
        self._connection.executemany(
            "INSERT INTO date_claims VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    assignment_id,
                    record.channel.value,
                    record.asserted_value,
                    record.observed_at.isoformat(),
                    record.confidence,
                    record.seen_in,
                )
                for record in records
            ],
        )

    def deadline_records(self, assignment_id: str) -> list[SourceRecord]:
        """Every channel's claim about one assignment's due date, in the order made.

        An empty list is a valid answer and means nothing corroborates the
        date; it is not an error.
        """
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT channel, asserted_value, observed_at, confidence, seen_in
                FROM date_claims
                WHERE assignment_id = ?
                ORDER BY rowid
                """,
                (assignment_id,),
            ).fetchall()
        return [source_record_from(row) for row in rows]

    def deadline_records_by_assignment(
        self, assignment_ids: Iterable[str] | None = None
    ) -> dict[str, list[SourceRecord]]:
        """Every channel's claims under each assignment named, in the order made, in one read.

        Read as her chains are: with no names given, the whole table, and
        the names bound as one value. An assignment nothing was claimed about
        has no entry, which says what an empty list says from the single read.
        """
        wanted = None if assignment_ids is None else set(assignment_ids)
        if wanted is not None and not wanted:
            return {}
        with self._lock:
            if wanted is None:
                rows = self._connection.execute(EVERY_DATE_CLAIM).fetchall()
            else:
                rows = self._connection.execute(
                    DATE_CLAIMS_NAMED, (json.dumps(sorted(wanted)),)
                ).fetchall()
        claims: dict[str, list[SourceRecord]] = {}
        for row in rows:
            claims.setdefault(str(row[0]), []).append(source_record_from(row[1:]))
        return claims

    def due_between(self, start: date, end: date) -> list[Assignment]:
        """Return assignments due in ``[start, end]``, ordered by date then course.

        An assignment with no due date is not between any two dates and is not
        returned here; ``undated`` is the other half of the week.
        """
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT assignment_id, course, title, due_date, dependencies,
                       reported_submission_status, assigned_on, kind, note, origins
                FROM assignments
                WHERE due_date BETWEEN ? AND ?
                ORDER BY due_date, course, title
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [assignment_from(row) for row in rows]

    def week_from(self, start: date) -> list[Assignment]:
        """The week by the record alone: dated work in the window, then undated work.

        Served by ``lookup``. The page and the plan graph read the week through
        ``read_week`` in ``blossom/noticing.py`` instead, which selects the same
        window after the sources have been read, so a date a source gives can
        put an item in the week that the record leaves out.
        """
        return [*self.due_between(start, start + DUE_THIS_WEEK_SPAN), *self.undated()]

    def all_assignments(self) -> list[Assignment]:
        """Every assignment on record, dated work by date then course, undated work last."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT assignment_id, course, title, due_date, dependencies,
                       reported_submission_status, assigned_on, kind, note, origins
                FROM assignments
                ORDER BY due_date IS NULL, due_date, course, title
                """
            ).fetchall()
        return [assignment_from(row) for row in rows]

    def one_assignment(self, assignment_id: str) -> Assignment | None:
        """The assignment with this id, or ``None`` when none is on record. Read by id and
        nothing else, so a page about one assignment never reads a week to find it."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT assignment_id, course, title, due_date, dependencies,
                       reported_submission_status, assigned_on, kind, note, origins
                FROM assignments
                WHERE assignment_id = ?
                """,
                (assignment_id,),
            ).fetchone()
        return None if row is None else assignment_from(row)

    def undated(self) -> list[Assignment]:
        """Return every assignment with no due date on record, by course then title."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT assignment_id, course, title, due_date, dependencies,
                       reported_submission_status, assigned_on, kind, note, origins
                FROM assignments
                WHERE due_date IS NULL
                ORDER BY course, title
                """
            ).fetchall()
        return [assignment_from(row) for row in rows]

    def lookup(self, key: str) -> RetrievalResult | None:
        """Resolve a keyed structured query.

        "This week" means today plus the next six days, computed from the
        injected clock so tests can pin the date. The definition is a placeholder
        that belongs in a calendar policy once there is one.

        An assignment with no due date cannot be placed in any week, so it is
        in every week until it has one, after the dated work. Nothing is
        filtered, and an undated item is a problem to resolve, not to hide.
        """
        if key != DUE_THIS_WEEK_KEY:
            return None
        today = self._clock.today()
        return RetrievalResult(
            store_name=self.name,
            record_id=key,
            source_channel="fixture",
            asserted_at=self._clock.now(),
            payload={
                "assignments": [item.model_dump(mode="json") for item in self.week_from(today)]
            },
        )


def assignment_from(row: tuple[object, ...]) -> Assignment:
    """Build an assignment from a row in the order the two queries select."""
    return Assignment(
        assignment_id=str(row[0]),
        course=str(row[1]),
        title=str(row[2]),
        due_date=None if row[3] is None else date.fromisoformat(str(row[3])),
        dependencies=str(row[4]).split(",") if row[4] else [],
        reported_submission_status=str(row[5]),
        assigned_on=None if row[6] is None else date.fromisoformat(str(row[6])),
        kind=AssignmentKind(str(row[7])),
        note=None if row[8] is None else str(row[8]),
        origins={}
        if row[9] is None
        else {
            str(field): SourceChannel(str(value))
            for field, value in json.loads(str(row[9])).items()
        },
    )


def new_hand_in_id() -> str:
    """A stable id for one hand-in event, drawn once and never reused."""
    return f"hand-in-{uuid.uuid4().hex[:12]}"


def hand_in_event_from(row: tuple[object, ...]) -> HandInEvent:
    """Build one hand-in event from a row in the order the hand-in statements select."""
    return HandInEvent(
        event_id=str(row[0]),
        assignment_id=str(row[1]),
        operation=cast(Literal["report", "undo"], str(row[2])),
        state=cast(HandInState | None, None if row[3] is None else str(row[3])),
        next_action=None if row[4] is None else str(row[4]),
        note=None if row[5] is None else str(row[5]),
        cue_at_utc=None if row[6] is None else datetime.fromisoformat(str(row[6])),
        reported_at=datetime.fromisoformat(str(row[7])),
        reported_on=date.fromisoformat(str(row[8])),
        previous_event_id=None if row[9] is None else str(row[9]),
        undone_event_id=None if row[10] is None else str(row[10]),
        sequence=int(cast(int, row[11])),
    )


def new_report_id() -> str:
    """A stable id for one event, drawn once and never reused."""
    return f"report-{uuid.uuid4().hex[:12]}"


def new_check_id() -> str:
    """A stable id for one check event, drawn once and never reused."""
    return f"check-{uuid.uuid4().hex[:12]}"


def family_check_from(row: tuple[object, ...]) -> FamilyCheck:
    """Build one check event from a row in the columns' order."""
    return FamilyCheck(
        check_id=str(row[0]),
        assignment_id=str(row[1]),
        operation=cast(Literal["checked", "reopened"], str(row[2])),
        basis=str(row[3]),
        note=None if row[4] is None else str(row[4]),
        checked_at=datetime.fromisoformat(str(row[5])),
        checked_on=date.fromisoformat(str(row[6])),
        previous_check_id=None if row[7] is None else str(row[7]),
    )


def source_record_from(row: tuple[object, ...]) -> SourceRecord:
    """Build one claim about a due date from a row in the columns' order."""
    return SourceRecord(
        channel=SourceChannel(str(row[0])),
        asserted_value=str(row[1]),
        observed_at=datetime.fromisoformat(str(row[2])),
        confidence=float(cast(float, row[3])),
        seen_in=None if row[4] is None else str(row[4]),
    )


def student_report_from(row: tuple[object, ...]) -> StudentReport:
    """Build one of her reports from a row in the columns' order."""
    return StudentReport(
        report_id=str(row[0]),
        assignment_id=str(row[1]),
        operation=cast(Literal["report", "undo"], str(row[2])),
        status=cast(StudentStatus | None, None if row[3] is None else str(row[3])),
        note=None if row[4] is None else str(row[4]),
        reported_at=datetime.fromisoformat(str(row[5])),
        reported_on=date.fromisoformat(str(row[6])),
        previous_report_id=None if row[7] is None else str(row[7]),
        undoes_report_id=None if row[8] is None else str(row[8]),
    )


def report_from(row: tuple[object, ...]) -> StatusReport:
    """Build a report from a row in the order the report queries select."""
    return StatusReport(
        status=str(row[0]),
        channel=SourceChannel(str(row[1])),
        reported_on=date.fromisoformat(str(row[2])),
        dated_by=str(row[3]),
        observed_at=datetime.fromisoformat(str(row[4])),
        source_date_text=None if row[5] is None else str(row[5]),
    )
