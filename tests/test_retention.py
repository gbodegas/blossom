"""Saved graph state is the loop's short-term memory: kept while the loop runs, cleared after.

The routes clear a thread when its run ends or its decision lands, a draft
that waits too long is closed as expired, and the sweep at startup applies
both rules to whatever a crash left behind.
"""

import asyncio
import pathlib
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
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
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, Durability, StateSnapshot

from blossom.agent.graph import CompiledPlanGraph, PlanState, plan_graph_for
from blossom.agent.retention import (
    EXPIRED_REASON,
    PAUSED_RETENTION_DAYS,
    Swept,
    sweep_saved_state,
)
from blossom.agent.runs import DURABILITY, run_config
from blossom.app import create_app
from blossom.clock import FrozenClock
from blossom.dependencies import ApplicationState, build_application_state
from blossom.drafts import Decision, Draft, DraftStatus
from blossom.plans import DailyPlan
from blossom.routes.parent import DecisionRequest, decide_draft
from blossom.routes.runs import plan_graphs, run_plan
from blossom.settings import CHECKPOINT_PATH_VARIABLE, DATABASE_PATH_VARIABLE, TRACE_PATH_VARIABLE
from blossom.stores.drafts import DraftRecord, DraftsStore
from tests.support import (
    FIXTURE_TIMEZONE,
    OBSERVED_AT,
    Scripted,
    accepting,
    fixture_settings,
    fixture_week_plan,
    forgetful_fixture_plan,
    ok,
    scripted_graphs,
)

PLAN_DATE = date(2026, 8, 19)
ZONE = ZoneInfo(FIXTURE_TIMEZONE)


CREATED_LATER = datetime(2026, 8, 19, 23, 0, tzinfo=UTC)


def application(
    *, saver: BaseCheckpointSaver[str] | None = None, **environ: str
) -> ApplicationState:
    return build_application_state(
        fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **environ),
        saver if saver is not None else InMemorySaver(),
    )


def graph_for(state: ApplicationState, *plans: DailyPlan) -> CompiledPlanGraph:
    return plan_graph_for(
        state,
        planner=Scripted(*[ok(plan) for plan in plans]),
        critic=Scripted(*[ok(accepting()) for _ in plans]),
    )


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
            view = await run_plan(
                graph_for(
                    state,
                    forgetful_fixture_plan(),
                    forgetful_fixture_plan(),
                    forgetful_fixture_plan(),
                ),
                PLAN_DATE,
                state,
            )
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
                await run_plan(
                    plan_graph_for(state),
                    PLAN_DATE,
                    state,
                )
            except HTTPException as error:
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
            view = await run_plan(
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
                config=run_config("plan:waiting"),
                durability=DURABILITY,
            )
            await graph_for(
                state, forgetful_fixture_plan(), forgetful_fixture_plan(), forgetful_fixture_plan()
            ).ainvoke(
                PlanState(plan_date=PLAN_DATE, rounds=0),
                config=run_config("plan:finished"),
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
                config=run_config("plan:stale"),
                durability=DURABILITY,
            )
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
                config=run_config("plan:fresh"),
                durability=DURABILITY,
            )
            on_the_last_day = FrozenClock(OBSERVED_AT + timedelta(days=PAUSED_RETENTION_DAYS), ZONE)
            return await sweep_saved_state(state.checkpointer, state.drafts, on_the_last_day)

        swept = asyncio.run(scenario())
        waiting = state.drafts.waiting()
    finally:
        state.close()

    assert swept == Swept(expired=(), cleared=())
    assert [record.draft_id for record in waiting] == ["draft:plan:fresh"]


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
    with TestClient(first) as client:
        started = client.post("/parent/plans", json={}).json()
        assert started["waiting"] is True

    later = PLAN_DATE + timedelta(days=PAUSED_RETENTION_DAYS + 1)
    second = create_app(fixture_settings(BLOSSOM_TODAY=later.isoformat(), **files))
    with TestClient(second) as client:
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
            first = await run_plan(
                graph_for(state, fixture_week_plan()),
                PLAN_DATE,
                state,
            )
            second = await run_plan(
                graph_for(state, fixture_week_plan()),
                PLAN_DATE,
                state,
            )
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
            first = await run_plan(
                graph_for(state, fixture_week_plan()),
                PLAN_DATE,
                state,
            )
            saver.armed = True
            with pytest.raises(RuntimeError, match="checkpoint could not be written"):
                await run_plan(
                    graph_for(state, fixture_week_plan()),
                    PLAN_DATE,
                    state,
                )
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
            first = await run_plan(
                graph_for(state, fixture_week_plan()),
                PLAN_DATE,
                state,
            )
            saver.armed = True
            with pytest.raises(RuntimeError, match="checkpoint could not be written"):
                await run_plan(
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
    """A process dying between the draft's save and its checkpoint leaves no route to tidy up."""
    saver = SaverFailingAfterCompose()
    state = application(saver=saver)
    try:

        async def scenario() -> tuple[str, Swept, set[str]]:
            first = await run_plan(
                graph_for(state, fixture_week_plan()),
                PLAN_DATE,
                state,
            )
            saver.armed = True
            with pytest.raises(RuntimeError, match="checkpoint could not be written"):
                await graph_for(state, fixture_week_plan()).ainvoke(
                    PlanState(plan_date=PLAN_DATE, rounds=0),
                    config=run_config("plan:crashed"),
                    durability=DURABILITY,
                )
            saver.armed = False
            swept = await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
            return first.thread_id, swept, await thread_ids(state)

        first, swept, threads = asyncio.run(scenario())
        waiting = state.drafts.waiting()
        latest = state.drafts.latest_for(PLAN_DATE)
        interrupted = state.drafts.runs_without_a_draft()
    finally:
        state.close()

    assert swept.withdrawn == ("draft:plan:crashed",)
    assert swept.cleared == ("plan:crashed",)
    assert [record.thread_id for record in waiting] == [first]
    assert waiting[0].decision is None
    assert latest is not None
    assert latest.thread_id == first
    assert [run.thread_id for run in interrupted] == ["plan:crashed"]


class DisplacedDuringReview:
    """A graph whose resume first saves a later draft for the evening, as another run might."""

    def __init__(self, inner: CompiledPlanGraph, state: ApplicationState) -> None:
        self._inner = inner
        self._state = state

    async def aget_state(self, config: RunnableConfig) -> StateSnapshot:
        return await self._inner.aget_state(config)

    async def ainvoke(
        self, resume: Command[Any], *, config: RunnableConfig, durability: Durability
    ) -> dict[str, object]:
        self._state.drafts.record_waiting(
            Draft(draft_id="draft:plan:later", body="later", created_at=CREATED_LATER),
            thread_id="plan:later",
            plan_date=PLAN_DATE,
            outcome="accepted",
        )
        return dict(await self._inner.ainvoke(resume, config=config, durability=durability))


def test_a_review_that_meets_a_later_draft_settles_the_displaced_one_for_good() -> None:
    """The gate is passed and the table refuses: the thread goes, and the draft cannot come back."""
    state = application()
    try:

        async def scenario() -> tuple[str, int, set[str]]:
            first = await run_plan(
                graph_for(state, fixture_week_plan()),
                PLAN_DATE,
                state,
            )
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
            msg = "the review was accepted although a later draft had taken the draft's place"
            raise AssertionError(msg)

        draft_id, status_code, threads = asyncio.run(scenario())
        displaced = state.drafts.get(draft_id)
        taken_back = state.drafts.withdraw("draft:plan:later")
        waiting = state.drafts.waiting()
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert status_code == 409
    assert threads == set()
    assert displaced is not None
    assert displaced.decision == "superseded"
    assert taken_back is True
    assert waiting == []
    assert latest is None


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
        saver.sweep = lambda: sweep_saved_state(
            state.checkpointer, state.drafts, state.clock, in_flight=state.in_flight
        )

        async def scenario() -> tuple[str, set[str]]:
            view = await run_plan(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            assert view.waiting
            return view.thread_id, await thread_ids(state)

        thread, threads = asyncio.run(scenario())
        waiting = state.drafts.waiting()
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert saver.swept == Swept(expired=(), cleared=(), withdrawn=())
    assert [record.thread_id for record in waiting] == [thread]
    assert latest is not None
    assert latest.thread_id == thread
    assert threads == {thread}
    assert state.in_flight == set()


def test_the_sweep_keeps_taking_back_until_the_plan_left_waiting_has_a_thread() -> None:
    """Two runs died in the gap above a plan paused at the gate: that plan is what waits."""
    saver = SaverFailingAfterCompose()
    state = application(saver=saver)
    try:

        async def scenario() -> tuple[str, Swept, set[str]]:
            first = await run_plan(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            saver.armed = True
            for thread in ("plan:crash-b", "plan:crash-c"):
                with pytest.raises(RuntimeError, match="checkpoint could not be written"):
                    await graph_for(state, fixture_week_plan()).ainvoke(
                        PlanState(plan_date=PLAN_DATE, rounds=0),
                        config=run_config(thread),
                        durability=DURABILITY,
                    )
            saver.armed = False
            swept = await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
            return first.thread_id, swept, await thread_ids(state)

        first, swept, threads = asyncio.run(scenario())
        waiting = state.drafts.waiting()
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert swept.withdrawn == ("draft:plan:crash-c", "draft:plan:crash-b")
    assert swept.cleared == ("plan:crash-b", "plan:crash-c")
    assert [record.thread_id for record in waiting] == [first]
    assert latest is not None
    assert latest.thread_id == first
    assert threads == {first}


def test_the_sweep_takes_back_a_waiting_draft_whose_thread_does_not_exist() -> None:
    state = application()
    try:
        state.drafts.record_waiting(
            Draft(draft_id="draft:plan:ghost", body="ghost", created_at=CREATED_LATER),
            thread_id="plan:ghost",
            plan_date=PLAN_DATE + timedelta(days=1),
            outcome="accepted",
        )
        swept = asyncio.run(sweep_saved_state(state.checkpointer, state.drafts, state.clock))
        waiting = state.drafts.waiting()
        interrupted = state.drafts.runs_without_a_draft()
    finally:
        state.close()

    assert swept.withdrawn == ("draft:plan:ghost",)
    assert waiting == []
    assert [run.outcome for run in interrupted] == ["interrupted"]


class SaverRefusingDeletes(InMemorySaver):
    """A saver that cannot delete a thread, once armed."""

    armed = False

    async def adelete_thread(self, thread_id: str) -> None:
        if self.armed:
            msg = "database is locked"
            raise RuntimeError(msg)
        await super().adelete_thread(thread_id)


def test_a_displaced_draft_is_settled_before_its_spent_thread_is_cleared() -> None:
    """The table is consistent whatever the saved-state store does; the thread goes at the sweep."""
    saver = SaverRefusingDeletes()
    state = application(saver=saver)
    try:

        async def scenario() -> tuple[str, int, set[str], Swept]:
            first = await run_plan(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            assert first.draft_id is not None
            build = lambda: cast(  # noqa: E731
                "CompiledPlanGraph", DisplacedDuringReview(graph_for(state), state)
            )
            saver.armed = True
            try:
                await decide_draft(
                    state, build, first.draft_id, DecisionRequest(approved=True, reason=None)
                )
            except HTTPException as error:
                status_code = error.status_code
            else:
                msg = "the review was accepted although a later draft had taken the draft's place"
                raise AssertionError(msg)
            state.drafts.withdraw("draft:plan:later")
            before = await thread_ids(state)
            saver.armed = False
            swept = await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
            return first.thread_id, status_code, before, swept

        first, status_code, before, swept = asyncio.run(scenario())
        waiting = state.drafts.waiting()
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert status_code == 409
    assert before == {first}
    assert waiting == []
    assert latest is None
    assert swept.cleared == (first,)


class SaverSweepingThenFailing(SaverSweepingBeforeTheDraft):
    """The scheduled sweep fires in a run's gap, and then the run's checkpoint fails."""

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
            msg = "the checkpoint could not be written"
            raise RuntimeError(msg)
        return await super().aput(config, checkpoint, metadata, new_versions)


def test_the_sweep_in_a_runs_gap_keeps_the_thread_of_the_plan_that_run_displaced() -> None:
    """The run then fails and gives the plan back, thread and all."""
    saver = SaverSweepingThenFailing()
    state = application(saver=saver)
    try:

        async def scenario() -> tuple[str, set[str]]:
            first = await run_plan(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            saver.sweep = lambda: sweep_saved_state(
                state.checkpointer, state.drafts, state.clock, in_flight=state.in_flight
            )
            with pytest.raises(RuntimeError, match="checkpoint could not be written"):
                await run_plan(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            return first.thread_id, await thread_ids(state)

        first, threads = asyncio.run(scenario())
        waiting = state.drafts.waiting()
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert saver.swept is not None
    assert saver.swept.cleared == ()
    assert saver.swept.withdrawn == ()
    assert [record.thread_id for record in waiting] == [first]
    assert latest is not None
    assert latest.thread_id == first
    assert threads == {first}


def test_a_thread_that_cannot_be_tidied_after_a_pause_does_not_fail_the_run() -> None:
    """The run's outcome is what the caller hears; the thread goes at the next sweep."""
    saver = SaverRefusingDeletes()
    state = application(saver=saver)
    try:

        async def scenario() -> tuple[str, str, set[str], set[str]]:
            first = await run_plan(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
            saver.armed = True
            second = await run_plan(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
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
            first = await run_plan(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
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
            first = await run_plan(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
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
            first = await run_plan(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
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
            first = await run_plan(graph_for(state, fixture_week_plan()), PLAN_DATE, state)
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
