# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Which school result's score shows beside a homework assignment: who competes for it, exactly
one both ways, then whether the one pair shows; what a parent's links change before that count;
and which results wait for a parent's choice.

Every case states plain values. No store, clock or page is read here.
"""

import ast
import dataclasses
import inspect
import pathlib
from collections.abc import Callable
from datetime import date, timedelta

import pytest

from blossom.grades import school_scores as rules
from blossom.grades.draft import Presence
from blossom.grades.review import (
    Cell,
    CurrentValue,
    DueFrom,
    GradeValueUndecodable,
    HomeworkLinksConflict,
    ReportAt,
)
from blossom.grades.school_scores import (
    Candidates,
    Homework,
    Link,
    Result,
    ScoreAt,
    Waiting,
    candidate_homework,
    result_of,
    results_to_check,
    school_scores,
    status_to_check,
    waiting_results,
)

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
    status: str | None = None,
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
        status=status,
    )


def connected(
    *assignments: Homework, to: tuple[str, ...] = (GEOMETRY,)
) -> dict[str, tuple[str, ...]]:
    """Each assignment's name connected to the classes ``to``."""
    return {one.assignment_id: to for one in assignments}


def link(assignment_id: str, result_id: str) -> Link:
    """A parent's link of an assignment and a result."""
    return Link(assignment_id, result_id, confirmed=True)


def apart(assignment_id: str, result_id: str) -> Link:
    """A parent's decision that keeps an assignment and a result apart."""
    return Link(assignment_id, result_id, confirmed=False)


def score_at(
    result_id: str = "result-1", *, linked: bool, class_id: str = GEOMETRY, term: str = "T1"
) -> ScoreAt:
    """A result as an answer names it, standing by a link or by the rules."""
    return ScoreAt(class_id, term, result_id, linked=linked)


def waits(
    result_id: str = "result-1",
    *,
    class_id: str = GEOMETRY,
    term: str = "T1",
    assignments: int = 0,
    results: int = 0,
    linked_elsewhere: bool = False,
    unsettled: bool = False,
) -> Waiting:
    """A waiting result as an answer names it, with the reasons that hold for it."""
    return Waiting(class_id, term, result_id, assignments, results, linked_elsewhere, unsettled)


def test_one_assignment_and_one_result_show_the_result_in_its_own_term() -> None:
    one = homework()

    shown = school_scores([one], [result(term="T2")], connected(one))

    assert shown == {
        "homework-1": (ScoreAt(class_id=GEOMETRY, term="T2", result_id="result-1", linked=False),)
    }


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
    at = (ScoreAt(GEOMETRY, "T1", "result-1", linked=False),)

    assert school_scores([one], [mine, other], connected(one)) == {"homework-1": at}
    assert school_scores([one, later], [mine], connected(one, later)) == {"homework-1": at}
    assert school_scores([one, later], [mine, other], connected(one, later)) == {"homework-1": at}


def test_two_pairs_each_show_their_own_result() -> None:
    one, later = homework(), homework("homework-2", day=NEXT_DAY)
    results = [result(), result("result-2", day=NEXT_DAY)]

    assert school_scores([one, later], results, connected(one, later)) == {
        "homework-1": (ScoreAt(GEOMETRY, "T1", "result-1", linked=False),),
        "homework-2": (ScoreAt(GEOMETRY, "T1", "result-2", linked=False),),
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
        "homework-1": (ScoreAt(GEOMETRY, "T1", "result-g", linked=False),)
    }
    assert school_scores([one], results, connected(one, to=(BIOLOGY,))) == {
        "homework-1": (ScoreAt(BIOLOGY, "T1", "result-b", linked=False),)
    }


def test_a_class_given_twice_counts_once_and_two_connected_classes_compete() -> None:
    one = homework()
    results = [result("result-b", class_id=BIOLOGY), result("result-g")]

    assert school_scores([one], results, connected(one, to=(GEOMETRY, GEOMETRY))) == {
        "homework-1": (ScoreAt(GEOMETRY, "T1", "result-g", linked=False),)
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
        "homework-1": (ScoreAt(GEOMETRY, "T1", "result-now", linked=False),)
    }


def test_another_year_s_result_with_an_unresolved_date_leaves_nothing_to_show() -> None:
    one = homework()
    results = [result("result-then", class_id=LAST_YEAR, term="T4", day=None), result()]

    assert school_scores([one], results, connected(one, to=(LAST_YEAR, GEOMETRY))) == {}
    assert school_scores([one], results, connected(one)) == {
        "homework-1": (ScoreAt(GEOMETRY, "T1", "result-1", linked=False),)
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
        "homework-1": (ScoreAt(GEOMETRY, "T1", "result-1", linked=False),),
        "homework-2": (ScoreAt(GEOMETRY, "T1", "result-2", linked=False),),
    }
    assert school_scores(homework_read, results, classes, decided=()) == ruled
    assert (homework_read, results, classes) == before


def test_a_parent_s_links_are_read_and_the_values_given_are_left_as_they_were() -> None:
    one, other = homework(), homework("homework-2", day=NEXT_DAY)
    homework_read, results = [one, other], [result(), result("result-2", day=NEXT_DAY)]
    classes = connected(one, other)
    links = [apart("homework-1", "result-1"), link("homework-2", "result-2")]
    before = (list(homework_read), list(results), dict(classes), list(links))

    shown = school_scores(homework_read, results, classes, decided=links)

    assert shown == {"homework-2": (score_at("result-2", linked=True),)}
    assert (homework_read, results, classes, links) == before


ONE, TWIN = homework(), homework("homework-2")
RENAMED = homework("homework-2", title="Proof Set")
FIRST, SECOND = result(), result("result-2")
OTHER_TITLE = result("result-2", title="Proof Set")


@pytest.mark.parametrize(
    ("homework_read", "results", "links", "shown"),
    [
        pytest.param([ONE], [FIRST], [], {"homework-1": (score_at(linked=False),)}, id="no link"),
        pytest.param([ONE], [FIRST], [apart("homework-1", "result-1")], {}, id="kept apart"),
        pytest.param([ONE, TWIN], [FIRST], [], {}, id="two assignments, one result"),
        pytest.param(
            [ONE, TWIN],
            [FIRST],
            [apart("homework-1", "result-1")],
            {"homework-2": (score_at(linked=False),)},
            id="two assignments, one kept apart: the other shows by the rule",
        ),
        pytest.param(
            [ONE, TWIN],
            [FIRST],
            [link("homework-1", "result-1")],
            {"homework-1": (score_at(linked=True),)},
            id="two assignments, one linked: the other shows nothing",
        ),
        pytest.param([ONE], [FIRST, SECOND], [], {}, id="one assignment, two results"),
        pytest.param(
            [ONE],
            [FIRST, SECOND],
            [apart("homework-1", "result-1")],
            {"homework-1": (score_at("result-2", linked=False),)},
            id="two results, one kept apart: the other shows by the rule",
        ),
        pytest.param(
            [ONE],
            [FIRST, SECOND],
            [link("homework-1", "result-1")],
            {"homework-1": (score_at(linked=True),)},
            id="two results, one linked: the other shows nowhere",
        ),
        pytest.param(
            [ONE],
            [FIRST, SECOND],
            [link("homework-1", "result-2"), link("homework-1", "result-1")],
            {"homework-1": (score_at(linked=True), score_at("result-2", linked=True))},
            id="two results, both linked: both show, in the order the results came",
        ),
        pytest.param(
            [ONE],
            [SECOND, FIRST],
            [link("homework-1", "result-2"), link("homework-1", "result-1")],
            {"homework-1": (score_at("result-2", linked=True), score_at(linked=True))},
            id="the same two results given in the other order",
        ),
        pytest.param(
            [ONE],
            [FIRST, result("result-2", term="T2")],
            [link("homework-1", "result-1"), link("homework-1", "result-2")],
            {"homework-1": (score_at(linked=True), score_at("result-2", term="T2", linked=True))},
            id="two linked results in two terms: each keeps its own term",
        ),
        pytest.param(
            [ONE],
            [FIRST, result("result-2", scored=False)],
            [link("homework-1", "result-1"), link("homework-1", "result-2")],
            {"homework-1": (score_at(linked=True),)},
            id="two linked results, one without points: one shows",
        ),
        pytest.param(
            [ONE, TWIN],
            [FIRST, SECOND],
            [link("homework-1", "result-1")],
            {
                "homework-1": (score_at(linked=True),),
                "homework-2": (score_at("result-2", linked=False),),
            },
            id="two assignments and two results, one pair linked: the others pair by the rule",
        ),
        pytest.param(
            [ONE, RENAMED],
            [FIRST],
            [link("homework-2", "result-1")],
            {"homework-2": (score_at(linked=True),)},
            id="a result linked to other homework leaves the rule's assignment with nothing",
        ),
        pytest.param(
            [ONE],
            [FIRST, OTHER_TITLE],
            [link("homework-1", "result-2")],
            {"homework-1": (score_at("result-2", linked=True),)},
            id="a linked assignment shows no other result by the rule",
        ),
        pytest.param(
            [ONE],
            [FIRST, dataclasses.replace(OTHER_TITLE, scored=False)],
            [link("homework-1", "result-2")],
            {},
            id="a linked result with nothing to show leaves its assignment with nothing",
        ),
        pytest.param(
            [ONE],
            [FIRST],
            [link("homework-1", "result-gone")],
            {},
            id="a link to a result not given still takes its assignment out of the rule",
        ),
        pytest.param(
            [ONE],
            [FIRST],
            [link("homework-gone", "result-1")],
            {},
            id="a link to homework not given still takes its result out of the rule",
        ),
        pytest.param(
            [ONE],
            [FIRST],
            [apart("homework-gone", "result-1"), apart("homework-1", "result-gone")],
            {"homework-1": (score_at(linked=False),)},
            id="a pair kept apart takes neither of its sides out of the rule",
        ),
    ],
)
def test_a_parent_s_links_apply_before_the_count(
    homework_read: list[Homework],
    results: list[Result],
    links: list[Link],
    shown: dict[str, tuple[ScoreAt, ...]],
) -> None:
    """A pair kept apart leaves the candidates before the count, and a linked pair takes both
    of its sides out. Every assignment's name is connected to the one class."""
    classes = connected(*homework_read)

    assert school_scores(homework_read, results, classes, decided=links) == shown


def test_a_twin_result_in_another_term_or_another_year_s_class_is_settled_by_a_link() -> None:
    one = homework()
    terms = [result(), result("result-2", term="T2")]
    years = [result("result-then", class_id=LAST_YEAR, term="T4", day=None), result()]
    both = connected(one, to=(LAST_YEAR, GEOMETRY))

    assert school_scores([one], terms, connected(one)) == {}
    assert school_scores(
        [one], terms, connected(one), decided=[link("homework-1", "result-2")]
    ) == {"homework-1": (score_at("result-2", term="T2", linked=True),)}
    assert school_scores([one], years, both) == {}
    assert school_scores([one], years, both, decided=[link("homework-1", "result-then")]) == {
        "homework-1": (ScoreAt(LAST_YEAR, "T4", "result-then", linked=True),)
    }


@pytest.mark.parametrize(
    ("homework_read", "results"),
    [
        pytest.param([RENAMED], [FIRST], id="titles differ"),
        pytest.param([homework(resolved=False)], [FIRST], id="homework date in doubt"),
        pytest.param([homework(day=None)], [FIRST], id="homework undated"),
        pytest.param(
            [homework(day=NEXT_DAY, resolved=False)], [FIRST], id="a claim not read as a date"
        ),
        pytest.param([ONE], [result(day=None)], id="result date not resolved"),
        pytest.param([ONE], [result(day=NEXT_DAY)], id="both dates resolved and different"),
        pytest.param([ONE, TWIN], [FIRST], id="a second assignment that could be the result's"),
        pytest.param([ONE], [FIRST, SECOND], id="a second result that could be the assignment's"),
    ],
)
def test_a_link_shows_its_result_whatever_the_titles_the_dates_and_the_count_say(
    homework_read: list[Homework], results: list[Result]
) -> None:
    """Each case shows nothing on the linked assignment by the rules alone, and its linked
    result with the link."""
    mine = homework_read[0].assignment_id
    classes = connected(*homework_read)
    made = link(mine, "result-1")

    assert mine not in school_scores(homework_read, results, classes)
    assert school_scores(homework_read, results, classes, decided=[made])[mine] == (
        score_at(linked=True),
    )


@pytest.mark.parametrize(
    ("results", "to"),
    [
        pytest.param([result(class_id=BIOLOGY)], (GEOMETRY,), id="class not connected"),
        pytest.param([FIRST], (), id="name connected to no class"),
        pytest.param([result(scored=False)], (GEOMETRY,), id="points not reported"),
        pytest.param([result(shown=False)], (GEOMETRY,), id="not shown in the newest report"),
        pytest.param([SECOND], (GEOMETRY,), id="the result is not among those read"),
    ],
)
def test_a_link_shows_nothing_without_the_class_reported_points_and_a_result_still_shown(
    results: list[Result], to: tuple[str, ...]
) -> None:
    """Each case differs in one thing from a linked pair that shows, whose titles differ so the
    rules alone show nothing."""
    links = [link("homework-2", "result-1")]

    assert school_scores([RENAMED], [FIRST], connected(RENAMED), decided=links) == {
        "homework-2": (score_at(linked=True),)
    }
    assert school_scores([RENAMED], results, connected(RENAMED, to=to), decided=links) == {}


@pytest.mark.parametrize("reads", [school_scores, waiting_results])
@pytest.mark.parametrize(
    "links",
    [
        pytest.param([link("homework-1", "result-1"), link("homework-2", "result-1")], id="two"),
        pytest.param(
            [
                link("homework-1", "result-1"),
                apart("homework-1", "result-2"),
                link("homework-2", "result-1"),
                link("homework-3", "result-1"),
            ],
            id="three",
        ),
        pytest.param(
            [link("homework-1", "result-gone"), link("homework-2", "result-gone")],
            id="two that name a result not given",
        ),
    ],
)
def test_two_or_more_confirmed_links_naming_one_result_are_refused_whole(
    reads: Callable[..., object], links: list[Link]
) -> None:
    """Each function raises the conflict error itself, with its own message and with no cause
    and no context, and returns nothing."""
    with pytest.raises(GradeValueUndecodable) as refused:
        reads([ONE, TWIN], [FIRST, SECOND], connected(ONE, TWIN), decided=links)

    assert type(refused.value) is HomeworkLinksConflict
    assert str(refused.value) == "two confirmed homework links name one result"
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None


def test_one_assignment_linked_to_two_results_and_pairs_kept_apart_are_no_conflict() -> None:
    links = [
        link("homework-1", "result-1"),
        link("homework-1", "result-2"),
        apart("homework-2", "result-1"),
        apart("homework-2", "result-2"),
    ]
    classes = connected(ONE, TWIN)

    assert school_scores([ONE, TWIN], [FIRST, SECOND], classes, decided=links) == {
        "homework-1": (score_at(linked=True), score_at("result-2", linked=True))
    }
    assert waiting_results([ONE, TWIN], [FIRST, SECOND], classes, decided=links) == ()


THIRD = result("result-3")
IN_DOUBT = homework(resolved=False)


@pytest.mark.parametrize(
    ("homework_read", "results", "links", "waiting"),
    [
        pytest.param([ONE, TWIN], [FIRST], [], (waits(assignments=2),), id="two assignments"),
        pytest.param(
            [ONE],
            [FIRST, SECOND],
            [],
            (waits(results=2), waits("result-2", results=2)),
            id="two results",
        ),
        pytest.param(
            [ONE],
            [FIRST, result("result-2", term="T2")],
            [],
            (waits(results=2), waits("result-2", term="T2", results=2)),
            id="two results, one in another term",
        ),
        pytest.param([IN_DOUBT], [FIRST], [], (waits(unsettled=True),), id="homework in doubt"),
        pytest.param(
            [ONE], [result(day=None)], [], (waits(unsettled=True),), id="result date not resolved"
        ),
        pytest.param([ONE], [FIRST], [], (), id="one each way, both dates resolved and equal"),
        pytest.param([ONE], [FIRST], [apart("homework-1", "result-1")], (), id="kept apart"),
        pytest.param(
            [ONE, TWIN],
            [FIRST],
            [apart("homework-1", "result-1")],
            (),
            id="two assignments, one kept apart: the rule stands the other",
        ),
        pytest.param(
            [ONE, homework("homework-2", resolved=False)],
            [FIRST],
            [apart("homework-1", "result-1")],
            (waits(unsettled=True),),
            id="two assignments, one kept apart, the other in doubt",
        ),
        pytest.param(
            [ONE, TWIN, homework("homework-3")],
            [FIRST],
            [apart("homework-1", "result-1")],
            (waits(assignments=2),),
            id="three assignments, one kept apart",
        ),
        pytest.param(
            [ONE, TWIN],
            [FIRST],
            [link("homework-1", "result-1")],
            (),
            id="a linked result",
        ),
        pytest.param(
            [ONE],
            [FIRST, SECOND],
            [link("homework-1", "result-1")],
            (waits("result-2", linked_elsewhere=True),),
            id="two results, one linked: the other waits",
        ),
        pytest.param(
            [ONE],
            [FIRST, SECOND],
            [link("homework-1", "result-1"), apart("homework-1", "result-2")],
            (),
            id="two results, one linked and the other kept apart",
        ),
        pytest.param(
            [IN_DOUBT],
            [FIRST, SECOND],
            [link("homework-1", "result-1")],
            (waits("result-2", linked_elsewhere=True),),
            id="two results, one linked, the homework in doubt: the link is the one reason",
        ),
        pytest.param(
            [ONE],
            [FIRST, SECOND, THIRD],
            [link("homework-1", "result-1")],
            (
                waits("result-2", results=2, linked_elsewhere=True),
                waits("result-3", results=2, linked_elsewhere=True),
            ),
            id="three results, one linked: two reasons hold for each of the others",
        ),
        pytest.param(
            [ONE, TWIN],
            [FIRST, SECOND],
            [link("homework-1", "result-1")],
            (),
            id="the rule stands the result on other homework",
        ),
        pytest.param(
            [ONE, homework("homework-2", day=None)],
            [FIRST, SECOND, result("result-3", day=NEXT_DAY)],
            [link("homework-1", "result-1")],
            (waits("result-2", assignments=2), waits("result-3", results=2)),
            id="linked homework alone with a result never stands it by the rule",
        ),
        pytest.param([RENAMED], [FIRST], [], (), id="no assignment has the title"),
    ],
)
def test_a_result_waits_when_a_pair_is_open_and_the_rule_stands_none_of_its_pairs(
    homework_read: list[Homework],
    results: list[Result],
    links: list[Link],
    waiting: tuple[Waiting, ...],
) -> None:
    """Each waiting result comes with every reason that holds, in the order the results came.
    The answer is the same with no points reported and with no result still shown, and no
    result both waits and shows by the rule."""
    classes = connected(*homework_read)
    unscored = [dataclasses.replace(one, scored=False) for one in results]
    gone = [dataclasses.replace(one, shown=False) for one in results]

    found = waiting_results(homework_read, results, classes, decided=links)

    assert found == waiting
    assert waiting_results(homework_read, unscored, classes, decided=links) == waiting
    assert waiting_results(homework_read, gone, classes, decided=links) == waiting
    ruled = {
        (one.class_id, one.term, one.result_id)
        for shown in school_scores(homework_read, results, classes, decided=links).values()
        for one in shown
        if not one.linked
    }
    assert ruled.isdisjoint((one.class_id, one.term, one.result_id) for one in found)


def test_a_result_of_another_connected_class_counts_and_an_unconnected_class_s_never_waits() -> (
    None
):
    one = homework()
    results = [result(), result("result-b", class_id=BIOLOGY)]

    assert waiting_results([one], results, connected(one, to=(GEOMETRY, BIOLOGY))) == (
        waits(results=2),
        waits("result-b", class_id=BIOLOGY, results=2),
    )
    assert waiting_results([one], results, connected(one, to=())) == ()
    assert waiting_results([IN_DOUBT], results, connected(IN_DOUBT)) == (waits(unsettled=True),)


def test_a_link_and_an_answer_hold_closed_fields_and_both_functions_take_the_same_inputs() -> None:
    assert [field.name for field in dataclasses.fields(Link)] == [
        "assignment_id",
        "result_id",
        "confirmed",
    ]
    assert [field.name for field in dataclasses.fields(ScoreAt)] == [
        "class_id",
        "term",
        "result_id",
        "linked",
    ]
    assert [field.name for field in dataclasses.fields(Waiting)] == [
        "class_id",
        "term",
        "result_id",
        "assignments",
        "results",
        "linked_elsewhere",
        "unsettled",
    ]
    assert inspect.signature(waiting_results).parameters.keys() == (
        inspect.signature(school_scores).parameters.keys()
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
        "status",
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
    """A result's cells: a reported title and score and a blank Status, with ``cells`` over
    them."""
    return {
        "assignment": (Presence.REPORTED, TITLE),
        "points": (Presence.REPORTED, "25.0"),
        "max_points": (Presence.REPORTED, "30.0"),
        "average": (Presence.REPORTED, "83.3"),
        "status": BLANK,
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


def could_be(
    result_id: str,
    *assignments: str,
    stood: str | None = None,
    class_id: str = GEOMETRY,
    term: str = "T1",
) -> Candidates:
    """A result as an answer names it, with its candidate assignments and the one that stands."""
    return Candidates(class_id, term, result_id, assignments, stood)


def place_of(one: Candidates | ScoreAt | Waiting) -> tuple[str, str, str]:
    """The class, term and ID an answer names a result by."""
    return (one.class_id, one.term, one.result_id)


@pytest.mark.parametrize(
    ("homework_read", "results", "links", "found"),
    [
        pytest.param(
            [ONE], [FIRST], [], (could_be("result-1", "homework-1", stood="homework-1"),), id="one"
        ),
        pytest.param(
            [ONE],
            [FIRST],
            [apart("homework-1", "result-1")],
            (could_be("result-1", "homework-1"),),
            id="one each way, kept apart",
        ),
        pytest.param(
            [ONE],
            [FIRST],
            [link("homework-1", "result-1")],
            (could_be("result-1", "homework-1"),),
            id="one each way, linked",
        ),
        pytest.param(
            [RENAMED], [FIRST], [link("homework-2", "result-1")], (), id="linked, titles differ"
        ),
        pytest.param(
            [ONE, RENAMED],
            [FIRST],
            [link("homework-2", "result-1")],
            (could_be("result-1", "homework-1"),),
            id="linked to homework of another title, with homework of its own title",
        ),
        pytest.param(
            [ONE],
            [FIRST],
            [link("homework-gone", "result-1")],
            (could_be("result-1", "homework-1"),),
            id="linked to homework not given",
        ),
        pytest.param(
            [ONE],
            [FIRST],
            [link("homework-1", "result-gone")],
            (could_be("result-1", "homework-1"),),
            id="its homework linked to a result not given",
        ),
        pytest.param(
            [ONE, TWIN],
            [FIRST],
            [],
            (could_be("result-1", "homework-1", "homework-2"),),
            id="two assignments",
        ),
        pytest.param(
            [TWIN, ONE],
            [FIRST],
            [],
            (could_be("result-1", "homework-2", "homework-1"),),
            id="two assignments given in the other order",
        ),
        pytest.param(
            [ONE, TWIN],
            [FIRST],
            [link("homework-1", "result-1")],
            (could_be("result-1", "homework-1", "homework-2"),),
            id="two assignments, one linked",
        ),
        pytest.param(
            [ONE, TWIN],
            [FIRST],
            [apart("homework-1", "result-1")],
            (could_be("result-1", "homework-1", "homework-2", stood="homework-2"),),
            id="two assignments, one kept apart: the rules stand the other",
        ),
        pytest.param(
            [ONE],
            [FIRST, SECOND],
            [],
            (could_be("result-1", "homework-1"), could_be("result-2", "homework-1")),
            id="two results",
        ),
        pytest.param(
            [ONE],
            [FIRST, SECOND],
            [link("homework-1", "result-1")],
            (could_be("result-1", "homework-1"), could_be("result-2", "homework-1")),
            id="two results, one linked",
        ),
        pytest.param(
            [ONE],
            [FIRST, SECOND],
            [link("homework-1", "result-1"), link("homework-1", "result-2")],
            (could_be("result-1", "homework-1"), could_be("result-2", "homework-1")),
            id="two results, both linked",
        ),
        pytest.param(
            [ONE],
            [FIRST, result("result-2", term="T2")],
            [link("homework-1", "result-2")],
            (could_be("result-1", "homework-1"), could_be("result-2", "homework-1", term="T2")),
            id="two results, the one in another term linked",
        ),
        pytest.param(
            [ONE, TWIN],
            [FIRST, SECOND],
            [link("homework-1", "result-1")],
            (
                could_be("result-1", "homework-1", "homework-2"),
                could_be("result-2", "homework-1", "homework-2", stood="homework-2"),
            ),
            id="two assignments and two results, one pair linked: the rules stand the other",
        ),
        pytest.param([IN_DOUBT], [FIRST], [], (could_be("result-1", "homework-1"),), id="doubt"),
    ],
)
def test_a_result_s_candidates_are_the_same_whatever_links_stand(
    homework_read: list[Homework],
    results: list[Result],
    links: list[Link],
    found: tuple[Candidates, ...],
) -> None:
    """Each result with a candidate, its candidates in the order the assignments came, and the
    one the rules stand it with: the candidates alike with no link, no points and no result
    shown. Every waiting result is among them; the ones that stand are the scores' own."""
    classes = connected(*homework_read)
    withheld = [dataclasses.replace(one, scored=False, shown=False) for one in results]
    before = (list(homework_read), list(results), dict(classes), list(links))

    answer = candidate_homework(homework_read, results, classes, decided=links)

    assert answer == found
    assert (homework_read, results, classes, links) == before
    assert candidate_homework(homework_read, withheld, classes, decided=links) == found
    assert [(place_of(one), one.assignments) for one in found] == [
        (place_of(one), one.assignments)
        for one in candidate_homework(homework_read, results, classes)
    ]
    ruled = {
        place_of(at): assignment_id
        for assignment_id, shown in school_scores(
            homework_read, results, classes, decided=links
        ).items()
        for at in shown
        if not at.linked
    }
    assert {place_of(one): one.stood for one in answer if one.stood is not None} == ruled
    waiting = waiting_results(homework_read, results, classes, decided=links)
    assert {place_of(one) for one in waiting} <= {place_of(one) for one in answer}


@pytest.mark.parametrize(
    ("one", "school", "could"),
    [
        pytest.param(homework(), result(), True, id="equal dates"),
        pytest.param(homework(resolved=False), result(), True, id="homework date in doubt"),
        pytest.param(homework(day=None), result(), True, id="homework undated"),
        pytest.param(homework(day=NEXT_DAY, resolved=False), result(), True, id="a claim not read"),
        pytest.param(homework(), result(day=None), True, id="result date not resolved"),
        pytest.param(homework(), result(day=NEXT_DAY), False, id="both resolved and different"),
        pytest.param(homework(title="angle  PROOFS"), result(), True, id="spelled apart"),
        pytest.param(homework(title="Angle Proof"), result(), False, id="another title"),
        pytest.param(homework(title=""), result(title=None), False, id="title not reported"),
        pytest.param(homework(), result(class_id=BIOLOGY), False, id="another class"),
    ],
)
def test_a_candidate_has_the_class_the_title_and_a_date_that_can_t_be_ruled_out(
    one: Homework, school: Result, could: bool
) -> None:
    found = candidate_homework([one], [school], connected(one))

    assert [held.assignments for held in found] == ([("homework-1",)] if could else [])
    assert candidate_homework([one], [school], {}) == ()


@pytest.mark.parametrize(
    "links",
    [
        pytest.param([link("homework-1", "result-1"), link("homework-2", "result-1")], id="two"),
        pytest.param(
            [link("homework-1", "result-gone"), link("homework-2", "result-gone")],
            id="two that name a result not given",
        ),
    ],
)
def test_candidates_are_refused_whole_on_two_confirmed_links_naming_one_result(
    links: list[Link],
) -> None:
    """The function raises the conflict error itself, with its own message and with no cause
    and no context, and returns nothing."""
    with pytest.raises(GradeValueUndecodable) as refused:
        candidate_homework([ONE, TWIN], [FIRST, SECOND], connected(ONE, TWIN), decided=links)

    assert type(refused.value) is HomeworkLinksConflict
    assert str(refused.value) == "two confirmed homework links name one result"
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None


def test_candidates_hold_closed_fields_and_take_the_inputs_the_scores_take() -> None:
    assert [field.name for field in dataclasses.fields(Candidates)] == [
        "class_id",
        "term",
        "result_id",
        "assignments",
        "stood",
    ]
    assert inspect.signature(candidate_homework).parameters.keys() == (
        inspect.signature(school_scores).parameters.keys()
    )


def titled(title: str, *assignment_ids: str, resolved: bool = True) -> list[Homework]:
    """Homework rows of one title on the shared day."""
    return [homework(one, title=title, resolved=resolved) for one in assignment_ids]


EACH_CASE_HOMEWORK = [
    *titled("Twin Homework", "twin-1", "twin-2"),
    *titled("Twin Results", "shared"),
    *titled("In Doubt", "doubt", resolved=False),
    *titled("No Points", "unscored"),
    *titled("Showing", "showing"),
    *titled("Kept Apart", "apart"),
    *titled("Twins No Points", "pointless-1", "pointless-2"),
    *titled("Linked", "linked"),
    *titled("Second Result", "second"),
    *titled("Both Ways", "first-row", "second-row"),
]
EACH_CASE_RESULTS = [
    result("twin-homework", title="Twin Homework"),
    result("twin-result-1", title="Twin Results"),
    result("twin-result-2", title="Twin Results", term="T2"),
    result("in-doubt", title="In Doubt"),
    result("no-points", title="No Points", scored=False),
    result("not-shown", title="Showing", shown=False),
    result("kept-apart", title="Kept Apart"),
    result("twins-no-points", title="Twins No Points", scored=False),
    result("linked", title="Linked"),
    result("first-linked", title="Second Result"),
    result("second-waits", title="Second Result"),
    result("row-linked", title="Both Ways"),
    result("row-ruled", title="Both Ways"),
    result("no-homework", title="No Homework"),
    result("unconnected", class_id=BIOLOGY, title="Twin Homework"),
]
EACH_CASE_LINKS = [
    apart("apart", "kept-apart"),
    link("linked", "linked"),
    link("second", "first-linked"),
    link("first-row", "row-linked"),
]
"""One class with a result for each way a result waits or does not: twins of either side, a
date in doubt, lone pairs without points or not shown, a pair kept apart, links, the rules
standing it elsewhere, no homework of its title, and a class no name connects to."""


def test_a_class_with_one_result_of_each_kind_names_each_one_s_candidates_and_who_waits() -> None:
    """No result both stands by the rules and waits, and each result with a candidate and no
    link of its own either stands, waits, or has every pair kept apart."""
    classes = connected(*EACH_CASE_HOMEWORK)

    found = candidate_homework(
        EACH_CASE_HOMEWORK, EACH_CASE_RESULTS, classes, decided=EACH_CASE_LINKS
    )
    waiting = waiting_results(
        EACH_CASE_HOMEWORK, EACH_CASE_RESULTS, classes, decided=EACH_CASE_LINKS
    )

    assert found == (
        could_be("twin-homework", "twin-1", "twin-2"),
        could_be("twin-result-1", "shared"),
        could_be("twin-result-2", "shared", term="T2"),
        could_be("in-doubt", "doubt"),
        could_be("no-points", "unscored", stood="unscored"),
        could_be("not-shown", "showing", stood="showing"),
        could_be("kept-apart", "apart"),
        could_be("twins-no-points", "pointless-1", "pointless-2"),
        could_be("linked", "linked"),
        could_be("first-linked", "second"),
        could_be("second-waits", "second"),
        could_be("row-linked", "first-row", "second-row"),
        could_be("row-ruled", "first-row", "second-row", stood="second-row"),
    )
    assert waiting == (
        waits("twin-homework", assignments=2),
        waits("twin-result-1", results=2),
        waits("twin-result-2", term="T2", results=2),
        waits("in-doubt", unsettled=True),
        waits("twins-no-points", assignments=2),
        waits("second-waits", linked_elsewhere=True),
    )
    stands = {place_of(one) for one in found if one.stood is not None}
    waits_now = {place_of(one) for one in waiting}
    linked = {one.result_id for one in EACH_CASE_LINKS if one.confirmed}
    assert stands.isdisjoint(waits_now)
    assert {
        one.result_id
        for one in found
        if one.result_id not in linked and place_of(one) not in stands | waits_now
    } == {"kept-apart"}


def test_another_year_s_result_is_a_candidate_until_both_dates_rule_it_out() -> None:
    one = homework()
    both = connected(one, to=(LAST_YEAR, GEOMETRY))
    then = result("result-then", class_id=LAST_YEAR, term="T4", day=None)
    dated = dataclasses.replace(then, day=DAY.replace(year=2025), resolved=True)

    assert candidate_homework([one], [then, result()], both) == (
        could_be("result-then", "homework-1", class_id=LAST_YEAR, term="T4"),
        could_be("result-1", "homework-1"),
    )
    assert candidate_homework([one], [dated, result()], both) == (
        could_be("result-1", "homework-1", stood="homework-1"),
    )


# ------------------------------------------------------------- results to check together

COUNTED_STATUSES = [
    "Missing",
    "Incomplete",
    "MISSING",
    " missing ",
    " Incomplete ",
    "Missing\t",
    "\nincomplete\n",
    "mIsSiNg",
]
"""Statuses the test counts: either word with its ends trimmed and its case ignored."""
NOT_COUNTED_STATUSES = [
    "Valid",
    "In complete",
    "Miss ing",
    "In  complete",
    "Missing work",
    "Missing?",
    "Late",
    "",
    " ",
]
"""Statuses the test never counts: an interior space kept, another word, or nothing."""


@pytest.mark.parametrize("status", COUNTED_STATUSES)
def test_a_status_of_missing_or_incomplete_counts_with_ends_trimmed_and_case_ignored(
    status: str,
) -> None:
    one = homework()
    linked = result(status=status)

    assert status_to_check(status) is True
    assert results_to_check(
        [one], [linked], connected(one), decided=[link("homework-1", "result-1")]
    ) == {"homework-1": (linked,)}


@pytest.mark.parametrize("status", [*NOT_COUNTED_STATUSES, None])
def test_any_other_status_or_none_reported_never_counts_and_no_interior_space_is_removed(
    status: str | None,
) -> None:
    one = homework()
    linked = result(status=status)

    assert status_to_check(status) is False
    assert (
        results_to_check([one], [linked], connected(one), decided=[link("homework-1", "result-1")])
        == {}
    )


def test_the_status_test_reads_each_run_of_whitespace_as_one_space() -> None:
    """Runs collapse to one space and are never removed, so a word split by a run of spaces,
    a tab or a line break stays two words."""
    assert [status_to_check(text) for text in ("In\tcomplete", "Miss\n ing", "In complete")] == [
        False,
        False,
        False,
    ]
    assert status_to_check("\t Missing \r\n") is True


@pytest.mark.parametrize(
    ("cell", "status"),
    [
        pytest.param((Presence.REPORTED, " Missing "), " Missing ", id="reported, as written"),
        pytest.param(BLANK, None, id="left blank"),
        pytest.param((Presence.UNREADABLE, "Missing"), None, id="unreadable"),
        pytest.param((Presence.NOT_CAPTURED, ""), None, id="not in the copy"),
    ],
)
def test_a_result_s_status_is_the_school_s_cell_as_written_only_when_reported(
    cell: Cell, status: str | None
) -> None:
    value = CurrentValue(cells=current(status=cell), report_id="report", order=1)

    read = result_of(GEOMETRY, "T1", "result-1", value, DAY)

    assert read == result(status=status)
    assert bool(
        results_to_check(
            [homework()], [read], connected(homework()), decided=[link("homework-1", "result-1")]
        )
    ) is (status is not None)


def test_a_parent_s_status_assertion_is_never_read_as_the_school_s_status() -> None:
    """Valid asserted on a Missing result leaves it counted, and Missing asserted on a Valid
    one never counts it."""
    told_valid = {"status": (Presence.REPORTED, "Valid")}
    told_missing = {"status": (Presence.REPORTED, "Missing")}
    missing = CurrentValue(
        cells=current(status=(Presence.REPORTED, "Missing")),
        report_id="report",
        order=1,
        asserted=told_valid,
    )
    valid = CurrentValue(
        cells=current(status=(Presence.REPORTED, "Valid")),
        report_id="report",
        order=1,
        asserted=told_missing,
    )
    links = [link("homework-1", "result-1")]
    one = homework()

    on_missing = result_of(GEOMETRY, "T1", "result-1", missing, DAY)
    on_valid = result_of(GEOMETRY, "T1", "result-1", valid, DAY)

    assert on_missing.status == "Missing"
    assert on_valid.status == "Valid"
    assert results_to_check([one], [on_missing], connected(one), decided=links) == {
        "homework-1": (on_missing,)
    }
    assert results_to_check([one], [on_valid], connected(one), decided=links) == {}


@pytest.mark.parametrize(
    ("cells", "counted"),
    [
        pytest.param({"status": (Presence.REPORTED, "Valid")}, False, id="Valid"),
        pytest.param(
            {"points": (Presence.REPORTED, "0.0"), "status": BLANK},
            False,
            id="points 0 with the Status blank",
        ),
        pytest.param(
            {"points": (Presence.REPORTED, "1.0"), "status": (Presence.REPORTED, "Valid")},
            False,
            id="a low score",
        ),
        pytest.param(
            {"points": BLANK, "status": (Presence.REPORTED, "Missing")},
            True,
            id="Missing with the points blank",
        ),
        pytest.param(
            {"points": (Presence.UNREADABLE, "EX"), "status": (Presence.REPORTED, "Incomplete")},
            True,
            id="Incomplete with the points unreadable",
        ),
        pytest.param(
            {"points": (Presence.REPORTED, "0.0"), "status": (Presence.REPORTED, "Missing")},
            True,
            id="Missing with a zero",
        ),
    ],
)
def test_a_linked_result_counts_by_its_status_and_its_points_are_not_read(
    cells: dict[str, Cell], counted: bool
) -> None:
    value = CurrentValue(cells=current(**cells), report_id="report", order=1)
    read = result_of(GEOMETRY, "T1", "result-1", value, DAY)
    one = homework()

    assert (
        bool(
            results_to_check(
                [one], [read], connected(one), decided=[link("homework-1", "result-1")]
            )
        )
        is counted
    )


def test_only_a_confirmed_link_counts_never_the_rules_pair_or_a_pair_kept_apart() -> None:
    """The rules stand the pair and its score shows, yet nothing counts without a link; a
    pair kept apart counts nothing either."""
    one = homework()
    missing = result(status="Missing")
    classes = connected(one)

    assert school_scores([one], [missing], classes) == {"homework-1": (score_at(linked=False),)}
    assert results_to_check([one], [missing], classes) == {}
    assert (
        results_to_check([one], [missing], classes, decided=[apart("homework-1", "result-1")]) == {}
    )
    assert results_to_check(
        [one], [missing], classes, decided=[link("homework-1", "result-1")]
    ) == {"homework-1": (missing,)}


def test_a_linked_result_counts_only_while_its_class_connects_and_it_is_still_shown() -> None:
    one = homework()
    links = [link("homework-1", "result-1")]
    missing = result(status="Missing")

    assert results_to_check([one], [missing], connected(one, to=(BIOLOGY,)), decided=links) == {}
    assert results_to_check([one], [missing], {}, decided=links) == {}
    assert (
        results_to_check(
            [one], [dataclasses.replace(missing, shown=False)], connected(one), decided=links
        )
        == {}
    )
    assert results_to_check([one], [missing], connected(one), decided=links) == {
        "homework-1": (missing,)
    }


def test_a_link_counts_whatever_titles_dates_and_twins_say() -> None:
    """A confirmed link is read as it stands: another title, another day and a twin of the
    homework change nothing about what it counts."""
    one = homework()
    twin = homework("homework-2")
    other = result(status="Missing", title="Something Else", day=NEXT_DAY)

    assert results_to_check(
        [one, twin], [other], connected(one, twin), decided=[link("homework-1", "result-1")]
    ) == {"homework-1": (other,)}


def test_a_link_naming_homework_not_given_counts_nothing() -> None:
    one = homework()
    missing = result(status="Missing")

    assert (
        results_to_check(
            [one], [missing], connected(one, homework("gone")), decided=[link("gone", "result-1")]
        )
        == {}
    )


def test_each_homework_is_given_once_with_each_counted_result_in_the_order_given() -> None:
    """Two linked results of two classes, both Missing, and a third linked one Valid: the
    homework is given once, with the two in the order the results came."""
    one = homework()
    biology = result("result-b", class_id=BIOLOGY, status="Missing")
    geometry = result("result-g", status="Incomplete")
    valid = result("result-v", status="Valid")
    links = [
        link("homework-1", "result-g"),
        link("homework-1", "result-b"),
        link("homework-1", "result-v"),
    ]

    assert results_to_check(
        [one], [biology, valid, geometry], connected(one, to=(GEOMETRY, BIOLOGY)), decided=links
    ) == {"homework-1": (biology, geometry)}


def test_results_to_check_are_refused_whole_on_two_confirmed_links_naming_one_result() -> None:
    one, two = homework(), homework("homework-2")
    links = [link("homework-1", "result-1"), link("homework-2", "result-1")]

    with pytest.raises(HomeworkLinksConflict) as raised:
        results_to_check([one, two], [result(status="Missing")], connected(one, two), decided=links)

    assert isinstance(raised.value, GradeValueUndecodable)
    assert raised.value.__cause__ is None


def test_results_to_check_take_the_inputs_the_scores_take_and_change_none_of_them() -> None:
    one = homework()
    results = [result(status="Missing")]
    classes = connected(one)
    links = [link("homework-1", "result-1")]
    before = (dataclasses.replace(one), list(results), dict(classes), list(links))

    results_to_check([one], results, classes, decided=links)

    assert inspect.signature(results_to_check).parameters.keys() == (
        inspect.signature(school_scores).parameters.keys()
    )
    assert (one, results, classes, links) == before


def test_g3c_i2_only_a_confirmed_link_to_a_result_reported_missing_or_incomplete_counts() -> None:
    """One class with a result for each value that never counts, each linked to homework of its
    own, besides one unlinked, one kept apart and one the rules stand: only the one result
    reported " Incomplete " counts."""
    reported, valid = Presence.REPORTED, (Presence.REPORTED, "Valid")
    linked_cells: dict[str, dict[str, Cell]] = {
        "low-score": {"points": (reported, "1.0"), "status": valid},
        "zero": {"points": (reported, "0.0"), "status": BLANK},
        "valid": {"status": valid},
        "blank": {"status": BLANK},
        "unreadable": {"status": (Presence.UNREADABLE, "Missing")},
        "not-captured": {"status": (Presence.NOT_CAPTURED, "")},
        "in-complete": {"status": (reported, "In complete")},
        "miss-ing": {"status": (reported, "Miss ing")},
        "counts": {"status": (reported, " Incomplete ")},
    }
    missing: dict[str, Cell] = {"status": (reported, "Missing")}
    every = {**linked_cells, "unlinked": missing, "apart": missing, "stood": missing}

    def read(name: str, cells: dict[str, Cell]) -> Result:
        told = {"status": (reported, "Missing")} if name == "valid" else {}
        value = CurrentValue(
            cells=current(assignment=(reported, name), **cells),
            report_id="report",
            order=1,
            asserted=told,
        )
        return result_of(GEOMETRY, "T1", f"result-{name}", value, DAY)

    results = [read(name, cells) for name, cells in every.items()]
    work = [homework(name, title=name) for name in every if name != "unlinked"]
    work.append(homework("unlinked", title="Another Title"))
    links = [link(name, f"result-{name}") for name in linked_cells]
    links.append(apart("apart", "result-apart"))

    counted = results_to_check(work, results, connected(*work), decided=links)

    assert counted == {"counts": (read("counts", linked_cells["counts"]),)}
    assert "stood" in school_scores(work, results, connected(*work), decided=links)
