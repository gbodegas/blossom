"""What stands about each assignment's work: her account, and the school's, read apart.

Her report answers one question, whether she has finished her part; the
school's reports answer another, what the school's records say; and the
record's dates answer a third. This module reads the first two into one
projection per assignment without choosing between them. Her current
report decides whether an assignment is still work to plan; a school
report of "missing" beside her "done" is something for the family to check,
never a verdict on either account. The family's own record of having
checked it is read here too, and it changes nothing about the other two.
Nothing here writes.

Her account is a chain of events, each carrying the state that stands after
it, so the head of the chain says what stands now, whatever led there. The
report whose words stand is not always the head: after an undo, the head is
the undo and the standing report is the one it restored, however many undos
lie between, and both are read here so a page can say "restored your update
of the 16th" and mean it. The school speaks through channels, and what a
channel says now is its latest report, whatever another channel has said
since; every page that raises a check shows each channel's word it rests on.

A check the family marks is made against a basis: the assignment, the
report that began the Done standing, and each school statement of missing
current at the time. The Done period is read from her chain in the same
pass that reads what stands: a report that takes her account from anything
else to Done begins one, a Done that only changes the note keeps it, a
Not yet ends it, and an undo restores the period of the state it restores,
never beginning one of its own. The check stands while the basis is the one
now, and the row is open again when it is not.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal, NamedTuple

from blossom.reconciliation import SourceChannel
from blossom.stores.project_state import (
    CHECKED,
    DONE,
    NOT_YET,
    REPORT,
    FamilyCheck,
    ProjectStateStore,
    StatusReport,
    StudentReport,
)

WorkState = Literal["unreported", "not_yet", "done"]
UNREPORTED: Final = "unreported"
MISSING: Final = "missing"
"""The school's word that makes a "done" of hers something to check."""


@dataclass(frozen=True)
class HistoryRow:
    """One event of hers as a page lists it: the event, and for an undo what it put back."""

    event: StudentReport
    restored: StudentReport | None = None
    """For an undo, the report whose words stand after it, with its own day; ``None`` for
    a report, and for an undo that left no report standing."""


class Standing(NamedTuple):
    """What stands after one event of hers: the report whose words stand, and the report
    that began the Done period standing, when one does."""

    report: StudentReport | None
    done_since: str | None
    """The id of the report that took her account from anything else to Done, kept through
    a Done that only changes the note and restored by an undo; ``None`` while no Done
    stands."""


NOTHING: Final = Standing(None, None)


@dataclass(frozen=True)
class AssignmentStatus:
    """What stands about one assignment's work: hers, each school channel's, and the
    family's check of the two, when there is one."""

    assignment_id: str
    head: StudentReport | None
    """The last event in her chain, or ``None`` when she has said nothing."""
    asserted: StudentReport | None
    """The report whose words stand: the head when it is a report, the report an undo
    restored when it is an undo, ``None`` when nothing stands."""
    school: Mapping[SourceChannel, StatusReport]
    """The latest report from each school channel, read apart."""
    history: tuple[StudentReport, ...] = ()
    """Every event of hers under the assignment, in the order kept."""
    school_history: tuple[StatusReport, ...] = ()
    """Every report the school has made about it, by the day reported and the order kept."""
    checks: tuple[FamilyCheck, ...] = ()
    """Every check event of the family's under the assignment, in the order kept."""

    @property
    def status(self) -> str | None:
        """What stands: ``done``, ``not_yet``, or ``None`` for no report.

        Read from the report whose words stand, never from the head's own
        fields: an undo carries what it restored, and a chain whose links
        lead nowhere restores nothing, whatever its last event says.
        """
        return None if self.asserted is None else self.asserted.status

    @property
    def note(self) -> str | None:
        """The note that stands with her report, if any; from the same report."""
        return None if self.asserted is None else self.asserted.note

    @property
    def work_state(self) -> WorkState:
        """Her account as the planner reads it: unreported, not yet, or done."""
        if self.status == DONE:
            return DONE
        if self.status == NOT_YET:
            return NOT_YET
        return UNREPORTED

    @property
    def needs_homework(self) -> bool:
        """Whether the assignment is still work to plan: everything but a "done" of hers."""
        return self.work_state != DONE

    @property
    def reported_on(self) -> date | None:
        """The day of the report whose words stand."""
        return None if self.asserted is None else self.asserted.reported_on

    @property
    def restored_on(self) -> date | None:
        """The day an undo restored the standing report, when the head is an undo that
        restored one; ``None`` otherwise."""
        if self.head is None or self.head.operation == REPORT or self.asserted is None:
            return None
        return self.head.reported_on

    @property
    def cleared_on(self) -> date | None:
        """The day an undo left no update standing, when the head is such an undo."""
        if self.head is None or self.head.operation == REPORT or self.head.status is not None:
            return None
        return self.head.reported_on

    @property
    def head_id(self) -> str | None:
        """What a page carries back so a save lands on the chain it showed."""
        return None if self.head is None else self.head.report_id

    @property
    def school_statements(self) -> tuple[StatusReport, ...]:
        """What each school channel says now, the latest day first and then by channel: the
        school's current word, which the pages show whole rather than as one latest report."""
        return tuple(
            sorted(
                self.school.values(),
                key=lambda report: (-report.reported_on.toordinal(), str(report.channel)),
            )
        )

    @property
    def missing_reports(self) -> tuple[StatusReport, ...]:
        """The current statements that say missing: what a check rests on."""
        return tuple(report for report in self.school_statements if report.status == MISSING)

    @property
    def school_says_missing(self) -> bool:
        """Whether any school channel's latest word is that the work is missing."""
        return bool(self.missing_reports)

    @property
    def check_the_school_record(self) -> bool:
        """Whether her "done" stands beside a school "missing": something to check together,
        with both statements shown, and nothing decided for either."""
        return self.work_state == DONE and self.school_says_missing

    @property
    def history_rows(self) -> tuple[HistoryRow, ...]:
        """Her events as a page lists them, each undo with the report it put back, worked
        out for the whole chain in one pass."""
        return tuple(
            HistoryRow(event, None if event.operation == REPORT else stands)
            for event, stands in zip(self.history, standing_after_each(self.history), strict=True)
        )

    @property
    def done_since(self) -> str | None:
        """The id of the report that began the Done standing; ``None`` while none does."""
        if self.work_state != DONE:
            return None
        states = states_after_each(self.history)
        return states[-1].done_since if states else None

    @property
    def check_basis(self) -> str | None:
        """What a check of this row is made against, while there is something to check: her
        Done standing beside a school "missing". ``None`` otherwise."""
        since = self.done_since
        if since is None or not self.school_says_missing:
            return None
        return basis_of(self.assignment_id, since, self.missing_reports)

    @property
    def check_head(self) -> FamilyCheck | None:
        """The last check event of the family's, whatever it is, or ``None`` for none."""
        return self.checks[-1] if self.checks else None

    @property
    def check_head_id(self) -> str | None:
        """What the family page carries back so a check lands on the record it showed."""
        head = self.check_head
        return None if head is None else head.check_id

    @property
    def check(self) -> FamilyCheck | None:
        """The check that stands: the last check event, when it marks checked what there is
        to check now. ``None`` when the row is open, reopened, or the facts moved since."""
        head = self.check_head
        basis = self.check_basis
        if head is None or basis is None or head.operation != CHECKED or head.basis != basis:
            return None
        return head

    @property
    def checked(self) -> bool:
        """Whether a check stands against the facts as they are."""
        return self.check is not None

    @property
    def needs_a_check(self) -> bool:
        """Whether her "done" stands beside a school "missing" with no check standing against
        it: the row the family page lists as worth checking together."""
        return self.check_basis is not None and self.check is None


def statuses_for(
    store: ProjectStateStore, assignment_ids: Iterable[str]
) -> dict[str, AssignmentStatus]:
    """What stands about each assignment named, read in three batched reads.

    Callers that need the statuses to agree with the rows they were read
    beside hold the store's lock around both; the reads here take the same
    re-entrant lock and add none of their own per assignment: her events
    under the assignments named, the school's reports, and the family's
    checks. The head, the standing report, each channel's current word,
    both histories, and the check that stands are worked out from those in
    memory.
    """
    wanted = list(dict.fromkeys(assignment_ids))
    chains = store.student_report_chains(wanted)
    reports = store.status_reports_by_assignment()
    checks = store.family_check_chains(wanted)
    statuses: dict[str, AssignmentStatus] = {}
    for assignment_id in wanted:
        chain = chains.get(assignment_id, [])
        said = reports.get(assignment_id, [])
        statuses[assignment_id] = AssignmentStatus(
            assignment_id=assignment_id,
            head=chain[-1] if chain else None,
            asserted=standing_report(chain),
            school={report.channel: report for report in said},
            history=tuple(chain),
            school_history=tuple(said),
            checks=tuple(checks.get(assignment_id, [])),
        )
    return statuses


def basis_of(assignment_id: str, done_since: str, missing: Iterable[StatusReport]) -> str:
    """The basis a check is made against, as one line: the assignment, the report that began
    the Done, and each statement of missing as its channel, status, and day, sorted.

    A statement is named by what makes it one, the school's channel, its
    status, and its day, so the same page pasted again names the same
    statement and a report the school had not made before names a new one.
    An older statement that reaches the record late is not a channel's
    current word and is not in the line.
    """
    statements = sorted(
        f"{report.channel.value}:{report.status}:{report.reported_on.isoformat()}"
        for report in missing
    )
    return "|".join([assignment_id, done_since, *statements])


def basis_parts(basis: str) -> tuple[str, str, tuple[str, ...]]:
    """A basis taken apart: the assignment, the report that began the Done, and the
    statements, so a page can say which of them differs now."""
    parts = basis.split("|")
    return parts[0], parts[1] if len(parts) > 1 else "", tuple(parts[2:])


def standing_after_each(chain: Sequence[StudentReport]) -> list[StudentReport | None]:
    """The report whose words stand after each event of ``chain``, in the chain's order."""
    return [state.report for state in states_after_each(chain)]


def states_after_each(chain: Sequence[StudentReport]) -> list[Standing]:
    """What stands after each event of ``chain``, in the chain's order: the report whose
    words stand, and the report that began the Done period standing.

    One pass from the start. After a report, that report stands, and its
    Done period is the one before it when Done stood before it, its own
    when Done did not, and none when it says not yet. An undo restores what
    stood before the report it takes back, period included, and what stood
    there was worked out when the pass went by it, whether a report or
    something an earlier undo had put back; so a report, an undo, another
    report, and another undo end at the first report, with the first
    report's day and period, and a chain of any length costs one look at
    each event. An undo can only reach back: one that names an event the
    pass has not met, a link that leads nowhere or round in a ring, leaves
    nothing standing, and a page then shows her status without putting a
    correction's day on her words.
    """
    met: dict[str, StudentReport] = {}
    stood: dict[str, Standing] = {}
    after: list[Standing] = []
    for event in chain:
        prior = stood.get(event.previous_report_id or "", NOTHING)
        if event.operation == REPORT:
            if event.status != DONE:
                since = None
            elif prior.report is not None and prior.report.status == DONE:
                since = prior.done_since
            else:
                since = event.report_id
            stands = Standing(event, since)
        else:
            taken_back = met.get(event.undoes_report_id or "")
            before = None if taken_back is None else taken_back.previous_report_id
            stands = NOTHING if before is None else stood.get(before, NOTHING)
        met[event.report_id] = event
        stood[event.report_id] = stands
        after.append(stands)
    return after


def standing_report(chain: Sequence[StudentReport]) -> StudentReport | None:
    """The report whose words stand at the end of ``chain``, or ``None`` when none does."""
    after = standing_after_each(chain)
    return after[-1] if after else None
