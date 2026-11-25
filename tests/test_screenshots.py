# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Checking a screenshot reading's answer before anything of it is kept.

Every answer here is synthetic, written as a fake reader would return it: the parsed JSON of
the answer schema. The student is Wren; every other name is invented.
"""

import copy
import hashlib
import itertools
import json
import logging
from datetime import UTC, date, datetime
from typing import Any, get_args

import pytest

from blossom import screenshots
from blossom.intake import (
    DAY_HEADER,
    OWN_LINE,
    CardsRead,
    PortalCard,
    Reading,
    identity,
    read_cards,
)
from blossom.screenshot_prompt import PROMPT_VERSION, SCHEMA, SCHEMA_VERSION
from blossom.screenshot_review import dates_seen, unsure_texts_of, wordless_of
from blossom.screenshots import (
    Card,
    HomeworkReading,
    homework_assembly,
    homework_reading,
    homework_rows,
)
from blossom.stores.import_drafts import FAILURES, DraftRow, StoredReading
from tests.support import PS, RL, THU, card, heading, homework, shot, stored_homework
from tests.support import SCREENSHOT_NOW as NOW
from tests.support import due_card as due
from tests.support import homework_answer as an_answer
from tests.support import homework_card as a_card


def a_report(**fields: object) -> dict[str, Any]:
    """A grade report's answer, every property present, the teacher cell filled."""
    row: dict[str, object] = dict.fromkeys(("due", "curve", "bonus", "penalty", "note"), "")
    row |= {"title": "Lab 2", "points": "18", "max": "20", "average": "90%", "status": ""}
    report: dict[str, Any] = {
        "header_visible": True,
        "school_year": "2026-2027",
        "term": "Q1",
        "class_code": "SCI-08B",
        "class_name": "Science 8",
        "teacher": "Sentinel-Teacher-T1",
        "categories": [
            {
                "name": "Labs",
                "weight": "40",
                "average": "90.00%",
                "rows": [{**row, "y_top": 300, "y_bottom": 330, "uncertain": []}],
                "uncertain": [],
            }
        ],
        "term_grade_visible": True,
        "term_percent": "90.0",
        "term_letter": "A-",
        "uncertain": [{"field": "teacher", "reason": "Sentinel-Teacher-Note-T2"}],
    }
    report.update(fields)
    return report


def checked(answer: object) -> HomeworkReading:
    """The reading of an answer that fits, failing the test when it doesn't."""
    result = homework_reading(answer)
    assert result.failure is None
    assert result.reading is not None
    return result.reading


# ------------------------------------------------------------------ a homework list


def test_a_homework_list_keeps_its_headings_cards_and_selected_student() -> None:
    reading = checked(an_answer())

    assert reading.image_kind == "homework_list"
    assert not reading.wrong_kind
    assert (reading.student.name, reading.student.uncertain) == ("Wren", None)
    assert [heading.text for heading in reading.headings] == ["Monday 10/5/2026"]
    assert [card.title for card in reading.cards if card is not None] == [
        "Sentence Composing",
        "Chains Reading Guide 6",
    ]


def test_dates_are_kept_as_the_text_shown_and_parsed_in_code() -> None:
    reading = checked(an_answer())
    assigned, due = reading.cards

    assert reading.headings[0].day == date(2026, 10, 5)
    assert assigned is not None
    assert due is not None
    assert (assigned.footer, assigned.due) == ("Due (10/8/2026)", date(2026, 10, 8))
    assert due.due is None


@pytest.mark.parametrize(
    ("text", "day"),
    [
        ("Monday 10/5/2026", date(2026, 10, 5)),
        (" Friday 1/9/2026 ", date(2026, 1, 9)),
        ("Monday 13/45/2026", None),
        ("Week of 10/5/2026", None),
        ("10/5/2026", None),
        ("11/16/2026 - Monday", date(2026, 11, 16)),
        ("- 11/16/2026 - Monday", date(2026, 11, 16)),
        (" 1/9/2026 - Friday ", date(2026, 1, 9)),
        ("13/45/2026 - Monday", None),
        ("11/16/2026 - Someday", None),
        ("11/16/2026 - Monday and more", None),
    ],
)
def test_a_heading_names_a_day_only_when_its_text_reads_as_one(text: str, day: date | None) -> None:
    reading = checked(an_answer(headings=[{"text": text, "y": 100, "uncertain": None}]))

    assert reading.headings[0].day == day
    assert reading.headings[0].text == text


@pytest.mark.parametrize(
    ("footer", "due"),
    [
        ("Due (10/8/2026)", date(2026, 10, 8)),
        ("Due ( 2/3/2027 )", date(2027, 2, 3)),
        ("Due (2/30/2026)", None),
        ("Due 10/8/2026", None),
        ("", None),
    ],
)
def test_a_footer_names_a_due_date_only_when_its_text_reads_as_one(
    footer: str, due: date | None
) -> None:
    reading = checked(an_answer(cards=[a_card(footer=footer)]))

    card = reading.cards[0]
    assert card is not None
    assert (card.footer, card.due) == (footer, due)


@pytest.mark.parametrize(
    "value", ["2026-10-08", {"year": 2026, "month": 10, "day": 8}, 20261008], ids=str
)
def test_a_footer_that_is_not_text_makes_its_card_one_that_couldnt_be_read(value: object) -> None:
    """The model never supplies a date as a date: only the text shown, parsed here."""
    reading = checked(an_answer(cards=[a_card(footer=value), a_card()]))

    if isinstance(value, str):
        assert reading.cards[0] is not None
        assert reading.cards[0].due is None
    else:
        assert reading.cards[0] is None
    assert reading.cards[1] is not None


# ------------------------------------------------- marks and cards that can't be read


def test_an_uncertain_field_is_kept_with_its_mark() -> None:
    """A title hidden by the volume indicator: null, with the reader's reason."""
    mark = {"field": "title", "reason": "covered by an overlay"}
    reading = checked(an_answer(cards=[a_card(title=None, uncertain=[mark])]))

    card = reading.cards[0]
    assert card is not None
    assert card.title is None
    assert card.uncertain_fields == frozenset({"title"})
    assert [(m.field, m.reason) for m in card.uncertain] == [("title", "covered by an overlay")]
    assert not card.complete


def test_a_clipped_card_and_a_list_with_no_heading_are_kept_as_read() -> None:
    clipped = a_card(
        badge=None,
        footer=None,
        y_top=880,
        y_bottom=1000,
        clipped=True,
        uncertain=[{"field": "footer", "reason": "cut off at the bottom edge"}],
    )
    reading = checked(an_answer(headings=[], cards=[a_card(), clipped]))

    card = reading.cards[1]
    assert reading.headings == ()
    assert card is not None
    assert card.clipped
    assert (card.badge, card.footer, card.due) == (None, None, None)
    assert card.uncertain_fields == frozenset({"footer"})
    assert not card.complete
    assert reading.cards[0] is not None
    assert reading.cards[0].complete


UNTYPED_CARDS = {
    "title-a-number": a_card(title=42),
    "badge-unknown": a_card(badge="LATE"),
    "a-key-missing": {k: v for k, v in a_card().items() if k != "course"},
    "a-key-extra": a_card(teacher="Sentinel-Teacher-T3"),
    "y-as-text": a_card(y_top="210"),
    "clipped-as-a-number": a_card(clipped=1),
    "mark-unknown-field": a_card(uncertain=[{"field": "teacher", "reason": "hidden"}]),
    "mark-not-an-object": a_card(uncertain=["title"]),
    "title-too-long": a_card(title="T" * 301),
    "instruction-unwritable": a_card(instruction="Read\udfff"),
    "null": None,
    "a-list": ["English 8", "Sentence Composing"],
}


@pytest.mark.parametrize("card", UNTYPED_CARDS.values(), ids=UNTYPED_CARDS.keys())
def test_a_card_that_doesnt_fit_stays_at_its_place_as_one_that_couldnt_be_read(
    card: object,
) -> None:
    reading = checked(an_answer(cards=[a_card(), card, a_card(title="Vocab Quiz")]))

    assert len(reading.cards) == 3
    assert reading.cards[1] is None
    assert [card.title for card in reading.cards if card is not None] == [
        "Sentence Composing",
        "Vocab Quiz",
    ]
    assert "Sentinel" not in reading.model_dump_json()


# ------------------------------------------------- a long first line on an unlabeled card

LONG_LINE = "Read " + "one " * 73 + "page"
"""A first line of 301 characters, one past a title's limit."""
ODD_LINE = " Read  The OUTSIDERS,  chapter Two " + "and  Annotate it " * 16 + " "
"""A first line past a title's limit with inner double spaces, a leading and a trailing
space and mixed case."""


def unlabeled(title: object, instruction: object = None, **fields: object) -> dict[str, Any]:
    """A card with no label and no due line, as a printed list's subject is answered."""
    return a_card(badge=None, footer=None, title=title, instruction=instruction, **fields)


def one_card(card: object) -> Card | None:
    """The one card of an answer as it is checked: the card, or ``None`` for one that
    couldn't be read."""
    [found] = checked(an_answer(cards=[card])).cards
    return found


@pytest.mark.parametrize(
    "instruction", [None, "Then answer the questions."], ids=["alone", "with-an-instruction"]
)
def test_a_first_line_of_exactly_the_title_limit_stays_the_title(instruction: str | None) -> None:
    """An unlabeled card with no due line whose title is exactly 300 characters is kept as
    answered: its title is its title and its instruction is its instruction."""
    title = "T" * screenshots.TITLE_MOST
    card = one_card(unlabeled(title, instruction))

    assert card is not None
    assert (card.title, card.instruction) == (title, instruction)


def test_a_first_line_past_the_title_limit_is_read_as_the_instruction() -> None:
    """An unlabeled card with no due line whose title is 301 characters is read with those
    words in its instruction, exactly as given, and no title. The answer itself is left as
    given."""
    given = unlabeled(LONG_LINE)
    card = one_card(given)

    assert len(LONG_LINE) == screenshots.TITLE_MOST + 1
    assert card is not None
    assert (card.title, card.instruction) == (None, LONG_LINE)
    assert (card.badge, card.course, card.footer) == (None, "English 8", None)
    assert (given["title"], given["instruction"]) == (LONG_LINE, None)


def test_a_long_first_line_is_followed_by_a_line_break_and_its_continuation() -> None:
    """With an instruction, the long first line comes first, then one line break, then the
    instruction, each exactly as given, so the two stay lines of their own."""
    more = "Then write three sentences about it."
    card = one_card(unlabeled(LONG_LINE, more))

    assert card is not None
    assert (card.title, card.instruction) == (None, f"{LONG_LINE}\n{more}")
    assert (card.instruction or "").splitlines() == [LONG_LINE, more]


@pytest.mark.parametrize("more", [None, "  THEN  rest. "], ids=["alone", "with-an-instruction"])
def test_a_long_first_line_keeps_its_wording_character_for_character(more: str | None) -> None:
    """Inner double spaces, a leading and a trailing space and mixed case are kept as given:
    nothing is folded, trimmed or reworded."""
    card = one_card(unlabeled(ODD_LINE, more))

    assert len(ODD_LINE) > screenshots.TITLE_MOST
    assert card is not None
    assert card.title is None
    assert card.instruction == (ODD_LINE if more is None else ODD_LINE + "\n" + more)


@pytest.mark.parametrize(
    ("first", "more", "fits"),
    [(4_000, None, True), (4_001, None, False), (301, 3_698, True), (301, 3_699, False)],
    ids=[
        "alone-4000",
        "alone-4001",
        "with-a-continuation-4000",
        "with-a-continuation-4001",
    ],
)
def test_a_long_first_line_is_moved_only_when_the_whole_text_fits_an_instruction(
    first: int, more: int | None, fits: bool
) -> None:
    """The whole text, the first line or the first line, one line break and the instruction,
    is moved when it is 4,000 characters, an instruction's limit. At 4,001 nothing is moved
    and nothing is cut: the card is one that couldn't be read."""
    title = "T" * first
    instruction = None if more is None else "i" * more
    whole = title if instruction is None else f"{title}\n{instruction}"
    card = one_card(unlabeled(title, instruction))

    assert len(whole) == screenshots.INSTRUCTION_MOST + (0 if fits else 1)
    if fits:
        assert card is not None
        assert (card.title, card.instruction) == (None, whole)
    else:
        assert card is None


@pytest.mark.parametrize("badge", ["DUE", "ASSIGNED"])
def test_a_labeled_card_with_a_title_past_the_limit_couldnt_be_read(badge: str) -> None:
    """A card with a DUE or ASSIGNED label is checked as answered: with a title of 301
    characters, and no due line and no instruction, it is one that couldn't be read."""
    card = a_card(badge=badge, title=LONG_LINE, instruction=None, footer=None)

    assert one_card(card) is None


@pytest.mark.parametrize(
    "footer", ["Due (10/8/2026)", "See the class page"], ids=["a-due-line", "other-words"]
)
def test_an_unlabeled_card_whose_due_line_has_words_and_title_is_past_the_limit_couldnt_be_read(
    footer: str,
) -> None:
    """An unlabeled card whose due line has words is checked as answered: with a title of 301
    characters it is one that couldn't be read."""
    card = a_card(badge=None, title=LONG_LINE, instruction=None, footer=footer)

    assert one_card(card) is None


@pytest.mark.parametrize(
    "footer", ["", "  ", "\t\n"], ids=["an-empty-text", "only-spaces", "a-tab-and-a-line-break"]
)
def test_a_due_line_with_no_words_is_no_due_line_for_a_long_first_line(footer: str) -> None:
    """A due line that is present and is an empty text or only whitespace is no due line, as
    a note's due line is read: the unlabeled card's long first line is read as its
    instruction, and the due line is kept as given. The same card with a label is one that
    couldn't be read."""
    card = one_card(a_card(badge=None, title=LONG_LINE, instruction=None, footer=footer))
    labeled = a_card(badge="DUE", title=LONG_LINE, instruction=None, footer=footer)

    assert card is not None
    assert (card.title, card.instruction, card.footer) == (None, LONG_LINE, footer)
    assert one_card(labeled) is None


@pytest.mark.parametrize("missing", ["footer", "badge", "instruction"])
def test_a_long_first_line_on_a_card_that_lacks_a_property_couldnt_be_read(missing: str) -> None:
    """A card that lacks its due line, its label or its instruction as a property is one that
    couldn't be read, with a long first line as with a title that fits: a due line that is
    null or has no words is no due line, a due line that is absent is a card that doesn't fit,
    and the move supplies no property."""
    long_card = unlabeled(LONG_LINE)
    fitting = unlabeled("Read page 3")
    del long_card[missing]
    del fitting[missing]

    assert one_card(long_card) is None
    assert one_card(fitting) is None


@pytest.mark.parametrize(
    "more",
    ["11/17/2026 - Tuesday", "Due: Essay", "Then write a summary.\n11/17/2026 - Tuesday"],
    ids=["a-day-line", "a-card-line", "a-day-line-after-a-line-break"],
)
def test_a_long_first_line_with_a_continuation_line_shaped_as_structure_couldnt_be_read(
    more: str,
) -> None:
    """A continuation with a line shaped as a day or a card, the shapes the note rule refuses
    in a note, leaves the card as answered, so it is one that couldn't be read: each line is
    checked as given, before the continuation is joined to the first line. Under a title that
    fits, the same card is kept as answered, for the note rule to refuse when its rows are
    read."""
    fitting = one_card(unlabeled("Read page 3", more))

    assert one_card(unlabeled(LONG_LINE, more)) is None
    assert fitting is not None
    assert (fitting.title, fitting.instruction) == ("Read page 3", more)


def test_a_long_first_line_shaped_as_structure_itself_couldnt_be_read() -> None:
    """A long first line that is itself shaped as a card line is left as answered, so the card
    is one that couldn't be read."""
    line = "Due: " + "essay " * 60 + "draft"

    assert len(line) > screenshots.TITLE_MOST
    assert one_card(unlabeled(line)) is None


OTHERWISE_WRONG: dict[str, dict[str, object]] = {
    "a-position-out-of-range": {"y_bottom": 1001},
    "edges-reversed": {"y_top": 400, "y_bottom": 300},
    "a-wrong-type": {"clipped": 1},
    "an-unknown-name": {"teacher": "Sentinel-Teacher-T3"},
}


@pytest.mark.parametrize("fields", OTHERWISE_WRONG.values(), ids=OTHERWISE_WRONG.keys())
def test_a_long_first_line_on_a_card_that_fails_another_check_couldnt_be_read(
    fields: dict[str, object],
) -> None:
    """Every other check still decides: an unlabeled card that fails one is a card that
    couldn't be read, with a short title and with a title of 301 characters alike."""
    assert one_card(unlabeled("Read page 12.", **fields)) is None
    assert one_card(unlabeled(LONG_LINE, **fields)) is None


@pytest.mark.parametrize(
    "title",
    [42, [LONG_LINE], {"text": LONG_LINE}],
    ids=["a-number", "a-list-holding-a-long-text", "an-object-holding-a-long-text"],
)
def test_a_title_that_is_not_a_text_is_not_moved(title: object) -> None:
    """Only a title that is a text is moved; any other value leaves a card that couldn't be
    read."""
    assert one_card(unlabeled(title)) is None


def test_a_mark_on_a_long_first_line_names_the_instruction_it_is_read_as() -> None:
    """A mark of uncertainty that names the title goes with the words: on a card read with its
    long first line in its instruction, the mark names the instruction, its reason as given,
    and every other mark stays as given, in order."""
    marks = [
        {"field": "course", "reason": "blurred"},
        {"field": "title", "reason": "smudged ink"},
        {"field": "instruction", "reason": "cut off"},
    ]
    card = one_card(unlabeled(LONG_LINE, "Then rest.", uncertain=marks))

    assert card is not None
    assert [(mark.field, mark.reason) for mark in card.uncertain] == [
        ("course", "blurred"),
        ("instruction", "smudged ink"),
        ("instruction", "cut off"),
    ]
    assert card.uncertain_fields == frozenset({"course", "instruction"})


def test_the_row_of_a_long_first_line_asks_about_its_marked_words_as_its_instruction() -> None:
    """The row read from the stored reading carries the mark under the instruction, the field
    that holds the marked words, so the review asks the family about them."""
    mark = {"field": "title", "reason": "smudged ink"}
    answer = an_answer(cards=[unlabeled(LONG_LINE, uncertain=[mark])])

    [row] = homework_assembly([stored_homework("long-line", answer)], now=NOW).rows

    assert row.marks == frozenset({"instruction"})
    assert [seen.text for seen in row.reading.instructions] == [LONG_LINE]


def test_a_mark_on_a_title_that_fits_stays_on_the_title() -> None:
    mark = {"field": "title", "reason": "smudged ink"}
    card = one_card(unlabeled("T" * screenshots.TITLE_MOST, uncertain=[mark]))

    assert card is not None
    assert [(m.field, m.reason) for m in card.uncertain] == [("title", "smudged ink")]


def test_the_stored_text_of_a_long_first_line_holds_it_as_the_instruction() -> None:
    """A reading's stored text holds the card as read, no title and the words in its
    instruction, and decodes to the same reading."""
    reading = checked(an_answer(cards=[unlabeled(LONG_LINE)]))
    [stored] = json.loads(reading.model_dump_json())["cards"]

    assert (stored["title"], stored["instruction"]) == (None, LONG_LINE)
    assert HomeworkReading.model_validate_json(reading.model_dump_json()) == reading


def test_a_stored_reading_holding_a_title_past_the_limit_does_not_decode() -> None:
    """A long first line is moved when an answer is checked, never when a stored reading is
    decoded: a stored text whose card holds a title of 301 characters is a reading that
    couldn't be read."""
    title = "Study " + "two " * 72 + "sheets"
    fits = stored_homework("stored-long-title", an_answer(cards=[unlabeled(title)]))
    longer = fits._replace(body=fits.body.replace(title, title + "s"))

    assembly = homework_assembly([longer], now=NOW)

    assert len(title) == screenshots.TITLE_MOST
    assert json.loads(longer.body)["cards"][0]["title"] == title + "s"
    assert len(homework_assembly([fits], now=NOW).rows) == 1
    assert assembly.rows == ()
    assert [item.reason for item in assembly.unread] == ["reading_unreadable"]


# ------------------------------------------------------------------ the selected student


def test_the_selected_student_field_is_kept_as_given_in_its_closed_schema() -> None:
    """The answer's selected student passes through as given, a name and an uncertainty and
    nothing else. Which tab the reader selects is the model's reading, checked only by a live
    or recorded answer, and isn't tested here."""
    reading = checked(an_answer())

    assert reading.student.name == "Wren"
    assert reading.student.uncertain is None
    assert set(type(reading.student).model_fields) == {"name", "uncertain"}


def test_a_missing_name_is_kept_as_missing_with_its_note() -> None:
    reading = checked(
        an_answer(active_student={"name": None, "uncertain": "cut off at the top edge"})
    )

    assert (reading.student.name, reading.student.uncertain) == (None, "cut off at the top edge")


def test_another_name_is_kept_as_read_for_the_identity_question() -> None:
    reading = checked(an_answer(active_student={"name": "Rowan", "uncertain": "blurred"}))

    assert (reading.student.name, reading.student.uncertain) == ("Rowan", "blurred")


def test_the_name_and_its_note_are_kept_in_the_stored_reading() -> None:
    """The narrow exception: the name text lives in the reading of the image it came from."""
    reading = checked(an_answer(active_student={"name": "Sentinel-N1", "uncertain": "Sentinel-U2"}))

    body = reading.model_dump_json()
    assert "Sentinel-N1" in body
    assert "Sentinel-U2" in body
    assert HomeworkReading.model_validate_json(body) == reading


# ------------------------------------------------------------------ other kinds of image


def test_a_message_screenshot_is_the_wrong_kind_and_keeps_nothing_else() -> None:
    """Even an answer that lists cards for it keeps none of them."""
    reading = checked(
        an_answer(
            image_kind="unsupported",
            active_student={"name": "Sentinel-N3", "uncertain": None},
        )
    )

    assert reading.wrong_kind
    assert reading.image_kind == "unsupported"
    assert (reading.student.name, reading.student.uncertain) == (None, None)
    assert reading.headings == ()
    assert reading.cards == ()
    assert "Sentinel" not in reading.model_dump_json()


def test_a_report_with_a_teacher_cell_fits_and_keeps_neither_the_cell_nor_its_mark() -> None:
    answer = an_answer(
        image_kind="grade_report",
        active_student={"name": "Sentinel-N4", "uncertain": None},
        headings=[],
        cards=[],
        report=a_report(),
    )

    reading = checked(answer)

    body = reading.model_dump_json()
    assert reading.wrong_kind
    assert reading.image_kind == "grade_report"
    assert "Sentinel" not in body
    assert "teacher" not in body
    assert "SCI-08B" not in body


def test_a_report_that_doesnt_fit_the_schema_is_not_valid() -> None:
    for broken in (
        a_report(teacher=7),
        a_report(advisor="Sentinel-A5"),
        a_report(uncertain=[{"field": "advisor", "reason": "hidden"}]),
        {k: v for k, v in a_report().items() if k != "teacher"},
    ):
        answer = an_answer(image_kind="grade_report", cards=[], report=broken)
        assert homework_reading(answer) == (None, "not_valid")


# ------------------------------------------------------------------ answers that don't fit

MALFORMED: dict[str, object] = {
    "a-list": [an_answer()],
    "a-string": json.dumps(an_answer()),
    "null": None,
    "empty": {},
    "a-key-missing": {k: v for k, v in an_answer().items() if k != "report"},
    "an-extra-field": an_answer(confidence="high"),
    "an-unknown-kind": an_answer(image_kind="email"),
    "kind-not-text": an_answer(image_kind=1),
    "student-not-an-object": an_answer(active_student="Wren"),
    "student-name-a-number": an_answer(active_student={"name": 7, "uncertain": None}),
    "student-name-too-long": an_answer(active_student={"name": "W" * 101, "uncertain": None}),
    "student-extra-field": an_answer(
        active_student={"name": "Wren", "uncertain": None, "other_tabs": ["Rowan", "Ellis"]}
    ),
    "name-unwritable": an_answer(active_student={"name": "W\ud800", "uncertain": None}),
    "heading-unwritable": an_answer(headings=[{"text": "Monday\ud800", "y": 1, "uncertain": None}]),
    "headings-not-a-list": an_answer(headings={"text": "Monday 10/5/2026"}),
    "heading-without-y": an_answer(headings=[{"text": "Monday 10/5/2026", "uncertain": None}]),
    "heading-y-as-text": an_answer(headings=[{"text": "Monday", "y": "150", "uncertain": None}]),
    "heading-y-as-true": an_answer(headings=[{"text": "Monday", "y": True, "uncertain": None}]),
    "heading-date-as-object": an_answer(
        headings=[{"text": {"month": 10, "day": 5}, "y": 150, "uncertain": None}]
    ),
    "cards-not-a-list": an_answer(cards=a_card()),
    "too-many-cards": an_answer(cards=[a_card()] * 61),
    "report-not-an-object": an_answer(report="none"),
}


@pytest.mark.parametrize("answer", MALFORMED.values(), ids=MALFORMED.keys())
def test_an_answer_that_doesnt_fit_the_schema_is_a_failed_read(answer: object) -> None:
    """No repair, no rows, no value invented: the whole reading fails."""
    assert homework_reading(copy.deepcopy(answer)) == (None, "not_valid")


def test_the_failure_is_one_the_draft_store_keeps() -> None:
    assert set(get_args(screenshots.ReadingFailure)) == {"not_valid"}
    assert set(get_args(screenshots.ReadingFailure)) <= set(FAILURES)


# ------------------------------------------------------------------ the models and the schema


def schema_enum(*path: str) -> set[str]:
    """The enum at a path into the answer schema, following properties and items."""
    node: Any = SCHEMA
    for step in path:
        node = node["items"] if step == "[]" else node["properties"][step]
    if "anyOf" in node:
        node = next(option for option in node["anyOf"] if "enum" in option)
    return set(node["enum"])


def test_the_closed_choices_are_the_schemas_own() -> None:
    report = SCHEMA["properties"]["report"]["anyOf"][1]  # type: ignore[index]
    category = report["properties"]["categories"]["items"]
    row = category["properties"]["rows"]["items"]
    marks = (
        (screenshots.CardField, ("cards", "[]", "uncertain", "[]", "field")),
        (screenshots.ImageKind, ("image_kind",)),
        (screenshots.Badge, ("cards", "[]", "badge")),
    )
    for literal, path in marks:
        assert set(get_args(literal)) == schema_enum(*path)
    assert set(get_args(screenshots.ReportField)) == set(
        report["properties"]["uncertain"]["items"]["properties"]["field"]["enum"]
    )
    assert set(get_args(screenshots.CategoryField)) == set(
        category["properties"]["uncertain"]["items"]["properties"]["field"]["enum"]
    )
    assert set(get_args(screenshots.RowField)) == set(
        row["properties"]["uncertain"]["items"]["properties"]["field"]["enum"]
    )


def test_every_property_the_schema_requires_is_declared() -> None:
    """A valid answer fits: each model declares exactly the schema's properties."""
    card = SCHEMA["properties"]["cards"]["items"]  # type: ignore[index]
    report = SCHEMA["properties"]["report"]["anyOf"][1]  # type: ignore[index]

    assert set(Card.model_fields) == set(card["required"])
    assert set(screenshots.Heading.model_fields) == set(
        SCHEMA["properties"]["headings"]["items"]["required"]  # type: ignore[index]
    )
    assert set(screenshots.ReportAnswer.model_fields) == set(report["required"])


@pytest.mark.parametrize(
    "model",
    [
        screenshots.HomeworkReading,
        screenshots.Card,
        screenshots.CardMark,
        screenshots.Heading,
        screenshots.Student,
        screenshots.ReportAnswer,
    ],
)
def test_every_model_is_closed_frozen_and_hides_its_input(model: type) -> None:
    config = model.model_config  # type: ignore[attr-defined]

    assert config["extra"] == "forbid"
    assert config["frozen"] is True
    assert config["hide_input_in_errors"] is True


# ------------------------------------------------------------------ nothing reaches a log


def test_no_name_or_school_text_reaches_a_log_and_no_error_escapes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Checking logs nothing and raises nothing, whatever the answer holds."""
    named = {"name": "Sentinel-N6", "uncertain": "Sentinel-U7"}
    answers = [
        an_answer(active_student=named, cards=[a_card(title="Sentinel-Title-S8")]),
        an_answer(active_student=named, cards=[a_card(**{"Sentinel-Key-K9": "x"})]),
        an_answer(active_student=named, **{"Sentinel-Key-K10": "Sentinel-V11"}),
        an_answer(active_student={"name": "Sentinel-N12" * 20, "uncertain": None}),
        an_answer(image_kind="Sentinel-Kind-K13"),
        "Sentinel-Raw-R14",
    ]

    with caplog.at_level(logging.DEBUG):
        results = [homework_reading(answer) for answer in answers]

    assert [result.failure for result in results] == [
        None,
        None,
        "not_valid",
        "not_valid",
        "not_valid",
        "not_valid",
    ]
    assert "Sentinel" not in caplog.text
    for result in results[2:]:
        assert "Sentinel" not in repr(result)


# ------------------------------------------------------------------ text a card can carry


def test_a_blank_course_or_title_is_not_a_complete_card() -> None:
    reading = checked(an_answer(cards=[a_card(course="  "), a_card(title=""), a_card()]))

    assert [card.complete for card in reading.cards if card is not None] == [False, False, True]


# ------------------------------------------------------------------ from readings to rows


def assigned(
    course: str, title: str | None, footer: str, y_top: int, **fields: object
) -> dict[str, object]:
    """An ASSIGNED card with its due line and no instruction unless one is given."""
    card = a_card(course=course, title=title, footer=footer, instruction=None)
    card.update(y_top=y_top, y_bottom=y_top + 80, **fields)
    return card


def keys(*images: StoredReading) -> tuple[DraftRow, ...]:
    return homework_rows(images)


def test_sentence_composing_across_two_images_is_one_row_with_both_dates() -> None:
    """Assigned on Tuesday, due Wednesday, each card in its own screenshot, the teacher's
    exception kept word for word, once."""
    words = "Complete pages 12 to 14. Exception: skip number 9."
    tuesday = homework(
        assigned("English 8", "Sentence Composing", "Due (10/7/2026)", 200, instruction=words),
        headings=[heading("Tuesday 10/6/2026", 150)],
    )
    wednesday = homework(
        due("English 8", "Sentence Composing", 200, instruction=words),
        headings=[heading("Wednesday 10/7/2026", 150)],
    )

    assembly = homework_assembly(
        [stored_homework("t", tuesday), stored_homework("w", wednesday)], now=NOW
    )

    [row] = assembly.rows
    assert assembly.unread == ()
    assert (row.reading.assigned_on, row.reading.due_date) == (date(2026, 10, 6), date(2026, 10, 7))
    assert {(c.asserted_value, c.seen_in) for c in row.reading.claims} == {
        ("2026-10-07", OWN_LINE),
        ("2026-10-07", DAY_HEADER),
    }
    assert [seen.text for seen in row.reading.instructions] == [words]
    assert row.marks == frozenset()


def test_chains_reading_guide_6_assigned_monday_and_due_thursday_is_one_row() -> None:
    """Its footer says Thursday, and it sits under Thursday in the next screenshot, where it
    first repeats as the overlap at the top."""
    chains = assigned("Science 8", "Chains Reading Guide 6", "Due (10/8/2026)", 260)
    monday = homework(
        assigned("Math 8", "Lab Safety Quiz", "Due (10/9/2026)", 160),
        chains,
        headings=[heading("Monday 10/5/2026", 100)],
    )
    thursday = homework(
        dict(chains, y_top=10, y_bottom=90),
        due("Science 8", "Chains Reading Guide 6", 200),
        headings=[heading("Thursday 10/8/2026", 150)],
    )

    assembly = homework_assembly(
        [stored_homework("m", monday), stored_homework("t", thursday)], now=NOW
    )

    by_title = {row.reading.title: row.reading for row in assembly.rows}
    assert set(by_title) == {"Chains Reading Guide 6", "Lab Safety Quiz"}
    guide = by_title["Chains Reading Guide 6"]
    assert (guide.assigned_on, guide.due_date) == (date(2026, 10, 5), date(2026, 10, 8))
    assert {c.seen_in for c in guide.claims} == {OWN_LINE, DAY_HEADER}
    assert assembly.unread == ()


def test_u1_quiz_3_proposes_its_day_and_keeps_the_sentence_about_the_move() -> None:
    words = "moved from 10/1 to 10/5"
    monday = homework(
        due("Math 8", "U1 Quiz 3", 200, instruction=words),
        headings=[heading("Monday 10/5/2026", 150)],
    )

    [row] = homework_assembly([stored_homework("m", monday)], now=NOW).rows

    assert row.reading.due_date == date(2026, 10, 5)
    assert [(c.asserted_value, c.seen_in) for c in row.reading.claims] == [
        ("2026-10-05", DAY_HEADER)
    ]
    assert [seen.text for seen in row.reading.instructions] == [words]


def test_a_hidden_title_is_an_item_that_couldnt_be_read_and_the_other_row_stays_ready() -> None:
    hidden = assigned(
        "English 8",
        None,
        "Due (10/8/2026)",
        300,
        uncertain=[{"field": "title", "reason": "covered by an overlay"}],
    )
    monday = homework(
        assigned("Math 8", "Lab Safety Quiz", "Due (10/9/2026)", 160),
        hidden,
        headings=[heading("Monday 10/5/2026", 100)],
    )
    stored = stored_homework("m", monday)

    assembly = homework_assembly([stored], now=NOW)

    assert [row.reading.title for row in assembly.rows] == ["Lab Safety Quiz"]
    [item] = assembly.unread
    assert (item.sha256, item.place) == (stored.sha256, 1)
    assert sorted(readable for _, readable in keys(stored)) == [False, True]


def test_a_due_card_above_every_heading_is_never_given_a_day_without_an_overlap() -> None:
    """Nothing is guessed from the phone's time or the upload's order."""
    top = homework(
        due("Science 8", "Spelling List 5", 20),
        due("English 8", "Vocab 4", 300),
        headings=[heading("Thursday 10/8/2026", 250)],
    )

    assembly = homework_assembly([stored_homework("x", top)], now=NOW)

    assert [row.reading.title for row in assembly.rows] == ["Vocab 4"]
    [item] = assembly.unread
    assert item.card is not None
    assert item.card.title == "Spelling List 5"


def wednesday_and_after() -> tuple[StoredReading, StoredReading]:
    """Two screenshots of one list, scrolled: the second's top card repeats the first's last
    whole card under Wednesday, then goes on past the first's bottom edge."""
    first = homework(
        due("English 8", "Vocab 4", 300),
        due("Science 8", "Spelling List 5", 920, clipped=True),
        headings=[heading("Wednesday 10/7/2026", 250)],
    )
    second = homework(
        due("English 8", "Vocab 4", 10),
        due("Science 8", "Spelling List 5", 100),
        due("Math 8", "U1 Quiz 3", 400),
        headings=[heading("Thursday 10/8/2026", 350)],
    )
    return stored_homework("first", first), stored_homework("second", second)


def test_a_heading_never_carries_to_another_screenshots_top_cards() -> None:
    """The second screenshot's top cards repeat the first's Wednesday, but no heading carries
    between screenshots: they stay items, and so does the first's clip."""
    first, second = wednesday_and_after()

    assembly = homework_assembly([first, second], now=NOW)
    alone = homework_assembly([second], now=NOW)

    due_on = {row.reading.title: row.reading.due_date for row in assembly.rows}
    assert due_on == {"Vocab 4": date(2026, 10, 7), "U1 Quiz 3": date(2026, 10, 8)}
    assert sorted((str(i.card.title), i.reason) for i in assembly.unread if i.card) == [
        ("Spelling List 5", "clipped"),
        ("Spelling List 5", "no_day"),
        ("Vocab 4", "no_day"),
    ]
    assert {row.reading.title for row in alone.rows} == {"U1 Quiz 3"}
    assert len(alone.unread) == 2


def test_an_overlap_two_headings_could_explain_proves_neither() -> None:
    first, second = wednesday_and_after()
    other_week = homework(
        due("English 8", "Vocab 4", 300),
        headings=[heading("Wednesday 10/14/2026", 250)],
    )

    assembly = homework_assembly([first, second, stored_homework("other", other_week)], now=NOW)

    assert {row.reading.title for row in assembly.rows} == {"Vocab 4", "U1 Quiz 3"}
    assert {item.card.title for item in assembly.unread if item.card} == {
        "Vocab 4",
        "Spelling List 5",
    }


def test_a_clipped_card_is_an_item_that_couldnt_be_read() -> None:
    first, _ = wednesday_and_after()

    assembly = homework_assembly([first], now=NOW)

    assert [row.reading.title for row in assembly.rows] == ["Vocab 4"]
    [item] = assembly.unread
    assert item.card is not None
    assert item.card.clipped


def test_the_same_batch_reordered_repeated_or_recaptured_gives_the_same_rows() -> None:
    """In any order, and recaptured as new bytes, the batch gives the same rows; the recapture
    brings its clip back as an item of its own."""
    first, second = wednesday_and_after()
    recaptured = first._replace(sha256="0" * 64)

    expected = keys(first, second)
    for images in itertools.permutations([first, second]):
        assert keys(*images) == expected
    again = keys(first, second, recaptured)
    assert [key for key in again if key.readable] == [key for key in expected if key.readable]
    assert set(expected) < set(again)
    [added] = set(again) - set(expected)
    assert not added.readable
    [clip] = [
        item
        for item in homework_assembly([first, second, recaptured], now=NOW).unread
        if item.sha256 == recaptured.sha256
    ]
    assert (clip.key, clip.reason) == (added.key, "clipped")
    assert len({key for key, _ in expected}) == len(expected)


def test_a_recurring_name_is_two_rows_and_a_similar_title_is_its_own() -> None:
    """``pair()`` stays exact: a similar title is a row for the parent's decision."""
    october = homework(
        due("English 8", "Vocab Quiz", 200),
        due("English 8", "Vocab Quiz 4", 300),
        headings=[heading("Thursday 10/8/2026", 150)],
    )
    later = homework(
        due("English 8", "Vocab Quiz", 200), headings=[heading("Thursday 10/22/2026", 150)]
    )

    rows = homework_assembly(
        [stored_homework("a", october), stored_homework("b", later)], now=NOW
    ).rows

    found = sorted((row.reading.title, row.reading.occurrence or "") for row in rows)
    assert found == [("Vocab Quiz", ""), ("Vocab Quiz", "2026-10-22"), ("Vocab Quiz 4", "")]


def vocab_quiz() -> tuple[StoredReading, StoredReading]:
    """Image 1 shows Vocab Quiz due 10/8; image 2 shows it due 10/22."""
    one = homework(
        due("English 8", "Vocab Quiz", 200), headings=[heading("Thursday 10/8/2026", 150)]
    )
    two = homework(
        due("English 8", "Vocab Quiz", 200), headings=[heading("Thursday 10/22/2026", 150)]
    )
    return stored_homework("one", one), stored_homework("two", two)


def test_the_same_images_in_reverse_order_give_the_same_rows_keys_and_new_row_ids() -> None:
    one, two = vocab_quiz()

    forward = homework_assembly([one, two], now=NOW)
    backward = homework_assembly([two, one], now=NOW)

    assert [row.key for row in forward.rows] == [row.key for row in backward.rows]
    ids = {
        row.key: identity(row.reading.course, row.reading.title, row.reading.occurrence)
        for row in forward.rows
    }
    assert ids == {
        row.key: identity(row.reading.course, row.reading.title, row.reading.occurrence)
        for row in backward.rows
    }
    later = next(row for row in forward.rows if row.reading.due_date == date(2026, 10, 22))
    assert later.reading.occurrence == "2026-10-22"


def test_a_key_stands_for_the_content_and_moves_only_with_it() -> None:
    """An unrelated image or an identical recapture leaves a row's key; a claim added to the
    row gives a new key; no name, mark of place or instant enters a key."""
    tuesday = homework(
        assigned("English 8", "Sentence Composing", "Due (10/7/2026)", 200),
        headings=[heading("Tuesday 10/6/2026", 150)],
    )
    image = stored_homework("t", tuesday)
    [row] = keys(image)
    unrelated = stored_homework(
        "u", homework(due("Math 8", "U1 Quiz 3", 200), headings=[heading("Monday 10/5/2026", 150)])
    )
    renamed = stored_homework(
        "t", dict(tuesday, active_student={"name": "Rowan", "uncertain": "x"})
    )
    claim = stored_homework(
        "w",
        homework(
            due("English 8", "Sentence Composing", 200),
            headings=[heading("Wednesday 10/7/2026", 150)],
        ),
    )

    assert row in keys(image, unrelated)
    assert keys(image._replace(sha256="1" * 64)) == (row,)
    assert keys(renamed) == (row,)
    assert row not in keys(image, claim)
    assert homework_assembly([image], now=datetime(2030, 1, 1, tzinfo=UTC)).rows[0].key == row[0]


def test_a_mark_on_a_card_or_its_heading_travels_with_the_row() -> None:
    marked = homework(
        due("Math 8", "U1 Quiz 3", 200, uncertain=[{"field": "instruction", "reason": "blurred"}]),
        headings=[heading("Monday 10/5/2026", 150, uncertain="partly covered")],
    )
    plain = homework(due("Math 8", "U1 Quiz 3", 200), headings=[heading("Monday 10/5/2026", 150)])

    [row] = homework_assembly([stored_homework("m", marked)], now=NOW).rows

    assert row.marks == frozenset({"instruction", "heading"})
    assert keys(stored_homework("m", marked)) != keys(stored_homework("p", plain))


def test_another_kind_gives_no_rows_and_an_undecodable_reading_one_item() -> None:
    message = stored_homework("msg", an_answer(image_kind="unsupported"))
    broken = message._replace(sha256="2" * 64, body='{"image_kind": "homework_list"}')
    old = message._replace(sha256="3" * 64, schema_version="screenshot-reading/0")

    assert keys(message) == ()
    assert [readable for _, readable in keys(broken, old)] == [False, False]


def test_a_blank_instruction_reads_as_no_words_and_the_row_stands() -> None:
    """An instruction of spaces alone has no words to keep, so it never stops the rows."""
    monday = homework(
        due("Math 8", "U1 Quiz 3", 200, instruction="   "),
        headings=[heading("Monday 10/5/2026", 150)],
    )

    [row] = homework_assembly([stored_homework("m", monday)], now=NOW).rows

    assert row.reading.instructions == ()
    assert row.reading.due_date == date(2026, 10, 5)


# ------------------------------------------------------------------ what proves an overlap

LOG_WORDS = "Read 20 minutes and log it."


def test_top_cards_matching_only_the_start_of_a_closed_run_prove_nothing() -> None:
    """A shows Monday [Reading log, Worksheet 3], then Tuesday [Spelling List 5]. B shows a
    Reading log with the same words at its top, then Thursday. Monday goes on past the log to
    the worksheet, which B doesn't show before its heading, so B isn't continuing Monday: its
    top card stays an item that couldn't be read, never merged into Monday's."""
    a = homework(
        due("English 8", "Reading log", 200, instruction=LOG_WORDS),
        due("Math 8", "Worksheet 3", 300),
        due("Science 8", "Spelling List 5", 500),
        headings=[heading("Monday 10/5/2026", 150), heading("Tuesday 10/6/2026", 450)],
    )
    b = homework(
        due("English 8", "Reading log", 20, instruction=LOG_WORDS),
        due("Math 8", "U1 Quiz 3", 300),
        headings=[heading("Thursday 10/8/2026", 250)],
    )

    assembly = homework_assembly([stored_homework("a", a), stored_homework("b", b)], now=NOW)

    assert [item.card.title for item in assembly.unread if item.card] == ["Reading log"]
    log = next(row.reading for row in assembly.rows if row.reading.title == "Reading log")
    assert [(c.asserted_value, c.seen_in) for c in log.claims] == [("2026-10-05", DAY_HEADER)]


def test_top_cards_take_no_day_whatever_heading_follows_them() -> None:
    """B's top cards repeat Monday's whole run, which A closes with Tuesday. Whether B's first
    heading is Tuesday or Thursday, they take no day from another screenshot and stay items."""
    a = homework(
        due("English 8", "Reading log", 200, instruction=LOG_WORDS),
        due("Math 8", "Worksheet 3", 300),
        due("Science 8", "Spelling List 5", 500),
        headings=[heading("Monday 10/5/2026", 150), heading("Tuesday 10/6/2026", 450)],
    )

    def top_then(day: str) -> dict[str, object]:
        return homework(
            due("English 8", "Reading log", 20, instruction=LOG_WORDS),
            due("Math 8", "Worksheet 3", 120),
            due("Science 8", "Spelling List 5", 300),
            headings=[heading(day, 250)],
        )

    continuing = homework_assembly(
        [stored_homework("a", a), stored_homework("b", top_then("Tuesday 10/6/2026"))], now=NOW
    )
    elsewhere = homework_assembly(
        [stored_homework("a", a), stored_homework("b", top_then("Thursday 10/8/2026"))], now=NOW
    )

    assert {item.card.title for item in continuing.unread if item.card} == {
        "Reading log",
        "Worksheet 3",
    }
    assert {item.card.title for item in elsewhere.unread if item.card} == {
        "Reading log",
        "Worksheet 3",
    }


# ------------------------------------------------------------------ clips and item keys


def test_a_clip_stays_an_item_beside_a_copy_under_another_day() -> None:
    """A recurring card's clip under this Wednesday stays an item beside its whole copy under
    next Wednesday."""
    clipped = homework(
        due("English 8", "Vocab 4", 300),
        due("Science 8", "Spelling List 5", 920, clipped=True),
        headings=[heading("Wednesday 10/7/2026", 250)],
    )
    next_week = homework(
        due("Science 8", "Spelling List 5", 300), headings=[heading("Wednesday 10/14/2026", 250)]
    )

    assembly = homework_assembly(
        [stored_homework("this", clipped), stored_homework("next", next_week)], now=NOW
    )

    [item] = assembly.unread
    assert item.card is not None
    assert (item.card.title, item.card.clipped) == ("Spelling List 5", True)


def test_an_item_takes_its_image_and_place_in_its_key() -> None:
    """The same clip, read with certainty under one dated heading in two screenshots, is two
    items: no text read makes two screenshots' cards one."""
    clip = due("Science 8", "Spelling List 5", 920, clipped=True)
    first = homework(clip, headings=[heading("Wednesday 10/7/2026", 250)])
    second = homework(
        due("English 8", "Vocab 4", 300), clip, headings=[heading("Wednesday 10/7/2026", 250)]
    )

    [one] = homework_assembly([stored_homework("first", first)], now=NOW).unread
    [other] = homework_assembly([stored_homework("second", second)], now=NOW).unread
    both = homework_assembly(
        [stored_homework("first", first), stored_homework("second", second)], now=NOW
    ).unread

    assert (one.place, other.place) == (0, 1)
    assert one.sha256 != other.sha256
    assert one.key != other.key
    assert {item.key for item in both} == {one.key, other.key}


# ------------------------------------------------------------------ order, by content alone


def test_the_rows_are_the_same_in_every_order_whether_sha256_order_matches_dates_or_not() -> None:
    """Vocab Quiz due 10/8 in one image and 10/22 in another, under two pairs of labels: one
    where the images' SHA-256 order is the dates' order, one where it is the reverse."""
    early = homework(
        due("English 8", "Vocab Quiz", 200), headings=[heading("Thursday 10/8/2026", 150)]
    )
    late = homework(
        due("English 8", "Vocab Quiz", 200), headings=[heading("Thursday 10/22/2026", 150)]
    )
    found = []
    agree = []
    for first, second in (("one", "two"), ("two", "one")):
        images = [stored_homework(first, early), stored_homework(second, late)]
        agree.append(images[0].sha256 < images[1].sha256)
        for order in itertools.permutations(images):
            rows = homework_assembly(list(order), now=NOW).rows
            found.append(
                sorted(
                    (row.key, row.reading.due_date, row.reading.occurrence or "") for row in rows
                )
            )

    assert sorted(agree) == [False, True]
    assert all(rows == found[0] for rows in found)
    assert [(due_date, occurrence) for _, due_date, occurrence in found[0]] in (
        [(date(2026, 10, 8), ""), (date(2026, 10, 22), "2026-10-22")],
        [(date(2026, 10, 22), "2026-10-22"), (date(2026, 10, 8), "")],
    )


# ------------------------------------------------------------------ bounds


def test_forty_headings_fit_and_forty_one_do_not() -> None:
    def headings(count: int) -> list[dict[str, object]]:
        return [heading(f"Monday 10/{day}/2026", day * 10) for day in range(1, count + 1)]

    assert homework_reading(an_answer(headings=headings(40))).failure is None
    assert homework_reading(an_answer(headings=headings(41))) == (None, "not_valid")


def test_top_cards_matching_only_the_start_of_an_open_ended_run_prove_nothing() -> None:
    """A's Wednesday runs to its bottom with [Reading log, Worksheet 3]. B shows a Reading log,
    then Thursday: had B continued Wednesday, the worksheet would come before Thursday, so B's
    top card stays an item that couldn't be read."""
    a = homework(
        due("English 8", "Reading log", 200, instruction=LOG_WORDS),
        due("Math 8", "Worksheet 3", 300),
        headings=[heading("Wednesday 10/7/2026", 150)],
    )
    b = homework(
        due("English 8", "Reading log", 20, instruction=LOG_WORDS),
        due("Math 8", "U1 Quiz 3", 300),
        headings=[heading("Thursday 10/8/2026", 250)],
    )

    assembly = homework_assembly([stored_homework("a", a), stored_homework("b", b)], now=NOW)

    assert [item.card.title for item in assembly.unread if item.card] == ["Reading log"]
    log = next(row.reading for row in assembly.rows if row.reading.title == "Reading log")
    assert [(c.asserted_value, c.seen_in) for c in log.claims] == [("2026-10-07", DAY_HEADER)]


def test_a_card_not_read_whole_takes_no_part_in_proving_an_overlap() -> None:
    """A card whose title the volume indicator hid matches its copy in every field read, yet
    proves nothing: B's Vocab 4 after it gets no day from A's Wednesday and stays an item."""
    hidden = {"field": "title", "reason": "covered by an overlay"}

    def hidden_title(y_top: int) -> dict[str, object]:
        card = due("English 8", "Hidden", y_top)
        card.update(title=None, uncertain=[hidden])
        return card

    a = homework(
        hidden_title(200),
        due("English 8", "Vocab 4", 300),
        headings=[heading("Wednesday 10/7/2026", 150)],
    )
    b = homework(
        hidden_title(20),
        due("English 8", "Vocab 4", 120),
        due("Math 8", "U1 Quiz 3", 400),
        headings=[heading("Thursday 10/8/2026", 350)],
    )

    assembly = homework_assembly([stored_homework("a", a), stored_homework("b", b)], now=NOW)

    titles = sorted(str(item.card.title) for item in assembly.unread if item.card)
    assert titles == ["None", "None", "Vocab 4"]
    assert {row.reading.title for row in assembly.rows} == {"Vocab 4", "U1 Quiz 3"}


# ------------------------------------------------------------------ the overlap rule, case by case


def test_a_card_that_couldnt_be_read_inside_a_section_breaks_a_proof() -> None:
    """Case 1: A's Wednesday is [Reading log, a card that couldn't be typed, Worksheet 3];
    B's top is [Reading log, Worksheet 3, Spelling List 5]. The unread card matches nothing, so
    B continues nothing, and its Spelling List 5 gets no day."""
    a = homework(
        due("English 8", "Reading log", 150, instruction=LOG_WORDS),
        a_card(title=42),
        due("Math 8", "Worksheet 3", 400),
        headings=[heading("Wednesday 10/7/2026", 100)],
    )
    b = homework(
        due("English 8", "Reading log", 20, instruction=LOG_WORDS),
        due("Math 8", "Worksheet 3", 120),
        due("Science 8", "Spelling List 5", 220),
        due("Math 8", "U1 Quiz 3", 450),
        headings=[heading("Thursday 10/8/2026", 400)],
    )

    assembly = homework_assembly([stored_homework("a", a), stored_homework("b", b)], now=NOW)

    assert "Spelling List 5" in {item.card.title for item in assembly.unread if item.card}
    assert "Spelling List 5" not in {row.reading.title for row in assembly.rows}


def test_a_clipped_card_inside_a_compared_run_breaks_a_proof() -> None:
    """Case 2: a clip inside A's Wednesday, not at an edge, is no part of a run to match."""
    a = homework(
        due("English 8", "Reading log", 150, instruction=LOG_WORDS),
        due("Science 8", "Vocab 4", 250, clipped=True),
        due("Math 8", "Worksheet 3", 400),
        headings=[heading("Wednesday 10/7/2026", 100)],
    )
    b = homework(
        due("English 8", "Reading log", 20, instruction=LOG_WORDS),
        due("Math 8", "Worksheet 3", 120),
        due("Science 8", "Spelling List 5", 220),
        due("Math 8", "U1 Quiz 3", 450),
        headings=[heading("Thursday 10/8/2026", 400)],
    )

    assembly = homework_assembly([stored_homework("a", a), stored_homework("b", b)], now=NOW)

    assert "Spelling List 5" not in {row.reading.title for row in assembly.rows}


def test_a_scroll_across_a_day_boundary_leaves_its_top_cards_visible() -> None:
    """An ordinary scroll from Wednesday into Thursday: B's top cards, its top clip among them,
    take no day from A, and A's bottom clip, with no whole copy under Wednesday, stays too."""
    a = homework(
        due("English 8", "Vocab 4", 300),
        due("Science 8", "Spelling List 5", 920, clipped=True),
        headings=[heading("Wednesday 10/7/2026", 250)],
    )
    b = homework(
        due("Art 8", "Sketch 2", 0, clipped=True),
        due("English 8", "Vocab 4", 90),
        due("Science 8", "Spelling List 5", 190),
        due("Math 8", "U1 Quiz 3", 420),
        headings=[heading("Thursday 10/8/2026", 380)],
    )

    assembly = homework_assembly([stored_homework("a", a), stored_homework("b", b)], now=NOW)

    due_on = {row.reading.title: row.reading.due_date for row in assembly.rows}
    assert due_on == {"Vocab 4": date(2026, 10, 7), "U1 Quiz 3": date(2026, 10, 8)}
    assert sorted((str(i.card.title), i.reason) for i in assembly.unread if i.card) == [
        ("Sketch 2", "clipped"),
        ("Spelling List 5", "clipped"),
        ("Spelling List 5", "no_day"),
        ("Vocab 4", "no_day"),
    ]


def test_an_uncertain_field_is_no_evidence_that_two_cards_are_one() -> None:
    """Case 3: two equal partial titles, each marked uncertain, prove nothing."""
    partial = [{"field": "title", "reason": "covered by an overlay"}]
    a = homework(
        due("English 8", "Reading lo", 200, uncertain=partial),
        headings=[heading("Wednesday 10/7/2026", 150)],
    )
    b = homework(
        due("English 8", "Reading lo", 20, uncertain=partial),
        due("Science 8", "Spelling List 5", 120),
        due("Math 8", "U1 Quiz 3", 420),
        headings=[heading("Thursday 10/8/2026", 380)],
    )

    assembly = homework_assembly([stored_homework("a", a), stored_homework("b", b)], now=NOW)

    assert "Spelling List 5" not in {row.reading.title for row in assembly.rows}


def test_a_clip_stays_an_item_beside_an_uncertain_or_different_copy() -> None:
    """Case 3: a clip stays an item beside a whole copy with a field marked uncertain, and
    beside one with a due line the clip shows as absent."""

    def clip(**fields: object) -> dict[str, object]:
        return homework(
            due("Science 8", "Spelling List 5", 920, clipped=True, **fields),
            headings=[heading("Wednesday 10/7/2026", 250)],
        )

    blurred = [{"field": "instruction", "reason": "blurred"}]
    uncertain_copy = homework(
        due("Science 8", "Spelling List 5", 300, instruction="Study", uncertain=blurred),
        headings=[heading("Wednesday 10/7/2026", 250)],
    )
    footed_copy = homework(
        due("Science 8", "Spelling List 5", 300, footer="Due (10/7/2026)"),
        headings=[heading("Wednesday 10/7/2026", 250)],
    )

    for clipped, whole in ((clip(), uncertain_copy), (clip(), footed_copy)):
        assembly = homework_assembly(
            [stored_homework("clip", clipped), stored_homework("whole", whole)], now=NOW
        )
        assert [item.card.clipped for item in assembly.unread if item.card] == [True]


def test_a_heading_is_carried_only_when_no_proof_names_another() -> None:
    """Case 4: Monday [Y] and Tuesday [Z], then headingless [Z, Y], [Y, W] and [W, V]. [Y, W]
    continues Monday directly and Tuesday through [Z, Y], so it carries neither, and [W, V],
    which continues it, carries nothing either: W and V get no day."""
    monday = homework(
        due("Math 8", "Worksheet 3", 200), headings=[heading("Monday 10/5/2026", 100)]
    )
    tuesday = homework(due("Science 8", "Lab 2", 200), headings=[heading("Tuesday 10/6/2026", 100)])
    z_y = homework(due("Science 8", "Lab 2", 20), due("Math 8", "Worksheet 3", 120), headings=[])
    y_w = homework(due("Math 8", "Worksheet 3", 20), due("English 8", "Vocab 4", 120), headings=[])
    w_v = homework(due("English 8", "Vocab 4", 20), due("Art 8", "Sketch 2", 120), headings=[])
    images = [
        stored_homework(label, answer)
        for label, answer in (
            ("mon", monday),
            ("tue", tuesday),
            ("zy", z_y),
            ("yw", y_w),
            ("wv", w_v),
        )
    ]

    found = set()
    for order in itertools.permutations(images):
        assembly = homework_assembly(list(order), now=NOW)
        found.add(
            (
                frozenset(row.reading.title for row in assembly.rows),
                frozenset(item.card.title for item in assembly.unread if item.card),
            )
        )

    [(rows, unread)] = found
    assert {"Vocab 4", "Sketch 2"} <= unread
    assert not {"Vocab 4", "Sketch 2"} & rows


@pytest.mark.parametrize(
    "fields",
    [{"y_top": -5}, {"y_bottom": 1001}, {"y_top": 300, "y_bottom": 250}],
    ids=["top-above-the-image", "bottom-below-the-image", "bottom-above-top"],
)
def test_a_card_with_positions_outside_the_image_couldnt_be_read(fields: dict[str, int]) -> None:
    """Case 5: such a card is one that couldn't be read, at its place."""
    reading = checked(an_answer(cards=[a_card(**fields), a_card(title="Vocab Quiz")]))

    assert reading.cards[0] is None
    assert reading.cards[1] is not None


@pytest.mark.parametrize("y", [-1, 1001])
def test_a_heading_outside_the_image_makes_the_answer_not_valid(y: int) -> None:
    """Case 5: a heading's place decides every card's day, so a bad one fails the read."""
    answer = an_answer(headings=[{"text": "Monday 10/5/2026", "y": y, "uncertain": None}])

    assert homework_reading(answer) == (None, "not_valid")


def test_a_rounds_key_stays_when_an_earlier_round_of_its_name_arrives() -> None:
    """Case 6: Vocab Quiz due 10/22, read alone, then with an image of the 10/8 round."""
    later = stored_homework(
        "two",
        homework(
            due("English 8", "Vocab Quiz", 200), headings=[heading("Thursday 10/22/2026", 150)]
        ),
    )
    earlier = stored_homework(
        "one",
        homework(
            due("English 8", "Vocab Quiz", 200), headings=[heading("Thursday 10/8/2026", 150)]
        ),
    )

    [alone] = keys(later)

    assert alone in keys(later, earlier)


# ------------------------------------------------------------------ no carry between screenshots
#
# Each scenario lists its screenshots, then the rows it gives (title, due date) and the items
# that stay visible (title, reason), the same in every order of its images.

WS = ("Math 8", "Worksheet 3")
SL, MW, U1 = (
    ("Science 8", "Spelling List 5"),
    ("Social Studies 8", "Map work"),
    ("Math 8", "U1 Quiz 3"),
)
LQ, V4 = ("Science 8", "Lab quiz"), ("English 8", "Vocab 4")


def undated(name: tuple[str, str], y_top: int) -> dict[str, object]:
    """An ASSIGNED card read whole with no due line, so it has no day of its own."""
    return dict(card(name, y_top), badge="ASSIGNED")


MON, TUE, WED = (
    "Monday 10/5/2026",
    "Tuesday 10/6/2026",
    "Wednesday 10/7/2026",
)
FRI, NEXT_MON = "Friday 10/9/2026", "Monday 10/12/2026"
UNREAD = a_card(title=42)
HIDDEN = [{"field": "instruction", "reason": "cut off"}, {"field": "footer", "reason": "cut off"}]
UNSURE_NAME = [{"field": "course", "reason": "blurred"}, {"field": "title", "reason": "blurred"}]
DOUBT = "partly hidden"

SCENARIOS: dict[
    str, tuple[list[dict[str, object]], list[tuple[str, str]], list[tuple[str, str]]]
] = {
    "p4 a clipped top copy": (
        [
            shot((WED, 250), card(V4, 300), card(SL, 500)),
            shot(card(SL, 0, clipped=True), (THU, 250), card(U1, 300)),
        ],
        [("Spelling List 5", "10-07"), ("U1 Quiz 3", "10-08"), ("Vocab 4", "10-07")],
        [("Spelling List 5", "clipped")],
    ),
    "c2 a closed tail before its closing heading": (
        [
            shot((MON, 150), card(RL, 200), card(WS, 300), (TUE, 450), card(SL, 500)),
            shot(card(WS, 20), (TUE, 250), card(SL, 300)),
        ],
        [("Reading log", "10-05"), ("Spelling List 5", "10-06"), ("Worksheet 3", "10-05")],
        [("Worksheet 3", "no_day")],
    ),
    "c2 a closed tail with no heading": (
        [
            shot((MON, 150), card(RL, 200), card(WS, 300), (TUE, 450), card(SL, 500)),
            shot(card(WS, 20)),
        ],
        [("Reading log", "10-05"), ("Spelling List 5", "10-06"), ("Worksheet 3", "10-05")],
        [("Worksheet 3", "no_day")],
    ),
    "c2 cards running past an open end": (
        [
            shot((WED, 150), card(RL, 200), card(WS, 300)),
            shot(card(WS, 20), card(SL, 120), (THU, 350), card(U1, 400)),
        ],
        [("Reading log", "10-07"), ("U1 Quiz 3", "10-08"), ("Worksheet 3", "10-07")],
        [("Spelling List 5", "no_day"), ("Worksheet 3", "no_day")],
    ),
    "c2 a chain through a screenshot with no heading": (
        [
            shot((MON, 150), card(RL, 200)),
            shot(card(RL, 20), card(WS, 120)),
            shot(card(WS, 20), card(SL, 120), (TUE, 350), card(U1, 400)),
        ],
        [("Reading log", "10-05"), ("U1 Quiz 3", "10-06")],
        [
            ("Reading log", "no_day"),
            ("Spelling List 5", "no_day"),
            ("Worksheet 3", "no_day"),
            ("Worksheet 3", "no_day"),
        ],
    ),
    "c4 S and T": (
        [
            shot(card(RL, 20), card(PS, 120), (MON, 250), card(RL, 300), card(PS, 400)),
            shot(card(RL, 20), card(PS, 120)),
        ],
        [("Practice set", "10-05"), ("Reading log", "10-05")],
        [
            ("Practice set", "no_day"),
            ("Practice set", "no_day"),
            ("Reading log", "no_day"),
            ("Reading log", "no_day"),
        ],
    ),
    "c4 S and U": (
        [
            shot(card(RL, 20), card(PS, 120), (MON, 250), card(RL, 300), card(PS, 400)),
            shot((TUE, 150), card(RL, 200), card(PS, 300)),
        ],
        [
            ("Practice set", "10-05"),
            ("Practice set", "10-06"),
            ("Reading log", "10-05"),
            ("Reading log", "10-06"),
        ],
        [("Practice set", "no_day"), ("Reading log", "no_day")],
    ),
    "c5 a top clip": (
        [
            shot((WED, 150), card(RL, 200), card(PS, 300)),
            shot(
                card(LQ, 0, clipped=True),
                card(RL, 90),
                card(PS, 190),
                card(MW, 290),
                (THU, 450),
                card(U1, 500),
            ),
        ],
        [("Practice set", "10-07"), ("Reading log", "10-07"), ("U1 Quiz 3", "10-08")],
        [
            ("Lab quiz", "clipped"),
            ("Map work", "no_day"),
            ("Practice set", "no_day"),
            ("Reading log", "no_day"),
        ],
    ),
    "c5 a bottom clip": (
        [
            shot((WED, 150), card(RL, 200), card(PS, 300), card(LQ, 920, clipped=True)),
            shot(card(RL, 20), card(PS, 120), (THU, 250), card(MW, 300)),
        ],
        [("Map work", "10-08"), ("Practice set", "10-07"), ("Reading log", "10-07")],
        [("Lab quiz", "clipped"), ("Practice set", "no_day"), ("Reading log", "no_day")],
    ),
    "c7 screenshots with no heading never chain": (
        [
            shot((MON, 100), card(("Math 8", "Y"), 200)),
            shot((TUE, 100), card(("Math 8", "Z"), 200)),
            shot(card(("Math 8", "Z"), 20), card(("Math 8", "Y"), 120)),
            shot(card(("Math 8", "Y"), 20), card(("Math 8", "W"), 120)),
            shot(card(("Math 8", "W"), 20), card(("Math 8", "V"), 120)),
        ],
        [("Y", "10-05"), ("Z", "10-06")],
        [
            ("V", "no_day"),
            ("W", "no_day"),
            ("W", "no_day"),
            ("Y", "no_day"),
            ("Y", "no_day"),
            ("Z", "no_day"),
        ],
    ),
    "c9 a clip whose bottom is past the image": (
        [
            shot((WED, 250), card(V4, 300), card(SL, 940, clipped=True)),
            shot(card(V4, 10), card(SL, 100), (THU, 350), card(U1, 400)),
        ],
        [("U1 Quiz 3", "10-08"), ("Vocab 4", "10-07")],
        [("Spelling List 5", "clipped"), ("Spelling List 5", "no_day"), ("Vocab 4", "no_day")],
    ),
    "the day before shown carries nothing": (
        [
            shot((WED, 150), card(RL, 200), card(WS, 300)),
            shot(card(MW, 20), (THU, 250), card(U1, 300)),
        ],
        [("Reading log", "10-07"), ("U1 Quiz 3", "10-08"), ("Worksheet 3", "10-07")],
        [("Map work", "no_day")],
    ),
    "both headings shown in a third screenshot carry nothing": (
        [
            shot((FRI, 150), card(WS, 200)),
            shot(card(MW, 20), (NEXT_MON, 250), card(LQ, 300)),
            shot((FRI, 100), card(SL, 150), (NEXT_MON, 300), card(LQ, 350)),
        ],
        [("Lab quiz", "10-12"), ("Spelling List 5", "10-09"), ("Worksheet 3", "10-09")],
        [("Map work", "no_day")],
    ),
    "a recurring pair above Thursday": (
        [
            shot((MON, 150), card(RL, 200), card(PS, 300)),
            shot(card(RL, 20), card(PS, 120), (THU, 250), card(U1, 300)),
        ],
        [("Practice set", "10-05"), ("Reading log", "10-05"), ("U1 Quiz 3", "10-08")],
        [("Practice set", "no_day"), ("Reading log", "no_day")],
    ),
    "a long day's middle with no heading": (
        [
            shot((WED, 150), card(RL, 200), card(WS, 300)),
            shot(card(WS, 20), card(SL, 120), card(MW, 220)),
            shot(card(MW, 20), card(LQ, 120), (THU, 250), card(U1, 300)),
        ],
        [("Reading log", "10-07"), ("U1 Quiz 3", "10-08"), ("Worksheet 3", "10-07")],
        [
            ("Lab quiz", "no_day"),
            ("Map work", "no_day"),
            ("Map work", "no_day"),
            ("Spelling List 5", "no_day"),
            ("Worksheet 3", "no_day"),
        ],
    ),
    "a four-screenshot scroll": (
        [
            shot((MON, 100), card(RL, 150)),
            shot(card(PS, 20), (TUE, 250), card(WS, 300)),
            shot(card(SL, 20), (WED, 250), card(MW, 300)),
            shot(card(LQ, 20), (THU, 250), card(U1, 300)),
        ],
        [
            ("Map work", "10-07"),
            ("Reading log", "10-05"),
            ("U1 Quiz 3", "10-08"),
            ("Worksheet 3", "10-06"),
        ],
        [("Lab quiz", "no_day"), ("Practice set", "no_day"), ("Spelling List 5", "no_day")],
    ),
    "an assigned card with no heading is a row by its own date": (
        [
            shot(
                assigned("Science 8", "Chains Reading Guide 6", "Due (10/8/2026)", 20),
                (THU, 250),
                card(U1, 300),
            )
        ],
        [("Chains Reading Guide 6", "10-08"), ("U1 Quiz 3", "10-08")],
        [],
    ),
    "an unread top card": (
        [shot(UNREAD, (THU, 250), card(U1, 300))],
        [("U1 Quiz 3", "10-08")],
        [("", "card_unreadable")],
    ),
    "a top clip with its top past the image": (
        [shot(card(SL, -40, clipped=True), (THU, 250), card(U1, 300))],
        [("U1 Quiz 3", "10-08")],
        [("Spelling List 5", "clipped")],
    ),
    "rule 5: a clip with no heading stays an item": (
        [
            shot(card(SL, 0, clipped=True), (THU, 250), card(U1, 300)),
            shot(card(SL, 20), (NEXT_MON, 250), card(LQ, 300)),
        ],
        [("Lab quiz", "10-12"), ("U1 Quiz 3", "10-08")],
        [("Spelling List 5", "clipped"), ("Spelling List 5", "no_day")],
    ),
    "rule 6: copies with no heading in two screenshots stay two items": (
        [
            shot(card(SL, 20), (THU, 250), card(U1, 300)),
            shot(card(SL, 20), (NEXT_MON, 250), card(LQ, 300)),
        ],
        [("Lab quiz", "10-12"), ("U1 Quiz 3", "10-08")],
        [("Spelling List 5", "no_day"), ("Spelling List 5", "no_day")],
    ),
    "clips of a recurring card under two days stay two items": (
        [
            shot((MON, 150), card(RL, 920, clipped=True)),
            shot((WED, 150), card(RL, 920, clipped=True)),
        ],
        [],
        [("Reading log", "clipped"), ("Reading log", "clipped")],
    ),
    "a card whose name wasn't read": (
        [shot((MON, 100), dict(card(WS, 200), title=None))],
        [],
        [("", "incomplete")],
    ),
    "a clip under a heading with no readable day stays an item": (
        [
            shot(("Monday", 150), assigned(*U1, "Due (10/", 920, clipped=True, uncertain=HIDDEN)),
            shot(("Monday", 150), assigned(*U1, "Due (10/8/2026)", 200)),
        ],
        [("U1 Quiz 3", "10-08")],
        [("U1 Quiz 3", "clipped")],
    ),
    "a clip stays an item beside a card in its own screenshot": (
        [shot((THU, 150), card(U1, 200), card(U1, 920, clipped=True, uncertain=HIDDEN))],
        [("U1 Quiz 3", "10-08")],
        [("U1 Quiz 3", "clipped")],
    ),
    "a clip under a heading read with doubt stays an item": (
        [
            shot((THU, 150, DOUBT), card(U1, 920, clipped=True, uncertain=HIDDEN)),
            shot((THU, 150), card(U1, 200)),
        ],
        [("U1 Quiz 3", "10-08")],
        [("U1 Quiz 3", "clipped")],
    ),
    "a clip stays an item beside a card under a heading read with doubt": (
        [
            shot((THU, 150), card(U1, 920, clipped=True, uncertain=HIDDEN)),
            shot((THU, 150, DOUBT), card(U1, 200)),
        ],
        [("U1 Quiz 3", "10-08")],
        [("U1 Quiz 3", "clipped")],
    ),
    "clips with one uncertain name in two screenshots stay two items": (
        [
            shot((THU, 150), card(("Ma", "Prac"), 920, clipped=True, uncertain=UNSURE_NAME)),
            shot((THU, 150), card(("Ma", "Prac"), 920, clipped=True, uncertain=UNSURE_NAME)),
        ],
        [],
        [("Prac", "clipped"), ("Prac", "clipped")],
    ),
    "copies under a heading with no readable day in two screenshots stay two items": (
        [shot(("Monday", 150), card(PS, 200)), shot(("Monday", 150), card(PS, 200))],
        [],
        [("Practice set", "no_day"), ("Practice set", "no_day")],
    ),
    "copies under a heading read with doubt in two screenshots stay two items": (
        [shot((THU, 150, DOUBT), undated(PS, 200)), shot((THU, 150, DOUBT), undated(PS, 200))],
        [],
        [("Practice set", "no_day"), ("Practice set", "no_day")],
    ),
    "two copies in each of two screenshots are four items": (
        [
            shot((THU, 150), undated(PS, 200), undated(PS, 300)),
            shot((THU, 150), undated(PS, 200), undated(PS, 300), card(U1, 400)),
        ],
        [("U1 Quiz 3", "10-08")],
        [("Practice set", "no_day")] * 4,
    ),
    "a card with no due line recaptured under its day is two items": (
        [shot((THU, 150), undated(PS, 200)), shot((THU, 150), undated(PS, 200), card(U1, 300))],
        [("U1 Quiz 3", "10-08")],
        [("Practice set", "no_day"), ("Practice set", "no_day")],
    ),
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_cards_take_a_day_only_from_their_own_date_or_a_heading_above_them(name: str) -> None:
    """No heading carries from one screenshot to another: a card is dated by its own due line
    or by the heading above it in its screenshot, and every other card stays a visible item
    with its reason, never merged into another."""
    shots, rows, items = SCENARIOS[name]

    assert in_every_order(name, shots) == {(tuple(rows), tuple(items))}


def test_two_unlabeled_cards_with_one_name_under_one_heading_are_two_note_rows() -> None:
    """A card with no label and no due line under a day heading is a teacher's note: two of
    them with one name in one screenshot are two unclear rows, one for each layer."""
    shots = [shot((THU, 150), card(PS, 200, badge=None), card(PS, 300, badge=None))]
    images = [stored_homework(f"two-notes-{place}", answer) for place, answer in enumerate(shots)]
    assembly = homework_assembly(images, now=NOW)

    assert assembly.unread == ()
    assert sorted(
        (
            row.layer,
            row.reading.title,
            row.reading.due_date,
            None if row.reading.teacher_note is None else str(row.reading.teacher_note.standing),
        )
        for row in assembly.rows
    ) == [(0, "Practice set", None, "unclear"), (1, "Practice set", None, "unclear")]


Read = tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]


def in_every_order(label: str, shots: list[dict[str, object]]) -> set[Read]:
    """What the screenshots read as in every order of their images: the rows (title, due
    date) and the items (title, reason), each sorted."""
    images = [stored_homework(f"{label}-{place}", answer) for place, answer in enumerate(shots)]
    found: set[Read] = set()
    for order in itertools.permutations(images):
        assembly = homework_assembly(list(order), now=NOW)
        found.add(
            (
                tuple(
                    sorted(
                        (r.reading.title, r.reading.due_date.strftime("%m-%d"))
                        for r in assembly.rows
                        if r.reading.due_date
                    )
                ),
                tuple(
                    sorted(
                        (str(i.card.title or "") if i.card else "", i.reason)
                        for i in assembly.unread
                    )
                ),
            )
        )
    return found


# ------------------------------------------------------------------ every clip stays an item

CUT = "cut off by the edge"


def reading_log(y_top: int) -> dict[str, object]:
    """A DUE Reading log card with the teacher's words."""
    return card(RL, y_top, instruction=LOG_WORDS)


def clipped_log() -> dict[str, object]:
    """A Reading log cut off at the image's bottom, its words marked as cut off."""
    cut = [{"field": "instruction", "reason": CUT}]
    return dict(reading_log(920), clipped=True, uncertain=cut)


def test_a_clip_stays_an_item_when_the_next_screenshot_shows_its_day() -> None:
    """Both screenshots show Monday above the Reading log the first cuts off: the clip stays
    an item beside the rows, since matching text never shows that two cards are one."""
    shots = [
        shot((MON, 150), card(WS, 200), clipped_log()),
        shot((MON, 150), card(WS, 200), reading_log(300)),
    ]

    assert in_every_order("next-shows-its-day", shots) == {
        (
            (("Reading log", "10-05"), ("Worksheet 3", "10-05")),
            (("Reading log", "clipped"),),
        )
    }


def test_a_whole_card_alone_elsewhere_never_hides_a_second_clipped_card() -> None:
    """One screenshot shows a whole Reading log and a clip of another under Monday; a second
    shows the whole card alone. Monday holds at least two, so the clip stays an item."""
    shots = [
        shot((MON, 150), reading_log(200), clipped_log()),
        shot((MON, 150), reading_log(200)),
    ]

    assert in_every_order("whole-alone-elsewhere", shots) == {
        ((("Reading log", "10-05"),), (("Reading log", "clipped"),))
    }


def test_a_clip_with_its_label_and_due_line_cut_stays_an_item() -> None:
    """A Vocab quiz clip with its label and due line cut off, and marked, could be the next
    round's ASSIGNED card: it stays an item beside the DUE Vocab quiz under the same Monday."""
    cut_off = [{"field": "badge", "reason": CUT}, {"field": "footer", "reason": CUT}]
    clip = card(("English 8", "Vocab quiz"), 920, badge=None, clipped=True, uncertain=cut_off)
    shots = [
        shot((MON, 150), card(WS, 200), clip),
        shot((MON, 150), card(("English 8", "Vocab quiz"), 300)),
    ]

    assert in_every_order("label-and-due-line-cut", shots) == {
        (
            (("Vocab quiz", "10-05"), ("Worksheet 3", "10-05")),
            (("Vocab quiz", "clipped"),),
        )
    }


def test_each_row_that_would_write_what_another_does_is_its_own_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stand-in for the card reader reads every card as one fixed reading, so three cards of
    one layer would write the same: one is a row, and each of the other two is a ``repeated``
    item of its own, however the layer's cards are handed to the reader."""
    fixed: list[Reading] = []

    def all_the_same(cards: list[PortalCard], *, now: datetime) -> CardsRead:
        if not fixed:
            fixed.append(read_cards(cards[:1], now=now).readings[0])
        return CardsRead(readings=(fixed[0],) * len(cards), joined=tuple(range(len(cards))))

    monkeypatch.setattr(screenshots, "read_cards", all_the_same)
    three = shot((THU, 150), card(RL, 200), card(PS, 300), card(WS, 400))

    assembly = homework_assembly([stored_homework("three", three)], now=NOW)

    assert len(assembly.rows) == 1
    assert [item.reason for item in assembly.unread] == ["repeated", "repeated"]


# ------------------------------------------------------------------ matching cards in one shot

VQ = ("English 8", "Vocab Quiz")


def row_keys(*images: StoredReading) -> frozenset[str]:
    """The row keys the screenshots give, the same in every order of their images."""
    found = {
        frozenset(row.key for row in homework_assembly(list(order), now=NOW).rows)
        for order in itertools.permutations(images)
    }
    [keys_found] = found
    return keys_found


def test_two_matching_cards_in_one_screenshot_are_two_rows_in_every_capture() -> None:
    """Two DUE Vocab Quiz cards under one Thursday are two rows with keys of their own. Another
    screenshot showing one copy or both adds no row; a screenshot of one copy alone is one row,
    with the first copy's key."""
    both = homework(due(*VQ, 200), due(*VQ, 300), headings=[heading(THU, 150)])
    one = homework(due(*VQ, 200), headings=[heading(THU, 150)])
    pair_of, single, again = (
        stored_homework("both", both),
        stored_homework("one", one),
        stored_homework("both-again", both),
    )

    two = row_keys(pair_of)

    assert len(two) == 2
    assert row_keys(pair_of, single) == two
    assert row_keys(pair_of, again) == two
    assert row_keys(pair_of, single, again) == two
    [alone] = row_keys(single)
    assert alone in two


def test_copies_listed_out_of_top_to_bottom_order_pair_by_their_top_edges() -> None:
    """A lists the Vocab Quiz with the first words above the one with the second; B shows the
    same screen but lists the lower card first. Copies are numbered top to bottom, so each of
    the two rows keeps its own words."""
    first, second = "Study list one and two.", "Study list three and four."
    upper, lower = due(*VQ, 200, instruction=first), due(*VQ, 300, instruction=second)
    images = [
        stored_homework("listed-top-down", homework(upper, lower, headings=[heading(THU, 150)])),
        stored_homework("listed-bottom-up", homework(lower, upper, headings=[heading(THU, 150)])),
    ]

    found = {
        tuple(
            sorted(
                tuple(sorted(seen.text for seen in row.reading.instructions))
                for row in homework_assembly(list(order), now=NOW).rows
            )
        )
        for order in itertools.permutations(images)
    }

    assert found == {((first,), (second,))}


def test_an_assigned_card_elsewhere_joins_only_the_first_of_two_matching_due_cards() -> None:
    """Vocab Quiz assigned Monday and due Thursday, with two DUE cards under Thursday in one
    screenshot: the ASSIGNED card joins the first copy's row, and the second copy is a row of
    its own with only its day's claim."""
    both = homework(due(*VQ, 200), due(*VQ, 300), headings=[heading(THU, 150)])
    on_monday = homework(assigned(*VQ, "Due (10/8/2026)", 200), headings=[heading(MON, 150)])
    images = [stored_homework("both", both), stored_homework("assigned", on_monday)]

    found = set()
    for order in itertools.permutations(images):
        rows = homework_assembly(list(order), now=NOW).rows
        found.add(
            tuple(
                sorted(
                    (
                        str(row.reading.assigned_on),
                        tuple(sorted(c.seen_in or "" for c in row.reading.claims)),
                    )
                    for row in rows
                )
            )
        )

    assert found == {
        (("2026-10-05", tuple(sorted((DAY_HEADER, OWN_LINE)))), ("None", (DAY_HEADER,)))
    }


def test_a_note_card_listed_once_in_each_of_two_screenshots_is_one_note_row() -> None:
    """Monday holds Reading log cards whose label wasn't read, so each is read as the teacher's
    note under Monday. One screenshot lists one; another lists one lower down: the same words
    under the same day in two screenshots are one note row with both cards as its sources."""

    def unlabeled(y_top: int) -> dict[str, object]:
        return card(RL, y_top, badge=None, instruction=LOG_WORDS)

    shots = [
        shot((MON, 150), card(WS, 200), unlabeled(300)),
        shot((MON, 150), card(WS, 200), unlabeled(500)),
    ]

    assert in_every_order("missed", shots) == {((("Worksheet 3", "10-05"),), ())}
    images = [stored_homework(f"missed-{place}", answer) for place, answer in enumerate(shots)]
    notes = [
        row
        for row in homework_assembly(images, now=NOW).rows
        if row.reading.teacher_note is not None
    ]
    assert [(row.reading.course, row.reading.due_date) for row in notes] == [("English 8", None)]
    assert {place.sha256 for place in notes[0].sources} == {image.sha256 for image in images}


def test_a_changed_reading_at_the_same_image_and_place_gives_new_item_keys() -> None:
    """Read again with a changed reading, the same image and places give new item keys, a card
    that couldn't be read included; the same stored reading gives the same keys in any order."""
    before = shot(UNREAD, (THU, 250), undated(PS, 300), card(U1, 400))
    after = shot(UNREAD, (THU, 250), undated(PS, 300), card(("Math 8", "U1 Quiz 4"), 400))
    first, changed = stored_homework("image", before), stored_homework("image", after)
    elsewhere = stored_homework("other", shot((MON, 150), undated(WS, 200)))

    def item_keys(*images: StoredReading) -> set[tuple[int | None, str]]:
        return {
            (item.place, item.key)
            for item in homework_assembly(list(images), now=NOW).unread
            if item.sha256 == first.sha256
        }

    kept = item_keys(first, elsewhere)
    renewed = item_keys(changed, elsewhere)

    assert first.sha256 == changed.sha256
    assert {place for place, _ in kept} == {place for place, _ in renewed} == {0, 1}
    assert item_keys(elsewhere, first) == kept
    assert {key for _, key in kept}.isdisjoint(key for _, key in renewed)


def test_a_changed_reading_that_couldnt_be_decoded_gives_a_new_item_key() -> None:
    undecodable = StoredReading(
        sha256=hashlib.sha256(b"synthetic-png-old").hexdigest(),
        kind="homework_list",
        prompt_version="v0",
        schema_version="screenshot-reading/0",
        body='{"cards": []}',
    )
    changed = undecodable._replace(body='{"cards": [1]}')

    [one] = homework_assembly([undecodable], now=NOW).unread
    [again] = homework_assembly([undecodable], now=NOW).unread
    [other] = homework_assembly([changed], now=NOW).unread

    assert one.reason == other.reason == "reading_unreadable"
    assert one.key == again.key
    assert one.key != other.key


def test_the_same_body_under_other_versions_gives_a_new_item_key() -> None:
    """A reading's digest holds its prompt and schema versions with its body: the same body
    stored under another prompt version or another schema version is another key."""
    stored = StoredReading(
        sha256=hashlib.sha256(b"synthetic-png-old").hexdigest(),
        kind="homework_list",
        prompt_version="v0",
        schema_version="screenshot-reading/0",
        body='{"cards": []}',
    )
    versions = [
        stored,
        stored._replace(prompt_version="v0a"),
        stored._replace(schema_version="screenshot-reading/0a"),
    ]

    items = [homework_assembly([reading], now=NOW).unread for reading in versions]

    assert [[item.reason for item in found] for found in items] == [["reading_unreadable"]] * 3
    assert len({found[0].key for found in items}) == 3


@pytest.mark.parametrize("prompt_version", ["v1b", "v1c", "v1d"])
def test_a_reading_stored_under_either_prompt_of_this_schema_decodes(prompt_version: str) -> None:
    """The prompts v1b, v1c and v1d share the answer schema, so a reading stored under any of
    them reads as its rows and places its cards."""
    stored = stored_homework("m", an_answer())._replace(prompt_version=prompt_version)

    assembly = homework_assembly([stored], now=NOW)

    assert stored.schema_version == "screenshot-reading/1"
    assert len(assembly.rows) == 2
    assert assembly.unread == ()
    assert len(screenshots.cards_placed([stored])) == 2


def test_the_prompt_in_use_is_one_a_stored_reading_decodes_under() -> None:
    stored = stored_homework("m", an_answer())

    assert stored.prompt_version == PROMPT_VERSION == "v1d"
    assert len(homework_assembly([stored], now=NOW).rows) == 2


@pytest.mark.parametrize(
    ("prompt_version", "schema_version"),
    [
        ("v1", "screenshot-reading/1"),
        ("v1a", "screenshot-reading/1"),
        ("v1e", "screenshot-reading/1"),
        ("V1B", "screenshot-reading/1"),
        ("v1b ", "screenshot-reading/1"),
        ("", "screenshot-reading/1"),
        ("v1b", "screenshot-reading/0"),
        ("v1b", "screenshot-reading/2"),
        ("v1c", "screenshot-reading/0"),
        ("v1c", "screenshot-reading/2"),
        ("v1c", ""),
    ],
)
def test_a_reading_stored_under_any_other_prompt_or_schema_version_is_not_decoded(
    prompt_version: str, schema_version: str
) -> None:
    """The decoder takes the closed set of prompt versions that share this schema and this
    schema version alone: the same text under any other version is one item that couldn't be
    read, and places no card."""
    stored = stored_homework("m", an_answer())._replace(
        prompt_version=prompt_version, schema_version=schema_version
    )

    assembly = homework_assembly([stored], now=NOW)

    assert assembly.rows == ()
    assert [item.reason for item in assembly.unread] == ["reading_unreadable"]
    assert screenshots.cards_placed([stored]) == {}


def test_a_decoded_reading_s_item_keys_hold_the_prompt_version_it_was_made_with() -> None:
    """A stored reading's digest holds its own prompt version, so the same reading stored
    under v1b and under v1c gives its clipped card two keys, and each key is steady."""
    clipped = an_answer(cards=[a_card(clipped=True)])
    older = stored_homework("m", clipped)._replace(prompt_version="v1b")
    newer = older._replace(prompt_version="v1c")

    [first] = homework_assembly([older], now=NOW).unread
    [again] = homework_assembly([older], now=NOW).unread
    [second] = homework_assembly([newer], now=NOW).unread

    assert first.reason == second.reason == "clipped"
    assert first.key == again.key
    assert first.key != second.key


PORTAL_FORM = "Monday 10/5/2026"
"""A day heading as the portal's homework list shows it."""
PRINTED_FORM = "10/5/2026 - Monday"
"""The same day as a printed homework list's dated day line."""
BULLETED_FORM = "- 10/5/2026 - Monday"
"""The printed day line with the bullet the print view can put before it."""
THAT_MONDAY = date(2026, 10, 5)


def stored_under(prompt_version: str, text: str) -> StoredReading:
    """The default list, an assigned card with its due line and a due card with none, under
    one heading with this text, stored under a prompt version."""
    answer = an_answer(headings=[heading(text, 150)])
    return stored_homework("m", answer)._replace(prompt_version=prompt_version)


@pytest.mark.parametrize(
    ("prompt_version", "text", "day"),
    [
        ("v1b", PORTAL_FORM, THAT_MONDAY),
        ("v1b", PRINTED_FORM, None),
        ("v1b", BULLETED_FORM, None),
        ("v1c", PORTAL_FORM, THAT_MONDAY),
        ("v1c", PRINTED_FORM, THAT_MONDAY),
        ("v1c", BULLETED_FORM, THAT_MONDAY),
        ("v1d", PORTAL_FORM, THAT_MONDAY),
        ("v1d", PRINTED_FORM, THAT_MONDAY),
        ("v1d", BULLETED_FORM, THAT_MONDAY),
    ],
    ids=[
        "v1b-portal",
        "v1b-printed",
        "v1b-bulleted",
        "v1c-portal",
        "v1c-printed",
        "v1c-bulleted",
        "v1d-portal",
        "v1d-printed",
        "v1d-bulleted",
    ],
)
def test_a_stored_reading_s_heading_names_a_day_as_its_prompt_version_reads_it(
    prompt_version: str, text: str, day: date | None
) -> None:
    """A reading stored under prompt v1b names a day by the portal's form alone. One stored
    under v1c, or under v1d, the prompt in use, names it by a printed list's dated day line too,
    with or without its bullet. The rows follow the heading as its reading's version reads it: the
    assigned card is assigned on that day or on none, and the due card, dated by its heading
    alone, is a row or a card with no day."""
    stored = stored_under(prompt_version, text)

    placed = screenshots.cards_placed([stored]).values()
    assembly = homework_assembly([stored], now=NOW)

    assert [None if found.heading is None else found.heading.day for found in placed] == [day] * 2
    assert [row.reading.assigned_on for row in assembly.rows if row.reading.assigned_on] == (
        [] if day is None else [day]
    )
    assert [item.reason for item in assembly.unread] == (["no_day"] if day is None else [])
    assert len(assembly.rows) == (1 if day is None else 2)


@pytest.mark.parametrize(
    ("prompt_version", "day"),
    [("v1b", None), ("v1c", THAT_MONDAY), ("v1d", THAT_MONDAY)],
    ids=["v1b", "v1c", "v1d"],
)
def test_a_copy_of_a_decoded_reading_reads_its_headings_as_the_reading_does(
    prompt_version: str, day: date | None
) -> None:
    """A copy of a decoded reading, shallow or deep, and a copy of one of its headings, read
    a printed day line as the decoded reading does: as no day under v1b, as its day under
    v1c. The decoded reading and each copy write the text the reading was stored as."""
    stored = stored_under(prompt_version, PRINTED_FORM)
    reading = screenshots._decoded(stored)
    assert reading is not None
    [found] = reading.headings

    readings = [
        copy.copy(reading),
        copy.deepcopy(reading),
        reading.model_copy(),
        reading.model_copy(deep=True),
    ]
    headings = [
        copy.copy(found),
        copy.deepcopy(found),
        found.model_copy(),
        found.model_copy(deep=True),
        found.model_copy(update={"uncertain": "smudged"}),
    ]

    assert found.day == day
    assert [made.headings[0].day for made in readings] == [day] * 4
    assert [made.day for made in headings] == [day] * 5
    assert reading.model_dump_json() == stored.body
    assert [made.model_dump_json() for made in readings] == [stored.body] * 4


@pytest.mark.parametrize("name", ["printed", *screenshots.Heading.__private_attributes__])
def test_no_answer_and_no_stored_text_can_say_how_its_headings_are_read(name: str) -> None:
    """How a heading's day is read is no property of an answer or of a stored reading: a
    heading that carries any name but the schema's three, a private one of the model
    included, makes a fresh answer not valid and a stored text one that can't be decoded."""
    carried = {"text": PRINTED_FORM, "y": 150, "uncertain": None, name: False}
    stored = stored_under("v1b", PRINTED_FORM)
    body = json.loads(stored.body)
    body["headings"] = [carried]

    assembly = homework_assembly([stored._replace(body=json.dumps(body))], now=NOW)

    assert homework_reading(an_answer(headings=[carried])) == (None, "not_valid")
    assert [item.reason for item in assembly.unread] == ["reading_unreadable"]
    assert assembly.rows == ()
    assert set(screenshots.Heading.model_json_schema()["properties"]) == {"text", "y", "uncertain"}


@pytest.mark.parametrize(
    "student",
    [
        {"name": "Rowan", "uncertain": None},
        {"name": "Wren", "uncertain": "partly hidden"},
        {"name": None, "uncertain": "not shown"},
    ],
    ids=["the-name-alone", "the-note-alone", "both"],
)
def test_a_change_to_the_selected_student_alone_keeps_every_key(student: dict[str, object]) -> None:
    """The selected student, name and note, is left out of a decoded reading's digest: a
    reading of the same image that differs only there gives the same item and row keys."""
    cards = shot(UNREAD, (THU, 250), undated(PS, 300), card(U1, 400))
    before = stored_homework("image", cards)
    after = stored_homework("image", dict(cards, active_student=student))
    elsewhere = stored_homework("other", shot((MON, 150), undated(WS, 200)))

    assert before.body != after.body
    assert keys(before, elsewhere) == keys(after, elsewhere)
    assert len([key for key in keys(before, elsewhere) if not key.readable]) == 3


def test_a_reading_that_doesnt_decode_is_keyed_by_its_whole_text() -> None:
    """A stored reading under today's versions whose text doesn't decode is keyed by that whole
    text, a name in it included: the same text gives the same key, and any change a new one."""
    stored = StoredReading(
        sha256=hashlib.sha256(b"synthetic-png-cut").hexdigest(),
        kind="homework_list",
        prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION,
        body='{"image_kind": "homework_list", "active_student": {"name": "Wren"',
    )
    renamed = stored._replace(body=stored.body.replace("Wren", "Rowan"))

    [one] = homework_assembly([stored], now=NOW).unread
    [again] = homework_assembly([stored], now=NOW).unread
    [other] = homework_assembly([renamed], now=NOW).unread

    assert one.reason == other.reason == "reading_unreadable"
    assert one.key == again.key
    assert one.key != other.key


def test_chains_with_an_instruction_keeps_mondays_card_day() -> None:
    """P 6 row 2 with the teacher's words: the copy at the next screenshot's top has no heading,
    so Monday's copy, walked first, keeps its day on the instruction."""
    words = "Read chapter 6 and answer the guide."
    chains = assigned(
        "Science 8", "Chains Reading Guide 6", "Due (10/8/2026)", 260, instruction=words
    )
    monday = homework(chains, headings=[heading(MON, 100)])
    thursday = homework(
        dict(chains, y_top=10, y_bottom=90),
        due("Science 8", "Chains Reading Guide 6", 200),
        headings=[heading(THU, 150)],
    )

    [row] = homework_assembly(
        [stored_homework("m", monday), stored_homework("t", thursday)], now=NOW
    ).rows

    assert [(seen.text, seen.card, seen.card_day) for seen in row.reading.instructions] == [
        (words, "assigned", date(2026, 10, 5))
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"clipped": True, "y_top": 300, "y_bottom": 250},
        {"clipped": True, "y_top": 1001, "y_bottom": 1080},
        {"clipped": True, "y_top": -90, "y_bottom": -10},
        {"clipped": False, "y_top": 940, "y_bottom": 1020},
        {"clipped": True, "y_top": 1000, "y_bottom": 1040},
        {"clipped": True, "y_top": -40, "y_bottom": 0},
    ],
    ids=[
        "clipped-reversed",
        "clipped-below-the-image",
        "clipped-above-the-image",
        "unclipped-past-the-edge",
        "clipped-from-the-bottom-edge-down",
        "clipped-up-to-the-top-edge",
    ],
)
def test_only_a_clipped_card_partly_in_the_image_is_brought_into_it(
    fields: dict[str, object],
) -> None:
    reading = checked(an_answer(cards=[a_card(**fields)]))

    assert reading.cards == (None,)


def test_a_clipped_card_partly_in_the_image_is_brought_to_its_edges() -> None:
    reading = checked(
        an_answer(
            cards=[
                a_card(clipped=True, y_top=-40, y_bottom=60),
                a_card(clipped=True, y_top=940, y_bottom=1020),
            ]
        )
    )

    assert [(c.y_top, c.y_bottom) for c in reading.cards if c is not None] == [(0, 60), (940, 1000)]


@pytest.mark.parametrize("clipped", [False, True], ids=["unclipped", "clipped"])
@pytest.mark.parametrize("edge", [1000, 0, 500], ids=["at-the-bottom", "at-the-top", "mid-image"])
def test_a_card_with_no_height_couldnt_be_read(edge: int, clipped: bool) -> None:
    """A card whose bottom edge is its top shows nothing, so it is a card that couldn't be
    read, never a row: at the image's bottom, at its top, or in between."""
    ghost = dict(card(("Math 8", "Ghost card"), edge, clipped=clipped), y_bottom=edge)
    shots = [shot((MON, 150), card(WS, 200), ghost)]

    assert in_every_order(f"no-height-{edge}-{clipped}", shots) == {
        ((("Worksheet 3", "10-05"),), (("", "card_unreadable"),))
    }


# ------------------------------------------------------------------ different dates stay separate

THU_15 = "Thursday 10/15/2026"
Q, CRG, SC = ("English 8", "Q"), ("Science 8", "Chains Reading Guide 6"), ("English 8", "SC")


def due_line(day: str) -> str:
    return f"Due ({day.split()[1]})"


def handed(name: tuple[str, str], due: str, y_top: int) -> dict[str, object]:
    """An ASSIGNED card whose due line names the day given."""
    return assigned(name[0], name[1], due_line(due), y_top)


def day_list(*days: str, names: tuple[tuple[str, str], ...] = (RL, PS)) -> dict[str, object]:
    """One screenshot of the days given, each heading followed by a DUE card of each name."""
    parts: list[object] = []
    top = 20
    for day in days:
        parts.append((day, top))
        top += 30
        for name in names:
            parts.append(card(name, top))
            top += 90
    return shot(*parts)


Written = tuple[str, str, str, tuple[str, ...]]


def rows_in_every_order(label: str, shots: list[dict[str, object]]) -> set[tuple[Written, ...]]:
    """Each row as its title, assigned day, due date and sorted date claims, sorted, once for
    every order of the images; the reasons of any items are asserted to be the same too."""
    images = [stored_homework(f"{label}-{place}", answer) for place, answer in enumerate(shots)]
    found: set[tuple[Written, ...]] = set()
    items: set[tuple[str, ...]] = set()
    for order in itertools.permutations(images):
        assembly = homework_assembly(list(order), now=NOW)
        found.add(
            tuple(
                sorted(
                    (
                        row.reading.title,
                        str(row.reading.assigned_on),
                        str(row.reading.due_date),
                        tuple(sorted(claim.asserted_value for claim in row.reading.claims)),
                    )
                    for row in assembly.rows
                )
            )
        )
        items.add(tuple(sorted(item.reason for item in assembly.unread)))
    assert items == {()}
    return found


def due_row(title: str, day: str, *claims: str) -> Written:
    return (title, "None", day, claims or (day,))


DATES: dict[str, tuple[list[dict[str, object]], list[Written]]] = {
    "one name under two days of one screenshot": (
        [shot((MON, 100), card(VQ, 150), (TUE, 300), card(VQ, 350))],
        [due_row("Vocab Quiz", "2026-10-05"), due_row("Vocab Quiz", "2026-10-06")],
    ),
    "a due card and the next round's assigned card under one day": (
        [shot((THU, 100), card(PS, 150), handed(PS, "Monday 10/12/2026", 250))],
        [
            due_row("Practice set", "2026-10-08"),
            ("Practice set", "2026-10-08", "2026-10-12", ("2026-10-12",)),
        ],
    ),
    "a due card and the next round's assigned card, with that round's due card elsewhere": (
        [
            shot((THU, 100), card(PS, 150), handed(PS, "Monday 10/12/2026", 250)),
            shot((NEXT_MON, 100), card(PS, 150)),
        ],
        [
            due_row("Practice set", "2026-10-08"),
            ("Practice set", "2026-10-08", "2026-10-12", ("2026-10-12", "2026-10-12")),
        ],
    ),
    "an assigned card elsewhere pairs with one of two matching due cards": (
        [
            shot((THU, 100), card(VQ, 150), card(VQ, 250)),
            shot((MON, 100), handed(VQ, THU, 150)),
        ],
        [
            ("Vocab Quiz", "2026-10-05", "2026-10-08", ("2026-10-08", "2026-10-08")),
            due_row("Vocab Quiz", "2026-10-08"),
        ],
    ),
    "an assigned card and its due card in one screenshot are one row": (
        [shot((MON, 100), handed(CRG, THU, 150), (THU, 300), card(CRG, 350))],
        [("Chains Reading Guide 6", "2026-10-05", "2026-10-08", ("2026-10-08", "2026-10-08"))],
    ),
    "one assigned card and two due cards: the first due card pairs": (
        [shot((MON, 100), handed(Q, THU, 150), (THU, 300), card(Q, 350), card(Q, 450))],
        [
            ("Q", "2026-10-05", "2026-10-08", ("2026-10-08", "2026-10-08")),
            due_row("Q", "2026-10-08"),
        ],
    ),
    "two assigned cards and one due card: the first assigned card pairs": (
        [shot((MON, 100), handed(Q, THU, 150), handed(Q, THU, 250), (THU, 400), card(Q, 450))],
        [
            ("Q", "2026-10-05", "2026-10-08", ("2026-10-08",)),
            ("Q", "2026-10-05", "2026-10-08", ("2026-10-08", "2026-10-08")),
        ],
    ),
    "an assigned card and its due card in two screenshots are one row": (
        [shot((TUE, 100), handed(SC, WED, 150)), shot((WED, 100), card(SC, 150))],
        [("SC", "2026-10-06", "2026-10-07", ("2026-10-07", "2026-10-07"))],
    ),
    "a recapture of one day joins that day's card": (
        [
            shot((MON, 100), card(RL, 150), (TUE, 300), card(RL, 350)),
            shot((TUE, 100), card(RL, 150)),
        ],
        [due_row("Reading log", "2026-10-05"), due_row("Reading log", "2026-10-06")],
    ),
    "two days in two screenshots and both in a third": (
        [
            shot((MON, 100), card(RL, 150)),
            shot((TUE, 100), card(RL, 150)),
            shot((MON, 100), card(RL, 150), (TUE, 300), card(RL, 350)),
        ],
        [due_row("Reading log", "2026-10-05"), due_row("Reading log", "2026-10-06")],
    ),
    "two days in two screenshots": (
        [shot((MON, 100), card(RL, 150)), shot((TUE, 100), card(RL, 150))],
        [due_row("Reading log", "2026-10-05"), due_row("Reading log", "2026-10-06")],
    ),
    "a chain through both kinds": (
        [
            shot((THU, 100), card(PS, 150), handed(PS, "Monday 10/12/2026", 250)),
            shot((NEXT_MON, 100), card(PS, 150)),
            shot((MON, 100), handed(PS, THU, 150)),
        ],
        [
            ("Practice set", "2026-10-05", "2026-10-08", ("2026-10-08", "2026-10-08")),
            ("Practice set", "2026-10-08", "2026-10-12", ("2026-10-12", "2026-10-12")),
        ],
    ),
    "a chain of three days, two of them in one screenshot": (
        [
            shot((MON, 100), card(RL, 150)),
            shot((TUE, 100), card(RL, 150)),
            shot((WED, 100), card(RL, 150)),
            shot((MON, 100), card(RL, 150), (WED, 300), card(RL, 350)),
        ],
        [
            due_row("Reading log", "2026-10-05"),
            due_row("Reading log", "2026-10-06"),
            due_row("Reading log", "2026-10-07"),
        ],
    ),
    "an assigned card's due line and a due card's day that differ": (
        [shot((MON, 100), handed(Q, THU, 150)), shot((FRI, 100), card(Q, 150))],
        [("Q", "2026-10-05", "2026-10-08", ("2026-10-08",)), due_row("Q", "2026-10-09")],
    ),
    "the ordinary scroll, Monday and Tuesday then Tuesday and Wednesday": (
        [day_list(MON, TUE, names=(RL,)), day_list(TUE, WED, names=(RL,))],
        [
            due_row("Reading log", "2026-10-05"),
            due_row("Reading log", "2026-10-06"),
            due_row("Reading log", "2026-10-07"),
        ],
    ),
    "a longer scroll, Monday to Wednesday then Wednesday to Friday": (
        [day_list(MON, TUE, WED, names=(RL,)), day_list(WED, THU, FRI, names=(RL,))],
        [
            due_row("Reading log", "2026-10-05"),
            due_row("Reading log", "2026-10-06"),
            due_row("Reading log", "2026-10-07"),
            due_row("Reading log", "2026-10-08"),
            due_row("Reading log", "2026-10-09"),
        ],
    ),
}

EIGHT_DAILY = sorted(
    due_row(title, day)
    for title in ("Reading log", "Practice set")
    for day in ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08")
)
CUTS: dict[str, list[dict[str, object]]] = {
    "one tall screenshot": [day_list(MON, TUE, WED, THU)],
    "two, without overlap": [day_list(MON, TUE), day_list(WED, THU)],
    "two, overlapping": [day_list(MON, TUE, WED), day_list(WED, THU)],
    "four single days": [day_list(MON), day_list(TUE), day_list(WED), day_list(THU)],
}


@pytest.mark.parametrize("name", DATES)
def test_different_dates_stay_separate_and_same_date_copies_join(name: str) -> None:
    """Cards of one name with different due dates are separate rows, in one screenshot or
    several; a same-date recapture joins, and an assigned card pairs one-to-one with a due
    card of its due date."""
    shots, rows = DATES[name]

    assert rows_in_every_order(name, shots) == {tuple(sorted(rows))}


@pytest.mark.parametrize("name", CUTS)
def test_one_list_gives_the_same_rows_however_it_is_cut_into_screenshots(name: str) -> None:
    """Four days with a daily Reading log and Practice set: eight rows, captured whole, in two
    screenshots with or without overlap, or one day at a time."""
    assert rows_in_every_order(name, CUTS[name]) == {tuple(EIGHT_DAILY)}


def marked(
    name: tuple[str, str], y_top: int, words: str, *, due: str | None = None
) -> dict[str, object]:
    """A DUE card, or with ``due`` an ASSIGNED card due that day, carrying its own words."""
    found = card(name, y_top) if due is None else handed(name, due, y_top)
    return dict(found, instruction=words)


def test_no_set_of_screenshots_changes_how_one_screenshots_cards_are_grouped() -> None:
    """Every card carries its own words. For each screenshot, which of its cards share a row is
    the same in every set of screenshots that holds it, and every card is in one row."""
    batch = {
        "a": shot(
            (MON, 100),
            marked(RL, 150, "Card a1."),
            (TUE, 300),
            marked(RL, 350, "Card a2."),
            marked(RL, 450, "Card a3."),
        ),
        "b": shot((TUE, 100), marked(RL, 150, "Card b1."), (WED, 300), marked(RL, 350, "Card b2.")),
        "c": shot(
            (MON, 100),
            marked(Q, 150, "Card c1.", due=THU),
            (THU, 300),
            marked(Q, 350, "Card c2."),
            marked(Q, 450, "Card c3."),
        ),
        "d": shot((THU, 100), marked(Q, 150, "Card d1."), (FRI, 300), marked(RL, 350, "Card d2.")),
    }
    stored = {
        label: stored_homework(f"grouping-{label}", answer) for label, answer in batch.items()
    }
    count = {"a": 3, "b": 2, "c": 3, "d": 2}
    words = {label: {f"Card {label}{n}." for n in range(1, count[label] + 1)} for label in batch}

    def grouping(labels: tuple[str, ...]) -> dict[str, frozenset[frozenset[str]]]:
        rows = homework_assembly([stored[label] for label in labels], now=NOW).rows
        held = [{seen.text for seen in row.reading.instructions} for row in rows]
        every = [text for texts in held for text in texts]
        assert len(every) == len(set(every))
        assert set(every) == set().union(*(words[label] for label in labels))
        return {
            label: frozenset(
                frozenset(texts & words[label]) for texts in held if texts & words[label]
            )
            for label in labels
        }

    alone = {label: grouping((label,))[label] for label in batch}
    assert alone == {
        "a": {frozenset({"Card a1."}), frozenset({"Card a2."}), frozenset({"Card a3."})},
        "b": {frozenset({"Card b1."}), frozenset({"Card b2."})},
        "c": {frozenset({"Card c1.", "Card c2."}), frozenset({"Card c3."})},
        "d": {frozenset({"Card d1."}), frozenset({"Card d2."})},
    }
    for size in range(2, len(batch) + 1):
        for labels in itertools.combinations(sorted(batch), size):
            for order in itertools.permutations(labels):
                found = grouping(order)
                assert {label: found[label] for label in labels} == {
                    label: alone[label] for label in labels
                }


def occurrences(*images: StoredReading) -> set[tuple[tuple[str, str, str], ...]]:
    """Each row's due date, occurrence (empty for none) and new-row id, sorted, once for every
    order."""
    return {
        tuple(
            sorted(
                (str(row.reading.due_date), row.reading.occurrence or "", row.reading.assignment_id)
                for row in homework_assembly(list(order), now=NOW).rows
            )
        )
        for order in itertools.permutations(images)
    }


def test_rows_a_week_or_more_apart_have_their_own_occurrence_in_every_layer() -> None:
    """Vocab Quiz once under 10/8 and twice under 10/15: both 10/15 rows take the later
    round's occurrence, and so a new-row id that isn't the 10/8 row's."""
    eighth = stored_homework("eighth", shot((THU, 100), card(VQ, 150)))
    fifteenth = stored_homework("fifteenth", shot((THU_15, 100), card(VQ, 150), card(VQ, 250)))

    [found] = occurrences(eighth, fifteenth)

    assert [(day, occurrence) for day, occurrence, _ in found] == [
        ("2026-10-08", ""),
        ("2026-10-15", "2026-10-15"),
        ("2026-10-15", "2026-10-15"),
    ]
    assert found[0][2] != found[1][2] == found[2][2]


def test_occurrences_follow_the_rounds_of_a_name_in_every_order() -> None:
    """Reading log under 10/5, 10/9 and 10/13 in one screenshot and under 10/1 in another: a
    row within a week of a round takes its occurrence, a later one starts a round."""
    days = shot(
        ("Monday 10/5/2026", 100),
        card(RL, 150),
        ("Friday 10/9/2026", 300),
        card(RL, 350),
        ("Tuesday 10/13/2026", 500),
        card(RL, 550),
    )
    first = shot(("Thursday 10/1/2026", 100), card(RL, 150))

    [found] = occurrences(stored_homework("days", days), stored_homework("first", first))

    assert [(day, occurrence) for day, occurrence, _ in found] == [
        ("2026-10-01", ""),
        ("2026-10-05", ""),
        ("2026-10-09", "2026-10-09"),
        ("2026-10-13", "2026-10-09"),
    ]


# ------------------------------------------------------------------ assigned dates that disagree

TUE_29, WED_30 = "Tuesday 9/29/2026", "Wednesday 9/30/2026"


def assigned_on(day: str, label: str) -> StoredReading:
    """One screenshot of an ASSIGNED Q due 10/8 under the day given."""
    return stored_homework(label, shot((day, 100), handed(Q, THU, 150)))


def the_row(*images: StoredReading) -> screenshots.ScreenshotRow:
    """The one row the screenshots give, the same in every order of their images."""
    found = [
        homework_assembly(list(order), now=NOW).rows for order in itertools.permutations(images)
    ]
    assert all(rows == found[0] for rows in found)
    [row] = found[0]
    return row


def test_two_assigned_dates_of_one_row_are_both_kept_with_their_sources() -> None:
    """ASSIGNED Q due 10/8 under 9/29 in one screenshot and under 10/5 in another: one row that
    keeps both assigned dates, each with its image and place, and settles on neither."""
    first, second = assigned_on(TUE_29, "first-day"), assigned_on(MON, "second-day")

    row = the_row(first, second)

    assert row.assigned == (
        screenshots.AssignedDay(date(2026, 9, 29), (screenshots.CardPlace(first.sha256, 0),)),
        screenshots.AssignedDay(date(2026, 10, 5), (screenshots.CardPlace(second.sha256, 0),)),
    )
    assert row.assigned_dates_disagree
    assert row.reading.assigned_on is None
    assert "assigned_on" not in row.reading.field_origins
    assert row.reading.due_date == date(2026, 10, 8)


def test_an_identical_recapture_keeps_the_row_and_its_key() -> None:
    """A recapture of the 9/29 screenshot as new bytes adds a source to 9/29 and nothing else."""
    first, second = assigned_on(TUE_29, "first-day"), assigned_on(MON, "second-day")
    again = assigned_on(TUE_29, "first-day-again")

    before, after = the_row(first, second), the_row(first, second, again)

    assert after.key == before.key
    assert after.reading == before.reading
    assert [day for day, _ in after.assigned] == [date(2026, 9, 29), date(2026, 10, 5)]
    assert set(after.assigned[0].sources) == {
        screenshots.CardPlace(first.sha256, 0),
        screenshots.CardPlace(again.sha256, 0),
    }


def test_a_new_assigned_date_gives_the_row_a_new_key() -> None:
    """A second assigned date makes a new key, and so does a third: a decision on the row as it
    was never covers a date it didn't show."""
    first, second = assigned_on(TUE_29, "first-day"), assigned_on(MON, "second-day")
    third, again = assigned_on(WED_30, "third-day"), assigned_on(TUE_29, "first-day-again")

    keys_seen = [
        the_row(first, again).key,
        the_row(first, second).key,
        the_row(first, second, third).key,
    ]

    assert len(set(keys_seen)) == 3
    assert the_row(first, again).reading.assigned_on == date(2026, 9, 29)
    assert not the_row(first, again).assigned_dates_disagree


def test_a_row_with_one_assigned_date_keeps_its_reading_and_key() -> None:
    """One assigned date, as one screenshot shows it: the row settles on it, with one source,
    and its key is the pinned key of a single-date row."""
    alone = assigned_on(MON, "single-assigned")

    row = the_row(alone)

    assert row.key == "0295c30bb83f9480a2262bed4aad401d8813ab9369bf64a7a308d06ae12016aa"
    assert row.reading.assigned_on == date(2026, 10, 5)
    assert row.assigned == (
        screenshots.AssignedDay(date(2026, 10, 5), (screenshots.CardPlace(alone.sha256, 0),)),
    )
    assert not row.assigned_dates_disagree


READ_CHAPTER = "Read chapter 3."


def worded_on(day: str, label: str) -> StoredReading:
    """One screenshot of an ASSIGNED Q due 10/8, with the teacher's words, under the day given."""
    worded = dict(handed(Q, THU, 150), instruction=READ_CHAPTER)
    return stored_homework(label, shot((day, 100), worded))


def instruction_days(row: screenshots.ScreenshotRow) -> list[tuple[str, str | None, date | None]]:
    return [(seen.text, seen.card, seen.card_day) for seen in row.reading.instructions]


def test_on_a_row_whose_assigned_dates_disagree_each_day_keeps_its_instruction() -> None:
    """The same words under 9/29 and 10/5, then under 9/30 too: the row keeps the words once
    for each day, so no day is chosen by the order the cards are read in."""
    first, second = worded_on(TUE_29, "worded-first"), worded_on(MON, "worded-second")
    third = worded_on(WED_30, "worded-third")

    two, three = the_row(first, second), the_row(first, second, third)

    assert instruction_days(two) == [
        (READ_CHAPTER, "assigned", date(2026, 9, 29)),
        (READ_CHAPTER, "assigned", date(2026, 10, 5)),
    ]
    assert instruction_days(three) == [
        (READ_CHAPTER, "assigned", date(2026, 9, 29)),
        (READ_CHAPTER, "assigned", date(2026, 9, 30)),
        (READ_CHAPTER, "assigned", date(2026, 10, 5)),
    ]
    assert two.key != three.key


def test_a_row_with_one_assigned_date_keeps_its_instruction_and_key() -> None:
    """One date, its words read twice: one instruction with that day, and the pinned key."""
    row = the_row(worded_on(TUE_29, "worded-once"), worded_on(TUE_29, "worded-again"))

    assert instruction_days(row) == [(READ_CHAPTER, "assigned", date(2026, 9, 29))]
    assert row.key == "85317a0ae62e78896891191304d4da42c6e0a6dd1515ed48073df9c850cf225e"


# ------------------------------------------------------------------ what a row names beside its key


def layered() -> tuple[StoredReading, StoredReading, StoredReading]:
    """Two DUE Vocab Quiz cards under Thursday, another screenshot of the first alone, and the
    ASSIGNED card under Monday due that Thursday."""
    return (
        stored_homework(
            "layered-both", homework(due(*VQ, 200), due(*VQ, 300), headings=[heading(THU, 150)])
        ),
        stored_homework("layered-one", homework(due(*VQ, 200), headings=[heading(THU, 150)])),
        stored_homework(
            "layered-assigned",
            homework(assigned(*VQ, "Due (10/8/2026)", 200), headings=[heading(MON, 150)]),
        ),
    )


def test_a_row_names_its_layer_and_every_card_it_came_from() -> None:
    """The first copy's row is layer 0 and came from both copies of that card and the ASSIGNED
    card; the second copy's row is layer 1 and came from its own card alone. The same in every
    order of the images."""
    both, one, on_monday = layered()

    found = {
        tuple(
            sorted((row.layer, row.sources) for row in homework_assembly(list(order), now=NOW).rows)
        )
        for order in itertools.permutations([both, one, on_monday])
    }

    first = sorted(
        [
            screenshots.CardPlace(both.sha256, 0),
            screenshots.CardPlace(one.sha256, 0),
            screenshots.CardPlace(on_monday.sha256, 0),
        ]
    )
    assert found == {((0, tuple(first)), (1, (screenshots.CardPlace(both.sha256, 1),)))}


def test_a_row_with_assigned_dates_that_disagree_names_every_card_it_came_from() -> None:
    """ASSIGNED Q under 9/29 in one screenshot and under 10/5 in another: one row, layer 0,
    from both cards, each at its place."""
    first, second = assigned_on(TUE_29, "named-first"), assigned_on(MON, "named-second")

    row = the_row(first, second)

    assert row.layer == 0
    assert row.sources == tuple(
        sorted([screenshots.CardPlace(first.sha256, 0), screenshots.CardPlace(second.sha256, 0)])
    )


PINNED_ROW_KEYS = (
    "476d9d2696b48bee43f54b36df057955c5b5a01fde92174ac51b9fddccee5a40",
    "963da1c1dd585fbe0b1296ea99ad57a19305e3453087ed9296b68c571e65b977",
    "afacd3b6018d7da2f5b4df18aa46c9eac4fbd3e3aa4e1d625a968330699028db",
)
"""The keys these screenshots gave before a row named its layer and cards."""


def test_a_rows_layer_and_cards_are_in_no_key() -> None:
    """Naming the layer and the cards changes no key: the rows function gives the keys it gave
    before, and every row's key is among them."""
    images = [*layered(), assigned_on(TUE_29, "pinned-first"), assigned_on(MON, "pinned-second")]

    given = homework_rows(images)

    assert tuple(row.key for row in given) == PINNED_ROW_KEYS
    assert {row.key for row in homework_assembly(images, now=NOW).rows} <= set(PINNED_ROW_KEYS)


# --------------------------------------------------------------- printed card lines (#258)

THAT_THURSDAY = date(2026, 10, 8)


def printed_card(badge: str | None, course: str, title: str, **fields: object) -> dict[str, Any]:
    """A printed card line as the prompt in use answers it, no instruction or due line unless
    one is given."""
    found = a_card(badge=badge, course=course, title=title, instruction=None, footer=None)
    found.update({"y_top": 210, "y_bottom": 270, **fields})
    return found


def printed_list(*cards: dict[str, Any], text: str = PRINTED_FORM) -> dict[str, Any]:
    """A printed list's answer: one day line with this text, and these cards beneath it."""
    return an_answer(headings=[heading(text, 150)], cards=list(cards))


@pytest.mark.parametrize(
    ("footer", "due"),
    [
        ("(Due:10/8/2026)", THAT_THURSDAY),
        ("(Due: 10/8/2026)", THAT_THURSDAY),
        ("Due (10/8/2026)", THAT_THURSDAY),
        ("( Due:10/8/2026 )", None),
        ("(due:10/8/2026)", None),
        ("(Due:10/8/26)", None),
        ("(Due:10/32/2026)", None),
    ],
    ids=[
        "printed",
        "printed-spaced",
        "portal",
        "spaced-parens",
        "lowercase",
        "short-year",
        "no-date",
    ],
)
def test_a_due_line_read_under_the_prompt_in_use_takes_the_printed_form_as_school_text_does(
    footer: str, due: date | None
) -> None:
    """Under v1d, the prompt in use, a card's due line names its date in the portal's form or
    in a printed card line's, "(Due:M/D/YYYY)" with spaces after the colon, as School text
    reads it; no looser form, and no digits that name no date. A fresh card, a stored one and
    the review's dates all read it alike."""
    answer = printed_list(printed_card("ASSIGNED", "Art", "Poster", footer=footer))
    stored = stored_homework("m", answer)
    fresh = homework_reading(answer).reading
    assert fresh is not None
    [placed] = screenshots.cards_placed([stored]).values()
    [seen] = dates_seen([stored], [placed.place])

    assert fresh.cards[0] is not None
    assert fresh.cards[0].due == due
    assert placed.card is not None
    assert placed.card.due == due
    assert seen.line_day == due
    assert [row.reading.due_date for row in homework_assembly([stored], now=NOW).rows] == (
        [] if due is None else [due]
    )


@pytest.mark.parametrize("prompt_version", ["v1b", "v1c"])
def test_a_reading_stored_under_an_earlier_prompt_reads_the_portal_due_line_alone(
    prompt_version: str,
) -> None:
    """A reading stored under v1b or v1c reads a due line in the portal's form alone: a printed
    Assigned card stays a card read whole with no date, for the review's dates too, and a card
    with the portal's due line is a row, as on main."""
    answer = printed_list(
        printed_card("ASSIGNED", "Art", "Poster", footer="(Due:10/8/2026)"),
        printed_card(
            "ASSIGNED", "Music", "Song", footer="Due (10/9/2026)", y_top=300, y_bottom=360
        ),
    )
    stored = stored_homework("m", answer)._replace(prompt_version=prompt_version)
    placed = list(screenshots.cards_placed([stored]).values())
    seen = dates_seen([stored], [found.place for found in placed])
    assembly = homework_assembly([stored], now=NOW)

    assert [found.card.due for found in placed if found.card is not None] == [
        None,
        date(2026, 10, 9),
    ]
    assert [each.line_day for each in seen] == [None, date(2026, 10, 9)]
    assert [item.reason for item in assembly.unread] == ["no_day"]
    assert [row.reading.course for row in assembly.rows] == ["Music"]


@pytest.mark.parametrize("name", list(screenshots.Card.__private_attributes__))
def test_no_answer_and_no_stored_text_can_say_how_a_card_s_due_line_is_read(name: str) -> None:
    """How a card's due line is read is no property of an answer or of a stored reading: a card
    that carries a private name of the model is a card that couldn't be read in a fresh answer,
    and makes a stored text one that can't be decoded."""
    carried = {**printed_card("ASSIGNED", "Art", "Poster", footer="(Due:10/8/2026)"), name: False}
    stored = stored_homework("m", printed_list())._replace(prompt_version="v1c")
    body = json.loads(stored.body)
    body["cards"] = [carried]

    fresh = homework_reading(printed_list(carried)).reading
    assembly = homework_assembly([stored._replace(body=json.dumps(body))], now=NOW)

    assert screenshots.Card.__private_attributes__
    assert fresh is not None
    assert fresh.cards == (None,)
    assert [item.reason for item in assembly.unread] == ["reading_unreadable"]


@pytest.mark.parametrize(
    ("instruction", "kept", "aside"),
    [
        ("Due: check answers with a partner", [], "Due: check answers with a partner"),
        (
            "Due: check answers with a partner\nUse a pencil.",
            [],
            "Due: check answers with a partner\nUse a pencil.",
        ),
        (
            "Show your work.\nDue: check answers with a partner\nUse a pencil.",
            ["Show your work."],
            "Due: check answers with a partner\nUse a pencil.",
        ),
        (
            "Finish it at home (Due: Friday)\nUse a pencil.",
            [],
            "Finish it at home (Due: Friday)\nUse a pencil.",
        ),
        (
            "Show your work.\n10/9/2026 bring the folder",
            ["Show your work."],
            "10/9/2026 bring the folder",
        ),
        (
            "Show your work.\nScience - Due: Lab report:",
            ["Show your work."],
            "Science - Due: Lab report:",
        ),
    ],
    ids=[
        "label-line",
        "label-line-then-plain",
        "plain-first",
        "due-text-inside",
        "a-date",
        "a-card-line",
    ],
)
def test_a_printed_card_s_instruction_is_set_apart_from_its_first_line_shaped_as_structure(
    instruction: str, kept: list[str], aside: str
) -> None:
    """On a card under a printed day line, read under the prompt in use, the first instruction
    line shaped like a day, a card or a due date, the shapes School text sets aside when a line
    doesn't read as one, and every line after it, are one item shown apart, with the card's
    class and title; the card is a row with the lines before it as its instruction. The item
    and the row have keys of their own, steady for the same reading."""
    answer = printed_list(printed_card("DUE", "Math", "Worksheet 4", instruction=instruction))
    stored = stored_homework("m", answer)

    assembly = homework_assembly([stored], now=NOW)
    again = homework_assembly([stored], now=NOW)
    [row] = assembly.rows
    [item] = assembly.unread

    assert (row.reading.course, row.reading.title, row.reading.due_date) == (
        "Math",
        "Worksheet 4",
        THAT_MONDAY,
    )
    assert [seen.text for seen in row.reading.instructions] == kept
    assert item.reason == "instruction_set_aside"
    assert item.place == 0
    assert item.card is not None
    assert (item.card.course, item.card.title, item.card.instruction) == (
        "Math",
        "Worksheet 4",
        aside,
    )
    assert item.key != row.key
    assert [row.key for row in again.rows] == [row.key]
    assert [found.key for found in again.unread] == [item.key]
    assert sorted(found.key for found in homework_rows([stored])) == sorted([row.key, item.key])


def test_the_review_reads_a_set_apart_card_with_the_lines_it_keeps() -> None:
    """The review reads a set-apart card as its row does, with the lines it keeps and never the
    lines set apart: its placed instruction is its row's text, and an instruction the reader
    wasn't sure of is tied to that text."""
    unsure = [{"field": "instruction", "reason": "faint"}]
    answer = printed_list(
        printed_card(
            "DUE",
            "Math",
            "Worksheet 4",
            instruction="Show your work.\nDue: check answers with a partner\nUse a pencil.",
            uncertain=unsure,
        )
    )
    stored = stored_homework("m", answer)
    placed = screenshots.cards_placed([stored])
    [row] = homework_assembly([stored], now=NOW).rows
    texts = [seen.text for seen in row.reading.instructions]
    [found] = placed.values()

    assert found.card is not None
    assert texts == ["Show your work."]
    assert found.card.instruction == "Show your work."
    assert unsure_texts_of(placed, row.sources, texts) == ((found.place, 0),)
    assert wordless_of(placed, row.sources) == ()


def test_a_set_apart_card_with_no_line_kept_is_read_as_a_card_with_no_words() -> None:
    """A card whose every instruction line is set apart is read by the review as a card read
    with no words: when the reader wasn't sure of its instruction, it is a wordless card whose
    instruction the family can type, as a card read with none is."""
    unsure = [{"field": "instruction", "reason": "faint"}]
    set_apart = printed_card(
        "DUE",
        "Math",
        "Worksheet 4",
        instruction="Due: check answers with a partner\nUse a pencil.",
        uncertain=unsure,
    )
    no_words = printed_card("DUE", "Math", "Worksheet 4", uncertain=unsure)
    given = []
    for card_read in (set_apart, no_words):
        stored = stored_homework("m", printed_list(card_read))
        placed = screenshots.cards_placed([stored])
        [row] = homework_assembly([stored], now=NOW).rows
        [found] = placed.values()
        given.append(
            (
                found.card.instruction if found.card is not None else "no card",
                wordless_of(placed, row.sources) == (found.place,),
                wordless_of(placed, row.sources, unsure=False),
            )
        )

    assert given == [(None, True, ()), (None, True, ())]


@pytest.mark.parametrize(
    ("prompt_version", "text"),
    [("v1d", PORTAL_FORM), ("v1c", PRINTED_FORM), ("v1b", PORTAL_FORM)],
    ids=["portal-heading", "v1c", "v1b"],
)
def test_an_instruction_line_shaped_like_a_label_stays_on_a_portal_card_or_an_earlier_reading(
    prompt_version: str, text: str
) -> None:
    """A card under the portal's day heading, or any card of a reading stored under v1b or v1c,
    keeps every line of its instruction, a line shaped like a card's label included, as on
    main."""
    answer = printed_list(
        printed_card("DUE", "Math", "Worksheet 4", instruction="Due: Friday in class\nBring it."),
        text=text,
    )
    stored = stored_homework("m", answer)._replace(prompt_version=prompt_version)

    assembly = homework_assembly([stored], now=NOW)
    [row] = assembly.rows
    [found] = screenshots.cards_placed([stored]).values()

    assert assembly.unread == ()
    assert [seen.text for seen in row.reading.instructions] == ["Due: Friday in class\nBring it."]
    assert found.card is not None
    assert found.card.instruction == "Due: Friday in class\nBring it."


def test_a_printed_card_that_cant_be_dated_stays_one_card_whatever_its_instruction() -> None:
    """A printed Assigned card with no due line can't be dated, so it stays one card read whole
    with no date, its instruction whole, and no line of it is shown apart."""
    answer = printed_list(
        printed_card("ASSIGNED", "Art", "Poster", instruction="Due: Thursday\nBring markers.")
    )

    assembly = homework_assembly([stored_homework("m", answer)], now=NOW)

    assert assembly.rows == ()
    assert [(item.reason, item.card.instruction) for item in assembly.unread if item.card] == [
        ("no_day", "Due: Thursday\nBring markers.")
    ]
