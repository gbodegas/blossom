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

Starting a run needs the model seam, which needs a key, and says so with a
503 when there is none. Reading the queue and deciding do not: nothing past
the gate asks a model, so a graph built without a key can still resume a
paused thread and record the decision. The graph is built inside the handler,
after the table has been consulted, because a dependency is resolved before a
handler runs: a draft that does not exist is a 404 with or without a key, and
one already decided is a 409.

Two decisions about one draft cannot both land. The handler holds the
application's decision lock from the table check through the resume, and the
table refuses a second, different decision even if a request arrives from
another process.

The page at ``/parent`` is the same three things as a form, for a person
rather than a client: a date to plan, the drafts waiting with their text and
two buttons, and what has been decided. Its two form actions call the same
functions the JSON routes call and redirect back to the page, so there is one
way to start a run and one way to decide, whichever door it comes through.

Not yet implemented: when the system notifies a parent that a deadline is at
risk, the parent needs to be able to see that the notification happened.
Without that, the visibility policy is stated but not observable.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Final
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, StrictBool

from blossom.agent.graph import CompiledPlanGraph, PlanState, plan_graph_for
from blossom.agent.runs import DURABILITY, StaleGraphVersion, ensure_current_version, run_config
from blossom.anthropic_client import MISSING_KEY, ModelUnavailable, model_configured
from blossom.dependencies import ApplicationState, get_application_state
from blossom.settings import TEMPLATE_PATH
from blossom.stores.drafts import AlreadyDecided, DraftRecord, DraftsStore
from blossom.views import (
    ApprovalQueueView,
    ApprovalView,
    DecisionView,
    ParentCheckpointAssignmentView,
    ParentCheckpointView,
    PlanRunView,
    RunView,
)

router = APIRouter(prefix="/parent", tags=["parent"])
templates = Jinja2Templates(directory=TEMPLATE_PATH)

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
        steps=list(result.get("steps", [])),
    )


def approval_view(drafts: DraftsStore, record: DraftRecord) -> ApprovalView:
    """A draft with the record of the run that made it."""
    return ApprovalView.from_record(record, drafts.steps_for(record.thread_id))


async def run_plan(graph: CompiledPlanGraph, plan_date: date, drafts: DraftsStore) -> PlanRunView:
    """Run the graph for one evening on a fresh thread, to the gate or to the reason it stopped.

    The run's record is saved once it has stopped, whether or not a draft came
    of it, so a run that produced nothing to approve can still be read.
    """
    thread_id = thread_for(plan_date)
    try:
        result = await graph.ainvoke(
            PlanState(plan_date=plan_date, rounds=0),
            config=run_config(thread_id),
            durability=DURABILITY,
        )
    except ModelUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    view = run_view(thread_id, plan_date, dict(result))
    drafts.record_run(
        thread_id=thread_id, plan_date=plan_date, outcome=view.outcome, steps=view.steps
    )
    return view


@router.post("/plans", response_model=PlanRunView, status_code=status.HTTP_201_CREATED)
async def start_plan(request: PlanRequest, state: State, graphs: Graphs) -> PlanRunView:
    """Run the plan graph for one evening, up to the gate or to the reason it stopped."""
    require_model(graphs)
    return await run_plan(graphs.build(), request.plan_date or state.clock.today(), state.drafts)


@router.get("/approvals", response_model=ApprovalQueueView)
def approvals(state: State) -> ApprovalQueueView:
    """Every draft waiting for a decision, oldest first. Needs no model to read."""
    return ApprovalQueueView(
        generated_at=state.clock.now(),
        waiting=[approval_view(state.drafts, record) for record in state.drafts.waiting()],
    )


@router.get("/approvals/{draft_id}", response_model=ApprovalView)
def approval(draft_id: str, state: State) -> ApprovalView:
    """One draft, waiting or decided, as the table records it."""
    record = state.drafts.get(draft_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no draft {draft_id!r}")
    return approval_view(state.drafts, record)


@router.post("/approvals/{draft_id}", response_model=DecisionView)
async def decide(
    draft_id: str, request: DecisionRequest, state: State, graphs: Graphs
) -> DecisionView:
    """Resume the paused run with the decision, and report what was recorded."""
    return await decide_draft(state, graphs.build, draft_id, request)


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


# --------------------------------------------------------------------- the page

DECISIONS: Final = ("approve", "refuse")
"""The two buttons. Anything else in the field is a 422 page, not a guess."""


def review_page(
    request: Request,
    state: ApplicationState,
    *,
    problem: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """Render the queue, the decisions, and the form to plan an evening.

    ``problem`` is what a form action could not do, shown once at the top with
    the status the JSON route would have answered, so the page tells the truth
    the API tells.
    """
    return templates.TemplateResponse(
        request,
        "parent_review.html",
        {
            "today": state.clock.today(),
            "model_available": model_configured(state.settings),
            "waiting": [approval_view(state.drafts, record) for record in state.drafts.waiting()],
            "decided": [approval_view(state.drafts, record) for record in state.drafts.decided()],
            "ended": [RunView.from_record(run) for run in state.drafts.runs_without_a_draft()],
            "problem": problem,
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def review(request: Request, state: State) -> HTMLResponse:
    """The parent's page: what is waiting, what was decided, and a date to plan."""
    return review_page(request, state)


@router.post("/actions/plan", response_class=HTMLResponse, include_in_schema=False)
async def plan_from_the_page(
    request: Request,
    state: State,
    graphs: Graphs,
    plan_date: Annotated[str, Form()] = "",
) -> Response:
    """The plan form. A blank date means today; a bad one is said, not guessed at."""
    try:
        evening = date.fromisoformat(plan_date) if plan_date.strip() else state.clock.today()
    except ValueError:
        return review_page(
            request,
            state,
            problem=f"{plan_date!r} is not a date. Use the form YYYY-MM-DD.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    try:
        require_model(graphs)
        await run_plan(graphs.build(), evening, state.drafts)
    except HTTPException as error:
        return review_page(request, state, problem=str(error.detail), status_code=error.status_code)
    return RedirectResponse("/parent", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/actions/decide/{draft_id}", response_class=HTMLResponse, include_in_schema=False)
async def decide_from_the_page(
    request: Request,
    draft_id: str,
    state: State,
    graphs: Graphs,
    decision: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    """The two buttons under a waiting draft, through the same path the JSON route takes.

    The field is read as text and checked here rather than typed as a literal,
    because the framework's own validation would answer a bad value with a
    JSON error, and a form failure is promised as this page with the problem.
    """
    if decision not in DECISIONS:
        return review_page(
            request,
            state,
            problem=f"{decision!r} is not one of the two buttons, approve or refuse.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    decided = DecisionRequest(approved=decision == "approve", reason=reason.strip() or None)
    try:
        await decide_draft(state, graphs.build, draft_id, decided)
    except HTTPException as error:
        return review_page(request, state, problem=str(error.detail), status_code=error.status_code)
    return RedirectResponse("/parent", status_code=status.HTTP_303_SEE_OTHER)
