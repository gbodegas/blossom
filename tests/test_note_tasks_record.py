# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Readings made from a teacher's note, and readings left out of a save.

Every card and note here is made up, in the shapes the school's weekly summary
writes them: a fictional first name in the heading, ordinary course names, and
made-up titles. The year is 2026, where November 2nd is a Monday.
"""

import dataclasses
import hashlib
import pathlib
from datetime import UTC, date, datetime
from typing import Final

import pytest

from blossom.intake import (
    FAMILY_CONFIDENCE,
    KNOWN,
    LEFT_OUT,
    NEW,
    PORTAL_CONFIDENCE,
    REVIEW,
    TEACHER_NOTE,
    UPDATE,
    Kept,
    NotAsked,
    Reading,
    by_hand,
    changes_for,
    conflicting_choices,
    keep,
    left_out_by_default,
    note_reading,
    read_text,
    with_family_deadline,
    with_note_readings,
)
from blossom.reconciliation import SourceChannel
from blossom.school_instructions import InstructionSeen, SubmittedChoice
from blossom.stores.project_state import Assignment, AssignmentKind, ProjectStateStore
from blossom.teacher_notes import NoteBlock, NoteTask, Standing, read_notes
from tests.support import fixture_clock, waiting_note

NOW = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)
TODAY = date(2026, 11, 3)
"""The moment and the day the made-up text is read: the Tuesday of its week."""

WEEK = """Homework for Wren
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
"""Two days of the weekly summary: five cards, one with an instruction of its own, and
what teachers typed under a subject, each note after the subject and a hyphen on a line
of their own."""


def nov(day: int) -> date:
    """A day of November 2026."""
    return date(2026, 11, day)


def cards() -> tuple[Reading, ...]:
    """The readings the reader makes from the cards of the week."""
    return read_text(WEEK, now=NOW, today=TODAY).items


def test_a_reading_from_a_card_is_made_from_no_note() -> None:
    """A card's reading names no teacher's note, and the place a note's date is seen in has
    a name of its own."""
    assert TEACHER_NOTE == "the teacher's note"
    assert len(cards()) == 4
    assert [item.teacher_note for item in cards()] == [None] * 4


def test_readings_equal_without_a_note_are_equal() -> None:
    """Two readings of the same text are equal, field by field."""
    assert cards() == cards()


def one_task(subject: str, day: date, *paragraphs: str, line: int = 0) -> NoteTask:
    """The one task a made-up note reads as."""
    (task,) = read_notes((NoteBlock(subject, day, paragraphs, line),))
    return task


def test_a_dated_task_reads_as_the_schools_homework_due_on_its_deadline() -> None:
    """A task with a deadline is a reading from the portal, due that day, whose one claim is
    seen in the teacher's note and whose instruction is the task's words under its first day."""
    task = one_task("Humanities", nov(2), "Map project due on Wednesday", line=5)

    reading = note_reading(task, now=NOW)

    assert reading.pair == ("Humanities", "Map project")
    assert (reading.due_date, reading.assigned_on) == (nov(4), None)
    assert reading.kind is AssignmentKind.HOMEWORK
    assert reading.origin is SourceChannel.LMS
    assert [
        (said.channel, said.asserted_value, said.seen_in, said.confidence, said.observed_at)
        for said in reading.claims
    ] == [(SourceChannel.LMS, "2026-11-04", TEACHER_NOTE, PORTAL_CONFIDENCE, NOW)]
    assert reading.instructions == (
        InstructionSeen("Map project due on Wednesday", SourceChannel.LMS, None, nov(2)),
    )
    assert (reading.note, reading.reports, reading.occurrence) == (None, (), None)
    assert reading.at_line == 5
    assert reading.teacher_note is task
    assert reading.assignment().origins == {
        "record": SourceChannel.LMS,
        "due_date": SourceChannel.LMS,
        "kind": SourceChannel.LMS,
    }


NO_HOMEWORK: Final = "Unit test today. No additional Religion homework assigned."
"""A note that says no homework is assigned."""

UNDATED = [
    pytest.param(
        "Spanish",
        nov(13),
        (
            "Study and memorize the verb categories",
            "Be ready to form sentences with each category.",
        ),
        "Study and memorize the verb categories",
        Standing.HOMEWORK,
        id="homework-with-no-deadline",
    ),
    pytest.param(
        "Science",
        nov(10),
        ("Online practice due Tuesday",),
        "Online practice",
        Standing.HOMEWORK,
        id="a-deadline-with-two-choices",
    ),
    pytest.param(
        "08 Geometry",
        nov(3),
        ("Quiz on unit two", "Sections 2 and 3"),
        "Quiz on unit two",
        Standing.UNCLEAR,
        id="unclear",
    ),
    pytest.param(
        "Religion",
        nov(12),
        (NO_HOMEWORK,),
        "Unit test today",
        Standing.NOT_ASSIGNMENT,
        id="no-homework",
    ),
]
"""Notes that settle no deadline: the subject, the school day, the paragraphs, and the name
and standing the task has."""


@pytest.mark.parametrize(("subject", "day", "paragraphs", "name", "standing"), UNDATED)
def test_a_task_with_no_deadline_reads_with_no_due_date_and_no_claim(
    subject: str, day: date, paragraphs: tuple[str, ...], name: str, standing: Standing
) -> None:
    """A task without one settled deadline has no due date and makes no claim, whatever its
    standing; the school day is never its date, and a note with no line number has no line."""
    task = one_task(subject, day, *paragraphs)

    reading = note_reading(task, now=NOW)

    assert reading.pair == (subject, name)
    assert task.standing is standing
    assert (reading.due_date, reading.assigned_on, reading.claims) == (None, None, ())
    assert reading.instructions == (
        InstructionSeen("\n\n".join(paragraphs), SourceChannel.LMS, None, day),
    )
    assert reading.at_line is None
    assert reading.teacher_note is task
    assert reading.assignment().origins == {
        "record": SourceChannel.LMS,
        "kind": SourceChannel.LMS,
    }


def said(reading: Reading) -> list[tuple[SourceChannel, str, str | None, float]]:
    """Each claim a reading makes: its channel, its date, where it was seen, how sure it is."""
    return [
        (each.channel, each.asserted_value, each.seen_in, each.confidence)
        for each in reading.claims
    ]


def map_project() -> Reading:
    """A note reading whose note settles a deadline: Wednesday, November 4."""
    return note_reading(one_task("Humanities", nov(2), "Map project due on Wednesday"), now=NOW)


def online_practice() -> Reading:
    """A note reading whose note settles no deadline."""
    return note_reading(one_task("Science", nov(10), "Online practice due Tuesday"), now=NOW)


def test_a_date_the_family_sets_on_an_undated_note_reading_is_the_familys_entry() -> None:
    """The date becomes the due date, claimed by the parent's entry as a typed entry claims
    it, and nothing else of the reading changes."""
    reading = online_practice()

    dated = with_family_deadline(reading, nov(17), now=NOW)

    assert dated.due_date == nov(17)
    assert said(dated) == [(SourceChannel.PARENT_ENTRY, "2026-11-17", None, FAMILY_CONFIDENCE)]
    assert dated.field_origins == {"due_date": SourceChannel.PARENT_ENTRY}
    assert dated.assignment().origins == {
        "record": SourceChannel.LMS,
        "due_date": SourceChannel.PARENT_ENTRY,
        "kind": SourceChannel.LMS,
    }
    assert dataclasses.replace(dated, due_date=None, claims=(), field_origins={}) == reading
    assert reading.field_origins == {}


def test_a_date_the_family_corrects_replaces_the_claim_seen_in_the_note() -> None:
    """Another date than the one the note settles is the family's: the claim seen in the
    teacher's note is gone, and the parent's entry claims the date given."""
    reading = map_project()

    dated = with_family_deadline(reading, nov(6), now=NOW)

    assert dated.due_date == nov(6)
    assert said(dated) == [(SourceChannel.PARENT_ENTRY, "2026-11-06", None, FAMILY_CONFIDENCE)]
    assert dated.origin_of("due_date") is SourceChannel.PARENT_ENTRY
    assert dated.origin is SourceChannel.LMS
    assert dated.instructions == reading.instructions
    assert dated.teacher_note is reading.teacher_note


def test_a_date_the_family_clears_leaves_no_due_date_and_no_claim() -> None:
    """No date given where the note settles one: no due date, no claim, and no origin for
    a due date."""
    cleared = with_family_deadline(map_project(), None, now=NOW)

    assert (cleared.due_date, cleared.claims) == (None, ())
    assert "due_date" not in cleared.field_origins
    assert cleared.assignment().origins == {
        "record": SourceChannel.LMS,
        "kind": SourceChannel.LMS,
    }


def test_the_date_the_note_settles_given_back_leaves_the_reading_as_it_is() -> None:
    """The reader's own date, or none where the note settles none, changes nothing: the
    date stays the school's, seen in the teacher's note."""
    dated, undated = map_project(), online_practice()

    assert with_family_deadline(dated, nov(4), now=NOW) is dated
    assert with_family_deadline(undated, None, now=NOW) is undated
    assert said(dated) == [(SourceChannel.LMS, "2026-11-04", TEACHER_NOTE, PORTAL_CONFIDENCE)]


def test_a_reading_made_from_no_note_takes_no_family_deadline() -> None:
    """A card's reading is handed back as it is, whatever date is given."""
    card = cards()[0]

    assert with_family_deadline(card, nov(9), now=NOW) is card
    assert with_family_deadline(card, None, now=NOW) is card


def by_pair(items: tuple[Reading, ...]) -> dict[tuple[str, str], list[Reading]]:
    """The readings under each class and title, in the order given."""
    found: dict[tuple[str, str], list[Reading]] = {}
    for item in items:
        found.setdefault(item.pair, []).append(item)
    return found


def week_with(note: str, title: str = "Short essay") -> str:
    """The week with a Religion note on Tuesday right above the formal card of a title."""
    card = f"Religion - Due: {title}:"
    return WEEK.replace("Religion - Due: Short essay:", f"Religion -\n{note}\n{card}")


def test_the_cards_come_first_as_read_and_each_task_of_the_notes_is_one_reading() -> None:
    """The card readings keep their places and their content. After them comes one reading
    for each task of the notes, in the order the notes give them; a paragraph listed again
    the next day adds none, and a task has a due date only where its note settles one."""
    read = read_text(WEEK, now=NOW, today=TODAY)
    tasks = read_notes([note.block for note in read.notes])

    items = with_note_readings(read, now=NOW)
    made = items[len(read.items) :]

    assert items[: len(read.items)] == read.items
    assert [item.pair for item in made] == [
        ("Humanities", "Study for the vocabulary quiz on Thursday"),
        ("Humanities", "Map project"),
        ("Science", "Lab notebook"),
        ("Art", "Please bring a shoebox"),
        ("08 Geometry", "Quiz on unit two"),
        ("Science", "Reading log"),
        ("Science", "Online practice"),
    ]
    assert [item.due_date for item in made] == [None, nov(4), nov(3), None, None, nov(3), None]
    assert made == tuple(note_reading(task, now=NOW) for task in tasks)
    assert tuple(item.teacher_note for item in made) == tasks
    assert tasks[1].seen_on == (nov(2), nov(3))
    assert [item.occurrence for item in made] == [None] * 7
    assert made[3].at_line == 12


def test_only_the_note_readings_that_are_not_homework_are_left_out_by_default() -> None:
    """The keys left out before anyone chooses are those of note readings whose task is
    unclear or not an assignment; no card's key is among them."""
    read = read_text(WEEK, now=NOW, today=TODAY)
    none = one_task("Religion", nov(12), NO_HOMEWORK)
    items = (*with_note_readings(read, now=NOW), note_reading(none, now=NOW))

    assert left_out_by_default(items) == frozenset({8, 11})
    assert items[8].pair == ("08 Geometry", "Quiz on unit two")
    assert left_out_by_default(read.items) == frozenset()


SAYS_MORE = [
    pytest.param("Short essay Due.", "Short essay", False, id="due-alone"),
    pytest.param("Short essay due today.", "Short essay", False, id="due-with-a-date"),
    pytest.param("The class project is due today.", "The class project", False, id="is-before-due"),
    pytest.param(
        "Short essay due today. Write it in ink.", "Short essay", True, id="words-of-its-own"
    ),
]
"""A note above the formal card of the same name and day: the note, the card's title, and
whether the note's words say more than the name and the deadline statement."""


@pytest.mark.parametrize(("note", "title", "adds"), SAYS_MORE)
def test_a_note_naming_the_card_of_its_day_is_that_card_and_adds_only_what_it_says(
    note: str, title: str, adds: bool
) -> None:
    """A task named as a formal card of the same class and school day is that card's
    assignment: no reading of its own, and its words are one more instruction only when
    they say more than the name and the deadline statement."""
    read = read_text(week_with(note, title), now=NOW, today=TODAY)
    card = by_pair(read.items)["Religion", title][0]
    (task,) = [item for item in read_notes([n.block for n in read.notes]) if item.name == title]

    items = with_note_readings(read, now=NOW)
    (joined,) = by_pair(items)["Religion", title]

    assert len(items) == len(read.items) + 7
    assert items.index(joined) == read.items.index(card)
    assert joined.teacher_note is None
    more = (InstructionSeen(task.words, SourceChannel.LMS, None, nov(3)),) if adds else ()
    assert joined.instructions == card.instructions + more
    assert dataclasses.replace(joined, claims=card.claims, instructions=card.instructions) == card


def test_a_joined_note_with_a_deadline_adds_its_claim_and_one_without_adds_none() -> None:
    """The card keeps its own claims; a deadline the note settles is one more, seen in the
    teacher's note, and a note that settles none leaves the card exactly as read."""
    dated = read_text(week_with("Short essay due Friday."), now=NOW, today=TODAY)
    undated = read_text(week_with("Short essay Due."), now=NOW, today=TODAY)
    card = by_pair(dated.items)["Religion", "Short essay"][0]

    (with_claim,) = by_pair(with_note_readings(dated, now=NOW))["Religion", "Short essay"]
    (as_read,) = by_pair(with_note_readings(undated, now=NOW))["Religion", "Short essay"]

    assert said(with_claim) == [
        *said(card),
        (SourceChannel.LMS, "2026-11-06", TEACHER_NOTE, PORTAL_CONFIDENCE),
    ]
    assert with_claim.due_date == card.due_date == nov(3)
    assert as_read == by_pair(undated.items)["Religion", "Short essay"][0]


def test_a_note_naming_a_card_of_another_day_is_a_reading_of_its_own() -> None:
    """A task named as a card that was read under other school days joins nothing: the card
    stays as read, and the task's reading carries its first day and a mark of its words, so
    the two have different ids."""
    text = WEEK + "11/04/2026 - Wednesday\n08 Geometry -\nPractice set 4 due Friday\n\n"
    read = read_text(text, now=NOW, today=TODAY)

    items = with_note_readings(read, now=NOW)
    card, noted = by_pair(items)["08 Geometry", "Practice set 4"]
    mark = hashlib.sha256(b"Practice set 4 due Friday").hexdigest()[:8]

    assert card == by_pair(read.items)["08 Geometry", "Practice set 4"][0]
    assert items.index(noted) == len(items) - 1
    assert noted.teacher_note is not None
    assert noted.due_date == nov(6)
    assert noted.occurrence == f"2026-11-04/{mark}"
    assert noted.assignment_id != card.assignment_id


TWO_WEEKS = (
    "Homework for Wren\n"
    "- 11/02/2026 - Monday\nMath - Due: Weekly practice:\n"
    "- 11/09/2026 - Monday\nMath - Due: Weekly practice:\n"
)
"""One name due on two Mondays a week apart: two readings, the later a question."""

QUIZ: Final = ("08 Geometry", "Quiz on unit two")
"""The one task of the week that is unclear, and so left out by default."""


def week_items() -> tuple[Reading, ...]:
    """The week's four card readings, then its seven note readings."""
    return with_note_readings(read_text(WEEK, now=NOW, today=TODAY), now=NOW)


def rows(store: ProjectStateStore) -> dict[tuple[str, str], Assignment]:
    """The assignments on record, by class and title."""
    return {(row.course, row.title): row for row in store.all_assignments()}


def test_a_reading_left_out_is_shown_as_not_selected_and_nothing_else_changes(
    tmp_path: pathlib.Path,
) -> None:
    """A key in ``left_out`` gives a change in a state of its own, with no row, no claims
    and no instructions decided; every other change is what it is with nothing left out."""
    items = week_items()
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        whole = changes_for(items, store)
        changes = changes_for(items, store, left_out=left_out_by_default(items))
    finally:
        store.close()
    out = changes[8]

    assert LEFT_OUT == "left_out"
    assert out.reading.pair == QUIZ
    assert (out.left_out, out.state, out.label) == (True, LEFT_OUT, "Not selected")
    assert out.effect == "Not selected. Nothing from this note is saved."
    assert (out.on_record, out.new_claims, out.new_reports) == (None, (), ())
    assert (out.instructions, out.offers, out.lands_nowhere) == (None, None, False)
    assert [change.state for change in whole] == [NEW] * 11
    assert [change.left_out for change in whole] == [False] * 11
    assert changes[:8] + changes[9:] == whole[:8] + whole[9:]


def test_a_save_writes_nothing_for_a_reading_left_out_and_does_not_count_it(
    tmp_path: pathlib.Path,
) -> None:
    """The ten other readings are saved; the one left out has no row, no claim and no
    instruction on record, and is in none of the counts."""
    items = week_items()
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        kept = keep(items, store, left_out={8}, now=NOW, today=TODAY)
        saved = rows(store)
        name = items[8].assignment_id
        claims = store.deadline_records(name)
        instructions = store.school_instruction_readings([name])
    finally:
        store.close()

    assert kept == Kept(added=10, updated=0, unchanged=0)
    assert len(saved) == 10
    assert QUIZ not in saved
    assert saved["Humanities", "Map project"].due_date == nov(4)
    assert claims == []
    assert (instructions.readable.get(name), instructions.unreadable) == (None, frozenset())


def test_an_answer_about_instructions_on_a_reading_left_out_is_read_nowhere(
    tmp_path: pathlib.Path,
) -> None:
    """With the questions a page asked given, an answer on a reading left out is neither an
    answer to a question never asked nor a choice: the save goes through without it."""
    items = week_items()
    (text,) = (seen.text for seen in items[8].instructions)
    answer = SubmittedChoice(0, (text,), frozenset({text}))
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        refused = keep(items, store, asked={}, instruction_answers={8: answer})
        kept = keep(
            items, store, asked={}, instruction_answers={8: answer}, left_out=frozenset({8})
        )
        saved = rows(store)
    finally:
        store.close()

    assert refused == NotAsked(frozenset({8}))
    assert kept == Kept(added=10, updated=0, unchanged=0)
    assert QUIZ not in saved


def test_a_later_reading_of_the_same_name_is_not_folded_into_one_left_out(
    tmp_path: pathlib.Path,
) -> None:
    """A reading left out is no first card of its name: the later one a week away asks
    nothing, an answer that it is the same assignment folds nothing, and it is saved as
    read, alone."""
    items = read_text(TWO_WEEKS, now=NOW, today=TODAY).items
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        asked = changes_for(items, store)
        alone = changes_for(items, store, left_out={0})
        answered = changes_for(items, store, left_out={0}, occurrences={1: UPDATE})
        kept = keep(items, store, left_out={0}, occurrences={1: UPDATE})
        saved = store.all_assignments()
    finally:
        store.close()

    assert [change.state for change in asked] == [NEW, REVIEW]
    assert [change.state for change in alone] == [LEFT_OUT, NEW]
    assert [change.state for change in answered] == [LEFT_OUT, NEW]
    assert [change.folded_into for change in answered] == [None, None]
    assert kept == Kept(added=1, updated=0, unchanged=0)
    assert [(row.title, row.due_date) for row in saved] == [("Weekly practice", nov(9))]


def test_a_type_chosen_on_a_reading_left_out_conflicts_with_no_other(
    tmp_path: pathlib.Path,
) -> None:
    """Two entries for one assignment that choose different types are a conflict, and
    nothing is saved; with one of them left out the other is saved with its own type."""
    items = (
        by_hand("Math", "Weekly practice", nov(2), None, AssignmentKind.TASK, now=NOW),
        by_hand("Math", "Weekly practice", nov(3), None, AssignmentKind.HOMEWORK, now=NOW),
    )
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        both = keep(items, store)
        nothing = store.all_assignments()
        names = conflicting_choices(changes_for(items, store, left_out={0}))
        kept = keep(items, store, left_out={0})
        saved = store.all_assignments()
    finally:
        store.close()

    assert isinstance(both, list)
    assert conflicting_choices(both) == ["Math: Weekly practice"]
    assert nothing == []
    assert names == []
    assert kept == Kept(added=1, updated=0, unchanged=0)
    assert [(row.kind, row.due_date) for row in saved] == [(AssignmentKind.HOMEWORK, nov(3))]


def test_a_waiting_note_of_hers_is_not_offered_on_a_reading_left_out(
    tmp_path: pathlib.Path,
) -> None:
    """Her note that waits with a card's class and title is offered on that card, and on
    none that is left out."""
    items = week_items()
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        name = waiting_note(store, course=QUIZ[0], title=QUIZ[1], text="Quiz on unit two")
        offered = changes_for(items, store)[8]
        out = changes_for(items, store, left_out={8})[8]
    finally:
        store.close()

    assert [note.capture_id for note in offered.waiting] == [name]
    assert out.waiting == ()


def test_with_nothing_left_out_every_result_is_what_it_is_without_the_argument(
    tmp_path: pathlib.Path,
) -> None:
    """``None`` and an empty set leave nothing out: the changes and the counts of a paste of
    cards are the same with either as with no argument, on a first save and on a second."""
    items = cards()
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        new = changes_for(items, store)
        new_none = changes_for(items, store, left_out=None)
        new_empty = changes_for(items, store, left_out=frozenset())
        first = keep(items, store, left_out=None)
        known = changes_for(items, store)
        known_empty = changes_for(items, store, left_out=())
        second = keep(items, store, left_out=frozenset())
        third = keep(items, store)
    finally:
        store.close()

    assert new == new_none == new_empty
    assert [change.state for change in new] == [NEW] * 4
    assert first == Kept(added=4, updated=0, unchanged=0)
    assert known == known_empty
    assert [change.state for change in known] == [KNOWN] * 4
    assert second == third == Kept(added=0, updated=0, unchanged=4)
    assert not any(change.left_out for change in (*new, *known))
