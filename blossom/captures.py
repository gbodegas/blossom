"""A homework note: her own words about something to remember, kept before it is homework.

She hears about work outside the portal, from a classmate, in class, on the
way out, and writes down what she knows. That is a note and nothing more: it
is no assignment, no claim about a date anyone can plan on, and nothing a
model reads. One note is one note, however many things it mentions.

A note keeps three things apart. The words she first saved never change. The
words that stand now are hers to edit. And what she sent with the first save,
the words, a class, a day, is kept as sent, so the same form sent twice is
found to be the same form, and the same id with anything else in it is found
to be another. A class and a day are optional, and each says who supplied it
and through which way in, so a later step that turns a note into homework can
keep that and need not guess. A title, a kind, and a note for the assignment
are for that later step; nothing here sets or infers them.

Every change is an event with what stood before it and after it, and who made
it: the student, a parent, or the household when the sign-in is off and the
application cannot say which person pressed. The record's store writes a
change and its event together.

This module holds the types and the rules. It reads no file and no clock.
"""

import hashlib
import json
import uuid
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal, Protocol, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator, model_validator

from blossom.authored_text import multiline, single_line
from blossom.pairing import pair
from blossom.reconciliation import SourceChannel

CAPTURE_TEXT_MAX_LENGTH: Final = 500
CAPTURE_COURSE_MAX_LENGTH: Final = 60
CAPTURE_TITLE_MAX_LENGTH: Final = 200
CAPTURE_NOTE_MAX_LENGTH: Final = 500
"""The limits an assignment entered by hand is held to, so a note's details fit the
assignment they may become."""
CAPTURE_CLAIM_CONFIDENCE: Final = 0.8
"""The confidence a claim about a due date carries when it is made from a note. The claims
table needs a value, and this is the one a family entry carries. It is not a confidence she
reported and it scores nothing about her: it is shown nowhere, ranks no account, and
settles no disagreement between dates."""
HOMEWORK_NOTE: Final = "homework note"
"""Where a claim made from a note says it was read."""

Author = Literal["student", "parent", "household"]
"""Who made a change. ``household`` is every change made while the sign-in is off, when a
page cannot say which person pressed; it is never a guess at one of the other two."""
STUDENT: Final = "student"
PARENT: Final = "parent"
HOUSEHOLD: Final = "household"

CaptureOperation = Literal[
    "create", "edit", "archive", "restore", "clarify", "promote", "link", "unlink"
]
CREATE: Final = "create"
EDIT: Final = "edit"
ARCHIVE: Final = "archive"
RESTORE: Final = "restore"
CLARIFY: Final = "clarify"
"""Details were added or changed, by her or by a parent. Her words are not details."""
PROMOTE: Final = "promote"
"""The note was added to homework as an assignment of its own."""
LINK: Final = "link"
"""The note was joined to homework already on record."""
UNLINK: Final = "unlink"
"""The note left the homework it was joined to and waits again; that homework stays."""

CaptureKind = Literal["HOMEWORK", "TASK"]
"""The kinds an assignment has, as a note's details hold one. Written out here because this
module reads no store; a test holds it to the record's own kinds."""
KINDS: Final = ("HOMEWORK", "TASK")

ATTRIBUTED: Final = ("course", "title", "due_date", "kind", "note")
"""The optional fields of a note, each with who supplied what stands in it."""
DETAILS: Final = ATTRIBUTED
"""The details a note may be given so that it can become homework. Her words are not one."""

PromotionChoice = Literal["new", "same", "separate", "found"]
"""What a person chose when adding a note to homework: there was no homework of that class
and title, it is the same homework as one shown, it is to be kept apart from those shown, or
it is homework found by search, named by the one row chosen."""


class NotACaptureId(ValueError):
    """Raised for an id that is not a UUID written the one way a form of this site writes it."""


class UnknownCapture(LookupError):
    """Raised when a change names a note the record does not have."""


class UnreadableCapture(ValueError):
    """Raised for a note whose row cannot be read as one. What it says is unavailable, which
    is not the same as there being no such note, and nothing is written on top of it."""


class CaptureNotSaved(RuntimeError):
    """The file refused a write to a note. Whatever was begun was rolled back with it, the
    note and its event together, so nothing of the change is kept."""

    def __init__(self, capture_id: str, cause: BaseException) -> None:
        super().__init__(f"the homework note {capture_id!r} could not be saved: {cause}")


def capture_id_from(value: str) -> str:
    """The id of a note as the record keeps it, or ``NotACaptureId``.

    A form gets a random UUID when it is made and sends it back. Anything
    else, another spelling of one included, is not an id these pages wrote,
    so it names no note and is looked up nowhere.
    """
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as error:
        raise NotACaptureId(value) from error
    if str(parsed) != value:
        raise NotACaptureId(value)
    return value


def new_capture_id() -> str:
    """A random id for a form that has not been saved yet. Making one writes nothing."""
    return str(uuid.uuid4())


class CaptureWords(BaseModel):
    """What she can write on a note: the words, and if she likes a class and a day.

    The one shape for what stands now and for what the first save sent, so
    the two are compared whole. The words are required and kept as written
    under the shared rule; a class is one line; a day is a day or nothing,
    and is never worked out from the words.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    course: str | None = None
    due_date: date | None = None

    @field_validator("text", mode="before")
    @classmethod
    def _is_words_the_record_keeps(cls, text: object) -> str:
        kept = multiline(text if isinstance(text, str) else None, CAPTURE_TEXT_MAX_LENGTH)
        if kept is None:
            msg = "a homework note needs some words"
            raise ValueError(msg)
        return kept

    @field_validator("course", mode="before")
    @classmethod
    def _is_one_kept_line(cls, course: object) -> str | None:
        return single_line(course if isinstance(course, str) else None, CAPTURE_COURSE_MAX_LENGTH)


def words_as_held(text: str, course: str | None, due_date: date | None, *, of: str) -> CaptureWords:
    """Words read back from the file, which holds them only as the text rule keeps them.

    ``CaptureWords`` tidies what a form sends: edges off, line endings as
    one kind, a blank class as no class. That is right on the way in and
    wrong on the way out, where it would quietly mend a row the store never
    wrote. Here what is held must already be what the rule keeps, or it is
    a ``ValueError``, which a row's reader says as a note that cannot be read.
    """
    words = CaptureWords(text=text, course=course, due_date=due_date)
    if (words.text, words.course) != (text, course):
        msg = f"{of} are not held as the text rule keeps them"
        raise ValueError(msg)
    return words


class CaptureDetails(BaseModel):
    """The details a note may be given: a class, a title, a day, a kind, and a note about
    the work. Each is optional on a note and held to the limits an assignment is held to.
    None of them is her words, and none is worked out from her words."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    course: str | None = None
    title: str | None = None
    due_date: date | None = None
    kind: CaptureKind | None = None
    note: str | None = None

    @field_validator("course", mode="before")
    @classmethod
    def _course_is_one_kept_line(cls, value: object) -> str | None:
        return single_line(value if isinstance(value, str) else None, CAPTURE_COURSE_MAX_LENGTH)

    @field_validator("title", mode="before")
    @classmethod
    def _title_is_one_kept_line(cls, value: object) -> str | None:
        return single_line(value if isinstance(value, str) else None, CAPTURE_TITLE_MAX_LENGTH)

    @field_validator("note", mode="before")
    @classmethod
    def _note_is_kept_text(cls, value: object) -> str | None:
        return multiline(value if isinstance(value, str) else None, CAPTURE_NOTE_MAX_LENGTH)

    @property
    def missing(self) -> tuple[str, ...]:
        """What a note still needs before it can be homework: a class, a title, or both."""
        return tuple(name for name in ("course", "title") if getattr(self, name) is None)


def details_as_held(
    course: str | None,
    title: str | None,
    due_date: date | None,
    kind: str | None,
    note: str | None,
    *,
    of: str,
) -> CaptureDetails:
    """Details read back from the file, which holds them only as the rules keep them. As
    with her words, what is tidied on the way in is not tidied on the way out: a row held
    any other way was not written by the store."""
    held = CaptureDetails(course=course, title=title, due_date=due_date, kind=kind, note=note)  # type: ignore[arg-type]
    if (held.course, held.title, held.note) != (course, title, note):
        msg = f"{of} are not held as the text rule keeps them"
        raise ValueError(msg)
    return held


class NoWords(ValueError):
    """Raised when a note is sent with no words in it, which is the one thing it needs."""


def kept_words(text: str | None, course: str | None, due_date: date | None) -> CaptureWords:
    """What is kept of what a form sent, or the refusal that says which rule was met.

    The shared text rule is applied here by name, so a caller gets
    ``TextRefused`` with its reason, or ``NoWords``, and never a validation
    error that has lost both. Nothing is read or written before this.
    """
    words = multiline(text, CAPTURE_TEXT_MAX_LENGTH)
    if words is None:
        raise NoWords
    return CaptureWords(
        text=words, course=single_line(course, CAPTURE_COURSE_MAX_LENGTH), due_date=due_date
    )


class FieldSource(BaseModel):
    """Who supplied what stands in one optional field, and through which way in."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authored_by: Author
    channel: SourceChannel


class Capture(BaseModel):
    """One homework note as the record holds it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capture_id: str
    original_text: str
    """The words of the first save, which no edit touches."""
    initial: CaptureWords
    """Everything the first save sent, as sent, which a repeat of that save is compared with."""
    text: str
    course: str | None = None
    due_date: date | None = None
    title: str | None = None
    kind: str | None = None
    note: str | None = None
    """``title``, ``kind``, and ``note`` are details she or a parent may add so that the note
    can become homework. Nothing infers them from her words."""
    attribution: dict[str, FieldSource] = {}
    """For each optional field that holds something, who supplied it."""
    created_at: AwareDatetime
    created_on: date
    updated_at: AwareDatetime
    updated_on: date
    revision: int
    """Counts every change, an archive and a restore included. A page sends back the one it
    showed, so a change made from an old page is found to be from an old page."""
    archived: bool = False
    assignment_id: str | None = None
    created_order: int
    """Its place among all notes by when it was first saved, given by the file and never
    moved by an edit, an archive, or a restore."""

    @field_validator("capture_id")
    @classmethod
    def _is_a_capture_id(cls, value: str) -> str:
        return capture_id_from(value)

    @model_validator(mode="after")
    def _is_whole(self) -> Self:
        if self.original_text != self.initial.text:
            msg = f"note {self.capture_id!r} has first words that are not what its first save sent"
            raise ValueError(msg)
        words_as_held(
            self.text, self.course, self.due_date, of=f"the words of note {self.capture_id!r}"
        )
        details_as_held(
            self.course,
            self.title,
            self.due_date,
            self.kind,
            self.note,
            of=f"the details of note {self.capture_id!r}",
        )
        if self.assignment_id is not None and single_line(self.assignment_id, 200) != (
            self.assignment_id
        ):
            msg = f"note {self.capture_id!r} names an assignment by no id"
            raise ValueError(msg)
        held = {name for name in ATTRIBUTED if getattr(self, name) is not None}
        if set(self.attribution) != held:
            msg = f"note {self.capture_id!r} does not say who supplied each optional field it holds"
            raise ValueError(msg)
        if self.revision < 1:
            msg = f"note {self.capture_id!r} has a revision before its first"
            raise ValueError(msg)
        return self

    @property
    def words(self) -> CaptureWords:
        """What stands now, in the shape a form sends."""
        return CaptureWords(text=self.text, course=self.course, due_date=self.due_date)

    @property
    def details(self) -> CaptureDetails:
        """The details that stand now, in the shape a form sends."""
        return CaptureDetails(
            course=self.course,
            title=self.title,
            due_date=self.due_date,
            kind=self.kind,  # type: ignore[arg-type]
            note=self.note,
        )

    @property
    def outstanding(self) -> bool:
        """Still a note to do something about: not archived, and not yet homework."""
        return not self.archived and self.assignment_id is None


class CaptureSnapshot(BaseModel):
    """What stood on a note at one moment, enough to show its history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    course: str | None = None
    due_date: date | None = None
    archived: bool = False
    title: str | None = None
    kind: str | None = None
    note: str | None = None
    assignment_id: str | None = None
    """The last four came with details and adding to homework. A moment written before
    them holds none, which reads as nothing in each."""

    @classmethod
    def of(cls, capture: Capture) -> "CaptureSnapshot":
        """What stands on a note now, as one moment of its history."""
        return cls(
            text=capture.text,
            course=capture.course,
            due_date=capture.due_date,
            archived=capture.archived,
            title=capture.title,
            kind=capture.kind,
            note=capture.note,
            assignment_id=capture.assignment_id,
        )

    @property
    def words(self) -> tuple[str, str | None, date | None]:
        """Her words with the class and day she may edit beside them."""
        return self.text, self.course, self.due_date

    @property
    def details(self) -> tuple[object, ...]:
        """The details, as one value to compare."""
        return self.course, self.title, self.due_date, self.kind, self.note


class CandidateDecision(BaseModel):
    """What a person chose about homework of the same class and title when adding a note,
    kept with the change: the choice, the homework that was shown, and the fingerprint of
    what was shown, so the choice can be read later against what it was made about."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    choice: PromotionChoice
    candidates: tuple[str, ...] = ()
    basis: str


class CaptureEvent(BaseModel):
    """One change to a note: what it was, what stood before and after, when, and who."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    capture_id: str
    operation: CaptureOperation
    before: CaptureSnapshot | None
    """``None`` for the first save only."""
    after: CaptureSnapshot
    revision: int
    """The note's revision once this change was made."""
    occurred_at: AwareDatetime
    occurred_on: date
    authored_by: Author
    sequence: int | None = None
    """Its place in the file's order; ``None`` until it is written."""
    decision: CandidateDecision | None = None
    """What was chosen about homework already on record, for a note added to homework."""

    @model_validator(mode="after")
    def _is_whole(self) -> Self:
        if (self.operation == CREATE) != (self.before is None):
            msg = f"note event {self.event_id!r}: only a first save has nothing before it"
            raise ValueError(msg)
        return self


class UnsoundCaptureHistory(UnreadableCapture):
    """Raised when a note's changes do not make one line from its first save to the note as
    it stands. Each row may read on its own and the line still be broken; such a note is as
    unavailable as one whose row cannot be read, and nothing is added to the line."""

    def __init__(self, capture_id: str, reason: str) -> None:
        super().__init__(f"the changes of note {capture_id!r} are not one line: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class CaptureHistoryReading:
    """A note's changes, the first save first, read and found to be one sound line."""

    events: tuple[CaptureEvent, ...]

    @property
    def head(self) -> CaptureEvent:
        """The latest change, which a save that wrote nothing names as what it found."""
        return self.events[-1]

    @property
    def accepted(self) -> CaptureEvent | None:
        """The change that put the note in homework, or ``None`` for a note still waiting:
        the press that was accepted, with the choice made and everything it carried. A
        later press is the same press only when it is this one again, whatever has become
        of the note's own words, class, and day since."""
        return accepted_press(self.events)


def accepted_press(events: Sequence[CaptureEvent]) -> CaptureEvent | None:
    """The latest change that added a note to homework or joined it to homework on record,
    unless the note was unlinked since: then there is none, and the note waits again."""
    for made in reversed(events):
        if made.operation == UNLINK:
            return None
        if made.operation in (PROMOTE, LINK):
            return made
    return None


@dataclass(frozen=True)
class AcceptedSearchPress:
    """The press that joined a note to homework found by search, as its events keep it: the
    homework named, the row as it was shown, the revision the page showed, and for a move
    the homework left. A later press is that press again when it carries the same four,
    whatever the note's words, class, or day have become since."""

    target: str
    basis: str
    revision_before: int
    left: str | None


def accepted_search_press(events: Sequence[CaptureEvent]) -> AcceptedSearchPress | None:
    """The press by search that put the note where it is, or ``None``: none for a note that
    waits, one unlinked since, one joined by a candidate, or one that made its own
    assignment. A move is its link and the unlink just before it, read together."""
    made = accepted_press(events)
    if (
        made is None
        or made.operation != LINK
        or made.decision is None
        or made.decision.choice != "found"
        or made.after.assignment_id is None
    ):
        return None
    place = next(index for index, event in enumerate(events) if event is made)
    before = events[place - 1] if place > 0 else None
    if before is not None and before.operation == UNLINK and before.before is not None:
        return AcceptedSearchPress(
            made.after.assignment_id,
            made.decision.basis,
            before.revision - 1,
            before.before.assignment_id,
        )
    return AcceptedSearchPress(
        made.after.assignment_id, made.decision.basis, made.revision - 1, None
    )


def same_press(
    accepted: CaptureEvent | None,
    details: CaptureDetails,
    choice: PromotionChoice,
    assignment_id: str | None,
) -> bool:
    """Whether a press is the accepted one again: the same kind of press, the same choice
    about homework already on record, the same assignment, and the same five details as the
    rules keep them. The assignment's id alone says which note and nothing about the press,
    since every assignment made from one note has the one id."""
    return (
        accepted is not None
        and accepted.decision is not None
        and accepted.operation == (LINK if choice in ("same", "found") else PROMOTE)
        and accepted.decision.choice == choice
        and accepted.after.assignment_id == assignment_id
        and accepted.after.details
        == (details.course, details.title, details.due_date, details.kind, details.note)
    )


def _kept_as_written(capture_id: str, snapshot: CaptureSnapshot) -> CaptureWords:
    """A snapshot's words and details under the rules a note's are held to, by the same
    checks a note's own row is read with."""
    try:
        details_as_held(
            snapshot.course,
            snapshot.title,
            snapshot.due_date,
            snapshot.kind,
            snapshot.note,
            of="the details of a change",
        )
        return words_as_held(
            snapshot.text, snapshot.course, snapshot.due_date, of="the words of a change"
        )
    except ValueError as fault:
        raise UnsoundCaptureHistory(capture_id, "a change holds words a note cannot") from fault


def _is_what_its_kind_does(capture_id: str, change: CaptureEvent) -> None:
    """Each kind of change does one thing and leaves the rest as it was.

    A first save is not archived, holds no details but a class and a day,
    and names no assignment. An edit changes her words, class, or day on a
    note that is not archived. An archive and a restore move a note one way
    each. Details change details, on a note that waits: not archived, and not
    yet homework. Adding to homework and joining homework name an assignment
    on a note that named none, and carry the choice that was made, which is
    what they did: adding makes the note's own assignment, by the choice of
    new work where nothing was shown or of a separate assignment where
    something was, with a class, a title, and a kind on the note by then,
    which it may settle in the same act; joining names one of the homework
    that was shown, by the choice of the same, with those three settled the
    same way, or by the choice of homework found by search, which changes no
    detail. Nothing else carries a choice. An unlink takes the assignment
    away from a note that was joined and changes nothing else, so the note
    waits again. No change but an edit touches her words, and nothing but an
    unlink ever takes an assignment away.
    """
    before, after = change.before, change.after
    decision = change.decision
    choice = None if decision is None else decision.choice
    shown = () if decision is None else decision.candidates
    if before is None:
        sound = (
            not after.archived
            and after.assignment_id is None
            and (after.title, after.kind, after.note) == (None, None, None)
        )
    else:
        same_words = before.words == after.words
        same_text = before.text == after.text
        same_details = before.details == after.details
        same_rest = (before.title, before.kind, before.note) == (
            after.title,
            after.kind,
            after.note,
        )
        same_link = before.assignment_id == after.assignment_id
        moved = (before.archived, after.archived)
        waiting = moved == (False, False) and before.assignment_id is None
        named = waiting and after.assignment_id is not None and same_text
        settled = None not in (after.course, after.title, after.kind)
        own = after.assignment_id == derived_assignment_id(capture_id)
        added = (
            named
            and settled
            and own
            and ((choice == "new" and not shown) or (choice == "separate" and bool(shown)))
        )
        joined = (
            named
            and after.assignment_id in shown
            and (
                (choice == "same" and settled)
                or (choice == "found" and same_details and shown == (after.assignment_id,))
            )
        )
        left = (
            before.assignment_id is not None
            and after.assignment_id is None
            and same_words
            and same_details
            and moved == (False, False)
        )
        sound = {
            EDIT: not same_words and same_rest and same_link and moved == (False, False),
            ARCHIVE: same_words and same_rest and same_link and moved == (False, True),
            RESTORE: same_words and same_rest and same_link and moved == (True, False),
            CLARIFY: same_text and not same_details and same_link and waiting,
            PROMOTE: added,
            LINK: joined,
            UNLINK: left,
        }.get(change.operation, False)
    if (decision is not None) != (change.operation in (PROMOTE, LINK)):
        sound = False
    if not sound:
        raise UnsoundCaptureHistory(
            capture_id, f"revision {change.revision} is no {change.operation}"
        )


def sound_history(note: Capture, events: Sequence[CaptureEvent]) -> CaptureHistoryReading:
    """A note's changes as one line, or ``UnsoundCaptureHistory``.

    The first is the first save, at revision 1, with nothing before it and
    what that save sent after it, at the place among all notes the file gave
    it. Each later change is this note's, one revision on, in the file's
    order, starts where the one before ended, and is what its kind does.
    Every snapshot is within the rules a note's words are held to. The last
    ends at the note as it stands, at its revision. Days and times are not
    compared, since a clock may run backward and the line be sound.

    A change about to be written is checked the same way, as the last of the
    line with the note as it will stand, before anything is written.
    """
    name = note.capture_id
    if not events:
        raise UnsoundCaptureHistory(name, "there is no first save")
    first = events[0]
    if first.operation != CREATE or first.revision != 1:
        raise UnsoundCaptureHistory(name, "the first change is not a first save at revision 1")
    if _kept_as_written(name, first.after) != note.initial:
        raise UnsoundCaptureHistory(name, "the first save is not what the note says it sent")
    if first.sequence is not None and first.sequence != note.created_order:
        raise UnsoundCaptureHistory(
            name, "the note's place is not the one its first save was given"
        )
    previous: CaptureEvent | None = None
    for place, change in enumerate(events, start=1):
        if change.capture_id != name:
            raise UnsoundCaptureHistory(name, "a change belongs to another note")
        if change.revision != place:
            raise UnsoundCaptureHistory(name, f"revision {place} is missing or comes twice")
        _kept_as_written(name, change.after)
        if previous is not None:
            if change.operation == CREATE or change.before != previous.after:
                raise UnsoundCaptureHistory(
                    name, f"revision {place} does not start where the one before ended"
                )
            if (
                previous.sequence is not None
                and change.sequence is not None
                and change.sequence <= previous.sequence
            ):
                raise UnsoundCaptureHistory(name, "the changes are not in the file's order")
        if change.operation == UNLINK:
            joined_by = accepted_press(events[: place - 1])
            if joined_by is None or joined_by.operation != LINK:
                raise UnsoundCaptureHistory(
                    name, f"revision {place} unlinks a note that was not joined"
                )
        _is_what_its_kind_does(name, change)
        previous = change
    last = events[-1]
    if last.revision != note.revision or last.after != CaptureSnapshot.of(note):
        raise UnsoundCaptureHistory(name, "the changes do not end at the note as it stands")
    return CaptureHistoryReading(tuple(events))


Remaining = Literal["needs", "choice", "ready"]
"""What a waiting note needs before it can be in a plan: a class and a title, a choice about
homework of that class and title already on record, or nothing more than adding it."""


def what_remains(
    notes: Iterable["Capture"], on_record: Collection[tuple[str, str]]
) -> dict[str, Remaining]:
    """What each note still waiting needs, by its id. ``on_record`` is the class and title of
    every assignment on record, paired by the rule the school's paste pairs by. A note that is
    archived or in homework needs nothing and is left out. Nothing is read from her words."""
    remains: dict[str, Remaining] = {}
    for note in notes:
        if not note.outstanding:
            continue
        if note.course is None or note.title is None:
            remains[note.capture_id] = "needs"
        elif pair(note.course, note.title) in on_record:
            remains[note.capture_id] = "choice"
        else:
            remains[note.capture_id] = "ready"
    return remains


NOTE_ASSIGNMENTS: Final = uuid.UUID("c2f4a8d1-6b3e-4a97-8d05-1e7b9c3a5f42")
"""The namespace an assignment made from a note is named in."""


def derived_assignment_id(capture_id: str) -> str:
    """The id of the assignment a note becomes: drawn from the note's own id, which never
    changes, and from nothing that can, so the same note can only ever become the same
    assignment, however often the press is sent and whatever its title is by then."""
    name = capture_id_from(capture_id)
    return f"assignment-from-note-{uuid.uuid5(NOTE_ASSIGNMENTS, name).hex[:16]}"


class CandidateLike(Protocol):
    """Homework on record shown as a candidate: which it is, and every value its row shows,
    in a fixed order and spelling. ``blossom.candidates`` makes them; a page shows a row
    from one and the fingerprint is made from the same one, so the two cannot differ."""

    @property
    def assignment_id(self) -> str:
        """The id of the assignment shown."""
        ...

    def facts(self) -> list[object]:
        """Every value the row shows, the id first, in a fixed order and spelling."""
        ...


def candidate_basis(candidates: Sequence[CandidateLike]) -> str:
    """A fingerprint of the homework shown as candidates: which, and everything about each
    that the page showed, what she and the school currently say and where the record came
    from included. The page sends it back with the choice, and the save compares it with the
    candidates as they stand inside its own transaction, so a choice made about homework that
    has since arrived, left, or changed in anything shown is put to the person again. The
    order the candidates came in is no part of it."""
    shown = sorted((item.facts() for item in candidates), key=lambda facts: str(facts[0]))
    serialized = json.dumps(shown, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CapturePromoted:
    """The note is homework now: an assignment of its own was made, or it was joined to one
    already on record. ``created`` says which."""

    capture: Capture
    event: CaptureEvent
    assignment_id: str
    created: bool


@dataclass(frozen=True)
class CaptureAlreadyPromoted:
    """The same press again: the note already names that assignment, nothing was written,
    and ``head`` is the latest change of a line found sound."""

    capture: Capture
    head: CaptureEvent
    assignment_id: str


@dataclass(frozen=True)
class DetailsMissing:
    """The note has no class, no title, or neither, so it cannot be homework yet. Nothing is
    made up for either, and nothing was written."""

    capture: Capture
    missing: tuple[str, ...]


@dataclass(frozen=True)
class ChoiceNeeded:
    """Homework of the same class and title is on record and no choice that fits it was
    made: none at all, or one naming homework that is no candidate. Nothing was written."""

    capture: Capture
    candidates: tuple[CandidateLike, ...]


@dataclass(frozen=True)
class CandidatesChanged:
    """The homework of that class and title is not what the page showed: some arrived, left,
    or changed. Nothing was written, and ``candidates`` is what stands now."""

    capture: Capture
    candidates: tuple[CandidateLike, ...]


@dataclass(frozen=True)
class CaptureCreated:
    """The note was saved for the first time."""

    capture: Capture
    event: CaptureEvent


@dataclass(frozen=True)
class CaptureAlreadyCreated:
    """The same form was sent again: the note exists, nothing was written, and ``capture`` is
    the note as it stands now, edited or archived as it may since have been. ``head`` is the
    latest change of the note's line of changes, read and found sound in the transaction
    that found nothing to do, so a page can say what this save found from an id only the
    record gives out."""

    capture: Capture
    head: CaptureEvent


@dataclass(frozen=True)
class CaptureIdTaken:
    """The id is a note's already and what was sent is not what that note's first save
    sent: nothing was written."""

    capture: Capture


@dataclass(frozen=True)
class CaptureChanged:
    """An edit, an archive, or a restore was made."""

    capture: Capture
    event: CaptureEvent


@dataclass(frozen=True)
class CaptureUnchanged:
    """What was asked for is what stands: nothing was written. ``head`` is the latest change
    of the note's line of changes, read and found sound in the transaction that found
    nothing to do."""

    capture: Capture
    head: CaptureEvent


@dataclass(frozen=True)
class CaptureConflict:
    """The note changed since the page was made, or cannot take this change as it stands
    now: nothing was written, and ``capture`` is the note as the refusing transaction
    read it."""

    capture: Capture


@dataclass(frozen=True)
class CaptureUnlinked:
    """The note left the homework it was joined to and waits again, its details kept; its
    claims on that homework count no more, and nothing else about the homework changed."""

    capture: Capture
    event: CaptureEvent
    assignment_id: str


@dataclass(frozen=True)
class CaptureAlreadyUnlinked:
    """The press was this unlink again: nothing was written. ``head`` is the unlink."""

    capture: Capture
    head: CaptureEvent
    assignment_id: str


@dataclass(frozen=True)
class CaptureNotJoined:
    """The note is not joined to homework that was on record before it: it waits, or it made
    an assignment of its own, which is not moved this way. Nothing was written."""

    capture: Capture


@dataclass(frozen=True)
class HomeworkGone:
    """The homework chosen is not on record now. Nothing was written."""

    capture: Capture
    assignment_id: str
