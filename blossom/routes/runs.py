"""Running the plan graph for one evening, from either page.

She plans from her page; a parent may start an evening's plan for her from
theirs. Both doors lead here, so there is one way a run starts, one way it is
built, and one place its saved state is cleared when it ends without a pause.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, HTTPException, status

from blossom.agent.graph import CompiledPlanGraph, PlanState, plan_graph_for
from blossom.agent.retention import clear_thread
from blossom.agent.runs import DURABILITY, draft_id_for, run_config
from blossom.anthropic_client import MISSING_KEY, ModelUnavailable, model_configured
from blossom.dependencies import ApplicationState, get_application_state
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
    file. A thread that cannot be cleared now is left to the sweep; the failure
    that ended the run is the one the caller sees, not the failure to tidy.
    """
    state.drafts.withdraw(draft_id_for(thread_id))
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

    The thread is in ``state.in_flight`` from start to pause or end, so the
    scheduled sweep, which takes back drafts whose run died between saving
    them and pausing, does not mistake a run still between the two for one.

    A run that stops before the gate has nothing left to resume, and its
    record is already in the drafts file, so its saved state is cleared here;
    so is the state of a run that raised, since a raise never pauses at the
    gate and the first node had already been saved. A run paused at the gate
    keeps its state until a review or its expiry.

    A run that pauses with a draft has taken the place of any draft still
    waiting for the same evening; the table closed those as superseded when
    the draft was saved, and their threads are cleared here, since nothing can
    resume them. The threads of runs still in flight are left alone, this
    run's own among them: a run in flight clears what it displaced when it
    pauses, and gives it back if it fails, so a thread it displaced must
    survive until then, and a run whose own draft was displaced by one still in
    flight keeps its thread for the same reason. What such a run leaves behind
    goes when the displacing run pauses, or at the next sweep.

    A run can fail between saving its draft and pausing with it, since the
    save is a transaction of its own and the checkpoint after it is another.
    The draft is then taken back by the store's rule: its row goes, the run is
    kept as interrupted, and the draft it displaced waits again or is handed on
    to a draft that had already displaced the failed one. So the plan on her
    page and the parent's queue are what they were before the run, and the
    run itself appears among the runs that ended without a plan.
    """
    thread_id = thread_for(plan_date)
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
        for superseded in state.drafts.superseded_for(plan_date):
            if superseded.thread_id not in state.in_flight:
                await tidy_thread(superseded.thread_id, state)
        return view
    finally:
        state.in_flight.discard(thread_id)
