"""Finding homework already here, and joining a note to it or correcting the link.

A note can be joined to homework named nothing like it. The search page
finds homework on record by its class or title, however the note puts it,
and shows each result as the candidate rows show homework, with enough to
choose on. Choosing one is a press of its own, held inside the
save's transaction to the note's revision and to the row as it was shown;
the search itself joins nothing. A note joined to homework that was on
record before it can be moved to other homework through the same page, the
old link withdrawn and the new one made together, or unlinked, which puts
it back in Homework notes with its details kept and withdraws only that
note's claims. A note that made its own assignment stays with it.

Both trees have the pages: hers under ``/student``, the family's under
``/parent``. The tree a press came through is the channel and who pressed is
the sign-in, as on the details page. Who pressed is settled before the
note's name is read, so the other person's press is refused with what was
typed kept whether or not the name is a note's.
"""

import logging
from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from blossom.authored_text import TextRefused, single_line
from blossom.candidates import readings_for, row_reader
from blossom.captures import (
    CandidatesChanged,
    CaptureAlreadyPromoted,
    CaptureAlreadyUnlinked,
    CaptureConflict,
    CaptureNotJoined,
    CaptureNotSaved,
    CapturePromoted,
    CaptureUnlinked,
    HomeworkGone,
    NotACaptureId,
    UnknownCapture,
    UnreadableCapture,
    candidate_basis,
    capture_id_from,
    derived_assignment_id,
)
from blossom.dependencies import ApplicationState, get_application_state
from blossom.homework_search import QUERY_MAX_LENGTH, found, page_number, page_of, terms_of
from blossom.routes.captures import (
    NOTE_CHANGED,
    NOTE_GONE,
    NOTE_UNREADABLE,
    gone,
    revision_of,
    unreadable,
    ways_back,
)
from blossom.routes.forms import TOKEN_MAX_LENGTH, fields_of
from blossom.routes.hand_in import accepted_at
from blossom.routes.navigation import NOTE_RESULT, note_href, note_search_href
from blossom.routes.note_details import (
    HERS,
    NOT_SAVED,
    THEIRS,
    CandidateRow,
    Way,
    actor,
    candidate_row,
    details_page,
)
from blossom.routes.student import BAD_FORM, NOT_HERS_TO_UPDATE, templates, viewer_of

logger = logging.getLogger(__name__)


student_router = APIRouter(prefix="/student", tags=["student"])
family_router = APIRouter(prefix="/parent", tags=["parent"])
State = Annotated[ApplicationState, Depends(get_application_state)]

SEARCH_WORDS_NEEDED: Final = "Type a word or two from the class or the title to search."
QUERY_REFUSED: Final = (
    "Search words are one line of up to 200 characters. Nothing was searched, and nothing "
    "was changed."
)
NO_SUCH_PAGE: Final = "There is no such page of results."
NOTHING_FOUND: Final = "Nothing on record matches every word. Try fewer words, or other ones."
NO_CHOICE: Final = "Choose one of the homework found to link this note to it. Nothing was changed."
HOMEWORK_CHANGED: Final = (
    "That homework changed since this page was made. Here it is as it stands now; choose "
    "again if it is still the one. Nothing was changed."
)
HOMEWORK_GONE: Final = "That homework is not on record now. Nothing was changed."
NOT_JOINED: Final = (
    "This note is not joined to homework that was here before it, so there is no link to "
    "change or to unlink. Nothing was changed."
)
OWN_ASSIGNMENT: Final = (
    "This note made an assignment of its own, and stays with it. Its link is the record of that."
)

LINK_MAY_BE_ABSENT: Final = frozenset({"from", "q", "page"})
LINK_FIELDS: Final = LINK_MAY_BE_ABSENT | {"target", "basis", "revision"}
UNLINK_FIELDS: Final = frozenset({"revision", "from"})


@dataclass(frozen=True)
class SearchForm:
    """What a press on the search page sent, every readable value kept, so a refusal shows
    the same thing whatever it refuses for: the search words, the page, the homework chosen,
    and the homework the page showed as the note's own link."""

    capture_id: str
    query: str = ""
    page: str = ""
    target: str = ""
    leaving: str = ""
    revision: int | None = None
    unsaved: bool = False


@dataclass(frozen=True)
class FoundRow:
    """One result as the page shows it: the candidate row, and the fingerprint of that row
    the link press carries, so the save is held to the row as shown."""

    row: CandidateRow
    basis: str


def one_row(fields: dict[str, str], name: str) -> str | None:
    """A field's value as an id these pages wrote, or ``None`` for anything else."""
    given = fields.get(name, "")
    try:
        kept = single_line(given, TOKEN_MAX_LENGTH)
    except TextRefused:
        return None
    return given if kept == given and given else None


def search_page(
    request: Request,
    state: ApplicationState,
    capture_id: str,
    way: Way,
    *,
    query: str = "",
    page: str | None = None,
    form: SearchForm | None = None,
    problem: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """The search page: the note's words, where it stands, the query, and the homework the
    words find, twenty to a page, each with the press that joins the note to it. A note in
    homework by a link is moved by that press instead; a note that made its own assignment,
    or one put away, is shown the page with no press on it. Reading the page writes nothing.
    A note or a line that cannot be read is said as unavailable; a name that is no note's
    is said so."""
    try:
        found_note = state.project_state.sound_capture_history(capture_id)
    except UnreadableCapture:
        if form is not None:
            return plain_search(request, state, form, NOTE_UNREADABLE, status_code)
        return unreadable(request, state, status_code)
    if found_note is None:
        if form is not None:
            return plain_search(request, state, form, NOTE_GONE, status_code)
        return gone(request, state)
    note = found_note[0]
    viewer = viewer_of(request)
    joined = note.assignment_id is not None and note.assignment_id != derived_assignment_id(
        note.capture_id
    )
    own = note.assignment_id is not None and not joined
    may_write = way.open_to(viewer) and not note.archived and not own
    leaving = note.assignment_id if joined else None
    store = state.project_state
    terms: tuple[str, ...] = ()
    refused = None
    try:
        terms = terms_of(query)
    except TextRefused:
        refused = QUERY_REFUSED
    number = page_number(page)
    results = None
    rows: list[FoundRow] = []
    current = None
    with store.reading():
        if leaving is not None:
            current = store.one_assignment(leaving)
        if number is None or (not terms and number != 1):
            problem = problem or NO_SUCH_PAGE
            status_code = (
                status.HTTP_404_NOT_FOUND if status_code == status.HTTP_200_OK else status_code
            )
        elif terms:
            results = page_of(found(store.all_assignments(), terms), number)
            if results is None:
                problem = problem or NO_SUCH_PAGE
                status_code = (
                    status.HTTP_404_NOT_FOUND if status_code == status.HTTP_200_OK else status_code
                )
            else:
                rows = [
                    FoundRow(candidate_row(item, family=way.family), candidate_basis([item]))
                    for item in readings_for(store, results.items)
                ]
    hint = None
    if refused is None and not problem:
        if not terms:
            hint = SEARCH_WORDS_NEEDED
        elif results is not None and results.total == 0:
            hint = NOTHING_FOUND
    page_hrefs = {
        "previous": None
        if results is None or results.previous is None
        else note_search_href(
            note.capture_id, family=way.family, q=query, page=str(results.previous)
        ),
        "next": None
        if results is None or results.next is None
        else note_search_href(note.capture_id, family=way.family, q=query, page=str(results.next)),
    }
    return templates.TemplateResponse(
        request,
        "student_note_search.html",
        {
            "note": note,
            "family": way.family,
            "viewer": viewer,
            "may_write": may_write,
            "joined": joined,
            "own": own,
            "own_sentence": OWN_ASSIGNMENT,
            "current": current,
            "leaving": leaving,
            "query": query,
            "query_max_length": QUERY_MAX_LENGTH,
            "problem": problem or refused,
            "hint": hint,
            "results": results,
            "rows": rows,
            "pages": page_hrefs,
            "chosen": form.target if form is not None else "",
            "not_hers": NOT_HERS_TO_UPDATE,
            "ways_back": ways_back(),
            "sample": state.settings.sample,
        },
        status_code=status_code,
    )


def plain_search(
    request: Request, state: ApplicationState, form: SearchForm, problem: str, status_code: int
) -> HTMLResponse:
    """The page for a refused press whenever the search page cannot be made: the note cannot
    be read, or it is not on record. It reads no store, tries nothing again, and keeps the
    search words and the choice, to copy."""
    try:
        name: str | None = capture_id_from(form.capture_id)
    except NotACaptureId:
        name = None
    return templates.TemplateResponse(
        request,
        "student_update_recovery.html",
        {
            "card": None,
            "hand_in_card": None,
            "heading": "Nothing was saved",
            "note_problem": problem,
            "search_form": form,
            "help_note": name,
            "ways_back": ways_back(),
            "sample": state.settings.sample,
        },
        status_code=status_code,
    )


def search_or_plain(
    request: Request,
    state: ApplicationState,
    way: Way,
    form: SearchForm,
    problem: str,
    status_code: int,
) -> HTMLResponse:
    """The search page with a refused press said first, the search words and the choice kept,
    tried once; when that page cannot be made, or the name is no note's, the plain page that
    reads no store."""
    try:
        capture_id_from(form.capture_id)
    except NotACaptureId:
        return plain_search(request, state, form, problem, status_code)
    try:
        return search_page(
            request,
            state,
            form.capture_id,
            way,
            query=form.query,
            page=form.page or None,
            form=form,
            problem=problem,
            status_code=status_code,
        )
    except Exception:
        logger.exception("the search page could not be read back after a refused press")
        return plain_search(request, state, form, problem, status_code)


def refused_press(
    request: Request, state: ApplicationState, way: Way, form: SearchForm
) -> HTMLResponse | None:
    """The answer to a press through the other person's tree, made before the note's name
    is read, so what was typed is kept whether or not the name is a note's. ``None`` for a
    press by someone the tree is open to."""
    if way.open_to(viewer_of(request)):
        return None
    return search_or_plain(request, state, way, form, way.refusal, status.HTTP_403_FORBIDDEN)


def form_of(capture_id: str, fields: dict[str, str]) -> SearchForm:
    """What a press sent, every readable value kept."""
    return SearchForm(
        capture_id,
        query=fields.get("q", ""),
        page=fields.get("page", ""),
        target=fields.get("target", ""),
        leaving=fields.get("from", ""),
        revision=revision_of(fields),
        unsaved=True,
    )


async def link_to_homework(
    request: Request, capture_id: str, state: ApplicationState, way: Way
) -> Response:
    """Join the note to the homework chosen, or move it there from the homework the page
    showed as its link. The store holds the press to the note's revision and to the row as
    it was shown, inside its own transaction; the page shows what it found again."""
    fields, whole = await fields_of(request, LINK_FIELDS, may_be_absent=LINK_MAY_BE_ABSENT)
    form = form_of(capture_id, fields)
    refused = refused_press(request, state, way, form)
    if refused is not None:
        return refused
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return gone(request, state)
    target = one_row(fields, "target")
    basis = one_row(fields, "basis")
    leaving = one_row(fields, "from") if fields.get("from") else None
    if not whole or form.revision is None or basis is None or (fields.get("from") and not leaving):
        return search_or_plain(
            request, state, way, form, BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    if target is None:
        return search_or_plain(
            request, state, way, form, NO_CHOICE, status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    try:
        async with state.decision_lock:
            now, today = accepted_at(state)
            outcome = state.project_state.link_capture(
                name,
                target=target,
                expected_revision=form.revision,
                basis=basis,
                leaving=leaving,
                shown=row_reader(state.project_state),
                authored_by=actor(viewer_of(request)),
                channel=way.channel,
                now=now,
                today=today,
            )
    except UnknownCapture:
        return search_or_plain(request, state, way, form, NOT_SAVED, status.HTTP_404_NOT_FOUND)
    except CaptureNotSaved:
        logger.exception("note %s could not be joined to homework found", name)
        return search_or_plain(
            request, state, way, form, NOT_SAVED, status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    said = "relinked" if leaving else "joined"
    match outcome:
        case CapturePromoted(event=made):
            where = note_href(name, fragment=NOTE_RESULT, said=said, event=made.event_id)
        case CaptureAlreadyPromoted(head=head):
            where = note_href(name, fragment=NOTE_RESULT, said=said, event=head.event_id)
        case CaptureConflict():
            return search_or_plain(
                request, state, way, form, NOTE_CHANGED, status.HTTP_409_CONFLICT
            )
        case CaptureNotJoined():
            return search_or_plain(request, state, way, form, NOT_JOINED, status.HTTP_409_CONFLICT)
        case HomeworkGone():
            return search_or_plain(
                request, state, way, form, HOMEWORK_GONE, status.HTTP_409_CONFLICT
            )
        case CandidatesChanged():
            return search_or_plain(
                request, state, way, form, HOMEWORK_CHANGED, status.HTTP_409_CONFLICT
            )
    return RedirectResponse(where, status_code=status.HTTP_303_SEE_OTHER)


async def unlink_from_homework(
    request: Request, capture_id: str, state: ApplicationState, way: Way
) -> Response:
    """Unlink the note from the homework the page showed as its link, so it waits again with
    its details kept. A refusal is said on the details page, where the press lives."""
    fields, whole = await fields_of(request, UNLINK_FIELDS)
    form = form_of(capture_id, fields)
    refused = refused_press(request, state, way, form)
    if refused is not None:
        return refused
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return gone(request, state)
    leaving = one_row(fields, "from")
    if not whole or form.revision is None or leaving is None:
        return details_page(
            request,
            state,
            name,
            way,
            problem=BAD_FORM,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    try:
        async with state.decision_lock:
            now, today = accepted_at(state)
            outcome = state.project_state.unlink_capture(
                name,
                expected_revision=form.revision,
                leaving=leaving,
                authored_by=actor(viewer_of(request)),
                now=now,
                today=today,
            )
    except UnknownCapture:
        return details_page(
            request, state, name, way, problem=NOT_SAVED, status_code=status.HTTP_404_NOT_FOUND
        )
    except CaptureNotSaved:
        logger.exception("note %s could not be unlinked", name)
        return details_page(
            request,
            state,
            name,
            way,
            problem=NOT_SAVED,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    match outcome:
        case CaptureUnlinked(event=made):
            where = note_href(name, fragment=NOTE_RESULT, said="unlinked", event=made.event_id)
        case CaptureAlreadyUnlinked(head=head):
            where = note_href(name, fragment=NOTE_RESULT, said="unlinked", event=head.event_id)
        case CaptureConflict():
            return details_page(
                request,
                state,
                name,
                way,
                problem=NOTE_CHANGED,
                status_code=status.HTTP_409_CONFLICT,
            )
        case CaptureNotJoined():
            return details_page(
                request, state, name, way, problem=NOT_JOINED, status_code=status.HTTP_409_CONFLICT
            )
    return RedirectResponse(where, status_code=status.HTTP_303_SEE_OTHER)


def open_search(
    request: Request, capture_id: str, state: ApplicationState, way: Way, q: str, page: str | None
) -> HTMLResponse:
    """Open the search page. It writes nothing."""
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return gone(request, state)
    return search_page(request, state, name, way, query=q, page=page)


@student_router.get(
    "/homework-notes/{capture_id}/search", response_class=HTMLResponse, include_in_schema=False
)
def her_search(
    request: Request, capture_id: str, state: State, q: str = "", page: str | None = None
) -> HTMLResponse:
    """The search page in her tree."""
    return open_search(request, capture_id, state, HERS, q, page)


@student_router.post(
    "/actions/homework-notes/{capture_id}/link",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def her_link(request: Request, capture_id: str, state: State) -> Response:
    """The link press in her tree."""
    return await link_to_homework(request, capture_id, state, HERS)


@student_router.post(
    "/actions/homework-notes/{capture_id}/unlink",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def her_unlink(request: Request, capture_id: str, state: State) -> Response:
    """The unlink press in her tree."""
    return await unlink_from_homework(request, capture_id, state, HERS)


@family_router.get(
    "/homework-notes/{capture_id}/search", response_class=HTMLResponse, include_in_schema=False
)
def family_search(
    request: Request, capture_id: str, state: State, q: str = "", page: str | None = None
) -> HTMLResponse:
    """The search page in the family's tree."""
    return open_search(request, capture_id, state, THEIRS, q, page)


@family_router.post(
    "/actions/homework-notes/{capture_id}/link",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def family_link(request: Request, capture_id: str, state: State) -> Response:
    """The link press in the family's tree."""
    return await link_to_homework(request, capture_id, state, THEIRS)


@family_router.post(
    "/actions/homework-notes/{capture_id}/unlink",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def family_unlink(request: Request, capture_id: str, state: State) -> Response:
    """The unlink press in the family's tree."""
    return await unlink_from_homework(request, capture_id, state, THEIRS)
