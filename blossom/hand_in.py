"""Her account of turning work in, kept apart from her account of the work.

Done means she finished her part; it turns nothing in. What she says about
delivery is a second account with its own events: that something is still to
turn in, that she turned it in, that there is nothing to turn in, or that she
is not sure. Nothing the school reports, no check by the family, and no
update to her work ever changes it, and it never reaches a planner.

An event carries what stands after it, as her work reports do: for a report,
the state, one optional next action, and an optional note; for an undo, what
is restored, or nothing. Three things are read from a chain and none stands
in for another. The head is the last event, and the only thing a save is
compared with. The event that began the state standing now gives the day a
page says and her place in a list, and an edit of the action or the note
inside that state does not move it; leaving the state and coming back begins
a new period, and an undo puts back the period that stood before, day and
all. The words shown come from the latest event of the period, and a note
changed on a later day carries that day beside it.

This module holds the types and the reading of a chain. It reads no file and
no clock; the record's store keeps the events and checks a chain as it is
written.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal, NamedTuple, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator, model_validator

from blossom.authored_text import multiline, single_line

HandInState = Literal["unknown", "needs_hand_in", "turned_in", "not_required"]
"""What she can say about delivery. ``unknown`` is her saying she is not sure, which is
a report; having said nothing is the absence of one."""
UNKNOWN: Final = "unknown"
NEEDS_HAND_IN: Final = "needs_hand_in"
TURNED_IN: Final = "turned_in"
NOT_REQUIRED: Final = "not_required"
REPORT: Final = "report"
UNDO: Final = "undo"
NEXT_ACTION_MAX_LENGTH: Final = 200
HAND_IN_NOTE_MAX_LENGTH: Final = 500

Words = tuple[HandInState | None, str | None, str | None, AwareDatetime | None]
"""Everything an event leaves standing: state, next action, note, and cue."""
NO_WORDS: Final[Words] = (None, None, None, None)


class BrokenChain(ValueError):
    """Raised for a chain that does not hold together: what it says is unavailable.

    Not the same as a chain that says nothing. A reader shows that her
    hand-in record cannot be read, and never that she has reported nothing;
    a writer writes nothing on top of it."""


class HandInEvent(BaseModel):
    """One thing she said about turning an assignment in, as of a moment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    assignment_id: str
    operation: Literal["report", "undo"]
    state: HandInState | None
    """What stands after this event: ``None`` only when an undo restores no report."""
    next_action: str | None = None
    """The one next step she chose, kept only with still to turn in."""
    note: str | None = None
    cue_at_utc: AwareDatetime | None = None
    """A calendar cue she chose, not a reminder anyone delivered. No page sets one yet."""
    reported_at: AwareDatetime
    reported_on: date
    """The household's day the event was accepted, not a claim about when she acted."""
    previous_event_id: str | None = None
    undone_event_id: str | None = None
    sequence: int | None = None
    """Its place in the table's order, given by the file; ``None`` until it is written.
    An order, never a name: events are named by ``event_id``."""

    @field_validator("next_action")
    @classmethod
    def _is_one_kept_line(cls, next_action: str | None) -> str | None:
        return single_line(next_action, NEXT_ACTION_MAX_LENGTH)

    @field_validator("note")
    @classmethod
    def _is_kept_as_it_is_compared(cls, note: str | None) -> str | None:
        return multiline(note, HAND_IN_NOTE_MAX_LENGTH)

    @model_validator(mode="after")
    def _is_a_whole_event(self) -> Self:
        """A report says a state and takes nothing back; an undo follows what it takes back.

        A next action and a cue are for something still to turn in and go
        with no other state; a note goes with any state and with none of no
        state. What the chain adds is the store's to check as it writes.
        """
        if self.operation == REPORT:
            if self.state is None:
                msg = f"hand-in report {self.event_id!r} says no state"
                raise ValueError(msg)
            if self.undone_event_id is not None:
                msg = f"hand-in report {self.event_id!r} names an event to take back"
                raise ValueError(msg)
        elif self.undone_event_id is None or self.previous_event_id != self.undone_event_id:
            msg = f"hand-in undo {self.event_id!r} must follow the event it takes back, and name it"
            raise ValueError(msg)
        if self.state != NEEDS_HAND_IN and (
            self.next_action is not None or self.cue_at_utc is not None
        ):
            msg = (
                f"event {self.event_id!r} carries a next action or a cue, kept only with "
                "still to turn in"
            )
            raise ValueError(msg)
        if self.state is None and self.note is not None:
            msg = f"event {self.event_id!r} carries a note with no state for it to stand with"
            raise ValueError(msg)
        return self

    @property
    def words(self) -> Words:
        """Everything this event leaves standing: state, next action, note, and cue.

        The whole of it, cue included, is what an undo must carry to restore
        what stood before. It is not what a save is compared with: a save
        says nothing about a cue, so the store compares the first three.
        """
        return (self.state, self.next_action, self.note, self.cue_at_utc)


@dataclass(frozen=True)
class HandInSaved:
    """A new report was appended; it is the head now."""

    event: HandInEvent


@dataclass(frozen=True)
class HandInAlreadySaved:
    """What she sent is what stands; nothing was written."""

    head: HandInEvent


@dataclass(frozen=True)
class HandInConflict:
    """The chain moved on since her page was made; nothing was written. The head is as
    the transaction that refused the save read it."""

    head: HandInEvent | None


@dataclass(frozen=True)
class HandInUndone:
    """The head was taken back; the undo is the head now."""

    event: HandInEvent


def already_undone(conflict: HandInConflict, event_id: str) -> bool:
    """True when an Undo was refused because that very event is already taken back.

    Read from the head the refusing transaction saw: it is the undo of the
    event the button named. Anything said since makes it a change like any
    other, and equal words prove nothing.
    """
    head = conflict.head
    return head is not None and head.operation == UNDO and head.undone_event_id == event_id


class _Standing(NamedTuple):
    source: HandInEvent | None
    entered: HandInEvent | None
    note_on: date | None


_NOTHING: Final = _Standing(None, None, None)


@dataclass(frozen=True)
class HandInHistoryRow:
    """One event, with what stood after it."""

    event: HandInEvent
    source: HandInEvent | None
    """The event whose words stood after this one; ``None`` when nothing did."""
    entered: HandInEvent | None
    """The event that began the state standing after this one."""

    @property
    def state(self) -> HandInState | None:
        """The state that stood after this event."""
        return None if self.source is None else self.source.state

    @property
    def reported_on(self) -> date | None:
        """The day the state standing after this event was entered."""
        return None if self.entered is None else self.entered.reported_on


@dataclass(frozen=True)
class HandInProjection:
    """What stands about turning one assignment in, read from its chain."""

    assignment_id: str
    head: HandInEvent | None
    source: HandInEvent | None
    """The event her current words came from; ``None`` when nothing is reported."""
    entered: HandInEvent | None
    """The event that began the state standing now."""
    note_on: date | None
    history: tuple[HandInHistoryRow, ...]

    @property
    def state(self) -> HandInState | None:
        """The state that stands, or ``None`` when she has reported nothing."""
        return None if self.source is None else self.source.state

    @property
    def next_action(self) -> str | None:
        """The one next step that stands, from the latest event of the period."""
        return None if self.source is None else self.source.next_action

    @property
    def note(self) -> str | None:
        """The note that stands, from the latest event of the period."""
        return None if self.source is None else self.source.note

    @property
    def reported_on(self) -> date | None:
        """The day she entered the state standing now."""
        return None if self.entered is None else self.entered.reported_on

    @property
    def entered_by(self) -> str | None:
        """The id of the event that began the state standing now. Never sent back by a
        page as the head."""
        return None if self.entered is None else self.entered.event_id

    @property
    def note_updated_on(self) -> date | None:
        """The day the note shown was written, when that is not the day the state began."""
        if self.note is None or self.note_on is None or self.note_on == self.reported_on:
            return None
        return self.note_on

    @property
    def head_id(self) -> str | None:
        """What a page carries back so a save can be compared. Never the period's event."""
        return None if self.head is None else self.head.event_id

    @property
    def words(self) -> Words:
        """Everything that stands now: state, next action, note, and cue."""
        return NO_WORDS if self.source is None else self.source.words

    @property
    def words_before_head(self) -> Words:
        """What stood before the head, which is what an undo of the head restores."""
        source = self.history[-2].source if len(self.history) > 1 else None
        return NO_WORDS if source is None else source.words

    @property
    def undo_event_id(self) -> str | None:
        """The event an Undo would take back: the head, when the head is a report."""
        head = self.head
        return head.event_id if head is not None and head.operation == REPORT else None


def project(assignment_id: str, chain: Sequence[HandInEvent]) -> HandInProjection:
    """Read one assignment's chain, in stored order, in one pass, or refuse it whole.

    After a report in the state already standing, the period and its day
    stay and the words are the new event's; the note's day moves only when
    the note's words do. After a report in another state, a period begins.
    An undo takes back the report immediately before it and nothing else,
    and restores what stood before that report, as worked out when the pass
    went by it.

    An event that is whole by itself is not thereby whole in its chain, so
    the chain is held to what the store holds a write to. Every event is
    this assignment's and follows the one before it; no id is met twice; an
    undo names the report just before it, never an undo and never something
    further back; and an undo carries exactly what it restores, cue
    included, since the head and this reading would otherwise give two
    accounts of one record. Anything else is ``BrokenChain``, and what the
    chain says is unavailable: the store never writes such a chain, so one
    that is read was made some other way and is evidence of nothing.
    """
    stood: dict[str, _Standing] = {}
    history: list[HandInHistoryRow] = []
    standing = _NOTHING
    before: HandInEvent | None = None
    for event in chain:
        if event.event_id in stood:
            msg = f"the hand-in chain of {assignment_id!r} repeats an event id, {event.event_id!r}"
            raise BrokenChain(msg)
        follows = None if before is None else before.event_id
        if event.assignment_id != assignment_id or event.previous_event_id != follows:
            msg = f"hand-in event {event.event_id!r} does not follow the chain of {assignment_id!r}"
            raise BrokenChain(msg)
        if event.operation == REPORT:
            same_period = standing.source is not None and standing.source.state == event.state
            if same_period:
                assert standing.source is not None  # noqa: S101  (checked above, for the reader)
                note_on = (
                    standing.note_on if event.note == standing.source.note else event.reported_on
                )
                standing = _Standing(event, standing.entered, note_on)
            else:
                standing = _Standing(event, event, event.reported_on)
        else:
            if (
                before is None
                or before.operation != REPORT
                or event.undone_event_id != before.event_id
            ):
                msg = (
                    f"hand-in undo {event.event_id!r} must take back the report immediately "
                    "before it"
                )
                raise BrokenChain(msg)
            restored = (
                _NOTHING if before.previous_event_id is None else stood[before.previous_event_id]
            )
            expected = NO_WORDS if restored.source is None else restored.source.words
            if event.words != expected:
                msg = f"hand-in undo {event.event_id!r} does not carry the state it restores"
                raise BrokenChain(msg)
            standing = restored
        stood[event.event_id] = standing
        history.append(HandInHistoryRow(event, standing.source, standing.entered))
        before = event
    return HandInProjection(
        assignment_id=assignment_id,
        head=before,
        source=standing.source,
        entered=standing.entered,
        note_on=standing.note_on,
        history=tuple(history),
    )


def read_chains(
    chains: Mapping[str, Sequence[HandInEvent]], assignment_ids: Iterable[str]
) -> tuple[dict[str, HandInProjection], frozenset[str]]:
    """What stands about each assignment named, and the ones whose chain does not hold.

    A page reads every chain in one go and cannot stop at the first that is
    broken, so the broken ones are handed back by name, apart. They are not
    in the first answer and must never be shown as having said nothing: an
    assignment with no events has a reading that says nothing, and one named
    in the second answer has a record that cannot be read.
    """
    readable: dict[str, HandInProjection] = {}
    unavailable: set[str] = set()
    for assignment_id in assignment_ids:
        try:
            readable[assignment_id] = project(assignment_id, chains.get(assignment_id, ()))
        except BrokenChain:
            unavailable.add(assignment_id)
    return readable, frozenset(unavailable)
