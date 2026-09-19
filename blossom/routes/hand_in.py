"""Her hand-in update, from an assignment's details: two routes beside her work update's.

Done means she finished her part and turns nothing in, so what she says about
delivery has forms of its own. They are on the assignment's details and
nowhere else, so every result is shown there, at the section the forms sit
in, with the way back the details carried. The routes follow her work
update's: the form is read whole before anything else, who may save is
decided from the sign-in and never from the form, the save is one operation
under the decision lock, a success is a redirect to a page that says what
stands, and a refusal is the same section with what she chose and wrote
still in it. A parent reads the section and is told the update is hers to
make. Nothing here asks a model, sends a reminder, or tells the school
anything, and a plan never reads what is saved here.
"""

import logging
from typing import Final, cast

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from blossom.authored_text import TextRefused, multiline, single_line
from blossom.dependencies import ApplicationState
from blossom.hand_in import (
    HAND_IN_NOTE_MAX_LENGTH,
    NEEDS_HAND_IN,
    NEXT_ACTION_MAX_LENGTH,
    NOT_REQUIRED,
    TURNED_IN,
    UNKNOWN,
    HandInAlreadySaved,
    HandInConflict,
    HandInSaved,
    HandInState,
    HandInUndone,
    already_undone,
)
from blossom.routes.forms import TOKEN_MAX_LENGTH, fields_of
from blossom.routes.navigation import RETURN_FIELDS, ReturnTo, details_href, read_return
from blossom.routes.student import (
    ALREADY_UNDONE,
    BAD_FORM,
    BAD_RETURN,
    NOT_HERS_TO_UPDATE,
    HandInCard,
    ReturnLink,
    State,
    detail_page,
    gone_page,
    showable,
    templates,
    viewer_of,
)
from blossom.stores.project_state import CouldNotSave, UnknownAssignment, UnknownHandIn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/student", tags=["student"])

STATES: Final = (NEEDS_HAND_IN, TURNED_IN, NOT_REQUIRED, UNKNOWN)
RESULT: Final = "hand-in-result"
CHOOSE_A_STATE: Final = "Choose one: Still to turn in, Turned in, Nothing to turn in, or Not sure."
NEXT_ACTION_TOO_LONG: Final = f"Keep the next step to {NEXT_ACTION_MAX_LENGTH} characters or fewer."
NEXT_ACTION_ONE_LINE: Final = "Keep the next step on one line."
HAND_IN_NOTE_TOO_LONG: Final = f"Keep your note to {HAND_IN_NOTE_MAX_LENGTH} characters or fewer."
UNKEPT_CHARACTER: Final = (
    "That has a character Blossom cannot keep. Take it out and save again; the rest is as you "
    "wrote it."
)
HAND_IN_CHANGED: Final = (
    "These details changed while you were away. Your update has not been saved."
)
HAND_IN_CANNOT_UNDO: Final = (
    "Your hand-in update has changed, so it cannot be undone from that page. This page shows "
    "what stands."
)
NOT_A_HAND_IN_OF_THIS: Final = (
    "That form names a hand-in update this assignment does not have, so nothing was saved. "
    "This page shows what stands; choose and save from here."
)
HAND_IN_NOT_SAVED: Final = (
    "Your hand-in update could not be saved. Your words are still here. Try again."
)
HAND_IN_NOT_UNDONE: Final = (
    "Your hand-in update could not be undone, and nothing was changed. Try again."
)
HAND_IN_FIELDS: Final = (
    frozenset({"state", "next_action", "note", "expected_hand_in_id"}) | RETURN_FIELDS
)
HAND_IN_UNDO_FIELDS: Final = frozenset({"hand_in_id"}) | RETURN_FIELDS
"""The fields each form sends, each once. Anything else, or anything twice, is refused."""
NOTHING_CHOSEN: Final = frozenset({"state"}) | RETURN_FIELDS
"""What a browser may leave out: the radio group when none is chosen, and the way back,
which a form opened with none carries none of."""


def after(assignment_id: str, said: str, back: ReturnTo) -> str:
    """Where a save or an undo sends her: the details, at the result, with what happened."""
    return details_href(
        assignment_id, fragment=f"{RESULT}-{assignment_id}", hand_in=said, **back.fields()
    )


def words_refused(refusal: TextRefused, *, next_action: bool) -> str:
    """The sentence for words the record will not keep, by the rule they met."""
    if refusal.reason == "too_long":
        return NEXT_ACTION_TOO_LONG if next_action else HAND_IN_NOTE_TOO_LONG
    if refusal.reason == "line_break":
        return NEXT_ACTION_ONE_LINE
    return UNKEPT_CHARACTER


def could_not(
    request: Request,
    state: ApplicationState,
    assignment_id: str,
    back: ReturnTo,
    card: HandInCard,
) -> HTMLResponse:
    """The page after a write the file refused: the section with what she chose and wrote,
    and no word of a save. When the details cannot be read back either, the plain page her
    work update uses, which reads no store."""
    try:
        return detail_page(
            request,
            state,
            assignment_id,
            back,
            hand_in=card,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    except Exception:
        logger.exception("the details could not be read back after a failed hand-in save")
        return templates.TemplateResponse(
            request,
            "student_update_recovery.html",
            {
                "card": None,
                "hand_in_card": card,
                "ways_back": [
                    ReturnLink(
                        details_href(assignment_id, **back.fields()), "Back to the assignment"
                    )
                ],
                "sample": state.settings.sample,
            },
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@router.post(
    "/actions/assignments/{assignment_id}/hand-in",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def hand_in_from_the_page(request: Request, assignment_id: str, state: State) -> Response:
    """What she says about turning one assignment in, with one next step and a note if she
    wants them.

    The form is read whole. It carries the last hand-in event the page
    showed, which must be one of this assignment's. The same state, action,
    and note as what stands is already saved, with nothing written and no new
    day; a page whose head has moved on is answered 409 with what stands now
    above a form still holding what she meant to save, and nothing is
    overwritten; anything else is appended. A next step left in the form
    beside another state is not kept, and the page says so beside the field.
    Words the record will not keep are said beside the field they are in,
    422, with everything else as she left it. A parent signed in is answered
    403. Her work update is not read and not changed.
    """
    fields, whole = await fields_of(request, HAND_IN_FIELDS, may_be_absent=NOTHING_CHOSEN)
    viewer = viewer_of(request)
    back, valid = read_return(fields, viewer=viewer, showable=showable)
    if viewer == "parent":
        return detail_page(
            request,
            state,
            assignment_id,
            back,
            problem=NOT_HERS_TO_UPDATE,
            status_code=status.HTTP_403_FORBIDDEN,
        )
    said = fields.get("state", "").strip()
    action = fields.get("next_action", "")
    note = fields.get("note", "")
    token = fields.get("expected_hand_in_id", "").strip()
    chosen = said if said in STATES and whole else None

    def refused(problem: str, code: int, *, field: str | None = None) -> Response:
        return detail_page(
            request,
            state,
            assignment_id,
            back,
            hand_in=HandInCard(
                change=True,
                problem=problem,
                field=field,
                state=chosen,
                next_action=action,
                note=note,
                unsaved=code == status.HTTP_409_CONFLICT,
            ),
            status_code=code,
        )

    if not whole:
        return refused(BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if not valid:
        return refused(BAD_RETURN, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if said not in STATES:
        return refused(CHOOSE_A_STATE, status.HTTP_422_UNPROCESSABLE_CONTENT, field="state")
    if said == NEEDS_HAND_IN:
        try:
            single_line(action, NEXT_ACTION_MAX_LENGTH)
        except TextRefused as refusal:
            return refused(
                words_refused(refusal, next_action=True),
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                field="next_action",
            )
    try:
        multiline(note, HAND_IN_NOTE_MAX_LENGTH)
    except TextRefused as refusal:
        return refused(
            words_refused(refusal, next_action=False),
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            field="note",
        )
    if len(token) > TOKEN_MAX_LENGTH:
        return refused(NOT_A_HAND_IN_OF_THIS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    try:
        async with state.decision_lock:
            result = state.project_state.record_hand_in(
                assignment_id,
                cast(HandInState, said),
                action,
                note,
                expected_head=token or None,
                now=state.clock.now(),
                today=state.clock.today(),
            )
    except UnknownAssignment:
        return gone_page(
            request,
            state,
            back,
            assignment_id,
            hand_in=HandInCard(state=chosen, next_action=action, note=note),
        )
    except UnknownHandIn:
        return refused(NOT_A_HAND_IN_OF_THIS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    except CouldNotSave:
        logger.exception("her hand-in update on %s could not be saved", assignment_id)
        return could_not(
            request,
            state,
            assignment_id,
            back,
            HandInCard(
                change=True,
                problem=HAND_IN_NOT_SAVED,
                state=chosen,
                next_action=action,
                note=note,
            ),
        )
    match result:
        case HandInSaved():
            where = after(assignment_id, "saved", back)
        case HandInAlreadySaved():
            where = after(assignment_id, "same", back)
        case HandInConflict():
            return refused(HAND_IN_CHANGED, status.HTTP_409_CONFLICT)
    return RedirectResponse(where, status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/actions/assignments/{assignment_id}/undo-hand-in",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def undo_hand_in_from_the_page(
    request: Request, assignment_id: str, state: State
) -> Response:
    """Take her latest hand-in update back, restoring what stood before it.

    The button names the update it takes back, which must be one of this
    assignment's. One that is not the latest, or is itself an undo, is
    answered 409 and nothing is written: when the latest event is the undo
    of that very update, the page says it was already undone; anything else
    says the update has changed. It is never aimed at a newer update
    instead. A parent is answered 403.
    """
    fields, whole = await fields_of(request, HAND_IN_UNDO_FIELDS, may_be_absent=RETURN_FIELDS)
    viewer = viewer_of(request)
    back, valid = read_return(fields, viewer=viewer, showable=showable)
    if viewer == "parent":
        return detail_page(
            request,
            state,
            assignment_id,
            back,
            problem=NOT_HERS_TO_UPDATE,
            status_code=status.HTTP_403_FORBIDDEN,
        )
    named = fields.get("hand_in_id", "").strip()

    def refused(problem: str, code: int) -> Response:
        return detail_page(
            request,
            state,
            assignment_id,
            back,
            hand_in=HandInCard(problem=problem),
            status_code=code,
        )

    if not whole:
        return refused(BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if not valid:
        return refused(BAD_RETURN, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if len(named) > TOKEN_MAX_LENGTH:
        return refused(NOT_A_HAND_IN_OF_THIS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    try:
        async with state.decision_lock:
            result = state.project_state.undo_hand_in(
                assignment_id, named, now=state.clock.now(), today=state.clock.today()
            )
    except UnknownAssignment:
        return gone_page(request, state, back, assignment_id)
    except UnknownHandIn:
        return refused(NOT_A_HAND_IN_OF_THIS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    except CouldNotSave:
        logger.exception("her hand-in update on %s could not be undone", assignment_id)
        return could_not(
            request, state, assignment_id, back, HandInCard(problem=HAND_IN_NOT_UNDONE)
        )
    match result:
        case HandInUndone():
            return RedirectResponse(
                after(assignment_id, "undone", back), status_code=status.HTTP_303_SEE_OTHER
            )
        case HandInConflict() as conflict:
            return refused(
                ALREADY_UNDONE if already_undone(conflict, named) else HAND_IN_CANNOT_UNDO,
                status.HTTP_409_CONFLICT,
            )
