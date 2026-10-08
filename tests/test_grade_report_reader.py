# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Reading a pasted gradebook report: every value as the report wrote it, with its presence;
the lines the reader didn't recognize, kept in order; and a capture key that names the same
report the same however the paste was spaced, wrapped or ended."""

import ast
import json
import logging
import pathlib
import re
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from blossom.grades.draft import (
    DueText,
    GradeNumber,
    GradeReportDraft,
    GradeRow,
    GradeValue,
    Presence,
    ReportHeader,
    canonical,
    capture_key,
)
from blossom.grades.identity import name_form, name_form_key
from blossom.grades.text_reader import (
    COLUMNS,
    GradeReportReading,
    HeldBack,
    LinePlace,
    NotRead,
    read_grade_report,
    reading_complete,
)
from blossom.settings import PACKAGE_ROOT
from tests.support import FIXTURES, without_the_due_column

REPORT = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")
"""A synthetic report in the shape the school's gradebook pastes as: Wren's, for an invented
class, with a placeholder teacher."""

ROWS = {
    "Homework / Practice": [
        "Seed Germination Log|18.0|20.0|90.0|Valid|09/22|0.0|0.0||1.0|",
        "Cell Diagram|7.0|10.0|70.0|Missing|09/26|0.0|0.0||1.0|",
    ],
    "Labs": [
        "Microscope Practice|27.0|30.0|90.0|Valid|09/24|0.0|0.0||1.0|",
        "Osmosis with Potato Slices|31.0|40.0|77.5|Valid|10/02|0.0|0.0||1.0|",
    ],
    "Quizzes": [],
    "Tests /Projects": [],
}
"""Each category's results, the cells in the report's column order, Assignment to Note, an
empty cell blank."""

TWINS = (
    "| IXL Practice | 18.0 | 20.0 | 90.0 | Valid | 09/22 | 0.0 | 0.0 |  | 1.0 |  |\n"
    "| IXL Practice | 7.0 | 10.0 | 70.0 | Missing | 09/22 | 0.0 | 0.0 |  | 1.0 |  |\n"
)
"""Two results with the same title, category and due date, as practice sets often come."""

PRINT_TIME = "Printed 10/06/2026 9:21 PM"


def read(text: str) -> tuple[GradeReportReading, GradeReportDraft]:
    """The reading of ``text`` and its draft, which it has to have."""
    reading = read_grade_report(text)
    assert reading.draft is not None, reading.not_read
    return reading, reading.draft


def written(row: GradeRow) -> list[tuple[Presence, str]]:
    return [(value.presence, value.text) for value in row.cells()]


def as_cells(row: str) -> list[tuple[Presence, str]]:
    return [(Presence.REPORTED, text) if text else (Presence.BLANK, "") for text in row.split("|")]


def homework_rows_replaced(rows: str) -> str:
    start = REPORT.index("| Seed Germination Log")
    end = REPORT.index("|                 |         |")
    return REPORT[:start] + rows + REPORT[end:]


def without_the_note_column(text: str) -> str:
    """The report with the last column of each result table, Note, left out."""
    lines = []
    for line in text.split("\n"):
        if line.count("|") == 12:
            body = line.rstrip()[:-1]
            line = body[: body.rfind("|") + 1]
        lines.append(line)
    return "\n".join(lines)


def test_the_header_is_read_as_written() -> None:
    _, draft = read(REPORT)
    header = draft.header
    assert header.student_line == "Bramble, Wren"
    assert header.year_label == "2026-2027"
    assert header.class_code == "07 BIO - C"
    assert header.term_label == "T1"
    assert header.class_name == "Biology"


def test_the_term_result_is_the_report_s_own_percent_and_letter() -> None:
    _, draft = read(REPORT)
    assert (draft.term.percent.presence, draft.term.percent.text) == (Presence.REPORTED, "81.9")
    assert draft.term.percent.decimal() == Decimal("81.9")
    assert (draft.term.letter.presence, draft.term.letter.text) == (Presence.REPORTED, "B-")


def test_each_category_keeps_its_name_weight_and_average_in_order() -> None:
    _, draft = read(REPORT)
    assert [category.name.text for category in draft.categories] == list(ROWS)
    assert [category.weight.text for category in draft.categories] == [
        "15.0",
        "25.0",
        "20.0",
        "30.0",
    ]
    assert [(c.average.presence, c.average.text) for c in draft.categories] == [
        (Presence.REPORTED, "80.0"),
        (Presence.REPORTED, "83.8"),
        (Presence.BLANK, ""),
        (Presence.BLANK, ""),
    ]


def test_a_blank_category_average_is_blank_and_never_zero() -> None:
    _, draft = read(REPORT)
    quizzes, tests = draft.categories[2:]
    for category in (quizzes, tests):
        assert category.average.presence is Presence.BLANK
        assert category.average.decimal() is None
        assert category.average != GradeNumber.read("0")
        assert category.average != GradeNumber.not_captured()
        assert category.rows == ()


def test_the_four_results_keep_every_cell_as_written() -> None:
    _, draft = read(REPORT)
    for category in draft.categories:
        assert [written(row) for row in category.rows] == [
            as_cells(row) for row in ROWS[category.name.text]
        ]
        assert [row.occurrence for row in category.rows] == [1] * len(category.rows)
    cell_diagram = draft.categories[0].rows[1]
    assert (cell_diagram.status.presence, cell_diagram.status.text) == (
        Presence.REPORTED,
        "Missing",
    )
    assert cell_diagram.penalty.presence is Presence.BLANK


def test_the_weights_total_ninety() -> None:
    _, draft = read(REPORT)
    weights = [category.weight.decimal() for category in draft.categories]
    assert weights == [Decimal("15.0"), Decimal("25.0"), Decimal("20.0"), Decimal("30.0")]
    assert sum(weight for weight in weights if weight is not None) == Decimal("90")


def test_the_teacher_is_never_kept_and_the_student_line_stays_out_of_reprs_and_the_key() -> None:
    reading, draft = read(REPORT)
    assert "Teacher" not in draft.model_dump_json()
    assert "Example" not in draft.model_dump_json()
    assert "Teacher" not in "".join(reading.unrecognized)
    assert "Wren" not in repr(draft)
    assert "Wren" not in repr(reading)
    assert "Wren" not in canonical(draft)
    assert "Bramble" not in canonical(draft)


def test_every_line_of_the_report_is_one_the_reader_knows() -> None:
    reading, _ = read(REPORT)
    assert reading.unrecognized == ()
    assert reading.not_read is None


def tabs_for_padding(text: str) -> str:
    return "\n".join(re.sub(r" *\| *", "\t|\t", line).strip("\t") for line in text.split("\n"))


@pytest.mark.parametrize(
    "variant",
    [
        tabs_for_padding,
        lambda text: text.replace("\n", "\r\n"),
        lambda text: text.replace(
            "**Gradebook Student Progress Report**", "**Gradebook Student\nProgress Report**"
        ),
    ],
    ids=["tabs-for-padding", "crlf", "wrapped-title"],
)
def test_a_paste_laid_out_another_way_reads_the_same(variant: Callable[[str], str]) -> None:
    changed = variant(REPORT)
    assert changed != REPORT
    seed, draft = read(REPORT)
    reading, again = read(changed)
    assert again == draft
    assert reading.unrecognized == ()
    assert reading.capture_key == seed.capture_key


def test_extra_spaces_keep_the_capture_key() -> None:
    spaced = REPORT.replace(" ", "  ")
    seed, _ = read(REPORT)
    reading, draft = read(spaced)
    assert reading.capture_key == seed.capture_key
    assert reading.unrecognized == ()
    assert draft.categories[0].rows[0].assignment.text == "Seed  Germination  Log"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("**83.8**\n", f"**83.8**\n{PRINT_TIME}\n"),
        ("\n**83.8**", f"\n{PRINT_TIME}\n\n**83.8**"),
    ],
    ids=["between-categories", "between-an-average-and-its-label"],
)
def test_an_unknown_line_is_shown_in_order_and_changes_nothing_else(
    before: str, after: str
) -> None:
    assert REPORT.count(before) == 1
    changed = REPORT.replace(before, after)
    seed, draft = read(REPORT)
    reading, again = read(changed)
    assert reading.unrecognized == (PRINT_TIME,)
    assert again == draft
    assert reading.capture_key == seed.capture_key


def test_the_structural_lines_are_consumed_silently() -> None:
    """The class and term selectors, as lines repeating the header's class and term, and
    Print."""
    seed, draft = read(REPORT)
    reading, again = read(f"07 BIO - C\nT1\n**Biology**\nPrint\n{REPORT}")
    assert reading.unrecognized == ()
    assert again == draft
    assert reading.capture_key == seed.capture_key


def test_one_changed_value_makes_another_capture() -> None:
    seven = "| Cell Diagram             | 7.0 "
    assert REPORT.count(seven) == 1
    changed = REPORT.replace(seven, seven.replace("7.0", "8.0"))
    seed, _ = read(REPORT)
    reading, draft = read(changed)
    assert draft.categories[0].rows[1].points.text == "8.0"
    assert reading.capture_key != seed.capture_key
    assert reading.capture_key is not None
    assert re.fullmatch(r"[0-9a-f]{64}", reading.capture_key)


def test_two_rows_with_the_same_evidence_are_two_results_in_order() -> None:
    _, draft = read(homework_rows_replaced(TWINS))
    first, second = draft.categories[0].rows
    assert (first.assignment.text, first.due.text, first.points.text) == (
        "IXL Practice",
        "09/22",
        "18.0",
    )
    assert (second.assignment.text, second.due.text, second.points.text) == (
        "IXL Practice",
        "09/22",
        "7.0",
    )
    assert [first.occurrence, second.occurrence] == [1, 2]
    assert [row.occurrence for row in draft.categories[1].rows] == [1, 1]


def test_the_draft_refuses_occurrences_its_rows_do_not_have() -> None:
    _, draft = read(homework_rows_replaced(TWINS))
    dumped = draft.model_dump()
    dumped["categories"][0]["rows"][1]["occurrence"] = 1
    with pytest.raises(ValidationError):
        GradeReportDraft.model_validate(dumped)
    assert GradeReportDraft.model_validate(draft.model_dump()) == draft


def test_a_missing_student_line_is_read_as_none_and_keeps_the_capture_key() -> None:
    seed, seed_draft = read(REPORT)
    reading, draft = read(REPORT.replace("**Bramble, Wren**", ""))
    assert draft.header.student_line is None
    assert draft != seed_draft
    assert (draft.term, draft.categories) == (seed_draft.term, seed_draft.categories)
    assert reading.capture_key == seed.capture_key


def test_identical_reports_for_two_siblings_share_a_capture_key_and_keep_their_names() -> None:
    """The key names a report by its grades alone; whose report it is stays with the identity
    check, which reads each draft's own student line."""
    seed, wren = read(REPORT)
    reading, linnet = read(REPORT.replace("**Bramble, Wren**", "**Bramble, Linnet**"))
    assert (wren.header.student_line, linnet.header.student_line) == (
        "Bramble, Wren",
        "Bramble, Linnet",
    )
    assert linnet != wren
    assert (linnet.term, linnet.categories) == (wren.term, wren.categories)
    assert canonical(linnet) == canonical(wren)
    assert reading.capture_key == seed.capture_key


def replaced_once(text: str, before: str, after: str) -> str:
    assert text.count(before) == 1, before
    return text.replace(before, after)


def test_the_percent_label_is_known_only_once_between_the_header_and_the_class_name() -> None:
    seed, draft = read(REPORT)
    without = replaced_once(REPORT, "**PERCENT**\n\n", "")
    title = "**Gradebook Student Progress Report**\n"
    for changed, shown in [
        (replaced_once(REPORT, title, f"PERCENT\n{title}"), "PERCENT"),
        (replaced_once(REPORT, "**PERCENT**\n", "**PERCENT**\nPERCENT\n"), "PERCENT"),
        (
            replaced_once(without, "| ----------- |\n", "| ----------- |\n\n**PERCENT**\n"),
            "**PERCENT**",
        ),
        (replaced_once(without, "**83.8**\n", "**83.8**\n\n**PERCENT**\n"), "**PERCENT**"),
        (f"{without}\n**PERCENT**\n", "**PERCENT**"),
    ]:
        reading, again = read(changed)
        assert reading.unrecognized == (shown,)
        assert again == draft
        assert reading.capture_key == seed.capture_key


def test_a_line_or_a_value_with_the_word_percent_in_it_is_kept() -> None:
    for label in ("**Percent**", "PERCENT complete", "**PERCENT** 81.9"):
        reading, _ = read(replaced_once(REPORT, "**PERCENT**\n", f"{label}\n"))
        assert reading.unrecognized == (label,)
    row = next(line for line in REPORT.split("\n") if line.startswith("| Microscope Practice"))
    noted = row.removesuffix("          |") + " PERCENT  |"
    assert noted != row
    changed = replaced_once(REPORT, "| Cell Diagram ", "| PERCENT Diagram ")
    reading, draft = read(replaced_once(changed, row, noted))
    assert reading.unrecognized == ()
    assert draft.categories[0].rows[1].assignment.text == "PERCENT Diagram"
    assert draft.categories[1].rows[0].note.text == "PERCENT"


LABS = REPORT[REPORT.index("| **Labs** |") : REPORT.index("\n", REPORT.index("| Osmosis")) + 1]
"""The Labs category's row, its column headers and its two results, as a fragment of another
report would carry them."""

LABS_SHOWN = tuple(line for line in LABS.split("\n") if line.strip() and set(line) - set("|- "))
"""The fragment's lines a reader shows when they are out of place: all but its separator."""

TITLE = "**Gradebook Student Progress Report**\n"


@pytest.mark.parametrize("where", ["before the header", "after the term grade"])
def test_a_report_fragment_out_of_place_stays_visible_and_out_of_the_draft(where: str) -> None:
    seed, draft = read(REPORT)
    if where == "before the header":
        changed = replaced_once(REPORT, TITLE, f"{LABS}\n{TITLE}")
    else:
        changed = f"{REPORT}\n{LABS}"
    reading, again = read(changed)
    assert reading.unrecognized == LABS_SHOWN
    assert again == draft
    assert reading.capture_key == seed.capture_key


def test_a_class_name_table_before_the_header_stays_visible() -> None:
    seed, draft = read(REPORT)
    reading, again = read(
        replaced_once(REPORT, TITLE, f"| **Chemistry** |\n| ------------- |\n\n{TITLE}")
    )
    assert reading.unrecognized == ("| **Chemistry** |",)
    assert again == draft
    assert reading.capture_key == seed.capture_key


@pytest.mark.parametrize("where", ["before", "after"])
def test_a_second_report_s_title_makes_the_paste_unreadable(where: str) -> None:
    other = f"{TITLE}\n{LABS}"
    text = f"{other}\n{REPORT}" if where == "before" else f"{REPORT}\n{other}"
    reading = read_grade_report(text)
    assert reading.draft is None
    assert reading.not_read is NotRead.SEVERAL_REPORTS
    assert reading.unrecognized == tuple(line for line in text.split("\n") if line.strip())


def test_print_and_the_selectors_are_known_only_before_the_header() -> None:
    seed, draft = read(REPORT)
    for shown in ("Print", "T1", "07 BIO - C", "**Biology**"):
        for changed in (
            replaced_once(REPORT, "**83.8**\n", f"**83.8**\n\n{shown}\n"),
            f"{REPORT}\n{shown}\n",
        ):
            reading, again = read(changed)
            assert reading.unrecognized == (shown,)
            assert again == draft
            assert reading.capture_key == seed.capture_key


REORDERED_RESULT = (
    "| **Assignment** | **Max** | **Pts** | **Avg** | **Status** | **Due** | **Curve** "
    "| **Bonus** | **Penalty** | **Weight** | **Note** |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
    "| Leaf Rubbing | 10.0 | 9.0 | 90.0 | Valid | 09/29 | 0.0 | 0.0 |  | 1.0 |  |\n"
)
"""A column header that differs from its category's, as another report's table would bring
one, then a result under it."""

HOMEWORK_ROWS = ["Seed Germination Log", "Cell Diagram"]
LABS_LINE = "| **Labs** |   | **Weight = 25.0** |\n"


def homework_of(draft: GradeReportDraft) -> tuple[GradeNumber, list[str]]:
    homework = draft.categories[0]
    return homework.average, [row.assignment.text for row in homework.rows]


def test_a_conflicting_column_header_closes_its_category_and_its_rows_stay_visible() -> None:
    """Rows are decoded only under the header they sit under: after one that differs, the
    category takes nothing more, its average included."""
    _, seed = read(REPORT)
    rows = REPORT[REPORT.index("| Seed Germination Log") : REPORT.index("|                 |")]
    reading, draft = read(homework_rows_replaced(rows + REORDERED_RESULT))
    lines = REORDERED_RESULT.splitlines()
    assert reading.unrecognized == (lines[0], lines[2], "**Category Average**", "**80.0**")
    assert homework_of(draft) == (GradeNumber.not_captured(), HOMEWORK_ROWS)
    assert draft.categories[1:] == seed.categories[1:]


@pytest.mark.parametrize("shown", ["**Print**", "**PERCENT**"])
def test_a_bold_structure_line_after_a_label_stays_visible_and_the_average_is_read(
    shown: str,
) -> None:
    seed, draft = read(REPORT)
    reading, again = read(replaced_once(REPORT, "**80.0**\n", f"{shown}\n\n**80.0**\n"))
    assert reading.unrecognized == (shown,)
    assert again == draft
    assert reading.capture_key == seed.capture_key


@pytest.mark.parametrize("shown", ["**PERCENT**", PRINT_TIME])
def test_a_line_after_a_blank_category_s_label_stays_visible_and_changes_nothing(
    shown: str,
) -> None:
    """A print time or a stray label between a blank category's label and the next category is
    shown, and neither the blank average nor the capture key moves."""
    seed, draft = read(REPORT)
    label = REPORT.index("**Category Average**", REPORT.index("| **Quizzes** |"))
    end = label + len("**Category Average**")
    reading, again = read(f"{REPORT[:end]}\n\n{shown}{REPORT[end:]}")
    assert reading.unrecognized == (shown,)
    assert again == draft
    assert again.categories[2].average.presence is Presence.BLANK
    assert reading.capture_key == seed.capture_key


def test_a_value_never_binds_to_a_category_whose_label_a_structure_followed() -> None:
    """Homework's value and Labs' category line are lost: Labs' table, label and value stay
    visible, and Homework's average is not captured rather than Labs'."""
    changed = replaced_once(replaced_once(REPORT, "**80.0**\n", ""), LABS_LINE, "")
    reading, draft = read(changed)
    assert homework_of(draft) == (GradeNumber.not_captured(), HOMEWORK_ROWS)
    assert [category.name.text for category in draft.categories] == [
        "Homework / Practice",
        "Quizzes",
        "Tests /Projects",
    ]
    assert "**83.8**" in reading.unrecognized


def test_rows_after_a_lost_category_line_never_join_the_category_above() -> None:
    """Homework's label and Labs' category line are lost: Labs' header puts Homework in doubt,
    so Labs' rows and both values stay visible."""
    label = REPORT.index("**Category Average**\n")
    changed = REPORT[:label] + REPORT[label + len("**Category Average**\n") :]
    reading, draft = read(replaced_once(changed, LABS_LINE, ""))
    assert homework_of(draft) == (GradeNumber.not_captured(), HOMEWORK_ROWS)
    assert "**80.0**" in reading.unrecognized
    assert "**83.8**" in reading.unrecognized
    assert any(line.startswith("| Microscope Practice") for line in reading.unrecognized)


def test_a_paste_without_the_header_is_not_read_and_keeps_its_lines() -> None:
    text = REPORT.replace("| **Bramble, Wren** | **2026-2027** | **Teacher, Example** |\n", "")
    reading = read_grade_report(text)
    assert reading.draft is None
    assert reading.not_read is NotRead.NO_HEADER
    assert reading.capture_key is None
    assert reading.unrecognized == tuple(line for line in text.split("\n") if line.strip())


def test_two_reports_in_one_paste_are_not_read() -> None:
    reading = read_grade_report(f"{REPORT}\n{REPORT}")
    assert reading.draft is None
    assert reading.not_read is NotRead.SEVERAL_REPORTS
    assert reading.capture_key is None


def test_a_copy_cut_before_the_term_grade_leaves_what_it_lacks_not_captured() -> None:
    cut = REPORT[: REPORT.index("|                |          |        |")]
    reading, draft = read(cut)
    assert draft.term.percent == GradeNumber.not_captured()
    assert draft.term.letter == GradeValue.not_captured()
    assert draft.categories[2].average.presence is Presence.BLANK
    assert draft.categories[3].average.presence is Presence.NOT_CAPTURED
    assert reading.unrecognized == ()


def test_a_cell_without_its_column_s_form_is_unreadable_with_its_text_kept() -> None:
    changed = REPORT.replace("| 7.0     | 10.0    |", "| 7,0     | 10.0    |").replace(
        "| 09/26   |", "| 9/26    |"
    )
    _, draft = read(changed)
    cell_diagram = draft.categories[0].rows[1]
    assert (cell_diagram.points.presence, cell_diagram.points.text) == (
        Presence.UNREADABLE,
        "7,0",
    )
    assert cell_diagram.points.decimal() is None
    assert (cell_diagram.due.presence, cell_diagram.due.text) == (Presence.UNREADABLE, "9/26")


def test_a_column_the_report_leaves_out_is_not_captured() -> None:
    _, draft = read(without_the_note_column(REPORT))
    rows = [row for category in draft.categories for row in category.rows]
    assert len(rows) == 4
    assert {row.note for row in rows} == {GradeValue.not_captured()}
    assert [row.points.text for row in rows] == ["18.0", "7.0", "27.0", "31.0"]


def test_a_value_disagreeing_with_its_presence_is_refused() -> None:
    for kind, text, presence in [
        (GradeValue, "", Presence.REPORTED),
        (GradeValue, "", Presence.UNREADABLE),
        (GradeValue, "Valid", Presence.BLANK),
        (GradeValue, "Valid", Presence.NOT_CAPTURED),
        (GradeValue, "  ", Presence.REPORTED),
        (GradeValue, " ", Presence.BLANK),
        (GradeNumber, " 7", Presence.REPORTED),
        (GradeNumber, "seven", Presence.REPORTED),
        (DueText, "9/26", Presence.REPORTED),
    ]:
        with pytest.raises(ValidationError):
            kind(text=text, presence=presence)
    assert GradeNumber(text="seven", presence=Presence.UNREADABLE).decimal() is None
    with pytest.raises(ValidationError):
        GradeValue(text="Valid", presence=Presence.REPORTED, score="0")  # type: ignore[call-arg]
    value = GradeValue.read("Valid")
    with pytest.raises(ValidationError):
        value.text = "Missing"


def test_the_four_presences_never_stand_for_one_another() -> None:
    values = {
        GradeNumber.read("0"),
        GradeNumber.read(""),
        GradeNumber.read("0..0"),
        GradeNumber.not_captured(),
    }
    assert {value.presence for value in values} == set(Presence)
    assert [value.decimal() for value in values if value.decimal() is not None] == [Decimal("0")]


def test_an_unreadable_value_never_has_its_kind_s_form() -> None:
    for kind, text in [(GradeValue, "Valid"), (GradeNumber, "7"), (DueText, "09/26")]:
        with pytest.raises(ValidationError):
            kind(text=text, presence=Presence.UNREADABLE)
        assert kind.read(text).presence is Presence.REPORTED


def test_a_due_date_is_a_month_and_a_day_that_exist() -> None:
    for text in ("02/30", "02/31", "04/31", "06/31", "09/31", "11/31", "13/01", "00/10"):
        assert DueText.read(text) == DueText(text=text, presence=Presence.UNREADABLE), text
    for text in ("01/31", "02/29", "04/30", "12/31"):
        assert DueText.read(text).presence is Presence.REPORTED, text


def test_no_pasted_line_shows_in_a_repr() -> None:
    header = "| **Bramble, Wren** | **2026-2027** | **Teacher, Example** |\n"
    for text in (replaced_once(REPORT, header, "| **Bramble, Wren** |\n"), f"{REPORT}\n{REPORT}"):
        reading = read_grade_report(text)
        assert reading.draft is None
        assert any("Wren" in line for line in reading.unrecognized)
        assert "Wren" not in repr(reading)
        assert "Teacher" not in repr(reading)


def test_a_header_needs_a_school_year_of_two_years_in_a_row() -> None:
    for label in ("2026-2028", "2027-2026", "2026-2026"):
        reading = read_grade_report(replaced_once(REPORT, "**2026-2027**", f"**{label}**"))
        assert reading.not_read is NotRead.NO_HEADER, label
    with pytest.raises(ValidationError):
        ReportHeader(
            student_line=None,
            year_label="2026-2028",
            class_code="07 BIO - C",
            term_label="T1",
            class_name=None,
        )


def test_a_refused_value_never_echoes_what_was_pasted() -> None:
    with pytest.raises(ValidationError) as refused:
        ReportHeader(
            student_line=" Bramble, Wren ",
            year_label="2026-2027",
            class_code="07 BIO - C",
            term_label="T1",
            class_name=None,
        )
    assert "Wren" not in str(refused.value)
    with pytest.raises(ValidationError) as refused:
        GradeReportReading(draft=None, not_read=None, unrecognized=("| **Bramble, Wren** |",))
    assert "Wren" not in str(refused.value)


def test_reading_a_report_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        read_grade_report(REPORT)
        read_grade_report("Bramble, Wren")
    assert caplog.records == []


READER = ("draft.py", "text_reader.py")
READER_IMPORTS = {
    "blossom.grades.draft",
    "decimal",
    "enum",
    "hashlib",
    "json",
    "pydantic",
    "re",
    "typing",
}
"""Everything the reader imports: no logging, and nothing that reaches a model."""


def test_the_reader_imports_no_logging_and_nothing_that_reaches_a_model() -> None:
    imported: set[str] = set()
    for name in READER:
        tree = ast.parse((PACKAGE_ROOT / "grades" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
    assert imported == READER_IMPORTS


def test_the_capture_key_is_the_draft_s_own() -> None:
    reading, draft = read(REPORT)
    assert reading.capture_key == capture_key(draft)


# ------------------------------------------------------------- where unread lines fell


TERM_ROW = REPORT[
    REPORT.index("| **Term Grade**") : REPORT.index("\n", REPORT.index("| **Term Grade**")) + 1
]
"""The Term Grade row as the fixture writes it."""
BODY_LINE = "Updated 10/06/2026"
"""A line no class knows, of the kind a copy may carry."""


def test_the_fixture_reads_complete() -> None:
    reading, _ = read(REPORT)
    assert (reading.unrecognized, reading.places) == ((), ())
    assert reading_complete(reading) is True


def test_each_unrecognized_line_says_where_it_fell_and_changes_nothing_else() -> None:
    """Before the header, between it and the Term Grade row, or after that row; the draft and
    the capture key stay the fixture's."""
    seed, draft = read(REPORT)
    labs = "| **Labs** |"
    text = f"{PRINT_TIME}\n{REPORT.replace(labs, f'{BODY_LINE}\n\n{labs}', 1)}\n{PRINT_TIME}\n"
    reading, again = read(text)

    assert reading.unrecognized == (PRINT_TIME, BODY_LINE, PRINT_TIME)
    assert reading.places == (
        LinePlace.BEFORE_HEADER,
        LinePlace.BEFORE_TERM,
        LinePlace.AFTER_TERM,
    )
    assert again == draft
    assert reading.capture_key == seed.capture_key
    assert reading_complete(reading) is False


def test_lines_before_the_header_or_text_after_the_term_keep_a_reading_complete() -> None:
    reading, _ = read(f"{PRINT_TIME}\n{REPORT}\n{PRINT_TIME}\n")
    assert reading.places == (LinePlace.BEFORE_HEADER, LinePlace.AFTER_TERM)
    assert reading_complete(reading) is True


def test_a_term_grade_row_placed_before_a_category_is_incomplete() -> None:
    """The categories after it go unplaced as table lines after the Term Grade row, so the
    reading can't say what the report doesn't show."""
    labs = (
        "|          |   |                   |\n| -------- | - | ----------------- |\n| **Labs** |"
    )
    assert REPORT.count(labs) == 1
    moved = REPORT.replace(TERM_ROW, "").replace(labs, f"{TERM_ROW}\n{labs}", 1)
    reading, draft = read(moved)

    assert draft.term.percent.presence is Presence.REPORTED
    assert len(draft.categories) == 1
    assert LinePlace.AFTER_TERM in reading.places
    assert reading_complete(reading) is False


def test_a_reading_without_a_term_grade_row_is_incomplete() -> None:
    reading, _ = read(REPORT.replace(TERM_ROW, ""))
    assert reading_complete(reading) is False


def test_a_category_whose_average_was_not_read_is_incomplete() -> None:
    reading, draft = read(REPORT.replace("**Category Average**", "**Average**", 1))
    assert draft.categories[0].average.presence is Presence.NOT_CAPTURED
    assert reading_complete(reading) is False


def test_a_result_row_whose_due_cell_was_not_captured_makes_the_reading_incomplete() -> None:
    """His fourteenth round, 5: a reading with a result row missing its due date can't say what
    its report doesn't show, while the format waits for his samples."""
    reading, draft = read(without_the_due_column(REPORT, "Homework / Practice"))

    assert {row.due.presence for row in draft.categories[0].rows} == {Presence.NOT_CAPTURED}
    assert {row.due.presence for row in draft.categories[1].rows} == {Presence.REPORTED}
    assert (reading.unrecognized, reading.places) == ((), ())
    assert reading_complete(reading) is False


def test_a_reading_with_no_draft_places_nothing_and_is_incomplete() -> None:
    reading = read_grade_report("nothing here")
    assert reading.places == ()
    assert reading_complete(reading) is False


# ------------------------------------------------- the gradebook's own tab-separated copy


CLIPBOARD = FIXTURES / "grade_clipboard"
"""Five synthetic reports exactly as the gradebook's copy puts them on the clipboard: tab
separated, with CRLF line endings, empty cells and trailing tabs, kept byte for byte. Beside
them, ``markdown`` holds the same five reports as Markdown tables, as controls."""
INVENTORY: dict[str, Any] = json.loads(
    (CLIPBOARD / "expected-values.json").read_bytes().decode("utf-8")
)
"""Each clipboard report's values, cell by cell, as the report wrote them."""
PASTES = sorted(INVENTORY)
CLASSES = {
    "geometry-grade-report.txt": ("08 Fixture 1 - A", "Patterns"),
    "humanities-grade-report.txt": ("08 Fixture 2 - A", "World Studies"),
    "religion-grade-report.txt": ("08 Fixture 3 - A", "Community Studies"),
    "science-grade-report.txt": ("08 Fixture 4 - A", "Nature Studies"),
    "spanish-grade-report.txt": ("08 Fixture 5 - A", "Language Studies"),
}
"""Each clipboard report's class code and class name, which the inventory doesn't list."""
LONG_TITLE = (
    "Practice 1.7: classroom exercise. Read the instructions and complete the practice before"
    " the next lesson. Use the examples to explain each answer. Use the examples to explain each"
    " answer. Use the examples to explain each answer. Use the examples "
)
"""Geometry's longest title, its last space part of the cell as the copy wrote it."""
DATE_CHANGE_TITLE = (
    "Quiz 3: Pattern rulesDate change- moved from 09/12 to 09/19. Read the instructions and"
    " complete the practice before the next les"
)
"""Geometry's quiz title, its description and a date-change note run together as written."""


def pasted(name: str, folder: pathlib.Path = CLIPBOARD) -> str:
    """A fixture's text as a paste gives it: its bytes decoded, every line ending kept."""
    return (folder / name).read_bytes().decode("utf-8")


def as_listed(entry: object) -> tuple[str, str]:
    """An inventory value as a presence and a text: a plain string is a reported value."""
    if isinstance(entry, str):
        return (Presence.REPORTED.value, entry)
    assert isinstance(entry, dict)
    return (entry["presence"], entry["text"])


def as_read(value: GradeValue) -> tuple[str, str]:
    return (value.presence.value, value.text)


def differences(name: str, listed: dict[str, Any], draft: GradeReportDraft) -> list[str]:
    """Each value of ``draft`` that differs from the inventory, said where it is."""
    found: list[str] = []

    def compare(where: str, expected: tuple[str, str], got: tuple[str, str]) -> None:
        if expected != got:
            found.append(f"{name}, {where}: listed {expected!r}, read {got!r}")

    compare("year", as_listed(listed["year"]), as_listed(draft.header.year_label))
    compare("term percent", as_listed(listed["term"]["percent"]), as_read(draft.term.percent))
    compare("term letter", as_listed(listed["term"]["letter"]), as_read(draft.term.letter))
    categories = listed["categories"]
    if len(categories) != len(draft.categories):
        found.append(f"{name}: {len(categories)} categories listed, {len(draft.categories)} read")
    for at, (expected, category) in enumerate(zip(categories, draft.categories, strict=False), 1):
        where = f"category {at}"
        compare(f"{where} name", as_listed(expected["name"]), as_read(category.name))
        compare(f"{where} weight", as_listed(expected["weight"]), as_read(category.weight))
        compare(f"{where} average", as_listed(expected["average"]), as_read(category.average))
        if len(expected["rows"]) != len(category.rows):
            found.append(
                f"{name}, {where}: {len(expected['rows'])} rows listed, {len(category.rows)} read"
            )
        for number, (cells, row) in enumerate(
            zip(expected["rows"], category.rows, strict=False), 1
        ):
            assert list(cells) == list(COLUMNS)
            for column, value in zip(COLUMNS, row.cells(), strict=True):
                compare(f"{where} row {number} {column}", as_listed(cells[column]), as_read(value))
    return found


def test_the_inventory_lists_forty_rows_of_eleven_cells() -> None:
    rows = [
        row
        for listed in INVENTORY.values()
        for category in listed["categories"]
        for row in category["rows"]
    ]
    assert sorted(CLASSES) == PASTES
    assert len(rows) == 40
    assert {tuple(row) for row in rows} == {tuple(COLUMNS)}


@pytest.mark.parametrize("name", PASTES)
def test_a_tab_separated_clipboard_report_is_read_whole(name: str) -> None:
    text = pasted(name)
    assert "\r\n" in text
    assert "\t\r\n" in text
    reading = read_grade_report(text)
    assert reading.not_read is None
    assert reading.draft is not None
    assert (reading.unrecognized, reading.places) == ((), ())


def test_every_cell_of_every_clipboard_report_is_read_as_its_inventory_lists_it() -> None:
    """Category membership and order, row counts, decimal places as written, blank Penalty and
    Note cells, empty categories with blank averages, term values and long titles."""
    found: list[str] = []
    for name in PASTES:
        reading = read_grade_report(pasted(name))
        if reading.draft is None:
            found.append(f"{name}: not read ({reading.not_read})")
            continue
        found += differences(name, INVENTORY[name], reading.draft)
    assert found == []


@pytest.mark.parametrize("name", PASTES)
def test_a_clipboard_report_s_header_is_read_and_the_teacher_never_kept(name: str) -> None:
    _, draft = read(pasted(name))
    code, class_name = CLASSES[name]
    assert draft.header.student_line == "Wren"
    assert (draft.header.class_code, draft.header.term_label) == (code, "T1")
    assert draft.header.class_name == class_name
    assert "Teacher" not in draft.model_dump_json()
    assert "Wren" not in canonical(draft)


def test_long_titles_are_kept_whole_and_an_embedded_date_is_never_the_due_date() -> None:
    _, draft = read(pasted("geometry-grade-report.txt"))
    homework, quizzes, tests = draft.categories
    longest = homework.rows[6]
    assert as_read(longest.assignment) == ("reported", LONG_TITLE)
    assert as_read(longest.due) == ("reported", "09/19")
    quiz = quizzes.rows[2]
    assert as_read(quiz.assignment) == ("reported", DATE_CHANGE_TITLE)
    assert as_read(quiz.due) == ("reported", "10/07")
    assert (len(homework.rows), len(quizzes.rows), tests.rows) == (15, 3, ())


@pytest.mark.parametrize("name", PASTES)
def test_lf_line_endings_read_as_crlf_do(name: str) -> None:
    seed, draft = read(pasted(name))
    reading, again = read(pasted(name).replace("\r\n", "\n"))
    assert again == draft
    assert (reading.unrecognized, reading.capture_key) == ((), seed.capture_key)


def every_value(draft: GradeReportDraft) -> list[tuple[str, tuple[str, str]]]:
    """Each value of ``draft`` but the student line, said where it is."""
    header = draft.header
    values = [
        ("header", ("", f"{header.year_label}|{header.class_code}|{header.term_label}")),
        ("class name", ("", header.class_name or "")),
        ("term percent", as_read(draft.term.percent)),
        ("term letter", as_read(draft.term.letter)),
    ]
    for at, category in enumerate(draft.categories, 1):
        values += [
            (f"category {at} name", as_read(category.name)),
            (f"category {at} weight", as_read(category.weight)),
            (f"category {at} average", as_read(category.average)),
        ]
        for number, row in enumerate(category.rows, 1):
            values += [
                (f"category {at} row {number} {column}", as_read(value))
                for column, value in zip(COLUMNS, row.cells(), strict=True)
            ]
    return values


@pytest.mark.parametrize("name", PASTES)
def test_a_report_read_from_tabs_and_from_markdown_has_one_capture_key(name: str) -> None:
    """The canonical form folds a cell's spaces, so the one cell the two layouts write apart,
    Geometry's longest title with the space its tab cell keeps, gives the same key."""
    tabs, from_tabs = read(pasted(name))
    markdown, from_markdown = read(pasted(name, CLIPBOARD / "markdown"))
    apart = [
        (where, ours, theirs)
        for (where, ours), (_, theirs) in zip(
            every_value(from_tabs), every_value(from_markdown), strict=True
        )
        if ours != theirs
    ]
    expected = (
        [("category 1 row 7 Assignment", ("reported", LONG_TITLE), ("reported", LONG_TITLE[:-1]))]
        if name == "geometry-grade-report.txt"
        else []
    )
    assert apart == expected
    assert canonical(from_tabs) == canonical(from_markdown)
    assert tabs.capture_key == markdown.capture_key
    assert from_tabs.header.student_line == from_markdown.header.student_line == "Wren"


def test_the_student_line_stays_out_of_a_clipboard_report_s_key_and_its_check() -> None:
    """Two siblings' identical reports share a key and keep their own lines; a line left out
    reads as none; the name form of the clipboard's line is the Markdown line's."""
    text = pasted("science-grade-report.txt")
    seed, wren = read(text)
    sibling, linnet = read(replaced_once(text, "Wren\t2026", "Linnet\t2026"))
    missing, unnamed = read(replaced_once(text, "Wren\t2026", "\t2026"))
    assert (wren.header.student_line, linnet.header.student_line) == ("Wren", "Linnet")
    assert unnamed.header.student_line is None
    assert seed.capture_key == sibling.capture_key == missing.capture_key
    _, control = read(pasted("science-grade-report.txt", CLIPBOARD / "markdown"))
    key = name_form_key(b"synthetic household secret")
    assert control.header.student_line is not None
    assert name_form(key, "Wren") == name_form(key, control.header.student_line)
    assert name_form(key, "Wren") != name_form(key, "Linnet")


@pytest.mark.parametrize("name", PASTES)
def test_a_clipboard_reading_is_complete_by_its_rows_and_its_lines(name: str) -> None:
    """Complete because every physical result line became a row and no line went unplaced,
    not because the reader says so."""
    text = pasted(name)
    reading, draft = read(text)
    result_lines = [
        line
        for line in text.splitlines()
        if line.count("\t") == len(COLUMNS) - 1 and not line.startswith("Assignment\t")
    ]
    listed = sum(len(category["rows"]) for category in INVENTORY[name]["categories"])
    read_rows = [row for category in draft.categories for row in category.rows]
    assert len(result_lines) == listed == len(read_rows)
    assert [line.split("\t", 1)[0] for line in result_lines] == [
        row.assignment.text for row in read_rows
    ]
    assert (reading.unrecognized, reading.places, reading.held_back) == ((), (), ())
    assert reading_complete(reading) is True


SCIENCE = pasted("science-grade-report.txt")
LABS_TAB_LINE = "Labs\t\tWeight = 20.0\r\n"
SCIENCE_TERM = "Term Grade\t84.0\tB\t\t "
"""Science's last line, its trailing cells as the copy ends them, with no line ending."""
SCIENCE_ROW = (
    "Practice 4.3: classroom exercise\t9.0\t15.0\t64.0\tValid\t09/25\t0.0\t0.0\t\t1.0\t\r\n"
)
NEXT_SCIENCE_ROW = (
    "Practice 4.4: classroom exercise\t6.0\t12.0\t65.0\tValid\t10/03\t0.0\t0.0\t\t1.0\t"
)
"""The row after ``SCIENCE_ROW``: a stray line between the two may belong to either, so neither
is read."""


@pytest.mark.parametrize(
    ("before", "after", "shown", "places", "rows", "complete"),
    [
        (
            LABS_TAB_LINE,
            f"{BODY_LINE}\r\n{LABS_TAB_LINE}",
            (BODY_LINE,),
            ("before_term",),
            4,
            False,
        ),
        (SCIENCE_TERM, f"{SCIENCE_TERM}\r\n{PRINT_TIME}", (PRINT_TIME,), ("after_term",), 4, True),
        (
            SCIENCE_TERM,
            f"{SCIENCE_TERM}\r\nUpdated\t10/06",
            ("Updated\t10/06",),
            ("after_term",),
            4,
            False,
        ),
        (
            "Gradebook Student Progress Report",
            f"{PRINT_TIME}\r\nGradebook Student Progress Report",
            (PRINT_TIME,),
            ("before_header",),
            4,
            True,
        ),
        (
            SCIENCE_ROW,
            SCIENCE_ROW.replace("\t\t1.0", "\t1.0"),
            (SCIENCE_ROW.replace("\t\t1.0", "\t1.0").removesuffix("\r\n"),),
            ("before_term",),
            3,
            False,
        ),
        (
            SCIENCE_ROW,
            SCIENCE_ROW.replace("classroom exercise\t", "classroom\r\nexercise\t"),
            (
                "Practice 4.3: classroom",
                SCIENCE_ROW.removeprefix("Practice 4.3: classroom ").removesuffix("\r\n"),
            ),
            ("before_term", "before_term"),
            3,
            False,
        ),
        (
            SCIENCE_ROW,
            SCIENCE_ROW.replace("1.0\t\r\n", "1.0\tSee\r\nthe note\r\n"),
            (SCIENCE_ROW.replace("1.0\t\r\n", "1.0\tSee"), "the note", NEXT_SCIENCE_ROW),
            ("before_term", "before_term", "before_term"),
            2,
            False,
        ),
    ],
    ids=[
        "body-line",
        "text-after-term",
        "table-line-after-term",
        "line-before-header",
        "row-short-a-cell",
        "row-split-in-its-title",
        "row-split-in-its-note",
    ],
)
def test_a_clipboard_reading_s_completeness_follows_its_rows_and_unread_lines(
    before: str,
    after: str,
    shown: tuple[str, ...],
    places: tuple[str, ...],
    rows: int,
    complete: bool,
) -> None:
    """A line the reader didn't place keeps its place and its text, the rows it could read stay
    as they were, and a row split across lines is never joined or read in part."""
    seed, draft = read(SCIENCE)
    reading, again = read(replaced_once(SCIENCE, before, after))
    assert reading.unrecognized == shown
    assert reading.places == tuple(LinePlace(place) for place in places)
    assert sum(len(category.rows) for category in again.categories) == rows
    if rows == 4:
        assert again == draft
        assert reading.capture_key == seed.capture_key
    assert reading_complete(reading) is complete


def test_a_clipboard_report_cut_before_its_term_grade_is_incomplete() -> None:
    reading, draft = read(SCIENCE.removesuffix(SCIENCE_TERM))
    assert draft.term.percent.presence is Presence.NOT_CAPTURED
    assert draft.categories[-1].average.presence is Presence.NOT_CAPTURED
    assert reading_complete(reading) is False


@pytest.mark.parametrize(
    ("cell", "presence"),
    [
        ("0", Presence.REPORTED),
        ("0.00", Presence.REPORTED),
        ("", Presence.BLANK),
        (" ", Presence.BLANK),
        ("seven", Presence.UNREADABLE),
        ("EX", Presence.UNREADABLE),
        (" 9.0", Presence.UNREADABLE),
    ],
)
def test_a_tab_cell_keeps_its_text_and_its_presence(cell: str, presence: Presence) -> None:
    """A zero, a blank, unreadable text such as ``EX``, and a number with space inside its cell
    stay apart, each with its text as written."""
    row = replaced_once(SCIENCE_ROW, "\t9.0\t", f"\t{cell}\t")
    _, draft = read(replaced_once(SCIENCE, SCIENCE_ROW, row))
    points = draft.categories[1].rows[0].points
    assert (points.presence, points.text) == (presence, "" if presence is Presence.BLANK else cell)


def test_a_clipboard_table_without_its_note_column_leaves_each_note_not_captured() -> None:
    text = SCIENCE.replace("\tWeight\tNote\r\n", "\tWeight\r\n").replace("\t1.0\t\r\n", "\t1.0\r\n")
    reading, draft = read(text)
    rows = [row for category in draft.categories for row in category.rows]
    assert len(rows) == 4
    assert {row.note.presence for row in rows} == {Presence.NOT_CAPTURED}
    assert {row.penalty.presence for row in rows} == {Presence.BLANK}
    assert reading.unrecognized == ()


def test_a_markdown_paste_reads_a_tab_separated_line_as_it_reads_any_other_line() -> None:
    """The Markdown layout is chosen by its header, and its lines are read as before: a line of
    tabs in it is a line, never a table."""
    reading, draft = read(f"{REPORT}\nUpdated\t10/06\n")
    seed, _ = read(REPORT)
    assert draft == seed.draft
    assert (reading.unrecognized, reading.places) == (("Updated\t10/06",), (LinePlace.AFTER_TERM,))
    assert reading_complete(reading) is True


def test_a_tab_row_framed_by_pipes_is_read_as_a_tab_row() -> None:
    """Once the header chose the tab layout, a line splits only on its tabs: a title that starts
    with a pipe and a note that ends with one are the row's text, never a Markdown table."""
    framed = SCIENCE_ROW.replace("Practice 4.3", "|Practice 4.3").replace(
        "\t1.0\t\r\n", "\t1.0\tSee page 4|\r\n"
    )
    seed, _ = read(SCIENCE)
    reading, draft = read(replaced_once(SCIENCE, SCIENCE_ROW, framed))
    labs = draft.categories[1].rows
    assert len(labs) == 2
    assert (labs[0].assignment.text, labs[0].note.text) == (
        "|Practice 4.3: classroom exercise",
        "See page 4|",
    )
    assert (reading.unrecognized, reading.places) == ((), ())
    assert reading_complete(reading) is True


@pytest.mark.parametrize(
    ("before", "after", "held_back"),
    [
        (
            SCIENCE_ROW,
            SCIENCE_ROW.replace("classroom exercise\t", "classroom\r\nexercise\t"),
            (HeldBack(rows=(1,), stray=(0,)),),
        ),
        (
            SCIENCE_ROW,
            SCIENCE_ROW.replace("1.0\t\r\n", "1.0\tSee\r\nthe note\r\n"),
            (HeldBack(rows=(0, 2), stray=(1,)),),
        ),
        (LABS_TAB_LINE, f"{BODY_LINE}\r\n{LABS_TAB_LINE}", ()),
        (SCIENCE_ROW, SCIENCE_ROW.replace("\t\t1.0", "\t1.0"), ()),
    ],
    ids=["row-split-in-its-title", "row-split-in-its-note", "body-line", "row-short-a-cell"],
)
def test_a_reading_names_the_rows_it_held_back_and_the_stray_lines_beside_them(
    before: str, after: str, held_back: tuple[HeldBack, ...]
) -> None:
    """Each group is the rows a stray line made ambiguous and that line, as positions among the
    lines the reader didn't recognize, in report order; a line unread for another reason, or a
    stray line beside no row, holds nothing back."""
    reading, _ = read(replaced_once(SCIENCE, before, after))
    assert reading.held_back == held_back


def test_a_markdown_reading_holds_nothing_back_and_held_lines_are_kept_lines() -> None:
    reading, _ = read(REPORT)
    assert reading.held_back == ()
    for held_back in [
        (HeldBack(rows=(0,), stray=(1,)),),
        (HeldBack(rows=(1,), stray=(0,)), HeldBack(rows=(1,), stray=(2,))),
    ]:
        with pytest.raises(ValidationError):
            GradeReportReading(
                draft=reading.draft,
                not_read=None,
                unrecognized=("one line",),
                places=(LinePlace.BEFORE_TERM,),
                held_back=held_back,
            )
