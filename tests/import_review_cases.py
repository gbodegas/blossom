# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Import review pages of each size and shape a press has to answer, and the presses themselves.

Each page is made through the real routes, with a fake reader and synthetic words, and each press
posts what a browser would post from the page before it. Sizes here are what the form parser
counts: bytes as sent, a field's name plus its value. The two modules that use this are
``tests/test_import_review_bounds.py``, in the default suite, and
``tests/full_scale_import_review.py``, which runs by name.
"""

import base64
import dataclasses
import html
import http.client
import json
import pathlib
import random
import re
import socket
import sqlite3
import threading
import time
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, timedelta
from io import BytesIO
from typing import Any, Final, NamedTuple
from urllib.parse import unquote_plus, urlencode

import pytest
import uvicorn
from fastapi.testclient import TestClient
from PIL import Image

from blossom import screenshot_posts, screenshot_reading
from blossom.anthropic_client import ScreenshotAnswer
from blossom.app import create_app
from blossom.school_instructions import to_wire
from blossom.screenshot_page import page_check
from blossom.screenshot_review import field_most
from blossom.screenshot_save import correction_digest
from blossom.settings import ANTHROPIC_API_KEY_VARIABLE, SCREENSHOT_IMPORT_VARIABLE, Settings
from blossom.stores.import_drafts import AnswerToKeep, new_creation_token
from tests.support import (
    PLAN_DATE,
    Answer,
    fixture_settings,
    homework_answer,
    homework_card,
    state_of,
)

MOST_FIELDS: Final = 200_000
MOST_BYTES: Final = 139_952_128
MOST_IN_A_FIELD: Final = 139_952_128
"""The limits a review press is read within, as literal numbers, so a test holds a limit that
changes rather than reading it from the code."""
LOOPBACK: Final = "127.0.0.1:8781"
FORM: Final = {"content-type": "application/x-www-form-urlencoded"}
TYPED: Final = "Typed words to keep."
TYPED_INSTRUCTION: Final = "Typed words to keep. In the Instructions box."
START: Final = date(2026, 10, 8)
CHECKED_COLUMNS: Final = (
    "assignment_id, text, channel, card, card_day, first_seen_at_utc, first_seen_on, imported_by, "
    "state, settled_by, settled_at_utc, settled_on, revision"
)
"""The columns of a kept instruction a new synthetic row copies from one already kept."""

Fields = Sequence[tuple[str, str]]
Made = tuple[str, str]
"""A review's id and the page it first shows."""


def png(color: tuple[int, int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (64, 48), color).save(buffer, format="PNG")
    return buffer.getvalue()


def images(count: int) -> list[bytes]:
    """This many images, each read as its own."""
    return [png((10 + 12 * n, 240 - 9 * n, 90)) for n in range(count)]


def open_settings(tmp_path: pathlib.Path) -> Settings:
    """Sign-in off, files under ``tmp_path``, and screenshot import on."""
    return fixture_settings(
        BLOSSOM_TODAY=PLAN_DATE.isoformat(),
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
        **{SCREENSHOT_IMPORT_VARIABLE: "1", ANTHROPIC_API_KEY_VARIABLE: "not-a-key-and-never-sent"},
    )


@contextmanager
def here(settings: Settings) -> Iterator[TestClient]:
    """The app as this computer's browser reaches it with the sign-in off."""
    with TestClient(
        create_app(settings),
        follow_redirects=False,
        headers={"Host": LOOPBACK, "Origin": f"http://{LOOPBACK}"},
        client=("127.0.0.1", 50000),
    ) as browser:
        yield browser


def read_form(page: str) -> dict[str, str]:
    """The Read form's fields, as the review page signs them."""
    found = re.search(r'<form method="post" action="([^"]+/read)".*?</form>', page, re.S)
    assert found is not None, page[:400]
    return dict(re.findall(r'name="([^"]+)" value="([^"]*)"', found.group(0)))


def read_review(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch, *answers: tuple[bytes, object]
) -> Made:
    """A review made of the given images, each read as its answer: its id and the page after the
    Read press."""
    readings = {image: ScreenshotAnswer(answer, None, 10, 5) for image, answer in answers}

    def read(settings: Settings, png_bytes: bytes, *, timeout: float) -> ScreenshotAnswer:
        return readings[png_bytes]

    monkeypatch.setattr(screenshot_reading, "read_screenshot", read)
    files = [
        ("screenshots", (f"shot-{n}.png", image, "image/png"))
        for n, (image, _) in enumerate(answers)
    ]
    issued = state_of(browser).real_clock.now().isoformat()
    form = {"token": new_creation_token(), "issued": issued}
    sent = browser.post("/parent/imports", data=form, files=files)
    assert sent.status_code == 303, sent.text[:400]
    draft_id = sent.headers["location"].rsplit("/", 1)[1]
    page = browser.get(f"/parent/imports/{draft_id}").text
    pressed = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page))
    assert pressed.status_code == 200, pressed.text[:400]
    return draft_id, read_until_all_are_kept(browser, draft_id, pressed.text)


def read_until_all_are_kept(browser: TestClient, draft_id: str, page: str) -> str:
    """The page after a Read press, pressed again as the page asks if a large reading was refused
    as busy."""
    for _ in range(4):
        if "Read again" not in page:
            break
        form = read_form(browser.get(f"/parent/imports/{draft_id}").text)
        page = browser.post(f"/parent/imports/{draft_id}/read", data=form).text
    return page


def review_form(page: str) -> str:
    found = re.search(r'<form method="post" action="[^"]+/review".*?</form>', page, re.S)
    assert found is not None, page[:400]
    return found.group(0)


def has_review_form(page: str) -> bool:
    return 'name="asked"' in page


def fields_named(form: str, prefix: str) -> list[str]:
    found = re.findall(rf'name="{re.escape(prefix)}([^"]+)"', form)
    return list(dict.fromkeys(html.unescape(name) for name in found))


def fields_of(page: str, prefix: str) -> list[str]:
    """The whole names of this page's fields with this prefix."""
    return [prefix + subject for subject in fields_named(review_form(page), prefix)]


def units_on(page: str) -> list[str]:
    return [html.unescape(unit) for unit in re.findall(r'name="unit" value="([^"]+)"', page)]


def attribute(tag: str, name: str) -> str | None:
    found = re.search(rf'{name}="([^"]*)"', tag)
    return None if found is None else html.unescape(found.group(1))


def options_of(page: str, field: str) -> list[tuple[str, bool]]:
    """Each control of this field on the page: its value, and whether it's checked."""
    found = []
    for tag in re.findall(r"<input [^>]*>", review_form(page)):
        if attribute(tag, "name") == field:
            found.append((attribute(tag, "value") or "", " checked" in tag))
    return found


def worded_options(page: str, field: str) -> list[tuple[str, str]]:
    """Each instruction option of this field on the page: its value and the words it shows."""
    found = []
    for name, value, label in re.findall(
        r'<input type="checkbox" name="([^"]*)" value="([^"]*)"(?: checked)?> <span>(.*?)</span>',
        review_form(page),
        re.S,
    ):
        if html.unescape(name) == field:
            found.append((html.unescape(value), html.unescape(label).strip("\u201c\u201d")))
    return found


def page_post(
    page: str,
    press: str,
    *,
    choose: Mapping[str, str] | None = None,
    tick: Sequence[str] = (),
    typed: Mapping[str, str] | None = None,
) -> list[tuple[str, str]]:
    """The review form's fields as a browser sends them with scripts off: the hidden fields, the
    checked controls (or those chosen here), the text in its boxes and text areas, the groups
    ticked, the button."""
    choose, typed = dict(choose or {}), dict(typed or {})
    fields: list[tuple[str, str]] = []
    form = review_form(page)
    for tag in re.findall(r"<input [^>]*>", form):
        name = attribute(tag, "name") or ""
        value = attribute(tag, "value") or ""
        kind = attribute(tag, "type")
        if kind == "hidden":
            fields.append((name, value))
        elif kind in ("text", "date"):
            fields.append((name, typed.get(name, value)))
        elif name != "unit" and name not in choose and " checked" in tag:
            fields.append((name, value))
    for tag, content in re.findall(r"(<textarea [^>]*>)(.*?)</textarea>", form, re.S):
        name = attribute(tag, "name") or ""
        shown = html.unescape(content)
        fields.append((name, typed.get(name, shown[1:] if shown.startswith("\n") else shown)))
    fields += [*choose.items(), *(("unit", unit) for unit in tick), ("press", press)]
    return fields


def every_choice(
    page: str, press: str, typed: Mapping[str, str] | None = None, *, none_ticked: bool = False
) -> list[tuple[str, str]]:
    """The most a browser sends from this page: the hidden fields, the boxes, every checkbox, one
    radio of each group as shown, a Type of Task, every group's tick, the button. Without a conflict
    unless asked for: "None of these apply" and "No assigned date was given" stay unticked."""
    typed = dict(typed or {})
    form = review_form(page)
    fields: list[tuple[str, str]] = []
    radios: dict[str, str] = {}
    for tag in re.findall(r"<input [^>]*>", form):
        name = attribute(tag, "name") or ""
        value = attribute(tag, "value") or ""
        kind = attribute(tag, "type")
        if kind == "hidden":
            fields.append((name, value))
        elif kind in ("text", "date"):
            fields.append((name, typed.get(name, value)))
        elif kind == "checkbox":
            says_none = name.startswith("none:") or (is_instruction(name) and not value)
            if none_ticked or not says_none:
                fields.append((name, value))
        elif kind == "radio" and (name not in radios or " checked" in tag):
            radios[name] = "TASK" if name.startswith("answer:kind:") else value
    for tag, content in re.findall(r"(<textarea [^>]*>)(.*?)</textarea>", form, re.S):
        name = attribute(tag, "name") or ""
        shown = html.unescape(content)
        fields.append((name, typed.get(name, shown[1:] if shown.startswith("\n") else shown)))
    return [*fields, *radios.items(), ("press", press)]


def is_instruction(name: str) -> bool:
    return name.startswith("answer:instructions:")


def as_a_browser_sends(fields: Fields) -> bytes:
    """A form's fields serialized as a browser submits them: every line break in a name or a
    value, CR, LF or CRLF, becomes CRLF, then the form is URL-encoded."""

    def lines(text: str) -> str:
        return re.sub(r"\r\n|\r|\n", "\r\n", text)

    return urlencode([(lines(name), lines(value)) for name, value in fields]).encode()


def send(browser: TestClient, draft_id: str, fields: Fields) -> Answer:
    return browser.post(
        f"/parent/imports/{draft_id}/review", content=as_a_browser_sends(fields), headers=FORM
    )


def followed(browser: TestClient, answer: Answer) -> str:
    """The page a press answers with: the review it renders, or the one it redirects to."""
    if answer.status_code != 303:
        return answer.text
    return browser.get(answer.headers["location"]).text


def page_text(page: str) -> str:
    """What the page's main part says, as a reader reads it."""
    part = page[page.find("<main") : page.find("</main>")]
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", part)))


def sizes_as_sent(body: bytes) -> list[tuple[str, int]]:
    """Each field of a body by name, with what the parser counts of it: its name and value as
    sent, without the separators."""
    found = []
    for pair in body.split(b"&"):
        name, _, value = pair.partition(b"=")
        found.append((unquote_plus(name.decode()), len(name) + len(value)))
    return found


def largest_a_page_makes(fields: Fields) -> tuple[int, str]:
    """The largest field of this post that the page made and nobody typed, as the parser counts
    it, with the kind of field it is."""
    return max(
        (size, name.split(":")[0])
        for name, size in sizes_as_sent(as_a_browser_sends(fields))
        if not name.startswith(("correct:", "refused:"))
    )


def every_row(browser: TestClient) -> dict[str, list[tuple[Any, ...]]]:
    """Every table of the record, through a connection of its own."""
    fresh = sqlite3.connect(state_of(browser).settings.database_path)
    try:
        tables = fresh.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {
            table: fresh.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
            for (table,) in tables
        }
    finally:
        fresh.close()


class OnRecord(NamedTuple):
    """What a press left on the record, read through a connection of its own."""

    assignments: int
    answers: int
    progress: str


def on_record(browser: TestClient, draft_id: str) -> OnRecord:
    fresh = sqlite3.connect(state_of(browser).settings.database_path)
    try:
        assignments = fresh.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
        answers = fresh.execute(
            "SELECT COUNT(*) FROM import_draft_answers WHERE draft_id = ?", (draft_id,)
        ).fetchone()[0]
        progress = fresh.execute(
            "SELECT progress FROM import_draft_progress WHERE draft_id = ?", (draft_id,)
        ).fetchone()
    finally:
        fresh.close()
    return OnRecord(int(assignments), int(answers), "" if progress is None else str(progress[0]))


def kept_words_on_record(browser: TestClient) -> dict[int, str]:
    """Every kept instruction's words, by its row."""
    fresh = sqlite3.connect(state_of(browser).settings.database_path)
    try:
        return dict(fresh.execute("SELECT sequence, text FROM school_instructions").fetchall())
    finally:
        fresh.close()


def instructions_answered(browser: TestClient, draft_id: str) -> list[list[str]]:
    """Each stored answer to an instruction question, as the words it holds."""
    fresh = sqlite3.connect(state_of(browser).settings.database_path)
    try:
        rows = fresh.execute(
            "SELECT value FROM import_draft_answers WHERE draft_id = ? AND kind = 'instructions'",
            (draft_id,),
        ).fetchall()
    finally:
        fresh.close()
    return [json.loads(value) for (value,) in rows]


# ------------------------------------------------------------------ what the fake reader reads

MARKS: Final = ("badge", "course", "title", "instruction", "footer")


def unsure_of_everything() -> list[dict[str, str]]:
    return [{"field": field, "reason": "faint"} for field in MARKS]


def card_at(place: int, **fields: object) -> dict[str, Any]:
    return homework_card(y_top=40 + place * 15, y_bottom=50 + place * 15, **fields)


def plain(shot: int, cards: int) -> dict[str, Any]:
    """Plain new rows: no question but where each goes."""
    return homework_answer(
        cards=[card_at(c, course=f"Class {c % 6}", title=f"Set {shot}-{c}") for c in range(cards)]
    )


def every_question(shot: int, cards: int) -> dict[str, Any]:
    """New rows with every mark of doubt the reader can leave on a card."""
    return homework_answer(
        cards=[
            card_at(
                c,
                course=f"Class {shot} {c}",
                title=f"Set {shot} {c}",
                instruction=f"Do part {c} of set {shot}.",
                footer="Due (10/8/2026)",
                uncertain=unsure_of_everything(),
            )
            for c in range(cards)
        ],
        headings=[{"text": "Monday 10/5/2026", "y": 10, "uncertain": "faint"}],
    )


def first_reading(shot: int, cards: int) -> dict[str, Any]:
    """Rows to save, for a later review to land on."""
    return homework_answer(
        cards=[
            card_at(
                c,
                course=f"Class {shot} {c}",
                title=f"Set {shot} {c}",
                instruction=f"Old words {shot} {c}.",
                footer="Due (10/8/2026)",
            )
            for c in range(cards)
        ],
        headings=[{"text": "Monday 10/5/2026", "y": 10, "uncertain": None}],
    )


def read_again(shot: int, cards: int) -> dict[str, Any]:
    """The same rows read again with other words and another due date."""
    return homework_answer(
        cards=[
            card_at(
                c,
                course=f"Class {shot} {c}",
                title=f"Set {shot} {c}",
                instruction=f"New words {shot} {c}.",
                footer="Due (10/9/2026)",
            )
            for c in range(cards)
        ],
        headings=[{"text": "Tuesday 10/6/2026", "y": 10, "uncertain": None}],
    )


def read_again_unsure(shot: int, cards: int) -> dict[str, Any]:
    """The same rows read again with every mark of doubt, under a name the reader saw."""
    return homework_answer(
        cards=[
            card_at(
                c,
                course=f"Class {shot} {c}",
                title=f"Set {shot} {c}",
                instruction=f"New words {shot} {c}.",
                footer="Due (10/9/2026)",
                uncertain=unsure_of_everything(),
            )
            for c in range(cards)
        ],
        active_student={"name": f"Name{shot}", "uncertain": None},
        headings=[{"text": "Tuesday 10/6/2026", "y": 10, "uncertain": "faint"}],
    )


def in_groups(shot: int, cards: int, group: int) -> dict[str, Any]:
    """Cards in name groups of this size, each with its own due date: each card of a name offers
    one new slot per card of that name."""
    made = []
    for c in range(cards):
        n = shot * cards + c
        day = START + timedelta(days=n % group)
        made.append(
            card_at(
                c,
                course=f"Class {n // group}",
                title=f"Homework {n // group}",
                instruction=None,
                footer=f"Due ({day.month}/{day.day}/{day.year})",
            )
        )
    return homework_answer(cards=made)


def her_name_answered(browser: TestClient, draft_id: str, page: str) -> str:
    """The name question answered "hers" directly on the record, and the page after."""
    [form] = fields_named(review_form(page), "answer:name:")
    student = state_of(browser).project_state.student_id()
    drafts = state_of(browser).import_drafts
    version = drafts.snapshot(student, draft_id).version
    answer = AnswerToKeep("name", form, "hers", correction_digest(None), ())
    drafts.keep_answers(student, draft_id, version=version, answers=[answer])
    return browser.get(f"/parent/imports/{draft_id}").text


def new_rows(
    browser: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    shots: int,
    cards: int,
    answer: Callable[[int, int], dict[str, Any]],
    *,
    name: bool = False,
) -> Made:
    made = images(shots)
    draft_id, page = read_review(
        browser, monkeypatch, *[(made[n], answer(n, cards)) for n in range(shots)]
    )
    return draft_id, her_name_answered(browser, draft_id, page) if name else page


def grouped(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch, shots: int, cards: int, group: int
) -> Made:
    made = images(shots)
    return read_review(
        browser, monkeypatch, *[(made[n], in_groups(n, cards, group)) for n in range(shots)]
    )


def add_kept(browser: TestClient, more: int, words: Callable[[str, int], str]) -> None:
    """More kept instructions of these words on every saved row, written directly."""
    fresh = sqlite3.connect(state_of(browser).settings.database_path)
    try:
        holders = fresh.execute(
            "SELECT assignment_id, MAX(sequence) FROM school_instructions GROUP BY assignment_id"
        ).fetchall()
        for assignment, last in holders:
            row = fresh.execute(
                f"SELECT {CHECKED_COLUMNS} FROM school_instructions WHERE sequence = ?",  # noqa: S608
                (last,),
            ).fetchone()
            for k in range(more):
                fresh.execute(
                    f"INSERT INTO school_instructions ({CHECKED_COLUMNS}) "  # noqa: S608
                    f"VALUES ({','.join('?' * 13)})",
                    (row[0], words(assignment, k), *row[2:]),
                )
        fresh.commit()
    finally:
        fresh.close()


def saved_rows(
    browser: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    shots: int,
    cards: int,
    again: Callable[[int, int], dict[str, Any]],
    *,
    more: int = 0,
    words: Callable[[str, int], str] | None = None,
) -> Made:
    """Rows saved from one review, with ``more`` kept instructions added to each, then a second
    review that reads them again."""
    made = images(2 * shots)
    earlier, page = read_review(
        browser, monkeypatch, *[(made[n], first_reading(n, cards)) for n in range(shots)]
    )
    page = her_name_answered(browser, earlier, page)
    saved = send(browser, earlier, page_post(page, "save", tick=units_on(page)))
    assert saved.status_code == 200, saved.text[:400]
    if more and words is not None:
        add_kept(browser, more, words)
    return read_review(
        browser, monkeypatch, *[(made[shots + n], again(n, cards)) for n in range(shots)]
    )


def one_saved_row(browser: TestClient, monkeypatch: pytest.MonkeyPatch, image: bytes) -> str:
    """One row of "English 8" and "Homework" saved from a review of its own: its id."""
    answer = homework_answer(
        cards=[
            card_at(
                0, course="English 8", title="Homework", instruction="Old words.", footer=due(START)
            )
        ]
    )
    earlier, page = read_review(browser, monkeypatch, (image, answer))
    page = her_name_answered(browser, earlier, page)
    saved = send(browser, earlier, page_post(page, "save", tick=units_on(page)))
    assert saved.status_code == 200, saved.text[:400]
    return next(
        row.assignment_id
        for row in state_of(browser).project_state.all_assignments()
        if row.title == "Homework"
    )


def due(day: date) -> str:
    return f"Due ({day.month}/{day.day}/{day.year})"


def far_apart(shot: int, cards: int, words: str) -> dict[str, Any]:
    """Cards of one class and title, due too far apart for any to offer another's saved row."""
    return homework_answer(
        cards=[
            card_at(
                c,
                course="English 8",
                title="Homework",
                instruction=f"{shot} {c} {words}",
                footer=due(START + timedelta(days=20 * (shot * cards + c))),
            )
            for c in range(cards)
        ]
    )


def all_found_onto_one_row(
    browser: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    shots: int,
    cards: int,
    words: str = "New words.",
    more: int = 0,
) -> tuple[str, str, str]:
    """Every card of one name found onto one saved row holding this many more kept instructions, as
    "Find under another name" stores its choice, so the row's instruction question names every
    card: the review's id, the page that asks it, and the question."""
    made = images(1 + shots)
    target = one_saved_row(browser, monkeypatch, made[0])
    if more:
        add_kept(browser, more, numbered)
    draft_id, page = read_review(
        browser, monkeypatch, *[(made[1 + n], far_apart(n, cards, words)) for n in range(shots)]
    )
    chosen = json.dumps({"found": target}, separators=(",", ":"))
    first = fields_of(page, "answer:destination:")[0].removeprefix("answer:destination:")
    student = state_of(browser).project_state.student_id()
    drafts = state_of(browser).import_drafts
    version = drafts.snapshot(student, draft_id).version
    answer = AnswerToKeep("destination", first, chosen, correction_digest(None), (first,))
    drafts.keep_answers(student, draft_id, version=version, answers=[answer])
    page = browser.get(f"/parent/imports/{draft_id}").text
    for _ in range(2):
        fields = [
            (name, chosen if name.startswith("answer:destination:") else value)
            for name, value in every_choice(page, "keep")
        ]
        kept = send(browser, draft_id, fields)
        assert kept.status_code == 200, page_text(kept.text)[:300]
        page = followed(browser, kept)
    [question] = fields_of(page, "answer:instructions:")
    return draft_id, page, question


def with_a_kept_question(
    browser: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cards: int = 1,
    more: int = 2,
    words: Callable[[str, int], str] | None = None,
    again: Callable[[int, int], dict[str, Any]] = read_again,
) -> tuple[str, str, str]:
    """Cards on saved rows holding this many more kept instructions, pressed until a question asks
    which apply: the review's id, the page, and the first such question."""
    draft_id, page = saved_rows(
        browser,
        monkeypatch,
        1,
        cards,
        again,
        more=more,
        words=words or (lambda _, k: f"Kept words {k}."),
    )
    for _ in range(3):
        if "answer:instructions:" in page:
            break
        answer = send(browser, draft_id, every_choice(page, "keep"))
        assert answer.status_code == 200, page_text(answer.text)[:300]
        page = followed(browser, answer)
    return draft_id, page, fields_of(page, "answer:instructions:")[0]


def the_worst_page(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch, shots: int, cards: int
) -> tuple[str, list[tuple[str, str]]]:
    """Every card on a saved row with every question and box, every date typed as text no date reads
    as, so each carries that text beside its box: the review's id, and the post of every field that
    page can send."""

    def not_dates(fields: Fields) -> list[tuple[str, str]]:
        return [(name, "not a date" if is_date_box(name) else value) for name, value in fields]

    draft_id, page = saved_rows(browser, monkeypatch, shots, cards, read_again_unsure)
    for _ in range(2):
        page = followed(browser, send(browser, draft_id, not_dates(every_choice(page, "keep"))))
    page = followed(
        browser, send(browser, draft_id, not_dates(every_choice(page, "keep", none_ticked=True)))
    )
    return draft_id, every_choice(page, "keep", none_ticked=True)


def is_date_box(name: str) -> bool:
    return name.startswith("correct:") and json.loads(name.removeprefix("correct:"))[1] in (
        "due",
        "assigned",
    )


BEYOND_THE_PLANE: Final = "\U0001f600"
"""A character of four bytes in UTF-8, twelve as sent."""


def at_their_limits(fields: Fields, *field_starts: str) -> list[tuple[str, str]]:
    """This post with each box whose field starts with one of these filled to its field's limit."""
    made = []
    for name, value in fields:
        if name.startswith("correct:"):
            field = str(json.loads(name.removeprefix("correct:"))[1])
            if field.startswith(field_starts):
                made.append((name, BEYOND_THE_PLANE * field_most(field)))
                continue
        made.append((name, value))
    return made


def every_box_at_its_limit(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch, shots: int, cards: int
) -> tuple[str, list[tuple[str, str]]]:
    """Every card on a saved row with a new instruction at its limit in characters of four bytes,
    ticked; every box at its limit in those characters; every date carrying its text beside its
    box: the review's id and that post."""
    long_course = BEYOND_THE_PLANE * 194
    long_title = BEYOND_THE_PLANE * 294
    long_words = BEYOND_THE_PLANE * 3_990

    def card(shot: int, c: int, instruction: str, day: str) -> dict[str, Any]:
        return card_at(
            c,
            course=f"{shot}-{c} {long_course}",
            title=f"{shot}-{c} {long_title}",
            instruction=instruction,
            footer=f"Due ({day}/2026)",
        )

    made = images(2 * shots)
    earlier, page = read_review(
        browser,
        monkeypatch,
        *[
            (
                made[n],
                homework_answer(
                    cards=[card(n, c, f"old {n} {c}", "10/8") for c in range(cards)],
                    headings=[{"text": "Monday 10/5/2026", "y": 10, "uncertain": None}],
                ),
            )
            for n in range(shots)
        ],
    )
    page = her_name_answered(browser, earlier, page)
    saved = send(browser, earlier, page_post(page, "save", tick=units_on(page)))
    assert saved.status_code == 200, page_text(saved.text)[:300]
    draft_id, page = read_review(
        browser,
        monkeypatch,
        *[
            (
                made[shots + n],
                homework_answer(
                    cards=[card(n, c, f"{n} {c} {long_words}", "10/9") for c in range(cards)],
                    headings=[{"text": "Tuesday 10/6/2026", "y": 10, "uncertain": None}],
                ),
            )
            for n in range(shots)
        ],
    )
    for _ in range(2):
        page = followed(browser, send(browser, draft_id, every_choice(page, "keep")))
    dates = at_their_limits(every_choice(page, "keep"), "due", "assigned")
    page = send(browser, draft_id, dates).text
    return draft_id, at_their_limits(
        every_choice(page, "keep"), "course", "title", "instruction", "due", "assigned"
    )


def box_named(page: str, field: str, nth: int = 0) -> str:
    """The whole name of the nth box of "Correct what was read" whose field starts with this."""
    found = [
        subject
        for subject in fields_named(review_form(page), "correct:")
        if str(json.loads(subject)[1]).startswith(field)
    ]
    return f"correct:{found[nth]}"


def row_of_box(box: str) -> str:
    return str(json.loads(box.removeprefix("correct:"))[0])


def in_its_box(page: str, box: str) -> str | None:
    """What this box holds on the page, or None for a page without it."""
    if not has_review_form(page):
        return None
    return dict(page_post(page, "keep")).get(box)


# ------------------------------------------------------------------ the acceptance list


def random_cjk(assignment: str, k: int) -> str:
    """Words that don't compress, the same on each run."""
    made = random.Random(f"{assignment}-{k}")  # noqa: S311
    return "".join(chr(made.randint(0x4E00, 0x9FFF)) for _ in range(4000))


def numbered(_: str, k: int) -> str:
    return f"K{k}"


def repeated(character: str, count: int) -> Callable[[str, int], str]:
    def words(_: str, k: int) -> str:
        return f"{k} " + character * count

    return words


@dataclasses.dataclass(frozen=True)
class Shape:
    """One review of the acceptance list: how it's made, and what the record holds after it."""

    make: Callable[[TestClient, pytest.MonkeyPatch], Made]
    assignments: tuple[int, ...] = ()
    """Assignments before and after its Save; none for the shapes that update a saved row."""
    answers: tuple[int, ...] = ()
    """The review's answers before its Save and after each."""
    saved: tuple[str, ...] = ()
    """What each Save says it saved."""


def plain_new(shots: int, cards: int) -> Callable[[TestClient, pytest.MonkeyPatch], Made]:
    def make(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> Made:
        return new_rows(browser, monkeypatch, shots, cards, plain)

    return make


def every_new(shots: int, cards: int) -> Callable[[TestClient, pytest.MonkeyPatch], Made]:
    def make(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> Made:
        return new_rows(browser, monkeypatch, shots, cards, every_question, name=True)

    return make


def of_one_name(
    shots: int, cards: int, group: int
) -> Callable[[TestClient, pytest.MonkeyPatch], Made]:
    def make(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> Made:
        return grouped(browser, monkeypatch, shots, cards, group)

    return make


def on_saved(
    shots: int, cards: int, again: Callable[[int, int], dict[str, Any]]
) -> Callable[[TestClient, pytest.MonkeyPatch], Made]:
    def make(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> Made:
        return saved_rows(browser, monkeypatch, shots, cards, again)

    return make


def one_card_kept(
    more: int, words: Callable[[str, int], str]
) -> Callable[[TestClient, pytest.MonkeyPatch], Made]:
    """One card on a saved row, with this many more kept instructions."""

    def make(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> Made:
        return saved_rows(browser, monkeypatch, 1, 1, read_again_unsure, more=more, words=words)

    return make


def many_kept(
    shots: int, cards: int, more: int, words: Callable[[str, int], str]
) -> Callable[[TestClient, pytest.MonkeyPatch], Made]:
    def make(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> Made:
        return saved_rows(browser, monkeypatch, shots, cards, read_again, more=more, words=words)

    return make


def updated(rows: int) -> str:
    return f"Saved: 0 new assignments. {rows} updated."


def added(rows: int) -> str:
    return f"Saved: {rows} new assignment{'' if rows == 1 else 's'}."


SHAPES: Final[dict[str, Shape]] = {
    "90 plain": Shape(plain_new(10, 9), (7, 96), (181, 3), (added(89),)),
    "300 plain": Shape(plain_new(10, 30), (7, 306), (601, 3), (added(299),)),
    "600 plain": Shape(plain_new(10, 60), (7, 606), (1201, 3), (added(599),)),
    "460 every question": Shape(every_new(10, 46), (7, 466), (3681, 9), (added(459),)),
    "600 every question": Shape(every_new(10, 60), (7, 606), (4801, 9), (added(599),)),
    "140 of one name": Shape(of_one_name(10, 14, 140), (7, 8), (282, 3), (added(1),)),
    "600 in groups of 20": Shape(of_one_name(10, 60, 20), (7, 37), (1231, 3), (added(30),)),
    "300 in groups of 60": Shape(of_one_name(10, 30, 60), (7, 12), (606, 3), (added(5),)),
    "600 of one name": Shape(of_one_name(10, 60, 600), (7, 8), (1202, 3), (added(1),)),
    "450 on saved rows": Shape(on_saved(10, 45, read_again), (), (2696, 2), (updated(449),)),
    "600 on saved rows": Shape(on_saved(10, 60, read_again), (), (3596, 2), (updated(599),)),
    "450 every question on saved rows": Shape(
        on_saved(10, 45, read_again_unsure), (), (5406, 4866, 4326), (updated(45), updated(45))
    ),
    "600 every question on saved rows": Shape(
        on_saved(10, 60, read_again_unsure), (), (7206, 6486, 5766), (updated(60), updated(60))
    ),
    "one card, 2 kept of 40,000": Shape(
        one_card_kept(2, repeated("\u00e9", 39_990)), (), (13, 0), (updated(1),)
    ),
    "one card, 20 kept of 4,000 CJK": Shape(
        one_card_kept(20, repeated("\u65e5", 3_990)), (), (13, 0), (updated(1),)
    ),
    "one card, 300 kept of about 3,900 plain": Shape(
        one_card_kept(300, repeated("Read the section and answer the questions. ", 90)),
        (),
        (13, 0),
        (updated(1),),
    ),
    "one card, 1 kept of 200,000 CJK": Shape(
        one_card_kept(1, repeated("\u65e5", 200_000)), (), (13, 0), (updated(1),)
    ),
    "one card, 1 kept of 2,000,000 plain": Shape(
        one_card_kept(1, repeated("word ", 400_000)), (), (13, 0), (updated(1),)
    ),
    "one card, 200 kept of 4,000 random CJK": Shape(
        one_card_kept(200, random_cjk), (), (13, 0), (updated(1),)
    ),
    "one card, 18,100 kept of a few characters": Shape(
        one_card_kept(18_100, numbered), (), (13, 0), (updated(1),)
    ),
    "one card, 199,900 kept of a few characters": Shape(
        one_card_kept(199_900, numbered), (), (13, 0), (updated(1),)
    ),
    "600 saved rows, each with 1 kept of 4,000 random CJK": Shape(
        many_kept(10, 60, 1, random_cjk), (), (3596, 2), (updated(599),)
    ),
}
ACCEPTANCE: Final = tuple(SHAPES)
"""Every shape the presses are held to. A test keeps each in one of the two modules' lists."""


@dataclasses.dataclass(frozen=True)
class Pressed:
    """One press and what came of it, with sizes as the parser counts them."""

    label: str
    status: int
    fields: int
    body: int
    largest: tuple[int, str]
    """The largest field the page made, with its kind."""
    box: str | None
    """What the typed box holds on the page the press came back with, or None for a page
    without it."""
    could_not_be_read: bool
    record: OnRecord
    said: str


class Presses:
    """One review pressed through, each press posting what the page before it shows."""

    def __init__(self, browser: TestClient, draft_id: str, box: str) -> None:
        self.browser, self.draft_id, self.box = browser, draft_id, box
        self.pressed: list[Pressed] = []
        self.kept_words: tuple[str, ...] = ()
        """On a one-card shape, the words of every kept row before its Save."""
        self.answered: list[list[str]] = []
        """On a one-card shape, what its instruction answers held before its Save."""
        self.values: tuple[str, ...] = ()
        """On a one-card shape, the values its instruction question offered before its Save."""
        self.kept_after_save: tuple[str, ...] = ()
        """On a one-card shape, the words of every kept row after its Save."""

    def press(self, label: str, fields: Fields) -> str:
        """This post sent: the page it comes back with."""
        answer = send(self.browser, self.draft_id, fields)
        assert answer.status_code != 400, f"{label}: {page_text(answer.text)[:200]}"
        page = followed(self.browser, answer)
        self.pressed.append(
            Pressed(
                label,
                answer.status_code,
                len(fields),
                len(as_a_browser_sends(fields)),
                largest_a_page_makes(fields),
                in_its_box(page, self.box),
                "could not be read" in page,
                on_record(self.browser, self.draft_id),
                page_text(page),
            )
        )
        return page

    def keep_later_keep(self, page: str, words: str) -> str:
        """Keep with the words typed, Keep from its page, Later from that, Keep from Later's: the
        page the last comes back with."""
        page = self.press("keep", every_choice(page, "keep", {self.box: words}))
        page = self.press("keep from the page keep returned", every_choice(page, "keep"))
        page = self.press("later from the page keep returned", every_choice(page, "later"))
        return self.press("keep from the page later returned", every_choice(page, "keep"))

    def save(self, page: str, ticks: Iterable[str], label: str = "save") -> str:
        fields = [field for field in every_choice(page, "save") if field[0] != "unit"]
        return self.press(label, [*fields, *(("unit", tick) for tick in ticks)])

    def keep_and_later_after(self, page: str) -> None:
        page = self.press("keep from the page save returned", every_choice(page, "keep"))
        self.press("later from that page", every_choice(page, "later"))


def of_screenshot(page: str, number: int) -> list[str]:
    """The groups this screenshot's cards are in, by each group's tick: Blossom won't save the
    groups of different names together."""
    found = []
    for unit, about in re.findall(
        r'name="unit" value="([^"]+)"[^>]*> <span>.*?visually-hidden"> for ([^<]*)<',
        review_form(page),
        re.S,
    ):
        if f"creenshot {number}," in about or f"creenshot {number}" in about.split(",")[0]:
            found.append(html.unescape(unit))
    return found


def press_through(browser: TestClient, shape: str, monkeypatch: pytest.MonkeyPatch) -> Presses:
    """This shape's review, pressed through all three presses and the pages they return. The
    typing is in an Instructions box on the one-card shapes, since a typed title moves the row off
    its saved row and the kept question never shows, and in a title box on the rest."""
    draft_id, page = SHAPES[shape].make(browser, monkeypatch)
    if shape.startswith("one card"):
        return kept_presses(browser, draft_id, page)
    box = box_named(page, "title")
    presses = Presses(browser, draft_id, box)
    page = presses.keep_later_keep(page, TYPED)
    row = row_of_box(box)
    if "every question on saved rows" in shape:
        for number in (2, 3):
            ticks = [unit for unit in of_screenshot(page, number) if row not in unit]
            page = presses.save(page, ticks, f"save of screenshot {number}")
        presses.press("later from the page save returned", every_choice(page, "later"))
    else:
        page = presses.save(page, [unit for unit in units_on(page) if row not in unit])
        presses.keep_and_later_after(page)
    return presses


def kept_presses(browser: TestClient, draft_id: str, page: str) -> Presses:
    """A one-card shape's presses, which end when Save finishes the review."""
    presses = Presses(browser, draft_id, box_named(page, "instruction"))
    page = presses.keep_later_keep(page, TYPED_INSTRUCTION)
    presses.kept_words = tuple(kept_words_on_record(browser).values())
    presses.answered = instructions_answered(browser, draft_id)
    [question] = fields_of(page, "answer:instructions:")
    presses.values = tuple(value for value, _ in options_of(page, question))
    presses.save(page, units_on(page))
    presses.kept_after_save = tuple(kept_words_on_record(browser).values())
    return presses


def every_press_answered(presses: Presses, shape: str) -> None:
    """The acceptance check on a shape pressed through: the answers its presses gave, the typing in
    its box on every page that has it, what Later's progress held on a connection of its own, and
    what's on the record before and after each Save."""
    made = SHAPES[shape]
    statuses = [press.status for press in presses.pressed]
    saves = [n for n, press in enumerate(presses.pressed) if press.label.startswith("save")]
    assert not any(press.could_not_be_read for press in presses.pressed)
    assert TYPED in presses.pressed[2].record.progress
    assert [presses.pressed[saves[0] - 1].record.answers, *held_after(presses, saves)] == list(
        made.answers
    )
    for n, said in zip(saves, made.saved, strict=True):
        assert said in presses.pressed[n].said
    for press in presses.pressed:
        assert press.largest[0] <= 1024 * 1024, press.label
    if shape.startswith("one card"):
        assert statuses == [200, 200, 303, 200, 200]
        assert all(press.box == TYPED_INSTRUCTION for press in presses.pressed[:4])
        assert presses.pressed[-1].box is None
        assert set(presses.kept_words) <= set(presses.answered[-1])
        assert not {to_wire(words) for words in presses.kept_words} & set(presses.values)
        assert TYPED_INSTRUCTION in presses.kept_after_save
        return
    assert statuses == [200, 200, 303, 200, 200, 200, 303]
    assert all(press.box == TYPED for press in presses.pressed)
    if made.assignments:
        before, after = presses.pressed[saves[0] - 1], presses.pressed[saves[0]]
        assert (before.record.assignments, after.record.assignments) == made.assignments


def held_after(presses: Presses, saves: Sequence[int]) -> list[int]:
    return [presses.pressed[n].record.answers for n in saves]


# ------------------------------------------------------------------ what a signed form holds


def unpacked(asked: str) -> dict[str, Any]:
    """The signed list a page wrote, read here rather than by the code it tests."""
    assert asked.startswith("z1."), asked[:40]
    text = asked.removeprefix("z1.")
    body = zlib.decompress(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
    found: dict[str, Any] = json.loads(body)
    return found


def signed_again(
    browser: TestClient, draft_id: str, fields: Fields, asked: str
) -> list[tuple[str, str]]:
    """This post with this signed list, signed with the review's own key: what only someone holding
    that key can send."""
    student = state_of(browser).project_state.student_id()
    key = state_of(browser).import_drafts.snapshot(student, draft_id).page_key
    changed = [(name, asked if name == "asked" else value) for name, value in fields]
    signed = [
        (name, value)
        for name, value in changed
        if name in screenshot_posts.SIGNED or name.startswith("digest:")
    ]
    check = page_check(key, signed)
    return [(name, check if name == "check" else value) for name, value in changed]


def written_as_the_older_form(browser: TestClient, draft_id: str, page: str) -> str:
    """This page as it was written before its signed list was compact, signed with the review's key:
    each kept option as its words, in its value and in a list of JSON inside JSON, and new words as
    themselves. A page already of that form is left as it is."""
    fields = page_post(page, "keep")
    asked = dict(fields)["asked"]
    if asked.startswith("["):
        return page
    rows = {f"k{row}": to_wire(words) for row, words in kept_words_on_record(browser).items()}
    found = unpacked(asked)
    asks = []
    for name, index in found["asks"]:
        shown = (
            [rows.get(value, value) for value, _ in options_of(page, name)]
            if is_instruction(name)
            else found["lists"][index]
        )
        asks.append((name, json.dumps(shown, separators=(",", ":"))))
    older = json.dumps(sorted(asks), separators=(",", ":"))
    check = dict(signed_again(browser, draft_id, fields, older))["check"]
    form = review_form(page)
    made = re.sub(
        r'(name="asked" value=")[^"]*', lambda m: m[1] + html.escape(older), form, count=1
    )
    made = re.sub(r'(name="check" value=")[^"]*', lambda m: m[1] + check, made, count=1)
    made = re.sub(r'value="(k[1-9][0-9]*)"', lambda m: f'value="{html.escape(rows[m[1]])}"', made)
    return page.replace(form, made, 1)


# ------------------------------------------------------------------ over a real socket


@contextmanager
def serving(browser: TestClient) -> Iterator[int]:
    """The app this client holds, on a free loopback port for this block: the port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            browser.app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            log_config=None,
            lifespan="off",
            ws="none",
            http="h11",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(400):
        if server.started:
            break
        time.sleep(0.05)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=30)


class Reply(NamedTuple):
    status: int
    text: str


def press_over_socket(
    port: int, draft_id: str, pieces: Iterable[bytes], *, length: int | None
) -> Reply:
    """Each piece written as it comes, under a declared length, or chunked when there is none."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
    connection.putrequest(
        "POST", f"/parent/imports/{draft_id}/review", skip_host=True, skip_accept_encoding=True
    )
    connection.putheader("Host", LOOPBACK)
    connection.putheader("Origin", f"http://{LOOPBACK}")
    connection.putheader("Content-Type", FORM["content-type"])
    if length is None:
        connection.putheader("Transfer-Encoding", "chunked")
    else:
        connection.putheader("Content-Length", str(length))
    connection.endheaders()
    try:
        for piece in pieces:
            connection.send(
                f"{len(piece):x}\r\n".encode() + piece + b"\r\n" if length is None else piece
            )
        if length is None:
            connection.send(b"0\r\n\r\n")
        answer = connection.getresponse()
        return Reply(answer.status, answer.read().decode())
    finally:
        connection.close()


def body_of_fields(fields: Fields, count: int, total: int | None = None) -> bytes:
    """This post with fields nothing reads, up to this many fields, each of one length so the body
    comes to exactly this many bytes, or as short as they can be."""
    body = as_a_browser_sends(fields)
    more = count - len(fields)
    head = b"&read%3Azz="
    if total is None:
        return body + (head + b"1") * more
    each, over = divmod(total - len(body) - more * len(head), more)
    return body + (head + b"a" * each) * (more - over) + (head + b"a" * (each + 1)) * over


def body_with_a_paste(fields: Fields, box: str, typed: str, total: int) -> tuple[bytes, str]:
    """This post with this box holding as much of this character as makes the body exactly this many
    bytes, plain characters making up what's left: the body, and what the box holds."""
    others = as_a_browser_sends([field for field in fields if field[0] != box])
    name = urlencode([(box, "")]).encode()
    as_sent = urlencode([("", typed)]).encode().removeprefix(b"=")
    room = total - len(others) - 1 - len(name)
    count, left = divmod(room, len(as_sent))
    return others + b"&" + name + as_sent * count + b"a" * left, typed * count + "a" * left


def in_writes(body: bytes, size: int) -> Iterator[bytes]:
    for at in range(0, len(body), size):
        yield body[at : at + size]


def padded_to(body: bytes, total: int, *, each: int = 4 * 1024 * 1024) -> Iterator[bytes]:
    """This body, then fields nothing reads, up to exactly this many bytes as sent, none of them
    past ``each``."""
    head = b"&read%3Azz="
    yield body
    left = total - len(body)
    assert left > len(head)
    while left > 0:
        take = min(left, each)
        if left - take and left - take <= len(head):
            take -= len(head)
        yield head + b"a" * (take - len(head))
        left -= take
