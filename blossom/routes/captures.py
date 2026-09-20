"""Her homework notes, through pages: a first save that asks for words and nothing else.

A note is written on a page of its own, reached from her week. The form is
given a random id when it is made, which writes nothing, and sends it back, so
the same form sent twice is one note and a lost answer loses nothing. A class
and a day are optional and folded away. A day that cannot be read does not
take the note with it and is never dropped without her say: the form comes
back whole, and the next save needs a day that reads or her explicit choice
to save without one.

The routes follow her other forms: the form is read whole before anything
else, who may write is decided from the sign-in and never from the form, the
write is one operation under the decision lock, a success is a redirect to a
page that says what stands, and a refusal keeps every readable word. A parent
reads every note, archived ones and history included, and changes none. A
result names the revision the save made or found, and the page that answers
looks that revision up in the note's own history: while it is the latest the
result is what stands, and once something newer follows it is said as
something done earlier, on a page that shows the note as it stands.

Nothing here makes an assignment, asks a model, or touches what a plan is made
from. Turning a note into homework is a later step with routes of its own, and
no page here offers it or suggests it can be done.
"""

import logging
from dataclasses import dataclass, replace
from datetime import date
from typing import Final

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from blossom.authored_text import TextRefused, multiline, single_line
from blossom.captures import (
    ARCHIVE,
    CAPTURE_COURSE_MAX_LENGTH,
    CAPTURE_TEXT_MAX_LENGTH,
    CREATE,
    EDIT,
    HOUSEHOLD,
    RESTORE,
    STUDENT,
    Author,
    Capture,
    CaptureAlreadyCreated,
    CaptureChanged,
    CaptureConflict,
    CaptureCreated,
    CaptureEvent,
    CaptureIdTaken,
    CaptureNotSaved,
    CaptureUnchanged,
    NotACaptureId,
    UnknownCapture,
    UnreadableCapture,
    capture_id_from,
    new_capture_id,
)
from blossom.dependencies import ApplicationState
from blossom.reconciliation import SourceChannel
from blossom.routes.forms import TOKEN_MAX_LENGTH, fields_of
from blossom.routes.hand_in import accepted_at
from blossom.routes.navigation import (
    ARCHIVED_NOTES_PAGE,
    NEW_NOTE_PAGE,
    NOTE_RESULT,
    NOTES_PAGE,
    WEEK_PAGE,
    note_href,
)
from blossom.routes.student import (
    BAD_FORM,
    NOT_HERS_TO_UPDATE,
    ReturnLink,
    State,
    templates,
    viewer_of,
)
from blossom.stores.help_requests import NOTE_MAX_LENGTH, UnknownCaptureReference

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/student", tags=["student"])

NOTE_SAVED: Final = "Homework note saved. It is not in a plan yet."
NOTE_ALREADY_SAVED: Final = "Already saved. This is the note as it stands now."
NOTE_EDITED: Final = "Your changes are saved. It is not in a plan yet."
NOTE_ARCHIVED: Final = "This note is put away. You can bring it back."
NOTE_RESTORED: Final = "This note is back on your list. It is not in a plan yet."
NOTE_ASKED: Final = "You asked for help about this note. Your parents can see the request."
NOTE_SAVED_EARLIER: Final = (
    "You saved this earlier, and the note has changed since. This page shows it as it stands now."
)
NOTE_ID_TAKEN: Final = (
    "This form already saved another note, so these words were not saved. Both are below; "
    "save these as a new note if you want to keep them."
)
NOTE_CHANGED: Final = (
    "This note changed while you were away, so nothing was saved. This page shows it as it "
    "stands now."
)
NOTE_DATE_UNREADABLE: Final = (
    "That date could not be read, so nothing was saved yet. Pick the date again, or save "
    "without the date."
)
NOTE_NEEDS_A_DATE_CHOICE: Final = (
    "You gave a date that could not be read. Pick a date, or choose Save without the date."
)
NOTE_NEEDS_WORDS: Final = "Write what you need to remember."
NOTE_TOO_LONG: Final = f"Keep your note to {CAPTURE_TEXT_MAX_LENGTH} characters or fewer."
COURSE_TOO_LONG: Final = f"Keep the class to {CAPTURE_COURSE_MAX_LENGTH} characters or fewer."
COURSE_ONE_LINE: Final = "Keep the class on one line."
UNKEPT_CHARACTER: Final = (
    "That has a character Blossom cannot keep. Take it out and save again; the rest is as you "
    "wrote it."
)
QUESTION_TOO_LONG: Final = f"Keep your question to {NOTE_MAX_LENGTH} characters or fewer."
NOTE_NOT_SAVED: Final = "Your note could not be saved. Your words are still here. Try again."
NOTE_NOT_MOVED: Final = "That could not be saved, and nothing was changed. Try again."
HELP_NOT_ASKED: Final = "Your request could not be sent, and nothing was changed. Try again."
NOTE_GONE: Final = "This homework note is not on record."
NOTE_UNREADABLE: Final = "This homework note cannot be read right now. Nothing was changed."
WITHOUT_DATE: Final = "without_date"

CREATE_FIELDS: Final = frozenset(
    {"capture_id", "text", "course", "due_date", "date_pending", "choice"}
)
EDIT_FIELDS: Final = frozenset({"revision", "text", "course", "due_date", "date_pending", "choice"})
MOVE_FIELDS: Final = frozenset({"revision"})
HELP_FIELDS: Final = frozenset({"note"})
PRESSED_OR_PENDING: Final = frozenset({"date_pending", "choice"})
"""What a browser leaves out of a form these pages made: the mark that a day was refused,
which only a refused form carries, and the name of the button pressed, which a form sent
with the Enter key does not carry."""

SAID: Final[dict[str, tuple[str, str | None]]] = {
    "saved": (NOTE_SAVED, CREATE),
    "same": (NOTE_ALREADY_SAVED, None),
    "edited": (NOTE_EDITED, EDIT),
    "unchanged": (NOTE_ALREADY_SAVED, None),
    "archived": (NOTE_ARCHIVED, ARCHIVE),
    "restored": (NOTE_RESTORED, RESTORE),
}
"""What an address says a save did, the sentence for it, and the kind of change its
revision must be in the note's history. The server writes the address; the page believes
none of it until the history bears it out."""


@dataclass(frozen=True)
class NoteForm:
    """What a note's form holds when it is shown: what she typed, as she typed it, and what
    the page says about it. Nothing here is what is stored."""

    capture_id: str
    text: str = ""
    course: str = ""
    due_date: str = ""
    """The day as a native control can hold it: a day that reads, or nothing."""
    date_refused: str | None = None
    """The day as she sent it, when it could not be read; shown escaped beside the field."""
    date_pending: bool = False
    revision: int | None = None
    """The revision an edit was opened on; ``None`` for a first save."""
    problem: str | None = None
    field: str | None = None
    unsaved: bool = False

    @property
    def details_open(self) -> bool:
        """The optional fields stay folded unless one is in use or needs correcting."""
        return bool(self.course or self.due_date or self.date_pending or self.field in DETAILS)


DETAILS: Final = ("course", "due_date")


@dataclass(frozen=True)
class NoteResult:
    """What a save did, as a note's page says it: the sentence, and whether what it made is
    still the latest."""

    said: str
    stands: bool


def author_of(viewer: str) -> Author:
    """Who a change is recorded as made by: the student when she is signed in, and the
    household when the sign-in is off and a page cannot say which person pressed."""
    return STUDENT if viewer == "student" else HOUSEHOLD


def refusal_for(refusal: TextRefused, *, course: bool) -> str:
    """The sentence for words the record will not keep, by the rule they met."""
    if refusal.reason == "too_long":
        return COURSE_TOO_LONG if course else NOTE_TOO_LONG
    if refusal.reason == "line_break":
        return COURSE_ONE_LINE
    return UNKEPT_CHARACTER


def checked(form: NoteForm, fields: dict[str, str]) -> tuple[NoteForm, date | None]:
    """Hold what she typed to the rules, field by field, and read the day.

    Returns the form with its problem set when there is one. A day that
    cannot be read is kept as she sent it and marks the form, so the next
    save cannot drop it without her choosing to. A form so marked that
    arrives with no day needs that choice.
    """
    try:
        if multiline(form.text, CAPTURE_TEXT_MAX_LENGTH) is None:
            return replace(form, problem=NOTE_NEEDS_WORDS, field="text"), None
    except TextRefused as refusal:
        return replace(form, problem=refusal_for(refusal, course=False), field="text"), None
    try:
        single_line(form.course, CAPTURE_COURSE_MAX_LENGTH)
    except TextRefused as refusal:
        return replace(form, problem=refusal_for(refusal, course=True), field="course"), None
    raw = fields.get("due_date", "").strip()
    if raw:
        try:
            return replace(form, due_date=raw, date_pending=False), date.fromisoformat(raw)
        except ValueError:
            return (
                replace(
                    form,
                    due_date="",
                    date_refused=raw[:TOKEN_MAX_LENGTH],
                    date_pending=True,
                    problem=NOTE_DATE_UNREADABLE,
                    field="due_date",
                ),
                None,
            )
    if form.date_pending and fields.get("choice") != WITHOUT_DATE:
        return replace(form, problem=NOTE_NEEDS_A_DATE_CHOICE, field="due_date"), None
    return replace(form, date_pending=False), None


def form_from(fields: dict[str, str], capture_id: str, revision: int | None = None) -> NoteForm:
    """The form as she sent it, every readable word kept."""
    return NoteForm(
        capture_id=capture_id,
        text=fields.get("text", ""),
        course=fields.get("course", ""),
        date_pending=fields.get("date_pending") == "1",
        revision=revision,
    )


def ways_back() -> list[ReturnLink]:
    """The two ways on from a note's pages, fixed addresses of this site: her notes, and
    her week."""
    return [
        ReturnLink(NOTES_PAGE, "Back to Homework notes"),
        ReturnLink(WEEK_PAGE, "Back to my week"),
    ]


def new_note_page(
    request: Request,
    state: ApplicationState,
    form: NoteForm,
    *,
    taken: Capture | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """The page a note is first written on. It reads no store, so it can always be shown."""
    return templates.TemplateResponse(
        request,
        "student_note_new.html",
        {
            "form": form,
            "taken": taken,
            "viewer": viewer_of(request),
            "not_hers": NOT_HERS_TO_UPDATE,
            "text_max_length": CAPTURE_TEXT_MAX_LENGTH,
            "course_max_length": CAPTURE_COURSE_MAX_LENGTH,
            "sample": state.settings.sample,
        },
        status_code=status_code,
    )


def gone(request: Request, state: ApplicationState) -> HTMLResponse:
    """The small page for a name that is no note of this record."""
    return templates.TemplateResponse(
        request,
        "student_note_gone.html",
        {"problem": NOTE_GONE, "ways_back": ways_back(), "sample": state.settings.sample},
        status_code=status.HTTP_404_NOT_FOUND,
    )


def plain_failure(
    request: Request, state: ApplicationState, problem: str, form: NoteForm | None
) -> HTMLResponse:
    """The page after a write the file refused when the note's page cannot be read back
    either. It reads no store, tries nothing again, and keeps her words as she sent them."""
    return templates.TemplateResponse(
        request,
        "student_update_recovery.html",
        {
            "card": None,
            "hand_in_card": None,
            "note_problem": problem,
            "note_form": form,
            "ways_back": ways_back(),
            "sample": state.settings.sample,
        },
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def result_of(
    note: Capture, history: list[CaptureEvent], said: str | None, rev: str | None
) -> NoteResult | None:
    """The result an address names, when the note's own history bears it out."""
    if said not in SAID or rev is None or not rev.isdecimal() or len(rev) > 9:
        return None
    sentence, kind = SAID[said]
    made = next((event for event in history if event.revision == int(rev)), None)
    if made is None or (kind is not None and made.operation != kind):
        return None
    stands = note.revision == made.revision
    return NoteResult(sentence if stands else NOTE_SAVED_EARLIER, stands)


def note_page(
    request: Request,
    state: ApplicationState,
    capture_id: str,
    *,
    form: NoteForm | None = None,
    problem: str | None = None,
    said: str | None = None,
    rev: str | None = None,
    asked: str | None = None,
    edit: bool = False,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """One note's page: what stands, the first words when they differ, who supplied a class
    or a day, the history, and for her the ways to change it. Two reads in one snapshot."""
    store = state.project_state
    try:
        with store.reading():
            note = store.capture(capture_id)
            history = [] if note is None else store.capture_history(capture_id)
    except UnreadableCapture:
        return templates.TemplateResponse(
            request,
            "student_note_gone.html",
            {"problem": NOTE_UNREADABLE, "ways_back": ways_back(), "sample": state.settings.sample},
            status_code=status_code if status_code != status.HTTP_200_OK else status.HTTP_200_OK,
        )
    if note is None:
        return gone(request, state)
    viewer = viewer_of(request)
    mine = viewer != "parent"
    result = result_of(note, history, said, rev) if mine else None
    if mine and result is None and asked:
        request_made = state.help_requests.get(asked[:TOKEN_MAX_LENGTH])
        if request_made is not None and request_made.capture_id == note.capture_id:
            result = NoteResult(NOTE_ASKED, stands=True)
    if form is None and edit and mine and not note.archived:
        form = NoteForm(
            capture_id=note.capture_id,
            text=note.text,
            course=note.course or "",
            due_date="" if note.due_date is None else note.due_date.isoformat(),
            revision=note.revision,
        )
    return templates.TemplateResponse(
        request,
        "student_note.html",
        {
            "note": note,
            "history": history,
            "form": form,
            "problem": problem,
            "result": result,
            "viewer": viewer,
            "mine": mine,
            "ways_back": ways_back(),
            "text_max_length": CAPTURE_TEXT_MAX_LENGTH,
            "course_max_length": CAPTURE_COURSE_MAX_LENGTH,
            "sample": state.settings.sample,
        },
        status_code=status_code,
    )


def note_or_plain(
    request: Request,
    state: ApplicationState,
    capture_id: str,
    problem: str,
    form: NoteForm | None,
) -> HTMLResponse:
    """The note's page with a refused write said first, tried once; when that page cannot be
    read back either, the plain page that reads no store."""
    try:
        return note_page(
            request,
            state,
            capture_id,
            form=form,
            problem=problem,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    except Exception:
        logger.exception("a note's page could not be read back after a failed write")
        return plain_failure(request, state, problem, form)


# ------------------------------------------------------------------ the pages


@router.get("/homework-notes/new", response_class=HTMLResponse, include_in_schema=False)
def new_note(request: Request, state: State) -> HTMLResponse:
    """The form for a new note, with an id of its own. Making it writes nothing."""
    return new_note_page(request, state, NoteForm(capture_id=new_capture_id()))


def notes_list(request: Request, state: ApplicationState, *, archived: bool) -> HTMLResponse:
    """One of her two lists of notes, each a single read: the ones still waiting, or the ones
    she put away."""
    store = state.project_state
    read = store.archived_captures() if archived else store.outstanding_captures()
    return templates.TemplateResponse(
        request,
        "student_notes.html",
        {
            "notes": read.notes,
            "unreadable": read.unreadable,
            "archived": archived,
            "viewer": viewer_of(request),
            "new_note_page": NEW_NOTE_PAGE,
            "notes_page": NOTES_PAGE,
            "archived_notes_page": ARCHIVED_NOTES_PAGE,
            "sample": state.settings.sample,
        },
    )


@router.get("/homework-notes", response_class=HTMLResponse, include_in_schema=False)
def homework_notes(request: Request, state: State) -> HTMLResponse:
    """Every note still to do something about, the first saved first. One read."""
    return notes_list(request, state, archived=False)


@router.get("/homework-notes/archived", response_class=HTMLResponse, include_in_schema=False)
def archived_notes(request: Request, state: State) -> HTMLResponse:
    """Every note she put away, kept with its history. One read."""
    return notes_list(request, state, archived=True)


@router.get("/homework-notes/{capture_id}", response_class=HTMLResponse, include_in_schema=False)
def one_note(
    request: Request,
    capture_id: str,
    state: State,
    said: str | None = None,
    rev: str | None = None,
    asked: str | None = None,
    edit: str | None = None,
) -> HTMLResponse:
    """One note. ``said`` and ``rev`` are what a save did and the revision it made or found,
    looked up in the note's history; ``edit`` opens the form; nothing here writes."""
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return gone(request, state)
    return note_page(request, state, name, said=said, rev=rev, asked=asked, edit=edit == "1")


@router.get(
    "/homework-notes/{capture_id}/help", response_class=HTMLResponse, include_in_schema=False
)
def help_about_a_note(request: Request, capture_id: str, state: State) -> HTMLResponse:
    """The page that offers to ask for help about one note. Opening it sends nothing."""
    try:
        name = capture_id_from(capture_id)
        note = state.project_state.capture(name)
    except (NotACaptureId, UnreadableCapture):
        return gone(request, state)
    if note is None:
        return gone(request, state)
    return help_page(request, state, note)


def help_page(
    request: Request,
    state: ApplicationState,
    note: Capture,
    *,
    question: str = "",
    problem: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """The page that offers the request, with the note as context and her question as she
    typed it. Rendering it sends nothing."""
    return templates.TemplateResponse(
        request,
        "student_note_help.html",
        {
            "note": note,
            "question": question,
            "problem": problem,
            "viewer": viewer_of(request),
            "not_hers": NOT_HERS_TO_UPDATE,
            "note_max_length": NOTE_MAX_LENGTH,
            "sample": state.settings.sample,
        },
        status_code=status_code,
    )


# ------------------------------------------------------------------ the writes


@router.post("/actions/homework-notes", response_class=HTMLResponse, include_in_schema=False)
async def save_a_new_note(request: Request, state: State) -> Response:
    """Save a note for the first time, once.

    The same form again is the same note, answered with the note as it
    stands now. The same id with anything else in it is refused with both
    shown, and her words come back in a form with an id of its own, to save
    as another note if she wants them. A parent is answered 403.
    """
    fields, whole = await fields_of(request, CREATE_FIELDS, may_be_absent=PRESSED_OR_PENDING)
    viewer = viewer_of(request)
    try:
        name = capture_id_from(fields.get("capture_id", ""))
    except NotACaptureId:
        name, whole = new_capture_id(), False
    form = form_from(fields, name)
    if viewer == "parent":
        return new_note_page(
            request,
            state,
            replace(form, problem=NOT_HERS_TO_UPDATE),
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if not whole:
        return new_note_page(
            request,
            state,
            replace(form, problem=BAD_FORM),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    form, due = checked(form, fields)
    if form.problem is not None:
        return new_note_page(
            request, state, form, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    try:
        async with state.decision_lock:
            now, today = accepted_at(state)
            outcome = state.project_state.create_capture(
                name,
                form.text,
                form.course,
                due,
                authored_by=author_of(viewer),
                channel=SourceChannel.STUDENT_REPORT,
                now=now,
                today=today,
            )
    except CaptureNotSaved:
        logger.exception("her homework note %s could not be saved", name)
        return new_note_page(
            request,
            state,
            replace(form, problem=NOTE_NOT_SAVED),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    match outcome:
        case CaptureCreated(capture=note):
            where = note_href(name, fragment=NOTE_RESULT, said="saved", rev=str(note.revision))
        case CaptureAlreadyCreated(capture=note):
            where = note_href(name, fragment=NOTE_RESULT, said="same", rev=str(note.revision))
        case CaptureIdTaken(capture=note):
            return new_note_page(
                request,
                state,
                replace(form, capture_id=new_capture_id(), problem=NOTE_ID_TAKEN, unsaved=True),
                taken=note,
                status_code=status.HTTP_409_CONFLICT,
            )
    return RedirectResponse(where, status_code=status.HTTP_303_SEE_OTHER)


def revision_of(fields: dict[str, str]) -> int | None:
    """The revision a form says its page showed, or ``None`` for anything that is no count."""
    given = fields.get("revision", "")
    return int(given) if given.isdecimal() and len(given) <= 9 else None


@router.post(
    "/actions/homework-notes/{capture_id}/edit",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def edit_a_note(request: Request, capture_id: str, state: State) -> Response:
    """Change what stands on a note, from the revision the page showed.

    The same words, class, and day as stand now are already saved. A page
    that is behind is answered 409 with the note as it stands and her words
    kept in a form that names the newer revision, to save again if she
    still wants them; nothing is overwritten and an archived note is not
    brought back. A parent is answered 403.
    """
    fields, whole = await fields_of(request, EDIT_FIELDS, may_be_absent=PRESSED_OR_PENDING)
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return gone(request, state)
    viewer = viewer_of(request)
    if viewer == "parent":
        return note_page(
            request,
            state,
            name,
            problem=NOT_HERS_TO_UPDATE,
            status_code=status.HTTP_403_FORBIDDEN,
        )
    revision = revision_of(fields)
    form = form_from(fields, name, revision)
    if not whole or revision is None:
        return note_page(
            request,
            state,
            name,
            form=replace(form, revision=revision or 0),
            problem=BAD_FORM,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    form, due = checked(form, fields)
    if form.problem is not None:
        return note_page(
            request,
            state,
            name,
            form=form,
            problem=form.problem,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    try:
        async with state.decision_lock:
            now, today = accepted_at(state)
            outcome = state.project_state.edit_capture(
                name,
                form.text,
                form.course,
                due,
                expected_revision=revision,
                authored_by=author_of(viewer),
                channel=SourceChannel.STUDENT_REPORT,
                now=now,
                today=today,
            )
    except UnknownCapture:
        return gone(request, state)
    except CaptureNotSaved:
        logger.exception("her homework note %s could not be changed", name)
        return note_or_plain(request, state, name, NOTE_NOT_SAVED, form)
    match outcome:
        case CaptureChanged(capture=note):
            where = note_href(name, fragment=NOTE_RESULT, said="edited", rev=str(note.revision))
        case CaptureUnchanged(capture=note):
            where = note_href(name, fragment=NOTE_RESULT, said="unchanged", rev=str(note.revision))
        case CaptureConflict(capture=note):
            return note_page(
                request,
                state,
                name,
                form=replace(form, revision=note.revision, unsaved=True),
                problem=NOTE_CHANGED,
                status_code=status.HTTP_409_CONFLICT,
            )
    return RedirectResponse(where, status_code=status.HTTP_303_SEE_OTHER)


async def move_a_note(
    request: Request, capture_id: str, state: ApplicationState, *, archive: bool
) -> Response:
    """Archive or restore, from the revision the page showed. Already where it was asked to
    be is already done; a page that is behind is answered 409 with the note as it stands,
    whose own button names the newer revision."""
    fields, whole = await fields_of(request, MOVE_FIELDS)
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return gone(request, state)
    viewer = viewer_of(request)
    if viewer == "parent":
        return note_page(
            request,
            state,
            name,
            problem=NOT_HERS_TO_UPDATE,
            status_code=status.HTTP_403_FORBIDDEN,
        )
    revision = revision_of(fields)
    if not whole or revision is None:
        return note_page(
            request,
            state,
            name,
            problem=BAD_FORM,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    store = state.project_state
    try:
        async with state.decision_lock:
            now, today = accepted_at(state)
            move = store.archive_capture if archive else store.restore_capture
            outcome = move(
                name,
                expected_revision=revision,
                authored_by=author_of(viewer),
                now=now,
                today=today,
            )
    except UnknownCapture:
        return gone(request, state)
    except CaptureNotSaved:
        logger.exception("her homework note %s could not be moved", name)
        return note_or_plain(request, state, name, NOTE_NOT_MOVED, None)
    match outcome:
        case CaptureChanged(capture=note):
            said = "archived" if archive else "restored"
            where = note_href(name, fragment=NOTE_RESULT, said=said, rev=str(note.revision))
        case CaptureUnchanged(capture=note):
            where = note_href(name, fragment=NOTE_RESULT, said="unchanged", rev=str(note.revision))
        case CaptureConflict():
            return note_page(
                request,
                state,
                name,
                problem=NOTE_CHANGED,
                status_code=status.HTTP_409_CONFLICT,
            )
    return RedirectResponse(where, status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/actions/homework-notes/{capture_id}/archive",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def archive_a_note(request: Request, capture_id: str, state: State) -> Response:
    """Put a note away. It is kept with its history and can be brought back."""
    return await move_a_note(request, capture_id, state, archive=True)


@router.post(
    "/actions/homework-notes/{capture_id}/restore",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def restore_a_note(request: Request, capture_id: str, state: State) -> Response:
    """Bring a note back to the place it had. Nothing is made of it."""
    return await move_a_note(request, capture_id, state, archive=False)


@router.post(
    "/actions/homework-notes/{capture_id}/ask-for-help",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def ask_for_help_about_a_note(request: Request, capture_id: str, state: State) -> Response:
    """Ask for help about one note, with a question if she wants one.

    Only this, an explicit submit from her, makes a request. It carries the
    note's id and her question, never the note's words. The name is checked
    again where the request is written, in the help store's own transaction.
    A request the file refuses is said on the page she sent it from, made from
    the note already read, so it reads no store again and keeps her question.
    A parent is answered 403 and nothing is sent in her name.
    """
    fields, whole = await fields_of(request, HELP_FIELDS)
    try:
        name = capture_id_from(capture_id)
        note = state.project_state.capture(name)
    except (NotACaptureId, UnreadableCapture):
        return gone(request, state)
    if note is None:
        return gone(request, state)
    question = fields.get("note", "")
    if viewer_of(request) == "parent":
        return help_page(
            request,
            state,
            note,
            question=question,
            problem=NOT_HERS_TO_UPDATE,
            status_code=status.HTTP_403_FORBIDDEN,
        )
    words = question.strip()
    if not whole or len(words) > NOTE_MAX_LENGTH:
        return help_page(
            request,
            state,
            note,
            question=question,
            problem=QUESTION_TOO_LONG if whole else BAD_FORM,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    try:
        async with state.decision_lock:
            asked = state.help_requests.ask(state.clock.today(), words or None, capture_id=name)
    except UnknownCaptureReference:
        return gone(request, state)
    except Exception:
        logger.exception("her request for help about note %s could not be sent", name)
        return help_page(
            request,
            state,
            note,
            question=question,
            problem=HELP_NOT_ASKED,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return RedirectResponse(
        note_href(name, fragment=NOTE_RESULT, asked=asked.request_id),
        status_code=status.HTTP_303_SEE_OTHER,
    )
