"""Tier two: a critic's judgment about a plan, kept out of the verifier.

A hard check in ``blossom/verification.py`` and ``blossom/plan_checks.py`` is
deterministic and settles a question. What lives here is an estimate: whether
the order of an evening makes sense, whether an hour is the right size for
that essay, whether the reason given for deferring something holds up. A
judgment that shares a module with a check eventually gets read as one.

The verdict has a shape rather than a score. A number between zero and one
invites a threshold, and a threshold turns an opinion into a gate; the tiers
exist to stop exactly that. So the critic reports one finding per criterion,
each with the reasoning written before the verdict, and the overall result is
derived from them rather than asserted.

The criteria are a closed list, and the type carries it. The schema the model
fills in admits only those five, the prompt that asks for them is rendered
from the same mapping, and a verdict that leaves one out is not an acceptance,
however the ones it did report came out. A plan judged on four criteria was
partly reviewed, and the person at the gate is told which one was skipped.

``CANNOT_TELL`` is a first-class answer. A critic that must choose between
pass and fail will invent a reason to do so, and a plan it cannot judge is
worth surfacing to a person rather than resolving. It does not fail a plan,
and it does not pass one either.

Whether the plan is right for her is tier three, and nothing here reaches it.
Her workload signal settles that directly.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class Criterion(StrEnum):
    """What the critic judges. Every one is reported in every verdict."""

    ORDER = "order"
    SIZING = "sizing"
    DEFERRALS = "deferrals"
    SUPPORT_RULES = "support rules"
    RATIONALE = "rationale"


CRITERIA: Final[Mapping[Criterion, str]] = {
    Criterion.ORDER: (
        "does the sequence make sense for a tired thirteen-year-old, with the "
        "hardest or most uncertain work while attention is best?"
    ),
    Criterion.SIZING: (
        "is each block about the right length for the work it names, given what the assignment is?"
    ),
    Criterion.DEFERRALS: (
        "does each reason for putting something off hold up against its due date "
        "and its confidence label?"
    ),
    Criterion.SUPPORT_RULES: "does the plan follow every standing rule about how she works?",
    Criterion.RATIONALE: (
        "would she recognize each sentence as true, and is it written to her rather than about her?"
    ),
}
"""The question each criterion asks, in the order the critic considers them.

The critic's prompt is rendered from this mapping, so the critic is asked
exactly what the verdict is then checked against, and the two cannot drift."""


class Judgment(StrEnum):
    """What a critic can say about one criterion."""

    PASSES = "PASSES"
    FAILS = "FAILS"
    CANNOT_TELL = "CANNOT_TELL"


class CriterionFinding(BaseModel):
    """One criterion, the reasoning, and the judgment it led to.

    The critique is a field rather than a comment because it is read by a
    person at the gate, and because a verdict written before its reasoning is
    a verdict looking for support.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion: Criterion
    critique: str = Field(
        description="What the plan does about it, and why that reads well or badly."
    )
    judgment: Judgment


class CriticVerdict(BaseModel):
    """Everything a critic concluded about one plan.

    Nothing here can be set to "the plan is fine": the overall result is read
    off the findings, the same way a hard check's is. A critic that wanted to
    approve a plan it had faulted would have to change a finding, and one that
    wanted to approve a plan it had not fully judged would have to invent the
    missing findings; both are visible.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    findings: list[CriterionFinding] = Field(
        description="One entry per criterion, every criterion, in the order considered."
    )

    @property
    def covered(self) -> frozenset[Criterion]:
        """The criteria the critic reported on, whatever it concluded about them."""
        return frozenset(finding.criterion for finding in self.findings)

    @property
    def missing(self) -> tuple[Criterion, ...]:
        """Criteria the critic did not report on, in the order they are asked.

        A repeated criterion does not stand in for an absent one; coverage is
        by criterion, not by count.
        """
        return tuple(criterion for criterion in CRITERIA if criterion not in self.covered)

    @property
    def failed(self) -> tuple[CriterionFinding, ...]:
        """Findings the critic judged against the plan."""
        return tuple(finding for finding in self.findings if finding.judgment is Judgment.FAILS)

    @property
    def undecided(self) -> tuple[CriterionFinding, ...]:
        """Findings the critic could not settle, which are for a person to read."""
        return tuple(
            finding for finding in self.findings if finding.judgment is Judgment.CANNOT_TELL
        )

    @property
    def accepted(self) -> bool:
        """True only when every criterion was reported and every one passed.

        An empty verdict is not an acceptance, and neither is a partial one. A
        critic that returned nothing judged nothing; one that skipped a
        criterion judged less than it was asked to.
        """
        return not self.missing and not self.failed and not self.undecided
