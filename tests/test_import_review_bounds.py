# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""An import review's three presses at every size and shape a page can take, and the bounds its
form is read within.

The form a page makes stays small however long the words in it are: a kept instruction travels
by its row, new words stay in their checkbox with a digest in the signed list, and each distinct
list of options is signed once, packed. The route reads a press within fixed limits of its own, on
fields, on one field's bytes and on all its bytes, and every other route keeps the parser's
defaults. Past a limit a press isn't read: it says nothing was saved, and what it typed is gone.
Sizes here are bytes as sent, as the parser counts them, and there are no time assertions at any
size. The largest pages run by name, in ``tests/full_scale_import_review.py``.
"""

import asyncio
import base64
import hashlib
import html
import json
import pathlib
import re
import sqlite3
import zlib
from collections.abc import Callable, Sequence
from fnmatch import fnmatch
from typing import Any, ClassVar, Final, cast
from urllib.parse import quote_plus, urlencode

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from blossom import screenshot_page, screenshot_posts
from blossom.routes import forms, imports
from blossom.school_instructions import to_wire
from blossom.screenshot_review import field_most
from blossom.screenshot_save import ReviewState
from blossom.stores.drafts import StoreBusy
from tests import full_scale_import_review
from tests.import_review_cases import (
    ACCEPTANCE,
    FORM,
    MOST_BYTES,
    MOST_FIELDS,
    MOST_IN_A_FIELD,
    TYPED,
    Fields,
    Made,
    Presses,
    add_kept,
    all_found_onto_one_row,
    as_a_browser_sends,
    body_with_a_paste,
    box_named,
    every_box_at_its_limit,
    every_choice,
    every_press_answered,
    every_row,
    fields_of,
    followed,
    has_review_form,
    her_name_answered,
    here,
    images,
    in_its_box,
    in_writes,
    instructions_answered,
    kept_words_on_record,
    largest_a_page_makes,
    new_rows,
    on_record,
    open_settings,
    options_of,
    padded_to,
    page_post,
    page_text,
    plain,
    press_over_socket,
    press_through,
    read_again_unsure,
    read_review,
    send,
    serving,
    signed_again,
    sizes_as_sent,
    the_worst_page,
    units_on,
    unpacked,
    with_a_kept_question,
    worded_options,
    written_as_the_older_form,
)
from tests.support import homework_answer, homework_card, state_of

PARSERS_FIELDS: Final = 1_000
PARSERS_FIELD_BYTES: Final = 1024 * 1024
NOT_READ: Final = "That form could not be read, so nothing was saved."
NOTHING_KEPT: Final = "Nothing was kept or saved."
NOT_CARRIED: Final = (
    "Some of your choices can't be shown on this page. Check the review before trying again."
)
NOT_SAVED_YET: Final = "Not saved yet."
A_LONG_PASTE: Final = 30 * 1024 * 1024
"""Far past the parser's 1 MiB, at much less than a paste near the byte limit costs."""
THE_PRESSES: Final = ("keep", "later", "save")
ANSWERS: Final = {"keep": 200, "later": 303, "save": 200}
DEFAULT_SHAPES: Final = (
    "90 plain",
    "140 of one name",
    "one card, 2 kept of 40,000",
    "one card, 20 kept of 4,000 CJK",
    "one card, 300 kept of about 3,900 plain",
    "one card, 1 kept of 200,000 CJK",
    "one card, 1 kept of 2,000,000 plain",
    "one card, 200 kept of 4,000 random CJK",
)
"""The shapes of the acceptance list this module presses; the larger ones run by name."""
WHOLE_LIST: Final = (
    "90 plain",
    "300 plain",
    "600 plain",
    "460 every question",
    "600 every question",
    "140 of one name",
    "600 in groups of 20",
    "300 in groups of 60",
    "600 of one name",
    "450 on saved rows",
    "600 on saved rows",
    "450 every question on saved rows",
    "600 every question on saved rows",
    "one card, 2 kept of 40,000",
    "one card, 20 kept of 4,000 CJK",
    "one card, 300 kept of about 3,900 plain",
    "one card, 1 kept of 200,000 CJK",
    "one card, 1 kept of 2,000,000 plain",
    "one card, 200 kept of 4,000 random CJK",
    "one card, 18,100 kept of a few characters",
    "one card, 199,900 kept of a few characters",
    "600 saved rows, each with 1 kept of 4,000 random CJK",
)
"""The acceptance list, written out, so a shape can't leave it unseen."""
AWKWARD: Final = (
    'A "quote" and a \\ back',
    "Line one\nline two\r\nline three",
    "\U0001f600 beyond the basic plane",
    "plus + percent % amp & equals = hash #",
    "<b>markup</b> &amp; an entity",
)
"""Words whose every character has to come back as it was."""


@pytest.fixture(scope="module")
def pressed(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], Presses]:
    """A shape's review pressed through, once however many tests read it."""
    done: dict[str, Presses] = {}

    def of(shape: str) -> Presses:
        if shape not in done:
            folder = tmp_path_factory.mktemp("shape")
            with (
                pytest.MonkeyPatch.context() as monkeypatch,
                here(open_settings(folder)) as browser,
            ):
                done[shape] = press_through(browser, shape, monkeypatch)
        return done[shape]

    return of


@pytest.fixture
def first_reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, int]]:
    """The limits each request's first read of its form was made with."""
    seen: list[tuple[float, int]] = []
    real = Request._get_form

    async def noted(
        self: Request,
        *,
        max_files: float = 1000,
        max_fields: float = 1000,
        max_part_size: int = 1024 * 1024,
    ) -> Any:  # noqa: ANN401  (Starlette's own form, passed on as it is)
        if self._form is None:
            seen.append((max_fields, max_part_size))
        return await real(
            self, max_files=max_files, max_fields=max_fields, max_part_size=max_part_size
        )

    monkeypatch.setattr(Request, "_get_form", noted)
    return seen


def a_small_page(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> Made:
    draft_id, page = new_rows(browser, monkeypatch, 1, 3, plain, name=True)
    return draft_id, page


def padded_fields(fields: Fields, total: int) -> list[tuple[str, str]]:
    """This post with fields nothing reads, up to this many fields."""
    assert total >= len(fields)
    return [*fields, *[("read:zz", "1")] * (total - len(fields))]


def is_not_read(answer: Any) -> bool:  # noqa: ANN401  (a response of either client)
    return answer.status_code == 400 and NOT_READ in page_text(answer.text)


def as_a_field_of(name: str, size: int) -> str:
    """A value that makes this name's field this many bytes as sent."""
    as_sent = len(quote_plus(name, safe="*-._"))
    return "a" * (size - as_sent)


# ------------------------------------------------------------------ every press at each size


@pytest.mark.parametrize("shape", DEFAULT_SHAPES)
def test_every_press_at_each_size(pressed: Callable[[str], Presses], shape: str) -> None:
    """Keep, Keep from its page, Later, Keep from Later's page, Save, and the presses from Save's
    page. The typing stays in its box on every page that has it, and Later's progress holds it."""
    every_press_answered(pressed(shape), shape)


@pytest.mark.parametrize("shape", DEFAULT_SHAPES)
def test_a_field_a_page_makes_stays_within_1_mib(
    pressed: Callable[[str], Presses], shape: str
) -> None:
    for press in pressed(shape).pressed:
        assert press.largest[0] <= 1024 * 1024, press.label


def test_the_acceptance_list_is_whole() -> None:
    """The module that runs by name holds the rest of the list, and pressing is what it does."""
    assert ACCEPTANCE == WHOLE_LIST
    assert sorted((*DEFAULT_SHAPES, *full_scale_import_review.FULL_SIZE_SHAPES)) == sorted(
        WHOLE_LIST
    )
    marks = getattr(full_scale_import_review.test_every_press_at_each_size, "pytestmark", [])
    assert [mark.args for mark in marks] == [
        ("shape", full_scale_import_review.FULL_SIZE_SHAPES),
    ]
    assert {name for name in vars(full_scale_import_review) if name.startswith("test_")} == {
        "test_every_press_at_each_size",
        "test_the_worst_page_at_full_scale",
        "test_every_box_at_its_limit",
        "test_the_longest_name_a_tick_carries_with_every_box_at_its_limit",
        "test_a_large_page_made_before_the_compact_list",
        "test_a_page_of_199900_kept_rows_made_before_the_compact_list",
        "test_the_42_mb_page_made_before_the_compact_list_saves_its_groups",
        "test_fields_at_the_limit_are_read_and_one_past_it_are_not",
        "test_a_conflict_on_199900_kept_rows_comes_back_as_posted",
        "test_600_rows_of_290_kept_rows",
        "test_a_page_whose_own_press_is_past_the_field_limit_is_not_read",
        "test_ticks_under_the_longest_name_up_to_137_mb",
        "test_a_paste_with_room_comes_back_whole_and_can_be_pressed",
        "test_a_paste_at_the_byte_limit_comes_back_on_a_page_whose_own_press_is_past_it",
    }


def test_the_module_that_runs_by_name_isnt_collected() -> None:
    """Its file's name is what keeps it out of the suite and the gate's shards. It runs by name,
    and that a run happened on a given head shows only in that run's log."""
    file = pathlib.Path(full_scale_import_review.__file__).name
    assert not fnmatch(file, "test_*.py")
    assert not fnmatch(file, "*_test.py")


# ------------------------------------------------------------------ the fields a page can make


def test_the_worst_page_counts_each_kind_of_field(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record of what a card carries, which holds no limit. Four cards on saved rows with every
    question and box, each date typed as text no date reads as, ticking everything: every kind of
    field a page makes, so a new one breaks this. A card sends 28, and the rest are the six signed
    fields, the check, the press and one name question."""
    with here(open_settings(tmp_path)) as browser:
        _, fields = the_worst_page(browser, monkeypatch, 1, 4)

    kinds: dict[str, int] = {}
    for name, _ in fields:
        head = name.split(":")[0]
        kind = ":".join(name.split(":")[:2]) if head == "answer" else head
        kinds[kind] = kinds.get(kind, 0) + 1
    assert kinds == {
        "version": 1,
        "progress_revision": 1,
        "acceptance": 1,
        "asked": 1,
        "defaults": 1,
        "identity": 1,
        "check": 1,
        "press": 1,
        "answer:name": 1,
        "digest": 4,
        "correct": 20,
        "read": 20,
        "refused": 8,
        "none": 4,
        "answer:confirm": 24,
        "answer:destination": 4,
        "answer:kind": 8,
        "answer:due": 4,
        "answer:assigned": 4,
        "answer:instructions": 12,
    }
    assert len(fields) == 28 * 4 + 9


def test_the_limits_are_these_numbers() -> None:
    assert imports.REVIEW_FORM_FIELDS == MOST_FIELDS == 200_000
    assert imports.REVIEW_FORM_BYTES == MOST_BYTES == 139_952_128
    assert imports.REVIEW_FIELD_BYTES == MOST_IN_A_FIELD == MOST_BYTES


# ------------------------------------------------------------------ kept instructions by their rows


def test_a_kept_instruction_travels_by_its_row(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each kept option's value is its row, its label is that row's words, none of those words is in
    any value, and what's stored is those words exactly."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(
            browser, monkeypatch, more=len(AWKWARD), words=lambda _, k: AWKWARD[k]
        )
        rows = kept_words_on_record(browser)
        offered = worded_options(page, question)
        ticks = [value for value, _ in offered if value.startswith("k")]
        fields = ticks_of(page_post(page, "keep"), question, ticks)
        kept = send(browser, draft_id, fields)
        stored = instructions_answered(browser, draft_id)

    body = as_a_browser_sends(fields)
    assert len(ticks) == len(AWKWARD) + 1
    for value, words in offered:
        if value.startswith("k"):
            assert re.fullmatch(r"k[1-9][0-9]*", value)
            assert rows[int(value[1:])] == words
    assert not {to_wire(words) for words in rows.values()} & {value for value, _ in offered}
    assert not any(quote_plus(to_wire(words)).encode() in body for words in AWKWARD)
    assert not any(quote_plus(words).encode() in body for words in AWKWARD)
    assert kept.status_code == 200
    assert stored == [sorted(rows[int(tick[1:])] for tick in ticks)]


@pytest.mark.parametrize("standing", ["current", "history", "awaiting"])
def test_a_kept_row_in_each_state_is_resolved(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, standing: str
) -> None:
    """Ticked alone, it's stored in its words whichever way it stands."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, _, _ = with_a_kept_question(browser, monkeypatch, more=1)
        fresh = sqlite3.connect(state_of(browser).settings.database_path)
        try:
            fresh.execute(
                "UPDATE school_instructions SET state = ? WHERE text = 'Kept words 0.'",
                (standing,),
            )
            fresh.commit()
        finally:
            fresh.close()
        page = browser.get(f"/parent/imports/{draft_id}").text
        [question] = fields_of(page, "answer:instructions:")
        [tick] = [v for v, words in worded_options(page, question) if words == "Kept words 0."]
        answer = send(browser, draft_id, ticks_of(page_post(page, "keep"), question, [tick]))
        stored = instructions_answered(browser, draft_id)

    assert answer.status_code == 200
    assert stored == [["Kept words 0."]]
    assert NOT_CARRIED not in page_text(answer.text)


def test_each_kept_option_names_a_row_of_its_own_question(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four cards with four kept rows each: each option's row is one of its question's assignment,
    shown in that row's words."""
    with here(open_settings(tmp_path)) as browser:
        _, page, _ = with_a_kept_question(
            browser, monkeypatch, cards=4, more=3, words=lambda _, k: f"Kept words {k}."
        )
        fresh = sqlite3.connect(state_of(browser).settings.database_path)
        try:
            rows = {
                f"k{row}": (assignment, words)
                for row, assignment, words in fresh.execute(
                    "SELECT sequence, assignment_id, text FROM school_instructions"
                )
            }
        finally:
            fresh.close()

    seen = 0
    for question in fields_of(page, "answer:instructions:"):
        assignment = json.loads(question.removeprefix("answer:instructions:"))[0][1]
        for value, words in worded_options(page, question):
            if value.startswith("k"):
                seen += 1
                assert rows[value] == (assignment, words)
    assert seen == 16


@pytest.mark.parametrize("press", ["keep", "later"])
def test_a_kept_row_that_moved_the_revision_after_the_page_leaves_its_ticks_as_shown(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, press: str
) -> None:
    """The rows it ticked are stored in their words under the question it was shown, and the review
    asks again about the instructions as they stand now."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(browser, monkeypatch)
        rows = [value for value, _ in options_of(page, question) if value.startswith("k")]
        words = kept_words_on_record(browser)
        posted = ticks_of(page_post(page, press), question, rows)
        add_kept(browser, 1, lambda _, __: "Kept by another tab.")
        fresh = sqlite3.connect(state_of(browser).settings.database_path)
        try:
            fresh.execute(
                "UPDATE school_instructions SET revision = revision + 1 WHERE text = ?",
                ("Kept by another tab.",),
            )
            fresh.commit()
        finally:
            fresh.close()
        answer = send(browser, draft_id, posted)
        stored = instructions_answered(browser, draft_id)
        now = followed(browser, answer)

    assert answer.status_code == ANSWERS[press]
    assert stored == [sorted(words[int(row[1:])] for row in rows)]
    assert fields_of(now, "answer:instructions:") != [question]
    assert "Kept by another tab." in {
        shown
        for name in fields_of(now, "answer:instructions:")
        for _, shown in worded_options(now, name)
    }


def test_a_new_word_tampered_with_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New words travel in the post, against their digest in the signed list: other words aren't
    what the page offered, and neither are a kept row's words written out."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(browser, monkeypatch)
        before = every_row(browser)
        fields = [field for field in page_post(page, "keep") if field[0] != question]
        new = next(value for value, _ in worded_options(page, question) if value.startswith('"'))
        kept_words = next(
            words for value, words in worded_options(page, question) if value.startswith("k")
        )
        tampered = [
            send(browser, draft_id, [*fields, (question, other)])
            for other in (new[:-1] + 'x"', to_wire(kept_words), '"', "k")
        ]
        after = every_row(browser)

    assert [answer.status_code for answer in tampered] == [303, 303, 303, 303]
    assert before == after


def test_a_new_word_s_digest_is_its_sha_256() -> None:
    """A digest shorter or weaker than this lets other words pass for the ones a page offered."""
    field = 'answer:instructions:[["id","a"],["b"],1]'
    wire = to_wire("New words \u65e5")
    assert (
        screenshot_page.signed_option(field, wire) == hashlib.sha256(wire.encode()).hexdigest()[:32]
    )
    assert screenshot_page.signed_option(field, "k7") == "k7"
    assert screenshot_page.signed_option(field, "") == ""
    assert screenshot_page.signed_option("answer:due:x", wire) == wire


# ------------------------------------------------------------------ the limits


def test_a_post_at_the_field_limit_is_read_and_one_past_it_is_not(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real signed page's post, padded. At the limit the parser reads it. One past it says nothing
    was saved, every row on the record is as it was, and what it typed is gone."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = a_small_page(browser, monkeypatch)
        box = box_named(page, "title")
        fields = page_post(page, "keep", typed={box: TYPED})
        at = send(browser, draft_id, padded_fields(fields, MOST_FIELDS))
        before = every_row(browser)
        past = send(browser, draft_id, padded_fields(fields, MOST_FIELDS + 1))
        after = every_row(browser)

    assert at.status_code == 200
    assert in_its_box(at.text, box) == TYPED
    assert is_not_read(past)
    assert TYPED not in past.text
    assert before == after


@pytest.mark.parametrize(
    "size",
    [PARSERS_FIELD_BYTES, PARSERS_FIELD_BYTES + 1, A_LONG_PASTE],
    ids=["1 MiB", "and 1", "30 MiB"],
)
def test_a_paste_comes_back_whole_in_its_box(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    """A box of these bytes as sent, past the parser's default, is too long for its field and comes
    back whole in its box."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = a_small_page(browser, monkeypatch)
        box = box_named(page, "title")
        value = as_a_field_of(box, size)
        fields = page_post(page, "keep", typed={box: value})
        before = every_row(browser)
        answer = send(browser, draft_id, fields)
        after = every_row(browser)

    assert dict(sizes_as_sent(as_a_browser_sends(fields)))[box] == size
    assert answer.status_code == 422
    assert in_its_box(answer.text, box) == value
    assert before == after


def test_a_field_past_the_byte_limit_is_not_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = a_small_page(browser, monkeypatch)
        box = box_named(page, "title")
        fields = page_post(page, "keep", typed={box: as_a_field_of(box, MOST_IN_A_FIELD + 1)})
        before = every_row(browser)
        answer = send(browser, draft_id, fields)
        after = every_row(browser)

    assert dict(sizes_as_sent(as_a_browser_sends(fields)))[box] == MOST_IN_A_FIELD + 1
    assert is_not_read(answer)
    assert before == after


def test_a_body_at_the_byte_limit_is_read_and_one_past_it_is_not(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real signed page's post, padded, read at the limit and not one byte past it."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = a_small_page(browser, monkeypatch)
        box = box_named(page, "title")
        body = as_a_browser_sends(page_post(page, "keep", typed={box: TYPED}))
        address = f"/parent/imports/{draft_id}/review"
        at_limit = b"".join(padded_to(body, MOST_BYTES))
        at = browser.post(address, content=at_limit, headers=FORM)
        before = every_row(browser)
        past = browser.post(address, content=at_limit + b"a", headers=FORM)
        after = every_row(browser)

    assert len(at_limit) == MOST_BYTES
    assert at.status_code == 200
    assert in_its_box(at.text, box) == TYPED
    assert is_not_read(past)
    assert TYPED not in past.text
    assert before == after


def test_reading_stops_at_the_byte_limit() -> None:
    """A body that goes on coming is read no further than the chunk that passes the limit."""
    reads = 0

    async def receive() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"a=" + b"b" * 1022 + b"&", "more_body": True}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-type", FORM["content-type"].encode())],
        "query_string": b"",
        "app": object(),
    }

    async def read() -> None:
        request = forms.WithinBytes(Request(scope, receive), 10 * 1024)
        await forms.form_of(request, most_fields=10**6, most_field_bytes=10**6)

    with pytest.raises(forms.FormUnreadable):
        asyncio.run(read())
    assert reads == 10


def test_the_largest_body_within_the_limits_is_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every box at its limit in characters of four bytes, every date carrying its text and every
    new instruction ticked, within what the limit leaves a card."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, fields = every_box_at_its_limit(browser, monkeypatch, 1, 6)
        answer = send(browser, draft_id, fields)

    body = as_a_browser_sends(fields)
    assert answer.status_code == 200
    assert 100 * 1024 < len(body) / 6 <= 180 * 1024
    assert NOT_READ not in page_text(answer.text)


# ------------------------------------------------------------------ over a real socket


@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
def test_a_body_at_the_byte_limit_and_one_past_it_over_a_socket(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, declared: bool
) -> None:
    """Delivered in many chunks, as a server delivers a body, counted across them all."""
    with here(open_settings(tmp_path)) as browser, serving(browser) as port:
        draft_id, page = a_small_page(browser, monkeypatch)
        box = box_named(page, "title")
        body = as_a_browser_sends(page_post(page, "keep", typed={box: TYPED}))
        at = press_over_socket(
            port,
            draft_id,
            padded_to(body, MOST_BYTES),
            length=MOST_BYTES if declared else None,
        )
        before = every_row(browser)
        past = press_over_socket(
            port,
            draft_id,
            padded_to(body, MOST_BYTES + 1),
            length=MOST_BYTES + 1 if declared else None,
        )
        after = every_row(browser)

    assert at.status == 200
    assert in_its_box(at.text, box) == TYPED
    assert past.status == 400
    assert NOT_READ in page_text(past.text)
    assert before == after


def test_a_post_at_the_field_limit_and_one_past_it_over_a_socket(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser, serving(browser) as port:
        draft_id, page = a_small_page(browser, monkeypatch)
        box = box_named(page, "title")
        fields = page_post(page, "keep", typed={box: TYPED})
        at = as_a_browser_sends(padded_fields(fields, MOST_FIELDS))
        past = as_a_browser_sends(padded_fields(fields, MOST_FIELDS + 1))
        answered = press_over_socket(port, draft_id, in_writes(at, 256), length=len(at))
        before = every_row(browser)
        refused = press_over_socket(port, draft_id, in_writes(past, 256), length=len(past))
        after = every_row(browser)

    assert answered.status == 200
    assert in_its_box(answered.text, box) == TYPED
    assert refused.status == 400
    assert NOT_READ in page_text(refused.text)
    assert before == after


def test_a_field_past_the_byte_limit_over_a_socket(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser, serving(browser) as port:
        draft_id, page = a_small_page(browser, monkeypatch)
        box = box_named(page, "title")
        body, _ = body_with_a_paste(page_post(page, "keep"), box, "a", MOST_BYTES + 1)
        before = every_row(browser)
        refused = press_over_socket(port, draft_id, in_writes(body, 64 * 1024), length=len(body))
        after = every_row(browser)

    assert len(body) == MOST_BYTES + 1
    assert refused.status == 400
    assert NOT_READ in page_text(refused.text)
    assert before == after


@pytest.mark.parametrize(
    ("size", "declared"), [(1, True), (7, False), (1025, True)], ids=["1", "7 chunked", "1025"]
)
def test_small_writes_keep_the_typing_exact(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, size: int, declared: bool
) -> None:
    """Every escape in the typing can be cut in two between writes."""
    words = 'Words \u65e5\u672c\u8a9e + % & = \U0001f600 "quoted"'
    with here(open_settings(tmp_path)) as browser, serving(browser) as port:
        draft_id, page = a_small_page(browser, monkeypatch)
        box = box_named(page, "title")
        body = as_a_browser_sends(page_post(page, "keep", typed={box: words}))
        answer = press_over_socket(
            port, draft_id, in_writes(body, size), length=len(body) if declared else None
        )

    assert answer.status == 200
    assert in_its_box(answer.text, box) == words


# ------------------------------------------------------------------ the longest name a tick carries


def test_the_longest_name_a_tick_carries(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sixty cards of one name on one saved row: one instruction question names every card, and
    each of its ticks is sent under that name."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = all_found_onto_one_row(browser, monkeypatch, 1, 60)
        box = box_named(page, "title")
        fields = every_choice(page, "keep", {box: TYPED})
        answer = send(browser, draft_id, fields)
        stored = instructions_answered(browser, draft_id)

    ticks = [value for name, value in fields if name == question]
    assert len(ticks) == 61
    assert len(quote_plus(question, safe="*-._")) == 4_471
    assert answer.status_code == 200
    assert in_its_box(answer.text, box) == TYPED
    assert any(len(words) == 61 for words in stored)


# ------------------------------------------------------------------ other routes keep their limits


@pytest.mark.parametrize(
    "action", ["read", "find", "set-aside", "finish", "discard", "not-hers", "family"]
)
def test_another_route_keeps_its_limits(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """Status alone says nothing here: some of these answer 400 to any form they don't expect."""
    fields_at = urlencode([(f"f{n}", "1") for n in range(PARSERS_FIELDS)]).encode()
    one_more = fields_at + b"&g=1"
    field_at = b"f=" + b"a" * (PARSERS_FIELD_BYTES - 1)
    with here(open_settings(tmp_path)) as browser:
        draft_id, _ = a_small_page(browser, monkeypatch)
        address = (
            "/parent/actions/plan" if action == "family" else f"/parent/imports/{draft_id}/{action}"
        )
        before = every_row(browser)
        at_limit = [
            browser.post(address, content=body, headers=FORM) for body in (fields_at, field_at)
        ]
        past_limit = [
            browser.post(address, content=body, headers=FORM)
            for body in (one_more, field_at + b"a")
        ]
        after = every_row(browser)

    assert [NOT_READ in page_text(answer.text) for answer in at_limit] == [False, False]
    assert [is_not_read(answer) for answer in past_limit] == [True, True]
    assert before == after


# ------------------------------------------------------------------ every way into a press


@pytest.mark.parametrize(
    ("way", "press", "status"),
    [
        ("closed", "keep", 200),
        ("closed", "save", 200),
        ("busy", "keep", 503),
        ("busy", "later", 503),
        ("busy", "save", 503),
        ("refusing", "keep", 503),
        ("refusing", "later", 503),
        ("refusing", "save", 503),
        ("absent", "keep", 404),
        ("absent", "later", 404),
        ("absent", "save", 404),
    ],
)
def test_every_way_in_reads_within_the_bounds(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    first_reads: list[tuple[float, int]],
    way: str,
    press: str,
    status: int,
) -> None:
    """Past the parser's default at the door, whichever way the press goes after it: a closed
    review, a busy or refusing file, and a review that isn't there."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = a_small_page(browser, monkeypatch)
        box = box_named(page, "title")
        fields = padded_fields(
            page_post(page, press, typed={box: TYPED}, tick=units_on(page)), PARSERS_FIELDS + 1
        )
        drafts = state_of(browser).import_drafts
        target = draft_id

        def busy(*_: object, **__: object) -> None:
            raise StoreBusy

        def refusing(*_: object, **__: object) -> None:
            raise sqlite3.OperationalError

        if way == "closed":
            drafts._connection.execute(
                "UPDATE import_drafts SET closed_at = ? WHERE draft_id = ?",
                ("2026-10-05T12:00:00+00:00", draft_id),
            )
            drafts._connection.commit()
        elif way == "busy":
            monkeypatch.setattr(drafts, "snapshot", busy)
        elif way == "refusing":
            monkeypatch.setattr(drafts, "snapshot", refusing)
        elif way == "absent":
            target = "a" * 32
        first_reads.clear()
        answer = send(browser, target, fields)

    assert answer.status_code == status, page_text(answer.text)[:300]
    assert NOT_READ not in page_text(answer.text)
    assert first_reads == [(MOST_FIELDS, MOST_IN_A_FIELD)]
    if (way, press) != ("closed", "keep"):
        assert TYPED in html.unescape(answer.text)


def test_a_press_too_long_for_its_box_past_a_thousand_fields(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, first_reads: list[tuple[float, int]]
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = a_small_page(browser, monkeypatch)
        box = box_named(page, "title")
        words = "x" * (field_most("title") + 1)
        fields = padded_fields(page_post(page, "keep", typed={box: words}), PARSERS_FIELDS + 1)
        first_reads.clear()
        answer = send(browser, draft_id, fields)

    assert answer.status_code == 422
    assert in_its_box(answer.text, box) == words
    assert first_reads == [(MOST_FIELDS, MOST_IN_A_FIELD)]


def test_a_page_out_of_date_past_a_thousand_fields(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, first_reads: list[tuple[float, int]]
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = a_small_page(browser, monkeypatch)
        box = box_named(page, "title")
        first = send(browser, draft_id, every_choice(page, "keep"))
        fields = padded_fields(page_post(page, "keep", typed={box: TYPED}), PARSERS_FIELDS + 1)
        first_reads.clear()
        answer = send(browser, draft_id, fields)

    assert first.status_code == 200
    assert answer.status_code == 409
    assert in_its_box(answer.text, box) == TYPED
    assert first_reads == [(MOST_FIELDS, MOST_IN_A_FIELD)]


def test_a_save_resent_past_a_thousand_fields_writes_nothing_more(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, first_reads: list[tuple[float, int]]
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = a_small_page(browser, monkeypatch)
        fields = padded_fields(page_post(page, "save", tick=units_on(page)[:1]), PARSERS_FIELDS + 1)
        first = send(browser, draft_id, fields)
        before = every_row(browser)
        first_reads.clear()
        again = send(browser, draft_id, fields)
        after = every_row(browser)

    assert first.status_code == 200
    assert again.status_code == 200
    assert "Saved: 1 new assignment." in page_text(again.text)
    assert before == after
    assert first_reads == [(MOST_FIELDS, MOST_IN_A_FIELD)]


def test_a_review_id_that_isnt_one_answers_404_with_no_body_read(tmp_path: pathlib.Path) -> None:
    reads = 0
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"a=1", "more_body": False}

    async def send_back(message: dict[str, Any]) -> None:
        sent.append(message)

    with here(open_settings(tmp_path)) as browser:
        address = "/parent/imports/not-an-id/review"
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": address,
            "raw_path": address.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"127.0.0.1:8781"),
                (b"origin", b"http://127.0.0.1:8781"),
                (b"content-type", FORM["content-type"].encode()),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8781),
        }
        asyncio.run(browser.app(scope, receive, send_back))  # type: ignore[arg-type]

    assert next(m["status"] for m in sent if m["type"] == "http.response.start") == 404
    assert reads == 0


# ------------------------------------------------------------------ a paste


def test_a_long_paste_in_an_instructions_box_comes_back_whole(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paste past the parser's default in an Instructions box, with nothing on the record
    touched: the press is refused for its length, with the paste in the box."""
    paste = "\u65e5" * 200_000
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, _ = with_a_kept_question(browser, monkeypatch)
        box = box_named(page, "instruction")
        fields = every_choice(page, "keep", {box: paste})
        before = every_row(browser)
        answer = send(browser, draft_id, fields)
        after = every_row(browser)

    assert dict(sizes_as_sent(as_a_browser_sends(fields)))[box] > PARSERS_FIELD_BYTES
    assert answer.status_code == 422
    assert in_its_box(answer.text, box) == paste
    assert before == after


def test_a_paste_never_becomes_an_option(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paste = "\u65e5" * 200_000
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(browser, monkeypatch)
        box = box_named(page, "instruction")
        answer = send(browser, draft_id, every_choice(page, "keep", {box: paste}))

    assert answer.status_code == 422
    assert to_wire(paste)[:40] not in html.unescape(answer.text)
    assert paste not in {words for _, words in worded_options(answer.text, question)}


# ------------------------------------------------------------------ the signed list


def random_words(seed: int, count: int) -> str:
    """Words zlib can't shorten: each character from the digest of its place."""
    digest = hashlib.sha256()
    made = []
    for n in range(count):
        digest.update(f"{seed}-{n}".encode())
        made.append(chr(0x4E00 + digest.digest()[0] * 80 + digest.digest()[1] % 80))
    return "".join(made)


def test_the_signed_list_does_not_grow_with_words() -> None:
    """New words are a digest in the list and a kept row is its row, so an option costs the list
    the same however long its words are."""
    field = 'answer:instructions:[["id","a"],["b"],1]'

    def list_with(words: str) -> str:
        return screenshot_page.asks_text([(field, ["k1", "k2", to_wire(words), ""])])

    short, long = list_with("Short words."), list_with(random_words(1, 4_000))
    assert abs(len(long) - len(short)) <= 16
    assert len(long) < 256


def test_a_kept_word_is_nowhere_in_the_signed_list(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long = random_words(2, 40_000)
    with here(open_settings(tmp_path)) as browser:
        _, page, question = with_a_kept_question(
            browser, monkeypatch, more=2, words=lambda _, k: f"{k} {long}"
        )
        asked = dict(page_post(page, "keep"))["asked"]

    assert len(asked) < 1024
    assert sum(1 for value, _ in options_of(page, question) if value.startswith("k")) == 3


@pytest.mark.parametrize("count", [1, 2, 3, 10, 200, 201, 202])
def test_a_list_is_sent_as_it_is(count: int) -> None:
    """A browser sends it unchanged, whatever its length: nothing in it is encoded, and it has no
    padding."""
    asks = [(f"answer:kind:{n}", [random_words(n, 5)]) for n in range(count)]
    asked = screenshot_page.asks_text(asks)
    assert asked.startswith("z1.")
    assert quote_plus(asked, safe="*-._") == asked
    assert "=" not in asked


def test_a_list_is_packed(pressed: Callable[[str], Presses]) -> None:
    """Dedupe alone leaves this page's list at 142,643 bytes as sent."""
    largest = max(press.largest for press in pressed("140 of one name").pressed)
    assert largest[1] == "asked"
    assert largest[0] < 32 * 1024


def test_each_distinct_list_once() -> None:
    """Six hundred names of one 40 KiB list: zlib sees only 32 KiB back, so writing the list out
    each time would hold it all."""
    long = [f"k{hashlib.sha256(str(n).encode()).hexdigest()[:24]}" for n in range(1_500)]
    asks = [(f"answer:destination:{n}", long) for n in range(600)]
    asked = screenshot_page.asks_text(asks)
    found = unpacked(asked)
    assert len(found["lists"]) == 1
    assert {index for _, index in found["asks"]} == {0}
    assert len(asked) < 80 * 1024
    assert screenshot_page.asks_read(asked)[0]["answer:destination:7"] == frozenset(long)


# ------------------------------------------------------------------ a row that can't be resolved


def ticks_of(fields: Fields, question: str, ticks: Sequence[str]) -> list[tuple[str, str]]:
    """This post with these ticks on this question, and nothing else ticked on it."""
    return [*(field for field in fields if field[0] != question), *((question, t) for t in ticks)]


def delete_row(browser: TestClient, row: str) -> None:
    fresh = sqlite3.connect(state_of(browser).settings.database_path)
    try:
        fresh.execute("DELETE FROM school_instructions WHERE sequence = ?", (int(row[1:]),))
        fresh.commit()
    finally:
        fresh.close()


@pytest.mark.parametrize("press", THE_PRESSES)
def test_a_row_that_cant_be_resolved_is_said(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, press: str
) -> None:
    """A kept row gone before the press, a defense since no code deletes one: the press is refused
    whole, the sentence said once, every answer as it was, and the typing back in its box."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(browser, monkeypatch)
        box = box_named(page, "instruction")
        rows = [value for value, _ in options_of(page, question) if value.startswith("k")]
        posted = page_post(page, press, typed={box: TYPED}, tick=units_on(page))
        delete_row(browser, rows[0])
        before = every_row(browser)
        answer = send(browser, draft_id, ticks_of(posted, question, rows))
        after = every_row(browser)

    said = page_text(answer.text)
    assert answer.status_code == 422, said[:300]
    assert NOTHING_KEPT in said
    assert said.count(NOT_CARRIED) == 1
    assert before == after
    assert in_its_box(answer.text, box) == TYPED


@pytest.mark.parametrize("press", THE_PRESSES)
def test_a_row_that_cant_be_resolved_brings_back_the_choices_that_can_show(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, press: str
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(browser, monkeypatch)
        rows = [value for value, _ in options_of(page, question) if value.startswith("k")]
        posted = page_post(page, press, tick=units_on(page))
        delete_row(browser, rows[0])
        answer = send(browser, draft_id, ticks_of(posted, question, rows))

    shown = dict(options_of(answer.text, question))
    assert answer.status_code == 422
    assert rows[0] not in shown
    assert all(shown[row] for row in rows[1:])
    assert not shown[""]
    assert page_text(answer.text).count(NOT_CARRIED) == 1


def test_a_row_that_cant_be_resolved_on_a_page_out_of_date_is_said(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its choices don't carry onto a page of another version, and that's said with the rest."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(browser, monkeypatch)
        rows = [value for value, _ in options_of(page, question) if value.startswith("k")]
        posted = ticks_of(page_post(page, "keep"), question, rows)
        assert send(browser, draft_id, ticks_of(posted, question, rows[1:])).status_code == 200
        delete_row(browser, rows[0])
        before = every_row(browser)
        answer = send(browser, draft_id, posted)
        after = every_row(browser)

    said = page_text(answer.text)
    assert answer.status_code == 422
    assert imports.OUT_OF_DATE in said
    assert said.count(NOT_CARRIED) == 1
    assert before == after


def test_a_row_that_cant_be_resolved_with_no_rows_left_to_show_is_said(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_rows(*_: object, **__: object) -> None:
        return None

    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(browser, monkeypatch)
        rows = [value for value, _ in options_of(page, question) if value.startswith("k")]
        posted = ticks_of(page_post(page, "keep"), question, rows)
        delete_row(browser, rows[0])
        monkeypatch.setattr(imports, "rows_of", no_rows)
        answer = send(browser, draft_id, posted)

    assert answer.status_code == 422
    assert page_text(answer.text).count(NOT_CARRIED) == 1


def test_a_refused_row_keeps_the_revision_it_was_posted_with(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Save for later in another tab moves the revision with no change of version. A page signed
    with the newer one would let its next Save for later overwrite that tab's progress unseen."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(browser, monkeypatch)
        rows = [value for value, _ in options_of(page, question) if value.startswith("k")]
        posted = ticks_of(page_post(page, "keep"), question, rows)
        other_tab = page_post(page, "later")
        assert send(browser, draft_id, other_tab).status_code == 303
        now = browser.get(f"/parent/imports/{draft_id}").text
        delete_row(browser, rows[0])
        answer = send(browser, draft_id, posted)

    signed = dict(posted)["progress_revision"]
    assert dict(page_post(now, "keep"))["progress_revision"] != signed
    assert answer.status_code == 422
    assert dict(page_post(answer.text, "keep"))["progress_revision"] == signed


@pytest.mark.parametrize("press", THE_PRESSES)
def test_a_row_that_resolves_says_nothing_of_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, press: str
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(browser, monkeypatch)
        rows = [value for value, _ in options_of(page, question) if value.startswith("k")]
        words = kept_words_on_record(browser)
        posted = page_post(page, press, tick=units_on(page))
        answer = send(browser, draft_id, ticks_of(posted, question, rows))
        said = page_text(followed(browser, answer))
        stored = instructions_answered(browser, draft_id)

    assert answer.status_code == ANSWERS[press]
    assert NOT_CARRIED not in said
    assert stored == [sorted(words[int(row[1:])] for row in rows)]


@pytest.mark.parametrize("press", THE_PRESSES)
def test_a_press_with_one_row_gone_keeps_nothing_of_the_rest(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, press: str
) -> None:
    """Two questions on the page, one naming a row that's gone: the other's answer isn't kept
    either."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, _ = with_a_kept_question(browser, monkeypatch, cards=2)
        first, second = fields_of(page, "answer:instructions:")
        gone = next(value for value, _ in options_of(page, first) if value.startswith("k"))
        stays = next(value for value, _ in options_of(page, second) if value.startswith("k"))
        posted = page_post(page, press, tick=units_on(page))
        fields = ticks_of(ticks_of(posted, first, [gone]), second, [stays])
        delete_row(browser, gone)
        before = every_row(browser)
        answer = send(browser, draft_id, fields)
        after = every_row(browser)

    assert answer.status_code == 422
    assert before == after
    assert page_text(answer.text).count(NOT_CARRIED) == 1
    assert (stays, True) in options_of(answer.text, second)


@pytest.mark.parametrize("press", THE_PRESSES)
def test_a_store_that_refuses_the_kept_read_gives_the_typing_back(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, press: str
) -> None:
    async def refuses(*_: object) -> None:
        raise sqlite3.OperationalError

    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(browser, monkeypatch)
        box = box_named(page, "instruction")
        rows = [value for value, _ in options_of(page, question) if value.startswith("k")]
        posted = page_post(page, press, typed={box: TYPED}, tick=units_on(page))
        before = every_row(browser)
        monkeypatch.setattr(imports, "kept_words", refuses)
        answer = send(browser, draft_id, ticks_of(posted, question, rows))
        after = every_row(browser)

    assert answer.status_code == 503
    assert TYPED in html.unescape(answer.text)
    assert before == after


# ------------------------------------------------------------------ what a press doesn't scan


def test_an_older_page_reads_its_kept_rows_once_a_question(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its ticks hold words, each to be matched with its row, and there can be as many as a press
    has fields: once for the map and once to read the question, however many it ticks."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(
            browser, monkeypatch, more=2_000, words=lambda _, k: f"K{k}"
        )
        older = written_as_the_older_form(browser, draft_id, page)
        words = [value for value, _ in options_of(older, question) if value.startswith('"')]
        reads = 0
        real = screenshot_posts._kept_of

        def counted(*args: Any) -> Any:  # noqa: ANN401  (passed on as it is)
            nonlocal reads
            reads += 1
            return real(*args)

        monkeypatch.setattr(screenshot_posts, "_kept_of", counted)
        answer = send(browser, draft_id, ticks_of(page_post(older, "keep"), question, words))

    assert len(words) > 2_000
    assert answer.status_code == 200
    assert reads <= 2


def test_a_posted_choice_is_never_scanned() -> None:
    """A question's options are checked against its posted values, and there can be as many of each
    as a press has fields."""

    class NoScan(tuple[str, ...]):
        __slots__ = ()

        def __contains__(self, _: object) -> bool:
            raise AssertionError

    question = screenshot_page.Question(
        "answer:kind:x", "Which?", (("one", "One", True), ("two", "Two", False)), many=True
    )
    _, splits, _, _, _ = screenshot_page._carried(
        [], (question,), [], {"answer:kind:x": NoScan(("two",))}, {}
    )

    assert [checked for _, _, checked in splits[0].options] == [False, True]


def test_a_post_s_fields_arent_compared_with_its_signed_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page signs a digest for each of its groups, and a press can have as many fields as its
    limit allows. Telling the two apart takes the same few comparisons however many groups."""

    class Counted(str):
        __slots__ = ()
        compared: ClassVar[int] = 0
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            Counted.compared += 1
            return str.__eq__(self, other)

    key = b"synthetic page key"
    fields = [
        ("version", "1"),
        ("progress_revision", "0"),
        ("acceptance", "a" * 32),
        ("asked", screenshot_page.asks_text([])),
        ("defaults", "{}"),
        ("identity", "[]"),
        *((f"digest:{n}", "") for n in range(500)),
    ]
    monkeypatch.setattr(screenshot_posts, "fold_reads", lambda _: {})
    many = 2_000
    items = [
        *fields,
        ("check", screenshot_page.page_check(key, fields)),
        ("press", "keep"),
        *((Counted("read:zz"), "1") for _ in range(many)),
    ]
    post = screenshot_posts.read_review(items, key, cast(ReviewState, None), {})

    assert post.press == "keep"
    assert Counted.compared < many * 20


# ------------------------------------------------------------------ what a post has to be first


def kept_row_statements(browser: TestClient, press: Callable[[], Any]) -> list[str]:
    """The statements about kept rows this press ran on the record's connection, values in them."""
    seen: list[str] = []
    connection = state_of(browser).project_state._connection
    connection.set_trace_callback(
        lambda statement: seen.append(statement) if "school_instructions" in statement else None
    )
    try:
        press()
    finally:
        connection.set_trace_callback(None)
    return seen


CRAFTED_NAMES: Final = (
    "answer:instructions:" + "[" * 200_000,
    "answer:instructions:5",
    'answer:instructions:[["id","never signed"],[],1]',
)


@pytest.mark.parametrize("name", CRAFTED_NAMES, ids=["deep", "number", "an id"])
def test_a_post_its_page_didnt_sign_looks_nothing_up(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Refused before anything it names is read: no more statements about kept rows than the same
    post without the name, and none about what it names, whether or not its own list holds that
    name. Its list isn't signed either."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, _ = with_a_kept_question(browser, monkeypatch)
        unsigned = [
            *(field for field in page_post(page, "keep") if field[0] != "check"),
            ("check", "0" * 64),
        ]
        its_own_list = [
            (
                field[0],
                screenshot_page.asks_text([(name, ["k1"])]) if field[0] == "asked" else field[1],
            )
            for field in unsigned
        ]
        answers = []
        plain_post = kept_row_statements(
            browser, lambda: answers.append(send(browser, draft_id, unsigned))
        )
        named_post = kept_row_statements(
            browser, lambda: answers.append(send(browser, draft_id, [*unsigned, (name, "k1")]))
        )
        listed_post = kept_row_statements(
            browser, lambda: answers.append(send(browser, draft_id, [*its_own_list, (name, "k1")]))
        )

    assert [answer.status_code for answer in answers] == [303, 303, 303]
    assert len(named_post) == len(plain_post)
    assert len(listed_post) == len(plain_post)
    assert not any("never signed" in statement for statement in (*named_post, *listed_post))


@pytest.mark.parametrize("name", CRAFTED_NAMES, ids=["deep", "number", "an id"])
def test_a_name_the_signed_list_doesnt_hold_looks_nothing_up(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Field names aren't signed, so a good signature says nothing of them. The question has to be
    in the signed list before its name is read as a question."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, _ = with_a_kept_question(browser, monkeypatch)
        fields = page_post(page, "keep")
        answers = []
        named_post = kept_row_statements(
            browser, lambda: answers.append(send(browser, draft_id, [*fields, (name, "k1")]))
        )

    assert [answer.status_code for answer in answers] == [303]
    assert not any("never signed" in statement for statement in named_post)


@pytest.mark.parametrize(
    "value",
    ["kx", "k", "k-1", "k1_0", "k" + chr(0xFF11), "x1", "k1e3", "k01", "k1 ", " k1"],
)
def test_a_value_no_page_writes_under_a_good_signature_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Only a list signed with the page's key offers one of these."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(browser, monkeypatch)
        before = every_row(browser)
        answer = send(browser, draft_id, forged_offering(browser, draft_id, page, question, value))
        after = every_row(browser)

    assert answer.status_code == 303
    assert before == after


@pytest.mark.parametrize("value", ["k99999999", "k" + "9" * 5_000])
def test_a_row_written_as_a_page_writes_one_with_no_row_is_said(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(browser, monkeypatch)
        before = every_row(browser)
        answer = send(browser, draft_id, forged_offering(browser, draft_id, page, question, value))
        after = every_row(browser)

    assert answer.status_code == 422
    assert page_text(answer.text).count(NOT_CARRIED) == 1
    assert before == after


def forged_offering(
    browser: TestClient, draft_id: str, page: str, question: str, value: str
) -> list[tuple[str, str]]:
    """The page's post ticking this value on this question, under a list signed to offer it."""
    fields = page_post(page, "keep")
    found = unpacked(dict(fields)["asked"])
    asks = [(name, found["lists"][index]) for name, index in found["asks"]]
    asks = [(name, [*values, value] if name == question else values) for name, values in asks]
    return signed_again(
        browser, draft_id, ticks_of(fields, question, [value]), screenshot_page.asks_text(asks)
    )


def packed(body: bytes) -> str:
    return "z1." + base64.urlsafe_b64encode(zlib.compress(body)).decode().rstrip("=")


@pytest.mark.parametrize(
    "asked",
    [
        "z1.AAAA",
        "z9.AAAA",
        "z1.\u65e5",
        "",
        packed(b"not json"),
        packed(b"[1,2]"),
        packed(b'{"lists":[],"asks":[["answer:due:x",3]]}'),
        packed(b'{"lists":[5],"asks":[["answer:due:x",0]]}'),
        '[["answer:due:x"]]',
        "[[1,5]]",
        "{}",
    ],
    ids=[
        "not zlib",
        "another shape",
        "not base64",
        "empty",
        "not json inside",
        "no lists",
        "no such list",
        "a list that isn't one",
        "an old list of one",
        "an old list of a number",
        "neither",
    ],
)
def test_signed_garbage_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, asked: str
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = a_small_page(browser, monkeypatch)
        before = every_row(browser)
        answer = send(
            browser, draft_id, signed_again(browser, draft_id, page_post(page, "keep"), asked)
        )
        after = every_row(browser)

    assert answer.status_code == 303
    assert before == after


# ------------------------------------------------------------------ a page of the older form


def older_page(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch, kind: str
) -> tuple[str, str, str]:
    """A page of this class in the older form, with the box to type in: its review's id, the page
    and the box."""
    if kind == "small":
        draft_id, page = new_rows(browser, monkeypatch, 1, 9, plain)
        return draft_id, written_as_the_older_form(browser, draft_id, page), "title"
    if kind == "90 plain":
        draft_id, page = new_rows(browser, monkeypatch, 10, 9, plain)
        return draft_id, written_as_the_older_form(browser, draft_id, page), "title"
    words: dict[str, Callable[[str, int], str]] = {
        "a kept question": lambda _, k: f"Short kept words {k}.",
        "2 kept of 40,000": lambda _, k: f"{k} " + "\u00e9" * 39_990,
        "1 kept of 200,000 CJK": lambda _, k: f"{k} " + "\u65e5" * 200_000,
    }
    draft_id, page, _ = with_a_kept_question(
        browser,
        monkeypatch,
        more=1 if kind == "1 kept of 200,000 CJK" else 2,
        words=words[kind],
        again=read_again_unsure,
    )
    return draft_id, written_as_the_older_form(browser, draft_id, page), "instruction"


@pytest.mark.parametrize("press", THE_PRESSES)
@pytest.mark.parametrize(
    ("kind", "most_fields", "largest_from", "largest_to"),
    [
        ("small", 1_000, 1, PARSERS_FIELD_BYTES),
        ("a kept question", 1_000, 1, PARSERS_FIELD_BYTES),
        ("90 plain", MOST_FIELDS, 1, PARSERS_FIELD_BYTES),
        ("2 kept of 40,000", 1_000, PARSERS_FIELD_BYTES, MOST_IN_A_FIELD),
        ("1 kept of 200,000 CJK", 1_000, PARSERS_FIELD_BYTES, MOST_IN_A_FIELD),
    ],
)
def test_a_page_made_before_the_compact_list(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    press: str,
    kind: str,
    most_fields: int,
    largest_from: int,
    largest_to: int,
) -> None:
    """An older page open across the upgrade: its press answers as a new page's does, what's stored
    holds the words it was shown, its typing survives, and the page it comes back with is the
    compact form and can be pressed."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, box_field = older_page(browser, monkeypatch, kind)
        box = box_named(page, box_field)
        fields = every_choice(page, press, {box: TYPED})
        words = kept_words_on_record(browser)
        answer = send(browser, draft_id, fields)
        shown = followed(browser, answer)
        record = on_record(browser, draft_id)
        stored = instructions_answered(browser, draft_id)
        typed_kept = TYPED in kept_words_on_record(browser).values()
        again = (
            send(browser, draft_id, every_choice(shown, "keep")) if has_review_form(shown) else None
        )

    largest = largest_a_page_makes(fields)[0]
    assert len(fields) <= most_fields
    assert kind != "90 plain" or len(fields) > PARSERS_FIELDS
    assert largest_from < largest <= largest_to
    assert answer.status_code == ANSWERS[press], page_text(shown)[:300]
    if has_review_form(shown):
        assert in_its_box(shown, box) == TYPED
        assert dict(page_post(shown, "keep"))["asked"].startswith("z1.")
        assert again is not None
        assert again.status_code == 200
    else:
        assert typed_kept
    if press == "later":
        assert TYPED in record.progress
    if box_field == "instruction" and press != "save":
        assert set(words.values()) <= set(stored[-1])


@pytest.mark.parametrize("press", THE_PRESSES)
def test_an_older_page_s_conflict_comes_back_as_rows(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, press: str
) -> None:
    """Its kept words, posted beside "None of these apply", come back ticked on the rows the
    returned page offers, and the press from that page takes them."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(browser, monkeypatch)
        rows = [value for value, _ in options_of(page, question) if value.startswith("k")]
        older = written_as_the_older_form(browser, draft_id, page)
        words = [value for value, _ in options_of(older, question) if value.startswith('"')]
        posted = page_post(older, press, tick=units_on(older))
        fields = ticks_of(posted, question, [*words, ""])
        answer = send(browser, draft_id, fields)
        shown = dict(options_of(answer.text, question))
        again = send(
            browser,
            draft_id,
            ticks_of(page_post(answer.text, "keep"), question, [*rows]),
        )

    assert answer.status_code == 422
    assert all(shown[row] for row in rows)
    assert shown[""]
    assert NOT_CARRIED not in page_text(answer.text)
    assert again.status_code == 200


def test_an_older_page_s_new_words_are_refused_when_tampered_with(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(browser, monkeypatch)
        older = written_as_the_older_form(browser, draft_id, page)
        fields = ticks_of(page_post(older, "keep"), question, ['"Words it never offered."'])
        answer = send(browser, draft_id, fields)

    assert answer.status_code == 303


@pytest.mark.parametrize("value", ["not json", "5", "[]", "k1"])
def test_an_older_list_offering_what_no_page_wrote_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(browser, monkeypatch)
        older = written_as_the_older_form(browser, draft_id, page)
        fields = page_post(older, "keep")
        asks = json.loads(dict(fields)["asked"])
        asked = json.dumps(
            [
                (name, json.dumps([*json.loads(values), value]) if name == question else values)
                for name, values in asks
            ]
        )
        answer = send(
            browser,
            draft_id,
            signed_again(browser, draft_id, ticks_of(fields, question, [value]), asked),
        )

    assert answer.status_code == 303


# ------------------------------------------------------------------ pages rebuilt from a post


def a_kept_conflict(fields: Fields, question: str, row: str) -> list[tuple[str, str]]:
    return ticks_of(fields, question, [row, ""])


@pytest.mark.parametrize("press", THE_PRESSES)
def test_a_conflict_comes_back_as_it_was_posted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, press: str
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, question = with_a_kept_question(browser, monkeypatch)
        row = next(value for value, _ in options_of(page, question) if value.startswith("k"))
        answer = send(
            browser,
            draft_id,
            a_kept_conflict(page_post(page, press, tick=units_on(page)), question, row),
        )

    shown = dict(options_of(answer.text, question))
    assert answer.status_code == 422
    assert shown[row]
    assert shown[""]
    assert NOT_CARRIED not in page_text(answer.text)


def a_page_with_groups_left(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> Made:
    """Three cards on saved rows, with their questions all answered, kept rows ticked."""
    draft_id, page, _ = with_a_kept_question(browser, monkeypatch, cards=3)
    for _ in range(3):
        if len(units_on(page)) >= 3:
            break
        page = followed(browser, send(browser, draft_id, every_choice(page, "keep")))
    return draft_id, page


def test_a_resend_carries_kept_rows(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain resend of a Save shows its counts with the kept rows ticked and no sentence, and one
    with a kept row unticked shows that as not saved yet, writing nothing."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = a_page_with_groups_left(browser, monkeypatch)
        fields = [field for field in every_choice(page, "save") if field[0] != "unit"]
        fields.append(("unit", units_on(page)[0]))
        first = send(browser, draft_id, fields)
        again = send(browser, draft_id, fields)
        question = fields_of(again.text, "answer:instructions:")[0]
        row = next(value for value, _ in options_of(again.text, question) if value.startswith("k"))
        before = every_row(browser)
        changed = send(browser, draft_id, [f for f in fields if f != (question, row)])
        after = every_row(browser)

    assert first.status_code == 200
    assert "1 updated." in page_text(again.text)
    assert NOT_CARRIED not in page_text(again.text)
    assert all(ticked for value, ticked in options_of(again.text, question) if value)
    assert NOT_SAVED_YET in page_text(changed.text)
    assert not dict(options_of(changed.text, question))[row]
    assert NOT_CARRIED not in page_text(changed.text)
    assert before == after


def a_date_asked_beside_a_kept_question(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str, str, str, list[tuple[str, str]]]:
    """English on file due 10/8 and read again due 10/9, with 10/8 kept; Math on file and read with
    other words, its kept row ticked; and Art, new, saved. The review's id, the page, the date's
    question, the instruction's question and the Save's post."""
    both = [
        homework_card(footer="Due (10/8/2026)"),
        homework_card(course="Math 8", title="Set 3", y_top=360, y_bottom=430),
    ]
    first, second, _ = images(3)
    earlier, page = read_review(browser, monkeypatch, (first, homework_answer(cards=both)))
    page = her_name_answered(browser, earlier, page)
    assert send(browser, earlier, page_post(page, "save", tick=units_on(page))).status_code == 200
    again = [
        homework_card(footer="Due (10/9/2026)"),
        homework_card(
            course="Math 8", title="Set 3", instruction="Other words.", y_top=360, y_bottom=430
        ),
        homework_card(course="Art 8", title="Sketch", y_top=450, y_bottom=520),
    ]
    draft_id, page = read_review(browser, monkeypatch, (second, homework_answer(cards=again)))
    english, math, art = fields_of(page, "answer:destination:")
    choose = {
        field: next(value for value, _ in options_of(page, field) if "existing" in value)
        for field in (english, math)
    }
    page = followed(browser, send(browser, draft_id, page_post(page, "keep", choose=choose)))
    [date_question] = fields_of(page, "answer:due:")
    [question] = fields_of(page, "answer:instructions:")
    page = followed(
        browser,
        send(browser, draft_id, page_post(page, "keep", choose={date_question: "2026-10-08"})),
    )
    row = next(value for value, _ in options_of(page, question) if value.startswith("k"))
    tick = [art.removeprefix("answer:destination:")]
    posted = page_post(page, "save", tick=tick, choose={question: row})
    assert "Saved: 1 new assignment." in page_text(send(browser, draft_id, posted).text)
    return draft_id, page, date_question, question, posted


def another_review_moves_the_date(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A review of its own, whose real Save settles English on 10/12."""
    third = images(3)[2]
    answer = homework_answer(cards=[homework_card(footer="Due (10/12/2026)")])
    draft_id, page = read_review(browser, monkeypatch, (third, answer))
    first = fields_of(page, "answer:destination:")[0]
    existing = next(value for value, _ in options_of(page, first) if "existing" in value)
    page = followed(
        browser, send(browser, draft_id, page_post(page, "keep", choose={first: existing}))
    )
    [field] = fields_of(page, "answer:due:")
    page = followed(
        browser, send(browser, draft_id, page_post(page, "keep", choose={field: "2026-10-12"}))
    )
    saved = send(browser, draft_id, page_post(page, "save", tick=units_on(page)))
    assert "1 updated." in page_text(saved.text)


def test_an_option_gone_with_kept_rows(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Save resent at another version, its answers on record, its kept row still offered, and its
    posted date no longer offered: that's said once, and nothing stands in for the date."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, _, date_question, question, posted = a_date_asked_beside_a_kept_question(
            browser, monkeypatch
        )
        another_review_moves_the_date(browser, monkeypatch)
        answer = send(browser, draft_id, posted)

    said = page_text(answer.text)
    assert answer.status_code == 200
    assert said.count(NOT_CARRIED) == 1
    assert ("2026-10-08", True) not in options_of(answer.text, date_question)
    assert any(
        value.startswith("k") and ticked for value, ticked in options_of(answer.text, question)
    )


def two_kept_questions(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str, str, str, str]:
    """Two cards on file read again with other words, the first's question answered with its kept
    row: the review's id, the page, the first question, the other, and the first's Instructions
    box."""
    both = [
        homework_card(),
        homework_card(course="Math 8", title="Set 3", y_top=360, y_bottom=430),
    ]
    first_image, second_image, _ = images(3)
    earlier, page = read_review(browser, monkeypatch, (first_image, homework_answer(cards=both)))
    page = her_name_answered(browser, earlier, page)
    assert send(browser, earlier, page_post(page, "save", tick=units_on(page))).status_code == 200
    again = [
        homework_card(instruction="Complete pages 15 to 16."),
        homework_card(
            course="Math 8", title="Set 3", instruction="Other words.", y_top=360, y_bottom=430
        ),
    ]
    draft_id, page = read_review(browser, monkeypatch, (second_image, homework_answer(cards=again)))
    english, math = fields_of(page, "answer:destination:")
    choose = {
        field: next(value for value, _ in options_of(page, field) if "existing" in value)
        for field in (english, math)
    }
    page = followed(browser, send(browser, draft_id, page_post(page, "keep", choose=choose)))
    card = english.removeprefix("answer:destination:")
    one, two = fields_of(page, "answer:instructions:")
    first, other = (one, two) if card in one else (two, one)
    row = next(value for value, _ in options_of(page, first) if value.startswith("k"))
    page = followed(browser, send(browser, draft_id, page_post(page, "keep", choose={first: row})))
    box = "correct:" + json.dumps([card, "instruction:0"], separators=(",", ":"))
    return draft_id, page, first, other, box


def test_an_answer_cleared_with_its_question_still_asked_says_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conflict on one question, beside a kept answer cleared on another that's still asked: the
    cleared answer comes back blank, and nothing is lost to say."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, first, other, _ = two_kept_questions(browser, monkeypatch)
        row = next(value for value, _ in options_of(page, other) if value.startswith("k"))
        posted = ticks_of(ticks_of(page_post(page, "keep"), first, []), other, [row, ""])
        answer = send(browser, draft_id, posted)

    assert answer.status_code == 422
    assert NOT_CARRIED not in page_text(answer.text)
    assert not any(ticked for _, ticked in options_of(answer.text, first))


def test_an_answer_cleared_with_its_question_gone_is_said(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same, with words typed that take the cleared answer's question off the page, while the
    record still holds the answer."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, first, other, box = two_kept_questions(browser, monkeypatch)
        row = next(value for value, _ in options_of(page, other) if value.startswith("k"))
        typed = page_post(page, "keep", typed={box: TYPED})
        posted = ticks_of(ticks_of(typed, first, []), other, [row, ""])
        answer = send(browser, draft_id, posted)

    assert answer.status_code == 422
    assert first not in fields_of(answer.text, "answer:instructions:")
    assert page_text(answer.text).count(NOT_CARRIED) == 1
    assert in_its_box(answer.text, box) == TYPED
