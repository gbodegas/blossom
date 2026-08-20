"""Verifier routes: what was claimed, and what backs it.

The verifier is a checking layer between generation and anything that leaves
the system, not a person. This view exists so the basis of a claim -- which
channels asserted it, which policy applies, how it was checked -- can be
inspected rather than inferred from the output.

Status: the handler returns a hardcoded literal and is not connected to
``blossom.verification``.
"""

from fastapi import APIRouter

from blossom.views import VerifierClaimView

router = APIRouter(prefix="/verifier", tags=["verifier"])


@router.get("/claims", response_model=list[VerifierClaimView])
def claims() -> list[VerifierClaimView]:
    """Return claims and their verification basis. Currently a fixed placeholder."""
    return [
        VerifierClaimView(
            claim_id="claim-deadline-canal-essay",
            factual_claim="Canal Era comparison essay has conflicting deadline records.",
            policy_basis="Never silently resolve source conflicts.",
            source_channels=["LMS", "PARENT_ENTRY"],
            verification_status="passed_with_disagreement",
        )
    ]
