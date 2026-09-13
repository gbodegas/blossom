"""Reading the school's text: the weekly summary, the homework page, and the email.

The text here is synthetic, in the portal's shapes: a fictional first name in
the heading, ordinary course names, and made-up titles with the portal's
punctuation. Nothing from a real family appears in it.
"""

import pathlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime

from blossom.intake import (
    CLAIMED,
    DAY_HEADER,
    KNOWN,
    NEW,
    OWN_LINE,
    SCHOOL_EMAIL,
    Reading,
    by_hand,
    changes_for,
    identity,
    keep,
    kind_of,
    nearest_year,
    read_text,
    slug,
    within_a_school_year,
)
from blossom.reconciliation import SourceChannel
from blossom.sources import FixtureSource, read_whole
from blossom.stores.project_state import AssignmentKind, ProjectStateStore
from tests.support import FIXTURES, fixture_clock

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
09/09 Spanish - A: Homework: Vocabulary list Grade: Complete
"""


def by_pair(read_items: tuple[Reading, ...]) -> dict[tuple[str, str], Reading]:
    return {item.pair: item for item in read_items}


def test_the_weekly_summary_is_read_card_by_card_and_merged_across_weeks() -> None:
    read = read_text(SUMMARY, now=NOW, today=TODAY)
    items = by_pair(read.items)

    assert read.unread == ()
    assert len(items) == 9
    covers = items["08 Geometry", "Book Covers"]
    assert covers.due_date == date(2026, 9, 8)
    assert covers.assigned_on == date(2026, 9, 3)
    assert covers.kind is AssignmentKind.TASK
    assert [(said.channel, said.seen_in, said.asserted_value) for said in covers.claims] == [
        (SourceChannel.LMS, OWN_LINE, "2026-09-08"),
        (SourceChannel.LMS, DAY_HEADER, "2026-09-08"),
    ]
    assert covers.note == "Cover both books with paper."
    assert all(said.observed_at == NOW and said.confidence == 0.9 for said in covers.claims)
    assert ("08 Geometry", "Syllabus- Signed") in items
    assert ("08 Geometry", "PR2 U2 pg. 41 #10, 12, 15") in items
    log = items["Humanities", "Summer Reading - Log"]
    assert log.due_date == date(2026, 9, 4)
    poem = items["Humanities", "First draft, What I See? poem"]
    assert poem.claims[0].seen_in == DAY_HEADER
    assert poem.assigned_on is None
    assert items["Religion", "Syllabus Due"].note == (
        "Return the syllabus with a parent's signature after reading it."
    )


def test_the_homework_page_is_read_with_the_course_on_its_own_line() -> None:
    read = read_text(PAGE, now=NOW, today=TODAY)
    items = by_pair(read.items)

    assert read.unread == ()
    assert set(items) == {("08 Geometry", "Book Covers"), ("Humanities", "Reading check")}
    covers = items["08 Geometry", "Book Covers"]
    assert covers.assigned_on == date(2026, 9, 1)
    assert covers.note == "Cover both books with paper."
    assert [said.seen_in for said in covers.claims] == [OWN_LINE, DAY_HEADER]
    check = items["Humanities", "Reading check"]
    assert check.due_date == date(2026, 9, 1)
    assert check.note is None


def test_the_missing_email_is_read_with_the_nearest_year_and_no_other_grade() -> None:
    read = read_text(EMAIL, now=NOW, today=TODAY)
    items = by_pair(read.items)

    assert read.unread == ("09/09 Spanish - A: Homework: Vocabulary list Grade: Complete",)
    covers = items["08 Geometry", "Book Covers"]
    assert covers.due_date == date(2026, 9, 9)
    assert [(said.channel, said.seen_in) for said in covers.claims] == [
        (SourceChannel.EMAIL, SCHOOL_EMAIL)
    ]
    assert items["Humanities", "Reading check"].due_date == date(2027, 1, 15)
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


def test_the_kind_follows_whole_words_in_the_title() -> None:
    assert kind_of("Syllabus- Signed") is AssignmentKind.TASK
    assert kind_of("Book Covers") is AssignmentKind.TASK
    assert kind_of("Binder, labeled dividers and lined paper check") is AssignmentKind.TASK
    assert kind_of("PR1 U2.3 Pg 40 #1-9") is AssignmentKind.HOMEWORK
    assert kind_of("First draft, What I See? poem") is AssignmentKind.HOMEWORK
    assert kind_of("Discovering cells") is AssignmentKind.HOMEWORK
    assert kind_of("Design a book cover") is AssignmentKind.TASK


def test_an_id_is_readable_and_keeps_titles_that_read_alike_apart() -> None:
    """The same pair always gives the same id; a different pair never gives the same one."""
    slashed = identity("08 Geometry", "Quiz A/B")
    dashed = identity("08 Geometry", "Quiz A-B")
    long_title = identity("Humanities", "A title " + "long " * 30 + "past the readable part")

    assert slug("First draft, What I See? poem") == "first-draft-what-i-see-poem"
    assert slashed.startswith("assignment-quiz-a-b-")
    assert dashed.startswith("assignment-quiz-a-b-")
    assert slashed != dashed
    assert identity(" 08  Geometry ", "Quiz  A/B") == slashed
    assert len(long_title) <= len("assignment-") + 40 + 1 + 8
    read = read_text(
        "Tuesday 9/1/2026\n08 Geometry\nDue: Quiz A/B:\nDue: Quiz A-B:\n", now=NOW, today=TODAY
    )
    assert len(read.items) == 2


def test_by_hand_makes_the_familys_claim_only_when_a_date_is_given() -> None:
    dated = by_hand(
        "Spanish", " Vocabulary  list ", date(2026, 9, 20), None, AssignmentKind.HOMEWORK, now=NOW
    )
    undated = by_hand("Spanish", "Vocabulary list", None, None, AssignmentKind.TASK, now=NOW)

    assert dated.title == "Vocabulary list"
    assert dated.assignment_id == undated.assignment_id
    assert [(said.channel, said.seen_in, said.confidence) for said in dated.claims] == [
        (SourceChannel.PARENT_ENTRY, None, 0.8)
    ]
    assert undated.claims == ()
    assert undated.assignment().reported_submission_status == "unknown"


def test_a_year_is_measured_by_the_calendar() -> None:
    today = date(2027, 9, 12)

    assert within_a_school_year(date(2028, 9, 12), today)
    assert not within_a_school_year(date(2028, 9, 13), today)
    assert within_a_school_year(date(2026, 9, 12), today)
    assert not within_a_school_year(date(2026, 9, 11), today)
    assert within_a_school_year(date(2029, 2, 28), date(2028, 2, 29))
    assert not within_a_school_year(date(2029, 3, 1), date(2028, 2, 29))


def test_changes_are_new_known_or_claimed_and_kept_as_one(tmp_path: pathlib.Path) -> None:
    """A paste is new the first time, known the second, and a claim beside the record when
    a later week names another date; the recorded date is never replaced."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        read = read_text(SUMMARY, now=NOW, today=TODAY)
        first = changes_for(read.items, store)
        kept_first = keep(read.items, store)
        again = changes_for(read.items, store)
        kept_again = keep(read.items, store)
        later = read_text(
            "Homework for Wren\n* 09/10/2026 - Thursday\n08 Geometry - Due: Book Covers:\n",
            now=NOW,
            today=TODAY,
        )
        moved = changes_for(later.items, store)
        kept_moved = keep(later.items, store)
        covers = {(item.course, item.title): item for item in store.all_assignments()}[
            "08 Geometry", "Book Covers"
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
        keep((typed,), store)
        page = read_text(
            "Tuesday 9/8/2026\nArt\nAssigned: Sketchbook, three pages: (Due:09/18/2026)\n",
            now=NOW,
            today=TODAY,
        )
        filled = changes_for(page.items, store)
        keep(page.items, store)
        sketch = {(item.course, item.title): item for item in store.all_assignments()}[
            "Art", "Sketchbook, three pages"
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


def test_a_paste_matches_a_row_on_record_by_its_course_and_title(tmp_path: pathlib.Path) -> None:
    """A row from the fixtures, under an id of its own, takes a paste's claims rather than
    getting a twin beside it."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        assignments, claims = read_whole(FixtureSource(FIXTURES))
        store.put_on_record(assignments, claims)
        before = len(store.all_assignments())
        read = read_text(
            "Tuesday 8/18/2026\nWorld History\nDue: Canal Era comparison essay:\n",
            now=NOW,
            today=date(2026, 8, 19),
        )
        changes = changes_for(read.items, store)
        kept = keep(read.items, store)
        after = store.all_assignments()
        essay_claims = store.deadline_records("assignment-canal-essay")
    finally:
        store.close()

    assert [change.state for change in changes] == [CLAIMED]
    assert changes[0].assignment_id == "assignment-canal-essay"
    assert kept == 1
    assert len(after) == before
    assert essay_claims[-1].asserted_value == "2026-08-18"
    assert essay_claims[-1].seen_in == DAY_HEADER


def test_two_keepings_of_one_text_at_once_add_nothing_twice(tmp_path: pathlib.Path) -> None:
    """The comparison and the write are one: whichever keeping goes first, the other finds
    the record filled and adds nothing, and the claims are on record once."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    read = read_text(SUMMARY, now=NOW, today=TODAY)
    released = threading.Barrier(2)

    def one_keeping(_: int) -> int:
        released.wait()
        return keep(read.items, store)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(one_keeping, range(2)))
        rows = store.all_assignments()
        covers = {(item.course, item.title): item for item in rows}["08 Geometry", "Book Covers"]
        claims = store.deadline_records(covers.assignment_id)
    finally:
        store.close()

    assert sorted(counts) == [0, 9]
    assert len(rows) == 9
    assert len(claims) == 2
