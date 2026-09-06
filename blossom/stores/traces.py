"""The framework's trace of a run, kept on this machine: every call with what went in and out.

The step records in the drafts file are Blossom's own account of a run, one
line per node in words a parent reads. This is the other account: what the
framework saw, every node and every model call beneath it, with inputs,
outputs, and errors. It is for finding out why a run did what it did, and it
holds the student's schoolwork verbatim, prompts and plans and the draft. So
it lives in its own file under the same guard as the other two, everything in
it passes through a redaction hook before it is written, and rows older than
``TRACE_RETENTION_DAYS`` are swept. Nothing reads it to decide anything.
"""

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from blossom.clock import Clock
from blossom.stores.checkpoints import refuse_unsafe_path

TRACE_RETENTION_DAYS: Final = 14
"""How long a run's trace is kept: long enough to look into a run that went wrong."""


@dataclass(frozen=True, kw_only=True)
class TracedRun:
    """One run in the framework's tree, already redacted, with its children beneath it.

    ``inputs``, ``outputs``, and ``error`` are text, JSON where the framework
    gave a structure, so the redaction hook sees everything as text and the
    store never interprets it.
    """

    run_id: str
    trace_id: str
    parent_run_id: str | None
    thread_id: str | None
    name: str
    run_type: str
    started_at: datetime
    ended_at: datetime | None
    inputs: str
    outputs: str | None
    error: str | None
    children: tuple["TracedRun", ...] = ()
    recorded_at: datetime | None = None
    """When the store wrote the row, by the store's clock; unset until it is read back."""


class TraceStore:
    """SQLite-backed run traces, in their own file, shared across threads behind a lock."""

    name = "traces"
    retention_policy = (
        "Keep the framework's trace of a run for two weeks, long enough to look into "
        "a run that went wrong; it holds prompts and answers verbatim, so nothing older "
        "stays."
    )

    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._clock = clock
        self._lock = threading.Lock()
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trace_runs (
                run_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                parent_run_id TEXT,
                thread_id TEXT,
                name TEXT NOT NULL,
                run_type TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                inputs TEXT NOT NULL,
                outputs TEXT,
                error TEXT,
                recorded_at TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    @classmethod
    def open(cls, path: Path, clock: Clock) -> "TraceStore":
        """Open the trace file, refusing the places the saved-state store refuses."""
        safe = refuse_unsafe_path(path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(safe, check_same_thread=False)
        connection.execute("PRAGMA secure_delete=ON")
        return cls(connection, clock)

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._connection.close()

    def record(self, root: TracedRun) -> None:
        """Save a whole tree in one transaction, replacing any run saved under the same id."""
        stamp = self._clock.now().isoformat()
        rows = [
            (
                run.run_id,
                run.trace_id,
                run.parent_run_id,
                run.thread_id,
                run.name,
                run.run_type,
                run.started_at.isoformat(),
                None if run.ended_at is None else run.ended_at.isoformat(),
                run.inputs,
                run.outputs,
                run.error,
                stamp,
            )
            for run in flattened(root)
        ]
        with self._lock, self._connection:
            self._connection.executemany(
                """
                INSERT OR REPLACE INTO trace_runs (
                    run_id, trace_id, parent_run_id, thread_id, name, run_type,
                    started_at, ended_at, inputs, outputs, error, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def sweep(self, keep_days: int = TRACE_RETENTION_DAYS) -> int:
        """Delete every run recorded more than ``keep_days`` ago; return how many went."""
        cutoff = (self._clock.now() - timedelta(days=keep_days)).isoformat()
        with self._lock, self._connection:
            removed = self._connection.execute(
                "DELETE FROM trace_runs WHERE recorded_at < ?", (cutoff,)
            ).rowcount
        return int(removed)

    def runs_for_thread(self, thread_id: str) -> list[TracedRun]:
        """Every run saved for one thread, flat, in the order they started."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM trace_runs WHERE thread_id=?
                ORDER BY started_at, run_id
                """,
                (thread_id,),
            ).fetchall()
        return [run_from(row) for row in rows]

    def count(self) -> int:
        """How many runs the file holds, across every thread."""
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) AS n FROM trace_runs").fetchone()
        return int(row["n"])


def flattened(root: TracedRun) -> list[TracedRun]:
    """The tree as a list, parents before children."""
    runs = [root]
    for child in root.children:
        runs.extend(flattened(child))
    return runs


def run_from(row: sqlite3.Row) -> TracedRun:
    """Build a run from a row read by column name; children are not reattached."""
    ended_at = row["ended_at"]
    return TracedRun(
        run_id=str(row["run_id"]),
        trace_id=str(row["trace_id"]),
        parent_run_id=None if row["parent_run_id"] is None else str(row["parent_run_id"]),
        thread_id=None if row["thread_id"] is None else str(row["thread_id"]),
        name=str(row["name"]),
        run_type=str(row["run_type"]),
        started_at=datetime.fromisoformat(str(row["started_at"])),
        ended_at=None if ended_at is None else datetime.fromisoformat(str(ended_at)),
        inputs=str(row["inputs"]),
        outputs=None if row["outputs"] is None else str(row["outputs"]),
        error=None if row["error"] is None else str(row["error"]),
        recorded_at=datetime.fromisoformat(str(row["recorded_at"])),
    )
