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

from blossom.agent.steps import StepRecord, describe_outcome
from blossom.drafts import Decision, DraftStatus
from blossom.reconciliation import SourceConfidence
from blossom.stores.drafts import DraftRecord, RunRecord
from blossom.stores.help_requests import HelpState
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
    """Each channel that spoke about the date, once, in the order first heard."""
    sources: str = ""
    """The same channels in the words her page uses for them, joined for a sentence."""
    disagreement: list[str]
    contradiction: list[str] = []
    """What the sources say when none of them supports the record's date; empty otherwise."""
    assigned_on: date | None = None


class WorkloadSignalView(BaseModel):
    """One press of her control as she sees it: which evening, when, and her words.

    This is the whole of what the store keeps about a press. What she can see
    on her page and what is kept are the same thing.
    """

    model_config = ConfigDict(extra="forbid")

    signal_id: str
    evening: date
    given_at: AwareDatetime
    given_local: AwareDatetime
    detail: str | None = None
    """Words she chose to add, exactly as kept; ``None`` when she pressed and said nothing."""


class HelpRequestView(BaseModel):
    """One request for help as both pages see it: her words, where it stands, the word back.

    The same view serves her page and the parent's: what a parent does with a
    request is shown to her in full, and nothing is kept about a request that
    either of them cannot see.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    evening: date
    asked_at: AwareDatetime
    asked_local: AwareDatetime
    note: str | None = None
    state: HelpState
    accepted_at: AwareDatetime | None = None
    resolved_at: AwareDatetime | None = None
    response: str | None = None


class StudentPlanView(BaseModel):
    """Today's plan as she sees it: the text, when it was made, and what a parent said.

    The plan is hers from the moment it is made. ``decision`` is a parent's
    review of it, ``None`` until one is given, and never a condition on her
    using the plan. ``stale`` says, in her words, why the plan has stopped fitting
    the evening and should be made again.
    """

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    plan_date: date
    body: str
    made_at: AwareDatetime
    made_local: AwareDatetime
    outcome: str
    too_much: bool
    """Whether the plan was made for a reduced evening, after she said today was too much."""
    decision: Decision | None = None
    reason: str | None = None
    stale: str | None = None


class WeekView(BaseModel):
    """The school week her page frames, Monday to Sunday, and the ones either side."""

    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    current: bool
    """Whether this is the week that holds today."""
    previous: date
    following: date


class StudentDueThisWeekView(BaseModel):
    """Her week. Every assignment in the window appears, nothing is filtered out.

    ``week`` is the school week shown, the one that holds today unless she has
    moved to another. ``assigned_this_week`` is work given out in that week
    and due after it, listed so the week reads the way the school's does.
    ``plan_horizon_end`` is the last day a plan made today looks at, stated on
    the page because it is not the week's end. ``plan`` is today's latest plan
    when one has been made. ``can_plan`` is whether a new one can be asked
    for, which needs a model. ``too_much`` is tonight's signal when she has
    given one, and ``budget_minutes`` is what the next plan is held to as a
    result. ``signals`` is everything the store still keeps, so she can see it
    and take any of it back.
    """

    model_config = ConfigDict(extra="forbid")

    generated_at: AwareDatetime
    week: WeekView
    assignments: list[StudentAssignmentView]
    assigned_this_week: list[StudentAssignmentView] = []
    plan_horizon_end: date
    full_budget_minutes: int
    budget_minutes: int
    plan: StudentPlanView | None = None
    can_plan: bool = False
    too_much: WorkloadSignalView | None = None
    signals: list[WorkloadSignalView] = []
    help_requests: list[HelpRequestView] = []
    """Her requests for help still open, oldest first, then those resolved within
    two weeks, most recent first, so she sees each step a parent takes."""


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
    steps: list[StepRecord] = []
    """What each node expected and found, in order, so the run explains itself."""


class RunView(BaseModel):
    """A run that ended before the gate, for the parent who would have decided.

    ``summary`` says in one sentence why there is nothing to approve, and the
    steps say what happened on the way.
    """

    model_config = ConfigDict(extra="forbid")

    plan_date: date
    outcome: str
    summary: str
    recorded_at: AwareDatetime
    steps: list[StepRecord]

    @classmethod
    def from_record(cls, record: RunRecord) -> "RunView":
        """The parent's projection of a run row. The thread id stays out of it."""
        return cls(
            plan_date=record.plan_date,
            outcome=record.outcome,
            summary=describe_outcome(record.outcome),
            recorded_at=record.recorded_at,
            steps=record.steps,
        )


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
    steps: list[StepRecord] = []
    """How the plan was made, read from the same snapshot as the draft."""
    stale: str | None = None
    """Why this draft is not approved as it stands, when her signal has changed
    since it was made; ``None`` while the draft still fits the evening."""

    @classmethod
    def from_record(cls, record: DraftRecord, stale: str | None = None) -> "ApprovalView":
        """The parent's projection of a table row. The thread id stays out of it."""
        return cls(
            stale=stale,
            steps=record.steps,
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
