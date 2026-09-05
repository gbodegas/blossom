"""Store four: drafts, the record of what waited at the gate and what was decided.

The graph saves a draft here twice. Once when it is composed, so the parent's
queue shows it while the run waits at the gate, and once more with the
decision. Saved graph state holds the same facts, but one thread at a time, and
a queue is a question across threads: what is waiting, what was approved, what
was refused and why. That question is answered here, in one table, and the
rows outlive the process that wrote them.

Both writes are upserts keyed by a draft id the graph derives from its thread,
so a node that runs twice, as a resumed or crashed node does, leaves one row
rather than two. The file lives at ``BLOSSOM_DATABASE_PATH``, under the same
guard as the saved-state store: not on a share, not in a synced folder, with
deleted rows overwritten, because a refused draft is still text about her.

Nothing here sends anything. A row whose status is ``APPROVED_FOR_MANUAL_SEND``
is a draft a person may now copy out by hand. The store records that the
permission was given, and nothing else.
"""

import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict

from blossom.clock import Clock
from blossom.drafts import Decision, Draft, DraftStatus
from blossom.stores.checkpoints import refuse_unsafe_path

Outcome = Literal["accepted", "unsettled"]
"""The two run outcomes that produce a draft. The others end without one."""


class AlreadyDecided(RuntimeError):
    """Raised when a different decision is recorded for a draft that has one.

    Carries the record that stands, so a caller can say what was decided and
    when without reading the table again.
    """

    def __init__(self, record: "DraftRecord") -> None:
        super().__init__(f"draft {record.draft_id!r} was already {record.decision}")
        self.record = record


class DraftRecord(BaseModel):
    """One row: a draft, the run that produced it, and what a person decided."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    draft_id: str
    thread_id: str
    plan_date: date
    status: DraftStatus
    outcome: str
    body: str
    created_at: AwareDatetime
    decided_at: AwareDatetime | None = None
    decision: Decision | None = None
    reason: str | None = None

    @property
    def waiting(self) -> bool:
        """True while no person has decided."""
        return self.decision is None


class DraftsStore:
    """SQLite-backed drafts, shared across worker threads behind a lock."""

    name = "drafts"
    retention_policy = (
        "Keep a draft and its decision for the school year; a refused draft is "
        "kept so the refusal is visible, not so the text is reused."
    )

    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        # Rows are read by column name, so a query never spells the column list
        # and the order of columns in the table is not a contract.
        self._connection.row_factory = sqlite3.Row
        self._clock = clock
        self._lock = threading.Lock()
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS drafts (
                draft_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL UNIQUE,
                plan_date TEXT NOT NULL,
                status TEXT NOT NULL,
                outcome TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decided_at TEXT,
                decision TEXT,
                reason TEXT
            )
            """
        )
        self._connection.commit()

    @classmethod
    def open(cls, path: Path, clock: Clock) -> "DraftsStore":
        """Open the drafts file, refusing the places the saved-state store refuses."""
        safe = refuse_unsafe_path(path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(safe, check_same_thread=False)
        connection.execute("PRAGMA secure_delete=ON")
        return cls(connection, clock)

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._connection.close()

    def record_waiting(
        self, draft: Draft, *, thread_id: str, plan_date: date, outcome: Outcome
    ) -> None:
        """Save a draft the moment it exists, before the gate pauses on it.

        An upsert: the same draft saved again replaces its text and status and
        keeps its first ``created_at``, so a node that runs twice leaves one row.
        """
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO drafts (
                    draft_id, thread_id, plan_date, status, outcome, body, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(draft_id) DO UPDATE SET
                    status=excluded.status,
                    outcome=excluded.outcome,
                    body=excluded.body
                """,
                (
                    draft.draft_id,
                    thread_id,
                    plan_date.isoformat(),
                    draft.status.value,
                    outcome,
                    draft.body,
                    draft.created_at.isoformat(),
                ),
            )
            self._connection.commit()

    def record_decision(
        self, draft_id: str, *, status: DraftStatus, decision: Decision, reason: str | None
    ) -> DraftRecord:
        """Save what a person decided about a waiting draft, once.

        The update applies while no decision is recorded, or when the same
        decision is recorded again, which is what a node that runs twice does;
        the first time stamp is kept on a repeat. A different decision for a
        draft that has one is refused with ``AlreadyDecided``, so two people
        deciding at once cannot overwrite each other: the row is the referee,
        and the second is told what stood. The time is the store's clock, not
        the caller's, so every decision is stamped the same way.
        """
        stamp = self._clock.now().isoformat()
        with self._lock:
            updated = self._connection.execute(
                """
                UPDATE drafts
                SET status=?, decision=?, reason=?, decided_at=COALESCE(decided_at, ?)
                WHERE draft_id=?
                  AND (decision IS NULL OR (decision = ? AND reason IS ?))
                """,
                (status.value, decision, reason, stamp, draft_id, decision, reason),
            ).rowcount
            self._connection.commit()
        record = self.get(draft_id)
        if record is None:
            msg = f"no draft {draft_id!r} to decide about"
            raise KeyError(msg)
        if updated == 0:
            raise AlreadyDecided(record)
        return record

    def get(self, draft_id: str) -> DraftRecord | None:
        """One draft by id, or ``None``."""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM drafts WHERE draft_id=?", (draft_id,)
            ).fetchone()
        return None if row is None else record_from(row)

    def waiting(self) -> list[DraftRecord]:
        """Every draft no person has decided about, oldest first."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM drafts WHERE decision IS NULL ORDER BY created_at, draft_id"
            ).fetchall()
        return [record_from(row) for row in rows]

    def decided(self) -> list[DraftRecord]:
        """Every draft a person has decided about, most recent decision first."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM drafts WHERE decision IS NOT NULL ORDER BY decided_at DESC, draft_id"
            ).fetchall()
        return [record_from(row) for row in rows]


def record_from(row: sqlite3.Row) -> DraftRecord:
    """Build a record from a row read by column name."""
    decided_at = row["decided_at"]
    decision = row["decision"]
    reason = row["reason"]
    return DraftRecord(
        draft_id=str(row["draft_id"]),
        thread_id=str(row["thread_id"]),
        plan_date=date.fromisoformat(str(row["plan_date"])),
        status=DraftStatus(str(row["status"])),
        outcome=str(row["outcome"]),
        body=str(row["body"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        decided_at=None if decided_at is None else datetime.fromisoformat(str(decided_at)),
        decision=cast(Decision | None, None if decision is None else str(decision)),
        reason=None if reason is None else str(reason),
    )
