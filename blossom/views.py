from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class StudentAssignmentView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    course: str
    title: str
    due_date: date | None
    submission_status: str
    workload_signal_count: int
    disagreement: list[str]


class StudentDueThisWeekView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    expectation: str
    assignments: list[StudentAssignmentView]


class ParentCheckpointAssignmentView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course: str
    title: str
    aggregate_status: str
    has_schedule_conflict: bool


class ParentCheckpointView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint_at: datetime
    assignments: list[ParentCheckpointAssignmentView]


class VerifierClaimView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    factual_claim: str
    policy_basis: str | None
    source_channels: list[str]
    verification_status: str
