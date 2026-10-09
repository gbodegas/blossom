# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""What each node of the plan graph expected and found, kept as the record of a run.

A run of the plan graph is a few decisions in a row: what the week holds,
whether the plan passes the checks, whether the reviewer accepts it. The state
at the end says where the run landed but not how it got there, because a
passing check clears the findings that sent the plan back. A step record keeps
that: one line per node, saying what the node expected before it acted and
what it found, in words a parent can read on the family page.

The records are data about the run, not the run itself. Nothing reads them to
decide what happens next; the graph's edges do that from the typed values.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Final
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, TypeAdapter

from blossom.heuristic_relevance import Criterion, CriticVerdict
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


class StageTime(BaseModel):
    """How long one node of a run took, from the end of the node before it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: str
    round: int
    seconds: float


class RunTiming(BaseModel):
    """How long a run took and what it asked for, kept with the run's record.

    ``model_calls`` counts every request sent, each retry included, and ``retries``
    the retries alone. Output is counted in the tokens the service reported, which a
    stand-in model does not report. ``category`` names how a run without a plan
    failed, and is ``None`` for a run that made one or had nothing to plan.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    seconds: float
    stages: list[StageTime] = []
    model_calls: int = 0
    retries: int = 0
    output_tokens: int | None = None
    largest_output_tokens: int | None = None
    category: str | None = None
    generation_seconds: float | None = None
    """Seconds the plan graph ran, until its result or the deadline."""
    settle_seconds: float | None = None
    """Seconds settling took: the wait for the lock, held reviews and the transaction."""
    response_seconds: float | None = None
    """Seconds from the start of the run until its answer was decided."""
    unconfirmed: bool = False
    """Whether the answer could not confirm that the plan was saved."""


class PastDueWork(BaseModel):
    """One assignment a run found due before its evening, as the run's record keeps it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_id: str
    title: str
    course: str
    due_date: date


PAST_DUE_WORK: Final = TypeAdapter(tuple[PastDueWork, ...])
"""The past-due work a run keeps, as the JSON text its record holds."""


def past_due_work(
    assignments: Iterable[Assignment], past_due: Mapping[str, date]
) -> tuple[PastDueWork, ...]:
    """Each assignment ``past_due`` names that the run read, in ``past_due``'s order, by its
    id, title, course and the due date that passed: what the run's answer and record name."""
    read = {item.assignment_id: item for item in assignments}
    return tuple(
        PastDueWork(
            assignment_id=name, title=read[name].title, course=read[name].course, due_date=day
        )
        for name, day in past_due.items()
        if name in read
    )


EXPECT_RECORD_HOLDS = "the record's due dates hold against the school's sources"
EXPECT_ALL_CHECKS = "every tier-one check passes"
EXPECT_ACCEPTANCE = "the reviewer passes every criterion"
EXPECT_A_KEPT_PLAN = "the latest plan that passed every check goes to review"

KEPT_FOR_REVIEW: Final = (
    "The last revision broke a rule, so this is the latest version that passed every check."
)
"""What the step says when the last revision broke a rule and an earlier plan went on."""

FAILURES = {
    "model_truncated": "the answer was cut off",
    "model_refused": "the model declined to answer",
    "model_unparseable": "the answer couldn't be read",
    "timed_out": "the run's time ran out while it waited",
    "service_failed": "the planning service failed or didn't answer",
}

WHAT_CAME_BACK = {"plan": "plan", "verdict": "review"}

OUTCOMES = {
    "checks_failed": (
        "Every version Blossom wrote broke one of its rules, so there's no plan to review. "
        "Planning again may work, since each try writes a fresh plan."
    ),
    "model_truncated": (
        "An answer from the planning model was cut off, so there's no plan to review. "
        "Planning again may work."
    ),
    "model_refused": "The planning model declined to answer, so there's no plan to review.",
    "model_unparseable": (
        "An answer from the planning model couldn't be read, so there's no plan to review. "
        "Planning again may work."
    ),
    "interrupted": (
        "The run stopped before its plan could wait for review, so the plan was set aside."
    ),
    "nothing_to_schedule": "The run ended because nothing was left to schedule.",
    "date_problem": (
        "A due date on record comes before the evening being planned, so no plan could keep "
        "every rule. No model was asked. Check the dates named in the steps."
    ),
    "timed_out": (
        "Planning took longer than the time a run is allowed, so it stopped with no plan to review."
    ),
    "service_failed": (
        "The planning service failed or didn't answer, so there's no plan to review. "
        "Planning again may work."
    ),
    "overtaken": (
        "A newer plan for the evening was made while this run was working, so this one was "
        "set aside."
    ),
}
NOTHING_TO_SCHEDULE: Final = "nothing_to_schedule"
"""The outcome of a run whose window held no work still to plan when it was read: it
ends before any model is asked, with a record and no draft."""
DATE_PROBLEM: Final = "date_problem"
"""The outcome of a run whose window held work due before the evening it plans, which no
plan can schedule or put off in time: it ends before any model is asked, with a record."""

CRITERION_NAMES: Final = {
    Criterion.ORDER: "the order",
    Criterion.SIZING: "the block lengths",
    Criterion.DEFERRALS: "what was put off",
    Criterion.SUPPORT_RULES: "the standing rules",
    Criterion.RATIONALE: "the reasons",
}
"""Each criterion as a parent reads it."""

LABELS: Final = {
    "retrieve": "Read the week",
    "verify": "Rules check",
    "critique": "Reviewer",
    "rescue": "Plan kept for review",
    "time_limit": "Time limit",
}
SECONDS_SAID: Final = 10
"""Below this many seconds a time is said to the tenth of a second."""
ORDINALS: Final = {1: "First", 2: "Second", 3: "Third", 4: "Fourth", 5: "Fifth"}


def count(number: int, noun: str) -> str:
    """``1 block``, ``2 blocks``."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def joined(parts: Sequence[str]) -> str:
    """``a``, ``a and b``, ``a, b, and c``."""
    if len(parts) <= 2:
        return " and ".join(parts)
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def how_many_have(number: int, what: str) -> str:
    """``1 has a due date ...``, ``3 have a due date ...``."""
    return f"{number} {'has' if number == 1 else 'have'} {what}."


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
    budget: int,
    too_much: bool,
    done: int = 0,
) -> str:
    """The week in a few sentences: how much there is, how much is in doubt, and what the
    evening allows.

    ``done`` is how much of the week she has reported done, which is left out
    of ``assignments`` and said apart, so the record shows the work the plan
    was made from and the work it was not. Work with no due date on record is
    counted as that alone, whatever the sources say of it.
    """
    dated = {item.assignment_id for item in assignments if item.due_date is not None}
    contradicted = sum(item.contradicted for item in noticings if item.assignment_id in dated)
    uncertain = sum(
        label is not SourceConfidence.CORROBORATED
        for name, label in confidence.items()
        if name in dated
    )
    undated = sum(item.due_date is None for item in assignments)
    left_out = (
        f"; {done} she reported done {'was' if done == 1 else 'were'} left out" if done else ""
    )
    sentences = [f"{count(len(assignments), 'assignment')} to plan{left_out}."]
    if contradicted:
        sentences.append(
            how_many_have(contradicted, "a due date the school's sources don't support")
        )
    if uncertain:
        sentences.append(how_many_have(uncertain, "a due date that isn't confirmed"))
    if undated:
        sentences.append(how_many_have(undated, "no due date"))
    if rules or notes:
        sentences.append(
            f"{count(rules, 'standing rule')} and {count(notes, 'note')} about what has "
            "worked to follow."
        )
    sentences.append(
        f"She said today is too much, so the evening allows {budget} minutes."
        if too_much
        else f"The evening allows {budget} minutes."
    )
    return " ".join(sentences)


def describe_plan(plan: DailyPlan, zone: ZoneInfo, *, evening: date) -> str:
    """The shape of a plan: how many blocks, what it puts off, and how long it asks for,
    measured on the evening the run plans, whatever date the plan came back with."""
    parts = [count(len(plan.blocks), "block")]
    if plan.deferred:
        parts.append(f"{len(plan.deferred)} put off")
    return f"{' and '.join(parts)}, {plan.total_minutes(zone, on=evening)} minutes in all."


def describe_failure(outcome: str, what: str) -> str:
    """Why a model call produced no usable ``what``: no plan, or no verdict."""
    return f"No {WHAT_CAME_BACK[what]} came back: {FAILURES.get(outcome, outcome)}."


def describe_verification(verification: PlanVerification) -> str:
    """Which checks passed, or which failed and why."""
    total = len(ORDERED_PLAN_CHECKS)
    if verification.passed:
        return f"It kept all {total} rules."
    failed = verification.failed_checks
    findings = "; ".join(verification.as_plain())
    return f"It broke {len(failed)} of {total} rules: {findings}."


def named(criteria: Iterable[Criterion]) -> str:
    """Criteria as a parent reads them, in a list."""
    return joined([CRITERION_NAMES[criterion] for criterion in criteria])


def describe_verdict(verdict: CriticVerdict) -> str:
    """Which criteria the reviewer faulted, could not tell, or left out.

    The judgments are typed values; the reviewer's own words are not, so they
    stay in the draft's notes and out of the record.
    """
    if verdict.accepted:
        return "The reviewer found nothing to change."
    clauses = []
    if verdict.failed:
        clauses.append("found a problem with " + named(item.criterion for item in verdict.failed))
    if verdict.undecided:
        clauses.append("couldn't judge " + named(item.criterion for item in verdict.undecided))
    if verdict.missing:
        clauses.append("didn't consider " + named(verdict.missing))
    return f"The reviewer {joined(clauses)}."


def seconds_said(seconds: float) -> str:
    """``0.4 seconds``, ``1.0 second``, ``12 seconds``: a time as a parent reads it."""
    amount = f"{seconds:.1f}" if seconds < SECONDS_SAID else f"{round(seconds)}"
    return f"{amount} second" if amount in ("1", "1.0") else f"{amount} seconds"


def describe_timing(timing: RunTiming) -> str:
    """The run's time and requests in one sentence, for the family page."""
    parts = [
        f"Took {seconds_said(timing.seconds)} in all",
        count(timing.model_calls, "model request"),
    ]
    if timing.retries:
        parts[-1] += f" ({timing.retries} {'retry' if timing.retries == 1 else 'retries'})"
    if timing.output_tokens is not None:
        parts.append(tokens_said(timing.output_tokens))
    if timing.largest_output_tokens is not None:
        parts.append(f"the longest answer {tokens_said(timing.largest_output_tokens)}")
    return joined(parts) + "."


def tokens_said(number: int) -> str:
    """``1 output token``, ``2,400 output tokens``."""
    return f"{number:,} output token" if number == 1 else f"{number:,} output tokens"


def describe_past_due(names: Sequence[str], due: Sequence[date]) -> str:
    """Why a run ended before any model was asked: work due before the evening it plans,
    which a parent may set days ahead, so the date hasn't always passed."""
    said = [f"{name} is due {day}" for name, day in zip(names, due, strict=True)]
    return (
        f"{joined(said)}, before the evening being planned, so no plan can keep every rule; "
        "no model was asked."
    )


def describe_outcome(outcome: str) -> str:
    """Plain sentences for the family page about a run that ended without a plan to show."""
    return OUTCOMES.get(outcome, f"The run ended with {outcome}.")


def step_label(node: str, round_number: int) -> str:
    """What a step did, as a parent reads it: ``Read the week``, ``Second plan``."""
    if node == "plan":
        ordinal = ORDINALS.get(round_number)
        return f"{ordinal} plan" if ordinal else f"Plan {round_number}"
    return LABELS.get(node, node)


def step_sentence(found: str) -> str:
    """A found line as a sentence: trimmed, capitalized, and ended, whichever version wrote
    it. A blank line stays empty."""
    found = found.strip()
    if not found:
        return found
    sentence = found[0].upper() + found[1:]
    return sentence if sentence.endswith(".") else sentence + "."


def describe_last_check(steps: Sequence[StepRecord]) -> str | None:
    """What the last rules check of a run found, said on its own, or ``None`` when the run
    ended anywhere but a rules check or the check's line is blank."""
    if not steps or steps[-1].node != "verify":
        return None
    found = step_sentence(steps[-1].found)
    if not found:
        return None
    return f"In the last version, {found[0].lower()}{found[1:]}"
