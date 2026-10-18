# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A form the parser can't read: 400, on a page that reads no store, says that the form could
not be read and that nothing was saved, and offers the ways back its route gives.

The status order holds on every form route, for her, a parent and the sign-in off: a caller
the route is not open to is refused first, 403, whatever the body; then a body the parser
can't read is 400; then a form that parses but isn't whole keeps its 422. Nothing is read
from the store and nothing is written. The sign-in's own page says only that its form could
not be read: no cookie is set, changed or cleared, the passphrase is in no page and no log
line, and nothing is said about being signed in or out.
"""

import asyncio
import codecs
import logging
import pathlib
import re
from collections.abc import Buffer, Iterator
from html import unescape
from tempfile import SpooledTemporaryFile
from typing import Final, cast
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.params import Form as FormParameter
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from python_multipart.exceptions import (
    DecodeError,
    FormParserError,
    MultipartParseError,
    ParseError,
    QuerystringParseError,
)
from starlette.formparsers import MultiPartParser
from starlette.routing import Mount, Route
from starlette.types import Message, Scope

from blossom.dependencies import ApplicationState
from blossom.routes.forms import FormRoute
from tests.support import (
    ESSAY_ID,
    HERS,
    PAGE_HEADERS,
    THEIRS,
    Answer,
    Statements,
    database_of,
    every_route,
    every_row,
    household_client,
    main_of,
    rules_named,
    sign_in_as,
    spy_on_stores,
    state_of,
    store_free_page,
    ways_back_of,
    whole_form,
    words,
)

CR: Final = b"\r\n"
TYPED: Final = "Seven blue kites"
"""Words in every unreadable body, which no answer may show."""
MULTIPART: Final = "multipart/form-data; boundary=synthetic"
URL_ENCODED: Final = "application/x-www-form-urlencoded"


LONGEST_BOUNDARY: Final = 256
"""The longest multipart boundary the form parser reads, in bytes."""


def multipart(
    fields: list[tuple[str, str]],
    boundary: str,
    *,
    quoted: bool = False,
    charset: str | None = None,
) -> tuple[bytes, str]:
    """A whole multipart body of these fields, split by this boundary, its type naming the
    charset when one is given."""
    split = b"--" + boundary.encode()
    sent = b"".join(
        split
        + CR
        + f'Content-Disposition: form-data; name="{name}"'.encode()
        + CR
        + CR
        + value.encode()
        + CR
        for name, value in fields
    )
    named = f'"{boundary}"' if quoted else boundary
    kind = "multipart/form-data" + (f"; charset={charset}" if charset else "")
    return sent + split + b"--" + CR, f"{kind}; boundary={named}"


HALF: Final = "+2AA-"
"""Half of a surrogate pair as UTF-7 writes it: text no page can show and no record keeps."""
HALF_ESCAPED: Final = "\\ud800"
"""The same half, as the unicode_escape codec reads it."""


def unreadable(typed: str = TYPED) -> dict[str, tuple[bytes, str]]:
    """The bodies the form parser can't read: multipart its parser rejects, a part with no
    name, a multipart type with no boundary, a boundary over 256 bytes, a charset no part can
    be decoded in, a charset that decodes a part to half of a character, beside the typed
    words or within them, 1001 fields, and one field over 1 MiB."""
    sent = typed.encode()
    name, _, value = typed.partition("=")
    return {
        "multipart the parser rejects": (b"not the boundary" + CR + CR + sent, MULTIPART),
        "a part with no name": (
            b"--synthetic" + CR + b"Content-Disposition: form-data" + CR + CR + sent,
            MULTIPART,
        ),
        "no boundary": (urlencode({"note": typed}).encode(), "multipart/form-data"),
        "a boundary over 256 bytes": multipart(
            [(name, value) if value else ("note", typed)], "b" * (LONGEST_BOUNDARY + 1)
        ),
        "a charset no part can be decoded in": multipart(
            [(name, value) if value else ("note", typed)], "synthetic", charset="undefined"
        ),
        "half a character beside the words": multipart(
            [(name, value) if value else ("note", typed), ("next", HALF)],
            "synthetic",
            charset="utf-7",
        ),
        "half a character within the words": multipart(
            [(name, value + HALF_ESCAPED) if value else ("note", typed + HALF_ESCAPED)],
            "synthetic",
            charset="unicode_escape",
        ),
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
BACK_TO_HELP: Final = (f"{WEEK}#help", "Back to Help")


def expected(path: str, reader: str) -> tuple[str, str, list[tuple[str, str]]]:
    """The heading, the sentence and the ways back a 400 for this path gives this reader."""
    if path == SIGN_IN:
        return SIGN_IN_HEADING, SIGN_IN_SAID, [(SIGN_IN, "Back to sign in")]
    if path.startswith("/parent/"):
        return "Nothing was saved", NOTHING_SAVED, [("/parent", "Back to Family review")]
    week = (WEEK, "Back to her week" if reader == "parent" else "Back to my week")
    note = [(f"/student/homework-notes/{NOTE}", "Back to the note")] if NOTE in path else []
    if path.endswith("/ask-for-help"):
        return "Request not sent", NOTHING_SENT, [*note, BACK_TO_HELP]
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


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize(
    "length", [LONGEST_BOUNDARY - 1, LONGEST_BOUNDARY, LONGEST_BOUNDARY + 1, 1000]
)
def test_a_boundary_reads_up_to_256_bytes_and_a_longer_one_is_a_form_that_cannot_be_read(
    length: int, quoted: bool, tmp_path: pathlib.Path
) -> None:
    """Up to 256 bytes, quoted or not, the form reads as it does with a short boundary, and the
    right passphrase opens the door; past them, sign-in, a handler's form and a form of
    parameters alike answer with the 400 page, as often as the same body is sent."""
    boundary = "b" * length
    with household_client("her", tmp_path / "her") as client:
        sent = multipart([("passphrase", HERS)], boundary, quoted=quoted)
        doors = [press(client, SIGN_IN, *sent) for _ in range(2)]
        week = client.get(WEEK, headers=PAGE_HEADERS).status_code
    answers = {}
    with household_client("open", tmp_path / "open") as client:
        for path in (REPORT, ASK, PASTE, DECIDE):
            fields = [("note", TYPED)]
            short = press(client, path, *multipart(fields, "synthetic", quoted=quoted))
            sent = multipart(fields, boundary, quoted=quoted)
            answers[path] = (short, [press(client, path, *sent) for _ in range(2)])

    if length <= LONGEST_BOUNDARY:
        assert doors[0].status_code == 303
        assert "set-cookie" in doors[0].headers
        assert week == 200
        for path, (short, longs) in answers.items():
            for long in longs:
                assert long.status_code == short.status_code != 400, path
                assert long.headers["content-type"] == short.headers["content-type"], path
    else:
        for door in doors:
            store_free_page(door, status=400, heading=SIGN_IN_HEADING, alert=SIGN_IN_SAID)
            assert "set-cookie" not in door.headers
        assert week == 303
        for path, (_, longs) in answers.items():
            heading, said, back = expected(path, "open")
            for long in longs:
                main = store_free_page(long, status=400, heading=heading, alert=said)
                assert ways_back_of(main) == back, path
                assert TYPED not in long.text, path


UNDECODABLE: Final = "undecodable"
"""A charset these tests register: it reads ASCII, and refuses a part holding ``TYPED`` with a
plain ``UnicodeError``, which Starlette does not read as Latin-1."""


def undecodable_codec(name: str) -> codecs.CodecInfo | None:
    """The codec for ``UNDECODABLE``, as Python's codec search asks for it; none for any
    other name."""
    if name != UNDECODABLE:
        return None

    def decode(sent: Buffer, errors: str = "strict") -> tuple[str, int]:
        if TYPED.encode() in bytes(sent):
            raise UnicodeError(UNDECODABLE)
        return codecs.ascii_decode(sent, errors)

    return codecs.CodecInfo(codecs.ascii_encode, decode, name=UNDECODABLE)


@pytest.fixture
def undecodable() -> Iterator[None]:
    """``UNDECODABLE`` known to Python's codecs for one test, and unknown again after it."""
    codecs.register(undecodable_codec)
    yield
    codecs.unregister(undecodable_codec)


@pytest.mark.usefixtures("undecodable")
@pytest.mark.parametrize("charset", ["undefined", UNDECODABLE])
def test_a_charset_a_part_cannot_be_decoded_in_is_a_form_that_cannot_be_read(
    charset: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A charset whose codec fails on a part with a plain ``UnicodeError``, on every part or
    only on the typed words, gets the 400 page, through a handler and through a form of
    parameters alike: no cookie, no store call, no statement, no row changed, and nothing
    typed in a page or a log line, which names the failure's kind alone."""
    with pytest.raises(UnicodeError) as raised:
        TYPED.encode().decode(charset)
    assert type(raised.value) is UnicodeError
    caplog.set_level(logging.DEBUG)
    answers = {}
    for reader, paths, fields in (
        ("her", (SIGN_IN,), [("passphrase", HERS), ("x", TYPED)]),
        ("open", (ASK, PASTE, DECIDE), [("note", TYPED)]),
    ):
        with household_client(reader, tmp_path / reader) as client:
            state = state_of(client)
            before = every_row(database_of(client))
            calls: list[str] = []
            spy_on_stores(monkeypatch, state, calls)
            sent = multipart(fields, "synthetic", charset=charset)
            with Statements(state) as seen:
                for path in paths:
                    answers[path] = press(client, path, *sent)
            assert (calls, seen, every_row(database_of(client))) == ([], [], before), reader

    for path, answer in answers.items():
        heading, said, back = expected(path, "open")
        main = store_free_page(answer, status=400, heading=heading, alert=said)
        assert ways_back_of(main) == back, path
        assert "set-cookie" not in answer.headers, path
        assert TYPED not in answer.text, path
        assert HERS not in answer.text, path
    logged = [r.getMessage() for r in caplog.records]
    assert [line for line in logged if TYPED in line or HERS in line] == []
    assert [r.getMessage() for r in caplog.records if r.name == "blossom.app"] == [
        "a form could not be read: UnicodeError"
    ] * len(answers)


SENT: Final = "caf\u00e9"
"""Words whose UTF-8 bytes ASCII can't decode, and Latin-1 reads as other words."""


def as_met(answer: Answer) -> tuple[int, str | None, str | None, bool, str]:
    """An answer as its reader meets it: the status, the kind, where it sends them, whether it
    sets a cookie, and its words."""
    headers = answer.headers
    return (
        answer.status_code,
        headers.get("content-type"),
        headers.get("location"),
        "set-cookie" in headers,
        words(answer.text),
    )


def sent_both_ways(
    charset: str, read_in: str, tmp_path: pathlib.Path
) -> dict[str, tuple[Answer, Answer]]:
    """Each form with no charset, carrying the text ``read_in`` reads from its bytes, names and
    values alike, and then its own bytes naming ``charset``: the sign-in's with the right
    passphrase, and ``SENT`` as a note through a handler and through a form of parameters."""

    def both(fields: list[tuple[str, str]]) -> tuple[tuple[bytes, str], tuple[bytes, str]]:
        read = [(n.encode().decode(read_in), v.encode().decode(read_in)) for n, v in fields]
        return multipart(read, "synthetic"), multipart(fields, "synthetic", charset=charset)

    answers: dict[str, tuple[Answer, Answer]] = {}
    with household_client("her", tmp_path / "her") as client:
        plain, named = both([("passphrase", HERS), ("x", SENT)])
        answers[SIGN_IN] = (press(client, SIGN_IN, *plain), press(client, SIGN_IN, *named))
    with household_client("open", tmp_path / "open") as client:
        plain, named = both([("note", SENT)])
        for path in (ASK, PASTE, DECIDE):
            answers[path] = (press(client, path, *plain), press(client, path, *named))
    return answers


def note_kept(answer: Answer) -> str:
    """The words an Ask for help that is not whole keeps in its fresh form."""
    found = re.findall(r'<input id="help-note" [^>]*\bvalue="([^"]*)"', answer.text)
    assert len(found) == 1, found
    return unescape(cast(str, found[0]))


@pytest.mark.parametrize(
    ("charset", "refusal"), [("ascii", UnicodeDecodeError), ("no-such-charset", LookupError)]
)
def test_a_charset_whose_codec_refuses_a_part_reads_that_part_as_latin_1(
    charset: str, refusal: type[Exception], tmp_path: pathlib.Path
) -> None:
    """A codec that refuses the typed bytes with ``UnicodeDecodeError``, or a charset Python
    doesn't know, has Starlette read the part as Latin-1: the form answers as one with no
    charset that carries the Latin-1 reading, the right passphrase opens the door, and the
    note comes back as Latin-1 reads it."""
    with pytest.raises(refusal) as raised:
        SENT.encode().decode(charset)
    assert type(raised.value) is refusal
    answers = sent_both_ways(charset, "latin-1", tmp_path)

    for path, (plain, named) in answers.items():
        assert as_met(named) == as_met(plain), path
    assert {path: named.status_code for path, (_, named) in answers.items()} == {
        SIGN_IN: 303,
        ASK: 422,
        PASTE: 422,
        DECIDE: 422,
    }
    plain, named = answers[ASK]
    assert note_kept(named) == note_kept(plain) == "caf\u00c3\u00a9"


@pytest.mark.parametrize(
    ("charset", "door", "kept"),
    [("utf-8", 303, SENT), ("latin-1", 303, "caf\u00c3\u00a9"), ("cp500", 422, "")],
)
def test_a_charset_that_decodes_every_part_reads_the_form_as_decoded_names_included(
    charset: str, door: int, kept: str, tmp_path: pathlib.Path
) -> None:
    """A charset that decodes every part reads the form as the text it decodes, even when that
    is not what was typed: Latin-1 reads the note as other words, and EBCDIC reads each
    field's name as another name, so no passphrase and no note are read. Each answers as a
    form with no charset that carries the same text, through the same validation."""
    answers = sent_both_ways(charset, charset, tmp_path)

    for path, (plain, named) in answers.items():
        assert as_met(named) == as_met(plain), path
    assert {path: named.status_code for path, (_, named) in answers.items()} == {
        SIGN_IN: door,
        ASK: 422,
        PASTE: 422,
        DECIDE: 422,
    }
    plain, named = answers[ASK]
    assert note_kept(named) == note_kept(plain) == kept


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
    f"/student/actions/take-back/{uuid4().hex}",
    f"/student/actions/take-back-help/{uuid4().hex}",
)


@pytest.mark.parametrize("unread", ["multipart the parser rejects", "a boundary over 256 bytes"])
@pytest.mark.parametrize("reader", ["her", "parent", "open"])
def test_a_route_that_reads_no_body_answers_as_it_does_with_none(
    reader: str, unread: str, tmp_path: pathlib.Path
) -> None:
    body, kind = unreadable()[unread]
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


@pytest.mark.parametrize("unread", ["multipart the parser rejects", "a boundary over 256 bytes"])
@pytest.mark.parametrize("reader", ["her", "parent"])
def test_the_json_routes_answer_as_they_did(
    reader: str, unread: str, tmp_path: pathlib.Path
) -> None:
    """A JSON route is not a form route: FastAPI's 422 for her, the 403 for a parent."""
    body, kind = unreadable()[unread]
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
            "FormParserError",
            "UnicodeError",
            "UnicodeEncodeError",
            "UnicodeEncodeError",
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


@pytest.mark.parametrize(
    "failure",
    [
        FormParserError,
        ParseError,
        MultipartParseError,
        QuerystringParseError,
        DecodeError,
        UnicodeError,
    ],
    ids=lambda failure: failure.__name__,
)
def test_every_failure_of_the_form_parser_is_a_form_that_cannot_be_read(
    failure: type[Exception],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The parser's base failure, each kind of it, and a part it can't decode get the 400
    page, through a handler and through a form of parameters alike, and only the kind is
    logged."""
    caplog.set_level(logging.DEBUG)

    async def refused(*_: object, **__: object) -> None:
        raise failure(TYPED)

    monkeypatch.setattr(Request, "form", refused)
    with household_client("open", tmp_path) as client:
        asked = press(client, ASK, b"", URL_ENCODED)
        signing = press(client, SIGN_IN, b"", URL_ENCODED)

    store_free_page(asked, status=400, heading="Request not sent", alert=NOTHING_SENT)
    store_free_page(signing, status=400, heading=SIGN_IN_HEADING, alert=SIGN_IN_SAID)
    assert [r.getMessage() for r in caplog.records if r.name == "blossom.app"] == [
        f"a form could not be read: {failure.__name__}"
    ] * 2
    assert [r for r in caplog.records if TYPED in r.getMessage()] == []


@pytest.mark.parametrize("failure", [RuntimeError, ValueError], ids=lambda f: f.__name__)
def test_another_failure_while_the_form_is_read_is_not_taken_for_an_unreadable_one(
    failure: type[Exception], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the parser's own failures are a form that could not be read; anything else raised
    while the form is read still raises, a ValueError the parser didn't raise included."""

    async def broken(*_: object, **__: object) -> None:
        msg = "not the parser"
        raise failure(msg)

    monkeypatch.setattr(Request, "form", broken)
    with household_client("open", tmp_path) as client:
        fields = client.post(SIGN_IN, data={"passphrase": "x"}, headers=PAGE_HEADERS)
        with pytest.raises(failure):
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


# ------------------------------------------------------------------ 6. the parser's own log


QUOTED_BY_THE_PARSER: Final = {
    "a boundary's byte": b"not the boundary" + CR + CR + TYPED.encode(),
    "a header's byte": b"--synthetic" + CR + b"Content Disposition: form-data" + CR + CR,
    "a header's line end": (
        b"--synthetic" + CR + b'Content-Disposition: form-data; name="note"\rX' + TYPED.encode()
    ),
    "the headers' end": (
        b"--synthetic" + CR + b'Content-Disposition: form-data; name="note"' + CR + b"\rX"
    ),
}
"""Multipart the parser rejects with a warning of its own that names a byte of the body."""


class Kept(logging.Handler):
    """A plain handler on the root logger, as a server's log setup adds one: every record it
    is handed, kept."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_the_parser_logs_nothing_of_a_body_it_rejects_and_the_kind_is_still_logged(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The parser's own lines, which quote a byte of the body even as a number, reach no
    handler, pytest's or a plain one on the root logger; the application's line naming the
    kind alone still does, once for each body refused."""
    caplog.set_level(logging.DEBUG)
    kept = Kept()
    logging.getLogger().addHandler(kept)
    try:
        with household_client("open", tmp_path) as client:
            answers = [
                press(client, path, body, MULTIPART)
                for body in QUOTED_BY_THE_PARSER.values()
                for path in (ASK, DECIDE)
            ]
    finally:
        logging.getLogger().removeHandler(kept)

    assert [answer.status_code for answer in answers] == [400] * len(answers)
    for records in (caplog.records, kept.records):
        said = [(r.name, r.getMessage()) for r in records]
        assert [line for line in said if line[0].startswith("python_multipart")] == []
        assert [line for line in said if re.search(r"\b(?:45|110|32|88)\b", line[1])] == []
        assert [message for name, message in said if name == "blossom.app"] == [
            "a form could not be read: MultipartParseError"
        ] * len(answers)


# ------------------------------------------------------------------ 7. back to Help


@pytest.mark.parametrize("reader", ["her", "open"])
def test_her_ask_for_help_goes_back_to_help_on_her_week(
    reader: str, tmp_path: pathlib.Path
) -> None:
    """An Ask for help that could not be read goes back to Help on her week, a note's after
    the note, and Help is there to land on."""
    body, kind = unreadable()["multipart the parser rejects"]
    note_ask = f"/student/actions/homework-notes/{NOTE}/ask-for-help"
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        shown = {
            path: ways_back_of(main_of(press(client, path, body, kind).text))
            for path in (
                ASK,
                note_ask,
                f"/student/actions/homework-notes/{NOT_A_NOTE}/ask-for-help",
            )
        }
        week = client.get(WEEK, headers=PAGE_HEADERS)

    assert shown == {
        ASK: [BACK_TO_HELP],
        note_ask: [(f"/student/homework-notes/{NOTE}", "Back to the note"), BACK_TO_HELP],
        f"/student/actions/homework-notes/{NOT_A_NOTE}/ask-for-help": [BACK_TO_HELP],
    }
    assert week.status_code == 200
    assert len(re.findall(r'<h2 id="help" tabindex="-1">', week.text)) == 1


# ------------------------------------------------------------------ 8. half of a character


def part(disposition: str, content: bytes) -> bytes:
    """One multipart part split by the boundary ``synthetic``."""
    head = f"Content-Disposition: {disposition}".encode()
    return b"--synthetic" + CR + head + CR + CR + content + CR


UTF_7: Final = "multipart/form-data; charset=utf-7; boundary=synthetic"


@pytest.mark.parametrize("where", ["a field's name", "a file's name", "a file's bytes"])
def test_half_a_character_in_any_text_the_parser_decodes_is_a_form_that_cannot_be_read(
    where: str, tmp_path: pathlib.Path
) -> None:
    """A field's name is text the parser decodes, so half a character there is the 400 page,
    through a handler and through a form of parameters alike, where the same body with a plain
    text part is read; a file part is the 400 page whatever its name or bytes hold."""
    plain = part('form-data; name="f"', b"words")
    sent = {
        "a field's name": part(f'form-data; name="{HALF}"', b"1"),
        "a file's name": part(f'form-data; name="f"; filename="{HALF}"', b"words"),
        "a file's bytes": part('form-data; name="f"; filename="a.txt"', HALF.encode()),
    }[where]
    answers = {}
    with household_client("open", tmp_path) as client:
        for path in (ASK, DECIDE):
            note = part('form-data; name="note"', TYPED.encode())
            answers[path] = (
                press(client, path, note + plain + b"--synthetic--" + CR, UTF_7),
                press(client, path, note + sent + b"--synthetic--" + CR, UTF_7),
            )

    for path, (control, answer) in answers.items():
        assert control.status_code != 400, path
        heading, said, back = expected(path, "open")
        main = store_free_page(answer, status=400, heading=heading, alert=said)
        assert ways_back_of(main) == back, path


UNKEPT: Final = "That has a character Blossom cannot keep."


def test_a_control_character_keeps_its_own_answer_and_half_a_character_is_the_page(
    tmp_path: pathlib.Path,
) -> None:
    """A new note's words with a control character in them are refused on the note's own
    page, 422, with its words; the same words with half a character in them are a form that
    could not be read. Neither is saved."""
    with household_client("her", tmp_path) as client:
        sign_in_as(client, "her")
        new = whole_form(
            client.get("/student/homework-notes/new", headers=PAGE_HEADERS).text,
            "/student/actions/homework-notes",
        )
        before = every_row(database_of(client))
        control = client.post(
            "/student/actions/homework-notes",
            data={**new, "text": "a bell \x07 here"},
            headers=PAGE_HEADERS,
        )
        half = press(
            client,
            "/student/actions/homework-notes",
            *multipart(
                list({**new, "text": f"a bell {HALF} here"}.items()), "synthetic", charset="utf-7"
            ),
        )
        after = every_row(database_of(client))

    assert control.status_code == 422
    assert UNKEPT in words(control.text)
    store_free_page(half, status=400, heading="Nothing was saved", alert=NOTHING_SAVED)
    assert after == before


# ------------------------------------------------------------------ 9. a file among the fields


FILE_NAME: Final = "Paper lanterns.txt"
"""A file's name in a body that carries a file, which no answer and no log line may show."""
MORE_FORMS: Final = (
    "/student/actions/plan",
    "/student/actions/too-much",
    f"/student/actions/assignments/{ESSAY_ID}/earlier-work",
    "/student/grades/view",
    *[
        f"/parent/grades/{rest}"
        for rest in (
            "add/edit",
            "add/review",
            "add/save",
            "add/check",
            "view",
            "current-term",
            "homework-classes",
            f"results/{uuid4().hex}/homework",
            f"results/{uuid4().hex}/homework/search",
        )
    ],
)
"""The form routes the tests above don't press: her plan, Too much right now, earlier work, and
the grade pages."""
EVERY_FORM_ROUTE: Final = (*EVERY_FORM, *MORE_FORMS)
NO_BODY: Final = frozenset(
    {
        "/sign-out",
        "/student/workload-signals/{signal_id}",
        "/student/help-requests/{request_id}",
        "/student/actions/take-back-help/{request_id}",
        "/student/actions/take-back/{signal_id}",
        "/parent/imports/{draft_id}/images/{image_id}/remove",
    }
)
"""The routes that change something and read no body at all."""
READS_ITS_OWN_BODY: Final = frozenset(
    {
        "/parent/imports",
        "/parent/imports/{draft_id}/images",
        "/parent/imports/{draft_id}/read",
        "/parent/imports/{draft_id}/review",
        "/parent/imports/{draft_id}/find",
        "/parent/imports/{draft_id}/set-aside",
        "/parent/imports/{draft_id}/finish",
        "/parent/imports/{draft_id}/discard",
        "/parent/imports/{draft_id}/not-hers",
    }
)
"""The import routes that take no form parameter and read their body themselves once their
parent-only check has passed: an upload's multipart body within its limits, a Read press's two
fields, a review press's answers against the fields its page signed, a search's card and
words, and the review's small signed forms that set rows aside or discard the review.
``tests/test_import_routes.py`` presses them."""
READS: Final = frozenset({"GET", "HEAD"})
PARENT_MAY_PRESS: Final = frozenset({"/student/actions/plan", "/student/grades/view"})
"""Her routes that read the body of a parent's press rather than refusing it first: the plan
and the grades view."""


SHOWS_HER_WEEK: Final = frozenset(
    {
        REPORT,
        f"/student/actions/assignments/{ESSAY_ID}/undo-report",
        f"/student/actions/assignments/{ESSAY_ID}/hand-in",
        f"/student/actions/assignments/{ESSAY_ID}/undo-hand-in",
        f"/student/actions/assignments/{ESSAY_ID}/earlier-work",
        ASK,
        "/student/actions/too-much",
    }
)
"""Her presses that tell a parent, 403, on her current week, that the press is hers: the
refusal reads the store to show her week. Every other refusal here reads no store."""
TEXT_ONLY: Final = multipart([("note", TYPED)], "synthetic")
"""A multipart body of the typed note alone, the file part's body without its file."""
MOMENT: Final = re.compile(r"'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00'")
"""A moment of the real clock written into a statement, as a store's retention window is."""


def traced(
    client: TestClient,
    state: ApplicationState,
    calls: list[str],
    path: str,
    sent: tuple[bytes, str],
) -> tuple[Answer, list[str], list[str]]:
    """A press, with the store calls it made and the statements the stores ran for it, each
    moment of the real clock in them written as ``'<moment>'``, so two presses compare."""
    calls.clear()
    with Statements(state) as seen:
        answer = press(client, path, *sent)
    return answer, list(calls), [MOMENT.sub("'<moment>'", statement) for statement in seen]


def refused_before_reading(path: str, reader: str) -> bool:
    """Whether the route refuses this reader, 403, before it reads a body: the family's tree
    to her, hers to a parent but for the routes a parent may press, and the family's grade
    routes to a client that is not this computer when the sign-in is off."""
    if reader == "open":
        return path.startswith("/parent/grades/")
    if reader == "her":
        return path.startswith("/parent/")
    return path.startswith("/student/") and path not in PARENT_MAY_PRESS


def with_a_file(filename: str) -> tuple[bytes, str]:
    """A multipart body of a typed note and one file part holding the typed words."""
    filed = part(f'form-data; name="attachment"; filename="{filename}"', TYPED.encode())
    note = part('form-data; name="note"', TYPED.encode())
    return note + filed + b"--synthetic--" + CR, MULTIPART


@pytest.fixture
def files_made(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The size each temporary file the form parser makes for a file part may hold in
    memory, recorded as the file is made."""
    made: list[int] = []

    def making(max_size: int) -> SpooledTemporaryFile[bytes]:
        made.append(max_size)
        return SpooledTemporaryFile(max_size=max_size)

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", making)
    return made


def test_the_counted_class_is_the_one_the_parser_makes_for_a_file_part(
    files_made: list[int],
) -> None:
    """Read with Starlette's own limits, outside the application, a file part makes one
    temporary file through the class these tests count, so a count of none is a refusal
    and not a file made some other way."""
    body, kind = with_a_file(FILE_NAME)
    messages: list[Message] = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> Message:
        return messages.pop(0)

    async def read() -> list[str | None]:
        scope: Scope = {
            "type": "http",
            "method": "POST",
            "headers": [(b"content-type", kind.encode())],
        }
        async with Request(scope, receive).form() as form:
            return [value.filename for _, value in form.multi_items() if not isinstance(value, str)]

    assert asyncio.run(read()) == [FILE_NAME]
    assert files_made == [MultiPartParser.spool_max_size]


def test_every_route_that_changes_something_is_a_form_route_or_reads_no_form(
    tmp_path: pathlib.Path,
) -> None:
    """Every route that takes a method other than a read is a FastAPI route: one pressed here
    as a form route, one that reads no body, named here, or one whose body is JSON. A route
    whose fields are form parameters is a ``FormRoute``, and the one mount serves files. So
    a new route is one or the other before these tests pass. Screenshot import is on, so its
    routes are served to be counted."""
    with household_client("open", tmp_path, screenshot_import=True) as client:
        served = list(every_route(cast(FastAPI, client.app).routes))
    mounts = [route for route in served if isinstance(route, Mount)]
    routes = [route for route in served if isinstance(route, Route)]
    writes = [route for route in routes if route.methods is None or route.methods - READS]
    api = [route for route in writes if isinstance(route, APIRoute)]
    json_bodies = {
        route.path
        for route in api
        if route.body_field is not None
        and not isinstance(route.body_field.field_info, FormParameter)
    }
    pressed = {
        route.path for route in api for path in EVERY_FORM_ROUTE if route.path_regex.fullmatch(path)
    }

    assert len(mounts) + len(routes) == len(served)
    assert [type(mount.app) for mount in mounts] == [StaticFiles]
    assert [route.path for route in writes if not isinstance(route, APIRoute)] == []
    for route in api:
        if route.body_field is not None and isinstance(route.body_field.field_info, FormParameter):
            assert isinstance(route, FormRoute), route.path
        if route.path in NO_BODY | READS_ITS_OWN_BODY:
            assert route.body_field is None, route.path
    assert {route.path for route in api} - json_bodies - NO_BODY - READS_ITS_OWN_BODY == pressed
    assert {route.path for route in api} >= NO_BODY | READS_ITS_OWN_BODY
    assert pressed.isdisjoint(json_bodies | NO_BODY)


@pytest.mark.parametrize("filename", [FILE_NAME, ""], ids=["named", "unnamed"])
@pytest.mark.parametrize("reader", ["her", "parent", "open"])
def test_a_file_among_the_fields_is_a_form_that_cannot_be_read_and_no_file_is_made(
    reader: str,
    filename: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    files_made: list[int],
) -> None:
    """Every form route answers a body carrying a file part, named or with an empty name, as
    a body the parser can't read. Where the route refuses the reader before reading a body,
    it answers with that same 403, reading the store exactly as its refusal of the typed
    note alone does: not at all, but for her week shown to a parent. Otherwise it answers
    the 400 page with the route's ways back, reading no store. No temporary file is made,
    no row changes, no cookie is set, and neither the typed words nor the file's name reach
    a page or a log line."""
    caplog.set_level(logging.DEBUG)
    body, kind = with_a_file(filename)
    broken, broken_kind = unreadable()["multipart the parser rejects"]
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        state = state_of(client)
        before = every_row(database_of(client))
        calls: list[str] = []
        spy_on_stores(monkeypatch, state, calls)
        for path in EVERY_FORM_ROUTE:
            refused = refused_before_reading(path, reader)
            refused_first = press(client, path, broken, broken_kind)
            if refused:
                text_only, text_calls, text_seen = traced(client, state, calls, path, TEXT_ONLY)
            rows = every_row(database_of(client))
            answer, file_calls, file_seen = traced(client, state, calls, path, (body, kind))
            assert files_made == [], path
            assert every_row(database_of(client)) == rows, path
            assert "set-cookie" not in answer.headers, path
            assert TYPED not in answer.text, path
            assert not filename or filename not in answer.text, path
            if refused:
                assert refused_first.status_code == text_only.status_code == 403, path
                assert as_met(answer) == as_met(refused_first), path
                assert (file_calls, file_seen) == (text_calls, text_seen), path
                shows_her_week = reader == "parent" and path in SHOWS_HER_WEEK
                assert bool(file_calls) == bool(file_seen) == shows_her_week, (path, file_calls)
                continue
            assert refused_first.status_code == 400, path
            heading, said, back = expected(path, reader)
            main = store_free_page(answer, status=400, heading=heading, alert=said)
            assert ways_back_of(main) == back, path
            assert file_calls == [], (path, file_calls)
            assert file_seen == [], (path, file_seen[:3])
        after = every_row(database_of(client))

    assert after == before
    logged = [r.getMessage() for r in caplog.records]
    assert [line for line in logged if TYPED in line or (filename and filename in line)] == []
    assert {r.getMessage() for r in caplog.records if r.name == "blossom.app"} == {
        "a form could not be read: MultipartParseError",
        "a form could not be read: MultiPartException",
    }


def test_a_text_form_answers_the_same_sent_either_way_and_makes_no_file(
    tmp_path: pathlib.Path, files_made: list[int]
) -> None:
    """A form of text alone reads through a handler and through a form of parameters alike,
    url-encoded or multipart: the right passphrase opens the door either way, and a note
    answers as the same note sent the other way does."""
    help_path = f"/parent/actions/help/{uuid4().hex}"
    paths = (ASK, REPORT, PASTE, DECIDE, help_path, "/parent/actions/plan")
    answers = {}
    for encoding in ("url-encoded", "multipart"):

        def sent(fields: list[tuple[str, str]], encoding: str = encoding) -> tuple[bytes, str]:
            if encoding == "multipart":
                return multipart(fields, "synthetic")
            return urlencode(fields).encode(), URL_ENCODED

        with household_client("her", tmp_path / encoding / "her") as client:
            door = press(client, SIGN_IN, *sent([("passphrase", HERS)]))
            week = client.get(WEEK, headers=PAGE_HEADERS).status_code
        with household_client("open", tmp_path / encoding / "open") as client:
            notes = {path: as_met(press(client, path, *sent([("note", SENT)]))) for path in paths}
        answers[encoding] = ((door.status_code, "set-cookie" in door.headers, week), notes)

    assert answers["multipart"] == answers["url-encoded"]
    door_met, notes = answers["multipart"]
    assert door_met == (303, True, 200)
    assert {path: met[0] for path, met in notes.items()} == dict.fromkeys(paths, 422)
    assert files_made == []
