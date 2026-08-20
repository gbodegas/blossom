from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from blossom.reconciliation import SourceConfidence


class StudentAssignmentView(BaseModel):
    """One assignment as she sees it, including how well its date is corroborated.

    ``workload_signal_count`` used to live here, hardcoded to ``1`` for every
    assignment regardless of anything. It is removed rather than fixed: a
    workload signal says the current plan is too much, which is a statement
    about the plan as a whole, not a counter that belongs on an individual row.
    """

    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    course: str
    title: str
    due_date: date | None
    submission_status: str
    deadline_confidence: SourceConfidence
    source_channels: list[str]
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
