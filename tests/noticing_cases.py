"""The labeled table the comparator is held to.

Each row states what the record said, what the sources said, and the verdict a
person would give. The rows are balanced on purpose: as many contradictions
as confirmations, and a separate group of cases where the honest answer is
that nothing can be concluded, so a comparator that reads every mismatch as a
contradiction fails here rather than in the household.
"""

from dataclasses import dataclass
from datetime import date

from blossom.noticing import Verdict
from blossom.reconciliation import SourceChannel, SourceRecord
from tests.support import record

FRIDAY = date(2026, 8, 21)
SATURDAY = date(2026, 8, 22)
SUNDAY = date(2026, 8, 23)


@dataclass(frozen=True, kw_only=True)
class Case:
    name: str
    expected: date | None
    records: list[SourceRecord]
    verdict: Verdict


def lms(value: str) -> SourceRecord:
    return record(SourceChannel.LMS, value)


def parent(value: str) -> SourceRecord:
    return record(SourceChannel.PARENT_ENTRY, value)


def student(value: str) -> SourceRecord:
    return record(SourceChannel.STUDENT_REPORT, value)


CASES: tuple[Case, ...] = (
    # ------------------------------------------------- the sources contradict the record
    Case(
        name="one source gives a different date",
        expected=FRIDAY,
        records=[lms("2026-08-22")],
        verdict=Verdict.CONTRADICTED,
    ),
    Case(
        name="two sources agree on a different date",
        expected=FRIDAY,
        records=[lms("2026-08-22"), parent("2026-08-22")],
        verdict=Verdict.CONTRADICTED,
    ),
    Case(
        name="two sources disagree with each other and neither supports the record",
        expected=FRIDAY,
        records=[lms("2026-08-22"), parent("2026-08-23")],
        verdict=Verdict.CONTRADICTED,
    ),
    Case(
        name="the record has no date and a source gives one",
        expected=None,
        records=[lms("2026-08-21")],
        verdict=Verdict.CONTRADICTED,
    ),
    Case(
        name="a different date beside a value that cannot be read",
        expected=FRIDAY,
        records=[lms("2026-08-22"), student("Friday")],
        verdict=Verdict.CONTRADICTED,
    ),
    Case(
        name="the same day in the wrong year",
        expected=FRIDAY,
        records=[lms("2025-08-21")],
        verdict=Verdict.CONTRADICTED,
    ),
    # ------------------------------------------------------ the sources confirm the record
    Case(
        name="one source gives the record's date",
        expected=FRIDAY,
        records=[lms("2026-08-21")],
        verdict=Verdict.CONFIRMED,
    ),
    Case(
        name="two sources give the record's date",
        expected=FRIDAY,
        records=[lms("2026-08-21"), parent("2026-08-21")],
        verdict=Verdict.CONFIRMED,
    ),
    Case(
        name="the record's date with whitespace around it",
        expected=FRIDAY,
        records=[lms(" 2026-08-21 ")],
        verdict=Verdict.CONFIRMED,
    ),
    Case(
        name="the record's date beside a value that cannot be read",
        expected=FRIDAY,
        records=[lms("2026-08-21"), parent("Friday")],
        verdict=Verdict.CONFIRMED,
    ),
    Case(
        name="three channels give the record's date",
        expected=SATURDAY,
        records=[lms("2026-08-22"), parent("2026-08-22"), student("2026-08-22")],
        verdict=Verdict.CONFIRMED,
    ),
    Case(
        name="the record's date from the same channel twice",
        expected=SUNDAY,
        records=[lms("2026-08-23"), lms("2026-08-23")],
        verdict=Verdict.CONFIRMED,
    ),
    # ------------------------------------------------------------ nothing can be concluded
    Case(
        name="no source has reported and the record has a date",
        expected=FRIDAY,
        records=[],
        verdict=Verdict.UNDECIDABLE,
    ),
    Case(
        name="no source has reported and the record has no date",
        expected=None,
        records=[],
        verdict=Verdict.UNDECIDABLE,
    ),
    Case(
        name="the only source names a weekday",
        expected=FRIDAY,
        records=[lms("Friday")],
        verdict=Verdict.UNDECIDABLE,
    ),
    Case(
        name="the only source writes the date another way",
        expected=FRIDAY,
        records=[lms("8/21/2026")],
        verdict=Verdict.UNDECIDABLE,
    ),
    Case(
        name="the sources disagree and one of them supports the record",
        expected=FRIDAY,
        records=[lms("2026-08-21"), parent("2026-08-22")],
        verdict=Verdict.UNDECIDABLE,
    ),
    Case(
        name="the record has no date and the only source says to be announced",
        expected=None,
        records=[lms("TBD")],
        verdict=Verdict.UNDECIDABLE,
    ),
)
