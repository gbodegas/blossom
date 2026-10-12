# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The school score beside homework: what a homework page reads from the gradebook, and each
assignment's score line with the way to its result on class details.

A page reads the grade facts once, inside its own hold of the record, and makes one mapping
from them, so every place on the page that lists an assignment shows the same line or none.
Which result is an assignment's is decided each time by ``school_scores`` and stored nowhere.
The term anyone has in use is never asked for: every term of a class's year that holds a
report is read, and a link names the result's own term by its place in that year.

Nothing a plan is made from reads this module or holds what it returns.
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import tzinfo

from blossom.grades.dates import due_date_of
from blossom.grades.review import CurrentValue
from blossom.grades.school_scores import Homework, Result, result_of, school_scores
from blossom.grades.wording import report_names, score_line
from blossom.noticing import read_date
from blossom.reconciliation import SourceRecord
from blossom.routes.navigation import CLASS_AT, address
from blossom.stores.gradebook import ClassTermRead, GradebookRecords, HomeworkClasses, class_for
from blossom.stores.project_state import Assignment


@dataclass(frozen=True)
class YearRead:
    """A school year as a homework page reads it: the year's classes with a parent's answers,
    its first month, its terms in the order an address counts them, and the reports and
    current values of each class a homework name connects to, by class and term."""

    mapping: HomeworkClasses
    first_month: int | None
    terms: tuple[str, ...]
    reads: Mapping[tuple[str, str], ClassTermRead]


@dataclass(frozen=True)
class ScoreShown:
    """An assignment's school score as a page shows it: the line, and the address of its
    result on class details."""

    line: str
    details: str


def read_grade_facts(store: GradebookRecords, courses: Iterable[str]) -> tuple[YearRead, ...]:
    """Every school year on record, with the results of each class one of the homework class
    names ``courses`` connects to, in each term of its year that holds a report. With no
    homework nothing is read. A read alone, made inside the caller's hold of the record."""
    names = set(courses)
    if not names:
        return ()
    by_year: dict[str, list[str]] = {}
    for year, term in store.grade_contexts().terms:
        by_year.setdefault(year, []).append(term)
    years = []
    for year, terms in by_year.items():
        mapping = store.homework_classes(year)
        connected = {class_for(name, mapping) for name in names} - {None}
        reads: dict[tuple[str, str], ClassTermRead] = {}
        first_month = store.first_month_of(year) if connected else None
        for term in terms if connected else ():
            for one in store.classes_in(year, term):
                if one.reported and one.class_id in connected:
                    reads[(one.class_id, term)] = store.class_term(one.class_id, term)
        years.append(YearRead(mapping, first_month, tuple(terms), reads))
    return tuple(years)


def results_read(years: Iterable[YearRead]) -> bool:
    """Whether any class and term was read: with none, no assignment shows a score."""
    return any(year.reads for year in years)


def homework_of(item: Assignment, claims: Sequence[SourceRecord], unreadable: bool) -> Homework:
    """An assignment as the score rules read it. Its date is fully resolved when the record has
    one, every claim about it can be read, and each claim reads as that same day."""
    day = item.due_date
    resolved = (
        day is not None
        and not unreadable
        and all(read_date(claim.asserted_value) == day for claim in claims)
    )
    return Homework(item.assignment_id, item.title, day, resolved)


def scores_shown(
    assignments: Iterable[Assignment],
    claims: Mapping[str, Sequence[SourceRecord]],
    unreadable: Collection[str],
    years: Iterable[YearRead],
    *,
    zone: tzinfo,
    base: str,
) -> dict[str, ScoreShown]:
    """Each assignment's school score, by assignment ID, for a page under the grade tree
    ``base``; an assignment that shows none is left out. ``assignments`` is every assignment on
    record, ``claims`` each one's claims about its date, and ``unreadable`` the assignments
    with a claim that can't be read."""
    listed = list(assignments)
    homework = [
        homework_of(item, claims.get(item.assignment_id, ()), item.assignment_id in unreadable)
        for item in listed
    ]
    classes: dict[str, list[str]] = {}
    results: list[Result] = []
    found: dict[tuple[str, str, str], tuple[CurrentValue, dict[str, str], int]] = {}
    for year in years:
        for item in listed:
            class_id = class_for(item.course, year.mapping)
            if class_id is not None:
                classes.setdefault(item.assignment_id, []).append(class_id)
        for (class_id, term), read in year.reads.items():
            names = report_names(read.reports, zone)
            place = year.terms.index(term) + 1
            for result_id, value in read.current.results.items():
                day = (
                    None
                    if value.due is None
                    else due_date_of(value.due.cell, year.mapping.year, year.first_month)
                )
                results.append(result_of(class_id, term, result_id, value, day))
                found[(class_id, term, result_id)] = (value, names, place)
    shown: dict[str, ScoreShown] = {}
    for assignment_id, at in school_scores(homework, results, classes).items():
        value, names, place = found[(at.class_id, at.term, at.result_id)]
        shown[assignment_id] = ScoreShown(
            line=score_line(value, names),
            details=address(
                CLASS_AT.format(base=base, class_id=at.class_id, n=place), fragment=at.result_id
            ),
        )
    return shown
