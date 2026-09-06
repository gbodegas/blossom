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
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Final, Literal, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict

from blossom.agent.steps import StepRecord
from blossom.clock import Clock
from blossom.drafts import Decision, Draft, DraftStatus
from blossom.stores.checkpoints import refuse_unsafe_path

Outcome = Literal["accepted", "unsettled"]

SUPERSEDED_REASON: Final = "she planned again, and the later plan for the evening took its place"
"""The reason recorded on a waiting draft when a newer one for the same evening
is saved. System-recorded, like an expiry: no person said it."""
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
    steps: list[StepRecord] = []
    """The record of the run that produced the draft, read with it in one query."""
    too_much: bool = False
    """Whether she had said the evening was too much when this draft was made. A
    draft made for one kind of evening is not approved for the other."""

    @property
    def waiting(self) -> bool:
        """True while no person has decided."""
        return self.decision is None


class RunRecord(BaseModel):
    """One run of the plan graph: where it ended, and each step on the way."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: str
    plan_date: date
    outcome: str
    recorded_at: AwareDatetime
    steps: list[StepRecord]


class DraftsStore:
    """SQLite-backed drafts, shared across worker threads behind a lock."""

    name = "drafts"
    retention_policy = (
        "Keep a draft, its decision, and the record of the run that made it for the "
        "school year; a refused draft is kept so the refusal is visible, not so the "
        "text is reused, and the record of a run that produced no draft is kept for "
        "the same span so a parent can see why nothing came of it. A draft nobody "
        "decided within two weeks of its evening is closed as expired and kept the "
        "same way."
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
                reason TEXT,
                too_much INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        columns = {
            str(row["name"]) for row in self._connection.execute("PRAGMA table_info(drafts)")
        }
        if "too_much" not in columns:
            # A file written before drafts carried the signal: every draft in it
            # was made for a full evening.
            self._connection.execute(
                "ALTER TABLE drafts ADD COLUMN too_much INTEGER NOT NULL DEFAULT 0"
            )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                thread_id TEXT PRIMARY KEY,
                plan_date TEXT NOT NULL,
                outcome TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS steps (
                thread_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                node TEXT NOT NULL,
                round INTEGER NOT NULL,
                expected TEXT NOT NULL,
                found TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (thread_id, position)
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
        self,
        draft: Draft,
        *,
        thread_id: str,
        plan_date: date,
        outcome: Outcome,
        steps: Sequence[StepRecord] = (),
        too_much: bool = False,
    ) -> None:
        """Save a draft the moment it exists, before the gate pauses on it.

        An upsert: the same draft saved again replaces its text and status and
        keeps its first ``created_at``, so a node that runs twice leaves one row.
        The record of the run that produced the draft is saved in the same
        transaction, so a draft is never on the page without its account or
        the other way around.

        At most one draft waits per evening. Any other draft for the same
        evening still waiting is closed as superseded in the same transaction,
        so her page and the parent's queue always mean the same plan: the one
        she sees is the one a review can land on.
        """
        stamp = self._clock.now().isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO drafts (
                    draft_id, thread_id, plan_date, status, outcome, body, created_at, too_much
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(draft_id) DO UPDATE SET
                    status=excluded.status,
                    outcome=excluded.outcome,
                    body=excluded.body,
                    too_much=excluded.too_much
                """,
                (
                    draft.draft_id,
                    thread_id,
                    plan_date.isoformat(),
                    draft.status.value,
                    outcome,
                    draft.body,
                    draft.created_at.isoformat(),
                    int(too_much),
                ),
            )
            self._connection.execute(
                """
                UPDATE drafts
                SET decision='superseded', reason=?, decided_at=?
                WHERE plan_date=? AND decision IS NULL AND draft_id<>?
                """,
                (SUPERSEDED_REASON, stamp, plan_date.isoformat(), draft.draft_id),
            )
            self._write_run(thread_id, plan_date, outcome, steps)

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

    def record_run(
        self, *, thread_id: str, plan_date: date, outcome: str, steps: Sequence[StepRecord]
    ) -> None:
        """Save how a run went: where it ended and every step on the way.

        For a run that ended before the gate and so has no draft to carry its
        record; a run with a draft saves both together in ``record_waiting``.
        Saving the same thread again replaces its steps and keeps the first
        time stamp, so a node that runs twice leaves one account dated once.
        The whole replacement is one transaction: a failure part way through
        rolls it back and the earlier account stands.
        """
        with self._lock, self._connection:
            self._write_run(thread_id, plan_date, outcome, steps)

    def _write_run(
        self, thread_id: str, plan_date: date, outcome: str, steps: Sequence[StepRecord]
    ) -> None:
        """The run row and its steps, inside a transaction the caller holds open."""
        stamp = self._clock.now().isoformat()
        rows = [
            (
                thread_id,
                position,
                item.node,
                item.round,
                item.expected,
                item.found,
                item.recorded_at.isoformat(),
            )
            for position, item in enumerate(steps)
        ]
        self._connection.execute(
            """
            INSERT INTO runs (thread_id, plan_date, outcome, recorded_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET outcome=excluded.outcome
            """,
            (thread_id, plan_date.isoformat(), outcome, stamp),
        )
        self._connection.execute("DELETE FROM steps WHERE thread_id=?", (thread_id,))
        self._connection.executemany(
            """
            INSERT INTO steps (thread_id, position, node, round, expected, found, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def steps_for(self, thread_id: str) -> list[StepRecord]:
        """The steps of one run, in the order they happened; empty for a thread never saved."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM steps WHERE thread_id=? ORDER BY position", (thread_id,)
            ).fetchall()
        return [step_from(row) for row in rows]

    def runs_without_a_draft(self) -> list[RunRecord]:
        """Runs that ended before the gate, most recent first, each with its steps.

        One query joins the runs to their steps, so every record is assembled
        from a single snapshot and a replacement landing between two reads
        cannot pair one run's outcome with another's steps.
        """
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT runs.thread_id, runs.plan_date, runs.outcome, runs.recorded_at,
                       steps.node, steps.round, steps.expected, steps.found,
                       steps.recorded_at AS step_recorded_at
                FROM runs LEFT JOIN steps ON steps.thread_id = runs.thread_id
                WHERE runs.thread_id NOT IN (SELECT thread_id FROM drafts)
                ORDER BY runs.recorded_at DESC, runs.thread_id, steps.position
                """
            ).fetchall()
        grouped: dict[str, tuple[sqlite3.Row, list[StepRecord]]] = {}
        for row in rows:
            _, steps = grouped.setdefault(str(row["thread_id"]), (row, []))
            if row["node"] is not None:
                steps.append(joined_step_from(row))
        return [
            RunRecord(
                thread_id=thread_id,
                plan_date=date.fromisoformat(str(row["plan_date"])),
                outcome=str(row["outcome"]),
                recorded_at=datetime.fromisoformat(str(row["recorded_at"])),
                steps=steps,
            )
            for thread_id, (row, steps) in grouped.items()
        ]

    def get(self, draft_id: str) -> DraftRecord | None:
        """One draft by id with its run's record, or ``None``."""
        found = self._drafts(ONE_DRAFT, (draft_id,))
        return found[0] if found else None

    def waiting(self) -> list[DraftRecord]:
        """Every draft no person has decided about, oldest first."""
        return self._drafts(WAITING_DRAFTS, ())

    def decided(self) -> list[DraftRecord]:
        """Every draft a person has decided about, most recent decision first."""
        return self._drafts(DECIDED_DRAFTS, ())

    def superseded_for(self, plan_date: date) -> list[DraftRecord]:
        """Every draft for one evening that a later one took the place of."""
        return self._drafts(SUPERSEDED_DRAFTS, (plan_date.isoformat(),))

    def latest_for(self, plan_date: date) -> DraftRecord | None:
        """The most recent draft for one evening, reviewed or not; ``None`` when there is none.

        Her page shows the evening's latest plan whatever a parent has said
        about it, since the plan is hers from the moment it is made.
        """
        found = self._drafts(LATEST_FOR_EVENING, (plan_date.isoformat(),))
        return found[0] if found else None

    def _drafts(self, query: str, parameters: tuple[str, ...]) -> list[DraftRecord]:
        """Drafts and their steps from one query, so each record is one snapshot."""
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        grouped: dict[str, tuple[sqlite3.Row, list[StepRecord]]] = {}
        for row in rows:
            _, steps = grouped.setdefault(str(row["draft_id"]), (row, []))
            if row["node"] is not None:
                steps.append(joined_step_from(row))
        return [record_from(row, steps) for row, steps in grouped.values()]


def step_from(row: sqlite3.Row) -> StepRecord:
    """Build a step from a row read by column name."""
    return StepRecord(
        node=str(row["node"]),
        round=int(row["round"]),
        expected=str(row["expected"]),
        found=str(row["found"]),
        recorded_at=datetime.fromisoformat(str(row["recorded_at"])),
    )


DRAFTS_WITH_STEPS = """
    SELECT drafts.draft_id, drafts.thread_id, drafts.plan_date, drafts.status,
           drafts.outcome, drafts.body, drafts.created_at, drafts.decided_at,
           drafts.decision, drafts.reason, drafts.too_much,
           steps.node, steps.round, steps.expected, steps.found,
           steps.recorded_at AS step_recorded_at
    FROM drafts LEFT JOIN steps ON steps.thread_id = drafts.thread_id
"""
"""Every read of a draft starts here, so the steps come from the same snapshot."""

ONE_DRAFT = DRAFTS_WITH_STEPS + "WHERE drafts.draft_id=? ORDER BY steps.position"
WAITING_DRAFTS = (
    DRAFTS_WITH_STEPS
    + "WHERE drafts.decision IS NULL ORDER BY drafts.created_at, drafts.draft_id, steps.position"
)
DECIDED_DRAFTS = (
    DRAFTS_WITH_STEPS
    + "WHERE drafts.decision IS NOT NULL "
    + "ORDER BY drafts.decided_at DESC, drafts.draft_id, steps.position"
)
SUPERSEDED_DRAFTS = (
    DRAFTS_WITH_STEPS
    + "WHERE drafts.plan_date=? AND drafts.decision='superseded' "
    + "ORDER BY drafts.created_at, drafts.draft_id, steps.position"
)
LATEST_FOR_EVENING = (
    DRAFTS_WITH_STEPS
    + "WHERE drafts.plan_date=? "
    + "ORDER BY drafts.created_at DESC, drafts.draft_id DESC, steps.position"
)


def joined_step_from(row: sqlite3.Row) -> StepRecord:
    """Build a step from a joined row, where its time is ``step_recorded_at``."""
    return StepRecord(
        node=str(row["node"]),
        round=int(row["round"]),
        expected=str(row["expected"]),
        found=str(row["found"]),
        recorded_at=datetime.fromisoformat(str(row["step_recorded_at"])),
    )


def record_from(row: sqlite3.Row, steps: list[StepRecord]) -> DraftRecord:
    """Build a record from a row read by column name, with the steps read beside it."""
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
        steps=steps,
        too_much=bool(row["too_much"]),
    )
