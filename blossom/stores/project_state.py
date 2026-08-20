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
    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    course: str
    title: str
    due_date: date
    dependencies: list[str]
    reported_submission_status: str


class ProjectStateStore:
    name = "project_state"
    retention_policy = "Keep structured assignment state for the academic year, then archive."

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self._connection = connection
        self._clock = SystemClock() if clock is None else clock
        # The connection is opened once at startup and shared by the worker
        # threads FastAPI uses for synchronous handlers, so every statement is
        # serialised. See blossom/dependencies.py for the full rationale.
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

        The window is computed from the injected clock rather than the fixed
        August 2026 dates the scaffold hardcoded. Defining "this week" as the
        next seven days inclusive is a placeholder: it preserves the previous
        behaviour exactly when the clock reads 2026-08-19, and the definition
        belongs in a calendar policy rather than in a store once there is one.
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
