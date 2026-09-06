"""Parent routes: review the plan she has, and start one for her when asked.

A parent is a collaborator who sets goals, corrects information and reviews
drafts, so these routes expose a queue and a checkpoint rather than a live
feed. A live feed would turn collaboration into monitoring.

The plan is hers from the moment it is made: it is on her page before anyone
here has seen it, and nothing she does with it waits for this page. What a
parent records here is a review, looks good or a change asked for, and the
review shows on her page under the plan. The pause at the gate is the same
mechanism that will one day hold a note to a teacher until a person decides;
for an evening's plan it holds only the review.

Three routes drive the plan graph. ``POST /parent/plans`` starts a run for one
evening, the same run her page starts, and reports how it ended: at the gate
with a draft, or before it with a reason. ``GET /parent/approvals`` lists the
drafts waiting for a review, read from the drafts table rather than from graph
state, because the table is the record across threads and needs no model to
read. ``POST /parent/approvals/{draft_id}`` resumes the paused run with the
review; the graph's gate node records it in saved state and the node after the
gate records it in the table.

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
rather than a client: a date to plan for her, the drafts waiting with their
text and two buttons, and what has been reviewed. Its two form actions call
the same functions the JSON routes call and redirect back to the page, so
there is one way to start a run and one way to review, whichever door it
comes through.

Not yet implemented: when the system notifies a parent that a deadline is at
risk, the parent needs to be able to see that the notification happened.
Without that, the visibility policy is stated but not observable.
"""

import logging
from datetime import date
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from blossom.agent.runs import DURABILITY, StaleGraphVersion, ensure_current_version, run_config
from blossom.anthropic_client import model_configured
from blossom.dependencies import ApplicationState, get_application_state
from blossom.evening import Staleness, staleness
from blossom.routes.runs import Graphs, PlanGraphBuilder, require_model, run_plan, tidy_thread
from blossom.settings import TEMPLATE_PATH
from blossom.stores.drafts import AlreadyDecided, DraftRecord
from blossom.views import (
    ApprovalQueueView,
    ApprovalView,
    DecisionView,
    ParentCheckpointAssignmentView,
    ParentCheckpointView,
    PlanRunView,
    RunView,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/parent", tags=["parent"])
templates = Jinja2Templates(directory=TEMPLATE_PATH)

State = Annotated[ApplicationState, Depends(get_application_state)]


class PlanRequest(BaseModel):
    """Which evening to plan. Defaults to today in the household's zone."""

    model_config = ConfigDict(extra="forbid")

    plan_date: date | None = None


REASON_MAX_LENGTH: Final = 500
"""The most that is kept of a reason: a sentence or two she would recognize.
The form and the JSON route hold to the same number, so a reason that fits
one fits the other."""


class DecisionRequest(BaseModel):
    """What the parent decided.

    ``approved`` is strict: a JSON ``true`` or ``false`` and nothing else. The
    gate already reads only the boolean ``True``, and the default coercion here
    would have read the string ``"yes"`` as approval before the gate ever saw
    it, so the same rule is applied at the boundary.
    """

    model_config = ConfigDict(extra="forbid")

    approved: StrictBool
    reason: str | None = Field(default=None, max_length=REASON_MAX_LENGTH)


SIGNALED_SINCE: Final = (
    "She has said today is too much since this plan was made. Plan again before approving."
)
SIGNAL_ENDED: Final = (
    "Her signal for this evening has ended, taken back or past its week, and this plan "
    "was kept short for it. Plan again for the full evening."
)


def stale_reason(state: ApplicationState, record: DraftRecord) -> str | None:
    """Why a waiting draft has stopped fitting the evening, or ``None`` while it fits.

    A draft is made for the evening as she had described it at the time. When
    her signal has changed since, the plan on the page is not the plan the
    checks held to the current budget, so it is not approved as it stands.
    Refusing it is still allowed; refusing never sends anything. Only a
    waiting draft can be stale: a decided one is a record of what was decided,
    and is not measured against the evening again.

    A signal that is gone was either taken back or aged out of the store, and
    the store does not say which, so the message names both rather than
    putting an action on her that she may not have taken. A draft for an
    evening that has passed is never stale: it cannot be planned again, since
    the pages refuse a past evening, and it reaches no page of hers, so there
    is nothing a fresh plan would put right.
    """
    if not record.waiting or record.plan_date < state.clock.today():
        return None
    match staleness(state.workload_signals, record):
        case Staleness.SIGNALED_SINCE:
            return SIGNALED_SINCE
        case Staleness.SIGNAL_ENDED:
            return SIGNAL_ENDED
        case None:
            return None


def approval_view(state: ApplicationState, record: DraftRecord) -> ApprovalView:
    """A draft as the parent sees it, with whether it still fits the evening."""
    return ApprovalView.from_record(record, stale=stale_reason(state, record))


def passed(evening: date) -> str:
    """Why a plan for a past evening is refused: no page of hers would ever show it."""
    return (
        f"The evening of {evening.isoformat()} has passed. Plans are for today or a later evening."
    )


@router.post("/plans", response_model=PlanRunView, status_code=status.HTTP_201_CREATED)
async def start_plan(request: PlanRequest, state: State, graphs: Graphs) -> PlanRunView:
    """Run the plan graph for one evening, up to the gate or to the reason it stopped.

    An evening that has passed is refused with 422 before anything runs.
    """
    evening = request.plan_date or state.clock.today()
    if evening < state.clock.today():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=passed(evening))
    require_model(graphs)
    return await run_plan(
        graphs.build(),
        evening,
        state,
    )


@router.get("/approvals", response_model=ApprovalQueueView)
def approvals(state: State) -> ApprovalQueueView:
    """Every draft waiting for a decision, oldest first. Needs no model to read."""
    return ApprovalQueueView(
        generated_at=state.clock.now(),
        waiting=[approval_view(state, record) for record in state.drafts.waiting()],
    )


@router.get("/approvals/{draft_id}", response_model=ApprovalView)
def approval(draft_id: str, state: State) -> ApprovalView:
    """One draft, waiting or decided, as the table records it."""
    record = state.drafts.get(draft_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no draft {draft_id!r}")
    return approval_view(state, record)


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

    A thread past the gate with its record unwritten is a review that reached
    the thread and then failed to land in the table. It is finished with the
    decision the thread holds rather than given a new one, whatever the request
    says and whatever the evening's signal says now, since the review was
    checked against the evening when it was given; a request that disagrees
    with what stood is told so.
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
        config = run_config(record.thread_id, callbacks=[state.tracer])
        snapshot = await graph.aget_state(config)
        resume: Command[Any] | None
        if snapshot.next == ("require_human_approval",):
            stale = stale_reason(state, record)
            if request.approved and stale is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, detail=stale)
            resume = Command(resume={"approved": request.approved, "reason": request.reason})
        elif snapshot.next == ("record_decision",):
            resume = None
        else:
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail=f"draft {draft_id!r} is not waiting at the gate"
            )
        try:
            ensure_current_version(snapshot)
        except StaleGraphVersion as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        try:
            await graph.ainvoke(resume, config=config, durability=DURABILITY)
        except AlreadyDecided as error:
            # The review reached the thread, so the gate is passed and the
            # thread cannot pause again, and the table refused because the
            # draft was closed meanwhile: a later plan published from another
            # process, since publication in this one waits for the lock this
            # review holds. Nothing can resume the thread, so it goes, and a
            # thread that cannot be cleared now is left to the sweep.
            await tidy_thread(record.thread_id, state)
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        decided = state.drafts.get(draft_id)
        # The decision is in the table; the loop is over and its state is cleared.
        await tidy_thread(record.thread_id, state)
    if decided is None or decided.waiting:
        msg = f"the run resumed but no decision was recorded for {draft_id!r}"
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=msg)
    if resume is None and (decided.decision == "approved") != request.approved:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"draft {draft_id!r} was already {decided.decision}"
        )
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
            "waiting": [approval_view(state, record) for record in state.drafts.waiting()],
            "decided": [approval_view(state, record) for record in state.drafts.decided()],
            "ended": [RunView.from_record(run) for run in state.drafts.runs_without_a_draft()],
            "problem": problem,
            "reason_max_length": REASON_MAX_LENGTH,
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
    """The plan form. A blank date means today; a bad one is said, not guessed at.

    A run that fails on the way for any reason other than a refusal is said on
    the page too, with the queue below unchanged; the run has already taken
    back what it left, and the failure goes to the process log. An evening that
    has passed is refused before anything runs: a plan for it could reach no
    page of hers.
    """
    try:
        evening = date.fromisoformat(plan_date) if plan_date.strip() else state.clock.today()
    except ValueError:
        return review_page(
            request,
            state,
            problem=f"{plan_date!r} is not a date. Use the form YYYY-MM-DD.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if evening < state.clock.today():
        return review_page(
            request,
            state,
            problem=passed(evening),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    try:
        require_model(graphs)
        await run_plan(
            graphs.build(),
            evening,
            state,
        )
    except HTTPException as error:
        return review_page(request, state, problem=str(error.detail), status_code=error.status_code)
    except Exception:
        logger.exception("the plan for %s failed on the way", evening)
        return review_page(
            request,
            state,
            problem=(
                "The plan could not be made: something went wrong on the way. "
                "What is waiting below is unchanged."
            ),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
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
    reason = reason.strip()
    if len(reason) > REASON_MAX_LENGTH:
        return review_page(
            request,
            state,
            problem=(
                f"A reason is at most {REASON_MAX_LENGTH} characters; this one is {len(reason)}."
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    decided = DecisionRequest(approved=decision == "approve", reason=reason or None)
    try:
        await decide_draft(state, graphs.build, draft_id, decided)
    except HTTPException as error:
        return review_page(request, state, problem=str(error.detail), status_code=error.status_code)
    return RedirectResponse("/parent", status_code=status.HTTP_303_SEE_OTHER)
