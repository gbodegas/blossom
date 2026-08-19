from fastapi import APIRouter

from blossom.views import VerifierClaimView

router = APIRouter(prefix="/verifier", tags=["verifier"])


@router.get("/claims", response_model=list[VerifierClaimView])
def claims() -> list[VerifierClaimView]:
    return [
        VerifierClaimView(
            claim_id="claim-deadline-canal-essay",
            factual_claim="Canal Era comparison essay has conflicting deadline records.",
            policy_basis="Never silently resolve source conflicts.",
            source_channels=["LMS", "PARENT_ENTRY"],
            verification_status="passed_with_disagreement",
        )
    ]
