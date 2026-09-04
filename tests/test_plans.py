"""What a plan is, what code can decide about one, and what it cannot.

The tier-one checks here settle the questions that have answers: does this
assignment exist, is anything missing, does a block run past its deadline, do
two blocks claim the same minute, is the evening too long. The critic's
verdict, which is tier two, is shaped so that its overall result is read off
its findings rather than asserted.

Durations are measured through UTC, so the two nights a year when a day is not
twenty-four hours long are covered here rather than discovered later.
"""

from datetime import date, time
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from blossom.heuristic_relevance import CriterionFinding, CriticVerdict, Judgment
from blossom.plan_checks import (
    DEFAULT_DAILY_MINUTES,
    ORDERED_PLAN_CHECKS,
    PlanCheck,
    PlanVerification,
    check_plan,
)
from blossom.plans import DailyPlan, Deferral, PlanBlock
from blossom.reconciliation import SourceConfidence
from blossom.stores.project_state import Assignment
from blossom.verification import CheckOutcome
from tests.support import FIXTURE_TIMEZONE

ZONE = ZoneInfo(FIXTURE_TIMEZONE)
PLAN_DATE = date(2026, 8, 19)

ESSAY = Assignment(
    assignment_id="assignment-canal-essay",
    course="World History",
    title="Canal Era comparison essay",
    due_date=date(2026, 8, 21),
    dependencies=[],
    reported_submission_status="in_progress",
)
PROBLEM_SET = Assignment(
    assignment_id="assignment-algebra-set",
    course="Algebra II",
    title="Quadratic modeling problem set",
    due_date=date(2026, 8, 24),
    dependencies=[],
    reported_submission_status="not_started",
)
WINDOW = [ESSAY, PROBLEM_SET]


def block(
    assignment: str, start: str, end: str, why: str = "she has the most energy now"
) -> PlanBlock:
    return PlanBlock(
        assignment_id=assignment,
        starts_at=time.fromisoformat(start),
        ends_at=time.fromisoformat(end),
        rationale=why,
    )


def workable_plan() -> DailyPlan:
    """A plan that passes every check, which the failing cases vary from."""
    return DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            block("assignment-canal-essay", "16:30", "17:30"),
            block("assignment-algebra-set", "18:00", "18:45"),
        ],
    )


# ------------------------------------------------------------------- a block


def test_a_block_must_end_after_it_starts() -> None:
    with pytest.raises(ValidationError, match="end after it starts"):
        block("assignment-canal-essay", "17:30", "16:30")
    with pytest.raises(ValidationError, match="end after it starts"):
        block("assignment-canal-essay", "17:30", "17:30")


def test_a_blocks_length_is_measured_on_an_ordinary_day() -> None:
    assert block("assignment-canal-essay", "16:30", "17:30").minutes(PLAN_DATE, ZONE) == 60


def test_an_hour_on_the_night_the_clocks_go_back_is_longer_than_it_reads() -> None:
    """Not a realistic study hour, but the arithmetic is the point: wall-clock
    subtraction would say ninety minutes and the evening would really take
    two and a half hours."""
    autumn = date(2026, 11, 1)

    spans = block("assignment-canal-essay", "01:30", "03:00").minutes(autumn, ZONE)

    assert spans == 150


def test_an_hour_on_the_night_the_clocks_go_forward_is_shorter_than_it_reads() -> None:
    spring = date(2027, 3, 14)

    spans = block("assignment-canal-essay", "01:30", "03:30").minutes(spring, ZONE)

    assert spans == 60


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (("16:00", "17:00"), ("17:00", "18:00"), False),
        (("16:00", "17:00"), ("16:30", "17:30"), True),
        (("16:00", "18:00"), ("16:30", "17:00"), True),
        (("16:00", "17:00"), ("18:00", "19:00"), False),
    ],
    ids=["back-to-back", "partly", "nested", "apart"],
)
def test_blocks_know_when_they_claim_the_same_minute(
    first: tuple[str, str], second: tuple[str, str], expected: bool
) -> None:
    """Back to back is not an overlap: a block ends the minute the next begins."""
    one = block("a", *first)
    two = block("b", *second)

    assert one.overlaps(two) is expected
    assert two.overlaps(one) is expected


# -------------------------------------------------------------------- a plan


def test_a_plan_speaks_about_blocked_and_deferred_work_alike() -> None:
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[block("assignment-canal-essay", "16:30", "17:30")],
        deferred=[Deferral(assignment_id="assignment-algebra-set", reason="not due until Monday")],
    )

    assert set(plan.assignment_ids) == {"assignment-canal-essay", "assignment-algebra-set"}


def test_a_plan_totals_the_minutes_it_asks_for() -> None:
    assert workable_plan().total_minutes(ZONE) == 105


def test_a_plan_reports_every_overlapping_pair_not_just_the_first() -> None:
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            block("a", "16:00", "18:00"),
            block("b", "16:30", "17:00"),
            block("c", "17:30", "19:00"),
        ],
    )

    pairs = {
        (earlier.assignment_id, later.assignment_id) for earlier, later in plan.overlapping_pairs()
    }

    assert pairs == {("a", "b"), ("a", "c")}


# ------------------------------------------------------------- the checks


def test_a_workable_plan_passes_every_check() -> None:
    result = check_plan(workable_plan(), due_in_window=WINDOW, zone=ZONE)

    assert result.passed
    assert result.failed_checks == ()
    assert result.as_findings() == ()
    assert set(result.outcomes) == set(ORDERED_PLAN_CHECKS)


def test_a_plan_naming_an_assignment_nobody_has_fails_and_says_which() -> None:
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[block("assignment-invented", "16:30", "17:30")],
        deferred=[
            Deferral(assignment_id="assignment-canal-essay", reason="tomorrow"),
            Deferral(assignment_id="assignment-algebra-set", reason="tomorrow"),
        ],
    )

    result = check_plan(plan, due_in_window=WINDOW, zone=ZONE)

    assert not result.passed
    assert result.failed_checks == (PlanCheck.ASSIGNMENTS_EXIST,)
    assert "assignment-invented" in result.as_findings()[0]


def test_leaving_work_out_without_saying_so_fails() -> None:
    """Nothing is dropped silently: an assignment is blocked or deferred with a reason."""
    plan = DailyPlan(
        plan_date=PLAN_DATE, blocks=[block("assignment-canal-essay", "16:30", "17:30")]
    )

    result = check_plan(plan, due_in_window=WINDOW, zone=ZONE)

    assert result.failed_checks == (PlanCheck.NOTHING_OMITTED,)
    assert "assignment-algebra-set" in result.as_findings()[0]


def test_deferring_work_with_a_reason_is_not_leaving_it_out() -> None:
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[block("assignment-canal-essay", "16:30", "17:30")],
        deferred=[Deferral(assignment_id="assignment-algebra-set", reason="not due until Monday")],
    )

    assert check_plan(plan, due_in_window=WINDOW, zone=ZONE).passed


def test_a_block_scheduled_after_its_deadline_fails() -> None:
    late = DailyPlan(
        plan_date=date(2026, 8, 22),
        blocks=[block("assignment-canal-essay", "16:30", "17:30")],
        deferred=[Deferral(assignment_id="assignment-algebra-set", reason="tomorrow")],
    )

    result = check_plan(late, due_in_window=WINDOW, zone=ZONE)

    assert result.failed_checks == (PlanCheck.BLOCKS_MEET_DEADLINES,)
    assert "after it" in result.as_findings()[0]


def test_two_blocks_at_once_fails() -> None:
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            block("assignment-canal-essay", "16:30", "17:30"),
            block("assignment-algebra-set", "17:00", "18:00"),
        ],
    )

    result = check_plan(plan, due_in_window=WINDOW, zone=ZONE)

    assert result.failed_checks == (PlanCheck.BLOCKS_DO_NOT_OVERLAP,)


def test_an_evening_longer_than_the_budget_fails_and_names_both_numbers() -> None:
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            block("assignment-canal-essay", "15:00", "18:00"),
            block("assignment-algebra-set", "18:00", "20:00"),
        ],
    )

    result = check_plan(plan, due_in_window=WINDOW, zone=ZONE, daily_minutes=60)

    assert result.failed_checks == (PlanCheck.WITHIN_TIME_BUDGET,)
    assert "300 minutes" in result.as_findings()[0]
    assert "allows 60" in result.as_findings()[0]


def test_the_budget_has_a_default_a_plan_can_exceed() -> None:
    assert workable_plan().total_minutes(ZONE) < DEFAULT_DAILY_MINUTES


def test_an_uncertain_due_date_is_flagged_and_does_not_fail_the_plan() -> None:
    """A plan cannot be more certain than the record it was built from, and
    saying so is not the same as refusing to plan."""
    result = check_plan(
        workable_plan(),
        due_in_window=WINDOW,
        zone=ZONE,
        confidence={
            "assignment-canal-essay": SourceConfidence.SOURCES_DISAGREE,
            "assignment-algebra-set": SourceConfidence.CORROBORATED,
        },
    )

    assert result.passed
    assert result.uncertain_due_dates == ("assignment-canal-essay",)


@pytest.mark.parametrize(
    "label",
    [
        SourceConfidence.SOURCES_DISAGREE,
        SourceConfidence.UNVERIFIED,
        SourceConfidence.SINGLE_SOURCE,
    ],
)
def test_every_kind_of_doubt_is_flagged(label: SourceConfidence) -> None:
    """One source is doubt too. It is a state of its own precisely because a
    date one channel asserted is not the same claim as one two channels agree
    on, so reading it as corroboration would undo that distinction here."""
    result = check_plan(
        workable_plan(),
        due_in_window=WINDOW,
        zone=ZONE,
        confidence={"assignment-canal-essay": label},
    )

    assert result.uncertain_due_dates == ("assignment-canal-essay",)


def test_a_corroborated_date_is_not_flagged() -> None:
    result = check_plan(
        workable_plan(),
        due_in_window=WINDOW,
        zone=ZONE,
        confidence={name: SourceConfidence.CORROBORATED for name in workable_plan().assignment_ids},
    )

    assert result.uncertain_due_dates == ()


def test_an_assignment_with_no_label_is_not_flagged() -> None:
    """A plan can be checked before reconciliation has run, and silence about a
    date is not the same as doubt about it."""
    assert check_plan(workable_plan(), due_in_window=WINDOW, zone=ZONE).uncertain_due_dates == ()


# ------------------------------------------------- one decision per assignment


def test_work_may_be_split_across_two_sittings() -> None:
    """An essay in two blocks is good planning, not a duplicate."""
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            block("assignment-canal-essay", "16:30", "17:00"),
            block("assignment-canal-essay", "19:00", "19:30", "after dinner, second pass"),
        ],
        deferred=[Deferral(assignment_id="assignment-algebra-set", reason="not due until Monday")],
    )

    assert check_plan(plan, due_in_window=WINDOW, zone=ZONE).passed


def test_an_assignment_cannot_be_both_worked_on_and_put_off() -> None:
    """The plan would be contradicting itself, and the omission check alone
    reads it as covered because the name appears somewhere."""
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            block("assignment-canal-essay", "16:30", "17:30"),
            block("assignment-algebra-set", "18:00", "18:30"),
        ],
        deferred=[Deferral(assignment_id="assignment-canal-essay", reason="not tonight after all")],
    )

    result = check_plan(plan, due_in_window=WINDOW, zone=ZONE)

    assert result.failed_checks == (PlanCheck.ONE_DECISION_PER_ASSIGNMENT,)
    assert "both worked on and put off" in result.as_findings()[0]


def test_an_assignment_cannot_be_put_off_twice() -> None:
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[block("assignment-canal-essay", "16:30", "17:30")],
        deferred=[
            Deferral(assignment_id="assignment-algebra-set", reason="not due until Monday"),
            Deferral(assignment_id="assignment-algebra-set", reason="and she is tired"),
        ],
    )

    result = check_plan(plan, due_in_window=WINDOW, zone=ZONE)

    assert result.failed_checks == (PlanCheck.ONE_DECISION_PER_ASSIGNMENT,)
    assert "put off 2 times" in result.as_findings()[0]


# ------------------------------------------------------------ a real reason


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"], ids=["empty", "spaces", "tab-newline"])
def test_a_block_without_a_rationale_is_refused(blank: str) -> None:
    with pytest.raises(ValidationError):
        block("assignment-canal-essay", "16:30", "17:30", blank)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"], ids=["empty", "spaces", "tab-newline"])
def test_a_deferral_without_a_reason_is_refused(blank: str) -> None:
    """Deferring is allowed because it comes with an account of itself. A blank
    reason would let a plan drop work while looking like it had explained."""
    with pytest.raises(ValidationError):
        Deferral(assignment_id="assignment-algebra-set", reason=blank)


def test_surrounding_whitespace_is_trimmed_rather_than_counted() -> None:
    kept = Deferral(assignment_id="assignment-algebra-set", reason="  not due until Monday  ")

    assert kept.reason == "not due until Monday"


def test_several_failures_are_all_reported() -> None:
    """A plan is told everything that is wrong with it, not the first thing."""
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            block("assignment-invented", "16:00", "17:00"),
            block("assignment-canal-essay", "16:30", "17:30"),
        ],
    )

    result = check_plan(plan, due_in_window=WINDOW, zone=ZONE)

    assert set(result.failed_checks) == {
        PlanCheck.ASSIGNMENTS_EXIST,
        PlanCheck.NOTHING_OMITTED,
        PlanCheck.BLOCKS_DO_NOT_OVERLAP,
    }
    assert len(result.as_findings()) == 3


def test_a_verification_missing_a_check_does_not_pass() -> None:
    """``passed`` is derived from every check having run, so a partial result
    cannot be presented as a clean one."""
    partial = PlanVerification(outcomes={PlanCheck.ASSIGNMENTS_EXIST: CheckOutcome.PASSED})

    assert not partial.passed


# ------------------------------------------------------------- the verdict


def finding(judgment: Judgment, criterion: str = "order") -> CriterionFinding:
    return CriterionFinding(
        criterion=criterion, critique="the essay sits before the problem set", judgment=judgment
    )


def test_a_verdict_is_accepted_only_when_every_criterion_passed() -> None:
    verdict = CriticVerdict(findings=[finding(Judgment.PASSES), finding(Judgment.PASSES, "length")])

    assert verdict.accepted
    assert verdict.failed == ()
    assert verdict.undecided == ()


def test_one_failure_is_enough_to_withhold_acceptance() -> None:
    verdict = CriticVerdict(findings=[finding(Judgment.PASSES), finding(Judgment.FAILS, "length")])

    assert not verdict.accepted
    assert [item.criterion for item in verdict.failed] == ["length"]


def test_cannot_tell_neither_passes_nor_fails_a_plan() -> None:
    """A critic that must choose will invent a reason to; an undecided
    criterion goes to a person instead."""
    verdict = CriticVerdict(
        findings=[finding(Judgment.PASSES), finding(Judgment.CANNOT_TELL, "fit")]
    )

    assert not verdict.accepted
    assert verdict.failed == ()
    assert [item.criterion for item in verdict.undecided] == ["fit"]


def test_a_verdict_that_judged_nothing_is_not_an_acceptance() -> None:
    assert not CriticVerdict(findings=[]).accepted


def test_a_verdict_cannot_be_told_it_passed() -> None:
    """There is no field to set: acceptance is read off the findings."""
    payload = {"findings": [finding(Judgment.FAILS).model_dump()], "accepted": True}

    with pytest.raises(ValidationError, match="accepted"):
        CriticVerdict.model_validate(payload)
