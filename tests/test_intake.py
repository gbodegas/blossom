"""Reading the school's text: the weekly summary, the homework page, and the email.

The text here is synthetic, in the portal's shapes: a fictional first name in
the heading, ordinary course names, and made-up titles with the portal's
punctuation. Nothing from a real family appears in it.
"""

import pathlib
from datetime import UTC, date, datetime

from blossom.intake import (
    CLAIMED,
    DAY_HEADER,
    KNOWN,
    NEW,
    OWN_LINE,
    SCHOOL_EMAIL,
    by_hand,
    changes_for,
    keep,
    kind_of,
    nearest_year,
    read_text,
    slug,
)
from blossom.reconciliation import SourceChannel
from blossom.stores.project_state import AssignmentKind, ProjectStateStore
from tests.support import fixture_clock

NOW = datetime(2026, 9, 12, 20, 0, tzinfo=UTC)
TODAY = date(2026, 9, 12)

SUMMARY = """Homework for Wren

* 09/01/2026 - Tuesday
Humanities - Due: Reading check:
Religion - Assigned: Syllabus Due: (Due:09/09/2026)
Return the syllabus with a parent's signature after reading it.
* 09/02/2026 - Wednesday
08 Geometry - Assigned: Syllabus- Signed : (Due:09/08/2026)
* 09/03/2026 - Thursday
08 Geometry - Assigned: Book Covers: (Due:09/08/2026)
Cover both books with paper.
* 09/04/2026 - Friday
08 Geometry - Assigned: PR1 U2.3 Pg 40 #1-9: (Due:09/08/2026)
This should be done IN your math notebook.
Humanities - Due: Summer Reading - Log:
Spanish - Assigned: Binder, labeled dividers and lined paper check: (Due:09/11/2026)


Homework for Wren

* 09/08/2026 - Tuesday
08 Geometry - Assigned: PR2 U2 pg. 41 #10, 12, 15 : (Due:09/09/2026)
08 Geometry - Due: Book Covers:
Cover both books with paper.
08 Geometry - Due: PR1 U2.3 Pg 40 #1-9:
This should be done IN your math notebook.
08 Geometry - Due: Syllabus- Signed :
Humanities - Due: First draft, What I See? poem:
* 09/09/2026 - Wednesday
08 Geometry - Due: PR2 U2 pg. 41 #10, 12, 15 :
Religion - Due: Syllabus Due:
Return the syllabus with a parent's signature after reading it.
* 09/11/2026 - Friday
Spanish - Due: Binder, labeled dividers and lined paper check:
"""

PAGE = """Week of 9/1/2026
Previous
Next
Tuesday 9/1/2026
08 Geometry
Assigned: Book Covers: (Due:09/08/2026)
Cover both books with paper.
Humanities
Due: Reading check:
Tuesday 9/8/2026
08 Geometry
Due: Book Covers:
"""

EMAIL = """Assignments:
09/09 08 Geometry - A: Homework/Classwork: Book Covers Grade: Missing
01/15 Humanities - B: Classwork: Reading check Grade: Missing
"""


def test_the_weekly_summary_is_read_card_by_card_and_merged_across_weeks() -> None:
    read = read_text(SUMMARY, now=NOW, today=TODAY)
    by_id = {item.assignment_id: item for item in read.items}

    assert read.unread == ()
    assert len(by_id) == 9
    covers = by_id["assignment-08-geometry-book-covers"]
    assert covers.course == "08 Geometry"
    assert covers.due_date == date(2026, 9, 8)
    assert covers.assigned_on == date(2026, 9, 3)
    assert covers.kind is AssignmentKind.TASK
    assert [(said.channel, said.seen_in, said.asserted_value) for said in covers.claims] == [
        (SourceChannel.LMS, OWN_LINE, "2026-09-08"),
        (SourceChannel.LMS, DAY_HEADER, "2026-09-08"),
    ]
    assert covers.note == "Cover both books with paper."
    assert all(said.observed_at == NOW and said.confidence == 0.9 for said in covers.claims)
    assert by_id["assignment-08-geometry-syllabus-signed"].title == "Syllabus- Signed"
    spaced = by_id["assignment-08-geometry-pr2-u2-pg-41-10-12-15"]
    assert spaced.title == "PR2 U2 pg. 41 #10, 12, 15"
    log = by_id["assignment-humanities-summer-reading-log"]
    assert (log.course, log.title) == ("Humanities", "Summer Reading - Log")
    assert log.due_date == date(2026, 9, 4)
    poem = by_id["assignment-humanities-first-draft-what-i-see-poem"]
    assert poem.claims[0].seen_in == DAY_HEADER
    assert poem.assigned_on is None
    assert by_id["assignment-religion-syllabus-due"].note == (
        "Return the syllabus with a parent's signature after reading it."
    )


def test_the_homework_page_is_read_with_the_course_on_its_own_line() -> None:
    read = read_text(PAGE, now=NOW, today=TODAY)
    by_id = {item.assignment_id: item for item in read.items}

    assert read.unread == ()
    assert set(by_id) == {
        "assignment-08-geometry-book-covers",
        "assignment-humanities-reading-check",
    }
    covers = by_id["assignment-08-geometry-book-covers"]
    assert covers.assigned_on == date(2026, 9, 1)
    assert covers.note == "Cover both books with paper."
    assert [said.seen_in for said in covers.claims] == [OWN_LINE, DAY_HEADER]
    check = by_id["assignment-humanities-reading-check"]
    assert check.course == "Humanities"
    assert check.due_date == date(2026, 9, 1)
    assert check.note is None


def test_the_missing_email_is_read_with_the_nearest_year() -> None:
    read = read_text(EMAIL, now=NOW, today=TODAY)
    by_id = {item.assignment_id: item for item in read.items}

    assert read.unread == ()
    covers = by_id["assignment-08-geometry-book-covers"]
    assert covers.course == "08 Geometry"
    assert covers.due_date == date(2026, 9, 9)
    assert [(said.channel, said.seen_in) for said in covers.claims] == [
        (SourceChannel.EMAIL, SCHOOL_EMAIL)
    ]
    assert by_id["assignment-humanities-reading-check"].due_date == date(2027, 1, 15)
    assert nearest_year(2, 29, date(2027, 6, 1)) == date(2028, 2, 29)
    assert nearest_year(13, 1, TODAY) is None


def test_lines_not_understood_are_kept_as_unread_and_nothing_is_invented() -> None:
    text = """Tuesday 9/1/2026
Assigned: A card with no course before it: (Due:09/08/2026)
A stray line nobody expected
Wednesday 9/31/2026
08 Geometry
Due: A card under a day that is no date:
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert read.items == ()
    assert read.unread == (
        "Assigned: A card with no course before it: (Due:09/08/2026)",
        "A stray line nobody expected",
        "Wednesday 9/31/2026",
        "Due: A card under a day that is no date:",
    )


def test_the_kind_and_the_id_follow_the_title() -> None:
    assert kind_of("Syllabus- Signed") is AssignmentKind.TASK
    assert kind_of("Book Covers") is AssignmentKind.TASK
    assert kind_of("Binder, labeled dividers and lined paper check") is AssignmentKind.TASK
    assert kind_of("PR1 U2.3 Pg 40 #1-9") is AssignmentKind.HOMEWORK
    assert kind_of("First draft, What I See? poem") is AssignmentKind.HOMEWORK
    assert slug("First draft, What I See? poem") == "first-draft-what-i-see-poem"
    assert slug(" 08  Geometry ") == "08-geometry"


def test_by_hand_makes_the_familys_claim_only_when_a_date_is_given() -> None:
    dated = by_hand(
        "Spanish", " Vocabulary list ", date(2026, 9, 20), None, AssignmentKind.HOMEWORK, now=NOW
    )
    undated = by_hand("Spanish", "Vocabulary list", None, None, AssignmentKind.TASK, now=NOW)

    assert dated.title == "Vocabulary list"
    assert dated.assignment_id == "assignment-spanish-vocabulary-list"
    assert [(said.channel, said.seen_in, said.confidence) for said in dated.claims] == [
        (SourceChannel.PARENT_ENTRY, None, 0.8)
    ]
    assert undated.claims == ()
    assert undated.assignment().reported_submission_status == "unknown"


def test_changes_are_new_known_or_claimed_and_kept_in_one_write(tmp_path: pathlib.Path) -> None:
    """A paste is new the first time, known the second, and a claim beside the record when
    a later week names another date; the recorded date is never replaced."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        read = read_text(SUMMARY, now=NOW, today=TODAY)
        first = changes_for(read.items, store)
        kept_first = keep(first, store)
        again = changes_for(read.items, store)
        kept_again = keep(again, store)
        later = read_text(
            "Homework for Wren\n* 09/10/2026 - Thursday\n08 Geometry - Due: Book Covers:\n",
            now=NOW,
            today=TODAY,
        )
        moved = changes_for(later.items, store)
        kept_moved = keep(moved, store)
        covers = {item.assignment_id: item for item in store.all_assignments()}[
            "assignment-08-geometry-book-covers"
        ]
        claims = store.deadline_records(covers.assignment_id)
        typed = by_hand(
            "Art",
            "Sketchbook, three pages",
            date(2026, 9, 18),
            None,
            AssignmentKind.HOMEWORK,
            now=NOW,
        )
        keep(changes_for((typed,), store), store)
        page = read_text(
            "Tuesday 9/8/2026\nArt\nAssigned: Sketchbook, three pages: (Due:09/18/2026)\n",
            now=NOW,
            today=TODAY,
        )
        filled = changes_for(page.items, store)
        keep(filled, store)
        sketch = {item.assignment_id: item for item in store.all_assignments()}[
            "assignment-art-sketchbook-three-pages"
        ]
    finally:
        store.close()

    assert [change.state for change in first] == [NEW] * 9
    assert kept_first == 9
    assert [change.state for change in again] == [KNOWN] * 9
    assert kept_again == 0
    assert [change.state for change in moved] == [CLAIMED]
    assert moved[0].label == "On record; a new date claim"
    assert kept_moved == 1
    assert covers.due_date == date(2026, 9, 8)
    assert [said.asserted_value for said in claims] == ["2026-09-08", "2026-09-08", "2026-09-10"]
    assert filled[0].label == "On record; a new date claim and the assigned date"
    assert sketch.assigned_on == date(2026, 9, 8)
    assert sketch.due_date == date(2026, 9, 18)
