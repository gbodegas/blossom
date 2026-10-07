# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Saved graph state is the loop's short-term memory: kept while the loop runs, cleared after.

The routes clear a thread when its run ends or its decision lands, a draft
that waits too long is closed as expired, and the sweep at startup applies
both rules to whatever a crash left behind.
"""

import asyncio
import contextlib
import gc
import pathlib
import sqlite3
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from time import monotonic, sleep
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, Durability, StateSnapshot

from blossom.agent.graph import CompiledPlanGraph, ModelAnswer, PlanState, plan_graph_for
from blossom.agent.retention import (
    EXPIRED_REASON,
    PAUSED_RETENTION_DAYS,
    Swept,
    sweep_saved_state,
)
from blossom.agent.runs import DURABILITY, RUN_DEADLINE_SECONDS, RunBudget, run_config
from blossom.app import create_app
from blossom.clock import FrozenClock
from blossom.dependencies import (
    STATE_ATTRIBUTE,
    ApplicationState,
    build_application_state,
    sweep_aged,
)
from blossom.drafts import Decision, Draft, DraftStatus
from blossom.plans import DailyPlan, PlanBlock
from blossom.routes import runs as plan_runs
from blossom.routes.parent import DecisionRequest, decide_draft
from blossom.routes.runs import AlreadyPlanning, CouldNotStart, NotSaved, plan_graphs
from blossom.settings import (
    CALENDAR_MARGIN,
    CHECKPOINT_PATH_VARIABLE,
    DATABASE_PATH_VARIABLE,
    TRACE_PATH_VARIABLE,
)
from blossom.stores.drafts import (
    SETTLE_GRACE_SECONDS,
    DraftRecord,
    DraftsStore,
    RunRecord,
    RunState,
    Settled,
)
from blossom.views import PlanRunView
from tests.support import (
    FIXTURE_TIMEZONE,
    OBSERVED_AT,
    SAME_ORIGIN,
    FakeTime,
    Scripted,
    Spending,
    accepting,
    fixture_settings,
    fixture_week_plan,
    forgetful_fixture_plan,
    ok,
    plan_evening,
    scripted_graphs,
    settled_run,
)

PLAN_DATE = date(2026, 8, 19)
STORE_WAIT = 5.0
ZONE = ZoneInfo(FIXTURE_TIMEZONE)


CREATED_LATER = datetime(2026, 8, 19, 23, 0, tzinfo=UTC)


def application(
    *,
    saver: BaseCheckpointSaver[str] | None = None,
    monotonic: Callable[[], float] = monotonic,
    **environ: str,
) -> ApplicationState:
    return build_application_state(
        fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **environ),
        saver if saver is not None else InMemorySaver(),
        monotonic,
    )


def graph_for(state: ApplicationState, *plans: DailyPlan) -> CompiledPlanGraph:
    return plan_graph_for(
        state,
        planner=Scripted(*[ok(plan) for plan in plans]),
        critic=Scripted(*[ok(accepting()) for _ in plans]),
    )


def admitted(state: ApplicationState, thread_id: str, plan_date: date = PLAN_DATE) -> str:
    """Admit a run, as a planning route does before it invokes the graph."""
    blocking = state.drafts.admit_run(
        thread_id, plan_date=plan_date, deadline_mono=state.monotonic() + RUN_DEADLINE_SECONDS
    )
    assert blocking is None
    return thread_id


async def detached_done(state: ApplicationState) -> None:
    """Let the work a run left running on its own, such as tidying its thread, finish."""
    while state.detached:
        await asyncio.wait(set(state.detached))


def published_elsewhere(state: ApplicationState, draft_id: str, thread_id: str) -> None:
    """A plan for the evening written as published outside any run of this process."""
    connection = state.drafts._connection
    connection.execute(
        """
        INSERT INTO drafts (draft_id, thread_id, plan_date, status, outcome, body, created_at,
                            published, published_order)
        VALUES (?, ?, ?, 'DRAFT', 'accepted', 'elsewhere', ?, 1,
                (SELECT COALESCE(MAX(published_order), 0) + 1 FROM drafts))
        """,
        (draft_id, thread_id, PLAN_DATE.isoformat(), CREATED_LATER.isoformat()),
    )
    connection.commit()


async def thread_ids(state: ApplicationState) -> set[str]:
    """Every thread the saver still holds."""
    found: set[str] = set()
    async for item in state.checkpointer.alist(None):
        found.add(str(item.config["configurable"]["thread_id"]))
    return found


def test_a_run_that_ends_before_the_gate_leaves_no_saved_state() -> None:
    state = application()
    try:

        async def scenario() -> tuple[str, set[str]]:
            view = await plan_evening(
                graph_for(
                    state,
                    forgetful_fixture_plan(),
                    forgetful_fixture_plan(),
                    forgetful_fixture_plan(),
                ),
                PLAN_DATE,
                state,
            )
            await detached_done(state)
            return view.outcome, await thread_ids(state)

        outcome, remaining = asyncio.run(scenario())
        ended = state.drafts.runs_without_a_draft()
    finally:
        state.close()

    assert outcome == "checks_failed"
    assert remaining == set()
    assert [run.outcome for run in ended] == ["checks_failed"]


def test_a_run_the_model_cannot_serve_leaves_no_saved_state_either() -> None:
    """The first node is saved before the planner is asked, so a refusal to start
    would otherwise leave a thread behind until the next startup."""
    state = application()
    try:

        async def scenario() -> tuple[int, set[str]]:
            try:
                await plan_evening(
                    plan_graph_for(state),
                    PLAN_DATE,
                    state,
                )
            except HTTPException as error:
                await detached_done(state)
                return error.status_code, await thread_ids(state)
            msg = "a graph with no key started a run"
            raise AssertionError(msg)

        status_code, remaining = asyncio.run(scenario())
    finally:
        state.close()

    assert status_code == 503
    assert remaining == set()


def test_a_run_paused_at_the_gate_keeps_its_state_until_the_decision() -> None:
    state = application()
    try:

        async def scenario() -> tuple[set[str], set[str]]:
            view = await plan_evening(
                graph_for(state, fixture_week_plan()),
                PLAN_DATE,
                state,
            )
            while_waiting = await thread_ids(state)
            assert view.draft_id is not None
            await decide_draft(
                state,
                lambda: graph_for(state),
                view.draft_id,
                DecisionRequest(approved=True, reason=None),
            )
            return while_waiting, await thread_ids(state)

        while_waiting, after = asyncio.run(scenario())
        decided = state.drafts.decided()
    finally:
        state.close()

    assert len(while_waiting) == 1
    assert after == set()
    assert [record.decision for record in decided] == ["approved"]


def test_the_sweep_clears_what_no_waiting_draft_needs_and_keeps_what_one_does() -> None:
    state = application()
    try:

        async def scenario() -> tuple[Swept, set[str]]:
            waiting = await graph_for(state, fixture_week_plan()).ainvoke(
                PlanState(plan_date=PLAN_DATE, rounds=0),
                config=run_config(admitted(state, "plan:waiting")),
                durability=DURABILITY,
            )
            state.drafts.settle_run("plan:waiting")
            await graph_for(
                state, forgetful_fixture_plan(), forgetful_fixture_plan(), forgetful_fixture_plan()
            ).ainvoke(
                PlanState(plan_date=PLAN_DATE, rounds=0),
                config=run_config(admitted(state, "plan:finished")),
                durability=DURABILITY,
            )
            assert "__interrupt__" in waiting
            assert await thread_ids(state) == {"plan:waiting", "plan:finished"}
            swept = await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
            return swept, await thread_ids(state)

        swept, remaining = asyncio.run(scenario())
    finally:
        state.close()

    assert swept == Swept(expired=(), cleared=("plan:finished",))
    assert remaining == {"plan:waiting"}


def test_a_draft_that_waited_past_its_evening_is_closed_as_expired_and_its_thread_cleared() -> None:
    state = application()
    try:

        async def scenario() -> tuple[Swept, set[str]]:
            await graph_for(state, fixture_week_plan()).ainvoke(
                PlanState(plan_date=PLAN_DATE, rounds=0),
                config=run_config(admitted(state, "plan:stale")),
                durability=DURABILITY,
            )
            state.drafts.settle_run("plan:stale")
            long_after = FrozenClock(OBSERVED_AT + timedelta(days=PAUSED_RETENTION_DAYS + 2), ZONE)
            swept = await sweep_saved_state(state.checkpointer, state.drafts, long_after)
            return swept, await thread_ids(state)

        swept, remaining = asyncio.run(scenario())
        record = state.drafts.get("draft:plan:stale")
        still_waiting = state.drafts.waiting()
    finally:
        state.close()

    assert swept == Swept(expired=("draft:plan:stale",), cleared=("plan:stale",))
    assert remaining == set()
    assert record is not None
    assert record.decision == "expired"
    assert record.reason == EXPIRED_REASON
    assert record.status is DraftStatus.DRAFT
    assert still_waiting == []


def test_a_draft_still_within_the_window_is_left_waiting() -> None:
    state = application()
    try:

        async def scenario() -> Swept:
            await graph_for(state, fixture_week_plan()).ainvoke(
                PlanState(plan_date=PLAN_DATE, rounds=0),
                config=run_config(admitted(state, "plan:fresh")),
                durability=DURABILITY,
            )
            state.drafts.settle_run("plan:fresh")
            on_the_last_day = FrozenClock(OBSERVED_AT + timedelta(days=PAUSED_RETENTION_DAYS), ZONE)
            return await sweep_saved_state(state.checkpointer, state.drafts, on_the_last_day)

        swept = asyncio.run(scenario())
        waiting = state.drafts.waiting()
    finally:
        state.close()

    assert swept == Swept(expired=(), cleared=())
    assert [record.draft_id for record in waiting] == ["draft:plan:fresh"]


def test_a_waiting_draft_for_the_last_plannable_evening_is_swept_without_overflow() -> None:
    """Retention subtracts dates rather than adding a span to the plan date, so a
    draft for the last evening a plan may be made for is kept, and the sweep runs,
    through the calendar's last day. The far week holds only the undated form."""
    far = date.max - CALENDAR_MARGIN
    plan = DailyPlan(
        plan_date=far,
        blocks=[
            PlanBlock(
                assignment_id="assignment-signed-syllabus",
                starts_at=time(16, 30),
                ends_at=time(16, 40),
                rationale="ask what the date is and get it signed",
            )
        ],
    )
    state = application()
    try:

        async def scenario() -> tuple[Swept, Swept]:
            await graph_for(state, plan).ainvoke(
                PlanState(plan_date=far, rounds=0),
                config=run_config(admitted(state, "plan:far", far)),
                durability=DURABILITY,
            )
            state.drafts.settle_run("plan:far")
            on_the_evening = FrozenClock(datetime(9999, 12, 24, 9, 0, tzinfo=UTC), ZONE)
            last_day = FrozenClock(datetime(9999, 12, 31, 9, 0, tzinfo=UTC), ZONE)
            return (
                await sweep_saved_state(state.checkpointer, state.drafts, on_the_evening),
                await sweep_saved_state(state.checkpointer, state.drafts, last_day),
            )

        first, second = asyncio.run(scenario())
        waiting = [record.draft_id for record in state.drafts.waiting()]
    finally:
        state.close()

    assert first == Swept(expired=(), cleared=())
    assert second == Swept(expired=(), cleared=())
    assert waiting == ["draft:plan:far"]


def test_a_restart_expires_a_stale_draft_and_the_page_says_so(tmp_path: pathlib.Path) -> None:
    """Two starts of the application on the same files, two weeks apart."""
    files = {
        DATABASE_PATH_VARIABLE: str(tmp_path / "blossom.sqlite3"),
        CHECKPOINT_PATH_VARIABLE: str(tmp_path / "checkpoints.sqlite3"),
        TRACE_PATH_VARIABLE: str(tmp_path / "traces.sqlite3"),
    }
    first = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **files))
    first.dependency_overrides[plan_graphs] = scripted_graphs(
        lambda: [fixture_week_plan()], lambda: [accepting()]
    )
    with TestClient(first, headers=SAME_ORIGIN) as client:
        started = client.post("/parent/plans", json={}).json()
        assert started["waiting"] is True

    later = PLAN_DATE + timedelta(days=PAUSED_RETENTION_DAYS + 1)
    second = create_app(fixture_settings(BLOSSOM_TODAY=later.isoformat(), **files))
    with TestClient(second, headers=SAME_ORIGIN) as client:
        page = client.get("/parent").text
        queue = client.get("/parent/approvals").json()

    assert queue["waiting"] == []
    assert "Expired." in page
    assert "The evening passed without a review." in page
    assert "Reason:" not in page


def test_planning_again_clears_the_thread_of_the_plan_it_replaces() -> None:
    """The superseded draft can never be resumed, so its saved state goes at once."""
    state = application()
    try:

        async def scenario() -> tuple[str, str, set[str]]:
            first = await plan_evening(
                graph_for(state, fixture_week_plan()),
                PLAN_DATE,
                state,
            )
            second = await plan_evening(
                graph_for(state, fixture_week_plan()),
                PLAN_DATE,
                state,
            )
            await detached_done(state)
            return first.thread_id, second.thread_id, await thread_ids(state)

        first, second, threads = asyncio.run(scenario())
    finally:
        state.close()

    assert first != second
    assert threads == {second}


class SaverFailingAfterCompose(InMemorySaver):
    """A saver that cannot write the checkpoint carrying the draft, once armed.

    The draft is in the table by then, since ``compose`` saved it in its own
    transaction, and the run has not paused. This is the gap between the two.
    """

    armed = False

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        if self.armed and checkpoint["channel_values"].get("draft") is not None:
            msg = "the checkpoint could not be written"
            raise RuntimeError(msg)
        return await super().aput(config, checkpoint, metadata, new_versions)


def test_a_run_that_fails_after_saving_its_draft_takes_it_back() -> None:
    """Whatever the pages showed before the run is what they show after the failure."""
    saver = SaverFailingAfterCompose()
    state = application(saver=saver)
    try:

        async def scenario() -> tuple[str, set[str]]:
            first = await plan_evening(
                graph_for(state, fixture_week_plan()),
                PLAN_DATE,
                state,
            )
            saver.armed = True
            with pytest.raises(RuntimeError, match="checkpoint could not be written"):
                await plan_evening(
                    graph_for(state, fixture_week_plan()),
                    PLAN_DATE,
                    state,
                )
            await detached_done(state)
            return first.thread_id, await thread_ids(state)

        first, threads = asyncio.run(scenario())
        waiting = state.drafts.waiting()
        interrupted = state.drafts.runs_without_a_draft()
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert threads == {first}
    assert [record.thread_id for record in waiting] == [first]
    assert waiting[0].decision is None
    assert [run.outcome for run in interrupted] == ["interrupted"]
    assert interrupted[0].thread_id != first
    assert latest is not None
    assert latest.thread_id == first


class SaverFailingAfterComposeAndOnDelete(SaverFailingAfterCompose):
    """The saved-state store failing outright: no checkpoint written, no thread deleted."""

    async def adelete_thread(self, thread_id: str) -> None:
        if self.armed:
            msg = "database is locked"
            raise RuntimeError(msg)
        await super().adelete_thread(thread_id)


def test_a_failed_run_takes_its_draft_back_even_when_its_thread_cannot_be_cleared() -> None:
    """The drafts file is what the pages read; a thread that cannot go now goes at the sweep."""
    saver = SaverFailingAfterComposeAndOnDelete()
    state = application(saver=saver)
    try:

        async def scenario() -> tuple[str, set[str], set[str]]:
            first = await plan_evening(
                graph_for(state, fixture_week_plan()),
                PLAN_DATE,
                state,
            )
            saver.armed = True
            with pytest.raises(RuntimeError, match="checkpoint could not be written"):
                await plan_evening(
                    graph_for(state, fixture_week_plan()),
                    PLAN_DATE,
                    state,
                )
            before_sweep = await thread_ids(state)
            saver.armed = False
            await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
            return first.thread_id, before_sweep, await thread_ids(state)

        first, before_sweep, after_sweep = asyncio.run(scenario())
        waiting = state.drafts.waiting()
        interrupted = state.drafts.runs_without_a_draft()
    finally:
        state.close()

    assert [record.thread_id for record in waiting] == [first]
    assert [run.outcome for run in interrupted] == ["interrupted"]
    assert len(before_sweep) == 2
    assert after_sweep == {first}


def test_the_sweep_takes_back_a_draft_whose_run_died_before_pausing() -> None:
    """A process dying between the draft's save and its checkpoint leaves no route to tidy up,
    so the sweep ends the run once its time is out, taking its draft back."""
    saver = SaverFailingAfterCompose()
    time_now = FakeTime()
    state = application(saver=saver, monotonic=time_now)
    try:

        async def scenario() -> tuple[str, Swept, set[str]]:
            first = await plan_evening(
                graph_for(state, fixture_week_plan()),
                PLAN_DATE,
                state,
            )
            saver.armed = True
            with pytest.raises(RuntimeError, match="checkpoint could not be written"):
                await graph_for(state, fixture_week_plan()).ainvoke(
                    PlanState(plan_date=PLAN_DATE, rounds=0),
                    config=run_config(admitted(state, "plan:crashed")),
                    durability=DURABILITY,
                )
            saver.armed = False
            time_now.now += RUN_DEADLINE_SECONDS + 1
            swept = await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
            return first.thread_id, swept, await thread_ids(state)

        first, swept, threads = asyncio.run(scenario())
        waiting = state.drafts.waiting()
        latest = state.drafts.latest_for(PLAN_DATE)
        interrupted = state.drafts.runs_without_a_draft()
        taken_back = state.drafts.get("draft:plan:crashed")
    finally:
        state.close()

    assert swept.ended == ("plan:crashed",)
    assert taken_back is None
    assert swept.cleared == ("plan:crashed",)
    assert [record.thread_id for record in waiting] == [first]
    assert waiting[0].decision is None
    assert latest is not None
    assert latest.thread_id == first
    assert [run.thread_id for run in interrupted] == ["plan:crashed"]


class DisplacedDuringReview:
    """A graph whose resume first publishes a later draft, as another process might have."""

    def __init__(self, inner: CompiledPlanGraph, state: ApplicationState) -> None:
        self._inner = inner
        self._state = state

    async def aget_state(self, config: RunnableConfig) -> StateSnapshot:
        return await self._inner.aget_state(config)

    async def ainvoke(
        self, resume: Command[Any], *, config: RunnableConfig, durability: Durability
    ) -> dict[str, object]:
        settled_run(
            self._state.drafts,
            Draft(draft_id="draft:plan:later", body="later", created_at=CREATED_LATER),
            thread_id="plan:later",
            plan_date=PLAN_DATE,
            now=self._state.monotonic,
        )
        return dict(await self._inner.ainvoke(resume, config=config, durability=durability))


class SaverSweepingBeforeTheDraft(InMemorySaver):
    """A saver in whose gap the scheduled sweep fires once, before the checkpoint with the draft."""

    sweep: Callable[[], Awaitable[Swept]] | None = None
    swept: Swept | None = None

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        if self.sweep is not None and checkpoint["channel_values"].get("draft") is not None:
            sweep, self.sweep = self.sweep, None
            self.swept = await sweep()
        return await super().aput(config, checkpoint, metadata, new_versions)


def test_the_scheduled_sweep_leaves_a_run_in_flight_and_its_draft_alone() -> None:
    """Between the draft's save and its checkpoint, a live run looks dead from the tables alone."""
    saver = SaverSweepingBeforeTheDraft()
    state = application(saver=saver)
    try:
        saver.sweep = lambda: sweep_saved_state(state.checkpointer, state.drafts, state.clock)

        async def scenario() -> tuple[str, set[str]]:
            view = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            assert view.waiting
            return view.thread_id, await thread_ids(state)

        thread, threads = asyncio.run(scenario())
        waiting = state.drafts.waiting()
        latest = state.drafts.latest_for(PLAN_DATE)
        running = state.drafts.running_threads()
    finally:
        state.close()

    assert saver.swept == Swept(expired=(), cleared=())
    assert [record.thread_id for record in waiting] == [thread]
    assert latest is not None
    assert latest.thread_id == thread
    assert threads == {thread}
    assert running == frozenset()


def test_a_settle_between_the_sweeps_two_reads_of_what_to_keep_keeps_its_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settle moves a thread from the runs still running to the waiting drafts, never back,
    so a settle that commits between the sweep's two reads of what to keep is in one of them."""
    state = application()
    drafts = state.drafts
    reconcile, waiting, running_threads = (
        drafts.reconcile_runs,
        drafts.waiting,
        drafts.running_threads,
    )
    phase = ["sweeping"]
    kept_reads: list[str] = []
    first_kept_read = threading.Event()
    settled = threading.Event()
    waited_for_the_settle: list[bool] = []
    settles: list[Settled] = []

    def reconciling(wait: float = STORE_WAIT) -> list[str]:
        ended = reconcile(wait)
        phase[0] = "expiring"
        return ended

    def then_settle[T](name: str, read: Callable[[], T]) -> Callable[[], T]:
        def reading() -> T:
            found = read()
            if phase[0] == "expiring" and name == "waiting":
                # The waiting drafts read just after the ending are those checked for expiry.
                phase[0] = "keeping"
            elif phase[0] == "keeping":
                kept_reads.append(name)
                if len(kept_reads) == 1:
                    first_kept_read.set()
                    waited_for_the_settle.append(settled.wait(timeout=STORE_WAIT))
            return found

        return reading

    def settle() -> None:
        if first_kept_read.wait(timeout=STORE_WAIT):
            settles.append(drafts.settle_run("plan:settling"))
            settled.set()

    settler = threading.Thread(target=settle)
    try:

        async def scenario() -> tuple[Swept, set[str]]:
            paused = await graph_for(state, fixture_week_plan()).ainvoke(
                PlanState(plan_date=PLAN_DATE, rounds=0),
                config=run_config(admitted(state, "plan:settling")),
                durability=DURABILITY,
            )
            assert "__interrupt__" in paused
            monkeypatch.setattr(drafts, "reconcile_runs", reconciling)
            monkeypatch.setattr(drafts, "waiting", then_settle("waiting", waiting))
            monkeypatch.setattr(
                drafts, "running_threads", then_settle("running_threads", running_threads)
            )
            settler.start()
            swept = await sweep_saved_state(state.checkpointer, drafts, state.clock)
            return swept, await thread_ids(state)

        swept, remaining = asyncio.run(scenario())
        settler.join(timeout=2 * STORE_WAIT)
        assert not settler.is_alive()
        waiting_after = waiting()
        latest = drafts.latest_for(PLAN_DATE)
        run = drafts.run_status("plan:settling", reconcile=False)
    finally:
        state.close()

    assert sorted(kept_reads) == ["running_threads", "waiting"]
    assert waited_for_the_settle == [True]
    assert len(settles) == 1
    assert remaining == {"plan:settling"}
    assert swept == Swept(expired=(), cleared=())
    assert [record.thread_id for record in waiting_after] == ["plan:settling"]
    assert latest is not None
    assert (latest.thread_id, latest.published, latest.decision) == ("plan:settling", True, None)
    assert run is not None
    assert run.status == "published"


class SaverRefusingDeletes(InMemorySaver):
    """A saver that cannot delete a thread, once armed."""

    armed = False

    async def adelete_thread(self, thread_id: str) -> None:
        if self.armed:
            msg = "database is locked"
            raise RuntimeError(msg)
        await super().adelete_thread(thread_id)


def test_a_thread_that_cannot_be_tidied_after_a_pause_does_not_fail_the_run() -> None:
    """The run's outcome is what the caller hears; the thread goes at the next sweep."""
    saver = SaverRefusingDeletes()
    state = application(saver=saver)
    try:

        async def scenario() -> tuple[str, str, set[str], set[str]]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            saver.armed = True
            second = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            assert second.waiting
            before = await thread_ids(state)
            saver.armed = False
            await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
            return first.thread_id, second.thread_id, before, await thread_ids(state)

        first, second, before, after = asyncio.run(scenario())
        waiting = state.drafts.waiting()
    finally:
        state.close()

    assert [record.thread_id for record in waiting] == [second]
    assert before == {first, second}
    assert after == {second}


def test_a_review_whose_record_failed_is_finished_by_the_next_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate passed the decision on; the table refused once; the thread holds the decision."""
    state = application()
    try:
        original = state.drafts.record_decision
        calls = {"n": 0}

        def failing_once(
            draft_id: str, *, status: DraftStatus, decision: Decision, reason: str | None
        ) -> DraftRecord:
            calls["n"] += 1
            if calls["n"] == 1:
                msg = "database is locked"
                raise RuntimeError(msg)
            return original(draft_id, status=status, decision=decision, reason=reason)

        monkeypatch.setattr(state.drafts, "record_decision", failing_once)

        async def scenario() -> tuple[str, str, int, set[str]]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            assert first.draft_id is not None
            approve = DecisionRequest(approved=True, reason="fine")
            with pytest.raises(RuntimeError, match="database is locked"):
                await decide_draft(state, lambda: graph_for(state), first.draft_id, approve)
            still_waiting = state.drafts.get(first.draft_id)
            assert still_waiting is not None
            assert still_waiting.waiting
            try:
                await decide_draft(
                    state,
                    lambda: graph_for(state),
                    first.draft_id,
                    DecisionRequest(approved=False, reason="changed my mind"),
                )
            except HTTPException as error:
                disagreeing = error.status_code
            else:
                msg = "a request that disagreed with the decision the thread held was accepted"
                raise AssertionError(msg)
            return first.draft_id, first.thread_id, disagreeing, await thread_ids(state)

        draft_id, thread, disagreeing, threads = asyncio.run(scenario())
        decided = state.drafts.get(draft_id)
    finally:
        state.close()

    assert disagreeing == 409
    assert decided is not None
    assert decided.decision == "approved"
    assert decided.reason == "fine"
    assert threads == set()
    assert thread not in threads


def test_a_decision_that_landed_is_answered_even_when_its_thread_cannot_be_cleared() -> None:
    saver = SaverRefusingDeletes()
    state = application(saver=saver)
    try:

        async def scenario() -> tuple[str, str, set[str], set[str]]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            assert first.draft_id is not None
            saver.armed = True
            view = await decide_draft(
                state,
                lambda: graph_for(state),
                first.draft_id,
                DecisionRequest(approved=True, reason=None),
            )
            before = await thread_ids(state)
            saver.armed = False
            await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
            return view.decision, first.thread_id, before, await thread_ids(state)

        decision, thread, before, after = asyncio.run(scenario())
    finally:
        state.close()

    assert decision == "approved"
    assert before == {thread}
    assert after == set()


def failing_once_then(store: DraftsStore) -> Callable[..., DraftRecord]:
    """The store's record_decision failing on its first call, as a locked file would make it."""
    original = store.record_decision
    calls = {"n": 0}

    def record(
        draft_id: str, *, status: DraftStatus, decision: Decision, reason: str | None
    ) -> DraftRecord:
        calls["n"] += 1
        if calls["n"] == 1:
            msg = "database is locked"
            raise RuntimeError(msg)
        return original(draft_id, status=status, decision=decision, reason=reason)

    return record


def test_the_sweep_finishes_a_review_the_thread_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A review that reached the thread is never lost to expiry or to nobody pressing again."""
    state = application()
    try:
        monkeypatch.setattr(state.drafts, "record_decision", failing_once_then(state.drafts))

        async def scenario() -> tuple[str, Swept, set[str]]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            assert first.draft_id is not None
            with pytest.raises(RuntimeError, match="database is locked"):
                await decide_draft(
                    state,
                    lambda: graph_for(state),
                    first.draft_id,
                    DecisionRequest(approved=False, reason="two sittings"),
                )
            swept = await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
            return first.draft_id, swept, await thread_ids(state)

        draft_id, swept, threads = asyncio.run(scenario())
        decided = state.drafts.get(draft_id)
    finally:
        state.close()

    assert swept.finished == (draft_id,)
    assert swept.expired == ()
    assert decided is not None
    assert decided.decision == "rejected"
    assert decided.reason == "two sittings"
    assert decided.status == DraftStatus.DRAFT
    assert threads == set()


def test_a_held_review_is_finished_whatever_the_evening_says_by_then(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Her signal after the review changes nothing: the review was checked against its evening."""
    state = application()
    try:
        monkeypatch.setattr(state.drafts, "record_decision", failing_once_then(state.drafts))

        async def scenario() -> tuple[str, str]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            assert first.draft_id is not None
            approve = DecisionRequest(approved=True, reason="fine")
            with pytest.raises(RuntimeError, match="database is locked"):
                await decide_draft(state, lambda: graph_for(state), first.draft_id, approve)
            state.workload_signals.record(PLAN_DATE)
            view = await decide_draft(state, lambda: graph_for(state), first.draft_id, approve)
            return first.draft_id, view.decision

        draft_id, decision = asyncio.run(scenario())
        decided = state.drafts.get(draft_id)
    finally:
        state.close()

    assert decision == "approved"
    assert decided is not None
    assert decided.decision == "approved"


class SaverActingBeforeTheDraft(InMemorySaver):
    """A saver that lets the test act once in a run's gap, before the checkpoint with the draft."""

    act: Callable[[], None] | None = None

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        if self.act is not None and checkpoint["channel_values"].get("draft") is not None:
            act, self.act = self.act, None
            act()
        return await super().aput(config, checkpoint, metadata, new_versions)


def test_a_review_that_meets_a_later_plan_is_refused_and_its_spent_thread_goes() -> None:
    """Publication in this process waits for the lock a review holds; another process would not."""
    state = application()
    try:

        async def scenario() -> tuple[str, int, set[str]]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            assert first.draft_id is not None
            build = lambda: cast(  # noqa: E731
                "CompiledPlanGraph", DisplacedDuringReview(graph_for(state), state)
            )
            try:
                await decide_draft(
                    state, build, first.draft_id, DecisionRequest(approved=True, reason=None)
                )
            except HTTPException as error:
                return first.draft_id, error.status_code, await thread_ids(state)
            msg = "the review was accepted although a later plan had taken the draft's place"
            raise AssertionError(msg)

        draft_id, status_code, threads = asyncio.run(scenario())
        displaced = state.drafts.get(draft_id)
        waiting = state.drafts.waiting()
    finally:
        state.close()

    assert status_code == 409
    assert threads == set()
    assert displaced is not None
    assert displaced.decision == "superseded"
    assert [record.draft_id for record in waiting] == ["draft:plan:later"]


def test_a_draft_is_on_no_page_until_its_run_has_paused() -> None:
    """In the gap between the save and the pause, both pages still show the plan before it."""
    saver = SaverActingBeforeTheDraft()
    state = application(saver=saver)
    try:
        seen: dict[str, object] = {}

        def look_at_the_pages() -> None:
            latest = state.drafts.latest_for(PLAN_DATE)
            seen["latest"] = None if latest is None else latest.thread_id
            seen["waiting"] = [record.thread_id for record in state.drafts.waiting()]
            seen["unpublished"] = [record.thread_id for record in state.drafts.unpublished()]

        async def scenario() -> tuple[str, str]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            saver.act = look_at_the_pages
            second = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            return first.thread_id, second.thread_id

        first, second = asyncio.run(scenario())
        waiting_after = [record.thread_id for record in state.drafts.waiting()]
        latest_after = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert seen["latest"] == first
    assert seen["waiting"] == [first]
    assert seen["unpublished"] == [second]
    assert waiting_after == [second]
    assert latest_after is not None
    assert latest_after.thread_id == second


def test_publication_waits_for_a_review_in_progress() -> None:
    """The lock a review holds is the lock a run needs to publish: the run reaches its
    gate, and the pages keep the plan before it until the review lets the lock go."""
    state = application()
    try:

        async def scenario() -> tuple[str, str, bool, list[str], set[str], set[str]]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            assert await asyncio.wait_for(state.decision_lock.acquire(), timeout=STORE_WAIT)
            pausing = asyncio.create_task(
                plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            )
            await asyncio.sleep(0.3)
            done_while_held = pausing.done()
            waiting_while_held = [record.thread_id for record in state.drafts.waiting()]
            threads_while_held = await thread_ids(state)
            state.decision_lock.release()
            second = await pausing
            await detached_done(state)
            return (
                first.thread_id,
                second.thread_id,
                done_while_held,
                waiting_while_held,
                threads_while_held,
                await thread_ids(state),
            )

        first, second, done_while_held, waiting_while_held, while_held, after = asyncio.run(
            scenario()
        )
        waiting = [record.thread_id for record in state.drafts.waiting()]
    finally:
        state.close()

    assert done_while_held is False
    assert waiting_while_held == [first]
    assert first in while_held
    assert after == {second}
    assert waiting == [second]


def test_a_publication_that_fails_is_a_failed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The page then says nothing changed, and nothing did: not now, and not at the next sweep."""
    state = application()
    try:

        def refusing(run_id: str, **_: object) -> Settled:
            msg = "database or disk is full"
            raise RuntimeError(msg)

        async def scenario() -> tuple[str, set[str], Swept]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            monkeypatch.setattr(state.drafts, "settle_run", refusing)
            with pytest.raises(NotSaved):
                await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            monkeypatch.undo()
            await detached_done(state)
            threads = await thread_ids(state)
            swept = await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
            return first.thread_id, threads, swept

        first, threads, swept = asyncio.run(scenario())
        waiting = [record.thread_id for record in state.drafts.waiting()]
        unpublished = state.drafts.unpublished()
        interrupted = [run.outcome for run in state.drafts.runs_without_a_draft()]
        running = state.drafts.running_threads()
    finally:
        state.close()

    assert threads == {first}
    assert waiting == [first]
    assert unpublished == []
    assert interrupted == ["interrupted"]
    assert swept.ended == ()
    assert running == frozenset()


class SaverLosingTheInterrupt(InMemorySaver):
    """A saver that writes the checkpoint before the gate and then cannot write the pause."""

    armed = False

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        if self.armed and any(channel == "__interrupt__" for channel, _ in writes):
            msg = "the pause could not be written"
            raise RuntimeError(msg)
        await super().aput_writes(config, writes, task_id, task_path)


def test_a_draft_whose_thread_never_paused_is_taken_back_not_published() -> None:
    """The checkpoint before the gate carries the draft; without the pause nothing can resume
    it, so once the run's time is out the sweep ends it and takes the draft back."""
    saver = SaverLosingTheInterrupt()
    time_now = FakeTime()
    state = application(saver=saver, monotonic=time_now)
    try:

        async def scenario() -> tuple[str, Swept, set[str]]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            saver.armed = True
            with pytest.raises(RuntimeError, match="pause could not be written"):
                await graph_for(state, fixture_week_plan()).ainvoke(
                    PlanState(plan_date=PLAN_DATE, rounds=0),
                    config=run_config(admitted(state, "plan:unpaused")),
                    durability=DURABILITY,
                )
            saver.armed = False
            time_now.now += RUN_DEADLINE_SECONDS + 1
            swept = await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
            return first.thread_id, swept, await thread_ids(state)

        first, swept, threads = asyncio.run(scenario())
        waiting = [record.thread_id for record in state.drafts.waiting()]
        taken_back = state.drafts.get("draft:plan:unpaused")
    finally:
        state.close()

    assert swept.ended == ("plan:unpaused",)
    assert taken_back is None
    assert waiting == [first]
    assert threads == {first}


def test_a_retry_with_the_same_button_and_other_words_is_told_the_first_words_stand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = application()
    try:
        monkeypatch.setattr(state.drafts, "record_decision", failing_once_then(state.drafts))

        async def scenario() -> tuple[str, int]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            assert first.draft_id is not None
            with pytest.raises(RuntimeError, match="database is locked"):
                await decide_draft(
                    state,
                    lambda: graph_for(state),
                    first.draft_id,
                    DecisionRequest(approved=True, reason="fine"),
                )
            try:
                await decide_draft(
                    state,
                    lambda: graph_for(state),
                    first.draft_id,
                    DecisionRequest(approved=True, reason="fine, but start earlier"),
                )
            except HTTPException as error:
                return first.draft_id, error.status_code
            msg = "a retry with other words was reported as its own success"
            raise AssertionError(msg)

        draft_id, status_code = asyncio.run(scenario())
        decided = state.drafts.get(draft_id)
    finally:
        state.close()

    assert status_code == 409
    assert decided is not None
    assert decided.decision == "approved"
    assert decided.reason == "fine"


def test_a_review_the_thread_holds_is_recorded_before_a_later_plan_takes_its_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A review whose record failed, then a new plan: the review lands, and the plan is hers."""
    state = application()
    try:
        monkeypatch.setattr(state.drafts, "record_decision", failing_once_then(state.drafts))

        async def scenario() -> tuple[str, str, set[str]]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            assert first.draft_id is not None
            with pytest.raises(RuntimeError, match="database is locked"):
                await decide_draft(
                    state,
                    lambda: graph_for(state),
                    first.draft_id,
                    DecisionRequest(approved=True, reason="good pacing"),
                )
            second = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            return first.draft_id, second.thread_id, await thread_ids(state)

        draft_id, second, threads = asyncio.run(scenario())
        reviewed = state.drafts.get(draft_id)
        waiting = [record.thread_id for record in state.drafts.waiting()]
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert reviewed is not None
    assert reviewed.decision == "approved"
    assert reviewed.reason == "good pacing"
    assert waiting == [second]
    assert latest is not None
    assert latest.thread_id == second
    assert threads == {second}


class HeldUntil:
    """A planner whose one answer waits for the test to let it go."""

    def __init__(self) -> None:
        self.go = asyncio.Event()

    async def __call__(self, messages: Sequence[object]) -> ModelAnswer[DailyPlan]:
        await self.go.wait()
        return ok(fixture_week_plan())


class Counted(HeldUntil):
    """A held planner that counts how often it was asked."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def __call__(self, messages: Sequence[object]) -> ModelAnswer[DailyPlan]:
        self.calls += 1
        return await super().__call__(messages)


def test_two_presses_for_one_evening_start_one_run() -> None:
    """Both presses arrive together. One run starts and asks its model; the other is refused
    before any thread is written or model asked, and once the first ends a press starts a new
    run again."""
    state = application()
    try:

        async def scenario() -> tuple[list[PlanRunView | BaseException], int, str, str]:
            first, second = Counted(), Counted()
            presses = [
                asyncio.create_task(
                    plan_evening(
                        plan_graph_for(state, planner=asked, critic=Scripted(ok(accepting()))),
                        PLAN_DATE,
                        state,
                    )
                )
                for asked in (first, second)
            ]
            await asyncio.sleep(0.2)
            first.go.set()
            second.go.set()
            ended = await asyncio.gather(*presses, return_exceptions=True)
            later = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            made = [view.thread_id for view in ended if not isinstance(view, BaseException)]
            return ended, first.calls + second.calls, made[0], later.thread_id

        ended, calls, made, later = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
        recorded = state.drafts.runs_without_a_draft()
        running = state.drafts.running_threads()
    finally:
        state.close()

    refused = [item for item in ended if isinstance(item, BaseException)]
    assert len(refused) == 1
    assert isinstance(refused[0], AlreadyPlanning)
    assert refused[0].status_code == 409
    assert cast("dict[str, object]", refused[0].detail)["run_id"] == made
    assert calls == 1
    assert recorded == []
    assert latest is not None
    assert latest.thread_id == later != made
    assert running == frozenset()


def test_a_run_overtaken_by_a_newer_plan_never_replaces_it() -> None:
    """A plan for the evening is published while a run is working, as another process might
    publish one. When the run's answer comes, the published plan stays today's; the late
    draft is taken back and never shown, its run is kept as overtaken, and its thread is
    cleared."""
    state = application()
    try:

        async def scenario() -> tuple[str, str, set[str]]:
            slow = HeldUntil()
            working = asyncio.create_task(
                plan_evening(
                    plan_graph_for(state, planner=slow, critic=Scripted(ok(accepting()))),
                    PLAN_DATE,
                    state,
                )
            )
            await asyncio.sleep(0.2)
            async with state.decision_lock:
                published_elsewhere(state, "draft:plan:recovered", "plan:recovered")
            slow.go.set()
            late = await working
            await detached_done(state)
            return late.thread_id, late.outcome, await thread_ids(state)

        late, outcome, threads = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
        waiting = [record.thread_id for record in state.drafts.waiting()]
        late_draft = state.drafts.get(f"draft:{late}")
        ended = {run.thread_id: run.outcome for run in state.drafts.runs_without_a_draft()}
        running = state.drafts.running_threads()
    finally:
        state.close()

    assert outcome == "overtaken"
    assert latest is not None
    assert latest.thread_id == "plan:recovered"
    assert waiting == ["plan:recovered"]
    assert late_draft is None
    assert ended == {late: "overtaken"}
    assert late not in threads
    assert running == frozenset()


def test_a_run_started_after_a_plan_was_published_still_replaces_it() -> None:
    """Planning again is how a newer plan is made: a run that starts after the evening's
    last publication is not overtaken by it."""
    state = application()
    try:

        async def scenario() -> tuple[str, str]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            second = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            return first.thread_id, second.thread_id

        first, second = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert latest is not None
    assert latest.thread_id == second != first


def test_a_run_that_times_out_publishes_nothing_and_leaves_no_thread() -> None:
    """Today's plan and its thread are as they were; the run that ran out of time is on
    record as timed out and nothing of it waits or is saved."""
    state = application()
    clock = FakeTime()
    try:

        async def scenario() -> tuple[str, str, set[str]]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            slow: Spending[DailyPlan] = Spending(
                clock, (RUN_DEADLINE_SECONDS, ok(forgetful_fixture_plan()))
            )
            late = await plan_evening(
                plan_graph_for(state, planner=slow, critic=Scripted()),
                PLAN_DATE,
                state,
                budget=clock.budget(),
            )
            await detached_done(state)
            return first.thread_id, late.outcome, await thread_ids(state)

        first, outcome, threads = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
        waiting = [record.thread_id for record in state.drafts.waiting()]
        unpublished = state.drafts.unpublished()
        ended = [run.outcome for run in state.drafts.runs_without_a_draft()]
        running = state.drafts.running_threads()
    finally:
        state.close()

    assert outcome == "timed_out"
    assert latest is not None
    assert latest.thread_id == first
    assert waiting == [first]
    assert unpublished == []
    assert ended == ["timed_out"]
    assert threads == {first}
    assert running == frozenset()


@pytest.mark.parametrize("failures", [1, 2])
def test_a_failed_admission_leaves_no_run_in_flight(failures: int) -> None:
    """The admission fails before the run starts. The press can't start, no run is on
    record, and once the store works again each press makes a plan."""
    state = application()
    admit = state.drafts.admit_run
    left = [failures]

    def failing_then_admitting(run_id: str, **arguments: Any) -> RunState | None:  # noqa: ANN401
        if left[0]:
            left[0] -= 1
            msg = "disk I/O error"
            raise sqlite3.OperationalError(msg)
        return admit(run_id, **arguments)

    state.drafts.admit_run = failing_then_admitting  # type: ignore[method-assign]
    try:

        async def scenario() -> tuple[int, list[str], int]:
            for _ in range(failures):
                with pytest.raises(CouldNotStart) as refused:
                    await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
                assert refused.value.status_code == 503
            in_flight_after = len(state.drafts.running_threads()) + (
                state.drafts.latest_run() is not None
            )
            planners = [Counted(), Counted()]
            outcomes = []
            for planner in planners:
                planner.go.set()
                view = await plan_evening(
                    plan_graph_for(state, planner=planner, critic=Scripted(ok(accepting()))),
                    PLAN_DATE,
                    state,
                )
                outcomes.append(view.outcome)
            return in_flight_after, outcomes, sum(planner.calls for planner in planners)

        in_flight_after, outcomes, calls = asyncio.run(scenario())
        ended = state.drafts.runs_without_a_draft()
        running = state.drafts.running_threads()
    finally:
        state.close()

    assert in_flight_after == 0
    assert outcomes == ["accepted", "accepted"]
    assert calls == 2
    assert ended == []
    assert running == frozenset()


type Waited = tuple[str, PlanRunView, DraftRecord | None, list[RunRecord], set[str]]


def published_after_a_wait(seconds: float, waited: float) -> Waited:
    """Today's plan, then a run whose model answers at once and whose publication waits on
    the decision lock while ``waited`` seconds of the run's ``seconds`` pass."""
    clock = FakeTime()
    state = application()
    try:

        async def scenario() -> tuple[str, PlanRunView, set[str], set[str]]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            answered = HeldUntil()
            working = asyncio.create_task(
                plan_evening(
                    plan_graph_for(state, planner=answered, critic=Scripted(ok(accepting()))),
                    PLAN_DATE,
                    state,
                    budget=clock.budget(seconds),
                )
            )
            await asyncio.sleep(0.2)
            async with state.decision_lock:
                answered.go.set()
                await asyncio.sleep(0.3)
                clock.now += waited
                in_flight_while_held = set(state.drafts.running_threads())
            late = await working
            await detached_done(state)
            return first.thread_id, late, in_flight_while_held, await thread_ids(state)

        first, late, in_flight_while_held, threads = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
        ended = state.drafts.runs_without_a_draft()
        running = state.drafts.running_threads()
    finally:
        state.close()
    assert in_flight_while_held == {late.thread_id}
    assert running == frozenset()
    assert not state.decision_lock.locked()
    return first, late, latest, ended, threads


@pytest.mark.parametrize(("seconds", "waited"), [(2.0, 2.328), (2.0, 2.0), (90.0, 90.0)])
def test_a_run_whose_time_runs_out_waiting_to_publish_publishes_nothing(
    seconds: float, waited: float
) -> None:
    """The model answered in time, but the run's time ran out while it waited for the lock
    it publishes under. Today's plan stays, the late draft and its thread are taken back,
    and the run is kept as timed out."""
    first, late, latest, ended, threads = published_after_a_wait(seconds, waited)

    assert late.outcome == "timed_out"
    assert (late.draft_id, late.waiting) == (None, False)
    assert latest is not None
    assert latest.thread_id == first
    assert [(run.thread_id, run.outcome) for run in ended] == [(late.thread_id, "timed_out")]
    assert ended[0].timing is not None
    assert ended[0].timing.category == "timeout"
    assert ended[0].steps[-1].found == f"The run's {RUN_DEADLINE_SECONDS:g} seconds ran out."
    assert threads == {first}


@pytest.mark.parametrize(("seconds", "waited"), [(5.0, 2.328), (2.0, 1.9)])
def test_a_run_that_gets_the_lock_in_time_publishes(seconds: float, waited: float) -> None:
    first, late, latest, ended, threads = published_after_a_wait(seconds, waited)

    assert late.outcome == "accepted"
    assert latest is not None
    assert latest.thread_id == late.thread_id != first
    assert ended == []
    assert threads == {late.thread_id}


@pytest.mark.parametrize("where", ["start", "publication"])
def test_a_run_waits_for_the_decision_lock_only_until_its_time_runs_out(where: str) -> None:
    """On the process's own clock, the lock is held past the run's limit, from before the
    run starts or from once its model has answered. The run asks its model, waits for the
    lock only to publish, and ends at its limit, timed out, while the lock is still held;
    nothing of it is published or kept in flight."""
    state = application()
    try:

        async def scenario() -> tuple[PlanRunView, bool, int]:
            answered = Counted()
            graph = plan_graph_for(state, planner=answered, critic=Scripted(ok(accepting())))
            budget = RunBudget(seconds=1.0)
            if where == "start":
                answered.go.set()
                async with state.decision_lock:
                    working = asyncio.create_task(
                        plan_evening(graph, PLAN_DATE, state, budget=budget)
                    )
                    await asyncio.sleep(2.5)
                    ended_while_held = working.done()
            else:
                working = asyncio.create_task(plan_evening(graph, PLAN_DATE, state, budget=budget))
                await asyncio.sleep(0.2)
                async with state.decision_lock:
                    answered.go.set()
                    await asyncio.sleep(2.5)
                    ended_while_held = working.done()
            late = await working
            await detached_done(state)
            return late, ended_while_held, answered.calls

        late, ended_while_held, calls = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
        ended = state.drafts.runs_without_a_draft()
        threads = asyncio.run(thread_ids(state))
        running = state.drafts.running_threads()
    finally:
        state.close()

    assert late.outcome == "timed_out"
    assert ended_while_held
    assert calls == 1
    assert latest is None
    assert [(run.thread_id, run.outcome) for run in ended] == [(late.thread_id, "timed_out")]
    assert ended[0].timing is not None
    assert ended[0].timing.category == "timeout"
    assert threads == set()
    assert running == frozenset()
    assert not state.decision_lock.locked()


class SlowToRead(InMemorySaver):
    """A saver whose read of one thread takes ``seconds``: waited out on the process's
    clock, or moved on a ``FakeTime`` with no wait, as a read that holds the loop would.
    With ``error`` the read raises it instead."""

    def __init__(self) -> None:
        super().__init__()
        self.slow: str | None = None
        self.seconds = 0.0
        self.clock: FakeTime | None = None
        self.error: Exception | None = None

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        if config["configurable"]["thread_id"] == self.slow:
            if self.error is not None:
                raise self.error
            if self.clock is None:
                await asyncio.sleep(self.seconds)
            else:
                self.clock.now += self.seconds
        return await super().aget_tuple(config)


type SlowRead = tuple[str, PlanRunView, DraftRecord | None, list[RunRecord], set[str], PlanRunView]


def published_after_a_slow_read(
    state: ApplicationState, saver: SlowToRead, seconds: float, read: float, *, waits: bool
) -> SlowRead:
    """Today's plan, then a run whose model answers at once and whose publication reads that
    plan's thread for a review it holds, the read taking ``read`` of the run's ``seconds``.
    Then a press with time to spare."""
    clock = FakeTime()

    async def scenario() -> SlowRead:
        first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
        saver.slow, saver.seconds, saver.clock = first.thread_id, read, None if waits else clock
        budget = RunBudget(seconds=seconds) if waits else clock.budget(seconds)
        late = await plan_evening(
            graph_for(state, fixture_week_plan()), PLAN_DATE, state, budget=budget
        )
        await detached_done(state)
        latest = state.drafts.latest_for(PLAN_DATE)
        ended = state.drafts.runs_without_a_draft()
        threads = await thread_ids(state)
        assert state.drafts.running_threads() == frozenset()
        assert not state.decision_lock.locked()
        saver.slow = None
        again = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
        return first.thread_id, late, latest, ended, threads, again

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    ("waits", "seconds", "read"),
    [(True, 2.0, 2.3), (False, 2.0, 2.3), (False, 2.0, 2.0), (False, 90.0, 90.001)],
)
def test_a_run_whose_time_runs_out_reading_held_reviews_publishes_nothing(
    waits: bool, seconds: float, read: float
) -> None:
    """The run got the lock in time, but reading today's plan's thread for a held review
    spent the rest. Today's plan stays, the late draft and its thread are taken back, the
    run is kept as timed out, and the next press publishes."""
    saver = SlowToRead()
    state = application(saver=saver)
    try:
        first, late, latest, ended, threads, again = published_after_a_slow_read(
            state, saver, seconds, read, waits=waits
        )
        latest_after = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert late.outcome == "timed_out"
    assert (late.draft_id, late.waiting) == (None, False)
    assert latest is not None
    assert latest.thread_id == first
    assert [(run.thread_id, run.outcome) for run in ended] == [(late.thread_id, "timed_out")]
    assert ended[0].timing is not None
    assert ended[0].timing.category == "timeout"
    assert ended[0].steps[-1].found == f"The run's {RUN_DEADLINE_SECONDS:g} seconds ran out."
    assert threads == {first}
    assert again.outcome == "accepted"
    assert latest_after is not None
    assert latest_after.thread_id == again.thread_id


@pytest.mark.parametrize("waits", [True, False])
def test_a_review_held_through_a_publication_out_of_time_still_lands(
    monkeypatch: pytest.MonkeyPatch, waits: bool
) -> None:
    """A review of today's plan whose record failed is held in its thread when a run runs
    out of time reading it. The review is not lost: by the next press it is recorded, and
    that press's plan is published."""
    saver = SlowToRead()
    state = application(saver=saver)
    clock = FakeTime()
    try:
        monkeypatch.setattr(state.drafts, "record_decision", failing_once_then(state.drafts))

        async def scenario() -> tuple[str, PlanRunView, PlanRunView]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            assert first.draft_id is not None
            with pytest.raises(RuntimeError, match="database is locked"):
                await decide_draft(
                    state,
                    lambda: graph_for(state),
                    first.draft_id,
                    DecisionRequest(approved=True, reason="good pacing"),
                )
            saver.slow, saver.seconds, saver.clock = first.thread_id, 2.3, None if waits else clock
            budget = RunBudget(seconds=2.0) if waits else clock.budget(2.0)
            late = await plan_evening(
                graph_for(state, fixture_week_plan()), PLAN_DATE, state, budget=budget
            )
            saver.slow = None
            again = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            return first.draft_id, late, again

        draft_id, late, again = asyncio.run(scenario())
        reviewed = state.drafts.get(draft_id)
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert late.outcome == "timed_out"
    assert reviewed is not None
    assert (reviewed.decision, reviewed.reason) == ("approved", "good pacing")
    assert again.outcome == "accepted"
    assert latest is not None
    assert latest.thread_id == again.thread_id


@pytest.mark.parametrize("where", ["read", "publish"])
def test_a_timeout_error_that_is_not_the_runs_limit_is_a_failed_publication(
    monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """A store raises a ``TimeoutError`` of its own while the run has time left. The
    publication failed, as with any other error, and the run isn't kept as out of time.
    A settle that raises is read back, finds the run still running, and isn't saved."""
    saver = SlowToRead()
    state = application(saver=saver)
    try:

        def timing_out(run_id: str, **_: object) -> Settled:
            msg = "the store timed out"
            raise TimeoutError(msg)

        async def scenario() -> tuple[str, set[str]]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            if where == "read":
                saver.slow, saver.error = first.thread_id, TimeoutError("the store timed out")
                with pytest.raises(TimeoutError, match="the store timed out"):
                    await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            else:
                monkeypatch.setattr(state.drafts, "settle_run", timing_out)
                with pytest.raises(NotSaved):
                    await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            await detached_done(state)
            return first.thread_id, await thread_ids(state)

        first, threads = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
        ended = [run.outcome for run in state.drafts.runs_without_a_draft()]
        running = state.drafts.running_threads()
    finally:
        state.close()

    assert latest is not None
    assert latest.thread_id == first
    assert ended == ["interrupted"]
    assert threads == {first}
    assert running == frozenset()
    assert not state.decision_lock.locked()


class FailsToSave(InMemorySaver):
    """A saver whose reads or writes, as ``failing`` says, raise ``error`` once it is set.
    Every read first waits ``delay`` seconds."""

    def __init__(self, failing: str) -> None:
        super().__init__()
        self.failing = failing
        self.error: Exception | None = None
        self.delay = 0.0

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        if self.error is not None and self.failing == "read":
            raise self.error
        await asyncio.sleep(self.delay)
        return await super().aget_tuple(config)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        if self.error is not None and self.failing == "write":
            raise self.error
        return await super().aput(config, checkpoint, metadata, new_versions)


@pytest.mark.parametrize("error", [TimeoutError, ValueError])
@pytest.mark.parametrize("failing", ["read", "write"])
def test_a_timeout_error_from_saved_state_with_time_left_fails_the_run_like_any_other(
    failing: str, error: type[Exception]
) -> None:
    """The run's saved state raises its own ``TimeoutError`` while the run has time left.
    The run fails as it does for any other error, and is kept as interrupted, not as out
    of time."""
    saver = FailsToSave(failing)
    state = application(saver=saver)
    try:

        async def scenario() -> tuple[str, Exception, set[str]]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            saver.error = error("the store failed")
            with pytest.raises(error, match="the store failed") as raised:
                await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            saver.error = None
            await detached_done(state)
            return first.thread_id, raised.value, await thread_ids(state)

        first, failure, threads = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
        ended = [run.outcome for run in state.drafts.runs_without_a_draft()]
        running = state.drafts.running_threads()
    finally:
        state.close()

    assert type(failure) is error
    assert latest is not None
    assert latest.thread_id == first
    assert ended == ["interrupted"]
    assert threads == {first}
    assert running == frozenset()
    assert not state.decision_lock.locked()


def test_saved_state_slower_than_the_runs_time_times_the_run_out() -> None:
    """The run's first read of its saved state takes longer than the whole run may. The
    run's own limit ends it, and it is kept as timed out, not as a failure."""
    saver = FailsToSave("read")
    state = application(saver=saver)
    try:

        async def scenario() -> tuple[str, PlanRunView, list[RunRecord]]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            saver.delay = 0.6
            late = await plan_evening(
                graph_for(state, fixture_week_plan()),
                PLAN_DATE,
                state,
                budget=RunBudget(seconds=0.2),
            )
            saver.delay = 0.0
            await detached_done(state)
            return first.thread_id, late, state.drafts.runs_without_a_draft()

        first, late, ended = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
        running = state.drafts.running_threads()
    finally:
        state.close()

    assert late.outcome == "timed_out"
    assert [(run.thread_id, run.outcome) for run in ended] == [(late.thread_id, "timed_out")]
    assert latest is not None
    assert latest.thread_id == first
    assert running == frozenset()
    assert not state.decision_lock.locked()


@pytest.mark.parametrize(("seconds", "outcome"), [(2.0, "timed_out"), (5.0, "accepted")])
def test_a_publication_waiting_on_the_drafts_file_ends_at_the_runs_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, seconds: float, outcome: str
) -> None:
    """Another connection holds the drafts file's write lock for 2.3 seconds from just before
    the run publishes. With 2 seconds to run, the publication gives up by the limit and its
    grace: today's plan stays, the run is kept as timed out, and the next press publishes.
    With 5 seconds the run waits for the file and publishes."""
    path = tmp_path / "blossom.sqlite3"
    state = application(
        BLOSSOM_DATABASE_PATH=str(path),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
    )
    other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    settle = state.drafts.settle_run
    releases: list[threading.Timer] = []
    gave_up: list[float] = []

    def contended(run_id: str, **arguments: Any) -> Settled:  # noqa: ANN401
        if not releases:
            other.execute("BEGIN EXCLUSIVE")
            releases.append(threading.Timer(2.3, lambda: other.execute("ROLLBACK")))
            releases[0].start()
        try:
            return settle(run_id, **arguments)
        finally:
            gave_up.append(monotonic())

    try:

        async def scenario() -> tuple[
            str, PlanRunView, float, float, DraftRecord | None, list[RunRecord], PlanRunView
        ]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            await detached_done(state)
            monkeypatch.setattr(state.drafts, "settle_run", contended)
            started = monotonic()
            late = await plan_evening(
                graph_for(state, fixture_week_plan()),
                PLAN_DATE,
                state,
                budget=RunBudget(seconds=seconds),
            )
            answered = monotonic() - started
            releases[0].join(timeout=STORE_WAIT)
            assert not releases[0].is_alive()
            await detached_done(state)
            kept = state.drafts.latest_for(PLAN_DATE)
            ended = state.drafts.runs_without_a_draft()
            again = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            return first.thread_id, late, gave_up[0] - started, answered, kept, ended, again

        first, late, publishing, answered, kept, ended, again = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        other.close()
        state.close()

    assert late.outcome == outcome
    assert kept is not None
    assert again.outcome == "accepted"
    assert latest is not None
    assert latest.thread_id == again.thread_id
    if outcome == "timed_out":
        assert publishing < seconds + SETTLE_GRACE_SECONDS
        assert answered < seconds + SETTLE_GRACE_SECONDS
        assert kept.thread_id == first
        assert (late.draft_id, late.waiting) == (None, False)
        assert [(run.thread_id, run.outcome) for run in ended] == [(late.thread_id, "timed_out")]
        assert ended[0].timing is not None
        assert ended[0].timing.category == "timeout"
    else:
        assert publishing > 2.3
        assert kept.thread_id == late.thread_id
        assert ended == []


class HeldFile:
    """Settles a run through the drafts store while another connection holds the file for
    ``held`` seconds from just before the settle, begun with ``hold``: ``BEGIN EXCLUSIVE``
    keeps out readers too, ``BEGIN IMMEDIATE`` only the writer, as another write holds it.
    Each settle asks the event loop for a callback 0.05 seconds on and records how late it
    ran."""

    def __init__(
        self,
        state: ApplicationState,
        path: pathlib.Path,
        held: float,
        loop: asyncio.AbstractEventLoop,
        *,
        hold: str = "BEGIN EXCLUSIVE",
    ) -> None:
        self.settle = state.drafts.settle_run
        self.other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.held = held
        self.hold = hold
        self.loop = loop
        self.releases: list[threading.Timer] = []
        self.started = asyncio.Event()
        self.ended: list[float] = []
        self.late_by: asyncio.Future[float] = loop.create_future()

    def __call__(self, run_id: str, **arguments: Any) -> Settled:  # noqa: ANN401
        if self.held and not self.releases:
            self.other.execute(self.hold)
            self.releases.append(threading.Timer(self.held, self.release))
            self.releases[0].start()
        due = monotonic()

        def ran() -> None:
            self.late_by.set_result(monotonic() - due)

        self.loop.call_soon_threadsafe(self.loop.call_later, 0.05, ran)
        self.loop.call_soon_threadsafe(self.started.set)
        try:
            return self.settle(run_id, **arguments)
        finally:
            self.ended.append(monotonic())

    def release(self) -> None:
        self.other.execute("ROLLBACK")

    def close(self) -> None:
        for release in self.releases:
            release.join(timeout=STORE_WAIT)
            assert not release.is_alive()
        self.other.close()


def file_backed_application(
    tmp_path: pathlib.Path, monotonic: Callable[[], float] = monotonic
) -> ApplicationState:
    return application(
        monotonic=monotonic,
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
    )


@pytest.mark.parametrize(
    ("held", "seconds", "outcome", "hold"),
    [
        (0.7, 5.0, "accepted", "BEGIN EXCLUSIVE"),
        (2.3, 1.0, "timed_out", "BEGIN IMMEDIATE"),
        (0.0, 5.0, "accepted", "BEGIN EXCLUSIVE"),
    ],
)
def test_the_server_goes_on_answering_while_a_publication_waits_for_the_drafts_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    held: float,
    seconds: float,
    outcome: str,
    hold: str,
) -> None:
    """A callback due on the same event loop 0.05 seconds after the publication starts runs
    on time, whether the run waits for the file and publishes, runs out of time first, or
    finds the file free. The run that runs out of time meets the writer held, as another
    write holds it: on Windows, a wait against a file that keeps out readers too runs most
    of a second past its busy timeout, close to the whole grace."""
    state = file_backed_application(tmp_path)
    try:

        async def scenario() -> tuple[str, PlanRunView, float, DraftRecord | None]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            contended = HeldFile(
                state, tmp_path / "blossom.sqlite3", held, asyncio.get_running_loop(), hold=hold
            )
            monkeypatch.setattr(state.drafts, "settle_run", contended)
            try:
                late = await plan_evening(
                    graph_for(state, fixture_week_plan()),
                    PLAN_DATE,
                    state,
                    budget=RunBudget(seconds=seconds),
                )
                late_by = await contended.late_by
            finally:
                contended.close()
            await detached_done(state)
            return first.thread_id, late, late_by, state.drafts.latest_for(PLAN_DATE)

        first, late, late_by, latest = asyncio.run(scenario())
        running = state.drafts.running_threads()
    finally:
        state.close()

    assert late_by < 0.3
    assert late.outcome == outcome
    assert latest is not None
    assert latest.thread_id == (late.thread_id if outcome == "accepted" else first)
    assert running == frozenset()
    assert not state.decision_lock.locked()


@pytest.mark.parametrize("times", [1, 2])
def test_a_run_canceled_while_its_publication_waits_publishes_nothing_later(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, times: int
) -> None:
    """The run is canceled, once or twice, while its settle waits for the drafts file. The
    settle and the ending the cancel asks for meet in the store, and the record keeps
    whichever committed first: the run's plan published, or the run interrupted with
    today's plan in place. The lock is let go, no run is left running, and nothing is left
    over for the event loop to report."""
    state = file_backed_application(tmp_path)
    try:

        async def scenario() -> tuple[str, bool, list[float], bool, frozenset[str], list[str]]:
            reported: list[str] = []
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(lambda _, context: reported.append(context["message"]))
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            contended = HeldFile(state, tmp_path / "blossom.sqlite3", 1.0, loop)
            monkeypatch.setattr(state.drafts, "settle_run", contended)
            try:
                late = asyncio.create_task(
                    plan_evening(
                        graph_for(state, fixture_week_plan()),
                        PLAN_DATE,
                        state,
                        budget=RunBudget(seconds=5.0),
                    )
                )
                await asyncio.wait_for(contended.started.wait(), timeout=STORE_WAIT)
                await asyncio.sleep(0.1)
                for _ in range(times):
                    late.cancel()
                    await asyncio.sleep(0)
                (outcome,) = await asyncio.gather(late, return_exceptions=True)
                stopped = isinstance(outcome, BaseException) and not isinstance(outcome, Exception)
                locked = state.decision_lock.locked()
            finally:
                contended.close()
            await detached_done(state)
            # The settle goes on on its own thread after the cancel, until the file frees.
            settled_by = monotonic() + STORE_WAIT
            while not contended.ended and monotonic() < settled_by:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
            ended = list(contended.ended)
            in_flight = state.drafts.running_threads()
            return first.thread_id, stopped, ended, locked, in_flight, reported

        first, stopped, ended, locked, in_flight, reported = asyncio.run(scenario())
        # Whatever the run left behind is collected now, so the loop's reports of it are in.
        gc.collect()
        latest = state.drafts.latest_for(PLAN_DATE)
        run = state.drafts.latest_run()
    finally:
        state.close()

    assert stopped
    assert len(ended) == 1
    assert not locked
    assert in_flight == frozenset()
    assert reported == []
    assert latest is not None
    assert run is not None
    assert run.run_id != first
    if run.status == "published":
        assert latest.thread_id == run.run_id
    else:
        assert (run.status, run.reason) == ("ended", "interrupted")
        assert latest.thread_id == first


async def ended_short_of_publishing(
    monkeypatch: pytest.MonkeyPatch,
    state: ApplicationState,
    path: pathlib.Path,
    where: str,
    times: int,
) -> str:
    """Run a plan that ends before its plan is published, undo every fault, and return its run.

    It is stopped ``times`` times while its settle waits for the drafts file at ``path`` or
    while it waits for the decision lock to publish, or its settle fails. "held" makes the
    ending that follows fail, and "all held" the deletion of its thread too.
    """
    loop = asyncio.get_running_loop()
    contended = HeldFile(state, path, 1.0, loop)
    if where.startswith("publishing"):
        monkeypatch.setattr(state.drafts, "settle_run", contended)
    if where.startswith("failing"):

        def failing(run_id: str, **_: object) -> Settled:
            broken = "disk I/O error"
            raise sqlite3.OperationalError(broken)

        monkeypatch.setattr(state.drafts, "settle_run", failing)
    if "held" in where:

        def held(run_id: str, **_: object) -> RunState | None:
            locked = "database is locked"
            raise sqlite3.OperationalError(locked)

        monkeypatch.setattr(state.drafts, "end_run", held)
    if "all held" in where:

        async def undeletable(thread_id: str) -> None:
            locked = "database is locked"
            raise sqlite3.OperationalError(locked)

        monkeypatch.setattr(state.checkpointer, "adelete_thread", undeletable)
    # Every ending is joined before the record is read: one whose wait a second stop cut
    # goes on on its own thread, and lands whenever that thread is scheduled.
    endings: list[threading.Event] = []
    end_run = state.drafts.end_run

    def joined(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        done = threading.Event()
        endings.append(done)
        try:
            return end_run(*args, **kwargs)
        finally:
            done.set()

    monkeypatch.setattr(state.drafts, "end_run", joined)
    waiting = asyncio.Event()
    hold_in_time = plan_runs.hold_in_time

    async def noted(lock: asyncio.Lock, budget: RunBudget) -> bool:
        waiting.set()
        return await hold_in_time(lock, budget)

    monkeypatch.setattr(plan_runs, "hold_in_time", noted)
    on_the_lock = not where.startswith(("publishing", "failing"))
    try:
        if on_the_lock:
            assert await asyncio.wait_for(state.decision_lock.acquire(), timeout=STORE_WAIT)
        try:
            late = asyncio.create_task(
                plan_evening(
                    graph_for(state, fixture_week_plan()),
                    PLAN_DATE,
                    state,
                    budget=RunBudget(seconds=5.0),
                )
            )
            if where.startswith("publishing"):
                await asyncio.wait_for(contended.started.wait(), timeout=STORE_WAIT)
                await asyncio.sleep(0.1)
            elif on_the_lock:
                await asyncio.wait_for(waiting.wait(), timeout=STORE_WAIT)
                await asyncio.sleep(0.05)
            for _ in range(times):
                late.cancel()
                await asyncio.sleep(0)
            await asyncio.wait_for(
                asyncio.gather(late, return_exceptions=True), timeout=2 * STORE_WAIT
            )
        finally:
            if on_the_lock:
                state.decision_lock.release()
    finally:
        contended.close()
    # A settle the stop didn't wait for goes on on its own thread until the file frees.
    settled_by = monotonic() + STORE_WAIT
    while contended.started.is_set() and not contended.ended and monotonic() < settled_by:  # noqa: ASYNC110
        await asyncio.sleep(0.05)
    await asyncio.wait_for(detached_done(state), timeout=2 * STORE_WAIT)
    # Each way here ends the run at least once, on a thread that may not have begun yet.
    begun_by = monotonic() + 2 * STORE_WAIT
    while not endings and monotonic() < begun_by:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    assert endings
    for done in endings:
        assert await asyncio.to_thread(done.wait, 2 * STORE_WAIT)
    monkeypatch.undo()
    run = state.drafts.latest_run()
    assert run is not None
    return run.run_id


@pytest.mark.parametrize("newer", [False, True])
@pytest.mark.parametrize(
    ("where", "times"),
    [
        ("publishing", 1),
        ("publishing", 2),
        ("waiting to publish", 1),
        ("waiting to publish", 2),
        ("draft held", 1),
        ("draft held", 2),
        ("publishing, draft held", 1),
        ("publishing, draft held", 2),
        ("publishing, all held", 1),
        ("publishing, all held", 2),
        ("waiting to publish, all held", 1),
        ("waiting to publish, all held", 2),
        ("failing, all held", 0),
    ],
)
def test_a_run_canceled_before_its_plan_is_published_is_never_published_by_the_sweep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, where: str, times: int, newer: bool
) -> None:
    """A run stopped, once or twice, while its settle waits for the drafts file or while it
    waits for the decision lock to publish, or whose settle fails, keeps one account of how
    it ended: published, when its settle committed first, or interrupted with its draft and
    thread taken back. When that ending fails too, the run keeps its draft and thread until
    its deadline and then ends timed out. Either way the sweep never puts it in place of
    today's plan or of a newer one."""
    time_now = FakeTime()
    state = file_backed_application(tmp_path, monotonic=time_now)
    try:

        async def scenario() -> tuple[
            str,
            str,
            str,
            RunState | None,
            RunState | None,
            list[DraftRecord],
            set[str],
            list[RunRecord],
            DraftRecord | None,
            frozenset[str],
            set[str],
        ]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            run_id = await ended_short_of_publishing(
                monkeypatch, state, tmp_path / "blossom.sqlite3", where, times
            )
            before = state.drafts.run_status(run_id, reconcile=False)
            left = state.drafts.unpublished()
            threads = await thread_ids(state)
            ended = state.drafts.runs_without_a_draft()
            time_now.now += RUN_DEADLINE_SECONDS + 1
            published = before is not None and before.status == "published"
            newest = run_id if published else first.thread_id
            if newer:
                newest = (
                    await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
                ).thread_id
            await sweep_aged(state)
            return (
                first.thread_id,
                run_id,
                newest,
                before,
                state.drafts.run_status(run_id, reconcile=False),
                left,
                threads,
                ended,
                state.drafts.latest_for(PLAN_DATE),
                state.drafts.running_threads(),
                await thread_ids(state),
            )

        (
            first,
            run_id,
            newest,
            before,
            after,
            left,
            threads,
            ended,
            latest,
            running,
            swept_threads,
        ) = asyncio.run(scenario())
        swept = state.drafts.unpublished()
    finally:
        state.close()

    assert before is not None
    assert after is not None
    if before.status == "published":
        # The route stopped before the settle answered, so the sweep clears the displaced thread.
        assert left == []
        assert ended == []
        assert threads == {first, run_id}
    elif "held" in where or before.status == "running":
        # An ending that failed, or didn't land within its grace, leaves the run to its deadline.
        assert before.status == "running"
        assert len(left) == 1
        assert ended == []
        assert threads == {first, run_id}
    else:
        assert left == []
        (stopped,) = ended
        assert stopped.outcome == "interrupted"
        assert stopped.timing is not None
        assert stopped.timing.category == "interrupted"
        # A second stop also cuts the wait for the ending, and the tidying after it; the
        # sweep clears that thread.
        assert threads == {first} or (times == 2 and threads == {first, run_id})
        assert run_id not in swept_threads
    assert after.status == ("published" if before.status == "published" else "ended")
    if before.status == "running":
        assert after.reason == "timed_out"
    assert swept == []
    assert latest is not None
    assert latest.thread_id == newest
    assert running == frozenset()


@contextlib.asynccontextmanager
async def started(
    files: dict[str, str], monotonic: Callable[[], float] = monotonic
) -> AsyncIterator[ApplicationState]:
    """One start of the app on ``files``, startup sweep included, closed on the way out."""
    app = create_app(
        fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **files), monotonic=monotonic
    )
    async with app.router.lifespan_context(app):
        yield cast(ApplicationState, getattr(app.state, STATE_ATTRIBUTE))


def files_in(tmp_path: pathlib.Path) -> dict[str, str]:
    return {
        DATABASE_PATH_VARIABLE: str(tmp_path / "blossom.sqlite3"),
        CHECKPOINT_PATH_VARIABLE: str(tmp_path / "checkpoints.sqlite3"),
        TRACE_PATH_VARIABLE: str(tmp_path / "traces.sqlite3"),
    }


@pytest.mark.parametrize("newer", [False, True])
@pytest.mark.parametrize(
    ("where", "times"),
    [
        ("publishing, all held", 1),
        ("publishing, all held", 2),
        ("waiting to publish, all held", 1),
        ("waiting to publish, all held", 2),
        ("publishing, draft held", 1),
        ("publishing, draft held", 2),
        ("failing, all held", 0),
    ],
)
def test_a_run_that_ends_before_its_plan_is_published_is_never_published_after_a_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, where: str, times: int, newer: bool
) -> None:
    """The app is closed after the run ends, with no sweep in between, and started twice.
    Neither start puts the run's draft in place of today's plan or a newer one, a plan its
    settle published first stays, and a plan made after that outlives the next start."""
    files = files_in(tmp_path)
    time_now = FakeTime()

    async def scenario() -> tuple[str, list[str | None], list[int], str, str | None]:
        async with started(files, monotonic=time_now) as state:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            run_id = await ended_short_of_publishing(
                monkeypatch, state, tmp_path / "blossom.sqlite3", where, times
            )
            before = state.drafts.run_status(run_id, reconcile=False)
            published = before is not None and before.status == "published"
            newest = run_id if published else first.thread_id
            if newer:
                time_now.now += RUN_DEADLINE_SECONDS + 1
                newest = (
                    await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
                ).thread_id
        latest: list[str | None] = []
        left: list[int] = []
        for _ in range(2):
            async with started(files) as state:
                found = state.drafts.latest_for(PLAN_DATE)
                latest.append(None if found is None else found.thread_id)
                left.append(len(state.drafts.unpublished()))
        async with started(files) as state:
            retry = (
                await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            ).thread_id
        async with started(files) as state:
            found = state.drafts.latest_for(PLAN_DATE)
        return newest, latest, left, retry, None if found is None else found.thread_id

    newest, latest, left, retry, kept = asyncio.run(scenario())

    assert latest == [newest, newest]
    assert left == [0, 0]
    assert kept == retry


def test_a_run_canceled_after_its_plan_is_published_keeps_it_even_when_the_draft_cant_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request canceled once its plan is published, while its displaced threads are cleared,
    leaves the plan and its thread in place, also when the draft can't be read back."""
    state = application()
    try:

        async def scenario() -> tuple[str, DraftRecord | None, set[str]]:
            await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            tidy_thread = plan_runs.tidy_thread
            clearing = asyncio.Event()

            async def held_tidy(thread_id: str, state: ApplicationState) -> None:
                clearing.set()
                await asyncio.sleep(0.2)
                await tidy_thread(thread_id, state)

            def unreadable(draft_id: str) -> DraftRecord | None:
                locked = "database is locked"
                raise sqlite3.OperationalError(locked)

            monkeypatch.setattr(plan_runs, "tidy_thread", held_tidy)
            late = asyncio.create_task(
                plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            )
            await asyncio.wait_for(clearing.wait(), timeout=STORE_WAIT)
            monkeypatch.setattr(state.drafts, "get", unreadable)
            late.cancel()
            await asyncio.gather(late, return_exceptions=True)
            monkeypatch.undo()
            (published,) = [record for record in state.drafts.waiting() if record.published]
            await sweep_aged(state)
            return published.thread_id, state.drafts.latest_for(PLAN_DATE), await thread_ids(state)

        published, latest, threads = asyncio.run(scenario())
    finally:
        state.close()

    assert latest is not None
    assert latest.thread_id == published
    assert published in threads


@pytest.mark.parametrize("times", [1, 2])
def test_a_run_canceled_while_it_ends_out_of_time_keeps_one_account_of_how_it_ended(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, times: int
) -> None:
    """A run canceled while it ends after its time ran out still keeps its record as timed
    out, with the step that says so, and its time under the same category."""
    state = file_backed_application(tmp_path)
    try:

        async def scenario() -> list[RunRecord]:
            await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            loop = asyncio.get_running_loop()
            held = HeldStoreCall(state.drafts.end_run, tmp_path / "blossom.sqlite3", 0.6, loop)
            monkeypatch.setattr(state.drafts, "end_run", held)
            try:
                late = asyncio.create_task(
                    plan_evening(
                        plan_graph_for(
                            state, planner=HeldUntil(), critic=Scripted(ok(accepting()))
                        ),
                        PLAN_DATE,
                        state,
                        budget=RunBudget(seconds=0.2),
                    )
                )
                await asyncio.wait_for(asyncio.shield(held.late_by), timeout=STORE_WAIT)
                for _ in range(times):
                    late.cancel()
                    await asyncio.sleep(0)
                await asyncio.gather(late, return_exceptions=True)
            finally:
                held.close()
            await detached_done(state)
            monkeypatch.undo()
            return state.drafts.runs_without_a_draft()

        (run,) = asyncio.run(scenario())
    finally:
        state.close()

    assert run.outcome == "timed_out"
    assert run.steps[-1].found == "No plan came back: the run's time ran out while it waited."
    assert run.timing is not None
    assert run.timing.category == "timeout"


class HeldStoreCall:
    """Wraps one drafts store method: its first call whose BEGIN succeeds before ``close()``
    starts another connection's hold on the file's write lock for at least ``held`` seconds,
    or until the test ends it sooner, sets ``holding`` once the hold has begun, and asks the
    event loop for a callback 0.05 seconds on, recording how late it ran. One use of the
    other connection runs at a time, so closing it waits for a first call still waiting for
    the file.

    ``end_hold()`` comes only after ``holding``, and ``end_hold()`` and ``close()`` only on the
    event loop's thread. ``close()`` keeps that thread until the hold ends, so nothing is timed
    across it."""

    def __init__(
        self,
        call: Callable[..., object],
        path: pathlib.Path,
        held: float,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.call = call
        self.other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.held = held
        self.loop = loop
        self.releases: list[threading.Timer] = []
        self.late_by: asyncio.Future[float] = loop.create_future()
        self.holding = asyncio.Event()
        self.using = threading.Lock()
        self.ended = False
        self.closed = False

    def __call__(self, *args: object, **kwargs: object) -> object:
        with self.using:
            if not self.releases and not self.closed:
                self.other.execute("BEGIN EXCLUSIVE")
                self.releases.append(threading.Timer(self.held, self.release))
                self.releases[0].start()
                due = monotonic()

                def ran() -> None:
                    self.late_by.set_result(monotonic() - due)

                self.loop.call_soon_threadsafe(self.loop.call_later, 0.05, ran)
                self.loop.call_soon_threadsafe(self.holding.set)
        return self.call(*args, **kwargs)

    def release(self) -> None:
        """End the hold once, whichever of its timer and the test ends it first."""
        with self.using:
            if self.releases and not self.ended:
                self.ended = True
                self.other.execute("ROLLBACK")

    def in_force(self) -> bool:
        """Whether the hold has begun and not yet ended."""
        with self.using:
            return bool(self.releases) and not self.ended

    def end_hold(self) -> None:
        """End the hold now, before its timer, once the test has what it held the file for."""
        assert self.holding.is_set(), "a hold ends only once it has begun"
        for release in self.releases:
            release.cancel()
        self.release()

    def close(self) -> None:
        """Close the other connection once nothing uses it: a first call still waiting for
        the file takes it first, no hold begins after, and a hold that began ends."""
        with self.using:
            self.closed = True
        for release in self.releases:
            release.join(timeout=STORE_WAIT)
            assert not release.is_alive()
        with self.using:
            self.other.close()


@pytest.mark.parametrize(
    ("method", "outcome"),
    [
        ("end_run", "timed_out"),
        ("record_run", "checks_failed"),
        ("record_timing", "accepted"),
        ("overtaken", "overtaken"),
    ],
)
def test_a_write_as_a_run_ends_waits_for_the_drafts_file_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, method: str, outcome: str
) -> None:
    """Ending a run out of time, ending it from its last node, settling it and keeping its
    time each wait for a held drafts file on a worker thread, so a callback due meanwhile
    runs on time and the run ends as it would have."""
    state = file_backed_application(tmp_path)
    try:

        async def scenario() -> tuple[str, PlanRunView, float, DraftRecord | None]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            name = {"overtaken": "settle_run", "record_run": "end_run"}.get(method, method)
            held = HeldStoreCall(
                getattr(state.drafts, name),
                tmp_path / "blossom.sqlite3",
                0.5,
                asyncio.get_running_loop(),
            )
            monkeypatch.setattr(state.drafts, name, held)
            try:
                if method == "record_timing":
                    view = await plan_evening(
                        graph_for(state, fixture_week_plan()), PLAN_DATE, state
                    )
                elif method == "overtaken":
                    slow = HeldUntil()
                    working = asyncio.create_task(
                        plan_evening(
                            plan_graph_for(state, planner=slow, critic=Scripted(ok(accepting()))),
                            PLAN_DATE,
                            state,
                        )
                    )
                    await asyncio.sleep(0.2)
                    async with state.decision_lock:
                        published_elsewhere(state, "draft:plan:recovered", "plan:recovered")
                    slow.go.set()
                    view = await working
                elif method == "record_run":
                    view = await plan_evening(
                        graph_for(state, *[forgetful_fixture_plan()] * 3), PLAN_DATE, state
                    )
                else:
                    view = await plan_evening(
                        plan_graph_for(
                            state, planner=HeldUntil(), critic=Scripted(ok(accepting()))
                        ),
                        PLAN_DATE,
                        state,
                        budget=RunBudget(seconds=0.2),
                    )
                late_by = await asyncio.wait_for(asyncio.shield(held.late_by), timeout=STORE_WAIT)
            finally:
                held.close()
            await detached_done(state)
            return first.thread_id, view, late_by, state.drafts.latest_for(PLAN_DATE)

        first, view, late_by, latest = asyncio.run(scenario())
        running = state.drafts.running_threads()
    finally:
        state.close()

    assert late_by < 0.25
    assert view.outcome == outcome
    assert latest is not None
    assert (
        latest.thread_id
        == {
            "timed_out": first,
            "checks_failed": first,
            "accepted": view.thread_id,
            "overtaken": "plan:recovered",
        }[outcome]
    )
    assert running == frozenset()
    assert not state.decision_lock.locked()


@pytest.mark.parametrize("cycle", [1, 2])
def test_cleanup_after_a_publication_runs_out_of_time_leaves_the_server_answering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, cycle: int
) -> None:
    """A run whose time runs out while its settle waits for a file held 0.7 seconds is ended,
    its draft taken back, without holding up the event loop: callbacks due before and
    after the limit run on time, today's plan stays, and a retry publishes."""
    state = file_backed_application(tmp_path)
    try:

        async def scenario() -> tuple[str, PlanRunView, float, float, str | None, PlanRunView]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            loop = asyncio.get_running_loop()
            contended = HeldFile(state, tmp_path / "blossom.sqlite3", 0.7, loop)
            monkeypatch.setattr(state.drafts, "settle_run", contended)
            after: asyncio.Future[float] = loop.create_future()
            try:
                late = asyncio.create_task(
                    plan_evening(
                        graph_for(state, fixture_week_plan()),
                        PLAN_DATE,
                        state,
                        budget=RunBudget(seconds=0.3),
                    )
                )
                await asyncio.wait_for(contended.started.wait(), timeout=5)
                due = monotonic() + 0.35
                loop.call_later(0.35, lambda: after.set_result(monotonic() - due))
                view = await late
                before_by = await contended.late_by
                after_by = await after
                kept = state.drafts.latest_for(PLAN_DATE)
            finally:
                contended.close()
            monkeypatch.undo()
            retry = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            return (
                first.thread_id,
                view,
                before_by,
                after_by,
                None if kept is None else kept.thread_id,
                retry,
            )

        first, view, before_by, after_by, kept, retry = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert view.outcome == "timed_out"
    assert before_by < 0.3
    assert after_by < 0.2
    assert kept == first
    assert retry.outcome == "accepted"
    assert latest is not None
    assert latest.thread_id == retry.thread_id


@pytest.mark.parametrize("cycle", [1, 2])
def test_a_run_for_another_evening_starts_on_time_while_an_expired_run_waits_to_tidy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, cycle: int
) -> None:
    """A run out of time is ended while another connection holds the drafts file for 0.9
    seconds, and a run for the next evening starts meanwhile. A callback due 0.05 seconds
    later runs on time, today's plan stays, the other run plans, and a retry publishes."""
    state = file_backed_application(tmp_path)
    try:

        async def scenario() -> tuple[str, PlanRunView, PlanRunView, float, PlanRunView]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            loop = asyncio.get_running_loop()
            held = HeldStoreCall(state.drafts.end_run, tmp_path / "blossom.sqlite3", 0.9, loop)
            monkeypatch.setattr(state.drafts, "end_run", held)
            try:
                late = asyncio.create_task(
                    plan_evening(
                        plan_graph_for(
                            state, planner=HeldUntil(), critic=Scripted(ok(accepting()))
                        ),
                        PLAN_DATE,
                        state,
                        budget=RunBudget(seconds=0.2),
                    )
                )
                await asyncio.wait_for(held.holding.wait(), timeout=STORE_WAIT)
                await asyncio.sleep(0.05)
                tomorrow = PLAN_DATE + timedelta(days=1)
                other = asyncio.create_task(
                    plan_evening(
                        graph_for(
                            state, fixture_week_plan().model_copy(update={"plan_date": tomorrow})
                        ),
                        tomorrow,
                        state,
                    )
                )
                on_time: asyncio.Future[float] = loop.create_future()
                due = monotonic() + 0.05
                loop.call_later(0.05, lambda: on_time.set_result(monotonic() - due))
                late_by = await on_time
                view = await late
                next_evening = await other
            finally:
                held.close()
            monkeypatch.undo()
            retry = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            return first.thread_id, view, next_evening, late_by, retry

        first, view, next_evening, late_by, retry = asyncio.run(scenario())
        kept = state.drafts.latest_for(PLAN_DATE + timedelta(days=1))
        latest = state.drafts.latest_for(PLAN_DATE)
        timed_out = [run.thread_id for run in state.drafts.runs_without_a_draft()]
        running = state.drafts.running_threads()
    finally:
        state.close()

    assert late_by < 0.3
    assert view.outcome == "timed_out"
    assert view.thread_id in timed_out
    assert next_evening.outcome == "accepted"
    assert kept is not None
    assert kept.thread_id == next_evening.thread_id
    assert retry.outcome == "accepted"
    assert latest is not None
    assert latest.thread_id == retry.thread_id
    assert running == frozenset()
    assert not state.decision_lock.locked()


@pytest.mark.parametrize("times", [1, 2])
def test_a_run_canceled_while_it_records_a_held_review_lets_the_lock_go_and_the_review_lands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, times: int
) -> None:
    """A run canceled, once or twice, while it records a review a thread holds and the drafts
    file is held lets the decision lock go without waiting for the write: it lets it go
    once, while the file is still held, and the write lands on its own. The review stands
    and today's plan stays. The file's hold ends once the lock is let go, so the canceled
    run's ending, which waits only its grace for the store, finds the write landed."""
    state = file_backed_application(tmp_path)
    try:

        async def scenario() -> tuple[
            str, list[float], list[float], list[bool], DraftRecord | None
        ]:
            first = await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            assert first.draft_id is not None
            monkeypatch.setattr(state.drafts, "record_decision", failing_once_then(state.drafts))
            with pytest.raises(RuntimeError, match="database is locked"):
                await decide_draft(
                    state,
                    lambda: graph_for(state),
                    first.draft_id,
                    DecisionRequest(approved=True, reason="good pacing"),
                )
            loop = asyncio.get_running_loop()
            held = HeldStoreCall(
                state.drafts.record_decision, tmp_path / "blossom.sqlite3", 1.0, loop
            )
            recorded: list[float] = []
            releases: list[float] = []
            held_at_release: list[bool] = []
            release = state.decision_lock.release

            def noted(*args: object, **kwargs: object) -> object:
                try:
                    return held(*args, **kwargs)
                finally:
                    recorded.append(monotonic())

            def let_go() -> None:
                releases.append(monotonic())
                held_at_release.append(held.in_force())
                release()

            monkeypatch.setattr(state.drafts, "record_decision", noted)
            monkeypatch.setattr(state.decision_lock, "release", let_go)
            try:
                late = asyncio.create_task(
                    plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
                )
                await asyncio.wait_for(held.holding.wait(), timeout=STORE_WAIT)
                await asyncio.sleep(0.05)
                for _ in range(times):
                    late.cancel()
                    await asyncio.sleep(0)
                let_go_by = monotonic() + STORE_WAIT
                while not releases and monotonic() < let_go_by:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
                held.end_hold()
                await asyncio.gather(late, return_exceptions=True)
            finally:
                held.close()
            # The review's write goes on on its own thread after the cancel, until the file frees.
            recorded_by = monotonic() + STORE_WAIT
            while not recorded and monotonic() < recorded_by:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
            await detached_done(state)
            monkeypatch.undo()
            reviewed = state.drafts.get(first.draft_id)
            return first.thread_id, recorded, releases, held_at_release, reviewed

        first, recorded, releases, held_at_release, reviewed = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
        running = state.drafts.running_threads()
    finally:
        state.close()

    assert len(recorded) == 1
    assert releases
    assert held_at_release == [True]
    assert not state.decision_lock.locked()
    assert reviewed is not None
    assert reviewed.decision == "approved"
    assert latest is not None
    assert latest.thread_id == first
    assert running == frozenset()


@pytest.mark.parametrize(
    ("withdraw_fails", "fails_after"), [(False, False), (True, False), (False, True)]
)
@pytest.mark.parametrize("times", [1, 2])
def test_a_run_canceled_while_its_plan_is_published_anyway_keeps_the_plan_and_its_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    times: int,
    withdraw_fails: bool,
    fails_after: bool,
) -> None:
    """A run canceled, once or twice, once its settle has committed but before the answer,
    keeps its plan and the thread a review resumes, also when the ending the cancel asks
    for fails or the settle fails after its commit, and the sweep leaves both in place."""
    state = file_backed_application(tmp_path)
    try:

        async def scenario() -> tuple[str, DraftRecord | None, set[str]]:
            await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            loop = asyncio.get_running_loop()
            settle = state.drafts.settle_run
            began = asyncio.Event()
            gate = threading.Event()
            seen: list[str] = []

            def committed_late(run_id: str, **arguments: Any) -> Settled:  # noqa: ANN401
                seen.append(run_id)
                settled = settle(run_id, **arguments)
                loop.call_soon_threadsafe(began.set)
                gate.wait(5)
                if fails_after:
                    failed = "disk I/O error"
                    raise sqlite3.OperationalError(failed)
                return settled

            def held(run_id: str, **_: object) -> RunState | None:
                locked = "database is locked"
                raise sqlite3.OperationalError(locked)

            monkeypatch.setattr(state.drafts, "settle_run", committed_late)
            if withdraw_fails:
                monkeypatch.setattr(state.drafts, "end_run", held)
            late = asyncio.create_task(
                plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            )
            await asyncio.wait_for(began.wait(), timeout=STORE_WAIT)
            for _ in range(times):
                late.cancel()
                await asyncio.sleep(0)
            gate.set()
            await asyncio.gather(late, return_exceptions=True)
            monkeypatch.undo()
            await detached_done(state)
            await sweep_aged(state)
            published = state.drafts.get(f"draft:{seen[0]}")
            assert published is not None
            return published.thread_id, state.drafts.latest_for(PLAN_DATE), await thread_ids(state)

        published, latest, threads = asyncio.run(scenario())
    finally:
        state.close()

    assert latest is not None
    assert latest.thread_id == published
    assert published in threads


@pytest.mark.parametrize(("plans", "outcome"), [(3, "checks_failed"), (1, "accepted")])
def test_a_graph_that_comes_back_past_the_runs_limit_is_timed_out(
    monkeypatch: pytest.MonkeyPatch, plans: int, outcome: str
) -> None:
    """Work that holds the event loop past the run's limit keeps the limit from firing. Once
    the graph comes back, a run it ended keeps the reason its record committed, and a run
    that paused is timed out: nothing it made is published or left waiting, and today's
    plan stays."""
    state = application()
    try:

        async def scenario() -> tuple[str, str, PlanRunView, list[DraftRecord], str | None]:
            await plan_evening(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            control = await plan_evening(
                graph_for(state, *[forgetful_fixture_plan()] * plans)
                if plans > 1
                else graph_for(state, fixture_week_plan()),
                PLAN_DATE,
                state,
            )
            graph = (
                graph_for(state, *[forgetful_fixture_plan()] * plans)
                if plans > 1
                else graph_for(state, fixture_week_plan())
            )
            ainvoke: Callable[..., Awaitable[object]] = graph.ainvoke

            async def held_after(*args: object, **kwargs: object) -> object:
                result = await ainvoke(*args, **kwargs)
                sleep(0.35)  # noqa: ASYNC251
                return result

            monkeypatch.setattr(graph, "ainvoke", held_after)
            kept = state.drafts.latest_for(PLAN_DATE)
            late = await plan_evening(graph, PLAN_DATE, state, budget=RunBudget(seconds=0.2))
            await detached_done(state)
            left = state.drafts.unpublished()
            latest = state.drafts.latest_for(PLAN_DATE)
            assert kept is not None
            return (
                kept.thread_id,
                control.outcome,
                late,
                left,
                None if latest is None else latest.thread_id,
            )

        kept, control, late, left, latest = asyncio.run(scenario())
        running = state.drafts.running_threads()
    finally:
        state.close()

    assert control == outcome
    assert late.outcome == ("timed_out" if outcome == "accepted" else outcome)
    assert late.draft_id is None
    assert left == []
    assert latest == kept
    assert running == frozenset()
