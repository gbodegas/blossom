# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The reader for a gradebook report pasted as text: a closed set of line classes, read by a
closed set of states, never an interpreter.

Each line is classed by a fixed precedence, then read by the state it arrives in. Before the
header: the report's title, Print, and a selector repeating the header's class, its term or the
class name. After the header: the PERCENT label once, the class name, then each category with its
column header, its result rows and its average, and last the term grade. A value is read only
under the structure directly above it, in its own category, and a structure that interrupts a
category closes it. Blank lines and tables with nothing in them are layout anywhere. Any other
line, and any line out of its place, is kept, verbatim and in order, as a line the reader didn't
recognize, and it never changes the draft or the capture key. The reading says where each such
line fell: before the header, before the Term Grade row, or after it. A second title, a title
after the header, or anything but one header means the paste isn't read.

Print, the selectors and a wrapped title are read narrowly, as the one known report shape has
them, and a result row a copy wraps isn't joined: a line the reader can't be sure of stays
visible. Nothing here logs, and nothing calls a model.
"""

import re
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from blossom.grades.draft import (
    NUMBER,
    DueText,
    Evidence,
    GradeCategory,
    GradeNumber,
    GradeReportDraft,
    GradeRow,
    GradeValue,
    Presence,
    ReportHeader,
    TermResult,
    capture_key,
    folded,
    is_school_year,
    row_evidence,
)

TITLE = "Gradebook Student Progress Report"
CATEGORY_AVERAGE = "Category Average"
TERM_GRADE = "Term Grade"
PRINT = "Print"
PERCENT = "PERCENT"
WEIGHT = re.compile(r"Weight\s*=\s*(.*)")
"""A category's weight cell, such as ``Weight = 15.0``."""
SEPARATOR_CELL = re.compile(r":?-+:?")
"""A cell of the line under a table's first row."""

COLUMNS: dict[str, tuple[str, type[GradeValue]]] = {
    "Assignment": ("assignment", GradeValue),
    "Pts": ("points", GradeNumber),
    "Max": ("max_points", GradeNumber),
    "Avg": ("average", GradeNumber),
    "Status": ("status", GradeValue),
    "Due": ("due", DueText),
    "Curve": ("curve", GradeNumber),
    "Bonus": ("bonus", GradeNumber),
    "Penalty": ("penalty", GradeNumber),
    "Weight": ("weight", GradeNumber),
    "Note": ("note", GradeValue),
}
"""Each column a result table may have, the row field it fills, and the kind of value it holds.
A column the table leaves out is not captured in each of its rows."""


class State(StrEnum):
    """Where the reader is in a report: each line is read by the state it arrives in."""

    BEFORE = "before"
    """Before the title and the header."""
    TITLED = "titled"
    """After the title, before the header."""
    HEADED = "headed"
    """After the header, before the PERCENT label, the class name or any category."""
    PERCENT_READ = "percent_read"
    """After the PERCENT label, before the class name or any category."""
    NAMED = "named"
    """After the class name, before any category."""
    NEEDS_COLUMNS = "needs_columns"
    """A category has begun, with no column header yet."""
    ROWS = "rows"
    """A category with a column header in force."""
    LABELED = "labeled"
    """The category's average label is read, and its value awaited: a line the reader doesn't
    know stays visible and leaves it so."""
    CLOSED = "closed"
    """The category takes nothing more: its average is read, or a structure interrupted it."""
    AFTER_TERM = "after_term"
    """The term grade is read."""


class LineClass(StrEnum):
    """What a line is, by a fixed precedence, before its place is weighed."""

    BLANK = "blank"
    TITLE = "title"
    PRINT = "print"
    PERCENT = "percent"
    LABEL = "label"
    VALUE = "value"
    OTHER = "other"
    LAYOUT = "layout"
    CLASS_TABLE = "class_table"
    CATEGORY = "category"
    TERM = "term"
    COLUMNS = "columns"
    ROW = "row"
    HEADER = "header"


IN_A_CATEGORY = frozenset({State.NEEDS_COLUMNS, State.ROWS, State.LABELED, State.CLOSED})
"""The states with a category open."""
TAKES_A_CATEGORY = frozenset({State.HEADED, State.PERCENT_READ, State.NAMED}) | IN_A_CATEGORY
"""The states a category line or the term grade is read in."""


class NotRead(StrEnum):
    """Why a paste gave no draft, for the review to say in its own words."""

    NO_HEADER = "no_header"
    SEVERAL_REPORTS = "several_reports"


class LinePlace(StrEnum):
    """Where an unrecognized line fell in a paste that gave a draft."""

    BEFORE_HEADER = "before_header"
    BEFORE_TERM = "before_term"
    """Between the header and the Term Grade row, or after the header when there is none."""
    AFTER_TERM = "after_term"


class GradeReportReading(BaseModel):
    """What one paste gave: its draft, or the reason it has none, and every line the reader
    didn't recognize, verbatim and in order, left out of every repr since a paste can carry
    names. With a draft, ``places`` says where each of those lines fell."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    draft: GradeReportDraft | None
    not_read: NotRead | None
    unrecognized: tuple[str, ...] = Field(repr=False)
    places: tuple[LinePlace, ...] = ()

    @model_validator(mode="after")
    def _a_draft_or_the_reason_for_none(self) -> Self:
        if (self.draft is None) == (self.not_read is None):
            msg = "a reading has a draft or the reason it has none"
            raise ValueError(msg)
        if len(self.places) != (0 if self.draft is None else len(self.unrecognized)):
            msg = "a reading with a draft places each line it didn't recognize, and only then"
            raise ValueError(msg)
        return self

    @property
    def capture_key(self) -> str | None:
        """The draft's capture key; None when the paste gave no draft."""
        return None if self.draft is None else capture_key(self.draft)


def bare(text: str) -> str:
    """A line or a cell's text without the space around it or the bold marks around that."""
    text = text.strip()
    if len(text) >= 4 and text.startswith("**") and text.endswith("**"):
        text = text[2:-2].strip()
    return text


def cells_of(line: str) -> list[str] | None:
    """A table line's cells, each bare; None for a line that isn't a table line."""
    text = line.strip()
    if len(text) < 2 or not (text.startswith("|") and text.endswith("|")):
        return None
    return [bare(cell) for cell in text[1:-1].split("|")]


def is_separator(cells: list[str]) -> bool:
    """The line under a table's first row."""
    return all(SEPARATOR_CELL.fullmatch(cell) for cell in cells)


def is_layout(cells: list[str]) -> bool:
    """A separator, or a row with nothing in it: a layout table's lines and a result table's
    empty last row."""
    return is_separator(cells) or not any(cells)


def is_column_header(cells: list[str]) -> bool:
    """A result table's first row: known column names, each once, Assignment among them."""
    return "Assignment" in cells and len(set(cells)) == len(cells) and set(cells) <= set(COLUMNS)


def is_term_grade(cells: list[str]) -> bool:
    """The term grade row: its label, the percent, the letter, then nothing."""
    return len(cells) >= 3 and folded(cells[0]) == TERM_GRADE and not any(cells[3:])


def title_lines(lines: list[str], start: int) -> int:
    """How many lines from ``start`` hold the report's title, which a copy may wrap; 0 when
    they don't."""
    joined = ""
    for count, line in enumerate(lines[start : start + 3], 1):
        if not line.strip():
            return 0
        joined = folded(f"{joined} {line}")
        if bare(joined) == TITLE:
            return count
        if not TITLE.startswith(joined.removeprefix("**").strip()):
            return 0
    return 0


def header_rows(lines: list[str]) -> list[tuple[int, list[str], list[str]]]:
    """Each header table: where it starts, the student, year and teacher cells, and the class
    and term cells under them."""
    found = []
    for start in range(len(lines) - 2):
        top, rule, bottom = (cells_of(line) for line in lines[start : start + 3])
        if top is None or len(top) != 3 or not is_school_year(top[1]):
            continue
        if rule is None or not is_separator(rule) or bottom is None or len(bottom) != 3:
            continue
        if bottom[0] and bottom[1] and not bottom[2]:
            found.append((start, top, bottom))
    return found


class _Category:
    """A category while its lines are read: data only, since the reader's state says what each
    next line may be."""

    def __init__(self, name: GradeValue, weight: GradeNumber) -> None:
        self.name = name
        self.weight = weight
        self.columns: list[str] | None = None
        self.rows: list[list[str]] = []
        self.average: GradeNumber | None = None
        """The average as read; ``None`` leaves it not captured."""

    def built(self, seen: dict[Evidence, int]) -> GradeCategory:
        """The category as read, each row's occurrence counted in ``seen`` across the report."""
        written = []
        for cells in self.rows:
            by_column = dict(zip(self.columns or (), cells, strict=True))
            values = {
                field: kind.read(by_column[column]) if column in by_column else kind.not_captured()
                for column, (field, kind) in COLUMNS.items()
            }
            row = GradeRow.model_validate({**values, "occurrence": 1})
            evidence = row_evidence(self.name, row)
            seen[evidence] = seen.get(evidence, 0) + 1
            written.append(row.model_copy(update={"occurrence": seen[evidence]}))
        return GradeCategory(
            name=self.name,
            weight=self.weight,
            average=self.average if self.average is not None else GradeNumber.not_captured(),
            rows=tuple(written),
        )


def title_starts(lines: list[str]) -> list[int]:
    """Where each copy of the report's title starts, a wrapped one counted once."""
    starts = []
    index = 0
    while index < len(lines):
        if taken := title_lines(lines, index):
            starts.append(index)
            index += taken
        else:
            index += 1
    return starts


def line_class(lines: list[str], index: int, header: int) -> tuple[LineClass, int]:
    """What the line at ``index`` is, and how many lines it takes: the header by its place at
    ``header``, then the first class that fits, in the order the enum lists them."""
    line = lines[index]
    if index == header:
        return LineClass.HEADER, 3
    cells = cells_of(line)
    if cells is None:
        if not line.strip():
            return LineClass.BLANK, 1
        if taken := title_lines(lines, index):
            return LineClass.TITLE, taken
        text = bare(line)
        for plain, written in (
            (LineClass.PRINT, folded(text) == PRINT),
            (LineClass.PERCENT, text == PERCENT),
            (LineClass.LABEL, folded(text) == CATEGORY_AVERAGE),
            (LineClass.VALUE, is_bold_or_number(line)),
        ):
            if written:
                return plain, 1
        return LineClass.OTHER, 1
    if is_layout(cells):
        return LineClass.LAYOUT, 1
    rule = cells_of(lines[index + 1]) if index + 1 < len(lines) else None
    for table, shaped in (
        (LineClass.CLASS_TABLE, len(cells) == 1 and rule is not None and is_separator(rule)),
        (LineClass.CATEGORY, len(cells) == 3 and not cells[1] and bool(WEIGHT.fullmatch(cells[2]))),
        (LineClass.TERM, is_term_grade(cells)),
        (LineClass.COLUMNS, is_column_header(cells)),
    ):
        if shaped:
            return table, 1
    return LineClass.ROW, 1


def weight_of(cell: str) -> str:
    """The text a category's ``Weight = N`` cell gives for its weight."""
    written = WEIGHT.fullmatch(cell)
    return "" if written is None else written[1]


def closed(category: _Category, state: State) -> None:
    """Close ``category`` as a category line or the term grade arrives in ``state``: a label still
    awaiting its value is a blank average; any other close keeps the average as it stands, read
    or not captured."""
    if state is State.LABELED:
        category.average = GradeNumber.read("")


def read_grade_report(text: str) -> GradeReportReading:
    """Read pasted report text into its draft, or into the reason it can't be read, keeping
    every line outside the line classes or out of its place."""
    lines = text.splitlines()
    headers = header_rows(lines)
    titles = title_starts(lines)
    if len(headers) != 1 or len(titles) > 1 or any(title > headers[0][0] for title in titles):
        return GradeReportReading(
            draft=None,
            not_read=NotRead.SEVERAL_REPORTS if headers else NotRead.NO_HEADER,
            unrecognized=tuple(line for line in lines if line.strip()),
        )
    ((start, top, bottom),) = headers
    unrecognized: list[tuple[int, str]] = []
    categories: list[_Category] = []
    term: TermResult | None = None
    term_at: int | None = None
    class_name: str | None = None
    state = State.BEFORE
    index = 0
    while index < len(lines):
        line = lines[index]
        kind, step = line_class(lines, index, start)
        cells = cells_of(line) or []
        match state, kind:
            case _, LineClass.BLANK | LineClass.LAYOUT:
                pass
            case State.BEFORE, LineClass.TITLE:
                state = State.TITLED
            case State.BEFORE | State.TITLED, LineClass.PRINT:
                pass
            case State.BEFORE | State.TITLED, LineClass.HEADER:
                state = State.HEADED
            case State.HEADED, LineClass.PERCENT:
                state = State.PERCENT_READ
            case State.HEADED | State.PERCENT_READ, LineClass.CLASS_TABLE:
                class_name = cells[0]
                state = State.NAMED
            case _, LineClass.CATEGORY if state in TAKES_A_CATEGORY:
                if state in IN_A_CATEGORY:
                    closed(categories[-1], state)
                name, weight = GradeValue.read(cells[0]), GradeNumber.read(weight_of(cells[2]))
                categories.append(_Category(name, weight))
                state = State.NEEDS_COLUMNS
            case _, LineClass.TERM if state in TAKES_A_CATEGORY:
                if state in IN_A_CATEGORY:
                    closed(categories[-1], state)
                term = TermResult(
                    percent=GradeNumber.read(cells[1]), letter=GradeValue.read(cells[2])
                )
                term_at = index
                state = State.AFTER_TERM
            case State.NEEDS_COLUMNS, LineClass.COLUMNS:
                categories[-1].columns = cells
                state = State.ROWS
            case State.NEEDS_COLUMNS | State.ROWS, LineClass.LABEL:
                state = State.LABELED
            case State.ROWS, LineClass.ROW if len(cells) == len(categories[-1].columns or ()):
                categories[-1].rows.append(cells)
            case State.ROWS, LineClass.COLUMNS:
                unrecognized.append((index, line))
                state = State.CLOSED
            case State.LABELED, LineClass.VALUE:
                categories[-1].average = GradeNumber.read(bare(line))
                state = State.CLOSED
            case (
                State.LABELED,
                LineClass.COLUMNS | LineClass.ROW | LineClass.CLASS_TABLE | LineClass.LABEL,
            ):
                unrecognized.append((index, line))
                state = State.CLOSED
            case _:
                unrecognized.append((index, line))
        index += step
    selectors = {folded(bottom[0]), folded(bottom[1])} | (
        {folded(class_name)} if class_name else set()
    )
    seen: dict[Evidence, int] = {}
    draft = GradeReportDraft(
        header=ReportHeader(
            student_line=top[0] or None,
            year_label=top[1],
            class_code=bottom[0],
            term_label=bottom[1],
            class_name=class_name,
        ),
        term=term
        if term is not None
        else TermResult(percent=GradeNumber.not_captured(), letter=GradeValue.not_captured()),
        categories=tuple(category.built(seen) for category in categories),
    )
    kept = [
        (index, line)
        for index, line in unrecognized
        if index >= start or cells_of(line) is not None or folded(bare(line)) not in selectors
    ]

    def place(index: int) -> LinePlace:
        if index < start:
            return LinePlace.BEFORE_HEADER
        if term_at is None or index < term_at:
            return LinePlace.BEFORE_TERM
        return LinePlace.AFTER_TERM

    return GradeReportReading(
        draft=draft,
        not_read=None,
        unrecognized=tuple(line for _, line in kept),
        places=tuple(place(index) for index, _ in kept),
    )


def reading_complete(reading: GradeReportReading) -> bool:
    """Whether a reading is complete enough to say what its report doesn't show: its term result
    came from a Term Grade row, every category's average was read by its own structure, every
    result row's due cell was captured, no line it didn't recognize fell between the header and
    the Term Grade row, and no table line it didn't place fell after it."""
    draft = reading.draft
    if draft is None:
        return False
    term = (draft.term.percent.presence, draft.term.letter.presence)
    if Presence.NOT_CAPTURED in term:
        return False
    if any(category.average.presence is Presence.NOT_CAPTURED for category in draft.categories):
        return False
    if any(
        row.due.presence is Presence.NOT_CAPTURED
        for category in draft.categories
        for row in category.rows
    ):
        return False
    return not any(
        place is LinePlace.BEFORE_TERM
        or (place is LinePlace.AFTER_TERM and cells_of(line) is not None)
        for line, place in zip(reading.unrecognized, reading.places, strict=True)
    )


def is_bold_or_number(line: str) -> bool:
    """A line that can hold a category's average: bold all through, or a number alone."""
    text = line.strip()
    bold = len(text) >= 4 and text.startswith("**") and text.endswith("**")
    return bold or NUMBER.fullmatch(text) is not None
