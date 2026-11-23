# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The screenshot upload forms' two presses, and what each press reads, through the routes with
the stand-in reader and made-up images only.

"Upload without reading" stores and reads nothing. "Upload and read" stores, then reads exactly
the images that press added. The review's Read press names each screenshot it sends and reads
exactly those.
"""

import html
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from blossom.anthropic_client import ScreenshotAnswer
from blossom.routes import imports
from tests.support import Answer, homework_answer, state_of
from tests.test_import_routes import (
    BLUE,
    GREEN,
    PROBLEM,
    RED,
    TOLD,
    Reader,
    drafts,
    first_form,
    frozen_after_a_partial_save,
    gif,
    here,
    images_of,
    lines_in,
    made,
    open_settings,
    png,
    read_form,
    says,
    token_of,
    uncertain_upload,
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

UPLOAD_FORM = re.compile(
    r'<form method="post" action="(/parent/imports(?:/[0-9a-f]{32}/images)?)" '
    r'enctype="multipart/form-data">(.*?)</form>',
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
    return [found.group(2) for found in UPLOAD_FORM.finditer(page)]


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
    it; Upload without reading second, secondary, with no name, described by its own line."""
    presses = presses_of(form)
    assert len(presses) == 2, form
    first, second = presses
    read = re.search(
        r'<button type="submit" class="primary" name="read" value="now" '
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
        r'<button type="submit" class="secondary" '
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
    assert "data-pending" not in form


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
