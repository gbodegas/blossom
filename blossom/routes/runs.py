"""Running the plan graph for one evening, from either page.

She plans from her page; a parent may start an evening's plan for her from
theirs. Both doors lead here, so there is one way a run starts, one way it is
built, and one place its saved state is cleared when it ends without a pause.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Final
from uuid import uuid4

from fastapi import Depends, HTTPException, status

from blossom.agent.graph import CompiledPlanGraph, PlanState, plan_graph_for
from blossom.agent.retention import clear_thread, finish_held_reviews
from blossom.agent.runs import DURABILITY, draft_id_for, run_config
from blossom.agent.steps import NOTHING_TO_SCHEDULE as NOTHING_TO_SCHEDULE_OUTCOME
from blossom.anthropic_client import MISSING_KEY, ModelUnavailable, model_configured
from blossom.dependencies import ApplicationState, get_application_state
from blossom.noticing import read_week
from blossom.views import PlanRunView

logger = logging.getLogger(__name__)

State = Annotated[ApplicationState, Depends(get_application_state)]


PlanGraphBuilder = Callable[[], CompiledPlanGraph]


@dataclass(frozen=True)
class PlanGraphs:
    """What a route needs from the graph: a way to build it, and whether it may start one.

    A dependency is resolved before its handler runs, so this hands back a
    builder rather than a graph, and the handler calls it after consulting the
    table. ``may_start`` is whether a run can be started at all, which needs a
    model; resuming a paused thread does not. A test substitutes the whole
    object, scripted models and permission together, over the real stores.
    """

    build: PlanGraphBuilder
    may_start: bool


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


def ended_without_a_plan(outcome: str) -> str:
    """The same, as her page says it."""
    if outcome == NOTHING_TO_SCHEDULE_OUTCOME:
        return NOTHING_TO_SCHEDULE
    return f"No plan was made this time: the run ended with {outcome}."


Graphs = Annotated[PlanGraphs, Depends(plan_graphs)]


def thread_for(plan_date: date) -> str:
    """A new thread for one evening. The date is for a person reading the table."""
    return f"plan:{plan_date.isoformat()}:{uuid4().hex[:8]}"


def run_view(thread_id: str, plan_date: date, result: dict[str, Any]) -> PlanRunView:
    """What a finished or paused run looks like to the parent."""
    draft = result.get("draft")
    return PlanRunView(
        thread_id=thread_id,
        plan_date=plan_date,
        outcome=result["outcome"],
        draft_id=None if draft is None else draft.draft_id,
        waiting="__interrupt__" in result,
        steps=list(result.get("steps", [])),
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


async def run_plan(
    graph: CompiledPlanGraph, plan_date: date, state: ApplicationState
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
    """
    thread_id = thread_for(plan_date)
    async with state.decision_lock:
        state.in_flight.add(thread_id)
    try:
        try:
            result = await graph.ainvoke(
                PlanState(plan_date=plan_date, rounds=0),
                config=run_config(thread_id, callbacks=[state.tracer]),
                durability=DURABILITY,
            )
        except ModelUnavailable as error:
            await abandon(thread_id, state)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
        except Exception:
            await abandon(thread_id, state)
            raise
        view = run_view(thread_id, plan_date, dict(result))
        if not view.waiting:
            await tidy_thread(thread_id, state)
            return view
        try:
            async with state.decision_lock:
                # A review a waiting draft's thread holds, that the table never
                # got, is recorded before this plan takes the draft's place, so
                # the review is never superseded away with the thread.
                finished = await finish_held_reviews(
                    state.checkpointer, state.drafts, plan_date=plan_date, in_flight=state.in_flight
                )
                displaced = state.drafts.publish(draft_id_for(thread_id))
                for thread in [
                    *(thread for _, thread in finished),
                    *(d.thread_id for d in displaced),
                ]:
                    await tidy_thread(thread, state)
        except Exception:
            # The run paused but its draft could not be published. Left as it
            # is, the sweep would publish it later, after the page had said
            # the request changed nothing; so the run is treated as failed
            # here and now, its draft taken back and its thread cleared.
            await abandon(thread_id, state)
            raise
        return view
    finally:
        state.in_flight.discard(thread_id)
