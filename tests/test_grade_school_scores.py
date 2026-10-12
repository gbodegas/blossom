# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Which school result's score shows beside a homework assignment: who competes for it, exactly
one both ways, then whether the one pair shows.

Every case states plain values. No store, clock or page is read here.
"""

import ast
import dataclasses
import inspect
import pathlib
from datetime import date, timedelta
from typing import cast

import pytest

from blossom.grades import school_scores as rules
from blossom.grades.draft import Presence
from blossom.grades.review import Cell, CurrentValue, DueFrom, ReportAt
from blossom.grades.school_scores import Homework, Result, ScoreAt, result_of, school_scores

DAY = date(2026, 9, 22)
NEXT_DAY = DAY + timedelta(days=1)
TITLE = "Angle Proofs"
GEOMETRY = "class-geometry"
BIOLOGY = "class-biology"
LAST_YEAR = "class-geometry-last-year"


def homework(
    assignment_id: str = "homework-1",
    *,
    title: str = TITLE,
    day: date | None = DAY,
    resolved: bool | None = None,
) -> Homework:
    """A homework assignment, its date fully resolved unless ``resolved`` says otherwise or it
    has none."""
    return Homework(
        assignment_id=assignment_id,
        title=title,
        day=day,
        resolved=day is not None if resolved is None else resolved,
    )


def result(
    result_id: str = "result-1",
    *,
    class_id: str = GEOMETRY,
    term: str = "T1",
    title: str | None = TITLE,
    day: date | None = DAY,
    scored: bool = True,
    shown: bool = True,
) -> Result:
    """A result with a current value, its date fully resolved when it has one."""
    return Result(
        class_id=class_id,
        term=term,
        result_id=result_id,
        title=title,
        day=day,
        resolved=day is not None,
        scored=scored,
        shown=shown,
    )


def connected(
    *assignments: Homework, to: tuple[str, ...] = (GEOMETRY,)
) -> dict[str, tuple[str, ...]]:
    """Each assignment's name connected to the classes ``to``."""
    return {one.assignment_id: to for one in assignments}


def test_one_assignment_and_one_result_show_the_result_in_its_own_term() -> None:
    one = homework()

    shown = school_scores([one], [result(term="T2")], connected(one))

    assert shown == {"homework-1": ScoreAt(class_id=GEOMETRY, term="T2", result_id="result-1")}


@pytest.mark.parametrize(
    "twin",
    [
        pytest.param(result("result-2"), id="both clean"),
        pytest.param(result("result-2", scored=False), id="points not reported"),
        pytest.param(result("result-2", shown=False), id="not shown in the newest report"),
        pytest.param(result("result-2", day=None), id="due blank or not captured"),
        pytest.param(result("result-2", term="T2"), id="another term"),
        pytest.param(result("result-1", term="T2"), id="one ID in another term"),
    ],
)
def test_a_second_result_that_cannot_be_ruled_out_leaves_nothing_to_show(twin: Result) -> None:
    """A result of the class with the title competes unless its own date is fully resolved and
    different, whatever its score, its showing or its term."""
    one = homework()

    assert school_scores([one], [result(), twin], connected(one)) == {}
    assert school_scores([one], [twin, result()], connected(one)) == {}


@pytest.mark.parametrize(
    "other",
    [
        pytest.param(homework("homework-2"), id="the same date, as a Done row has it"),
        pytest.param(homework("homework-2", resolved=False), id="a source disagrees"),
        pytest.param(
            homework("homework-2", day=NEXT_DAY, resolved=False), id="a claim can't be read"
        ),
        pytest.param(homework("homework-2", day=None), id="no due date"),
        pytest.param(homework("homework-2", title="angle  PROOFS"), id="a twin spelled apart"),
    ],
)
def test_a_second_assignment_that_cannot_be_ruled_out_leaves_nothing_on_either(
    other: Homework,
) -> None:
    """Every assignment on record counts, whatever is said about it: one with the class and the
    title competes unless its own date is fully resolved and different."""
    one = homework()

    assert school_scores([one, other], [result()], connected(one, other)) == {}
    assert school_scores([other, one], [result()], connected(one, other)) == {}


def test_a_competitor_leaves_the_count_only_with_a_resolved_different_date() -> None:
    one, later = homework(), homework("homework-2", day=NEXT_DAY)
    mine, other = result(), result("result-2", day=NEXT_DAY - timedelta(days=2))
    at = ScoreAt(GEOMETRY, "T1", "result-1")

    assert school_scores([one], [mine, other], connected(one)) == {"homework-1": at}
    assert school_scores([one, later], [mine], connected(one, later)) == {"homework-1": at}
    assert school_scores([one, later], [mine, other], connected(one, later)) == {"homework-1": at}


def test_two_pairs_each_show_their_own_result() -> None:
    one, later = homework(), homework("homework-2", day=NEXT_DAY)
    results = [result(), result("result-2", day=NEXT_DAY)]

    assert school_scores([one, later], results, connected(one, later)) == {
        "homework-1": ScoreAt(GEOMETRY, "T1", "result-1"),
        "homework-2": ScoreAt(GEOMETRY, "T1", "result-2"),
    }


@pytest.mark.parametrize(
    ("written", "shows"),
    [
        ("  angle\tPROOFS ", True),
        ("ANGLE PROOFS", True),
        ("Angle Proofs.", False),
        ("Angle-Proofs", False),
        ("Angle Pröofs", False),
        ("Angle Proof", False),
        ("AngleProofs", False),
    ],
)
def test_titles_match_but_for_capitalization_and_spaces_alone(written: str, shows: bool) -> None:
    one = homework(title=written)
    other = homework(title=TITLE)

    assert bool(school_scores([one], [result()], connected(one))) is shows
    assert bool(school_scores([other], [result(title=written)], connected(other))) is shows


def test_a_result_whose_title_was_not_reported_is_no_candidate() -> None:
    one = homework(title="")

    assert school_scores([one], [result(title=None)], connected(one)) == {}


def test_two_classes_with_one_title_and_date_show_only_the_connected_class_s_result() -> None:
    one = homework()
    results = [result("result-b", class_id=BIOLOGY), result("result-g")]

    assert school_scores([one], results, connected(one)) == {
        "homework-1": ScoreAt(GEOMETRY, "T1", "result-g")
    }
    assert school_scores([one], results, connected(one, to=(BIOLOGY,))) == {
        "homework-1": ScoreAt(BIOLOGY, "T1", "result-b")
    }


def test_a_class_given_twice_counts_once_and_two_connected_classes_compete() -> None:
    one = homework()
    results = [result("result-b", class_id=BIOLOGY), result("result-g")]

    assert school_scores([one], results, connected(one, to=(GEOMETRY, GEOMETRY))) == {
        "homework-1": ScoreAt(GEOMETRY, "T1", "result-g")
    }
    assert school_scores([one], results, connected(one, to=(BIOLOGY, GEOMETRY))) == {}


def test_an_assignment_shows_no_result_of_a_class_its_name_is_not_connected_to() -> None:
    one = homework()

    assert school_scores([one], [result()], connected(one, to=())) == {}
    assert school_scores([one], [result()], {}) == {}
    assert school_scores([one], [result()], connected(one, to=(BIOLOGY,))) == {}


def test_a_name_connected_in_two_years_shows_the_result_whose_date_is_equal() -> None:
    """Each year's class brings its results into the count, and a fully resolved date of
    another year rules its result out."""
    one = homework()
    results = [
        result("result-then", class_id=LAST_YEAR, term="T4", day=DAY.replace(year=2025)),
        result("result-now"),
    ]

    assert school_scores([one], results, connected(one, to=(LAST_YEAR, GEOMETRY))) == {
        "homework-1": ScoreAt(GEOMETRY, "T1", "result-now")
    }


def test_another_year_s_result_with_an_unresolved_date_leaves_nothing_to_show() -> None:
    one = homework()
    results = [result("result-then", class_id=LAST_YEAR, term="T4", day=None), result()]

    assert school_scores([one], results, connected(one, to=(LAST_YEAR, GEOMETRY))) == {}
    assert school_scores([one], results, connected(one)) == {
        "homework-1": ScoreAt(GEOMETRY, "T1", "result-1")
    }


@pytest.mark.parametrize(
    ("one", "school"),
    [
        pytest.param(homework(day=None), result(), id="homework undated"),
        pytest.param(homework(resolved=False), result(), id="homework date in doubt"),
        pytest.param(
            homework(day=NEXT_DAY, resolved=False), result(), id="a claim not read as a date"
        ),
        pytest.param(homework(), result(day=None), id="result date not resolved"),
        pytest.param(homework(), result(day=NEXT_DAY), id="dates differ by a day"),
        pytest.param(homework(), result(scored=False), id="points not reported"),
        pytest.param(homework(), result(shown=False), id="not shown in the newest report"),
    ],
)
def test_each_display_rule_failing_alone_shows_nothing(one: Homework, school: Result) -> None:
    assert school_scores([one], [school], connected(one)) == {}


def test_a_date_marked_resolved_without_a_day_is_read_as_unresolved() -> None:
    one = homework(day=None, resolved=True)
    school = dataclasses.replace(result(day=None), resolved=True)

    assert school_scores([one], [result()], connected(one)) == {}
    assert school_scores([homework()], [school], connected(one)) == {}


def test_no_explicit_link_decided_gives_what_the_rules_give_and_stores_nothing() -> None:
    one, other = homework(), homework("homework-2", day=NEXT_DAY)
    homework_read, results = [one, other], [result(), result("result-2", day=NEXT_DAY)]
    classes = connected(one, other)
    before = (list(homework_read), list(results), dict(classes))

    ruled = school_scores(homework_read, results, classes)

    assert ruled == {
        "homework-1": ScoreAt(GEOMETRY, "T1", "result-1"),
        "homework-2": ScoreAt(GEOMETRY, "T1", "result-2"),
    }
    assert school_scores(homework_read, results, classes, decided=()) == ruled
    assert (homework_read, results, classes) == before


def test_an_explicit_link_is_refused_since_none_is_read() -> None:
    one = homework()

    with pytest.raises(ValueError, match="explicit link"):
        school_scores(
            [one], [result()], connected(one), decided=cast("tuple[()]", (("homework-1",),))
        )


def test_the_rules_read_no_state_about_an_assignment_and_no_term_in_use() -> None:
    """What decides is closed: an assignment's ID, title and date, with the classes its name
    connects to. Her update, a hand-in and the household's current term have no way in."""
    assert [field.name for field in dataclasses.fields(Homework)] == [
        "assignment_id",
        "title",
        "day",
        "resolved",
    ]
    assert [field.name for field in dataclasses.fields(Result)] == [
        "class_id",
        "term",
        "result_id",
        "title",
        "day",
        "resolved",
        "scored",
        "shown",
    ]
    assert list(inspect.signature(school_scores).parameters) == [
        "homework",
        "results",
        "classes",
        "decided",
    ]


def test_the_rules_import_no_store_clock_route_or_date_reader() -> None:
    source = pathlib.Path(inspect.getfile(rules)).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert imported == {
        "blossom.grades.draft",
        "blossom.grades.review",
        "collections.abc",
        "dataclasses",
        "datetime",
        "typing",
    }


BLANK: Cell = (Presence.BLANK, "")
POINTS: dict[Presence, Cell] = {
    Presence.REPORTED: (Presence.REPORTED, "0.0"),
    Presence.BLANK: BLANK,
    Presence.UNREADABLE: (Presence.UNREADABLE, "EX"),
    Presence.NOT_CAPTURED: (Presence.NOT_CAPTURED, ""),
}
"""A Pts cell in each presence, the reported one a zero."""


def current(**cells: Cell) -> dict[str, Cell]:
    """A result's cells: a reported title and score, with ``cells`` over them."""
    return {
        "assignment": (Presence.REPORTED, TITLE),
        "points": (Presence.REPORTED, "25.0"),
        "max_points": (Presence.REPORTED, "30.0"),
        "average": (Presence.REPORTED, "83.3"),
        **cells,
    }


@pytest.mark.parametrize("presence", list(Presence))
def test_a_result_has_a_score_only_when_its_points_are_reported(presence: Presence) -> None:
    value = CurrentValue(cells=current(points=POINTS[presence]), report_id="report", order=1)

    read = result_of(GEOMETRY, "T1", "result-1", value, DAY)

    assert read.scored is (presence is Presence.REPORTED)
    assert bool(school_scores([homework()], [read], connected(homework()))) is read.scored


@pytest.mark.parametrize(
    "cells",
    [
        pytest.param({"average": BLANK}, id="average left blank"),
        pytest.param({"max_points": (Presence.NOT_CAPTURED, "")}, id="maximum not in the copy"),
        pytest.param({"points": (Presence.REPORTED, "0.0")}, id="a zero"),
    ],
)
def test_a_result_with_reported_points_shows_whatever_its_other_cells_hold(
    cells: dict[str, Cell],
) -> None:
    value = CurrentValue(cells=current(**cells), report_id="report", order=1)

    assert result_of(GEOMETRY, "T1", "result-1", value, DAY) == result()


def test_a_parent_s_assertion_is_never_read_as_the_school_s_cell() -> None:
    told = {"points": (Presence.REPORTED, "30.0"), "assignment": (Presence.REPORTED, "Other")}
    blank = CurrentValue(cells=current(points=BLANK), report_id="report", order=1, asserted=told)
    reported = CurrentValue(
        cells=current(), report_id="report", order=1, asserted={"points": BLANK}
    )

    assert result_of(GEOMETRY, "T1", "result-1", blank, DAY) == result(scored=False)
    assert result_of(GEOMETRY, "T1", "result-1", reported, DAY) == result()


def test_a_result_is_read_with_its_own_place_title_day_and_showing() -> None:
    at = ReportAt("report-2", 2)
    due = DueFrom((Presence.REPORTED, "09/22"), at)
    plain = CurrentValue(cells=current(), report_id="report", order=1, last_shown=at, due=due)
    gone = dataclasses.replace(plain, not_shown=at)
    untitled = dataclasses.replace(plain, cells=current(assignment=(Presence.UNREADABLE, TITLE)))

    assert result_of(BIOLOGY, "T3", "result-9", plain, DAY) == result(
        "result-9", class_id=BIOLOGY, term="T3"
    )
    assert result_of(GEOMETRY, "T1", "result-1", plain, None) == result(day=None)
    assert result_of(GEOMETRY, "T1", "result-1", gone, DAY) == result(shown=False)
    assert result_of(GEOMETRY, "T1", "result-1", untitled, DAY) == result(title=None)
