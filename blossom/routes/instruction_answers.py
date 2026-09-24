"""Reading an answer about the school's instructions from a form, one rule for the paste review
and for the family's review of one assignment.

A page that asks which instructions apply writes, for each question, the
revision it showed them at, each instruction's words as they travel on the
wire, a box for each, and a separate box for none applying. A browser sends
back the hidden fields as written and a ticked box as ``1``; an unticked box is
left out of the form, and that is no answer about it, never a malformed one.
Anything else is not a form the page made: a field sent twice, a file in place
of text, a name the page never writes, words that do not decode, a revision
that is not a count, a box ticked with another value, or a box for words the
page did not write. Such a question is no answer, and the form is refused
whole before anything is written.

A readable answer is kept whole even when it contradicts itself, so the page
returned can show what was sent, and can tell whether it was made against the
instructions as they stand.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from blossom.school_instructions import SubmittedChoice, from_wire

KEY_MAX_LENGTH: Final = 6
REVISION_MAX_LENGTH: Final = 9


def review_key(value: str) -> bool:
    """Whether ``value`` is a number the page writes: one to six ASCII digits, spelled as the
    count is, so no padded spelling names another field's place."""
    return (
        0 < len(value) <= KEY_MAX_LENGTH
        and value.isascii()
        and value.isdigit()
        and value == str(int(value))
    )


@dataclass
class AnswerFields:
    """The fields of one question as the form sent them, not yet read."""

    revision: str | None = None
    words: dict[str, str] = field(default_factory=dict)
    ticks: dict[str, str] = field(default_factory=dict)
    none: str | None = None


def answer_of(fields: AnswerFields) -> SubmittedChoice | None:
    """One question's answer as it was sent, or ``None`` when its fields are not ones the page
    writes. Words are read off the wire exactly; the same words twice, a box without words,
    and any value but ``1`` on a box are no answer."""
    revision = fields.revision
    if revision is None or not (
        revision.isascii() and revision.isdigit() and len(revision) <= REVISION_MAX_LENGTH
    ):
        return None
    decoded: dict[int, str] = {}
    for place, value in fields.words.items():
        text = from_wire(value)
        if not review_key(place) or text is None:
            return None
        decoded[int(place)] = text
    shown = tuple(decoded[place] for place in sorted(decoded))
    if len(set(shown)) != len(shown):
        return None
    applies: set[str] = set()
    for place, value in fields.ticks.items():
        if not review_key(place) or value != "1" or int(place) not in decoded:
            return None
        applies.add(decoded[int(place)])
    if fields.none not in (None, "1"):
        return None
    return SubmittedChoice(
        shown_revision=int(revision),
        shown=shown,
        applies=frozenset(applies),
        none_applies=fields.none == "1",
    )


@dataclass(frozen=True)
class ReadAnswers:
    """What a form said about the school's instructions: each readable answer by the card it
    was on, and whether anything in the form was not what the page writes."""

    answers: dict[int, SubmittedChoice]
    malformed: bool


PASTE_HEADS: Final = frozenset({"instructions", "instruction", "apply", "none"})


def paste_answers(items: Iterable[tuple[str, object]]) -> ReadAnswers:
    """The instruction answers on a paste review's form, card by card, read from every field
    as sent, before any is collapsed.

    The paste review writes ``instructions-<card>`` for the revision,
    ``instruction-<card>-<place>`` for the words, ``apply-<card>-<place>`` for
    a box, and ``none-<card>``. Every other field of the form is the review's
    own, read by its own reader. A field of these names sent twice, sent as a
    file, or named another way is malformed, and so is a card whose fields are
    not an answer the page could have written.
    """
    cards: dict[int, AnswerFields] = {}
    seen: set[str] = set()
    malformed = False
    for name, value in items:
        head, _, rest = name.partition("-")
        if head not in PASTE_HEADS:
            continue
        if name in seen or not isinstance(value, str):
            malformed = True
            continue
        seen.add(name)
        parts = rest.split("-")
        if head in ("instructions", "none"):
            if len(parts) != 1 or not review_key(parts[0]):
                malformed = True
                continue
            card = cards.setdefault(int(parts[0]), AnswerFields())
            if head == "instructions":
                card.revision = value
            else:
                card.none = value
            continue
        if len(parts) != 2 or not review_key(parts[0]):
            malformed = True
            continue
        card = cards.setdefault(int(parts[0]), AnswerFields())
        (card.words if head == "instruction" else card.ticks)[parts[1]] = value
    answers: dict[int, SubmittedChoice] = {}
    for key, fields in cards.items():
        answer = answer_of(fields)
        if answer is None:
            malformed = True
            continue
        answers[key] = answer
    return ReadAnswers(answers, malformed)


def review_answer(fields: Mapping[str, str]) -> SubmittedChoice | None:
    """The answer on the family's review of one assignment, or ``None`` for a form the page
    did not make. That page writes ``revision``, ``instruction-<place>``, ``apply-<place>``,
    and ``none``, and nothing else; the caller has already refused a field sent twice or as
    a file."""
    answer = AnswerFields()
    for name, value in fields.items():
        head, _, place = name.partition("-")
        if name == "revision":
            answer.revision = value
        elif name == "none":
            answer.none = value
        elif head == "instruction" and place:
            answer.words[place] = value
        elif head == "apply" and place:
            answer.ticks[place] = value
        else:
            return None
    return answer_of(answer)
