# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The screenshot upload forms' two presses, and what each press reads, through the routes with
the stand-in reader and made-up images only.

"Upload without reading" stores and reads nothing. "Upload and read" stores, then reads exactly
the images that press added. The review's Read press names each screenshot it sends and reads
exactly those.

Choosing sends nothing: each picker carries a line that says so, a list the page script fills
with what was chosen, and the page script holds no network call and no submit call. Each press
carries its own busy words, and the busy-text handler keeps a named press's value. Once the
browser has built what an upload sends, the list and its pickers are held, so the page can't say
it removed or replaced a file once the files are sent; a page shown again hands them back.

Each screenshot on the review says in words how it stands and why. One that needs attention stays
in view above the review; one read with homework folds. The missing-name question leaves out a
screenshot with nothing to save and names the ones it covers. Before a save, a line beside Save
says saving fixes the screenshots; after one, a screenshot that needs attention says what can be
done. No press but a read calls the reader.

While the review isn't frozen, each screenshot links to a replace step for it. One press puts a
new file in its place; the other screenshots, their readings and answers, the name answer and
the saved corrections and ticks stay, the replaced screenshot's own answers are cleared and
named, and a refused new file keeps the old one. Replace and read sends the new file only.

Where the review holds its form, Replace and Enter it by hand are presses of that form: each
keeps the corrections, ticks and answers as Save review for later does, with its refusals, then
goes on to the replace step or to hand entry on Family review, whose query only opens Add
assignments. Where it holds no form, they are links.

A screenshot whose read ended with no reading kept still says so, and offers Enter it by hand,
after another screenshot is read. The note after a change of screenshots stays small enough for
a browser to keep, whatever script its titles are in: one budget on its whole Set-Cookie header,
met by naming fewer rows in every list while every outcome and its count stays. Rows sharing a
title are named once by it, and every count is of the rows.

A line about screenshots left unread says "Press Read" only when the page that shows it offers
Read for those same screenshots, by their hashes, whatever another browser did in between. Enter
in a box of the review form presses the form's own default, never a screenshot's press.

The page an upload or a replace lands on keeps what that press did, the count it added or the
screenshot it replaced, and says what is in the review as its list shows it, whatever another
browser did in between: the totals and how the screenshots read, the new screenshot's place, and
each reading the busy file couldn't keep, by its place only while the list holds it and with
"Read again." only when Read sends it. A Read press's own page says those readings the same way.

An Upload and read or a Replace and read that read nothing because a read was under way says
reading is under way, with the time it ends, only while the page it lands on shows that same
read as current; otherwise it says a read was already under way, so the press didn't start
another, and the page offers Read as it would for any other unread screenshot.

A press whose store was busy as it loaded a screenshot sent nothing to be read, and the page it
answers with says the screenshot wasn't sent to be read, by its place only while the list holds
it and with "Press Read to send it." only when Read sends it; a press whose store was busy as it
kept the reading says the reading couldn't be kept. Every Read press, Upload and read and Replace
and read says it the same way.
"""

import base64
import html
import json
import pathlib
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Final, get_args

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom import screenshot_page, screenshot_review, screenshots
from blossom.anthropic_client import ReadFailure, ScreenshotAnswer
from blossom.app import create_app
from blossom.intake import CardsRead, PortalCard, Reading, read_cards
from blossom.routes import imports
from blossom.screenshot_review import ChangeTold, ClearedRow
from blossom.screenshots import KEY_INSTANT, homework_assembly
from blossom.settings import Settings
from blossom.stores.drafts import StoreBusy, WriterBusy
from blossom.stores.import_drafts import READ_DEADLINE_SECONDS, ImageState, StoredReading
from blossom.uploads import prepare_upload
from tests import import_review_cases
from tests.import_review_cases import page_post, send
from tests.support import (
    ASK_NAME_AGAIN,
    BLUE,
    CLEARED_INTRO,
    ESSAY_NOTE,
    GREEN,
    JOINED_TICK,
    LOOPBACK,
    PROBLEM,
    PS,
    RED,
    RL,
    THU,
    TOLD,
    Answer,
    FakeTime,
    Reader,
    again_form,
    box_subject,
    busy_store,
    card,
    checked_names,
    drafts,
    essay_answer,
    essay_row,
    first_button_of,
    first_form,
    followed,
    frozen_after_a_partial_save,
    gif,
    her_name_answered,
    here,
    homework_answer,
    homework_card,
    images_of,
    joined_field,
    lines_in,
    made,
    note_card,
    open_settings,
    opened_for_typing,
    png,
    press_outside,
    progress_on,
    read_form,
    read_review,
    review_form,
    says,
    says_each,
    shot,
    split_answers,
    state_of,
    stored_answers,
    stored_homework,
    token_of,
    uncertain_upload,
    units_on,
    unnamed,
    upload,
)
from tests.support import SCREENSHOT_NOW as NOW

READ_NOW = {"read": "now"}
AND_READ_SAID = (
    "Upload and read sends these screenshots to Blossom, then to Anthropic's API to be read."
)
WITHOUT_SAID = (
    "Upload without reading keeps them in Blossom; nothing is sent to Anthropic's API until "
    "you press Read."
)
"""The line beside each upload press, written out here so a change to its words fails here."""

UPLOADING = "Uploading the screenshots"
UPLOADING_AND_READING = "Uploading and reading the screenshots"
"""The busy words of Upload without reading, which are also the form's own, and of Upload and
read."""

SENDS_NOTHING = (
    "Choosing sends nothing. The chosen screenshots stay in this browser until you press an "
    "upload button below."
)
"""The server-written line beside each screenshot picker."""

UPLOAD_FORM = re.compile(
    r'<form method="post" action="(/parent/imports(?:/[0-9a-f]{32}/images)?)" '
    r'enctype="multipart/form-data"( data-pending="[^"]*")?>(.*?)</form>',
    re.S,
)


def prepared(browser: TestClient, draft_id: str, image_id: str) -> bytes:
    """The prepared PNG the store holds for one image: the bytes a read sends."""
    student = state_of(browser).project_state.student_id()
    return state_of(browser).import_drafts.image_png(student, draft_id, image_id)


def location_id(answer: Answer) -> str:
    found = re.fullmatch(r"/parent/imports/([0-9a-f]{32})", answer.headers["location"])
    assert found is not None, answer.text
    return found.group(1)


def upload_forms(page: str) -> list[str]:
    """Every screenshot upload form on a page, by its markup."""
    return [found.group(3) for found in UPLOAD_FORM.finditer(page)]


def upload_form_tags(page: str) -> list[str]:
    """The data-pending attribute of every screenshot upload form's tag on a page, or ""."""
    return [found.group(2) or "" for found in UPLOAD_FORM.finditer(page)]


def presses_of(form: str) -> list[str]:
    """The submit buttons of one form, by their markup."""
    return re.findall(r"<button [^>]*>.*?</button>", form, re.S)


# ------------------------------------------------------------------ C4: Upload without reading


def test_upload_without_reading_stores_and_reads_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No read field: the images are stored, the reader is called zero times, the note is the
    upload's own, and the review offers Read."""
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        sent = upload(browser, RED, BLUE)
        draft_id = location_id(sent)
        page = browser.get(sent.headers["location"]).text
        count = len(images_of(browser, draft_id))

    assert sent.status_code == 303
    assert count == 2
    assert reader.calls == []
    assert lines_in(TOLD.search(page)) == ["2 screenshots uploaded. None read yet."]
    assert read_form(page)["images"]


# ------------------------------------------------------------------ C5: Upload and read


def test_upload_and_read_reads_exactly_the_first_uploads_images(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first upload with read=now stores both images, then sends each prepared PNG once, and
    the review says what it uploaded and what it read."""
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        sent = upload(browser, RED, BLUE, form={**first_form(browser), **READ_NOW})
        draft_id = location_id(sent)
        stored = [prepared(browser, draft_id, image) for image in images_of(browser, draft_id)]
        page = browser.get(sent.headers["location"]).text

    assert sent.status_code == 303
    assert sorted(reader.calls) == sorted(stored)
    assert len(reader.calls) == 2
    told = lines_in(TOLD.search(page))
    assert told[0] == "2 screenshots uploaded."
    assert len(told) == 2
    assert told[1].startswith("2 screenshots read: ")


def test_upload_and_read_on_an_open_review_reads_only_the_new_image(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open review holds an earlier failed image and an earlier unread one; Upload and read
    of one new file sends that file's prepared PNG alone, and the earlier two stand as they
    were."""
    reader = Reader(monkeypatch)
    reader.answers[BLUE] = ScreenshotAnswer(None, "not_json", 10, 5)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, BLUE)
        failed = browser.post(
            f"/parent/imports/{draft_id}/read",
            data=read_form(browser.get(f"/parent/imports/{draft_id}").text),
        )
        assert failed.status_code == 200, failed.text
        to = f"/parent/imports/{draft_id}/images"
        assert upload(browser, RED, form={}, to=to).status_code == 303
        before = list(reader.calls)
        sent = upload(browser, GREEN, form=READ_NOW, to=to)
        ids = images_of(browser, draft_id)
        new_png = prepared(browser, draft_id, ids[2])
        page = browser.get(f"/parent/imports/{draft_id}").text

    assert before == [BLUE]
    assert sent.status_code == 303
    assert reader.calls[1:] == [new_png]
    assert says(page, f"Screenshot 1 · {imports.FAILED['not_json']}")
    assert says(page, f"Screenshot 2 · {imports.LABELS['unread']}")
    assert lines_in(TOLD.search(page))[0] == "1 more screenshot uploaded. 3 in this review, 1 read."


def test_upload_and_read_never_reads_a_duplicate_it_set_aside(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review holds RED unread; Upload and read of RED again and GREEN sets RED aside as a
    duplicate and sends GREEN alone."""
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        sent = upload(browser, RED, GREEN, form=READ_NOW, to=f"/parent/imports/{draft_id}/images")
        ids = images_of(browser, draft_id)
        green = prepared(browser, draft_id, ids[1])
        page = browser.get(f"/parent/imports/{draft_id}").text

    assert sent.status_code == 303
    assert len(ids) == 2
    assert reader.calls == [green]
    assert says(page, imports.image_refusal(1, "duplicate"))
    assert says(page, f"Screenshot 1 · {imports.LABELS['unread']}")


def test_upload_and_read_with_no_key_stores_reads_nothing_and_says_why(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale read=now with no key stores the images, makes no call, and says the no-key
    words."""
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path, key=False)) as browser:
        sent = upload(browser, RED, form={**first_form(browser), **READ_NOW})
        draft_id = location_id(sent)
        count = len(images_of(browser, draft_id))
        page = browser.get(sent.headers["location"]).text

    assert sent.status_code == 303
    assert count == 1
    assert reader.calls == []
    assert imports.SAID["no_key"] in lines_in(PROBLEM.search(page))


def test_upload_and_read_while_an_attempt_is_under_way_stores_and_says_until_when(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        student = state_of(browser).project_state.student_id()
        [image_id] = images_of(browser, draft_id)
        state_of(browser).import_drafts.start_attempt(student, draft_id, [image_id])
        sent = upload(browser, GREEN, form=READ_NOW, to=f"/parent/imports/{draft_id}/images")
        count = len(images_of(browser, draft_id))
        page = browser.get(f"/parent/imports/{draft_id}").text

    assert sent.status_code == 303
    assert count == 2
    assert reader.calls == []
    said = lines_in(PROBLEM.search(page))
    assert [line for line in said if line.startswith("Reading is under way until ")]


def test_upload_and_read_on_a_frozen_review_is_refused_as_today(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id = frozen_after_a_partial_save(browser, monkeypatch)
        reader = Reader(monkeypatch)
        more = upload(browser, GREEN, form=READ_NOW, to=f"/parent/imports/{draft_id}/images")
        form = {"token": token_of(browser, draft_id), "issued": first_form(browser)["issued"]}
        resent = upload(browser, GREEN, form={**form, **READ_NOW})
        count = len(images_of(browser, draft_id))

    assert more.status_code == 409
    assert says(more.text, imports.SAID["frozen"])
    assert resent.status_code == 409
    assert says(resent.text, imports.SAID["frozen"])
    assert count == 1
    assert reader.calls == []


def test_upload_and_read_with_every_file_refused_reads_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        first = upload(browser, gif(), form={**first_form(browser), **READ_NOW})
        made_none = drafts(browser)
        draft_id = made(browser, RED)
        more = upload(browser, gif(), form=READ_NOW, to=f"/parent/imports/{draft_id}/images")
        count = len(images_of(browser, draft_id))

    assert first.status_code == 422
    assert says(first.text, imports.NOTHING_ADDED)
    assert made_none == 0
    assert more.status_code == 303
    assert count == 1
    assert reader.calls == []


@pytest.mark.parametrize(
    "fields",
    [{"read": "yes"}, {"read": ""}, {"read": ["now", "now"]}],
    ids=["another value", "an empty value", "read sent twice"],
)
def test_a_read_field_out_of_its_rule_refuses_the_upload_whole(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, fields: dict[str, str]
) -> None:
    """A read value other than "now", or read sent twice, refuses the upload whole as one that
    couldn't be read: nothing stored, nothing read."""
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        first = upload(browser, RED, form={**first_form(browser), **fields})
        made_none = drafts(browser)
        draft_id = made(browser, BLUE)
        more = upload(browser, GREEN, form=fields, to=f"/parent/imports/{draft_id}/images")
        count = len(images_of(browser, draft_id))

    assert first.status_code == 400
    assert says(first.text, imports.UPLOAD_SAID["unreadable"])
    assert made_none == 0
    assert more.status_code == 400
    assert says(more.text, imports.UPLOAD_SAID["unreadable"])
    assert count == 1
    assert reader.calls == []


# ------------------------------------------------------------------ C3: the two presses


def assert_two_presses(form: str, *, typing: bool = False) -> None:
    """Upload and read first, primary, read=now, described by the line placed directly after
    it; Upload without reading second, secondary, with no name, described by its own line.
    Each carries its own busy words."""
    presses = presses_of(form)
    assert len(presses) == 2, form
    first, second = presses
    read = re.search(
        r'<button type="submit" class="primary" name="read" value="now" '
        rf'data-pending="{UPLOADING_AND_READING}" '
        r'aria-describedby="([a-z-]+)( typing-lost)?">Upload and read</button>\s*</div>\s*'
        r'<p class="note" id="([a-z-]+)">([^<]*)</p>',
        form,
    )
    assert read is not None, form
    assert read.group(1) == read.group(3)
    assert (read.group(2) is not None) is typing
    assert html.unescape(read.group(4)) == AND_READ_SAID
    assert first in read.group(0)
    kept = re.search(
        rf'<button type="submit" class="secondary" data-pending="{UPLOADING}" '
        r'aria-describedby="([a-z-]+)( typing-lost)?">Upload without reading</button>\s*</div>\s*'
        r'<p class="note" id="([a-z-]+)">([^<]*)</p>',
        form,
    )
    assert kept is not None, form
    assert kept.group(1) == kept.group(3)
    assert (kept.group(2) is not None) is typing
    assert html.unescape(kept.group(4)) == WITHOUT_SAID
    assert second in kept.group(0)
    assert 'name="read"' not in second


def test_family_review_offers_upload_and_read_and_upload_without_reading(
    tmp_path: pathlib.Path,
) -> None:
    with here(open_settings(tmp_path)) as browser:
        page = browser.get("/parent").text

    [form] = upload_forms(page)
    assert_two_presses(form)


def test_the_review_s_add_screenshots_offers_both_presses(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Reader(monkeypatch)
    reader.answers[RED] = ScreenshotAnswer(homework_answer(), None, 10, 5)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        plain = browser.get(f"/parent/imports/{draft_id}").text
        browser.post(f"/parent/imports/{draft_id}/read", data=read_form(plain))
        rows = browser.get(f"/parent/imports/{draft_id}").text

    [before] = upload_forms(plain)
    assert_two_presses(before)
    [after] = upload_forms(rows)
    assert_two_presses(after, typing=True)
    assert plain.index('action="/parent/imports/' + draft_id + '/read"') < plain.index(
        'action="/parent/imports/' + draft_id + '/images"'
    )


def test_the_retry_form_after_an_unknown_upload_offers_both_presses(
    tmp_path: pathlib.Path,
) -> None:
    with here(open_settings(tmp_path)) as browser:
        answer, _ = uncertain_upload(browser, RED, committed=False)

    [form] = upload_forms(answer.text)
    assert_two_presses(form)


def test_with_no_key_every_upload_form_offers_only_upload_without_reading(
    tmp_path: pathlib.Path,
) -> None:
    with here(open_settings(tmp_path, key=False)) as browser:
        family = browser.get("/parent").text
        draft_id = made(browser, RED)
        review = browser.get(f"/parent/imports/{draft_id}").text
        retry, _ = uncertain_upload(browser, BLUE, committed=False)

    forms = [*upload_forms(family), *upload_forms(review), *upload_forms(retry.text)]
    assert len(forms) == 3
    for form in forms:
        presses = presses_of(form)
        assert len(presses) == 1, form
        assert ">Upload without reading</button>" in presses[0]
        assert 'name="read"' not in form
        assert "Upload and read" not in form


# ------------------------------------------------------------------ C6: the review's Read press


def test_the_read_press_names_each_screenshot_it_sends_and_reads_exactly_those(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED read, BLUE failed, GREEN never read: the line beside Read names Screenshot 2 and
    Screenshot 3, and the press sends those two alone; a stored reading is never read again."""
    reader = Reader(monkeypatch)
    reader.answers[BLUE] = ScreenshotAnswer(None, "not_json", 10, 5)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED, BLUE)
        first = browser.get(f"/parent/imports/{draft_id}").text
        browser.post(f"/parent/imports/{draft_id}/read", data=read_form(first))
        upload(browser, GREEN, form={}, to=f"/parent/imports/{draft_id}/images")
        page = browser.get(f"/parent/imports/{draft_id}").text
        reader.calls.clear()
        reader.answers[BLUE] = ScreenshotAnswer(homework_answer(), None, 10, 5)
        pressed = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page))

    assert says(
        first,
        "Read sends Screenshot 1 and Screenshot 2 to Anthropic's API to be read. "
        "They aren't sent until you press Read.",
    )
    assert says(
        page,
        "Read sends Screenshot 2 and Screenshot 3 to Anthropic's API to be read. "
        "They aren't sent until you press Read.",
    )
    assert pressed.status_code == 200
    assert sorted(reader.calls) == sorted([BLUE, GREEN])


def test_the_read_line_names_three_or_more_screenshots_in_a_list(
    tmp_path: pathlib.Path,
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED, BLUE, GREEN, png(color=(9, 9, 9)))
        page = browser.get(f"/parent/imports/{draft_id}").text

    assert says(
        page,
        "Read sends Screenshot 1, Screenshot 2, Screenshot 3, and Screenshot 4 to Anthropic's "
        "API to be read.",
    )


# ------------------------------------------------------------------ C1 and C2: choosing


CHOSEN = re.compile(
    r'<input type="file" [^>]*>\s*</div>\s*'
    r'<p class="note">([^<]*)</p>\s*'
    r'<p class="note" role="status" data-chosen></p>\s*'
    r'<ul class="chosen-files" data-chosen-files hidden></ul>\s*'
    r'<div class="actions">'
)
"""The picker, then the line that says choosing sends nothing, the line the page script says
what was chosen in, and the empty list it fills, all before the first press."""


def upload_pages(browser: TestClient) -> list[str]:
    """Family review, a review's Add screenshots and the retry form after an unknown upload."""
    family = browser.get("/parent").text
    draft_id = made(browser, RED)
    review = browser.get(f"/parent/imports/{draft_id}").text
    retry, _ = uncertain_upload(browser, BLUE, committed=False)
    return [family, review, retry.text]


@pytest.mark.parametrize("key", [True, False], ids=["with a key", "with no key"])
def test_beside_each_picker_a_line_says_choosing_sends_nothing(
    tmp_path: pathlib.Path, key: bool
) -> None:
    """With scripts off the browser's own line shows what was chosen, and the server's line
    beside it says that choosing sends nothing; the status line and the list the page script
    fills follow it, on all three forms."""
    with here(open_settings(tmp_path, key=key)) as browser:
        pages = upload_pages(browser)

    forms = [form for page in pages for form in upload_forms(page)]
    assert len(forms) == 3
    for form in forms:
        found = CHOSEN.search(form)
        assert found is not None, form
        assert html.unescape(found.group(1)) == SENDS_NOTHING
        assert form.count("data-chosen-files") == 1
        assert form.count("data-chosen") == 2


@pytest.mark.parametrize("key", [True, False], ids=["with a key", "with no key"])
def test_each_upload_press_carries_its_own_busy_words(tmp_path: pathlib.Path, key: bool) -> None:
    """The form's own words are true of either press, for a browser that doesn't say which
    button was pressed; each press carries the words for what it does."""
    with here(open_settings(tmp_path, key=key)) as browser:
        pages = upload_pages(browser)

    tags = [tag for page in pages for tag in upload_form_tags(page)]
    assert tags == [f' data-pending="{UPLOADING}"'] * 3
    for form in (form for page in pages for form in upload_forms(page)):
        words = [re.search(r'data-pending="([^"]*)"', press) for press in presses_of(form)]
        assert [found.group(1) if found else None for found in words] == (
            [UPLOADING_AND_READING, UPLOADING] if key else [UPLOADING]
        )


SCRIPT = pathlib.Path(imports.__file__).parents[1] / "static" / "blossom.js"

NO_SENDING_NAMES = (
    "fetch",
    "XMLHttpRequest",
    "sendBeacon",
    "WebSocket",
    "EventSource",
    "requestSubmit",
    "importScripts",
    "Worker",
    "SharedWorker",
    "postMessage",
    "BroadcastChannel",
    "RTCPeerConnection",
    "Image",
    "Audio",
    "formAction",
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "srcset",
    "srcdoc",
)
"""Names the page script never holds, each matched as a whole name: a network call, a submit
call, an element that would fetch an address, or markup written as text."""

NO_SENDING_TEXT = (
    ".submit(",
    "navigator.",
    "window.open",
    "location.assign",
    "location.replace",
    "location.href",
    "location.reload",
    "location =",
    "location=",
    "import(",
    "document.write",
    ".action =",
    ".action=",
    'setAttribute("src"',
    'setAttribute("href"',
    'setAttribute("action"',
    "url(",
    "http:",
    "https:",
    '"//',
    "'//",
)
"""Text the page script never holds, matched as written."""

OBJECT_URL = """        var url = URL.createObjectURL(file);
        previews.push(url);
        preview.alt = "";
        preview.src = url;"""
"""The one address the script gives an element: an object URL made from a chosen file."""

ONE_FILE_PICKER = """        swap = document.createElement("input");
        swap.type = "file";
        swap.accept = picker.accept;
        swap.hidden = true;"""
"""The Replace picker: a file field with no name, so no form ever sends it."""


def held_by(script: str) -> list[str]:
    """Every name and text of the closed lists above that the script holds."""
    names = [
        name
        for name in NO_SENDING_NAMES
        if re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", script)
    ]
    return names + [text for text in NO_SENDING_TEXT if text in script]


def test_the_page_script_holds_no_network_call_and_no_submit_call() -> None:
    """A closed list refused on any match, never an interpretation of the script: a name is
    matched whole, so "fetched" in a comment is not "fetch", and the rest as written. Beside
    it, the script's one src is an object URL made from a chosen file, and its one .click(
    opens the Replace picker, a file field with no name."""
    script = SCRIPT.read_text(encoding="utf-8")

    assert held_by(script) == []
    assert script.count("src") == 1
    assert script.count(OBJECT_URL) == 1
    assert script.count(".click(") == 1
    assert script.count("      swap.click();\n") == 1
    assert script.count(ONE_FILE_PICKER) == 1
    assert "swap.name" not in script


def test_the_closed_lists_refuse_what_they_name() -> None:
    """Each name and text of the lists is refused where it stands in a line of script."""
    for name in NO_SENDING_NAMES:
        assert held_by(f"  var sent = {name}(x);\n") == [name]
    assert held_by("  // the page is fetched again\n") == []
    for text in NO_SENDING_TEXT:
        assert text in held_by(f"  x{text}y\n")


def lines_of_block(script: str, start: str, end: str) -> list[str]:
    """The stripped lines of the one block that begins with ``start``, up to ``end``."""
    assert script.count(start) == 1
    begun = script.index(start)
    return [line.strip() for line in script[begun : script.index(end, begun)].splitlines()]


def in_order(lines: list[str], required: tuple[str, ...]) -> list[int]:
    """Where each required statement stands in a block, each present exactly once."""
    assert [lines.count(statement) for statement in required] == [1] * len(required), lines
    return [lines.index(statement) for statement in required]


COPIES_THE_PRESS = (
    "var pressed = event.submitter;",
    "var words = pressed && pressed.dataset.pending",
    "? pressed.dataset.pending",
    ": form.dataset.pending;",
    "if (pressed) {",
    "if (pressed.name) {",
    'var kept = document.createElement("input");',
    'kept.type = "hidden";',
    "kept.name = pressed.name;",
    "kept.value = pressed.value;",
    'kept.setAttribute("data-pressed", "");',
    "form.appendChild(kept);",
    "button.disabled = true;",
    "pressed.textContent = words;",
    "say(form, words);",
    "document.title = words;",
)
"""The submit handler's statements, in order: the pressed button's name and value go into a
hidden field before any button is disabled, and its own busy words come before the form's.
Only a known press disables anything; the words show either way."""

LETS_GO_OF_THE_PRESS = (
    "button.disabled = false;",
    'var pressed = form.querySelector("input[data-pressed]");',
    "pressed.remove();",
)
"""What a page shown again undoes: the buttons held, and the hidden field of the press."""


def test_the_busy_text_handler_keeps_a_named_press_s_value() -> None:
    """A disabled button sends neither its name nor its value, so Upload and read would arrive
    as Upload without reading; the handler copies them first and removes the copy when the
    page is shown again."""
    script = SCRIPT.read_text(encoding="utf-8")
    handler = lines_of_block(
        script, '    form.addEventListener("submit", function (event) {\n', "\n    });\n"
    )
    reset = lines_of_block(script, "  function reset(form) {\n", "\n  }\n")

    places = in_order(handler, COPIES_THE_PRESS)
    assert places == sorted(places)
    assert "button.textContent = form.dataset.pending;" not in handler
    assert in_order(reset, LETS_GO_OF_THE_PRESS)


RELEASE = """    function release() {
      previews.forEach(function (url) {
        URL.revokeObjectURL(url);
      });
      previews = [];
    }"""
"""How the list lets go of every preview it made."""


def test_the_chosen_list_lets_go_of_its_previews_and_inserts_text_only() -> None:
    """Every rebuild of the list (a new choice, a Remove, a Replace, a page shown again) starts
    by revoking the previews it made, and so does leaving the page; names reach the page only
    as text."""
    script = SCRIPT.read_text(encoding="utf-8")

    assert script.count(RELEASE) == 1
    assert script.count("    function show(said) {\n      release();\n") == 1
    assert script.count('    window.addEventListener("pagehide", release);\n') == 1
    assert script.count('form.querySelector("[data-chosen-files]")') == 1
    assert script.count("name.textContent = file.name;") == 1
    assert script.count('whose.textContent = " " + file.name;') == 1
    assert script.count('"To change what you chose, choose again."') == 1


LOCK: Final = """    function lock(holding) {
      locked = holding;
      picker.disabled = holding;
      if (swap) {
        swap.disabled = holding;
      }
      Array.prototype.forEach.call(list ? list.querySelectorAll("button") : [], function (button) {
        button.disabled = holding;
      });
    }"""
"""How the list holds, or hands back, its picker, its Replace picker and each file's Remove and
Replace."""

TAKEN: Final = """    form.addEventListener("formdata", function () {
      if (form.dataset.busy) {
        lock(true);
      }
    });"""
"""Where the list is held: as the browser builds what the submission sends, the submit having
gone ahead (the busy-text handler marks the form busy then)."""

HAND_BACK: Final = """    function handBack() {
      lock(false);
      show("");
    }"""
"""What a page shown again does first: hands the controls back, then lists what is chosen."""

HELD_STEPS: Final = ("remove(place)", "replace(place)", "swapped()", "chose()")
"""Each step of the list that changes what is chosen or what the page says of it."""


def test_an_upload_under_way_holds_the_list_and_its_pickers_once_the_files_are_taken() -> None:
    """A disabled field sends nothing, so the picker is never disabled before the browser has
    built what the submission sends: the list is held from the form's formdata event, which a
    browser fires while it builds those entries, after the submit went ahead and with the
    picker's files already among them, and only on a form the busy-text handler marked busy.
    The submit handler holds nothing. Once held, no step of the list changes what is chosen
    or what the page says of it."""
    script = SCRIPT.read_text(encoding="utf-8")
    handler = lines_of_block(
        script, '    form.addEventListener("submit", function (event) {\n', "\n    });\n"
    )

    assert script.count(LOCK) == 1
    assert script.count(TAKEN) == 1
    assert script.count('addEventListener("formdata"') == 1
    assert script.count("lock(true);") == 1
    assert script.count("picker.disabled") == 1
    assert script.count("swap.disabled") == 1
    assert script.count("    var locked = false;\n") == 1
    assert [line for line in handler if "lock(" in line or "picker" in line] == []
    for step in HELD_STEPS:
        opening = f"    function {step} {{\n      if (locked) {{\n        return;\n      }}\n"
        assert script.count(opening) == 1, step


def test_a_page_shown_again_hands_the_list_and_its_pickers_back() -> None:
    """Every showing of the page, a first one, one restored from the browser's cache or one
    fetched again by Back, hands the controls back before the list is drawn again; the busy
    handler's own reset hands its buttons back as before."""
    script = SCRIPT.read_text(encoding="utf-8")

    assert script.count(HAND_BACK) == 1
    assert script.count("lock(false);") == 1
    assert script.count('    window.addEventListener("pageshow", handBack);\n') == 1
    assert 'addEventListener("pageshow", chose)' not in script
    assert script.index(LOCK) < script.index(HAND_BACK) < script.index(TAKEN)


# ------------------------------------------------------------------ C8: how each screenshot stands

SHOT_LINE = re.compile(r"<p>(Screenshot \d+ · [^<]*)</p>")
READ_FOLD = re.compile(r'<details class="screenshot-fold"[^>]*>.*?</details>', re.S)
NAME_PANEL = re.compile(
    r'<section class="panel" aria-labelledby="name-heading">.*?</section>', re.S
)

WITH_HOMEWORK = "Read. 2 assignments found."
"""How a screenshot read as the stand-in's homework list stands: two cards, two assignments."""
NOTHING_FOUND = "Read, but no homework was found on it."
TEXT_ONLY = "Read, but none of it became homework: 2 cards couldn't be used."
WITH_A_CUT_OFF_CARD = (
    "Read, but none of it became homework: 1 card couldn't be used, 1 cut off at the "
    "screenshot's edge."
)
"""How a homework list stands with no cards; with two cards that couldn't become homework; and
with one of those and one the reader marked cut off."""

SAVE_FIXES = (
    "After a save, this review's screenshots can't be replaced or removed, so replace or remove "
    "Screenshot 2 before saving."
)
"""The line beside Save while Screenshot 2 needs attention and nothing is saved."""
AFTER_A_SAVE = (
    "To fix it, add the right screenshot in a new upload, or enter its homework by hand from "
    "Family review. Finish without the rest closes this review."
)
"""What a screenshot needing attention says once some homework from its review is saved."""


def where_each_stands(page: str) -> dict[str, str]:
    """Each screenshot's line as the review says it, and where it shows: "fold" inside the fold
    of screenshots read, "above" outside it and before the review form (or on a page with no
    form), "below" outside it and after the form."""
    folded = "".join(READ_FOLD.findall(page))
    form_at = page.find('id="review-form"')
    out = {}
    for found in SHOT_LINE.finditer(page):
        if found.group(0) in folded:
            where = "fold"
        elif form_at == -1 or found.start() < form_at:
            where = "above"
        else:
            where = "below"
        out[html.unescape(found.group(1))] = where
    return out


def text_only() -> dict[str, Any]:
    """A homework list whose two cards couldn't become homework: one with no title, and one
    with no date on it or in a heading above it."""
    return homework_answer(
        headings=[],
        cards=[
            homework_card(title=None),
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


def cut_off() -> dict[str, Any]:
    """A homework list with one card with no title, and one the reader marked cut off."""
    return homework_answer(
        headings=[],
        cards=[
            homework_card(title=None),
            homework_card(title="Lab Report", clipped=True, y_top=450, y_bottom=520),
        ],
    )


ATTENTION: dict[str, tuple[dict[str, Any], str]] = {
    "a homework list with no cards": (homework_answer(cards=[]), NOTHING_FOUND),
    "only cards that couldn't become homework": (text_only(), TEXT_ONLY),
    "a card cut off at its edge": (cut_off(), WITH_A_CUT_OFF_CARD),
    "not a homework list": (
        homework_answer(image_kind="unsupported"),
        imports.LABELS["wrong_kind"],
    ),
    "a grade report": (
        homework_answer(image_kind="grade_report", headings=[], cards=[]),
        imports.LABELS["wrong_kind"],
    ),
}
"""Each stored reading that leaves a screenshot needing attention, and how its row says so."""


@pytest.mark.parametrize("case", list(ATTENTION))
def test_a_read_screenshot_needing_attention_says_why_above_the_review(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Screenshot 1 is read with homework: it folds below the review, with its own counts.
    Screenshot 2 needs attention: its row says why in its own words and stays in view above the
    review form, with its Remove press."""
    answer, said = ATTENTION[case]
    with here(open_settings(tmp_path)) as browser:
        draft_id, _ = read_review(browser, monkeypatch, (RED, homework_answer()), (BLUE, answer))
        page = browser.get(f"/parent/imports/{draft_id}").text
        [_, blue] = images_of(browser, draft_id)

    assert where_each_stands(page) == {
        f"Screenshot 1 · {WITH_HOMEWORK}": "fold",
        f"Screenshot 2 · {said}": "above",
    }
    assert -1 < page.find(f'form="remove-shot-{blue}"') < page.find('id="review-form"')


STAND_IN_FAILURES: dict[str, ScreenshotAnswer] = {
    **{kind: ScreenshotAnswer(None, kind, 10, 5) for kind in get_args(ReadFailure)},
    "not_valid": ScreenshotAnswer({"image_kind": "homework_list"}, None, 10, 5),
}
"""Each failure the stand-in reader gives: one per failure a reading call returns, and an answer
that doesn't fit the schema."""


@pytest.mark.parametrize("failure", list(STAND_IN_FAILURES))
def test_a_screenshot_whose_read_failed_says_why_above_the_review(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Each failure the stand-in reader can give keeps its own words on Screenshot 2's row,
    above the review form, and the Read press offers that screenshot again."""
    reader = Reader(monkeypatch)
    reader.answers[BLUE] = STAND_IN_FAILURES[failure]
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED, BLUE)
        page = browser.get(f"/parent/imports/{draft_id}").text
        browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page))
        page = browser.get(f"/parent/imports/{draft_id}").text
        [_, blue] = images_of(browser, draft_id)

    assert where_each_stands(page) == {
        f"Screenshot 1 · {WITH_HOMEWORK}": "fold",
        f"Screenshot 2 · {imports.FAILED[failure]}": "above",
    }
    assert read_form(page)["images"] == blue


def test_a_lone_screenshot_with_nothing_found_is_never_folded_away(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no rows to review, a homework list with no cards says so at the top, outside any
    fold, with its Remove press."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, _ = read_review(browser, monkeypatch, (RED, homework_answer(cards=[])))
        page = browser.get(f"/parent/imports/{draft_id}").text
        [red] = images_of(browser, draft_id)

    assert where_each_stands(page) == {f"Screenshot 1 · {NOTHING_FOUND}": "above"}
    assert "screenshot-fold" not in page
    assert f'form="remove-shot-{red}"' in page


def test_each_screenshot_read_with_homework_counts_its_own_assignments_and_notes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Screenshot 2 holds one card of its own and a teacher's note: its row counts those alone,
    and both screenshots fold below the review."""
    note = homework_card(
        badge=None,
        course="Humanities 8",
        title="The class project is due today.",
        instruction=None,
        footer=None,
        y_top=360,
        y_bottom=440,
    )
    second = homework_answer(cards=[homework_card(title="Lab Report"), note])
    with here(open_settings(tmp_path)) as browser:
        draft_id, _ = read_review(browser, monkeypatch, (RED, homework_answer()), (BLUE, second))
        page = browser.get(f"/parent/imports/{draft_id}").text

    assert where_each_stands(page) == {
        f"Screenshot 1 · {WITH_HOMEWORK}": "fold",
        "Screenshot 2 · Read. 1 assignment found, 1 note to check.": "fold",
    }


# ------------------------------------------------------------------ C9: a missing name apart


def test_the_missing_name_question_leaves_out_a_screenshot_with_nothing_to_save(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Screenshot 1 has rows and no name; Screenshot 2 has no cards and no name. The question
    counts and names Screenshot 1 only, in the Student name panel; Screenshot 2's own row says it
    has no homework, and no screenshot's row speaks of a name."""
    nameless_and_empty = homework_answer(active_student={"name": None, "uncertain": None}, cards=[])
    with here(open_settings(tmp_path)) as browser:
        draft_id, _ = read_review(
            browser,
            monkeypatch,
            (RED, unnamed("Sentence Composing")),
            (BLUE, nameless_and_empty),
        )
        page = browser.get(f"/parent/imports/{draft_id}").text

    panel = NAME_PANEL.search(page)
    assert panel is not None
    assert says(
        panel.group(0),
        "No student name was found on 1 screenshot. Confirm these are her homework before saving.",
    )
    assert says(panel.group(0), "Screenshot 1 shows no student name.")
    assert "Screenshot 2" not in panel.group(0)
    assert where_each_stands(page) == {
        "Screenshot 1 · Read. 1 assignment found.": "fold",
        f"Screenshot 2 · {NOTHING_FOUND}": "above",
    }
    assert not any("name" in line for line in where_each_stands(page))


def test_the_missing_name_question_names_every_screenshot_it_covers(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, _ = read_review(
            browser,
            monkeypatch,
            (RED, unnamed("Sentence Composing")),
            (BLUE, unnamed("Lab Report")),
        )
        page = browser.get(f"/parent/imports/{draft_id}").text

    panel = NAME_PANEL.search(page)
    assert panel is not None
    assert says(panel.group(0), "No student name was found on 2 screenshots.")
    assert says(panel.group(0), "Screenshot 1 and Screenshot 2 show no student name.")


NO_NAME: dict[str, Any] = {"name": None, "uncertain": None}
CONFIRMED = "answer:name:missing"


def name_panel(page: str) -> str:
    panel = NAME_PANEL.search(page)
    assert panel is not None, page
    return panel.group(0)


@pytest.mark.parametrize("how", ["replace and read", "upload and read"])
def test_a_nameless_screenshot_with_nothing_to_save_keeps_the_confirmation(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """Screenshot 1 shows no name and has a row, and her homework is confirmed; Screenshot 2
    failed. A homework list showing no name and holding no card, read in Screenshot 2's place or
    added and read beside it, has nothing to save: the confirmation stays stored and checked, the
    page doesn't ask for it again, and the question still names Screenshot 1 alone."""
    reader = Reader(monkeypatch)
    reader.answers[RED] = ScreenshotAnswer(unnamed("Sentence Composing"), None, 10, 5)
    reader.answers[BLUE] = ScreenshotAnswer(None, "not_json", 10, 5)
    reader.answers[GREEN] = ScreenshotAnswer(
        homework_answer(active_student=NO_NAME, cards=[]), None, 10, 5
    )
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED, BLUE)
        page = browser.get(f"/parent/imports/{draft_id}").text
        page = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page)).text
        followed(
            browser,
            send(browser, draft_id, page_post(page, "keep", choose={CONFIRMED: "confirmed"})),
        )
        held = stored_answers(browser, draft_id)
        [_, blue] = images_of(browser, draft_id)
        if how == "replace and read":
            sent = replace_with(browser, draft_id, blue, GREEN, form=READ_NOW)
        else:
            sent = upload(browser, GREEN, form=READ_NOW, to=f"/parent/imports/{draft_id}/images")
        page = followed(browser, sent)
        after = stored_answers(browser, draft_id)

    assert held[("name", "missing")] == "confirmed"
    assert sent.status_code == 303, sent.text
    assert says(page, NOTHING_FOUND)
    assert after.get(("name", "missing")) == "confirmed"
    assert not says(page, ASK_NAME_AGAIN)
    assert CONFIRMED in checked_names(page)
    assert says(name_panel(page), "No student name was found on 1 screenshot.")
    assert says(name_panel(page), "Screenshot 1 shows no student name.")


def nameless_essay_and_note() -> tuple[dict[str, Any], dict[str, Any]]:
    """The essay card in a screenshot showing no name, and the note that joins it in another
    screenshot showing no name."""
    card = essay_answer()["cards"][0]  # type: ignore[index]
    note = note_card("Religion 8", ESSAY_NOTE, 300)
    return (
        homework_answer(cards=[card], active_student=NO_NAME),
        homework_answer(cards=[note], active_student=NO_NAME),
    )


def test_the_missing_name_question_covers_a_joined_note_s_screenshot_before_it_is_ticked(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Screenshot 1 holds the essay card and Screenshot 2 only the note that joins it, neither
    showing a name. Before the note is ticked, the question already counts and names both
    screenshots, so the confirmation given there covers the note's screenshot too; ticking the
    note then saves once with it."""
    card, note = nameless_essay_and_note()
    with here(open_settings(tmp_path)) as browser:
        draft_id, _ = read_review(browser, monkeypatch, (RED, card), (BLUE, note))
        shown = browser.get(f"/parent/imports/{draft_id}").text
        confirmed = followed(
            browser,
            send(browser, draft_id, page_post(shown, "keep", choose={CONFIRMED: "confirmed"})),
        )
        choose = {joined_field(confirmed): "1"}
        saved = followed(
            browser,
            send(
                browser,
                draft_id,
                page_post(confirmed, "save", choose=choose, tick=units_on(confirmed)),
            ),
        )
        _, _, words = essay_row(browser)

    assert says(saved, imports.counted(1, 0, 0, 0))
    assert words == [ESSAY_NOTE]
    assert says(name_panel(shown), "No student name was found on 2 screenshots.")
    assert says(name_panel(shown), "Screenshot 1 and Screenshot 2 show no student name.")
    assert JOINED_TICK.findall(review_form(shown)) == [""]


def test_a_joined_note_s_nameless_screenshot_added_after_the_confirmation_asks_for_it_again(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Screenshot 1 holds the essay card with no name, and her homework is confirmed. Then a
    screenshot showing no name and holding only the note that joins it is added and read: the
    question gains a screenshot, so the confirmation is cleared and asked for again, naming both
    screenshots."""
    card, note = nameless_essay_and_note()
    reader = Reader(monkeypatch)
    reader.answers[RED] = ScreenshotAnswer(card, None, 10, 5)
    reader.answers[BLUE] = ScreenshotAnswer(note, None, 10, 5)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        page = browser.get(f"/parent/imports/{draft_id}").text
        page = browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page)).text
        followed(
            browser,
            send(browser, draft_id, page_post(page, "keep", choose={CONFIRMED: "confirmed"})),
        )
        held = stored_answers(browser, draft_id)
        sent = upload(browser, BLUE, form=READ_NOW, to=f"/parent/imports/{draft_id}/images")
        page = followed(browser, sent)
        after = stored_answers(browser, draft_id)

    assert held[("name", "missing")] == "confirmed"
    assert sent.status_code == 303, sent.text
    assert ("name", "missing") not in after
    assert says(page, ASK_NAME_AGAIN)
    assert CONFIRMED not in checked_names(page)
    assert says(name_panel(page), "Screenshot 1 and Screenshot 2 show no student name.")


# ------------------------------------------------------------------ C13: the frozen rule, said


def test_before_a_save_the_line_beside_save_says_saving_fixes_the_screenshots(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While Screenshot 2 needs attention and nothing is saved, the line sits beside Save, after
    the typing note and before the press."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, _ = read_review(
            browser, monkeypatch, (RED, homework_answer()), (BLUE, homework_answer(cards=[]))
        )
        page = her_name_answered(browser, draft_id, browser.get(f"/parent/imports/{draft_id}").text)

    line = page.find(str(escape(SAVE_FIXES)))
    assert page.count(str(escape(SAVE_FIXES))) == 1
    assert page.find('id="typing-lost"') < line < page.find('name="press" value="save"')
    assert not says(page, AFTER_A_SAVE)


def test_with_every_screenshot_read_with_homework_no_line_sits_beside_save(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id, _ = read_review(browser, monkeypatch, (RED, homework_answer()))
        page = her_name_answered(browser, draft_id, browser.get(f"/parent/imports/{draft_id}").text)

    assert 'name="press" value="save"' in page
    assert "After a save, this review" not in page


def test_after_a_save_a_screenshot_needing_attention_says_what_can_be_done(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once some homework is saved, the screenshots stay as they are: Screenshot 2 says how to
    fix it elsewhere, Screenshot 1 doesn't, nothing offers Remove, and the line beside Save is
    gone."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, _ = read_review(
            browser, monkeypatch, (RED, homework_answer()), (BLUE, homework_answer(cards=[]))
        )
        page = her_name_answered(browser, draft_id, browser.get(f"/parent/imports/{draft_id}").text)
        send(browser, draft_id, page_post(page, "save", tick=units_on(page)[:1]))
        page = browser.get(f"/parent/imports/{draft_id}").text

    assert says(page, imports.SAID["frozen"])
    assert page.count(str(escape(AFTER_A_SAVE))) == 1
    assert where_each_stands(page)[f"Screenshot 2 · {NOTHING_FOUND}"] == "above"
    assert 'form="remove-shot-' not in page
    assert "After a save, this review" not in page
    assert 'form="finish"' in page


# ------------------------------------------------------------------ C14: no silent reread


def test_no_press_but_a_read_calls_the_reader(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After one Read, a visit, a reopen, Keep these answers, Save review for later, Save and
    Finish without the rest call the stand-in reader zero times, the failed screenshot
    included."""
    reader = Reader(monkeypatch)
    reader.answers[BLUE] = ScreenshotAnswer(None, "not_json", 10, 5)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED, BLUE)
        page = browser.get(f"/parent/imports/{draft_id}").text
        browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page))
        read_once = sorted(reader.calls)
        browser.get(f"/parent/imports/{draft_id}")
        page = browser.get(f"/parent/imports/{draft_id}").text
        kept = followed(browser, send(browser, draft_id, page_post(page, "keep")))
        later = followed(browser, send(browser, draft_id, page_post(kept, "later")))
        page = her_name_answered(browser, draft_id, later)
        saved = followed(
            browser, send(browser, draft_id, page_post(page, "save", tick=units_on(page)[:1]))
        )
        finished = press_outside(browser, saved, "finish")
        student = state_of(browser).project_state.student_id()
        closed = state_of(browser).import_drafts.snapshot(student, draft_id).closed_at

    assert read_once == sorted([RED, BLUE])
    assert sorted(reader.calls) == read_once
    assert finished.status_code == 200
    assert closed is not None


# ------------------------------------------------------------------ C10: replace one screenshot

REPLACE_AND_READ_SAID = (
    "Replace and read sends this screenshot to Blossom, then to Anthropic's API to be read."
)
REPLACE_WITHOUT_SAID = (
    "Replace without reading keeps it in Blossom; nothing is sent to Anthropic's API until you "
    "press Read."
)
"""The line beside each press of a screenshot's replace step."""
REPLACE_SENDS_NOTHING = (
    "Choosing sends nothing. The chosen screenshot stays in this browser until you press a "
    "replace button below."
)
KEPT_AS_IT_WAS = "Screenshot 2 wasn't replaced; it stays as it was."
REPLACED = "Screenshot 2 was replaced."
BIG = b"\x89PNG\r\n\x1a\n" + bytes(12 * 1_000_000)
"""A file just over the 12 MB a screenshot may be."""


def replace_path(draft_id: str, image_id: str) -> str:
    return f"/parent/imports/{draft_id}/images/{image_id}/replace"


def replace_with(
    browser: TestClient,
    draft_id: str,
    image_id: str,
    *images: bytes,
    form: dict[str, str] | None = None,
) -> Answer:
    """A press of the replace step with these files chosen, as a browser sends its one-file
    form; Replace and read adds read=now."""
    return upload(browser, *images, form=form or {}, to=replace_path(draft_id, image_id))


def one_read_one_failed(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch, reader: Reader
) -> tuple[str, str, str]:
    """A review whose Screenshot 1 (RED) is read with homework and whose Screenshot 2 (BLUE)
    failed: its id, and the two image ids."""
    reader.answers[BLUE] = ScreenshotAnswer(None, "not_json", 10, 5)
    draft_id = made(browser, RED, BLUE)
    page = browser.get(f"/parent/imports/{draft_id}").text
    browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page))
    red, blue = images_of(browser, draft_id)
    return draft_id, red, blue


def edited(browser: TestClient, draft_id: str) -> str:
    """The family's edits on the review: its answers kept with Keep these answers, its name
    answered, a title corrected and one group ticked, saved with Save review for later. The
    page after."""
    page = browser.get(f"/parent/imports/{draft_id}").text
    kept = followed(browser, send(browser, draft_id, page_post(page, "keep")))
    page = her_name_answered(browser, draft_id, kept)
    box = f"correct:{box_subject(page, 'title')}"
    typed = {box: "Sentence Composing, corrected"}
    page = opened_for_typing(browser, draft_id, page, typed)
    return followed(
        browser,
        send(browser, draft_id, page_post(page, "later", typed=typed, tick=units_on(page)[:1])),
    )


def kept_state(browser: TestClient, draft_id: str, image_id: str) -> dict[str, object]:
    """What the replace must leave alone: one image's id and place, every reading but the
    replaced one's, the stored answers, the saved progress and the expiry."""
    student = state_of(browser).project_state.student_id()
    snapshot = state_of(browser).import_drafts.snapshot(student, draft_id)
    [kept] = [item for item in snapshot.images if item.image_id == image_id]
    return {
        "image": (kept.image_id, snapshot.images.index(kept), kept.sha256),
        "readings": [r for r in snapshot.readings if r.sha256 == kept.sha256],
        "answers": stored_answers(browser, draft_id),
        "progress": snapshot.progress,
        "expires_at": snapshot.expires_at,
    }


def test_replace_without_reading_keeps_the_other_screenshot_and_the_family_s_edits(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Screenshot 1 is read, answered, named, corrected and ticked; Screenshot 2 failed. Replace
    without reading puts the new file at place 2, unread, and leaves Screenshot 1's image,
    reading, answers, name answer, saved corrections and ticks and the review's expiry as they
    were, with zero reader calls."""
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id, red, blue = one_read_one_failed(browser, monkeypatch, reader)
        later = edited(browser, draft_id)
        answers = stored_answers(browser, draft_id)
        before = kept_state(browser, draft_id, red)
        calls = list(reader.calls)
        sent = replace_with(browser, draft_id, blue, GREEN)
        page = followed(browser, sent)
        after = kept_state(browser, draft_id, red)
        ids = images_of(browser, draft_id)

    assert says(later, imports.PRESS_SAID["later"])
    assert "Sentence Composing, corrected" in str(before["progress"])
    assert '"selected"' in str(before["progress"])
    assert ("name", "hers") in {(kind, value) for (kind, _), value in answers.items()}
    assert any(kind == "destination" for kind, _ in answers)
    assert sent.status_code == 303, sent.text
    assert sent.headers["location"] == f"/parent/imports/{draft_id}"
    assert after == before
    assert ids[0] == red
    assert ids[1] != blue
    assert len(ids) == 2
    assert reader.calls == calls
    assert lines_in(TOLD.search(page)) == [REPLACED]
    assert PROBLEM.search(page) is None
    assert says(page, f"Screenshot 2 · {imports.LABELS['unread']}")


def test_replacing_a_screenshot_read_with_homework_clears_and_names_its_own_answers(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Screenshot 2 was read with its own card and answered: replacing it clears that card's
    answers and the review names them; Screenshot 1's answers stay."""
    vocab = homework_answer(
        cards=[homework_card(title="Vocab Quiz", instruction="Study list one.")]
    )
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(browser, monkeypatch, (RED, homework_answer()), (BLUE, vocab))
        followed(browser, send(browser, draft_id, page_post(page, "keep")))
        before = stored_answers(browser, draft_id)
        [_, blue] = images_of(browser, draft_id)
        sent = replace_with(browser, draft_id, blue, GREEN)
        page = followed(browser, sent)
        after = stored_answers(browser, draft_id)

    assert sent.status_code == 303, sent.text
    assert says(page, f"{CLEARED_INTRO} Vocab Quiz. Answer them again.")
    assert lines_in(TOLD.search(page)) == [REPLACED]
    assert after
    assert after == {key: value for key, value in before.items() if key in after}
    assert len(after) < len(before)


def test_replace_and_read_sends_the_new_file_and_nothing_else(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stand-in reader receives exactly the new file's prepared PNG, once; Screenshot 1's
    reading is unchanged and the failed file is never sent again."""
    reader = Reader(monkeypatch)
    lab = homework_answer(cards=[homework_card(title="Lab Report")])
    reader.answers[GREEN] = ScreenshotAnswer(lab, None, 10, 5)
    with here(open_settings(tmp_path)) as browser:
        draft_id, red, blue = one_read_one_failed(browser, monkeypatch, reader)
        before = kept_state(browser, draft_id, red)
        calls = list(reader.calls)
        sent = replace_with(browser, draft_id, blue, GREEN, form=READ_NOW)
        ids = images_of(browser, draft_id)
        new_png = prepared(browser, draft_id, ids[1])
        after = kept_state(browser, draft_id, red)
        page = followed(browser, sent)

    assert sent.status_code == 303, sent.text
    assert reader.calls[: len(calls)] == calls
    assert reader.calls[len(calls) :] == [new_png]
    assert after == before
    told = lines_in(TOLD.search(page))
    assert told[0] == REPLACED
    assert told[1].startswith("2 screenshots read: ")
    assert says(page, "Screenshot 2 · Read. 1 assignment found.")
    assert says(page, f"Screenshot 1 · {WITH_HOMEWORK}")


def test_a_new_file_that_copies_a_card_of_the_other_screenshot_follows_the_identical_cards_rule(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Screenshot 1's lone card is answered; the new file read in place of the failed
    Screenshot 2 holds the same card, so the existing rule clears that card's answers and says
    the copies can't be told apart."""
    lone = homework_answer(cards=[homework_card()])
    reader = Reader(monkeypatch)
    reader.answers[RED] = ScreenshotAnswer(lone, None, 10, 5)
    reader.answers[GREEN] = ScreenshotAnswer(lone, None, 10, 5)
    with here(open_settings(tmp_path)) as browser:
        draft_id, _, blue = one_read_one_failed(browser, monkeypatch, reader)
        page = browser.get(f"/parent/imports/{draft_id}").text
        followed(browser, send(browser, draft_id, page_post(page, "keep")))
        kept = [kind for kind, _ in stored_answers(browser, draft_id)]
        sent = replace_with(browser, draft_id, blue, GREEN, form=READ_NOW)
        page = followed(browser, sent)
        left = [kind for kind, _ in stored_answers(browser, draft_id)]

    assert "destination" in kept
    assert "destination" not in left
    assert says(page, f"{CLEARED_INTRO} Sentence Composing. Answer them again.")
    assert says(page, screenshot_page.CHANGE_SAID["copies"])


REFUSED_FILES: dict[str, tuple[tuple[bytes, ...], int, str]] = {
    "a GIF": ((gif(),), 422, "The new file wasn't added: it isn't a PNG or JPEG."),
    "over 12 MB": ((BIG,), 413, imports.UPLOAD_SAID["file_too_large"]),
    "a copy of Screenshot 1": (
        (RED,),
        422,
        "The new file is already in this review as Screenshot 1.",
    ),
    "the same as Screenshot 2": ((BLUE,), 422, "The new file is the same as Screenshot 2."),
    "no file": ((), 400, "Choose one screenshot to put in its place."),
    "two files": (
        (GREEN, png(color=(9, 9, 9))),
        400,
        "Choose one screenshot only; more than one was sent.",
    ),
}
"""Each new file the replace step refuses: the files sent, the answer's status, and why."""


@pytest.mark.parametrize("case", list(REFUSED_FILES))
@pytest.mark.parametrize("read", [False, True], ids=["without reading", "and read"])
def test_a_refused_new_file_keeps_the_old_one_and_says_why(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, case: str, read: bool
) -> None:
    files, status, why = REFUSED_FILES[case]
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id, red, blue = one_read_one_failed(browser, monkeypatch, reader)
        calls = list(reader.calls)
        before = kept_state(browser, draft_id, red)
        path = replace_path(draft_id, blue)
        if files:
            sent = replace_with(browser, draft_id, blue, *files, form=READ_NOW if read else None)
        else:
            # A browser sends a picker left empty as a file part with an empty name.
            field = (
                '--b\r\nContent-Disposition: form-data; name="read"\r\n\r\nnow\r\n' if read else ""
            )
            body = (
                f"{field}--b\r\n"
                'Content-Disposition: form-data; name="screenshots"; filename=""\r\n'
                "Content-Type: application/octet-stream\r\n\r\n\r\n"
                "--b--\r\n"
            )
            sent = browser.post(
                path,
                content=body.encode(),
                headers={"content-type": "multipart/form-data; boundary=b"},
            )
        ids = images_of(browser, draft_id)
        after = kept_state(browser, draft_id, red)

    assert sent.status_code == status
    assert ids == [red, blue]
    assert after == before
    assert reader.calls == calls
    problem = PROBLEM.search(sent.text)
    assert lines_in(problem) == [KEPT_AS_IT_WAS, why]
    assert f'action="{path}"' in sent.text


def test_a_frozen_review_offers_no_replace_and_refuses_one(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with here(open_settings(tmp_path)) as browser:
        draft_id = frozen_after_a_partial_save(browser, monkeypatch)
        [red] = images_of(browser, draft_id)
        page = browser.get(f"/parent/imports/{draft_id}").text
        step = browser.get(replace_path(draft_id, red))
        sent = replace_with(browser, draft_id, red, GREEN)
        ids = images_of(browser, draft_id)

    assert "/replace" not in page
    assert step.status_code == 409
    assert says(step.text, imports.SAID["frozen"])
    assert sent.status_code == 409
    assert says(sent.text, imports.SAID["frozen"])
    assert ids == [red]


def test_an_unknown_screenshot_or_review_is_refused_as_today(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An image not in the review: the step answers as View screenshot does and the press as
    Remove does; a review that isn't there: the missing review's page."""
    unknown = "0" * 32
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED, BLUE)
        step = browser.get(replace_path(draft_id, unknown))
        sent = replace_with(browser, draft_id, unknown, GREEN)
        page = followed(browser, sent)
        nowhere = browser.get(replace_path(unknown, unknown))
        pressed = replace_with(browser, unknown, unknown, GREEN)
        count = len(images_of(browser, draft_id))

    assert step.status_code == 404
    assert says(step.text, imports.SAID["image_gone"])
    assert sent.status_code == 303
    assert lines_in(PROBLEM.search(page)) == [imports.SAID["image_gone"]]
    assert (nowhere.status_code, pressed.status_code) == (404, 404)
    assert says(nowhere.text, imports.SAID["absent"])
    assert says(pressed.text, imports.SAID["absent"])
    assert count == 2


def test_a_review_holding_ten_screenshots_can_replace_one(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ten = [png(color=(n * 20, 7, 7)) for n in range(10)]
    Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, *ten)
        before = images_of(browser, draft_id)
        sent = replace_with(browser, draft_id, before[4], GREEN)
        after = images_of(browser, draft_id)
        page = followed(browser, sent)

    assert sent.status_code == 303, sent.text
    assert len(after) == 10
    assert after[:4] == before[:4]
    assert after[5:] == before[5:]
    assert after[4] not in before
    assert lines_in(TOLD.search(page)) == ["Screenshot 5 was replaced."]


def test_with_no_key_a_stale_replace_and_read_replaces_and_reads_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path, key=False)) as browser:
        draft_id = made(browser, RED, BLUE)
        [red, blue] = images_of(browser, draft_id)
        sent = replace_with(browser, draft_id, blue, GREEN, form=READ_NOW)
        ids = images_of(browser, draft_id)
        page = followed(browser, sent)

    assert sent.status_code == 303, sent.text
    assert ids[0] == red
    assert ids[1] != blue
    assert reader.calls == []
    assert lines_in(PROBLEM.search(page)) == [imports.SAID["no_key"]]
    assert lines_in(TOLD.search(page)) == [REPLACED]


STEP_FORM = re.compile(
    r'<form method="post" action="(/parent/imports/[0-9a-f]{32}/images/[0-9a-f]{32}/replace)" '
    r'enctype="multipart/form-data" data-pending="Replacing the screenshot">(.*?)</form>',
    re.S,
)


@pytest.mark.parametrize("key", [True, False], ids=["with a key", "with no key"])
def test_the_replace_step_offers_one_file_and_its_presses_with_their_lines(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, key: bool
) -> None:
    """The step shows the screenshot by its place, its state and its preview linked to the
    stored copy; its form takes one file, says choosing sends nothing, and offers Replace and
    read with the sharing line beside it (with a key) and Replace without reading with its own
    line; and a way back to the review. Opening the step reads nothing."""
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path, key=key)) as browser:
        draft_id = made(browser, RED, BLUE)
        [_, blue] = images_of(browser, draft_id)
        step = browser.get(replace_path(draft_id, blue))

    assert step.status_code == 200
    assert reader.calls == []
    page = step.text
    shot = f"/parent/imports/{draft_id}/images/{blue}"
    assert f'<a href="{shot}"><img src="{shot}" alt="Screenshot 2" width="72"></a>' in page
    assert says(page, f"Screenshot 2 · {imports.LABELS['unread']}")
    assert f'<a href="/parent/imports/{draft_id}">Back to the review</a>' in page
    [(action, form)] = STEP_FORM.findall(page)
    assert action == replace_path(draft_id, blue)
    pickers = re.findall(r'<input type="file"[^>]*>', form)
    assert len(pickers) == 1
    assert 'name="screenshots"' in pickers[0]
    assert "multiple" not in pickers[0]
    assert says(form, REPLACE_SENDS_NOTHING)
    assert "data-chosen" in form
    presses = presses_of(form)
    without = (
        '<button type="submit" class="{}" data-pending="Replacing the screenshot" '
        'aria-describedby="replace-without">Replace without reading</button>'
    ).format("secondary" if key else "primary")
    and_read = (
        '<button type="submit" class="primary" name="read" value="now" data-pending="Replacing '
        'and reading the screenshot" aria-describedby="replace-and-read">Replace and read</button>'
    )
    assert presses == ([and_read, without] if key else [without])
    # The lines are the template's own text, written as it is, not escaped.
    assert f'<p class="note" id="replace-without">{REPLACE_WITHOUT_SAID}</p>' in form
    if key:
        beside = form[form.index(and_read) :]
        assert beside.index(REPLACE_AND_READ_SAID) < beside.index(without)
        assert f'<p class="note" id="replace-and-read">{REPLACE_AND_READ_SAID}</p>' in form
    else:
        assert "Replace and read" not in page


def test_each_screenshot_on_the_review_offers_replace_beside_remove(
    tmp_path: pathlib.Path,
) -> None:
    """While the review isn't frozen and holds no review form, each screenshot's row links to
    its replace step, named for that screenshot and as tall as a button, beside its Remove
    press."""
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED, BLUE)
        ids = images_of(browser, draft_id)
        page = browser.get(f"/parent/imports/{draft_id}").text

    for place, image_id in enumerate(ids, start=1):
        link = (
            f'<a class="step-link" href="{replace_path(draft_id, image_id)}">Replace'
            f'<span class="visually-hidden"> screenshot {place}</span></a>'
        )
        assert page.count(link) == 1
        assert page.index(link) < page.index(f'form="remove-shot-{image_id}"')


# ------------------------------------------------------------------ D1, C11: edits kept on the way

REPLACE = "Replace"
BY_HAND = "Enter it by hand"
HAND_ENTRY = "/parent?add=1#enter-one"
CORRECTED = "Sentence Composing, corrected"
KEPT_FIRST = (
    "Replace and Enter it by hand keep your corrections, ticks and answers first, as Save review "
    "for later does."
)
KEPT_FIRST_BY_HAND = (
    "Enter it by hand keeps your corrections, ticks and answers first, as Save review for later "
    "does."
)
STAYS_OPEN = (
    "Until then, unless it is finished or discarded, this review stays open with its screenshots, "
    "saved corrections and kept answers; find it on Family review under Open screenshot reviews."
)
CONTROL = re.compile(r"<(button|a) [^>]*>.*?</\1>", re.S)
ADD_FOLD = '<details class="steps panel-fold" id="add-assignments"'
TICKED = re.compile(r'<input type="checkbox" name="unit" value="([^"]+)" checked>')


def control_for(page: str, words: str, place: int) -> str:
    """The one button or link with these words for Screenshot N, whole."""
    hidden = f" for screenshot {place}" if words == BY_HAND else f" screenshot {place}"
    end = f'>{words}<span class="visually-hidden">{hidden}</span></'
    found = [m.group(0) for m in CONTROL.finditer(page) if end in m.group(0)]
    assert len(found) == 1, found
    return found[0]


def press_value(control: str) -> str:
    """The value a review form's press sends, as the page offers it."""
    assert control.startswith('<button type="submit" class="secondary" form="review-form" ')
    assert 'name="press"' in control
    assert "formnovalidate" in control
    found = re.search(r'value="([^"]+)"', control)
    assert found is not None
    return html.unescape(found.group(1))


def typed_and_ticked(browser: TestClient, draft_id: str) -> tuple[str, dict[str, str], list[str]]:
    """The review with Screenshot 1's answers kept, its title corrected in its open box and its
    group ticked on the page, and nothing pressed that keeps the correction or the tick: the
    page, the typing and the tick."""
    page = browser.get(f"/parent/imports/{draft_id}").text
    page = followed(browser, send(browser, draft_id, page_post(page, "keep")))
    typed = {f"correct:{box_subject(page, 'title')}": CORRECTED}
    page = opened_for_typing(browser, draft_id, page, typed)
    tick = units_on(page)[:1]
    assert tick
    return page, typed, tick


def kept_by_the_press(browser: TestClient, draft_id: str, tick: list[str]) -> None:
    """The saved progress holds the title correction and the tick the press posted."""
    progress = progress_on(browser, draft_id)
    assert progress is not None
    assert list(progress.corrected.values()) == [{"title": CORRECTED}]
    assert progress.selected
    assert len(tick) == 1


def shows_the_edits(page: str, tick: list[str]) -> None:
    """The review shows the saved correction and the ticked group."""
    assert CORRECTED in html.unescape(page)
    assert [html.unescape(unit) for unit in TICKED.findall(page)] == tick


def test_replace_keeps_the_typing_and_the_tick_then_goes_to_its_replace_step(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Screenshot 1 is read with homework, Screenshot 2 failed. With Screenshot 1's title
    corrected and its group ticked on the page, and Save review for later never pressed,
    Screenshot 2's Replace keeps both and goes to Screenshot 2's replace step; once Screenshot 2
    is replaced, the review shows the correction and the tick, Screenshot 1's reading is as it
    was, and the reader is never called."""
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id, red, blue = one_read_one_failed(browser, monkeypatch, reader)
        page, typed, tick = typed_and_ticked(browser, draft_id)
        before = kept_state(browser, draft_id, red)
        calls = list(reader.calls)
        press = press_value(control_for(page, REPLACE, 2))
        sent = send(browser, draft_id, page_post(page, press, typed=typed, tick=tick))
        kept_by_the_press(browser, draft_id, tick)
        progress = kept_state(browser, draft_id, red)["progress"]
        step = browser.get(sent.headers.get("location", "/missing"))
        replaced = replace_with(browser, draft_id, blue, GREEN)
        review = followed(browser, replaced)
        after = kept_state(browser, draft_id, red)

    assert CORRECTED not in str(before["progress"])
    assert sent.status_code == 303, sent.text[:400]
    assert sent.headers["location"] == replace_path(draft_id, blue)
    assert step.status_code == 200
    assert "Replace Screenshot 2" in step.text
    assert replaced.status_code == 303, replaced.text[:400]
    assert after["readings"] == before["readings"]
    assert after["image"] == before["image"]
    assert after["progress"] == progress
    assert lines_in(TOLD.search(review)) == [REPLACED]
    shows_the_edits(review, tick)
    assert reader.calls == calls


def test_enter_it_by_hand_keeps_the_typing_and_the_tick_then_opens_hand_entry(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Screenshot 2's Enter it by hand keeps Screenshot 1's typed correction and tick, then goes
    to Family review with Add assignments open at Enter one assignment; the review, reopened
    from Open screenshot reviews, shows the correction and the tick, and the reader is never
    called."""
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id, red, _ = one_read_one_failed(browser, monkeypatch, reader)
        page, typed, tick = typed_and_ticked(browser, draft_id)
        before = kept_state(browser, draft_id, red)
        calls = list(reader.calls)
        press = press_value(control_for(page, BY_HAND, 2))
        sent = send(browser, draft_id, page_post(page, press, typed=typed, tick=tick))
        kept_by_the_press(browser, draft_id, tick)
        family = browser.get(sent.headers.get("location", "/missing").split("#")[0]).text
        listed = re.search(rf'<li><a href="(/parent/imports/{draft_id})">', family)
        reopened = browser.get(listed.group(1) if listed else "/missing").text
        after = kept_state(browser, draft_id, red)

    assert sent.status_code == 303, sent.text[:400]
    assert sent.headers["location"] == HAND_ENTRY
    assert f"{ADD_FOLD} open>" in family
    assert '<h2 class="entry-heading" id="enter-one">Enter one assignment</h2>' in family
    assert listed is not None
    assert after["readings"] == before["readings"]
    shows_the_edits(reopened, tick)
    assert reader.calls == calls


@pytest.mark.parametrize("words", [REPLACE, BY_HAND])
def test_a_keep_refused_as_save_review_for_later_refuses_it_goes_nowhere_and_says_why(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, words: str
) -> None:
    """A title longer than its field takes: the press answers as Save review for later answers
    the same post, 422 with the review and the same lines, keeps nothing and goes nowhere: no
    progress is stored before the press, and none after it, though the press posted a tick and
    a correction."""
    too_long = "y" * 301
    with here(open_settings(tmp_path)) as browser:
        draft_id, _, _ = one_read_one_failed(browser, monkeypatch, Reader(monkeypatch))
        page, typed, tick = typed_and_ticked(browser, draft_id)
        student = state_of(browser).project_state.student_id()
        drafts = state_of(browser).import_drafts
        before = drafts.snapshot(student, draft_id).progress
        typed = dict.fromkeys(typed, too_long)
        press = press_value(control_for(page, words, 2))
        sent = send(browser, draft_id, page_post(page, press, typed=typed, tick=tick))
        after = drafts.snapshot(student, draft_id).progress
        later = send(browser, draft_id, page_post(page, "later", typed=typed, tick=tick))

    assert sent.status_code == later.status_code == 422
    assert "location" not in sent.headers
    assert says(sent.text, imports.PRESS_SAID["too_long"])
    assert lines_in(PROBLEM.search(sent.text)) == lines_in(PROBLEM.search(later.text))
    assert tick
    assert before is None
    assert after is None


@pytest.mark.parametrize("prefix", ["replace:", "by-hand:"])
def test_a_press_naming_a_screenshot_the_review_doesn_t_hold_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    """A press naming another image id keeps nothing, goes nowhere and says the screenshot
    isn't in this review; one naming no id at all is refused as a post its page didn't make."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, _, _ = one_read_one_failed(browser, monkeypatch, Reader(monkeypatch))
        page, typed, tick = typed_and_ticked(browser, draft_id)
        other = send(browser, draft_id, page_post(page, prefix + "0" * 32, typed=typed, tick=tick))
        after_other = progress_on(browser, draft_id)
        bare = send(browser, draft_id, page_post(page, prefix + "x", typed=typed, tick=tick))
        after_bare = progress_on(browser, draft_id)
        bare_page = followed(browser, bare)

    assert other.status_code == 409, other.text[:400]
    assert "location" not in other.headers
    assert says(other.text, imports.SAID["image_gone"])
    assert after_other is None or not after_other.corrected
    assert bare.status_code == 303
    assert bare.headers["location"] == f"/parent/imports/{draft_id}"
    assert says(bare_page, imports.PRESS_SAID["later_invalid"])
    assert after_bare is None or not after_bare.corrected


def test_with_no_review_form_replace_and_enter_it_by_hand_are_links(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lone screenshot whose read failed has nothing to correct: Replace links to its replace
    step and Enter it by hand to hand entry, each as tall as a button, and the page says once
    that the review stays open, with no line about keeping first."""
    reader = Reader(monkeypatch)
    reader.answers[BLUE] = ScreenshotAnswer(None, "not_json", 10, 5)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, BLUE)
        page = browser.get(f"/parent/imports/{draft_id}").text
        browser.post(f"/parent/imports/{draft_id}/read", data=read_form(page))
        [blue] = images_of(browser, draft_id)
        page = browser.get(f"/parent/imports/{draft_id}").text

    assert 'id="review-form"' not in page
    assert 'form="review-form"' not in page
    assert control_for(page, REPLACE, 1) == (
        f'<a class="step-link" href="{replace_path(draft_id, blue)}">Replace'
        '<span class="visually-hidden"> screenshot 1</span></a>'
    )
    assert control_for(page, BY_HAND, 1) == (
        f'<a class="step-link" href="{HAND_ENTRY}" aria-describedby="stays-open">Enter it by hand'
        '<span class="visually-hidden"> for screenshot 1</span></a>'
    )
    assert page.count(str(escape(STAYS_OPEN))) == 1
    assert "keep your corrections, ticks and answers first" not in page


def test_with_the_review_form_each_press_says_it_keeps_first_and_only_attention_offers_by_hand(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each screenshot's Replace, and Enter it by hand on the one needing attention only, are
    presses of the review form that point to the one line saying they keep first; Enter it by
    hand also points to the line saying the review stays open. Remove still points to the typing
    note, and Enter in a box still presses Review changes, above the form or below."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, red, blue = one_read_one_failed(browser, monkeypatch, Reader(monkeypatch))
        page = browser.get(f"/parent/imports/{draft_id}").text
        typed_page, _, _ = typed_and_ticked(browser, draft_id)

    for shown in (page, typed_page):
        assert shown.count(str(escape(KEPT_FIRST))) == 1
        assert shown.count(str(escape(STAYS_OPEN))) == 1
        assert f'<p class="note" id="kept-first">{escape(KEPT_FIRST)}</p>' in shown
        assert f'<p class="note" id="stays-open">{escape(STAYS_OPEN)}</p>' in shown
        for place, image_id in ((1, red), (2, blue)):
            replace = control_for(shown, REPLACE, place)
            assert press_value(replace) == f"replace:{image_id}"
            assert 'aria-describedby="kept-first"' in replace
        by_hand = control_for(shown, BY_HAND, 2)
        assert press_value(by_hand) == f"by-hand:{blue}"
        assert 'aria-describedby="kept-first stays-open"' in by_hand
        assert f">{BY_HAND}<span" not in shown.replace(by_hand, "")
        remove = re.search(rf'<button [^>]*form="remove-shot-{red}"[^>]*>', shown)
        assert remove is not None
        assert 'aria-describedby="typing-lost"' in remove.group(0)
        first = first_button_of(shown, "review-form")
        assert (first.get("name"), first.get("value")) == ("press", "review")


def test_a_frozen_review_offers_enter_it_by_hand_and_no_replace(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once some homework is saved, the screenshot needing attention still offers Enter it by
    hand, as a press of the review form, whose line names it alone, and no screenshot offers
    Replace."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, _ = read_review(
            browser, monkeypatch, (RED, homework_answer()), (BLUE, homework_answer(cards=[]))
        )
        page = her_name_answered(browser, draft_id, browser.get(f"/parent/imports/{draft_id}").text)
        send(browser, draft_id, page_post(page, "save", tick=units_on(page)[:1]))
        page = browser.get(f"/parent/imports/{draft_id}").text
        [_, blue] = images_of(browser, draft_id)

    assert says(page, imports.SAID["frozen"])
    by_hand = control_for(page, BY_HAND, 2)
    assert press_value(by_hand) == f"by-hand:{blue}"
    assert 'aria-describedby="kept-first stays-open"' in by_hand
    assert f'<p class="note" id="kept-first">{escape(KEPT_FIRST_BY_HAND)}</p>' in page
    assert str(escape(KEPT_FIRST)) not in page
    assert f">{REPLACE}<span" not in page
    assert "/replace" not in page


def masked(page: str) -> str:
    """A family page with what each showing makes anew masked: the upload form's time and every
    one-time id, its token among them."""
    page = re.sub(r'(name="issued" value=")[^"]*"', r'\1-"', page)
    return re.sub(r'value="[0-9a-f]{32}"', 'value="-"', page)


def test_family_review_with_add_open_differs_only_in_the_fold_s_state(
    tmp_path: pathlib.Path,
) -> None:
    """The query that Enter it by hand goes to opens Add assignments and changes nothing else,
    and Enter one assignment carries the id it lands on."""
    with here(open_settings(tmp_path)) as browser:
        plain = browser.get("/parent")
        opened = browser.get("/parent?add=1")
        other = browser.get("/parent?add=yes")

    assert plain.status_code == opened.status_code == other.status_code == 200
    assert f"{ADD_FOLD}>" in plain.text
    assert f"{ADD_FOLD} open>" in opened.text
    assert masked(opened.text) == masked(plain.text).replace(f"{ADD_FOLD}>", f"{ADD_FOLD} open>")
    assert masked(other.text) == masked(plain.text)
    assert plain.text.count('id="enter-one"') == 1
    assert '<h2 class="entry-heading" id="enter-one">Enter one assignment</h2>' in plain.text


STEP_LINK_RULE = (
    "/* A screenshot's Replace and Enter it by hand, where they are links, and the replace step's"
    "\n   View screenshot keep their place in a line of actions and are as tall as a button. */\n"
    ".screenshot-list a.step-link {\n  display: inline-flex;\n  align-items: center;\n"
    "  min-height: 2.75rem;\n}\n"
)


def test_the_links_this_journey_adds_are_as_tall_as_a_button() -> None:
    """Replace and Enter it by hand where they are links, and the replace step's View
    screenshot, which stands alone in its line of actions, carry the one rule that makes them
    as tall as a button; the replace step's way back sits in a line of the app's ways back,
    which give each link its height."""
    css = (pathlib.Path(imports.__file__).parents[1] / "static" / "blossom.css").read_text(
        encoding="utf-8"
    )
    step = (
        pathlib.Path(imports.__file__).parents[1] / "templates" / "import_replace.html"
    ).read_text(encoding="utf-8")
    assert css.count(STEP_LINK_RULE) == 1
    assert css.count("step-link") == 1
    assert (
        '<p class="return"><a href="/parent/imports/{{ draft_id }}">Back to the review</a></p>'
        in step
    )
    assert (
        '<a class="step-link" href="/parent/imports/{{ draft_id }}/images/{{ image.id }}">'
        "View screenshot<span"
    ) in step
    assert step.count("View screenshot") == 1


# ------------------------------------------------------------------ what the review's lines name


@pytest.mark.parametrize(
    ("images", "line"),
    [
        (
            (RED,),
            "Read sends Screenshot 1 to Anthropic's API to be read. It isn't sent until you "
            "press Read.",
        ),
        (
            (RED, BLUE),
            "Read sends Screenshot 1 and Screenshot 2 to Anthropic's API to be read. They "
            "aren't sent until you press Read.",
        ),
    ],
    ids=["one screenshot", "two screenshots"],
)
def test_the_line_above_read_says_only_the_screenshots_it_names_wait_for_read(
    tmp_path: pathlib.Path, images: tuple[bytes, ...], line: str
) -> None:
    """The review's Add screenshots form offers Upload and read, which sends what it uploads
    with no Read press, so the line directly above Read says the screenshots it names wait for
    Read, and never that nothing is sent until then."""
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, *images)
        page = browser.get(f"/parent/imports/{draft_id}").text

    assert any(">Upload and read</button>" in form for form in upload_forms(page))
    assert f'<p class="note" id="read-disclosed">{escape(line)}</p>' in page
    assert "Nothing is sent until you press Read." not in page


@pytest.mark.parametrize("read", [False, True], ids=["without reading", "and read"])
def test_the_replace_note_names_the_place_the_new_screenshot_holds_after_its_write(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, read: bool
) -> None:
    """Another tab's Remove of Screenshot 1 commits while a replace of Screenshot 3 reads its
    body: once the replace's write commits, the new screenshot holds place 2, and the note
    names that place."""
    Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED, BLUE, GREEN)
        red, _, green = images_of(browser, draft_id)
        store = state_of(browser).import_drafts
        student = state_of(browser).project_state.student_id()
        real = store.replace_image

        def after_a_removal(*args: Any, **kwargs: Any) -> object:  # noqa: ANN401
            store.remove_image(student, draft_id, red)
            return real(*args, **kwargs)

        monkeypatch.setattr(store, "replace_image", after_a_removal)
        sent = replace_with(
            browser, draft_id, green, png(color=(9, 9, 9)), form=READ_NOW if read else None
        )
        page = followed(browser, sent)
        ids = images_of(browser, draft_id)

    assert sent.status_code == 303, sent.text
    assert len(ids) == 2
    assert green not in ids
    assert red not in ids
    assert lines_in(TOLD.search(page))[0] == "Screenshot 2 was replaced."


def every_card_read_as_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stand-in for the card reader that reads every card as one fixed reading, so cards of
    one layer from different screenshots would write the same row."""
    fixed: list[Reading] = []

    def all_the_same(cards: list[PortalCard], *, now: datetime) -> CardsRead:
        if not fixed:
            fixed.append(read_cards(cards[:1], now=now).readings[0])
        return CardsRead(readings=(fixed[0],) * len(cards), joined=tuple(range(len(cards))))

    monkeypatch.setattr(screenshots, "read_cards", all_the_same)


def read_image(place: int, reading: StoredReading) -> ImageState:
    """A stored image read as ``reading``, at its place in the review."""
    return ImageState(
        f"{place:032x}", place, reading.sha256, 390, 844, 1, NOW, True, None, False, False
    )


def test_a_screenshot_whose_lone_card_repeats_another_row_says_none_of_it_became_homework(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each of two screenshots holds one card, and the second card's row would write what the
    first card's row does, so it is a ``repeated`` item drawn from no place. That screenshot
    needs attention and says none of it became homework, never that no homework was found on
    it."""
    every_card_read_as_one(monkeypatch)
    readings = [
        stored_homework("first", shot((THU, 150), card(RL, 200))),
        stored_homework("second", shot((THU, 150), card(PS, 200))),
    ]
    assembly = homework_assembly(readings, now=NOW)
    [row] = assembly.rows
    [repeated] = assembly.unread
    with_row = {place.sha256 for place in row.sources}

    outcomes = screenshot_page.shot_outcomes(
        [read_image(index, reading) for index, reading in enumerate(readings)], readings, now=NOW
    )

    assert repeated.reason == "repeated"
    assert len(with_row) == 1
    by_sha = {reading.sha256: outcome for reading, outcome in zip(readings, outcomes, strict=True)}
    [other] = [sha for sha in by_sha if sha not in with_row]
    assert by_sha[next(iter(with_row))].state == "homework"
    assert by_sha[other].state == "text_only"
    assert by_sha[other].attention
    assert imports.label_of(by_sha[other]) == "Read, but none of it became homework."


COOKIE_MOST = 4096
"""The most bytes a browser keeps for one cookie's name and value."""
LONG_TITLES = [(f"Reading log {n:02d} " + "with a long title " * 11)[:200] for n in range(16)]
"""Sixteen titles of 200 characters each."""


def sixteen_long_rows() -> dict[str, Any]:
    """A homework list of sixteen cards, each its own row with a 200-character title."""
    return homework_answer(
        cards=[
            homework_card(title=title, y_top=200 + 40 * n, y_bottom=230 + 40 * n)
            for n, title in enumerate(LONG_TITLES)
        ]
    )


def note_cookie(answer: Answer) -> str:
    """The note cookie a redirect sets, as name=value."""
    found = re.search(rf"{imports.NOTE_COOKIE}=[^;]*", answer.headers.get("set-cookie", ""))
    assert found is not None, answer.headers
    return found.group(0)


@pytest.mark.parametrize("how", ["replace", "remove", "upload and read"])
def test_a_change_that_clears_many_long_rows_leaves_a_note_a_browser_keeps(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """Screenshot 2 holds sixteen answered rows with 200-character titles. Replacing or removing
    it, or adding and reading a screenshot that copies its cards, clears all sixteen; the note
    the page after reads stays within what a browser keeps for one cookie, and names the first
    three rows, shortened, and how many more."""
    long_rows = sixteen_long_rows()
    with here(open_settings(tmp_path)) as browser:
        draft_id, page = read_review(
            browser, monkeypatch, (RED, homework_answer()), (BLUE, long_rows), (GREEN, long_rows)
        )
        [_, blue, green] = images_of(browser, draft_id)
        browser.post(f"/parent/imports/{draft_id}/images/{green}/remove")
        page = browser.get(f"/parent/imports/{draft_id}").text
        followed(browser, send(browser, draft_id, page_post(page, "keep")))
        before = stored_answers(browser, draft_id)
        if how == "replace":
            sent = replace_with(browser, draft_id, blue, png(color=(9, 9, 9)))
        elif how == "remove":
            sent = browser.post(f"/parent/imports/{draft_id}/images/{blue}/remove")
        else:
            sent = upload(browser, GREEN, form=READ_NOW, to=f"/parent/imports/{draft_id}/images")
        cookie = note_cookie(sent)
        page = followed(browser, sent)

    assert len(before) >= 16, sorted(before)
    assert sent.status_code == 303, sent.text
    assert len(cookie.encode()) <= COOKIE_MOST, len(cookie.encode())
    line = re.search(
        rf"{re.escape(CLEARED_INTRO)} ([^<]*?); and 13 more\. Answer them again\.",
        html.unescape(page),
    )
    assert line is not None
    named = line.group(1).split("; ")
    assert len(named) == 3
    assert set(named) <= {title[:57] + "..." for title in LONG_TITLES}


def test_every_line_after_a_change_names_a_few_rows_and_how_many_more() -> None:
    """Sixteen rows with 200-character titles cleared, sixteen kept unsaved and sixteen asked
    about again: each list names its first three, shortened, and says how many more, and the
    note carrying every line, the confirmation's and the replace's among them, stays within what
    a browser keeps for one cookie. Four rows are all named, as "1 more" is never said."""
    many = ChangeTold(
        tuple(ClearedRow(title, None, True) for title in LONG_TITLES),
        tuple(LONG_TITLES),
        tuple(LONG_TITLES),
        name_asked=True,
    )
    said = screenshot_page.change_said(many)
    note = imports.note_for(bytes(32), "0" * 32, said, ["Screenshot 2 was replaced."])
    short = [title[:57] + "..." for title in LONG_TITLES[:3]]
    four = screenshot_page.change_said(ChangeTold((), tuple(LONG_TITLES[:4]), ()))

    assert said == [
        f"{CLEARED_INTRO} {'; '.join(short)}; and 13 more. Answer them again.",
        screenshot_page.CHANGE_SAID["copies"],
        *(screenshot_page.CHANGE_SAID["carried"].format(label=title) for title in short),
        screenshot_page.CHANGE_SAID["carried_more"].format(count=13),
        *(screenshot_page.CHANGE_SAID["asked"].format(label=title) for title in short),
        screenshot_page.CHANGE_SAID["asked_more"].format(count=13),
        screenshot_page.CHANGE_SAID["name"],
    ]
    assert len(f"{imports.NOTE_COOKIE}={note}".encode()) <= COOKIE_MOST
    assert len(four) == 4
    assert not any("more rows" in line for line in four)


NOTE_MOST = 4000
"""The most bytes the note cookie's name and value may take here, below what a browser keeps."""
WIDE_TITLES = ["".join(chr(0x4E00 + 60 * n + place) for place in range(60)) for n in range(16)]
"""Sixteen titles of 60 CJK characters each, written as code points so this file stays ASCII."""


def wide_lists() -> ChangeTold:
    """Sixteen rows with 60-character CJK titles in each list a change can name: cleared, as
    identical cards, kept unsaved and asked about again, and the confirmation cleared."""
    return ChangeTold(
        tuple(ClearedRow(title, None, True) for title in WIDE_TITLES),
        tuple(WIDE_TITLES),
        tuple(WIDE_TITLES),
        name_asked=True,
    )


def test_lines_with_long_non_ascii_titles_name_fewer_rows_so_the_note_stays_small() -> None:
    """Three lists of sixteen rows with 60-character CJK titles, as the redirect's note carries
    them: each list names its first rows, at least one and at most three, and counts the rest;
    the note's whole Set-Cookie header stays within 4,096 bytes, its name and value under 4,000,
    and it reads back whole and signed."""

    class Keyed:
        result_key = bytes(32)

    keyed: Any = Keyed()
    told = wide_lists()
    answer = imports.to_review(keyed, "0" * 32, [told], ["Screenshot 2 was replaced."])
    header = answer.headers["set-cookie"]
    note = header.split(";", 1)[0].split("=", 1)[1].strip('"')
    said = imports.note_read(bytes(32), "0" * 32, note).said
    named = {
        list_: [title for title in WIDE_TITLES if any(title in line for line in lines)]
        for list_, lines in (
            ("cleared", said[:1]),
            ("carried", [line for line in said if line.endswith("as you chose for these cards.")]),
            ("asked", [line for line in said if line.endswith("You can leave it unsaved again.")]),
        )
    }

    assert len(header.encode()) <= NOTE_HEADER_MOST
    assert len(f"{imports.NOTE_COOKIE}={note}".encode()) < NOTE_MOST
    assert imports.note_read(bytes(32), "0" * 32, note) == imports.Note(
        said, ["Screenshot 2 was replaced."], []
    )
    for list_, titles in named.items():
        assert 1 <= len(titles) <= screenshot_page.NAMED_MOST, list_
        assert titles == WIDE_TITLES[: len(titles)], list_
    shown = len(named["cleared"])
    assert said[0].endswith(f"; and {16 - shown} more. Answer them again.")
    assert screenshot_page.CHANGE_SAID["carried_more"].format(count=16 - shown) in said
    assert screenshot_page.CHANGE_SAID["asked_more"].format(count=16 - shown) in said
    assert said[-1] == screenshot_page.CHANGE_SAID["name"]


@pytest.mark.parametrize("how", ["replace", "remove", "upload and read"])
def test_a_change_naming_long_non_ascii_titles_leaves_a_note_a_browser_keeps(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """A change whose three lists each hold sixteen rows with 60-character CJK titles, through
    Replace, Remove and Upload and read: the note cookie's name and value stay under 4,000 bytes,
    and the page after reads it and says each of its lines."""
    with here(open_settings(tmp_path)) as browser:
        draft_id, _ = read_review(
            browser, monkeypatch, (RED, homework_answer()), (BLUE, homework_answer(cards=[]))
        )
        [_, blue] = images_of(browser, draft_id)
        monkeypatch.setattr(imports, "change_told", lambda *_: wide_lists())
        if how == "replace":
            sent = replace_with(browser, draft_id, blue, GREEN)
        elif how == "remove":
            sent = browser.post(f"/parent/imports/{draft_id}/images/{blue}/remove")
        else:
            sent = upload(browser, GREEN, form=READ_NOW, to=f"/parent/imports/{draft_id}/images")
        cookie = note_cookie(sent)
        page = followed(browser, sent)

    assert sent.status_code == 303, sent.text
    assert len(cookie.encode()) < NOTE_MOST, len(cookie.encode())
    assert says_each(page, screenshot_page.change_said(wide_lists()))


ENDED_FIRST = f"Screenshot 1 · {imports.LABELS['ended']}"


def busy_for(patch: pytest.MonkeyPatch, browser: TestClient, image_id: str | None) -> None:
    """While ``patch`` holds, the household file's writer is busy whenever a reading of
    ``image_id``, or of any screenshot when it is None, is stored, as when another connection
    holds a write transaction: the reader answers, and its reading isn't kept."""
    store = state_of(browser).import_drafts
    real = store.store_reading

    def busy(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
        if image_id is None or kwargs.get("image_id") == image_id:
            raise WriterBusy
        return real(*args, **kwargs)

    patch.setattr(store, "store_reading", busy)


@pytest.mark.parametrize("how", ["upload and read", "replace and read", "read"])
def test_a_screenshot_whose_read_ended_keeps_its_outcome_when_another_is_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """Screenshot 1's read ended with no reading kept, as the writer was busy: it says so and
    offers Enter it by hand. Then another screenshot is read: a new file by Upload and read,
    Screenshot 2's new file by Replace and read, or Screenshot 2 by a Read press from a page that
    named it alone. On the page after and when the review is opened again, Screenshot 1 still
    says so and offers Enter it by hand, Read still offers it, and it was sent once only."""
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        stale: dict[str, str] = {}
        if how == "read":
            draft_id = made(browser, RED)
            review = f"/parent/imports/{draft_id}"
            browser.post(f"{review}/read", data=read_form(browser.get(review).text))
            assert upload(browser, BLUE, form={}, to=f"{review}/images").status_code == 303
            stale = read_form(browser.get(review).text)
            [red, blue] = images_of(browser, draft_id)
            assert stale["images"] == blue
            with monkeypatch.context() as patch:
                busy_for(patch, browser, None)
                replace_with(browser, draft_id, red, GREEN, form=READ_NOW)
        else:
            draft_id = made(browser, RED) if how == "upload and read" else made(browser, RED, BLUE)
            review = f"/parent/imports/{draft_id}"
            with monkeypatch.context() as patch:
                busy_for(patch, browser, images_of(browser, draft_id)[0])
                browser.post(f"{review}/read", data=read_form(browser.get(review).text))
        first = images_of(browser, draft_id)[0]
        first_png = prepared(browser, draft_id, first)
        ended = browser.get(review).text
        if how == "upload and read":
            other = upload(browser, png(color=(9, 9, 9)), form=READ_NOW, to=f"{review}/images")
        elif how == "replace and read":
            second = images_of(browser, draft_id)[1]
            other = replace_with(browser, draft_id, second, png(color=(9, 9, 9)), form=READ_NOW)
        else:
            other = browser.post(f"{review}/read", data=stale)
        after = followed(browser, other)
        reopened = browser.get(review).text
        held = images_of(browser, draft_id)

    assert other.status_code in (200, 303), other.text[:400]
    assert len(held) == 2
    assert held[0] == first
    assert says(ended, ENDED_FIRST)
    assert control_for(ended, BY_HAND, 1)
    for page in (after, reopened):
        assert says(page, ENDED_FIRST), where_each_stands(page)
        assert control_for(page, BY_HAND, 1)
    assert read_form(reopened)["images"] == first
    assert reader.calls.count(first_png) == 1


RESENT = "These screenshots are already in this review. This retry didn't read them again."
RESENT_ONE = "This screenshot is already in this review. This retry didn't read it again."
PRESS_READ = "Press Read to read them."
PRESS_READ_ONE = "Press Read to read it."
"""What a retried Upload and read says when every screenshot it sent is already in the review,
and what it adds when the page offers Read for them."""
WERE_THERE = (
    "These screenshots were in this review when this retry ran. This retry didn't read them again."
)
WAS_THERE = (
    "This screenshot was in this review when this retry ran. This retry didn't read it again."
)
"""The retry's line when the review doesn't hold every screenshot it resent, as the page shows
it."""
RETRIED: Final = {
    "stored unread": ((RED, BLUE), f"{RESENT} {PRESS_READ}"),
    "stored and read": ((RED, BLUE), RESENT),
    "no key": ((RED, BLUE), RESENT),
    "one file": ((RED,), f"{RESENT_ONE} {PRESS_READ_ONE}"),
}
"""Each way the first send stood when it was sent again: its screenshots, and the line the
review then says."""


def the_open_review(browser: TestClient) -> str:
    """The one open review's id."""
    student = state_of(browser).project_state.student_id()
    [only] = state_of(browser).import_drafts.open_drafts(student)
    return only.draft_id


@pytest.mark.parametrize("first", list(RETRIED))
def test_a_retried_upload_and_read_reads_nothing_already_stored_and_says_so(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    """A first Upload and read whose write committed, though the page couldn't tell, is sent
    again with Upload and read from that page's own form: its screenshots stored unread, stored
    and then read by the review's Read press, with no key set, or a single screenshot. The retry
    calls the reader zero times and the review says, in place of a line per screenshot, that
    they are already in it and weren't read again, adding "Press Read" only where the page offers
    Read for them."""
    images, expected = RETRIED[first]
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path, key=first != "no key")) as browser:
        sent = {**first_form(browser), **READ_NOW}
        answer, _ = uncertain_upload(browser, *images, committed=True, form=sent)
        draft_id = the_open_review(browser)
        review = f"/parent/imports/{draft_id}"
        if first == "stored and read":
            browser.post(f"{review}/read", data=read_form(browser.get(review).text))
        calls = list(reader.calls)
        again = upload(browser, *images, form={**again_form(answer.text), **READ_NOW})
        page = followed(browser, again)
        held = images_of(browser, draft_id)

    assert answer.status_code == 503
    assert again.status_code == 303, again.text[:400]
    assert again.headers["location"] == review
    assert len(calls) == (len(images) if first == "stored and read" else 0)
    assert reader.calls == calls
    assert len(held) == len(images)
    assert lines_in(PROBLEM.search(page)) == [expected]
    if "Press Read" in expected:
        assert read_form(page)["images"] == ",".join(held)
    else:
        assert 'action="' + review + '/read"' not in page


@pytest.mark.parametrize("key", [False, True], ids=["no key", "a key"])
def test_with_no_key_and_no_rows_each_unread_screenshot_links_to_hand_entry(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, key: bool
) -> None:
    """Two screenshots uploaded without reading and no rows to review. With no key, each offers
    Enter it by hand as a link to hand entry, which opens Family review with Enter one assignment
    open; with a key, Read is offered and Enter it by hand isn't. The reader is never called."""
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path, key=key)) as browser:
        draft_id = made(browser, RED, BLUE)
        page = browser.get(f"/parent/imports/{draft_id}").text
        family = browser.get(HAND_ENTRY.split("#")[0]).text

    assert 'id="review-form"' not in page
    assert reader.calls == []
    if key:
        assert f">{BY_HAND}<" not in page
        return
    for place in (1, 2):
        assert control_for(page, BY_HAND, place) == (
            f'<a class="step-link" href="{HAND_ENTRY}" aria-describedby="stays-open">Enter it '
            f'by hand<span class="visually-hidden"> for screenshot {place}</span></a>'
        )
        assert says(page, f"Screenshot {place} · {imports.LABELS['unread']}")
    assert f"{ADD_FOLD} open>" in family
    assert '<h2 class="entry-heading" id="enter-one">Enter one assignment</h2>' in family


def test_with_no_key_an_unread_screenshot_s_enter_it_by_hand_keeps_the_edits(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Screenshot 1 was read while a key was set, so the review has rows; Screenshot 2 was
    uploaded without reading. With the key gone, Screenshot 2 offers Enter it by hand as a press
    of the review form. With Screenshot 1's title corrected and its group ticked on the page and
    nothing saved, the press keeps both and goes to Family review with Enter one assignment open;
    the review, opened again, shows the correction and the tick, and the reader isn't called once
    the key is gone."""
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        review = f"/parent/imports/{draft_id}"
        browser.post(f"{review}/read", data=read_form(browser.get(review).text))
        assert upload(browser, BLUE, form={}, to=f"{review}/images").status_code == 303
        calls = list(reader.calls)
    with here(open_settings(tmp_path, key=False)) as browser:
        [_, blue] = images_of(browser, draft_id)
        page, typed, tick = typed_and_ticked(browser, draft_id)
        control = control_for(page, BY_HAND, 2)
        press = press_value(control)
        sent = send(browser, draft_id, page_post(page, press, typed=typed, tick=tick))
        kept_by_the_press(browser, draft_id, tick)
        family = browser.get(sent.headers.get("location", "/missing").split("#")[0]).text
        reopened = browser.get(review).text

    assert calls == [RED]
    assert says(page, f"Screenshot 2 · {imports.LABELS['unread']}")
    assert press == f"by-hand:{blue}"
    assert 'aria-describedby="kept-first stays-open"' in control
    assert f">{BY_HAND}<span" not in page.replace(control, "")
    assert sent.status_code == 303, sent.text[:400]
    assert sent.headers["location"] == HAND_ENTRY
    assert f"{ADD_FOLD} open>" in family
    assert '<h2 class="entry-heading" id="enter-one">Enter one assignment</h2>' in family
    shows_the_edits(reopened, tick)
    assert reader.calls == calls


def only_unreadable_cards() -> dict[str, Any]:
    """A homework list whose one card's title couldn't be read."""
    return homework_answer(headings=[], cards=[homework_card(title=None)])


ENTER_PRESSES: Final = {
    "split question": "keep",
    "unreadable card": "keep",
    "a card": "review",
}
"""Reviews whose rows ask only which assigned day applies, ask only about a card that couldn't
be read, or show a card, each with the press Enter in one of the review form's boxes sends."""
HIDDEN_COPY = re.compile(r'<button type="submit" class="visually-hidden" form="review-form"[^>]*>')
"""The copy of the review form's default press above it."""


@pytest.mark.parametrize("case", list(ENTER_PRESSES))
def test_enter_in_the_review_form_presses_its_own_default_and_never_a_screenshot_s(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A review with rows and an unrelated screenshot uploaded without reading, which offers
    Replace as a press of the review form above it. The form's first submit button in tree order,
    the one Enter in a box presses, is the form's own default: Keep these answers where the rows
    offer no Review changes, Review changes where they do; never a screenshot's press. The copy is
    out of the tab order, hidden from assistive technology and sends no validation."""
    monday, tuesday = split_answers()
    answers = {
        "split question": ((RED, monday), (BLUE, tuesday)),
        "unreadable card": ((RED, only_unreadable_cards()),),
        "a card": ((RED, homework_answer()),),
    }[case]
    with here(open_settings(tmp_path)) as browser:
        draft_id, _ = read_review(browser, monkeypatch, *answers)
        added = upload(browser, GREEN, form={}, to=f"/parent/imports/{draft_id}/images")
        page = browser.get(f"/parent/imports/{draft_id}").text
        green = images_of(browser, draft_id)[-1]

    first = first_button_of(page, "review-form")
    offered = 'name="press" value="review" formnovalidate aria-describedby' in review_form(page)
    copy = HIDDEN_COPY.search(page)

    assert added.status_code == 303
    assert press_value(control_for(page, REPLACE, len(answers) + 1)) == f"replace:{green}"
    assert offered == (ENTER_PRESSES[case] == "review")
    assert (first.get("name"), first.get("value")) == ("press", ENTER_PRESSES[case])
    assert copy is not None
    assert copy.group(0) == (
        '<button type="submit" class="visually-hidden" form="review-form" name="press" '
        f'value="{ENTER_PRESSES[case]}" formnovalidate tabindex="-1" aria-hidden="true">'
    )
    assert page.index(copy.group(0)) < page.index(f"replace:{green}")


NOTE_HEADER_MOST = 4096
"""The most bytes the note's whole Set-Cookie header may take: its name, value and attributes."""
TWO_WIDE = tuple(WIDE_TITLES[:2])


def two_of_each() -> ChangeTold:
    """Two rows with 60-character CJK titles in each list a change can name: cleared, as
    identical cards, kept unsaved and asked about again, and the confirmation cleared."""
    return ChangeTold(
        tuple(ClearedRow(title, None, True) for title in TWO_WIDE),
        TWO_WIDE,
        TWO_WIDE,
        name_asked=True,
    )


def note_header(answer: Answer) -> str:
    """The whole Set-Cookie header a redirect sets for the note."""
    headers: Any = answer.headers
    found = [
        str(value)
        for value in headers.get_list("set-cookie")
        if value.startswith(f"{imports.NOTE_COOKIE}=")
    ]
    assert len(found) == 1, answer.headers
    return found[0]


ONE_NAMED: Final = [
    f"{CLEARED_INTRO} {TWO_WIDE[0]}; and 1 more. Answer them again.",
    screenshot_page.CHANGE_SAID["copies"],
    screenshot_page.CHANGE_SAID["carried"].format(label=TWO_WIDE[0]),
    "Your screenshots changed. 1 more row is still left unsaved, as you chose for its cards.",
    screenshot_page.CHANGE_SAID["asked"].format(label=TWO_WIDE[0]),
    "Your screenshots changed. 1 more row now has cards you hadn't left unsaved, so it asks "
    "again. You can leave it unsaved again.",
    screenshot_page.CHANGE_SAID["name"],
]
"""The lines of ``two_of_each`` when each list names one row and counts the other."""
NONE_NAMED: Final = [
    "Your screenshots changed, so 2 rows' answers were cleared. Answer them again.",
    screenshot_page.CHANGE_SAID["copies"],
    "Your screenshots changed. 2 rows are still left unsaved, as you chose for their cards.",
    "Your screenshots changed. 2 rows now have cards you hadn't left unsaved, so they ask again. "
    "You can leave them unsaved again.",
    screenshot_page.CHANGE_SAID["name"],
]
"""The lines of ``two_of_each`` when the lists name no row and keep their counts."""
ONE_ROW_NONE_NAMED: Final = [
    "Your screenshots changed, so 1 row's answers were cleared. Answer them again.",
    "Your screenshots changed. 1 row is still left unsaved, as you chose for its cards.",
    "Your screenshots changed. 1 row now has cards you hadn't left unsaved, so it asks again. "
    "You can leave it unsaved again.",
]
"""The count-only lines of one row in each list, none of them identical cards."""


def test_a_change_s_lists_name_three_two_one_then_no_rows_keeping_every_count() -> None:
    """Each list of a change's lines names at most three, two or one row, or none: at one, one row
    and the count of the rest even when one is left, in the singular; at none, only the counts.
    Every outcome sentence stays at every step."""
    two = two_of_each()
    one_row = ChangeTold((ClearedRow(TWO_WIDE[0], None, False),), TWO_WIDE[:1], TWO_WIDE[:1])
    full = screenshot_page.change_said(two)

    assert screenshot_page.change_said(two, 3) == screenshot_page.change_said(two, 2) == full
    assert all(title in "".join(full) for title in TWO_WIDE)
    assert screenshot_page.change_said(two, 1) == ONE_NAMED
    assert screenshot_page.change_said(two, 0) == NONE_NAMED
    assert screenshot_page.change_said(one_row, 0) == ONE_ROW_NONE_NAMED
    assert screenshot_page.change_said(one_row, 1) == screenshot_page.change_said(one_row)
    assert not hasattr(screenshot_page, "CHANGE_BYTES_MOST")


LOG = "Reading log"
QUIZ = "Vocab Quiz"
LAB = "Lab Report"
MAP = "Map project"
POSTER = "Timeline poster"
ESSAY = "Essay outline"


def sharing_titles() -> ChangeTold:
    """A report whose rows share titles in every list: five cleared rows under three titles
    (two Reading log, one Vocab Quiz, two Lab Report), two rows kept unsaved under one title and
    four rows asked about again under two (three Timeline poster, one Essay outline)."""
    return ChangeTold(
        (
            ClearedRow(LOG, None, False),
            ClearedRow(LOG, None, False),
            ClearedRow(QUIZ, None, False),
            ClearedRow(LAB, None, False),
            ClearedRow(LAB, None, False),
        ),
        (MAP, MAP),
        (POSTER, POSTER, POSTER, ESSAY),
    )


def kept_line(title: str) -> str:
    return screenshot_page.CHANGE_SAID["carried"].format(label=title)


def asked_line(title: str) -> str:
    return screenshot_page.CHANGE_SAID["asked"].format(label=title)


KEPT_ONE_MORE = (
    "Your screenshots changed. 1 more row is still left unsaved, as you chose for its cards."
)
ASKED_TWO_MORE = (
    "Your screenshots changed. 2 more rows now have cards you hadn't left unsaved, so they ask "
    "again. You can leave them unsaved again."
)
ASKED_THREE_MORE = ASKED_TWO_MORE.replace("2 more", "3 more")
SHARING_SAID: Final = {
    3: [
        f"{CLEARED_INTRO} {LOG}; {QUIZ}; {LAB}; and 2 more. Answer them again.",
        kept_line(MAP),
        KEPT_ONE_MORE,
        asked_line(POSTER),
        asked_line(ESSAY),
        ASKED_TWO_MORE,
    ],
    1: [
        f"{CLEARED_INTRO} {LOG}; and 4 more. Answer them again.",
        kept_line(MAP),
        KEPT_ONE_MORE,
        asked_line(POSTER),
        ASKED_THREE_MORE,
    ],
    0: [
        "Your screenshots changed, so 5 rows' answers were cleared. Answer them again.",
        "Your screenshots changed. 2 rows are still left unsaved, as you chose for their cards.",
        "Your screenshots changed. 4 rows now have cards you hadn't left unsaved, so they ask "
        "again. You can leave them unsaved again.",
    ],
}
"""The lines of ``sharing_titles`` naming at most three, one and no rows a list. Each title is
named once; every count is of the rows: five cleared, two kept unsaved, four asked again. At two
the lines are those at three, since naming two titles of three would leave one title over."""


def test_counts_after_a_change_are_of_the_rows_and_titles_are_named_once() -> None:
    """Rows sharing a title are named once by it, and "and N more", "N more rows" and the
    count-only lines count every row the change touched: N is the rows less the titles named."""
    told = sharing_titles()

    assert screenshot_page.change_said(told) == SHARING_SAID[3]
    assert screenshot_page.change_said(told, 3) == SHARING_SAID[3]
    assert screenshot_page.change_said(told, 2) == SHARING_SAID[3]
    assert screenshot_page.change_said(told, 1) == SHARING_SAID[1]
    assert screenshot_page.change_said(told, 0) == SHARING_SAID[0]


def two_logs(words: str = "Read 20 minutes.") -> dict[str, Any]:
    """One screenshot of two cards titled Reading log, in two classes: two rows sharing a
    title."""
    return homework_answer(
        cards=[
            homework_card(course="English 8", title=LOG, instruction=words),
            homework_card(
                course="History 8", title=LOG, instruction=words, y_top=400, y_bottom=520
            ),
        ]
    )


def logs_before() -> tuple[list[StoredReading], list[str]]:
    """The screenshot of ``two_logs`` as stored, and its two rows' keys."""
    before = [stored_homework("logs", two_logs())]
    keys = [row.key for row in homework_assembly(before, now=KEY_INSTANT).rows]
    assert len(keys) == 2
    return before, keys


def test_the_report_of_a_change_holds_every_row_it_touched_titles_shared_or_not() -> None:
    """Two rows titled Reading log: removing their screenshot clears both, reading it again
    with other words keeps both unsaved, and a second copy of it asks about both again. The
    report holds both rows each time, and the page counts both."""
    before, keys = logs_before()
    answered = {("destination", key): "{}" for key in keys}
    aside = frozenset(keys)
    reread = [stored_homework("logs", two_logs("Read 30 minutes."))]
    copied = [*before, stored_homework("logs-again", two_logs())]

    cleared = screenshot_review.change_told(before, [], answered, frozenset())
    kept = screenshot_review.change_told(before, reread, {}, aside)
    asked = screenshot_review.change_told(before, copied, {}, aside)

    assert cleared.cleared == (ClearedRow(LOG, None, False), ClearedRow(LOG, None, False))
    assert screenshot_page.change_said(cleared) == [
        f"{CLEARED_INTRO} {LOG}; and 1 more. Answer them again."
    ]
    assert kept.carried == (LOG, LOG)
    assert screenshot_page.change_said(kept, 0) == [
        "Your screenshots changed. 2 rows are still left unsaved, as you chose for their cards."
    ]
    assert asked.asked == (LOG, LOG)
    assert screenshot_page.change_said(asked) == [
        asked_line(LOG),
        "Your screenshots changed. 1 more row now has cards you hadn't left unsaved, so it asks "
        "again. You can leave it unsaved again.",
    ]


def test_replacing_a_screenshot_whose_two_rows_share_a_title_counts_both(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Screenshot 2 holds two answered rows titled Reading log; Replace clears both, and the
    page names the title once and counts the other row."""
    with here(open_settings(tmp_path)) as browser:
        shots = ((RED, homework_answer()), (BLUE, two_logs()))
        draft_id, page = read_review(browser, monkeypatch, *shots)
        followed(browser, send(browser, draft_id, page_post(page, "keep")))
        before = stored_answers(browser, draft_id)
        [_, blue] = images_of(browser, draft_id)
        sent = replace_with(browser, draft_id, blue, GREEN)
        page = followed(browser, sent)

    assert sent.status_code == 303, sent.text
    assert len([key for key in before if key[0] == "destination"]) >= 3
    assert says(page, f"{CLEARED_INTRO} {LOG}; and 1 more. Answer them again.")
    assert lines_in(TOLD.search(page)) == [REPLACED]


CHANGES: Final = ["replace and read", "replace", "remove", "add and read", "add with refusals"]
"""Each press whose note carries a change's report: Replace and read carries two, the replace's
and its read's; the others one, Add screenshots with refused files beside it."""


@pytest.mark.parametrize("how", CHANGES)
def test_one_budget_on_the_whole_note_keeps_every_outcome_and_the_replacement(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    how: str,
) -> None:
    """Each change of screenshots reports two rows with 60-character CJK titles in every list,
    the identical cards' line and the confirmation cleared, as when three groups of long CJK
    titles change. The note's whole Set-Cookie header stays within 4,096 bytes, and the page
    after says every outcome sentence with its count, the replacement where there is one, and
    each refused file: Replace and read's two reports name one row a list and count the other;
    a single report names both."""
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED, BLUE)
        review = f"/parent/imports/{draft_id}"
        browser.post(f"{review}/read", data=read_form(browser.get(review).text))
        [_, blue] = images_of(browser, draft_id)
        monkeypatch.setattr(imports, "change_told", lambda *_: two_of_each())
        if how == "replace and read":
            sent = replace_with(browser, draft_id, blue, GREEN, form=READ_NOW)
        elif how == "replace":
            sent = replace_with(browser, draft_id, blue, GREEN)
        elif how == "remove":
            sent = browser.post(f"{review}/images/{blue}/remove")
        elif how == "add and read":
            sent = upload(browser, GREEN, form=READ_NOW, to=f"{review}/images")
        else:
            sent = upload(browser, GREEN, gif(), gif(), form=READ_NOW, to=f"{review}/images")
        header = note_header(sent)
        page = followed(browser, sent)

    record_property("note_header_bytes", len(header.encode()))
    reports = 2 if how == "replace and read" else 1
    lines = ONE_NAMED if reports == 2 else screenshot_page.change_said(two_of_each())
    shown = lines_in(PROBLEM.search(page))
    assert sent.status_code == 303, sent.text[:400]
    assert len(header.encode()) <= NOTE_HEADER_MOST, len(header.encode())
    assert [line for line in shown if line in lines] == lines * reports
    assert says(page, "Screenshot 2 was replaced.") == how.startswith("replace")
    if how == "add with refusals":
        assert says(page, "Screenshot 2 wasn't added: it isn't a PNG or JPEG.")
        assert says(page, "Screenshot 3 wasn't added: it isn't a PNG or JPEG.")
    assert len(reader.calls) == (2 if how in ("replace", "remove") else 3)


def test_a_replace_and_read_note_of_two_wide_reports_names_one_row_a_list_and_fits(
    record_property: Callable[[str, object], None],
) -> None:
    """The note of Replace and read, as the redirect sets it, with both reports of two rows a list
    with 60-character CJK titles: three names a list would pass 4,096 bytes; one name a list and
    the count of the other fits, and reads back whole with the replacement line."""
    told = ["Screenshot 2 was replaced."]
    key, draft_id = bytes(32), "0" * 32
    full = imports.note_for(key, draft_id, screenshot_page.change_said(two_of_each()) * 2, told)

    class Keyed:
        result_key = key

    keyed: Any = Keyed()
    answer = imports.to_review(keyed, draft_id, [two_of_each(), two_of_each()], told)
    header = answer.headers["set-cookie"]
    value = header.split(";", 1)[0].split("=", 1)[1].strip('"')
    record_property("names_all_bytes", len(f"{imports.NOTE_COOKIE}={full}".encode()))
    record_property("note_header_bytes", len(header.encode()))

    assert len(f"{imports.NOTE_COOKIE}={full}".encode()) > NOTE_HEADER_MOST
    assert len(header.encode()) <= NOTE_HEADER_MOST
    assert imports.note_read(key, draft_id, value) == imports.Note(ONE_NAMED * 2, told, [])


def other_browser(browser: TestClient) -> TestClient:
    """A second browser of the family on the same app: its own cookies, nothing else apart."""
    return TestClient(
        browser.app,
        follow_redirects=False,
        headers={"Host": LOOPBACK, "Origin": f"http://{LOOPBACK}"},
        client=("127.0.0.1", 50001),
    )


RACED: Final = {
    "nobody touches it": ((RED,), f"{RESENT_ONE} {PRESS_READ_ONE}"),
    "the other browser reads it": ((RED,), RESENT_ONE),
    "the other browser replaces it": ((RED,), WAS_THERE),
    "nobody touches them": ((RED, BLUE), f"{RESENT} {PRESS_READ}"),
    "the other browser reads one": ((RED, BLUE), RESENT),
    "the other browser removes one": ((RED, BLUE), WERE_THERE),
}
"""What another browser of the family does between the retry's redirect and its landing, with
the screenshots the first upload stored, and the line the landing page says: "already in this
review" only while it holds every one of them, read or not, and "Press Read" only when its Read
press sends every one."""


@pytest.mark.parametrize("between", list(RACED))
def test_a_retry_s_landing_page_says_press_read_only_when_its_read_sends_those_screenshots(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, between: str
) -> None:
    """Browser A's first Upload and read committed though its page couldn't tell, and A sends it
    again with Upload and read: zero reader calls. Before A follows the redirect, browser B reads
    the screenshot, reads both with one failing, replaces it without reading, or removes one of
    two, or nobody does anything. A's landing page says they are already in the review only
    while it holds every one of them, and that they were there when the retry ran once one is
    gone; it says "Press Read" only when its own Read form sends every one of the resent
    screenshots; otherwise the line stands alone, and no Read form is offered for them as though
    it were."""
    images, expected = RACED[between]
    reader = Reader(monkeypatch)
    reader.answers[BLUE] = ScreenshotAnswer(None, "not_json", 10, 5)
    with here(open_settings(tmp_path)) as browser:
        sent = {**first_form(browser), **READ_NOW}
        answer, _ = uncertain_upload(browser, *images, committed=True, form=sent)
        draft_id = the_open_review(browser)
        review = f"/parent/imports/{draft_id}"
        again = upload(browser, *images, form={**again_form(answer.text), **READ_NOW})
        calls = list(reader.calls)
        stored = images_of(browser, draft_id)
        other = other_browser(browser)
        if between.startswith("the other browser reads"):
            other.post(f"{review}/read", data=read_form(other.get(review).text))
        elif between == "the other browser replaces it":
            replace_with(other, draft_id, stored[0], GREEN)
        elif between == "the other browser removes one":
            assert other.post(f"{review}/images/{stored[1]}/remove").status_code == 303
        page = browser.get(again.headers["location"]).text
        held = images_of(browser, draft_id)

    assert again.status_code == 303, again.text[:400]
    assert calls == []
    assert reader.calls == calls or between.startswith("the other browser reads")
    assert lines_in(PROBLEM.search(page)) == [expected]
    if "Press Read" in expected:
        assert held == stored
        assert read_form(page)["images"] == ",".join(held)
    elif between == "the other browser replaces it":
        assert held[0] != stored[0]
        assert read_form(page)["images"] == held[0]
    elif between == "the other browser reads one":
        assert read_form(page)["images"] == stored[1]
    elif between == "the other browser removes one":
        assert held == stored[:1]
        assert read_form(page)["images"] == stored[0]
    else:
        assert f'action="{review}/read"' not in page


def test_a_note_s_screenshot_hashes_are_read_strictly_and_older_notes_still_read() -> None:
    """A note's fifth part holds the hashes of the screenshots its "Press Read" waits on: kept
    when every one is 64 lowercase hex characters, ignored whole otherwise. Notes of two and four
    parts read as before."""
    key, draft_id = bytes(32), "0" * 32
    digest = "a" * 64

    def signed(parts: list[object]) -> str:
        body = base64.urlsafe_b64encode(json.dumps(parts).encode()).decode()
        return f"{body}.{imports.note_check(key, body)}"

    read = imports.note_read
    assert read(key, draft_id, signed([draft_id, ["a"], ["b"], ["c"], [digest]])).reread == (
        digest,
    )
    for bad in ([digest.upper()], [digest[:-1]], [1], "a" * 64, [digest, None]):
        found = read(key, draft_id, signed([draft_id, ["a"], ["b"], ["c"], bad]))
        assert found == imports.Note(["a"], ["b"], ["c"]), bad
    assert read(key, draft_id, signed([draft_id, ["a"], ["b"], ["c"]])) == imports.Note(
        ["a"], ["b"], ["c"]
    )
    assert read(key, draft_id, signed([draft_id, ["a"]])) == imports.Note(["a"], [], [])


def test_a_retry_s_line_asks_if_its_screenshots_are_held_apart_from_if_read_sends_them() -> None:
    """Held and sent by Read: already in the review, and Press Read. Held, read by then: already
    in the review, alone. One gone: they were in the review when the retry ran, alone. Other
    lines keep their words whether or not the screenshots are held."""
    other = "Screenshot 1 couldn't be uploaded."
    read_again = imports.read_again

    assert read_again([RESENT, other], True, held=True) == [f"{RESENT} {PRESS_READ}", other]
    assert read_again([RESENT_ONE], True, held=True) == [f"{RESENT_ONE} {PRESS_READ_ONE}"]
    assert read_again([RESENT, other], False, held=True) == [RESENT, other]
    assert read_again([RESENT, other], False, held=False) == [WERE_THERE, other]
    assert read_again([RESENT_ONE], False, held=False) == [WAS_THERE]


BUSY_LINE = "Blossom was busy, so nothing was read."
NOT_STARTED_LINE = "Blossom couldn't start reading, so nothing was read."
RACED_BUSY: Final = {
    "nobody touches it": f"{BUSY_LINE} Press Read again.",
    "the other browser reads it": BUSY_LINE,
    "the other browser replaces it": BUSY_LINE,
    "nobody touches it, not started": f"{NOT_STARTED_LINE} Press Read to try again.",
    "the other browser reads it, not started": NOT_STARTED_LINE,
    "replace and read, nobody touches it": f"{BUSY_LINE} Press Read again.",
    "replace and read, the other browser reads it": BUSY_LINE,
}
"""What another browser does between an Upload and read, or a Replace and read, whose read
couldn't begin and its landing, and the line the landing page says."""


@pytest.mark.parametrize("between", list(RACED_BUSY))
def test_an_upload_and_read_that_read_nothing_says_press_read_only_when_read_sends_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, between: str
) -> None:
    """Browser A adds a screenshot with Upload and read, or puts it in Screenshot 1's place with
    Replace and read, and the household's file refuses the read before it begins: busy, or for
    another reason. Before A follows the redirect, browser B reads the new screenshot, replaces it
    without reading, or nobody does anything. A's landing page says "Press Read" only when its own
    Read form sends that screenshot."""
    reader = Reader(monkeypatch)
    replaced = between.startswith("replace and read")
    target = 0 if replaced else 1
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        review = f"/parent/imports/{draft_id}"
        browser.post(f"{review}/read", data=read_form(browser.get(review).text))
        with monkeypatch.context() as patch:
            if between.endswith("not started"):
                patch.setattr(imports, "tables_of", refusing_tables)
            else:
                patch.setattr(imports, "read_named", busy_read)
            if replaced:
                first = images_of(browser, draft_id)[0]
                sent = replace_with(browser, draft_id, first, BLUE, form=READ_NOW)
            else:
                sent = upload(browser, BLUE, form=READ_NOW, to=f"{review}/images")
        calls = list(reader.calls)
        added = images_of(browser, draft_id)
        other = other_browser(browser)
        if "the other browser reads" in between:
            other.post(f"{review}/read", data=read_form(other.get(review).text))
        elif "the other browser replaces" in between:
            replace_with(other, draft_id, added[target], GREEN)
        page = browser.get(sent.headers["location"]).text
        held = images_of(browser, draft_id)

    expected = RACED_BUSY[between]
    assert sent.status_code == 303, sent.text[:400]
    assert calls == [RED]
    assert len(held) == target + 1
    assert expected in lines_in(PROBLEM.search(page))
    if "Press Read" in expected or "the other browser replaces" in between:
        assert read_form(page)["images"] == held[target]
        assert (held[target] == added[target]) == ("Press Read" in expected)
    else:
        assert f'action="{review}/read"' not in page


async def busy_read(*_: object) -> None:
    """A read whose attempt the household's busy file refused before it began."""
    raise StoreBusy


async def refusing_tables(*_: object) -> None:
    """The review's tables, refused by the household's file for a reason that isn't a busy
    file."""
    why = "disk I/O error"
    raise sqlite3.OperationalError(why)


# ------------------------------------------------------------------ what a landing says of the list

AMBER, TEAL = png(color=(220, 150, 30)), png(color=(30, 150, 150))


def one_card(title: str) -> ScreenshotAnswer:
    """A homework list of one card titled ``title``: one assignment of its own."""
    return ScreenshotAnswer(homework_answer(cards=[homework_card(title=title)]), None, 10, 5)


def answered_apart(reader: Reader, *images: bytes) -> None:
    """Each image read as a homework list of its own one card, so every screenshot read adds
    one assignment."""
    for n, image in enumerate(images):
        reader.answers[image] = one_card(f"Worksheet {n + 1}")


def listed(page: str) -> int:
    """How many screenshots the review's list shows."""
    return len(SHOT_LINE.findall(page))


def counts_of(page: str) -> str | None:
    """The review's own "N uploaded · R read" line."""
    found = re.search(r"\d+ uploaded · \d+ read", page)
    return None if found is None else found.group(0)


TWO_READ: Final = "2 screenshots read: 2 assignments found."
ONE_READ: Final = "1 screenshot read: 1 assignment found."
UPLOAD_RACED: Final = {
    "nobody touches the review": (
        ["1 more screenshot uploaded. 2 in this review, 2 read.", TWO_READ],
        "2 uploaded · 2 read",
    ),
    "the other browser removes the first": (
        ["1 more screenshot uploaded. 1 in this review, 1 read.", ONE_READ],
        "1 uploaded · 1 read",
    ),
    "the other browser adds a third without reading": (
        ["1 more screenshot uploaded. 3 in this review, 2 read.", TWO_READ],
        "3 uploaded · 2 read",
    ),
}
"""What another browser does between an Upload and read that adds a second screenshot to a
review holding one read screenshot and its landing, and the status lines and counts the landing
shows: the press's receipt, then totals and read lines that agree with the list."""


@pytest.mark.parametrize("between", list(UPLOAD_RACED))
def test_an_upload_s_landing_keeps_its_receipt_and_counts_the_list_it_shows(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, between: str
) -> None:
    """Browser A's review holds one read screenshot, and A adds a second with Upload and read.
    Before A follows the redirect, browser B removes the first, adds a third without reading, or
    does nothing. A's landing keeps "1 more screenshot uploaded.", and its totals and read lines
    count the screenshots the list shows; the landing calls no reader."""
    expected, counts = UPLOAD_RACED[between]
    reader = Reader(monkeypatch)
    answered_apart(reader, RED, BLUE, GREEN)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        review = f"/parent/imports/{draft_id}"
        browser.post(f"{review}/read", data=read_form(browser.get(review).text))
        [first] = images_of(browser, draft_id)
        sent = upload(browser, BLUE, form=READ_NOW, to=f"{review}/images")
        other = other_browser(browser)
        if between == "the other browser removes the first":
            assert other.post(f"{review}/images/{first}/remove").status_code == 303
        elif between == "the other browser adds a third without reading":
            assert upload(other, GREEN, form={}, to=f"{review}/images").status_code == 303
        calls = list(reader.calls)
        page = browser.get(sent.headers["location"]).text

    assert sent.status_code == 303, sent.text[:400]
    assert calls == [RED, BLUE]
    assert reader.calls == calls
    assert lines_in(TOLD.search(page)) == expected
    assert counts_of(page) == counts
    assert listed(page) == int(counts[0])


FIRST_RACED: Final = {
    "nobody touches it": (["1 screenshot uploaded. None read yet."], "1 uploaded · 0 read"),
    "the other browser reads it": (["1 screenshot uploaded."], "1 uploaded · 1 read"),
}
"""What another browser does between a first upload without reading and its landing, and the
status lines and counts the landing shows."""


@pytest.mark.parametrize("between", list(FIRST_RACED))
def test_a_first_upload_s_landing_says_none_read_yet_only_while_none_is(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, between: str
) -> None:
    """Browser A's first upload stores one screenshot without reading it. Before A follows the
    redirect, browser B reads it, or nobody does anything. A's landing says "None read yet." only
    while the list shows none read; the landing calls no reader."""
    expected, counts = FIRST_RACED[between]
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        sent = upload(browser, RED)
        review = sent.headers["location"]
        other = other_browser(browser)
        if between == "the other browser reads it":
            other.post(f"{review}/read", data=read_form(other.get(review).text))
        calls = list(reader.calls)
        page = browser.get(review).text

    assert sent.status_code == 303, sent.text[:400]
    assert calls == ([RED] if between == "the other browser reads it" else [])
    assert reader.calls == calls
    assert lines_in(TOLD.search(page)) == expected
    assert counts_of(page) == counts
    assert listed(page) == 1


REPLACE_RACED: Final = {
    "nobody touches the review": ("Screenshot 2 was replaced.", 2, 1),
    "the other browser removes screenshot 1": ("Screenshot 1 was replaced.", 1, 0),
    "the other browser removes the new screenshot": ("A screenshot was replaced.", 1, 1),
}
"""What another browser does between a replace of Screenshot 2 and its landing, the line the
landing says first, how many screenshots its list shows, and how many of them are read when the
new screenshot isn't (with Replace and read, every one is)."""


@pytest.mark.parametrize("read", [False, True], ids=["without reading", "and read"])
@pytest.mark.parametrize("between", list(REPLACE_RACED))
def test_a_replace_s_landing_names_the_place_the_new_screenshot_holds_as_it_is_shown(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, between: str, read: bool
) -> None:
    """Browser A's review holds two read screenshots, and A replaces Screenshot 2, with or
    without reading. Before A follows the redirect, browser B removes Screenshot 1, removes the
    new screenshot, or does nothing. A's landing names the new screenshot by the place the list
    shows it in, says a screenshot was replaced when the list doesn't hold it, and Replace and
    read's read lines count the list it shows; the landing calls no reader."""
    expected, count, read_without = REPLACE_RACED[between]
    reader = Reader(monkeypatch)
    answered_apart(reader, RED, BLUE, GREEN)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED, BLUE)
        review = f"/parent/imports/{draft_id}"
        browser.post(f"{review}/read", data=read_form(browser.get(review).text))
        [first, second] = images_of(browser, draft_id)
        sent = replace_with(browser, draft_id, second, GREEN, form=READ_NOW if read else None)
        new = images_of(browser, draft_id)[1]
        other = other_browser(browser)
        if between == "the other browser removes screenshot 1":
            assert other.post(f"{review}/images/{first}/remove").status_code == 303
        elif between == "the other browser removes the new screenshot":
            assert other.post(f"{review}/images/{new}/remove").status_code == 303
        calls = list(reader.calls)
        page = browser.get(sent.headers["location"]).text

    assert sent.status_code == 303, sent.text[:400]
    assert sorted(calls) == sorted([RED, BLUE, GREEN] if read else [RED, BLUE])
    assert reader.calls == calls
    read_lines = [TWO_READ if count == 2 else ONE_READ] if read else []
    assert lines_in(TOLD.search(page)) == [expected, *read_lines]
    assert counts_of(page) == f"{count} uploaded · {count if read else read_without} read"
    assert listed(page) == count


BUSY_TWO: Final = "Blossom was busy and couldn't keep the reading of screenshot 2."
BUSY_UNPLACED: Final = "Blossom was busy and couldn't keep the reading of a screenshot."
BUSY_RACED: Final = {
    "nobody touches it": (f"{BUSY_TWO} Read again.", "it"),
    "the other browser replaces it": (BUSY_UNPLACED, "its replacement"),
    "the other browser reads it": (BUSY_TWO, None),
    "the other browser removes it": (BUSY_UNPLACED, None),
}
"""What another browser does between a press whose new screenshot's reading the busy file
couldn't keep and its landing: the line the landing says, and what its Read form sends (that
screenshot, the file that replaced it, or no Read form at all)."""


def busy_lines(page: str) -> list[str]:
    """The lines of a page's problems saying a reading couldn't be kept."""
    return [line for line in lines_in(PROBLEM.search(page)) if "couldn't keep the reading" in line]


@pytest.mark.parametrize("how", ["upload and read", "replace and read"])
@pytest.mark.parametrize("between", list(BUSY_RACED))
def test_a_reading_the_busy_file_couldn_t_keep_is_named_only_while_the_page_shows_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, between: str, how: str
) -> None:
    """Browser A adds a second screenshot with Upload and read, or puts one in Screenshot 2's
    place with Replace and read, and the busy file keeps none of the new screenshot's reading.
    Before A follows the redirect, browser B replaces that screenshot without reading, reads it,
    removes it, or does nothing. A's landing names it by its place only while the list holds it,
    and says "Read again." only when its own Read form sends it; otherwise it says the reading of a
    screenshot couldn't be kept. The landing calls no reader."""
    expected, sends = BUSY_RACED[between]
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED) if how == "upload and read" else made(browser, RED, BLUE)
        review = f"/parent/imports/{draft_id}"
        browser.post(f"{review}/read", data=read_form(browser.get(review).text))
        with monkeypatch.context() as patch:
            busy_for(patch, browser, None)
            if how == "upload and read":
                sent = upload(browser, GREEN, form=READ_NOW, to=f"{review}/images")
            else:
                second = images_of(browser, draft_id)[1]
                sent = replace_with(browser, draft_id, second, GREEN, form=READ_NOW)
        new = images_of(browser, draft_id)[1]
        other = other_browser(browser)
        if between == "the other browser replaces it":
            assert replace_with(other, draft_id, new, AMBER).status_code == 303
        elif between == "the other browser reads it":
            other.post(f"{review}/read", data=read_form(other.get(review).text))
        elif between == "the other browser removes it":
            assert other.post(f"{review}/images/{new}/remove").status_code == 303
        calls = list(reader.calls)
        page = browser.get(sent.headers["location"]).text
        held = images_of(browser, draft_id)

    earlier = [RED] if how == "upload and read" else [RED, BLUE]
    assert sent.status_code == 303, sent.text[:400]
    assert sorted(calls) == sorted(
        [*earlier, GREEN, *([GREEN] if between.endswith("reads it") else [])]
    )
    assert reader.calls == calls
    assert busy_lines(page) == [expected]
    if sends == "it":
        assert read_form(page)["images"] == new
    elif sends == "its replacement":
        assert held[1] != new
        assert read_form(page)["images"] == held[1]
    else:
        assert f'action="{review}/read"' not in page


def test_a_reading_kept_back_while_its_attempt_can_t_end_offers_no_read_again(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upload and read adds a second screenshot, and the busy file keeps neither its reading nor
    the end of its attempt: the attempt stays current, so the landing names the screenshot with
    no "Read again." and says reading is under way, with no Read form."""
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        review = f"/parent/imports/{draft_id}"
        browser.post(f"{review}/read", data=read_form(browser.get(review).text))
        store = state_of(browser).import_drafts
        with monkeypatch.context() as patch:
            busy_for(patch, browser, None)
            patch.setattr(store, "end_attempt", busy_store(WriterBusy))
            sent = upload(browser, GREEN, form=READ_NOW, to=f"{review}/images")
        calls = list(reader.calls)
        page = browser.get(sent.headers["location"]).text

    assert sent.status_code == 303, sent.text[:400]
    assert calls == [RED, GREEN]
    assert reader.calls == calls
    assert busy_lines(page) == [BUSY_TWO]
    assert "Reading is under way until " in page
    assert f'action="{review}/read"' not in page


@pytest.mark.parametrize("between", list(BUSY_RACED))
def test_a_read_press_names_a_reading_it_couldn_t_keep_by_the_page_it_answers_with(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, between: str
) -> None:
    """A Read press sends Screenshot 2, and the busy file keeps none of its reading. Before the
    press reads the review again for its own page, another browser's change is stood in through
    the store: Screenshot 2 replaced, read, removed, or nothing. The page names it by its place
    only while it holds it, says "Read again." only when its Read form sends it, and otherwise
    says the reading of a screenshot couldn't be kept."""
    expected, sends = BUSY_RACED[between]
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        review = f"/parent/imports/{draft_id}"
        browser.post(f"{review}/read", data=read_form(browser.get(review).text))
        assert upload(browser, BLUE, form={}, to=f"{review}/images").status_code == 303
        form = read_form(browser.get(review).text)
        [_, second] = images_of(browser, draft_id)
        store = state_of(browser).import_drafts
        student = state_of(browser).project_state.student_id()
        real_store, real_named = store.store_reading, imports.read_named

        with monkeypatch.context() as patch:

            async def then_another(*args: Any) -> object:  # noqa: ANN401
                outcome = await real_named(*args)
                if between == "the other browser replaces it":
                    [(_, image)] = prepare_upload([AMBER]).images
                    store.replace_image(student, draft_id, second, image)
                elif between == "the other browser reads it":
                    patch.setattr(store, "store_reading", real_store)
                    await real_named(args[0], student, draft_id, [second])
                elif between == "the other browser removes it":
                    store.remove_image(student, draft_id, second)
                return outcome

            busy_for(patch, browser, None)
            patch.setattr(imports, "read_named", then_another)
            pressed = browser.post(f"{review}/read", data=form)
        held = images_of(browser, draft_id)

    assert pressed.status_code == 200, pressed.text[:400]
    assert form["images"] == second
    assert reader.calls == [RED, BLUE, *([BLUE] if between.endswith("reads it") else [])]
    assert busy_lines(pressed.text) == [expected]
    if sends == "it":
        assert read_form(pressed.text)["images"] == second
    elif sends == "its replacement":
        assert held[1] != second
        assert read_form(pressed.text)["images"] == held[1]
    else:
        assert f'action="{review}/read"' not in pressed.text


def test_a_note_s_press_facts_are_read_strictly_and_older_notes_still_read() -> None:
    """A note's sixth part holds what its press did, as counts, flags and hashes: kept when it is
    an object of known keys with exact types, ignored whole otherwise, with the note's lines read
    either way. Notes of two, four and five parts read as before."""
    key, draft_id = bytes(32), "0" * 32
    digest = "b" * 64
    facts = imports.PressFacts(added=2, first=True, read=True, busy=(digest,), replaced=digest)

    def signed(parts: list[object]) -> str:
        body = base64.urlsafe_b64encode(json.dumps(parts).encode()).decode()
        return f"{body}.{imports.note_check(key, body)}"

    def read(*more: object) -> imports.Note:
        return imports.note_read(key, draft_id, signed([draft_id, ["a"], ["b"], ["c"], *more]))

    whole = {"added": 2, "first": True, "read": True, "busy": [digest], "replaced": digest}
    assert read([], whole) == imports.Note(["a"], ["b"], ["c"], (), facts)
    assert read([digest], {"added": 1}) == imports.Note(
        ["a"], ["b"], ["c"], (digest,), imports.PressFacts(added=1)
    )
    assert read([], {}) == imports.Note(["a"], ["b"], ["c"])
    malformed: tuple[object, ...] = (
        [],
        "added",
        None,
        1,
        {"added": True},
        {"added": 0},
        {"added": -1},
        {"added": "2"},
        {"added": 1.0},
        {"first": 1},
        {"read": "yes"},
        {"busy": [digest.upper()]},
        {"busy": digest},
        {"busy": [digest, 1]},
        {"replaced": digest[:-1]},
        {"replaced": None},
        {"forged": 1},
        {"added": 1, "forged": True},
    )
    for bad in malformed:
        assert read([digest], bad) == imports.Note(["a"], ["b"], ["c"], (digest,)), bad
    assert read([digest]) == imports.Note(["a"], ["b"], ["c"], (digest,))
    assert read() == imports.Note(["a"], ["b"], ["c"])
    assert imports.note_read(key, draft_id, signed([draft_id, ["a"]])) == imports.Note(
        ["a"], [], []
    )
    assert read([], whole, []) == imports.Note([], [], [])
    sent = imports.note_for(key, draft_id, ["a"], facts=facts)
    assert imports.note_read(key, draft_id, sent) == imports.Note(["a"], [], [], (), facts)


def test_a_note_with_every_new_reading_kept_back_fits_one_header_and_says_each(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    """Upload and read adds nine screenshots to a review of one, the busy file keeping none of
    their readings, beside a change's report of two rows with 60-character CJK titles in every
    list. The note's whole Set-Cookie header, its sixth part holding nine hashes, stays within
    4,096 bytes, and the landing says the receipt, each reading kept back by its place with
    "Read again.", and every outcome sentence with its count."""
    reader = Reader(monkeypatch)
    nine = [png(color=(20 * n, 90, 160)) for n in range(1, 10)]
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        review = f"/parent/imports/{draft_id}"
        browser.post(f"{review}/read", data=read_form(browser.get(review).text))
        monkeypatch.setattr(imports, "change_told", lambda *_: two_of_each())
        with monkeypatch.context() as patch:
            busy_for(patch, browser, None)
            sent = upload(browser, *nine, form=READ_NOW, to=f"{review}/images")
        header = note_header(sent)
        calls = list(reader.calls)
        page = browser.get(sent.headers["location"]).text

    record_property("note_header_bytes", len(header.encode()))
    shown = lines_in(PROBLEM.search(page))
    assert sent.status_code == 303, sent.text[:400]
    assert len(header.encode()) <= NOTE_HEADER_MOST, len(header.encode())
    assert sorted(calls) == sorted([RED, *nine])
    assert reader.calls == calls
    assert lines_in(TOLD.search(page))[0] == (
        "9 more screenshots uploaded. 10 in this review, 1 read."
    )
    assert busy_lines(page) == [
        f"Blossom was busy and couldn't keep the reading of screenshot {n}. Read again."
        for n in range(2, 11)
    ]
    assert any(
        [line for line in shown if line in screenshot_page.change_said(two_of_each(), most)]
        == screenshot_page.change_said(two_of_each(), most)
        for most in screenshot_page.NAMED_STEPS
    )


# ------------------------------------------------------------------ a read already under way

UNDER_WAY_PAST: Final = "A read was already under way, so this press didn't start another."
"""What a press that found a read under way says once the page it lands on doesn't show that
same read as current, written out here so a change to its words fails here."""
UNDER_WAY_RACED: Final = (
    "the read is still running",
    "the read ended",
    "the read ran out of time",
    "another read began",
    "the read ended and the other browser read them",
)
"""What happens between a press that found a read under way and its landing: nothing, the read
ends as one whose calls all ended does, its deadline passes on the clock attempts are timed on,
it ends and another begins, or it ends and another browser reads the screenshots."""
OWN_UNDER_WAY: Final = re.compile(r'<p class="note">(Reading is under way until [^<]*)</p>')
"""The review's own line in place of its Read press while a read is current."""


@contextmanager
def here_on(settings: Settings, clock: FakeTime) -> Iterator[TestClient]:
    """The app as ``here`` gives it, with its attempts timed on ``clock``, which the test
    moves by hand."""
    with (
        TestClient(
            create_app(settings, monotonic=clock),
            follow_redirects=False,
            headers={"Host": LOOPBACK, "Origin": f"http://{LOOPBACK}"},
            client=("127.0.0.1", 50000),
        ) as browser,
        import_review_cases.reached_by(browser),
    ):
        yield browser


def shown_under_way(browser: TestClient, draft_id: str) -> str | None:
    """The under-way line for the review's current read, from a fresh snapshot, with the time
    it ends as the review says it; None while no read is current."""
    state = state_of(browser)
    student = state.project_state.student_id()
    ends = state.import_drafts.snapshot(student, draft_id).attempt_ends
    return None if ends is None else imports.under_way(imports.ends_text(state, ends))


def under_way_lines(page: str) -> list[str]:
    """The lines of a page's problems about a read under way."""
    return [line for line in lines_in(PROBLEM.search(page)) if "under way" in line]


@pytest.mark.parametrize("how", ["upload and read", "replace and read"])
@pytest.mark.parametrize("between", UNDER_WAY_RACED)
def test_a_press_that_found_a_read_under_way_says_until_when_only_while_that_read_is_current(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    between: str,
    how: str,
) -> None:
    """A read is forced current over every screenshot of browser A's review, through the store
    as a press starts one, and A adds a screenshot with Upload and read, or puts one in
    Screenshot 2's place with Replace and read: nothing is read. Before A follows the redirect,
    that read keeps running, ends, runs out of time, ends while another begins, or ends and
    browser B reads the screenshots. A's landing says reading is under way until the time the
    page shows only while that same read is current; otherwise it says a read was already under
    way, so the press didn't start another, and offers Read for the unread screenshots whenever
    no read is current. The landing calls no reader."""
    reader = Reader(monkeypatch)
    clock = FakeTime()
    with here_on(open_settings(tmp_path), clock) as browser:
        state = state_of(browser)
        store = state.import_drafts
        student = state.project_state.student_id()
        draft_id = made(browser, RED) if how == "upload and read" else made(browser, RED, BLUE)
        review = f"/parent/imports/{draft_id}"
        attempt = store.start_attempt(student, draft_id, images_of(browser, draft_id))
        assert attempt is not None
        if how == "upload and read":
            sent = upload(browser, GREEN, form=READ_NOW, to=f"{review}/images")
        else:
            second = images_of(browser, draft_id)[1]
            sent = replace_with(browser, draft_id, second, GREEN, form=READ_NOW)
        header = note_header(sent)
        pressed = list(reader.calls)
        if between == "the read ran out of time":
            clock.advance(READ_DEADLINE_SECONDS)
        elif between != "the read is still running":
            assert store.end_attempt(student, draft_id, attempt.attempt_id)
        if between == "another read began":
            clock.advance(1)
            assert store.start_attempt(student, draft_id, images_of(browser, draft_id))
        elif between == "the read ended and the other browser read them":
            clock.advance(1)
            other = other_browser(browser)
            other.post(f"{review}/read", data=read_form(other.get(review).text))
        calls = list(reader.calls)
        shown = {shown_under_way(browser, draft_id)}
        page = browser.get(sent.headers["location"]).text
        shown.add(shown_under_way(browser, draft_id))
        held = images_of(browser, draft_id)

    offered = f'action="{review}/read"' in page
    sends = read_form(page)["images"].split(",") if offered else []
    record_property("problems", lines_in(PROBLEM.search(page)))
    record_property("own_line", OWN_UNDER_WAY.findall(page))
    record_property("read_sends_places", [held.index(image) + 1 for image in sends])
    record_property("reader_calls_before_landing", len(calls))
    record_property("reader_calls_after_landing", len(reader.calls))
    assert sent.status_code == 303, sent.text[:400]
    assert len(header.encode()) <= NOTE_HEADER_MOST
    assert pressed == []
    assert reader.calls == calls
    assert len(held) == 2
    if between == "the read ended and the other browser read them":
        assert sorted(calls) == sorted([RED, GREEN])
    else:
        assert calls == []
    if between == "the read is still running":
        [line] = under_way_lines(page)
        assert line in shown
        assert OWN_UNDER_WAY.findall(page) == [line]
    else:
        assert under_way_lines(page) == [UNDER_WAY_PAST]
    if between == "another read began":
        [line] = OWN_UNDER_WAY.findall(page)
        assert line in shown
    elif between != "the read is still running":
        assert OWN_UNDER_WAY.findall(page) == []
    if between in ("the read ended", "the read ran out of time"):
        assert read_form(page)["images"] == ",".join(held)
    else:
        assert f'action="{review}/read"' not in page


def test_a_note_s_under_way_deadline_is_read_strictly() -> None:
    """A note's facts may hold the deadline of the read under way that kept its press from
    starting one, on the clock attempts are timed on: kept when it is a finite float, the whole
    part ignored otherwise, with the note's lines and hashes read either way."""
    key, draft_id = bytes(32), "0" * 32
    digest = "c" * 64
    facts = imports.PressFacts(added=1, under_way=1120.0)

    def signed(parts: list[object]) -> str:
        body = base64.urlsafe_b64encode(json.dumps(parts).encode()).decode()
        return f"{body}.{imports.note_check(key, body)}"

    def read(*more: object) -> imports.Note:
        return imports.note_read(key, draft_id, signed([draft_id, ["a"], ["b"], ["c"], *more]))

    assert read([], {"added": 1, "under_way": 1120.0}) == imports.Note(
        ["a"], ["b"], ["c"], (), facts
    )
    assert read([digest], {"under_way": 0.0}) == imports.Note(
        ["a"], ["b"], ["c"], (digest,), imports.PressFacts(under_way=0.0)
    )
    malformed: tuple[object, ...] = (
        {"under_way": 1120},
        {"under_way": True},
        {"under_way": "1120.0"},
        {"under_way": None},
        {"under_way": [1120.0]},
        {"under_way": float("nan")},
        {"under_way": float("inf")},
        {"under_way": float("-inf")},
        {"under_way": 1120.0, "forged": 1},
        {"added": 0, "under_way": 1120.0},
    )
    for bad in malformed:
        assert read([digest], bad) == imports.Note(["a"], ["b"], ["c"], (digest,)), bad
    for sent in (facts, imports.PressFacts(under_way=0.0)):
        value = imports.note_for(key, draft_id, ["a"], facts=sent)
        assert imports.note_read(key, draft_id, value) == imports.Note(["a"], [], [], (), sent)


WRITTEN_UNDER_WAY: Final = "Reading is under way until Monday, November 23, 12:00 PM."
FIRST_RECEIPT: Final = "1 screenshot uploaded. None read yet."
OLDER_NOTES: Final = {
    "with the press's facts": ([], imports.PressFacts(added=1, first=True)),
    "with its receipt written": ([FIRST_RECEIPT], imports.NO_FACTS),
}
"""Notes written with the under-way sentence among their problem lines: one whose press's facts
carry no deadline, and one with no facts and its receipt written out; each with its status lines
and its facts."""


@pytest.mark.parametrize("written", list(OLDER_NOTES))
def test_an_older_note_s_under_way_line_shows_once_as_it_was_written(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, written: str
) -> None:
    """A note whose lines already hold "Reading is under way until ..." and whose facts carry no
    deadline: with no read current, the review shows that line once, as written, works out no
    under-way line of its own and calls no reader."""
    told, facts = OLDER_NOTES[written]
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        review = f"/parent/imports/{draft_id}"
        key = state_of(browser).result_key
        note = imports.note_for(key, draft_id, [WRITTEN_UNDER_WAY], told, facts=facts)
        cookie = f"{imports.NOTE_COOKIE}={note}".encode()
        page = other_browser(browser).get(review, headers={b"Cookie": cookie}).text
        held = images_of(browser, draft_id)

    assert reader.calls == []
    assert under_way_lines(page) == [WRITTEN_UNDER_WAY]
    assert page.count(WRITTEN_UNDER_WAY) == 1
    assert OWN_UNDER_WAY.findall(page) == []
    assert lines_in(TOLD.search(page)) == [FIRST_RECEIPT]
    assert read_form(page)["images"] == ",".join(held)


# ------------------------------------------------------------------ a screenshot not sent

UNSENT_TWO: Final = "Blossom was busy, so screenshot 2 wasn't sent to be read."
UNSENT_THREE: Final = "Blossom was busy, so screenshot 3 wasn't sent to be read."
SEND_IT: Final = "Press Read to send it."
UNSENT_UNPLACED: Final = "Blossom was busy, so a screenshot wasn't sent to be read."
UNSENT_TWO_GONE: Final = "Blossom was busy, so 2 screenshots weren't sent to be read."
"""What a page says of a screenshot a busy store kept its press from sending, written out here so
a change to its words fails here."""
BUSY_AT: Final = {
    "while its image loads": (0, f"{UNSENT_TWO} {SEND_IT}"),
    "while its reading is kept": (1, f"{BUSY_TWO} Read again."),
}
"""Where the store is busy as a press reads its new screenshot: the reader calls made for it, and
the line the page says."""
PRESSES: Final = ["upload and read", "replace and read", "read"]


def busy_at(patch: pytest.MonkeyPatch, browser: TestClient, lock: str) -> None:
    """While ``patch`` holds, the store is busy at ``lock``: as a read loads a screenshot's
    prepared PNG, before any reader call, or as it keeps the reading the reader gave."""
    if lock == "while its image loads":
        patch.setattr(state_of(browser).import_drafts, "image_png", busy_store(StoreBusy))
    else:
        busy_for(patch, browser, None)


def busy_problems(page: str) -> list[str]:
    """The lines of a page's problems that say Blossom was busy."""
    return [line for line in lines_in(PROBLEM.search(page)) if line.startswith("Blossom was busy")]


@pytest.mark.parametrize("lock", list(BUSY_AT))
@pytest.mark.parametrize("how", PRESSES)
def test_a_busy_store_says_whether_the_screenshot_was_sent_to_be_read(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    how: str,
    lock: str,
) -> None:
    """Upload and read adds a second screenshot, Replace and read puts one in Screenshot 2's
    place, or a Read press sends Screenshot 2, and the store is busy for that press alone. Busy as
    the screenshot's image loads, no reader call is made for it, and the page says it wasn't sent
    to be read, with "Press Read to send it."; busy as its reading is kept, one call is made and
    the page says the reading couldn't be kept, with "Read again.". Either way no reading is kept
    for it, its Read form sends it, and a redirect's whole note header stays within 4,096
    bytes."""
    for_it, expected = BUSY_AT[lock]
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED, BLUE) if how == "replace and read" else made(browser, RED)
        review = f"/parent/imports/{draft_id}"
        browser.post(f"{review}/read", data=read_form(browser.get(review).text))
        form: dict[str, str] = {}
        if how == "read":
            assert upload(browser, GREEN, form={}, to=f"{review}/images").status_code == 303
            form = read_form(browser.get(review).text)
        with monkeypatch.context() as patch:
            busy_at(patch, browser, lock)
            if how == "upload and read":
                sent = upload(browser, GREEN, form=READ_NOW, to=f"{review}/images")
            elif how == "replace and read":
                second = images_of(browser, draft_id)[1]
                sent = replace_with(browser, draft_id, second, GREEN, form=READ_NOW)
            else:
                sent = browser.post(f"{review}/read", data=form)
        calls = list(reader.calls)
        header = "" if how == "read" else note_header(sent)
        page = sent.text if how == "read" else browser.get(sent.headers["location"]).text
        student = state_of(browser).project_state.student_id()
        stored = state_of(browser).import_drafts.snapshot(student, draft_id)

    new = stored.images[1]
    offered = f'action="{review}/read"' in page
    record_property("problem_lines", busy_problems(page))
    record_property("read_form_ids", read_form(page)["images"] if offered else None)
    record_property("new_image_id", new.image_id)
    record_property("calls_for_it", calls.count(GREEN))
    record_property("note_header_bytes", len(header.encode()))
    earlier = [RED, BLUE] if how == "replace and read" else [RED]
    assert sent.status_code == (200 if how == "read" else 303), sent.text[:400]
    assert sorted(calls) == sorted([*earlier, *([GREEN] * for_it)])
    assert reader.calls == calls
    assert not new.read
    assert new.sha256 not in {reading.sha256 for reading in stored.readings}
    assert busy_problems(page) == [expected]
    assert read_form(page)["images"] == new.image_id
    assert len(header.encode()) <= NOTE_HEADER_MOST, len(header.encode())


UNSENT_RACED: Final = {
    "nobody touches it": (f"{UNSENT_TWO} {SEND_IT}", "it"),
    "the other browser replaces it": (UNSENT_UNPLACED, "its replacement"),
    "the other browser reads it": (UNSENT_TWO, None),
    "the other browser removes it": (UNSENT_UNPLACED, None),
}
"""What another browser does between a press whose new screenshot a busy store kept from being
sent and its landing: the line the landing says, and what its Read form sends (that screenshot,
the file that replaced it, or no Read form at all)."""


@pytest.mark.parametrize("how", ["upload and read", "replace and read"])
@pytest.mark.parametrize("between", list(UNSENT_RACED))
def test_a_screenshot_not_sent_is_named_only_while_the_page_shows_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, between: str, how: str
) -> None:
    """Browser A adds a second screenshot with Upload and read, or puts one in Screenshot 2's
    place with Replace and read, and the store is busy as its image loads, so it isn't sent.
    Before A follows the redirect, browser B replaces that screenshot without reading, reads it,
    removes it, or does nothing. A's landing names it by its place only while the list holds it,
    and says "Press Read to send it." only when its own Read form sends it; otherwise it says a
    screenshot wasn't sent to be read. The landing calls no reader."""
    expected, sends = UNSENT_RACED[between]
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED) if how == "upload and read" else made(browser, RED, BLUE)
        review = f"/parent/imports/{draft_id}"
        browser.post(f"{review}/read", data=read_form(browser.get(review).text))
        with monkeypatch.context() as patch:
            busy_at(patch, browser, "while its image loads")
            if how == "upload and read":
                sent = upload(browser, GREEN, form=READ_NOW, to=f"{review}/images")
            else:
                second = images_of(browser, draft_id)[1]
                sent = replace_with(browser, draft_id, second, GREEN, form=READ_NOW)
        new = images_of(browser, draft_id)[1]
        other = other_browser(browser)
        if between == "the other browser replaces it":
            assert replace_with(other, draft_id, new, AMBER).status_code == 303
        elif between == "the other browser reads it":
            other.post(f"{review}/read", data=read_form(other.get(review).text))
        elif between == "the other browser removes it":
            assert other.post(f"{review}/images/{new}/remove").status_code == 303
        calls = list(reader.calls)
        page = browser.get(sent.headers["location"]).text
        held = images_of(browser, draft_id)

    earlier = [RED] if how == "upload and read" else [RED, BLUE]
    assert sent.status_code == 303, sent.text[:400]
    assert sorted(calls) == sorted([*earlier, *([GREEN] if between.endswith("reads it") else [])])
    assert reader.calls == calls
    assert busy_problems(page) == [expected]
    if sends == "it":
        assert read_form(page)["images"] == new
    elif sends == "its replacement":
        assert held[1] != new
        assert read_form(page)["images"] == held[1]
    else:
        assert f'action="{review}/read"' not in page


UNSENT_TWO_RACED: Final = {
    "nobody touches them": [f"{UNSENT_TWO} {SEND_IT}", f"{UNSENT_THREE} {SEND_IT}"],
    "the other browser removes both": [UNSENT_TWO_GONE],
}
"""What another browser does between an Upload and read whose two new screenshots a busy store
kept from being sent and its landing, and the lines the landing says."""


@pytest.mark.parametrize("between", list(UNSENT_TWO_RACED))
def test_screenshots_not_sent_are_named_in_order_or_counted_once_gone(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, between: str
) -> None:
    """Upload and read adds two screenshots to a review of one, and the store is busy as their
    images load, so neither is sent. With nobody touching them, the landing names each by its
    place, in order, with "Press Read to send it.", and its Read form sends both; once another
    browser removes both, it counts them with no place and nothing to do."""
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        review = f"/parent/imports/{draft_id}"
        browser.post(f"{review}/read", data=read_form(browser.get(review).text))
        with monkeypatch.context() as patch:
            busy_at(patch, browser, "while its image loads")
            sent = upload(browser, GREEN, AMBER, form=READ_NOW, to=f"{review}/images")
        [_, *new] = images_of(browser, draft_id)
        if between == "the other browser removes both":
            other = other_browser(browser)
            for image_id in new:
                assert other.post(f"{review}/images/{image_id}/remove").status_code == 303
        page = browser.get(sent.headers["location"]).text

    assert sent.status_code == 303, sent.text[:400]
    assert len(new) == 2
    assert reader.calls == [RED]
    assert busy_problems(page) == UNSENT_TWO_RACED[between]
    if between == "nobody touches them":
        assert read_form(page)["images"] == ",".join(new)
    else:
        assert f'action="{review}/read"' not in page


@pytest.mark.parametrize("between", list(UNSENT_RACED))
def test_a_read_press_names_a_screenshot_it_didn_t_send_by_the_page_it_answers_with(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, between: str
) -> None:
    """A Read press would send Screenshot 2, and the store is busy as its image loads, so it isn't
    sent. Before the press reads the review again for its own page, another browser's change is
    stood in through the store: Screenshot 2 replaced, read, removed, or nothing. The page names it
    by its place only while it holds it, says "Press Read to send it." only when its Read form
    sends it, and otherwise says a screenshot wasn't sent to be read."""
    expected, sends = UNSENT_RACED[between]
    reader = Reader(monkeypatch)
    with here(open_settings(tmp_path)) as browser:
        draft_id = made(browser, RED)
        review = f"/parent/imports/{draft_id}"
        browser.post(f"{review}/read", data=read_form(browser.get(review).text))
        assert upload(browser, BLUE, form={}, to=f"{review}/images").status_code == 303
        form = read_form(browser.get(review).text)
        [_, second] = images_of(browser, draft_id)
        store = state_of(browser).import_drafts
        student = state_of(browser).project_state.student_id()
        real_png, real_named = store.image_png, imports.read_named

        with monkeypatch.context() as patch:

            async def then_another(*args: Any) -> object:  # noqa: ANN401
                outcome = await real_named(*args)
                if between == "the other browser replaces it":
                    [(_, image)] = prepare_upload([AMBER]).images
                    store.replace_image(student, draft_id, second, image)
                elif between == "the other browser reads it":
                    patch.setattr(store, "image_png", real_png)
                    await real_named(args[0], student, draft_id, [second])
                elif between == "the other browser removes it":
                    store.remove_image(student, draft_id, second)
                return outcome

            busy_at(patch, browser, "while its image loads")
            patch.setattr(imports, "read_named", then_another)
            pressed = browser.post(f"{review}/read", data=form)
        held = images_of(browser, draft_id)

    assert pressed.status_code == 200, pressed.text[:400]
    assert form["images"] == second
    assert reader.calls == [RED, *([BLUE] if between.endswith("reads it") else [])]
    assert busy_problems(pressed.text) == [expected]
    if sends == "it":
        assert read_form(pressed.text)["images"] == second
    elif sends == "its replacement":
        assert held[1] != second
        assert read_form(pressed.text)["images"] == held[1]
    else:
        assert f'action="{review}/read"' not in pressed.text


def test_a_note_s_unsent_screenshots_are_read_strictly() -> None:
    """A note's facts may hold the hashes of the screenshots a busy store kept its press from
    sending: kept when they are a list of 64 lowercase hex characters each, the whole part ignored
    otherwise, with the note's lines and hashes read either way. Facts without them read as
    before."""
    key, draft_id = bytes(32), "0" * 32
    digest = "d" * 64
    facts = imports.PressFacts(added=1, read=True, busy=(digest,), unsent=(digest,))

    def signed(parts: list[object]) -> str:
        body = base64.urlsafe_b64encode(json.dumps(parts).encode()).decode()
        return f"{body}.{imports.note_check(key, body)}"

    def read(*more: object) -> imports.Note:
        return imports.note_read(key, draft_id, signed([draft_id, ["a"], ["b"], ["c"], *more]))

    whole = {"added": 1, "read": True, "busy": [digest], "unsent": [digest]}
    assert read([], whole) == imports.Note(["a"], ["b"], ["c"], (), facts)
    assert read([digest], {"unsent": [digest, digest]}) == imports.Note(
        ["a"], ["b"], ["c"], (digest,), imports.PressFacts(unsent=(digest, digest))
    )
    malformed: tuple[object, ...] = (
        {"unsent": [digest.upper()]},
        {"unsent": [digest[:-1]]},
        {"unsent": digest},
        {"unsent": [digest, 1]},
        {"unsent": None},
        {"unsent": {digest: 1}},
        {"unsent": [digest], "forged": 1},
        {"unsent": [digest], "busy": [1]},
        {"unsent": [digest], "added": 0},
    )
    for bad in malformed:
        assert read([digest], bad) == imports.Note(["a"], ["b"], ["c"], (digest,)), bad
    assert read([], {"added": 1, "read": True, "busy": [digest]}) == imports.Note(
        ["a"], ["b"], ["c"], (), imports.PressFacts(added=1, read=True, busy=(digest,))
    )
    for sent in (facts, imports.PressFacts(unsent=(digest,))):
        value = imports.note_for(key, draft_id, ["a"], facts=sent)
        assert imports.note_read(key, draft_id, value) == imports.Note(["a"], [], [], (), sent)
