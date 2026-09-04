"""Assembling one fact from several channels that routinely disagree.

Assignment state is assembled from a school platform, email notifications, a
parent typing something in, and the student's own account, and any of those
channels may be unavailable or in conflict. Nothing here picks a winner: a
conflict is reported as a ``Disagreement`` that keeps every claim with the
channel that made it, because the disagreement is the information the family
needs. ``NoSourceRecords`` reports absence as a result rather than an
exception, so one uncorroborated fact does not remove the rest of the page with
it, and the caller decides what to show.

``SourceRecord.observed_at`` is recorded but nothing reads it. Neither
staleness (accurate when observed, since changed) nor validity (accurate but
not supporting the conclusion drawn from it) is modeled here.
"""

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict


class SourceChannel(StrEnum):
    """The channel a claim came from."""

    LMS = "LMS"
    EMAIL = "EMAIL"
    PARENT_ENTRY = "PARENT_ENTRY"
    STUDENT_REPORT = "STUDENT_REPORT"


class SourceRecord(BaseModel):
    """One channel's claim about one fact, at one moment.

    ``confidence`` is the channel's own reported certainty. It is not used to
    resolve a disagreement.
    """

    model_config = ConfigDict(extra="forbid")

    channel: SourceChannel
    asserted_value: str
    observed_at: AwareDatetime
    confidence: float


class Agreement(BaseModel):
    """Every channel asserted the same value.

    The records are kept so the caller can tell one agreeing channel from
    several.
    """

    model_config = ConfigDict(extra="forbid")

    value: str
    records: list[SourceRecord]


class Disagreement(BaseModel):
    """Channels asserted different values, and none of them has been chosen."""

    model_config = ConfigDict(extra="forbid")

    conflicting_claims: list[SourceRecord]


class NoSourceRecords(BaseModel):
    """No channel reported anything about this fact.

    Returned as a result rather than raised, so the caller decides what to show
    instead of losing the whole page.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = "no source records were observed for this fact"


type ReconciliationResult = Agreement | Disagreement | NoSourceRecords


class SourceConfidence(StrEnum):
    """How well corroborated a reconciled fact is.

    Four states rather than a boolean so a single-channel value stays distinct
    from a corroborated one all the way to the view, where it is presented as a
    question rather than as settled.
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
    """Combines source records for one fact. Total: it never raises."""

    def reconcile(self, records: list[SourceRecord]) -> ReconciliationResult:
        """Combine source records for one fact without ever choosing a winner."""
        if not records:
            return NoSourceRecords()
        values = {record.asserted_value for record in records}
        if len(values) == 1:
            return Agreement(value=records[0].asserted_value, records=records)
        return Disagreement(conflicting_claims=records)
