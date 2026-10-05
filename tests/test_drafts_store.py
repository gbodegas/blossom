# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The drafts table: the record of what waited at the gate and what was decided.

The store is written twice per draft by nodes that may run twice, so the
tests here are mostly about idempotence and durability: the same draft saved
again is one row, a decision is stamped by the store's clock, and the rows are
still there after the file is closed and reopened.
"""

import logging
import pathlib
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from time import monotonic
from zoneinfo import ZoneInfo

import pytest

from blossom.agent.runs import RUN_DEADLINE_SECONDS
from blossom.agent.steps import RunTiming, StageTime, StepRecord
from blossom.clock import Clock
from blossom.drafts import Draft, DraftStatus
from blossom.stores.drafts import (
    INTERRUPTED,
    OVERTAKEN,
    SUPERSEDED_REASON,
    TIMED_OUT,
    AlreadyDecided,
    DraftsStore,
    IncompatibleReplay,
    NotPublished,
    Outcome,
    RunEnded,
    StoreBusy,
    WriterBusy,
)
from blossom.stores.paths import UnsafeCheckpointPath
from tests.support import FIXTURE_TIMEZONE, FakeTime, ended_run, fixture_clock, settled_run

PLAN_DATE = date(2026, 8, 19)
CREATED = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)


def save_and_publish(
    store: DraftsStore,
    draft: Draft,
    *,
    thread_id: str,
    plan_date: date,
    outcome: Outcome,
    steps: Sequence[StepRecord] = (),
    too_much: bool = False,
    plan_assignment_ids: Sequence[str] | None = None,
) -> None:
    """Admit a run, save its draft and settle it, as a run that ends well does."""
    settled_run(
        store,
        draft,
        thread_id=thread_id,
        plan_date=plan_date,
        outcome=outcome,
        steps=steps,
        too_much=too_much,
        plan_assignment_ids=plan_assignment_ids,
    )


def admitted(
    store: DraftsStore,
    thread_id: str,
    plan_date: date = PLAN_DATE,
    *,
    now: Callable[[], float] = monotonic,
    seconds: float = RUN_DEADLINE_SECONDS,
) -> None:
    """Admit a run whose deadline is ``seconds`` ahead on ``now``, the store's own clock."""
    blocking = store.admit_run(thread_id, plan_date=plan_date, deadline_mono=now() + seconds)
    assert blocking is None


def store_in_memory() -> DraftsStore:
    return DraftsStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())


def draft(body: str = "Plan for Wednesday, August 19") -> Draft:
    return Draft(draft_id="draft:plan:2026-08-19:abc12345", body=body, created_at=CREATED)


def test_a_saved_draft_is_waiting_until_somebody_decides() -> None:
    store = store_in_memory()
    try:
        save_and_publish(
            store,
            draft(),
            thread_id="plan:2026-08-19:abc12345",
            plan_date=PLAN_DATE,
            outcome="accepted",
        )
        waiting = store.waiting()
    finally:
        store.close()

    assert [record.draft_id for record in waiting] == ["draft:plan:2026-08-19:abc12345"]
    record = waiting[0]
    assert record.waiting
    assert record.status is DraftStatus.DRAFT
    assert record.outcome == "accepted"
    assert record.plan_date == PLAN_DATE
    assert record.created_at == CREATED
    assert record.decision is None


def test_saving_the_same_draft_again_leaves_one_row_with_the_first_created_at() -> None:
    """A node that runs twice before its run settles, as a crashed node does, must not queue
    twice: the later composition takes the place of the earlier one, whole, and the draft
    keeps the time it was first made."""
    store = store_in_memory()
    try:
        admitted(store, "t")
        store.record_waiting(
            draft("first rendering"), thread_id="t", plan_date=PLAN_DATE, outcome="accepted"
        )
        later = draft("second rendering").model_copy(
            update={"created_at": CREATED.replace(hour=23)}
        )
        store.record_waiting(later, thread_id="t", plan_date=PLAN_DATE, outcome="unsettled")
        store.settle_run("t")
        waiting = store.waiting()
    finally:
        store.close()

    assert len(waiting) == 1
    assert waiting[0].body == "second rendering"
    assert waiting[0].outcome == "unsettled"
    assert waiting[0].created_at == CREATED


def test_a_decision_is_recorded_with_the_stores_clock_and_leaves_the_queue() -> None:
    store = store_in_memory()
    try:
        save_and_publish(store, draft(), thread_id="t", plan_date=PLAN_DATE, outcome="accepted")
        decided = store.record_decision(
            "draft:plan:2026-08-19:abc12345",
            status=DraftStatus.APPROVED_FOR_MANUAL_SEND,
            decision="approved",
            reason="looks right",
        )
        still_waiting = store.waiting()
        history = store.decided()
    finally:
        store.close()

    assert not decided.waiting
    assert decided.status is DraftStatus.APPROVED_FOR_MANUAL_SEND
    assert decided.decision == "approved"
    assert decided.reason == "looks right"
    assert decided.decided_at == fixture_clock().now()
    assert still_waiting == []
    assert history == [decided]


def test_a_refusal_keeps_the_draft_a_draft() -> None:
    store = store_in_memory()
    try:
        save_and_publish(store, draft(), thread_id="t", plan_date=PLAN_DATE, outcome="unsettled")
        refused = store.record_decision(
            "draft:plan:2026-08-19:abc12345",
            status=DraftStatus.DRAFT,
            decision="rejected",
            reason="too late in the evening",
        )
    finally:
        store.close()

    assert refused.status is DraftStatus.DRAFT
    assert refused.decision == "rejected"


class Ticking:
    """A clock that moves a minute every time it is read, so two stamps differ."""

    def __init__(self) -> None:
        self._at = CREATED
        self.zone = ZoneInfo(FIXTURE_TIMEZONE)

    def now(self) -> datetime:
        self._at += timedelta(minutes=1)
        return self._at

    def today(self) -> date:
        return self._at.astimezone(self.zone).date()


def ticking_store() -> DraftsStore:
    clock: Clock = Ticking()
    return DraftsStore(sqlite3.connect(":memory:", check_same_thread=False), clock)


def test_a_different_decision_on_a_decided_draft_is_refused_and_the_first_stands() -> None:
    """Two people deciding at once: the row is the referee, and the second is told."""
    store = ticking_store()
    try:
        save_and_publish(store, draft(), thread_id="t", plan_date=PLAN_DATE, outcome="accepted")
        first = store.record_decision(
            "draft:plan:2026-08-19:abc12345",
            status=DraftStatus.APPROVED_FOR_MANUAL_SEND,
            decision="approved",
            reason="first",
        )
        with pytest.raises(AlreadyDecided, match="already approved") as refused:
            store.record_decision(
                "draft:plan:2026-08-19:abc12345",
                status=DraftStatus.DRAFT,
                decision="rejected",
                reason="second",
            )
        standing = store.get("draft:plan:2026-08-19:abc12345")
    finally:
        store.close()

    assert standing == first
    assert refused.value.record == first
    assert standing is not None
    assert standing.decision == "approved"
    assert standing.reason == "first"


def test_the_same_decision_recorded_again_keeps_its_first_time() -> None:
    """A node that runs twice records the same decision twice; the row does not move."""
    store = ticking_store()
    try:
        save_and_publish(store, draft(), thread_id="t", plan_date=PLAN_DATE, outcome="accepted")
        first = store.record_decision(
            "draft:plan:2026-08-19:abc12345",
            status=DraftStatus.APPROVED_FOR_MANUAL_SEND,
            decision="approved",
            reason="looks right",
        )
        again = store.record_decision(
            "draft:plan:2026-08-19:abc12345",
            status=DraftStatus.APPROVED_FOR_MANUAL_SEND,
            decision="approved",
            reason="looks right",
        )
    finally:
        store.close()

    assert again == first
    assert again.decided_at == first.decided_at


def test_deciding_about_an_unknown_draft_is_an_error_not_a_row() -> None:
    store = store_in_memory()
    try:
        with pytest.raises(KeyError, match="no draft"):
            store.record_decision(
                "draft:nobody", status=DraftStatus.DRAFT, decision="rejected", reason=None
            )
        assert store.get("draft:nobody") is None
    finally:
        store.close()


def test_the_queue_is_oldest_first() -> None:
    store = store_in_memory()
    try:
        newer = Draft(draft_id="draft:b", body="b", created_at=CREATED.replace(hour=23))
        older = Draft(draft_id="draft:a", body="a", created_at=CREATED)
        next_evening = PLAN_DATE + timedelta(days=1)
        save_and_publish(store, newer, thread_id="tb", plan_date=next_evening, outcome="accepted")
        save_and_publish(store, older, thread_id="ta", plan_date=PLAN_DATE, outcome="accepted")
        order = [record.draft_id for record in store.waiting()]
    finally:
        store.close()

    assert order == ["draft:a", "draft:b"]


def test_a_decided_draft_is_not_taken_back() -> None:
    store = store_in_memory()
    try:
        save_and_publish(store, draft(), thread_id="t", plan_date=PLAN_DATE, outcome="accepted")
        store.record_decision(
            draft().draft_id, status=DraftStatus.DRAFT, decision="rejected", reason="no"
        )
        ended = store.end_run("t", reason=INTERRUPTED)
        kept = store.get(draft().draft_id)
    finally:
        store.close()

    assert ended is not None
    assert ended.status == "published"
    assert kept is not None
    assert kept.decision == "rejected"


def test_rows_survive_closing_and_reopening_the_file(tmp_path: pathlib.Path) -> None:
    """The queue is a record, so a restart must not empty it."""
    path = tmp_path / "state" / "blossom.sqlite3"

    first = DraftsStore.open(path, fixture_clock())
    try:
        save_and_publish(first, draft(), thread_id="t", plan_date=PLAN_DATE, outcome="accepted")
    finally:
        first.close()

    second = DraftsStore.open(path, fixture_clock())
    try:
        revived = second.get("draft:plan:2026-08-19:abc12345")
        with sqlite3.connect(path) as connection:
            secure_delete = connection.execute("PRAGMA secure_delete").fetchone()
    finally:
        second.close()

    assert revived is not None
    assert revived.body == "Plan for Wednesday, August 19"
    assert revived.created_at == CREATED
    assert secure_delete is not None


def test_the_file_is_refused_where_the_saved_state_store_refuses_it(
    tmp_path: pathlib.Path,
) -> None:
    """Same guard, same reason: a refused draft is still text about her."""
    with pytest.raises(UnsafeCheckpointPath):
        DraftsStore.open(tmp_path / "OneDrive" / "blossom.sqlite3", fixture_clock())


# ------------------------------------------------------------- the run's record


def step(node: str, round_number: int, found: str = "as expected") -> StepRecord:
    return StepRecord(
        node=node, round=round_number, expected="something", found=found, recorded_at=CREATED
    )


def test_a_runs_record_is_saved_with_its_steps_and_read_back_in_order() -> None:
    store = store_in_memory()
    steps = [step("retrieve", 0), step("plan", 1), step("verify", 1, "1 of 7 checks failed")]

    ended_run(
        store,
        thread_id="plan:2026-08-19:x",
        plan_date=PLAN_DATE,
        outcome="checks_failed",
        steps=steps,
    )

    assert store.steps_for("plan:2026-08-19:x") == steps
    runs = store.runs_without_a_draft()
    assert [(run.thread_id, run.outcome) for run in runs] == [
        ("plan:2026-08-19:x", "checks_failed")
    ]
    assert runs[0].steps == steps
    assert runs[0].plan_date == PLAN_DATE
    assert runs[0].recorded_at == fixture_clock().now()


def test_ending_a_run_again_changes_nothing_and_keeps_its_first_account() -> None:
    """A run ends once: a second ending, from a late worker or a repeated call, is told how
    the run ended and leaves its reason, its steps and its time as they were."""
    store = ticking_store()
    ended_run(
        store,
        thread_id="plan:x",
        plan_date=PLAN_DATE,
        outcome="model_refused",
        steps=[step("plan", 1)],
    )
    first_time = store.runs_without_a_draft()[0].recorded_at

    again = store.end_run(
        "plan:x", reason="checks_failed", steps=[step("retrieve", 0), step("plan", 1)]
    )

    saved = store.runs_without_a_draft()
    assert again is not None
    assert (again.status, again.reason) == ("ended", "model_refused")
    assert [item.node for item in store.steps_for("plan:x")] == ["plan"]
    assert [(run.outcome, run.recorded_at) for run in saved] == [("model_refused", first_time)]


def test_a_run_that_left_a_draft_is_not_among_those_that_ended_without_one() -> None:
    store = store_in_memory()
    save_and_publish(
        store,
        draft(),
        thread_id="plan:y",
        plan_date=PLAN_DATE,
        outcome="accepted",
        steps=[step("critique", 1)],
    )
    ended = store.end_run("plan:y", reason="accepted", steps=[step("plan", 1)])

    assert ended is not None
    assert ended.status == "published"
    assert store.runs_without_a_draft() == []
    assert [item.node for item in store.steps_for("plan:y")] == ["critique"]


def test_a_thread_never_saved_has_no_steps() -> None:
    assert store_in_memory().steps_for("plan:nobody") == []


def test_runs_that_ended_are_most_recent_first() -> None:
    store = ticking_store()
    ended_run(store, thread_id="plan:first", plan_date=PLAN_DATE, outcome="checks_failed")
    ended_run(store, thread_id="plan:second", plan_date=PLAN_DATE, outcome="model_refused")

    assert [run.thread_id for run in store.runs_without_a_draft()] == ["plan:second", "plan:first"]


def test_runs_saved_at_one_instant_are_listed_most_recent_first_by_the_order_saved() -> None:
    """Thread ids are random, so two runs stamped alike are listed by the order they were
    admitted, the same order that says which one is newest."""
    store = store_in_memory()
    ended_run(store, thread_id="plan:a", plan_date=PLAN_DATE, outcome="checks_failed")
    ended_run(store, thread_id="plan:z", plan_date=PLAN_DATE, outcome="checks_failed")

    runs = store.runs_without_a_draft()

    assert [(run.thread_id, run.newest) for run in runs] == [
        ("plan:z", True),
        ("plan:a", False),
    ]


def test_a_run_that_ended_knows_whether_it_is_the_newest_for_its_evening() -> None:
    """A later run of the same evening, with a plan or without, makes an earlier one old;
    a run of another evening does not."""
    store = ticking_store()
    tomorrow = PLAN_DATE + timedelta(days=1)
    yesterday = PLAN_DATE - timedelta(days=1)
    ended_run(store, thread_id="plan:a", plan_date=PLAN_DATE, outcome="checks_failed")
    ended_run(store, thread_id="plan:b", plan_date=PLAN_DATE, outcome="model_refused")
    ended_run(store, thread_id="plan:c", plan_date=tomorrow, outcome="checks_failed")
    save_and_publish(store, draft(), thread_id="plan:d", plan_date=tomorrow, outcome="accepted")
    ended_run(store, thread_id="plan:e", plan_date=yesterday, outcome="checks_failed")

    newest = {run.thread_id: run.newest for run in store.runs_without_a_draft()}

    assert newest == {"plan:a": False, "plan:b": True, "plan:c": False, "plan:e": True}


def test_two_runs_of_an_evening_saved_at_one_instant_leave_the_later_saved_newest() -> None:
    """Thread ids are random, so the order they sort in says nothing about which run came
    last; the order the runs were admitted does, and ending one again keeps its place."""
    store = store_in_memory()
    ended_run(store, thread_id="plan:z", plan_date=PLAN_DATE, outcome="checks_failed")
    ended_run(store, thread_id="plan:a", plan_date=PLAN_DATE, outcome="checks_failed")
    store.end_run("plan:z", reason="model_refused")

    newest = {run.thread_id: run.newest for run in store.runs_without_a_draft()}

    assert newest == {"plan:z": False, "plan:a": True}


def test_a_draft_and_the_record_of_its_run_are_saved_together() -> None:
    store = store_in_memory()

    save_and_publish(
        store,
        draft(),
        thread_id="plan:z",
        plan_date=PLAN_DATE,
        outcome="accepted",
        steps=[step("retrieve", 0), step("critique", 1)],
    )

    assert [item.node for item in store.steps_for("plan:z")] == ["retrieve", "critique"]
    assert store.runs_without_a_draft() == []


def test_an_ending_that_fails_part_way_leaves_the_run_as_it_was() -> None:
    """The failure lands inside the transaction, after the run is marked ended, where a
    missing rollback would show: the run is still running, its draft and its steps stand."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    store = DraftsStore(connection, fixture_clock())
    first = [step("retrieve", 0), step("plan", 1)]
    admitted(store, "plan:x")
    store.record_waiting(
        draft(), thread_id="plan:x", plan_date=PLAN_DATE, outcome="accepted", steps=first
    )
    connection.execute(
        """
        CREATE TRIGGER refuse_a_third_step BEFORE INSERT ON steps
        WHEN NEW.position = 2 BEGIN SELECT RAISE(ABORT, 'no third step'); END
        """
    )

    with pytest.raises(sqlite3.DatabaseError, match="no third step"):
        store.end_run("plan:x", reason="service_failed", terminal=step("verify", 1))

    standing = store.run_status("plan:x")
    assert standing is not None
    assert standing.status == "running"
    assert store.steps_for("plan:x") == first
    assert [record.draft_id for record in store.unpublished()] == [draft().draft_id]
    connection.execute("DROP TRIGGER refuse_a_third_step")
    ended = store.end_run("plan:x", reason="service_failed", terminal=step("verify", 1))
    assert ended is not None
    assert (ended.status, ended.reason) == ("ended", "service_failed")
    assert [item.node for item in store.steps_for("plan:x")] == ["retrieve", "plan", "verify"]
    assert store.unpublished() == []


def test_the_retention_policy_covers_the_runs_and_their_steps() -> None:
    policy = DraftsStore.retention_policy

    assert "draft" in policy
    assert "decision" in policy
    assert "record of the run" in policy
    assert "produced no draft" in policy


def test_a_run_with_no_steps_is_listed_with_an_empty_record() -> None:
    store = store_in_memory()
    ended_run(store, thread_id="plan:bare", plan_date=PLAN_DATE, outcome="model_refused")

    assert [(run.thread_id, run.steps) for run in store.runs_without_a_draft()] == [
        ("plan:bare", [])
    ]


def test_each_listed_run_carries_only_its_own_steps() -> None:
    store = ticking_store()
    ended_run(
        store,
        thread_id="plan:one",
        plan_date=PLAN_DATE,
        outcome="checks_failed",
        steps=[step("plan", 1)],
    )
    ended_run(
        store,
        thread_id="plan:two",
        plan_date=PLAN_DATE,
        outcome="model_refused",
        steps=[step("retrieve", 0), step("plan", 1)],
    )

    listed = store.runs_without_a_draft()

    assert [(run.thread_id, len(run.steps)) for run in listed] == [("plan:two", 2), ("plan:one", 1)]
    assert [item.node for item in listed[0].steps] == ["retrieve", "plan"]


def test_a_draft_is_read_with_its_steps_from_one_query() -> None:
    store = store_in_memory()
    steps = [step("retrieve", 0), step("plan", 1), step("verify", 1), step("critique", 1)]
    save_and_publish(
        store, draft(), thread_id="plan:one", plan_date=PLAN_DATE, outcome="accepted", steps=steps
    )
    other = draft().model_copy(update={"draft_id": "draft:plan:two"})
    save_and_publish(
        store,
        other,
        thread_id="plan:two",
        plan_date=PLAN_DATE + timedelta(days=1),
        outcome="unsettled",
    )

    fetched = store.get(draft().draft_id)
    queue = store.waiting()

    assert fetched is not None
    assert fetched.steps == steps
    assert [(record.draft_id, len(record.steps)) for record in queue] == [
        (draft().draft_id, 4),
        ("draft:plan:two", 0),
    ]
    decided = store.record_decision(
        draft().draft_id,
        status=DraftStatus.APPROVED_FOR_MANUAL_SEND,
        decision="approved",
        reason=None,
    )
    assert decided.steps == steps
    assert store.decided()[0].steps == steps


def test_an_older_file_with_two_drafts_waiting_for_one_evening_keeps_the_latest() -> None:
    """Opening the file brings every evening to one waiting draft, the one her page would show."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.execute(
        """
        CREATE TABLE drafts (
            draft_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE, plan_date TEXT NOT NULL,
            status TEXT NOT NULL, outcome TEXT NOT NULL, body TEXT NOT NULL,
            created_at TEXT NOT NULL, decided_at TEXT, decision TEXT, reason TEXT
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO drafts
        VALUES (?, ?, '2026-08-19', 'DRAFT', 'accepted', ?, ?, NULL, NULL, NULL)
        """,
        [
            ("draft:early", "plan:early", "early", "2026-08-19T20:00:00+00:00"),
            ("draft:late", "plan:late", "late", "2026-08-19T22:00:00+00:00"),
            ("draft:middle", "plan:middle", "middle", "2026-08-19T21:00:00+00:00"),
        ],
    )
    connection.execute(
        """
        INSERT INTO drafts VALUES (
            'draft:other', 'plan:other', '2026-08-20', 'DRAFT', 'accepted', 'other',
            '2026-08-20T20:00:00+00:00', NULL, NULL, NULL
        )
        """
    )
    connection.commit()

    store = DraftsStore(connection, fixture_clock())
    waiting = [record.draft_id for record in store.waiting()]
    early = store.get("draft:early")
    middle = store.get("draft:middle")
    latest = store.latest_for(PLAN_DATE)

    assert waiting == ["draft:late", "draft:other"]
    assert early is not None
    assert early.decision == "superseded"
    assert early.reason == SUPERSEDED_REASON
    assert early.decided_at == fixture_clock().now()
    assert middle is not None
    assert middle.decision == "superseded"
    assert latest is not None
    assert latest.draft_id == "draft:late"


def test_a_file_from_the_previous_version_is_brought_into_line_behind_a_reviewed_draft() -> None:
    """The schema with the signal column but not the two newer ones, holding two waiting drafts
    behind one a parent already reviewed: the reviewed one is the evening's latest, so both
    waiting drafts close as superseded by it, and her page and the queue agree."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.execute(
        """
        CREATE TABLE drafts (
            draft_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE, plan_date TEXT NOT NULL,
            status TEXT NOT NULL, outcome TEXT NOT NULL, body TEXT NOT NULL,
            created_at TEXT NOT NULL, decided_at TEXT, decision TEXT, reason TEXT,
            too_much INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO drafts VALUES (?, ?, '2026-08-19', 'DRAFT', 'accepted', ?, ?, ?, ?, ?, 0)
        """,
        [
            ("draft:a", "plan:a", "a", "2026-08-19T20:00:00+00:00", None, None, None),
            ("draft:b", "plan:b", "b", "2026-08-19T21:00:00+00:00", None, None, None),
            (
                "draft:c",
                "plan:c",
                "c",
                "2026-08-19T22:00:00+00:00",
                "2026-08-19T22:30:00+00:00",
                "rejected",
                "too late",
            ),
        ],
    )
    connection.commit()

    store = DraftsStore(connection, fixture_clock())
    latest = store.latest_for(PLAN_DATE)
    closed = {record.draft_id: record.decision for record in store.decided()}

    assert store.waiting() == []
    assert latest is not None
    assert latest.draft_id == "draft:c"
    assert closed == {"draft:a": "superseded", "draft:b": "superseded", "draft:c": "rejected"}


def test_a_file_with_a_superseded_draft_numbered_above_the_waiting_one_keeps_the_waiting_one() -> (
    None
):
    """A superseded draft is never the evening's latest, on open as on her page."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.execute(
        """
        CREATE TABLE drafts (
            draft_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE, plan_date TEXT NOT NULL,
            status TEXT NOT NULL, outcome TEXT NOT NULL, body TEXT NOT NULL,
            created_at TEXT NOT NULL, decided_at TEXT, decision TEXT, reason TEXT,
            too_much INTEGER NOT NULL DEFAULT 0, superseded_by TEXT
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO drafts VALUES (?, ?, '2026-08-19', 'DRAFT', 'accepted', ?, ?, ?, ?, ?, 0, ?)
        """,
        [
            (
                "draft:a",
                "plan:a",
                "a",
                "2026-08-19T22:00:00+00:00",
                "2026-08-19T22:05:00+00:00",
                "superseded",
                "a later plan for the evening took its place",
                "draft:b",
            ),
            ("draft:b", "plan:b", "b", "2026-08-19T21:00:00+00:00", None, None, None, None),
        ],
    )
    connection.commit()

    store = DraftsStore(connection, fixture_clock())
    waiting = [record.draft_id for record in store.waiting()]
    latest = store.latest_for(PLAN_DATE)

    assert waiting == ["draft:b"]
    assert latest is not None
    assert latest.draft_id == "draft:b"


def test_a_file_left_half_opened_is_finished_by_the_next_open() -> None:
    """The saved-order column exists but nothing was numbered and duplicates still wait."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.execute(
        """
        CREATE TABLE drafts (
            draft_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE, plan_date TEXT NOT NULL,
            status TEXT NOT NULL, outcome TEXT NOT NULL, body TEXT NOT NULL,
            created_at TEXT NOT NULL, decided_at TEXT, decision TEXT, reason TEXT,
            too_much INTEGER NOT NULL DEFAULT 0, saved_order INTEGER
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO drafts VALUES
        (?, ?, '2026-08-19', 'DRAFT', 'accepted', ?, ?, NULL, NULL, NULL, 0, NULL)
        """,
        [
            ("draft:a", "plan:a", "a", "2026-08-19T20:00:00+00:00"),
            ("draft:b", "plan:b", "b", "2026-08-19T21:00:00+00:00"),
        ],
    )
    connection.commit()

    store = DraftsStore(connection, fixture_clock())
    waiting = [record.draft_id for record in store.waiting()]
    save_and_publish(store, draft(), thread_id="plan:new", plan_date=PLAN_DATE, outcome="accepted")
    latest = store.latest_for(PLAN_DATE)

    assert waiting == ["draft:b"]
    assert latest is not None
    assert latest.draft_id == draft().draft_id


def test_a_file_written_before_drafts_carried_the_signal_gains_the_column() -> None:
    """Every draft in such a file was made for a full evening."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.execute(
        """
        CREATE TABLE drafts (
            draft_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE, plan_date TEXT NOT NULL,
            status TEXT NOT NULL, outcome TEXT NOT NULL, body TEXT NOT NULL,
            created_at TEXT NOT NULL, decided_at TEXT, decision TEXT, reason TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO drafts VALUES (
            'draft:old', 'plan:old', '2026-08-19', 'DRAFT', 'accepted', 'Plan',
            '2026-08-19T22:00:00+00:00', NULL, NULL, NULL
        )
        """
    )
    connection.commit()

    store = DraftsStore(connection, fixture_clock())
    old = store.get("draft:old")
    save_and_publish(
        store, draft(), thread_id="plan:new", plan_date=PLAN_DATE, outcome="accepted", too_much=True
    )
    new = store.get(draft().draft_id)
    displaced = store.get("draft:old")

    assert old is not None
    assert old.too_much is False
    assert old.published is True
    assert new is not None
    assert new.too_much is True
    assert displaced is not None
    assert displaced.decision == "superseded"


def test_a_runs_time_is_kept_with_its_record() -> None:
    timing = RunTiming(
        seconds=41.25,
        stages=[StageTime(node="retrieve", round=0, seconds=0.01)],
        model_calls=3,
        retries=1,
        output_tokens=2400,
        largest_output_tokens=1800,
        category="invalid_output",
        generation_seconds=40.5,
        settle_seconds=0.25,
        response_seconds=41.0,
        unconfirmed=True,
    )
    store = DraftsStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())
    ended_run(store, thread_id="plan:a", plan_date=PLAN_DATE, outcome="checks_failed")
    store.record_timing("plan:a", timing)
    store.record_timing("plan:never-saved", timing)

    (ended,) = store.runs_without_a_draft()

    assert ended.timing == timing


def test_a_file_written_before_runs_kept_time_gains_the_column() -> None:
    """Its runs keep no time; a run saved after the upgrade does."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.execute(
        "CREATE TABLE runs (thread_id TEXT PRIMARY KEY, plan_date TEXT NOT NULL, "
        "outcome TEXT NOT NULL, recorded_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO runs VALUES ('plan:old', '2026-08-19', 'checks_failed', "
        "'2026-08-19T22:00:00+00:00')"
    )
    connection.commit()

    store = DraftsStore(connection, fixture_clock())
    ended_run(store, thread_id="plan:new", plan_date=PLAN_DATE, outcome="timed_out")
    store.record_timing("plan:new", RunTiming(seconds=90.0, category="timeout"))
    timed = {run.thread_id: run.timing for run in store.runs_without_a_draft()}

    assert timed["plan:old"] is None
    assert timed["plan:new"] == RunTiming(seconds=90.0, category="timeout")


# ---------------------------------------------------------------- publication


def test_a_saved_draft_reaches_no_page_until_its_run_settles_and_publishes_it() -> None:
    """Saving is the record; publishing is what the pages read."""
    store = store_in_memory()
    try:
        admitted(store, "t")
        store.record_waiting(draft(), thread_id="t", plan_date=PLAN_DATE, outcome="accepted")
        before = (store.waiting(), store.latest_for(PLAN_DATE), store.unpublished())
        saved = store.get(draft().draft_id)
        settled = store.settle_run("t")
        after = (store.waiting(), store.latest_for(PLAN_DATE), store.unpublished())
    finally:
        store.close()

    assert before[0] == []
    assert before[1] is None
    assert [record.draft_id for record in before[2]] == [draft().draft_id]
    assert saved is not None
    assert saved.published is False
    assert settled.displaced == []
    assert settled.run.status == "published"
    assert [record.draft_id for record in after[0]] == [draft().draft_id]
    assert after[1] is not None
    assert after[1].published is True
    assert after[2] == []


def test_publishing_takes_the_place_of_the_published_draft_waiting_for_the_evening() -> None:
    store = store_in_memory()
    try:
        first = Draft(draft_id="draft:a", body="a", created_at=CREATED)
        second = Draft(draft_id="draft:b", body="b", created_at=CREATED.replace(hour=23))
        save_and_publish(store, first, thread_id="ta", plan_date=PLAN_DATE, outcome="accepted")
        admitted(store, "tb")
        store.record_waiting(second, thread_id="tb", plan_date=PLAN_DATE, outcome="accepted")
        still_first = store.latest_for(PLAN_DATE)
        settled = store.settle_run("tb")
        waiting = [record.draft_id for record in store.waiting()]
        latest = store.latest_for(PLAN_DATE)
        closed = store.get("draft:a")
        again = store.settle_run("tb")
    finally:
        store.close()

    assert still_first is not None
    assert still_first.draft_id == "draft:a"
    assert [record.draft_id for record in settled.displaced] == ["draft:a"]
    assert waiting == ["draft:b"]
    assert latest is not None
    assert latest.draft_id == "draft:b"
    assert closed is not None
    assert closed.decision == "superseded"
    assert closed.reason == SUPERSEDED_REASON
    assert closed.decided_at == fixture_clock().now()
    assert again.displaced == []
    assert again.run.status == "published"
    assert again.run.draft == settled.run.draft


def one_published_and_one_waiting(store: DraftsStore) -> None:
    first = Draft(draft_id="draft:a", body="a", created_at=CREATED)
    second = Draft(draft_id="draft:b", body="b", created_at=CREATED.replace(hour=23))
    save_and_publish(store, first, thread_id="ta", plan_date=PLAN_DATE, outcome="accepted")
    admitted(store, "tb")
    store.record_waiting(second, thread_id="tb", plan_date=PLAN_DATE, outcome="accepted")


def test_a_settle_waits_for_the_store_only_as_long_as_it_was_given() -> None:
    """Another caller holds the store. The settle gives up at its wait with ``StoreBusy``,
    before any transaction begins, and the run can still settle once the store is free."""
    store = store_in_memory()
    try:
        one_published_and_one_waiting(store)
        store._lock.acquire()
        started = monotonic()
        try:
            with pytest.raises(StoreBusy):
                store.settle_run("tb", wait=0.2)
            waited = monotonic() - started
        finally:
            store._lock.release()
        latest = store.latest_for(PLAN_DATE)
        standing = store.run_status("tb")
        settled = store.settle_run("tb")
    finally:
        store.close()

    assert 0.15 < waited < 1.0
    assert latest is not None
    assert latest.draft_id == "draft:a"
    assert standing is not None
    assert standing.status == "running"
    assert settled.run.status == "published"


def test_the_current_plan_is_the_last_published_whatever_times_the_drafts_carry() -> None:
    """The draft made later can settle first; the order of publication decides."""
    store = store_in_memory()
    try:
        made_first = Draft(draft_id="draft:a", body="a", created_at=CREATED)
        made_second = Draft(draft_id="draft:b", body="b", created_at=CREATED.replace(hour=23))
        save_and_publish(
            store, made_second, thread_id="tb", plan_date=PLAN_DATE, outcome="accepted"
        )
        save_and_publish(store, made_first, thread_id="ta", plan_date=PLAN_DATE, outcome="accepted")
        waiting = [record.draft_id for record in store.waiting()]
        latest = store.latest_for(PLAN_DATE)
    finally:
        store.close()

    assert waiting == ["draft:a"]
    assert latest is not None
    assert latest.draft_id == "draft:a"


def test_a_replayed_save_of_a_published_draft_is_refused_and_changes_nothing() -> None:
    """A save for a run that settled, made at a later moment or with another composition,
    is refused: not its decision, its place, nor its steps change."""
    store = store_in_memory()
    try:
        first = Draft(draft_id="draft:a", body="a", created_at=CREATED)
        second = Draft(draft_id="draft:b", body="b", created_at=CREATED.replace(hour=23))
        save_and_publish(
            store,
            first,
            thread_id="ta",
            plan_date=PLAN_DATE,
            outcome="accepted",
            steps=[step("plan", 1)],
        )
        save_and_publish(store, second, thread_id="tb", plan_date=PLAN_DATE, outcome="accepted")
        before = store.get("draft:a")
        for replay in (
            first.model_copy(update={"created_at": CREATED.replace(hour=23)}),
            first.model_copy(update={"body": "a, again"}),
        ):
            with pytest.raises(RunEnded, match="published") as refused:
                store.record_waiting(
                    replay, thread_id="ta", plan_date=PLAN_DATE, outcome="unsettled", too_much=True
                )
            assert refused.value.run is not None
            assert refused.value.run.status == "published"
        replayed = store.get("draft:a")
        waiting = [record.draft_id for record in store.waiting()]
    finally:
        store.close()

    assert before is not None
    assert replayed == before
    assert replayed.published is True
    assert replayed.decision == "superseded"
    assert replayed.body == "a"
    assert [item.node for item in replayed.steps] == ["plan"]
    assert waiting == ["draft:b"]


def test_a_draft_saved_for_another_thread_or_evening_is_refused() -> None:
    store = store_in_memory()
    try:
        admitted(store, "ta")
        store.record_waiting(draft(), thread_id="ta", plan_date=PLAN_DATE, outcome="accepted")
        with pytest.raises(IncompatibleReplay, match="another thread or evening"):
            store.record_waiting(
                draft(), thread_id="ta", plan_date=PLAN_DATE + timedelta(days=1), outcome="accepted"
            )
        kept = store.get(draft().draft_id)
    finally:
        store.close()

    assert kept is not None
    assert kept.plan_date == PLAN_DATE


def test_a_run_ended_before_publication_leaves_the_plan_before_it_untouched() -> None:
    store = store_in_memory()
    try:
        first = Draft(draft_id="draft:a", body="a", created_at=CREATED)
        second = Draft(draft_id="draft:b", body="b", created_at=CREATED.replace(hour=23))
        save_and_publish(store, first, thread_id="ta", plan_date=PLAN_DATE, outcome="accepted")
        admitted(store, "tb")
        store.record_waiting(
            second, thread_id="tb", plan_date=PLAN_DATE, outcome="accepted", steps=[step("plan", 1)]
        )
        ended = store.end_run("tb", reason=INTERRUPTED)
        again = store.end_run("tb", reason="service_failed")
        waiting = [record.draft_id for record in store.waiting()]
        gone = store.get("draft:b")
        runs = store.runs_without_a_draft()
    finally:
        store.close()

    assert ended is not None
    assert (ended.status, ended.reason) == ("ended", INTERRUPTED)
    assert again == ended
    assert waiting == ["draft:a"]
    assert gone is None
    assert [(run.thread_id, run.outcome, len(run.steps)) for run in runs] == [
        ("tb", INTERRUPTED, 1)
    ]


def test_what_a_publication_displaced_is_read_before_the_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing after the commit can fail and make a committed publication look failed: the
    published record and what it displaced come from inside the transaction."""
    store = store_in_memory()
    try:
        save_and_publish(
            store,
            Draft(draft_id="draft:a", body="a", created_at=CREATED),
            thread_id="ta",
            plan_date=PLAN_DATE,
            outcome="accepted",
        )
        admitted(store, "tb")
        store.record_waiting(
            Draft(draft_id="draft:b", body="b", created_at=CREATED),
            thread_id="tb",
            plan_date=PLAN_DATE,
            outcome="accepted",
        )

        def broken(*_: object, **__: object) -> None:
            msg = "no reads after the commit"
            raise AssertionError(msg)

        for name in ("get", "latest_for", "run_status", "latest_run", "_drafts"):
            monkeypatch.setattr(store, name, broken)
        settled = store.settle_run("tb")
        monkeypatch.undo()
        waiting = [record.draft_id for record in store.waiting()]
    finally:
        store.close()

    assert [(item.draft_id, item.thread_id) for item in settled.displaced] == [("draft:a", "ta")]
    assert settled.run.draft is not None
    assert settled.run.draft.draft_id == "draft:b"
    assert settled.run.draft.published is True
    assert waiting == ["draft:b"]


def test_settling_an_unknown_run_is_an_error() -> None:
    store = store_in_memory()
    try:
        with pytest.raises(KeyError, match="plan:nobody"):
            store.settle_run("plan:nobody")
    finally:
        store.close()


def test_a_superseded_draft_is_never_the_latest() -> None:
    store = store_in_memory()
    try:
        save_and_publish(
            store,
            Draft(draft_id="draft:a", body="a", created_at=CREATED),
            thread_id="ta",
            plan_date=PLAN_DATE,
            outcome="accepted",
        )
        store.record_decision(
            "draft:a", status=DraftStatus.APPROVED_FOR_MANUAL_SEND, decision="approved", reason=None
        )
        save_and_publish(
            store,
            Draft(draft_id="draft:b", body="b", created_at=CREATED),
            thread_id="tb",
            plan_date=PLAN_DATE,
            outcome="accepted",
        )
        save_and_publish(
            store,
            Draft(draft_id="draft:c", body="c", created_at=CREATED),
            thread_id="tc",
            plan_date=PLAN_DATE,
            outcome="accepted",
        )
        latest = store.latest_for(PLAN_DATE)
        closed = store.get("draft:b")
    finally:
        store.close()

    assert latest is not None
    assert latest.draft_id == "draft:c"
    assert closed is not None
    assert closed.decision == "superseded"


def test_a_file_from_before_publication_has_every_draft_published() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.execute(
        """
        CREATE TABLE drafts (
            draft_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE, plan_date TEXT NOT NULL,
            status TEXT NOT NULL, outcome TEXT NOT NULL, body TEXT NOT NULL,
            created_at TEXT NOT NULL, decided_at TEXT, decision TEXT, reason TEXT,
            too_much INTEGER NOT NULL DEFAULT 0, superseded_by TEXT, saved_order INTEGER
        )
        """
    )
    connection.execute(
        """
        INSERT INTO drafts VALUES (
            'draft:old', 'plan:old', '2026-08-19', 'DRAFT', 'accepted', 'old',
            '2026-08-19T20:00:00+00:00', NULL, NULL, NULL, 0, NULL, 7
        )
        """
    )
    connection.commit()

    store = DraftsStore(connection, fixture_clock())
    old = store.get("draft:old")
    waiting = [record.draft_id for record in store.waiting()]
    latest = store.latest_for(PLAN_DATE)

    assert old is not None
    assert old.published is True
    assert waiting == ["draft:old"]
    assert latest is not None
    assert latest.draft_id == "draft:old"


def test_a_draft_names_the_work_its_plan_speaks_about_and_an_older_file_names_none() -> None:
    """The ids come back as saved, in order; a plan that speaks about nothing names an
    empty list; a draft from a file written before drafts carried them names none, which
    is told from the empty list."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.execute(
        """
        CREATE TABLE drafts (
            draft_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE, plan_date TEXT NOT NULL,
            status TEXT NOT NULL, outcome TEXT NOT NULL, body TEXT NOT NULL,
            created_at TEXT NOT NULL, decided_at TEXT, decision TEXT, reason TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO drafts VALUES (
            'draft:old', 'plan:old', '2026-08-19', 'DRAFT', 'accepted', 'Plan',
            '2026-08-19T22:00:00+00:00', NULL, NULL, NULL
        )
        """
    )
    connection.commit()

    store = DraftsStore(connection, fixture_clock())
    old = store.get("draft:old")
    save_and_publish(
        store,
        draft(),
        thread_id="plan:new",
        plan_date=PLAN_DATE,
        outcome="accepted",
        plan_assignment_ids=["assignment-b", "assignment-a"],
    )
    named = store.get(draft().draft_id)
    save_and_publish(
        store,
        Draft(draft_id="draft:empty", body="Nothing", created_at=CREATED),
        thread_id="plan:empty",
        plan_date=PLAN_DATE,
        outcome="accepted",
        plan_assignment_ids=[],
    )
    empty = store.get("draft:empty")

    assert old is not None
    assert old.plan_assignment_ids is None
    assert named is not None
    assert named.plan_assignment_ids == ["assignment-b", "assignment-a"]
    assert empty is not None
    assert empty.plan_assignment_ids == []


# ------------------------------------------------------------- the plan in force for each evening


def plan(
    store: DraftsStore,
    name: str,
    evening: date,
    *,
    decision: str | None = None,
    created: datetime = CREATED,
) -> None:
    """A draft for ``evening`` saved, published, and decided when ``decision`` says so."""
    save_and_publish(
        store,
        Draft(draft_id=name, body=name, created_at=created),
        thread_id=f"t-{name}",
        plan_date=evening,
        outcome="accepted",
    )
    if decision is not None:
        store.record_decision(
            name,
            status=DraftStatus.APPROVED_FOR_MANUAL_SEND
            if decision == "approved"
            else DraftStatus.DRAFT,
            decision=decision,  # type: ignore[arg-type]
            reason=None,
        )


def in_force(store: DraftsStore, today: date, evenings: int = 7) -> frozenset[str]:
    """What ``latest_for`` names for today and each later evening, read one by one."""
    found = (store.latest_for(today + timedelta(days=days)) for days in range(evenings))
    return frozenset(item.draft_id for item in found if item is not None)


def test_the_plan_in_force_for_each_evening_is_the_latest_published_whatever_was_decided() -> None:
    """Today's approved plan replaced by a second; tomorrow's the same; a refused and an
    approved plan for later evenings, each alone; and one plan kept for yesterday."""
    tomorrow, later, last = (PLAN_DATE + timedelta(days=days) for days in (1, 2, 3))
    store = store_in_memory()
    try:
        plan(store, "draft:yesterday", PLAN_DATE - timedelta(days=1))
        plan(store, "draft:today-first", PLAN_DATE, decision="approved")
        plan(store, "draft:today-second", PLAN_DATE)
        plan(store, "draft:tomorrow-first", tomorrow, decision="approved")
        plan(store, "draft:tomorrow-second", tomorrow)
        plan(store, "draft:refused", later, decision="rejected")
        plan(store, "draft:approved", last, decision="approved")
        before = {item.draft_id: item for item in (*store.waiting(), *store.decided())}
        snapshot = store.review_snapshot(PLAN_DATE)
        after = {item.draft_id: item for item in (*store.waiting(), *store.decided())}
        expected = in_force(store, PLAN_DATE)
    finally:
        store.close()

    assert (
        snapshot.operative
        == expected
        == {
            "draft:today-second",
            "draft:tomorrow-second",
            "draft:refused",
            "draft:approved",
        }
    )
    assert snapshot.current_id == "draft:today-second"
    assert after == before
    assert before["draft:refused"].decision == "rejected"
    assert before["draft:today-first"].decision == "approved"
    assert before["draft:yesterday"].decision is None


def test_the_order_published_decides_between_plans_made_and_decided_at_one_instant() -> None:
    """Two plans for one evening with the same made and decided times, whose ids sort the
    other way round from the order they were published in."""
    evening = PLAN_DATE + timedelta(days=4)
    store = store_in_memory()
    try:
        plan(store, "draft:z", evening, decision="approved")
        plan(store, "draft:a", evening, decision="approved")
        snapshot = store.review_snapshot(PLAN_DATE)
        latest = store.latest_for(evening)
        first, second = store.get("draft:z"), store.get("draft:a")
    finally:
        store.close()

    assert first is not None
    assert second is not None
    assert (first.created_at, first.decided_at) == (second.created_at, second.decided_at)
    assert latest is not None
    assert snapshot.operative == {latest.draft_id} == {"draft:a"}


def test_the_plans_in_force_move_with_the_household_day() -> None:
    tomorrow = PLAN_DATE + timedelta(days=1)
    store = store_in_memory()
    try:
        plan(store, "draft:today", PLAN_DATE)
        plan(store, "draft:tomorrow", tomorrow, decision="approved")
        plan(store, "draft:tomorrow-again", tomorrow)
        admitted(store, "t-saved-only", tomorrow)
        store.record_waiting(
            Draft(draft_id="draft:saved-only", body="s", created_at=CREATED),
            thread_id="t-saved-only",
            plan_date=tomorrow,
            outcome="accepted",
        )
        today = store.review_snapshot(PLAN_DATE)
        next_day = store.review_snapshot(tomorrow)
        expected = (in_force(store, PLAN_DATE), in_force(store, tomorrow))
    finally:
        store.close()

    assert (today.operative, next_day.operative) == expected
    assert next_day.operative == {"draft:tomorrow-again"}
    assert "draft:today" not in next_day.operative
    assert "draft:saved-only" not in today.operative | next_day.operative
    assert next_day.current_id == "draft:tomorrow-again"


# ---------------------------------------------------------------- the run's lifecycle


class Traced:
    """The statements a connection runs, in order, and an action to take as one starts."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.statements: list[str] = []
        self.on: dict[str, Callable[[], None]] = {}
        connection.set_trace_callback(self._saw)

    def _saw(self, statement: str) -> None:
        self.statements.append(statement)
        action = self.on.get(statement)
        if action is not None:
            action()


def traced_store(clock: Callable[[], float] = monotonic) -> tuple[DraftsStore, Traced]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    return DraftsStore(connection, fixture_clock(), clock), Traced(connection)


def faked_store(fake: FakeTime) -> DraftsStore:
    return DraftsStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock(), fake)


def running_rows(store: DraftsStore) -> list[str]:
    return [
        str(row[0])
        for row in store._connection.execute(
            "SELECT thread_id FROM runs WHERE status='running' ORDER BY rowid"
        )
    ]


def test_a_second_run_is_refused_while_one_is_running_and_told_which() -> None:
    """One run per household: a run for another evening blocks today's too, and the refusal
    names the run, its evening and its time left, with no second row written."""
    fake = FakeTime()
    store = faked_store(fake)
    tomorrow = PLAN_DATE + timedelta(days=1)
    try:
        admitted(store, "plan:parent", tomorrow, now=fake)
        fake.now += 50
        blocking = store.admit_run("plan:hers", plan_date=PLAN_DATE, deadline_mono=fake() + 90)
        rows = store._connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        store.close()

    assert blocking is not None
    assert (blocking.run_id, blocking.plan_date, blocking.status) == (
        "plan:parent",
        tomorrow,
        "running",
    )
    assert blocking.seconds_left == pytest.approx(40)
    assert rows == 1


def test_a_run_past_its_deadline_is_ended_by_the_next_admission_and_by_a_status_read() -> None:
    fake = FakeTime()
    store = faked_store(fake)
    try:
        admitted(store, "plan:old", now=fake, seconds=10)
        store.record_waiting(draft(), thread_id="plan:old", plan_date=PLAN_DATE, outcome="accepted")
        fake.now += 10
        before = store.latest_run()
        admitted(store, "plan:new", now=fake)
        old = store.run_status("plan:old")
        candidate = store.get(draft().draft_id)
        fake.now += 90
        new = store.run_status("plan:new")
    finally:
        store.close()

    assert before is not None
    assert (before.status, before.seconds_left) == ("running", 0.0)
    assert old is not None
    assert (old.status, old.reason) == ("ended", TIMED_OUT)
    assert candidate is None
    assert new is not None
    assert (new.status, new.reason) == ("ended", TIMED_OUT)


def test_a_deadline_no_run_of_this_process_could_have_is_ended_as_interrupted() -> None:
    fake = FakeTime()
    store = faked_store(fake)
    try:
        admitted(store, "plan:before-a-restart", now=fake, seconds=RUN_DEADLINE_SECONDS + 1)
        admitted(store, "plan:new", now=fake)
        earlier = store.run_status("plan:before-a-restart")
        running = running_rows(store)
    finally:
        store.close()

    assert earlier is not None
    assert (earlier.status, earlier.reason) == ("ended", INTERRUPTED)
    assert running == ["plan:new"]


def test_a_start_ends_every_running_run_and_leaves_the_published_plan() -> None:
    store = store_in_memory()
    try:
        save_and_publish(
            store,
            Draft(draft_id="draft:a", body="a", created_at=CREATED),
            thread_id="ta",
            plan_date=PLAN_DATE,
            outcome="accepted",
        )
        admitted(store, "tb")
        store.record_waiting(draft(), thread_id="tb", plan_date=PLAN_DATE, outcome="accepted")
        ended = store.end_interrupted_runs()
        again = store.end_interrupted_runs()
        state = store.run_status("tb")
        latest = store.latest_for(PLAN_DATE)
    finally:
        store.close()

    assert ended == ["tb"]
    assert again == []
    assert state is not None
    assert (state.status, state.reason) == ("ended", INTERRUPTED)
    assert latest is not None
    assert latest.draft_id == "draft:a"


def test_a_late_save_for_an_ended_run_writes_nothing_and_the_next_run_publishes() -> None:
    """The old run timed out and a new one was admitted; the old run's compose, released
    late, is refused, leaving no draft and the old run's outcome and steps as they were."""
    fake = FakeTime()
    store = faked_store(fake)
    try:
        admitted(store, "plan:old", now=fake, seconds=10)
        fake.now += 11
        admitted(store, "plan:new", now=fake)
        with pytest.raises(RunEnded, match="ended") as refused:
            store.record_waiting(
                draft(),
                thread_id="plan:old",
                plan_date=PLAN_DATE,
                outcome="accepted",
                steps=[step("plan", 1)],
            )
        orphan = store.get(draft().draft_id)
        steps = store.steps_for("plan:old")
        store.record_waiting(
            Draft(draft_id="draft:new", body="new", created_at=CREATED),
            thread_id="plan:new",
            plan_date=PLAN_DATE,
            outcome="accepted",
        )
        settled = store.settle_run("plan:new")
    finally:
        store.close()

    assert refused.value.run is not None
    assert (refused.value.run.status, refused.value.run.reason) == ("ended", TIMED_OUT)
    assert orphan is None
    assert steps == []
    assert settled.run.status == "published"


def test_a_save_past_the_deadline_records_the_timeout_first_and_is_refused() -> None:
    """The run ends ``timed_out`` with the steps the save was given, and no draft is saved."""
    fake = FakeTime()
    store = faked_store(fake)
    steps = [step("retrieve", 0), step("plan", 1), step("verify", 1)]
    try:
        admitted(store, "plan:x", now=fake, seconds=10)
        fake.now += 10
        with pytest.raises(RunEnded) as refused:
            store.record_waiting(
                draft(), thread_id="plan:x", plan_date=PLAN_DATE, outcome="accepted", steps=steps
            )
        state = store.latest_run()
        saved = store.get(draft().draft_id)
        kept = store.steps_for("plan:x")
        listed = store.runs_without_a_draft()
    finally:
        store.close()

    assert refused.value.run is not None
    assert refused.value.run.status == "ended"
    assert state is not None
    assert (state.status, state.reason) == ("ended", TIMED_OUT)
    assert saved is None
    assert kept == steps
    assert [(run.thread_id, run.outcome, run.steps) for run in listed] == [
        ("plan:x", TIMED_OUT, steps)
    ]


def test_a_save_for_a_run_never_admitted_is_refused() -> None:
    store = store_in_memory()
    try:
        with pytest.raises(RunEnded, match="never admitted"):
            store.record_waiting(
                draft(), thread_id="plan:x", plan_date=PLAN_DATE, outcome="accepted"
            )
        saved = store.get(draft().draft_id)
    finally:
        store.close()

    assert saved is None


def test_an_ending_past_the_deadline_is_a_timeout_whatever_reason_it_gives() -> None:
    fake = FakeTime()
    store = faked_store(fake)
    try:
        admitted(store, "plan:late", now=fake, seconds=10)
        admitted_steps = [step("retrieve", 0), step("verify", 1, "1 of 7 checks failed")]
        fake.now += 10.5
        late = store.end_run("plan:late", reason="checks_failed", steps=admitted_steps)
        admitted(store, "plan:early", now=fake, seconds=10)
        early = store.end_run("plan:early", reason="checks_failed")
        steps = store.steps_for("plan:late")
    finally:
        store.close()

    assert late is not None
    assert (late.status, late.reason) == ("ended", TIMED_OUT)
    assert early is not None
    assert (early.status, early.reason) == ("ended", "checks_failed")
    assert steps == admitted_steps


def test_an_ending_keeps_the_steps_saved_with_the_draft_and_adds_the_last() -> None:
    store = store_in_memory()
    try:
        admitted(store, "plan:x")
        store.record_waiting(
            draft(),
            thread_id="plan:x",
            plan_date=PLAN_DATE,
            outcome="accepted",
            steps=[step("retrieve", 0), step("critique", 1)],
        )
        ended = store.end_run(
            "plan:x",
            reason="service_failed",
            steps=[step("budget", 0)],
            terminal=step("gate", 1, "the service failed"),
        )
        steps = [item.node for item in store.steps_for("plan:x")]
        gone = store.get(draft().draft_id)
    finally:
        store.close()

    assert ended is not None
    assert (ended.status, ended.reason) == ("ended", "service_failed")
    assert steps == ["retrieve", "critique", "gate"]
    assert gone is None


def test_an_ending_after_publication_is_told_and_changes_nothing() -> None:
    store = store_in_memory()
    try:
        save_and_publish(store, draft(), thread_id="t", plan_date=PLAN_DATE, outcome="accepted")
        ended = store.end_run("t", reason=INTERRUPTED)
        latest = store.latest_for(PLAN_DATE)
        unknown = store.end_run("plan:never", reason=INTERRUPTED)
    finally:
        store.close()

    assert ended is not None
    assert ended.status == "published"
    assert latest is not None
    assert latest.draft_id == draft().draft_id
    assert unknown is None


def test_a_decision_about_an_unpublished_draft_is_refused() -> None:
    store = store_in_memory()
    try:
        admitted(store, "t")
        store.record_waiting(draft(), thread_id="t", plan_date=PLAN_DATE, outcome="accepted")
        with pytest.raises(NotPublished):
            store.record_decision(
                draft().draft_id, status=DraftStatus.DRAFT, decision="rejected", reason="no"
            )
        undecided = store.get(draft().draft_id)
        store.settle_run("t")
        decided = store.record_decision(
            draft().draft_id, status=DraftStatus.DRAFT, decision="rejected", reason="no"
        )
    finally:
        store.close()

    assert undecided is not None
    assert undecided.decision is None
    assert decided.decision == "rejected"


def test_each_call_sets_its_wait_before_it_begins_and_before_it_commits_and_nothing_after() -> None:
    """A settle whose deadline is 0.2 seconds off waits for its commit until the deadline and
    the grace, at most; the statement before the commit sets that wait, and the commit is
    the last statement the settle runs."""
    fake = FakeTime()
    store, traced = traced_store(fake)
    try:
        save_and_publish_on(store, fake)
        admitted(store, "tb", now=fake, seconds=0.2)
        store.record_waiting(
            Draft(draft_id="draft:b", body="b", created_at=CREATED),
            thread_id="tb",
            plan_date=PLAN_DATE,
            outcome="accepted",
        )
        traced.statements.clear()
        settled = store.settle_run("tb", wait=2.0)
        statements = list(traced.statements)
    finally:
        store.close()

    assert settled.run.status == "published"
    assert statements[1] == "BEGIN IMMEDIATE"
    assert statements[-1] == "COMMIT"
    begin_wait = int(statements[0].removeprefix("PRAGMA busy_timeout="))
    commit_wait = int(statements[-2].removeprefix("PRAGMA busy_timeout="))
    assert 1500 < begin_wait <= 2000
    assert 1000 < commit_wait <= 1200


def save_and_publish_on(store: DraftsStore, fake: FakeTime) -> None:
    settled_run(
        store,
        Draft(draft_id="draft:a", body="a", created_at=CREATED),
        thread_id="ta",
        plan_date=PLAN_DATE,
        now=fake,
    )


def test_a_deadline_passing_while_the_settle_waits_for_the_writer_refuses_it() -> None:
    """The deadline is read once the writer is held: time that passes while the settle
    waits for it counts, and the run ends ``timed_out`` with the plan before it in place."""
    fake = FakeTime()
    store, traced = traced_store(fake)
    try:
        save_and_publish_on(store, fake)
        admitted(store, "tb", now=fake)
        store.record_waiting(
            Draft(draft_id="draft:b", body="b", created_at=CREATED),
            thread_id="tb",
            plan_date=PLAN_DATE,
            outcome="accepted",
        )

        def late() -> None:
            fake.now += RUN_DEADLINE_SECONDS

        traced.on["BEGIN IMMEDIATE"] = late
        settled = store.settle_run("tb")
        traced.on.clear()
        latest = store.latest_for(PLAN_DATE)
        candidate = store.get("draft:b")
    finally:
        store.close()

    assert (settled.run.status, settled.run.reason) == ("ended", TIMED_OUT)
    assert settled.displaced == []
    assert latest is not None
    assert latest.draft_id == "draft:a"
    assert candidate is None


def test_a_run_whose_evening_gained_a_newer_plan_is_overtaken() -> None:
    store = store_in_memory()
    try:
        save_and_publish(
            store,
            Draft(draft_id="draft:a", body="a", created_at=CREATED),
            thread_id="ta",
            plan_date=PLAN_DATE,
            outcome="accepted",
        )
        admitted(store, "tb")
        store.record_waiting(draft(), thread_id="tb", plan_date=PLAN_DATE, outcome="accepted")
        store._connection.execute(
            """
            INSERT INTO drafts (draft_id, thread_id, plan_date, status, outcome, body, created_at,
                                published, published_order)
            VALUES ('draft:other', 'tother', '2026-08-19', 'DRAFT', 'accepted', 'other', ?, 1, 9)
            """,
            (CREATED.isoformat(),),
        )
        store._connection.commit()
        settled = store.settle_run("tb")
        latest = store.latest_for(PLAN_DATE)
        candidate = store.get(draft().draft_id)
    finally:
        store.close()

    assert (settled.run.status, settled.run.reason) == ("ended", OVERTAKEN)
    assert latest is not None
    assert latest.draft_id == "draft:other"
    assert candidate is None


def test_runs_in_progress_are_not_listed_as_ended_until_their_deadline_passes() -> None:
    """A run still within its deadline neither shows as ended nor makes an earlier one old;
    past its deadline it has no draft that could publish, and shows as timed out."""
    fake = FakeTime()
    store = faked_store(fake)
    try:
        ended_run(store, thread_id="plan:x", plan_date=PLAN_DATE, outcome="checks_failed", now=fake)
        admitted(store, "plan:y", now=fake, seconds=10)
        during = [(run.thread_id, run.outcome, run.newest) for run in store.runs_without_a_draft()]
        fake.now += 10
        after = [(run.thread_id, run.outcome, run.newest) for run in store.runs_without_a_draft()]
    finally:
        store.close()

    assert during == [("plan:x", "checks_failed", True)]
    assert after == [("plan:y", TIMED_OUT, True), ("plan:x", "checks_failed", False)]


def test_the_newest_run_says_whether_the_plan_it_meant_to_replace_still_stands() -> None:
    store = store_in_memory()
    try:
        nothing = store.latest_run()
        ended_run(store, thread_id="plan:x", plan_date=PLAN_DATE, outcome="checks_failed")
        unchanged = store.latest_run()
        settled = settled_run(store, draft(), thread_id="plan:y", plan_date=PLAN_DATE)
        latest = store.latest_run()
        earlier = store.run_status("plan:x")
    finally:
        store.close()

    assert nothing is None
    assert unchanged is not None
    assert (unchanged.run_id, unchanged.status, unchanged.plan_unchanged) == (
        "plan:x",
        "ended",
        True,
    )
    assert latest is not None
    assert (latest.run_id, latest.status) == ("plan:y", "published")
    assert settled.run.draft is not None
    assert earlier is not None
    assert earlier.plan_unchanged is False


# ---------------------------------------------------------------- real files and threads


@contextmanager
def held(path: pathlib.Path, begin: str, seconds: float) -> Iterator[threading.Timer]:
    """Another connection holds the file from ``begin`` until a timer lets go after ``seconds``."""
    other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    other.execute(begin)
    if begin == "BEGIN":
        other.execute("SELECT COUNT(*) FROM drafts").fetchone()
    release = threading.Timer(seconds, lambda: other.execute("ROLLBACK"))
    release.start()
    try:
        yield release
    finally:
        release.join(timeout=10)
        other.close()


def file_store(tmp_path: pathlib.Path) -> tuple[DraftsStore, pathlib.Path]:
    path = tmp_path / "state" / "blossom.sqlite3"
    return DraftsStore.open(path, fixture_clock()), path


def test_a_settle_waits_for_the_writer_only_as_long_as_it_was_given(tmp_path: pathlib.Path) -> None:
    """Another connection holds the writer for 0.7 seconds. A settle given 0.3 is refused
    with ``WriterBusy`` before anything begins; one given the full wait publishes after."""
    store, path = file_store(tmp_path)
    try:
        one_published_and_one_waiting(store)
        with held(path, "BEGIN IMMEDIATE", 0.7):
            started = monotonic()
            with pytest.raises(WriterBusy):
                store.settle_run("tb", wait=0.3)
            refused_after = monotonic() - started
            standing = store.latest_run()
            settled = store.settle_run("tb")
            published_after = monotonic() - started
    finally:
        store.close()

    assert 0.25 < refused_after < 0.7
    assert standing is not None
    assert standing.status == "running"
    assert settled.run.status == "published"
    assert 0.6 < published_after < 5.0


def test_a_commit_held_behind_a_reading_waits_until_the_deadline_and_the_grace(
    tmp_path: pathlib.Path,
) -> None:
    """A reading on another connection holds the file through the settle's commit. With its
    deadline 0.3 seconds off and no grace, the commit fails then and rolls back: the run is
    still running and the plan before it stands, and the next status read ends it."""
    store, path = file_store(tmp_path)
    try:
        save_and_publish(
            store,
            Draft(draft_id="draft:a", body="a", created_at=CREATED),
            thread_id="ta",
            plan_date=PLAN_DATE,
            outcome="accepted",
        )
        admitted(store, "tb", seconds=0.3)
        store.record_waiting(draft(), thread_id="tb", plan_date=PLAN_DATE, outcome="accepted")
        with held(path, "BEGIN", 1.2):
            started = monotonic()
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                store.settle_run("tb", grace=0.0)
            failed_after = monotonic() - started
        standing = store.latest_run()
        latest = store.latest_for(PLAN_DATE)
        reconciled = store.run_status("tb")
    finally:
        store.close()

    assert 0.15 < failed_after < 1.0
    assert standing is not None
    assert standing.status == "running"
    assert latest is not None
    assert latest.draft_id == "draft:a"
    assert reconciled is not None
    assert (reconciled.status, reconciled.reason) == ("ended", TIMED_OUT)


def test_a_commit_held_behind_a_reading_lands_once_the_reading_ends(
    tmp_path: pathlib.Path,
) -> None:
    store, path = file_store(tmp_path)
    try:
        one_published_and_one_waiting(store)
        with held(path, "BEGIN", 0.4):
            started = monotonic()
            settled = store.settle_run("tb")
            landed_after = monotonic() - started
    finally:
        store.close()

    assert settled.run.status == "published"
    assert 0.3 < landed_after < 5.0


def test_a_page_read_after_a_short_settle_waits_its_own_time(tmp_path: pathlib.Path) -> None:
    """The settle's commit wait was 0.2 seconds; a page read after it still waits the store's
    own time for a file another connection holds for 0.6 seconds."""
    store, path = file_store(tmp_path)
    try:
        admitted(store, "ta", seconds=0.2)
        store.record_waiting(draft(), thread_id="ta", plan_date=PLAN_DATE, outcome="accepted")
        store.settle_run("ta", grace=0.0)
        with held(path, "BEGIN EXCLUSIVE", 0.6):
            started = monotonic()
            latest = store.latest_for(PLAN_DATE)
            read_after = monotonic() - started
    finally:
        store.close()

    assert latest is not None
    assert latest.draft_id == draft().draft_id
    assert 0.4 < read_after < 5.0


def test_a_press_during_the_settles_commit_is_admitted_once_the_plan_is_published(
    tmp_path: pathlib.Path,
) -> None:
    """The second admission waits for the store while the first run commits, then finds no
    running run and expects the plan just published; two runs never run at once."""
    path = tmp_path / "state" / "blossom.sqlite3"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    store = DraftsStore(connection, fixture_clock())
    traced = Traced(connection)
    committing = threading.Event()
    results: dict[str, object] = {}
    try:
        admitted(store, "plan:first")
        store.record_waiting(
            draft(), thread_id="plan:first", plan_date=PLAN_DATE, outcome="accepted"
        )
        traced.on["COMMIT"] = committing.set

        def settling() -> None:
            results["settled"] = store.settle_run("plan:first")

        def pressing() -> None:
            results["admitted"] = store.admit_run(
                "plan:second", plan_date=PLAN_DATE, deadline_mono=monotonic() + 90
            )

        with held(path, "BEGIN", 0.4):
            first = threading.Thread(target=settling)
            first.start()
            assert committing.wait(timeout=5)
            traced.on.clear()
            second = threading.Thread(target=pressing)
            second.start()
            first.join(timeout=10)
            second.join(timeout=10)
        base = connection.execute(
            "SELECT base_order FROM runs WHERE thread_id='plan:second'"
        ).fetchone()[0]
        running = running_rows(store)
        published = store.get(draft().draft_id)
    finally:
        store.close()

    assert not first.is_alive()
    assert not second.is_alive()
    assert results["admitted"] is None
    assert published is not None
    assert published.published is True
    assert base == connection_order(path, draft().draft_id)
    assert running == ["plan:second"]


def connection_order(path: pathlib.Path, draft_id: str) -> int:
    with sqlite3.connect(path) as reading:
        (order,) = reading.execute(
            "SELECT published_order FROM drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
    reading.close()
    return int(order)


# ---------------------------------------------------------- files from before runs had a status


def dump(path: pathlib.Path, table: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as reading:
        rows = reading.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()  # noqa: S608
    reading.close()
    return rows


def legacy_file(path: pathlib.Path) -> None:
    """A file whose runs have no status, holding a paused unpublished
    draft, a withheld one, a draft with no run, a decided unpublished one, published and
    decided drafts, and steps."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as legacy:
        legacy.executescript(
            """
            CREATE TABLE drafts (
                draft_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE,
                plan_date TEXT NOT NULL, status TEXT NOT NULL, outcome TEXT NOT NULL,
                body TEXT NOT NULL, created_at TEXT NOT NULL, decided_at TEXT, decision TEXT,
                reason TEXT, too_much INTEGER NOT NULL DEFAULT 0, superseded_by TEXT,
                published INTEGER NOT NULL DEFAULT 0, published_order INTEGER,
                plan_assignment_ids TEXT, plan_snapshot TEXT, inputs_digest TEXT
            );
            CREATE TABLE runs (
                thread_id TEXT PRIMARY KEY, plan_date TEXT NOT NULL, outcome TEXT NOT NULL,
                recorded_at TEXT NOT NULL, timing TEXT
            );
            CREATE TABLE withheld_drafts (draft_id TEXT PRIMARY KEY);
            CREATE TABLE steps (
                thread_id TEXT NOT NULL, position INTEGER NOT NULL, node TEXT NOT NULL,
                round INTEGER NOT NULL, expected TEXT NOT NULL, found TEXT NOT NULL,
                recorded_at TEXT NOT NULL, PRIMARY KEY (thread_id, position)
            );
            INSERT INTO runs (thread_id, plan_date, outcome, recorded_at) VALUES
                ('r-pub', '2026-08-19', 'accepted', '2026-08-19T20:00:00+00:00'),
                ('r-paused', '2026-08-19', 'accepted', '2026-08-19T20:10:00+00:00'),
                ('r-withheld', '2026-08-19', 'unsettled', '2026-08-19T20:20:00+00:00'),
                ('r-none', '2026-08-19', 'checks_failed', '2026-08-19T20:30:00+00:00'),
                ('r-decided', '2026-08-19', 'accepted', '2026-08-19T20:40:00+00:00'),
                ('r-approved', '2026-08-20', 'accepted', '2026-08-19T20:50:00+00:00');
            INSERT INTO drafts (draft_id, thread_id, plan_date, status, outcome, body,
                                created_at, decided_at, decision, reason, published,
                                published_order) VALUES
                ('d-pub', 'r-pub', '2026-08-19', 'DRAFT', 'accepted', 'pub',
                 '2026-08-19T20:00:00+00:00', NULL, NULL, NULL, 1, 1),
                ('d-paused', 'r-paused', '2026-08-19', 'DRAFT', 'accepted', 'paused',
                 '2026-08-19T20:10:00+00:00', NULL, NULL, NULL, 0, NULL),
                ('d-withheld', 'r-withheld', '2026-08-19', 'DRAFT', 'unsettled', 'withheld',
                 '2026-08-19T20:20:00+00:00', NULL, NULL, NULL, 0, NULL),
                ('d-orphan', 't-orphan', '2026-08-19', 'DRAFT', 'accepted', 'orphan',
                 '2026-08-19T20:25:00+00:00', NULL, NULL, NULL, 0, NULL),
                ('d-decided', 'r-decided', '2026-08-19', 'DRAFT', 'accepted', 'decided',
                 '2026-08-19T20:40:00+00:00', '2026-08-19T21:00:00+00:00', 'rejected', 'no',
                 0, NULL),
                ('d-approved', 'r-approved', '2026-08-20', 'APPROVED_FOR_MANUAL_SEND',
                 'accepted', 'approved', '2026-08-19T20:50:00+00:00',
                 '2026-08-19T21:10:00+00:00', 'approved', NULL, 1, 2);
            INSERT INTO withheld_drafts VALUES ('d-withheld');
            INSERT INTO steps VALUES
                ('r-pub', 0, 'plan', 1, 'e', 'f', '2026-08-19T20:00:00+00:00'),
                ('r-paused', 0, 'plan', 1, 'e', 'f', '2026-08-19T20:10:00+00:00');
            """
        )
    legacy.close()


def test_a_file_from_before_runs_had_a_status_is_settled_once_and_publishes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "state" / "blossom.sqlite3"
    legacy_file(path)
    kept_before = [
        row for row in dump(path, "drafts") if row[0] in ("d-pub", "d-decided", "d-approved")
    ]
    steps_before = dump(path, "steps")

    DraftsStore.open(path, fixture_clock()).close()
    after = {table: dump(path, table) for table in ("drafts", "runs", "steps")}
    for _ in range(2):
        DraftsStore.open(path, fixture_clock()).close()
    again = {table: dump(path, table) for table in ("drafts", "runs", "steps")}
    with sqlite3.connect(path) as reading:
        tables = {row[0] for row in reading.execute("SELECT name FROM sqlite_master")}
        statuses = dict(
            reading.execute("SELECT thread_id, status || ':' || outcome FROM runs").fetchall()
        )
    reading.close()

    assert statuses == {
        "r-pub": "published:accepted",
        "r-paused": "ended:interrupted",
        "r-withheld": "ended:interrupted",
        "r-none": "ended:checks_failed",
        "r-decided": "ended:interrupted",
        "r-approved": "published:accepted",
    }
    assert [row[0] for row in after["drafts"]] == ["d-approved", "d-decided", "d-pub"]
    assert [row[:17] for row in after["drafts"]] == sorted(kept_before)
    assert after["steps"] == steps_before
    assert "withheld_drafts" not in tables
    assert again == after


def test_a_run_whose_status_disagrees_with_its_draft_is_reported_and_left_alone(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    store, path = file_store(tmp_path)
    try:
        save_and_publish(store, draft(), thread_id="t-pub", plan_date=PLAN_DATE, outcome="accepted")
        ended_run(store, thread_id="t-ended", plan_date=PLAN_DATE, outcome="checks_failed")
    finally:
        store.close()
    with sqlite3.connect(path) as editing:
        editing.execute("UPDATE runs SET status='published' WHERE thread_id='t-ended'")
        editing.execute("UPDATE runs SET status='ended' WHERE thread_id='t-pub'")
    editing.close()
    before = {table: dump(path, table) for table in ("drafts", "runs")}

    with caplog.at_level(logging.WARNING, logger="blossom.stores.drafts"):
        DraftsStore.open(path, fixture_clock()).close()

    assert "t-ended" in caplog.text
    assert "t-pub" in caplog.text
    assert {table: dump(path, table) for table in ("drafts", "runs")} == before


def test_a_published_run_whose_draft_is_unpublished_keeps_both_rows_at_every_open(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The open that reports a published run with an unpublished, undecided draft keeps
    that draft, so every open names rows that are still there to look into."""
    store, path = file_store(tmp_path)
    try:
        save_and_publish(store, draft(), thread_id="t-pub", plan_date=PLAN_DATE, outcome="accepted")
    finally:
        store.close()
    with sqlite3.connect(path) as editing:
        editing.execute("UPDATE drafts SET published=0, published_order=NULL")
    editing.close()
    before = {table: dump(path, table) for table in ("drafts", "runs")}

    reported: list[bool] = []
    kept: list[bool] = []
    for _ in range(3):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="blossom.stores.drafts"):
            DraftsStore.open(path, fixture_clock()).close()
        reported.append("t-pub" in caplog.text)
        kept.append({table: dump(path, table) for table in ("drafts", "runs")} == before)

    assert [row[0] for row in before["runs"]] == ["t-pub"]
    assert [row[0] for row in before["drafts"]] == [draft().draft_id]
    assert reported == [True, True, True]
    assert kept == [True, True, True]


def test_a_runs_state_says_whether_its_evening_has_a_published_plan() -> None:
    store = store_in_memory()
    try:
        admitted(store, "plan:x")
        running = store.run_status("plan:x")
        newest = store.latest_run()
        store.record_waiting(draft(), thread_id="plan:x", plan_date=PLAN_DATE, outcome="accepted")
        store.settle_run("plan:x")
        published = store.run_status("plan:x")
        ended_run(store, thread_id="plan:y", plan_date=PLAN_DATE, outcome="checks_failed")
        later = store.latest_run()
        ended_run(
            store,
            thread_id="plan:z",
            plan_date=PLAN_DATE + timedelta(days=1),
            outcome="checks_failed",
        )
        other_evening = store.run_status("plan:z")
    finally:
        store.close()

    assert running is not None
    assert (running.status, running.has_plan) == ("running", False)
    assert newest is not None
    assert (newest.run_id, newest.has_plan) == ("plan:x", False)
    assert published is not None
    assert (published.status, published.has_plan) == ("published", True)
    assert later is not None
    assert (later.run_id, later.status, later.has_plan) == ("plan:y", "ended", True)
    assert other_evening is not None
    assert (other_evening.status, other_evening.has_plan) == ("ended", False)
