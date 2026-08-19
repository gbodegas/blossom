from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class SourceChannel(StrEnum):
    LMS = "LMS"
    EMAIL = "EMAIL"
    PARENT_ENTRY = "PARENT_ENTRY"
    STUDENT_REPORT = "STUDENT_REPORT"


class SourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: SourceChannel
    asserted_value: str
    observed_at: datetime
    confidence: float


class Agreement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    records: list[SourceRecord]


class Disagreement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conflicting_claims: list[SourceRecord]


type ReconciliationResult = Agreement | Disagreement


class Reconciler:
    def reconcile(self, records: list[SourceRecord]) -> ReconciliationResult:
        if not records:
            msg = "at least one source record is required"
            raise ValueError(msg)
        values = {record.asserted_value for record in records}
        if len(values) == 1:
            return Agreement(value=records[0].asserted_value, records=records)
        return Disagreement(conflicting_claims=records)
