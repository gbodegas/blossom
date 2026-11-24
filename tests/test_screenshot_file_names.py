# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Each stored screenshot shows the name of the file it came from beside its preview, after the
upload and whenever the review is opened again, through the routes with made-up images only.

The name is the file's own, without any folder. A replacement brings its own name and the
replaced one leaves with its screenshot. A review saved before screenshots kept their names
opens as it always did, by place and preview. No name reaches the reader's request, a log
line, an address or the note a press leaves.
"""

import hashlib
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from blossom import anthropic_client, screenshot_reading
from blossom.routes import imports
from blossom.settings import Settings
from blossom.uploads import prepare_image
from tests.support import (
    BLUE,
    GREEN,
    RED,
    Answer,
    answer_body,
    first_form,
    here,
    homework_answer,
    images_of,
    open_settings,
    state_of,
)

FIRST = "fractions-page.png"
SECOND = "spelling-words.png"
REPLACEMENT = "spelling-words-retake.png"
NAMES = (FIRST, SECOND, REPLACEMENT)
READ_NOW = {"read": "now"}
ITEM = re.compile(
    r'<li>\s*<a href="/parent/imports/[0-9a-f]{32}/images/([0-9a-f]{32})">'
    r'<img [^>]*alt="Screenshot (\d+)"[^>]*></a>\s*<div>(.*?)</div>\s*</li>',
    re.S,
)
"""One stored screenshot of a page's list: its preview's image id, its place, and the words
beside the preview."""
NAME_LINE = re.compile(r'<p class="file-name"><bdi>(.*?)</bdi></p>')


def send(browser: TestClient, to: str, form: dict[str, str], *files: tuple[str, bytes]) -> Answer:
    """A press with these files chosen, each under its file's name, as a browser sends it."""
    parts = [("screenshots", (name, data, "image/png")) for name, data in files]
    return browser.post(to, data=form, files=parts)


def started(browser: TestClient, *files: tuple[str, bytes], read: bool = False) -> str:
    """A first upload of these files that commits: the review its redirect names."""
    form = {**first_form(browser), **(READ_NOW if read else {})}
    sent = send(browser, "/parent/imports", form, *files)
    assert sent.status_code == 303, sent.text
    found = re.fullmatch(r"/parent/imports/([0-9a-f]{32})", sent.headers["location"])
    assert found is not None
    return found.group(1)


def shown(page: str) -> list[tuple[int, str, str | None]]:
    """Each stored screenshot a page lists: its place, its preview's image id, and the name
    beside the preview, or None when there is no name line."""
    items = []
    for image_id, place, beside in ITEM.findall(page):
        names = NAME_LINE.findall(beside)
        assert len(names) <= 1, beside
        assert f"Screenshot {place} \u00b7 " in beside, beside
        items.append((int(place), image_id, names[0] if names else None))
    return items


def digest(data: bytes) -> str:
    """The SHA-256 of the PNG Blossom keeps for an uploaded file."""
    return hashlib.sha256(prepare_image(data).png).hexdigest()


def preview(browser: TestClient, draft_id: str, image_id: str) -> str:
    """The SHA-256 of the preview a screenshot's link serves."""
    answer = browser.get(f"/parent/imports/{draft_id}/images/{image_id}")
    assert answer.status_code == 200
    return hashlib.sha256(answer.content).hexdigest()


def replace_path(draft_id: str, image_id: str) -> str:
    return f"/parent/imports/{draft_id}/images/{image_id}/replace"


def names_in_file(settings: Settings) -> list[str | None]:
    connection = sqlite3.connect(settings.database_path)
    try:
        query = "SELECT file_name FROM import_draft_images ORDER BY draft_id, position"
        return [row[0] for row in connection.execute(query)]
    finally:
        connection.close()


def test_each_name_stays_beside_its_preview_through_reopening_and_a_replace(
    tmp_path: Path,
) -> None:
    """His journey: two files with distinct names, the review reopened, the second replaced,
    and the review reopened again."""
    settings = open_settings(tmp_path)
    with here(settings) as browser:
        draft_id = started(browser, (FIRST, RED), (SECOND, BLUE))
        after_upload = browser.get(f"/parent/imports/{draft_id}").text
        first_id, second_id = images_of(browser, draft_id)
        reopened = browser.get(f"/parent/imports/{draft_id}").text
        step = browser.get(replace_path(draft_id, second_id)).text
        replaced = send(browser, replace_path(draft_id, second_id), {}, (REPLACEMENT, GREEN))
        again = browser.get(f"/parent/imports/{draft_id}").text
        kept_id, new_id = images_of(browser, draft_id)
        previews = {
            image_id: preview(browser, draft_id, image_id)
            for image_id in images_of(browser, draft_id)
        }
        gone = browser.get(f"/parent/imports/{draft_id}/images/{second_id}")
    stored = names_in_file(settings)

    assert shown(after_upload) == [(1, first_id, FIRST), (2, second_id, SECOND)]
    assert shown(reopened) == [(1, first_id, FIRST), (2, second_id, SECOND)]
    assert shown(step) == [(2, second_id, SECOND)]
    assert replaced.status_code == 303
    assert (kept_id, new_id != second_id) == (first_id, True)
    assert shown(again) == [(1, first_id, FIRST), (2, new_id, REPLACEMENT)]
    assert previews == {first_id: digest(RED), new_id: digest(GREEN)}
    assert gone.status_code != 200
    assert SECOND not in again
    assert stored == [FIRST, REPLACEMENT]


def test_a_name_sent_with_its_folder_shows_and_is_kept_without_it(tmp_path: Path) -> None:
    settings = open_settings(tmp_path)
    windows = f"C:\\Users\\Sentinel-Folder-K\\Pictures\\{FIRST}"
    posix = f"Sentinel-Folder-S/2026/{SECOND}"
    with here(settings) as browser:
        draft_id = started(browser, (windows, RED), (posix, BLUE))
        page = browser.get(f"/parent/imports/{draft_id}").text

    assert [name for _, _, name in shown(page)] == [FIRST, SECOND]
    assert "Sentinel-Folder" not in page
    assert names_in_file(settings) == [FIRST, SECOND]
    assert b"Sentinel-Folder" not in settings.database_path.read_bytes()


def test_a_review_saved_before_names_opens_with_its_place_and_preview(tmp_path: Path) -> None:
    settings = open_settings(tmp_path)
    with here(settings) as browser:
        draft_id = started(browser, (FIRST, RED), (SECOND, BLUE))
        first_id, second_id = images_of(browser, draft_id)
    connection = sqlite3.connect(settings.database_path)
    with connection:
        connection.execute("ALTER TABLE import_draft_images DROP COLUMN file_name")
    connection.close()

    with here(settings) as browser:
        page = browser.get(f"/parent/imports/{draft_id}").text
        step = browser.get(replace_path(draft_id, second_id)).text
        previews = [preview(browser, draft_id, image_id) for image_id in (first_id, second_id)]

    assert shown(page) == [(1, first_id, None), (2, second_id, None)]
    assert shown(step) == [(2, second_id, None)]
    assert previews == [digest(RED), digest(BLUE)]
    assert 'class="file-name"' not in page + step
    assert [name for name in NAMES if name in page + step] == []
    assert names_in_file(settings) == [None, None]


def test_no_name_reaches_a_reader_request_a_log_an_address_or_a_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stand-in reader behind the real request: the service's side is a mock transport that
    keeps every request it is sent, so nothing leaves the machine."""
    requests: list[httpx.Request] = []

    def service(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = answer_body(homework_answer())
        return httpx.Response(200, headers={"content-type": "application/json"}, content=body)

    def reader(settings: Settings, png: bytes, *, timeout: float) -> object:
        return anthropic_client.read_screenshot(
            settings, png, timeout=timeout, transport=httpx.MockTransport(service)
        )

    monkeypatch.setattr(screenshot_reading, "read_screenshot", reader)
    caplog.set_level(logging.DEBUG)
    settings = open_settings(tmp_path)
    answers: list[Any] = []
    with here(settings) as browser:
        key = state_of(browser).result_key
        form = {**first_form(browser), **READ_NOW}
        answers.append(send(browser, "/parent/imports", form, (FIRST, RED), (SECOND, BLUE)))
        draft_id = answers[-1].headers["location"].rsplit("/", 1)[1]
        answers.append(browser.get(f"/parent/imports/{draft_id}"))
        _, second_id = images_of(browser, draft_id)
        to = replace_path(draft_id, second_id)
        answers.append(send(browser, to, READ_NOW, (REPLACEMENT, GREEN)))
        answers.append(browser.get(f"/parent/imports/{draft_id}"))
        page = answers[-1].text

    sent = [
        str(request.url).encode()
        + b"".join(name + b": " + value for name, value in request.headers.raw)
        + request.content
        for request in requests
    ]
    logged = " ".join(
        f"{record.getMessage()} {record.args!r} {record.exc_text}" for record in caplog.records
    )
    headers = " ".join(
        value
        for answer in answers
        for value in answer.headers.get_list("location") + answer.headers.get_list("set-cookie")
    )
    cookie = re.compile(rf"{imports.NOTE_COOKIE}=([^;]+)")
    notes = [repr(imports.note_read(key, draft_id, value)) for value in cookie.findall(headers)]
    holders = sorted(
        path.name
        for path in tmp_path.rglob("*")
        if path.is_file() and any(name.encode() in path.read_bytes() for name in NAMES)
    )

    assert len(requests) == 3
    assert all(b"image/png" in body for body in sent)
    assert [name for name in NAMES for body in sent if name.encode() in body] == []
    assert caplog.records
    assert [name for name in NAMES if name in logged] == []
    assert notes
    assert [name for name in NAMES if name in headers + " ".join(notes)] == []
    assert [name for name in NAMES for answer in answers if name in str(answer.url)] == []
    assert [name for _, _, name in shown(page)] == [FIRST, REPLACEMENT]
    assert holders == [settings.database_path.name]
    assert SECOND.encode() not in settings.database_path.read_bytes()
