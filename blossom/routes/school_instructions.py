"""Review school instructions: which of the school's instructions for one assignment apply,
chosen after the paste that brought them.

The page lists every instruction kept for the assignment, those that apply,
those said before, and any waiting for review, each with a box, and a
separate box for none applying. Nothing is ticked in advance, and opening
it writes nothing. A save needs an answer, as the paste review does, and
goes through the store's one rule with the revision the page showed.

An answer that is not saved is never lost and never moved onto other facts.
It is read whole before anything touches the store, contradictions included.
When the instructions changed since the page it was made on, it is said in
words as the parent's unsaved choice, beside the facts as they stand, and
nothing is ticked; when they did not, its own ticks come back to be put right.
A form the page did not write is refused whole, and what it chose that can be
read is said back the same way, with nothing ticked. A choice said back is
carried in the form, with why, and shown again until a fresh one is made. A save the file refuses
is tried once, never again, and answered from one normal reading of the
record, or, when that fails too, by a page that reads no store and shows the
answer as it was sent. A save that lands returns to the page with what it
did, bound to the revision it was accepted at and signed by the running
process, so the page claims a choice applies only while that revision is the
one that stands, and says nothing of a result it did not sign.

A kept instruction longer than any paste travels in the form by its row, and
is put back in its words from what is kept before the choice is saved.

The page lives under the family's address, so her sign-in never reaches it;
a parent does, and the household with the sign-in off does, from the family's
own pages.
"""

import hmac
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from blossom.dependencies import ApplicationState
from blossom.routes.inbox import State
from blossom.routes.instruction_answers import (
    CarriedAccount,
    SaidBack,
    UnsavedChoice,
    review_answers,
    said_back,
)
from blossom.routes.navigation import (
    details_href,
    instructions_action_href,
    instructions_review_href,
)
from blossom.routes.student import viewer_of
from blossom.school_instructions import (
    InstructionsSettled,
    InstructionsStanding,
    InstructionsUnchanged,
    SchoolInstruction,
    SubmittedChoice,
)
from blossom.stores.project_state import Assignment
from blossom.stores.school_instructions import InstructionsForNoAssignment, UnreadableInstruction
from blossom.templating import page_templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/parent", tags=["parent"])
templates = page_templates()

SAVED_AS_CHOSEN: Final = (
    "Saved. The instructions chosen apply now, and the others are kept as history."
)
ALREADY_STOOD: Final = "Nothing changed: the school's instructions already stood as chosen."
SAVED_NONE_APPLIES: Final = (
    "Saved. No school instruction applies now. The instructions are kept as history."
)
STOOD_NONE_APPLIES: Final = "Nothing changed. No school instruction applies now."
SAVED_SINCE_CHANGED: Final = (
    "That choice was saved, and the school's instructions for this assignment have changed "
    "since. They are shown below as they stand now."
)
STOOD_SINCE_CHANGED: Final = (
    "That choice already stood when it was sent, and the school's instructions for this "
    "assignment have changed since. They are shown below as they stand now."
)
NOTHING_CHOSEN: Final = (
    "Choose the instructions that apply now, or that none applies. Nothing was saved."
)
CHOICES_CONTRADICT: Final = (
    "Choose the instructions that apply now, or that none applies, not both. Nothing was "
    "saved; the choices are still ticked."
)
CHANGED_SINCE_OPENED: Final = (
    "The school's instructions for this assignment changed since this page was opened, so "
    "nothing was saved. They are shown below as they stand now; choose again."
)
FORM_UNREADABLE: Final = (
    "The form could not be read, so nothing was saved. The instructions are shown below as "
    "they stand; choose again."
)
NOT_ON_RECORD: Final = (
    "This assignment is not on record, so there are no instructions of the school's to review."
)
INSTRUCTIONS_UNREADABLE: Final = (
    "The school's instructions for this assignment cannot be read right now, so they are not "
    "shown and nothing can be saved."
)
NOT_SAVED: Final = (
    "The choice could not be saved: the file refused it. Nothing was saved, and the choice is "
    "shown below as it was sent."
)

STANDINGS: Final = {
    "current": "Applies now.",
    "history": "Earlier, does not apply now.",
    "awaiting": "Waiting for review.",
}
RESULT: Final = "result"
"""The id of the paragraph a save's redirect lands on."""
RESULT_SHAPE: Final = re.compile(r"(saved|stood)\.(0|[1-9][0-9]{0,8})\.([0-9a-f]{16})")
"""The one shape a save writes: what it did, the revision as the count is spelled, and the
check as sixteen lower-case hexadecimal digits."""
RESULT_MAX_LENGTH: Final = 32

ResultKind = Literal["saved", "stood"]


def result_token(key: bytes, assignment_id: str, kind: ResultKind, revision: int) -> str:
    """What a save carries to the page it returns to: what it did, the revision it was
    accepted at, and a check signed with the running process's key that ties both to this
    assignment, so a token made for another assignment, worked out from the address, or
    signed before a restart says nothing."""
    return f"{kind}.{revision}.{_check(key, assignment_id, kind, revision)}"


def _check(key: bytes, assignment_id: str, kind: str, revision: int) -> str:
    signed = f"{assignment_id}\n{kind}\n{revision}".encode()
    return hmac.new(key, signed, "sha256").hexdigest()[:16]


def result_of(key: bytes, assignment_id: str, token: str | None) -> tuple[ResultKind, int] | None:
    """What a save did and the revision it was accepted at, from the address, or ``None``
    for anything the running process did not sign for this assignment. The whole token is
    held to the one shape a save writes before its check is compared, so any other
    character, length, or spelling says nothing."""
    if token is None or len(token) > RESULT_MAX_LENGTH:
        return None
    shape = RESULT_SHAPE.fullmatch(token)
    if shape is None:
        return None
    kind, revision, check = shape.groups()
    number = int(revision)
    if not hmac.compare_digest(check, _check(key, assignment_id, kind, number)):
        return None
    return ("saved" if kind == "saved" else "stood"), number


def result_said(
    result: tuple[ResultKind, int] | None, standing: InstructionsStanding | None
) -> str | None:
    """What the page says of a save it was returned to: what the save did, only while the
    revision it was accepted at still stands, and in its own words when what stands is that
    no instruction applies; that the instructions have changed since, when a later revision
    stands. A revision the record has not reached says nothing."""
    if result is None:
        return None
    kind, accepted = result
    revision = 0 if standing is None else standing.revision
    if accepted == revision:
        # Said from what stands, and only once the revision it was accepted at is the one
        # that stands.
        if standing is None or not standing.current:
            return SAVED_NONE_APPLIES if kind == "saved" else STOOD_NONE_APPLIES
        return SAVED_AS_CHOSEN if kind == "saved" else ALREADY_STOOD
    if accepted < revision:
        return SAVED_SINCE_CHANGED if kind == "saved" else STOOD_SINCE_CHANGED
    return None


def context_of(item: SchoolInstruction) -> str:
    """Where an instruction was read, as the page says it beside its standing. A card's day
    says where it was read, never when the teacher wrote it. An instruction kept by a save
    with no card to say where was read somewhere not known; one with no moment of its own
    came from the old note field."""
    if item.card_day is not None:
        return f"Read on the school's card for {spoken_month_day(item.card_day)}."
    if item.state == "awaiting":
        return "Found in the old note field after the upgrade, so where it was read is not known."
    if item.first_seen_at is not None:
        return "Where it was read is not known."
    return "Kept from the note saved before the school's instructions were kept apart."


def spoken_month_day(day: date) -> str:
    """A card's day as the page says it: ``October 1``."""
    return f"{day.strftime('%B')} {day.day}"


@dataclass(frozen=True)
class ShownRow:
    """One box on the page: the instruction's words, its row, what the page says of it, and
    whether it comes ticked, which only a page returned with the parent's own ticks, made
    against what stands, does."""

    number: int
    sequence: int
    text: str
    said: str
    ticked: bool


def reading_of(
    state: ApplicationState, assignment_id: str
) -> tuple[Assignment | None, InstructionsStanding | None, bool]:
    """The assignment, its instructions' standing, and whether they could be read, in one
    hold of the store."""
    on_record = state.project_state
    with on_record.reading():
        item = on_record.one_assignment(assignment_id)
        found = on_record.school_instruction_readings([assignment_id])
    if assignment_id in found.unreadable:
        return item, None, False
    return item, found.readable.get(assignment_id), True


def account_of(
    submitted: SubmittedChoice | None, unsaved: UnsavedChoice | None
) -> UnsavedChoice | None:
    """What an answer that was not saved chose: the choice read from a form the page did not
    write, or the readable answer itself."""
    if unsaved is not None:
        return unsaved
    return None if submitted is None else UnsavedChoice.of(submitted)


def carried_or(
    said: SaidBack | None, why: str | None, carried: CarriedAccount | None
) -> tuple[SaidBack | None, str | None]:
    """What the page says as not saved, and why: this answer's own account, or, when it has
    none, the one the page before showed and the form carried."""
    if said is None and carried is not None and carried.said:
        return carried.said[0], carried.why
    return said, why


def review_page(
    request: Request,
    state: ApplicationState,
    assignment_id: str,
    *,
    submitted: SubmittedChoice | None = None,
    unsaved: UnsavedChoice | None = None,
    carried: CarriedAccount | None = None,
    problem: str | None = None,
    status_code: int = status.HTTP_200_OK,
    failed: bool = False,
    result: tuple[ResultKind, int] | None = None,
) -> HTMLResponse:
    """The page as the record stands now, from one reading of it.

    ``submitted`` is an answer that was not saved, and ``problem`` and
    ``status_code`` what to say of it when it was made against what stands;
    when it was not, the page says the instructions changed, gives no tick
    back, and says the answer in words. ``unsaved`` is what a form the page
    did not write chose, said in words with nothing ticked. ``failed`` is a
    save the file refused, whose answer is said in words either way.
    ``carried`` is a choice the page before showed as not saved, shown again
    when this answer has nothing of its own to say. ``result`` is what a save
    did, said only as far as the revision it was accepted at still stands.
    """
    item, standing, readable = reading_of(state, assignment_id)
    account = account_of(submitted, unsaved)
    pressed = account is not None or (problem is not None and status_code >= 400)
    said, why = carried_or(said_back(account, None), None, carried)
    back = details_href(assignment_id, return_to="family")
    if item is None:
        return templates.TemplateResponse(
            request,
            "school_instructions_review.html",
            {
                "item": None,
                "problem": NOT_ON_RECORD,
                "pressed": pressed,
                "not_saved": said,
                "not_saved_why": why,
                "back": "/parent",
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )
    if not readable:
        return templates.TemplateResponse(
            request,
            "school_instructions_review.html",
            {
                "item": item,
                "problem": INSTRUCTIONS_UNREADABLE,
                "pressed": pressed,
                "not_saved": said,
                "not_saved_why": why,
                "back": back,
            },
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    kept = () if standing is None else standing.kept
    revision = 0 if standing is None else standing.revision
    ticked: frozenset[str] = frozenset()
    none_ticked = False
    not_saved: SaidBack | None = None
    why = None
    answer = None if submitted is None else submitted.resolved(kept)
    if unsaved is not None:
        # A form the page did not write: what it chose is said back, and nothing is ticked, so
        # the next save is a choice made afresh on the facts as they stand.
        not_saved, why = said_back(unsaved, kept), "unreadable"
    elif submitted is not None:
        if answer is None or not answer.made_against(revision, [row.text for row in kept]):
            # Made against instructions that changed since: nothing it ticked comes back on a
            # form carrying the revision that stands, and the parent chooses again.
            not_saved, why = said_back(account, kept), "stale"
            if not failed:
                problem, status_code = CHANGED_SINCE_OPENED, status.HTTP_409_CONFLICT
        else:
            ticked, none_ticked = answer.applies, answer.none_applies
            if failed:
                not_saved, why = said_back(UnsavedChoice.of(answer), kept), "failed"
    not_saved, why = carried_or(not_saved, why, carried)
    rows = [
        ShownRow(
            number=number,
            sequence=row.sequence,
            text=row.text,
            said=f"{STANDINGS[row.state]} {context_of(row)}",
            ticked=row.text in ticked,
        )
        for number, row in enumerate(kept)
    ]
    return templates.TemplateResponse(
        request,
        "school_instructions_review.html",
        {
            "item": item,
            "rows": rows,
            "revision": revision,
            "none_ticked": none_ticked,
            "not_saved": not_saved,
            "not_saved_why": why,
            "notice": None if pressed else result_said(result, standing),
            "problem": problem,
            "pressed": pressed,
            "action": instructions_action_href(assignment_id),
            "back": back,
        },
        status_code=status_code,
    )


def plain_page(
    request: Request,
    assignment_id: str,
    account: UnsavedChoice | None,
    problem: str,
    status_code: int,
    carried: CarriedAccount | None = None,
) -> HTMLResponse:
    """The page when the record cannot be read back: what happened, the answer as it was
    sent, or the choice the form carried when it chose nothing, and the ways on. Nothing
    here reads the store, offers a save, or claims one; an instruction selected by
    reference to a row is counted, since its text is not read."""
    said, why = carried_or(said_back(account, None), None, carried)
    return templates.TemplateResponse(
        request,
        "school_instructions_review.html",
        {
            "item": None,
            "plain": True,
            "problem": problem,
            "pressed": True,
            "not_saved": said,
            "not_saved_why": why,
            "back": details_href(assignment_id, return_to="family"),
        },
        status_code=status_code,
    )


def page_or_plain(
    request: Request,
    state: ApplicationState,
    assignment_id: str,
    submitted: SubmittedChoice | None,
    problem: str,
    status_code: int,
    *,
    unsaved: UnsavedChoice | None = None,
    carried: CarriedAccount | None = None,
    failed: bool = False,
) -> HTMLResponse:
    """The page with a refusal said first, read once; when the record cannot be read back,
    the plain page that reads no store and keeps the answer as it was sent."""
    try:
        return review_page(
            request,
            state,
            assignment_id,
            submitted=submitted,
            unsaved=unsaved,
            carried=carried,
            problem=problem,
            status_code=status_code,
            failed=failed,
        )
    except Exception:
        logger.exception("the review of school instructions could not be read back")
        return plain_page(
            request, assignment_id, account_of(submitted, unsaved), problem, status_code, carried
        )


@router.get(
    "/school-instructions/{assignment_id:path}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def review(
    request: Request, assignment_id: str, state: State, result: str | None = None
) -> Response:
    """The review, as the record stands. Nothing here writes. ``result`` is what a save just
    did, which the save wrote and the address only carries; a value this process did not sign
    for this assignment says nothing."""
    return review_page(
        request,
        state,
        assignment_id,
        result=result_of(state.result_key, assignment_id, result),
    )


@router.post(
    "/actions/school-instructions/{assignment_id:path}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def choose(request: Request, assignment_id: str, state: State) -> Response:
    """Save which instructions apply, or that none does, against the revision shown.

    The answer is read whole before the store is touched. A form the page
    did not make, nothing ticked, or a box beside none writes nothing and
    returns the page, with what the form chose said back. A kept instruction
    named by its row is put back in its words from what is kept. The choice
    is saved through the store's rule under the decision lock, once; a
    refusal of the file, an assignment gone, or instructions that cannot be
    read are answered after the lock is let go, with the answer kept.
    Settled, or already standing, it returns to the review with the revision
    it was accepted at. Who chose is a parent signed in, or the household
    with the sign-in off.
    """
    read = review_answers((await request.form()).multi_items())
    submitted = read.answer
    carried = read.carried
    if submitted is None:
        return page_or_plain(
            request,
            state,
            assignment_id,
            None,
            FORM_UNREADABLE,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            unsaved=read.unsaved,
            carried=carried,
        )
    if submitted.rows:
        # A kept row's words never change, so the words its row names now are the words the
        # parent was shown; a row not kept for this assignment is no form this page wrote.
        try:
            _, standing, readable = reading_of(state, assignment_id)
        except sqlite3.Error:
            logger.exception("the rows a choice named could not be read")
            return page_or_plain(
                request,
                state,
                assignment_id,
                submitted,
                NOT_SAVED,
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                failed=True,
                carried=carried,
            )
        if not readable:
            return page_or_plain(
                request,
                state,
                assignment_id,
                submitted,
                INSTRUCTIONS_UNREADABLE,
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                carried=carried,
            )
        resolved = submitted.resolved(() if standing is None else standing.kept)
        if resolved is None:
            return page_or_plain(
                request,
                state,
                assignment_id,
                None,
                FORM_UNREADABLE,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                unsaved=read.unsaved,
                carried=carried,
            )
        submitted = resolved
    choice = submitted.choice()
    if choice is None:
        return page_or_plain(
            request,
            state,
            assignment_id,
            submitted,
            CHOICES_CONTRADICT if submitted.contradicts else NOTHING_CHOSEN,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            carried=carried,
        )
    now, today = state.project_state.instruction_moment()
    try:
        async with state.decision_lock:
            outcome = state.project_state.settle_school_instructions(
                assignment_id,
                [],
                choice,
                authored_by="parent" if viewer_of(request) == "parent" else "household",
                now=now,
                today=today,
            )
    except UnreadableInstruction:
        return page_or_plain(
            request,
            state,
            assignment_id,
            submitted,
            INSTRUCTIONS_UNREADABLE,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    except sqlite3.Error:
        logger.exception("a choice of school instructions could not be saved")
        return page_or_plain(
            request,
            state,
            assignment_id,
            submitted,
            NOT_SAVED,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            failed=True,
        )
    if isinstance(outcome, InstructionsForNoAssignment):
        return page_or_plain(
            request, state, assignment_id, submitted, NOT_ON_RECORD, status.HTTP_404_NOT_FOUND
        )
    if isinstance(outcome, InstructionsSettled):
        token = result_token(state.result_key, assignment_id, "saved", outcome.revision)
    elif isinstance(outcome, InstructionsUnchanged):
        token = result_token(state.result_key, assignment_id, "stood", outcome.revision)
    else:
        return page_or_plain(
            request,
            state,
            assignment_id,
            submitted,
            CHANGED_SINCE_OPENED,
            status.HTTP_409_CONFLICT,
        )
    return RedirectResponse(
        instructions_review_href(assignment_id, result=token),
        status_code=status.HTTP_303_SEE_OTHER,
    )
