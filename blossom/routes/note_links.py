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
from blossom.candidates import CandidateReading, readings_for, row_reader
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
    BASIS_AS_WRITTEN,
    HERS,
    NOT_SAVED,
    THEIRS,
    CandidateRow,
    UnlinkRequest,
    Way,
    actor,
    candidate_row,
    details_page,
)
from blossom.routes.student import (
    BAD_FORM,
    NOT_HERS_TO_UPDATE,
    parent_reads,
    templates,
    viewer_of,
)

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

LINK_MAY_BE_ABSENT: Final = frozenset({"from"})
"""Every press the page makes carries its search words and its page, empty or not; only the
homework left is there for a note joined already."""
LINK_FIELDS: Final = LINK_MAY_BE_ABSENT | {"target", "basis", "revision", "q", "page"}
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
    selected: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """The search page: the note's words, where it stands, the query, and the homework the
    words find, twenty to a page, each with the press that joins the note to it. A note in
    homework by a link is moved by that press instead; a note that made its own assignment,
    or one put away, is shown the page with no press on it. Reading the page writes nothing.
    A note or a line that cannot be read is said as unavailable; a name that is no note's
    is said so."""
    store = state.project_state
    viewer = viewer_of(request)
    parent = parent_reads(request)
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
    chosen_row: FoundRow | None = None
    chosen_gone = False
    # One reading for everything the page shows: the note and its line, the note's own
    # link, the page of results, and the homework chosen, read with the same rows whether
    # or not the words still find it, so nothing on the page comes from another reading.
    with store.reading():
        try:
            found_note = store.sound_capture_history(capture_id)
        except UnreadableCapture:
            if form is not None:
                # A refusal of who pressed stands whatever became of the note.
                said = problem if status_code == status.HTTP_403_FORBIDDEN and problem else None
                return plain_search(request, state, form, said or NOTE_UNREADABLE, status_code)
            return unreadable(request, state, status_code)
        if found_note is None:
            if form is not None:
                said = problem if status_code == status.HTTP_403_FORBIDDEN and problem else None
                return plain_search(request, state, form, said or NOTE_GONE, status_code)
            return gone(request, state)
        note = found_note[0]
        joined = note.assignment_id is not None and note.assignment_id != derived_assignment_id(
            note.capture_id
        )
        leaving = note.assignment_id if joined else None
        if leaving is not None:
            current = store.one_assignment(leaving)
        if refused is not None:
            problem = problem or refused
        elif number is None or (not terms and number != 1):
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
        shown = list(results.items) if results is not None else []
        beside = None
        if selected and selected not in {item.assignment_id for item in shown}:
            beside = store.one_assignment(selected)
            chosen_gone = beside is None
        readings = readings_for(store, [*shown, *([beside] if beside is not None else [])])
        rows = [row_of(item, parent=parent) for item in readings[: len(shown)]]
        if beside is not None:
            chosen_row = row_of(readings[-1], parent=parent)
    if selected and problem in (HOMEWORK_CHANGED, HOMEWORK_GONE):
        # The store refused on what it read; the page says the homework chosen as its own
        # reading finds it, gone or standing, so the sentence and the rows agree.
        problem = HOMEWORK_GONE if chosen_gone else HOMEWORK_CHANGED
    own = note.assignment_id is not None and not joined
    may_write = way.open_to(viewer) and not note.archived and not own
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
            "parent": parent,
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
            "chosen": selected or "",
            # What a refused press asked, kept apart from the record and from any fresh press.
            "attempt": form if form is not None and form.unsaved else None,
            "chosen_row": chosen_row,
            "chosen_gone": chosen_gone,
            "chosen_gone_sentence": HOMEWORK_GONE,
            "query_error": refused is not None,
            "not_hers": NOT_HERS_TO_UPDATE,
            "ways_back": ways_back(request),
            "sample": state.settings.sample,
        },
        status_code=status_code,
    )


def row_of(item: CandidateReading, *, parent: bool) -> FoundRow:
    """One result as the page shows it, from the page's one reading, in the reader's voice."""
    return FoundRow(candidate_row(item, parent=parent), candidate_basis([item]))


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
            "ways_back": ways_back(request),
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
            selected=form.target or None,
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
        return plain_search(request, state, form, NOTE_GONE, status.HTTP_404_NOT_FOUND)
    target = one_row(fields, "target")
    # The fingerprint is sixty-four hexadecimal digits as the page writes it; anything
    # else is a form this page never made, not homework that changed.
    basis = fields.get("basis", "") if BASIS_AS_WRITTEN.fullmatch(fields.get("basis", "")) else None
    leaving = one_row(fields, "from") if fields.get("from") else None
    if not whole or form.revision is None or basis is None or (fields.get("from") and not leaving):
        return search_or_plain(
            request, state, way, form, BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    if target is None:
        return search_or_plain(
            request, state, way, form, NO_CHOICE, status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    # The words and the page the press carries are held to the search's own rules before
    # the store is asked, so nothing is written for a context the page would refuse; what
    # was typed is kept as typed for the page that says so.
    try:
        terms_of(form.query)
    except TextRefused:
        return search_or_plain(
            request, state, way, form, QUERY_REFUSED, status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    number = page_number(form.page)
    if number is None or number < 1:
        return search_or_plain(
            request, state, way, form, NO_SUCH_PAGE, status.HTTP_422_UNPROCESSABLE_CONTENT
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
            where = note_href(name, fragment=NOTE_RESULT, said="already", event=head.event_id)
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
            # The page reads the homework chosen again with its own reading and shows it
            # as it stands then, once, with a fresh press.
            return search_or_plain(
                request, state, way, form, HOMEWORK_CHANGED, status.HTTP_409_CONFLICT
            )
    return RedirectResponse(where, status_code=status.HTTP_303_SEE_OTHER)


def unlink_or_plain(
    request: Request,
    state: ApplicationState,
    way: Way,
    form: SearchForm,
    problem: str,
    status_code: int,
) -> HTMLResponse:
    """The details page, where the unlink press lives, with the refusal said first and the
    record read once for what stands now; when that page cannot be made, or the name is no
    note's, the plain page that reads no store, with the homework the form named kept."""
    try:
        name = capture_id_from(form.capture_id)
    except NotACaptureId:
        return plain_search(request, state, form, problem, status_code)
    asked = UnlinkRequest(form.revision, form.leaving) if form.leaving else None
    store = state.project_state
    try:
        with store.reading():
            found = store.sound_capture_history(name)
            if found is None:
                return plain_search(request, state, form, problem, status_code)
            return details_page(
                request,
                state,
                name,
                way,
                problem=problem,
                found=found,
                unlink_request=asked,
                status_code=status_code,
            )
    except UnreadableCapture:
        return plain_search(request, state, form, problem, status_code)
    except Exception:
        logger.exception("the details page could not be read back after a refused unlink")
        return plain_search(request, state, form, problem, status_code)


async def unlink_from_homework(
    request: Request, capture_id: str, state: ApplicationState, way: Way
) -> Response:
    """Unlink the note from the homework the page showed as its link, so it waits again with
    its details kept. A refusal is said on the details page, where the press lives, and on
    the page that reads no store when that one cannot be made."""
    fields, whole = await fields_of(request, UNLINK_FIELDS)
    form = form_of(capture_id, fields)
    # Who pressed is settled first, before the note's name is read, and said where the
    # press lives: on the details page, or on the page that reads no store.
    if not way.open_to(viewer_of(request)):
        return unlink_or_plain(request, state, way, form, way.refusal, status.HTTP_403_FORBIDDEN)
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return plain_search(request, state, form, NOTE_GONE, status.HTTP_404_NOT_FOUND)
    leaving = one_row(fields, "from")
    if not whole or form.revision is None or leaving is None:
        return unlink_or_plain(
            request, state, way, form, BAD_FORM, status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    try:
        async with state.decision_lock:
            now, today = accepted_at(state)
            outcome = state.project_state.unlink_capture(
                name,
                expected_revision=form.revision,
                leaving=leaving,
                authored_by=actor(viewer_of(request)),
                channel=way.channel,
                now=now,
                today=today,
            )
    except UnknownCapture:
        return unlink_or_plain(request, state, way, form, NOT_SAVED, status.HTTP_404_NOT_FOUND)
    except CaptureNotSaved:
        logger.exception("note %s could not be unlinked", name)
        return unlink_or_plain(
            request, state, way, form, NOT_SAVED, status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    match outcome:
        case CaptureUnlinked(event=made):
            where = note_href(name, fragment=NOTE_RESULT, said="unlinked", event=made.event_id)
        case CaptureAlreadyUnlinked(head=head):
            where = note_href(name, fragment=NOTE_RESULT, said="unlinked", event=head.event_id)
        case CaptureConflict():
            return unlink_or_plain(
                request, state, way, form, NOTE_CHANGED, status.HTTP_409_CONFLICT
            )
        case CaptureNotJoined():
            return unlink_or_plain(request, state, way, form, NOT_JOINED, status.HTTP_409_CONFLICT)
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
