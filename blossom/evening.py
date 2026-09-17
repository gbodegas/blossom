"""Whether a plan still fits the evening it was made for.

A plan is made for the evening as she had described it when the run read it:
the full evening, or a reduced one after she said today was too much. When her
signal as it stands is not that one, the plan on the page is not the plan the
checks held to the current budget. Both pages read this one rule and word it
for their reader, and neither says which came first, since a signal can change
while a run is still on its way to the draft.
"""

from dataclasses import dataclass
from enum import StrEnum

from blossom.assignment_status import statuses_for
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
    """The work in the plan's window reads differently from when the run read
    it: work added or taken away, a date, a kind, a note, what a source says
    about a date, or what she has reported about her part."""


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


NOT_ON_RECORD = "an assignment that is not on record"
"""What a notice calls work a plan speaks about whose row cannot be found: never a guess at
another assignment by its title."""


@dataclass(frozen=True)
class ReportedDone:
    """Work a plan speaks about that she has since reported done."""

    titles: tuple[str, ...]
    """The assignments named, by title, in the plan's order, when the plan carries the
    ids it speaks about."""
    known: bool
    """Whether the plan carries those ids. A plan from before plans carried them cannot
    name the work; it is told only that something in its window is reported done."""


def reported_done(project_state: ProjectStateStore, record: DraftRecord) -> ReportedDone | None:
    """Work in ``record``'s plan she has reported done since, or ``None`` while there is none.

    A plan is made from the work still to do when the run read it, and says
    nothing about what she had reported done by then; what it speaks about
    and she reports done afterward is what a page names. A plan that carries
    no ids is measured against its window instead, and named nothing. Both
    pages read this one rule and word it for their reader; neither judges
    whether the plan should be made again, which is hers to decide.
    """
    with project_state.exclusively():
        if record.plan_assignment_ids is None:
            week = read_week(project_state, project_state, record.plan_date)
            return ReportedDone(titles=(), known=False) if week.done_ids() else None
        if not record.plan_assignment_ids:
            return None
        statuses = statuses_for(project_state, record.plan_assignment_ids)
        titles = {item.assignment_id: item.title for item in project_state.all_assignments()}
    named = tuple(
        titles.get(name, NOT_ON_RECORD)
        for name in record.plan_assignment_ids
        if not statuses[name].needs_homework
    )
    return ReportedDone(titles=named, known=True) if named else None
