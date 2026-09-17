"""What stands about each assignment's work: her account, and the school's, read apart.

Her report answers one question, whether she has finished her part; the
school's reports answer another, what the school's records say; and the
record's dates answer a third. This module reads the first two into one
projection per assignment without choosing between them. Her current
report decides whether an assignment is still work to plan; a school
report of "missing" beside her "done" is something for the family to check,
never a verdict on either account. Nothing here writes.

Her account is a chain of events, each carrying the state that stands after
it, so the head of the chain says what stands now, whatever led there. The
report whose words stand is not always the head: after an undo, the head is
the undo and the standing report is the one it restored, however many undos
lie between, and both are read here so a page can say "restored your update
of the 16th" and mean it. The school speaks through channels, and what a
channel says now is its latest report, whatever another channel has said
since; every page that raises a check shows each channel's word it rests on.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal

from blossom.reconciliation import SourceChannel
from blossom.stores.project_state import (
    DONE,
    NOT_YET,
    REPORT,
    ProjectStateStore,
    StatusReport,
    StudentReport,
)

WorkState = Literal["unreported", "not_yet", "done"]
UNREPORTED: Final = "unreported"
MISSING: Final = "missing"
"""The school's word that makes a "done" of hers something to check."""
NOTE_MAX_LENGTH: Final = 500
"""How long her note may be, in code points, once its edges and line endings are normalized."""


def normalize_note(text: str | None) -> str | None:
    """Her note as it is kept: line endings as one kind, edges trimmed, blank as none.

    The words inside stay as she typed them, line breaks included; the same
    note typed on two devices reads the same, so a repeat is a repeat.
    """
    if text is None:
        return None
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return cleaned or None


@dataclass(frozen=True)
class HistoryRow:
    """One event of hers as a page lists it: the event, and for an undo what it put back."""

    event: StudentReport
    restored: StudentReport | None = None
    """For an undo, the report whose words stand after it, with its own day; ``None`` for
    a report, and for an undo that left no report standing."""


@dataclass(frozen=True)
class AssignmentStatus:
    """What stands about one assignment's work: hers, and each school channel's."""

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

    @property
    def status(self) -> str | None:
        """What stands: ``done``, ``not_yet``, or ``None`` for no report."""
        return None if self.head is None else self.head.status

    @property
    def note(self) -> str | None:
        """The note that stands with her report, if any."""
        return None if self.head is None else self.head.note

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
        """Her events as a page lists them, each undo with the report it put back."""
        return tuple(
            HistoryRow(event, None if event.operation == REPORT else standing_report(chain))
            for event, chain in (
                (event, self.history[: index + 1]) for index, event in enumerate(self.history)
            )
        )


def statuses_for(
    store: ProjectStateStore, assignment_ids: Iterable[str]
) -> dict[str, AssignmentStatus]:
    """What stands about each assignment named, read in two batched reads.

    Callers that need the statuses to agree with the rows they were read
    beside hold the store's lock around both; the reads here take the same
    re-entrant lock and add none of their own per assignment: her events
    under the assignments named, and the school's reports. The head, the
    standing report, each channel's current word, and both histories are
    worked out from those in memory.
    """
    wanted = list(dict.fromkeys(assignment_ids))
    chains = store.student_report_chains(wanted)
    reports = store.status_reports_by_assignment()
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
        )
    return statuses


def standing_report(chain: Sequence[StudentReport]) -> StudentReport | None:
    """The report whose words stand at the end of ``chain``, or ``None`` when none does.

    An undo restores what stood before the report it takes back, and what
    stood there may itself have been put back by an earlier undo. So the
    walk goes from the head back through every undo it meets, each time to
    the event before the report that undo took back, until it reaches a
    report or the start of the chain. A report, an undo, another report, and
    another undo end at the first report, with the first report's day. Each
    step moves toward the start, so the walk ends within the chain's length;
    a link that leads nowhere, or round in a ring, ends it with no report,
    and a page then shows her status without putting a correction's day on
    her words.
    """
    by_id = {event.report_id: event for event in chain}
    event = chain[-1] if chain else None
    for _ in chain:
        if event is None or event.operation == REPORT:
            return event
        taken_back = by_id.get(event.undoes_report_id or "")
        before = None if taken_back is None else taken_back.previous_report_id
        event = None if before is None else by_id.get(before)
    return event if event is None or event.operation == REPORT else None
