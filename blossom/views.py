"""What each principal is allowed to see, as three separate models.

There is no shared view with a role flag on it. Each principal has its own
model, and every one sets ``extra="forbid"`` so a field that does not belong in
a projection cannot be serialised into it by accident. That is why the parent
view has no workload field to omit -- it has no such field at all, and adding
one fails validation rather than leaking.

Known gap, and a significant one. Separate models make an accidental leak hard,
but the design calls for something stronger: a visibility policy sitting
between the shared state and both agents, so that neither can read the store
directly and each receives only what the policy permits. These models are a
convention enforced at serialisation time. They are not that policy, and they
would not stop a future route that reads a store and renders whatever it likes.
"""

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

    A parent gets a periodic summary by design. The narrower shape is the
    point: it is what keeps a collaborator's view from becoming surveillance.
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
