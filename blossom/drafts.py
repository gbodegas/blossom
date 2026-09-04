"""Drafts: the only form in which work leaves the agent.

Everything outbound terminates here. A draft is text for a human to read and
transmit by hand, which is why no field on it records a recipient, a channel, or
a send time. There is nothing to record because there is no sending path.

``blossom.agent.gates`` sets ``DraftStatus.APPROVED_FOR_MANUAL_SEND`` when a
person approves at the gate. Nothing stores a draft outside the graph's
saved state; the persistence a review queue would need is not built.
"""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class DraftStatus(StrEnum):
    """Where a draft sits in review."""

    DRAFT = "DRAFT"
    APPROVED_FOR_MANUAL_SEND = "APPROVED_FOR_MANUAL_SEND"


class Draft(BaseModel):
    """Text prepared for a human to transmit."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(default_factory=lambda: str(uuid4()))
    body: str
    status: DraftStatus = DraftStatus.DRAFT
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
