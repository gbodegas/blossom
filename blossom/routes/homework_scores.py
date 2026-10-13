# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The school score beside homework: what a homework page reads from the gradebook, and each
assignment's score lines with the way to each result on class details.

A page reads the grade facts once, inside its own hold of the record, and makes one mapping
from them, so every place on the page that lists an assignment shows the same results or none.
Which result is an assignment's by the rules is decided each time by ``school_scores`` and
stored nowhere. A parent's homework links are read in the same hold, after the facts and only
when a class and term was read, and handed to those rules as plain values.
The current year and term are fetched and validated. Their valid values do not select results,
terms or links. Failure of that validation omits all school scores and Grade details links for
that page read. For each class a homework name connects to, every term of its year that holds
a report is read, and a link names the result's own term by its place in that year.

A page read validates the stored grade values it fetches: a cell's text and presence, a
report's order, row count and moments, an action's copied list and chain of sources, a school
year's first month, the term of each report of a class a homework name connects to against the
terms of that class's year, and every covered text column in rows actually returned by the
school-score reads, which must be stored as text and as UTF-8. A column that may hold
nothing keeps holding nothing: a null there is read as a null and is not refused. A value that
fails raises ``GradeValueUndecodable``, in the read or in the lines made from it, and leaves
the page without any school score: the page discards what it read and every line with it,
since a result left out of the count could make another look like an assignment's only one.
A link value that is not text fails the same way, and two confirmed links that name one result
raise ``HomeworkLinksConflict``, a kind of that error: the page shows no school score either.

A malformed key can cause SQL WHERE or JOIN to exclude a row before validation: a school year
label on an alias or on a parent's answer, a student ID, or the report ID that observations,
matches and corrections are joined on. Such excluded rows are not checked. Covered values
actually returned are validated. The consequence remains: a withheld score can appear, or a
removed connection can apply again, without a diagnostic.

A parent's stored link decisions are trusted to be the parent's. A page read checks that each
link value it fetches is text, and refuses two confirmed links that name one result. It checks
nothing else about a link, and no check here could: a decision changed by hand to another valid
state, or to another valid ID, reads exactly as a parent's own decision would. So after a hand
edit of the link table, a school score can appear on homework, or leave it, and nothing is
logged. A link whose result is not on record, or holds no current value, shows nothing, keeps
its assignment out of automatic matching, and can let other homework show that result's score.
No page can remove such a link: the file has to be repaired. A link row whose student ID was
changed is not returned by the read and stops applying, without a diagnostic. Nothing here
authenticates who made a decision, and nothing here detects every edit.

Nothing a plan is made from reads this module or holds what it returns.
"""

import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import tzinfo

from blossom.grades.dates import due_date_of
from blossom.grades.draft import Presence, folded
from blossom.grades.review import CurrentValue, GradeValueUndecodable
from blossom.grades.school_scores import Homework, Link, Result, result_of, school_scores
from blossom.grades.wording import presence_words, report_names, score_line
from blossom.noticing import read_date
from blossom.reconciliation import SourceRecord
from blossom.routes.navigation import CLASS_AT, address
from blossom.stores.gradebook import (
    LINK_CONFIRMED,
    ClassTermRead,
    GradebookRecords,
    HomeworkClasses,
    class_for,
)
from blossom.stores.project_state import Assignment

logger = logging.getLogger(__name__)


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
    """A result's school score as a page shows it: the line, the address of the result on
    class details, its title when the school reported one, and whether it stands by a
    parent's link or else by the rules."""

    line: str
    details: str
    title: str | None
    linked: bool


def read_grade_facts(store: GradebookRecords, courses: Iterable[str]) -> tuple[YearRead, ...]:
    """Every school year on record, with the results of each class one of the homework class
    names ``courses`` connects to, in each term of its year that holds a report. With no
    homework nothing is read. A read alone, made inside the caller's hold of the record."""
    names = set(courses)
    if not names:
        return ()
    # Text is decoded strictly for these reads. A report of a connected class in a term with
    # no row among its year's terms is refused: that term is never read, so its results
    # would drop out of the count unseen.
    with store.strict_text():
        by_year: dict[str, list[str]] = {}
        for year, term in store.grade_contexts().terms:
            by_year.setdefault(year, []).append(term)
        reported: dict[str, set[tuple[str, str]]] = {}
        for year, class_id, term in store.terms_reported():
            reported.setdefault(year, set()).add((class_id, term))
            by_year.setdefault(year, [])
        years = []
        for year, terms in by_year.items():
            mapping = store.homework_classes(year)
            connected = {class_for(name, mapping) for name in names} - {None}
            held = {folded(term) for term in terms}
            if any(
                class_id in connected and term not in held
                for class_id, term in reported.get(year, ())
            ):
                raise GradeValueUndecodable
            reads: dict[tuple[str, str], ClassTermRead] = {}
            first_month = store.first_month_of(year) if connected else None
            for term in terms if connected else ():
                for one in store.classes_in(year, term):
                    if one.reported and one.class_id in connected:
                        reads[(one.class_id, term)] = store.class_term(one.class_id, term)
            years.append(YearRead(mapping, first_month, tuple(terms), reads))
    return tuple(years)


def read_score_facts(
    store: GradebookRecords, courses: Iterable[str]
) -> tuple[tuple[YearRead, ...], tuple[Link, ...]]:
    """The grade facts ``read_grade_facts`` gives, and her homework links as the score rules
    read them: both, or an error and neither. Her links are asked for after the facts, only
    when a class and term was read, with text decoded strictly. A read alone."""
    years = read_grade_facts(store, courses)
    if not results_read(years):
        return years, ()
    with store.strict_text():
        stored = store.homework_links()
    return years, tuple(
        Link(one.assignment_id, one.result_id, confirmed=one.state == LINK_CONFIRMED)
        for one in stored
    )


def scores_left_out(error: GradeValueUndecodable) -> None:
    """Records that a homework page shows no school score, by the failure's type alone:
    nothing the gradebook holds is written."""
    logger.warning("a homework page shows no school score: %s", type(error).__name__)


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
    links: Collection[Link] = (),
    zone: tzinfo,
    base: str,
) -> dict[str, tuple[ScoreShown, ...]]:
    """Each assignment's school scores by assignment ID, for a page under the grade tree
    ``base``, in the order of school year, term place, then class details' own. Given every
    assignment, each one's date claims, those with one unreadable, and a parent's links."""
    listed = list(assignments)
    homework = [
        homework_of(item, claims.get(item.assignment_id, ()), item.assignment_id in unreadable)
        for item in listed
    ]
    classes: dict[str, list[str]] = {}
    ordered: list[tuple[tuple[str, int, str, bool, int, str, int], Result]] = []
    found: dict[tuple[str, str, str], tuple[CurrentValue, dict[str, str], int]] = {}
    for year in years:
        for item in listed:
            class_id = class_for(item.course, year.mapping)
            if class_id is not None:
                classes.setdefault(item.assignment_id, []).append(class_id)
        for (class_id, term), read in year.reads.items():
            names = report_names(read.reports, zone)
            place = year.terms.index(term) + 1
            for order, (result_id, value) in enumerate(read.current.results.items()):
                day = (
                    None
                    if value.due is None
                    else due_date_of(value.due.cell, year.mapping.year, year.first_month)
                )
                # Class details lists a term's dated results newest first, then the undated,
                # each by title as it words the cell, and keeps the record's order after that.
                title = value.cells["assignment"]
                worded = (
                    title[1] if title[0] is Presence.REPORTED else presence_words("Title", title)
                )
                within = (day is None, 0 if day is None else -day.toordinal())
                key = (
                    year.mapping.year,
                    place,
                    class_id,
                    *within,
                    folded(worded).casefold(),
                    order,
                )
                ordered.append((key, result_of(class_id, term, result_id, value, day)))
                found[(class_id, term, result_id)] = (value, names, place)
    results = [result for _, result in sorted(ordered, key=lambda one: one[0])]
    shown: dict[str, tuple[ScoreShown, ...]] = {}
    for assignment_id, several in school_scores(homework, results, classes, decided=links).items():
        scores = []
        for at in several:
            value, names, place = found[(at.class_id, at.term, at.result_id)]
            title = value.cells["assignment"]
            scores.append(
                ScoreShown(
                    line=score_line(value, names),
                    details=address(
                        CLASS_AT.format(base=base, class_id=at.class_id, n=place),
                        fragment=at.result_id,
                    ),
                    title=title[1] if title[0] is Presence.REPORTED else None,
                    linked=at.linked,
                )
            )
        shown[assignment_id] = tuple(scores)
    return shown
