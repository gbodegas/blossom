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
import sqlite3
import traceback
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, timedelta
from io import BytesIO
from typing import Any, Final, NamedTuple
from urllib.parse import unquote_plus, urlencode

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from blossom import screenshot_posts, screenshot_reading
from blossom.anthropic_client import ScreenshotAnswer
from blossom.app import create_app
from blossom.school_instructions import to_wire
from blossom.screenshot_page import page_check
from blossom.screenshot_review import field_most
from blossom.screenshot_save import correction_digest, read_digest
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
MOST_UNPACKED: Final = 1_073_741_824
"""The limits every post to a review's route is read within, as literal numbers, so a test holds a
limit that changes rather than reading it from the code."""
LOOPBACK: Final = "127.0.0.1:8781"
FORM: Final = {"content-type": "application/x-www-form-urlencoded"}
TOO_LARGE: Final = (
    "This review is too large for Blossom to process. Nothing was kept or saved by this attempt."
)
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


BROWSERS: Final[list[TestClient]] = []
"""The app each open ``here`` reaches, the innermost last."""


@contextmanager
def reached_by(browser: TestClient) -> Iterator[TestClient]:
    """This app, the one a helper's post opens its cards through while it is open."""
    BROWSERS.append(browser)
    try:
        yield browser
    finally:
        BROWSERS.remove(browser)


@contextmanager
def here(settings: Settings) -> Iterator[TestClient]:
    """The app as this computer's browser reaches it with the sign-in off."""
    with (
        TestClient(
            create_app(settings),
            follow_redirects=False,
            headers={"Host": LOOPBACK, "Origin": f"http://{LOOPBACK}"},
            client=("127.0.0.1", 50000),
        ) as browser,
        reached_by(browser),
    ):
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


def no_date_subjects(page: str) -> list[str]:
    """The subject of each assigned date box's "No assigned date was given" on this page's review
    form, in page order, its card open (the checkbox shown) or closed (the assigned date the card
    carries): every assigned date box has one."""
    return [
        subject
        for subject in fields_named(review_form(page), "correct:")
        if json.loads(subject)[1] == "assigned"
    ]


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


class Post(list[tuple[str, str]]):
    """A review post a helper made from a page, with that page, the closed cards its typing or
    choices land in (``closed``, each by its "Correct what was read" press), how to make the
    same post from another page (``remade``), the typing it was made with (``typed``) and the
    names it chose (``chosen``). No helper opens a card on its own: a test opens one by its own
    press (``open_cards``) and makes the post from the page that press returns. A post whose
    typing or choice isn't in it fails its test as it is sent (``carried``)."""

    def __init__(
        self,
        fields: Iterable[tuple[str, str]],
        page: str,
        closed: Sequence[str],
        remade: Callable[[str], "Post"],
        typed: Mapping[str, str] | None = None,
        chosen: Iterable[str] = (),
    ) -> None:
        super().__init__(fields)
        self.made = tuple(self)
        self.page = page
        self.closed = tuple(closed)
        self.remade = remade
        self.typed = dict(typed or {})
        self.chosen = tuple(chosen)


class RawPost(list[tuple[str, str]]):
    """A submission no page offers, sent on purpose and exactly as it stands: ``raw_post`` makes
    one from a helper's post, stating every change, and ``unoffered`` from a Save its page does
    not offer."""


class Reviewed(list[tuple[str, str]]):
    """A post whose earlier presses were already made (``reviewed``), sent as given: a helper's
    post or a raw post, checked as ``carried`` checks each. A plain list fails the test."""

    def __init__(self, fields: Fields) -> None:
        carried(fields)
        super().__init__(fields)


def the_call() -> str:
    """The file, line and function of the test code a post came from, past this module and the
    thin wrappers that pass a post on."""
    passing_on = {"send", "posted", "as_a_browser_sends", "<lambda>"}
    for frame in reversed(traceback.extract_stack()):
        if pathlib.Path(frame.filename).name == pathlib.Path(__file__).name:
            continue
        if frame.name not in passing_on:
            return f"{pathlib.Path(frame.filename).name}:{frame.lineno} in {frame.name}"
    return "a call outside the tests"


def carried(fields: Fields) -> None:
    """The one gate every test post passes, closed to all but three kinds: a helper's post
    (``Post``) as it was made, a post already reviewed (``Reviewed``) and a raw post (``RawPost``).
    Anything else, a plain list made from a post included, fails the test at once, naming the
    call. A helper's post fails the same way where it was changed after it was made, and where it
    doesn't carry what its test typed or chose, naming the field and the page the post was made
    from: typing in a box that page offers only in a closed card the test did not open, typing in
    a box the page does not offer, typing the post sends another value for, and "No assigned date
    was given" chosen in a closed card."""
    if isinstance(fields, Reviewed | RawPost):
        return
    if not isinstance(fields, Post):
        pytest.fail(
            f"{the_call()} sends a plain list, not a post a helper made: send the helper's post "
            "as made, or state each change with raw_post.",
            pytrace=False,
        )
    if tuple(fields) != fields.made:
        pytest.fail(
            f"{the_call()} sends a helper's post changed after it was made: state each change "
            "with raw_post.",
            pytrace=False,
        )
    form = review_form(fields.page)
    boxes: set[str] = set()
    hidden: dict[str, str] = {}
    shown: set[str] = set()
    for tag in re.findall(r"<input [^>]*>", form):
        name, kind = attribute(tag, "name") or "", attribute(tag, "type")
        shown.add(name)
        if kind == "hidden":
            hidden[name] = attribute(tag, "value") or ""
        elif kind in ("text", "date"):
            boxes.add(name)
    boxes.update(attribute(tag, "name") or "" for tag in re.findall(r"<textarea [^>]*>", form))
    title = re.search(r"<title>(.*?)</title>", fields.page, re.S)
    action = re.search(r'action="([^"]+/review)"', form)
    page = (
        f"{html.unescape(title.group(1)).strip() if title else 'a page with no title'}, its review "
        f"form posting to {action.group(1) if action else 'no address'}"
    )
    missed: list[tuple[str, str]] = []
    for name, value in fields.typed.items():
        if name in boxes and (name, value) in fields:
            continue
        if name not in boxes and hidden.get(name) == value and (name, value) in fields:
            continue
        if name in boxes:
            missed.append((name, "typing the post sends another value for"))
        elif name.startswith("correct:") and name in hidden:
            missed.append((name, "a closed card the test did not open"))
        else:
            missed.append((name, "a box the page does not offer"))
    for name in fields.chosen:
        subject = name.removeprefix("none:")
        if name.startswith("none:") and name not in shown and f"correct:{subject}" in shown:
            missed.append((name, "a closed card the test did not open"))
    if missed:
        pytest.fail(
            "; ".join(
                f"{name!r} does not reach the post it sends: {why}, on the page {page}"
                for name, why in missed
            ),
            pytrace=False,
        )


def closed_cards(form: str, typed: Mapping[str, str], chosen: Iterable[str]) -> list[str]:
    """The "Correct what was read" press of each closed card on this form that a post types into
    or chooses in, in page order: a box the card carries hidden with another value typed into it,
    or a "No assigned date was given" choice the form doesn't show. A closed card's carried boxes
    sit before its press."""
    presses = [
        (found.start(), html.unescape(found.group(1)))
        for found in re.finditer(r'name="press" value="(correct:[^"]+)"', form)
    ]
    shown = {attribute(tag, "name") or "" for tag in re.findall(r"<input [^>]*>", form)}
    wanted = [
        name
        for name in chosen
        if name.startswith("none:") and name not in shown and f"correct:{name[5:]}" in shown
    ]
    cards: list[str] = []
    for found in re.finditer(r"<input [^>]*>", form):
        tag = found.group(0)
        name = attribute(tag, "name") or ""
        if attribute(tag, "type") != "hidden" or not name.startswith("correct:"):
            continue
        typed_in = name in typed and typed[name] != (attribute(tag, "value") or "")
        if not typed_in and f"none:{name.removeprefix('correct:')}" not in wanted:
            continue
        after = [press for start, press in presses if start > found.start()]
        if after and after[0] not in cards:
            cards.append(after[0])
    return [press for _, press in presses if press in cards]


def page_post(
    page: str,
    press: str,
    *,
    choose: Mapping[str, str] | None = None,
    tick: Sequence[str] = (),
    typed: Mapping[str, str] | None = None,
) -> Post:
    """The review form's fields as a browser sends them with scripts off: the hidden fields, the
    checked controls (or those chosen here), the text in its boxes and text areas, the groups
    ticked, the button. Typing or a choice in a closed card isn't in it: a test opens the card
    by its own press first (``open_cards``), and ``carried`` fails the test otherwise."""
    choose, typed = dict(choose or {}), dict(typed or {})

    def remade(other: str) -> Post:
        return page_post(other, press, choose=choose, tick=tick, typed=typed)

    fields = page_fields(page, press, choose, tick, typed)
    return Post(fields, page, closed_cards(review_form(page), typed, choose), remade, typed, choose)


def open_cards(browser: TestClient, draft_id: str, page: str, cards: Sequence[str]) -> str:
    """The review after "Correct what was read" pressed on each of these cards (each by its
    press's value), in turn, each from the page the one before returned, as the family presses
    them: the page the last press returns. Each press carries the form as its page stands, as a
    browser sends it with that button: the hidden fields, the boxes, every checked control and
    every group box the page shows ticked. Each press writes nothing; one that doesn't come back
    with the review form and 200 fails the test, naming the card."""
    for card in cards:
        ticked = [
            attribute(tag, "value") or ""
            for tag in re.findall(r"<input [^>]*>", review_form(page))
            if attribute(tag, "name") == "unit" and " checked" in tag
        ]
        answer = posted(browser, draft_id, page_post(page, card, tick=ticked))
        if answer.status_code != 200 or not has_review_form(answer.text):
            pytest.fail(
                f"Correct what was read on {card!r} came back {answer.status_code} with no "
                f"review form: {page_text(answer.text)[:300]}",
                pytrace=False,
            )
        page = answer.text
    return page


def page_fields(
    page: str,
    press: str,
    choose: Mapping[str, str],
    tick: Sequence[str],
    typed: Mapping[str, str],
) -> list[tuple[str, str]]:
    """``page_post``'s fields as this page gives them."""
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
    page: str,
    press: str,
    typed: Mapping[str, str] | None = None,
    *,
    none_ticked: bool = False,
    ticks: Sequence[str] | None = None,
) -> Post:
    """The most a browser sends from this page: the hidden fields, the boxes, every checkbox, one
    radio of each group as shown, a Type of Task, every group's tick (or only the groups of
    ``ticks``), the button. Without a conflict unless asked for: "None of these apply" and "No
    assigned date was given" stay unticked. The typing, or a "No assigned date was given" asked
    for, in a closed card isn't in it: a test opens the card by its own press first
    (``open_cards``), and ``carried`` fails the test otherwise."""
    typed = dict(typed or {})

    def remade(other: str) -> Post:
        return every_choice(other, press, typed, none_ticked=none_ticked, ticks=ticks)

    fields = every_field(page, press, typed, none_ticked, ticks)
    nones = [f"none:{subject}" for subject in no_date_subjects(page)] if none_ticked else []
    return Post(fields, page, closed_cards(review_form(page), typed, nones), remade, typed, nones)


def every_field(
    page: str,
    press: str,
    typed: Mapping[str, str],
    none_ticked: bool,
    ticks: Sequence[str] | None = None,
) -> list[tuple[str, str]]:
    """``every_choice``'s fields as this page gives them."""
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
            chosen_group = ticks is None or name != "unit"
            if chosen_group and (none_ticked or not says_none):
                fields.append((name, value))
        elif kind == "radio" and (name not in radios or " checked" in tag):
            radios[name] = "TASK" if name.startswith("answer:kind:") else value
    for tag, content in re.findall(r"(<textarea [^>]*>)(.*?)</textarea>", form, re.S):
        name = attribute(tag, "name") or ""
        shown = html.unescape(content)
        fields.append((name, typed.get(name, shown[1:] if shown.startswith("\n") else shown)))
    return [*fields, *radios.items(), *(("unit", tick) for tick in ticks or ()), ("press", press)]


def is_instruction(name: str) -> bool:
    return name.startswith("answer:instructions:")


def as_a_browser_sends(fields: Fields) -> bytes:
    """A form's fields serialized as a browser submits them: every line break in a name or a
    value, CR, LF or CRLF, becomes CRLF, then the form is URL-encoded. A helper's post is checked
    to carry what its test typed and chose first (``carried``)."""
    carried(fields)

    def lines(text: str) -> str:
        return re.sub(r"\r\n|\r|\n", "\r\n", text)

    return urlencode([(lines(name), lines(value)) for name, value in fields]).encode()


def posted(browser: TestClient, draft_id: str, fields: Fields) -> Answer:
    return browser.post(
        f"/parent/imports/{draft_id}/review", content=as_a_browser_sends(fields), headers=FORM
    )


def every_card_open(browser: TestClient, draft_id: str, page: str) -> str:
    """The review as the family sees it after pressing "Correct what was read" on each closed
    card in turn, for a test that looks for a box or a choice inside a card: each press writes
    nothing and carries the group boxes its page shows ticked, as ``open_cards`` does. The page as
    given where a press doesn't come back with the review form."""
    for card in every_press_to_correct(page):
        ticked = [
            attribute(tag, "value") or ""
            for tag in re.findall(r"<input [^>]*>", review_form(page))
            if attribute(tag, "name") == "unit" and " checked" in tag
        ]
        answer = posted(browser, draft_id, page_post(page, card, tick=ticked))
        if answer.status_code != 200 or not has_review_form(answer.text):
            return page
        page = answer.text
    return page


def every_press_to_correct(page: str) -> list[str]:
    """Each closed card's "Correct what was read" press on the review form, in page order."""
    return [
        html.unescape(press)
        for press in re.findall(r'name="press" value="(correct:[^"]+)"', review_form(page))
    ]


def send(browser: TestClient, draft_id: str, fields: Fields) -> Answer:
    """The post sent to the review exactly as given: no card opened and no press added. A
    helper's post is checked to carry what its test typed and chose (``carried``)."""
    return posted(browser, draft_id, fields)


def unoffered_save(
    browser: TestClient, draft_id: str, page: str, units: Sequence[str]
) -> tuple[list[tuple[str, str]], Answer]:
    """A Save this page does not offer (a card's boxes open on it), sent on purpose as a raw POST:
    the page's own fields with nothing typed or chosen, these groups ticked and the press. The
    test fails where the page offers a Save or the payload is anything else. The payload and the
    answer come back for the test to assert."""
    if not offers_review_changes(page):
        pytest.fail("unoffered_save is for a page that offers no Save", pytrace=False)
    payload = page_post(page, "save", tick=units)
    assert [value for name, value in payload if name == "unit"] == list(units)
    assert [value for name, value in payload if name == "press"] == ["save"]
    assert payload[: -1 - len(units)] == page_fields(page, "save", {}, (), {})[:-1]
    return list(payload), posted(browser, draft_id, payload)


def unoffered(post: Fields) -> RawPost:
    """A Save this post's page does not offer (a card's boxes open on it), sent on purpose as the
    raw POST it is: the post as made, every value it typed or chose in it. The test fails where
    the post isn't a helper's, its page offers a Save or the post presses anything else."""
    if not isinstance(post, Post):
        pytest.fail("unoffered is for a post a helper made from a page", pytrace=False)
    if not offers_review_changes(post.page):
        pytest.fail("unoffered is for a post whose page offers no Save", pytrace=False)
    carried(post)
    assert [value for name, value in post if name == "press"] == ["save"]
    return RawPost(post)


def raw_post(
    fields: Fields,
    replaced: Mapping[str, str] | None = None,
    *,
    removed: Iterable[str | tuple[str, str]] = (),
    added: Iterable[tuple[str, str]] = (),
) -> RawPost:
    """A submission the page does not offer, altered or injected on purpose: this post, checked as
    ``carried`` checks it, with exactly the changes stated here and nothing else. ``replaced``
    gives a named field's value in place, ``removed`` takes out a name's every field or one
    (name, value) field, and ``added`` puts fields at the end. The test fails where a change names
    a field the post doesn't hold or leaves the post as it was."""
    carried(fields)
    replacing, taken = dict(replaced or {}), list(removed)
    extra = list(added)
    names = {name for name, _ in fields}
    missing: list[str | tuple[str, str]] = [name for name in replacing if name not in names]
    missing += [
        item
        for item in taken
        if (item not in names if isinstance(item, str) else item not in fields)
    ]
    if missing:
        pytest.fail(f"raw_post names fields the post doesn't hold: {missing}", pytrace=False)
    same = [
        name
        for name, value in replacing.items()
        if all(held == value for other, held in fields if other == name)
    ]
    if same or not (replacing or taken or extra):
        pytest.fail(f"raw_post changes nothing in {same or 'the post'}", pytrace=False)
    whole = {item for item in taken if isinstance(item, str)}
    single = {item for item in taken if not isinstance(item, str)}
    payload = [
        (name, replacing.get(name, value))
        for name, value in fields
        if name not in whole and (name, value) not in single
    ]
    return RawPost([*payload, *extra])


def carrying(post: Fields, typed: Mapping[str, str]) -> Fields:
    """This post, made from the page Review changes returned, checked to carry this typing in its
    hidden fields as a browser sends it (line breaks as CRLF): the typing reviewed on that page."""
    sent = dict(post)
    for name, value in typed.items():
        lines = re.sub(r"\r\n|\r|\n", "\n", value)
        if re.sub(r"\r\n|\r|\n", "\n", sent.get(name, "\0")) != lines:
            pytest.fail(f"{name!r} is not carried as reviewed: {sent.get(name)!r}", pytrace=False)
    return post


def typed_beside_read(fields: Fields) -> bool:
    """Whether a post carries a box whose value differs from the value read, as its ``read:``
    mark gives it."""
    reads = {
        name.removeprefix("read:"): value for name, value in fields if name.startswith("read:")
    }
    return any(
        name.startswith("correct:")
        and name.removeprefix("correct:") in reads
        and read_digest(value) != reads[name.removeprefix("correct:")]
        for name, value in fields
    )


def newly_typed(post: "Post") -> bool:
    """Whether a helper's post types into a box a value other than the one its page shows."""
    shown = dict(page_fields(post.page, "save", {}, (), {}))
    return any(name in shown and shown[name] != value for name, value in post.typed.items())


def offers_review_changes(page: str) -> bool:
    """Whether this page's review form offers Review changes as its action and no Save: a card's
    boxes are open on it."""
    form = review_form(page)
    return 'name="press" value="review"' in form and 'name="press" value="save"' not in form


def still_open(page: str) -> list[str]:
    """Each card whose boxes are open on this page's review form, as the page names it, with the
    lines its group and its boxes show."""

    def text(part: str) -> str:
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", part))).strip()

    said: list[str] = []
    form = review_form(page)
    for unit in re.findall(r'<article class="assignment".*?</article>', form, re.S):
        head = re.findall(r'<p class="note">(.*?)</p>', unit.split("<details", 1)[0], re.S)
        for card in re.findall(r'<details class="steps" open>(.*?)</details>', unit, re.S):
            about = re.search(r'<span class="visually-hidden"> for (.*?)</span>', card, re.S)
            notes = re.findall(r'<p class="note">(.*?)</p>', card, re.S)
            lines = [text(line) for line in (*head, *notes)]
            name = text(about.group(1)) if about else "a card the page doesn't name"
            said.append(f"{name}: {' '.join(lines) or 'no line'}")
    return said or ["no open card found on the form"]


def reviewed(browser: TestClient, draft_id: str, fields: Fields) -> Reviewed:
    """The Review changes press the family makes before a Save, made now, for a test that then
    arranges what the post itself meets: that Save is pressed as Review changes, the Save then
    made from the page that returns, its corrections as reviewed and the same groups ticked. The
    family presses Review changes where the page a helper's post is made from offers it as the
    action for that post (a card's boxes open there, so Save isn't offered:
    ``offers_review_changes``), whatever its boxes hold, and before a Save carrying newly typed
    corrections. A correction is newly typed where a box differs from the value read, as its
    ``read:`` mark gives it, and, for a helper's post, its typing differs from the value its page
    shows (``newly_typed``): corrections the page carries, kept from an earlier press, are no new
    typing, and that Save from a page offering Save goes as one press. No card is opened here: a
    helper's post is checked to carry what its test typed and chose first (``carried``). Where
    Review changes returns a page that offers no Save (a card's boxes still open there), the test
    fails, naming each card still open and the lines it shows (``still_open``): no Save the page
    doesn't offer is sent. Where Review changes is refused (an answer of 400 or more, such as 422
    "A due date is needed."), the test fails, naming the refusal the page shows and each card
    left open (the cards of the page the post was made from where no review form comes back): a
    test that means to observe that refusal presses Review changes itself (``send``) and asserts
    it, and no Save is sent. A raw post goes exactly as it stands, with no press before it, and
    anything else, and a Save whose Review changes returns no review form with 200, is the post
    as given."""
    if isinstance(fields, Reviewed):
        return fields
    carried(fields)
    if not isinstance(fields, Post):
        return Reviewed(fields)
    typing = newly_typed(fields)
    sent = list(fields)
    offered = offers_review_changes(fields.page)
    if ("press", "save") not in sent or not (offered or (typing and typed_beside_read(sent))):
        return Reviewed(fields)
    answer = posted(browser, draft_id, raw_post(fields, {"press": "review"}))
    if answer.status_code >= 400:
        problem = re.search(r'<div class="problem"[^>]*>(.*?)</div>', answer.text, re.S)
        refusal = (
            re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", problem.group(1)))).strip()
            if problem
            else page_text(answer.text).strip()[:300]
        )
        cards = still_open(answer.text if has_review_form(answer.text) else fields.page)
        pytest.fail(
            f"Review changes was refused ({answer.status_code}), so no Save is sent after it: "
            f"{refusal or 'no refusal line shown'}; " + "; ".join(cards),
            pytrace=False,
        )
    if answer.status_code != 200 or not has_review_form(answer.text):
        return Reviewed(fields)
    if offers_review_changes(answer.text):
        pytest.fail(
            "Review changes came back with no Save to press, a card still open: "
            + "; ".join(still_open(answer.text)),
            pytrace=False,
        )
    ticks = [value for name, value in sent if name == "unit"]
    return Reviewed(page_post(answer.text, "save", tick=ticks))


def saved_with_corrections(browser: TestClient, draft_id: str, fields: Fields) -> Answer:
    """The one call through which every test makes a Save on the review that carries typed
    corrections (where that call also makes other presses, they go through it too), sent as the
    family sends it (``reviewed``): a Save carrying a box whose value differs from the one read is
    sent as Review changes first, then Save from the page that returns. Anything else is sent
    exactly as ``send`` sends it."""
    return posted(browser, draft_id, reviewed(browser, draft_id, fields))


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


def keep_on_record(browser: TestClient, draft_id: str, *answers: tuple[str, str, str]) -> None:
    """These answers kept on the record directly, at the review's version: kind, subject, value."""
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


def her_name_answered(browser: TestClient, draft_id: str, page: str) -> str:
    """The name question answered "hers" directly on the record, and the page after."""
    [form] = fields_named(review_form(page), "answer:name:")
    keep_on_record(browser, draft_id, ("name", form, "hers"))
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
        post = every_choice(page, "keep")
        found_row = {
            name: chosen
            for name, value in post
            if name.startswith("answer:destination:") and value != chosen
        }
        kept = send(browser, draft_id, raw_post(post, found_row) if found_row else post)
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

    def not_dates(fields: Fields) -> Fields:
        typed = {
            name: "not a date"
            for name, value in fields
            if is_date_box(name) and value != "not a date"
        }
        return raw_post(fields, typed) if typed else fields

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


def at_their_limits(fields: Fields, *field_starts: str) -> RawPost:
    """This post with each box whose field starts with one of these filled to its field's limit,
    sent as the raw post it is."""
    filled = {}
    for name, _ in fields:
        if name.startswith("correct:"):
            field = str(json.loads(name.removeprefix("correct:"))[1])
            if field.startswith(field_starts):
                filled[name] = BEYOND_THE_PLANE * field_most(field)
    return raw_post(fields, filled)


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


def small_form_fields(form: str) -> RawPost:
    """A small form's own fields, as the page wrote them: sent exactly as they stand."""
    return RawPost(
        (html.unescape(name), html.unescape(value))
        for name, value in re.findall(r'name="([^"]+)" value="([^"]*)"', form)
    )


def an_offer_s_post(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Fields]:
    """A row split into its two days, each kept as new homework, so its refused group offers a whole
    change: the review's id, and that offer's post."""
    card = homework_card(footer="Due (10/9/2026)")
    days = [
        homework_answer(headings=[{"text": heading, "y": 150, "uncertain": None}], cards=[card])
        for heading in ("Monday 10/5/2026", "Tuesday 10/6/2026")
    ]
    first, second = images(2)
    draft_id, page = read_review(browser, monkeypatch, (first, days[0]), (second, days[1]))
    [row] = fields_named(review_form(page), "answer:split:")
    keep_on_record(browser, draft_id, ("split", row, json.dumps("split")))
    page = browser.get(f"/parent/imports/{draft_id}").text
    pieces = fields_named(review_form(page), "answer:destination:")
    keep_on_record(browser, draft_id, *(("destination", piece, '{"new":1}') for piece in pieces))
    page = browser.get(f"/parent/imports/{draft_id}").text
    offers = re.findall(
        r'<form method="post" action="[^"]+/review" id="offer-[^"]+">.*?</form>', page, re.S
    )
    return draft_id, small_form_fields(offers[-1])


def a_found_row_s_post(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Fields]:
    """A card of another name, and a saved row "Find under another name" lists for it: the review's
    id, and the post of choosing that row."""
    first, second = images(2)
    one_saved_row(browser, monkeypatch, first)
    draft_id, page = read_review(browser, monkeypatch, (second, plain(0, 1)))
    [piece] = fields_named(review_form(page), "answer:destination:")
    finding = browser.post(
        f"/parent/imports/{draft_id}/find", data={"piece": piece, "words": "homework"}
    ).text
    choice = re.search(
        r'<form method="post" action="[^"]+/review" class="choice">.*?</form>', finding, re.S
    )
    assert choice is not None, finding[:400]
    return draft_id, small_form_fields(choice.group(0))


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
        answer = saved_with_corrections(self.browser, self.draft_id, fields)
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

    def opened_then_keep_later_keep(self, page: str, words: str) -> str:
        """ "Correct what was read" pressed on the card of the box, then Keep with the words typed,
        Keep from its page, Later from that, Keep from Later's: the page the last comes back
        with."""
        cards = closed_cards(review_form(page), {self.box: words}, ())
        page = open_cards(self.browser, self.draft_id, page, cards)
        page = self.press("keep", every_choice(page, "keep", {self.box: words}))
        page = self.press("keep from the page keep returned", every_choice(page, "keep"))
        page = self.press("later from the page keep returned", every_choice(page, "later"))
        return self.press("keep from the page later returned", every_choice(page, "keep"))

    def save(self, page: str, ticks: Iterable[str], label: str = "save") -> str:
        return self.press(label, every_choice(page, "save", ticks=list(ticks)))

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
    page = presses.opened_then_keep_later_keep(page, TYPED)
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
    page = presses.opened_then_keep_later_keep(page, TYPED_INSTRUCTION)
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


def stream_of(asked: str) -> bytes:
    """The compressed stream a signed list holds, read here rather than by the code it tests."""
    assert asked.startswith("z1."), asked[:40]
    text = asked.removeprefix("z1.")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def list_of_stream(stream: bytes) -> str:
    """A signed list holding this stream, whatever it is."""
    return "z1." + base64.urlsafe_b64encode(stream).decode().rstrip("=")


def unpacked_bytes(asked: str) -> bytes:
    return zlib.decompress(stream_of(asked))


def unpacked(asked: str) -> dict[str, Any]:
    """The signed list a page wrote, read here rather than by the code it tests."""
    found: dict[str, Any] = json.loads(unpacked_bytes(asked))
    return found


def a_list_of_exactly(asked: str, size: int) -> str:
    """This page's signed list with one long string added, so it comes to exactly this many bytes
    once unpacked."""
    found = unpacked(asked)
    lists = json.dumps(found["lists"], separators=(",", ":"))[1:-1]
    asks = json.dumps(found["asks"], separators=(",", ":"))[1:-1]
    head = f'{{"lists":[{lists},["'.encode()
    tail = f'"]],"asks":[{asks}]}}'.encode()
    packing = zlib.compressobj(9)
    parts = [packing.compress(head)]
    left = size - len(head) - len(tail)
    block = b"a" * (1024 * 1024)
    for _ in range(left // len(block)):
        parts.append(packing.compress(block))
    parts += [
        packing.compress(b"a" * (left % len(block))),
        packing.compress(tail),
        packing.flush(),
    ]
    return list_of_stream(b"".join(parts))


def signed_again(browser: TestClient, draft_id: str, fields: Fields, asked: str) -> RawPost:
    """This post with this signed list, signed with the review's own key: what only someone holding
    that key can send, sent as the raw post it is."""
    student = state_of(browser).project_state.student_id()
    key = state_of(browser).import_drafts.snapshot(student, draft_id).page_key
    changed = [(name, asked if name == "asked" else value) for name, value in fields]
    signed = [
        (name, value)
        for name, value in changed
        if name in screenshot_posts.SIGNED or name.startswith("digest:")
    ]
    held = dict(fields)
    made = {"asked": asked, "check": page_check(key, signed)}
    return raw_post(fields, {name: value for name, value in made.items() if held[name] != value})


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
    comes to exactly this many bytes, or as short as they can be. The fields one byte longer have
    a name of their own, so no name is sent twice with different values."""
    body = as_a_browser_sends(fields)
    more = count - len(fields)
    head, longer = b"&read%3Azz=", b"&read%3Azy="
    if total is None:
        return body + (head + b"1") * more
    each, over = divmod(total - len(body) - more * len(head), more)
    return body + (head + b"a" * each) * (more - over) + (longer + b"a" * (each + 1)) * over


def body_with_a_paste(fields: Fields, box: str, typed: str, total: int) -> tuple[bytes, str]:
    """This post with this box holding as much of this character as makes the body exactly this many
    bytes, plain characters making up what's left: the body, and what the box holds."""
    others = as_a_browser_sends(raw_post(fields, removed=[box]))
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
    past ``each``. Each field has a name of its own, so no name is sent twice with different
    values."""

    def head(count: int) -> bytes:
        return b"&read%3Azz" + str(count).encode() + b"="

    yield body
    left = total - len(body)
    assert left > len(head(0))
    count = 0
    while left > 0:
        take = min(left, each)
        if left - take and left - take <= len(head(count + 1)):
            take -= len(head(count + 1))
        yield head(count) + b"a" * (take - len(head(count)))
        left -= take
        count += 1
