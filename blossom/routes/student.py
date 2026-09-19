"""Student routes. She is the primary user, and this is the primary view.

Nothing is filtered out of her week: an assignment the system cannot corroborate
is the one she most needs to see, so every assignment in the window reaches the
page with a line saying where its date came from. Only a disagreement between
sources, or a school date the record does not match, is made prominent; the
rest is said quietly, so a warning on this page means something.

The week is the school week, Monday to Sunday, the way the school's own page
frames it, and she can move to the weeks either side. The planner looks at
the seven days from the evening it plans instead, and her page says through
which day. Work assigned in the week and due after it is listed under the
week's cards. The window is read the way the plan graph reads it, so the two
never differ about whether an item is in a week, and an assignment whose
record date the school's sources contradict says so on her page.

Today's plan is hers. She asks for it from this page, it appears here the
moment it is made, and a parent's review of it shows under it afterwards
without ever being a condition on it. Making a plan asks the model, which
takes a minute or two and needs a key; without one the page says so and
everything else on it still works.

The workload signal takes no argument: rating or describing the load requires
stepping back, and that capacity is least available exactly when the signal
matters. One press records that today is too much, the page shows it at once
with what it changes, tonight's plan is held to a reduced budget, and she can
take it back. The page also lists every signal still kept, each with a way to
remove it, because the record is hers.

Asking for help is the other press on her page. It is addressed to a person,
so it has a state a parent moves, requested to accepted to resolved, and each
step shows here in plain words: nothing says a parent is on it before that
parent has said so. A sentence may go with it and is never asked for. She can
take a request back while nobody has taken it up.

Her update on an assignment is the third thing her page takes from her: Done,
meaning she has finished her part, or Not yet, with a note if she wants one.
It is her account, kept apart from the school's and from the record's dates,
and it decides one thing, whether the assignment is still work to plan. A
card with a standing update shows it with its day, what it means for the next
plan, and a way to change or take it back; work reported done folds under
the active cards rather than leaving the week. The form is a form alone, so
it works without a script, and a save that lands on a card another device
has since changed is shown that change and asked to look again. A parent
signed in sees her update and cannot make one in her name.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import Annotated, Final, cast

from fastapi import (
    APIRouter,
    Body,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from blossom.anthropic_client import model_configured
from blossom.assignment_status import AssignmentStatus, statuses_for
from blossom.clock import local_now
from blossom.dependencies import ApplicationState, get_application_state
from blossom.evening import PlanUpdates, ReportedDone, Staleness, plan_updates, staleness
from blossom.hand_in import (
    HAND_IN_NOTE_MAX_LENGTH,
    NEXT_ACTION_MAX_LENGTH,
    BrokenChain,
    project,
)
from blossom.noticing import (
    Everything,
    Noticing,
    expect_due_date,
    in_week,
    monday_of,
    notice_due_date,
    read_date,
    read_everything,
    reconcile_dates,
    week_from,
)
from blossom.plan_reading import DoneMark, PlanReading, Reader, anchor_for, read_plan
from blossom.principals import Principal
from blossom.reconciliation import (
    CHANNEL_NAMES,
    SCHOOL_CHANNELS,
    Disagreement,
    SourceChannel,
    SourceConfidence,
    SourceRecord,
    classify_confidence,
)
from blossom.routes.forms import TOKEN_MAX_LENGTH, fields_of
from blossom.routes.navigation import (
    FAMILY_PAGE,
    WEEK_PAGE,
    ReturnTo,
    address,
    details_href,
    read_return,
    result_anchor,
    safe_default,
    segment,
    todays_plan_href,
    week_href,
)
from blossom.routes.runs import (
    Graphs,
    ended_without_a_plan,
    no_plan_made,
    require_model,
    require_work,
    run_plan,
)
from blossom.settings import CALENDAR_MARGIN
from blossom.stores.drafts import DraftRecord
from blossom.stores.help_requests import NOTE_MAX_LENGTH, HelpRequest, RequestClosed
from blossom.stores.project_state import (
    DONE,
    DUE_THIS_WEEK_SPAN,
    NOT_YET,
    UNDO,
    AlreadySaved,
    Assignment,
    Conflict,
    CouldNotSave,
    NoteTooLong,
    Saved,
    StudentStatus,
    Undone,
    UnknownAssignment,
    UnknownReport,
    normalize_note,
)
from blossom.stores.project_state import NOTE_MAX_LENGTH as UPDATE_NOTE_MAX_LENGTH
from blossom.stores.workload_signals import DETAIL_MAX_LENGTH, WorkloadSignal
from blossom.templating import page_templates
from blossom.views import (
    HandInView,
    HelpRequestView,
    NamedAssignmentView,
    SchoolStatementView,
    StudentAssignmentView,
    StudentDueThisWeekView,
    StudentPlanView,
    UpdateHistoryRowView,
    WeekView,
    WorkloadSignalView,
)

logger = logging.getLogger(__name__)

PAGE: Final = "/student/due-this-week"
A_WEEK: Final = timedelta(days=7)

# The words her page uses for each channel a date can come from. The page
# speaks to her, so her own report is "what you reported".
CHANNEL_WORDS: Final[dict[str, str]] = {
    SourceChannel.LMS: "the school portal",
    SourceChannel.EMAIL: "an email from the school",
    SourceChannel.PARENT_ENTRY: "what a parent entered",
    SourceChannel.STUDENT_REPORT: "what you reported",
}
NOT_A_WEEK: Final = "That is not a date, so this is the week that holds today."
BEYOND_THE_CALENDAR: Final = (
    "That week is past the edge of the calendar, so this is the week that holds today."
)

SIGNALED_SINCE: Final = (
    "You have said today is too much, and this plan was made for the full evening. "
    "Your current plan has not changed yet. Make a smaller plan when you are ready."
)
SIGNAL_ENDED: Final = (
    "This plan was kept to the smaller evening for a signal that is not there now. "
    "It stays until a new one is made; plan again for the full evening."
)
ASSIGNMENTS_CHANGED: Final = (
    "Your assignments or updates differ from the work this plan used, so it does not cover "
    "your week as it stands. It stays until a new one is made; plan again when you are "
    "ready."
)
PLAN_INCLUDES_DONE: Final = "This plan includes work you now report as Done."
PLAN_WINDOW_DONE: Final = "Some work in this plan's window is now reported Done."
"""The notice for a plan from before plans carried their ids: a fact about its window and
no more. What to do about it, when anything can be done, is the stale warning's to say."""
UPDATE_SAVED: Final = "Your update is saved."
UPDATE_ALREADY_SAVED: Final = "Your update is already saved."
UPDATE_UNDONE: Final = "Your update is undone."
CHOOSE_ONE: Final = "Choose Done or Not yet."
NOTE_TOO_LONG: Final = f"Keep your note to {UPDATE_NOTE_MAX_LENGTH} characters or fewer."
SAVED_ELSEWHERE: Final = "An update was saved on another device. Review it before saving yours."
NOT_HERS_TO_UPDATE: Final = "Sign in as the student to update."
NOT_ON_RECORD: Final = "That assignment is not on record, so nothing was changed."
NOT_THIS_CARDS: Final = (
    "That form names an update this assignment does not have, so nothing was saved. The card "
    "shows what stands; choose and save from here."
)
BAD_FORM: Final = (
    "That form carried a field twice, or one this page does not send, so nothing was saved. "
    "Choose and save again."
)
CANNOT_UNDO: Final = (
    "Your update has changed, so it cannot be undone from that page. The card shows what stands."
)
ALREADY_UNDONE: Final = "That update was already undone. The card shows what stands now."
"""Said for an Undo of the very update that the latest event already took back: a repeat,
refused like any other stale Undo and writing nothing, told apart from a change by the
head the refusing save read and never by comparing words."""
NOT_SAVED: Final = "Your update could not be saved. Your words are still here. Try again."
NOT_UNDONE: Final = "Your update could not be undone, and nothing was changed. Try again."
FROM_DETAILS: Final = frozenset({"report_view", "return_to", "plan_id"})
"""The fields a form on an assignment's details sends beside the rest: that the result is
to be shown there, and where its reader came from. A card on her week sends none of them,
and is as whole without them as it always was."""
REPORT_FIELDS: Final = frozenset({"status", "note", "expected_report_id", "week"}) | FROM_DETAILS
UNDO_FIELDS: Final = frozenset({"report_id", "week"}) | FROM_DETAILS
"""The fields each form sends, each once. Anything else, anything twice, or a form with one
of them left out, is refused."""
NOTHING_CHOSEN: Final = frozenset({"status"})
"""The field her browser leaves out when neither Done nor Not yet is chosen: two radio
buttons with none checked send nothing. The card then asks her to choose one."""
BAD_RETURN: Final = (
    "The form named a page to go back to that these pages do not make. Nothing was saved."
)
GONE: Final = "This assignment is not on record now."
NO_PLAN_NOW: Final = "No plan is saved for today now."
TURNING_IT_IN: Final = "turning-it-in"
"""The id of the section on an assignment's details that holds her hand-in account."""
HAND_IN_SAVED: Final = "Your hand-in update is saved."
HAND_IN_ALREADY_SAVED: Final = "Already saved."
HAND_IN_UNDONE: Final = "Your hand-in update is undone."
HAND_IN_CONFIRMATIONS: Final[dict[str, str]] = {
    "saved": HAND_IN_SAVED,
    "same": HAND_IN_ALREADY_SAVED,
    "undone": HAND_IN_UNDONE,
}
"""What the address says a hand-in save or undo did, chosen by the server as her
update's are; what stands is shown beside it, with its day."""
CONFIRMATIONS: Final[dict[str, str]] = {
    "saved": UPDATE_SAVED,
    "same": UPDATE_ALREADY_SAVED,
    "undone": UPDATE_UNDONE,
}
"""What the address says happened to a card's update, and the sentence the card shows for
it: the server chooses which, the address only carries the choice."""


class Unread(Enum):
    """The one value that says a caller has not read today's plan, told from having read it
    and found none."""

    UNREAD = "unread"


UNREAD: Final = Unread.UNREAD


router = APIRouter(prefix="/student", tags=["student"])
templates = page_templates()

State = Annotated[ApplicationState, Depends(get_application_state)]


class WorkloadSignalRequest(BaseModel):
    """Optional detail attached to a signal. The signal itself needs no body.

    The words are capped at the boundary, so a request cannot grow the drafts
    file or every later page by sending more than a sentence or two.
    """

    model_config = ConfigDict(extra="forbid")

    detail: str | None = Field(default=None, max_length=DETAIL_MAX_LENGTH)


class WorkloadSignalResponse(BaseModel):
    """What one press did: the signal exactly as kept, words included."""

    model_config = ConfigDict(extra="forbid")

    principal: Principal
    signal: WorkloadSignalView


def signal_view(state: ApplicationState, signal: WorkloadSignal) -> WorkloadSignalView:
    """Her projection of a signal, with the time in the household's zone.

    Everything the store holds about a signal is in it, her words included, so
    what she can see is what is kept.
    """
    return WorkloadSignalView(
        signal_id=signal.signal_id,
        evening=signal.evening,
        given_at=signal.given_at,
        given_local=signal.given_at.astimezone(state.clock.zone),
        detail=signal.detail,
    )


async def record_signal(state: ApplicationState, detail: str | None) -> WorkloadSignal:
    """Keep one press, about today by the household's clock.

    Written under the decision lock: a decision is checked against the evening
    as signaled, and the evening must not change between that check and the
    decision landing. A press during a decision waits the moment it takes.
    """
    async with state.decision_lock:
        return state.workload_signals.record(state.clock.today(), detail)


async def withdraw_signal(state: ApplicationState, signal_id: str) -> bool:
    """Take one signal back, under the same lock and for the same reason."""
    async with state.decision_lock:
        return state.workload_signals.withdraw(signal_id)


@router.post("/workload-signals", status_code=status.HTTP_201_CREATED)
async def register_workload_signal(
    state: State,
    payload: Annotated[WorkloadSignalRequest | None, Body()] = None,
) -> WorkloadSignalResponse:
    """Record that today is too much. ``payload`` is optional so an empty POST works."""
    detail = None if payload is None else payload.detail
    signal = await record_signal(state, detail)
    return WorkloadSignalResponse(principal=Principal.STUDENT, signal=signal_view(state, signal))


@router.get("/workload-signals")
def held_workload_signals(state: State) -> list[WorkloadSignalView]:
    """Every signal still kept, most recent first: what she can see and take back."""
    return [signal_view(state, signal) for signal in state.workload_signals.held()]


@router.delete("/workload-signals/{signal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def withdraw_workload_signal(signal_id: str, state: State) -> Response:
    """Take a signal back. It is gone, not marked; the record is hers to remove."""
    if not await withdraw_signal(state, signal_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no signal {signal_id!r}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class HelpRequestBody(BaseModel):
    """Optional words with a request for help. The request itself needs no body."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)


class HelpRequestResponse(BaseModel):
    """What asking did: the request exactly as kept."""

    model_config = ConfigDict(extra="forbid")

    principal: Principal
    request: HelpRequestView


def help_view(state: ApplicationState, request: HelpRequest) -> HelpRequestView:
    """The request as both pages see it, with the time she asked in the household's zone."""
    return HelpRequestView(
        request_id=request.request_id,
        evening=request.evening,
        asked_at=request.asked_at,
        asked_local=request.asked_at.astimezone(state.clock.zone),
        note=request.note,
        state=request.state,
        accepted_at=request.accepted_at,
        resolved_at=request.resolved_at,
        response=request.response,
    )


def help_requests_shown(state: ApplicationState) -> list[HelpRequestView]:
    """What she sees: open requests oldest first, then those resolved within two weeks."""
    return [
        help_view(state, request)
        for request in [
            *state.help_requests.open_requests(),
            *state.help_requests.recently_resolved(),
        ]
    ]


@router.post("/help-requests", status_code=status.HTTP_201_CREATED)
def ask_for_help(
    state: State, payload: Annotated[HelpRequestBody | None, Body()] = None
) -> HelpRequestResponse:
    """Ask for help today. ``payload`` is optional so an empty POST works."""
    note = None if payload is None else payload.note
    request = state.help_requests.ask(state.clock.today(), note)
    return HelpRequestResponse(principal=Principal.STUDENT, request=help_view(state, request))


@router.get("/help-requests")
def her_help_requests(state: State) -> list[HelpRequestView]:
    """Her requests as she sees them: open ones, then those resolved within two weeks."""
    return help_requests_shown(state)


@router.delete("/help-requests/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
def take_back_help(request_id: str, state: State) -> Response:
    """Take a request back while nobody has taken it up; 409 once a parent has."""
    try:
        removed = state.help_requests.take_back(request_id)
    except RequestClosed as error:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no help request {request_id!r}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def done_in(
    record: DraftRecord, found: ReportedDone | None, *, today: date
) -> tuple[str, list[NamedAssignmentView]] | None:
    """In her words, that a plan includes work she reports as done as things stand, and
    which work, or ``None`` while it includes none.

    Said for a plan of today's or a later evening, whatever a parent decided:
    a parent's "looks good" is about the plan as it was. ``found`` is what
    the record says, read once with everything else the page shows about
    the plan. What is compared is the ids the plan carries and her updates
    as they stand, never the time of either, since an update can land while
    a plan is being made. A plan from before plans carried their ids is told
    only that its window holds such work, and named nothing.
    """
    if record.plan_date < today or found is None:
        return None
    if not found.known:
        return PLAN_WINDOW_DONE, []
    return PLAN_INCLUDES_DONE, [
        NamedAssignmentView(assignment_id=name, title=title) for name, title in found.named
    ]


def done_marks(updates: PlanUpdates) -> dict[str, DoneMark]:
    """The current mark for each assignment of a plan she reports as Done as things stand:
    the day of the report whose words stand, and the day an undo put it back when one did.
    The one rule is that the assignment's effective status is Done now; nothing is
    compared with when the plan was made, what a parent checked, or what the school says."""
    return {
        name: DoneMark(reported_on=status.reported_on, restored_on=status.restored_on)
        for name, status in updates.statuses.items()
        if not status.needs_homework and status.reported_on is not None
    }


@dataclass(frozen=True)
class PlanRead:
    """One plan read once for a page: her view of it, and how the page shows it."""

    view: StudentPlanView
    reading: PlanReading


def read_a_plan(
    state: ApplicationState,
    record: DraftRecord,
    *,
    reader: Reader = "student",
    current: bool,
    today: date,
    everything: Everything | None = None,
) -> PlanRead:
    """Her projection of a draft and its reading, from one reading of the record.

    A decided plan is history: it is measured against her signal, as it always
    was, but not against the week, which may well change after a parent has said
    the plan looks good. Work it speaks about that she has since reported done
    is said whatever a parent decided. The notice above the plan, the marks
    beside its rows, and whether the week reads as it did all come from one
    reading of the record, ``everything``, the page's own when it has one and
    read here otherwise: read apart, a report landing between them could leave
    them at odds, and her reports would be read once for each. ``current`` is
    the caller's word that this is today's working plan, the one reading that
    shows marks, and ``today`` is the household day the caller's page read,
    once, so a page rendered across midnight is about one day from its heading
    to its plan.
    """
    stale = None
    store = state.project_state
    with store.exclusively():
        if everything is None:
            everything = read_everything(store, store, also=record.plan_assignment_ids or ())
        updates = plan_updates(store, record, everything=everything)
        included = done_in(record, updates.done, today=today)
        found = staleness(
            state.workload_signals, record, everything=everything if record.waiting else None
        )
    match found:
        case Staleness.SIGNALED_SINCE:
            stale = SIGNALED_SINCE
        case Staleness.SIGNAL_ENDED:
            stale = SIGNAL_ENDED
        case Staleness.ASSIGNMENTS_CHANGED:
            stale = ASSIGNMENTS_CHANGED
        case None:
            stale = None
    view = StudentPlanView(
        draft_id=record.draft_id,
        plan_date=record.plan_date,
        body=record.body,
        made_at=record.created_at,
        made_local=record.created_at.astimezone(state.clock.zone),
        outcome=record.outcome,
        too_much=record.too_much,
        decision=record.decision,
        reason=record.reason,
        stale=stale,
        reported_done=None if included is None else included[0],
        reported_done_work=[] if included is None else included[1],
    )
    reading = read_plan(
        record,
        reader=reader,
        current=current,
        link_for=lambda name: details_href(name, return_to="today"),
        on_record=updates.on_record,
        done=done_marks(updates),
    )
    return PlanRead(view=view, reading=reading)


def plan_view(state: ApplicationState, record: DraftRecord) -> StudentPlanView:
    """Her projection of a draft: the plan, a parent's review if any, and whether it still fits."""
    return read_a_plan(state, record, current=False, today=state.clock.today()).view


def todays_plan_read(
    state: ApplicationState, reader: Reader = "student", *, today: date
) -> PlanRead | None:
    """The latest plan for ``today``, looked up once, with the reading that shows her
    updates beside it; ``None`` when none has been made. Latest is by the published
    order, whatever a parent decided and whenever it was made."""
    record = state.drafts.latest_for(today)
    if record is None:
        return None
    return read_a_plan(state, record, reader=reader, current=True, today=today)


def todays_plan(state: ApplicationState) -> StudentPlanView | None:
    """Today's latest plan, or ``None`` when none has been made."""
    found = todays_plan_read(state, today=state.clock.today())
    return None if found is None else found.view


@router.get("/plans/today", response_model=StudentPlanView)
def plan_for_today(state: State) -> StudentPlanView:
    """Today's plan as she sees it. 404 until one has been made."""
    plan = todays_plan(state)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no plan has been made for today")
    return plan


@router.post("/plans", response_model=StudentPlanView, status_code=status.HTTP_201_CREATED)
async def make_todays_plan(state: State, graphs: Graphs) -> StudentPlanView:
    """Ask for today's plan. It is hers as soon as it is made; a parent's review comes after.

    A run that ends without a plan, because the checks never passed or the
    model did not answer, is a 409 naming the outcome, and her page keeps
    whatever plan it had. An evening with nothing left to plan is a 409 too,
    before any run is written or a model asked for.
    """
    require_work(state, state.clock.today())
    require_model(graphs)
    run = await run_plan(
        graphs.build(),
        state.clock.today(),
        state,
    )
    if run.draft_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=no_plan_made(run.outcome))
    record = state.drafts.get(run.draft_id)
    if record is None:
        msg = f"the run made {run.draft_id!r} but the table has no such draft"
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=msg)
    return plan_view(state, record)


def source_label(
    confidence: SourceConfidence, confirming: Sequence[str], *, unreadable: bool
) -> str:
    """One short label for where the date shown came from, or empty when it takes a sentence.

    The channels that gave the record's date are named, "School portal" or
    "School portal and your report"; a date only the family has is "Entered by
    the family". Disagreement, contradiction, and claims that could not be
    read are said in a line of their own instead.
    """
    if confidence is SourceConfidence.UNVERIFIED:
        return "" if unreadable else "Entered by the family"
    if confidence is SourceConfidence.SOURCES_DISAGREE or not confirming:
        return ""
    names = " and ".join(CHANNEL_NAMES.get(SourceChannel(c), c) for c in confirming)
    return names[0].upper() + names[1:]


def channels_in_words(channels: Sequence[str]) -> str:
    """The channels, in the page's words for them, joined for a sentence."""
    names = [CHANNEL_WORDS.get(channel, channel) for channel in channels]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def assignment_view(
    assignment: Assignment,
    records: Sequence[SourceRecord],
    noticed: Noticing,
    status: AssignmentStatus | None = None,
    *,
    in_planning_window: bool = False,
    hand_in: HandInView | None = None,
) -> StudentAssignmentView:
    """One assignment as she sees it, with where its date came from said once per channel.

    Claims are reconciled as dates, readable ones only, so two channels giving
    the same date in different spellings agree and a weekday name is neither a
    second source nor a conflict. The card shows the record's date, so a
    channel confirms it only by giving a value that reads as that date. A value
    the comparator cannot read is carried apart; it is not the origin of
    anything. ``status`` is what she and the school have reported, read with
    the rows: her update goes on the card as hers, and what each school
    channel says now goes on it as the school's, every channel, so a check of
    her "done" against a "missing" always shows the report it rests on. Every
    claim is carried too, as it was given, for the details to list whole.
    """
    said = status.asserted if status is not None else None
    readable = [record for record in records if read_date(record.asserted_value) is not None]
    unreadable = [record for record in records if read_date(record.asserted_value) is None]
    reconciliation = reconcile_dates(records)
    disagreement = []
    if isinstance(reconciliation, Disagreement):
        disagreement = [claim.spoken() for claim in reconciliation.conflicting_claims]

    def channels_of(claims: Sequence[SourceRecord]) -> list[str]:
        return list(dict.fromkeys(str(claim.channel) for claim in claims))

    channels = channels_of(records)
    readable_channels = channels_of(readable)
    unreadable_channels = channels_of(unreadable)
    confirming = channels_of(
        [
            record
            for record in readable
            if assignment.due_date is not None
            and read_date(record.asserted_value) == assignment.due_date
        ]
    )
    confidence = classify_confidence(reconciliation)
    return StudentAssignmentView(
        assignment_id=assignment.assignment_id,
        course=assignment.course,
        title=assignment.title,
        due_date=assignment.due_date,
        kind=assignment.kind,
        submission_status=assignment.reported_submission_status,
        deadline_confidence=confidence,
        source_label=""
        if noticed.contradicted
        else source_label(confidence, confirming, unreadable=bool(unreadable)),
        source_channels=channels,
        sources=channels_in_words(channels),
        confirming_channels=confirming,
        confirming=channels_in_words(confirming),
        readable_channels=readable_channels,
        readable_sources=channels_in_words(readable_channels),
        unreadable=[record.spoken() for record in unreadable],
        unreadable_sources=channels_in_words(unreadable_channels),
        source_claims=list(dict.fromkeys(record.spoken() for record in records)),
        disagreement=disagreement,
        contradiction=[record.spoken() for record in readable] if noticed.contradicted else [],
        school_contradicts=noticed.contradicted
        and any(record.channel in SCHOOL_CHANNELS for record in readable),
        assigned_on=assignment.assigned_on,
        note=assignment.note,
        note_by_a_parent=assignment.origins.get("note") == SourceChannel.PARENT_ENTRY,
        entered_by_a_parent=assignment.origins.get("record") == SourceChannel.PARENT_ENTRY,
        school_statements=[
            SchoolStatementView.from_report(report)
            for report in (status.school_statements if status is not None else ())
        ],
        school_history=[
            SchoolStatementView.from_report(report)
            for report in (status.school_history if status is not None else ())
        ],
        update_history=[
            UpdateHistoryRowView.from_row(row)
            for row in (status.history_rows if status is not None else ())
        ],
        update_status=None if status is None else status.status,
        update_note=None if status is None else status.note,
        update_reported_on=None if said is None else said.reported_on,
        update_restored_on=None if status is None else status.restored_on,
        update_head_id=None if status is None else status.head_id,
        undo_report_id=None
        if status is None or status.head is None or status.head.operation != "report"
        else status.head.report_id,
        hand_in=HandInView() if hand_in is None else hand_in,
        in_planning_window=in_planning_window,
        check_school=status is not None and status.check_the_school_record,
        checked_on=None if status is None or status.check is None else status.check.checked_on,
        check_note=None if status is None or status.check is None else status.check.note,
    )


def hand_in_of(everything: Everything, assignment_id: str) -> HandInView:
    """What one reading of the record says she has said about turning an assignment in.

    A chain that does not hold is a record that cannot be read, and is shown
    as that, never as nothing recorded."""
    if assignment_id in everything.hand_ins_unavailable:
        return HandInView.of(None)
    reading = everything.hand_ins.get(assignment_id)
    return HandInView() if reading is None else HandInView.of(reading)


def showable(day: date) -> bool:
    """Whether the week holding ``day`` has a week either side of it on the calendar.

    The page links to the weeks before and after, so the first and last weeks
    the date type can hold are refused rather than shown with a link that
    cannot be computed. A pinned clock is held to the same margin by the
    settings, so today's week is always showable.
    """
    start = monday_of(day)
    return date.min + CALENDAR_MARGIN <= start <= date.max - CALENDAR_MARGIN


def assigned_for_later(item: Assignment, frame: WeekView) -> bool:
    """Given out in the week shown and due after it, by the record's own dates.

    Work due before the week is outside the window too, and is not "due
    later"; it belongs to the week it was due in.
    """
    return (
        item.assigned_on is not None
        and frame.start <= item.assigned_on <= frame.end
        and item.due_date is not None
        and item.due_date > frame.end
    )


def week_shown(today: date, chosen: date | None) -> WeekView:
    """The school week holding ``chosen``, or today's when nothing is chosen."""
    start = monday_of(today if chosen is None else chosen)
    return WeekView(
        start=start,
        end=start + DUE_THIS_WEEK_SPAN,
        current=start == monday_of(today),
        previous=start - A_WEEK,
        following=start + A_WEEK,
    )


def build_student_due_this_week_view(
    state: ApplicationState,
    week: date | None = None,
    *,
    viewer: str = "anyone",
    focus: str | None = None,
    plan: StudentPlanView | None | Unread = UNREAD,
    today: date | None = None,
    everything: Everything | None = None,
) -> StudentDueThisWeekView:
    """Assemble the student's weekly view from the stores ``ApplicationState``
    opened at startup; nothing is opened or seeded per request. ``week`` is any
    day in the school week to show; today's week when ``None``. ``plan`` is
    today's plan when the caller has read it already, so a page looks it up
    once; left out, it is read here. ``today`` is the household day the
    caller read for its page; left out, it is read here, once. ``everything``
    is the record as the caller read it for its page; left out, it is read
    here, once, and the week shown, the planning window, and the cards
    beside them all come out of that one reading. ``viewer`` is who is at
    the keyboard as the gate says. ``focus`` is the assignment a save, an
    undo, or a link named: when it is on record and in neither list of the
    week shown, its dates having changed meanwhile, it is built apart so the
    page can still show it.
    """
    today = state.clock.today() if today is None else today
    frame = week_shown(today, week)
    # One snapshot: a saving landing between two reads could otherwise
    # show a card whose status, report, and update disagree.
    found = (
        read_everything(state.project_state, state.project_state)
        if everything is None
        else everything
    )
    shown = week_from(found, frame.start)
    window = week_from(found, today)
    in_frame = {item.assignment_id for item in shown.assignments}
    later = [
        item
        for item in found.assignments
        if item.assignment_id not in in_frame and assigned_for_later(item, frame)
    ]
    listed = in_frame | {item.assignment_id for item in later}
    elsewhere = [
        item
        for item in found.assignments
        if focus is not None and item.assignment_id == focus and focus not in listed
    ]
    in_window = {item.assignment_id for item in window.assignments}

    def beside_view(item: Assignment) -> StudentAssignmentView:
        records = found.records[item.assignment_id]
        return assignment_view(
            item,
            records,
            notice_due_date(expect_due_date(item), records),
            found.statuses.get(item.assignment_id),
            in_planning_window=item.assignment_id in in_window,
            hand_in=hand_in_of(found, item.assignment_id),
        )

    # Never filter here; see the module docstring.
    views = [
        assignment_view(
            item,
            shown.records[item.assignment_id],
            shown.noticings[item.assignment_id],
            shown.statuses.get(item.assignment_id),
            in_planning_window=item.assignment_id in in_window,
            hand_in=hand_in_of(found, item.assignment_id),
        )
        for item in shown.assignments
    ]
    tonight = state.workload_signals.for_evening(today)
    household = state.settings
    return StudentDueThisWeekView(
        generated_at=datetime.now(UTC),
        today=today,
        week=frame,
        assignments=views,
        assigned_this_week=[beside_view(item) for item in later],
        plan_horizon_end=today + DUE_THIS_WEEK_SPAN,
        full_budget_minutes=household.evening_minutes,
        budget_minutes=household.too_much_minutes if tonight else household.evening_minutes,
        plan=todays_plan(state) if isinstance(plan, Unread) else plan,
        can_plan=model_configured(state.settings),
        too_much=signal_view(state, tonight[-1]) if tonight else None,
        signals=[signal_view(state, signal) for signal in state.workload_signals.held()],
        help_requests=help_requests_shown(state),
        viewer=viewer,
        can_update=viewer != "parent",
        nothing_to_plan=not window.active(),
        apart=beside_view(elsewhere[0]) if elsewhere else None,
    )


def viewer_of(request: Request) -> str:
    """Who is at the keyboard: ``student`` or ``parent`` as the gate read the sign-in, or
    ``anyone`` while the sign-in is off and the gate says nothing."""
    role = getattr(request.state, "household", None)
    if role is Principal.STUDENT:
        return "student"
    if role is Principal.PARENT:
        return "parent"
    return "anyone"


@dataclass(frozen=True)
class CardState:
    """What one card shows beyond its record: a confirmation, its form open, or a problem.

    ``said`` is the sentence a save or an undo left for the card; ``change``
    opens the form on a card with a standing update, with ``status`` and
    ``note`` as she had them, so nothing she typed is lost to a refusal; and
    ``problem`` is what the save could not do. ``field`` says which field the
    problem is about, ``status`` or ``note``, so the page can mark that field,
    tie the words to it, and put the cursor there; a problem about neither,
    an update saved elsewhere among them, is said at the top of the page with
    a link to the card. ``saved_elsewhere`` marks the refusal that comes with
    a newer update to look at.
    """

    assignment_id: str
    said: str | None = None
    change: bool = False
    problem: str | None = None
    field: str | None = None
    status: str | None = None
    note: str | None = None
    saved_elsewhere: bool = False


@dataclass(frozen=True)
class HandInCard:
    """What the hand-in section shows beyond its record, as ``CardState`` is for her update.

    ``said`` is the sentence a save or an undo left; ``change`` opens the
    form, with ``state``, ``next_action``, and ``note`` as she had them, so
    nothing she chose or typed is lost to a refusal; ``problem`` is what the
    save could not do, and ``field`` the field it is about. ``unsaved`` marks
    the refusal that shows what stands now above a form still holding what
    she meant to save.
    """

    said: str | None = None
    change: bool = False
    problem: str | None = None
    field: str | None = None
    state: str | None = None
    next_action: str | None = None
    note: str | None = None
    unsaved: bool = False


def student_page(
    request: Request,
    state: ApplicationState,
    *,
    week: date | None = None,
    problem: str | None = None,
    refreshed: bool = False,
    card: CardState | None = None,
    plan_asked: bool = False,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """Render her page. ``problem`` is what an action could not do, said once at the top.

    ``refreshed`` says when the page was last asked for; ``card`` is what one
    card shows beyond its record. Both change how the page is presented and
    nothing else. ``plan_asked`` says the address asked for today's plan, as a
    way back to it does: when the day has no plan, the place the plan would be
    is still there to land on, and says so, which an ordinary visit to a day
    with no plan has no need of. Today's saved plan is unfolded on every visit,
    so finding her next step takes no remembered action. The household day is
    read once, here, and everything on the page is about that day: the heading,
    the week, the planning window, which plan is today's, its notice, and the
    marks beside its rows. The record is read once too, and all of those are
    about that one reading. A card's problem is said at the top too, with a link
    to the card, so it is met on a page that opens at its top; the card named is
    shown even when its dates have taken it out of the week.
    """
    viewer = viewer_of(request)
    today = state.clock.today()
    # The record is read once for the page, with today's plan's assignments
    # named to it: the week, the planning window, the plan's notice and
    # marks, and whether the plan still fits all come out of that reading.
    record = state.drafts.latest_for(today)
    everything = read_everything(
        state.project_state,
        state.project_state,
        also=() if record is None else record.plan_assignment_ids or (),
    )
    todays = (
        None
        if record is None
        else read_a_plan(
            state,
            record,
            reader="family" if viewer == "parent" else "student",
            current=True,
            today=today,
            everything=everything,
        )
    )
    view = build_student_due_this_week_view(
        state,
        week,
        viewer=viewer,
        focus=None if card is None else card.assignment_id,
        plan=None if todays is None else todays.view,
        today=today,
        everything=everything,
    )
    about_a_card = card is not None and card.problem is not None and problem is None
    listed = [*view.assignments, *view.assigned_this_week, *([view.apart] if view.apart else [])]
    return templates.TemplateResponse(
        request,
        "student_due_this_week.html",
        {
            "view": view,
            "report_contexts": {
                item.assignment_id: week_context(item.assignment_id, view.week.start, viewer)
                for item in listed
            },
            "problem": card.problem if card is not None and about_a_card else problem,
            "problem_target": card.assignment_id if card is not None and about_a_card else None,
            "plan_reading": None if todays is None else todays.reading,
            "plan_asked": plan_asked,
            "no_plan_now": NO_PLAN_NOW,
            "refreshed_at": local_now(state.clock.zone) if refreshed else None,
            "note_max_length": NOTE_MAX_LENGTH,
            "update_note_max_length": UPDATE_NOTE_MAX_LENGTH,
            "card": card,
            "sample": state.settings.sample,
        },
        status_code=status_code,
    )


def card_shown(
    saved: str | None, same: str | None, undone: str | None, change: str | None, show: str | None
) -> CardState | None:
    """What the address says about one card, read in a fixed order and one thing at a time."""
    for said, given in (("saved", saved), ("same", same), ("undone", undone)):
        if given:
            return CardState(given, said=CONFIRMATIONS[said])
    if change:
        return CardState(change, change=True)
    if show:
        return CardState(show)
    return None


@router.get("/due-this-week", response_class=HTMLResponse)
def due_this_week(
    request: Request,
    state: State,
    week: Annotated[str | None, Query(description="Any day in the school week to show")] = None,
    show_plan: Annotated[
        str | None, Query(description="1 to show today's plan unfolded; changes nothing else")
    ] = None,
    refreshed: Annotated[
        str | None, Query(description="1 after a refresh, to say when; changes nothing else")
    ] = None,
    saved: Annotated[
        str | None, Query(description="the assignment whose update was just saved; a note")
    ] = None,
    same: Annotated[
        str | None, Query(description="the assignment whose update was saved already; a note")
    ] = None,
    undone: Annotated[
        str | None, Query(description="the assignment whose update was just undone; a note")
    ] = None,
    change: Annotated[
        str | None, Query(description="the assignment whose update form to open; changes nothing")
    ] = None,
    show: Annotated[
        str | None, Query(description="the assignment to bring into view; changes nothing")
    ] = None,
) -> HTMLResponse:
    """Render her week and today's plan.

    The week is the school week that holds today, or the one holding the day
    ``week`` names. A value that is not a date, a blank one included, or a week
    at the edge of the calendar, is said at the top of today's week rather than
    answered with an error page. Only an absent ``week`` means today's week
    without a word. ``show_plan`` changes nothing about a plan, which the page
    shows unfolded on every visit; links to the plan carry it so that following
    one is a fresh page, with the fold open again if she had closed it, and so
    that a day with no plan still has the place the link lands on, saying that
    no plan is saved. A GET never makes a plan. The rest name one card: what a
    save or an undo just did to it, which the server chose and the address only
    carries, or that its form is to be open, or that it is to be in view, with
    the fold around it open.
    """
    was_refreshed = refreshed == "1"
    asked = show_plan == "1"
    card = card_shown(saved, same, undone, change, show)
    if week is None:
        return student_page(request, state, refreshed=was_refreshed, card=card, plan_asked=asked)
    try:
        chosen = date.fromisoformat(week.strip())
    except ValueError:
        return student_page(
            request,
            state,
            problem=NOT_A_WEEK,
            card=card,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if not showable(chosen):
        return student_page(
            request,
            state,
            problem=BEYOND_THE_CALENDAR,
            card=card,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    return student_page(request, state, week=chosen, card=card, plan_asked=asked)


def week_named(given: str) -> date | None:
    """The week a form carried, or ``None`` for today's when it carried none or nonsense."""
    try:
        chosen = date.fromisoformat(given.strip()) if given.strip() else None
    except ValueError:
        return None
    return chosen if chosen is not None and showable(chosen) else None


def back_to_the_card(week: date | None, said: str, assignment_id: str) -> str:
    """Where a save or an undo sends her: the week she was on, the card, and what happened."""
    where = "" if week is None else f"week={week.isoformat()}&"
    return f"{PAGE}?{where}{said}={assignment_id}#assignment-{assignment_id}"


@dataclass(frozen=True)
class ReportContext:
    """What the update component needs beyond the assignment it is about: who may update,
    where its forms and links go, and what rides along with them.

    A card on her week and an assignment's details show the same component
    through this one object, so there is one form, one set of words, and one
    pair of routes, and only the addresses differ. It carries no permission:
    who may save is decided by the routes, from the sign-in, whatever a form
    says about where it came from.
    """

    viewer: str
    can_update: bool
    report_action: str
    undo_action: str
    change_action: str
    """Where the Change form, a GET, goes: the page that opens the editor."""
    change_fields: list[tuple[str, str]]
    cancel_href: str
    """The same page with the saved summary shown and nothing written."""
    post_fields: list[tuple[str, str]]
    """What rides along on a save and an undo: the week, or that the result is to be shown
    on the details with the way back from there."""
    with_year: bool = False
    """Whether days are said with their year, as a page with no week above it needs."""
    back: "ReturnLink | None" = None
    """The way back the page offers at its top, repeated beside what a save or an undo
    did, so the result and the way on are found together. ``None`` on a card, which is
    already where she was."""
    history_apart: bool = False
    """Whether the page shows the update history itself, below the longer evidence,
    instead of under the component."""


def hand_in_actions(assignment_id: str) -> tuple[str, str]:
    """The two routes every hand-in update goes through."""
    base = f"/student/actions/assignments/{segment(assignment_id)}"
    return f"{base}/hand-in", f"{base}/undo-hand-in"


def report_actions(assignment_id: str) -> tuple[str, str]:
    """The two routes every update goes through, wherever its form is shown."""
    base = f"/student/actions/assignments/{segment(assignment_id)}"
    return f"{base}/report", f"{base}/undo-report"


def week_context(assignment_id: str, week: date, viewer: str) -> ReportContext:
    """The component on a card of her week: results come back to the card."""
    report, undo = report_actions(assignment_id)
    return ReportContext(
        viewer=viewer,
        can_update=viewer != "parent",
        report_action=report,
        undo_action=undo,
        change_action=address(WEEK_PAGE, fragment=f"assignment-{segment(assignment_id)}"),
        change_fields=[("week", week.isoformat()), ("change", assignment_id)],
        cancel_href=week_href(week, assignment_id, show=assignment_id),
        post_fields=[("week", week.isoformat())],
    )


def detail_context(
    assignment_id: str, back: ReturnTo, viewer: str, link: "ReturnLink"
) -> ReportContext:
    """The component on an assignment's details: results come back to the details, with the
    way back from there carried along.

    Change is a GET form, and a browser writes a GET form's fields over any
    query in its action, so the way back rides in the form's hidden fields
    beside the flag that opens the editor, and the action is the bare
    address of the details."""
    report, undo = report_actions(assignment_id)
    carried = back.fields()
    return ReportContext(
        viewer=viewer,
        can_update=viewer != "parent",
        report_action=report,
        undo_action=undo,
        change_action=details_href(assignment_id),
        change_fields=[
            *[(name, value) for name, value in carried.items() if value],
            ("change", "1"),
        ],
        cancel_href=details_href(assignment_id, **carried),
        post_fields=[("report_view", "detail"), *carried.items()],
        with_year=True,
        back=link,
        history_apart=True,
    )


@dataclass(frozen=True)
class ReturnLink:
    """The way back from an assignment's details, made on the server from checked data."""

    href: str
    label: str
    note: str | None = None


def way_back(
    state: ApplicationState, back: ReturnTo, assignment_id: str, *, today: date
) -> ReturnLink:
    """The link an assignment's details offer back to where their reader came from.

    Her week, with the card in view and its fold open. Today's plan, by the
    place on her week that holds it and never by the plan that was followed: the
    link is followed later than it is written, and another plan may have taken
    that one's place by then, or the day may have moved on, and the place is
    there either way. Today itself, with a word, when no plan is left as the
    link is written. The family page at this assignment's row, or at the plan
    that was being read when it is still on the pages; a plan that is not sends
    the reader to the family page and nothing more.
    """
    if back.target == "week":
        return ReturnLink(
            week_href(back.week, assignment_id, show=assignment_id), "Back to the week"
        )
    if back.target == "today":
        latest = state.drafts.latest_for(today)
        if latest is None:
            return ReturnLink(address(WEEK_PAGE, fragment="today"), "Back to Today", NO_PLAN_NOW)
        return ReturnLink(todays_plan_href(), "Back to today's plan")
    if back.plan_id is None:
        return ReturnLink(
            address(FAMILY_PAGE, fragment=f"update-{segment(assignment_id)}", focus=assignment_id),
            "Back to family review",
        )
    plan = state.drafts.get(back.plan_id)
    if plan is None or not plan.published:
        return ReturnLink(FAMILY_PAGE, "Back to family review")
    return ReturnLink(
        address(FAMILY_PAGE, fragment=anchor_for(plan.draft_id), plan=plan.draft_id),
        "Back to family review",
    )


def gone_page(
    request: Request,
    state: ApplicationState,
    back: ReturnTo,
    assignment_id: str,
    *,
    card: CardState | None = None,
    hand_in: HandInCard | None = None,
    today: date | None = None,
) -> HTMLResponse:
    """The small page for an assignment that is not on record: said plainly, 404, with a
    safe way back and, when a form brought her here, what she chose and wrote, so it can
    be copied. No form, and nothing is put back on record."""
    return templates.TemplateResponse(
        request,
        "student_assignment_gone.html",
        {
            "problem": GONE,
            "card": card,
            "hand_in_card": hand_in,
            "back": way_back(
                state, back, assignment_id, today=state.clock.today() if today is None else today
            ),
            "sample": state.settings.sample,
        },
        status_code=status.HTTP_404_NOT_FOUND,
    )


def detail_page(
    request: Request,
    state: ApplicationState,
    assignment_id: str,
    back: ReturnTo,
    *,
    card: CardState | None = None,
    hand_in: HandInCard | None = None,
    problem: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """One assignment's current record, with her update and the way back.

    The assignment is read by its id and nothing else, so work that is done,
    undated, assigned for later, or long past due is shown as any other;
    her week is never read to find it. The assignment, the claims about its
    date, her events, the school's reports, and the family's check are read
    in one hold of the store, and whether it is in the planning window is
    decided as her week decides it. Everything shown is the record as it
    stands, whatever plan or page the reader came from; the way back is data
    the reader's link carried, checked, and never chooses the assignment.
    """
    viewer = viewer_of(request)
    today = state.clock.today()
    on_record = state.project_state
    with on_record.reading():
        item = on_record.one_assignment(assignment_id)
        records = [] if item is None else on_record.deadline_records(assignment_id)
        found = None if item is None else statuses_for(on_record, [assignment_id])
        chains = {} if item is None else on_record.hand_in_chains([assignment_id])
    if item is None or found is None:
        return gone_page(
            request, state, back, assignment_id, card=card, hand_in=hand_in, today=today
        )
    try:
        turning_in = HandInView.of(project(assignment_id, chains.get(assignment_id, [])))
    except BrokenChain:
        logger.exception("the hand-in record of %s cannot be read", assignment_id)
        turning_in = HandInView.of(None)
    noticed = notice_due_date(expect_due_date(item), records)
    view = assignment_view(
        item,
        records,
        noticed,
        found[assignment_id],
        in_planning_window=in_week(item, noticed, today),
        hand_in=turning_in,
    )
    link = way_back(state, back, assignment_id, today=today)
    return templates.TemplateResponse(
        request,
        "student_assignment.html",
        {
            "assignment": view,
            "ctx": detail_context(assignment_id, back, viewer, link),
            "back": link,
            "card": card,
            "hand_in_card": hand_in,
            "hand_in_actions": hand_in_actions(assignment_id),
            "hand_in_fields": [(name, value) for name, value in back.fields().items() if value],
            "hand_in_links": {
                "change": details_href(
                    assignment_id, fragment=TURNING_IT_IN, hand_in="change", **back.fields()
                ),
                "keep": details_href(assignment_id, fragment=TURNING_IT_IN, **back.fields()),
                "open": details_href(assignment_id, fragment=TURNING_IT_IN),
            },
            "hand_in_change_fields": [
                *[(name, value) for name, value in back.fields().items() if value],
                ("hand_in", "change"),
            ],
            "next_action_max_length": NEXT_ACTION_MAX_LENGTH,
            "hand_in_note_max_length": HAND_IN_NOTE_MAX_LENGTH,
            "problem": problem,
            "update_note_max_length": UPDATE_NOTE_MAX_LENGTH,
            "sample": state.settings.sample,
        },
        status_code=status_code,
    )


@router.get("/assignments/{assignment_id}", response_class=HTMLResponse, include_in_schema=False)
def assignment_details(
    request: Request,
    assignment_id: str,
    state: State,
    return_to: Annotated[str | None, Query(description="week, today, or family")] = None,
    week: Annotated[
        str | None, Query(description="with week: a day of the week to go back to")
    ] = None,
    plan_id: Annotated[str | None, Query(description="with family: the plan to go back to")] = None,
    change: Annotated[str | None, Query(description="1 to open the update form")] = None,
    said: Annotated[
        str | None, Query(description="what a save or an undo just did; a note")
    ] = None,
    hand_in: Annotated[
        str | None, Query(description="change to open the hand-in form, or what a save did")
    ] = None,
) -> HTMLResponse:
    """One assignment's details, for her, a parent, or whoever is there with the sign-in off.

    Nothing here writes. The way back is read from three fields and checked;
    anything else is her safe default, or a parent's. ``change`` opens the
    form on an update that stands, and ``said`` is one of the three things a
    save or an undo can have done, which the server chose and the address
    only carries: any other word says nothing.
    """
    back, _ = read_return(
        {"return_to": return_to or "", "week": week or "", "plan_id": plan_id or ""},
        viewer=viewer_of(request),
        showable=showable,
    )
    card = None
    if said in CONFIRMATIONS:
        card = CardState(assignment_id, said=CONFIRMATIONS[said])
    elif change == "1":
        card = CardState(assignment_id, change=True)
    turning_in = None
    if hand_in in HAND_IN_CONFIRMATIONS:
        turning_in = HandInCard(said=HAND_IN_CONFIRMATIONS[hand_in])
    elif hand_in == "change":
        turning_in = HandInCard(change=True)
    return detail_page(request, state, assignment_id, back, card=card, hand_in=turning_in)


@dataclass(frozen=True)
class Origin:
    """Where a report form came from, which decides where its result is shown and nothing
    else: never who may save, and never which assignment is saved."""

    detail: bool
    week: date | None
    back: ReturnTo
    valid: bool
    """Whether the form said where it came from in words these pages make."""


def origin_of(request: Request, fields: Mapping[str, str]) -> Origin:
    """Read where a report form came from. A form that says nothing is a card on her week, as
    it always was. One from an assignment's details says so and carries its way back.

    A card's week is blank, for her current week, or a day her week page can
    show. Anything else, words that are no day or a day past either edge of
    the calendar, is not a week her page made: the form is not valid, nothing
    is written for it, and its refusal is shown on her current week."""
    viewer = viewer_of(request)
    shown = fields.get("report_view", "").strip()
    if shown == "detail":
        back, valid = read_return(fields, viewer=viewer, showable=showable)
        return Origin(detail=True, week=None, back=back, valid=valid)
    stray = any(fields.get(name, "").strip() for name in ("return_to", "plan_id"))
    given = fields.get("week", "").strip()
    week = week_named(given)
    return Origin(
        detail=False,
        week=week,
        back=safe_default(viewer),
        valid=shown in ("", "week") and not stray and (week is not None or not given),
    )


def result_page(
    request: Request,
    state: ApplicationState,
    assignment_id: str,
    origin: Origin,
    *,
    card: CardState | None = None,
    problem: str | None = None,
    status_code: int,
) -> HTMLResponse:
    """The page a form's result is shown on: the details it came from, or her week."""
    if origin.detail:
        return detail_page(
            request,
            state,
            assignment_id,
            origin.back,
            card=card,
            problem=problem,
            status_code=status_code,
        )
    return student_page(
        request, state, week=origin.week, card=card, problem=problem, status_code=status_code
    )


def after(origin: Origin, said: str, assignment_id: str) -> str:
    """Where a save or an undo sends her once it is committed: back to the page the form
    was on, with what happened. On the details the address lands on the result itself,
    where the way back is repeated, so neither is below a long page's first screen."""
    if origin.detail:
        return details_href(
            assignment_id,
            fragment=result_anchor(assignment_id),
            said=said,
            **origin.back.fields(),
        )
    return back_to_the_card(origin.week, said, assignment_id)


def plain_ways_back(origin: Origin, assignment_id: str) -> list[ReturnLink]:
    """The ways back a failure page offers, made from checked values and nothing else.

    No store is read, since this is the page for when the record cannot be:
    a form from the details gets the assignment's details again, with the
    way back they carried, and the page that way back names; a card gets
    its week with the card in view. Today is the Today panel, whichever plan
    is there when she arrives, and a plan on the family page is named to the
    family page, which opens it or not as it finds it.
    """
    back = origin.back
    if not origin.detail:
        return [
            ReturnLink(
                week_href(origin.week, assignment_id, show=assignment_id), "Back to the week"
            )
        ]
    details = ReturnLink(details_href(assignment_id, **back.fields()), "Back to the assignment")
    if back.target == "week":
        return [
            details,
            ReturnLink(week_href(back.week, assignment_id, show=assignment_id), "Back to the week"),
        ]
    if back.target == "today":
        return [details, ReturnLink(address(WEEK_PAGE, fragment="today"), "Back to Today")]
    if back.plan_id is None:
        where = address(
            FAMILY_PAGE, fragment=f"update-{segment(assignment_id)}", focus=assignment_id
        )
    else:
        where = address(FAMILY_PAGE, fragment=anchor_for(back.plan_id), plan=back.plan_id)
    return [details, ReturnLink(where, "Back to family review")]


def could_not(
    request: Request,
    state: ApplicationState,
    assignment_id: str,
    origin: Origin,
    card: CardState,
) -> HTMLResponse:
    """The page after a write the file refused: the component with what she typed, and no
    word of a save. When the page itself cannot be read back, a plain page with her words
    and the way back her form carried, which reads no store and tries nothing again."""
    try:
        return result_page(
            request,
            state,
            assignment_id,
            origin,
            card=card,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    except Exception:
        logger.exception("her page could not be read back after a failed save")
        return templates.TemplateResponse(
            request,
            "student_update_recovery.html",
            {
                "card": card,
                "ways_back": plain_ways_back(origin, assignment_id),
                "sample": state.settings.sample,
            },
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@router.post(
    "/actions/assignments/{assignment_id}/report",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def report_from_the_page(request: Request, assignment_id: str, state: State) -> Response:
    """Her update on one assignment: Done or Not yet, with a note if she wants one.

    The form is read whole before anything else: its fields, each once, and
    nothing more. It carries the last event the page showed, which must be
    one of this assignment's, so a save lands on the chain the page showed
    or is shown what changed: the same update as the one standing is already
    saved, with no write and no new day; a page whose head has moved on is
    answered 409 with the newer update above her form and her typed words
    kept; anything else is appended. The comparison and the write are one
    operation under the decision lock, so two devices saving together get
    one save and one refusal. A write the file refuses is answered with the
    component and her words, and never with a word of a save. A parent
    signed in is told the update is hers to make, 403, and nothing is
    written.

    One route serves the card on her week and the assignment's details. The
    form says which it came from, and that decides only where the result is
    shown: a save from the details comes back to the details, every refusal
    is shown there with what she typed, and an assignment taken off the
    record meanwhile is said on a small page with her words to copy. A form
    that names a page to go back to that these pages do not make is refused,
    422, with her input kept.
    """
    fields, whole = await fields_of(
        request, REPORT_FIELDS, may_be_absent=NOTHING_CHOSEN | FROM_DETAILS
    )
    origin = origin_of(request, fields)
    if viewer_of(request) == "parent":
        return result_page(
            request,
            state,
            assignment_id,
            origin,
            problem=NOT_HERS_TO_UPDATE,
            status_code=status.HTTP_403_FORBIDDEN,
        )
    said = fields.get("status", "").strip()
    note = fields.get("note", "")
    words = normalize_note(note)
    token = fields.get("expected_report_id", "").strip()
    chosen = said if said in (DONE, NOT_YET) and whole else None

    def refused(
        problem: str, code: int, *, field: str | None = None, saved_elsewhere: bool = False
    ) -> Response:
        return result_page(
            request,
            state,
            assignment_id,
            origin,
            card=CardState(
                assignment_id,
                change=True,
                problem=problem,
                field=field,
                status=chosen,
                note=note,
                saved_elsewhere=saved_elsewhere,
            ),
            status_code=code,
        )

    if not whole:
        return refused(BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if not origin.valid:
        return refused(BAD_RETURN, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if said not in (DONE, NOT_YET):
        return refused(CHOOSE_ONE, status.HTTP_422_UNPROCESSABLE_CONTENT, field="status")
    if words is not None and len(words) > UPDATE_NOTE_MAX_LENGTH:
        return refused(NOTE_TOO_LONG, status.HTTP_422_UNPROCESSABLE_CONTENT, field="note")
    if len(token) > TOKEN_MAX_LENGTH:
        return refused(NOT_THIS_CARDS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    try:
        async with state.decision_lock:
            result = state.project_state.report_status(
                assignment_id,
                cast(StudentStatus, said),
                words,
                expected_head=token or None,
                now=state.clock.now(),
                today=state.clock.today(),
            )
    except UnknownAssignment:
        if origin.detail:
            return gone_page(
                request,
                state,
                origin.back,
                assignment_id,
                card=CardState(assignment_id, status=chosen, note=note),
            )
        return student_page(
            request,
            state,
            week=origin.week,
            problem=NOT_ON_RECORD,
            status_code=status.HTTP_404_NOT_FOUND,
        )
    except UnknownReport:
        return refused(NOT_THIS_CARDS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    except NoteTooLong:
        return refused(NOTE_TOO_LONG, status.HTTP_422_UNPROCESSABLE_CONTENT, field="note")
    except CouldNotSave:
        logger.exception("her update on %s could not be saved", assignment_id)
        return could_not(
            request,
            state,
            assignment_id,
            origin,
            CardState(assignment_id, change=True, problem=NOT_SAVED, status=said, note=note),
        )
    match result:
        case Saved():
            return RedirectResponse(
                after(origin, "saved", assignment_id), status_code=status.HTTP_303_SEE_OTHER
            )
        case AlreadySaved():
            return RedirectResponse(
                after(origin, "same", assignment_id), status_code=status.HTTP_303_SEE_OTHER
            )
        case Conflict():
            return refused(SAVED_ELSEWHERE, status.HTTP_409_CONFLICT, saved_elsewhere=True)


@router.post(
    "/actions/assignments/{assignment_id}/undo-report",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def undo_report_from_the_page(request: Request, assignment_id: str, state: State) -> Response:
    """Take her latest update back, restoring what stood before it.

    The form is read whole, its fields each once. The button names the
    update it takes back, which must be one of this assignment's; one that
    is not the latest, or is itself an undo, meets a 409 that says the update
    has changed, with the component as it stands, since what she meant to
    take back is not what is there. A write the file refuses is said as
    that, and never as an undo. A parent is answered 403. The result is
    shown where the form was, her week or the assignment's details, and an
    undo never sends her to another week.
    """
    fields, whole = await fields_of(request, UNDO_FIELDS, may_be_absent=FROM_DETAILS)
    origin = origin_of(request, fields)
    if viewer_of(request) == "parent":
        return result_page(
            request,
            state,
            assignment_id,
            origin,
            problem=NOT_HERS_TO_UPDATE,
            status_code=status.HTTP_403_FORBIDDEN,
        )
    named = fields.get("report_id", "").strip()

    def refused(problem: str, code: int, *, saved_elsewhere: bool = False) -> Response:
        return result_page(
            request,
            state,
            assignment_id,
            origin,
            card=CardState(assignment_id, problem=problem, saved_elsewhere=saved_elsewhere),
            status_code=code,
        )

    if not whole:
        return refused(BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if not origin.valid:
        return refused(BAD_RETURN, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if len(named) > TOKEN_MAX_LENGTH:
        return refused(NOT_THIS_CARDS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    try:
        async with state.decision_lock:
            result = state.project_state.undo_report(
                assignment_id,
                named,
                now=state.clock.now(),
                today=state.clock.today(),
            )
    except UnknownAssignment:
        if origin.detail:
            return gone_page(request, state, origin.back, assignment_id)
        return student_page(
            request,
            state,
            week=origin.week,
            problem=NOT_ON_RECORD,
            status_code=status.HTTP_404_NOT_FOUND,
        )
    except UnknownReport:
        return refused(NOT_THIS_CARDS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    except CouldNotSave:
        logger.exception("her update on %s could not be undone", assignment_id)
        return could_not(
            request, state, assignment_id, origin, CardState(assignment_id, problem=NOT_UNDONE)
        )
    match result:
        case Undone():
            return RedirectResponse(
                after(origin, "undone", assignment_id), status_code=status.HTTP_303_SEE_OTHER
            )
        case Conflict(head=head):
            repeat = head is not None and head.operation == UNDO and head.undoes_report_id == named
            return refused(
                ALREADY_UNDONE if repeat else CANNOT_UNDO,
                status.HTTP_409_CONFLICT,
                saved_elsewhere=True,
            )


@router.post("/actions/plan", response_class=HTMLResponse, include_in_schema=False)
async def plan_from_the_page(request: Request, state: State, graphs: Graphs) -> Response:
    """The plan button. Makes today's plan and returns to the page, which shows it.

    A run that could not start or ended without a plan is said on the page
    with the status the JSON route would have answered, and whatever plan the
    page already had stays. So is a run that failed on the way, for any other
    reason: the run has already taken back what it left by then, the failure
    goes to the process log, and the page says something went wrong rather
    than answering with a bare error.
    """
    try:
        require_work(state, state.clock.today())
        require_model(graphs)
        run = await run_plan(
            graphs.build(),
            state.clock.today(),
            state,
        )
    except HTTPException as error:
        return student_page(
            request,
            state,
            problem=f"Blossom could not make a plan: {error.detail}",
            status_code=error.status_code,
        )
    except Exception:
        logger.exception("today's plan failed on the way")
        return student_page(
            request,
            state,
            problem=(
                "Blossom could not make a plan: something went wrong on the way. "
                "The plan already here, if any, is unchanged."
            ),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    if run.draft_id is None:
        return student_page(
            request,
            state,
            problem=ended_without_a_plan(run.outcome),
            status_code=status.HTTP_409_CONFLICT,
        )
    return RedirectResponse(f"{PAGE}?show_plan=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/actions/ask-for-help", response_class=HTMLResponse, include_in_schema=False)
def ask_for_help_from_the_page(
    request: Request, state: State, note: Annotated[str, Form()] = ""
) -> Response:
    """The other press on her page. A blank note is no note; a long one is said, not cut."""
    words = note.strip()
    if len(words) > NOTE_MAX_LENGTH:
        return student_page(
            request,
            state,
            problem=f"A note is at most {NOTE_MAX_LENGTH} characters; this one is {len(words)}.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    state.help_requests.ask(state.clock.today(), words or None)
    return RedirectResponse(PAGE, status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/actions/take-back-help/{request_id}", response_class=HTMLResponse, include_in_schema=False
)
def take_back_help_from_the_page(request: Request, request_id: str, state: State) -> Response:
    """Remove a request from her page while nobody has taken it up; otherwise the page says why."""
    try:
        removed = state.help_requests.take_back(request_id)
    except RequestClosed as error:
        return student_page(
            request, state, problem=str(error), status_code=status.HTTP_409_CONFLICT
        )
    if not removed:
        return student_page(
            request,
            state,
            problem="That request is not here any more; nothing was changed.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return RedirectResponse(PAGE, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/actions/too-much", response_class=HTMLResponse, include_in_schema=False)
async def too_much_from_the_page(state: State) -> Response:
    """The one control on her page. Records the signal and returns to the page, which shows it."""
    await record_signal(state, None)
    return RedirectResponse(PAGE, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/actions/take-back/{signal_id}", response_class=HTMLResponse, include_in_schema=False)
async def take_back_from_the_page(signal_id: str, state: State) -> Response:
    """Remove a signal from her page. A signal already gone is not an error here."""
    await withdraw_signal(state, signal_id)
    return RedirectResponse(PAGE, status_code=status.HTTP_303_SEE_OTHER)
