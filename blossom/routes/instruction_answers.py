"""Reading an answer about the school's instructions from a form, one rule for the paste review
and for the family's review of one assignment.

A page that asks which instructions apply writes, for each question, the
revision it showed them at, each instruction's words as they travel on the
wire, or the row of a kept instruction too long to travel in words, a box for
each, and a separate box for none applying. A browser sends back the hidden
fields as written and a ticked box as ``1``; an unticked box is left out of
the form, and that is no answer about it, never a malformed one. Anything else
is not a form the page made: a field sent twice, a file in place of text, a
name the page never writes, words that do not decode, a revision or a row not
spelled as the page spells a count, a box ticked with another value, or a box
for words the page did not write. Such a question is no answer, and the form
is refused whole before anything is written.

A readable answer is kept whole even when it contradicts itself, so the page
returned can show what was sent, and can tell whether it was made against the
instructions as they stand. What a question that cannot be read chose is read
apart, from the first text value of each field, as the family's other forms
keep what was typed: the words ticked that decode, the rows ticked, and
whether none applying was. It is said back as not saved and nothing else.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from blossom.school_instructions import SchoolInstruction, SubmittedChoice, from_wire

KEY_MAX_LENGTH: Final = 6
REVISION_MAX_LENGTH: Final = 9
ROW_MAX_LENGTH: Final = 18
"""Longer than any row the store numbers; a longer value is no row."""


def count_of(value: str, longest: int) -> int | None:
    """A count as the page writes one, or ``None``: ASCII digits, no more than ``longest`` of
    them, spelled as the count is, so a padded or signed spelling names nothing."""
    if (
        0 < len(value) <= longest
        and value.isascii()
        and value.isdigit()
        and value == str(int(value))
    ):
        return int(value)
    return None


def review_key(value: str) -> bool:
    """Whether ``value`` is a number the page writes: one to six ASCII digits, spelled as the
    count is, so no padded spelling names another field's place."""
    return count_of(value, KEY_MAX_LENGTH) is not None


@dataclass
class AnswerFields:
    """The fields of one question as the form sent them, the first text value of each, not
    yet read."""

    revision: str | None = None
    words: dict[str, str] = field(default_factory=dict)
    rows: dict[str, str] = field(default_factory=dict)
    ticks: dict[str, str] = field(default_factory=dict)
    none: str | None = None


def answer_of(fields: AnswerFields) -> SubmittedChoice | None:
    """One question's answer as it was sent, or ``None`` when its fields are not ones the page
    writes. Words are read off the wire exactly and a row as the count the page wrote; the
    same words or row twice, a place with both, a box for neither, and any value but ``1``
    on a box are no answer."""
    revision = None if fields.revision is None else count_of(fields.revision, REVISION_MAX_LENGTH)
    if revision is None:
        return None
    words: dict[int, str] = {}
    for place, value in fields.words.items():
        text = from_wire(value)
        if not review_key(place) or text is None:
            return None
        words[int(place)] = text
    rows: dict[int, int] = {}
    for place, value in fields.rows.items():
        row = count_of(value, ROW_MAX_LENGTH)
        if not review_key(place) or row is None or int(place) in words:
            return None
        rows[int(place)] = row
    shown = tuple(words[place] for place in sorted(words))
    shown_rows = tuple(rows[place] for place in sorted(rows))
    if len(set(shown)) != len(shown) or len(set(shown_rows)) != len(shown_rows):
        return None
    applies: set[str] = set()
    applies_rows: set[int] = set()
    for place, value in fields.ticks.items():
        if not review_key(place) or value != "1":
            return None
        if int(place) in words:
            applies.add(words[int(place)])
        elif int(place) in rows:
            applies_rows.add(rows[int(place)])
        else:
            return None
    if fields.none not in (None, "1"):
        return None
    return SubmittedChoice(
        shown_revision=revision,
        shown=shown,
        applies=frozenset(applies),
        none_applies=fields.none == "1",
        rows=shown_rows,
        applies_rows=frozenset(applies_rows),
    )


@dataclass(frozen=True)
class UnsavedChoice:
    """What an answer that was not saved chose, as far as it can be read: the words ticked, the
    kept rows ticked, and whether none applying was ticked. It is said back and nothing else:
    it carries no revision, and nothing is ever saved from it."""

    applies: tuple[str, ...] = ()
    rows: tuple[int, ...] = ()
    none_applies: bool = False

    @classmethod
    def of(cls, answer: SubmittedChoice) -> "UnsavedChoice":
        """A readable answer that was not saved, to say back."""
        return cls(
            tuple(sorted(answer.applies)), tuple(sorted(answer.applies_rows)), answer.none_applies
        )

    @property
    def chooses(self) -> bool:
        """Whether anything was ticked: some instruction, or that none applies."""
        return bool(self.applies or self.rows or self.none_applies)


def unsaved_of(fields: AnswerFields) -> UnsavedChoice:
    """What a question chose, read from the first text value of each of its fields: a box
    ticked as the page ticks one, for words that decode or for a row as the page spells one.
    Nothing that cannot be read is said, and nothing is made up in its place."""
    applies: list[str] = []
    rows: list[int] = []
    for place, value in fields.ticks.items():
        if value != "1" or not review_key(place):
            continue
        words = fields.words.get(place)
        row = fields.rows.get(place)
        if words is not None and row is None:
            text = from_wire(words)
            if text is not None and text not in applies:
                applies.append(text)
        elif row is not None and words is None:
            number = count_of(row, ROW_MAX_LENGTH)
            if number is not None and number not in rows:
                rows.append(number)
    return UnsavedChoice(tuple(applies), tuple(rows), fields.none == "1")


@dataclass(frozen=True)
class SaidBack:
    """An answer not saved as a page says it: its words in the one order, how many
    instructions it selected by reference to a row whose text this page could not read, and
    whether it chose that none applies."""

    words: tuple[str, ...]
    unshown: int
    none_applies: bool


def said_back(
    unsaved: UnsavedChoice | None, kept: Sequence[SchoolInstruction] | None
) -> SaidBack | None:
    """What a page says of an answer not saved, or ``None`` when it chose nothing. A row it
    chose is put in its words from what the page read, and counted when the page read
    nothing; a row not kept for this assignment is no choice of its, and is not said."""
    if unsaved is None:
        return None
    words = list(unsaved.applies)
    unshown = 0
    if kept is None:
        unshown = len(unsaved.rows)
    else:
        by_row = {item.sequence: item.text for item in kept}
        words.extend(by_row[row] for row in unsaved.rows if row in by_row)
    ordered = tuple(sorted(dict.fromkeys(words)))
    if not ordered and not unshown and not unsaved.none_applies:
        return None
    return SaidBack(ordered, unshown, unsaved.none_applies)


WAITING_HEADS: Final = frozenset({"waiting", "waiting_unshown", "waiting_none"})


def waiting_choices(items: Iterable[tuple[str, object]]) -> dict[int, SaidBack]:
    """What each card says back of a choice made before it was known which homework the card
    is, as the paste review carries it: ``waiting-<card>-<place>`` for each instruction's
    words on the wire, ``waiting_unshown-<card>`` for how many were selected by reference,
    and ``waiting_none-<card>`` when none applying was chosen.

    It is said back and nothing else: it carries no revision, and nothing is
    ever saved from it. The first text value of each field is read; what
    cannot be read is left out, and nothing is made up in its place.
    """
    words: dict[int, list[str]] = {}
    unshown: dict[int, int] = {}
    none: set[int] = set()
    seen: set[str] = set()
    for name, value in items:
        head, _, rest = name.partition("-")
        if head not in WAITING_HEADS or name in seen or not isinstance(value, str):
            continue
        seen.add(name)
        parts = rest.split("-")
        if not review_key(parts[0]):
            continue
        card = int(parts[0])
        if head == "waiting" and len(parts) == 2 and review_key(parts[1]):
            text = from_wire(value)
            if text is not None:
                words.setdefault(card, []).append(text)
        elif head == "waiting_unshown" and len(parts) == 1:
            count = count_of(value, KEY_MAX_LENGTH)
            if count is not None:
                unshown[card] = count
        elif head == "waiting_none" and len(parts) == 1 and value == "1":
            none.add(card)
    return {
        card: SaidBack(
            tuple(sorted(dict.fromkeys(words.get(card, [])))),
            unshown.get(card, 0),
            card in none,
        )
        for card in sorted({*words, *unshown, *none})
    }


@dataclass(frozen=True)
class ReadReview:
    """What the family's review form said: its answer, when every field is one the page
    writes, and what it chose, to say back when it is not."""

    answer: SubmittedChoice | None
    unsaved: UnsavedChoice


def review_answers(items: Iterable[tuple[str, object]]) -> ReadReview:
    """The answer on the family's review of one assignment, read from every field as sent. That
    page writes ``revision``, ``instruction-<place>`` or ``row-<place>``, ``apply-<place>``,
    and ``none``, and nothing else. A field sent twice, a file, or a name the page never
    writes makes the form no answer; the first text value of each field is still read for
    what it chose."""
    fields = AnswerFields()
    seen: set[str] = set()
    whole = True
    for name, value in items:
        if name in seen or not isinstance(value, str):
            whole = False
            continue
        seen.add(name)
        head, _, place = name.partition("-")
        if name == "revision":
            fields.revision = value
        elif name == "none":
            fields.none = value
        elif head == "instruction" and place:
            fields.words[place] = value
        elif head == "row" and place:
            fields.rows[place] = value
        elif head == "apply" and place:
            fields.ticks[place] = value
        else:
            whole = False
    return ReadReview(answer_of(fields) if whole else None, unsaved_of(fields))


def review_answer(fields: Mapping[str, str]) -> SubmittedChoice | None:
    """The answer on the family's review of one assignment from fields sent once each, or
    ``None`` for a form the page did not make."""
    return review_answers(fields.items()).answer


@dataclass(frozen=True)
class ReadAnswers:
    """What a form said about the school's instructions: each readable answer by the card it
    was on, whether anything in the form was not what the page writes, and what each card
    that could not be read chose, to say back."""

    answers: dict[int, SubmittedChoice]
    malformed: bool
    unsaved: dict[int, UnsavedChoice] = field(default_factory=dict)


PASTE_HEADS: Final = frozenset({"instructions", "instruction", "row", "apply", "none"})


def paste_answers(items: Iterable[tuple[str, object]]) -> ReadAnswers:
    """The instruction answers on a paste review's form, card by card, read from every field
    as sent, before any is collapsed.

    The paste review writes ``instructions-<card>`` for the revision,
    ``instruction-<card>-<place>`` for the words, or ``row-<card>-<place>``
    for a kept instruction too long to travel in words, ``apply-<card>-<place>``
    for a box, and ``none-<card>``. Every other field of the form is the
    review's own, read by its own reader. A field of these names sent twice,
    sent as a file, or named another way is malformed, and so is a card whose
    fields are not an answer the page could have written. What such a card
    chose, as far as it can be read, is kept to say back; every other card's
    answer stands as it was sent.
    """
    cards: dict[int, AnswerFields] = {}
    bent: set[int] = set()
    seen: set[str] = set()
    malformed = False
    for name, value in items:
        head, _, rest = name.partition("-")
        if head not in PASTE_HEADS:
            continue
        parts = rest.split("-")
        card = int(parts[0]) if review_key(parts[0]) else None
        whole = 1 if head in ("instructions", "none") else 2
        if name in seen or not isinstance(value, str) or card is None or len(parts) != whole:
            malformed = True
            if card is not None:
                bent.add(card)
            continue
        seen.add(name)
        fields = cards.setdefault(card, AnswerFields())
        if head == "instructions":
            fields.revision = value
        elif head == "none":
            fields.none = value
        elif head == "instruction":
            fields.words[parts[1]] = value
        elif head == "row":
            fields.rows[parts[1]] = value
        else:
            fields.ticks[parts[1]] = value
    answers: dict[int, SubmittedChoice] = {}
    unsaved: dict[int, UnsavedChoice] = {}
    for key, fields in cards.items():
        answer = None if key in bent else answer_of(fields)
        if answer is None:
            malformed = True
            unsaved[key] = unsaved_of(fields)
            continue
        answers[key] = answer
    return ReadAnswers(answers, malformed, unsaved)
