"""Assembling one fact from several channels that routinely disagree.

Assignment state is not retrieved from an authoritative record. It is assembled
from a school platform, email notifications, a parent typing something in, and
the student's own account. Any channel may be unavailable, and when several are
available they may conflict.

The rule this module enforces is that a conflict is a finding, never a tie to
break. Nothing here picks a winner. ``Disagreement`` keeps every claim with the
channel that made it, because the disagreement is precisely the information the
family needs and precisely what disappears when a system quietly chooses one
source and discards the other.

``NoSourceRecords`` is the same principle applied to absence. It used to be a
``ValueError``, which meant an assignment nothing corroborated could not be
reported at all -- the exception removed that assignment and every other one on
the page along with it.

One distinction this module does not yet make. A record can be stale, meaning
it was accurate when observed and the situation has since changed, or it can be
invalid, meaning it is accurate but does not support the conclusion drawn from
it. A submission flag confirms a file was uploaded; it does not confirm the
work was finished or the right file was sent. Staleness is answered by
observing again. Validity is not, and observing the same flag repeatedly
provides no evidence about what it means. ``SourceRecord`` carries
``observed_at`` so staleness can eventually be reasoned about; nothing reasons
about it today, and validity is unmodelled.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class SourceChannel(StrEnum):
    """Where a claim came from. Never dropped, never ranked into a winner."""

    LMS = "LMS"
    EMAIL = "EMAIL"
    PARENT_ENTRY = "PARENT_ENTRY"
    STUDENT_REPORT = "STUDENT_REPORT"


class SourceRecord(BaseModel):
    """One channel's claim about one fact, at one moment.

    ``confidence`` is the channel's own reported certainty. It is deliberately
    not used to resolve a disagreement, because resolving silently is the
    behaviour this module exists to prevent.
    """

    model_config = ConfigDict(extra="forbid")

    channel: SourceChannel
    asserted_value: str
    observed_at: datetime
    confidence: float


class Agreement(BaseModel):
    """Every channel that spoke asserted the same value.

    The contributing records are kept rather than collapsed to the value, so a
    reader can still see whether one channel agreed or four did.
    """

    model_config = ConfigDict(extra="forbid")

    value: str
    records: list[SourceRecord]


class Disagreement(BaseModel):
    """Channels asserted different values, and none of them has been chosen."""

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
    """Combines source records for one fact. Total: it never raises."""

    def reconcile(self, records: list[SourceRecord]) -> ReconciliationResult:
        """Combine source records for one fact without ever choosing a winner."""
        if not records:
            return NoSourceRecords()
        values = {record.asserted_value for record in records}
        if len(values) == 1:
            return Agreement(value=records[0].asserted_value, records=records)
        return Disagreement(conflicting_claims=records)
