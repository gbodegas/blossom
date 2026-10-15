# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Checking a screenshot reading's answer before anything of it is kept.

Every answer here is synthetic, written as a fake reader would return it: the parsed JSON of
the answer schema. The student is Wren; every other name is invented.
"""

import copy
import json
import logging
from datetime import date
from typing import Any, get_args

import pytest

from blossom import screenshots
from blossom.screenshot_prompt import SCHEMA
from blossom.screenshots import Card, HomeworkReading, homework_reading
from blossom.stores.import_drafts import FAILURES


def a_card(**fields: object) -> dict[str, Any]:
    """One card as the schema shapes it, every property present."""
    card: dict[str, Any] = {
        "badge": "ASSIGNED",
        "course": "English 8",
        "title": "Sentence Composing",
        "instruction": "Complete pages 12 to 14.",
        "footer": "Due (10/8/2026)",
        "y_top": 210,
        "y_bottom": 340,
        "clipped": False,
        "uncertain": [],
    }
    card.update(fields)
    return card


def an_answer(**fields: object) -> dict[str, Any]:
    """A homework list's answer: one heading and two cards, the selected student Wren."""
    answer: dict[str, Any] = {
        "image_kind": "homework_list",
        "active_student": {"name": "Wren", "uncertain": None},
        "headings": [{"text": "Monday 10/5/2026", "y": 150, "uncertain": None}],
        "cards": [
            a_card(),
            a_card(
                badge="DUE",
                course="Science 8",
                title="Chains Reading Guide 6",
                instruction=None,
                footer=None,
                y_top=360,
                y_bottom=430,
            ),
        ],
        "report": None,
    }
    answer.update(fields)
    return answer


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


# ------------------------------------------------------------------ the selected student


def test_only_the_selected_tab_is_kept() -> None:
    """The answer names the selected student alone; a field for other tabs doesn't fit."""
    reading = checked(an_answer())
    others = an_answer(
        active_student={"name": "Wren", "uncertain": None, "other_tabs": ["Rowan", "Ellis"]}
    )

    assert reading.student.name == "Wren"
    assert homework_reading(others) == (None, "not_valid")


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
