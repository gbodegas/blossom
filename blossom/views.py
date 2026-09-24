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

from collections.abc import Mapping, Sequence
from datetime import date

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from blossom.agent.steps import StepRecord, describe_outcome
from blossom.assignment_status import HistoryRow
from blossom.captures import Capture
from blossom.drafts import Decision, DraftStatus
from blossom.hand_in import HandInProjection
from blossom.intake import spoken_report
from blossom.reconciliation import CHANNEL_NAMES, SourceConfidence
from blossom.school_instructions import InstructionsStanding
from blossom.stores.drafts import DraftRecord, RunRecord
from blossom.stores.help_requests import HelpState
from blossom.stores.project_state import Assignment, AssignmentKind, NoteBy, StatusReport


class SchoolWordsView(BaseModel):
    """What the school said about one assignment and what anyone of the family wrote on it,
    kept apart, as every page that lists the assignment shows them.

    ``current`` are the school's instructions that apply, in the one order;
    ``earlier`` those said before, and ``awaiting`` those waiting for a
    parent's review, in the order kept; ``unreadable`` says they cannot be
    read, and then none is shown. ``note`` is her note or a parent's, with
    ``note_by``; a school note left in the old note field is shown apart, as
    not yet reviewed.
    """

    model_config = ConfigDict(extra="forbid")

    current: list[str] = []
    earlier: list[str] = []
    awaiting: list[str] = []
    unreadable: bool = False
    note: str | None = None
    note_by: NoteBy | None = None

    @property
    def any_instruction(self) -> bool:
        """Whether there is anything of the school's to say, that none can be read included."""
        return bool(self.current or self.earlier or self.awaiting or self.unreadable)


def school_words(
    item: Assignment, standing: InstructionsStanding | None, unreadable: bool
) -> SchoolWordsView:
    """The school's words and anyone's note on one assignment, from one reading."""
    shown = None if unreadable else standing
    return SchoolWordsView(
        current=[] if shown is None else list(shown.texts),
        earlier=[] if shown is None else [row.text for row in shown.history],
        awaiting=[] if shown is None else [row.text for row in shown.awaiting],
        unreadable=unreadable,
        note=item.note or None,
        note_by=item.note_by if item.note else None,
    )


class SchoolStatementView(BaseModel):
    """What one school channel reported about an assignment's status, with its day and where
    the day came from: a fact of the school's, shown as the school's on every page."""

    model_config = ConfigDict(extra="forbid")

    channel: str
    source: str
    """The channel as a person reads it, "school email" or "school portal"."""
    status: str
    reported_on: date
    sentence: str
    """Where and when it was reported, with how the day is known, as the pages say it."""

    @classmethod
    def from_report(cls, report: StatusReport) -> "SchoolStatementView":
        """One report of the school's as the pages show it."""
        return cls(
            channel=report.channel.value,
            source=CHANNEL_NAMES[report.channel],
            status=report.status,
            reported_on=report.reported_on,
            sentence=spoken_report(report),
        )


class UpdateHistoryRowView(BaseModel):
    """One event of hers as the history lists it: an update, or a correction of one."""

    model_config = ConfigDict(extra="forbid")

    operation: str
    """``report`` for an update of hers, ``undo`` for a correction that took one back."""
    status: str | None
    """What stood after the event; ``None`` when a correction left no update standing."""
    note: str | None
    reported_on: date
    """The day the event was accepted: for a correction, the day of the correction."""
    restored_from: date | None = None
    """For a correction that put an update back, that update's own day."""

    @classmethod
    def from_row(cls, row: HistoryRow) -> "UpdateHistoryRowView":
        """One row of her history as the pages show it."""
        return cls(
            operation=row.event.operation,
            status=row.event.status,
            note=row.event.note,
            reported_on=row.event.reported_on,
            restored_from=None if row.restored is None else row.restored.reported_on,
        )


class HandInEventView(BaseModel):
    """One event of her hand-in account, for the history fold."""

    model_config = ConfigDict(extra="forbid")

    on: date
    undo: bool
    """Whether the event took the one before it back; ``state`` is then what it restored."""
    state: str | None
    next_action: str | None = None
    note: str | None = None


class HandInView(BaseModel):
    """What she has said about turning one assignment in, as a page shows it.

    Her own account of delivery, read apart from her account of the work, the
    school's reports, and the family's checks. ``state`` is ``None`` while she
    has said nothing, which is not the same as her saying she is not sure;
    ``unavailable`` means the record of it cannot be read, which is neither.
    """

    model_config = ConfigDict(extra="forbid")

    unavailable: bool = False
    state: str | None = None
    reported_on: date | None = None
    """The day she entered the state standing now; an edit inside it does not move it."""
    next_action: str | None = None
    """The one next step she chose, as kept; ``None`` when she chose none."""
    note: str | None = None
    note_updated_on: date | None = None
    """The day the note shown was written, when that is not the day the state began."""
    head_id: str | None = None
    """The last event in the chain, carried by the form so a save lands on the chain the
    page showed. Never the event that began the state."""
    undo_event_id: str | None = None
    """The report she can take back, when the head is one she made."""
    history: list[HandInEventView] = []

    @classmethod
    def of(cls, reading: HandInProjection | None) -> "HandInView":
        """The view of one reading; ``None`` is a record that cannot be read."""
        if reading is None:
            return cls(unavailable=True)
        return cls(
            state=reading.state,
            reported_on=reading.reported_on,
            next_action=reading.next_action,
            note=reading.note,
            note_updated_on=reading.note_updated_on,
            head_id=reading.head_id,
            undo_event_id=reading.undo_event_id,
            history=[
                HandInEventView(
                    on=row.event.reported_on,
                    undo=row.event.operation == "undo",
                    state=row.event.state,
                    next_action=row.event.next_action,
                    note=row.event.note,
                )
                for row in reading.history
            ],
        )


class ToTurnInRowView(BaseModel):
    """One assignment on her To turn in list: what it is, what she said, and what the
    school says now, read apart. ``hand_in`` carries the head the row's one press sends
    back, so the press lands on the chain the row showed."""

    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    course: str
    title: str
    hand_in: HandInView
    missing: list[SchoolStatementView] = []
    """Each school channel whose current word is missing, with its day: shown beside her
    report, never in place of it."""


class HandInRowView(BaseModel):
    """One assignment in the family page's section on turning work in."""

    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    course: str
    title: str
    hand_in: HandInView


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
    confirming_channels: list[str] = []
    """Channels whose value reads as the record's date, once each. Empty when the
    record has no date or nothing readable matches it, so a source is named as
    the origin of the date shown only when it gave that date."""
    confirming: str = ""
    """The confirming channels in the page's words."""
    readable_channels: list[str] = []
    """Channels that gave a value which reads as a date, once each; the only
    ones that can agree, disagree, or contradict."""
    readable_sources: str = ""
    """The readable channels in the page's words."""
    unreadable: list[str] = []
    """Claims whose value could not be read as a date, as ``SourceRecord.spoken``
    renders them, with channels named as she reads them. Kept apart: they neither
    confirm nor contradict anything, and the page says so."""
    claims_unreadable: bool = False
    """Whether a claim about the date is held as nothing the store writes: neither evidence
    nor its absence. The page says so, and the date is read without it."""
    unreadable_sources: str = ""
    """The channels behind ``unreadable``, in the page's words."""
    source_label: str = ""
    """Where the date shown came from, when one short label can say it: the channels
    that gave it, or the family. Empty in the states that need a sentence."""
    source_claims: list[str] = []
    """Every claim a source has made about the date, as ``SourceRecord.spoken`` renders
    it, in the order heard and each once, whether it agrees with the record, differs, or
    cannot be read as a date. An assignment's details list them all as the evidence; a
    card lists claims only when the date is in doubt, from the fields below."""
    disagreement: list[str]
    """Each claim when the sources give different dates, as ``SourceRecord.spoken``
    renders it; empty otherwise."""
    contradiction: list[str] = []
    """What the sources say when none of them supports the record's date, in the same
    spoken form; empty otherwise."""
    school_contradicts: bool = False
    """Whether a school channel is among those; only then is the contradiction a banner."""
    assigned_on: date | None = None
    note: str | None = None
    """What the teacher wrote under the card, as the portal shows it, or what a parent
    typed, or what she wrote on work she added; ``note_by`` says whose words it is."""
    note_by: NoteBy | None = None
    words: SchoolWordsView = Field(default_factory=SchoolWordsView)
    """The school's instructions, those that apply and the rest, and the note, apart."""
    entered_by_a_parent: bool = False
    """Whether the assignment itself came from a parent's entry rather than the school."""
    school_statements: list[SchoolStatementView] = []
    """What each school channel says now, the latest day first: every current statement,
    not one latest report, so a check never rests on something the card does not show."""
    school_history: list[SchoolStatementView] = []
    """Every report the school has made about it, oldest first, for the history fold."""
    update_history: list[UpdateHistoryRowView] = []
    """Every update and correction of hers, oldest first, for the history fold."""
    update_status: str | None = None
    """What she has reported about her part, ``done`` or ``not_yet``; ``None`` while no
    report of hers stands. Hers, read apart from the school's, and never merged with it."""
    update_note: str | None = None
    update_reported_on: date | None = None
    """The day of the report whose words stand."""
    update_restored_on: date | None = None
    """The day an undo restored that report, when one did."""
    update_head_id: str | None = None
    """The last event in her chain, carried by the form so a save lands on the chain the
    page showed; ``None`` when she has said nothing yet."""
    undo_report_id: str | None = None
    """The report she can take back, when the head is one she made."""
    hand_in: HandInView = HandInView()
    """What she has said about turning it in: a second account of hers, which changes
    nothing about her update and is changed by nothing here."""
    in_planning_window: bool = False
    """Whether the assignment is in today's planning window, which is what a "not yet"
    means for the next plan."""
    check_school: bool = False
    """Whether her "done" stands beside a school report of missing: something for the
    family to check, said on both pages and decided by neither."""
    checked_on: date | None = None
    """The day a parent marked that check as made with her, while the check stands against
    the facts as they are; ``None`` otherwise. The family's own record, changing nothing."""
    check_note: str | None = None
    """A parent's words with that check, if any."""


class AssignmentUpdateView(BaseModel):
    """One assignment with what she and the school have reported, as the family page lists it.

    The two accounts sit side by side, each with its day and its source, and
    the page decides nothing between them: a "done" of hers beside a
    "missing" of the school's is something to check, not a verdict on either.
    """

    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    course: str
    title: str
    status: str | None = None
    """Her standing report, ``done`` or ``not_yet``; ``None`` when the school alone has spoken."""
    reported_on: date | None = None
    restored_on: date | None = None
    cleared_on: date | None = None
    """The day her latest event took back her only update, leaving none standing: a
    correction the page lists as recent activity, saying that no update stands."""
    note: str | None = None
    school_statements: list[SchoolStatementView] = []
    """What each school channel says now, the latest day first; the check rests on the
    ones that say missing, and every one is shown."""
    check: bool = False
    """Whether her "done" stands beside a school "missing"."""
    basis: str | None = None
    """What a check of the row is made against, carried by the form so a check lands on the
    facts the page showed; ``None`` while there is nothing to check."""
    check_head_id: str | None = None
    """The last check event under the assignment, carried by the form so a check lands on
    the record the page showed; ``None`` when there is none."""
    words: SchoolWordsView = Field(default_factory=SchoolWordsView)
    """The school's instructions for it and anyone's note on it, apart, as her pages show
    them."""
    checked: bool = False
    """Whether a check stands against the facts as they are."""
    checked_on: date | None = None
    check_note: str | None = None
    check_id: str | None = None
    """The last check a parent marked, standing or not, which Check again reopens; ``None``
    when there is none or it was reopened."""
    checked_before_on: date | None = None
    """The day of a check that is in the record against facts that differ now. The row says
    so whichever group it is in, with what differs, so a check made is never out of sight
    because her update or the school's report moved."""
    checked_before_note: str | None = None
    """A parent's words with that check, if any."""
    differs: list[str] = []
    """What differs from the facts that check was made against, each as a few words."""
    new_missing: bool = False
    """Whether the school's statements of missing are not the ones that check was made
    against: a report the school had not made then, or a day from a fresh paste."""
    hand_in: HandInView | None = None
    """What she has said about turning it in, shown as context on a row worth checking
    together; ``None`` on every other row. It decides nothing about the check."""


class AssignmentUpdatesView(BaseModel):
    """The family page's section on assignment updates, in its four groups."""

    model_config = ConfigDict(extra="forbid")

    check: list[AssignmentUpdateView] = []
    """Her "done" beside a school "missing" with no check standing: worth checking
    together, shown open, with the form to mark it checked."""
    checked: list[AssignmentUpdateView] = []
    """The rows a parent marked checked in the last fourteen household days, the latest
    check first, while the check stands; folded, each with Check again."""
    recent: list[AssignmentUpdateView] = []
    """Her standing reports of the last fourteen household days, most recent first."""
    school: list[AssignmentUpdateView] = []
    """Every other assignment the school has reported on, with its latest report."""
    turning_in: list[HandInRowView] = []
    """Turning work in: everything she reports as still to turn in, the longest
    standing first and however old, then whatever else she has said about delivery
    in the last fourteen household days, and any assignment whose hand-in record
    cannot be read. A section of its own, with no form in it."""


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


class HelpNoteView(BaseModel):
    """The homework note a request for help is about, as it stands when the page is read.

    A request carries the note's id and none of its words, so what is shown
    is the note now, which she may have edited or put away since she asked,
    and the page says so. ``unavailable`` means the note cannot be read or is
    not on record; the request is shown all the same.
    """

    model_config = ConfigDict(extra="forbid")

    capture_id: str | None
    """The note's id, or ``None`` when the request is about a note and its reference cannot
    be read: the note is then unavailable, and nothing of what was held is given out."""
    text: str | None = None
    archived: bool = False
    unavailable: bool = False

    @classmethod
    def about(
        cls,
        capture_id: str | None,
        notes: Mapping[str, Capture],
        *,
        unreadable_reference: bool = False,
    ) -> "HelpNoteView | None":
        """The note a request names, out of the notes read for it in one batch; ``None`` for
        a request about no note. One the batch does not hold is unavailable, so whoever
        shows a request reads the notes first: there is no way to say they were not read. A
        request whose reference cannot be read is about a note all the same, and says so."""
        if unreadable_reference:
            return cls(capture_id=None, unavailable=True)
        if capture_id is None:
            return None
        note = notes.get(capture_id)
        if note is None:
            return cls(capture_id=capture_id, unavailable=True)
        return cls(capture_id=note.capture_id, text=note.text, archived=note.archived)


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
    about_note: HelpNoteView | None = None
    """The homework note the request is about, when it is about one."""


class NamedAssignmentView(BaseModel):
    """One assignment a notice names: its id, which is what tells it apart, and its title."""

    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    title: str


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
    reported_done: str | None = None
    """In her words, that the plan includes work she reports as done as things stand;
    ``None`` while it includes none. Nothing here says which came first, the plan or the
    report, since a report can land while a plan is being made."""
    reported_done_work: list[NamedAssignmentView] = []
    """That work, each by id and title, when the plan carries the ids it speaks about."""


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
    and take any of it back. ``viewer`` is who is at the keyboard as the gate
    says, and ``can_update`` whether the cards offer her update: to her, or to
    anyone while the sign-in is off, and never to a parent. ``nothing_to_plan``
    is whether today's planning window holds no work still to do, in which
    case the page says so and offers no plan button.
    """

    model_config = ConfigDict(extra="forbid")

    generated_at: AwareDatetime
    today: date
    """The household's date, which the page's today panel is about."""
    week: WeekView
    assignments: list[StudentAssignmentView]
    assigned_this_week: list[StudentAssignmentView] = []
    plan_horizon_end: date
    full_budget_minutes: int
    budget_minutes: int
    plan: StudentPlanView | None = None
    to_turn_in: list[ToTurnInRowView] = []
    """Everything she reports as still to turn in, whatever the week shown, in the order
    she took each on. Her own list, which no plan is drawn from."""
    to_turn_in_unreadable: list[ToTurnInRowView] = []
    """The assignments whose hand-in record cannot be read, named under the list."""
    can_plan: bool = False
    too_much: WorkloadSignalView | None = None
    signals: list[WorkloadSignalView] = []
    help_requests: list[HelpRequestView] = []
    """Her requests for help still open, oldest first, then those resolved within
    two weeks, most recent first, so she sees each step a parent takes."""
    viewer: str = "anyone"
    can_update: bool = True
    nothing_to_plan: bool = False
    apart: StudentAssignmentView | None = None
    """The assignment a save, an undo, or a link named, when it is on record and outside the
    week shown, its dates having changed: shown apart, so the result and anything she
    typed are never lost to a week the card has left."""


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
    reported_done: str | None = None
    """That today's plan includes work she reports as done as things stand; ``None`` for
    any other plan, and while it includes none."""
    reported_done_work: list[NamedAssignmentView] = []
    """That work, each by id and title, when the plan carries the ids it speaks about."""

    @classmethod
    def from_record(
        cls,
        record: DraftRecord,
        stale: str | None = None,
        reported_done: str | None = None,
        reported_done_work: Sequence[NamedAssignmentView] = (),
    ) -> "ApprovalView":
        """The parent's projection of a table row. The thread id stays out of it."""
        return cls(
            stale=stale,
            reported_done=reported_done,
            reported_done_work=list(reported_done_work),
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
