"""Reading the school's text: the weekly summary, the homework page, and the email.

The text here is synthetic, in the portal's shapes: a fictional first name in
the heading, ordinary course names, and made-up titles with the portal's
punctuation. Nothing from a real family appears in it. The three-week summary
has the structure of a real download, hyphen bullets included.
"""

import pathlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime

from blossom.intake import (
    CLAIMED,
    DAY_HEADER,
    EMAIL_DATE_LINE,
    KNOWN,
    NEW,
    NEW_WORK,
    OWN_LINE,
    PASTE_DAY,
    REVIEW,
    UPDATE,
    Change,
    Kept,
    Reading,
    by_hand,
    by_week,
    changes_for,
    identity,
    keep,
    kind_of,
    read_text,
    slug,
    spoken_report,
    within_a_school_year,
)
from blossom.reconciliation import SourceChannel
from blossom.sources import FixtureSource, read_whole
from blossom.stores.project_state import AssignmentKind, ProjectStateStore
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


def test_the_heading_under_a_card_is_the_teachers_words_and_a_long_paste_is_quick() -> None:
    text = """Homework for Wren

- 09/03/2026 - Thursday
08 Geometry - Assigned: Book Covers: (Due:09/08/2026)
Homework for tomorrow: cover both books with paper.
"""
    read = read_text(text, now=NOW, today=TODAY)
    started = datetime.now(UTC)
    many = read_text("\n".join(["A stray line"] * 20_000), now=NOW, today=TODAY)
    took = datetime.now(UTC) - started

    assert read.unread == ()
    assert by_pair(read.items)["08 Geometry", "Book Covers"].note == (
        "Homework for tomorrow: cover both books with paper."
    )
    assert len(many.unread) == 20_000
    assert took.total_seconds() < 5


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


def test_the_type_is_a_task_only_for_paperwork_and_materials() -> None:
    assert kind_of("Syllabus- Signed") is AssignmentKind.TASK
    assert kind_of("Book Covers") is AssignmentKind.TASK
    assert kind_of("Binder, labeled dividers and lined paper check") is AssignmentKind.TASK
    assert kind_of("Signing the form") is AssignmentKind.TASK
    assert kind_of("Covering a book") is AssignmentKind.TASK
    assert kind_of("Bringing materials") is AssignmentKind.TASK
    assert kind_of("Signed numbers practice") is AssignmentKind.HOMEWORK
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


def test_repeated_work_under_one_name_is_a_question_and_the_answer_is_kept(
    tmp_path: pathlib.Path,
) -> None:
    """A weekly practice due a week after the saved one is not merged in silence: the parent
    says whether it is the same assignment moved or new work, and the next paste of the new
    week finds the new row."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        first = read_text(
            "Homework for Wren\n- 09/07/2026 - Monday\nMath - Due: Weekly practice:\n",
            now=NOW,
            today=TODAY,
        )
        keep(first.items, store)
        next_week = read_text(
            "Homework for Wren\n- 09/14/2026 - Monday\nMath - Due: Weekly practice:\n",
            now=NOW,
            today=TODAY,
        )
        asked = changes_for(next_week.items, store)
        unanswered = keep(next_week.items, store)
        as_update = changes_for(next_week.items, store, occurrences={0: UPDATE})
        kept_new = keep(next_week.items, store, occurrences={0: NEW_WORK})
        rows = store.all_assignments()
        again = changes_for(next_week.items, store)
        moved_a_day = read_text(
            "Homework for Wren\n- 09/08/2026 - Tuesday\nMath - Due: Weekly practice:\n",
            now=NOW,
            today=TODAY,
        )
        nudged = changes_for(moved_a_day.items, store)
    finally:
        store.close()

    assert [change.state for change in asked] == [REVIEW]
    assert asked[0].label == "Needs your answer"
    assert isinstance(unanswered, list)
    assert [change.state for change in as_update] == [CLAIMED]
    assert kept_new == Kept(added=1, updated=0, unchanged=0)
    assert len(rows) == 2
    assert len({row.assignment_id for row in rows}) == 2
    assert [change.state for change in again] == [KNOWN]
    assert [change.state for change in nudged] == [CLAIMED]
    assert nudged[0].saved_due == date(2026, 9, 7)


def test_a_paste_matches_a_row_on_record_by_its_course_and_title(tmp_path: pathlib.Path) -> None:
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
    assert kept == Kept(added=0, updated=1, unchanged=0)
    assert len(after) == before
    assert essay_claims[-1].asserted_value == "2026-08-18"


def test_two_savings_of_one_text_at_once_add_nothing_twice(tmp_path: pathlib.Path) -> None:
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    read = read_text(THREE_WEEKS, now=NOW, today=TODAY)
    released = threading.Barrier(2)

    def one_saving(_: int) -> Kept | list[Change]:
        released.wait()
        return keep(read.items, store)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(one_saving, range(2)))
        rows = store.all_assignments()
        covers = {(item.course, item.title): item for item in rows}["08 Geometry", "Book Covers"]
        claims = store.deadline_records(covers.assignment_id)
    finally:
        store.close()

    assert sorted(count.added for count in counts if isinstance(count, Kept)) == [0, 12]
    assert len(rows) == 12
    assert len(claims) == 2


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
