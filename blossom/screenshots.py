# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""What a homework screenshot's reading may hold, checked in code before any of it is kept.

The reader answers in JSON by instruction alone, so every answer is checked against the
answer schema, ``screenshot-reading/1``, with closed models: frozen, refusing an unknown
field, hiding their input from any error, with closed choices and bounded text. An answer
that doesn't fit is a failed read, never repaired.

The image's kind is read first. A homework list keeps its day headings, its cards and the
selected student's name as read, with the reader's note on it, inside the reading only. Any
other kind keeps its kind alone. A card that doesn't fit stays at its place as one that
couldn't be read. A report's cells, the teacher's among them, are declared so that a report
fits, and none is kept. Dates are kept as the text shown and read as dates here.

A draft's readings are put together into rows by their content alone, never by the order
the screenshots came in, and read as today's portal claims through ``intake.read_cards``.
``homework_rows`` gives the draft store each row's key from the same assembly, so the rows
the parent sees and the keys the store keeps can't disagree.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Annotated, Final, Literal, NamedTuple, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from blossom.grades.draft import folded
from blossom.intake import DAY_LINE, PortalCard, Reading, a_date, read_cards
from blossom.school_instructions import instruction_words
from blossom.screenshot_prompt import PROMPT_VERSION, SCHEMA_VERSION
from blossom.stores.import_drafts import DraftRow, StoredReading

NAME_MOST: Final = 100
"""The most characters of a student's name as read."""
NOTE_MOST: Final = 300
"""The most characters of a note the reader gives on why a field is uncertain."""
LINE_MOST: Final = 200
"""The most characters of a day heading, a card's course or its due line."""
TITLE_MOST: Final = 300
"""The most characters of an assignment's title."""
INSTRUCTION_MOST: Final = 4_000
"""The most characters of a teacher's instruction under a card."""
CELL_MOST: Final = 300
"""The most characters of one cell of a grade report."""
MOST_HEADINGS: Final = 40
POSITION_MOST: Final = 1_000
"""The bottom edge of the image, in the thousandths of its height positions are given in."""
MOST_CARDS: Final = 60
MOST_MARKS: Final = 20
"""The most uncertain fields one item may list."""
MOST_CATEGORIES: Final = 30
MOST_ROWS: Final = 200

ImageKind = Literal["homework_list", "grade_report", "unsupported"]
Badge = Literal["DUE", "ASSIGNED"]
CardField = Literal["badge", "course", "title", "instruction", "footer"]
ReportField = Literal[
    "school_year", "term", "class_code", "class_name", "teacher", "term_percent", "term_letter"
]
CategoryField = Literal["name", "weight", "average"]
RowField = Literal[
    "title", "due", "points", "max", "average", "status", "curve", "bonus", "penalty", "note"
]
ReadingFailure = Literal["not_valid"]
"""How a checked answer can fail, named as the import drafts store keeps failures."""


Name = Annotated[str, Field(strict=True, max_length=NAME_MOST)]
Note = Annotated[str, Field(strict=True, max_length=NOTE_MOST)]
Line = Annotated[str, Field(strict=True, max_length=LINE_MOST)]
Title = Annotated[str, Field(strict=True, max_length=TITLE_MOST)]
Instruction = Annotated[str, Field(strict=True, max_length=INSTRUCTION_MOST)]
Cell = Annotated[str, Field(strict=True, max_length=CELL_MOST)]
Position = Annotated[int, Field(strict=True, ge=0, le=POSITION_MOST)]

FOOTER: Final = re.compile(
    r"^Due\s*\(\s*(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})\s*\)$", re.IGNORECASE
)
"""A card's due line as the portal shows it, such as "Due (10/8/2026)"."""


class _Closed(BaseModel):
    """A model that refuses an unknown field, can't change, and keeps input out of errors."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class CardMark(_Closed):
    """A card field the reader marked uncertain, and its reason."""

    field: CardField
    reason: Note


class Card(_Closed):
    """One course card as read: each field as shown, or ``None`` where it isn't visible."""

    badge: Badge | None
    course: Line | None
    title: Title | None
    instruction: Instruction | None
    footer: Line | None
    y_top: Position
    y_bottom: Position
    clipped: StrictBool
    uncertain: Annotated[tuple[CardMark, ...], Field(max_length=MOST_MARKS)]

    @model_validator(mode="after")
    def _edges_in_order(self) -> Self:
        if self.y_bottom < self.y_top:
            message = "a card's bottom edge is above its top"
            raise ValueError(message)
        return self

    @property
    def due(self) -> date | None:
        """The date the due line names, read here from its text, or ``None``."""
        found = None if self.footer is None else FOOTER.match(self.footer.strip())
        return None if found is None else a_date(found["year"], found["month"], found["day"])

    @property
    def uncertain_fields(self) -> frozenset[str]:
        """The fields the reader marked uncertain."""
        return frozenset(mark.field for mark in self.uncertain)

    @property
    def complete(self) -> bool:
        """Whether its label, course and title were all read, the course and title with words."""
        return self.badge is not None and _words(self.course) and _words(self.title)

    @property
    def certain(self) -> bool:
        """Whether it is read whole: complete, unclipped, and with no field marked uncertain,
        so it can stand as evidence that it is the same card as another."""
        return self.complete and not self.clipped and not self.uncertain


class Heading(_Closed):
    """One day heading as read, with the reader's note when it is uncertain."""

    text: Line
    y: Position
    uncertain: Note | None

    @property
    def day(self) -> date | None:
        """The day the heading names, read here from its text, or ``None``."""
        found = DAY_LINE.match(self.text.strip())
        return None if found is None else a_date(found["year"], found["month"], found["day"])


class Student(_Closed):
    """The selected student's name as read, and the reader's note on it; never logged."""

    name: Name | None
    uncertain: Note | None


class _ReportMark(_Closed):
    field: ReportField
    reason: Note


class _CategoryMark(_Closed):
    field: CategoryField
    reason: Note


class _RowMark(_Closed):
    field: RowField
    reason: Note


class _Row(_Closed):
    title: Cell | None
    due: Cell | None
    points: Cell | None
    max: Cell | None
    average: Cell | None
    status: Cell | None
    curve: Cell | None
    bonus: Cell | None
    penalty: Cell | None
    note: Cell | None
    y_top: StrictInt
    y_bottom: StrictInt
    uncertain: Annotated[tuple[_RowMark, ...], Field(max_length=MOST_MARKS)]


class _Category(_Closed):
    name: Cell | None
    weight: Cell | None
    average: Cell | None
    rows: Annotated[tuple[_Row, ...], Field(max_length=MOST_ROWS)]
    uncertain: Annotated[tuple[_CategoryMark, ...], Field(max_length=MOST_MARKS)]


class ReportAnswer(_Closed):
    """A grade report as the schema shapes it. Checked so that a report fits; never kept here,
    so its teacher cell and that cell's note go with it."""

    header_visible: StrictBool
    school_year: Cell | None
    term: Cell | None
    class_code: Cell | None
    class_name: Cell | None
    teacher: Cell | None
    categories: Annotated[tuple[_Category, ...], Field(max_length=MOST_CATEGORIES)]
    term_grade_visible: StrictBool
    term_percent: Cell | None
    term_letter: Cell | None
    uncertain: Annotated[tuple[_ReportMark, ...], Field(max_length=MOST_MARKS)]


class _Answer(_Closed):
    """The whole answer. Each card is checked on its own, so one that doesn't fit fails alone."""

    image_kind: ImageKind
    active_student: Student
    headings: Annotated[tuple[Heading, ...], Field(max_length=MOST_HEADINGS)]
    cards: Annotated[tuple[object, ...], Field(max_length=MOST_CARDS)]
    report: ReportAnswer | None


class HomeworkReading(_Closed):
    """What one screenshot's reading keeps: its kind, and for a homework list the selected
    student, the headings and the cards in the order read. ``None`` among the cards is a card
    that couldn't be read, at its place. Its JSON is the stored reading."""

    image_kind: ImageKind
    student: Student
    headings: tuple[Heading, ...]
    cards: tuple[Card | None, ...]

    @property
    def wrong_kind(self) -> bool:
        """Whether the image isn't a homework list, so the reading has no cards."""
        return self.image_kind != "homework_list"


class Checked(NamedTuple):
    """A checked answer: the reading kept from it, or why there is none."""

    reading: HomeworkReading | None
    failure: ReadingFailure | None


def homework_reading(answer: object) -> Checked:
    """Check a parsed answer and keep what a homework draft may hold of it.

    An answer that doesn't fit the schema is ``not_valid``. Nothing is logged, and no error
    leaves here, so nothing of the answer reaches a log or a traceback.
    """
    checked: _Answer | None = None
    try:
        checked = _Answer.model_validate(answer)
    except (ValueError, RecursionError):
        checked = None
    if checked is None:
        return Checked(None, "not_valid")
    if checked.image_kind != "homework_list":
        nobody = Student(name=None, uncertain=None)
        return Checked(
            HomeworkReading(image_kind=checked.image_kind, student=nobody, headings=(), cards=()),
            None,
        )
    return Checked(
        HomeworkReading(
            image_kind=checked.image_kind,
            student=checked.active_student,
            headings=checked.headings,
            cards=tuple(_card(value) for value in checked.cards),
        ),
        None,
    )


def _card(value: object) -> Card | None:
    """One card as read, or ``None`` when it doesn't fit, so it shows as one not read."""
    try:
        return Card.model_validate(value)
    except (ValueError, RecursionError):
        return None


# ------------------------------------------------------------------ from readings to rows

KEY_INSTANT: Final = datetime(1970, 1, 1, tzinfo=UTC)
"""The instant the rows function stamps its claims with. No key includes a claim's instant,
so the keys never read the clock."""


class ScreenshotRow(NamedTuple):
    """One row the screenshots read as: its key, the reading it would write, and the fields
    marked uncertain on any card it came from, the day heading among them."""

    key: str
    reading: Reading
    marks: frozenset[str]


class UnreadCard(NamedTuple):
    """A card that couldn't be read as a row, or a stored reading that couldn't be decoded
    (``place`` and ``card`` then ``None``), keyed by what was read of it."""

    key: str
    sha256: str
    place: int | None
    card: Card | None


class Assembly(NamedTuple):
    """What a draft's stored readings read as: its rows, and what couldn't be read."""

    rows: tuple[ScreenshotRow, ...]
    unread: tuple[UnreadCard, ...]


class _Seen(NamedTuple):
    """A card at its place in its image, with the day heading it sits under, if one is known."""

    sha256: str
    place: int
    card: Card | None
    heading: Heading | None


def homework_rows(readings: Sequence[StoredReading]) -> tuple[DraftRow, ...]:
    """The homework rows function: every row key a draft's readings give, each a row or a
    "couldn't read" item. It reads nothing but the readings, and gives the same keys for the
    same readings in any order."""
    assembly = homework_assembly(readings, now=KEY_INSTANT)
    keys = [DraftRow(row.key, True) for row in assembly.rows]
    keys += [DraftRow(item.key, False) for item in assembly.unread]
    return tuple(sorted(keys))


def homework_assembly(readings: Sequence[StoredReading], *, now: datetime) -> Assembly:
    """Put a draft's readings together, by their content alone, never their order.

    The cards of every reading are one set. A day heading carries to another screenshot's top
    cards only through a proven overlap. A clipped card goes when a whole copy of it is read.
    The rest are walked in an order fixed by content and read as today's portal claims; a card
    with no date, an unread course or title, or a clip no whole copy covers, is an item that
    couldn't be read. ``now`` stamps the claims and is in no key.
    """
    images: dict[str, HomeworkReading | None] = {}
    for stored in readings:
        images[stored.sha256] = _decoded(stored)
    unread: dict[str, UnreadCard] = {}
    placed: dict[str, tuple[HomeworkReading, list[_Seen]]] = {}
    for sha, reading in sorted(images.items()):
        if reading is None:
            key = _key({"reading": "undecodable", "image": sha})
            unread[key] = UnreadCard(key, sha, None, None)
        elif not reading.wrong_kind:
            placed[sha] = (reading, _placed(sha, reading))
    carried = _carried(placed)
    seen = [
        seen._replace(heading=carried.get(sha)) if _orphan(seen) else seen
        for sha, (_, cards) in sorted(placed.items())
        for seen in cards
    ]
    whole = [item for item in seen if item.card is not None and not item.card.clipped]
    units: list[tuple[tuple[object, ...], PortalCard, frozenset[str]]] = []
    for item in seen:
        card = item.card
        if card is not None and card.clipped and _covered(item, whole):
            continue
        portal = None if card is None or card.clipped else _portal(card, item.heading)
        if card is None or portal is None:
            found = _unread(item)
            unread.setdefault(found.key, found)
            continue
        marks = _marks(card, item.heading)
        units.append((_walk_order(portal, card, item.heading, marks), portal, marks))
    units.sort(key=lambda unit: unit[0])
    cards_read = read_cards([portal for _, portal, _ in units], now=now)
    marks_of: list[set[str]] = [set() for _ in cards_read.readings]
    for (_, _, marks), place in zip(units, cards_read.joined, strict=True):
        marks_of[place] |= marks
    rows: list[ScreenshotRow] = []
    taken: set[str] = set()
    for written, marked in zip(cards_read.readings, marks_of, strict=True):
        key = _key(_written(written, frozenset(marked)))
        if key in taken:
            # Two rows that would write the same: shown as not read, never merged.
            repeat = _key({"repeated": key, "count": len(taken)})
            unread.setdefault(repeat, UnreadCard(repeat, "", None, None))
            continue
        taken.add(key)
        rows.append(ScreenshotRow(key, written, frozenset(marked)))
    return Assembly(
        rows=tuple(sorted(rows, key=lambda row: row.key)),
        unread=tuple(sorted(unread.values(), key=lambda item: item.key)),
    )


def _decoded(stored: StoredReading) -> HomeworkReading | None:
    """A stored reading as its model, or ``None`` when it can't be decoded as this schema's."""
    if (stored.prompt_version, stored.schema_version) != (PROMPT_VERSION, SCHEMA_VERSION):
        return None
    try:
        return HomeworkReading.model_validate_json(stored.body)
    except (ValueError, RecursionError):
        return None


def _placed(sha: str, reading: HomeworkReading) -> list[_Seen]:
    """Each card of a reading at its place, under the last heading above its top edge."""
    headings = sorted(reading.headings, key=lambda heading: heading.y)
    seen: list[_Seen] = []
    for place, card in enumerate(reading.cards):
        above = None
        if card is not None:
            above = next((h for h in reversed(headings) if h.y <= card.y_top), None)
        seen.append(_Seen(sha, place, card, above))
    return seen


def _orphan(seen: _Seen) -> bool:
    """A card read above every heading its screenshot shows."""
    return seen.card is not None and seen.heading is None


def _carried(placed: dict[str, tuple[HomeworkReading, list[_Seen]]]) -> dict[str, Heading]:
    """For each screenshot whose top cards sit under no heading it shows, the heading they sit
    under in another screenshot, when the overlap is proven and no proof names another.

    Every screenshot's candidates are worked out together, adding only, until nothing more is
    added: a proof from a section under a heading gives that heading, and one from another
    screenshot's top cards gives every candidate that screenshot has. Only a screenshot left
    with one candidate carries it, so a conflict found late undoes what rested on it.
    """
    shaped = {sha: _slots(reading, cards) for sha, (reading, cards) in placed.items()}
    candidates: dict[str, dict[str, list[Heading]]] = {sha: {} for sha in placed}
    added = True
    while added:
        added = False
        for sha, (headings, slots) in shaped.items():
            top = slots.get(_TOP, [])
            if not any(card is not None for card in top):
                continue
            first = headings[0] if headings else None
            for other, (other_headings, other_slots) in shaped.items():
                if other == sha:
                    continue
                for heading, section, after in _sections(other_headings, other_slots):
                    if not _continues(top, first, section, after):
                        continue
                    named = (
                        [heading]
                        if heading is not None
                        else [h for found in candidates[other].values() for h in found]
                    )
                    for proven in named:
                        bucket = candidates[sha].setdefault(folded(proven.text), [])
                        if proven not in bucket:
                            bucket.append(proven)
                            added = True
    return {
        sha: min(found, key=lambda h: (h.uncertain is not None, h.y, h.text))
        for sha, named in candidates.items()
        if len(named) == 1
        for found in named.values()
    }


_TOP: Final = -1
"""Where a screenshot's top cards sit: above every heading it shows."""


def _slots(
    reading: HomeworkReading, cards: list[_Seen]
) -> tuple[list[Heading], dict[int, list[Card | None]]]:
    """A screenshot's headings, top to bottom, and the cards under each in the order read,
    by the heading's place, ``_TOP`` for the cards above them all. A card that couldn't be
    read has no position, so it is put under every heading it could sit under between the
    cards read before and after it."""
    headings = sorted(reading.headings, key=lambda heading: heading.y)
    under = [
        None
        if item.card is None
        else _TOP
        if item.heading is None
        else headings.index(item.heading)
        for item in cards
    ]
    last = len(headings) - 1
    found: dict[int, list[Card | None]] = {}
    for place, item in enumerate(cards):
        label = under[place]
        if label is None:
            before = next((at for at in reversed(under[:place]) if at is not None), _TOP)
            after = next((at for at in under[place + 1 :] if at is not None), last)
            spans = list(range(before, max(before, after) + 1))
        else:
            spans = [label]
        for span in spans:
            found.setdefault(span, []).append(item.card)
    return headings, found


def _sections(
    headings: list[Heading], slots: dict[int, list[Card | None]]
) -> list[tuple[Heading | None, list[Card | None], Heading | None]]:
    """Each heading of a screenshot with the cards under it and the heading that closes its
    section, ``None`` when it runs to the screenshot's bottom; then its top cards, under no
    heading of its own (``None``), closed by its first heading."""
    sections: list[tuple[Heading | None, list[Card | None], Heading | None]] = [
        (heading, slots.get(place, []), headings[place + 1] if place + 1 < len(headings) else None)
        for place, heading in enumerate(headings)
    ]
    sections.append((None, slots.get(_TOP, []), headings[0] if headings else None))
    return sections


def _continues(
    top: list[Card | None], first: Heading | None, section: list[Card | None], after: Heading | None
) -> bool:
    """Whether a screenshot's top cards, above its first heading ``first``, continue a section
    closed by ``after`` (``None``: running to its screenshot's bottom). They repeat the
    section's cards card for card, each read whole, from one of them to the section's end,
    then stop there, before no heading or before the one closing the section, or run on past
    it only when the section runs to its screenshot's bottom. A clip at the top screenshot's
    top edge, or at the section's bottom edge, is set aside; anything else not read whole,
    a card that couldn't be read included, matches nothing."""
    if top and top[0] is not None and top[0].clipped:
        top = top[1:]
    if after is None and section and section[-1] is not None and section[-1].clipped:
        section = section[:-1]
    for start in range(len(section)):
        run = section[start:]
        if len(top) < len(run) or not all(map(_same, run, top)):
            continue
        if after is None:
            return True
        if len(top) == len(run) and (first is None or _text(first) == _text(after)):
            return True
    return False


def _same(one: Card | None, other: Card | None) -> bool:
    """Two cards each read whole that read the same, field for field."""
    return (
        one is not None
        and other is not None
        and one.certain
        and other.certain
        and _as_read(one) == _as_read(other)
    )


def _as_read(card: Card) -> tuple[object, ...]:
    """A card's fields as read, text folded."""
    return (
        card.badge,
        _fold(card.course),
        _fold(card.title),
        _fold(card.instruction),
        _fold(card.footer),
    )


def _covered(clipped: _Seen, whole: list[_Seen]) -> bool:
    """Whether a card read whole under the same heading reads as everything a clipped card
    read with certainty, its course and title among it. A field the clip marks uncertain is
    no evidence either way; one it shows with certainty as absent must be absent there too."""
    card = clipped.card
    if card is None or not (_words(card.course) and _words(card.title)):
        return False
    marked = card.uncertain_fields
    if {"course", "title"} & marked:
        return False
    for other in whole:
        full = other.card
        if full is None or not full.certain or _text(other.heading) != _text(clipped.heading):
            continue
        if all(
            field in marked or _fold(getattr(card, field)) == _fold(getattr(full, field))
            for field in ("badge", "course", "title", "instruction", "footer")
        ):
            return True
    return False


def _portal(card: Card, heading: Heading | None) -> PortalCard | None:
    """A whole card as a portal card, or ``None`` when it has no date or an unread name."""
    if not card.complete or card.course is None or card.title is None:
        return None
    day = None if heading is None else heading.day
    when = card.due if card.badge == "ASSIGNED" else day
    if when is None:
        return None
    return PortalCard(
        course=card.course,
        title=card.title,
        card="assigned" if card.badge == "ASSIGNED" else "due",
        when=when,
        day=day,
        instruction=card.instruction if instruction_words(card.instruction) else None,
    )


def _marks(card: Card, heading: Heading | None) -> frozenset[str]:
    """The fields marked uncertain on a card, and its day heading when that was."""
    heading_mark = {"heading"} if heading is not None and heading.uncertain is not None else set()
    return card.uncertain_fields | heading_mark


def _walk_order(
    portal: PortalCard, card: Card, heading: Heading | None, marks: frozenset[str]
) -> tuple[object, ...]:
    """The content-fixed order: the card's date, class, title, assigned before due, then its
    own fields as read (instruction, footer, heading text, each field's presence and mark)."""
    return (
        portal.when,
        _fold(portal.course),
        _fold(portal.title),
        0 if portal.card == "assigned" else 1,
        _fold(card.instruction) or "",
        _fold(card.footer) or "",
        _text(heading) or "",
        (card.instruction is None, card.footer is None, heading is None),
        tuple(sorted(marks)),
        # Spacing alone, which folding reads as one, still decides between two copies.
        (portal.course, portal.title, card.instruction or "", card.footer or ""),
        "" if heading is None else heading.text,
    )


def _unread(item: _Seen) -> UnreadCard:
    """A card that couldn't be read as a row, keyed by what was read of it, and by its
    screenshot and place too when its course or title wasn't read."""
    card = item.card
    payload: dict[str, object] = {
        "unread": None if card is None else [*_as_read(card), card.clipped, _text(item.heading)],
        "marks": [] if card is None else sorted(_marks(card, item.heading)),
    }
    if card is None or not (_words(card.course) and _words(card.title)):
        payload |= {"image": item.sha256, "place": item.place}
    return UnreadCard(_key(payload), item.sha256, item.place, card)


def _written(reading: Reading, marks: frozenset[str]) -> dict[str, object]:
    """Everything a row would write, as read, with its marks: never a name, a position, a
    correction or an instant. A round of a recurring name is told by its own dates; its
    occurrence, which changes when an earlier round's screenshot arrives, is left out."""
    return {
        "course": _fold(reading.course),
        "title": _fold(reading.title),
        "due_date": _day(reading.due_date),
        "assigned_on": _day(reading.assigned_on),
        "claims": sorted(
            [str(claim.channel), claim.asserted_value, claim.seen_in or ""]
            for claim in reading.claims
        ),
        "instructions": sorted(
            [_fold(seen.text) or "", seen.card or "", _day(seen.card_day)]
            for seen in reading.instructions
        ),
        "reports": sorted([report.status, _day(report.reported_on)] for report in reading.reports),
        "marks": sorted(marks),
    }


def _key(payload: object) -> str:
    """The SHA-256 of one fixed serialization."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _fold(text: str | None) -> str | None:
    return None if text is None else folded(text)


def _words(text: str | None) -> bool:
    return text is not None and bool(text.strip())


def _text(heading: Heading | None) -> str | None:
    return None if heading is None else folded(heading.text)


def _day(day: date | None) -> str:
    return "" if day is None else day.isoformat()
