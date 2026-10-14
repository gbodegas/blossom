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

A parent's stored link decisions are trusted to be the parent's. Returned link values are checked
as text and as UTF-8, and two confirmed links naming one result are refused. These checks do not
verify that referenced records exist or authenticate who made a decision. Additional checks could
detect missing references and some inconsistent relationships. A valid-looking state or ID change
can be indistinguishable from a parent's decision without an independent trusted record, which
Blossom does not keep. Such edits can make school scores appear or disappear without a diagnostic.

A confirmed link to a missing result, or one with no current value, contributes no score and keeps
its assignment out of automatic matching. If a hand edit replaced the original result ID, the
original result can return to automatic matching and appear on other homework. No page repairs that
dangling decision. A link row whose student ID does not match the read is excluded and stops
applying without a diagnostic. Nothing here detects every edit.

One result's homework is worked out from the same values: ``rule_values`` makes what the
rules take from one page read, the mapping of school scores is made from that, and
``result_homework`` gives one result's candidates, its link or the pair the rules stand, the
pairs kept apart, and why a linked or matched result's score stays off her homework.
``homework_found`` filters the same values by a parent's search words: the homework whose
class name connects to the result's class, a page of twenty at a time, each row with its
standing against the result. It reads nothing more and writes nothing.

Nothing a plan is made from reads this module or holds what it returns.
"""

import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, tzinfo
from enum import StrEnum
from typing import Final, NamedTuple

from blossom.authored_text import TextRefused
from blossom.grades.dates import due_date_of
from blossom.grades.draft import Presence, folded
from blossom.grades.review import CurrentValue, GradeValueUndecodable, HomeworkLinksConflict
from blossom.grades.school_scores import (
    Candidates,
    Homework,
    Link,
    Result,
    Waiting,
    candidate_homework,
    result_of,
    school_scores,
    waiting_results,
)
from blossom.grades.wording import presence_words, report_names, score_line
from blossom.homework_search import ResultsPage, found, page_number, page_of, terms_of
from blossom.noticing import read_date
from blossom.reconciliation import SourceRecord
from blossom.routes.navigation import CLASS_AT, address
from blossom.stores.gradebook import (
    LINK_CONFIRMED,
    ClassOfYear,
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
    class details, its title as class details words it, and whether it stands by a parent's
    link or else by the rules."""

    line: str
    details: str
    title: str
    linked: bool


class RuleValues(NamedTuple):
    """One page read as the score rules take it: each assignment's record and the rules' value
    made from it, every result read in the page's order, each assignment's connected classes,
    and each result's value with its term's place in its year, by class, term and result ID."""

    assignments: list[Assignment]
    homework: list[Homework]
    results: list[Result]
    classes: dict[str, list[str]]
    found: dict[tuple[str, str, str], tuple[CurrentValue, int]]


class PairStanding(StrEnum):
    """A parent's standing decision about one result and one homework, or none."""

    NONE = "none"
    LINKED = "linked"
    APART = "apart"


class Withheld(StrEnum):
    """Why a linked or matched result's score stays off her homework: its points cell by
    presence, a class and term that doesn't show it, or, for a link, the homework's class name
    not connected to its class, a class of the current school year or of another."""

    POINTS_BLANK = "points_blank"
    POINTS_UNREADABLE = "points_unreadable"
    POINTS_NOT_IN_COPY = "points_not_in_copy"
    NOT_SHOWN = "not_shown"
    NAME_NOT_CONNECTED = "name_not_connected"
    NAME_NOT_CONNECTED_IN_ANOTHER_YEAR = "name_not_connected_in_another_year"


POINTS_WITHHELD: Final = {
    Presence.BLANK: Withheld.POINTS_BLANK,
    Presence.UNREADABLE: Withheld.POINTS_UNREADABLE,
    Presence.NOT_CAPTURED: Withheld.POINTS_NOT_IN_COPY,
}
"""The reason for each presence of a points cell the school did not report."""


@dataclass(frozen=True)
class HomeworkRow:
    """One homework beside a result: its class name, title and due date as on record, none of
    the three when it is not; the pair's standing decision; whether linking it would move the
    result's link from other homework; and whether another result is linked to it."""

    assignment_id: str
    on_record: bool
    course: str | None
    title: str | None
    due: date | None
    standing: PairStanding
    moves_link: bool
    another_result: bool


@dataclass(frozen=True)
class ResultHomework:
    """One result's homework: the homework a link names, or else the one the rules stand it
    with; its candidates whatever links stand; the pairs kept apart; each reason a linked or
    matched result's score stays off her homework; and the rules' answer when it waits."""

    class_id: str
    term: str
    result_id: str
    class_name: str
    year: str
    linked: HomeworkRow | None
    matched: HomeworkRow | None
    candidates: tuple[HomeworkRow, ...]
    apart: tuple[HomeworkRow, ...]
    withheld: tuple[Withheld, ...]
    waiting: Waiting | None


class FoundState(StrEnum):
    """A found homework's standing against the result it is searched for, the first that holds:
    linked to it, kept apart from it, linked to another result, or none of these."""

    LINKED = "linked"
    APART = "apart"
    ANOTHER_RESULT = "another_result"
    NONE = "none"


class NoList(StrEnum):
    """Why a search lists no homework: it holds no words, its words are refused, or its page
    field names no page of what they find."""

    NO_WORDS = "no_words"
    WORDS_REFUSED = "words_refused"
    NO_SUCH_PAGE = "no_such_page"


@dataclass(frozen=True)
class FoundRow:
    """A homework a search found, as on record, with its standing against the result."""

    item: Assignment
    state: FoundState


@dataclass(frozen=True)
class HomeworkFound:
    """One page of the homework a search found for a result: the page as ``page_of`` gives it,
    and each of its rows with its standing, in the page's order."""

    page: ResultsPage
    rows: tuple[FoundRow, ...]


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
    return years, read_links(store)


def read_links(store: GradebookRecords) -> tuple[Link, ...]:
    """Her homework links as the score rules read them, with text decoded strictly: the
    store's link read and its checks. A read alone."""
    with store.strict_text():
        stored = store.homework_links()
    return tuple(
        Link(one.assignment_id, one.result_id, confirmed=one.state == LINK_CONFIRMED)
        for one in stored
    )


def title_of(value: CurrentValue) -> str:
    """A result's title as class details words it: the title as written, or the words for a
    title left blank, unreadable or not in the copy."""
    cell = value.cells["assignment"]
    return cell[1] if cell[0] is Presence.REPORTED else presence_words("Title", cell)


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


def rule_values(
    assignments: Iterable[Assignment],
    claims: Mapping[str, Sequence[SourceRecord]],
    unreadable: Collection[str],
    years: Iterable[YearRead],
) -> RuleValues:
    """What the score rules take from one page read, the results in the order of school year,
    term place, then class details' own. Given every assignment, each one's date claims, and
    those with one unreadable."""
    listed = list(assignments)
    homework = [
        homework_of(item, claims.get(item.assignment_id, ()), item.assignment_id in unreadable)
        for item in listed
    ]
    classes: dict[str, list[str]] = {}
    ordered: list[tuple[tuple[str, int, str, bool, int, str, int], Result]] = []
    found: dict[tuple[str, str, str], tuple[CurrentValue, int]] = {}
    for year in years:
        for item in listed:
            class_id = class_for(item.course, year.mapping)
            if class_id is not None:
                classes.setdefault(item.assignment_id, []).append(class_id)
        for (class_id, term), read in year.reads.items():
            place = year.terms.index(term) + 1
            for order, (result_id, value) in enumerate(read.current.results.items()):
                day = (
                    None
                    if value.due is None
                    else due_date_of(value.due.cell, year.mapping.year, year.first_month)
                )
                # Class details lists a term's dated results newest first, then the undated,
                # each by title as it words the cell, and keeps the record's order after that.
                within = (day is None, 0 if day is None else -day.toordinal())
                key = (
                    year.mapping.year,
                    place,
                    class_id,
                    *within,
                    folded(title_of(value)).casefold(),
                    order,
                )
                ordered.append((key, result_of(class_id, term, result_id, value, day)))
                found[(class_id, term, result_id)] = (value, place)
    results = [result for _, result in sorted(ordered, key=lambda one: one[0])]
    return RuleValues(listed, homework, results, classes, found)


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
    facts = tuple(years)
    names = {
        at: report_names(read.reports, zone) for year in facts for at, read in year.reads.items()
    }
    rules = rule_values(assignments, claims, unreadable, facts)
    ruled = school_scores(rules.homework, rules.results, rules.classes, decided=links)
    shown: dict[str, tuple[ScoreShown, ...]] = {}
    for assignment_id, several in ruled.items():
        scores = []
        for at in several:
            value, place = rules.found[(at.class_id, at.term, at.result_id)]
            scores.append(
                ScoreShown(
                    line=score_line(value, names[(at.class_id, at.term)]),
                    details=address(
                        CLASS_AT.format(base=base, class_id=at.class_id, n=place),
                        fragment=at.result_id,
                    ),
                    title=title_of(value),
                    linked=at.linked,
                )
            )
        shown[assignment_id] = tuple(scores)
    return shown


def result_homework(
    of: ClassOfYear,
    term: str,
    result_id: str,
    value: CurrentValue,
    read: RuleValues,
    *,
    links: Collection[Link],
    current_year: str | None,
) -> ResultHomework:
    """The homework of the result ``result_id`` of the class ``of`` in ``term``, whose current
    value is ``value``: from what ``rule_values`` made of one page read, its records included,
    a parent's links and the current school year. Terms compare with their spaces folded."""
    listed = {item.assignment_id: item for item in read.assignments}
    could = candidate_homework(read.homework, read.results, read.classes, decided=links)
    waits = waiting_results(read.homework, read.results, read.classes, decided=links)

    def this(one: Candidates | Waiting) -> bool:
        there = (one.class_id, one.result_id, folded(one.term))
        return there == (of.class_id, result_id, folded(term))

    standing, shared = standings(result_id, links)
    holder = next((one for one, how in standing.items() if how is PairStanding.LINKED), None)

    def row(assignment_id: str) -> HomeworkRow:
        item = listed.get(assignment_id)
        return HomeworkRow(
            assignment_id=assignment_id,
            on_record=item is not None,
            course=None if item is None else item.course,
            title=None if item is None else item.title,
            due=None if item is None else item.due_date,
            standing=standing.get(assignment_id, PairStanding.NONE),
            moves_link=item is not None and holder not in (None, assignment_id),
            another_result=assignment_id in shared,
        )

    found = next((one for one in could if this(one)), None)
    linked_here = holder is not None and holder in listed
    withheld: list[Withheld] = []
    # A pair the rules stand connects by its class already, so only a link can lack it.
    if linked_here or (found is not None and found.stood is not None):
        points = value.cells["points"][0]
        if points is not Presence.REPORTED:
            withheld.append(POINTS_WITHHELD[points])
        if value.not_shown is not None:
            withheld.append(Withheld.NOT_SHOWN)
    if holder is not None and linked_here and of.class_id not in read.classes.get(holder, ()):
        withheld.append(
            Withheld.NAME_NOT_CONNECTED
            if of.year == current_year
            else Withheld.NAME_NOT_CONNECTED_IN_ANOTHER_YEAR
        )
    kept = {one for one, how in standing.items() if how is PairStanding.APART}
    # Homework on record first, in the order the assignments came, then the rest by ID.
    apart = [one for one in listed if one in kept] + sorted(kept - set(listed))
    return ResultHomework(
        class_id=of.class_id,
        term=term,
        result_id=result_id,
        class_name=of.name,
        year=of.year,
        linked=None if holder is None else row(holder),
        matched=None if found is None or found.stood is None else row(found.stood),
        candidates=() if found is None else tuple(row(one) for one in found.assignments),
        apart=tuple(row(one) for one in apart),
        withheld=tuple(withheld),
        waiting=next((one for one in waits if this(one)), None),
    )


def standings(result_id: str, links: Collection[Link]) -> tuple[dict[str, PairStanding], set[str]]:
    """Each homework's standing decision with the result ``result_id``, by assignment ID, and
    the homework a confirmed link names with another result. Two confirmed links that name one
    result are refused whole with ``HomeworkLinksConflict``."""
    held: set[str] = set()
    for link in links:
        if link.confirmed:
            if link.result_id in held:
                raise HomeworkLinksConflict
            held.add(link.result_id)
    standing = {
        link.assignment_id: PairStanding.LINKED if link.confirmed else PairStanding.APART
        for link in links
        if link.result_id == result_id
    }
    shared = {
        link.assignment_id for link in links if link.confirmed and link.result_id != result_id
    }
    return standing, shared


def homework_found(
    words: str,
    page: str,
    class_id: str,
    result_id: str,
    read: RuleValues,
    *,
    links: Collection[Link],
) -> HomeworkFound | NoList:
    """The page the field ``page`` names of what ``words`` find for the result ``result_id`` of
    ``class_id``, from what ``rule_values`` made of one page read: assignments whose class name
    connects to that class, a class of one school year. Words are read before the page field."""
    standing, shared = standings(result_id, links)
    try:
        terms = terms_of(words)
    except TextRefused:
        return NoList.WORDS_REFUSED
    if not terms:
        return NoList.NO_WORDS
    number = page_number(page)
    if number is None:
        return NoList.NO_SUCH_PAGE
    eligible = (
        item for item in read.assignments if class_id in read.classes.get(item.assignment_id, ())
    )
    shown = page_of(found(eligible, terms), number)
    if shown is None:
        return NoList.NO_SUCH_PAGE

    # A found row has at most one line of state: the first of these that holds.
    def state(assignment_id: str) -> FoundState:
        held = standing.get(assignment_id)
        if held is PairStanding.LINKED:
            return FoundState.LINKED
        if held is PairStanding.APART:
            return FoundState.APART
        return FoundState.ANOTHER_RESULT if assignment_id in shared else FoundState.NONE

    return HomeworkFound(
        shown, tuple(FoundRow(item, state(item.assignment_id)) for item in shown.items)
    )
