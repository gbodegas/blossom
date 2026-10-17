# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Homework screenshots in Family review, through their routes: the upload, the images and a
Read press, with a fake reader and synthetic images only.

Every route serves a parent alone, checked before any body is read, and every answer is marked
no-store. Nothing on Family review offers the upload yet.
"""

import dataclasses
import html
import io
import json
import logging
import pathlib
import re
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from PIL import Image

from blossom import screenshot_page, screenshot_reading
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
    fixture_settings,
    homework_answer,
    homework_card,
    signed_in,
    signed_in_household,
    state_of,
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
    assert control.count("Sentence Composing, due Fri 10/9, no assigned date, No instructions") == 2
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
        "Due date for Sentence Composing, due Fri 10/9, no assigned date, No instructions" in form
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
        elif kind == "text":
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
    assert answer.status_code == 303, answer.text
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
        "corrections": {json.loads(field)[0]: {"title": "Sentence Combining"}},
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
        found = browser.get(
            f"/parent/imports/{draft_id}/find", params={"piece": piece, "words": "grammar"}
        )
        nothing = browser.get(
            f"/parent/imports/{draft_id}/find", params={"piece": piece, "words": "zzzz"}
        )
        gone = browser.get(f"/parent/imports/{draft_id}/find", params={"piece": "x", "words": "a"})
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


IMPORT_REVIEW_RULES = (
    ".import-review .choice {\n  min-width: 0;\n"
    "  padding-inline: clamp(0px, 13vw - 1rem, 1rem);\n}\n\n"
    ".import-review .choice label > span {\n  overflow-wrap: anywhere;\n}\n\n"
    ".import-review .panel,\n.import-review article {\n"
    "  margin-inline: min(0px, 13vw - 2.25rem);\n"
    "  padding-inline: clamp(0px, 13vw - 1rem, 1.6rem);\n}\n\n"
    "@media (max-width: 30rem) {\n"
    "  .import-review .panel,\n  .import-review article {\n"
    "    padding-inline: clamp(0px, 13vw - 1rem, 1.2rem);\n  }\n}\n"
)
"""The screenshot review's rules, pinned whole: on a narrow screen with large text its
fieldsets take the page's width and its cards' side padding gives way before a word breaks."""


def test_the_screenshot_review_s_own_rules_are_pinned_and_only_its_rows_carry_its_class() -> None:
    static = pathlib.Path(imports.__file__).parents[1] / "static" / "blossom.css"
    css = static.read_text(encoding="utf-8")
    carriers = sorted(
        page.name
        for page in (static.parents[1] / "templates").glob("*.html")
        if "import-review" in page.read_text(encoding="utf-8")
    )

    assert css.count("import-review") == 6
    assert css.count(IMPORT_REVIEW_RULES) == 1
    assert carriers == ["import_rows.html"]


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
        ("GET", f"/parent/imports/{draft_id}/find"),
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
        ("GET", "/parent/imports/{draft_id}/find"),
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


def test_family_review_offers_no_screenshot_upload_yet(tmp_path: pathlib.Path) -> None:
    with here(open_settings(tmp_path)) as browser:
        page = browser.get("/parent")

    assert page.status_code == 200
    assert "/parent/imports" not in page.text
    assert "Add screenshots" not in page.text


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
