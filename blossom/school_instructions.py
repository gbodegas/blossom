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
Beside it is the one way an instruction's words travel in a form, so what a
browser sends back names exactly the words that are kept. Words are words
however long: a note kept from before the school's instructions were kept
apart had no limit, and moves whole. A form carries in words only what a
paste can bring; a longer instruction kept from before travels by its row,
and is put back in its words against what is kept.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from blossom.captures import Author
from blossom.reconciliation import SCHOOL_CHANNELS, SourceChannel

InstructionState = Literal["current", "history", "awaiting"]
"""Current applies now; history was said and does not apply; awaiting is a school note
found in the old note field after the move, beside instructions already kept, which a
parent has not yet reviewed. Awaiting is never current and never reaches the planner."""
Card = Literal["assigned", "due"]
STATES: Final = frozenset({"current", "history", "awaiting"})
CARDS: Final = frozenset({"assigned", "due"})
CarriedState = Literal["nothing", "current", "awaiting"]
INSTRUCTION_MAX_LENGTH: Final = 40_000
"""The longest instruction a form carries in its words: the longest text a paste may be, so
every instruction a paste brings travels in its words. A longer one kept from before travels
by its row."""
WIRE_MAX_LENGTH: Final = 12 * INSTRUCTION_MAX_LENGTH + 2
"""The longest an instruction's words are in a form: every character at its longest
escape, a character beyond the basic plane as two escaped halves, and the two quotes."""


def instruction_words(text: object) -> bool:
    """Whether a value can be an instruction's words: text with something in it that is not
    white space, and writable as it is. Its length is not limited here: a note kept from
    before the instructions were kept apart had no limit, and is kept whole."""
    if not isinstance(text, str) or not text.strip():
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def travels_in_words(text: str) -> bool:
    """Whether a form carries these words as they are: they fit in a paste. A longer
    instruction, kept from before, travels by its row."""
    return len(text) <= INSTRUCTION_MAX_LENGTH


def to_wire(text: str) -> str:
    """An instruction's words as a form carries them: one JSON string in ASCII. A browser
    turns every line break it sends into a carriage return and a line feed; the words on
    the wire hold no line break, no quote, and nothing beyond ASCII, so they come back as
    they left, line endings, quotes, and every character included."""
    return json.dumps(text, ensure_ascii=True)


def from_wire(value: str) -> str | None:
    """The words a form carried, exactly, or ``None`` for a value the page did not write: one
    that is too long, is no JSON string, or decodes to no words a form carries."""
    if len(value) > WIRE_MAX_LENGTH or not value.startswith('"') or not value.endswith('"'):
        return None
    try:
        text = json.loads(value)
    except ValueError:
        return None
    return text if instruction_words(text) and travels_in_words(text) else None


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
        if not instruction_words(self.text):
            msg = "an instruction has words that can be written as they are"
            raise ValueError(msg)
        if self.channel is not None and self.channel not in SCHOOL_CHANNELS:
            msg = "an instruction is the school's: from the portal, the email, or not known"
            raise ValueError(msg)
        if self.card is not None and self.card not in CARDS:
            msg = "an instruction is read under an assigned or a due card"
            raise ValueError(msg)
        if self.card_day is not None and (
            not isinstance(self.card_day, date) or isinstance(self.card_day, datetime)
        ):
            msg = "a card's day is a day"
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
class SubmittedChoice:
    """An answer as a form sent it, whatever it says: the revision and the instructions it
    was made against, those ticked, and whether none applying was ticked.

    It is kept whole even when it contradicts itself or answers nothing, so a
    page returned can say what was sent, and can tell whether it was made
    against what stands before it gives any tick back: a tick made against
    instructions that have changed since is never put on a form carrying the
    revision that stands now. A kept instruction too long to travel in its
    words is named by its row, in ``rows`` and ``applies_rows``, until the
    answer is put back in words against what is kept.
    """

    shown_revision: int
    shown: tuple[str, ...]
    applies: frozenset[str] = field(default_factory=frozenset)
    none_applies: bool = False
    rows: tuple[int, ...] = ()
    applies_rows: frozenset[int] = field(default_factory=frozenset)

    @property
    def contradicts(self) -> bool:
        """Whether it ticks some instructions and that none applies."""
        return self.none_applies and bool(self.applies or self.applies_rows)

    @property
    def answers(self) -> bool:
        """Whether it is a choice: some apply, or none does, and not both."""
        return bool(self.applies or self.applies_rows) != self.none_applies

    def resolved(self, kept: Iterable[SchoolInstruction]) -> "SubmittedChoice | None":
        """The answer with every row it names put back in that row's words, or ``None`` when it
        names a row not kept here, or the same words twice. A kept row's words never change,
        so the words a row names now are the words it was shown with."""
        if not self.rows:
            return self
        words = {item.sequence: item.text for item in kept}
        if not set(self.rows) <= words.keys():
            return None
        shown = (*self.shown, *(words[row] for row in self.rows))
        if len(set(shown)) != len(shown):
            return None
        return SubmittedChoice(
            shown_revision=self.shown_revision,
            shown=shown,
            applies=self.applies | {words[row] for row in self.applies_rows},
            none_applies=self.none_applies,
        )

    def choice(self) -> "InstructionChoice | None":
        """The choice it makes, or ``None`` when it makes none. An answer that names rows is
        put back in words first."""
        if self.rows:
            msg = "an answer that names kept rows is put back in their words before it chooses"
            raise ValueError(msg)
        if not self.answers:
            return None
        return InstructionChoice(
            shown_revision=self.shown_revision,
            shown=self.shown,
            applies=self.applies,
            none_applies=self.none_applies,
        )

    def made_against(self, revision: int, texts: Iterable[str]) -> bool:
        """Whether it was made against these instructions at this revision; an answer that
        still names rows is put back in words first, and until then says it was not."""
        return not self.rows and self.shown_revision == revision and set(self.shown) == set(texts)


@dataclass(frozen=True)
class InstructionsUnchanged:
    """Nothing new: no new text, and any choice given already stands in full. ``revision``
    is the revision that stands, the one the outcome was decided against."""

    revision: int = 0


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
    choice of words neither kept nor new is refused. A choice that already
    stands in full, every new text kept and every state as chosen, is nothing
    new. Otherwise the choice must have been made against
    the revision that stands and against exactly the instructions it would
    place, kept and new; then every new text is kept, applying when chosen and
    as history when not, and every kept row takes the state chosen for it.
    """
    fresh = new_texts(kept, seen)
    revision = revision_of(kept)
    if choice is None:
        if not fresh:
            return InstructionsUnchanged(revision)
        if not kept and len(fresh) == 1:
            return InstructionsSettled(((fresh[0], "current"),), (), revision + 1)
        return InstructionsNeedAChoice(tuple(kept), fresh, revision)
    # A choice of words that are neither kept nor new in this text asks for something that
    # cannot stand, so it is never taken for a result that already stands.
    available = {item.text for item in kept} | {item.text for item in fresh}
    if not choice.applies <= available:
        return InstructionChoiceStale(tuple(kept), revision)
    wanted = {
        text: ("current" if text in choice.applies else "history")
        for text in [*(item.text for item in kept), *(item.text for item in fresh)]
    }
    if not fresh and all(item.state == wanted[item.text] for item in kept):
        return InstructionsUnchanged(revision)
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
