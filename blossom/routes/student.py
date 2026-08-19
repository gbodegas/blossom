import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from blossom.agent.loop import AgentStep, compare_expectation_to_observation
from blossom.principals import Principal
from blossom.reconciliation import Disagreement, Reconciler
from blossom.retrieval import (
    NothingRetrieved,
    RetrievalQuery,
    RetrievalRouter,
    SemanticRetriever,
    StructuredRetriever,
)
from blossom.sources import FixtureSource
from blossom.stores.project_state import Assignment, ProjectStateStore
from blossom.verification import Verifier
from blossom.views import StudentAssignmentView, StudentDueThisWeekView

router = APIRouter(prefix="/student", tags=["student"])
templates = Jinja2Templates(directory="blossom/templates")


class WorkloadSignalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: str | None = None


class WorkloadSignalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal: Principal
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    detail_attached: bool


class EmptySemanticCollection:
    def query(self, *, query_texts: list[str], n_results: int) -> dict[str, list[list[object]]]:
        return {"ids": [[]], "distances": [[]], "metadatas": [[]]}


@router.post("/workload-signals")
def register_workload_signal(
    payload: Annotated[WorkloadSignalRequest | None, Body()] = None,
) -> WorkloadSignalResponse:
    return WorkloadSignalResponse(
        principal=Principal.STUDENT,
        detail_attached=payload is not None and payload.detail is not None,
    )


def build_student_due_this_week_view(fixture_root: Path) -> StudentDueThisWeekView:
    source = FixtureSource(fixture_root)
    connection = sqlite3.connect(":memory:")
    project_store = ProjectStateStore(connection)
    project_store.upsert_assignments(source.assignments())
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
    verifier = Verifier()
    views: list[StudentAssignmentView] = []
    for assignment in assignments:
        records = source.deadline_records(assignment.assignment_id)
        reconciliation = reconciler.reconcile(records)
        verification = verifier.verify_fact(assignment.title, len(records))
        disagreement = []
        if isinstance(reconciliation, Disagreement):
            disagreement = [
                f"{claim.channel}: {claim.asserted_value}"
                for claim in reconciliation.conflicting_claims
            ]
        if verification.passed:
            views.append(
                StudentAssignmentView(
                    assignment_id=assignment.assignment_id,
                    course=assignment.course,
                    title=assignment.title,
                    due_date=assignment.due_date,
                    submission_status=assignment.reported_submission_status,
                    workload_signal_count=1,
                    disagreement=disagreement,
                )
            )
    return StudentDueThisWeekView(
        generated_at=datetime.now(UTC),
        expectation=checked_step.expectation,
        assignments=views,
    )


@router.get("/due-this-week", response_class=HTMLResponse)
def due_this_week(request: Request) -> HTMLResponse:
    view = build_student_due_this_week_view(Path("data/synthetic"))
    return templates.TemplateResponse(
        request,
        "student_due_this_week.html",
        {"view": view},
    )
