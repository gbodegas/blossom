"""Store one: structured project state, queried exactly.

Assignments, due dates, dependencies and reported submission status. Small,
structured, and always asked about by exact criteria, which is why this is
SQLite and not a vector index: retrieving a due date by similarity would return
the most similar assignment rather than the correct one.

Retention is the academic year, then archive. Noticing that a project entered
eleven days ago has no progress against it needs that history.

The field is named ``reported_submission_status`` on purpose: a submission flag
confirms a file was uploaded, not that the assignment was finished, that the
right file went up, or that the teacher considers it done. Code reading it must
not treat it as completion.
"""

import sqlite3
import threading
from collections.abc import Iterable
from datetime import date, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from blossom.clock import Clock
from blossom.retrieval import RetrievalResult

DUE_THIS_WEEK_KEY = "due_this_week"
DUE_THIS_WEEK_SPAN = timedelta(days=6)


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


class ProjectStateStore:
    """SQLite-backed project state, opened once and shared across worker threads.

    The connection is created at application startup rather than per request,
    so every statement is serialized behind a lock. ``sqlite3`` refuses a
    connection used from a thread other than the one that created it, and
    FastAPI runs synchronous handlers in a thread pool.
    """

    name = "project_state"
    retention_policy = "Keep structured assignment state for the academic year, then archive."

    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        # Required, not defaulted: a clock needs the household's zone, and this
        # store has no business choosing one.
        self._clock = clock
        # Shared across FastAPI's handler threads; see blossom/dependencies.py.
        self._lock = threading.Lock()
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
                kind TEXT NOT NULL
            )
            """
        )

    def close(self) -> None:
        """Close the underlying connection. Called when the application shuts down."""
        with self._lock:
            self._connection.close()

    def upsert_assignments(self, assignments: Iterable[Assignment]) -> None:
        """Insert or update each assignment, keyed by ``assignment_id``."""
        with self._lock:
            self._upsert_assignments_locked(assignments)

    def _upsert_assignments_locked(self, assignments: Iterable[Assignment]) -> None:
        for assignment in assignments:
            self._connection.execute(
                """
                INSERT INTO assignments VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(assignment_id) DO UPDATE SET
                    course=excluded.course,
                    title=excluded.title,
                    due_date=excluded.due_date,
                    dependencies=excluded.dependencies,
                    reported_submission_status=excluded.reported_submission_status,
                    assigned_on=excluded.assigned_on,
                    kind=excluded.kind
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
                ),
            )
        self._connection.commit()

    def due_between(self, start: date, end: date) -> list[Assignment]:
        """Return assignments due in ``[start, end]``, ordered by date then course.

        An assignment with no due date is not between any two dates and is not
        returned here; ``undated`` is the other half of the week.
        """
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT assignment_id, course, title, due_date, dependencies,
                       reported_submission_status, assigned_on, kind
                FROM assignments
                WHERE due_date BETWEEN ? AND ?
                ORDER BY due_date, course, title
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [assignment_from(row) for row in rows]

    def undated(self) -> list[Assignment]:
        """Return every assignment with no due date on record, by course then title."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT assignment_id, course, title, due_date, dependencies,
                       reported_submission_status, assigned_on, kind
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
        end = today + DUE_THIS_WEEK_SPAN
        return RetrievalResult(
            store_name=self.name,
            record_id=key,
            source_channel="fixture",
            asserted_at=self._clock.now(),
            payload={
                "assignments": [
                    item.model_dump(mode="json")
                    for item in [*self.due_between(today, end), *self.undated()]
                ]
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
    )
