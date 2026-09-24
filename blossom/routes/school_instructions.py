"""Review school instructions: which of the school's instructions for one assignment apply,
chosen after the paste that brought them.

The page lists every instruction kept for the assignment, those that apply,
those said before, and any waiting for review, each with a box, and a
separate box for none applying. Nothing is ticked in advance, and opening
it writes nothing. A save needs an answer, as the paste review does, and
goes through the store's one rule with the revision the page showed: a
choice made against instructions that changed since is refused whole, with
the facts as they stand and the choice that was not saved; a choice that
already stands saves nothing more. The page lives under the family's
address, so her sign-in never reaches it; a parent does, and the household
with the sign-in off does, from the family's own pages.
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Final

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from blossom.dependencies import ApplicationState
from blossom.intake import TEXT_MAX_LENGTH
from blossom.routes.inbox import State, review_key
from blossom.routes.navigation import (
    details_href,
    instructions_action_href,
    instructions_review_href,
)
from blossom.routes.student import viewer_of
from blossom.school_instructions import (
    InstructionChoice,
    InstructionChoiceStale,
    InstructionsSettled,
    InstructionsStanding,
    SchoolInstruction,
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

STANDINGS: Final = {
    "current": "Applies now.",
    "history": "Earlier, does not apply now.",
    "awaiting": "Waiting for review.",
}


def context_of(item: SchoolInstruction) -> str:
    """Where an instruction was read, as the page says it beside its standing. A card's day
    says where it was read, never when the teacher wrote it."""
    if item.card_day is not None:
        return f"Read on the school's card for {spoken_month_day(item.card_day)}."
    if item.state == "awaiting":
        return "Found in the old note field after the upgrade, so where it was read is not known."
    return "Kept from the note saved before the school's instructions were kept apart."


def spoken_month_day(day: date) -> str:
    """A card's day as the page says it: ``October 1``."""
    return f"{day.strftime('%B')} {day.day}"


@dataclass(frozen=True)
class ShownRow:
    """One box on the page: the instruction's words, what the page says of it, and whether it
    comes ticked, which only a page returned with the parent's own ticks does."""

    number: int
    text: str
    said: str
    ticked: bool


@dataclass(frozen=True)
class Answer:
    """What the form said: the revision and the words it showed, the words ticked, and none."""

    revision: int
    shown: tuple[str, ...]
    applies: frozenset[str]
    none_applies: bool


def answer_from(fields: dict[str, str]) -> Answer | None:
    """The form's answer, or ``None`` for a form this page did not make: a revision that is
    not a count, a box or words under a number that is no number the page writes, words
    missing for a box, or words too long for any instruction."""
    revision = fields.get("revision", "")
    if not (revision.isascii() and revision.isdigit() and len(revision) <= 9):
        return None
    words: dict[int, str] = {}
    ticked: set[int] = set()
    for name, value in fields.items():
        head, _, number = name.partition("-")
        if name in ("revision", "none"):
            continue
        if not review_key(number):
            return None
        if head == "instruction" and len(value) <= TEXT_MAX_LENGTH:
            words[int(number)] = value
        elif head == "apply" and value == "1":
            ticked.add(int(number))
        else:
            return None
    if not ticked <= set(words) or fields.get("none", "1") != "1":
        return None
    return Answer(
        revision=int(revision),
        shown=tuple(words[number] for number in sorted(words)),
        applies=frozenset(words[number] for number in ticked),
        none_applies="none" in fields,
    )


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


def review_page(
    request: Request,
    state: ApplicationState,
    assignment_id: str,
    *,
    ticked: frozenset[str] = frozenset(),
    none_ticked: bool = False,
    not_saved: Answer | None = None,
    notice: str | None = None,
    problem: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """The page as the record stands now. A page returned for a problem shows the parent's
    own ticks only when they were made against what stands; otherwise the choice that was
    not saved is said in words, beside the facts."""
    item, standing, readable = reading_of(state, assignment_id)
    if item is None:
        return templates.TemplateResponse(
            request,
            "school_instructions_review.html",
            {"item": None, "problem": NOT_ON_RECORD, "back": "/parent"},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    if not readable:
        return templates.TemplateResponse(
            request,
            "school_instructions_review.html",
            {
                "item": item,
                "problem": INSTRUCTIONS_UNREADABLE,
                "back": details_href(assignment_id, return_to="family"),
            },
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    kept = () if standing is None else standing.kept
    rows = [
        ShownRow(
            number=number,
            text=kept_item.text,
            said=f"{STANDINGS[kept_item.state]} {context_of(kept_item)}",
            ticked=kept_item.text in ticked,
        )
        for number, kept_item in enumerate(kept)
    ]
    return templates.TemplateResponse(
        request,
        "school_instructions_review.html",
        {
            "item": item,
            "rows": rows,
            "revision": 0 if standing is None else standing.revision,
            "none_ticked": none_ticked,
            "not_saved": not_saved,
            "notice": notice,
            "problem": problem,
            "action": instructions_action_href(assignment_id),
            "back": details_href(assignment_id, return_to="family"),
        },
        status_code=status_code,
    )


@router.get(
    "/school-instructions/{assignment_id:path}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def review(
    request: Request, assignment_id: str, state: State, saved: str | None = None
) -> Response:
    """The review, as the record stands. Nothing here writes. ``saved`` is what the save just
    did, which the server chose and the address only carries: any other word says nothing."""
    notice = {"1": SAVED_AS_CHOSEN, "0": ALREADY_STOOD}.get(saved or "")
    return review_page(request, state, assignment_id, notice=notice)


@router.post(
    "/actions/school-instructions/{assignment_id:path}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def choose(request: Request, assignment_id: str, state: State) -> Response:
    """Save which instructions apply, or that none does, against the revision shown.

    A form this page did not make, nothing ticked, or a box beside none
    writes nothing and returns the page. The choice is saved through the
    store's rule under the decision lock: settled, or already standing, it
    returns to the review; made against instructions that changed since, it
    is refused whole. Who chose is a parent signed in, or the household
    with the sign-in off.
    """
    fields = await form_of(request)
    answer = None if fields is None else answer_from(fields)
    if answer is None:
        return review_page(
            request,
            state,
            assignment_id,
            problem=FORM_UNREADABLE,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if answer.applies and answer.none_applies:
        return review_page(
            request,
            state,
            assignment_id,
            ticked=answer.applies,
            none_ticked=True,
            problem=CHOICES_CONTRADICT,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if not answer.applies and not answer.none_applies:
        return review_page(
            request,
            state,
            assignment_id,
            problem=NOTHING_CHOSEN,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    choice = InstructionChoice(
        shown_revision=answer.revision,
        shown=answer.shown,
        applies=answer.applies,
        none_applies=answer.none_applies,
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
        logger.warning("school instructions unreadable at a review save")
        return review_page(request, state, assignment_id)
    if isinstance(outcome, InstructionsForNoAssignment):
        return review_page(request, state, assignment_id)
    if isinstance(outcome, InstructionChoiceStale):
        return review_page(
            request,
            state,
            assignment_id,
            not_saved=answer,
            problem=CHANGED_SINCE_OPENED,
            status_code=status.HTTP_409_CONFLICT,
        )
    saved = "1" if isinstance(outcome, InstructionsSettled) else "0"
    return RedirectResponse(
        instructions_review_href(assignment_id, saved=saved),
        status_code=status.HTTP_303_SEE_OTHER,
    )
