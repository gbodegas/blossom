# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The grade report reader's parse states: every line class read in every state.

Each pair is read twice: once to see what the line itself does, and once with a probe to see
the state it leaves, since a probe's outcome is one only that state gives among those a line can
lead to. The matrix here is kept by hand and closed against the reader's own states and line
classes, so a state or a class added to the reader fails until it is placed here.
"""

from collections.abc import Callable

import pytest

from blossom.grades.draft import GradeNumber, Presence
from blossom.grades.text_reader import (
    GradeReportReading,
    LineClass,
    NotRead,
    State,
    read_grade_report,
)

TITLE = "**Gradebook Student Progress Report**"
HEADER = (
    "| **Bramble, Wren** | **2026-2027** | **Teacher, Example** |\n"
    "| --- | --- | --- |\n"
    "| **07 BIO - C** | **T1** |  |"
)
COLUMNS = (
    "| **Assignment** | **Pts** | **Max** | **Avg** | **Status** | **Due** | **Curve** "
    "| **Bonus** | **Penalty** | **Weight** | **Note** |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
)
REORDERED = (
    "| **Assignment** | **Max** | **Pts** | **Avg** | **Status** | **Due** | **Curve** "
    "| **Bonus** | **Penalty** | **Weight** | **Note** |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
)
LABEL = "**Category Average**"
TERM = "| **Term Grade** | **81.9** | **B-** |"


def result(title: str) -> str:
    return f"| {title} | 5.0 | 10.0 | 50.0 | Valid | 09/30 | 0.0 | 0.0 |  | 1.0 |  |"


AFTER_THE_HEADER = f"{TITLE}\n{HEADER}"
IN_A_CATEGORY = (
    f"{AFTER_THE_HEADER}\n**PERCENT**\n| **Biology** |\n| --- |\n"
    "| **Homework** |  | **Weight = 15.0** |"
)
WITH_A_ROW = f"{IN_A_CATEGORY}\n{COLUMNS}\n{result('Seed Log')}"

PREFIXES: dict[State, str] = {
    State.BEFORE: "",
    State.TITLED: TITLE,
    State.HEADED: AFTER_THE_HEADER,
    State.PERCENT_READ: f"{AFTER_THE_HEADER}\n**PERCENT**",
    State.NAMED: f"{AFTER_THE_HEADER}\n**PERCENT**\n| **Biology** |\n| --- |",
    State.NEEDS_COLUMNS: IN_A_CATEGORY,
    State.ROWS: WITH_A_ROW,
    State.LABELED: f"{WITH_A_ROW}\n{LABEL}",
    State.LABELED_AFTER_OTHER: f"{WITH_A_ROW}\n{LABEL}\nPrinted 10/06/2026 9:21 PM",
    State.CLOSED: f"{WITH_A_ROW}\n{LABEL}\n**80.0**",
    State.AFTER_TERM: f"{WITH_A_ROW}\n{LABEL}\n**80.0**\n{TERM}",
}
"""A paste up to the point where the reader is in each state."""

SAMPLES: dict[str, tuple[LineClass, str]] = {
    "blank": (LineClass.BLANK, ""),
    "title": (LineClass.TITLE, TITLE),
    "print": (LineClass.PRINT, "**Print**"),
    "percent": (LineClass.PERCENT, "PERCENT"),
    "label": (LineClass.LABEL, "**Category  Average**"),
    "value": (LineClass.VALUE, "**42.0**"),
    "other": (LineClass.OTHER, "Printed 10/07/2026 7:00 AM"),
    "layout": (LineClass.LAYOUT, "| - | - |"),
    "class_table": (LineClass.CLASS_TABLE, "| **Sample Class** |\n| --- |"),
    "category": (LineClass.CATEGORY, "| **Sample Category** |  | **Weight = 5.0** |"),
    "term": (LineClass.TERM, "| **Term Grade** | **70.0** | **C-** |"),
    "columns": (LineClass.COLUMNS, COLUMNS),
    "reordered_columns": (LineClass.COLUMNS, REORDERED),
    "row": (LineClass.ROW, result("Sample Row")),
    "short_row": (LineClass.ROW, "| Sample Row | 5.0 | 10.0 |"),
    "header": (LineClass.HEADER, HEADER),
}
"""A line of each class, none of them the same text as a prefix's or a probe's line. The bold
Print, the plain PERCENT and the spaced label are also bold or plain values, so precedence
decides them."""

KEPT, SHOWN, NOT_READ = "kept", "shown", "not read"
Cell = tuple[str, State | None]


def matrix_row(stays: State, **listed: Cell) -> dict[str, Cell]:
    """A state's row: blank and layout kept, and every other sample shown with the state as it
    was, except those listed."""
    row: dict[str, Cell] = dict.fromkeys(SAMPLES, (SHOWN, stays))
    row.update(blank=(KEPT, stays), layout=(KEPT, stays))
    if stays not in (State.BEFORE, State.TITLED):
        row.update(title=(NOT_READ, None), header=(NOT_READ, None))
    row.update(listed)
    return row


CLOSING: dict[str, Cell] = {
    "category": (KEPT, State.NEEDS_COLUMNS),
    "term": (KEPT, State.AFTER_TERM),
}
"""The two lines that close whatever category is open and go on."""
INTERRUPTING = ("columns", "reordered_columns", "row", "short_row", "class_table", "label")
"""The structures that close a labeled category, its average not captured."""

MATRIX: dict[State, dict[str, Cell]] = {
    State.BEFORE: matrix_row(
        State.BEFORE,
        title=(KEPT, State.TITLED),
        print=(KEPT, State.BEFORE),
        header=(KEPT, State.HEADED),
    ),
    State.TITLED: matrix_row(
        State.TITLED,
        title=(NOT_READ, None),
        print=(KEPT, State.TITLED),
        header=(KEPT, State.HEADED),
    ),
    State.HEADED: matrix_row(
        State.HEADED,
        percent=(KEPT, State.PERCENT_READ),
        class_table=(KEPT, State.NAMED),
        **CLOSING,
    ),
    State.PERCENT_READ: matrix_row(State.PERCENT_READ, class_table=(KEPT, State.NAMED), **CLOSING),
    State.NAMED: matrix_row(State.NAMED, **CLOSING),
    State.NEEDS_COLUMNS: matrix_row(
        State.NEEDS_COLUMNS,
        columns=(KEPT, State.ROWS),
        reordered_columns=(KEPT, State.ROWS),
        label=(KEPT, State.LABELED),
        **CLOSING,
    ),
    State.ROWS: matrix_row(
        State.ROWS,
        row=(KEPT, State.ROWS),
        columns=(SHOWN, State.CLOSED),
        reordered_columns=(SHOWN, State.CLOSED),
        label=(KEPT, State.LABELED),
        **CLOSING,
    ),
    State.LABELED: matrix_row(
        State.LABELED,
        value=(KEPT, State.CLOSED),
        print=(SHOWN, State.LABELED_AFTER_OTHER),
        percent=(SHOWN, State.LABELED_AFTER_OTHER),
        other=(SHOWN, State.LABELED_AFTER_OTHER),
        **dict.fromkeys(INTERRUPTING, (SHOWN, State.CLOSED)),
        **CLOSING,
    ),
    State.LABELED_AFTER_OTHER: matrix_row(
        State.LABELED_AFTER_OTHER,
        value=(KEPT, State.CLOSED),
        **dict.fromkeys(INTERRUPTING, (SHOWN, State.CLOSED)),
        **CLOSING,
    ),
    State.CLOSED: matrix_row(State.CLOSED, **CLOSING),
    State.AFTER_TERM: matrix_row(State.AFTER_TERM),
}
"""What each line class does in each state, and the state it leaves: the design's matrix."""

COMPLETIONS: dict[State, str] = {
    State.BEFORE: f"{HEADER}\n{TERM}",
    State.TITLED: f"{HEADER}\n{TERM}",
    **dict.fromkeys(
        (
            State.HEADED,
            State.PERCENT_READ,
            State.NAMED,
            State.NEEDS_COLUMNS,
            State.ROWS,
            State.LABELED,
            State.LABELED_AFTER_OTHER,
            State.CLOSED,
        ),
        TERM,
    ),
    State.AFTER_TERM: "",
}
"""What finishes a paste from each state, with nothing left over in that state."""

PROBE_CLASS = "| **Probe Class** |\n| --- |"
PROBE_CATEGORY = "| **Probe Category** |  | **Weight = 1.0** |"
Check = Callable[[GradeReportReading], bool]


def shown(reading: GradeReportReading, *lines: str) -> bool:
    return all(line in reading.unrecognized for line in lines)


def last(reading: GradeReportReading, back: int = 1) -> tuple[str, Presence, list[str]]:
    """The name, the average's presence and the result titles of a category, counted from the
    end of the draft."""
    assert reading.draft is not None
    category = reading.draft.categories[-back]
    titles = [row.assignment.text for row in category.rows]
    return category.name.text, category.average.presence, titles


PROBES: dict[State, list[tuple[str, Check]]] = {
    State.BEFORE: [
        (f"{TITLE}\n{HEADER}\n{TERM}", lambda r: r.draft is not None and not shown(r, TITLE)),
    ],
    State.TITLED: [
        (f"{TITLE}\n{HEADER}\n{TERM}", lambda r: r.not_read is NotRead.SEVERAL_REPORTS),
    ],
    State.HEADED: [
        (
            f"**PERCENT**\n{PROBE_CLASS}\n{TERM}",
            lambda r: not shown(r, "**PERCENT**")
            and r.draft is not None
            and r.draft.header.class_name == "Probe Class",
        ),
    ],
    State.PERCENT_READ: [
        (
            f"**PERCENT**\n{PROBE_CLASS}\n{TERM}",
            lambda r: shown(r, "**PERCENT**")
            and r.draft is not None
            and r.draft.header.class_name == "Probe Class",
        ),
    ],
    State.NAMED: [
        (
            f"{PROBE_CLASS}\n{LABEL}\n{PROBE_CATEGORY}\n{TERM}",
            lambda r: shown(r, "| **Probe Class** |", LABEL) and last(r)[0] == "Probe Category",
        ),
    ],
    State.NEEDS_COLUMNS: [
        (
            f"{result('Probe Row')}\n{COLUMNS}\n{result('Probe Row Two')}\n{TERM}",
            lambda r: shown(r, result("Probe Row")) and last(r)[2][-1:] == ["Probe Row Two"],
        ),
    ],
    State.ROWS: [
        (f"{result('Probe Row')}\n{TERM}", lambda r: last(r)[2][-1:] == ["Probe Row"]),
    ],
    State.LABELED: [
        (f"{PROBE_CATEGORY}\n{TERM}", lambda r: last(r, 2)[1] is Presence.BLANK),
    ],
    State.LABELED_AFTER_OTHER: [
        (f"**42.5**\n{TERM}", lambda r: not shown(r, "**42.5**")),
        (f"{PROBE_CATEGORY}\n{TERM}", lambda r: last(r, 2)[1] is Presence.NOT_CAPTURED),
    ],
    State.CLOSED: [
        (
            f"**42.5**\n{LABEL}\n{PROBE_CATEGORY}\n{TERM}",
            lambda r: shown(r, "**42.5**", LABEL) and last(r)[0] == "Probe Category",
        ),
    ],
    State.AFTER_TERM: [
        (
            PROBE_CATEGORY,
            lambda r: shown(r, PROBE_CATEGORY)
            and r.draft is not None
            and all(category.name.text != "Probe Category" for category in r.draft.categories),
        ),
    ],
}
"""For each state, probes whose outcomes only that state gives among the states a line can lead
to from the same prefix."""


def pasted(*parts: str) -> str:
    return "\n".join(part for part in parts if part)


def test_the_matrix_covers_every_state_and_line_class_the_reader_has() -> None:
    assert set(MATRIX) == set(PREFIXES) == set(PROBES) == set(COMPLETIONS) == set(State)
    assert {kind for kind, _ in SAMPLES.values()} == set(LineClass)
    assert all(set(row) == set(SAMPLES) for row in MATRIX.values())


def test_every_prefix_ends_in_its_state() -> None:
    for state, prefix in PREFIXES.items():
        for probe, check in PROBES[state]:
            assert check(read_grade_report(pasted(prefix, probe))), state


@pytest.mark.parametrize(
    ("state", "sample"), [(state, sample) for state in State for sample in SAMPLES]
)
def test_each_line_does_what_its_state_says_and_leaves_the_state_the_matrix_names(
    state: State, sample: str
) -> None:
    outcome, after = MATRIX[state][sample]
    line = SAMPLES[sample][1]
    first = line.split("\n")[0]
    if outcome == NOT_READ:
        reading = read_grade_report(pasted(PREFIXES[state], line, COMPLETIONS[state]))
        assert reading.not_read is NotRead.SEVERAL_REPORTS
        return
    assert after is not None
    reading = read_grade_report(pasted(PREFIXES[state], line, COMPLETIONS[after]))
    assert reading.draft is not None, reading.not_read
    if first:
        assert (first in reading.unrecognized) == (outcome == SHOWN)
    for probe, check in PROBES[after]:
        assert check(read_grade_report(pasted(PREFIXES[state], line, probe))), (after, probe)


END_OF_INPUT: dict[State, tuple[Presence | None, Presence]] = {
    State.HEADED: (None, Presence.NOT_CAPTURED),
    State.PERCENT_READ: (None, Presence.NOT_CAPTURED),
    State.NAMED: (None, Presence.NOT_CAPTURED),
    State.NEEDS_COLUMNS: (Presence.NOT_CAPTURED, Presence.NOT_CAPTURED),
    State.ROWS: (Presence.NOT_CAPTURED, Presence.NOT_CAPTURED),
    State.LABELED: (Presence.NOT_CAPTURED, Presence.NOT_CAPTURED),
    State.LABELED_AFTER_OTHER: (Presence.NOT_CAPTURED, Presence.NOT_CAPTURED),
    State.CLOSED: (Presence.REPORTED, Presence.NOT_CAPTURED),
    State.AFTER_TERM: (Presence.REPORTED, Presence.REPORTED),
}
"""A paste that ends in each state after the header: the open category's average, when there is
a category, and the term grade."""


@pytest.mark.parametrize("state", list(END_OF_INPUT))
def test_a_paste_that_ends_in_each_state_leaves_what_it_lacks_not_captured(state: State) -> None:
    average, term = END_OF_INPUT[state]
    reading = read_grade_report(PREFIXES[state])
    assert reading.draft is not None
    categories = reading.draft.categories
    assert (categories[-1].average.presence if categories else None) is average
    assert reading.draft.term.percent.presence is term
    if average is Presence.REPORTED:
        assert categories[-1].average == GradeNumber.read("80.0")
