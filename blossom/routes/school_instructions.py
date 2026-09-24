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
words as not saved, beside the facts as they stand, and nothing is ticked;
when they did not, its own ticks come back to be put right. A save the file
refuses is tried once, never again, and answered from one normal reading of
the record, or, when that fails too, by a page that reads no store and shows
the answer as it was sent. A save that lands returns to the page with what it
did, bound to the revision it was accepted at, so the page claims a choice
applies only while that revision is the one that stands.

The page lives under the family's address, so her sign-in never reaches it;
a parent does, and the household with the sign-in off does, from the family's
own pages.
"""

import hashlib
import hmac
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from blossom.dependencies import ApplicationState
from blossom.routes.inbox import State
from blossom.routes.instruction_answers import review_answer
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

ResultKind = Literal["saved", "stood"]


def result_token(assignment_id: str, kind: ResultKind, revision: int) -> str:
    """What a save carries to the page it returns to: what it did, the revision it was
    accepted at, and a check that ties both to this assignment, so a token made for another
    assignment, or made up, says nothing."""
    return f"{kind}.{revision}.{_check(assignment_id, kind, revision)}"


def _check(assignment_id: str, kind: str, revision: int) -> str:
    return hashlib.sha256(f"{assignment_id}\n{kind}\n{revision}".encode()).hexdigest()[:16]


def result_of(assignment_id: str, token: str | None) -> tuple[ResultKind, int] | None:
    """What a save did and the revision it was accepted at, from the address, or ``None``
    for anything the save did not write for this assignment."""
    if not token:
        return None
    kind, _, rest = token.partition(".")
    revision, _, check = rest.partition(".")
    if kind not in ("saved", "stood") or not (
        revision.isascii() and revision.isdigit() and 0 < len(revision) <= 9
    ):
        return None
    number = int(revision)
    if str(number) != revision or not hmac.compare_digest(
        check, _check(assignment_id, kind, number)
    ):
        return None
    return ("saved" if kind == "saved" else "stood"), number


def result_said(result: tuple[ResultKind, int] | None, revision: int) -> str | None:
    """What the page says of a save it was returned to: that the choice applies only while
    the revision it was accepted at still stands, and that it has changed since when a
    later one stands. A revision the record has not reached says nothing."""
    if result is None:
        return None
    kind, accepted = result
    if accepted == revision:
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
    """One box on the page: the instruction's words, what the page says of it, and whether it
    comes ticked, which only a page returned with the parent's own ticks, made against what
    stands, does."""

    number: int
    text: str
    said: str
    ticked: bool


async def form_of(request: Request) -> dict[str, str] | None:
    """The form's fields, each once and each text, or ``None`` when a field came twice or was
    a file: not a form this page made."""
    form = await request.form()
    fields: dict[str, str] = {}
    for name, value in form.multi_items():
        if name in fields or not isinstance(value, str):
            return None
        fields[name] = value
    return fields


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


def sent(submitted: SubmittedChoice | None) -> SubmittedChoice | None:
    """The answer to say as not saved: one that chose something, some or none."""
    if submitted is None or not (submitted.applies or submitted.none_applies):
        return None
    return submitted


def review_page(
    request: Request,
    state: ApplicationState,
    assignment_id: str,
    *,
    submitted: SubmittedChoice | None = None,
    problem: str | None = None,
    status_code: int = status.HTTP_200_OK,
    failed: bool = False,
    result: tuple[ResultKind, int] | None = None,
) -> HTMLResponse:
    """The page as the record stands now, from one reading of it.

    ``submitted`` is an answer that was not saved, and ``problem`` and
    ``status_code`` what to say of it when it was made against what stands;
    when it was not, the page says the instructions changed, gives no tick
    back, and says the answer in words. ``failed`` is a save the file refused,
    whose answer is said in words either way. ``result`` is what a save did,
    said only as far as the revision it was accepted at still stands.
    """
    item, standing, readable = reading_of(state, assignment_id)
    pressed = submitted is not None or (problem is not None and status_code >= 400)
    back = details_href(assignment_id, return_to="family")
    if item is None:
        return templates.TemplateResponse(
            request,
            "school_instructions_review.html",
            {
                "item": None,
                "problem": NOT_ON_RECORD,
                "pressed": pressed,
                "not_saved": sent(submitted),
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
                "not_saved": sent(submitted),
                "back": back,
            },
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    kept = () if standing is None else standing.kept
    revision = 0 if standing is None else standing.revision
    ticked: frozenset[str] = frozenset()
    none_ticked = False
    not_saved = None
    if submitted is not None:
        if not submitted.made_against(revision, [row.text for row in kept]):
            # Made against instructions that changed since: nothing it ticked comes back on a
            # form carrying the revision that stands, and the parent chooses again.
            not_saved = sent(submitted)
            if not failed:
                problem, status_code = CHANGED_SINCE_OPENED, status.HTTP_409_CONFLICT
        else:
            ticked, none_ticked = submitted.applies, submitted.none_applies
            if failed:
                not_saved = sent(submitted)
    rows = [
        ShownRow(
            number=number,
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
            "notice": None if pressed else result_said(result, revision),
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
    submitted: SubmittedChoice | None,
    problem: str,
    status_code: int,
) -> HTMLResponse:
    """The page when the record cannot be read back: what happened, the answer as it was
    sent, and the ways on. Nothing here reads the store, offers a save, or claims one."""
    return templates.TemplateResponse(
        request,
        "school_instructions_review.html",
        {
            "item": None,
            "plain": True,
            "problem": problem,
            "pressed": True,
            "not_saved": sent(submitted),
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
            problem=problem,
            status_code=status_code,
            failed=failed,
        )
    except Exception:
        logger.exception("the review of school instructions could not be read back")
        return plain_page(request, assignment_id, submitted, problem, status_code)


@router.get(
    "/school-instructions/{assignment_id:path}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def review(
    request: Request, assignment_id: str, state: State, result: str | None = None
) -> Response:
    """The review, as the record stands. Nothing here writes. ``result`` is what a save just
    did, which the save wrote and the address only carries; a value it did not write for this
    assignment says nothing."""
    return review_page(request, state, assignment_id, result=result_of(assignment_id, result))


@router.post(
    "/actions/school-instructions/{assignment_id:path}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def choose(request: Request, assignment_id: str, state: State) -> Response:
    """Save which instructions apply, or that none does, against the revision shown.

    The answer is read whole before the store is touched. A form this page
    did not make, nothing ticked, or a box beside none writes nothing and
    returns the page. The choice is saved through the store's rule under the
    decision lock, once; a refusal of the file, an assignment gone, or
    instructions that cannot be read are answered after the lock is let go,
    with the answer kept. Settled, or already standing, it returns to the
    review with the revision it was accepted at. Who chose is a parent signed
    in, or the household with the sign-in off.
    """
    fields = await form_of(request)
    submitted = None if fields is None else review_answer(fields)
    if submitted is None:
        return page_or_plain(
            request,
            state,
            assignment_id,
            None,
            FORM_UNREADABLE,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    choice = submitted.choice()
    if choice is None:
        return page_or_plain(
            request,
            state,
            assignment_id,
            submitted,
            CHOICES_CONTRADICT if submitted.contradicts else NOTHING_CHOSEN,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
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
        token = result_token(assignment_id, "saved", outcome.revision)
    elif isinstance(outcome, InstructionsUnchanged):
        token = result_token(assignment_id, "stood", outcome.revision)
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
