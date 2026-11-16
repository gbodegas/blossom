# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""What a parent pastes or types, read into what the record can keep.

The school's portal writes its homework page and its weekly summary in two
small sets of line shapes, and its "Missing" email in a third. A parent
pastes any of them as text, and this module reads the shapes into
assignments, into each channel's claim about a due date, into what the
teacher wrote under a card, and into what the school reports about an
assignment's status, with no credential, no scraper, and no network.
Nothing is written here: what was read is shown to the parent first, and
put on record only when they say so. A line the reader does not understand
is kept, with its line number, and shown as text that needs review, since a
line dropped in silence is an assignment lost; a line that looks like a card
or a day but does not read as one is never taken for a teacher's words.

The shapes, as the portal writes them:

- The homework page: a day header, ``Tuesday 9/1/2026``, then cards, each a
  course on one line and on the next either ``Assigned: <Title>:
  (Due:MM/DD/YYYY)`` on the day the work was given or ``Due: <Title>:`` on
  the day it is due, where the header is the date.
- The weekly summary: a heading ``Homework for <first name>``, then a day
  line, ``09/01/2026 - Tuesday``, with or without a bullet before it, then
  the same cards with the course and the card on one line, ``<Course> -
  Assigned: <Title>: (Due:MM/DD/YYYY)`` or ``<Course> - Due: <Title>:``. A
  teacher's instruction may follow either kind of card, on lines of its own,
  and is kept with the assignment as written, line breaks and all. A
  backslash ending a line is the download's line break and is dropped; a
  backslash anywhere else is the teacher's. What a teacher types for a
  subject, apart from its cards, is written under the subject and a hyphen
  on a line of their own, ``<Course> -``. That heading ends the card before
  it, whose subject may be another, and opens a note. The note holds the
  heading and every line after it, across blank lines and whatever a line
  looks like, until a line that reads as a day, as a Missing line, or as a
  card with its course on its line, which is read as what it is, or, where
  the text opened with the summary's heading, until that same line over its
  next week. A line only shaped like a day or a card stays with the note.
  The heading is the summary's shape: where the homework page's shape is in
  force, which only a day written as the page writes it with digits that
  name a date puts in force, a line that ends in a hyphen is read as any
  other line. A short line of a card's own instruction that ends in such a
  hyphen reads as a heading too, and opens a note of what follows it.
- The email: ``MM/DD <Course> - <Section>: <Category>: <Title> Grade:
  Missing``, one line per assignment, under ``Assignments:``. Each such line
  is the school reporting the assignment missing, kept as a report with the
  day: the email's own date when the paste carries its date line, otherwise
  the day it was pasted, and the report says which. A date line kept for
  review in a teacher's note, or shaped as a subject heading, dates no
  report: the reports after it are dated by the day of the paste until a
  date line is read as one. The date beside the
  assignment is kept as the text it is, since the email does not say what it
  means; it is not a due date. Lines about work the text gives no date are
  read apart, since each can be about different homework under the name. A
  line in that shape with any other grade is not the "Missing" email and is
  left for review.

A teacher's note is read as tasks by the rules ``teacher_notes`` holds, each
with the words it was read from, a standing (homework, not an assignment, or
unclear), and a deadline only where its words settle one date. A task with
the class and title of a card read under a school day the task is listed
under is that card's assignment: the card takes the claim of a deadline the
note settles and, where they say more than the name and the deadline
statement, the task's words as one more instruction. Every other task is a
reading of its own, its words kept as the school's instruction under the
first school day it is listed under, with no card. A reading of a task that
is unclear or not an assignment is left out of a save unless someone selects
it. Before a save the family may correct a task's title and due date; a due
date other than the one the note settles is the family's entry, not the
school's claim. A task saved before is found again by its class, its words
and that first school day, whatever its title or due date, and its class and
title alone never put it on a row. A note with no words under its heading,
or with a line only shaped like a day, a Missing line, a card or a mail
program's date line, is read as no task, and its lines are text that needs
review.

One item appears under an assigned day and under a due day, often in two
weeks, and is one assignment matched by its course and title, exactly as the
portal writes them: the date in its own line and the date in the header are
two claims from the same channel, told apart by where each was read. The
record is matched the same way, by the pair, so an assignment already on
record under any id takes the claims rather than a twin. Work that comes
round again under the same name, a weekly practice, is the one case the
pair cannot settle: when a text names an assignment again a week or more
from its first date, in the text itself or against the record, the parent
is asked whether it is the same assignment with its due date changed or
new work, and the answer is kept: an update moves the saved date, and new
work gets a row of its own that the next paste finds by its date. A new
row's id is the title made readable plus a hash of the pair, so two titles
that read alike never become one. Titles are kept as the portal writes them, punctuation and all;
a course is kept as written too, grade prefix included, since that is how
the portal names it everywhere. Forms to sign, books to cover, and supplies
to bring are tasks, not homework, and a parent can say otherwise. The
heading's first name is read as nothing and never kept.
"""

import dataclasses
import functools
import hashlib
import json
import re
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Final, NamedTuple

from blossom.candidates import readings_for
from blossom.captures import Author, Capture, CaptureNotSaved, CapturePromoted, candidate_basis
from blossom.pairing import pair as pair  # the one rule, kept where both askers share it
from blossom.reconciliation import CHANNEL_NAMES, SourceChannel, SourceRecord
from blossom.school_instructions import (
    Card,
    InstructionChoice,
    InstructionChoiceStale,
    InstructionSeen,
    InstructionsNeedAChoice,
    InstructionsOutcome,
    InstructionsSettled,
    SchoolInstruction,
    SubmittedChoice,
    new_texts,
    overtaken,
    revision_of,
    settle,
)
from blossom.stores.intake_decisions import (
    CREATION,
    DecisionKind,
    DecisionToKeep,
    IntakeDecision,
)
from blossom.stores.project_state import (
    Assignment,
    AssignmentKind,
    ProjectStateStore,
    StatusReport,
)
from blossom.stores.school_instructions import InstructionReadings, UnreadableInstruction
from blossom.teacher_notes import NoteBlock, NoteTask, Standing, read_notes

PORTAL_CONFIDENCE: Final = 0.9
"""The portal's own page, pasted whole: the school's word, as it wrote it."""
FAMILY_CONFIDENCE: Final = 0.8
"""A parent's own entry: sure of the item, less sure of the date they typed."""

OWN_LINE: Final = "the assignment's own line"
DAY_HEADER: Final = "the day's header"
TEACHER_NOTE: Final = "the teacher's note"
EMAIL_DATE_LINE: Final = "the email's date line"
PASTE_DAY: Final = "the day it was pasted"

NOTE_MAX_LENGTH: Final = 500
"""How long a note a parent types may be; a teacher's is kept as the portal shows it."""
ANOTHER_OCCURRENCE: Final = timedelta(days=7)
"""How far past the recorded due date a pasted one must be to ask whether it is new work."""
UPDATE: Final = "update"
NEW_WORK: Final = "new"
KEEP_DATE: Final = "keep"
"""The answer that a reading made from a teacher's note is the same assignment and that
the assignment keeps its due date: the note's own date is kept as a claim beside it."""
DIFFERENT: Final = "different"
WOULD_BE_NEW: Final = "new"
"""How a card that would save a new assignment stands when it offers to find homework
already here under another name."""
"""The review form's word for different homework with the same title. Homework on record
is sent with a prefix, so no id can be read as this word."""
KEPT_NOTE: Final = (
    "Her note will stay. Your note won't be saved to this assignment. The other changes can "
    "still be saved."
)

TASK_THING: Final = re.compile(
    r"\b(?:syllabus|binders?|supplies|permission|dividers|book\s+covers?)\b", re.IGNORECASE
)
"""Things a title names that make it a task: paperwork and materials, not a sitting."""
TASK_DOING: Final = re.compile(
    r"\b(?:sign(?:s|ed|ing|ature)?|cover(?:s|ed|ing)?|bring(?:s|ing)?|brought)\b",
    re.IGNORECASE,
)
"""Doings that make a title a task only with a thing to do them to."""
TASK_THING_FOR_DOING: Final = re.compile(
    r"\b(?:form|forms|syllabus|slip|sheet|paper|papers|contract|agreement|book|books|"
    r"textbook|textbooks|materials|supplies|binder|folder|notebook)\b",
    re.IGNORECASE,
)
"""The things a signing, covering, or bringing must name for the title to be a task."""

NOISE: Final = frozenset(
    {
        "previous",
        "next",
        "homework",
        "homework by date",
        "homework by subject",
        "assignments:",
        "student home",
        "classes",
    }
)
"""Lines the portal writes around the cards, read as nothing rather than as unread."""

WEEKDAYS: Final = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
DAY_LINE: Final = re.compile(
    rf"^(?:{WEEKDAYS})\s+(?P<month>\d{{1,2}})/(?P<day>\d{{1,2}})/(?P<year>\d{{4}})\s*$"
)
SUMMARY_DAY_LINE: Final = re.compile(
    rf"^(?:[-*•]\s*)?(?P<month>\d{{1,2}})/(?P<day>\d{{1,2}})/(?P<year>\d{{4}})"
    rf"\s*-\s*(?:{WEEKDAYS})\s*$"
)
WEEK_LINE: Final = re.compile(r"^Week of\s+\d{1,2}/\d{1,2}/\d{4}\s*$", re.IGNORECASE)
HEADING_LINE: Final = re.compile(r"^Homework for\b", re.IGNORECASE)
"""The summary's heading, which carries her first name: read as nothing, outside a card."""
CARD_LINE: Final = re.compile(r"^(?P<course>.+?)\s+-\s+(?P<card>(?:Assigned|Due)\s*:.*)$")
"""The summary's card, course and card on one line, split at the first joining dash."""
SUBJECT_LINE: Final = re.compile(r"^.*[^\W_].*\s-$")
"""The summary's heading over what a teacher typed for a subject: the subject, with a letter
or a digit in it, then a hyphen that ends the line."""
ASSIGNED_LINE: Final = re.compile(
    r"^Assigned:\s*(?P<title>.+):\s*\(Due:\s*(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})\)\s*$"
)
DUE_LINE: Final = re.compile(r"^Due:\s*(?P<title>.*?):?\s*$")
LOOKS_STRUCTURAL: Final = re.compile(
    r"^(?:(?:[-*•]\s*)?\d{1,2}/\d{1,2}/\d{2,4}\b|(?:.+?\s+-\s+)?(?:Assigned|Due)\s*:|.*\(Due\s*:)",
    re.IGNORECASE,
)
"""A line shaped like a day, a card, or a due date: if it does not read as one, it is text
that needs review, never a teacher's words."""
EMAIL_LINE: Final = re.compile(
    r"^(?P<date>\d{1,2}/\d{1,2})\s+(?P<course>.+?)(?:\s+-\s+\S+)?:\s+"
    r"(?P<category>[^:]+?):\s+(?P<title>.+?)\s+Grade:\s+(?P<grade>.+?)\s*$"
)
MISSING: Final = "missing"
"""The one grade the school's email is read for, kept as the status it reports."""
MONTHS: Final = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
EMAIL_DATE: Final = re.compile(
    r"\b(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(?P<day>\d{1,2}),\s+(?P<year>\d{4})\b"
)
"""A date as a mail program writes it, ``Sep 9, 2026`` or ``September 9, 2026``, on the
email's date line: the day the school reported, when the paste carries that line."""
MAIL_DATE_LINE: Final = re.compile(
    r"^(?:(?:Date|Sent)\s*:|On\s+(?=(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun))|"
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,)",
    re.IGNORECASE,
)
"""The lines a mail program dates a message on: ``Date:`` or ``Sent:`` in a header, a
forwarded ``On Tue, Sep 8, 2026 at 9:14 AM ... wrote:``, or the line ``Tue, Sep 8, 2026``
itself. A date in a title or an instruction is never the email's."""
MAIL_HEADER: Final = re.compile(r"^(?:From|To|Cc|Subject)\s*:", re.IGNORECASE)
"""The other lines of a mail header, read as nothing outside a card."""
COURSE_LENGTH: Final = 60
"""A course line is short; a longer plain line is a teacher's instruction or a stray."""
TEXT_MAX_LENGTH: Final = 40_000
"""How much one paste may hold: a page or a week is a few thousand characters."""
IDENTITY: Final = uuid.UUID("5b0f9b2e-2a3c-4d0e-9b7a-0b2f8a1c6d33")
"""The namespace an assignment's id is drawn from, so the same pair always gives the same id."""


def slug(text: str) -> str:
    """Lowercase letters and digits joined by hyphens, and nothing else."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def identity(course: str, title: str, occurrence: str | None = None) -> str:
    """A stable id for a new row: the title made readable, and a hash of the exact pair.

    The hash keeps two titles that read alike apart, ``Quiz A/B`` and
    ``Quiz A-B`` among them, and keeps the id the same length however long
    the title runs. ``occurrence`` tells a second round of work under the
    same name from the first, and is given only when a parent has said the
    work is new.
    """
    course, title = pair(course, title)
    tag = uuid.uuid5(IDENTITY, f"{course}\n{title}\n{occurrence or ''}").hex[:8]
    return f"assignment-{slug(title)[:40].rstrip('-') or 'untitled'}-{tag}"


@dataclass(frozen=True)
class Reading:
    """One assignment as the pasted text describes it, and what it claims about the date."""

    course: str
    title: str
    due_date: date | None
    assigned_on: date | None
    kind: AssignmentKind
    claims: tuple[SourceRecord, ...]
    origin: SourceChannel
    """Which channel the reading came from: the portal, the school's email, or a parent."""
    note: str | None = None
    """What a parent typed with an entry, as written: a parent's own note. The school's
    words under a card are never a note; they are ``instructions``."""
    instructions: tuple[InstructionSeen, ...] = ()
    """The teacher's instruction under each card the assignment was read from, once per
    text, each with the card it was under and that card's day."""
    reports: tuple[StatusReport, ...] = ()
    """What the school reports about the assignment's status, with the day."""
    at_line: int | None = None
    """The first line of the pasted text this reading was read from, counted from one."""
    occurrence: str | None = None
    """The due date this round of a repeated name was read with, when the same text names
    the assignment again a week or more from its first date; ``None`` for the first."""
    field_origins: Mapping[str, SourceChannel] = field(default_factory=dict)
    """Where each field came from when that is not the reading's own channel: a text that
    holds the school's email and the portal's page names the assignment from the email and
    dates it from the page, and each fact keeps the channel that gave it."""
    teacher_note: NoteTask | None = None
    """The task a reading made from a teacher's note was read as; ``None`` for a reading
    made from a card, the school's email, or a parent's entry."""
    joined_notes: tuple[NoteTask, ...] = field(default=(), compare=False)
    """The tasks of teachers' notes that are this card's assignment, each with its words,
    so a note that says no more than the card's name and a deadline has its words with the
    card. Two readings are compared without it."""

    @property
    def pair(self) -> tuple[str, str]:
        """What the reading is matched by, in the paste and against the record."""
        return pair(self.course, self.title)

    kind_chosen: bool = False
    """Whether a parent chose the kind on the entry form. A kind left unchosen is the saved
    row's when the entry lands on one, and the title's suggestion for a new row, and is
    no choice either way."""

    def origin_of(self, name: str) -> SourceChannel:
        """The channel a field came from: its own when it has one, else the reading's."""
        return self.field_origins.get(name, self.origin)

    @property
    def assignment_id(self) -> str:
        """The id a new row would have; a row already on record keeps its own."""
        return identity(self.course, self.title, self.occurrence)

    @property
    def reported_status(self) -> str | None:
        """The status the school reports, when it reports one."""
        return self.reports[-1].status if self.reports else None

    def assignment(
        self,
        *,
        assignment_id: str | None = None,
        kind: AssignmentKind | None = None,
        kind_by: SourceChannel | None = None,
    ) -> Assignment:
        """The record's row for a reading that is new to it, with where each fact came from."""
        origins = {"record": self.origin}
        if self.note:
            origins["note"] = self.origin_of("note")
        if self.due_date is not None:
            origins["due_date"] = self.origin_of("due_date")
        if self.assigned_on is not None:
            origins["assigned_on"] = self.origin_of("assigned_on")
        origins["kind"] = kind_by or self.origin
        return Assignment(
            assignment_id=assignment_id or self.assignment_id,
            course=self.course,
            title=self.title,
            due_date=self.due_date,
            dependencies=[],
            reported_submission_status=self.reported_status or "unknown",
            assigned_on=self.assigned_on,
            kind=kind or self.kind,
            note=self.note,
            origins=origins,
        )


@dataclass(frozen=True)
class Unread:
    """A line of the paste the reader did not take as anything, with where it was."""

    line: int
    text: str


@dataclass(frozen=True)
class NoteRead:
    """One subject's note as the reader collected it, with where it was in the paste.

    ``lines`` holds the line number of the note's heading and of every line kept in the
    note, ascending. Each of them is listed in ``Read.unread`` as well.
    """

    block: NoteBlock
    lines: tuple[int, ...]


NO_DUE_DATE: Final = "none"
"""The due-date answer for a reading made from a teacher's note that gives it no due date."""
ANOTHER_DATE: Final = "other"
"""The due-date answer that takes the date typed beside it."""


@dataclass(frozen=True)
class TaskAnswer:
    """What a review page sends for one reading made from a teacher's note, by its key, as
    typed: the title in its box, the due-date choice (a date, ``NO_DUE_DATE``,
    ``ANOTHER_DATE``, or empty for none chosen), and the date typed beside another date."""

    key: int
    title: str = ""
    when: str = ""
    other: str = ""


@dataclass(frozen=True)
class Read:
    """Everything one paste says: the readings, the lines that need review, each subject's
    note as a block, in the order the notes open, the keys of the readings left out of a
    save unless someone selects them, the title and due-date answers sent for the readings
    made from teachers' notes, as typed, the keys whose due-date answer names no date
    a paste can mean, and the notes joined to a card that the family selected to add to it."""

    items: tuple[Reading, ...]
    unread: tuple[Unread, ...]
    notes: tuple[NoteRead, ...] = ()
    left_out: frozenset[int] = frozenset()
    answers: tuple[TaskAnswer, ...] = ()
    dates_unread: frozenset[int] = frozenset()
    joined: frozenset[tuple[int, int]] = frozenset()
    """Each note joined to a card that is selected, by the card's key and the note's place
    among the card's ``joined_notes``. A joined note starts unselected."""


def kind_of(title: str) -> AssignmentKind:
    """A task for paperwork and materials; homework for everything else, including doubt.

    A thing a task names, a syllabus, a binder, supplies, book covers, makes
    a task on its own. A doing, signing, covering, bringing, makes one only
    with a thing to do it to, so a signed syllabus is a task and signed
    numbers practice is homework.
    """
    if TASK_THING.search(title):
        return AssignmentKind.TASK
    if TASK_DOING.search(title) and TASK_THING_FOR_DOING.search(title):
        return AssignmentKind.TASK
    return AssignmentKind.HOMEWORK


def a_date(year: str, month: str, day: str) -> date | None:
    """The date the digits name, or ``None`` for digits that name no date."""
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def mail_date(line: str) -> date | None:
    """The day a mail program's date line names, or ``None`` for any other line.

    Only a line in a mail program's own shape counts, so a date inside a
    title or an instruction, "Read September 8, 2026", dates nothing.
    """
    if not MAIL_DATE_LINE.match(line):
        return None
    found = EMAIL_DATE.search(line)
    if found is None:
        return None
    month = MONTHS.index(found.group("month").lower()) + 1
    return a_date(found.group("year"), str(month), found.group("day"))


def claim(
    channel: SourceChannel, value: date, seen_in: str | None, now: datetime, confidence: float
) -> SourceRecord:
    """One channel's claim about a date, made now."""
    return SourceRecord(
        channel=channel,
        asserted_value=value.isoformat(),
        observed_at=now,
        confidence=confidence,
        seen_in=seen_in,
    )


def spoken_day(day: date) -> str:
    """A day as the pages say it: ``Tuesday, September 8, 2026``."""
    return f"{day.strftime('%A, %B')} {day.day}, {day.year}"


def spoken_report(report: StatusReport) -> str:
    """The report's source and day as the pages say them, after the status itself."""
    where = CHANNEL_NAMES[report.channel]
    when = spoken_day(report.reported_on)
    how = "dated" if report.dated_by == EMAIL_DATE_LINE else "pasted"
    beside = ""
    if report.source_date_text:
        beside = (
            f" The email writes {report.source_date_text} beside it, which it does not explain."
        )
    return f"From the {where}, {how} {when}.{beside}"


@dataclass
class _Draft:
    """A reading being assembled while the lines are walked."""

    course: str
    title: str
    origin: SourceChannel
    at_line: int
    due_date: date | None = None
    assigned_on: date | None = None
    claims: list[SourceRecord] = field(default_factory=list)
    reports: list[StatusReport] = field(default_factory=list)
    report_lines: list[int] = field(default_factory=list)
    """The line each report was read from, in the same order."""
    notes: list[tuple[Card, date | None, list[str]]] = field(default_factory=list)
    """The instruction under each card the item was seen on, one block per card, with the
    card it was under, assigned or due, and that card's day."""
    occurrence: str | None = None
    field_origins: dict[str, SourceChannel] = field(default_factory=dict)
    """The channel that gave each dated field, which the portal's cards do; the draft's own
    ``origin`` is the channel that first named the assignment."""

    def add(self, record: SourceRecord) -> None:
        """Keep a claim once per text: the same channel, value, and place is one claim."""
        if all(_same_claim(record, kept) is False for kept in self.claims):
            self.claims.append(record)

    def report(self, report: StatusReport, line: int) -> None:
        """Keep a report once per text as written: a line repeated word for word is one
        report, and two lines of one day with different dates beside them are two."""
        if all(_same_report_as_written(report, kept) is False for kept in self.reports):
            self.reports.append(report)
            self.report_lines.append(line)

    def readings(self) -> list[Reading]:
        """What the draft reads as: a card's reading, or a reading for each of the school's
        Missing lines, since each line can be about different homework under the name and
        is placed on its own."""
        whole = self.reading()
        if not self.reports:
            return [whole]
        return [
            dataclasses.replace(whole, reports=(report,), at_line=line)
            for report, line in zip(self.reports, self.report_lines, strict=True)
        ]

    def reading(self) -> Reading:
        # Each card's instruction is its own, the same words under two cards once: an
        # instruction is read under a card, and cards are the portal's.
        instructions: list[InstructionSeen] = []
        for card, day, lines in self.notes:
            block = "\n".join(lines)
            if block and all(item.text != block for item in instructions):
                instructions.append(InstructionSeen(block, SourceChannel.LMS, card, day))
        return Reading(
            course=self.course,
            title=self.title,
            due_date=self.due_date,
            assigned_on=self.assigned_on,
            kind=kind_of(self.title),
            claims=tuple(self.claims),
            origin=self.origin,
            instructions=tuple(instructions),
            reports=tuple(self.reports),
            at_line=self.at_line,
            occurrence=self.occurrence,
            field_origins=dict(self.field_origins),
        )


def _same_claim(one: SourceRecord, other: SourceRecord) -> bool:
    return (one.channel, one.asserted_value, one.seen_in) == (
        other.channel,
        other.asserted_value,
        other.seen_in,
    )


def _same_report(one: StatusReport, other: StatusReport) -> bool:
    return (one.channel, one.status, one.reported_on) == (
        other.channel,
        other.status,
        other.reported_on,
    )


def _same_report_as_written(one: StatusReport, other: StatusReport) -> bool:
    """The same report from the same line of the school's text: two Missing lines of one day
    that begin with different dates are two reports."""
    return _same_report(one, other) and one.source_date_text == other.source_date_text


def read_text(text: str, *, now: datetime, today: date) -> Read:
    """Read pasted text in the portal's shapes or the email's, in one pass over its lines.

    A plain line just before a card's line is the card's course, on the
    homework page; the summary names the course on the card's own line.
    Plain lines just after a card are the teacher's instruction, until the
    next card, day, course line, or subject heading, or a blank; the same
    instruction under both of an item's days is kept once. Only a day
    written as the homework page writes it, with digits that name a date,
    puts the page's shape in force. A subject heading opens a note unless
    that shape is in force: each line after the heading is text that needs
    review, read as nothing else, across blank lines, until a line that
    reads as a day, a Missing
    line, or a card with its course on its line, which is read as what it
    is, or until the line the text opened with as the summary's heading,
    where a day whose digits name a date follows it. A line in a note that
    is itself a heading, or only shaped like a day, a Missing line or a
    card, stays with the note. A day whose digits name no date, there or
    outside a note, ends the day in force and, written the summary's way,
    the homework page's shape; written the page's way it leaves that shape
    as it was and starts none.
    Outside the homework page's shape, a line shaped as a heading is not read by
    the rules for plain text outside a card. Where a heading has been read,
    a card's line left for review for want of a course keeps the rest of its
    paragraph for review with it, but for a line there that reads as a day,
    a Missing line or a card with its course on its line, which is read as
    what it is. A text with no heading-shaped line outside the homework
    page's shape is read by the other rules alone. A line shaped like a day or a card
    that does not read as one is text that needs review, and it ends the
    card before it, so nothing after it is taken for that card's words. A
    mail program's date line outside a card dates the school's reports read
    after it, and the other lines of a mail header are read as nothing. One
    kept for review, in a note or as a heading, dates none: the reports
    after it are dated by the day of the paste until a date line is read
    as one. Each
    Missing line is a reading of its own, apart from any card of its name,
    and the same line twice is one. Any
    other plain line outside a card needs review too. A card that names an
    assignment again a week or more from the date it was first read with is
    another round of the same name and is read apart, so the parent can say
    what it is; a card repeated within a week is the same assignment, and a
    card repeated word for word is read once.

    Each subject's note is handed out as a block as well, in the order the
    notes open: the subject from its heading, the day in force where the
    heading is read, and the lines kept after the heading, a paragraph for
    each run of them between blank lines. A note with no day in force at its
    heading, or with no line kept after it, is no block, and neither is the
    paragraph kept under a card's line with no course, unless a subject
    heading is read in it. A block is marked suspect when a line of its note
    is shaped as a day, a Missing line, a card or a mail program's date line.
    Every line of a block stays listed for review.
    """
    lines = [_unbroken(line) for line in text.splitlines()]
    following = _next_lines(lines)
    reported_on, dated_by = today, PASTE_DAY
    drafts: dict[tuple[str, str], list[_Draft]] = {}
    unread: list[Unread] = []
    day: date | None = None
    course: str | None = None
    last: _Draft | None = None
    in_note = False
    to_blank = False
    headings_read = False
    page_day = False
    opening_heading: str | None = None
    opening = next((index for index, line in enumerate(lines) if line), None)
    notes: list[NoteRead] = []
    note: _OpenNote | None = None
    for index, line in enumerate(lines):
        number = index + 1
        if not line:
            last = None
            if to_blank:
                in_note = to_blank = False
            if note is not None:
                note.after_blank = True
            continue
        ends_note = False
        if in_note:
            if line == opening_heading and _names_a_day(following[index]):
                # The summary's own heading over its next week, the very line this text
                # opened with, as read, before a day whose digits name a date: it ends
                # the note and is read as nothing.
                in_note = to_blank = False
                _close(note, notes)
                note = None
                continue
            heading = not page_day and _is_subject_heading(line)
            if heading or not _ends_a_note(line, day):
                # In a note a line is the teacher's, whatever it looks like, unless it
                # reads as a day, a Missing line or a card. A day that is no date still
                # ends the day and the course in force, as it does outside a note, so no
                # card after it takes an earlier one. Written the summary's way it ends
                # the homework page's shape too; written the page's way it starts none,
                # since here it is a teacher's line and under that shape no heading is
                # read. A heading, which a line is only outside the homework page's
                # shape, makes the note a subject's.
                if _is_a_day(line):
                    day = course = None
                    page_day = page_day and bool(DAY_LINE.match(line))
                if heading:
                    to_blank = False
                    _close(note, notes)
                    note = _OpenNote.under(line, day, number)
                elif note is not None:
                    note.keep(number, line)
                if mail_date(line) is not None:
                    # A mail date line kept here may head another email, so an earlier
                    # email's date is not taken for the reports after it: they are dated
                    # by the day of the paste, and say so.
                    reported_on, dated_by = today, PASTE_DAY
                unread.append(Unread(number, line))
                continue
            # What ends a note is read below as what it is, past the rules for plain
            # text outside a card.
            in_note = to_blank = False
            _close(note, notes)
            note = None
            ends_note = True
        # The four rules below read plain text outside a card as nothing or as the
        # email's date. They pass over a line that ended a note and over a subject heading.
        plain = not ends_note and (page_day or not _is_subject_heading(line))
        if plain and (line.lower() in NOISE or WEEK_LINE.match(line)):
            last = None
            continue
        if plain and HEADING_LINE.match(line) and last is None:
            # The summary's heading, outside a card; under a card the same
            # words are a teacher's instruction and are kept as one.
            if index == opening:
                opening_heading = line
            continue
        if plain and last is None and (sent := mail_date(line)) is not None:
            # The email's date line, outside a card, dates the reports read
            # after it; under a card the same words are the teacher's.
            reported_on, dated_by = sent, EMAIL_DATE_LINE
            continue
        if plain and last is None and MAIL_HEADER.match(line):
            continue
        if day_match := DAY_LINE.match(line) or SUMMARY_DAY_LINE.match(line):
            day = a_date(day_match.group("year"), day_match.group("month"), day_match.group("day"))
            # The homework page's shape is put in force only by a day written the page's way
            # whose digits name a date. A day that names no date may be a teacher's line
            # under a card, and under the page's shape no subject heading is read: written
            # the summary's way it ends that shape, written the page's way it leaves the
            # shape as it was, as in a note.
            page_day = bool(DAY_LINE.match(line)) and (day is not None or page_day)
            course = None
            last = None
            if day is None:
                unread.append(Unread(number, line))
            continue
        if email := EMAIL_LINE.match(line):
            last = None
            if email.group("grade").strip().lower() != MISSING:
                unread.append(Unread(number, line))
                continue
            draft = _draft_for(
                drafts,
                email.group("course").strip(),
                email.group("title").strip(),
                SourceChannel.EMAIL,
                number,
                None,
            )
            draft.report(
                StatusReport(
                    status=MISSING,
                    channel=SourceChannel.EMAIL,
                    reported_on=reported_on,
                    dated_by=dated_by,
                    observed_at=now,
                    source_date_text=email.group("date"),
                ),
                number,
            )
            continue
        card_course, body = course, line
        if combined := CARD_LINE.match(line):
            card_course, body = combined.group("course").strip(), combined.group("card")
        if assigned := ASSIGNED_LINE.match(body):
            when = a_date(assigned.group("year"), assigned.group("month"), assigned.group("day"))
            if card_course is None or when is None:
                unread.append(Unread(number, line))
                in_note = to_blank = headings_read and card_course is None
                last = None
                continue
            draft = _draft_for(
                drafts,
                card_course,
                assigned.group("title").strip(),
                SourceChannel.LMS,
                number,
                when,
            )
            draft.add(claim(SourceChannel.LMS, when, OWN_LINE, now, PORTAL_CONFIDENCE))
            if draft.due_date is None:
                draft.due_date = when
                draft.field_origins["due_date"] = SourceChannel.LMS
            if draft.assigned_on is None and day is not None:
                draft.assigned_on = day
                draft.field_origins["assigned_on"] = SourceChannel.LMS
            draft.notes.append(("assigned", day, []))
            last = draft
            continue
        if due := DUE_LINE.match(body):
            title = due.group("title").strip()
            if card_course is None or day is None or not title:
                unread.append(Unread(number, line))
                in_note = to_blank = headings_read and card_course is None
                last = None
                continue
            draft = _draft_for(drafts, card_course, title, SourceChannel.LMS, number, day)
            draft.add(claim(SourceChannel.LMS, day, DAY_HEADER, now, PORTAL_CONFIDENCE))
            if draft.due_date is None:
                draft.due_date = day
                draft.field_origins["due_date"] = SourceChannel.LMS
            draft.notes.append(("due", day, []))
            last = draft
            continue
        if LOOKS_STRUCTURAL.match(line):
            # Shaped like a day, a card, or a due date, and not read as one:
            # for review, and never a teacher's words.
            unread.append(Unread(number, line))
            last = None
            continue
        if not page_day and _is_subject_heading(line):
            # The summary's heading over a teacher's note: neither it nor the note is
            # the words of the card before it, whose subject may be another.
            unread.append(Unread(number, line))
            if mail_date(line) is not None:
                # A mail date line shaped as a heading is kept for review with its note,
                # as one kept in a note is, so the reports after it are dated by the day
                # of the paste, and say so.
                reported_on, dated_by = today, PASTE_DAY
            note = _OpenNote.under(line, day, number)
            course = None
            last = None
            in_note = True
            to_blank = False
            headings_read = True
            continue
        if _is_course_line(line, following[index]):
            course = line
            last = None
            continue
        if last is not None:
            last.notes[-1][2].append(line)
            continue
        unread.append(Unread(number, line))
    items = tuple(
        reading for rounds in drafts.values() for draft in rounds for reading in draft.readings()
    )
    _close(note, notes)
    return Read(items=items, unread=tuple(unread), notes=tuple(notes))


class PortalCard(NamedTuple):
    """One card of the portal's homework page as a screenshot showed it: its course and title,
    whether it is assigned or due, its date, the day heading it sits under, and the teacher's
    words under it. An assigned card's date is its own due date; a due card's is its day."""

    course: str
    title: str
    card: Card
    when: date
    day: date | None
    instruction: str | None


class CardsRead(NamedTuple):
    """What a run of portal cards reads as, and which reading each card joined."""

    readings: tuple[Reading, ...]
    joined: tuple[int, ...]
    """For each card given, in order, the place in ``readings`` of the reading it joined."""


def read_cards(cards: Sequence[PortalCard], *, now: datetime) -> CardsRead:
    """Read portal cards, in the order given, as ``read_text`` reads the same cards in a paste.

    Each card is one of today's portal claims: an assigned card's own line, a due card's day
    header. Rounds of a name are told apart as a paste's are, by the first card of each, so
    the order given decides them; a caller fixes that order by the cards' content.
    """
    drafts: dict[tuple[str, str], list[_Draft]] = {}
    joined: list[_Draft] = []
    for number, card in enumerate(cards, start=1):
        draft = _draft_for(drafts, card.course, card.title, SourceChannel.LMS, number, card.when)
        seen_in = OWN_LINE if card.card == "assigned" else DAY_HEADER
        draft.add(claim(SourceChannel.LMS, card.when, seen_in, now, PORTAL_CONFIDENCE))
        if draft.due_date is None:
            draft.due_date = card.when
            draft.field_origins["due_date"] = SourceChannel.LMS
        if card.card == "assigned" and draft.assigned_on is None and card.day is not None:
            draft.assigned_on = card.day
            draft.field_origins["assigned_on"] = SourceChannel.LMS
        # A due card is filed under its day, as a paste's is under its day line.
        day = card.when if card.card == "due" else card.day
        draft.notes.append((card.card, day, [] if card.instruction is None else [card.instruction]))
        joined.append(draft)
    ordered = [draft for rounds in drafts.values() for draft in rounds]
    place = {id(draft): index for index, draft in enumerate(ordered)}
    return CardsRead(
        readings=tuple(draft.reading() for draft in ordered),
        joined=tuple(place[id(draft)] for draft in joined),
    )


def _unbroken(line: str) -> str:
    """The line without the download's line-break mark, a lone backslash at its end."""
    stripped = line.strip()
    if stripped.endswith("\\") and not stripped.endswith("\\\\"):
        return stripped[:-1].rstrip()
    return stripped


def _next_lines(lines: list[str]) -> list[str | None]:
    """For each line, the next line that is not blank, found in one pass from the end."""
    following: list[str | None] = [None] * len(lines)
    ahead: str | None = None
    for index in range(len(lines) - 1, -1, -1):
        following[index] = ahead
        if lines[index]:
            ahead = lines[index]
    return following


def _draft_for(
    drafts: dict[tuple[str, str], list[_Draft]],
    course: str,
    title: str,
    origin: SourceChannel,
    number: int,
    when: date | None,
) -> _Draft:
    """The draft a line adds to. A card joins the round of its name whose date is within a
    week of the card's, else starts a new round. The school's Missing lines have no date of
    their own, so they share a draft of their own and never join a card's."""
    key = pair(course, title)
    rounds = drafts.setdefault(key, [])
    for draft in rounds:
        if when is None:
            if draft.due_date is None:
                return draft
        elif draft.due_date is not None and abs(draft.due_date - when) < ANOTHER_OCCURRENCE:
            return draft
    dated = any(draft.due_date is not None for draft in rounds)
    draft = _Draft(
        course=key[0],
        title=key[1],
        origin=origin,
        at_line=number,
        occurrence=None if not dated or when is None else when.isoformat(),
    )
    rounds.append(draft)
    return draft


def _is_course_line(line: str, following: str | None) -> bool:
    """A short plain line whose next line is a card's line names the card's course."""
    if len(line) > COURSE_LENGTH or line.endswith(":") or following is None:
        return False
    return bool(ASSIGNED_LINE.match(following) or DUE_LINE.match(following))


def _is_subject_heading(line: str) -> bool:
    """A short line that ends in a hyphen set off from the words before it: the summary's
    heading over what a teacher typed for a subject."""
    return len(line) <= COURSE_LENGTH and bool(SUBJECT_LINE.match(line))


def _is_a_day(line: str | None) -> bool:
    """Whether a line is written as a day, in either shape, whatever its digits."""
    return line is not None and bool(DAY_LINE.match(line) or SUMMARY_DAY_LINE.match(line))


def _names_a_day(line: str | None) -> bool:
    """Whether a line is written as a day, in either shape, and its digits name a date."""
    if line is None:
        return False
    found = DAY_LINE.match(line) or SUMMARY_DAY_LINE.match(line)
    if found is None:
        return False
    return a_date(found.group("year"), found.group("month"), found.group("day")) is not None


def _is_shaped_as_structure(line: str) -> bool:
    """Whether a line is written as a day in either shape, as a Missing line, as a card, with
    its course on its line or with none, or as a mail program's date line, whatever it reads
    as."""
    return (
        _is_a_day(line)
        or mail_date(line) is not None
        or bool(
            EMAIL_LINE.match(line)
            or CARD_LINE.match(line)
            or ASSIGNED_LINE.match(line)
            or DUE_LINE.match(line)
        )
    )


is_shaped_as_structure = _is_shaped_as_structure
"""``_is_shaped_as_structure`` by the name the screenshot path's note cards use."""


@dataclass
class _OpenNote:
    """A subject's note while ``read_text`` is inside it: its subject, the day in force
    where its heading was read, the line numbers of the heading and of each line kept after
    it, and those lines, a paragraph for each run of them between blank lines.

    The note is suspect when a line of it is shaped as a day, a Missing line, a card or a mail
    program's date line, and so was kept only because it was not read as one. Its heading
    counts when it is shaped that way too.
    """

    subject: str
    day: date | None
    numbers: list[int]
    suspect: bool
    paragraphs: list[list[str]] = field(default_factory=list)
    after_blank: bool = False

    @classmethod
    def under(cls, heading: str, day: date | None, number: int) -> "_OpenNote":
        """The note a subject heading opens: the subject is the heading without its closing
        hyphen, trimmed."""
        return cls(heading[:-1].strip(), day, [number], _is_shaped_as_structure(heading))

    def keep(self, number: int, line: str) -> None:
        """Add a line kept in the note to its paragraph, or to a new one after a blank line."""
        if self.after_blank or not self.paragraphs:
            self.paragraphs.append([])
        self.after_blank = False
        self.paragraphs[-1].append(line)
        self.numbers.append(number)
        self.suspect = self.suspect or _is_shaped_as_structure(line)


def _close(note: _OpenNote | None, notes: list[NoteRead]) -> None:
    """Hand out a subject's note where the reader ends it: its block joins ``notes`` when a
    day was in force at its heading and a line was kept after the heading."""
    if note is None or note.day is None or not note.paragraphs:
        return
    block = NoteBlock(
        subject=note.subject,
        day=note.day,
        paragraphs=tuple("\n".join(paragraph) for paragraph in note.paragraphs),
        line=note.numbers[0],
        suspect=note.suspect,
    )
    notes.append(NoteRead(block=block, lines=tuple(note.numbers)))


def _ends_a_note(line: str, day: date | None) -> bool:
    """Whether a line in a note reads as what ends it: a day whose digits name a date, a
    Missing line, or a card with its course on its line, an assigned card with a real due
    date or a due card with a title under a day already read. The first shape a line has
    decides, and the conditions are the ones ``read_text`` reads each of them by."""
    if found := DAY_LINE.match(line) or SUMMARY_DAY_LINE.match(line):
        return a_date(found.group("year"), found.group("month"), found.group("day")) is not None
    if found := EMAIL_LINE.match(line):
        return found.group("grade").strip().lower() == MISSING
    if found := CARD_LINE.match(line):
        body = found.group("card")
        if assigned := ASSIGNED_LINE.match(body):
            when = a_date(assigned.group("year"), assigned.group("month"), assigned.group("day"))
            return when is not None
        if due := DUE_LINE.match(body):
            return day is not None and bool(due.group("title").strip())
    return False


def by_hand(
    course: str,
    title: str,
    due_date: date | None,
    assigned_on: date | None,
    kind: AssignmentKind | None,
    note: str | None = None,
    *,
    now: datetime,
) -> Reading:
    """One assignment as a parent typed it: the family's own, in every field it fills. A
    kind left unchosen is suggested from the title, and a saved row keeps its own."""
    claims = (
        ()
        if due_date is None
        else (claim(SourceChannel.PARENT_ENTRY, due_date, None, now, FAMILY_CONFIDENCE),)
    )
    course, title = pair(course, title)
    return Reading(
        course=course,
        title=title,
        due_date=due_date,
        assigned_on=assigned_on,
        kind=kind or kind_of(title),
        claims=claims,
        origin=SourceChannel.PARENT_ENTRY,
        note=(note or "").strip() or None,
        kind_chosen=kind is not None,
    )


def note_reading(task: NoteTask, *, now: datetime) -> Reading:
    """One task read from a teacher's note as a reading: the school's, from the portal.

    Its class and title are the note's subject and the task's name. It is due on the
    task's deadline when the note settles one, which is then the school's claim, seen in
    the teacher's note; with no settled deadline it has no due date and makes no claim. It
    is never dated by the school day it is listed under, and it has no assigned date. The
    task's words are its instruction, read under no card, on the first school day the task
    is listed under.
    """
    course, title = pair(task.subject, task.name)
    claims = (
        ()
        if task.deadline is None
        else (claim(SourceChannel.LMS, task.deadline, TEACHER_NOTE, now, PORTAL_CONFIDENCE),)
    )
    return Reading(
        course=course,
        title=title,
        due_date=task.deadline,
        assigned_on=None,
        kind=kind_of(title),
        claims=claims,
        origin=SourceChannel.LMS,
        instructions=(InstructionSeen(task.words, SourceChannel.LMS, None, task.first_day),),
        at_line=task.line or None,
        teacher_note=task,
    )


def with_family_deadline(reading: Reading, day: date | None, *, now: datetime) -> Reading:
    """A reading made from a teacher's note, with the deadline the family gives it.

    A date other than the one the note settles is the family's entry: it becomes the due
    date, the claim seen in the teacher's note gives way to the parent's claim of that
    date, made as a typed entry makes it, and the due date's origin is the parent's entry.
    ``None`` where the note settles a date leaves no due date, no claim seen in the note,
    and no origin for a due date. The date the note itself settles, or ``None`` where it
    settles none, hands the reading back as it is, and so does a reading made from no
    note. Nothing is changed in place.
    """
    task = reading.teacher_note
    if task is None or day == task.deadline:
        return reading
    claims = tuple(said for said in reading.claims if said.seen_in != TEACHER_NOTE)
    origins = {
        name: channel for name, channel in reading.field_origins.items() if name != "due_date"
    }
    if day is not None:
        claims += (claim(SourceChannel.PARENT_ENTRY, day, None, now, FAMILY_CONFIDENCE),)
        origins["due_date"] = SourceChannel.PARENT_ENTRY
    return dataclasses.replace(reading, due_date=day, claims=claims, field_origins=origins)


def _day_answered(answer: TaskAnswer, today: date) -> tuple[bool, date | None]:
    """Whether a due-date answer names a date a paste can mean, and that date: ``None`` for
    no due date. A date chosen, or typed beside another date, reads only as year, month and
    day within a school year of ``today``."""
    if answer.when == NO_DUE_DATE:
        return True, None
    given = answer.other if answer.when == ANOTHER_DATE else answer.when
    try:
        day = date.fromisoformat(given.strip())
    except ValueError:
        return False, None
    return within_a_school_year(day, today), day


def with_answers(read: Read, answers: Sequence[TaskAnswer], *, now: datetime, today: date) -> Read:
    """A paste with the title and due-date answers sent for its readings made from teachers'
    notes applied, and kept with it as sent; an answer for any other key is ignored.

    A title typed, single-spaced, that is neither blank nor the title as read is the
    reading's title, with the kind it suggests. A due date chosen or typed, or no due date,
    goes through ``with_family_deadline``, so the date the note settles, or none where it
    settles none, leaves the reading the school's. No choice leaves the date as read. A date
    that reads as none a paste can mean is not applied, and its key is in ``dates_unread``.

    Each note joined to a card that ``read.joined`` holds adds to that card what ``_joined``
    adds: the claim of its deadline, seen in the teacher's note, and its words where they
    say more. The card's due date is its own. A joined note not selected adds nothing, so
    the page, Review changes, a refusal and the save all compare one reading. Applying this
    again adds nothing more. Nothing is changed in place."""
    items = list(read.items)
    for card, place in sorted(read.joined):
        if 0 <= card < len(items) and 0 <= place < len(items[card].joined_notes):
            items[card] = _joined(items[card], items[card].joined_notes[place], now)
    kept: list[TaskAnswer] = []
    unread: set[int] = set()
    for answer in answers:
        if not 0 <= answer.key < len(items) or items[answer.key].teacher_note is None:
            continue
        kept.append(answer)
        reading = items[answer.key]
        title = " ".join(answer.title.split())
        if title and title != reading.title:
            reading = dataclasses.replace(reading, title=title, kind=kind_of(title))
        if answer.when:
            readable, day = _day_answered(answer, today)
            if readable:
                reading = with_family_deadline(reading, day, now=now)
            else:
                unread.add(answer.key)
        items[answer.key] = reading
    if not kept:
        return read if tuple(items) == read.items else dataclasses.replace(read, items=tuple(items))
    return dataclasses.replace(
        read, items=tuple(items), answers=tuple(kept), dates_unread=frozenset(unread)
    )


STATEMENT_END: Final = " .!?,;:-"
"""What may trail a task's words once its deadline statement is taken out of them."""
IS_OR_ARE: Final = re.compile(r"(?:^|\s+)(?:is|are)$", re.IGNORECASE)
"""The "is" or "are" that links a name to its deadline statement, at the end of the words."""


def _says_more(task: NoteTask) -> bool:
    """Whether a task's words say more than its name and its deadline statement: the words
    without that statement, single-spaced, without end punctuation and a trailing "is" or
    "are", are not the name."""
    words = task.words
    if task.deadline_words:
        words = words.replace(task.deadline_words, "", 1)
    rest = " ".join(words.split()).rstrip(STATEMENT_END)
    return IS_OR_ARE.sub("", rest).rstrip(STATEMENT_END) != task.name


says_more = _says_more
"""``_says_more`` by the name the screenshot path's join uses."""


def _read_under(card: Reading, days: Collection[date]) -> bool:
    """Whether a card's reading was read under one of the school days: assigned on it, or
    due on it by the day's header."""
    written = {day.isoformat() for day in days}
    return card.assigned_on in days or any(
        said.channel is SourceChannel.LMS
        and said.seen_in == DAY_HEADER
        and said.asserted_value in written
        for said in card.claims
    )


def _joined(card: Reading, task: NoteTask, now: datetime) -> Reading:
    """A card's reading with what a note about the same assignment adds once the family
    selects it: the claim of a deadline the note settles, seen in the teacher's note, and the
    task's words as one more instruction when they say more than the name and the deadline
    statement. A claim or an instruction the card has is not added twice, so applying it
    again changes nothing. The card's due date and joined notes are as they were."""
    claims, instructions = card.claims, card.instructions
    if task.deadline is not None:
        noted = claim(SourceChannel.LMS, task.deadline, TEACHER_NOTE, now, PORTAL_CONFIDENCE)
        if noted not in claims:
            claims += (noted,)
    if _says_more(task) and all(seen.text != task.words for seen in instructions):
        instructions += (InstructionSeen(task.words, SourceChannel.LMS, None, task.first_day),)
    return dataclasses.replace(card, claims=claims, instructions=instructions)


def saved_joined_notes(
    items: Sequence[Reading], changes: Sequence["Change"], store: ProjectStateStore
) -> frozenset[tuple[int, int]]:
    """Each note joined to a card, by the card's key and the note's place among its
    ``joined_notes``, that the record shows saved where the card lands, so selecting it adds
    nothing: the card lands on one row, and that row keeps the note's words, single-spaced,
    as a school instruction under the note's first school day and, where the note settles a
    date, a claim of that date seen in a teacher's note. Changed words, another school day's
    occurrence, and a note whose words were never kept as an instruction, such as one that
    added only a date claim, which carries no school day, are not shown saved."""
    landed = {
        change.key: change.on_record
        for change in changes
        if change.on_record is not None
        and not change.identity_asked
        and change.folded_into is None
        and 0 <= change.key < len(items)
        and items[change.key].joined_notes
    }
    if not landed:
        return frozenset()
    saved = _OnRecord(store, list(landed.values()), {})
    shown: set[tuple[int, int]] = set()
    for key, row in landed.items():
        name = row.assignment_id
        claimed = {(said.channel, said.asserted_value, said.seen_in) for said in saved.claims(name)}
        for place, task in enumerate(items[key].joined_notes):
            if (" ".join(task.words.split()), task.first_day) not in saved.noted(name):
                continue
            if task.deadline is not None and (
                (SourceChannel.LMS, task.deadline.isoformat(), TEACHER_NOTE) not in claimed
            ):
                continue
            shown.add((key, place))
    return frozenset(shown)


def joins(card: Reading, task: NoteTask, read_under: Callable[[Collection[date]], bool]) -> bool:
    """Whether a note's task may join a card: the same class and title, the card read under
    one of the school days the task is listed under, and the task undated, due on the card's
    due date, or listed first on a day the card was read under. ``read_under`` says whether
    the card was read under one of the days it is given."""
    return (
        card.pair == pair(task.subject, task.name)
        and read_under(task.seen_on)
        and (
            task.deadline is None or task.deadline == card.due_date or read_under((task.first_day,))
        )
    )


def the_one(places: Sequence[int]) -> int | None:
    """The one place given, or ``None`` for none or several: a task two cards could take
    joins neither."""
    return places[0] if len(places) == 1 else None


def with_note_readings(read: Read, *, now: datetime) -> tuple[Reading, ...]:
    """The readings of a paste with its teachers' notes read as tasks: the cards first, in
    their places, then one reading for each task that is no card's.

    A task that ``joins`` exactly one card is that card's assignment: the card's reading
    stays where it is, as read, with the task among its ``joined_notes``; what the note adds
    is applied only for a joined note the family selects (``with_answers``). A task two
    cards could take joins neither. Every other task is a reading of its own, after the
    cards, in the order the notes give the tasks. One whose class and title a reading before
    it has carries its first school day and a mark of its words as its occurrence, so its
    id is its own. A suspect note is read as no task: its lines are text that needs review.
    A deadline outside a school year of the task's first school day is named, not kept
    (``deadline_in_reach``).
    """
    items = list(read.items)
    made: list[Reading] = []
    blocks = [note.block for note in read.notes if not note.block.suspect]
    for task in map(deadline_in_reach, read_notes(blocks)):
        place = the_one(
            [
                index
                for index, card in enumerate(read.items)
                if joins(card, task, functools.partial(_read_under, card))
            ]
        )
        if place is not None:
            items[place] = dataclasses.replace(
                items[place], joined_notes=(*items[place].joined_notes, task)
            )
            continue
        reading = note_reading(task, now=now)
        if any(other.pair == reading.pair for other in (*items, *made)):
            reading = dataclasses.replace(reading, occurrence=_note_occurrence(task))
        made.append(reading)
    return (*items, *made)


def deadline_in_reach(task: NoteTask) -> NoteTask:
    """A task with a deadline a due date can have: one within a school year of the task's
    first school day, as ``within_a_school_year`` takes a due date chosen or typed on the
    review. A deadline outside it, read with the year written with it, is no deadline: the
    task keeps no deadline and that date, as written, is named for the family to choose. No
    other year is put in its place. A pasted note and a note on a screenshot are read through
    it alike."""
    if task.deadline is None or within_a_school_year(task.deadline, task.first_day):
        return task
    named = task.named if task.deadline in task.named else (*task.named, task.deadline)
    return dataclasses.replace(
        task, deadline=None, deadline_words=None, choices=(), named=named, listed_after=()
    )


def with_notes(read: Read, *, now: datetime) -> Read:
    """A paste with its teachers' notes read as tasks, for the review page.

    A paste with no note is handed back as it is. Otherwise the readings are those of
    ``with_note_readings``; the lines that need review are the paste's own without the
    lines of each note that became tasks, which the readings carry as their words; and the
    readings left out unless someone selects them are those ``left_out_by_default`` names.
    A suspect note becomes no task, so its lines stay for review.
    """
    if not read.notes:
        return read
    items = with_note_readings(read, now=now)
    taken = {
        line
        for note in read.notes
        if not note.block.suspect and any(words.strip() for words in note.block.paragraphs)
        for line in note.lines
    }
    return dataclasses.replace(
        read,
        items=items,
        unread=tuple(line for line in read.unread if line.line not in taken),
        left_out=left_out_by_default(items),
    )


def _note_occurrence(task: NoteTask) -> str:
    """What sets one task of the notes apart from every other of its class and title: its
    first school day and a mark of its words, single-spaced. ``with_note_readings`` gives
    it to a task whose name a reading before it has."""
    words = " ".join(task.words.split()).encode("utf-8")
    return f"{task.first_day.isoformat()}/{hashlib.sha256(words).hexdigest()[:8]}"


def left_out_by_default(items: Sequence[Reading]) -> frozenset[int]:
    """The keys of the readings left out of a save unless someone selects them: every one
    made from a teacher's note, whatever its standing. A card starts selected."""
    return frozenset(key for key, item in enumerate(items) if item.teacher_note is not None)


def a_year_from(today: date, years: int) -> date:
    """The same day of the month ``years`` away; a leap day lands on the last of February."""
    try:
        return today.replace(year=today.year + years)
    except ValueError:
        return today.replace(year=today.year + years, day=28)


def within_a_school_year(when: date, today: date) -> bool:
    """A date a paste can mean: from a year ago today to a year from today, by the calendar."""
    return a_year_from(today, -1) <= when <= a_year_from(today, 1)


NEW: Final = "new"
KNOWN: Final = "known"
CLAIMED: Final = "claimed"
REVIEW: Final = "review"
FOLDED: Final = "folded"
LEFT_OUT: Final = "left_out"


@dataclass(frozen=True)
class Change:
    """What keeping one reading would do to the record, worked out before anything is written."""

    key: int
    """The reading's place in the paste, which the review page's choices refer to."""
    reading: Reading
    on_record: Assignment | None
    new_claims: tuple[SourceRecord, ...]
    new_reports: tuple[StatusReport, ...]
    fills_due_date: bool
    """Whether the record has no due date and the reading gives one, which becomes it."""
    moves_due_date: bool
    """Whether the parent said the saved assignment moved, so the pasted date becomes its
    due date."""
    fills_assigned_on: bool
    note_change: str | None
    """``fill`` when the record has no note and a parent typed one; ``update`` when a
    parent's typed note replaces the saved one; ``kept`` when her own note stays instead;
    ``None`` when there is nothing to say. The school's words are never a note: they are
    its instructions, decided apart."""
    kind: AssignmentKind
    """The kind the row will have: the reader's suggestion, or the parent's choice."""
    kind_by_parent: bool
    ambiguous: bool
    """Whether the pasted work may be another round under a name, unanswered."""
    occurrence: str | None
    """The parent's answer, ``update``, ``keep`` or ``new``, when one was given."""
    beside: date | None = None
    """The other date the question is about: the saved row's, or another card's in the
    same text."""
    base: Assignment | None = None
    """The saved row as the cards before this one in the same text leave it, when they
    change it; what this card fills or corrects is measured against that, so several
    cards about one assignment compose rather than each starting from the saved row."""
    choices_conflict: bool = False
    """Whether two cards folded into this one chose different types, which no saving
    settles on its own."""
    suggested_kind: AssignmentKind | None = None
    """What the review page shows for the card before any choice: the saved row's kind, or
    the reader's suggestion. Kept apart from ``kind`` so a page returned with a choice
    still pending shows the choice and still knows it was one."""
    folded_into: int | None = None
    """The key of the card this one was folded into: as the parent said, whose answers
    travel with the page, or a report shown on the card for the same homework. Such a card
    is not shown and saves nothing of its own."""
    instructions: InstructionsOutcome | None = None
    """What keeping the school's instructions would do to the assignment this card lands
    on, decided once for every card of the text that lands there, and carried by the
    first of them; ``None`` on every other card, and on a card with none."""
    instructions_seen: tuple[InstructionSeen, ...] = ()
    """Every instruction the text gives the assignment, from every card that lands there."""
    instructions_kept: tuple[SchoolInstruction, ...] = ()
    instructions_revision: int = 0
    instructions_answer: InstructionChoice | None = None
    """The parent's choice of which instructions apply, when one was given."""
    instructions_submitted: SubmittedChoice | None = None
    """The one answer given for the assignment, whatever it says, contradictions included,
    with the revision and the instructions it was made against, when it was made against
    what stands; a page returned shows it as made."""
    instructions_unsaved: tuple[SubmittedChoice, ...] = ()
    """The answers given for the assignment that are said back as not saved and never shown
    as made: those made against instructions that have changed since, or answers on its
    cards that ask for different things, one for each thing asked for, in card order; or,
    on a card still waiting on which homework it is, the answer given on it."""
    instructions_with: int | None = None
    """The key of the card that carries this card's assignment's instructions, when that is
    another card of the same text."""
    candidates: tuple[Assignment, ...] = ()
    """The homework on record the card asks about, in the order shown."""
    identity_asked: bool = False
    """Whether the card asks which homework it is about before anything of it is saved."""
    identity_question: str | None = None
    """Why it asks: ``hers`` for homework made from her note, ``which`` for several under
    one name, ``report`` for an undated report that more than one could mean."""
    identity_shown: tuple[str, ...] = ()
    """The ids of the homework the question lists, in order."""
    identity_hers: frozenset[str] = frozenset()
    """Which of those are homework made from her note."""
    identity_basis: str = ""
    """The fingerprint of what the question shows, which an answer is checked against."""
    identity_stale: bool = False
    """Whether an answer given on the card was made against facts that changed since."""
    identity_decision: DecisionToKeep | None = None
    """The answer the save keeps with the card."""
    made_as: str | None = None
    """The id of the school's own assignment made by the answer that it is different
    homework: the creation token the review page made, never the course, title, or date."""
    waiting: tuple[Capture, ...] = ()
    """Her notes that wait with this card's class and title, offered to link to the
    homework it lands on."""
    offers: str | None = None
    """How the card stood when it offered to find homework already here under another
    name: ``new`` where it would save a new assignment, or the fingerprint of the question
    it asks; ``None`` where it doesn't offer it."""
    picked: bool = False
    """Whether the card lands on homework the parent found and chose on it."""
    pick_refused: str | None = None
    """The id of homework chosen on the card that the save can't take: the card, or that
    homework, changed since it was chosen, or the card doesn't offer the choice now."""
    kept_separate: bool = False
    """Whether a recorded answer kept apart two of the homework the question lists."""
    left_out: bool = False
    """Whether the reading is left out of the save: nothing of it is compared with the
    record or written, and no other card of the text is measured against it."""

    @property
    def saved_as(self) -> Assignment | None:
        """The homework the card lands on when it's saved here under another class or title
        than the one the card reads."""
        row = self.on_record
        if row is None or self.occurrence == NEW_WORK or self.made_as is not None:
            return None
        return row if pair(row.course, row.title) != self.reading.pair else None

    @property
    def lands_nowhere(self) -> bool:
        """Whether the card waits on the parent's word about which homework it is, and so
        lands on no assignment yet."""
        return self.ambiguous or self.identity_asked

    @property
    def instructions_asked(self) -> bool:
        """Whether the school's instructions need the parent's choice before anything is
        saved."""
        return isinstance(self.instructions, InstructionsNeedAChoice)

    @property
    def instructions_question(self) -> bool:
        """Whether the card puts the question of which instructions apply: asked and not yet
        answered, or answered, so a page returned for another question keeps the answer, or
        answered against instructions that have changed since, so the parent chooses again
        where the answer not saved is said."""
        return (
            self.instructions_asked
            or self.instructions_answer is not None
            or self.instructions_stale
        )

    @property
    def instructions_shown(self) -> tuple["ShownInstruction", ...]:
        """The instructions the question lists: those kept, in the order kept, then those new
        in this text, in the order read. The page's boxes follow this order."""
        new = new_texts(self.instructions_kept, self.instructions_seen)
        return (
            *(
                ShownInstruction(item.text, item.state, item.card, item.card_day, item.sequence)
                for item in self.instructions_kept
            ),
            *(ShownInstruction(item.text, "new", item.card, item.card_day) for item in new),
        )

    @property
    def instructions_read(self) -> tuple["ShownInstruction", ...]:
        """What the text says of the school's instructions where no question is put: each
        instruction it gives the assignment, once, under the card this text shows it on,
        with how it stands, or ``new`` when this text brings it."""
        standing = {item.text: item.state for item in self.instructions_kept}
        read: dict[str, ShownInstruction] = {}
        for item in self.instructions_seen:
            read.setdefault(
                item.text,
                ShownInstruction(
                    item.text, standing.get(item.text, "new"), item.card, item.card_day
                ),
            )
        return tuple(read.values())

    @property
    def instructions_stale(self) -> bool:
        """Whether an answer given for the assignment, on any of its cards, was made against
        instructions that have changed since; none of them is put right onto the facts as
        they stand now."""
        return isinstance(self.instructions, InstructionChoiceStale)

    @property
    def instructions_ticked(self) -> frozenset[str]:
        """The instructions a page returned shows ticked: the parent's answer, by its words,
        never by its place, and nothing when the answer was made against instructions that
        have changed since, which are asked afresh."""
        if self.instructions_submitted is None or self.instructions_stale:
            return frozenset()
        return self.instructions_submitted.applies

    @property
    def instructions_none_ticked(self) -> bool:
        """Whether a page returned shows none applying ticked, by the same rule."""
        answer = self.instructions_submitted
        return answer is not None and not self.instructions_stale and answer.none_applies

    @property
    def suggested(self) -> AssignmentKind:
        """The kind the page shows for the card before any choice."""
        return self.kind if self.suggested_kind is None else self.suggested_kind

    @property
    def standing(self) -> Assignment | None:
        """The row this card's changes are measured against: after the cards before it."""
        return self.base if self.base is not None else self.on_record

    @property
    def note_shown(self) -> str | None:
        """What the review page says the typed note does, as the page signs it: ``fill``,
        or, where a note is saved already, ``update``, ``kept``, or ``same`` for a typed
        note that matches it, with a fingerprint of that note, so a save that finds
        another note there asks again."""
        outcome = self.note_change
        if outcome is None and self.reading.note and self.standing is not None:
            outcome = "same"
        if outcome in (None, "fill") or self.standing is None:
            return outcome
        saved = (self.standing.note or "").encode("utf-8")
        return f"{outcome}:{hashlib.sha256(saved).hexdigest()[:16]}"

    @property
    def assignment_id(self) -> str:
        """The row the reading lands in: the record's own when it has one, else a new one.
        New work has an id that carries its due date; new work read from a teacher's note,
        one that carries the note's first school day and a mark of its words."""
        if self.made_as is not None:
            return self.made_as
        if self.on_record is not None and self.occurrence != NEW_WORK:
            return self.on_record.assignment_id
        note = self.reading.teacher_note
        if self.occurrence == NEW_WORK and note is not None:
            # New work read from a teacher's note is a row of that note: two notes of one
            # name that settle one date are two rows.
            return identity(self.reading.course, self.reading.title, _note_occurrence(note))
        if self.occurrence == NEW_WORK and self.reading.due_date is not None:
            return identity(
                self.reading.course, self.reading.title, self.reading.due_date.isoformat()
            )
        return self.reading.assignment_id

    @property
    def new_kind(self) -> AssignmentKind | None:
        """The kind a row on record would change to, when the parent chose another."""
        standing = self.standing
        if standing is None or not self.kind_by_parent or standing.kind == self.kind:
            return None
        return self.kind

    @property
    def additions(self) -> tuple[str, ...]:
        """What a reading adds to a row already on record, in the review page's words."""
        parts = []
        if self.fills_due_date or self.moves_due_date:
            parts.append("the due date")
        elif self.new_claims:
            parts.append("a date to review")
        if self.new_reports:
            parts.append("what the school reports")
        if self.fills_assigned_on:
            parts.append("the assigned date")
        if self.note_change in ("fill", "update"):
            parts.append("the note")
        if self.new_kind is not None:
            parts.append("the type")
        if isinstance(self.instructions, InstructionsSettled):
            parts.append("the school's instructions")
        return tuple(parts)

    @property
    def state(self) -> str:
        """``left_out`` for a reading left out of the save, ``review`` for a question the
        parent must answer, ``folded`` for a card folded into another, else ``new``,
        ``claimed``, or ``known``."""
        if self.left_out:
            return LEFT_OUT
        if self.folded_into is not None:
            return FOLDED
        if self.lands_nowhere or self.instructions_asked or self.instructions_stale:
            return REVIEW
        if self.on_record is None or self.occurrence == NEW_WORK:
            return NEW
        if self.additions:
            return CLAIMED
        return KNOWN

    @property
    def label(self) -> str:
        """The state as the review page says it."""
        if self.state == LEFT_OUT:
            return "Not selected"
        if self.state == REVIEW:
            return "Needs your answer"
        if self.state == FOLDED:
            return "Folded into the card for the same assignment"
        if self.state == NEW:
            return "New"
        if self.state == KNOWN:
            return "Already saved"
        parts = self.additions
        joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        return f"Saved; adds {joined}"

    @property
    def saved_due(self) -> date | None:
        """The due date on record, when the reading matches a row."""
        return None if self.on_record is None else self.on_record.due_date

    @property
    def dates_differ(self) -> bool:
        """Whether the pasted due date and the saved one are both known and different."""
        return (
            self.on_record is not None
            and self.reading.due_date is not None
            and self.saved_due is not None
            and self.reading.due_date != self.saved_due
        )

    @property
    def effect(self) -> str:
        """Exactly what saving does for this reading, in one or two sentences."""
        return " ".join(part for part in (self.effect_base, self.type_effect) if part)

    @property
    def type_effect(self) -> str:
        """What saving does with the type, when a parent chose one; empty otherwise."""
        if self.state == NEW and self.kind_by_parent:
            return f"The type becomes {self.kind.value.lower()}, as chosen."
        if self.state == CLAIMED and self.new_kind is not None:
            return f"The type becomes {self.new_kind.value.lower()}, as chosen."
        return ""

    @property
    def given(self) -> str:
        """How the reading's date reached the page: pasted from the school's text, or entered
        by a parent."""
        return "entered" if self.reading.origin == SourceChannel.PARENT_ENTRY else "pasted"

    @property
    def effect_base(self) -> str:
        """What saving does apart from the type, which the page rewrites as the select changes."""
        if self.state == LEFT_OUT:
            return "Not selected. Nothing from this note is saved."
        if self.identity_asked:
            if self.identity_question == "report":
                return (
                    "More than one assignment on record has this class and title. Say which "
                    "one this report is about; the answer places this report only."
                )
            return "Say which homework this is before anything of it is saved."
        if self.state == REVIEW and not self.ambiguous:
            if self.instructions_stale:
                return (
                    "The school's instructions saved for this assignment changed after the "
                    "review was shown, so the choice made here was not saved. Choose again from "
                    "what is saved now."
                )
            what = (
                "The school's instructions here differ from what is saved."
                if self.instructions_kept
                else "This text gives more than one instruction from the school."
            )
            return (
                f"{what} Choose which apply now; the ones not chosen are kept as history, and "
                "nothing the school said is lost."
            )
        if self.state == REVIEW and self.reading.teacher_note is not None and self.beside is None:
            return (
                "This text names it again in another note. Say whether it is the same "
                "assignment or new work under the same name."
            )
        if self.state == REVIEW:
            where = (
                "The saved assignment is due"
                if self.on_record is not None
                else "This text also has it due"
            )
            when = "" if self.beside is None else f" {spoken_day(self.beside)}"
            if self.reading.teacher_note is not None:
                # The choices under a note's question say what each does to the due date.
                return (
                    f"{where}{when}. Say whether this is the same assignment or new work "
                    "under the same name."
                )
            return (
                f"{where}{when}. Say whether this is the same assignment with its due date "
                "changed, or new work under the same name."
            )
        if self.state == FOLDED:
            return "Folded into the card for the same assignment, as you said."
        if self.state == NEW:
            return " ".join(
                part for part in ("Saved as a new assignment.", self.instructions_effect) if part
            )
        if self.state == KNOWN:
            return KEPT_NOTE if self.note_change == "kept" else "Nothing changes."
        parts = []
        if self.fills_due_date:
            parts.append(
                f"The record has no due date of its own, so the {self.given} date becomes it."
            )
        elif self.moves_due_date and self.reading.due_date is not None:
            parts.append(
                f"The due date becomes {spoken_day(self.reading.due_date)}, as you said; the "
                "saved date stays in the history as a claim."
            )
        elif self.new_claims and self.dates_differ:
            parts.append(
                f"The {self.given} date is added as evidence beside the saved date, which "
                "stays; her page will say the sources disagree."
            )
        elif self.new_claims:
            parts.append(f"The {self.given} date is added as evidence for the saved date.")
        if self.new_reports:
            parts.append("What the school reports is saved with the day.")
        if self.fills_assigned_on:
            parts.append("The assigned date is filled in.")
        if self.note_change == "fill":
            parts.append("The note is saved.")
        elif self.note_change == "update":
            parts.append("Your note replaces the saved note.")
        elif self.note_change == "kept":
            parts.append(KEPT_NOTE)
        if self.instructions_effect:
            parts.append(self.instructions_effect)
        return " ".join(parts)

    @property
    def instructions_effect(self) -> str:
        """What saving does with the school's instructions, in one sentence; empty when it
        does nothing with them."""
        if not isinstance(self.instructions, InstructionsSettled):
            return ""
        if self.instructions_answer is None:
            return "The school's instruction is saved, apart from anyone's own note."
        return "The school's instructions are saved as chosen, apart from anyone's own note."


@dataclass(frozen=True)
class ShownInstruction:
    """One instruction as the question lists it: its words, whether it is kept and how it
    stands or is new in this text, the card it was first read under, and its row when it is
    kept. The card's day is where it was read, never when the teacher wrote it."""

    text: str
    standing: str
    card: str | None
    card_day: date | None
    sequence: int | None = None


@dataclass(frozen=True)
class IdentityAnswer:
    """A parent's answer to which homework a card is about, with what the card showed: an id
    it listed, or ``None`` for different homework; the ids it listed in order; the
    fingerprint of all it showed; and, for different homework, the creation token the
    review page made for the card."""

    choice: str | None
    shown: tuple[str, ...]
    basis: str
    creation: str | None = None


@dataclass(frozen=True)
class Pick:
    """Homework a parent found on a card and chose as the one the card is about, though it's
    saved under another name: its id, its fingerprint as the page showed it, and how the
    card stood when it offered the search (``WOULD_BE_NEW``, or its question's
    fingerprint)."""

    target: str
    basis: str
    situation: str


@dataclass(frozen=True)
class NotOffered:
    """Nothing was saved: a choice of homework under another name came for cards that never
    offered it, or beside an answer to the card's own question. ``cards`` are those cards."""

    cards: frozenset[int]


@dataclass(frozen=True)
class _Where:
    """Where the record puts a reading before any answer: on a row, by an answer kept or by
    its date; nowhere yet, with a question; or by today's rules for one row or none."""

    lands: Assignment | None = None
    asks: str | None = None
    candidates: tuple[Assignment, ...] = ()
    today: bool = False


def _where(
    reading: Reading,
    candidates: list[Assignment],
    mine: set[str],
    decided: tuple[IntakeDecision, ...],
    holds: Callable[[Assignment, Reading], bool],
    apart: Mapping[str, tuple[str, ...]],
    unchanged: Callable[[Assignment, Reading], bool],
    renamed: Sequence[Assignment] = (),
) -> _Where:
    """Which homework a reading is about, by the rules the review page states. Text saved
    before is never asked about again: a reading whose every claim and report one candidate
    already ``holds`` lands there, as the same text saved twice moves nothing. Then, for a
    dated reading: the latest answer kept for the same name and due date, while it showed
    every candidate now due that day; her homework asks until a parent has said, here or
    when her note was kept apart from one of them; one candidate due that day; several ask;
    one candidate due another day or none follow today's rules. Undated text lands on the
    only candidate once any of hers is settled. When several could be meant, it lands where
    the latest answer to its own question put it, while that answer showed every candidate
    and the text would change nothing there (``unchanged``); otherwise each report asks
    where it goes and anything else asks which homework it is. A placement of one report
    is never an answer about another.

    Homework a parent chose for this class and title though it's saved under another
    (``renamed``) is where the reading lands while nothing on record has the name, by
    today's date rules. Once something does, the reading asks which, listing both, unless
    it's text saved before or an answer to its own question showed every one.

    A reading made from a teacher's note that no one row holds from a save before follows
    ``_where_noted`` instead, where its class and title alone put it on no row."""
    named = {item.assignment_id for item in candidates}
    elsewhere = [item for item in renamed if item.assignment_id not in named]
    pool = [*candidates, *elsewhere]
    by_id = {item.assignment_id: item for item in pool}
    saved_before = [item for item in pool if holds(item, reading)]
    settled = {item.lands_on for item in decided if item.kind in ("same", "which", "renamed")} | {
        name for item in decided if item.kind == "different" for name in item.shown
    }
    # Homework whose note was kept apart from one of these when it was added is settled too.
    settled |= {name for name, others in apart.items() if by_id.keys() & set(others)}
    open_hers = [
        item
        for item in candidates
        if item.assignment_id in mine and item.assignment_id not in settled
    ]
    if len(saved_before) == 1:
        return _Where(lands=saved_before[0])
    if reading.teacher_note is not None:
        return _where_noted(reading, candidates, pool, decided, bool(open_hers))
    if elsewhere:
        if not candidates and len(elsewhere) == 1:
            return _Where(lands=elsewhere[0], today=True)
        latest = next(
            (
                item
                for item in reversed(decided)
                if item.kind != "renamed" and _answers(item, reading)
            ),
            None,
        )
        if (
            latest is not None
            and latest.lands_on in by_id
            and by_id.keys() <= {*latest.shown, latest.lands_on}
            and (reading.due_date is not None or unchanged(by_id[latest.lands_on], reading))
        ):
            return _Where(lands=by_id[latest.lands_on])
        asks = (
            "hers"
            if open_hers
            else "report"
            if reading.due_date is None and reading.reports
            else "which"
        )
        return _Where(asks=asks, candidates=tuple(pool))
    if reading.due_date is not None:
        due_that_day = {
            item.assignment_id for item in candidates if item.due_date == reading.due_date
        }
        latest = next(
            (
                item
                for item in reversed(decided)
                if item.kind != "report_placed" and item.due_date == reading.due_date
            ),
            None,
        )
        if (
            latest is not None
            and latest.lands_on in by_id
            and due_that_day <= {*latest.shown, latest.lands_on}
        ):
            return _Where(lands=by_id[latest.lands_on])
        if open_hers:
            return _Where(asks="hers", candidates=tuple(candidates))
        if len(due_that_day) == 1:
            return _Where(lands=by_id[next(iter(due_that_day))])
        if len(candidates) >= 2:
            return _Where(asks="which", candidates=tuple(candidates))
        return _Where(lands=candidates[0] if candidates else None, today=True)
    if not candidates:
        return _Where(today=True)
    if len(candidates) == 1:
        if open_hers:
            return _Where(asks="hers", candidates=tuple(candidates))
        return _Where(lands=candidates[0])
    latest = next((item for item in reversed(decided) if _answers(item, reading)), None)
    if (
        latest is not None
        and latest.lands_on in by_id
        and by_id.keys() <= {*latest.shown, latest.lands_on}
        and unchanged(by_id[latest.lands_on], reading)
    ):
        return _Where(lands=by_id[latest.lands_on])
    return _Where(asks="report" if reading.reports else "which", candidates=tuple(candidates))


def _where_noted(
    reading: Reading,
    candidates: list[Assignment],
    pool: list[Assignment],
    decided: tuple[IntakeDecision, ...],
    hers_open: bool,
) -> _Where:
    """Which homework a reading made from a teacher's note is about, when no row holds the
    note from a save before. Its class and title alone never put it on a row. ``pool`` is
    every row it could be: those of its name (``candidates``) and any chosen for the name.

    With a date: where the latest answer kept for the name and that date put it, while
    that answer showed every row of the name due that day; her homework asks; the one row
    of the name due that day; a row of the name with a due date a week or more away, when
    it is the only row, by today's rules, which ask whether it is the same assignment; any
    other row or rows ask which homework it is. With no date: her homework asks; any other
    row or rows ask which. An answer kept for the name places no undated note: the note
    the answer was given for is held by its row under its words and first school day and
    never comes here, and the same words first listed on another school day are another
    occurrence, which is asked about. With no row at all it follows today's rules for a
    reading that lands nowhere."""
    if not pool:
        return _Where(today=True)
    by_id = {item.assignment_id: item for item in pool}
    if reading.due_date is None:
        return _Where(asks="hers" if hers_open else "which", candidates=tuple(pool))
    due_that_day = {item.assignment_id for item in candidates if item.due_date == reading.due_date}
    latest = next(
        (
            item
            for item in reversed(decided)
            if item.kind != "report_placed" and item.due_date == reading.due_date
        ),
        None,
    )
    if (
        latest is not None
        and latest.lands_on in by_id
        and due_that_day <= {*latest.shown, latest.lands_on}
    ):
        return _Where(lands=by_id[latest.lands_on])
    if hers_open:
        return _Where(asks="hers", candidates=tuple(pool))
    if len(due_that_day) == 1:
        return _Where(lands=by_id[next(iter(due_that_day))])
    if len(pool) == 1 and len(candidates) == 1:
        saved_due = candidates[0].due_date
        if saved_due is not None and abs(reading.due_date - saved_due) >= ANOTHER_OCCURRENCE:
            return _Where(lands=candidates[0], today=True)
    return _Where(asks="which", candidates=tuple(pool))


def _report_of(reading: Reading) -> dict[str, str | None] | None:
    """The report an undated reading carries, as a placement keeps it: evidence of which
    report was placed, never a key for another."""
    return _placement(reading.reports[-1]) if reading.reports else None


def _placement(report: StatusReport) -> dict[str, str | None]:
    """One report as a placement keeps it: its channel, status, day, and source date line."""
    return {
        "channel": report.channel.value,
        "status": report.status,
        "day": report.reported_on.isoformat(),
        "source_date_text": report.source_date_text,
    }


def _stands(
    reply: IdentityAnswer, reading: Reading, decided: tuple[IntakeDecision, ...]
) -> IntakeDecision | None:
    """The answer kept already that says what ``reply`` asks for: a retry finds it, so an
    answer sent twice writes nothing more. Different homework is found by its creation
    token; the same or which, by the name, the due date when the text has one, and where
    it landed; a report's placement, only for that same report."""
    for item in decided:
        if reply.choice is None:
            if reply.creation is not None and item.creation == reply.creation:
                return item
            continue
        if item.lands_on != reply.choice:
            continue
        if reading.due_date is not None:
            if item.kind in ("same", "which") and item.due_date == reading.due_date:
                return item
        elif item.kind in ("same", "which") or (
            item.kind == "report_placed" and item.report == _report_of(reading)
        ):
            return item
    return None


def _answers(item: IntakeDecision, reading: Reading) -> bool:
    """Whether a kept answer is one to the question a reading asks: for an undated report,
    that report's placement; for any other text, an answer about the same due date or the
    same lack of one."""
    if reading.due_date is None and reading.reports:
        return item.kind == "report_placed" and item.report == _report_of(reading)
    return item.kind != "report_placed" and item.due_date == reading.due_date


def identity_basis(
    candidates: Sequence[Assignment],
    decided: tuple[IntakeDecision, ...],
    mine: Collection[str],
) -> str:
    """The fingerprint of an identity question: each candidate as the question shows it, by
    its id, title, class, due date, where it came from, and her note where it's quoted, and
    the answers kept to that same question (``decided``). A choice sent back is checked
    against it in the save's transaction. The question is asked again when homework has
    since arrived, left, changed in what the question shows, or been answered elsewhere, and
    not for a change it doesn't show or an answer to another question of the name."""
    shown = sorted(
        [
            item.assignment_id,
            item.title,
            item.course,
            None if item.due_date is None else item.due_date.isoformat(),
            "hers" if item.assignment_id in mine else item.origins.get("record"),
            item.note if item.assignment_id in mine and item.note_by == "student" else None,
        ]
        for item in candidates
    )
    answered = [item.sequence for item in decided]
    serialized = json.dumps([shown, answered], separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _asked(
    key: int,
    reading: Reading,
    where: _Where,
    shown: tuple[str, ...],
    basis: str,
    kinds: Mapping[int, AssignmentKind],
    mine: Collection[str],
    *,
    stale: bool = False,
) -> Change:
    """A card that asks which homework it is about, and lands nowhere until answered."""
    kind, by_parent, suggested = _kind_for(key, reading, None, kinds)
    hers = frozenset(item.assignment_id for item in where.candidates if item.assignment_id in mine)
    return Change(
        key,
        reading,
        None,
        (),
        (),
        False,
        False,
        False,
        None,
        kind,
        by_parent,
        False,
        None,
        suggested_kind=suggested,
        candidates=where.candidates,
        identity_asked=True,
        identity_question=where.asks,
        identity_shown=shown,
        identity_hers=hers,
        identity_basis=basis,
        identity_stale=stale,
    )


def _decision(
    kind: DecisionKind,
    reading: Reading,
    lands_on: str,
    shown: tuple[str, ...],
    basis: str,
    creation: str | None = None,
) -> DecisionToKeep:
    """The answer the save keeps, with the row's name and due date."""
    return DecisionToKeep(
        kind=kind,
        course=reading.pair[0],
        title=reading.pair[1],
        due_date=reading.due_date,
        lands_on=lands_on,
        shown=shown,
        basis=basis,
        creation=creation,
        report=_report_of(reading) if kind == "report_placed" else None,
    )


@dataclass(frozen=True)
class ChangedSinceShown:
    """Nothing was saved: a choice on the page was made against instructions that changed
    since the page was made. The changes as they stand now come back, to look at again.
    ``moved`` holds the readings made from teachers' notes that would do something other
    than the page said for them."""

    changes: list["Change"]
    moved: frozenset[int] = frozenset()


@dataclass(frozen=True)
class NotAsked:
    """Nothing was saved: an answer about the school's instructions came on a card where the
    signed page asked no such question, checked against the record as the save found it.
    ``cards`` are those cards."""

    cards: frozenset[int]


class _Seen:
    """What is already accounted for while one text is compared: the claims and reports on
    record for each row, from the comparison's one reading of them, and those earlier
    readings of the same text add to it."""

    def __init__(self, saved: "_OnRecord") -> None:
        self._saved = saved
        self._claims: dict[str, list[SourceRecord]] = {}
        self._reports: dict[str, list[StatusReport]] = {}

    def novel_claims(
        self, assignment_id: str, saved: bool, claims: tuple[SourceRecord, ...]
    ) -> tuple[SourceRecord, ...]:
        """The claims not yet accounted for under a row, now accounted for."""
        if assignment_id not in self._claims:
            self._claims[assignment_id] = list(self._saved.claims(assignment_id)) if saved else []
        kept = self._claims[assignment_id]
        fresh = []
        for record in claims:
            if all(_same_claim(record, other) is False for other in kept):
                kept.append(record)
                fresh.append(record)
        return tuple(fresh)

    def novel_reports(
        self, assignment_id: str, saved: bool, reports: tuple[StatusReport, ...]
    ) -> tuple[StatusReport, ...]:
        """The reports not yet accounted for under a row, now accounted for, each as the
        school wrote it: a second Missing line of the same day is a report of its own."""
        if assignment_id not in self._reports:
            self._reports[assignment_id] = list(self._saved.reports(assignment_id)) if saved else []
        kept = self._reports[assignment_id]
        fresh = []
        for report in reports:
            if all(_same_report_as_written(report, other) is False for other in kept):
                kept.append(report)
                fresh.append(report)
        return tuple(fresh)


class _OnRecord:
    """What the rows a text could land on keep already: their claims, the school's reports,
    the reports an answer placed there, and the texts of the school's instructions, read
    for every such row at once the first time a comparison asks, so a text of any length
    costs the same few reads."""

    def __init__(
        self,
        store: ProjectStateStore,
        rows: Sequence[Assignment],
        decided: Mapping[tuple[str, str], tuple[IntakeDecision, ...]],
    ) -> None:
        self._store = store
        self._names = [row.assignment_id for row in rows]
        self._placed: dict[str, list[Mapping[str, str | None]]] = {}
        for answers in decided.values():
            for item in answers:
                if item.kind == "report_placed" and item.report is not None:
                    self._placed.setdefault(item.lands_on, []).append(item.report)

    @functools.cached_property
    def _kept(
        self,
    ) -> tuple[dict[str, list[SourceRecord]], dict[str, list[StatusReport]], InstructionReadings]:
        return (
            self._store.deadline_records_by_assignment(self._names),
            self._store.status_reports_by_assignment(self._names),
            self._store.school_instruction_readings(self._names),
        )

    def claims(self, assignment_id: str) -> list[SourceRecord]:
        return self._kept[0].get(assignment_id, [])

    def reports(self, assignment_id: str) -> list[StatusReport]:
        return self._kept[1].get(assignment_id, [])

    def placed(self, assignment_id: str) -> list[Mapping[str, str | None]]:
        """The reports an answer placed on a row, as the answer keeps them: a second line
        of one day that the school's reports count as the first is still placed there."""
        return self._placed.get(assignment_id, [])

    def texts(self, assignment_id: str) -> set[str]:
        """The school's instructions kept under a row, by text; none where they can't be
        read, so nothing is taken as saved there."""
        standing = self._kept[2].readable.get(assignment_id)
        return set() if standing is None else {item.text for item in standing.kept}

    def noted(self, assignment_id: str) -> set[tuple[str, date | None]]:
        """The school's instructions kept under a row, each by its words, single-spaced,
        and the day it was first read under; none where they can't be read, so nothing is
        taken as saved there."""
        standing = self._kept[2].readable.get(assignment_id)
        if standing is None:
            return set()
        return {(" ".join(item.text.split()), item.card_day) for item in standing.kept}


def changes_for(
    items: tuple[Reading, ...],
    store: ProjectStateStore,
    *,
    occurrences: Mapping[int, str] | None = None,
    kinds: Mapping[int, AssignmentKind] | None = None,
    instruction_answers: Mapping[int, InstructionChoice | SubmittedChoice] | None = None,
    identities: Mapping[int, IdentityAnswer] | None = None,
    picks: Mapping[int, Pick] | None = None,
    left_out: Collection[int] | None = None,
) -> list[Change]:
    """Compare each reading with the record: what is new, what is known, what a paste adds.

    Which homework a reading is about follows ``_where``: where the record
    cannot tell, the card asks, lists the homework it could be, and lands
    nowhere until ``identities`` answers it. An answer made against the facts
    as they stand lands the card and is kept with it; one that is kept
    already lands it and keeps nothing more; one made against facts that
    changed since marks the card stale and writes nothing.

    A card that asks, or that would save a new assignment as the first card
    of its name in the text, offers to find homework already here under
    another name, and ``picks`` holds what was found and chosen there. A
    choice made against the card and that homework as they stand lands the
    card there and is kept as a ``renamed`` answer; the later cards of the
    name in the text go there too, where they'd have joined the new one. One
    sent again once it's saved lands there and keeps nothing more; any other
    is refused on its card (``pick_refused``) and writes nothing.

    A reading matches a row by its course and title, whatever the row's id,
    so an assignment on record from a fixture or an earlier paste takes the
    claims rather than a twin; with several rows under one name, the one
    due nearest the pasted date. A date a week or more from the matched
    row's, or from another card's in the same text, is a question when the
    text adds anything, answered by ``occurrences``: ``update`` moves the
    saved date to the pasted one, or folds the later card into the first,
    and ``new`` makes a row of its own whose id carries the date, so the
    same answer given twice finds that row and changes nothing. A claim
    already on record, the same channel saying the same value from the same
    place, is not made twice, nor is a report of the same status on the same
    day, nor is either made twice by one text. The recorded due date is
    otherwise never replaced by a paste; a different date is a claim beside
    it, which the page shows as a disagreement, and a record with no due
    date takes the pasted one as its own. The assigned date is filled in
    when the record has none. A parent's typed note fills the note or
    replaces the saved one; the school's words are never a note, and are
    kept as its instructions by the store's rule, once for each assignment
    the text lands on. ``kinds`` are the parent's choices of type on the page; a
    type typed with an entry is the parent's choice as well, and one left
    unchosen there is no choice. Several cards
    about one saved row compose: each is measured against the row as the
    cards before it leave it, so a date one moves, a note another adds, and
    an assigned date a third fills all arrive together.

    ``left_out`` holds the keys of the readings left out of the save. Each
    stays in the list, in its place, as a change that lands on no row and
    brings nothing: it asks nothing, it is the first card of no name, so a
    later card of its name is measured as if the text did not have it, and
    no instruction of the school and no note of hers is decided or offered
    on it. ``None`` leaves nothing out.

    A reading made from a teacher's note meets the record by rules of its own.
    It is the note saved before on the one row of its class that keeps the
    note's words under the school day the note was first listed under,
    whatever that row's title or due date: there it brings no instruction, and
    the row's due date stands with no claim beside it, unless the family gave
    the date the reading carries. Otherwise its class and title alone put it
    on no row (``_where_noted``): a row of its name is a question unless it is
    due the day the note settles or a kept answer says so. Under the name of
    an earlier new reading of the same text it asks whether it is the same
    assignment or new work, however near its date or with none; said to be the
    same with no date of its own, it leaves the first reading's due date as it
    is. Said to be new work, here or beside a saved row, it makes a row whose
    id carries the note's first school day and a mark of its words, not its
    date, so two notes of one name that settle one date are two rows.
    """
    occurrences = occurrences or {}
    kinds = kinds or {}
    identities = identities or {}
    picks = picks or {}
    on_record: dict[tuple[str, str], list[Assignment]] = {}
    by_id: dict[str, Assignment] = {}
    for item in store.all_assignments():
        on_record.setdefault(pair(item.course, item.title), []).append(item)
        by_id[item.assignment_id] = item
    mine = store.assignments_made_from_notes()
    apart = store.kept_apart()
    decided = store.intake_decisions(reading.pair for reading in items)
    names = dict.fromkeys(reading.pair for reading in items)
    compared = {item.assignment_id: item for name in names for item in on_record.get(name, [])}
    # Homework under another name a card can land on is read with the rest: what a parent
    # chose for a name before, and what's chosen on the cards now.
    for name in [
        *(
            item.lands_on
            for answers in decided.values()
            for item in answers
            if item.kind == "renamed"
        ),
        *(pick.target for pick in picks.values()),
    ]:
        if name in by_id:
            compared.setdefault(name, by_id[name])
    # A teacher's note saved before can stand under another title, so a reading made from
    # one is compared with every row of its class as well.
    noted_classes = {reading.pair[0] for reading in items if reading.teacher_note is not None}
    of_class: dict[str, list[Assignment]] = {}
    if noted_classes:
        for item in by_id.values():
            course = pair(item.course, item.title)[0]
            if course in noted_classes:
                of_class.setdefault(course, []).append(item)
                compared.setdefault(item.assignment_id, item)
    saved = _OnRecord(store, list(compared.values()), decided)

    def writes_nothing(item: Assignment, reading: Reading, kind: AssignmentKind | None) -> bool:
        """Whether landing a reading on a row on record would write nothing: every claim,
        report, and school instruction it brings is kept there already, and nothing typed
        with it differs from the row: its note, the type the card asks for (``kind``), or an
        assigned date the row lacks. A report an answer placed there counts as saved, and a
        typed note her own note keeps off the row writes nothing."""
        name = item.assignment_id
        return (
            all(
                any(_same_claim(one, other) for other in saved.claims(name))
                for one in reading.claims
            )
            and all(
                any(_same_report_as_written(one, other) for other in saved.reports(name))
                or _placement(one) in saved.placed(name)
                for one in reading.reports
            )
            and (
                not reading.instructions
                or {seen.text for seen in reading.instructions} <= saved.texts(name)
            )
            and _note_change(item, reading) in (None, "kept")
            and kind in (None, item.kind)
            and not (reading.assigned_on is not None and item.assigned_on is None)
        )

    def holds(item: Assignment, reading: Reading, kind: AssignmentKind | None) -> bool:
        """Whether a row on record holds everything a reading brings: the same text, saved
        there before, which moves nothing when saved again. A reading made from a teacher's
        note is held where the note's words are kept under the school day the note was
        first listed under, whatever the row's title or due date."""
        note = reading.teacher_note
        if note is not None:
            words = " ".join(note.words.split())
            return (words, note.first_day) in saved.noted(item.assignment_id)
        return bool(reading.claims or reading.reports) and writes_nothing(item, reading, kind)

    seen = _Seen(saved)
    anchors: dict[tuple[str, str], int] = {}
    """The place in ``changes`` of the first new reading under each name in this text."""
    pending: dict[str, Assignment] = {}
    """Each saved row as the cards read so far leave it."""
    chosen_here: dict[tuple[str, str], Assignment] = {}
    """Homework chosen for a name on a card of this text that would have saved it new."""
    separated = {frozenset((name, other)) for name, others in apart.items() for other in others}
    extras: dict[int, dict[str, object]] = {}
    changes: list[Change] = []
    for key, reading in enumerate(items):
        if left_out is not None and key in left_out:
            changes.append(_left_out(key, reading, kinds))
            continue
        answer = occurrences.get(key)
        known = decided.get(reading.pair, ())
        kind = kinds.get(key, reading.kind if reading.kind_chosen else None)
        remembered = [
            by_id[item.lands_on]
            for item in known
            if item.kind == "renamed" and item.lands_on in by_id
        ]
        if reading.pair in chosen_here:
            remembered.append(chosen_here[reading.pair])
        if reading.teacher_note is not None:
            # The rows of the note's class that keep its words under its first day,
            # whatever their title.
            remembered.extend(
                row for row in of_class.get(reading.pair[0], ()) if holds(row, reading, kind)
            )
        where = _where(
            reading,
            on_record.get(reading.pair, []),
            mine,
            known,
            functools.partial(holds, kind=kind),
            apart,
            functools.partial(writes_nothing, kind=kind),
            list({item.assignment_id: item for item in remembered}.values()),
        )
        reply = identities.get(key)
        pick = picks.get(key)
        decision: DecisionToKeep | None = None
        stale = False
        here = tuple(item for item in known if _answers(item, reading))
        offers = None
        if where.asks is not None:
            offers = identity_basis(where.candidates, here, mine)
            listed = {item.assignment_id for item in where.candidates}
            apart_here = separated | {
                frozenset((item.lands_on, name))
                for item in known
                if item.kind == "different"
                for name in item.shown
            }
            extras[key] = {"kept_separate": any(both <= listed for both in apart_here)}
        elif where.lands is None and reading.pair not in anchors:
            offers = WOULD_BE_NEW
        extras.setdefault(key, {})["offers"] = offers
        if pick is not None:
            target = by_id.get(pick.target)
            if (
                target is not None
                and where.lands is not None
                and where.lands.assignment_id == target.assignment_id
                and writes_nothing(target, reading, kind)
            ):
                # Chosen and saved before: it lands there and keeps nothing more.
                pass
            elif (
                reply is None
                and target is not None
                and pair(target.course, target.title) != reading.pair
                and pick.situation == offers
                and candidate_basis(readings_for(store, [target])) == pick.basis
            ):
                decision = _decision(
                    "renamed", reading, target.assignment_id, (target.assignment_id,), pick.basis
                )
                where = _Where(lands=target)
                extras[key]["picked"] = True
                if offers == WOULD_BE_NEW:
                    chosen_here[reading.pair] = target
            else:
                extras[key]["pick_refused"] = pick.target
        if decision is None and (reply is not None or where.asks is not None):
            shown = tuple(item.assignment_id for item in where.candidates)
            basis = "" if where.asks is None else offers or ""
            standing = None if reply is None else _stands(reply, reading, known)
            # An answer kept already is a retry only when everything the card brings is
            # saved where it landed; anything new is judged by the question as it stands.
            if (
                standing is not None
                and standing.lands_on in by_id
                and writes_nothing(by_id[standing.lands_on], reading, kind)
            ):
                where = _Where(lands=by_id[standing.lands_on])
            elif reply is None:
                changes.append(_asked(key, reading, where, shown, basis, kinds, mine))
                continue
            elif (
                where.asks is None
                or reply.basis != basis
                or (reply.choice is not None and reply.choice not in shown)
                or (
                    reply.choice is None
                    and (reply.creation is None or CREATION.fullmatch(reply.creation) is None)
                )
            ):
                stale = True
                if where.asks is not None:
                    changes.append(
                        _asked(key, reading, where, shown, basis, kinds, mine, stale=True)
                    )
                    continue
            elif reply.choice is None:
                made = f"assignment-{reply.creation}"
                decision = _decision("different", reading, made, shown, basis, reply.creation)
                change = dataclasses.replace(
                    _new_change(key, reading, None, None, False, kinds, seen, None, made),
                    made_as=made,
                    identity_decision=decision,
                )
                changes.append(change)
                continue
            else:
                answered: DecisionKind = (
                    "report_placed"
                    if where.asks == "report"
                    else "same"
                    if reply.choice in mine
                    else "which"
                )
                decision = _decision(answered, reading, reply.choice, shown, basis)
                where = _Where(lands=by_id[reply.choice])
        existing = where.lands
        if (
            reading.teacher_note is not None
            and existing is not None
            and holds(existing, reading, kind)
        ):
            reading = _as_saved(reading, existing)
        twin = None if existing is not None else anchors.get(reading.pair)
        other = None
        if existing is not None and where.today:
            other = pending.get(existing.assignment_id, existing).due_date
        if twin is not None:
            other = changes[twin].reading.due_date
        asked = (
            reading.due_date is not None
            and other is not None
            and abs(reading.due_date - other) >= ANOTHER_OCCURRENCE
        )
        if reading.teacher_note is not None and twin is not None:
            # A teacher's note under the name of an earlier reading of this text is asked
            # about, however near its date or with none: the name alone never makes it the
            # same work.
            asked = True
        if not asked or (answer == KEEP_DATE and reading.teacher_note is None):
            # A card offers no choice to keep the due date, so that is no answer on one.
            answer = None
        if twin is not None and asked and answer in (UPDATE, KEEP_DATE):
            changes[twin] = _folded(
                changes[twin], key, reading, kinds, seen, keep_date=answer == KEEP_DATE
            )
            changes.append(_folded_marker(key, reading, changes[twin].key, kinds, answer))
            continue
        if existing is None or answer == NEW_WORK:
            change = _new_change(key, reading, existing, answer, asked, kinds, seen, other)
            if change.state == NEW and change.assignment_id in by_id:
                # The row this answer would make exists, from the same answer
                # given before: it is that row, and nothing is made twice.
                existing, answer, asked = by_id[change.assignment_id], None, False
            else:
                if existing is None and reading.pair not in anchors:
                    anchors[reading.pair] = len(changes)
                changes.append(dataclasses.replace(change, identity_stale=stale))
                continue
        base = pending.get(existing.assignment_id, existing)
        change = _change_to(key, reading, existing, base, answer, asked, kinds, seen)
        change = dataclasses.replace(change, identity_stale=stale, identity_decision=decision)
        changes.append(change)
        if change.state == CLAIMED:
            row = _updated_row(change)
            if row is not None:
                pending[existing.assignment_id] = row
    changes = [
        dataclasses.replace(change, **extras[change.key])  # type: ignore[arg-type]
        if change.key in extras and change.folded_into is None
        else change
        for change in changes
    ]
    changes = _reports_together(changes, {*identities, *picks})
    changes = _with_instructions(changes, store, submitted_answers(instruction_answers or {}))
    return _with_waiting_notes(changes, store)


def _reports_together(changes: list[Change], answered: Collection[int]) -> list[Change]:
    """The school's reports read apart, shown on one card where they can mean only one
    homework: an undated card from the email, with no answer given, that lands where
    another card of the text lands, a new row included, adds its reports to that card and
    is folded in. A dated card lands by its own date and is the one shown, and it keeps
    the email's channel as the record's when a Missing line named the work first. A card
    that asks, or that an answer placed, keeps its own."""

    def told(change: Change) -> bool:
        return change.reading.due_date is None and change.reading.origin is SourceChannel.EMAIL

    lands = (NEW, CLAIMED, KNOWN)
    first: dict[str, int] = {}
    for index, change in enumerate(changes):
        if change.state in lands and not told(change):
            first.setdefault(change.assignment_id, index)
    for index, change in enumerate(changes):
        if change.state not in lands or not told(change) or change.key in answered:
            continue
        place = first.setdefault(change.assignment_id, index)
        if place == index:
            continue
        anchor = changes[place]
        reading = dataclasses.replace(
            anchor.reading,
            reports=anchor.reading.reports + change.reading.reports,
            origin=change.reading.origin if index < place else anchor.reading.origin,
        )
        changes[place] = dataclasses.replace(
            anchor, reading=reading, new_reports=anchor.new_reports + change.new_reports
        )
        changes[index] = dataclasses.replace(change, folded_into=anchor.key)
    return changes


def _with_waiting_notes(changes: list[Change], store: ProjectStateStore) -> list[Change]:
    """Each note of hers that waits, offered on every card of the text with its class and
    title, read in one statement: which homework it joins is the parent's choice, never
    the order of the cards."""
    waiting: dict[tuple[str, str], list[Capture]] = {}
    for note in store.outstanding_captures().notes:
        if note.course is not None and note.title is not None:
            waiting.setdefault(pair(note.course, note.title), []).append(note)
    for index, change in enumerate(changes):
        if change.left_out:
            continue
        notes = waiting.get(change.reading.pair)
        if notes:
            changes[index] = dataclasses.replace(change, waiting=tuple(notes))
    return changes


def submitted_answers(
    answers: Mapping[int, InstructionChoice | SubmittedChoice],
) -> dict[int, SubmittedChoice]:
    """Answers as sent, each whole: a choice a direct caller made is the answer it sends."""
    return {
        key: answer
        if isinstance(answer, SubmittedChoice)
        else SubmittedChoice(
            shown_revision=answer.shown_revision,
            shown=answer.shown,
            applies=answer.applies,
            none_applies=answer.none_applies,
        )
        for key, answer in answers.items()
    }


def answers_to_no_question(
    changes: Sequence[Change],
    answers: Mapping[int, SubmittedChoice],
    asked: Mapping[int, tuple[int, str]] | None = None,
) -> frozenset[int]:
    """The cards an answer about the school's instructions is given on where the review page
    puts no such question, from ``changes`` read with the answers about which homework each
    card is and no answer about instructions: a card the text does
    not have; a card that lands on an assignment with no decision; a card of an assignment
    that needs a choice that did not ask it in ``asked``, even where it carries the decision
    now; a card that asked at another revision or about another assignment; and a decision
    that needs no choice, where the answer would write anything.

    ``asked`` holds each card the page asked on, as the page signed it, with the revision
    it showed and the assignment it asked about, so a question the record raised since
    the page was made has no answer on it. A card that asked answers its assignment's
    question, on whichever card it is put now. A choice needed when the page was read
    is still needed now unless an instruction was kept since; then an answer sent
    again finds what it asks for standing, and any other finds the facts changed. So
    an answer that would write where no choice is needed was never an answer to a
    question the page put. A card folded into another answers for the assignment it
    joins, and only where the page asked on that card: a question the fold raises is
    one the page never asked. A card that lands nowhere yet carries no decision, and
    nothing reads its answer here; nor does a card whose answer about which homework it
    is was made against facts that changed, which leaves the whole text unsaved.
    """
    decided = {change.key: change for change in changes if change.instructions is not None}
    carriers = {change.assignment_id: change for change in decided.values()}
    cards = {change.key: change for change in changes}
    asked = asked or {}
    unlanded = {
        change.key
        for change in changes
        if change.lands_nowhere or change.identity_stale or change.left_out
    }
    never: set[int] = set()
    for key, answer in answers.items():
        if key in unlanded:
            continue
        if key in asked:
            # A choice stays with the assignment it was asked about; a card that chooses
            # nothing may be said to be other homework since.
            revision, assignment = asked[key]
            card = cards.get(key)
            if card is not None and card.folded_into is not None:
                card = cards.get(card.folded_into)
            elsewhere = card is None or card.assignment_id != assignment
            if answer.shown_revision != revision or (answer.answers and elsewhere):
                never.add(key)
                continue
        change = decided.get(key)
        if change is None:
            card = cards.get(key)
            into = None if card is None else card.folded_into
            if into is not None:
                card = cards.get(into)
            change = None if card is None else carriers.get(card.assignment_id)
            if change is None or (change.instructions_asked and key not in asked):
                never.add(key)
                continue
        elif change.instructions_asked and key not in asked:
            # The page asked which homework this card is, and nothing about its instructions.
            never.add(key)
            continue
        if isinstance(change.instructions, InstructionsNeedAChoice):
            continue
        resolved = answer.resolved(change.instructions_kept)
        choice = None if resolved is None else resolved.choice()
        if choice is not None and isinstance(
            settle(change.instructions_kept, change.instructions_seen, choice), InstructionsSettled
        ):
            never.add(key)
    return frozenset(never)


def _with_instructions(
    changes: list[Change], store: ProjectStateStore, answers: Mapping[int, SubmittedChoice]
) -> list[Change]:
    """Decide the school's instructions once for each assignment the text lands on.

    Every card that lands on one assignment, an Assigned card and a Due card
    of it among them, gives its instructions to one decision. The decision is
    carried by the first card of the text that lands on the assignment, whether
    or not that card brings an instruction of its own, and answered there; the
    others that bring one point to it. A card still waiting on whether it is new
    work lands nowhere yet, and gives nothing; an answer given on it is kept to
    say back, never read. The kept instructions of every assignment are read in
    one statement.

    Every answer given on a card that lands on the assignment, or on a card
    folded into one, answers its question, rows put back in their words
    against what is kept, and each is checked against what stands before any
    is taken with another: a row not kept here, a choice the rule refuses as
    stale, or an answer that makes no choice made against another revision or
    another set, on any card, leaves the whole assignment stale. Answers that
    ask for different things choose nothing: the question stays open, nothing
    is written, and each is kept to say back. One answer that ticks an
    instruction and that none applies is no choice either. No answer at all
    leaves the rule to decide, the first instruction of new work standing as
    it always does.
    """
    landed: dict[str, list[int]] = {}
    folded: dict[int, list[int]] = {}
    for index, change in enumerate(changes):
        if change.left_out:
            continue
        if change.folded_into is not None:
            folded.setdefault(change.folded_into, []).append(change.key)
        elif change.lands_nowhere:
            if change.key in answers:
                changes[index] = dataclasses.replace(
                    change, instructions_unsaved=(answers[change.key],)
                )
        else:
            landed.setdefault(change.assignment_id, []).append(index)
    landing = {
        assignment_id: places
        for assignment_id, places in landed.items()
        if any(changes[place].reading.instructions for place in places)
    }
    if not landing:
        return changes
    found = store.school_instruction_readings(landing)
    for assignment_id in landing:
        if assignment_id in found.unreadable:
            msg = f"the school's instructions for {assignment_id!r} cannot be read"
            raise UnreadableInstruction(msg)
    for assignment_id, places in landing.items():
        standing = found.readable.get(assignment_id)
        kept = () if standing is None else standing.kept
        revision = revision_of(kept)
        seen = tuple(item for place in places for item in changes[place].reading.instructions)
        first = changes[places[0]]
        keys = [changes[place].key for place in places]
        keys += [key for card in keys for key in folded.get(card, [])]
        given = [answers[key] for key in keys if key in answers]
        read = [answer.resolved(kept) for answer in given]
        # One answer for each thing asked for, the first card's; one naming a row not kept
        # here is said as it was sent.
        asked_for: dict[tuple[frozenset[str], frozenset[int], bool], SubmittedChoice] = {}
        for sent, item in zip(given, read, strict=True):
            said = sent if item is None else item
            asked_for.setdefault((said.applies, said.applies_rows, said.none_applies), said)
        requests = tuple(asked_for.values())
        submitted: SubmittedChoice | None = None
        answer: InstructionChoice | None = None
        unsaved: tuple[SubmittedChoice, ...] = ()
        outcome: InstructionsOutcome
        if any(item is None or overtaken(kept, seen, item) for item in read):
            outcome = InstructionChoiceStale(tuple(kept), revision)
            unsaved = requests
        elif len(requests) > 1:
            outcome = InstructionsNeedAChoice(tuple(kept), new_texts(kept, seen), revision)
            unsaved = requests
        else:
            submitted = requests[0] if requests else None
            if submitted is not None and submitted.contradicts:
                outcome = InstructionsNeedAChoice(tuple(kept), new_texts(kept, seen), revision)
            else:
                answer = None if submitted is None else submitted.choice()
                outcome = settle(kept, seen, answer)
        changes[places[0]] = dataclasses.replace(
            first,
            instructions=outcome,
            instructions_seen=seen,
            instructions_kept=kept,
            instructions_revision=revision,
            instructions_answer=answer,
            instructions_submitted=submitted,
            instructions_unsaved=unsaved,
        )
        for place in places[1:]:
            if changes[place].reading.instructions:
                changes[place] = dataclasses.replace(changes[place], instructions_with=first.key)
    return changes


def _kind_for(
    key: int, reading: Reading, saved: Assignment | None, kinds: Mapping[int, AssignmentKind]
) -> tuple[AssignmentKind, bool, AssignmentKind]:
    """The kind a reading's row will have, whether a parent chose it, on the page or with
    the entry they typed, and what the page shows for the card before any choice.

    ``kinds`` holds the parent's choices and nothing else: the page reads a
    choice as a select changed from what it showed, or a choice carried from
    a page before, and a select left as shown is no choice. A choice equal
    to the page's suggestion is still a choice when the parent made it, a
    change back from a pending type among them. The page's suggestion is
    the saved row as it was when the page was made, never the row as the
    cards before this one leave it, so a card left as suggested never undoes
    what another card about the same assignment chose.
    """
    if reading.origin == SourceChannel.PARENT_ENTRY:
        # A kind typed with the entry is the parent's; one left unchosen is
        # the saved row's, or the title's suggestion for a new row.
        theirs = reading.kind_chosen or saved is None
        suggested = reading.kind if theirs or saved is None else saved.kind
        return kinds.get(key, suggested), key in kinds or reading.kind_chosen, suggested
    suggested = reading.kind if saved is None else saved.kind
    return kinds.get(key, suggested), key in kinds, suggested


def _new_change(
    key: int,
    reading: Reading,
    existing: Assignment | None,
    answer: str | None,
    asked: bool,
    kinds: Mapping[int, AssignmentKind],
    seen: _Seen,
    beside: date | None,
    made_as: str | None = None,
) -> Change:
    """A reading with no row to land on, or one the parent said is new work, or one that
    waits on the parent's answer about ``beside``, the other date under its name.
    ``made_as`` is the id the answer that it is different homework gives the new row."""
    kind, by_parent, suggested = _kind_for(key, reading, None, kinds)
    change = Change(
        key,
        reading,
        existing,
        (),
        (),
        False,
        False,
        False,
        None,
        kind,
        by_parent,
        asked and answer is None,
        answer,
        beside=beside,
        suggested_kind=suggested,
        made_as=made_as,
    )
    if change.state == REVIEW:
        return change
    return dataclasses.replace(
        change,
        new_claims=seen.novel_claims(change.assignment_id, False, reading.claims),
        new_reports=seen.novel_reports(change.assignment_id, False, reading.reports),
    )


def _change_to(
    key: int,
    reading: Reading,
    existing: Assignment,
    base: Assignment,
    answer: str | None,
    asked: bool,
    kinds: Mapping[int, AssignmentKind],
    seen: _Seen,
) -> Change:
    """What a reading adds to the row it lands on, ``base`` being that row as the cards
    before this one leave it. A reading whose claims and reports are all on record already
    was saved before, so an answer sent with it again is not read: the same text saved
    twice moves nothing."""
    fresh = seen.novel_claims(existing.assignment_id, True, reading.claims)
    news = seen.novel_reports(existing.assignment_id, True, reading.reports)
    if not fresh and not news:
        asked, answer = False, None
    kind, by_parent, suggested = _kind_for(key, reading, existing, kinds)
    moves = asked and answer == UPDATE and reading.due_date != base.due_date
    return Change(
        key,
        reading,
        existing,
        fresh,
        news,
        base.due_date is None and reading.due_date is not None,
        moves,
        base.assigned_on is None and reading.assigned_on is not None,
        _note_change(base, reading),
        kind,
        by_parent,
        asked and answer is None,
        answer,
        beside=existing.due_date,
        base=base,
        suggested_kind=suggested,
    )


def _folded(
    anchor: Change,
    key: int,
    reading: Reading,
    kinds: Mapping[int, AssignmentKind],
    seen: _Seen,
    keep_date: bool = False,
) -> Change:
    """The first reading of a name in this text with a later card folded in, as the parent
    said: the due date is the later card's, and its claims, note, and reports come along,
    as does a type chosen on it. With ``keep_date`` the first reading keeps its due date and
    where that date came from, and the later card's date comes along as a claim only. The
    card the parent sees for the assignment is the one that decides its type: a choice made
    on it stands over any carried by a folded card, and two folded cards choosing
    differently, with none on the card shown, is a conflict."""
    kind, chosen, _ = _kind_for(key, reading, None, kinds)
    if anchor.key in kinds:
        chosen = False
    elif chosen and anchor.kind_by_parent and anchor.kind != kind:
        return dataclasses.replace(anchor, choices_conflict=True)
    first = anchor.reading
    notes = [note for note in (first.note, reading.note) if note]
    instructions = first.instructions + tuple(
        item
        for item in reading.instructions
        if all(item.text != other.text for other in first.instructions)
    )
    origins = {**reading.field_origins, **first.field_origins}
    # A teacher's note that settles no date leaves the first reading's due date as it is,
    # and so does the answer that the due date is kept.
    undated_note = keep_date or (reading.teacher_note is not None and reading.due_date is None)
    if not undated_note:
        origins["due_date"] = reading.origin_of("due_date")
    if first.assigned_on is None and reading.assigned_on is not None:
        origins["assigned_on"] = reading.origin_of("assigned_on")
    merged = dataclasses.replace(
        first,
        due_date=first.due_date if undated_note else reading.due_date,
        assigned_on=first.assigned_on or reading.assigned_on,
        claims=first.claims + reading.claims,
        note="\n".join(dict.fromkeys(notes)) or None,
        instructions=instructions,
        reports=first.reports + reading.reports,
        field_origins=origins,
    )
    return dataclasses.replace(
        anchor,
        reading=merged,
        new_claims=anchor.new_claims
        + seen.novel_claims(anchor.assignment_id, False, reading.claims),
        new_reports=anchor.new_reports
        + seen.novel_reports(anchor.assignment_id, False, reading.reports),
        kind=kind if chosen else anchor.kind,
        kind_by_parent=anchor.kind_by_parent or chosen,
    )


def _folded_marker(
    key: int,
    reading: Reading,
    into: int,
    kinds: Mapping[int, AssignmentKind],
    answer: str = UPDATE,
) -> Change:
    """The card that was folded, kept in the list so the page can carry its answers, the
    parent's word that it is the same assignment (``answer``, which also says whether the
    due date is kept) and any type chosen on it, through a page that comes back with
    another question still open."""
    kind, chosen, suggested = _kind_for(key, reading, None, kinds)
    return Change(
        key,
        reading,
        None,
        (),
        (),
        False,
        False,
        False,
        None,
        kind,
        chosen,
        False,
        answer,
        suggested_kind=suggested,
        folded_into=into,
    )


def _as_saved(reading: Reading, row: Assignment) -> Reading:
    """A reading made from a teacher's note, on the row that holds the note from a save
    before, as that save left it: the note's words are decided there, so it brings no
    instruction, and its due date is the row's, with no claim, unless the family gave the
    date it carries. Its class and title stay as read."""
    if reading.origin_of("due_date") == SourceChannel.PARENT_ENTRY:
        return dataclasses.replace(reading, instructions=())
    return dataclasses.replace(reading, instructions=(), due_date=row.due_date, claims=())


def _left_out(key: int, reading: Reading, kinds: Mapping[int, AssignmentKind]) -> Change:
    """The card of a reading left out of the save, kept in the list in its place: it lands
    on no row, brings no claim and no report, and is the first card of no name."""
    kind, chosen, suggested = _kind_for(key, reading, None, kinds)
    return Change(
        key,
        reading,
        None,
        (),
        (),
        False,
        False,
        False,
        None,
        kind,
        chosen,
        False,
        None,
        suggested_kind=suggested,
        left_out=True,
    )


def conflicting_choices(changes: list[Change]) -> list[str]:
    """The assignments whose cards choose different types, named as the page names them.

    Two cards about one assignment can each carry a choice; when the
    choices differ, no saving picks one, and the page asks instead.
    """
    chosen: dict[str, set[AssignmentKind]] = {}
    names: dict[str, str] = {}
    for change in changes:
        if change.state in (FOLDED, LEFT_OUT):
            continue
        if change.choices_conflict:
            chosen.setdefault(change.assignment_id, set()).update(AssignmentKind)
        elif change.kind_by_parent:
            chosen.setdefault(change.assignment_id, set()).add(change.kind)
        names.setdefault(change.assignment_id, f"{change.reading.course}: {change.reading.title}")
    return [names[key] for key, kinds in chosen.items() if len(kinds) > 1]


def _note_change(existing: Assignment, reading: Reading) -> str | None:
    """What a parent's typed note does to the saved note. Only a parent's entry carries a
    note; the reader keeps the school's words as instructions, never as one. The note she
    gave her own homework stays hers: a parent's note is ``kept`` off it, and never saved
    in its place."""
    if not reading.note or reading.note == existing.note:
        return None
    if existing.note_by == "student":
        return "kept"
    return "fill" if existing.note is None else "update"


def _note_moved(change: Change, shown: str | None) -> bool:
    """Whether a typed note would do something the review page didn't say: keep her note
    where the page said it wouldn't, or the other way round, or meet a saved note other
    than the one the page showed. A note that now matches what was typed writes nothing,
    so a retry passes."""
    if shown is not None and change.note_change is not None:
        return change.note_shown != shown
    said = None if shown is None else shown.partition(":")[0]
    return (change.note_change == "kept") != (said == "kept")


def task_said(change: Change) -> str:
    """What a review page signs for a reading made from a teacher's note when it is
    selected: its state, a mark of what the save does for it, and the row it lands in. The
    mark covers whether the save makes the row, the due date it writes there, what it adds
    apart from the type, the school's instructions among them, and which questions the card
    asks: which homework it is, whether it is the same assignment, and which instructions
    apply."""
    writes_date = change.state == NEW or change.fills_due_date or change.moves_due_date
    due = change.reading.due_date if writes_date else None
    said = "|".join(
        (
            "makes" if change.state == NEW else "",
            "" if due is None else due.isoformat(),
            ",".join(part for part in change.additions if part != "the type"),
            "".join(
                "1" if flag else "0"
                for flag in (
                    change.identity_asked,
                    change.ambiguous,
                    change.instructions_asked,
                    change.lands_nowhere,
                )
            ),
        )
    )
    mark = hashlib.sha256(said.encode("utf-8")).hexdigest()[:12]
    return f"{change.state}:{mark}:{change.assignment_id}"


def _task_moved(change: Change, shown: str | None, answered: bool) -> bool:
    """Whether a selected reading made from a teacher's note would do something the review
    page didn't say for it. ``shown`` is what the page signed for it: whether it was shown
    ticked, then what ``task_said`` gives for it when selected. A note the page signed
    nothing for has moved. A card that asked a question has not moved while the question is
    open or when the form answers it (``answered``), since each answer is held to its own
    check. A note of which nothing is written, in the row the page named, has not moved,
    so a retry passes. Any other note has moved unless it lands in the row the page named
    and the save does there what the page said: the same date written, the same things
    added apart from the type, and the same questions asked."""
    if change.reading.teacher_note is None or change.left_out:
        return False
    if shown is None:
        return True
    _, state, mark, row = (*shown.split(":", 3), "", "", "")[:4]
    if state == REVIEW:
        return change.state != REVIEW and not answered
    if change.state == KNOWN and state != FOLDED and row == change.assignment_id:
        return False
    stands, mark_as_it_stands, lands = task_said(change).split(":", 2)
    apart = (REVIEW, FOLDED)
    return (state if state in apart else "", mark, row) != (
        stands if stands in apart else "",
        mark_as_it_stands,
        lands,
    )


def tasks_moved(
    changes: Sequence[Change], shown: Mapping[int, str], answered: Collection[int]
) -> frozenset[int]:
    """The keys of the selected readings made from teachers' notes that would do something
    other than a review page said for them (``shown``, as the page signed it); ``answered``
    holds the cards the form answers a question on."""
    return frozenset(
        change.key
        for change in changes
        if _task_moved(change, shown.get(change.key), change.key in answered)
    )


@dataclass(frozen=True)
class Kept:
    """What one saving did, counted in assignments: rows added, rows already saved that
    changed, and rows unchanged. Two cards about one row are one row here. ``kept_notes``
    counts the typed notes left off her homework, whose own note stayed, and ``linked``
    the notes of hers the save linked to the homework their card landed on."""

    added: int
    updated: int
    unchanged: int
    kept_notes: int = 0
    linked: int = 0


def keep(
    items: tuple[Reading, ...],
    store: ProjectStateStore,
    *,
    occurrences: Mapping[int, str] | None = None,
    kinds: Mapping[int, AssignmentKind] | None = None,
    instruction_answers: Mapping[int, InstructionChoice | SubmittedChoice] | None = None,
    asked: Mapping[int, tuple[int, str]] | None = None,
    identities: Mapping[int, IdentityAnswer] | None = None,
    shown_identities: Collection[int] | None = None,
    shown_notes: Mapping[int, str] | None = None,
    links: Mapping[int, Sequence[tuple[str, int, str | None]]] | None = None,
    picks: Mapping[int, Pick] | None = None,
    offered: Mapping[int, str] | None = None,
    imported_by: Author | None = None,
    now: datetime | None = None,
    today: date | None = None,
    dates: Mapping[int, tuple[date | None, date | None]] | None = None,
    left_out: Collection[int] | None = None,
    shown_tasks: Mapping[int, str] | None = None,
) -> Kept | ChangedSinceShown | NotAsked | NotOffered | list[Change]:
    """Compare and write as one: save what the record lacks, and say what changed.

    The comparison and the write happen while the store is held for this
    caller alone, so two savings of the same text, a double press or two
    tabs, cannot both find a reading new: the second finds what the first
    wrote and adds nothing, an answer of new work included, since the row
    that answer makes is found by its id. When the record has changed since
    the preview in a way that leaves a question open, or when two cards
    about one assignment choose different types, nothing is written and the
    changes are handed back for the parent to look at again.

    The file's writer is reserved before anything is read, so nothing another
    connection writes can land between the comparison and the write. Which
    homework each row is about is judged there too: an answer made against
    facts that changed since, or a question the review page did not ask
    (``shown_identities``), leaves the whole text unsaved with what changed
    handed back; so does a typed note whose fate differs from what the page
    said (``shown_notes``), such as her note arriving after the review or
    another note saved where it would go.

    The school's instructions are decided once for each assignment, in the
    same transaction: a new instruction that needs a choice leaves the whole
    text unsaved with the question open, and a choice made against
    instructions that changed since the page was made leaves it unsaved with
    what changed handed back, whichever card it is on. ``imported_by`` is who
    pasted, kept apart from the school channel the words came from.

    ``asked`` holds the questions about the school's instructions that a signed
    page asked. When it's given, the answers are checked against it again here,
    since another connection can change which assignment a card lands on
    after the page's own check; an answer to a question the page never
    asked leaves the whole text unsaved.

    ``picks`` holds homework found under another name and chosen on a card,
    and ``offered`` the cards the page offered that on, as it signed them. A
    choice needs a page's offer: one on a card the page didn't offer it on,
    one with no page's offers at all (``offered`` of ``None``), or one beside
    an answer to the card's own question is refused as ``NotOffered``. One
    made against a card or a homework that has changed since leaves the text
    unsaved with what changed handed back.

    ``dates`` holds a due date and an assigned date a parent chose for a card's row, each
    ``None`` to leave it: they are written with the row in the same transaction, whatever the
    distance from the saved date. A paste passes none, and nothing changes for it.

    ``left_out`` holds the keys of the readings left out of the save, read as ``changes_for``
    reads it in every comparison made here: nothing is written for one, it is in none of the
    counts, and a date chosen for it in ``dates`` is not written. ``None`` leaves nothing out.

    ``shown_tasks`` holds what a signed page said each reading made from a teacher's note
    does when it is selected. When it's given, a selected note that would do something else,
    or one the page signed nothing for, leaves the whole text unsaved with what changed
    handed back and those readings named.
    """
    picks = picks or {}
    crossed = picks.keys() & (identities or {}).keys()
    unsigned = {
        key for key, pick in picks.items() if offered is None or offered.get(key) != pick.situation
    }
    if crossed or unsigned:
        return NotOffered(frozenset(crossed | unsigned))
    with store.comparing_and_writing():
        if asked is not None:
            never_put = answers_to_no_question(
                changes_for(
                    items,
                    store,
                    occurrences=occurrences,
                    kinds=kinds,
                    identities=identities,
                    picks=picks,
                    left_out=left_out,
                ),
                submitted_answers(instruction_answers or {}),
                asked,
            )
            if never_put:
                return NotAsked(never_put)
        changes = changes_for(
            items,
            store,
            occurrences=occurrences,
            kinds=kinds,
            instruction_answers=instruction_answers,
            identities=identities,
            picks=picks,
            left_out=left_out,
        )
        refused = frozenset(change.key for change in changes if change.pick_refused is not None)
        # A choice made against instructions that have changed is refused before any other
        # question is put again, so a page returned never shows it as still made.
        if refused or any(change.instructions_stale or change.identity_stale for change in changes):
            return ChangedSinceShown(changes)
        if shown_identities is not None and any(
            change.identity_asked and change.key not in shown_identities for change in changes
        ):
            return ChangedSinceShown(changes)
        if shown_notes is not None and any(
            _note_moved(change, shown_notes.get(change.key))
            for change in changes
            if not change.left_out
        ):
            return ChangedSinceShown(changes)
        if shown_tasks is not None:
            answered = {
                *(occurrences or {}),
                *(identities or {}),
                *picks,
                *(instruction_answers or {}),
            }
            not_as_said = tasks_moved(changes, shown_tasks, answered)
            if not_as_said:
                return ChangedSinceShown(changes, not_as_said)
        if any(change.state == REVIEW for change in changes) or conflicting_choices(changes):
            return changes
        # Each note ticked to link goes to the homework its card landed on when the page was
        # made, or where an answer now lands a card that asked, and nowhere else; it must
        # still wait as the page showed it, and one joined there already is what was
        # asked. Anything else leaves the text unsaved. A reading left out of the save lands
        # on no homework, so a note ticked on it is linked nowhere.
        linking: list[tuple[str, str, int]] = []
        for change in changes:
            if change.left_out:
                continue
            for capture_id, revision, lands in (links or {}).get(change.key, ()):
                target = change.assignment_id
                if lands not in (None, target):
                    return ChangedSinceShown(changes)
                standing = store.paste_link_standing(
                    capture_id, target=target, expected_revision=revision
                )
                if standing == "changed":
                    return ChangedSinceShown(changes)
                if standing == "waits":
                    linking.append((capture_id, target, revision))
        rows: dict[str, Assignment] = {}
        claims: dict[str, list[SourceRecord]] = {}
        reports: dict[str, list[StatusReport]] = {}
        added: set[str] = set()
        updated: set[str] = set()
        unchanged: set[str] = set()
        for change in changes:
            if change.state in (FOLDED, LEFT_OUT):
                continue
            if change.state == KNOWN:
                unchanged.add(change.assignment_id)
                continue
            if change.state == NEW:
                added.add(change.assignment_id)
                rows[change.assignment_id] = change.reading.assignment(
                    assignment_id=change.assignment_id,
                    kind=change.kind,
                    kind_by=SourceChannel.PARENT_ENTRY if change.kind_by_parent else None,
                )
            else:
                updated.add(change.assignment_id)
                row = _updated_row(change)
                if row is not None:
                    rows[change.assignment_id] = row
            claims.setdefault(change.assignment_id, []).extend(change.new_claims)
            reports.setdefault(change.assignment_id, []).extend(change.new_reports)
        for key, (due, assigned) in sorted((dates or {}).items()):
            dated = next(
                (c for c in changes if c.key == key and c.state not in (FOLDED, LEFT_OUT)), None
            )
            if dated is None:
                continue
            target = dated.assignment_id
            current = rows.get(target) or dated.standing
            if current is None:
                continue
            moved: dict[str, object] = {}
            origins = dict(current.origins)
            if due is not None and current.due_date != due:
                moved["due_date"] = due
                origins["due_date"] = dated.reading.origin_of("due_date")
            if assigned is not None and current.assigned_on != assigned:
                moved["assigned_on"] = assigned
                origins["assigned_on"] = dated.reading.origin_of("assigned_on")
            if moved:
                rows[target] = current.model_copy(update={**moved, "origins": origins})
                if target in unchanged:
                    unchanged.discard(target)
                    updated.add(target)
        store.put_on_record(rows.values(), claims, reports)
        moment = store.instruction_moment()
        for change in changes:
            if change.identity_decision is None or change.state == FOLDED:
                continue
            store.record_intake_decision(
                change.identity_decision,
                authored_by=imported_by,
                channel=SourceChannel.PARENT_ENTRY,
                now=now or moment[0],
                today=today or moment[1],
            )
        for change in changes:
            if not isinstance(change.instructions, InstructionsSettled):
                continue
            settled = store.settle_school_instructions(
                change.assignment_id,
                change.instructions_seen,
                change.instructions_answer,
                authored_by=imported_by,
                now=now or moment[0],
                today=today or moment[1],
            )
            if not isinstance(settled, InstructionsSettled):
                # Decided a moment ago in this same transaction, so nothing can have come
                # between; a different outcome means something is wrong, and nothing of the
                # text is kept.
                msg = f"the school's instructions for {change.assignment_id!r} moved under the save"
                raise RuntimeError(msg)
        for capture_id, target, revision in linking:
            row = store.one_assignment(target)
            done = store.link_capture_from_paste(
                capture_id,
                target=target,
                expected_revision=revision,
                basis=candidate_basis(readings_for(store, [] if row is None else [row])),
                authored_by=imported_by or "household",
                channel=SourceChannel.PARENT_ENTRY,
                now=now or moment[0],
                today=today or moment[1],
            )
            if not isinstance(done, CapturePromoted):
                # The homework was written a moment ago in this same transaction, under
                # the note's own class and title; anything else rolls the text back.
                raise CaptureNotSaved(capture_id, RuntimeError("the homework to link moved"))
    return Kept(
        added=len(added),
        updated=len(updated - added),
        unchanged=len(unchanged - added - updated),
        kept_notes=sum(1 for change in changes if change.note_change == "kept"),
        linked=len(linking),
    )


def _updated_row(change: Change) -> Assignment | None:
    """The row the reading lands on, as the cards before it leave it, with what this one
    fills or corrects; ``None`` for no change."""
    existing = change.standing
    if existing is None:
        return None
    filled: dict[str, object] = {}
    origins = dict(existing.origins)
    if change.fills_due_date or change.moves_due_date:
        filled["due_date"] = change.reading.due_date
        origins["due_date"] = change.reading.origin_of("due_date")
    if change.fills_assigned_on:
        filled["assigned_on"] = change.reading.assigned_on
        origins["assigned_on"] = change.reading.origin_of("assigned_on")
    if change.note_change in ("fill", "update"):
        filled["note"] = change.reading.note
        origins["note"] = change.reading.origin_of("note")
    if change.new_kind is not None:
        filled["kind"] = change.new_kind
        origins["kind"] = SourceChannel.PARENT_ENTRY
    if change.new_reports:
        filled["reported_submission_status"] = change.new_reports[-1].status
    if not filled:
        return None
    filled["origins"] = origins
    return existing.model_copy(update=filled)


def monday_of(day: date) -> date:
    """The Monday that starts the school week a day falls in."""
    return day - timedelta(days=day.weekday())


@dataclass(frozen=True)
class WeekGroup:
    """The changes due in one school week, Monday to Sunday, or the undated ones."""

    start: date | None
    changes: list[Change]

    @property
    def label(self) -> str:
        """The week as the review page heads it, by the Monday it starts on."""
        if self.start is None:
            return "No due date yet"
        return f"Week of Monday, {self.start.strftime('%B')} {self.start.day}, {self.start.year}"

    def count(self, state: str) -> int:
        """How many of the week's changes are in one state."""
        return sum(1 for change in self.changes if change.state == state)


def by_week(changes: list[Change]) -> list[WeekGroup]:
    """The changes grouped by the school week of their due date, earliest first, undated last."""
    groups: dict[date | None, list[Change]] = {}
    for change in changes:
        if change.state == FOLDED:
            continue
        due = change.reading.due_date
        groups.setdefault(None if due is None else monday_of(due), []).append(change)
    dated = sorted(start for start in groups if start is not None)
    ordered = [WeekGroup(start, sorted(groups[start], key=_by_due)) for start in dated]
    if None in groups:
        ordered.append(WeekGroup(None, groups[None]))
    return ordered


def notes_apart(
    changes: Sequence[Change],
) -> tuple[list[Change], list[Change], list[Change]]:
    """The changes the review shows week by week, those of notes that may be homework, and
    those of notes that say no homework is assigned, each in the order given.

    A card is with the weeks, and so is a note read as homework, selected or not. A note
    whose task is unclear, or is not an assignment, is set apart whether or not it is
    selected. A folded change stays with the weeks, where no card is shown for it.
    """
    weeks: list[Change] = []
    apart: dict[Standing, list[Change]] = {Standing.UNCLEAR: [], Standing.NOT_ASSIGNMENT: []}
    for change in changes:
        task = change.reading.teacher_note
        if task is not None and task.standing in apart and change.state != FOLDED:
            apart[task.standing].append(change)
        else:
            weeks.append(change)
    return weeks, apart[Standing.UNCLEAR], apart[Standing.NOT_ASSIGNMENT]


def _by_due(change: Change) -> tuple[date, str, str]:
    return (change.reading.due_date or date.max, change.reading.course, change.reading.title)
