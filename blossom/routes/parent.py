"""Parent routes: start a plan, see what waits at the gate, and decide.

A parent is a collaborator who sets goals, corrects information and reviews
drafts, so these routes expose a queue and a checkpoint rather than a live
feed. A live feed would turn collaboration into monitoring.

Three routes drive the plan graph. ``POST /parent/plans`` starts a run for one
evening and reports how it ended: at the gate with a draft, or before it with
a reason. ``GET /parent/approvals`` lists the drafts waiting for a decision,
read from the drafts table rather than from graph state, because the table is
the record across threads and needs no model to read. ``POST
/parent/approvals/{draft_id}`` resumes the paused run with the decision; the
graph's gate node records it in saved state and the node after the gate
records it in the table.

Building the graph needs the model seam, which needs a key. Reading the queue
does not, so a parent can always see what is waiting; only starting or
deciding a run answers 503 without one, and it says so. The graph is built
inside the handler, after the table has been consulted, because a dependency
is resolved before a handler runs: a draft that does not exist is a 404 with
or without a key, and one already decided is a 409.

Two decisions about one draft cannot both land. The handler holds the
application's decision lock from the table check through the resume, and the
table refuses a second, different decision even if a request arrives from
another process.

Not yet implemented: when the system notifies a parent that a deadline is at
risk, the parent needs to be able to see that the notification happened.
Without that, the visibility policy is stated but not observable.
"""

from collections.abc import Callable
from datetime import date
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, StrictBool

from blossom.agent.graph import CompiledPlanGraph, PlanState, plan_graph_for
from blossom.agent.runs import DURABILITY, StaleGraphVersion, ensure_current_version, run_config
from blossom.anthropic_client import ModelUnavailable
from blossom.dependencies import ApplicationState, get_application_state
from blossom.stores.drafts import AlreadyDecided
from blossom.views import (
    ApprovalQueueView,
    ApprovalView,
    DecisionView,
    ParentCheckpointAssignmentView,
    ParentCheckpointView,
    PlanRunView,
)

router = APIRouter(prefix="/parent", tags=["parent"])

State = Annotated[ApplicationState, Depends(get_application_state)]


PlanGraphBuilder = Callable[[], CompiledPlanGraph]


def plan_graph_builder(state: State) -> PlanGraphBuilder:
    """A way to build the graph later, rather than the graph itself.

    A dependency is resolved before its handler runs. One that built the graph
    would answer 503 for a missing key before the handler could say 404 or 409
    about the draft, so the dependency hands back a builder and the handler
    calls it only when it is about to run a graph. A test substitutes this
    dependency to supply scripted models over the real stores.
    """

    def build() -> CompiledPlanGraph:
        try:
            return plan_graph_for(state)
        except ModelUnavailable as error:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error

    return build


Builder = Annotated[PlanGraphBuilder, Depends(plan_graph_builder)]


class PlanRequest(BaseModel):
    """Which evening to plan. Defaults to today in the household's zone."""

    model_config = ConfigDict(extra="forbid")

    plan_date: date | None = None


class DecisionRequest(BaseModel):
    """What the parent decided.

    ``approved`` is strict: a JSON ``true`` or ``false`` and nothing else. The
    gate already reads only the boolean ``True``, and the default coercion here
    would have read the string ``"yes"`` as approval before the gate ever saw
    it, so the same rule is applied at the boundary.
    """

    model_config = ConfigDict(extra="forbid")

    approved: StrictBool
    reason: str | None = None


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
    )


@router.post("/plans", response_model=PlanRunView, status_code=status.HTTP_201_CREATED)
async def start_plan(request: PlanRequest, state: State, build: Builder) -> PlanRunView:
    """Run the plan graph for one evening, up to the gate or to the reason it stopped."""
    graph = build()
    plan_date = request.plan_date or state.clock.today()
    thread_id = thread_for(plan_date)
    result = await graph.ainvoke(
        PlanState(plan_date=plan_date, rounds=0),
        config=run_config(thread_id),
        durability=DURABILITY,
    )
    return run_view(thread_id, plan_date, dict(result))


@router.get("/approvals", response_model=ApprovalQueueView)
def approvals(state: State) -> ApprovalQueueView:
    """Every draft waiting for a decision, oldest first. Needs no model to read."""
    return ApprovalQueueView(
        generated_at=state.clock.now(),
        waiting=[ApprovalView.from_record(record) for record in state.drafts.waiting()],
    )


@router.get("/approvals/{draft_id}", response_model=ApprovalView)
def approval(draft_id: str, state: State) -> ApprovalView:
    """One draft, waiting or decided, as the table records it."""
    record = state.drafts.get(draft_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no draft {draft_id!r}")
    return ApprovalView.from_record(record)


@router.post("/approvals/{draft_id}", response_model=DecisionView)
async def decide(
    draft_id: str, request: DecisionRequest, state: State, build: Builder
) -> DecisionView:
    """Resume the paused run with the decision, and report what was recorded."""
    return await decide_draft(state, build, draft_id, request)


async def decide_draft(
    state: ApplicationState, build: PlanGraphBuilder, draft_id: str, request: DecisionRequest
) -> DecisionView:
    """The decision, from the table check to the resumed thread, under one lock.

    The table is read first, so an unknown or already decided draft is refused
    without a graph and therefore without a key. The graph is built only after
    that, and asked whether the thread is still waiting at the gate and was
    written by this version, since the table can say a draft waits while the
    thread has moved on. The lock spans the whole sequence: two requests about
    one draft cannot both see it waiting, and the table's own refusal covers a
    second process.
    """
    async with state.decision_lock:
        record = state.drafts.get(draft_id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no draft {draft_id!r}")
        if not record.waiting:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"draft {draft_id!r} was already {record.decision}",
            )
        graph = build()
        config = run_config(record.thread_id)
        snapshot = await graph.aget_state(config)
        if snapshot.next != ("require_human_approval",):
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail=f"draft {draft_id!r} is not waiting at the gate"
            )
        try:
            ensure_current_version(snapshot)
        except StaleGraphVersion as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        resume: Command[Any] = Command(
            resume={"approved": request.approved, "reason": request.reason}
        )
        try:
            await graph.ainvoke(resume, config=config, durability=DURABILITY)
        except AlreadyDecided as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        decided = state.drafts.get(draft_id)
    if decided is None or decided.waiting:
        msg = f"the run resumed but no decision was recorded for {draft_id!r}"
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=msg)
    return DecisionView.from_record(decided)


@router.get("/checkpoint", response_model=ParentCheckpointView)
def checkpoint(state: State) -> ParentCheckpointView:
    """Return the parent checkpoint as a fixed placeholder response."""
    return ParentCheckpointView(
        checkpoint_at=state.clock.now(),
        assignments=[
            ParentCheckpointAssignmentView(
                course="World History",
                title="Canal Era comparison essay",
                aggregate_status="in_progress",
                has_schedule_conflict=True,
            )
        ],
    )
