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
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict

from blossom.clock import Clock
from blossom.reconciliation import SourceChannel, SourceRecord
from blossom.retrieval import RetrievalResult
from blossom.stores.paths import refuse_unsafe_path

DUE_THIS_WEEK_KEY = "due_this_week"
DUE_THIS_WEEK_SPAN = timedelta(days=6)

type Seed = tuple[Sequence[Assignment], Mapping[str, Sequence[SourceRecord]]]
"""What a blank file is seeded with: assignments, and the claims about each one's date."""


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
        self._upgrade()

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
                    assignments, claims = seed()
                    store._upsert_assignments_locked(assignments)
                    for assignment_id, records in claims.items():
                        store._record_claims_locked(assignment_id, records)
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
        """Keep what a school channel reported about one assignment, once per day and status."""
        with self._lock, self._connection:
            self._record_status_reports_locked(assignment_id, reports)

    def _record_status_reports_locked(
        self, assignment_id: str, reports: Iterable[StatusReport]
    ) -> None:
        """Keep each report once per channel, status, and day, whoever writes it: the index
        the file keeps refuses a second, and only that conflict is passed over."""
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
        return [
            SourceRecord(
                channel=SourceChannel(str(row[0])),
                asserted_value=str(row[1]),
                observed_at=datetime.fromisoformat(str(row[2])),
                confidence=float(row[3]),
                seen_in=None if row[4] is None else str(row[4]),
            )
            for row in rows
        ]

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
