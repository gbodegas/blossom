"""What each principal is allowed to see, as three separate models.

There is no shared view with a role flag. Each principal has its own model with
``extra="forbid"``, so a field that does not belong in a projection cannot be
serialized into it by accident. The parent view has no workload field to omit;
it has no such field at all, and adding one fails validation rather than leaking.

Known gap. These models are a convention enforced at serialization time, not a
visibility policy between the shared state and the agents. A route that reads a
store directly and renders whatever it likes would bypass them; the design notes
call for that policy layer.
"""

from datetime import date

from pydantic import AwareDatetime, BaseModel, ConfigDict

from blossom.drafts import Decision, DraftStatus
from blossom.reconciliation import SourceConfidence
from blossom.stores.drafts import DraftRecord
from blossom.stores.project_state import AssignmentKind


class StudentAssignmentView(BaseModel):
    """One assignment as she sees it, including how well its date is corroborated.

    A workload signal is a statement about the plan as a whole, not a
    per-assignment counter, so no such field lives on this row.
    """

    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    course: str
    title: str
    due_date: date | None
    kind: AssignmentKind = AssignmentKind.HOMEWORK
    submission_status: str
    deadline_confidence: SourceConfidence
    source_channels: list[str]
    disagreement: list[str]
    contradiction: list[str] = []
    """What the sources say when none of them supports the record's date; empty otherwise."""


class StudentDueThisWeekView(BaseModel):
    """Her week. Every assignment in the window appears, nothing is filtered out."""

    model_config = ConfigDict(extra="forbid")

    generated_at: AwareDatetime
    assignments: list[StudentAssignmentView]


class ParentCheckpointAssignmentView(BaseModel):
    """One assignment as a parent sees it: aggregate status, no detail."""

    model_config = ConfigDict(extra="forbid")

    course: str
    title: str
    aggregate_status: str
    has_schedule_conflict: bool


class ParentCheckpointView(BaseModel):
    """A checkpoint rather than a live feed.

    A parent gets a periodic summary by design. The narrow shape keeps a
    collaborator's view from becoming surveillance.
    """

    model_config = ConfigDict(extra="forbid")

    checkpoint_at: AwareDatetime
    assignments: list[ParentCheckpointAssignmentView]


class PlanRunView(BaseModel):
    """How a run of the plan graph ended, for the parent who started it.

    ``waiting`` is true when the run paused at the gate with a draft; then
    ``draft_id`` names what to look at. Otherwise ``outcome`` says why there
    is nothing to approve.
    """

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    plan_date: date
    outcome: str
    draft_id: str | None
    waiting: bool


class ApprovalView(BaseModel):
    """One draft as the parent sees it: the text, its status, and any decision.

    The body already carries the plan, the doubtful due dates, and the
    reviewer's notes as prose, so the parent reads one thing.
    """

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    plan_date: date
    status: DraftStatus
    outcome: str
    body: str
    created_at: AwareDatetime
    decision: Decision | None
    reason: str | None
    decided_at: AwareDatetime | None

    @classmethod
    def from_record(cls, record: DraftRecord) -> "ApprovalView":
        """The parent's projection of a table row. The thread id stays out of it."""
        return cls(
            draft_id=record.draft_id,
            plan_date=record.plan_date,
            status=record.status,
            outcome=record.outcome,
            body=record.body,
            created_at=record.created_at,
            decision=record.decision,
            reason=record.reason,
            decided_at=record.decided_at,
        )


class ApprovalQueueView(BaseModel):
    """What waits for a decision, oldest first."""

    model_config = ConfigDict(extra="forbid")

    generated_at: AwareDatetime
    waiting: list[ApprovalView]


class DecisionView(BaseModel):
    """What was recorded when the parent decided."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    status: DraftStatus
    decision: Decision
    reason: str | None
    decided_at: AwareDatetime

    @classmethod
    def from_record(cls, record: DraftRecord) -> "DecisionView":
        """A decided row. The caller checks it is decided; here that is required."""
        if record.decision is None or record.decided_at is None:
            msg = f"draft {record.draft_id!r} has no decision to show"
            raise ValueError(msg)
        return cls(
            draft_id=record.draft_id,
            status=record.status,
            decision=record.decision,
            reason=record.reason,
            decided_at=record.decided_at,
        )


class VerifierClaimView(BaseModel):
    """A claim and its checkable basis, for the layer that checks before anything ships."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    factual_claim: str
    policy_basis: str | None
    source_channels: list[str]
    verification_status: str
