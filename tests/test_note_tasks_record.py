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
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Final

import pytest

from blossom.intake import (
    CLAIMED,
    FAMILY_CONFIDENCE,
    FOLDED,
    KNOWN,
    LEFT_OUT,
    NEW,
    NEW_WORK,
    PORTAL_CONFIDENCE,
    REVIEW,
    TEACHER_NOTE,
    UPDATE,
    IdentityAnswer,
    Kept,
    NotAsked,
    Read,
    Reading,
    TaskAnswer,
    by_hand,
    changes_for,
    conflicting_choices,
    join_effects,
    joins_moved_from,
    keep,
    left_out_by_default,
    note_reading,
    read_text,
    saved_joined_notes,
    with_answers,
    with_family_deadline,
    with_joined,
    with_note_readings,
    with_notes,
)
from blossom.reconciliation import SourceChannel
from blossom.school_instructions import InstructionSeen, SubmittedChoice
from blossom.stores.project_state import Assignment, AssignmentKind, ProjectStateStore
from blossom.teacher_notes import NoteBlock, NoteTask, Standing, read_notes
from tests.support import fixture_clock, homework_from_a_note, reported, waiting_note

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


def test_every_note_reading_is_left_out_by_default() -> None:
    """The keys left out before anyone chooses are those of every note reading, whatever
    its standing; no card's key is among them."""
    read = read_text(WEEK, now=NOW, today=TODAY)
    none = one_task("Religion", nov(12), NO_HOMEWORK)
    items = (*with_note_readings(read, now=NOW), note_reading(none, now=NOW))

    assert left_out_by_default(items) == frozenset({4, 5, 6, 7, 8, 9, 10, 11})
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
def test_a_note_naming_the_card_of_its_day_is_that_card_as_read(
    note: str, title: str, adds: bool
) -> None:
    """A task named as a formal card of the same class and school day is that card's
    assignment: no reading of its own. As read, the card is exactly as the school's card
    gave it, with the task among its joined notes: the note adds nothing until the family
    selects it."""
    read = read_text(week_with(note, title), now=NOW, today=TODAY)
    card = by_pair(read.items)["Religion", title][0]
    (task,) = [item for item in read_notes([n.block for n in read.notes]) if item.name == title]

    items = with_note_readings(read, now=NOW)
    (joined,) = by_pair(items)["Religion", title]

    assert len(items) == len(read.items) + 7
    assert items.index(joined) == read.items.index(card)
    assert joined.teacher_note is None
    assert joined.joined_notes == (task,)
    assert joined.claims == card.claims
    assert joined.instructions == card.instructions
    assert joined == card


@pytest.mark.parametrize(("note", "title", "adds"), SAYS_MORE)
def test_a_selected_joined_note_adds_only_what_it_says(note: str, title: str, adds: bool) -> None:
    """Once the family selects a note joined to its card, the card takes the claim of a
    deadline the note settles, seen in the teacher's note, and the note's words as one more
    instruction only when they say more than the name and the deadline statement. Its due
    date stays the card's, applying the answers again adds nothing, and a note left
    unselected leaves the card as read."""
    read = read_text(week_with(note, title), now=NOW, today=TODAY)
    card = by_pair(read.items)["Religion", title][0]
    key = read.items.index(card)
    noted = with_notes(read, now=NOW)
    (task,) = noted.items[key].joined_notes

    chosen = with_answers(
        dataclasses.replace(noted, joined=frozenset({(key, 0)})), [], now=NOW, today=TODAY
    )
    left = with_answers(noted, [], now=NOW, today=TODAY)

    more = (InstructionSeen(task.words, SourceChannel.LMS, None, nov(3)),) if adds else ()
    claimed = (
        [(SourceChannel.LMS, task.deadline.isoformat(), TEACHER_NOTE, PORTAL_CONFIDENCE)]
        if task.deadline is not None
        else []
    )
    assert chosen.items[key].instructions == card.instructions + more
    assert said(chosen.items[key]) == said(card) + claimed
    assert chosen.items[key].due_date == card.due_date
    assert chosen.items[key].joined_notes == (task,)
    assert with_answers(chosen, [], now=NOW, today=TODAY).items == chosen.items
    assert said(left.items[key]) == said(card)
    assert left.items[key].instructions == card.instructions


JOINED_NOTE = (
    "Homework for Wren\n11/03/2026 - Tuesday\nReligion -\n{}\nReligion - Due: Short essay:\n"
)
"""A Tuesday with a note above the formal card of its name, which carries no instruction."""


@pytest.mark.parametrize("selected", [False, True], ids=["unselected", "selected"])
def test_a_joined_note_adds_its_claim_and_words_on_save_only_when_selected(
    tmp_path: pathlib.Path, selected: bool
) -> None:
    """A card with a joined note that names a deadline and says more is saved as read while
    the note is unselected: no claim of the note's and no instruction. Selected, the note's
    deadline is one more claim, seen in the teacher's note, and its words are an
    instruction of the row; the row's due date is the card's either way."""
    text = JOINED_NOTE.format("Short essay due Friday. Write it in ink.")
    read = with_notes(read_text(text, now=NOW, today=TODAY), now=NOW)
    chosen = with_answers(
        dataclasses.replace(read, joined=frozenset({(0, 0)} if selected else ())),
        [],
        now=NOW,
        today=TODAY,
    )
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        kept = keep(chosen.items, store, left_out=chosen.left_out, now=NOW, today=TODAY)
        (row,) = store.all_assignments()
        claims = store.deadline_records(row.assignment_id)
        instructions = store.school_instruction_readings([row.assignment_id])
    finally:
        store.close()

    readable = instructions.readable.get(row.assignment_id)
    assert len(read.items) == 1
    assert kept == Kept(added=1, updated=0, unchanged=0)
    assert row.due_date == nov(3)
    noted = ("2026-11-06", TEACHER_NOTE) in {(each.asserted_value, each.seen_in) for each in claims}
    assert noted is selected
    assert ([] if readable is None else [each.text for each in readable.kept]) == (
        ["Short essay due Friday. Write it in ink."] if selected else []
    )


def test_a_familys_due_date_stays_when_a_selected_joined_note_is_saved(
    tmp_path: pathlib.Path,
) -> None:
    """A row the family dated Wednesday, read again under a Tuesday card with a selected
    joined note that says Friday: the save keeps the family's due date and the family as
    where it came from, with the note's date one more claim beside it."""
    read = with_notes(
        read_text(JOINED_NOTE.format("Short essay due Friday."), now=NOW, today=TODAY), now=NOW
    )
    chosen = with_answers(
        dataclasses.replace(read, joined=frozenset({(0, 0)})), [], now=NOW, today=TODAY
    )
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        name = parents_row(store, "Religion", "Short essay", nov(4), None)
        (change,) = changes_for(chosen.items, store)
        identities = (
            {0: IdentityAnswer(name, change.identity_shown, change.identity_basis)}
            if change.identity_asked
            else {}
        )
        kept = keep(chosen.items, store, identities=identities, now=NOW, today=TODAY)
        row = store.one_assignment(name)
        claims = store.deadline_records(name)
    finally:
        store.close()

    assert kept == Kept(added=0, updated=1, unchanged=0)
    assert row is not None
    assert row.due_date == nov(4)
    assert row.origins["due_date"] == SourceChannel.PARENT_ENTRY
    assert ("2026-11-06", TEACHER_NOTE) in {(each.asserted_value, each.seen_in) for each in claims}


def test_a_joined_note_with_a_deadline_adds_no_claim_as_read() -> None:
    """As read, the card keeps its own claims and due date whatever deadline a joined note
    settles: the note's deadline is no claim of the card's until the family selects it."""
    dated = read_text(week_with("Short essay due Friday."), now=NOW, today=TODAY)
    undated = read_text(week_with("Short essay Due."), now=NOW, today=TODAY)
    card = by_pair(dated.items)["Religion", "Short essay"][0]

    (with_note,) = by_pair(with_note_readings(dated, now=NOW))["Religion", "Short essay"]
    (as_read,) = by_pair(with_note_readings(undated, now=NOW))["Religion", "Short essay"]

    assert said(with_note) == said(card)
    assert [task.deadline for task in with_note.joined_notes] == [nov(6)]
    assert with_note.due_date == card.due_date == nov(3)
    assert as_read == by_pair(undated.items)["Religion", "Short essay"][0]
    assert said(as_read) == said(by_pair(undated.items)["Religion", "Short essay"][0])


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


INK = "Short essay due Friday. Write it in ink."
"""A joined note that settles a date and says more than its name."""


def joined_read(words: str) -> Read:
    """The Tuesday's card with a note of the words above it, read as the review reads it."""
    return with_notes(read_text(JOINED_NOTE.format(words), now=NOW, today=TODAY), now=NOW)


def save_selected(store: ProjectStateStore, words: str) -> Kept:
    """The Tuesday's card saved with its joined note of the words selected."""
    read = joined_read(words)
    chosen = with_answers(
        dataclasses.replace(read, joined=frozenset({(0, 0)})), [], now=NOW, today=TODAY
    )
    (change,) = changes_for(chosen.items, store)
    identities = (
        {0: IdentityAnswer(change.identity_shown[0], change.identity_shown, change.identity_basis)}
        if change.identity_asked
        else {}
    )
    kept = keep(chosen.items, store, identities=identities, now=NOW, today=TODAY)
    assert isinstance(kept, Kept)
    return kept


def test_a_joined_notes_tick_either_way_builds_the_card_from_the_card_as_read() -> None:
    """``with_joined`` gives the card with exactly what its ticked notes add: ticked, as
    ``with_answers`` applies it; ticked then unticked, the card as read; a place that names no
    joined note is ignored."""
    read = joined_read(INK)
    ticked = with_joined(read, {(0, 0)}, now=NOW)
    chosen = with_answers(
        dataclasses.replace(read, joined=frozenset({(0, 0)})), [], now=NOW, today=TODAY
    )

    assert ticked.items == chosen.items
    assert ticked.joined == frozenset({(0, 0)})
    assert with_joined(ticked, (), now=NOW).items == read.items
    assert with_joined(read, {(0, 1), (5, 0)}, now=NOW).joined == frozenset()


def test_a_card_whose_joined_tick_the_page_said_nothing_for_has_moved(
    tmp_path: pathlib.Path,
) -> None:
    """A card whose save does what the page said for its joined tick as sent has not moved;
    one the page said nothing for, such as two ticks changed on it, has; and one saved since,
    of which nothing would be written, has not moved on a retry and has without one."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        read = joined_read(INK)
        unticked = changes_for(read.items, store)
        ticked = changes_for(with_joined(read, {(0, 0)}, now=NOW).items, store)
        _, as_sent = join_effects(unticked, ticked, 0)
        before = joins_moved_from(ticked, {0: as_sent})
        unsigned = joins_moved_from(ticked, {0: ""})
        assert save_selected(store, INK) == Kept(added=1, updated=0, unchanged=0)
        saved = changes_for(with_joined(read, {(0, 0)}, now=NOW).items, store)
        retried = joins_moved_from(saved, {0: as_sent})
        strict = joins_moved_from(saved, {0: as_sent}, retry=False)
    finally:
        store.close()

    assert before == frozenset()
    assert unsigned == frozenset({0})
    assert retried == frozenset()
    assert strict == frozenset({0})


def proven(store: ProjectStateStore, items: tuple[Reading, ...]) -> frozenset[tuple[int, int]]:
    """The joined notes the record shows saved where their cards land."""
    return saved_joined_notes(items, changes_for(items, store, left_out=frozenset()), store)


def test_an_exact_repeat_of_a_saved_joined_note_is_shown_saved_and_saving_it_adds_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """A joined note saved selected, read again with its card: the card lands on the row it
    was saved to, which keeps the note's words under its first school day and the claim of
    its date seen in the teacher's note, so the note is shown saved, and saving it selected
    again writes nothing."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        assert save_selected(store, INK) == Kept(added=1, updated=0, unchanged=0)
        (row,) = store.all_assignments()
        claims = len(store.deadline_records(row.assignment_id))
        again = joined_read(INK)
        shown = proven(store, again.items)
        chosen = with_answers(dataclasses.replace(again, joined=shown), [], now=NOW, today=TODAY)
        kept = keep(chosen.items, store, now=NOW, today=TODAY)
        readable = store.school_instruction_readings([row.assignment_id]).readable
        after = len(store.deadline_records(row.assignment_id))
        rows = len(store.all_assignments())
    finally:
        store.close()

    assert shown == frozenset({(0, 0)})
    assert kept == Kept(added=0, updated=0, unchanged=1)
    assert (after, rows) == (claims, 1)
    assert [each.text for each in readable[row.assignment_id].kept] == [INK]


@pytest.mark.parametrize(
    ("saved", "again", "later"),
    [
        (INK, "Short essay due Friday. Write it in pen.", False),
        (INK, INK, True),
        ("Short essay due Friday.", "Short essay due Friday.", False),
    ],
    ids=["changed-words", "later-week", "date-only"],
)
def test_a_joined_note_the_record_cannot_show_saved_starts_unselected(
    tmp_path: pathlib.Path, saved: str, again: str, later: bool
) -> None:
    """A joined note is not shown saved when its words changed, when it is another week's
    occurrence of the same words (listed first on another school day), or when it added
    only a date claim: a claim carries no school day and the note's words were never kept
    as an instruction, so the record cannot prove that occurrence was saved. Saving with it
    left unselected removes nothing."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        save_selected(store, saved)
        (row,) = store.all_assignments()
        before = (
            len(store.deadline_records(row.assignment_id)),
            store.school_instruction_readings([row.assignment_id]).readable,
        )
        read = joined_read(again)
        items = read.items
        if later:
            (task,) = items[0].joined_notes
            moved = dataclasses.replace(task, first_day=nov(10), seen_on=(nov(10),))
            items = (dataclasses.replace(items[0], joined_notes=(moved,)),)
        shown = proven(store, items)
        keep(items, store, now=NOW, today=TODAY)
        after = (
            len(store.deadline_records(row.assignment_id)),
            store.school_instruction_readings([row.assignment_id]).readable,
        )
    finally:
        store.close()

    assert shown == frozenset()
    assert after == before


def test_a_saved_joined_note_on_a_row_the_family_dated_keeps_that_date_and_adds_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """A row the family dated Wednesday, saved with the Tuesday card's joined note that says
    Friday, read again: the note is shown saved, saving it again writes no second claim or
    instruction, and the family's due date stays, from the family."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        name = parents_row(store, "Religion", "Short essay", nov(4), None)
        save_selected(store, INK)
        claims = len(store.deadline_records(name))
        texts = store.school_instruction_readings([name]).readable[name].kept
        again = joined_read(INK)
        shown = proven(store, again.items)
        chosen = with_answers(dataclasses.replace(again, joined=shown), [], now=NOW, today=TODAY)
        kept = keep(chosen.items, store, now=NOW, today=TODAY)
        row = store.one_assignment(name)
        after = len(store.deadline_records(name))
        texts_after = store.school_instruction_readings([name]).readable[name].kept
    finally:
        store.close()

    assert shown == frozenset({(0, 0)})
    assert kept == Kept(added=0, updated=0, unchanged=1)
    assert row is not None
    assert (row.due_date, row.origins["due_date"]) == (nov(4), SourceChannel.PARENT_ENTRY)
    assert after == claims
    assert texts_after == texts


DECEMBER_NOTE = "Homework for Wren\n12/15/2026 - Tuesday\nReligion -\n{}\n"
"""A Tuesday in December with one note under it."""

DECEMBER_NOW = datetime(2026, 12, 15, 20, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("words", "written"),
    [
        ("Essay due January 8, 2028.", date(2028, 1, 8)),
        ("Essay due 1/8/1500.", date(1500, 1, 8)),
        ("Essay due 8 de enero de 2028.", date(2028, 1, 8)),
        ("Essay due January 8, 2028 at noon.", date(2028, 1, 8)),
        ("Essay due January 8, 1500 at noon.", date(1500, 1, 8)),
    ],
    ids=["english", "numeric", "spanish", "english-then-prose", "no-count-no-school-year"],
)
def test_a_written_year_no_school_year_holds_leaves_the_note_undated_with_that_date_named(
    words: str, written: date
) -> None:
    """A deadline read with its written year that falls outside a school year of the note's
    first school day, the range a due date typed or chosen on the review must fall in, is no
    deadline: the reading has no due date and no claim, its words stay its instruction for
    review, and the date as written is named for the family to choose. No other year is put
    in its place."""
    read = read_text(DECEMBER_NOTE.format(words), now=DECEMBER_NOW, today=date(2026, 12, 15))

    (reading,) = with_note_readings(read, now=DECEMBER_NOW)

    task = reading.teacher_note
    assert task is not None
    assert (reading.due_date, reading.claims) == (None, ())
    assert (task.deadline, task.deadline_words, task.choices, task.listed_after) == (
        None,
        None,
        (),
        (),
    )
    assert task.named == (written,)
    assert reading.instructions == (
        InstructionSeen(words, SourceChannel.LMS, None, date(2026, 12, 15)),
    )


@pytest.mark.parametrize(
    ("words", "deadline"),
    [
        ("Essay due January 8, 2026.", date(2026, 1, 8)),
        ("Essay due 8 de enero del 2026.", date(2026, 1, 8)),
        ("Essay due January 8.", date(2027, 1, 8)),
        ("Essay due January 8, 1500 words.", date(2027, 1, 8)),
    ],
    ids=["english-near", "spanish-near", "yearless-rollover", "count"],
)
def test_a_deadline_within_a_school_year_of_the_note_is_kept(words: str, deadline: date) -> None:
    """A written year near the note's school day, a January date read under a December
    school day with no year, and a date followed by a count all keep their deadline."""
    read = read_text(DECEMBER_NOTE.format(words), now=DECEMBER_NOW, today=date(2026, 12, 15))

    (reading,) = with_note_readings(read, now=DECEMBER_NOW)

    assert reading.teacher_note is not None
    assert (reading.due_date, reading.teacher_note.deadline) == (deadline, deadline)
    assert reading.teacher_note.named == ()


NEXT_MONDAY = datetime(2026, 11, 9, 20, 0, tzinfo=UTC)
"""The moment a text of two school weeks is read: the Monday of its second week."""

TWO_LISTINGS = """Homework for Wren
11/03/2026 - Tuesday
Spanish -
Spelling packet due Friday.

11/09/2026 - Monday
Spanish -
Spelling packet due Friday.
"""
"""The same note listed on a Tuesday and again on the next Monday, six days later."""

WITH_THE_CARD = TWO_LISTINGS.replace(
    "11/09/2026 - Monday\n",
    "11/09/2026 - Monday\nSpanish - Assigned: Spelling packet: (Due:11/13/2026)\n",
)
"""The same two listings with the Monday's formal card of the name above its note."""


def test_the_same_note_in_the_next_school_week_is_a_task_of_its_own_and_asks(
    tmp_path: pathlib.Path,
) -> None:
    """A note listed again in a later school week, six days after the first listing, is a
    reading of its own with its own deadline, and no line of either note is left for
    review. The later reading asks whether it is the same assignment or new work, and
    nothing is written until that is answered."""
    read = read_text(TWO_LISTINGS, now=NEXT_MONDAY, today=nov(9))
    noted = with_notes(read, now=NEXT_MONDAY)
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        changes = changes_for(noted.items, store)
        held = keep(noted.items, store, now=NEXT_MONDAY, today=nov(9))
        saved = store.all_assignments()
    finally:
        store.close()
    first, later = noted.items

    assert [item.pair for item in noted.items] == [("Spanish", "Spelling packet")] * 2
    assert [item.due_date for item in noted.items] == [nov(6), nov(13)]
    assert first.teacher_note is not None
    assert later.teacher_note is not None
    assert (first.teacher_note.seen_on, later.teacher_note.seen_on) == ((nov(3),), (nov(9),))
    assert first.assignment_id != later.assignment_id
    assert [item.text for item in noted.unread if "Spanish" in item.text] == []
    assert [item.text for item in noted.unread if "Spelling" in item.text] == []
    assert changes[0].state == NEW
    assert (changes[1].state, changes[1].ambiguous) == (REVIEW, True)
    assert isinstance(held, list)
    assert saved == []


def test_a_note_joins_the_card_of_its_own_school_week_only() -> None:
    """The Monday's note joins the Monday's card of its name, which takes only the deadline
    that note settles. The same words listed the week before are a reading of their own, with
    their own deadline, and give the card no claim."""
    read = read_text(WITH_THE_CARD, now=NEXT_MONDAY, today=nov(9))

    items = with_note_readings(read, now=NEXT_MONDAY)
    card, noted = by_pair(items)["Spanish", "Spelling packet"]

    assert card.teacher_note is None
    assert [task.first_day for task in card.joined_notes] == [nov(9)]
    assert [task.seen_on for task in card.joined_notes] == [(nov(9),)]
    assert card.due_date == nov(13)
    assert {each.asserted_value for each in card.claims} == {"2026-11-13"}
    assert noted.teacher_note is not None
    assert noted.teacher_note.seen_on == (nov(3),)
    assert noted.due_date == nov(6)
    assert said(noted) == [(SourceChannel.LMS, "2026-11-06", TEACHER_NOTE, PORTAL_CONFIDENCE)]
    assert len(items) == 2


MAP_PROJECT = """Homework for Wren
11/02/2026 - Monday
Humanities -
Map project due on Wednesday.

11/05/2026 - Thursday
Humanities - Assigned: Map project: (Due:11/12/2026)
Humanities -
Map project due on Wednesday.
"""
"""A note listed on a Monday and again on the Thursday, beside the Thursday's card of its
name, which is due a week later than the note's own deadline."""


def test_a_note_dated_apart_from_a_card_of_another_listing_day_is_its_own_reading() -> None:
    """The note's deadline, Wednesday, November 4, is not the card's due date, and the card
    was read under the Thursday, not the note's first listing day: the note joins nothing.
    The card stays as read, and the note is a reading of its own, due on its own deadline."""
    read = read_text(MAP_PROJECT, now=NOW, today=nov(5))

    items = with_note_readings(read, now=NOW)
    card, noted = by_pair(items)["Humanities", "Map project"]

    assert card == read.items[0]
    assert card.joined_notes == ()
    assert noted.teacher_note is not None
    assert noted.teacher_note.seen_on == (nov(2), nov(5))
    assert noted.due_date == nov(4)
    assert said(noted) == [(SourceChannel.LMS, "2026-11-04", TEACHER_NOTE, PORTAL_CONFIDENCE)]
    assert len(items) == 2


def every_day_of(note: str, card: str, *, on: int) -> str:
    """A school week, Monday to Friday, with the note under each day and the card under the
    day ``on``, above that day's note."""
    days = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
    out = ["Homework for Wren"]
    for number, name in enumerate(days, start=2):
        out.append(f"11/{number:02d}/2026 - {name}")
        if number == on:
            out.append(card)
        out += ["Humanities -", note, ""]
    return "\n".join(out)


def test_a_note_listed_all_week_joins_the_card_of_its_due_date() -> None:
    """A note listed Monday to Friday beside the Wednesday's card of its name, due on the
    note's own deadline, joins that card: one reading, the card's."""
    text = every_day_of(
        "Map project due Friday.", "Humanities - Assigned: Map project: (Due:11/06/2026)", on=4
    )
    read = read_text(text, now=NOW, today=nov(6))

    items = with_note_readings(read, now=NOW)
    (card,) = by_pair(items)["Humanities", "Map project"]

    assert card.teacher_note is None
    assert card.due_date == nov(6)
    assert [task.seen_on for task in card.joined_notes] == [
        (nov(2), nov(3), nov(4), nov(5), nov(6))
    ]
    assert len(items) == 1


def test_a_note_two_cards_could_join_joins_neither() -> None:
    """An undated note listed on the Monday and the Wednesday, where a card of its name was
    read under each day: two cards qualify, so the note joins neither and is a reading of its
    own, never the first card's."""
    text = (
        "Homework for Wren\n11/02/2026 - Monday\n"
        "Humanities - Assigned: Map project: (Due:11/06/2026)\n"
        "Humanities -\nMap project Due.\n\n"
        "11/04/2026 - Wednesday\n"
        "Humanities - Assigned: Map project: (Due:11/20/2026)\n"
        "Humanities -\nMap project Due.\n"
    )
    read = read_text(text, now=NOW, today=nov(4))

    items = with_note_readings(read, now=NOW)
    found = by_pair(items)["Humanities", "Map project"]

    assert [item.due_date for item in read.items] == [nov(6), nov(20)]
    assert [item.joined_notes for item in found[:2]] == [(), ()]
    assert found[:2] == list(read.items)
    assert len(found) == 3
    assert found[2].teacher_note is not None
    assert found[2].teacher_note.seen_on == (nov(2), nov(4))
    assert found[2].due_date is None


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
        changes = changes_for(items, store, left_out=frozenset({8}))
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


def test_note_readings_saved_before_are_known_and_a_second_save_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """Every reading of the week saved, the same readings are each known on the row they
    made, dated or not: nothing of a note's words is decided again, and a second save adds
    and updates nothing."""
    items = week_items()
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        first = keep(items, store, now=NOW, today=TODAY)
        again = changes_for(items, store)
        second = keep(items, store, now=NOW, today=TODAY)
        saved = store.all_assignments()
    finally:
        store.close()

    assert first == Kept(added=11, updated=0, unchanged=0)
    assert [change.state for change in again] == [KNOWN] * 11
    assert [change.assignment_id for change in again] == [item.assignment_id for item in items]
    assert [change.instructions for change in again[4:]] == [None] * 7
    assert [change.reading.pair for change in again] == [item.pair for item in items]
    assert second == Kept(added=0, updated=0, unchanged=11)
    assert len(saved) == 11


def test_a_note_saved_under_a_corrected_title_is_known_on_that_row(
    tmp_path: pathlib.Path,
) -> None:
    """A note reading saved under another title is found again by its words and the school
    day it was first listed under: the reading as read is known on that row, keeps the
    class and title it reads, and no second row is made."""
    reading = map_project()
    corrected = dataclasses.replace(reading, title="Map of the colonies")
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        keep((corrected,), store, now=NOW, today=TODAY)
        (change,) = changes_for((reading,), store)
        kept = keep((reading,), store, now=NOW, today=TODAY)
        saved = rows(store)
    finally:
        store.close()

    assert list(saved) == [("Humanities", "Map of the colonies")]
    assert change.state == KNOWN
    assert change.saved_as == saved["Humanities", "Map of the colonies"]
    assert change.reading.pair == ("Humanities", "Map project")
    assert kept == Kept(added=0, updated=0, unchanged=1)


@pytest.mark.parametrize("given", [None, nov(6)], ids=["cleared", "corrected"])
def test_a_note_saved_with_the_familys_date_is_known_and_the_saved_date_stays(
    tmp_path: pathlib.Path, given: date | None
) -> None:
    """A note reading saved with the date the family gave, or with none, is found again by
    its words and its first day: the reading as read, with the date its note settles, is
    known there, and neither the saved date nor the claims on record change."""
    reading = map_project()
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        keep((with_family_deadline(reading, given, now=NOW),), store, now=NOW, today=TODAY)
        before = store.deadline_records(reading.assignment_id)
        (change,) = changes_for((reading,), store)
        kept = keep((reading,), store, now=NOW, today=TODAY)
        (row,) = store.all_assignments()
        claims = store.deadline_records(reading.assignment_id)
    finally:
        store.close()

    assert reading.due_date == nov(4)
    assert (change.state, change.new_claims) == (KNOWN, ())
    assert change.on_record is not None
    assert change.on_record.assignment_id == row.assignment_id
    assert kept == Kept(added=0, updated=0, unchanged=1)
    assert row.due_date == given
    assert claims == before
    assert len(claims) == (0 if given is None else 1)


def test_the_same_words_first_listed_another_day_are_not_a_note_saved_before(
    tmp_path: pathlib.Path,
) -> None:
    """The words alone recognize nothing: the same words first listed under another school
    day, with no row of their class and title, land on no row."""
    reading = map_project()
    later = note_reading(one_task("Humanities", nov(9), "Map project due on Wednesday"), now=NOW)
    corrected = dataclasses.replace(reading, title="Map of the colonies")
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        keep((corrected,), store, now=NOW, today=TODAY)
        (change,) = changes_for((later,), store)
    finally:
        store.close()

    assert later.teacher_note is not None
    assert reading.teacher_note is not None
    assert later.teacher_note.words == reading.teacher_note.words
    assert later.pair == reading.pair
    assert (change.state, change.on_record) == (NEW, None)


def test_a_note_with_the_words_and_day_of_a_cards_instruction_is_known_on_that_card(
    tmp_path: pathlib.Path,
) -> None:
    """Recognition is by words and school day within a class, whatever the name: a note
    whose words are an instruction saved from a formal card of that day is that card's
    assignment, known there, and makes no row."""
    note = note_reading(one_task("Religion", nov(3), "Write 250 words on your question."), now=NOW)
    other_day = note_reading(
        one_task("Religion", nov(4), "Write 250 words on your question."), now=NOW
    )
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        keep(cards(), store, now=NOW, today=TODAY)
        (change,) = changes_for((note,), store)
        (apart,) = changes_for((other_day,), store)
        kept = keep((note,), store, now=NOW, today=TODAY)
        saved = rows(store)
    finally:
        store.close()

    assert note.pair != ("Religion", "Short essay")
    assert change.state == KNOWN
    assert change.saved_as == saved["Religion", "Short essay"]
    assert (apart.state, apart.on_record) == (NEW, None)
    assert kept == Kept(added=0, updated=0, unchanged=1)
    assert len(saved) == 4


def rows_read(
    store: ProjectStateStore, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, list[str]]]:
    """A list that fills with each read of claims or of the school's instructions for
    several rows at once: the name of the read and the ids it asks about, in order."""
    reads: list[tuple[str, list[str]]] = []
    claims = store.deadline_records_by_assignment
    instructions = store.school_instruction_readings

    def claims_of(ids: list[str]) -> object:
        reads.append(("claims", list(ids)))
        return claims(ids)

    def instructions_of(ids: list[str]) -> object:
        reads.append(("instructions", list(ids)))
        return instructions(ids)

    monkeypatch.setattr(store, "deadline_records_by_assignment", claims_of)
    monkeypatch.setattr(store, "school_instruction_readings", instructions_of)
    return reads


def test_a_paste_of_cards_reads_only_the_rows_of_its_own_names(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With rows of note readings on record in the same classes, a paste with no note
    reading asks the record about the rows under its own classes and titles and no other:
    the claims and the instructions of the four cards' rows, then the instructions of the
    one row a card brings an instruction to."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        keep(week_items(), store, now=NOW, today=TODAY)
        saved = rows(store)
        reads = rows_read(store, monkeypatch)
        changes_for(cards(), store)
    finally:
        store.close()
    named = [saved[item.pair].assignment_id for item in cards()]

    assert len(saved) == 11
    assert reads == [
        ("claims", named),
        ("instructions", named),
        ("instructions", [saved["Religion", "Short essay"].assignment_id]),
    ]


def test_a_note_reading_is_compared_with_every_row_of_its_class(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paste with a note reading asks the record about the rows under its name and then
    about every other row of the note's class, since a note saved before can stand under
    another title; rows of other classes are not asked about."""
    items = week_items()
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        keep(items, store, now=NOW, today=TODAY)
        saved = rows(store)
        reads = rows_read(store, monkeypatch)
        changes_for((items[10],), store)
    finally:
        store.close()
    science = [row.assignment_id for name, row in saved.items() if name[0] == "Science"]
    own = saved["Science", "Online practice"].assignment_id

    assert items[10].pair == ("Science", "Online practice")
    assert len(science) == 3
    assert reads[:2] == [
        ("claims", [own, *(name for name in science if name != own)]),
        ("instructions", [own, *(name for name in science if name != own)]),
    ]


def parents_row(
    store: ProjectStateStore, course: str, title: str, due: date | None, note: str | None
) -> str:
    """Homework a parent typed in, saved; its id."""
    entry = by_hand(course, title, due, None, AssignmentKind.HOMEWORK, note, now=NOW)
    assert keep((entry,), store, now=NOW, today=TODAY) == Kept(added=1, updated=0, unchanged=0)
    return entry.assignment_id


def test_a_dated_note_lands_on_the_one_row_of_its_name_due_that_day(
    tmp_path: pathlib.Path,
) -> None:
    """A parent's row of the note's class and title, due the day the note settles, takes
    the note: the note's date is one more claim there, and the row's title, due date and
    note and her own update stay as they are."""
    reading = map_project()
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        name = parents_row(store, "Humanities", "Map project", nov(4), "Bring the atlas")
        reported(store, "done", name)
        row = store.one_assignment(name)
        hers = store.student_reports(name)
        (change,) = changes_for((reading,), store)
        kept = keep((reading,), store, now=NOW, today=TODAY)
        after = store.one_assignment(name)
        claims = store.deadline_records(name)
        hers_after = store.student_reports(name)
        count = len(store.all_assignments())
    finally:
        store.close()

    assert row is not None
    assert after is not None
    assert change.state == CLAIMED
    assert change.on_record == row
    assert not change.identity_asked
    assert kept == Kept(added=0, updated=1, unchanged=0)
    assert count == 1
    assert (after.title, after.due_date, after.note) == ("Map project", nov(4), "Bring the atlas")
    assert (after.title, after.due_date, after.note) == (row.title, row.due_date, row.note)
    assert [(each.channel, each.asserted_value, each.seen_in) for each in claims] == [
        (SourceChannel.PARENT_ENTRY, "2026-11-04", None),
        (SourceChannel.LMS, "2026-11-04", TEACHER_NOTE),
    ]
    assert [update.status for update in hers] == ["done"]
    assert hers_after == hers


def test_a_dated_note_asks_about_the_one_row_of_its_name_due_another_day(
    tmp_path: pathlib.Path,
) -> None:
    """A row of the note's class and title due two days from the note's date is not taken
    for the note's homework: the note asks which homework it is, listing that row, and
    nothing is written. Answered, it lands there, its date is a claim beside the saved
    date, which stays; the same answer sent again writes nothing more, and the note read
    again is known there with no question."""
    reading = map_project()
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        name = parents_row(store, "Humanities", "Map project", nov(6), "Bring the atlas")
        (asked,) = changes_for((reading,), store)
        unanswered = keep((reading,), store, now=NOW, today=TODAY)
        nothing = store.deadline_records(name)
        answer = IdentityAnswer(name, asked.identity_shown, asked.identity_basis)
        (landed,) = changes_for((reading,), store, identities={0: answer})
        kept = keep((reading,), store, identities={0: answer}, now=NOW, today=TODAY)
        retried = keep((reading,), store, identities={0: answer}, now=NOW, today=TODAY)
        (again,) = changes_for((reading,), store)
        (row,) = store.all_assignments()
        claims = store.deadline_records(name)
    finally:
        store.close()

    assert (asked.identity_asked, asked.identity_question) == (True, "which")
    assert asked.identity_shown == (name,)
    assert asked.state == REVIEW
    assert isinstance(unanswered, list)
    assert len(nothing) == 1
    assert landed.state == CLAIMED
    assert landed.assignment_id == name
    assert kept == Kept(added=0, updated=1, unchanged=0)
    assert retried == Kept(added=0, updated=0, unchanged=1)
    assert (again.state, again.identity_asked, again.assignment_id) == (KNOWN, False, name)
    assert (row.title, row.due_date, row.note) == ("Map project", nov(6), "Bring the atlas")
    assert [(each.channel, each.asserted_value, each.seen_in) for each in claims] == [
        (SourceChannel.PARENT_ENTRY, "2026-11-06", None),
        (SourceChannel.LMS, "2026-11-04", TEACHER_NOTE),
    ]


def test_an_undated_note_asks_about_the_one_row_of_its_name_where_a_card_lands(
    tmp_path: pathlib.Path,
) -> None:
    """A note that settles no deadline is never put on the only row of its class and title
    by the name alone: it asks which homework it is. The same reading made from no note
    lands there with no question."""
    note = online_practice()
    card = dataclasses.replace(note, teacher_note=None)
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        name = parents_row(store, "Science", "Online practice", nov(12), None)
        (asked,) = changes_for((note,), store)
        held = keep((note,), store, now=NOW, today=TODAY)
        (landed,) = changes_for((card,), store)
        answer = IdentityAnswer(name, asked.identity_shown, asked.identity_basis)
        kept = keep((note,), store, identities={0: answer}, now=NOW, today=TODAY)
        retried = keep((note,), store, identities={0: answer}, now=NOW, today=TODAY)
        (row,) = store.all_assignments()
    finally:
        store.close()

    assert note.due_date is None
    assert (asked.identity_asked, asked.identity_question) == (True, "which")
    assert asked.identity_shown == (name,)
    assert isinstance(held, list)
    assert (landed.identity_asked, landed.assignment_id) == (False, name)
    assert landed.state != REVIEW
    assert kept == Kept(added=0, updated=1, unchanged=0)
    assert retried == Kept(added=0, updated=0, unchanged=1)
    assert row.due_date == nov(12)


def test_next_weeks_dated_note_of_the_same_words_asks_and_is_not_merged(
    tmp_path: pathlib.Path,
) -> None:
    """The same words first listed a week later settle a date a week from the saved one:
    the note is not the one saved before, and it asks whether it is the same assignment or
    new work. Nothing is written until that is answered, and answered as new work it is a
    row of its own, the first row's date unmoved."""
    first = map_project()
    later = note_reading(one_task("Humanities", nov(10), "Map project due on Wednesday"), now=NOW)
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        keep((first,), store, now=NOW, today=TODAY)
        (change,) = changes_for((later,), store)
        held = keep((later,), store, now=NOW, today=TODAY)
        one = [row.due_date for row in store.all_assignments()]
        kept = keep((later,), store, occurrences={0: NEW_WORK}, now=NOW, today=TODAY)
        two = sorted(row.due_date or date.max for row in store.all_assignments())
    finally:
        store.close()

    assert later.due_date == nov(11)
    assert (change.state, change.ambiguous, change.identity_asked) == (REVIEW, True, False)
    assert change.assignment_id == first.assignment_id
    assert change.beside == nov(4)
    assert isinstance(held, list)
    assert one == [nov(4)]
    assert kept == Kept(added=1, updated=0, unchanged=0)
    assert two == [nov(4), nov(11)]


def test_next_weeks_undated_note_of_the_same_words_asks_and_is_not_merged(
    tmp_path: pathlib.Path,
) -> None:
    """The same words with no settled deadline, first listed a week later, are not the note
    saved before and are not put on its row by the name: the note asks which homework it
    is, and nothing is written."""
    first = online_practice()
    later = note_reading(one_task("Science", nov(17), "Online practice due Tuesday"), now=NOW)
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        keep((first,), store, now=NOW, today=TODAY)
        (change,) = changes_for((later,), store)
        held = keep((later,), store, now=NOW, today=TODAY)
        count = len(store.all_assignments())
    finally:
        store.close()

    assert (first.due_date, later.due_date) == (None, None)
    assert (change.identity_asked, change.identity_question) == (True, "which")
    assert change.identity_shown == (first.assignment_id,)
    assert isinstance(held, list)
    assert count == 1


HERS = [
    pytest.param("Humanities", "Map project", nov(4), map_project, id="dated"),
    pytest.param("Science", "Online practice", None, online_practice, id="undated"),
]
"""Homework made from her own note, and the note reading of its class and title."""


@pytest.mark.parametrize(("course", "title", "due", "reading"), HERS)
def test_a_note_about_homework_made_from_her_note_asks_about_hers_first(
    tmp_path: pathlib.Path,
    course: str,
    title: str,
    due: date | None,
    reading: Callable[[], Reading],
) -> None:
    """Homework she made from her own note is never taken for the teacher's by its name or
    its date: the note asks whether it is hers, and nothing is written."""
    item = reading()
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        made = homework_from_a_note(store, course=course, title=title, due_date=due)
        (change,) = changes_for((item,), store)
        held = keep((item,), store, now=NOW, today=TODAY)
        claims = store.deadline_records(made)
    finally:
        store.close()

    assert item.due_date == due
    assert (change.identity_asked, change.identity_question) == (True, "hers")
    assert change.identity_shown == (made,)
    assert change.identity_hers == frozenset({made})
    assert isinstance(held, list)
    assert all(each.seen_in != TEACHER_NOTE for each in claims)


AFTER_A_DUE_CARD: Final = (
    WEEK + "11/04/2026 - Wednesday\n08 Geometry -\nPractice set 4 due Friday\n\n"
)
"""The week, then a note on Wednesday that names Practice set 4, whose cards were listed
on Monday and Tuesday: the note settles a date three days from the card's."""

AFTER_AN_ASSIGNED_CARD: Final = (
    WEEK + "Spanish - Assigned: Oral quiz: (Due:11/10/2026)\nSpanish -\nOral quiz Due.\n\n"
)
"""The week, then on Tuesday the Oral quiz card first listed on Monday, listed again, and a
note that names it and settles no date."""

NAMED_AGAIN = [
    pytest.param(AFTER_A_DUE_CARD, ("08 Geometry", "Practice set 4"), nov(3), nov(6), id="dated"),
    pytest.param(AFTER_AN_ASSIGNED_CARD, ("Spanish", "Oral quiz"), nov(10), None, id="undated"),
]
"""A text whose last note names a card of another school day: the text, the name, the
card's due date, and the date the note settles."""


def named_again(text: str) -> tuple[Reading, ...]:
    """The readings of the week with one more note after it: twelve, the note's last."""
    items = with_note_readings(read_text(text, now=NOW, today=TODAY), now=NOW)
    assert len(items) == 12
    return items


@pytest.mark.parametrize(("text", "name", "card_due", "note_due"), NAMED_AGAIN)
def test_a_note_naming_a_card_of_another_day_asks_and_nothing_is_written(
    tmp_path: pathlib.Path,
    text: str,
    name: tuple[str, str],
    card_due: date,
    note_due: date | None,
) -> None:
    """A note that names a card listed under other school days is a second reading of the
    name, however near its date or with none: it asks whether it is the same assignment or
    new work, the card's reading is as it is without the note, and a save writes nothing."""
    items = named_again(text)
    card, note = by_pair(items)[name]
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        changes = changes_for(items, store)
        without = changes_for(items[:11], store)
        held = keep(items, store, now=NOW, today=TODAY)
        saved = store.all_assignments()
    finally:
        store.close()
    asked = changes[11]

    assert (card.teacher_note, card.due_date) == (None, card_due)
    assert note.teacher_note is not None
    assert (note.due_date, items[11]) == (note_due, note)
    assert note.assignment_id != card.assignment_id
    assert (asked.state, asked.ambiguous, asked.identity_asked) == (REVIEW, True, False)
    assert (asked.on_record, asked.beside) == (None, card_due)
    assert asked.effect_base.startswith("This text also has it due ")
    assert asked.effect_base.endswith(
        "Say whether this is the same assignment or new work under the same name."
    )
    assert "due date changed" not in asked.effect_base
    assert [change.state for change in changes[:11]] == [NEW] * 11
    assert changes[:11] == without
    assert isinstance(held, list)
    assert saved == []


def test_an_undated_note_said_to_be_the_card_is_one_row_with_the_cards_due_date(
    tmp_path: pathlib.Path,
) -> None:
    """Answered as the same assignment, the note is folded into the card: one row of the
    name, the card's due date and its origin kept, the note's words kept as its
    instruction. Read again, both are known there and nothing is asked."""
    items = named_again(AFTER_AN_ASSIGNED_CARD)
    card, note = by_pair(items)["Spanish", "Oral quiz"]
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        changes = changes_for(items, store, occurrences={11: UPDATE})
        kept = keep(items, store, occurrences={11: UPDATE}, now=NOW, today=TODAY)
        saved = store.all_assignments()
        again = changes_for(items, store)
        second = keep(items, store, now=NOW, today=TODAY)
        instructions = store.school_instruction_readings([card.assignment_id])
    finally:
        store.close()
    quizzes = [row for row in saved if row.title == "Oral quiz"]

    assert [change.state for change in changes] == [NEW] * 11 + [FOLDED]
    assert changes[11].folded_into == items.index(card)
    assert kept == Kept(added=11, updated=0, unchanged=0)
    assert [(row.assignment_id, row.due_date) for row in quizzes] == [(card.assignment_id, nov(10))]
    assert quizzes[0].assigned_on == nov(2)
    assert quizzes[0].origins["due_date"] == SourceChannel.LMS
    assert [each.text for each in instructions.readable[card.assignment_id].kept] == [
        "Oral quiz Due."
    ]
    assert [change.state for change in again] == [KNOWN] * 12
    assert again[11].assignment_id == card.assignment_id
    assert second == Kept(added=0, updated=0, unchanged=11)
    assert note.assignment_id not in {row.assignment_id for row in saved}


@pytest.mark.parametrize(("text", "name", "card_due", "note_due"), NAMED_AGAIN)
def test_a_note_said_to_be_new_work_is_a_row_of_its_own_beside_the_cards(
    tmp_path: pathlib.Path,
    text: str,
    name: tuple[str, str],
    card_due: date,
    note_due: date | None,
) -> None:
    """Answered as new work, the note is saved as a second row of the name with an id of
    its own, and the card's row is as the card reads. The same text read again finds the
    note on its own row, asks nothing, and a save of it writes nothing."""
    items = named_again(text)
    card, _ = by_pair(items)[name]
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        changes = changes_for(items, store, occurrences={11: NEW_WORK})
        kept = keep(items, store, occurrences={11: NEW_WORK}, now=NOW, today=TODAY)
        saved = store.all_assignments()
        again = changes_for(items, store)
        second = keep(items, store, now=NOW, today=TODAY)
        count = len(store.all_assignments())
    finally:
        store.close()
    named = [row for row in saved if (row.course, row.title) == name]

    assert changes[11].state == NEW
    assert kept == Kept(added=12, updated=0, unchanged=0)
    assert [change.state for change in again] == [KNOWN] * 12
    assert again[11].assignment_id == changes[11].assignment_id
    assert second == Kept(added=0, updated=0, unchanged=12)
    assert count == 12
    assert len(saved) == 12
    assert len({row.assignment_id for row in named}) == 2
    assert {row.assignment_id: row.due_date for row in named} == {
        card.assignment_id: card_due,
        changes[11].assignment_id: note_due,
    }


TWO_NOTES: Final = (
    "Homework for Wren\n"
    "11/02/2026 - Monday\nArt -\nPoster Due.\n\n"
    "11/04/2026 - Wednesday\nArt -\nPoster due Friday\n\n"
)
"""Two notes of one class, on two days, that name the same work in different words: the
first settles no date."""


def test_a_second_note_of_a_name_with_no_date_beside_it_asks_in_words_of_its_own(
    tmp_path: pathlib.Path,
) -> None:
    """When the first reading of the name has no due date, the question names no date: it
    says the text names the work again in another note. The same second reading made from
    no note asks nothing, as any reading with no date beside it. Answered as the same
    assignment, the second note is folded into the first with the date it settles, and the
    words of both are put to the choice of which instructions apply."""
    items = with_note_readings(read_text(TWO_NOTES, now=NOW, today=TODAY), now=NOW)
    plain = (items[0], dataclasses.replace(items[1], teacher_note=None))
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        changes = changes_for(items, store)
        of_plain = changes_for(plain, store)
        folded = changes_for(items, store, occurrences={1: UPDATE})
    finally:
        store.close()

    assert [item.pair for item in items] == [("Art", "Poster")] * 2
    assert [item.due_date for item in items] == [None, nov(6)]
    assert items[0].assignment_id != items[1].assignment_id
    assert [change.state for change in changes] == [NEW, REVIEW]
    assert (changes[1].ambiguous, changes[1].beside) == (True, None)
    assert changes[1].effect_base == (
        "This text names it again in another note. Say whether it is the same assignment or "
        "new work under the same name."
    )
    assert [change.state for change in of_plain] == [NEW, NEW]
    assert [change.folded_into for change in folded] == [None, 0]
    assert (folded[0].assignment_id, folded[0].reading.due_date) == (
        items[0].assignment_id,
        nov(6),
    )
    assert folded[0].instructions_asked
    assert [seen.text for seen in folded[0].instructions_seen] == [
        "Poster Due.",
        "Poster due Friday",
    ]


TWO_LATER_NOTES: Final = (
    AFTER_A_DUE_CARD
    + "11/05/2026 - Thursday\n08 Geometry -\nPractice set 4 due Friday. Show your work.\n\n"
)
"""The week, then two notes on two later days that name Practice set 4 in different words
and settle the same date, Friday, November 6."""

BOTH_NEW: Final = {11: NEW_WORK, 12: NEW_WORK}
"""Each of the two later notes answered as new work."""


def two_later_notes() -> tuple[Reading, ...]:
    """The readings of the week with its two later notes: thirteen, the notes' last."""
    items = with_note_readings(read_text(TWO_LATER_NOTES, now=NOW, today=TODAY), now=NOW)
    assert [item.pair for item in items[11:]] == [("08 Geometry", "Practice set 4")] * 2
    assert [item.due_date for item in items[11:]] == [nov(6), nov(6)]
    return items


def test_two_notes_of_one_name_and_date_said_to_be_new_work_are_two_rows(
    tmp_path: pathlib.Path,
) -> None:
    """Two notes that name a card's work and settle the same date, each answered as new
    work, are a row each, with ids of their own beside the card's: each row keeps its own
    note's words as its one instruction, and no choice of instructions is asked. The same
    answers sent again write nothing, and the text read again is known, each note on its
    own row."""
    items = two_later_notes()
    card = by_pair(items)["08 Geometry", "Practice set 4"][0]
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        changes = changes_for(items, store, occurrences=BOTH_NEW)
        kept = keep(items, store, occurrences=BOTH_NEW, now=NOW, today=TODAY)
        saved = {row.assignment_id: row for row in store.all_assignments()}
        words = store.school_instruction_readings([change.assignment_id for change in changes])
        retried = keep(items, store, occurrences=BOTH_NEW, now=NOW, today=TODAY)
        again = changes_for(items, store)
        second = keep(items, store, now=NOW, today=TODAY)
        count = len(store.all_assignments())
    finally:
        store.close()
    one, other = (change.assignment_id for change in changes[11:])

    assert [change.state for change in changes] == [NEW] * 13
    assert not any(change.instructions_asked for change in changes)
    assert len({card.assignment_id, one, other}) == 3
    assert [one, other] == [item.assignment_id for item in items[11:]]
    assert kept == Kept(added=13, updated=0, unchanged=0)
    assert len(saved) == 13
    assert [(saved[name].title, saved[name].due_date) for name in (one, other)] == [
        ("Practice set 4", nov(6)),
        ("Practice set 4", nov(6)),
    ]
    assert saved[card.assignment_id].due_date == nov(3)
    assert {name: [each.text for each in words.readable[name].kept] for name in (one, other)} == {
        one: ["Practice set 4 due Friday"],
        other: ["Practice set 4 due Friday. Show your work."],
    }
    assert card.assignment_id not in words.readable
    assert retried == Kept(added=0, updated=0, unchanged=13)
    assert [change.state for change in again] == [KNOWN] * 13
    assert [change.assignment_id for change in again[11:]] == [one, other]
    assert second == Kept(added=0, updated=0, unchanged=13)
    assert count == 13


def test_the_same_two_readings_made_from_no_note_are_saved_as_they_read(
    tmp_path: pathlib.Path,
) -> None:
    """The two later readings with no note behind them are three days from the card, so
    nothing is asked and an answer of new work is not read: each is saved new under the id
    it reads with."""
    items = two_later_notes()
    plain = (*items[:11], *(dataclasses.replace(item, teacher_note=None) for item in items[11:]))
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        unanswered = changes_for(plain, store)
        changes = changes_for(plain, store, occurrences=BOTH_NEW)
        kept = keep(plain, store, occurrences=BOTH_NEW, now=NOW, today=TODAY)
        saved = {row.assignment_id: row.due_date for row in store.all_assignments()}
    finally:
        store.close()

    assert changes == unanswered
    assert [change.state for change in changes] == [NEW] * 13
    assert [change.occurrence for change in changes[11:]] == [None, None]
    assert [change.assignment_id for change in changes] == [item.assignment_id for item in plain]
    assert plain[11].assignment_id != plain[12].assignment_id
    assert kept == Kept(added=13, updated=0, unchanged=0)
    assert [saved[item.assignment_id] for item in plain[11:]] == [nov(6), nov(6)]


def test_a_note_said_to_be_new_work_has_an_id_from_its_first_day_and_its_words(
    tmp_path: pathlib.Path,
) -> None:
    """A note a week from the saved row of its name, answered as new work, makes a row
    whose id carries the note's first school day and a mark of its words, as a note named
    again in one text has it, whether or not the reading came with that mark. The same
    reading made from no note has the id that carries its date. Saved, the note's row
    stands beside the first, and the same answer sent again writes nothing."""
    first = map_project()
    later = note_reading(one_task("Humanities", nov(10), "Map project due on Wednesday"), now=NOW)
    mark = hashlib.sha256(b"Map project due on Wednesday").hexdigest()[:8]
    own = dataclasses.replace(later, occurrence=f"2026-11-10/{mark}")
    plain = dataclasses.replace(later, teacher_note=None)
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        keep((first,), store, now=NOW, today=TODAY)
        (noted,) = changes_for((later,), store, occurrences={0: NEW_WORK})
        (marked,) = changes_for((own,), store, occurrences={0: NEW_WORK})
        (dated,) = changes_for((plain,), store, occurrences={0: NEW_WORK})
        made = keep((later,), store, occurrences={0: NEW_WORK}, now=NOW, today=TODAY)
        retried = keep((later,), store, occurrences={0: NEW_WORK}, now=NOW, today=TODAY)
        saved = {row.assignment_id: row.due_date for row in store.all_assignments()}
    finally:
        store.close()

    assert made == Kept(added=1, updated=0, unchanged=0)
    assert retried == Kept(added=0, updated=0, unchanged=1)
    assert saved == {first.assignment_id: nov(4), noted.assignment_id: nov(11)}
    assert later.occurrence is None
    assert (noted.state, marked.state, dated.state) == (NEW, NEW, NEW)
    assert (noted.occurrence, marked.occurrence, dated.occurrence) == (NEW_WORK,) * 3
    assert noted.assignment_id == marked.assignment_id == own.assignment_id
    assert noted.assignment_id != first.assignment_id
    assert dated.assignment_id == dataclasses.replace(plain, occurrence="2026-11-11").assignment_id
    assert dated.assignment_id != noted.assignment_id


CARDS_ONLY = "Homework for Wren\n11/02/2026 - Monday\n08 Geometry - Due: Textbook page 12:\n"
"""A day of the weekly summary with one card and no teacher's note."""


def single(text: str) -> str:
    """A text with every run of spaces and line breaks made one space."""
    return " ".join(text.split())


def test_a_paste_with_no_note_comes_back_as_the_same_read() -> None:
    """A text with no teacher's note is handed back as the object it was read as, with
    nothing left out."""
    read = read_text(CARDS_ONLY, now=NOW, today=TODAY)

    assert read.notes == ()
    assert with_notes(read, now=NOW) is read
    assert read.left_out == frozenset()


def test_the_notes_read_as_tasks_take_their_lines_out_of_the_text_for_review() -> None:
    """With its notes read as tasks a paste holds the cards and the note readings, leaves
    out every note reading, and lists for review every line but the lines
    of the notes: the lines for review lose exactly those."""
    read = read_text(WEEK, now=NOW, today=TODAY)
    in_notes = {line for note in read.notes for line in note.lines}

    noted = with_notes(read, now=NOW)

    assert noted.items == with_note_readings(read, now=NOW)
    assert noted.left_out == left_out_by_default(noted.items) == frozenset(range(4, 11))
    assert noted.notes == read.notes
    assert len(in_notes) == 14
    assert in_notes <= {item.line for item in read.unread}
    assert noted.unread == tuple(item for item in read.unread if item.line not in in_notes)


def test_a_note_that_only_names_its_days_card_has_its_words_on_that_card() -> None:
    """A task that joins a card is carried by the card's reading, so words that are no
    instruction of the card are with it all the same; a card no note names carries none."""
    read = read_text(week_with("Short essay Due."), now=NOW, today=TODAY)
    (task,) = [
        item for item in read_notes([n.block for n in read.notes]) if item.name == "Short essay"
    ]

    noted = with_notes(read, now=NOW)
    (joined,) = by_pair(noted.items)["Religion", "Short essay"]

    assert joined.joined_notes == (task,)
    assert task.words == "Short essay Due."
    assert joined.teacher_note is None
    assert all(seen.text != task.words for seen in joined.instructions)
    assert [item.joined_notes for item in noted.items if item is not joined] == [()] * 10


ACCOUNTED = [
    pytest.param(WEEK, id="week"),
    pytest.param(week_with("Short essay Due."), id="a-note-that-only-names-a-card"),
    pytest.param(week_with("Short essay due today. Write it in ink."), id="a-note-that-says-more"),
]
"""Texts whose every note is read as tasks."""


@pytest.mark.parametrize("text", ACCOUNTED)
def test_every_line_of_the_text_is_listed_for_review_or_is_in_the_words_of_a_task(
    text: str,
) -> None:
    """No line goes unshown. Each line listed for review before the notes are read as tasks
    is listed for review after, or is a line of a note that became tasks, never both. Each
    such note's subject is the class of every reading made from it, and its kept lines,
    single-spaced, are made up of the words of those readings and of the tasks its cards
    carry, with nothing over."""
    read = read_text(text, now=NOW, today=TODAY)
    before = {item.line: item.text for item in read.unread}

    noted = with_notes(read, now=NOW)
    after = {item.line for item in noted.unread}
    became = [note for note in read.notes if not after & set(note.lines)]
    taken = {line for note in became for line in note.lines}
    carried = [
        (item, task)
        for item in noted.items
        for task in ((item.teacher_note,) if item.teacher_note else item.joined_notes)
    ]

    assert became == [note for note in read.notes if not note.block.suspect]
    assert after | taken == set(before)
    assert not after & taken
    assert len(became) == len(read.notes) > 0
    for note in became:
        block = note.block
        mine = [
            (item, task)
            for item, task in carried
            if task.subject == block.subject and block.day in task.seen_on
        ]
        rest = single(" ".join(before[line] for line in note.lines if line != block.line))
        assert mine
        for item, task in mine:
            assert single(item.course) == single(block.subject)
            assert single(task.words) in rest
            rest = rest.replace(single(task.words), " ", 1)
        assert rest.strip() == ""


HEAD = "Homework for Wren\n11/02/2026 - Monday\nScience -\nLab notes\n"
"""The summary's opening, a day, and a subject's note of one line."""

STAYS = [
    pytest.param("Homework for Wren\nArt -\nPaint words\n", ["Art -", "Paint words"], id="no-day"),
    pytest.param(
        HEAD + "Art -\nMusic -\n\n11/03/2026 - Tuesday\nSpanish -\n",
        ["Art -", "Music -", "Spanish -"],
        id="nothing-kept-under-a-heading",
    ),
    pytest.param(
        HEAD + "Math - Assigned: Sheet 3: (Due:11/06/2026)\nDue: Orphan:\nClasses\n",
        ["Due: Orphan:", "Classes"],
        id="a-short-paragraph-under-a-card",
    ),
    pytest.param(
        "Homework for Wren\n11/02/2026 - Monday\nArt -\nPaint words\n13/45/2026 - Monday\nMore\n",
        ["Art -", "Paint words", "13/45/2026 - Monday", "More"],
        id="a-suspect-note",
    ),
]
"""A text, and the lines of it that stay listed for review when its notes are read as tasks."""


@pytest.mark.parametrize(("text", "lines"), STAYS)
def test_a_line_that_is_no_task_stays_listed_for_review(text: str, lines: list[str]) -> None:
    """A heading with no day in force and its lines, a heading with nothing kept under it,
    a card's short paragraph, and a suspect note stay listed for review, and no reading is
    made from their words."""
    read = read_text(text, now=NOW, today=TODAY)

    noted = with_notes(read, now=NOW)
    listed = [item.text for item in noted.unread]
    words = [item.teacher_note.words for item in noted.items if item.teacher_note is not None]

    assert [line for line in lines if line not in listed] == []
    assert [line for line in lines if any(line in said for said in words)] == []
    assert [item.text for item in read.unread if item.text in lines] == lines
    assert noted.left_out == left_out_by_default(noted.items)


def test_a_homework_page_day_has_no_note_so_its_read_comes_back_as_it_is() -> None:
    """Under a day written as the homework page writes it no note is read, so the text is
    handed back as the object it was read as, every line where the reader put it."""
    page = "Tuesday 11/3/2026\nArt -\nPaint words\nDue: Orphan:\nMusic -\nSing words\n"
    read = read_text(page, now=NOW, today=TODAY)

    assert read.notes == ()
    assert with_notes(read, now=NOW) is read


def test_a_note_of_hers_ticked_on_a_reading_left_out_is_not_linked(
    tmp_path: pathlib.Path,
) -> None:
    """A link ticked on a reading that is left out of the save is no part of the save: it
    is not checked against her note as it stands, the ten other readings are saved, and
    nothing is linked."""
    items = week_items()
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        hers = waiting_note(
            store, course=items[8].course, title=items[8].title, text="Made-up words of hers"
        )
        kept = keep(
            items, store, left_out={8}, links={8: ((hers, 99, None),)}, now=NOW, today=TODAY
        )
        saved = rows(store)
    finally:
        store.close()

    assert kept == Kept(added=10, updated=0, unchanged=0)
    assert len(saved) == 10
    assert QUIZ not in saved


def test_what_a_page_said_of_a_typed_note_is_not_held_against_a_reading_left_out(
    tmp_path: pathlib.Path,
) -> None:
    """What a page signed about a typed note on a reading that is left out of the save is
    not compared with anything: the ten other readings are saved."""
    items = week_items()
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        kept = keep(
            items, store, left_out={8}, shown_notes={8: "kept:made-up"}, now=NOW, today=TODAY
        )
        saved = rows(store)
    finally:
        store.close()

    assert kept == Kept(added=10, updated=0, unchanged=0)
    assert len(saved) == 10


def test_a_dated_note_said_to_be_the_card_with_its_date_kept_leaves_the_cards_date(
    tmp_path: pathlib.Path,
) -> None:
    """A note that settles another date than the card of its name, answered as the same
    assignment with its due date kept, is folded into the card: one row of the name with
    the card's due date and where that date came from, and the note's date beside it as a
    claim. Answered as the same assignment with its due date changed, the row takes the
    note's date."""
    items = named_again(AFTER_A_DUE_CARD)
    card, note = by_pair(items)["08 Geometry", "Practice set 4"]
    answers = {}
    for answer in ("keep", UPDATE):
        store = ProjectStateStore.open(tmp_path / f"{answer}.sqlite3", fixture_clock())
        try:
            changes = changes_for(items, store, occurrences={11: answer})
            kept = keep(items, store, occurrences={11: answer}, now=NOW, today=TODAY)
            rows_of_name = [row for row in store.all_assignments() if row.title == card.title]
            claims = store.deadline_records(card.assignment_id)
            again = keep(items, store, occurrences={11: answer}, now=NOW, today=TODAY)
        finally:
            store.close()
        answers[answer] = (changes, kept, rows_of_name, claims, again)

    assert (card.due_date, note.due_date) == (nov(3), nov(6))
    for changes, kept, rows_of_name, claims, again in answers.values():
        assert [change.state for change in changes] == [NEW] * 11 + [FOLDED]
        assert kept == Kept(added=11, updated=0, unchanged=0)
        assert [row.assignment_id for row in rows_of_name] == [card.assignment_id]
        assert ("2026-11-06", TEACHER_NOTE) in {
            (each.asserted_value, each.seen_in) for each in claims
        }
        assert again == Kept(added=0, updated=0, unchanged=11)
    assert answers["keep"][0][11].occurrence == "keep"
    assert answers["keep"][2][0].due_date == nov(3)
    assert answers["keep"][2][0].origins["due_date"] == card.origin_of("due_date")
    assert answers[UPDATE][2][0].due_date == nov(6)


def test_a_card_answered_with_a_notes_choice_still_asks(tmp_path: pathlib.Path) -> None:
    """The choice to keep the due date is offered on a reading made from a teacher's note
    only: sent for a card, it is no answer, and the card's question stays open."""
    items = read_text(TWO_WEEKS, now=NOW, today=TODAY).items
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        changes = changes_for(items, store, occurrences={1: "keep"})
        held = keep(items, store, occurrences={1: "keep"}, now=NOW, today=TODAY)
        saved = store.all_assignments()
    finally:
        store.close()

    assert [change.state for change in changes] == [NEW, REVIEW]
    assert changes[1].ambiguous
    assert isinstance(held, list)
    assert saved == []


def test_a_dated_note_confirmed_as_a_row_the_family_dated_leaves_that_date_the_familys(
    tmp_path: pathlib.Path,
) -> None:
    """A note that settles a date, confirmed as homework whose due date the family entered,
    writes over nothing: the row keeps its due date and the family as where it came from,
    the note's date is a claim beside it, and the note read again, landing by the answer
    kept, changes nothing."""
    reading = map_project()
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        name = parents_row(store, "Humanities", "Map project", nov(6), None)
        before = store.one_assignment(name)
        (asked,) = changes_for((reading,), store)
        answer = IdentityAnswer(name, asked.identity_shown, asked.identity_basis)
        kept = keep((reading,), store, identities={0: answer}, now=NOW, today=TODAY)
        row = store.one_assignment(name)
        (again,) = changes_for((reading,), store)
        second = keep((reading,), store, now=NOW, today=TODAY)
        after = store.one_assignment(name)
        claims = store.deadline_records(name)
    finally:
        store.close()

    assert before is not None
    assert row is not None
    assert after is not None
    assert reading.due_date == nov(4)
    assert before.origins["due_date"] == SourceChannel.PARENT_ENTRY
    assert kept == Kept(added=0, updated=1, unchanged=0)
    assert (again.state, again.identity_asked) == (KNOWN, False)
    assert second == Kept(added=0, updated=0, unchanged=1)
    for each in (row, after):
        assert each.due_date == nov(6)
        assert each.origins["due_date"] == SourceChannel.PARENT_ENTRY
    assert [(each.channel, each.asserted_value, each.seen_in) for each in claims] == [
        (SourceChannel.PARENT_ENTRY, "2026-11-06", None),
        (SourceChannel.LMS, "2026-11-04", TEACHER_NOTE),
    ]


def test_a_dated_note_a_week_from_the_familys_date_keeps_or_changes_it_as_answered(
    tmp_path: pathlib.Path,
) -> None:
    """A note that settles a date a week or more from the one row of its name asks whether
    it is the same assignment. Answered with the due date kept, the row keeps the date the
    family entered and the family as where it came from, with the note's date a claim
    beside it; answered with the due date changed, the row takes the note's date."""
    reading = map_project()
    rows_after = {}
    for answer in ("keep", UPDATE):
        store = ProjectStateStore.open(tmp_path / f"{answer}.sqlite3", fixture_clock())
        try:
            name = parents_row(store, "Humanities", "Map project", nov(20), None)
            (asked,) = changes_for((reading,), store)
            kept = keep((reading,), store, occurrences={0: answer}, now=NOW, today=TODAY)
            rows_after[answer] = (asked, kept, store.one_assignment(name))
            claims = store.deadline_records(name)
        finally:
            store.close()
        assert ("2026-11-04", TEACHER_NOTE) in {
            (each.asserted_value, each.seen_in) for each in claims
        }

    for asked, kept, row in rows_after.values():
        assert (asked.state, asked.ambiguous, asked.beside) == (REVIEW, True, nov(20))
        assert kept == Kept(added=0, updated=1, unchanged=0)
        assert row is not None
    kept_row, changed_row = rows_after["keep"][2], rows_after[UPDATE][2]
    assert kept_row is not None
    assert changed_row is not None
    assert kept_row.due_date == nov(20)
    assert kept_row.origins["due_date"] == SourceChannel.PARENT_ENTRY
    assert changed_row.due_date == nov(4)


def test_a_later_listing_of_an_undated_note_asks_again_after_an_earlier_answer(
    tmp_path: pathlib.Path,
) -> None:
    """An undated note said to be a row on record is kept there under its words and school
    day: the same answer sent again writes nothing, and the same text read again is known
    with no question. The same words first listed a week later are another occurrence,
    which the earlier answer does not place: the note asks which homework it is, and
    nothing is written until that is answered. Its answer sent twice writes nothing more,
    and the first listing read again is known as before."""
    first = online_practice()
    later = note_reading(one_task("Science", nov(17), "Online practice due Tuesday"), now=NOW)
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        name = parents_row(store, "Science", "Online practice", nov(12), None)
        (asked,) = changes_for((first,), store)
        answer = IdentityAnswer(name, asked.identity_shown, asked.identity_basis)
        kept = keep((first,), store, identities={0: answer}, now=NOW, today=TODAY)
        retried = keep((first,), store, identities={0: answer}, now=NOW, today=TODAY)
        (again,) = changes_for((first,), store)
        (next_week,) = changes_for((later,), store)
        held = keep((later,), store, now=NOW, today=TODAY)
        rows_held = store.all_assignments()
        reply = IdentityAnswer(name, next_week.identity_shown, next_week.identity_basis)
        answered = keep((later,), store, identities={0: reply}, now=NOW, today=TODAY)
        twice = keep((later,), store, identities={0: reply}, now=NOW, today=TODAY)
        (first_again,) = changes_for((first,), store)
        row = store.one_assignment(name)
    finally:
        store.close()

    assert (first.due_date, later.due_date) == (None, None)
    assert kept == Kept(added=0, updated=1, unchanged=0)
    assert retried == Kept(added=0, updated=0, unchanged=1)
    assert (again.state, again.identity_asked, again.assignment_id) == (KNOWN, False, name)
    assert (next_week.state, next_week.identity_asked) == (REVIEW, True)
    assert next_week.identity_question == "which"
    assert next_week.identity_shown == (name,)
    assert isinstance(held, list)
    assert [each.assignment_id for each in rows_held] == [name]
    assert isinstance(answered, Kept)
    assert twice == Kept(added=0, updated=0, unchanged=1)
    assert (first_again.state, first_again.identity_asked) == (KNOWN, False)
    assert row is not None
    assert row.due_date == nov(12)


def answered(*answers: TaskAnswer) -> Read:
    """A paste of three readings, a dated note, an undated note due on a weekday either
    Tuesday may mean, and a card, with the answers sent for them applied."""
    read = Read(items=(map_project(), online_practice(), cards()[0]), unread=())
    return with_answers(read, answers, now=NOW, today=TODAY)


DATE_ANSWERS = [
    pytest.param(TaskAnswer(1, when="2026-11-17"), 1, nov(17), id="set"),
    pytest.param(TaskAnswer(0, when="other", other="2026-11-06"), 0, nov(6), id="correct"),
    pytest.param(TaskAnswer(1, when="2026-11-10"), 1, nov(10), id="choose-a-tuesday"),
]
"""Answers that give a note reading a date of the family's: set on an undated note, typed
beside another date for a dated one, and one of the two dates a weekday may mean."""


@pytest.mark.parametrize(("answer", "key", "day"), DATE_ANSWERS)
def test_a_date_the_family_answers_is_the_familys_entry(
    answer: TaskAnswer, key: int, day: date
) -> None:
    """The reading takes the date, claimed by the parent's entry, with the due date's origin
    the parent's entry, and nothing else of the paste changes."""
    read = answered(answer)
    reading = read.items[key]

    assert reading.due_date == day
    assert said(reading) == [(SourceChannel.PARENT_ENTRY, day.isoformat(), None, FAMILY_CONFIDENCE)]
    assert reading.origin_of("due_date") is SourceChannel.PARENT_ENTRY
    assert reading.title == answered().items[key].title
    assert [item for place, item in enumerate(read.items) if place != key] == [
        item for place, item in enumerate(answered().items) if place != key
    ]
    assert read.dates_unread == frozenset()


def test_a_named_date_chosen_is_the_familys_entry() -> None:
    """A date the note names outside a deadline statement, chosen, is the family's entry."""
    task = one_task("Humanities", nov(2), "Study for the vocabulary quiz on Thursday.")
    read = with_answers(
        Read(items=(note_reading(task, now=NOW),), unread=()),
        (TaskAnswer(0, when="2026-11-05"),),
        now=NOW,
        today=TODAY,
    )

    assert task.named == (nov(5),)
    assert read.items[0].due_date == nov(5)
    assert read.items[0].origin_of("due_date") is SourceChannel.PARENT_ENTRY


def test_no_due_date_chosen_clears_the_notes_date_and_its_claim() -> None:
    """No due date for a dated note: no due date, no claim, and no origin for a due date."""
    reading = answered(TaskAnswer(0, when="none")).items[0]

    assert (reading.due_date, reading.claims) == (None, ())
    assert "due_date" not in reading.field_origins


@pytest.mark.parametrize(
    "answer",
    [
        TaskAnswer(0, when="2026-11-04"),
        TaskAnswer(0, when="other", other="2026-11-04"),
        TaskAnswer(1, when="none"),
        TaskAnswer(0),
        TaskAnswer(0, title="   "),
        TaskAnswer(0, title=" Map   project "),
    ],
    ids=["as-read", "as-read-typed", "none-where-none", "nothing", "blank-title", "same-title"],
)
def test_an_answer_that_changes_nothing_leaves_the_reading_the_schools(answer: TaskAnswer) -> None:
    """The date as read, chosen or typed, no date where none was read, nothing chosen, a blank
    title and the title as read are no correction: the reading stays as read."""
    read = answered(answer)

    assert read.items == answered().items
    assert said(read.items[0]) == [
        (SourceChannel.LMS, "2026-11-04", TEACHER_NOTE, PORTAL_CONFIDENCE)
    ]
    assert read.dates_unread == frozenset()


def test_a_corrected_title_is_the_readings_title_and_suggests_its_kind() -> None:
    """A title typed is the reading's title, single-spaced, with the kind it suggests; the
    date and its claim stay the school's."""
    reading = answered(TaskAnswer(0, title=" Bring the   binder ")).items[0]

    assert reading.title == "Bring the binder"
    assert reading.kind is AssignmentKind.TASK
    assert reading.course == "Humanities"
    assert said(reading) == [(SourceChannel.LMS, "2026-11-04", TEACHER_NOTE, PORTAL_CONFIDENCE)]
    assert reading.assignment().title == "Bring the binder"


@pytest.mark.parametrize("other", ["", "11/06/2026", "someday", "2030-01-01"])
def test_another_date_that_reads_as_no_date_is_not_applied(other: str) -> None:
    """Another date left empty, not a date, or more than a year away is not applied, and the
    paste says which reading's answer it was."""
    read = answered(TaskAnswer(0, when="other", other=other), TaskAnswer(1, when="2026-11-10"))

    assert read.items[0] == answered().items[0]
    assert read.items[1].due_date == nov(10)
    assert read.dates_unread == frozenset({0})


def test_answers_for_a_card_or_no_reading_are_ignored_and_the_rest_kept() -> None:
    """An answer for a reading made from no note, or for a key with no reading, changes
    nothing; the answers for note readings are kept with the paste as sent."""
    kept = TaskAnswer(0, title="Map of the river", when="other", other="2026-11-06")
    read = answered(TaskAnswer(2, title="Other"), TaskAnswer(7, when="none"), kept)

    assert read.items[2] == answered().items[2]
    assert read.answers == (kept,)
