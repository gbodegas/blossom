from datetime import UTC, datetime

from fastapi import APIRouter

from blossom.views import ParentCheckpointAssignmentView, ParentCheckpointView

router = APIRouter(prefix="/parent", tags=["parent"])


@router.get("/checkpoint", response_model=ParentCheckpointView)
def checkpoint() -> ParentCheckpointView:
    return ParentCheckpointView(
        checkpoint_at=datetime.now(UTC),
        assignments=[
            ParentCheckpointAssignmentView(
                course="World History",
                title="Canal Era comparison essay",
                aggregate_status="in_progress",
                has_schedule_conflict=True,
            )
        ],
    )
