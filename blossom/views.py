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

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from blossom.reconciliation import SourceConfidence


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
    submission_status: str
    deadline_confidence: SourceConfidence
    source_channels: list[str]
    disagreement: list[str]


class StudentDueThisWeekView(BaseModel):
    """Her week. Every assignment in the window appears, nothing is filtered out.

    ``expectation`` carries what the agent said it expected to retrieve before
    it looked, so the page shows the belief the data was gathered against.
    """

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    expectation: str
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

    checkpoint_at: datetime
    assignments: list[ParentCheckpointAssignmentView]


class VerifierClaimView(BaseModel):
    """A claim and its checkable basis, for the layer that checks before anything ships."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    factual_claim: str
    policy_basis: str | None
    source_channels: list[str]
    verification_status: str
