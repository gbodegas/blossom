"""Expectation before action: the record's due date is stated before the school is read.

An observation on its own is data. Set against an expectation stated before
the look, it becomes confirmation or contradiction, and contradiction is the
signal this system exists to notice: the family's record and the school say
different things, and until now nobody has been told.

The comparison is typed and deterministic. A date is compared with a date,
never one string with another, so "Friday" set against 2026-08-21 is not a
contradiction; it is a value the comparator cannot read, and the verdict says
so. There are three verdicts rather than two because "cannot tell" is not
"these disagree". Reading the undecidable as a contradiction would bury the
one signal that most needs to stay clean under noise about formats and
missing sources.

The expectation is a value of its own, built from the record alone, and the
comparison takes it as an argument. So the order the design asks for, state
the belief and then look, is the order the code has to be called in.

No model takes part. The rules fit in one function, and
``tests/noticing_cases.py`` holds them to a labeled table.
"""

import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from blossom.assignment_status import AssignmentStatus, statuses_for
from blossom.reconciliation import (
    Reconciler,
    ReconciliationResult,
    SourceChannel,
    SourceRecord,
)
from blossom.sources import DateClaims
from blossom.stores.project_state import DUE_THIS_WEEK_SPAN, Assignment, ProjectStateStore


class Verdict(StrEnum):
    """What comparing an expectation with what was observed can conclude."""

    CONFIRMED = "CONFIRMED"
    """Every source with a readable date gives the record's date."""

    CONTRADICTED = "CONTRADICTED"
    """At least one source has a readable date, and none of them gives the record's."""

    UNDECIDABLE = "UNDECIDABLE"
    """Nothing to compare against, or sources that give the record's date beside another."""


@dataclass(frozen=True, kw_only=True)
class DueDateExpectation:
    """What the record says an assignment is due, stated before any source is read.

    Built from the assignment alone; nothing observed reaches it. ``None`` is
    a belief too: the record has no date, and a source that gives one
    contradicts it.
    """

    assignment_id: str
    due_date: date | None


def expect_due_date(assignment: Assignment) -> DueDateExpectation:
    """State the record's due date for one assignment."""
    return DueDateExpectation(assignment_id=assignment.assignment_id, due_date=assignment.due_date)


class Noticing(BaseModel):
    """One expectation, what was observed against it, and the verdict.

    Carried in the graph's saved state and shown to the planner, the critic,
    and the person at the gate, so a contradiction changes what is planned
    rather than being logged and forgotten.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_id: str
    expected: date | None
    """The record's due date, as stated before the sources were read."""
    observed: tuple[str, ...]
    """Each source's claim as ``SourceRecord.describe`` renders it, in the order observed."""
    spoken: tuple[str, ...] = ()
    """The same claims as ``SourceRecord.spoken`` renders them, with channels named
    as she reads them, for the draft."""
    observed_dates: tuple[date, ...]
    """The distinct dates the sources gave that could be read as dates, earliest first."""
    verdict: Verdict

    @property
    def contradicted(self) -> bool:
        """Whether the sources leave the record unsupported; the verdict the graph acts on."""
        return self.verdict is Verdict.CONTRADICTED

    @property
    def earliest_date(self) -> date | None:
        """The earliest date anyone gives, record or source. The deadline a plan must meet."""
        candidates = list(self.observed_dates)
        if self.expected is not None:
            candidates.append(self.expected)
        return min(candidates) if candidates else None

    def sources_say(self) -> str:
        """The sources' claims in one line, for a brief or a draft."""
        return "; ".join(self.observed)


def read_date(value: str) -> date | None:
    """An ISO date, or ``None`` for anything the comparator will not guess at."""
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def reconcile_dates(records: Sequence[SourceRecord]) -> ReconciliationResult:
    """Reconcile the claims that read as dates, compared by the date each names.

    Two spellings of one date agree, so a stray space around a date is not a
    disagreement, and every claim is kept as the source spelled it. A value
    that cannot be read as a date takes no part: it is neither a second
    source nor a conflicting one, and a caller that shows claims lists it
    apart. With no readable claim at all the outcome is ``NoSourceRecords``,
    whatever else was said.
    """
    readable = [record for record in records if read_date(record.asserted_value) is not None]
    return Reconciler().reconcile(readable, key=read_date)


def notice_due_date(expectation: DueDateExpectation, records: list[SourceRecord]) -> Noticing:
    """Compare what the record said with what the sources say, and name the verdict.

    Only values that read as dates take part in the verdict. A source whose
    value cannot be read is kept in ``observed`` so a person can see it, but
    it neither confirms nor contradicts anything.
    """
    readable = sorted(
        {parsed for record in records if (parsed := read_date(record.asserted_value))}
    )
    if not readable:
        verdict = Verdict.UNDECIDABLE
    elif expectation.due_date is None:
        verdict = Verdict.CONTRADICTED
    elif readable == [expectation.due_date]:
        verdict = Verdict.CONFIRMED
    elif expectation.due_date in readable:
        verdict = Verdict.UNDECIDABLE
    else:
        verdict = Verdict.CONTRADICTED
    return Noticing(
        assignment_id=expectation.assignment_id,
        expected=expectation.due_date,
        observed=tuple(record.describe() for record in records),
        spoken=tuple(record.spoken() for record in records),
        observed_dates=tuple(readable),
        verdict=verdict,
    )


def in_week(assignment: Assignment, noticing: Noticing, start: date) -> bool:
    """Whether the record or any source puts the assignment in the week from ``start``.

    Undated work is always in the week. Dated work is in it when any date
    anyone gives falls inside the window, so an item the record puts next
    month and a source puts this week is planned for, and so is one the record
    puts this week and a source says was due already.
    """
    if assignment.due_date is None:
        return True
    end = start + DUE_THIS_WEEK_SPAN
    return any(start <= given <= end for given in (assignment.due_date, *noticing.observed_dates))


@dataclass(frozen=True, kw_only=True)
class Week:
    """The week as both readers see it, with what the sources said about each item."""

    assignments: list[Assignment]
    records: dict[str, list[SourceRecord]]
    """Every source's claims about each assignment in the week, by id."""
    noticings: dict[str, Noticing]
    """The record set against those claims, by id."""
    statuses: dict[str, AssignmentStatus] = field(default_factory=dict)
    """What stands about each assignment's work, hers and the school's, by id, read with
    the rows so a card and her update agree. An id with no entry is unreported."""

    def needs_homework(self, assignment_id: str) -> bool:
        """Whether an assignment is still work to plan: everything but a "done" of hers."""
        status = self.statuses.get(assignment_id)
        return status is None or status.needs_homework

    def active(self) -> list[Assignment]:
        """The week's work still to plan, in the week's order."""
        return [item for item in self.assignments if self.needs_homework(item.assignment_id)]

    def done_ids(self) -> list[str]:
        """The week's assignments she has reported done, in the week's order."""
        return [
            item.assignment_id
            for item in self.assignments
            if not self.needs_homework(item.assignment_id)
        ]


def monday_of(day: date) -> date:
    """The Monday that starts the school week ``day`` falls in."""
    return day - timedelta(days=day.weekday())


PLANNING_DIGEST: Final = uuid.UUID("3b9c1f52-8a47-4d06-b1e3-5c7a9d2f4e68")
"""The namespace a week's fingerprint is drawn from. A namespace of its own for each
shape the fingerprint has had, so a draft fingerprinted under an earlier one reads as
stale rather than as unchanged."""


def canonical_active_input(week: Week) -> list[dict[str, object]]:
    """What a plan is made from, as plain values in a fixed order: the one account of it
    that the fingerprint is drawn from.

    For each assignment she has not reported done, by id: its course, title,
    due and assigned dates, kind, note and whether a parent wrote it, which
    the planner is told, and reported status; whether she has said "not yet"
    and what she wrote with it; and each claim about its date, channel,
    value, and where it was read, sorted. Left out: when a claim or a report
    was made, which report it was, how sure a claim was, her history, and
    anything about work reported done, which is out of what a plan is built
    on.
    """
    rows: list[dict[str, object]] = []
    for item in sorted(week.active(), key=lambda item: item.assignment_id):
        said = week.statuses.get(item.assignment_id)
        rows.append(
            {
                "id": item.assignment_id,
                "course": item.course,
                "title": item.title,
                "due": None if item.due_date is None else item.due_date.isoformat(),
                "assigned": None if item.assigned_on is None else item.assigned_on.isoformat(),
                "kind": item.kind.value,
                "note": item.note,
                "note_by_a_parent": bool(item.note)
                and item.origins.get("note") == SourceChannel.PARENT_ENTRY,
                "reported_status": item.reported_submission_status,
                "student_status": None if said is None else said.status,
                "student_note": None if said is None else said.note,
                "claims": sorted(
                    [record.channel.value, record.asserted_value, record.seen_in or ""]
                    for record in week.records.get(item.assignment_id, [])
                ),
            }
        )
    return rows


def planning_digest(week: Week) -> str:
    """A fingerprint of what a plan is made from, so the same week reads the same and any
    change reads differently.

    Drawn from ``canonical_active_input`` written as JSON with sorted keys:
    every value sits in a field of its own, so words in a note can never
    read as another field or another claim, whatever they hold. An undo that
    restores the week's input restores its fingerprint, and a report that
    changes nothing a plan reads changes nothing here.
    """
    serialized = json.dumps(
        canonical_active_input(week), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return uuid.uuid5(PLANNING_DIGEST, serialized).hex


@dataclass(frozen=True, kw_only=True)
class Everything:
    """The whole record at one reading: every assignment, every source's claims about each,
    and what she, the school, and the family have said about each.

    A page says several things about the record: a week, the planning
    window, what a saved plan includes that she now reports done, the marks
    beside its rows, and whether it still fits the work as it stands. Each
    is drawn from one of these, so they cannot disagree, and her reports
    are read once however many things the page says.
    """

    assignments: list[Assignment]
    """Every assignment on record, in the record's order."""
    records: dict[str, list[SourceRecord]]
    """Every source's claims about each of them, by id."""
    statuses: dict[str, AssignmentStatus]
    """What stands about each of them, by id, and about any other id the reader named as
    well: a saved plan can speak about work that has left the record."""

    @property
    def ids(self) -> frozenset[str]:
        """The id of every assignment on record at this reading."""
        return frozenset(item.assignment_id for item in self.assignments)


def read_everything(
    project_state: ProjectStateStore, source: DateClaims, *, also: Iterable[str] = ()
) -> Everything:
    """Read the whole record in one read transaction while the store is held: one snapshot.

    The rows, the claims, and what she, the school, and the family have
    reported are read together, so a saving landing between two reads
    cannot give a week whose rows and claims disagree, nor a plan's
    fingerprint that misses a change just made, nor a card whose update is
    another card's. That holds for a saving through this store and for one
    through another connection to the same file. ``also`` names assignments
    the reader speaks about whether or not they are on record, a saved
    plan's, so what stands about them comes from the same batch.

    The cost is at most five reads however much the record holds, the claims
    among them asked for once and not once per assignment. It is fewer for a
    record that holds nothing, since a reader asked about no assignments runs
    no statement. What comes back is plain lists and mappings with nothing
    left open, so the transaction is over before anything is rendered or a
    model is asked.
    """
    with project_state.reading():
        everything = project_state.all_assignments()
        on_record = [item.assignment_id for item in everything]
        claimed = source.deadline_records_by_assignment(on_record)
        records = {name: list(claimed.get(name, [])) for name in on_record}
        statuses = statuses_for(project_state, [*on_record, *also])
    return Everything(assignments=everything, records=records, statuses=statuses)


def week_from(everything: Everything, start: date) -> Week:
    """The week from ``start`` out of one reading: state each record's date, set the
    sources against it, then select. Every assignment on record is considered, because
    the sources decide the window along with the record."""
    noticed = {
        item.assignment_id: notice_due_date(
            expect_due_date(item), everything.records[item.assignment_id]
        )
        for item in everything.assignments
    }
    assignments = [
        item for item in everything.assignments if in_week(item, noticed[item.assignment_id], start)
    ]
    return Week(
        assignments=assignments,
        records={
            item.assignment_id: everything.records[item.assignment_id] for item in assignments
        },
        noticings={item.assignment_id: noticed[item.assignment_id] for item in assignments},
        statuses={
            item.assignment_id: everything.statuses[item.assignment_id] for item in assignments
        },
    )


def read_week(project_state: ProjectStateStore, source: DateClaims, start: date) -> Week:
    """Read the week from ``start``: one reading of the record, and the week out of it.

    The student's page and the plan graph both read a week this way, so they
    never differ about whether an item is in a week. They start it on
    different days: her page starts on the Monday of the school week she is
    looking at, the planner on the evening being planned, so a plan looks at
    the seven days ahead and her page says so. A reader that needs more than
    one week, or a week and a saved plan's updates, reads everything once
    and takes each from it.
    """
    return week_from(read_everything(project_state, source), start)
