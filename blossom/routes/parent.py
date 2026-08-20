"""Parent routes: a checkpoint, deliberately not a live feed.

A parent is a collaborator who sets goals, corrects information and reviews
drafts, which needs a periodic summary rather than continuous visibility. The
narrower shape is the design; widening it would turn collaboration into
monitoring.

Status: the handler returns a hardcoded literal. It reads no store and is not
connected to project state.

Missing, and specified: if the system notifies a parent that a deadline is at
risk, she must be able to see that the notification happened. A visibility
policy she can read but whose consequences she cannot observe is not visible.
Nothing here implements that yet.
"""

from datetime import UTC, datetime

from fastapi import APIRouter

from blossom.views import ParentCheckpointAssignmentView, ParentCheckpointView

router = APIRouter(prefix="/parent", tags=["parent"])


@router.get("/checkpoint", response_model=ParentCheckpointView)
def checkpoint() -> ParentCheckpointView:
    """Return the parent checkpoint. Currently a fixed placeholder response."""
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
