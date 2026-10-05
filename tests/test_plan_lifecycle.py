# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A planning request from admission to its answer: one run per household, a deadline the
answer keeps, publication only by ``settle_run``, and an answer that matches the record."""

import asyncio
import json
import pathlib
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from html import unescape
from typing import Any, Final

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from starlette.requests import Request

from blossom.agent.graph import Ask, ModelAnswer, PlanState, plan_graph_for
from blossom.agent.retention import SavedThread, reviewable, saved_thread, sweep_saved_state
from blossom.agent.runs import DURABILITY, RUN_DEADLINE_SECONDS, RunBudget, run_config
from blossom.agent.steps import RunTiming, StepRecord, step_label, step_sentence
from blossom.anthropic_client import ServiceBusy, ServiceFailed
from blossom.app import create_app
from blossom.dependencies import (
    STATE_ATTRIBUTE,
    ApplicationState,
    build_application_state,
    sweep_aged,
)
from blossom.drafts import Draft
from blossom.heuristic_relevance import CriticVerdict
from blossom.noticing import read_week
from blossom.plans import DailyPlan, Deferral
from blossom.reconciliation import SourceChannel
from blossom.routes import parent as parent_routes
from blossom.routes import runs as runs_module
from blossom.routes import student as student_routes
from blossom.routes.parent import DecisionRequest, decide_draft
from blossom.routes.runs import (
    ADMISSION_ALLOWANCE_SECONDS,
    AlreadyPlanning,
    CouldNotStart,
    NotSaved,
    PlanGraphs,
    PlanMade,
    Unconfirmed,
    make_plan,
    plan_graphs,
    require_work,
    seconds_to_wait,
)
from blossom.settings import Settings
from blossom.stores import drafts as drafts_module
from blossom.stores.checkpoints import open_checkpointer
from blossom.stores.drafts import (
    SETTLE_GRACE_SECONDS,
    STORE_WAIT_SECONDS,
    DraftRecord,
    DraftsStore,
    RunState,
    Settled,
    StoreBusy,
)
from blossom.stores.household_claim import AnotherProcessHasTheHousehold, claim_household
from blossom.stores.project_state import Assignment
from blossom.views import DecisionView, PlanRunView
from tests.support import (
    HER_PAGE,
    HERS,
    OBSERVED_AT,
    ORIGIN,
    PAGE_HEADERS,
    PLAN_DATE,
    SAME_ORIGIN,
    FakeTime,
    Scripted,
    SetClock,
    Spending,
    Turn,
    accepting,
    due,
    every_row,
    files_in,
    fixture_settings,
    fixture_week_plan,
    forgetful_fixture_plan,
    human_text,
    light_fixture_plan,
    model_graphs,
    ok,
    plan_evening,
    record,
    scripted_graphs,
    settled_run,
    signed_in,
    signed_in_household,
    state_of,
    with_clock,
    words,
)


def application(tmp_path: pathlib.Path) -> ApplicationState:
    return build_application_state(
        fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **files_in(tmp_path)),
        InMemorySaver(),
    )


def critic() -> Ask[Any]:
    return Scripted(ok(accepting()))


def waits_for(gate: asyncio.Event) -> Ask[DailyPlan]:
    """A planner that answers once ``gate`` is set."""

    async def ask(messages: Sequence[BaseMessage]) -> ModelAnswer[DailyPlan]:
        await gate.wait()
        return ok(fixture_week_plan())

    return ask


def past_the_grace_on_its_loop(fake: FakeTime) -> None:
    """From a worker thread, move ``fake`` past a run's deadline and its grace on the loop
    that reads it, so a wait the loop began before the thread was handed its work ends past
    the grace however the threads are scheduled."""
    loop = fake.loop
    assert loop is not None
    loop.call_soon_threadsafe(fake.advance, RUN_DEADLINE_SECONDS + SETTLE_GRACE_SECONDS + 0.5)


def unwinds_until(released: asyncio.Event) -> Ask[DailyPlan]:
    """A planner that never answers and, once canceled, takes until ``released`` to stop."""

    async def ask(messages: Sequence[BaseMessage]) -> ModelAnswer[DailyPlan]:
        try:
            await asyncio.Event().wait()
        except BaseException:
            await released.wait()
            raise
        msg = "unreachable"
        raise AssertionError(msg)

    return ask


async def until(read: Callable[[], RunState | None], seconds: float = 3.0) -> RunState:
    """The run ``read`` returns once it returns one, within ``seconds`` of real time, on any
    loop, one whose ``time()`` reads a ``FakeTime`` included."""
    ends = time.monotonic() + seconds
    while time.monotonic() < ends:
        found = read()
        if found:
            return found
        await asyncio.to_thread(time.sleep, 0.01)
    msg = "nothing came within the wait"
    raise AssertionError(msg)


async def ends_stopped(task: "asyncio.Future[Any]") -> bool:
    """Whether awaiting ``task`` ends in its cancel, rather than in an answer or a failure."""
    try:
        await task
    except Exception:
        return False
    except BaseException:
        return True
    return False


def run_of(state: ApplicationState, run_id: str) -> RunState | None:
    return state.drafts.run_status(run_id, reconcile=False)


def ended_run_of(state: ApplicationState, run_id: str) -> RunState | None:
    found = run_of(state, run_id)
    return found if found is not None and found.status == "ended" else None


def test_a_press_while_the_households_run_is_running_is_refused_naming_that_run(
    tmp_path: pathlib.Path,
) -> None:
    """The second press gets 409 with the first run's id, evening and wait; the
    graph runs once, and the first run then publishes."""
    state = application(tmp_path)
    try:

        async def scenario() -> tuple[AlreadyPlanning, Any, RunState | None]:
            gate = asyncio.Event()
            first = asyncio.ensure_future(
                make_plan(
                    plan_graph_for(state, planner=waits_for(gate), critic=critic()),
                    PLAN_DATE,
                    state,
                )
            )
            running = await until(state.drafts.latest_run)
            second = plan_graph_for(state, planner=Scripted(), critic=critic())
            with pytest.raises(AlreadyPlanning) as refused:
                await make_plan(second, PLAN_DATE, state)
            gate.set()
            made = await asyncio.wait_for(first, 10)
            return refused.value, made, run_of(state, running.run_id)

        refused, made, settled = asyncio.run(scenario())
    finally:
        state.close()

    assert refused.status_code == 409
    assert refused.run.run_id == made.view.thread_id
    detail: Any = refused.detail
    assert isinstance(detail, dict)
    assert detail["run_id"] == made.view.thread_id
    assert detail["plan_date"] == PLAN_DATE.isoformat()
    assert 1 <= detail["seconds_left"] <= 91
    assert made.record is not None
    assert settled is not None
    assert settled.status == "published"


def test_the_answer_comes_at_the_deadline_without_waiting_for_the_graph_to_unwind(
    tmp_path: pathlib.Path,
) -> None:
    """A graph still unwinding at the deadline is left to finish on its own;
    the answer says timed out within the deadline and its grace, and the record agrees."""
    state = application(tmp_path)
    try:

        async def scenario() -> tuple[Any, float, bool]:
            released = asyncio.Event()
            asyncio.get_running_loop().call_later(3.0, released.set)
            graph = plan_graph_for(state, planner=unwinds_until(released), critic=critic())
            began = time.monotonic()
            view = await plan_evening(graph, PLAN_DATE, state, budget=RunBudget(seconds=0.3))
            answered_in = time.monotonic() - began
            unwinding = not released.is_set()
            released.set()
            return view, answered_in, unwinding

        view, answered_in, unwinding = asyncio.run(scenario())
        ended = state.drafts.run_status(view.thread_id)
    finally:
        state.close()

    assert view.outcome == "timed_out"
    assert view.draft_id is None
    assert answered_in < 0.3 + 1.0
    assert unwinding
    assert ended is not None
    assert ended.status == "ended"
    assert ended.reason == "timed_out"


def settling_through(
    state: ApplicationState, then: Callable[[Callable[..., Any]], Callable[..., Any]]
) -> list[int]:
    """Replace the store's ``settle_run`` with ``then(original)``, counting the calls."""
    original = state.drafts.settle_run
    calls: list[int] = []
    replaced = then(original)

    def counted(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return replaced(*args, **kwargs)

    state.drafts.settle_run = counted  # type: ignore[method-assign, assignment]
    return calls


def test_a_settle_refused_before_it_began_is_not_saved_and_ends_the_run(
    tmp_path: pathlib.Path,
) -> None:
    """``StoreBusy`` from the settle is a confirmed refusal: the answer is not
    saved, the settle is asked once, and the run ends interrupted with nothing published."""
    state = application(tmp_path)
    try:

        def busy(original: Callable[..., Any]) -> Callable[..., Any]:
            def refuse(*args: object, **kwargs: object) -> object:
                raise StoreBusy

            return refuse

        calls = settling_through(state, busy)

        async def scenario() -> tuple[BaseException | None, RunState]:
            graph = plan_graph_for(
                state, planner=Scripted(ok(fixture_week_plan())), critic=critic()
            )
            caught: BaseException | None = None
            try:
                await make_plan(graph, PLAN_DATE, state)
            except NotSaved as error:
                caught = error
            latest = await until(state.drafts.latest_run)
            ended = await until(lambda: ended_run_of(state, latest.run_id))
            return caught, ended

        caught, ended = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert isinstance(caught, NotSaved)
    assert calls == [1]
    assert ended.reason == "interrupted"
    assert latest is None


def test_an_error_after_a_committed_settle_is_read_back_as_published(
    tmp_path: pathlib.Path,
) -> None:
    """The settle commits and then its call fails with time left; the read-back
    finds the run published, the answer is the plan, and the settle was asked once."""
    state = application(tmp_path)
    try:

        def lost(original: Callable[..., Any]) -> Callable[..., Any]:
            def settle_then_fail(*args: object, **kwargs: object) -> object:
                original(*args, **kwargs)
                msg = "the answer was lost"
                raise RuntimeError(msg)

            return settle_then_fail

        calls = settling_through(state, lost)

        async def scenario() -> PlanMade:
            graph = plan_graph_for(
                state, planner=Scripted(ok(fixture_week_plan())), critic=critic()
            )
            return await make_plan(graph, PLAN_DATE, state)

        made = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
        status = state.drafts.run_status(made.view.thread_id)
    finally:
        state.close()

    assert calls == [1]
    assert made.record is not None
    assert latest is not None
    assert latest.draft_id == made.record.draft_id
    assert status is not None
    assert status.status == "published"


def test_an_error_from_the_settle_after_the_deadline_is_unconfirmed_without_a_lookup(
    tmp_path: pathlib.Path,
) -> None:
    """The settle fails inside the grace, after the deadline; the answer is
    unconfirmed at once with no read of the record, and a later check ends the run."""
    state = application(tmp_path)
    looked: list[int] = []
    try:

        def late(original: Callable[..., Any]) -> Callable[..., Any]:
            def fail_late(*args: object, **kwargs: object) -> object:
                time.sleep(0.5)
                msg = "the file failed"
                raise RuntimeError(msg)

            return fail_late

        settling_through(state, late)
        reads = state.drafts.run_status

        def spied(
            run_id: str, wait: float = STORE_WAIT_SECONDS, *, reconcile: bool = True
        ) -> RunState | None:
            looked.append(1)
            return reads(run_id, wait, reconcile=reconcile)

        state.drafts.run_status = spied  # type: ignore[method-assign]

        async def scenario() -> BaseException | None:
            graph = plan_graph_for(
                state, planner=Scripted(ok(fixture_week_plan())), critic=critic()
            )
            try:
                await make_plan(graph, PLAN_DATE, state, budget=RunBudget(seconds=0.3))
            except Unconfirmed as error:
                return error
            return None

        caught = asyncio.run(scenario())
        during = list(looked)
        state.drafts.run_status = reads  # type: ignore[method-assign]
        assert isinstance(caught, Unconfirmed)
        later = state.drafts.run_status(caught.run_id)
    finally:
        state.close()

    assert during == []
    assert caught.view().status == "unconfirmed"
    assert later is not None
    assert later.status == "ended"
    assert later.reason == "timed_out"


def test_a_run_left_running_by_a_stopped_process_ends_at_startup_and_is_never_published(
    tmp_path: pathlib.Path,
) -> None:
    """A run that saved its draft and stopped before its settle is ended interrupted at the
    next start, its draft deleted, and then its thread cleared by the start's sweep, which
    keeps the thread of a run still running; the sweep publishes nothing."""
    settings = household_settings(tmp_path)
    run_id = "plan:2026-08-19:stopped"

    async def stopped_before_the_settle() -> None:
        async with open_checkpointer(settings.checkpoint_path) as checkpointer:
            state = build_application_state(settings, checkpointer)
            try:
                # Its deadline is still ahead on this clock, so only the start's ending of
                # stopped runs ends it.
                blocking = state.drafts.admit_run(
                    run_id, plan_date=PLAN_DATE, deadline_mono=time.monotonic() + 90
                )
                assert blocking is None
                graph = plan_graph_for(
                    state, planner=Scripted(ok(fixture_week_plan())), critic=critic()
                )
                # The graph composes and saves its draft for the admitted run, as a run does
                # before the process stops.
                await graph.ainvoke(
                    PlanState(plan_date=PLAN_DATE, rounds=0),
                    config=run_config(run_id),
                    durability=DURABILITY,
                    context=RunBudget(),
                )
                assert [record.draft_id for record in state.drafts.unpublished()] == [
                    f"draft:{run_id}"
                ]
            finally:
                state.close()

    asyncio.run(stopped_before_the_settle())
    before = asyncio.run(saved_threads(settings, run_id))

    with TestClient(create_app(settings)) as client:
        restarted = state_of(client)
        ended = restarted.drafts.run_status(run_id)
        unpublished = restarted.drafts.unpublished()
        latest = restarted.drafts.latest_for(PLAN_DATE)
    after = asyncio.run(saved_threads(settings, run_id))

    assert before[run_id] is not None
    assert ended is not None
    assert ended.status == "ended"
    assert ended.reason == "interrupted"
    assert unpublished == []
    assert latest is None
    assert after[run_id] is None


def test_a_run_that_ends_before_the_gate_is_answered_with_the_reason_its_record_committed(
    tmp_path: pathlib.Path,
) -> None:
    """A run whose plans never pass the checks is answered
    with the reason ``record_run`` committed, and the record holds the same."""
    state = application(tmp_path)
    try:
        plans = [ok(forgetful_fixture_plan()) for _ in range(5)]
        graph = plan_graph_for(state, planner=Scripted(*plans), critic=critic())
        view = asyncio.run(plan_evening(graph, PLAN_DATE, state))
        ended = state.drafts.run_status(view.thread_id)
    finally:
        state.close()

    assert view.outcome == "checks_failed"
    assert view.draft_id is None
    assert ended is not None
    assert (ended.status, ended.reason) == ("ended", "checks_failed")


def test_an_ending_held_across_the_deadline_is_recorded_and_answered_as_timed_out(
    tmp_path: pathlib.Path,
) -> None:
    """The store write that ends a run is held past its 0.3 s deadline; the
    answer says timed out at the deadline, and the record commits ``timed_out`` whatever
    reason the run ended with."""
    state = application(tmp_path)
    original = state.drafts.end_run

    def held(run_id: str, **kwargs: Any) -> RunState | None:  # noqa: ANN401
        time.sleep(0.5)
        return original(run_id, **kwargs)

    state.drafts.end_run = held  # type: ignore[method-assign]
    try:
        plans = [ok(forgetful_fixture_plan()) for _ in range(5)]
        graph = plan_graph_for(state, planner=Scripted(*plans), critic=critic())

        async def scenario() -> tuple[PlanRunView, RunState]:
            view = await plan_evening(graph, PLAN_DATE, state, budget=RunBudget(seconds=0.3))
            return view, await until(lambda: ended_run_of(state, view.thread_id))

        view, ended = asyncio.run(scenario())
    finally:
        state.close()

    assert view.outcome == "timed_out"
    assert (ended.status, ended.reason) == ("ended", "timed_out")


def test_a_press_after_a_run_timed_out_is_admitted_while_its_compose_is_still_held(
    tmp_path: pathlib.Path,
) -> None:
    """The first run times out while its draft's save waits on a barrier; the
    next press is admitted once the ending commits and publishes, and the released save
    is refused, leaving no draft behind."""
    state = application(tmp_path)
    original = state.drafts.record_waiting
    barrier = threading.Event()
    finished = threading.Event()
    held_for: list[str] = []

    def held(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        if not held_for:
            held_for.append(str(kwargs["thread_id"]))
            barrier.wait(5)
            try:
                original(*args, **kwargs)
            finally:
                finished.set()
            return
        original(*args, **kwargs)

    state.drafts.record_waiting = held  # type: ignore[method-assign]
    try:

        def graph() -> Any:  # noqa: ANN401
            return plan_graph_for(state, planner=Scripted(ok(fixture_week_plan())), critic=critic())

        async def scenario() -> tuple[PlanRunView, RunState, PlanMade]:
            first = await plan_evening(graph(), PLAN_DATE, state, budget=RunBudget(seconds=0.3))
            ended = await until(lambda: ended_run_of(state, first.thread_id))
            second = await make_plan(graph(), PLAN_DATE, state)
            barrier.set()
            await asyncio.to_thread(finished.wait, 5)
            return first, ended, second

        first, ended, second = asyncio.run(scenario())
        orphan = state.drafts.get(f"draft:{first.thread_id}")
        latest = state.drafts.latest_for(PLAN_DATE)
        after = state.drafts.run_status(first.thread_id)
    finally:
        state.close()

    assert held_for == [first.thread_id]
    assert first.outcome == "timed_out"
    assert (ended.status, ended.reason) == ("ended", "timed_out")
    assert second.record is not None
    assert latest is not None
    assert latest.draft_id == second.record.draft_id
    assert orphan is None
    assert after is not None
    assert (after.status, after.reason) == ("ended", "timed_out")


def test_a_request_canceled_after_its_settle_committed_keeps_the_plan_and_its_review(
    tmp_path: pathlib.Path,
) -> None:
    """The settle commits, then the request is canceled while it waits; ending the run
    finds it published and changes nothing, and the paused thread a review resumes stays."""
    state = application(tmp_path)
    committed = threading.Event()
    release = threading.Event()

    def commit_then_wait(original: Callable[..., Any]) -> Callable[..., Any]:
        def settle(*args: object, **kwargs: object) -> object:
            settled = original(*args, **kwargs)
            committed.set()
            release.wait(5)
            return settled

        return settle

    settling_through(state, commit_then_wait)
    try:

        async def scenario() -> tuple[RunState, bool]:
            graph = plan_graph_for(
                state, planner=Scripted(ok(fixture_week_plan())), critic=critic()
            )
            request = asyncio.ensure_future(make_plan(graph, PLAN_DATE, state))
            await asyncio.to_thread(committed.wait, 5)
            request.cancel()
            assert await ends_stopped(request)
            release.set()
            run = await until(state.drafts.latest_run)
            saved = await state.checkpointer.aget_tuple({"configurable": {"thread_id": run.run_id}})
            return run, saved is not None

        run, thread_kept = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert run.status == "published"
    assert latest is not None
    assert latest.thread_id == run.run_id
    assert thread_kept


# ------------------------------------------------- on a clock the test moves


def application_on(tmp_path: pathlib.Path, fake: FakeTime) -> ApplicationState:
    """The household over files in ``tmp_path``, its runs and its store timed on ``fake``."""
    return build_application_state(
        fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **files_in(tmp_path)),
        InMemorySaver(),
        fake,
    )


def accepted_graph(state: ApplicationState) -> Any:  # noqa: ANN401
    return plan_graph_for(state, planner=Scripted(ok(fixture_week_plan())), critic=critic())


def settles_then(state: ApplicationState, action: Callable[[], None]) -> list[int]:
    """Run ``action`` as the settle's ``COMMIT`` starts, once its checks have authorized the
    publication, and count the settles asked for."""
    original = state.drafts.settle_run
    calls: list[int] = []
    armed = threading.Event()

    def settle(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        calls.append(1)
        armed.set()
        try:
            return original(*args, **kwargs)
        finally:
            armed.clear()

    def saw(statement: str) -> None:
        if statement == "COMMIT" and armed.is_set():
            armed.clear()
            action()

    state.drafts.settle_run = settle  # type: ignore[method-assign]
    state.drafts._connection.set_trace_callback(saw)
    return calls


async def settled_down(state: ApplicationState, seconds: float = 5.0) -> None:
    """Return once the work the requests went on without has ended, within ``seconds``."""
    ends = time.monotonic() + seconds
    while state.detached and time.monotonic() < ends:
        await asyncio.to_thread(time.sleep, 0.01)
    assert not state.detached


def timing_of(tmp_path: pathlib.Path, run_id: str) -> RunTiming:
    """The time a run's row records, read through a connection of its own."""
    connection = sqlite3.connect(files_in(tmp_path)["BLOSSOM_DATABASE_PATH"])
    try:
        (text,) = connection.execute(
            "SELECT timing FROM runs WHERE thread_id=?", (run_id,)
        ).fetchone()
    finally:
        connection.close()
    return RunTiming.model_validate_json(text)


def rows_of(tmp_path: pathlib.Path) -> int:
    connection = sqlite3.connect(files_in(tmp_path)["BLOSSOM_DATABASE_PATH"])
    try:
        (count,) = connection.execute("SELECT COUNT(*) FROM runs").fetchone()
    finally:
        connection.close()
    return int(count)


def published_run_of(state: ApplicationState, run_id: str) -> RunState | None:
    found = run_of(state, run_id)
    return found if found is not None and found.status == "published" else None


def noticing_the_lock_wait(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """An event set as a run starts waiting for the decision lock to publish."""
    waiting = asyncio.Event()
    original = runs_module.hold_in_time

    async def noticed(lock: asyncio.Lock, budget: RunBudget) -> bool:
        waiting.set()
        return await original(lock, budget)

    monkeypatch.setattr(runs_module, "hold_in_time", noticed)
    return waiting


def test_a_publication_authorized_in_time_that_commits_inside_the_grace_is_reported(
    tmp_path: pathlib.Path,
) -> None:
    """The settle's checks pass before the deadline and its commit starts just
    after it, inside the grace; the plan is published and reported, and the run's time
    shows the overrun."""
    fake = FakeTime()
    state = application_on(tmp_path, fake)
    try:
        calls = settles_then(state, lambda: fake.advance(RUN_DEADLINE_SECONDS + 0.5))

        async def scenario() -> PlanMade:
            made = await make_plan(accepted_graph(state), PLAN_DATE, state, budget=fake.budget())
            await settled_down(state)
            return made

        made = fake.run(scenario())
        status = state.drafts.run_status(made.view.thread_id)
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()
    timing = timing_of(tmp_path, made.view.thread_id)

    assert calls == [1]
    assert made.record is not None
    assert latest is not None
    assert latest.draft_id == made.record.draft_id
    assert status is not None
    assert status.status == "published"
    assert timing.response_seconds is not None
    assert timing.response_seconds > RUN_DEADLINE_SECONDS
    assert not timing.unconfirmed


def test_a_save_held_past_the_deadline_ends_its_run_timed_out_with_the_steps_compose_made(
    tmp_path: pathlib.Path,
) -> None:
    """Compose has its draft, and the store is held until past the run's deadline, so its save
    gets the store before the route's ending does: the run ends ``timed_out`` with the steps
    compose made, and the family page lists the run with them."""
    fake = FakeTime()
    state = application_on(tmp_path, fake)
    asked = threading.Event()
    saved = threading.Event()
    given: list[list[StepRecord]] = []
    saving = state.drafts.record_waiting
    ending = state.drafts.end_run

    def held_past_the_deadline(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        given.append(list(kwargs["steps"]))
        past_the_grace_on_its_loop(fake)
        asked.wait(5)
        try:
            saving(*args, **kwargs)
        finally:
            saved.set()

    def after_the_save(run_id: str, **kwargs: Any) -> RunState | None:  # noqa: ANN401
        asked.set()
        saved.wait(5)
        return ending(run_id, **kwargs)

    state.drafts.record_waiting = held_past_the_deadline  # type: ignore[method-assign]
    state.drafts.end_run = after_the_save  # type: ignore[method-assign]
    try:

        async def scenario() -> PlanMade:
            made = await make_plan(accepted_graph(state), PLAN_DATE, state, budget=fake.budget())
            await settled_down(state)
            return made

        made = fake.run(scenario())
        run = state.drafts.run_status(made.view.thread_id)
        kept = state.drafts.steps_for(made.view.thread_id)
        ended = state.drafts.runs_without_a_draft()
    finally:
        state.close()
    with household_files(tmp_path) as client:
        family = words(client.get("/parent", headers=PAGE_HEADERS).text)

    composed = [(step.node, step.round, step.found) for step in given[0]]
    assert made.view.outcome == "timed_out"
    assert run is not None
    assert (run.status, run.reason) == ("ended", "timed_out")
    assert [(step.node, step.round, step.found) for step in kept] == composed
    assert [item.outcome for item in ended] == ["timed_out"]
    assert [(step.node, step.round, step.found) for step in ended[0].steps] == composed
    assert {"retrieve", "plan", "verify", "critique"} <= {node for node, _, _ in composed}
    listed = family.split("Plans that couldn't be made", 1)[1]
    for step in given[0]:
        assert words(f"{step_label(step.node, step.round)} {step_sentence(step.found)}") in listed


def her_json_press() -> Request:
    """Her ``POST /student/plans`` with the sign-in off, for her route called on a loop the
    test drives."""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "root_path": "",
            "path": "/student/plans",
            "query_string": b"",
            "headers": [],
        }
    )


@pytest.mark.parametrize("spent", ["while it is read", "before it is read"])
def test_her_json_answer_to_a_plan_published_inside_the_grace_comes_by_the_graces_end(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, spent: str
) -> None:
    """The settle's commit starts half a second past the deadline, inside the grace. With her
    plan's reading held, or the grace spent before it is read, her JSON route answers 201
    naming the published run by the deadline plus the grace, and reads nothing once the
    grace is spent."""
    fake = FakeTime()
    state = application_on(tmp_path, fake)
    graces_end = fake.now + RUN_DEADLINE_SECONDS + SETTLE_GRACE_SECONDS
    graphs = PlanGraphs(build=lambda: accepted_graph(state), may_start=True, budget=fake.budget)
    reading = threading.Event()
    release = threading.Event()
    planned = make_plan

    def held(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        reading.set()
        assert release.wait(30)

    async def then_spent(*args: Any, **kwargs: Any) -> PlanMade:  # noqa: ANN401
        made = await planned(*args, **kwargs)
        fake.advance(graces_end - fake.now)
        return made

    monkeypatch.setattr(student_routes, "plan_view", held)
    if spent == "before it is read":
        monkeypatch.setattr(student_routes, "make_plan", then_spent)
    try:
        calls = settles_then(state, lambda: fake.advance(RUN_DEADLINE_SECONDS + 0.5))

        async def scenario() -> tuple[Any, float]:
            pressed = asyncio.ensure_future(
                student_routes.make_todays_plan(her_json_press(), state, graphs)
            )
            if spent == "while it is read":
                assert await asyncio.to_thread(reading.wait, 5)
                fake.advance(graces_end - fake.now)
            ends = time.monotonic() + 3
            while not pressed.done() and time.monotonic() < ends:
                await asyncio.to_thread(time.sleep, 0.01)
            answered_at = fake.now
            release.set()
            assert pressed.done(), "no answer by the deadline plus the grace"
            await settled_down(state)
            return pressed.result(), answered_at

        answer, answered_at = fake.run(scenario())
        run = state.drafts.latest_run()
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        release.set()
        state.close()

    assert calls == [1]
    assert run is not None
    assert run.status == "published"
    assert latest is not None
    assert latest.thread_id == run.run_id
    assert answer.status_code == 201
    assert json.loads(answer.body) == {
        "run_id": run.run_id,
        "plan_date": PLAN_DATE.isoformat(),
        "status": "published",
    }
    assert answered_at <= graces_end
    assert reading.is_set() == (spent == "while it is read")


def test_a_publication_that_commits_past_the_grace_is_unconfirmed_then_found_published(
    tmp_path: pathlib.Path,
) -> None:
    """The settle's checks pass before the deadline and its commit is held past
    the deadline and the grace; the answer is unconfirmed with the run's id, the commit then
    lands, the run reads published, nothing is deleted, and a restart keeps the plan."""
    fake = FakeTime()
    state = application_on(tmp_path, fake)
    release = threading.Event()

    def past_the_grace() -> None:
        past_the_grace_on_its_loop(fake)
        release.wait(30)

    try:
        calls = settles_then(state, past_the_grace)

        async def scenario() -> Unconfirmed:
            with pytest.raises(Unconfirmed) as unconfirmed:
                await make_plan(accepted_graph(state), PLAN_DATE, state, budget=fake.budget())
            release.set()
            await until(lambda: published_run_of(state, unconfirmed.value.run_id))
            await settled_down(state)
            return unconfirmed.value

        caught = fake.run(scenario())
        status = state.drafts.run_status(caught.run_id)
    finally:
        release.set()
        state.close()
    timing = timing_of(tmp_path, caught.run_id)

    settings = fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **files_in(tmp_path))
    with TestClient(create_app(settings)) as client:
        restarted = state_of(client)
        kept = restarted.drafts.latest_for(PLAN_DATE)
        after = restarted.drafts.run_status(caught.run_id)

    assert calls == [1]
    assert caught.view().status == "unconfirmed"
    assert caught.plan_date == PLAN_DATE
    assert status is not None
    assert status.status == "published"
    assert timing.unconfirmed
    assert kept is not None
    assert kept.thread_id == caught.run_id
    assert after is not None
    assert after.status == "published"


def test_a_run_whose_time_runs_out_waiting_for_the_decision_lock_is_never_settled(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review holds the decision lock while the run waits to publish, and the
    deadline passes during the wait; the run ends timed out, its settle is never asked
    for, and the loop goes on serving meanwhile."""
    fake = FakeTime()
    state = application_on(tmp_path, fake)
    waiting = noticing_the_lock_wait(monkeypatch)
    try:
        calls = settles_then(state, lambda: None)

        async def scenario() -> tuple[PlanMade, RunState, bool]:
            assert await asyncio.wait_for(state.decision_lock.acquire(), timeout=5)
            try:
                request = asyncio.ensure_future(
                    make_plan(accepted_graph(state), PLAN_DATE, state, budget=fake.budget())
                )
                await asyncio.wait_for(waiting.wait(), timeout=5)
                served = not request.done()
                fake.advance(RUN_DEADLINE_SECONDS + 0.5)
                made = await request
            finally:
                state.decision_lock.release()
            ended = await until(lambda: ended_run_of(state, made.view.thread_id))
            await settled_down(state)
            return made, ended, served

        made, ended, served = fake.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
        draft = state.drafts.get(f"draft:{made.view.thread_id}")
    finally:
        state.close()

    assert served
    assert calls == []
    assert made.view.outcome == "timed_out"
    assert made.record is None
    assert (ended.status, ended.reason) == ("ended", "timed_out")
    assert latest is None
    assert draft is None


def test_a_run_whose_time_runs_out_reading_a_held_review_is_never_settled(
    tmp_path: pathlib.Path,
) -> None:
    """Before the next run publishes it reads the waiting plan's thread for a
    review the table never got, and that read waits past the deadline; the run ends timed
    out, its settle is never asked for, and the waiting plan stays."""
    fake = FakeTime()
    state = application_on(tmp_path, fake)
    reading = asyncio.Event()
    held = asyncio.Event()
    try:

        async def scenario() -> tuple[PlanMade, PlanMade, RunState, list[int]]:
            first = await make_plan(accepted_graph(state), PLAN_DATE, state, budget=fake.budget())
            await settled_down(state)
            original = state.checkpointer.aget_tuple

            async def slow(config: Any) -> Any:  # noqa: ANN401
                if config["configurable"]["thread_id"] == first.view.thread_id:
                    reading.set()
                    await asyncio.wait_for(held.wait(), timeout=5)
                return await original(config)

            state.checkpointer.aget_tuple = slow  # type: ignore[method-assign]
            calls = settles_then(state, lambda: None)
            request = asyncio.ensure_future(
                make_plan(accepted_graph(state), PLAN_DATE, state, budget=fake.budget())
            )
            await asyncio.wait_for(reading.wait(), timeout=5)
            fake.advance(RUN_DEADLINE_SECONDS + 0.5)
            made = await request
            held.set()
            ended = await until(lambda: ended_run_of(state, made.view.thread_id))
            await settled_down(state)
            return first, made, ended, calls

        first, made, ended, calls = fake.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert first.record is not None
    assert calls == []
    assert made.view.outcome == "timed_out"
    assert (ended.status, ended.reason) == ("ended", "timed_out")
    assert latest is not None
    assert latest.draft_id == first.record.draft_id


def test_a_press_while_the_run_waits_to_publish_is_refused_naming_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the household's run waits for the decision lock to publish, a second
    press gets 409 naming it, asks no model and adds no row; the first then publishes."""
    state = application(tmp_path)
    waiting = noticing_the_lock_wait(monkeypatch)
    try:

        async def scenario() -> tuple[AlreadyPlanning, PlanMade, int, int]:
            assert await asyncio.wait_for(state.decision_lock.acquire(), timeout=5)
            try:
                first = asyncio.ensure_future(make_plan(accepted_graph(state), PLAN_DATE, state))
                await asyncio.wait_for(waiting.wait(), timeout=5)
                rows = rows_of(tmp_path)
                second = plan_graph_for(state, planner=Scripted(), critic=Scripted())
                with pytest.raises(AlreadyPlanning) as refused:
                    await make_plan(second, PLAN_DATE, state)
                rows_after = rows_of(tmp_path)
            finally:
                state.decision_lock.release()
            return refused.value, await asyncio.wait_for(first, 10), rows, rows_after

        refused, made, rows, rows_after = asyncio.run(scenario())
        status = state.drafts.run_status(made.view.thread_id)
    finally:
        state.close()

    assert refused.run.run_id == made.view.thread_id
    assert refused.run.plan_date == PLAN_DATE
    assert rows == rows_after == 1
    assert made.record is not None
    assert status is not None
    assert status.status == "published"


def test_a_run_canceled_before_its_settle_is_never_published_by_the_sweep_or_a_restart(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request canceled during generation, and one canceled twice while waiting for
    the decision lock, end interrupted before any settle; the generation's unwinding is not
    waited for, and the sweep and a restart after it publish nothing and adopt no thread."""
    state = application(tmp_path)
    waiting = noticing_the_lock_wait(monkeypatch)
    try:
        calls = settles_then(state, lambda: None)

        async def scenario() -> tuple[RunState, RunState, bool]:
            released = asyncio.Event()
            graph = plan_graph_for(state, planner=unwinds_until(released), critic=critic())
            generating = asyncio.ensure_future(make_plan(graph, PLAN_DATE, state))
            running = await until(state.drafts.latest_run)
            generating.cancel()
            assert await ends_stopped(generating)
            unwinding = not released.is_set()
            released.set()
            first = await until(lambda: ended_run_of(state, running.run_id))

            assert await asyncio.wait_for(state.decision_lock.acquire(), timeout=5)
            try:
                publishing = asyncio.ensure_future(
                    make_plan(accepted_graph(state), PLAN_DATE, state)
                )
                await asyncio.wait_for(waiting.wait(), timeout=5)
                publishing.cancel()
                await asyncio.sleep(0)
                publishing.cancel()
                assert await ends_stopped(publishing)
            finally:
                state.decision_lock.release()
            latest = await until(state.drafts.latest_run)
            second = await until(lambda: ended_run_of(state, latest.run_id))
            await settled_down(state)
            await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
            return first, second, unwinding

        first, second, unwinding = asyncio.run(scenario())
        swept_latest = state.drafts.latest_for(PLAN_DATE)
        unpublished = state.drafts.unpublished()
    finally:
        state.close()

    settings = fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **files_in(tmp_path))
    with TestClient(create_app(settings)) as client:
        restarted = state_of(client)
        kept = restarted.drafts.latest_for(PLAN_DATE)
        again = [restarted.drafts.run_status(run.run_id) for run in (first, second)]

    assert calls == []
    assert unwinding
    assert first.run_id != second.run_id
    assert (first.status, first.reason) == ("ended", "interrupted")
    assert (second.status, second.reason) == ("ended", "interrupted")
    assert swept_latest is None
    assert unpublished == []
    assert kept is None
    assert [(run.status, run.reason) for run in again if run is not None] == [
        ("ended", "interrupted"),
        ("ended", "interrupted"),
    ]


def runs_on_the_loop(record: list[bool], page: Callable[..., Any]) -> Callable[..., Any]:
    """``page``, noting for each call whether it ran on the event loop's own thread."""

    def noted(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            record.append(False)
        else:
            record.append(True)
        return page(*args, **kwargs)

    return noted


def test_a_planning_press_that_fails_renders_its_page_off_the_event_loop(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused plan press reads its page on a worker thread, never on the event loop: the
    family page for an evening that isn't a date, and her week with no planning model."""
    family: list[bool] = []
    hers: list[bool] = []
    monkeypatch.setattr(
        parent_routes, "review_page", runs_on_the_loop(family, parent_routes.review_page)
    )
    monkeypatch.setattr(
        student_routes, "student_page", runs_on_the_loop(hers, student_routes.student_page)
    )
    settings = fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **files_in(tmp_path))
    with TestClient(create_app(settings), headers=SAME_ORIGIN) as client:
        refused = client.post("/parent/actions/plan", data={"plan_date": "someday"})
        not_made = client.post("/student/actions/plan", data={})

    assert refused.status_code == 422
    assert "is not a date" in refused.text
    assert family == [False]
    assert not_made.status_code == 503
    assert hers == [False]


class EndsPastTheDeadline(set["asyncio.Future[Any]"]):
    """The application's set of detached work, which also moves ``fake`` past the deadline
    as the first task it holds, the graph's, ends: what a loop held past the deadline looks
    like to the request, which finds the graph's result and the deadline gone together."""

    def __init__(self, fake: FakeTime) -> None:
        super().__init__()
        self.fake = fake
        self.moved = False

    def add(self, work: "asyncio.Future[Any]") -> None:
        if not self.moved:
            self.moved = True
            work.add_done_callback(lambda _: self.fake.advance(RUN_DEADLINE_SECONDS + 0.5))
        super().add(work)


def reading_saved_state(state: ApplicationState, fake: FakeTime, deadline: float) -> list[float]:
    """Note the moment, on ``fake``, of every read of saved state past ``deadline``."""
    late: list[float] = []
    original = state.checkpointer.aget_tuple

    async def noted(config: Any) -> Any:  # noqa: ANN401
        if fake() > deadline:
            late.append(fake())
        return await original(config)

    state.checkpointer.aget_tuple = noted  # type: ignore[method-assign]
    return late


EXPIRIES: Final[dict[str, tuple[list[Turn[Any]], list[Turn[Any]], int, int]]] = {
    "during a request": ([(RUN_DEADLINE_SECONDS + 1, None)], [], 1, 0),
    "during a retry pause": (
        [(RUN_DEADLINE_SECONDS - 0.2, ServiceBusy("overloaded"))],
        [],
        1,
        0,
    ),
    "an answer that held the loop past the limit": (
        [(RUN_DEADLINE_SECONDS + 1, ok(fixture_week_plan()))],
        [],
        1,
        0,
    ),
    "a late critic answer": (
        [(1.0, ok(fixture_week_plan()))],
        [(RUN_DEADLINE_SECONDS, ok(accepting()))],
        1,
        1,
    ),
}


@pytest.mark.parametrize("expiry", list(EXPIRIES))
def test_a_deadline_during_model_work_ends_the_run_with_the_prior_plan_intact(
    tmp_path: pathlib.Path, expiry: str
) -> None:
    """The deadline passes during a request, a retry pause, an answer
    that held the loop, or a late critic answer; no request starts after it, the run ends
    timed out with the budget's timeout step, its settle is never asked for, no saved
    state is read after the deadline, and the evening's plan stays."""
    planner_turns, critic_turns, planner_calls, critic_calls = EXPIRIES[expiry]
    fake = FakeTime()
    state = application_on(tmp_path, fake)
    try:

        async def scenario() -> tuple[PlanMade, PlanMade, RunState, list[int], list[float]]:
            prior = await make_plan(accepted_graph(state), PLAN_DATE, state, budget=fake.budget())
            await settled_down(state)
            planner: Spending[DailyPlan] = Spending(fake, *planner_turns)
            critic: Spending[CriticVerdict] = Spending(fake, *critic_turns)
            graph = plan_graph_for(state, planner=planner, critic=critic)
            calls = settles_then(state, lambda: None)
            budget = fake.budget()
            late = reading_saved_state(state, fake, fake() + RUN_DEADLINE_SECONDS)
            made = await make_plan(graph, PLAN_DATE, state, budget=budget)
            ended = await until(lambda: ended_run_of(state, made.view.thread_id))
            await settled_down(state)
            assert (planner.calls, critic.calls) == (planner_calls, critic_calls)
            return prior, made, ended, calls, late

        prior, made, ended, calls, late = fake.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert prior.record is not None
    assert made.view.outcome == "timed_out"
    assert made.view.steps
    assert made.view.steps[-1].expected == "an answer inside the run's time"
    assert (ended.status, ended.reason) == ("ended", "timed_out")
    assert calls == []
    assert late == []
    assert latest is not None
    assert latest.draft_id == prior.record.draft_id


def test_a_run_ended_before_the_deadline_is_answered_with_its_reason_when_the_loop_was_held(
    tmp_path: pathlib.Path,
) -> None:
    """The graph's last node commits ``checks_failed`` before the deadline, and
    the request finds the result only once the deadline has gone; the answer gives the
    committed reason, never timed out, and the record keeps it."""
    fake = FakeTime()
    state = replace(application_on(tmp_path, fake), detached=EndsPastTheDeadline(fake))
    try:
        plans = [ok(forgetful_fixture_plan()) for _ in range(5)]
        graph = plan_graph_for(state, planner=Scripted(*plans), critic=critic())

        async def scenario() -> tuple[PlanMade, float]:
            budget = fake.budget()
            made = await make_plan(graph, PLAN_DATE, state, budget=budget)
            left = budget.remaining()
            await settled_down(state)
            return made, left

        made, left = fake.run(scenario())
        ended = state.drafts.run_status(made.view.thread_id)
    finally:
        state.close()

    assert left == 0
    assert made.view.outcome == "checks_failed"
    assert made.view.draft_id is None
    assert ended is not None
    assert (ended.status, ended.reason) == ("ended", "checks_failed")


def test_a_paused_graph_found_after_the_deadline_is_ended_without_a_settle(
    tmp_path: pathlib.Path,
) -> None:
    """The graph pauses with its draft before the deadline, and the request finds
    the result only once the deadline has gone; no settle is asked for, the run ends timed
    out, and its draft is deleted."""
    fake = FakeTime()
    state = replace(application_on(tmp_path, fake), detached=EndsPastTheDeadline(fake))
    try:
        calls = settles_then(state, lambda: None)

        async def scenario() -> tuple[PlanMade, RunState]:
            made = await make_plan(accepted_graph(state), PLAN_DATE, state, budget=fake.budget())
            ended = await until(lambda: ended_run_of(state, made.view.thread_id))
            await settled_down(state)
            return made, ended

        made, ended = fake.run(scenario())
        draft = state.drafts.get(f"draft:{made.view.thread_id}")
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert calls == []
    assert made.view.outcome == "timed_out"
    assert made.record is None
    assert (ended.status, ended.reason) == ("ended", "timed_out")
    assert draft is None
    assert latest is None


def value_of(tmp_path: pathlib.Path, sql: str, *parameters: object) -> object:
    """One value from the household's file, read through a connection of its own."""
    connection = sqlite3.connect(files_in(tmp_path)["BLOSSOM_DATABASE_PATH"])
    try:
        (value,) = connection.execute(sql, parameters).fetchone()
    finally:
        connection.close()
    return value


def test_a_press_whose_admission_comes_before_the_settle_takes_the_store_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """A press admitted after the first run asked for its settle, but before the
    settle took the store, is refused naming the first run; one row, and the first run
    then publishes."""
    state = application(tmp_path)
    asked = threading.Event()
    go = threading.Event()

    def before_the_store(original: Callable[..., Any]) -> Callable[..., Any]:
        def settle(*args: object, **kwargs: object) -> object:
            asked.set()
            go.wait(5)
            return original(*args, **kwargs)

        return settle

    calls = settling_through(state, before_the_store)
    try:

        async def scenario() -> tuple[AlreadyPlanning, PlanMade, int]:
            first = asyncio.ensure_future(make_plan(accepted_graph(state), PLAN_DATE, state))
            await asyncio.to_thread(asked.wait, 5)
            second = plan_graph_for(state, planner=Scripted(), critic=Scripted())
            with pytest.raises(AlreadyPlanning) as refused:
                await make_plan(second, PLAN_DATE, state)
            rows = rows_of(tmp_path)
            go.set()
            return refused.value, await asyncio.wait_for(first, 10), rows

        refused, made, rows = asyncio.run(scenario())
    finally:
        go.set()
        state.close()

    assert refused.run.run_id == made.view.thread_id
    assert rows == 1
    assert calls == [1]
    assert made.record is not None


def test_a_press_during_the_settles_commit_is_admitted_once_the_plan_is_published(
    tmp_path: pathlib.Path,
) -> None:
    """A press that arrives while the settle commits waits for the store and
    is admitted once the first run is published, expecting that plan, and publishes in
    turn."""
    state = application(tmp_path)
    committing = threading.Event()
    proceed = threading.Event()

    def at_the_commit() -> None:
        committing.set()
        proceed.wait(5)

    settles_then(state, at_the_commit)
    try:

        async def scenario() -> tuple[PlanMade, PlanMade]:
            first = asyncio.ensure_future(make_plan(accepted_graph(state), PLAN_DATE, state))
            await asyncio.to_thread(committing.wait, 5)
            second = asyncio.ensure_future(make_plan(accepted_graph(state), PLAN_DATE, state))
            await asyncio.sleep(0.2)
            proceed.set()
            return await asyncio.wait_for(first, 10), await asyncio.wait_for(second, 10)

        first, second = asyncio.run(scenario())
    finally:
        proceed.set()
        state.close()
    expected = value_of(
        tmp_path, "SELECT base_order FROM runs WHERE thread_id=?", second.view.thread_id
    )
    published = value_of(
        tmp_path, "SELECT published_order FROM drafts WHERE thread_id=?", first.view.thread_id
    )

    assert first.record is not None
    assert second.record is not None
    assert expected == published


def test_a_settle_whose_read_back_cannot_be_made_is_unconfirmed_then_found_published(
    tmp_path: pathlib.Path,
) -> None:
    """The settle commits and its call fails with time left, and the store can't
    be read for the read-back; the answer is unconfirmed with the run's id, which later
    reads published."""
    state = application(tmp_path)
    try:

        def lost(original: Callable[..., Any]) -> Callable[..., Any]:
            def settle_then_fail(*args: object, **kwargs: object) -> object:
                original(*args, **kwargs)
                msg = "the answer was lost"
                raise RuntimeError(msg)

            return settle_then_fail

        calls = settling_through(state, lost)
        reads = state.drafts.run_status

        def unreadable(
            run_id: str, wait: float = STORE_WAIT_SECONDS, *, reconcile: bool = True
        ) -> RunState | None:
            if not reconcile:
                msg = "disk I/O error"
                raise sqlite3.OperationalError(msg)
            return reads(run_id, wait, reconcile=reconcile)

        state.drafts.run_status = unreadable  # type: ignore[method-assign]

        async def scenario() -> BaseException | None:
            try:
                await make_plan(accepted_graph(state), PLAN_DATE, state)
            except Unconfirmed as error:
                return error
            return None

        caught = asyncio.run(scenario())
        assert isinstance(caught, Unconfirmed)
        later = reads(caught.run_id)
    finally:
        state.close()

    assert calls == [1]
    assert later is not None
    assert later.status == "published"


def test_an_error_before_the_settle_began_is_read_back_as_running_and_not_saved(
    tmp_path: pathlib.Path,
) -> None:
    """The settle's call fails before its transaction with an error that is not a
    typed refusal; the read-back finds the run still running, the answer is not saved, the
    settle was asked once, and the run ends interrupted with nothing published."""
    state = application(tmp_path)
    try:

        def broken(original: Callable[..., Any]) -> Callable[..., Any]:
            def settle(*args: object, **kwargs: object) -> object:
                msg = "the worker failed"
                raise RuntimeError(msg)

            return settle

        calls = settling_through(state, broken)

        async def scenario() -> tuple[BaseException | None, RunState]:
            caught: BaseException | None = None
            try:
                await make_plan(accepted_graph(state), PLAN_DATE, state)
            except NotSaved as error:
                caught = error
            latest = await until(state.drafts.latest_run)
            ended = await until(lambda: ended_run_of(state, latest.run_id))
            return caught, ended

        caught, ended = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert isinstance(caught, NotSaved)
    assert calls == [1]
    assert (ended.status, ended.reason) == ("ended", "interrupted")
    assert latest is None


def test_a_run_whose_ending_fails_blocks_presses_only_until_its_deadline(
    tmp_path: pathlib.Path,
) -> None:
    """A service failure at 10 s whose ending fails is answered as a service
    failure; a press at 20 s is refused naming the run, its evening and the seconds left;
    a failed admission leaves no row; past the deadline a press is admitted, and the old
    run reads timed out."""
    fake = FakeTime()
    state = application_on(tmp_path, fake)
    ending = state.drafts.end_run
    admission = state.drafts.admit_run

    def failing(*args: object, **kwargs: object) -> Any:  # noqa: ANN401
        msg = "disk I/O error"
        raise sqlite3.OperationalError(msg)

    try:

        async def scenario() -> tuple[PlanMade, AlreadyPlanning, int, PlanMade]:
            state.drafts.end_run = failing  # type: ignore[method-assign]
            planner: Spending[DailyPlan] = Spending(fake, (10.0, ServiceFailed("unavailable")))
            failed = await make_plan(
                plan_graph_for(state, planner=planner, critic=critic()),
                PLAN_DATE,
                state,
                budget=fake.budget(),
            )
            await settled_down(state)
            state.drafts.end_run = ending  # type: ignore[method-assign]
            fake.advance(10)
            idle = plan_graph_for(state, planner=Scripted(), critic=Scripted())
            with pytest.raises(AlreadyPlanning) as refused:
                await make_plan(idle, PLAN_DATE, state, budget=fake.budget())
            fake.advance(80)
            state.drafts.admit_run = failing  # type: ignore[method-assign]
            with pytest.raises(CouldNotStart):
                await make_plan(idle, PLAN_DATE, state, budget=fake.budget())
            rows = rows_of(tmp_path)
            state.drafts.admit_run = admission  # type: ignore[method-assign]
            later = await make_plan(accepted_graph(state), PLAN_DATE, state, budget=fake.budget())
            await settled_down(state)
            return failed, refused.value, rows, later

        failed, refused, rows, later = fake.run(scenario())
        old = state.drafts.run_status(failed.view.thread_id)
    finally:
        state.close()

    assert failed.view.outcome == "service_failed"
    assert refused.run.run_id == failed.view.thread_id
    assert refused.run.plan_date == PLAN_DATE
    assert seconds_to_wait(refused.run) == 71
    assert rows == 1
    assert later.record is not None
    assert old is not None
    assert (old.status, old.reason) == ("ended", "timed_out")


def test_a_start_whose_ending_of_stopped_runs_fails_does_not_serve(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ending the runs a stopped process left fails at startup, the start
    fails before serving and the household's files are closed, so the next start opens
    them."""

    def failing(self: DraftsStore, wait: float = STORE_WAIT_SECONDS) -> list[str]:
        msg = "disk I/O error"
        raise sqlite3.OperationalError(msg)

    settings = fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **files_in(tmp_path))
    with monkeypatch.context() as patched:
        patched.setattr(DraftsStore, "end_interrupted_runs", failing)
        with pytest.raises(sqlite3.OperationalError), TestClient(create_app(settings)):
            pass
    with TestClient(create_app(settings)) as client:
        served = client.get("/student/due-this-week")

    assert served.status_code == 200


def test_a_running_row_with_an_impossible_deadline_ends_and_the_next_press_is_admitted(
    tmp_path: pathlib.Path,
) -> None:
    """A running row whose deadline lies further ahead than any run's limit can't
    be this process's; the next press ends it interrupted and is admitted."""
    state = application(tmp_path)
    try:
        assert (
            state.drafts.admit_run(
                "plan:2026-08-19:before-a-reboot",
                plan_date=PLAN_DATE,
                deadline_mono=state.monotonic() + 10 * RUN_DEADLINE_SECONDS,
            )
            is None
        )
        made = asyncio.run(make_plan(accepted_graph(state), PLAN_DATE, state))
        old = state.drafts.run_status("plan:2026-08-19:before-a-reboot")
    finally:
        state.close()

    assert made.record is not None
    assert old is not None
    assert (old.status, old.reason) == ("ended", "interrupted")


def test_the_json_routes_name_another_evenings_running_run(tmp_path: pathlib.Path) -> None:
    """While a parent's run for tomorrow is running, her press for today and the
    family's JSON press are refused 409 with that run's id, evening and wait, in words true
    of another evening."""
    settings = fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **files_in(tmp_path))
    app = create_app(settings)
    app.dependency_overrides[plan_graphs] = scripted_graphs(
        lambda: [fixture_week_plan()], lambda: [accepting()]
    )
    tomorrow = PLAN_DATE + timedelta(days=1)
    with TestClient(app, headers=SAME_ORIGIN) as client:
        state = state_of(client)
        assert (
            state.drafts.admit_run(
                "plan:2026-08-20:family",
                plan_date=tomorrow,
                deadline_mono=state.monotonic() + RUN_DEADLINE_SECONDS,
            )
            is None
        )
        hers = client.post("/student/plans")
        family = client.post("/parent/plans", json={"plan_date": PLAN_DATE.isoformat()})
        rows = rows_of(tmp_path)

    for answer in (hers, family):
        assert answer.status_code == 409
        detail = answer.json()["detail"]
        assert detail["run_id"] == "plan:2026-08-20:family"
        assert detail["plan_date"] == tomorrow.isoformat()
        assert 1 <= detail["seconds_left"] <= RUN_DEADLINE_SECONDS + 1
        assert detail["message"].startswith(
            "The last plan request, for Thursday, August 20, is still being finished."
        )
    assert hers.json()["detail"]["message"].endswith("Your homework updates are saved.")
    assert family.json()["detail"]["message"].endswith("Her homework updates are saved.")
    assert rows == 1


def test_a_parents_run_for_tomorrow_holds_the_households_one_run(
    tmp_path: pathlib.Path,
) -> None:
    """While a parent's run for tomorrow is being made, her press for today is refused
    naming tomorrow's run; once that run is terminal her press is admitted and publishes."""
    state = application(tmp_path)
    tomorrow = PLAN_DATE + timedelta(days=1)
    try:

        async def scenario() -> tuple[AlreadyPlanning, PlanMade, PlanMade]:
            gate = asyncio.Event()
            family = asyncio.ensure_future(
                make_plan(
                    plan_graph_for(state, planner=waits_for(gate), critic=critic()),
                    tomorrow,
                    state,
                )
            )
            await until(state.drafts.latest_run)
            idle = plan_graph_for(state, planner=Scripted(), critic=Scripted())
            with pytest.raises(AlreadyPlanning) as refused:
                await make_plan(idle, PLAN_DATE, state)
            gate.set()
            made = await asyncio.wait_for(family, 10)
            hers = await make_plan(accepted_graph(state), PLAN_DATE, state)
            return refused.value, made, hers

        refused, made, hers = asyncio.run(scenario())
        family_run = state.drafts.run_status(made.view.thread_id)
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert refused.run.run_id == made.view.thread_id
    assert refused.run.plan_date == tomorrow
    assert family_run is not None
    assert family_run.status != "running"
    assert hers.record is not None
    assert latest is not None
    assert latest.draft_id == hers.record.draft_id


def test_the_sweep_keeps_a_running_runs_thread_and_ends_it_past_its_deadline(
    tmp_path: pathlib.Path,
) -> None:
    """The sweep keeps the saved thread of a run still running and publishes and
    deletes nothing; once the run is past its deadline the sweep ends it timed out, its
    draft goes, and its thread is cleared."""
    fake = FakeTime()
    state = application_on(tmp_path, fake)
    run_id = "plan:2026-08-19:running"
    try:
        assert (
            state.drafts.admit_run(
                run_id, plan_date=PLAN_DATE, deadline_mono=fake() + RUN_DEADLINE_SECONDS
            )
            is None
        )

        async def scenario() -> tuple[bool, Any, bool, Any]:
            await accepted_graph(state).ainvoke(
                PlanState(plan_date=PLAN_DATE, rounds=0),
                config=run_config(run_id),
                durability=DURABILITY,
                context=fake.budget(),
            )
            config: Any = {"configurable": {"thread_id": run_id}}
            during = await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
            kept = await state.checkpointer.aget_tuple(config) is not None
            fake.advance(RUN_DEADLINE_SECONDS + 1)
            after = await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
            gone = await state.checkpointer.aget_tuple(config) is None
            return kept, during, gone, after

        kept, during, gone, after = fake.run(scenario())
        ended = state.drafts.run_status(run_id)
        draft = state.drafts.get(f"draft:{run_id}")
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert kept
    assert during.ended == ()
    assert run_id not in during.cleared
    assert after.ended == (run_id,)
    assert run_id in after.cleared
    assert gone
    assert ended is not None
    assert (ended.status, ended.reason) == ("ended", "timed_out")
    assert draft is None
    assert latest is None


def test_cleanup_and_timing_failures_change_no_status_and_the_sweep_clears_later(
    tmp_path: pathlib.Path,
) -> None:
    """Tidying a thread and the final timing write both fail; the runs' status and the
    plan on the page are what they were, the next press is admitted and publishes, and a
    later sweep clears the thread left behind."""
    state = application(tmp_path)
    deleting = state.checkpointer.adelete_thread

    def no_timing(*args: object, **kwargs: object) -> None:
        msg = "disk I/O error"
        raise sqlite3.OperationalError(msg)

    async def no_delete(thread_id: str) -> None:
        msg = "the saver failed"
        raise RuntimeError(msg)

    state.drafts.record_timing = no_timing  # type: ignore[method-assign]
    state.checkpointer.adelete_thread = no_delete  # type: ignore[method-assign]
    try:

        async def scenario() -> tuple[PlanMade, PlanMade, Any]:
            plans = [ok(forgetful_fixture_plan()) for _ in range(5)]
            failing = plan_graph_for(state, planner=Scripted(*plans), critic=critic())
            failed = await make_plan(failing, PLAN_DATE, state)
            await settled_down(state)
            made = await make_plan(accepted_graph(state), PLAN_DATE, state)
            await settled_down(state)
            state.checkpointer.adelete_thread = deleting  # type: ignore[method-assign]
            swept = await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
            return failed, made, swept

        failed, made, swept = asyncio.run(scenario())
        ended = state.drafts.run_status(failed.view.thread_id)
        published = state.drafts.run_status(made.view.thread_id)
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert failed.view.outcome == "checks_failed"
    assert ended is not None
    assert (ended.status, ended.reason) == ("ended", "checks_failed")
    assert made.record is not None
    assert published is not None
    assert published.status == "published"
    assert latest is not None
    assert latest.draft_id == made.record.draft_id
    assert failed.view.thread_id in swept.cleared
    assert made.view.thread_id not in swept.cleared


def read_held(state: ApplicationState, seconds: float) -> threading.Thread:
    """A page's reading of the record held open on its own connection for ``seconds``, on a
    thread of its own, as a page read across a settle's commit would be."""
    started = threading.Event()

    def hold() -> None:
        with state.project_state.reading():
            state.project_state.is_empty()
            started.set()
            time.sleep(seconds)

    thread = threading.Thread(target=hold)
    thread.start()
    assert started.wait(5)
    return thread


def with_a_reader(state: ApplicationState, seconds: float, held: list[threading.Thread]) -> None:
    """Hold a page's reading open from the moment the settle is asked for."""

    def reading_first(original: Callable[..., Any]) -> Callable[..., Any]:
        def settle(*args: object, **kwargs: object) -> object:
            held.append(read_held(state, seconds))
            return original(*args, **kwargs)

        return settle

    settling_through(state, reading_first)


def test_a_commit_refused_behind_a_reader_with_time_left_is_read_back_and_not_saved(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page's read held across the settle's commit makes the commit fail and
    roll back with time left; the read-back finds the run running, the answer is not saved,
    and the run ends interrupted with nothing published."""
    monkeypatch.setattr(drafts_module, "STORE_WAIT_SECONDS", 0.3)
    state = application(tmp_path)
    held: list[threading.Thread] = []
    with_a_reader(state, 1.2, held)
    try:

        async def scenario() -> tuple[BaseException | None, RunState]:
            caught: BaseException | None = None
            try:
                await make_plan(accepted_graph(state), PLAN_DATE, state, budget=RunBudget(30))
            except NotSaved as error:
                caught = error
            latest = await until(state.drafts.latest_run)
            ended = await until(lambda: ended_run_of(state, latest.run_id), 10)
            return caught, ended

        caught, ended = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        for thread in held:
            thread.join(5)
        state.close()

    assert isinstance(caught, NotSaved)
    assert len(held) == 1
    assert (ended.status, ended.reason) == ("ended", "interrupted")
    assert latest is None


def test_a_commit_behind_a_reader_lands_when_the_reader_ends_in_time(
    tmp_path: pathlib.Path,
) -> None:
    """The settle's commit waits for a page's read
    held across it, lands, and the plan is published and reported."""
    state = application(tmp_path)
    held: list[threading.Thread] = []
    with_a_reader(state, 0.3, held)
    try:
        made = asyncio.run(make_plan(accepted_graph(state), PLAN_DATE, state, budget=RunBudget(5)))
        status = state.drafts.run_status(made.view.thread_id)
    finally:
        for thread in held:
            thread.join(5)
        state.close()

    assert len(held) == 1
    assert made.record is not None
    assert status is not None
    assert status.status == "published"


def test_a_commit_behind_a_reader_past_the_grace_is_unconfirmed_without_a_lookup(
    tmp_path: pathlib.Path,
) -> None:
    """A page's read held past the grace: the commit fails at the deadline plus
    the grace and rolls back; the answer is unconfirmed at once with no lookup of the
    record, and a later check ends the run timed out with nothing published."""
    state = application(tmp_path)
    held: list[threading.Thread] = []
    with_a_reader(state, 4.0, held)
    reads = state.drafts.run_status
    looked: list[int] = []

    def spied(
        run_id: str, wait: float = STORE_WAIT_SECONDS, *, reconcile: bool = True
    ) -> RunState | None:
        looked.append(1)
        return reads(run_id, wait, reconcile=reconcile)

    state.drafts.run_status = spied  # type: ignore[method-assign]
    try:

        async def scenario() -> tuple[BaseException | None, float]:
            began = time.monotonic()
            try:
                await make_plan(
                    accepted_graph(state), PLAN_DATE, state, budget=RunBudget(seconds=2.0)
                )
            except Unconfirmed as error:
                return error, time.monotonic() - began
            return None, time.monotonic() - began

        caught, answered_in = asyncio.run(scenario())
        during = list(looked)
        for thread in held:
            thread.join(5)
        assert isinstance(caught, Unconfirmed)
        later = reads(caught.run_id)
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        for thread in held:
            thread.join(5)
        state.close()

    assert during == []
    assert answered_in < 2.0 + SETTLE_GRACE_SECONDS + 0.5
    assert later is not None
    assert (later.status, later.reason) == ("ended", "timed_out")
    assert latest is None


class WriterHeld:
    """The household's file with its writer held by another connection until a timer ends
    the hold."""

    def __init__(self, path: str, seconds: float) -> None:
        self.connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.connection.execute("BEGIN IMMEDIATE")
        self.timer = threading.Timer(seconds, self.end)
        self.timer.start()

    def end(self) -> None:
        self.connection.execute("ROLLBACK")
        self.connection.close()


def test_a_settle_waiting_for_the_writer_past_the_deadline_is_timed_out(
    tmp_path: pathlib.Path,
) -> None:
    """Another connection holds the file's writer past the run's deadline; the
    settle waits at most what is left, the answer says timed out by the deadline and its
    grace, and a run with time to wait publishes once the writer is let go."""
    state = application(tmp_path)
    path = files_in(tmp_path)["BLOSSOM_DATABASE_PATH"]
    holds: list[WriterHeld] = []

    def behind_a_writer(original: Callable[..., Any]) -> Callable[..., Any]:
        def settle(*args: object, **kwargs: object) -> object:
            if not holds:
                holds.append(WriterHeld(path, 2.0))
            return original(*args, **kwargs)

        return settle

    settling_through(state, behind_a_writer)
    try:

        async def scenario() -> tuple[PlanMade, float, PlanMade]:
            began = time.monotonic()
            short = await make_plan(
                accepted_graph(state), PLAN_DATE, state, budget=RunBudget(seconds=1.0)
            )
            answered_in = time.monotonic() - began
            await until(lambda: ended_run_of(state, short.view.thread_id), 5)
            holds.append(WriterHeld(path, 0.7))
            neighbor = await make_plan(
                accepted_graph(state), PLAN_DATE, state, budget=RunBudget(seconds=5.0)
            )
            return short, answered_in, neighbor

        short, answered_in, neighbor = asyncio.run(scenario())
        ended = state.drafts.run_status(short.view.thread_id)
    finally:
        for hold in holds:
            hold.timer.join(5)
        state.close()

    assert short.view.outcome == "timed_out"
    assert answered_in < 1.0 + SETTLE_GRACE_SECONDS
    assert ended is not None
    assert (ended.status, ended.reason) == ("ended", "timed_out")
    assert neighbor.record is not None


def test_a_decision_on_an_unpublished_or_unresumable_draft_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """A decision on a running run's unpublished draft is refused, and so is one on a
    published plan whose saved review step is missing, with words that say why; neither
    draft changes."""
    settings = fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **files_in(tmp_path))
    made_at = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)
    with TestClient(create_app(settings), headers=SAME_ORIGIN) as client:
        state = state_of(client)
        settled_run(
            state.drafts,
            Draft(draft_id="draft:plan:2026-08-19:kept", body="Plan", created_at=made_at),
            thread_id="plan:2026-08-19:kept",
            plan_date=PLAN_DATE,
        )
        assert (
            state.drafts.admit_run(
                "plan:2026-08-19:running",
                plan_date=PLAN_DATE,
                deadline_mono=state.monotonic() + RUN_DEADLINE_SECONDS,
            )
            is None
        )
        state.drafts.record_waiting(
            Draft(draft_id="draft:plan:2026-08-19:running", body="Plan", created_at=made_at),
            thread_id="plan:2026-08-19:running",
            plan_date=PLAN_DATE,
            outcome="accepted",
        )
        unpublished = client.post(
            "/parent/actions/decide/draft:plan:2026-08-19:running", data={"decision": "approve"}
        )
        unresumable = client.post(
            "/parent/actions/decide/draft:plan:2026-08-19:kept", data={"decision": "approve"}
        )
        running = state.drafts.get("draft:plan:2026-08-19:running")
        kept = state.drafts.get("draft:plan:2026-08-19:kept")

    assert unpublished.status_code == 409
    assert unresumable.status_code == 409
    assert "can't be approved or refused here" in unescape(unresumable.text)
    assert running is not None
    assert (running.published, running.decision) == (False, None)
    assert kept is not None
    assert kept.decision is None


async def pressed_then_gone(
    app: Any,  # noqa: ANN401
    path: str,
    headers: list[tuple[bytes, bytes]],
) -> list[dict[str, Any]]:
    """Send a plan press to ``app`` through its middleware as a server would, and report the
    connection closed as soon as the form has been read; what the app sent back."""
    sent: list[dict[str, Any]] = []
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "extensions": {},
    }
    await app(scope, receive, send)
    return sent


@pytest.mark.parametrize("signed", [False, True], ids=["sign-in off", "sign-in on"])
def test_a_press_whose_connection_closes_after_the_form_still_publishes(
    tmp_path: pathlib.Path, signed: bool
) -> None:
    """The connection closes once the press's form is read, through the household's
    own middleware with the sign-in off and on; the run goes on and publishes, and its
    record says published."""
    settings = (
        signed_in_household(tmp_path)
        if signed
        else fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **files_in(tmp_path))
    )
    app = create_app(settings)
    app.dependency_overrides[plan_graphs] = scripted_graphs(
        lambda: [fixture_week_plan()], lambda: [accepting()]
    )
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        if signed:
            signed_in(client, HERS)
        headers = [
            (b"host", b"testserver"),
            (b"origin", ORIGIN.encode()),
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", b"0"),
        ]
        cookies = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
        if cookies:
            headers.append((b"cookie", cookies.encode()))
        assert client.portal is not None
        sent = client.portal.call(pressed_then_gone, app, "/student/actions/plan", headers)
        state = state_of(client)
        latest = state.drafts.latest_run()
        plan = state.drafts.latest_for(PLAN_DATE)

    assert sent
    assert sent[0]["status"] == 303
    assert latest is not None
    assert latest.status == "published"
    assert plan is not None
    assert plan.thread_id == latest.run_id


THE_RUNS_OWN: Final = ('INSERT INTO "runs"', 'INSERT INTO "drafts"', 'INSERT INTO "steps"')


def her_rows(tmp_path: pathlib.Path) -> list[str]:
    """Every row of the household's file but the runs' own: her record, her reports and
    selections, her signals and her requests, as statements that would make them again."""
    return [
        line
        for line in every_row(pathlib.Path(files_in(tmp_path)["BLOSSOM_DATABASE_PATH"]))
        if not line.startswith(THE_RUNS_OWN)
    ]


def test_runs_that_end_without_a_plan_leave_her_records_as_they_were(
    tmp_path: pathlib.Path,
) -> None:
    """A run whose plans fail the checks, one whose plan couldn't be saved, and one
    that failed on the way change nothing of hers: every row outside the runs' own tables is
    as it was."""
    state = application(tmp_path)
    try:
        before = her_rows(tmp_path)

        async def scenario() -> tuple[PlanMade, BaseException | None, BaseException | None]:
            plans = [ok(forgetful_fixture_plan()) for _ in range(5)]
            failing = plan_graph_for(state, planner=Scripted(*plans), critic=critic())
            checks_failed = await make_plan(failing, PLAN_DATE, state)
            await settled_down(state)

            settle = state.drafts.settle_run

            def busy(*args: object, **kwargs: object) -> Any:  # noqa: ANN401
                raise StoreBusy

            state.drafts.settle_run = busy  # type: ignore[method-assign]
            not_saved: BaseException | None = None
            try:
                await make_plan(accepted_graph(state), PLAN_DATE, state)
            except NotSaved as error:
                not_saved = error
            latest = await until(state.drafts.latest_run)
            await until(lambda: ended_run_of(state, latest.run_id))
            await settled_down(state)
            state.drafts.settle_run = settle  # type: ignore[method-assign]

            async def broken(messages: Sequence[BaseMessage]) -> ModelAnswer[DailyPlan]:
                msg = "the planner failed"
                raise RuntimeError(msg)

            interrupted: BaseException | None = None
            try:
                await make_plan(
                    plan_graph_for(state, planner=broken, critic=critic()), PLAN_DATE, state
                )
            except RuntimeError as error:
                interrupted = error
            latest = await until(state.drafts.latest_run)
            await until(lambda: ended_run_of(state, latest.run_id))
            await settled_down(state)
            return checks_failed, not_saved, interrupted

        checks_failed, not_saved, interrupted = asyncio.run(scenario())
        after = her_rows(tmp_path)
        latest_plan = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert checks_failed.view.outcome == "checks_failed"
    assert isinstance(not_saved, NotSaved)
    assert isinstance(interrupted, RuntimeError)
    assert latest_plan is None
    assert after == before


def test_a_held_review_that_cannot_be_read_ends_the_run_so_the_next_press_is_admitted(
    tmp_path: pathlib.Path,
) -> None:
    """Reading the evening's held review fails with an error of its own before the deadline:
    the run is ended interrupted before the failure is answered, and a press right after is
    admitted and publishes."""
    state = application(tmp_path)
    try:

        async def scenario() -> tuple[PlanMade, RunState | None, PlanMade]:
            first = await make_plan(accepted_graph(state), PLAN_DATE, state)
            await settled_down(state)
            original = state.checkpointer.aget_tuple

            async def failing(config: Any) -> Any:  # noqa: ANN401
                if config["configurable"]["thread_id"] == first.view.thread_id:
                    msg = "the saver failed"
                    raise RuntimeError(msg)
                return await original(config)

            state.checkpointer.aget_tuple = failing  # type: ignore[method-assign]
            with pytest.raises(RuntimeError):
                await make_plan(accepted_graph(state), PLAN_DATE, state)
            ended = state.drafts.latest_run()
            state.checkpointer.aget_tuple = original  # type: ignore[method-assign]
            again = await make_plan(accepted_graph(state), PLAN_DATE, state)
            await settled_down(state)
            return first, ended, again

        first, ended, again = asyncio.run(scenario())
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert first.record is not None
    assert ended is not None
    assert (ended.status, ended.reason) == ("ended", "interrupted")
    assert again.record is not None
    assert latest is not None
    assert latest.draft_id == again.record.draft_id


def test_a_held_review_that_cannot_be_read_frees_the_decision_lock_before_the_run_ends(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a run whose held review couldn't be read waits for its ending, a parent's
    decision on the evening's plan goes ahead at once: the run holds no decision lock."""
    state = application(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    try:

        async def scenario() -> tuple[PlanMade, DecisionView, bool, RunState | None]:
            first = await make_plan(accepted_graph(state), PLAN_DATE, state)
            await settled_down(state)
            assert first.record is not None

            async def unreadable(*args: object, **kwargs: object) -> Any:  # noqa: ANN401
                msg = "the saver failed"
                raise RuntimeError(msg)

            monkeypatch.setattr(runs_module, "finish_held_reviews", unreadable)
            original = state.drafts.end_run

            def held(run_id: str, **kwargs: Any) -> RunState | None:  # noqa: ANN401
                entered.set()
                release.wait(5)
                return original(run_id, **kwargs)

            state.drafts.end_run = held  # type: ignore[method-assign]
            second = asyncio.ensure_future(make_plan(accepted_graph(state), PLAN_DATE, state))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                decided = await asyncio.wait_for(
                    decide_draft(
                        state,
                        lambda: accepted_graph(state),
                        first.record.draft_id,
                        DecisionRequest(approved=True, reason=None),
                    ),
                    2,
                )
                ending_waited = not second.done()
            finally:
                release.set()
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(second, 10)
            ended = state.drafts.latest_run()
            await settled_down(state)
            return first, decided, ending_waited, ended

        first, decided, ending_waited, ended = asyncio.run(scenario())
    finally:
        release.set()
        state.close()

    assert first.record is not None
    assert decided.draft_id == first.record.draft_id
    assert decided.decision == "approved"
    assert ending_waited
    assert ended is not None
    assert ended.run_id != first.view.thread_id
    assert (ended.status, ended.reason) == ("ended", "interrupted")


# ------------------------------------------------- a process that stops mid-publication


class ProcessStopped(BaseException):
    """The process stopping where it stands, with nothing after that point run."""


class StopsAtTheCommit:
    """The drafts file's connection in a process that stops as its next commit begins: neither
    the commit nor the rollback after it runs, so the transaction stays open."""

    def __init__(self, connection: sqlite3.Connection, run_id: str) -> None:
        self.connection = connection
        self.run_id = run_id
        self.stopped = False
        self.written: tuple[str, int] | None = None
        self.open = False

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return getattr(self.connection, name)

    def commit(self) -> None:
        self.stopped = True
        self.open = self.connection.in_transaction
        status = self.connection.execute(
            "SELECT status FROM runs WHERE thread_id=?", (self.run_id,)
        ).fetchone()
        published = self.connection.execute(
            "SELECT published FROM drafts WHERE thread_id=?", (self.run_id,)
        ).fetchone()
        self.written = (str(status[0]), int(published[0]))
        raise ProcessStopped

    def rollback(self) -> None:
        if not self.stopped:
            self.connection.rollback()


def household_settings(tmp_path: pathlib.Path) -> Settings:
    return fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **files_in(tmp_path))


async def up_to_the_settle[T](
    settings: Settings, run_id: str, settle: Callable[[ApplicationState, str], T]
) -> tuple[PlanMade, T]:
    """One process over the household's files and a saved-state file: the evening's plan is
    published, ``run_id`` pauses at the gate with its draft, ``settle`` is asked, and the
    stores close with no answer given and no cleanup run."""
    async with open_checkpointer(settings.checkpoint_path) as checkpointer:
        state = build_application_state(settings, checkpointer)
        try:
            prior = await make_plan(accepted_graph(state), PLAN_DATE, state)
            await settled_down(state)
            blocking = state.drafts.admit_run(
                run_id,
                plan_date=PLAN_DATE,
                deadline_mono=state.monotonic() + RUN_DEADLINE_SECONDS,
            )
            assert blocking is None
            await accepted_graph(state).ainvoke(
                PlanState(plan_date=PLAN_DATE, rounds=0),
                config=run_config(run_id),
                durability=DURABILITY,
                context=RunBudget(),
            )
            assert [record.draft_id for record in state.drafts.unpublished()] == [f"draft:{run_id}"]
            settled = settle(state, run_id)
        finally:
            state.close()
    return prior, settled


def stopped_at_the_commit(state: ApplicationState, run_id: str) -> StopsAtTheCommit:
    """Ask the store's own settle, and stop the process as its transaction's commit begins."""
    stand_in = StopsAtTheCommit(state.drafts._connection, run_id)
    state.drafts._connection = stand_in  # type: ignore[assignment]
    with pytest.raises(ProcessStopped):
        state.drafts.settle_run(run_id)
    return stand_in


def settled_in_full(state: ApplicationState, run_id: str) -> Settled:
    return state.drafts.settle_run(run_id)


async def saved_threads(settings: Settings, *thread_ids: str) -> dict[str, SavedThread | None]:
    """Each thread's latest saved state in the saved-state file, read through a saver of its
    own once the restarted app has stopped."""
    async with open_checkpointer(settings.checkpoint_path) as checkpointer:
        return {thread_id: await saved_thread(checkpointer, thread_id) for thread_id in thread_ids}


def test_a_process_stopped_inside_the_settle_before_its_commit_publishes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """The settle writes the publication and its process stops before the commit; the file
    closes with the transaction open, which SQLite rolls back as it does a stopped process's
    journal. The next start ends the run interrupted and clears its draft and thread."""
    settings = household_settings(tmp_path)
    run_id = f"plan:{PLAN_DATE.isoformat()}:stopped-at-the-commit"
    prior, stopped = asyncio.run(up_to_the_settle(settings, run_id, stopped_at_the_commit))

    with TestClient(create_app(settings)) as client:
        restarted = state_of(client)
        ended = restarted.drafts.run_status(run_id)
        candidate = restarted.drafts.get(f"draft:{run_id}")
        latest = restarted.drafts.latest_for(PLAN_DATE)
    saved = asyncio.run(saved_threads(settings, run_id, prior.view.thread_id))

    assert stopped.open
    assert stopped.written == ("published", 1)
    assert ended is not None
    assert (ended.status, ended.reason) == ("ended", "interrupted")
    assert candidate is None
    assert saved[run_id] is None
    assert prior.record is not None
    assert latest is not None
    assert latest.draft_id == prior.record.draft_id
    assert (latest.published, latest.decision) == (True, None)
    assert reviewable(saved[prior.view.thread_id])


def test_a_process_stopped_after_the_settle_committed_keeps_the_plan_published_and_reviewable(
    tmp_path: pathlib.Path,
) -> None:
    """The settle commits and the process stops before any answer; after the next start the
    plan is published and the evening's latest, its run reads published, and a parent's
    approval resumes its thread and is recorded."""
    settings = household_settings(tmp_path)
    run_id = f"plan:{PLAN_DATE.isoformat()}:stopped-after-the-commit"
    prior, settled = asyncio.run(up_to_the_settle(settings, run_id, settled_in_full))
    assert prior.record is not None
    prior_draft = prior.record.draft_id

    with TestClient(create_app(settings), headers=SAME_ORIGIN) as client:
        restarted = state_of(client)
        status = restarted.drafts.run_status(run_id)
        latest = restarted.drafts.latest_for(PLAN_DATE)
        displaced = restarted.drafts.get(prior_draft)
        approved = client.post(
            f"/parent/approvals/draft:{run_id}", json={"approved": True, "reason": None}
        )
        decided = restarted.drafts.get(f"draft:{run_id}")
    saved = asyncio.run(saved_threads(settings, run_id, prior.view.thread_id))

    assert settled.run.status == "published"
    assert status is not None
    assert status.status == "published"
    assert latest is not None
    assert latest.draft_id == f"draft:{run_id}"
    assert displaced is not None
    assert displaced.decision == "superseded"
    assert approved.status_code == 200
    assert approved.json()["decision"] == "approved"
    assert decided is not None
    assert (decided.published, decided.decision) == (True, "approved")
    assert saved == {run_id: None, prior.view.thread_id: None}


@dataclass
class StoppedAndStarted:
    """What a household's app showed across a stop with a settle held at its commit, and the
    start after it."""

    answer: int
    body: dict[str, Any]
    held_at_the_stop: bool
    stopped_in: float
    stop_error: Exception | None
    claimed_after_the_stop: bool
    checked: dict[str, Any]
    pressed: int
    run: RunState | None
    prior: DraftRecord | None


def served(app: Any) -> httpx.AsyncClient:  # noqa: ANN401
    """A client that reaches ``app`` on the caller's own event loop, as its pages do."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=SAME_ORIGIN
    )


def accepting_graphs(seconds: float = RUN_DEADLINE_SECONDS) -> Callable[..., PlanGraphs]:
    """Her household's graphs with an accepted plan, each run given ``seconds``."""
    return model_graphs(
        lambda: Scripted(ok(fixture_week_plan())),
        critic,
        budget=lambda: RunBudget(seconds=seconds),
    )


async def stopped_while_a_settle_holds_the_writer(
    settings: Settings, *, let_go_after: float
) -> StoppedAndStarted:
    """One process: an app over the household's files publishes a plan and approves it, then
    answers a press whose settle is held at its commit, keeping the drafts store and the file's
    writer. The app stops through its own lifespan with the settle held, and lets it go
    ``let_go_after`` seconds into the stop. A new app then starts on the same files, reads the
    held run, and takes a press."""
    held = threading.Event()
    let_go = threading.Timer(let_go_after, held.set)

    def hold() -> None:
        held.wait(60)

    old = create_app(settings)
    old.dependency_overrides[plan_graphs] = accepting_graphs()
    stopping = old.router.lifespan_context(old)
    await stopping.__aenter__()
    try:
        state: ApplicationState = getattr(old.state, STATE_ATTRIBUTE)
        async with served(old) as client:
            assert (await client.post("/student/plans")).status_code == 201
            prior = state.drafts.latest_for(PLAN_DATE)
            assert prior is not None
            approval = {"approved": True, "reason": None}
            approved = await client.post(f"/parent/approvals/{prior.draft_id}", json=approval)
            assert approved.status_code == 200
            await settled_down(state)
            settles_then(state, hold)
            old.dependency_overrides[plan_graphs] = accepting_graphs(2.0)
            pressed = await client.post("/student/plans")
        held_at_the_stop = not held.is_set() and bool(state.detached)
    except BaseException:
        held.set()
        await stopping.__aexit__(None, None, None)
        raise
    let_go.start()
    began = time.monotonic()
    stop_error: Exception | None = None
    try:
        await stopping.__aexit__(None, None, None)
    except Exception as error:
        stop_error = error
    stopped_in = time.monotonic() - began
    try:
        claim_household(settings.database_path, settings.checkpoint_path).release()
    except AnotherProcessHasTheHousehold:
        claimed = False
    else:
        claimed = True
    run_id = str(pressed.json()["run_id"])
    new = create_app(settings)
    new.dependency_overrides[plan_graphs] = accepting_graphs()
    try:
        async with new.router.lifespan_context(new):
            restarted: ApplicationState = getattr(new.state, STATE_ATTRIBUTE)
            async with served(new) as client:
                checked = (await client.get(f"/student/plans/runs/{run_id}")).json()
                second = (await client.post("/student/plans")).status_code
            await settled_down(restarted)
            run = restarted.drafts.run_status(run_id)
            kept = restarted.drafts.get(prior.draft_id)
    finally:
        held.set()
        await asyncio.to_thread(let_go.join, 60)
    return StoppedAndStarted(
        answer=pressed.status_code,
        body=pressed.json(),
        held_at_the_stop=held_at_the_stop,
        stopped_in=stopped_in,
        stop_error=stop_error,
        claimed_after_the_stop=claimed,
        checked=checked,
        pressed=second,
        run=run,
        prior=kept,
    )


def test_a_stop_while_a_settle_holds_the_writer_ends_cleanly_and_the_next_start_serves(
    tmp_path: pathlib.Path,
) -> None:
    """Her press is answered unconfirmed while its settle still holds the drafts store and the
    file's writer at its commit. The app stops through its lifespan with the settle held, and
    the settle lets go inside the store's wait: the stop closes every store and frees the
    claim after it, the authorized plan stands published, the prior approval is kept, and the
    next start serves, finds the run, and publishes a new press."""
    settings = household_settings(tmp_path)
    seen = asyncio.run(stopped_while_a_settle_holds_the_writer(settings, let_go_after=1.0))

    assert seen.answer == 202
    assert seen.body["status"] == "unconfirmed"
    assert seen.held_at_the_stop
    assert seen.stop_error is None
    assert 0.9 <= seen.stopped_in < STORE_WAIT_SECONDS
    assert seen.claimed_after_the_stop
    assert seen.checked["status"] == "published"
    assert seen.pressed == 201
    assert seen.run is not None
    assert seen.run.status == "published"
    assert seen.prior is not None
    assert (seen.prior.published, seen.prior.decision) == (True, "approved")


# ------------------------------------------------------------------ her household, held runs

EARLIER: Final = due("earlier-aug-18", "Fractions practice", date(2026, 8, 18))
"""Homework due the day before the evening with no Done from her: earlier work to choose."""

ESSAY_ID: Final = "assignment-canal-essay"

NEXT_DAY: Final = PLAN_DATE + timedelta(days=1)


class HeldPlanner:
    """A planner that says when it is asked, then answers ``plan`` once ``released`` is set."""

    def __init__(self, plan: DailyPlan) -> None:
        self.plan = plan
        self.asked = threading.Event()
        self.released = threading.Event()
        self.calls = 0

    async def __call__(self, messages: Sequence[BaseMessage]) -> ModelAnswer[DailyPlan]:
        self.calls += 1
        self.asked.set()
        released = await asyncio.to_thread(self.released.wait, 5)
        assert released, "the planner was never released"
        return ok(self.plan)


def household_files(tmp_path: pathlib.Path) -> TestClient:
    """Her household on the pinned day over files in ``tmp_path``, as its own pages reach it."""
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **files_in(tmp_path)))
    return TestClient(app, follow_redirects=False, headers=SAME_ORIGIN)


@contextmanager
def held_household(tmp_path: pathlib.Path, planner: HeldPlanner) -> Iterator[TestClient]:
    """The same, with earlier work on record, every run asking ``planner`` and an accepting
    critic."""
    with household_files(tmp_path) as client:
        client.app.dependency_overrides[plan_graphs] = model_graphs(  # type: ignore[attr-defined]
            lambda: planner, critic
        )
        state_of(client).project_state.upsert_assignments([EARLIER])
        yield client


def pressed_while_held(
    client: TestClient, planner: HeldPlanner, meanwhile: Callable[[], object]
) -> Any:  # noqa: ANN401
    """Ask for her plan through the JSON route, and let ``meanwhile`` happen while the
    planner holds the run, before it answers."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        pressed = pool.submit(client.post, "/student/plans")
        try:
            assert planner.asked.wait(5), "the run never asked the planner"
            meanwhile()
        finally:
            planner.released.set()
        return pressed.result(timeout=10)


def with_earlier_put_off() -> DailyPlan:
    """The fixture week's plan, with the earlier work she chose put off."""
    whole = fixture_week_plan()
    return whole.model_copy(
        update={
            "deferred": [
                *whole.deferred,
                Deferral(assignment_id=EARLIER.assignment_id, reason="caught up on Saturday"),
            ]
        }
    )


def choose_earlier(state: ApplicationState) -> None:
    state.project_state.choose_catch_up(EARLIER.assignment_id, PLAN_DATE, include=True)


def mark_the_essay_done(state: ApplicationState) -> None:
    state.project_state.report_status(
        ESSAY_ID, "done", None, expected_head=None, now=OBSERVED_AT, today=PLAN_DATE
    )


def say_too_much(state: ApplicationState) -> None:
    state.workload_signals.record(PLAN_DATE)


def take_too_much_back(state: ApplicationState) -> None:
    for signal in state.workload_signals.for_evening(PLAN_DATE):
        assert state.workload_signals.withdraw(signal.signal_id)


def no_change(state: ApplicationState) -> None:
    return None


@dataclass(frozen=True)
class Revision:
    """One change to what a run read, made while the run is held, and how both pages and a
    parent's approval then say it."""

    name: str
    before: Callable[[ApplicationState], None]
    during: Callable[[ApplicationState], None]
    plan: Callable[[], DailyPlan]
    hers: str
    theirs: str
    done_named: list[str]


REVISIONS: Final = [
    Revision(
        "earlier work chosen",
        no_change,
        choose_earlier,
        fixture_week_plan,
        student_routes.ASSIGNMENTS_CHANGED,
        parent_routes.ASSIGNMENTS_CHANGED,
        [],
    ),
    Revision(
        "work marked done",
        no_change,
        mark_the_essay_done,
        fixture_week_plan,
        student_routes.ASSIGNMENTS_CHANGED,
        parent_routes.ASSIGNMENTS_CHANGED,
        [ESSAY_ID],
    ),
    Revision(
        "too much said",
        no_change,
        say_too_much,
        fixture_week_plan,
        student_routes.SIGNALED_SINCE,
        parent_routes.SIGNALED_SINCE,
        [],
    ),
    Revision(
        "too much taken back",
        say_too_much,
        take_too_much_back,
        light_fixture_plan,
        student_routes.SIGNAL_ENDED,
        parent_routes.SIGNAL_ENDED,
        [],
    ),
]


@pytest.mark.parametrize("revision", REVISIONS, ids=[item.name for item in REVISIONS])
def test_a_change_during_a_run_publishes_its_plan_stale_on_both_pages_and_unapproved(
    tmp_path: pathlib.Path, revision: Revision
) -> None:
    """Her choice, her Done or her signal changes while the planner is held: the plan is
    published, both pages say it does not fit the week as it stands, as they say it for any
    plan, and a parent's approval is refused with the same words."""
    planner = HeldPlanner(revision.plan())
    with held_household(tmp_path, planner) as client:
        state = state_of(client)
        revision.before(state)
        made = pressed_while_held(client, planner, lambda: revision.during(state))
        draft_id = made.json()["draft_id"]
        hers = client.get("/student/plans/today").json()
        her_page = words(client.get(HER_PAGE, params={"show_plan": "1"}, headers=PAGE_HEADERS).text)
        waiting = client.get("/parent/approvals").json()["waiting"]
        family = words(client.get("/parent", headers=PAGE_HEADERS).text)
        approve = client.post(f"/parent/approvals/{draft_id}", json={"approved": True})
        latest = state.drafts.latest_for(PLAN_DATE)

    assert made.status_code == 201
    assert planner.calls == 1
    assert hers["draft_id"] == draft_id
    assert hers["stale"] == revision.hers
    assert revision.hers in her_page
    assert [work["assignment_id"] for work in hers["reported_done_work"]] == revision.done_named
    assert (student_routes.PLAN_INCLUDES_DONE in her_page) == bool(revision.done_named)
    assert [item["draft_id"] for item in waiting] == [draft_id]
    assert waiting[0]["stale"] == revision.theirs
    assert revision.theirs in family
    assert (parent_routes.PLAN_INCLUDES_DONE in family) == bool(revision.done_named)
    assert approve.status_code == 409
    assert approve.json()["detail"] == revision.theirs
    assert latest is not None
    assert latest.draft_id == draft_id
    assert latest.waiting


def chosen_rows(client: TestClient) -> list[tuple[str, str]]:
    """Every earlier-work choice the household's file keeps, as its day and its work."""
    connection: sqlite3.Connection = state_of(client).project_state._connection
    return sorted(
        (str(day), str(name))
        for day, name in connection.execute(
            "SELECT plan_date, assignment_id FROM catch_up_choices"
        ).fetchall()
    )


def test_a_run_that_finishes_past_midnight_keeps_the_evening_it_was_asked_for(
    tmp_path: pathlib.Path,
) -> None:
    """The household day turns while the planner is held: the plan is recorded for the
    evening the run was admitted for, the next day has no plan, and its choices start empty."""
    clock = SetClock(PLAN_DATE, datetime(2026, 8, 20, 3, 55, tzinfo=UTC))
    planner = HeldPlanner(with_earlier_put_off())
    with held_household(tmp_path, planner) as client:
        with_clock(client, clock)
        state = state_of(client)
        choose_earlier(state)

        def past_midnight() -> None:
            clock.day = NEXT_DAY
            clock.at = datetime(2026, 8, 20, 4, 5, tzinfo=UTC)

        made = pressed_while_held(client, planner, past_midnight)
        run = state.drafts.latest_run()
        evening = state.drafts.latest_for(PLAN_DATE)
        next_day = state.drafts.latest_for(NEXT_DAY)
        today = client.get("/student/plans/today")
        rows = chosen_rows(client)
        store = state.project_state
        planned_next = [item.assignment_id for item in read_week(store, store, NEXT_DAY).active()]

    assert made.status_code == 201
    assert made.json()["plan_date"] == PLAN_DATE.isoformat()
    assert run is not None
    assert (run.plan_date, run.status) == (PLAN_DATE, "published")
    assert evening is not None
    assert evening.draft_id == made.json()["draft_id"]
    assert next_day is None
    assert today.status_code == 404
    assert rows == [(PLAN_DATE.isoformat(), EARLIER.assignment_id)]
    assert EARLIER.assignment_id not in planned_next


def scripted_household(
    tmp_path: pathlib.Path, plans: list[DailyPlan], planners: list[Scripted[DailyPlan]]
) -> TestClient:
    """Her household whose runs ask scripted models: the planner answers ``plans``."""
    client = household_files(tmp_path)
    client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
        lambda: list(plans), lambda: [accepting()], planners=planners
    )
    return client


def test_chosen_earlier_work_is_read_by_the_run_and_kept_with_the_plan_by_its_id(
    tmp_path: pathlib.Path,
) -> None:
    """Work she chose from earlier homework is in the planner's brief, and the published plan
    keeps that work's own id among the ids it speaks about."""
    planners: list[Scripted[DailyPlan]] = []
    plan = with_earlier_put_off()
    with scripted_household(tmp_path, [plan], planners) as client:
        state = state_of(client)
        state.project_state.upsert_assignments([EARLIER])
        choose_earlier(state)
        made = client.post("/student/plans")
        published = state.drafts.latest_for(PLAN_DATE)

    assert made.status_code == 201
    assert [planner.calls for planner in planners] == [1]
    assert EARLIER.assignment_id in human_text(planners[0].briefs[0])
    assert published is not None
    ids = published.plan_assignment_ids
    assert ids is not None
    assert EARLIER.assignment_id in ids
    assert ids == sorted(ids)
    spoken = {block.assignment_id for block in plan.blocks}
    spoken |= {item.assignment_id for item in plan.deferred}
    assert spoken <= set(ids)


PAST_SCHOOL_DATE: Final = Assignment(
    assignment_id="assignment-map-quiz",
    course="Geography",
    title="Map quiz",
    due_date=None,
    dependencies=[],
    reported_submission_status="not_started",
)
"""Work with no date on record whose only date, the school portal's, is the day before the
evening: no plan for the evening can finish it in time."""


ANSWERED: Final = {"/student/plans": 409, "/parent/plans": 201, "/student/actions/plan": 409}
"""How each planning route answers a run that ended without a plan: her routes refuse it, and
the family's answers with the run's record, which its page lists."""


@pytest.mark.parametrize("route", list(ANSWERED))
def test_an_evening_with_a_date_problem_ends_at_its_first_step_and_asks_no_model(
    tmp_path: pathlib.Path, route: str
) -> None:
    """A request for an evening whose work has only a passed date ends at ``retrieve`` as a
    date problem: no plan, the work named, and no planner or critic asked."""
    planners: list[Scripted[DailyPlan]] = []
    critics: list[Scripted[Any]] = []
    with household_files(tmp_path) as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            list, list, planners=planners, critics=critics
        )
        state = state_of(client)
        state.project_state.upsert_assignments([PAST_SCHOOL_DATE])
        state.project_state.record_claims(
            PAST_SCHOOL_DATE.assignment_id, [record(SourceChannel.LMS, "2026-08-18")]
        )
        if route == "/student/actions/plan":
            answer = client.post(route, headers=PAGE_HEADERS)
        else:
            answer = client.post(route, json={})
        run = state.drafts.latest_run()
        ended = state.drafts.runs_without_a_draft()
        plan = state.drafts.latest_for(PLAN_DATE)

    assert answer.status_code == ANSWERED[route]
    if route == "/parent/plans":
        body = answer.json()
        assert (body["outcome"], body["draft_id"]) == ("date_problem", None)
        assert [work["assignment_id"] for work in body["past_due"]] == [
            PAST_SCHOOL_DATE.assignment_id
        ]
    elif route == "/student/plans":
        detail = answer.json()["detail"]
        assert "Map quiz (Geography, due August 18)" in detail
        assert "date_problem" not in detail
    else:
        assert "Map quiz (Geography, due August 18)" in words(answer.text)
    assert [planner.calls for planner in planners] == [0]
    assert [checker.calls for checker in critics] == [0]
    assert run is not None
    assert (run.status, run.reason) == ("ended", "date_problem")
    assert [item.outcome for item in ended] == ["date_problem"]
    assert [step.node for step in ended[0].steps] == ["retrieve"]
    assert plan is None


# ------------------------------------------- a decision and a newer plan, raced


APPROVE: Final = DecisionRequest(approved=True, reason=None)


def running_now(state: ApplicationState) -> RunState | None:
    """The household's newest run while it is still running; ``None`` otherwise."""
    found = state.drafts.latest_run()
    return found if found is not None and found.status == "running" else None


async def saved_thread_of(state: ApplicationState, thread_id: str) -> object:
    """What the saver holds for ``thread_id``; ``None`` once it is cleared."""
    return await state.checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})


def published_outside(tmp_path: pathlib.Path, draft_id: str, thread_id: str) -> None:
    """A plan for the evening written as published through a connection of its own, as
    another process would write one, outside any run of this process."""
    connection = sqlite3.connect(files_in(tmp_path)["BLOSSOM_DATABASE_PATH"])
    try:
        connection.execute(
            """
            INSERT INTO drafts (draft_id, thread_id, plan_date, status, outcome, body,
                                created_at, published, published_order)
            VALUES (?, ?, ?, 'DRAFT', 'accepted', 'elsewhere', ?, 1,
                    (SELECT COALESCE(MAX(published_order), 0) + 1 FROM drafts))
            """,
            (
                draft_id,
                thread_id,
                PLAN_DATE.isoformat(),
                datetime(2026, 8, 19, 22, 30, tzinfo=UTC).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_a_decision_pressed_while_a_newer_plan_publishes_is_refused_as_superseded(
    tmp_path: pathlib.Path,
) -> None:
    """A parent approves the waiting plan while a newer one for the evening is settling;
    the decision waits for the publication, is refused as already superseded, and the
    first plan's paused thread is cleared."""
    state = application(tmp_path)
    settling = threading.Event()
    go = threading.Event()

    def held(original: Callable[..., Any]) -> Callable[..., Any]:
        def settle(*args: object, **kwargs: object) -> object:
            settling.set()
            go.wait(5)
            return original(*args, **kwargs)

        return settle

    try:

        async def scenario() -> tuple[PlanMade, PlanMade, bool, HTTPException, object]:
            first = await make_plan(accepted_graph(state), PLAN_DATE, state)
            assert first.record is not None
            await settled_down(state)
            calls = settling_through(state, held)
            second = asyncio.ensure_future(make_plan(accepted_graph(state), PLAN_DATE, state))
            assert await asyncio.to_thread(settling.wait, 5)
            decision = asyncio.ensure_future(
                parent_routes.decide_draft(
                    state, lambda: accepted_graph(state), first.record.draft_id, APPROVE
                )
            )
            await asyncio.sleep(0)
            waited = not decision.done() and state.decision_lock.locked()
            go.set()
            made = await asyncio.wait_for(second, 10)
            with pytest.raises(HTTPException) as refused:
                await asyncio.wait_for(decision, 10)
            await settled_down(state)
            assert calls == [1]
            return (
                first,
                made,
                waited,
                refused.value,
                await saved_thread_of(state, first.view.thread_id),
            )

        first, made, waited, refused, saved = asyncio.run(scenario())
        assert first.record is not None
        prior = state.drafts.get(first.record.draft_id)
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        go.set()
        state.close()

    assert waited
    assert made.record is not None
    assert refused.status_code == 409
    assert "already superseded" in str(refused.detail)
    assert prior is not None
    assert prior.decision == "superseded"
    assert (
        value_of(
            tmp_path, "SELECT superseded_by FROM drafts WHERE draft_id=?", first.record.draft_id
        )
        == made.record.draft_id
    )
    assert latest is not None
    assert latest.draft_id == made.record.draft_id
    assert saved is None


def test_a_decision_on_a_plan_a_newer_one_superseded_is_refused_and_its_thread_cleared(
    tmp_path: pathlib.Path,
) -> None:
    """A newer plan for the evening is published by a settle whose route never cleared the
    plan it displaced. A decision on that plan is refused as already superseded, and its
    paused thread is cleared in the background."""
    state = application(tmp_path)
    try:

        async def scenario() -> tuple[PlanMade, object, HTTPException, object]:
            first = await make_plan(accepted_graph(state), PLAN_DATE, state)
            assert first.record is not None
            await settled_down(state)
            newer = "plan:2026-08-19:newer"
            settled_run(
                state.drafts,
                Draft(draft_id=f"draft:{newer}", body="Plan for Wednesday", created_at=OBSERVED_AT),
                thread_id=newer,
                plan_date=PLAN_DATE,
                now=state.monotonic,
            )
            kept = await saved_thread_of(state, first.view.thread_id)
            with pytest.raises(HTTPException) as refused:
                await parent_routes.decide_draft(
                    state, lambda: accepted_graph(state), first.record.draft_id, APPROVE
                )
            await settled_down(state)
            return first, kept, refused.value, await saved_thread_of(state, first.view.thread_id)

        first, kept, refused, cleared = asyncio.run(scenario())
        assert first.record is not None
        prior = state.drafts.get(first.record.draft_id)
    finally:
        state.close()

    assert kept is not None
    assert refused.status_code == 409
    assert "already superseded" in str(refused.detail)
    assert prior is not None
    assert prior.decision == "superseded"
    assert cleared is None


def test_a_decision_made_while_a_newer_plan_is_worked_on_stands_beside_it(
    tmp_path: pathlib.Path,
) -> None:
    """A parent approves the waiting plan while a newer run for the evening is still
    working; the newer plan then publishes as the evening's latest, and the approval
    stands, never superseded."""
    state = application(tmp_path)
    try:

        async def scenario() -> tuple[PlanMade, Any, RunState | None, PlanMade]:
            first = await make_plan(accepted_graph(state), PLAN_DATE, state)
            assert first.record is not None
            await settled_down(state)
            gate = asyncio.Event()
            second = asyncio.ensure_future(
                make_plan(
                    plan_graph_for(state, planner=waits_for(gate), critic=critic()),
                    PLAN_DATE,
                    state,
                )
            )
            running = await until(lambda: running_now(state))
            decided = await asyncio.wait_for(
                parent_routes.decide_draft(
                    state, lambda: accepted_graph(state), first.record.draft_id, APPROVE
                ),
                10,
            )
            still = run_of(state, running.run_id)
            gate.set()
            made = await asyncio.wait_for(second, 10)
            await settled_down(state)
            return first, decided, still, made

        first, decided, still, made = asyncio.run(scenario())
        assert first.record is not None
        prior = state.drafts.get(first.record.draft_id)
        latest = state.drafts.latest_for(PLAN_DATE)
        status = state.drafts.run_status(made.view.thread_id)
    finally:
        state.close()

    assert decided.decision == "approved"
    assert still is not None
    assert still.status == "running"
    assert made.record is not None
    assert status is not None
    assert status.status == "published"
    assert latest is not None
    assert latest.draft_id == made.record.draft_id
    assert prior is not None
    assert prior.decision == "approved"
    assert (
        value_of(
            tmp_path, "SELECT superseded_by FROM drafts WHERE draft_id=?", first.record.draft_id
        )
        is None
    )


def test_a_decision_pressed_during_a_commit_past_the_grace_waits_for_it_and_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """A newer plan's commit is held past the deadline and its grace: the answer is
    unconfirmed and the decision lock is free, and a decision pressed then waits for the
    pending commit, is refused as already superseded, and the newer plan is the latest."""
    fake = FakeTime()
    state = application_on(tmp_path, fake)
    release = threading.Event()
    reading = threading.Event()

    def past_the_grace() -> None:
        past_the_grace_on_its_loop(fake)
        release.wait(30)

    try:

        async def scenario() -> tuple[str, Unconfirmed, bool, bool, tuple[object, object]]:
            first = await make_plan(accepted_graph(state), PLAN_DATE, state, budget=fake.budget())
            assert first.record is not None
            prior = first.record.draft_id
            await settled_down(state)
            calls = settles_then(state, past_the_grace)
            with pytest.raises(Unconfirmed) as unconfirmed:
                await make_plan(accepted_graph(state), PLAN_DATE, state, budget=fake.budget())
            free = not state.decision_lock.locked()
            original_get = state.drafts.get

            def noticed(draft_id: str) -> Any:  # noqa: ANN401
                if draft_id == prior:
                    reading.set()
                return original_get(draft_id)

            state.drafts.get = noticed  # type: ignore[method-assign]
            decision = asyncio.ensure_future(
                parent_routes.decide_draft(state, lambda: accepted_graph(state), prior, APPROVE)
            )
            began = await asyncio.to_thread(reading.wait, 5)
            pending = (
                value_of(
                    tmp_path,
                    "SELECT status FROM runs WHERE thread_id=?",
                    unconfirmed.value.run_id,
                ),
                value_of(tmp_path, "SELECT decision FROM drafts WHERE draft_id=?", prior),
            )
            release.set()
            with pytest.raises(HTTPException) as refused:
                await decision
            assert refused.value.status_code == 409
            assert "already superseded" in str(refused.value.detail)
            await until(lambda: published_run_of(state, unconfirmed.value.run_id))
            await settled_down(state)
            assert calls == [1]
            return prior, unconfirmed.value, free, began, pending

        prior, caught, free, began, pending = fake.run(scenario())
        decided = state.drafts.get(prior)
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        release.set()
        state.close()

    assert caught.view().status == "unconfirmed"
    assert free
    assert began
    assert pending == ("running", None)
    assert decided is not None
    assert decided.decision == "superseded"
    assert latest is not None
    assert latest.thread_id == caught.run_id


def test_a_decision_that_lands_before_a_settle_begun_past_the_grace_stands_alone(
    tmp_path: pathlib.Path,
) -> None:
    """A newer plan's settle begins only past the deadline and its grace, after the answer
    said unconfirmed; a decision pressed meanwhile lands, and the settle then finds the
    deadline passed, so the run ends timed out and the approved plan stays the latest."""
    fake = FakeTime()
    state = application_on(tmp_path, fake)
    release = threading.Event()

    def begun_past_the_grace(original: Callable[..., Any]) -> Callable[..., Any]:
        def settle(*args: object, **kwargs: object) -> object:
            past_the_grace_on_its_loop(fake)
            release.wait(30)
            return original(*args, **kwargs)

        return settle

    try:

        async def scenario() -> tuple[str, Unconfirmed, Any, RunState]:
            first = await make_plan(accepted_graph(state), PLAN_DATE, state, budget=fake.budget())
            assert first.record is not None
            await settled_down(state)
            calls = settling_through(state, begun_past_the_grace)
            with pytest.raises(Unconfirmed) as unconfirmed:
                await make_plan(accepted_graph(state), PLAN_DATE, state, budget=fake.budget())
            decided = await parent_routes.decide_draft(
                state, lambda: accepted_graph(state), first.record.draft_id, APPROVE
            )
            release.set()
            ended = await until(lambda: ended_run_of(state, unconfirmed.value.run_id))
            await settled_down(state)
            assert calls == [1]
            return first.record.draft_id, unconfirmed.value, decided, ended

        prior, caught, decided, ended = fake.run(scenario())
        kept = state.drafts.get(prior)
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        release.set()
        state.close()

    assert caught.view().status == "unconfirmed"
    assert decided.decision == "approved"
    assert ended.reason == "timed_out"
    assert value_of(tmp_path, "SELECT COUNT(*) FROM drafts WHERE thread_id=?", caught.run_id) == 0
    assert kept is not None
    assert kept.decision == "approved"
    assert latest is not None
    assert latest.draft_id == prior


def test_a_plan_published_outside_a_run_before_its_settle_ends_it_overtaken(
    tmp_path: pathlib.Path,
) -> None:
    """A plan for the evening is published outside the run between its admission and its
    settle; the answer says overtaken, the run ends overtaken with its draft deleted and
    its thread cleared, and the other plan stays the evening's latest."""
    state = application(tmp_path)
    candidates: list[object] = []

    def counting(original: Callable[..., Any]) -> Callable[..., Any]:
        def settle(run_id: str, *args: object, **kwargs: object) -> object:
            candidates.append(
                value_of(tmp_path, "SELECT COUNT(*) FROM drafts WHERE thread_id=?", run_id)
            )
            return original(run_id, *args, **kwargs)

        return settle

    try:
        calls = settling_through(state, counting)

        async def scenario() -> tuple[RunState, PlanMade, object]:
            gate = asyncio.Event()
            request = asyncio.ensure_future(
                make_plan(
                    plan_graph_for(state, planner=waits_for(gate), critic=critic()),
                    PLAN_DATE,
                    state,
                )
            )
            running = await until(lambda: running_now(state))
            published_outside(tmp_path, "draft:plan:outside", "plan:outside")
            gate.set()
            made = await asyncio.wait_for(request, 10)
            await settled_down(state)
            return running, made, await saved_thread_of(state, running.run_id)

        running, made, saved = asyncio.run(scenario())
        ended = run_of(state, running.run_id)
        latest = state.drafts.latest_for(PLAN_DATE)
    finally:
        state.close()

    assert calls == [1]
    assert candidates == [1]
    assert made.record is None
    assert made.view.thread_id == running.run_id
    assert made.view.outcome == "overtaken"
    assert made.view.draft_id is None
    assert not made.view.waiting
    assert ended is not None
    assert (ended.status, ended.reason) == ("ended", "overtaken")
    assert value_of(tmp_path, "SELECT COUNT(*) FROM drafts WHERE thread_id=?", running.run_id) == 0
    assert latest is not None
    assert latest.draft_id == "draft:plan:outside"
    assert saved is None


# ------------------------------------------------ the loop while the file is held

HOLD_SECONDS: Final = 0.7
"""How long another connection keeps the household's file in these cases."""

DUE_AFTER_SECONDS: Final = 0.05
"""When the loop callback that measures the loop is due, after the hold begins."""

ON_TIME_SECONDS: Final = 0.15
"""How late that callback may run: far below the hold, so a loop held for it fails."""

TIMER_SLACK_SECONDS: Final = 0.3
"""Room past a wait's cap for the loop's timer to fire and the answer to be read."""

LONG_HOLD_SECONDS: Final = 2 * HOLD_SECONDS
"""A hold for an answer due inside its wait: longer than the wait and the slack together,
so an answer that waited out the hold fails."""


class FileHeld:
    """The household's file held by a connection of its own, as ``BEGIN IMMEDIATE`` holds
    its writer or ``BEGIN EXCLUSIVE`` holds out readers too, until a timer ends the hold."""

    def __init__(self, path: str, seconds: float, *, kind: str = "IMMEDIATE") -> None:
        self.connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.connection.execute(f"BEGIN {kind}")
        self.timer = threading.Timer(seconds, self.end)
        self.timer.start()

    def end(self) -> None:
        self.connection.execute("ROLLBACK")
        self.connection.close()


def a_callback_due(loop: asyncio.AbstractEventLoop, late: list[float]) -> None:
    """Ask ``loop``, from any thread, for a callback due shortly, and note in ``late`` how
    long after it was due it ran."""
    due = loop.time() + DUE_AFTER_SECONDS

    def ran() -> None:
        late.append(loop.time() - due)

    loop.call_soon_threadsafe(loop.call_at, due, ran)


def ran_on_time(late: list[float]) -> bool:
    """Whether the one callback asked for ran, and ran on time."""
    return len(late) == 1 and late[0] < ON_TIME_SECONDS


def test_a_press_behind_another_connections_writer_waits_off_the_event_loop(
    tmp_path: pathlib.Path,
) -> None:
    """A press while another connection holds the file's writer waits for it on a worker
    thread: the loop keeps its timers, the run is admitted and published once the writer is
    let go, and a press with less time than the hold can't start, inside its wait."""
    state = application(tmp_path)
    path = files_in(tmp_path)["BLOSSOM_DATABASE_PATH"]
    holds: list[FileHeld] = []
    try:

        async def scenario() -> tuple[Any, float, list[float], float, list[float], RunState | None]:
            loop = asyncio.get_running_loop()
            admitted_late: list[float] = []
            holds.append(FileHeld(path, HOLD_SECONDS))
            a_callback_due(loop, admitted_late)
            began = time.monotonic()
            made = await make_plan(accepted_graph(state), PLAN_DATE, state, budget=RunBudget(10))
            made_in = time.monotonic() - began
            await settled_down(state)
            refused_late: list[float] = []
            holds.append(FileHeld(path, LONG_HOLD_SECONDS))
            a_callback_due(loop, refused_late)
            began = time.monotonic()
            with pytest.raises(CouldNotStart):
                await make_plan(accepted_graph(state), PLAN_DATE, state, budget=RunBudget(0.3))
            refused_in = time.monotonic() - began
            for hold in holds:
                await asyncio.to_thread(hold.timer.join, 5)
            latest = await asyncio.to_thread(state.drafts.latest_run)
            return made, made_in, admitted_late, refused_in, refused_late, latest

        made, made_in, admitted_late, refused_in, refused_late, latest = asyncio.run(scenario())
    finally:
        for hold in holds:
            hold.timer.join(5)
        state.close()

    assert made.record is not None
    assert made_in >= HOLD_SECONDS - 0.1
    assert ran_on_time(admitted_late)
    assert refused_in < min(0.3, STORE_WAIT_SECONDS) + ADMISSION_ALLOWANCE_SECONDS + (
        TIMER_SLACK_SECONDS
    )
    assert ran_on_time(refused_late)
    assert latest is not None
    assert latest.run_id == made.view.thread_id


def test_a_decision_waits_for_the_held_file_off_the_event_loop(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent's decision on a waiting plan reads the draft, reads whether the evening still
    fits it, and writes the decision while another connection holds the file: each waits on a
    worker thread, the loop keeps its timers, and the decision lands once the file is let go."""
    state = application(tmp_path)
    path = files_in(tmp_path)["BLOSSOM_DATABASE_PATH"]
    holds: list[FileHeld] = []
    stale_late: list[float] = []
    stale_waits: list[float] = []
    write_late: list[float] = []
    write_waits: list[float] = []
    original = parent_routes.stale_reason

    try:

        async def scenario() -> tuple[Any, list[float], float]:
            loop = asyncio.get_running_loop()
            first = await make_plan(accepted_graph(state), PLAN_DATE, state)
            await settled_down(state)
            assert first.record is not None

            def held_first(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                holds.append(FileHeld(path, HOLD_SECONDS, kind="EXCLUSIVE"))
                a_callback_due(loop, stale_late)
                began = time.monotonic()
                try:
                    return original(*args, **kwargs)
                finally:
                    stale_waits.append(time.monotonic() - began)

            monkeypatch.setattr(parent_routes, "stale_reason", held_first)
            writes = state.drafts.record_decision

            def writer_held_first(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                holds.append(FileHeld(path, HOLD_SECONDS))
                a_callback_due(loop, write_late)
                began = time.monotonic()
                try:
                    return writes(*args, **kwargs)
                finally:
                    write_waits.append(time.monotonic() - began)

            state.drafts.record_decision = writer_held_first  # type: ignore[method-assign]
            read_late: list[float] = []
            holds.append(FileHeld(path, HOLD_SECONDS, kind="EXCLUSIVE"))
            a_callback_due(loop, read_late)
            began = time.monotonic()
            decided = await decide_draft(
                state,
                lambda: accepted_graph(state),
                first.record.draft_id,
                DecisionRequest(approved=True, reason=None),
            )
            return decided, read_late, time.monotonic() - began

        decided, read_late, decided_in = asyncio.run(scenario())
    finally:
        for hold in holds:
            hold.timer.join(5)
        state.close()

    assert decided.decision == "approved"
    assert len(holds) == 3
    assert ran_on_time(read_late)
    assert ran_on_time(stale_late)
    assert ran_on_time(write_late)
    assert stale_waits[0] >= HOLD_SECONDS - 0.1
    assert write_waits[0] >= HOLD_SECONDS - 0.1
    assert decided_in >= 3 * HOLD_SECONDS - 0.1


def test_the_sweep_ends_an_expired_run_behind_a_held_writer_off_the_event_loop(
    tmp_path: pathlib.Path,
) -> None:
    """The sweep's ending of a run past its deadline waits for another connection's writer
    on a worker thread: the loop keeps its timers, and the run ends timed out once the
    writer is let go."""
    state = application(tmp_path)
    path = files_in(tmp_path)["BLOSSOM_DATABASE_PATH"]
    run_id = "plan:2026-08-19:expired"
    holds: list[FileHeld] = []
    try:
        assert (
            state.drafts.admit_run(
                run_id, plan_date=PLAN_DATE, deadline_mono=state.monotonic() - 1.0
            )
            is None
        )

        async def scenario() -> tuple[Any, list[float], float]:
            loop = asyncio.get_running_loop()
            late: list[float] = []
            holds.append(FileHeld(path, HOLD_SECONDS))
            a_callback_due(loop, late)
            began = time.monotonic()
            swept = await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
            return swept, late, time.monotonic() - began

        swept, late, swept_in = asyncio.run(scenario())
        ended = run_of(state, run_id)
    finally:
        for hold in holds:
            hold.timer.join(5)
        state.close()

    assert swept.ended == (run_id,)
    assert ran_on_time(late)
    assert swept_in >= HOLD_SECONDS - 0.1
    assert ended is not None
    assert (ended.status, ended.reason) == ("ended", "timed_out")


def test_the_scheduled_sweep_waits_for_a_held_file_off_the_event_loop(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduled sweep's deletes of aged rows wait for another connection's hold on the
    household's file on a worker thread: the loop keeps its timers, and the sweep ends once
    the file is let go."""
    state = application(tmp_path)
    path = files_in(tmp_path)["BLOSSOM_DATABASE_PATH"]
    holds: list[FileHeld] = []
    late: list[float] = []
    original = sweep_saved_state

    async def then_held(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        swept = await original(*args, **kwargs)
        holds.append(FileHeld(path, HOLD_SECONDS))
        a_callback_due(asyncio.get_running_loop(), late)
        return swept

    monkeypatch.setattr("blossom.dependencies.sweep_saved_state", then_held)
    try:

        async def scenario() -> float:
            began = time.monotonic()
            await sweep_aged(state)
            return time.monotonic() - began

        swept_in = asyncio.run(scenario())
    finally:
        for hold in holds:
            hold.timer.join(5)
        state.close()

    assert len(holds) == 1
    assert swept_in >= HOLD_SECONDS - 0.1
    assert ran_on_time(late)


def holding_then_waiting(path: str, holds: list[FileHeld], late: list[float]) -> Ask[DailyPlan]:
    """A planner that holds the file's writer from another connection, asks the loop for a
    callback, and then never answers."""

    async def ask(messages: Sequence[BaseMessage]) -> ModelAnswer[DailyPlan]:
        holds.append(FileHeld(path, LONG_HOLD_SECONDS))
        a_callback_due(asyncio.get_running_loop(), late)
        await asyncio.Event().wait()
        msg = "unreachable"
        raise AssertionError(msg)

    return ask


def test_a_run_out_of_time_answers_while_its_ending_waits_behind_a_held_writer(
    tmp_path: pathlib.Path,
) -> None:
    """A run whose time runs out while another connection holds the file's writer is
    answered timed out at its deadline: its ending and its timing wait on worker threads,
    the loop keeps its timers, and both land once the writer is let go."""
    state = application(tmp_path)
    path = files_in(tmp_path)["BLOSSOM_DATABASE_PATH"]
    holds: list[FileHeld] = []
    waiting_late: list[float] = []
    budget_seconds = 0.4
    try:

        async def scenario() -> tuple[Any, float, list[float], bool, RunState | None]:
            graph = plan_graph_for(
                state, planner=holding_then_waiting(path, holds, waiting_late), critic=critic()
            )
            began = time.monotonic()
            made = await make_plan(graph, PLAN_DATE, state, budget=RunBudget(budget_seconds))
            answered_in = time.monotonic() - began
            ending_late: list[float] = []
            a_callback_due(asyncio.get_running_loop(), ending_late)
            ending_held = bool(state.detached) and holds[0].timer.is_alive()
            await settled_down(state)
            ended = await asyncio.to_thread(run_of, state, made.view.thread_id)
            return made, answered_in, ending_late, ending_held, ended

        made, answered_in, ending_late, ending_held, ended = asyncio.run(scenario())
        timing = timing_of(tmp_path, made.view.thread_id)
    finally:
        for hold in holds:
            hold.timer.join(5)
        state.close()

    assert made.view.outcome == "timed_out"
    assert len(holds) == 1
    assert answered_in < budget_seconds + TIMER_SLACK_SECONDS
    assert ending_held
    assert ran_on_time(waiting_late)
    assert ran_on_time(ending_late)
    assert ended is not None
    assert (ended.status, ended.reason) == ("ended", "timed_out")
    assert timing.response_seconds is not None


@pytest.mark.parametrize("held_by", ["a page's reading", "another connection's lock"])
def test_the_check_for_work_waits_for_the_held_record_off_the_event_loop(
    tmp_path: pathlib.Path, held_by: str
) -> None:
    """The check that an evening has work to plan waits for a held record on a worker
    thread: the loop keeps its timers, the check passes once the record is let go, and one
    with less time than the hold can't start, inside its time."""
    state = application(tmp_path)
    path = files_in(tmp_path)["BLOSSOM_DATABASE_PATH"]
    readers: list[threading.Thread] = []
    holds: list[FileHeld] = []

    def hold(seconds: float) -> None:
        if held_by == "a page's reading":
            readers.append(read_held(state, seconds))
        else:
            holds.append(FileHeld(path, seconds, kind="EXCLUSIVE"))

    try:

        async def scenario() -> tuple[float, list[float], float, list[float]]:
            loop = asyncio.get_running_loop()
            passed_late: list[float] = []
            hold(HOLD_SECONDS)
            a_callback_due(loop, passed_late)
            began = time.monotonic()
            await require_work(state, PLAN_DATE, RunBudget(10))
            passed_in = time.monotonic() - began
            await asyncio.to_thread(time.sleep, 0.05)
            refused_late: list[float] = []
            hold(LONG_HOLD_SECONDS)
            a_callback_due(loop, refused_late)
            began = time.monotonic()
            with pytest.raises(CouldNotStart):
                await require_work(state, PLAN_DATE, RunBudget(0.3))
            return passed_in, passed_late, time.monotonic() - began, refused_late

        passed_in, passed_late, refused_in, refused_late = asyncio.run(scenario())
    finally:
        for thread in readers:
            thread.join(5)
        for held in holds:
            held.timer.join(5)
        state.close()

    assert passed_in >= HOLD_SECONDS - 0.1
    assert ran_on_time(passed_late)
    assert refused_in < 0.3 + TIMER_SLACK_SECONDS
    assert ran_on_time(refused_late)


def test_a_settle_waits_for_a_page_reading_across_its_commit_off_the_event_loop(
    tmp_path: pathlib.Path,
) -> None:
    """A page's reading held across the settle's commit makes the commit wait on a worker
    thread: the loop keeps its timers while it waits, and the plan is published once the
    reading ends."""
    state = application(tmp_path)
    held: list[threading.Thread] = []
    late: list[float] = []
    settle_waits: list[float] = []
    try:

        async def scenario() -> Any:  # noqa: ANN401
            loop = asyncio.get_running_loop()

            def reading_first(original: Callable[..., Any]) -> Callable[..., Any]:
                def settle(*args: object, **kwargs: object) -> object:
                    held.append(read_held(state, HOLD_SECONDS))
                    a_callback_due(loop, late)
                    began = time.monotonic()
                    try:
                        return original(*args, **kwargs)
                    finally:
                        settle_waits.append(time.monotonic() - began)

                return settle

            settling_through(state, reading_first)
            return await make_plan(accepted_graph(state), PLAN_DATE, state, budget=RunBudget(5))

        made = asyncio.run(scenario())
        status = run_of(state, made.view.thread_id)
    finally:
        for thread in held:
            thread.join(5)
        state.close()

    assert len(held) == 1
    assert made.record is not None
    assert status is not None
    assert status.status == "published"
    assert settle_waits[0] >= HOLD_SECONDS - 0.1
    assert ran_on_time(late)


def test_a_page_read_after_a_short_settle_waits_the_stores_full_wait(
    tmp_path: pathlib.Path,
) -> None:
    """A page's read of the plans right after a settle that had a fifth of a second left
    still waits the store's full wait for a held file, on a worker thread with the loop
    keeping its timers, and reads the plan the settle published."""
    state = application(tmp_path)
    path = files_in(tmp_path)["BLOSSOM_DATABASE_PATH"]
    run_id = "plan:2026-08-19:short"
    holds: list[FileHeld] = []
    try:
        assert (
            state.drafts.admit_run(
                run_id, plan_date=PLAN_DATE, deadline_mono=state.monotonic() + 0.3
            )
            is None
        )
        state.drafts.record_waiting(
            Draft(
                draft_id=f"draft:{run_id}",
                body="Plan",
                created_at=datetime(2026, 8, 19, 22, 0, tzinfo=UTC),
            ),
            thread_id=run_id,
            plan_date=PLAN_DATE,
            outcome="accepted",
        )
        settled = state.drafts.settle_run(run_id, wait=0.2, grace=0.0)
        (left_after,) = state.drafts._connection.execute("PRAGMA busy_timeout").fetchone()

        async def scenario() -> tuple[Any, list[float], float]:
            loop = asyncio.get_running_loop()
            late: list[float] = []
            holds.append(FileHeld(path, HOLD_SECONDS, kind="EXCLUSIVE"))
            a_callback_due(loop, late)
            began = time.monotonic()
            latest = await asyncio.to_thread(state.drafts.latest_for, PLAN_DATE)
            return latest, late, time.monotonic() - began

        latest, late, read_in = asyncio.run(scenario())
    finally:
        for hold in holds:
            hold.timer.join(5)
        state.close()

    assert settled.run.status == "published"
    assert left_after / 1000 < HOLD_SECONDS / 2
    assert latest is not None
    assert latest.draft_id == f"draft:{run_id}"
    assert read_in >= HOLD_SECONDS - 0.1
    assert ran_on_time(late)
