# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The reader for a gradebook report pasted as text: a closed set of line classes, never an
interpreter.

A line is the report's title, its header, the PERCENT label once between the header and the
class name, its class name, a category, a column header row, a result row, a category average,
the term grade, Print, a selector repeating the header's class or term, or a table with nothing
in it. Any other line is kept, verbatim and in order, as a line the reader didn't recognize, and
it never changes the draft or the capture key.

Print, the selectors and a wrapped title are read narrowly, as the one known report shape has
them, and a result row a copy wraps isn't joined: a line the reader can't be sure of stays
visible. Nothing here logs, and nothing calls a model.
"""

import re
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from blossom.grades.draft import (
    NUMBER,
    SCHOOL_YEAR,
    DueText,
    Evidence,
    GradeCategory,
    GradeNumber,
    GradeReportDraft,
    GradeRow,
    GradeValue,
    ReportHeader,
    TermResult,
    capture_key,
    folded,
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


class NotRead(StrEnum):
    """Why a paste gave no draft, for the review to say in its own words."""

    NO_HEADER = "no_header"
    SEVERAL_REPORTS = "several_reports"


class GradeReportReading(BaseModel):
    """What one paste gave: its draft, or the reason it has none, and every line the reader
    didn't recognize, verbatim and in order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    draft: GradeReportDraft | None
    not_read: NotRead | None
    unrecognized: tuple[str, ...]

    @model_validator(mode="after")
    def _a_draft_or_the_reason_for_none(self) -> Self:
        if (self.draft is None) == (self.not_read is None):
            msg = "a reading has a draft or the reason it has none"
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
        if top is None or len(top) != 3 or SCHOOL_YEAR.fullmatch(top[1]) is None:
            continue
        if rule is None or not is_separator(rule) or bottom is None or len(bottom) != 3:
            continue
        if bottom[0] and bottom[1] and not bottom[2]:
            found.append((start, top, bottom))
    return found


class _Category:
    """A category while its lines are read."""

    def __init__(self, name: GradeValue, weight: GradeNumber) -> None:
        self.name = name
        self.weight = weight
        self.columns: list[str] | None = None
        self.rows: list[list[str]] = []
        self.labeled = False
        self.average: GradeNumber | None = None

    def awaits_average(self) -> bool:
        return self.labeled and self.average is None

    def close(self) -> None:
        """At the next category or the term grade, an average label with no value is blank."""
        if self.awaits_average():
            self.average = GradeNumber.read("")

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


def read_grade_report(text: str) -> GradeReportReading:
    """Read pasted report text into its draft, or into the reason it can't be read, keeping
    every line outside the line classes."""
    lines = text.splitlines()
    headers = header_rows(lines)
    if len(headers) != 1:
        return GradeReportReading(
            draft=None,
            not_read=NotRead.SEVERAL_REPORTS if headers else NotRead.NO_HEADER,
            unrecognized=tuple(line for line in lines if line.strip()),
        )
    ((start, top, bottom),) = headers
    unrecognized: list[str] = []
    categories: list[_Category] = []
    term: TermResult | None = None
    class_name: str | None = None
    percent = False
    index = 0
    while index < len(lines):
        line = lines[index]
        step = 1
        current = categories[-1] if categories else None
        cells = cells_of(line)
        if start <= index < start + 3 or not line.strip():
            pass
        elif cells is None:
            if taken := title_lines(lines, index):
                step = taken
            elif folded(bare(line)) == PRINT:
                pass
            elif (
                bare(line) == PERCENT
                and index > start
                and not percent
                and class_name is None
                and not categories
            ):
                percent = True
            elif folded(bare(line)) == CATEGORY_AVERAGE:
                if current and not current.labeled:
                    current.labeled = True
                else:
                    unrecognized.append(line)
            elif current and current.awaits_average() and is_bold_or_number(line):
                current.average = GradeNumber.read(bare(line))
            else:
                unrecognized.append(line)
        elif is_layout(cells):
            pass
        elif (
            len(cells) == 1
            and class_name is None
            and not categories
            and index + 1 < len(lines)
            and (rule := cells_of(lines[index + 1])) is not None
            and is_separator(rule)
        ):
            class_name = cells[0]
        elif len(cells) == 3 and not cells[1] and (weight := WEIGHT.fullmatch(cells[2])):
            if current:
                current.close()
            categories.append(_Category(GradeValue.read(cells[0]), GradeNumber.read(weight[1])))
        elif is_term_grade(cells) and term is None:
            if current:
                current.close()
            term = TermResult(percent=GradeNumber.read(cells[1]), letter=GradeValue.read(cells[2]))
        elif (
            is_column_header(cells)
            and current
            and not current.labeled
            and current.columns in (None, cells)
        ):
            current.columns = cells
        elif (
            not is_column_header(cells)
            and not is_term_grade(cells)
            and current
            and not current.labeled
            and current.columns is not None
            and len(cells) == len(current.columns)
        ):
            current.rows.append(cells)
        else:
            unrecognized.append(line)
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
    return GradeReportReading(
        draft=draft,
        not_read=None,
        unrecognized=tuple(
            line
            for line in unrecognized
            if cells_of(line) is not None or folded(bare(line)) not in selectors
        ),
    )


def is_bold_or_number(line: str) -> bool:
    """A line that can hold a category's average: bold all through, or a number alone."""
    text = line.strip()
    bold = len(text) >= 4 and text.startswith("**") and text.endswith("**")
    return bold or NUMBER.fullmatch(text) is not None
