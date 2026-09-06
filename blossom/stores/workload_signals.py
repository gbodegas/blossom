"""Her signal that today is too much, kept briefly, visibly, and on her terms.

The signal is one gesture with nothing attached: no rating, no reason. Its
whole purpose is to reduce the next plan. It records something she told the
system, not an inference about her, so the store keeps each signal for a short
time, shows her everything it holds, and lets her take one back.

The store answers one question for the planner, whether she has said a given
evening is too much, and nothing about patterns. No query here groups signals
by weekday or counts them over a month. A record like that would be about her
rather than about the plan, which is the line this design does not cross.
"""

import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from blossom.clock import Clock
from blossom.stores.checkpoints import refuse_unsafe_path

SIGNAL_RETENTION_DAYS: Final = 7
"""How long a signal is kept: long enough for her to see it and take it back."""

DETAIL_MAX_LENGTH: Final = 500
"""The most that is kept of the words she adds: a sentence or two, the same
cap as the parent's reason. The signal is the press; the words are an aside,
and the store takes no more of them than a page can show."""


class WorkloadSignal(BaseModel):
    """One press of the control: which evening it was about and when it was given."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_id: str
    evening: date
    """The household date the signal is about, by the household's clock."""
    given_at: AwareDatetime
    """When it was given, by the real clock, so retention runs even when the clock is pinned."""
    detail: str | None = Field(default=None, max_length=DETAIL_MAX_LENGTH)
    """Words she chose to add. Never asked for, never required, and capped here
    as well as at the boundary, so nothing longer is ever stored."""


class WorkloadSignalsStore:
    """SQLite-backed workload signals, shared across threads behind a lock."""

    name = "workload_signals"
    retention_policy = (
        "Keep a signal for seven days, so she can see what she told the system and take "
        "it back; nothing older stays, and nothing is ever derived from the pattern of them."
    )

    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._clock = clock
        self._lock = threading.Lock()
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workload_signals (
                signal_id TEXT PRIMARY KEY,
                evening TEXT NOT NULL,
                given_at TEXT NOT NULL,
                detail TEXT
            )
            """
        )
        self._connection.commit()

    @classmethod
    def open(cls, path: Path, clock: Clock) -> "WorkloadSignalsStore":
        """Open the file, refusing the places the saved-state store refuses."""
        safe = refuse_unsafe_path(path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(safe, check_same_thread=False)
        connection.execute("PRAGMA secure_delete=ON")
        return cls(connection, clock)

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._connection.close()

    def record(self, evening: date, detail: str | None = None) -> WorkloadSignal:
        """Keep one press, about ``evening``, stamped now."""
        signal = WorkloadSignal(
            signal_id=uuid4().hex, evening=evening, given_at=self._clock.now(), detail=detail
        )
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO workload_signals (signal_id, evening, given_at, detail)
                VALUES (?, ?, ?, ?)
                """,
                (signal.signal_id, evening.isoformat(), signal.given_at.isoformat(), detail),
            )
        return signal

    def withdraw(self, signal_id: str) -> bool:
        """Remove one signal because she took it back. False when there was none to remove."""
        with self._lock, self._connection:
            removed = self._connection.execute(
                "DELETE FROM workload_signals WHERE signal_id=?", (signal_id,)
            ).rowcount
        return removed > 0

    def for_evening(self, evening: date) -> list[WorkloadSignal]:
        """Every signal about one evening still within retention, earliest first.

        The cutoff is applied on the read as well as by the sweep, so a signal
        past its week stops counting the moment it ages out, whether or not a
        sweep has run since.
        """
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM workload_signals
                WHERE evening=? AND given_at >= ?
                ORDER BY given_at, signal_id
                """,
                (evening.isoformat(), self._cutoff()),
            ).fetchall()
        return [signal_from(row) for row in rows]

    def held(self) -> list[WorkloadSignal]:
        """Everything still within retention, most recent first: what she can see and take back."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM workload_signals
                WHERE given_at >= ?
                ORDER BY given_at DESC, signal_id
                """,
                (self._cutoff(),),
            ).fetchall()
        return [signal_from(row) for row in rows]

    def sweep(self) -> int:
        """Delete every signal past retention; return how many went."""
        with self._lock, self._connection:
            removed = self._connection.execute(
                "DELETE FROM workload_signals WHERE given_at < ?", (self._cutoff(),)
            ).rowcount
        return int(removed)

    def _cutoff(self) -> str:
        """The oldest instant still within retention, by the store's clock."""
        return (self._clock.now() - timedelta(days=SIGNAL_RETENTION_DAYS)).isoformat()


def signal_from(row: sqlite3.Row) -> WorkloadSignal:
    """Build a signal from a row read by column name."""
    detail = row["detail"]
    return WorkloadSignal(
        signal_id=str(row["signal_id"]),
        evening=date.fromisoformat(str(row["evening"])),
        given_at=datetime.fromisoformat(str(row["given_at"])),
        detail=None if detail is None else str(detail),
    )
