"""Reading the school's text: the weekly summary, the homework page, and the email.

The text here is synthetic, in the portal's shapes: a fictional first name in
the heading, ordinary course names, and made-up titles with the portal's
punctuation. Nothing from a real family appears in it. The three-week summary
has the structure of a real download, hyphen bullets included.
"""

import pathlib
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, date, datetime

import pytest

from blossom.intake import (
    CLAIMED,
    DAY_HEADER,
    EMAIL_DATE_LINE,
    FOLDED,
    KNOWN,
    NEW,
    NEW_WORK,
    OWN_LINE,
    PASTE_DAY,
    REVIEW,
    UPDATE,
    Change,
    Held,
    Kept,
    Reading,
    by_hand,
    by_week,
    changes_for,
    conflicting_choices,
    identity,
    keep,
    kind_of,
    read_text,
    slug,
    spoken_report,
    within_a_school_year,
)
from blossom.noticing import read_week
from blossom.reconciliation import SourceChannel
from blossom.sources import FixtureSource, read_whole
from blossom.stores.project_state import Assignment, AssignmentKind, ProjectStateStore
from tests.support import FIXTURES, fixture_clock

NOW = datetime(2026, 9, 12, 20, 0, tzinfo=UTC)
TODAY = date(2026, 9, 12)

THREE_WEEKS = """Homework for Wren

- 09/01/2026 - Tuesday
Humanities - Due: Reading check:
Religion - Assigned: Syllabus Due: (Due:09/09/2026)
Return the syllabus with a parent's signature after reading it.
- 09/02/2026 - Wednesday
08 Geometry - Assigned: Syllabus- Signed : (Due:09/08/2026)
- 09/03/2026 - Thursday
08 Geometry - Assigned: Book Covers: (Due:09/08/2026)
- 09/04/2026 - Friday
08 Geometry - Assigned: PR1 U2.3 Pg 40 #1-9: (Due:09/08/2026)
This should be done IN your math notebook.
Humanities - Due: Summer Reading - Log:
Spanish - Assigned: Binder, labeled dividers and lined paper check: (Due:09/11/2026)


Homework for Wren

- 09/08/2026 - Tuesday
08 Geometry - Assigned: PR2 U2 pg. 41 #10, 12, 15 : (Due:09/09/2026)
08 Geometry - Assigned: U2 Quiz 1: (Due:09/11/2026)
08 Geometry - Due: Book Covers:
08 Geometry - Due: PR1 U2.3 Pg 40 #1-9:
This should be done IN your math notebook.
08 Geometry - Due: Syllabus- Signed :
Humanities - Due: First draft, What I See? poem:
- 09/09/2026 - Wednesday
08 Geometry - Assigned: PR3 U2-3 Practice WS: (Due:09/10/2026)
08 Geometry - Due: PR2 U2 pg. 41 #10, 12, 15 :
Religion - Due: Syllabus Due:
Return the syllabus with a parent's signature after reading it.
- 09/10/2026 - Thursday
08 Geometry - Due: PR3 U2-3 Practice WS:
- 09/11/2026 - Friday
08 Geometry - Due: U2 Quiz 1:
Humanities - Assigned: What I See? poem and artwork: (Due:09/15/2026)
Spanish - Due: Binder, labeled dividers and lined paper check:


Homework for Wren

- 09/15/2026 - Tuesday
Humanities - Due: What I See? poem and artwork:
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
09/09 07 MTH ALG - C: Homework: Fraction practice Grade: Missing
09/09 Spanish - A: Homework: Vocabulary list Grade: Complete
"""


def by_pair(items: tuple[Reading, ...]) -> dict[tuple[str, str], Reading]:
    return {item.pair: item for item in items}


def test_the_three_week_summary_yields_every_card_and_every_date_where_it_belongs() -> None:
    """Twelve assignments from twenty-one cards, nine of them assigned, with the due dates
    the cards give and each instruction under the one card it was written under."""
    read = read_text(THREE_WEEKS, now=NOW, today=TODAY)
    items = by_pair(read.items)

    assert read.unread == ()
    assert len(items) == 12
    assert sum(len(item.claims) for item in read.items) == 21
    assert sum(1 for item in read.items if item.assigned_on is not None) == 9
    assert {item.due_date for item in read.items} == {
        date(2026, 9, 1),
        date(2026, 9, 4),
        date(2026, 9, 8),
        date(2026, 9, 9),
        date(2026, 9, 10),
        date(2026, 9, 11),
        date(2026, 9, 15),
    }
    notebook = [item for item in read.items if item.note and "math notebook" in item.note]
    assert [item.title for item in notebook] == ["PR1 U2.3 Pg 40 #1-9"]
    syllabus = [item for item in read.items if item.note and "syllabus" in item.note]
    assert [(item.course, item.title) for item in syllabus] == [("Religion", "Syllabus Due")]
    covers = items["08 Geometry", "Book Covers"]
    assert (covers.due_date, covers.assigned_on) == (date(2026, 9, 8), date(2026, 9, 3))
    assert [(said.seen_in, said.asserted_value) for said in covers.claims] == [
        (OWN_LINE, "2026-09-08"),
        (DAY_HEADER, "2026-09-08"),
    ]
    assert items["Humanities", "Summer Reading - Log"].due_date == date(2026, 9, 4)
    assert items["Humanities", "What I See? poem and artwork"].assigned_on == date(2026, 9, 11)
    assert covers.at_line == 10


def test_bullets_line_endings_and_download_line_breaks_read_the_same() -> None:
    plain = read_text(THREE_WEEKS, now=NOW, today=TODAY)
    starred = read_text(THREE_WEEKS.replace("\n- ", "\n* "), now=NOW, today=TODAY)
    bare = read_text(THREE_WEEKS.replace("\n- ", "\n"), now=NOW, today=TODAY)
    crlf = read_text(THREE_WEEKS.replace("\n", "\r\n"), now=NOW, today=TODAY)
    broken = read_text(THREE_WEEKS.replace("\n", "\\\n"), now=NOW, today=TODAY)
    kept = read_text(
        "Tuesday 9/1/2026\nArt\nAssigned: Poster: (Due:09/08/2026)\nUse C:\\\\art\\\\poster.\n",
        now=NOW,
        today=TODAY,
    )

    for other in (starred, bare, crlf, broken):
        assert other.unread == ()
        assert [(item.pair, item.due_date, item.note) for item in other.items] == [
            (item.pair, item.due_date, item.note) for item in plain.items
        ]
    assert kept.items[0].note == "Use C:\\\\art\\\\poster."


def test_the_homework_page_is_read_with_the_course_on_its_own_line() -> None:
    read = read_text(PAGE, now=NOW, today=TODAY)
    items = by_pair(read.items)

    assert read.unread == ()
    assert set(items) == {("08 Geometry", "Book Covers"), ("Humanities", "Reading check")}
    covers = items["08 Geometry", "Book Covers"]
    assert covers.assigned_on == date(2026, 9, 1)
    assert covers.note == "Cover both books with paper."
    assert [said.seen_in for said in covers.claims] == [OWN_LINE, DAY_HEADER]
    assert items["Humanities", "Reading check"].note is None


def test_a_line_shaped_like_a_card_that_does_not_read_as_one_is_never_a_note() -> None:
    """A due date the reader cannot read, an assigned card with no course, a day that is no
    date: each is text for review, and each ends the card before it."""
    text = """Homework for Wren

- 09/08/2026 - Tuesday
Math - Assigned: Practice: (Due:09/10/2026)
Math - Assigned: Worksheet: (Due:TBD)
Bring a ruler.
- 09/31/2026 - Wednesday
Math - Due: Practice:
Tuesday 9/1/2026
Assigned: A card with no course before it: (Due:09/08/2026)
Do it neatly.
"""
    read = read_text(text, now=NOW, today=TODAY)
    items = by_pair(read.items)

    assert [(u.line, u.text) for u in read.unread] == [
        (5, "Math - Assigned: Worksheet: (Due:TBD)"),
        (6, "Bring a ruler."),
        (7, "- 09/31/2026 - Wednesday"),
        (8, "Math - Due: Practice:"),
        (10, "Assigned: A card with no course before it: (Due:09/08/2026)"),
        (11, "Do it neatly."),
    ]
    assert set(items) == {("Math", "Practice")}
    assert items["Math", "Practice"].note is None


def test_the_heading_under_a_card_is_the_teachers_words_and_a_long_paste_reads_whole() -> None:
    text = """Homework for Wren

- 09/03/2026 - Thursday
08 Geometry - Assigned: Book Covers: (Due:09/08/2026)
Homework for tomorrow: cover both books with paper.
"""
    read = read_text(text, now=NOW, today=TODAY)
    many = read_text("\n".join(["A stray line"] * 20_000), now=NOW, today=TODAY)

    assert read.unread == ()
    assert by_pair(read.items)["08 Geometry", "Book Covers"].note == (
        "Homework for tomorrow: cover both books with paper."
    )
    assert [item.line for item in many.unread] == list(range(1, 20_001))


def test_an_instruction_keeps_its_lines_and_is_kept_once_across_two_weeks() -> None:
    text = """Tuesday 9/8/2026
08 Geometry
Assigned: Poster: (Due:09/15/2026)
Use the big paper.
Two colors at least.
Tuesday 9/15/2026
08 Geometry
Due: Poster:
Use the big paper.
Two colors at least.
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert read.items[0].note == "Use the big paper.\nTwo colors at least."


def test_the_missing_email_is_a_report_with_its_day_and_never_a_due_date() -> None:
    read = read_text(EMAIL, now=NOW, today=TODAY)
    items = by_pair(read.items)

    assert [u.text for u in read.unread] == [
        "09/09 Spanish - A: Homework: Vocabulary list Grade: Complete"
    ]
    assert set(items) == {
        ("08 Geometry", "Book Covers"),
        ("Humanities", "Reading check"),
        ("07 MTH ALG", "Fraction practice"),
    }
    covers = items["08 Geometry", "Book Covers"]
    assert covers.claims == ()
    assert covers.due_date is None
    assert covers.origin is SourceChannel.EMAIL
    assert covers.reported_status == "missing"
    report = covers.reports[0]
    assert (report.channel, report.reported_on, report.dated_by) == (
        SourceChannel.EMAIL,
        TODAY,
        PASTE_DAY,
    )
    assert report.source_date_text == "09/09"
    assert spoken_report(report) == (
        "From the school email, pasted Saturday, September 12, 2026. The email writes 09/09 "
        "beside it, which it does not explain."
    )
    assert covers.assignment().reported_submission_status == "missing"
    dated = read_text("Tue, Sep 8, 2026 at 9:14 AM\n" + EMAIL, now=NOW, today=TODAY)
    dated_report = by_pair(dated.items)["08 Geometry", "Book Covers"].reports[0]
    assert (dated_report.reported_on, dated_report.dated_by) == (date(2026, 9, 8), EMAIL_DATE_LINE)
    assert spoken_report(dated_report).startswith(
        "From the school email, dated Tuesday, September 8, 2026."
    )


def test_the_emails_day_comes_only_from_its_date_line() -> None:
    """A date in a title or an instruction dates nothing; a mail program's own date line,
    in a header or a forwarded message, dates the report."""
    in_a_title = "Assignments:\n09/09 Math - A: Homework: Read September 8, 2026 Grade: Missing\n"
    in_an_instruction = (
        "Tuesday 9/1/2026\nMath\nDue: Practice:\nFinish by September 8, 2026.\n" + EMAIL
    )
    header = "From: The School\nDate: Tue, Sep 8, 2026 09:14\nSubject: Missing work\n" + EMAIL
    forwarded = "On Tue, Sep 8, 2026 at 9:14 AM The School wrote:\n" + EMAIL
    stray = "Read by Sep 8, 2026.\n" + EMAIL
    under_a_card = "Tuesday 9/1/2026\nMath\nDue: Practice:\nDate: Sep 8, 2026\n" + EMAIL
    after_the_reports = EMAIL + "Date: Tue, Sep 8, 2026 09:14\n"

    def report_day(text: str) -> tuple[date, str]:
        told = next(item for item in read_text(text, now=NOW, today=TODAY).items if item.reports)
        return told.reports[0].reported_on, told.reports[0].dated_by

    assert report_day(in_a_title) == (TODAY, PASTE_DAY)
    assert report_day(in_an_instruction) == (TODAY, PASTE_DAY)
    assert report_day(stray) == (TODAY, PASTE_DAY)
    assert report_day(header) == (date(2026, 9, 8), EMAIL_DATE_LINE)
    assert report_day(forwarded) == (date(2026, 9, 8), EMAIL_DATE_LINE)
    assert report_day(under_a_card) == (TODAY, PASTE_DAY)
    assert read_text(under_a_card, now=NOW, today=TODAY).items[0].note == "Date: Sep 8, 2026"
    assert report_day(after_the_reports) == (TODAY, PASTE_DAY)
    assert [item.text for item in read_text(header, now=NOW, today=TODAY).unread] == [
        "09/09 Spanish - A: Homework: Vocabulary list Grade: Complete"
    ]
    assert [item.text for item in read_text(forwarded, now=NOW, today=TODAY).unread] == [
        "09/09 Spanish - A: Homework: Vocabulary list Grade: Complete"
    ]


def test_the_type_is_a_task_only_for_paperwork_and_materials() -> None:
    assert kind_of("Syllabus- Signed") is AssignmentKind.TASK
    assert kind_of("Book Covers") is AssignmentKind.TASK
    assert kind_of("Binder, labeled dividers and lined paper check") is AssignmentKind.TASK
    assert kind_of("Signing the form") is AssignmentKind.TASK
    assert kind_of("Covering a book") is AssignmentKind.TASK
    assert kind_of("Bringing materials") is AssignmentKind.TASK
    assert kind_of("Signs permission form") is AssignmentKind.TASK
    assert kind_of("Brings materials") is AssignmentKind.TASK
    assert kind_of("Brought a book") is AssignmentKind.TASK
    assert kind_of("Covers the textbook") is AssignmentKind.TASK
    assert kind_of("Signed numbers practice") is AssignmentKind.HOMEWORK
    assert kind_of("Signs of life essay") is AssignmentKind.HOMEWORK
    assert kind_of("Discovery of cells worksheet") is AssignmentKind.HOMEWORK
    assert kind_of("Discovering cells") is AssignmentKind.HOMEWORK
    assert kind_of("PR1 U2.3 Pg 40 #1-9") is AssignmentKind.HOMEWORK
    assert kind_of("Cover letter draft") is AssignmentKind.HOMEWORK


def test_an_id_is_readable_and_keeps_titles_that_read_alike_apart() -> None:
    slashed = identity("08 Geometry", "Quiz A/B")
    dashed = identity("08 Geometry", "Quiz A-B")
    again = identity("08 Geometry", "Quiz A/B", occurrence="2026-09-14")
    long_title = identity("Humanities", "A title " + "long " * 30 + "past the readable part")

    assert slug("First draft, What I See? poem") == "first-draft-what-i-see-poem"
    assert slashed.startswith("assignment-quiz-a-b-")
    assert dashed.startswith("assignment-quiz-a-b-")
    assert len({slashed, dashed, again}) == 3
    assert identity(" 08  Geometry ", "Quiz  A/B") == slashed
    assert len(long_title) <= len("assignment-") + 40 + 1 + 8
    read = read_text(
        "Tuesday 9/1/2026\n08 Geometry\nDue: Quiz A/B:\nDue: Quiz A-B:\n", now=NOW, today=TODAY
    )
    assert len(read.items) == 2


def test_by_hand_is_the_familys_own_in_every_field_it_fills() -> None:
    dated = by_hand(
        "Spanish",
        " Vocabulary  list ",
        date(2026, 9, 20),
        None,
        AssignmentKind.HOMEWORK,
        "Ten words, both ways.",
        now=NOW,
    )
    undated = by_hand("Spanish", "Vocabulary list", None, None, AssignmentKind.TASK, now=NOW)

    assert dated.title == "Vocabulary list"
    assert dated.origin is SourceChannel.PARENT_ENTRY
    assert dated.note == "Ten words, both ways."
    assert [(said.channel, said.seen_in, said.confidence) for said in dated.claims] == [
        (SourceChannel.PARENT_ENTRY, None, 0.8)
    ]
    assert dated.assignment().origins == {
        "record": SourceChannel.PARENT_ENTRY,
        "note": SourceChannel.PARENT_ENTRY,
        "due_date": SourceChannel.PARENT_ENTRY,
        "kind": SourceChannel.PARENT_ENTRY,
    }
    assert undated.claims == ()
    assert undated.assignment().origins == {
        "record": SourceChannel.PARENT_ENTRY,
        "kind": SourceChannel.PARENT_ENTRY,
    }
    assert undated.assignment().reported_submission_status == "unknown"


def test_a_year_is_measured_by_the_calendar() -> None:
    today = date(2027, 9, 12)

    assert within_a_school_year(date(2028, 9, 12), today)
    assert not within_a_school_year(date(2028, 9, 13), today)
    assert within_a_school_year(date(2026, 9, 12), today)
    assert not within_a_school_year(date(2026, 9, 11), today)
    assert within_a_school_year(date(2029, 2, 28), date(2028, 2, 29))
    assert not within_a_school_year(date(2029, 3, 1), date(2028, 2, 29))


def test_changes_are_new_updated_or_unchanged_and_saved_as_one(tmp_path: pathlib.Path) -> None:
    """A paste is new the first time, unchanged the second, an update when a later week names
    another date, the recorded date standing, and an update again when the school reports."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        read = read_text(THREE_WEEKS, now=NOW, today=TODAY)
        first = changes_for(read.items, store)
        kept_first = keep(read.items, store)
        kept_again = keep(read.items, store)
        later = read_text(
            "Homework for Wren\n- 09/10/2026 - Thursday\n08 Geometry - Due: Book Covers:\n",
            now=NOW,
            today=TODAY,
        )
        moved = changes_for(later.items, store)
        kept_moved = keep(later.items, store)
        covers = {(item.course, item.title): item for item in store.all_assignments()}[
            "08 Geometry", "Book Covers"
        ]
        claims = store.deadline_records(covers.assignment_id)
        told = read_text(EMAIL, now=NOW, today=TODAY)
        reported = {change.reading.pair: change for change in changes_for(told.items, store)}
        kept_told = keep(told.items, store)
        rows = {(item.course, item.title): item for item in store.all_assignments()}
        reports = store.status_reports(rows["08 Geometry", "Book Covers"].assignment_id)
        kept_told_again = keep(told.items, store)
    finally:
        store.close()

    assert [change.state for change in first] == [NEW] * 12
    assert kept_first == Kept(added=12, updated=0, unchanged=0)
    assert kept_again == Kept(added=0, updated=0, unchanged=12)
    assert [change.state for change in moved] == [CLAIMED]
    assert moved[0].label == "Saved; adds a date to review"
    assert moved[0].dates_differ
    assert moved[0].saved_due == date(2026, 9, 8)
    assert "beside the saved date, which stays" in moved[0].effect
    assert kept_moved == Kept(added=0, updated=1, unchanged=0)
    assert covers.due_date == date(2026, 9, 8)
    assert [said.asserted_value for said in claims] == ["2026-09-08", "2026-09-08", "2026-09-10"]
    assert reported["08 Geometry", "Book Covers"].label == "Saved; adds what the school reports"
    assert reported["07 MTH ALG", "Fraction practice"].state == NEW
    assert kept_told == Kept(added=1, updated=2, unchanged=0)
    assert rows["08 Geometry", "Book Covers"].reported_submission_status == "missing"
    assert rows["08 Geometry", "Book Covers"].due_date == date(2026, 9, 8)
    assert rows["07 MTH ALG", "Fraction practice"].due_date is None
    assert [(r.status, r.reported_on) for r in reports] == [("missing", TODAY)]
    assert kept_told_again == Kept(added=0, updated=0, unchanged=3)


def test_what_the_record_lacks_is_filled_and_a_note_follows_a_stated_policy(
    tmp_path: pathlib.Path,
) -> None:
    """A row typed without dates takes the pasted due and assigned dates as its own; the
    school's note fills an empty note and replaces the school's earlier note; a parent's
    note replaces any and is never replaced by the school's; a parent's type is theirs."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        typed = by_hand(
            "Art", "Sketchbook, three pages", None, None, AssignmentKind.HOMEWORK, now=NOW
        )
        keep((typed,), store)
        page = read_text(
            "Tuesday 9/8/2026\nArt\nAssigned: Sketchbook, three pages: (Due:09/18/2026)\n"
            "Pencil only.\n",
            now=NOW,
            today=TODAY,
        )
        filled = changes_for(page.items, store)
        keep(page.items, store)
        sketch = {(i.course, i.title): i for i in store.all_assignments()}[
            "Art", "Sketchbook, three pages"
        ]
        revised = read_text(
            "Tuesday 9/8/2026\nArt\nAssigned: Sketchbook, three pages: (Due:09/18/2026)\n"
            "Pencil or ink.\n",
            now=NOW,
            today=TODAY,
        )
        updated_note = changes_for(revised.items, store)
        keep(revised.items, store)
        parent_note = by_hand(
            "Art",
            "Sketchbook, three pages",
            None,
            None,
            AssignmentKind.HOMEWORK,
            "Use the sketchbook from last year.",
            now=NOW,
        )
        replaces = changes_for((parent_note,), store)
        keep((parent_note,), store)
        kind_chosen = changes_for(page.items, store, kinds={0: AssignmentKind.TASK})
        keep(page.items, store, kinds={0: AssignmentKind.TASK})
        after = {(i.course, i.title): i for i in store.all_assignments()}[
            "Art", "Sketchbook, three pages"
        ]
        remembered = changes_for(page.items, store)
    finally:
        store.close()

    assert filled[0].label == "Saved; adds the due date, the assigned date and the note"
    assert "The record has no due date of its own" in filled[0].effect
    assert sketch.due_date == date(2026, 9, 18)
    assert sketch.assigned_on == date(2026, 9, 8)
    assert sketch.note == "Pencil only."
    assert sketch.origins["record"] is SourceChannel.PARENT_ENTRY
    assert sketch.origins["due_date"] is SourceChannel.LMS
    assert sketch.origins["note"] is SourceChannel.LMS
    assert sketch.origins["assigned_on"] is SourceChannel.LMS
    assert updated_note[0].note_change == "update"
    assert "The school's note replaces the school's earlier note." in updated_note[0].effect
    assert replaces[0].note_change == "update"
    assert "Your note replaces the saved note." in replaces[0].effect
    assert kind_chosen[0].note_change == "kept"
    assert kind_chosen[0].new_kind is AssignmentKind.TASK
    assert "The saved note stands" in kind_chosen[0].effect
    assert after.kind is AssignmentKind.TASK
    assert after.origins["kind"] is SourceChannel.PARENT_ENTRY
    assert after.origins["note"] is SourceChannel.PARENT_ENTRY
    assert after.note == "Use the sketchbook from last year."
    assert remembered[0].kind is AssignmentKind.TASK
    assert remembered[0].state == KNOWN
    assert "The saved note stands" in remembered[0].effect


WEEK_ONE = "Homework for Wren\n- 09/07/2026 - Monday\nMath - Due: Weekly practice:\n"
WEEK_TWO = "Homework for Wren\n- 09/14/2026 - Monday\nMath - Due: Weekly practice:\n"
WEEK_THREE = "Homework for Wren\n- 09/21/2026 - Monday\nMath - Due: Weekly practice:\n"


def readings(text: str) -> tuple[Reading, ...]:
    return read_text(text, now=NOW, today=TODAY).items


def test_repeated_work_under_one_name_is_a_question_and_either_answer_is_kept(
    tmp_path: pathlib.Path,
) -> None:
    """A weekly practice due a week after the saved one is not merged in silence. Saying it
    is the same assignment moves the saved date, so the next paste, before or after a
    restart, finds it and asks nothing; saying it is new work makes a row of its own, and
    the same answer given again finds that row and changes nothing."""
    path = tmp_path / "blossom.sqlite3"
    store = ProjectStateStore.open(path, fixture_clock())
    try:
        keep(readings(WEEK_ONE), store)
        asked = changes_for(readings(WEEK_TWO), store)
        unanswered = keep(readings(WEEK_TWO), store)
        as_update = changes_for(readings(WEEK_TWO), store, occurrences={0: UPDATE})
        kept_update = keep(readings(WEEK_TWO), store, occurrences={0: UPDATE})
        moved = store.all_assignments()
        moved_claims = store.deadline_records(moved[0].assignment_id)
        again = changes_for(readings(WEEK_TWO), store)
        old_week = changes_for(readings(WEEK_ONE), store)
    finally:
        store.close()
    store = ProjectStateStore.open(path, fixture_clock())
    try:
        after_restart = changes_for(readings(WEEK_TWO), store)
        asked_again = changes_for(readings(WEEK_THREE), store)
        kept_new = keep(readings(WEEK_THREE), store, occurrences={0: NEW_WORK})
        rows = store.all_assignments()
        replayed = keep(readings(WEEK_THREE), store, occurrences={0: NEW_WORK})
        rows_after_replay = store.all_assignments()
        nudged = changes_for(
            readings("Homework for Wren\n- 09/22/2026 - Tuesday\nMath - Due: Weekly practice:\n"),
            store,
        )
    finally:
        store.close()

    assert [change.state for change in asked] == [REVIEW]
    assert asked[0].label == "Needs your answer"
    assert asked[0].beside == date(2026, 9, 7)
    assert asked[0].effect.startswith("The saved assignment is due Monday, September 7, 2026.")
    assert isinstance(unanswered, list)
    assert [change.state for change in as_update] == [CLAIMED]
    assert as_update[0].label == "Saved; adds the due date"
    assert "The due date becomes Monday, September 14, 2026, as you said" in as_update[0].effect
    assert kept_update == Kept(added=0, updated=1, unchanged=0)
    assert [(row.due_date, row.origins["due_date"]) for row in moved] == [
        (date(2026, 9, 14), SourceChannel.LMS)
    ]
    assert [said.asserted_value for said in moved_claims] == ["2026-09-07", "2026-09-14"]
    assert [change.state for change in again] == [KNOWN]
    assert [change.state for change in old_week] == [KNOWN]
    assert [change.state for change in after_restart] == [KNOWN]
    assert [change.state for change in asked_again] == [REVIEW]
    assert asked_again[0].beside == date(2026, 9, 14)
    assert kept_new == Kept(added=1, updated=0, unchanged=0)
    assert sorted(row.due_date for row in rows if row.due_date) == [
        date(2026, 9, 14),
        date(2026, 9, 21),
    ]
    assert len({row.assignment_id for row in rows}) == 2
    assert replayed == Kept(added=0, updated=0, unchanged=1)
    assert len(rows_after_replay) == 2
    assert [change.state for change in nudged] == [CLAIMED]
    assert nudged[0].saved_due == date(2026, 9, 21)


def test_a_confirmed_new_round_keeps_what_the_family_added_when_the_answer_is_replayed(
    tmp_path: pathlib.Path,
) -> None:
    """A retry, or an older review page still open, sends the same new-work answer again
    after a parent has added a note and the school has reported on the new row: the answer
    finds that row and changes nothing, so nothing the family added is lost."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        keep(readings(WEEK_ONE), store)
        keep(readings(WEEK_TWO), store, occurrences={0: NEW_WORK})
        note = by_hand(
            "Math",
            "Weekly practice",
            date(2026, 9, 14),
            None,
            AssignmentKind.HOMEWORK,
            "Parent clarification",
            now=NOW,
        )
        keep((note,), store)
        told = readings("Assignments:\n09/14 Math - A: Homework: Weekly practice Grade: Missing\n")
        keep(told, store)
        replayed = keep(readings(WEEK_TWO), store, occurrences={0: NEW_WORK})
        rows = {row.due_date: row for row in store.all_assignments()}
        second = rows[date(2026, 9, 14)]
        claims = store.deadline_records(second.assignment_id)
    finally:
        store.close()

    assert replayed == Kept(added=0, updated=0, unchanged=1)
    assert len(rows) == 2
    assert second.note == "Parent clarification"
    assert second.origins["note"] is SourceChannel.PARENT_ENTRY
    assert second.reported_submission_status == "missing"
    assert len(claims) == 2
    assert [said.channel for said in claims] == [SourceChannel.LMS, SourceChannel.PARENT_ENTRY]


def test_two_rounds_of_a_name_in_one_text_are_read_apart_and_asked_about(
    tmp_path: pathlib.Path,
) -> None:
    """A text that names an assignment due a week apart twice, weeks in either order, is
    two readings and one question: new work makes two rows that the next paste finds by
    their dates; the same assignment folds the later card into the first."""
    both = WEEK_ONE + WEEK_TWO.replace("Homework for Wren\n", "")
    reverse = WEEK_TWO + WEEK_ONE.replace("Homework for Wren\n", "")
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        read = readings(both)
        asked = changes_for(read, store)
        unanswered = keep(read, store)
        kept_new = keep(read, store, occurrences={1: NEW_WORK})
        rows = store.all_assignments()
        again = changes_for(read, store)
        reversed_again = changes_for(readings(reverse), store)
    finally:
        store.close()
    folded_store = ProjectStateStore.open(tmp_path / "folded.sqlite3", fixture_clock())
    try:
        folded_changes = changes_for(readings(both), folded_store, occurrences={1: UPDATE})
        kept_folded = keep(readings(both), folded_store, occurrences={1: UPDATE})
        folded_rows = folded_store.all_assignments()
        folded_claims = folded_store.deadline_records(folded_rows[0].assignment_id)
    finally:
        folded_store.close()
    reverse_store = ProjectStateStore.open(tmp_path / "reverse.sqlite3", fixture_clock())
    try:
        backwards = readings(reverse)
        kept_backwards = keep(backwards, reverse_store, occurrences={1: NEW_WORK})
        backwards_rows = reverse_store.all_assignments()
        forwards_again = changes_for(readings(both), reverse_store)
    finally:
        reverse_store.close()

    assert [(item.due_date, item.occurrence) for item in read] == [
        (date(2026, 9, 7), None),
        (date(2026, 9, 14), "2026-09-14"),
    ]
    assert len({item.assignment_id for item in read}) == 2
    assert [change.state for change in asked] == [NEW, REVIEW]
    assert asked[1].beside == date(2026, 9, 7)
    assert asked[1].effect.startswith("This text also has it due Monday, September 7, 2026.")
    assert isinstance(unanswered, list)
    assert kept_new == Kept(added=2, updated=0, unchanged=0)
    assert sorted(row.due_date for row in rows if row.due_date) == [
        date(2026, 9, 7),
        date(2026, 9, 14),
    ]
    assert len({row.assignment_id for row in rows}) == 2
    assert [change.state for change in again] == [KNOWN, KNOWN]
    assert [change.state for change in reversed_again] == [KNOWN, KNOWN]
    assert [change.state for change in folded_changes] == [NEW, FOLDED]
    assert folded_changes[1].folded_into == 0
    assert folded_changes[0].reading.due_date == date(2026, 9, 14)
    assert kept_folded == Kept(added=1, updated=0, unchanged=0)
    assert [row.due_date for row in folded_rows] == [date(2026, 9, 14)]
    assert [said.asserted_value for said in folded_claims] == ["2026-09-07", "2026-09-14"]
    assert [item.occurrence for item in backwards] == [None, "2026-09-07"]
    assert kept_backwards == Kept(added=2, updated=0, unchanged=0)
    assert sorted(row.due_date for row in backwards_rows if row.due_date) == [
        date(2026, 9, 7),
        date(2026, 9, 14),
    ]
    assert [change.state for change in forwards_again] == [KNOWN, KNOWN]


TWO_WEEKS_OF_PRACTICE = (
    "Tuesday 9/8/2026\nMath\nDue: Weekly practice:\n\n"
    "Tuesday 9/15/2026\nMath\nDue: Weekly practice:\n"
)
"""One name a week apart: two cards, and one question."""


def test_a_type_chosen_on_any_card_about_one_assignment_is_the_assignments(
    tmp_path: pathlib.Path,
) -> None:
    """A type chosen on the later of two cards, folded into the first as the same
    assignment, is the new row's; a type chosen on one of two cards about a saved row is
    the row's, whichever card. Each outlives a restart, is what the next paste suggests,
    and the same choices sent again change nothing."""
    cards = readings(TWO_WEEKS_OF_PRACTICE)
    folded_path = tmp_path / "folded.sqlite3"
    store = ProjectStateStore.open(folded_path, fixture_clock())
    try:
        promised = changes_for(
            cards, store, occurrences={1: UPDATE}, kinds={1: AssignmentKind.TASK}
        )
        kept_folded = keep(cards, store, occurrences={1: UPDATE}, kinds={1: AssignmentKind.TASK})
        folded_rows = store.all_assignments()
    finally:
        store.close()
    store = ProjectStateStore.open(folded_path, fixture_clock())
    try:
        after_restart = store.all_assignments()
        later = changes_for(readings(TWO_WEEKS_OF_PRACTICE.split("\n\n")[1]), store)
        replayed = keep(cards, store, occurrences={1: UPDATE}, kinds={1: AssignmentKind.TASK})
        after_replay = store.all_assignments()
    finally:
        store.close()
    outcomes: dict[str, tuple[Kept | Held | list[Change], list[Assignment]]] = {}
    for name, kinds in {
        "chosen on the first": {0: AssignmentKind.TASK},
        "chosen on the second": {1: AssignmentKind.TASK},
    }.items():
        saved = ProjectStateStore.open(tmp_path / f"{slug(name)}.sqlite3", fixture_clock())
        try:
            keep(readings(SAVED_WEEK), saved)
            both = {0: UPDATE, 1: UPDATE}
            kept = keep(cards, saved, occurrences=both, kinds=kinds)
            outcomes[name] = (kept, saved.all_assignments())
        finally:
            saved.close()

    assert [change.state for change in promised] == [NEW, FOLDED]
    assert (promised[0].kind, promised[0].kind_by_parent) == (AssignmentKind.TASK, True)
    assert kept_folded == Kept(added=1, updated=0, unchanged=0)
    assert [(row.due_date, row.kind) for row in folded_rows] == [
        (date(2026, 9, 15), AssignmentKind.TASK)
    ]
    assert folded_rows[0].origins["kind"] is SourceChannel.PARENT_ENTRY
    assert after_restart == folded_rows
    assert [(change.state, change.kind) for change in later] == [(KNOWN, AssignmentKind.TASK)]
    assert replayed == Kept(added=0, updated=0, unchanged=1)
    assert after_replay == folded_rows
    for name, (kept, rows) in outcomes.items():
        assert kept == Kept(added=0, updated=1, unchanged=0), name
        assert [(row.due_date, row.kind) for row in rows] == [
            (date(2026, 9, 15), AssignmentKind.TASK)
        ], name
        assert rows[0].origins["kind"] is SourceChannel.PARENT_ENTRY, name


THREE_WEEKS_OF_PRACTICE = (
    TWO_WEEKS_OF_PRACTICE + "\nTuesday 9/22/2026\nMath\nDue: Weekly practice:\n"
)


def test_the_card_shown_for_an_assignment_decides_its_type_over_a_folded_card(
    tmp_path: pathlib.Path,
) -> None:
    """A choice on the card shown for an assignment stands over any a folded card carried,
    a choice of the reader's own suggestion included; two folded cards choosing differently
    with no choice on the card shown is a conflict, which the saving hands back rather than
    settling on its own."""
    cards = readings(TWO_WEEKS_OF_PRACTICE)
    three = readings(THREE_WEEKS_OF_PRACTICE)
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        shown_wins = changes_for(
            cards,
            store,
            occurrences={1: UPDATE},
            kinds={0: AssignmentKind.HOMEWORK, 1: AssignmentKind.TASK},
        )
        kept = keep(
            cards,
            store,
            occurrences={1: UPDATE},
            kinds={0: AssignmentKind.HOMEWORK, 1: AssignmentKind.TASK},
        )
        rows = store.all_assignments()
    finally:
        store.close()
    other = ProjectStateStore.open(tmp_path / "conflict.sqlite3", fixture_clock())
    try:
        both_folds = {1: UPDATE, 2: UPDATE}
        disagreeing = {1: AssignmentKind.TASK, 2: AssignmentKind.HOMEWORK}
        conflict = changes_for(three, other, occurrences=both_folds, kinds=disagreeing)
        refused = keep(three, other, occurrences=both_folds, kinds=disagreeing)
        nothing = other.all_assignments()
        settled = keep(
            three, other, occurrences=both_folds, kinds={**disagreeing, 0: AssignmentKind.HOMEWORK}
        )
        settled_rows = other.all_assignments()
    finally:
        other.close()

    assert [change.state for change in shown_wins] == [NEW, FOLDED]
    assert (shown_wins[0].kind, shown_wins[0].kind_by_parent) == (AssignmentKind.HOMEWORK, True)
    assert kept == Kept(added=1, updated=0, unchanged=0)
    assert [(row.due_date, row.kind) for row in rows] == [
        (date(2026, 9, 15), AssignmentKind.HOMEWORK)
    ]
    assert rows[0].origins["kind"] is SourceChannel.PARENT_ENTRY
    assert [change.state for change in conflict] == [NEW, FOLDED, FOLDED]
    assert conflict[0].choices_conflict
    assert conflicting_choices(conflict) == ["Math: Weekly practice"]
    assert isinstance(refused, list)
    assert nothing == []
    assert settled == Kept(added=1, updated=0, unchanged=0)
    assert [(row.due_date, row.kind) for row in settled_rows] == [
        (date(2026, 9, 22), AssignmentKind.HOMEWORK)
    ]


def test_a_type_left_unchosen_on_an_entry_is_the_saved_rows_or_the_titles(
    tmp_path: pathlib.Path,
) -> None:
    """An entry that only adds a note to a saved task leaves it a task; an entry for a new
    row with no type chosen takes the title's suggestion; a type chosen is the parent's."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        keep(readings("Tuesday 9/8/2026\nReligion\nDue: Syllabus:\n"), store)
        noted = by_hand("Religion", "Syllabus", None, None, None, "Bring it Monday.", now=NOW)
        adds_a_note = changes_for((noted,), store)
        keep((noted,), store)
        syllabus = store.all_assignments()[0]
        covers = by_hand("Art", "Book covers", None, None, None, now=NOW)
        keep((covers,), store)
        rows = {row.title: row for row in store.all_assignments()}
    finally:
        store.close()

    assert not noted.kind_chosen
    assert [change.state for change in adds_a_note] == [CLAIMED]
    assert adds_a_note[0].label == "Saved; adds the note"
    assert (adds_a_note[0].kind, adds_a_note[0].kind_by_parent) == (AssignmentKind.TASK, False)
    assert syllabus.kind is AssignmentKind.TASK
    assert syllabus.origins["kind"] is SourceChannel.LMS
    assert syllabus.note == "Bring it Monday."
    assert (covers.kind, covers.kind_chosen) == (AssignmentKind.TASK, False)
    assert rows["Book covers"].kind is AssignmentKind.TASK


def test_an_entered_date_is_called_entered_in_the_effect(tmp_path: pathlib.Path) -> None:
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        keep((by_hand("Art", "Sketchbook", None, None, AssignmentKind.HOMEWORK, now=NOW),), store)
        fills = changes_for(
            (
                by_hand(
                    "Art", "Sketchbook", date(2026, 9, 18), None, AssignmentKind.HOMEWORK, now=NOW
                ),
            ),
            store,
        )
        keep(readings(SAVED_WEEK), store)
        beside = changes_for(
            (
                by_hand(
                    "Math",
                    "Weekly practice",
                    date(2026, 9, 3),
                    None,
                    AssignmentKind.HOMEWORK,
                    now=NOW,
                ),
            ),
            store,
        )
        pasted = changes_for(readings("Thursday 9/3/2026\nMath\nDue: Weekly practice:\n"), store)
    finally:
        store.close()

    assert "so the entered date becomes it." in fills[0].effect
    assert "The entered date is added as evidence beside the saved date" in beside[0].effect
    assert "The pasted date is added as evidence beside the saved date" in pasted[0].effect


def test_a_card_repeated_in_one_text_is_one_claim_and_a_report_repeated_is_one_report(
    tmp_path: pathlib.Path,
) -> None:
    doubled = "Tuesday 9/8/2026\nMath\nDue: Practice:\nMath\nDue: Practice:\n"
    told_twice = (
        "Assignments:\n09/08 Math - A: Homework: Practice Grade: Missing\n"
        "09/08 Math - A: Homework: Practice Grade: Missing\n"
    )
    enriched = doubled + "Monday 9/7/2026\nMath\nAssigned: Practice: (Due:09/08/2026)\n"
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        read = readings(doubled)
        told = readings(told_twice)
        kept = keep(read, store)
        row = store.all_assignments()[0]
        claims_once = store.deadline_records(row.assignment_id)
        more = changes_for(readings(enriched), store)
        keep(readings(enriched), store)
        claims_twice = store.deadline_records(row.assignment_id)
        keep(told, store)
        reports = store.status_reports(row.assignment_id)
    finally:
        store.close()

    assert len(read) == 1
    assert len(read[0].claims) == 1
    assert len(told) == 1
    assert len(told[0].reports) == 1
    assert kept == Kept(added=1, updated=0, unchanged=0)
    assert len(claims_once) == 1
    assert [len(change.new_claims) for change in more] == [1]
    assert [said.seen_in for said in claims_twice] == [DAY_HEADER, OWN_LINE]
    assert len(reports) == 1


def test_a_type_typed_with_an_entry_corrects_a_saved_row_and_the_correction_is_kept(
    tmp_path: pathlib.Path,
) -> None:
    """The type on the entry form is the parent's word: it corrects a saved row, outlives a
    restart, and stands when the school's text is pasted again."""
    path = tmp_path / "blossom.sqlite3"
    school = "Tuesday 9/8/2026\nReligion\nDue: Syllabus:\n"
    store = ProjectStateStore.open(path, fixture_clock())
    try:
        keep(readings(school), store)
        as_read = store.all_assignments()[0]
        typed = by_hand("Religion", "Syllabus", None, None, AssignmentKind.HOMEWORK, now=NOW)
        correction = changes_for((typed,), store)
        same = changes_for(
            (by_hand("Religion", "Syllabus", None, None, AssignmentKind.TASK, now=NOW),), store
        )
        kept = keep((typed,), store)
    finally:
        store.close()
    store = ProjectStateStore.open(path, fixture_clock())
    try:
        corrected = store.all_assignments()[0]
        pasted_again = changes_for(readings(school), store)
        keep(readings(school), store)
        still = store.all_assignments()[0]
    finally:
        store.close()

    assert as_read.kind is AssignmentKind.TASK
    assert [change.state for change in correction] == [CLAIMED]
    assert correction[0].label == "Saved; adds the type"
    assert correction[0].effect == "The type becomes homework, as chosen."
    assert [change.state for change in same] == [KNOWN]
    assert kept == Kept(added=0, updated=1, unchanged=0)
    assert corrected.kind is AssignmentKind.HOMEWORK
    assert corrected.origins["kind"] is SourceChannel.PARENT_ENTRY
    assert [change.state for change in pasted_again] == [KNOWN]
    assert pasted_again[0].kind is AssignmentKind.HOMEWORK
    assert still.kind is AssignmentKind.HOMEWORK


SAVED_WEEK = "Tuesday 9/1/2026\nMath\nDue: Weekly practice:\n"
THREE_CARDS = (
    SAVED_WEEK
    + "Tuesday 9/8/2026\nMath\nDue: Weekly practice:\nTeacher instruction.\n"
    + "Monday 9/14/2026\nMath\nAssigned: Weekly practice: (Due:09/15/2026)\n"
)
"""One saved week again, a week later with the teacher's words, and a week after that as
assigned work: three cards about one assignment."""


def test_several_cards_about_one_saved_row_compose_into_one_row(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two updates said yes to and one card already saved, in one text: the date the last
    card gives, the note the middle one gives, the assigned date the last one fills, and
    every date observation, all in one row, counted once; the same text saved again moves
    nothing; and a write refused by the file leaves the row as it was."""
    path = tmp_path / "blossom.sqlite3"
    store = ProjectStateStore.open(path, fixture_clock())
    try:
        keep(readings(SAVED_WEEK), store)
        cards = readings(THREE_CARDS)
        asked = changes_for(cards, store)
        answered = changes_for(cards, store, occurrences={1: UPDATE, 2: UPDATE})
        kept = keep(cards, store, occurrences={1: UPDATE, 2: UPDATE})
        rows = store.all_assignments()
        claims = store.deadline_records(rows[0].assignment_id)
    finally:
        store.close()
    store = ProjectStateStore.open(path, fixture_clock())
    try:
        after_restart = store.all_assignments()
        replayed = keep(cards, store, occurrences={1: UPDATE, 2: UPDATE})
        rows_after_replay = store.all_assignments()
        claims_after_replay = store.deadline_records(rows[0].assignment_id)
    finally:
        store.close()
    refusing = ProjectStateStore.open(tmp_path / "refusing.sqlite3", fixture_clock())
    try:
        keep(readings(SAVED_WEEK), refusing)

        def refuse(*_: object) -> None:
            msg = "the file refused the claims"
            raise RuntimeError(msg)

        monkeypatch.setattr(refusing, "_record_claims_locked", refuse)
        with pytest.raises(RuntimeError, match="refused"):
            keep(cards, refusing, occurrences={1: UPDATE, 2: UPDATE})
        untouched = refusing.all_assignments()
        untouched_claims = refusing.deadline_records(untouched[0].assignment_id)
    finally:
        refusing.close()

    assert [(item.due_date, item.occurrence) for item in cards] == [
        (date(2026, 9, 1), None),
        (date(2026, 9, 8), "2026-09-08"),
        (date(2026, 9, 15), "2026-09-15"),
    ]
    assert [change.state for change in asked] == [KNOWN, REVIEW, REVIEW]
    assert [change.state for change in answered] == [KNOWN, CLAIMED, CLAIMED]
    assert answered[1].label == "Saved; adds the due date and the note"
    assert answered[2].label == "Saved; adds the due date and the assigned date"
    assert kept == Kept(added=0, updated=1, unchanged=0)
    assert len(rows) == 1
    assert (rows[0].due_date, rows[0].assigned_on) == (date(2026, 9, 15), date(2026, 9, 14))
    assert rows[0].note == "Teacher instruction."
    assert rows[0].origins["due_date"] is SourceChannel.LMS
    assert rows[0].origins["note"] is SourceChannel.LMS
    assert [(said.asserted_value, said.seen_in) for said in claims] == [
        ("2026-09-01", DAY_HEADER),
        ("2026-09-08", DAY_HEADER),
        ("2026-09-15", OWN_LINE),
    ]
    assert after_restart == rows
    assert replayed == Kept(added=0, updated=0, unchanged=1)
    assert rows_after_replay == rows
    assert claims_after_replay == claims
    assert [(row.due_date, row.note) for row in untouched] == [(date(2026, 9, 1), None)]
    assert len(untouched_claims) == 1


MISSING_LINE = "09/09 Math - A: Homework: Practice Grade: Missing\n"
PAGE_CARD = "Tuesday 9/8/2026\nMath\nAssigned: Practice: (Due:09/10/2026)\nBring the packet.\n"


def test_each_field_keeps_the_channel_that_gave_it_whatever_the_order(
    tmp_path: pathlib.Path,
) -> None:
    """The email names an assignment and reports it missing; the portal's card dates it
    and carries the teacher's words. Pasted together in either order, or saved one after
    the other in either order, the dates and the note are the portal's, the report is the
    email's, and only the record's own origin says which named it first."""
    email_first = MISSING_LINE + "\n" + PAGE_CARD
    page_first = PAGE_CARD + "\n" + MISSING_LINE
    outcomes: dict[str, tuple[Assignment, list[SourceChannel], list[SourceChannel]]] = {}
    for name, texts in {
        "email first": [email_first],
        "page first": [page_first],
        "email, then the page": [MISSING_LINE, PAGE_CARD],
        "the page, then the email": [PAGE_CARD, MISSING_LINE],
    }.items():
        path = tmp_path / f"{slug(name)}.sqlite3"
        store = ProjectStateStore.open(path, fixture_clock())
        try:
            for text in texts:
                keep(readings(text), store)
        finally:
            store.close()
        store = ProjectStateStore.open(path, fixture_clock())
        try:
            row = store.all_assignments()[0]
            outcomes[name] = (
                row,
                [said.channel for said in store.deadline_records(row.assignment_id)],
                [report.channel for report in store.status_reports(row.assignment_id)],
            )
        finally:
            store.close()
    mixed = readings(email_first)[0]

    assert mixed.origin is SourceChannel.EMAIL
    assert mixed.origin_of("due_date") is SourceChannel.LMS
    assert mixed.origin_of("note") is SourceChannel.LMS
    for name, (row, claim_channels, report_channels) in outcomes.items():
        assert row.due_date == date(2026, 9, 10), name
        assert row.assigned_on == date(2026, 9, 8), name
        assert row.note == "Bring the packet.", name
        assert row.reported_submission_status == "missing", name
        assert row.origins["due_date"] is SourceChannel.LMS, name
        assert row.origins["assigned_on"] is SourceChannel.LMS, name
        assert row.origins["note"] is SourceChannel.LMS, name
        assert claim_channels == [SourceChannel.LMS], name
        assert report_channels == [SourceChannel.EMAIL], name
    assert outcomes["email first"][0].origins["record"] is SourceChannel.EMAIL
    assert outcomes["page first"][0].origins["record"] is SourceChannel.LMS
    assert outcomes["email, then the page"][0].origins["record"] is SourceChannel.EMAIL
    assert outcomes["the page, then the email"][0].origins["record"] is SourceChannel.LMS


def test_a_paste_matches_a_row_on_record_by_its_course_and_title(tmp_path: pathlib.Path) -> None:
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        seed = read_whole(FixtureSource(FIXTURES))
        store.put_on_record(seed.assignments, seed.claims)
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
    assert kept == Kept(added=0, updated=1, unchanged=0)
    assert len(after) == before
    assert essay_claims[-1].asserted_value == "2026-08-18"


def test_the_week_is_read_as_one_snapshot_of_the_record(tmp_path: pathlib.Path) -> None:
    """The week's rows and claims are read while the store is held, so a saving cannot land
    between the two reads: while the store is held elsewhere the reading waits, and comes
    whole once it is let go. Her page, the planner, and a plan's fingerprint all read
    the week this way."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        keep(readings(SAVED_WEEK), store)
        with ThreadPoolExecutor(max_workers=1) as pool:
            held = store.exclusively()
            held.__enter__()
            try:
                reading = pool.submit(read_week, store, store, date(2026, 8, 31))
                _, still_waiting = wait([reading], timeout=0.3)
            finally:
                held.__exit__(None, None, None)
            week = reading.result(timeout=10)
    finally:
        store.close()

    assert reading in still_waiting
    assert [item.title for item in week.assignments] == ["Weekly practice"]
    assert len(week.records[week.assignments[0].assignment_id]) == 1


def test_two_savings_of_one_text_at_once_add_nothing_twice(tmp_path: pathlib.Path) -> None:
    """Two savings released together, of a whole paste and of a new-work answer: the second
    finds what the first wrote."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    read = read_text(THREE_WEEKS, now=NOW, today=TODAY)
    released = threading.Barrier(2)

    def one_saving(_: int) -> Kept | Held | list[Change]:
        released.wait()
        return keep(read.items, store)

    def one_answer(_: int) -> Kept | Held | list[Change]:
        released.wait()
        return keep(readings(WEEK_TWO), store, occurrences={0: NEW_WORK})

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(one_saving, range(2)))
        rows = store.all_assignments()
        covers = {(item.course, item.title): item for item in rows}["08 Geometry", "Book Covers"]
        claims = store.deadline_records(covers.assignment_id)
        keep(readings(WEEK_ONE), store)
        with ThreadPoolExecutor(max_workers=2) as pool:
            answers = list(pool.map(one_answer, range(2)))
        practice = [row for row in store.all_assignments() if row.title == "Weekly practice"]
    finally:
        store.close()

    assert sorted(count.added for count in counts if isinstance(count, Kept)) == [0, 12]
    assert len(rows) == 12
    assert len(claims) == 2
    assert sorted(count.added for count in answers if isinstance(count, Kept)) == [0, 1]
    assert len(practice) == 2
    assert len({row.assignment_id for row in practice}) == 2


def test_the_review_is_grouped_by_school_week_with_the_undated_last(
    tmp_path: pathlib.Path,
) -> None:
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        read = read_text(THREE_WEEKS + EMAIL, now=NOW, today=TODAY)
        weeks = by_week(changes_for(read.items, store))
    finally:
        store.close()

    assert [week.label for week in weeks] == [
        "Week of Monday, August 31, 2026",
        "Week of Monday, September 7, 2026",
        "Week of Monday, September 14, 2026",
        "No due date yet",
    ]
    assert [len(week.changes) for week in weeks] == [2, 9, 1, 1]
    assert weeks[0].count(NEW) == 2
    assert [change.reading.due_date for change in weeks[1].changes] == sorted(
        change.reading.due_date or date.max for change in weeks[1].changes
    )
    assert weeks[3].changes[0].reading.title == "Fraction practice"
