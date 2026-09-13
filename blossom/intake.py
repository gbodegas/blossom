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
  backslash anywhere else is the teacher's.
- The email: ``MM/DD <Course> - <Section>: <Category>: <Title> Grade:
  Missing``, one line per assignment, under ``Assignments:``. Each such line
  is the school reporting the assignment missing, kept as a report with the
  day: the email's own date when the paste carries its date line, otherwise
  the day it was pasted, and the report says which. The date beside the
  assignment is kept as the text it is, since the email does not say what it
  means; it is not a due date. A line in that shape with any other grade is
  not the "Missing" email and is left for review.

One item appears under an assigned day and under a due day, often in two
weeks, and is one assignment matched by its course and title, exactly as the
portal writes them: the date in its own line and the date in the header are
two claims from the same channel, told apart by where each was read. The
record is matched the same way, by the pair, so an assignment already on
record under any id takes the claims rather than a twin. Work that comes
round again under the same name, a weekly practice, is the one case the
pair cannot settle: when the pasted due date is a week or more past the
recorded one, the parent is asked whether it is the same assignment moved
or new work, and the answer is kept. A new row's id is the title made
readable plus a hash of the pair, so two titles that read alike never
become one. Titles are kept as the portal writes them, punctuation and all;
a course is kept as written too, grade prefix included, since that is how
the portal names it everywhere. Forms to sign, books to cover, and supplies
to bring are tasks, not homework, and a parent can say otherwise. The
heading's first name is read as nothing and never kept.
"""

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Final

from blossom.reconciliation import CHANNEL_NAMES, SourceChannel, SourceRecord
from blossom.stores.project_state import (
    Assignment,
    AssignmentKind,
    ProjectStateStore,
    StatusReport,
)

PORTAL_CONFIDENCE: Final = 0.9
"""The portal's own page, pasted whole: the school's word, as it wrote it."""
FAMILY_CONFIDENCE: Final = 0.8
"""A parent's own entry: sure of the item, less sure of the date they typed."""

OWN_LINE: Final = "the assignment's own line"
DAY_HEADER: Final = "the day's header"
EMAIL_DATE_LINE: Final = "the email's date line"
PASTE_DAY: Final = "the day it was pasted"

NOTE_MAX_LENGTH: Final = 500
"""How long a note a parent types may be; a teacher's is kept as the portal shows it."""
ANOTHER_OCCURRENCE: Final = timedelta(days=7)
"""How far past the recorded due date a pasted one must be to ask whether it is new work."""
UPDATE: Final = "update"
NEW_WORK: Final = "new"

TASK_THING: Final = re.compile(
    r"\b(?:syllabus|binders?|supplies|permission|dividers|book\s+covers?)\b", re.IGNORECASE
)
"""Things a title names that make it a task: paperwork and materials, not a sitting."""
TASK_DOING: Final = re.compile(
    r"\b(?:sign(?:ed|ing|ature)?|cover(?:ed|ing|s)?|bring(?:ing)?)\b", re.IGNORECASE
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
"""A date as a mail program writes it, ``Sep 9, 2026`` or ``September 9, 2026``, anywhere in
the pasted email: the day the school reported, when the paste carries it."""
COURSE_LENGTH: Final = 60
"""A course line is short; a longer plain line is a teacher's instruction or a stray."""
TEXT_MAX_LENGTH: Final = 40_000
"""How much one paste may hold: a page or a week is a few thousand characters."""
IDENTITY: Final = uuid.UUID("5b0f9b2e-2a3c-4d0e-9b7a-0b2f8a1c6d33")
"""The namespace an assignment's id is drawn from, so the same pair always gives the same id."""


def pair(course: str, title: str) -> tuple[str, str]:
    """The course and title as the record matches them: their words, single-spaced."""
    return " ".join(course.split()), " ".join(title.split())


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
    """What the teacher wrote under the card, or what a parent typed, as written."""
    reports: tuple[StatusReport, ...] = ()
    """What the school reports about the assignment's status, with the day."""
    at_line: int | None = None
    """The first line of the pasted text this reading was read from, counted from one."""

    @property
    def pair(self) -> tuple[str, str]:
        """What the reading is matched by, in the paste and against the record."""
        return pair(self.course, self.title)

    @property
    def assignment_id(self) -> str:
        """The id a new row would have; a row already on record keeps its own."""
        return identity(self.course, self.title)

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
            origins["note"] = self.origin
        if self.due_date is not None:
            origins["due_date"] = self.origin
        if self.assigned_on is not None:
            origins["assigned_on"] = self.origin
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
class Read:
    """Everything one paste says: the readings, and the lines that need review."""

    items: tuple[Reading, ...]
    unread: tuple[Unread, ...]


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


def email_date(text: str) -> date | None:
    """The day the email says it was sent, when the paste carries its date line."""
    found = EMAIL_DATE.search(text)
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
    notes: list[list[str]] = field(default_factory=list)
    """The instruction under each card the item was seen on, one block per card."""

    def reading(self) -> Reading:
        blocks: list[str] = []
        for lines in self.notes:
            block = "\n".join(lines)
            if block and block not in blocks:
                blocks.append(block)
        return Reading(
            course=self.course,
            title=self.title,
            due_date=self.due_date,
            assigned_on=self.assigned_on,
            kind=kind_of(self.title),
            claims=tuple(self.claims),
            origin=self.origin,
            note="\n".join(blocks) or None,
            reports=tuple(self.reports),
            at_line=self.at_line,
        )


def read_text(text: str, *, now: datetime, today: date) -> Read:
    """Read pasted text in the portal's shapes or the email's, in one pass over its lines.

    A plain line just before a card's line is the card's course, on the
    homework page; the summary names the course on the card's own line.
    Plain lines just after a card are the teacher's instruction, until the
    next card, day, or course line, or a blank; the same instruction under
    both of an item's days is kept once. A line shaped like a day or a card
    that does not read as one is text that needs review, and it ends the
    card before it, so nothing after it is taken for that card's words. Any
    other plain line outside a card needs review too.
    """
    lines = [_unbroken(line) for line in text.splitlines()]
    following = _next_lines(lines)
    reported_on, dated_by = email_date(text), EMAIL_DATE_LINE
    if reported_on is None:
        reported_on, dated_by = today, PASTE_DAY
    drafts: dict[tuple[str, str], _Draft] = {}
    unread: list[Unread] = []
    day: date | None = None
    course: str | None = None
    last: _Draft | None = None
    for index, line in enumerate(lines):
        number = index + 1
        if not line:
            last = None
            continue
        if line.lower() in NOISE or WEEK_LINE.match(line):
            last = None
            continue
        if HEADING_LINE.match(line) and last is None:
            # The summary's heading, outside a card; under a card the same
            # words are a teacher's instruction and are kept as one.
            continue
        if day_match := DAY_LINE.match(line) or SUMMARY_DAY_LINE.match(line):
            day = a_date(day_match.group("year"), day_match.group("month"), day_match.group("day"))
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
            )
            draft.reports.append(
                StatusReport(
                    status=MISSING,
                    channel=SourceChannel.EMAIL,
                    reported_on=reported_on,
                    dated_by=dated_by,
                    observed_at=now,
                    source_date_text=email.group("date"),
                )
            )
            continue
        card_course, body = course, line
        if combined := CARD_LINE.match(line):
            card_course, body = combined.group("course").strip(), combined.group("card")
        if assigned := ASSIGNED_LINE.match(body):
            when = a_date(assigned.group("year"), assigned.group("month"), assigned.group("day"))
            if card_course is None or when is None:
                unread.append(Unread(number, line))
                last = None
                continue
            draft = _draft_for(
                drafts, card_course, assigned.group("title").strip(), SourceChannel.LMS, number
            )
            draft.claims.append(claim(SourceChannel.LMS, when, OWN_LINE, now, PORTAL_CONFIDENCE))
            if draft.due_date is None:
                draft.due_date = when
            if draft.assigned_on is None:
                draft.assigned_on = day
            draft.notes.append([])
            last = draft
            continue
        if due := DUE_LINE.match(body):
            title = due.group("title").strip()
            if card_course is None or day is None or not title:
                unread.append(Unread(number, line))
                last = None
                continue
            draft = _draft_for(drafts, card_course, title, SourceChannel.LMS, number)
            draft.claims.append(claim(SourceChannel.LMS, day, DAY_HEADER, now, PORTAL_CONFIDENCE))
            if draft.due_date is None:
                draft.due_date = day
            draft.notes.append([])
            last = draft
            continue
        if LOOKS_STRUCTURAL.match(line):
            # Shaped like a day, a card, or a due date, and not read as one:
            # for review, and never a teacher's words.
            unread.append(Unread(number, line))
            last = None
            continue
        if _is_course_line(line, following[index]):
            course = line
            last = None
            continue
        if last is not None:
            last.notes[-1].append(line)
            continue
        unread.append(Unread(number, line))
    return Read(items=tuple(draft.reading() for draft in drafts.values()), unread=tuple(unread))


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
    drafts: dict[tuple[str, str], _Draft],
    course: str,
    title: str,
    origin: SourceChannel,
    number: int,
) -> _Draft:
    key = pair(course, title)
    if key not in drafts:
        drafts[key] = _Draft(course=key[0], title=key[1], origin=origin, at_line=number)
    return drafts[key]


def _is_course_line(line: str, following: str | None) -> bool:
    """A short plain line whose next line is a card's line names the card's course."""
    if len(line) > COURSE_LENGTH or line.endswith(":") or following is None:
        return False
    return bool(ASSIGNED_LINE.match(following) or DUE_LINE.match(following))


def by_hand(
    course: str,
    title: str,
    due_date: date | None,
    assigned_on: date | None,
    kind: AssignmentKind,
    note: str | None = None,
    *,
    now: datetime,
) -> Reading:
    """One assignment as a parent typed it: the family's own, in every field it fills."""
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
        kind=kind,
        claims=claims,
        origin=SourceChannel.PARENT_ENTRY,
        note=(note or "").strip() or None,
    )


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
NOTE_STANDS: Final = "The saved note stands; the pasted one differs and is not saved."


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
    fills_assigned_on: bool
    note_change: str | None
    """``fill`` when the record has no note and the reading has one; ``update`` when the
    school's text replaces the school's earlier text; ``kept`` when the saved note
    stands and the pasted one differs; ``None`` when there is nothing to say."""
    kind: AssignmentKind
    """The kind the row will have: the reader's suggestion, or the parent's choice."""
    kind_by_parent: bool
    ambiguous: bool
    """Whether the pasted work may be a new round under a saved name, unanswered."""
    occurrence: str | None
    """The parent's answer, ``update`` or ``new``, when one was given."""

    @property
    def assignment_id(self) -> str:
        """The row the reading lands in: the record's own when it has one, else a new one."""
        if self.on_record is not None and self.occurrence != NEW_WORK:
            return self.on_record.assignment_id
        if self.occurrence == NEW_WORK and self.reading.due_date is not None:
            return identity(
                self.reading.course, self.reading.title, self.reading.due_date.isoformat()
            )
        return self.reading.assignment_id

    @property
    def new_kind(self) -> AssignmentKind | None:
        """The kind a row on record would change to, when the parent chose another."""
        if self.on_record is None or not self.kind_by_parent or self.on_record.kind == self.kind:
            return None
        return self.kind

    @property
    def additions(self) -> tuple[str, ...]:
        """What a reading adds to a row already on record, in the review page's words."""
        parts = []
        if self.fills_due_date:
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
        return tuple(parts)

    @property
    def state(self) -> str:
        """``review`` for a question the parent must answer, else ``new``, ``claimed``, or
        ``known``."""
        if self.ambiguous:
            return REVIEW
        if self.on_record is None or self.occurrence == NEW_WORK:
            return NEW
        if self.additions:
            return CLAIMED
        return KNOWN

    @property
    def label(self) -> str:
        """The state as the review page says it."""
        if self.state == REVIEW:
            return "Needs your answer"
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
        if self.state == REVIEW:
            return (
                "The saved assignment has an earlier due date. Say whether this is the same "
                "assignment with a later date, or new work under the same name."
            )
        if self.state == NEW:
            return "Saved as a new assignment."
        if self.state == KNOWN:
            if self.note_change == "kept":
                return f"Nothing changes. {NOTE_STANDS}"
            return "Nothing changes."
        parts = []
        if self.fills_due_date:
            parts.append("The record has no due date of its own, so the pasted date becomes it.")
        elif self.new_claims and self.dates_differ:
            parts.append(
                "The pasted date is added as evidence beside the saved date, which stays; "
                "her page will say the sources disagree."
            )
        elif self.new_claims:
            parts.append("The pasted date is added as evidence for the saved date.")
        if self.new_reports:
            parts.append("What the school reports is saved with the day.")
        if self.fills_assigned_on:
            parts.append("The assigned date is filled in.")
        if self.note_change == "fill":
            parts.append("The note is saved.")
        elif self.note_change == "update" and self.reading.origin == SourceChannel.PARENT_ENTRY:
            parts.append("Your note replaces the saved note.")
        elif self.note_change == "update":
            parts.append("The school's note replaces the school's earlier note.")
        elif self.note_change == "kept":
            parts.append(NOTE_STANDS)
        if self.new_kind is not None:
            parts.append(f"The type becomes {self.new_kind.value.lower()}, as chosen.")
        return " ".join(parts)


def changes_for(
    items: tuple[Reading, ...],
    store: ProjectStateStore,
    *,
    occurrences: Mapping[int, str] | None = None,
    kinds: Mapping[int, AssignmentKind] | None = None,
) -> list[Change]:
    """Compare each reading with the record: what is new, what is known, what a paste adds.

    A reading matches a row by its course and title, whatever the row's id,
    so an assignment on record from a fixture or an earlier paste takes the
    claims rather than a twin; with several rows under one name, the one
    due on the pasted date, else the latest. A pasted due date a week or
    more past the saved one is a question, answered by ``occurrences``: the
    same assignment moved, or new work. A claim already on record, the same
    channel saying the same value from the same place, is not made twice,
    nor is a report of the same status on the same day. The recorded due
    date is never replaced by a paste; a different date is a claim beside
    it, which the page shows as a disagreement, and a record with no due
    date takes the pasted one as its own. The assigned date is filled in
    when the record has none. The note is filled in when the record has
    none; a parent's note replaces any saved note, the school's note
    replaces the school's earlier note, and the school's never replaces a
    parent's. ``kinds`` are the parent's choices of type.
    """
    occurrences = occurrences or {}
    kinds = kinds or {}
    on_record: dict[tuple[str, str], list[Assignment]] = {}
    for item in store.all_assignments():
        on_record.setdefault(pair(item.course, item.title), []).append(item)
    changes: list[Change] = []
    for key, reading in enumerate(items):
        answer = occurrences.get(key)
        existing = _match(on_record.get(reading.pair, []), reading)
        suggested = reading.kind if existing is None else existing.kind
        chosen_kind = kinds.get(key, suggested)
        by_parent = key in kinds and kinds[key] != suggested
        if existing is None or answer == NEW_WORK:
            changes.append(
                Change(
                    key,
                    reading,
                    existing,
                    reading.claims,
                    reading.reports,
                    False,
                    False,
                    None,
                    chosen_kind,
                    by_parent,
                    False,
                    answer,
                )
            )
            continue
        ambiguous = (
            answer is None
            and existing.due_date is not None
            and reading.due_date is not None
            and reading.due_date - existing.due_date >= ANOTHER_OCCURRENCE
        )
        had = {
            (record.channel, record.asserted_value, record.seen_in)
            for record in store.deadline_records(existing.assignment_id)
        }
        fresh = tuple(
            record
            for record in reading.claims
            if (record.channel, record.asserted_value, record.seen_in) not in had
        )
        said = {
            (report.channel, report.status, report.reported_on)
            for report in store.status_reports(existing.assignment_id)
        }
        news = tuple(
            report
            for report in reading.reports
            if (report.channel, report.status, report.reported_on) not in said
        )
        fills_due = existing.due_date is None and reading.due_date is not None
        fills = existing.assigned_on is None and reading.assigned_on is not None
        changes.append(
            Change(
                key,
                reading,
                existing,
                fresh,
                news,
                fills_due,
                fills,
                _note_change(existing, reading),
                chosen_kind,
                by_parent,
                ambiguous,
                answer,
            )
        )
    return changes


def _match(rows: list[Assignment], reading: Reading) -> Assignment | None:
    """The row a reading lands on among those under its name: the one due nearest its
    date, a row with no date last; with no date in the reading, the latest."""
    if not rows:
        return None
    if reading.due_date is None:
        return max(rows, key=lambda row: (row.due_date or date.min, row.assignment_id))
    pasted = reading.due_date

    def distance(row: Assignment) -> tuple[int, str]:
        apart = timedelta.max if row.due_date is None else abs(row.due_date - pasted)
        return (apart.days, row.assignment_id)

    return min(rows, key=distance)


def _note_change(existing: Assignment, reading: Reading) -> str | None:
    if not reading.note or reading.note == existing.note:
        return None
    if existing.note is None:
        return "fill"
    if reading.origin == SourceChannel.PARENT_ENTRY:
        return "update"
    if existing.origins.get("note", SourceChannel.LMS) == SourceChannel.LMS:
        return "update"
    return "kept"


@dataclass(frozen=True)
class Kept:
    """What one saving did: rows added, rows already saved that changed, and rows unchanged."""

    added: int
    updated: int
    unchanged: int


def keep(
    items: tuple[Reading, ...],
    store: ProjectStateStore,
    *,
    occurrences: Mapping[int, str] | None = None,
    kinds: Mapping[int, AssignmentKind] | None = None,
) -> Kept | list[Change]:
    """Compare and write as one: save what the record lacks, and say what changed.

    The comparison and the write happen while the store is held for this
    caller alone, so two savings of the same text, a double press or two
    tabs, cannot both find a reading new: the second finds what the first
    wrote and adds nothing. When the record has changed since the preview in
    a way that leaves a question open, nothing is written and the changes
    are handed back for the parent to look at again.
    """
    with store.exclusively():
        changes = changes_for(items, store, occurrences=occurrences, kinds=kinds)
        if any(change.state == REVIEW for change in changes):
            return changes
        rows: list[Assignment] = []
        claims: dict[str, list[SourceRecord]] = {}
        reports: dict[str, list[StatusReport]] = {}
        added = updated = unchanged = 0
        for change in changes:
            if change.state == KNOWN:
                unchanged += 1
                continue
            if change.state == NEW:
                added += 1
                rows.append(
                    change.reading.assignment(
                        assignment_id=change.assignment_id,
                        kind=change.kind,
                        kind_by=SourceChannel.PARENT_ENTRY if change.kind_by_parent else None,
                    )
                )
            else:
                updated += 1
                row = _updated_row(change)
                if row is not None:
                    rows.append(row)
            if change.new_claims:
                claims[change.assignment_id] = list(change.new_claims)
            if change.new_reports:
                reports[change.assignment_id] = list(change.new_reports)
        store.put_on_record(rows, claims, reports)
    return Kept(added=added, updated=updated, unchanged=unchanged)


def _updated_row(change: Change) -> Assignment | None:
    """The saved row with what the reading fills or corrects, or ``None`` for no change."""
    existing = change.on_record
    if existing is None:
        return None
    filled: dict[str, object] = {}
    origins = dict(existing.origins)
    if change.fills_due_date:
        filled["due_date"] = change.reading.due_date
        origins["due_date"] = change.reading.origin
    if change.fills_assigned_on:
        filled["assigned_on"] = change.reading.assigned_on
        origins["assigned_on"] = change.reading.origin
    if change.note_change in ("fill", "update"):
        filled["note"] = change.reading.note
        origins["note"] = change.reading.origin
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
        due = change.reading.due_date
        groups.setdefault(None if due is None else monday_of(due), []).append(change)
    dated = sorted(start for start in groups if start is not None)
    ordered = [WeekGroup(start, sorted(groups[start], key=_by_due)) for start in dated]
    if None in groups:
        ordered.append(WeekGroup(None, groups[None]))
    return ordered


def _by_due(change: Change) -> tuple[date, str, str]:
    return (change.reading.due_date or date.max, change.reading.course, change.reading.title)
