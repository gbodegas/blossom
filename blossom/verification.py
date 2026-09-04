"""Verification: the tier-one hard checks, kept separate from the other two tiers.

The tiers exist so the system cannot claim more confidence than its evidence
supports. A signal from the third tier must never change an answer produced by
the first.

Tier one is a deterministic hard check, pass or fail, and it runs before
anything is shown or proposed to another person (``HardCheck``). Tier two is a
heuristic score from a critic; it lives in ``blossom/heuristic_relevance.py``,
not here, so a score cannot be mistaken for a check. Tier three is whether a
plan is right for her. No automated check can answer that, so nothing here
tries; her workload signal settles it directly rather than being weighed
against anything the system computed.

``passed`` is a derived property, not a field, so no caller (workload signal,
principal, or anything added later) can flip a failed verification to passed.
A field that does not exist cannot be assigned.
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

    ``NOT_IMPLEMENTED`` is distinct from ``PASSED``: a check that has not been
    written has produced no evidence.
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
        """True only when every hard check ran and passed.

        A result missing any hard check does not pass.
        """
        if set(self.outcomes) != set(ORDERED_HARD_CHECKS):
            return False
        return all(outcome is CheckOutcome.PASSED for outcome in self.outcomes.values())

    @property
    def failed_checks(self) -> tuple[HardCheck, ...]:
        """Checks that ran and failed, in the order they are defined."""
        return tuple(
            check
            for check in ORDERED_HARD_CHECKS
            if self.outcomes.get(check) is CheckOutcome.FAILED
        )

    @property
    def unimplemented_checks(self) -> tuple[HardCheck, ...]:
        """Checks that produced no evidence, so a caller can see why this cannot pass."""
        return tuple(
            check
            for check in ORDERED_HARD_CHECKS
            if self.outcomes.get(check) is not CheckOutcome.PASSED
            and self.outcomes.get(check) is not CheckOutcome.FAILED
        )


class Verifier:
    """Runs the tier-one hard checks. It has no tier-two or tier-three role."""

    def verify_reconciled_fact(self, reconciliation: ReconciliationResult) -> VerificationResult:
        """Check a fact assembled from source records.

        ``POLICY_CONFORMANCE`` reports ``NOT_IMPLEMENTED`` until the
        drafts-and-approval rules exist, so this method cannot yet return a
        passing result.
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
