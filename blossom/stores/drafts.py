# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Store four: drafts, the record of what waited at the gate and what was decided.

The graph saves a draft here twice. Once when it is composed, so the parent's
queue shows it while the run waits at the gate, and once more with the
decision. Saved graph state holds the same facts, but one thread at a time, and
a queue is a question across threads: what is waiting, what was approved, what
was refused and why. That question is answered here, in one table, and the
rows outlive the process that wrote them.

Both writes are keyed by a draft id the graph derives from its thread, so a
node that runs twice, as a resumed or crashed node does, leaves one row rather
than two. What a composition saves is one bundle, the text, the snapshot of
the plan as data, the assignments it speaks about, and the fingerprint of what
the run read, written whole or not at all; once a draft is on the pages that
bundle is the record, and a different one is refused rather than written over
it. The file lives at ``BLOSSOM_DATABASE_PATH``, under the same
guard as the saved-state store: not on a share, not in a synced folder, with
deleted rows overwritten, because a refused draft is still text about her.

Nothing here sends anything. A row whose status is ``APPROVED_FOR_MANUAL_SEND``
is a draft a person may now copy out by hand. The store records that the
permission was given, and nothing else.
"""

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Final, Literal, NamedTuple, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict

from blossom.agent.runs import RUN_DEADLINE_SECONDS
from blossom.agent.steps import DATE_PROBLEM, PAST_DUE_WORK, PastDueWork, RunTiming, StepRecord
from blossom.clock import Clock
from blossom.drafts import Decision, Draft, DraftStatus
from blossom.plan_snapshot import PlanSnapshot
from blossom.stores.paths import refuse_unsafe_path

logger = logging.getLogger(__name__)

Outcome = Literal["accepted", "unsettled"]
"""The two run outcomes that produce a draft. The others end without one."""

RunStatus = Literal["running", "published", "ended"]
"""Where a run stands: still working, settled with its plan on the pages, or ended without one."""

SUPERSEDED_REASON: Final = "a later plan for the evening took its place"
"""The reason recorded on a waiting draft when a newer one for the same evening
is published, from whichever page. System-recorded, like an expiry: no person said it."""

STORE_WAIT_SECONDS: Final = 5.0
"""The longest one call waits for the store, the file's writer and its commit together,
unless its caller gives it less."""

SETTLE_GRACE_SECONDS: Final = 1.0
"""How long past its run's deadline a publication authorized in time may wait to commit."""

RUNNING: Final = "running"
"""The outcome a run holds from admission until its draft is composed. Never shown."""

TIMED_OUT: Final = "timed_out"
"""The reason recorded on a run whose deadline passed before it settled."""

INTERRUPTED: Final = "interrupted"
"""The reason recorded on a run cut off before it settled: canceled, stopped by a
restart, or left without a draft it could publish. Its draft is deleted; its steps stay."""

OVERTAKEN: Final = "overtaken"
"""The reason recorded on a run whose evening gained a newer plan while it worked. Its
draft is deleted, so a late result never replaces a newer plan; its steps stay."""


class Displaced(NamedTuple):
    """A draft a publication displaced, read before the commit; enough to clear its thread."""

    draft_id: str
    thread_id: str


class StoreBusy(TimeoutError):
    """The store stayed in use by another caller for the whole wait. Nothing was begun."""


class WriterBusy(TimeoutError):
    """Another connection held the file's writer for the whole wait. Nothing was begun."""


class NotPublished(RuntimeError):
    """A decision about a draft that is not on the pages. Nothing is written."""


class IncoherentBundle(ValueError):
    """A composition whose parts do not describe one plan: a snapshot for another evening
    than its draft, or for other assignments than the draft lists. Refused before anything
    is written."""


class IncompatibleReplay(RuntimeError):
    """A save that would change a record it may not change: a different composition for a
    draft already on the pages, or one that would drop the snapshot a saved draft has.
    Nothing is written, and the draft stands as it was."""


class RunState(NamedTuple):
    """Where one run stands in the record, read in one transaction."""

    run_id: str
    plan_date: date
    status: RunStatus
    reason: str
    """The run's outcome: ``running`` until its draft is composed, then ``accepted`` or
    ``unsettled``; for an ended run, why it ended."""
    seconds_left: float
    """Seconds before a running run's deadline on the store's monotonic clock; 0 once it
    has passed, and for a run that is not running."""
    plan_unchanged: bool
    """Whether the evening's last publication is still the one in force when the run was
    admitted."""
    draft: "DraftRecord | None" = None
    """The plan a published run put on the pages, when it was read with the run."""
    has_plan: bool = False
    """Whether the evening has a published plan as the run is read."""
    past_due: tuple[PastDueWork, ...] | None = None
    """The past-due work a run that ended on the date problem kept, in the order its answer
    names it; ``None`` for a run that kept none, or whose kept work can't be read."""


class StaleBasis(RuntimeError):
    """A press made from a page that didn't show the evening's newest plan: a plan for the
    evening was published after the one the page named. Nothing is written."""


class UnknownBasis(RuntimeError):
    """A press naming, as the newest plan its page knew, a draft never published. Nothing is
    written."""


class Settled(NamedTuple):
    """What ``settle_run`` committed: the run as it stands, and what its plan displaced."""

    run: RunState
    displaced: list[Displaced]


class RunEnded(RuntimeError):
    """A write for a run that is not running. Nothing of it is written.

    Carries the run as it stands, or ``None`` for a run the store never admitted.
    """

    def __init__(self, run_id: str, run: RunState | None) -> None:
        standing = "never admitted" if run is None else run.status
        super().__init__(f"run {run_id!r} is {standing}")
        self.run = run


class ReviewSnapshot(NamedTuple):
    """The drafts a page of plans shows, from one reading of the table: what waits, what was
    decided, and which draft is the household day's working plan. Read together, so no
    draft is in two groups, none is named that was not read, and the plan called current is
    one of the records in hand."""

    waiting: tuple["DraftRecord", ...]
    """Every published draft no person has decided about, oldest first."""
    decided: tuple["DraftRecord", ...]
    """Every draft a person has decided about, most recent decision first."""
    current_id: str | None
    """The last draft published for the day that no later one displaced, whatever was
    decided about it; ``None`` when the day has none."""
    operative: frozenset[str]
    """The same for the day and every later evening: each evening's plan in force, waiting,
    approved, or refused, which a page shows beside what stands now."""
    newest: str = ""
    """The draft last in the published order, of any evening; empty when none is."""


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
    inputs_digest: str | None = None
    """A fingerprint of the assignments the run read for this evening's window, taken
    when it read them: what was due, when, of what kind, with what note and whose words
    the note is, and what the sources said. A plan whose window reads differently now was
    made for other work.
    ``None`` for a draft from before plans carried one."""
    published: bool = False
    """Whether the run that made this draft settled with it. A draft is saved the
    moment it is composed, as the record, and published when its run settles, which
    is when it reaches the pages and takes the place of the plan before it. Until
    then it is nobody's plan."""
    plan_assignment_ids: list[str] | None = None
    """Every assignment the plan speaks about, worked on or put off, each once, so a page
    can say which of them she has since reported done. ``None`` for a draft from before
    plans carried them, which is not the same as a plan that speaks about nothing."""
    plan_snapshot: str | None = None
    """The plan as data, one versioned JSON document saved with the text, as it was
    written. ``None`` for a draft from before plans carried one. Kept as text here: what
    it lets a page show is decided where it is read, one record at a time, so one that
    cannot be used never stops another from being shown."""

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
    newest: bool = False
    """Whether no later run of the same evening, with a draft or without, was saved; read
    only for runs that ended without a draft."""
    timing: RunTiming | None = None
    """How long the run took and what it asked for; ``None`` for a run that kept no time."""


class DraftsStore:
    """SQLite-backed drafts, shared across worker threads behind a lock."""

    name = "drafts"
    retention_policy = (
        "Keep a draft, its decision, and the record of the run that made it for the "
        "school year; a refused draft is kept so the refusal is visible, not so the "
        "text is reused, and the record of a run that produced no draft is kept for "
        "the same span so a parent can see why nothing came of it. A draft nobody "
        "decided within two weeks of its evening is closed as expired and kept the "
        "same way, and so is a draft a later plan for the same evening took the place "
        "of. A draft whose run failed before it could wait for review is the one row "
        "removed, since it was never anyone's plan; its run stays. A run's row is also "
        "what makes the plan form that started it used, and a form expires seven days "
        "after its page was opened, well inside that span."
    )

    def __init__(
        self,
        connection: sqlite3.Connection,
        clock: Clock,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connection = connection
        # Rows are read by column name, so a query never spells the column list
        # and the order of columns in the table is not a contract.
        self._connection.row_factory = sqlite3.Row
        self._clock = clock
        # Run deadlines are instants on this clock, the one the process's run budgets read.
        self._monotonic = monotonic
        self._lock = threading.Lock()
        # One transaction for the whole of opening: the tables, any column an
        # older file lacks, and the two invariants below commit together or not
        # at all, so a process that dies half way through leaves the file as it
        # found it and the next open starts over.
        self._connection.execute("BEGIN")
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
                too_much INTEGER NOT NULL DEFAULT 0,
                superseded_by TEXT,
                published INTEGER NOT NULL DEFAULT 0,
                published_order INTEGER,
                plan_assignment_ids TEXT,
                plan_snapshot TEXT
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
        if "superseded_by" not in columns:
            self._connection.execute("ALTER TABLE drafts ADD COLUMN superseded_by TEXT")
        if "published" not in columns:
            # A file from before drafts were published apart from being saved:
            # every draft in it was on the pages, so every one is published.
            self._connection.execute(
                "ALTER TABLE drafts ADD COLUMN published INTEGER NOT NULL DEFAULT 0"
            )
            self._connection.execute("UPDATE drafts SET published=1")
        if "inputs_digest" not in columns:
            # A file from before plans carried a fingerprint of their window:
            # its drafts have none, and are not measured against the week.
            self._connection.execute("ALTER TABLE drafts ADD COLUMN inputs_digest TEXT")
        if "plan_assignment_ids" not in columns:
            # A file from before drafts named the work their plan speaks about:
            # its drafts name none, which is told from a plan that speaks
            # about nothing, which names an empty list.
            self._connection.execute("ALTER TABLE drafts ADD COLUMN plan_assignment_ids TEXT")
        if "plan_snapshot" not in columns:
            # A file from before plans were saved as data beside their text: its
            # drafts have no snapshot and keep none, and are read as text.
            self._connection.execute("ALTER TABLE drafts ADD COLUMN plan_snapshot TEXT")
        if "published_order" not in columns:
            self._connection.execute("ALTER TABLE drafts ADD COLUMN published_order INTEGER")
            if "saved_order" in columns:
                # A file numbered in the order saved, from before publication
                # had an order of its own; the numbers carry over.
                self._connection.execute("UPDATE drafts SET published_order=saved_order")
        # Two invariants, restored on every open rather than only when a column
        # is added, so a file an older version wrote, or one left half way by a
        # process that died while opening it, is brought into line by the next
        # open. Every published draft has a place in the published order, older
        # files' drafts taking theirs by when they were made; and one draft
        # waits per evening, the rest closed as superseded by the evening's
        # latest. The threads of drafts closed here go at the startup sweep,
        # which clears every thread no waiting draft refers to.
        self._number_in_order_made()
        self._keep_one_waiting_per_evening()
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                thread_id TEXT PRIMARY KEY,
                plan_date TEXT NOT NULL,
                outcome TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                timing TEXT
            )
            """
        )
        run_columns = {
            str(row["name"]) for row in self._connection.execute("PRAGMA table_info(runs)")
        }
        if "timing" not in run_columns:
            # A file from before runs were timed: its runs keep no time.
            self._connection.execute("ALTER TABLE runs ADD COLUMN timing TEXT")
        if "past_due" not in run_columns:
            # A file from before runs kept their past-due work: its runs keep none.
            self._connection.execute("ALTER TABLE runs ADD COLUMN past_due TEXT")
        for column, kind in RUN_LIFECYCLE_COLUMNS:
            if column not in run_columns:
                self._connection.execute(f"ALTER TABLE runs ADD COLUMN {column} {kind}")
        self._settle_runs_without_a_status()
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
        self._report_publication_mismatches()
        self._connection.commit()

    def _settle_runs_without_a_status(self) -> None:
        """Give each run without a status one; delete drafts no running or published run owns.

        Runs in the open transaction, at every open, and changes nothing the second
        time. A run whose draft is published is ``published``; any other is ``ended``,
        and one that stopped with a draft it never published ended ``interrupted``. An
        unpublished draft nobody decided about is deleted unless its run is running or
        published: a missing mark never makes a draft publishable, and a published run's
        draft stays for the check that reports it. A decided draft is never deleted.
        """
        self._connection.execute(
            """
            UPDATE runs SET status='published'
            WHERE status IS NULL
              AND thread_id IN (SELECT thread_id FROM drafts WHERE published=1)
            """
        )
        self._connection.execute(
            """
            UPDATE runs
            SET status='ended',
                outcome=CASE WHEN outcome IN ('accepted', 'unsettled') THEN ? ELSE outcome END
            WHERE status IS NULL
            """,
            (INTERRUPTED,),
        )
        self._connection.execute(
            """
            DELETE FROM drafts
            WHERE published=0 AND decision IS NULL
              AND thread_id NOT IN (SELECT thread_id FROM runs WHERE status='running')
              AND thread_id NOT IN (SELECT thread_id FROM runs WHERE status='published')
            """
        )
        self._connection.execute("DROP TABLE IF EXISTS withheld_drafts")

    def _report_publication_mismatches(self) -> None:
        """Log any run whose status disagrees with whether its draft is published.

        A published run has a published draft, and a published draft with a run has a
        published run. A disagreement is logged with its threads and changes nothing.
        """
        rows = self._connection.execute(
            """
            SELECT runs.thread_id FROM runs
            WHERE runs.status='published' AND NOT EXISTS (
                SELECT 1 FROM drafts
                WHERE drafts.thread_id=runs.thread_id AND drafts.published=1
            )
            UNION
            SELECT drafts.thread_id FROM drafts JOIN runs ON runs.thread_id=drafts.thread_id
            WHERE drafts.published=1 AND runs.status<>'published'
            ORDER BY 1
            """
        ).fetchall()
        if rows:
            logger.warning(
                "runs and published drafts disagree for threads: %s",
                ", ".join(str(row["thread_id"]) for row in rows),
            )

    def _number_in_order_made(self) -> None:
        """Give every published draft lacking a place in the published order one, after the rest.

        Such drafts come from files written before the order existed, and are
        ordered by when they were made, then by id. A file with nothing to
        number is left as it is, so this runs on every open at no cost.
        """
        (highest,) = self._connection.execute(
            "SELECT COALESCE(MAX(published_order), 0) FROM drafts"
        ).fetchone()
        rows = self._connection.execute(
            """
            SELECT draft_id FROM drafts
            WHERE published=1 AND published_order IS NULL
            ORDER BY created_at, draft_id
            """
        ).fetchall()
        self._connection.executemany(
            "UPDATE drafts SET published_order=? WHERE draft_id=?",
            [
                (position, str(row["draft_id"]))
                for position, row in enumerate(rows, start=int(highest) + 1)
            ],
        )

    def _keep_one_waiting_per_evening(self, plan_date: str | None = None) -> None:
        """Close every published draft waiting behind the evening's latest, whatever its state.

        The latest published draft for an evening is the one her page shows:
        the last published that no later draft displaced, decided or not. A
        draft still waiting behind it would be a plan the queue holds and her
        page does not, so it is closed as superseded by the latest. A
        superseded draft is never the latest here, as it is not for her page;
        an unpublished draft is not in the running, as it is not on any page.
        Runs in the caller's transaction, over one evening when ``plan_date``
        is given and over the whole file otherwise, and does nothing to what
        is already in line.
        """
        self._connection.execute(
            """
            UPDATE drafts
            SET decision='superseded', reason=?, decided_at=?,
                superseded_by=(
                    SELECT latest.draft_id FROM drafts AS latest
                    WHERE latest.plan_date=drafts.plan_date AND latest.published=1
                      AND (latest.decision IS NULL OR latest.decision<>'superseded')
                    ORDER BY latest.published_order DESC LIMIT 1
                )
            WHERE decision IS NULL AND published=1
              AND (? IS NULL OR plan_date=?)
              AND published_order < (
                SELECT MAX(latest.published_order) FROM drafts AS latest
                WHERE latest.plan_date=drafts.plan_date AND latest.published=1
                  AND (latest.decision IS NULL OR latest.decision<>'superseded')
              )
            """,
            (SUPERSEDED_REASON, self._clock.now().isoformat(), plan_date, plan_date),
        )

    @classmethod
    def open(
        cls, path: Path, clock: Clock, monotonic: Callable[[], float] = time.monotonic
    ) -> "DraftsStore":
        """Open the drafts file, refusing the places the saved-state store refuses."""
        safe = refuse_unsafe_path(path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(safe, check_same_thread=False)
        connection.execute("PRAGMA secure_delete=ON")
        return cls(connection, clock, monotonic)

    def close(self, wait: float = STORE_WAIT_SECONDS) -> None:
        """Close the underlying connection once no other call holds the store."""
        if not self._lock.acquire(timeout=max(0.0, wait)):
            raise StoreBusy
        try:
            self._connection.close()
        finally:
            self._lock.release()

    @contextmanager
    def _session(
        self, wait: float = STORE_WAIT_SECONDS, *, write: bool = False
    ) -> Iterator["_Session"]:
        """One call's hold on the store: the store, writer and commit waits share ``wait``.

        The file's busy timeout is set to what is left before the first statement and again
        before the commit, so no call inherits another's. ``StoreBusy`` and ``WriterBusy``
        are raised before any transaction begins. A write reserves the writer with
        ``BEGIN IMMEDIATE`` before it reads, and commits inside the scope that rolls back,
        so a commit the file refuses leaves nothing open.
        """
        ends = time.monotonic() + max(0.0, wait)
        if not self._lock.acquire(timeout=max(0.0, wait)):
            raise StoreBusy
        try:
            session = _Session(ends)
            self._wait_until(ends)
            if not write:
                yield session
                return
            try:
                self._connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                    raise WriterBusy from error
                raise
            try:
                yield session
                self._wait_until(session.commit_ends)
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        finally:
            self._lock.release()

    def _wait_until(self, ends: float) -> None:
        """Let the file's busy handler wait until ``ends``, an instant on ``time.monotonic``."""
        milliseconds = max(0, int((ends - time.monotonic()) * 1000))
        self._connection.execute(f"PRAGMA busy_timeout={milliseconds}")

    def record_waiting(
        self,
        draft: Draft,
        *,
        thread_id: str,
        plan_date: date,
        outcome: Outcome,
        steps: Sequence[StepRecord] = (),
        too_much: bool = False,
        inputs_digest: str | None = None,
        plan_assignment_ids: Sequence[str] | None = None,
        plan_snapshot: PlanSnapshot | None = None,
        wait: float = STORE_WAIT_SECONDS,
    ) -> None:
        """Save a draft the moment it exists, before the gate pauses on it, as the record.

        Only a running run saves. Past its deadline the run is recorded ``timed_out``
        with ``steps`` first, and the save is refused with ``RunEnded``; a run that is
        not running is refused the same way, and nothing of the draft is written.

        What is saved is one bundle: the text, the snapshot of the plan as
        data, the assignments the plan speaks about, the fingerprint of what
        the run read, and the run's record. It is checked and written to text
        before anything is written: a snapshot for another evening than the
        draft's, or for other assignments than the draft lists, is
        ``IncoherentBundle``. Then the bundle and the run's record are
        written in one transaction, so a draft is never on the page without
        its account, its text never without its snapshot, or the other way
        around, and a write that fails leaves nothing of any of them.

        The same draft saved again, as a node that runs twice does, keeps its
        first ``created_at`` and leaves one row. Before its run has paused,
        the whole bundle takes the place of the one before, never part of
        it; a save that would drop the snapshot the draft has, or that names
        another thread or evening, is ``IncompatibleReplay``. A draft on the
        pages belongs to a run that has settled, so a save for it is
        ``RunEnded`` and changes nothing, not the status, the decision, its
        reason, its place in the published order, nor the steps its run
        recorded since: a plan already shown or reviewed is not written over.

        Saving is not publishing. The draft reaches no page and displaces no
        plan until ``settle_run`` publishes it, so a run that ends between
        saving and settling has shown nobody anything and taken nothing away.
        """
        names = None if plan_assignment_ids is None else list(plan_assignment_ids)
        if plan_snapshot is not None:
            if plan_snapshot.plan.plan_date != plan_date:
                msg = (
                    f"the snapshot of {draft.draft_id!r} is for "
                    f"{plan_snapshot.plan.plan_date.isoformat()}, its draft for "
                    f"{plan_date.isoformat()}"
                )
                raise IncoherentBundle(msg)
            if names != plan_snapshot.assignment_ids:
                msg = (
                    f"the snapshot of {draft.draft_id!r} speaks about other assignments than "
                    "its draft lists"
                )
                raise IncoherentBundle(msg)
        composed = (
            thread_id,
            plan_date.isoformat(),
            outcome,
            draft.body,
            int(too_much),
            inputs_digest,
            None if names is None else json.dumps(names),
            None if plan_snapshot is None else plan_snapshot.model_dump_json(),
        )
        refused = False
        with self._session(wait, write=True):
            run = self._run_state(thread_id)
            if run is None or run.status != "running":
                refused = True
            elif run.seconds_left <= 0:
                self._replace_steps(thread_id, steps)
                self._end(thread_id, TIMED_OUT)
                run = self._run_state(thread_id)
                refused = True
            else:
                self._save_bundle(draft, composed, steps)
        if refused:
            raise RunEnded(thread_id, run)

    def _save_bundle(
        self,
        draft: Draft,
        composed: tuple[str, str, str, str, int, str | None, str | None, str | None],
        steps: Sequence[StepRecord],
    ) -> None:
        """The writes of ``record_waiting`` for a running run, inside the caller's transaction."""
        row = self._connection.execute(
            """
            SELECT thread_id, plan_date, outcome, body, too_much, inputs_digest,
                   plan_assignment_ids, plan_snapshot, published
            FROM drafts WHERE draft_id=?
            """,
            (draft.draft_id,),
        ).fetchone()
        if row is not None:
            standing = (
                str(row["thread_id"]),
                str(row["plan_date"]),
                str(row["outcome"]),
                str(row["body"]),
                int(row["too_much"]),
                None if row["inputs_digest"] is None else str(row["inputs_digest"]),
                None if row["plan_assignment_ids"] is None else str(row["plan_assignment_ids"]),
                None if row["plan_snapshot"] is None else str(row["plan_snapshot"]),
            )
            if row["published"]:
                if standing == composed:
                    return
                msg = (
                    f"draft {draft.draft_id!r} is on the pages; a different composition "
                    "cannot take its place"
                )
                raise IncompatibleReplay(msg)
            if standing[:2] != composed[:2]:
                msg = f"draft {draft.draft_id!r} was saved for another thread or evening"
                raise IncompatibleReplay(msg)
            if standing[7] is not None and composed[7] is None:
                msg = f"a save of draft {draft.draft_id!r} would drop the snapshot it has"
                raise IncompatibleReplay(msg)
        self._connection.execute(
            """
            INSERT INTO drafts (
                draft_id, thread_id, plan_date, status, outcome, body, created_at,
                too_much, inputs_digest, plan_assignment_ids, plan_snapshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(draft_id) DO UPDATE SET
                status=excluded.status,
                outcome=excluded.outcome,
                body=excluded.body,
                too_much=excluded.too_much,
                inputs_digest=excluded.inputs_digest,
                plan_assignment_ids=excluded.plan_assignment_ids,
                plan_snapshot=excluded.plan_snapshot
            """,
            (
                draft.draft_id,
                composed[0],
                composed[1],
                draft.status.value,
                composed[2],
                composed[3],
                draft.created_at.isoformat(),
                composed[4],
                composed[5],
                composed[6],
                composed[7],
            ),
        )
        self._connection.execute(
            "UPDATE runs SET outcome=? WHERE thread_id=? AND status='running'",
            (composed[2], composed[0]),
        )
        self._replace_steps(composed[0], steps)

    def admit_run(
        self,
        run_id: str,
        *,
        plan_date: date,
        deadline_mono: float,
        wait: float = STORE_WAIT_SECONDS,
        basis: str | None = None,
    ) -> RunState | None:
        """Admit a run for the household, or return the run that stands in its way.

        One transaction: runs past their deadline are ended first. A run already recorded
        under ``run_id`` is returned as it stands, read with its draft; then a running run,
        if one remains, is returned. With ``basis``, the newest plan the pressing page knew
        (empty for none), a later publication for the evening is ``StaleBasis`` and a draft
        never published is ``UnknownBasis``. In each case nothing is written. Otherwise
        this run is recorded ``running`` with its deadline, an instant on the store's
        monotonic clock, and the evening's last place in the published order, the plan it
        expects to replace.
        """
        refusal: RuntimeError | None = None
        with self._session(wait, write=True):
            self._reconciled()
            same = self._run_state(run_id, with_draft=True)
            if same is not None:
                return same
            row = self._connection.execute(
                RUN_STATE + "WHERE runs.status='running' ORDER BY runs.recorded_at, runs.rowid"
            ).fetchone()
            if row is not None:
                return self._state_from(row)
            known = 0 if not basis else self._published_place(basis)
            if known is None:
                refusal = UnknownBasis(basis)
            elif basis is not None and self._last_published(plan_date.isoformat()) > known:
                refusal = StaleBasis(plan_date.isoformat())
            else:
                self._insert_run(run_id, plan_date, deadline_mono)
        if refusal is not None:
            raise refusal
        return None

    def _published_place(self, draft_id: str) -> int | None:
        """A published draft's place in the published order, ``None`` for any other id,
        inside the caller's hold."""
        row = self._connection.execute(
            "SELECT published_order FROM drafts WHERE draft_id=? AND published=1", (draft_id,)
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def newest_published(self) -> str:
        """The draft id last in the published order, of any evening; empty when none is."""
        with self._session():
            row = self._connection.execute(
                "SELECT draft_id FROM drafts WHERE published=1 "
                "ORDER BY published_order DESC LIMIT 1"
            ).fetchone()
        return "" if row is None else str(row[0])

    def _insert_run(self, run_id: str, plan_date: date, deadline_mono: float) -> None:
        """Record an admitted run ``running``, inside the caller's write."""
        self._connection.execute(
            """
                INSERT INTO runs (
                    thread_id, plan_date, outcome, recorded_at, status, deadline_mono, base_order
                ) VALUES (?, ?, ?, ?, 'running', ?, ?)
                """,
            (
                run_id,
                plan_date.isoformat(),
                RUNNING,
                self._clock.now().isoformat(),
                deadline_mono,
                self._last_published(plan_date.isoformat()),
            ),
        )

    def settle_run(
        self,
        run_id: str,
        *,
        timing: RunTiming | None = None,
        wait: float = STORE_WAIT_SECONDS,
        grace: float = SETTLE_GRACE_SECONDS,
    ) -> Settled:
        """Publish a running run's draft, or end the run, in one transaction.

        ``wait`` bounds the waits for the store and the writer. Once the writer is
        held, the run must still be running, its deadline still ahead, the evening's
        last publication the one it was admitted against, and its draft saved,
        unpublished and undecided. Then the draft takes the next place in the
        published order and closes every published draft still waiting for its
        evening as superseded by it. Otherwise the run ends ``timed_out``,
        ``overtaken`` or ``interrupted`` and its draft is deleted. A run that is
        not running is returned as it stands, so a repeated call changes nothing.

        The commit waits until the deadline plus ``grace``, at most
        ``STORE_WAIT_SECONDS``. What is returned was read inside the transaction,
        and nothing runs after the commit.
        """
        displaced: list[Displaced] = []
        with self._session(wait, write=True) as session:
            row = self._connection.execute(
                "SELECT plan_date, status, deadline_mono, base_order FROM runs WHERE thread_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                msg = f"no run {run_id!r} to settle"
                raise KeyError(msg)
            now = self._monotonic()
            if row["deadline_mono"] is not None:
                commit_wait = min(STORE_WAIT_SECONDS, float(row["deadline_mono"]) + grace - now)
                session.commit_ends = time.monotonic() + max(0.0, commit_wait)
            if row["status"] == "running":
                candidate = self._connection.execute(
                    "SELECT draft_id, published, decision FROM drafts WHERE thread_id=?",
                    (run_id,),
                ).fetchone()
                if row["deadline_mono"] is None or float(row["deadline_mono"]) <= now:
                    self._end(run_id, TIMED_OUT, timing)
                elif row["base_order"] != self._last_published(str(row["plan_date"])):
                    self._end(run_id, OVERTAKEN, timing)
                elif (
                    candidate is None or candidate["published"] or candidate["decision"] is not None
                ):
                    self._end(run_id, INTERRUPTED, timing)
                else:
                    displaced = self._published(str(candidate["draft_id"]))
                    self._connection.execute(
                        """
                        UPDATE runs SET status='published', ended_at=?, timing=COALESCE(?, timing)
                        WHERE thread_id=? AND status='running'
                        """,
                        (
                            self._clock.now().isoformat(),
                            None if timing is None else timing.model_dump_json(),
                            run_id,
                        ),
                    )
            settled = self._run_state(run_id, with_draft=True)
        return Settled(cast(RunState, settled), displaced)

    def _published(self, draft_id: str) -> list[Displaced]:
        """The writes of a publication, inside the caller's transaction."""
        stamp = self._clock.now().isoformat()
        row = self._connection.execute(
            "SELECT plan_date, published FROM drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
        if row is None:
            msg = f"no draft {draft_id!r} to publish"
            raise KeyError(msg)
        if row["published"]:
            return []
        self._connection.execute(
            """
            UPDATE drafts
            SET published=1,
                published_order=(SELECT COALESCE(MAX(published_order), 0) + 1 FROM drafts)
            WHERE draft_id=?
            """,
            (draft_id,),
        )
        displaced = [
            Displaced(str(found["draft_id"]), str(found["thread_id"]))
            for found in self._connection.execute(
                """
                SELECT draft_id, thread_id FROM drafts
                WHERE plan_date=? AND published=1 AND decision IS NULL AND draft_id<>?
                ORDER BY published_order
                """,
                (str(row["plan_date"]), draft_id),
            ).fetchall()
        ]
        self._connection.execute(
            """
            UPDATE drafts
            SET decision='superseded', reason=?, decided_at=?, superseded_by=?
            WHERE plan_date=? AND published=1 AND decision IS NULL AND draft_id<>?
            """,
            (SUPERSEDED_REASON, stamp, draft_id, str(row["plan_date"]), draft_id),
        )
        return displaced

    def end_run(
        self,
        run_id: str,
        *,
        reason: str,
        steps: Sequence[StepRecord] = (),
        terminal: StepRecord | None = None,
        timing: RunTiming | None = None,
        past_due: Sequence[PastDueWork] = (),
        wait: float = STORE_WAIT_SECONDS,
    ) -> RunState | None:
        """End a running run without a plan, and return the run as it stands.

        Past the deadline the run ends ``timed_out``, whatever ``reason`` says. Its
        unpublished, undecided draft is deleted in the same transaction. Steps saved
        with its draft are kept and ``terminal`` is added after them; a run that saved
        none keeps ``steps`` and then ``terminal``. A run that ends on the date problem
        keeps ``past_due``, the work its answer names, in the same transaction. A run that
        is not running is returned unchanged, and ``None`` is returned for a run the store
        never admitted.
        """
        with self._session(wait, write=True):
            run = self._run_state(run_id)
            if run is None or run.status != "running":
                return run
            (stored,) = self._connection.execute(
                "SELECT COUNT(*) FROM steps WHERE thread_id=?", (run_id,)
            ).fetchone()
            ended = TIMED_OUT if run.seconds_left <= 0 else reason
            self._end(run_id, ended, timing)
            if ended == DATE_PROBLEM and past_due:
                self._connection.execute(
                    "UPDATE runs SET past_due=? WHERE thread_id=?",
                    (PAST_DUE_WORK.dump_json(tuple(past_due)).decode(), run_id),
                )
            ending = [] if terminal is None else [terminal]
            if stored:
                self._add_steps(run_id, ending)
            else:
                self._replace_steps(run_id, [*steps, *ending])
            return self._run_state(run_id)

    def reconcile_runs(self, wait: float = STORE_WAIT_SECONDS) -> list[str]:
        """End every running run whose deadline passed, or could not be this process's.

        Returns the ids of the runs ended, each with its draft deleted.
        """
        with self._session(wait, write=True):
            return self._reconciled()

    def end_interrupted_runs(self, wait: float = STORE_WAIT_SECONDS) -> list[str]:
        """End every running run ``interrupted``, for a process starting before it serves.

        A run still running in the file belongs to a process that stopped. Returns the
        ids of the runs ended, each with its draft deleted.
        """
        with self._session(wait, write=True):
            ended = [
                str(row["thread_id"])
                for row in self._connection.execute(
                    "SELECT thread_id FROM runs WHERE status='running' ORDER BY rowid"
                ).fetchall()
            ]
            for run_id in ended:
                self._end(run_id, INTERRUPTED)
        return ended

    def run_status(
        self, run_id: str, wait: float = STORE_WAIT_SECONDS, *, reconcile: bool = True
    ) -> RunState | None:
        """Where one run stands, after ending any run whose deadline passed.

        With ``reconcile`` off it is a plain read that ends and writes nothing. A
        published run is read with its draft. ``None`` for a run the store never admitted.
        """
        with self._session(wait, write=reconcile):
            if reconcile:
                self._reconciled()
            return self._run_state(run_id, with_draft=True)

    def latest_run(self) -> RunState | None:
        """The household's newest run as it stands, without ending anything; ``None`` for none."""
        with self._session():
            row = self._connection.execute(
                RUN_STATE + "ORDER BY runs.recorded_at DESC, runs.rowid DESC"
            ).fetchone()
            return None if row is None else self._state_from(row)

    def running_threads(self) -> frozenset[str]:
        """The threads of the runs still running, whatever their deadlines."""
        with self._session():
            rows = self._connection.execute(
                "SELECT thread_id FROM runs WHERE status='running'"
            ).fetchall()
        return frozenset(str(row["thread_id"]) for row in rows)

    def _run_state(self, run_id: str, *, with_draft: bool = False) -> RunState | None:
        """One run as it stands, read by a caller holding the store."""
        row = self._connection.execute(RUN_STATE + "WHERE runs.thread_id=?", (run_id,)).fetchone()
        return None if row is None else self._state_from(row, with_draft=with_draft)

    def _state_from(self, row: sqlite3.Row, *, with_draft: bool = False) -> RunState:
        """A run's state from a row of ``RUN_STATE``, read by a caller holding the store."""
        status = cast(RunStatus, str(row["status"]))
        deadline = row["deadline_mono"]
        left = 0.0
        if status == "running" and deadline is not None:
            left = max(0.0, float(deadline) - self._monotonic())
        found = None
        if with_draft and status == "published":
            drafts = self._read_drafts(
                DRAFTS_WITH_STEPS + "WHERE drafts.thread_id=? ORDER BY steps.position",
                (str(row["thread_id"]),),
            )
            found = drafts[0] if drafts else None
        return RunState(
            run_id=str(row["thread_id"]),
            plan_date=date.fromisoformat(str(row["plan_date"])),
            status=status,
            reason=str(row["outcome"]),
            seconds_left=left,
            plan_unchanged=bool(row["plan_unchanged"]),
            draft=found,
            has_plan=bool(row["has_plan"]),
            past_due=past_due_of(row),
        )

    def _last_published(self, plan_date: str) -> int:
        """The evening's last place in the published order, or 0, inside the caller's hold."""
        (newest,) = self._connection.execute(
            "SELECT COALESCE(MAX(published_order), 0) FROM drafts "
            "WHERE plan_date=? AND published=1",
            (plan_date,),
        ).fetchone()
        return int(newest)

    def _end(self, run_id: str, reason: str, timing: RunTiming | None = None) -> None:
        """End a running run with ``reason`` and delete its undecided, unpublished draft.

        Inside the caller's transaction. A run that is not running is left as it is.
        """
        self._connection.execute(
            """
            UPDATE runs SET status='ended', outcome=?, ended_at=?, timing=COALESCE(?, timing)
            WHERE thread_id=? AND status='running'
            """,
            (
                reason,
                self._clock.now().isoformat(),
                None if timing is None else timing.model_dump_json(),
                run_id,
            ),
        )
        self._connection.execute(
            "DELETE FROM drafts WHERE thread_id=? AND published=0 AND decision IS NULL",
            (run_id,),
        )

    def _reconciled(self) -> list[str]:
        """End the running runs past their deadline, and those no deadline of this process allows.

        Inside the caller's transaction. A deadline more than ``RUN_DEADLINE_SECONDS``
        ahead was set on another process's clock, so that run ends ``interrupted``; one
        that has passed ends ``timed_out``. Returns the ids of the runs ended.
        """
        now = self._monotonic()
        rows = self._connection.execute(
            """
            SELECT thread_id, deadline_mono FROM runs
            WHERE status='running'
              AND (deadline_mono IS NULL OR deadline_mono <= ? OR deadline_mono > ?)
            ORDER BY rowid
            """,
            (now, now + RUN_DEADLINE_SECONDS),
        ).fetchall()
        for row in rows:
            deadline = row["deadline_mono"]
            passed = deadline is not None and float(deadline) <= now
            self._end(str(row["thread_id"]), TIMED_OUT if passed else INTERRUPTED)
        return [str(row["thread_id"]) for row in rows]

    def record_decision(
        self,
        draft_id: str,
        *,
        status: DraftStatus,
        decision: Decision,
        reason: str | None,
        wait: float = STORE_WAIT_SECONDS,
    ) -> DraftRecord:
        """Save what a person decided about a waiting published draft, once.

        The update applies while no decision is recorded, or when the same
        decision is recorded again, which is what a node that runs twice does;
        the first time stamp is kept on a repeat. A different decision for a
        draft that has one is refused with ``AlreadyDecided``, so two people
        deciding at once cannot overwrite each other: the row is the referee,
        and the second is told what stood. A draft that is not on the pages is
        refused with ``NotPublished``. The time is the store's clock, not the
        caller's, so every decision is stamped the same way.
        """
        stamp = self._clock.now().isoformat()
        with self._session(wait, write=True):
            row = self._connection.execute(
                "SELECT published FROM drafts WHERE draft_id=?", (draft_id,)
            ).fetchone()
            if row is None:
                msg = f"no draft {draft_id!r} to decide about"
                raise KeyError(msg)
            if not row["published"]:
                msg = f"draft {draft_id!r} is not on the pages"
                raise NotPublished(msg)
            updated = self._connection.execute(
                """
                UPDATE drafts
                SET status=?, decision=?, reason=?, decided_at=COALESCE(decided_at, ?)
                WHERE draft_id=? AND published=1
                  AND (decision IS NULL OR (decision = ? AND reason IS ?))
                """,
                (status.value, decision, reason, stamp, draft_id, decision, reason),
            ).rowcount
            (record,) = self._read_drafts(ONE_DRAFT, (draft_id,))
        if updated == 0:
            raise AlreadyDecided(record)
        return record

    def record_timing(
        self, thread_id: str, timing: RunTiming, wait: float = STORE_WAIT_SECONDS
    ) -> None:
        """Keep how long a saved run took and what it asked for. Its status is never touched.

        A thread never saved has no row, and nothing is written for it.
        """
        with self._session(wait, write=True):
            self._connection.execute(
                "UPDATE runs SET timing=? WHERE thread_id=?",
                (timing.model_dump_json(), thread_id),
            )

    def _replace_steps(self, thread_id: str, steps: Sequence[StepRecord]) -> None:
        """A run's steps, replacing any it had, inside a transaction the caller holds open."""
        self._connection.execute("DELETE FROM steps WHERE thread_id=?", (thread_id,))
        self._add_steps(thread_id, steps)

    def _add_steps(self, thread_id: str, steps: Sequence[StepRecord]) -> None:
        """Steps added after those a run has, inside a transaction the caller holds open."""
        (after,) = self._connection.execute(
            "SELECT COALESCE(MAX(position) + 1, 0) FROM steps WHERE thread_id=?", (thread_id,)
        ).fetchone()
        self._connection.executemany(
            """
            INSERT INTO steps (thread_id, position, node, round, expected, found, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    thread_id,
                    position,
                    item.node,
                    item.round,
                    item.expected,
                    item.found,
                    item.recorded_at.isoformat(),
                )
                for position, item in enumerate(steps, start=int(after))
            ],
        )

    def steps_for(self, thread_id: str) -> list[StepRecord]:
        """The steps of one run, in the order they happened; empty for a thread never saved."""
        with self._session():
            rows = self._connection.execute(
                "SELECT * FROM steps WHERE thread_id=? ORDER BY position", (thread_id,)
            ).fetchall()
        return [step_from(row) for row in rows]

    def runs_without_a_draft(self) -> list[RunRecord]:
        """Runs that ended before the gate, most recent first, each with its steps, and
        whether each is the newest run of its evening.

        One query joins the runs to their steps, so every record is assembled
        from a single snapshot and a replacement landing between two reads
        cannot pair one run's outcome with another's steps. Newest is read in
        the same query, against every run of the evening, with a draft or
        without. Two saved at one instant are told apart by the order they were
        admitted, both for the listing and for which is newest.

        A run still running within its deadline is in neither the listing nor the
        comparison, so a run in progress neither shows as ended nor makes another
        old. One past its deadline has no draft that could still publish, and is
        listed ``timed_out``.
        """
        with self._session():
            now = self._monotonic()
            rows = self._connection.execute(
                """
                SELECT runs.thread_id, runs.plan_date,
                       CASE WHEN runs.status = 'running' THEN ? ELSE runs.outcome END
                           AS outcome,
                       runs.recorded_at, runs.timing,
                       NOT EXISTS (
                           SELECT 1 FROM runs AS later
                           WHERE later.plan_date = runs.plan_date
                             AND NOT (later.status = 'running' AND later.deadline_mono > ?)
                             AND (later.recorded_at > runs.recorded_at
                                  OR (later.recorded_at = runs.recorded_at
                                      AND later.rowid > runs.rowid))
                       ) AS newest,
                       steps.node, steps.round, steps.expected, steps.found,
                       steps.recorded_at AS step_recorded_at
                FROM runs LEFT JOIN steps ON steps.thread_id = runs.thread_id
                WHERE runs.thread_id NOT IN (SELECT thread_id FROM drafts)
                  AND NOT (runs.status = 'running' AND runs.deadline_mono > ?)
                ORDER BY runs.recorded_at DESC, runs.rowid DESC, steps.position
                """,
                (TIMED_OUT, now, now),
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
                newest=bool(row["newest"]),
                timing=None
                if row["timing"] is None
                else RunTiming.model_validate_json(str(row["timing"])),
            )
            for thread_id, (row, steps) in grouped.items()
        ]

    def get(self, draft_id: str) -> DraftRecord | None:
        """One draft by id with its run's record, or ``None``."""
        found = self._drafts(ONE_DRAFT, (draft_id,))
        return found[0] if found else None

    def waiting(self) -> list[DraftRecord]:
        """Every published draft no person has decided about, oldest first."""
        return self._drafts(WAITING_DRAFTS, ())

    def unpublished(self) -> list[DraftRecord]:
        """Every undecided draft saved by a run that has not settled, oldest first.

        Each belongs to the run in progress: a run that ends deletes its draft.
        """
        return self._drafts(UNPUBLISHED_DRAFTS, ())

    def decided(self) -> list[DraftRecord]:
        """Every draft a person has decided about, most recent decision first."""
        return self._drafts(DECIDED_DRAFTS, ())

    def review_snapshot(self, today: date) -> ReviewSnapshot:
        """What waits, what was decided, and the day's working plan, from one read.

        One statement reads every draft with its steps, under the store's
        lock, and the answers are worked out from those rows in memory by
        the rules the separate reads follow: waiting is published and
        undecided, oldest first; decided is most recent decision first; an
        evening's plan is the last in the published order for that evening
        that was not superseded, the day's and each later evening's alike.
        A publication or a decision from anywhere lands wholly before this
        reading or wholly after it, so a page built from it agrees with
        itself, which separate reads one after another cannot promise.
        """
        with self._session():
            rows = self._connection.execute(EVERY_DRAFT).fetchall()
        grouped: dict[str, tuple[sqlite3.Row, list[StepRecord]]] = {}
        for row in rows:
            _, steps = grouped.setdefault(str(row["draft_id"]), (row, []))
            if row["node"] is not None:
                steps.append(joined_step_from(row))
        records = {name: record_from(row, steps) for name, (row, steps) in grouped.items()}
        placed = {name: row["published_order"] for name, (row, _) in grouped.items()}
        waiting = sorted(
            (item for item in records.values() if item.published and item.decision is None),
            key=lambda item: (item.created_at, item.draft_id),
        )
        decided = sorted(
            (item for item in records.values() if item.decision is not None),
            key=lambda item: item.draft_id,
        )
        decided.sort(key=lambda item: item.decided_at or item.created_at, reverse=True)
        ahead: dict[date, list[DraftRecord]] = {}
        for item in records.values():
            if item.plan_date >= today and item.published and item.decision != "superseded":
                ahead.setdefault(item.plan_date, []).append(item)
        latest = {
            evening: max(found, key=lambda item: placed[item.draft_id] or 0)
            for evening, found in ahead.items()
        }
        published = [name for name, place in placed.items() if records[name].published and place]
        return ReviewSnapshot(
            waiting=tuple(waiting),
            decided=tuple(decided),
            current_id=latest[today].draft_id if today in latest else None,
            operative=frozenset(item.draft_id for item in latest.values()),
            newest=max(published, key=lambda name: placed[name]) if published else "",
        )

    def latest_for(self, plan_date: date) -> DraftRecord | None:
        """The last draft published for one evening that no later one displaced; ``None`` when none.

        Her page shows the evening's latest plan whatever a parent has said
        about it, since the plan is hers from the moment it is made. Latest is
        by the published order, the order supersession follows, never by a
        clock, and an unpublished draft is not in the running: its run has not
        settled, and may yet end without a plan. A superseded draft is never the latest, since
        another took its place.
        """
        found = self._drafts(LATEST_FOR_EVENING, (plan_date.isoformat(),))
        return found[0] if found else None

    def _drafts(self, query: str, parameters: tuple[str, ...]) -> list[DraftRecord]:
        """Drafts and their steps from one query, so each record is one snapshot."""
        with self._session():
            return self._read_drafts(query, parameters)

    def _read_drafts(self, query: str, parameters: tuple[str, ...]) -> list[DraftRecord]:
        """``_drafts`` for a caller already holding the store."""
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
           drafts.decision, drafts.reason, drafts.too_much, drafts.inputs_digest, drafts.published,
           drafts.plan_assignment_ids, drafts.plan_snapshot,
           steps.node, steps.round, steps.expected, steps.found,
           steps.recorded_at AS step_recorded_at
    FROM drafts LEFT JOIN steps ON steps.thread_id = drafts.thread_id
"""
"""Every read of a draft starts here, so the steps come from the same snapshot."""

EVERY_DRAFT = """
    SELECT drafts.draft_id, drafts.thread_id, drafts.plan_date, drafts.status,
           drafts.outcome, drafts.body, drafts.created_at, drafts.decided_at,
           drafts.decision, drafts.reason, drafts.too_much, drafts.inputs_digest, drafts.published,
           drafts.plan_assignment_ids, drafts.plan_snapshot, drafts.published_order,
           steps.node, steps.round, steps.expected, steps.found,
           steps.recorded_at AS step_recorded_at
    FROM drafts LEFT JOIN steps ON steps.thread_id = drafts.thread_id
    ORDER BY drafts.draft_id, steps.position
"""
"""Every draft with its steps and its place in the published order, in one statement, for
a page that shows several groups of drafts and must not read them apart."""

ONE_DRAFT = DRAFTS_WITH_STEPS + "WHERE drafts.draft_id=? ORDER BY steps.position"
WAITING_DRAFTS = (
    DRAFTS_WITH_STEPS
    + "WHERE drafts.decision IS NULL AND drafts.published=1 "
    + "ORDER BY drafts.created_at, drafts.draft_id, steps.position"
)
UNPUBLISHED_DRAFTS = (
    DRAFTS_WITH_STEPS
    + "WHERE drafts.decision IS NULL AND drafts.published=0 "
    + "ORDER BY drafts.created_at, drafts.draft_id, steps.position"
)
DECIDED_DRAFTS = (
    DRAFTS_WITH_STEPS
    + "WHERE drafts.decision IS NOT NULL "
    + "ORDER BY drafts.decided_at DESC, drafts.draft_id, steps.position"
)
LATEST_FOR_EVENING = (
    DRAFTS_WITH_STEPS
    + "WHERE drafts.plan_date=? AND drafts.published=1 "
    + "AND (drafts.decision IS NULL OR drafts.decision<>'superseded') "
    + "ORDER BY drafts.published_order DESC, steps.position"
)
"""The last draft published for the evening that was not displaced: the same
order supersession follows, so the plan her page shows is the plan the queue holds."""


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
        inputs_digest=None if row["inputs_digest"] is None else str(row["inputs_digest"]),
        published=bool(row["published"]),
        plan_assignment_ids=None
        if row["plan_assignment_ids"] is None
        else [str(name) for name in json.loads(str(row["plan_assignment_ids"]))],
        plan_snapshot=None if row["plan_snapshot"] is None else str(row["plan_snapshot"]),
    )


def past_due_of(row: sqlite3.Row) -> tuple[PastDueWork, ...] | None:
    """The past-due work a row of ``RUN_STATE`` kept; ``None`` for none, and for kept work
    that can't be read, which is logged."""
    kept = row["past_due"]
    if kept is None:
        return None
    try:
        return PAST_DUE_WORK.validate_json(str(kept))
    except ValueError as error:
        logger.warning(
            "the past-due work of run %s could not be read: %s",
            row["thread_id"],
            type(error).__name__,
        )
        return None


RUN_LIFECYCLE_COLUMNS: Final = (
    ("status", "TEXT"),
    ("deadline_mono", "REAL"),
    ("base_order", "INTEGER"),
    ("ended_at", "TEXT"),
)
"""The columns that carry a run's lifecycle, added on open to a file whose runs lack them."""

RUN_STATE = """
    SELECT runs.thread_id, runs.plan_date, runs.status, runs.outcome, runs.deadline_mono,
           runs.past_due,
           runs.base_order IS (
               SELECT COALESCE(MAX(drafts.published_order), 0) FROM drafts
               WHERE drafts.plan_date = runs.plan_date AND drafts.published = 1
           ) AS plan_unchanged,
           EXISTS (
               SELECT 1 FROM drafts
               WHERE drafts.plan_date = runs.plan_date AND drafts.published = 1
           ) AS has_plan
    FROM runs
"""
"""Every read of a run's state starts here."""


class _Session:
    """When one call's waits end, on ``time.monotonic``: its own, and its commit's."""

    def __init__(self, ends: float) -> None:
        self.ends = ends
        self.commit_ends = ends
