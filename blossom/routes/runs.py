# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Running the plan graph for one evening, from either page.

She plans from her page; a parent may start an evening's plan for her from
theirs. Both doors lead here, so there is one way a run starts, one way it is
built, and one place its saved state is cleared when it ends without a pause.

A run has ``RUN_DEADLINE_SECONDS`` in all. One that runs out of time, or meets a
service that fails, ends with nothing published, its record kept with the steps
it took, and the page says which. Every run's time is kept with its record.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Any, Final
from uuid import uuid4

from fastapi import Depends, HTTPException, status

from blossom.agent.graph import CompiledPlanGraph, PlanState, plan_graph_for
from blossom.agent.retention import clear_thread, finish_held_reviews
from blossom.agent.runs import (
    DURABILITY,
    RUN_DEADLINE_SECONDS,
    RunBudget,
    RunTimedOut,
    draft_id_for,
    run_config,
)
from blossom.agent.steps import DATE_PROBLEM, StepRecord, describe_failure
from blossom.agent.steps import NOTHING_TO_SCHEDULE as NOTHING_TO_SCHEDULE_OUTCOME
from blossom.anthropic_client import (
    MISSING_KEY,
    ModelUnavailable,
    ServiceFailed,
    model_configured,
)
from blossom.dependencies import ApplicationState, get_application_state
from blossom.noticing import read_week
from blossom.stores.drafts import INTERRUPTED, OVERTAKEN, OutOfTime
from blossom.views import PastDueView, PlanRunView

logger = logging.getLogger(__name__)

State = Annotated[ApplicationState, Depends(get_application_state)]


PlanGraphBuilder = Callable[[], CompiledPlanGraph]


@dataclass(frozen=True)
class PlanGraphs:
    """What a route needs from the graph: a way to build it, and whether it may start one.

    A dependency is resolved before its handler runs, so this hands back a
    builder rather than a graph, and the handler calls it after consulting the
    table. ``may_start`` is whether a run can be started at all, which needs a
    model; resuming a paused thread does not. ``budget`` makes each run's time
    limit as the run starts. A test substitutes the whole object, scripted
    models, permission, and clock together, over the real stores.
    """

    build: PlanGraphBuilder
    may_start: bool
    budget: Callable[[], RunBudget] = field(default=RunBudget)


def plan_graphs(state: State) -> PlanGraphs:
    """The application's graphs: built from the seam, allowed to start when there is a key."""
    return PlanGraphs(
        build=lambda: plan_graph_for(state), may_start=model_configured(state.settings)
    )


def require_model(graphs: PlanGraphs) -> None:
    """Refuse to start a run without a model, before any thread is written."""
    if not graphs.may_start:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=MISSING_KEY)


NOTHING_TO_SCHEDULE: Final = "Nothing to schedule from the work in this planning window."
"""What every planning route answers, 409, for an evening whose window holds no work
still to do: nothing has been planned, no run has been written, and no model asked."""


def require_work(state: ApplicationState, plan_date: date) -> None:
    """Refuse to start a run for an evening with nothing left to plan, before any thread is
    written and before the model is asked for.

    The window is read as the graph reads it. The graph reads it again when it
    runs, since a report of hers can land in between, and ends the same way.
    """
    if not read_week(state.project_state, state.project_state, plan_date).active():
        raise HTTPException(status.HTTP_409_CONFLICT, detail=NOTHING_TO_SCHEDULE)


def no_plan_made(outcome: str) -> str:
    """Why a run that ended without a plan is answered 409: the one outcome with a sentence
    of its own, or the outcome named."""
    if outcome == NOTHING_TO_SCHEDULE_OUTCOME:
        return NOTHING_TO_SCHEDULE
    return f"no plan was made: the run ended with {outcome}"


def refuse_an_empty_run(run: PlanRunView) -> None:
    """Refuse, 409, a run that ended at its first node with nothing left to schedule.

    The route asked whether there was work before the run, and a report of
    hers landed between that question and the run's reading. Such a run made
    no plan and asked no model, so it is answered as the guard would have
    answered, not as a plan made; the run's own record stays in the ledger.
    """
    if run.draft_id is None and run.outcome == NOTHING_TO_SCHEDULE_OUTCOME:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=NOTHING_TO_SCHEDULE)


TIMED_OUT: Final = "timed_out"
"""The outcome recorded on a run whose time ran out before it ended or paused."""

SERVICE_FAILED: Final = "service_failed"
"""The outcome recorded on a run a model request failed in, retries spent."""

INVALID_OUTPUT: Final = "invalid_output"
TIMEOUT: Final = "timeout"
SERVICE: Final = "service"
CATEGORIES: Final[dict[str, str]] = {
    "checks_failed": INVALID_OUTPUT,
    "model_truncated": INVALID_OUTPUT,
    "model_refused": INVALID_OUTPUT,
    "model_unparseable": INVALID_OUTPUT,
    TIMED_OUT: TIMEOUT,
    SERVICE_FAILED: SERVICE,
    DATE_PROBLEM: DATE_PROBLEM,
    INTERRUPTED: INTERRUPTED,
}
"""How each way of ending without a plan is told apart: the service at fault, an answer
that couldn't be used, time running out, or a date on record no plan can keep to; a run
that failed on the way for a reason of its own is interrupted. A run that made a plan, had
nothing to plan, or was overtaken by a newer one has no category."""


def failure_category(outcome: str) -> str | None:
    """The category of a run that ended with ``outcome``, or ``None`` when it did not fail."""
    return CATEGORIES.get(outcome)


def due_on(day: date) -> str:
    """``August 18``: a day as her page says it."""
    return f"{day:%B} {day.day}"


def ended_without_a_plan(
    outcome: str, *, parent: bool, past_due: Sequence[PastDueView] = ()
) -> str:
    """The same, as her page says it: what went wrong in plain words, that her updates are
    kept, and, to a parent reading her page, where the run's record is. The run's own name
    for how it ended is never shown, and work is named only when the record shows its date
    has passed."""
    if outcome == NOTHING_TO_SCHEDULE_OUTCOME:
        return NOTHING_TO_SCHEDULE
    saved = "Her homework updates are saved." if parent else "Your homework updates are saved."
    category = failure_category(outcome)
    if category == DATE_PROBLEM and past_due:
        named = [f"{work.title} ({work.course}, due {due_on(work.due_date)})" for work in past_due]
        listed = named[0] if len(named) == 1 else ", ".join(named[:-1]) + f" and {named[-1]}"
        has, it = ("has a due date", "it") if len(named) == 1 else ("have due dates", "them")
        what = (
            f"Blossom can't make today's plan: {listed} {has} that already passed, so no "
            f"plan can finish {it} on time."
        )
    elif category == TIMEOUT:
        what = "Planning took too long, so Blossom stopped."
    elif category == SERVICE:
        what = "Blossom couldn't reach the planning service this time."
    else:
        what = "Blossom couldn't finish a reliable plan this time."
    then = "Family review shows what happened." if parent else ""
    return " ".join(part for part in (what, saved, then) if part)


Graphs = Annotated[PlanGraphs, Depends(plan_graphs)]


def evening_prefix(plan_date: date) -> str:
    """How every thread for one evening begins."""
    return f"plan:{plan_date.isoformat()}:"


def has_a_run_in_flight(state: ApplicationState, plan_date: date) -> bool:
    """Whether a run for the evening is in flight in this process."""
    return any(thread.startswith(evening_prefix(plan_date)) for thread in state.in_flight)


def thread_for(plan_date: date) -> str:
    """A new thread for one evening. The date is for a person reading the table."""
    return f"{evening_prefix(plan_date)}{uuid4().hex[:8]}"


ALREADY_PLANNING: Final = "A plan for this evening is already being made."
"""What a press answers, 409, while a run for the same evening is in flight: no run is
started and no model is asked."""


class AlreadyPlanning(HTTPException):
    """A run for the evening is in flight in this process, so another is not started."""

    def __init__(self) -> None:
        super().__init__(status.HTTP_409_CONFLICT, detail=ALREADY_PLANNING)


def already_planning(*, parent: bool) -> str:
    """The same, as her page says it, to her or to a parent reading it."""
    saved = "Her homework updates are saved." if parent else "Your homework updates are saved."
    return f"A plan for today is already being made. {saved}"


def run_view(thread_id: str, plan_date: date, result: dict[str, Any]) -> PlanRunView:
    """What a finished or paused run looks like to the parent."""
    draft = result.get("draft")
    read = {item.assignment_id: item for item in result.get("assignments", [])}
    return PlanRunView(
        thread_id=thread_id,
        plan_date=plan_date,
        outcome=result["outcome"],
        draft_id=None if draft is None else draft.draft_id,
        waiting="__interrupt__" in result,
        steps=list(result.get("steps", [])),
        past_due=[
            PastDueView(
                assignment_id=name,
                title=read[name].title,
                course=read[name].course,
                due_date=day,
            )
            for name, day in result.get("past_due", {}).items()
            if name in read
        ],
    )


async def abandon(thread_id: str, state: ApplicationState) -> None:
    """Take back what a failed run left behind: its draft first, then its thread.

    The draft comes first because the pages read the drafts file, and because
    the saved-state store is the likelier of the two to be what failed: a
    checkpoint that could not be written is followed by a delete on the same
    file. Each is attempted whatever became of the other: a draft that cannot
    be taken back now is logged and its thread cleared all the same, so the
    sweep finds a draft with no thread and takes it back rather than a paused
    thread it would publish; a thread that cannot be cleared now is left to the
    sweep. The failure that ended the run is the one the caller sees, not the
    failure to tidy.
    """
    try:
        state.drafts.withdraw(draft_id_for(thread_id))
    except Exception:
        logger.exception(
            "the draft of the failed run %s not taken back; the sweep takes it", thread_id
        )
    await tidy_thread(thread_id, state)


async def tidy_thread(thread_id: str, state: ApplicationState) -> None:
    """Clear a thread nothing will resume, or log why not and leave it to the sweep.

    Tidying is never what a caller hears about: a run that paused or ended has
    its outcome, and the sweep clears every thread no waiting draft refers to
    within the hour.
    """
    try:
        await clear_thread(state.checkpointer, thread_id)
    except Exception:
        logger.exception("saved state of thread %s not cleared; the sweep clears it", thread_id)


EXPECT_AN_ANSWER: Final = "an answer inside the run's time"
WAITED_FOR: Final = {"plan": "plan", "critique": "verdict"}


async def steps_so_far(graph: CompiledPlanGraph, thread_id: str) -> list[StepRecord]:
    """The steps a run saved before it stopped, read from its thread; none when unreadable."""
    try:
        snapshot = await graph.aget_state(run_config(thread_id))
    except Exception:
        logger.exception("the steps of run %s could not be read", thread_id)
        return []
    return list(snapshot.values.get("steps", []))


async def ended_on_the_way(
    graph: CompiledPlanGraph,
    thread_id: str,
    plan_date: date,
    state: ApplicationState,
    budget: RunBudget,
    outcome: str,
) -> PlanRunView:
    """Keep the record of a run that ran out of time or met a failing service, and end it.

    The steps it saved are kept, with one more for the request it was waiting on,
    and the run is recorded under ``outcome``. Whatever it left behind is taken
    back first, so nothing it made reaches a page: the plan already there, and
    her updates, are as they were.
    """
    steps = await steps_so_far(graph, thread_id)
    node, round_number = budget.waiting_on or ("time_limit", 0)
    budget.lap(node, round_number)
    found = (
        describe_failure(outcome, WAITED_FOR[node])
        if node in WAITED_FOR
        else f"The run's {RUN_DEADLINE_SECONDS:g} seconds ran out."
    )
    steps.append(
        StepRecord(
            node=node,
            round=round_number,
            expected=EXPECT_AN_ANSWER,
            found=found,
            recorded_at=state.clock.now(),
        )
    )
    await abandon(thread_id, state)
    try:
        state.drafts.record_run(
            thread_id=thread_id, plan_date=plan_date, outcome=outcome, steps=steps
        )
    except Exception:
        logger.exception("the record of run %s could not be kept", thread_id)
    return PlanRunView(
        thread_id=thread_id,
        plan_date=plan_date,
        outcome=outcome,
        draft_id=None,
        waiting=False,
        steps=steps,
    )


async def hold_in_time(lock: asyncio.Lock, budget: RunBudget) -> bool:
    """True once ``lock`` is held with time left in the run; False, holding nothing, otherwise.

    The decision lock is held across saved-state reads by reviews and the sweep, so a
    run waiting for it is still spending its time, and one that gets it too late goes
    no further.
    """
    try:
        async with asyncio.timeout(budget.remaining()):
            await lock.acquire()
    except TimeoutError:
        return False
    if budget.remaining() <= 0:
        lock.release()
        return False
    return True


async def read_in_time[T](reading: Awaitable[T], budget: RunBudget) -> T:
    """What ``reading`` returns, with time left in the run, or ``RunTimedOut``.

    Only the run's own limit is a timeout here: a ``TimeoutError`` the reading raises
    itself is the failure it is.
    """
    limit = asyncio.timeout(budget.remaining())
    try:
        async with limit:
            read = await reading
    except TimeoutError as error:
        if limit.expired():
            raise RunTimedOut from error
        raise
    if budget.remaining() <= 0:
        raise RunTimedOut
    return read


def keep_timing(
    state: ApplicationState, thread_id: str, budget: RunBudget, outcome: str | None
) -> None:
    """Keep the run's time with its record. Never a reason for the run to fail."""
    try:
        state.drafts.record_timing(
            thread_id, budget.timing(None if outcome is None else failure_category(outcome))
        )
    except Exception:
        logger.exception("the time of run %s could not be kept", thread_id)


async def run_plan(
    graph: CompiledPlanGraph,
    plan_date: date,
    state: ApplicationState,
    *,
    budget: RunBudget | None = None,
) -> PlanRunView:
    """Run the graph for one evening on a fresh thread, to the gate or to the reason it stopped.

    The thread is in ``state.in_flight`` from start to pause or end, joined
    under the decision lock, so the scheduled sweep, which takes back drafts
    whose run died between saving them and pausing, does not mistake a run
    still between the two for one and never counts threads while a run is
    joining.

    A run that stops before the gate has nothing left to resume, and its
    record is already in the drafts file, so its saved state is cleared here;
    so is the state of a run that raised, since a raise never pauses at the
    gate and the first node had already been saved. A run paused at the gate
    keeps its state until a review or its expiry.

    A run that pauses with a draft publishes it, under the decision lock: any
    review a waiting draft's thread holds that the table never got is recorded
    first, then the draft reaches the pages, takes the place of any published
    draft still waiting for the evening, and the threads of those are cleared,
    since no review can reach them. The lock means a review in progress lands or is
    refused before its thread goes, and nothing the pages show is ever a draft
    whose run might still fail. A publication that fails is a failed run: the
    draft is taken back and the thread cleared before the failure reaches the
    page, so what the page then says, that nothing changed, stays true.

    A run can fail between saving its draft and pausing with it, since the
    save is a transaction of its own and the checkpoint after it is another.
    The draft, never published, is taken back: its row goes and the run is
    kept as interrupted. It displaced nothing, so the plan on her page and the
    parent's queue are what they were before the run, and the run itself
    appears among the runs that ended without a plan.

    One run per evening is in flight at a time: a press for an evening that
    has one is refused with ``AlreadyPlanning`` before any thread is written or
    model asked. The run has ``budget``, made as it starts, and no more: each model request
    gets what is left of it, and the whole run is cut off when it is spent, waits
    for the decision lock included, to start and to publish, and the reads of
    held reviews before publishing. A
    run cut off, or whose service failed, publishes nothing and is recorded,
    with its steps, as timed out or as a service failure. A late result never
    replaces a newer plan either: the evening's last publication is noted as
    the run joins the runs in flight, and a run that pauses after another
    plan for the evening was published is taken back as overtaken. Every run's
    time is kept with its record as it ends.
    """
    thread_id = thread_for(plan_date)
    budget = RunBudget() if budget is None else budget
    # The outcome the run's time is kept under; a run that raises is interrupted.
    outcome: str | None = INTERRUPTED
    if not await hold_in_time(state.decision_lock, budget):
        # Out of time before it could start: a press while the evening has a run in
        # flight is still refused, and any other is kept as timed out.
        if has_a_run_in_flight(state, plan_date):
            raise AlreadyPlanning
        view = await ended_on_the_way(graph, thread_id, plan_date, state, budget, TIMED_OUT)
        keep_timing(state, thread_id, budget, TIMED_OUT)
        return view
    try:
        # Checked and joined under one hold of the lock, so two presses for one evening
        # cannot both find it free. The read comes first, so one that fails leaves
        # nothing in flight to refuse the next press.
        if has_a_run_in_flight(state, plan_date):
            raise AlreadyPlanning
        newest = state.drafts.newest_published(plan_date)
        state.in_flight.add(thread_id)
    finally:
        state.decision_lock.release()
    try:
        limit = asyncio.timeout(budget.remaining())
        try:
            async with limit:
                result = await graph.ainvoke(
                    PlanState(plan_date=plan_date, rounds=0),
                    config=run_config(thread_id, callbacks=[state.tracer]),
                    durability=DURABILITY,
                    context=budget,
                )
        except ModelUnavailable as error:
            await abandon(thread_id, state)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
        except TimeoutError as error:
            if not limit.expired() and not isinstance(error, RunTimedOut):
                # A store's or a request's own TimeoutError is a failure like any other,
                # not the run's time running out.
                await abandon(thread_id, state)
                raise
            outcome = TIMED_OUT
            return await ended_on_the_way(graph, thread_id, plan_date, state, budget, outcome)
        except ServiceFailed:
            outcome = SERVICE_FAILED
            return await ended_on_the_way(graph, thread_id, plan_date, state, budget, outcome)
        except Exception:
            await abandon(thread_id, state)
            raise
        view = run_view(thread_id, plan_date, dict(result))
        outcome = view.outcome
        if not view.waiting:
            await tidy_thread(thread_id, state)
            return view
        if not await hold_in_time(state.decision_lock, budget):
            # The time ran out before the draft could be published, so it never is.
            outcome = TIMED_OUT
            return await ended_on_the_way(graph, thread_id, plan_date, state, budget, outcome)
        try:
            try:
                if state.drafts.newest_published(plan_date) != newest:
                    # A plan for the evening was published while this one was
                    # being made, from another press or the other page. That
                    # plan stays; this one is taken back and never shown.
                    state.drafts.withdraw(draft_id_for(thread_id), outcome=OVERTAKEN)
                    outcome = OVERTAKEN
                    view = view.model_copy(
                        update={"outcome": OVERTAKEN, "draft_id": None, "waiting": False}
                    )
                else:
                    # A review a waiting draft's thread holds, that the table
                    # never got, is recorded before this plan takes the draft's
                    # place, so the review is never superseded away with the thread.
                    # Reading the threads spends the run's time too, and the plan
                    # is published only with time left.
                    finished = await read_in_time(
                        finish_held_reviews(
                            state.checkpointer,
                            state.drafts,
                            plan_date=plan_date,
                            in_flight=state.in_flight,
                        ),
                        budget,
                    )
                    displaced = state.drafts.publish(
                        draft_id_for(thread_id), within=budget.remaining
                    )
                    for thread in [
                        *(thread for _, thread in finished),
                        *(d.thread_id for d in displaced),
                    ]:
                        await tidy_thread(thread, state)
            finally:
                state.decision_lock.release()
        except (RunTimedOut, OutOfTime):
            # The time ran out before the draft was published, so it never is. The
            # reviews already recorded stand, and the sweep clears their threads.
            outcome = TIMED_OUT
            return await ended_on_the_way(graph, thread_id, plan_date, state, budget, outcome)
        except Exception:
            # The run paused but its draft could not be published. Left as it
            # is, the sweep would publish it later, after the page had said
            # the request changed nothing; so the run is treated as failed
            # here and now, its draft taken back and its thread cleared.
            outcome = INTERRUPTED
            await abandon(thread_id, state)
            raise
        if outcome == OVERTAKEN:
            await tidy_thread(thread_id, state)
        return view
    finally:
        state.in_flight.discard(thread_id)
        keep_timing(state, thread_id, budget, outcome)
