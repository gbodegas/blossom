# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Which school result's score shows beside a homework assignment, read each time a page asks
and never stored.

Identity decides who competes: a result is a candidate for an assignment when its class is one
the assignment's class name connects to, their titles are equal but for capitalization and
spaces, and their dates are equal or can't be ruled out. A pair is ruled out by date only when
both dates are fully resolved and differ. A pair stands when the assignment has exactly one
candidate result and that result has exactly one candidate assignment. Display then decides
whether the pair shows: both dates fully resolved and equal, points the school reported, and a
result its class and term still show.

Everything arrives as plain values, hers alone: each side's day with whether it is fully
resolved, and the classes each assignment's name connects to. Nothing here reads a store, a
clock, a term in use or what is said about an assignment.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Never

from blossom.grades.draft import Presence, folded
from blossom.grades.review import CurrentValue


@dataclass(frozen=True)
class Homework:
    """A homework assignment as the rules read it: its ID, its title, and its due day with
    whether that day is fully resolved."""

    assignment_id: str
    title: str
    day: date | None
    resolved: bool


@dataclass(frozen=True)
class Result:
    """A school result with a current value: its class, its own term and its ID, its title when
    the school reported one, its due day with whether that day is fully resolved, whether its
    points are reported, and whether its class and term still show it."""

    class_id: str
    term: str
    result_id: str
    title: str | None
    day: date | None
    resolved: bool
    scored: bool
    shown: bool


@dataclass(frozen=True)
class ScoreAt:
    """The result whose score shows beside an assignment: its class, its own term and its ID."""

    class_id: str
    term: str
    result_id: str


def result_of(
    class_id: str, term: str, result_id: str, value: CurrentValue, day: date | None
) -> Result:
    """A result of ``class_id`` and ``term`` as the rules read its current value, with the day
    its due date resolves to, if it does. Only the school's cells are read, never a parent's
    assertion."""
    title, points = value.cells["assignment"], value.cells["points"]
    return Result(
        class_id=class_id,
        term=term,
        result_id=result_id,
        title=title[1] if title[0] is Presence.REPORTED else None,
        day=day,
        resolved=day is not None,
        scored=points[0] is Presence.REPORTED,
        shown=value.not_shown is None,
    )


def title_key(title: str) -> str:
    """A title as the rules compare it: spaces and capitalization folded, nothing else."""
    return folded(title).casefold()


def settled(side: Homework | Result) -> date | None:
    """A side's day when it is fully resolved, else None."""
    return side.day if side.resolved else None


def school_scores(
    homework: Iterable[Homework],
    results: Iterable[Result],
    classes: Mapping[str, Collection[str]],
    *,
    decided: Collection[Never] = (),
) -> dict[str, ScoreAt]:
    """The result each assignment shows, by assignment ID; an assignment that shows none is
    left out. ``classes`` gives, by assignment ID, the classes that assignment's name connects
    to. ``decided`` is where explicit links arrive; none is read, so it stays empty."""
    if decided:
        msg = "an explicit link has no rule here"
        raise ValueError(msg)
    assignments, school = list(homework), list(results)
    titled: dict[tuple[str, str], list[int]] = {}
    for place, result in enumerate(school):
        if result.title is not None:
            titled.setdefault((result.class_id, title_key(result.title)), []).append(place)
    candidates: list[list[int]] = []
    rivals = [0] * len(school)
    for one in assignments:
        day = settled(one)
        key = title_key(one.title)
        mine = []
        for class_id in set(classes.get(one.assignment_id, ())):
            for place in titled.get((class_id, key), ()):
                other = settled(school[place])
                if day is None or other is None or day == other:
                    mine.append(place)
                    rivals[place] += 1
        candidates.append(mine)
    shown: dict[str, ScoreAt] = {}
    for one, mine in zip(assignments, candidates, strict=True):
        if len(mine) != 1 or rivals[mine[0]] != 1:
            continue
        result = school[mine[0]]
        day = settled(one)
        if day is None or day != settled(result):
            continue
        if result.scored and result.shown:
            shown[one.assignment_id] = ScoreAt(result.class_id, result.term, result.result_id)
    return shown
