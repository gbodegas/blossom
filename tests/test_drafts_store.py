"""The drafts table: the record of what waited at the gate and what was decided.

The store is written twice per draft by nodes that may run twice, so the
tests here are mostly about idempotence and durability: the same draft saved
again is one row, a decision is stamped by the store's clock, and the rows are
still there after the file is closed and reopened.
"""

import pathlib
import sqlite3
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from blossom.clock import Clock
from blossom.drafts import Draft, DraftStatus
from blossom.stores.checkpoints import UnsafeCheckpointPath
from blossom.stores.drafts import AlreadyDecided, DraftsStore
from tests.support import FIXTURE_TIMEZONE, fixture_clock

PLAN_DATE = date(2026, 8, 19)
CREATED = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)


def store_in_memory() -> DraftsStore:
    return DraftsStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())


def draft(body: str = "Plan for Wednesday, August 19") -> Draft:
    return Draft(draft_id="draft:plan:2026-08-19:abc12345", body=body, created_at=CREATED)


def test_a_saved_draft_is_waiting_until_somebody_decides() -> None:
    store = store_in_memory()
    try:
        store.record_waiting(
            draft(), thread_id="plan:2026-08-19:abc12345", plan_date=PLAN_DATE, outcome="accepted"
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
        store.record_waiting(
            draft("first rendering"), thread_id="t", plan_date=PLAN_DATE, outcome="accepted"
        )
        later = draft("second rendering").model_copy(
            update={"created_at": CREATED.replace(hour=23)}
        )
        store.record_waiting(later, thread_id="t", plan_date=PLAN_DATE, outcome="unsettled")
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
        store.record_waiting(draft(), thread_id="t", plan_date=PLAN_DATE, outcome="accepted")
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
        store.record_waiting(draft(), thread_id="t", plan_date=PLAN_DATE, outcome="unsettled")
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
        store.record_waiting(draft(), thread_id="t", plan_date=PLAN_DATE, outcome="accepted")
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
        store.record_waiting(draft(), thread_id="t", plan_date=PLAN_DATE, outcome="accepted")
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
        store.record_waiting(newer, thread_id="tb", plan_date=PLAN_DATE, outcome="accepted")
        store.record_waiting(older, thread_id="ta", plan_date=PLAN_DATE, outcome="accepted")
        order = [record.draft_id for record in store.waiting()]
    finally:
        store.close()

    assert order == ["draft:a", "draft:b"]


def test_rows_survive_closing_and_reopening_the_file(tmp_path: pathlib.Path) -> None:
    """The queue is a record, so a restart must not empty it."""
    path = tmp_path / "state" / "blossom.sqlite3"

    first = DraftsStore.open(path, fixture_clock())
    try:
        first.record_waiting(draft(), thread_id="t", plan_date=PLAN_DATE, outcome="accepted")
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
