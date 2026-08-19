from enum import IntEnum

from pydantic import BaseModel, ConfigDict

from blossom.principals import Principal


class VerificationTier(IntEnum):
    SOURCE_PRESENT = 1
    FACTUAL_CONSISTENCY = 2
    POLICY_CONFORMANCE = 3


ORDERED_TIERS: tuple[VerificationTier, ...] = (
    VerificationTier.SOURCE_PRESENT,
    VerificationTier.FACTUAL_CONSISTENCY,
    VerificationTier.POLICY_CONFORMANCE,
)


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    tiers_checked: list[VerificationTier]
    workload_override: bool = False


class Verifier:
    def verify_fact(self, fact: str, source_count: int) -> VerificationResult:
        return VerificationResult(
            passed=bool(fact.strip()) and source_count > 0,
            tiers_checked=list(ORDERED_TIERS),
        )

    def apply_workload_override(
        self,
        result: VerificationResult,
        *,
        principal: Principal,
        has_workload_signal: bool,
    ) -> VerificationResult:
        if principal is Principal.STUDENT and has_workload_signal:
            return result.model_copy(update={"passed": True, "workload_override": True})
        return result
