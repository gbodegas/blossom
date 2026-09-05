"""Student routes. She is the primary user, and this is the primary view.

Nothing is filtered out of her week: an assignment the system cannot corroborate
is the one she most needs to see, so every assignment in the window reaches the
page with its date confidence attached. The workload signal takes no argument:
rating or describing the load requires stepping back, and that capacity is least
available exactly when the signal matters.

Gaps: the signal is accepted and discarded; it should produce an immediate
visible result, since a control that changes nothing observable gets abandoned.
The week is read the way the plan graph reads it, so the two never differ
about what is in it, and an assignment whose record date the school's sources
contradict says so on her page.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from blossom.dependencies import ApplicationState, get_application_state
from blossom.noticing import read_week
from blossom.principals import Principal
from blossom.reconciliation import Disagreement, Reconciler, classify_confidence
from blossom.settings import TEMPLATE_PATH
from blossom.views import StudentAssignmentView, StudentDueThisWeekView

router = APIRouter(prefix="/student", tags=["student"])
templates = Jinja2Templates(directory=TEMPLATE_PATH)


class WorkloadSignalRequest(BaseModel):
    """Optional detail attached to a signal. The signal itself needs no body."""

    model_config = ConfigDict(extra="forbid")

    detail: str | None = None


class WorkloadSignalResponse(BaseModel):
    """Acknowledgment that a signal was received. It reports receipt and nothing
    more, because nothing more happens yet.
    """

    model_config = ConfigDict(extra="forbid")

    principal: Principal
    recorded_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    detail_attached: bool


@router.post("/workload-signals")
def register_workload_signal(
    payload: Annotated[WorkloadSignalRequest | None, Body()] = None,
) -> WorkloadSignalResponse:
    """Accept a workload signal. ``payload`` is optional so an empty POST works
    with no fields and no decisions. Records nothing yet.
    """
    return WorkloadSignalResponse(
        principal=Principal.STUDENT,
        detail_attached=payload is not None and payload.detail is not None,
    )


def build_student_due_this_week_view(state: ApplicationState) -> StudentDueThisWeekView:
    """Assemble the student's weekly view from the stores ``ApplicationState``
    opened at startup; nothing is opened or seeded per request.
    """
    week = read_week(state.project_state, state.source, state.clock.today())
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
    return StudentDueThisWeekView(generated_at=datetime.now(UTC), assignments=views)


@router.get("/due-this-week", response_class=HTMLResponse)
def due_this_week(
    request: Request,
    state: Annotated[ApplicationState, Depends(get_application_state)],
) -> HTMLResponse:
    """Render her week, every assignment labeled with its source confidence."""
    view = build_student_due_this_week_view(state)
    return templates.TemplateResponse(
        request,
        "student_due_this_week.html",
        {"view": view},
    )
