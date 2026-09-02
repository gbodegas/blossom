"""Parent routes: a checkpoint view, not a live feed.

A parent is a collaborator who sets goals, corrects information and reviews
drafts, so the route exposes a periodic summary rather than continuous
visibility. A live feed would turn collaboration into monitoring.

The handler returns a hardcoded placeholder. It reads no store and is not
connected to project state.

Not yet implemented: when the system notifies a parent that a deadline is at
risk, the parent needs to be able to see that the notification happened.
Without that, the visibility policy is stated but not observable.
"""

from datetime import UTC, datetime

from fastapi import APIRouter

from blossom.views import ParentCheckpointAssignmentView, ParentCheckpointView

router = APIRouter(prefix="/parent", tags=["parent"])


@router.get("/checkpoint", response_model=ParentCheckpointView)
def checkpoint() -> ParentCheckpointView:
    """Return the parent checkpoint as a fixed placeholder response."""
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
