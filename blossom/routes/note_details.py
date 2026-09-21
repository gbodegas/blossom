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
from dataclasses import dataclass, replace
from datetime import date
from typing import Final

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from blossom.authored_text import TextRefused, multiline, single_line
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
    CaptureNotSaved,
    CapturePromoted,
    CaptureUnchanged,
    ChoiceNeeded,
    DetailsMissing,
    NotACaptureId,
    PromotionChoice,
    UnknownCapture,
    UnreadableCapture,
    candidate_basis,
    capture_id_from,
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

CHOOSE_ABOUT_THESE: Final = (
    "Homework with this class and title is already here, so nothing was added yet. Say "
    "whether this is the same homework or a separate assignment, then add it again."
)
CHOICE_IS_PAST: Final = (
    "The homework that choice was about has changed since the page was opened, so nothing "
    "was added yet. Check the details and add it again."
)
NEEDS_A_CLASS: Final = "Choose the class, or choose Another class and type it."
CLASS_NOT_OFFERED: Final = "Choose a class from the list, or Another class and type it."
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
DATE_NEEDS_A_CHOICE: Final = (
    "A date was given that could not be read. Pick a date, or tick the box to leave the due "
    "date out."
)
NOT_SAVED: Final = "That could not be saved, and nothing was changed. What was typed is still here."

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
    candidate: str = ""
    """The choice among homework already on record, as sent: one of them, or separate."""
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


def choice_is_as_written(fields: dict[str, str]) -> bool:
    """Whether the fingerprint and the choice are as a page writes them: the fingerprint a
    digest in plain hexadecimal, and the choice, when one came, an id of bounded length with
    nothing around it. Both buttons send both, so both routes hold them to this; saving
    details then uses neither."""
    chosen = fields.get("candidate")
    return BASIS_AS_WRITTEN.fullmatch(fields.get("basis", "")) is not None and (
        chosen is None or (0 < len(chosen) <= TOKEN_MAX_LENGTH and chosen == chosen.strip())
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
    blank is never read as that. The tick counts only in the save it was
    ticked for, so a form that comes back is never ticked.
    """
    marked = "date_pending" in fields or "date_refused" in fields
    carried = fields.get("date_refused", "")
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
        candidate=fields.get("candidate", "") if choice_is_as_written(fields) else "",
    )
    without = marked and fields.get("without_date") == "1"
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
            return PreparedDetails(refused, None, None if without else DATE_UNREADABLE)
        if without:
            return PreparedDetails(replace(form, due_date=day.isoformat()))
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
    saving details does not."""
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
    elif choice and choice not in courses:
        return problem(CLASS_NOT_OFFERED, "course")
    else:
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


@dataclass(frozen=True)
class CandidateView:
    """Homework on record with the class and title the form gives, as the choice shows it:
    what it is, when it is due, what the school last reported, and where it came from."""

    assignment_id: str
    course: str
    title: str
    due_date: date | None
    status: str
    source: str


def candidate_view(item: Assignment, *, family: bool) -> CandidateView:
    """One candidate for the choice. Where it came from is the channel of its record, in the
    words her pages use, and said about her on the family's."""
    channel = item.origins.get("record")
    source = "" if channel is None else f"the {CHANNEL_NAMES[channel]}"
    if channel is SourceChannel.STUDENT_REPORT:
        source = "her report" if family else "your report"
    return CandidateView(
        assignment_id=item.assignment_id,
        course=item.course,
        title=item.title,
        due_date=item.due_date,
        status=item.reported_submission_status,
        source=source,
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


def details_page(
    request: Request,
    state: ApplicationState,
    capture_id: str,
    way: Way,
    *,
    form: DetailsForm | None = None,
    problem: str | None = None,
    choosing: bool = False,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """The page with her words, the details, and the two buttons. The note is read as its own
    page reads it, its line of changes included, and the homework on record once: for the
    list of classes, and for the homework of the class and title the form gives, whose
    fingerprint the form carries back.

    When the page is the answer to a refused press, ``form`` holds what was
    typed, and a note that cannot be shown does not take that with it: the
    answer is then the page that reads no store, with every detail kept. It
    says what became of the note, except to the one who may not write here,
    who is told that.
    """
    store = state.project_state
    turned_away = status_code == status.HTTP_403_FORBIDDEN
    try:
        found = store.sound_capture_history(capture_id)
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
    candidates: list[Assignment] = []
    try:
        given = shown.course_other if shown.course_choice == OTHER_CLASS else shown.course_choice
        named = CaptureDetails(course=given, title=shown.title)
        candidates = store.promotion_candidates(named, among=assignments)
    except ValueError:
        # A class or a title the rules will not keep names no homework; the form says so.
        candidates = []
    viewer = viewer_of(request)
    offered = {item.assignment_id for item in candidates}
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
            "candidates": [candidate_view(item, family=way.family) for item in candidates],
            "basis": candidate_basis(candidates),
            "choosing": choosing,
            # A choice made is kept through a refusal about something else, while it is
            # still one of the choices shown. A choice put again is made again.
            "chosen": (
                shown.candidate
                if not choosing
                and candidates
                and (shown.candidate == SEPARATE or shown.candidate in offered)
                else ""
            ),
            "family": way.family,
            "may_write": way.open_to(viewer) and note.outstanding,
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
    it typed is weighed: by who pressed, or because the form is not one these pages make.
    Either refusal keeps what was typed."""
    form = ready.form
    if not way.open_to(viewer_of(request)):
        return details_or_plain(
            request,
            state,
            form.capture_id,
            way,
            form,
            NOT_HERS_TO_UPDATE,
            status.HTTP_403_FORBIDDEN,
        )
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
                NOTE_CHANGED,
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
    chosen = fields.get("candidate")
    choice: PromotionChoice = (
        "new" if chosen is None else ("separate" if chosen == SEPARATE else "same")
    )
    try:
        async with state.decision_lock:
            now, today = accepted_at(state)
            outcome = state.project_state.promote_capture(
                name,
                details,
                expected_revision=revision,
                basis=basis,
                choice=choice,
                target=chosen if choice == "same" else None,
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
                NOTE_CHANGED,
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
