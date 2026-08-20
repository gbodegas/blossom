"""Student routes. She is the primary user, and this is the primary view.

Two commitments shape what is here.

Nothing is filtered out of her week. An assignment the system cannot
corroborate is the one she most needs to see, so every assignment in the window
reaches the page carrying how well its date is supported. A confident,
incomplete week is the failure this view exists to avoid.

The workload signal takes no argument. It does not ask her to rate or describe
anything, because assigning a rating requires stepping back and assessing, and
that capacity is least available exactly when the signal matters most. An
undifferentiated "this is too much" is the whole of the input.

Known gaps:

The workload signal is accepted and discarded. Nothing stores it, nothing
reduces a plan in response, and the response body reports only that it was
received. The design requires it to produce an immediate visible result, since
a control that changes nothing observable gets abandoned.

``EmptySemanticCollection`` is a stub that always returns no candidates, so the
semantic half of the retrieval router cannot return anything in the running
system. The structured half is real.

The expectation check is inert here. ``expectation`` is a lookup key and the
observation is the record id the store echoes back, so the comparison is a
string against itself and can never register a contradiction. Its result is
discarded; only ``expectation`` is read from the checked step.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from blossom.agent.loop import AgentStep, compare_expectation_to_observation
from blossom.dependencies import ApplicationState, get_application_state
from blossom.principals import Principal
from blossom.reconciliation import Disagreement, Reconciler, classify_confidence
from blossom.retrieval import (
    NothingRetrieved,
    RetrievalQuery,
    RetrievalRouter,
    SemanticRetriever,
    StructuredRetriever,
)
from blossom.settings import TEMPLATE_PATH
from blossom.stores.project_state import Assignment
from blossom.views import StudentAssignmentView, StudentDueThisWeekView

router = APIRouter(prefix="/student", tags=["student"])
templates = Jinja2Templates(directory=TEMPLATE_PATH)


class WorkloadSignalRequest(BaseModel):
    """Optional detail attached to a signal. The signal itself needs no body."""

    model_config = ConfigDict(extra="forbid")

    detail: str | None = None


class WorkloadSignalResponse(BaseModel):
    """Acknowledgement that a signal was received.

    It reports receipt and nothing more, because nothing more happens yet.
    """

    model_config = ConfigDict(extra="forbid")

    principal: Principal
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    detail_attached: bool


class EmptySemanticCollection:
    """A collection that never returns candidates. Stands in for an unwired store.

    Returning nothing is the safe stub: the retriever's contract already treats
    an empty result as a legitimate answer, so the router degrades to
    structured-only rather than inventing matches.
    """

    def query(self, *, query_texts: list[str], n_results: int) -> dict[str, list[list[object]]]:
        """Return no candidates, in the shape the retriever expects."""
        return {"ids": [[]], "distances": [[]], "metadatas": [[]]}


@router.post("/workload-signals")
def register_workload_signal(
    payload: Annotated[WorkloadSignalRequest | None, Body()] = None,
) -> WorkloadSignalResponse:
    """Accept a workload signal, with or without a body.

    The empty POST is the important case and is why ``payload`` is optional:
    the signal must work with no navigation, no fields and no decisions.

    Currently records nothing. See the module docstring.
    """
    return WorkloadSignalResponse(
        principal=Principal.STUDENT,
        detail_attached=payload is not None and payload.detail is not None,
    )


def build_student_due_this_week_view(state: ApplicationState) -> StudentDueThisWeekView:
    """Assemble the student's weekly view from stores opened at startup.

    Previously this function opened a SQLite connection, created the schema and
    re-seeded every assignment on each request. It now reads the stores that
    ``ApplicationState`` already holds.
    """
    source = state.source
    project_store = state.project_state
    router_for_retrieval = RetrievalRouter(
        structured=StructuredRetriever(project_store),
        semantic=SemanticRetriever(
            EmptySemanticCollection(),
            store_name="support_rules",
            source_channel="synthetic",
        ),
    )
    expectation = "due_this_week"
    step = AgentStep(
        expectation=expectation,
        tool_name="retrieval_router.retrieve",
        tool_input={"lookup_key": expectation},
        timestamp=datetime.now(UTC),
    )
    retrieved = router_for_retrieval.retrieve(
        RetrievalQuery(text="what is due this week", lookup_key=expectation)
    )
    if isinstance(retrieved, NothingRetrieved):
        observation = retrieved.reason
        assignments: list[Assignment] = []
    else:
        observation = retrieved.record_id
        assignments = [Assignment.model_validate(item) for item in retrieved.payload["assignments"]]
    checked_step = compare_expectation_to_observation(step, observation)
    reconciler = Reconciler()
    views: list[StudentAssignmentView] = []
    for assignment in assignments:
        records = source.deadline_records(assignment.assignment_id)
        reconciliation = reconciler.reconcile(records)
        disagreement = []
        if isinstance(reconciliation, Disagreement):
            disagreement = [
                f"{claim.channel}: {claim.asserted_value}"
                for claim in reconciliation.conflicting_claims
            ]
        # Every assignment is appended. Nothing here may filter: an assignment
        # the system cannot corroborate is precisely the one she most needs to
        # see, and dropping it would present a confident, incomplete week.
        views.append(
            StudentAssignmentView(
                assignment_id=assignment.assignment_id,
                course=assignment.course,
                title=assignment.title,
                due_date=assignment.due_date,
                submission_status=assignment.reported_submission_status,
                deadline_confidence=classify_confidence(reconciliation),
                source_channels=[record.channel for record in records],
                disagreement=disagreement,
            )
        )
    return StudentDueThisWeekView(
        generated_at=datetime.now(UTC),
        expectation=checked_step.expectation,
        assignments=views,
    )


@router.get("/due-this-week", response_class=HTMLResponse)
def due_this_week(
    request: Request,
    state: Annotated[ApplicationState, Depends(get_application_state)],
) -> HTMLResponse:
    """Render her week, every assignment labelled with its source confidence."""
    view = build_student_due_this_week_view(state)
    return templates.TemplateResponse(
        request,
        "student_due_this_week.html",
        {"view": view},
    )
