"""Saved graph state is the loop's short-term memory: kept while the loop runs, cleared after.

The routes clear a thread when its run ends or its decision lands, a draft
that waits too long is closed as expired, and the sweep at startup applies
both rules to whatever a crash left behind.
"""

import asyncio
import pathlib
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

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
from blossom.drafts import DraftStatus
from blossom.plans import DailyPlan
from blossom.routes.parent import DecisionRequest, decide_draft
from blossom.routes.runs import plan_graphs, run_plan
from blossom.settings import CHECKPOINT_PATH_VARIABLE, DATABASE_PATH_VARIABLE, TRACE_PATH_VARIABLE
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


def application(**environ: str) -> ApplicationState:
    return build_application_state(
        fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **environ), InMemorySaver()
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
                state.tracer,
                state.checkpointer,
                state.drafts,
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
                    plan_graph_for(state), PLAN_DATE, state.tracer, state.checkpointer, state.drafts
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
                state.tracer,
                state.checkpointer,
                state.drafts,
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
                state.tracer,
                state.checkpointer,
                state.drafts,
            )
            second = await run_plan(
                graph_for(state, fixture_week_plan()),
                PLAN_DATE,
                state.tracer,
                state.checkpointer,
                state.drafts,
            )
            return first.thread_id, second.thread_id, await thread_ids(state)

        first, second, threads = asyncio.run(scenario())
    finally:
        state.close()

    assert first != second
    assert threads == {second}
