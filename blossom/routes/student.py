"""Student routes. She is the primary user, and this is the primary view.

Nothing is filtered out of her week: an assignment the system cannot corroborate
is the one she most needs to see, so every assignment in the window reaches the
page with its date confidence attached. The week is read the way the plan
graph reads it, so the two never differ about what is in it, and an assignment
whose record date the school's sources contradict says so on her page.

The workload signal takes no argument: rating or describing the load requires
stepping back, and that capacity is least available exactly when the signal
matters. One press records that today is too much, the page shows it at once
with what it changes, tonight's plan is held to a reduced budget, and she can
take it back. The page also lists every signal still kept, each with a way to
remove it, because the record is hers.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from blossom.dependencies import ApplicationState, get_application_state
from blossom.noticing import read_week
from blossom.plan_checks import DEFAULT_DAILY_MINUTES, reduced_budget
from blossom.principals import Principal
from blossom.reconciliation import Disagreement, Reconciler, classify_confidence
from blossom.settings import TEMPLATE_PATH
from blossom.stores.workload_signals import DETAIL_MAX_LENGTH, WorkloadSignal
from blossom.views import StudentAssignmentView, StudentDueThisWeekView, WorkloadSignalView

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
        too_much=signal_view(state, tonight[-1]) if tonight else None,
        signals=[signal_view(state, signal) for signal in state.workload_signals.held()],
    )


@router.get("/due-this-week", response_class=HTMLResponse)
def due_this_week(request: Request, state: State) -> HTMLResponse:
    """Render her week, every assignment labeled with its source confidence."""
    view = build_student_due_this_week_view(state)
    return templates.TemplateResponse(request, "student_due_this_week.html", {"view": view})


@router.post("/actions/too-much", response_class=HTMLResponse, include_in_schema=False)
async def too_much_from_the_page(state: State) -> Response:
    """The one control on her page. Records the signal and returns to the page, which shows it."""
    await record_signal(state, None)
    return RedirectResponse("/student/due-this-week", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/actions/take-back/{signal_id}", response_class=HTMLResponse, include_in_schema=False)
async def take_back_from_the_page(signal_id: str, state: State) -> Response:
    """Remove a signal from her page. A signal already gone is not an error here."""
    await withdraw_signal(state, signal_id)
    return RedirectResponse("/student/due-this-week", status_code=status.HTTP_303_SEE_OTHER)
