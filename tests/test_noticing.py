"""Expectation before action: a typed comparison with a three-way verdict.

The comparator is deterministic tier-one code, so its error rate is not
estimated; it is a regression, and the table in ``tests/noticing_cases.py``
holds it at zero. Precision and recall are reported for the contradicted
verdict, the one the system acts on, and the undecidable share separately, so
a change that starts calling the unreadable a contradiction shows up as a lost
point of precision rather than as a green run.
"""

from collections import Counter
from datetime import date

import pytest

from blossom.noticing import (
    DueDateExpectation,
    Verdict,
    expect_due_date,
    notice_due_date,
    read_date,
)
from blossom.reconciliation import SourceChannel
from blossom.stores.project_state import Assignment
from tests.noticing_cases import CASES, FRIDAY, SATURDAY, Case, lms, parent
from tests.support import record

ESSAY = Assignment(
    assignment_id="assignment-canal-essay",
    course="World History",
    title="Canal Era comparison essay",
    due_date=FRIDAY,
    dependencies=[],
    reported_submission_status="in_progress",
)


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_each_labeled_case_gets_the_verdict_a_person_would_give(case: Case) -> None:
    expectation = DueDateExpectation(assignment_id="a", due_date=case.expected)

    assert notice_due_date(expectation, case.records).verdict is case.verdict


def test_the_table_is_balanced_and_covers_every_verdict() -> None:
    labeled = Counter(case.verdict for case in CASES)

    assert set(labeled) == set(Verdict)
    assert labeled[Verdict.CONTRADICTED] == labeled[Verdict.CONFIRMED]
    assert min(labeled.values()) >= 5


def test_precision_and_recall_for_contradicted_are_both_whole() -> None:
    """Counted rather than assumed, so the numbers are in the run's record."""
    verdicts = {
        case.name: notice_due_date(
            DueDateExpectation(assignment_id="a", due_date=case.expected), case.records
        ).verdict
        for case in CASES
    }
    flagged = {name for name, verdict in verdicts.items() if verdict is Verdict.CONTRADICTED}
    labeled = {case.name for case in CASES if case.verdict is Verdict.CONTRADICTED}
    undecidable = sum(verdict is Verdict.UNDECIDABLE for verdict in verdicts.values())

    true_positives = len(flagged & labeled)
    false_positives = len(flagged - labeled)
    false_negatives = len(labeled - flagged)

    assert (true_positives, false_positives, false_negatives) == (len(labeled), 0, 0)
    assert true_positives / (true_positives + false_positives) == 1.0
    assert true_positives / (true_positives + false_negatives) == 1.0
    assert undecidable == sum(case.verdict is Verdict.UNDECIDABLE for case in CASES)


def test_the_expectation_is_the_records_date_and_nothing_observed() -> None:
    expectation = expect_due_date(ESSAY)

    assert expectation == DueDateExpectation(
        assignment_id="assignment-canal-essay", due_date=FRIDAY
    )


def test_a_noticing_keeps_what_every_source_said_including_the_unreadable() -> None:
    noticed = notice_due_date(expect_due_date(ESSAY), [lms("2026-08-22"), parent("Saturday")])

    assert noticed.verdict is Verdict.CONTRADICTED
    assert noticed.observed == ("LMS: 2026-08-22", "PARENT_ENTRY: Saturday")
    assert noticed.observed_dates == (SATURDAY,)
    assert noticed.sources_say() == "LMS: 2026-08-22; PARENT_ENTRY: Saturday"


def test_the_earliest_date_is_the_deadline_whichever_side_gives_it() -> None:
    later = notice_due_date(expect_due_date(ESSAY), [lms("2026-08-22")])
    earlier = notice_due_date(expect_due_date(ESSAY), [lms("2026-08-18")])
    none_on_record = notice_due_date(
        DueDateExpectation(assignment_id="a", due_date=None), [lms("2026-08-20")]
    )
    nothing_anywhere = notice_due_date(DueDateExpectation(assignment_id="a", due_date=None), [])

    assert later.earliest_date == FRIDAY
    assert earlier.earliest_date == date(2026, 8, 18)
    assert none_on_record.earliest_date == date(2026, 8, 20)
    assert nothing_anywhere.earliest_date is None


def test_a_seen_in_label_travels_with_the_claim() -> None:
    header = record(SourceChannel.LMS, "2026-08-22").model_copy(update={"seen_in": "day header"})

    noticed = notice_due_date(expect_due_date(ESSAY), [header])

    assert noticed.observed == ("LMS (day header): 2026-08-22",)


@pytest.mark.parametrize("value", ["Friday", "8/21/2026", "TBD", "", "2026-8-21"])
def test_only_an_iso_date_is_read_as_a_date(value: str) -> None:
    assert read_date(value) is None


def test_an_iso_date_is_read_with_whitespace_around_it() -> None:
    assert read_date(" 2026-08-21\n") == FRIDAY
