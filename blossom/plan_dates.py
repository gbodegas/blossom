"""A saved plan's dates beside the record as it stands.

One rule says when an assignment's due date is in doubt: no date on record, sources that
leave the recorded date unsupported, or sources that give different dates. The composer
words a plan's clarifications from it, and a page applies it again to its own reading.
Beside each row of a plan still in force for its evening, a page can then say what stands
now: a recorded date that changed, what the readable sources give, and what cannot be
read. Nothing here reads a store or a plan's saved words.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final

from blossom.noticing import (
    Everything,
    Noticing,
    noticings_of,
    planning_digest,
    read_date,
    reconcile_dates,
    week_from,
)
from blossom.reconciliation import SourceConfidence, SourceRecord, classify_confidence
from blossom.stores.project_state import Assignment

CHANGED_TO: Final = "Since this plan was made, the recorded due date changed to {date}."
NOT_RECORDED_NOW: Final = (
    "When this plan was made, a due date was recorded. There is no due date on record now."
)
RECORDED: Final = "Since this plan was made, a due date was recorded: {date}."
NOT_A_DATE: Final = "Some source information cannot be read as a date. Check the current record."
UNREADABLE: Final = (
    "A claim about this date cannot be read right now, so what the sources say is not fully known."
)
SOURCE_LIMIT: Final = (
    "This saved plan does not keep enough detail to compare every change in its sources."
)
"""Said once in a plan whose rows restate present evidence: a saved plan keeps the dates it
asked about, not the claims behind them, so it cannot say which source changed or when."""


class Doubt(StrEnum):
    """Why an assignment's due date needs a word with someone."""

    UNDATED = "undated"
    UNDATED_CLAIMED = "undated_claimed"
    """No date on record, and a source gives one."""
    CONTRADICTED = "contradicted"
    DISAGREE = "disagree"


def doubt_of(
    due_date: date | None, noticing: Noticing | None, confidence: SourceConfidence | None
) -> Doubt | None:
    """Whether a due date is in doubt, and why; ``None`` when it is not. A date one channel
    gives and nothing disputes is not, nor is a date only the record has, nor one the
    sources spoke about in words that are not dates."""
    if due_date is None:
        if noticing is not None and noticing.contradicted:
            return Doubt.UNDATED_CLAIMED
        return Doubt.UNDATED
    if noticing is not None and noticing.contradicted:
        return Doubt.CONTRADICTED
    if confidence == SourceConfidence.SOURCES_DISAGREE:
        return Doubt.DISAGREE
    return None


class Evidence(StrEnum):
    """What the readable claims say now about an assignment's recorded due date."""

    SUPPORT = "support"
    """Every readable claim gives the recorded date."""
    NONE = "none"
    """A date is recorded and no claim reads as a date."""
    CONTRADICTED = "contradicted"
    """No readable claim gives the recorded date."""
    DISAGREE = "disagree"
    """The readable claims give the recorded date and another."""
    UNKNOWN_NONE = "unknown_none"
    """No date is recorded and no claim reads as a date."""
    UNKNOWN_CLAIMS = "unknown_claims"
    """No date is recorded and a claim gives one."""


EVIDENCE_WORDS: Final = {
    Evidence.SUPPORT: "The readable sources now match the recorded due date.",
    Evidence.NONE: "No readable source currently supports the recorded due date.",
    Evidence.CONTRADICTED: "The sources give {dates}, not the recorded due date.",
    Evidence.DISAGREE: "The sources give different dates: {dates}.",
    Evidence.UNKNOWN_NONE: "No readable source gives a due date.",
    Evidence.UNKNOWN_CLAIMS: "The sources give {dates}, but no due date is on record.",
}
"""Dates and never channels: a channel's name is worded for her, and the details list each."""


def dates_said(dates: Sequence[date]) -> str:
    """Dates as a sentence says them, the year once unless they span years:
    ``August 21 and August 22, 2026``."""
    *before, last = dates
    one_year = all(value.year == last.year for value in before)
    said = [f"{value:%B} {value.day}" + ("" if one_year else f", {value.year}") for value in before]
    ending = f"{last:%B} {last.day}, {last.year}"
    return f"{', '.join(said)} and {ending}" if said else ending


@dataclass(frozen=True)
class DateNow:
    """What stands about one assignment's due date at a page's reading."""

    recorded: date | None
    readable: tuple[date, ...]
    """The distinct dates the counting claims give, earliest first."""
    evidence: Evidence
    in_doubt: bool
    """Whether the rule the planner clarifies by holds now."""
    unparseable: int
    """How many counting claims hold a value that is not a date."""
    unreadable: bool
    """Whether a claim row about the date cannot be read."""


def date_now(
    assignment: Assignment,
    noticing: Noticing,
    records: Sequence[SourceRecord],
    *,
    unreadable: bool,
) -> DateNow:
    """One assignment's due date set against its counting claims, by the planner's rules."""
    readable = noticing.observed_dates
    confidence = classify_confidence(reconcile_dates(records))
    if assignment.due_date is None:
        evidence = Evidence.UNKNOWN_CLAIMS if readable else Evidence.UNKNOWN_NONE
    elif not readable:
        evidence = Evidence.NONE
    elif noticing.contradicted:
        evidence = Evidence.CONTRADICTED
    elif confidence == SourceConfidence.SOURCES_DISAGREE:
        evidence = Evidence.DISAGREE
    else:
        evidence = Evidence.SUPPORT
    return DateNow(
        recorded=assignment.due_date,
        readable=readable,
        evidence=evidence,
        in_doubt=doubt_of(assignment.due_date, noticing, confidence) is not None,
        unparseable=sum(1 for item in records if read_date(item.asserted_value) is None),
        unreadable=unreadable,
    )


@dataclass(frozen=True)
class DatesNow:
    """What stands about the dates the plans in force on one page speak about, from the
    page's one reading: each assignment's present evidence, and the noticings every
    evening's week is chosen by, worked out once for all of them."""

    everything: Everything
    noticed: Mapping[str, Noticing]
    by_id: Mapping[str, DateNow]
    """Every assignment those plans speak about that is on record, by id."""

    def inputs_changed(self, plan_date: date, inputs_digest: str | None) -> bool:
        """Whether the week a plan was made from cannot be shown to read the same now. A plan
        without a fingerprint never reads as unchanged, and nothing here asks whether a plan
        is stale."""
        if inputs_digest is None:
            return True
        week = week_from(self.everything, plan_date, noticed=self.noticed)
        return planning_digest(week) != inputs_digest


def dates_now(everything: Everything, ids: Iterable[str]) -> DatesNow:
    """Present evidence for each assignment on record among ``ids``, worked out once each."""
    noticed = noticings_of(everything)
    on_record = {item.assignment_id: item for item in everything.assignments}
    return DatesNow(
        everything=everything,
        noticed=noticed,
        by_id={
            name: date_now(
                on_record[name],
                noticed[name],
                everything.records[name],
                unreadable=name in everything.claims_unavailable,
            )
            for name in sorted(set(ids))
            if name in on_record
        },
    )


def evidence_said(now: DateNow) -> str:
    """The present evidence about one date in a sentence."""
    words = EVIDENCE_WORDS[now.evidence]
    return words.format(dates=dates_said(now.readable)) if now.readable else words


@dataclass(frozen=True)
class RowNow:
    """What a row of a plan in force says about its date as things stand, in order."""

    words: tuple[str, ...]
    check: bool
    """Whether the row asks for the date to be checked here, since its saved line does not."""
    restates: bool
    """Whether the row restates present evidence, which the plan then qualifies once."""


def recorded_change(saved: date | None, now: date | None) -> str | None:
    """The recorded due date's change since the plan was made, or ``None`` when it is the
    same. Exact, since the plan keeps the date it read."""
    if saved == now:
        return None
    if now is None:
        return NOT_RECORDED_NOW
    if saved is None:
        return RECORDED.format(date=dates_said([now]))
    return CHANGED_TO.format(date=dates_said([now]))


def row_now(
    saved_due: date | None, cued: bool, now: DateNow, *, inputs_changed: bool
) -> RowNow | None:
    """The current line of a row of a plan in force, or ``None`` when it has nothing to say.
    A changed date and an unreadable claim always show; the sources show only when the week
    reads differently now, and never as agreement beside something that cannot be read."""
    words: list[str] = []
    moved = recorded_change(saved_due, now.recorded)
    if moved is not None:
        words.append(moved)
    withheld = now.evidence is Evidence.SUPPORT and (now.unreadable or now.unparseable > 0)
    evidence = inputs_changed and (now.in_doubt or cued or moved is not None) and not withheld
    if evidence:
        words.append(evidence_said(now))
    unparseable = inputs_changed and now.unparseable > 0
    if unparseable:
        words.append(NOT_A_DATE)
    if now.unreadable:
        words.append(UNREADABLE)
    check = now.in_doubt and (not cued or moved is not None)
    if not words and not check:
        return None
    return RowNow(words=tuple(words), check=check, restates=evidence or unparseable)
