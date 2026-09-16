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
the undo and the standing report is the one it restored, and both are read
here so a page can say "restored your update of the 16th" and mean it.
"""

from collections.abc import Iterable, Mapping
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
    def school_says_missing(self) -> bool:
        """Whether any school channel's latest word is that the work is missing."""
        return any(report.status == MISSING for report in self.school.values())

    @property
    def check_the_school_record(self) -> bool:
        """Whether her "done" stands beside a school "missing": something to check together,
        with both statements shown, and nothing decided for either."""
        return self.work_state == DONE and self.school_says_missing


def statuses_for(
    store: ProjectStateStore, assignment_ids: Iterable[str]
) -> dict[str, AssignmentStatus]:
    """What stands about each assignment named, read in a few batched reads.

    Callers that need the statuses to agree with the rows they were read
    beside hold the store's lock around both; the reads here take the same
    re-entrant lock and add none of their own per assignment.
    """
    wanted = list(dict.fromkeys(assignment_ids))
    heads = store.student_report_heads()
    school = store.latest_status_reports_by_channel()
    restored_ids = [
        head.undoes_report_id
        for head in heads.values()
        if head.operation != REPORT and head.undoes_report_id is not None
    ]
    undone = store.student_reports_by_id(restored_ids)
    before_undone = store.student_reports_by_id(
        [
            report.previous_report_id
            for report in undone.values()
            if report.previous_report_id is not None
        ]
    )
    statuses: dict[str, AssignmentStatus] = {}
    for assignment_id in wanted:
        head = heads.get(assignment_id)
        statuses[assignment_id] = AssignmentStatus(
            assignment_id=assignment_id,
            head=head,
            asserted=_asserted(head, undone, before_undone),
            school=school.get(assignment_id, {}),
        )
    return statuses


def _asserted(
    head: StudentReport | None,
    undone: Mapping[str, StudentReport],
    before_undone: Mapping[str, StudentReport],
) -> StudentReport | None:
    """The report whose words stand after ``head``."""
    if head is None:
        return None
    if head.operation == REPORT:
        return head
    if head.status is None or head.undoes_report_id is None:
        return None
    taken_back = undone.get(head.undoes_report_id)
    if taken_back is None or taken_back.previous_report_id is None:
        return None
    return before_undone.get(taken_back.previous_report_id)
