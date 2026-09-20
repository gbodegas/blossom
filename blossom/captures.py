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

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator, model_validator

from blossom.authored_text import multiline, single_line
from blossom.reconciliation import SourceChannel

CAPTURE_TEXT_MAX_LENGTH: Final = 500
CAPTURE_COURSE_MAX_LENGTH: Final = 60

Author = Literal["student", "parent", "household"]
"""Who made a change. ``household`` is every change made while the sign-in is off, when a
page cannot say which person pressed; it is never a guess at one of the other two."""
STUDENT: Final = "student"
PARENT: Final = "parent"
HOUSEHOLD: Final = "household"

CaptureOperation = Literal["create", "edit", "archive", "restore"]
CREATE: Final = "create"
EDIT: Final = "edit"
ARCHIVE: Final = "archive"
RESTORE: Final = "restore"

ATTRIBUTED: Final = ("course", "due_date")
"""The optional fields a note accepts now, each with who supplied what stands in it."""


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
    """``title``, ``kind``, and ``note`` are for the step that makes a note homework. Nothing
    sets them yet, and nothing infers them from her words."""
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
        CaptureWords(text=self.text, course=self.course, due_date=self.due_date)
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

    @classmethod
    def of(cls, capture: Capture) -> "CaptureSnapshot":
        """What stands on a note now, as one moment of its history."""
        return cls(
            text=capture.text,
            course=capture.course,
            due_date=capture.due_date,
            archived=capture.archived,
        )


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

    @model_validator(mode="after")
    def _is_whole(self) -> Self:
        if (self.operation == CREATE) != (self.before is None):
            msg = f"note event {self.event_id!r}: only a first save has nothing before it"
            raise ValueError(msg)
        return self


@dataclass(frozen=True)
class CaptureCreated:
    """The note was saved for the first time."""

    capture: Capture
    event: CaptureEvent


@dataclass(frozen=True)
class CaptureAlreadyCreated:
    """The same form was sent again: the note exists, nothing was written, and ``capture`` is
    the note as it stands now, edited or archived as it may since have been. ``head`` is the
    latest change of the note, read in the transaction that found nothing to do, so a page
    can say what this save found from an id only the record gives out."""

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
    of the note, read in the transaction that found nothing to do."""

    capture: Capture
    head: CaptureEvent


@dataclass(frozen=True)
class CaptureConflict:
    """The note changed since the page was made, or cannot take this change as it stands
    now: nothing was written, and ``capture`` is the note as the refusing transaction
    read it."""

    capture: Capture
