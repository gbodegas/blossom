# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
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
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from blossom.agent.retention import unresumable_threads
from blossom.agent.runs import (
    DURABILITY,
    StaleGraphVersion,
    Unfinished,
    bounded,
    ensure_current_version,
    run_config,
)
from blossom.anthropic_client import model_configured
from blossom.assignment_status import AssignmentStatus, basis_parts, statuses_for
from blossom.captures import what_remains
from blossom.clock import local_now
from blossom.dependencies import ApplicationState, get_application_state
from blossom.evening import PlanUpdates, Staleness, limit_now, plan_updates, staleness
from blossom.hand_in import NEEDS_HAND_IN
from blossom.intake import NOTE_MAX_LENGTH as ENTRY_NOTE_MAX_LENGTH
from blossom.intake import TEXT_MAX_LENGTH
from blossom.noticing import Everything, read_everything
from blossom.pairing import pair
from blossom.plan_dates import DatesNow, dates_now
from blossom.plan_reading import DoneMark, PlanReading, read_plan
from blossom.routes.forms import TOKEN_MAX_LENGTH, FormRoute, fields_of
from blossom.routes.navigation import (
    ADDED_NOTES_PAGE,
    ARCHIVED_NOTES_PAGE,
    EVIDENCE,
    FAMILY_PAGE,
    address,
    asked_address,
    details_href,
    instructions_review_href,
    segment,
)
from blossom.routes.runs import (
    CHECK_AGAIN,
    CHECK_ON_THAT_REQUEST,
    COULD_NOT_START,
    PLAN_ANSWERS,
    RUN_STATUS_ANSWERS,
    UNCONFIRMED,
    AlreadyPlanning,
    CouldNotStart,
    Graphs,
    NotSaved,
    PlanGraphBuilder,
    RunCheck,
    RunNotice,
    Unconfirmed,
    already_planning,
    ended_without_a_plan,
    graph_for_a_run,
    make_plan,
    not_saved,
    refuse_an_empty_run,
    require_model,
    require_work,
    run_check,
    run_notice,
    run_status_view,
    saved_sentence,
    thread_for,
    tidy_later,
    tidy_thread,
)
from blossom.routes.student import help_view, notes_named_by
from blossom.settings import CALENDAR_MARGIN
from blossom.stores.drafts import (
    INTERRUPTED,
    STORE_WAIT_SECONDS,
    AlreadyDecided,
    DraftRecord,
    NotPublished,
)
from blossom.stores.help_requests import (
    NOTE_MAX_LENGTH,
    AlreadyTakenUp,
    HelpRequest,
    NotARequestId,
    NotTakenUp,
    RequestClosed,
    UnreadableHelpRequest,
    UpdateFormUsed,
    UpdateTooLong,
    UpdateWithoutWords,
    kept_words,
    new_update_id,
    update_id_from,
)
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
    HandInRowView,
    HandInView,
    HelpRequestView,
    NamedAssignmentView,
    ParentCheckpointAssignmentView,
    ParentCheckpointView,
    PlanRunView,
    RunStatusView,
    RunView,
    SchoolStatementView,
    SchoolWordsView,
    school_words,
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


def over_the_limit(minutes: int) -> str:
    """The family page's notice for a waiting plan whose blocks are over today's limit of
    ``minutes``, which is not approved as it stands."""
    return f"This plan is longer than today's {minutes}-minute limit. Plan again before approving."


PLAN_INCLUDES_DONE: Final = "This plan includes work she now reports as Done."
PLAN_WINDOW_DONE: Final = "Some work in this plan's window is now reported Done."
FAMILY_UNREADABLE_WHY: Final = (
    "The record cannot be read right now, so reviews, updates and current date information "
    "are not shown. Try again in a moment."
)
RECENT_DAYS: Final = 14
"""How many household days an update of hers stays under "Recent updates", and a check of
the family's under "Checked recently"."""
CHECK_FIELDS: Final = frozenset({"basis", "expected_check_id", "note"})
AGAIN_FIELDS: Final = frozenset({"check_id", "basis"})
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
NEW_DONE: Final = "her Done is a new one"
NEW_MISSING: Final = "the school's report listed is not the one checked then"
DONE_GONE: Final = "her update is not Done"
MISSING_GONE: Final = "no school channel reports it missing"
"""What can differ from the facts a check was made against, as the row says it."""
CHECK_CONFIRMATIONS: Final[dict[str, str]] = {
    "checked": CHECK_RECORDED,
    "checked_already": CHECK_ALREADY,
    "reopened": CHECK_REOPENED,
}
"""What the address says happened to a row's check, and the sentence the row shows for it:
the server chooses which, the address only carries the choice."""
PLAN_INTERRUPTED: Final = ended_without_a_plan(INTERRUPTED, parent=True)
"""What the family's plan form says of a run that failed on the way: a plan Blossom couldn't
finish, as the record keeps it, in a parent's words."""
FAMILY_NOT_SHOWN: Final = "Family review can't be shown right now."
"""The line the family page's stand-in adds after a refusal's own words when the family
page can't be read."""
HELP_STEP_NOT_SAVED: Final = (
    "That could not be saved, and nothing was changed. Your reply is below. Try again."
)
STEP_NOT_SAVED: Final = "That could not be saved, and nothing was changed. Try again."
"""What a parent's move on her request the file refused says, with and without words: the
store rolls back a write it could not finish, a refused commit included."""
ALREADY_TAKEN_UP: Final = (
    "This request was already taken up. Your words weren't added; they're below in Add an update."
)
CLOSED_BEFORE_UPDATE: Final = "This request was closed before your update was added."
ALREADY_CLOSED: Final = "This request is already closed."
FORM_ADDED_OTHER_WORDS: Final = (
    "This page already sent a different update. Your new message wasn't added. It is below in "
    "Add an update."
)
NOT_TAKEN_UP_YET: Final = (
    "Choose I can help before adding an update. Your message wasn't added and is kept below."
)
UPDATE_NEEDS_WORDS: Final = "Enter a message before adding an update."
"""What a move on her request says when the request's state or the form refuses it. Each
writes nothing, and the family page keeps the words as typed."""
WORDS_BELOW: Final = frozenset({ALREADY_TAKEN_UP, FORM_ADDED_OTHER_WORDS, NOT_TAKEN_UP_YET})
"""The refusals that say the words are below, in the request's own box, with a way to it."""
WITHOUT_THE_PAGE: Final[dict[str, str]] = {
    CHECK_MOVED_ON: (
        "This row was marked checked or reopened from another device since this page was made. "
        "Nothing was written."
    ),
    CHECK_FACTS_CHANGED: (
        "What this row rests on has changed since this page was made: her update, or the "
        "school's report. Nothing was written."
    ),
    CHECK_NOTE_TOO_LONG: (
        f"Nothing was written, because the note is longer than {CHECK_NOTE_MAX_LENGTH} characters."
    ),
    PLAN_INTERRUPTED: (
        "Blossom couldn't finish a reliable plan this time. Her homework updates are saved."
    ),
    ALREADY_TAKEN_UP: "This request was already taken up. Your words weren't added.",
    FORM_ADDED_OTHER_WORDS: (
        "This page already sent a different update. Your new message wasn't added."
    ),
    NOT_TAKEN_UP_YET: "Choose I can help before adding an update. Your message wasn't added.",
}
"""Each refusal written for the family page, as it reads where that page is not shown: on its
stand-in, and in a JSON answer, which names no place on a page. Any other refusal reads the
same in every place."""


@dataclass(frozen=True)
class FamilyKept:
    """What a parent typed into a refused form, shown on the family page's stand-in to copy:
    a check's note, a reply to her request, or the reason for a decision."""

    note: str = ""
    reply: str = ""
    reason: str = ""


@dataclass(frozen=True)
class HelpReplyKept:
    """Words for her request that a refused press sends back to the family page as typed: the
    request they were for, the words, whether the words are what was refused, and whether
    the refusal says they are below, in the request's own box."""

    request_id: str
    reply: str
    at_reply: bool = False
    below: bool = False


@dataclass(frozen=True)
class CheckState:
    """What one row of the assignment updates shows beyond the record: what a check form did,
    a problem with it, which field the problem is about, and the note typed, kept."""

    assignment_id: str
    said: str | None = None
    problem: str | None = None
    field: str | None = None
    note: str = ""


def stale_reason(
    state: ApplicationState,
    record: DraftRecord,
    *,
    today: date | None = None,
    everything: Everything | None = None,
) -> str | None:
    """Why a waiting draft has stopped fitting the evening, or ``None`` while it fits.

    A draft is made for the evening as she had described it when the run
    read it. When her signal as it stands is not that one, or its blocks as
    saved are over the limit set now, the plan on the page is not the plan the
    checks held to the current budget, so it is not approved as it stands.
    Neither message about her signal says which came first: her signal can
    change while a run is still on its way to the draft, so the pages say
    only that the two do not match.
    Refusing it is still allowed; refusing never sends anything. Only a
    waiting draft can be stale: a decided one is a record of what was decided,
    and is not measured against the evening again.

    A signal that is gone was either taken back or aged out of the store, and
    the store does not say which, so the message names both rather than
    putting an action on her that she may not have taken. A draft for an
    evening that has passed is never stale: it cannot be planned again, since
    the pages refuse a past evening, and it reaches no page of hers, so there
    is nothing a fresh plan would put right. ``everything`` is the record as
    the caller's page read it, which the week is measured from; left out,
    the record is read when the week is reached.
    """
    today = state.clock.today() if today is None else today
    if not record.waiting or record.plan_date < today:
        return None
    match staleness(
        state.workload_signals,
        record,
        state.project_state,
        settings=state.settings,
        zone=state.clock.zone,
        everything=everything,
    ):
        case Staleness.SIGNALED_SINCE:
            return SIGNALED_SINCE
        case Staleness.SIGNAL_ENDED:
            return SIGNAL_ENDED
        case Staleness.OVER_THE_LIMIT:
            return over_the_limit(limit_now(record, state.settings))
        case Staleness.ASSIGNMENTS_CHANGED:
            return ASSIGNMENTS_CHANGED
        case None:
            return None


def is_current(record: DraftRecord, today: date, latest: DraftRecord | None) -> bool:
    """Whether ``record`` is today's working plan: the last draft published for today that
    no later one displaced, whatever was decided about it. Never the newest by a clock,
    a decision, or a title; an earlier plan for today is history, and so is every plan
    for another day."""
    return latest is not None and record.plan_date == today and latest.draft_id == record.draft_id


def done_in(
    found: PlanUpdates | None,
) -> tuple[str, list[NamedAssignmentView]] | None:
    """That today's plan includes work she reports as done as things stand, and which work.

    Said only for the plan her page shows as today's, whatever was decided
    about it: earlier plans are history and get no notice from an update
    made today, and a plan a later one took the place of is one of those;
    for those nothing was read and ``found`` is ``None``. What is compared
    is the plan's ids and her updates as they stand, never the time of
    either.
    """
    if found is None or found.done is None:
        return None
    if not found.done.known:
        return PLAN_WINDOW_DONE, []
    return PLAN_INCLUDES_DONE, [
        NamedAssignmentView(assignment_id=name, title=title) for name, title in found.done.named
    ]


@dataclass(frozen=True)
class PlanRead:
    """One plan read once for the family page: the parent's view of it, and how the page
    shows it."""

    view: ApprovalView
    reading: PlanReading


def read_a_plan(
    state: ApplicationState,
    record: DraftRecord,
    *,
    current: bool,
    everything: Everything | None = None,
    today: date | None = None,
    dates: DatesNow | None = None,
) -> PlanRead:
    """A draft as the parent sees it, whether it still fits the evening, and its reading.

    The notice, the stale state, and the marks beside the rows come from
    one reading of the record, ``everything``, so a report landing between
    them cannot leave them at odds and her reports are read once for all
    three. The family page reads it once for every plan it shows and for
    the assignment updates below them; a caller with one plan leaves it out,
    and the record is read here when today's working plan or a waiting one
    needs it. Her updates are shown only for today's working plan; any
    other plan is history, read with no updates at all, and takes from the
    reading only which ids are on record. ``today`` is the household day
    the caller's page read, once, so which plan is current and whether a
    plan has passed are about the same day. ``dates`` is what stands about
    the dates in the same reading, handed in for a plan in force for its
    evening, today's or a later one's, whatever was decided about it.
    """
    store = state.project_state
    with store.exclusively():
        if everything is None and (current or record.waiting):
            everything = read_everything(store, store, also=record.plan_assignment_ids or ())
        updates = plan_updates(store, record, everything=everything) if current else None
        included = done_in(updates)
        stale = stale_reason(state, record, today=today, everything=everything)
    marks = (
        {}
        if updates is None
        else {
            name: DoneMark(reported_on=found.reported_on, restored_on=found.restored_on)
            for name, found in updates.statuses.items()
            if not found.needs_homework and found.reported_on is not None
        }
    )
    unread = frozenset() if updates is None else updates.unread
    view = ApprovalView.from_record(
        record,
        stale=stale,
        reported_done=None if included is None else included[0],
        reported_done_work=[] if included is None else included[1],
    )
    reading = read_plan(
        record,
        reader="family",
        current=current,
        link_for=lambda name: details_href(name, return_to="family", plan_id=record.draft_id),
        evidence_for=lambda name: details_href(
            name, fragment=EVIDENCE, return_to="family", plan_id=record.draft_id
        ),
        on_record=None if everything is None else everything.ids,
        done=marks,
        unread=unread,
        now=dates,
    )
    return PlanRead(view=view, reading=reading)


def approval_view(state: ApplicationState, record: DraftRecord) -> ApprovalView:
    """A draft as the parent sees it, with whether it still fits the evening."""
    today = state.clock.today()
    current = is_current(record, today, state.drafts.latest_for(today))
    return read_a_plan(state, record, current=current, today=today).view


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


@router.post(
    "/plans",
    response_model=PlanRunView,
    status_code=status.HTTP_201_CREATED,
    responses=PLAN_ANSWERS,
)
async def start_plan(
    request: PlanRequest, state: State, graphs: Graphs
) -> PlanRunView | JSONResponse:
    """Run the plan graph for one evening, up to the gate or to the reason it stopped.

    An evening that has passed, or one past the edge of the calendar, is
    refused with 422 before anything runs, and so is an evening with nothing
    left to plan, before and after the run: a report of hers can land between
    the question and the run's reading, and such a run made no plan. Any other
    run that ended without a plan is answered 201 with its record, since the
    page lists those, a run that ended on a date problem before any model was
    asked, or one that failed on the way, included. A press while the
    household's run is still running is a 409 naming it, in a parent's words; a
    plan whose saving couldn't be confirmed in time is a 202 with its run's id;
    and a plan not saved or a run that couldn't start is a 503 that says her
    updates are saved.
    """
    budget = graphs.budget()
    evening = request.plan_date or state.clock.today()
    if evening < state.clock.today():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=passed(evening))
    if evening > date.max - CALENDAR_MARGIN:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=beyond(evening))
    could_not_start = f"{COULD_NOT_START} {saved_sentence(parent=True)}"
    try:
        await require_work(state, evening, budget)
    except CouldNotStart as error:
        raise HTTPException(error.status_code, detail=could_not_start) from None
    require_model(graphs)
    run_id = thread_for(evening)
    try:
        made = await make_plan(
            graph_for_a_run(graphs), evening, state, budget=budget, run_id=run_id
        )
    except AlreadyPlanning as error:
        raise AlreadyPlanning(error.run, parent=True) from None
    except NotSaved as error:
        raise HTTPException(
            error.status_code, detail=not_saved(parent=True, kept=error.kept)
        ) from None
    except CouldNotStart as error:
        raise HTTPException(error.status_code, detail=could_not_start) from None
    except Unconfirmed as unconfirmed:
        return JSONResponse(
            unconfirmed.view().model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED
        )
    except HTTPException:
        raise
    except Exception:
        # The run was admitted and has been ended interrupted: its record, like any run
        # that ended without a plan.
        logger.exception("the plan for %s failed on the way", evening)
        return PlanRunView(
            thread_id=run_id,
            plan_date=evening,
            outcome=INTERRUPTED,
            draft_id=None,
            waiting=False,
            steps=list(budget.steps),
        )
    refuse_an_empty_run(made.view)
    return made.view


@router.get("/plans/runs/{run_id}", response_model=RunStatusView, responses=RUN_STATUS_ANSWERS)
async def plan_run(run_id: str, state: State) -> RunStatusView:
    """Where one planning run stands, after ending any run past its deadline."""
    return await run_status_view(state, run_id)


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


def unresumable(plan_date: date, today: date) -> str:
    """What a decision on a published plan answers, 409, when its saved review step is
    missing or was written by another version of the graph, so nothing can resume it: why,
    and what becomes of the plan, which stays on her page only for today's evening."""
    then = (
        "It stays on her page until a new plan replaces it, and closes as expired two weeks "
        "after its evening."
        if plan_date == today
        else "It closes as expired two weeks after its evening."
    )
    return (
        "This plan can't be approved or refused here, because Blossom can't finish its saved "
        f"review step. {then}"
    )


RECORD_TOO_SLOW: Final = "The record couldn't be read in time. Try again in a moment."


async def read_for_the_decision[T](call: Callable[[], T]) -> T:
    """What ``call`` returns, read on a worker thread for at most the store's wait, so the
    server goes on answering while the decision lock is held; 503 past it."""
    try:
        return await bounded(call, STORE_WAIT_SECONDS)
    except Unfinished as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=RECORD_TOO_SLOW) from error


async def decide_draft(
    state: ApplicationState, build: PlanGraphBuilder, draft_id: str, request: DecisionRequest
) -> DecisionView:
    """The decision, from the table check to the resumed thread, under one lock.

    The table is read first, so an unknown, already decided or unpublished draft
    is refused without a graph and therefore without a key. The graph is built
    only after that, and asked whether the thread is still waiting at the gate
    and was written by this version, since the table can say a draft waits while
    the thread has moved on; one that can't be resumed is refused with
    ``unresumable``'s words. The lock spans the whole sequence: two requests about one
    draft cannot both see it waiting, and the table's own refusal covers a
    second process. Every read of the record runs on a worker thread.

    A thread past the gate with its record unwritten is a review that reached
    the thread and then failed to land in the table. It is finished with the
    decision the thread holds rather than given a new one, whatever the request
    says and whatever the evening's signal says now, since the review was
    checked against the evening when it was given; a request that disagrees
    with what stood is told so.
    """
    async with state.decision_lock:
        record = await read_for_the_decision(partial(state.drafts.get, draft_id))
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no draft {draft_id!r}")
        if not record.waiting:
            if record.decision == "superseded":
                # Nothing can resume a superseded draft's thread, and a settle this process
                # stopped waiting for may have left it; it goes without a wait.
                tidy_later(record.thread_id, state)
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"draft {draft_id!r} was already {record.decision}",
            )
        if not record.published:
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail=f"draft {draft_id!r} is not on the pages"
            )
        graph = build()
        config = run_config(record.thread_id, callbacks=[state.tracer])
        snapshot = await graph.aget_state(config)
        resume: Command[Any] | None
        if snapshot.next == ("require_human_approval",):
            stale = await read_for_the_decision(partial(stale_reason, state, record))
            if request.approved and stale is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, detail=stale)
            resume = Command(resume={"approved": request.approved, "reason": request.reason})
        elif snapshot.next == ("record_decision",):
            resume = None
        else:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=unresumable(record.plan_date, state.clock.today()),
            )
        try:
            ensure_current_version(snapshot)
        except StaleGraphVersion as error:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=unresumable(record.plan_date, state.clock.today()),
            ) from error
        try:
            await graph.ainvoke(resume, config=config, durability=DURABILITY)
        except (AlreadyDecided, NotPublished) as error:
            # The review reached the thread, so the gate is passed and the
            # thread cannot pause again, and the table refused because the
            # draft was closed meanwhile: a later plan published from another
            # process, or from a settle this process stopped waiting for. Nothing
            # can resume the thread, so it goes, and a thread that cannot be
            # cleared now is left to the sweep.
            await tidy_thread(record.thread_id, state)
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        decided = await read_for_the_decision(partial(state.drafts.get, draft_id))
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
    """A parent's move on a request, with words if any, and the id of the form that sends it.
    Without an id, every call moves as a form of its own, so retrying one isn't safe. The words
    are held to the cap as the store keeps them, as the family page's are."""

    model_config = ConfigDict(extra="forbid")

    response: str | None = None
    update_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


def reply_too_long(length: int) -> str:
    """What both routes say of a parent's words past the cap, counted as they would be kept."""
    return f"A reply is at most {NOTE_MAX_LENGTH} characters; this one is {length}."


def move_request(
    state: ApplicationState,
    request_id: str,
    step: str,
    response: str | None,
    update_id: str | None = None,
) -> HelpRequest:
    """Take a request up, add an update to it, or close it. Words past the cap are 422 before
    anything is read; then an unknown request is 404, an update with no words 422, and a move
    its state or its form refuses 409. Any other step is 422."""
    store = state.help_requests
    try:
        if step == "accept":
            return store.accept(request_id, response, update_id=update_id)
        if step == "update":
            return store.add_update(request_id, response, update_id=update_id)
        if step == "resolve":
            return store.resolve(request_id, response, update_id=update_id)
    except UpdateTooLong as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=reply_too_long(error.length)
        ) from error
    except KeyError as error:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"no help request {request_id!r}"
        ) from error
    except RequestClosed as error:
        said = CLOSED_BEFORE_UPDATE if step == "update" else ALREADY_CLOSED
        raise HTTPException(status.HTTP_409_CONFLICT, detail=said) from error
    except AlreadyTakenUp as error:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=ALREADY_TAKEN_UP) from error
    except UpdateFormUsed as error:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=FORM_ADDED_OTHER_WORDS) from error
    except NotTakenUp as error:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=NOT_TAKEN_UP_YET) from error
    except UpdateWithoutWords as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=UPDATE_NEEDS_WORDS
        ) from error
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=f"{step!r} is not one of the three moves, accept, update or resolve.",
    )


def step_of(payload: HelpStep | None) -> tuple[str | None, str | None]:
    """The words and the form's id a JSON move carries, none when it carries no body."""
    return (None, None) if payload is None else (payload.response, payload.update_id)


def moved_over_json(
    state: ApplicationState, request_id: str, step: str, payload: HelpStep | None
) -> HelpRequestView:
    """A move a JSON route makes, as the request reads after it, with a refusal said as it
    reads where the family page is not shown."""
    try:
        moved = move_request(state, request_id, step, *step_of(payload))
    except HTTPException as error:
        said = str(error.detail)
        raise HTTPException(error.status_code, detail=WITHOUT_THE_PAGE.get(said, said)) from error
    return help_view(state, moved, notes_named_by(state, [moved]))


@router.get("/help-requests")
def help_requests(state: State) -> list[HelpRequestView]:
    """Every request she has open, oldest first, then those resolved within two weeks, each
    with the note it is about, read once for all of them."""
    asked = [*state.help_requests.open_requests(), *state.help_requests.recently_resolved()]
    named = notes_named_by(state, asked)
    return [help_view(state, request, named) for request in asked]


@router.post("/help-requests/{request_id}/accept")
def accept_help_request(
    request_id: str, state: State, payload: Annotated[HelpStep | None, Body()] = None
) -> HelpRequestView:
    """Take a request up, so her page says a parent is on it, any words its first update."""
    return moved_over_json(state, request_id, "accept", payload)


@router.post("/help-requests/{request_id}/update")
def update_help_request(
    request_id: str, state: State, payload: Annotated[HelpStep | None, Body()] = None
) -> HelpRequestView:
    """Add an update to a request a parent has taken up; it stays open."""
    return moved_over_json(state, request_id, "update", payload)


@router.post("/help-requests/{request_id}/resolve")
def resolve_help_request(
    request_id: str, state: State, payload: Annotated[HelpStep | None, Body()] = None
) -> HelpRequestView:
    """Close a request, any words its last update; her page shows it for two weeks."""
    return moved_over_json(state, request_id, "resolve", payload)


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
    kept_note: bool = False,
    linked: int | None = None,
    paste: str | None = None,
    entry: Mapping[str, str] | None = None,
    entry_open: bool = False,
    check: CheckState | None = None,
    open_plan: str | None = None,
    problem_check: RunCheck | None = None,
    run_notice: RunNotice | None = None,
    help_reply: HelpReplyKept | None = None,
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
    ``open_plan`` is a plan a link came back to, whose folds are opened.
    ``problem_check`` links the problem to the run it names, and ``run_notice`` is what
    the page says about a planning run. A waiting plan no review could resume says so in
    place of its two buttons, as the decision itself refuses it. ``help_reply`` is the
    words a refused press sent back, shown in its request's box while that request is
    open on the page, and under the problem otherwise. Each open request's form gets a
    fresh id of its own, so the same form sent again is known as one.

    The household day is read once, and today's working plan with it: the
    last draft published for today that no later one displaced. That one
    plan is read as current, with her updates beside its rows, whatever was
    decided about it. While it waits it stays in the review queue; once it
    is decided it is shown under a heading of its own after the queue, and
    left out of the earlier plans for that render, so it is on the page
    once. Every other plan is history and shows none of her updates.
    """
    about_a_row = check is not None and check.problem is not None and problem is None
    # The household day is read once for the page. The drafts are read once
    # too, in one reading of the table: what waits, what was decided, and
    # which draft is today's working plan, so every list below is made from
    # records in hand and the page agrees with itself whatever is published
    # or decided while it is being built. The record is read once as well,
    # with today's working plan's assignments named to it: her updates
    # beside that plan, whether each waiting plan still fits the week, which
    # ids are on record for the plans shown as history, and the assignment
    # updates below them all come out of that reading.
    today = state.clock.today()
    records = state.drafts.review_snapshot(today)
    shown = (*records.waiting, *records.decided)
    # Kept for a page that cannot read the record after this, which shows them as saved.
    request.state.plans_read = [record for record in shown if record.draft_id in records.operative]
    working = next((record for record in shown if record.draft_id == records.current_id), None)
    # What she added as homework notes is read beside the record, in the same snapshot,
    # and is no part of it. The notes her requests are about come in one more statement.
    asked = [*state.help_requests.open_requests(), *state.help_requests.recently_resolved()]
    with state.project_state.reading():
        everything = read_everything(
            state.project_state,
            state.project_state,
            also=() if working is None else working.plan_assignment_ids or (),
        )
        notes = state.project_state.outstanding_captures()
        named = notes_named_by(state, asked)
    # What stands about the dates is worked out once, from the same reading, for every
    # assignment the plans in force speak about: today's and each later evening's.
    live = [record for record in shown if record.draft_id in records.operative]
    dates = dates_now(
        everything, [name for record in live for name in record.plan_assignment_ids or ()]
    )
    plans = {
        record.draft_id: read_a_plan(
            state,
            record,
            current=record.draft_id == records.current_id,
            everything=everything,
            today=today,
            dates=dates if record.draft_id in records.operative else None,
        )
        for record in shown
    }
    waiting = [plans[record.draft_id].view for record in records.waiting]
    stuck = unresumable_threads(
        state.checkpointer, [record.thread_id for record in records.waiting], STORE_WAIT_SECONDS
    )
    every_decided = [plans[record.draft_id].view for record in records.decided]
    todays = next((view for view in every_decided if view.draft_id == records.current_id), None)
    decided = [view for view in every_decided if view is not todays]
    ended = [RunView.from_record(run) for run in state.drafts.runs_without_a_draft()]
    return templates.TemplateResponse(
        request,
        "parent_review.html",
        {
            "today": today,
            "model_available": model_configured(state.settings),
            "waiting": waiting,
            "decided": decided,
            "todays_decided": todays,
            "open_plan": open_plan if open_plan in plans else None,
            "readings": {name: found.reading for name, found in plans.items()},
            "ended": ended,
            "unresumable": {
                record.draft_id: unresumable(record.plan_date, today)
                for record in records.waiting
                if record.thread_id in stuck
            },
            "problem_check": problem_check,
            "run_notice": run_notice,
            # Open when the latest run of an evening still ahead made no plan, so the
            # parent sees why without looking for it.
            "ended_open": any(run.newest and run.plan_date >= today for run in ended),
            "problem": check.problem if about_a_row and check is not None else problem,
            "problem_target": check.assignment_id if about_a_row and check is not None else None,
            "reason_max_length": REASON_MAX_LENGTH,
            "help_open": [help_view(state, r, named) for r in asked if r.open],
            "help_resolved": [help_view(state, r, named) for r in asked if not r.open],
            "update_ids": {r.request_id: new_update_id() for r in asked if r.open},
            "help_reply": help_reply,
            "help_reply_listed": help_reply is not None
            and any(r.open and r.request_id == help_reply.request_id for r in asked),
            "homework_notes": notes.notes,
            "homework_notes_unreadable": notes.unreadable,
            "remains": what_remains(
                notes.notes, {pair(item.course, item.title) for item in everything.assignments}
            ),
            "archived_notes_page": ARCHIVED_NOTES_PAGE,
            "added_notes_page": ADDED_NOTES_PAGE,
            "note_max_length": NOTE_MAX_LENGTH,
            "marks": state.settings.page_marks,
            "zone": state.clock.zone,
            "refreshed_at": local_now(state.clock.zone) if refreshed else None,
            "added": added,
            "updated": updated,
            "unchanged": unchanged,
            "kept_note": kept_note,
            "linked": linked,
            "problem_field": problem_field,
            "paste": paste or "",
            "entry": dict(entry or {}),
            "entry_open": entry_open or bool(paste) or bool(entry),
            "text_max_length": TEXT_MAX_LENGTH,
            "entry_note_max_length": ENTRY_NOTE_MAX_LENGTH,
            "updates": assignment_updates(everything, today),
            "instructions_to_review": [
                (item, everything.instructions[item.assignment_id].awaiting)
                for item in everything.assignments
                if item.assignment_id in everything.instructions
                and everything.instructions[item.assignment_id].awaiting
            ],
            "instructions_review_href": instructions_review_href,
            "check": check,
            "check_note_max_length": CHECK_NOTE_MAX_LENGTH,
            "check_routes": check_actions,
        },
        status_code=status_code,
    )


def assignment_updates(everything: Everything, today: date) -> AssignmentUpdatesView:
    """What she and the school have reported, in the family page's groups.

    Each assignment is in one group, the first that fits. Her "done" beside
    any school channel's current "missing", with no check of the family's
    standing against it, is worth checking together, and comes first, open,
    however old. Next, folded, the rows a parent marked checked in the last
    fourteen household days, by the check's day, the latest check first,
    while the check stands against the facts as they are. Of the rest, the
    assignments with an event of hers in the last fourteen household days,
    a correction included, follow, by that latest event, most recent first,
    each showing the day of the update that stands, or that none does.
    Every other assignment the school has a current statement about follows,
    in the record's order. A row whose updates or checks cannot be read goes
    where its readable facts put it, and one they put nowhere closes the
    section, in the record's order, so it is never out of sight and never
    dated as recent. Every row whose updates cannot be read, and every row
    whose checks cannot be, is named at the section's head. Whichever group
    a row is in, it shows her update when she has one, what each school
    channel says now, every channel, and the check that stands, so a row
    never leaves out a fact it was grouped by. The rows, her events, the school's reports, and
    the family's checks are the ones the page read, once, for everything it
    says about the record, ``everything``: one snapshot, as her page reads
    them, in a few batched reads whatever the number of rows, so a row here
    and a mark beside a plan's row never disagree.
    """
    rows = everything.assignments
    statuses = everything.statuses
    views = {
        item.assignment_id: update_view(
            item,
            statuses[item.assignment_id],
            school_words(
                item,
                everything.instructions.get(item.assignment_id),
                item.assignment_id in everything.instructions_unavailable,
            ),
        )
        for item in rows
    }
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
    shown |= {view.assignment_id for view in school}
    unreadable = [
        view
        for view in views.values()
        if view.assignment_id not in shown and (view.updates_unavailable or view.checks_unavailable)
    ]
    # What she has said about turning work in rides along on a row worth checking
    # together, as context, and decides nothing about the check.
    check = [
        view.model_copy(update={"hand_in": hand_in_shown(everything, view.assignment_id)})
        for view in check
    ]
    return AssignmentUpdatesView(
        check=check,
        checked=checked,
        recent=recent,
        school=school,
        unreadable=unreadable,
        updates_unreadable=[view for view in views.values() if view.updates_unavailable],
        checks_unreadable=[view for view in views.values() if view.checks_unavailable],
        turning_in=turning_in(everything, today),
    )


def hand_in_shown(everything: Everything, assignment_id: str) -> HandInView | None:
    """What she has said about turning one assignment in, when there is anything to show:
    a statement of hers, or a record that cannot be read. ``None`` while she has said
    nothing, which a family row has no need to say."""
    if assignment_id in everything.hand_ins_unavailable:
        return HandInView.of(None)
    reading = everything.hand_ins.get(assignment_id)
    return None if reading is None or reading.state is None else HandInView.of(reading)


def turning_in(everything: Everything, today: date) -> list[HandInRowView]:
    """The family page's section on turning work in, from the page's one reading.

    Everything she reports as still to turn in comes first, the longest
    standing first and however old, since nothing unresolved drops out of
    sight with age. Whatever else she has said about delivery follows while
    her latest word on it is within the last fourteen household days, the
    latest first, and then any assignment whose hand-in record cannot be
    read. The order of events is the order the file gave them, never the
    clock's. There is no form here: what she turned in is hers to say.
    """
    waiting: list[tuple[int, HandInRowView]] = []
    lately: list[tuple[int, HandInRowView]] = []
    unreadable: list[HandInRowView] = []
    for item in everything.assignments:
        shown = hand_in_shown(everything, item.assignment_id)
        if shown is None:
            continue
        row = HandInRowView(
            assignment_id=item.assignment_id, course=item.course, title=item.title, hand_in=shown
        )
        if shown.unavailable:
            unreadable.append(row)
            continue
        reading = everything.hand_ins[item.assignment_id]
        head = reading.head
        entered = reading.entered
        if reading.state == NEEDS_HAND_IN and entered is not None:
            waiting.append((entered.sequence or 0, row))
        elif head is not None and head.reported_on > today - timedelta(days=RECENT_DAYS):
            lately.append((head.sequence or 0, row))
    return [
        *(row for _, row in sorted(waiting, key=lambda pair: pair[0])),
        *(row for _, row in sorted(lately, key=lambda pair: pair[0], reverse=True)),
        *unreadable,
    ]


def update_view(
    item: Assignment, status: AssignmentStatus, words: SchoolWordsView | None = None
) -> AssignmentUpdateView:
    """One row of the section: her account, the school's, and the family's check, read apart,
    with the school's instructions and anyone's note as her pages show them.

    The last check a parent marked stays on the row whatever happened to
    the facts since. While it stands against them, the row says it was
    checked. When her update or the school's report moved, the row says
    the check was made, on what day, with what note, and what differs now:
    a Done that is a new one or a school report that is not the one
    checked, on a row that is open again, or an update that is not Done
    and a school that does not say missing, on a row with nothing to
    check. The current basis decides only whether the row is open. While
    her updates can't be read, nothing is said to differ.
    """
    standing = status.check
    head = status.check_head
    marked = head if head is not None and head.operation == CHECKED else None
    before = marked if standing is None else None
    differs: list[str] = []
    new_missing = False
    if before is not None and status.check_basis is not None:
        then, now = basis_parts(before.basis), basis_parts(status.check_basis)
        new_missing = then[2] != now[2]
        differs = [
            words
            for words, applies in ((NEW_DONE, then[1] != now[1]), (NEW_MISSING, new_missing))
            if applies
        ]
    elif before is not None and not status.updates_unavailable:
        differs = [
            words
            for words, applies in (
                (DONE_GONE, status.needs_homework),
                (MISSING_GONE, not status.school_says_missing),
            )
            if applies
        ]
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
        words=SchoolWordsView() if words is None else words,
        checked=standing is not None,
        checked_on=None if standing is None else standing.checked_on,
        check_note=None if standing is None else standing.note,
        check_id=None if marked is None else marked.check_id,
        checked_before_on=None if before is None else before.checked_on,
        checked_before_note=None if before is None else before.note,
        differs=differs,
        new_missing=new_missing,
        updates_unavailable=status.updates_unavailable,
        checks_unavailable=status.checks_unavailable,
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
    kept_note: Annotated[
        str | None,
        Query(description="1 when the last save left a typed note off her homework; a note"),
    ] = None,
    linked: Annotated[
        str | None, Query(description="how many of her notes the last save linked; a note")
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
    focus: Annotated[
        str | None, Query(description="the assignment whose row to bring into view; a note")
    ] = None,
    plan: Annotated[
        str | None, Query(description="the plan whose folds to open; changes nothing")
    ] = None,
    run: Annotated[
        str | None, Query(description="a planning run to say where it stands; changes nothing")
    ] = None,
) -> HTMLResponse:
    """The parent's page: what she asked for, what is waiting, and the folds below. When the
    record cannot be read, a page that says so and offers the same address again. A run the
    address names is said where it stands; otherwise a run still running is said, since the
    plans that couldn't be made are listed below."""
    said = [
        (CHECK_CONFIRMATIONS[name], value)
        for name, value in (
            ("checked", checked),
            ("checked_already", checked_already),
            ("reopened", reopened),
        )
        if value
    ]
    try:
        return review_page(
            request,
            state,
            refreshed=refreshed == "1",
            added=a_count(added),
            updated=a_count(updated),
            unchanged=a_count(unchanged),
            kept_note=kept_note == "1",
            linked=a_count(linked),
            check=CheckState(said[0][1], said=said[0][0])
            if said
            else (CheckState(focus) if focus else None),
            open_plan=plan,
            run_notice=run_notice(state, run, FAMILY_PAGE, parent=True, today=None),
        )
    except sqlite3.Error as error:
        return review_unreadable(
            request,
            error,
            again=asked_address(FAMILY_PAGE, request.scope["query_string"]),
        )


def review_unreadable(request: Request, error: sqlite3.Error, *, again: str) -> HTMLResponse:
    """The family page when the record cannot be read as the page is made: the failure said
    once, with the way to try again, and each evening's plan in force as saved when the plans
    were read first. Nothing is read here, and the failure is logged by its kind alone."""
    logger.warning("the family page could not be read: %s", type(error).__name__)
    read: list[DraftRecord] = getattr(request.state, "plans_read", [])
    return templates.TemplateResponse(
        request,
        "plans_unavailable.html",
        {
            "page": "parent",
            "heading": "Family review",
            "alert": FAMILY_UNREADABLE_WHY,
            "marks": get_application_state(request).settings.page_marks,
            "again": again,
            "parent": True,
            "todays": False,
            "plans": [
                (record, saved_reading(record))
                for record in sorted(read, key=lambda item: item.plan_date)
            ],
        },
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def saved_reading(record: DraftRecord) -> PlanReading:
    """A plan as the family page shows it when the record cannot be read: its rows as
    planned, with links that are only the way to each assignment's details."""
    return read_plan(
        record,
        reader="family",
        link_for=lambda name: details_href(name, return_to="family", plan_id=record.draft_id),
        evidence_for=lambda name: details_href(
            name, fragment=EVIDENCE, return_to="family", plan_id=record.draft_id
        ),
        dates_unread=True,
    )


def a_count(given: str | None) -> int | None:
    """A count from the address, or none: anything that is not a count, a number below
    zero included, is no note."""
    try:
        count = None if given is None else int(given)
    except ValueError:
        return None
    return None if count is not None and count < 0 else count


async def plan_from_the_page(
    request: Request,
    state: State,
    graphs: Graphs,
    plan_date: Annotated[str, Form()] = "",
) -> Response:
    """The plan form. A blank date means today; a bad one is said, not guessed at.

    A run that fails on the way for any reason other than a refusal is said on
    the page too, as a plan Blossom couldn't finish, with the queue below
    unchanged; the run has already taken back what it left, and the failure
    goes to the process log. An evening that
    has passed is refused before anything runs, since a plan for it could
    reach no page of hers, and so is one past the edge of the calendar, whose
    week cannot be read: the same two refusals the JSON route makes. When the
    family page can't be read either, its stand-in says so, with the same status.
    """
    budget = graphs.budget()

    async def not_made(problem: str, code: int, check: RunCheck | None = None) -> HTMLResponse:
        # The family page is read on a worker thread, for at most the store's wait; past
        # it, its stand-in that reads no store says the same.
        try:
            return await bounded(
                lambda: refused_on_the_page(request, state, problem, code, check=check),
                STORE_WAIT_SECONDS,
            )
        except Unfinished:
            return family_not_shown(
                request, state, WITHOUT_THE_PAGE.get(problem, problem), code, line=FAMILY_NOT_SHOWN
            )

    try:
        evening = date.fromisoformat(plan_date) if plan_date.strip() else state.clock.today()
    except ValueError:
        return await not_made(
            f"{plan_date!r} is not a date. Use the form YYYY-MM-DD.",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if evening < state.clock.today():
        return await not_made(passed(evening), status.HTTP_422_UNPROCESSABLE_CONTENT)
    if evening > date.max - CALENDAR_MARGIN:
        return await not_made(beyond(evening), status.HTTP_422_UNPROCESSABLE_CONTENT)
    try:
        await require_work(state, evening, budget)
        require_model(graphs)
        made = await make_plan(graph_for_a_run(graphs), evening, state, budget=budget)
        refuse_an_empty_run(made.view)
    except AlreadyPlanning as error:
        return await not_made(
            already_planning(error.run, parent=True),
            error.status_code,
            run_check(FAMILY_PAGE, error.run.run_id, CHECK_ON_THAT_REQUEST),
        )
    except NotSaved as error:
        return await not_made(not_saved(parent=True, kept=error.kept), error.status_code)
    except CouldNotStart as error:
        return await not_made(f"{COULD_NOT_START} {saved_sentence(parent=True)}", error.status_code)
    except Unconfirmed as unconfirmed:
        return await not_made(
            f"{UNCONFIRMED} {saved_sentence(parent=True)}",
            status.HTTP_202_ACCEPTED,
            run_check(FAMILY_PAGE, unconfirmed.run_id, CHECK_AGAIN),
        )
    except HTTPException as error:
        return await not_made(str(error.detail), error.status_code)
    except Exception:
        logger.exception("the plan for %s failed on the way", evening)
        return await not_made(PLAN_INTERRUPTED, status.HTTP_409_CONFLICT)
    return RedirectResponse("/parent", status_code=status.HTTP_303_SEE_OTHER)


router.add_api_route(
    "/actions/plan",
    plan_from_the_page,
    methods=["POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
    route_class_override=FormRoute,
)


def help_from_the_page(
    request: Request,
    request_id: str,
    state: State,
    step: Annotated[str, Form()] = "",
    response: Annotated[str, Form()] = "",
    update_id: Annotated[str, Form()] = "",
) -> Response:
    """The buttons under a request, I can help, Add an update and Close request, through the
    same path the JSON routes take.

    The words are read with one kind of line ending, as the box counts them, so a line
    break is one character of the cap. The form's id goes with the move, so the same form
    sent again changes nothing; a form with no id moves as one of its own, and one whose id
    no form carries is refused. A step the family page refuses is said at its top with the
    words as typed: in the request's own box while the request is open there, marked and
    focused when the words are what was refused, with a way to the box when the refusal
    says they are below, and under the problem otherwise. A move the file refuses, or a
    request whose row can't be read, is rolled back and answered at once on the family
    page's stand-in, which reads no store, 500, with the words as typed. A refusal whose
    page can't be read keeps its status there, with the words.
    """
    typed = FamilyKept(reply=response)
    try:
        words = kept_words(response) or ""
    except UpdateTooLong as error:
        return refused_on_the_page(
            request,
            state,
            reply_too_long(error.length),
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            typed,
            help_reply=HelpReplyKept(request_id, response, at_reply=True),
        )
    try:
        form = update_id_from(update_id) if update_id else None
    except NotARequestId:
        return refused_on_the_page(
            request,
            state,
            BAD_CHECK_FORM,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            typed,
            help_reply=HelpReplyKept(request_id, response) if words else None,
        )
    try:
        move_request(state, request_id, step, words or None, form)
    except HTTPException as error:
        said = str(error.detail)
        kept = None
        if said == UPDATE_NEEDS_WORDS:
            kept = HelpReplyKept(request_id, response, at_reply=True)
        elif words:
            kept = HelpReplyKept(request_id, response, below=said in WORDS_BELOW)
        return refused_on_the_page(request, state, said, error.status_code, typed, help_reply=kept)
    except (sqlite3.Error, UnreadableHelpRequest) as error:
        logger.warning(
            "a parent's move on her request could not be saved: %s", type(error).__name__
        )
        return family_not_shown(
            request,
            state,
            HELP_STEP_NOT_SAVED if words else STEP_NOT_SAVED,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            kept=typed if words else None,
        )
    return RedirectResponse("/parent", status_code=status.HTTP_303_SEE_OTHER)


router.add_api_route(
    "/actions/help/{request_id}",
    help_from_the_page,
    methods=["POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
    route_class_override=FormRoute,
)


def check_could_not(request: Request, state: ApplicationState, check: CheckState) -> HTMLResponse:
    """The page after a check or a reopening the file refused: the row with the note typed,
    and no word of a check. When the family page cannot be read back either, a plain page
    with the note instead, which reads no store."""
    try:
        return review_page(
            request, state, check=check, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    except Exception:
        logger.exception("the family page could not be read back after a failed check")
        return family_not_shown(
            request,
            state,
            check.problem or "",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            kept=FamilyKept(note=check.note),
            title="Check not saved",
        )


def family_not_shown(
    request: Request,
    state: ApplicationState,
    said: str,
    status_code: int,
    *,
    line: str | None = None,
    kept: FamilyKept | None = None,
    title: str = "Family review",
) -> HTMLResponse:
    """The family page's stand-in, which reads no store: what happened, which page can't be
    shown when a refusal's own page is what failed, and what a parent typed."""
    typed = kept or FamilyKept()
    return templates.TemplateResponse(
        request,
        "family_check_recovery.html",
        {
            "title": title,
            "said": said,
            "line": line,
            "note": typed.note,
            "reply": typed.reply,
            "reason": typed.reason,
            "marks": state.settings.page_marks,
        },
        status_code=status_code,
    )


def reviewed_once(
    request: Request,
    state: ApplicationState,
    page: Callable[[], HTMLResponse],
    problem: str,
    status_code: int,
    kept: FamilyKept | None = None,
) -> HTMLResponse:
    """A refusal shown on the family page, tried once. When a read for that page fails, its
    stand-in, with the refusal's status, its words as they read without the page, and what
    was typed: made from what the request already held, so no store is called after the
    failure. Any other failure is not caught here."""
    try:
        return page()
    except sqlite3.Error as error:
        logger.warning("the family page could not be read for a refusal: %s", type(error).__name__)
        return family_not_shown(
            request,
            state,
            WITHOUT_THE_PAGE.get(problem, problem),
            status_code,
            line=FAMILY_NOT_SHOWN,
            kept=kept,
        )


def refused_on_the_page(
    request: Request,
    state: ApplicationState,
    problem: str,
    status_code: int,
    kept: FamilyKept | None = None,
    *,
    check: RunCheck | None = None,
    help_reply: HelpReplyKept | None = None,
) -> HTMLResponse:
    """A form action the family page refused, said at its top with the status the JSON route
    would answer, ``check`` the link to the run it names, and ``help_reply`` a reply to her
    request as typed, tried once."""
    return reviewed_once(
        request,
        state,
        lambda: review_page(
            request,
            state,
            problem=problem,
            problem_check=check,
            help_reply=help_reply,
            status_code=status_code,
        ),
        problem,
        status_code,
        kept,
    )


def another_rows(basis: str, assignment_id: str) -> bool:
    """Whether a basis names another assignment, or none at all.

    A basis begins with its assignment, so one that begins otherwise was not
    made for this row: a form put together wrong, refused as such, 422,
    before anything is compared, and never taken for a page the facts moved
    past, which is a 409.
    """
    return basis_parts(basis)[0] != assignment_id


def back_to_the_row(said: str, assignment_id: str) -> str:
    """Where a check or a reopening sends a parent: the page, the row, and what happened.
    The id is escaped in the query, and in the fragment too, which a browser undoes to
    find the row, so an id that holds a hash names its row in both."""
    return address(
        FAMILY_PAGE, fragment=f"update-{segment(assignment_id)}", **{said: assignment_id}
    )


def check_actions(assignment_id: str) -> tuple[str, str]:
    """The two routes a check goes through, mark checked and check again, with the id as
    one escaped segment. The server undoes the escaping before it matches, so both routes
    take the id as the rest of the path up to their own ending."""
    base = f"/parent/actions/checks/{segment(assignment_id)}"
    return f"{base}/mark", f"{base}/again"


@router.post(
    "/actions/checks/{assignment_id:path}/mark",
    response_class=HTMLResponse,
    include_in_schema=False,
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
    and the check event with the head: the same check standing already,
    note and all, is already made, with no write and no new day; a basis
    that differs, a record that moved on, or a check another parent made
    with other words, is answered 409 with the row as it stands now and
    the note typed kept; otherwise the check is appended. Whatever is
    refused, the note that could be read comes back with the page. A write
    the file refuses is answered with the page and the note, or with a
    plain page and the note when the page cannot be read either, and never
    with a word of a check. The gate admits only a parent's device to this
    path, so her device is answered 403 before this runs; with the sign-in
    off, whoever is at the keyboard is the family. Her events, the school's
    reports, the plans, and the digest are untouched by any answer here.
    A refusal whose page can't be read keeps its status and the note on the
    family page's stand-in.
    """
    fields, whole = await fields_of(request, CHECK_FIELDS)
    basis = fields.get("basis", "").strip()
    token = fields.get("expected_check_id", "").strip()
    note = fields.get("note", "")
    words = normalize_note(note)
    typed = FamilyKept(note=note)

    def refused(problem: str, code: int, *, field: str | None = None) -> Response:
        check = CheckState(assignment_id, problem=problem, field=field, note=note)
        return reviewed_once(
            request,
            state,
            lambda: review_page(request, state, check=check, status_code=code),
            problem,
            code,
            typed,
        )

    if not whole:
        return refused(BAD_CHECK_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if (
        not basis
        or len(basis) > BASIS_MAX_LENGTH
        or len(token) > TOKEN_MAX_LENGTH
        or another_rows(basis, assignment_id)
    ):
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
        return refused_on_the_page(request, state, NOT_ON_RECORD, status.HTTP_404_NOT_FOUND, typed)
    except UnknownCheck:
        return refused(NOT_THIS_ROWS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    except NoteTooLong:
        return refused(CHECK_NOTE_TOO_LONG, status.HTTP_422_UNPROCESSABLE_CONTENT, field="note")
    except CouldNotSave:
        logger.exception("the check on %s could not be saved", assignment_id)
        return check_could_not(
            request, state, CheckState(assignment_id, problem=CHECK_NOT_SAVED, note=note)
        )
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
            marked_since = head is not None and head.operation == CHECKED and head.basis == basis
            moved = marked_since or (None if head is None else head.check_id) != (token or None)
            return refused(
                CHECK_MOVED_ON if moved else CHECK_FACTS_CHANGED, status.HTTP_409_CONFLICT
            )


@router.post(
    "/actions/checks/{assignment_id:path}/again",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def check_again_from_the_page(request: Request, assignment_id: str, state: State) -> Response:
    """Check again: a parent reopens the family's check on one assignment, and nothing else.

    The form carries the check it reopens and the basis its page showed,
    blank when the page showed nothing to check. The check must be this
    assignment's and at the head of its record, and the basis must be the
    one worked out from her events and the school's reports as they stand,
    inside the store's transaction under the decision lock: a record that
    moved on, or facts that moved since the page was made, is answered 409
    with the row as it stands and nothing written. The check reopened
    stays in the record; her update and the school's report are untouched,
    and the row is worth checking together again while her Done stands
    beside a Missing. The gate admits only a parent's device here. A refusal
    whose page can't be read keeps its status on the family page's stand-in.
    """
    fields, whole = await fields_of(request, AGAIN_FIELDS)
    token = fields.get("check_id", "").strip()
    basis = fields.get("basis", "").strip()

    def refused(problem: str, code: int) -> Response:
        check = CheckState(assignment_id, problem=problem)
        return reviewed_once(
            request,
            state,
            lambda: review_page(request, state, check=check, status_code=code),
            problem,
            code,
        )

    if not whole:
        return refused(BAD_CHECK_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if (
        not token
        or len(token) > TOKEN_MAX_LENGTH
        or len(basis) > BASIS_MAX_LENGTH
        or (basis and another_rows(basis, assignment_id))
    ):
        return refused(NOT_THIS_ROWS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    store = state.project_state
    try:
        async with state.decision_lock:
            result = store.check_again(
                assignment_id,
                token,
                expected_basis=basis or None,
                basis_now=lambda: statuses_for(store, [assignment_id])[assignment_id].check_basis,
                now=state.clock.now(),
                today=state.clock.today(),
            )
    except UnknownAssignment:
        return refused_on_the_page(request, state, NOT_ON_RECORD, status.HTTP_404_NOT_FOUND)
    except UnknownCheck:
        return refused(NOT_THIS_ROWS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    except CouldNotSave:
        logger.exception("the check on %s could not be reopened", assignment_id)
        return check_could_not(
            request, state, CheckState(assignment_id, problem=CHECK_NOT_REOPENED)
        )
    match result:
        case Reopened():
            return RedirectResponse(
                back_to_the_row("reopened", assignment_id), status_code=status.HTTP_303_SEE_OTHER
            )
        case CheckConflict(head=head):
            same_check = head is not None and head.check_id == token and head.operation == CHECKED
            return refused(
                CHECK_FACTS_CHANGED if same_check else CHECK_MOVED_ON, status.HTTP_409_CONFLICT
            )


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
    A refusal whose page can't be read keeps its status, with the reason as
    typed, on the family page's stand-in.
    """
    typed = FamilyKept(reason=reason)
    if decision not in DECISIONS:
        return refused_on_the_page(
            request,
            state,
            f"{decision!r} is not one of the two buttons, approve or refuse.",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            typed,
        )
    words = reason.strip()
    if len(words) > REASON_MAX_LENGTH:
        return refused_on_the_page(
            request,
            state,
            f"A reason is at most {REASON_MAX_LENGTH} characters; this one is {len(words)}.",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            typed,
        )
    decided = DecisionRequest(approved=decision == "approve", reason=words or None)
    try:
        await decide_draft(state, graphs.build, draft_id, decided)
    except HTTPException as error:
        return refused_on_the_page(request, state, str(error.detail), error.status_code, typed)
    return RedirectResponse("/parent", status_code=status.HTTP_303_SEE_OTHER)


router.add_api_route(
    "/actions/decide/{draft_id}",
    decide_from_the_page,
    methods=["POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
    route_class_override=FormRoute,
)
