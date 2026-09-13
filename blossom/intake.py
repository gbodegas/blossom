"""What a parent pastes or types, read into what the record can keep.

The school's portal writes its homework page and its weekly summary in two
small sets of line shapes, and its "Missing" email in a third. A parent
pastes any of them as text, and this module reads the shapes into
assignments, into each channel's claim about a due date, into what the
teacher wrote under a card, and into what the school reports about an
assignment's status, with no credential, no scraper, and no network.
Nothing is written here: what was read is shown to the parent first, and
put on record only when they say so. A line the reader does not understand
is kept and shown as such, since a line dropped in silence is an assignment
lost.

The shapes, as the portal writes them:

- The homework page: a day header, ``Tuesday 9/1/2026``, then cards, each a
  course on one line and on the next either ``Assigned: <Title>:
  (Due:MM/DD/YYYY)`` on the day the work was given or ``Due: <Title>:`` on
  the day it is due, where the header is the date.
- The weekly summary: a heading ``Homework for <first name>``, then a day
  line ``* MM/DD/YYYY - Tuesday``, then the same cards with the course and
  the card on one line, ``<Course> - Assigned: <Title>: (Due:MM/DD/YYYY)``
  or ``<Course> - Due: <Title>:``. A teacher's instruction may follow either
  kind of card, on lines of its own, and is kept with the assignment.
- The email: ``MM/DD <Course> - <Section>: <Category>: <Title> Grade:
  Missing``, one line per assignment, under ``Assignments:``. Each such line
  is the school reporting the assignment missing, kept as a report with the
  day: the email's own date when the paste carries its date line, otherwise
  the day it was pasted, and the report says which. A line in that shape
  with any other grade is not the "Missing" email and is left unread.

One item appears under an assigned day and under a due day, often in two
weeks, and is one assignment matched by its course and title, exactly as the
portal writes them: the date in its own line and the date in the header are
two claims from the same channel, told apart by where each was read. The
record is matched the same way, by the pair, so an assignment already on
record under any id takes the claims rather than a twin. A new row's id is
the title made readable plus a hash of the pair, so two titles that read
alike never become one. Titles are kept as the portal writes them,
punctuation and all; a course is kept as written too, grade prefix included,
since that is how the portal names it everywhere. Forms to sign and books to
cover are tasks, not homework. The heading's first name is read as nothing
and never kept.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
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
EMAIL_CONFIDENCE: Final = 0.9
"""The school's email, pasted whole: the same word by another route."""
FAMILY_CONFIDENCE: Final = 0.8
"""A parent's own entry: sure of the item, less sure of the date they typed."""

OWN_LINE: Final = "the assignment's own line"
DAY_HEADER: Final = "the day's header"
SCHOOL_EMAIL: Final = "the school's email"
EMAIL_DATE_LINE: Final = "the email's date line"
PASTE_DAY: Final = "the day it was pasted"

TASK_WORD: Final = re.compile(
    r"\b(?:sign|signed|signature|syllabus|cover|covers|covered|binder|supplies|permission|bring)\b",
    re.IGNORECASE,
)
"""Whole words in a title that make it a task rather than a sitting of homework."""

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
    rf"^\*?\s*(?P<month>\d{{1,2}})/(?P<day>\d{{1,2}})/(?P<year>\d{{4}})\s*-\s*(?:{WEEKDAYS})\s*$"
)
WEEK_LINE: Final = re.compile(r"^Week of\s+\d{1,2}/\d{1,2}/\d{4}\s*$", re.IGNORECASE)
HEADING_LINE: Final = re.compile(r"^Homework for\b", re.IGNORECASE)
"""The summary's heading, which carries her first name: read as nothing, outside a card."""
CARD_LINE: Final = re.compile(r"^(?P<course>.+?)\s+-\s+(?P<card>(?:Assigned|Due):.*)$")
"""The summary's card, course and card on one line, split at the first joining dash."""
ASSIGNED_LINE: Final = re.compile(
    r"^Assigned:\s*(?P<title>.+):\s*\(Due:\s*(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})\)\s*$"
)
DUE_LINE: Final = re.compile(r"^Due:\s*(?P<title>.*?):?\s*$")
EMAIL_LINE: Final = re.compile(
    r"^(?P<month>\d{1,2})/(?P<day>\d{1,2})\s+(?P<course>.+?)(?:\s+-\s+\S+)?:\s+"
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


def identity(course: str, title: str) -> str:
    """A stable id for a new row: the title made readable, and a hash of the exact pair.

    The hash keeps two titles that read alike apart, ``Quiz A/B`` and
    ``Quiz A-B`` among them, and keeps the id the same length however long
    the title runs.
    """
    course, title = pair(course, title)
    tag = uuid.uuid5(IDENTITY, f"{course}\n{title}").hex[:8]
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
    note: str | None = None
    """What the teacher wrote under the card, kept with the assignment."""
    reports: tuple[StatusReport, ...] = ()
    """What the school reports about the assignment's status, with the day."""

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

    def assignment(self) -> Assignment:
        """The record's row for a reading that is new to it."""
        return Assignment(
            assignment_id=self.assignment_id,
            course=self.course,
            title=self.title,
            due_date=self.due_date,
            dependencies=[],
            reported_submission_status=self.reported_status or "unknown",
            assigned_on=self.assigned_on,
            kind=self.kind,
            note=self.note,
        )


@dataclass(frozen=True)
class Read:
    """Everything one paste says: the readings, and the lines that were not read."""

    items: tuple[Reading, ...]
    unread: tuple[str, ...]


def kind_of(title: str) -> AssignmentKind:
    """A task for the things to sign, cover, bring, or check; homework otherwise."""
    if TASK_WORD.search(title):
        return AssignmentKind.TASK
    return AssignmentKind.HOMEWORK


def a_date(year: str, month: str, day: str) -> date | None:
    """The date the digits name, or ``None`` for digits that name no date."""
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def nearest_year(month: int, day: int, today: date) -> date | None:
    """The date nearest ``today`` with this month and day: an email names no year."""
    years = (today.year - 1, today.year, today.year + 1)
    found = [when for year in years if (when := a_date(str(year), str(month), str(day)))]
    if not found:
        return None
    return min(found, key=lambda when: abs(when - today))


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


def spoken_report(report: StatusReport) -> str:
    """The report's source and day as the pages say them, after the status itself."""
    day = report.reported_on
    when = f"{day.strftime('%A, %B')} {day.day}, {day.year}"
    where = CHANNEL_NAMES[report.channel]
    if report.dated_by == EMAIL_DATE_LINE:
        return f"From the {where}, dated {when}."
    return f"From the {where}, pasted {when}."


@dataclass
class _Draft:
    """A reading being assembled while the lines are walked."""

    course: str
    title: str
    due_date: date | None = None
    assigned_on: date | None = None
    claims: list[SourceRecord] = field(default_factory=list)
    reports: list[StatusReport] = field(default_factory=list)
    note_lines: list[str] = field(default_factory=list)

    def reading(self) -> Reading:
        return Reading(
            course=self.course,
            title=self.title,
            due_date=self.due_date,
            assigned_on=self.assigned_on,
            kind=kind_of(self.title),
            claims=tuple(self.claims),
            note=" ".join(self.note_lines) or None,
            reports=tuple(self.reports),
        )


def read_text(text: str, *, now: datetime, today: date) -> Read:
    """Read pasted text in the portal's shapes or the email's, in one pass over its lines.

    A plain line just before a card's line is the card's course, on the
    homework page; the summary names the course on the card's own line.
    Plain lines just after a card are the teacher's instruction, until the
    next course line; an instruction seen twice, under both of an item's
    days, is kept once; any other plain line is unread and said so. The same
    course and title seen twice, under an assigned day and under a due day,
    are one reading with both claims.
    """
    lines = [line.strip() for line in text.splitlines()]
    following = _next_lines(lines)
    reported_on, dated_by = email_date(text), EMAIL_DATE_LINE
    if reported_on is None:
        reported_on, dated_by = today, PASTE_DAY
    drafts: dict[tuple[str, str], _Draft] = {}
    unread: list[str] = []
    day: date | None = None
    course: str | None = None
    last: _Draft | None = None
    for index, line in enumerate(lines):
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
                unread.append(line)
            continue
        if email := EMAIL_LINE.match(line):
            when = nearest_year(int(email.group("month")), int(email.group("day")), today)
            if when is None or email.group("grade").strip().lower() != MISSING:
                unread.append(line)
                continue
            draft = _draft_for(drafts, email.group("course").strip(), email.group("title").strip())
            draft.claims.append(
                claim(SourceChannel.EMAIL, when, SCHOOL_EMAIL, now, EMAIL_CONFIDENCE)
            )
            draft.reports.append(
                StatusReport(
                    status=MISSING,
                    channel=SourceChannel.EMAIL,
                    reported_on=reported_on,
                    dated_by=dated_by,
                    observed_at=now,
                )
            )
            if draft.due_date is None:
                draft.due_date = when
            last = None
            continue
        card_course, body = course, line
        if combined := CARD_LINE.match(line):
            card_course, body = combined.group("course").strip(), combined.group("card")
        if assigned := ASSIGNED_LINE.match(body):
            when = a_date(assigned.group("year"), assigned.group("month"), assigned.group("day"))
            if card_course is None or when is None:
                unread.append(line)
                continue
            draft = _draft_for(drafts, card_course, assigned.group("title").strip())
            draft.claims.append(claim(SourceChannel.LMS, when, OWN_LINE, now, PORTAL_CONFIDENCE))
            if draft.due_date is None:
                draft.due_date = when
            if draft.assigned_on is None:
                draft.assigned_on = day
            last = draft
            continue
        if due := DUE_LINE.match(body):
            title = due.group("title").strip()
            if card_course is None or day is None or not title:
                unread.append(line)
                continue
            draft = _draft_for(drafts, card_course, title)
            draft.claims.append(claim(SourceChannel.LMS, day, DAY_HEADER, now, PORTAL_CONFIDENCE))
            if draft.due_date is None:
                draft.due_date = day
            last = draft
            continue
        if _is_course_line(line, following[index]):
            course = line
            last = None
            continue
        if last is not None:
            if line not in last.note_lines:
                last.note_lines.append(line)
            continue
        unread.append(line)
    return Read(items=tuple(draft.reading() for draft in drafts.values()), unread=tuple(unread))


def _next_lines(lines: list[str]) -> list[str | None]:
    """For each line, the next line that is not blank, found in one pass from the end."""
    following: list[str | None] = [None] * len(lines)
    ahead: str | None = None
    for index in range(len(lines) - 1, -1, -1):
        following[index] = ahead
        if lines[index]:
            ahead = lines[index]
    return following


def _draft_for(drafts: dict[tuple[str, str], _Draft], course: str, title: str) -> _Draft:
    key = pair(course, title)
    if key not in drafts:
        drafts[key] = _Draft(course=key[0], title=key[1])
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
    *,
    now: datetime,
) -> Reading:
    """One assignment as a parent typed it, its date a claim of the family's own."""
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


@dataclass(frozen=True)
class Change:
    """What keeping one reading would do to the record, worked out before anything is written."""

    reading: Reading
    on_record: Assignment | None
    new_claims: tuple[SourceRecord, ...]
    new_reports: tuple[StatusReport, ...]
    fills_assigned_on: bool
    fills_note: bool

    @property
    def assignment_id(self) -> str:
        """The row the reading lands in: the record's own when it has one, else a new one."""
        if self.on_record is not None:
            return self.on_record.assignment_id
        return self.reading.assignment_id

    @property
    def additions(self) -> tuple[str, ...]:
        """What a reading adds to a row already on record, in the preview's words."""
        parts = []
        if self.new_claims:
            parts.append("a new date claim")
        if self.new_reports:
            parts.append("what the school reports")
        if self.fills_assigned_on:
            parts.append("the assigned date")
        if self.fills_note:
            parts.append("the teacher's note")
        return tuple(parts)

    @property
    def state(self) -> str:
        """``new`` to the record, ``known`` with nothing to add, or ``claimed`` with something."""
        if self.on_record is None:
            return NEW
        if self.additions:
            return CLAIMED
        return KNOWN

    @property
    def label(self) -> str:
        """The state as the preview says it."""
        if self.state == NEW:
            return "New"
        if self.state == KNOWN:
            return "Already on record, nothing new"
        parts = self.additions
        joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        return f"On record; {joined}"


def changes_for(items: tuple[Reading, ...], store: ProjectStateStore) -> list[Change]:
    """Compare each reading with the record: what is new, what is known, what a paste adds.

    A reading matches a row by its course and title, whatever the row's id,
    so an assignment on record from a fixture or an earlier paste takes the
    claims rather than a twin. A claim already on record, the same channel
    saying the same value from the same place, is not made twice, and
    neither is a report of the same status on the same day. The recorded
    due date is never replaced by a paste; a different date is a claim
    beside it, which the page shows as a disagreement. The assigned date and
    the teacher's note are filled in when the record has none, since those
    are facts the record lacked, not ones it holds.
    """
    on_record = {pair(item.course, item.title): item for item in store.all_assignments()}
    changes: list[Change] = []
    for reading in items:
        existing = on_record.get(reading.pair)
        if existing is None:
            changes.append(Change(reading, None, reading.claims, reading.reports, False, False))
            continue
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
        fills = existing.assigned_on is None and reading.assigned_on is not None
        fills_note = existing.note is None and reading.note is not None
        changes.append(Change(reading, existing, fresh, news, fills, fills_note))
    return changes


@dataclass(frozen=True)
class Kept:
    """What one keeping did: rows put on record, and rows already there that changed."""

    new: int
    changed: int


def keep(items: tuple[Reading, ...], store: ProjectStateStore) -> Kept:
    """Compare and write as one: put on record what the record lacks, and say what changed.

    The comparison and the write happen while the store is held for this
    caller alone, so two keepings of the same text, a double press or two
    tabs, cannot both find a reading new: the second finds what the first
    wrote and adds nothing.
    """
    with store.exclusively():
        changes = changes_for(items, store)
        rows: list[Assignment] = []
        claims: dict[str, list[SourceRecord]] = {}
        reports: dict[str, list[StatusReport]] = {}
        new = changed = 0
        for change in changes:
            if change.state == KNOWN:
                continue
            if change.on_record is None:
                new += 1
                rows.append(change.reading.assignment())
            else:
                changed += 1
                filled: dict[str, object] = {}
                if change.fills_assigned_on:
                    filled["assigned_on"] = change.reading.assigned_on
                if change.fills_note:
                    filled["note"] = change.reading.note
                if change.new_reports:
                    filled["reported_submission_status"] = change.new_reports[-1].status
                if filled:
                    rows.append(change.on_record.model_copy(update=filled))
            if change.new_claims:
                claims[change.assignment_id] = list(change.new_claims)
            if change.new_reports:
                reports[change.assignment_id] = list(change.new_reports)
        store.put_on_record(rows, claims, reports)
    return Kept(new=new, changed=changed)
