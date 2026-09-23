"""Homework on record found by its class or title: the rules a search is held to.

The search reads course and title and nothing else, of every assignment on
record, Done and work outside today's window included. The query is trimmed,
its whitespace collapsed, and case-folded for the search alone; it is split
into terms, and every term must occur in the course or in the title. Results
come in a fixed order, twenty to a page. Nothing here reads her words or sends
anything anywhere.
"""

from datetime import date

import pytest

from blossom.authored_text import TextRefused
from blossom.homework_search import (
    PAGE_SIZE,
    QUERY_MAX_LENGTH,
    found,
    page_number,
    page_of,
    terms_of,
)
from blossom.reconciliation import SourceChannel
from blossom.stores.project_state import Assignment


def row(
    assignment_id: str,
    course: str,
    title: str,
    due: date | None = None,
    status: str = "not_started",
) -> Assignment:
    return Assignment(
        assignment_id=assignment_id,
        course=course,
        title=title,
        due_date=due,
        dependencies=[],
        reported_submission_status=status,
        origins={"record": SourceChannel.LMS},
    )


ON_RECORD = [
    row("assignment-b", "Geometry", "Questions 4-8", date(2026, 9, 25)),
    row("assignment-a", "Geometry", "Questions 4-8", date(2026, 9, 18)),
    row("assignment-c", "Geometry", "Questions 4-8"),
    row("assignment-d", "geometry", "Chapter review", date(2026, 9, 11), status="submitted"),
    row("assignment-e", "Humanities", "Summer   reading log", date(2026, 6, 1)),
    row("assignment-f", "Spanish", "Vocabulary list, unit two", date(2026, 9, 11)),
]


def test_the_query_is_read_for_the_search_alone() -> None:
    """Trimmed, its whitespace collapsed, case-folded, and split into terms; nothing is
    stored, and an empty query has no terms."""
    assert terms_of("  Geometry   QUESTIONS ") == ("geometry", "questions")
    assert terms_of("") == ()
    assert terms_of("   ") == ()
    assert terms_of("Straße") == ("strasse",)


def test_a_query_the_rules_refuse_is_refused_before_anything_is_searched() -> None:
    assert len(terms_of("q" * QUERY_MAX_LENGTH)) == 1
    with pytest.raises(TextRefused):
        terms_of("q" * (QUERY_MAX_LENGTH + 1))
    with pytest.raises(TextRefused):
        terms_of("two" + chr(10) + "lines")


def test_every_term_must_occur_in_the_course_or_the_title() -> None:
    """A term matches as a substring of either, whatever the case and spacing; Done work and
    work with no date or long past are found like any other; no term finds nothing."""
    assert [item.assignment_id for item in found(ON_RECORD, ("geometry",))] == [
        "assignment-d",
        "assignment-a",
        "assignment-b",
        "assignment-c",
    ]
    assert [item.assignment_id for item in found(ON_RECORD, ("review",))] == ["assignment-d"]
    assert [item.assignment_id for item in found(ON_RECORD, ("summer", "log"))] == ["assignment-e"]
    assert [item.assignment_id for item in found(ON_RECORD, ("geometry", "review"))] == [
        "assignment-d"
    ]
    assert found(ON_RECORD, ("geometry", "spanish")) == []
    assert found(ON_RECORD, ("4-8",)) == found(ON_RECORD, ("4-8", "geo"))
    assert found(ON_RECORD, ()) == []


def test_results_come_in_one_order() -> None:
    """By the course as searched, then the title as searched, then the due date with none
    last, then the assignment's id."""
    assert [item.assignment_id for item in found(ON_RECORD, ("e",))] == [
        "assignment-d",
        "assignment-a",
        "assignment-b",
        "assignment-c",
        "assignment-e",
    ]
    assert [item.assignment_id for item in found(ON_RECORD, ("i",))][-1] == "assignment-f"


def test_twenty_results_make_a_page_and_the_pages_are_counted() -> None:
    many = [row(f"assignment-{n:03d}", "Math", f"Practice {n:03d}") for n in range(45)]
    first = page_of(many, 1)
    third = page_of(many, 3)
    assert first is not None
    assert third is not None
    assert (first.number, first.last, first.total, len(first.items)) == (1, 3, 45, PAGE_SIZE)
    assert (first.previous, first.next) == (None, 2)
    assert (third.previous, third.next) == (2, None)
    assert [item.assignment_id for item in third.items] == [
        f"assignment-{n:03d}" for n in range(40, 45)
    ]
    assert page_of(many, 4) is None
    assert page_of(many, 0) is None
    none = page_of([], 1)
    assert none is not None
    assert (none.number, none.last, none.total, none.items) == (1, 1, 0, ())
    assert page_of([], 2) is None


@pytest.mark.parametrize(
    ("given", "read"),
    [
        (None, 1),
        ("", 1),
        ("1", 1),
        ("12", 12),
        ("0", 0),
        ("01", None),
        ("x", None),
        ("²", None),
        ("1234567", None),
    ],
)
def test_a_page_number_is_read_as_a_count_or_not_at_all(
    given: str | None, read: int | None
) -> None:
    assert page_number(given) == read


@pytest.mark.parametrize("query", ["a" + " " * 201 + "b", "a" + chr(160) * 201 + "b"])
def test_the_limit_follows_the_whitespace_collapse(query: str) -> None:
    """The limit counts the query as searched, its whitespace collapsed, not as typed."""
    assert terms_of(query) == ("a", "b")


def test_a_query_too_long_once_collapsed_is_still_refused() -> None:
    assert len(terms_of("q" * 100 + "   " + "q" * 99)) == 2
    with pytest.raises(TextRefused):
        terms_of("q" * 100 + "   " + "q" * 100)
    with pytest.raises(TextRefused):
        terms_of("q" * (QUERY_MAX_LENGTH + 1))
