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

``CANNOT_TELL`` is a first-class answer. A critic that must choose between
pass and fail will invent a reason to do so, and a plan it cannot judge is
worth surfacing to a person rather than resolving. It does not fail a plan,
and it does not pass one either.

Whether the plan is right for her is tier three, and nothing here reaches it.
Her workload signal settles that directly.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


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

    criterion: str = Field(description="What was being judged, in a few words.")
    critique: str = Field(
        description="What the plan does about it, and why that reads well or badly."
    )
    judgment: Judgment


class CriticVerdict(BaseModel):
    """Everything a critic concluded about one plan.

    Nothing here can be set to "the plan is fine": the overall result is read
    off the findings, the same way a hard check's is. A critic that wanted to
    approve a plan it had faulted would have to change a finding, which is
    visible.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    findings: list[CriterionFinding] = Field(
        description="One entry per criterion considered, in the order considered."
    )

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
        """True only when every criterion was considered and every one passed.

        An empty verdict is not an acceptance. A critic that returned nothing
        judged nothing.
        """
        return bool(self.findings) and not self.failed and not self.undecided
