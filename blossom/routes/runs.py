"""Running the plan graph for one evening, from either page.

She plans from her page; a parent may start an evening's plan for her from
theirs. Both doors lead here, so there is one way a run starts, one way it is
built, and one place its saved state is cleared when it ends without a pause.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, HTTPException, status
from langgraph.checkpoint.base import BaseCheckpointSaver

from blossom.agent.graph import CompiledPlanGraph, PlanState, plan_graph_for
from blossom.agent.retention import clear_thread
from blossom.agent.runs import DURABILITY, run_config
from blossom.agent.trace import LocalRunTracer
from blossom.anthropic_client import MISSING_KEY, ModelUnavailable, model_configured
from blossom.dependencies import ApplicationState, get_application_state
from blossom.views import PlanRunView

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


async def run_plan(
    graph: CompiledPlanGraph,
    plan_date: date,
    tracer: LocalRunTracer,
    checkpointer: BaseCheckpointSaver[Any],
) -> PlanRunView:
    """Run the graph for one evening on a fresh thread, to the gate or to the reason it stopped.

    A run that stops before the gate has nothing left to resume, and its
    record is already in the drafts file, so its saved state is cleared here;
    so is the state of a run that raised, since a raise never pauses at the
    gate and the first node had already been saved. A run paused at the gate
    keeps its state until a decision or its expiry.
    """
    thread_id = thread_for(plan_date)
    try:
        result = await graph.ainvoke(
            PlanState(plan_date=plan_date, rounds=0),
            config=run_config(thread_id, callbacks=[tracer]),
            durability=DURABILITY,
        )
    except ModelUnavailable as error:
        await clear_thread(checkpointer, thread_id)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except Exception:
        await clear_thread(checkpointer, thread_id)
        raise
    view = run_view(thread_id, plan_date, dict(result))
    if not view.waiting:
        await clear_thread(checkpointer, thread_id)
    return view
