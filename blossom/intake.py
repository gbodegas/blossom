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
  Missing``, one line per assignment, under ``Assignments:``.

One item appears under an assigned day and under a due day, often in two
weeks, and is one assignment matched by course and title: the date in its
own line and the date in the header are two claims from the same channel,
told apart by where each was read. Titles are kept as the portal writes
them, punctuation and all; a course is kept as written too, grade prefix
included, since that is how the portal names it everywhere. Forms to sign
and books to cover are tasks, not homework. The heading's first name is read
as nothing and never kept.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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

TASK_WORDS: Final = (
    "signed",
    "sign ",
    "syllabus",
    "cover",
    "binder",
    "supplies",
    "permission",
    "bring ",
)
"""Words in a title that make it a task rather than a sitting of homework."""

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
COURSE_LENGTH: Final = 60
"""A course line is short; a longer plain line is a teacher's instruction or a stray."""
TEXT_MAX_LENGTH: Final = 40_000
"""How much one paste may hold: a page or a week is a few thousand characters."""


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
    def assignment_id(self) -> str:
        """The same course and title always name the same assignment."""
        return f"assignment-{slug(self.course)}-{slug(self.title)}"

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


def slug(text: str) -> str:
    """Lowercase letters and digits joined by hyphens, and nothing else."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def kind_of(title: str) -> AssignmentKind:
    """A task for the things to sign, cover, bring, or check; homework otherwise."""
    lowered = f"{title.lower()} "
    if any(word in lowered for word in TASK_WORDS):
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
    drafts: dict[str, _Draft] = {}
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
            if when is None:
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


def _draft_for(drafts: dict[str, _Draft], course: str, title: str) -> _Draft:
    key = f"{slug(course)}-{slug(title)}"
    if key not in drafts:
        drafts[key] = _Draft(course=course, title=title)
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
    return Reading(
        course=course.strip(),
        title=title.strip(),
        due_date=due_date,
        assigned_on=assigned_on,
        kind=kind,
        claims=claims,
    )


def within_a_school_year(when: date, today: date) -> bool:
    """A date a paste can mean: within a year either side of today."""
    return abs(when - today) <= timedelta(days=366)


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

    A claim already on record, the same channel saying the same value from
    the same place, is not made twice. The recorded due date is never
    replaced by a paste; a different date is a claim beside it, which the
    page shows as a disagreement. The assigned date is filled in when the
    record has none, since that is a fact the record lacked, not one it
    holds.
    """
    on_record = {item.assignment_id: item for item in store.all_assignments()}
    changes: list[Change] = []
    for reading in items:
        existing = on_record.get(reading.assignment_id)
        if existing is None:
            changes.append(Change(reading, None, reading.claims, False))
            continue
        had = {
            (record.channel, record.asserted_value, record.seen_in)
            for record in store.deadline_records(reading.assignment_id)
        }
        fresh = tuple(
            record
            for record in reading.claims
            if (record.channel, record.asserted_value, record.seen_in) not in had
        )
        fills = existing.assigned_on is None and reading.assigned_on is not None
        changes.append(Change(reading, existing, fresh, fills))
    return changes


def keep(changes: list[Change], store: ProjectStateStore) -> int:
    """Put the changes on record in one write, and say how many readings changed it."""
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
            claims[change.reading.assignment_id] = list(change.new_claims)
    store.put_on_record(rows, claims)
    return kept
