import sqlite3
import threading
from collections.abc import Iterable
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from blossom.retrieval import RetrievalResult


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

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
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
        if key != "due_this_week":
            return None
        today = date(2026, 8, 19)
        end = date(2026, 8, 25)
        return RetrievalResult(
            store_name=self.name,
            record_id=key,
            source_channel="fixture",
            asserted_at=datetime(2026, 8, 19, 9, 0, 0),
            payload={
                "assignments": [
                    item.model_dump(mode="json") for item in self.due_between(today, end)
                ]
            },
        )
