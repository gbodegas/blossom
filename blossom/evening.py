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
from blossom.noticing import Everything, planning_digest, read_everything, week_from
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
    *,
    everything: Everything | None = None,
) -> Staleness | None:
    """How ``record`` fails to fit the evening as it stands now, or ``None`` while it fits.

    Her signal is measured first, since it changes the budget the checks held
    the plan to. Then the window: with the record handed in, the week the
    run read for this evening is taken from it and its fingerprint compared
    to the one the draft carries; a draft from before plans carried one is
    not measured against the week. ``everything`` is a reading a page has
    in hand already, which is measured as it is; with the store alone, the
    record is read here, and only when the week is reached. With neither,
    the week is not measured.
    """
    signaled = bool(signals.for_evening(record.plan_date))
    if signaled != record.too_much:
        return Staleness.SIGNALED_SINCE if signaled else Staleness.SIGNAL_ENDED
    if record.inputs_digest is None:
        return None
    if everything is None and project_state is not None:
        everything = read_everything(project_state, project_state)
    if everything is None:
        return None
    if planning_digest(week_from(everything, record.plan_date)) != record.inputs_digest:
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


def plan_updates(
    project_state: ProjectStateStore,
    record: DraftRecord,
    *,
    everything: Everything | None = None,
) -> PlanUpdates:
    """What stands about ``record``'s work, from one reading of the record.

    A plan is made from the work still to do when the run read it, and says
    nothing about what she had reported done by then; what it speaks about
    and she reports done afterward is what a page names. A plan that carries
    no ids is measured against its window instead, and named nothing.
    ``everything`` is the reading a page has in hand, the one it measures
    the plan against the week with, so the notice, the marks, and the stale
    state are about one record and her reports are read once; without one,
    the record is read here, the plan's assignments named to it. An id the
    reading was not asked about is read apart, which a caller avoids by
    naming the plan's ids when it reads. Both pages read this one rule and
    word it for their reader; neither judges whether the plan should be made
    again, which is hers to decide.
    """
    names = record.plan_assignment_ids
    if everything is None:
        everything = read_everything(project_state, project_state, also=names or ())
    on_record = everything.ids
    if names is None:
        week = week_from(everything, record.plan_date)
        found = ReportedDone(named=(), known=False) if week.done_ids() else None
        return PlanUpdates(done=found, statuses={}, on_record=on_record)
    if not names:
        return PlanUpdates(done=None, statuses={}, on_record=on_record)
    unread = [name for name in names if name not in everything.statuses]
    apart = statuses_for(project_state, unread) if unread else {}
    statuses = {
        name: everything.statuses[name] if name in everything.statuses else apart[name]
        for name in names
    }
    titles = {item.assignment_id: item.title for item in everything.assignments}
    named = tuple(
        (name, titles.get(name, NOT_ON_RECORD))
        for name in names
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
