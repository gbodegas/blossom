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
from collections.abc import Sequence
from datetime import date
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from blossom.noticing import Noticing
from blossom.plans import DailyPlan
from blossom.reconciliation import SourceConfidence
from blossom.settings import DEFAULT_EVENING_MINUTES
from blossom.stores.project_state import Assignment
from blossom.verification import CheckOutcome

"""How much work a plan may ask for in one evening before a check fails.

A placeholder for a household decision, not a finding about her. It is a
check rather than advice because a plan that asks for six hours is wrong
whatever its reasoning."""


class PlanCheck(StrEnum):
    """The tier-one checks a plan must pass. Every one of them is decidable."""

    ASSIGNMENTS_EXIST = "ASSIGNMENTS_EXIST"
    """Every assignment the plan names is one the store knows."""

    NOTHING_OMITTED = "NOTHING_OMITTED"
    """Every assignment due in the window and still to do is blocked or deferred with a
    reason."""

    NO_REPORTED_DONE_WORK = "NO_REPORTED_DONE_WORK"
    """Nothing she had reported done when the run read the week is worked on or put off.
    The ids are the run's own reading, frozen with the rest of its input and kept on the
    server; no model is sent them. Such an id is outside the window the plan was given,
    so ``ASSIGNMENTS_EXIST`` fails with it, and this check names the reason for the
    record. A report that lands after the reading is not this check's to catch: the
    draft's fingerprint is, on the pages and at approval."""

    ONE_DECISION_PER_ASSIGNMENT = "ONE_DECISION_PER_ASSIGNMENT"
    """Each assignment is worked on or put off, not both, and put off at most
    once. Several blocks for one assignment are fine: work can be split."""

    BLOCKS_MEET_DEADLINES = "BLOCKS_MEET_DEADLINES"
    """No block is scheduled after the day its assignment is due, and nothing
    due by the plan date is put off, since putting it off moves it past the day."""

    BLOCKS_DO_NOT_OVERLAP = "BLOCKS_DO_NOT_OVERLAP"
    """She is in one place at a time."""

    WITHIN_TIME_BUDGET = "WITHIN_TIME_BUDGET"
    """The evening's total is inside the household's limit."""


ONLY_WHAT_IS_LISTED = "plan only the assignments listed, and leave out anything else"
"""What the planner is told when its plan spoke about work she had reported done, in place
of the finding that says so."""

ORDERED_PLAN_CHECKS: tuple[PlanCheck, ...] = (
    PlanCheck.ASSIGNMENTS_EXIST,
    PlanCheck.NOTHING_OMITTED,
    PlanCheck.NO_REPORTED_DONE_WORK,
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
    undated: tuple[str, ...] = ()
    """Assignments in the window with no due date on record. Also a flag rather
    than a failure: the plan still has to account for them, and the person at
    the gate is told the date is missing rather than merely doubtful."""
    contradicted: tuple[str, ...] = ()
    """Assignments whose due date on record no source supports. The deadline
    check measures these against the earliest date anyone gives, record or
    source, so a plan built on the record alone cannot pass by trusting it."""

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
        """Every finding, flattened, for the run's record and a page to render."""
        return tuple(
            finding for check in ORDERED_PLAN_CHECKS for finding in self.findings.get(check, ())
        )

    def as_feedback(self) -> tuple[str, ...]:
        """The findings as the planner is sent them back: every one but what she has reported.

        That a plan speaks about work reported done stays in the record. The
        planner was never given that work, and is not told of it now: the
        same id fails ``ASSIGNMENTS_EXIST``, whose finding says only that it
        is not in the window, and one plain instruction goes with it.
        """
        kept = tuple(
            finding
            for check in ORDERED_PLAN_CHECKS
            if check is not PlanCheck.NO_REPORTED_DONE_WORK
            for finding in self.findings.get(check, ())
        )
        if PlanCheck.NO_REPORTED_DONE_WORK not in self.findings:
            return kept
        return (*kept, ONLY_WHAT_IS_LISTED)


def check_plan(
    plan: DailyPlan,
    *,
    due_in_window: list[Assignment],
    zone: ZoneInfo,
    confidence: dict[str, SourceConfidence] | None = None,
    noticings: Sequence[Noticing] = (),
    daily_minutes: int = DEFAULT_EVENING_MINUTES,
    reported_done: Sequence[str] = (),
) -> PlanVerification:
    """Run every tier-one check over ``plan`` and report what failed and why.

    ``due_in_window`` is what the store says is due and still to do; the plan
    is measured against it rather than against itself. ``confidence`` is
    optional because a plan can be checked before reconciliation has run, and
    an absent label is simply not flagged. ``noticings`` are the record's due
    dates set against the sources; where the sources contradict the record,
    the deadline is the earliest date either gives. ``reported_done`` names
    the work in the window she has reported done, which the plan was given
    nothing about and must say nothing about.
    """
    known = {assignment.assignment_id: assignment for assignment in due_in_window}
    done = set(reported_done)
    contradicted = {item.assignment_id: item for item in noticings if item.contradicted}
    findings: dict[PlanCheck, list[str]] = {check: [] for check in ORDERED_PLAN_CHECKS}

    unknown = [name for name in plan.assignment_ids if name not in known]
    findings[PlanCheck.ASSIGNMENTS_EXIST].extend(
        f"{name} is not an assignment in this window" for name in sorted(set(unknown))
    )
    findings[PlanCheck.NO_REPORTED_DONE_WORK].extend(
        f"{name} is reported done and the plan still speaks about it"
        for name in sorted(done.intersection(plan.assignment_ids))
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

    def deadline_of(assignment: Assignment) -> tuple[date | None, str]:
        """The day the work must be done by, and where that day comes from."""
        noticed = contradicted.get(assignment.assignment_id)
        if noticed is None:
            return assignment.due_date, ""
        return noticed.earliest_date, " by the earliest date the record or a source gives"

    for block in plan.blocks:
        assignment = known.get(block.assignment_id)
        if assignment is None:
            continue
        deadline, basis = deadline_of(assignment)
        # An undated assignment has no deadline to run past; it is flagged below.
        if deadline is not None and plan.plan_date > deadline:
            findings[PlanCheck.BLOCKS_MEET_DEADLINES].append(
                f"{block.assignment_id} is due {deadline}{basis} and is scheduled "
                f"{plan.plan_date}, after it"
            )

    for deferral in plan.deferred:
        assignment = known.get(deferral.assignment_id)
        if assignment is None:
            continue
        deadline, basis = deadline_of(assignment)
        # Put off means another day at the earliest, so due today is already too late.
        if deadline is not None and plan.plan_date >= deadline:
            findings[PlanCheck.BLOCKS_MEET_DEADLINES].append(
                f"{deferral.assignment_id} is due {deadline}{basis} and is put off from "
                f"{plan.plan_date}, past it"
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
        undated=tuple(
            sorted(item.assignment_id for item in due_in_window if item.due_date is None)
        ),
        contradicted=tuple(sorted(name for name in contradicted if name in known)),
    )
