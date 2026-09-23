"""Homework on record that a note's class and title name, as a person is shown it to choose.

Adding a note to homework asks a question when homework of the same class and
title is already on record: is this the same homework, or a separate
assignment. The answer is only as good as what the person was shown, so one
reading serves both ends. A page shows each candidate from it: what the record
holds, where the record came from, what she currently says about the work, and
what each school channel currently says. The fingerprint a page sends back is
made from the same values, and the save reads the same reading again inside its
own transaction, so a choice about homework whose shown facts have since
changed is put to the person again. There is one list of what is shown and
compared, here, and no second one to drift from it.

Her account and the school's are read apart, as everywhere: no report of hers
is said as that, and what the record's own status column holds is never offered
in its place. Moments of reading, how a day was dated, and the family's checks
are shown by no row and are no part of the fingerprint, so a statement repeated
with nothing visible changed moves nothing. The reads are batched: one for the
assignments unless they are given, and three for what stands about the
candidates however many there are, and none when there are none.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from blossom.assignment_status import AssignmentStatus, statuses_for
from blossom.captures import CaptureDetails
from blossom.reconciliation import SourceChannel
from blossom.stores.project_state import Assignment, ProjectStateStore


@dataclass(frozen=True)
class SchoolWord:
    """What one school channel says now about a candidate's status, and the day it said it."""

    channel: SourceChannel
    status: str
    reported_on: date


@dataclass(frozen=True)
class CandidateReading:
    """One assignment on record with the class and title a note names, with everything a
    person is shown about it before choosing."""

    assignment_id: str
    course: str
    title: str
    due_date: date | None
    kind: str
    recorded_status: str
    """The record's own status column, which the school's paste fills; never her account."""
    record_source: SourceChannel | None
    """The channel the record came through, when it says."""
    work_state: str | None
    """What she currently says about the work, ``done`` or ``not_yet``, or ``None`` when no
    report of hers stands: after none, or after an undo that left none."""
    work_reported_on: date | None
    school: tuple[SchoolWord, ...]
    """Each school channel's current word, by channel."""

    def facts(self) -> list[object]:
        """The values a row shows, in a fixed order and spelling, for the fingerprint."""
        return [
            self.assignment_id,
            self.course,
            self.title,
            "" if self.due_date is None else self.due_date.isoformat(),
            self.kind,
            self.recorded_status,
            "" if self.record_source is None else self.record_source.value,
            self.work_state or "",
            "" if self.work_reported_on is None else self.work_reported_on.isoformat(),
            [
                [word.channel.value, word.status, word.reported_on.isoformat()]
                for word in self.school
            ],
        ]


def reading_of(item: Assignment, standing: AssignmentStatus | None) -> CandidateReading:
    """One candidate as it is shown, from its row and what stands about its work."""
    asserted = None if standing is None else standing.asserted
    said = () if standing is None else standing.school.values()
    return CandidateReading(
        assignment_id=item.assignment_id,
        course=item.course,
        title=item.title,
        due_date=item.due_date,
        kind=str(getattr(item.kind, "value", item.kind)),
        recorded_status=item.reported_submission_status,
        record_source=item.origins.get("record"),
        work_state=None if asserted is None else asserted.status,
        work_reported_on=None if asserted is None else asserted.reported_on,
        school=tuple(
            sorted(
                (SchoolWord(word.channel, word.status, word.reported_on) for word in said),
                key=lambda word: word.channel.value,
            )
        ),
    )


def readings_for(store: ProjectStateStore, items: Sequence[Assignment]) -> list[CandidateReading]:
    """These assignments as they are shown, in the order given: the rows a search shows, and
    the one row a link to homework found by search is held to. Called inside a write's
    transaction it reads through that transaction, which is what makes the comparison
    there one with the write."""
    if not items:
        return []
    standing = statuses_for(store, [item.assignment_id for item in items])
    return [reading_of(item, standing.get(item.assignment_id)) for item in items]


def candidate_readings(
    store: ProjectStateStore,
    details: CaptureDetails,
    among: Sequence[Assignment] | None = None,
) -> list[CandidateReading]:
    """Every candidate for these details, as shown, in the record's order."""
    return readings_for(store, store.promotion_candidates(details, among=among))


CandidateReader = Callable[[CaptureDetails, Sequence[Assignment]], Sequence[CandidateReading]]


def reader(store: ProjectStateStore) -> CandidateReader:
    """The reading a save makes inside its own transaction, through the store it writes."""

    def read(details: CaptureDetails, among: Sequence[Assignment]) -> Sequence[CandidateReading]:
        return candidate_readings(store, details, among)

    return read


RowReader = Callable[[Sequence[Assignment]], Sequence[CandidateReading]]


def row_reader(store: ProjectStateStore) -> RowReader:
    """The reading of named rows a link by search makes inside its own transaction."""

    def read(items: Sequence[Assignment]) -> Sequence[CandidateReading]:
        return readings_for(store, items)

    return read
