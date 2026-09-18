"""Whether a plan still fits the evening it was made for.

A plan is made for the evening as she had described it when the run read it:
the full evening, or a reduced one after she said today was too much. When her
signal as it stands is not that one, the plan on the page is not the plan the
checks held to the current budget. Both pages read this one rule and word it
for their reader, and neither says which came first, since a signal can change
while a run is still on its way to the draft.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from blossom.assignment_status import AssignmentStatus, statuses_for
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

    named: tuple[tuple[str, str], ...]
    """The assignments, each as its id and its title, in the order the draft keeps their
    ids, which is sorted, when the plan carries the ids it speaks about. The id is what
    tells two assignments with one title apart, so the pages show it with the title."""
    known: bool
    """Whether the plan carries those ids. A plan from before plans carried them cannot
    name the work; it is told only that something in its window is reported done."""


@dataclass(frozen=True)
class PlanUpdates:
    """What stands now about the work one saved plan speaks about, from one reading of the
    record: what a page says above the plan and what it shows beside the plan's rows come
    from here, so the two never disagree."""

    done: ReportedDone | None
    """What the notice above the plan says, or ``None`` while it has nothing to say."""
    statuses: Mapping[str, AssignmentStatus]
    """What she and the school have said about each assignment the plan carries by id;
    empty for a plan from before plans carried their ids."""
    on_record: frozenset[str]
    """Every assignment id on record at that reading, so a row whose assignment is gone
    gets no link, and no other assignment is ever put in its place."""


def plan_updates(project_state: ProjectStateStore, record: DraftRecord) -> PlanUpdates:
    """Read what stands about ``record``'s work, once, while the store is held.

    A plan is made from the work still to do when the run read it, and says
    nothing about what she had reported done by then; what it speaks about
    and she reports done afterward is what a page names. A plan that carries
    no ids is measured against its window instead, and named nothing. Her
    updates are read in one batch for the distinct assignments of the plan,
    however many rows it has, and the assignments on record in one more.
    Both pages read this one rule and word it for their reader; neither
    judges whether the plan should be made again, which is hers to decide.
    """
    with project_state.exclusively():
        rows = project_state.all_assignments()
        on_record = frozenset(item.assignment_id for item in rows)
        if record.plan_assignment_ids is None:
            week = read_week(project_state, project_state, record.plan_date)
            found = ReportedDone(named=(), known=False) if week.done_ids() else None
            return PlanUpdates(done=found, statuses={}, on_record=on_record)
        if not record.plan_assignment_ids:
            return PlanUpdates(done=None, statuses={}, on_record=on_record)
        statuses = statuses_for(project_state, record.plan_assignment_ids)
    titles = {item.assignment_id: item.title for item in rows}
    named = tuple(
        (name, titles.get(name, NOT_ON_RECORD))
        for name in record.plan_assignment_ids
        if not statuses[name].needs_homework
    )
    return PlanUpdates(
        done=ReportedDone(named=named, known=True) if named else None,
        statuses=statuses,
        on_record=on_record,
    )


def reported_done(project_state: ProjectStateStore, record: DraftRecord) -> ReportedDone | None:
    """Work in ``record``'s plan she has reported done since, or ``None`` while there is none."""
    return plan_updates(project_state, record).done
