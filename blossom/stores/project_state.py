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

from pydantic import BaseModel, ConfigDict

from blossom.clock import Clock, SystemClock
from blossom.retrieval import RetrievalResult

DUE_THIS_WEEK_KEY = "due_this_week"
DUE_THIS_WEEK_SPAN = timedelta(days=6)


class Assignment(BaseModel):
    """One assignment as structured state, with dependencies and reported status."""

    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    course: str
    title: str
    due_date: date
    dependencies: list[str]
    reported_submission_status: str


class ProjectStateStore:
    """SQLite-backed project state, opened once and shared across worker threads.

    The connection is created at application startup rather than per request,
    so every statement is serialized behind a lock. ``sqlite3`` refuses a
    connection used from a thread other than the one that created it, and
    FastAPI runs synchronous handlers in a thread pool.
    """

    name = "project_state"
    retention_policy = "Keep structured assignment state for the academic year, then archive."

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self._connection = connection
        self._clock = SystemClock() if clock is None else clock
        # Shared across FastAPI's handler threads; see blossom/dependencies.py.
        self._lock = threading.Lock()
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS assignments (
                assignment_id TEXT PRIMARY KEY,
                course TEXT NOT NULL,
                title TEXT NOT NULL,
                due_date TEXT NOT NULL,
                dependencies TEXT NOT NULL,
                reported_submission_status TEXT NOT NULL
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
                INSERT INTO assignments VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(assignment_id) DO UPDATE SET
                    course=excluded.course,
                    title=excluded.title,
                    due_date=excluded.due_date,
                    dependencies=excluded.dependencies,
                    reported_submission_status=excluded.reported_submission_status
                """,
                (
                    assignment.assignment_id,
                    assignment.course,
                    assignment.title,
                    assignment.due_date.isoformat(),
                    ",".join(assignment.dependencies),
                    assignment.reported_submission_status,
                ),
            )
        self._connection.commit()

    def due_between(self, start: date, end: date) -> list[Assignment]:
        """Return assignments due in ``[start, end]``, ordered by date then course."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT assignment_id, course, title, due_date, dependencies,
                       reported_submission_status
                FROM assignments
                WHERE due_date BETWEEN ? AND ?
                ORDER BY due_date, course, title
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [
            Assignment(
                assignment_id=str(row[0]),
                course=str(row[1]),
                title=str(row[2]),
                due_date=date.fromisoformat(str(row[3])),
                dependencies=str(row[4]).split(",") if row[4] else [],
                reported_submission_status=str(row[5]),
            )
            for row in rows
        ]

    def lookup(self, key: str) -> RetrievalResult | None:
        """Resolve a keyed structured query.

        "This week" means today plus the next six days, computed from the
        injected clock so tests can pin the date. The definition is a placeholder
        that belongs in a calendar policy once there is one.
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
                    item.model_dump(mode="json") for item in self.due_between(today, end)
                ]
            },
        )
