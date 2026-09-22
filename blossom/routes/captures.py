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
result names the change the save made, or for a save that wrote nothing the
change it found standing, by the id the record gave that change, which no
page can work out. The page that answers looks that id up in the note's own
history: while it is the latest the result is what stands, and once something
newer follows it is said as something done earlier, on a page that shows the
note as it stands.

Nothing here makes an assignment, asks a model, or touches what a plan is made
from. Adding a note to homework has a page and routes of its own, which a note
still waiting links to; the result of that is said here, on the note's page,
with the way to the assignment, and with whether the assignment is inside
today's planning window.
"""

import logging
import re
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
    CLARIFY,
    CREATE,
    EDIT,
    HOUSEHOLD,
    LINK,
    PROMOTE,
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
    Remaining,
    UnknownCapture,
    UnreadableCapture,
    capture_id_from,
    new_capture_id,
    what_remains,
)
from blossom.dependencies import ApplicationState
from blossom.noticing import expect_due_date, in_week, notice_due_date
from blossom.pairing import pair
from blossom.reconciliation import SourceChannel
from blossom.routes.forms import TOKEN_MAX_LENGTH, fields_of
from blossom.routes.hand_in import accepted_at
from blossom.routes.navigation import (
    ADDED_NOTES_PAGE,
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
NOTE_EDITED_IN_HOMEWORK: Final = "Your changes are saved. The assignment is unchanged."
NOTE_RESTORED_IN_HOMEWORK: Final = (
    "This note is back in Notes added to homework. The assignment is unchanged."
)
NOTE_ASKED: Final = "You asked for help about this note. Your parents can see the request."
DETAILS_SAVED: Final = "Details saved. This is still a note, and it is not in a plan yet."
ADDED_TO_HOMEWORK: Final = "Added to homework."
JOINED_TO_HOMEWORK: Final = (
    "Joined to homework already here. Nothing on that assignment was changed."
)
ALREADY_ADDED: Final = "Already added to homework. This is the note as it stands now."
OUT_OF_THE_WINDOW: Final = "Saved here. It is not in today's planning window."
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
REVISION_AS_WRITTEN: Final = re.compile(r"[1-9][0-9]{0,8}", re.ASCII)

WORDS_AND_DAY: Final = frozenset({"text", "course", "due_date"})
PRESSED_OR_PENDING: Final = frozenset({"date_pending", "date_refused", "choice"})
"""What a browser leaves out of a form these pages made: the mark that a day was refused
and the words of that day, which only a refused form carries, and the name of the button
pressed, which a form sent with the Enter key does not carry."""
CREATE_FIELDS: Final = WORDS_AND_DAY | PRESSED_OR_PENDING | {"capture_id"}
EDIT_FIELDS: Final = WORDS_AND_DAY | PRESSED_OR_PENDING | {"revision"}
MOVE_FIELDS: Final = frozenset({"revision"})
HELP_FIELDS: Final = frozenset({"note"})

SAID: Final[dict[str, tuple[str, str | None]]] = {
    "saved": (NOTE_SAVED, CREATE),
    "same": (NOTE_ALREADY_SAVED, None),
    "edited": (NOTE_EDITED, EDIT),
    "unchanged": (NOTE_ALREADY_SAVED, None),
    "archived": (NOTE_ARCHIVED, ARCHIVE),
    "restored": (NOTE_RESTORED, RESTORE),
    "clarified": (DETAILS_SAVED, CLARIFY),
    "added": (ADDED_TO_HOMEWORK, PROMOTE),
    "joined": (JOINED_TO_HOMEWORK, LINK),
    "already": (ALREADY_ADDED, None),
}
"""What an address says a save did, the sentence for it, and the kind of change the event
it names must be in the note's history. A save that wrote nothing names the change it found
standing, which may be of any kind. The server writes the address; the page believes none
of it until the history bears it out, and an event id is not a number a person can count
to: a revision in its place, or an id of another note's, says nothing."""
SAID_OF_A_NOTE_IN_HOMEWORK: Final = {
    "edited": NOTE_EDITED_IN_HOMEWORK,
    "restored": NOTE_RESTORED_IN_HOMEWORK,
}
"""The sentence for a change that left the note in homework, read from the change itself: a
note in homework is in no queue of notes, and whether its assignment is in a plan is nothing
a note's page reads, so neither is said. The assignment is never changed by a change to a
note, and that is said."""
SAID_TO_EITHER: Final = frozenset({"clarified", "added", "joined", "already", "unchanged"})
"""The results a parent is shown too: what a save through the family's tree did, in words
that address nobody. The rest are about changes only she can make, and are said to her."""


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
    """What a save did, as a note's page says it: the sentence, whether what it made is
    still the latest, and whether it is about adding the note to homework, which is the one
    result that goes on to say where the assignment stands."""

    said: str
    stands: bool
    about_adding: bool = False


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


@dataclass(frozen=True)
class Prepared:
    """A form as it will be shown again whatever is then refused, with the day it carried
    settled: the day when it reads, and what to say about it when it does not."""

    form: NoteForm
    day: date | None = None
    about_the_day: str | None = None


def reads_as_a_day(written: str) -> bool:
    """Whether words are a day as the form reads one, in any spelling it reads."""
    try:
        date.fromisoformat(written.strip())
    except ValueError:
        return False
    return True


def date_controls_are_valid(fields: dict[str, str]) -> bool:
    """Whether the fields that steer the date are a combination one of these pages renders.

    A form with no mark has the one button, or none for the Enter key. A
    form marked by a refused day has both buttons, and carries the refused
    words only when they could be shown. So the button that saves without
    the date comes only with the mark, and would otherwise drop a day on the
    word of a button no page showed. The refused words come only with the
    mark, only as a page writes them, which is what ``shown_day`` makes of a
    day, and only when they do not read as a day, since a day that reads is
    never refused. Words with a control character, a line break, space
    around them, more than a page sends, or nothing in them were written by
    no page. A value that is near one of these is not one of these, and
    nothing is trimmed into it.
    """
    choice, mark, refused = (
        fields.get(name) for name in ("choice", "date_pending", "date_refused")
    )
    marked = mark == "1"
    return (
        choice in (None, "save", WITHOUT_DATE)
        and mark in (None, "1")
        and (choice != WITHOUT_DATE or marked)
        and (
            refused is None
            or (marked and shown_day(refused) == refused and not reads_as_a_day(refused))
        )
    )


def prepared(fields: dict[str, str], capture_id: str, revision: int | None = None) -> Prepared:
    """The form as she sent it, every readable word kept, made before anything is refused
    so that every refusal, of the whole form included, shows the same thing.

    The day is settled here, apart from which problem is then said. A day
    that reads is kept in the one spelling a native date control holds. A
    day that does not is kept as she sent it and marks the form. A form that
    arrives marked, by either of the two fields that say so and whatever
    they hold, stays marked until a day that reads replaces the refused one:
    her choice to save without it takes effect only in the save it is
    pressed for, so it is never read out of a blank day after another error.

    Save without the date, on a form that carries the mark, does what it
    says whatever the date control holds beside it: no day goes to the store
    and nothing is said about the day. On a form with no mark no page showed
    that button, so it settles nothing here and the route refuses the form.
    What she typed there is still kept on the form, with its mark, so that
    another refusal of the same form shows it and offers both buttons again.
    """
    marked = "date_pending" in fields or "date_refused" in fields
    carried = fields.get("date_refused", "")
    form = NoteForm(
        capture_id=capture_id,
        text=fields.get("text", ""),
        course=fields.get("course", ""),
        # Shown again only as a page wrote them; forged words are not tidied into view, and
        # words that read as a day were refused by no page.
        date_refused=(
            carried
            if carried and shown_day(carried) == carried and not reads_as_a_day(carried)
            else None
        ),
        date_pending=marked,
        revision=revision,
    )
    # The button counts only on a form that carries the mark; without it no page showed the
    # button, and a day beside it is kept as any day is.
    without = marked and fields.get("choice") == WITHOUT_DATE
    raw = fields.get("due_date", "").strip()
    if raw:
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            shown = shown_day(raw)
            refused = replace(
                form,
                due_date="",
                date_refused=None if shown is None or reads_as_a_day(shown) else shown,
                date_pending=True,
            )
            return Prepared(refused, None, None if without else NOTE_DATE_UNREADABLE)
        if without:
            return Prepared(replace(form, due_date=day.isoformat(), date_pending=True))
        return Prepared(
            replace(form, due_date=day.isoformat(), date_refused=None, date_pending=False), day
        )
    if marked and not without:
        return Prepared(form, None, NOTE_NEEDS_A_DATE_CHOICE)
    return Prepared(form)


def checked(ready: Prepared) -> tuple[NoteForm, date | None]:
    """Hold what she typed to the rules, field by field. One problem is said at a time: her
    words, then the class, then the day. The form that goes back is the prepared one, so
    it holds the day whichever of them is refused."""
    form = ready.form
    try:
        if multiline(form.text, CAPTURE_TEXT_MAX_LENGTH) is None:
            return replace(form, problem=NOTE_NEEDS_WORDS, field="text"), None
    except TextRefused as refusal:
        return replace(form, problem=refusal_for(refusal, course=False), field="text"), None
    try:
        single_line(form.course, CAPTURE_COURSE_MAX_LENGTH)
    except TextRefused as refusal:
        return replace(form, problem=refusal_for(refusal, course=True), field="course"), None
    if ready.about_the_day is not None:
        return replace(form, problem=ready.about_the_day, field="due_date"), None
    return form, ready.day


def shown_day(raw: str) -> str | None:
    """The words of a day that could not be read, as a page may say them back: cut to a
    bounded length, and nothing at all when the text rule would not keep them. They are only
    ever shown, escaped, and carried by the form; nothing stores them."""
    try:
        return single_line(raw.strip()[:TOKEN_MAX_LENGTH], TOKEN_MAX_LENGTH)
    except TextRefused:
        return None


def ways_back(*, added: bool = False) -> list[ReturnLink]:
    """The two ways on from a note's pages, fixed addresses of this site: her notes, and
    her week. A note in homework is on the list of those, so that is the list it goes back
    to."""
    notes = (
        ReturnLink(ADDED_NOTES_PAGE, "Back to notes added to homework")
        if added
        else ReturnLink(NOTES_PAGE, "Back to Homework notes")
    )
    return [notes, ReturnLink(WEEK_PAGE, "Back to my week")]


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
    request: Request,
    state: ApplicationState,
    problem: str,
    form: NoteForm | None,
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
) -> HTMLResponse:
    """The page for a refused change whenever the note's own page cannot be made: the note
    cannot be read, it left the record, or the file cannot be read. It reads no store, tries
    nothing again, and keeps everything she typed: her words, the class, the day, and the
    words of a day that could not be read."""
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
        status_code=status_code,
    )


def help_not_sent(
    request: Request,
    state: ApplicationState,
    problem: str,
    question: str,
    capture_id: str | None,
    status_code: int,
) -> HTMLResponse:
    """The page for a request for help that was refused where no note can be shown beside
    it. It needs no note and reads no store: what happened, her question as she typed it,
    and the ways back, the note's own page among them when its name is one. A parent is
    shown no question, since the request is not theirs to make."""
    mine = viewer_of(request) != "parent"
    return templates.TemplateResponse(
        request,
        "student_update_recovery.html",
        {
            "card": None,
            "hand_in_card": None,
            "heading": "Request not sent",
            "note_problem": problem if mine else NOT_HERS_TO_UPDATE,
            "help_question": question if mine else "",
            "help_note": capture_id,
            "ways_back": ways_back(),
            "sample": state.settings.sample,
        },
        status_code=status_code if mine else status.HTTP_403_FORBIDDEN,
    )


def result_of(
    history: list[CaptureEvent], said: str | None, event: str | None
) -> NoteResult | None:
    """The result an address names, when the note's own history bears it out: the event is
    one of this note's, of the kind the sentence is about, and the sentence is said as what
    stands only while that event is the latest."""
    if said not in SAID or not event or len(event) > TOKEN_MAX_LENGTH:
        return None
    sentence, kind = SAID[said]
    made = next((change for change in history if change.event_id == event), None)
    if made is None or (kind is not None and made.operation != kind):
        return None
    stands = history[-1].event_id == made.event_id
    if made.after.assignment_id is not None:
        sentence = SAID_OF_A_NOTE_IN_HOMEWORK.get(said, sentence)
    return NoteResult(
        sentence if stands else NOTE_SAVED_EARLIER, stands, about_adding=kind in (PROMOTE, LINK)
    )


def unreadable(
    request: Request, state: ApplicationState, status_code: int = status.HTTP_200_OK
) -> HTMLResponse:
    """The small page for a note that is on record and cannot be read, which is not the page
    for a name that is no note: it says the note is unavailable and that nothing changed."""
    return templates.TemplateResponse(
        request,
        "student_note_gone.html",
        {"problem": NOTE_UNREADABLE, "ways_back": ways_back(), "sample": state.settings.sample},
        status_code=status_code,
    )


def note_page(
    request: Request,
    state: ApplicationState,
    capture_id: str,
    *,
    form: NoteForm | None = None,
    problem: str | None = None,
    said: str | None = None,
    event: str | None = None,
    asked: str | None = None,
    edit: bool = False,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """One note's page: what stands, the first words when they differ, who supplied a class
    or a day, the history, and for her the ways to change it. Two reads in one snapshot, and
    the changes held to being one sound line. A note or a change of it that cannot be read,
    or a line that is broken, is said as unavailable, never as gone, and gives no result.

    When the page is the answer to a refused change, ``form`` holds what she
    typed, and a note that cannot be shown does not take that with it: the
    answer is then the page that reads no store, with everything she typed.
    """
    try:
        found = state.project_state.sound_capture_history(capture_id)
    except UnreadableCapture:
        if form is not None:
            return plain_failure(request, state, NOTE_UNREADABLE, form, refusal(status_code))
        return unreadable(request, state, status_code)
    except Exception:
        if form is None:
            raise
        logger.exception("the note %s could not be read to answer a refused change", capture_id)
        return plain_failure(request, state, problem or NOTE_UNREADABLE, form, refusal(status_code))
    if found is None:
        if form is not None:
            return plain_failure(request, state, NOTE_GONE, form, refusal(status_code, gone=True))
        return gone(request, state)
    note, history = found[0], list(found[1])
    viewer = viewer_of(request)
    mine = viewer != "parent"
    result = result_of(history, said, event) if mine or said in SAID_TO_EITHER else None
    if form is not None and not form.revision:
        # A form that named no revision these pages made comes back on the note as it
        # stands, as her unsaved words: saving them again is her choice, from this page.
        form = replace(form, revision=note.revision, unsaved=True)
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
            "in_homework": in_homework(state, note),
            "out_of_the_window": OUT_OF_THE_WINDOW,
            "ways_back": ways_back(added=note.assignment_id is not None and not note.archived),
            "text_max_length": CAPTURE_TEXT_MAX_LENGTH,
            "course_max_length": CAPTURE_COURSE_MAX_LENGTH,
            "sample": state.settings.sample,
        },
        status_code=status_code,
    )


@dataclass(frozen=True)
class InHomework:
    """The assignment a note is in, as its page says it: whether the assignment is still on
    record, and whether it is inside today's planning window."""

    assignment_id: str
    on_record: bool
    in_window: bool


def in_homework(state: ApplicationState, note: Capture) -> InHomework | None:
    """Where a note added to homework stands today, or ``None`` for a note still waiting.
    The assignment and the claims about its date are read in one hold of the store, and
    the window is decided as her week decides it."""
    if note.assignment_id is None:
        return None
    store = state.project_state
    with store.reading():
        item = store.one_assignment(note.assignment_id)
        records = (
            []
            if item is None
            else store.read_claims([note.assignment_id]).records.get(note.assignment_id, [])
        )
    if item is None:
        return InHomework(note.assignment_id, on_record=False, in_window=False)
    noticed = notice_due_date(expect_due_date(item), records)
    return InHomework(
        note.assignment_id,
        on_record=True,
        in_window=in_week(item, noticed, state.clock.today()),
    )


def remains_for(state: ApplicationState, notes: list[Capture]) -> dict[str, Remaining]:
    """What each waiting note on a list still needs, from one read of the homework on
    record, and from no read when none of the notes waits."""
    if not any(note.outstanding for note in notes):
        return {}
    on_record = {pair(item.course, item.title) for item in state.project_state.all_assignments()}
    return what_remains(notes, on_record)


def refusal(status_code: int, *, gone: bool = False) -> int:
    """The status of the page that reads no store: the refusal's own, which it stands in
    for, and for a page that had none, what became of the note."""
    if status_code != status.HTTP_200_OK:
        return status_code
    return status.HTTP_404_NOT_FOUND if gone else status.HTTP_500_INTERNAL_SERVER_ERROR


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


def notes_list(request: Request, state: ApplicationState, *, which: str) -> HTMLResponse:
    """One of her three lists of notes, each a single read of the notes: the ones still
    waiting, the ones added to homework, or the ones she put away. The waiting ones say
    what each still needs, from one read of the homework on record."""
    store = state.project_state
    read = {
        "waiting": store.outstanding_captures,
        "added": store.added_captures,
        "archived": store.archived_captures,
    }[which]()
    return templates.TemplateResponse(
        request,
        "student_notes.html",
        {
            "notes": read.notes,
            "unreadable": read.unreadable,
            "which": which,
            "archived": which == "archived",
            "remains": remains_for(state, read.notes),
            "viewer": viewer_of(request),
            "new_note_page": NEW_NOTE_PAGE,
            "notes_page": NOTES_PAGE,
            "added_notes_page": ADDED_NOTES_PAGE,
            "archived_notes_page": ARCHIVED_NOTES_PAGE,
            "sample": state.settings.sample,
        },
    )


@router.get("/homework-notes", response_class=HTMLResponse, include_in_schema=False)
def homework_notes(request: Request, state: State) -> HTMLResponse:
    """Every note still to do something about, the first saved first."""
    return notes_list(request, state, which="waiting")


@router.get("/homework-notes/added", response_class=HTMLResponse, include_in_schema=False)
def added_notes(request: Request, state: State) -> HTMLResponse:
    """Every note that is in homework and not put away, kept with its history."""
    return notes_list(request, state, which="added")


@router.get("/homework-notes/archived", response_class=HTMLResponse, include_in_schema=False)
def archived_notes(request: Request, state: State) -> HTMLResponse:
    """Every note she put away, kept with its history. One read."""
    return notes_list(request, state, which="archived")


@router.get("/homework-notes/{capture_id}", response_class=HTMLResponse, include_in_schema=False)
def one_note(
    request: Request,
    capture_id: str,
    state: State,
    said: str | None = None,
    event: str | None = None,
    asked: str | None = None,
    edit: str | None = None,
) -> HTMLResponse:
    """One note. ``said`` and ``event`` are what a save did and the change it made or found,
    looked up in the note's history; ``edit`` opens the form; nothing here writes."""
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return gone(request, state)
    return note_page(request, state, name, said=said, event=event, asked=asked, edit=edit == "1")


@router.get(
    "/homework-notes/{capture_id}/help", response_class=HTMLResponse, include_in_schema=False
)
def help_about_a_note(request: Request, capture_id: str, state: State) -> HTMLResponse:
    """The page that offers to ask for help about one note. Opening it sends nothing. The
    note is read as its own page reads it, its line of changes included, so a note that page
    cannot show is not shown here either: it is said as one that cannot be read, never as
    not on record, and no form is offered."""
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return gone(request, state)
    try:
        found = state.project_state.sound_capture_history(name)
    except UnreadableCapture:
        return unreadable(request, state)
    if found is None:
        return gone(request, state)
    return help_page(request, state, found[0])


def help_page(
    request: Request,
    state: ApplicationState,
    note: Capture,
    *,
    question: str = "",
    problem: str | None = None,
    question_error: bool = False,
    before_the_refusal: bool = False,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """The page that offers the request, with the note as context and her question as she
    typed it. Rendering it sends nothing. ``question_error`` says the problem is about the
    question itself, so the alert links to it and the field points back.
    ``before_the_refusal`` says the note shown was read before a write the file refused
    and was not read again, so the page says when it was read and not that it stands."""
    return templates.TemplateResponse(
        request,
        "student_note_help.html",
        {
            "note": note,
            "question": question,
            "problem": problem,
            "question_error": question_error,
            "before_the_refusal": before_the_refusal,
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
    ready = prepared(fields, name)
    form = ready.form
    if viewer == "parent":
        return new_note_page(
            request,
            state,
            replace(form, problem=NOT_HERS_TO_UPDATE),
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if not whole or not date_controls_are_valid(fields):
        return new_note_page(
            request,
            state,
            replace(form, problem=BAD_FORM),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    form, due = checked(ready)
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
        case CaptureCreated(event=made):
            where = note_href(name, fragment=NOTE_RESULT, said="saved", event=made.event_id)
        case CaptureAlreadyCreated(head=found):
            where = note_href(name, fragment=NOTE_RESULT, said="same", event=found.event_id)
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
    """The revision a form says its page showed, or ``None`` for anything these pages do
    not write: a revision is a count from 1, in plain digits with nothing before them. A
    nought, a padded count, or a digit of another script is no revision of a note, and is
    refused where the form is read, since a save that asks for what already stands is
    answered before revisions are compared."""
    given = fields.get("revision", "")
    return int(given) if REVISION_AS_WRITTEN.fullmatch(given) else None


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
    ready = prepared(fields, name, revision)
    form = ready.form
    if not whole or revision is None or not date_controls_are_valid(fields):
        return note_page(
            request,
            state,
            name,
            form=form,
            problem=BAD_FORM,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    form, due = checked(ready)
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
        return plain_failure(request, state, NOTE_GONE, form, status.HTTP_404_NOT_FOUND)
    except CaptureNotSaved:
        logger.exception("her homework note %s could not be changed", name)
        return note_or_plain(request, state, name, NOTE_NOT_SAVED, form)
    match outcome:
        case CaptureChanged(event=made):
            where = note_href(name, fragment=NOTE_RESULT, said="edited", event=made.event_id)
        case CaptureUnchanged(head=found):
            where = note_href(name, fragment=NOTE_RESULT, said="unchanged", event=found.event_id)
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
        case CaptureChanged(event=made):
            said = "archived" if archive else "restored"
            where = note_href(name, fragment=NOTE_RESULT, said=said, event=made.event_id)
        case CaptureUnchanged(head=found):
            where = note_href(name, fragment=NOTE_RESULT, said="unchanged", event=found.event_id)
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
    Her question is read before the note is, so no refusal loses it. The
    note is read as its own page reads it, its line of changes included, so
    no request is sent about a note that page cannot show. A
    request the file refuses is said on the page she sent it from, made from
    the note already read, so it reads no store again. Where no note can be
    shown, since it cannot be read, left the record before or during the
    write, or the file cannot be read, the answer is the page that needs no
    note, with her question, and nothing is sent.
    A parent is answered 403 and nothing is sent in her name.
    """
    fields, whole = await fields_of(request, HELP_FIELDS)
    question = fields.get("note", "")
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return help_not_sent(request, state, NOTE_GONE, question, None, status.HTTP_404_NOT_FOUND)
    try:
        found = state.project_state.sound_capture_history(name)
    except UnreadableCapture:
        return help_not_sent(
            request, state, NOTE_UNREADABLE, question, name, status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    except Exception:
        logger.exception("the note %s could not be read for her request for help", name)
        return help_not_sent(
            request, state, HELP_NOT_ASKED, question, name, status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    if found is None:
        return help_not_sent(request, state, NOTE_GONE, question, None, status.HTTP_404_NOT_FOUND)
    note = found[0]
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
            question_error=whole,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    try:
        async with state.decision_lock:
            asked = state.help_requests.ask(state.clock.today(), words or None, capture_id=name)
    except UnknownCaptureReference:
        return help_not_sent(request, state, NOTE_GONE, question, None, status.HTTP_404_NOT_FOUND)
    except Exception:
        logger.exception("her request for help about note %s could not be sent", name)
        return help_page(
            request,
            state,
            note,
            question=question,
            problem=HELP_NOT_ASKED,
            before_the_refusal=True,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return RedirectResponse(
        note_href(name, fragment=NOTE_RESULT, asked=asked.request_id),
        status_code=status.HTTP_303_SEE_OTHER,
    )
