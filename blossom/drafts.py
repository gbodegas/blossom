from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class DraftStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED_FOR_MANUAL_SEND = "APPROVED_FOR_MANUAL_SEND"


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(default_factory=lambda: str(uuid4()))
    body: str
    status: DraftStatus = DraftStatus.DRAFT
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
