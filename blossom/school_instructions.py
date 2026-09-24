"""The school's instructions for an assignment, kept apart from anyone's own note.

A teacher's instruction under a card is the school's words: where to do the
work, what to study, that not every problem is required. Her note about work
she added is hers, and a parent's note is a parent's; neither is the school's,
and the school's never becomes theirs. So the school's instructions are kept
on their own, one row per text for each assignment, each with the school
channel it came from, the card it was first read under, and who pasted it.

Which of them apply is the family's choice, never the order things were
pasted in. The first instruction for an assignment applies, since there is
nothing to choose between; any other new text waits for a choice of which
apply, made against the revision of the assignment's instructions that the
chooser was shown. A new text is always kept, as history when not chosen, so
keeping what applies never loses what the school said. Every change to the
kept instructions takes the next revision, so a form made before a change and
a change back is still known as older. A choice that already stands in full
is nothing new, whatever revision it names.

This module is the rule and nothing else: no database, no clock, no page.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from blossom.captures import Author
from blossom.reconciliation import SourceChannel

InstructionState = Literal["current", "history", "awaiting"]
"""Current applies now; history was said and does not apply; awaiting is a school note
found in the old note field after the move, beside instructions already kept, which a
parent has not yet reviewed. Awaiting is never current and never reaches the planner."""
Card = Literal["assigned", "due"]
STATES: Final = frozenset({"current", "history", "awaiting"})
CARDS: Final = frozenset({"assigned", "due"})
CarriedState = Literal["nothing", "current", "awaiting"]


@dataclass(frozen=True)
class InstructionSeen:
    """One instruction as it was read: its words, the school channel, and the card it was
    under with that card's day. The card and its day say where the words were read, never
    when the teacher wrote or changed them."""

    text: str
    channel: SourceChannel | None
    card: Card | None = None
    card_day: date | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            msg = "an instruction has words"
            raise ValueError(msg)
        if self.card is not None and self.card not in CARDS:
            msg = f"an instruction is read under an assigned or a due card, not {self.card!r}"
            raise ValueError(msg)


class SchoolInstruction(BaseModel):
    """One of the school's instructions as it is kept.

    ``channel`` is the school channel it came from, or nothing for one carried
    over from the old note field without a mark. ``card`` and ``card_day`` say
    where it was first read. ``first_seen_at``, ``first_seen_on``, and
    ``imported_by`` say which paste first kept it and who pasted it, and are
    nothing for one carried over, whose moment is not known. ``settled_by`` and
    the moment beside it say who last chose its state, when anyone did.
    ``revision`` is the revision of the assignment's instructions at which this
    row last changed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int
    assignment_id: str
    text: str
    channel: SourceChannel | None
    card: Card | None
    card_day: date | None
    first_seen_at: AwareDatetime | None
    first_seen_on: date | None
    imported_by: Author | None
    state: InstructionState
    settled_by: Author | None
    settled_at: AwareDatetime | None
    settled_on: date | None
    revision: int


@dataclass(frozen=True)
class InstructionChoice:
    """A choice of which instructions apply, made against what the chooser was shown.

    ``shown`` is every instruction the chooser saw, kept and new; ``applies``
    is those chosen to apply, and ``none_applies`` says, on purpose, that none
    does. Choosing nothing is no answer, and choosing none beside some is a
    contradiction; neither is a choice.
    """

    shown_revision: int
    shown: tuple[str, ...]
    applies: frozenset[str] = field(default_factory=frozenset)
    none_applies: bool = False

    def __post_init__(self) -> None:
        if self.none_applies and self.applies:
            msg = "choosing that none applies and choosing some contradict each other"
            raise ValueError(msg)
        if not self.none_applies and not self.applies:
            msg = "a choice must answer: at least one applies, or none does"
            raise ValueError(msg)
        if not self.applies <= set(self.shown):
            msg = "a choice can only choose instructions it was shown"
            raise ValueError(msg)


@dataclass(frozen=True)
class InstructionsUnchanged:
    """Nothing new: no new text, and any choice given already stands in full."""


@dataclass(frozen=True)
class InstructionsSettled:
    """What a write does: new rows with their states, kept rows whose state changes, and the
    revision every one of them takes."""

    inserted: tuple[tuple[InstructionSeen, InstructionState], ...]
    changed: tuple[tuple[SchoolInstruction, InstructionState], ...]
    revision: int


@dataclass(frozen=True)
class InstructionsNeedAChoice:
    """New text that no rule may place: the kept instructions and the new ones, to be put to
    a parent with the revision they are shown at."""

    kept: tuple[SchoolInstruction, ...]
    new: tuple[InstructionSeen, ...]
    revision: int


@dataclass(frozen=True)
class InstructionChoiceStale:
    """A choice made against another revision, or another set of instructions, that asks for
    something other than what stands: nothing is written, and what stands is handed back."""

    kept: tuple[SchoolInstruction, ...]
    revision: int


InstructionsOutcome = (
    InstructionsUnchanged | InstructionsSettled | InstructionsNeedAChoice | InstructionChoiceStale
)


def revision_of(kept: Iterable[SchoolInstruction]) -> int:
    """The revision of an assignment's instructions: the latest any row changed at, or zero
    when none is kept."""
    return max((item.revision for item in kept), default=0)


def new_texts(
    kept: Sequence[SchoolInstruction], seen: Sequence[InstructionSeen]
) -> tuple[InstructionSeen, ...]:
    """What was read that is not kept already, once per text, the first reading of each."""
    known = {item.text for item in kept}
    fresh: dict[str, InstructionSeen] = {}
    for item in seen:
        if item.text not in known and item.text not in fresh:
            fresh[item.text] = item
    return tuple(fresh.values())


def settle(
    kept: Sequence[SchoolInstruction],
    seen: Sequence[InstructionSeen],
    choice: InstructionChoice | None,
) -> InstructionsOutcome:
    """What keeping these readings, with this choice or none, does to one assignment's kept
    instructions.

    With no choice: nothing new is nothing; the first text, with nothing kept
    and no other new text beside it, applies; anything else needs a choice. A
    choice that already stands in full, every new text kept and every state as
    chosen, is nothing new. Otherwise the choice must have been made against
    the revision that stands and against exactly the instructions it would
    place, kept and new; then every new text is kept, applying when chosen and
    as history when not, and every kept row takes the state chosen for it.
    """
    fresh = new_texts(kept, seen)
    revision = revision_of(kept)
    if choice is None:
        if not fresh:
            return InstructionsUnchanged()
        if not kept and len(fresh) == 1:
            return InstructionsSettled(((fresh[0], "current"),), (), revision + 1)
        return InstructionsNeedAChoice(tuple(kept), fresh, revision)
    wanted = {
        text: ("current" if text in choice.applies else "history")
        for text in [*(item.text for item in kept), *(item.text for item in fresh)]
    }
    if not fresh and all(item.state == wanted[item.text] for item in kept):
        return InstructionsUnchanged()
    if choice.shown_revision != revision or set(choice.shown) != set(wanted):
        return InstructionChoiceStale(tuple(kept), revision)
    inserted: tuple[tuple[InstructionSeen, InstructionState], ...] = tuple(
        (item, "current" if item.text in choice.applies else "history") for item in fresh
    )
    changed: tuple[tuple[SchoolInstruction, InstructionState], ...] = tuple(
        (item, "current" if item.text in choice.applies else "history")
        for item in kept
        if item.state != wanted[item.text]
    )
    return InstructionsSettled(inserted, changed, revision + 1)


def carried_state(kept: Sequence[SchoolInstruction], text: str) -> CarriedState:
    """Where a school note in the old note field goes, at the move or found there later: it
    is nothing new when its text is kept already, it applies when nothing is kept, and it
    waits for a parent's review beside what is kept otherwise. Nothing here decides that it
    replaces what applies."""
    if any(item.text == text for item in kept):
        return "nothing"
    return "current" if not kept else "awaiting"


@dataclass(frozen=True)
class InstructionsStanding:
    """One assignment's instructions as a reader shows them: those that apply, in the one
    order, then those said before and those awaiting review, in the order kept, and the
    revision a choice is made against.

    The one order is by text, code point by code point: the same set of
    instructions reads the same whichever was read first, so a planner told
    them and the fingerprint drawn from what it is told never depend on the
    order anything was pasted in. The order means nothing else.
    """

    current: tuple[SchoolInstruction, ...]
    history: tuple[SchoolInstruction, ...]
    awaiting: tuple[SchoolInstruction, ...]
    revision: int

    @property
    def texts(self) -> tuple[str, ...]:
        """The words of the instructions that apply, in the one order."""
        return tuple(item.text for item in self.current)

    @property
    def kept(self) -> tuple[SchoolInstruction, ...]:
        """Every kept instruction, in the order kept."""
        return tuple(
            sorted((*self.current, *self.history, *self.awaiting), key=lambda item: item.sequence)
        )


def standing_of(kept: Iterable[SchoolInstruction]) -> InstructionsStanding:
    """The standing of one assignment's kept instructions."""
    rows = sorted(kept, key=lambda item: item.sequence)
    return InstructionsStanding(
        current=tuple(
            sorted((item for item in rows if item.state == "current"), key=lambda item: item.text)
        ),
        history=tuple(item for item in rows if item.state == "history"),
        awaiting=tuple(item for item in rows if item.state == "awaiting"),
        revision=revision_of(rows),
    )
