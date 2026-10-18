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
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Final
from urllib.parse import urlencode

import pytest
import uvicorn
from fastapi.testclient import TestClient
from markupsafe import escape
from PIL import Image

from blossom import screenshot_page, screenshot_posts, screenshot_reading
from blossom.anthropic_client import ScreenshotAnswer
from blossom.app import create_app
from blossom.routes import imports
from blossom.screenshot_save import (
    CONFIRMABLE,
    correction_digest,
)
from blossom.settings import ANTHROPIC_API_KEY_VARIABLE, SCREENSHOT_IMPORT_VARIABLE, Settings
from blossom.stores.import_drafts import (
    DRAFT_LIFETIME,
    FAILURES,
    AnswerToKeep,
    DraftExpired,
    new_creation_token,
)
from blossom.stores.project_state import Assignment, AssignmentKind
from blossom.uploads import as_new_image, prepare_image
from tests.support import (
    HERS,
    PLAN_DATE,
    THEIRS,
    Answer,
    Element,
    Rule,
    UnreadCss,
    View,
    elements_of,
    fixture_settings,
    homework_answer,
    homework_card,
    homework_from_a_note,
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

    assert says(page, "Screenshot 1: Not read yet.")
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
    assert says(page, "A new Sentence Composing, due Thu 10/8")
    assert says(page, "Adds \u201cComplete pages 12 to 14.\u201d")
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
    signed = [(name, value) for name, value in hidden if name != "check"]
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
    composing = re.search(r"<legend>Sentence Composing \(.*?</fieldset>", form, re.S)
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
        assert says(saved.text, imports.counted(1, 0, 0))
        _, again = read_review(browser, monkeypatch, (RED, answer))

    form = review_form(again)
    label = (
        "Sentence Composing, due Thu 10/8, assigned Mon 10/5, \u201cComplete pages 12 to 14.\u201d"
    )
    assert f"Already saved in {label}." in html.unescape(form)
    assert len([n for n, _ in checked_values(form) if n.startswith("answer:destination:")]) == 1
    assert "A new Sentence Composing (1)" in form
    assert screenshot_page.SAID["find"] in html.unescape(form)
    assert f"Already saved in {label}. Nothing new is written." in html.unescape(form)


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
    assert "Sentence Composing (screenshot 1, card 1): The reader wasn't sure of the title." in form
    assert screenshot_page.SAID["confirm"] in form
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
    assert " checked" not in form.split("Where each card goes")[0].split("Different assigned")[-1]


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
    checked controls (or those chosen here), the text typed, the groups ticked, the button."""
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
    fields += [*choose.items(), *(("unit", unit) for unit in tick), ("press", press)]
    return fields


def send(browser: TestClient, draft_id: str, fields: Sequence[tuple[str, str]]) -> Answer:
    return browser.post(
        f"/parent/imports/{draft_id}/review",
        content=urlencode(list(fields)).encode(),
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


def test_a_page_made_before_a_newer_answer_keeps_nothing_and_says_the_review_changed(
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
    assert says(after, imports.CHANGED_IN_ANOTHER_TAB)
    assert says(after, imports.PRESS_SAID["keep_stale"])
    assert says(refused, imports.PRESS_SAID["save_stale"])
    assert says(later, imports.PRESS_SAID["later_stale"])


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
    assert says(saved.text, imports.counted(2, 0, 0))
    assert says(saved.text, imports.SAID["closed"])
    assert made_now - before == made_after - before == 2
    assert says(again.text, imports.counted(2, 0, 0))


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

    assert says(after, imports.counted(1, 0, 1))
    assert says(after, imports.PRESS_SAID["left_open"])
    assert units_on(after) == [second]
    assert says(again, imports.counted(1, 0, 1))
    assert made - before == 1


REFUSED_OPEN = (
    "The homework wasn't saved: a group you chose still has a question to answer. Nothing of "
    "it was written."
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
    assert says(saved.text, imports.counted(1, 0, 0))


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
    assert says(saved.text, imports.counted(2, 0, 0))
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
        [field] = fields_named(review_form(page), "correct:")
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
        "v": 3,
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
    assert "Type: Sentence Composing (screenshot 1, card 1)" in form
    assert screenshot_page.SAID["type_helper"] in form
    assert "Homework or task for Sentence Composing, due Thu 10/8" in html.unescape(after)
    assert "homework or task" in html.unescape(after)


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

    assert says(saved.text, imports.counted(1, 0, 0))
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

    assert says(saved.text, imports.counted(1, 0, 0))
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
        content=urlencode(fields).encode(),
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


def test_each_card_shows_what_was_read_for_it_beside_its_destination_choices(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        _, page = read_review(browser, monkeypatch, (RED, homework_answer()))

    form = html.unescape(review_form(page))
    assert (
        "This card: Sentence Composing, due Thu 10/8, assigned Mon 10/5, “Complete pages 12 to 14.”"
    ) in form
    assert "This card: Chains Reading Guide 6, due Mon 10/5, no assigned date, no instructions" in (
        form
    )


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
    assert says(saved.text, imports.counted(1, 0, 0))
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
        [field] = fields_named(review_form(page), "correct:")
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
        [field] = fields_named(review_form(page), "correct:")
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
        [field] = fields_named(review_form(page), "correct:")
        send(browser, draft_id, page_post(page, "keep"))
        typed = {f"correct:{field}": "Sentence Craft"}
        stale = send(browser, draft_id, page_post(page, "keep", typed=typed))

    assert stale.status_code == 409
    assert says(stale.text, imports.CHANGED_IN_ANOTHER_TAB)
    assert says(stale.text, imports.PRESS_SAID["keep_stale"])
    assert 'value="Sentence Craft"' in review_form(stale.text)


# ------------------------------------------------------------------ saved progress


def test_saved_progress_is_kept_in_its_one_shape_and_any_other_reads_as_none(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, unsure_answer()))
        page = her_name_answered(browser, draft_id, page)
        [field] = fields_named(review_form(page), "correct:")
        key = json.loads(field)[0]
        typed = {f"correct:{field}": "Sentence Combining"}
        send(browser, draft_id, page_post(page, "later", typed=typed))
        student = state_of(browser).project_state.student_id()
        drafts = state_of(browser).import_drafts
        written = drafts.snapshot(student, draft_id).progress
        shown = []
        for other in (
            {"corrections": {key: {"title": "Sentence Combining"}}, "selected": []},
            {"v": 2, "corrected": {key: {"title": "Sentence Combining"}}, "selected": []},
            {"v": 3, "corrected": {key: {"title": "Sentence Combining"}}, "selected": [], "x": 1},
            {"v": 3, "corrected": {key: {"shade": "red"}}, "selected": []},
            {"v": 3, "corrected": {key: {"title": "Sentence Combining"}}, "selected": [7]},
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
        "v": 3,
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
        assert says(answer.text, imports.counted(1, 0, 1))
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
        [field] = fields_named(review_form(page), "correct:")
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
    control = re.search(r"<legend>Sentence Composing \(.*?</fieldset>", form, re.S)
    assert control is not None
    assert "would copy the words of" in form
    assert f'value="{offered}"' in control.group(0)
    assert "Sentence Composing, due Fri 10/30, assigned Mon 10/5" in control.group(0)
    assert "ref " in control.group(0) or holder.assignment_id in control.group(0)
    assert "Has this card's words." in control.group(0)
    assert "Due Fri 10/30: this card says Thu 10/8." in control.group(0)
    assert chosen.status_code == 200
    assert answers[("destination", piece)] == offered


def preview_labels(page: str) -> list[str]:
    """The destination each entry of the group preview names, as the page reads."""
    found = re.search(r'aria-labelledby="saving-heading">.*?</section>', page, re.S)
    assert found is not None
    return [html.unescape(h) for h in re.findall(r"<h3>(.*?)</h3>", found.group(0))]


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
    assert plain in preview_labels(kept)


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
    assert f"Found under another name: {destination}" in preview_labels(refused)


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
            send(browser, draft_id, [*page_post(asked, "keep"), (field, "Study list one.")]),
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
    assert says(added, "1 assignment added. 0 updated.")
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
            browser, send(browser, draft_id, page_post(page, "keep") + [(name, t) for t in texts])
        )
        saved = followed(
            browser, send(browser, draft_id, page_post(page, "save", tick=units_on(page)))
        )
        counts = acceptance_counts(browser, draft_id, page)
        decided = state_of(browser).project_state.intake_decisions(
            [("English 8", "Vocab Test"), ("English 8", "Spelling List")]
        )

    assert says(saved, "0 assignments added. 1 updated.")
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
    assert offered == ["Study list one.", "Study list two."]
    assert [v for n, v in posted if n.startswith("answer:instructions:")] == offered
    assert says(saved, "0 assignments added. 1 updated.")


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
        assert says(shown, FROZEN)
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


def typed_on(page: str) -> list[str]:
    return [html.unescape(v) for v in re.findall(r'name="correct:[^"]*" value="([^"]*)"', page)]


def assert_returned_with_saved_typing(page: str, chosen: str, typed: str) -> None:
    """The page a found row's or an offer's post returns: the correction saved for later in
    its box, the destination posted chosen, and no answer set aside for a lost correction."""
    assert typed_on(page) == [typed]
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
    same_words = "Complete pages 12 to 14."
    with here(open_settings(tmp_path)) as browser:
        first, page = read_review(browser, monkeypatch, (BLUE, later_card))
        page = her_name_answered(browser, first, page)
        send(browser, first, page_post(page, "save", tick=units_on(page)))
        state_of(browser).project_state.upsert_assignments([found_row])
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer(cards=[unsure])))
        [box] = fields_named(review_form(page), "correct:")
        followed(
            browser,
            send(browser, draft_id, page_post(page, "later", typed={f"correct:{box}": same_words})),
        )
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
        assert_returned_with_saved_typing(found_page, found, same_words)
        _, offer_fields = outside(found_page, "offer-1-1")
        offered = dict(offer_fields)[f"answer:destination:{piece}"]
        offer_page = followed(browser, press_outside(browser, found_page, "offer-1-1"))

    assert_returned_with_saved_typing(offer_page, offered, same_words)


def test_each_card_s_own_line_shows_its_instructions_whole(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long = "Complete pages 12 to 14, then write three sentences about each of the stories."
    with here(open_settings(tmp_path)) as browser:
        _, page = read_review(
            browser, monkeypatch, (RED, homework_answer(cards=[homework_card(instruction=long)]))
        )

    line = f"This card: Sentence Composing, due Thu 10/8, assigned Mon 10/5, \u201c{long}\u201d"
    assert line in html.unescape(review_form(page))


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
    assert "This card: Sentence Composing, due Thu 10/8" in html.unescape(review_form(settled))
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
    assert "This card: Sentence Composing, due Tue 10/6" in form
    assert "said: due line none; day heading \u201cTuesday 10/6/2026\u201d." in form


def test_a_due_card_has_one_due_date_box_and_an_assigned_card_its_two_labeled(
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
    assert due_form.count('type="date"') == 1
    assert "The reader wasn't sure of the due date." in due_form
    assert '>Due date <input type="date"' in due_form
    assigned_form = html.unescape(review_form(assigned_page))
    assert assigned_form.count('type="date"') == 2
    assert '>Due date <input type="date"' in assigned_form
    assert '>Assigned date (day heading) <input type="date"' in assigned_form


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
    assert 'name="correct:' not in form
    assert screenshot_page.SAID["confirm"] in form
    assert says(refused, imports.PRESS_SAID["keep_invalid"])
    key = json.loads(subject)[0]
    badge = {"v": 3, "corrected": {key: {"badge": "DUE"}}, "selected": []}
    assert screenshot_posts.progress_read(json.dumps(badge)) is None


def test_a_typed_date_blossom_can_t_read_is_said_and_not_used(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = homework_answer(
        cards=[homework_card(uncertain=[{"field": "footer", "reason": "blurred"}])]
    )
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, answer))
        [field] = fields_named(review_form(page), "correct:")
        kept = send(browser, draft_id, page_post(page, "keep", typed={f"correct:{field}": "10/14"}))

    shown = html.unescape(kept.text)
    assert "The date given was “10/14”, which is not a date Blossom can read." in shown
    assert "This card: Sentence Composing, due Thu 10/8" in shown


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
        for card in re.findall(r'<fieldset class="choice">.*?</fieldset>', review_form(page), re.S)
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
        [field] = fields_named(review_form(page), "correct:")
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
        [field] = fields_named(review_form(page), "correct:")
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
    '.import-review input[type="file"] {\n  max-width: 100%;\n}\n\n'
    ".import-review legend,\n.import-review .note,\n.import-review .source,\n"
    ".import-review .effect,\n.import-review .problem,\n.import-review .choice > p,\n"
    ".import-review li {\n  overflow-wrap: anywhere;\n}\n\n"
    ".import-review button,\n.import-review input {\n  overflow-wrap: normal;\n}\n\n"
    "@media (max-width: 30rem) {\n"
    "  .import-review .panel,\n  .import-review .panel article {\n"
    "    padding-inline: clamp(0px, 13vw - 1rem, 1.2rem);\n  }\n\n"
    '  .import-review input[type="date"] {\n'
    "    padding-inline: clamp(2vw, 100000vw - 14650rem - 14500px, 0.85rem);\n  }\n}\n"
)
"""The screenshot review's rules, pinned whole: on a narrow screen with large text its
fieldsets take the page's width, a panel's side padding gives way and the panel reaches into
the page's margin, a card inside a panel gives way without reaching further, a file input is
never wider than its panel, and school text breaks where its line can't hold it while a
control's words never do."""
STYLESHEET = pathlib.Path(imports.__file__).parents[1] / "static" / "blossom.css"


def test_the_screenshot_review_s_own_rules_are_pinned_and_only_its_pages_carry_its_class() -> None:
    css = STYLESHEET.read_text(encoding="utf-8")
    carriers = sorted(
        page.name
        for page in (STYLESHEET.parents[1] / "templates").glob("*.html")
        if "import-review" in page.read_text(encoding="utf-8")
    )

    assert css.count("import-review") == 17
    assert css.count(IMPORT_REVIEW_RULES) == 1
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
    assert says(page.text, imports.LIMITS)
    assert says(page.text, imports.DISCLOSURE)
    assert (
        '<form method="post" action="/parent/imports" enctype="multipart/form-data">' in page.text
    )
    assert sent.status_code == 303
    draft_id = sent.headers["location"].rsplit("/", 1)[-1]
    assert f'href="/parent/imports/{draft_id}"' in listed
    assert "Homework screenshots from " in listed
    assert "Available until " in listed


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


def test_turned_off_every_import_address_answers_as_a_missing_one_before_any_body(
    tmp_path: pathlib.Path,
) -> None:
    """Off by default, each import route answers exactly as an address that doesn't exist, with
    an upload far over the limits unread, no review made, and nothing marking the address."""
    settings = open_settings(tmp_path, turned_on=False)
    assert settings.screenshot_import is False
    with here(settings) as browser:
        missing = browser.get("/parent/no-such-address")
        answers = {
            (method, path): browser.request(method, path, content=UNREAD_BODY)
            for method, path in every_route("0" * 32, "1" * 32)
        }
        count = drafts(browser)

    assert missing.status_code == 404
    assert {key: answer.status_code for key, answer in answers.items()} == dict.fromkeys(
        answers, 404
    )
    assert {answer.text for answer in answers.values()} == {missing.text}
    assert all("cache-control" not in answer.headers for answer in answers.values())
    assert count == 0


def test_turned_off_a_signed_in_parent_finds_no_import_address(tmp_path: pathlib.Path) -> None:
    with TestClient(
        create_app(signed_in_household(tmp_path)),
        follow_redirects=False,
        headers={"Origin": "http://testserver"},
    ) as parent:
        signed_in(parent, THEIRS)
        answers = [
            parent.request(method, path, content=b"--unread body")
            for method, path in every_route("0" * 32, "1" * 32)
        ]
        count = drafts(parent)

    assert [answer.status_code for answer in answers] == [404] * len(answers)
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
        ("a split question open", "1 assignment added. 0 updated. 1 still needs checking.", 1),
        ("two cards it couldn't read", "1 assignment added. 0 updated. 2 still need checking.", 2),
        ("nothing left", "1 assignment added. 0 updated. 0 still need checking.", 0),
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
    outcome = "1 assignment added. 0 updated. 0 still need checking."
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

    assert f'form="{form_id}">Find under another name' in page
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
    """Each correction box's subject, by its field."""
    return {json.loads(s)[1]: s for s in fields_named(review_form(page), "correct:")}


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
        typed = {f"correct:{boxes['heading']}": "2026-10-06"}
        choose: dict[str, str] = {}
        if case == "with the due date chosen":
            choose = {
                f"answer:due_read:{piece}": due_choice(page, "Due Fri 10/9, as the due line says")
            }
        if case == "due date typed":
            typed = {f"correct:{boxes['footer']}": "2026-10-09"}
            choose = {f"answer:confirm:{boxes['heading']}": "as_read"}
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
        box = boxes_of(page)[field]
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
    assert saved.corrected[key][field] == "10/14"
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
    "the piece legends": ("review", lambda e: e.tag == "legend" and not e.text.startswith("Type:")),
    "the question legends": ("review", lambda e: e.tag == "legend" and e.text.startswith("Type:")),
    "the card's own line": (
        "review",
        lambda e: e.tag == "p" and "note" in e.classes and e.text.startswith("This card:"),
    ),
    "the facts": ("review", lambda e: "source" in e.classes),
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
