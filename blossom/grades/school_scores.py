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

A parent's links are read before that count. Each link confirms one pair or keeps it apart.
A pair with a link of either kind is no candidate, and neither is any other pair of an
assignment or of a result that a confirmed link names, whether or not that link's other side
is among the values given. A linked assignment shows the results its confirmed links name and
no other, each when it is among the results given, its class is one the assignment's name
connects to, the school reported points, and its class and term still show it. A confirmed
pair's titles and dates are not read, and it is not counted. Two confirmed links that name one
result are refused whole with ``HomeworkLinksConflict``.

A result waits for a parent's choice when it has a candidate pair with no link, no confirmed
link names the result, and the rules stand none of its pairs. For this alone, a pair whose
assignment has a confirmed link to another result still counts as the result's. Whether a
result waits never depends on its points or on whether its class and term still show it.

Everything arrives as plain values, hers alone: each side's day with whether it is fully
resolved, the classes each assignment's name connects to, and a parent's standing links.
Nothing here reads a store, a clock, a term in use or what is said about an assignment.
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import NamedTuple

from blossom.grades.draft import Presence, folded
from blossom.grades.review import CurrentValue, HomeworkLinksConflict


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
    """A result whose score shows beside an assignment: its class, its own term and its ID,
    and whether it stands by a parent's link or else by the rules."""

    class_id: str
    term: str
    result_id: str
    linked: bool


@dataclass(frozen=True)
class Link:
    """A parent's standing decision about one assignment and one result, each by its ID: the
    pair is confirmed as linked, or else kept apart."""

    assignment_id: str
    result_id: str
    confirmed: bool


@dataclass(frozen=True)
class Waiting:
    """A result that waits for a parent's choice. ``assignments`` and ``results`` count what
    could be it and what could be its one assignment's, each 0 unless two or more; then whether
    that assignment has another linked result, or the two are alone with a day unresolved."""

    class_id: str
    term: str
    result_id: str
    assignments: int
    results: int
    linked_elsewhere: bool
    unsettled: bool


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


class Pairs(NamedTuple):
    """The pairs left once a parent's links are read: the results in a list; per assignment,
    the places of the results in its pairs with no link (``unjudged``) and of the one the rules
    stand it with, if any (``stood``); and the result IDs each assignment's confirmed links name."""

    school: list[Result]
    unjudged: list[list[int]]
    stood: list[int | None]
    named: dict[str, set[str]]


def pairs_of(
    assignments: Sequence[Homework],
    results: Iterable[Result],
    classes: Mapping[str, Collection[str]],
    links: Collection[Link],
) -> Pairs:
    """The step both answers share: who competes once a parent's links are read, and which
    pairs the rules stand before points and showing are read. Two confirmed links that name
    one result raise ``HomeworkLinksConflict`` before anything is paired."""
    named: dict[str, set[str]] = {}
    held: set[str] = set()
    for link in links:
        if not link.confirmed:
            continue
        if link.result_id in held:
            raise HomeworkLinksConflict
        held.add(link.result_id)
        named.setdefault(link.assignment_id, set()).add(link.result_id)
    judged = {(link.assignment_id, link.result_id) for link in links}
    school = list(results)
    titled: dict[tuple[str, str], list[int]] = {}
    for place, result in enumerate(school):
        if result.title is not None and result.result_id not in held:
            titled.setdefault((result.class_id, title_key(result.title)), []).append(place)
    unjudged: list[list[int]] = []
    rivals = [0] * len(school)
    for one in assignments:
        day = settled(one)
        key = title_key(one.title)
        mine = []
        for class_id in set(classes.get(one.assignment_id, ())):
            for place in titled.get((class_id, key), ()):
                if (one.assignment_id, school[place].result_id) in judged:
                    continue
                other = settled(school[place])
                if day is None or other is None or day == other:
                    mine.append(place)
                    # A linked assignment's pairs stay unjudged and are out of the count.
                    if one.assignment_id not in named:
                        rivals[place] += 1
        unjudged.append(mine)
    stood: list[int | None] = []
    for one, mine in zip(assignments, unjudged, strict=True):
        alone = one.assignment_id not in named and len(mine) == 1 and rivals[mine[0]] == 1
        day = settled(one)
        both = alone and day is not None and day == settled(school[mine[0]])
        stood.append(mine[0] if both else None)
    return Pairs(school, unjudged, stood, named)


def school_scores(
    homework: Iterable[Homework],
    results: Iterable[Result],
    classes: Mapping[str, Collection[str]],
    *,
    decided: Collection[Link] = (),
) -> dict[str, tuple[ScoreAt, ...]]:
    """The results each assignment shows, by assignment ID, in the order ``results`` gives
    them: its linked ones, or else the one the rules stand. One that shows none is left out.
    ``classes`` gives each assignment's connected classes; ``decided``, a parent's links."""
    assignments = list(homework)
    school, _, stood, named = pairs_of(assignments, results, classes, decided)
    shown: dict[str, tuple[ScoreAt, ...]] = {}
    for one, place in zip(assignments, stood, strict=True):
        links = named.get(one.assignment_id)
        mine: tuple[Result, ...]
        if links is None:
            mine = () if place is None else (school[place],)
        else:
            connects = classes.get(one.assignment_id, ())
            mine = tuple(
                result
                for result in school
                if result.result_id in links and result.class_id in connects
            )
        showing = tuple(
            ScoreAt(result.class_id, result.term, result.result_id, linked=links is not None)
            for result in mine
            if result.scored and result.shown
        )
        if showing:
            shown[one.assignment_id] = showing
    return shown


def waiting_results(
    homework: Iterable[Homework],
    results: Iterable[Result],
    classes: Mapping[str, Collection[str]],
    *,
    decided: Collection[Link] = (),
) -> tuple[Waiting, ...]:
    """The results that wait for a parent's choice, in the order ``results`` gives them, each
    with every reason that holds for it. The parameters are those of ``school_scores``."""
    assignments = list(homework)
    school, unjudged, stood, named = pairs_of(assignments, results, classes, decided)
    could_be: list[list[int]] = [[] for _ in school]
    for index, mine in enumerate(unjudged):
        for place in mine:
            could_be[place].append(index)
    matched = {place for place in stood if place is not None}
    waiting = []
    for place, result in enumerate(school):
        # A result a confirmed link names has no unjudged pair, so it never waits.
        mine = could_be[place]
        if not mine or place in matched:
            continue
        several = len(mine) > 1
        theirs = 0 if several else len(unjudged[mine[0]])
        elsewhere = not several and assignments[mine[0]].assignment_id in named
        waiting.append(
            Waiting(
                class_id=result.class_id,
                term=result.term,
                result_id=result.result_id,
                assignments=len(mine) if several else 0,
                results=theirs if theirs > 1 else 0,
                linked_elsewhere=elsewhere,
                # Alone both ways, unlinked and not stood by the rules: a day is unresolved.
                unsettled=theirs == 1 and not elsewhere,
            )
        )
    return tuple(waiting)
