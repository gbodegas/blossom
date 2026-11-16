# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Reading the school's text: the weekly summary, the homework page, and the email.

The text here is synthetic, in the portal's shapes: a fictional first name in
the heading, ordinary course names, and made-up titles with the portal's
punctuation. Nothing from a real family appears in it. The three-week summary
has the structure of a real download, hyphen bullets included.
"""

import dataclasses
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
    ChangedSinceShown,
    IdentityAnswer,
    Kept,
    NotAsked,
    NotOffered,
    PortalCard,
    Reading,
    by_hand,
    by_week,
    changes_for,
    conflicting_choices,
    identity,
    keep,
    kind_of,
    read_cards,
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


def words(item: Reading) -> tuple[str, ...]:
    """The school's instructions a reading carries, by their words, in the order read."""
    return tuple(said.text for said in item.instructions)


def applying(store: ProjectStateStore, assignment_id: str) -> tuple[str, ...]:
    """The words of the school's instructions that apply to an assignment on record."""
    found = store.school_instruction_readings([assignment_id]).readable.get(assignment_id)
    return () if found is None else found.texts


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
    notebook = [item for item in read.items if any("math notebook" in w for w in words(item))]
    assert [item.title for item in notebook] == ["PR1 U2.3 Pg 40 #1-9"]
    syllabus = [item for item in read.items if any("syllabus" in w for w in words(item))]
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
        assert [(item.pair, item.due_date, words(item)) for item in other.items] == [
            (item.pair, item.due_date, words(item)) for item in plain.items
        ]
    assert words(kept.items[0]) == ("Use C:\\\\art\\\\poster.",)


def test_the_homework_page_is_read_with_the_course_on_its_own_line() -> None:
    read = read_text(PAGE, now=NOW, today=TODAY)
    items = by_pair(read.items)

    assert read.unread == ()
    assert set(items) == {("08 Geometry", "Book Covers"), ("Humanities", "Reading check")}
    covers = items["08 Geometry", "Book Covers"]
    assert covers.assigned_on == date(2026, 9, 1)
    assert words(covers) == ("Cover both books with paper.",)
    assert covers.note is None
    assert [said.seen_in for said in covers.claims] == [OWN_LINE, DAY_HEADER]
    assert words(items["Humanities", "Reading check"]) == ()


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
    assert words(items["Math", "Practice"]) == ()


def test_the_heading_under_a_card_is_the_teachers_words_and_a_long_paste_reads_whole() -> None:
    text = """Homework for Wren

- 09/03/2026 - Thursday
08 Geometry - Assigned: Book Covers: (Due:09/08/2026)
Homework for tomorrow: cover both books with paper.
"""
    read = read_text(text, now=NOW, today=TODAY)
    many = read_text("\n".join(["A stray line"] * 20_000), now=NOW, today=TODAY)

    assert read.unread == ()
    assert words(by_pair(read.items)["08 Geometry", "Book Covers"]) == (
        "Homework for tomorrow: cover both books with paper.",
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

    assert [(said.text, said.card, said.card_day) for said in read.items[0].instructions] == [
        ("Use the big paper.\nTwo colors at least.", "assigned", date(2026, 9, 8))
    ]


NOTES_WEEK = """Homework for Wren
11/02/2026 - Monday
08 Geometry - Assigned: Practice set 4: (Due:11/03/2026)
08 Geometry - Due: Textbook page 12:
Humanities -
Study for the vocabulary quiz on Thursday. Map project due on Wednesday.

Science -
Lab notebook due Tuesday

Spanish - Assigned: Oral quiz: (Due:11/10/2026)
Art -
Please bring a shoebox by next Tuesday.

11/03/2026 - Tuesday
08 Geometry -
Quiz on unit two

Sections 2 and 3

08 Geometry - Due: Practice set 4:
Humanities -
Study for the vocabulary quiz on Thursday. Map project due on Wednesday.

Religion - Due: Short essay:
Write 250 words on your question.

Science -
Reading log due today

Online practice due Tuesday

"""
"""A week of the summary as its print view writes it, with what teachers type under a
subject: the subject and a hyphen on a line of their own, then the note, a blank line after
each of its paragraphs. A card's own instruction sits under the card with no subject between."""


def test_a_subject_heading_ends_the_card_before_it_and_its_note_is_no_cards_words() -> None:
    """A teacher's note under another subject is never the instruction of the card above it,
    and a card keeps the instruction that is its own. No line of the text is dropped: each is
    the summary's heading, a day, a card, a card's own instruction, or listed for review."""
    read = read_text(NOTES_WEEK, now=NOW, today=TODAY)
    items = by_pair(read.items)
    written = [line for line in NOTES_WEEK.splitlines() if line.strip()]
    heading, days, cards, own_instruction = 1, 2, 5, 1

    assert set(items) == {
        ("08 Geometry", "Practice set 4"),
        ("08 Geometry", "Textbook page 12"),
        ("Spanish", "Oral quiz"),
        ("Religion", "Short essay"),
    }
    assert words(items["Religion", "Short essay"]) == ("Write 250 words on your question.",)
    assert words(items["08 Geometry", "Practice set 4"]) == ()
    assert words(items["08 Geometry", "Textbook page 12"]) == ()
    assert words(items["Spanish", "Oral quiz"]) == ()
    assert [(u.line, u.text) for u in read.unread] == [
        (5, "Humanities -"),
        (6, "Study for the vocabulary quiz on Thursday. Map project due on Wednesday."),
        (8, "Science -"),
        (9, "Lab notebook due Tuesday"),
        (12, "Art -"),
        (13, "Please bring a shoebox by next Tuesday."),
        (16, "08 Geometry -"),
        (17, "Quiz on unit two"),
        (19, "Sections 2 and 3"),
        (22, "Humanities -"),
        (23, "Study for the vocabulary quiz on Thursday. Map project due on Wednesday."),
        (28, "Science -"),
        (29, "Reading log due today"),
        (31, "Online practice due Tuesday"),
    ]
    assert len(written) == heading + days + cards + own_instruction + len(read.unread)


def test_a_subject_heading_right_under_a_cards_instruction_ends_it_there() -> None:
    text = """Homework for Wren
11/05/2026 - Thursday
Religion - Due: Short essay:
Write 250 words on your question.
Science -
Reading log due today
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert words(by_pair(read.items)["Religion", "Short essay"]) == (
        "Write 250 words on your question.",
    )
    assert [(u.line, u.text) for u in read.unread] == [
        (5, "Science -"),
        (6, "Reading log due today"),
    ]


def test_a_teachers_line_with_a_hyphen_in_it_stays_the_cards_words() -> None:
    """A hyphen inside a line, a line that is only a hyphen, and a long line that happens to
    end in one are a teacher's words: a subject heading is short, and ends in its hyphen."""
    long_line = "Finish the reading and bring the questions you could not answer to class -"
    text = f"""Homework for Wren
11/05/2026 - Thursday
Religion - Due: Short essay:
Pages 4 - 9 of the reader.
-
{long_line}
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert len(long_line) > 60
    assert read.unread == ()
    assert words(by_pair(read.items)["Religion", "Short essay"]) == (
        f"Pages 4 - 9 of the reader.\n-\n{long_line}",
    )


def test_under_the_homework_pages_day_a_hyphen_ended_line_is_read_as_before() -> None:
    """A subject heading is the summary's shape. Under a day written as the homework page
    writes it, weekday first, a line ending in a hyphen is read as it was before: here as the
    course on the line above a card, hyphen and all."""
    text = """Tuesday 9/1/2026
08 Geometry
Due: Book Covers:
Science -
Due: Lab report:
Bring the graph.
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [item.pair for item in read.items] == [
        ("08 Geometry", "Book Covers"),
        ("Science -", "Lab report"),
    ]
    assert words(read.items[1]) == ("Bring the graph.",)
    assert read.unread == ()


def test_the_lines_under_a_subject_heading_are_kept_whatever_they_look_like() -> None:
    """Under a subject heading, words the reader takes for the summary's heading, a mail
    header, a mail date line or the portal's own words elsewhere are a teacher's words: none
    is read as nothing, none dates a later report, and a line over a card's line names no
    course for it."""
    text = """Homework for Wren
11/05/2026 - Thursday
Religion - Due: Short essay:
Science -
Homework for tonight: finish the lab write-up
Mon, Nov 9, 2026 is the unit test
Subject: verbs
Classes
Lab report
Due: Friday
Include your graph.

09/09 Math - A: Homework: Practice Grade: Missing
"""
    read = read_text(text, now=NOW, today=TODAY)
    items = by_pair(read.items)

    assert set(items) == {("Religion", "Short essay"), ("Math", "Practice")}
    assert words(items["Religion", "Short essay"]) == ()
    assert [(u.line, u.text) for u in read.unread] == [
        (4, "Science -"),
        (5, "Homework for tonight: finish the lab write-up"),
        (6, "Mon, Nov 9, 2026 is the unit test"),
        (7, "Subject: verbs"),
        (8, "Classes"),
        (9, "Lab report"),
        (10, "Due: Friday"),
        (11, "Include your graph."),
    ]
    report = items["Math", "Practice"].reports[0]
    assert (report.reported_on, report.dated_by) == (TODAY, PASTE_DAY)


def test_a_day_a_card_and_a_missing_line_that_read_end_a_note_with_no_blank_between() -> None:
    """Right under a subject heading, with no blank line between, a day, a card with its
    course and a Missing line are read as themselves; a heading under a heading is kept for
    review like the one above it."""
    text = """Homework for Wren
11/05/2026 - Thursday
Science -
11/06/2026 - Friday
Art -
Spanish - Due: Quiz:
Science -
09/09 Math - A: Homework: Practice Grade: Missing
Art -
Humanities -
Read chapter two.
"""
    read = read_text(text, now=NOW, today=TODAY)
    items = by_pair(read.items)

    assert set(items) == {("Spanish", "Quiz"), ("Math", "Practice")}
    assert items["Spanish", "Quiz"].due_date == date(2026, 11, 6)
    assert [(u.line, u.text) for u in read.unread] == [
        (3, "Science -"),
        (5, "Art -"),
        (7, "Science -"),
        (9, "Art -"),
        (10, "Humanities -"),
        (11, "Read chapter two."),
    ]


UNDER_A_HEADING = """Homework for Wren
11/05/2026 - Thursday
Religion - Due: Short essay:
Science -
{line}
Subject: fractions review
Date: Nov 9, 2026
Homework for tonight is the lab

Math - Due: Sheet 4:
09/09 Math - A: Homework: Practice Grade: Missing
"""
"""A subject heading, one line to try right under it, then three lines the reader takes for
a mail header, a mail date line and the summary's heading where they stand outside a card;
after the blank, a card that tells which day is in force and a Missing line that tells which
day the reports are dated by."""

KEPT_UNDER_A_HEADING = [
    (6, "Subject: fractions review"),
    (7, "Date: Nov 9, 2026"),
    (8, "Homework for tonight is the lab"),
]


def after_the_blank(read: object) -> tuple[date | None, date, str]:
    """The due date of the card after the blank, and the day and source of the Missing
    report's date."""
    items = by_pair(read.items)  # type: ignore[attr-defined]
    report = items["Math", "Practice"].reports[0]
    return items["Math", "Sheet 4"].due_date, report.reported_on, report.dated_by


@pytest.mark.parametrize(
    "line",
    [
        "Homework for tonight -",
        "Date: Nov 9, 2026 -",
        "Subject: verbs -",
        "Due: Friday -",
        "Math - Due: Sheet 3 -",
        "11/09 Math: Homework: Sheet Grade: -",
    ],
)
def test_a_heading_shaped_line_under_a_heading_stays_with_the_note(line: str) -> None:
    """A line under a subject heading that is itself shaped as a heading is kept for review
    and the note goes on, whatever else it is shaped like and whatever words it begins with."""
    read = read_text(UNDER_A_HEADING.format(line=line), now=NOW, today=TODAY)

    assert [(u.line, u.text) for u in read.unread] == [
        (4, "Science -"),
        (5, line),
        *KEPT_UNDER_A_HEADING,
    ]
    assert words(by_pair(read.items)["Religion", "Short essay"]) == ()
    assert after_the_blank(read) == (date(2026, 11, 5), TODAY, PASTE_DAY)


@pytest.mark.parametrize(
    "line",
    [
        "Chapter 5 - Assigned: Worksheet: (Due:13/45/2026)",
        "Chapter 5 - Assigned: tomorrow",
        "Chapter 5 - Due:",
        "11/09 Science - A: Homework: Lab 3 Grade: 95",
        "11/09 Math - Due: Sheet 3: Homework: Lab Grade: 95",
    ],
)
def test_a_line_shaped_like_structure_that_does_not_read_stays_with_the_note(line: str) -> None:
    """Under a subject heading, a card with an impossible date, a body that is neither kind
    or no title, and a Missing-shaped line of another grade, the last also where it is shaped
    like a card, are kept for review. The note goes on and nothing else changes: the day in
    force stays, and no report is dated."""
    read = read_text(UNDER_A_HEADING.format(line=line), now=NOW, today=TODAY)

    assert [(u.line, u.text) for u in read.unread] == [
        (4, "Science -"),
        (5, line),
        *KEPT_UNDER_A_HEADING,
    ]
    assert after_the_blank(read) == (date(2026, 11, 5), TODAY, PASTE_DAY)


@pytest.mark.parametrize("line", ["Friday 13/45/2026", "13/45/2026 - Friday"])
def test_a_day_that_is_no_date_in_a_note_is_kept_and_ends_the_day_in_force(line: str) -> None:
    """A day line whose digits name no date is kept for review in a note, as anywhere, and
    as anywhere it ends the day in force: the due card after it takes no earlier day, so it
    is kept for review too, and the note goes on."""
    read = read_text(UNDER_A_HEADING.format(line=line), now=NOW, today=TODAY)
    items = by_pair(read.items)

    assert [(u.line, u.text) for u in read.unread] == [
        (4, "Science -"),
        (5, line),
        *KEPT_UNDER_A_HEADING,
        (10, "Math - Due: Sheet 4:"),
    ]
    assert set(items) == {("Religion", "Short essay"), ("Math", "Practice")}
    report = items["Math", "Practice"].reports[0]
    assert (report.reported_on, report.dated_by) == (TODAY, PASTE_DAY)


def test_a_due_card_under_a_heading_with_no_day_read_stays_with_the_note() -> None:
    text = """Homework for Wren
Science -
Chapter 5 - Due: Friday
Subject: fractions review
Date: Nov 9, 2026

09/09 Math - A: Homework: Practice Grade: Missing
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(u.line, u.text) for u in read.unread] == [
        (2, "Science -"),
        (3, "Chapter 5 - Due: Friday"),
        (4, "Subject: fractions review"),
        (5, "Date: Nov 9, 2026"),
    ]
    report = read.items[0].reports[0]
    assert (report.reported_on, report.dated_by) == (TODAY, PASTE_DAY)


@pytest.mark.parametrize(
    ("line", "card"),
    [
        ("Homework for tonight - Due: tomorrow", ("Homework for tonight", "tomorrow")),
        ("Date: Nov 9, 2026 - Due: quiz", ("Date: Nov 9, 2026", "quiz")),
        ("Subject: verbs - Due: quiz", ("Subject: verbs", "quiz")),
    ],
)
def test_a_card_with_its_course_under_a_heading_is_read_as_the_card_it_is_shaped_as(
    line: str, card: tuple[str, str]
) -> None:
    """A line under a subject heading in the summary's own card shape, under a day already
    read, is read as that card whatever words it begins with: it ends the note, and the lines
    after it are that card's instruction. None is read as nothing and no report is re-dated."""
    read = read_text(UNDER_A_HEADING.format(line=line), now=NOW, today=TODAY)
    items = by_pair(read.items)

    assert [(u.line, u.text) for u in read.unread] == [(4, "Science -")]
    assert items[card].due_date == date(2026, 11, 5)
    assert words(items[card]) == (
        "Subject: fractions review\nDate: Nov 9, 2026\nHomework for tonight is the lab",
    )
    assert after_the_blank(read) == (date(2026, 11, 5), TODAY, PASTE_DAY)


@pytest.mark.parametrize(
    "day_line", ["Friday 11/13/2026", "11/13/2026 - Friday", "- 11/13/2026 - Friday"]
)
def test_a_line_that_reads_as_a_day_under_a_heading_ends_the_note_and_is_the_day(
    day_line: str,
) -> None:
    text = f"""Homework for Wren
11/05/2026 - Thursday
Science -
Lab notes
{day_line}
Math - Due: Sheet 4:
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(item.pair, item.due_date) for item in read.items] == [
        (("Math", "Sheet 4"), date(2026, 11, 13))
    ]
    assert [(u.line, u.text) for u in read.unread] == [(3, "Science -"), (4, "Lab notes")]


def test_an_assigned_card_under_a_heading_reads_with_no_day_read_above() -> None:
    text = """Homework for Wren
Science -
Math - Assigned: Sheet 3: (Due:11/09/2026)
Show your work.
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(item.pair, item.due_date) for item in read.items] == [
        (("Math", "Sheet 3"), date(2026, 11, 9))
    ]
    assert words(read.items[0]) == ("Show your work.",)
    assert [(u.line, u.text) for u in read.unread] == [(2, "Science -")]


def test_a_note_is_kept_whole_across_its_blank_lines() -> None:
    """A teacher's note runs across its blank lines, and across a line holding only the
    download's line-break mark, until a line that reads as a day, a Missing line or a card
    with its course. Every line of it is kept for review whatever it begins with: none is
    read as the summary's heading, a mail header or a card with no course on its line."""
    text = """Homework for Wren
11/05/2026 - Thursday
Science -
Lab notes
\\
Subject: second part

Homework for tonight: third part
Still the third part

Due: Friday
Homework for the weekend: finish the graph
Religion - Due: Short essay:
Write 250 words.
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [item.pair for item in read.items] == [("Religion", "Short essay")]
    assert words(read.items[0]) == ("Write 250 words.",)
    assert [(u.line, u.text) for u in read.unread] == [
        (3, "Science -"),
        (4, "Lab notes"),
        (6, "Subject: second part"),
        (8, "Homework for tonight: third part"),
        (9, "Still the third part"),
        (11, "Due: Friday"),
        (12, "Homework for the weekend: finish the graph"),
    ]


def test_the_summarys_own_heading_over_its_next_week_ends_a_note() -> None:
    """The summary's heading, the line this text opened with, ends a note where a day line
    follows it, and is read as nothing. A teacher's line that only begins with the same two
    words stays with the note, even right before a day."""
    text = """Homework for Wren
11/05/2026 - Thursday
Science -
Lab notes
Homework for tonight: read chapter 4
Homework for Monday

11/06/2026 - Friday
Science -
More notes


Homework for Wren

11/09/2026 - Monday
Math - Due: Sheet 4:
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(item.pair, item.due_date) for item in read.items] == [
        (("Math", "Sheet 4"), date(2026, 11, 9))
    ]
    assert [(u.line, u.text) for u in read.unread] == [
        (3, "Science -"),
        (4, "Lab notes"),
        (5, "Homework for tonight: read chapter 4"),
        (6, "Homework for Monday"),
        (9, "Science -"),
        (10, "More notes"),
    ]


def test_only_the_line_a_text_opens_with_is_known_as_the_summarys_heading() -> None:
    """Inside a note, the line taken for the summary's own heading is the one the text opened
    with. A line elsewhere that is read as nothing for beginning with the same two words is
    not it, so its copy in a note is kept, even with a day line after it."""
    text = """11/05/2026 - Thursday
Homework for the weekend

Science -
Lab notes
Homework for the weekend
11/06/2026 - Friday
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert read.items == ()
    assert [(u.line, u.text) for u in read.unread] == [
        (4, "Science -"),
        (5, "Lab notes"),
        (6, "Homework for the weekend"),
    ]


def test_a_note_line_unlike_the_opening_heading_in_its_capitals_is_kept_for_review() -> None:
    """The summary's own heading is the line the text opened with, as read. A line in a note
    that equals it only when capitals are set aside is the teacher's: it is kept for review,
    even with a day line after it."""
    text = """Homework for Wren
11/05/2026 - Thursday
Math - Assigned: Sheet 3: (Due:11/06/2026)
Science -
HOMEWORK FOR WREN
11/06/2026 - Friday
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(item.pair, [seen.text for seen in item.instructions]) for item in read.items] == [
        (("Math", "Sheet 3"), [])
    ]
    assert [(u.line, u.text) for u in read.unread] == [
        (4, "Science -"),
        (5, "HOMEWORK FOR WREN"),
    ]


def test_an_impossible_day_in_a_note_can_end_the_page_shape_and_never_starts_it() -> None:
    """Inside a note a day line whose digits name no date is a teacher's line or part of a
    card's paragraph, so it never puts the homework page's rules in force. Written the page's
    way under the summary's day, it leaves the next subject's heading a heading, and that
    subject's words stay out of the card above. Written the summary's way under the homework
    page's day, it ends the page's shape, with the same result."""
    page_way = """Homework for Wren
11/05/2026 - Thursday
Religion - Due: Short essay:
Science -
Test moved to
Friday 11/31/2026
Spanish - Assigned: Oral quiz: (Due:11/10/2026)
Art -
Bring a shoebox
"""
    summary_way = """Homework for Wren
11/05/2026 - Thursday
Science -
Lab notes
Friday 11/06/2026
Due: Worksheet:
13/45/2026 - Monday

Math - Assigned: Sheet 3: (Due:11/10/2026)
Art -
Paint words
"""
    first = read_text(page_way, now=NOW, today=TODAY)
    second = read_text(summary_way, now=NOW, today=TODAY)

    assert [(item.pair, [seen.text for seen in item.instructions]) for item in first.items] == [
        (("Religion", "Short essay"), []),
        (("Spanish", "Oral quiz"), []),
    ]
    assert [(u.line, u.text) for u in first.unread] == [
        (4, "Science -"),
        (5, "Test moved to"),
        (6, "Friday 11/31/2026"),
        (8, "Art -"),
        (9, "Bring a shoebox"),
    ]
    assert [(item.pair, [seen.text for seen in item.instructions]) for item in second.items] == [
        (("Math", "Sheet 3"), [])
    ]
    assert [(u.line, u.text) for u in second.unread] == [
        (3, "Science -"),
        (4, "Lab notes"),
        (6, "Due: Worksheet:"),
        (7, "13/45/2026 - Monday"),
        (10, "Art -"),
        (11, "Paint words"),
    ]


def test_the_opening_heading_in_a_note_with_no_day_after_it_is_kept_for_review() -> None:
    """The line the text opened with ends a note only where a day line follows it. With a
    teacher's words after it instead, it is kept for review with them."""
    text = """Homework for Wren
11/05/2026 - Thursday
Science -
Lab notes
Homework for Wren
More notes
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert read.items == ()
    assert [(u.line, u.text) for u in read.unread] == [
        (3, "Science -"),
        (4, "Lab notes"),
        (5, "Homework for Wren"),
        (6, "More notes"),
    ]


def test_a_heading_in_a_due_cards_short_paragraph_opens_that_subjects_note() -> None:
    """Where a heading has been read, a due card's line with no course keeps the rest of its
    paragraph for review, a word that is read as nothing elsewhere included. A subject heading
    in that paragraph opens the subject's note, which a blank line does not end, so the same
    word after the blank is kept too."""
    text = """Homework for Wren
11/05/2026 - Thursday
Science -
Lab notes
Math - Assigned: Sheet 3: (Due:11/06/2026)
Due: Orphan:
Classes
Art -

Classes
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(item.pair, [seen.text for seen in item.instructions]) for item in read.items] == [
        (("Math", "Sheet 3"), [])
    ]
    assert [(u.line, u.text) for u in read.unread] == [
        (3, "Science -"),
        (4, "Lab notes"),
        (6, "Due: Orphan:"),
        (7, "Classes"),
        (8, "Art -"),
        (10, "Classes"),
    ]


def test_a_subject_heading_ends_the_course_in_force() -> None:
    """A course line read before a subject heading does not name the course of a card after
    that heading's note: a card's line with no course there is left for review."""
    text = """Homework for Wren
11/05/2026 - Thursday
Science
Due: Lab report:
Art -
Paint words
Math - Assigned: Sheet 3: (Due:11/06/2026)
Due: Poster:
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(item.pair, item.due_date) for item in read.items] == [
        (("Science", "Lab report"), date(2026, 11, 5)),
        (("Math", "Sheet 3"), date(2026, 11, 6)),
    ]
    assert [(u.line, u.text) for u in read.unread] == [
        (5, "Art -"),
        (6, "Paint words"),
        (8, "Due: Poster:"),
    ]


def test_a_cards_line_with_its_course_that_does_not_read_opens_no_short_paragraph() -> None:
    """The short review paragraph belongs to a card's line with no course. A card with its
    course on its line and a date that names no day is left for review alone, and the line
    after it is read as it is anywhere: here, as a word the portal prints, as nothing."""
    text = """Homework for Wren
11/05/2026 - Thursday
Science -
Lab notes
11/06/2026 - Friday
Math - Assigned: Sheet 3: (Due:13/45/2026)
Classes
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert read.items == ()
    assert [(u.line, u.text) for u in read.unread] == [
        (3, "Science -"),
        (4, "Lab notes"),
        (6, "Math - Assigned: Sheet 3: (Due:13/45/2026)"),
    ]


def test_the_opening_heading_before_a_day_that_is_no_date_stays_with_its_note() -> None:
    """The line the text opened with ends a note only before a day whose digits name a date.
    Before a day that names none it is kept for review, the note goes on, and the lines after
    it are neither read as nothing nor taken for the email's date."""
    text = """Homework for Wren
11/05/2026 - Thursday
Science -
Lab notes
Homework for Wren
13/45/2026 - Monday
Subject: verbs
Date: Nov 9, 2026
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert read.items == ()
    assert [(u.line, u.text) for u in read.unread] == [
        (3, "Science -"),
        (4, "Lab notes"),
        (5, "Homework for Wren"),
        (6, "13/45/2026 - Monday"),
        (7, "Subject: verbs"),
        (8, "Date: Nov 9, 2026"),
    ]


def test_the_opening_heading_is_known_in_a_note_as_the_reader_reads_lines() -> None:
    """The opening line is compared as every line is read, without the spaces around it or
    the download's line-break mark, and the day after it may come after blank lines."""
    text = """Homework for Wren
11/05/2026 - Thursday
Science -
Lab notes
   Homework for Wren  \\


11/06/2026 - Friday
Math - Due: Sheet 4:
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(item.pair, item.due_date) for item in read.items] == [
        (("Math", "Sheet 4"), date(2026, 11, 6))
    ]
    assert [(u.line, u.text) for u in read.unread] == [(3, "Science -"), (4, "Lab notes")]


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        (
            "Homework for Wren\n11/05/2026 - Thursday\nScience -\nHOMEWORK FOR WREN\n"
            "11/06/2026 - Friday\n",
            (4, "HOMEWORK FOR WREN"),
        ),
        (
            "Homework for Wren\n11/05/2026 - Thursday\nScience -\nLab notes\n"
            "Math - Assigned: Sheet 3: (Due:11/06/2026)\nDue: Orphan:\nHOMEWORK FOR WREN\n"
            "11/06/2026 - Friday\n",
            (7, "HOMEWORK FOR WREN"),
        ),
    ],
    ids=["a subject's note", "a card's short paragraph"],
)
def test_a_line_unlike_the_opening_heading_in_its_capitals_is_kept_in_either_note(
    text: str, kept: tuple[int, str]
) -> None:
    """In a subject's note and in a card's short review paragraph alike, a line right before a
    day that equals the opening heading only when capitals are set aside is kept for review."""
    read = read_text(text, now=NOW, today=TODAY)

    assert kept in [(u.line, u.text) for u in read.unread]


def test_a_heading_right_under_a_due_cards_line_keeps_the_word_after_the_blank() -> None:
    """A due card's line with no course, a subject heading right under it, a blank line, then
    a word the portal prints: the heading opened that subject's note, which the blank line
    does not end, so the word is kept for review."""
    text = """Homework for Wren
11/05/2026 - Thursday
Science -
Lab notes
Math - Assigned: Sheet 3: (Due:11/06/2026)
Due: Orphan:
Art -

Classes
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(u.line, u.text) for u in read.unread] == [
        (3, "Science -"),
        (4, "Lab notes"),
        (6, "Due: Orphan:"),
        (7, "Art -"),
        (9, "Classes"),
    ]


def test_a_line_that_ends_in_a_hyphen_with_no_space_before_it_is_no_heading() -> None:
    """A subject heading is set off from its hyphen by a space. A card's instruction line that
    ends in a hyphen right after a word stays that card's instruction."""
    text = """Homework for Wren
11/05/2026 - Thursday
Math - Assigned: Sheet 3: (Due:11/06/2026)
Finish the hand-
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(item.pair, [seen.text for seen in item.instructions]) for item in read.items] == [
        (("Math", "Sheet 3"), ["Finish the hand-"])
    ]
    assert read.unread == ()


def test_the_opening_heading_is_the_first_line_that_is_not_blank() -> None:
    """A text that begins with blank lines still opens with its summary heading, so the same
    line over the next week ends a note there too."""
    text = """

Homework for Wren
11/05/2026 - Thursday
Science -
Lab notes
Homework for Wren
11/06/2026 - Friday
Math - Due: Sheet 4:
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(item.pair, item.due_date) for item in read.items] == [
        (("Math", "Sheet 4"), date(2026, 11, 6))
    ]
    assert [(u.line, u.text) for u in read.unread] == [(5, "Science -"), (6, "Lab notes")]


def test_a_due_cards_line_with_no_course_opens_no_short_paragraph_where_no_heading_was_read() -> (
    None
):
    """With no subject heading read before it, a due card's line with no course is left for
    review alone, and the line after it is read as it is anywhere: here, as a word the portal
    prints, as nothing."""
    text = """Homework for Wren
11/05/2026 - Thursday
Due: Orphan:
Classes
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert read.items == ()
    assert [(u.line, u.text) for u in read.unread] == [(3, "Due: Orphan:")]


def test_a_due_card_with_its_course_and_no_day_read_opens_no_short_paragraph() -> None:
    """Where a heading has been read, a due card with its course on its line and no day in
    force is left for review alone, and the word after it is read as nothing, as anywhere."""
    text = """Homework for Wren
11/05/2026 - Thursday
Science -
Lab notes
11/06/2026 - Friday
13/45/2026 - Monday
Math - Due: Sheet 4:
Classes
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert read.items == ()
    assert [(u.line, u.text) for u in read.unread] == [
        (3, "Science -"),
        (4, "Lab notes"),
        (6, "13/45/2026 - Monday"),
        (7, "Math - Due: Sheet 4:"),
    ]


def test_an_impossible_day_written_the_pages_way_leaves_the_page_shape_in_force() -> None:
    """Under the homework page's day, a day line that names no date and is written the page's
    way, kept in a card's short review paragraph, leaves the page's shape in force: the
    hyphen-ended line after the next card stays that card's instruction, as on the page."""
    text = """Homework for Wren
11/05/2026 - Thursday
Science -
Lab notes
Friday 11/06/2026
Due: Worksheet:
Monday 13/45/2026

Math - Assigned: Sheet 3: (Due:11/10/2026)
Bring a ruler -
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(item.pair, [seen.text for seen in item.instructions]) for item in read.items] == [
        (("Math", "Sheet 3"), ["Bring a ruler -"])
    ]
    assert [(u.line, u.text) for u in read.unread] == [
        (3, "Science -"),
        (4, "Lab notes"),
        (6, "Due: Worksheet:"),
        (7, "Monday 13/45/2026"),
    ]


SUMMARY_WITH_NO_SUCH_DAY_UNDER_A_CARD = """Homework for Wren
11/05/2026 - Thursday
Religion - Due: Short essay:
Friday 11/31/2026
Spanish - Assigned: Oral quiz: (Due:11/10/2026)
Art -
Bring a shoebox
"""

SUMMARY_OPENING_WITH_NO_SUCH_DAY = """Homework for Wren
Friday 11/31/2026
Spanish - Assigned: Oral quiz: (Due:11/10/2026)
Art -
Bring a shoebox
"""


@pytest.mark.parametrize(
    ("text", "cards", "for_review"),
    [
        (
            SUMMARY_WITH_NO_SUCH_DAY_UNDER_A_CARD,
            [("Religion", "Short essay"), ("Spanish", "Oral quiz")],
            [(4, "Friday 11/31/2026"), (6, "Art -"), (7, "Bring a shoebox")],
        ),
        (
            SUMMARY_OPENING_WITH_NO_SUCH_DAY,
            [("Spanish", "Oral quiz")],
            [(2, "Friday 11/31/2026"), (4, "Art -"), (5, "Bring a shoebox")],
        ),
    ],
    ids=["under-a-card", "the-first-day-line"],
)
def test_an_impossible_day_outside_a_note_never_starts_the_page_shape(
    text: str, cards: list[tuple[str, str]], for_review: list[tuple[int, str]]
) -> None:
    """Outside a note, a day line written the homework page's way whose digits name no date
    puts no page rules in force. The next subject's heading stays a heading, so that
    subject's words are kept for review and no card takes them."""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(item.pair, [seen.text for seen in item.instructions]) for item in read.items] == [
        (pair, []) for pair in cards
    ]
    assert [(u.line, u.text) for u in read.unread] == for_review


@pytest.mark.parametrize(
    ("no_such_day", "instructions", "for_review"),
    [
        ("Friday 11/31/2026", ["Bring a ruler -"], [(4, "Friday 11/31/2026")]),
        ("13/45/2026 - Monday", [], [(4, "13/45/2026 - Monday"), (6, "Bring a ruler -")]),
    ],
    ids=["the-pages-way-leaves-it", "the-summarys-way-ends-it"],
)
def test_an_impossible_day_outside_a_note_under_the_page_shape(
    no_such_day: str, instructions: list[str], for_review: list[tuple[int, str]]
) -> None:
    """Under the homework page's day, a day line whose digits name no date, outside a note:
    written the page's way it leaves the page's shape in force, and the hyphen-ended line
    under the next card stays that card's instruction; written the summary's way it ends the
    page's shape, and that line is a heading kept for review."""
    text = f"""Homework for Wren
Thursday 11/05/2026
Math - Assigned: Sheet 3: (Due:11/10/2026)
{no_such_day}
Spanish - Assigned: Oral quiz: (Due:11/10/2026)
Bring a ruler -
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(item.pair, [seen.text for seen in item.instructions]) for item in read.items] == [
        (("Math", "Sheet 3"), []),
        (("Spanish", "Oral quiz"), instructions),
    ]
    assert [(u.line, u.text) for u in read.unread] == for_review


def test_a_page_day_that_names_no_date_leaves_its_cards_for_review_until_a_real_day() -> None:
    """A homework page whose day line names no date, read where the page's shape is not in
    force, is read under the summary's rules up to the page's next day that names a date: a
    hyphen-ended line under a card is a heading, and it and the lines after it, the page's
    own cards among them, are kept for review. Nothing is saved on another card and no line
    is dropped. The page's next real day puts its shape in force."""
    alone = """Friday 11/31/2026
Spanish
Assigned: Oral quiz: (Due:11/10/2026)
Bring a ruler -
Math
Assigned: Sheet 3: (Due:11/12/2026)
Use a pencil
Monday 11/09/2026
Art
Assigned: Collage: (Due:11/13/2026)
Bring glue -
"""
    after_a_summary = """Homework for Wren
11/05/2026 - Thursday
Religion - Due: Short essay:

Friday 11/31/2026
Spanish
Assigned: Oral quiz: (Due:11/10/2026)
Bring a ruler -
Math
Assigned: Sheet 3: (Due:11/12/2026)
Use a pencil
"""
    first = read_text(alone, now=NOW, today=TODAY)
    second = read_text(after_a_summary, now=NOW, today=TODAY)

    assert [(item.pair, [seen.text for seen in item.instructions]) for item in first.items] == [
        (("Spanish", "Oral quiz"), []),
        (("Art", "Collage"), ["Bring glue -"]),
    ]
    assert [(u.line, u.text) for u in first.unread] == [
        (1, "Friday 11/31/2026"),
        (4, "Bring a ruler -"),
        (5, "Math"),
        (6, "Assigned: Sheet 3: (Due:11/12/2026)"),
        (7, "Use a pencil"),
    ]
    assert [(item.pair, [seen.text for seen in item.instructions]) for item in second.items] == [
        (("Religion", "Short essay"), []),
        (("Spanish", "Oral quiz"), []),
    ]
    assert [(u.line, u.text) for u in second.unread] == [
        (5, "Friday 11/31/2026"),
        (8, "Bring a ruler -"),
        (9, "Math"),
        (10, "Assigned: Sheet 3: (Due:11/12/2026)"),
        (11, "Use a pencil"),
    ]


def test_under_the_summarys_days_a_heading_shaped_line_is_not_read_as_plain_text() -> None:
    """Where no homework-page day is in force, a line in a subject heading's shape that begins
    with a mail word or the summary's heading words is a heading, unless it is also shaped as
    a card: it is kept for review with its note, and dates no report."""
    text = """Date: Nov 9, 2026 -
Bring the form.

Homework for tonight -
Finish the lab.

09/09 Math - A: Homework: Practice Grade: Missing
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(u.line, u.text) for u in read.unread] == [
        (1, "Date: Nov 9, 2026 -"),
        (2, "Bring the form."),
        (4, "Homework for tonight -"),
        (5, "Finish the lab."),
    ]
    report = read.items[0].reports[0]
    assert (report.reported_on, report.dated_by) == (TODAY, PASTE_DAY)


def test_under_the_homework_pages_day_a_heading_shaped_mail_line_is_read_as_nothing() -> None:
    """Under a day written as the homework page writes it, a hyphen-ended line is no heading,
    so one that begins with a mail word is read as nothing there, as any such line is."""
    text = """Tuesday 9/1/2026
Subject: verbs -
plain words
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert read.items == ()
    assert [(u.line, u.text) for u in read.unread] == [(3, "plain words")]


def test_a_line_shaped_as_a_heading_and_as_a_card_is_read_as_the_card() -> None:
    """Outside a note, under a day already read, a line that ends in a hyphen and is also in
    the summary's card shape is read as that card, whatever words it begins with, and the
    line after it is the card's instruction."""
    text = """11/05/2026 - Thursday
Homework for tonight - Due: sheet -
words
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(item.pair, item.due_date) for item in read.items] == [
        (("Homework for tonight", "sheet -"), date(2026, 11, 5))
    ]
    assert words(read.items[0]) == ("words",)
    assert read.unread == ()


def test_under_a_page_day_a_hyphen_ended_line_in_a_short_note_is_no_heading() -> None:
    """A subject's note under the summary's day, then a day as the homework page writes it:
    there a card's line with no course opens its short note, a hyphen-ended line in it is
    kept for review as any other line, and the note ends at its blank line, so the page's
    course and card after it are read."""
    text = """Homework for Wren
11/05/2026 - Thursday
Science -
Bring goggles
Tuesday 9/1/2026
Due: Orphan:
Topic -

08 Geometry
Due: Book Covers:
Cover them.
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert [(item.pair, item.due_date) for item in read.items] == [
        (("08 Geometry", "Book Covers"), date(2026, 9, 1))
    ]
    assert words(read.items[0]) == ("Cover them.",)
    assert [(u.line, u.text) for u in read.unread] == [
        (3, "Science -"),
        (4, "Bring goggles"),
        (6, "Due: Orphan:"),
        (7, "Topic -"),
    ]


AFTER_A_NOTE_ENDED = """Homework for Wren
11/05/2026 - Thursday
{note}Religion - Due: Short essay:

Assigned: Poster: (Due:11/09/2026)
Subject: bring colors
Classes
Date: Nov 9, 2026

09/09 Math - A: Homework: Practice Grade: Missing
"""
"""A card's line with no course on it and no course above it, then three lines the reader
takes for a mail header, the portal's own word and a mail date line where they stand outside
a card; with or without a subject's note earlier in the text."""


def test_after_a_note_a_card_line_with_no_course_keeps_its_paragraph_for_review() -> None:
    """Where a subject heading has been read, a card's line left for review for want of a
    course opens a short note of its own: the lines after it, up to the blank line, are kept
    for review, none read as nothing and none dating the reports."""
    read = read_text(AFTER_A_NOTE_ENDED.format(note="Science -\nLab notes\n"), now=NOW, today=TODAY)
    items = by_pair(read.items)

    assert [(u.line, u.text) for u in read.unread] == [
        (3, "Science -"),
        (4, "Lab notes"),
        (7, "Assigned: Poster: (Due:11/09/2026)"),
        (8, "Subject: bring colors"),
        (9, "Classes"),
        (10, "Date: Nov 9, 2026"),
    ]
    assert words(items["Religion", "Short essay"]) == ()
    report = items["Math", "Practice"].reports[0]
    assert (report.reported_on, report.dated_by) == (TODAY, PASTE_DAY)


def test_an_email_date_line_kept_in_a_note_ends_the_earlier_emails_date() -> None:
    """Where a note keeps a later email's date line for review, the reports after it are dated
    by the day of the paste and say so, as with no email before it, never by an earlier email's
    date line."""
    text = """Date: Tue, Sep 1, 2026
Assignments:
08/28 Math - A: Homework: Practice Grade: Missing

Homework for Wren
09/09/2026 - Wednesday
Science -
Lab notes

Date: Thu, Sep 10, 2026
Assignments:
09/09 Art - A: Homework: Collage Grade: Missing
"""
    read = read_text(text, now=NOW, today=TODAY)
    items = by_pair(read.items)
    first = items["Math", "Practice"].reports[0]
    second = items["Art", "Collage"].reports[0]

    assert (first.reported_on, first.dated_by) == (date(2026, 9, 1), EMAIL_DATE_LINE)
    assert (second.reported_on, second.dated_by) == (TODAY, PASTE_DAY)
    assert [(u.line, u.text) for u in read.unread] == [
        (7, "Science -"),
        (8, "Lab notes"),
        (10, "Date: Thu, Sep 10, 2026"),
        (11, "Assignments:"),
    ]


@pytest.mark.parametrize(
    ("between", "kept"),
    [
        ("", 5),
        ("Homework for Wren\n09/09/2026 - Wednesday\n", 7),
    ],
    ids=["right-after-the-first-email", "under-the-summarys-day"],
)
def test_an_email_date_line_shaped_as_a_heading_ends_the_earlier_emails_date(
    between: str, kept: int
) -> None:
    """A later email's date line that ends in a hyphen set off by a space is read as a heading
    and kept for review with its note. The reports after it are dated by the day of the paste
    and say so, never by an earlier email's date line."""
    text = f"""Date: Tue, Sep 1, 2026
Assignments:
08/28 Math - A: Homework: Practice Grade: Missing

{between}Date: Thu, Sep 10, 2026 -
Assignments:
09/09 Art - A: Homework: Collage Grade: Missing
"""
    read = read_text(text, now=NOW, today=TODAY)
    items = by_pair(read.items)
    first = items["Math", "Practice"].reports[0]
    second = items["Art", "Collage"].reports[0]

    assert (first.reported_on, first.dated_by) == (date(2026, 9, 1), EMAIL_DATE_LINE)
    assert (second.reported_on, second.dated_by) == (TODAY, PASTE_DAY)
    assert [(u.line, u.text) for u in read.unread] == [
        (kept, "Date: Thu, Sep 10, 2026 -"),
        (kept + 1, "Assignments:"),
    ]


def test_a_text_with_no_subject_heading_is_read_by_the_other_rules_alone() -> None:
    """The same text with no subject heading in it is read by the other rules alone: the
    card's line with no course is kept for review, the mail header and the portal's own word
    are read as nothing, and the mail date line dates the report."""
    read = read_text(AFTER_A_NOTE_ENDED.format(note=""), now=NOW, today=TODAY)
    items = by_pair(read.items)

    assert [(u.line, u.text) for u in read.unread] == [(5, "Assigned: Poster: (Due:11/09/2026)")]
    report = items["Math", "Practice"].reports[0]
    assert (report.reported_on, report.dated_by) == (date(2026, 11, 9), EMAIL_DATE_LINE)


def test_a_subject_heading_is_short_and_has_a_word_before_its_hyphen() -> None:
    """Sixty characters is a heading and sixty-one is a teacher's line; a hyphen after a
    bullet, another hyphen or underscores, with no letter or digit before it, is no heading."""
    sixty = "A" * 58 + " -"
    longer = "A" * 59 + " -"
    under = "Homework for Wren\n11/05/2026 - Thursday\nReligion - Due: Short essay:\n"

    short = read_text(under + sixty + "\n", now=NOW, today=TODAY)
    long = read_text(under + longer + "\n", now=NOW, today=TODAY)
    marks = read_text(under + "* -\n- -\n___ -\n", now=NOW, today=TODAY)

    assert (len(sixty), len(longer)) == (60, 61)
    assert [(u.line, u.text) for u in short.unread] == [(4, sixty)]
    assert words(short.items[0]) == ()
    assert long.unread == ()
    assert words(long.items[0]) == (longer,)
    assert marks.unread == ()
    assert words(marks.items[0]) == ("* -\n- -\n___ -",)


def test_a_short_line_of_a_cards_instruction_that_ends_in_a_hyphen_goes_to_review() -> None:
    """A teacher's short line that ends in a hyphen set off by a space reads as a subject
    heading, so it and the rest of its paragraph are shown for review and not kept as the
    card's words: nothing is dropped, and the lines before it stay the card's."""
    text = """Homework for Wren
11/05/2026 - Thursday
Religion - Due: Short essay:
Write 250 words.
Choose one question -
Why do people tell stories?
What makes a rule fair?
"""
    read = read_text(text, now=NOW, today=TODAY)

    assert words(read.items[0]) == ("Write 250 words.",)
    assert [(u.line, u.text) for u in read.unread] == [
        (5, "Choose one question -"),
        (6, "Why do people tell stories?"),
        (7, "What makes a rule fair?"),
    ]


NOTE_HEAD = "Homework for Wren\n11/02/2026 - Monday\nScience -\nLab notes\n"
"""The summary's opening, a day, and a subject's note of one line, on lines 3 and 4."""
NOTE_DAY = date(2026, 11, 2)
SCIENCE_NOTE = ("Science", NOTE_DAY, ("Lab notes",), 3, (3, 4), False)
"""The block read from the note in ``NOTE_HEAD``, as ``blocks`` lists it."""


def blocks(text: str) -> list[tuple[str, date, tuple[str, ...], int, tuple[int, ...], bool]]:
    """Each subject's note read from a text, in the order the notes open: its subject, its
    school day, its paragraphs, its heading's line, its lines, and whether it is suspect.

    For every block it holds that each of its lines is also listed for review, that no line
    belongs to two blocks, that its lines ascend from its heading's, and that its paragraphs
    are the review text of its lines after the heading, in order."""
    read = read_text(text, now=NOW, today=TODAY)
    listed = {unread.line: unread.text for unread in read.unread}
    in_blocks = [number for note in read.notes for number in note.lines]
    assert set(in_blocks) <= set(listed)
    assert len(in_blocks) == len(set(in_blocks))
    for note in read.notes:
        assert note.lines == tuple(sorted(note.lines))
        assert note.lines[0] == note.block.line
        assert [line for paragraph in note.block.paragraphs for line in paragraph.split("\n")] == [
            listed[number] for number in note.lines[1:]
        ]
    return [
        (
            note.block.subject,
            note.block.day,
            note.block.paragraphs,
            note.block.line,
            note.lines,
            note.block.suspect,
        )
        for note in read.notes
    ]


def test_each_subjects_note_in_a_week_is_handed_out_as_a_block() -> None:
    """Each note of the week is one block: the subject without its hyphen, the school day
    its heading is under, its paragraphs, its heading's line, and the lines of the heading
    and of the note. A card and a card's own instruction are in no block, and none of these
    plain notes is suspect."""
    monday, tuesday = date(2026, 11, 2), date(2026, 11, 3)
    study = "Study for the vocabulary quiz on Thursday. Map project due on Wednesday."
    logs = ("Reading log due today", "Online practice due Tuesday")

    assert blocks(NOTES_WEEK) == [
        ("Humanities", monday, (study,), 5, (5, 6), False),
        ("Science", monday, ("Lab notebook due Tuesday",), 8, (8, 9), False),
        ("Art", monday, ("Please bring a shoebox by next Tuesday.",), 12, (12, 13), False),
        ("08 Geometry", tuesday, ("Quiz on unit two", "Sections 2 and 3"), 16, (16, 17, 19), False),
        ("Humanities", tuesday, (study,), 22, (22, 23), False),
        ("Science", tuesday, logs, 28, (28, 29, 31), False),
    ]


def test_a_note_of_several_paragraphs_is_one_block_with_its_paragraphs_apart() -> None:
    """A blank line in a note ends a paragraph, and several blank lines are one break. The
    lines of a paragraph are joined by a line break, each as the reader reads it: without
    the spaces around it and without the download's line-break mark. A blank line right
    under the heading opens no paragraph."""
    text = (
        "Homework for Wren\n11/02/2026 - Monday\nScience -\n\n"
        "Read chapter 4\\\n   and answer the questions.  \n\n\n"
        "Bring your lab notebook.\n\nQuiz on Friday.\n"
    )
    paragraphs = (
        "Read chapter 4\nand answer the questions.",
        "Bring your lab notebook.",
        "Quiz on Friday.",
    )

    assert blocks(text) == [("Science", NOTE_DAY, paragraphs, 3, (3, 5, 6, 9, 11), False)]


@pytest.mark.parametrize(
    "closing",
    [
        "11/03/2026 - Tuesday\n",
        "Tuesday 11/3/2026\n",
        "09/09 Math - A: Homework: Practice Grade: Missing\n",
        "Math - Assigned: Sheet 3: (Due:11/06/2026)\n",
        "Math - Due: Sheet 3:\n",
        "Homework for Wren\n11/09/2026 - Monday\n",
        "",
    ],
)
def test_a_note_closes_where_the_reader_ends_it_and_the_closing_line_is_in_no_block(
    closing: str,
) -> None:
    """A line that reads as a day in either shape, as a Missing line or as a card with its
    course, the text's opening line before a day whose digits name a date, and the end of
    the text each close a note. The closing line and the line after it are in no block."""
    after = "After words\n" if closing else ""

    assert blocks(NOTE_HEAD + closing + after) == [SCIENCE_NOTE]


def test_a_heading_inside_a_note_closes_it_and_opens_the_next_subjects_block() -> None:
    """A subject heading read inside a note ends that note and opens the next subject's,
    with a blank line before it or with none. Each note is a block of its own, and the
    heading is a line of the block it opens only."""
    text = NOTE_HEAD + "Art -\nBring a shoebox.\n\n08 Geometry -\nQuiz on unit two\n"

    assert blocks(text) == [
        SCIENCE_NOTE,
        ("Art", NOTE_DAY, ("Bring a shoebox.",), 5, (5, 6), False),
        ("08 Geometry", NOTE_DAY, ("Quiz on unit two",), 8, (8, 9), False),
    ]


def test_a_heading_with_no_line_kept_under_it_makes_no_block() -> None:
    """A heading followed at once by another heading, by a line that ends its note, or by
    the end of the text has no note to hand out. No block is made for it, and the heading
    is listed for review."""
    text = NOTE_HEAD + "Art -\nMusic -\n\n11/03/2026 - Tuesday\nSpanish -\n"
    read = read_text(text, now=NOW, today=TODAY)

    assert blocks(text) == [SCIENCE_NOTE]
    assert [(u.line, u.text) for u in read.unread][2:] == [
        (5, "Art -"),
        (6, "Music -"),
        (9, "Spanish -"),
    ]


def test_a_cards_short_review_paragraph_is_a_block_only_from_a_heading_inside_it() -> None:
    """The paragraph kept for review under a card's line with no course has no subject, so
    no block is made for it. A subject heading inside it opens that subject's note, a block
    like any other, which runs across the blank line; the paragraph's lines before the
    heading are in no block."""
    short = NOTE_HEAD + "Math - Assigned: Sheet 3: (Due:11/06/2026)\nDue: Orphan:\nClasses\n"

    assert blocks(short + "\nPaint words\n") == [SCIENCE_NOTE]
    assert blocks(short + "Art -\n\nPaint words\n") == [
        SCIENCE_NOTE,
        ("Art", NOTE_DAY, ("Paint words",), 8, (8, 10), False),
    ]


@pytest.mark.parametrize(
    "line",
    [
        "13/45/2026 - Monday",
        "Monday 13/45/2026",
        "09/09 Math - A: Homework: Practice Grade: 95",
        "Math - Assigned: Sheet 3: (Due:13/45/2026)",
        "Math - Assigned: Sheet 3",
        "Due: Orphan:",
        "Assigned: Poster: (Due:11/09/2026)",
    ],
)
def test_a_line_kept_in_a_note_only_because_it_did_not_read_makes_its_block_suspect(
    line: str,
) -> None:
    """A day in either shape whose digits name no date, a Missing-shaped line with another
    grade, a card with its course that does not read, and a page card's line with no course
    are kept in the note, in its paragraph, and mark its block suspect. The block keeps the
    school day its heading was read under."""
    assert blocks(NOTE_HEAD + line + "\nMore words\n") == [
        ("Science", NOTE_DAY, (f"Lab notes\n{line}\nMore words",), 3, (3, 4, 5, 6), True)
    ]


def test_a_heading_inside_a_note_that_is_also_shaped_as_a_card_opens_a_suspect_block() -> None:
    """Inside a note a short line that ends in a hyphen set off by a space is a heading,
    even when it is shaped as a card. The block it opens takes the line without its closing
    hyphen as its subject and is suspect; the note before it is not."""
    assert blocks(NOTE_HEAD + "Math - Due: Sheet -\nMore words\n") == [
        SCIENCE_NOTE,
        ("Math - Due: Sheet", NOTE_DAY, ("More words",), 5, (5, 6), True),
    ]


def test_a_heading_read_with_no_day_in_force_makes_no_block_and_stays_for_review() -> None:
    """A heading read before any day, or after a day in a note whose digits name no date,
    has no school day to be listed under. No block is made for its note, and the heading and
    the note's lines are listed for review."""
    before_any_day = "Homework for Wren\nScience -\nLab notes\n"
    after_no_date = NOTE_HEAD + "13/45/2026 - Monday\nArt -\nPaint words\n"
    first = read_text(before_any_day, now=NOW, today=TODAY)
    second = read_text(after_no_date, now=NOW, today=TODAY)

    assert first.notes == ()
    assert [(u.line, u.text) for u in first.unread] == [(2, "Science -"), (3, "Lab notes")]
    assert blocks(after_no_date) == [
        ("Science", NOTE_DAY, ("Lab notes\n13/45/2026 - Monday",), 3, (3, 4, 5), True)
    ]
    assert [(u.line, u.text) for u in second.unread][-2:] == [(6, "Art -"), (7, "Paint words")]


def test_nothing_under_a_homework_page_day_is_a_block() -> None:
    """Under a day written as the homework page writes it no heading is read, so a line that
    ends in a hyphen and the lines after it make no block, outside a card's short review
    paragraph or inside one. A note read under the summary's day earlier in the same text is
    a block all the same."""
    page = "Tuesday 11/3/2026\nArt -\nPaint words\nDue: Orphan:\nMusic -\nSing words\n"

    assert blocks(page) == []
    assert blocks(NOTE_HEAD + page) == [SCIENCE_NOTE]


def test_a_text_with_no_subject_heading_has_no_blocks() -> None:
    """The summary, the homework page and the email with no subject heading in them, and a
    card's line with no course in such a text, hand out no block."""
    no_note = AFTER_A_NOTE_ENDED.format(note="")

    for text in (THREE_WEEKS, PAGE, EMAIL, SAVED_WEEK, PORTAL_WEEK, no_note):
        assert blocks(text) == []


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
    assert words(read_text(under_a_card, now=NOW, today=TODAY).items[0]) == ("Date: Sep 8, 2026",)
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
    school's words are kept as its instructions, apart from the note, and a different one
    asks which apply and saves nothing until answered; a parent's typed note fills the note
    and leaves the school's instructions as they are; a parent's type is theirs."""
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
        after_page = applying(store, sketch.assignment_id)
        revised = read_text(
            "Tuesday 9/8/2026\nArt\nAssigned: Sketchbook, three pages: (Due:09/18/2026)\n"
            "Pencil or ink.\n",
            now=NOW,
            today=TODAY,
        )
        updated_note = changes_for(revised.items, store)
        unanswered = keep(revised.items, store)
        after_unanswered = applying(store, sketch.assignment_id)
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
        at_the_end = applying(store, sketch.assignment_id)
    finally:
        store.close()

    assert (
        filled[0].label
        == "Saved; adds the due date, the assigned date and the school's instructions"
    )
    assert "The record has no due date of its own" in filled[0].effect
    assert sketch.due_date == date(2026, 9, 18)
    assert sketch.assigned_on == date(2026, 9, 8)
    assert sketch.note is None
    assert after_page == ("Pencil only.",)
    assert sketch.origins["record"] is SourceChannel.PARENT_ENTRY
    assert sketch.origins["due_date"] is SourceChannel.LMS
    assert "note" not in sketch.origins
    assert sketch.origins["assigned_on"] is SourceChannel.LMS
    assert updated_note[0].state == REVIEW
    assert updated_note[0].instructions_asked
    assert updated_note[0].note_change is None
    assert "differ from what is saved" in updated_note[0].effect
    assert isinstance(unanswered, list)
    assert after_unanswered == ("Pencil only.",)
    assert replaces[0].note_change == "fill"
    assert "The note is saved." in replaces[0].effect
    assert kind_chosen[0].note_change is None
    assert kind_chosen[0].new_kind is AssignmentKind.TASK
    assert after.kind is AssignmentKind.TASK
    assert after.origins["kind"] is SourceChannel.PARENT_ENTRY
    assert after.origins["note"] is SourceChannel.PARENT_ENTRY
    assert after.note == "Use the sketchbook from last year."
    assert at_the_end == ("Pencil only.",)
    assert remembered[0].kind is AssignmentKind.TASK
    assert remembered[0].state == KNOWN
    assert remembered[0].effect == "Nothing changes."


def test_chosen_dates_are_written_with_the_row_and_a_paste_without_them_is_unchanged(
    tmp_path: pathlib.Path,
) -> None:
    """A later week names Book Covers due 09/10 beside the saved 09/08. Kept with no dates, as a
    paste is, the saved date stands and the card's is a claim beside it, the same as with an
    empty set; kept with chosen dates, the row takes them in the same save."""
    first = read_text(THREE_WEEKS, now=NOW, today=TODAY).items
    later = read_text(
        "Homework for Wren\n- 09/10/2026 - Thursday\n08 Geometry - Due: Book Covers:\n",
        now=NOW,
        today=TODAY,
    ).items
    outcomes = {}
    for name, extra in (
        ("paste", {}),
        ("empty", {"dates": {}}),
        ("chosen", {"dates": {0: (date(2026, 9, 10), date(2026, 9, 1))}}),
    ):
        store = ProjectStateStore.open(tmp_path / f"{name}.sqlite3", fixture_clock())
        try:
            keep(first, store)
            kept = keep(later, store, **extra)
            covers = {(item.course, item.title): item for item in store.all_assignments()}[
                "08 Geometry", "Book Covers"
            ]
            claims = [said.asserted_value for said in store.deadline_records(covers.assignment_id)]
            outcomes[name] = (kept, covers.due_date, covers.assigned_on, claims)
        finally:
            store.close()

    assert outcomes["paste"] == outcomes["empty"]
    kept, due, assigned, claims = outcomes["paste"]
    assert (kept, due) == (Kept(added=0, updated=1, unchanged=0), date(2026, 9, 8))
    assert claims == ["2026-09-08", "2026-09-08", "2026-09-10"]
    kept, due, assigned, claims = outcomes["chosen"]
    assert (kept, due, assigned) == (
        Kept(added=0, updated=1, unchanged=0),
        date(2026, 9, 10),
        date(2026, 9, 1),
    )
    assert claims == ["2026-09-08", "2026-09-08", "2026-09-10"]


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
    the same answer given again finds that row and changes nothing. With two rounds on
    record, a date matching neither asks which, never the nearest."""
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
    assert [change.state for change in nudged] == [REVIEW]
    assert nudged[0].identity_asked
    assert sorted(nudged[0].identity_shown) == sorted(row.assignment_id for row in rows)


def test_a_confirmed_new_round_keeps_what_the_family_added_when_the_answer_is_replayed(
    tmp_path: pathlib.Path,
) -> None:
    """A retry, or an older review page still open, sends the same new-work answer again
    after a parent has added a note and the school has reported on the new row, placed
    there by the parent's answer to which homework the undated report is about: the answer
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
        (asked,) = changes_for(told, store)
        dated = {row.due_date: row.assignment_id for row in store.all_assignments()}
        answer = IdentityAnswer(
            dated[date(2026, 9, 14)], asked.identity_shown, asked.identity_basis
        )
        keep(told, store, identities={0: answer})
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
    outcomes: dict[
        str,
        tuple[Kept | ChangedSinceShown | NotAsked | NotOffered | list[Change], list[Assignment]],
    ] = {}
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


@pytest.mark.parametrize(
    ("first", "second"),
    [("Monday 9/7/2026", "Tuesday 9/8/2026"), ("Tuesday 9/8/2026", "Monday 9/7/2026")],
    ids=["earlier-day-first", "later-day-first"],
)
def test_an_assigned_line_under_two_days_of_one_text_keeps_the_first_days_date(
    first: str, second: str
) -> None:
    """Pasted text keeps its rule: an assigned line repeated under two days is one assignment,
    assigned on the day the text names first."""
    line = "Math\nAssigned: Practice: (Due:09/10/2026)\n"
    [read] = readings(f"{first}\n{line}{second}\n{line}")

    assert read.assigned_on == date(2026, 9, int(first.split("/")[1]))
    assert read.due_date == date(2026, 9, 10)
    assert [claim.asserted_value for claim in read.claims] == ["2026-09-10"]


def test_a_card_under_two_days_of_one_text_is_one_reading_with_both_dates() -> None:
    """Pasted text keeps its rule: one name under two days within a week is one assignment,
    dated by the first day, with a claim for each."""
    [read] = readings(
        "Tuesday 9/8/2026\nMath\nDue: Practice:\nWednesday 9/9/2026\nMath\nDue: Practice:\n"
    )

    assert read.due_date == date(2026, 9, 8)
    assert sorted(claim.asserted_value for claim in read.claims) == ["2026-09-08", "2026-09-09"]
    assert read.occurrence is None


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


def test_missing_lines_about_undated_work_are_read_apart_one_reading_for_each() -> None:
    """Each Missing line can be about different homework under the name, so each is read
    apart, with its own line; a line repeated word for word is one report."""
    first = "09/14 Health - A: Homework: Course Guide Due Grade: Missing\n"
    second = first.replace("09/14", "09/21")
    two_emails = (
        "Date: Mon, Sep 14, 2026 12:00\nAssignments:\n"
        + first
        + "Date: Mon, Sep 21, 2026 12:00\nAssignments:\n"
        + second
    )
    one_day = "Assignments:\n" + first + "Assignments:\n" + second
    repeated = "Assignments:\n" + first + "Assignments:\n" + first

    def seen(text: str) -> list[tuple[int | None, list[tuple[str | None, date]]]]:
        return [
            (item.at_line, [(said.source_date_text, said.reported_on) for said in item.reports])
            for item in readings(text)
        ]

    assert seen(two_emails) == [
        (3, [("09/14", date(2026, 9, 14))]),
        (6, [("09/21", date(2026, 9, 21))]),
    ]
    assert seen(one_day) == [(2, [("09/14", TODAY)]), (4, [("09/21", TODAY)])]
    assert seen(repeated) == [(2, [("09/14", TODAY)])]


@pytest.mark.parametrize("order", ["card-first", "emails-first"])
@pytest.mark.parametrize("lines", [1, 2])
def test_missing_lines_beside_a_dated_card_of_their_name_are_read_apart_from_it(
    order: str, lines: int
) -> None:
    """A card's date, claims, and instructions are its own: each Missing line beside it is
    read with no date, as if the card weren't there, and placed on its own."""
    card = "Tuesday 9/8/2026\nHealth\nDue: Course Guide Due:\nBring the signed guide.\n"
    emails = "".join(
        f"Assignments:\n09/{day} Health - A: Homework: Course Guide Due Grade: Missing\n"
        for day in ("14", "21")[:lines]
    )
    text = card + "\n" + emails if order == "card-first" else emails + "\n" + card
    items = readings(text)
    dated = [item for item in items if item.due_date is not None]
    told = [item for item in items if item.due_date is None]

    assert len(items) == 1 + lines
    assert [(item.due_date, item.origin, item.reports) for item in dated] == [
        (date(2026, 9, 8), SourceChannel.LMS, ())
    ]
    assert words(dated[0]) == ("Bring the signed guide.",)
    assert [[said.source_date_text for said in item.reports] for item in told] == [
        ["09/14"],
        ["09/21"],
    ][:lines]
    for item in told:
        assert (item.origin, item.claims, item.instructions, item.occurrence) == (
            SourceChannel.EMAIL,
            (),
            (),
            None,
        )
    assert dated[0].occurrence is None


def test_a_missing_line_shares_a_dated_cards_card_only_once_both_land_on_one_homework(
    tmp_path: pathlib.Path,
) -> None:
    """A card a week or more from the saved date asks whether it's the same assignment; a
    Missing line beside it lands on the saved one by its own rule, and shares the card only
    once the answer puts the card there too."""
    text = (
        "Tuesday 9/15/2026\nMath\nDue: Practice:\n\n"
        "Assignments:\n09/14 Math - A: Homework: Practice Grade: Missing\n"
    )
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        keep(readings("Tuesday 9/1/2026\nMath\nDue: Practice:\n"), store)
        (saved,) = store.all_assignments()
        asked = changes_for(readings(text), store)
        same = changes_for(readings(text), store, occurrences={0: UPDATE})
        new = changes_for(readings(text), store, occurrences={0: NEW_WORK})
    finally:
        store.close()

    def placed(changes: list[Change]) -> list[tuple[str, int | None, str]]:
        return [(change.state, change.folded_into, change.assignment_id) for change in changes]

    assert placed(asked)[1:] == [(CLAIMED, None, saved.assignment_id)]
    assert asked[0].state == REVIEW
    assert placed(same) == [
        (CLAIMED, None, saved.assignment_id),
        (FOLDED, 0, saved.assignment_id),
    ]
    assert [report.source_date_text for report in same[0].new_reports] == ["09/14"]
    assert placed(new)[1:] == [(CLAIMED, None, saved.assignment_id)]
    assert new[0].state == NEW


@pytest.mark.parametrize("entries", ["typed", "dated"])
def test_a_card_that_brings_more_than_the_schools_reports_keeps_its_own(
    tmp_path: pathlib.Path, entries: str
) -> None:
    """Only the school's undated reports of one homework share a card: a typed entry, or a
    reading the portal dates, is never folded into another."""
    items: tuple[Reading, ...]
    if entries == "typed":
        items = (
            by_hand("Science", "Lab Log", None, None, None, "Bring the log.", now=NOW),
            by_hand("Science", "Lab Log", None, None, None, "Bring the goggles.", now=NOW),
        )
    else:
        email = "Assignments:\n09/08 Math - A: Homework: Practice Grade: Missing\n"
        items = readings(email + "Tuesday 9/8/2026\nMath\nDue: Practice:\n") + readings(
            email.replace("09/08", "09/09") + "Wednesday 9/9/2026\nMath\nDue: Practice:\n"
        )
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        changes = changes_for(items, store)
    finally:
        store.close()

    if entries == "typed":
        assert [change.state for change in changes] == [NEW, NEW]
    else:
        # Each Missing line is read apart and lands on the new homework the first card makes.
        assert [change.state for change in changes] == [FOLDED, NEW, FOLDED, NEW]
        assert [change.folded_into for change in changes] == [1, None, 1, None]


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
        instructions = store.school_instruction_readings([rows[0].assignment_id]).readable[
            rows[0].assignment_id
        ]
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
        untouched_instructions = refusing.school_instruction_readings(
            [untouched[0].assignment_id]
        ).readable
    finally:
        refusing.close()

    assert [(item.due_date, item.occurrence) for item in cards] == [
        (date(2026, 9, 1), None),
        (date(2026, 9, 8), "2026-09-08"),
        (date(2026, 9, 15), "2026-09-15"),
    ]
    assert [change.state for change in asked] == [KNOWN, REVIEW, REVIEW]
    # The first card of the assignment carries its one decision about the school's
    # instructions, the words the second card brought among them.
    assert [change.state for change in answered] == [CLAIMED, CLAIMED, CLAIMED]
    assert answered[0].label == "Saved; adds the school's instructions"
    assert answered[1].instructions_with == answered[0].key
    assert answered[1].label == "Saved; adds the due date"
    assert answered[2].label == "Saved; adds the due date and the assigned date"
    assert kept == Kept(added=0, updated=1, unchanged=0)
    assert len(rows) == 1
    assert (rows[0].due_date, rows[0].assigned_on) == (date(2026, 9, 15), date(2026, 9, 14))
    assert rows[0].note is None
    assert instructions.texts == ("Teacher instruction.",)
    assert instructions.current[0].channel is SourceChannel.LMS
    assert rows[0].origins["due_date"] is SourceChannel.LMS
    assert "note" not in rows[0].origins
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
    assert untouched_instructions == {}


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
    outcomes: dict[
        str,
        tuple[
            Assignment,
            list[SourceChannel],
            list[SourceChannel],
            list[tuple[str, SourceChannel | None]],
        ],
    ] = {}
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
            found = store.school_instruction_readings([row.assignment_id]).readable
            outcomes[name] = (
                row,
                [said.channel for said in store.deadline_records(row.assignment_id)],
                [report.channel for report in store.status_reports(row.assignment_id)],
                [(said.text, said.channel) for said in found[row.assignment_id].current],
            )
        finally:
            store.close()
    told, card = readings(email_first)

    assert (told.origin, told.due_date, told.claims, told.instructions) == (
        SourceChannel.EMAIL,
        None,
        (),
        (),
    )
    assert [report.channel for report in told.reports] == [SourceChannel.EMAIL]
    assert (card.origin, card.due_date, card.reports) == (SourceChannel.LMS, date(2026, 9, 10), ())
    assert [said.channel for said in card.instructions] == [SourceChannel.LMS]
    for name, (row, claim_channels, report_channels, applying_now) in outcomes.items():
        assert row.due_date == date(2026, 9, 10), name
        assert row.assigned_on == date(2026, 9, 8), name
        assert row.note is None, name
        assert applying_now == [("Bring the packet.", SourceChannel.LMS)], name
        assert row.reported_submission_status == "missing", name
        assert row.origins["due_date"] is SourceChannel.LMS, name
        assert row.origins["assigned_on"] is SourceChannel.LMS, name
        assert "note" not in row.origins, name
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

    def one_saving(_: int) -> Kept | ChangedSinceShown | NotAsked | NotOffered | list[Change]:
        released.wait()
        return keep(read.items, store)

    def one_answer(_: int) -> Kept | ChangedSinceShown | NotAsked | NotOffered | list[Change]:
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


# ------------------------------------------------------------------ cards from screenshots

PORTAL_WEEK = """Monday 10/5/2026
Science 8
Assigned: Chains Reading Guide 6: (Due: 10/8/2026)
Read chapter 6 and answer the guide.
Math 8
Due: U1 Quiz 3
moved from 10/1 to 10/5
Thursday 10/8/2026
Science 8
Due: Chains Reading Guide 6
Thursday 10/22/2026
Science 8
Due: Chains Reading Guide 6
"""


def test_portal_cards_read_as_the_same_cards_read_in_a_paste() -> None:
    """The screenshots' cards go through the paste's own assembly: the same claims, dates,
    instructions and rounds, apart from the paste's line numbers."""
    cards = [
        PortalCard(
            "Science 8",
            "Chains Reading Guide 6",
            "assigned",
            date(2026, 10, 8),
            date(2026, 10, 5),
            "Read chapter 6 and answer the guide.",
        ),
        PortalCard(
            "Math 8",
            "U1 Quiz 3",
            "due",
            date(2026, 10, 5),
            date(2026, 10, 5),
            "moved from 10/1 to 10/5",
        ),
        PortalCard(
            "Science 8", "Chains Reading Guide 6", "due", date(2026, 10, 8), date(2026, 10, 8), None
        ),
        PortalCard(
            "Science 8",
            "Chains Reading Guide 6",
            "due",
            date(2026, 10, 22),
            date(2026, 10, 22),
            None,
        ),
    ]

    pasted = read_text(PORTAL_WEEK, now=NOW, today=TODAY)
    read = read_cards(cards, now=NOW)

    assert pasted.unread == ()
    assert [dataclasses.replace(r, at_line=None) for r in read.readings] == [
        dataclasses.replace(r, at_line=None) for r in pasted.items
    ]
    assert read.joined == (0, 2, 0, 1)
