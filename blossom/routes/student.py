"""Student routes. She is the primary user, and this is the primary view.

Nothing is filtered out of her week: an assignment the system cannot corroborate
is the one she most needs to see, so every assignment in the window reaches the
page with its date confidence attached. The week is read the way the plan
graph reads it, so the two never differ about what is in it, and an assignment
whose record date the school's sources contradict says so on her page.

Today's plan is hers. She asks for it from this page, it appears here the
moment it is made, and a parent's review of it shows under it afterwards
without ever being a condition on it. Making a plan asks the model, which
takes a minute or two and needs a key; without one the page says so and
everything else on it still works.

The workload signal takes no argument: rating or describing the load requires
stepping back, and that capacity is least available exactly when the signal
matters. One press records that today is too much, the page shows it at once
with what it changes, tonight's plan is held to a reduced budget, and she can
take it back. The page also lists every signal still kept, each with a way to
remove it, because the record is hers.
"""

import logging
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from blossom.anthropic_client import model_configured
from blossom.dependencies import ApplicationState, get_application_state
from blossom.evening import Staleness, staleness
from blossom.noticing import read_week
from blossom.plan_checks import DEFAULT_DAILY_MINUTES, reduced_budget
from blossom.principals import Principal
from blossom.reconciliation import Disagreement, Reconciler, classify_confidence
from blossom.routes.runs import Graphs, require_model, run_plan
from blossom.settings import TEMPLATE_PATH
from blossom.stores.drafts import DraftRecord
from blossom.stores.workload_signals import DETAIL_MAX_LENGTH, WorkloadSignal
from blossom.views import (
    StudentAssignmentView,
    StudentDueThisWeekView,
    StudentPlanView,
    WorkloadSignalView,
)

logger = logging.getLogger(__name__)

PAGE: Final = "/student/due-this-week"

SIGNALED_SINCE: Final = (
    "You have said today is too much, and this plan was made for the full evening. "
    "Plan again to make it smaller."
)
SIGNAL_ENDED: Final = (
    "This plan was kept short for a signal that is not there now. Plan again for the full evening."
)

router = APIRouter(prefix="/student", tags=["student"])
templates = Jinja2Templates(directory=TEMPLATE_PATH)

State = Annotated[ApplicationState, Depends(get_application_state)]


class WorkloadSignalRequest(BaseModel):
    """Optional detail attached to a signal. The signal itself needs no body.

    The words are capped at the boundary, so a request cannot grow the drafts
    file or every later page by sending more than a sentence or two.
    """

    model_config = ConfigDict(extra="forbid")

    detail: str | None = Field(default=None, max_length=DETAIL_MAX_LENGTH)


class WorkloadSignalResponse(BaseModel):
    """What one press did: the signal exactly as kept, words included."""

    model_config = ConfigDict(extra="forbid")

    principal: Principal
    signal: WorkloadSignalView


def signal_view(state: ApplicationState, signal: WorkloadSignal) -> WorkloadSignalView:
    """Her projection of a signal, with the time in the household's zone.

    Everything the store holds about a signal is in it, her words included, so
    what she can see is what is kept.
    """
    return WorkloadSignalView(
        signal_id=signal.signal_id,
        evening=signal.evening,
        given_at=signal.given_at,
        given_local=signal.given_at.astimezone(state.clock.zone),
        detail=signal.detail,
    )


async def record_signal(state: ApplicationState, detail: str | None) -> WorkloadSignal:
    """Keep one press, about today by the household's clock.

    Written under the decision lock: a decision is checked against the evening
    as signaled, and the evening must not change between that check and the
    decision landing. A press during a decision waits the moment it takes.
    """
    async with state.decision_lock:
        return state.workload_signals.record(state.clock.today(), detail)


async def withdraw_signal(state: ApplicationState, signal_id: str) -> bool:
    """Take one signal back, under the same lock and for the same reason."""
    async with state.decision_lock:
        return state.workload_signals.withdraw(signal_id)


@router.post("/workload-signals", status_code=status.HTTP_201_CREATED)
async def register_workload_signal(
    state: State,
    payload: Annotated[WorkloadSignalRequest | None, Body()] = None,
) -> WorkloadSignalResponse:
    """Record that today is too much. ``payload`` is optional so an empty POST works."""
    detail = None if payload is None else payload.detail
    signal = await record_signal(state, detail)
    return WorkloadSignalResponse(principal=Principal.STUDENT, signal=signal_view(state, signal))


@router.get("/workload-signals")
def held_workload_signals(state: State) -> list[WorkloadSignalView]:
    """Every signal still kept, most recent first: what she can see and take back."""
    return [signal_view(state, signal) for signal in state.workload_signals.held()]


@router.delete("/workload-signals/{signal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def withdraw_workload_signal(signal_id: str, state: State) -> Response:
    """Take a signal back. It is gone, not marked; the record is hers to remove."""
    if not await withdraw_signal(state, signal_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no signal {signal_id!r}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def plan_view(state: ApplicationState, record: DraftRecord) -> StudentPlanView:
    """Her projection of a draft: the plan, a parent's review if any, and whether it still fits."""
    stale = None
    match staleness(state.workload_signals, record):
        case Staleness.SIGNALED_SINCE:
            stale = SIGNALED_SINCE
        case Staleness.SIGNAL_ENDED:
            stale = SIGNAL_ENDED
        case None:
            stale = None
    return StudentPlanView(
        draft_id=record.draft_id,
        plan_date=record.plan_date,
        body=record.body,
        made_at=record.created_at,
        made_local=record.created_at.astimezone(state.clock.zone),
        outcome=record.outcome,
        too_much=record.too_much,
        decision=record.decision,
        reason=record.reason,
        stale=stale,
    )


def todays_plan(state: ApplicationState) -> StudentPlanView | None:
    """Today's latest plan, or ``None`` when none has been made."""
    record = state.drafts.latest_for(state.clock.today())
    return None if record is None else plan_view(state, record)


@router.get("/plans/today", response_model=StudentPlanView)
def plan_for_today(state: State) -> StudentPlanView:
    """Today's plan as she sees it. 404 until one has been made."""
    plan = todays_plan(state)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no plan has been made for today")
    return plan


@router.post("/plans", response_model=StudentPlanView, status_code=status.HTTP_201_CREATED)
async def make_todays_plan(state: State, graphs: Graphs) -> StudentPlanView:
    """Ask for today's plan. It is hers as soon as it is made; a parent's review comes after.

    A run that ends without a plan, because the checks never passed or the
    model did not answer, is a 409 naming the outcome, and her page keeps
    whatever plan it had.
    """
    require_model(graphs)
    run = await run_plan(
        graphs.build(),
        state.clock.today(),
        state,
    )
    if run.draft_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"no plan was made: the run ended with {run.outcome}"
        )
    record = state.drafts.get(run.draft_id)
    if record is None:
        msg = f"the run made {run.draft_id!r} but the table has no such draft"
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=msg)
    return plan_view(state, record)


def build_student_due_this_week_view(state: ApplicationState) -> StudentDueThisWeekView:
    """Assemble the student's weekly view from the stores ``ApplicationState``
    opened at startup; nothing is opened or seeded per request.
    """
    today = state.clock.today()
    week = read_week(state.project_state, state.source, today)
    reconciler = Reconciler()
    views: list[StudentAssignmentView] = []
    for assignment in week.assignments:
        records = week.records[assignment.assignment_id]
        noticed = week.noticings[assignment.assignment_id]
        reconciliation = reconciler.reconcile(records)
        disagreement = []
        if isinstance(reconciliation, Disagreement):
            disagreement = [claim.describe() for claim in reconciliation.conflicting_claims]
        # Never filter here; see the module docstring.
        views.append(
            StudentAssignmentView(
                assignment_id=assignment.assignment_id,
                course=assignment.course,
                title=assignment.title,
                due_date=assignment.due_date,
                kind=assignment.kind,
                submission_status=assignment.reported_submission_status,
                deadline_confidence=classify_confidence(reconciliation),
                source_channels=[record.channel for record in records],
                disagreement=disagreement,
                contradiction=list(noticed.observed) if noticed.contradicted else [],
            )
        )
    tonight = state.workload_signals.for_evening(today)
    return StudentDueThisWeekView(
        generated_at=datetime.now(UTC),
        assignments=views,
        full_budget_minutes=DEFAULT_DAILY_MINUTES,
        budget_minutes=reduced_budget(DEFAULT_DAILY_MINUTES) if tonight else DEFAULT_DAILY_MINUTES,
        plan=todays_plan(state),
        can_plan=model_configured(state.settings),
        too_much=signal_view(state, tonight[-1]) if tonight else None,
        signals=[signal_view(state, signal) for signal in state.workload_signals.held()],
    )


def student_page(
    request: Request,
    state: ApplicationState,
    *,
    problem: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """Render her page. ``problem`` is what an action could not do, said once at the top."""
    view = build_student_due_this_week_view(state)
    return templates.TemplateResponse(
        request,
        "student_due_this_week.html",
        {"view": view, "problem": problem},
        status_code=status_code,
    )


@router.get("/due-this-week", response_class=HTMLResponse)
def due_this_week(request: Request, state: State) -> HTMLResponse:
    """Render her week, every assignment labeled with its source confidence, and today's plan."""
    return student_page(request, state)


@router.post("/actions/plan", response_class=HTMLResponse, include_in_schema=False)
async def plan_from_the_page(request: Request, state: State, graphs: Graphs) -> Response:
    """The plan button. Makes today's plan and returns to the page, which shows it.

    A run that could not start or ended without a plan is said on the page
    with the status the JSON route would have answered, and whatever plan the
    page already had stays. So is a run that failed on the way, for any other
    reason: the run has already taken back what it left by then, the failure
    goes to the process log, and the page says something went wrong rather
    than answering with a bare error.
    """
    try:
        require_model(graphs)
        run = await run_plan(
            graphs.build(),
            state.clock.today(),
            state,
        )
    except HTTPException as error:
        return student_page(
            request,
            state,
            problem=f"Blossom could not make a plan: {error.detail}",
            status_code=error.status_code,
        )
    except Exception:
        logger.exception("today's plan failed on the way")
        return student_page(
            request,
            state,
            problem=(
                "Blossom could not make a plan: something went wrong on the way. "
                "The plan already here, if any, is unchanged."
            ),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    if run.draft_id is None:
        return student_page(
            request,
            state,
            problem=f"No plan was made this time: the run ended with {run.outcome}.",
            status_code=status.HTTP_409_CONFLICT,
        )
    return RedirectResponse(PAGE, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/actions/too-much", response_class=HTMLResponse, include_in_schema=False)
async def too_much_from_the_page(state: State) -> Response:
    """The one control on her page. Records the signal and returns to the page, which shows it."""
    await record_signal(state, None)
    return RedirectResponse(PAGE, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/actions/take-back/{signal_id}", response_class=HTMLResponse, include_in_schema=False)
async def take_back_from_the_page(signal_id: str, state: State) -> Response:
    """Remove a signal from her page. A signal already gone is not an error here."""
    await withdraw_signal(state, signal_id)
    return RedirectResponse(PAGE, status_code=status.HTTP_303_SEE_OTHER)
