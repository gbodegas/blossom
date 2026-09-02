"""Tests that the three verification tiers stay separate and tier one stays unfalsifiable.

The workload signal is a tier-three judgment about whether a plan suits her.
Tier one is a deterministic check about whether a claim is corroborated. A
tier-three input must never change a tier-one answer, so the Verifier exposes
only the hard check and `passed` is derived from the outcomes, never assigned.
Partial evidence and unimplemented checks do not count as a pass.

These tests pin the Verifier surface and the derived pass/fail so no input can
override a hard check.
"""

import pytest
from pydantic import ValidationError

from blossom.reconciliation import (
    Agreement,
    Disagreement,
    NoSourceRecords,
    SourceChannel,
    SourceRecord,
)
from blossom.verification import (
    ORDERED_HARD_CHECKS,
    CheckOutcome,
    HardCheck,
    VerificationResult,
    VerificationTier,
    Verifier,
)
from tests.support import record


def all_passing() -> dict[HardCheck, CheckOutcome]:
    return dict.fromkeys(ORDERED_HARD_CHECKS, CheckOutcome.PASSED)


def test_the_three_tiers_are_the_three_the_design_names() -> None:
    """Tier one is a check, tier two is a score, tier three is hers."""
    assert list(VerificationTier) == [
        VerificationTier.HARD_CHECK,
        VerificationTier.HEURISTIC_SCORE,
        VerificationTier.HER_JUDGMENT,
    ]


def test_the_verifier_exposes_no_tier_two_or_tier_three_entry_point() -> None:
    """The Verifier exposes only the hard check; scoring and judgment live elsewhere."""
    surface = {name for name in dir(Verifier) if not name.startswith("_")}

    assert surface == {"verify_reconciled_fact"}


def test_passed_is_derived_and_cannot_be_assigned() -> None:
    """`passed` is derived from the outcomes; there is no field to set."""
    assert "passed" not in VerificationResult.model_fields

    with pytest.raises(ValidationError):
        VerificationResult(outcomes=all_passing(), passed=True)  # type: ignore[call-arg]


def test_a_passing_result_cannot_be_mutated_into_a_failing_one_or_back() -> None:
    result = VerificationResult(outcomes=all_passing())
    assert result.passed is True

    with pytest.raises(ValidationError):
        result.outcomes = {}


def test_a_result_missing_a_check_does_not_pass() -> None:
    """Partial evidence is not a weaker yes."""
    partial = VerificationResult(
        outcomes={HardCheck.SOURCE_PRESENT: CheckOutcome.PASSED}
    )

    assert partial.passed is False


def test_an_unimplemented_check_does_not_count_as_a_pass() -> None:
    result = VerificationResult(
        outcomes={
            HardCheck.SOURCE_PRESENT: CheckOutcome.PASSED,
            HardCheck.FACTUAL_CONSISTENCY: CheckOutcome.PASSED,
            HardCheck.POLICY_CONFORMANCE: CheckOutcome.NOT_IMPLEMENTED,
        }
    )

    assert result.passed is False
    assert result.unimplemented_checks == (HardCheck.POLICY_CONFORMANCE,)


def test_agreeing_sources_pass_the_two_implemented_checks() -> None:
    lms = record(SourceChannel.LMS, "2026-08-21")
    student = record(SourceChannel.STUDENT_REPORT, "2026-08-21")

    result = Verifier().verify_reconciled_fact(
        Agreement(value="2026-08-21", records=[lms, student])
    )

    assert result.outcomes[HardCheck.SOURCE_PRESENT] is CheckOutcome.PASSED
    assert result.outcomes[HardCheck.FACTUAL_CONSISTENCY] is CheckOutcome.PASSED
    assert result.failed_checks == ()


def test_disagreeing_sources_fail_the_consistency_check_but_not_the_presence_check() -> None:
    lms = record(SourceChannel.LMS, "2026-08-21")
    parent = record(SourceChannel.PARENT_ENTRY, "2026-08-22")

    result = Verifier().verify_reconciled_fact(Disagreement(conflicting_claims=[lms, parent]))

    assert result.outcomes[HardCheck.SOURCE_PRESENT] is CheckOutcome.PASSED
    assert result.failed_checks == (HardCheck.FACTUAL_CONSISTENCY,)


def test_a_fact_with_no_sources_fails_both_implemented_checks() -> None:
    result = Verifier().verify_reconciled_fact(NoSourceRecords())

    assert result.failed_checks == (
        HardCheck.SOURCE_PRESENT,
        HardCheck.FACTUAL_CONSISTENCY,
    )
    assert result.passed is False


def test_nothing_in_the_package_reintroduces_a_verification_override() -> None:
    """No module in the package defines an override, bypass, or force_pass method."""
    import pathlib
    import re

    pattern = re.compile(r"def\s+\w*(override|bypass|force_pass)\w*\s*\(", re.IGNORECASE)
    for path in pathlib.Path("blossom").rglob("*.py"):
        content = path.read_text(encoding="utf-8")
        assert not pattern.search(content), f"{path} defines an override-shaped method"


def test_source_record_is_still_required_to_carry_provenance() -> None:
    """Tier one depends on every source record carrying channel, value, time, and confidence."""
    assert {"channel", "asserted_value", "observed_at", "confidence"} <= set(
        SourceRecord.model_fields
    )
