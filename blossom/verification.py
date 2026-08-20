"""Verification, separated into the three tiers the design actually distinguishes.

The previous version of this module named three things "tiers" that are all
the same tier. ``SOURCE_PRESENT``, ``FACTUAL_CONSISTENCY`` and
``POLICY_CONFORMANCE`` are three deterministic hard checks; they are tier one,
three times over. Nothing in the module represented tier two or tier three at
all.

That mislabelling is what made the workload override look reasonable. If tier
three is just the last item in a list of checks, then a tier-three input that
sets ``passed = True`` reads like an ordinary escalation rule. In the design it
is nothing of the kind: the tiers exist precisely so the system cannot claim
more confidence than its evidence supports, and a signal from the third tier
must never be able to change an answer produced by the first.

The three tiers:

Tier one is a hard check. Deterministic, with a clear pass or fail, and it runs
before anything is shown or proposed to another person. Does a value match the
source it came from? Do two deadlines conflict? Did an action that requires
human approval actually stop at the gate? These live here, in ``HardCheck``.

Tier two is a heuristic score produced by a critic: whether a notification is
worth an interruption, whether one candidate plan reads better than another.
Useful, but an estimate rather than a verified conclusion. It deliberately does
not live in this module -- see ``blossom/heuristic_relevance.py`` -- so that a
score can never be mistaken for a check.

Tier three is whether a plan is actually right for her. No automated check can
answer that, and this module does not attempt to. Her workload signal settles
it directly rather than being weighed against anything the system computed. The
absence of a tier-three implementation here is the design, not a gap.

One structural note. ``passed`` is a derived property, not a field. There is no
attribute to assign, so no caller -- workload signal, principal, or anything
added later -- can flip a failed verification to passed. The project already
argues that a tool which was never built cannot be called; the same reasoning
applies to a field that does not exist.
"""

from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict

from blossom.reconciliation import Agreement, Disagreement, ReconciliationResult


class VerificationTier(IntEnum):
    """The three kinds of confidence the design keeps separate."""

    HARD_CHECK = 1
    """Deterministic, pass or fail. Implemented here as ``HardCheck``."""

    HEURISTIC_SCORE = 2
    """A critic's estimate. Lives in ``blossom/heuristic_relevance.py``, not here."""

    HER_JUDGMENT = 3
    """Whether the plan is right for her. Not automatable, and not implemented anywhere."""


class HardCheck(StrEnum):
    """The tier-one checks. Every one must pass for a verification to pass."""

    SOURCE_PRESENT = "SOURCE_PRESENT"
    """At least one channel asserted this fact."""

    FACTUAL_CONSISTENCY = "FACTUAL_CONSISTENCY"
    """The channels that asserted it agree on the value."""

    POLICY_CONFORMANCE = "POLICY_CONFORMANCE"
    """The claim carries nothing that must not leave the family, and any action
    requiring human approval stopped at its gate."""


ORDERED_HARD_CHECKS: tuple[HardCheck, ...] = (
    HardCheck.SOURCE_PRESENT,
    HardCheck.FACTUAL_CONSISTENCY,
    HardCheck.POLICY_CONFORMANCE,
)


class CheckOutcome(StrEnum):
    """The result of one hard check.

    ``NOT_IMPLEMENTED`` is a distinct outcome rather than an optimistic pass.
    A check that has not been written has produced no evidence, and recording
    that as a pass would be the module claiming confidence it has not earned --
    the exact failure the tier separation exists to prevent.
    """

    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class VerificationResult(BaseModel):
    """The outcome of running the tier-one checks over one claim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcomes: dict[HardCheck, CheckOutcome]

    @property
    def passed(self) -> bool:
        """True only when every hard check ran and every one of them passed.

        A result missing a check does not pass. Partial evidence is not a
        weaker yes; it is not a yes.
        """
        if set(self.outcomes) != set(ORDERED_HARD_CHECKS):
            return False
        return all(outcome is CheckOutcome.PASSED for outcome in self.outcomes.values())

    @property
    def failed_checks(self) -> tuple[HardCheck, ...]:
        """Checks that ran and failed, in the order they are defined."""
        return tuple(
            check for check in ORDERED_HARD_CHECKS
            if self.outcomes.get(check) is CheckOutcome.FAILED
        )

    @property
    def unimplemented_checks(self) -> tuple[HardCheck, ...]:
        """Checks that produced no evidence, so a caller can see why this cannot pass."""
        return tuple(
            check for check in ORDERED_HARD_CHECKS
            if self.outcomes.get(check) is not CheckOutcome.PASSED
            and self.outcomes.get(check) is not CheckOutcome.FAILED
        )


class Verifier:
    """Runs the tier-one hard checks. It has no tier-two or tier-three role."""

    def verify_reconciled_fact(self, reconciliation: ReconciliationResult) -> VerificationResult:
        """Check a fact assembled from source records.

        ``POLICY_CONFORMANCE`` reports ``NOT_IMPLEMENTED``, which means this
        method cannot currently return a passing result. That is deliberate and
        honest: the policy check needs the drafts-and-approval rules, which do
        not exist yet, and reporting an unwritten check as a pass would let a
        caller act on confidence nothing established.
        """
        return VerificationResult(
            outcomes={
                HardCheck.SOURCE_PRESENT: (
                    CheckOutcome.PASSED
                    if isinstance(reconciliation, Agreement | Disagreement)
                    else CheckOutcome.FAILED
                ),
                HardCheck.FACTUAL_CONSISTENCY: (
                    CheckOutcome.PASSED
                    if isinstance(reconciliation, Agreement)
                    else CheckOutcome.FAILED
                ),
                HardCheck.POLICY_CONFORMANCE: CheckOutcome.NOT_IMPLEMENTED,
            }
        )
