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

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from blossom.reconciliation import SourceRecord
from blossom.stores.project_state import DUE_THIS_WEEK_SPAN, Assignment


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
