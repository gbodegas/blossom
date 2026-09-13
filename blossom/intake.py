"""What a parent pastes or types, read into what the record can keep.

The school's portal writes its homework page and its weekly summary in two
small sets of line shapes, and its "Missing" email in a third. A parent
pastes any of them as text, and this module reads the shapes into
assignments and into each channel's claim about a due date, with no
credential, no scraper, and no network. Nothing is written here: what was
read is shown to the parent first, and put on record only when they say so.
A line the reader does not understand is kept and shown as such, since a
line dropped in silence is an assignment lost.

The shapes, as the portal writes them:

- The homework page: a day header, ``Tuesday 9/1/2026``, then cards, each a
  course on one line and on the next either ``Assigned: <Title>:
  (Due:MM/DD/YYYY)`` on the day the work was given or ``Due: <Title>:`` on
  the day it is due, where the header is the date.
- The weekly summary: a heading ``Homework for <first name>``, then a day
  line ``* MM/DD/YYYY - Tuesday``, then the same cards with the course and
  the card on one line, ``<Course> - Assigned: <Title>: (Due:MM/DD/YYYY)``
  or ``<Course> - Due: <Title>:``. A teacher's instruction may follow either
  kind of card, on lines of its own.
- The email: ``MM/DD <Course> - <Section>: <Category>: <Title> Grade:
  Missing``, one line per assignment, under ``Assignments:``. A line in that
  shape with any other grade is not the "Missing" email and is left unread.

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

from blossom.reconciliation import SourceChannel, SourceRecord
from blossom.stores.project_state import Assignment, AssignmentKind, ProjectStateStore

PORTAL_CONFIDENCE: Final = 0.9
"""The portal's own page, pasted whole: the school's word, as it wrote it."""
EMAIL_CONFIDENCE: Final = 0.9
"""The school's email, pasted whole: the same word by another route."""
FAMILY_CONFIDENCE: Final = 0.8
"""A parent's own entry: sure of the item, less sure of the date they typed."""

OWN_LINE: Final = "the assignment's own line"
DAY_HEADER: Final = "the day's header"
SCHOOL_EMAIL: Final = "the school's email"

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
"""The summary's heading, which carries her first name: read as nothing."""
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
"""The one grade the school's email is read for."""
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
    """A teacher's instruction under the card, shown to the parent and not kept."""

    @property
    def pair(self) -> tuple[str, str]:
        """What the reading is matched by, in the paste and against the record."""
        return pair(self.course, self.title)

    @property
    def assignment_id(self) -> str:
        """The id a new row would have; a row already on record keeps its own."""
        return identity(self.course, self.title)

    def assignment(self) -> Assignment:
        """The record's row for a reading that is new to it."""
        return Assignment(
            assignment_id=self.assignment_id,
            course=self.course,
            title=self.title,
            due_date=self.due_date,
            dependencies=[],
            reported_submission_status="unknown",
            assigned_on=self.assigned_on,
            kind=self.kind,
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


@dataclass
class _Draft:
    """A reading being assembled while the lines are walked."""

    course: str
    title: str
    due_date: date | None = None
    assigned_on: date | None = None
    claims: list[SourceRecord] = field(default_factory=list)
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
    drafts: dict[tuple[str, str], _Draft] = {}
    unread: list[str] = []
    day: date | None = None
    course: str | None = None
    last: _Draft | None = None
    for index, line in enumerate(lines):
        if not line:
            last = None
            continue
        if line.lower() in NOISE or WEEK_LINE.match(line) or HEADING_LINE.match(line):
            last = None
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
        if _is_course_line(line, lines[index + 1 :]):
            course = line
            last = None
            continue
        if last is not None:
            if line not in last.note_lines:
                last.note_lines.append(line)
            continue
        unread.append(line)
    return Read(items=tuple(draft.reading() for draft in drafts.values()), unread=tuple(unread))


def _draft_for(drafts: dict[tuple[str, str], _Draft], course: str, title: str) -> _Draft:
    key = pair(course, title)
    if key not in drafts:
        drafts[key] = _Draft(course=key[0], title=key[1])
    return drafts[key]


def _is_course_line(line: str, following: list[str]) -> bool:
    """A short plain line whose next line is a card's line names the card's course."""
    if len(line) > COURSE_LENGTH or line.endswith(":"):
        return False
    for candidate in following:
        if not candidate:
            continue
        return bool(ASSIGNED_LINE.match(candidate) or DUE_LINE.match(candidate))
    return False


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
    fills_assigned_on: bool

    @property
    def assignment_id(self) -> str:
        """The row the reading lands in: the record's own when it has one, else a new one."""
        if self.on_record is not None:
            return self.on_record.assignment_id
        return self.reading.assignment_id

    @property
    def state(self) -> str:
        """``new`` to the record, ``known`` with nothing to add, or ``claimed`` with something."""
        if self.on_record is None:
            return NEW
        if self.new_claims or self.fills_assigned_on:
            return CLAIMED
        return KNOWN

    @property
    def label(self) -> str:
        """The state as the preview says it."""
        if self.state == NEW:
            return "New"
        if self.state == KNOWN:
            return "Already on record, nothing new"
        if self.new_claims and self.fills_assigned_on:
            return "On record; a new date claim and the assigned date"
        if self.new_claims:
            return "On record; a new date claim"
        return "On record; the assigned date is new"


def changes_for(items: tuple[Reading, ...], store: ProjectStateStore) -> list[Change]:
    """Compare each reading with the record: what is new, what is known, what a paste adds.

    A reading matches a row by its course and title, whatever the row's id,
    so an assignment on record from a fixture or an earlier paste takes the
    claims rather than a twin. A claim already on record, the same channel
    saying the same value from the same place, is not made twice. The
    recorded due date is never replaced by a paste; a different date is a
    claim beside it, which the page shows as a disagreement. The assigned
    date is filled in when the record has none, since that is a fact the
    record lacked, not one it holds.
    """
    on_record = {pair(item.course, item.title): item for item in store.all_assignments()}
    changes: list[Change] = []
    for reading in items:
        existing = on_record.get(reading.pair)
        if existing is None:
            changes.append(Change(reading, None, reading.claims, False))
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
        fills = existing.assigned_on is None and reading.assigned_on is not None
        changes.append(Change(reading, existing, fresh, fills))
    return changes


def keep(items: tuple[Reading, ...], store: ProjectStateStore) -> int:
    """Compare and write as one: put on record what the record lacks, and say how many.

    The comparison and the write happen while the store is held for this
    caller alone, so two keepings of the same text, a double press or two
    tabs, cannot both find a reading new: the second finds what the first
    wrote and adds nothing.
    """
    with store.exclusively():
        changes = changes_for(items, store)
        rows: list[Assignment] = []
        claims: dict[str, list[SourceRecord]] = {}
        kept = 0
        for change in changes:
            if change.state == KNOWN:
                continue
            kept += 1
            if change.on_record is None:
                rows.append(change.reading.assignment())
            elif change.fills_assigned_on:
                rows.append(
                    change.on_record.model_copy(update={"assigned_on": change.reading.assigned_on})
                )
            if change.new_claims:
                claims[change.assignment_id] = list(change.new_claims)
        store.put_on_record(rows, claims)
    return kept
