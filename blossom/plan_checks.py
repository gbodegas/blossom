"""Tier-one checks over a proposed plan: what code can decide without judgment.

These run before a critic sees a plan and before a person does. Everything
here is decidable from the plan, the assignments, and the clock, so none of it
is an opinion: a block that ends after its assignment is due is wrong, and no
amount of good reasoning about it makes it right.

What a check cannot settle is kept out. Whether the order suits her, whether
an hour is the right size for that essay, whether tonight is too much: the
first two are the critic's, in
``blossom/heuristic_relevance.py``, and the last is hers alone. Keeping them
apart is what stops a heuristic from being read as a check.

A due date that is anything short of corroborated does not fail a plan. It is
carried through as a flag on the result, because the plan cannot be more
certain than the record it was built from, and hiding that would be the system
claiming more than it knows. One source counts as short of corroborated: that
is why it is a state of its own rather than a kind of yes.
"""

from collections import Counter
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from blossom.plans import DailyPlan
from blossom.reconciliation import SourceConfidence
from blossom.stores.project_state import Assignment
from blossom.verification import CheckOutcome

DEFAULT_DAILY_MINUTES = 150
"""How much work a plan may ask for in one evening before a check fails.

A placeholder for a household decision, not a finding about her. It is a
check rather than advice because a plan that asks for six hours is wrong
whatever its reasoning."""


class PlanCheck(StrEnum):
    """The tier-one checks a plan must pass. Every one of them is decidable."""

    ASSIGNMENTS_EXIST = "ASSIGNMENTS_EXIST"
    """Every assignment the plan names is one the store knows."""

    NOTHING_OMITTED = "NOTHING_OMITTED"
    """Every assignment due in the window is blocked or deferred with a reason."""

    ONE_DECISION_PER_ASSIGNMENT = "ONE_DECISION_PER_ASSIGNMENT"
    """Each assignment is worked on or put off, not both, and put off at most
    once. Several blocks for one assignment are fine: work can be split."""

    BLOCKS_MEET_DEADLINES = "BLOCKS_MEET_DEADLINES"
    """No block is scheduled after the day its assignment is due."""

    BLOCKS_DO_NOT_OVERLAP = "BLOCKS_DO_NOT_OVERLAP"
    """She is in one place at a time."""

    WITHIN_TIME_BUDGET = "WITHIN_TIME_BUDGET"
    """The evening's total is inside the household's limit."""


ORDERED_PLAN_CHECKS: tuple[PlanCheck, ...] = (
    PlanCheck.ASSIGNMENTS_EXIST,
    PlanCheck.NOTHING_OMITTED,
    PlanCheck.ONE_DECISION_PER_ASSIGNMENT,
    PlanCheck.BLOCKS_MEET_DEADLINES,
    PlanCheck.BLOCKS_DO_NOT_OVERLAP,
    PlanCheck.WITHIN_TIME_BUDGET,
)


class PlanVerification(BaseModel):
    """The outcome of running every tier-one check over one plan.

    ``passed`` is derived, as it is for a reconciled fact, so nothing
    downstream can mark a failing plan as checked.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcomes: dict[PlanCheck, CheckOutcome]
    findings: dict[PlanCheck, tuple[str, ...]] = {}
    """What failed, in words, so a critic and a person are told rather than
    left to work it out from the plan."""
    uncertain_due_dates: tuple[str, ...] = ()
    """Assignments whose due date is anything short of corroborated: one source
    only, sources in conflict, or nothing at all. Not a failure, and not a
    judgment about the plan; a flag it carries forward, because the plan cannot
    be more certain than the record it was built from."""

    @property
    def passed(self) -> bool:
        """True only when every check ran and passed."""
        if set(self.outcomes) != set(ORDERED_PLAN_CHECKS):
            return False
        return all(outcome is CheckOutcome.PASSED for outcome in self.outcomes.values())

    @property
    def failed_checks(self) -> tuple[PlanCheck, ...]:
        """Checks that ran and failed, in the order they are defined."""
        return tuple(
            check
            for check in ORDERED_PLAN_CHECKS
            if self.outcomes.get(check) is CheckOutcome.FAILED
        )

    def as_findings(self) -> tuple[str, ...]:
        """Every finding, flattened, for a prompt or a page to render."""
        return tuple(
            finding for check in ORDERED_PLAN_CHECKS for finding in self.findings.get(check, ())
        )


def check_plan(
    plan: DailyPlan,
    *,
    due_in_window: list[Assignment],
    zone: ZoneInfo,
    confidence: dict[str, SourceConfidence] | None = None,
    daily_minutes: int = DEFAULT_DAILY_MINUTES,
) -> PlanVerification:
    """Run every tier-one check over ``plan`` and report what failed and why.

    ``due_in_window`` is what the store says is due; the plan is measured
    against it rather than against itself. ``confidence`` is optional because
    a plan can be checked before reconciliation has run, and an absent label
    is simply not flagged.
    """
    known = {assignment.assignment_id: assignment for assignment in due_in_window}
    findings: dict[PlanCheck, list[str]] = {check: [] for check in ORDERED_PLAN_CHECKS}

    unknown = [name for name in plan.assignment_ids if name not in known]
    findings[PlanCheck.ASSIGNMENTS_EXIST].extend(
        f"{name} is not an assignment in this window" for name in sorted(set(unknown))
    )

    spoken_for = set(plan.assignment_ids)
    findings[PlanCheck.NOTHING_OMITTED].extend(
        f"{assignment.assignment_id} is due in this window and the plan does not mention it"
        for assignment in due_in_window
        if assignment.assignment_id not in spoken_for
    )

    blocked = set(plan.blocked_ids)
    findings[PlanCheck.ONE_DECISION_PER_ASSIGNMENT].extend(
        f"{name} is both worked on and put off"
        for name in sorted(blocked.intersection(plan.deferred_ids))
    )
    findings[PlanCheck.ONE_DECISION_PER_ASSIGNMENT].extend(
        f"{name} is put off {count} times"
        for name, count in sorted(Counter(plan.deferred_ids).items())
        if count > 1
    )

    for block in plan.blocks:
        assignment = known.get(block.assignment_id)
        if assignment is not None and plan.plan_date > assignment.due_date:
            findings[PlanCheck.BLOCKS_MEET_DEADLINES].append(
                f"{block.assignment_id} is due {assignment.due_date} and is scheduled "
                f"{plan.plan_date}, after it"
            )

    findings[PlanCheck.BLOCKS_DO_NOT_OVERLAP].extend(
        f"{earlier.assignment_id} at {earlier.starts_at} overlaps "
        f"{later.assignment_id} at {later.starts_at}"
        for earlier, later in plan.overlapping_pairs()
    )

    total = plan.total_minutes(zone)
    if total > daily_minutes:
        findings[PlanCheck.WITHIN_TIME_BUDGET].append(
            f"the plan asks for {total} minutes and the evening allows {daily_minutes}"
        )

    labels = confidence or {}
    # Read by exclusion rather than by listing the doubtful states. A single
    # source is not corroboration, which is the reason it is its own state, and
    # a state added later should read as uncertain until somebody decides
    # otherwise rather than passing unnoticed.
    uncertain = tuple(
        sorted(
            name
            for name in set(plan.assignment_ids)
            if name in labels and labels[name] is not SourceConfidence.CORROBORATED
        )
    )
    return PlanVerification(
        outcomes={
            check: CheckOutcome.FAILED if findings[check] else CheckOutcome.PASSED
            for check in ORDERED_PLAN_CHECKS
        },
        findings={check: tuple(found) for check, found in findings.items() if found},
        uncertain_due_dates=uncertain,
    )
