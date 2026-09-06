"""The drafts table: the record of what waited at the gate and what was decided.

The store is written twice per draft by nodes that may run twice, so the
tests here are mostly about idempotence and durability: the same draft saved
again is one row, a decision is stamped by the store's clock, and the rows are
still there after the file is closed and reopened.
"""

import pathlib
import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from blossom.agent.steps import StepRecord
from blossom.clock import Clock
from blossom.drafts import Draft, DraftStatus
from blossom.stores.checkpoints import UnsafeCheckpointPath
from blossom.stores.drafts import (
    INTERRUPTED,
    SUPERSEDED_REASON,
    AlreadyDecided,
    DraftsStore,
    Outcome,
)
from tests.support import FIXTURE_TIMEZONE, fixture_clock

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
) -> None:
    """Save a draft and publish it at once, as a run that pauses without incident does."""
    store.record_waiting(
        draft,
        thread_id=thread_id,
        plan_date=plan_date,
        outcome=outcome,
        steps=steps,
        too_much=too_much,
    )
    store.publish(draft.draft_id)


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
    """A node that runs twice, as a resumed or crashed node does, must not queue twice."""
    store = store_in_memory()
    try:
        save_and_publish(
            store, draft("first rendering"), thread_id="t", plan_date=PLAN_DATE, outcome="accepted"
        )
        later = draft("second rendering").model_copy(
            update={"created_at": CREATED.replace(hour=23)}
        )
        save_and_publish(store, later, thread_id="t", plan_date=PLAN_DATE, outcome="unsettled")
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
        taken_back = store.withdraw(draft().draft_id)
        kept = store.get(draft().draft_id)
    finally:
        store.close()

    assert taken_back is False
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
    steps = [step("retrieve", 0), step("plan", 1), step("verify", 1, "1 of 6 checks failed")]

    store.record_run(
        thread_id="plan:2026-08-19:x", plan_date=PLAN_DATE, outcome="checks_failed", steps=steps
    )

    assert store.steps_for("plan:2026-08-19:x") == steps
    runs = store.runs_without_a_draft()
    assert [(run.thread_id, run.outcome) for run in runs] == [
        ("plan:2026-08-19:x", "checks_failed")
    ]
    assert runs[0].steps == steps
    assert runs[0].plan_date == PLAN_DATE
    assert runs[0].recorded_at == fixture_clock().now()


def test_saving_a_run_again_replaces_its_steps_and_keeps_its_first_time() -> None:
    store = ticking_store()
    store.record_run(
        thread_id="plan:x", plan_date=PLAN_DATE, outcome="model_refused", steps=[step("plan", 1)]
    )
    first_time = store.runs_without_a_draft()[0].recorded_at

    store.record_run(
        thread_id="plan:x",
        plan_date=PLAN_DATE,
        outcome="checks_failed",
        steps=[step("retrieve", 0), step("plan", 1)],
    )

    saved = store.runs_without_a_draft()
    assert [item.node for item in store.steps_for("plan:x")] == ["retrieve", "plan"]
    assert [(run.outcome, run.recorded_at) for run in saved] == [("checks_failed", first_time)]


def test_a_run_that_left_a_draft_is_not_among_those_that_ended_without_one() -> None:
    store = store_in_memory()
    save_and_publish(store, draft(), thread_id="plan:y", plan_date=PLAN_DATE, outcome="accepted")
    store.record_run(
        thread_id="plan:y", plan_date=PLAN_DATE, outcome="accepted", steps=[step("critique", 1)]
    )

    assert store.runs_without_a_draft() == []
    assert [item.node for item in store.steps_for("plan:y")] == ["critique"]


def test_a_thread_never_saved_has_no_steps() -> None:
    assert store_in_memory().steps_for("plan:nobody") == []


def test_runs_that_ended_are_most_recent_first() -> None:
    store = ticking_store()
    store.record_run(thread_id="plan:first", plan_date=PLAN_DATE, outcome="checks_failed", steps=[])
    store.record_run(
        thread_id="plan:second", plan_date=PLAN_DATE, outcome="model_refused", steps=[]
    )

    assert [run.thread_id for run in store.runs_without_a_draft()] == ["plan:second", "plan:first"]


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


def test_a_replacement_that_fails_part_way_leaves_the_earlier_account_standing() -> None:
    """The failure lands inside the transaction, after the run row is rewritten
    and the old steps are deleted, where a missing rollback would show."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    store = DraftsStore(connection, fixture_clock())
    first = [step("retrieve", 0), step("plan", 1)]
    store.record_run(thread_id="plan:x", plan_date=PLAN_DATE, outcome="model_refused", steps=first)
    connection.execute(
        """
        CREATE TRIGGER refuse_a_third_step BEFORE INSERT ON steps
        WHEN NEW.position = 2 BEGIN SELECT RAISE(ABORT, 'no third step'); END
        """
    )

    with pytest.raises(sqlite3.DatabaseError, match="no third step"):
        store.record_run(
            thread_id="plan:x",
            plan_date=PLAN_DATE,
            outcome="checks_failed",
            steps=[step("retrieve", 0), step("plan", 1), step("verify", 1)],
        )

    assert store.steps_for("plan:x") == first
    assert [run.outcome for run in store.runs_without_a_draft()] == ["model_refused"]
    connection.execute("DROP TRIGGER refuse_a_third_step")
    store.record_run(thread_id="plan:y", plan_date=PLAN_DATE, outcome="checks_failed", steps=[])
    assert {run.thread_id for run in store.runs_without_a_draft()} == {"plan:x", "plan:y"}


def test_the_retention_policy_covers_the_runs_and_their_steps() -> None:
    policy = DraftsStore.retention_policy

    assert "draft" in policy
    assert "decision" in policy
    assert "record of the run" in policy
    assert "produced no draft" in policy


def test_a_run_with_no_steps_is_listed_with_an_empty_record() -> None:
    store = store_in_memory()
    store.record_run(thread_id="plan:bare", plan_date=PLAN_DATE, outcome="model_refused", steps=[])

    assert [(run.thread_id, run.steps) for run in store.runs_without_a_draft()] == [
        ("plan:bare", [])
    ]


def test_each_listed_run_carries_only_its_own_steps() -> None:
    store = ticking_store()
    store.record_run(
        thread_id="plan:one", plan_date=PLAN_DATE, outcome="checks_failed", steps=[step("plan", 1)]
    )
    store.record_run(
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
    assert store.withdraw("draft:late") is True
    assert [record.draft_id for record in store.waiting()] == ["draft:other"]
    assert store.latest_for(PLAN_DATE) is None


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


# ---------------------------------------------------------------- publication


def test_a_saved_draft_reaches_no_page_until_its_run_pauses_and_publishes_it() -> None:
    """Saving is the record; publishing is what the pages read."""
    store = store_in_memory()
    try:
        store.record_waiting(draft(), thread_id="t", plan_date=PLAN_DATE, outcome="accepted")
        before = (store.waiting(), store.latest_for(PLAN_DATE), store.unpublished())
        saved = store.get(draft().draft_id)
        displaced = store.publish(draft().draft_id)
        after = (store.waiting(), store.latest_for(PLAN_DATE), store.unpublished())
    finally:
        store.close()

    assert before[0] == []
    assert before[1] is None
    assert [record.draft_id for record in before[2]] == [draft().draft_id]
    assert saved is not None
    assert saved.published is False
    assert displaced == []
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
        store.record_waiting(second, thread_id="tb", plan_date=PLAN_DATE, outcome="accepted")
        still_first = store.latest_for(PLAN_DATE)
        displaced = store.publish("draft:b")
        waiting = [record.draft_id for record in store.waiting()]
        latest = store.latest_for(PLAN_DATE)
        closed = store.get("draft:a")
        again = store.publish("draft:b")
    finally:
        store.close()

    assert still_first is not None
    assert still_first.draft_id == "draft:a"
    assert [record.draft_id for record in displaced] == ["draft:a"]
    assert waiting == ["draft:b"]
    assert latest is not None
    assert latest.draft_id == "draft:b"
    assert closed is not None
    assert closed.decision == "superseded"
    assert closed.reason == SUPERSEDED_REASON
    assert closed.decided_at == fixture_clock().now()
    assert again == []


def test_the_current_plan_is_the_last_published_whatever_order_the_drafts_were_saved_in() -> None:
    """Two runs can save in one order and pause in the other."""
    store = store_in_memory()
    try:
        saved_first = Draft(draft_id="draft:a", body="a", created_at=CREATED)
        saved_second = Draft(draft_id="draft:b", body="b", created_at=CREATED.replace(hour=23))
        store.record_waiting(saved_first, thread_id="ta", plan_date=PLAN_DATE, outcome="accepted")
        store.record_waiting(saved_second, thread_id="tb", plan_date=PLAN_DATE, outcome="accepted")
        store.publish("draft:b")
        store.publish("draft:a")
        waiting = [record.draft_id for record in store.waiting()]
        latest = store.latest_for(PLAN_DATE)
    finally:
        store.close()

    assert waiting == ["draft:a"]
    assert latest is not None
    assert latest.draft_id == "draft:a"


def test_a_replayed_save_of_a_published_draft_keeps_its_place_and_displaces_nothing() -> None:
    store = store_in_memory()
    try:
        first = Draft(draft_id="draft:a", body="a", created_at=CREATED)
        second = Draft(draft_id="draft:b", body="b", created_at=CREATED.replace(hour=23))
        save_and_publish(store, first, thread_id="ta", plan_date=PLAN_DATE, outcome="accepted")
        save_and_publish(store, second, thread_id="tb", plan_date=PLAN_DATE, outcome="accepted")
        store.record_waiting(
            first.model_copy(update={"body": "a, again"}),
            thread_id="ta",
            plan_date=PLAN_DATE,
            outcome="accepted",
        )
        replayed = store.get("draft:a")
        waiting = [record.draft_id for record in store.waiting()]
    finally:
        store.close()

    assert replayed is not None
    assert replayed.published is True
    assert replayed.decision == "superseded"
    assert replayed.body == "a, again"
    assert waiting == ["draft:b"]


def test_a_draft_taken_back_before_publication_leaves_the_plan_before_it_untouched() -> None:
    store = store_in_memory()
    try:
        first = Draft(draft_id="draft:a", body="a", created_at=CREATED)
        second = Draft(draft_id="draft:b", body="b", created_at=CREATED.replace(hour=23))
        save_and_publish(store, first, thread_id="ta", plan_date=PLAN_DATE, outcome="accepted")
        store.record_waiting(
            second, thread_id="tb", plan_date=PLAN_DATE, outcome="accepted", steps=[step("plan", 1)]
        )
        taken_back = store.withdraw("draft:b")
        again = store.withdraw("draft:b")
        waiting = [record.draft_id for record in store.waiting()]
        gone = store.get("draft:b")
        runs = store.runs_without_a_draft()
    finally:
        store.close()

    assert taken_back is True
    assert again is False
    assert waiting == ["draft:a"]
    assert gone is None
    assert [(run.thread_id, run.outcome, len(run.steps)) for run in runs] == [
        ("tb", INTERRUPTED, 1)
    ]


def test_publishing_an_unknown_draft_is_an_error() -> None:
    store = store_in_memory()
    try:
        with pytest.raises(KeyError, match="draft:nobody"):
            store.publish("draft:nobody")
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
