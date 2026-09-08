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
"""

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Final

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
from blossom.dependencies import ApplicationState, get_application_state
from blossom.evening import Staleness, staleness
from blossom.noticing import (
    Noticing,
    expect_due_date,
    monday_of,
    notice_due_date,
    read_date,
    read_week,
    reconcile_dates,
)
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
from blossom.routes.runs import Graphs, require_model, run_plan
from blossom.settings import CALENDAR_MARGIN
from blossom.stores.drafts import DraftRecord
from blossom.stores.help_requests import NOTE_MAX_LENGTH, HelpRequest, RequestClosed
from blossom.stores.project_state import DUE_THIS_WEEK_SPAN, Assignment
from blossom.stores.workload_signals import DETAIL_MAX_LENGTH, WorkloadSignal
from blossom.templating import page_templates
from blossom.views import (
    HelpRequestView,
    StudentAssignmentView,
    StudentDueThisWeekView,
    StudentPlanView,
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


def plan_view(state: ApplicationState, record: DraftRecord) -> StudentPlanView:
    """Her projection of a draft: the plan, a parent's review if any, and whether it still fits."""
    stale = None
    match staleness(state.workload_signals, record):
        case Staleness.SIGNALED_SINCE:
            stale = SIGNALED_SINCE
        case Staleness.SIGNAL_ENDED:
            stale = SIGNAL_ENDED
        case None:
            stale = None
    return StudentPlanView(
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
    )


def todays_plan(state: ApplicationState) -> StudentPlanView | None:
    """Today's latest plan, or ``None`` when none has been made."""
    record = state.drafts.latest_for(state.clock.today())
    return None if record is None else plan_view(state, record)


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
    whatever plan it had.
    """
    require_model(graphs)
    run = await run_plan(
        graphs.build(),
        state.clock.today(),
        state,
    )
    if run.draft_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"no plan was made: the run ended with {run.outcome}"
        )
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
    assignment: Assignment, records: Sequence[SourceRecord], noticed: Noticing
) -> StudentAssignmentView:
    """One assignment as she sees it, with where its date came from said once per channel.

    Claims are reconciled as dates, readable ones only, so two channels giving
    the same date in different spellings agree and a weekday name is neither a
    second source nor a conflict. The card shows the record's date, so a
    channel confirms it only by giving a value that reads as that date. A value
    the comparator cannot read is carried apart; it is not the origin of
    anything.
    """
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
        disagreement=disagreement,
        contradiction=[record.spoken() for record in readable] if noticed.contradicted else [],
        school_contradicts=noticed.contradicted
        and any(record.channel in SCHOOL_CHANNELS for record in readable),
        assigned_on=assignment.assigned_on,
    )


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
    state: ApplicationState, week: date | None = None
) -> StudentDueThisWeekView:
    """Assemble the student's weekly view from the stores ``ApplicationState``
    opened at startup; nothing is opened or seeded per request. ``week`` is any
    day in the school week to show; today's week when ``None``.
    """
    today = state.clock.today()
    frame = week_shown(today, week)
    shown = read_week(state.project_state, state.source, frame.start)
    # Never filter here; see the module docstring.
    views = [
        assignment_view(
            item, shown.records[item.assignment_id], shown.noticings[item.assignment_id]
        )
        for item in shown.assignments
    ]
    in_frame = {item.assignment_id for item in shown.assignments}
    assigned: list[StudentAssignmentView] = []
    for item in state.project_state.all_assignments():
        if item.assignment_id in in_frame or not assigned_for_later(item, frame):
            continue
        records = state.source.deadline_records(item.assignment_id)
        assigned.append(
            assignment_view(item, records, notice_due_date(expect_due_date(item), records))
        )
    tonight = state.workload_signals.for_evening(today)
    household = state.settings
    return StudentDueThisWeekView(
        generated_at=datetime.now(UTC),
        today=today,
        week=frame,
        assignments=views,
        assigned_this_week=assigned,
        plan_horizon_end=today + DUE_THIS_WEEK_SPAN,
        full_budget_minutes=household.evening_minutes,
        budget_minutes=household.too_much_minutes if tonight else household.evening_minutes,
        plan=todays_plan(state),
        can_plan=model_configured(state.settings),
        too_much=signal_view(state, tonight[-1]) if tonight else None,
        signals=[signal_view(state, signal) for signal in state.workload_signals.held()],
        help_requests=help_requests_shown(state),
    )


def student_page(
    request: Request,
    state: ApplicationState,
    *,
    week: date | None = None,
    problem: str | None = None,
    plan_open: bool = False,
    refreshed: bool = False,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """Render her page. ``problem`` is what an action could not do, said once at the top.

    ``plan_open`` shows today's plan unfolded and ``refreshed`` says when the
    page was last asked for; both change how the page is presented and
    nothing else.
    """
    view = build_student_due_this_week_view(state, week)
    return templates.TemplateResponse(
        request,
        "student_due_this_week.html",
        {
            "view": view,
            "problem": problem,
            "plan_open": plan_open,
            "refreshed_at": state.clock.now().astimezone(state.clock.zone) if refreshed else None,
            "note_max_length": NOTE_MAX_LENGTH,
            "sample": state.settings.sample,
        },
        status_code=status_code,
    )


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
) -> HTMLResponse:
    """Render her week and today's plan.

    The week is the school week that holds today, or the one holding the day
    ``week`` names. A value that is not a date, a blank one included, or a
    week at the edge of the calendar, is said at the top of today's week
    rather than answered with an error page. Only an absent ``week`` means
    today's week without a word. ``show_plan`` unfolds the plan, as the page
    does right after one is made; a GET never makes one.
    """
    plan_open = show_plan == "1"
    was_refreshed = refreshed == "1"
    if week is None:
        return student_page(request, state, plan_open=plan_open, refreshed=was_refreshed)
    try:
        chosen = date.fromisoformat(week.strip())
    except ValueError:
        return student_page(
            request,
            state,
            problem=NOT_A_WEEK,
            plan_open=plan_open,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if not showable(chosen):
        return student_page(
            request,
            state,
            problem=BEYOND_THE_CALENDAR,
            plan_open=plan_open,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    return student_page(request, state, week=chosen, plan_open=plan_open)


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
            problem=f"No plan was made this time: the run ended with {run.outcome}.",
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
