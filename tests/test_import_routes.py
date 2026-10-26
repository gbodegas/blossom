# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Homework screenshots in Family review, through their routes: the upload, the images and a
Read press, with a fake reader and synthetic images only.

Every route serves a parent alone, checked before any body is read, and every answer is marked
no-store. Nothing on Family review offers the upload yet.
"""

import ast
import asyncio
import dataclasses
import html
import http.client
import io
import json
import logging
import pathlib
import re
import socket
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final
from urllib.parse import urlencode

import pytest
import uvicorn
from fastapi.testclient import TestClient
from markupsafe import escape
from PIL import Image

from blossom import (
    screenshot_page,
    screenshot_posts,
    screenshot_reading,
    screenshot_review,
    screenshot_save,
)
from blossom.anthropic_client import ScreenshotAnswer
from blossom.app import create_app
from blossom.household import UnreadableHouseholdSecret
from blossom.routes import imports, navigation
from blossom.screenshot_save import (
    CONFIRMABLE,
    AlreadySaved,
    NotSaved,
    Saved,
    correction_digest,
)
from blossom.settings import ANTHROPIC_API_KEY_VARIABLE, SCREENSHOT_IMPORT_VARIABLE, Settings
from blossom.stores.drafts import StoreBusy, WriterBusy
from blossom.stores.import_drafts import (
    DRAFT_LIFETIME,
    FAILURES,
    AnswerToKeep,
    DraftExpired,
    DraftNotFound,
    new_creation_token,
)
from blossom.stores.project_state import Assignment, AssignmentKind
from blossom.uploads import as_new_image, prepare_image
from tests.support import (
    ARRIVAL_CUE,
    HERS,
    PLAN_DATE,
    THEIRS,
    Answer,
    Element,
    Rule,
    UnreadCss,
    View,
    control_names,
    declared_for,
    elements_of,
    field_names,
    fixture_settings,
    homework_answer,
    homework_card,
    homework_from_a_note,
    names_not_led_by_their_words,
    names_without_their_words,
    selector,
    signed_in,
    signed_in_household,
    state_of,
    style_rules,
    unshielded,
    winner,
)

LOOPBACK = "127.0.0.1:8781"
KEY = {ANTHROPIC_API_KEY_VARIABLE: "not-a-key-and-never-sent"}
ANSWERED = ScreenshotAnswer(homework_answer(), None, 1500, 300)


def says(page: str, text: str) -> bool:
    """Whether a page shows these words, as its template escapes them."""
    return str(escape(text)) in page


def png(width: int = 64, height: int = 48, color: tuple[int, int, int] = (200, 40, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def gif() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buffer, format="GIF")
    return buffer.getvalue()


RED, BLUE, GREEN = png(color=(200, 40, 40)), png(color=(40, 40, 200)), png(color=(40, 200, 40))


def open_settings(tmp_path: pathlib.Path, *, key: bool = True, turned_on: bool = True) -> Settings:
    """Sign-in off, files under ``tmp_path``, and screenshot import on unless told otherwise."""
    return fixture_settings(
        BLOSSOM_TODAY=PLAN_DATE.isoformat(),
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
        **({SCREENSHOT_IMPORT_VARIABLE: "1"} if turned_on else {}),
        **(KEY if key else {}),
    )


def signed_in_with_import(tmp_path: pathlib.Path) -> Settings:
    """The household's sign-in on, and screenshot import on."""
    return dataclasses.replace(signed_in_household(tmp_path), screenshot_import=True)


@contextmanager
def here(settings: Settings, *, client: str = "127.0.0.1") -> Iterator[TestClient]:
    """The app as this computer's browser reaches it with the sign-in off."""
    with TestClient(
        create_app(settings),
        follow_redirects=False,
        headers={"Host": LOOPBACK, "Origin": f"http://{LOOPBACK}"},
        client=(client, 50000),
    ) as browser:
        yield browser


class Reader:
    """A fake reader: each image's answer by its color, counted per call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.answers: dict[bytes, ScreenshotAnswer] = {}
        self.calls: list[bytes] = []
        monkeypatch.setattr(screenshot_reading, "read_screenshot", self.read)

    def read(self, settings: Settings, png_bytes: bytes, *, timeout: float) -> ScreenshotAnswer:
        self.calls.append(png_bytes)
        return self.answers.get(png_bytes, ANSWERED)


def now_of(browser: TestClient) -> datetime:
    return state_of(browser).real_clock.now()


def first_form(browser: TestClient, issued: datetime | None = None) -> dict[str, str]:
    return {
        "token": new_creation_token(),
        "issued": (issued or now_of(browser)).isoformat(),
    }


def upload(
    browser: TestClient,
    *images: bytes,
    form: dict[str, str] | None = None,
    to: str = "/parent/imports",
) -> Answer:
    files = [("screenshots", (f"shot-{n}.png", data, "image/png")) for n, data in enumerate(images)]
    return browser.post(to, data=form if form is not None else first_form(browser), files=files)


def made(browser: TestClient, *images: bytes) -> str:
    """A first upload that commits: the draft its redirect names."""
    sent = upload(browser, *images)
    assert sent.status_code == 303, sent.text
    found = re.fullmatch(r"/parent/imports/([0-9a-f]{32})", sent.headers["location"])
    assert found is not None
    return found.group(1)


def drafts(browser: TestClient) -> int:
    connection = state_of(browser).import_drafts._connection
    return int(connection.execute("SELECT COUNT(*) FROM import_drafts").fetchone()[0])


def images_of(browser: TestClient, draft_id: str) -> list[str]:
    student = state_of(browser).project_state.student_id()
    return [
        item.image_id for item in state_of(browser).import_drafts.snapshot(student, draft_id).images
    ]


def read_form(page: str) -> dict[str, str]:
    """The Read form's fields, as the review page signs them."""
    found = re.search(r'<form method="post" action="([^"]+/read)".*?</form>', page, re.S)
    assert found is not None, page
    fields = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', found.group(0)))
    return fields


# ------------------------------------------------------------------ uploads


def test_a_first_upload_makes_a_review_and_answers_with_a_redirect(tmp_path: pathlib.Path) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED, BLUE)
        page = browser.get(f"/parent/imports/{draft_id}")

    assert page.status_code == 200
    assert page.text.count('alt="Screenshot ') == 2
    assert "Available until " in page.text
    assert says(page.text, imports.LABELS["unread"])
    assert page.headers["cache-control"] == "no-store"


def test_a_declared_length_past_the_limit_is_refused_and_nothing_is_stored(
    tmp_path: pathlib.Path,
) -> None:
    with here(open_settings(tmp_path)) as browser:
        refused = browser.post(
            "/parent/imports",
            content=b"",
            headers={
                "content-type": "multipart/form-data; boundary=x",
                "content-length": str(40_000_001),
            },
        )
        assert drafts(browser) == 0

    assert refused.status_code == 413
    assert says(refused.text, imports.UPLOAD_SAID["body_too_large"])


def test_a_body_without_a_length_is_refused_once_it_passes_the_limit(
    tmp_path: pathlib.Path,
) -> None:
    def body() -> Iterator[bytes]:
        yield b"--x\r\n"
        for _ in range(41):
            yield b"0" * 1_000_000

    with here(open_settings(tmp_path)) as browser:
        refused = browser.post(
            "/parent/imports",
            content=body(),
            headers={"content-type": "multipart/form-data; boundary=x"},
        )
        assert drafts(browser) == 0

    assert refused.status_code == 413


def test_an_eleventh_file_or_one_past_12_mb_refuses_the_upload(tmp_path: pathlib.Path) -> None:
    with here(open_settings(tmp_path)) as browser:
        eleven = upload(browser, *[png(color=(n, 0, 0)) for n in range(11)])
        large = upload(browser, b"\x89PNG" + b"0" * 12_000_001)
        assert drafts(browser) == 0

    assert eleven.status_code == 413
    assert says(eleven.text, imports.UPLOAD_SAID["too_many_files"])
    assert large.status_code == 413
    assert says(large.text, imports.UPLOAD_SAID["file_too_large"])


@pytest.mark.parametrize(
    "form",
    [
        {"token": "a" * 32, "issued": "x", "other": "1"},
        {"token": "a" * 33, "issued": "2026-10-15T12:00:00+00:00"},
    ],
    ids=["an unknown field", "a field past its limit"],
)
def test_a_field_out_of_its_rules_refuses_the_upload(
    tmp_path: pathlib.Path, form: dict[str, str]
) -> None:
    with here(open_settings(tmp_path)) as browser:
        refused = upload(browser, RED, form=form)
        assert drafts(browser) == 0

    assert refused.status_code == 400


def test_a_body_that_isnt_multipart_is_refused(tmp_path: pathlib.Path) -> None:
    with here(open_settings(tmp_path)) as browser:
        refused = browser.post("/parent/imports", data={"token": "a" * 32})
        assert drafts(browser) == 0

    assert refused.status_code == 400
    assert says(refused.text, imports.UPLOAD_SAID["unreadable"])


def test_each_image_is_refused_by_its_place_and_the_others_are_kept(tmp_path: pathlib.Path) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, gif(), RED, b"plain text, not a picture")
        page = browser.get(f"/parent/imports/{draft_id}")
        again = browser.get(f"/parent/imports/{draft_id}")

    assert says(page.text, imports.image_refusal(1, "not_png_or_jpeg"))
    assert says(page.text, imports.image_refusal(3, "not_png_or_jpeg"))
    assert page.text.count('alt="Screenshot ') == 1
    assert not says(again.text, imports.image_refusal(1, "not_png_or_jpeg"))


def test_a_first_upload_with_no_valid_image_makes_no_review(tmp_path: pathlib.Path) -> None:
    with here(open_settings(tmp_path)) as browser:
        refused = upload(browser, gif())
        form = first_form(browser)
        body = (
            "--b\r\n"
            'Content-Disposition: form-data; name="token"\r\n\r\n'
            f"{form['token']}\r\n"
            "--b\r\n"
            'Content-Disposition: form-data; name="issued"\r\n\r\n'
            f"{form['issued']}\r\n"
            "--b\r\n"
            'Content-Disposition: form-data; name="screenshots"; filename=""\r\n'
            "Content-Type: application/octet-stream\r\n\r\n\r\n"
            "--b--\r\n"
        )
        empty = browser.post(
            "/parent/imports",
            content=body.encode(),
            headers={"content-type": "multipart/form-data; boundary=b"},
        )
        assert drafts(browser) == 0

    assert refused.status_code == 422
    assert says(refused.text, imports.image_refusal(1, "not_png_or_jpeg"))
    assert empty.status_code == 422


def test_an_upload_writes_no_temporary_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "temporary"
    folder.mkdir()
    with here(open_settings(tmp_path)) as browser:
        monkeypatch.setattr(tempfile, "tempdir", str(folder))

        def refused(*_: object, **__: object) -> object:
            msg = "no temporary file"
            raise OSError(msg)

        for name in ("NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile", "mkstemp"):
            monkeypatch.setattr(tempfile, name, refused)
        made(browser, RED)

    assert list(folder.iterdir()) == []


def test_the_same_first_upload_sent_again_finds_its_review_and_adds_nothing_twice(
    tmp_path: pathlib.Path,
) -> None:
    with here(open_settings(tmp_path)) as browser:
        form = first_form(browser)
        first = upload(browser, RED, form=form)
        second = upload(browser, RED, form=form)
        assert drafts(browser) == 1
        draft_id = first.headers["location"].rsplit("/", 1)[1]
        page = browser.get(f"/parent/imports/{draft_id}")

    assert second.headers["location"] == first.headers["location"]
    assert says(page.text, imports.image_refusal(1, "duplicate"))
    assert page.text.count('alt="Screenshot ') == 1


def test_adding_to_an_open_review_and_never_past_ten(tmp_path: pathlib.Path) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, *[png(color=(n, 1, 1)) for n in range(9)])
        to = f"/parent/imports/{draft_id}/images"
        too_many = upload(browser, BLUE, GREEN, form={}, to=to)
        one = upload(browser, BLUE, form={}, to=to)
        repeated = upload(browser, BLUE, form={}, to=to)
        count = len(images_of(browser, draft_id))

    assert too_many.status_code == 409
    assert says(too_many.text, imports.too_many(1))
    assert one.status_code == 303
    assert repeated.status_code == 303
    assert count == 10


def test_adding_to_a_closed_review_is_refused_and_to_an_emptied_one_accepted(
    tmp_path: pathlib.Path,
) -> None:
    with here(open_settings(tmp_path)) as browser:
        emptied = made(browser, RED)
        [image_id] = images_of(browser, emptied)
        removed = browser.post(f"/parent/imports/{emptied}/images/{image_id}/remove")
        again = upload(browser, BLUE, form={}, to=f"/parent/imports/{emptied}/images")
        closed = made(browser, GREEN)
        student = state_of(browser).project_state.student_id()
        store = state_of(browser).import_drafts
        store.discard_review(student, closed, version=store.snapshot(student, closed).version)
        refused = upload(browser, BLUE, form={}, to=f"/parent/imports/{closed}/images")

    assert removed.status_code == 303
    assert again.status_code == 303
    assert refused.status_code == 409
    assert says(refused.text, imports.SAID["closed"])


@pytest.mark.parametrize(
    ("moved", "allowed"),
    [
        (timedelta(0), True),
        (DRAFT_LIFETIME - timedelta(seconds=1), True),
        (DRAFT_LIFETIME, False),
        (-timedelta(seconds=5), False),
    ],
    ids=["now", "just under 48 hours", "48 hours", "later than now"],
)
def test_a_first_upload_form_is_used_only_within_48_hours_of_its_making(
    tmp_path: pathlib.Path, moved: timedelta, allowed: bool
) -> None:
    with here(open_settings(tmp_path)) as browser:
        form = first_form(browser, now_of(browser) - moved)
        sent = upload(browser, RED, form=form)
        unreadable = upload(browser, RED, form={**first_form(browser), "issued": "yesterday"})
        count = drafts(browser)

    assert (sent.status_code == 303) is allowed
    if not allowed:
        assert says(sent.text, imports.SAID["too_old"])
    assert unreadable.status_code == 400
    assert count == (1 if allowed else 0)


def test_a_form_from_before_a_restart_still_uploads(tmp_path: pathlib.Path) -> None:
    settings = open_settings(tmp_path)
    with here(settings) as browser:
        form = first_form(browser)
        first = upload(browser, RED, form=form)
    with here(settings) as browser:
        again = upload(browser, RED, BLUE, form=form)
        count = drafts(browser)

    assert again.headers["location"] == first.headers["location"]
    assert count == 1


def test_a_resent_upload_after_a_removal_adds_the_image_again_unread(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        page = browser.get(f"/parent/imports/{draft_id}").text
        browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page))
        [image_id] = images_of(browser, draft_id)
        browser.post(f"/parent/imports/{draft_id}/images/{image_id}/remove")
        resent = upload(browser, RED, form={}, to=f"/parent/imports/{draft_id}/images")
        shown = browser.get(f"/parent/imports/{draft_id}").text

    assert resent.status_code == 303
    assert says(shown, imports.LABELS["unread"])
    assert len(reader.calls) == 1


# ------------------------------------------------------------------ images


def test_view_screenshot_serves_the_prepared_png_and_a_removal_takes_it_out(
    tmp_path: pathlib.Path,
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        [image_id] = images_of(browser, draft_id)
        shown = browser.get(f"/parent/imports/{draft_id}/images/{image_id}")
        removed = browser.post(f"/parent/imports/{draft_id}/images/{image_id}/remove")
        gone = browser.get(f"/parent/imports/{draft_id}/images/{image_id}")
        again = browser.post(f"/parent/imports/{draft_id}/images/{image_id}/remove")
        page = browser.get(f"/parent/imports/{draft_id}")

    assert shown.status_code == 200
    assert shown.headers["content-type"] == "image/png"
    assert shown.headers["cache-control"] == "no-store"
    assert shown.content.startswith(b"\x89PNG")
    assert removed.status_code == 303
    assert gone.status_code == 404
    assert again.status_code == 303
    assert says(page.text, imports.SAID["image_gone"])


def test_another_students_or_an_unknown_review_is_not_found(tmp_path: pathlib.Path) -> None:
    with here(open_settings(tmp_path)) as browser:
        store = state_of(browser).import_drafts
        added = store.create(
            "another-student",
            kind="homework_screenshot",
            made_by="parent",
            creation_token=new_creation_token(),
            images=[],
        )
        assert added is None
        theirs = store.create(
            "another-student",
            kind="homework_screenshot",
            made_by="parent",
            creation_token=new_creation_token(),
            images=[as_new_image(prepare_image(RED))],
        )
        assert theirs is not None
        answers = [
            browser.get(f"/parent/imports/{theirs.draft_id}"),
            browser.get(f"/parent/imports/{'f' * 32}"),
            browser.get("/parent/imports/not-an-id"),
            browser.get(f"/parent/imports/{theirs.draft_id}/images/{theirs.added[0]}"),
        ]

    assert [answer.status_code for answer in answers] == [404, 404, 404, 404]
    assert all(says(answer.text, imports.SAID["absent"]) for answer in answers[:3])


# ------------------------------------------------------------------ reading


def test_a_read_press_reads_the_shown_images_and_the_page_says_how_each_stands(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Reader(monkeypatch)
    reader.answers[BLUE] = ScreenshotAnswer(None, "not_json", 10, 5)
    reader.answers[GREEN] = ScreenshotAnswer(homework_answer(image_kind="unsupported"), None, 9, 4)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED, BLUE, GREEN)
        page = browser.get(f"/parent/imports/{draft_id}").text
        assert says(page, imports.DISCLOSED)
        pressed = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page))

    assert pressed.status_code == 200
    assert len(reader.calls) == 3
    assert says(pressed.text, imports.LABELS["read"])
    assert says(pressed.text, imports.FAILED["not_json"])
    assert says(pressed.text, imports.LABELS["wrong_kind"])


def test_read_again_reads_only_unread_images_failed_ones_included(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Reader(monkeypatch)
    reader.answers[BLUE] = ScreenshotAnswer(None, "not_json", 10, 5)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED, BLUE)
        page = browser.get(f"/parent/imports/{draft_id}").text
        browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page))
        reader.answers[BLUE] = ANSWERED
        again = browser.get(f"/parent/imports/{draft_id}").text
        browser.post(f"/parent/imports/{draft_id}/read", data=read_form(again))

    assert sorted(reader.calls[:2]) == sorted([RED, BLUE])
    assert reader.calls[2:] == [BLUE]


def test_a_read_form_names_only_the_images_its_page_showed_and_is_checked(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        form = read_form(browser.get(f"/parent/imports/{draft_id}").text)
        upload(browser, BLUE, form={}, to=f"/parent/imports/{draft_id}/images")
        tampered = browser.post(
            f"/parent/imports/{draft_id}/read", data={**form, "images": form["images"] + ",x"}
        )
        assert reader.calls == []
        pressed = browser.post(f"/parent/imports/{draft_id}/read", data=form)

    assert says(tampered.text, imports.SAID["unchecked"])
    assert reader.calls == [RED]
    assert says(pressed.text, imports.LABELS["unread"])


def test_a_press_while_an_attempt_is_current_starts_no_call(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        form = read_form(browser.get(f"/parent/imports/{draft_id}").text)
        student = state_of(browser).project_state.student_id()
        [image_id] = images_of(browser, draft_id)
        state_of(browser).import_drafts.start_attempt(student, draft_id, [image_id])
        pressed = browser.post(f"/parent/imports/{draft_id}/read", data=form)

    assert reader.calls == []
    assert "Reading is under way until " in pressed.text


def test_a_press_whose_calls_all_ended_lets_read_again_start_at_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Reader(monkeypatch)
    reader.answers[RED] = ScreenshotAnswer(None, "service_refused", 0, 0)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        browser.post(
            f"/parent/imports/{draft_id}/read",
            data=read_form(browser.get(f"/parent/imports/{draft_id}").text),
        )
        reader.answers[RED] = ANSWERED
        browser.post(
            f"/parent/imports/{draft_id}/read",
            data=read_form(browser.get(f"/parent/imports/{draft_id}").text),
        )

    assert reader.calls == [RED, RED]


def test_with_no_key_no_read_is_offered_and_none_is_made(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path, key=False)) as browser:
        draft_id = made(browser, RED)
        page = browser.get(f"/parent/imports/{draft_id}").text
        pressed = browser.post(f"/parent/imports/{draft_id}/read", data={"images": "", "check": ""})

    assert "/read" not in page
    assert says(page, imports.SAID["no_key"])
    assert reader.calls == []
    assert says(pressed.text, imports.SAID["no_key"])


def test_after_a_restart_an_unread_image_and_a_read_form_from_before_still_work(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Reader(monkeypatch)
    settings = open_settings(tmp_path)
    with here(settings) as browser:
        draft_id = made(browser, RED)
        form = read_form(browser.get(f"/parent/imports/{draft_id}").text)
        student = state_of(browser).project_state.student_id()
        [image_id] = images_of(browser, draft_id)
        state_of(browser).import_drafts.start_attempt(student, draft_id, [image_id])
    with here(settings) as browser:
        page = browser.get(f"/parent/imports/{draft_id}").text
        pressed = browser.post(f"/parent/imports/{draft_id}/read", data=form)

    assert says(page, imports.LABELS["unread"])
    assert reader.calls == [RED]
    assert says(pressed.text, imports.LABELS["read"])


def test_a_reading_the_busy_file_couldnt_keep_is_named_on_the_press_page(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        form = read_form(browser.get(f"/parent/imports/{draft_id}").text)
        store = state_of(browser).import_drafts

        def busy(*_: object, **__: object) -> bool:
            from blossom.stores.drafts import WriterBusy

            raise WriterBusy

        monkeypatch.setattr(store, "store_reading", busy)
        pressed = browser.post(f"/parent/imports/{draft_id}/read", data=form)

    assert says(pressed.text, imports.busy_reading(1))
    assert says(pressed.text, imports.LABELS["ended"])


def test_each_image_label_is_one_of_the_closed_set() -> None:
    """ "Read again" is said only of an image an attempt covered; failures keep their own."""
    assert imports.LABELS == {
        "unread": "Not read yet.",
        "ended": "Not read. Read again.",
        "read": "Read.",
        "wrong_kind": "This doesn't look like the school's homework list.",
    }
    assert set(imports.FAILED) == set(FAILURES)


def test_an_image_never_read_is_never_told_to_read_again(tmp_path: pathlib.Path) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        page = browser.get(f"/parent/imports/{draft_id}").text

    assert says(page, "Screenshot 1 \u00b7 Not read yet.")
    assert "Read again" not in page


# ------------------------------------------------------------------ the review's rows


def read_review(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch, *answers: tuple[bytes, object]
) -> tuple[str, str]:
    """A review made of the given images, each read as its answer: its id and the page after
    the Read press."""
    reader = Reader(monkeypatch)
    for image, answer in answers:
        reader.answers[image] = ScreenshotAnswer(answer, None, 10, 5)
    draft_id = made(browser, *(image for image, _ in answers))
    page = browser.get(f"/parent/imports/{draft_id}").text
    pressed = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page))
    assert pressed.status_code == 200, pressed.text
    return draft_id, pressed.text


def review_form(page: str) -> str:
    found = re.search(r'<form method="post" action="[^"]+/review".*?</form>', page, re.S)
    assert found is not None, page
    return found.group(0)


def stored_answers(browser: TestClient, draft_id: str) -> dict[tuple[str, str], str]:
    student = state_of(browser).project_state.student_id()
    tables = state_of(browser).project_state.draft_for_save(student, draft_id)
    assert tables is not None
    return {key: value for key, (value, _) in tables.answers.items()}


def checked_values(form: str) -> list[tuple[str, str]]:
    return re.findall(r'name="([^"]+)" value="([^"]*)" checked', form)


def acceptance_of(page: str) -> str:
    found = re.search(r'name="acceptance" value="([0-9a-f]{32})"', page)
    assert found is not None
    return found.group(1)


def keep(browser: TestClient, draft_id: str, *answers: tuple[str, str, str]) -> None:
    """Answers kept as a press keeps them, at the review's version: (kind, subject, value)."""
    student = state_of(browser).project_state.student_id()
    drafts = state_of(browser).import_drafts
    version = drafts.snapshot(student, draft_id).version
    drafts.keep_answers(
        student,
        draft_id,
        version=version,
        answers=[
            AnswerToKeep(kind, subject, value, correction_digest(None), ())
            for kind, subject, value in answers
        ],
    )


def fields_named(form: str, prefix: str) -> list[str]:
    found = re.findall(rf'name="{re.escape(prefix)}([^"]+)"', form)
    return list(dict.fromkeys(html.unescape(name) for name in found))


def box_subject(page: str, field: str, nth: int = 0) -> str:
    """The subject of the nth box of "Correct what was read" for this field, in page order:
    ``course``, ``title``, ``instruction:{i}``, ``due`` or ``assigned``."""
    found = [s for s in fields_named(review_form(page), "correct:") if json.loads(s)[1] == field]
    return found[nth]


def her_name_answered(browser: TestClient, draft_id: str, page: str) -> str:
    [form] = fields_named(review_form(page), "answer:name:")
    keep(browser, draft_id, ("name", form, "hers"))
    return browser.get(f"/parent/imports/{draft_id}").text


def test_the_rows_appear_once_read_each_lone_card_preselected_and_nothing_stored(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        again = browser.get(f"/parent/imports/{draft_id}").text
        answers = stored_answers(browser, draft_id)

    form = review_form(page)
    pieces = fields_named(form, "answer:destination:")
    assert answers == {}
    assert checked_values(form) == [
        (f"answer:destination:{p}", "{&#34;new&#34;:1}") for p in pieces
    ]
    assert form.count(screenshot_page.SAID["preselected"]) == 2
    assert says(page, "A new Sentence Composing (1)")
    assert "<h3>Sentence Composing</h3>" in form
    assert says(page, "Due Thursday, October 8")
    assert 'From the school: <q class="authored-text">Complete pages 12 to 14.</q>' in form
    assert form.count(screenshot_page.SAID["new_assignment"]) == 2
    assert says(page, "Screenshot 1 names Wren. Is this her name? Answer before saving its rows.")
    assert form.count("Still to answer: the student name.") == 2
    assert 'name="unit"' not in form
    assert says(page, screenshot_page.SAID["status"])
    assert acceptance_of(page) != acceptance_of(again)


def test_the_review_form_signs_its_fields_with_the_review_s_own_key(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        student = state_of(browser).project_state.student_id()
        snapshot = state_of(browser).import_drafts.snapshot(student, draft_id)

    hidden = [
        (name, html.unescape(value))
        for name, value in re.findall(
            r'<input type="hidden" name="([^"]+)" value="([^"]*)">', review_form(page)
        )
    ]
    names = [name for name, _ in hidden]
    signed = [
        (name, value)
        for name, value in hidden
        if name != "check" and not name.startswith(screenshot_posts.READ_PREFIX)
    ]
    # Beside each box, after the signed fields, a digest of its value with no correction,
    # read only to give typing back from a review that has gone, and never signed.
    assert all(
        name.startswith(screenshot_posts.READ_PREFIX) for name in names[names.index("check") + 1 :]
    )
    assert names[:6] == [
        "version",
        "progress_revision",
        "acceptance",
        "asked",
        "defaults",
        "identity",
    ]
    assert sum(name.startswith("digest:") for name in names) == 2
    assert dict(hidden)["version"] == str(snapshot.version)
    assert dict(hidden)["check"] == screenshot_page.page_check(snapshot.page_key, signed)


def test_with_her_name_answered_each_ready_group_can_be_ticked_and_still_nothing_else_stored(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        after = her_name_answered(browser, draft_id, page)
        answers = stored_answers(browser, draft_id)

    form = review_form(after)
    assert [kind for kind, _ in answers] == ["name"]
    assert "Still to answer" not in form
    assert form.count('name="unit"') == 2
    assert ("answer:name:" + next(iter(answers))[1], "hers") in checked_values(form)


def test_same_name_assignments_are_each_listed_with_their_facts_and_none_is_preselected(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    twins = [
        Assignment(
            assignment_id=f"assignment-sentence-composing-{tail}",
            course="English 8",
            title="Sentence Composing",
            due_date=date(2026, 10, 9),
            dependencies=[],
            reported_submission_status="not_reported",
            assigned_on=None,
            kind=AssignmentKind.HOMEWORK,
        )
        for tail in ("aaaa1111", "bbbb1111")
    ]
    with here(open_settings(tmp_path)) as browser:
        state_of(browser).project_state.upsert_assignments(twins)
        _, page = read_review(browser, monkeypatch, (RED, homework_answer()))

    form = review_form(page)
    composing = re.search(
        r"<legend>Where it goes<span class=\"visually-hidden\"> for Sentence Composing \(.*?"
        r"</fieldset>",
        form,
        re.S,
    )
    assert composing is not None
    control = composing.group(0)
    assert " checked" not in control
    assert control.count("Sentence Composing, due Fri 10/9, no assigned date, no instructions") == 2
    assert "ref aa1111" in control
    assert "ref bb1111" in control
    assert control.count("Due Fri 10/9: this card says Thu 10/8.") == 2
    assert control.count("Doesn&#39;t have this card&#39;s words.") == 2
    assert "A new Sentence Composing (1)" in control
    assert screenshot_page.SAID["preselected"] not in control
    assert says(page, screenshot_page.SAID["choose_each"])


def test_a_card_saved_before_starts_on_already_saved_with_every_alternative_beside_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = homework_answer(cards=[homework_card()])
    with here(open_settings(tmp_path)) as browser:
        first, page = read_review(browser, monkeypatch, (RED, answer))
        page = her_name_answered(browser, first, page)
        saved = send(browser, first, page_post(page, "save", tick=units_on(page)))
        assert says(saved.text, imports.counted(1, 0, 0, 0))
        _, again = read_review(browser, monkeypatch, (RED, answer))

    form = review_form(again)
    label = (
        "Sentence Composing, due Thu 10/8, assigned Mon 10/5, \u201cComplete pages 12 to 14.\u201d"
    )
    assert f"Already saved in {label}." in html.unescape(form)
    assert len([n for n, _ in checked_values(form) if n.startswith("answer:destination:")]) == 1
    assert "A new Sentence Composing (1)" in form
    assert screenshot_page.SAID["find"] in html.unescape(form)
    assert screenshot_page.SAID["already"] in html.unescape(form)
    assert "Saved as homework" not in form


def test_a_destination_due_another_day_asks_with_nothing_chosen_and_says_what_is_kept(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = Assignment(
        assignment_id="assignment-sentence-composing-cccc2222",
        course="English 8",
        title="Sentence Composing",
        due_date=date(2026, 10, 9),
        dependencies=[],
        reported_submission_status="not_reported",
        assigned_on=None,
        kind=AssignmentKind.HOMEWORK,
    )
    answer = homework_answer(cards=[homework_card()])
    with here(open_settings(tmp_path)) as browser:
        state_of(browser).project_state.upsert_assignments([saved])
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        [piece] = fields_named(review_form(page), "answer:destination:")
        keep(
            browser,
            draft_id,
            (
                "destination",
                piece,
                json.dumps({"existing": saved.assignment_id}, separators=(",", ":")),
            ),
        )
        asked = browser.get(f"/parent/imports/{draft_id}").text
        [subject] = fields_named(review_form(asked), "answer:due:")
        keep(browser, draft_id, ("due", subject, "2026-10-08"))
        answered = browser.get(f"/parent/imports/{draft_id}").text

    form = html.unescape(review_form(asked))
    assert (
        "Due date for Sentence Composing, due Fri 10/9, no assigned date, no instructions" in form
    )
    assert "Due Fri 10/9, as saved" in form
    assert "Due Thu 10/8" in form
    assert not [v for n, v in checked_values(review_form(asked)) if n.startswith("answer:due:")]
    assert "Still to answer: the due date, the student name." in form
    assert (
        "Due Thu 10/8, as you chose. The school's Fri 10/9 is kept beside it, and her page "
        "will say the sources disagree."
    ) in html.unescape(answered)


def test_a_field_the_reader_wasnt_sure_of_asks_to_be_confirmed_or_corrected(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = homework_answer(
        cards=[homework_card(uncertain=[{"field": "title", "reason": "a smudge over it"}])]
    )
    with here(open_settings(tmp_path)) as browser:
        _, page = read_review(browser, monkeypatch, (RED, answer))

    form = html.unescape(review_form(page))
    unsure = re.search(
        r"<legend>The reader wasn't sure of the title\.<span class=\"visually-hidden\"> for "
        r"Sentence Composing \(screenshot 1, card 1\)</span></legend>(.*?)</fieldset>",
        form,
        re.S,
    )
    assert unsure is not None
    assert screenshot_page.SAID["confirm"] in unsure.group(1)
    assert 'name="correct:' not in unsure.group(1)
    assert 'or correct it under <a href="#fix-1">Correct what was read</a>' in unsure.group(1)
    assert 'name="correct:' in form
    assert "Still to answer: a field the reader wasn't sure of, the student name." in form


def split_answers() -> tuple[object, object]:
    """One assigned card read under two day headings: one row whose assigned days differ."""
    card = homework_card(footer="Due (10/9/2026)")
    monday = homework_answer(
        headings=[{"text": "Monday 10/5/2026", "y": 150, "uncertain": None}], cards=[card]
    )
    tuesday = homework_answer(
        headings=[{"text": "Tuesday 10/6/2026", "y": 150, "uncertain": None}], cards=[card]
    )
    return monday, tuesday


def test_a_row_whose_assigned_days_differ_asks_first_and_names_both_days(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monday, tuesday = split_answers()
    with here(open_settings(tmp_path)) as browser:
        _, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, tuesday))

    form = review_form(page)
    assert says(page, "Sentence Composing: " + screenshot_page.SAID["split"])
    assert says(page, "Assigned Mon 10/5 (screenshot 1, card 1)")
    assert says(page, "Assigned Tue 10/6 (screenshot 2, card 1)")
    assert says(page, screenshot_page.SAID["split_apart"])
    assert " checked" not in form.split("What saving adds")[0].split("Different assigned")[-1]


def test_a_refused_group_names_its_cause_and_offers_whole_changes_that_store_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monday, tuesday = split_answers()
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, tuesday))
        [row] = fields_named(review_form(page), "answer:split:")
        keep(browser, draft_id, ("split", row, json.dumps("split")))
        split = browser.get(f"/parent/imports/{draft_id}").text
        days = fields_named(review_form(split), "answer:destination:")
        keep(browser, draft_id, *(("destination", d, '{"new":1}') for d in days))
        refused = browser.get(f"/parent/imports/{draft_id}").text
        answers = stored_answers(browser, draft_id)

    assert len(days) == 2
    assert says(
        refused,
        "Sentence Composing (screenshot 1, card 1, assigned Mon 10/5) and Sentence Composing "
        "(screenshot 2, card 1, assigned Tue 10/6) are different days of one row, so they "
        "can't go to the same assignment.",
    )
    assert says(refused, "Use this change")
    assert says(
        refused,
        "Sentence Composing (screenshot 1, card 1, assigned Mon 10/5): from a new Sentence "
        "Composing (1) to a new Sentence Composing (2)",
    )
    assert says(
        refused,
        "Sentence Composing (screenshot 2, card 1, assigned Tue 10/6): from a new Sentence "
        "Composing (1) to a new Sentence Composing (3)",
    )
    assert 'name="unit"' not in review_form(refused)
    assert {(k, v) for (k, _), v in answers.items() if k == "destination"} == {
        ("destination", '{"new":1}')
    }
    offered = re.findall(
        r'<form method="post" action="[^"]+/review" id="offer-[^"]+">.*?</form>', refused, re.S
    )
    assert 1 <= len(offered) <= 5
    assert [form.count('name="answer:destination:') for form in offered][-1] == 2


# ------------------------------------------------------------------ the review's presses


def page_post(
    page: str,
    press: str,
    *,
    choose: Mapping[str, str] | None = None,
    tick: Sequence[str] = (),
    typed: Mapping[str, str] | None = None,
) -> list[tuple[str, str]]:
    """The review form's fields as a browser sends them with scripts off: the hidden fields, the
    checked controls (or those chosen here), the text typed in its boxes and text areas, the
    groups ticked, the button. A text area's first line break is the markup's, as a browser
    reads it. `send` serializes them as a browser does, line breaks as CRLF."""
    choose, typed = dict(choose or {}), dict(typed or {})
    fields: list[tuple[str, str]] = []
    for tag in re.findall(r"<input [^>]*>", review_form(page)):
        name = html.unescape(re.search(r'name="([^"]+)"', tag).group(1))  # type: ignore[union-attr]
        found = re.search(r'value="([^"]*)"', tag)
        value = html.unescape(found.group(1)) if found else ""
        kind = re.search(r'type="([^"]+)"', tag).group(1)  # type: ignore[union-attr]
        if kind == "hidden":
            fields.append((name, value))
        elif kind in ("text", "date"):
            fields.append((name, typed.get(name, value)))
        elif name != "unit" and name not in choose and " checked" in tag:
            fields.append((name, value))
    for tag, content in re.findall(r"(<textarea [^>]*>)(.*?)</textarea>", review_form(page), re.S):
        name = html.unescape(re.search(r'name="([^"]+)"', tag).group(1))  # type: ignore[union-attr]
        shown = html.unescape(content)
        fields.append((name, typed.get(name, shown[1:] if shown.startswith("\n") else shown)))
    fields += [*choose.items(), *(("unit", unit) for unit in tick), ("press", press)]
    return fields


def as_a_browser_sends(fields: Sequence[tuple[str, str]]) -> bytes:
    """A form's fields serialized as a browser submits them: every line break in a name or a
    value, CR, LF or CRLF, becomes CRLF, then the form is URL-encoded."""

    def lines(text: str) -> str:
        return re.sub(r"\r\n|\r|\n", "\r\n", text)

    return urlencode([(lines(name), lines(value)) for name, value in fields]).encode()


def send(browser: TestClient, draft_id: str, fields: Sequence[tuple[str, str]]) -> Answer:
    return browser.post(
        f"/parent/imports/{draft_id}/review",
        content=as_a_browser_sends(fields),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )


def followed(browser: TestClient, answer: Answer) -> str:
    """The page a press answers with: the review it renders, or the one it redirects to."""
    if answer.status_code != 303:
        return answer.text
    return browser.get(answer.headers["location"]).text


def units_on(page: str) -> list[str]:
    return [html.unescape(u) for u in re.findall(r'name="unit" value="([^"]+)"', page)]


def digests_on(page: str) -> list[str]:
    return [
        name.removeprefix("digest:")
        for name in fields_named(review_form(page), "")
        if name.startswith("digest:")
    ]


def assignment_count(browser: TestClient) -> int:
    return len(state_of(browser).project_state.all_assignments())


def test_keep_these_answers_stores_what_the_page_posts_and_says_so(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        after = followed(browser, send(browser, draft_id, page_post(page, "keep")))
        answers = stored_answers(browser, draft_id)

    assert sorted(kind for kind, _ in answers) == ["destination", "destination"]
    assert set(answers.values()) == {'{"new":1}'}
    assert says(after, imports.PRESS_SAID["kept"])
    assert screenshot_page.SAID["preselected"] not in after


def focused(page: str) -> list[str]:
    """Each element of a page that takes the focus as it arrives, as its opening tag."""
    return re.findall(r"<[a-z0-9]+\b[^>]*\sautofocus\b[^>]*>", page)


def test_each_press_lands_on_its_answer_and_an_ordinary_visit_takes_no_focus(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read, Keep these answers, a search under another name and a refusal each answer with a
    page whose answer takes the focus, once; the review opened again, the find page opened with
    no words, and the review shown again after the answer take none."""
    other = Assignment(
        assignment_id="assignment-grammar-practice-eeee4444",
        course="English 8",
        title="Grammar Practice",
        due_date=date(2026, 10, 8),
        dependencies=[],
        reported_submission_status="not_reported",
        assigned_on=None,
        kind=AssignmentKind.HOMEWORK,
    )
    with here(open_settings(tmp_path)) as browser:
        state_of(browser).project_state.upsert_assignments([other])
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        visit = browser.get(f"/parent/imports/{draft_id}").text
        piece = fields_named(review_form(page), "answer:destination:")[0]
        opened = browser.post(f"/parent/imports/{draft_id}/find", data={"piece": piece}).text
        found = browser.post(
            f"/parent/imports/{draft_id}/find", data={"piece": piece, "words": "grammar"}
        ).text
        nothing = browser.post(
            f"/parent/imports/{draft_id}/find", data={"piece": piece, "words": "zzzz"}
        ).text
        gone = browser.post(f"/parent/imports/{draft_id}/find", data={"piece": "x", "words": "a"})
        kept = followed(browser, send(browser, draft_id, page_post(page, "keep")))
        again = browser.get(f"/parent/imports/{draft_id}").text

    assert focused(page) == [TOLD_LANDS]
    assert says(page.split('id="told"', 1)[1].split("</div>", 1)[0], imports.read_said_line(1, 2))
    assert focused(visit) == focused(opened) == focused(again) == []
    assert focused(kept) == [
        '<div class="problem" role="status" id="said" tabindex="-1" autofocus>'
    ]
    assert says(kept.split('id="said"', 1)[1].split("</div>", 1)[0], imports.PRESS_SAID["kept"])
    assert focused(found) == ['<p id="find-first" tabindex="-1" autofocus>']
    assert "Grammar Practice" in found.split('id="find-first"', 1)[1].split("</p>", 1)[0]
    assert focused(nothing) == ['<p class="note" id="find-nothing" tabindex="-1" autofocus>']
    assert gone.status_code == 404
    assert focused(gone.text) == ['<p class="problem" role="alert" tabindex="-1" autofocus>']


SAID_LANDS: Final = '<div class="problem" role="status" id="said" tabindex="-1" autofocus>'
"""The screenshot review's answer block as it takes the focus."""
TOLD_LANDS: Final = '<div role="status" id="told" tabindex="-1" autofocus>'
"""The screenshot review's status lines, what an upload or a Read press did, as they take the
focus."""


def test_a_visit_that_shows_only_a_warning_takes_no_focus_and_a_press_s_answer_lands_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While her name can't be checked, the review opened from its address says so and takes
    no focus, since a warning is no answer; the page a press is sent to, here a screenshot
    already in the review, lands on its answer, the warning under it, and opened again takes
    none."""

    async def unreadable(*args: object, **kwargs: object) -> bytes:
        raise UnreadableHouseholdSecret

    with here(open_settings(tmp_path)) as browser:
        draft_id, _ = read_review(browser, monkeypatch, (RED, homework_answer()))
        monkeypatch.setattr(imports, "grade_name_key", unreadable)
        visit = browser.get(f"/parent/imports/{draft_id}").text
        sent = upload(browser, RED, form={}, to=f"/parent/imports/{draft_id}/images")
        landed = browser.get(sent.headers["location"]).text
        again = browser.get(f"/parent/imports/{draft_id}").text

    assert says(visit, imports.SAID["no_name_check"])
    assert focused(visit) == []
    assert sent.status_code == 303
    assert focused(landed) == [SAID_LANDS]
    assert says(landed, imports.SAID["no_name_check"])
    assert focused(again) == []


def test_a_visit_to_a_closed_or_missing_review_takes_no_focus_and_a_refused_press_does(
    tmp_path: pathlib.Path,
) -> None:
    """The page that says a review is finished or isn't available takes the focus only as the
    answer to a press; opened from an address, it takes none."""
    with here(open_settings(tmp_path)) as browser:
        closed = made(browser, GREEN)
        student = state_of(browser).project_state.student_id()
        store = state_of(browser).import_drafts
        store.discard_review(student, closed, version=store.snapshot(student, closed).version)
        visited = browser.get(f"/parent/imports/{closed}")
        missing = browser.get(f"/parent/imports/{'0' * 32}")
        refused = upload(browser, BLUE, form={}, to=f"/parent/imports/{closed}/images")

    landing = '<p class="problem" role="alert" tabindex="-1" autofocus>'
    assert (visited.status_code, missing.status_code, refused.status_code) == (200, 404, 409)
    assert says(visited.text, imports.SAID["closed"])
    assert says(missing.text, imports.SAID["absent"])
    assert focused(visited.text) == focused(missing.text) == []
    assert focused(refused.text) == [landing]


def test_a_page_made_before_a_newer_answer_keeps_nothing_and_says_it_is_out_of_date(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        [form] = fields_named(review_form(page), "answer:name:")
        followed(browser, send(browser, draft_id, page_post(page, "keep")))
        before = stored_answers(browser, draft_id)
        stale = page_post(page, "keep", choose={f"answer:name:{form}": "misread"})
        after = followed(browser, send(browser, draft_id, stale))
        saved = page_post(page, "save", tick=units_on(page))
        refused = followed(browser, send(browser, draft_id, saved))
        later = followed(browser, send(browser, draft_id, page_post(page, "later")))
        answers = stored_answers(browser, draft_id)

    assert answers == before
    for shown in (after, refused, later):
        assert says(shown, imports.OUT_OF_DATE)
        assert not says(shown, imports.CHANGED_IN_ANOTHER_TAB)
        assert not says(shown, "weren't kept")


def test_a_post_that_doesnt_match_its_page_keeps_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        fields = page_post(page, "keep")
        [piece, _] = fields_named(review_form(page), "answer:destination:")
        offered_not = [
            *[(n, v) for n, v in fields if n != f"answer:destination:{piece}"],
            (f"answer:destination:{piece}", '{"existing":"assignment-elsewhere-00000000"}'),
        ]
        unsigned = [(n, "9" if n == "version" else v) for n, v in fields]
        answers = [
            followed(browser, send(browser, draft_id, sent)) for sent in (offered_not, unsigned)
        ]
        stored = stored_answers(browser, draft_id)

    assert stored == {}
    assert all(says(a, imports.PRESS_SAID["keep_invalid"]) for a in answers)


def test_save_selected_homework_saves_the_ticked_groups_and_a_resend_writes_nothing_twice(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        fields = page_post(page, "save", tick=units_on(page))
        before = assignment_count(browser)
        saved = send(browser, draft_id, fields)
        made_now = assignment_count(browser)
        again = send(browser, draft_id, fields)
        made_after = assignment_count(browser)

    assert saved.status_code == 200
    assert says(saved.text, imports.counted(2, 0, 0, 0))
    assert says(saved.text, imports.SAID["closed"])
    assert made_now - before == made_after - before == 2
    assert says(again.text, imports.counted(2, 0, 0, 0))


def test_saving_one_group_leaves_the_rest_open_with_the_answers_kept(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        first, second = units_on(page)
        fields = page_post(page, "save", tick=[first])
        before = assignment_count(browser)
        after = followed(browser, send(browser, draft_id, fields))
        again = followed(browser, send(browser, draft_id, fields))
        made = assignment_count(browser)

    assert says(after, imports.counted(1, 0, 0, 1))
    assert says(after, imports.PRESS_SAID["left_open"])
    assert units_on(after) == [second]
    assert says(again, imports.counted(1, 0, 0, 1))
    assert made - before == 1


REFUSED_OPEN = (
    "The homework wasn't saved: an assignment you ticked still has a question to answer. "
    "Nothing of it was written."
)


def test_a_save_is_refused_while_a_ticked_group_still_has_a_question(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = homework_answer(
        cards=[homework_card(uncertain=[{"field": "title", "reason": "a smudge over it"}])]
    )
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        units = digests_on(page)
        before = assignment_count(browser)
        no_name = followed(browser, send(browser, draft_id, page_post(page, "save", tick=units)))
        page = her_name_answered(browser, draft_id, browser.get(f"/parent/imports/{draft_id}").text)
        unconfirmed = followed(
            browser, send(browser, draft_id, page_post(page, "save", tick=units))
        )
        made = assignment_count(browser)
        [subject] = fields_named(review_form(page), "answer:confirm:")
        confirmed = page_post(
            page, "save", tick=units, choose={f"answer:confirm:{subject}": "as_read"}
        )
        saved = send(browser, draft_id, confirmed)

    assert units
    assert units_on(page) == []
    assert says(no_name, imports.PRESS_SAID["answers_kept"])
    assert says(no_name, REFUSED_OPEN)
    assert says(unconfirmed, REFUSED_OPEN)
    assert made == before
    assert says(saved.text, imports.counted(1, 0, 0, 0))


def test_a_failure_inside_the_homework_transaction_writes_none_of_it_and_says_so(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        record = state_of(browser).project_state

        def broken(*_: object, **__: object) -> None:
            words = "synthetic note text that must stay out of the log"
            raise RuntimeError(words)

        before = assignment_count(browser)
        monkeypatch.setattr(record, "add_homework_acceptance", broken)
        with caplog.at_level(logging.WARNING):
            after = followed(
                browser, send(browser, draft_id, page_post(page, "save", tick=units_on(page)))
            )
        made = assignment_count(browser)

    assert made == before
    assert says(after, imports.PRESS_SAID["answers_kept"])
    assert says(after, imports.PRESS_SAID["failed"])
    assert "RuntimeError" in caplog.text
    assert "synthetic note text" not in caplog.text


def test_a_failure_after_the_commit_keeps_and_reports_the_save(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        drafts = state_of(browser).import_drafts
        calls: list[int] = []
        snapshot = drafts.snapshot

        def failing_after_the_first(*args: object, **kwargs: object) -> object:
            calls.append(1)
            if len(calls) > 1:
                raise DraftExpired
            return snapshot(*args, **kwargs)  # type: ignore[arg-type]

        before = assignment_count(browser)
        monkeypatch.setattr(drafts, "snapshot", failing_after_the_first)
        saved = send(browser, draft_id, page_post(page, "save", tick=units_on(page)))
        made = assignment_count(browser)

    assert saved.status_code == 200
    assert says(saved.text, imports.PRESS_SAID["incomplete"])
    assert says(saved.text, imports.counted(2, 0, 0, 0))
    assert made - before == 2


def test_a_record_changed_since_the_page_saves_nothing_and_shows_what_saving_does_now(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rival = Assignment(
        assignment_id="assignment-sentence-composing-dddd3333",
        course="English 8",
        title="Sentence Composing",
        due_date=date(2026, 10, 9),
        dependencies=[],
        reported_submission_status="not_reported",
        assigned_on=None,
        kind=AssignmentKind.HOMEWORK,
    )
    answer = homework_answer(cards=[homework_card()])
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        page = her_name_answered(browser, draft_id, page)
        state_of(browser).project_state.upsert_assignments([rival])
        before = assignment_count(browser)
        after = followed(
            browser, send(browser, draft_id, page_post(page, "save", tick=units_on(page)))
        )
        made = assignment_count(browser)

    assert made == before
    assert says(after, imports.PRESS_SAID["changed_by_answers"])


def test_save_for_later_keeps_the_typed_corrections_and_a_page_behind_keeps_its_typing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = homework_answer(
        cards=[homework_card(uncertain=[{"field": "title", "reason": "a smudge over it"}])]
    )
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        field = box_subject(page, "title")
        typed = {f"correct:{field}": "Sentence Combining"}
        later = followed(browser, send(browser, draft_id, page_post(page, "later", typed=typed)))
        student = state_of(browser).project_state.student_id()
        progress = state_of(browser).import_drafts.snapshot(student, draft_id).progress
        one_tab, other_tab = (browser.get(f"/parent/imports/{draft_id}").text for _ in range(2))
        followed(browser, send(browser, draft_id, page_post(one_tab, "later")))
        behind = send(
            browser,
            draft_id,
            page_post(other_tab, "later", typed={f"correct:{field}": "Sentence Craft"}),
        )
        kept = state_of(browser).import_drafts.snapshot(student, draft_id).progress

    assert says(later, imports.PRESS_SAID["later"])
    assert "Saved for later at " in later
    assert json.loads(str(progress)) == {
        "v": 4,
        "corrected": {json.loads(field)[0]: {"title": "Sentence Combining"}},
        "selected": [],
    }
    assert 'value="Sentence Combining"' in later
    assert "A new Sentence Combining (1)" in later
    assert behind.status_code == 409
    assert says(behind.text, imports.PRESS_SAID["answers_kept"])
    assert says(behind.text, imports.PRESS_SAID["later_behind"])
    assert 'value="Sentence Craft"' in behind.text
    assert kept == progress


def test_an_offer_is_kept_whole_and_part_of_one_is_an_ordinary_answer(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monday, tuesday = split_answers()
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, tuesday))
        [row] = fields_named(review_form(page), "answer:split:")
        keep(browser, draft_id, ("split", row, json.dumps("split")))
        split = browser.get(f"/parent/imports/{draft_id}").text
        days = fields_named(review_form(split), "answer:destination:")
        keep(browser, draft_id, *(("destination", d, '{"new":1}') for d in days))
        refused = browser.get(f"/parent/imports/{draft_id}").text
        offered = re.findall(
            r'<form method="post" action="[^"]+/review" id="offer-[^"]+">.*?</form>', refused, re.S
        )
        closure = [
            (html.unescape(n), html.unescape(v))
            for n, v in re.findall(r'name="([^"]+)" value="([^"]*)"', offered[-1])
        ]
        whole = followed(browser, send(browser, draft_id, closure))
        moved = stored_answers(browser, draft_id)
        part = page_post(
            whole, "keep", choose={f"answer:destination:{days[0]}": moved[("destination", days[1])]}
        )
        followed(browser, send(browser, draft_id, part))
        partly = stored_answers(browser, draft_id)
        forged = [(n, '{"new":7}' if n.startswith("answer:") else v) for n, v in closure]
        refused_again = followed(browser, send(browser, draft_id, forged))

    assert says(whole, imports.PRESS_SAID["kept"])
    assert sorted(moved[("destination", d)] for d in days) == ['{"new":2}', '{"new":3}']
    assert partly[("destination", days[0])] == moved[("destination", days[1])]
    assert partly[("destination", days[1])] == moved[("destination", days[1])]
    assert says(refused_again, imports.PRESS_SAID["keep_invalid"])


def test_find_under_another_name_lists_saved_work_by_its_words_and_keeps_the_one_chosen(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = Assignment(
        assignment_id="assignment-grammar-practice-eeee4444",
        course="English 8",
        title="Grammar Practice",
        due_date=date(2026, 10, 8),
        dependencies=[],
        reported_submission_status="not_reported",
        assigned_on=None,
        kind=AssignmentKind.HOMEWORK,
    )
    with here(open_settings(tmp_path)) as browser:
        state_of(browser).project_state.upsert_assignments([other])
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        piece = fields_named(review_form(page), "answer:destination:")[0]
        found = browser.post(
            f"/parent/imports/{draft_id}/find", data={"piece": piece, "words": "grammar"}
        )
        nothing = browser.post(
            f"/parent/imports/{draft_id}/find", data={"piece": piece, "words": "zzzz"}
        )
        gone = browser.post(f"/parent/imports/{draft_id}/find", data={"piece": "x", "words": "a"})
        chosen = re.search(
            r'<form method="post" action="[^"]+/review" class="choice">.*?</form>', found.text, re.S
        )
        assert chosen is not None
        fields = [
            (html.unescape(n), html.unescape(v))
            for n, v in re.findall(r'name="([^"]+)" value="([^"]*)"', chosen.group(0))
        ]
        after = followed(browser, send(browser, draft_id, fields))
        answers = stored_answers(browser, draft_id)

    assert found.status_code == 200
    assert says(found.text, "English 8: Grammar Practice, due Thu 10/8")
    assert says(nothing.text, imports.FIND_SAID["nothing"])
    assert gone.status_code == 404
    assert answers[("destination", piece)] == json.dumps(
        {"found": other.assignment_id}, separators=(",", ":")
    )
    assert "Found under another name: Grammar Practice, due Thu 10/8" in html.unescape(after)


def test_a_row_s_type_is_chosen_with_the_paste_page_s_words_and_can_ask_the_kind_question(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = Assignment(
        assignment_id="assignment-sentence-composing-ffff5555",
        course="English 8",
        title="Sentence Composing",
        due_date=date(2026, 10, 8),
        dependencies=[],
        reported_submission_status="not_reported",
        assigned_on=date(2026, 10, 5),
        kind=AssignmentKind.HOMEWORK,
    )
    answer = homework_answer(cards=[homework_card()])
    with here(open_settings(tmp_path)) as browser:
        state_of(browser).project_state.upsert_assignments([saved])
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        [piece] = fields_named(review_form(page), "answer:destination:")
        [row] = fields_named(review_form(page), "answer:kind:")
        chosen = page_post(
            page,
            "keep",
            choose={
                f"answer:destination:{piece}": json.dumps(
                    {"existing": saved.assignment_id}, separators=(",", ":")
                ),
                f"answer:kind:{row}": "TASK",
            },
        )
        after = followed(browser, send(browser, draft_id, chosen))

    form = html.unescape(review_form(page))
    assert (
        'Homework or task?<span class="visually-hidden"> for Sentence Composing (screenshot 1, '
        "card 1)</span>"
    ) in form
    assert screenshot_page.SAID["type_helper"] in form
    asked = re.search(
        r'<fieldset class="choice" id="(ask-\d+)"[^>]*>\s*<legend>Homework or task for '
        r"Sentence Composing, due Thu 10/8",
        html.unescape(after),
    )
    assert asked is not None
    assert "homework or task" in html.unescape(after)
    assert (
        f'<a href="#{asked.group(1)}">Answer \u201cHomework or task for Sentence Composing, due '
        "Thu 10/8"
    ) in html.unescape(after)


def test_the_name_answered_in_the_same_press_is_kept_first_and_read_by_the_save(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = homework_answer(cards=[homework_card()])
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        [form] = fields_named(review_form(page), "answer:name:")
        fields = page_post(
            page, "save", tick=digests_on(page), choose={f"answer:name:{form}": "hers"}
        )
        before = assignment_count(browser)
        saved = send(browser, draft_id, fields)
        made = assignment_count(browser)
        row = (
            state_of(browser)
            .project_state._connection.execute(
                "SELECT identity_status, identity_answer FROM grade_acceptances "
                "WHERE kind = 'homework_screenshot'"
            )
            .fetchone()
        )

    assert says(saved.text, imports.counted(1, 0, 0, 0))
    assert made - before == 1
    assert tuple(row) == ("first_use", "hers")


def test_a_group_another_tab_saved_is_already_saved_and_nothing_is_written_twice(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        followed(browser, send(browser, draft_id, page_post(page, "keep")))
        one_tab, other_tab = (browser.get(f"/parent/imports/{draft_id}").text for _ in range(2))
        first = units_on(one_tab)[0]
        followed(browser, send(browser, draft_id, page_post(one_tab, "save", tick=[first])))
        before = assignment_count(browser)
        again = followed(
            browser, send(browser, draft_id, page_post(other_tab, "save", tick=[first]))
        )
        made = assignment_count(browser)

    assert says(again, imports.PRESS_SAID["already"])
    assert made == before


def test_a_save_with_no_group_ticked_keeps_the_answers_and_saves_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        before = assignment_count(browser)
        after = followed(browser, send(browser, draft_id, page_post(page, "save")))
        made = assignment_count(browser)
        answers = stored_answers(browser, draft_id)

    assert says(after, imports.PRESS_SAID["answers_kept"])
    assert says(after, imports.PRESS_SAID["none_chosen"])
    assert made == before
    assert len(answers) == 2


def test_the_confirmable_fields_are_the_ones_the_page_names() -> None:
    assert set(screenshot_page.FIELD_NAMES) == CONFIRMABLE


def name_forms(browser: TestClient) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in state_of(browser).project_state._connection.execute(
            "SELECT * FROM grade_name_forms"
        )
    ]


def test_yes_this_is_her_name_adds_its_keyed_form_with_the_save_and_never_the_name(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = homework_answer(cards=[homework_card()])
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        before = name_forms(browser)
        page = her_name_answered(browser, draft_id, page)
        kept_only = name_forms(browser)
        send(browser, draft_id, page_post(page, "save", tick=units_on(page)))
        after = name_forms(browser)
        _, again = read_review(browser, monkeypatch, (RED, answer))

    assert before == kept_only == []
    assert len(after) == 1
    assert "Wren" not in repr(after)
    assert "names Wren" not in again
    assert 'name="answer:name:' not in again


def test_the_name_was_misread_saves_and_adds_no_form(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = homework_answer(cards=[homework_card()])
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        [form] = fields_named(review_form(page), "answer:name:")
        keep(browser, draft_id, ("name", form, "misread"))
        page = browser.get(f"/parent/imports/{draft_id}").text
        saved = send(browser, draft_id, page_post(page, "save", tick=units_on(page)))
        forms = name_forms(browser)

    assert says(saved.text, imports.counted(1, 0, 0, 0))
    assert forms == []


def test_a_save_deletes_its_rows_confirmations_and_keeps_the_open_rows(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marked = [{"field": "title", "reason": "a smudge over it"}]
    answer = homework_answer(
        cards=[
            homework_card(uncertain=marked),
            homework_card(
                course="Science 8",
                title="Chains Reading Guide 6",
                uncertain=marked,
                y_top=360,
                y_bottom=430,
            ),
        ]
    )
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        page = her_name_answered(browser, draft_id, page)
        subjects = fields_named(review_form(page), "answer:confirm:")
        keep(browser, draft_id, *(("confirm", s, "as_read") for s in subjects))
        page = browser.get(f"/parent/imports/{draft_id}").text
        first, _ = units_on(page)
        before = stored_answers(browser, draft_id)
        followed(browser, send(browser, draft_id, page_post(page, "save", tick=[first])))
        after = stored_answers(browser, draft_id)

    confirmations = sorted(s for k, s in before if k == "confirm")
    assert len(confirmations) == 2
    assert len([s for k, s in after if k == "confirm"]) == 1
    assert [k for k, _ in after if k == "name"] == ["name"]
    assert len([k for k, _ in after if k == "destination"]) == 1


def test_saved_progress_keeps_the_groups_ticked_and_ticks_only_those_still_open(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        first, second = units_on(page)
        unticked = units_ticked(page)
        later = followed(browser, send(browser, draft_id, page_post(page, "later", tick=[first])))
        student = state_of(browser).project_state.student_id()
        progress = state_of(browser).import_drafts.snapshot(student, draft_id).progress
        followed(browser, send(browser, draft_id, page_post(later, "save", tick=[first])))
        after_save = browser.get(f"/parent/imports/{draft_id}").text
        stale = page_post(after_save, "later", tick=[second])
        stale += [("unit", "a-unit-no-page-showed")]
        refused = followed(browser, send(browser, draft_id, stale))

    assert unticked == [first, second]
    assert json.loads(str(progress))["selected"] == [first]
    assert units_ticked(later) == [first]
    assert units_on(after_save) == [second]
    assert units_ticked(after_save) == []
    assert says(refused, imports.PRESS_SAID["later_invalid"])


def test_with_no_saved_progress_the_ready_groups_are_ticked_and_an_unready_one_isnt(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = homework_answer(
        cards=[
            homework_card(uncertain=[{"field": "title", "reason": "a smudge over it"}]),
            homework_card(
                course="Science 8", title="Chains Reading Guide 6", y_top=360, y_bottom=430
            ),
        ]
    )
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        page = her_name_answered(browser, draft_id, page)
        student = state_of(browser).project_state.student_id()
        progress = state_of(browser).import_drafts.snapshot(student, draft_id).progress
        answers = stored_answers(browser, draft_id)

    ready = units_on(page)
    assert progress is None
    assert len(digests_on(page)) == 2
    assert len(ready) == 1
    assert units_ticked(page) == ready
    assert [kind for kind, _ in answers] == ["name"]


def units_ticked(page: str) -> list[str]:
    return [html.unescape(u) for u in re.findall(r'name="unit" value="([^"]+)" checked', page)]


# ------------------------------------------------------------------ setting aside and discarding


def outside(page: str, form_id: str) -> tuple[str, list[tuple[str, str]]]:
    """A small form of the review page, by its id: its action and its fields."""
    found = re.search(
        rf'<form method="post" action="([^"]+)" id="{re.escape(form_id)}">(.*?)</form>',
        page,
        re.S,
    )
    assert found is not None, form_id
    fields = [
        (html.unescape(n), html.unescape(v))
        for n, v in re.findall(r'name="([^"]+)" value="([^"]*)"', found.group(2))
    ]
    return found.group(1), fields


def press_outside(browser: TestClient, page: str, form_id: str) -> Answer:
    action, fields = outside(page, form_id)
    return browser.post(
        action,
        content=as_a_browser_sends(fields),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )


def row_states(browser: TestClient, draft_id: str) -> dict[str, str]:
    student = state_of(browser).project_state.student_id()
    snapshot = state_of(browser).import_drafts.snapshot(student, draft_id)
    return {row.key: row.state for row in snapshot.rows}


def closed(browser: TestClient, draft_id: str) -> bool:
    student = state_of(browser).project_state.student_id()
    return state_of(browser).import_drafts.snapshot(student, draft_id).closed_at is not None


def test_leave_this_group_unsaved_sets_its_rows_aside_and_the_rest_stays_open(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        after = followed(browser, press_outside(browser, page, "leave-1"))
        states = row_states(browser, draft_id)
        still_open = not closed(browser, draft_id)

    assert len(units_on(page)) == 2
    assert says(after, imports.AFTER_SAID["set_aside"])
    assert sorted(states.values()) == ["discarded", "open"]
    assert len(units_on(after)) == 1
    assert still_open


def test_leaving_the_last_group_unsaved_finishes_the_review(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = homework_answer(cards=[homework_card()])
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        left = press_outside(browser, page, "leave-1")
        done = closed(browser, draft_id)

    assert left.status_code == 200
    assert says(left.text, imports.AFTER_SAID["set_aside"])
    assert says(left.text, imports.AFTER_SAID["nothing_saved"])
    assert done


def test_a_row_whose_days_differ_can_be_left_unsaved_before_its_question_is_answered(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monday, tuesday = split_answers()
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, tuesday))
        left = press_outside(browser, page, "leave-split-1")
        done = closed(browser, draft_id)

    assert says(left.text, imports.AFTER_SAID["set_aside"])
    assert done


def test_finish_without_the_rest_closes_the_review_after_a_save(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        first, _ = units_on(page)
        after = followed(browser, send(browser, draft_id, page_post(page, "save", tick=[first])))
        finished = press_outside(browser, after, "finish")
        done = closed(browser, draft_id)

    assert finished.status_code == 200
    assert says(finished.text, imports.AFTER_SAID["finished"])
    assert done


def test_discard_the_review_removes_it_and_says_nothing_was_saved(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        before = assignment_count(browser)
        gone = press_outside(browser, page, "discard")
        done = closed(browser, draft_id)
        made = assignment_count(browser)
        again = browser.get(f"/parent/imports/{draft_id}")

    assert gone.status_code == 200
    assert says(gone.text, imports.AFTER_SAID["discarded"])
    assert says(gone.text, imports.AFTER_SAID["nothing_saved"])
    assert done
    assert made == before
    assert says(again.text, imports.SAID["closed"])


def test_not_hers_discards_a_review_nothing_was_saved_from(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        offered = 'form="not-hers"' in page
        gone = press_outside(browser, page, "not-hers")
        done = closed(browser, draft_id)

    assert offered
    assert gone.status_code == 200
    assert says(gone.text, imports.AFTER_SAID["not_hers"])
    assert done


def test_not_hers_is_not_offered_after_a_save_and_a_page_from_before_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        [form] = fields_named(review_form(page), "answer:name:")
        keep(browser, draft_id, ("name", form, "misread"))
        page = browser.get(f"/parent/imports/{draft_id}").text
        first, _ = units_on(page)
        after = followed(browser, send(browser, draft_id, page_post(page, "save", tick=[first])))
        refused = followed(browser, press_outside(browser, page, "not-hers"))
        still_open = not closed(browser, draft_id)

    assert 'form="not-hers"' in page
    assert 'form="not-hers"' not in after
    assert says(refused, imports.AFTER_SAID["not_hers_after_a_save"])
    assert still_open


def test_a_small_form_from_a_page_before_a_change_changes_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        followed(browser, send(browser, draft_id, page_post(page, "keep")))
        left = followed(browser, press_outside(browser, page, "leave-1"))
        finished = followed(browser, press_outside(browser, page, "finish"))
        discarded = followed(browser, press_outside(browser, page, "discard"))
        states = row_states(browser, draft_id)
        action, fields = outside(browser.get(f"/parent/imports/{draft_id}").text, "leave-1")
        forged = [(n, '["0:elsewhere"]' if n == "keys" else v) for n, v in fields]
        unchecked = followed(
            browser,
            browser.post(
                action,
                content=urlencode(forged).encode(),
                headers={"content-type": "application/x-www-form-urlencoded"},
            ),
        )

    assert says(left, imports.CHANGED_IN_ANOTHER_TAB)
    assert says(left, imports.AFTER_SAID["nothing_set_aside"])
    assert says(finished, imports.AFTER_SAID["nothing_set_aside"])
    assert says(discarded, imports.AFTER_SAID["nothing_discarded"])
    assert set(states.values()) == {"open"}
    assert says(unchecked, imports.AFTER_SAID["unchecked"])


def test_the_disclosure_stands_directly_above_read(tmp_path: pathlib.Path) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        page = browser.get(f"/parent/imports/{draft_id}").text

    disclosed = page.index(str(escape(imports.DISCLOSED)))
    read = page.index(f'action="/parent/imports/{draft_id}/read"')
    assert disclosed < read
    assert "<p" not in page[disclosed:read].split("</p>", 1)[1]


# ------------------------------------------------------------------ what each card was read as


def unreadable_answer() -> dict[str, object]:
    """One card read whole, one whose title couldn't be read, and one with no date anywhere."""
    return homework_answer(
        headings=[],
        cards=[
            homework_card(),
            homework_card(title=None, y_top=360, y_bottom=430),
            homework_card(
                badge="DUE",
                course="Science 8",
                title="Vocab 4",
                instruction=None,
                footer=None,
                y_top=450,
                y_bottom=520,
            ),
        ],
    )


def test_each_card_s_fold_holds_what_was_read_for_it_whole(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each card's "Correct what was read" holds every field as read, beside its destination
    choices: its row's class, title and instruction texts, and its own dates, empty where none
    was read."""
    with here(open_settings(tmp_path)) as browser:
        _, page = read_review(browser, monkeypatch, (RED, homework_answer()))

    form = review_form(page)
    held = {
        field: [
            str((box_of(form, f"correct:{s}") or ("", ""))[1])
            for s in fields_named(form, "correct:")
            if json.loads(s)[1] == field
        ]
        for field in ("course", "title", "instruction:0", "due", "assigned")
    }
    assert held == {
        "course": ["English 8", "Science 8"],
        "title": ["Sentence Composing", "Chains Reading Guide 6"],
        "instruction:0": ["Complete pages 12 to 14."],
        "due": ["2026-10-08", "2026-10-05"],
        "assigned": ["2026-10-05", ""],
    }


def test_a_found_row_chosen_for_a_card_shows_the_same_facts_as_every_other_entry(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = Assignment(
        assignment_id="assignment-grammar-practice-gggg6666",
        course="English 8",
        title="Grammar Practice",
        due_date=date(2026, 10, 9),
        dependencies=[],
        reported_submission_status="not_reported",
        assigned_on=date(2026, 10, 2),
        kind=AssignmentKind.HOMEWORK,
    )
    answer = homework_answer(cards=[homework_card()])
    with here(open_settings(tmp_path)) as browser:
        state_of(browser).project_state.upsert_assignments([other])
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        [piece] = fields_named(review_form(page), "answer:destination:")
        found = json.dumps({"found": other.assignment_id}, separators=(",", ":"))
        keep(browser, draft_id, ("destination", piece, found))
        after = html.unescape(review_form(browser.get(f"/parent/imports/{draft_id}").text))

    chosen = re.search(
        r"Found under another name: Grammar Practice[^<]*(<span.*?)</label>", after, re.S
    )
    assert chosen is not None
    facts = chosen.group(1)
    assert "Doesn't have this card's words." in facts
    assert "Due Fri 10/9: this card says Thu 10/8." in facts
    assert "Assigned Fri 10/2: this card says Mon 10/5." in facts


def test_cards_that_couldn_t_be_read_are_listed_with_their_reason_text_and_place(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        _, page = read_review(browser, monkeypatch, (RED, unreadable_answer()))

    shown = html.unescape(page)
    assert "Couldn't read" in shown
    assert "Screenshot 1: Text Blossom couldn't read" in shown
    assert "Look at the screenshot and enter it by hand if it's homework." in shown
    assert "Card 2: English 8 · Complete pages 12 to 14. · Due (10/8/2026)" in shown
    assert "Screenshot 1: Homework read, but its date couldn't be told" in shown
    assert "No date was on the card or in a heading above it in this screenshot." in shown
    assert "Card 3: Science 8 · Vocab 4" in shown
    assert shown.count('form="leave-item-') == 2


def test_a_card_that_couldn_t_be_read_can_be_set_aside_and_the_review_then_finishes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, unreadable_answer()))
        page = her_name_answered(browser, draft_id, page)
        stale = page
        left = followed(browser, press_outside(browser, page, "leave-item-1"))
        refused = followed(browser, press_outside(browser, stale, "leave-item-2"))
        states = row_states(browser, draft_id)
        [remaining] = re.findall(r'form="(leave-item-\d+)"', left)
        left_again = press_outside(browser, left, remaining)
        saved = send(
            browser,
            draft_id,
            page_post(browser.get(f"/parent/imports/{draft_id}").text, "save", tick=units_on(left)),
        )
        done = closed(browser, draft_id)

    assert says(left, imports.AFTER_SAID["set_aside"])
    assert says(refused, imports.CHANGED_IN_ANOTHER_TAB)
    assert sorted(states.values()) == ["discarded", "open", "open"]
    assert left_again.status_code == 303
    assert says(saved.text, imports.counted(1, 0, 0, 0))
    assert done


def test_finish_without_the_rest_sets_aside_the_cards_that_couldn_t_be_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, unreadable_answer()))
        before = row_states(browser, draft_id)
        finished = press_outside(browser, page, "finish")
        done = closed(browser, draft_id)

    assert sorted(before.values()) == ["open", "open", "open"]
    assert says(finished.text, imports.AFTER_SAID["nothing_saved"])
    assert done


def test_a_review_left_with_only_cards_that_couldn_t_be_read_still_shows_them(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, unreadable_answer()))
        page = her_name_answered(browser, draft_id, page)
        send(browser, draft_id, page_post(page, "save", tick=units_on(page)))
        after = browser.get(f"/parent/imports/{draft_id}").text
        still_open = not closed(browser, draft_id)

    assert still_open
    assert "Couldn't read" in html.unescape(after)
    assert after.count('form="leave-item-') == 2
    assert 'form="finish"' in after


# ------------------------------------------------------------------ typing kept in the form


def unsure_answer() -> dict[str, object]:
    return homework_answer(
        cards=[homework_card(uncertain=[{"field": "title", "reason": "a smudge over it"}])]
    )


def test_keep_these_answers_answers_with_the_review_and_the_typing_still_in_the_form(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, unsure_answer()))
        field = box_subject(page, "title")
        typed = {f"correct:{field}": "Sentence Combining"}
        kept = send(browser, draft_id, page_post(page, "keep", typed=typed))
        answers = stored_answers(browser, draft_id)
        again = send(browser, draft_id, page_post(kept.text, "keep"))

    form = review_form(kept.text)
    [piece] = [s for k, s in answers if k == "destination"]
    assert kept.status_code == 200
    assert says(kept.text, imports.PRESS_SAID["kept"])
    assert 'value="Sentence Combining"' in form
    assert (f"answer:destination:{piece}", "{&#34;new&#34;:1}") in checked_values(form)
    assert "A new Sentence Combining (1)" in form
    assert screenshot_page.SAID["preselected"] not in form
    assert again.status_code == 200
    assert 'value="Sentence Combining"' in review_form(again.text)
    assert screenshot_page.SAID["preselected"] not in review_form(again.text)


def test_a_save_that_saves_nothing_answers_with_the_typing_in_the_form(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, unsure_answer()))
        field = box_subject(page, "title")
        typed = {f"correct:{field}": "Sentence Combining"}
        units = digests_on(page)
        refused = send(browser, draft_id, page_post(page, "save", tick=units, typed=typed))

    assert refused.status_code == 409
    assert says(refused.text, imports.PRESS_SAID["changed_by_answers"])
    assert 'value="Sentence Combining"' in review_form(refused.text)
    assert "A new Sentence Combining (1)" in review_form(refused.text)


def test_a_save_refused_while_a_group_is_open_keeps_the_ticks_it_sent(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        first, second = units_on(page)
        none_ticked = send(browser, draft_id, page_post(page, "save"))
        later = followed(browser, send(browser, draft_id, page_post(page, "later", tick=[first])))

    assert none_ticked.status_code == 200
    assert says(none_ticked.text, imports.PRESS_SAID["none_chosen"])
    assert units_ticked(none_ticked.text) == []
    assert units_ticked(later) == [first]


def test_a_page_from_before_a_change_answers_with_its_typing_in_the_current_review(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, unsure_answer()))
        field = box_subject(page, "title")
        send(browser, draft_id, page_post(page, "keep"))
        typed = {f"correct:{field}": "Sentence Craft"}
        stale = send(browser, draft_id, page_post(page, "keep", typed=typed))

    assert stale.status_code == 409
    assert not says(stale.text, imports.CHANGED_IN_ANOTHER_TAB)
    assert says(stale.text, imports.OUT_OF_DATE)
    assert 'value="Sentence Craft"' in review_form(stale.text)


# ------------------------------------------------------------------ saved progress


def test_saved_progress_is_kept_in_its_one_shape_and_any_other_reads_as_none(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, unsure_answer()))
        page = her_name_answered(browser, draft_id, page)
        field = box_subject(page, "title")
        key = json.loads(field)[0]
        dates = f"date:{key}"
        typed = {f"correct:{field}": "Sentence Combining"}
        send(browser, draft_id, page_post(page, "later", typed=typed))
        student = state_of(browser).project_state.student_id()
        drafts = state_of(browser).import_drafts
        written = drafts.snapshot(student, draft_id).progress
        title = {"title": "Sentence Combining"}
        shown = []
        for other in (
            {"corrections": {key: title}, "selected": []},
            {"v": 2, "corrected": {key: title}, "selected": []},
            {"v": 3, "corrected": {key: title}, "selected": [], "x": 1},
            {"v": 3, "corrected": {key: {"shade": "red"}}, "selected": []},
            {"v": 3, "corrected": {key: title}, "selected": [7]},
            {"v": 3, "corrected": {key: {**title, "instruction:0": "x"}}, "selected": []},
            {"v": 3, "corrected": {key: {**title, "instruction": None}}, "selected": []},
            {"v": 4, "corrected": {key: {**title, "footer": "2026-10-09"}}, "selected": []},
            {"v": 4, "corrected": {key: {**title, "instruction": "x"}}, "selected": []},
            {"v": 4, "corrected": {key: {**title, "course": None}}, "selected": []},
            {"v": 4, "corrected": {key: title, dates: {"due": None}}, "selected": []},
            {"v": 4, "corrected": {key: title, dates: {"course": "x"}}, "selected": []},
            {"v": 4, "corrected": {key: {**title, "due": "2026-10-09"}}, "selected": []},
            {"v": 4, "corrected": {key: {**title, "instruction:01": "x"}}, "selected": []},
            {"v": 4, "corrected": {key: {**title, "instruction:0": "   "}}, "selected": []},
            {"v": True, "corrected": {key: title}, "selected": []},
        ):
            snapshot = drafts.snapshot(student, draft_id)
            drafts.save_progress(
                student,
                draft_id,
                revision=snapshot.progress_revision,
                progress=json.dumps(other),
            )
            shown.append(browser.get(f"/parent/imports/{draft_id}").text)

    assert json.loads(str(written)) == {
        "v": 4,
        "corrected": {key: {"title": "Sentence Combining"}},
        "selected": [],
    }
    for one in shown:
        assert 'value="Sentence Combining"' not in one
        assert "A new Sentence Composing (1)" in one


SCREENSHOT_LIST_RULE = ".screenshot-list {\n  list-style: none;\n  margin: 0;\n  padding: 0;\n}\n"
"""The review's screenshots, pinned whole: a list without markers, as the app's other lists."""


def test_the_review_s_screenshots_are_a_list_without_markers(tmp_path: pathlib.Path) -> None:
    with here(open_settings(tmp_path)) as browser:
        page = browser.get(f"/parent/imports/{made(browser, RED)}").text

    assert '<ul class="screenshot-list">' in page
    assert STYLESHEET.read_text(encoding="utf-8").count(SCREENSHOT_LIST_RULE) == 1


EIGHT = tuple(png(color=(30 * n, 90, 120)) for n in range(1, 9))
"""Eight screenshots, each a color of its own."""
FOLD = re.compile(
    r'<details class="(?:panel )?screenshot-fold"( open)?>\s*'
    r"<summary>(.*?)</summary>(.*?)</details>",
    re.S,
)
SHOWN_SCREENSHOT = re.compile(r"<li>\s*<a [^>]*><img [^>]*alt=\"(Screenshot \d+)\"")


def screenshots_in(markup: str) -> list[str]:
    """The screenshots a piece of the review page lists, by their place, in order."""
    return SHOWN_SCREENSHOT.findall(markup)


def beside_the_fold(page: str, fold: re.Match[str]) -> str:
    """The review page without the fold of screenshots read."""
    return page[: fold.start()] + page[fold.end() :]


FIRST_OPEN = re.compile(r'<p class="jump"><a href="#([a-z]+-\d+)">First question to answer</a></p>')


def eight_cards() -> tuple[tuple[bytes, object], ...]:
    """Eight screenshots, each read as one card of a title of its own."""
    return tuple(
        (image, homework_answer(cards=[homework_card(title=f"Practice {n}")]))
        for n, image in enumerate(EIGHT, 1)
    )


def fieldsets_of(page: str) -> list[tuple[str, str]]:
    """Each fieldset of the review form, in page order, as (its id, its markup)."""
    return re.findall(r'<fieldset class="choice" id="([^"]+)"[^>]*>(.*?)</fieldset>', page, re.S)


def first_open(page: str) -> str:
    """The markup of the fieldset the first-question link names, after checking that every
    fieldset before it has an answer chosen and that it has none."""
    link = FIRST_OPEN.search(page)
    assert link is not None
    shown = fieldsets_of(page)
    ids = [found for found, _ in shown]
    assert len(ids) == len(set(ids))
    assert page.count(f'id="{link.group(1)}"') == 1
    place = ids.index(link.group(1))
    assert all(" checked" in markup for _, markup in shown[:place])
    assert " checked" not in shown[place][1]
    return shown[place][1]


def test_read_screenshots_fold_away_under_a_way_to_the_first_question_still_open(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, *eight_cards())
        answered = her_name_answered(browser, draft_id, page)

    for shown in (page, answered):
        fold = FOLD.search(shown)
        assert fold is not None
        assert fold.group(1) is None
        assert fold.group(2) == "8 screenshots read"
        assert screenshots_in(fold.group(3)) == [f"Screenshot {n}" for n in range(1, 9)]
        assert screenshots_in(beside_the_fold(shown, fold)) == []
        # Opened or not, the fold follows the whole review form, its questions first.
        form_end = shown.index("</form>", shown.index('id="review-form"'))
        assert shown.index('id="saving-heading"') < form_end < fold.start()
        assert 'form="discard"' in shown[: fold.start()]
    link = FIRST_OPEN.search(page)
    assert link is not None
    assert link.start() < page.index('id="saving-heading"')
    assert "answer:name:" in first_open(page)
    # A Type left unchosen saves as its line says, so it is no question still to answer.
    assert FIRST_OPEN.search(answered) is None
    assert declared_for('.import-review .choice[tabindex="-1"]:focus') == [ARRIVAL_CUE]


def test_a_correction_refused_as_too_long_is_the_first_question_still_to_answer(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Save review for later with an instruction past its field's length: the review keeps the
    typing in its box and refuses it, and the first-question link goes to that box's question,
    whatever else is answered."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, boxes = unsure_review(browser, monkeypatch, "instruction", "title")
        [kind] = fields_named(review_form(page), "answer:kind:")
        keep(browser, draft_id, ("kind", kind, "HOMEWORK"))
        page = browser.get(f"/parent/imports/{draft_id}").text
        typed = {boxes["instruction"]: LINES_OF_A + "a", boxes["title"]: "Typed title"}
        refused = followed(browser, send(browser, draft_id, page_post(page, "later", typed=typed)))

    link = FIRST_OPEN.search(refused)
    box = f'name="{escape(boxes["instruction"])}"'
    [holding] = [
        (anchor, markup)
        for anchor, markup in re.findall(
            r'<details class="steps" id="([^"]+)"[^>]*>(.*?)</details>', refused, re.S
        )
        if box in markup
    ]
    assert 'aria-invalid="true"' in holding[1]
    assert re.search(rf'<details class="steps" id="{holding[0]}" tabindex="-1" open>', refused)
    assert link is not None
    assert link.group(1) == holding[0]


def test_screenshots_still_to_read_or_read_as_something_else_stay_in_view_with_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Reader(monkeypatch)
    reader.answers[BLUE] = ScreenshotAnswer(None, "not_json", 10, 5)
    reader.answers[GREEN] = ScreenshotAnswer(homework_answer(image_kind="unsupported"), None, 9, 4)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED, BLUE, GREEN)
        unread = browser.get(f"/parent/imports/{draft_id}").text
        mixed = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(unread)).text

    fold = FOLD.search(mixed)
    assert "screenshot-fold" not in unread
    assert screenshots_in(unread) == ["Screenshot 1", "Screenshot 2", "Screenshot 3"]
    assert fold is not None
    assert fold.group(1) is None
    assert fold.group(2) == "1 screenshot read"
    assert screenshots_in(fold.group(3)) == ["Screenshot 1"]
    above = mixed[: fold.start()]
    assert screenshots_in(beside_the_fold(mixed, fold)) == ["Screenshot 2", "Screenshot 3"]
    assert screenshots_in(above) == ["Screenshot 2", "Screenshot 3"]
    assert says(above, imports.FAILED["not_json"])
    assert says(above, imports.LABELS["wrong_kind"])
    assert f'action="/parent/imports/{draft_id}/read"' in above
    assert says(above, imports.DISCLOSED)
    assert above.index('id="saving-heading"') < above.index("</form>", above.index("review-form"))


def eight_read(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    """Eight screenshots, every one read as her homework list."""
    return read_review(browser, monkeypatch, *eight_cards())[1]


def some_unread(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    """One screenshot read, one whose reading failed and one read as something else."""
    reader = Reader(monkeypatch)
    reader.answers[BLUE] = ScreenshotAnswer(None, "not_json", 10, 5)
    reader.answers[GREEN] = ScreenshotAnswer(homework_answer(image_kind="unsupported"), None, 9, 4)
    draft_id = made(browser, RED, BLUE, GREEN)
    unread = browser.get(f"/parent/imports/{draft_id}").text
    return browser.post(f"/parent/imports/{draft_id}/read", data=read_form(unread)).text


def partly_saved(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    """Two screenshots read and one group saved: the screenshots are fixed."""
    lab = homework_answer(cards=[homework_card(course="Science 8", title="Lab Report")])
    draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()), (BLUE, lab))
    page = her_name_answered(browser, draft_id, page)
    tick = units_on(page)[:1]
    return followed(browser, send(browser, draft_id, page_post(page, "save", tick=tick)))


def none_read(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    """Two screenshots not read yet."""
    return browser.get(f"/parent/imports/{made(browser, RED, BLUE)}").text


SCREENSHOT_STATES: Final = {
    "every screenshot read": eight_read,
    "some still to read": some_unread,
    "fixed after a save": partly_saved,
    "nothing read yet": none_read,
}
SCREENSHOTS_HEADING: Final = '<h2 id="screenshots-heading">Screenshots</h2>'


@pytest.mark.parametrize("state", list(SCREENSHOT_STATES))
def test_one_screenshots_heading_leads_the_screenshots_read_wherever_they_are(
    state: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page's headings name Screenshots once. When the screenshots read follow the review
    form, that heading leads their section, so heading navigation reaches them; before the
    review asks anything, it leads the screenshots at the top."""
    with here(open_settings(tmp_path)) as browser:
        page = SCREENSHOT_STATES[state](browser, monkeypatch)

    headings = [re.sub(r"<[^>]+>", "", h) for h in re.findall(r"<h2[^>]*>(.*?)</h2>", page, re.S)]
    assert headings.count("Screenshots") == 1
    assert page.count(SCREENSHOTS_HEADING) == 1
    heading = page.index(SCREENSHOTS_HEADING)
    section = page.rfind("<section", 0, heading)
    assert 'aria-labelledby="screenshots-heading"' in page[section:heading]
    fold = FOLD.search(page)
    if 'id="review-form"' in page:
        assert fold is not None
        form_end = page.index("</form>", page.index('id="review-form"'))
        assert form_end < section < heading < fold.start()
        assert "</section>" not in page[heading : fold.start()]
        assert page.index('id="saving-heading"') < heading
    else:
        assert fold is None
        assert headings[0] == "Screenshots"


SAVING = re.compile(
    r'<article class="assignment" id="(saving-\d+)" tabindex="-1">(.*?)</article>', re.S
)


def cards_held(page: str) -> dict[str, list[str]]:
    """Each group of what saving adds by its id, with the pieces whose choices it holds, after
    checking that every piece's choices on the page sit inside a group."""
    held = {
        anchor: [html.unescape(p) for p in re.findall(r'name="answer:destination:([^"]+)"', markup)]
        for anchor, markup in SAVING.findall(page)
    }
    everywhere = fields_named(review_form(page), "answer:destination:")
    assert sorted(p for pieces in held.values() for p in dict.fromkeys(pieces)) == sorted(
        everywhere
    )
    return {anchor: list(dict.fromkeys(pieces)) for anchor, pieces in held.items()}


def kinds_answered(browser: TestClient, draft_id: str, page: str) -> str:
    """The page after every card's Type is kept as homework, so each group can be ticked."""
    for key in fields_named(review_form(page), "answer:kind:"):
        keep(browser, draft_id, ("kind", key, "HOMEWORK"))
    return browser.get(f"/parent/imports/{draft_id}").text


def test_each_card_s_choices_sit_inside_the_group_of_what_saving_adds_holding_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each card's choices, its fold and its Type sit inside the group of what saving adds that
    holds it, under her week's card of where it goes, so no link between them is needed."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, *eight_cards())
        page = kinds_answered(browser, draft_id, her_name_answered(browser, draft_id, page))
        before = assignment_count(browser)
        saved = followed(
            browser, send(browser, draft_id, page_post(page, "save", tick=units_on(page)))
        )
        added = assignment_count(browser) - before

    held = cards_held(page)
    ticks = {
        anchor: unit.split("+")
        for anchor, markup in SAVING.findall(page)
        for unit in re.findall(r'name="unit" value="([^"]+)"', markup)
    }
    assert len(held) == len(ticks) == 8
    assert all(held[anchor] == ticks[anchor] for anchor in held)
    for _, markup in SAVING.findall(page):
        assert markup.count("<h3>") == 1
        assert markup.index("<h3>") < markup.index("answer:destination:")
        assert markup.count('name="answer:kind:') == 2
    assert "See what saving does" not in page
    assert says(saved, "Saved: 8 new assignments.")
    assert added == 8


def test_a_destination_two_cards_go_to_is_one_group_holding_both(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        _, page = two_names_found_onto_one_row(browser, monkeypatch, date(2026, 10, 8))

    held = cards_held(page)
    [(_, pieces)] = held.items()
    [(_, markup)] = SAVING.findall(page)
    assert len(pieces) == 2
    assert markup.count("<h3>") == 1


def test_one_title_s_cards_in_groups_of_their_own_each_show_their_title_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cards = [
        homework_card(title="Practice", instruction="Page one."),
        homework_card(title="Practice", instruction="Page two.", y_top=360, y_bottom=450),
    ]
    with here(open_settings(tmp_path)) as browser:
        _, page = read_review(browser, monkeypatch, (RED, homework_answer(cards=cards)))

    held = cards_held(page)
    assert len(held) == 2
    assert all(len(pieces) == 1 for pieces in held.values())
    for _, markup in SAVING.findall(page):
        assert markup.count("<h3>Practice</h3>") == 1
        assert "(screenshot 1, card" not in re.sub(
            r'<span class="visually-hidden">[^<]*</span>', "", markup
        )


def test_repeated_controls_of_the_screenshot_review_name_their_card_and_keep_their_words(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, *eight_cards())
        page = kinds_answered(browser, draft_id, her_name_answered(browser, draft_id, page))

    shown = control_names(page) + field_names(page)
    each = (
        "Save this assignment",
        "Leave this group unsaved",
        "Find under another name…",
        "Remove",
        "Correct what was read",
        "What the screenshot said",
        "Where it goes",
        "Homework or task?",
        "Class",
        "Title",
        "Due date",
        "Assigned date",
        "No assigned date was given",
    )
    repeated = {
        words: [name for said, name in shown if said == words]
        for words in (*each, "View screenshot")
    }
    counts = {words: len(names) for words, names in repeated.items()}
    assert counts == {**dict.fromkeys(each, 8), "View screenshot": 16}
    assert all(len(set(names)) == len(names) for names in repeated.values())
    assert all(
        re.search(r"Practice \d", name)
        for words, names in repeated.items()
        for name in names
        if words != "Remove" and not re.fullmatch(r"View screenshot \d", name)
    )
    assert names_without_their_words(page) == names_not_led_by_their_words(page) == []
    assert "Suggested choice. You can change it." in page
    assert "Preselected; kept" not in page


def two_split_rows(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    """Alpha and Beta, each read under a Monday and a Tuesday heading: two rows whose assigned
    days differ, each with its own "Leave this group unsaved"."""

    def day(heading: str) -> object:
        return homework_answer(
            headings=[{"text": heading, "y": 150, "uncertain": None}],
            cards=[
                homework_card(title="Alpha", footer="Due (10/9/2026)"),
                homework_card(title="Beta", footer="Due (10/9/2026)", y_top=360, y_bottom=450),
            ],
        )

    _, page = read_review(
        browser, monkeypatch, (RED, day("Monday 10/5/2026")), (BLUE, day("Tuesday 10/6/2026"))
    )
    return page


def cards_unread(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    """Two cards that couldn't be read, each with its own "Set aside"."""
    return read_review(browser, monkeypatch, (RED, unreadable_answer()))[1]


def a_refused_group(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    """A row split into its two days, each kept as new homework: a refused group whose offers
    each have a "Use this change"."""
    monday, tuesday = split_answers()
    draft_id, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, tuesday))
    [row] = fields_named(review_form(page), "answer:split:")
    keep(browser, draft_id, ("split", row, json.dumps("split")))
    split = browser.get(f"/parent/imports/{draft_id}").text
    days = fields_named(review_form(split), "answer:destination:")
    keep(browser, draft_id, *(("destination", d, '{"new":1}') for d in days))
    return browser.get(f"/parent/imports/{draft_id}").text


REPEATING: Final = {
    "split rows": (two_split_rows, "Leave this group unsaved"),
    "cards that couldn't be read": (cards_unread, "Set aside"),
    "a refused group's offers": (a_refused_group, "Use this change"),
}
"""Reviews that show a control once per row, card or offer, and that control's words."""


@pytest.mark.parametrize("case", list(REPEATING))
def test_every_repeated_control_of_the_screenshot_review_has_a_name_of_its_own(
    case: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every button, link and summary shown more than once says which row, card or offer it
    acts on. A radio's label repeats across questions under each question's own legend, which
    names its group."""
    make, words = REPEATING[case]
    with here(open_settings(tmp_path)) as browser:
        page = make(browser, monkeypatch)

    by_words: dict[str, list[str]] = {}
    for said, name in control_names(page):
        by_words.setdefault(said, []).append(name)
    assert len(by_words[words]) >= 2
    assert {said: names for said, names in by_words.items() if len(set(names)) < len(names)} == {}
    assert names_without_their_words(page) == names_not_led_by_their_words(page) == []


# ------------------------------------------------------------------ partial saves and holders


def two_unsure() -> dict[str, object]:
    marked = [{"field": "title", "reason": "a smudge over it"}]
    return homework_answer(
        cards=[
            homework_card(uncertain=marked),
            homework_card(
                course="Science 8",
                title="Chains Reading Guide 6",
                instruction="Read chapter 6.",
                uncertain=marked,
                y_top=360,
                y_bottom=430,
            ),
        ]
    )


def test_a_partial_save_returns_the_rows_left_open_with_their_typing_marked_unsaved(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, two_unsure()))
        page = her_name_answered(browser, draft_id, page)
        first_confirm, second_confirm = fields_named(review_form(page), "answer:confirm:")
        first_unit = digests_on(page)[0]
        fields = page_post(
            page,
            "save",
            tick=[first_unit],
            choose={f"answer:confirm:{first_confirm}": "as_read"},
            typed={f"correct:{second_confirm}": "Chains Reading Guide Six"},
        )
        saved = send(browser, draft_id, fields)
        again = send(browser, draft_id, fields)
        student = state_of(browser).project_state.student_id()
        progress = state_of(browser).import_drafts.snapshot(student, draft_id).progress

    for answer in (saved, again):
        assert answer.status_code == 200
        assert says(answer.text, imports.counted(1, 0, 0, 1))
        assert says(answer.text, imports.PRESS_SAID["unsaved_typing"])
        form = review_form(answer.text)
        assert 'value="Chains Reading Guide Six"' in form
        assert says(form, screenshot_page.SAID["not_saved_yet"])
    assert progress is None


def test_typing_already_saved_for_later_isn_t_marked_unsaved(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, unsure_answer()))
        field = box_subject(page, "title")
        typed = {f"correct:{field}": "Sentence Combining"}
        later = followed(browser, send(browser, draft_id, page_post(page, "later", typed=typed)))
        kept = send(browser, draft_id, page_post(later, "keep"))

    assert 'value="Sentence Combining"' in review_form(kept.text)
    assert not says(review_form(kept.text), screenshot_page.SAID["not_saved_yet"])


def test_a_refusal_s_holder_outside_the_card_s_list_is_offered_as_its_destination(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    later_card = homework_answer(cards=[homework_card(footer="Due (10/30/2026)")])
    found_row = Assignment(
        assignment_id="assignment-grammar-practice-hhhh7777",
        course="English 8",
        title="Grammar Practice",
        due_date=date(2026, 10, 30),
        dependencies=[],
        reported_submission_status="not_reported",
        assigned_on=None,
        kind=AssignmentKind.HOMEWORK,
    )
    with here(open_settings(tmp_path)) as browser:
        first, page = read_review(browser, monkeypatch, (BLUE, later_card))
        page = her_name_answered(browser, first, page)
        send(browser, first, page_post(page, "save", tick=units_on(page)))
        [holder] = [
            a
            for a in state_of(browser).project_state.all_assignments()
            if a.title == "Sentence Composing" and a.due_date == date(2026, 10, 30)
        ]
        state_of(browser).project_state.upsert_assignments([found_row])
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        [piece] = fields_named(review_form(page), "answer:destination:")
        found = json.dumps({"found": found_row.assignment_id}, separators=(",", ":"))
        keep(browser, draft_id, ("destination", piece, found))
        refused = browser.get(f"/parent/imports/{draft_id}").text
        offered = json.dumps({"found": holder.assignment_id}, separators=(",", ":"))
        chosen = send(
            browser,
            draft_id,
            page_post(refused, "keep", choose={f"answer:destination:{piece}": offered}),
        )
        answers = stored_answers(browser, draft_id)

    form = html.unescape(review_form(refused))
    control = re.search(
        r'<legend>Where it goes<span class="visually-hidden"> for Sentence Composing \(.*?'
        r"</fieldset>",
        form,
        re.S,
    )
    assert control is not None
    assert "would copy the words of" in form
    assert f'value="{offered}"' in control.group(0)
    assert "Sentence Composing, due Fri 10/30, assigned Mon 10/5" in control.group(0)
    assert "ref " in control.group(0) or holder.assignment_id in control.group(0)
    assert "Has this card's words." in control.group(0)
    assert "Due Fri 10/30: this card says Thu 10/8." in control.group(0)
    assert chosen.status_code == 200
    assert answers[("destination", piece)] == offered


def preview_cards(page: str) -> list[tuple[str, str, str]]:
    """Each destination of what saving adds as her week's card shows it: its class, its title
    and its due date, as the page reads."""
    found = re.search(r'aria-labelledby="saving-heading">.*?</section>', page, re.S)
    assert found is not None
    return [
        (html.unescape(card[0]), html.unescape(card[1]), html.unescape(card[2]))
        for card in re.findall(
            r'<p class="course">([^<]*)</p>\s*<h3>([^<]*)</h3>\s*<p class="due">([^<]*)</p>',
            found.group(0),
        )
    ]


def test_an_assignment_reads_the_same_in_its_control_and_in_the_preview(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another class's assignment reads like the one listed here, but the page never names it,
    so neither the control nor the preview tells the listed one apart from it."""

    def saved(course: str, tail: str) -> Assignment:
        return Assignment(
            assignment_id=f"assignment-sentence-composing-{tail}",
            course=course,
            title="Sentence Composing",
            due_date=date(2026, 10, 8),
            dependencies=[],
            reported_submission_status="not_reported",
            assigned_on=None,
            kind=AssignmentKind.HOMEWORK,
        )

    plain = "Sentence Composing, due Thu 10/8, no assigned date, no instructions"
    with here(open_settings(tmp_path)) as browser:
        state_of(browser).project_state.upsert_assignments(
            [saved("English 8", "aaaa1111"), saved("Science 8", "bbbb1111")]
        )
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        [piece] = fields_named(review_form(page), "answer:destination:")
        listed = json.dumps(
            {"existing": "assignment-sentence-composing-aaaa1111"}, separators=(",", ":")
        )
        kept = followed(
            browser,
            send(
                browser,
                draft_id,
                page_post(page, "keep", choose={f"answer:destination:{piece}": listed}),
            ),
        )

    [card] = destination_cards(kept)
    shown = re.search(rf'value="{re.escape(str(escape(listed)))}" checked> <span>([^<]*)', card)
    assert shown is not None
    assert shown.group(1).strip() == plain
    assert preview_cards(kept) == [("English 8", "Sentence Composing", "Due Thursday, October 8")]
    assert says(kept, screenshot_page.SAID["updates"].format(title="Sentence Composing"))


def test_a_refused_destination_reads_the_same_in_the_refusal_its_control_and_the_preview(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    later_card = homework_answer(cards=[homework_card(footer="Due (10/30/2026)")])
    found_row = Assignment(
        assignment_id="assignment-grammar-practice-hhhh7777",
        course="English 8",
        title="Grammar Practice",
        due_date=date(2026, 10, 30),
        dependencies=[],
        reported_submission_status="not_reported",
        assigned_on=None,
        kind=AssignmentKind.HOMEWORK,
    )
    with here(open_settings(tmp_path)) as browser:
        first, page = read_review(browser, monkeypatch, (BLUE, later_card))
        page = her_name_answered(browser, first, page)
        send(browser, first, page_post(page, "save", tick=units_on(page)))
        state_of(browser).project_state.upsert_assignments([found_row])
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        [piece] = fields_named(review_form(page), "answer:destination:")
        found = json.dumps({"found": found_row.assignment_id}, separators=(",", ":"))
        keep(browser, draft_id, ("destination", piece, found))
        refused = browser.get(f"/parent/imports/{draft_id}").text

    form = html.unescape(review_form(refused))
    said = re.search(r"would copy the words of (.*?) into (.*?), so this group isn't saved\.", form)
    assert said is not None
    holder, destination = said.groups()
    [card] = destination_cards(refused)
    radios = [html.unescape(label).strip() for label in re.findall(r"> <span>([^<]*)", card)]
    assert f"Found under another name: {destination}" in radios
    assert holder in radios
    assert destination.startswith("Grammar Practice, due Fri 10/30")
    assert preview_cards(refused) == [("English 8", "Grammar Practice", "Due Friday, October 30")]


# ------------------------------------------------------------------ a box left unticked


def checked_names(page: str) -> list[str]:
    return [html.unescape(name) for name, _ in checked_values(review_form(page))]


def test_an_unticked_confirmation_is_withdrawn_so_a_save_writes_nothing_and_asks_again(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = homework_card(uncertain=[{"field": "instruction", "reason": "blurred"}])
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer(cards=[card])))
        page = her_name_answered(browser, draft_id, page)
        [subject] = fields_named(review_form(page), "answer:confirm:")
        field = f"answer:confirm:{subject}"
        kept = followed(
            browser, send(browser, draft_id, page_post(page, "keep", choose={field: "as_read"}))
        )
        held = stored_answers(browser, draft_id)
        units = units_on(kept)
        unticked = [f for f in page_post(kept, "save", tick=units) if f[0] != field]
        before = assignment_count(browser)
        saved = followed(browser, send(browser, draft_id, unticked))
        after = assignment_count(browser)
        answers = stored_answers(browser, draft_id)

    assert held[("confirm", subject)] == "as_read"
    assert units
    assert after == before
    assert ("confirm", subject) not in answers
    assert field not in checked_names(saved)
    assert units_on(saved) == []
    assert says(saved, screenshot_page.OPEN_NAMES["confirm"])


def test_clearing_every_instruction_box_withdraws_the_instructions_chosen(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def vocab(words: str) -> dict[str, object]:
        return homework_answer(cards=[homework_card(title="Vocab Quiz", instruction=words)])

    with here(open_settings(tmp_path)) as browser:
        first, page = read_review(browser, monkeypatch, (BLUE, vocab("Study list two.")))
        page = her_name_answered(browser, first, page)
        send(browser, first, page_post(page, "save", tick=units_on(page)))
        [row] = [
            a for a in state_of(browser).project_state.all_assignments() if a.title == "Vocab Quiz"
        ]
        draft_id, page = read_review(browser, monkeypatch, (RED, vocab("Study list one.")))
        [piece] = fields_named(review_form(page), "answer:destination:")
        existing = json.dumps({"existing": row.assignment_id}, separators=(",", ":"))
        asked = followed(
            browser,
            send(
                browser,
                draft_id,
                page_post(page, "keep", choose={f"answer:destination:{piece}": existing}),
            ),
        )
        [subject] = fields_named(review_form(asked), "answer:instructions:")
        field = f"answer:instructions:{subject}"
        chosen = followed(
            browser,
            send(
                browser,
                draft_id,
                [*page_post(asked, "keep"), (field, option_for(asked, subject, "Study list one."))],
            ),
        )
        held = stored_answers(browser, draft_id)
        cleared = followed(
            browser,
            send(browser, draft_id, [f for f in page_post(chosen, "keep") if f[0] != field]),
        )
        answers = stored_answers(browser, draft_id)

    assert held[("instructions", subject)] == json.dumps(["Study list one."])
    assert ("instructions", subject) not in answers
    assert field not in checked_names(cleared)
    assert says(cleared, screenshot_page.OPEN_NAMES["instructions"])


def test_an_unticked_these_are_her_homework_is_withdrawn(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unnamed = homework_answer(
        active_student={"name": None, "uncertain": None}, cards=[homework_card()]
    )
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, unnamed))
        field = "answer:name:missing"
        kept = followed(
            browser, send(browser, draft_id, page_post(page, "keep", choose={field: "confirmed"}))
        )
        held = stored_answers(browser, draft_id)
        units = units_on(kept)
        unticked = [f for f in page_post(kept, "save", tick=units) if f[0] != field]
        before = assignment_count(browser)
        saved = followed(browser, send(browser, draft_id, unticked))
        after = assignment_count(browser)
        answers = stored_answers(browser, draft_id)

    assert held[("name", "missing")] == "confirmed"
    assert units
    assert after == before
    assert ("name", "missing") not in answers
    assert field not in checked_names(saved)
    assert units_on(saved) == []


# ------------------------------------------------------------------ counts, names and her note


def acceptance_counts(
    browser: TestClient, draft_id: str, page: str
) -> tuple[int, int, int, int] | None:
    record = state_of(browser).project_state
    return record.homework_counts(record.student_id(), draft_id, acceptance_of(page))


def test_two_cards_in_one_assignment_count_once_added_and_once_already_saved(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def two_parts() -> dict[str, object]:
        return homework_answer(
            cards=[
                homework_card(badge="DUE", footer=None, instruction="Read chapter 1."),
                homework_card(
                    badge="DUE", footer=None, instruction="Read chapter 2.", y_top=360, y_bottom=450
                ),
            ]
        )

    with here(open_settings(tmp_path)) as browser:
        first, page = read_review(browser, monkeypatch, (RED, two_parts()))
        page = her_name_answered(browser, first, page)
        pieces = fields_named(review_form(page), "answer:destination:")
        slot = json.dumps({"new": 1}, separators=(",", ":"))
        kept = followed(
            browser,
            send(
                browser,
                first,
                page_post(page, "keep", choose={f"answer:destination:{p}": slot for p in pieces}),
            ),
        )
        before = assignment_count(browser)
        added = followed(
            browser, send(browser, first, page_post(kept, "save", tick=units_on(kept)))
        )
        made = assignment_count(browser) - before
        added_counts = acceptance_counts(browser, first, kept)
        [row] = [
            a
            for a in state_of(browser).project_state.all_assignments()
            if a.title == "Sentence Composing"
        ]
        again, page = read_review(browser, monkeypatch, (BLUE, two_parts()))
        pieces = fields_named(review_form(page), "answer:destination:")
        onto = json.dumps({"existing": row.assignment_id}, separators=(",", ":"))
        kept = followed(
            browser,
            send(
                browser,
                again,
                page_post(page, "keep", choose={f"answer:destination:{p}": onto for p in pieces}),
            ),
        )
        send(browser, again, page_post(kept, "save", tick=units_on(kept)))
        unchanged_counts = acceptance_counts(browser, again, kept)

    assert len(pieces) == 2
    assert made == 1
    assert says(added, "Saved: 1 new assignment.")
    assert added_counts is not None
    assert added_counts[0] == 1
    assert unchanged_counts is not None
    assert unchanged_counts[:3] == (0, 0, 1)


def test_two_cards_of_other_names_found_onto_one_row_count_once_and_each_name_is_kept(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Assignment(
        assignment_id="assignment-grammar-practice-eeee4444",
        course="English 8",
        title="Grammar Practice",
        due_date=date(2026, 10, 8),
        dependencies=[],
        reported_submission_status="not_reported",
        assigned_on=None,
        kind=AssignmentKind.HOMEWORK,
    )
    cards = [
        homework_card(title="Vocab Test", instruction="Study list one."),
        homework_card(
            title="Spelling List", instruction="Study list two.", y_top=360, y_bottom=450
        ),
    ]
    with here(open_settings(tmp_path)) as browser:
        state_of(browser).project_state.upsert_assignments([target])
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer(cards=cards)))
        page = her_name_answered(browser, draft_id, page)
        for piece in fields_named(review_form(page), "answer:destination:"):
            finding = browser.post(
                f"/parent/imports/{draft_id}/find", data={"piece": piece, "words": "grammar"}
            ).text
            choice = re.search(
                r'<form method="post" action="[^"]+/review" class="choice">.*?</form>',
                finding,
                re.S,
            )
            assert choice is not None
            fields = [
                (html.unescape(n), html.unescape(v))
                for n, v in re.findall(r'name="([^"]+)" value="([^"]*)"', choice.group(0))
            ]
            page = followed(browser, send(browser, draft_id, fields))
        [field] = fields_named(review_form(page), "answer:instructions:")
        name = f"answer:instructions:{field}"
        texts = ["Study list one.", "Study list two."]
        page = followed(
            browser,
            send(
                browser,
                draft_id,
                page_post(page, "keep") + [(name, option_for(page, field, t)) for t in texts],
            ),
        )
        saved = followed(
            browser, send(browser, draft_id, page_post(page, "save", tick=units_on(page)))
        )
        counts = acceptance_counts(browser, draft_id, page)
        decided = state_of(browser).project_state.intake_decisions(
            [("English 8", "Vocab Test"), ("English 8", "Spelling List")]
        )

    assert says(saved, "Saved: 0 new assignments. 1 updated.")
    assert counts is not None
    assert counts[:3] == (0, 1, 0)
    landed = {
        pair: [d.lands_on for d in found if d.kind == "renamed"] for pair, found in decided.items()
    }
    assert landed == {
        ("English 8", "Vocab Test"): [target.assignment_id],
        ("English 8", "Spelling List"): [target.assignment_id],
    }


def two_names_found_onto_one_row(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch, due: date
) -> tuple[str, str]:
    """A review whose "Vocab Test" and "Spelling List" cards, due Thu 10/8, are both found onto
    one "Grammar Practice" due on ``due``: its id and the page after."""
    target = Assignment(
        assignment_id="assignment-grammar-practice-eeee4444",
        course="English 8",
        title="Grammar Practice",
        due_date=due,
        dependencies=[],
        reported_submission_status="not_reported",
        assigned_on=None,
        kind=AssignmentKind.HOMEWORK,
    )
    cards = [
        homework_card(title="Vocab Test", instruction="Study list one."),
        homework_card(
            title="Spelling List", instruction="Study list two.", y_top=360, y_bottom=450
        ),
    ]
    state_of(browser).project_state.upsert_assignments([target])
    draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer(cards=cards)))
    page = her_name_answered(browser, draft_id, page)
    for piece in fields_named(review_form(page), "answer:destination:"):
        finding = browser.post(
            f"/parent/imports/{draft_id}/find", data={"piece": piece, "words": "grammar"}
        ).text
        choice = re.search(
            r'<form method="post" action="[^"]+/review" class="choice">.*?</form>', finding, re.S
        )
        assert choice is not None
        fields = [
            (html.unescape(n), html.unescape(v))
            for n, v in re.findall(r'name="([^"]+)" value="([^"]*)"', choice.group(0))
        ]
        page = followed(browser, send(browser, draft_id, fields))
    return draft_id, page


def legends_starting(page: str, start: str) -> int:
    return len(re.findall(rf"<legend>{re.escape(start)}", html.unescape(review_form(page))))


def test_the_instruction_question_of_one_destination_is_asked_once_and_saves_after_one_keep(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = two_names_found_onto_one_row(browser, monkeypatch, date(2026, 10, 8))
        [field] = fields_named(review_form(page), "answer:instructions:")
        offered = [
            html.unescape(v)
            for n, v in re.findall(r'name="([^"]+)" value="([^"]*)"', review_form(page))
            if html.unescape(n) == f"answer:instructions:{field}" and v
        ]
        kept = followed(
            browser,
            send(
                browser,
                draft_id,
                [
                    *page_post(page, "keep"),
                    *((f"answer:instructions:{field}", text) for text in offered),
                ],
            ),
        )
        posted = page_post(kept, "save", tick=units_on(kept))
        saved = followed(browser, send(browser, draft_id, posted))

    assert legends_starting(page, "Which instructions apply to") == 1
    assert [json.loads(v) for v in offered] == ["Study list one.", "Study list two."]
    assert [v for n, v in posted if n.startswith("answer:instructions:")] == offered
    assert says(saved, "Saved: 0 new assignments. 1 updated.")


def test_the_due_date_question_of_one_destination_is_asked_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        _, page = two_names_found_onto_one_row(browser, monkeypatch, date(2026, 10, 9))

    assert len(fields_named(review_form(page), "answer:due:")) == 1
    assert legends_starting(page, "Due date for") == 1


def test_homework_made_from_her_note_saves_in_one_press_when_chosen_as_the_destination(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        record = state_of(browser).project_state
        made = homework_from_a_note(
            record, course="English 8", title="Sentence Composing", due_date=date(2026, 10, 8)
        )
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        page = her_name_answered(browser, draft_id, page)
        [piece] = fields_named(review_form(page), "answer:destination:")
        value = json.dumps({"existing": made}, separators=(",", ":"))
        kept = followed(
            browser,
            send(
                browser,
                draft_id,
                page_post(page, "keep", choose={f"answer:destination:{piece}": value}),
            ),
        )
        saved = followed(
            browser, send(browser, draft_id, page_post(kept, "save", tick=units_on(kept)))
        )
        pairs = record.homework_acceptance(record.student_id(), draft_id, acceptance_of(kept))
        decided = record.intake_decisions([("English 8", "Sentence Composing")])

    assert value in html.unescape(review_form(page))
    assert not says(saved, imports.PRESS_SAID["changed"])
    assert pairs == ((piece, made),)
    assert not [d for found in decided.values() for d in found if d.kind != "renamed"]


def test_a_found_row_s_page_is_ticked_as_a_plain_load_would_tick_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = Assignment(
        assignment_id="assignment-grammar-practice-eeee4444",
        course="Science 8",
        title="Grammar Practice",
        due_date=date(2026, 10, 8),
        dependencies=[],
        reported_submission_status="not_reported",
        assigned_on=None,
        kind=AssignmentKind.HOMEWORK,
    )
    with here(open_settings(tmp_path)) as browser:
        state_of(browser).project_state.upsert_assignments([other])
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        pieces = fields_named(review_form(page), "answer:destination:")
        chains = next(p for p in pieces if f"answer:destination:{p}" in page)
        finding = browser.post(
            f"/parent/imports/{draft_id}/find", data={"piece": pieces[-1], "words": "grammar"}
        ).text
        choice = re.search(
            r'<form method="post" action="[^"]+/review" class="choice">.*?</form>', finding, re.S
        )
        assert choice is not None
        fields = [
            (html.unescape(n), html.unescape(v))
            for n, v in re.findall(r'name="([^"]+)" value="([^"]*)"', choice.group(0))
        ]
        found_page = followed(browser, send(browser, draft_id, fields))
        reloaded = browser.get(f"/parent/imports/{draft_id}").text

    assert chains
    assert units_ticked(reloaded)
    assert units_ticked(found_page) == units_ticked(reloaded)


# ------------------------------------------------------------------ after the first save

FROZEN = (
    "This review's screenshots are fixed now that some homework is saved. To add or change a "
    "screenshot, start a new upload; Blossom compares it with what is saved."
)


def test_after_a_partial_save_the_screenshots_are_fixed_and_a_remove_from_before_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = homework_answer(cards=[homework_card(course="Science 8", title="Lab Report")])
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()), (BLUE, lab))
        page = her_name_answered(browser, draft_id, page)
        units = units_on(page)
        partial = followed(
            browser, send(browser, draft_id, page_post(page, "save", tick=units[:1]))
        )
        reloaded = browser.get(f"/parent/imports/{draft_id}").text
        [first_image, second_image] = images_of(browser, draft_id)
        removed = browser.post(f"/parent/imports/{draft_id}/images/{first_image}/remove")
        added = upload(browser, GREEN, form={}, to=f"/parent/imports/{draft_id}/images")
        still = images_of(browser, draft_id)

    assert f"/images/{first_image}/remove" in page
    for shown in (partial, reloaded):
        fold = FOLD.search(shown)
        assert fold is not None
        assert screenshots_in(fold.group(3)) == ["Screenshot 1", "Screenshot 2"]
        assert screenshots_in(beside_the_fold(shown, fold)) == []
        assert says(beside_the_fold(shown, fold), FROZEN)
        assert "/remove" not in shown
        assert f'action="/parent/imports/{draft_id}/images"' not in shown
        assert f'action="/parent/imports/{draft_id}/read"' not in shown
        assert units_on(shown)
    assert removed.status_code == 409
    assert says(removed.text, FROZEN)
    assert added.status_code == 409
    assert says(added.text, FROZEN)
    assert still == [first_image, second_image]


# ------------------------------------------------------------------ a found row or an offer


def assert_returned_with_saved_typing(page: str, chosen: str, box: str, typed: str) -> None:
    """The page a found row's or an offer's post returns: the correction saved for later in
    its box, marked as nothing unsaved, the destination posted chosen, and no answer set aside
    for a lost correction."""
    assert box_of(review_form(page), box)[1] == typed  # type: ignore[index]
    assert not says(page, screenshot_page.SAID["not_saved_yet"])
    assert not says(page, screenshot_page.SAID["lost_correction"])
    assert chosen in [html.unescape(v) for _, v in checked_values(review_form(page))]


def test_a_found_row_or_an_offer_returns_the_page_with_the_corrections_saved_for_later(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    later_card = homework_answer(cards=[homework_card(footer="Due (10/30/2026)")])
    found_row = Assignment(
        assignment_id="assignment-grammar-practice-hhhh7777",
        course="English 8",
        title="Grammar Practice",
        due_date=date(2026, 10, 30),
        dependencies=[],
        reported_submission_status="not_reported",
        assigned_on=None,
        kind=AssignmentKind.HOMEWORK,
    )
    unsure = homework_card(uncertain=[{"field": "instruction", "reason": "blurred"}])
    moved = "2026-10-09"
    with here(open_settings(tmp_path)) as browser:
        first, page = read_review(browser, monkeypatch, (BLUE, later_card))
        page = her_name_answered(browser, first, page)
        send(browser, first, page_post(page, "save", tick=units_on(page)))
        state_of(browser).project_state.upsert_assignments([found_row])
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer(cards=[unsure])))
        box = f"correct:{box_subject(page, 'due')}"
        followed(browser, send(browser, draft_id, page_post(page, "later", typed={box: moved})))
        [piece] = fields_named(review_form(page), "answer:destination:")
        finding = browser.post(
            f"/parent/imports/{draft_id}/find", data={"piece": piece, "words": "grammar"}
        ).text
        choice = re.search(
            r'<form method="post" action="[^"]+/review" class="choice">.*?</form>', finding, re.S
        )
        assert choice is not None
        fields = [
            (html.unescape(n), html.unescape(v))
            for n, v in re.findall(r'name="([^"]+)" value="([^"]*)"', choice.group(0))
        ]
        found_page = followed(browser, send(browser, draft_id, fields))
        found = json.dumps({"found": found_row.assignment_id}, separators=(",", ":"))
        assert_returned_with_saved_typing(found_page, found, box, moved)
        _, offer_fields = outside(found_page, "offer-1-1")
        offered = dict(offer_fields)[f"answer:destination:{piece}"]
        offer_page = followed(browser, press_outside(browser, found_page, "offer-1-1"))

    assert_returned_with_saved_typing(offer_page, offered, box, moved)


def test_each_card_shows_its_instructions_whole_as_the_school_s_words(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long = "Complete pages 12 to 14, then write three sentences about each of the stories."
    with here(open_settings(tmp_path)) as browser:
        _, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card(instruction=long)]))
        )

    form = review_form(page)
    assert f'<p class="from-teacher">From the school: <q class="authored-text">{long}</q></p>' in (
        form
    )
    assert box_of(form, f"correct:{box_subject(page, 'instruction:0')}")[1] == long  # type: ignore[index]


# ------------------------------------------------------------------ dates as the screenshot said


def disagreeing_due_card(**fields: object) -> dict[str, object]:
    """A due card whose own due line says Thursday under a day heading that says Monday."""
    return homework_answer(cards=[homework_card(badge="DUE", footer="Due (10/8/2026)", **fields)])


def test_a_due_card_whose_line_and_heading_disagree_shows_both_and_waits_for_a_choice(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, disagreeing_due_card()))
        page = her_name_answered(browser, draft_id, page)
        [piece] = fields_named(review_form(page), "answer:destination:")
        units = digests_on(page)
        before = assignment_count(browser)
        refused = send(browser, draft_id, page_post(page, "save", tick=units))
        made = assignment_count(browser)
        keep(browser, draft_id, ("due_read", piece, "2026-10-08"))
        settled = browser.get(f"/parent/imports/{draft_id}").text
        [after] = fields_named(review_form(settled), "answer:destination:")

    form = html.unescape(review_form(page))
    assert (
        "Screenshot 1, card 1 said: due line “Due (10/8/2026)”; day heading “Monday 10/5/2026”."
        in form
    )
    assert (
        "Its due line and its day heading name different dates. Which due date does Blossom use?"
        in form
    )
    assert "Due Thu 10/8, as the due line says" in form
    assert "Due Mon 10/5, as the day heading says" in form
    assert not [n for n, _ in checked_values(review_form(page)) if n.startswith("answer:due_read:")]
    assert units_on(page) == []
    assert "which due date Blossom uses" in form
    assert says(refused.text, REFUSED_OPEN)
    assert made == before
    assert after == piece
    due_box = box_of(review_form(page), f"correct:{box_subject(page, 'due')}")
    assert due_box is not None
    assert due_box[1] == ""
    assert screenshot_page.SAID["due_hint"] in form
    assert '<p class="due">Due Thursday, October 8</p>' in review_form(settled)
    assert screenshot_page.SAID["due_hint"] not in html.unescape(review_form(settled))
    assert units_on(settled)


def test_the_heading_shown_for_a_card_is_the_one_its_row_was_dated_by(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two day headings read at the same height above a due card: the page names the one the
    assembly placed it under, the one its due date came from."""
    answer = homework_answer(
        headings=[
            {"text": "Monday 10/5/2026", "y": 150, "uncertain": None},
            {"text": "Tuesday 10/6/2026", "y": 150, "uncertain": None},
        ],
        cards=[homework_card(badge="DUE", footer=None)],
    )
    with here(open_settings(tmp_path)) as browser:
        _, page = read_review(browser, monkeypatch, (RED, answer))

    form = html.unescape(review_form(page))
    assert '<p class="due">Due Tuesday, October 6</p>' in form
    assert "said: due line none; day heading \u201cTuesday 10/6/2026\u201d." in form


def test_a_due_card_asks_once_of_its_due_date_and_an_assigned_card_of_each_date(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unsure_dates = [
        {"field": "footer", "reason": "blurred"},
    ]
    due_card = homework_answer(
        headings=[{"text": "Monday 10/5/2026", "y": 150, "uncertain": "faint"}],
        cards=[homework_card(badge="DUE", footer="Due (10/5/2026)", uncertain=unsure_dates)],
    )
    assigned_card = homework_answer(
        headings=[{"text": "Monday 10/5/2026", "y": 150, "uncertain": "faint"}],
        cards=[homework_card(uncertain=unsure_dates)],
    )
    with here(open_settings(tmp_path)) as browser:
        _, due_page = read_review(browser, monkeypatch, (RED, due_card))
        _, assigned_page = read_review(browser, monkeypatch, (BLUE, assigned_card))

    due_form = html.unescape(review_form(due_page))
    assigned_form = html.unescape(review_form(assigned_page))
    unsure = r"<legend>(The reader wasn't sure of the [^<]*)<span"
    assert re.findall(unsure, due_form) == ["The reader wasn't sure of the due date."]
    assert re.findall(unsure, assigned_form) == [
        "The reader wasn't sure of the due date.",
        "The reader wasn't sure of the assigned date (day heading).",
    ]
    for form in (due_form, assigned_form):
        assert form.count('type="date"') == 2
        assert re.search(r">Due date<span [^>]*>[^<]*</span> <input type=\"date\"", form)
        assert re.search(r">Assigned date<span [^>]*>[^<]*</span> <input type=\"date\"", form)


def test_the_due_or_assigned_label_is_confirmed_never_typed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = homework_answer(
        cards=[homework_card(uncertain=[{"field": "badge", "reason": "faint"}])]
    )
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        [subject] = fields_named(review_form(page), "answer:confirm:")
        forged = [*page_post(page, "keep"), (f"correct:{subject}", "DUE")]
        refused = followed(browser, send(browser, draft_id, forged))

    form = review_form(page)
    label = re.search(
        r"<legend>The reader wasn't sure of the due or assigned label\.<span[^>]*>[^<]*</span>"
        r"</legend>(.*?)</fieldset>",
        html.unescape(form),
        re.S,
    )
    assert label is not None
    assert 'name="correct:' not in label.group(1)
    assert screenshot_page.SAID["correct_it"] not in label.group(1)
    assert screenshot_page.SAID["confirm"] in label.group(1)
    assert says(refused, imports.PRESS_SAID["keep_invalid"])
    key = json.loads(subject)[0]
    for shape in (3, 4):
        badge: dict[str, object] = {
            "v": shape,
            "corrected": {key: {"badge": "DUE"}},
            "selected": [],
        }
        assert screenshot_posts.progress_read(json.dumps(badge)) is None


def test_a_typed_date_blossom_can_t_read_is_said_and_not_used(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = homework_answer(
        cards=[homework_card(uncertain=[{"field": "footer", "reason": "blurred"}])]
    )
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        field = box_subject(page, "due")
        kept = send(browser, draft_id, page_post(page, "keep", typed={f"correct:{field}": "10/14"}))

    shown = html.unescape(kept.text)
    assert "The date given was “10/14”, which is not a date Blossom can read." in shown
    assert '<p class="due">Due Thursday, October 8</p>' in shown
    assert "Still to answer: a field the reader wasn't sure of, the due date" in shown


def test_a_partial_save_lists_the_saved_cards_as_saved(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        first, _ = units_on(page)
        saved = send(browser, draft_id, page_post(page, "save", tick=[first]))

    shown = html.unescape(saved.text)
    assert '<h2 id="saved-heading">Saved</h2>' in shown
    assert re.search(r"<li>Sentence Composing \(screenshot 1, card 1\)</li>", shown)


def test_each_group_s_tick_sits_inline_with_its_label(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)

    ticks = re.findall(r'<div class="choice">\s*<label><input type="checkbox" name="unit"', page)
    assert len(ticks) == 2


# ------------------------------------------------------------------ changes that clear answers

CLEARED_INTRO = "Your screenshots changed, so these rows' answers were cleared:"


def test_removing_a_screenshot_that_clears_an_answer_says_which_rows_to_answer_again(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vocab = homework_answer(
        cards=[homework_card(title="Vocab Quiz", instruction="Study list one.")]
    )
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()), (BLUE, vocab))
        followed(browser, send(browser, draft_id, page_post(page, "keep")))
        [_, blue] = images_of(browser, draft_id)
        removed = followed(
            browser, browser.post(f"/parent/imports/{draft_id}/images/{blue}/remove")
        )
        again = browser.get(f"/parent/imports/{draft_id}").text

    assert says(removed, f"{CLEARED_INTRO} Vocab Quiz. Answer them again.")
    assert not says(removed, screenshot_page.CHANGE_SAID["copies"])
    assert CLEARED_INTRO not in html.unescape(again)


def test_removing_one_of_two_identical_screenshots_says_it_can_t_tell_the_copies_apart(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lone = homework_answer(cards=[homework_card()])
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, lone), (GREEN, lone))
        followed(browser, send(browser, draft_id, page_post(page, "keep")))
        [_, green] = images_of(browser, draft_id)
        removed = followed(
            browser, browser.post(f"/parent/imports/{draft_id}/images/{green}/remove")
        )

    assert says(removed, f"{CLEARED_INTRO} Sentence Composing. Answer them again.")
    assert says(removed, screenshot_page.CHANGE_SAID["copies"])


def test_reading_a_new_copy_of_an_answered_card_says_its_answers_were_cleared(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lone = homework_answer(cards=[homework_card()])
    reader = Reader(monkeypatch)
    for image in (RED, GREEN):
        reader.answers[image] = ScreenshotAnswer(lone, None, 10, 5)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        page = browser.get(f"/parent/imports/{draft_id}").text
        page = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page)).text
        followed(browser, send(browser, draft_id, page_post(page, "keep")))
        kept = [kind for kind, _ in stored_answers(browser, draft_id)]
        upload(browser, GREEN, form={}, to=f"/parent/imports/{draft_id}/images")
        page = browser.get(f"/parent/imports/{draft_id}").text
        read = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page))
        left = [kind for kind, _ in stored_answers(browser, draft_id)]

    assert "destination" in kept
    assert "destination" not in left
    assert says(read.text, f"{CLEARED_INTRO} Sentence Composing. Answer them again.")
    assert says(read.text, screenshot_page.CHANGE_SAID["copies"])


def two_cards() -> dict[str, object]:
    return homework_answer(
        cards=[
            homework_card(),
            homework_card(
                course="Science 8",
                title="Chains Reading Guide 6",
                instruction="Read chapter 6.",
                y_top=360,
                y_bottom=430,
            ),
        ]
    )


def test_removing_a_copy_of_a_group_left_unsaved_says_it_is_still_left_unsaved(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, two_cards()), (GREEN, two_cards()))
        page = her_name_answered(browser, draft_id, page)
        followed(browser, press_outside(browser, page, "leave-1"))
        [_, green] = images_of(browser, draft_id)
        removed = followed(
            browser, browser.post(f"/parent/imports/{draft_id}/images/{green}/remove")
        )
        states = row_states(browser, draft_id)

    assert says(
        removed,
        screenshot_page.CHANGE_SAID["carried"].format(label="Sentence Composing"),
    )
    assert not says(removed, CLEARED_INTRO)
    assert sorted(states.values()) == ["discarded", "open"]


def test_reading_a_new_copy_of_a_group_left_unsaved_says_it_asks_again(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Reader(monkeypatch)
    for image in (RED, GREEN):
        reader.answers[image] = ScreenshotAnswer(two_cards(), None, 10, 5)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        page = browser.get(f"/parent/imports/{draft_id}").text
        page = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page)).text
        page = her_name_answered(browser, draft_id, page)
        followed(browser, press_outside(browser, page, "leave-1"))
        upload(browser, GREEN, form={}, to=f"/parent/imports/{draft_id}/images")
        page = browser.get(f"/parent/imports/{draft_id}").text
        read = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page))
        states = row_states(browser, draft_id)

    assert read.status_code == 200
    assert says(read.text, screenshot_page.CHANGE_SAID["asked"].format(label="Sentence Composing"))
    assert sorted(states.values()) == ["open", "open"]


def destination_cards(page: str) -> list[str]:
    """Each piece's destination control on the review form, whole."""
    return [
        card
        for card in re.findall(
            r'<fieldset class="choice"[^>]*>.*?</fieldset>', review_form(page), re.S
        )
        if 'name="answer:destination:' in card
    ]


def asks_with_nothing_chosen(page: str) -> bool:
    """Whether the review's one card asks where it goes, nothing preselected, and its group
    waits to be answered."""
    [card] = destination_cards(page)
    return (
        " checked" not in card
        and screenshot_page.SAID["preselected"] not in card
        and units_on(page) == []
        and says(page, screenshot_page.SAID["choose_each"])
    )


def marks_of_review(browser: TestClient, draft_id: str) -> frozenset[str]:
    student = state_of(browser).project_state.student_id()
    return state_of(browser).import_drafts.review_answers(student, draft_id).marks


def test_a_card_whose_answer_a_change_cleared_asks_with_nothing_preselected(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lone = homework_answer(cards=[homework_card()])
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, lone), (GREEN, lone))
        page = her_name_answered(browser, draft_id, page)
        followed(browser, send(browser, draft_id, page_post(page, "keep")))
        [_, green] = images_of(browser, draft_id)
        removed = followed(
            browser, browser.post(f"/parent/imports/{draft_id}/images/{green}/remove")
        )
        reloaded = browser.get(f"/parent/imports/{draft_id}").text
        [piece] = fields_named(review_form(reloaded), "answer:destination:")
        [value] = re.findall(r'value="([^"]+)"', destination_cards(reloaded)[0])[:1]
        answered = followed(
            browser,
            send(
                browser,
                draft_id,
                page_post(
                    reloaded, "keep", choose={f"answer:destination:{piece}": html.unescape(value)}
                ),
            ),
        )
        marks = marks_of_review(browser, draft_id)

    assert screenshot_page.SAID["preselected"] in destination_cards(page)[0]
    assert asks_with_nothing_chosen(removed)
    assert asks_with_nothing_chosen(reloaded)
    assert " checked" in destination_cards(answered)[0]
    assert screenshot_page.SAID["preselected"] not in destination_cards(answered)[0]
    assert units_on(answered)
    assert marks == {piece}


def test_a_reading_stored_with_no_press_page_to_say_so_leaves_its_card_asking(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reading the Read press's own page never showed, as when that page was left before it
    came back: the change's mark still makes the card ask on the next page load."""
    lone = homework_answer(cards=[homework_card()])
    reader = Reader(monkeypatch)
    for image in (RED, GREEN):
        reader.answers[image] = ScreenshotAnswer(lone, None, 10, 5)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        page = browser.get(f"/parent/imports/{draft_id}").text
        page = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page)).text
        page = her_name_answered(browser, draft_id, page)
        followed(browser, send(browser, draft_id, page_post(page, "keep")))
        upload(browser, GREEN, form={}, to=f"/parent/imports/{draft_id}/images")
        state = state_of(browser)
        student = state.project_state.student_id()
        [_, green] = images_of(browser, draft_id)
        outcome = asyncio.run(
            screenshot_reading.read_screenshots(
                state.import_drafts,
                state.settings,
                student,
                draft_id,
                [green],
                monotonic=state.monotonic,
                held=set(),
            )
        )
        later = browser.get(f"/parent/imports/{draft_id}").text

    assert outcome.read == (green,)
    assert asks_with_nothing_chosen(later)


def test_an_answer_whose_correction_wasn_t_kept_says_why_its_row_asks_again(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, unsure_answer()))
        field = box_subject(page, "title")
        typed = {f"correct:{field}": "Sentence Combining"}
        send(browser, draft_id, page_post(page, "keep", typed=typed))
        reopened = browser.get(f"/parent/imports/{draft_id}").text

    assert screenshot_page.SAID["preselected"] in destination_cards(page)[0]
    assert says(reopened, screenshot_page.SAID["lost_correction"])
    assert asks_with_nothing_chosen(reopened)


# ------------------------------------------------------------------ the due date chosen, then saved


BOTH_READ = (
    "Screenshot 1, card 1 said: due line \u201cDue (10/8/2026)\u201d; day heading "
    "\u201cMonday 10/5/2026\u201d."
)


def due_card_and_another(**due_fields: object) -> dict[str, object]:
    return homework_answer(
        cards=[
            homework_card(badge="DUE", footer="Due (10/8/2026)", **due_fields),
            homework_card(
                course="Science 8",
                title="Chains Reading Guide 6",
                instruction="Read chapter 6.",
                y_top=360,
                y_bottom=430,
            ),
        ]
    )


def saved_due(browser: TestClient, title: str) -> date | None:
    [found] = [a for a in state_of(browser).project_state.all_assignments() if a.title == title]
    return found.due_date


@pytest.mark.parametrize(
    ("chosen", "written"),
    [
        ("Due Thu 10/8, as the due line says", date(2026, 10, 8)),
        ("Due Mon 10/5, as the day heading says", date(2026, 10, 5)),
    ],
)
def test_the_due_date_chosen_on_the_page_is_the_one_saved_and_its_answer_goes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, chosen: str, written: date
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, due_card_and_another()))
        page = her_name_answered(browser, draft_id, page)
        [piece, _] = fields_named(review_form(page), "answer:destination:")
        value = re.search(rf'value="([^"]+)"> <span>{re.escape(chosen)}</span>', review_form(page))
        assert value is not None
        kept = send(
            browser,
            draft_id,
            page_post(page, "keep", choose={f"answer:due_read:{piece}": value.group(1)}),
        )
        held = stored_answers(browser, draft_id)
        first = next(u for u in units_on(kept.text) if piece in u)
        send(browser, draft_id, page_post(kept.text, "save", tick=[first]))
        answers = stored_answers(browser, draft_id)
        due = saved_due(browser, "Sentence Composing")

    assert held[("due_read", piece)] == value.group(1)
    assert BOTH_READ in html.unescape(review_form(kept.text))
    assert due == written
    assert not [kind for kind, _ in answers if kind == "due_read"]


def test_a_due_date_typed_in_the_box_settles_the_question_and_is_the_one_saved(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unsure = [{"field": "footer", "reason": "blurred"}]
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser, monkeypatch, (RED, due_card_and_another(uncertain=unsure))
        )
        page = her_name_answered(browser, draft_id, page)
        asked = review_form(page)
        field = box_subject(page, "due")
        [subject] = fields_named(review_form(page), "answer:confirm:")
        kept = send(
            browser,
            draft_id,
            page_post(page, "keep", typed={f"correct:{field}": "2026-10-12"}),
        )
        piece = json.loads(subject)[0]
        first = next(u for u in units_on(kept.text) if piece in u)
        send(
            browser,
            draft_id,
            page_post(kept.text, "save", tick=[first]),
        )
        due = saved_due(browser, "Sentence Composing")

    assert "Which due date does Blossom use?" in asked
    assert "Which due date does Blossom use?" not in review_form(kept.text)
    assert BOTH_READ in html.unescape(review_form(kept.text))
    assert due == date(2026, 10, 12)


IMPORT_REVIEW_RULES = (
    ".import-review .choice {\n  min-width: 0;\n"
    "  padding-inline: clamp(0px, 13vw - 1rem, 1rem);\n}\n\n"
    ".import-review .choice label > span {\n  overflow-wrap: anywhere;\n}\n\n"
    ".import-review .panel {\n"
    "  margin-inline: min(0px, 13vw - 2.25rem);\n"
    "  padding-inline: clamp(0px, 13vw - 1rem, 1.6rem);\n}\n\n"
    ".import-review .panel article {\n"
    "  padding-inline: clamp(0px, 13vw - 1rem, 1.6rem);\n}\n\n"
    '.import-review input[type="file"] {\n  max-width: 100%;\n  min-height: 2.75rem;\n'
    "  padding-block: 0.6rem;\n"
    "  font: inherit;\n  font-family: var(--font-body);\n  color: var(--text);\n}\n\n"
    "/* The screenshot picker: the field's label is its button, in the app's own words and the"
    " look\n   of an outline button, and the browser's line beside it says what was chosen. The"
    " browser's\n   own button, worded by the browser, is hidden; a press on the label or on the"
    " line opens\n   the picker, and the focus rests on the line. The picker sits apart from the"
    " button that\n   sends it. */\n"
    ".import-review .file-pick {\n  display: flex;\n  flex-wrap: wrap;\n  align-items: center;\n"
    "  gap: 0.6rem 0.9rem;\n  margin-block: 0.75rem 1.25rem;\n}\n\n"
    ".import-review .file-choose {\n  display: inline-flex;\n  flex-direction: row;\n"
    "  align-items: center;\n  justify-content: center;\n  min-height: 2.75rem;\n"
    "  padding: 0.6rem 1.35rem;\n  border: 1.5px solid var(--pink);\n"
    "  border-radius: var(--pill);\n  background: transparent;\n  color: var(--rose-ink);\n"
    "  font-family: var(--font-display);\n  font-weight: 700;\n  cursor: pointer;\n"
    "  transition: background-color 120ms ease, color 120ms ease, border-color 120ms ease;\n}\n\n"
    ".import-review .file-choose:hover {\n  background: var(--rose-bg);\n"
    "  border-color: var(--pink-deep);\n}\n\n"
    '.import-review input[type="file"]::file-selector-button {\n  display: none;\n}\n\n'
    ".import-review legend,\n.import-review h3,\n.import-review .note,\n"
    ".import-review .source,\n"
    ".import-review .effect,\n.import-review .problem,\n.import-review .choice > p,\n"
    ".import-review .jump a,\n.import-review li {\n  overflow-wrap: anywhere;\n}\n\n"
    ".import-review button,\n.import-review input {\n  overflow-wrap: normal;\n}\n\n"
    ".import-review .choice .lines {\n  flex-direction: column;\n  align-items: stretch;\n}\n\n"
    ".import-review textarea {\n  min-height: 3.6rem;\n}\n\n"
    "/* A question's words sit inside its box, above its choices, and wrap there, never across"
    " its\n   border. */\n"
    ".import-review fieldset.choice > legend {\n  float: left;\n  width: 100%;\n  padding: 0;\n"
    "  margin-bottom: 0.5rem;\n}\n\n"
    ".import-review fieldset.choice > legend + * {\n  clear: both;\n}\n\n"
    "/* Each screenshot is one compact row: its thumbnail at a fixed width, then its place, how"
    " it\n   stands and its links, which go under the thumbnail where the line can't hold them."
    " Each box\n   of a card's corrections stands apart from the one above. */\n"
    ".import-review .screenshot-list li {\n  display: flex;\n  flex-wrap: wrap;\n"
    "  gap: 0.75rem;\n  align-items: flex-start;\n  margin-block: 0.75rem;\n}\n\n"
    ".import-review .screenshot-list li > a {\n  flex: 0 0 4.5rem;\n}\n\n"
    ".import-review .screenshot-list li > div {\n  flex: 1 1 12rem;\n  min-width: 0;\n}\n\n"
    ".import-review .screenshot-list img {\n  display: block;\n  width: 100%;\n  height: auto;\n"
    "  border: 1px solid var(--edge);\n  border-radius: var(--radius-small);\n}\n\n"
    ".import-review .screenshot-list p {\n  margin: 0 0 0.35rem;\n}\n\n"
    ".import-review .steps > label {\n  margin-top: 0.75rem;\n}\n\n"
    "@media (max-width: 30rem) {\n"
    "  .import-review .panel,\n  .import-review .panel article {\n"
    "    padding-inline: clamp(0px, 13vw - 1rem, 1.2rem);\n  }\n\n"
    '  .import-review input[type="date"] {\n'
    "    padding-inline: clamp(2vw, 100000vw - 14650rem - 14500px, 0.85rem);\n  }\n}\n"
)
"""The screenshot review's rules, pinned whole: on a narrow screen with large text its
fieldsets take the page's width, a panel's side padding gives way and the panel reaches into
the page's margin, a card inside a panel gives way without reaching further, a file input is
never wider than its panel and as tall as a control in the page's type at the reader's text
size, its picker's button is its label in the app's words, as tall and with the look of an
outline button, with the browser's own button hidden and the picker set apart from the button
that sends it, school text breaks where its line can't hold it while a
control's words never do, an instruction's correction box sits under its label, two lines
tall to start, a question's words sit inside its box, each screenshot is one compact row
with a fixed thumbnail, and each box of a card's corrections stands apart from the one
above."""
STYLESHEET = pathlib.Path(imports.__file__).parents[1] / "static" / "blossom.css"


def test_the_screenshot_review_s_own_rules_are_pinned_and_only_its_pages_carry_its_class() -> None:
    css = STYLESHEET.read_text(encoding="utf-8")
    carriers = sorted(
        page.name
        for page in (STYLESHEET.parents[1] / "templates").glob("*.html")
        if "import-review" in page.read_text(encoding="utf-8")
    )

    # Strict on purpose, beside the exact rules: any text that names the class changes this
    # count, a harmless comment included, and that upkeep is accepted.
    assert css.count("import-review") == 37
    assert css.count(IMPORT_REVIEW_RULES) == 1
    assert declared_for('.import-review article[tabindex="-1"]:focus') == [ARRIVAL_CUE]
    for landing in ("#told:focus", "#done:focus", "#outcome-heading:focus"):
        assert declared_for(landing) == [ARRIVAL_CUE]
    for landing in (
        '.import-review .choice[tabindex="-1"]',
        '.import-review article[tabindex="-1"]',
    ):
        assert declared_for(landing) == ["scroll-margin-top: 0.75rem;"]
    assert carriers == ["import_find.html", "import_review.html", "parent_review.html"]


# ------------------------------------------------------------------ the cascade at 320 px

NARROW = View(320, browser_text=32, root_text=32)
"""A phone 320 pixels wide with the browser's text at 200%."""
SPACING = ("margin-left", "margin-right", "padding-left", "padding-right", "max-width")
"""The properties the checks below read."""


def spacing_of(name: str, value: str) -> dict[str, str]:
    """The longhands a spacing declaration sets, refusing a form these checks don't read."""
    parts = value.split()
    if name in ("margin", "padding"):
        if not 1 <= len(parts) <= 4 or "(" in value:
            raise UnreadCss(value)
        right = parts[1] if len(parts) > 1 else parts[0]
        left = parts[3] if len(parts) == 4 else right
        return {f"{name}-left": left, f"{name}-right": right}
    if name in ("margin-inline", "padding-inline"):
        box = name.removesuffix("-inline")
        return {f"{box}-left": value, f"{box}-right": value}
    if name in SPACING:
        return {name: value}
    return {}


def spacing_sheet() -> dict[str, list[Rule]]:
    sheet: dict[str, list[Rule]] = {}
    for style in style_rules(STYLESHEET.read_text(encoding="utf-8")):
        declared = [
            (longhand, setting, important)
            for name, value, important in style.declarations
            for longhand, setting in spacing_of(name, value.strip().lower()).items()
        ]
        if not declared:
            continue
        chosen = [selector(unshielded(head)) for head in style.selectors.split(",")]
        for longhand, setting, important in declared:
            for one in chosen:
                sheet.setdefault(longhand, []).append(
                    Rule(one, setting, style.order, style.media, important)
                )
    return sheet


def px(value: str | None, view: View) -> float:
    """A margin or padding in pixels: unset, ``auto`` or ``0``; a length; or ``min`` or
    ``clamp`` of lengths or of their spaced sums and differences, read argument by argument.
    Any other form is refused, never worked out."""
    if value is None or value in ("auto", "0"):
        return 0.0
    called = re.fullmatch(r"(min|clamp)\((.+)\)", value)
    if called is None:
        return length_px(value, view)
    found = [summed_px(one.strip(), view) for one in called.group(2).split(",")]
    if called.group(1) == "min":
        return min(found)
    if len(found) != 3:
        raise UnreadCss(value)
    low, middle, high = found
    return max(low, min(middle, high))


def summed_px(value: str, view: View) -> float:
    """An argument of ``min`` or ``clamp``: a length, or lengths added and taken away with a
    spaced sign. A bare number, a keyword or a function there is refused."""
    terms = re.split(r"\s+([-+])\s+", value)
    total = length_px(terms[0], view)
    for sign, term in zip(terms[1::2], terms[2::2], strict=True):
        total += length_px(term, view) if sign == "+" else -length_px(term, view)
    return total


def length_px(value: str, view: View) -> float:
    """A length in px, rem or vw; anything else is refused."""
    read = re.fullmatch(r"(\d*\.?\d+)(px|rem|vw)", value)
    if read is None:
        raise UnreadCss(value)
    unit = {"px": 1.0, "rem": float(view.root_text), "vw": view.width / 100}
    return float(read.group(1)) * unit[read.group(2)]


def test_the_length_reader_reads_the_review_s_forms_and_refuses_any_other() -> None:
    assert px("clamp(0px, 13vw - 1rem, 1.2rem)", NARROW) == pytest.approx(9.6)
    assert px("min(0px, 13vw - 2.25rem)", NARROW) == pytest.approx(-30.4)
    assert px("auto", NARROW) == px("0", NARROW) == 0.0
    for unknown in ("calc(1px)", "max(1px, 2px)", "clamp(1px, 2px)", "50%", "1em", "1px+2px"):
        with pytest.raises(UnreadCss):
            px(unknown, NARROW)


def spacing(sheet: dict[str, list[Rule]], element: Element, name: str) -> float:
    rules = [rule for rule in sheet.get(name, []) if rule.chosen.matches(element)]
    found = winner(rules, NARROW)
    if found is None and element.tag == "ul" and name == "padding-left":
        return 40.0
    return px(None if found is None else found.value, NARROW)


def left_edge(sheet: dict[str, list[Rule]], element: Element) -> float:
    """Where an element's border box starts from the screen's left edge, with every block
    above it as wide as its page allows."""
    edge = spacing(sheet, element, "margin-left")
    for above in element.ancestors():
        edge += spacing(sheet, above, "margin-left") + spacing(sheet, above, "padding-left")
    return edge


def review_pages(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        family = browser.get("/parent").text
    return {"review": page, "family": family}


def test_at_320_px_with_large_text_no_card_of_the_preview_passes_the_screen_s_edge(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = spacing_sheet()
    page = review_pages(tmp_path, monkeypatch)["review"]
    cards = [e for e in elements_of(page) if e.tag == "article"]

    assert len(cards) == 2
    assert all(left_edge(sheet, card) >= 0 for card in cards)


def test_at_320_px_with_large_text_every_panel_of_the_review_gives_way(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = spacing_sheet()
    page = review_pages(tmp_path, monkeypatch)["review"]
    panels = [e for e in elements_of(page) if e.tag == "section" and "panel" in e.classes]
    narrowest = px("clamp(0px, 13vw - 1rem, 1.2rem)", NARROW)

    assert len(panels) >= 4
    assert all(spacing(sheet, panel, "padding-left") == narrowest for panel in panels)
    assert all(left_edge(sheet, panel) < 40 for panel in panels)


def test_every_file_input_of_screenshot_import_keeps_within_its_panel(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = spacing_sheet()
    pages = review_pages(tmp_path, monkeypatch)
    inputs = [
        e
        for page in pages.values()
        for e in elements_of(page)
        if e.tag == "input" and e.attributes.get("type") == "file"
    ]

    assert len(inputs) == 2
    for one in inputs:
        found = winner(
            [rule for rule in sheet.get("max-width", []) if rule.chosen.matches(one)], NARROW
        )
        assert found is not None
        assert found.value == "100%"


# ------------------------------------------------------------------ who may, and no-store


def every_route(draft_id: str, image_id: str) -> list[tuple[str, str]]:
    return [
        ("POST", "/parent/imports"),
        ("GET", f"/parent/imports/{draft_id}"),
        ("POST", f"/parent/imports/{draft_id}/images"),
        ("GET", f"/parent/imports/{draft_id}/images/{image_id}"),
        ("POST", f"/parent/imports/{draft_id}/images/{image_id}/remove"),
        ("POST", f"/parent/imports/{draft_id}/read"),
        ("POST", f"/parent/imports/{draft_id}/review"),
        ("POST", f"/parent/imports/{draft_id}/find"),
        ("POST", f"/parent/imports/{draft_id}/set-aside"),
        ("POST", f"/parent/imports/{draft_id}/finish"),
        ("POST", f"/parent/imports/{draft_id}/discard"),
        ("POST", f"/parent/imports/{draft_id}/not-hers"),
    ]


def test_every_import_route_is_one_this_module_names() -> None:
    found = {
        (method, route.path)  # type: ignore[attr-defined]
        for route in imports.router.routes
        for method in route.methods  # type: ignore[attr-defined]
    }
    assert found == {
        ("POST", "/parent/imports"),
        ("GET", "/parent/imports/{draft_id}"),
        ("POST", "/parent/imports/{draft_id}/images"),
        ("GET", "/parent/imports/{draft_id}/images/{image_id}"),
        ("POST", "/parent/imports/{draft_id}/images/{image_id}/remove"),
        ("POST", "/parent/imports/{draft_id}/read"),
        ("POST", "/parent/imports/{draft_id}/review"),
        ("POST", "/parent/imports/{draft_id}/find"),
        ("POST", "/parent/imports/{draft_id}/set-aside"),
        ("POST", "/parent/imports/{draft_id}/finish"),
        ("POST", "/parent/imports/{draft_id}/discard"),
        ("POST", "/parent/imports/{draft_id}/not-hers"),
    }


def test_with_the_sign_in_off_another_computer_is_refused_before_any_body(
    tmp_path: pathlib.Path,
) -> None:
    settings = open_settings(tmp_path)
    with here(settings) as browser:
        draft_id = made(browser, RED)
        [image_id] = images_of(browser, draft_id)
    with here(settings, client="192.168.1.20") as elsewhere:
        answers = {
            (method, path): elsewhere.request(method, path, content=b"--unread body")
            for method, path in every_route(draft_id, image_id)
        }
        count = drafts(elsewhere)

    assert {key: answer.status_code for key, answer in answers.items()} == dict.fromkeys(
        answers, 403
    )
    assert all(says(answer.text, imports.SAID["only_here"]) for answer in answers.values())
    assert all(answer.headers["cache-control"] == "no-store" for answer in answers.values())
    assert count == 1


def test_with_the_sign_in_on_her_and_no_one_are_refused_and_a_parent_is_served(
    tmp_path: pathlib.Path,
) -> None:
    settings = signed_in_with_import(tmp_path)
    with TestClient(
        create_app(settings), follow_redirects=False, headers={"Origin": "http://testserver"}
    ) as parent:
        signed_in(parent, THEIRS)
        draft_id = made(parent, RED)
        [image_id] = images_of(parent, draft_id)
        served = parent.get(f"/parent/imports/{draft_id}")
        image = parent.get(f"/parent/imports/{draft_id}/images/{image_id}")
    with TestClient(
        create_app(settings), follow_redirects=False, headers={"Origin": "http://testserver"}
    ) as her:
        signed_in(her, HERS)
        hers = {
            route: her.request(route[0], route[1], content=b"--unread body")
            for route in every_route(draft_id, image_id)
        }
    with TestClient(
        create_app(settings), follow_redirects=False, headers={"Origin": "http://testserver"}
    ) as nobody:
        nobodys = {
            route: nobody.request(route[0], route[1], content=b"--unread body")
            for route in every_route(draft_id, image_id)
        }

    assert served.status_code == 200
    assert served.headers["cache-control"] == "no-store"
    assert image.headers["cache-control"] == "no-store"
    assert {answer.status_code for answer in hers.values()} == {403}
    assert {answer.status_code for answer in nobodys.values()} <= {303, 401}


def test_every_import_answer_is_no_store_with_the_sign_in_off(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        [image_id] = images_of(browser, draft_id)
        form = read_form(browser.get(f"/parent/imports/{draft_id}").text)
        answers = [
            upload(browser, RED),
            upload(browser, gif()),
            browser.get(f"/parent/imports/{draft_id}"),
            browser.get(f"/parent/imports/{'f' * 32}"),
            browser.get(f"/parent/imports/{draft_id}/images/{image_id}"),
            browser.post(f"/parent/imports/{draft_id}/read", data=form),
            browser.post(f"/parent/imports/{draft_id}/images/{image_id}/remove"),
        ]

    assert [answer.headers["cache-control"] for answer in answers] == ["no-store"] * len(answers)


SCREENSHOT_SUMMARY: Final = (
    "Up to 10 screenshots per review, PNG or JPEG, 12 MB each. Screenshots are sent to "
    "Anthropic's API only when you press Read."
)
"""The approved line above the picker, written out here rather than taken from the code, so a
change to its words fails here."""
SCREENSHOT_DISCLOSURE: Final = (
    "When you press Read, Blossom sends the screenshots to Anthropic's API to be read. A "
    "review's screenshots, what Blossom read from them, and any progress saved for later are "
    "removed from Blossom's database file when the review finishes, is discarded, or expires, "
    "48 hours after it starts. The removal can wait for Blossom's next hourly cleanup, or its "
    "next start. It doesn't guarantee they are erased from the disk, and a backup taken while "
    "the review was open may still hold copies."
)
"""How screenshots are kept, word for word as approved, written out here for the same reason."""


def words_of(text: str) -> str:
    """Rendered text with each run of whitespace read as one space."""
    return " ".join(text.split())


def test_family_review_offers_screenshots_while_import_is_on(tmp_path: pathlib.Path) -> None:
    with here(open_settings(tmp_path)) as browser:
        page = browser.get("/parent")
        fields = dict(
            re.findall(r'<input type="hidden" name="(token|issued)" value="([^"]+)">', page.text)
        )
        files = [("screenshots", ("shot-0.png", RED, "image/png"))]
        sent = browser.post("/parent/imports", data=fields, files=files)
        listed = browser.get("/parent").text

    assert page.status_code == 200
    assert says(page.text, "Add screenshots")
    assert says(page.text, imports.ASIDE)
    assert says(page.text, SCREENSHOT_SUMMARY)
    assert says(page.text, SCREENSHOT_DISCLOSURE)
    assert (
        '<form method="post" action="/parent/imports" enctype="multipart/form-data">' in page.text
    )
    assert sent.status_code == 303
    draft_id = sent.headers["location"].rsplit("/", 1)[-1]
    assert f'href="/parent/imports/{draft_id}"' in listed
    assert "Homework screenshots from " in listed
    assert "Available until " in listed


def test_family_review_keeps_the_screenshot_words_short_and_the_picker_in_its_own_words(
    tmp_path: pathlib.Path,
) -> None:
    # The limits and when screenshots are sent stay in view; how they are kept folds under its
    # summary, word for word; the paste hint moves to the paste; and the picker's button is the
    # field's label, worded as the app's buttons are.
    with here(open_settings(tmp_path)) as browser:
        page = browser.get("/parent").text
    found = elements_of(page)
    by_id = {element.attributes["id"]: element for element in found if "id" in element.attributes}

    def fold_of(element: Element) -> Element:
        return next(above for above in element.ancestors() if above.tag == "details")

    summary = by_id["screenshot-summary"]
    assert words_of(summary.text) == SCREENSHOT_SUMMARY
    assert fold_of(summary) is by_id["add-assignments"]
    disclosure = by_id["screenshot-disclosure"]
    assert words_of(disclosure.text) == SCREENSHOT_DISCLOSURE
    fold = fold_of(disclosure)
    assert fold is not by_id["add-assignments"]
    assert fold_of(fold) is by_id["add-assignments"]
    assert "open" not in fold.attributes
    assert [e.text for e in found if e.tag == "summary" and e.parent is fold] == [
        "How screenshots are kept"
    ]
    assert not says(page, imports.LIMITS)

    picker = by_id["screenshot-files"]
    labels = [
        e for e in found if e.tag == "label" and e.attributes.get("for") == "screenshot-files"
    ]
    assert [(e.text, e.attributes.get("class")) for e in labels] == [
        ("Choose screenshots", "file-choose")
    ]
    assert picker.parent is labels[0].parent
    assert picker.parent is not None
    assert picker.parent.classes == {"file-pick"}
    assert picker.attributes["aria-describedby"] == "screenshot-summary screenshot-disclosure"

    headings = [e for e in found if e.tag == "h2"]
    paste = next(e for e in headings if e.text == "Paste school text")
    hint = next(e for e in found if e.tag == "p" and e.text.strip() == imports.ASIDE)
    assert paste.position < hint.position
    assert paste.parent is hint.parent
    assert not any("import-review" in above.classes for above in hint.ancestors())


def test_family_review_from_another_computer_with_the_sign_in_off_says_where_to_add_them(
    tmp_path: pathlib.Path,
) -> None:
    with here(open_settings(tmp_path), client="192.168.1.20") as browser:
        page = browser.get("/parent")

    assert page.status_code == 200
    assert says(page.text, imports.SAID["only_here"])
    assert 'action="/parent/imports"' not in page.text


def test_an_expired_review_says_so_and_takes_nothing(tmp_path: pathlib.Path) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        clock = state_of(browser).import_drafts._clock
        later = clock.now() + DRAFT_LIFETIME
        monkey = pytest.MonkeyPatch()
        monkey.setattr(clock, "now", lambda: later)
        try:
            page = browser.get(f"/parent/imports/{draft_id}")
            refused = upload(browser, BLUE, form={}, to=f"/parent/imports/{draft_id}/images")
        finally:
            monkey.undo()

    assert page.status_code == 410
    assert says(page.text, imports.SAID["expired"])
    assert refused.status_code == 410


# ------------------------------------------------------------------ screenshot import turned off

UNREAD_BODY = b"--unread body" * 600_000
"""Far over an upload's limits: read, it would be refused as too large rather than missing."""
METHODS: Final = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")
"""Every method a request can name here, the routes' own and the rest."""
MISSING: Final = "/parent/no-such-address"


def as_answered(answer: Answer) -> tuple[int, str, list[tuple[str, str]]]:
    """An answer's status, body and every header, in order."""
    return answer.status_code, answer.text, sorted(answer.headers.items())


def test_turned_off_every_import_address_answers_as_a_missing_one_before_any_body(
    tmp_path: pathlib.Path,
) -> None:
    """Off by default, each import address, with every method, answers exactly as an address
    that doesn't exist does with that method: its status, its body and every header, with no
    ``Allow`` naming a method; an upload far over the limits is unread, and no review is made."""
    settings = open_settings(tmp_path, turned_on=False)
    assert settings.screenshot_import is False
    with here(settings) as browser:
        missing = {
            method: as_answered(browser.request(method, MISSING, content=UNREAD_BODY))
            for method in METHODS
        }
        answers = {
            (method, path): as_answered(browser.request(method, path, content=UNREAD_BODY))
            for _, path in every_route("0" * 32, "1" * 32)
            for method in METHODS
        }
        count = drafts(browser)

    assert len(answers) == 84
    assert {method: answer[0] for method, answer in missing.items()} == dict.fromkeys(METHODS, 404)
    assert answers == {(method, path): missing[method] for method, path in answers}
    assert all("allow" not in dict(answer[2]) for answer in answers.values())
    assert all("cache-control" not in dict(answer[2]) for answer in answers.values())
    assert count == 0


def test_turned_off_a_signed_in_parent_finds_no_import_address(tmp_path: pathlib.Path) -> None:
    with TestClient(
        create_app(signed_in_household(tmp_path)),
        follow_redirects=False,
        headers={"Origin": "http://testserver"},
    ) as parent:
        signed_in(parent, THEIRS)
        missing = {
            method: as_answered(parent.request(method, MISSING, content=b"--unread body"))
            for method in METHODS
        }
        answers = {
            (method, path): as_answered(parent.request(method, path, content=b"--unread body"))
            for _, path in every_route("0" * 32, "1" * 32)
            for method in METHODS
        }
        count = drafts(parent)

    assert len(answers) == 84
    assert answers == {(method, path): missing[method] for method, path in answers}
    assert all("allow" not in dict(answer[2]) for answer in answers.values())
    assert count == 0


def test_the_family_review_page_offers_no_screenshot_import_while_off(
    tmp_path: pathlib.Path,
) -> None:
    """While off, the page names no import address and says nothing of screenshots."""
    with here(open_settings(tmp_path, turned_on=False)) as browser:
        page = browser.get("/parent")

    assert page.status_code == 200
    assert "/parent/imports" not in page.text
    assert "screenshot" not in page.text.lower()


# ------------------------------------------------------------------ usable corrections


@pytest.mark.parametrize("typed", ["10/14", "2026-02-30", "nonsense"])
def test_an_unreadable_typed_date_leaves_the_field_open_and_a_save_writes_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, typed: str
) -> None:
    unsure = [{"field": "footer", "reason": "blurred"}]
    answer = homework_answer(cards=[homework_card(uncertain=unsure)])
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        page = her_name_answered(browser, draft_id, page)
        [subject] = fields_named(review_form(page), "answer:confirm:")
        box = {f"correct:{subject}": typed}
        kept = followed(browser, send(browser, draft_id, page_post(page, "keep", typed=box)))
        [unit] = digests_on(kept)
        before = assignment_count(browser)
        forced = followed(
            browser, send(browser, draft_id, page_post(kept, "save", tick=[unit], typed=box))
        )
        unwritten = assignment_count(browser)
        assert units_on(kept) == []
        assert says(kept, screenshot_page.OPEN_NAMES["confirm"])
        assert unwritten == before
        confirm = {f"answer:confirm:{subject}": "as_read"}
        followed(
            browser,
            send(
                browser,
                draft_id,
                page_post(forced, "save", tick=[unit], typed=box, choose=confirm),
            ),
        )
        due = saved_due(browser, "Sentence Composing")

    assert units_on(forced) == []
    assert says(forced, screenshot_page.OPEN_NAMES["confirm"])
    assert due == date(2026, 10, 8)


# ------------------------------------------------------------------ what still needs checking


def acceptance_row(browser: TestClient, acceptance: str) -> tuple[object, ...]:
    found = (
        state_of(browser)
        .project_state._connection.execute(
            "SELECT left_to_check, complete FROM grade_acceptances WHERE acceptance_id = ?",
            (acceptance,),
        )
        .fetchone()
    )
    return tuple(found or ())


def open_rows(browser: TestClient, draft_id: str) -> list[str]:
    student = state_of(browser).project_state.student_id()
    snapshot = state_of(browser).import_drafts.snapshot(student, draft_id)
    return [row.key for row in snapshot.rows if row.state == "open"]


def left_shapes() -> dict[str, tuple[tuple[bytes, object], ...]]:
    monday, tuesday = split_answers()
    lab = homework_answer(
        cards=[homework_card(course="Science 8", title="Lab Report", footer="Due (10/9/2026)")]
    )
    return {
        "a split question open": ((RED, monday), (BLUE, tuesday), (GREEN, lab)),
        "two cards it couldn't read": ((RED, unreadable_answer()),),
        "nothing left": ((RED, homework_answer(cards=[homework_card()])),),
    }


@pytest.mark.parametrize(
    ("shape", "said", "left"),
    [
        ("a split question open", "Saved: 1 new assignment. 1 still needs checking.", 1),
        ("two cards it couldn't read", "Saved: 1 new assignment. 2 still need checking.", 2),
        ("nothing left", "Saved: 1 new assignment.", 0),
    ],
)
def test_still_needs_checking_counts_every_open_piece_split_question_and_unreadable_card(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, shape: str, said: str, left: int
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, *left_shapes()[shape])
        page = her_name_answered(browser, draft_id, page)
        saved = followed(
            browser, send(browser, draft_id, page_post(page, "save", tick=units_on(page)))
        )
        recorded = acceptance_row(browser, acceptance_of(page))
        still = open_rows(browser, draft_id)

    assert says(saved, said)
    assert recorded == (left, int(left == 0))
    assert (left == 0) == (not still)


# ------------------------------------------------------------------ a recorded acceptance


def test_a_recorded_acceptance_answers_only_for_its_own_review(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = "Saved: 1 new assignment."
    with here(open_settings(tmp_path)) as browser:
        mine, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        page = her_name_answered(browser, mine, page)
        acceptance = acceptance_of(page)
        saved = followed(browser, send(browser, mine, page_post(page, "save", tick=units_on(page))))
        lab = homework_answer(cards=[homework_card(course="Science 8", title="Lab Report")])
        closed, other = read_review(browser, monkeypatch, (GREEN, lab))
        press_outside(browser, other, "discard")
        opened, _ = read_review(browser, monkeypatch, (BLUE, lab))
        before = assignment_count(browser)
        resent = [("acceptance", acceptance), ("press", "save")]
        absent = send(browser, "f" * 32, resent)
        to_closed = send(browser, closed, resent)
        to_open = followed(browser, send(browser, opened, resent))
        again = followed(browser, send(browser, mine, resent))
        after = assignment_count(browser)

    assert says(saved, outcome)
    assert absent.status_code == 404
    assert not says(absent.text, outcome)
    assert says(to_closed.text, imports.SAID["closed"])
    assert not says(to_closed.text, outcome)
    assert not says(to_open, outcome)
    assert says(to_open, imports.PRESS_SAID["save_invalid"])
    assert says(again, outcome)
    assert after == before


# ------------------------------------------------------------------ addresses carry only ids

IMPORT_TEMPLATES: Final = pathlib.Path(imports.__file__).resolve().parent.parent / "templates"


def test_no_import_address_carries_anything_but_ids() -> None:
    """Closed-world: the module reads no query string, no import page has a form that isn't a
    post or a link with a query, and the only reads are the review and its images."""
    tree = ast.parse(pathlib.Path(imports.__file__).read_text(encoding="utf-8"))
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    pages = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(IMPORT_TEMPLATES.glob("import_*.html"))
    }
    forms = {name: re.findall(r"<form\b[^>]*>", text) for name, text in pages.items()}
    reads = sorted(
        route.path  # type: ignore[attr-defined]
        for route in imports.router.routes
        for method in route.methods  # type: ignore[attr-defined]
        if method == "GET"
    )

    assert "query_params" not in names
    assert sorted(pages) == [
        "import_find.html",
        "import_review.html",
        "import_rows.html",
        "import_said.html",
    ]
    assert [(n, f) for n, found in forms.items() for f in found if 'method="post"' not in f] == []
    assert [n for n, text in pages.items() if re.search(r'href="[^"]*\?', text)] == []
    assert reads == ["/parent/imports/{draft_id}", "/parent/imports/{draft_id}/images/{image_id}"]


def find_button(page: str, piece: str) -> str:
    """The id of the small form a card's "Find under another name…" button posts."""
    found = re.search(
        rf'<form method="post" action="[^"]+/find" id="([^"]+)">\s*'
        rf'<input type="hidden" name="piece" value="{re.escape(str(escape(piece)))}">',
        page,
    )
    assert found is not None
    return found.group(1)


def test_a_live_access_log_holds_no_search_text(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "SYNTHETIC_PRIVATE_HOMEWORK_SEARCH"
    monday, tuesday = split_answers()
    records: list[str] = []

    class Kept(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    access = logging.getLogger("uvicorn.access")
    handler = Kept()
    access.addHandler(handler)
    level = access.level
    access.setLevel(logging.INFO)
    try:
        with here(open_settings(tmp_path)) as browser:
            draft_id, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, tuesday))
            page = her_name_answered(browser, draft_id, page)
            [split] = fields_named(review_form(page), "answer:split:")
            apart = next(
                v
                for v in re.findall(r'name="answer:split:[^"]+" value="([^"]*)"', page)
                if "split" in html.unescape(v)
            )
            choose = {f"answer:split:{split}": html.unescape(apart)}
            page = followed(
                browser, send(browser, draft_id, page_post(page, "keep", choose=choose))
            )
            pieces = fields_named(review_form(page), "answer:destination:")
            _, fields = outside(page, find_button(page, pieces[0]))
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            server = uvicorn.Server(
                uvicorn.Config(
                    browser.app,
                    host="127.0.0.1",
                    port=port,
                    log_level="info",
                    access_log=True,
                    log_config=None,
                    lifespan="off",
                    ws="none",
                )
            )
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            for _ in range(200):
                if server.started:
                    break
                time.sleep(0.05)
            body = urlencode([*fields, ("words", sentinel)])
            headers = {
                "Host": LOOPBACK,
                "Origin": f"http://{LOOPBACK}",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("GET", f"/parent/imports/{draft_id}", headers={"Host": LOOPBACK})
            connection.getresponse().read()
            connection.request(
                "POST", f"/parent/imports/{draft_id}/find", body=body, headers=headers
            )
            answer = connection.getresponse()
            shown = answer.read().decode()
            connection.close()
            server.should_exit = True
            thread.join(timeout=10)
    finally:
        access.removeHandler(handler)
        access.setLevel(level)

    assert any("|" in piece and "2026-10-0" in piece for piece in pieces)
    assert answer.status == 200
    assert sentinel in shown
    assert any("/find" in line for line in records)
    assert not [line for line in records if sentinel in line or "2026-10-0" in line]


def test_find_under_another_name_is_a_button_whose_search_posts_and_whose_choice_is_kept(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = Assignment(
        assignment_id="assignment-grammar-practice-eeee4444",
        course="English 8",
        title="Grammar Practice",
        due_date=date(2026, 10, 8),
        dependencies=[],
        reported_submission_status="not_reported",
        assigned_on=None,
        kind=AssignmentKind.HOMEWORK,
    )
    with here(open_settings(tmp_path)) as browser:
        state_of(browser).project_state.upsert_assignments([other])
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        piece = fields_named(review_form(page), "answer:destination:")[0]
        form_id = find_button(page, piece)
        opened = press_outside(browser, page, form_id)
        search = re.search(
            r'<form method="post" action="([^"]+/find)">(.*?)</form>', opened.text, re.S
        )
        assert search is not None
        hidden = re.findall(
            r'<input type="hidden" name="([^"]+)" value="([^"]*)">', search.group(2)
        )
        results = browser.post(
            search.group(1),
            data={**{html.unescape(n): html.unescape(v) for n, v in hidden}, "words": "grammar"},
        )
        chosen = re.search(
            r'<form method="post" action="[^"]+/review" class="choice">.*?</form>',
            results.text,
            re.S,
        )
        assert chosen is not None
        fields = [
            (html.unescape(n), html.unescape(v))
            for n, v in re.findall(r'name="([^"]+)" value="([^"]*)"', chosen.group(0))
        ]
        after = followed(browser, send(browser, draft_id, fields))

    assert f'form="{form_id}" aria-describedby="typing-lost">Find under another name' in page
    assert says(page.split('id="typing-lost"', 1)[1], screenshot_page.SAID["typing_lost"])
    assert opened.status_code == 200
    assert 'name="words"' in search.group(2)
    assert results.status_code == 200
    assert says(results.text, "English 8: Grammar Practice, due Thu 10/8")
    assert "Found under another name: Grammar Practice, due Thu 10/8" in html.unescape(after)


# ------------------------------------------------------------------ discard on every open review


def read_as(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch, answer: ScreenshotAnswer
) -> tuple[str, str]:
    reader = Reader(monkeypatch)
    reader.answers[RED] = answer
    draft_id = made(browser, RED)
    page = browser.get(f"/parent/imports/{draft_id}").text
    return draft_id, browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page)).text


def unread_with_a_key(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    return made(browser, RED)


def a_failed_read(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    return read_as(browser, monkeypatch, ScreenshotAnswer(None, "not_json", 10, 5))[0]


def wrong_kind_only(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    unsupported = homework_answer(image_kind="unsupported")
    return read_as(browser, monkeypatch, ScreenshotAnswer(unsupported, None, 10, 5))[0]


def a_list_with_no_cards(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    empty = homework_answer(cards=[])
    return read_as(browser, monkeypatch, ScreenshotAnswer(empty, None, 10, 5))[0]


def emptied(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    draft_id = made(browser, RED)
    [image_id] = images_of(browser, draft_id)
    browser.post(f"/parent/imports/{draft_id}/images/{image_id}/remove")
    return draft_id


def a_read_review(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    return read_review(browser, monkeypatch, (RED, homework_answer()))[0]


def frozen_after_a_partial_save(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
    page = her_name_answered(browser, draft_id, page)
    send(browser, draft_id, page_post(page, "save", tick=units_on(page)[:1]))
    return draft_id


OPEN_REVIEWS: Final = {
    "unread with a key": (unread_with_a_key, True),
    "no key": (unread_with_a_key, False),
    "a failed read": (a_failed_read, True),
    "wrong kind only": (wrong_kind_only, True),
    "a list with no cards": (a_list_with_no_cards, True),
    "emptied of its only image": (emptied, True),
    "a read review": (a_read_review, True),
    "frozen after a partial save": (frozen_after_a_partial_save, True),
}


@pytest.mark.parametrize("case", list(OPEN_REVIEWS))
def test_every_open_review_offers_discard_the_review(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    build, key = OPEN_REVIEWS[case]
    with here(open_settings(tmp_path, key=key)) as browser:
        draft_id = build(browser, monkeypatch)
        page = browser.get(f"/parent/imports/{draft_id}").text
        gone = press_outside(browser, page, "discard")
        student = state_of(browser).project_state.student_id()
        after = state_of(browser).import_drafts.snapshot(student, draft_id)
        again = browser.get(f"/parent/imports/{draft_id}").text

    assert page.count(">Discard the review</button>") == 1
    assert gone.status_code == 200
    assert says(gone.text, imports.AFTER_SAID["discarded"])
    assert after.closed_at is not None
    assert after.images == ()
    assert says(again, imports.SAID["closed"])
    assert "Discard the review" not in again


# ------------------------------------------------------------------ which date a correction sets


def mixed_row(*unsure: str) -> dict[str, object]:
    """One item read as an assigned card under a Monday heading the reader wasn't sure of, due
    10/8, and as the due card under Thursday 10/8 whose own due line says 10/9."""
    return homework_answer(
        headings=[
            {"text": "Monday 10/5/2026", "y": 150, "uncertain": "faint"},
            {"text": "Thursday 10/8/2026", "y": 400, "uncertain": None},
        ],
        cards=[
            homework_card(
                badge="ASSIGNED",
                footer="Due (10/8/2026)",
                y_top=200,
                y_bottom=300,
                uncertain=[{"field": field, "reason": "blurred"} for field in unsure],
            ),
            homework_card(badge="DUE", footer="Due (10/9/2026)", y_top=450, y_bottom=550),
        ],
    )


def boxes_of(page: str) -> dict[str, str]:
    """Each correction box's subject, by its field, on a review of one card."""
    return {json.loads(s)[1]: s for s in fields_named(review_form(page), "correct:")}


BOX_FOR: Final = {"footer": "due", "heading": "assigned"}
"""The box of a card's own date that corrects an unsure due line or day heading."""


def due_choice(page: str, said: str) -> str:
    found = re.search(rf'value="([^"]+)"> <span>{re.escape(said)}</span>', review_form(page))
    assert found is not None
    return html.unescape(found.group(1))


def written(browser: TestClient) -> tuple[date | None, date | None]:
    [found] = [
        a
        for a in state_of(browser).project_state.all_assignments()
        if a.title == "Sentence Composing"
    ]
    return found.due_date, found.assigned_on


ASKED = "Which due date does Blossom use?"


@pytest.mark.parametrize(
    "case", ["assigned date typed", "with the due date chosen", "due date typed"]
)
def test_an_assigned_date_correction_leaves_the_due_date_question_and_its_choice(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    unsure = ("footer",) if case == "due date typed" else ()
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, mixed_row(*unsure)))
        page = her_name_answered(browser, draft_id, page)
        boxes = boxes_of(page)
        [piece] = fields_named(review_form(page), "answer:destination:")
        typed = {f"correct:{boxes['assigned']}": "2026-10-06"}
        choose: dict[str, str] = {}
        if case == "with the due date chosen":
            choose = {
                f"answer:due_read:{piece}": due_choice(page, "Due Fri 10/9, as the due line says")
            }
        if case == "due date typed":
            typed = {f"correct:{boxes['due']}": "2026-10-09"}
            heading = json.dumps([piece, "heading"], separators=(",", ":"))
            choose = {f"answer:confirm:{heading}": "as_read"}
        kept = followed(
            browser, send(browser, draft_id, page_post(page, "keep", typed=typed, choose=choose))
        )
        [unit] = digests_on(kept)
        before = assignment_count(browser)
        followed(
            browser, send(browser, draft_id, page_post(kept, "save", tick=[unit], typed=typed))
        )
        after = assignment_count(browser)
        dates = written(browser) if after > before else None

    assert ASKED in html.unescape(review_form(page))
    if case == "assigned date typed":
        assert ASKED in html.unescape(review_form(kept))
        assert units_on(kept) == []
        assert after == before
    elif case == "with the due date chosen":
        assert units_on(kept) == [unit]
        assert dates == (date(2026, 10, 9), date(2026, 10, 6))
    else:
        assert ASKED not in html.unescape(review_form(kept))
        assert dates == (date(2026, 10, 9), date(2026, 10, 5))


# ------------------------------------------------------------------ the returned form


@pytest.mark.parametrize("field", ["footer", "heading"])
def test_the_returned_page_posted_unchanged_keeps_a_date_blossom_can_t_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    answer = (
        homework_answer(cards=[homework_card(uncertain=[{"field": "footer", "reason": "blurred"}])])
        if field == "footer"
        else mixed_row()
    )
    refused = "The date given was “10/14”, which is not a date Blossom can read."
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        page = her_name_answered(browser, draft_id, page)
        box = boxes_of(page)[BOX_FOR[field]]
        [piece] = fields_named(review_form(page), "answer:destination:")
        key = json.loads(box)[0]
        kept = followed(
            browser,
            send(browser, draft_id, page_post(page, "keep", typed={f"correct:{box}": "10/14"})),
        )
        followed(browser, send(browser, draft_id, page_post(kept, "later")))
        student = state_of(browser).project_state.student_id()
        progress = state_of(browser).import_drafts.snapshot(student, draft_id).progress
        reloaded = browser.get(f"/parent/imports/{draft_id}").text
        again = followed(browser, send(browser, draft_id, page_post(reloaded, "keep")))
        held = stored_answers(browser, draft_id)
        fixed = followed(
            browser,
            send(
                browser, draft_id, page_post(again, "keep", typed={f"correct:{box}": "2026-10-12"})
            ),
        )

    saved = screenshot_posts.progress_read(progress)
    assert saved is not None
    assert saved.corrected[key][BOX_FOR[field]] == "10/14"
    for shown in (kept, reloaded, again):
        assert says(shown, refused)
        assert says(shown, screenshot_page.OPEN_NAMES["confirm"])
        assert not says(shown, screenshot_page.SAID["lost_correction"])
    assert not says(reloaded, screenshot_page.SAID["not_saved_yet"])
    assert ("destination", piece) in held
    assert 'value="2026-10-12"' in fixed
    assert not says(fixed, refused)
    assert not says(fixed, screenshot_page.OPEN_NAMES["confirm"])


# ------------------------------------------------------------------ the name checkbox


def test_these_are_her_homework_is_a_checkbox_a_browser_can_untick(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unnamed = homework_answer(
        active_student={"name": None, "uncertain": None}, cards=[homework_card()]
    )
    field = "answer:name:missing"
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, unnamed))
        kept = followed(
            browser, send(browser, draft_id, page_post(page, "keep", choose={field: "confirmed"}))
        )
        unticked = [f for f in page_post(kept, "save", tick=units_on(kept)) if f[0] != field]
        saved = followed(browser, send(browser, draft_id, unticked))
        answers = stored_answers(browser, draft_id)

    assert re.findall(rf'<input type="(\w+)" name="{field}"', page) == ["checkbox"]
    assert f'type="checkbox" name="{field}" value="confirmed" checked' in kept
    assert ("name", "missing") not in answers
    assert units_on(saved) == []


# ------------------------------------------------------------------ school text wraps

WRAPS: Final = frozenset({"anywhere", "normal", "break-word"})
"""The `overflow-wrap` values the checklist below reads; any other is refused."""
BREAKS_WORDS: Final = ("word-break", "hyphens", "white-space")
"""Properties that also decide where a line breaks, which the checklist doesn't read; a rule
of the review's own that sets one is refused."""


def wrap_sheet() -> list[Rule]:
    """Every `overflow-wrap` declaration of the sheet, under its selectors, read whole: a value
    outside `WRAPS` is refused, and so is a selector the reader can't read."""
    rules: list[Rule] = []
    for style in style_rules(STYLESHEET.read_text(encoding="utf-8")):
        if "import-review" in style.selectors and any(
            name in BREAKS_WORDS for name, _, _ in style.declarations
        ):
            raise UnreadCss(style.selectors)
        declared = [
            (value.strip().lower(), important)
            for name, value, important in style.declarations
            if name in ("overflow-wrap", "word-wrap")
        ]
        if not declared:
            continue
        chosen = [selector(unshielded(head)) for head in style.selectors.split(",")]
        for value, important in declared:
            if value not in WRAPS:
                raise UnreadCss(value)
            rules += [Rule(one, value, style.order, style.media, important) for one in chosen]
    return rules


def wrapping(rules: list[Rule], element: Element, view: View) -> str:
    """The `overflow-wrap` an element ends with: its own winning rule, else its nearest
    ancestor's, since the property is inherited, else `normal`."""
    for one in [element, *element.ancestors()]:
        found = winner([rule for rule in rules if rule.chosen.matches(one)], view)
        if found is not None:
            return found.value
    return "normal"


def long_text_pages(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The pages that show school text: a review whose card was found a row it would copy the
    words of, a search's results, and a partly saved review with cards it couldn't read."""
    later_card = homework_answer(cards=[homework_card(footer="Due (10/30/2026)")])
    found_row = Assignment(
        assignment_id="assignment-grammar-practice-hhhh7777",
        course="English 8",
        title="Grammar Practice",
        due_date=date(2026, 10, 30),
        dependencies=[],
        reported_submission_status="not_reported",
        assigned_on=None,
        kind=AssignmentKind.HOMEWORK,
    )
    with here(open_settings(tmp_path)) as browser:
        first, page = read_review(browser, monkeypatch, (BLUE, later_card))
        page = her_name_answered(browser, first, page)
        send(browser, first, page_post(page, "save", tick=units_on(page)))
        state_of(browser).project_state.upsert_assignments([found_row])
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        [piece] = fields_named(review_form(page), "answer:destination:")
        opened = press_outside(browser, page, find_button(page, piece))
        search = re.search(
            r'<form method="post" action="([^"]+/find)">(.*?)</form>', opened.text, re.S
        )
        assert search is not None
        hidden = re.findall(
            r'<input type="hidden" name="([^"]+)" value="([^"]*)">', search.group(2)
        )
        results = browser.post(
            search.group(1),
            data={**{html.unescape(n): html.unescape(v) for n, v in hidden}, "words": "grammar"},
        ).text
        keep(
            browser,
            draft_id,
            (
                "destination",
                piece,
                json.dumps({"found": found_row.assignment_id}, separators=(",", ":")),
            ),
        )
        refused = browser.get(f"/parent/imports/{draft_id}").text
        items, page = read_review(browser, monkeypatch, (GREEN, unreadable_answer()))
        send(browser, items, page_post(page, "save", tick=units_on(page)))
        partly = browser.get(f"/parent/imports/{items}").text
    return {"find": results, "review": refused, "partly saved": partly}


def in_section(element: Element, heading: str) -> bool:
    return any(
        above.tag == "section" and above.attributes.get("aria-labelledby") == heading
        for above in element.ancestors()
    )


SCHOOL_TEXT: Final[dict[str, tuple[str, Callable[[Element], bool]]]] = {
    "the find result": (
        "find",
        lambda e: e.tag == "p" and e.parent is not None and "choice" in e.parent.classes,
    ),
    "the find card's note": (
        "find",
        lambda e: e.tag == "p" and "note" in e.classes and in_section(e, "find-heading"),
    ),
    "the legends": ("review", lambda e: e.tag == "legend"),
    "the card's class": ("review", lambda e: e.tag == "p" and "course" in e.classes),
    "the card's title": ("review", lambda e: e.tag == "h3" and in_section(e, "saving-heading")),
    "the school's words": ("review", lambda e: "from-teacher" in e.classes),
    "the notes, the Type's lines among them": (
        "review",
        lambda e: e.tag == "p" and "note" in e.classes and in_section(e, "saving-heading"),
    ),
    "the facts and the card's source": ("review", lambda e: "source" in e.classes),
    "the preview's lines": ("review", lambda e: "effect" in e.classes),
    "the problems": ("review", lambda e: "problem" in e.classes),
    "the saved cards": ("partly saved", lambda e: e.tag == "li" and in_section(e, "saved-heading")),
    "the cards it couldn't read": (
        "partly saved",
        lambda e: e.tag == "li" and in_section(e, "unread-heading"),
    ),
}
"""The closed checklist: each element of the review and find pages that shows school text, by
the page it is on."""


def test_every_school_text_on_the_review_and_find_pages_wraps(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = wrap_sheet()
    pages = {
        name: elements_of(page) for name, page in long_text_pages(tmp_path, monkeypatch).items()
    }
    views = (View(320), NARROW)
    found = {
        name: [e for e in pages[page] if test(e)] for name, (page, test) in SCHOOL_TEXT.items()
    }
    controls = [e for elements in pages.values() for e in elements if e.tag in ("button", "input")]

    assert [name for name, elements in found.items() if not elements] == []
    assert [
        (name, e.tag, e.text[:40], view.browser_text)
        for name, elements in found.items()
        for e in elements
        for view in views
        if wrapping(rules, e, view) != "anywhere"
    ] == []
    assert controls
    assert [
        (e.tag, e.attributes.get("name", e.text)[:40])
        for e in controls
        for view in views
        if wrapping(rules, e, view) == "anywhere"
    ] == []


# ------------------------------------------------------------ signed fields, whatever they hold


def said_lines(page: str) -> list[str]:
    """What a page says first: the review's note, or a said page's lines."""
    found = re.search(r'<div class="problem" role="status" id="said">(.*?)</div>', page, re.S)
    if found is None:
        found = re.search(
            r'<section class="panel">\s*(<p class="problem".*?)</section>', page, re.S
        )
    if found is None:
        return []
    return [html.unescape(line) for line in re.findall(r"<p[^>]*>([^<]*)</p>", found.group(1))]


def draft_state(browser: TestClient, draft_id: str) -> tuple[object, ...]:
    student = state_of(browser).project_state.student_id()
    snapshot = state_of(browser).import_drafts.snapshot(student, draft_id)
    return (
        snapshot.version,
        snapshot.closed_at,
        len(snapshot.images),
        snapshot.discarded,
        snapshot.progress_revision,
        snapshot.progress,
        tuple(sorted(stored_answers(browser, draft_id).items())),
    )


SIGNED_SITES: Final = (
    "the review form",
    "an offer",
    "Read",
    "the upload note",
    "Leave this group unsaved",
    "Finish without the rest",
    "Discard the review",
    "Not hers",
)
"""Each signed value an import route compares: the review form's `check`, an offer's or found
row's `offer`, Read's `check`, the upload note cookie, and each small form's `check`."""
SMALL_FORMS: Final = {
    "Leave this group unsaved": "leave-1",
    "Finish without the rest": "finish",
    "Discard the review": "discard",
    "Not hers": "not-hers",
}


def signed_answer(
    browser: TestClient, draft_id: str, page: str, site: str, signature: str
) -> tuple[int, list[str]]:
    """The route's answer to a press whose signed value is ``signature``: its status, and what
    the page it answers with, or leads to, says first."""
    form = {"content-type": "application/x-www-form-urlencoded"}
    if site == "the upload note":
        cookie = f"{imports.NOTE_COOKIE}=abc.{signature}".encode()
        answer = browser.get(f"/parent/imports/{draft_id}", headers={b"Cookie": cookie})
        return answer.status_code, said_lines(answer.text)
    if site == "the review form":
        fields = [(n, signature if n == "check" else v) for n, v in page_post(page, "keep")]
        path = f"/parent/imports/{draft_id}/review"
    elif site == "an offer":
        [piece] = fields_named(review_form(page), "answer:destination:")[:1]
        version = re.search(r'name="version" value="(\d+)"', page)
        assert version is not None
        fields = [
            ("version", version.group(1)),
            ("press", "keep"),
            (f"answer:destination:{piece}", '{"new":1}'),
            ("offer", signature),
        ]
        path = f"/parent/imports/{draft_id}/review"
    elif site == "Read":
        [image_id] = images_of(browser, draft_id)[:1]
        fields = [("images", image_id), ("check", signature)]
        path = f"/parent/imports/{draft_id}/read"
    else:
        path, found = outside(page, SMALL_FORMS[site])
        fields = [(n, signature if n == "check" else v) for n, v in found]
    answer = browser.post(path, content=as_a_browser_sends(fields), headers=form)
    shown = followed(browser, answer)
    return answer.status_code, said_lines(shown)


@pytest.mark.parametrize("value", ["é", "€", "é€" * 32], ids=["e-acute", "euro", "sixty-four"])
@pytest.mark.parametrize("site", SIGNED_SITES)
def test_a_signed_field_that_isn_t_ascii_is_refused_like_any_bad_signature(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, site: str, value: str
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        before = draft_state(browser, draft_id)
        wrong = signed_answer(browser, draft_id, page, site, "0" * 64)
        between = draft_state(browser, draft_id)
        odd = signed_answer(browser, draft_id, page, site, value)
        after = draft_state(browser, draft_id)

    assert odd == wrong
    assert before == between == after


MALFORMED: Final = {
    "invalid UTF-8": b"version=1&words=%FF",
    "an encoded surrogate": b"version=1&words=%ED%A0%80",
}
"""Bodies a browser never sends, the bad bytes in an unsigned field; a signed field holding
what they decode to is the signature test's case."""
BODY_ROUTES: Final = {
    "read": ("read", {"invalid UTF-8": 400, "an encoded surrogate": 400, "a file part": 400}),
    "review": ("review", {"invalid UTF-8": 303, "an encoded surrogate": 303, "a file part": 400}),
    "find": ("find", {"invalid UTF-8": 400, "an encoded surrogate": 400, "a file part": 400}),
    "set aside": (
        "set-aside",
        {"invalid UTF-8": 303, "an encoded surrogate": 303, "a file part": 400},
    ),
    "finish": ("finish", {"invalid UTF-8": 303, "an encoded surrogate": 303, "a file part": 400}),
    "discard": ("discard", {"invalid UTF-8": 303, "an encoded surrogate": 303, "a file part": 400}),
    "not hers": (
        "not-hers",
        {"invalid UTF-8": 303, "an encoded surrogate": 303, "a file part": 400},
    ),
}
"""Each import route that reads a form body, by its address under the review, and its own
answer to each malformed body: 400 when the form can't be read, or the route's refusal of a
form that isn't whole or isn't signed."""


@pytest.mark.parametrize("body", [*MALFORMED, "a file part"])
@pytest.mark.parametrize("route", list(BODY_ROUTES))
def test_every_body_reading_import_route_refuses_a_malformed_body_and_changes_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, route: str, body: str
) -> None:
    action, expected = BODY_ROUTES[route]
    with here(open_settings(tmp_path)) as browser:
        draft_id, _ = read_review(browser, monkeypatch, (RED, homework_answer()))
        before = draft_state(browser, draft_id)
        path = f"/parent/imports/{draft_id}/{action}"
        if body == "a file part":
            answer = browser.post(path, files={"check": ("check.txt", b"0" * 8, "text/plain")})
        else:
            answer = browser.post(
                path,
                content=MALFORMED[body],
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        after = draft_state(browser, draft_id)

    assert answer.status_code == expected[body]
    assert before == after


# ---------------------------------------------------------- instructions as a browser posts them

MULTILINE = "Read pages 1-3.\nAnswer question 2."


def two_names_with_instructions(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch, first: str, second: str
) -> tuple[str, str, str]:
    """A review whose two cards of two names, with these instructions, are both found onto one
    "Grammar Practice": its id, the page after, and the instruction question's subject."""
    target = Assignment(
        assignment_id="assignment-grammar-practice-eeee4444",
        course="English 8",
        title="Grammar Practice",
        due_date=date(2026, 10, 8),
        dependencies=[],
        reported_submission_status="not_reported",
        assigned_on=None,
        kind=AssignmentKind.HOMEWORK,
    )
    cards = [
        homework_card(title="Vocab Test", instruction=first),
        homework_card(title="Spelling List", instruction=second, y_top=360, y_bottom=450),
    ]
    state_of(browser).project_state.upsert_assignments([target])
    draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer(cards=cards)))
    page = her_name_answered(browser, draft_id, page)
    for piece in fields_named(review_form(page), "answer:destination:"):
        _, fields = outside(page, find_button(page, piece))
        finding = browser.post(
            f"/parent/imports/{draft_id}/find", data={**dict(fields), "words": "grammar"}
        ).text
        choice = re.search(
            r'<form method="post" action="[^"]+/review" class="choice">.*?</form>', finding, re.S
        )
        assert choice is not None
        chosen = [
            (html.unescape(n), html.unescape(v))
            for n, v in re.findall(r'name="([^"]+)" value="([^"]*)"', choice.group(0))
        ]
        page = followed(browser, send(browser, draft_id, chosen))
    [subject] = fields_named(review_form(page), "answer:instructions:")
    return draft_id, page, subject


def option_for(page: str, subject: str, words: str) -> str:
    """The value of the instruction option the page shows with these words."""
    shown = f"“{words}”"
    for value, label in re.findall(
        rf'name="{re.escape(str(escape(f"answer:instructions:{subject}")))}" value="([^"]*)"'
        r"(?: checked)?> <span>(.*?)</span>",
        review_form(page),
        re.S,
    ):
        if html.unescape(str(label)) == shown:
            return html.unescape(str(value))
    raise AssertionError(words)


def test_a_multiline_instruction_kept_and_saved_as_a_browser_posts_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, subject = two_names_with_instructions(
            browser, monkeypatch, MULTILINE, "Study list two."
        )
        field = f"answer:instructions:{subject}"
        kept = followed(
            browser,
            send(
                browser,
                draft_id,
                [*page_post(page, "keep"), (field, option_for(page, subject, MULTILINE))],
            ),
        )
        held = stored_answers(browser, draft_id)
        followed(browser, send(browser, draft_id, page_post(kept, "save", tick=units_on(kept))))
        record = state_of(browser).project_state
        readings = record.school_instruction_readings(["assignment-grammar-practice-eeee4444"])

    assert f'name="{escape(field)}" value=""' in review_form(page)
    assert json.loads(held[("instructions", subject)]) == [MULTILINE]
    assert readings.readable["assignment-grammar-practice-eeee4444"].texts == (MULTILINE,)


def test_two_instructions_differing_only_in_line_endings_are_told_apart(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    crlf = MULTILINE.replace("\n", "\r\n")
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, subject = two_names_with_instructions(browser, monkeypatch, MULTILINE, crlf)
        field = f"answer:instructions:{subject}"
        lf_value = option_for(page, subject, MULTILINE)
        crlf_value = option_for(page, subject, crlf)
        followed(browser, send(browser, draft_id, [*page_post(page, "keep"), (field, lf_value)]))
        held = stored_answers(browser, draft_id)

    assert lf_value != crlf_value
    assert json.loads(held[("instructions", subject)]) == [MULTILINE]


def test_an_instruction_longer_than_a_paste_is_offered_and_kept_whole(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blossom.reconciliation import SourceChannel
    from blossom.school_instructions import INSTRUCTION_MAX_LENGTH

    longest = "Read every page. " + "z" * (INSTRUCTION_MAX_LENGTH + 1 - len("Read every page. "))
    with here(open_settings(tmp_path)) as browser:
        record = state_of(browser).project_state
        saved = Assignment(
            assignment_id="assignment-sentence-composing-llll0000",
            course="English 8",
            title="Sentence Composing",
            due_date=date(2026, 10, 8),
            dependencies=[],
            reported_submission_status="not_reported",
            assigned_on=None,
            kind=AssignmentKind.HOMEWORK,
        )
        record.upsert_assignments([saved])
        record.put_on_record(
            [saved.model_copy(update={"note": longest, "origins": {"note": SourceChannel.LMS}})],
            {},
        )
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        page = her_name_answered(browser, draft_id, page)
        [piece] = fields_named(review_form(page), "answer:destination:")
        onto = json.dumps({"existing": saved.assignment_id}, separators=(",", ":"))
        asked = followed(
            browser,
            send(
                browser,
                draft_id,
                page_post(page, "keep", choose={f"answer:destination:{piece}": onto}),
            ),
        )
        [subject] = fields_named(review_form(asked), "answer:instructions:")
        field = f"answer:instructions:{subject}"
        followed(
            browser,
            send(
                browser,
                draft_id,
                [*page_post(asked, "keep"), (field, option_for(asked, subject, longest))],
            ),
        )
        held = stored_answers(browser, draft_id)

    assert len(longest) == INSTRUCTION_MAX_LENGTH + 1
    assert json.loads(held[("instructions", subject)]) == [longest]


# ----------------------------------------------- her homework confirmed, then more screenshots

ASK_NAME_AGAIN = "Your screenshots changed, so confirm again that these are her homework."


def unnamed(title: str) -> dict[str, Any]:
    """A homework list showing no student name, with one card of this title."""
    return homework_answer(
        active_student={"name": None, "uncertain": None}, cards=[homework_card(title=title)]
    )


def confirmed_then_added(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str, str]:
    """A review of one screenshot showing no name, her homework confirmed by Keep these answers,
    then a second such screenshot added and read: its id, the page after the confirmation, and
    the page after the Read."""
    reader = Reader(monkeypatch)
    reader.answers[RED] = ScreenshotAnswer(unnamed("Sentence Composing"), None, 10, 5)
    reader.answers[BLUE] = ScreenshotAnswer(unnamed("Lab Report"), None, 10, 5)
    draft_id = made(browser, RED)
    page = browser.get(f"/parent/imports/{draft_id}").text
    page = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page)).text
    confirmed = followed(
        browser,
        send(
            browser, draft_id, page_post(page, "keep", choose={"answer:name:missing": "confirmed"})
        ),
    )
    upload(browser, BLUE, form={}, to=f"/parent/imports/{draft_id}/images")
    page = browser.get(f"/parent/imports/{draft_id}").text
    read = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page)).text
    return draft_id, confirmed, read


def test_a_screenshot_with_no_name_added_after_the_confirmation_asks_for_it_again(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    field = "answer:name:missing"
    with here(open_settings(tmp_path)) as browser:
        draft_id, confirmed, read = confirmed_then_added(browser, monkeypatch)
        held = stored_answers(browser, draft_id)
        before = assignment_count(browser)
        forced = followed(
            browser, send(browser, draft_id, page_post(read, "save", tick=digests_on(read)))
        )
        assert assignment_count(browser) == before
        answered = followed(
            browser, send(browser, draft_id, page_post(forced, "keep", choose={field: "confirmed"}))
        )
        saved = followed(
            browser, send(browser, draft_id, page_post(answered, "save", tick=units_on(answered)))
        )
        after = assignment_count(browser)

    assert field in checked_names(confirmed)
    assert ("name", "missing") not in held
    assert says(read, ASK_NAME_AGAIN)
    assert says(read, "No student name was found on 2 screenshots.")
    assert field not in checked_names(read)
    assert units_on(read) == []
    assert len(digests_on(read)) == 2
    assert field not in checked_names(forced)
    assert len(units_on(answered)) == 2
    assert says(saved, "Saved: 2 new assignments.")
    assert after == before + 2


def test_reopening_or_removing_a_screenshot_keeps_the_confirmation(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    field = "answer:name:missing"
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser,
            monkeypatch,
            (RED, unnamed("Sentence Composing")),
            (BLUE, unnamed("Lab Report")),
        )
        followed(
            browser, send(browser, draft_id, page_post(page, "keep", choose={field: "confirmed"}))
        )
        reopened = browser.get(f"/parent/imports/{draft_id}").text
        [_, blue] = images_of(browser, draft_id)
        removed = followed(
            browser, browser.post(f"/parent/imports/{draft_id}/images/{blue}/remove")
        )
        held = stored_answers(browser, draft_id)

    assert held[("name", "missing")] == "confirmed"
    for shown in (reopened, removed):
        assert field in checked_names(shown)
        assert not says(shown, ASK_NAME_AGAIN)
    assert len(units_on(reopened)) == 2
    assert len(units_on(removed)) == 1


def test_a_screenshot_with_no_name_that_replaces_the_one_confirmed_asks_for_it_again(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    field = "answer:name:missing"
    reader = Reader(monkeypatch)
    reader.answers[RED] = ScreenshotAnswer(unnamed("Sentence Composing"), None, 10, 5)
    reader.answers[BLUE] = ScreenshotAnswer(unnamed("Lab Report"), None, 10, 5)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        page = browser.get(f"/parent/imports/{draft_id}").text
        page = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page)).text
        followed(
            browser, send(browser, draft_id, page_post(page, "keep", choose={field: "confirmed"}))
        )
        [red] = images_of(browser, draft_id)
        removed = followed(browser, browser.post(f"/parent/imports/{draft_id}/images/{red}/remove"))
        kept = stored_answers(browser, draft_id)
        upload(browser, BLUE, form={}, to=f"/parent/imports/{draft_id}/images")
        page = browser.get(f"/parent/imports/{draft_id}").text
        read = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page)).text
        held = stored_answers(browser, draft_id)

    assert kept[("name", "missing")] == "confirmed"
    assert not says(removed, ASK_NAME_AGAIN)
    assert ("name", "missing") not in held
    assert says(read, ASK_NAME_AGAIN)
    assert says(read, "No student name was found on 1 screenshot.")
    assert field not in checked_names(read)
    assert units_on(read) == []


def test_a_screenshot_showing_a_name_added_later_asks_its_name_and_keeps_the_confirmation(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    field = "answer:name:missing"
    named = homework_answer(
        active_student={"name": "Avery", "uncertain": None},
        cards=[homework_card(title="Lab Report")],
    )
    reader = Reader(monkeypatch)
    reader.answers[RED] = ScreenshotAnswer(unnamed("Sentence Composing"), None, 10, 5)
    reader.answers[GREEN] = ScreenshotAnswer(named, None, 10, 5)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        page = browser.get(f"/parent/imports/{draft_id}").text
        page = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page)).text
        followed(
            browser, send(browser, draft_id, page_post(page, "keep", choose={field: "confirmed"}))
        )
        upload(browser, GREEN, form={}, to=f"/parent/imports/{draft_id}/images")
        page = browser.get(f"/parent/imports/{draft_id}").text
        read = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page)).text
        held = stored_answers(browser, draft_id)

    assert held[("name", "missing")] == "confirmed"
    assert not says(read, ASK_NAME_AGAIN)
    assert field in checked_names(read)
    assert says(read, "No student name was found on 1 screenshot.")
    assert says(read, "Screenshot 2 names Avery. Is this her name?")
    assert not any(
        name.startswith("answer:name:") and name != field for name in checked_names(read)
    )
    assert len(units_on(read)) == 1


def test_a_save_from_the_page_before_the_screenshot_was_added_writes_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    field = "answer:name:missing"
    with here(open_settings(tmp_path)) as browser:
        draft_id, confirmed, _ = confirmed_then_added(browser, monkeypatch)
        before = assignment_count(browser)
        stale = send(browser, draft_id, page_post(confirmed, "save", tick=units_on(confirmed)))
        shown = followed(browser, stale)
        after = assignment_count(browser)
        held = stored_answers(browser, draft_id)

    assert units_on(confirmed)
    assert stale.status_code == 409
    assert says(shown, imports.OUT_OF_DATE)
    assert after == before
    assert ("name", "missing") not in held
    assert field not in checked_names(shown)


# ------------------------------------------------------- corrections as long as a reading holds

LONG_TITLE = "Sentence Composing " + "a" * 231
LONG_WORDS = "Complete pages 12 to 14 " + "b" * 226


def unsure_review(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch, *fields: str
) -> tuple[str, str, dict[str, str]]:
    """A review of one card whose given fields the reader wasn't sure of, her name answered:
    its id, the page, and each box of "Correct what was read" by field, its one instruction
    text's box also named ``instruction``."""
    card = homework_card(uncertain=[{"field": f, "reason": "faint"} for f in fields])
    draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer(cards=[card])))
    page = her_name_answered(browser, draft_id, page)
    boxes = {
        str(json.loads(subject)[1]): f"correct:{subject}"
        for subject in fields_named(review_form(page), "correct:")
    }
    boxes["instruction"] = boxes["instruction:0"]
    return draft_id, page, boxes


def box_of(page: str, name: str) -> tuple[str, str] | None:
    """A correction box's tag, ``input`` or ``textarea``, and the text it holds as a browser
    reads it."""
    named = re.escape(str(escape(name)))
    found = re.search(rf'(<textarea [^>]*name="{named}"[^>]*>)(.*?)</textarea>', page, re.S)
    if found is not None:
        text = html.unescape(found.group(2))
        return found.group(1), text[1:] if text.startswith("\n") else text
    found = re.search(rf'<input [^>]*name="{named}" value="([^"]*)"[^>]*>', page)
    return None if found is None else (found.group(0), html.unescape(found.group(1)))


def progress_on(browser: TestClient, draft_id: str) -> screenshot_posts.SavedProgress | None:
    student = state_of(browser).project_state.student_id()
    return screenshot_posts.progress_read(
        state_of(browser).import_drafts.snapshot(student, draft_id).progress
    )


def test_no_correction_box_cuts_what_is_typed_or_pasted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A browser counts a box's length in UTF-16 units and cuts a paste to it before the post;
    no correction box names one, so the whole text reaches the server, which counts code
    points."""
    with here(open_settings(tmp_path)) as browser:
        _, page, boxes = unsure_review(browser, monkeypatch, "course", "title", "instruction")
        _, dated, date_boxes = unsure_review(browser, monkeypatch, "footer")

    form = review_form(page)
    tags = {field: str((box_of(form, name) or ("", ""))[0]) for field, name in boxes.items()}
    assert set(tags) == {"course", "title", "instruction", "instruction:0", "due", "assigned"}
    assert tags["course"].startswith('<input type="text"')
    assert tags["title"].startswith('<input type="text"')
    assert tags["instruction"].startswith("<textarea ")
    assert tags["due"].startswith('<input type="date"')
    assert tags["assigned"].startswith('<input type="date"')
    assert "maxlength" not in form
    assert "maxlength" not in review_form(dated)
    assert set(date_boxes) == set(boxes)


def test_a_long_title_and_instruction_are_corrected_and_kept_whole_through_every_press(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, boxes = unsure_review(browser, monkeypatch, "title", "instruction")
        typed = {boxes["title"]: LONG_TITLE, boxes["instruction"]: LONG_WORDS}
        kept = followed(browser, send(browser, draft_id, page_post(page, "keep", typed=typed)))
        later = followed(browser, send(browser, draft_id, page_post(kept, "later")))
        progress = progress_on(browser, draft_id)
        reloaded = browser.get(f"/parent/imports/{draft_id}").text
        saved = followed(
            browser, send(browser, draft_id, page_post(reloaded, "save", tick=units_on(reloaded)))
        )
        record = state_of(browser).project_state
        rows = [a for a in record.all_assignments() if a.title == LONG_TITLE]
        readings = record.school_instruction_readings([a.assignment_id for a in rows])

    assert len(LONG_TITLE) == len(LONG_WORDS) == 250
    assert says(kept, imports.PRESS_SAID["kept"])
    assert says(later, imports.PRESS_SAID["later"])
    for shown in (kept, reloaded):
        assert box_of(shown, boxes["title"])[1] == LONG_TITLE  # type: ignore[index]
        assert box_of(shown, boxes["instruction"])[1] == LONG_WORDS  # type: ignore[index]
    assert progress is not None
    [corrected] = progress.corrected.values()
    assert corrected == {"title": LONG_TITLE, "instruction:0": LONG_WORDS}
    assert says(saved, "Saved: 1 new assignment.")
    [row] = rows
    assert readings.readable[row.assignment_id].texts == (LONG_WORDS,)


LINES_OF_A: Final = ("a" * 99 + "\n") * 39 + "a" * 100
"""4,000 characters in 40 lines; a browser posts them as 4,039."""
WIDE: Final = "\N{SPEAKING HEAD IN SILHOUETTE}" * 4_000
"""4,000 characters of four bytes each, 48,000 bytes once a form encodes them."""


@pytest.mark.parametrize(
    ("words", "kept"),
    [(LINES_OF_A, True), (LINES_OF_A + "a", False), (WIDE, True)],
    ids=["4000", "4001", "4000 four-byte"],
)
def test_an_instruction_correction_of_4000_characters_is_kept_and_one_longer_is_refused_whole(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, words: str, kept: bool
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, boxes = unsure_review(browser, monkeypatch, "instruction")
        later = followed(
            browser,
            send(browser, draft_id, page_post(page, "later", typed={boxes["instruction"]: words})),
        )
        progress = progress_on(browser, draft_id)

    assert len(words) == (4000 if kept else 4001)
    if kept:
        assert says(later, imports.PRESS_SAID["later"])
        assert progress is not None
        [corrected] = progress.corrected.values()
        assert corrected == {"instruction:0": words}
    else:
        assert says(later, imports.PRESS_SAID["too_long"])
        assert progress is None
        assert box_of(later, boxes["instruction"])[1] == words  # type: ignore[index]


def test_a_multiline_instruction_correction_keeps_its_line_breaks_through_a_browser_s_post(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    words = "Read pages 4 to 6.\nAnswer the two questions."
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, boxes = unsure_review(browser, monkeypatch, "instruction")
        followed(
            browser,
            send(browser, draft_id, page_post(page, "later", typed={boxes["instruction"]: words})),
        )
        progress = progress_on(browser, draft_id)
        reloaded = browser.get(f"/parent/imports/{draft_id}").text
        followed(
            browser, send(browser, draft_id, page_post(reloaded, "save", tick=units_on(reloaded)))
        )
        record = state_of(browser).project_state
        [row] = [a for a in record.all_assignments() if a.title == "Sentence Composing"]
        readings = record.school_instruction_readings([row.assignment_id])

    assert progress is not None
    assert list(progress.corrected.values()) == [{"instruction:0": words}]
    assert box_of(reloaded, boxes["instruction"])[1] == words  # type: ignore[index]
    assert readings.readable[row.assignment_id].texts == (words,)


@pytest.mark.parametrize(
    ("shape", "key", "field", "most"),
    [
        (3, "k1", "course", 200),
        (3, "k1", "title", 300),
        (3, "k1", "instruction", 4000),
        (3, "k1", "footer", 200),
        (3, "k1", "heading", 200),
        (4, "k1", "course", 200),
        (4, "k1", "title", 300),
        (4, "k1", "instruction:0", 4000),
        (4, "k1", "instruction:12", 4000),
        (4, "date:k1", "due", 200),
        (4, "date:k1|2026-10-05", "assigned", 200),
    ],
)
def test_saved_progress_holds_each_correction_up_to_its_field_s_limit(
    shape: int, key: str, field: str, most: int
) -> None:
    def progress(length: int) -> str:
        return json.dumps({"v": shape, "corrected": {key: {field: "a" * length}}, "selected": []})

    within = screenshot_posts.progress_read(progress(most))
    assert within is not None
    assert within.corrected == {key: {field: "a" * most}}
    assert screenshot_posts.progress_read(progress(most + 1)) is None


# ----------------------------------------------- a closing press and a save between


CLOSING_PRESSES: Final = {
    "Not hers": ("discard_review", "not-hers"),
    "Discard the review": ("discard_review", "discard"),
    "Finish without the rest": ("discard_rest", "finish"),
    "Leave this group unsaved": ("set_aside", "leave-2"),
}
"""Each closing press: the store call its transaction runs, and the form that sends it."""


def acceptances_of(browser: TestClient, draft_id: str) -> int:
    connection = state_of(browser).import_drafts._connection
    found = connection.execute(
        "SELECT COUNT(*) FROM grade_acceptances WHERE source_key = ?", (draft_id,)
    ).fetchone()
    return int(found[0])


def kept_two_groups(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """A review of two cards whose answers, her name's "Yes, this is her name" among them, are
    kept: its id and the page Keep these answers returns."""
    draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
    [form] = fields_named(review_form(page), "answer:name:")
    choose = {f"answer:name:{form}": "hers"}
    kept = followed(browser, send(browser, draft_id, page_post(page, "keep", choose=choose)))
    return draft_id, kept


@pytest.mark.parametrize("press", list(CLOSING_PRESSES))
def test_a_save_committing_between_a_closing_press_and_its_transaction_is_reported_truthfully(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, press: str
) -> None:
    method, form_id = CLOSING_PRESSES[press]
    seen: dict[str, object] = {}
    with here(open_settings(tmp_path)) as browser:
        draft_id, kept = kept_two_groups(browser, monkeypatch)
        first, _ = units_on(kept)
        store = state_of(browser).import_drafts
        student = state_of(browser).project_state.student_id()
        real = getattr(store, method)

        def racing(*args: object, **kwargs: object) -> object:
            seen["before"] = store.snapshot(student, draft_id).version
            saving = page_post(kept, "save", tick=[first])
            seen["saved"] = followed(browser, send(browser, draft_id, saving))
            seen["after"] = store.snapshot(student, draft_id).version
            return real(*args, **kwargs)

        before = assignment_count(browser)
        monkeypatch.setattr(store, method, racing)
        shown = followed(browser, press_outside(browser, kept, form_id))
        monkeypatch.setattr(store, method, real)
        after = assignment_count(browser)
        accepted = acceptances_of(browser, draft_id)
        snapshot = store.snapshot(student, draft_id)

    assert says(str(seen["saved"]), "Saved: 1 new assignment.")
    assert seen["before"] == seen["after"]
    assert after == before + 1
    assert accepted == 1
    assert not says(shown, imports.AFTER_SAID["nothing_saved"])
    assert not says(shown, imports.AFTER_SAID["not_hers"])
    if press == "Not hers":
        assert says(shown, imports.AFTER_SAID["not_hers_after_a_save"])
        assert snapshot.closed_at is None
        assert len(snapshot.images) == 1
    elif press == "Discard the review":
        assert says(shown, imports.AFTER_SAID["discarded"])
        assert says(shown, imports.AFTER_SAID["safe"])
        assert snapshot.closed_at is not None
    else:
        assert says(shown, imports.AFTER_SAID["finished"])
        assert snapshot.closed_at is not None


@pytest.mark.parametrize("press", ["Finish without the rest", "Leave this group unsaved"])
def test_a_closing_press_whose_review_expires_after_its_commit_answers_from_its_transaction(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, press: str
) -> None:
    method = CLOSING_PRESSES[press][0]
    lone = homework_answer(cards=[homework_card()])
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, lone))
        store = state_of(browser).import_drafts
        real = getattr(store, method)
        later = store._clock.now() + DRAFT_LIFETIME

        def then_expiring(*args: object, **kwargs: object) -> object:
            done = real(*args, **kwargs)
            monkeypatch.setattr(store._clock, "now", lambda: later)
            return done

        monkeypatch.setattr(store, method, then_expiring)
        answer = press_outside(browser, page, "finish" if method == "discard_rest" else "leave-1")

    assert answer.status_code == 200
    assert says(answer.text, imports.AFTER_SAID["nothing_saved"])


@pytest.mark.parametrize("press", ["Not hers", "Discard the review", "Finish without the rest"])
def test_a_save_from_a_page_made_before_a_closing_press_writes_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, press: str
) -> None:
    form_id = CLOSING_PRESSES[press][1]
    with here(open_settings(tmp_path)) as browser:
        draft_id, kept = kept_two_groups(browser, monkeypatch)
        before = assignment_count(browser)
        closing = followed(browser, press_outside(browser, kept, form_id))
        shut = closed(browser, draft_id)
        saving = page_post(kept, "save", tick=units_on(kept))
        saved = followed(browser, send(browser, draft_id, saving))
        again = followed(browser, press_outside(browser, kept, form_id))
        after = assignment_count(browser)
        accepted = acceptances_of(browser, draft_id)

    assert shut
    assert not says(closing, imports.AFTER_SAID["safe"])
    assert says(saved, imports.SAID["closed"])
    assert says(again, imports.SAID["closed"])
    assert after == before
    assert accepted == 0


def test_after_a_save_whose_close_failed_a_closing_press_still_sees_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed: list[str] = []

    def failing_after_commit() -> None:
        if not failed:
            failed.append("after_commit")
            msg = "the review's close fails once"
            raise sqlite3.OperationalError(msg)

    with here(open_settings(tmp_path)) as browser:
        draft_id, kept = kept_two_groups(browser, monkeypatch)
        first, _ = units_on(kept)
        monkeypatch.setattr(screenshot_save.NO_HOOKS, "after_commit", failing_after_commit)
        followed(browser, send(browser, draft_id, page_post(kept, "save", tick=[first])))
        refused = followed(browser, press_outside(browser, kept, "not-hers"))
        still_open = not closed(browser, draft_id)
        discarded = followed(browser, press_outside(browser, kept, "discard"))
        accepted = acceptances_of(browser, draft_id)

    assert failed == ["after_commit"]
    assert says(refused, imports.AFTER_SAID["not_hers_after_a_save"])
    assert still_open
    assert says(discarded, imports.AFTER_SAID["safe"])
    assert accepted == 1


# ----------------------------------------------- one row from a named and an unnamed screenshot


def shown_by(name: str | None, heading: str, *cards: dict[str, Any]) -> dict[str, Any]:
    """A screenshot naming this student, or none, under one day heading."""
    return homework_answer(
        active_student={"name": name, "uncertain": None},
        headings=[{"text": heading, "y": 100, "uncertain": None}],
        cards=list(cards),
    )


MERGED_SHAPES: Final = {
    "the merged row alone": ((), ()),
    "beside a card of the named screenshot": (
        (
            homework_card(
                badge="DUE",
                title="Lab Report",
                instruction=None,
                footer=None,
                y_top=260,
                y_bottom=330,
            ),
        ),
        (),
    ),
    "after the unnamed screenshot's own card saved": (
        (),
        (
            homework_card(
                badge="DUE", title="Warm Up", instruction=None, footer=None, y_top=260, y_bottom=330
            ),
        ),
    ),
}
"""Each shape of a Sentence Composing row read from a screenshot naming Wren, its assigned
card under Tuesday, and one naming nobody, its due card under Wednesday: the named
screenshot's other cards, and the unnamed one's."""


def identities_of(browser: TestClient, draft_id: str) -> list[tuple[str, str]]:
    connection = state_of(browser).import_drafts._connection
    return [
        (str(status), str(answer))
        for status, answer in connection.execute(
            "SELECT identity_status, identity_answer FROM grade_acceptances "
            "WHERE source_key = ? ORDER BY rowid",
            (draft_id,),
        )
    ]


@pytest.mark.parametrize("shape", list(MERGED_SHAPES))
def test_a_merged_row_saves_with_both_confirmations_before_and_after_another_groups_save(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    named_cards, unnamed_cards = MERGED_SHAPES[shape]
    assigned = homework_card(footer="Due (10/7/2026)", y_top=150, y_bottom=230)
    due = homework_card(badge="DUE", instruction=None, footer=None, y_top=150, y_bottom=230)
    named = shown_by("Wren", "Tuesday 10/6/2026", assigned, *named_cards)
    unnamed = shown_by(None, "Wednesday 10/7/2026", due, *unnamed_cards)
    missing = {"answer:name:missing": "confirmed"}
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, named), (BLUE, unnamed))
        [form] = [f for f in fields_named(review_form(page), "answer:name:") if f != "missing"]
        hers = {f"answer:name:{form}": "hers"}
        start = assignment_count(browser)
        if unnamed_cards:
            page = followed(
                browser, send(browser, draft_id, page_post(page, "keep", choose=missing))
            )
            [own] = units_on(page)
            page = followed(browser, send(browser, draft_id, page_post(page, "save", tick=[own])))
            first, last = missing, hers
        else:
            first, last = hers, missing
        assert assignment_count(browser) == start + len(unnamed_cards)
        one = followed(browser, send(browser, draft_id, page_post(page, "keep", choose=first)))
        [merged] = [unit for unit in digests_on(one) if unit not in units_on(one)]
        forced = followed(browser, send(browser, draft_id, page_post(one, "save", tick=[merged])))
        waited = assignment_count(browser)
        both = followed(browser, send(browser, draft_id, page_post(forced, "keep", choose=last)))
        ready = units_on(both)
        posted = page_post(both, "save", tick=[merged])
        saved = followed(browser, send(browser, draft_id, posted))
        after = assignment_count(browser)
        resent = followed(browser, send(browser, draft_id, posted))
        fresh = browser.get(f"/parent/imports/{draft_id}").text
        again = assignment_count(browser)
        identities = identities_of(browser, draft_id)
        if named_cards:
            [other] = [unit for unit in units_on(fresh) if unit != merged]
            followed(browser, send(browser, draft_id, page_post(fresh, "save", tick=[other])))
            identities = identities_of(browser, draft_id)

    assert waited == start + len(unnamed_cards)
    assert merged in ready
    assert says(saved, "Saved: 1 new assignment.")
    assert after == waited + 1
    assert again == after
    assert not says(resent, "wasn't saved")
    assert merged not in units_on(fresh)
    expected = [("missing", "confirmed")] * len(unnamed_cards) + [("first_use", "hers")]
    expected += [("matches", "shown")] * len(named_cards)
    assert identities == expected


# ------------------------------------------------------- corrections past their field's limit

FIELD_LIMITS: Final = {"course": 200, "title": 300, "instruction": 4000}
"""Each one-line or multi-line correction's most characters, counted in code points."""
FIELD_SAID: Final = {"course": "class", "title": "title", "instruction": "instructions"}
STORED_AS: Final = {"course": "course", "title": "title", "instruction": "instruction:0"}
"""Each box's field as saved progress keeps it: an instruction by its text's index."""


def at_most(field: str, extra: str = "") -> str:
    """A correction of exactly its field's most code points, one of them outside the Basic
    Multilingual Plane, then ``extra``; an instruction's has line breaks."""
    most = FIELD_LIMITS[field]
    if field == "instruction":
        head = "Complete pages 12 to 14.\nAnswer both questions.\n"
    else:
        head = {"course": "English 8 ", "title": "Sentence Composing "}[field]
    return head + "b" * (most - len(head) - 2) + "\N{GRINNING FACE}z" + extra


@pytest.mark.parametrize("field", list(FIELD_LIMITS))
def test_a_correction_of_its_field_s_most_code_points_is_kept_whole(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    words = at_most(field)
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, boxes = unsure_review(browser, monkeypatch, field)
        later = followed(
            browser, send(browser, draft_id, page_post(page, "later", typed={boxes[field]: words}))
        )
        progress = progress_on(browser, draft_id)
        reloaded = browser.get(f"/parent/imports/{draft_id}").text

    assert len(words) == FIELD_LIMITS[field]
    assert len(words.encode("utf-16-le")) // 2 == FIELD_LIMITS[field] + 1
    assert says(later, imports.PRESS_SAID["later"])
    assert progress is not None
    assert list(progress.corrected.values()) == [{STORED_AS[field]: words}]
    assert box_of(reloaded, boxes[field])[1] == words  # type: ignore[index]


@pytest.mark.parametrize("press", ["keep", "later", "save"])
@pytest.mark.parametrize("field", list(FIELD_LIMITS))
def test_a_correction_past_its_field_s_limit_is_refused_whole_and_returned_as_typed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, field: str, press: str
) -> None:
    words = at_most(field, "y")
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, boxes = unsure_review(browser, monkeypatch, field)
        held = stored_answers(browser, draft_id)
        before = assignment_count(browser)
        posted = page_post(page, press, typed={boxes[field]: words}, tick=units_on(page))
        answer = send(browser, draft_id, posted)
        assert answer.status_code == 422
        progress = progress_on(browser, draft_id)
        after = assignment_count(browser)
        answers = stored_answers(browser, draft_id)
        again = send(browser, draft_id, page_post(answer.text, press))
        still = progress_on(browser, draft_id)

    named = FIELD_SAID[field]
    assert answer.status_code == 422
    assert says(answer.text, imports.PRESS_SAID["too_long"])
    assert says(answer.text, f"Keep the {named} to {FIELD_LIMITS[field]:,} characters or fewer.")
    assert box_of(answer.text, boxes[field])[1] == words  # type: ignore[index]
    assert says(answer.text, "Still to answer: a field the reader wasn't sure of")
    assert progress is None
    assert after == before
    assert answers == held
    assert again.status_code == 422
    assert box_of(again.text, boxes[field])[1] == words  # type: ignore[index]
    assert still is None


def test_a_date_typed_past_its_limit_is_refused_whole_and_kept_until_a_date_is_chosen(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    typed = "the Thursday after next, " + "e" * 180
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, boxes = unsure_review(browser, monkeypatch, "footer")
        box = boxes["due"]
        refused = send(browser, draft_id, page_post(page, "later", typed={box: typed}))
        assert refused.status_code == 422
        again = send(browser, draft_id, page_post(refused.text, "later"))
        chosen = send(browser, draft_id, page_post(again.text, "later", typed={box: "2026-10-09"}))
        progress = progress_on(browser, draft_id)

    carry = re.search(r'name="refused:[^"]*" value="([^"]*)"', refused.text)
    assert len(typed) == 205
    assert refused.status_code == 422
    assert says(refused.text, "Keep the due date to 200 characters or fewer.")
    assert carry is not None
    assert html.unescape(carry.group(1)) == typed
    assert again.status_code == 422
    assert says(again.text, "Keep the due date to 200 characters or fewer.")
    assert chosen.status_code == 303
    assert progress is not None
    assert list(progress.corrected.values()) == [{"due": "2026-10-09"}]


# ------------------------------------------------------- the save's clock, read inside its writer


def clock_moved_to(monkeypatch: pytest.MonkeyPatch, browser: TestClient, at: datetime) -> None:
    """The real clock, which the draft store reads too, set to ``at`` from now on."""
    monkeypatch.setattr(state_of(browser).real_clock, "now", lambda: at)


@pytest.mark.parametrize("past", [timedelta(seconds=1), timedelta(0)], ids=["after", "exactly at"])
def test_a_save_whose_wait_for_the_writer_crosses_expiry_writes_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, past: timedelta
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, kept = kept_two_groups(browser, monkeypatch)
        first, _ = units_on(kept)
        state = state_of(browser)
        student = state.project_state.student_id()
        expires = state.import_drafts.snapshot(student, draft_id).expires_at
        real = state.project_state.comparing_and_writing
        entered: list[bool] = []

        def waiting() -> object:
            if not entered:
                entered.append(True)
                clock_moved_to(monkeypatch, browser, expires + past)
            return real()

        before = assignment_count(browser)
        monkeypatch.setattr(state.project_state, "comparing_and_writing", waiting)
        answer = send(browser, draft_id, page_post(kept, "save", tick=[first]))
        monkeypatch.setattr(state.project_state, "comparing_and_writing", real)
        after = assignment_count(browser)
        accepted = acceptances_of(browser, draft_id)

    assert entered == [True]
    assert says(answer.text, imports.SAID["expired"])
    assert after == before
    assert accepted == 0


def test_a_save_that_passed_its_checks_before_expiry_completes_after_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, kept = kept_two_groups(browser, monkeypatch)
        first, _ = units_on(kept)
        state = state_of(browser)
        student = state.project_state.student_id()
        expires = state.import_drafts.snapshot(student, draft_id).expires_at
        moved: list[bool] = []

        def expiring_meanwhile(assignment_id: str) -> None:
            if not moved:
                moved.append(True)
                clock_moved_to(monkeypatch, browser, expires + timedelta(minutes=1))

        before = assignment_count(browser)
        monkeypatch.setattr(screenshot_save.NO_HOOKS, "after_keep", expiring_meanwhile)
        saved = followed(browser, send(browser, draft_id, page_post(kept, "save", tick=[first])))
        after = assignment_count(browser)
        accepted = acceptances_of(browser, draft_id)

    assert moved == [True]
    assert says(saved, "Saved: 1 new assignment.")
    assert after == before + 1
    assert accepted == 1


@pytest.mark.parametrize("gone", ["expired", "removed"])
def test_a_save_recorded_before_expiry_replays_its_outcome_after_it_and_writes_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, gone: str
) -> None:
    """A save recorded, then its review expires, or the sweep removes it: the resent press
    answers with what was saved, in the success panel, and the sentence that says the review
    expired or is gone, never that it finished, and writes nothing."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, kept = kept_two_groups(browser, monkeypatch)
        first, _ = units_on(kept)
        state = state_of(browser)
        student = state.project_state.student_id()
        expires = state.import_drafts.snapshot(student, draft_id).expires_at
        posted = page_post(kept, "save", tick=[first])
        saved = followed(browser, send(browser, draft_id, posted))
        after_save = assignment_count(browser)
        clock_moved_to(monkeypatch, browser, expires + timedelta(hours=2))
        if gone == "removed":
            assert state.import_drafts.sweep().expired == 1
        replayed = followed(browser, send(browser, draft_id, posted))
        after_replay = assignment_count(browser)
        accepted = acceptances_of(browser, draft_id)

    shown = OUTCOME.search(replayed)
    assert says(saved, "Saved: 1 new assignment.")
    assert shown is not None
    assert says(shown.group(1), "Saved: 1 new assignment.")
    assert says(shown.group(1), imports.SAID["expired" if gone == "expired" else "absent"])
    assert not says(replayed, imports.SAID["closed"])
    assert after_replay == after_save
    assert accepted == 1


# ------------------------------------------------------- what was typed, on a review that expired

TYPED_WORDS: Final = "Read pages 4 to 6.\nAnswer the two questions."
TYPED_SAID: Final = (
    "What you typed, as you sent it. This review can't keep it; copy anything you still need."
)
TYPED_DATE: Final = "the Thursday after next"


def typed_review(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str, list[tuple[str, str]], str]:
    """A review of two cards, her name answered: the first's instructions and due line the
    reader wasn't sure of, typed as a multiline correction and a date Blossom can't read; the
    second ready to save. Its id, the page, that page's post for Keep these answers with the
    typing, and the ready group."""
    unsure = homework_card(
        uncertain=[
            {"field": "instruction", "reason": "faint"},
            {"field": "footer", "reason": "faint"},
        ]
    )
    plain = homework_card(
        course="Science 8", title="Chains Reading Guide 6", y_top=360, y_bottom=430
    )
    draft_id, page = read_review(
        browser, monkeypatch, (RED, homework_answer(cards=[unsure, plain]))
    )
    page = her_name_answered(browser, draft_id, page)
    typed = {
        f"correct:{box_subject(page, 'instruction:0')}": TYPED_WORDS,
        f"correct:{box_subject(page, 'due')}": TYPED_DATE,
    }
    return draft_id, page, page_post(page, "keep", typed=typed), units_on(page)[-1]


def copyable(page: str) -> list[str]:
    """The text of each read-only box a page gives back, as a browser reads it."""
    found = re.findall(r"<textarea [^>]*readonly[^>]*>(.*?)</textarea>", page, re.S)
    shown = [html.unescape(text) for text in found]
    return [text[1:] if text.startswith("\n") else text for text in shown]


def stored_row(browser: TestClient, draft_id: str) -> tuple[object, object]:
    """The draft's expiry and its saved progress, read from the file itself, since the store
    refuses an expired draft."""
    connection = state_of(browser).import_drafts._connection
    expires = connection.execute(
        "SELECT expires_at FROM import_drafts WHERE draft_id = ?", (draft_id,)
    ).fetchone()
    progress = connection.execute(
        "SELECT progress FROM import_draft_progress WHERE draft_id = ?", (draft_id,)
    ).fetchone()
    return (None if expires is None else expires[0]), (None if progress is None else progress[0])


EXPIRY_MOMENTS: Final = ("before the press", "while it waits", "after it commits")


@pytest.mark.parametrize("moment", EXPIRY_MOMENTS)
@pytest.mark.parametrize("press", ["keep", "later", "save"])
def test_a_press_on_a_review_that_expires_gives_back_what_was_typed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, press: str, moment: str
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, keeping, ready = typed_review(browser, monkeypatch)
        state = state_of(browser)
        student = state.project_state.student_id()
        expires = state.import_drafts.snapshot(student, draft_id).expires_at
        posted = [(n, press if n == "press" else v) for n, v in keeping]
        if press == "save":
            posted = [*[f for f in posted if f[0] != "unit"], ("unit", ready)]
        stored_before = stored_row(browser, draft_id)
        before = assignment_count(browser)
        past = expires + timedelta(minutes=1)
        if moment == "before the press":
            clock_moved_to(monkeypatch, browser, past)
        elif moment == "while it waits":
            if press == "save":
                real = state.project_state.comparing_and_writing

                def waiting() -> object:
                    clock_moved_to(monkeypatch, browser, past)
                    return real()

                monkeypatch.setattr(state.project_state, "comparing_and_writing", waiting)
            else:
                method = "keep_answers" if press == "keep" else "save_for_later"
                writer = getattr(state.import_drafts, method)

                def late(*args: object, **kwargs: object) -> object:
                    clock_moved_to(monkeypatch, browser, past)
                    return writer(*args, **kwargs)

                monkeypatch.setattr(state.import_drafts, method, late)
        elif press == "save":

            def expiring_meanwhile(assignment_id: str) -> None:
                clock_moved_to(monkeypatch, browser, past)

            monkeypatch.setattr(screenshot_save.NO_HOOKS, "after_keep", expiring_meanwhile)
        else:
            method = "keep_answers" if press == "keep" else "save_for_later"
            writer = getattr(state.import_drafts, method)

            def committed_then_expired(*args: object, **kwargs: object) -> object:
                done = writer(*args, **kwargs)
                clock_moved_to(monkeypatch, browser, past)
                return done

            monkeypatch.setattr(state.import_drafts, method, committed_then_expired)
        answer = followed(browser, send(browser, draft_id, posted))
        after = assignment_count(browser)
        expires_after, progress = stored_row(browser, draft_id)

    assert says(answer, imports.SAID["expired"])
    assert sorted(copyable(answer)) == sorted([TYPED_WORDS, TYPED_DATE])
    assert says(answer, TYPED_SAID)
    assert expires_after == stored_before[0]
    committed = moment == "after it commits"
    if press == "save" and committed:
        assert says(answer, "Saved: 1 new assignment.")
        assert after == before + 1
    else:
        assert after == before
    if press == "later" and committed:
        assert progress is not None
    else:
        assert progress == stored_before[1] is None
    if press in ("keep", "later") and committed:
        assert says(answer, imports.PRESS_SAID["kept_before_ended"])
        assert focused(answer) == ALERT
        assert answer.index('<p class="problem" role="alert"') < answer.index(
            str(escape(imports.PRESS_SAID["kept_before_ended"]))
        )
    assert not says(answer, imports.PRESS_SAID["kept"])
    assert not says(answer, imports.PRESS_SAID["later"])
    assert not says(answer, imports.PRESS_SAID["left_open"])
    assert "Saved for later at " not in answer
    assert "Back to the review" not in answer


@pytest.mark.parametrize("gone", ["expired", "removed"])
@pytest.mark.parametrize("press", ["keep", "later", "save"])
def test_a_press_on_an_expired_or_removed_review_lands_on_what_it_says(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, press: str, gone: str
) -> None:
    """Keep these answers, Save review for later and Save on a review that expired, or that the
    sweep then removed, answer with the page that says so, its first line taking the focus,
    the typing given back to copy and nothing stored."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, _, keeping, ready = typed_review(browser, monkeypatch)
        state = state_of(browser)
        student = state.project_state.student_id()
        expires = state.import_drafts.snapshot(student, draft_id).expires_at
        posted = [(n, press if n == "press" else v) for n, v in keeping]
        if press == "save":
            posted = [*[f for f in posted if f[0] != "unit"], ("unit", ready)]
        before = assignment_count(browser)
        clock_moved_to(monkeypatch, browser, expires + timedelta(hours=2))
        if gone == "removed":
            assert state.import_drafts.sweep().expired == 1
        answer = send(browser, draft_id, posted)
        after = assignment_count(browser)

    assert answer.status_code == (410 if gone == "expired" else 404)
    assert says(answer.text, imports.SAID["expired" if gone == "expired" else "absent"])
    assert sorted(copyable(answer.text)) == sorted([TYPED_WORDS, TYPED_DATE])
    assert focused(answer.text) == ['<p class="problem" role="alert" tabindex="-1" autofocus>']
    assert after == before


def test_a_press_on_a_review_the_sweep_removed_after_expiry_gives_back_what_was_typed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, _, keeping, _ = typed_review(browser, monkeypatch)
        state = state_of(browser)
        student = state.project_state.student_id()
        expires = state.import_drafts.snapshot(student, draft_id).expires_at
        clock_moved_to(monkeypatch, browser, expires + timedelta(hours=2))
        swept = state.import_drafts.sweep()
        answer = send(browser, draft_id, keeping)
        left = stored_row(browser, draft_id)

    assert swept.expired == 1
    assert answer.status_code == 404
    assert says(answer.text, imports.SAID["absent"])
    assert sorted(copyable(answer.text)) == sorted([TYPED_WORDS, TYPED_DATE])
    assert left == (None, None)


# ------------------------------------------------------- a box is a correction where it differs


def progress_after(browser: TestClient, draft_id: str) -> dict[str, dict[str, str | None]]:
    """The corrections the review's saved progress holds."""
    found = progress_on(browser, draft_id)
    assert found is not None
    return found.corrected


def later(browser: TestClient, draft_id: str, page: str, **typed: str) -> str:
    """Save review for later from this page, its boxes as given by name, and the page after."""
    sent = send(browser, draft_id, page_post(page, "later", typed=typed))
    assert sent.status_code == 303, sent.text
    return browser.get(sent.headers["location"]).text


def test_a_page_posted_untouched_writes_no_correction(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every box holds its value as read; posted as it is, by Keep these answers or Save review
    for later, none is a correction, and every answer is kept against no correction."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        kept = followed(browser, send(browser, draft_id, page_post(page, "keep")))
        tables = state_of(browser).project_state.draft_for_save(
            state_of(browser).project_state.student_id(), draft_id
        )
        after = later(browser, draft_id, kept)
        held = progress_after(browser, draft_id)

    assert tables is not None
    assert {digest for _, digest in tables.answers.values()} == {correction_digest(None)}
    assert held == {}
    assert not says(after, screenshot_page.SAID["not_saved_yet"])


def test_an_untouched_earlier_correction_survives_later_and_save(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A correction saved for later comes back in its box; posted again untouched by the next
    Save review for later and by Save selected homework, it stays a correction and is saved."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        page = her_name_answered(browser, draft_id, page)
        title = f"correct:{box_subject(page, 'title')}"
        first = later(browser, draft_id, page, **{title: "Sentence Combining"})
        held = progress_after(browser, draft_id)
        second = later(browser, draft_id, first)
        again = progress_after(browser, draft_id)
        saved = followed(
            browser, send(browser, draft_id, page_post(second, "save", tick=units_on(second)))
        )
        [row] = [
            a
            for a in state_of(browser).project_state.all_assignments()
            if a.course == "English 8" and a.due_date == date(2026, 10, 8)
        ]

    assert box_of(review_form(first), title)[1] == "Sentence Combining"  # type: ignore[index]
    assert (
        held
        == again
        == {json.loads(title.removeprefix("correct:"))[0]: {"title": "Sentence Combining"}}
    )
    assert says(saved, "Saved: 1 new assignment.")
    assert row.title == "Sentence Combining"


def test_a_box_put_back_to_its_read_value_stops_being_a_correction(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        title = f"correct:{box_subject(page, 'title')}"
        corrected = later(browser, draft_id, page, **{title: "Sentence Combining"})
        held = progress_after(browser, draft_id)
        back = later(browser, draft_id, corrected, **{title: "  Sentence Composing "})
        last = progress_after(browser, draft_id)

    assert held != {}
    assert last == {}
    assert box_of(review_form(back), title)[1] == "Sentence Composing"  # type: ignore[index]
    assert "<h3>Sentence Composing</h3>" in back


@pytest.mark.parametrize(
    "words",
    [LINES_OF_A, "Complete pages 12 to 14.   ", "Read pages 4 to 6.\nAnswer the questions.\n"],
    ids=["4000 characters a browser posts as 4039", "trailing spaces", "line breaks"],
)
def test_an_instruction_untouched_is_no_correction_whatever_a_browser_posts(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, words: str
) -> None:
    """An instruction as read, posted untouched, is no correction, and never refused for its
    length: its line breaks come back as CRLF and its spaces as typed, and both sides are
    compared stripped, line endings made line feeds, before any length is counted."""
    card = homework_card(instruction=words)
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer(cards=[card])))
        box = f"correct:{box_subject(page, 'instruction:0')}"
        posted = dict(page_post(page, "later"))[box]
        sent = send(browser, draft_id, page_post(page, "later"))
        held = progress_after(browser, draft_id)

    assert posted.strip() == words.strip()
    assert sent.status_code == 303
    assert held == {}


def test_two_long_instruction_texts_of_one_row_each_have_their_own_box(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The design check's row of two texts of about 3,000 characters each: one box per text,
    together longer than one correction takes, posted untouched without refusal; an edit of the
    second text replaces it alone, and the first stays as read on the card."""
    first = "Assigned card words. " + "a" * 2990
    second = "Due card words. " + "b" * 2990
    answer = homework_answer(
        headings=[
            {"text": "Monday 10/5/2026", "y": 150, "uncertain": None},
            {"text": "Thursday 10/8/2026", "y": 400, "uncertain": None},
        ],
        cards=[
            homework_card(instruction=first, y_top=200, y_bottom=300),
            homework_card(badge="DUE", instruction=second, footer=None, y_top=450, y_bottom=550),
        ],
    )
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        boxes = [f"correct:{box_subject(page, f'instruction:{n}')}" for n in (0, 1)]
        untouched = later(browser, draft_id, page)
        first_held = progress_after(browser, draft_id)
        edited = later(
            browser, draft_id, untouched, **{boxes[1]: "Answer the due card's questions."}
        )
        held = progress_after(browser, draft_id)

    assert [box_of(review_form(page), b)[1] for b in boxes] == [first, second]  # type: ignore[index]
    assert first_held == {}
    assert list(held.values()) == [{"instruction:1": "Answer the due card's questions."}]
    shown = re.findall(r'<q class="authored-text">([^<]*)</q>', review_form(edited))
    assert sorted(html.unescape(s) for s in shown) == sorted(
        [first, "Answer the due card's questions."]
    )


CLEARED: Final = {
    "class": ("course", "A class is needed."),
    "title": ("title", "A title is needed."),
    "due date": ("due", "A due date is needed."),
}
"""The boxes a press can't leave empty, and what each says beneath it."""


@pytest.mark.parametrize("case", list(CLEARED))
def test_a_cleared_class_title_or_due_date_refuses_the_press_with_its_typing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    field, said = CLEARED[case]
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        page = her_name_answered(browser, draft_id, page)
        box = f"correct:{box_subject(page, field)}"
        other = f"correct:{box_subject(page, 'title' if field != 'title' else 'course')}"
        held = stored_answers(browser, draft_id)
        before = assignment_count(browser)
        refused = send(
            browser,
            draft_id,
            page_post(page, "save", typed={box: "", other: "Kept typing"}, tick=units_on(page)),
        )
        answers = stored_answers(browser, draft_id)
        count = assignment_count(browser) - before
        progress = progress_on(browser, draft_id)

    assert refused.status_code == 422
    assert says(refused.text, said)
    assert says(refused.text, imports.PRESS_SAID["nothing_kept"])
    shown = box_of(review_form(refused.text), box)
    assert shown is not None
    assert shown[1] == ""
    assert 'aria-invalid="true"' in shown[0]
    assert box_of(review_form(refused.text), other)[1] == "Kept typing"  # type: ignore[index]
    assert answers == held
    assert count == 0
    assert progress is None


def test_a_cleared_instruction_removes_its_text_and_reads_back(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleared Instructions box removes that text: kept as its entry with null, shown empty
    when the review is opened again, and the card then has no new words."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        box = f"correct:{box_subject(page, 'instruction:0')}"
        reopened = later(browser, draft_id, page, **{box: "  \n "})
        held = progress_after(browser, draft_id)
        page = her_name_answered(browser, draft_id, reopened)
        saved = followed(
            browser, send(browser, draft_id, page_post(page, "save", tick=units_on(page)))
        )
        record = state_of(browser).project_state
        [row] = [a for a in record.all_assignments() if a.title == "Sentence Composing"]
        readings = record.school_instruction_readings([row.assignment_id])

    key = json.loads(box.removeprefix("correct:"))[0]
    assert held == {key: {"instruction:0": None}}
    assert box_of(review_form(reopened), box)[1] == ""  # type: ignore[index]
    assert says(reopened, screenshot_page.SAID["no_new_words"])
    assert says(saved, "Saved: 1 new assignment.")
    standing = readings.readable.get(row.assignment_id)
    assert standing is None or standing.texts == ()


def test_a_cleared_assigned_date_is_the_date_as_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleared Assigned date keeps the date as read: no correction, and one saved before is
    put back to the date as read."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        box = f"correct:{box_subject(page, 'assigned')}"
        cleared = later(browser, draft_id, page, **{box: ""})
        first = progress_after(browser, draft_id)
        moved = later(browser, draft_id, cleared, **{box: "2026-10-02"})
        between = progress_after(browser, draft_id)
        again = later(browser, draft_id, moved, **{box: ""})
        last = progress_after(browser, draft_id)

    key = json.loads(box.removeprefix("correct:"))[0]
    assert first == last == {}
    assert between == {key: {"assigned": "2026-10-02"}}
    assert box_of(review_form(again), box)[1] == "2026-10-05"  # type: ignore[index]
    assert says(again, "Assigned Monday, October 5.")


def saved_progress(browser: TestClient, draft_id: str, progress: object) -> None:
    """Saved progress written as given, as a review saved for later left it."""
    student = state_of(browser).project_state.student_id()
    drafts = state_of(browser).import_drafts
    revision = drafts.snapshot(student, draft_id).progress_revision
    drafts.save_progress(student, draft_id, revision=revision, progress=json.dumps(progress))


def test_shape_three_progress_reads_with_its_own_meaning(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saved progress of shape 3 still reads: its instruction correction replaces every text
    and its due line moves the due date, and the boxes hold what they give."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        key = json.loads(box_subject(page, "title"))[0]
        old = {"instruction": "Read chapter 2.", "footer": "2026-10-09"}
        saved_progress(browser, draft_id, {"v": 3, "corrected": {key: old}, "selected": []})
        shown = browser.get(f"/parent/imports/{draft_id}").text

    form = review_form(shown)
    assert box_of(form, f"correct:{box_subject(shown, 'instruction:0')}")[1] == "Read chapter 2."  # type: ignore[index]
    assert box_of(form, f"correct:{box_subject(shown, 'due')}")[1] == "2026-10-09"  # type: ignore[index]
    assert '<q class="authored-text">Read chapter 2.</q>' in form
    assert '<p class="due">Due Friday, October 9</p>' in form


def test_old_date_corrections_convert_on_the_first_press_and_ask_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A day heading's correction kept in shape 3, its destination answer kept against it: the
    review reads it as the boxes it gives, so where the card goes is asked once, and once the
    card is placed again, Keep these answers never asks again, nor marks anything she never
    typed as not saved; Save review for later then writes it in shape 4."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        page = her_name_answered(browser, draft_id, page)
        [piece] = fields_named(review_form(page), "answer:destination:")
        old = {"heading": "2026-10-06"}
        saved_progress(browser, draft_id, {"v": 3, "corrected": {piece: old}, "selected": []})
        student = state_of(browser).project_state.student_id()
        drafts = state_of(browser).import_drafts
        drafts.keep_answers(
            student,
            draft_id,
            version=drafts.snapshot(student, draft_id).version,
            answers=[
                AnswerToKeep("destination", piece, '{"new":1}', correction_digest(old), (piece,))
            ],
        )
        place = {f"answer:destination:{piece}": '{"new":1}'}
        pages = [browser.get(f"/parent/imports/{draft_id}").text]
        kept = followed(
            browser, send(browser, draft_id, page_post(pages[-1], "keep", choose=place))
        )
        for _ in range(2):
            pages.append(browser.get(f"/parent/imports/{draft_id}").text)
            kept = followed(browser, send(browser, draft_id, page_post(pages[-1], "keep")))
        pages.append(browser.get(f"/parent/imports/{draft_id}").text)
        later_page = later(browser, draft_id, pages[-1])
        held = progress_after(browser, draft_id)

    lost = screenshot_page.SAID["lost_correction"]
    assigned = f"correct:{box_subject(pages[0], 'assigned')}"
    assert [says(shown, lost) for shown in pages] == [True, False, False, False]
    assert all(says(shown, "Assigned Tuesday, October 6.") for shown in [*pages, later_page])
    assert box_of(review_form(pages[0]), assigned)[1] == "2026-10-06"  # type: ignore[index]
    assert not says(kept, screenshot_page.SAID["not_saved_yet"])
    assert not says(kept, imports.PRESS_SAID["unsaved_typing"])
    assert held == {f"date:{piece}": {"assigned": "2026-10-06"}}
    assert not says(later_page, lost)


# ------------------------------------------- instructions the reader was unsure of and read none of


MISSING_WORDS: Final = "Read chapter 3 and answer the questions."


def wordless_review(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str, str, str]:
    """A review of one card whose instructions the reader wasn't sure of and read none of, her
    name answered: its id, the page, the name of its Instructions box and the field of its
    question."""
    card = homework_card(instruction=None, uncertain=[{"field": "instruction", "reason": "faint"}])
    draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer(cards=[card])))
    page = her_name_answered(browser, draft_id, page)
    [confirm] = fields_named(review_form(page), "answer:confirm:")
    [box] = word_boxes(page)
    return draft_id, page, box, f"answer:confirm:{confirm}"


def saved_texts(browser: TestClient) -> tuple[str, ...]:
    """The instruction texts kept for the one English 8 Sentence Composing assignment."""
    record = state_of(browser).project_state
    [row] = [
        a
        for a in record.all_assignments()
        if (a.course, a.title) == ("English 8", "Sentence Composing")
    ]
    found = record.school_instruction_readings([row.assignment_id]).readable.get(row.assignment_id)
    return () if found is None else found.texts


def test_unsure_instructions_read_as_none_offer_one_empty_box_whose_words_save_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Instructions the reader wasn't sure of and read none of: the card's "Correct what was
    read" holds one empty Instructions box, the one the question's link points to. Words typed
    there answer the question and reach the saved assignment once."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, box, _ = wordless_review(browser, monkeypatch)
        typed = {box: MISSING_WORDS}
        kept = followed(browser, send(browser, draft_id, page_post(page, "keep", typed=typed)))
        saved = followed(
            browser, send(browser, draft_id, page_post(kept, "save", tick=units_on(kept)))
        )
        texts = saved_texts(browser)

    form = review_form(page)
    words = [s for s in fields_named(form, "correct:") if json.loads(s)[1].startswith("instr")]
    [question] = re.findall(r"<legend>The reader wasn.*?</fieldset>", form, re.S)
    [fold] = re.findall(r'<a href="#([^"]+)">', question)
    pointed = re.search(rf'<details class="steps" id="{fold}".*?</details>', form, re.S)
    assert words == [box.removeprefix("correct:")]
    assert box_of(form, box) is not None
    assert str(box_of(form, box)[0]).startswith("<textarea ")  # type: ignore[index]
    assert box_of(form, box)[1] == ""  # type: ignore[index]
    assert pointed is not None
    assert box_of(pointed.group(0), box) is not None
    assert not says(kept, screenshot_page.OPEN_NAMES["confirm"])
    assert says(saved, imports.counted(1, 0, 0, 0))
    assert texts == (MISSING_WORDS,)


def test_an_untouched_empty_instructions_box_answers_nothing_and_saves_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty Instructions box left as it is, or holding only spaces: no correction is kept
    or saved for later, the question stays to answer, and the card isn't saved."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, box, _ = wordless_review(browser, monkeypatch)
        before = assignment_count(browser)
        kept = followed(browser, send(browser, draft_id, page_post(page, "keep")))
        spaces = followed(
            browser, send(browser, draft_id, page_post(kept, "keep", typed={box: "  \n "}))
        )
        later_page = later(browser, draft_id, spaces)
        held = progress_after(browser, draft_id)
        refused = followed(
            browser,
            send(browser, draft_id, page_post(later_page, "save", tick=digests_on(later_page))),
        )
        after = assignment_count(browser)

    unsure = screenshot_page.OPEN_NAMES["confirm"]
    for shown in (kept, spaces, later_page):
        assert says(shown, f"Still to answer: {unsure}.")
        assert box_of(review_form(shown), box)[1] == ""  # type: ignore[index]
        assert not says(shown, screenshot_page.SAID["not_saved_yet"])
    assert held == {}
    assert says(refused, REFUSED_OPEN)
    assert after == before


def test_a_removal_kept_for_a_row_read_without_words_answers_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saved progress holding a removal or words for a text the row doesn't have, or words for
    a card that isn't the row's, as a change of screenshots can leave it: the box stays empty,
    the question stays to answer, and the next press keeps none of them."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, box, _ = wordless_review(browser, monkeypatch)
        key = json.loads(box.removeprefix("correct:"))[0]
        gone = f"instruction:{'0' * 64}:0"
        kept = {key: {"instruction:0": None, "instruction:1": MISSING_WORDS, gone: MISSING_WORDS}}
        saved_progress(browser, draft_id, {"v": 4, "corrected": kept, "selected": []})
        shown = browser.get(f"/parent/imports/{draft_id}").text
        later_page = later(browser, draft_id, shown)
        held = progress_after(browser, draft_id)

    unsure = screenshot_page.OPEN_NAMES["confirm"]
    for page_shown in (shown, later_page):
        assert says(page_shown, f"Still to answer: {unsure}.")
        assert box_of(review_form(page_shown), box)[1] == ""  # type: ignore[index]
    assert held == {}


def test_confirm_as_read_with_the_instructions_box_empty_saves_no_instructions(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only "Confirm as read" says the card has no instructions: ticked beside the empty box, it
    answers the question, and the assignment is saved with none."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, _, confirm = wordless_review(browser, monkeypatch)
        posted = page_post(page, "keep", choose={confirm: "as_read"})
        kept = followed(browser, send(browser, draft_id, posted))
        saved = followed(
            browser, send(browser, draft_id, page_post(kept, "save", tick=units_on(kept)))
        )
        texts = saved_texts(browser)

    assert not says(kept, screenshot_page.OPEN_NAMES["confirm"])
    assert says(saved, imports.counted(1, 0, 0, 0))
    assert texts == ()


def test_words_typed_for_missing_instructions_survive_later_reopening_and_a_refused_save(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Words typed in the empty Instructions box are saved for later as the text the reading
    lacked, held by the box when the review opens again, when a save is refused for a cleared
    title and when the title is put back, and saved on the assignment once."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, box, _ = wordless_review(browser, monkeypatch)
        title = f"correct:{box_subject(page, 'title')}"
        later_page = later(browser, draft_id, page, **{box: MISSING_WORDS})
        held = progress_after(browser, draft_id)
        reopened = browser.get(f"/parent/imports/{draft_id}").text
        cleared = {title: ""}
        refused = send(
            browser,
            draft_id,
            page_post(reopened, "save", tick=units_on(reopened), typed=cleared),
        )
        restored = {title: "Sentence Composing"}
        kept = followed(
            browser, send(browser, draft_id, page_post(refused.text, "keep", typed=restored))
        )
        saved = followed(
            browser, send(browser, draft_id, page_post(kept, "save", tick=units_on(kept)))
        )
        texts = saved_texts(browser)

    key, field = json.loads(box.removeprefix("correct:"))
    assert held == {key: {field: MISSING_WORDS}}
    for shown in (later_page, reopened, refused.text, kept):
        assert box_of(review_form(shown), box)[1] == MISSING_WORDS  # type: ignore[index]
        assert not says(shown, screenshot_page.OPEN_NAMES["confirm"])
    assert refused.status_code == 422
    assert says(refused.text, screenshot_page.NEEDED["title"])
    assert says(saved, imports.counted(1, 0, 0, 0))
    assert texts == (MISSING_WORDS,)


def test_old_progress_with_words_for_a_row_read_without_any_reads_back_in_its_box(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saved progress of shape 3 holding instructions typed for a row read with none: the empty
    box holds those words, which answer the question; the next press writes them as the text
    the reading lacked, and they are saved once."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, box, _ = wordless_review(browser, monkeypatch)
        key = json.loads(box.removeprefix("correct:"))[0]
        old = {"v": 3, "corrected": {key: {"instruction": f" {MISSING_WORDS} "}}, "selected": []}
        saved_progress(browser, draft_id, old)
        shown = browser.get(f"/parent/imports/{draft_id}").text
        later_page = later(browser, draft_id, shown)
        held = progress_after(browser, draft_id)
        saved = followed(
            browser,
            send(browser, draft_id, page_post(later_page, "save", tick=units_on(later_page))),
        )
        texts = saved_texts(browser)

    field = json.loads(box.removeprefix("correct:"))[1]
    assert box_of(review_form(shown), box)[1] == MISSING_WORDS  # type: ignore[index]
    assert not says(shown, screenshot_page.OPEN_NAMES["confirm"])
    assert held == {key: {field: MISSING_WORDS}}
    assert says(saved, imports.counted(1, 0, 0, 0))
    assert texts == (MISSING_WORDS,)


# ------------------------------------------- an unsure card read without words, beside others


WORD_BOX: Final = re.compile(r"instruction:[0-9a-f]{64}:(0|[1-9][0-9]*)")
"""The field of the Instructions box of a card read without words."""


def word_boxes(page: str) -> list[str]:
    """The names of the Instructions boxes of cards read without words, in page order."""
    return [
        f"correct:{subject}"
        for subject in fields_named(review_form(page), "correct:")
        if WORD_BOX.fullmatch(json.loads(subject)[1])
    ]


def label_of(page: str, name: str) -> str:
    """The visible words of a box's label."""
    found = re.search(
        rf'<label class="lines">([^<]*)<span class="visually-hidden">[^<]*</span> '
        rf'<textarea name="{re.escape(str(escape(name)))}"',
        page,
    )
    assert found is not None
    return found.group(1)


def kept_texts(browser: TestClient, title: str = "Sentence Composing") -> list[tuple[str, ...]]:
    """The instruction texts kept for each English 8 assignment of this title."""
    record = state_of(browser).project_state
    rows = [a for a in record.all_assignments() if (a.course, a.title) == ("English 8", title)]
    readable = record.school_instruction_readings([a.assignment_id for a in rows]).readable
    return [readable[a.assignment_id].texts if a.assignment_id in readable else () for a in rows]


UNSURE_WORDS: Final = [{"field": "instruction", "reason": "faint"}]
READ_AT: Final = datetime(2026, 10, 6, 12, tzinfo=UTC)
"""An instant to read pieces at; no key or word reads it."""


def on_day(day: str, *cards: dict[str, Any]) -> dict[str, Any]:
    return homework_answer(headings=[{"text": day, "y": 150, "uncertain": None}], cards=list(cards))


def joined_answers() -> tuple[dict[str, Any], dict[str, Any]]:
    """The reported case: an assigned card with words under Monday, and the same assignment's
    due card under Thursday, read without words and unsure of them."""
    return (
        on_day("Monday 10/5/2026", homework_card()),
        on_day(
            "Thursday 10/8/2026",
            homework_card(badge="DUE", instruction=None, footer=None, uncertain=UNSURE_WORDS),
        ),
    )


def test_an_unsure_card_read_without_words_beside_one_with_words_has_its_own_box(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The due card read without words beside the assigned card's words: the row keeps its
    text's box and adds one empty box for the due card, labeled with its screenshot and card.
    Left empty it answers nothing; words typed there survive Save review for later and
    reopening, and are saved once beside the other card's words."""
    monday, thursday = joined_answers()
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, thursday))
        page = her_name_answered(browser, draft_id, page)
        [box] = word_boxes(page)
        text = f"correct:{box_subject(page, 'instruction:0')}"
        kept = followed(browser, send(browser, draft_id, page_post(page, "keep")))
        later_page = later(browser, draft_id, kept, **{box: MISSING_WORDS})
        held = progress_after(browser, draft_id)
        reopened = browser.get(f"/parent/imports/{draft_id}").text
        saved = followed(
            browser, send(browser, draft_id, page_post(reopened, "save", tick=units_on(reopened)))
        )
        texts = kept_texts(browser)

    form = review_form(page)
    key, field = json.loads(box.removeprefix("correct:"))
    unsure = screenshot_page.OPEN_NAMES["confirm"]
    assert box_of(form, text)[1] == "Complete pages 12 to 14."  # type: ignore[index]
    assert box_of(form, box)[1] == ""  # type: ignore[index]
    assert label_of(form, box) == "Instructions on screenshot 2, card 1"
    assert label_of(form, text) == "Instructions"
    assert says(kept, f"Still to answer: {unsure}.")
    assert held == {key: {field: MISSING_WORDS}}
    for shown in (later_page, reopened):
        assert box_of(review_form(shown), box)[1] == MISSING_WORDS  # type: ignore[index]
        assert box_of(review_form(shown), text)[1] == "Complete pages 12 to 14."  # type: ignore[index]
        assert not says(shown, unsure)
    assert says(saved, imports.counted(1, 0, 0, 0))
    assert texts == [("Complete pages 12 to 14.", MISSING_WORDS)]


@pytest.mark.parametrize(
    ("first", "second", "saved"),
    [
        (
            "Read chapter 3.",
            "Answer questions 1 to 5.",
            ("Read chapter 3.", "Answer questions 1 to 5."),
        ),
        ("Read chapter 3.", "Read chapter 3.", ("Read chapter 3.",)),
    ],
    ids=["different words", "the same words"],
)
def test_each_unsure_card_read_without_words_has_its_own_box_and_its_words_are_saved_once(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    second: str,
    saved: tuple[str, ...],
) -> None:
    """Two cards of one assignment read without words, each unsure: one empty box each, named
    by its screenshot and card. What each holds survives Save review for later and reopening,
    and is saved once, the same words once in all."""
    assigned = homework_card(instruction=None, uncertain=UNSURE_WORDS)
    due = homework_card(badge="DUE", instruction=None, footer=None, uncertain=UNSURE_WORDS)
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser,
            monkeypatch,
            (RED, on_day("Monday 10/5/2026", assigned)),
            (BLUE, on_day("Thursday 10/8/2026", due)),
        )
        page = her_name_answered(browser, draft_id, page)
        boxes = word_boxes(page)
        typed = dict(zip(boxes, (first, second), strict=True))
        later_page = later(browser, draft_id, page, **typed)
        held = progress_after(browser, draft_id)
        reopened = browser.get(f"/parent/imports/{draft_id}").text
        pressed = followed(
            browser, send(browser, draft_id, page_post(reopened, "save", tick=units_on(reopened)))
        )
        texts = kept_texts(browser)

    form = review_form(page)
    [key] = {json.loads(b.removeprefix("correct:"))[0] for b in boxes}
    assert len(boxes) == 2
    assert [label_of(form, b) for b in boxes] == [
        "Instructions on screenshot 1, card 1",
        "Instructions on screenshot 2, card 1",
    ]
    assert held == {key: {json.loads(b.removeprefix("correct:"))[1]: typed[b] for b in boxes}}
    for shown in (later_page, reopened):
        assert [box_of(review_form(shown), b)[1] for b in boxes] == [first, second]  # type: ignore[index]
    assert says(pressed, imports.counted(1, 0, 0, 0))
    assert [set(found) for found in texts] == [set(saved)]
    assert [len(found) for found in texts] == [len(saved)]


def test_words_for_an_unsure_card_go_to_its_own_piece_and_leave_the_other_piece_s_answer(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row split by its assigned days, Tuesday's card read without words and unsure: its box
    sits with the row's boxes, labeled with its screenshot and card, and the words typed there
    go to Tuesday's piece alone. Kept with those words and opened again before they were saved,
    only Tuesday's piece asks where it goes again; Monday's keeps its words and its answer."""
    monday = on_day("Monday 10/5/2026", homework_card(instruction="Read chapter 1."))
    tuesday = on_day("Tuesday 10/6/2026", homework_card(instruction=None, uncertain=UNSURE_WORDS))
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, tuesday))
        page = her_name_answered(browser, draft_id, page)
        [row] = fields_named(review_form(page), "answer:split:")
        keep(browser, draft_id, ("split", row, json.dumps("split")))
        split = browser.get(f"/parent/imports/{draft_id}").text
        days = fields_named(review_form(split), "answer:destination:")
        chosen = {f"answer:destination:{d}": f'{{"new":{n}}}' for n, d in enumerate(days, 1)}
        placed = followed(browser, send(browser, draft_id, page_post(split, "keep", choose=chosen)))
        [box] = word_boxes(placed)
        send(browser, draft_id, page_post(placed, "keep", typed={box: MISSING_WORDS}))
        reopened = browser.get(f"/parent/imports/{draft_id}").text
        student = state_of(browser).project_state.student_id()
        tables = state_of(browser).project_state.draft_for_save(student, draft_id)
        assert tables is not None
        key, field = json.loads(box.removeprefix("correct:"))
        pieces, _, _ = screenshot_review.pieces_from(
            tables.readings,
            {row: "split"},
            frozenset(),
            READ_AT,
            {key: {field: MISSING_WORDS}},
        )

    by_day = {p.name.split("|")[1]: p for p in pieces}
    assert label_of(review_form(placed), box) == "Instructions on screenshot 2, card 1"
    assert by_day["2026-10-05"].words == frozenset({"Read chapter 1."})
    assert by_day["2026-10-06"].words == frozenset({MISSING_WORDS})
    assert [
        name for name, _ in checked_values(review_form(reopened)) if "destination:" in name
    ] == [f"answer:destination:{days[0]}"]
    assert reopened.count(str(escape(screenshot_page.SAID["lost_correction"]))) == 1


def still_open_tick_and_save(
    browser: TestClient, draft_id: str, page: str
) -> tuple[bool, bool, str]:
    """Whether the page names the unsure field still to answer, whether its group can be
    ticked, and what Save of every group says."""
    unsure = says(page, f"Still to answer: {screenshot_page.OPEN_NAMES['confirm']}.")
    tickable = bool(units_on(page))
    saved = followed(
        browser, send(browser, draft_id, page_post(page, "save", tick=digests_on(page)))
    )
    return unsure, tickable, saved


def test_a_correction_of_another_card_s_words_doesn_t_answer_an_unsure_card_read_without_any(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported case with only the assigned card's words corrected: the due card's empty box
    is untouched, so its instructions stay to answer, its group can't be ticked and Save
    refuses it. "Confirm as read" then says it has none, and the edited words are saved
    alone."""
    monday, thursday = joined_answers()
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, thursday))
        page = her_name_answered(browser, draft_id, page)
        text = f"correct:{box_subject(page, 'instruction:0')}"
        [box] = word_boxes(page)
        [confirm] = fields_named(review_form(page), "answer:confirm:")
        edited = {text: "Complete pages 12 to 16."}
        kept = followed(browser, send(browser, draft_id, page_post(page, "keep", typed=edited)))
        before = assignment_count(browser)
        unsure, tickable, refused = still_open_tick_and_save(browser, draft_id, kept)
        assert (unsure, tickable) == (True, False)
        assert says(refused, REFUSED_OPEN)
        assert assignment_count(browser) == before
        chosen = {f"answer:confirm:{confirm}": "as_read"}
        confirmed = followed(
            browser,
            send(browser, draft_id, page_post(kept, "keep", typed=edited, choose=chosen)),
        )
        still, ready, saved = still_open_tick_and_save(browser, draft_id, confirmed)
        texts = kept_texts(browser)

    assert box_of(review_form(kept), box)[1] == ""  # type: ignore[index]
    assert (still, ready) == (False, True)
    assert says(saved, imports.counted(1, 0, 0, 0))
    assert texts == [("Complete pages 12 to 16.",)]


def test_each_unsure_card_read_without_words_needs_its_own_words_or_the_confirmation(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two cards of one assignment read without words, each unsure, words typed in one box
    alone: the other card's instructions stay to answer, its group can't be ticked and Save
    refuses it. "Confirm as read" then answers for the other, and the one card's words are
    saved alone."""
    assigned = homework_card(instruction=None, uncertain=UNSURE_WORDS)
    due = homework_card(badge="DUE", instruction=None, footer=None, uncertain=UNSURE_WORDS)
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser,
            monkeypatch,
            (RED, on_day("Monday 10/5/2026", assigned)),
            (BLUE, on_day("Thursday 10/8/2026", due)),
        )
        page = her_name_answered(browser, draft_id, page)
        first, _ = word_boxes(page)
        [confirm] = fields_named(review_form(page), "answer:confirm:")
        typed = {first: MISSING_WORDS}
        kept = followed(browser, send(browser, draft_id, page_post(page, "keep", typed=typed)))
        before = assignment_count(browser)
        unsure, tickable, refused = still_open_tick_and_save(browser, draft_id, kept)
        assert (unsure, tickable) == (True, False)
        assert says(refused, REFUSED_OPEN)
        assert assignment_count(browser) == before
        chosen = {f"answer:confirm:{confirm}": "as_read"}
        confirmed = followed(
            browser, send(browser, draft_id, page_post(kept, "keep", typed=typed, choose=chosen))
        )
        still, ready, saved = still_open_tick_and_save(browser, draft_id, confirmed)
        texts = kept_texts(browser)

    assert (still, ready) == (False, True)
    assert says(saved, imports.counted(1, 0, 0, 0))
    assert texts == [(MISSING_WORDS,)]


def test_words_for_a_card_read_without_any_don_t_answer_an_unsure_card_that_read_words(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assigned card read words the reader wasn't sure of, the due card read none and the
    reader wasn't sure of those either. Words typed in the due card's box alone leave the
    assigned card's words to answer: still to answer, no tick, and Save refuses with nothing
    written. "Confirm as read" then answers for them, and they are saved as read beside the
    typed words."""
    monday = on_day("Monday 10/5/2026", homework_card(uncertain=UNSURE_WORDS))
    _, thursday = joined_answers()
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, thursday))
        page = her_name_answered(browser, draft_id, page)
        [box] = word_boxes(page)
        [confirm] = fields_named(review_form(page), "answer:confirm:")
        typed = {box: MISSING_WORDS}
        kept = followed(browser, send(browser, draft_id, page_post(page, "keep", typed=typed)))
        before = assignment_count(browser)
        unsure, tickable, refused = still_open_tick_and_save(browser, draft_id, kept)
        assert (unsure, tickable) == (True, False)
        assert says(refused, REFUSED_OPEN)
        assert assignment_count(browser) == before
        chosen = {f"answer:confirm:{confirm}": "as_read"}
        confirmed = followed(
            browser, send(browser, draft_id, page_post(kept, "keep", typed=typed, choose=chosen))
        )
        still, ready, saved = still_open_tick_and_save(browser, draft_id, confirmed)
        texts = kept_texts(browser)

    assert (still, ready) == (False, True)
    assert says(saved, imports.counted(1, 0, 0, 0))
    assert texts == [("Complete pages 12 to 14.", MISSING_WORDS)]


@pytest.mark.parametrize(
    ("words", "saved"),
    [("Complete pages 12 to 16.", ("Complete pages 12 to 16.",)), ("", ())],
    ids=["replaced", "cleared"],
)
def test_a_correction_of_an_unsure_card_s_own_words_answers_it(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    words: str,
    saved: tuple[str, ...],
) -> None:
    """One card whose words the reader wasn't sure of: its words replaced, or cleared, answer
    it, so the group can be ticked and Save writes what the box holds."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, boxes = unsure_review(browser, monkeypatch, "instruction")
        typed = {boxes["instruction"]: words}
        kept = followed(browser, send(browser, draft_id, page_post(page, "keep", typed=typed)))
        unsure, tickable, pressed = still_open_tick_and_save(browser, draft_id, kept)
        texts = kept_texts(browser)

    assert (unsure, tickable) == (False, True)
    assert says(pressed, "Saved: 1 new assignment.")
    assert texts == [saved]


def test_a_correction_of_one_unsure_card_s_words_doesn_t_answer_another_s(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both cards read different words the reader wasn't sure of: correcting the assigned
    card's words alone leaves the due card's to answer; correcting both answers them."""
    due = homework_card(
        badge="DUE", instruction="Bring the workbook.", footer=None, uncertain=UNSURE_WORDS
    )
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser,
            monkeypatch,
            (RED, on_day("Monday 10/5/2026", homework_card(uncertain=UNSURE_WORDS))),
            (BLUE, on_day("Thursday 10/8/2026", due)),
        )
        page = her_name_answered(browser, draft_id, page)
        first, second = (f"correct:{box_subject(page, f'instruction:{i}')}" for i in range(2))
        one = {first: "Complete pages 12 to 16."}
        kept = followed(browser, send(browser, draft_id, page_post(page, "keep", typed=one)))
        both = {**one, second: "Bring the workbook and a pencil."}
        again = followed(browser, send(browser, draft_id, page_post(kept, "keep", typed=both)))

    unsure = f"Still to answer: {screenshot_page.OPEN_NAMES['confirm']}."
    assert box_of(review_form(page), second)[1] == "Bring the workbook."  # type: ignore[index]
    assert (says(kept, unsure), bool(units_on(kept))) == (True, False)
    assert (says(again, unsure), bool(units_on(again))) == (False, True)


def test_one_text_read_by_two_unsure_cards_is_answered_by_its_one_box(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assigned and due cards read the same words, the reader unsure of both: the row has
    one box for those words, and correcting it answers for both cards."""
    unsure_due = homework_card(badge="DUE", footer=None, uncertain=UNSURE_WORDS)
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser,
            monkeypatch,
            (RED, on_day("Monday 10/5/2026", homework_card(uncertain=UNSURE_WORDS))),
            (BLUE, on_day("Thursday 10/8/2026", unsure_due)),
        )
        page = her_name_answered(browser, draft_id, page)
        words_boxes = [
            subject
            for subject in fields_named(review_form(page), "correct:")
            if json.loads(subject)[1].startswith("instruction")
        ]
        typed = {f"correct:{box_subject(page, 'instruction:0')}": "Complete pages 12 to 16."}
        kept = followed(browser, send(browser, draft_id, page_post(page, "keep", typed=typed)))
        unsure, tickable, pressed = still_open_tick_and_save(browser, draft_id, kept)
        texts = kept_texts(browser)

    assert [json.loads(subject)[1] for subject in words_boxes] == ["instruction:0"]
    assert (unsure, tickable) == (False, True)
    assert says(pressed, imports.counted(1, 0, 0, 0))
    assert texts == [("Complete pages 12 to 16.",)]


def test_on_a_split_row_the_question_waits_for_the_unsure_card_s_own_words(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A split row whose Tuesday card was read without words and unsure, its question on
    Monday's card: Monday's words corrected alone leave it to answer there too, so the first
    question to answer is that question; Tuesday's words answer it."""
    monday = on_day("Monday 10/5/2026", homework_card(instruction="Read chapter 1."))
    tuesday = on_day("Tuesday 10/6/2026", homework_card(instruction=None, uncertain=UNSURE_WORDS))
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, tuesday))
        page = her_name_answered(browser, draft_id, page)
        [row] = fields_named(review_form(page), "answer:split:")
        keep(browser, draft_id, ("split", row, json.dumps("split")))
        split = browser.get(f"/parent/imports/{draft_id}").text
        days = fields_named(review_form(split), "answer:destination:")
        chosen = {f"answer:destination:{d}": f'{{"new":{n}}}' for n, d in enumerate(days, 1)}
        text = f"correct:{box_subject(split, 'instruction:0')}"
        [box] = word_boxes(split)
        edited = {text: "Read chapter 1 twice."}
        monday_only = followed(
            browser,
            send(browser, draft_id, page_post(split, "keep", typed=edited, choose=chosen)),
        )
        both = followed(
            browser,
            send(
                browser,
                draft_id,
                page_post(monday_only, "keep", typed={**edited, box: MISSING_WORDS}),
            ),
        )

    unsure = screenshot_page.OPEN_NAMES["confirm"]
    [question] = re.findall(
        r'<fieldset[^>]*id="(ask-\d+)"[^>]*>\s*<legend>The reader wasn', review_form(split)
    )
    link = FIRST_OPEN.search(monday_only)
    assert says(monday_only, f"Still to answer: {unsure}.")
    assert link is not None
    assert link.group(1) == question
    assert not says(both, unsure)
    later_link = FIRST_OPEN.search(both)
    assert later_link is None or later_link.group(1) != question


def test_only_a_card_the_reader_was_unsure_of_and_read_without_words_has_an_empty_box(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Of two cards read without words, only the one whose instructions the reader wasn't sure
    of has an empty Instructions box; Chains Reading Guide 6, read without words and sure, has
    none."""
    unsure = homework_card(instruction=None, uncertain=UNSURE_WORDS)
    sure = homework_answer()["cards"][1]
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[unsure, sure]))
        )
        page = her_name_answered(browser, draft_id, page)

    form = review_form(page)
    [box] = word_boxes(page)
    assert (sure["title"], sure["instruction"], sure["uncertain"]) == (
        "Chains Reading Guide 6",
        None,
        [],
    )
    assert label_of(form, box) == "Instructions on screenshot 1, card 1"


def split_unsure_dates(field: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """One assigned card read under Monday and under Tuesday, the reader unsure of the day
    heading over each (``heading``) or of the card's due line (``footer``)."""
    marks = [{"field": "footer", "reason": "faint"}] if field == "footer" else []
    unsure = "faint" if field == "heading" else None
    card = homework_card(footer="Due (10/9/2026)", uncertain=marks)

    def under(day: str) -> dict[str, Any]:
        return homework_answer(
            headings=[{"text": day, "y": 150, "uncertain": unsure}], cards=[card]
        )

    return under("Monday 10/5/2026"), under("Tuesday 10/6/2026")


@pytest.mark.parametrize(
    ("field", "box", "typed", "named"),
    [
        ("heading", "assigned", "2026-10-04", "assigned date (day heading)"),
        ("footer", "due", "2026-10-10", "due date"),
    ],
    ids=["assigned date", "due date"],
)
def test_each_piece_of_a_split_row_asks_of_its_own_unsure_date(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    box: str,
    typed: str,
    named: str,
) -> None:
    """A row split by its assigned days, the reader unsure of a date on each piece: each piece
    asks of its own date. Monday's date corrected alone leaves Tuesday's question open, the
    first question to answer, with no tick and Save refused; Tuesday's "Confirm as read"
    answers it alone, and the row saves."""
    monday, tuesday = split_unsure_dates(field)
    legend = screenshot_page.SAID["unsure"].format(named=named)
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, tuesday))
        page = her_name_answered(browser, draft_id, page)
        [row] = fields_named(review_form(page), "answer:split:")
        keep(browser, draft_id, ("split", row, json.dumps("split")))
        split = browser.get(f"/parent/imports/{draft_id}").text
        days = fields_named(review_form(split), "answer:destination:")
        chosen = {f"answer:destination:{d}": f'{{"new":{n}}}' for n, d in enumerate(days, 1)}
        first = {f"correct:{box_subject(split, box, 0)}": typed}
        kept = followed(
            browser, send(browser, draft_id, page_post(split, "keep", typed=first, choose=chosen))
        )
        questions = re.findall(
            rf'<fieldset[^>]*id="(ask-\d+)"[^>]*>\s*<legend>{re.escape(str(escape(legend)))}',
            review_form(kept),
        )
        link = FIRST_OPEN.search(kept)
        before = assignment_count(browser)
        unsure, tickable, refused = still_open_tick_and_save(browser, draft_id, kept)
        assert (unsure, tickable) == (True, False)
        assert says(refused, REFUSED_OPEN)
        assert assignment_count(browser) == before
        confirms = fields_named(review_form(kept), "answer:confirm:")
        tuesday_only = {f"answer:confirm:{confirms[-1]}": "as_read"}
        confirmed = followed(
            browser,
            send(browser, draft_id, page_post(kept, "keep", typed=first, choose=tuesday_only)),
        )
        still, ready, saved = still_open_tick_and_save(browser, draft_id, confirmed)

    assert len(questions) == 2
    assert link is not None
    assert link.group(1) == questions[1]
    assert len(confirms) == 2
    assert (still, ready) == (False, True)
    assert says(saved, imports.counted(2, 0, 0, 0))


@pytest.mark.parametrize("field", ["heading", "footer"], ids=["assigned date", "due date"])
def test_confirm_as_read_answers_only_its_own_piece_s_date(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    """A split row, the reader unsure of a date on each piece: Monday's "Confirm as read" alone
    leaves Tuesday's question the first to answer, with no tick and Save refused; both
    confirmed, the row saves, and the record counts two dates confirmed."""
    monday, tuesday = split_unsure_dates(field)
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, tuesday))
        page = her_name_answered(browser, draft_id, page)
        [row] = fields_named(review_form(page), "answer:split:")
        keep(browser, draft_id, ("split", row, json.dumps("split")))
        split = browser.get(f"/parent/imports/{draft_id}").text
        days = fields_named(review_form(split), "answer:destination:")
        chosen = {f"answer:destination:{d}": f'{{"new":{n}}}' for n, d in enumerate(days, 1)}
        mon, tue = fields_named(review_form(split), "answer:confirm:")
        questions = re.findall(
            r'<fieldset[^>]*id="(ask-\d+)"[^>]*>\s*<legend>The reader wasn', split
        )
        first = followed(
            browser,
            send(
                browser,
                draft_id,
                page_post(split, "keep", choose={**chosen, f"answer:confirm:{mon}": "as_read"}),
            ),
        )
        link = FIRST_OPEN.search(first)
        unsure, tickable, refused = still_open_tick_and_save(browser, draft_id, first)
        assert (unsure, tickable) == (True, False)
        assert says(refused, REFUSED_OPEN)
        both = followed(
            browser,
            send(
                browser,
                draft_id,
                page_post(first, "keep", choose={f"answer:confirm:{tue}": "as_read"}),
            ),
        )
        still, ready, saved = still_open_tick_and_save(browser, draft_id, both)
        counts = (
            sqlite3.connect(tmp_path / "blossom.sqlite3")
            .execute("SELECT corrected, confirmed FROM grade_acceptances")
            .fetchall()
        )

    assert [json.loads(subject) for subject in (mon, tue)] == [[days[0], field], [days[1], field]]
    assert link is not None
    assert link.group(1) == questions[1]
    assert (still, ready) == (False, True)
    assert says(saved, imports.counted(2, 0, 0, 0))
    assert counts == [(0, 2)]


def test_an_old_instruction_correction_on_a_split_row_reaches_every_piece(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saved progress of shape 3 holding one instruction correction for a split row whose
    pieces read different words: it replaced every text, so every piece holds the corrected
    words, before the first press converts it and after."""
    words = "Read both chapters."
    monday = on_day("Monday 10/5/2026", homework_card(instruction="Read chapter 1."))
    tuesday = on_day("Tuesday 10/6/2026", homework_card(instruction="Read chapter 2."))
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, tuesday))
        page = her_name_answered(browser, draft_id, page)
        [row] = fields_named(review_form(page), "answer:split:")
        keep(browser, draft_id, ("split", row, json.dumps("split")))
        saved_progress(
            browser, draft_id, {"v": 3, "corrected": {row: {"instruction": words}}, "selected": []}
        )
        shown = browser.get(f"/parent/imports/{draft_id}").text
        later(browser, draft_id, shown)
        held = progress_after(browser, draft_id)
        student = state_of(browser).project_state.student_id()
        tables = state_of(browser).project_state.draft_for_save(student, draft_id)
        assert tables is not None

    def words_of(corrections: screenshot_review.Corrections) -> list[frozenset[str]]:
        pieces, _, _ = screenshot_review.pieces_from(
            tables.readings, {row: "split"}, frozenset(), READ_AT, corrections
        )
        return [p.words for p in pieces if p.key == row]

    texts = [f"correct:{box_subject(shown, f'instruction:{i}')}" for i in range(2)]
    assert [box_of(review_form(shown), t)[1] for t in texts] == [words, words]  # type: ignore[index]
    assert words_of({row: {"instruction": words}}) == [frozenset({words})] * 2
    assert held == {row: {"instruction:0": words, "instruction:1": words}}
    assert words_of(held) == [frozenset({words})] * 2


@pytest.mark.parametrize("bare", [None, ""], ids=["no instruction", "an empty instruction"])
def test_an_old_instruction_correction_reaches_a_split_piece_read_without_words_and_sure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, bare: str | None
) -> None:
    """Saved progress of shape 3 holding one instruction correction for a split row whose
    Tuesday card was read without words, the reader sure of it: the correction added the words
    to Tuesday's piece, so they convert onto that card's own box, shown with its words and
    named by its screenshot and card, and every piece saves them once."""
    words = "Read both chapters."
    monday = on_day("Monday 10/5/2026", homework_card(instruction="Read chapter 1."))
    tuesday = on_day("Tuesday 10/6/2026", homework_card(instruction=bare))
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, tuesday))
        page = her_name_answered(browser, draft_id, page)
        [row] = fields_named(review_form(page), "answer:split:")
        keep(browser, draft_id, ("split", row, json.dumps("split")))
        old = {"v": 3, "corrected": {row: {"instruction": words}}, "selected": []}
        saved_progress(browser, draft_id, old)
        shown = browser.get(f"/parent/imports/{draft_id}").text
        later_page = later(browser, draft_id, shown)
        held = progress_after(browser, draft_id)
        student = state_of(browser).project_state.student_id()
        tables = state_of(browser).project_state.draft_for_save(student, draft_id)
        assert tables is not None
        days = fields_named(review_form(later_page), "answer:destination:")
        chosen = {f"answer:destination:{d}": f'{{"new":{n}}}' for n, d in enumerate(days, 1)}
        placed = followed(
            browser, send(browser, draft_id, page_post(later_page, "keep", choose=chosen))
        )
        _, _, saved = still_open_tick_and_save(browser, draft_id, placed)
        texts = kept_texts(browser)

    [box] = word_boxes(shown)
    field = json.loads(box.removeprefix("correct:"))[1]
    pieces, _, _ = screenshot_review.pieces_from(
        tables.readings, {row: "split"}, frozenset(), READ_AT, held
    )
    assert label_of(review_form(shown), box) == "Instructions on screenshot 2, card 1"
    assert box_of(review_form(shown), box)[1] == words  # type: ignore[index]
    assert held == {row: {"instruction:0": words, field: words}}
    assert [p.words for p in pieces if p.key == row] == [frozenset({words})] * 2
    assert says(saved, imports.counted(2, 0, 0, 0))
    assert texts == [(words,), (words,)]


@pytest.mark.parametrize(
    ("heading", "read_a_date"),
    [("Monday", False), ("Monday 10/5/2026", True)],
    ids=["no date read", "a date read"],
)
def test_no_assigned_date_given_on_an_unsure_heading_confirms_only_where_no_date_was_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, heading: str, read_a_date: bool
) -> None:
    """ "No assigned date was given" ticked on an assigned card under a day heading the reader
    wasn't sure of answers its question either way. Where no date was read it is that piece's
    "Confirm as read", writing no correction; where a date was read it stays the correction to
    none, with no confirmation kept."""
    card = homework_card(footer="Due (10/8/2026)")
    answer = homework_answer(
        headings=[{"text": heading, "y": 150, "uncertain": "faint"}], cards=[card]
    )
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        page = her_name_answered(browser, draft_id, page)
        [none] = fields_named(review_form(page), "none:")
        [confirm] = fields_named(review_form(page), "answer:confirm:")
        ticked = {f"none:{none}": "none"}
        kept = followed(browser, send(browser, draft_id, page_post(page, "keep", choose=ticked)))
        answers = stored_answers(browser, draft_id)
        later(browser, draft_id, kept)
        held = progress_after(browser, draft_id)

    piece = json.loads(none)[0]
    assert not says(kept, f"Still to answer: {screenshot_page.OPEN_NAMES['confirm']}.")
    if read_a_date:
        assert held == {piece: {"assigned": None}}
        assert ("confirm", confirm) not in answers
    else:
        assert held == {}
        assert answers[("confirm", confirm)] == "as_read"


def test_no_assigned_date_given_answers_an_unsure_day_heading_that_read_no_date(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An assigned card under a day heading the reader wasn't sure of and read no date from:
    "No assigned date was given" ticked answers its question, writing no correction since none
    was read. It stays ticked through Keep these answers, Save review for later and reopening,
    and the card saves with no assigned date."""
    card = homework_card(footer="Due (10/8/2026)")
    answer = homework_answer(
        headings=[{"text": "Monday", "y": 150, "uncertain": "faint"}], cards=[card]
    )
    unsure = f"Still to answer: {screenshot_page.OPEN_NAMES['confirm']}."
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        page = her_name_answered(browser, draft_id, page)
        [none] = fields_named(review_form(page), "none:")
        ticked = {f"none:{none}": "none"}
        kept = followed(browser, send(browser, draft_id, page_post(page, "keep", choose=ticked)))
        still, tickable = says(kept, unsure), bool(units_on(kept))
        later_page = later(browser, draft_id, kept)
        held = progress_after(browser, draft_id)
        reopened = browser.get(f"/parent/imports/{draft_id}").text
        again, ready, saved = still_open_tick_and_save(browser, draft_id, reopened)
        made = [
            a
            for a in state_of(browser).project_state.all_assignments()
            if a.title == "Sentence Composing"
        ]

    checked = f'name="{escape("none:" + none)}" value="none" checked'
    assert says(page, unsure)
    assert (still, tickable) == (False, True)
    assert held == {}
    for shown in (kept, later_page, reopened):
        assert checked in review_form(shown)
    assert (again, ready) == (False, True)
    assert says(saved, imports.counted(1, 0, 0, 0))
    assert [row.assigned_on for row in made] == [None]


@pytest.mark.parametrize(
    ("old", "box"),
    [("footer", "due"), ("heading", "assigned")],
)
def test_old_date_text_blossom_can_t_read_is_kept_as_typed_in_its_box(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, old: str, box: str
) -> None:
    """Saved progress of shape 3 holding a due line or day heading Blossom can't read as a date:
    the box that date goes to holds the text as typed, says it can't be read, and Save review
    for later keeps it as typed there."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        page = her_name_answered(browser, draft_id, page)
        key = json.loads(box_subject(page, "title"))[0]
        saved_progress(
            browser, draft_id, {"v": 3, "corrected": {key: {old: "next Tuesday"}}, "selected": []}
        )
        shown = browser.get(f"/parent/imports/{draft_id}").text
        later_page = later(browser, draft_id, shown)
        held = progress_after(browser, draft_id)

    named = f"correct:{box_subject(shown, box)}"
    said = screenshot_page.SAID["unreadable"].format(value="next Tuesday")
    for page_shown in (shown, later_page):
        assert says(page_shown, said)
        assert box_of(review_form(page_shown), f"refused:{named.removeprefix('correct:')}")
    assert held == {f"date:{key}": {box: "next Tuesday"}}


@pytest.mark.parametrize("press", ["tick", "tick beside a date", "untick"])
def test_no_assigned_date_given_wins_over_an_unreadable_date_kept_for_later(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, press: str
) -> None:
    """An assigned date kept for later as text Blossom can't read reopens with its box empty and
    the text carried beside it. "No assigned date was given" ticked with the box empty wins:
    no assigned date, the text dropped from progress for good, the card answered and saved
    with none. Ticked beside a date typed in the box, it is refused as both. Unticked, the
    text stays as it was: still to answer, not tickable, kept for later."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        page = her_name_answered(browser, draft_id, page)
        piece = json.loads(box_subject(page, "assigned"))[0]
        kept = {"v": 4, "corrected": {piece: {"assigned": "next Tuesday"}}, "selected": []}
        saved_progress(browser, draft_id, kept)
        reopened = browser.get(f"/parent/imports/{draft_id}").text
        box = f"correct:{box_subject(reopened, 'assigned')}"
        [none] = fields_named(review_form(reopened), "none:")
        choose = {} if press == "untick" else {f"none:{none}": "none"}
        typed = {box: "2026-10-03"} if press == "tick beside a date" else {}
        answer = send(browser, draft_id, page_post(reopened, "keep", choose=choose, typed=typed))
        after = followed(browser, answer)
        if answer.status_code == 200:
            later_page = later(browser, draft_id, after)
            held = progress_after(browser, draft_id)
            again = browser.get(f"/parent/imports/{draft_id}").text
            unsure, tickable, saved = still_open_tick_and_save(browser, draft_id, again)
        made = [
            a
            for a in state_of(browser).project_state.all_assignments()
            if a.title == "Sentence Composing"
        ]

    unreadable = screenshot_page.SAID["unreadable"].format(value="next Tuesday")
    still = f"Still to answer: {screenshot_page.OPEN_NAMES['assigned']}."
    checked = f'name="{escape("none:" + none)}" value="none" checked'
    assert says(reopened, unreadable)
    assert box_of(review_form(reopened), box)[1] == ""  # type: ignore[index]
    if press == "tick beside a date":
        assert answer.status_code == 422
        assert says(answer.text, screenshot_page.SAID["both"])
        assert made == []
        return
    assert answer.status_code == 200
    if press == "tick":
        assert held == {piece: {"assigned": None}}
        for shown in (after, later_page, again):
            assert checked in review_form(shown)
            assert not says(shown, unreadable)
            assert not says(shown, "next Tuesday")
        assert (unsure, tickable) == (False, True)
        assert says(saved, imports.counted(1, 0, 0, 0))
        assert [row.assigned_on for row in made] == [None]
    else:
        assert held == {piece: {"assigned": "next Tuesday"}}
        for shown in (after, later_page, again):
            assert checked not in review_form(shown)
            assert says(shown, unreadable)
            assert says(shown, still)
        assert tickable is False
        assert says(saved, REFUSED_OPEN)
        assert made == []


def test_a_kept_correction_for_an_instruction_box_the_row_lacks_answers_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saved progress holding words at an index past the row's texts, which no box offers: the
    unsure instructions stay to answer, as the save holds them, and the next press keeps
    nothing of it."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, boxes = unsure_review(browser, monkeypatch, "instruction")
        key = json.loads(boxes["instruction"].removeprefix("correct:"))[0]
        stale = {key: {"instruction:3": MISSING_WORDS}}
        saved_progress(browser, draft_id, {"v": 4, "corrected": stale, "selected": []})
        shown = browser.get(f"/parent/imports/{draft_id}").text
        refused = followed(
            browser, send(browser, draft_id, page_post(shown, "save", tick=digests_on(shown)))
        )
        later_page = later(browser, draft_id, refused)
        held = progress_after(browser, draft_id)

    unsure = screenshot_page.OPEN_NAMES["confirm"]
    assert says(shown, f"Still to answer: {unsure}.")
    assert says(refused, REFUSED_OPEN)
    assert says(later_page, f"Still to answer: {unsure}.")
    assert held == {}


OUTCOME: Final = re.compile(
    r'<section class="panel grade-outcome" aria-labelledby="outcome-heading">(.*?)</section>', re.S
)
TYPED_BACK: Final = re.compile(r"<textarea rows=\"2\" readonly>\n(.*?)</textarea>", re.S)


def busy_store(error: type[Exception]) -> Callable[..., object]:
    def refuse(*_: object, **__: object) -> object:
        raise error

    return refuse


@pytest.mark.parametrize("busy", [StoreBusy, WriterBusy], ids=["store busy", "writer busy"])
def test_a_resent_save_the_review_can_t_be_read_for_says_its_counts_and_gives_the_typing_back(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, busy: type[Exception]
) -> None:
    """One of two groups saved, a title typed for the other: the same press resent while the
    review can't be read answers with the counts it recorded in the success panel, which takes
    the focus, never that the review is finished, and gives the typed title back to copy. Sent
    again once the review can be read, it shows the review with those counts and the title
    still in its box."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        first_unit, open_unit = units_on(page)
        titles = [f"correct:{box_subject(page, 'title', n)}" for n in range(2)]
        [other] = [t for t in titles if json.loads(t.removeprefix("correct:"))[0] in open_unit]
        posted = page_post(page, "save", tick=[first_unit], typed={other: "Chains Guide Six"})
        first = followed(browser, send(browser, draft_id, posted))
        drafts = state_of(browser).import_drafts
        real = drafts.snapshot
        monkeypatch.setattr(drafts, "snapshot", busy_store(busy))
        again = send(browser, draft_id, posted)
        monkeypatch.setattr(drafts, "snapshot", real)
        healthy = send(browser, draft_id, posted)

    counts = ["Saved: 1 new assignment. 1 still needs checking."]
    shown = OUTCOME.search(again.text)
    assert lines_in(DONE.search(first))[0] == counts[0]
    assert again.status_code == 200
    assert shown is not None
    assert re.search(r'<h2 id="outcome-heading" tabindex="-1" autofocus>', shown.group(1))
    assert [html.unescape(t) for t in re.findall(r">([^<>]+)</(?:h2|p)>", shown.group(1))] == counts
    assert not says(again.text, imports.SAID["closed"])
    assert not says(again.text, imports.SAID["expired"])
    assert not says(again.text, imports.SAID["absent"])
    assert TYPED_BACK.findall(again.text) == ["Chains Guide Six"]
    assert healthy.status_code == 200
    assert lines_in(DONE.search(healthy.text)) == counts
    assert box_of(review_form(healthy.text), other)[1] == "Chains Guide Six"  # type: ignore[index]
    assert not says(healthy.text, imports.SAID["closed"])


def failing_read(
    real: Callable[..., object], at: int, error: type[Exception]
) -> Callable[..., object]:
    """The review's read as the store gives it, refusing with ``error`` at its ``at``-th call."""
    calls: list[int] = []

    def read(*args: object, **kwargs: object) -> object:
        calls.append(1)
        if len(calls) == at:
            raise error
        return real(*args, **kwargs)

    return read


@pytest.mark.parametrize(
    "busy",
    [StoreBusy, WriterBusy, DraftExpired, DraftNotFound],
    ids=["store busy", "writer busy", "expired", "removed"],
)
@pytest.mark.parametrize("at", [3, 4], ids=["read after the save", "read for the page"])
@pytest.mark.parametrize("path", ["save", "replay"])
def test_a_read_refused_after_a_save_committed_says_what_it_saved_and_gives_the_typing_back(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    busy: type[Exception],
    at: int,
    path: str,
) -> None:
    """One of two groups saved, a title typed for the other. The review's third or fourth read
    of the press refused, after the save committed (the save itself, or its resend): the press
    answers with what it saved in the success panel, which takes the focus, never that nothing
    was added nor that the review is finished, and gives the typed title back; a review found
    expired or gone says so there, a busy one says neither. Sent again once the review can be
    read, it shows the review with the counts and the title in its box, and nothing is written
    twice."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        first_unit, open_unit = units_on(page)
        titles = [f"correct:{box_subject(page, 'title', n)}" for n in range(2)]
        [other] = [t for t in titles if json.loads(t.removeprefix("correct:"))[0] in open_unit]
        posted = page_post(page, "save", tick=[first_unit], typed={other: "Chains Guide Six"})
        drafts = state_of(browser).import_drafts
        real = drafts.snapshot
        if path == "replay":
            assert send(browser, draft_id, posted).status_code == 200
        before = assignment_count(browser)
        monkeypatch.setattr(drafts, "snapshot", failing_read(real, at, busy))
        answer = send(browser, draft_id, posted)
        monkeypatch.setattr(drafts, "snapshot", real)
        refused = assignment_count(browser)
        again = send(browser, draft_id, posted)
        after = assignment_count(browser)

    counts = "Saved: 1 new assignment. 1 still needs checking."
    shown = OUTCOME.search(answer.text)
    assert answer.status_code == 200
    assert shown is not None
    assert re.search(
        rf'<h2 id="outcome-heading" tabindex="-1" autofocus>{re.escape(counts)}</h2>',
        shown.group(1),
    )
    assert not says(answer.text, imports.SAID["busy"])
    assert not says(answer.text, imports.SAID["closed"])
    endings = {DraftExpired: "expired", DraftNotFound: "absent"}
    for error, ending in endings.items():
        assert says(shown.group(1), imports.SAID[ending]) == (busy is error)
    assert TYPED_BACK.findall(answer.text) == ["Chains Guide Six"]
    assert refused == before + (1 if path == "save" else 0)
    assert again.status_code == 200
    assert lines_in(DONE.search(again.text))[0] == counts
    assert box_of(review_form(again.text), other)[1] == "Chains Guide Six"  # type: ignore[index]
    assert after == refused


def test_a_resent_save_that_finished_the_review_says_so_only_when_the_review_can_be_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save that finished the review, resent: read, it answers with the counts and that the
    review is finished; while the review can't be read, with the counts alone."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        page = her_name_answered(browser, draft_id, page)
        posted = page_post(page, "save", tick=units_on(page))
        first = send(browser, draft_id, posted)
        healthy = send(browser, draft_id, posted)
        drafts = state_of(browser).import_drafts
        real = drafts.snapshot
        monkeypatch.setattr(drafts, "snapshot", busy_store(StoreBusy))
        busy = send(browser, draft_id, posted)
        monkeypatch.setattr(drafts, "snapshot", real)

    for answer in (first, healthy, busy):
        assert answer.status_code == 200
        assert says(answer.text, "Saved: 1 new assignment.")
    assert says(first.text, imports.SAID["closed"])
    assert says(healthy.text, imports.SAID["closed"])
    assert not says(busy.text, imports.SAID["closed"])


def test_a_save_that_closed_the_review_says_so_when_her_week_can_t_be_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The save committed and closed the review, then reading the assignments for the way to
    her week fails: the press still answers with the saved outcome, with no way to her week, and
    a resend of it answers the same."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        record = state_of(browser).project_state
        real_save, real_read = screenshot_save.save_homework, record.all_assignments
        committed: list[object] = []

        def save_then_mark(*args: object, **kwargs: object) -> object:
            outcome = real_save(*args, **kwargs)  # type: ignore[arg-type]
            committed.append(outcome)
            return outcome

        def read_unless_saved() -> list[Assignment]:
            if committed:
                failed = "synthetic: the read failed"
                raise sqlite3.OperationalError(failed)
            return real_read()

        monkeypatch.setattr(imports, "save_homework", save_then_mark)
        monkeypatch.setattr(record, "all_assignments", read_unless_saved)
        posted = page_post(page, "save", tick=units_on(page))
        saved = send(browser, draft_id, posted)
        again = send(browser, draft_id, posted)
        monkeypatch.setattr(record, "all_assignments", real_read)
        made = [a for a in record.all_assignments() if a.course in ("English 8", "Science 8")]

    for answer in (saved, again):
        assert answer.status_code == 200
        assert says(answer.text, "Saved: 2 new assignments.")
        assert "See them on her week" not in answer.text
    assert len(made) == 2


# ------------------------------------------------------- a press whose review can't be read


BACK_TO_REVIEW: Final = '<a href="/parent/imports/{}">Back to the review</a>'
ALERT: Final = ['<p class="problem" role="alert" tabindex="-1" autofocus>']
KEPT_STATUS: Final = ['<div role="status" id="kept" tabindex="-1" autofocus>']
PROBLEM_BLOCK: Final = re.compile(
    r'<div class="problem" role="alert" id="said" tabindex="-1"(?: autofocus)?>(.*?)</div>', re.S
)
KEPT_BLOCK: Final = re.compile(r'<div role="status" id="kept"[^>]*>(.*?)</div>', re.S)


def restored(drafts: object, name: str, replacement: Callable[..., object]) -> Callable[[], None]:
    """Replace one store method, and the call that puts it back."""
    real = getattr(drafts, name)
    setattr(drafts, name, replacement)
    return lambda: setattr(drafts, name, real)


def name_and_title(page: str) -> tuple[str, str]:
    """The name question's field and the first title box's field, as a press posts them."""
    [form] = fields_named(review_form(page), "answer:name:")
    return f"answer:name:{form}", f"correct:{box_subject(page, 'title')}"


@pytest.mark.parametrize("busy", [StoreBusy, WriterBusy], ids=["store busy", "writer busy"])
def test_keep_these_answers_whose_review_can_t_be_read_says_they_are_kept_and_gives_the_typing_back(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, busy: type[Exception]
) -> None:
    """Keep these answers keeps her name answer, then the read for its page is refused: the
    press answers 200 with what it kept and that the review can't be shown, its first line
    taking the focus, the typed title to copy and the way back to the review. It never says
    nothing was added or changed, and nothing on it repeats the press."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        name, title = name_and_title(page)
        posted = page_post(page, "keep", choose={name: "hers"}, typed={title: "Chains Guide Six"})
        drafts = state_of(browser).import_drafts
        put_back = restored(drafts, "snapshot", failing_read(drafts.snapshot, 2, busy))
        answer = send(browser, draft_id, posted)
        put_back()
        stored = stored_answers(browser, draft_id)

    assert answer.status_code == 200
    assert says(answer.text, imports.PRESS_SAID["kept"])
    assert says(answer.text, imports.REVIEW_NOT_SHOWN)
    assert says(answer.text, imports.UNSHOWN_TYPED)
    assert TYPED_BACK.findall(answer.text) == ["Chains Guide Six"]
    assert BACK_TO_REVIEW.format(draft_id) in answer.text
    assert focused(answer.text) == KEPT_STATUS
    kept_block, problem = KEPT_BLOCK.search(answer.text), PROBLEM_BLOCK.search(answer.text)
    assert kept_block is not None
    assert problem is not None
    assert lines_in(kept_block) == [imports.PRESS_SAID["kept"]]
    assert '<p class="note">' in kept_block.group(1)
    assert lines_in(problem) == [imports.REVIEW_NOT_SHOWN]
    assert "steps-line" not in answer.text
    assert "3. Saved" not in answer.text
    assert not says(answer.text, imports.SAID["busy"])
    assert not says(answer.text, imports.AFTER_SAID["busy"])
    assert 'action="/parent/imports' not in answer.text
    assert stored[("name", name.removeprefix("answer:name:"))] == "hers"


def test_a_kept_answer_stays_when_its_page_can_t_be_read_and_reopening_the_review_writes_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a Keep whose page couldn't be read, the way back opens the review with her name
    answered, and Keep pressed there with the same answers writes nothing: the answers, the
    review's version and the assignments stay as the first press left them."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        name, _ = name_and_title(page)
        drafts = state_of(browser).import_drafts
        student = state_of(browser).project_state.student_id()
        put_back = restored(drafts, "snapshot", failing_read(drafts.snapshot, 2, StoreBusy))
        answer = send(browser, draft_id, page_post(page, "keep", choose={name: "hers"}))
        put_back()
        kept, version = (
            stored_answers(browser, draft_id),
            drafts.snapshot(student, draft_id).version,
        )
        count = assignment_count(browser)
        link = re.search(r'<a href="([^"]+)">Back to the review</a>', answer.text)
        assert link is not None
        reopened = browser.get(link.group(1))
        again = send(browser, draft_id, page_post(reopened.text, "keep"))
        after = stored_answers(browser, draft_id), drafts.snapshot(student, draft_id).version
        count_after = assignment_count(browser)

    assert answer.status_code == 200
    assert reopened.status_code == 200
    assert not says(reopened.text, "Still to answer: the student name.")
    assert again.status_code == 200
    assert after == (kept, version)
    assert count_after == count


KEPT_CASES: Final = {
    "later behind": (409, ["answers_kept", "later_behind"]),
    "nothing ticked": (200, ["answers_kept", "none_chosen"]),
    "not saved, busy": (503, ["answers_kept", "busy"]),
    "name check unreadable": (500, ["answers_kept", "no_name_check"]),
    "already saved": (200, ["answers_kept", "already"]),
    "changed by answers": (409, ["answers_kept", "changed_unshown_rest"]),
}


@pytest.mark.parametrize("case", list(KEPT_CASES))
def test_a_press_that_kept_answers_says_so_when_its_review_can_t_be_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A Save or Save review for later that kept her answers and then saved nothing, its page's
    read refused: it answers with its own status and its own lines, the kept answers first,
    then that the review can't be shown, never that nothing was added. A line that points at
    the review's rows says the review instead."""
    status, keys = KEPT_CASES[case]
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        drafts = state_of(browser).import_drafts
        student = state_of(browser).project_state.student_id()
        before = stored_answers(browser, draft_id)
        posted = page_post(page, "save", tick=units_on(page)[:1])
        if case == "later behind":
            revision = drafts.snapshot(student, draft_id).progress_revision
            drafts.save_progress(student, draft_id, revision=revision, progress="{}")
            posted = page_post(page, "later")
        elif case == "nothing ticked":
            posted = page_post(page, "save")
        elif case == "not saved, busy":
            monkeypatch.setattr(imports, "save_homework", lambda *_, **__: NotSaved("busy"))
        elif case == "already saved":
            monkeypatch.setattr(imports, "save_homework", lambda *_, **__: AlreadySaved(()))
        elif case == "changed by answers":
            monkeypatch.setattr(imports, "save_homework", lambda *_, **__: NotSaved("changed"))
        else:

            async def no_key(*_: object) -> bytes:
                raise OSError

            monkeypatch.setattr(imports, "grade_name_key", no_key)
        put_back = restored(drafts, "snapshot", failing_read(drafts.snapshot, 2, StoreBusy))
        answer = send(browser, draft_id, posted)
        put_back()
        after = stored_answers(browser, draft_id)

    lines = {
        **imports.PRESS_SAID,
        "no_name_check": imports.SAID["no_name_check"],
        "changed_unshown_rest": (
            "They change what saving does, so no homework was saved by this attempt. Check the "
            "effects in the review, then save."
        ),
    }
    assert answer.status_code == status
    for key in keys:
        assert says(answer.text, lines[key]), key
    assert says(answer.text, imports.REVIEW_NOT_SHOWN)
    assert BACK_TO_REVIEW.format(draft_id) in answer.text
    assert not says(answer.text, imports.SAID["busy"])
    assert not says(answer.text, imports.PRESS_SAID["changed_by_answers"])
    assert len(after) > len(before)
    kept_block = KEPT_BLOCK.search(answer.text)
    problem = PROBLEM_BLOCK.search(answer.text)
    assert problem is not None
    if "answers_kept" in keys:
        assert kept_block is not None
        assert lines_in(kept_block) == [imports.PRESS_SAID["answers_kept"]]
        assert focused(answer.text) == KEPT_STATUS
        assert kept_block.start() < problem.start()
    else:
        assert kept_block is None
    shown = lines_in(problem)
    assert shown == [
        *(lines[key] for key in keys if key != "answers_kept"),
        imports.REVIEW_NOT_SHOWN,
    ]


FAILED_WRITES: Final = {"keep": "keep_answers", "later": "save_for_later", "save": "keep_answers"}


@pytest.mark.parametrize("press", list(FAILED_WRITES))
def test_answers_that_couldn_t_be_kept_are_said_unkept_when_the_review_can_t_be_read_either(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, press: str
) -> None:
    """The press's own write refused busy, then its page's read refused too: 503 with the
    press's failure line, never that anything was kept, then that the review can't be shown,
    with the typed title to copy; nothing is stored."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        name, title = name_and_title(page)
        typed = {title: "Chains Guide Six"}
        posted = page_post(page, press, choose={name: "hers"}, typed=typed, tick=units_on(page))
        drafts = state_of(browser).import_drafts
        put_write = restored(drafts, FAILED_WRITES[press], busy_store(StoreBusy))
        put_read = restored(drafts, "snapshot", failing_read(drafts.snapshot, 2, StoreBusy))
        answer = send(browser, draft_id, posted)
        put_read()
        put_write()
        stored = stored_answers(browser, draft_id)

    assert answer.status_code == 503
    assert says(answer.text, imports.PRESS_SAID[f"{press}_failed"])
    assert says(answer.text, imports.REVIEW_NOT_SHOWN)
    assert not says(answer.text, imports.PRESS_SAID["answers_kept"])
    assert not says(answer.text, imports.SAID["busy"])
    assert TYPED_BACK.findall(answer.text) == ["Chains Guide Six"]
    assert KEPT_BLOCK.search(answer.text) is None
    assert focused(answer.text) == [
        '<div class="problem" role="alert" id="said" tabindex="-1" autofocus>'
    ]
    assert stored == {}


@pytest.mark.parametrize("press", list(FAILED_WRITES))
def test_answers_that_couldn_t_be_kept_come_back_in_the_review_when_it_can_be_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, press: str
) -> None:
    """The press's own write refused busy with the review readable: 503 with the failure line
    and the review, the typed title still in its box."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        _, title = name_and_title(page)
        posted = page_post(page, press, typed={title: "Chains Guide Six"}, tick=units_on(page))
        drafts = state_of(browser).import_drafts
        put_write = restored(drafts, FAILED_WRITES[press], busy_store(WriterBusy))
        answer = send(browser, draft_id, posted)
        put_write()

    assert answer.status_code == 503
    assert says(answer.text, imports.PRESS_SAID[f"{press}_failed"])
    assert 'value="Chains Guide Six"' in review_form(answer.text)


@pytest.mark.parametrize("refusal", ["out of date", "too long"])
def test_a_refused_press_says_its_own_refusal_when_its_review_can_t_be_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    """A Keep from a page the review moved on from, or with a title past its field, its page's
    read refused: its own status and refusal, then that the review can't be shown, and the
    typing given back whole."""
    title_text = "Chains Guide Six" if refusal == "out of date" else "T" * 2000
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        name, title = name_and_title(page)
        if refusal == "out of date":
            send(browser, draft_id, page_post(page, "keep", choose={name: "hers"}))
        posted = page_post(page, "keep", typed={title: title_text})
        drafts = state_of(browser).import_drafts
        put_back = restored(drafts, "snapshot", failing_read(drafts.snapshot, 2, StoreBusy))
        answer = send(browser, draft_id, posted)
        put_back()

    status, line = (
        (409, imports.OUT_OF_DATE)
        if refusal == "out of date"
        else (422, imports.PRESS_SAID["too_long"])
    )
    assert answer.status_code == status
    assert says(answer.text, line)
    assert says(answer.text, imports.REVIEW_NOT_SHOWN)
    assert TYPED_BACK.findall(answer.text) == [title_text]
    assert not says(answer.text, imports.SAID["busy"])


def test_the_review_s_address_says_it_can_t_be_shown_while_busy_and_keeps_the_press_s_note(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Save review for later commits and redirects; the review's address read while the store is
    busy answers 503 saying the review can't be shown, never that nothing was added or changed,
    and keeps the press's note, which the next readable visit shows."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        later = send(browser, draft_id, page_post(page, "later"))
        drafts = state_of(browser).import_drafts
        put_back = restored(drafts, "snapshot", busy_store(StoreBusy))
        landed = browser.get(later.headers["location"])
        put_back()
        healthy = browser.get(later.headers["location"])

    assert later.status_code == 303
    assert landed.status_code == 503
    assert says(landed.text, imports.REVIEW_UNAVAILABLE)
    assert not says(landed.text, imports.SAID["busy"])
    assert not says(landed.text, imports.AFTER_SAID["busy"])
    assert imports.NOTE_COOKIE not in landed.headers.get("set-cookie", "")
    assert says(healthy.text, imports.PRESS_SAID["later"])


@pytest.mark.parametrize("where", ["first upload", "more, before the body", "more, on storing"])
def test_an_upload_the_busy_file_refuses_still_says_nothing_was_added(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """An upload the busy store refuses began nothing, so its screenshots weren't added: it says
    so and asks for them again, 503, and the review holds what it held."""
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        drafts_store = state_of(browser).import_drafts
        method = {"first upload": "create", "more, before the body": "snapshot"}.get(
            where, "add_images"
        )
        put_back = restored(drafts_store, method, busy_store(WriterBusy))
        if where == "first upload":
            answer = upload(browser, BLUE)
        else:
            answer = upload(browser, BLUE, form={}, to=f"/parent/imports/{draft_id}/images")
        put_back()
        held = images_of(browser, draft_id)

    assert answer.status_code == 503
    assert says(answer.text, imports.SAID["busy"])
    assert len(held) == 1


def test_the_page_that_can_t_show_the_review_reads_no_store_after_the_refusal(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the read for a Keep's page is refused, the page that says so reads nothing more from
    either store."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        name, _ = name_and_title(page)
        drafts = state_of(browser).import_drafts
        record = state_of(browser).project_state
        refused: list[str] = []
        later_reads: list[str] = []

        def refuse_second(real: Callable[..., object]) -> Callable[..., object]:
            calls: list[int] = []

            def read(*args: object, **kwargs: object) -> object:
                calls.append(1)
                if refused:
                    later_reads.append("snapshot")
                if len(calls) == 2:
                    refused.append("snapshot")
                    raise StoreBusy
                return real(*args, **kwargs)

            return read

        def watched(label: str, real: Callable[..., object]) -> Callable[..., object]:
            def call(*args: object, **kwargs: object) -> object:
                if refused:
                    later_reads.append(label)
                return real(*args, **kwargs)

            return call

        put = [restored(drafts, "snapshot", refuse_second(drafts.snapshot))]
        for label in (
            "draft_for_save",
            "homework_counts",
            "homework_acceptance",
            "all_assignments",
        ):
            put.append(restored(record, label, watched(label, getattr(record, label))))
        for label in ("review_answers", "image_png", "open_drafts"):
            put.append(restored(drafts, label, watched(label, getattr(drafts, label))))
        answer = send(browser, draft_id, page_post(page, "keep", choose={name: "hers"}))
        for undo in put:
            undo()

    assert refused == ["snapshot"]
    assert says(answer.text, imports.REVIEW_NOT_SHOWN)
    assert later_reads == []


NOTHING_WRITTEN: Final = {
    "review": ("snapshot", "review"),
    "find": ("snapshot", "find"),
    "read": ("snapshot", "read"),
    "set aside": ("snapshot", "set-aside"),
    "finish": ("snapshot", "finish"),
    "discard": ("snapshot", "discard"),
    "not hers": ("snapshot", "not-hers"),
    "remove": ("remove_image", "images/{image}/remove"),
    "image": ("image_png", "images/{image}"),
}


@pytest.mark.parametrize("route", list(NOTHING_WRITTEN))
def test_a_press_that_wrote_nothing_while_the_file_was_busy_says_nothing_changed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """A press on a review, or its image, whose first store call the busy store refused began
    nothing: it says nothing changed and to try again, 503, never to send the screenshots again.
    A review press also gives its typing back with the way to the review."""
    method, path = NOTHING_WRITTEN[route]
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        _, title = name_and_title(page)
        [image] = images_of(browser, draft_id)
        drafts = state_of(browser).import_drafts
        before = stored_answers(browser, draft_id), images_of(browser, draft_id)
        address = f"/parent/imports/{draft_id}/{path.format(image=image)}"
        put_back = restored(drafts, method, busy_store(StoreBusy))
        if route == "review":
            answer = send(browser, draft_id, page_post(page, "keep", typed={title: "Craft"}))
        elif route == "image":
            answer = browser.get(address)
        else:
            answer = browser.post(address, data={"version": "1"})
        put_back()
        after = stored_answers(browser, draft_id), images_of(browser, draft_id)

    assert answer.status_code == 503
    assert says(answer.text, imports.AFTER_SAID["busy"])
    assert not says(answer.text, imports.SAID["busy"])
    assert after == before
    if route == "review":
        assert TYPED_BACK.findall(answer.text) == ["Craft"]
        assert BACK_TO_REVIEW.format(draft_id) in answer.text


def test_a_read_whose_readings_were_kept_goes_to_the_review_when_it_can_t_be_shown(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Read press stores its reading, then the read for its page is refused: it answers with
    the way to the review, built from what it has without reading again, and the review shows
    the reading once it can be read. Neither the answer nor the visit reads the screenshot
    again or runs another Read."""
    with here(open_settings(tmp_path)) as browser:
        reader = Reader(monkeypatch)
        reader.answers[RED] = ScreenshotAnswer(homework_answer(), None, 10, 5)
        draft_id = made(browser, RED)
        page = browser.get(f"/parent/imports/{draft_id}").text
        drafts = state_of(browser).import_drafts
        real_snapshot = drafts.snapshot
        real_read, real_changes = screenshot_reading.read_screenshots, imports.change_lines
        runs: list[int] = []
        changes: list[int] = []

        async def read_then_busy(*args: object, **kwargs: object) -> object:
            runs.append(1)
            outcome = await real_read(*args, **kwargs)  # type: ignore[arg-type]
            monkeypatch.setattr(drafts, "snapshot", busy_store(StoreBusy))
            return outcome

        async def counted_changes(*args: object, **kwargs: object) -> object:
            changes.append(1)
            return await real_changes(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(imports, "read_screenshots", read_then_busy)
        monkeypatch.setattr(imports, "change_lines", counted_changes)
        answer = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page))
        monkeypatch.setattr(drafts, "snapshot", real_snapshot)
        shown = browser.get(f"/parent/imports/{draft_id}")
        student = state_of(browser).project_state.student_id()
        read_now = [image.read for image in drafts.snapshot(student, draft_id).images]

    assert answer.status_code == 303
    assert answer.headers["location"] == f"/parent/imports/{draft_id}"
    assert changes == []
    assert runs == [1]
    assert reader.calls == [RED]
    assert read_now == [True]
    assert shown.status_code == 200
    assert says(shown.text, "Screenshot 1 · Read.")
    assert "<h3>Sentence Composing</h3>" in review_form(shown.text)


def test_a_reading_the_busy_file_couldn_t_keep_is_left_to_its_label_when_a_read_goes_back(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Read whose reading the busy file couldn't keep, then the read for its page refused:
    the way back to the review carries no line naming a screenshot by number, and the review's
    own label says it needs reading once the review can be read. The screenshot was read once,
    by one Read."""
    with here(open_settings(tmp_path)) as browser:
        reader = Reader(monkeypatch)
        draft_id = made(browser, RED)
        form = read_form(browser.get(f"/parent/imports/{draft_id}").text)
        drafts = state_of(browser).import_drafts
        real_snapshot, real_read = drafts.snapshot, screenshot_reading.read_screenshots

        runs: list[int] = []

        async def read_then_busy(*args: object, **kwargs: object) -> object:
            runs.append(1)
            outcome = await real_read(*args, **kwargs)  # type: ignore[arg-type]
            monkeypatch.setattr(drafts, "snapshot", busy_store(StoreBusy))
            return outcome

        monkeypatch.setattr(drafts, "store_reading", busy_store(WriterBusy))
        monkeypatch.setattr(imports, "read_screenshots", read_then_busy)
        answer = browser.post(f"/parent/imports/{draft_id}/read", data=form)
        monkeypatch.setattr(drafts, "snapshot", real_snapshot)
        shown = browser.get(answer.headers["location"])

    assert answer.status_code == 303
    assert imports.NOTE_COOKIE not in answer.headers.get("set-cookie", "")
    assert not says(shown.text, imports.busy_reading(1))
    assert "couldn&#39;t keep the reading" not in shown.text
    assert says(shown.text, imports.LABELS["ended"])
    assert reader.calls == [RED]
    assert runs == [1]


def test_a_read_whose_review_closed_meanwhile_says_it_is_finished(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Read whose review another press discarded while it read answers that the review is
    finished, not with the review as if it were open."""
    with here(open_settings(tmp_path)) as browser:
        Reader(monkeypatch)
        draft_id = made(browser, RED)
        form = read_form(browser.get(f"/parent/imports/{draft_id}").text)
        drafts = state_of(browser).import_drafts
        student = state_of(browser).project_state.student_id()
        real_read = screenshot_reading.read_screenshots

        async def read_then_discarded(*args: object, **kwargs: object) -> object:
            outcome = await real_read(*args, **kwargs)  # type: ignore[arg-type]
            version = drafts.snapshot(student, draft_id).version
            drafts.discard_review(student, draft_id, version=version, refuse_if_saved=False)
            return outcome

        monkeypatch.setattr(imports, "read_screenshots", read_then_discarded)
        answer = browser.post(f"/parent/imports/{draft_id}/read", data=form)

    assert answer.status_code == 200
    assert says(answer.text, imports.SAID["closed"])
    assert f'action="/parent/imports/{draft_id}/' not in answer.text


def test_a_save_that_would_now_do_something_else_says_to_check_it_in_the_review(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Save that changed no answer and found a ticked assignment would now do something else,
    its page's read refused: 409, the line that names the review instead of rows the page
    doesn't show, then that the review can't be shown."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        send(browser, draft_id, page_post(page, "keep"))
        page = browser.get(f"/parent/imports/{draft_id}").text
        before = stored_answers(browser, draft_id)
        monkeypatch.setattr(imports, "save_homework", lambda *_, **__: NotSaved("changed"))
        drafts = state_of(browser).import_drafts
        put_back = restored(drafts, "snapshot", failing_read(drafts.snapshot, 2, StoreBusy))
        answer = send(browser, draft_id, page_post(page, "save", tick=units_on(page)[:1]))
        put_back()
        after = stored_answers(browser, draft_id)

    assert answer.status_code == 409
    assert says(answer.text, imports.UNSHOWN_SAID[imports.PRESS_SAID["changed"]][0])
    assert not says(answer.text, imports.PRESS_SAID["changed"])
    assert not says(answer.text, imports.PRESS_SAID["answers_kept"])
    assert says(answer.text, imports.REVIEW_NOT_SHOWN)
    assert after == before


G6B: Final = (
    "Your answers were kept before this review ended. No homework was saved by this attempt."
)
ENDED_LINES: Final = {
    "busy, ended": (
        "The homework wasn't saved because the household file was busy; nothing of it was written."
    ),
    "G6b": G6B,
    "counts": "Saved: 1 new assignment. 1 still needs checking.",
}
ENDED_CASES: Final = {
    "keep": (200, 2, ["kept_before_ended"], []),
    "later saved": (200, 2, ["kept_before_ended"], []),
    "later behind": (409, 2, ["kept_before_ended", "later_behind"], []),
    "nothing ticked": (200, 2, ["kept_before_ended"], ["none_chosen"]),
    "name check unreadable": (500, 2, ["kept_before_ended", "no_name_check"], []),
    "not saved, busy": (503, 2, ["kept_before_ended", "busy, ended"], ["busy"]),
    "changed by answers": (409, 2, ["G6b"], ["changed_by_answers"]),
    "already saved": (200, 2, ["kept_before_ended", "already"], []),
    "partly saved": (200, 3, ["counts", "kept_before_ended"], []),
}


@pytest.mark.parametrize("ending", [DraftExpired, DraftNotFound], ids=["expired", "removed"])
@pytest.mark.parametrize("case", list(ENDED_CASES))
def test_a_press_that_kept_answers_says_they_were_kept_before_the_review_ended(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, case: str, ending: type[Exception]
) -> None:
    """A press that kept her answers and then found the review expired or removed says they
    were kept before it ended, never that they are kept, saved for later or left open, then why
    the review is gone, at the press's own status. The page keeps its styling: the first line
    takes the focus in the problem style, or the save's outcome does."""
    status, at, keys, dropped = ENDED_CASES[case]
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        drafts = state_of(browser).import_drafts
        student = state_of(browser).project_state.student_id()
        posted = page_post(page, "save", tick=units_on(page)[:1])
        if case == "keep":
            posted = page_post(page, "keep")
        elif case in ("later saved", "later behind"):
            if case == "later behind":
                revision = drafts.snapshot(student, draft_id).progress_revision
                drafts.save_progress(student, draft_id, revision=revision, progress="{}")
            posted = page_post(page, "later")
        elif case == "nothing ticked":
            posted = page_post(page, "save")
        elif case == "not saved, busy":
            monkeypatch.setattr(imports, "save_homework", lambda *_, **__: NotSaved("busy"))
        elif case == "changed by answers":
            monkeypatch.setattr(imports, "save_homework", lambda *_, **__: NotSaved("changed"))
        elif case == "already saved":
            monkeypatch.setattr(imports, "save_homework", lambda *_, **__: AlreadySaved(()))
        elif case == "name check unreadable":

            async def no_key(*_: object) -> bytes:
                raise OSError

            monkeypatch.setattr(imports, "grade_name_key", no_key)
        put_back = restored(drafts, "snapshot", failing_read(drafts.snapshot, at, ending))
        answer = send(browser, draft_id, posted)
        put_back()

    gone = imports.SAID["expired" if ending is DraftExpired else "absent"]
    lines = {**imports.PRESS_SAID, "no_name_check": imports.SAID["no_name_check"], **ENDED_LINES}
    assert answer.status_code == status
    for key in keys:
        assert says(answer.text, lines[key]), key
    for key in dropped:
        assert not says(answer.text, lines[key]), key
    for instruction in ("Save again.", "Try again.", "Choose at least one assignment to save."):
        assert instruction not in answer.text
    assert says(answer.text, gone)
    assert "Your answers are kept" not in answer.text
    assert not says(answer.text, imports.PRESS_SAID["left_open"])
    assert "Saved for later at " not in answer.text
    assert "Back to the review" not in answer.text
    if case == "partly saved":
        shown = OUTCOME.search(answer.text)
        assert shown is not None
        heading = re.search(r'<h2 id="outcome-heading"[^>]*>([^<]*)</h2>', shown.group(1))
        assert heading is not None
        assert html.unescape(heading.group(1)) == lines["counts"]
        assert lines_in(shown)[0] == lines["kept_before_ended"]
    else:
        assert focused(answer.text) == ALERT
        first = re.search(
            r'<p class="problem" role="alert" tabindex="-1" autofocus>([^<]*)</p>', answer.text
        )
        assert first is not None
        assert html.unescape(first.group(1)) == lines[keys[0]]
        assert answer.text.index(first.group(0)) < answer.text.index(str(escape(gone)))


def test_an_ended_review_s_page_says_each_press_line_as_listed() -> None:
    """Every line an ended review's page rewords or leaves out, as it says it; the changed line
    says no homework was saved only where no homework transaction committed."""
    said = imports.PRESS_SAID
    kept = "Your answers were kept before this review ended."
    expected = {
        said["kept"]: [kept],
        said["answers_kept"]: [kept],
        said["later"]: [kept],
        said["left_open"]: [kept],
        said["changed_by_answers"]: [G6B],
        said["none_chosen"]: [],
        said["busy"]: [ENDED_LINES["busy, ended"]],
        said["failed"]: ["The homework wasn't saved; nothing of it was written."],
        said["keep_failed"]: ["Your answers couldn't be kept."],
        said["save_failed"]: ["Your answers couldn't be kept, and nothing was saved."],
        said["later_failed"]: ["Your answers couldn't be kept, and nothing was saved for later."],
        said["too_long"]: [
            "A correction is longer than its field takes, so nothing was kept or saved."
        ],
        imports.OUT_OF_DATE: ["This page is out of date."],
        screenshot_page.SAID["both"]: [],
        said["already"]: [said["already"]],
        said["later_behind"]: [said["later_behind"]],
        imports.SAID["no_name_check"]: [imports.SAID["no_name_check"]],
    }
    for line, ended in expected.items():
        assert imports.ended_lines([line], saved=False) == ended, line
    assert imports.ended_lines([said["changed"]], saved=False) == [imports.NO_HOMEWORK]
    assert imports.ended_lines([said["changed"]], saved=True) == []


@pytest.mark.parametrize("ending", [DraftExpired, DraftNotFound], ids=["expired", "removed"])
@pytest.mark.parametrize("path", ["review left open", "close unconfirmed"])
def test_a_committed_save_with_a_changed_group_never_says_no_homework_when_its_review_ends(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, path: str, ending: type[Exception]
) -> None:
    """A save that committed homework, one ticked group of it changed, then its review found
    ended at the next read: its counts stand and it never says no homework was saved by this
    attempt, nor the changed line that needs the review, whether the review was left open or
    its close couldn't be confirmed."""
    saved = Saved(
        "a" * 32,
        (),
        ("first",),
        changed_units=("second",),
        incomplete=() if path == "review left open" else ("close",),
        counts=(1, 0, 0, 1),
    )
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        monkeypatch.setattr(imports, "save_homework", lambda *_, **__: saved)
        drafts = state_of(browser).import_drafts
        put_back = restored(drafts, "snapshot", failing_read(drafts.snapshot, 2, ending))
        answer = send(browser, draft_id, page_post(page, "save", tick=units_on(page)))
        put_back()

    gone = imports.SAID["expired" if ending is DraftExpired else "absent"]
    assert answer.status_code == 200
    assert says(answer.text, "Saved: 1 new assignment. 1 still needs checking.")
    assert not says(answer.text, imports.NO_HOMEWORK)
    assert not says(answer.text, imports.PRESS_SAID["changed"])
    assert says(answer.text, gone)
    if path == "close unconfirmed":
        assert says(answer.text, imports.PRESS_SAID["incomplete"])


INSTRUCTION_CASES: Final = {
    "keep refused busy": ("keep", "keep_answers", None, "keep_failed", "Try again."),
    "later refused busy": ("later", "save_for_later", None, "later_failed", "Try again."),
    "save refused busy": ("save", "keep_answers", None, "save_failed", "Try again."),
    "save not saved, failed": ("save", None, NotSaved("failed"), "failed", "Save again."),
    "save nothing ticked": ("save", None, None, "none_chosen", None),
    "keep out of date": (
        "keep",
        None,
        None,
        "out_of_date",
        "Check the review before trying again.",
    ),
    "keep too long": ("keep", None, None, "too_long", "Shorten it and press again."),
    "save changed, no answer": ("save", None, NotSaved("changed"), "changed", None),
}


@pytest.mark.parametrize("case", list(INSTRUCTION_CASES))
def test_an_ended_review_s_page_drops_instructions_that_need_the_review(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """The same press on a review that stays readable says its whole line, instruction
    included; on a review found ended at the page's read, the instruction is left out and the
    fact kept, a lone instruction is left out, and the changed line of a homework transaction
    that didn't commit says no homework was saved by this attempt."""
    press, refused_write, outcome, key, instruction = INSTRUCTION_CASES[case]
    full = {**imports.PRESS_SAID, "out_of_date": imports.OUT_OF_DATE}[key]
    answers: dict[str, Answer] = {}
    for mode in ("live", "ended"):
        with here(open_settings(tmp_path / mode)) as browser:
            draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
            page = her_name_answered(browser, draft_id, page)
            if case == "save changed, no answer":
                send(browser, draft_id, page_post(page, "keep"))
                page = browser.get(f"/parent/imports/{draft_id}").text
            drafts = state_of(browser).import_drafts
            _, title = name_and_title(page)
            typed = {title: "T" * 2000} if case == "keep too long" else {}
            tick = [] if case == "save nothing ticked" else units_on(page)[:1]
            posted = page_post(page, press, typed=typed, tick=tick)
            if case == "keep out of date":
                send(browser, draft_id, page_post(page, "keep"))
            puts = []
            if refused_write is not None:
                puts.append(restored(drafts, refused_write, busy_store(StoreBusy)))
            if outcome is not None:
                monkeypatch.setattr(imports, "save_homework", lambda *_, o=outcome, **__: o)
            if mode == "ended":
                puts.append(
                    restored(drafts, "snapshot", failing_read(drafts.snapshot, 2, DraftExpired))
                )
            answers[mode] = send(browser, draft_id, posted)
            for undo in reversed(puts):
                undo()

    live, ended = answers["live"].text, answers["ended"].text
    assert answers["live"].status_code == answers["ended"].status_code
    assert says(live, full)
    assert not says(ended, full)
    assert says(ended, imports.SAID["expired"])
    if instruction is not None:
        assert full.endswith(" " + instruction)
        assert says(ended, full.removesuffix(" " + instruction))
        assert instruction not in ended
    if case == "save changed, no answer":
        assert says(ended, imports.NO_HOMEWORK)
    elif case == "save nothing ticked":
        assert not says(ended, imports.NO_HOMEWORK)


@pytest.mark.parametrize("answered", [True, False], ids=["answers kept", "no answer changed"])
def test_a_save_already_saved_says_the_answers_it_kept(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, answered: bool
) -> None:
    """A Save whose groups another press already saved says so, after its own kept answers
    when it kept any."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        if not answered:
            send(browser, draft_id, page_post(page, "keep"))
            page = browser.get(f"/parent/imports/{draft_id}").text
        monkeypatch.setattr(imports, "save_homework", lambda *_, **__: AlreadySaved(()))
        answer = send(browser, draft_id, page_post(page, "save", tick=units_on(page)[:1]))

    assert answer.status_code == 200
    assert says(answer.text, imports.PRESS_SAID["already"])
    assert says(answer.text, imports.PRESS_SAID["answers_kept"]) == answered


@pytest.mark.parametrize("ending", ["expired", "absent"])
@pytest.mark.parametrize("answered", [True, False], ids=["answers kept", "no answer changed"])
def test_a_save_whose_review_ended_after_its_answers_were_kept_says_what_was_kept(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ending: str, answered: bool
) -> None:
    """A Save that kept answers and then found the review ended answers 409: that the answers
    were kept but the review ended before the homework was saved, then why the review is gone,
    and the typing to copy; with no answer changed (nothing typed, the answers as kept), only
    why it's gone."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        if not answered:
            send(browser, draft_id, page_post(page, "keep"))
            page = browser.get(f"/parent/imports/{draft_id}").text
        _, title = name_and_title(page)
        monkeypatch.setattr(imports, "save_homework", lambda *_, **__: NotSaved(ending))
        typed = {title: "Chains"} if answered else {}
        posted = page_post(page, "save", tick=units_on(page)[:1], typed=typed)
        answer = send(browser, draft_id, posted)

    assert answer.status_code == 409
    assert says(answer.text, imports.PRESS_SAID["kept_then_ended"]) == answered
    assert not says(answer.text, imports.PRESS_SAID["answers_kept"])
    assert says(answer.text, imports.SAID[ending])
    assert TYPED_BACK.findall(answer.text) == (["Chains"] if answered else [])


def test_an_unsure_field_corrected_in_the_fold_isn_t_the_first_question(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A title the reader wasn't sure of, corrected under Correct what was read: the save takes
    it as answered and "Still to answer" leaves it out, so the first-question link doesn't
    point at its question either."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, unsure_answer()))
        page = her_name_answered(browser, draft_id, page)
        asked = FIRST_OPEN.search(page)
        box = f"correct:{box_subject(page, 'title')}"
        kept = followed(
            browser, send(browser, draft_id, page_post(page, "keep", typed={box: "Sentence Craft"}))
        )

    [question] = re.findall(r"<fieldset[^>]*id=\"(ask-\d+)\"[^>]*>\s*<legend>The reader wasn", page)
    assert asked is not None
    assert asked.group(1) == question
    assert not says(kept, screenshot_page.OPEN_NAMES["confirm"])
    link = FIRST_OPEN.search(kept)
    assert link is None or link.group(1) != question


def test_each_field_has_one_box_on_the_page(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every box's name appears once, a row's class, title and instructions on its first open
    card alone, each piece's dates on its own card; an unsure field's question has no box of
    its own."""
    monday, tuesday = split_answers()
    unsure = {"uncertain": [{"field": f, "reason": "faint"} for f in ("title", "footer")]}
    for answer in (monday, tuesday):
        answer["cards"][0].update(unsure)  # type: ignore[index]
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, tuesday))
        [row] = fields_named(review_form(page), "answer:split:")
        keep(browser, draft_id, ("split", row, json.dumps("split")))
        split = browser.get(f"/parent/imports/{draft_id}").text

    form = review_form(split)
    names = re.findall(r'name="(correct:[^"]+)"', form)
    fields = sorted(json.loads(html.unescape(n).removeprefix("correct:"))[1] for n in names)
    assert len(names) == len(set(names))
    assert fields == sorted(
        ["course", "title", "instruction:0", "due", "assigned", "due", "assigned"]
    )
    for question in re.findall(r"<legend>The reader wasn't sure.*?</fieldset>", form, re.S):
        assert 'name="correct:' not in question


def test_an_untouched_box_doesn_t_answer_an_unsure_field(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A title the reader wasn't sure of stays to answer while its box holds the title as read,
    even typed again; a title that differs answers it."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, unsure_answer()))
        page = her_name_answered(browser, draft_id, page)
        box = f"correct:{box_subject(page, 'title')}"
        untouched = followed(browser, send(browser, draft_id, page_post(page, "keep")))
        retyped = followed(
            browser,
            send(
                browser, draft_id, page_post(untouched, "keep", typed={box: "Sentence Composing"})
            ),
        )
        changed = followed(
            browser,
            send(browser, draft_id, page_post(retyped, "keep", typed={box: "Sentence Craft"})),
        )

    unsure = screenshot_page.OPEN_NAMES["confirm"]
    assert says(untouched, f"Still to answer: {unsure}.")
    assert says(retyped, f"Still to answer: {unsure}.")
    assert not says(changed, unsure)


def test_an_edit_asks_again_only_where_it_goes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row's answers kept with a correction typed but not saved for later: opened again,
    where its card goes is asked again, in those words, and its Type, its confirmation and
    the student name are kept."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, unsure_answer()))
        page = her_name_answered(browser, draft_id, page)
        [confirm] = fields_named(review_form(page), "answer:confirm:")
        key = json.loads(confirm)[0]
        box = f"correct:{box_subject(page, 'course')}"
        choose = {f"answer:kind:{key}": "TASK", f"answer:confirm:{confirm}": "as_read"}
        followed(
            browser,
            send(
                browser, draft_id, page_post(page, "keep", choose=choose, typed={box: "English 9"})
            ),
        )
        reopened = browser.get(f"/parent/imports/{draft_id}").text
        answers = stored_answers(browser, draft_id)

    checked = {html.unescape(n): html.unescape(v) for n, v in checked_values(review_form(reopened))}
    assert says(reopened, screenshot_page.SAID["lost_correction"])
    assert f"answer:destination:{key}" not in checked
    assert checked[f"answer:kind:{key}"] == "TASK"
    assert checked[f"answer:confirm:{confirm}"] == "as_read"
    assert [kind for kind, _ in answers if kind == "name"] == ["name"]


def test_a_piece_s_own_date_edit_leaves_another_piece_s_destination(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two pieces of one split row, each kept on its destination: a correction of one piece's
    assigned date typed but not saved asks again where that piece goes, and only it."""
    monday, tuesday = split_answers()
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, monday), (BLUE, tuesday))
        [row] = fields_named(review_form(page), "answer:split:")
        keep(browser, draft_id, ("split", row, json.dumps("split")))
        split = browser.get(f"/parent/imports/{draft_id}").text
        first, second = fields_named(review_form(split), "answer:destination:")
        choose = {
            f"answer:destination:{first}": '{"new":1}',
            f"answer:destination:{second}": '{"new":2}',
        }
        box = json.dumps([f"date:{first}", "assigned"], separators=(",", ":"))
        typed = {f"correct:{box}": "2026-10-02"}
        followed(
            browser, send(browser, draft_id, page_post(split, "keep", choose=choose, typed=typed))
        )
        reopened = browser.get(f"/parent/imports/{draft_id}").text

    by_piece = {
        html.unescape(found.group(1)): card
        for card in destination_cards(reopened)
        if (found := re.search(r'name="answer:destination:([^"]+)"', card)) is not None
    }
    assert says(by_piece[first], screenshot_page.SAID["lost_correction"])
    assert " checked" not in by_piece[first]
    assert not says(by_piece[second], screenshot_page.SAID["lost_correction"])
    assert " checked" in by_piece[second]


def test_a_save_with_a_new_instruction_correction_doesn_t_save_a_stale_choice(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which instructions apply, chosen on a page, and a correction of one of those texts typed
    in the same press as Save: the choice names a text the corrected card doesn't have, and
    what saving does differs from what the page showed, so nothing is saved, and the question
    is asked of the corrected text with nothing chosen."""
    first, second = "Read pages 1 to 3.", "Read pages 4 to 6."
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, subject = two_names_with_instructions(browser, monkeypatch, first, second)
        choose = {f"answer:instructions:{subject}": option_for(page, subject, first)}
        box = f"correct:{box_subject(page, 'instruction:0')}"
        assert box_of(review_form(page), box)[1] == first  # type: ignore[index]
        record = state_of(browser).project_state
        before = record.school_instruction_readings(["assignment-grammar-practice-eeee4444"])
        sent = send(
            browser,
            draft_id,
            page_post(
                page,
                "save",
                choose=choose,
                typed={box: "Read pages 1 to 3, slowly."},
                tick=units_on(page) or digests_on(page),
            ),
        )
        after = record.school_instruction_readings(["assignment-grammar-practice-eeee4444"])
        again = followed(browser, sent)

    assert sent.status_code == 409
    assert says(sent.text, imports.PRESS_SAID["changed_by_answers"])
    assert before == after
    assert says(again, "“Read pages 1 to 3, slowly.”")
    options = re.findall(
        rf'name="{re.escape(str(escape(f"answer:instructions:{subject}")))}" value="[^"]*"'
        r"( checked)?> <span>“Read pages 1 to 3, slowly",
        review_form(again),
    )
    assert options in ([""], [])


def test_an_empty_due_box_while_which_due_date_waits_is_accepted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While "which due date" waits for a card, its Due box is empty with what to do said
    beneath it; posted empty it is no correction, so the press is accepted and its answers
    kept."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, disagreeing_due_card()))
        box = f"correct:{box_subject(page, 'due')}"
        kept = send(browser, draft_id, page_post(page, "keep"))
        answers = stored_answers(browser, draft_id)

    assert box_of(review_form(page), box)[1] == ""  # type: ignore[index]
    assert says(page, screenshot_page.SAID["due_hint"])
    assert kept.status_code == 200
    assert says(kept.text, imports.PRESS_SAID["kept"])
    assert [kind for kind, _ in answers] == ["destination"]


# ------------------------------------------------------------------ "No assigned date was given"


def none_given(page: str) -> str:
    """The field of the first card's "No assigned date was given"."""
    [subject] = fields_named(review_form(page), "none:")[:1]
    return f"none:{subject}"


def test_no_assigned_date_survives_later_reopening_and_save(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checked over an assigned date as read: kept as none by Save review for later, checked and
    empty when the review is opened again, posted again untouched, and saved with no assigned
    date, its due date as read."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        page = her_name_answered(browser, draft_id, page)
        none = none_given(page)
        box = f"correct:{box_subject(page, 'assigned')}"
        sent = send(browser, draft_id, page_post(page, "later", choose={none: "none"}))
        held = progress_after(browser, draft_id)
        reopened = browser.get(f"/parent/imports/{draft_id}").text
        saved = followed(
            browser, send(browser, draft_id, page_post(reopened, "save", tick=units_on(reopened)))
        )
        [row] = [
            a
            for a in state_of(browser).project_state.all_assignments()
            if a.title == "Sentence Composing"
        ]

    key = json.loads(box.removeprefix("correct:"))[0]
    assert sent.status_code == 303
    assert held == {key: {"assigned": None}}
    assert (html.unescape(none), "none") in [
        (html.unescape(n), v) for n, v in checked_values(review_form(reopened))
    ]
    assert box_of(review_form(reopened), box)[1] == ""  # type: ignore[index]
    assert says(reopened, screenshot_page.SAID["no_assigned_line"])
    assert says(saved, "Saved: 1 new assignment.")
    assert (row.assigned_on, row.due_date) == (None, date(2026, 10, 8))


def test_no_assigned_date_where_none_was_read_writes_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = homework_card(badge="DUE", footer=None)
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer(cards=[card])))
        assert box_of(review_form(page), f"correct:{box_subject(page, 'assigned')}")[1] == ""  # type: ignore[index]
        sent = send(browser, draft_id, page_post(page, "later", choose={none_given(page): "none"}))
        held = progress_after(browser, draft_id)

    assert sent.status_code == 303
    assert held == {}


def test_no_assigned_date_with_a_date_typed_is_refused_with_the_typing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        none = none_given(page)
        box = f"correct:{box_subject(page, 'assigned')}"
        held = stored_answers(browser, draft_id)
        refused = send(
            browser,
            draft_id,
            page_post(page, "later", choose={none: "none"}, typed={box: "2026-10-02"}),
        )
        answers = stored_answers(browser, draft_id)
        progress = progress_on(browser, draft_id)

    both = screenshot_page.SAID["both"]
    assert refused.status_code == 422
    assert says(refused.text, both)
    assert says(refused.text, imports.PRESS_SAID["nothing_kept"])
    form = review_form(refused.text)
    assert box_of(form, box)[1] == "2026-10-02"  # type: ignore[index]
    assert (html.unescape(none), "none") in [(html.unescape(n), v) for n, v in checked_values(form)]
    assert answers == held
    assert progress is None


def test_each_assigned_date_preview_line(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview states the assigned date Save leaves: the date read, none on a new
    assignment where none was given, and none on a saved assignment that has none."""
    saved = Assignment(
        assignment_id="assignment-sentence-composing-n0n0",
        course="English 8",
        title="Sentence Composing",
        due_date=date(2026, 10, 8),
        dependencies=[],
        reported_submission_status="not_reported",
        assigned_on=None,
        kind=AssignmentKind.HOMEWORK,
    )
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card()]))
        )
        [piece] = fields_named(review_form(page), "answer:destination:")
        none = none_given(page)
        new_none = followed(
            browser, send(browser, draft_id, page_post(page, "keep", choose={none: "none"}))
        )
        state_of(browser).project_state.upsert_assignments([saved])
        existing = {
            f"answer:destination:{piece}": '{"existing":"assignment-sentence-composing-n0n0"}'
        }
        reread = browser.get(f"/parent/imports/{draft_id}").text
        on_saved = followed(
            browser,
            send(browser, draft_id, page_post(reread, "keep", choose={none: "none", **existing})),
        )

    assert says(page, "Assigned Monday, October 5.")
    assert says(new_none, screenshot_page.SAID["no_assigned_line"])
    assert not says(new_none, "Assigned Monday, October 5.")
    assert says(on_saved, screenshot_page.SAID["no_assigned_line"])
    assert says(on_saved, screenshot_page.SAID["updates"].format(title="Sentence Composing"))


# ------------------------------------------------------- chosen, uploaded, read and saved, counted


TOLD: Final = re.compile(r'<div role="status" id="told"[^>]*>(.*?)</div>', re.S)
DONE: Final = re.compile(
    r'<div class="panel grade-outcome" role="status" id="done"[^>]*>(.*?)</div>', re.S
)
PROBLEM: Final = re.compile(r'<div class="problem" role="status" id="said"[^>]*>(.*?)</div>', re.S)


def lines_in(found: re.Match[str] | None) -> list[str]:
    return (
        []
        if found is None
        else [html.unescape(t) for t in re.findall(r"<p[^>]*>([^<]*)</p>", found.group(1))]
    )


def test_the_pane_and_the_list_say_chosen_uploaded_and_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The picker's submit says what it does, a line beneath the picker waits for the script to
    say how many are chosen, the first upload says what it uploaded, and the list says how many
    are uploaded and read, each screenshot on one compact row, the Read press naming how many
    it reads and what it says while it waits."""
    with here(open_settings(tmp_path)) as browser:
        family = browser.get("/parent").text
        sent = upload(browser, RED, BLUE)
        page = browser.get(sent.headers["location"]).text
        again = browser.get(sent.headers["location"]).text

    assert "Upload chosen screenshots</button>" in family
    assert '<p class="note" role="status" data-chosen></p>' in family
    assert lines_in(TOLD.search(page)) == ["2 screenshots uploaded. None read yet."]
    assert focused(page) == [TOLD_LANDS]
    assert TOLD.search(again) is None
    assert says(page, "2 uploaded · 0 read")
    assert says(page, "Screenshot 1 · Not read yet.")
    assert ">Read 2 screenshots</button>" in page
    assert 'data-pending="Reading the screenshots"' in page
    assert "Upload chosen screenshots</button>" in page
    assert '<p class="note" role="status" data-chosen></p>' in page


def test_a_second_upload_says_what_it_added_and_what_the_review_holds(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, _ = read_review(browser, monkeypatch, (RED, homework_answer()))
        sent = upload(browser, BLUE, GREEN, form={}, to=f"/parent/imports/{draft_id}/images")
        page = browser.get(sent.headers["location"]).text

    assert lines_in(TOLD.search(page)) == ["2 more screenshots uploaded. 3 in this review, 1 read."]
    assert PROBLEM.search(page) is None
    assert says(page, "3 uploaded · 1 read")


def test_refused_images_keep_their_lines_beside_the_count(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, gif(), RED)
        page = browser.get(f"/parent/imports/{draft_id}").text
        duplicate = upload(browser, RED, form={}, to=f"/parent/imports/{draft_id}/images")
        same = browser.get(duplicate.headers["location"]).text

    assert lines_in(TOLD.search(page)) == ["1 screenshot uploaded. None read yet."]
    assert lines_in(PROBLEM.search(page)) == [imports.image_refusal(1, "not_png_or_jpeg")]
    assert TOLD.search(same) is None
    assert lines_in(PROBLEM.search(same)) == [imports.image_refusal(1, "duplicate")]


def test_the_read_press_says_what_it_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Reader(monkeypatch)
    reader.answers[RED] = ScreenshotAnswer(homework_answer(), None, 10, 5)
    reader.answers[BLUE] = ScreenshotAnswer(homework_answer(image_kind="unsupported"), None, 9, 4)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED, BLUE)
        unread = browser.get(f"/parent/imports/{draft_id}").text
        pressed = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(unread)).text

    assert lines_in(TOLD.search(pressed)) == [
        imports.read_said_line(1, 2),
        imports.not_a_list_line(1),
    ]
    assert imports.read_said_line(1, 2) == "1 screenshot read: 2 assignments found."
    assert imports.not_a_list_line(1) == "1 screenshot read: not a homework list."
    assert imports.read_said_line(3, 1) == "3 screenshots read: 1 assignment found."


@pytest.mark.parametrize(
    ("counts", "said"),
    [
        ((3, 0, 0, 0), "Saved: 3 new assignments."),
        ((1, 0, 0, 0), "Saved: 1 new assignment."),
        (
            (2, 1, 1, 1),
            "Saved: 2 new assignments. 1 updated. 1 already up to date. 1 still needs checking.",
        ),
        ((0, 2, 0, 3), "Saved: 0 new assignments. 2 updated. 3 still need checking."),
        ((0, 0, 2, 0), "Nothing new to save: 2 assignments already up to date."),
        (
            (0, 0, 1, 1),
            "Nothing new to save: 1 assignment already up to date. 1 still needs checking.",
        ),
    ],
)
def test_a_save_s_counts_say_only_what_isn_t_zero(
    counts: tuple[int, int, int, int], said: str
) -> None:
    assert imports.counted(*counts) == said


def test_a_partial_save_says_its_counts_in_the_success_panel(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        first, _ = units_on(page)
        saved = send(browser, draft_id, page_post(page, "save", tick=[first]))

    assert saved.status_code == 200
    assert lines_in(DONE.search(saved.text)) == [
        "Saved: 1 new assignment. 1 still needs checking.",
        imports.PRESS_SAID["left_open"],
    ]
    assert focused(saved.text) == [
        '<div class="panel grade-outcome" role="status" id="done" tabindex="-1" autofocus>'
    ]
    assert PROBLEM.search(saved.text) is None


def test_nothing_new_to_save_says_what_is_up_to_date_and_still_open(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card saved before, saved again from a review with another card still open: nothing
    new is written, the outcome says so with what still needs checking, the review stays open
    with its answers kept."""
    answer = homework_answer(cards=[homework_card()])
    with here(open_settings(tmp_path)) as browser:
        first, page = read_review(browser, monkeypatch, (RED, answer))
        page = her_name_answered(browser, first, page)
        send(browser, first, page_post(page, "save", tick=units_on(page)))
        lab = homework_card(course="Science 8", title="Lab Report", y_top=360, y_bottom=450)
        draft_id, page = read_review(
            browser, monkeypatch, (BLUE, homework_answer(cards=[homework_card(), lab]))
        )
        before = assignment_count(browser)
        held = [
            html.unescape(unit)
            for _, markup in SAVING.findall(page)
            if says(markup, screenshot_page.SAID["already"])
            for unit in re.findall(r'name="unit" value="([^"]+)"', markup)
        ]
        saved = send(browser, draft_id, page_post(page, "save", tick=held))
        after = assignment_count(browser)

    assert len(held) == 1
    assert after == before
    assert lines_in(DONE.search(saved.text)) == [
        "Nothing new to save: 1 assignment already up to date. 1 still needs checking.",
        imports.PRESS_SAID["left_open"],
    ]


def test_a_save_that_closes_the_review_answers_in_the_success_pattern(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The save that finishes a review answers as the grade save does: the steps line at Saved,
    the counts as the panel's heading, which takes the focus, a way to her week where the
    earliest saved assignment is due, that card in view, and that the review is finished;
    nothing in the problem style."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        saved = send(browser, draft_id, page_post(page, "save", tick=units_on(page)))
        [earliest] = [
            a
            for a in state_of(browser).project_state.all_assignments()
            if a.title == "Chains Reading Guide 6"
        ]
        link = re.search(r'<a href="([^"]+)">See them on her week</a>', saved.text)
        assert link is not None
        week = browser.get(html.unescape(link.group(1)))

    text = saved.text
    assert saved.status_code == 200
    assert '<li aria-current="step"><strong>3. Saved</strong></li>' in text
    assert re.search(
        r'<section class="panel grade-outcome" aria-labelledby="outcome-heading">\s*'
        r'<h2 id="outcome-heading" tabindex="-1" autofocus>Saved: 2 new assignments\.</h2>',
        text,
    )
    assert html.unescape(link.group(1)) == navigation.week_href(
        earliest.due_date, earliest.assignment_id
    )
    assert says(text, imports.SAID["closed"])
    assert 'class="problem"' not in text.split("<main", 1)[1]
    assert week.status_code == 200
    assert "Chains Reading Guide 6" in week.text


# ------------------------------------------------------------------ the design's words


def test_the_status_line_and_the_asked_again_line_are_the_design_s(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        _, page = read_review(browser, monkeypatch, (RED, homework_answer()))

    assert screenshot_page.SAID["status"] == (
        "Save review for later keeps your choices and your corrections. Keep these answers "
        "keeps your choices; where a card goes is asked again if you come back before saving "
        "its corrections. Save selected homework saves the ticked assignments with your "
        "corrections."
    )
    assert screenshot_page.SAID["lost_correction"] == (
        "This card's corrections changed since you chose, so choose where it goes again."
    )
    status = re.search(r'<p class="note" id="review-status">([^<]*)</p>', page)
    assert status is not None
    assert html.unescape(status.group(1)) == screenshot_page.SAID["status"]
    presses = page.split('id="review-status"', 1)[1]
    assert presses.index('value="save"') < presses.index("<summary>Other choices</summary>")
    fold = presses.split("<summary>Other choices</summary>", 1)[1].split("</details>", 1)[0]
    assert all(
        words in fold
        for words in (
            "Keep these answers",
            "Save review for later",
            "Finish without the rest",
            "Discard the review",
        )
    )


def described(page: str, words: str) -> list[str]:
    """The ids a button's description names, for the button whose words begin so."""
    for button in re.findall(r"<button [^>]*>.*?</button>", page, re.S):
        text = html.unescape(re.sub(r"<[^>]+>", "", button)).strip()
        if text.startswith(words):
            found = re.search(r'aria-describedby="([^"]+)"', button)
            return [] if found is None else found.group(1).split()
    message = f"no button {words!r}"
    raise AssertionError(message)


def test_read_and_upload_point_to_the_typing_note_while_rows_show(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read and Upload chosen screenshots are forms of their own, which carry none of the fold's
    typing: with rows on the page, each points to the note that says so, Read beside its
    disclosure; before any row shows, there is no such note, and they don't."""
    reader = Reader(monkeypatch)
    reader.answers[RED] = ScreenshotAnswer(homework_answer(), None, 10, 5)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        plain = browser.get(f"/parent/imports/{draft_id}").text
        pressed = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(plain))
        assert pressed.status_code == 200, pressed.text
        sent = upload(browser, BLUE, form={}, to=f"/parent/imports/{draft_id}/images")
        rows = browser.get(sent.headers["location"]).text

    assert 'id="typing-lost"' not in plain
    assert described(plain, "Read") == ["read-disclosed"]
    assert described(plain, "Upload chosen screenshots") == []
    assert rows.count('id="typing-lost"') == 1
    assert described(rows, "Read") == ["read-disclosed", "typing-lost"]
    assert described(rows, "Upload chosen screenshots") == ["typing-lost"]


def test_each_separate_form_says_typing_isn_t_kept(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The note that typing in the fold isn't kept by a form of its own is said once, beside the
    status line, and every button of such a form, which reads saved progress, points to it:
    Find under another name, Leave this group unsaved, Use this change, Set aside, Finish
    without the rest, Remove, Remove only this screenshot, Read and Upload chosen
    screenshots."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, first = read_review(browser, monkeypatch, (RED, homework_answer()))
        sent = upload(browser, BLUE, form={}, to=f"/parent/imports/{draft_id}/images")
        pages = [
            first,
            browser.get(sent.headers["location"]).text,
            a_refused_group(browser, monkeypatch),
            cards_unread(browser, monkeypatch),
        ]

    said = screenshot_page.SAID["typing_lost"]
    seen: set[str] = set()
    for page in pages:
        assert page.count(str(escape(said))) == 1
        status = page.index('id="review-status"')
        assert status < page.index('id="typing-lost"') < page.index('value="save"', status)
        notes = {
            anchor: html.unescape(text)
            for anchor, text in re.findall(r'<p class="helper" id="([^"]+)">([^<]*)</p>', page)
        }
        for button in re.findall(r"<button [^>]*>.*?</button>", page, re.S):
            words = re.sub(r"<[^>]+>", "", button.split('<span class="visually-hidden">')[0])
            words = html.unescape(words).strip()
            if words not in SEPARATE_FORMS:
                continue
            described = re.search(r'aria-describedby="([^"]+)"', button)
            assert described is not None, button
            assert "typing-lost" in described.group(1).split(), button
            assert notes["typing-lost"] == said
            seen.add(words)
    assert seen == set(SEPARATE_FORMS)


SEPARATE_FORMS: Final = (
    "Find under another name…",
    "Leave this group unsaved",
    "Use this change",
    "Set aside",
    "Finish without the rest",
    "Remove",
    "Remove only this screenshot",
    "Read 1 screenshot",
    "Upload chosen screenshots",
)
"""The buttons whose forms read saved progress, not the review form's typing."""


def test_the_type_radios_start_unchecked_under_their_line(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Type's radios start unchecked, the line above them says what Save does with that,
    and a page posted untouched stores no kind: the suggestion stays a suggestion."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        followed(browser, send(browser, draft_id, page_post(page, "keep")))
        answers = stored_answers(browser, draft_id)

    form = review_form(page)
    types = re.findall(r"<legend>Homework or task\?<span.*?</fieldset>", form, re.S)
    assert len(types) == 2
    for markup in types:
        assert " checked" not in markup
        line = markup.index("Saving now: homework, suggested from the title.")
        assert line < markup.index('value="HOMEWORK"')
    assert [kind for kind, _ in answers if kind == "kind"] == []


def test_school_words_are_set_apart_on_the_review(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The school's words on a card are led in as the school's and set apart in their own
    element, never in Blossom's own lines; Blossom's lines carry none of them."""
    words = "Complete pages 12 to 14."
    with here(open_settings(tmp_path)) as browser:
        _, page = read_review(browser, monkeypatch, (RED, homework_answer()))

    form = review_form(page)
    assert (
        f'<p class="from-teacher">From the school: <q class="authored-text">{words}</q></p>' in form
    )
    for line in re.findall(r'<p class="(?:source|effect|note)">([^<]*)</p>', form):
        if not line.startswith("Screenshot 1, card"):
            assert words not in html.unescape(line)


def test_a_confident_field_corrected_saves_with_no_call(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A title and a due date the reader was sure of, corrected in the fold and saved: the
    assignment is saved as corrected, and the reader is never called again."""
    reader = Reader(monkeypatch)
    reader.answers[RED] = ScreenshotAnswer(homework_answer(cards=[homework_card()]), None, 10, 5)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        unread = browser.get(f"/parent/imports/{draft_id}").text
        page = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(unread)).text
        calls = len(reader.calls)
        page = her_name_answered(browser, draft_id, page)
        typed = {
            f"correct:{box_subject(page, 'title')}": "Sentence Craft",
            f"correct:{box_subject(page, 'due')}": "2026-10-09",
        }
        corrected = followed(browser, send(browser, draft_id, page_post(page, "keep", typed=typed)))
        saved = followed(
            browser, send(browser, draft_id, page_post(corrected, "save", tick=units_on(corrected)))
        )
        [row] = [
            a
            for a in state_of(browser).project_state.all_assignments()
            if a.title == "Sentence Craft"
        ]

    assert "<h3>Sentence Craft</h3>" in corrected
    assert '<p class="due">Due Friday, October 9</p>' in corrected
    assert says(saved, "Saved: 1 new assignment.")
    assert row.due_date == date(2026, 10, 9)
    assert len(reader.calls) == calls == 1


def test_a_correction_saved_with_its_group_isn_t_said_to_be_unsaved(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A correction typed on the group a save takes is saved with it, so the rows left open
    aren't said to hold unsaved changes. One typed on a row left open still is, as
    ``test_a_partial_save_returns_the_rows_left_open_with_their_typing_marked_unsaved``
    shows."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, page)
        saved_box = f"correct:{box_subject(page, 'title', 0)}"
        kept = followed(
            browser,
            send(browser, draft_id, page_post(page, "keep", typed={saved_box: "Sentence Craft"})),
        )
        assert says(kept, imports.PRESS_SAID["kept"])
        first = next(u for u in units_on(kept) if u in digests_on(kept))
        taken = send(browser, draft_id, page_post(kept, "save", tick=[first]))
        titles = [a.title for a in state_of(browser).project_state.all_assignments()]

    assert says(taken.text, "Saved: 1 new assignment. 1 still needs checking.")
    assert "Sentence Craft" in titles
    assert not says(taken.text, imports.PRESS_SAID["unsaved_typing"])
    assert not says(taken.text, screenshot_page.SAID["not_saved_yet"])


def test_a_press_refused_for_two_boxes_says_nothing_was_kept_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A press with an instruction past its limit and a cleared title: the refusal says each
    box's sentence and that nothing was kept or saved, once."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, page, boxes = unsure_review(browser, monkeypatch, "instruction")
        typed = {boxes["instruction"]: LINES_OF_A + "a", boxes["title"]: ""}
        refused = send(browser, draft_id, page_post(page, "keep", typed=typed))
        cleared = send(browser, draft_id, page_post(page, "keep", typed={boxes["title"]: ""}))

    lines = lines_in(PROBLEM.search(refused.text))
    assert refused.status_code == cleared.status_code == 422
    assert lines == [imports.PRESS_SAID["too_long"], "A title is needed."]
    assert sum("nothing was kept or saved" in line.casefold() for line in lines) == 1
    assert lines_in(PROBLEM.search(cleared.text)) == [
        "A title is needed.",
        imports.PRESS_SAID["nothing_kept"],
    ]
    assert imports.PRESS_SAID["none_chosen"] == "Choose at least one assignment to save."
