"""Her homework notes, through pages: a first save that asks for words and nothing else.

A note is written on a page of its own, reached from her week. The form is
given a random id when it is made, which writes nothing, and sends it back, so
the same form sent twice is one note and a lost answer loses nothing. A class
and a day are optional and folded away. A day that cannot be read does not
take the note with it and is never dropped without her say: the form comes
back whole, and the next save needs a day that reads or her explicit choice
to save without one.

The routes follow her other forms: who may write is decided first, from the
sign-in and never from the form, and a parent's press is refused before its
form is read or the note it names is looked up; for anyone else the form is
read whole, the write is one operation under the decision lock, a success is
a redirect to a page that says what stands, and a refusal keeps every
readable word. A parent reads every note, archived ones and history
included, and changes none. A
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
import sqlite3
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
    UNLINK,
    Author,
    Capture,
    CaptureAlreadyCreated,
    CaptureAlreadyDeleted,
    CaptureChanged,
    CaptureConflict,
    CaptureCreated,
    CaptureDeleted,
    CaptureEvent,
    CaptureIdTaken,
    CaptureInUse,
    CaptureNotSaved,
    CaptureUnchanged,
    CaptureUse,
    CaptureWasDeleted,
    NotACaptureId,
    Remaining,
    UnknownCapture,
    UnreadableCapture,
    capture_id_from,
    derived_assignment_id,
    new_capture_id,
    what_remains,
)
from blossom.dependencies import ApplicationState
from blossom.noticing import (
    PlanningWindow,
    expect_due_date,
    in_week,
    notice_due_date,
    planning_window,
)
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
    NOTES_RESULT,
    WEEK_PAGE,
    address,
    asked_address,
    note_href,
)
from blossom.routes.student import (
    BAD_FORM,
    FORM_SENT_OTHER_WORDS,
    FORM_USED,
    HELP_FORM_NOT_WHOLE,
    NOT_HERS_TO_ASK,
    NOT_HERS_TO_UPDATE,
    ReturnLink,
    State,
    parent_reads,
    templates,
    unavailable_page,
    viewer_of,
)
from blossom.stores.help_requests import (
    NOTE_MAX_LENGTH,
    AskOutcome,
    HelpAlreadyAsked,
    HelpAsked,
    HelpFormChanged,
    HelpFormUsed,
    NotARequestId,
    UnknownCaptureReference,
    new_request_id,
    request_id_from,
)
from blossom.stores.project_state import Assignment

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
LINK_CHANGED: Final = (
    "Joined to other homework already here. Nothing on either assignment was changed."
)
UNLINKED: Final = (
    "Unlinked. This note is back in Homework notes, and the homework it was joined to is unchanged."
)
OUT_OF_THE_WINDOW: Final = "Saved here. It is outside today's planning window ({window})."
"""Said of homework the planner would not take today, with the window's days."""
WINDOW_UNKNOWN: Final = (
    "A claim about its date cannot be read right now, so whether it is in today's planning "
    "window is not known."
)
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
NOTE_CANNOT_BE_READ: Final = "This homework note cannot be read right now."
NOTE_UNREADABLE: Final = f"{NOTE_CANNOT_BE_READ} Nothing was changed."
NOTE_DELETED: Final = "Note deleted."
NOTE_ALREADY_DELETED: Final = "That note was already deleted."
NOTE_WAS_DELETED: Final = "That note was deleted."
NOTE_DELETED_GONE: Final = "This homework note was deleted."
NOTE_NOT_DELETED: Final = "That could not be deleted, and nothing was changed. Try again."
NOTE_CHANGED_NOT_DELETED: Final = (
    "This note changed while you were away, so it wasn't deleted. This page shows it as it "
    "stands now."
)
NOTE_NOT_CHANGED: Final = "Note not changed"
NOTES_UNAVAILABLE: Final = "{whose} homework notes can't be shown right now. Try again in a moment."
NOTES_UNAVAILABLE_AFTER_A_DELETE: Final = (
    "{whose} homework notes can't be shown right now, so whether that note was deleted can't "
    "be checked yet. Try again in a moment."
)
"""What one of her lists of notes says when it can't be read: that it can't be shown, and,
when the address names a delete, that the delete can't be checked. An address proves no
delete, so neither says whether one happened."""
LISTS: Final[dict[str, tuple[str, str]]] = {
    "waiting": (NOTES_PAGE, "Homework notes"),
    "added": (ADDED_NOTES_PAGE, "Homework notes added to homework"),
    "archived": (ARCHIVED_NOTES_PAGE, "Archived homework notes"),
}
"""Each list of her notes: its address and its heading."""
"""The heading of the page that refuses a parent's change to her note, which reads no store."""
NOTE_USE_UNKNOWN: Final = (
    "This note was saved before notes could be deleted, and Blossom can't tell whether it "
    "was used, so it can't be deleted."
)
KEPT_BECAUSE: Final[dict[str, tuple[str, str]]] = {
    "added": ("It was just added to homework", "It's in homework"),
    "linked": (
        "It was just linked to homework the school lists",
        "It's linked to homework the school lists",
    ),
    "was_linked": ("It was linked to homework before",) * 2,
    "asked": ("It was named in a request for help",) * 2,
    "unknown": (
        "It was saved before notes could be deleted, and Blossom can't tell whether it was used",
    )
    * 2,
}
"""Why a note is kept, said as it happened just now and as it stands. A link removed or a
request for help is said as past, never as something that stands."""
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
HELP_FIELDS: Final = frozenset({"note", "request_id"})

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
    "relinked": (LINK_CHANGED, LINK),
    "unlinked": (UNLINKED, UNLINK),
    "already": (ALREADY_ADDED, None),
}
"""What an address says a save did, the sentence for it, and the kind of change the event
it names must be in the note's history. A save that wrote nothing names the change it found
standing, which may be of any kind. The server writes the address; the page believes none
of it until the history bears it out, and an event id is not a number a person can count
to: a revision in its place, or an id of another note's, says nothing. A link is joined or
moved as its own event keeps it, whatever the address says."""
SAID_OF_A_NOTE_IN_HOMEWORK: Final = {
    "edited": NOTE_EDITED_IN_HOMEWORK,
    "restored": NOTE_RESTORED_IN_HOMEWORK,
}
"""The sentence for a change that left the note in homework, read from the change itself: a
note in homework is in no queue of notes, and whether its assignment is in a plan is nothing
a note's page reads, so neither is said. The assignment is never changed by a change to a
note, and that is said."""
SAID_TO_EITHER: Final = frozenset(
    {"clarified", "added", "joined", "relinked", "unlinked", "already", "unchanged"}
)
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


def ways_back(request: Request, *, added: bool = False) -> list[ReturnLink]:
    """The two ways on from a note's pages, fixed addresses of this site: her notes, and
    her week, named for whoever reads. A note in homework is on the list of those, so that
    is the list it goes back to."""
    notes = (
        ReturnLink(ADDED_NOTES_PAGE, "Back to notes added to homework")
        if added
        else ReturnLink(NOTES_PAGE, "Back to Homework notes")
    )
    week = "Back to her week" if parent_reads(request) else "Back to my week"
    return [notes, ReturnLink(WEEK_PAGE, week)]


def new_note_page(
    request: Request,
    state: ApplicationState,
    form: NoteForm,
    *,
    taken: Capture | None = None,
    deleted: bool = False,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """The page a note is first written on. It reads no store, so it can always be shown.
    ``deleted`` answers a form whose note was deleted, with no form of its own."""
    return templates.TemplateResponse(
        request,
        "student_note_new.html",
        {
            "form": form,
            "taken": taken,
            "deleted": deleted,
            "new_note_page": NEW_NOTE_PAGE,
            "viewer": viewer_of(request),
            "parent": parent_reads(request),
            "not_hers": NOT_HERS_TO_UPDATE,
            "text_max_length": CAPTURE_TEXT_MAX_LENGTH,
            "course_max_length": CAPTURE_COURSE_MAX_LENGTH,
            "sample": state.settings.sample,
        },
        status_code=status_code,
    )


def gone(request: Request, state: ApplicationState, capture_id: str | None = None) -> HTMLResponse:
    """The small page for a name that is no note of this record. A note that was deleted
    is said to be, and nothing of it is shown, since nothing of it is kept."""
    problem = NOTE_GONE
    if capture_id is not None:
        try:
            if state.project_state.capture_deleted(capture_id):
                problem = NOTE_DELETED_GONE
        except (sqlite3.Error, ValueError):
            logger.exception("whether the note %s was deleted could not be read", capture_id)
    return templates.TemplateResponse(
        request,
        "student_note_gone.html",
        {"problem": problem, "ways_back": ways_back(request), "sample": state.settings.sample},
        status_code=status.HTTP_404_NOT_FOUND,
    )


def kept_because(use: CaptureUse, *, archived: bool, just: bool) -> str:
    """Why a note can't be deleted, and what she can do instead: archive it, or leave it
    archived. ``just`` is for a use that came after the page offered the delete."""
    now, before = KEPT_BECAUSE[use]
    instead = "It stays archived." if archived else "Archive it instead."
    return f"{now if just else before}, so it can't be deleted. {instead}"


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
            "ways_back": ways_back(request),
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
    *,
    before: AskOutcome | None = None,
) -> HTMLResponse:
    """The page for a request for help that was refused where no note can be shown beside
    it. It needs no note and reads no store: what happened, her question as she typed it,
    and the ways back, the note's own page among them when its name is one. A form whose
    id already asked, with other words or for a request since taken back, says that with
    409 and links to the help form on her week, which needs no note. A parent is shown no
    question, since the request is not theirs to make."""
    mine = viewer_of(request) != "parent"
    fresh_form = None
    if isinstance(before, HelpFormChanged | HelpFormUsed):
        problem = FORM_USED if isinstance(before, HelpFormUsed) else FORM_SENT_OTHER_WORDS
        status_code = status.HTTP_409_CONFLICT
        fresh_form = address(WEEK_PAGE, "ask-for-help")
    return templates.TemplateResponse(
        request,
        "student_update_recovery.html",
        {
            "card": None,
            "hand_in_card": None,
            "heading": "Request not sent",
            "note_problem": problem if mine else NOT_HERS_TO_ASK,
            "help_question": question if mine else "",
            "help_note": capture_id,
            "help_fresh_form": fresh_form,
            "ways_back": ways_back(request),
            "sample": state.settings.sample,
        },
        status_code=status_code if mine else status.HTTP_403_FORBIDDEN,
    )


def note_refused(request: Request, state: ApplicationState, capture_id: str) -> HTMLResponse:
    """The page for a parent's edit, archive, restore, or delete of her note, 403. It is made
    from the sign-in and the address alone: no store is read and no form, so it says the same
    whatever the note is or the form carried. The name's shape decides whether the way back
    to the note is offered, and nothing else about it is looked at."""
    try:
        shown: str | None = capture_id_from(capture_id)
    except NotACaptureId:
        shown = None
    return templates.TemplateResponse(
        request,
        "student_update_recovery.html",
        {
            "card": None,
            "hand_in_card": None,
            "heading": NOTE_NOT_CHANGED,
            "note_problem": NOT_HERS_TO_UPDATE,
            "help_note": shown,
            "ways_back": ways_back(request),
            "sample": state.settings.sample,
        },
        status_code=status.HTTP_403_FORBIDDEN,
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
    if kind == LINK:
        # A link by search that left no homework is joined, never moved, and a move is
        # moved, never joined: the event keeps which, and the address is held to it.
        asked = None if made.decision is None else made.decision.search_press
        moved = asked is not None and asked.leaving is not None
        if moved != (said == "relinked"):
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
    for a name that is no note. A refused press adds that nothing changed; a page opened
    doesn't, since it may follow a save that went through."""
    opened = request.method in ("GET", "HEAD")
    return templates.TemplateResponse(
        request,
        "student_note_gone.html",
        {
            "problem": NOTE_CANNOT_BE_READ if opened else NOTE_UNREADABLE,
            "ways_back": ways_back(request),
            "sample": state.settings.sample,
        },
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
    reraise: bool = False,
) -> HTMLResponse:
    """One note's page: what stands, the first words when they differ, who supplied a class
    or a day, the history, and for her the ways to change it. Two reads in one snapshot, and
    the changes held to being one sound line. A note or a change of it that cannot be read,
    or a line that is broken, is said as unavailable, never as gone, and gives no result.

    When the page is the answer to a refused change, ``form`` holds what she
    typed, and a note that cannot be shown does not take that with it: the
    answer is then the page that reads no store, with everything she typed.
    A read the file refuses is said as unavailable too, and a refused change says the
    note can't be read whenever its read fails. ``reraise`` marks a failed write instead:
    its own message stands, so a note the store can't decode, or any failed read without
    a form, goes back to the caller.
    """
    try:
        found = state.project_state.sound_capture_history(capture_id)
    except UnreadableCapture:
        if reraise:
            raise
        if form is not None:
            return plain_failure(request, state, NOTE_UNREADABLE, form, refusal(status_code))
        return unreadable(request, state, status_code)
    except Exception as fault:
        if form is None and (reraise or not isinstance(fault, sqlite3.Error)):
            raise
        logger.exception("the note %s could not be read", capture_id)
        if form is None:
            return unreadable(request, state, status_code)
        said = (problem or NOTE_UNREADABLE) if reraise else NOTE_UNREADABLE
        return plain_failure(request, state, said, form, refusal(status_code))
    if found is None:
        if form is not None:
            return plain_failure(request, state, NOTE_GONE, form, refusal(status_code, gone=True))
        return gone(request, state, capture_id)
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
    deletable = use_unknown = False
    if mine:
        try:
            use = state.project_state.capture_use(note.capture_id)
        except UnknownCapture:
            # Deleted from another tab after the note was read: nothing of it is shown.
            if form is not None:
                return plain_failure(
                    request, state, NOTE_GONE, form, refusal(status_code, gone=True)
                )
            return gone(request, state, capture_id)
        except (UnreadableCapture, sqlite3.Error):
            logger.exception("whether the note %s was used could not be read", capture_id)
        else:
            deletable, use_unknown = use is None, use == "unknown"
    if form is None and edit and mine and not note.archived:
        form = NoteForm(
            capture_id=note.capture_id,
            text=note.text,
            course=note.course or "",
            due_date="" if note.due_date is None else note.due_date.isoformat(),
            revision=note.revision,
        )
    homework = in_homework(state, note)
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
            "in_homework": homework,
            "deletable": deletable,
            "use_unknown": NOTE_USE_UNKNOWN if use_unknown else None,
            "out_of_the_window": (
                None
                if homework is None or homework.window is None
                else OUT_OF_THE_WINDOW.format(window=homework.window.said())
            ),
            "window_unknown": WINDOW_UNKNOWN,
            "ways_back": ways_back(
                request, added=note.assignment_id is not None and not note.archived
            ),
            "text_max_length": CAPTURE_TEXT_MAX_LENGTH,
            "course_max_length": CAPTURE_COURSE_MAX_LENGTH,
            "sample": state.settings.sample,
        },
        status_code=status_code,
    )


@dataclass(frozen=True)
class InHomework:
    """The assignment a note is in, as its page says it: whether the assignment is still on
    record, whether it is inside today's planning window, and whether a claim about its
    date cannot be read, in which case the window is not known: the claim that cannot be
    read may be the one that places the assignment in it."""

    assignment_id: str
    on_record: bool
    in_window: bool
    claims_unreadable: bool = False
    joined: bool = False
    """Whether the note was joined to homework that was on record before it, which can be
    moved or unlinked, rather than made into an assignment of its own, which cannot."""
    current: Assignment | None = None
    """The assignment as it stands, when it is on record, so a page can name it."""
    window: PlanningWindow | None = None
    """Today's planning window as the page read it, which a sentence about it names."""


def in_homework(state: ApplicationState, note: Capture) -> InHomework | None:
    """Where a note added to homework stands today, or ``None`` for a note still waiting.
    The assignment and the claims about its date are read in one hold of the store, and
    the window is decided as her week decides it."""
    if note.assignment_id is None:
        return None
    store = state.project_state
    with store.reading():
        item = store.one_assignment(note.assignment_id)
        claimed = None if item is None else store.read_claims([note.assignment_id])
    joined = note.assignment_id != derived_assignment_id(note.capture_id)
    if item is None or claimed is None:
        return InHomework(note.assignment_id, on_record=False, in_window=False, joined=joined)
    noticed = notice_due_date(expect_due_date(item), claimed.records.get(note.assignment_id, []))
    window = planning_window(state.clock.today())
    return InHomework(
        note.assignment_id,
        on_record=True,
        in_window=in_week(item, noticed, window.start),
        window=window,
        claims_unreadable=note.assignment_id in claimed.unreadable,
        joined=joined,
        current=item,
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
            reraise=True,
        )
    except Exception:
        logger.exception("a note's page could not be read back after a failed write")
        return plain_failure(request, state, problem, form)


# ------------------------------------------------------------------ the pages


@router.get("/homework-notes/new", response_class=HTMLResponse, include_in_schema=False)
def new_note(request: Request, state: State) -> HTMLResponse:
    """The form for a new note, with an id of its own. Making it writes nothing."""
    return new_note_page(request, state, NoteForm(capture_id=new_capture_id()))


def deleted_result(state: ApplicationState, deleted: str | None, already: str | None) -> str | None:
    """What her list says after a delete: that the note was deleted, or was already, when
    the record holds the note the address names as deleted. The address alone says
    nothing, so no name makes the list claim a deletion that did not happen."""
    for given, said in ((deleted, NOTE_DELETED), (already, NOTE_ALREADY_DELETED)):
        if not given or len(given) > TOKEN_MAX_LENGTH:
            continue
        try:
            if state.project_state.capture_deleted(given):
                return said
        except NotACaptureId:
            continue
    return None


def notes_list(
    request: Request, state: ApplicationState, *, which: str, result: str | None = None
) -> HTMLResponse:
    """One of her three lists of notes, each a single read of the notes: the ones still
    waiting, the ones added to homework, or the ones she put away. The waiting ones say
    what each still needs, from one read of the homework on record. ``result`` is what a
    delete did, said first."""
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
            "result": result,
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


def names_a_delete(deleted: str | None, already: str | None) -> bool:
    """Whether the address names a note her list would look up as deleted: one of the two
    words holds a note's id as these pages write it. Nothing is read."""
    for given in (deleted, already):
        try:
            capture_id_from(given or "")
        except NotACaptureId:
            continue
        return True
    return False


def notes_unavailable(
    request: Request, state: ApplicationState, which: str, error: sqlite3.Error, *, delete: bool
) -> HTMLResponse:
    """One of her lists of notes when the record can't be read: 503, what can't be shown,
    whether a delete the address names can't be checked, and the same address again."""
    path, heading = LISTS[which]
    parent = parent_reads(request)
    said = NOTES_UNAVAILABLE_AFTER_A_DELETE if delete else NOTES_UNAVAILABLE
    return unavailable_page(
        request,
        state,
        error,
        heading=heading,
        alert=said.format(whose="Her" if parent else "Your"),
        again=asked_address(path, request.scope["query_string"]),
        ways_back=[ReturnLink(WEEK_PAGE, "Back to her week" if parent else "Back to my week")],
    )


@router.get("/homework-notes", response_class=HTMLResponse, include_in_schema=False)
def homework_notes(
    request: Request, state: State, deleted: str | None = None, already: str | None = None
) -> HTMLResponse:
    """Every note still to do something about, the first saved first, and what a delete
    did when the address names a note the record holds as deleted. When the record can't
    be read, the page says so and never says whether a delete happened."""
    try:
        result = deleted_result(state, deleted, already)
        return notes_list(request, state, which="waiting", result=result)
    except sqlite3.Error as error:
        return notes_unavailable(
            request, state, "waiting", error, delete=names_a_delete(deleted, already)
        )


@router.get("/homework-notes/added", response_class=HTMLResponse, include_in_schema=False)
def added_notes(request: Request, state: State) -> HTMLResponse:
    """Every note that is in homework and not put away, kept with its history."""
    try:
        return notes_list(request, state, which="added")
    except sqlite3.Error as error:
        return notes_unavailable(request, state, "added", error, delete=False)


@router.get("/homework-notes/archived", response_class=HTMLResponse, include_in_schema=False)
def archived_notes(request: Request, state: State) -> HTMLResponse:
    """Every note she put away, kept with its history. One read."""
    try:
        return notes_list(request, state, which="archived")
    except sqlite3.Error as error:
        return notes_unavailable(request, state, "archived", error, delete=False)


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
    except (UnreadableCapture, sqlite3.Error):
        logger.exception("the note %s could not be read for its help page", name)
        return unreadable(request, state)
    if found is None:
        return gone(request, state, name)
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
    and was not read again, so the page says when it was read and not that it stands. Each
    time it is made, its form gets a fresh id."""
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
            "request_id": new_request_id(),
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
    as another note if she wants them. The id of a deleted note is answered
    409 whatever it sends, with a way to a new note. A parent is answered 403
    on the page a parent reads here, which holds no form and no words, before
    the form is read.
    """
    viewer = viewer_of(request)
    if viewer == "parent":
        return new_note_page(
            request,
            state,
            NoteForm(capture_id="", problem=NOT_HERS_TO_UPDATE),
            status_code=status.HTTP_403_FORBIDDEN,
        )
    fields, whole = await fields_of(request, CREATE_FIELDS, may_be_absent=PRESSED_OR_PENDING)
    try:
        name = capture_id_from(fields.get("capture_id", ""))
    except NotACaptureId:
        name, whole = new_capture_id(), False
    ready = prepared(fields, name)
    form = ready.form
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
        case CaptureWasDeleted():
            # Nothing of the deleted note comes back, not its id and not these words: a
            # new note is her choice, from a fresh page with an id of its own.
            return new_note_page(
                request,
                state,
                NoteForm(capture_id="", problem=NOTE_WAS_DELETED),
                deleted=True,
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
    brought back. A parent is answered 403 before the form is read or the
    note looked up.
    """
    viewer = viewer_of(request)
    if viewer == "parent":
        return note_refused(request, state, capture_id)
    fields, whole = await fields_of(request, EDIT_FIELDS, may_be_absent=PRESSED_OR_PENDING)
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return gone(request, state)
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
    whose own button names the newer revision. A parent is answered 403 before the form is
    read or the note looked up."""
    viewer = viewer_of(request)
    if viewer == "parent":
        return note_refused(request, state, capture_id)
    fields, whole = await fields_of(request, MOVE_FIELDS)
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return gone(request, state)
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
        return gone(request, state, name)
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


@router.api_route(
    "/homework-notes/{capture_id}/delete",
    methods=["GET", "HEAD"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
def delete_page(request: Request, capture_id: str, state: State) -> HTMLResponse:
    """The page that asks before a note is deleted: her words as they stand, what deleting
    does, and the two choices. Opening it writes nothing. A parent is answered 403 on the
    note's own page, and a note that can't be deleted 409, with the reason."""
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return gone(request, state)
    if viewer_of(request) == "parent":
        return note_page(
            request, state, name, problem=NOT_HERS_TO_UPDATE, status_code=status.HTTP_403_FORBIDDEN
        )
    try:
        found = state.project_state.sound_capture_history(name)
        use = None if found is None else state.project_state.capture_use(name)
    except (UnreadableCapture, sqlite3.Error):
        logger.exception("the note %s could not be read to ask before deleting it", name)
        return unreadable(request, state)
    except UnknownCapture:
        found = None
    if found is None:
        return gone(request, state, name)
    note = found[0]
    if use is not None:
        return note_page(
            request,
            state,
            name,
            problem=kept_because(use, archived=note.archived, just=False),
            status_code=status.HTTP_409_CONFLICT,
        )
    return templates.TemplateResponse(
        request,
        "student_note_delete.html",
        {
            "note": note,
            "ways_back": ways_back(request),
            "sample": state.settings.sample,
        },
    )


@router.post(
    "/actions/homework-notes/{capture_id}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def delete_a_note(request: Request, capture_id: str, state: State) -> Response:
    """Delete a note that was never used, from the revision the page showed.

    The form carries the revision and nothing else, never her words. The
    store checks, in the transaction that deletes, that the note was never
    used and still stands at that revision. A note used since is answered
    409 with the reason and a page that is behind 409 with the note as it
    stands, whose own page asks again. A note deleted before is already
    done. What a delete did is said on her list, from the record. A parent
    is answered 403 before the form is read or the note looked up, and
    nothing is deleted.
    """
    if viewer_of(request) == "parent":
        return note_refused(request, state, capture_id)
    fields, whole = await fields_of(request, MOVE_FIELDS)
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return gone(request, state)
    revision = revision_of(fields)
    if not whole or revision is None:
        return note_page(
            request,
            state,
            name,
            problem=BAD_FORM,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    try:
        async with state.decision_lock:
            outcome = state.project_state.delete_capture(name, expected_revision=revision)
    except UnknownCapture:
        return gone(request, state, name)
    except UnreadableCapture:
        return unreadable(request, state, status.HTTP_500_INTERNAL_SERVER_ERROR)
    except CaptureNotSaved:
        logger.exception("her homework note %s could not be deleted", name)
        return note_or_plain(request, state, name, NOTE_NOT_DELETED, None)
    match outcome:
        case CaptureDeleted():
            where = address(NOTES_PAGE, NOTES_RESULT, deleted=name)
        case CaptureAlreadyDeleted():
            where = address(NOTES_PAGE, NOTES_RESULT, already=name)
        case CaptureInUse(capture=note, use=use):
            return note_page(
                request,
                state,
                name,
                problem=kept_because(use, archived=note.archived, just=True),
                status_code=status.HTTP_409_CONFLICT,
            )
        case CaptureConflict():
            return note_page(
                request,
                state,
                name,
                problem=NOTE_CHANGED_NOT_DELETED,
                status_code=status.HTTP_409_CONFLICT,
            )
    return RedirectResponse(where, status_code=status.HTTP_303_SEE_OTHER)


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
    note, with her question, and nothing is sent. The form carries an id the
    page gave it: the same form sent again sends nothing more and lands on
    the request it made, as it stands now, on her week. That id is looked up
    before the note is, so the landing holds whatever became of the note
    since. One that already
    asked with other words, or whose request was taken back or has gone,
    sends nothing and keeps her question in a fresh form, or, where the note
    can't be shown, says so with 409 beside a link to a fresh form.
    A parent is answered 403 before the form is read, on the page that needs
    no note, and nothing is sent in her name.
    """
    if viewer_of(request) == "parent":
        try:
            shown: str | None = capture_id_from(capture_id)
        except NotACaptureId:
            shown = None
        return help_not_sent(request, state, NOT_HERS_TO_ASK, "", shown, status.HTTP_403_FORBIDDEN)
    fields, whole = await fields_of(request, HELP_FIELDS)
    question = fields.get("note", "")
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return help_not_sent(request, state, NOTE_GONE, question, None, status.HTTP_404_NOT_FOUND)
    words = question.strip()
    try:
        form = request_id_from(fields.get("request_id", ""))
    except NotARequestId:
        form = None
    sound = whole and form is not None
    before: AskOutcome | None = None
    if sound and form is not None and len(words) <= NOTE_MAX_LENGTH:
        try:
            before = state.help_requests.already_asked(form, words or None, capture_id=name)
        except Exception:
            logger.exception("the form for her request for help about note %s was not read", name)
            return help_not_sent(
                request,
                state,
                HELP_NOT_ASKED,
                question,
                name,
                status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        if isinstance(before, HelpAlreadyAsked):
            return RedirectResponse(
                address(WEEK_PAGE, "help-result", asked_again=before.request.request_id),
                status_code=status.HTTP_303_SEE_OTHER,
            )
    try:
        found = state.project_state.sound_capture_history(name)
    except UnreadableCapture:
        return help_not_sent(
            request,
            state,
            NOTE_UNREADABLE,
            question,
            name,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            before=before,
        )
    except Exception:
        logger.exception("the note %s could not be read for her request for help", name)
        return help_not_sent(
            request,
            state,
            HELP_NOT_ASKED,
            question,
            name,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            before=before,
        )
    if found is None:
        return help_not_sent(
            request, state, NOTE_GONE, question, None, status.HTTP_404_NOT_FOUND, before=before
        )
    note = found[0]
    if form is None or not sound or len(words) > NOTE_MAX_LENGTH:
        return help_page(
            request,
            state,
            note,
            question=question,
            problem=QUESTION_TOO_LONG if sound else HELP_FORM_NOT_WHOLE,
            question_error=sound,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    try:
        async with state.decision_lock:
            outcome = state.help_requests.ask_once(
                form, state.clock.today(), words or None, capture_id=name
            )
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
    match outcome:
        case HelpAsked(request=asked):
            where = note_href(name, fragment=NOTE_RESULT, asked=asked.request_id)
        case HelpAlreadyAsked(request=asked):
            where = address(WEEK_PAGE, "help-result", asked_again=asked.request_id)
        case HelpFormChanged() | HelpFormUsed():
            return help_page(
                request,
                state,
                note,
                question=question,
                problem=FORM_USED if isinstance(outcome, HelpFormUsed) else FORM_SENT_OTHER_WORDS,
                status_code=status.HTTP_409_CONFLICT,
            )
    return RedirectResponse(where, status_code=status.HTTP_303_SEE_OTHER)
