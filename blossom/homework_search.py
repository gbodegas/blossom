"""Homework on record found by its class or title, for a note to be joined to by choice.

A note can be joined to homework named nothing like it: a classmate's word
for it, the school's spelling, a title the note never gave. The search reads
the course and the title of every assignment on record, Done and work outside
today's window included, and nothing else: never her words, and nothing goes
to a model. The query is trimmed, its whitespace collapsed, and case-folded for
the search alone, which changes nothing stored and nothing about how the
school's paste pairs work; it is split into terms, and every term must occur
in the course or in the title. Results come in one order, twenty to a page.
Finding homework joins nothing to it: the choice is made on the page, and the
save holds that choice to the row as it was shown.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from blossom.authored_text import TOO_LONG, TextRefused, single_line
from blossom.stores.project_state import Assignment

QUERY_MAX_LENGTH: Final = 200
"""Search words, in normalized code points; a query beyond it is refused, never cut."""
PAGE_SIZE: Final = 20
PAGE_NUMBER_MAX_LENGTH: Final = 6


def searchable(text: str) -> str:
    """Text as the search reads it: whitespace collapsed and case folded. Nothing stored is
    changed, and the paste's identity rule, which keeps case, is untouched."""
    return " ".join(text.split()).casefold()


def terms_of(query: str) -> tuple[str, ...]:
    """The terms a query holds, or none for a blank one. A query holding what one line
    cannot is refused as ``TextRefused``, as is one longer than the limit once its
    whitespace is collapsed: the limit counts the query as searched, not as typed. Nothing
    here touches the rule saved words, classes, and titles are held to."""
    checked = single_line(query, max(len(query), 1))
    collapsed = " ".join((checked or "").split())
    if len(collapsed) > QUERY_MAX_LENGTH:
        raise TextRefused(TOO_LONG, length=len(collapsed), limit=QUERY_MAX_LENGTH)
    return tuple(collapsed.casefold().split())


def matches(item: Assignment, terms: Sequence[str]) -> bool:
    """Whether every term occurs in the course or in the title, as the search reads them."""
    course, title = searchable(item.course), searchable(item.title)
    return all(term in course or term in title for term in terms)


def order_key(item: Assignment) -> tuple[str, str, bool, date, str]:
    """The one order results come in: course, title, due date with none last, then id."""
    return (
        searchable(item.course),
        searchable(item.title),
        item.due_date is None,
        item.due_date or date.min,
        item.assignment_id,
    )


def found(rows: Iterable[Assignment], terms: Sequence[str]) -> list[Assignment]:
    """Every assignment the terms find, in the one order; none for no terms, since a search
    with no words makes nothing up."""
    if not terms:
        return []
    return sorted((item for item in rows if matches(item, terms)), key=order_key)


@dataclass(frozen=True)
class ResultsPage:
    """One page of results: which page, how many there are, and the rows on it."""

    number: int
    last: int
    total: int
    items: tuple[Assignment, ...]

    @property
    def previous(self) -> int | None:
        """The page before this one, when there is one."""
        return self.number - 1 if self.number > 1 else None

    @property
    def next(self) -> int | None:
        """The page after this one, when there is one."""
        return self.number + 1 if self.number < self.last else None


def page_of(results: Sequence[Assignment], number: int) -> ResultsPage | None:
    """The page numbered ``number`` of these results, twenty to a page, or ``None`` for a
    number no page has. No results make one empty page."""
    last = max(1, -(-len(results) // PAGE_SIZE))
    if number < 1 or number > last:
        return None
    start = (number - 1) * PAGE_SIZE
    return ResultsPage(number, last, len(results), tuple(results[start : start + PAGE_SIZE]))


def page_number(value: str | None) -> int | None:
    """A page number as an address names one: the first when none is named; otherwise ASCII
    digits spelled as the count is, up to six of them. Anything else names no page."""
    if value is None or value == "":
        return 1
    if not (
        len(value) <= PAGE_NUMBER_MAX_LENGTH
        and value.isascii()
        and value.isdigit()
        and str(int(value)) == value
    ):
        return None
    return int(value)
