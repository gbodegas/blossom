# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Her hand-in update, from an assignment's details: two routes beside her work update's.

Done means she finished her part and turns nothing in, so what she says about
delivery has forms of its own. They are on the assignment's details and
nowhere else, so every result is shown there, at the section the forms sit
in, with the way back the details carried. The routes follow her work
update's: who may save is decided first, from the sign-in and never from the
form, and a parent's press is answered on her current week before the form
is read; for anyone else the form is read whole, the save is one operation
under the decision lock, a success is a redirect to a page that says what
stands, and a refusal is the same section with what she chose and wrote
still in it. A parent reads the section and is told the update is hers to
make. Nothing here asks a model, sends a reminder, or tells the school
anything, and a plan never reads what is saved here.
"""

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, replace
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
    HandInNotOffered,
    HandInSaved,
    HandInState,
    HandInUndone,
    already_undone,
)
from blossom.noticing import read_everything
from blossom.routes.forms import TOKEN_MAX_LENGTH, fields_of, kept_fields_of
from blossom.routes.navigation import (
    RETURN_FIELDS,
    TO_TURN_IN,
    TO_TURN_IN_PAGE,
    WEEK_PAGE,
    ReturnTo,
    address,
    asked_address,
    details_href,
    hand_in_result_anchor,
    read_return,
)
from blossom.routes.student import (
    ALREADY_UNDONE,
    ASSIGNMENT_NOT_SHOWN,
    BAD_FORM,
    BAD_RETURN,
    FROM_A_CARD,
    FROM_DETAILS,
    HER_WEEK_NOT_SHOWN,
    IN_PLACE,
    NOT_HERS_TO_UPDATE,
    PAGE,
    TURN_IN_OVER,
    TURNING_IT_IN,
    UPDATE_NOT_SAVED,
    WITHOUT_THE_PAGE,
    YOUR_WEEK_NOT_SHOWN,
    CardState,
    HandInCard,
    InPlace,
    ListCard,
    NotShown,
    Origin,
    ReturnLink,
    State,
    TurnIn,
    detail_page,
    gone_page,
    hand_in_actions,
    list_card_shown,
    not_hers,
    origin_of,
    parent_reads,
    plain_ways_back,
    receipt_asked,
    sent_in_place,
    showable,
    shown_once,
    student_page,
    templates,
    unavailable_page,
    viewer_of,
)
from blossom.stores.project_state import (
    DONE,
    NOT_YET,
    CouldNotSave,
    UnknownAssignment,
    UnknownHandIn,
)
from blossom.to_turn_in import ON_THE_CARD, Attempt, to_turn_in

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/student", tags=["student"])

STATES: Final = (NEEDS_HAND_IN, TURNED_IN, NOT_REQUIRED, UNKNOWN)
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
    "That form carried a field twice, or a field or a value this list does not send, so "
    "nothing was saved. Press again from the list."
)
LIST_TURN_IN_FAILED: Final = (
    "Reporting it turned in could not be saved, and nothing was changed. Go back and try again."
)
LIST_UNDO_FAILED: Final = (
    "That Undo could not be saved, and nothing was changed. Go back and try again."
)
"""What the plain page says after a write the file refused when the list cannot be read back
either: the press that was tried, by name, and that nothing changed."""
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
CHANGED_WITHOUT_THE_PAGE: Final = "That changed while you were away, so nothing was saved."
NOT_A_HAND_IN_WITHOUT_THE_PAGE: Final = (
    "That form names a hand-in update this assignment does not have, so nothing was saved."
)
HAND_IN_WITHOUT_THE_PAGE: Final[dict[str, str]] = {
    **WITHOUT_THE_PAGE,
    CHOOSE_A_STATE: "Nothing was saved, because no choice about turning it in was made.",
    NEXT_ACTION_TOO_LONG: (
        f"Nothing was saved, because the next step is longer than {NEXT_ACTION_MAX_LENGTH} "
        "characters."
    ),
    NEXT_ACTION_ONE_LINE: "Nothing was saved, because the next step is on more than one line.",
    HAND_IN_NOTE_TOO_LONG: (
        f"Nothing was saved, because the note is longer than {HAND_IN_NOTE_MAX_LENGTH} characters."
    ),
    UNKEPT_CHARACTER: "Nothing was saved, because it has a character Blossom cannot keep.",
    HAND_IN_CHANGED: CHANGED_WITHOUT_THE_PAGE,
    LIST_CHANGED: CHANGED_WITHOUT_THE_PAGE,
    HAND_IN_CANNOT_UNDO: "Your hand-in update has changed, so it cannot be undone from that page.",
    NOT_A_HAND_IN_OF_THIS: NOT_A_HAND_IN_WITHOUT_THE_PAGE,
    LIST_NOT_A_HAND_IN_OF_THIS: NOT_A_HAND_IN_WITHOUT_THE_PAGE,
    LIST_BAD_FORM: (
        "That form carried a field twice, or a field or a value this list does not send, so "
        "nothing was saved."
    ),
    LIST_ALREADY_UNDONE: "That update was already undone.",
}
"""Each refusal of a hand-in press as it reads where the page it was made for is not shown,
her work update's among them: a sentence that points at the page goes, and a rule about a
field says what was not saved and why."""
YOUR_LIST_NOT_SHOWN: Final = "Your To turn in list can't be shown right now."
HER_LIST_NOT_SHOWN: Final = "Her To turn in list can't be shown right now."
LIST_UNAVAILABLE: Final = "{whose} To turn in list can't be shown right now. Try again in a moment."
HAND_IN_FIELDS: Final = (
    frozenset({"state", "next_action", "note", "expected_hand_in_id"}) | RETURN_FIELDS | SHOWN_ON
)
HAND_IN_UNDO_FIELDS: Final = frozenset({"hand_in_id"}) | RETURN_FIELDS | SHOWN_ON
"""The fields each form sends, each once. Anything else, or anything twice, is refused."""
NOTHING_CHOSEN: Final = frozenset({"state"}) | RETURN_FIELDS | SHOWN_ON
"""What a browser may leave out: the radio group when none is chosen, and the way back,
which a form opened with none carries none of."""
LIST_PRESS_FIELDS: Final = (
    frozenset({"state", "next_action", "note", "expected_hand_in_id"}) | SHOWN_ON
)
LIST_UNDO_FIELDS: Final = frozenset({"hand_in_id"}) | SHOWN_ON
"""The two forms the list makes, which are narrower than the details' forms that share
their routes: every field here, no other, and no way back, since a press on the list
comes back to the list. The one press says one thing, turned in with no next step and
no note; a form from the list that says anything else is not one the list made."""


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
    and what the address says a press did is looked up in that same reading,
    which is told the assignment the address names, so her events for it are
    in the batch whether or not it is still on record.
    """
    about = () if card is None or card.asked is None else (card.asked.about,)
    everything = read_everything(state.project_state, state.project_state, also=about)
    still = to_turn_in(everything)
    viewer = viewer_of(request)
    return templates.TemplateResponse(
        request,
        "student_to_turn_in.html",
        {
            "rows": still.rows,
            "unreadable": still.unreadable,
            "list_card": list_card_shown(card, everything, viewer),
            "hand_in_routes": hand_in_actions,
            "viewer": viewer,
            "marks": state.settings.page_marks,
        },
        status_code=status_code,
    )


@router.get("/to-turn-in", response_class=HTMLResponse, include_in_schema=False)
def to_turn_in_list(
    request: Request,
    state: State,
    hand_in_said: str | None = None,
    about: str | None = None,
    hand_in_event: str | None = None,
) -> HTMLResponse:
    """Everything she reports as still to turn in. Reading it changes nothing: no event, no
    reminder, no plan. ``hand_in_said``, ``about``, and ``hand_in_event`` are what a press
    did, to which assignment, and the event that press made, as the server wrote them into
    the address. They are looked up in the page's reading and say nothing unless that
    event is there, in that assignment's history. When the record can't be read, 503: a
    page that says so and offers the same address again, and says nothing of a press."""
    card = receipt_asked(hand_in_said, about, hand_in_event)
    try:
        return list_page(request, state, card=card)
    except sqlite3.Error as error:
        parent = parent_reads(request)
        return unavailable_page(
            request,
            state,
            error,
            heading="To turn in",
            alert=LIST_UNAVAILABLE.format(whose="Her" if parent else "Your"),
            again=asked_address(TO_TURN_IN_PAGE, request.scope["query_string"]),
            ways_back=[ReturnLink(WEEK_PAGE, "Back to her week" if parent else "Back to my week")],
        )


def shown_on(fields: dict[str, str]) -> tuple[str | None, bool]:
    """Where a form says its result is to be shown, and whether it said so in words these
    pages make: nothing for the details, or one of the two places the list is. The value is
    read as it was sent. One with space around it is not one a page wrote, and trimming it
    would let a form made by hand choose which rules it is held to."""
    given = fields.get("hand_in_view", "")
    return (given or None, given in ("", *LIST_VIEWS))


def on_the_list(
    request: Request,
    state: ApplicationState,
    view: str,
    *,
    problem: str,
    status_code: int,
    attempt: Attempt,
) -> HTMLResponse:
    """A refusal shown where the press was made, with the list as it stands now and, beside
    the refusal, the assignment it was about as it stands now, on the list or off it."""
    card = ListCard(problem=ON_THE_LIST.get(problem, problem), attempt=attempt)
    if view == "week":
        return student_page(request, state, turning_in=card, status_code=status_code)
    return list_page(request, state, card=card, status_code=status_code)


def list_refused(
    request: Request,
    state: ApplicationState,
    view: str,
    *,
    problem: str,
    status_code: int,
    attempt: Attempt,
) -> HTMLResponse:
    """A press the list refused, shown where it was made and tried once. When that page
    can't be read, the page that reads no store, with the refusal's status, what the list
    says as it reads without the list, and which page can't be shown."""
    said = ON_THE_LIST.get(problem, problem)
    if view == "week":
        line = HER_WEEK_NOT_SHOWN if parent_reads(request) else YOUR_WEEK_NOT_SHOWN
    else:
        line = HER_LIST_NOT_SHOWN if parent_reads(request) else YOUR_LIST_NOT_SHOWN
    return shown_once(
        request,
        state,
        lambda: on_the_list(
            request, state, view, problem=problem, status_code=status_code, attempt=attempt
        ),
        NotShown(
            UPDATE_NOT_SAVED,
            HAND_IN_WITHOUT_THE_PAGE.get(said, said),
            line,
            list_ways_back(view, attempt.assignment_id),
        ),
        status_code,
    )


def list_ways_back(view: str, assignment_id: str) -> list[ReturnLink]:
    """The ways back from a press on the list when the list can't be shown: where she
    pressed, and the assignment's section on turning it in."""
    on_week = view == "week"
    return [
        ReturnLink(WEEK_PAGE, "Back to my week")
        if on_week
        else ReturnLink(address(TO_TURN_IN_PAGE, fragment=TO_TURN_IN), "Back to To turn in"),
        ReturnLink(
            details_href(
                assignment_id,
                fragment=TURNING_IT_IN,
                return_to="week" if on_week else "to_turn_in",
            ),
            "Open this assignment",
        ),
    ]


def after_the_list(view: str, said: str, assignment_id: str, event_id: str) -> str:
    """Where a press or an undo on the list sends her: back to where she pressed, at the
    place that says what happened, which is there whether or not the row still is. The
    address names the event the save accepted, so the page that answers is about that
    event and no later one."""
    page = WEEK_PAGE if view == "week" else TO_TURN_IN_PAGE
    return address(
        page,
        fragment="to-turn-in-result",
        hand_in_said=said,
        about=assignment_id,
        hand_in_event=event_id,
    )


def could_not_on_the_list(
    request: Request, state: ApplicationState, view: str, attempt: Attempt, problem: str
) -> HTMLResponse:
    """The page after a write the file refused, for a press made on the list: the list with
    the refusal first, tried once. When the list cannot be read back either, the plain
    page her other updates use, made from what the request already held. It reads no
    store, tries nothing again, names the press that failed, and says nothing changed."""
    try:
        return on_the_list(
            request,
            state,
            view,
            problem=problem,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            attempt=attempt,
        )
    except Exception:
        logger.exception("the list could not be read back after a failed hand-in write")
        return templates.TemplateResponse(
            request,
            "student_update_recovery.html",
            {
                "card": None,
                "hand_in_card": HandInCard(
                    problem=LIST_TURN_IN_FAILED
                    if attempt.operation == "turn_in"
                    else LIST_UNDO_FAILED
                ),
                "ways_back": list_ways_back(view, attempt.assignment_id),
                "marks": state.settings.page_marks,
            },
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


def details_refused(
    request: Request,
    state: ApplicationState,
    assignment_id: str,
    back: ReturnTo,
    card: HandInCard,
    status_code: int,
) -> HTMLResponse:
    """A hand-in press the details refused, shown there with what she chose and wrote, tried
    once. When the details can't be read, the page that reads no store, with the refusal's
    status, its words as they read without the details, and what she chose and wrote."""
    said = card.problem or ""
    return shown_once(
        request,
        state,
        lambda: detail_page(
            request, state, assignment_id, back, hand_in=card, status_code=status_code
        ),
        NotShown(
            UPDATE_NOT_SAVED,
            HAND_IN_WITHOUT_THE_PAGE.get(said, said),
            ASSIGNMENT_NOT_SHOWN,
            plain_ways_back(Origin(detail=True, week=None, back=back, valid=True), assignment_id),
            hand_in=card,
        ),
        status_code,
    )


def after(assignment_id: str, said: str, back: ReturnTo) -> str:
    """Where a save or an undo sends her: the details, at the result, with what happened."""
    return details_href(
        assignment_id,
        fragment=hand_in_result_anchor(assignment_id),
        hand_in=said,
        **back.fields(),
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
                "marks": state.settings.page_marks,
            },
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@router.post(
    "/actions/assignments/{assignment_id:path}/hand-in",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def hand_in_from_the_page(request: Request, assignment_id: str, state: State) -> Response:
    """What she says about turning one assignment in, with one next step and a note if she
    wants them.

    A parent signed in is answered 403 on her current week from the sign-in
    and the route alone, before the form is read or the assignment looked up,
    whatever page the form came from, and nothing is written. For anyone
    else the form is read whole. It carries the last hand-in event the page
    showed, which must be one of this assignment's. The same state, action,
    and note as what stands is already saved, with nothing written and no new
    day; a page whose head has moved on is answered 409 with what stands now
    above a form still holding what she meant to save, and nothing is
    overwritten; anything else is appended. A next step left in the form
    beside another state is not kept, and the result says so.
    Words the record will not keep are said beside the field they are in,
    422, with everything else as she left it. Her work update is not read
    and not changed. A form from a card's turn-in press is no form of these:
    it is refused whole, and its note, which is her update's, is never kept
    as a note about turning it in.
    """
    viewer = viewer_of(request)
    if viewer == "parent":
        return not_hers(request, state, UPDATE_NOT_SAVED, NOT_HERS_TO_UPDATE)
    fields, whole = await fields_of(
        request, HAND_IN_FIELDS | CARD_ONLY, may_be_absent=NOTHING_CHOSEN | CARD_ONLY
    )
    from_a_card = bool(fields.keys() & CARD_ONLY)
    whole = whole and not from_a_card
    back, valid = read_return(fields, viewer=viewer, showable=showable)
    view, view_known = shown_on(fields)
    valid = valid and view_known
    view = view if view in LIST_VIEWS else None
    pressed = Attempt("turn_in", assignment_id)
    said = fields.get("state", "").strip()
    action = fields.get("next_action", "")
    note = "" if from_a_card else fields.get("note", "")
    token = fields.get("expected_hand_in_id", "").strip()
    # A recoverable choice is not permission to accept the malformed form.
    chosen = said if said in STATES else None
    step_dropped = said != NEEDS_HAND_IN and action.strip() != ""

    def refused(problem: str, code: int, *, field: str | None = None) -> Response:
        if view is not None:
            return list_refused(
                request, state, view, problem=problem, status_code=code, attempt=pressed
            )
        return details_refused(
            request,
            state,
            assignment_id,
            back,
            HandInCard(
                change=True,
                problem=problem,
                field=field,
                state=chosen,
                next_action=action,
                note=note,
                unsaved=code == status.HTTP_409_CONFLICT,
            ),
            code,
        )

    if not whole:
        return refused(BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if view is not None and (
        fields.keys() != LIST_PRESS_FIELDS
        or fields["state"] != TURNED_IN
        or fields["expected_hand_in_id"] != token
        or action != ""
        or note != ""
    ):
        # The list's one press says one thing, in the values the row wrote. Another state,
        # or words, or a way back, or a value with space around it, from a form that says it
        # is the list's is refused whole: nothing is coerced, trimmed, or dropped.
        return refused(LIST_BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT)
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
                # A row is drawn only over still to turn in, so that is all its press may
                # be written over. The store decides it where it decides the write.
                only_over=NEEDS_HAND_IN if view is not None else None,
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
            return could_not_on_the_list(request, state, view, pressed, HAND_IN_NOT_SAVED)
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
        case HandInSaved(event=made) if view is not None:
            where = after_the_list(view, "turned_in", assignment_id, made.event_id)
        case HandInAlreadySaved(head=stands) if view is not None:
            where = after_the_list(view, "same", assignment_id, stands.event_id)
        case HandInSaved():
            where = after(assignment_id, "saved_without_step" if step_dropped else "saved", back)
        case HandInAlreadySaved():
            where = after(assignment_id, "same_without_step" if step_dropped else "same", back)
        case HandInConflict():
            return refused(HAND_IN_CHANGED, status.HTTP_409_CONFLICT)
        case HandInNotOffered():
            return refused(LIST_BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT)
    return RedirectResponse(where, status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/actions/assignments/{assignment_id:path}/undo-hand-in",
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
    instead. A parent is answered 403 on her current week before the form
    is read or the assignment looked up, and nothing is written.
    """
    viewer = viewer_of(request)
    if viewer == "parent":
        return not_hers(request, state, UPDATE_NOT_SAVED, NOT_HERS_TO_UPDATE)
    fields, whole = await fields_of(
        request, HAND_IN_UNDO_FIELDS, may_be_absent=RETURN_FIELDS | SHOWN_ON
    )
    back, valid = read_return(fields, viewer=viewer, showable=showable)
    view, view_known = shown_on(fields)
    valid = valid and view_known
    view = view if view in LIST_VIEWS else None
    named = fields.get("hand_in_id", "").strip()
    pressed = Attempt(
        "undo", assignment_id, target=named if 0 < len(named) <= TOKEN_MAX_LENGTH else None
    )

    def refused(problem: str, code: int) -> Response:
        if view is not None:
            return list_refused(
                request, state, view, problem=problem, status_code=code, attempt=pressed
            )
        return details_refused(
            request, state, assignment_id, back, HandInCard(problem=problem), code
        )

    if not whole:
        return refused(BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if view is not None and (fields.keys() != LIST_UNDO_FIELDS or fields["hand_in_id"] != named):
        # The list's Undo names the update exactly as the result wrote it.
        return refused(LIST_BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if not valid:
        return refused(BAD_RETURN, status.HTTP_422_UNPROCESSABLE_CONTENT)
    if len(named) > TOKEN_MAX_LENGTH:
        return refused(NOT_A_HAND_IN_OF_THIS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    try:
        async with state.decision_lock:
            now, today = accepted_at(state)
            result = state.project_state.undo_hand_in(
                assignment_id,
                named,
                now=now,
                today=today,
                # The list offers Undo beside her report that it was turned in and nothing
                # else, so that is all an Undo from the list may take back.
                only_over=TURNED_IN if view is not None else None,
            )
    except UnknownAssignment:
        if view is not None:
            return refused(GONE_FROM_THE_LIST, status.HTTP_404_NOT_FOUND)
        return gone_page(request, state, back, assignment_id)
    except UnknownHandIn:
        return refused(NOT_A_HAND_IN_OF_THIS, status.HTTP_422_UNPROCESSABLE_CONTENT)
    except CouldNotSave:
        logger.exception("her hand-in update on %s could not be undone", assignment_id)
        if view is not None:
            return could_not_on_the_list(request, state, view, pressed, HAND_IN_NOT_UNDONE)
        return could_not(
            request, state, assignment_id, back, HandInCard(problem=HAND_IN_NOT_UNDONE)
        )
    match result:
        case HandInUndone(event=made):
            where = (
                after(assignment_id, "undone", back)
                if view is None
                else after_the_list(view, "undone", assignment_id, made.event_id)
            )
            return RedirectResponse(where, status_code=status.HTTP_303_SEE_OTHER)
        case HandInConflict() as conflict:
            return refused(
                ALREADY_UNDONE if already_undone(conflict, named) else HAND_IN_CANNOT_UNDO,
                status.HTTP_409_CONFLICT,
            )
        case HandInNotOffered():
            return refused(LIST_BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT)


# ------------------------------------------------------------- I turned it in, from a card

TURN_IN_HEAD: Final = "hand_in_head"
"""The field I turned it in sends, as the button's own value: the head of her hand-in record
the card showed, blank when it showed none. No other form sends it."""
TURN_IN_UNDO: Final = "undo_hand_in"
"""The field Undo beside a turn-in result sends, as the button's own value: the event that
result named. No other form sends it."""
WORK_WORDS: Final = frozenset({"note", "expected_report_id", "status"})
"""Her update's fields, sent beside a turn-in press when its button joins her update's form:
her note, the update her page showed, and her choice when the whole form is open. They are
shown again unsaved and never saved here; there is no note about turning it in."""
TURN_IN_FIELDS: Final = frozenset({TURN_IN_HEAD, "week", IN_PLACE}) | FROM_DETAILS | WORK_WORDS
TURN_IN_UNDO_FIELDS: Final = frozenset({TURN_IN_UNDO, "week", IN_PLACE}) | FROM_DETAILS | WORK_WORDS
"""The fields each press sends, each once: its button's value, where it came from as her
update's form says it, and her update's fields when its button joins that form."""
TURN_IN_ABSENT: Final = WORK_WORDS | FROM_A_CARD
"""What a turn-in press may leave out: her update's fields, when the button has a form of its
own, and the context of the other page."""
CARD_ONLY: Final = frozenset(
    {TURN_IN_HEAD, TURN_IN_UNDO, "expected_report_id", "status", "report_view", IN_PLACE}
)
"""Fields only a card's or the details' turn-in press sends beside her update's note. A form
here that carries any is refused whole, and its note is not taken for one of these."""
TURN_IN_COPY: Final = frozenset({"note"})
"""What a parent's refused turn-in press may show again of her update: the words of her note,
as that same request typed them."""
TURN_IN_OFFERED: Final[frozenset[HandInState | None]] = frozenset(TURN_IN_OVER)
TURN_IN_BAD_FORM: Final = WITHOUT_THE_PAGE[BAD_FORM]
"""A turn-in press that is not the form a page made: a field twice, one these pages do not
send, or a value they never write. Nothing was saved."""
TURN_IN_FAILED_HEADING: Final = "Turning it in"


@dataclass(frozen=True)
class CardPress:
    """A turn-in press as its form says it: where it came from, the cards her visit keeps in
    place, its button's value, her update's choice and words to show again, and what makes
    it a form these pages did not make, before anything is looked up."""

    assignment_id: str
    origin: Origin
    kept: InPlace
    named: str
    work: CardState | None
    problem: str | None


def work_kept(assignment_id: str, fields: dict[str, str]) -> CardState | None:
    """Her update's choice and words as a turn-in press sent them, to show again unsaved, or
    ``None`` when it sent none or only an empty note. The update her page showed rides along
    as sent, or blank when it is no id these pages write; a blank one meets any update that
    stands as a change made elsewhere when she saves."""
    if "note" not in fields or "expected_report_id" not in fields:
        return None
    note = fields["note"]
    chosen = fields.get("status")
    status_kept = chosen if chosen in (DONE, NOT_YET) else None
    if status_kept is None and not note.strip():
        return None
    token = fields["expected_report_id"]
    carried = token if len(token) <= TOKEN_MAX_LENGTH and token == token.strip() else ""
    return CardState(assignment_id, kept=True, status=status_kept, note=note, token=carried)


def card_press(
    request: Request, assignment_id: str, fields: dict[str, str], whole: bool, button: str
) -> CardPress:
    """Read a turn-in press. Its context is read by her update's own rule, so it comes back
    where her update would. Her update's note and the update it showed come together or not
    at all, and her choice only with them, and only as Done or Not yet, the two values her
    update's form writes."""
    origin = origin_of(request, fields)
    kept = InPlace() if origin.detail else InPlace.read(fields.get(IN_PLACE))
    named = fields.get(button, "")
    paired = ("note" in fields) == ("expected_report_id" in fields) and (
        "status" not in fields or "note" in fields
    )
    chosen = fields.get("status")
    paired = paired and (chosen is None or chosen in (DONE, NOT_YET))
    problem: str | None = None
    if not whole or not paired or named != named.strip() or (button == TURN_IN_UNDO and not named):
        problem = TURN_IN_BAD_FORM
    elif not origin.valid:
        problem = BAD_RETURN
    elif len(named) > TOKEN_MAX_LENGTH:
        problem = NOT_A_HAND_IN_OF_THIS
    return CardPress(assignment_id, origin, kept, named, work_kept(assignment_id, fields), problem)


async def parent_refused(request: Request, state: ApplicationState) -> HTMLResponse:
    """A parent's turn-in press: 403, fixed from the sign-in and the route before the form is
    read, the assignment looked up, or the decision lock taken, and nothing written. The words
    of her note are shown again only as far as ``kept_fields_of`` reads them, within its
    bound, once the refusal is fixed, read-only under the heading of her unsaved update, on a
    page that reads no store and logs none of them; whatever it finds, the answer is the
    same 403."""
    copy = await kept_fields_of(request, TURN_IN_COPY) or {}
    note = copy.get("note", "")
    shown = CardState("", note=note, problem=NOT_HERS_TO_UPDATE) if note.strip() else None
    return await asyncio.to_thread(
        templates.TemplateResponse,
        request,
        "student_update_recovery.html",
        {
            "card": shown,
            "hand_in_card": None,
            "note_problem": NOT_HERS_TO_UPDATE,
            "unsaved_update": True,
            "ways_back": [ReturnLink(PAGE, "Back to her week")],
            "marks": state.settings.page_marks,
        },
        status_code=status.HTTP_403_FORBIDDEN,
    )


def card_page(
    request: Request,
    state: ApplicationState,
    press: CardPress,
    turn_in: TurnIn,
    status_code: int,
) -> HTMLResponse:
    """The page a turn-in press is answered on, made from one reading: the details it came
    from, or her week with the cards her visit keeps in place, her update's choice and words
    in its form, and what the press did or could not do on its card. A refusal on the
    details is said in Turning it in and at the top."""
    if press.origin.detail:
        return detail_page(
            request,
            state,
            press.assignment_id,
            press.origin.back,
            card=press.work,
            hand_in=None if turn_in.problem is None else HandInCard(problem=turn_in.problem),
            status_code=status_code,
            turn_in=turn_in if turn_in.problem is None else None,
        )
    return student_page(
        request,
        state,
        week=press.origin.week,
        card=press.work,
        in_place=press.kept,
        pressed=turn_in.problem is not None,
        status_code=status_code,
        turn_in=turn_in,
    )


def page_line(request: Request, press: CardPress) -> str:
    """Which page can't be shown, in the reader's words."""
    if press.origin.detail:
        return ASSIGNMENT_NOT_SHOWN
    return HER_WEEK_NOT_SHOWN if parent_reads(request) else YOUR_WEEK_NOT_SHOWN


async def refused_on_the_card(
    request: Request, state: ApplicationState, press: CardPress, problem: str, status_code: int
) -> HTMLResponse:
    """A turn-in press refused, shown where it was made with her update's choice and words
    still in its form, tried once, built off the event loop. When that page can't be read,
    the page that reads no store, with the refusal's status, its words as they read without
    the page, and her choice and words headed as her unsaved update."""
    turn_in = TurnIn(press.assignment_id, problem=problem)
    said = HAND_IN_WITHOUT_THE_PAGE.get(problem, problem)
    return await asyncio.to_thread(
        shown_once,
        request,
        state,
        lambda: card_page(request, state, press, turn_in, status_code),
        NotShown(
            UPDATE_NOT_SAVED,
            said,
            page_line(request, press),
            plain_ways_back(press.origin, press.assignment_id, press.kept),
            card=press.work,
            unsaved_update=True,
        ),
        status_code,
    )


def committed_not_shown(
    request: Request, state: ApplicationState, press: CardPress, said: str
) -> HTMLResponse:
    """The page after a turn-in press that was saved when its own page can't be read: what
    was saved, which page can't be shown, her choice and words headed as her unsaved update,
    and the ways back. It reads no store and offers nothing that saves again."""
    work = press.work or CardState(press.assignment_id)
    return templates.TemplateResponse(
        request,
        "student_update_recovery.html",
        {
            "heading": TURN_IN_FAILED_HEADING,
            "card": replace(work, problem=ON_THE_CARD[said]),
            "hand_in_card": None,
            "page_line": page_line(request, press),
            "unsaved_update": True,
            "ways_back": plain_ways_back(press.origin, press.assignment_id, press.kept),
            "marks": state.settings.page_marks,
        },
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def answered_with_words(
    request: Request, state: ApplicationState, press: CardPress, said: str, event_id: str
) -> HTMLResponse:
    """A saved turn-in press that sent her update's words, answered with the page itself so
    her words come back unsaved in its form. The result is said from a reading made after
    the save, which bears it out. When that reading fails, the page that says what was
    saved and which page can't be shown."""
    try:
        return card_page(
            request,
            state,
            press,
            TurnIn(press.assignment_id, said=said, event_id=event_id, direct=True),
            status.HTTP_200_OK,
        )
    except sqlite3.Error as error:
        logger.warning("the page for a turn-in press could not be read: %s", type(error).__name__)
        return committed_not_shown(request, state, press, said)


async def answered_on_the_card(
    request: Request, state: ApplicationState, press: CardPress, said: str, event_id: str
) -> Response:
    """Where a saved turn-in press or its Undo is answered: with her update's words, the page
    itself, built off the event loop; without them, a redirect to where she pressed, at the
    line that says what happened, which names the event the save returned."""
    if press.work is not None:
        return await asyncio.to_thread(answered_with_words, request, state, press, said, event_id)
    name = press.assignment_id
    if press.origin.detail:
        location = details_href(
            name,
            fragment=hand_in_result_anchor(name),
            turn_in_said=said,
            turn_in_event=event_id,
            **press.origin.back.fields(),
        )
        return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)
    location = address(
        PAGE,
        fragment=hand_in_result_anchor(name),
        week=None if press.origin.week is None else press.origin.week.isoformat(),
        turn_in_said=said,
        turn_in_about=name,
        turn_in_event=event_id,
    )
    return sent_in_place(location, press.kept)


async def gone_from_the_card(
    request: Request, state: ApplicationState, press: CardPress
) -> HTMLResponse:
    """A turn-in press on an assignment not on record: the small page, 404, with her
    update's choice and words to copy, headed as unsaved, built off the event loop."""
    origin = press.origin
    return await asyncio.to_thread(
        gone_page,
        request,
        state,
        origin.back if origin.detail else ReturnTo("week", origin.week),
        press.assignment_id,
        card=press.work,
        in_place=press.kept,
        unsaved_update=True,
    )


async def failed_on_the_card(
    request: Request, state: ApplicationState, press: CardPress, problem: str, plain: str
) -> HTMLResponse:
    """A turn-in write the file refused and rolled back: the page with the refusal and her
    words, tried once, built off the event loop; when it can't be read either, the page that
    reads no store names the press and says nothing changed."""
    turn_in = TurnIn(press.assignment_id, problem=problem)
    return await asyncio.to_thread(
        shown_once,
        request,
        state,
        lambda: card_page(request, state, press, turn_in, status.HTTP_500_INTERNAL_SERVER_ERROR),
        NotShown(
            UPDATE_NOT_SAVED,
            plain,
            page_line(request, press),
            plain_ways_back(press.origin, press.assignment_id, press.kept),
            card=press.work,
            unsaved_update=True,
        ),
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


@router.post(
    "/actions/assignments/{assignment_id:path}/turned-in",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def turned_in_from_the_card(request: Request, assignment_id: str, state: State) -> Response:
    """I turned it in, pressed on a card of her week or on an assignment's details: turned in,
    with no next step, no note, and no cue, as her To turn in list's press says it.

    A parent is answered 403 before the form is read (``parent_refused``). For anyone else
    the form is read whole. The save is one operation under the decision lock, over what
    she has said only while that is nothing, not sure, or still to turn in, decided where
    the write is decided: the same report as what stands is already saved, whatever head
    the card held; a head that moved on is a change made elsewhere; any other state is no
    form a card made. Her update is not read and not changed; its choice and words, when
    the button sent them, come back unsaved in its form. Every page this answers with is
    built off the event loop after the decision lock is released, so no page adds a wait
    on the loop; the write itself waits on the loop as every press's does.
    """
    if viewer_of(request) == "parent":
        return await parent_refused(request, state)
    fields, whole = await fields_of(request, TURN_IN_FIELDS, may_be_absent=TURN_IN_ABSENT)
    press = card_press(request, assignment_id, fields, whole, TURN_IN_HEAD)
    if press.problem is not None:
        return await refused_on_the_card(
            request, state, press, press.problem, status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    try:
        async with state.decision_lock:
            now, today = accepted_at(state)
            result = state.project_state.record_hand_in(
                assignment_id,
                TURNED_IN,
                "",
                "",
                expected_head=press.named or None,
                now=now,
                today=today,
                offered_over=TURN_IN_OFFERED,
            )
    except UnknownAssignment:
        return await gone_from_the_card(request, state, press)
    except UnknownHandIn:
        return await refused_on_the_card(
            request, state, press, NOT_A_HAND_IN_OF_THIS, status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    except CouldNotSave:
        logger.exception("her turn-in press on %s could not be saved", assignment_id)
        return await failed_on_the_card(request, state, press, LIST_NOT_SAVED, LIST_TURN_IN_FAILED)
    match result:
        case HandInSaved(event=made):
            return await answered_on_the_card(request, state, press, "turned_in", made.event_id)
        case HandInAlreadySaved(head=stands):
            return await answered_on_the_card(request, state, press, "same", stands.event_id)
        case HandInConflict():
            return await refused_on_the_card(
                request, state, press, CHANGED_WITHOUT_THE_PAGE, status.HTTP_409_CONFLICT
            )
        case HandInNotOffered():
            return await refused_on_the_card(
                request, state, press, TURN_IN_BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT
            )


@router.post(
    "/actions/assignments/{assignment_id:path}/undo-turned-in",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def undo_turned_in_from_the_card(
    request: Request, assignment_id: str, state: State
) -> Response:
    """Undo beside a turn-in result: take back the very report that result named, restoring
    what stood before it, while it is her report that it was turned in and the latest event.

    Anything else is 409 and writes nothing: the page says it was already undone only when
    the latest event is the undo of that very report, and that it changed otherwise. It is
    never aimed at a newer event. A parent is answered as for the press. Her update's choice
    and words come back unsaved as they do for the press, and every page is built off the
    event loop.
    """
    if viewer_of(request) == "parent":
        return await parent_refused(request, state)
    fields, whole = await fields_of(request, TURN_IN_UNDO_FIELDS, may_be_absent=TURN_IN_ABSENT)
    press = card_press(request, assignment_id, fields, whole, TURN_IN_UNDO)
    if press.problem is not None:
        return await refused_on_the_card(
            request, state, press, press.problem, status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    try:
        async with state.decision_lock:
            now, today = accepted_at(state)
            result = state.project_state.undo_hand_in(
                assignment_id, press.named, now=now, today=today, only_over=TURNED_IN
            )
    except UnknownAssignment:
        return await gone_from_the_card(request, state, press)
    except UnknownHandIn:
        return await refused_on_the_card(
            request, state, press, NOT_A_HAND_IN_OF_THIS, status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    except CouldNotSave:
        logger.exception("her turn-in press on %s could not be undone", assignment_id)
        return await failed_on_the_card(request, state, press, HAND_IN_NOT_UNDONE, LIST_UNDO_FAILED)
    match result:
        case HandInUndone(event=made):
            return await answered_on_the_card(request, state, press, "undone", made.event_id)
        case HandInConflict() as conflict:
            return await refused_on_the_card(
                request,
                state,
                press,
                ALREADY_UNDONE if already_undone(conflict, press.named) else HAND_IN_CANNOT_UNDO,
                status.HTTP_409_CONFLICT,
            )
        case HandInNotOffered():
            return await refused_on_the_card(
                request, state, press, TURN_IN_BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT
            )
