"""A homework note's details, and adding the note to homework, in her tree and the family's.

One page holds her words, which nothing here changes, the details a note may
be given, and two buttons: save the details, or add the note to homework. She
reaches it under ``/student`` and a parent under ``/parent``. Which tree a
press comes through says whose way in it was, her report or a family entry,
and nothing a form carries can say otherwise; who pressed is read from the
sign-in, and is the household while the sign-in is off.

Adding is the one confirmation: the page shows the fields that will become the
assignment, and the button adds them. With homework of the same class and
title on record, the press is answered with those as a choice, same homework
or a separate assignment, and the page sends back a fingerprint of what it
showed so that a choice made about homework that has since arrived, left, or
changed is put again. Nothing here asks a model anything, and her words are
copied nowhere.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Final

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from blossom.authored_text import TextRefused, multiline, single_line
from blossom.candidates import CandidateReading, candidate_readings, reader
from blossom.captures import (
    CAPTURE_COURSE_MAX_LENGTH,
    CAPTURE_NOTE_MAX_LENGTH,
    CAPTURE_TITLE_MAX_LENGTH,
    HOUSEHOLD,
    KINDS,
    PARENT,
    STUDENT,
    Author,
    CandidatesChanged,
    Capture,
    CaptureAlreadyPromoted,
    CaptureChanged,
    CaptureConflict,
    CaptureDetails,
    CaptureEvent,
    CaptureNotSaved,
    CapturePromoted,
    CaptureUnchanged,
    ChoiceNeeded,
    DetailsMissing,
    NotACaptureId,
    PromotionChoice,
    UnknownCapture,
    UnreadableCapture,
    accepted_press,
    candidate_basis,
    capture_id_from,
    derived_assignment_id,
)
from blossom.dependencies import ApplicationState
from blossom.reconciliation import CHANNEL_NAMES, SourceChannel
from blossom.routes.captures import (
    NOTE_CHANGED,
    NOTE_GONE,
    NOTE_UNREADABLE,
    gone,
    reads_as_a_day,
    revision_of,
    shown_day,
    unreadable,
    ways_back,
)
from blossom.routes.forms import TOKEN_MAX_LENGTH, fields_of
from blossom.routes.hand_in import accepted_at
from blossom.routes.navigation import NOTE_RESULT, note_href
from blossom.routes.student import (
    BAD_FORM,
    NOT_HERS_TO_UPDATE,
    State,
    templates,
    viewer_of,
)
from blossom.stores.project_state import Assignment

logger = logging.getLogger(__name__)

student_router = APIRouter(prefix="/student", tags=["student"])
family_router = APIRouter(prefix="/parent", tags=["parent"])

OTHER_CLASS: Final = "__another__"
"""The choice in the list of classes that means the class is typed in the box beside it."""
SEPARATE: Final = "separate"
"""The choice among homework already on record that means none of them is this."""
SAME: Final = "same:"
"""What comes before an assignment's id in the choice that means it is that homework. The
kind of choice is written apart from the id, since an id is any text the record keeps, the
word for a separate assignment included."""

CHOOSE_ABOUT_THESE: Final = (
    "Homework with this class and title is already here, so nothing was added yet. Say "
    "whether this is the same homework or a separate assignment, then add it again."
)
CHOICE_IS_PAST: Final = (
    "The homework that choice was about has changed since the page was opened, so nothing "
    "was added yet. Check the details and add it again."
)
NEEDS_A_CLASS: Final = "Choose the class, or choose Another class and type it."
CLASS_NOT_OFFERED: Final = (
    "The class chosen before is not in the list now. Choose a class from the list, or choose "
    "Another class and type it."
)
CLASS_TYPED_AND_CHOSEN: Final = (
    "A class is chosen and another is typed. Choose Another class to use the one you typed, "
    "or clear the box."
)
NEEDS_A_TITLE: Final = "Give the homework a title."
LONG_CLASS: Final = f"Keep the class to {CAPTURE_COURSE_MAX_LENGTH} characters or fewer."
LONG_TITLE: Final = f"Keep the title to {CAPTURE_TITLE_MAX_LENGTH} characters or fewer."
LONG_NOTE: Final = f"Keep the note to {CAPTURE_NOTE_MAX_LENGTH} characters or fewer."
ONE_LINE: Final = "Keep this on one line."
UNKEPT: Final = "That has a character Blossom cannot keep. Take it out and save again."
NOT_A_KIND: Final = "Choose Homework or Task."
DATE_UNREADABLE: Final = (
    "That date could not be read, so nothing was saved yet. Pick the date again, or tick the "
    "box to leave the due date out."
)
DATE_AND_TICK: Final = (
    "A date was picked and the box that leaves the due date out was ticked. Keep one: clear "
    "the date, or leave the box unticked. Nothing was saved."
)
DATE_NEEDS_A_CHOICE: Final = (
    "A date was given that could not be read. Pick a date, or tick the box to leave the due "
    "date out."
)
NOT_SAVED: Final = "That could not be saved, and nothing was changed. What was typed is still here."
NOT_A_PARENTS_PRESS: Final = "Sign in as a parent to add details here. Nothing was saved."
ALREADY_IN_HOMEWORK: Final = (
    "This note is already in homework, so nothing was saved. What it was added with is shown "
    "here, and what was typed is kept to copy."
)

ADD_MAY_BE_ABSENT: Final = frozenset({"date_pending", "date_refused", "without_date", "candidate"})
"""What a browser leaves out of the form: the mark and the refused words when no day was
refused, the tick when it is not ticked, and the choice when none is made or none is shown."""
ADD_FIELDS: Final = ADD_MAY_BE_ABSENT | {
    "revision",
    "basis",
    "course_choice",
    "course_other",
    "title",
    "due_date",
    "kind",
    "note",
}
"""The one form both buttons send, whole: the second button changes only where it goes."""
BASIS_AS_WRITTEN: Final = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Way:
    """The tree a press came through: her own, or the family's. It decides the channel a
    change is recorded on, and who may use it."""

    family: bool

    @property
    def channel(self) -> SourceChannel:
        """Her report through her tree, a family entry through the family's."""
        return SourceChannel.PARENT_ENTRY if self.family else SourceChannel.STUDENT_REPORT

    def open_to(self, viewer: str) -> bool:
        """Her tree is hers and the household's; the family's is a parent's and the
        household's. Signed in as the other, a press writes nothing."""
        return viewer != ("student" if self.family else "parent")

    @property
    def refusal(self) -> str:
        """What the other person is told: whose the form is, and that nothing was saved."""
        return NOT_A_PARENTS_PRESS if self.family else NOT_HERS_TO_UPDATE


HERS: Final = Way(family=False)
THEIRS: Final = Way(family=True)


def actor(viewer: str) -> Author:
    """Who a change is recorded as made by: the one signed in, or the household while the
    sign-in is off and a page cannot say which person pressed."""
    return {"student": STUDENT, "parent": PARENT}.get(viewer, HOUSEHOLD)  # type: ignore[return-value]


@dataclass(frozen=True)
class DetailsForm:
    """What the details form holds when it is shown: what was chosen and typed, as it was,
    and what the page says about it. Nothing here is what is stored."""

    capture_id: str
    revision: int | None = None
    course_choice: str = ""
    course_other: str = ""
    title: str = ""
    due_date: str = ""
    kind: str = "HOMEWORK"
    note: str = ""
    date_refused: str | None = None
    date_pending: bool = False
    basis: str = ""
    """The fingerprint of the homework the page showed, as sent, when it is one a page writes.
    A choice stands only on the facts it was made on, so the page that comes back holds this
    against the fingerprint of what stands now."""
    candidate: str = ""
    """The choice among homework already on record, as sent: one of them, or separate. Whether
    it stays chosen is decided beside the rows as they stand; otherwise it is said back as
    unsaved."""

    @property
    def chosen_assignment(self) -> str | None:
        """The assignment the choice names as the same homework, or ``None`` for no choice
        or the choice of a separate assignment: what a page that cannot show the rows says."""
        read = choice_from(self.candidate or None)
        return read[1] if read is not None and read[0] == "same" else None

    problem: str | None = None
    field: str | None = None
    unsaved: bool = False


def details_date_controls_are_valid(fields: dict[str, str]) -> bool:
    """Whether the fields that steer the date are a combination this page renders: the
    whole table. A form with no mark carries neither the refused words nor the tick. A form
    marked by a refused day carries the mark as written, the refused words only as a page
    writes them and only when they do not read as a day, and the tick, when ticked, as
    written."""
    mark, refused, without = (
        fields.get(name) for name in ("date_pending", "date_refused", "without_date")
    )
    marked = mark == "1"
    return (
        mark in (None, "1")
        and without in (None, "1")
        and (without is None or marked)
        and (
            refused is None
            or (marked and shown_day(refused) == refused and not reads_as_a_day(refused))
        )
    )


def choice_value(assignment_id: str) -> str:
    """The choice that means a note is the same homework as this assignment, as a form
    sends it. The one place it is written, for the page and for a page shown again."""
    return f"{SAME}{assignment_id}"


def choice_from(value: str | None) -> tuple[PromotionChoice, str | None] | None:
    """The choice a form sent, or ``None`` for anything these pages do not write. No choice
    is what a page with no homework of that class and title sends. The word for a separate
    assignment is that. Anything else is the prefix, taken off once, and then an id exactly
    as the record keeps ids: one line, bounded, with nothing around it. It is never trimmed
    into one, and never split, so an id with a colon in it, or the prefix itself, is whole."""
    if value is None:
        return "new", None
    if value == SEPARATE:
        return "separate", None
    if not value.startswith(SAME):
        return None
    named = value.removeprefix(SAME)
    try:
        kept = single_line(named, TOKEN_MAX_LENGTH)
    except TextRefused:
        return None
    return ("same", named) if kept == named else None


def choice_is_as_written(fields: dict[str, str]) -> bool:
    """Whether the fingerprint and the choice are as a page writes them: the fingerprint a
    digest in plain hexadecimal, and the choice, when one came, in the one spelling
    ``choice_from`` reads. Both buttons send both, so both routes hold them to this; saving
    details then uses neither."""
    return (
        BASIS_AS_WRITTEN.fullmatch(fields.get("basis", "")) is not None
        and choice_from(fields.get("candidate")) is not None
    )


@dataclass(frozen=True)
class PreparedDetails:
    """A form as it will be shown again whatever is then refused, with the day it carried
    settled: the day when it reads, and what to say about it when it does not."""

    form: DetailsForm
    day: date | None = None
    about_the_day: str | None = None


def prepared(fields: dict[str, str], capture_id: str, revision: int | None) -> PreparedDetails:
    """The form as it was sent, every readable value kept, made before anything is refused
    so that every refusal, of the whole form and of who pressed included, shows the same
    thing: the class chosen and typed, the title, the day, the kind, the note, and the choice.

    The day is settled here, apart from which problem is then said. A day
    that reads is kept in the one spelling a date control holds and clears
    the mark. One that does not is said back and marks the form, and a
    marked form with no day needs the tick that leaves the due date out; a
    blank is never read as that. A day in the control beside the tick is two
    instructions and neither is taken: nothing is saved and the form asks
    for one. The tick counts only in the save it was ticked for, so a form
    that comes back is never ticked.
    """
    marked = "date_pending" in fields or "date_refused" in fields
    carried = fields.get("date_refused", "")
    sent = fields.get("candidate", "")
    given = fields.get("basis", "")
    basis = given if BASIS_AS_WRITTEN.fullmatch(given) else ""
    form = DetailsForm(
        capture_id=capture_id,
        revision=revision,
        course_choice=fields.get("course_choice", ""),
        course_other=fields.get("course_other", ""),
        title=fields.get("title", ""),
        kind=fields.get("kind", ""),
        note=fields.get("note", ""),
        # Shown again only as a page wrote them; forged words are not tidied into view.
        date_refused=(
            carried
            if carried and shown_day(carried) == carried and not reads_as_a_day(carried)
            else None
        ),
        date_pending=marked,
        basis=basis,
        # A choice is kept only in the spelling a page writes one; anything else is dropped.
        candidate=sent if sent and choice_from(sent) is not None else "",
    )
    without = marked and fields.get("without_date") == "1"
    raw = fields.get("due_date", "").strip()
    if raw and without:
        # A day in the control beside the tick that leaves the day out is two instructions,
        # and nothing here picks one: the form goes back marked, the day where it was when
        # it reads and said back when it does not, the tick unticked, and the question put.
        try:
            kept = date.fromisoformat(raw).isoformat()
        except ValueError:
            shown = shown_day(raw)
            said = None if shown is None or reads_as_a_day(shown) else shown
            asked = replace(form, due_date="", date_refused=said, date_pending=True)
        else:
            asked = replace(form, due_date=kept, date_refused=None, date_pending=True)
        return PreparedDetails(asked, None, DATE_AND_TICK)
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
            return PreparedDetails(refused, None, DATE_UNREADABLE)
        return PreparedDetails(
            replace(form, due_date=day.isoformat(), date_refused=None, date_pending=False), day
        )
    if marked and not without:
        return PreparedDetails(form, None, DATE_NEEDS_A_CHOICE)
    return PreparedDetails(form)


def form_for(note: Capture, courses: list[str]) -> DetailsForm:
    """The form for a note as it stands. The title is proposed only from her words when they
    are one line that fits a title, and is hers to change; longer words are shown above the
    box and nothing is cut to fit."""
    proposed = note.title
    if proposed is None:
        try:
            proposed = single_line(note.text, CAPTURE_TITLE_MAX_LENGTH)
        except TextRefused:
            proposed = None
    listed = note.course is not None and note.course in courses
    return DetailsForm(
        capture_id=note.capture_id,
        revision=note.revision,
        course_choice=(note.course or "") if listed else (OTHER_CLASS if note.course else ""),
        course_other="" if listed else (note.course or ""),
        title=proposed or "",
        due_date="" if note.due_date is None else note.due_date.isoformat(),
        kind=note.kind or "HOMEWORK",
        note=note.note or "",
    )


def refused_text(refusal: TextRefused, too_long: str) -> str:
    """The sentence for text the record will not keep, by the rule it met."""
    if refusal.reason == "too_long":
        return too_long
    return ONE_LINE if refusal.reason == "line_break" else UNKEPT


def read_details(
    ready: PreparedDetails, courses: list[str], *, to_add: bool
) -> tuple[DetailsForm, CaptureDetails | None]:
    """Hold what was sent to the rules, one problem at a time in the order of the page, and
    give the details when there is none. The form that goes back is the prepared one, so it
    holds the day whichever field is refused. Adding to homework needs a class and a title;
    saving details does not. ``courses`` is the list the page offers; whether a class outside
    it is refused is the routes' to say."""
    form = ready.form

    def problem(text: str, field: str) -> tuple[DetailsForm, None]:
        return replace(form, problem=text, field=field), None

    if not form.course_choice and form.course_other.strip():
        # A class typed with none chosen is the class she typed; the list goes back saying so.
        form = replace(form, course_choice=OTHER_CLASS)
    choice, typed = form.course_choice, form.course_other
    if choice == OTHER_CLASS:
        given, at = typed, "course_other"
    elif typed.strip():
        return problem(CLASS_TYPED_AND_CHOSEN, "course_other")
    else:
        # A class chosen from the list the page showed is held to the rules a typed one is.
        # One the list does not hold now is not refused here: the routes ask the fingerprint
        # of the homework shown whether that homework changed, and refuse it only when not.
        given, at = choice, "course"
    try:
        course = single_line(given, CAPTURE_COURSE_MAX_LENGTH)
    except TextRefused as refusal:
        return problem(refused_text(refusal, LONG_CLASS), at)
    if course is None and (to_add or choice == OTHER_CLASS):
        return problem(NEEDS_A_CLASS, at)
    try:
        title = single_line(form.title, CAPTURE_TITLE_MAX_LENGTH)
    except TextRefused as refusal:
        return problem(refused_text(refusal, LONG_TITLE), "title")
    if title is None and to_add:
        return problem(NEEDS_A_TITLE, "title")
    if ready.about_the_day is not None:
        return problem(ready.about_the_day, "due_date")
    if form.kind not in KINDS:
        return problem(NOT_A_KIND, "kind")
    try:
        about = multiline(form.note, CAPTURE_NOTE_MAX_LENGTH)
    except TextRefused as refusal:
        return problem(refused_text(refusal, LONG_NOTE), "note")
    return form, CaptureDetails(
        course=course,
        title=title,
        due_date=ready.day,
        kind=form.kind,  # type: ignore[arg-type]
        note=about,
    )


# ------------------------------------------------------------------ the page


def courses_of(assignments: list[Assignment]) -> list[str]:
    """The classes homework on record names, each once, in order, for the list of classes.
    A class spelled as the list's own word for another class is typed, never chosen."""
    return sorted({item.course for item in assignments} - {OTHER_CLASS})


def spoken(day: date) -> str:
    """A day as these pages say one."""
    return f"{day.strftime('%B')} {day.day}, {day.year}"


WORK_STATES: Final = {"done": "Done", "not_yet": "Not yet"}


@dataclass(frozen=True)
class CandidateRow:
    """One candidate as the choice shows it, every sentence made from the one reading the
    fingerprint is made from: what it is, what she currently says about the work, what each
    school channel currently says, what the record's own status holds, and where the record
    came from. Her account and the school's are said apart, and no report of hers is said as
    that, never filled in from the record's status."""

    value: str
    assignment_id: str
    course: str
    title: str
    due_date: date | None
    kind: str
    hers: str
    school: tuple[str, ...]
    recorded: str | None
    source: str | None


def candidate_row(item: CandidateReading, *, family: bool) -> CandidateRow:
    """The row for one reading, in her words on her pages and about her on the family's."""
    if item.work_state in WORK_STATES and item.work_reported_on is not None:
        who = "She" if family else "You"
        hers = f"{who} said {WORK_STATES[item.work_state]} on {spoken(item.work_reported_on)}."
    else:
        hers = f"No update from {'her' if family else 'you'} on it."
    source = None
    if item.record_source is SourceChannel.STUDENT_REPORT:
        source = "From her report." if family else "From your report."
    elif item.record_source is not None:
        source = f"From the {CHANNEL_NAMES[item.record_source]}."
    return CandidateRow(
        value=choice_value(item.assignment_id),
        assignment_id=item.assignment_id,
        course=item.course,
        title=item.title,
        due_date=item.due_date,
        kind=f"Kind: {'Task' if item.kind == 'TASK' else 'Homework'}.",
        hers=hers,
        school=tuple(
            f"The {CHANNEL_NAMES[word.channel]} reported {word.status.replace('_', ' ')} "
            f"on {spoken(word.reported_on)}."
            for word in item.school
        ),
        recorded=(
            None
            if item.recorded_status == "unknown"
            else f"Recorded status: {item.recorded_status.replace('_', ' ')}."
        ),
        source=source,
    )


@dataclass(frozen=True)
class ClassLeftBehind:
    """A class chosen from the list that is not in the list now: kept as the chosen option
    when it is one line within the bound, marked as not in the list, and refused as one the
    list does not offer unless the homework it named has changed since the page, which the
    fingerprint says and the store answers. A value that cannot be offered as a choice is
    shown as bounded words, or said to be unshowable."""

    option: str | None
    words: str | None


def class_left_behind(form: DetailsForm, courses: Sequence[str]) -> ClassLeftBehind | None:
    """The class the form chose from the list, when the list does not hold it now, or
    ``None``: a class in the list, the choice of another class, or no choice is not left
    behind. Never a class the record has, so nothing here makes a stale choice valid."""
    chosen = form.course_choice
    if not chosen or chosen == OTHER_CLASS or chosen in courses:
        return None
    try:
        kept = single_line(chosen, CAPTURE_COURSE_MAX_LENGTH)
    except TextRefused:
        return ClassLeftBehind(None, shown_day(chosen))
    if kept == chosen:
        return ClassLeftBehind(chosen, None)
    return ClassLeftBehind(None, shown_day(chosen))


@dataclass(frozen=True)
class PreviousChoice:
    """A choice that came with a press and was not taken, because what it was made about has
    changed: said back as an unsaved decision, and never made again for anyone."""

    separate: bool
    row: CandidateRow | None


def previous_choice(form: DetailsForm, rows: Sequence[CandidateRow]) -> PreviousChoice | None:
    """What the form chose, for a page that puts the choice again, or ``None`` when it chose
    nothing."""
    chosen = choice_from(form.candidate or None)
    if chosen is None or chosen[0] == "new":
        return None
    return PreviousChoice(
        separate=chosen[0] == "separate",
        row=next((row for row in rows if row.value == form.candidate), None),
    )


def plain_details(
    request: Request,
    state: ApplicationState,
    form: DetailsForm,
    problem: str,
    status_code: int,
    *,
    back_to_the_note: bool = True,
) -> HTMLResponse:
    """The page for a refused press whenever the page with the form cannot be made: the note
    cannot be read, it is not on record, or the file cannot be read. It reads no store, tries
    nothing again, and keeps every detail that was typed or chosen, to copy."""
    return templates.TemplateResponse(
        request,
        "student_update_recovery.html",
        {
            "card": None,
            "hand_in_card": None,
            "note_problem": problem,
            "details_form": form,
            "help_note": form.capture_id if back_to_the_note else None,
            "ways_back": ways_back(),
            "sample": state.settings.sample,
        },
        status_code=status_code,
    )


def standing_in(status_code: int, otherwise: int) -> int:
    """The status of the page that reads no store: the refusal's own, which it stands in for,
    and for a page that had none, what became of the note."""
    return status_code if status_code != status.HTTP_200_OK else otherwise


@dataclass(frozen=True)
class UnlinkRequest:
    """What an unlink press asked, carried through its refusal: the revision the page showed
    and the homework it named, which the page says apart from the link that stands."""

    revision: int | None
    leaving: str


def details_page(
    request: Request,
    state: ApplicationState,
    capture_id: str,
    way: Way,
    *,
    form: DetailsForm | None = None,
    problem: str | None = None,
    choosing: bool = False,
    found: tuple[Capture, tuple[CaptureEvent, ...]] | None = None,
    unlink_request: UnlinkRequest | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """The page with her words, the details, and the two buttons. The note is read as its own
    page reads it, its line of changes included, and the homework on record once: for the
    list of classes, and for the homework of the class and title the form gives, whose
    fingerprint the form carries back.

    Every page carries the fingerprint of the homework it shows. A choice a
    form came back with stands only on the facts it was made on: it stays
    chosen when the fingerprint it came with is the fingerprint of what
    stands now and it is still one of the choices offered. Otherwise,
    whatever the press was refused for, the choice is cleared and said back
    as unsaved beside the rows as they stand, so nobody's choice is carried
    onto facts nobody saw. ``choosing`` is the store's own answer that the
    choice is to be made or made again, which clears it as well.

    When the page is the answer to a refused press, ``form`` holds what was
    typed, and a note that cannot be shown does not take that with it: the
    answer is then the page that reads no store, with every detail kept. It
    says what became of the note, except to the one who may not write here,
    who is told that. ``found`` is the note and its line when the caller read them
    already, inside the reading this page then shares; ``unlink_request`` is what a
    refused unlink asked, said apart from the link that stands.
    """
    store = state.project_state
    turned_away = status_code == status.HTTP_403_FORBIDDEN
    try:
        found = found if found is not None else store.sound_capture_history(capture_id)
    except UnreadableCapture:
        if form is None:
            return unreadable(request, state, status_code)
        return plain_details(
            request,
            state,
            form,
            problem if turned_away and problem else NOTE_UNREADABLE,
            standing_in(status_code, status.HTTP_500_INTERNAL_SERVER_ERROR),
        )
    if found is None:
        if form is None:
            return gone(request, state)
        return plain_details(
            request,
            state,
            form,
            problem if turned_away and problem else NOTE_GONE,
            standing_in(status_code, status.HTTP_404_NOT_FOUND),
            back_to_the_note=False,
        )
    note = found[0]
    assignments = store.all_assignments()
    courses = courses_of(assignments)
    shown = form or form_for(note, courses)
    if not shown.revision:
        shown = replace(shown, revision=note.revision, unsaved=True)
    candidates: list[CandidateReading] = []
    try:
        given = shown.course_other if shown.course_choice == OTHER_CLASS else shown.course_choice
        named = CaptureDetails(course=given, title=shown.title)
        candidates = candidate_readings(store, named, assignments)
    except ValueError:
        # A class or a title the rules will not keep names no homework; the form says so.
        candidates = []
    viewer = viewer_of(request)
    rows = [candidate_row(item, family=way.family) for item in candidates]
    offered = ({row.value for row in rows} | {SEPARATE}) if rows else set()
    current = candidate_basis(candidates)
    made = form is not None and bool(shown.candidate)
    standing = made and shown.basis == current and shown.candidate in offered
    accepted = accepted_press(found[1])
    left = class_left_behind(shown, courses)
    return templates.TemplateResponse(
        request,
        "student_note_details.html",
        {
            "note": note,
            "form": shown,
            "problem": problem or shown.problem,
            "courses": courses,
            "other_class": OTHER_CLASS,
            "separate": SEPARATE,
            "candidates": rows,
            "basis": current,
            "choosing": choosing,
            "chosen": shown.candidate if standing and not choosing else "",
            "previous": previous_choice(shown, rows)
            if made and (choosing or not standing)
            else None,
            "left": left,
            # What a note in homework was added with: the press that was accepted, as its
            # event keeps it, whatever the note's own words have become since.
            "accepted": (
                None if accepted is None or note.assignment_id is None else accepted.after
            ),
            "family": way.family,
            "may_write": way.open_to(viewer) and note.outstanding,
            "joined": note.assignment_id is not None
            and note.assignment_id != derived_assignment_id(note.capture_id),
            "may_unlink": way.open_to(viewer) and not note.archived,
            "current": next(
                (item for item in assignments if item.assignment_id == note.assignment_id), None
            ),
            "unlink_request": unlink_request,
            "typed": form is not None,
            "not_hers": NOT_HERS_TO_UPDATE,
            "viewer": viewer,
            "ways_back": ways_back(),
            "course_max_length": CAPTURE_COURSE_MAX_LENGTH,
            "title_max_length": CAPTURE_TITLE_MAX_LENGTH,
            "note_max_length": CAPTURE_NOTE_MAX_LENGTH,
            "sample": state.settings.sample,
        },
        status_code=status_code,
    )


def details_or_plain(
    request: Request,
    state: ApplicationState,
    capture_id: str,
    way: Way,
    form: DetailsForm,
    problem: str,
    status_code: int,
    *,
    choosing: bool = False,
) -> HTMLResponse:
    """The page with a refusal said first, tried once; when the page cannot be read back, the
    plain page that reads no store and keeps everything that was typed."""
    try:
        return details_page(
            request,
            state,
            capture_id,
            way,
            form=form,
            problem=problem,
            choosing=choosing,
            status_code=status_code,
        )
    except Exception:
        logger.exception("a note's details page could not be read back after a refusal")
        return plain_details(request, state, form, problem, status_code)


def classes_on_record(state: ApplicationState) -> list[str] | None:
    """The classes a form's choice is held to, or ``None`` when the file cannot be read, which
    a press answers as a save that did not happen, with what was typed kept."""
    try:
        return courses_of(state.project_state.all_assignments())
    except Exception:
        logger.exception("the classes on record could not be read for a note's details")
        return None


def on_arrival(
    request: Request,
    state: ApplicationState,
    way: Way,
    ready: PreparedDetails,
    fields: dict[str, str],
    *,
    whole: bool,
) -> HTMLResponse | int:
    """The revision a press goes on from, or the answer to one that is refused before anything
    it typed is weighed, because the form is not one these pages make. The refusal keeps
    what was typed. Who pressed is settled before this, before the note's name is read."""
    form = ready.form
    if (
        not whole
        or form.revision is None
        or not choice_is_as_written(fields)
        or not details_date_controls_are_valid(fields)
    ):
        # A revision these pages did not write is part of a form they did not make.
        return details_or_plain(
            request,
            state,
            form.capture_id,
            way,
            form,
            BAD_FORM,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    return form.revision


def refused_press(
    request: Request, state: ApplicationState, way: Way, fields: dict[str, str], capture_id: str
) -> HTMLResponse | None:
    """The answer to a press through the other person's tree, made before the note's name is
    read, so what was typed is kept whether or not the name is a note's: the page with the
    form when the note can be shown, the page that reads no store otherwise, 403 either way
    and nothing written. ``None`` for a press by someone the tree is open to."""
    if way.open_to(viewer_of(request)):
        return None
    form = prepared(fields, capture_id, revision_of(fields)).form
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return plain_details(
            request, state, form, way.refusal, status.HTTP_403_FORBIDDEN, back_to_the_note=False
        )
    return details_or_plain(request, state, name, way, form, way.refusal, status.HTTP_403_FORBIDDEN)


def open_details(
    request: Request, capture_id: str, state: ApplicationState, way: Way
) -> HTMLResponse:
    """Open the page. It writes nothing."""
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return gone(request, state)
    return details_page(request, state, name, way)


# ------------------------------------------------------------------ the writes


async def save_details(
    request: Request, capture_id: str, state: ApplicationState, way: Way
) -> Response:
    """Save a note's details from the revision the page showed. Her words are no part of the
    form. The tree the press came through is the channel, and who pressed is the sign-in."""
    fields, whole = await fields_of(request, ADD_FIELDS, may_be_absent=ADD_MAY_BE_ABSENT)
    refused = refused_press(request, state, way, fields, capture_id)
    if refused is not None:
        return refused
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return gone(request, state)
    ready = prepared(fields, name, revision_of(fields))
    revision = on_arrival(request, state, way, ready, fields, whole=whole)
    if not isinstance(revision, int):
        return revision
    courses = classes_on_record(state)
    if courses is None:
        return plain_details(
            request, state, ready.form, NOT_SAVED, status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    form, details = read_details(ready, courses, to_add=False)
    if details is None:
        return details_or_plain(
            request,
            state,
            name,
            way,
            form,
            form.problem or BAD_FORM,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if class_left_behind(form, courses) is not None:
        # Saving details asks no fingerprint, so a class the list does not offer is refused
        # here, kept as the chosen option and marked as not in the list.
        return details_or_plain(
            request,
            state,
            name,
            way,
            replace(form, field="course"),
            CLASS_NOT_OFFERED,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    try:
        async with state.decision_lock:
            now, today = accepted_at(state)
            outcome = state.project_state.clarify_capture(
                name,
                details,
                expected_revision=revision,
                authored_by=actor(viewer_of(request)),
                channel=way.channel,
                now=now,
                today=today,
            )
    except UnknownCapture:
        return details_or_plain(
            request, state, name, way, form, NOT_SAVED, status.HTTP_404_NOT_FOUND
        )
    except CaptureNotSaved:
        logger.exception("the details of note %s could not be saved", name)
        return details_or_plain(
            request, state, name, way, form, NOT_SAVED, status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    match outcome:
        case CaptureChanged(event=made):
            where = note_href(name, fragment=NOTE_RESULT, said="clarified", event=made.event_id)
        case CaptureUnchanged(head=head):
            where = note_href(name, fragment=NOTE_RESULT, said="unchanged", event=head.event_id)
        case CaptureConflict(capture=note):
            return details_or_plain(
                request,
                state,
                name,
                way,
                replace(form, revision=note.revision, unsaved=True),
                NOTE_CHANGED if note.assignment_id is None else ALREADY_IN_HOMEWORK,
                status.HTTP_409_CONFLICT,
            )
    return RedirectResponse(where, status_code=status.HTTP_303_SEE_OTHER)


async def add_to_homework(
    request: Request, capture_id: str, state: ApplicationState, way: Way
) -> Response:
    """Add a note to homework with the details the form shows: the one confirmation.

    No choice sent means the page showed no homework of that class and title.
    A choice is one of the homework shown, or that this is separate. The
    fingerprint of what the page showed goes to the store with it, and the
    store compares it inside its transaction.
    """
    fields, whole = await fields_of(request, ADD_FIELDS, may_be_absent=ADD_MAY_BE_ABSENT)
    refused = refused_press(request, state, way, fields, capture_id)
    if refused is not None:
        return refused
    try:
        name = capture_id_from(capture_id)
    except NotACaptureId:
        return gone(request, state)
    ready = prepared(fields, name, revision_of(fields))
    revision = on_arrival(request, state, way, ready, fields, whole=whole)
    if not isinstance(revision, int):
        return revision
    courses = classes_on_record(state)
    if courses is None:
        return plain_details(
            request, state, ready.form, NOT_SAVED, status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    form, details = read_details(ready, courses, to_add=True)
    if details is None:
        return details_or_plain(
            request,
            state,
            name,
            way,
            form,
            form.problem or BAD_FORM,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    basis = fields.get("basis", "")
    # A class the list does not hold now was on the list the page showed, or was on no page
    # at all. When the homework of that class and title has changed since the page, the
    # fingerprint says so and the store answers, 409, with what changed; with nothing changed
    # about that homework the class is refused as one the list does not offer.
    if class_left_behind(form, courses) is not None:
        # A precheck and nothing more; the comparison that decides is the store's, inside
        # its transaction. A read that fails here is answered by the page that reads no
        # store, with everything typed kept, and the write is not tried.
        try:
            unchanged = candidate_basis(candidate_readings(state.project_state, details)) == basis
        except Exception:
            logger.exception("the homework for a note's chosen class could not be read")
            return plain_details(
                request, state, form, NOT_SAVED, status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        if unchanged:
            return details_or_plain(
                request,
                state,
                name,
                way,
                replace(form, field="course"),
                CLASS_NOT_OFFERED,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
    choice, target = choice_from(fields.get("candidate")) or ("new", None)
    try:
        async with state.decision_lock:
            now, today = accepted_at(state)
            outcome = state.project_state.promote_capture(
                name,
                details,
                expected_revision=revision,
                basis=basis,
                choice=choice,
                target=target,
                candidates=reader(state.project_state),
                authored_by=actor(viewer_of(request)),
                channel=way.channel,
                now=now,
                today=today,
            )
    except UnknownCapture:
        return details_or_plain(
            request, state, name, way, form, NOT_SAVED, status.HTTP_404_NOT_FOUND
        )
    except CaptureNotSaved:
        logger.exception("note %s could not be added to homework", name)
        return details_or_plain(
            request, state, name, way, form, NOT_SAVED, status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    match outcome:
        case CapturePromoted(event=made, created=created):
            said = "added" if created else "joined"
            where = note_href(name, fragment=NOTE_RESULT, said=said, event=made.event_id)
        case CaptureAlreadyPromoted(head=head):
            where = note_href(name, fragment=NOTE_RESULT, said="already", event=head.event_id)
        case CaptureConflict(capture=note):
            return details_or_plain(
                request,
                state,
                name,
                way,
                replace(form, revision=note.revision, unsaved=True),
                NOTE_CHANGED if note.assignment_id is None else ALREADY_IN_HOMEWORK,
                status.HTTP_409_CONFLICT,
            )
        case DetailsMissing():
            return details_or_plain(
                request,
                state,
                name,
                way,
                form,
                NEEDS_A_TITLE,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        case ChoiceNeeded(candidates=found) | CandidatesChanged(candidates=found):
            # With homework of that class and title on record the choice is put, or put
            # again. With none left, the choice sent was about homework that has gone.
            return details_or_plain(
                request,
                state,
                name,
                way,
                form,
                CHOOSE_ABOUT_THESE if found else CHOICE_IS_PAST,
                status.HTTP_409_CONFLICT,
                choosing=bool(found),
            )
    return RedirectResponse(where, status_code=status.HTTP_303_SEE_OTHER)


# ------------------------------------------------------------------ her tree


@student_router.get(
    "/homework-notes/{capture_id}/add", response_class=HTMLResponse, include_in_schema=False
)
def her_details(request: Request, capture_id: str, state: State) -> HTMLResponse:
    """The page in her tree. Opening it writes nothing."""
    return open_details(request, capture_id, state, HERS)


@student_router.post(
    "/actions/homework-notes/{capture_id}/details",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def save_her_details(request: Request, capture_id: str, state: State) -> Response:
    """Save details through her tree, recorded on her channel."""
    return await save_details(request, capture_id, state, HERS)


@student_router.post(
    "/actions/homework-notes/{capture_id}/add",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def add_hers_to_homework(request: Request, capture_id: str, state: State) -> Response:
    """Add the note to homework through her tree, recorded on her channel."""
    return await add_to_homework(request, capture_id, state, HERS)


# ------------------------------------------------------------------ the family's tree


@family_router.get(
    "/homework-notes/{capture_id}/add", response_class=HTMLResponse, include_in_schema=False
)
def their_details(request: Request, capture_id: str, state: State) -> HTMLResponse:
    """The page in the family's tree. Opening it writes nothing."""
    return open_details(request, capture_id, state, THEIRS)


@family_router.post(
    "/actions/homework-notes/{capture_id}/details",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def save_their_details(request: Request, capture_id: str, state: State) -> Response:
    """Save details through the family's tree, recorded as a family entry."""
    return await save_details(request, capture_id, state, THEIRS)


@family_router.post(
    "/actions/homework-notes/{capture_id}/add",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def add_theirs_to_homework(request: Request, capture_id: str, state: State) -> Response:
    """Add the note to homework through the family's tree, recorded as a family entry."""
    return await add_to_homework(request, capture_id, state, THEIRS)
