# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Reading a pasted gradebook report: every value as the report wrote it, with its presence;
the lines the reader didn't recognize, kept in order; and a capture key that names the same
report the same however the paste was spaced, wrapped or ended."""

import ast
import logging
import re
from collections.abc import Callable
from decimal import Decimal

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
from blossom.grades.text_reader import GradeReportReading, NotRead, read_grade_report
from blossom.settings import PACKAGE_ROOT
from tests.support import FIXTURES

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
        (GradeValue, " Valid", Presence.REPORTED),
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
