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


class NoSourceRecords(BaseModel):
    """Nothing was observed about this fact from any channel.

    This used to be a ``ValueError``. Raising was the wrong response: the
    design treats an absence of corroboration as a finding to surface, exactly
    like a disagreement, and an exception cannot be surfaced -- it removes the
    assignment it concerns and every other assignment on the page along with
    it. Making the reconciler total means the caller has to decide what to show
    rather than being handed a crash.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = "no source records were observed for this fact"


type ReconciliationResult = Agreement | Disagreement | NoSourceRecords


class SourceConfidence(StrEnum):
    """How well corroborated a reconciled fact is.

    Four states rather than a verified/unverified boolean, because the design
    draws a distinction a boolean cannot carry: a due date two channels agree
    on is not the same claim as the identical date asserted by one channel
    alone. Single-channel observations are meant to be raised as questions
    rather than presented as settled, so they need to stay distinguishable all
    the way to the view.
    """

    CORROBORATED = "CORROBORATED"
    SINGLE_SOURCE = "SINGLE_SOURCE"
    SOURCES_DISAGREE = "SOURCES_DISAGREE"
    UNVERIFIED = "UNVERIFIED"


def classify_confidence(result: ReconciliationResult) -> SourceConfidence:
    """Map a reconciliation outcome onto how much the family should trust it."""
    if isinstance(result, NoSourceRecords):
        return SourceConfidence.UNVERIFIED
    if isinstance(result, Disagreement):
        return SourceConfidence.SOURCES_DISAGREE
    if len(result.records) == 1:
        return SourceConfidence.SINGLE_SOURCE
    return SourceConfidence.CORROBORATED


class Reconciler:
    def reconcile(self, records: list[SourceRecord]) -> ReconciliationResult:
        """Combine source records for one fact without ever choosing a winner."""
        if not records:
            return NoSourceRecords()
        values = {record.asserted_value for record in records}
        if len(values) == 1:
            return Agreement(value=records[0].asserted_value, records=records)
        return Disagreement(conflicting_claims=records)
