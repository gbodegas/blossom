"""Parent routes: review the plan she has, and start one for her when asked.

A parent is a collaborator who sets goals, corrects information and reviews
drafts, so these routes expose a queue and a checkpoint rather than a live
feed. A live feed would turn collaboration into monitoring.

The plan is hers from the moment it is made: it is on her page before anyone
here has seen it, and nothing she does with it waits for this page. What a
parent records here is a review, looks good or a change asked for, and the
review shows on her page under the plan. The pause at the gate is the same
mechanism that will one day hold a note to a teacher until a person decides;
for an evening's plan it holds only the review.

Three routes drive the plan graph. ``POST /parent/plans`` starts a run for one
evening, the same run her page starts, and reports how it ended: at the gate
with a draft, or before it with a reason. ``GET /parent/approvals`` lists the
drafts waiting for a review, read from the drafts table rather than from graph
state, because the table is the record across threads and needs no model to
read. ``POST /parent/approvals/{draft_id}`` resumes the paused run with the
review; the graph's gate node records it in saved state and the node after the
gate records it in the table.

Starting a run needs the model seam, which needs a key, and says so with a
503 when there is none. Reading the queue and deciding do not: nothing past
the gate asks a model, so a graph built without a key can still resume a
paused thread and record the decision. The graph is built inside the handler,
after the table has been consulted, because a dependency is resolved before a
handler runs: a draft that does not exist is a 404 with or without a key, and
one already decided is a 409.

Two decisions about one draft cannot both land. The handler holds the
application's decision lock from the table check through the resume, and the
table refuses a second, different decision even if a request arrives from
another process.

The page at ``/parent`` is the same three things as a form, for a person
rather than a client: a date to plan for her, the drafts waiting with their
text and two buttons, and what has been reviewed. Its two form actions call
the same functions the JSON routes call and redirect back to the page, so
there is one way to start a run and one way to review, whichever door it
comes through.

Not yet implemented: when the system notifies a parent that a deadline is at
risk, the parent needs to be able to see that the notification happened.
Without that, the visibility policy is stated but not observable.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from blossom.agent.runs import DURABILITY, StaleGraphVersion, ensure_current_version, run_config
from blossom.anthropic_client import model_configured
from blossom.assignment_status import AssignmentStatus, basis_parts, statuses_for
from blossom.clock import local_now
from blossom.dependencies import ApplicationState, get_application_state
from blossom.evening import Staleness, reported_done, staleness
from blossom.intake import NOTE_MAX_LENGTH as ENTRY_NOTE_MAX_LENGTH
from blossom.intake import TEXT_MAX_LENGTH
from blossom.routes.forms import TOKEN_MAX_LENGTH, fields_of
from blossom.routes.runs import (
    Graphs,
    PlanGraphBuilder,
    refuse_an_empty_run,
    require_model,
    require_work,
    run_plan,
    tidy_thread,
)
from blossom.settings import CALENDAR_MARGIN
from blossom.stores.drafts import AlreadyDecided, DraftRecord
from blossom.stores.help_requests import NOTE_MAX_LENGTH, HelpRequest, RequestClosed
from blossom.stores.project_state import (
    CHECKED,
    AlreadyChecked,
    Assignment,
    CheckConflict,
    Checked,
    CouldNotSave,
    NoteTooLong,
    Reopened,
    UnknownAssignment,
    UnknownCheck,
    normalize_note,
)
from blossom.stores.project_state import NOTE_MAX_LENGTH as CHECK_NOTE_MAX_LENGTH
from blossom.templating import page_templates
from blossom.views import (
    ApprovalQueueView,
    ApprovalView,
    AssignmentUpdatesView,
    AssignmentUpdateView,
    DecisionView,
    HelpRequestView,
    NamedAssignmentView,
    ParentCheckpointAssignmentView,
    ParentCheckpointView,
    PlanRunView,
    RunView,
    SchoolStatementView,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/parent", tags=["parent"])
templates = page_templates()

State = Annotated[ApplicationState, Depends(get_application_state)]


class PlanRequest(BaseModel):
    """Which evening to plan. Defaults to today in the household's zone."""

    model_config = ConfigDict(extra="forbid")

    plan_date: date | None = None


REASON_MAX_LENGTH: Final = 500
"""The most that is kept of a reason: a sentence or two she would recognize.
The form and the JSON route hold to the same number, so a reason that fits
one fits the other."""


class DecisionRequest(BaseModel):
    """What the parent decided.

    ``approved`` is strict: a JSON ``true`` or ``false`` and nothing else. The
    gate already reads only the boolean ``True``, and the default coercion here
    would have read the string ``"yes"`` as approval before the gate ever saw
    it, so the same rule is applied at the boundary.
    """

    model_config = ConfigDict(extra="forbid")

    approved: StrictBool
    reason: str | None = Field(default=None, max_length=REASON_MAX_LENGTH)


SIGNALED_SINCE: Final = (
    "She has said today is too much, and this plan was made for the full evening. "
    "Plan again before approving."
)
SIGNAL_ENDED: Final = (
    "Her signal for this evening is gone, taken back or past its week, and this plan "
    "was kept short for it. Plan again for the full evening."
)
ASSIGNMENTS_CHANGED: Final = (
    "The assignments or her updates differ from the work this plan used: the work in its "
    "window, a date, a type, a note, a status the school reports, what she reports about "
    "her part, or what a source says about a date. The plan does not cover the week as it "
    "stands. Plan again before approving."
)
PLAN_INCLUDES_DONE: Final = "This plan includes work she now reports as Done."
PLAN_WINDOW_DONE: Final = "Some work in this plan's window is now reported Done."
RECENT_DAYS: Final = 14
"""How many household days an update of hers stays under "Recent updates", and a check of
the family's under "Checked recently"."""
CHECK_FIELDS: Final = frozenset({"basis", "expected_check_id", "note"})
AGAIN_FIELDS: Final = frozenset({"check_id"})
"""The fields each check form sends, each once. Anything else, or anything twice, is refused."""
BASIS_MAX_LENGTH: Final = 500
"""Longer than any basis the page makes: an assignment id, a report id, and a statement or two."""
CHECK_RECORDED: Final = (
    "Marked checked. This records the check here; her update and the school's report are "
    "as they were."
)
CHECK_ALREADY: Final = (
    "Already marked checked, from this device or another, so nothing was written. The note "
    "on record is the one shown."
)
CHECK_REOPENED: Final = "Open to check again. The earlier check stays in the record."
CHECK_MOVED_ON: Final = (
    "This row was marked checked or reopened from another device since this page was made. "
    "Nothing was written; the row shows what stands now."
)
CHECK_FACTS_CHANGED: Final = (
    "What this row rests on has changed since this page was made: her update, or the "
    "school's report. Nothing was written; the row shows what stands now."
)
CHECK_NOTE_TOO_LONG: Final = f"A note is at most {CHECK_NOTE_MAX_LENGTH} characters."
BAD_CHECK_FORM: Final = "The form did not arrive whole. Nothing was written."
NOT_THIS_ROWS: Final = (
    "The form does not fit this row: it names a check, or a basis, that is not this "
    "assignment's as shown. Nothing was written."
)
NOT_ON_RECORD: Final = "No assignment with that id is on record. Nothing was written."
CHECK_NOT_SAVED: Final = "The check could not be saved. Nothing was written."
CHECK_NOT_REOPENED: Final = "The check could not be reopened. Nothing was written."
CHECK_CONFIRMATIONS: Final[dict[str, str]] = {
    "checked": CHECK_RECORDED,
    "checked_already": CHECK_ALREADY,
    "reopened": CHECK_REOPENED,
}
"""What the address says happened to a row's check, and the sentence the row shows for it:
the server chooses which, the address only carries the choice."""


@dataclass(frozen=True)
class CheckState:
    """What one row of the assignment updates shows beyond the record: what a check form did,
    a problem with it, which field the problem is about, and the note typed, kept."""

    assignment_id: str
    said: str | None = None
    problem: str | None = None
    field: str | None = None
    note: str = ""


def stale_reason(state: ApplicationState, record: DraftRecord) -> str | None:
    """Why a waiting draft has stopped fitting the evening, or ``None`` while it fits.

    A draft is made for the evening as she had described it when the run
    read it. When her signal as it stands is not that one, the plan on the
    page is not the plan the checks held to the current budget, so it is not
    approved as it stands. Neither message says which came first: her signal
    can change while a run is still on its way to the draft, so the pages say
    only that the two do not match.
    Refusing it is still allowed; refusing never sends anything. Only a
    waiting draft can be stale: a decided one is a record of what was decided,
    and is not measured against the evening again.

    A signal that is gone was either taken back or aged out of the store, and
    the store does not say which, so the message names both rather than
    putting an action on her that she may not have taken. A draft for an
    evening that has passed is never stale: it cannot be planned again, since
    the pages refuse a past evening, and it reaches no page of hers, so there
    is nothing a fresh plan would put right.
    """
    if not record.waiting or record.plan_date < state.clock.today():
        return None
    match staleness(state.workload_signals, record, state.project_state):
        case Staleness.SIGNALED_SINCE:
            return SIGNALED_SINCE
        case Staleness.SIGNAL_ENDED:
            return SIGNAL_ENDED
        case Staleness.ASSIGNMENTS_CHANGED:
            return ASSIGNMENTS_CHANGED
        case None:
            return None


def done_in(
    state: ApplicationState, record: DraftRecord
) -> tuple[str, list[NamedAssignmentView]] | None:
    """That today's plan includes work she reports as done as things stand, and which work.

    Said only for the plan her page shows as today's, whatever was decided
    about it: earlier plans are history and get no notice from an update
    made today, and a plan a later one took the place of is one of those.
    What is compared is the plan's ids and her updates as they stand, never
    the time of either.
    """
    today = state.clock.today()
    if record.plan_date != today:
        return None
    current = state.drafts.latest_for(today)
    if current is None or current.draft_id != record.draft_id:
        return None
    found = reported_done(state.project_state, record)
    if found is None:
        return None
    if not found.known:
        return PLAN_WINDOW_DONE, []
    return PLAN_INCLUDES_DONE, [
        NamedAssignmentView(assignment_id=name, title=title) for name, title in found.named
    ]


def approval_view(state: ApplicationState, record: DraftRecord) -> ApprovalView:
    """A draft as the parent sees it, with whether it still fits the evening.

    The notice and the stale state are read from one reading of her reports,
    so a report landing between the two cannot leave them at odds.
    """
    with state.project_state.exclusively():
        included = done_in(state, record)
        stale = stale_reason(state, record)
    return ApprovalView.from_record(
        record,
        stale=stale,
        reported_done=None if included is None else included[0],
        reported_done_work=[] if included is None else included[1],
    )


def passed(evening: date) -> str:
    """Why a plan for a past evening is refused: no page of hers would ever show it."""
    return (
        f"The evening of {evening.isoformat()} has passed. Plans are for today or a later evening."
    )


def beyond(evening: date) -> str:
    """Why a plan for an evening past the calendar's edge is refused: its week cannot be read."""
    return (
        f"The evening of {evening.isoformat()} is past the edge of the calendar. "
        f"Plans reach no later than {(date.max - CALENDAR_MARGIN).isoformat()}."
    )


@router.post("/plans", response_model=PlanRunView, status_code=status.HTTP_201_CREATED)
async def start_plan(request: PlanRequest, state: State, graphs: Graphs) -> PlanRunView:
    """Run the plan graph for one evening, up to the gate or to the reason it stopped.

    An evening that has passed, or one past the edge of the calendar, is
    refused with 422 before anything runs, and so is an evening with nothing
    left to plan, before and after the run: a report of hers can land between
    the question and the run's reading, and such a run made no plan. A run
    that reached a model and ended without a plan is answered with its
    record, since the page lists those.
    """
    evening = request.plan_date or state.clock.today()
    if evening < state.clock.today():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=passed(evening))
    if evening > date.max - CALENDAR_MARGIN:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=beyond(evening))
    require_work(state, evening)
    require_model(graphs)
    run = await run_plan(
        graphs.build(),
        evening,
        state,
    )
    refuse_an_empty_run(run)
    return run


@router.get("/approvals", response_model=ApprovalQueueView)
def approvals(state: State) -> ApprovalQueueView:
    """Every draft waiting for a decision, oldest first. Needs no model to read."""
    return ApprovalQueueView(
        generated_at=state.clock.now(),
        waiting=[approval_view(state, record) for record in state.drafts.waiting()],
    )


@router.get("/approvals/{draft_id}", response_model=ApprovalView)
def approval(draft_id: str, state: State) -> ApprovalView:
    """One draft, waiting or decided, as the table records it."""
    record = state.drafts.get(draft_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no draft {draft_id!r}")
    return approval_view(state, record)


@router.post("/approvals/{draft_id}", response_model=DecisionView)
async def decide(
    draft_id: str, request: DecisionRequest, state: State, graphs: Graphs
) -> DecisionView:
    """Resume the paused run with the decision, and report what was recorded."""
    return await decide_draft(state, graphs.build, draft_id, request)


async def decide_draft(
    state: ApplicationState, build: PlanGraphBuilder, draft_id: str, request: DecisionRequest
) -> DecisionView:
    """The decision, from the table check to the resumed thread, under one lock.

    The table is read first, so an unknown or already decided draft is refused
    without a graph and therefore without a key. The graph is built only after
    that, and asked whether the thread is still waiting at the gate and was
    written by this version, since the table can say a draft waits while the
    thread has moved on. The lock spans the whole sequence: two requests about
    one draft cannot both see it waiting, and the table's own refusal covers a
    second process.

    A thread past the gate with its record unwritten is a review that reached
    the thread and then failed to land in the table. It is finished with the
    decision the thread holds rather than given a new one, whatever the request
    says and whatever the evening's signal says now, since the review was
    checked against the evening when it was given; a request that disagrees
    with what stood is told so.
    """
    async with state.decision_lock:
        record = state.drafts.get(draft_id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no draft {draft_id!r}")
        if not record.waiting:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"draft {draft_id!r} was already {record.decision}",
            )
        graph = build()
        config = run_config(record.thread_id, callbacks=[state.tracer])
        snapshot = await graph.aget_state(config)
        resume: Command[Any] | None
        if snapshot.next == ("require_human_approval",):
            stale = stale_reason(state, record)
            if request.approved and stale is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, detail=stale)
            resume = Command(resume={"approved": request.approved, "reason": request.reason})
        elif snapshot.next == ("record_decision",):
            resume = None
        else:
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail=f"draft {draft_id!r} is not waiting at the gate"
            )
        try:
            ensure_current_version(snapshot)
        except StaleGraphVersion as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        try:
            await graph.ainvoke(resume, config=config, durability=DURABILITY)
        except AlreadyDecided as error:
            # The review reached the thread, so the gate is passed and the
            # thread cannot pause again, and the table refused because the
            # draft was closed meanwhile: a later plan published from another
            # process, since publication in this one waits for the lock this
            # review holds. Nothing can resume the thread, so it goes, and a
            # thread that cannot be cleared now is left to the sweep.
            await tidy_thread(record.thread_id, state)
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        decided = state.drafts.get(draft_id)
        # The decision is in the table; the loop is over and its state is cleared.
        await tidy_thread(record.thread_id, state)
    if decided is None or decided.waiting:
        msg = f"the run resumed but no decision was recorded for {draft_id!r}"
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=msg)
    if resume is None and (
        (decided.decision == "approved") != request.approved or decided.reason != request.reason
    ):
        # The review that reached the thread first stands, words and all, as
        # it would had it been recorded at once; a request that differs in
        # either is told so rather than reported as its own success.
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"draft {draft_id!r} was already {decided.decision}"
        )
    return DecisionView.from_record(decided)


@router.get("/checkpoint", response_model=ParentCheckpointView)
def checkpoint(state: State) -> ParentCheckpointView:
    """Return the parent checkpoint as a fixed placeholder response."""
    return ParentCheckpointView(
        checkpoint_at=state.clock.now(),
        assignments=[
            ParentCheckpointAssignmentView(
                course="World History",
                title="Canal Era comparison essay",
                aggregate_status="in_progress",
                has_schedule_conflict=True,
            )
        ],
    )


# ------------------------------------------------------------ requests for help


class HelpStep(BaseModel):
    """A parent's move on a request: taking it up or resolving it, with a word back if any."""

    model_config = ConfigDict(extra="forbid")

    response: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)


def help_view(state: ApplicationState, request: HelpRequest) -> HelpRequestView:
    """The request as the parent sees it, which is exactly as she sees it."""
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


def move_request(
    state: ApplicationState, request_id: str, step: str, response: str | None
) -> HelpRequest:
    """Take a request up or resolve it; unknown is 404, closed is 409, any other step is 422."""
    try:
        if step == "accept":
            return state.help_requests.accept(request_id, response)
        if step == "resolve":
            return state.help_requests.resolve(request_id, response)
    except KeyError as error:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"no help request {request_id!r}"
        ) from error
    except RequestClosed as error:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=f"{step!r} is not one of the two moves, accept or resolve.",
    )


@router.get("/help-requests")
def help_requests(state: State) -> list[HelpRequestView]:
    """Every request she has open, oldest first, then those resolved within two weeks."""
    return [
        help_view(state, request)
        for request in [
            *state.help_requests.open_requests(),
            *state.help_requests.recently_resolved(),
        ]
    ]


@router.post("/help-requests/{request_id}/accept")
def accept_help_request(
    request_id: str, state: State, payload: Annotated[HelpStep | None, Body()] = None
) -> HelpRequestView:
    """Take a request up, so her page says a parent is on it."""
    response = None if payload is None else payload.response
    return help_view(state, move_request(state, request_id, "accept", response))


@router.post("/help-requests/{request_id}/resolve")
def resolve_help_request(
    request_id: str, state: State, payload: Annotated[HelpStep | None, Body()] = None
) -> HelpRequestView:
    """Answer a request, with a word back if given; her page shows it for two weeks."""
    response = None if payload is None else payload.response
    return help_view(state, move_request(state, request_id, "resolve", response))


# --------------------------------------------------------------------- the page

DECISIONS: Final = ("approve", "refuse")
"""The two buttons. Anything else in the field is a 422 page, not a guess."""


def review_page(
    request: Request,
    state: ApplicationState,
    *,
    problem: str | None = None,
    problem_field: str | None = None,
    refreshed: bool = False,
    added: int | None = None,
    updated: int | None = None,
    unchanged: int | None = None,
    paste: str | None = None,
    entry: Mapping[str, str] | None = None,
    entry_open: bool = False,
    check: CheckState | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """Render the queue, the decisions, the forms to plan an evening and to add assignments,
    and the folds below.

    ``problem`` is what a form action could not do, shown once at the top with
    the status the JSON route would have answered, so the page tells the truth
    the API tells; ``problem_field`` names the entry field it is about, so the
    page can mark it and put the cursor there. ``paste`` and ``entry`` are a
    draft to show again, as it was, with the section open. ``check`` is what
    one row of the assignment updates shows beyond the record, a check made
    or a problem with one; a row's problem is said at the top too, with a
    link to the row, so it is met on a page that opens at its top.
    """
    about_a_row = check is not None and check.problem is not None and problem is None
    return templates.TemplateResponse(
        request,
        "parent_review.html",
        {
            "today": state.clock.today(),
            "model_available": model_configured(state.settings),
            "waiting": [approval_view(state, record) for record in state.drafts.waiting()],
            "decided": [approval_view(state, record) for record in state.drafts.decided()],
            "ended": [RunView.from_record(run) for run in state.drafts.runs_without_a_draft()],
            "problem": check.problem if about_a_row and check is not None else problem,
            "problem_target": check.assignment_id if about_a_row and check is not None else None,
            "reason_max_length": REASON_MAX_LENGTH,
            "help_open": [help_view(state, r) for r in state.help_requests.open_requests()],
            "help_resolved": [help_view(state, r) for r in state.help_requests.recently_resolved()],
            "note_max_length": NOTE_MAX_LENGTH,
            "sample": state.settings.sample,
            "zone": state.clock.zone,
            "refreshed_at": local_now(state.clock.zone) if refreshed else None,
            "added": added,
            "updated": updated,
            "unchanged": unchanged,
            "problem_field": problem_field,
            "paste": paste or "",
            "entry": dict(entry or {}),
            "entry_open": entry_open or bool(paste) or bool(entry),
            "text_max_length": TEXT_MAX_LENGTH,
            "entry_note_max_length": ENTRY_NOTE_MAX_LENGTH,
            "updates": assignment_updates(state),
            "check": check,
            "check_note_max_length": CHECK_NOTE_MAX_LENGTH,
        },
        status_code=status_code,
    )


def assignment_updates(state: ApplicationState) -> AssignmentUpdatesView:
    """What she and the school have reported, in the family page's four groups.

    Each assignment is in one group, the first that fits. Her "done" beside
    any school channel's current "missing", with no check of the family's
    standing against it, is worth checking together, and comes first, open,
    however old. Next, folded, the rows a parent marked checked in the last
    fourteen household days, by the check's day, the latest check first,
    while the check stands against the facts as they are. Of the rest, the
    assignments with an event of hers in the last fourteen household days,
    a correction included, follow, by that latest event, most recent first,
    each showing the day of the update that stands, or that none does.
    Every other assignment the school has a current statement about closes
    the section, in the record's order. Whichever group a row is in, it
    shows her update when she has one, what each school channel says now,
    every channel, and the check that stands, so a row never leaves out a
    fact it was grouped by. The rows, her events, the school's reports, and
    the family's checks are read while the store is held, one snapshot, as
    her page reads them, in a few batched reads whatever the number of
    rows.
    """
    today = state.clock.today()
    with state.project_state.exclusively():
        rows = state.project_state.all_assignments()
        statuses = statuses_for(state.project_state, [item.assignment_id for item in rows])
    views = {item.assignment_id: update_view(item, statuses[item.assignment_id]) for item in rows}
    check = [view for view in views.values() if statuses[view.assignment_id].needs_a_check]
    shown = {view.assignment_id for view in check}

    def check_made_at(view: AssignmentUpdateView) -> datetime:
        standing = statuses[view.assignment_id].check
        return datetime.min.replace(tzinfo=UTC) if standing is None else standing.checked_at

    checked = sorted(
        (
            view
            for view in views.values()
            if view.assignment_id not in shown
            and view.checked_on is not None
            and view.checked_on > today - timedelta(days=RECENT_DAYS)
        ),
        key=check_made_at,
        reverse=True,
    )
    shown |= {view.assignment_id for view in checked}

    def latest_at(view: AssignmentUpdateView) -> datetime:
        head = statuses[view.assignment_id].head
        return datetime.min.replace(tzinfo=UTC) if head is None else head.reported_at

    def lately(view: AssignmentUpdateView) -> bool:
        """Whether her latest event under the assignment, a correction included, falls in
        the window: an old update she puts back today is recent activity, dated as the
        old update it is, and so is taking back her only update, which leaves none."""
        head = statuses[view.assignment_id].head
        return head is not None and head.reported_on > today - timedelta(days=RECENT_DAYS)

    recent = sorted(
        (view for view in views.values() if view.assignment_id not in shown and lately(view)),
        key=latest_at,
        reverse=True,
    )
    shown |= {view.assignment_id for view in recent}
    school = [
        view
        for view in views.values()
        if view.assignment_id not in shown and view.school_statements
    ]
    return AssignmentUpdatesView(check=check, checked=checked, recent=recent, school=school)


def update_view(item: Assignment, status: AssignmentStatus) -> AssignmentUpdateView:
    """One row of the section: her account, the school's, and the family's check, read apart.

    A check that stands in the record against facts that differ now, a
    check event at the head that marks checked something other than what
    there is to check, is said with what differs, her Done or the school's
    statements, and the row says what makes it worth checking again.
    """
    standing = status.check
    before = status.check_head if status.needs_a_check else None
    before = before if before is not None and before.operation == CHECKED else None
    new_done = new_missing = False
    if before is not None and status.check_basis is not None:
        then, now = basis_parts(before.basis), basis_parts(status.check_basis)
        new_done = then[1] != now[1]
        new_missing = then[2] != now[2]
    return AssignmentUpdateView(
        assignment_id=item.assignment_id,
        course=item.course,
        title=item.title,
        status=status.status,
        reported_on=status.reported_on,
        restored_on=status.restored_on,
        cleared_on=status.cleared_on,
        note=status.note,
        school_statements=[
            SchoolStatementView.from_report(report) for report in status.school_statements
        ],
        check=status.check_the_school_record,
        basis=status.check_basis,
        check_head_id=status.check_head_id,
        checked=standing is not None,
        checked_on=None if standing is None else standing.checked_on,
        check_note=None if standing is None else standing.note,
        check_id=None if standing is None else standing.check_id,
        checked_before_on=None if before is None else before.checked_on,
        new_done=new_done,
        new_missing=new_missing,
    )


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def review(
    request: Request,
    state: State,
    refreshed: Annotated[
        str | None, Query(description="1 after a refresh, to say when; changes nothing else")
    ] = None,
    added: Annotated[
        str | None, Query(description="how many assignments the last save added; a note")
    ] = None,
    updated: Annotated[
        str | None, Query(description="how many saved assignments the last save changed; a note")
    ] = None,
    unchanged: Annotated[
        str | None, Query(description="how many the last save left as they were; a note")
    ] = None,
    checked: Annotated[
        str | None, Query(description="the assignment whose row a check was just made on")
    ] = None,
    checked_already: Annotated[
        str | None, Query(description="the assignment whose row was checked already")
    ] = None,
    reopened: Annotated[
        str | None, Query(description="the assignment whose check was just reopened")
    ] = None,
) -> HTMLResponse:
    """The parent's page: what she asked for, what is waiting, and the folds below."""
    said = [
        (CHECK_CONFIRMATIONS[name], value)
        for name, value in (
            ("checked", checked),
            ("checked_already", checked_already),
            ("reopened", reopened),
        )
        if value
    ]
    return review_page(
        request,
        state,
        refreshed=refreshed == "1",
        added=a_count(added),
        updated=a_count(updated),
        unchanged=a_count(unchanged),
        check=CheckState(said[0][1], said=said[0][0]) if said else None,
    )


def a_count(given: str | None) -> int | None:
    """A count from the address, or none: anything that is not a count, a number below
    zero included, is no note."""
    try:
        count = None if given is None else int(given)
    except ValueError:
        return None
    return None if count is not None and count < 0 else count


@router.post("/actions/plan", response_class=HTMLResponse, include_in_schema=False)
async def plan_from_the_page(
    request: Request,
    state: State,
    graphs: Graphs,
    plan_date: Annotated[str, Form()] = "",
) -> Response:
    """The plan form. A blank date means today; a bad one is said, not guessed at.

    A run that fails on the way for any reason other than a refusal is said on
    the page too, with the queue below unchanged; the run has already taken
    back what it left, and the failure goes to the process log. An evening that
    has passed is refused before anything runs, since a plan for it could
    reach no page of hers, and so is one past the edge of the calendar, whose
    week cannot be read: the same two refusals the JSON route makes.
    """
    try:
        evening = date.fromisoformat(plan_date) if plan_date.strip() else state.clock.today()
    except ValueError:
        return review_page(
            request,
            state,
            problem=f"{plan_date!r} is not a date. Use the form YYYY-MM-DD.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if evening < state.clock.today():
        return review_page(
            request,
            state,
            problem=passed(evening),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if evening > date.max - CALENDAR_MARGIN:
        return review_page(
            request,
            state,
            problem=beyond(evening),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    try:
        require_work(state, evening)
        require_model(graphs)
        run = await run_plan(
            graphs.build(),
            evening,
            state,
        )
        refuse_an_empty_run(run)
    except HTTPException as error:
        return review_page(request, state, problem=str(error.detail), status_code=error.status_code)
    except Exception:
        logger.exception("the plan for %s failed on the way", evening)
        return review_page(
            request,
            state,
            problem=(
                "The plan could not be made: something went wrong on the way. "
                "What is waiting below is unchanged."
            ),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return RedirectResponse("/parent", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/actions/help/{request_id}", response_class=HTMLResponse, include_in_schema=False)
def help_from_the_page(
    request: Request,
    request_id: str,
    state: State,
    step: Annotated[str, Form()] = "",
    response: Annotated[str, Form()] = "",
) -> Response:
    """The two buttons under a request, through the same path the JSON routes take."""
    words = response.strip()
    if len(words) > NOTE_MAX_LENGTH:
        return review_page(
            request,
            state,
            problem=(f"A reply is at most {NOTE_MAX_LENGTH} characters; this one is {len(words)}."),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    try:
        move_request(state, request_id, step, words or None)
    except HTTPException as error:
        return review_page(request, state, problem=str(error.detail), status_code=error.status_code)
    return RedirectResponse("/parent", status_code=status.HTTP_303_SEE_OTHER)


def back_to_the_row(said: str, assignment_id: str) -> str:
    """Where a check or a reopening sends a parent: the page, the row, and what happened."""
    return f"/parent?{said}={assignment_id}#update-{assignment_id}"


@router.post(
    "/actions/checks/{assignment_id}/mark", response_class=HTMLResponse, include_in_schema=False
)
async def mark_checked_from_the_page(
    request: Request, assignment_id: str, state: State
) -> Response:
    """Mark checked: a parent records that the discrepancy on one assignment was checked
    with her, and nothing else.

    The form is read whole before anything else: its three fields, each
    once, and nothing more. It carries the basis the row was made against,
    the assignment, the report that began her Done, and each school
    statement of missing, and the last check event the row showed. Under
    the decision lock and the store's, in one transaction that reserves the
    writer before it reads, the basis is worked out again from her events
    and the school's reports as they stand and compared with the form's,
    and the check event with the head: the same check standing already is
    already made, with no write and no new day; a basis that differs, or a
    record that moved on, is answered 409 with the row as it stands now and
    the note typed kept; otherwise the check is appended. A write the file
    refuses is answered with the page and the note, and never with a word
    of a check. The gate admits only a parent's device to this path, so her
    device is answered 403 before this runs; with the sign-in off, whoever
    is at the keyboard is the family. Her events, the school's reports, the
    plans, and the digest are untouched by any answer here.
    """
    fields, whole = await fields_of(request, CHECK_FIELDS)
    basis = fields.get("basis", "").strip()
    token = fields.get("expected_check_id", "").strip()
    note = fields.get("note", "")
    words = normalize_note(note)

    def refused(problem: str, code: int, *, field: str | None = None) -> Response:
        return review_page(
            request,
            state,
            check=CheckState(
                assignment_id, problem=problem, field=field, note=note if whole else ""
            ),
            status_code=code,
        )

    if not whole:
        return refused(BAD_CHECK_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if not basis or len(basis) > BASIS_MAX_LENGTH or len(token) > TOKEN_MAX_LENGTH:
        return refused(NOT_THIS_ROWS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if words is not None and len(words) > CHECK_NOTE_MAX_LENGTH:
        return refused(CHECK_NOTE_TOO_LONG, status.HTTP_422_UNPROCESSABLE_CONTENT, field="note")
    store = state.project_state
    try:
        async with state.decision_lock:
            result = store.mark_checked(
                assignment_id,
                basis,
                words,
                expected_check=token or None,
                basis_now=lambda: statuses_for(store, [assignment_id])[assignment_id].check_basis,
                now=state.clock.now(),
                today=state.clock.today(),
            )
    except UnknownAssignment:
        return review_page(
            request, state, problem=NOT_ON_RECORD, status_code=status.HTTP_404_NOT_FOUND
        )
    except UnknownCheck:
        return refused(NOT_THIS_ROWS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    except NoteTooLong:
        return refused(CHECK_NOTE_TOO_LONG, status.HTTP_422_UNPROCESSABLE_CONTENT, field="note")
    except CouldNotSave:
        logger.exception("the check on %s could not be saved", assignment_id)
        return refused(CHECK_NOT_SAVED, status.HTTP_500_INTERNAL_SERVER_ERROR)
    match result:
        case Checked():
            return RedirectResponse(
                back_to_the_row("checked", assignment_id), status_code=status.HTTP_303_SEE_OTHER
            )
        case AlreadyChecked():
            return RedirectResponse(
                back_to_the_row("checked_already", assignment_id),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        case CheckConflict(head=head):
            moved = (None if head is None else head.check_id) != (token or None)
            return refused(
                CHECK_MOVED_ON if moved else CHECK_FACTS_CHANGED, status.HTTP_409_CONFLICT
            )


@router.post(
    "/actions/checks/{assignment_id}/again", response_class=HTMLResponse, include_in_schema=False
)
async def check_again_from_the_page(request: Request, assignment_id: str, state: State) -> Response:
    """Check again: a parent reopens the family's check on one assignment, and nothing else.

    The form carries the check it reopens, which must be this assignment's
    and at the head of its record, or the record moved on since the page
    was made and the answer is 409 with the row as it stands. The check
    reopened stays in the record; her update and the school's report are
    untouched, and the row is worth checking together again while her Done
    stands beside a Missing. The gate admits only a parent's device here.
    """
    fields, whole = await fields_of(request, AGAIN_FIELDS)
    token = fields.get("check_id", "").strip()

    def refused(problem: str, code: int) -> Response:
        return review_page(
            request, state, check=CheckState(assignment_id, problem=problem), status_code=code
        )

    if not whole:
        return refused(BAD_CHECK_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if not token or len(token) > TOKEN_MAX_LENGTH:
        return refused(NOT_THIS_ROWS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    try:
        async with state.decision_lock:
            result = state.project_state.check_again(
                assignment_id, token, now=state.clock.now(), today=state.clock.today()
            )
    except UnknownAssignment:
        return review_page(
            request, state, problem=NOT_ON_RECORD, status_code=status.HTTP_404_NOT_FOUND
        )
    except UnknownCheck:
        return refused(NOT_THIS_ROWS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    except CouldNotSave:
        logger.exception("the check on %s could not be reopened", assignment_id)
        return refused(CHECK_NOT_REOPENED, status.HTTP_500_INTERNAL_SERVER_ERROR)
    match result:
        case Reopened():
            return RedirectResponse(
                back_to_the_row("reopened", assignment_id), status_code=status.HTTP_303_SEE_OTHER
            )
        case CheckConflict():
            return refused(CHECK_MOVED_ON, status.HTTP_409_CONFLICT)


@router.post("/actions/decide/{draft_id}", response_class=HTMLResponse, include_in_schema=False)
async def decide_from_the_page(
    request: Request,
    draft_id: str,
    state: State,
    graphs: Graphs,
    decision: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    """The two buttons under a waiting draft, through the same path the JSON route takes.

    The field is read as text and checked here rather than typed as a literal,
    because the framework's own validation would answer a bad value with a
    JSON error, and a form failure is promised as this page with the problem.
    """
    if decision not in DECISIONS:
        return review_page(
            request,
            state,
            problem=f"{decision!r} is not one of the two buttons, approve or refuse.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    reason = reason.strip()
    if len(reason) > REASON_MAX_LENGTH:
        return review_page(
            request,
            state,
            problem=(
                f"A reason is at most {REASON_MAX_LENGTH} characters; this one is {len(reason)}."
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    decided = DecisionRequest(approved=decision == "approve", reason=reason or None)
    try:
        await decide_draft(state, graphs.build, draft_id, decided)
    except HTTPException as error:
        return review_page(request, state, problem=str(error.detail), status_code=error.status_code)
    return RedirectResponse("/parent", status_code=status.HTTP_303_SEE_OTHER)
