# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""One school gradebook report as read, before anyone accepts it: the draft a reader hands
the review, whatever the report was read from.

Each value keeps the text the report wrote and whether the report had it, so a blank cell, an
unreadable one, a value the copy left out and a zero never stand for one another. Numbers stay
text; ``GradeNumber.decimal`` is for comparing values, never for showing them. The capture key
names a report by what it says, so a second paste of the same report is the same capture
however it was spaced, wrapped or ended.
"""

import hashlib
import json
import re
from decimal import Decimal
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
"""A number as the gradebook writes one: digits, then a point and digits, a minus at most."""
MONTH_AND_DAY = re.compile(r"(0[1-9]|1[0-2])/(0[1-9]|[12][0-9]|3[01])")
"""A due date as the gradebook writes it, MM/DD, with no year."""
LONGEST_MONTHS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
"""The most days each month has in any year, February's in a leap year."""
SCHOOL_YEAR = re.compile(r"([0-9]{4})-([0-9]{4})")
"""A school year's label, such as 2026-2027."""


def is_school_year(text: str) -> bool:
    """Whether ``text`` is a school year's label: two years in a row, such as 2026-2027."""
    written = SCHOOL_YEAR.fullmatch(text)
    return written is not None and int(written[2]) == int(written[1]) + 1


class Presence(StrEnum):
    """Whether the report had a value: four states that never collapse into one another, and
    none of them zero."""

    REPORTED = "reported"
    BLANK = "blank"
    UNREADABLE = "unreadable"
    NOT_CAPTURED = "not_captured"


class GradeValue(BaseModel):
    """One value as the report wrote it, with its presence: a reported value has its kind's form,
    an unreadable one keeps text without it, and a blank or uncaptured one has no text. Text
    keeps any space its cell held around it."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    text: str
    presence: Presence

    @classmethod
    def fits(cls, text: str) -> bool:
        """Whether ``text`` has the form a reported value of this kind takes; any written text
        does."""
        return bool(text)

    @classmethod
    def read(cls, written: str) -> Self:
        """A cell's text as a value, exactly as written: blank when it holds nothing but space,
        reported when it has this kind's form, and unreadable, its text kept, when it doesn't."""
        if not written.strip():
            return cls(text="", presence=Presence.BLANK)
        kind = Presence.REPORTED if cls.fits(written) else Presence.UNREADABLE
        return cls(text=written, presence=kind)

    @classmethod
    def not_captured(cls) -> Self:
        """A value the report, as it was read, didn't include."""
        return cls(text="", presence=Presence.NOT_CAPTURED)

    @model_validator(mode="after")
    def _text_agrees_with_presence(self) -> Self:
        if (self.text.strip() == "") != (self.presence in (Presence.BLANK, Presence.NOT_CAPTURED)):
            msg = "a blank or uncaptured value has no text, and any other value has some"
            raise ValueError(msg)
        if not self.text.strip() and self.text:
            msg = "a blank or uncaptured value's text is empty, never space"
            raise ValueError(msg)
        if self.presence is Presence.REPORTED and not self.fits(self.text):
            msg = "a reported value has the form of its kind"
            raise ValueError(msg)
        if self.presence is Presence.UNREADABLE and self.fits(self.text):
            msg = "an unreadable value lacks the form of its kind"
            raise ValueError(msg)
        return self


class GradeNumber(GradeValue):
    """A number the report wrote, such as a score, a weight or an average, kept as its text."""

    @classmethod
    def fits(cls, text: str) -> bool:
        """Whether ``text`` is a number as the gradebook writes one."""
        return NUMBER.fullmatch(text) is not None

    def decimal(self) -> Decimal | None:
        """The reported number as a ``Decimal``, for comparing values; None for any other
        presence."""
        return Decimal(self.text) if self.presence is Presence.REPORTED else None


class DueText(GradeValue):
    """A due date as the report wrote it, MM/DD. The year it falls in is worked out elsewhere,
    from the school year's first month."""

    @classmethod
    def fits(cls, text: str) -> bool:
        """Whether ``text`` is a month and a day, MM/DD, that some year has."""
        written = MONTH_AND_DAY.fullmatch(text)
        return written is not None and int(written[2]) <= LONGEST_MONTHS[int(written[1]) - 1]


class ReportHeader(BaseModel):
    """The report's header as written. The student line is kept for the identity check alone and
    is left out of every repr and of the capture key; the teacher's cell is never kept."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    student_line: str | None = Field(repr=False)
    year_label: str
    class_code: str
    term_label: str
    class_name: str | None

    @field_validator("student_line", "year_label", "class_code", "term_label", "class_name")
    @classmethod
    def _is_written_text(cls, value: str | None) -> str | None:
        if value is not None and (not value or value != value.strip()):
            msg = "a header field is text with no space around it"
            raise ValueError(msg)
        return value

    @field_validator("year_label")
    @classmethod
    def _is_a_school_year(cls, value: str) -> str:
        if not is_school_year(value):
            msg = "a school year is written as two years in a row, such as 2026-2027"
            raise ValueError(msg)
        return value


class TermResult(BaseModel):
    """The term grade the report gives: its percent and its letter, as written."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    percent: GradeNumber
    letter: GradeValue


class GradeRow(BaseModel):
    """One result row, every cell as written, and its occurrence: its place among the rows of
    this report with the same evidence, counting from 1."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    assignment: GradeValue
    points: GradeNumber
    max_points: GradeNumber
    average: GradeNumber
    status: GradeValue
    due: DueText
    curve: GradeNumber
    bonus: GradeNumber
    penalty: GradeNumber
    weight: GradeNumber
    note: GradeValue
    occurrence: int = Field(ge=1)

    def cells(self) -> tuple[GradeValue, ...]:
        """The row's cells in the report's column order, Assignment to Note."""
        return (
            self.assignment,
            self.points,
            self.max_points,
            self.average,
            self.status,
            self.due,
            self.curve,
            self.bonus,
            self.penalty,
            self.weight,
            self.note,
        )


class GradeCategory(BaseModel):
    """One category: its name, weight and average as written, then its rows in order."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    name: GradeValue
    weight: GradeNumber
    average: GradeNumber
    rows: tuple[GradeRow, ...]


Evidence = tuple[tuple[Presence, str], tuple[Presence, str], tuple[Presence, str]]
"""A row's category, title and due date, each with its presence and its spaces folded."""


def folded(text: str) -> str:
    """``text`` with each run of spaces, tabs or line breaks read as one space, none at the
    ends: how the capture key and the evidence compare text."""
    return " ".join(text.split())


def row_evidence(category_name: GradeValue, row: GradeRow) -> Evidence:
    """What tells a row apart from the others: its category's name, its title and its due
    date."""
    return (
        (category_name.presence, folded(category_name.text)),
        (row.assignment.presence, folded(row.assignment.text)),
        (row.due.presence, folded(row.due.text)),
    )


class GradeReportDraft(BaseModel):
    """The validated reading of one report: its header, the term result and the categories in
    order. It holds what the report says and nothing the review could take as an answer."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    header: ReportHeader
    term: TermResult
    categories: tuple[GradeCategory, ...]

    @model_validator(mode="after")
    def _occurrences_follow_the_evidence(self) -> Self:
        seen: dict[Evidence, int] = {}
        for category in self.categories:
            for row in category.rows:
                evidence = row_evidence(category.name, row)
                seen[evidence] = seen.get(evidence, 0) + 1
                if row.occurrence != seen[evidence]:
                    msg = "each row's occurrence counts the rows before it with the same evidence"
                    raise ValueError(msg)
        return self


def canonical(draft: GradeReportDraft) -> str:
    """The draft written out in one fixed form: every field and value in order but the student
    line, which a stored hash would expose to name guesses, each with its presence and its text
    folded. Equal for the same report however it was laid out."""

    def value(one: GradeValue) -> list[str]:
        return [one.presence.value, folded(one.text)]

    def optional(text: str | None) -> str | None:
        return None if text is None else folded(text)

    header = draft.header
    shape = [
        [
            folded(header.year_label),
            folded(header.class_code),
            folded(header.term_label),
            optional(header.class_name),
        ],
        [value(draft.term.percent), value(draft.term.letter)],
        [
            [
                value(category.name),
                value(category.weight),
                value(category.average),
                [[value(cell) for cell in row.cells()] for row in category.rows],
            ]
            for category in draft.categories
        ],
    ]
    return json.dumps(shape, ensure_ascii=False, separators=(",", ":"))


HEX_KEY: Final = re.compile(r"[0-9a-f]{64}")
"""The shape of a capture key, and of a name form: a SHA-256 digest in lowercase hex."""


def capture_key(draft: GradeReportDraft) -> str:
    """The report's capture key: the SHA-256 of its canonical form, in hex."""
    return hashlib.sha256(canonical(draft).encode("utf-8")).hexdigest()
