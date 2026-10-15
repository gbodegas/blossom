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
"""

import re
from datetime import date
from typing import Annotated, Final, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from blossom.intake import DAY_LINE, a_date

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
    y_top: StrictInt
    y_bottom: StrictInt
    clipped: StrictBool
    uncertain: Annotated[tuple[CardMark, ...], Field(max_length=MOST_MARKS)]

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
        """Whether its label, course and title were all read."""
        return None not in (self.badge, self.course, self.title)


class Heading(_Closed):
    """One day heading as read, with the reader's note when it is uncertain."""

    text: Line
    y: StrictInt
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
