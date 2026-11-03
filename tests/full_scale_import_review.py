# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""An import review's presses at the largest sizes, too long for the default suite.

Run it by naming this file: its name keeps it out of a plain ``pytest``, while ruff and mypy still
check it. A test in ``tests/test_import_review_bounds.py`` holds its shapes to the acceptance list,
which can't show that this module ran; only a run's own output can. It doesn't run in continuous
integration.
"""

import pathlib
from typing import Final
from urllib.parse import quote_plus

import pytest
from fastapi.testclient import TestClient

from tests.import_review_cases import (
    BEYOND_THE_PLANE,
    FORM,
    MOST_BYTES,
    MOST_FIELDS,
    MOST_IN_A_FIELD,
    TYPED,
    all_found_onto_one_row,
    as_a_browser_sends,
    at_their_limits,
    body_of_fields,
    body_with_a_paste,
    box_named,
    every_box_at_its_limit,
    every_choice,
    every_press_answered,
    every_question,
    every_row,
    followed,
    grouped,
    has_review_form,
    here,
    in_its_box,
    instructions_answered,
    largest_a_page_makes,
    new_rows,
    numbered,
    on_record,
    open_settings,
    options_of,
    page_post,
    page_text,
    plain,
    press_over_socket,
    press_through,
    random_cjk,
    read_again,
    read_again_unsure,
    saved_rows,
    send,
    serving,
    the_worst_page,
    units_on,
    with_a_kept_question,
    written_as_the_older_form,
)

FULL_SIZE_SHAPES: Final = (
    "300 plain",
    "600 plain",
    "460 every question",
    "600 every question",
    "600 in groups of 20",
    "300 in groups of 60",
    "600 of one name",
    "450 on saved rows",
    "600 on saved rows",
    "450 every question on saved rows",
    "600 every question on saved rows",
    "600 saved rows, each with 1 kept of 4,000 random CJK",
    "one card, 18,100 kept of a few characters",
    "one card, 199,900 kept of a few characters",
)
"""The shapes of the acceptance list the default suite is too short to press."""
ANSWERS: Final = {"keep": 200, "later": 303, "save": 200}
NOT_READ: Final = "That form could not be read, so nothing was saved."
FORTY_TWO_MB: Final = "600 saved rows, each with 1 kept of 4,000 random CJK"
ROOM: Final = 16 * 1024
"""What a paste leaves under the byte limit, so the press from the page it returns is read too."""


@pytest.mark.parametrize("shape", FULL_SIZE_SHAPES)
def test_every_press_at_each_size(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """Keep, Keep from its page, Later, Keep from Later's page, Save, and the presses from Save's
    page, with every field a page made within 1 MiB as sent."""
    with here(open_settings(tmp_path)) as browser:
        presses = press_through(browser, shape, monkeypatch)

    every_press_answered(presses, shape)


def test_the_worst_page_at_full_scale(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six hundred cards on saved rows with every question and box, each date typed as text no date
    reads as, and every box ticked: 28 fields a card and 18 for the page."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, fields = the_worst_page(browser, monkeypatch, 10, 60)
        before = every_row(browser)
        answer = send(browser, draft_id, fields)
        after = every_row(browser)

    assert len(fields) == 28 * 600 + 18
    assert largest_a_page_makes(fields)[0] <= 1024 * 1024
    assert answer.status_code == 422, page_text(answer.text)[:300]
    assert before == after


def test_every_box_at_its_limit(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Six hundred cards, each box at its limit in characters of four bytes, each new instruction
    ticked, each date carrying its text: the largest body of ordinary fields a real page sends."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, fields = every_box_at_its_limit(browser, monkeypatch, 10, 60)
        answer = send(browser, draft_id, fields)

    body = as_a_browser_sends(fields)
    assert answer.status_code == 200, page_text(answer.text)[:300]
    assert 100 * 1024 < len(body) / 600 <= 180 * 1024
    assert len(body) <= MOST_BYTES
    assert largest_a_page_makes(fields)[0] <= 1024 * 1024


def test_the_longest_name_a_tick_carries_with_every_box_at_its_limit(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six hundred cards of one name found onto one saved row, so one instruction question names
    every card and each of its ticks goes under that name. Each card's new instruction is at its
    limit too, so a card sends all its bytes can come to: every box, its words, and that name."""
    words = BEYOND_THE_PLANE * 3_980
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = all_found_onto_one_row(browser, monkeypatch, 10, 60, words)
        short = every_choice(page, "keep")
        fields = at_their_limits(short, "course", "title", "instruction", "due", "assigned")
        answer = send(browser, draft_id, fields)
        stored = instructions_answered(browser, draft_id)

    ticks = [value for name, value in fields if name == question]
    body = as_a_browser_sends(fields)
    assert len(ticks) == 601
    assert len(quote_plus(question, safe="*-._")) == 43_891
    assert answer.status_code == 200, page_text(answer.text)[:300]
    assert len(body) / 600 <= 180 * 1024
    assert len(body) <= MOST_BYTES
    assert largest_a_page_makes(fields)[0] <= 1024 * 1024
    assert any(len(kept) == 601 for kept in stored)


def test_ticks_under_the_longest_name_up_to_137_mb(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six hundred cards found onto one saved row holding 2,500 more kept rows, every tick under the
    name that holds every card: real ticks that bring a body near the byte limit."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = all_found_onto_one_row(browser, monkeypatch, 10, 60, more=2_500)
        box = box_named(page, "title")
        fields = every_choice(page, "keep", {box: TYPED})
        answer = send(browser, draft_id, fields)
        shown = followed(browser, answer)
        typed_back = in_its_box(shown, box)
        again = send(browser, draft_id, every_choice(shown, "keep"))

    body = as_a_browser_sends(fields)
    assert len(quote_plus(question, safe="*-._")) == 43_891
    assert 130_000_000 < len(body) <= MOST_BYTES
    assert answer.status_code == 200, page_text(shown)[:300]
    assert typed_back == TYPED
    assert again.status_code == 200, page_text(again.text)[:300]


def pressed_until_asked(browser: TestClient, draft_id: str, page: str) -> str:
    """The page after this one's Keeps, once a question asks which instructions apply."""
    for _ in range(3):
        if "answer:instructions:" in page:
            break
        answer = send(browser, draft_id, every_choice(page, "keep"))
        assert answer.status_code == 200, page_text(answer.text)[:300]
        page = followed(browser, answer)
    return page


def large_older_page(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch, kind: str
) -> tuple[str, str, str]:
    """A page of this kind in the older form, with the box to type in: its review's id, the page and
    the field of the box."""
    if kind == "600 every question":
        draft_id, page = new_rows(browser, monkeypatch, 10, 60, every_question, name=True)
    elif kind == "600 of one name":
        draft_id, page = grouped(browser, monkeypatch, 10, 60, 600)
    elif kind == "1 kept of 3,000,000 CJK":
        draft_id, page, _ = with_a_kept_question(
            browser,
            monkeypatch,
            more=1,
            words=lambda _, __: "\u65e5" * 3_000_000,
            again=read_again_unsure,
        )
        return draft_id, written_as_the_older_form(browser, draft_id, page), "instruction"
    else:
        draft_id, page = saved_rows(
            browser, monkeypatch, 10, 60, read_again, more=1, words=random_cjk
        )
        page = pressed_until_asked(browser, draft_id, page)
    return draft_id, written_as_the_older_form(browser, draft_id, page), "title"


@pytest.mark.parametrize("press", ["keep", "later", "save"])
@pytest.mark.parametrize(
    "kind",
    ["600 every question", "600 of one name", "1 kept of 3,000,000 CJK", FORTY_TWO_MB],
)
def test_a_large_page_made_before_the_compact_list(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, kind: str, press: str
) -> None:
    """An older page open across the upgrade, its list past the parser's default as sent, up to
    42 MB and 51 MB: its press answers as a new page's does, its typing survives, and the page it
    comes back with is compact and can be pressed."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, older, field = large_older_page(browser, monkeypatch, kind)
        box = box_named(older, field)
        fields = every_choice(older, press, {box: TYPED})
        answer = send(browser, draft_id, fields)
        shown = followed(browser, answer)
        record = on_record(browser, draft_id)
        again = send(browser, draft_id, every_choice(shown, "keep"))

    assert 1024 * 1024 < largest_a_page_makes(fields)[0] <= MOST_IN_A_FIELD
    assert len(as_a_browser_sends(fields)) <= MOST_BYTES
    assert answer.status_code == ANSWERS[press], page_text(shown)[:300]
    assert has_review_form(shown)
    assert in_its_box(shown, box) == TYPED
    assert dict(page_post(shown, "keep"))["asked"].startswith("z1.")
    assert again.status_code == 200
    assert press != "later" or TYPED in record.progress


def test_the_42_mb_page_made_before_the_compact_list_saves_its_groups(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = saved_rows(
            browser, monkeypatch, 10, 60, read_again, more=1, words=random_cjk
        )
        page = pressed_until_asked(browser, draft_id, page)
        for _ in range(2):
            page = followed(browser, send(browser, draft_id, every_choice(page, "keep")))
        older = written_as_the_older_form(browser, draft_id, page)
        fields = every_choice(older, "save")
        answer = send(browser, draft_id, fields)

    assert largest_a_page_makes(fields)[0] > 40_000_000
    assert len(units_on(older)) == 600
    assert answer.status_code == 200, page_text(answer.text)[:300]
    assert "600 updated." in page_text(answer.text)


def test_a_page_of_199900_kept_rows_made_before_the_compact_list(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every tick holds its row's words, each matched with its row, and the press from the page it
    comes back with ticks the rows themselves."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, _ = with_a_kept_question(browser, monkeypatch, more=199_900, words=numbered)
        older = written_as_the_older_form(browser, draft_id, page)
        box = box_named(older, "instruction")
        fields = every_choice(older, "keep", {box: TYPED})
        answer = send(browser, draft_id, fields)
        typed_back = in_its_box(answer.text, box)
        again = send(browser, draft_id, every_choice(answer.text, "keep"))

    assert len(fields) <= MOST_FIELDS
    assert answer.status_code == 200, page_text(answer.text)[:300]
    assert typed_back == TYPED
    assert again.status_code == 200, page_text(again.text)[:300]


@pytest.mark.parametrize("total", [None, MOST_BYTES], ids=["short", "long"])
def test_fields_at_the_limit_are_read_and_one_past_it_are_not(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, total: int | None
) -> None:
    """A small page's post padded by hand, over a socket, on all three presses, with fields as short
    as they can be or as long as the byte limit lets them all be."""
    with here(open_settings(tmp_path)) as browser, serving(browser) as port:
        draft_id, page = new_rows(browser, monkeypatch, 1, 3, plain, name=True)
        box = box_named(page, "title")
        for press, status in ANSWERS.items():
            shown = browser.get(f"/parent/imports/{draft_id}").text
            ticks = units_on(shown)[1:2] if press == "save" else []
            fields = page_post(shown, press, typed={box: TYPED}, tick=ticks)
            past = body_of_fields(fields, MOST_FIELDS + 1, total)
            before = every_row(browser)
            refused = press_over_socket(port, draft_id, [past], length=len(past))
            assert every_row(browser) == before
            assert refused.status == 400
            assert NOT_READ in page_text(refused.text)
            at = body_of_fields(fields, MOST_FIELDS, total)
            assert total is None or len(at) == total
            answered = press_over_socket(port, draft_id, [at], length=len(at))
            assert answered.status == status, page_text(answered.text)[:300]


def test_a_conflict_on_199900_kept_rows_comes_back_as_posted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every kept row ticked beside "None of these apply", with nothing kept."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(
            browser, monkeypatch, more=199_900, words=numbered
        )
        fields = [*every_choice(page, "keep"), (question, "")]
        before = every_row(browser)
        answer = send(browser, draft_id, fields)
        after = every_row(browser)

    shown = options_of(answer.text, question)
    assert len(fields) <= MOST_FIELDS
    assert answer.status_code == 422
    assert before == after
    assert len(shown) > 199_900
    assert all(ticked for value, ticked in shown if value.startswith("k") or not value)


def test_600_rows_of_290_kept_rows(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every row ticking every kept row, its typing kept in its box, and the press from the page it
    comes back with."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = saved_rows(
            browser, monkeypatch, 10, 60, read_again, more=290, words=numbered
        )
        page = pressed_until_asked(browser, draft_id, page)
        box = box_named(page, "instruction")
        fields = every_choice(page, "keep", {box: TYPED})
        answer = send(browser, draft_id, fields)
        typed_back = in_its_box(answer.text, box)
        again = send(browser, draft_id, every_choice(answer.text, "keep"))

    assert 150_000 < len(fields) <= MOST_FIELDS
    assert answer.status_code == 200, page_text(answer.text)[:300]
    assert typed_back == TYPED
    assert again.status_code == 200, page_text(again.text)[:300]


def test_a_page_whose_own_press_is_past_the_field_limit_is_not_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six hundred rows of 315 kept rows: a Keep just under the limit is read, and the page it comes
    back with asks more, so its own press is past the limit. That says nothing was saved, every row
    on the record is as it was, and what it typed is gone."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = saved_rows(
            browser, monkeypatch, 10, 60, read_again, more=315, words=numbered
        )
        page = pressed_until_asked(browser, draft_id, page)
        box = box_named(page, "instruction")
        fields = every_choice(page, "keep", {box: TYPED})
        answer = send(browser, draft_id, fields)
        typed_back = in_its_box(answer.text, box)
        next_press = every_choice(answer.text, "keep")
        before = every_row(browser)
        past = send(browser, draft_id, next_press)
        after = every_row(browser)

    assert len(fields) <= MOST_FIELDS
    assert answer.status_code == 200, page_text(answer.text)[:300]
    assert typed_back == TYPED
    assert len(next_press) > MOST_FIELDS
    assert past.status_code == 400
    assert NOT_READ in page_text(past.text)
    assert TYPED not in past.text
    assert before == after


PASTES: Final = [("a", "title"), ("&", "title"), ("a", "due")]
PASTE_IDS: Final = ["plain", "as a percent escape", "plain in a date box"]


def where_it_comes_back(box: str, field: str) -> str:
    """The field a paste comes back in: its box, or for text no date reads as, beside its date box,
    which stays empty."""
    return box.replace("correct:", "refused:", 1) if field == "due" else box


@pytest.mark.parametrize(("typed", "field"), PASTES, ids=PASTE_IDS)
def test_a_paste_with_room_comes_back_whole_and_can_be_pressed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, typed: str, field: str
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = new_rows(browser, monkeypatch, 1, 3, plain, name=True)
        box = box_named(page, field)
        holder = where_it_comes_back(box, field)
        body, value = body_with_a_paste(page_post(page, "keep"), box, typed, MOST_BYTES - ROOM)
        address = f"/parent/imports/{draft_id}/review"
        answer = browser.post(address, content=body, headers=FORM)
        next_press = page_post(answer.text, "keep")
        whole = dict(next_press).get(holder) == value
        again = send(browser, draft_id, next_press)
        whole_again = dict(page_post(again.text, "keep")).get(holder) == value

    assert len(body) == MOST_BYTES - ROOM
    assert answer.status_code == 422
    assert whole
    assert again.status_code == 422
    assert whole_again


@pytest.mark.parametrize(("typed", "field"), PASTES, ids=PASTE_IDS)
def test_a_paste_at_the_byte_limit_comes_back_on_a_page_whose_own_press_is_past_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, typed: str, field: str
) -> None:
    """The first press is read, and its paste comes back whole. Save for later from the page it
    comes back with sends more than that, which is past the limit: that says nothing was saved,
    every row on the record is as it was, and what it typed is gone."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = new_rows(browser, monkeypatch, 1, 3, plain, name=True)
        box = box_named(page, field)
        holder = where_it_comes_back(box, field)
        body, value = body_with_a_paste(page_post(page, "keep"), box, typed, MOST_BYTES)
        address = f"/parent/imports/{draft_id}/review"
        answer = browser.post(address, content=body, headers=FORM)
        fields = page_post(answer.text, "later")
        whole = dict(fields).get(holder) == value
        next_press = as_a_browser_sends(fields)
        before = every_row(browser)
        past = browser.post(address, content=next_press, headers=FORM)
        after = every_row(browser)

    assert len(body) == MOST_BYTES
    assert answer.status_code == 422
    assert whole
    assert len(next_press) > MOST_BYTES
    assert past.status_code == 400
    assert NOT_READ in page_text(past.text)
    assert before == after
