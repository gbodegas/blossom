"""Drafts: the only form in which work leaves the agent.

Everything outbound terminates here. A draft is text for a human to read and
transmit by hand, which is why no field on it records a recipient, a channel or
a send time -- there is nothing to record, because there is no sending path.

Known gap: ``DraftStatus.APPROVED_FOR_MANUAL_SEND`` is never set by any code,
and nothing stores a draft once ``blossom.tools.create_draft`` returns one. The
approval step it names does not exist yet.
"""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class DraftStatus(StrEnum):
    """Where a draft sits in review.

    ``APPROVED_FOR_MANUAL_SEND`` records that a human approved the text for
    them to send themselves. It is never set by any current code path.
    """

    DRAFT = "DRAFT"
    APPROVED_FOR_MANUAL_SEND = "APPROVED_FOR_MANUAL_SEND"


class Draft(BaseModel):
    """Text prepared for a human to transmit.

    There is no recipient, channel or sent-at field, because there is nothing
    to record: no code path sends anything.
    """

    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(default_factory=lambda: str(uuid4()))
    body: str
    status: DraftStatus = DraftStatus.DRAFT
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
