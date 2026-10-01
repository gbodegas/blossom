"""A form the parser can't read: 400, on a page that reads no store, says that the form could
not be read and that nothing was saved, and offers the ways back its route gives.

The status order holds on every form route, for her, a parent and the sign-in off: a caller
the route is not open to is refused first, 403, whatever the body; then a body the parser
can't read is 400; then a form that parses but isn't whole keeps its 422. Nothing is read
from the store and nothing is written. The sign-in's own page says only that its form could
not be read: no cookie is set, changed or cleared, the passphrase is in no page and no log
line, and nothing is said about being signed in or out.
"""

import logging
import pathlib
import re
from typing import Final, cast
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from tests.support import (
    ESSAY_ID,
    HERS,
    PAGE_HEADERS,
    THEIRS,
    Answer,
    Statements,
    database_of,
    every_row,
    household_client,
    main_of,
    rules_named,
    sign_in_as,
    spy_on_stores,
    state_of,
    store_free_page,
    ways_back_of,
    words,
)

CR: Final = b"\r\n"
TYPED: Final = "Seven blue kites"
"""Words in every unreadable body, which no answer may show."""
MULTIPART: Final = "multipart/form-data; boundary=synthetic"
URL_ENCODED: Final = "application/x-www-form-urlencoded"


def unreadable(typed: str = TYPED) -> dict[str, tuple[bytes, str]]:
    """The bodies the form parser can't read: multipart its parser rejects, a part with no
    name, a multipart type with no boundary, 1001 fields, and one field over 1 MiB."""
    sent = typed.encode()
    return {
        "multipart the parser rejects": (b"not the boundary" + CR + CR + sent, MULTIPART),
        "a part with no name": (
            b"--synthetic" + CR + b"Content-Disposition: form-data" + CR + CR + sent,
            MULTIPART,
        ),
        "no boundary": (urlencode({"note": typed}).encode(), "multipart/form-data"),
        "1001 fields": (
            urlencode([("note", typed), *[(f"f{n}", "1") for n in range(1000)]]).encode(),
            URL_ENCODED,
        ),
        "a field over 1 MiB": (
            urlencode([("note", typed + "x" * (1024 * 1024))]).encode(),
            URL_ENCODED,
        ),
    }


NOTE: Final = str(uuid4())
NOT_A_NOTE: Final = "not-a-note"
HER_NOTE_PRESSES: Final = (
    "edit",
    "archive",
    "restore",
    "delete",
    "ask-for-help",
    "details",
    "add",
    "link",
    "unlink",
)
REPORT: Final = f"/student/actions/assignments/{ESSAY_ID}/report"
ASK: Final = "/student/actions/ask-for-help"
HERS_TREE: Final = (
    REPORT,
    f"/student/actions/assignments/{ESSAY_ID}/undo-report",
    f"/student/actions/assignments/{ESSAY_ID}/hand-in",
    f"/student/actions/assignments/{ESSAY_ID}/undo-hand-in",
    ASK,
    "/student/actions/homework-notes",
    *[f"/student/actions/homework-notes/{NOTE}/{press}" for press in HER_NOTE_PRESSES],
    f"/student/actions/homework-notes/{NOT_A_NOTE}/edit",
    f"/student/actions/homework-notes/{NOT_A_NOTE}/ask-for-help",
)
PASTE: Final = "/parent/inbox/read"
DECIDE: Final = "/parent/actions/decide/draft-not-here"
FAMILY_TREE: Final = (
    *[
        f"/parent/actions/homework-notes/{NOTE}/{press}"
        for press in ("details", "add", "link", "unlink")
    ],
    f"/parent/actions/school-instructions/{ESSAY_ID}",
    PASTE,
    "/parent/inbox/enter",
    "/parent/inbox/edit",
    "/parent/inbox/find",
    "/parent/inbox/keep",
    f"/parent/actions/checks/{ESSAY_ID}/mark",
    f"/parent/actions/checks/{ESSAY_ID}/again",
    "/parent/actions/plan",
    f"/parent/actions/help/{uuid4().hex}",
    DECIDE,
)
SIGN_IN: Final = "/sign-in"
EVERY_FORM: Final = (*HERS_TREE, *FAMILY_TREE, SIGN_IN)

NOTHING_SAVED: Final = "That form could not be read, so nothing was saved."
NOTHING_SENT: Final = "That form could not be read, so nothing was sent."
SIGN_IN_HEADING: Final = "Sign-in form could not be read"
SIGN_IN_SAID: Final = "That form could not be read. Open sign-in and try again."
WEEK: Final = "/student/due-this-week"


def expected(path: str, reader: str) -> tuple[str, str, list[tuple[str, str]]]:
    """The heading, the sentence and the ways back a 400 for this path gives this reader."""
    if path == SIGN_IN:
        return SIGN_IN_HEADING, SIGN_IN_SAID, [(SIGN_IN, "Back to sign in")]
    if path.startswith("/parent/"):
        return "Nothing was saved", NOTHING_SAVED, [("/parent", "Back to Family review")]
    week = (WEEK, "Back to her week" if reader == "parent" else "Back to my week")
    note = [(f"/student/homework-notes/{NOTE}", "Back to the note")] if NOTE in path else []
    if path.endswith("/ask-for-help"):
        return "Request not sent", NOTHING_SENT, [*note, week]
    return "Nothing was saved", NOTHING_SAVED, [*note, week]


def forbidden(path: str, reader: str) -> bool:
    """Whether the route is closed to this reader before its body is read: her tree to a
    parent, the family's to her. Sign-in is open to everyone."""
    if path == SIGN_IN or reader == "open":
        return False
    return path.startswith("/parent/") if reader == "her" else path.startswith("/student/")


def press(client: TestClient, path: str, body: bytes, kind: str) -> Answer:
    return client.post(path, content=body, headers={**PAGE_HEADERS, "Content-Type": kind})


# ------------------------------------------------------------------ 1. every form route


@pytest.mark.parametrize("reader", ["her", "parent", "open"])
def test_every_form_route_answers_a_body_it_cannot_read_with_a_page_that_reads_no_store(
    reader: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every HTML form route, with every unreadable body: 403 first where the route is
    closed to the reader, otherwise 400 with the route's heading, sentence and ways back,
    one focused explanation, no typed words, no store call, no statement, and no row
    changed."""
    caplog.set_level(logging.DEBUG)
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        state = state_of(client)
        before = every_row(database_of(client))
        calls: list[str] = []
        spy_on_stores(monkeypatch, state, calls)
        for path in EVERY_FORM:
            for body_name, (body, kind) in unreadable().items():
                calls.clear()
                with Statements(state) as seen:
                    answer = press(client, path, body, kind)
                assert TYPED not in answer.text, (path, body_name)
                assert "set-cookie" not in answer.headers, (path, body_name)
                if forbidden(path, reader):
                    assert answer.status_code == 403, (path, body_name)
                    continue
                heading, said, back = expected(path, reader)
                main = store_free_page(answer, status=400, heading=heading, alert=said)
                assert ways_back_of(main) == back, (path, body_name, ways_back_of(main))
                assert calls == [], (path, body_name, calls)
                assert seen == [], (path, body_name, seen[:3])
        after = every_row(database_of(client))

    assert after == before
    assert [r for r in caplog.records if TYPED in r.getMessage()] == []


@pytest.mark.parametrize(
    ("path", "family"), [(REPORT, False), (ASK, False), (PASTE, True), (DECIDE, True)]
)
def test_the_page_is_in_the_tree_the_route_is_in(
    path: str, family: bool, tmp_path: pathlib.Path
) -> None:
    """With the sign-in off, a family route's 400 reads as a family page and hers as her
    week's, in the masthead as on every other page."""
    body, kind = unreadable()["multipart the parser rejects"]
    with household_client("open", tmp_path) as client:
        answer = press(client, path, body, kind)

    assert answer.status_code == 400
    current = re.findall(r'aria-current="page">([^<]*)</a>', answer.text)
    assert current == (["Family review"] if family else ["My week"])


def test_the_ways_back_are_named_for_the_reader_not_the_tree(tmp_path: pathlib.Path) -> None:
    """A parent who meets the page on her tree reads her week as hers; she reads it as her
    own."""
    from blossom.routes.forms import FormUnreadable

    async def unread(request: Request) -> None:
        raise FormUnreadable

    readers = {}
    for reader in ("parent", "her"):
        with household_client(reader, tmp_path / reader) as client:
            cast(FastAPI, client.app).add_api_route(
                "/student/actions/synthetic", unread, methods=["POST"]
            )
            sign_in_as(client, reader)
            answer = client.post("/student/actions/synthetic", headers=PAGE_HEADERS)
            readers[reader] = (answer.status_code, ways_back_of(main_of(answer.text)))

    assert readers == {
        "parent": (400, [(WEEK, "Back to her week")]),
        "her": (400, [(WEEK, "Back to my week")]),
    }


# ------------------------------------------------------------------ 2. the order


@pytest.mark.parametrize("reader", ["her", "open"])
def test_a_form_that_parses_but_is_not_whole_keeps_its_answer(
    reader: str, tmp_path: pathlib.Path
) -> None:
    """403 before 400 before 422: what parses is still read as before, a field twice or a
    body of another kind included, and a misshapen note id is still 404 once the body reads."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        statuses = {
            "report, plain text": press(client, REPORT, TYPED.encode(), "text/plain"),
            "report, a field twice": press(client, REPORT, b"status=done&status=done", URL_ENCODED),
            "misshapen note, readable": press(
                client, f"/student/actions/homework-notes/{NOT_A_NOTE}/edit", b"", URL_ENCODED
            ),
            "sign-in, plain text": press(client, SIGN_IN, b"passphrase=x", "text/plain"),
        }
    assert {name: answer.status_code for name, answer in statuses.items()} == {
        "report, plain text": 422,
        "report, a field twice": 422,
        "misshapen note, readable": 404,
        "sign-in, plain text": 422,
    }


BODYLESS: Final = (
    "/student/actions/too-much",
    f"/student/actions/take-back/{uuid4().hex}",
    f"/student/actions/take-back-help/{uuid4().hex}",
)


@pytest.mark.parametrize("reader", ["her", "parent", "open"])
def test_a_route_that_reads_no_body_answers_as_it_does_with_none(
    reader: str, tmp_path: pathlib.Path
) -> None:
    body, kind = unreadable()["multipart the parser rejects"]
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        statuses = {
            path: (
                press(client, path, b"", URL_ENCODED).status_code,
                press(client, path, body, kind).status_code,
            )
            for path in BODYLESS
        }
    assert all(empty == broken for empty, broken in statuses.values()), statuses
    assert 400 not in {broken for _, broken in statuses.values()}


@pytest.mark.parametrize("reader", ["her", "parent"])
def test_the_json_routes_answer_as_they_did(reader: str, tmp_path: pathlib.Path) -> None:
    """A JSON route is not a form route: FastAPI's 422 for her, the 403 for a parent."""
    body, kind = unreadable()["multipart the parser rejects"]
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        answer = client.post("/student/help-requests", content=body, headers={"Content-Type": kind})
    assert answer.status_code == (403 if reader == "parent" else 422)
    assert answer.headers["content-type"] == "application/json"


# ------------------------------------------------------------------ 3. sign-in


def sign_in_bodies() -> dict[str, tuple[bytes, str]]:
    """Each unreadable body, carrying a passphrase that would open the door."""
    return unreadable(f"passphrase={THEIRS}")


@pytest.mark.parametrize("session", ["none", "hers"])
def test_a_sign_in_form_that_cannot_be_read_says_so_and_touches_no_session(
    session: str, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with household_client("her", tmp_path) as client:
        if session == "hers":
            sign_in_as(client, "her")
        cookies = dict(client.cookies)
        answers = [press(client, SIGN_IN, body, kind) for body, kind in sign_in_bodies().values()]
        kept = dict(client.cookies)
        week = client.get(WEEK, headers=PAGE_HEADERS)

    for answer in answers:
        main = store_free_page(answer, status=400, heading=SIGN_IN_HEADING, alert=SIGN_IN_SAID)
        assert ways_back_of(main) == [(SIGN_IN, "Back to sign in")]
        assert "set-cookie" not in answer.headers
        assert THEIRS not in answer.text
        said = words(main).lower()
        assert "signed" not in said
        assert "session" not in said
        assert "sign out" not in answer.text.lower()
    assert kept == cookies
    assert week.status_code == (200 if session == "hers" else 303)
    assert [r for r in caplog.records if THEIRS in r.getMessage()] == []
    assert [r.getMessage() for r in caplog.records if r.name == "blossom.app"] == [
        f"a form could not be read: {kind}"
        for kind in (
            "MultipartParseError",
            "MultiPartException",
            "MultiPartException",
            "MultiPartException",
            "MultiPartException",
        )
    ]


def test_a_sign_in_form_that_cannot_be_read_is_not_a_try(tmp_path: pathlib.Path) -> None:
    """The device's tries count only forms that were read: ten rounds of unreadable ones leave
    the right passphrase opening the door, and once wrong ones use up the tries, an unreadable
    one is still 400 and a readable one is told to wait."""
    with household_client("her", tmp_path) as client:
        for _ in range(10):
            for body, kind in sign_in_bodies().values():
                assert press(client, SIGN_IN, body, kind).status_code == 400
        came_in = client.post(SIGN_IN, data={"passphrase": HERS})
        client.post("/sign-out")
        wrong = [client.post(SIGN_IN, data={"passphrase": "wrong"}).status_code for _ in range(10)]
        body, kind = sign_in_bodies()["1001 fields"]
        broken = press(client, SIGN_IN, body, kind)
        waiting = client.post(SIGN_IN, data={"passphrase": HERS})

    assert came_in.status_code == 303
    assert wrong == [422] * 10
    assert broken.status_code == 400
    assert waiting.status_code == 429


@pytest.mark.parametrize("where", [SIGN_IN, REPORT, PASTE])
def test_a_form_from_another_origin_is_refused_before_its_body_is_read(
    where: str, tmp_path: pathlib.Path
) -> None:
    body, kind = sign_in_bodies()["multipart the parser rejects"]
    with household_client("open", tmp_path) as client:
        answer = client.post(
            where,
            content=body,
            headers={"Content-Type": kind, "Origin": "http://elsewhere.example"},
        )
    assert answer.status_code == 403
    assert answer.text == "This request must come from the same origin."


# ------------------------------------------------------------------ 4. only the parser


def test_another_failure_while_the_form_is_read_is_not_taken_for_an_unreadable_one(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the parser's own failures are a form that could not be read; anything else raised
    while the form is read still raises."""

    async def broken(*_: object, **__: object) -> None:
        msg = "not the parser"
        raise RuntimeError(msg)

    monkeypatch.setattr(Request, "form", broken)
    with household_client("open", tmp_path) as client:
        fields = client.post(SIGN_IN, data={"passphrase": "x"}, headers=PAGE_HEADERS)
        with pytest.raises(RuntimeError):
            client.post(ASK, data={"note": "x"}, headers=PAGE_HEADERS)

    assert fields.status_code == 400
    assert fields.json() == {"detail": "There was an error parsing the body"}


# ------------------------------------------------------------------ 5. the ways back


def ways_back_block(main: str) -> str:
    """The one block of the page that holds its ways back, whole."""
    found = re.findall(r'<div class="ways-back">(.*?)</div>', main, flags=re.S)
    assert len(found) == 1, found
    return cast(str, found[0])


@pytest.mark.parametrize("reader", ["her", "parent", "open"])
def test_every_way_back_stands_on_a_line_of_its_own_in_the_ways_back(
    reader: str, tmp_path: pathlib.Path
) -> None:
    """Every page the reader can meet holds its ways back, the note and the week included, in
    one block of lines that holds nothing else."""
    body, kind = unreadable()["multipart the parser rejects"]
    shown = {}
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        for path in EVERY_FORM:
            if forbidden(path, reader):
                continue
            block = ways_back_block(main_of(press(client, path, body, kind).text))
            line = r'\s*<p class="return"><a href="[^"]*">[^<]*</a></p>\s*'
            assert re.fullmatch(f"(?:{line})+", block), (path, block)
            shown[path] = ways_back_of(block)

    assert shown == {path: expected(path, reader)[2] for path in shown}
    assert any(len(back) == 2 for back in shown.values()) == (reader != "parent")


def test_each_way_back_keeps_its_press_area_inside_its_own_line() -> None:
    """The note and the week stand one under another, and neither one's 44 pixels to press
    reaches into the other's."""
    declared = {
        line.strip()
        for rule in rules_named(".ways-back .return a")
        for line in rule.split("\n")
        if line.strip()
    }

    assert {"display: inline-block;", "padding: 0.8rem 0;", "margin: 0;"} <= declared
