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
from dataclasses import replace
from datetime import date, datetime
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
from blossom.noticing import read_everything
from blossom.routes.forms import TOKEN_MAX_LENGTH, fields_of
from blossom.routes.navigation import (
    RETURN_FIELDS,
    TO_TURN_IN_PAGE,
    WEEK_PAGE,
    ReturnTo,
    address,
    details_href,
    read_return,
)
from blossom.routes.student import (
    ALREADY_UNDONE,
    BAD_FORM,
    BAD_RETURN,
    NOT_HERS_TO_UPDATE,
    HandInCard,
    ListCard,
    ReturnLink,
    State,
    detail_page,
    gone_page,
    showable,
    student_page,
    templates,
    viewer_of,
)
from blossom.stores.project_state import CouldNotSave, UnknownAssignment, UnknownHandIn
from blossom.to_turn_in import result_for, to_turn_in

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
SHOWN_ON: Final = frozenset({"hand_in_view"})
"""The field a form on her To turn in list sends beside the rest: ``list`` from the page of
its own, ``week`` from the section on her week. It decides where the result is shown
and nothing else. A form on an assignment's details sends none."""
LIST_VIEWS: Final = ("list", "week")
GONE_FROM_THE_LIST: Final = "That assignment is not on record now, so nothing was changed."
LIST_CHANGED: Final = (
    "That changed while you were away, so nothing was saved. The list shows what stands now."
)
LIST_NOT_SAVED: Final = "That could not be saved, and nothing was changed. Try again."
LIST_NOT_A_HAND_IN_OF_THIS: Final = (
    "That form names a hand-in update this assignment does not have, so nothing was saved. "
    "The list shows what stands now."
)
LIST_BAD_FORM: Final = (
    "That form carried a field twice, or one this page does not send, so nothing was saved. "
    "Press again from the list."
)
LIST_ALREADY_UNDONE: Final = "That update was already undone. The list shows what stands now."
ON_THE_LIST: Final[dict[str, str]] = {
    HAND_IN_CHANGED: LIST_CHANGED,
    HAND_IN_NOT_SAVED: LIST_NOT_SAVED,
    NOT_A_HAND_IN_OF_THIS: LIST_NOT_A_HAND_IN_OF_THIS,
    BAD_FORM: LIST_BAD_FORM,
    ALREADY_UNDONE: LIST_ALREADY_UNDONE,
}
"""The sentences written for an assignment's details that would be untrue on the list, which
has no card, no form to choose from, and no words of hers to keep, each with what the list
says instead. Any other refusal reads the same in both places."""
HAND_IN_FIELDS: Final = (
    frozenset({"state", "next_action", "note", "expected_hand_in_id"}) | RETURN_FIELDS | SHOWN_ON
)
HAND_IN_UNDO_FIELDS: Final = frozenset({"hand_in_id"}) | RETURN_FIELDS | SHOWN_ON
"""The fields each form sends, each once. Anything else, or anything twice, is refused."""
NOTHING_CHOSEN: Final = frozenset({"state"}) | RETURN_FIELDS | SHOWN_ON
"""What a browser may leave out: the radio group when none is chosen, and the way back,
which a form opened with none carries none of."""


def list_page(
    request: Request,
    state: ApplicationState,
    *,
    card: ListCard | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """Her To turn in list on a page of its own, from one reading of the record.

    The reading is the one her week makes, so the list costs no read of its
    own and a long list costs no more than a short one. Nothing is written,
    and what the address says a press did is looked up in that same reading.
    """
    everything = read_everything(state.project_state, state.project_state)
    still = to_turn_in(everything)
    shown = None if card is None else replace(card, result=result_for(everything, *card.asked))
    return templates.TemplateResponse(
        request,
        "student_to_turn_in.html",
        {
            "rows": still.rows,
            "unreadable": still.unreadable,
            "list_card": shown,
            "viewer": viewer_of(request),
            "sample": state.settings.sample,
        },
        status_code=status_code,
    )


@router.get("/to-turn-in", response_class=HTMLResponse, include_in_schema=False)
def to_turn_in_list(
    request: Request,
    state: State,
    hand_in_said: str | None = None,
    about: str | None = None,
) -> HTMLResponse:
    """Everything she reports as still to turn in. Reading it changes nothing: no event, no
    reminder, no plan. ``hand_in_said`` and ``about`` are what a press just did and to
    which assignment, chosen by the server; any other word says nothing."""
    card = ListCard(asked=(hand_in_said, about)) if hand_in_said else None
    return list_page(request, state, card=card)


def shown_on(fields: dict[str, str]) -> tuple[str | None, bool]:
    """Where a form says its result is to be shown, and whether it said so in words these
    pages make: nothing for the details, or one of the two places the list is."""
    given = fields.get("hand_in_view", "").strip()
    return (given or None, given in ("", *LIST_VIEWS))


def on_the_list(
    request: Request,
    state: ApplicationState,
    view: str,
    *,
    problem: str,
    status_code: int,
) -> HTMLResponse:
    """A refusal shown where the press was made, with the list as it stands now."""
    card = ListCard(problem=ON_THE_LIST.get(problem, problem))
    if view == "week":
        return student_page(request, state, turning_in=card, status_code=status_code)
    return list_page(request, state, card=card, status_code=status_code)


def after_the_list(view: str, said: str, assignment_id: str) -> str:
    """Where a press or an undo on the list sends her: back to where she pressed, at the
    place that says what happened, which is there whether or not the row still is."""
    page = WEEK_PAGE if view == "week" else TO_TURN_IN_PAGE
    return address(page, fragment="to-turn-in-result", hand_in_said=said, about=assignment_id)


def after(assignment_id: str, said: str, back: ReturnTo) -> str:
    """Where a save or an undo sends her: the details, at the result, with what happened."""
    return details_href(
        assignment_id, fragment=f"{RESULT}-{assignment_id}", hand_in=said, **back.fields()
    )


def accepted_at(state: ApplicationState) -> tuple[datetime, date]:
    """The moment a hand-in event is accepted and the household day of that same moment.

    The clock is read once. Reading it again for the day would let the
    household's midnight fall between the two, and keep an event stamped
    on one day under the next."""
    now = state.clock.now()
    return now, now.astimezone(state.clock.zone).date()


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
    view, view_known = shown_on(fields)
    valid = valid and view_known
    view = view if view in LIST_VIEWS else None
    if viewer == "parent" and view is not None:
        return on_the_list(
            request, state, view, problem=NOT_HERS_TO_UPDATE, status_code=status.HTTP_403_FORBIDDEN
        )
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
    # A recoverable choice is not permission to accept the malformed form.
    chosen = said if said in STATES else None

    def refused(problem: str, code: int, *, field: str | None = None) -> Response:
        if view is not None:
            return on_the_list(request, state, view, problem=problem, status_code=code)
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
            now, today = accepted_at(state)
            result = state.project_state.record_hand_in(
                assignment_id,
                cast(HandInState, said),
                action,
                note,
                expected_head=token or None,
                now=now,
                today=today,
            )
    except UnknownAssignment:
        if view is not None:
            return refused(GONE_FROM_THE_LIST, status.HTTP_404_NOT_FOUND)
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
        if view is not None:
            return refused(HAND_IN_NOT_SAVED, status.HTTP_500_INTERNAL_SERVER_ERROR)
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
        case HandInSaved() if view is not None:
            where = after_the_list(view, "turned_in", assignment_id)
        case HandInAlreadySaved() if view is not None:
            where = after_the_list(view, "same", assignment_id)
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
    fields, whole = await fields_of(
        request, HAND_IN_UNDO_FIELDS, may_be_absent=RETURN_FIELDS | SHOWN_ON
    )
    viewer = viewer_of(request)
    back, valid = read_return(fields, viewer=viewer, showable=showable)
    view, view_known = shown_on(fields)
    valid = valid and view_known
    view = view if view in LIST_VIEWS else None
    if viewer == "parent" and view is not None:
        return on_the_list(
            request, state, view, problem=NOT_HERS_TO_UPDATE, status_code=status.HTTP_403_FORBIDDEN
        )
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
        if view is not None:
            return on_the_list(request, state, view, problem=problem, status_code=code)
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
            now, today = accepted_at(state)
            result = state.project_state.undo_hand_in(assignment_id, named, now=now, today=today)
    except UnknownAssignment:
        if view is not None:
            return refused(GONE_FROM_THE_LIST, status.HTTP_404_NOT_FOUND)
        return gone_page(request, state, back, assignment_id)
    except UnknownHandIn:
        return refused(NOT_A_HAND_IN_OF_THIS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    except CouldNotSave:
        logger.exception("her hand-in update on %s could not be undone", assignment_id)
        if view is not None:
            return refused(HAND_IN_NOT_UNDONE, status.HTTP_500_INTERNAL_SERVER_ERROR)
        return could_not(
            request, state, assignment_id, back, HandInCard(problem=HAND_IN_NOT_UNDONE)
        )
    match result:
        case HandInUndone():
            where = (
                after(assignment_id, "undone", back)
                if view is None
                else after_the_list(view, "undone", assignment_id)
            )
            return RedirectResponse(where, status_code=status.HTTP_303_SEE_OTHER)
        case HandInConflict() as conflict:
            return refused(
                ALREADY_UNDONE if already_undone(conflict, named) else HAND_IN_CANNOT_UNDO,
                status.HTTP_409_CONFLICT,
            )
