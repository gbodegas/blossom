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
the screenshots came in, and read as today's portal claims through ``intake.read_cards``. A
card is dated only by its own due line or by a day heading above it in its own screenshot; no
heading carries from one screenshot to another. Two matching cards of one screenshot are two
rows, and every card that is clipped or can't be dated or read stays a visible item with its
reason, keyed by its screenshot, its stored reading and its place.
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

    @model_validator(mode="before")
    @classmethod
    def _clipped_into_the_image(cls, data: object) -> object:
        """A clipped card partly in the image, its top above the image's bottom and its bottom
        below the image's top, has its edges past the image brought to the image's edges, where
        it runs off. An unclipped card is never moved. A card wholly outside the image, even one
        that only touches an edge, or with its edges reversed, is left as given, so it stays a
        card that couldn't be read."""
        if not isinstance(data, dict) or data.get("clipped") is not True:
            return data
        top, bottom = data.get("y_top"), data.get("y_bottom")
        if not (isinstance(top, int) and isinstance(bottom, int)):
            return data
        if isinstance(top, bool) or isinstance(bottom, bool):
            return data
        if not (top < POSITION_MOST and bottom > 0):
            return data
        return {**data, "y_top": max(0, top), "y_bottom": min(POSITION_MOST, bottom)}

    @model_validator(mode="after")
    def _edges_in_order(self) -> Self:
        if self.y_bottom <= self.y_top:
            message = "a card's bottom edge is not below its top"
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
    """One row the screenshots read as: its key (what it would write and its layer), the
    reading it would write, and the fields marked uncertain on any card it came from, the day
    heading among them."""

    key: str
    reading: Reading
    marks: frozenset[str]


ItemReason = Literal[
    "reading_unreadable", "card_unreadable", "incomplete", "clipped", "no_day", "repeated"
]
"""Why an item couldn't be read as a row. The first three say text couldn't be read: a whole
reading, a card, or a card's label, course or title. ``clipped`` is a card partly cut off,
whether or not a whole copy is read elsewhere. ``no_day`` is a card read whole whose date
can't be told: no due line of its own and no day heading above it in its screenshot.
``repeated`` is a second row that would write what another does."""


class UnreadCard(NamedTuple):
    """A card that couldn't be read as a row, keyed by its image's SHA-256, its stored
    reading's SHA-256, its place and what was read of it; a stored reading that couldn't be
    decoded (``place`` and ``card`` then ``None``), keyed by the two SHA-256s; or a later row
    of one layer that would write what another does (``sha256`` empty), keyed by that row's
    key, its layer and how many came before it. Each with its reason."""

    key: str
    sha256: str
    place: int | None
    card: Card | None
    reason: ItemReason


class Assembly(NamedTuple):
    """What a draft's stored readings read as: its rows, and what couldn't be read."""

    rows: tuple[ScreenshotRow, ...]
    unread: tuple[UnreadCard, ...]


class _Seen(NamedTuple):
    """A card at its place in its image's stored reading, with the day heading it sits under,
    if one is known."""

    sha256: str
    stored: str
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

    The cards of every reading are one set. A card takes a day only from its own due line or
    from a day heading above it in its own screenshot; no heading carries to another
    screenshot, and no card's text is evidence of where another card is, so no card, a clipped
    one included, is set aside for a copy read elsewhere.

    The cards read whole and dated are read as today's portal claims in layers. A card's layer
    is how many such cards before it in its screenshot have its course and title, folded, its
    kind and its date, so two matching cards of one screenshot are in two layers and never
    join. Each layer is walked in an order fixed by content and read on its own, where cards
    of different screenshots join as a paste's do, and a row's key holds its layer. Every other
    card stays a visible item with its reason, never merged into another. ``now`` stamps the
    claims and is in no key.
    """
    images: dict[str, tuple[str, HomeworkReading | None]] = {}
    for stored in readings:
        images[stored.sha256] = (_stored(stored), _decoded(stored))
    unread: dict[str, UnreadCard] = {}
    seen: list[_Seen] = []
    for sha, (stored_key, reading) in sorted(images.items(), key=lambda image: image[0]):
        if reading is None:
            key = _key({"reading": "undecodable", "image": sha, "stored": stored_key})
            unread[key] = UnreadCard(key, sha, None, None, "reading_unreadable")
        elif not reading.wrong_kind:
            seen += _placed(sha, stored_key, reading)
    layers: dict[int, list[tuple[tuple[object, ...], PortalCard, frozenset[str]]]] = {}
    matching: dict[tuple[object, ...], int] = {}
    for item in seen:
        card = item.card
        portal = None if card is None or card.clipped else _portal(card, item.heading)
        if card is None or portal is None:
            found = _unread(item, _reason(card))
            unread[found.key] = found
            continue
        marks = _marks(card, item.heading)
        same = (item.sha256, _fold(portal.course), _fold(portal.title), portal.card, portal.when)
        layer = matching.get(same, 0)
        matching[same] = layer + 1
        unit = (_walk_order(portal, card, item.heading, marks), portal, marks)
        layers.setdefault(layer, []).append(unit)
    rows: list[ScreenshotRow] = []
    for layer, units in sorted(layers.items()):
        units.sort(key=lambda unit: unit[0])
        cards_read = read_cards([portal for _, portal, _ in units], now=now)
        marks_of: list[set[str]] = [set() for _ in cards_read.readings]
        for (_, _, marks), place in zip(units, cards_read.joined, strict=True):
            marks_of[place] |= marks
        taken: dict[str, int] = {}
        for written, marked in zip(cards_read.readings, marks_of, strict=True):
            key = _key(_written(written, frozenset(marked), layer))
            if key in taken:
                # Two rows of one layer that would write the same: each later one shown as not
                # read, never merged.
                taken[key] += 1
                repeat = _key({"repeated": key, "layer": layer, "count": taken[key]})
                unread[repeat] = UnreadCard(repeat, "", None, None, "repeated")
                continue
            taken[key] = 0
            rows.append(ScreenshotRow(key, written, frozenset(marked)))
    return Assembly(
        rows=tuple(sorted(rows, key=lambda row: row.key)),
        unread=tuple(sorted(unread.values(), key=lambda item: item.key)),
    )


def _stored(stored: StoredReading) -> str:
    """The SHA-256 of a stored reading itself: its prompt version, schema version and body."""
    return _key([stored.prompt_version, stored.schema_version, stored.body])


def _decoded(stored: StoredReading) -> HomeworkReading | None:
    """A stored reading as its model, or ``None`` when it can't be decoded as this schema's."""
    if (stored.prompt_version, stored.schema_version) != (PROMPT_VERSION, SCHEMA_VERSION):
        return None
    try:
        return HomeworkReading.model_validate_json(stored.body)
    except (ValueError, RecursionError):
        return None


def _placed(sha: str, stored: str, reading: HomeworkReading) -> list[_Seen]:
    """Each card of a reading at its place, under the last heading above its top edge."""
    headings = sorted(reading.headings, key=lambda heading: heading.y)
    seen: list[_Seen] = []
    for place, card in enumerate(reading.cards):
        above = None
        if card is not None:
            above = next((h for h in reversed(headings) if h.y <= card.y_top), None)
        seen.append(_Seen(sha, stored, place, card, above))
    return seen


def _as_read(card: Card) -> tuple[object, ...]:
    """A card's fields as read, text folded."""
    return (
        card.badge,
        _fold(card.course),
        _fold(card.title),
        _fold(card.instruction),
        _fold(card.footer),
    )


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
        # A card under a heading comes before an equal one with none, so the day it was read
        # under is the one its words keep.
        heading is None,
        _text(heading) or "",
        (card.instruction is None, card.footer is None, heading is None),
        tuple(sorted(marks)),
        # Spacing alone, which folding reads as one, still decides between two copies.
        (portal.course, portal.title, card.instruction or "", card.footer or ""),
        "" if heading is None else heading.text,
    )


def _reason(card: Card | None) -> ItemReason:
    """Why a card couldn't be read as a row."""
    if card is None:
        return "card_unreadable"
    if card.clipped:
        return "clipped"
    if not card.complete:
        return "incomplete"
    return "no_day"


def _as_item(item: _Seen) -> dict[str, object]:
    """What an item's key is made of: what was read of the card, its image's SHA-256, its
    stored reading's SHA-256 and its place there. No two cards share it, a recapture as new
    bytes included, and a changed reading of the same image gives a new one."""
    card = item.card
    return {
        "unread": None if card is None else [*_as_read(card), card.clipped, _text(item.heading)],
        "marks": [] if card is None else sorted(_marks(card, item.heading)),
        "image": item.sha256,
        "stored": item.stored,
        "place": item.place,
    }


def _unread(item: _Seen, reason: ItemReason) -> UnreadCard:
    """A card that couldn't be read as a row, keyed by what ``_as_item`` makes of it."""
    return UnreadCard(_key(_as_item(item)), item.sha256, item.place, item.card, reason)


def _written(reading: Reading, marks: frozenset[str], layer: int) -> dict[str, object]:
    """Everything a row would write, as read, with its marks and its layer: never a name, a
    position, a correction or an instant. A round of a recurring name is told by its own
    dates; its occurrence, which changes when an earlier round's screenshot arrives, is left
    out. Its layer, how many matching cards come before its own in their screenshot, is kept,
    and no other screenshot changes it."""
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
        "layer": layer,
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
