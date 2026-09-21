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
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Final

from blossom.pairing import pair as pair  # the one rule, kept where both askers share it
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
    """What the teacher wrote under the card, or what a parent typed, as written."""
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
    notes: list[list[str]] = field(default_factory=list)
    """The instruction under each card the item was seen on, one block per card."""
    occurrence: str | None = None
    field_origins: dict[str, SourceChannel] = field(default_factory=dict)
    """The channel that gave each dated field, which the portal's cards do; the draft's own
    ``origin`` is the channel that first named the assignment."""

    def add(self, record: SourceRecord) -> None:
        """Keep a claim once per text: the same channel, value, and place is one claim."""
        if all(_same_claim(record, kept) is False for kept in self.claims):
            self.claims.append(record)

    def report(self, report: StatusReport) -> None:
        """Keep a report once per text: the same channel, status, and day is one report."""
        if all(_same_report(report, kept) is False for kept in self.reports):
            self.reports.append(report)

    def reading(self) -> Reading:
        blocks: list[str] = []
        for lines in self.notes:
            block = "\n".join(lines)
            if block and block not in blocks:
                blocks.append(block)
        origins = dict(self.field_origins)
        if blocks:
            # An instruction is read under a card, and cards are the portal's.
            origins["note"] = SourceChannel.LMS
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
            occurrence=self.occurrence,
            field_origins=origins,
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


def read_text(text: str, *, now: datetime, today: date) -> Read:
    """Read pasted text in the portal's shapes or the email's, in one pass over its lines.

    A plain line just before a card's line is the card's course, on the
    homework page; the summary names the course on the card's own line.
    Plain lines just after a card are the teacher's instruction, until the
    next card, day, or course line, or a blank; the same instruction under
    both of an item's days is kept once. A line shaped like a day or a card
    that does not read as one is text that needs review, and it ends the
    card before it, so nothing after it is taken for that card's words. A
    mail program's date line outside a card dates the school's reports read
    after it, and the other lines of a mail header are read as nothing. Any
    other plain line outside a card needs review too. A card that names an
    assignment again a week or more from the date it was first read with is
    another round of the same name and is read apart, so the parent can say
    what it is; a card repeated within a week is the same assignment, and a
    card repeated word for word is read once.
    """
    lines = [_unbroken(line) for line in text.splitlines()]
    following = _next_lines(lines)
    reported_on, dated_by = today, PASTE_DAY
    drafts: dict[tuple[str, str], list[_Draft]] = {}
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
        if last is None and (sent := mail_date(line)) is not None:
            # The email's date line, outside a card, dates the reports read
            # after it; under a card the same words are the teacher's.
            reported_on, dated_by = sent, EMAIL_DATE_LINE
            continue
        if last is None and MAIL_HEADER.match(line):
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
            draft.notes.append([])
            last = draft
            continue
        if due := DUE_LINE.match(body):
            title = due.group("title").strip()
            if card_course is None or day is None or not title:
                unread.append(Unread(number, line))
                last = None
                continue
            draft = _draft_for(drafts, card_course, title, SourceChannel.LMS, number, day)
            draft.add(claim(SourceChannel.LMS, day, DAY_HEADER, now, PORTAL_CONFIDENCE))
            if draft.due_date is None:
                draft.due_date = day
                draft.field_origins["due_date"] = SourceChannel.LMS
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
    items = tuple(draft.reading() for rounds in drafts.values() for draft in rounds)
    return Read(items=items, unread=tuple(unread))


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
    """The draft a card adds to: the round of its name whose date is within a week of the
    card's, else a new round; a card with no date of its own joins the first round."""
    key = pair(course, title)
    rounds = drafts.setdefault(key, [])
    for draft in rounds:
        if (
            when is None
            or draft.due_date is None
            or abs(draft.due_date - when) < ANOTHER_OCCURRENCE
        ):
            return draft
    draft = _Draft(
        course=key[0],
        title=key[1],
        origin=origin,
        at_line=number,
        occurrence=None if not rounds or when is None else when.isoformat(),
    )
    rounds.append(draft)
    return draft


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
    moves_due_date: bool
    """Whether the parent said the saved assignment moved, so the pasted date becomes its
    due date."""
    fills_assigned_on: bool
    note_change: str | None
    """``fill`` when the record has no note and the reading has one; ``update`` when the
    school's text replaces the school's earlier text; ``kept`` when the saved note
    stands and the pasted one differs; ``None`` when there is nothing to say."""
    kind: AssignmentKind
    """The kind the row will have: the reader's suggestion, or the parent's choice."""
    kind_by_parent: bool
    ambiguous: bool
    """Whether the pasted work may be another round under a name, unanswered."""
    occurrence: str | None
    """The parent's answer, ``update`` or ``new``, when one was given."""
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
    """The key of the card this one was folded into, as the parent said; such a card is
    not shown, saves nothing of its own, and its answers travel with the page."""

    @property
    def suggested(self) -> AssignmentKind:
        """The kind the page shows for the card before any choice."""
        return self.kind if self.suggested_kind is None else self.suggested_kind

    @property
    def standing(self) -> Assignment | None:
        """The row this card's changes are measured against: after the cards before it."""
        return self.base if self.base is not None else self.on_record

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
        return tuple(parts)

    @property
    def state(self) -> str:
        """``review`` for a question the parent must answer, ``folded`` for a card folded
        into another, else ``new``, ``claimed``, or ``known``."""
        if self.folded_into is not None:
            return FOLDED
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
        if self.state == REVIEW:
            where = (
                "The saved assignment is due"
                if self.on_record is not None
                else "This text also has it due"
            )
            when = "" if self.beside is None else f" {spoken_day(self.beside)}"
            return (
                f"{where}{when}. Say whether this is the same assignment with its due date "
                "changed, or new work under the same name."
            )
        if self.state == FOLDED:
            return "Folded into the card for the same assignment, as you said."
        if self.state == NEW:
            return "Saved as a new assignment."
        if self.state == KNOWN:
            if self.note_change == "kept":
                return f"Nothing changes. {NOTE_STANDS}"
            return "Nothing changes."
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
        elif (
            self.note_change == "update"
            and self.reading.origin_of("note") == SourceChannel.PARENT_ENTRY
        ):
            parts.append("Your note replaces the saved note.")
        elif self.note_change == "update":
            parts.append("The school's note replaces the school's earlier note.")
        elif self.note_change == "kept":
            parts.append(NOTE_STANDS)
        return " ".join(parts)


class _Seen:
    """What is already accounted for while one text is compared: the claims and reports on
    record for each row, and those earlier readings of the same text add to it."""

    def __init__(self, store: ProjectStateStore) -> None:
        self._store = store
        self._claims: dict[str, list[SourceRecord]] = {}
        self._reports: dict[str, list[StatusReport]] = {}

    def novel_claims(
        self, assignment_id: str, saved: bool, claims: tuple[SourceRecord, ...]
    ) -> tuple[SourceRecord, ...]:
        """The claims not yet accounted for under a row, now accounted for."""
        if assignment_id not in self._claims:
            self._claims[assignment_id] = (
                list(self._store.deadline_records(assignment_id)) if saved else []
            )
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
        """The reports not yet accounted for under a row, now accounted for."""
        if assignment_id not in self._reports:
            self._reports[assignment_id] = (
                list(self._store.status_reports(assignment_id)) if saved else []
            )
        kept = self._reports[assignment_id]
        fresh = []
        for report in reports:
            if all(_same_report(report, other) is False for other in kept):
                kept.append(report)
                fresh.append(report)
        return tuple(fresh)


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
    when the record has none. The note is filled in when the record has
    none; a parent's note replaces any saved note, the school's note
    replaces the school's earlier note, and the school's never replaces a
    parent's. ``kinds`` are the parent's choices of type on the page; a
    type typed with an entry is the parent's choice as well, and one left
    unchosen there is no choice. Several cards
    about one saved row compose: each is measured against the row as the
    cards before it leave it, so a date one moves, a note another adds, and
    an assigned date a third fills all arrive together.
    """
    occurrences = occurrences or {}
    kinds = kinds or {}
    on_record: dict[tuple[str, str], list[Assignment]] = {}
    by_id: dict[str, Assignment] = {}
    for item in store.all_assignments():
        on_record.setdefault(pair(item.course, item.title), []).append(item)
        by_id[item.assignment_id] = item
    seen = _Seen(store)
    anchors: dict[tuple[str, str], int] = {}
    """The place in ``changes`` of the first new reading under each name in this text."""
    pending: dict[str, Assignment] = {}
    """Each saved row as the cards read so far leave it."""
    changes: list[Change] = []
    for key, reading in enumerate(items):
        answer = occurrences.get(key)
        existing = _match(on_record.get(reading.pair, []), reading)
        twin = None if existing is not None else anchors.get(reading.pair)
        other = None
        if existing is not None:
            other = pending.get(existing.assignment_id, existing).due_date
        if twin is not None:
            other = changes[twin].reading.due_date
        asked = (
            reading.due_date is not None
            and other is not None
            and abs(reading.due_date - other) >= ANOTHER_OCCURRENCE
        )
        if not asked:
            answer = None
        if twin is not None and asked and answer == UPDATE:
            changes[twin] = _folded(changes[twin], key, reading, kinds, seen)
            changes.append(_folded_marker(key, reading, changes[twin].key, kinds))
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
                changes.append(change)
                continue
        base = pending.get(existing.assignment_id, existing)
        change = _change_to(key, reading, existing, base, answer, asked, kinds, seen)
        changes.append(change)
        if change.state == CLAIMED:
            row = _updated_row(change)
            if row is not None:
                pending[existing.assignment_id] = row
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
) -> Change:
    """A reading with no row to land on, or one the parent said is new work, or one that
    waits on the parent's answer about ``beside``, the other date under its name."""
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
) -> Change:
    """The first reading of a name in this text with a later card folded in, as the parent
    said: the due date is the later card's, and its claims, note, and reports come along,
    as does a type chosen on it. The card the parent sees for the assignment is the one
    that decides its type: a choice made on it stands over any carried by a folded card,
    and two folded cards choosing differently, with none on the card shown, is a conflict."""
    kind, chosen, _ = _kind_for(key, reading, None, kinds)
    if anchor.key in kinds:
        chosen = False
    elif chosen and anchor.kind_by_parent and anchor.kind != kind:
        return dataclasses.replace(anchor, choices_conflict=True)
    first = anchor.reading
    notes = [note for note in (first.note, reading.note) if note]
    origins = {**reading.field_origins, **first.field_origins}
    origins["due_date"] = reading.origin_of("due_date")
    if first.assigned_on is None and reading.assigned_on is not None:
        origins["assigned_on"] = reading.origin_of("assigned_on")
    merged = dataclasses.replace(
        first,
        due_date=reading.due_date,
        assigned_on=first.assigned_on or reading.assigned_on,
        claims=first.claims + reading.claims,
        note="\n".join(dict.fromkeys(notes)) or None,
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
    key: int, reading: Reading, into: int, kinds: Mapping[int, AssignmentKind]
) -> Change:
    """The card that was folded, kept in the list so the page can carry its answers, the
    parent's word that it is the same assignment and any type chosen on it, through a
    page that comes back with another question still open."""
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
        UPDATE,
        suggested_kind=suggested,
        folded_into=into,
    )


def conflicting_choices(changes: list[Change]) -> list[str]:
    """The assignments whose cards choose different types, named as the page names them.

    Two cards about one assignment can each carry a choice; when the
    choices differ, no saving picks one, and the page asks instead.
    """
    chosen: dict[str, set[AssignmentKind]] = {}
    names: dict[str, str] = {}
    for change in changes:
        if change.state == FOLDED:
            continue
        if change.choices_conflict:
            chosen.setdefault(change.assignment_id, set()).update(AssignmentKind)
        elif change.kind_by_parent:
            chosen.setdefault(change.assignment_id, set()).add(change.kind)
        names.setdefault(change.assignment_id, f"{change.reading.course}: {change.reading.title}")
    return [names[key] for key, kinds in chosen.items() if len(kinds) > 1]


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
    if reading.origin_of("note") == SourceChannel.PARENT_ENTRY:
        return "update"
    if existing.origins.get("note", SourceChannel.LMS) == SourceChannel.LMS:
        return "update"
    return "kept"


@dataclass(frozen=True)
class Kept:
    """What one saving did, counted in assignments: rows added, rows already saved that
    changed, and rows unchanged. Two cards about one row are one row here."""

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
    wrote and adds nothing, an answer of new work included, since the row
    that answer makes is found by its id. When the record has changed since
    the preview in a way that leaves a question open, or when two cards
    about one assignment choose different types, nothing is written and the
    changes are handed back for the parent to look at again.
    """
    with store.exclusively():
        changes = changes_for(items, store, occurrences=occurrences, kinds=kinds)
        if any(change.state == REVIEW for change in changes) or conflicting_choices(changes):
            return changes
        rows: dict[str, Assignment] = {}
        claims: dict[str, list[SourceRecord]] = {}
        reports: dict[str, list[StatusReport]] = {}
        added: set[str] = set()
        updated: set[str] = set()
        unchanged: set[str] = set()
        for change in changes:
            if change.state == FOLDED:
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
        store.put_on_record(rows.values(), claims, reports)
    return Kept(
        added=len(added),
        updated=len(updated - added),
        unchanged=len(unchanged - added - updated),
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


def _by_due(change: Change) -> tuple[date, str, str]:
    return (change.reading.due_date or date.max, change.reading.course, change.reading.title)
