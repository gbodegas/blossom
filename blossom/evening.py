"""Whether a plan still fits the evening it was made for.

A plan is made for the evening as she had described it when the run read it:
the full evening, or a reduced one after she said today was too much. When her
signal as it stands is not that one, the plan on the page is not the plan the
checks held to the current budget. Both pages read this one rule and word it
for their reader, and neither says which came first, since a signal can change
while a run is still on its way to the draft.
"""

from enum import StrEnum

from blossom.noticing import planning_digest, read_week
from blossom.stores.drafts import DraftRecord
from blossom.stores.project_state import ProjectStateStore
from blossom.stores.workload_signals import WorkloadSignalsStore


class Staleness(StrEnum):
    """Why a plan has stopped fitting its evening."""

    SIGNALED_SINCE = "signaled_since"
    """She has said today is too much, and the plan was made for the full evening."""
    SIGNAL_ENDED = "signal_ended"
    """The plan was made for a reduced evening and the signal is gone, taken
    back or past its week; the store does not say which."""
    ASSIGNMENTS_CHANGED = "assignments_changed"
    """The assignments in the plan's window read differently from when the run
    read them: work added or taken away, a date, a kind, a note, or what a
    source says about a date."""


def staleness(
    signals: WorkloadSignalsStore,
    record: DraftRecord,
    project_state: ProjectStateStore | None = None,
) -> Staleness | None:
    """How ``record`` fails to fit the evening as it stands now, or ``None`` while it fits.

    Her signal is measured first, since it changes the budget the checks held
    the plan to. Then the window: with the record handed in, the week the
    run read for this evening is read again and its fingerprint compared to
    the one the draft carries; a draft from before plans carried one is not
    measured against the week.
    """
    signaled = bool(signals.for_evening(record.plan_date))
    if signaled != record.too_much:
        return Staleness.SIGNALED_SINCE if signaled else Staleness.SIGNAL_ENDED
    if project_state is not None and record.inputs_digest is not None:
        now = planning_digest(read_week(project_state, project_state, record.plan_date))
        if now != record.inputs_digest:
            return Staleness.ASSIGNMENTS_CHANGED
    return None
