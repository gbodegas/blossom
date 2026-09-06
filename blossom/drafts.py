"""Drafts: the only form in which work leaves the agent.

Everything outbound terminates here. A draft is text for a human to read and
transmit by hand, which is why no field on it records a recipient, a channel, or
a send time. There is nothing to record because there is no sending path.

``blossom.agent.gates`` sets ``DraftStatus.APPROVED_FOR_MANUAL_SEND`` when a
person approves at the gate, and ``blossom.stores.drafts`` keeps the record of
every draft and every decision across threads.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Decision = Literal["approved", "rejected", "expired", "superseded"]
"""What a person can say about a draft, approved or rejected, and the two things
the system records itself: expired, when nobody said either within the
retention window, and superseded, when a later draft for the same evening took
its place. No value means half approved: neither an expired nor a superseded
draft was ever approved, and neither was ever reviewed."""


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
