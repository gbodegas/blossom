"""Her To turn in list, drawn from a page's one reading of the record.

Everything she reports as still to turn in, whatever her work update says and
whatever week the work is due in, since the step after Done is the one that
goes missing and a list that followed the week would hide it. Nothing leaves
the list with age. The order is the order she took each on: by the event that
began the state standing now, as the file ordered it, so an edit of the next
step or the note does not move a row, leaving the state and coming back puts
it at the end, and an undo puts it back where it was. A cue she chose, when
cues exist, comes ahead of that; the assignment's id settles a tie.

An assignment whose hand-in record cannot be read is not in the list and is
not dropped either: it is named under the list as unreadable, since it may be
one she meant to turn in. Reading the list writes nothing, reminds no one, and
no plan or digest is drawn from it.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from blossom.hand_in import NEEDS_HAND_IN, TURNED_IN
from blossom.noticing import Everything
from blossom.views import HandInView, SchoolStatementView, ToTurnInRowView

TURNED_IN_FROM_THE_LIST: Final = "You reported it turned in. It is off your To turn in list."
ALREADY_TURNED_IN: Final = "Already saved."
BACK_ON_THE_LIST: Final = "Your hand-in update is undone."
SAID: Final[dict[str, str]] = {
    "turned_in": TURNED_IN_FROM_THE_LIST,
    "same": ALREADY_TURNED_IN,
    "undone": BACK_ON_THE_LIST,
}
"""What the address says a press or an undo on the list did, and the sentence shown for it:
the server chooses which, the address only carries the choice."""
COMPACT_ROWS: Final = 3
"""How many rows her week shows before it points at the whole list."""
NO_CUE: Final = datetime.max.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ToTurnIn:
    """The list as a page shows it: the rows in order, and the records that cannot be read."""

    rows: list[ToTurnInRowView]
    unreadable: list[ToTurnInRowView]


@dataclass(frozen=True)
class ListResult:
    """What a press or an undo on the list just did, about which assignment, said at a place
    that is on the page whether or not the row still is."""

    said: str
    assignment_id: str
    title: str | None
    course: str | None
    undo_event_id: str | None
    """The report an Undo beside the result would take back: hers saying it was turned in,
    read from the record as it stands now, never from the address."""


def to_turn_in(everything: Everything) -> ToTurnIn:
    """Build the list from one reading; no read of its own."""
    waiting: list[tuple[datetime, int, str, ToTurnInRowView]] = []
    unreadable: list[ToTurnInRowView] = []
    for item in everything.assignments:
        name = item.assignment_id
        if name in everything.hand_ins_unavailable:
            unreadable.append(
                ToTurnInRowView(
                    assignment_id=name,
                    course=item.course,
                    title=item.title,
                    hand_in=HandInView.of(None),
                )
            )
            continue
        reading = everything.hand_ins.get(name)
        if reading is None or reading.state != NEEDS_HAND_IN or reading.entered is None:
            continue
        status = everything.statuses.get(name)
        row = ToTurnInRowView(
            assignment_id=name,
            course=item.course,
            title=item.title,
            hand_in=HandInView.of(reading),
            missing=[
                SchoolStatementView.from_report(report)
                for report in (status.missing_reports if status is not None else ())
            ],
        )
        cue = reading.words[3] or NO_CUE
        waiting.append((cue, reading.entered.sequence or 0, name, row))
    waiting.sort(key=lambda entry: entry[:3])
    return ToTurnIn([row for *_, row in waiting], unreadable)


def result_for(everything: Everything, said: str | None, about: str | None) -> ListResult | None:
    """The result an address names, when it names one of the three and an assignment."""
    if said not in SAID or not about:
        return None
    item = next((row for row in everything.assignments if row.assignment_id == about), None)
    reading = everything.hand_ins.get(about)
    undo = None
    if said != "undone" and reading is not None and reading.state == TURNED_IN:
        undo = reading.undo_event_id
    return ListResult(
        said=SAID[said],
        assignment_id=about,
        title=None if item is None else item.title,
        course=None if item is None else item.course,
        undo_event_id=undo,
    )
