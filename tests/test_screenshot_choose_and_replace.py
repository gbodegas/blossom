# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The screenshot upload forms' two presses, and what each press reads, through the routes with
the stand-in reader and made-up images only.

"Upload without reading" stores and reads nothing. "Upload and read" stores, then reads exactly
the images that press added. The review's Read press names each screenshot it sends and reads
exactly those.

Choosing sends nothing: each picker carries a line that says so, a list the page script fills
with what was chosen, and the page script holds no network call and no submit call. Each press
carries its own busy words, and the busy-text handler keeps a named press's value.

Each screenshot on the review says in words how it stands and why. One that needs attention stays
in view above the review; one read with homework folds. The missing-name question leaves out a
screenshot with nothing to save and names the ones it covers. Before a save, a line beside Save
says saving fixes the screenshots; after one, a screenshot that needs attention says what can be
done. No press but a read calls the reader.
"""

import html
import pathlib
import re
from typing import Any, get_args

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.anthropic_client import ReadFailure, ScreenshotAnswer
from blossom.routes import imports
from tests.support import Answer, homework_answer, homework_card, state_of
from tests.test_import_routes import (
    BLUE,
    GREEN,
    PROBLEM,
    RED,
    TOLD,
    Reader,
    drafts,
    first_form,
    followed,
    frozen_after_a_partial_save,
    gif,
    her_name_answered,
    here,
    images_of,
    lines_in,
    made,
    open_settings,
    page_post,
    png,
    press_outside,
    read_form,
    read_review,
    says,
    send,
    token_of,
    uncertain_upload,
    units_on,
    unnamed,
    upload,
)

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
        "Nothing is sent until you press Read.",
    )
    assert says(
        page,
        "Read sends Screenshot 2 and Screenshot 3 to Anthropic's API to be read. "
        "Nothing is sent until you press Read.",
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
    "Saving fixes this review's screenshots, so remove or replace Screenshot 2 first to fix it "
    "here."
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
    assert "Saving fixes" not in page


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
    assert "Saving fixes" not in page
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
