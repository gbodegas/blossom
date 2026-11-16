# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A teacher's note on a homework screenshot read as rows.

A note card has a course and words under a day heading, and no label and no due line. Its
words come from its title, its instruction, or both, since which field the reader uses for a
note is not known. Every card here is made up, as a fake reader would return it.
"""

import itertools
from datetime import UTC, date, datetime

import pytest

from blossom import screenshot_page
from blossom import screenshot_review as review
from blossom.intake import TEACHER_NOTE, Reading
from blossom.reconciliation import SourceChannel
from blossom.routes import inbox
from blossom.school_instructions import InstructionSeen
from blossom.screenshot_review import Piece, SavedRow
from blossom.screenshots import Assembly, ScreenshotRow, homework_assembly, homework_rows
from blossom.stores.import_drafts import StoredReading
from blossom.teacher_notes import Standing
from tests.support import homework_answer, homework_card, stored_homework

NOW = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)
MONDAY, TUESDAY = "Monday 11/16/2026", "Tuesday 11/17/2026"
PROJECT = "The class project is due today."
STUDY, READY = "Study the vocabulary on page 12.", "Be ready to form sentences with the new words."
FORM = "Bring the signed form by Tuesday."


def note(
    course: str,
    title: str | None = None,
    instruction: str | None = None,
    y_top: int = 200,
    **fields: object,
) -> dict[str, object]:
    """A badge-less card with no due line unless one is given: a teacher's note as the reader
    gives it."""
    shape: dict[str, object] = {"badge": None, "footer": None, "y_bottom": y_top + 80}
    return homework_card(
        course=course, title=title, instruction=instruction, y_top=y_top, **(shape | fields)
    )


def formal(course: str, title: str, y_top: int) -> dict[str, object]:
    """A DUE card with no instruction."""
    return homework_card(
        badge="DUE",
        course=course,
        title=title,
        instruction=None,
        footer=None,
        y_top=y_top,
        y_bottom=y_top + 80,
    )


def shot(*cards: dict[str, object], day: str | None = MONDAY) -> dict[str, object]:
    headings = [] if day is None else [{"text": day, "y": 150, "uncertain": None}]
    return homework_answer(headings=headings, cards=list(cards))


def stored(label: str, *shots: dict[str, object]) -> list[StoredReading]:
    return [stored_homework(f"{label}-{place}", answer) for place, answer in enumerate(shots)]


def assembled(label: str, *shots: dict[str, object]) -> Assembly:
    """The assembly of the screenshots, the same in every order of their images."""
    images = stored(label, *shots)
    found = [homework_assembly(list(order), now=NOW) for order in itertools.permutations(images)]
    assert all(each == found[0] for each in found)
    return found[0]


def only_row(assembly: Assembly) -> ScreenshotRow:
    assert assembly.unread == ()
    [row] = assembly.rows
    return row


def standing(row: ScreenshotRow) -> Standing:
    task = row.reading.teacher_note
    assert task is not None
    return task.standing


# ------------------------------------------------------------------ where the words are


def test_a_note_in_the_title_reads_as_one_dated_homework_row() -> None:
    row = only_row(assembled("title", shot(note("Humanities 8", title=PROJECT))))

    assert (row.reading.course, row.reading.title) == ("Humanities 8", "The class project")
    assert row.reading.due_date == date(2026, 11, 16)
    assert [(c.channel, c.asserted_value, c.seen_in) for c in row.reading.claims] == [
        (SourceChannel.LMS, "2026-11-16", TEACHER_NOTE)
    ]
    assert row.reading.instructions == (
        InstructionSeen(PROJECT, SourceChannel.LMS, None, date(2026, 11, 16)),
    )
    assert standing(row) is Standing.HOMEWORK
    assert row.layer == 0


def test_the_same_words_in_the_instruction_give_the_same_reading_and_key() -> None:
    in_title = only_row(assembled("title", shot(note("Humanities 8", title=PROJECT))))
    in_instruction = only_row(assembled("words", shot(note("Humanities 8", instruction=PROJECT))))

    assert in_instruction.reading == in_title.reading
    assert in_instruction.key == in_title.key


def test_a_note_split_across_both_fields_is_one_task_wherever_its_lines_are() -> None:
    split = only_row(assembled("split", shot(note("Spanish 8", title=STUDY, instruction=READY))))
    in_title = only_row(assembled("title", shot(note("Spanish 8", title=f"{STUDY} {READY}"))))
    in_words = only_row(assembled("words", shot(note("Spanish 8", instruction=f"{STUDY} {READY}"))))

    assert split.reading.title == "Study the vocabulary on page 12"
    assert split.reading.due_date is None
    assert split.reading.claims == ()
    assert standing(split) is Standing.HOMEWORK
    assert in_title.reading == split.reading
    assert in_words.reading == split.reading
    assert in_title.key == in_words.key == split.key


def test_the_same_sentence_in_both_fields_is_one_task() -> None:
    both = only_row(
        assembled("both", shot(note("Humanities 8", title=PROJECT, instruction=PROJECT)))
    )
    title = only_row(assembled("title", shot(note("Humanities 8", title=PROJECT))))

    assert both.reading == title.reading
    assert both.key == title.key


def test_a_title_that_begins_the_instruction_is_kept_once() -> None:
    begun = only_row(
        assembled("begun", shot(note("Spanish 8", title=STUDY, instruction=f"{STUDY} {READY}")))
    )
    split = only_row(assembled("split", shot(note("Spanish 8", title=STUDY, instruction=READY))))

    assert begun.reading == split.reading


# ------------------------------------------------------------------ standing


def test_an_unclear_note_and_a_no_homework_note_are_rows_of_their_standing() -> None:
    unclear = only_row(assembled("unclear", shot(note("Math 8", title="Practice set"))))
    none = only_row(
        assembled("none", shot(note("Science 8", instruction="No additional homework assigned.")))
    )

    assert (unclear.reading.title, standing(unclear)) == ("Practice set", Standing.UNCLEAR)
    assert standing(none) is Standing.NOT_ASSIGNMENT
    assert unclear.reading.due_date is None
    assert none.reading.due_date is None


# ------------------------------------------------------------------ cards that stay unread


def test_note_shaped_cards_that_cannot_be_read_as_notes_stay_items() -> None:
    cases = {
        "no_day": shot(note("Humanities 8", title=PROJECT), day=None),
        "incomplete": shot(note("Humanities 8", title=PROJECT, footer="Due (11/18/2026)")),
        "unsure": shot(
            note(
                "Humanities 8",
                title=PROJECT,
                uncertain=[{"field": "badge", "reason": "faint"}],
            )
        ),
        "clipped": shot(note("Humanities 8", title=PROJECT, clipped=True)),
    }
    reasons = {label: assembled(label, answer) for label, answer in cases.items()}

    assert {label: found.rows for label, found in reasons.items()} == dict.fromkeys(cases, ())
    assert {label: [i.reason for i in found.unread] for label, found in reasons.items()} == {
        "no_day": ["no_day"],
        "incomplete": ["incomplete"],
        "unsure": ["incomplete"],
        "clipped": ["clipped"],
    }


SHAPED = [
    pytest.param(None, f"{STUDY}\n11/18/2026 - Wednesday", id="a-day"),
    pytest.param(None, f"{STUDY}\n11/12 Math 8: Homework: Sheet Grade: Missing", id="a-missing"),
    pytest.param(f"{STUDY}\nMath 8 - Due: Worksheet 3:", None, id="a-card-in-the-title"),
    pytest.param(
        None, f"{STUDY}\nOn Tue, Nov 17, 2026 at 9:14 AM The School wrote:", id="a-mail-date"
    ),
]
"""A note card's title and instruction, a line of one shaped as a day, a Missing line, a card
or a mail program's date line."""


@pytest.mark.parametrize(("title", "instruction"), SHAPED)
def test_a_note_card_with_a_line_shaped_as_structure_is_a_card_that_could_not_be_read(
    title: str | None, instruction: str | None
) -> None:
    found = assembled("shaped", shot(note("Spanish 8", title=title, instruction=instruction)))

    assert found.rows == ()
    assert [item.reason for item in found.unread] == ["card_unreadable"]


def test_the_same_words_on_one_line_are_a_note() -> None:
    words = f"{STUDY} 11/18/2026 - Wednesday"
    row = only_row(assembled("one-line", shot(note("Spanish 8", instruction=words))))

    assert row.reading.teacher_note is not None


# ------------------------------------------------------------------ days and copies


def test_a_weekday_is_read_from_the_earliest_school_day_the_note_is_listed_under() -> None:
    both = assembled(
        "form",
        shot(note("Humanities 8", instruction=FORM)),
        shot(note("Humanities 8", instruction=FORM), day=TUESDAY),
    )
    row = only_row(both)
    images = {image.sha256 for image in stored("form", shot(), shot())}

    assert row.reading.due_date == date(2026, 11, 17)
    assert {place.sha256 for place in row.sources} == images
    task = row.reading.teacher_note
    assert task is not None
    assert task.seen_on == (date(2026, 11, 16), date(2026, 11, 17))


def test_the_same_note_in_the_next_school_week_is_a_row_of_its_own() -> None:
    found = assembled(
        "weeks",
        shot(note("Humanities 8", title=PROJECT), day=TUESDAY),
        shot(note("Humanities 8", title=PROJECT), day="Monday 11/23/2026"),
    )
    tasks = sorted(
        (row.reading.teacher_note for row in found.rows if row.reading.teacher_note is not None),
        key=lambda task: task.first_day,
    )

    assert found.unread == ()
    assert len(found.rows) == 2
    assert [(task.seen_on, task.deadline) for task in tasks] == [
        ((date(2026, 11, 17),), date(2026, 11, 17)),
        ((date(2026, 11, 23),), date(2026, 11, 23)),
    ]


def test_on_its_own_a_weekday_equal_to_the_school_day_offers_both_weeks() -> None:
    row = only_row(assembled("tuesday", shot(note("Humanities 8", instruction=FORM), day=TUESDAY)))

    task = row.reading.teacher_note
    assert task is not None
    assert row.reading.due_date is None
    assert task.choices == (date(2026, 11, 17), date(2026, 11, 24))


def test_two_tasks_in_one_card_are_two_rows_from_that_card() -> None:
    words = "Map project due Wednesday. Timeline poster due Thursday."
    found = assembled("two", shot(note("Social Studies 8", instruction=words)))

    assert found.unread == ()
    assert sorted((row.reading.title, row.reading.due_date) for row in found.rows) == [
        ("Map project", date(2026, 11, 18)),
        ("Timeline poster", date(2026, 11, 19)),
    ]
    assert {row.sources for row in found.rows} == {found.rows[0].sources}
    assert len(found.rows[0].sources) == 1


def test_two_identical_notes_in_one_screenshot_are_two_rows() -> None:
    found = assembled(
        "twice",
        shot(note("Math 8", title="Practice set"), note("Math 8", title="Practice set", y_top=300)),
    )

    assert found.unread == ()
    assert sorted(row.layer for row in found.rows) == [0, 1]
    assert len({row.key for row in found.rows}) == 2


def test_the_same_note_in_overlapping_screenshots_is_one_row() -> None:
    found = assembled(
        "overlap",
        shot(
            formal("Math 8", "Worksheet 3", 200), note("Humanities 8", instruction=FORM, y_top=300)
        ),
        shot(note("Humanities 8", instruction=FORM, y_top=200)),
    )
    notes = [row for row in found.rows if row.reading.teacher_note is not None]

    assert found.unread == ()
    assert len(notes) == 1
    assert len(notes[0].sources) == 2


# ------------------------------------------------------------------ formal rows


FORMAL_KEYS = {
    "b8e727e81ec318af848234c0f5eb1022b16e2648f2bd35d395c3ba58bac6660b",
    "b9975595dd5e3ceda7bb7f57e75f00dbb4428ec7f25d4788f644b33a3cd5a031",
}
"""The keys of the two rows of the default made-up homework list, as literal values."""
WORKSHEET_KEY = "7a1f6033db7528438a4b5f18c9fe5dc06ddf54036bbd52b610b68b2b7fb5f750"
"""The key of Worksheet 3 due Monday 11/16/2026, as a literal value."""


def test_formal_row_keys_are_the_same_with_or_without_a_note_card() -> None:
    plain = shot(formal("Math 8", "Worksheet 3", 200), formal("Science 8", "Lab quiz", 300))
    with_note = shot(
        formal("Math 8", "Worksheet 3", 200),
        note("Humanities 8", title=PROJECT, y_top=250),
        formal("Science 8", "Lab quiz", 300),
    )

    def formal_keys(found: Assembly) -> set[str]:
        return {row.key for row in found.rows if row.reading.teacher_note is None}

    assert formal_keys(assembled("plain", plain)) == formal_keys(assembled("noted", with_note))
    assert len(formal_keys(assembled("plain", plain))) == 2


def test_three_formal_keys_are_the_literal_keys_pinned_here() -> None:
    worksheet = shot(formal("Math 8", "Worksheet 3", 200))

    assert {row.key for row in homework_rows(stored("default", homework_answer()))} == FORMAL_KEYS
    assert [row.key for row in homework_rows(stored("worksheet", worksheet))] == [WORKSHEET_KEY]


# ------------------------------------------------------------------ a note naming its card


ESSAY_DUE = "Short essay Due."
ESSAY_MORE = "Short essay due Wednesday. Write 250 words on your question."


def essay(y_top: int = 200) -> dict[str, object]:
    """Religion 8's "Short essay", assigned on the day it is listed under and due 11/18."""
    return homework_card(
        badge="ASSIGNED",
        course="Religion 8",
        title="Short essay",
        instruction=None,
        footer="Due (11/18/2026)",
        y_top=y_top,
        y_bottom=y_top + 80,
    )


def essay_alone(label: str) -> ScreenshotRow:
    """The essay card's row read with no note, from an image of the same label."""
    return only_row(assembled(label, shot(essay())))


def two_days(*cards: dict[str, object]) -> dict[str, object]:
    """One screenshot with Monday 11/16 at the top and Tuesday 11/17 from y 250 down."""
    headings = [
        {"text": MONDAY, "y": 150, "uncertain": None},
        {"text": TUESDAY, "y": 250, "uncertain": None},
    ]
    return homework_answer(headings=headings, cards=list(cards))


def test_a_note_naming_its_card_under_the_same_day_is_that_card_s_row() -> None:
    faint = [{"field": "title", "reason": "faint"}]
    found = assembled(
        "essay", shot(essay(), note("Religion 8", title=ESSAY_DUE, y_top=300, uncertain=faint))
    )

    row = only_row(found)
    assert row.reading.teacher_note is None
    assert row == essay_alone("essay")


def test_a_note_naming_a_card_of_another_day_stays_a_note_row() -> None:
    cases = {
        "one-shot": [two_days(essay(), note("Religion 8", title=ESSAY_DUE, y_top=300))],
        "two-shots": [shot(essay()), shot(note("Religion 8", title=ESSAY_DUE), day=TUESDAY)],
    }
    for label, shots in cases.items():
        found = assembled(label, *shots)
        formal_rows = [row for row in found.rows if row.reading.teacher_note is None]
        notes = [row for row in found.rows if row.reading.teacher_note is not None]

        assert found.unread == ()
        assert [row.key for row in formal_rows] == [essay_alone(label).key]
        assert formal_rows[0].reading == essay_alone(label).reading
        assert [(row.reading.course, row.reading.title) for row in notes] == [
            ("Religion 8", "Short essay")
        ]
        task = notes[0].reading.teacher_note
        assert task is not None
        assert task.seen_on == (date(2026, 11, 17),)


def test_a_note_that_says_more_adds_its_deadline_claim_and_its_words_to_the_card() -> None:
    found = assembled("more", shot(essay(), note("Religion 8", instruction=ESSAY_MORE, y_top=300)))
    alone = essay_alone("more")

    row = only_row(found)
    assert row.reading.teacher_note is None
    assert (row.key, row.sources, row.marks, row.layer) == (
        alone.key,
        alone.sources,
        alone.marks,
        alone.layer,
    )
    assert row.reading.due_date == date(2026, 11, 18)
    assert [(c.channel, c.asserted_value, c.seen_in) for c in row.reading.claims] == [
        (c.channel, c.asserted_value, c.seen_in) for c in alone.reading.claims
    ] + [(SourceChannel.LMS, "2026-11-18", TEACHER_NOTE)]
    assert row.reading.instructions == (
        *alone.reading.instructions,
        InstructionSeen(ESSAY_MORE, SourceChannel.LMS, None, date(2026, 11, 16)),
    )


def test_a_note_in_another_class_with_the_card_s_title_stays_a_note_row() -> None:
    found = assembled("other", shot(essay(), note("English 8", title=ESSAY_DUE, y_top=300)))

    assert found.unread == ()
    assert sorted((row.reading.course, row.reading.teacher_note is None) for row in found.rows) == [
        ("English 8", False),
        ("Religion 8", True),
    ]
    [formal_row] = [row for row in found.rows if row.reading.teacher_note is None]
    assert formal_row == essay_alone("other")


def test_a_joined_card_keeps_its_literal_key_in_every_order_of_the_images() -> None:
    worksheet = shot(formal("Math 8", "Worksheet 3", 200))
    named = shot(note("Math 8", title="Worksheet 3 Due.", y_top=200))

    found = assembled("joined", worksheet, named)

    assert found.unread == ()
    assert [row.key for row in found.rows] == [WORKSHEET_KEY]
    assert [row.key for row in homework_rows(stored("joined", worksheet, named))] == [WORKSHEET_KEY]


# ------------------------------------------------------------------ the text path's words


def test_the_note_headings_and_helpers_are_the_text_path_s_words() -> None:
    said = screenshot_page.SAID
    assert (said["may_be_homework"], said["may_be_homework_helper"]) == (
        inbox.MAY_BE_HOMEWORK,
        inbox.MAY_BE_HOMEWORK_HELPER,
    )
    assert (said["no_homework"], said["no_homework_helper"]) == (
        inbox.NO_HOMEWORK,
        inbox.NO_HOMEWORK_HELPER,
    )


# ------------------------------------------------------------------ corrections


def project_reading() -> Reading:
    """Humanities 8's note "The class project is due today." under Monday 11/16/2026."""
    return only_row(assembled("title", shot(note("Humanities 8", title=PROJECT)))).reading


def test_a_typed_date_on_a_note_is_the_family_s_entry() -> None:
    fixed = review.corrected(project_reading(), None, {"due": "2026-11-18"}, now=NOW)

    assert fixed.due_date == date(2026, 11, 18)
    assert [(c.channel, c.asserted_value, c.seen_in) for c in fixed.claims] == [
        (SourceChannel.PARENT_ENTRY, "2026-11-18", None)
    ]
    assert fixed.field_origins["due_date"] is SourceChannel.PARENT_ENTRY


def test_the_date_as_read_typed_on_a_note_leaves_the_school_s_claim() -> None:
    read = project_reading()

    assert review.corrected(read, None, {"due": "2026-11-16"}, now=NOW) == read
    assert [c.seen_in for c in read.claims] == [TEACHER_NOTE]


def test_a_cleared_date_on_a_note_leaves_no_due_date_and_no_claim() -> None:
    fixed = review.corrected(project_reading(), None, {"due": None}, now=NOW)

    assert fixed.due_date is None
    assert fixed.claims == ()
    assert "due_date" not in fixed.field_origins


def test_a_cleared_date_on_a_formal_reading_is_passed_over() -> None:
    row = only_row(assembled("worksheet", shot(formal("Math 8", "Worksheet 3", 200))))

    assert review.corrected(row.reading, None, {"due": None}, now=NOW) == row.reading


# ------------------------------------------------------------------ recognized by words and day


def note_piece(label: str, answer: dict[str, object]) -> Piece:
    [piece], _, _ = review.pieces_from(stored(label, answer), {}, frozenset(), NOW)
    return piece


def poster(piece: Piece, day: date) -> SavedRow:
    """The Humanities note saved as "Class project poster" due 11/18, its words kept under
    ``day``."""
    return SavedRow(
        "assignment-class-project-poster",
        "Humanities 8",
        "Class project poster",
        date(2026, 11, 18),
        None,
        piece.words,
        noted=frozenset({(PROJECT, day)}),
    )


def test_a_note_lists_offers_and_preselects_the_row_holding_its_words_and_school_day() -> None:
    piece = note_piece("again", shot(note("Humanities 8", title=PROJECT)))
    saved = poster(piece, date(2026, 11, 16))
    rows = {saved.assignment_id: saved}

    assert review.listed(piece, [saved]) == [saved]
    assert review.offerable(piece, {"existing": saved.assignment_id}, rows, [])
    assert review.defaults([piece], {piece.pair: [piece]}, rows, [], {}, lambda p, a: True) == {
        piece.name: {"existing": saved.assignment_id}
    }


def test_next_week_s_identical_note_is_not_that_row() -> None:
    piece = note_piece("next", shot(note("Humanities 8", title=PROJECT), day="Monday 11/23/2026"))
    saved = poster(piece, date(2026, 11, 16))
    rows = {saved.assignment_id: saved}

    assert review.listed(piece, [saved]) == []
    assert not review.offerable(piece, {"existing": saved.assignment_id}, rows, [])
    assert review.defaults([piece], {piece.pair: [piece]}, rows, [], {}, lambda p, a: True) == {
        piece.name: {"new": 1}
    }


def test_a_formal_piece_never_lists_a_row_by_its_noted_words() -> None:
    piece = note_piece("worksheet", shot(formal("Math 8", "Worksheet 3", 200)))
    saved = SavedRow(
        "assignment-other",
        "Math 8",
        "Other",
        piece.due,
        None,
        frozenset(),
        noted=frozenset({("Worksheet 3", date(2026, 11, 16))}),
    )

    assert review.listed(piece, [saved]) == []
