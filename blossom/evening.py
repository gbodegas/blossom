"""Whether a plan still fits the evening it was made for.

A plan is made for the evening as she had described it at the time: the full
evening, or a reduced one after she said today was too much. When her signal
changes after that, the plan on the page is not the plan the checks held to
the current budget. Both pages read this one rule and word it for their reader.
"""

from enum import StrEnum

from blossom.stores.drafts import DraftRecord
from blossom.stores.workload_signals import WorkloadSignalsStore


class Staleness(StrEnum):
    """Why a plan has stopped fitting its evening."""

    SIGNALED_SINCE = "signaled_since"
    """She said today is too much after the plan was made for the full evening."""
    SIGNAL_ENDED = "signal_ended"
    """The plan was made for a reduced evening and the signal is gone, taken
    back or past its week; the store does not say which."""


def staleness(signals: WorkloadSignalsStore, record: DraftRecord) -> Staleness | None:
    """How ``record`` fails to fit the evening as signaled now, or ``None`` while it fits."""
    signaled = bool(signals.for_evening(record.plan_date))
    if signaled == record.too_much:
        return None
    return Staleness.SIGNALED_SINCE if signaled else Staleness.SIGNAL_ENDED
