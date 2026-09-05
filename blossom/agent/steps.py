"""What each node of the plan graph expected and found, kept as the record of a run.

A run of the plan graph is a few decisions in a row: what the week holds,
whether the plan passes the checks, whether the reviewer accepts it. The state
at the end says where the run landed but not how it got there, because a
passing check clears the findings that sent the plan back. A step record keeps
that: one line per node, saying what the node expected before it acted and
what it found, in words a person can read on the parent's page.

The records are data about the run, not the run itself. Nothing reads them to
decide what happens next; the graph's edges do that from the typed values.
"""

from collections.abc import Sequence
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict

from blossom.heuristic_relevance import CriticVerdict
from blossom.noticing import Noticing
from blossom.plan_checks import ORDERED_PLAN_CHECKS, PlanVerification
from blossom.plans import DailyPlan
from blossom.reconciliation import SourceConfidence
from blossom.stores.project_state import Assignment


class StepRecord(BaseModel):
    """One node's turn: what it expected before acting, what it found, and when."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: str
    round: int
    """The planner round the step belongs to; the first read of the week is round 0."""
    expected: str
    found: str
    recorded_at: AwareDatetime


EXPECT_RECORD_HOLDS = "the record's due dates hold against the school's sources"
EXPECT_ALL_CHECKS = "every tier-one check passes"
EXPECT_ACCEPTANCE = "the reviewer passes every criterion"

FAILURES = {
    "model_truncated": "the answer was cut off",
    "model_refused": "the model declined",
    "model_unparseable": "the answer did not parse",
}

OUTCOMES = {
    "checks_failed": "The plan failed its checks after every revision.",
    "model_truncated": "The model's answer was cut off.",
    "model_refused": "The model declined to answer.",
    "model_unparseable": "The model's answer did not parse.",
}


def count(number: int, noun: str) -> str:
    """``1 block``, ``2 blocks``."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def tokens_note(input_tokens: int | None, output_tokens: int | None) -> str:
    """The cost of a model call in tokens, or nothing when the answer did not carry it."""
    if input_tokens is None or output_tokens is None:
        return ""
    return f" ({input_tokens} tokens in, {output_tokens} out)"


def expect_plan(round_number: int, findings: int, budget_minutes: int) -> str:
    """What the planner is asked for: a first plan, or a revision that answers findings."""
    if round_number == 1:
        return f"a plan that accounts for every assignment inside {budget_minutes} minutes"
    return f"a revised plan that answers {count(findings, 'finding')}"


def describe_week(
    assignments: Sequence[Assignment],
    noticings: Sequence[Noticing],
    confidence: dict[str, SourceConfidence],
    *,
    rules: int,
    notes: int,
) -> str:
    """The week in one line: how much there is and how much of it is in doubt."""
    contradicted = sum(item.contradicted for item in noticings)
    uncertain = sum(
        label is not SourceConfidence.CORROBORATED
        for name, label in confidence.items()
        if name in {item.assignment_id for item in assignments}
    )
    undated = sum(item.due_date is None for item in assignments)
    return (
        f"{count(len(assignments), 'assignment')} in the week: {contradicted} contradicted, "
        f"{uncertain} uncertain, {undated} undated; {count(rules, 'rule')} and "
        f"{count(notes, 'note')} to follow"
    )


def describe_plan(plan: DailyPlan, zone: ZoneInfo, tokens: str) -> str:
    """The shape of a plan: how many blocks and deferrals, and how long it asks for."""
    return (
        f"{count(len(plan.blocks), 'block')} and {count(len(plan.deferred), 'deferral')} "
        f"asking {plan.total_minutes(zone)} minutes{tokens}"
    )


def describe_failure(outcome: str, what: str, tokens: str) -> str:
    """Why a model call produced no usable ``what``: no plan, or no verdict."""
    return f"no {what}: {FAILURES.get(outcome, outcome)}{tokens}"


def describe_verification(verification: PlanVerification) -> str:
    """Which checks passed, or which failed and why."""
    total = len(ORDERED_PLAN_CHECKS)
    if verification.passed:
        return f"all {total} checks passed"
    failed = verification.failed_checks
    findings = "; ".join(verification.as_findings())
    return f"{len(failed)} of {total} checks failed: {findings}"


def describe_verdict(verdict: CriticVerdict, tokens: str) -> str:
    """What the reviewer said, criterion by criterion where it did not pass."""
    if verdict.accepted:
        return f"accepted on every criterion{tokens}"
    parts = []
    if verdict.failed:
        faults = "; ".join(f"{item.criterion}: {item.critique}" for item in verdict.failed)
        parts.append(f"faulted {faults}")
    if verdict.undecided:
        parts.append(
            "could not tell on " + ", ".join(str(item.criterion) for item in verdict.undecided)
        )
    if verdict.missing:
        parts.append("did not consider " + ", ".join(str(item) for item in verdict.missing))
    return "; ".join(parts) + tokens


def describe_outcome(outcome: str) -> str:
    """One sentence for the page about a run that ended without a plan to show."""
    return OUTCOMES.get(outcome, f"The run ended with {outcome}.")
