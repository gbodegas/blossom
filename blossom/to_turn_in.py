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

What a press or an undo did is said on the page she made it from, and that
result belongs to the event the save accepted, which the address names. The
event is looked up in the named assignment's own history, in the page's one
reading, and trusted for nothing until it is found there: an Undo is
offered only for that event, only while it is the report that stands, and
never for whatever came after it. A refusal is said with the assignment it
was about as it stands now, whether or not that is on the list.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

from blossom.hand_in import NEEDS_HAND_IN, REPORT, TURNED_IN, UNDO, HandInEvent
from blossom.noticing import Everything
from blossom.views import HandInEventView, HandInView, SchoolStatementView, ToTurnInRowView

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
TURNED_IN_EARLIER: Final = (
    "You reported it turned in from here. It has changed since, and this is what stands now."
)
SAME_EARLIER: Final = (
    "It was already saved as turned in when you pressed here. It has changed since, and this "
    "is what stands now."
)
UNDONE_EARLIER: Final = (
    "You undid a hand-in update from here. It has changed since, and this is what stands now."
)
EARLIER: Final[dict[str, str]] = {
    "turned_in": TURNED_IN_EARLIER,
    "same": SAME_EARLIER,
    "undone": UNDONE_EARLIER,
}
"""The same three, said of an event that something newer has followed: what she did, as
something done earlier, never as what stands."""
RESULT_UNREADABLE: Final = (
    "What was done here cannot be shown, because this assignment's hand-in record cannot be "
    "read right now."
)
RESULT_GONE: Final = "That assignment is not on record now, so what was done here cannot be shown."
COMPACT_ROWS: Final = 3
"""How many rows her week shows before it points at the whole list."""
NO_CUE: Final = datetime.max.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ToTurnIn:
    """The list as a page shows it: the rows in order, and the records that cannot be read."""

    rows: list[ToTurnInRowView]
    unreadable: list[ToTurnInRowView]


@dataclass(frozen=True)
class Receipt:
    """What an address says a press or an undo did: which of the three, to which assignment,
    and the event the save accepted. Words from an address, good for nothing until the
    page's reading has that event in that assignment's history."""

    said: str
    about: str
    event_id: str


@dataclass(frozen=True)
class Attempt:
    """A press the list refused: what was tried, on which assignment, and for an Undo the
    update its button named."""

    operation: Literal["turn_in", "undo"]
    assignment_id: str
    target: str | None = None


@dataclass(frozen=True)
class Affected:
    """The assignment a result or a refusal is about, as the page's reading has it now,
    whether or not it is on the list. ``hand_in`` is ``None`` when the assignment is off
    the record, and unavailable when its hand-in record cannot be read."""

    assignment_id: str
    title: str | None
    course: str | None
    hand_in: HandInView | None


@dataclass(frozen=True)
class ListResult:
    """What a press or an undo on the list did, said at a place that is on the page whether
    or not the row still is."""

    said: str
    about: Affected
    stands: bool
    """Whether the event the result is about is still the latest. When it is not, the
    result is said as something done earlier, beside what stands now."""
    undo_event_id: str | None
    """The report an Undo beside the result takes back: the very event the press made,
    while it is the report that stands, and never a later one."""


@dataclass(frozen=True)
class ListRefusal:
    """What a refusal on the list is about: the assignment as it stands now, the update a
    refused Undo named when it is in that assignment's history, and whether saying it was
    turned in can be pressed again on the head shown here."""

    operation: str
    about: Affected
    target: HandInEventView | None
    retry: bool


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


def affected(everything: Everything, assignment_id: str) -> Affected:
    """One assignment as the reading has it, apart from whether the list holds it."""
    item = next((row for row in everything.assignments if row.assignment_id == assignment_id), None)
    if item is None:
        return Affected(assignment_id, None, None, None)
    if assignment_id in everything.hand_ins_unavailable:
        return Affected(assignment_id, item.title, item.course, HandInView.of(None))
    reading = everything.hand_ins.get(assignment_id)
    return Affected(
        assignment_id,
        item.title,
        item.course,
        HandInView() if reading is None else HandInView.of(reading),
    )


def _is_what_was_said(event: HandInEvent, said: str) -> bool:
    """Match a report, a no-op state, or an undo against validated history."""
    if said == "undone":
        return event.operation == UNDO
    if said == "same":
        return event.state == TURNED_IN
    return event.operation == REPORT and event.state == TURNED_IN


def result_for(everything: Everything, receipt: Receipt | None) -> ListResult | None:
    """The result an address names, when the reading bears it out.

    The event must be in the named assignment's history and of the kind the
    address says; anything else, a made-up id, another assignment's event, a
    report named as an undo, says nothing at all. A press that was already
    saved names whatever event stood then, which is her report or an undo
    that put her report back, so it is held to the state that event left
    standing and not to its kind. While that event is the latest the result
    is what stands, and a report of hers can be taken back from here; an
    undo that stands offers none, since the head is no report. Once
    something newer follows it, the result is something she did earlier,
    shown beside what stands now, with no Undo.

    An assignment on record whose hand-in record cannot be read keeps the
    place and says so, which is true of that record whatever the address
    names. An assignment off the record is held to the same proof as any
    other: her events stay in the file when an assignment leaves it, the
    page's reading holds them for the one the address names, and only an
    address they bear out is told the assignment is gone. An address about
    an assignment that was never there, or naming an event that history
    does not hold, says nothing, as it would for one on record.
    """
    if receipt is None or receipt.said not in SAID:
        return None
    about = affected(everything, receipt.about)
    if about.hand_in is not None and about.hand_in.unavailable:
        return ListResult(RESULT_UNREADABLE, about, stands=False, undo_event_id=None)
    reading = everything.hand_ins.get(receipt.about)
    if reading is None or reading.head is None:
        return None
    event = next(
        (row.event for row in reading.history if row.event.event_id == receipt.event_id), None
    )
    if event is None or not _is_what_was_said(event, receipt.said):
        return None
    if about.hand_in is None:
        return ListResult(RESULT_GONE, about, stands=False, undo_event_id=None)
    if reading.head.event_id != event.event_id:
        return ListResult(EARLIER[receipt.said], about, stands=False, undo_event_id=None)
    return ListResult(
        SAID[receipt.said],
        about,
        stands=True,
        undo_event_id=None if receipt.said == "undone" else reading.undo_event_id,
    )


def refusal_for(everything: Everything, attempt: Attempt | None) -> ListRefusal | None:
    """What a refusal shows beside its sentence, from the reading the list is drawn from."""
    if attempt is None:
        return None
    about = affected(everything, attempt.assignment_id)
    reading = everything.hand_ins.get(attempt.assignment_id)
    named = (
        None
        if reading is None or attempt.target is None
        else next(
            (row.event for row in reading.history if row.event.event_id == attempt.target), None
        )
    )
    return ListRefusal(
        operation=attempt.operation,
        about=about,
        target=None
        if named is None
        else HandInEventView(
            on=named.reported_on,
            undo=named.operation == UNDO,
            state=named.state,
            next_action=named.next_action,
            note=named.note,
        ),
        retry=attempt.operation == "turn_in"
        and about.hand_in is not None
        and about.hand_in.state == NEEDS_HAND_IN,
    )
