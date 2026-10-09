# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
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
can take up to about a minute and a half and needs a key; without one the page
says so and everything else on it still works.

The workload signal takes no argument: rating or describing the load requires
stepping back, and that capacity is least available exactly when the signal
matters. One press records that today is too much, the page shows it at once
with what it changes, tonight's plan is held to a reduced budget, and Undo
'Too much right now' beside it takes it back. Each press keeps a row of its own
and the evening stays shorter while any is left, so after an Undo the page says
what still stands and never promises the usual evening. The page also lists
every signal still kept, each with a way to remove it, because the record is hers.

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
the active cards rather than leaving the week. A card she saves stays where it
was for the rest of her visit, and the week groups it by its update on her
next visit: a fresh arrival, a return, or a refresh. The form is a form alone, so
it works without a script, and a save that lands on a card another device
has since changed is shown that change and asked to look again. A parent
signed in sees her update and cannot make one in her name.
"""

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
from collections.abc import Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from functools import partial
from typing import Annotated, Any, ClassVar, Final, Literal, cast
from zoneinfo import ZoneInfo

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import URL

from blossom.agent.runs import Unfinished, bounded
from blossom.agent.steps import DATE_PROBLEM
from blossom.agent.steps import NOTHING_TO_SCHEDULE as NOTHING_TO_SCHEDULE_OUTCOME
from blossom.anthropic_client import model_configured
from blossom.assignment_status import AssignmentStatus, statuses_for
from blossom.captures import derived_assignment_id, what_remains
from blossom.clock import local_now
from blossom.dependencies import ApplicationState, get_application_state
from blossom.evening import (
    PlanUpdates,
    ReportedDone,
    Staleness,
    limit_now,
    plan_updates,
    staleness,
)
from blossom.hand_in import HAND_IN_NOTE_MAX_LENGTH, NEXT_ACTION_MAX_LENGTH
from blossom.noticing import (
    Everything,
    Noticing,
    WindowSide,
    earlier_to_check,
    expect_due_date,
    in_week,
    monday_of,
    notice_due_date,
    noticings_of,
    planning_week,
    planning_window,
    read_date,
    read_everything,
    reconcile_dates,
    week_from,
)
from blossom.pairing import pair
from blossom.plan_dates import DatesNow, dates_now
from blossom.plan_reading import DoneMark, PlanReading, Reader, anchor_for, long_date, read_plan
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
    DETAILS,
    EVIDENCE,
    FAMILY_PAGE,
    NEW_NOTE_PAGE,
    NOTES_PAGE,
    TO_TURN_IN,
    TO_TURN_IN_PAGE,
    UPDATE_OR_TURN_IN,
    WEEK_PAGE,
    ReturnTo,
    address,
    asked_address,
    details_href,
    instructions_review_href,
    read_return,
    result_anchor,
    safe_default,
    segment,
    title_anchor,
    todays_plan_href,
    update_choice_anchor,
    week_href,
)
from blossom.routes.runs import (
    CHECK_AGAIN,
    CHECK_ON_THAT_REQUEST,
    COULD_NOT_START,
    COULD_NOT_START_SENTENCES,
    NOT_USED,
    OPENED_A_WEEK_AGO,
    PLAN_ANSWERS,
    PLAN_FORM_FIELDS,
    RUN_STATUS_ANSWERS,
    STORE_FAILURES,
    UNCONFIRMED,
    AlreadyPlanning,
    Clause,
    CouldNotStart,
    Graphs,
    Needs,
    NotSaved,
    PlanAnswer,
    PlanRow,
    RunCheck,
    RunNotice,
    SameRun,
    Sentences,
    Unconfirmed,
    being_made,
    ended_sentences,
    ended_without_a_plan,
    evening_named,
    fact,
    fresh_plan_form,
    graph_for_a_run,
    landed,
    make_plan,
    named_work,
    not_saved,
    not_saved_sentences,
    past_due_views,
    plan_form_from,
    planning_sentences,
    require_model,
    require_work,
    run_check,
    run_notice,
    run_of_the_form,
    run_replaced,
    run_status_view,
    saved_sentence,
)
from blossom.school_instructions import InstructionsStanding
from blossom.settings import CALENDAR_MARGIN
from blossom.stores.captures import NamedCaptures
from blossom.stores.catch_up import ChoiceMade, ChoiceNotSaved, NotOnRecord
from blossom.stores.drafts import (
    INTERRUPTED,
    OVERTAKEN,
    SETTLE_GRACE_SECONDS,
    STORE_WAIT_SECONDS,
    DraftRecord,
    RunState,
    StaleBasis,
    UnknownBasis,
)
from blossom.stores.help_requests import (
    HELP_RECENT_DAYS,
    NOTE_MAX_LENGTH,
    HelpAlreadyAsked,
    HelpAsked,
    HelpFormChanged,
    HelpFormUsed,
    HelpHeld,
    HelpListed,
    HelpRequest,
    NotARequestId,
    RequestClosed,
    UnreadableHelpRequest,
    new_request_id,
    request_id_from,
)
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
    UnreadableClaim,
    normalize_note,
)
from blossom.stores.project_state import NOTE_MAX_LENGTH as UPDATE_NOTE_MAX_LENGTH
from blossom.stores.workload_signals import DETAIL_MAX_LENGTH, WorkloadSignal
from blossom.templating import page_templates
from blossom.to_turn_in import (
    COMPACT_ROWS,
    Attempt,
    ListRefusal,
    ListResult,
    Receipt,
    refusal_for,
    result_for,
    to_turn_in,
)
from blossom.views import (
    EarlierWorkView,
    HandInView,
    HelpNoteView,
    HelpRequestView,
    NamedAssignmentView,
    ParentUpdateView,
    PastDueView,
    PublishedRunView,
    RunStatusView,
    SchoolStatementView,
    StudentAssignmentView,
    StudentDueThisWeekView,
    StudentPlanView,
    UpdateHistoryRowView,
    WeekView,
    WorkloadSignalView,
    school_words,
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
SHE_SIGNALED_SINCE: Final = (
    "She has said today is too much, and this plan was made for the full evening. "
    "Her current plan has not changed yet. Make a smaller plan when she is ready."
)
"""The same notice to a parent reading her week. ``SIGNAL_ENDED`` names nobody and is
said to everyone alike: a signal is gone when she takes it back and when its week is
over, and it never says which."""
SIGNAL_ENDED: Final = (
    "This plan was kept to the smaller evening for a signal that is not there now. "
    "It stays until a new one is made; plan again for the full evening."
)
ASSIGNMENTS_CHANGED: Final = (
    "Your assignments or updates differ from the work this plan used, so it does not cover "
    "your week as it stands. It stays until a new one is made; plan again when you are "
    "ready."
)


def over_the_limit(minutes: int) -> str:
    """The notice for a saved plan whose blocks are over today's limit of ``minutes``, said
    alike to her and to a parent reading her week, since it names nobody."""
    return (
        f"This plan is longer than today's {minutes}-minute limit. "
        "It stays until a new one is made."
    )


PLAN_INCLUDES_DONE: Final = "This plan includes work you now report as Done."
PLAN_WINDOW_DONE: Final = "Some work in this plan's window is now reported Done."
"""The notice for a plan from before plans carried their ids: a fact about its window and
no more. What to do about it, when anything can be done, is the stale warning's to say."""
UPDATE_SAVED: Final = "Your update is saved."
UPDATE_ALREADY_SAVED: Final = "Your update is already saved."
UPDATE_UNDONE: Final = "Your update is undone."
"""What an Undo is said to have done when the undo its address names is not her latest event:
an update saved after it, an address that names no undo, or updates that cannot be read."""
UNDONE_TO_NOTHING: Final = "Last update undone. No update is recorded."
UNDONE_TO_NOT_YET: Final = "Last update undone. This is marked Not yet."
UNDONE_STILL_DONE: Final = "Last update undone. This is still Done."
UNDONE_DONE_AGAIN: Final = "Last update undone. This is Done again."
"""What Undo last update restored: no update, a Not yet, the Done a note edit was made on, or
a Done that a Not yet was saved over."""
CHOOSE_ONE: Final = "Choose Done or Not yet."
NOTE_TOO_LONG: Final = f"Keep your note to {UPDATE_NOTE_MAX_LENGTH} characters or fewer."
SAVED_ELSEWHERE: Final = "An update was saved on another device. Review it before saving yours."
NOT_HERS_TO_UPDATE: Final = "Sign in as the student to update."
NOT_ON_RECORD: Final = "That assignment is not on record, so nothing was changed."
EARLIER_WORK: Final = "earlier-work"
"""The id of the section that lists earlier homework to check, on every week shown."""
EARLIER_SHOWN: Final = 10
"""How many items from the last ``EARLIER_DAYS`` show above the fold, newest first."""
EARLIER_DAYS: Final = 14
"""How many calendar days before today an item may be due and still show above the fold."""
EARLIER_FIELDS: Final = frozenset({"choice", "made_with", "week", "place"})
EARLIER_CHOICES: Final = ("include", "remove")
EARLIER_SAID: Final = {
    "included": "Added to your choices for today.",
    "removed": "Removed from your choices for today.",
    "already_included": "This was already in your choices for today.",
    "already_removed": "This was already out of your choices for today.",
}
"""What a choice in Earlier homework to check did, by the word its address carries."""
EARLIER_IN: Final = frozenset({EARLIER_SAID["included"], EARLIER_SAID["already_included"]})
"""What a choice says that holds only while the item is chosen for today."""
EARLIER_PLACES: Final = ("list", "fold", "card")
"""Where an item was shown when she pressed it: above the fold, in it, or on its card in the
week, beside her Not yet."""
SENTENCE_ENDS: Final = ".!?\u2026\u3002\uff01\uff1f\u061f"
"""Marks a reason may already end its sentence with: the stops, ellipsis, and the full-width
and Arabic forms."""
CLOSING_MARKS: Final = "\"')]\u201d\u2019\u00bb"
"""Quotes and brackets that may follow a sentence's last mark."""
CHOICE_NOT_SAVED: Final = "Your choice was not saved"
NOT_HERS_TO_CHOOSE: Final = "Sign in as the student to choose work for today's plan."
CHOICE_FROM_ANOTHER_DAY: Final = (
    "That page was made on another day, and choices are for today's plan, so nothing was "
    "changed. Choose again here if you want it today."
)
CHOICE_BAD_FORM: Final = (
    "That form isn't one this page sends, so nothing was changed. Choose again here."
)
NOT_EARLIER_NOW: Final = (
    "That homework isn't on the list of earlier homework now, so it can't be chosen. Nothing "
    "was changed."
)
CHOICE_FAILED: Final = "Your choice could not be saved, so nothing was changed. Try again."
NOT_THIS_CARDS: Final = (
    "That form names an update this assignment does not have, so nothing was saved. The card "
    "shows what stands; choose and save from here."
)
BAD_FORM: Final = (
    "That form carried a field twice, or one this page does not send, so nothing was saved. "
    "Choose and save again."
)
NOT_HERS_TO_ASK: Final = "Sign in as the student to ask for help or take a request back."
NOT_HERS_TO_SIGNAL: Final = (
    "Sign in as the student to press Too much right now or Undo 'Too much right now'."
)
HELP_FORM_NOT_WHOLE: Final = (
    "That form carried a field twice, left one out, or had one this page doesn't send, so "
    "nothing was sent. Your words are below; ask again."
)
HELP_NOT_SENT: Final = "Your request could not be sent, and nothing was changed. Try again."
ALREADY_SENT: Final = "That request is already saved."
SENT: Final = "Your request is saved. A parent can see it in Family review."
NOT_ON_THIS_PAGE: Final = "That request is not on this page now."
CANNOT_CHECK: Final = "That request can't be checked right now."
"""The lines for the request an address names, checked against the page's one reading and
said to her alone: saved, for one she has just sent that still waits for a parent; already
saved, beside any other request on the page; not on this page, when no request there is that
one; and can't be checked, when that reading failed."""
ALREADY_RESPONDING: Final = (
    "A parent is already responding to this request, so it cannot be taken back. "
    "Nothing was changed."
)
ALREADY_CLOSED: Final = (
    "This request is already closed, so it cannot be taken back. Nothing was changed."
)
NOT_HERE_ANY_MORE: Final = "That request is not here any more; nothing was changed."
REQUEST_UNREADABLE: Final = "This request for help can't be read right now, so nothing was changed."
"""What a move on a request the store can't read says, on either page and in JSON: the move
read the row, refused it, and wrote nothing."""
ASK_ANEW: Final = "If you still need help, you can send a new request."
"""What her week adds, beside her ask form, to the line counting requests that can't be read:
her form keeps a new request under a fresh id and leaves those rows as they are."""
UNREADABLE_COUNT: Final = "Help-Requests-Unreadable"
"""The header each JSON list of her requests sends: how many kept requests it set apart
because they can't be read, 0 when none."""
FORM_SENT_OTHER_WORDS: Final = (
    "This form already sent a request with other words, so these weren't sent. Ask again "
    "to send them as a new request."
)
FORM_USED: Final = "This form was already used. Open a new help form to ask again."
ASK_FIELDS: Final = frozenset({"note", "request_id"})
"""What her Ask for help forms send: her words, blank allowed, and the id the page gave the
form, each once."""
CANNOT_UNDO: Final = (
    "Your update has changed, so it cannot be undone from that page. The card shows what stands."
)
ALREADY_UNDONE: Final = "That update was already undone. The card shows what stands now."
"""Said for an Undo of the very update that the latest event already took back: a repeat,
refused like any other stale Undo and writing nothing, told apart from a change by the
head the refusing save read and never by comparing words."""
NOT_SAVED: Final = "Your update could not be saved. Your words are still here. Try again."
NOT_UNDONE: Final = "Your update could not be undone, and nothing was changed. Try again."
TAKE_BACK_NOT_SAVED: Final = (
    "Your request could not be taken back, and nothing was changed. Try again."
)
SIGNAL_NOT_SAVED: Final = "That could not be saved, and nothing was changed. Try again."
"""What a take-back or a signal the file refused says: each store rolls back a write it
could not finish, a refused commit included, so nothing was changed and pressing again is
safe."""


@dataclass(frozen=True)
class PlanFailure:
    """A plan press that ended without a plan, for the line that says so: each assignment the
    run named for a date that already passed, by its title, course and due date and the
    address of its dates, the link to where a run named by the answer stands, and whether
    another press may help, which the plan button then offers as Try again: never after an
    unconfirmed outcome or a date problem, which planning again can't fix."""

    checks: tuple[tuple[str, str], ...] = ()
    run: RunCheck | None = None
    try_again: bool = True
    uncertain: bool = False
    """Whether the run may still publish: the page then offers only the check, and no plan
    button that could start another run."""


def date_checks(past_due: Iterable[PastDueView]) -> tuple[tuple[str, str], ...]:
    """Each past-due assignment, in the order given, by its title, course and due date, and
    the address of its dates on her week."""
    return tuple(
        (named_work(work), details_href(work.assignment_id, fragment=EVIDENCE, return_to="week"))
        for work in past_due
    )


UPDATE_NOT_SAVED: Final = "Update not saved"
REQUEST_NOT_SENT: Final = "Request not sent"
REQUEST_NOT_TAKEN_BACK: Final = "Request not taken back"
PLAN_NOT_MADE: Final = "Plan not made"
NOT_SAVED_HEADING: Final = "Not saved"
"""The headings of the page that reads no store, each naming the press it answers."""
YOUR_WEEK_NOT_SHOWN: Final = "Your week can't be shown right now."
HER_WEEK_NOT_SHOWN: Final = "Her week can't be shown right now."
ASSIGNMENT_NOT_SHOWN: Final = "This assignment can't be shown right now."
"""The line that page adds after a press's own words: which page the press would have been
shown on, and that it can't be shown."""
DETAILS_UNAVAILABLE: Final = "This assignment can't be shown right now. Try again in a moment."
WITHOUT_THE_PAGE: Final[dict[str, str]] = {
    CHOOSE_ONE: "Nothing was saved, because neither Done nor Not yet was chosen.",
    NOTE_TOO_LONG: (
        f"Nothing was saved, because the note is longer than {UPDATE_NOTE_MAX_LENGTH} characters."
    ),
    BAD_FORM: (
        "That form carried a field twice, or one this page does not send, so nothing was saved."
    ),
    SAVED_ELSEWHERE: "An update was saved on another device, so yours was not saved.",
    NOT_THIS_CARDS: (
        "That form names an update this assignment does not have, so nothing was saved."
    ),
    CANNOT_UNDO: "Your update has changed, so it cannot be undone from that page.",
    ALREADY_UNDONE: "That update was already undone.",
}
"""Each refusal written for a page, as it reads where that page is not shown: a sentence that
points at the page goes, and a rule about a field says what was not saved and why. Any other
refusal reads the same in both places."""
FROM_DETAILS: Final = frozenset({"report_view", "return_to", "plan_id"})
"""The fields a form on an assignment's details sends beside the rest: that the result is
to be shown there, and where its reader came from. A card on her week sends none of them,
and is as whole without them as it always was."""
IN_PLACE: Final = "in_place"
"""The field her update forms on her week carry, and the query Change and Keep it as it is
carry: the cards this visit keeps where they were, the form's own card among them."""
IN_PLACE_LANDING: Final = "landing"
"""The query a redirect that keeps cards in place sends her to her week with: a value new for
each redirect, naming the cookie that holds the cards, so only the page it lands on reads
them and no other visit to her week does."""
IN_PLACE_LANDING_DIGITS: Final = 16
"""How many hex digits a landing has."""
IN_PLACE_COOKIE: Final = "blossom-in-place"
"""The start of the cookie a save, an undo, Change, Keep it as it is or a removed signal
leaves for the one page that answers it, followed by that page's landing. The page reads it
once and clears it."""
IN_PLACE_SECONDS: Final = 60
"""How long that cookie waits for the page that answers it, which a browser asks for at once."""
IN_PLACE_MAX: Final = 40
"""The most cards one visit keeps in place; a visit that saves more keeps the latest."""
IN_PLACE_KEY: Final = 16
"""How many hex digits of a card's key are carried: the same for an id of any length, so
``IN_PLACE_MAX`` cards fit the 4,096 bytes a browser keeps for one cookie."""
DONE_COOKIE: Final = "blossom-done"
"""The start of the cookie a save on an assignment's details that has just made it Done
leaves for the one page that answers it, followed by that page's landing. The details keep
no card in place, so this cookie holds that one mark and nothing else."""
IN_PLACE_DONE: Final = "n"
"""The mark the cookie a save leaves puts ahead of the cards it keeps in place, on the key of
the card that save has just made Done. No form or address carries it, and ``InPlace.read``
passes over it."""
SIGNAL_REMOVED: Final = "r"
"""The whole of the cookie Undo 'Too much right now' or a Remove leaves, so the one page that
answers it says that request isn't active, beside what still stands. No form or address
carries it, and ``InPlace.read`` passes over it."""
TOO_MUCH_STATE: Final = "too-much-state"
"""The id of the line on her week that says where her request for a shorter plan stands, where
Undo 'Too much right now' and a Remove land, with the focus, as a save lands on its card."""
WELL_DONE: Final = "Done. Nice work."
"""The words a save that has just made a card Done is met with, beside a small petal, on the
one page that answers it."""
REPORT_FIELDS: Final = (
    frozenset({"status", "note", "expected_report_id", "week", IN_PLACE}) | FROM_DETAILS
)
UNDO_FIELDS: Final = frozenset({"report_id", "week", IN_PLACE}) | FROM_DETAILS
"""The fields each form sends, each once. Anything else, anything twice, or a form with one
of them left out, is refused."""
PLAN_FIELDS: Final = frozenset({IN_PLACE, *PLAN_FORM_FIELDS})
PLAN_LABEL_MARK: Final = "{plan label}"
"""Where a plan answer names the plan button: the page puts the button's own words there,
Plan today, Plan again or Make a smaller plan."""
FOR_A_NEW_PLAN: Final = Clause(f"For a new plan, press {PLAN_LABEL_MARK}.", "button")
NEWER_PLAN_MADE: Final = ("A newer plan for today was made.", "This press did not replace it.")
NEWER_PLAN: Final = PlanAnswer(
    "her-newer-plan",
    status.HTTP_409_CONFLICT,
    (*fact(*NEWER_PLAN_MADE), Clause("It is shown below.", "plan"), FOR_A_NEW_PLAN),
    NEWER_PLAN_MADE,
)
FORM_NOT_WHOLE: Final = PlanAnswer(
    "her-not-whole",
    status.HTTP_422_UNPROCESSABLE_CONTENT,
    (*fact(*NOT_USED), FOR_A_NEW_PLAN),
    NOT_USED,
)
FORM_EXPIRED: Final = PlanAnswer(
    "her-expired",
    status.HTTP_409_CONFLICT,
    (*fact(*OPENED_A_WEEK_AGO), FOR_A_NEW_PLAN),
    OPENED_A_WEEK_AGO,
)
PLAN_MADE_ALREADY_SAID: Final = (
    "That request made a plan for today.",
    "No new plan was started.",
)
PLAN_MADE_ALREADY: Final = PlanAnswer(
    "her-plan-made",
    status.HTTP_409_CONFLICT,
    (*fact(*PLAN_MADE_ALREADY_SAID), FOR_A_NEW_PLAN),
    PLAN_MADE_ALREADY_SAID,
)


def another_evening(evening: date) -> PlanAnswer:
    """A plan press for an evening that isn't today, which starts nothing, naming that
    evening."""
    said = (f"That plan button was for {evening_named(evening)}.", "No new plan was started.")
    return PlanAnswer(
        "her-another-evening", status.HTTP_409_CONFLICT, (*fact(*said), FOR_A_NEW_PLAN), said
    )


"""What the plan button's form sends: only the cards the visit keeps in place, when it keeps
any. Planning reads none of it; it decides only where the page it returns to shows cards."""
FROM_A_CARD: Final = FROM_DETAILS | {IN_PLACE}
"""The fields a form may leave out: the details' own, and the cards a week's form keeps in
place, which a form from the details carries none of."""
NOTHING_CHOSEN: Final = frozenset({"status"})
"""The field her browser leaves out when neither Done nor Not yet is chosen: two radio
buttons with none checked send nothing. The card then asks her to choose one."""
BAD_RETURN: Final = (
    "The form named a page to go back to that these pages do not make. Nothing was saved."
)
GONE: Final = "This assignment is not on record now."
NO_PLAN_NOW: Final = "No plan is saved for today now."
WEEK_UNREADABLE: Final = "This week cannot be shown right now"
WEEK_UNREADABLE_WHY: Final = (
    "The record cannot be read right now, so the week, its updates and current date "
    "information are not shown. Try again in a moment."
)
TURNING_IT_IN: Final = "turning-it-in"
"""The id of the section on an assignment's details that holds her hand-in account."""
ASK_FOR_HELP: Final = "ask-for-help"
"""The id of the place on her week that holds her Ask for help form, or a parent's line
about her requests."""
HOMEWORK: Final = "homework"
"""The id of her week's homework heading, where Refresh list lands."""
HELP_SHE_ASKED_FOR: Final = "help-she-asked-for"
"""The id of the family page's Help section, which holds her requests and takes the focus."""
FAMILY_HELP: Final = address(FAMILY_PAGE, fragment=HELP_SHE_ASKED_FOR)
"""The help she asked for, on the family page, where a parent answers it and where each
of a parent's moves on a request lands."""
HAND_IN_SAVED: Final = "Your hand-in update is saved."
HAND_IN_ALREADY_SAVED: Final = "Already saved."
HAND_IN_UNDONE: Final = "Your hand-in update is undone."
STEP_NOT_SAVED: Final = "The next step was not saved: it goes only with Still to turn in."
HAND_IN_CONFIRMATIONS: Final[dict[str, str]] = {
    "saved": HAND_IN_SAVED,
    "same": HAND_IN_ALREADY_SAVED,
    "undone": HAND_IN_UNDONE,
    "saved_without_step": f"{HAND_IN_SAVED} {STEP_NOT_SAVED}",
    "same_without_step": f"{HAND_IN_ALREADY_SAVED} {STEP_NOT_SAVED}",
}
"""What the address says a hand-in save or undo did, chosen by the server as her
update's are; what stands is shown beside it, with its day. A save of another answer with
a next step typed says the step was not saved."""
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


class HersAlone(APIRoute):
    """A route only she may call. The framework reads and checks a JSON body before the
    handler or any dependency runs, so a parent is answered 403 here, before the body
    is read or anything the route names is looked up. Each kind says what to do in its
    own words."""

    refusal: ClassVar[str]

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """The framework's handler, reached only once the reader is her."""
        handle = super().get_route_handler()
        refusal = self.refusal

        async def hers(request: Request) -> Response:
            if viewer_of(request) == "parent":
                raise HTTPException(status.HTTP_403_FORBIDDEN, detail=refusal)
            return await handle(request)

        return hers


class HersToAsk(HersAlone):
    """Asking for help: hers alone."""

    refusal = NOT_HERS_TO_ASK


class HersToSignal(HersAlone):
    """Saying today is too much, and taking it back: hers alone."""

    refusal = NOT_HERS_TO_SIGNAL


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


async def register_workload_signal(
    state: State,
    payload: Annotated[WorkloadSignalRequest | None, Body()] = None,
) -> WorkloadSignalResponse:
    """Record that today is too much. ``payload`` is optional so an empty POST works."""
    detail = None if payload is None else payload.detail
    signal = await record_signal(state, detail)
    return WorkloadSignalResponse(principal=Principal.STUDENT, signal=signal_view(state, signal))


router.add_api_route(
    "/workload-signals",
    register_workload_signal,
    methods=["POST"],
    status_code=status.HTTP_201_CREATED,
    route_class_override=HersToSignal,
)


@router.get("/workload-signals")
def held_workload_signals(state: State) -> list[WorkloadSignalView]:
    """Every signal still kept, most recent first: what she can see and take back."""
    return [signal_view(state, signal) for signal in state.workload_signals.held()]


async def withdraw_workload_signal(signal_id: str, state: State) -> Response:
    """Take a signal back. It is gone, not marked; the record is hers to remove."""
    if not await withdraw_signal(state, signal_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no signal {signal_id!r}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


router.add_api_route(
    "/workload-signals/{signal_id}",
    withdraw_workload_signal,
    methods=["DELETE"],
    status_code=status.HTTP_204_NO_CONTENT,
    route_class_override=HersToSignal,
)


class HelpRequestBody(BaseModel):
    """Optional words with a request for help, and the id of the form that asks, if any.
    The request itself needs no body."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)
    request_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class HelpRequestResponse(BaseModel):
    """What asking did: the request exactly as kept."""

    model_config = ConfigDict(extra="forbid")

    principal: Principal
    request: HelpRequestView


def notes_named_by(state: ApplicationState, requests: list[HelpRequest]) -> NamedCaptures:
    """The notes these requests are about, in one statement for all of them, and in none
    when no request is about a note.

    The notes are context for a request and never the answer itself, and a
    parent's move on it is already written when they are read. So a read of
    them that fails is logged and answered as no notes read, which shows each
    such note as unavailable, and never fails what it is context for.
    """
    try:
        return state.project_state.captures_named(
            request.capture_id for request in requests if request.capture_id
        )
    except Exception:
        logger.exception("the homework notes her requests are about could not be read")
        return NamedCaptures({}, [])


def help_view(
    state: ApplicationState, request: HelpRequest, named: NamedCaptures
) -> HelpRequestView:
    """The request as both pages see it, with the time she asked and each update's time in
    the household's zone, and the note it is about out of ``named``, the notes read for the
    requests being shown."""
    zone = state.clock.zone
    return HelpRequestView(
        about_note=HelpNoteView.about(
            request.capture_id,
            named.notes,
            unreadable_reference=request.capture_reference_unreadable,
        ),
        request_id=request.request_id,
        evening=request.evening,
        asked_at=request.asked_at,
        asked_local=request.asked_at.astimezone(zone),
        note=request.note,
        state=request.state,
        accepted_at=request.accepted_at,
        resolved_at=request.resolved_at,
        resolved_local=(
            None if request.resolved_at is None else request.resolved_at.astimezone(zone)
        ),
        response=request.parent_updates[-1].body if request.parent_updates else None,
        updates=[
            ParentUpdateView(
                update_id=update.update_id,
                body=update.body,
                written_at=update.written_at,
                written_local=(
                    None if update.written_at is None else update.written_at.astimezone(zone)
                ),
            )
            for update in request.parent_updates
        ],
    )


def help_requests_shown(state: ApplicationState, listed: HelpListed) -> list[HelpRequestView]:
    """Her requests as the JSON routes list them: open ones oldest first, then those resolved
    within two weeks, with the notes they name read once for all of them."""
    requests = listed.every()
    about = notes_named_by(state, requests)
    return [help_view(state, request, about) for request in requests]


def set_apart(count: int, *, hers: bool = False) -> str | None:
    """What a help list says of the requests it set apart because they can't be read, or
    ``None`` when it set none apart: how many, and nothing they hold. Beside her own ask
    form, ``hers``, it adds that she can send a new request."""
    if not count:
        return None
    said = f"{count} request{'' if count == 1 else 's'} for help can't be read right now."
    return f"{said} {ASK_ANEW}" if hers else said


@dataclass(frozen=True)
class HelpGroups:
    """Her requests as the Help section on her week shows them: those still open, oldest
    first; those resolved less than ``HELP_RECENT_DAYS`` ago; and those resolved earlier and
    still kept. Each list of resolved requests is most recently resolved first."""

    open: list[HelpRequest]
    recent: list[HelpRequest]
    earlier: list[HelpRequest]
    unreadable: int = 0
    """How many kept requests were set apart because they can't be read."""

    def every(self) -> list[HelpRequest]:
        """Every request in the three lists, in the order the section shows them."""
        return [*self.open, *self.recent, *self.earlier]


def help_groups(held: HelpHeld) -> HelpGroups:
    """Her requests sorted for her page by the instant they were read at, and by nothing
    else: a resolved request is recent while less than ``HELP_RECENT_DAYS`` have passed
    since it was resolved, and among the earlier ones from that moment on, whatever was
    asked after it. Requests stamped alike keep the order of their ids. Pure: it reads no
    clock and no store."""
    recent_for = timedelta(days=HELP_RECENT_DAYS)
    waiting = sorted(
        (request for request in held.requests if request.open),
        key=lambda request: (request.asked_at, request.request_id),
    )
    resolved = sorted(
        (
            (request.resolved_at, request)
            for request in held.requests
            if not request.open and request.resolved_at is not None
        ),
        key=lambda pair: pair[1].request_id,
    )
    resolved.sort(key=lambda pair: pair[0], reverse=True)
    recent: list[HelpRequest] = []
    earlier: list[HelpRequest] = []
    for when, request in resolved:
        (recent if held.now - when < recent_for else earlier).append(request)
    return HelpGroups(waiting, recent, earlier, held.unreadable)


def help_read(state: ApplicationState) -> HelpGroups | None:
    """Her requests for her week, from the one statement a page makes for them, or ``None``
    when that read fails: the file refuses it, or a row cannot be read as a request. The
    failure is logged in one line that names its kind and none of her words, and nothing is
    read again for it. Any other exception is not caught here."""
    try:
        held = state.help_requests.retained()
    except (sqlite3.Error, UnreadableHelpRequest) as error:
        logger.warning("her requests for help could not be read: %s", type(error).__name__)
        return None
    return help_groups(held)


@dataclass(frozen=True)
class HelpMarker:
    """What an address says a help form did, sent a new request (``fresh``) or was sent
    again, and the id it names. A note, checked against the page's one reading and never
    trusted: nothing is written, counted or logged for it, and no control reads it."""

    request_id: str
    fresh: bool


def marker_from(asked: str | None, asked_again: str | None) -> HelpMarker | None:
    """What an address says a help form did: ``asked`` when it names a request, else
    ``asked_again``, else nothing."""
    if asked is not None:
        return HelpMarker(asked, fresh=True)
    if asked_again is not None:
        return HelpMarker(asked_again, fresh=False)
    return None


@dataclass(frozen=True)
class HelpResult:
    """The one line a marker gets: what it says, and the request whose row holds it, or
    ``None`` for the top of the Help section."""

    said: str
    request_id: str | None = None


@dataclass(frozen=True)
class HelpProblem:
    """What a press in the Help section could not do, said there with the focus: a refused
    take-back, with the request it named while that request's row is on the page, or a
    parent's press from a page left open."""

    said: str
    request_id: str | None = None


def help_result_for(
    marker: HelpMarker | None, groups: HelpGroups | None, *, hers: bool
) -> HelpResult | None:
    """The line for what an address says a form did, from the page's one reading. The form
    is hers, so the line is said to her alone: a parent who opens such an address reads her
    requests as they stand and no line. Sent only for a request just sent that still waits
    for a parent; already sent beside any other request on the page, whose row says where it
    stands; and a line at the top of Help when the reading failed or no request on the page
    is the one named."""
    if marker is None or not hers:
        return None
    if groups is None:
        return HelpResult(CANNOT_CHECK)
    try:
        wanted = request_id_from(marker.request_id)
    except NotARequestId:
        return HelpResult(NOT_ON_THIS_PAGE)
    named = next((item for item in groups.every() if item.request_id == wanted), None)
    if named is None:
        return HelpResult(NOT_ON_THIS_PAGE)
    fresh = marker.fresh and named.state == "requested"
    return HelpResult(SENT if fresh else ALREADY_SENT, named.request_id)


def ask_for_help(
    request: Request,
    response: Response,
    state: State,
    payload: Annotated[HelpRequestBody | None, Body()] = None,
) -> HelpRequestResponse:
    """Ask for help today. ``payload`` is optional so an empty POST works. With a
    ``request_id`` the same id sent again is the request it made, 200, and never a second;
    without one every POST asks again, so a retry is not safe. An id whose request can't be
    read asks nothing, 500. ``HersToAsk`` answers a parent 403 before the body is read."""
    note = None if payload is None else payload.note
    form = None if payload is None else payload.request_id
    if form is None:
        asked = state.help_requests.ask(state.clock.today(), note)
    else:
        try:
            outcome = state.help_requests.ask_once(form, state.clock.today(), note)
        except UnreadableHelpRequest as error:
            logger.warning("her request for help could not be sent: %s", type(error).__name__)
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, detail=REQUEST_UNREADABLE
            ) from error
        match outcome:
            case HelpAsked(request=asked):
                pass
            case HelpAlreadyAsked(request=asked):
                response.status_code = status.HTTP_200_OK
            case HelpFormChanged():
                raise HTTPException(status.HTTP_409_CONFLICT, detail=FORM_SENT_OTHER_WORDS)
            case HelpFormUsed():
                raise HTTPException(status.HTTP_409_CONFLICT, detail=FORM_USED)
    return HelpRequestResponse(
        principal=Principal.STUDENT,
        request=help_view(state, asked, notes_named_by(state, [asked])),
    )


router.add_api_route(
    "/help-requests",
    ask_for_help,
    methods=["POST"],
    status_code=status.HTTP_201_CREATED,
    route_class_override=HersToAsk,
)


@router.get("/help-requests")
def her_help_requests(state: State, response: Response) -> list[HelpRequestView]:
    """Her requests as she sees them: open ones, then those resolved within two weeks. One
    that can't be read is left out and counted in the ``Help-Requests-Unreadable`` header."""
    listed = state.help_requests.listed()
    response.headers[UNREADABLE_COUNT] = str(listed.unreadable)
    return help_requests_shown(state, listed)


@router.delete("/help-requests/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
def take_back_help(request: Request, request_id: str, state: State) -> Response:
    """Take a request back while nobody has taken it up; 409 once a parent has, and 500 for
    one that can't be read, which stays. A parent is answered 403: the request is hers to
    take back."""
    if viewer_of(request) == "parent":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=NOT_HERS_TO_ASK)
    try:
        removed = state.help_requests.take_back(request_id)
    except RequestClosed as error:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
    except UnreadableHelpRequest as error:
        logger.warning("her request could not be taken back: %s", type(error).__name__)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, detail=REQUEST_UNREADABLE
        ) from error
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
    dates: DatesNow | None = None,
) -> PlanRead:
    """Her projection of a draft and its reading, from one reading of the record.

    A decided plan is history: it is measured against her signal, as it always
    was, but not against the limit set now or the week, which may well change
    after a parent has said the plan looks good. Work it speaks about that she
    has since reported done is said whatever a parent decided. The notice above
    the plan, the marks beside its rows, and whether the week reads as it did
    all come from one reading of the record, ``everything``, the page's own when
    it has one and read here otherwise: read apart, a report landing between
    them could leave them at odds, and her reports would be read once for each.
    ``current`` is the caller's word that this is today's working plan, the one
    reading that shows marks, and ``today`` is the household day the caller's
    page read, once, so a page rendered across midnight is about one day from
    its heading to its plan. ``dates`` is what stands about the plan's dates in
    that same reading, which the rows show beside what was planned; a caller
    hands it in for the plan in force for its evening and for no other.
    """
    stale = None
    store = state.project_state
    with store.exclusively():
        if everything is None:
            everything = read_everything(store, store, also=record.plan_assignment_ids or ())
        updates = plan_updates(store, record, everything=everything)
        included = done_in(record, updates.done, today=today)
        found = staleness(
            state.workload_signals,
            record,
            settings=state.settings,
            zone=state.clock.zone,
            today=today,
            everything=everything if record.waiting else None,
        )
    match found:
        case Staleness.SIGNALED_SINCE:
            stale = SHE_SIGNALED_SINCE if reader == "family" else SIGNALED_SINCE
        case Staleness.SIGNAL_ENDED:
            stale = SIGNAL_ENDED
        case Staleness.OVER_THE_LIMIT:
            stale = over_the_limit(limit_now(record, state.settings))
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
        evidence_for=lambda name: details_href(name, fragment=EVIDENCE, return_to="today"),
        on_record=updates.on_record,
        done=done_marks(updates),
        unread=updates.unread,
        now=dates,
    )
    return PlanRead(view=view, reading=reading)


def plan_view(
    state: ApplicationState, record: DraftRecord, reader: Reader = "student"
) -> StudentPlanView:
    """Her projection of a draft: the plan, a parent's review if any, and whether it still fits,
    in the words of ``reader``."""
    return read_a_plan(state, record, reader=reader, current=False, today=state.clock.today()).view


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


def planned_minutes(read: PlanRead, zone: ZoneInfo) -> int | None:
    """The minutes a plan's blocks ask for as saved, measured on its evening as the plan check
    measured them; ``None`` for a plan kept as text alone."""
    saved = read.reading.saved_plan
    return None if saved is None else saved.total_minutes(zone, on=read.view.plan_date)


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


@router.post(
    "/plans",
    response_model=StudentPlanView | PublishedRunView,
    status_code=status.HTTP_201_CREATED,
    responses=PLAN_ANSWERS,
)
async def make_todays_plan(
    request: Request, state: State, graphs: Graphs
) -> StudentPlanView | JSONResponse:
    """Ask for today's plan. It is hers as soon as it is made; a parent's review comes after.

    A run that ends without a plan, because the checks never passed, the
    model did not answer, or it failed on the way, is a 409 saying why in the
    reader's words, and her page keeps whatever plan it had. An evening with
    nothing left to plan is a 409 too, before any run is written or a model
    asked for, and so is a press while the household's run is still running,
    naming it. A plan whose saving couldn't be confirmed in time is a 202 with
    its run's id, to check at ``plans/runs/{run_id}``; a plan not saved and a
    run that couldn't start are 503s that say her updates are saved, in the
    reader's words. The plan answered is the record its publication returned,
    read for the reader on a worker thread by the run's deadline and its settle
    grace; when that reading doesn't finish in time, or no time is left, the plan
    stands published and the 201 names its run instead.
    """
    budget = graphs.budget()
    today = state.clock.today()
    parent = parent_reads(request)
    try:
        await require_work(state, today, budget)
        require_model(graphs)
        made = await make_plan(graph_for_a_run(graphs), today, state, budget=budget)
    except AlreadyPlanning as error:
        raise AlreadyPlanning(error.run, parent=parent) from None
    except NotSaved as error:
        raise HTTPException(
            error.status_code, detail=not_saved(parent=parent, kept=error.kept)
        ) from None
    except CouldNotStart as error:
        raise HTTPException(
            error.status_code, detail=f"{COULD_NOT_START} {saved_sentence(parent=parent)}"
        ) from None
    except Unconfirmed as unconfirmed:
        return JSONResponse(
            unconfirmed.view().model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED
        )
    except HTTPException:
        raise
    except Exception as error:
        logger.exception("today's plan failed on the way")
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=ended_without_a_plan(INTERRUPTED, parent=parent)
        ) from error
    if made.record is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=ended_without_a_plan(
                made.view.outcome, parent=parent, past_due=made.view.past_due
            ),
        )
    record = made.record
    reader: Reader = "family" if parent else "student"
    # The reading has what is left of the deadline and its grace, and at most the store's wait.
    left = budget.seconds - budget.elapsed() + SETTLE_GRACE_SECONDS
    if left > 0:
        try:
            return await bounded(
                partial(plan_view, state, record, reader), min(left, STORE_WAIT_SECONDS)
            )
        except Exception as error:
            if not isinstance(error, Unfinished):
                logger.warning("the plan of run %s could not be read: %s", record.thread_id, error)
    published = PublishedRunView(run_id=record.thread_id, plan_date=record.plan_date)
    return JSONResponse(published.model_dump(mode="json"), status_code=status.HTTP_201_CREATED)


@router.get("/plans/runs/{run_id}", response_model=RunStatusView, responses=RUN_STATUS_ANSWERS)
async def plan_run(run_id: str, state: State) -> RunStatusView:
    """Where one planning run stands, after ending any run past its deadline."""
    return await run_status_view(state, run_id)


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
    outside_window: WindowSide | None = None,
    hand_in: HandInView | None = None,
    claims_unreadable: bool = False,
    instructions: InstructionsStanding | None = None,
    instructions_unreadable: bool = False,
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
    ``outside_window`` is kept only while every claim reads as a date, since one that
    cannot be read could put the work anywhere.
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
        claims_unreadable=claims_unreadable,
        source_claims=list(dict.fromkeys(record.spoken() for record in records)),
        disagreement=disagreement,
        contradiction=[record.spoken() for record in readable] if noticed.contradicted else [],
        school_contradicts=noticed.contradicted
        and any(record.channel in SCHOOL_CHANNELS for record in readable),
        assigned_on=assignment.assigned_on,
        note=assignment.note,
        note_by=assignment.note_by,
        words=school_words(assignment, instructions, instructions_unreadable),
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
        outside_window=None if unreadable or claims_unreadable else outside_window,
        check_school=status is not None and status.check_the_school_record,
        checked_on=None if status is None or status.check is None else status.check.checked_on,
        check_note=None if status is None or status.check is None else status.check.note,
        updates_unavailable=status is not None and status.updates_unavailable,
        checks_unavailable=status is not None and status.checks_unavailable,
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
    plan: PlanRead | None | Unread = UNREAD,
    today: date | None = None,
    everything: Everything | None = None,
    groups: HelpGroups | None | Unread = UNREAD,
    named: NamedCaptures | None = None,
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
    page can still show it. ``groups`` is her requests for help as the
    caller read them, ``None`` when that read failed, and ``named`` the notes
    they name; each is read here when left out.
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
    noticed = noticings_of(found)
    shown = week_from(found, frame.start, noticed=noticed)
    window = planning_week(found, today, noticed=noticed)
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
    # The window itself: earlier work she chose is planned, and its card still says it was
    # due before today.
    in_window = {
        item.assignment_id
        for item in window.assignments
        if item.assignment_id not in window.catch_up
    }
    todays = planning_window(today)

    def beside_view(item: Assignment) -> StudentAssignmentView:
        records = found.records[item.assignment_id]
        noticed = notice_due_date(expect_due_date(item), records)
        return assignment_view(
            item,
            records,
            noticed,
            found.statuses.get(item.assignment_id),
            in_planning_window=item.assignment_id in in_window,
            outside_window=todays.outside(item, noticed),
            claims_unreadable=item.assignment_id in found.claims_unavailable,
            hand_in=hand_in_of(found, item.assignment_id),
            instructions=found.instructions.get(item.assignment_id),
            instructions_unreadable=item.assignment_id in found.instructions_unavailable,
        )

    # Never filter here; see the module docstring.
    views = [
        assignment_view(
            item,
            shown.records[item.assignment_id],
            shown.noticings[item.assignment_id],
            shown.statuses.get(item.assignment_id),
            in_planning_window=item.assignment_id in in_window,
            outside_window=todays.outside(item, shown.noticings[item.assignment_id]),
            claims_unreadable=item.assignment_id in shown.claims_unavailable,
            hand_in=hand_in_of(found, item.assignment_id),
            instructions=found.instructions.get(item.assignment_id),
            instructions_unreadable=item.assignment_id in found.instructions_unavailable,
        )
        for item in shown.assignments
    ]
    tonight = state.workload_signals.for_evening(today)
    household = state.settings
    still_to_turn_in = to_turn_in(found)
    if isinstance(groups, Unread):
        groups = help_read(state)
    if named is None:
        named = NamedCaptures({}, []) if groups is None else notes_named_by(state, groups.every())

    def help_views(requests: list[HelpRequest]) -> list[HelpRequestView]:
        return [help_view(state, request, named) for request in requests]

    # The saved plan against the limit set now, which need not be the one it was made under:
    # a shorter plan whose blocks fit uses it, one over it calls for a smaller plan, and one
    # kept as text alone, which can't be measured, is said to do neither.
    saved = todays_plan_read(state, today=today) if isinstance(plan, Unread) else plan
    budget = household.too_much_minutes if tonight else household.evening_minutes
    shorter = bool(tonight) and saved is not None and saved.view.too_much
    measured = None if saved is None else planned_minutes(saved, state.clock.zone)
    fits = shorter and measured is not None and measured <= budget
    over = shorter and measured is not None and measured > budget
    return StudentDueThisWeekView(
        generated_at=datetime.now(UTC),
        today=today,
        week=frame,
        assignments=views,
        assigned_this_week=[beside_view(item) for item in later],
        plan_horizon_end=today + DUE_THIS_WEEK_SPAN,
        full_budget_minutes=household.evening_minutes,
        budget_minutes=budget,
        plan=None if saved is None else saved.view,
        to_turn_in=still_to_turn_in.rows,
        to_turn_in_unreadable=still_to_turn_in.unreadable,
        can_plan=model_configured(state.settings),
        too_much=signal_view(state, tonight[-1]) if tonight else None,
        plan_uses_limit=fits,
        smaller_wanted=bool(tonight) and (not shorter or over),
        signals=[signal_view(state, signal) for signal in state.workload_signals.held()],
        help_open=[] if groups is None else help_views(groups.open),
        help_recent=[] if groups is None else help_views(groups.recent),
        help_earlier=[] if groups is None else help_views(groups.earlier),
        help_unavailable=groups is None,
        help_set_apart=(
            None if groups is None else set_apart(groups.unreadable, hers=viewer != "parent")
        ),
        viewer=viewer,
        can_update=viewer != "parent",
        nothing_to_plan=not window.active(),
        apart=beside_view(elsewhere[0]) if elsewhere else None,
        earlier=earlier_views(found, today, noticed),
    )


def earlier_views(
    found: Everything, today: date, noticed: Mapping[str, Noticing]
) -> list[EarlierWorkView]:
    """Earlier homework to check at one reading, with her standing update and her choices for
    today and yesterday, folded as ``fold_earlier`` says. Today's plan date decides it, never
    the week a page shows."""
    chosen = found.catch_up.get(today, frozenset())
    before = found.catch_up.get(today - timedelta(days=1), frozenset())
    views = []
    for item in earlier_to_check(found, today, noticed=noticed):
        standing = found.statuses.get(item.assignment_id)
        said = noticed[item.assignment_id]
        given = {day for day in (item.due_date, *said.observed_dates) if day is not None}
        views.append(
            EarlierWorkView(
                assignment_id=item.assignment_id,
                title=item.title,
                course=item.course,
                due_date=item.due_date,
                earliest=min(given),
                dates_differ=len(given) > 1,
                chosen=item.assignment_id in chosen,
                chosen_yesterday=item.assignment_id in before,
                update=earlier_update(standing),
            )
        )
    return fold_earlier(views, today)


def earlier_update(standing: AssignmentStatus | None) -> str:
    """Her standing update on earlier work: ``not_yet``, ``unavailable`` when her updates
    can't be read, or ``none`` when she has said nothing."""
    if standing is not None and standing.updates_unavailable:
        return "unavailable"
    return "not_yet" if standing is not None and standing.status == "not_yet" else "none"


def fold_earlier(earlier: list[EarlierWorkView], today: date) -> list[EarlierWorkView]:
    """Which items show and which wait in the fold. Up to ``EARLIER_SHOWN`` due in the
    ``EARLIER_DAYS`` before today show, newest first, and so does anything chosen for today
    or yesterday, whatever its age; the rest are folded. Nothing here changes a choice or
    an update."""
    # Counted in days, so a clock pinned near the first day the calendar holds still works.
    first = today.toordinal() - EARLIER_DAYS
    recent = [
        item.assignment_id
        for item in earlier
        if first <= item.earliest.toordinal() < today.toordinal()
    ]
    shown = set(recent[:EARLIER_SHOWN])
    shown.update(item.assignment_id for item in earlier if item.chosen or item.chosen_yesterday)
    return [item.model_copy(update={"folded": item.assignment_id not in shown}) for item in earlier]


def receipt_holds(note: "EarlierNote", earlier: list[EarlierWorkView]) -> bool:
    """Whether today's choices still agree with what a press said: the item in today's plan
    after an Include, out of it after a Remove."""
    chosen = {item.assignment_id: item.chosen for item in earlier}
    return note.said is None or chosen.get(note.assignment_id) == (note.said in EARLIER_IN)


def kept_in_place(
    earlier: list[EarlierWorkView], note: "EarlierNote | None"
) -> list[EarlierWorkView]:
    """The item a press named, shown where she pressed it, above the fold or in it, so her
    place holds until her next visit wherever the rules would now put it."""
    if note is None or note.folded is None:
        return earlier
    return [
        item.model_copy(update={"folded": note.folded})
        if item.assignment_id == note.assignment_id
        else item
        for item in earlier
    ]


def as_sentence(reason: str) -> str:
    """A reason from a plan, ended once so the next sentence doesn't run into it, or nothing
    when it shows no words at all."""
    if not any(char.isprintable() and not char.isspace() for char in reason):
        return ""
    last = reason.rstrip().rstrip(CLOSING_MARKS)[-1:]
    return reason if last and last in SENTENCE_ENDS else f"{reason}."


def in_todays_plan(
    earlier: list[EarlierWorkView], record: DraftRecord | None, reading: PlanReading | None
) -> list[EarlierWorkView]:
    """Where today's plan put each piece of earlier work it was made with: worked on, put off
    with its reason, or somewhere its rows can't say when only its text can be read. Work she
    chose after the plan was made is not in it. A saved plan never changes, so work she took
    out of today's choices keeps its place there, shown above the fold, until she plans
    again."""
    if record is None:
        return earlier
    named = set(record.plan_assignment_ids or ())
    put_off = (
        {row.work.assignment_id: row.reason for row in reading.deferrals}
        if reading is not None and reading.structured
        else None
    )
    shown = []
    for item in earlier:
        if item.assignment_id not in named:
            shown.append(item.model_copy(update={"in_plan": "missing"}) if item.chosen else item)
            continue
        if put_off is None:
            placed = {"in_plan": "unknown"}
        elif item.assignment_id in put_off:
            reason = as_sentence(put_off[item.assignment_id])
            placed = {"in_plan": "put_off", "put_off_reason": reason}
        else:
            placed = {"in_plan": "scheduled"}
        shown.append(item.model_copy(update={**placed, "folded": False}))
    return shown


def viewer_of(request: Request) -> str:
    """Who is at the keyboard: ``student`` or ``parent`` as the gate read the sign-in, or
    ``anyone`` while the sign-in is off and the gate says nothing."""
    role = getattr(request.state, "household", None)
    if role is Principal.STUDENT:
        return "student"
    if role is Principal.PARENT:
        return "parent"
    return "anyone"


def parent_reads(request: Request) -> bool:
    """Whether a page's words are said to a parent. They follow who is reading, never the
    tree a page's address is in on its own: a parent may open her pages and is told about
    her there, and she reads her pages as hers. With the sign-in off nobody is known, and
    the tree decides: a page under the family's address reads as a parent's."""
    viewer = viewer_of(request)
    if viewer != "anyone":
        return viewer == "parent"
    path = request.url.path
    return path == FAMILY_PAGE or path.startswith(FAMILY_PAGE + "/")


@dataclass(frozen=True)
class HelpForm:
    """Her Ask for help form as a refusal shows it again: her words as typed, what went
    wrong, and whether it was her words themselves, which then take the cursor."""

    words: str = ""
    problem: str | None = None
    at_words: bool = False


@dataclass(frozen=True)
class CardState:
    """What one card shows beyond its record: a confirmation, its form open, or a problem.

    ``said`` is the sentence a save or an undo left for the card; ``change``
    opens the form on a card with a standing update, with ``status`` and
    ``note`` as she had them, so nothing she typed is lost to a refusal; and
    ``problem`` is what the save could not do. ``field`` says which field the
    problem is about, ``status`` or ``note``, so the page can mark that field,
    tie the words to it, and put the cursor there; a problem about neither,
    an update saved elsewhere among them, takes the focus on the card, and is
    said at the top of the page too, with a link to the card. ``saved_elsewhere``
    marks the refusal that comes with a newer update to look at. ``well_done`` is the words
    for a Done the save has just made, on the page that answers it alone. ``undo_event`` is
    the undo an address says an Undo made: an id, which only her latest event can bear out.
    """

    assignment_id: str
    said: str | None = None
    well_done: str | None = None
    change: bool = False
    problem: str | None = None
    field: str | None = None
    status: str | None = None
    note: str | None = None
    saved_elsewhere: bool = False
    undo_event: str | None = None


@dataclass(frozen=True)
class InPlace:
    """The cards her saves keep where they were for the rest of one visit to her week.

    Each is a card's key (``place_key``) and whether it is shown with the work reported done. A
    card saved Done stays among the active cards, and one changed or undone from Done stays
    in Reported done, until her next visit groups the week by its updates again. Only the
    page uses it; what is saved and what a plan reads never do.
    """

    cards: tuple[tuple[str, bool], ...] = ()

    def shown_done(self, assignment_id: str, done: bool) -> bool:
        """Whether a card is shown with the work reported done: where this visit keeps it,
        or by its update when the visit keeps it nowhere."""
        key = place_key(assignment_id)
        return next((kept for name, kept in self.cards if name == key), done)

    def keeping(self, assignment_id: str, done: bool) -> "InPlace":
        """These cards and one more, kept where it is shown now."""
        return self.holding(place_key(assignment_id), done)

    def holding(self, key: str, done: bool) -> "InPlace":
        """These cards and the one with this key. A card kept already stays where it was
        first kept, and only the latest ``IN_PLACE_MAX`` are kept."""
        if any(name == key for name, _ in self.cards):
            return self
        return InPlace((*self.cards, (key, done))[-IN_PLACE_MAX:])

    def said(self) -> str:
        """The cards as a form, an address and the cookie carry them: each key after ``d:``
        or ``a:``, joined by ``|``."""
        return "|".join(f"{'d' if done else 'a'}:{key}" for key, done in self.cards)

    @classmethod
    def read(cls, given: str | None) -> "InPlace":
        """The cards a form, an address or the cookie named, the latest ``IN_PLACE_MAX`` of
        them. A part that names no card's place, a card just made Done or anything these
        pages never write, is passed over: the value decides only where a card is shown."""
        kept = cls()
        for part in (given or "").split("|")[-IN_PLACE_MAX:]:
            mark, _, key = part.partition(":")
            if mark in ("a", "d") and len(key) == IN_PLACE_KEY and set(key) <= HEX_DIGITS:
                kept = kept.holding(key, mark == "d")
        return kept


HEX_DIGITS: Final = frozenset("0123456789abcdef")
"""The characters of a key as ``place_key`` writes it."""


def place_key(assignment_id: str) -> str:
    """The key a card is kept in place by: the start of its id's SHA-256, in hex. Ids that
    differ in any way, case and spacing included, get different keys."""
    return hashlib.sha256(assignment_id.encode()).hexdigest()[:IN_PLACE_KEY]


def carried(in_place: InPlace | None) -> str | None:
    """The cards an address to her week carries for the page to keep in place, or ``None``
    when it keeps none."""
    return None if in_place is None else in_place.said() or None


def leave_in_place[Answered: Response](
    response: Answered, kept: InPlace, landing: str, made_done: str | None = None
) -> Answered:
    """Leave the cards a press keeps in place for the one page whose address names this
    landing, after the key of the card ``made_done`` names when the press has just made it
    Done; with none kept, nothing is left."""
    if not kept.cards:
        return response
    done = "" if made_done is None else f"{IN_PLACE_DONE}:{place_key(made_done)}|"
    response.set_cookie(
        landing_cookie(landing),
        done + kept.said(),
        max_age=IN_PLACE_SECONDS,
        path=WEEK_PAGE,
        httponly=True,
        samesite="lax",
    )
    return response


def sent_in_place(location: str, kept: InPlace, made_done: str | None = None) -> RedirectResponse:
    """The redirect to her week that keeps these cards in place on the page it lands on and
    on no other: its address names a new landing, and the cookie that holds the cards, and
    the card the press has just made Done, is named for it. With none kept, the address is
    sent as it is."""
    if not kept.cards:
        return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)
    landing = secrets.token_hex(IN_PLACE_LANDING_DIGITS // 2)
    moved = RedirectResponse(
        str(URL(location).include_query_params(**{IN_PLACE_LANDING: landing})),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    return leave_in_place(moved, kept, landing, made_done)


def newly_done(left: str | None) -> str | None:
    """The key of the card a save has just made Done, when the cookie it left for its landing
    names one ahead of the cards it keeps in place."""
    mark, _, key = (left or "").partition("|")[0].partition(":")
    return key if mark == IN_PLACE_DONE else None


def landing_cookie(landing: str) -> str:
    """The name of the cookie that holds the cards kept for one landing."""
    return f"{IN_PLACE_COOKIE}-{landing}"


def forget_landing[Answered: Response](response: Answered, landing: str) -> Answered:
    """Clear the cookie one landing's cards were left in."""
    response.delete_cookie(landing_cookie(landing), path=WEEK_PAGE, httponly=True, samesite="lax")
    return response


def done_cookie(landing: str) -> str:
    """The name of the cookie that tells one landing on an assignment's details that the save
    it answers has just made the assignment Done."""
    return f"{DONE_COOKIE}-{landing}"


def forget_done[Answered: Response](response: Answered, landing: str) -> Answered:
    """Clear the cookie one landing on the details was told of a new Done in."""
    response.delete_cookie(done_cookie(landing), path=DETAILS, httponly=True, samesite="lax")
    return response


def sent_done_to_the_details(location: str, assignment_id: str) -> RedirectResponse:
    """The redirect back to an assignment's details after a save that has just made it Done:
    its address names a new landing, and a cookie named for it, for the details alone, says
    so to the one page that answers it, as her week's landing cookie does. It holds no card,
    since the details keep none in place."""
    landing = secrets.token_hex(IN_PLACE_LANDING_DIGITS // 2)
    moved = RedirectResponse(
        str(URL(location).include_query_params(**{IN_PLACE_LANDING: landing})),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    moved.set_cookie(
        done_cookie(landing),
        f"{IN_PLACE_DONE}:{place_key(assignment_id)}",
        max_age=IN_PLACE_SECONDS,
        path=DETAILS,
        httponly=True,
        samesite="lax",
    )
    return moved


def sent_removed(location: str) -> RedirectResponse:
    """The redirect to her week after a signal of hers is removed: its address names a new
    landing and the line that says what stands, and the cookie named for the landing tells
    that one page, and no other, that her request isn't active. It holds no card."""
    landing = secrets.token_hex(IN_PLACE_LANDING_DIGITS // 2)
    moved = RedirectResponse(
        str(URL(location).include_query_params(**{IN_PLACE_LANDING: landing})),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    moved.set_cookie(
        landing_cookie(landing),
        SIGNAL_REMOVED,
        max_age=IN_PLACE_SECONDS,
        path=WEEK_PAGE,
        httponly=True,
        samesite="lax",
    )
    return moved


def named_once(request: Request, name: str) -> str | None:
    """The value her week's address gives a query, or ``None`` when it gives none or gives
    more than one."""
    named = request.query_params.getlist(name)
    return named[0] if len(named) == 1 else None


def landing_asked(request: Request) -> str | None:
    """The landing her week's address names, or ``None`` when it names none, names one more
    than once, or names one in a form these pages don't write."""
    named = named_once(request, IN_PLACE_LANDING)
    if named is None or len(named) != IN_PLACE_LANDING_DIGITS:
        return None
    return named if set(named) <= HEX_DIGITS else None


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


@dataclass(frozen=True)
class ListCard:
    """What a visit adds to her To turn in list: what a press or an undo just did, or what
    one could not do. ``asked`` is what the address named and ``attempt`` what was refused,
    both before the record was read; ``result`` and ``refusal`` are what the page's one
    reading makes of them."""

    asked: Receipt | None = None
    result: ListResult | None = None
    problem: str | None = None
    attempt: Attempt | None = None
    refusal: ListRefusal | None = None


def receipt_asked(said: str | None, about: str | None, event_id: str | None) -> ListCard | None:
    """What an address says a press on the list did, held to the length of any id the store
    makes. An address that names no event, or one too long to be one, says nothing."""
    if not said or not about or not event_id or len(event_id) > TOKEN_MAX_LENGTH:
        return None
    return ListCard(asked=Receipt(said, about, event_id))


def list_card_shown(card: ListCard | None, everything: Everything, viewer: str) -> ListCard | None:
    """A visit's card read against the page's one reading. A result is hers: a parent who
    opens such an address is shown the list and no result, and a card that comes to
    nothing is no card."""
    if card is None:
        return None
    shown = replace(
        card,
        result=None if viewer == "parent" else result_for(everything, card.asked),
        refusal=refusal_for(everything, card.attempt),
    )
    return shown if shown.result is not None or shown.problem is not None else None


def as_read_by[Shown: (CardState, HandInCard)](shown: Shown | None, viewer: str) -> Shown | None:
    """What a visit shows beyond the record, for who is reading. A result is hers, as on her
    To turn in list: a parent who opens an address her save or undo left reads what stands,
    in the parent's words, and no result."""
    if shown is None or viewer != "parent":
        return shown
    return replace(shown, said=None)


def student_page(
    request: Request,
    state: ApplicationState,
    *,
    week: date | None = None,
    problem: str | None = None,
    refreshed: bool = False,
    card: CardState | None = None,
    plan_asked: bool = False,
    turning_in: ListCard | None = None,
    help_form: HelpForm | None = None,
    help_marker: HelpMarker | None = None,
    help_problem: HelpProblem | None = None,
    pressed: bool = False,
    plan_failure: PlanFailure | None = None,
    earlier_note: "EarlierNote | None" = None,
    in_place: InPlace | None = None,
    status_code: int = status.HTTP_200_OK,
    today: date | None = None,
    run_notice: RunNotice | None = None,
    signal_removed: bool = False,
    plan_answer: PlanAnswer | None = None,
) -> HTMLResponse:
    """Render her page. ``problem`` is what an action could not do, said once at the top.

       ``refreshed`` says when the page was last asked for; ``card`` is what one
       card shows beyond its record. Both change how the page is presented and
       nothing else. ``plan_asked`` says the address asked for today's plan, as a
       way back to it does: when the day has no plan, the place the plan would be
       is still there to land on, and says so, which an ordinary visit to a day
       with no plan has no need of. Today's saved plan is unfolded on every visit,
       so finding her next step takes no remembered action. The household day is
       read once, here, unless the caller read it as ``today`` to check what the address
       says, and everything on the page is about that day: the heading,
       the week, the planning window, which plan is today's, its notice, and the
       marks beside its rows. The record is read once too, and all of those are
       about that one reading. A card's problem is said at the top too, with a link
       to the card, so it is met on a page that opens at its top; the card named is
       shown even when its dates have taken it out of the week. The card's own line
       is the alert and takes the focus there, unless its field does. A problem no
       card on the page holds is the top line's alone. ``pressed`` says a form's press
       brought her here, and then that top line takes the focus; a visit by address
       asks for none. ``plan_failure`` is a plan press that ended without a plan: the
       top line then links to her homework, and to any assignment it names, and the plan
       button offers to try again when another press may help. ``help_form`` is her Ask for
       help form as a refusal shows it again, beside the form; ``help_marker`` is what the
       address says a help form did, checked against the page's one reading of her requests;
       and ``help_problem`` is what a press in the Help section could not do, said there. Each
       Ask for help form gets a fresh id, which needs no read. ``earlier_note`` is what a choice
       in Earlier homework to check did or could not do, said beside the item it named.
       ``in_place`` is the cards this visit keeps where they were; every update form on the
       page carries them on, with its own card kept where it is shown. ``signal_removed`` says
       an Undo or a Remove just took a request of hers away, which Today says to her alone,
       beside what the signals kept for today say now.
    ``plan_answer`` is a plan press's answer, its line
       made from what this page shows: today's plan, and the plan button. Its first sentence,
       as the answer is built, takes the focus in place of the line.
    """
    viewer = viewer_of(request)
    # An address that only brings a card into view, as a way back from its details does.
    only_shown = card is not None and card == CardState(card.assignment_id)
    card = as_read_by(card, viewer)
    today = state.clock.today() if today is None else today
    # The newest plan the plan button knows is read before the plan the page shows, so a
    # plan published between the two reads makes the press stale, never the reverse.
    newest = state.drafts.newest_published()
    # The record is read once for the page, with today's plan's assignments
    # named to it: the week, the planning window, the plan's notice and
    # marks, and whether the plan still fits all come out of that reading.
    record = state.drafts.latest_for(today)
    # Kept for a page that cannot read the record after this, which shows it as saved.
    request.state.plans_read = () if record is None else (record,)
    # The assignment an address says a press was about is named to the reading too, so a
    # result is checked against that assignment's own events even when it is off the record.
    about = () if turning_in is None or turning_in.asked is None else (turning_in.asked.about,)
    # Her homework notes are read beside the record, in the same snapshot, and are no part
    # of it: nothing a plan, a digest, or a brief is made from ever holds one. Her requests
    # for help come in one statement, and the notes they are about in one more, however
    # many there are; when the requests can't be read, neither is anything they name.
    groups = help_read(state)
    with state.project_state.reading():
        everything = read_everything(
            state.project_state,
            state.project_state,
            also=(*(() if record is None else record.plan_assignment_ids or ()), *about),
        )
        notes = state.project_state.outstanding_captures()
        named = NamedCaptures({}, []) if groups is None else notes_named_by(state, groups.every())
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
            # Today's latest plan is the plan in force for today by the store's own rule.
            dates=dates_now(everything, record.plan_assignment_ids or ()),
        )
    )
    view = build_student_due_this_week_view(
        state,
        week,
        viewer=viewer,
        focus=None if card is None else card.assignment_id,
        plan=todays,
        today=today,
        everything=everything,
        groups=groups,
        named=named,
    )
    planned = in_todays_plan(view.earlier, record, None if todays is None else todays.reading)
    view = view.model_copy(update={"earlier": kept_in_place(planned, earlier_note)})
    problem_opening: tuple[str, str] | None = None
    if plan_answer is not None:
        # A press's line says a plan is shown, or names the button, only when this page does.
        button = view.can_plan and plan_answer.offers_form and not view.nothing_to_plan

        def shows(needs: Needs) -> bool:
            return (
                needs == "nothing"
                or (needs == "plan" and view.plan is not None)
                or (needs == "button" and button)
            )

        problem = plan_answer.said(shows)
        problem_opening = plan_answer.opening(shows)
    if earlier_note is not None and (
        viewer == "parent" or not receipt_holds(earlier_note, planned)
    ):
        earlier_note = replace(earlier_note, said=None)
    help_result = help_result_for(help_marker, groups, hers=not parent_reads(request))
    on_page = set() if groups is None else {item.request_id for item in groups.every()}
    folded = set() if groups is None else {item.request_id for item in groups.earlier}
    if help_problem is not None and help_problem.request_id not in on_page:
        help_problem = replace(help_problem, request_id=None)
    named_rows = {
        None if help_result is None else help_result.request_id,
        None if help_problem is None else help_problem.request_id,
    }
    listed = [*view.assignments, *view.assigned_this_week, *([view.apart] if view.apart else [])]
    kept = InPlace() if in_place is None else in_place
    shown_done = {
        item.assignment_id
        for item in listed
        if kept.shown_done(item.assignment_id, item.update_status == "done")
    }
    for item in listed:
        card = said_after_undo(card, item)
    held_week = held_from_its_group(view.assignments, shown_done)
    held_later = held_from_its_group(view.assigned_this_week, shown_done)
    by_a_card = card is not None and card.problem is not None and problem is None
    # A card's problem is said on the card only when the card is on the page: a form made
    # for an id that is not on record is refused before any lookup, and has no card.
    about_a_card = (
        by_a_card
        and card is not None
        and card.assignment_id in {item.assignment_id for item in listed}
    )
    return templates.TemplateResponse(
        request,
        "student_due_this_week.html",
        {
            "view": view,
            "report_contexts": {
                item.assignment_id: week_context(
                    item.assignment_id,
                    view.week.start,
                    viewer,
                    kept.keeping(item.assignment_id, item.assignment_id in shown_done),
                )
                for item in listed
            },
            "shown_done": shown_done,
            # Whether a card of this week's list, or of the work given out for later, is shown
            # away from the group its saved update puts it in, and whether every card held so
            # has an update standing, which an Undo can leave it without; and the plain
            # address that asks for the week again, grouped, at its homework heading.
            "held": {
                "week": bool(held_week),
                "later": bool(held_later),
                "updated": all(
                    item.update_status is not None for item in (*held_week, *held_later)
                ),
            },
            "refresh_list": address(
                WEEK_PAGE,
                fragment=HOMEWORK,
                week=None if view.week.current else view.week.start.isoformat(),
            ),
            # What Earlier homework to check's presses carry, so a choice keeps every card
            # this visit keeps where it was.
            "earlier_in_place": kept.said(),
            "shown_gone": only_shown
            and card is not None
            and card.assignment_id not in {item.assignment_id for item in listed},
            "problem": card.problem if card is not None and by_a_card else problem,
            "problem_target": card.assignment_id if card is not None and about_a_card else None,
            "pressed": pressed,
            "problem_opening": problem_opening,
            "plan_failure": plan_failure,
            # A fresh form for every page, except one that answers a run that may still
            # publish, which offers only the check.
            "plan_form": None
            if plan_failure is not None and plan_failure.uncertain
            else fresh_plan_form(state, today, newest).fields(),
            "plan_label_mark": PLAN_LABEL_MARK,
            "run_notice": run_notice,
            # A notice about a run of today's evening links the past-due work it kept, as
            # the answer to its press does.
            "notice_checks": () if run_notice is None else date_checks(run_notice.past_due),
            "signal_removed": signal_removed and viewer != "parent",
            "in_place_said": kept.said(),
            "plan_reading": None if todays is None else todays.reading,
            "plan_asked": plan_asked,
            "list_card": list_card_shown(turning_in, everything, viewer),
            "hand_in_routes": hand_in_actions,
            "compact_rows": COMPACT_ROWS,
            "to_turn_in_page": TO_TURN_IN_PAGE,
            "homework_notes": notes.notes,
            "homework_notes_unreadable": notes.unreadable,
            "remains": what_remains(
                notes.notes, {pair(item.course, item.title) for item in everything.assignments}
            ),
            "notes_page": NOTES_PAGE,
            "new_note_page": NEW_NOTE_PAGE,
            "viewer": viewer,
            "no_plan_now": NO_PLAN_NOW,
            # Today's window, which every week shown names, and the planner reads.
            "planning_window": planning_window(view.today).said(),
            "planning_window_end": planning_window(view.today).end,
            # Her Help, where a card that names it sends her: on this week, or this week's page.
            "help_href": (
                f"#{ASK_FOR_HELP}"
                if view.week.current
                else address(WEEK_PAGE, fragment=ASK_FOR_HELP)
            ),
            "parent": parent_reads(request),
            "refreshed_at": local_now(state.clock.zone) if refreshed else None,
            "note_max_length": NOTE_MAX_LENGTH,
            "help_form": help_form or HelpForm(),
            "help_request_id": new_request_id(),
            "help_result": help_result,
            "help_problem": help_problem,
            "help_fold_open": bool(named_rows & folded),
            "family_help": FAMILY_HELP,
            "update_note_max_length": UPDATE_NOTE_MAX_LENGTH,
            "card": card,
            "marks": state.settings.page_marks,
            "earlier_note": earlier_note,
            "earlier_listed": {item.assignment_id for item in view.earlier},
            "earlier_by_id": {item.assignment_id: item for item in view.earlier},
            "earlier_anchor": earlier_anchor,
            "earlier_card_anchor": earlier_card_anchor,
            "earlier_action": earlier_action,
            "earlier_made_with": {
                item.assignment_id: earlier_made_with(
                    state.result_key,
                    today,
                    item.assignment_id,
                    "remove" if item.chosen else "include",
                )
                for item in view.earlier
            }
            if viewer != "parent"
            else {},
        },
        status_code=status_code,
    )


def card_shown(
    saved: str | None,
    same: str | None,
    undone: str | None,
    change: str | None,
    show: str | None,
    undo_event: str | None = None,
) -> CardState | None:
    """What the address says about one card, read in a fixed order and one thing at a time,
    an undo with the undo it names. An id to change that is longer than any the store makes
    opens nothing."""
    for said, given in (("saved", saved), ("same", same), ("undone", undone)):
        if given:
            named = undo_event if said == "undone" else None
            return CardState(given, said=CONFIRMATIONS[said], undo_event=named)
    if change and len(change) <= TOKEN_MAX_LENGTH:
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
    undo_event: Annotated[
        str | None, Query(description="the undo that press made; compared, never trusted")
    ] = None,
    change: Annotated[
        str | None, Query(description="the assignment whose update form to open; changes nothing")
    ] = None,
    show: Annotated[
        str | None, Query(description="the assignment to bring into view; changes nothing")
    ] = None,
    hand_in_said: Annotated[
        str | None, Query(description="what a press on the To turn in list just did; a note")
    ] = None,
    about: Annotated[str | None, Query(description="the assignment it was about")] = None,
    hand_in_event: Annotated[
        str | None, Query(description="the event that press made; looked up, never trusted")
    ] = None,
    asked: Annotated[
        str | None, Query(description="the request a help form just made; a note, checked")
    ] = None,
    asked_again: Annotated[
        str | None, Query(description="the request a help form sent twice had made; a note")
    ] = None,
    earlier: Annotated[
        str | None, Query(description="the earlier work a choice was about, or to show")
    ] = None,
    earlier_said: Annotated[
        str | None, Query(description="what that choice did, signed; a note, checked")
    ] = None,
    earlier_place: Annotated[
        str | None, Query(description="where that item was shown, list or fold; a note")
    ] = None,
    in_place: Annotated[
        str | None,
        Query(description="the cards Change or Keep it as it is keeps in place; read once"),
    ] = None,
    run: Annotated[
        str | None, Query(description="a planning run to say where it stands; changes nothing")
    ] = None,
) -> Response:
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
    the fold around it open. An undo names the undo it made too, in ``undo_event``, and
    what it restored is said only while that undo is her latest event. ``asked`` names
    the request a help form just made, and
    ``asked_again`` the one a help form sent twice had made; the first is read when both
    are there. Either is said to her in Help, beside that request when it is on the page,
    and at the top of Help when it is not. When the record cannot be read, the page says so
    and offers the same address again. ``earlier`` and ``earlier_said`` name what a choice in
    Earlier homework to check just did, said beside that item only when this process signed
    it for today and that item and today's choices still agree; a word not in the set says
    nothing. The household day is read once, for that check and the page alike. Today says
    that request isn't active only on the page her Undo of Too much right now or a Remove lands
    on, from the mark that press left in its landing's cookie, beside what the signals kept
    now say; no address says it, and a parent is told nothing of it.

    The cards a visit keeps where they were come to the page once. A save or an undo leaves
    them in a cookie named for the landing its redirect names in ``landing``; Change and Keep
    it as it is carry them in ``in_place``, which is answered with the same address, a new
    landing in place of it, and that landing's cookie. Only the page whose address names the
    landing reads the cookie, and it clears it, so another tab's visit leaves it alone, and a
    refresh, a return, or Back to an address asks for the week grouped by its updates. When
    the record cannot be read, the page that says so leaves the cards again for its landing,
    and its Try again carries them in ``in_place``, so a try made after the cookie is gone
    still finds them where they were; after a removal, its Try again lands on the line that
    says what stands, as the removal's redirect does. A save that has just made a card Done
    says so in its cookie, and that one page meets the card with a few words beside a small
    petal, and is sent to be kept by no cache, so a step back or forward through history
    asks again. The page that reads a removed signal's mark is sent the same way.
    """
    landing = landing_asked(request)
    if in_place is not None:
        moved = sent_in_place(once_more(request), InPlace.read(named_once(request, IN_PLACE)))
        return moved if landing is None else forget_landing(moved, landing)
    left = None if landing is None else request.cookies.get(landing_cookie(landing))
    kept = InPlace.read(left)
    removed = left == SIGNAL_REMOVED
    card = met(card_shown(saved, same, undone, change, show, undo_event), left)
    try:
        today = state.clock.today()
        signed = (
            receipt_word(state.result_key, today, earlier, earlier_said)
            if earlier and earlier_said
            else None
        )
        chose = (
            EarlierNote(
                earlier,
                said=None if signed is None else EARLIER_SAID[signed],
                folded={"fold": True, "list": False}.get(earlier_place or ""),
                on_card=earlier_place == "card",
            )
            if earlier and (earlier_said or "").partition(".")[0] in EARLIER_SAID
            else EarlierNote(earlier)
            if earlier and not earlier_said
            else None
        )
        parent = parent_reads(request)
        page = week_page(
            request,
            state,
            kept,
            week=week,
            plan_asked=show_plan == "1",
            was_refreshed=refreshed == "1",
            card=card,
            turning_in=receipt_asked(hand_in_said, about, hand_in_event),
            marker=marker_from(asked, asked_again),
            earlier_note=chose,
            today=today,
            run_notice=run_notice(state, run, PAGE, parent=parent, today=today),
            signal_removed=removed,
        )
    except sqlite3.Error as error:
        again = asked_address(WEEK_PAGE, carrying(request.scope["query_string"], kept))
        if removed:
            again = f"{again}#{TOO_MUCH_STATE}"
        unreadable = week_unreadable(request, error, again=again)
        return unreadable if landing is None else leave_in_place(unreadable, kept, landing)
    if landing is not None and left is not None:
        forget_landing(page, landing)
    if (card is not None and card.well_done) or removed:
        # Not kept by the browser, so Back or Forward to it asks again and meets no petal
        # and no word of a removal.
        page.headers["Cache-Control"] = "no-store"
    return page


def met(card: CardState | None, left: str | None) -> CardState | None:
    """The card a save names, with the words for a Done it has just made when the cookie its
    landing left names that card so. Only the page whose address names the landing reads the
    cookie, once, so a refresh, a return, or Back to the address shows the result alone."""
    if card is None or card.said != UPDATE_SAVED:
        return card
    if newly_done(left) != place_key(card.assignment_id):
        return card
    return replace(card, well_done=WELL_DONE)


def what_undo_restored(assignment: StudentAssignmentView, undo_event: str | None) -> str:
    """What Undo last update restored, read from her history as the page shows it, while the
    undo the address names is her latest event. Otherwise, a later event of hers or an
    address that names no undo, it says only that her update is undone."""
    history = assignment.update_history
    if (
        assignment.updates_unavailable
        or undo_event is None
        or assignment.update_head_id != undo_event
        or not history
        or history[-1].operation != UNDO
    ):
        return UPDATE_UNDONE
    if assignment.update_status is None:
        return UNDONE_TO_NOTHING
    if assignment.update_status != DONE:
        return UNDONE_TO_NOT_YET
    taken_back = history[-2].status if len(history) > 1 else None
    return UNDONE_STILL_DONE if taken_back == DONE else UNDONE_DONE_AGAIN


def said_after_undo(card: CardState | None, assignment: StudentAssignmentView) -> CardState | None:
    """The card an Undo names, saying what it restored once the page has read the card."""
    if card is None or card.said != UPDATE_UNDONE or card.assignment_id != assignment.assignment_id:
        return card
    return replace(card, said=what_undo_restored(assignment, card.undo_event))


def held_from_its_group(
    cards: Sequence[StudentAssignmentView], shown_done: set[str]
) -> list[StudentAssignmentView]:
    """The cards shown away from the group their saved update puts them in: Done among the
    active cards, or anything else in the fold of finished homework."""
    return [
        card for card in cards if (card.assignment_id in shown_done) != (card.update_status == DONE)
    ]


def carrying(query: bytes, kept: InPlace) -> bytes:
    """A query as a request sent it with the cards a visit keeps in place added, so an
    address made from it finds them where they were after the cookie is gone."""
    if not kept.cards:
        return query
    added = URL().include_query_params(**{IN_PLACE: kept.said()}).query.encode()
    return query + b"&" + added if query else added


def once_more(request: Request) -> str:
    """Her week's address as asked, without the cards it keeps in place or the landing it
    named, landing where Change or Keep it as it is lands."""
    asked = request.url.remove_query_params([IN_PLACE, IN_PLACE_LANDING])
    change = request.query_params.get("change")
    show = request.query_params.get("show")
    fragment = update_choice_anchor(change) if change else title_anchor(show) if show else ""
    query = f"?{asked.query}" if asked.query else ""
    return f"{WEEK_PAGE}{query}{f'#{fragment}' if fragment else ''}"


def week_page(
    request: Request,
    state: ApplicationState,
    in_place: InPlace,
    *,
    week: str | None,
    plan_asked: bool,
    was_refreshed: bool,
    card: CardState | None,
    turning_in: ListCard | None,
    marker: HelpMarker | None,
    earlier_note: "EarlierNote | None" = None,
    today: date | None = None,
    run_notice: RunNotice | None = None,
    signal_removed: bool = False,
) -> HTMLResponse:
    """Her week as an address asks for it, with the cards a visit keeps in place, what a
    choice in Earlier homework to check did, what today's panel says about a planning run,
    and whether an Undo just removed a request of hers, for the household day ``today`` the
    caller read. A record that cannot be read raises ``sqlite3.Error``."""
    if week is None:
        return student_page(
            request,
            state,
            refreshed=was_refreshed,
            card=card,
            plan_asked=plan_asked,
            turning_in=turning_in,
            help_marker=marker,
            in_place=in_place,
            earlier_note=earlier_note,
            today=today,
            run_notice=run_notice,
            signal_removed=signal_removed,
        )
    try:
        chosen = date.fromisoformat(week.strip())
    except ValueError:
        return student_page(
            request,
            state,
            problem=NOT_A_WEEK,
            card=card,
            help_marker=marker,
            in_place=in_place,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            today=today,
        )
    if not showable(chosen):
        return student_page(
            request,
            state,
            problem=BEYOND_THE_CALENDAR,
            card=card,
            help_marker=marker,
            in_place=in_place,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            today=today,
        )
    return student_page(
        request,
        state,
        week=chosen,
        card=card,
        plan_asked=plan_asked,
        help_marker=marker,
        in_place=in_place,
        earlier_note=earlier_note,
        today=today,
        run_notice=run_notice,
    )


def week_unreadable(request: Request, error: sqlite3.Error, *, again: str) -> HTMLResponse:
    """Her week when the record cannot be read as the page is made: the failure said once,
    with the way to try again, and today's plan as saved when it was read first. Nothing is
    read here, and the failure is logged by its kind alone."""
    logger.warning("her week could not be read: %s", type(error).__name__)
    parent = parent_reads(request)
    read: tuple[DraftRecord, ...] = getattr(request.state, "plans_read", ())
    return templates.TemplateResponse(
        request,
        "plans_unavailable.html",
        {
            "page": "student",
            "heading": WEEK_UNREADABLE,
            "alert": WEEK_UNREADABLE_WHY,
            "marks": get_application_state(request).settings.page_marks,
            "again": again,
            "parent": parent,
            "todays": True,
            "plans": [
                (
                    record,
                    read_plan(
                        record,
                        reader="family" if parent else "student",
                        link_for=lambda name: details_href(name, return_to="today"),
                        evidence_for=lambda name: details_href(
                            name, fragment=EVIDENCE, return_to="today"
                        ),
                        dates_unread=True,
                    ),
                )
                for record in read
            ],
        },
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def week_named(given: str) -> date | None:
    """The week a form carried, or ``None`` for today's when it carried none or nonsense."""
    try:
        chosen = date.fromisoformat(given.strip()) if given.strip() else None
    except ValueError:
        return None
    return chosen if chosen is not None and showable(chosen) else None


def back_to_the_card(
    week: date | None, said: str, assignment_id: str, undo_event: str | None = None
) -> str:
    """Where a save or an undo sends her: the week she was on, the card, and what happened,
    with the undo an undo made, landing on the card's line that says it. The id is escaped
    where it goes, in the query, and the fragment is the id that line is written with, so one
    that holds a hash, an ampersand, or a question mark is still one value and one place."""
    return address(
        PAGE,
        fragment=result_anchor(assignment_id),
        week=None if week is None else week.isoformat(),
        **{said: assignment_id},
        undo_event=undo_event,
    )


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
    place: Literal["week", "details"]
    """Which page the component is on, her week or an assignment's details. It decides how a
    problem with the update is said: on the details as plain text beside the form, which the
    summary at the top links to; on her week as an alert of its own. On her week the form of
    a card with no update is folded too, and opened for the card a response is about."""
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


def week_context(
    assignment_id: str, week: date, viewer: str, kept: InPlace | None = None
) -> ReportContext:
    """The component on a card of her week: results come back to the card, and Change lands
    on the group of the form it opens. Its save, Undo, Change and Keep it as it is carry the
    cards ``kept`` keeps in place, this one among them."""
    report, undo = report_actions(assignment_id)
    carried = [] if kept is None else [(IN_PLACE, kept.said())]
    return ReportContext(
        viewer=viewer,
        can_update=viewer != "parent",
        report_action=report,
        undo_action=undo,
        change_action=address(WEEK_PAGE, fragment=update_choice_anchor(assignment_id)),
        change_fields=[("week", week.isoformat()), ("change", assignment_id), *carried],
        cancel_href=week_href(week, assignment_id, show=assignment_id, **dict(carried)),
        post_fields=[("week", week.isoformat()), *carried],
        place="week",
    )


def detail_context(
    assignment_id: str, back: ReturnTo, viewer: str, link: "ReturnLink"
) -> ReportContext:
    """The component on an assignment's details: results come back to the details, with the
    way back from there carried along.

    Change is a GET form, and a browser writes a GET form's fields over any
    query in its action, so the way back rides in the form's hidden fields
    beside the flag that opens the editor, and the action is the address of
    the details with no query. Change and Keep it as it is both land on the
    heading over her update, below the school's instructions."""
    report, undo = report_actions(assignment_id)
    carried = back.fields()
    return ReportContext(
        viewer=viewer,
        can_update=viewer != "parent",
        report_action=report,
        undo_action=undo,
        change_action=details_href(assignment_id, fragment=UPDATE_OR_TURN_IN),
        change_fields=[
            *[(name, value) for name, value in carried.items() if value],
            ("change", "1"),
        ],
        cancel_href=details_href(assignment_id, fragment=UPDATE_OR_TURN_IN, **carried),
        post_fields=[("report_view", "detail"), *carried.items()],
        place="details",
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
    state: ApplicationState,
    back: ReturnTo,
    assignment_id: str,
    *,
    today: date,
    in_place: InPlace | None = None,
) -> ReturnLink:
    """The link an assignment's details offer back to where their reader came from.

    Her week, with the card in view and its fold open. Today's plan, by the
    place on her week that holds it and never by the plan that was followed: the
    link is followed later than it is written, and another plan may have taken
    that one's place by then, or the day may have moved on, and the place is
    there either way. Today itself, with a word, when no plan is left as the
    link is written. The family page at this assignment's row, or at the plan
    that was being read when it is still on the pages; a plan that is not sends
    the reader to the family page and nothing more. A link to her week carries the cards
    ``in_place`` names.
    """
    if back.target == "week":
        return ReturnLink(
            week_href(back.week, assignment_id, show=assignment_id, in_place=carried(in_place)),
            "Back to the week",
        )
    if back.target == "to_turn_in":
        return ReturnLink(address(TO_TURN_IN_PAGE, fragment=TO_TURN_IN), "Back to To turn in")
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
    in_place: InPlace | None = None,
) -> HTMLResponse:
    """The small page for an assignment that is not on record: said plainly, 404, with a
    safe way back and, when a form brought her here, what she chose and wrote, so it can
    be copied. No form, and nothing is put back on record. A way back to her week carries
    the cards ``in_place`` names.

    The explanation takes the focus when a form's press brought her here,
    whatever the form carried, and never on a look by link, whatever the
    address carries. When the plan the way back looks up can't be read, the
    way back is made from the address alone and nothing more is read."""
    try:
        link = way_back(
            state,
            back,
            assignment_id,
            today=state.clock.today() if today is None else today,
            in_place=in_place,
        )
    except sqlite3.Error as error:
        logger.warning("a way back could not be read: %s", type(error).__name__)
        link = plain_way_back(back, assignment_id)
    return templates.TemplateResponse(
        request,
        "student_assignment_gone.html",
        {
            "problem": GONE,
            "pressed": request.method == "POST",
            "card": card,
            "hand_in_card": hand_in,
            "back": link,
            "marks": state.settings.page_marks,
        },
        status_code=status.HTTP_404_NOT_FOUND,
    )


@dataclass(frozen=True)
class WithdrawnClaim:
    """A claim about the date that was withdrawn, as the details list it: the claim as she
    reads one, the household day it was withdrawn, and whether a homework note made it.
    Nothing a plan is made from reads these."""

    said: str
    on: str | None
    from_a_note: bool


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
    card, hand_in = as_read_by(card, viewer), as_read_by(hand_in, viewer)
    today = state.clock.today()
    on_record = state.project_state
    history_unavailable = False
    with on_record.reading():
        item = on_record.one_assignment(assignment_id)
        # The claims that count, read around any that cannot be read, which the page says.
        claimed = None if item is None else on_record.read_claims([assignment_id])
        records = [] if claimed is None else claimed.records.get(assignment_id, [])
        claims_unavailable = claimed is not None and assignment_id in claimed.unreadable
        found = None if item is None else statuses_for(on_record, [assignment_id])
        turned_in = None if item is None else on_record.hand_in_readings([assignment_id])
        # Whether she chose it for today's plan, which a Not yet on earlier work says.
        chosen = item is not None and assignment_id in on_record.catch_up_choices().get(
            today, frozenset()
        )
        # The homework notes this assignment was added from or joined by: her words, kept
        # as evidence beside the record and copied into none of it.
        notes = None if item is None else on_record.captures_of_assignment(assignment_id)
        # The school's instructions, kept apart from anyone's own note.
        instructions = on_record.school_instruction_readings([assignment_id])
        # Every claim ever made about the date, the withdrawn ones included: reconciliation
        # above reads only the ones that count, and the details say the rest apart. A history
        # that cannot be read leaves the record, her update, and her hand-in as they are;
        # the page says the history is unavailable, and never that there is none.
        try:
            history = [] if item is None else on_record.claim_history(assignment_id)
        except UnreadableClaim:
            history = []
            history_unavailable = True
    if item is None or found is None or turned_in is None or notes is None:
        return gone_page(
            request, state, back, assignment_id, card=card, hand_in=hand_in, today=today
        )
    turning_in = HandInView.of(turned_in.readable.get(assignment_id))
    noticed = notice_due_date(expect_due_date(item), records)
    view = assignment_view(
        item,
        records,
        noticed,
        found[assignment_id],
        in_planning_window=in_week(item, noticed, today),
        outside_window=planning_window(today).outside(item, noticed),
        hand_in=turning_in,
        claims_unreadable=claims_unavailable,
        instructions=instructions.readable.get(assignment_id),
        instructions_unreadable=assignment_id in instructions.unreadable,
    )
    card = said_after_undo(card, view)
    link = way_back(state, back, assignment_id, today=today)
    # Which of the school's instructions apply is the family's to choose: a parent's, or the
    # household's with the sign-in off when it came from the family's pages. She reads them.
    family_chooses = viewer == "parent" or (viewer == "anyone" and back.target == "family")
    return templates.TemplateResponse(
        request,
        "student_assignment.html",
        {
            "assignment": view,
            "from_notes": [
                (note, derived_assignment_id(note.capture_id) == assignment_id)
                for note in notes.notes
            ],
            "from_notes_unreadable": len(notes.unreadable),
            "claim_history_unavailable": history_unavailable,
            "withdrawn_claims": [
                WithdrawnClaim(
                    said=claim.record.spoken(),
                    on=(
                        None
                        if claim.withdrawn_at is None
                        else long_date(claim.withdrawn_at.astimezone(state.clock.zone).date())
                    ),
                    from_a_note=claim.capture_id is not None,
                )
                for claim in history
                if not claim.active
            ],
            "ctx": detail_context(assignment_id, back, viewer, link),
            # Help is hers to ask for on her week, and a parent's to answer on the family page.
            "help_href": (
                FAMILY_HELP if viewer == "parent" else address(WEEK_PAGE, fragment=ASK_FOR_HELP)
            ),
            "planning_window": planning_window(today).said(),
            "planning_window_end": planning_window(today).end,
            "earlier_chosen": chosen,
            "earlier_link": address(
                WEEK_PAGE, fragment=earlier_anchor(assignment_id), earlier=assignment_id
            ),
            "instructions_review": (
                instructions_review_href(assignment_id) if family_chooses else None
            ),
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
            "marks": state.settings.page_marks,
        },
        status_code=status_code,
    )


@router.get(
    "/assignments/{assignment_id:path}", response_class=HTMLResponse, include_in_schema=False
)
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
    undo_event: Annotated[
        str | None, Query(description="the undo that press made; compared, never trusted")
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
    only carries: any other word says nothing. An undo names the undo it made
    in ``undo_event``, as on her week. When the record can't be read,
    503: a page that says so, offers the same address again and the way back
    the address names, and says nothing of a save. A save that has just made
    the assignment Done names a landing whose cookie says so; the page that
    reads it, once, meets the update with a few words beside a small petal.
    """
    back, _ = read_return(
        {"return_to": return_to or "", "week": week or "", "plan_id": plan_id or ""},
        viewer=viewer_of(request),
        showable=showable,
    )
    landing = landing_asked(request)
    left = None if landing is None else request.cookies.get(done_cookie(landing))
    card = None
    if said in CONFIRMATIONS:
        named = undo_event if said == "undone" else None
        card = met(CardState(assignment_id, said=CONFIRMATIONS[said], undo_event=named), left)
    elif change == "1":
        card = CardState(assignment_id, change=True)
    turning_in = None
    if hand_in in HAND_IN_CONFIRMATIONS:
        turning_in = HandInCard(said=HAND_IN_CONFIRMATIONS[hand_in])
    elif hand_in in ("change", "remember"):
        # A link that says ``remember`` opens the same form, so a saved address still lands.
        turning_in = HandInCard(change=True)
    try:
        page = detail_page(request, state, assignment_id, back, card=card, hand_in=turning_in)
    except sqlite3.Error as error:
        failed = unavailable_page(
            request,
            state,
            error,
            heading="Assignment",
            alert=DETAILS_UNAVAILABLE,
            again=asked_address(details_href(assignment_id), request.scope["query_string"]),
            ways_back=[plain_way_back(back, assignment_id)],
        )
        return failed if left is None or landing is None else forget_done(failed, landing)
    if left is not None and landing is not None:
        forget_done(page, landing)
    if card is not None and card.well_done:
        # Not kept by the browser, so Back or Forward to it asks again and meets no petal.
        page.headers["Cache-Control"] = "no-store"
    return page


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
    in_place: InPlace | None = None,
    status_code: int,
) -> HTMLResponse:
    """The page a form's result is shown on: the details it came from, or her week, with
    the cards her visit keeps in place."""
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
        request,
        state,
        week=origin.week,
        card=card,
        problem=problem,
        pressed=True,
        in_place=in_place,
        status_code=status_code,
    )


def after(origin: Origin, said: str, assignment_id: str, undo_event: str | None = None) -> str:
    """Where a save or an undo sends her once it is committed: back to the page the form
    was on, with what happened and, for an undo, the undo it made. The address lands on
    the result itself, on the details with the way back repeated beside it, so neither is
    below a long page's first screen."""
    if origin.detail:
        return details_href(
            assignment_id,
            fragment=result_anchor(assignment_id),
            said=said,
            undo_event=undo_event,
            **origin.back.fields(),
        )
    return back_to_the_card(origin.week, said, assignment_id, undo_event)


def answered(
    origin: Origin,
    said: str,
    assignment_id: str,
    kept: InPlace,
    *,
    made_done: bool = False,
    undo_event: str | None = None,
) -> Response:
    """The redirect a committed save or undo is answered with. From her week it leaves the
    cards the form kept in place for the page it lands on, the saved card where it was; from
    her week or the details it says, to the one page that answers it, when the save has just
    made that card Done. An undo's address names the undo it made."""
    location = after(origin, said, assignment_id, undo_event)
    if not origin.detail:
        done = assignment_id if made_done else None
        return sent_in_place(location, kept, done)
    if made_done:
        return sent_done_to_the_details(location, assignment_id)
    return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)


def plain_ways_back(
    origin: Origin, assignment_id: str, in_place: InPlace | None = None
) -> list[ReturnLink]:
    """The ways back a failure page offers, made from checked values and nothing else.

    No store is read, since this is the page for when the record cannot be:
    a form from the details gets the assignment's details again, with the
    way back they carried, and the page that way back names; a card gets
    its week with the card in view and the cards ``in_place`` names.
    """
    if not origin.detail:
        return [
            ReturnLink(
                week_href(
                    origin.week, assignment_id, show=assignment_id, in_place=carried(in_place)
                ),
                "Back to the week",
            )
        ]
    details = ReturnLink(
        details_href(assignment_id, **origin.back.fields()), "Back to the assignment"
    )
    return [details, plain_way_back(origin.back, assignment_id)]


def plain_way_back(back: ReturnTo, assignment_id: str) -> ReturnLink:
    """The page a way back names, from its checked values alone, with no store read. Today
    is the Today panel, whichever plan is there when she arrives, and a plan on the family
    page is named to the family page, which opens it or not as it finds it."""
    if back.target == "week":
        return ReturnLink(
            week_href(back.week, assignment_id, show=assignment_id), "Back to the week"
        )
    if back.target == "to_turn_in":
        return ReturnLink(address(TO_TURN_IN_PAGE, fragment=TO_TURN_IN), "Back to To turn in")
    if back.target == "today":
        return ReturnLink(address(WEEK_PAGE, fragment="today"), "Back to Today")
    if back.plan_id is None:
        where = address(
            FAMILY_PAGE, fragment=f"update-{segment(assignment_id)}", focus=assignment_id
        )
    else:
        where = address(FAMILY_PAGE, fragment=anchor_for(back.plan_id), plan=back.plan_id)
    return ReturnLink(where, "Back to family review")


@dataclass(frozen=True)
class NotShown:
    """What a press's answer says when the page it is shown on can't be read: a heading that
    names the press, the press's own words as they read without that page, the line that
    says which page can't be shown, the ways back, and what she chose and typed. ``opening``
    is a plan press's answer as its first sentence, which takes the focus, and the rest."""

    heading: str
    said: str
    line: str
    ways_back: list[ReturnLink]
    card: CardState | None = None
    hand_in: HandInCard | None = None
    opening: tuple[str, str] | None = None


def shown_once(
    request: Request,
    state: ApplicationState,
    page: Callable[[], HTMLResponse],
    fallback: NotShown,
    status_code: int,
) -> HTMLResponse:
    """The page a press is answered on, tried once. When a read for it fails, the page that
    reads no store, with the press's own status: made from what the request already held,
    so no store is called after the failure and nothing is tried again. The failure is
    logged by its kind alone; any other failure is not caught here."""
    try:
        return page()
    except sqlite3.Error as error:
        logger.warning("the page for a press could not be read: %s", type(error).__name__)
        return not_shown(request, state, fallback, status_code)


def not_shown(
    request: Request, state: ApplicationState, fallback: NotShown, status_code: int
) -> HTMLResponse:
    """The page that reads no store, for a press whose own page can't be read."""
    return templates.TemplateResponse(
        request,
        "student_update_recovery.html",
        {
            "heading": fallback.heading,
            "card": None
            if fallback.card is None
            else replace(fallback.card, problem=fallback.said),
            "hand_in_card": (
                None
                if fallback.hand_in is None
                else replace(fallback.hand_in, problem=fallback.said)
            ),
            "note_problem": fallback.said,
            "page_line": fallback.line,
            "opening": fallback.opening,
            "ways_back": fallback.ways_back,
            "marks": state.settings.page_marks,
        },
        status_code=status_code,
    )


def week_not_shown(request: Request, heading: str, problem: str, *, fragment: str = "") -> NotShown:
    """What a press on her week with no card of its own says when her week can't be read: its
    words, the line for her week in the reader's words, and the way back to it, at
    ``fragment`` when the press was made there."""
    parent = parent_reads(request)
    week = "Back to her week" if parent else "Back to my week"
    return NotShown(
        heading,
        WITHOUT_THE_PAGE.get(problem, problem),
        HER_WEEK_NOT_SHOWN if parent else YOUR_WEEK_NOT_SHOWN,
        [ReturnLink(address(WEEK_PAGE, fragment=fragment), week)],
    )


def card_not_shown(
    request: Request, origin: Origin, assignment_id: str, card: CardState, in_place: InPlace
) -> NotShown:
    """What a refused update says when the page its form came from can't be read: the
    details, or her week with the cards ``in_place`` names."""
    said = card.problem or ""
    if origin.detail:
        line = ASSIGNMENT_NOT_SHOWN
    else:
        line = HER_WEEK_NOT_SHOWN if parent_reads(request) else YOUR_WEEK_NOT_SHOWN
    return NotShown(
        UPDATE_NOT_SAVED,
        WITHOUT_THE_PAGE.get(said, said),
        line,
        plain_ways_back(origin, assignment_id, in_place),
        card=card,
    )


def not_hers(
    request: Request, state: ApplicationState, heading: str, problem: str, *, in_help: bool = False
) -> HTMLResponse:
    """A parent's press on her week that only she may make: 403 on her current week, decided
    from the sign-in and the route alone, the refusal at the top or, ``in_help``, in Help.
    When her week can't be read, the page that reads no store keeps the 403."""
    return shown_once(
        request,
        state,
        lambda: student_page(
            request,
            state,
            problem=None if in_help else problem,
            help_problem=HelpProblem(problem) if in_help else None,
            pressed=True,
            status_code=status.HTTP_403_FORBIDDEN,
        ),
        week_not_shown(request, heading, problem, fragment="help" if in_help else ""),
        status.HTTP_403_FORBIDDEN,
    )


def unavailable_page(
    request: Request,
    state: ApplicationState,
    error: sqlite3.Error,
    *,
    heading: str,
    alert: str,
    again: str,
    ways_back: list[ReturnLink],
    family: bool = False,
) -> HTMLResponse:
    """A page that is only read, when a read for it fails: 503, what can't be shown, the same
    address to ask for again, and the ways back; ``family`` for a page in the family's tree.
    Nothing is read here, and the failure is logged by its kind alone."""
    logger.warning("%s could not be read: %s", heading, type(error).__name__)
    return templates.TemplateResponse(
        request,
        "page_unavailable.html",
        {
            "heading": heading,
            "alert": alert,
            "again": again,
            "ways_back": ways_back,
            "family": family,
            "marks": state.settings.page_marks,
        },
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def could_not(
    request: Request,
    state: ApplicationState,
    assignment_id: str,
    origin: Origin,
    card: CardState,
    in_place: InPlace | None = None,
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
            in_place=in_place,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    except Exception:
        logger.exception("her page could not be read back after a failed save")
        recovery = templates.TemplateResponse(
            request,
            "student_update_recovery.html",
            {
                "card": card,
                "ways_back": plain_ways_back(origin, assignment_id, in_place),
                "marks": state.settings.page_marks,
            },
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
        return recovery


@router.post(
    "/actions/assignments/{assignment_id:path}/report",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def report_from_the_page(request: Request, assignment_id: str, state: State) -> Response:
    """Her update on one assignment: Done or Not yet, with a note if she wants one.

    A parent signed in is told the update is hers to make, 403, on her
    current week, from the sign-in and the route alone: before the form is
    read, before the assignment is looked up, and without the decision lock,
    whatever page the form came from. Nothing is written.

    For anyone else the form is read whole first: its fields, each once, and
    nothing more. It carries the last event the page showed, which must be
    one of this assignment's, so a save lands on the chain the page showed
    or is shown what changed: the same update as the one standing is already
    saved, with no write and no new day; a page whose head has moved on is
    answered 409 with the newer update above her form and her typed words
    kept; anything else is appended. The comparison and the write are one
    operation under the decision lock, so two devices saving together get
    one save and one refusal. A write the file refuses is answered with the
    component and her words, and never with a word of a save. An assignment
    not on record now is answered 404 with her choice and words to copy.

    One route serves the card on her week and the assignment's details. The
    form says which it came from, and that decides only where the result is
    shown: a save from the details comes back to the details, every refusal
    is shown there with what she typed, and an assignment taken off the
    record meanwhile is said on a small page with her words to copy. A form
    that names a page to go back to that these pages do not make is refused,
    422, with her input kept. A refusal whose page can't be read is said on the
    page that reads no store, with its status and her input.
    """
    if viewer_of(request) == "parent":
        return not_hers(request, state, UPDATE_NOT_SAVED, NOT_HERS_TO_UPDATE)
    fields, whole = await fields_of(
        request, REPORT_FIELDS, may_be_absent=NOTHING_CHOSEN | FROM_A_CARD
    )
    origin = origin_of(request, fields)
    # The details are not her week, so a press there keeps no card in place.
    kept = InPlace() if origin.detail else InPlace.read(fields.get(IN_PLACE))
    said = fields.get("status", "").strip()
    note = fields.get("note", "")
    words = normalize_note(note)
    token = fields.get("expected_report_id", "").strip()
    chosen = said if said in (DONE, NOT_YET) and whole else None

    def refused(
        problem: str, code: int, *, field: str | None = None, saved_elsewhere: bool = False
    ) -> Response:
        card = CardState(
            assignment_id,
            change=True,
            problem=problem,
            field=field,
            status=chosen,
            note=note,
            saved_elsewhere=saved_elsewhere,
        )
        return shown_once(
            request,
            state,
            lambda: result_page(
                request, state, assignment_id, origin, card=card, in_place=kept, status_code=code
            ),
            card_not_shown(request, origin, assignment_id, card, kept),
            code,
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
        # The card is gone, so her choice and words go to the page that keeps them to
        # copy, with the way back to where she was and the other cards still in place.
        return gone_page(
            request,
            state,
            origin.back if origin.detail else ReturnTo("week", origin.week),
            assignment_id,
            card=CardState(assignment_id, status=chosen, note=note),
            in_place=kept,
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
            kept,
        )
    match result:
        case Saved(before=before):
            new = said == DONE and before != DONE
            return answered(origin, "saved", assignment_id, kept, made_done=new)
        case AlreadySaved():
            return answered(origin, "same", assignment_id, kept)
        case Conflict():
            return refused(SAVED_ELSEWHERE, status.HTTP_409_CONFLICT, saved_elsewhere=True)


@router.post(
    "/actions/assignments/{assignment_id:path}/undo-report",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def undo_report_from_the_page(request: Request, assignment_id: str, state: State) -> Response:
    """Take her latest update back, restoring what stood before it.

    A parent is answered 403 on her current week before the form is read or
    the assignment looked up, and nothing is written. For anyone else the
    form is read whole, its fields each once. The button names the update it
    takes back, which must be one of this assignment's; one that is not the
    latest, or is itself an undo, meets a 409 that says the update has
    changed, with the component as it stands, since what she meant to take
    back is not what is there. A write the file refuses is said as that, and
    never as an undo. The result is shown where the form was, her week or
    the assignment's details, at an address that names the undo made, and an
    undo never sends her to another week. A refusal whose page can't be read
    is said on the page that reads no store.
    """
    if viewer_of(request) == "parent":
        return not_hers(request, state, UPDATE_NOT_SAVED, NOT_HERS_TO_UPDATE)
    fields, whole = await fields_of(request, UNDO_FIELDS, may_be_absent=FROM_A_CARD)
    origin = origin_of(request, fields)
    # The details are not her week, so a press there keeps no card in place.
    kept = InPlace() if origin.detail else InPlace.read(fields.get(IN_PLACE))
    named = fields.get("report_id", "").strip()

    def refused(problem: str, code: int, *, saved_elsewhere: bool = False) -> Response:
        card = CardState(assignment_id, problem=problem, saved_elsewhere=saved_elsewhere)
        return shown_once(
            request,
            state,
            lambda: result_page(
                request, state, assignment_id, origin, card=card, in_place=kept, status_code=code
            ),
            card_not_shown(request, origin, assignment_id, card, kept),
            code,
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
        return shown_once(
            request,
            state,
            lambda: student_page(
                request,
                state,
                week=origin.week,
                problem=NOT_ON_RECORD,
                pressed=True,
                in_place=kept,
                status_code=status.HTTP_404_NOT_FOUND,
            ),
            card_not_shown(
                request,
                origin,
                assignment_id,
                CardState(assignment_id, problem=NOT_ON_RECORD),
                kept,
            ),
            status.HTTP_404_NOT_FOUND,
        )
    except UnknownReport:
        return refused(NOT_THIS_CARDS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    except CouldNotSave:
        logger.exception("her update on %s could not be undone", assignment_id)
        return could_not(
            request,
            state,
            assignment_id,
            origin,
            CardState(assignment_id, problem=NOT_UNDONE),
            kept,
        )
    match result:
        case Undone(report=made):
            return answered(origin, "undone", assignment_id, kept, undo_event=made.report_id)
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
    goes to the process log, and the page says Blossom couldn't finish a
    reliable plan, as the JSON route does. When her week can't be read either,
    the page that reads no store says so, with the same status.

    A press stays in the visit: the cards the visit keeps in place, which the form carries,
    stay where they were on the page that answers it, a plan made or not. A form with a
    field sent twice or one no page writes keeps none.

    Each form plans once. Its id names its run, and a form whose run is recorded starts
    nothing and is answered by that run: being made, its plan, a newer plan, or why it
    ended. A form that is missing a field, or holds one of another shape, is from another
    evening, or was issued seven days ago or more, starts nothing, and neither does one
    whose page didn't know the evening's newest plan, which is then shown.
    """
    fields, whole = await fields_of(request, PLAN_FIELDS, may_be_absent=PLAN_FIELDS)
    budget = graphs.budget()
    # The first cards the form carried stay in place, whole form or not, as ``fields_of``
    # hands them back for.
    kept = InPlace.read(fields.get(IN_PLACE))
    parent = parent_reads(request)
    today = state.clock.today()
    now = state.real_clock.now()

    async def not_made(answer: PlanAnswer, failure: PlanFailure | None = None) -> HTMLResponse:
        # Her page is read on a worker thread, for at most the store's wait; past it,
        # the page that reads no store says the answer's own words for it.
        said = " ".join(answer.elsewhere)
        shown = week_not_shown(request, PLAN_NOT_MADE, said)
        fallback = replace(shown, said=said, opening=answer.stand_in(shown.line))
        failure = (
            PlanFailure(try_again=False, uncertain=True)
            if failure is None and not answer.offers_form
            else failure
        )

        def page() -> HTMLResponse:
            return shown_once(
                request,
                state,
                lambda: student_page(
                    request,
                    state,
                    plan_answer=answer,
                    pressed=True,
                    plan_failure=failure,
                    in_place=kept,
                    status_code=answer.status,
                ),
                fallback,
                answer.status,
            )

        try:
            return await bounded(page, STORE_WAIT_SECONDS)
        except Unfinished:
            return not_shown(request, state, fallback, answer.status)

    def before(code: int, said: Sentences, elsewhere: Sentences | None = None) -> PlanAnswer:
        # An answer the button gave before each form planned once, said as it was.
        return PlanAnswer("her-before", code, fact(*said), said if elsewhere is None else elsewhere)

    async def ended_here(
        row: PlanRow, outcome: str, past_due: Sequence[PastDueView]
    ) -> HTMLResponse:
        # A run that ended without a plan, said alike on its first press and on its form
        # pressed again. Her week names each past-due assignment once, in its link to its
        # dates, so its line names none; the stand-in has no links, so its words name them.
        return await not_made(
            PlanAnswer(
                row,
                status.HTTP_409_CONFLICT,
                fact(*ended_sentences(outcome, parent=parent)),
                ended_sentences(outcome, parent=parent, past_due=past_due, where=False),
            ),
            None
            if outcome == NOTHING_TO_SCHEDULE_OUTCOME
            else PlanFailure(checks=date_checks(past_due), try_again=outcome != DATE_PROBLEM),
        )

    async def repeated(run: RunState) -> Response:
        # A form whose run is recorded is answered by that run as it stands.
        if run.plan_date != today:
            return await not_made(another_evening(run.plan_date))
        if run.status == "running":
            notice = being_made(run, PAGE)
            return await not_made(
                PlanAnswer(
                    "her-running",
                    status.HTTP_202_ACCEPTED,
                    fact(notice.said),
                    (notice.said,),
                    offers_form=False,
                ),
                PlanFailure(run=notice.check, try_again=False, uncertain=True),
            )
        try:
            replaced = await bounded(partial(run_replaced, state, run), STORE_WAIT_SECONDS)
        except STORE_FAILURES as error:
            # A published run made a plan, whatever came after it; nothing says it is the
            # newest.
            logger.warning("the plans after run %s could not be read: %s", run.run_id, error)
            return await not_made(PLAN_MADE_ALREADY)
        if replaced:
            return await not_made(NEWER_PLAN)
        if run.status == "published":
            return landed("her-plan-latest", sent_in_place(f"{PAGE}?show_plan=1", kept))
        if run.reason == DATE_PROBLEM and not run.plan_unchanged:
            # A plan for today was published since the run was admitted, so the run's own
            # opening, that Blossom can't make today's plan, isn't true of the page: the newer
            # plan is said, and shown.
            return await not_made(NEWER_PLAN)
        return await ended_here("her-ended", run.reason, past_due_views(run.past_due or ()))

    form = plan_form_from(fields, now) if whole else None
    if form is None:
        return await not_made(FORM_NOT_WHOLE)
    found = await run_of_the_form(state, form.run_id)
    if found is not None:
        return await repeated(found)
    if form.evening != today:
        return await not_made(another_evening(form.evening))
    if form.expired(now):
        return await not_made(FORM_EXPIRED)
    try:
        await require_work(state, today, budget)
        require_model(graphs)
        made = await make_plan(
            graph_for_a_run(graphs),
            today,
            state,
            budget=budget,
            run_id=form.run_id,
            basis=form.newest_plan,
        )
    except SameRun as same:
        return await repeated(same.run)
    except StaleBasis:
        return await not_made(NEWER_PLAN)
    except UnknownBasis:
        return await not_made(FORM_NOT_WHOLE)
    except AlreadyPlanning as error:
        return await not_made(
            before(error.status_code, planning_sentences(error.run, parent=parent)),
            PlanFailure(run=run_check(PAGE, error.run.run_id, CHECK_ON_THAT_REQUEST)),
        )
    except NotSaved as error:
        return await not_made(
            before(error.status_code, not_saved_sentences(parent=parent, kept=error.kept)),
            PlanFailure(),
        )
    except CouldNotStart as error:
        return await not_made(
            before(error.status_code, (*COULD_NOT_START_SENTENCES, saved_sentence(parent=parent))),
            PlanFailure(),
        )
    except Unconfirmed as unconfirmed:
        said = (UNCONFIRMED, saved_sentence(parent=parent))
        return await not_made(
            PlanAnswer(
                "her-unconfirmed", status.HTTP_202_ACCEPTED, fact(*said), said, offers_form=False
            ),
            PlanFailure(
                run=run_check(PAGE, unconfirmed.run_id, CHECK_AGAIN),
                try_again=False,
                uncertain=True,
            ),
        )
    except HTTPException as error:
        return await not_made(
            before(error.status_code, (f"Blossom could not make a plan: {error.detail}",))
        )
    except Exception:
        logger.exception("today's plan failed on the way")
        return await not_made(
            before(
                status.HTTP_409_CONFLICT,
                ended_sentences(INTERRUPTED, parent=parent),
                ended_sentences(INTERRUPTED, parent=parent, where=False),
            ),
            PlanFailure(),
        )
    run = made.view
    if run.outcome == OVERTAKEN:
        # A newer plan for today reached the page while this one was being made; the answer
        # says so and shows that plan, never landing as if this press made it.
        return await not_made(NEWER_PLAN)
    if run.draft_id is None:
        return await ended_here("her-before", run.outcome, run.past_due)
    return landed("her-made", sent_in_place(f"{PAGE}?show_plan=1", kept))


@router.post("/actions/ask-for-help", response_class=HTMLResponse, include_in_schema=False)
async def ask_for_help_from_the_page(request: Request, state: State) -> Response:
    """The other press on her page. A blank note is no note; a long one is said, not cut.

    A parent is answered 403 before the form is read, the refusal said in Help. Her form
    is read whole, her words and the id the page gave the form, each once, before
    anything is looked up; a form that is not whole sends nothing and comes back with
    her first words in a fresh form. A new request lands on its own row, and the same
    form sent again is the request it made, said beside it, and never a second; a form
    that already asked with other words, or whose request was taken back or has gone,
    sends nothing and keeps her words.
    """
    if viewer_of(request) == "parent":
        return not_hers(request, state, REQUEST_NOT_SENT, NOT_HERS_TO_ASK, in_help=True)
    fields, whole = await fields_of(request, ASK_FIELDS)
    words = fields.get("note", "")
    try:
        form = request_id_from(fields.get("request_id", ""))
    except NotARequestId:
        form = None
    if not whole or form is None:
        return help_again(
            request,
            state,
            HelpForm(words, HELP_FORM_NOT_WHOLE),
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    note = words.strip()
    if len(note) > NOTE_MAX_LENGTH:
        return help_again(
            request,
            state,
            HelpForm(
                words,
                f"A note is at most {NOTE_MAX_LENGTH} characters; this one is {len(note)}.",
                at_words=True,
            ),
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    try:
        outcome = state.help_requests.ask_once(form, state.clock.today(), note or None)
    except Exception:
        logger.exception("her request for help could not be sent")
        return help_again(
            request,
            state,
            HelpForm(words, HELP_NOT_SENT),
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    match outcome:
        case HelpAsked(request=asked):
            return RedirectResponse(
                f"{PAGE}?asked={asked.request_id}#help-result",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        case HelpAlreadyAsked(request=asked):
            return RedirectResponse(
                f"{PAGE}?asked_again={asked.request_id}#help-result",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        case HelpFormChanged():
            problem = FORM_SENT_OTHER_WORDS
        case HelpFormUsed():
            problem = FORM_USED
    return help_again(request, state, HelpForm(words, problem), status.HTTP_409_CONFLICT)


def help_again(
    request: Request, state: ApplicationState, form: HelpForm, status_code: int
) -> HTMLResponse:
    """Her page with her Ask for help form shown again, her words in a fresh form and what
    went wrong beside them. When her page can't be made, the page that reads no store."""
    try:
        return student_page(request, state, help_form=form, status_code=status_code)
    except Exception:
        logger.exception("her page could not be read back after a request for help")
        return templates.TemplateResponse(
            request,
            "student_update_recovery.html",
            {
                "card": None,
                "hand_in_card": None,
                "heading": "Request not sent",
                "note_problem": form.problem,
                "help_question": form.words,
                "ways_back": [ReturnLink(PAGE, "Back to the week")],
                "marks": state.settings.page_marks,
            },
            status_code=status_code,
        )


@router.post(
    "/actions/take-back-help/{request_id}", response_class=HTMLResponse, include_in_schema=False
)
def take_back_help_from_the_page(request: Request, request_id: str, state: State) -> Response:
    """Remove a request from her page while nobody has taken it up, and return to Help.
    Otherwise Help says why, in words chosen by where the request stands and never by the
    store's message, with a way to the request when its row is on the page. A take-back the
    file refuses is rolled back and said the same way, 500, and so is one on a request that
    can't be read, which stays as it is. A parent is answered 403 before
    anything is read: the request is hers to take back. When her week can't be read, the
    page that reads no store says the same, with the same status."""
    if viewer_of(request) == "parent":
        return not_hers(request, state, REQUEST_NOT_TAKEN_BACK, NOT_HERS_TO_ASK, in_help=True)

    def refused(problem: HelpProblem, code: int) -> HTMLResponse:
        return shown_once(
            request,
            state,
            lambda: student_page(request, state, help_problem=problem, status_code=code),
            week_not_shown(request, REQUEST_NOT_TAKEN_BACK, problem.said, fragment="help"),
            code,
        )

    try:
        removed = state.help_requests.take_back(request_id)
    except RequestClosed as error:
        said = ALREADY_RESPONDING if error.request.state == "accepted" else ALREADY_CLOSED
        return refused(HelpProblem(said, error.request.request_id), status.HTTP_409_CONFLICT)
    except UnreadableHelpRequest as error:
        logger.warning("her request could not be taken back: %s", type(error).__name__)
        return refused(HelpProblem(REQUEST_UNREADABLE), status.HTTP_500_INTERNAL_SERVER_ERROR)
    except sqlite3.Error as error:
        logger.warning("her request could not be taken back: %s", type(error).__name__)
        return refused(
            HelpProblem(TAKE_BACK_NOT_SAVED, request_id), status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    if not removed:
        return refused(HelpProblem(NOT_HERE_ANY_MORE), status.HTTP_404_NOT_FOUND)
    return RedirectResponse(f"{PAGE}#help", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/actions/too-much", response_class=HTMLResponse, include_in_schema=False)
async def too_much_from_the_page(request: Request, state: State) -> Response:
    """The one control on her page. Records the signal and returns to the page, which shows it.
    A parent is answered 403 before anything is read or kept: the signal is hers to give. A
    signal the file refuses is rolled back and said at the top of her week, 500."""
    if viewer_of(request) == "parent":
        return not_hers(request, state, NOT_SAVED_HEADING, NOT_HERS_TO_SIGNAL)
    try:
        await record_signal(state, None)
    except sqlite3.Error as error:
        logger.warning("her signal could not be kept: %s", type(error).__name__)
        return signal_not_saved(request, state)
    return RedirectResponse(PAGE, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/actions/take-back/{signal_id}", response_class=HTMLResponse, include_in_schema=False)
async def take_back_from_the_page(request: Request, signal_id: str, state: State) -> Response:
    """Remove a signal from her page, by Undo 'Too much right now' or a Remove, and return to
    her week, on the line that says what stands, where only the page the redirect lands on says
    that request isn't active, beside what still stands. A signal already gone is not an error
    here, and is answered the same way. A parent is answered 403 before the signal is looked
    up: the signal is hers to take back. A removal the file refuses is rolled back and said at
    the top of her week, 500."""
    if viewer_of(request) == "parent":
        return not_hers(request, state, NOT_SAVED_HEADING, NOT_HERS_TO_SIGNAL)
    try:
        await withdraw_signal(state, signal_id)
    except sqlite3.Error as error:
        logger.warning("her signal could not be taken back: %s", type(error).__name__)
        return signal_not_saved(request, state)
    return sent_removed(address(PAGE, fragment=TOO_MUCH_STATE))


def signal_not_saved(request: Request, state: ApplicationState) -> HTMLResponse:
    """Her week after a signal the file refused, said at the top, 500; the page that reads
    no store when her week can't be read either."""
    return shown_once(
        request,
        state,
        lambda: student_page(
            request,
            state,
            problem=SIGNAL_NOT_SAVED,
            pressed=True,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        ),
        week_not_shown(request, NOT_SAVED_HEADING, SIGNAL_NOT_SAVED),
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


# ---------------------------------------------------------------- earlier homework to check


@dataclass(frozen=True)
class EarlierNote:
    """What one choice in Earlier homework to check did, ``said``, or could not do,
    ``problem``, for the item it named, with ``choice`` the press she made, kept on a
    refusal. ``folded`` is where the item was shown when she pressed it, in the fold or
    above it, so it is shown there again; ``None`` places it by the rules. ``on_card`` says
    the press was on the item's card in the week, where its result is said."""

    assignment_id: str
    said: str | None = None
    problem: str | None = None
    choice: str | None = None
    folded: bool | None = None
    on_card: bool = False


def earlier_card_anchor(assignment_id: str) -> str:
    """The id of the line on an item's card in the week that says what a press there did."""
    return f"earlier-card-{segment(assignment_id)}"


def earlier_anchor(assignment_id: str) -> str:
    """The id of an item in Earlier homework to check, apart from its card's on a week."""
    return f"earlier-{segment(assignment_id)}"


def earlier_action(assignment_id: str) -> str:
    """Where an item's choice form posts."""
    return f"/student/actions/assignments/{segment(assignment_id)}/earlier-work"


def earlier_made_with(key: bytes, day: date, assignment_id: str, choice: str) -> str:
    """What a choice form was made with, signed with the running process's key: the plan
    date its page was made for, tied to the assignment and the choice it offers."""
    made = day.isoformat()
    signed = json.dumps([made, assignment_id, choice]).encode("utf-8")
    return f"{made}.{hmac.new(key, signed, 'sha256').hexdigest()[:16]}"


def day_made_for(key: bytes, given: str, assignment_id: str, choice: str) -> date | None:
    """The plan date a choice form's page was made for, or ``None`` for a value no page of
    this process made for this assignment and choice."""
    made, _, _ = given.partition(".")
    day = read_date(made)
    if day is None or day.isoformat() != made:
        return None
    expected = earlier_made_with(key, day, assignment_id, choice)
    return day if hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")) else None


def earlier_receipt(key: bytes, day: date, assignment_id: str, said: str) -> str:
    """What a saved choice did, as its address carries it: the word, signed with the
    running process's key for the day it was saved for and the item it was about."""
    signed = json.dumps(["said", day.isoformat(), assignment_id, said]).encode("utf-8")
    return f"{said}.{hmac.new(key, signed, 'sha256').hexdigest()[:16]}"


def receipt_word(key: bytes, day: date, assignment_id: str, given: str) -> str | None:
    """The word an address carries for what a choice did, or ``None`` for one this process
    didn't sign for ``day`` and this item."""
    said, _, _ = given.partition(".")
    if said not in EARLIER_SAID:
        return None
    expected = earlier_receipt(key, day, assignment_id, said)
    return said if hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")) else None


def after_choice(week: date | None, said: str, assignment_id: str, place: str) -> str:
    """Where a saved choice sends her: the week she was on, at the item, shown where she
    pressed it, with what it did."""
    return address(
        PAGE,
        fragment=earlier_card_anchor(assignment_id)
        if place == "card"
        else earlier_anchor(assignment_id),
        week=None if week is None else week.isoformat(),
        earlier=assignment_id,
        earlier_said=said,
        earlier_place=place,
    )


@router.post(
    "/actions/assignments/{assignment_id:path}/earlier-work",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def choose_earlier_work(request: Request, assignment_id: str, state: State) -> Response:
    """Include earlier homework in today's plan, or take it out.

    A parent is answered 403 before the form is read, and nothing is written. Her form is
    read whole, each field once. The choice is for the household's day once the decision
    lock is held, and the form carries the day its page was made for, signed: a form no page
    made is refused, 422, and one made on another day is refused, 409, since its choices were
    that day's, even when the day turned while the press waited for the lock. Including
    work that is not on the earlier list now, reported Done or redated, is refused, 409.
    The same choice twice writes nothing and says so. Where the item was shown, above the
    fold or in it, only places it there again. Every refusal comes back on the week
    she was on with her press named beside the item. The cards the visit keeps where they
    were, which every press carries, stay there on the page it lands on. A refused write
    changes nothing. The choice never touches the assignment, its dates, her reports, or
    anyone's notes.
    """
    if viewer_of(request) == "parent":
        return not_hers(request, state, CHOICE_NOT_SAVED, NOT_HERS_TO_CHOOSE)
    fields, whole = await fields_of(
        request, EARLIER_FIELDS | {IN_PLACE}, may_be_absent=frozenset({IN_PLACE})
    )
    choice = fields.get("choice", "")
    given = fields.get("week", "").strip()
    week = week_named(given)
    place = fields.get("place", "")
    # The cards this visit keeps where they were, carried on so a choice regroups none.
    kept = InPlace.read(fields.get(IN_PLACE))
    # The household day once the lock has read it, so a refusal is shown for that day.
    today: date | None = None

    def refused(problem: str, code: int) -> Response:
        note = EarlierNote(
            assignment_id,
            problem=problem,
            choice=choice if choice in EARLIER_CHOICES else None,
            folded={"fold": True, "list": False}.get(place),
            on_card=place == "card",
        )
        return shown_once(
            request,
            state,
            lambda: student_page(
                request,
                state,
                week=week,
                earlier_note=note,
                pressed=True,
                in_place=kept,
                status_code=code,
                today=today,
            ),
            week_not_shown(request, CHOICE_NOT_SAVED, problem, fragment=EARLIER_WORK),
            code,
        )

    if (
        not whole
        or choice not in EARLIER_CHOICES
        or place not in EARLIER_PLACES
        or (given and week is None)
    ):
        return refused(CHOICE_BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT)
    made_for = day_made_for(state.result_key, fields["made_with"], assignment_id, choice)
    if made_for is None:
        return refused(CHOICE_BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT)
    include = choice == "include"
    try:
        async with state.decision_lock:
            today = state.clock.today()
            if made_for != today:
                return refused(CHOICE_FROM_ANOTHER_DAY, status.HTTP_409_CONFLICT)
            if include:
                found = read_everything(state.project_state, state.project_state)
                if assignment_id not in {item.assignment_id for item in found.assignments}:
                    return refused(NOT_ON_RECORD, status.HTTP_404_NOT_FOUND)
                eligible = {item.assignment_id for item in earlier_to_check(found, today)}
                if assignment_id not in eligible:
                    return refused(NOT_EARLIER_NOW, status.HTTP_409_CONFLICT)
            outcome = state.project_state.choose_catch_up(assignment_id, today, include=include)
    except NotOnRecord:
        return refused(NOT_ON_RECORD, status.HTTP_404_NOT_FOUND)
    except (ChoiceNotSaved, sqlite3.Error):
        logger.exception("a choice of earlier work could not be saved")
        return refused(CHOICE_FAILED, status.HTTP_500_INTERNAL_SERVER_ERROR)
    if isinstance(outcome, ChoiceMade):
        said = "included" if include else "removed"
    else:
        said = "already_included" if include else "already_removed"
    receipt = earlier_receipt(state.result_key, today, assignment_id, said)
    return sent_in_place(after_choice(week, receipt, assignment_id, place), kept)
