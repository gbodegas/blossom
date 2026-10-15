# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Her signal that today is too much, kept briefly, visibly, and on her terms.

The signal is one gesture with nothing attached: no rating, no reason. Its
whole purpose is to reduce the next plan. It records something she told the
system, not an inference about her, so the store keeps each signal for a short
time, shows her every signal it holds, and lets her take one back.

Each press carries the id its page gave it, and the store keeps that id with one time,
the later of when the press was handled and when it was issued, and nothing else of
it. That is how a press sent again, from the same page or another tab, is known: it records
nothing, and a signal she took back stays gone. The id becomes due for removal once more
than seven days have passed since its time, which can be up to seven days and five minutes
after the press was handled, and the next cleanup removes it. Removing a signal removes the
signal and leaves its id, so the same press can't ask again.

The store answers one question for the planner, whether she has said a given
evening is too much, and nothing about patterns. No query here groups signals
by weekday or counts them over a month. A record like that would be about her
rather than about the plan, which is the line this design does not cross.
"""

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from blossom.clock import Clock
from blossom.stores.help_requests import request_id_from
from blossom.stores.paths import refuse_unsafe_path

SIGNAL_RETENTION_DAYS: Final = 7
"""How long a signal is kept: long enough for her to see it and take it back."""
PRESS_LIFETIME: Final = timedelta(days=SIGNAL_RETENTION_DAYS)
"""Seven days: a press issued this long ago or more is expired, and a press's id is due for
removal once more than this has passed since its one kept time, the later of when it was
handled and when it was issued. So no press that can still be sent has lost its id."""
IDS_TABLE: Final = """
    CREATE TABLE IF NOT EXISTS workload_signal_ids (
        signal_id TEXT PRIMARY KEY,
        used_at TEXT NOT NULL
    )
"""
IDS_FROM_SIGNALS: Final = """
    INSERT OR IGNORE INTO workload_signal_ids (signal_id, used_at)
    SELECT signal_id, given_at FROM workload_signals
"""
TONIGHT: Final = """
    SELECT * FROM workload_signals
    WHERE evening=? AND given_at >= ?
    ORDER BY given_at, signal_id
"""

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


@dataclass(frozen=True)
class SignalRecorded:
    """A press that kept a new signal, under its own id."""

    signal: WorkloadSignal


@dataclass(frozen=True)
class SignalUsed:
    """A press that records nothing: its id was handled before, or a signal already stood for
    its evening, and the id is now kept as handled. ``standing`` is the evening's signal as it
    stands, and ``own`` the signal this id recorded, while it is kept."""

    standing: WorkloadSignal | None
    own: WorkloadSignal | None = None


@dataclass(frozen=True)
class SignalExpired:
    """A press issued ``PRESS_LIFETIME`` or more before it was handled. Nothing is written."""


SignalOutcome = SignalRecorded | SignalUsed | SignalExpired


class WorkloadSignalsStore:
    """SQLite-backed workload signals, shared across threads behind a lock."""

    name = "workload_signals"
    retention_policy = (
        "Keep a signal for seven days, so she can see what she told the system and take "
        "it back; past that it is left out of every read and the next cleanup removes it, and "
        "nothing is ever derived from the pattern of them. The id of each press is kept with "
        "one time, the later of when it was handled and when it was issued, and nothing "
        "else of it, so a press sent again records nothing, even after its signal is taken "
        "back. The id is due for removal once more than seven days have passed since that "
        "time, which can be up to seven days and five minutes after the press was handled, "
        "and the next cleanup removes it."
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
        # The ids of the presses handled, by id and time alone. Each start fills the table
        # from the signals still here, in one transaction with it, so every kept signal's id
        # is used; doing it again adds nothing.
        self._connection.execute("BEGIN")
        try:
            self._connection.execute(IDS_TABLE)
            self._connection.execute(IDS_FROM_SIGNALS)
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

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
        """One press about ``evening`` under a fresh id, issued now: ``record_once`` for a
        caller with no form to send twice. The evening's standing signal when one stands."""
        outcome = self.record_once(uuid4().hex, evening, self._clock.now(), detail)
        if isinstance(outcome, SignalRecorded):
            return outcome.signal
        if isinstance(outcome, SignalUsed) and outcome.standing is not None:
            return outcome.standing
        msg = "a fresh signal id was already used"
        raise RuntimeError(msg)

    def record_once(
        self, signal_id: str, evening: date, issued_at: datetime, detail: str | None = None
    ) -> SignalOutcome:
        """Keep one signal for the press whose id this is, and never a second.

        The writer is reserved on this store's own connection, and the clock is read once,
        inside that transaction: a press issued ``PRESS_LIFETIME`` or more before then writes
        nothing. An id already kept records nothing. While a signal stands for ``evening``,
        the id is kept as handled and nothing else is written. Otherwise the id is kept and
        the signal written under it. The id is kept with the later of now and ``issued_at``,
        in UTC, so it outlasts every press that can still be sent. Whatever the file refuses
        is rolled back whole, and the id is left unused.
        """
        form = request_id_from(signal_id)
        if issued_at.tzinfo is None:
            msg = "an issue time needs its offset"
            raise ValueError(msg)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                outcome = self._handle(form, evening, issued_at, detail)
            except BaseException:
                self._connection.rollback()
                raise
        return outcome

    def _handle(
        self, form: str, evening: date, issued_at: datetime, detail: str | None
    ) -> SignalOutcome:
        """``record_once``'s work inside its transaction, which it ends."""
        now = self._clock.now().astimezone(UTC)
        if now - issued_at >= PRESS_LIFETIME:
            self._connection.rollback()
            return SignalExpired()
        cutoff = (now - PRESS_LIFETIME).isoformat()
        tonight = self._connection.execute(TONIGHT, (evening.isoformat(), cutoff)).fetchall()
        standing = signal_from(tonight[-1]) if tonight else None
        used_at = max(now, issued_at).astimezone(UTC).isoformat()
        reserved = self._connection.execute(
            "INSERT OR IGNORE INTO workload_signal_ids (signal_id, used_at) VALUES (?, ?)",
            (form, used_at),
        ).rowcount
        if reserved != 1:
            own = self._connection.execute(
                "SELECT * FROM workload_signals WHERE signal_id=? AND given_at >= ?",
                (form, cutoff),
            ).fetchone()
            self._connection.rollback()
            return SignalUsed(standing, None if own is None else signal_from(own))
        if standing is not None:
            self._connection.commit()
            return SignalUsed(standing)
        signal = WorkloadSignal(signal_id=form, evening=evening, given_at=now, detail=detail)
        self._connection.execute(
            """
            INSERT INTO workload_signals (signal_id, evening, given_at, detail)
            VALUES (?, ?, ?, ?)
            """,
            (form, evening.isoformat(), now.isoformat(), detail),
        )
        self._connection.commit()
        return SignalRecorded(signal)

    def withdraw(self, signal_id: str) -> bool:
        """Remove one signal because she took it back. False when there was none to remove. The
        id that asked for it stays kept as handled until a cleanup removes it, once more than
        seven days have passed since its kept time."""
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
                TONIGHT, (evening.isoformat(), self._cutoff())
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
        """Delete every signal past retention, and every id whose kept time, the later of its
        handling and its issue, is more than that long past, in one transaction by one
        cutoff; return how many signals went."""
        with self._lock:
            cutoff = self._cutoff()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                removed = self._connection.execute(
                    "DELETE FROM workload_signals WHERE given_at < ?", (cutoff,)
                ).rowcount
                self._connection.execute(
                    "DELETE FROM workload_signal_ids WHERE used_at < ?", (cutoff,)
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return int(removed)

    def _cutoff(self) -> str:
        """The oldest instant still within retention, by the store's clock."""
        return (self._clock.now().astimezone(UTC) - PRESS_LIFETIME).isoformat()


def signal_from(row: sqlite3.Row) -> WorkloadSignal:
    """Build a signal from a row read by column name."""
    detail = row["detail"]
    return WorkloadSignal(
        signal_id=str(row["signal_id"]),
        evening=date.fromisoformat(str(row["evening"])),
        given_at=datetime.fromisoformat(str(row["given_at"])),
        detail=None if detail is None else str(detail),
    )
