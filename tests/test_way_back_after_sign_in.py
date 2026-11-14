# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The page asked for without a sign-in is the page returned to after it.

A page added under each tree notes the id and the query it receives, so a case compares
what the server was asked for after the sign-in with what was asked for before it, by the
tests' client and over a real loopback server. The household is made for the test, and the
passphrases are these two.
"""

import html
import http.client
import pathlib
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import NamedTuple, cast
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.household import COOKIE, home_of, way_back
from blossom.principals import Principal
from tests.import_review_cases import serving
from tests.support import SAME_ORIGIN, fixture_settings

HERS = "quiet mornings and loud music"
THEIRS = "the kitchen table at seven"
PAGE = {"Accept": "text/html"}
HER_PAGES = "/student/asked-for"
THEIR_PAGES = "/parent/asked-for"

IDS = {
    "a plain id": ("plain-id", "plain-id"),
    "an escaped question mark": ("unit%3Fpart", "unit?part"),
    "an escaped number sign": ("unit%23part", "unit#part"),
    "both and a space": ("unit%20a%3Fb%23c", "unit a?b#c"),
    "an escaped slash": ("unit%2F3", "unit/3"),
    "colons": ("draft:plan:2026-08-19:abcd", "draft:plan:2026-08-19:abcd"),
}
"""Ids as a page's address carries them, and as the route receives them."""

OVER_A_SOCKET = {
    "a plain id": IDS["a plain id"],
    "an escaped question mark": IDS["an escaped question mark"],
    "an escaped number sign": IDS["an escaped number sign"],
    "the text of an escape": ("unit%2541", "unit%41"),
    "a percent sign alone": ("100%25", "100%"),
}
"""Ids asked for over a real server, which decodes a path once. The tests' client decodes a
path twice, so the text of an escape inside an id is only told apart here. A percent sign
alone is a control: no escape follows it, so it reads the same however often it is decoded."""

QUERY = "choice=one&choice=two&blank=&b=2&a=1&note=a%20b+c%26d"
"""A query with a repeated field, a blank one, fields out of alphabetical order and escapes
of its own."""

READERS = {"the student": (HERS, HER_PAGES), "a parent": (THEIRS, THEIR_PAGES)}
"""Who signs in, and the tree the page asked for is under."""


def noting(asked: list[tuple[str, str]]) -> Callable[..., Awaitable[PlainTextResponse]]:
    """A page that notes each id and query it receives."""

    async def page(request: Request, item: str) -> PlainTextResponse:
        asked.append((item, request.scope["query_string"].decode()))
        return PlainTextResponse("reached")

    return page


@contextmanager
def a_household(tmp_path: pathlib.Path, asked: list[tuple[str, str]]) -> Iterator[TestClient]:
    """A household with the sign-in on and such a page under each tree, as a browser on this
    origin reaches it, no redirect followed."""
    settings = fixture_settings(
        BLOSSOM_TODAY="2026-08-19",
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
        BLOSSOM_STUDENT_PASSPHRASE=HERS,
        BLOSSOM_PARENT_PASSPHRASE=THEIRS,
    )
    with TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN) as client:
        for tree in (HER_PAGES, THEIR_PAGES):
            cast("FastAPI", client.app).add_api_route(
                f"{tree}/{{item:path}}", noting(asked), methods=["GET", "POST"]
            )
        yield client


def way_back_on(page: str) -> dict[str, str]:
    """The way back the sign-in page carries in its form, as a browser would send it; no
    field when the page carries none."""
    found = re.search(r'name="next" value="([^"]*)"', page)
    return {} if found is None else {"next": html.unescape(found.group(1))}


def signed_in_from(client: TestClient, target: str, passphrase: str) -> str:
    """Ask for ``target`` with no sign-in, open the sign-in page the gate sends to, and sign
    in with the form as that page wrote it. Where the sign-in sends the browser."""
    sent = client.get(target, headers=PAGE)
    assert sent.status_code == 303, sent.text[:300]
    sign_in = client.get(sent.headers["location"], headers=PAGE)
    assert sign_in.status_code == 200, sign_in.text[:300]
    came_in = client.post("/sign-in", data={"passphrase": passphrase, **way_back_on(sign_in.text)})
    assert came_in.status_code == 303, came_in.text[:300]
    return str(came_in.headers["location"])


@pytest.mark.parametrize("reader", list(READERS))
@pytest.mark.parametrize("case", list(IDS))
def test_the_sign_in_returns_to_the_same_id_and_query(
    case: str, reader: str, tmp_path: pathlib.Path
) -> None:
    """A page asked for with no sign-in, its id holding what an address escapes: after the
    sign-in the page receives the id and the query that were first asked for, the query in
    its own spelling and order, repeated fields and all."""
    sent, item = IDS[case]
    passphrase, tree = READERS[reader]
    asked: list[tuple[str, str]] = []
    with a_household(tmp_path, asked) as client:
        back_to = signed_in_from(client, f"{tree}/{sent}?{QUERY}", passphrase)
        before = list(asked)
        back = client.get(back_to, headers=PAGE)

    assert before == [], "the page is not reached before the sign-in"
    assert back.status_code == 200, (back_to, back.text[:300])
    assert asked == [(item, QUERY)]


def test_the_sign_in_returns_to_an_id_asked_for_with_no_query(tmp_path: pathlib.Path) -> None:
    """With no query, an id's escaped question mark does not become one."""
    asked: list[tuple[str, str]] = []
    with a_household(tmp_path, asked) as client:
        back_to = signed_in_from(client, f"{HER_PAGES}/unit%3Fpart", HERS)
        back = client.get(back_to, headers=PAGE)

    assert back.status_code == 200, (back_to, back.text[:300])
    assert asked == [("unit?part", "")]


ADDRESSES = {
    "a plain path": ("/student/asked-for/plain-id", b"", "/student/asked-for/plain-id"),
    "a plain path and a query": (
        "/student/asked-for/plain-id",
        QUERY.encode(),
        f"/student/asked-for/plain-id?{QUERY}",
    ),
    "a question mark": ("/a/unit?part", b"x=1", "/a/unit%3Fpart?x=1"),
    "a number sign": ("/a/unit#part", b"x=1", "/a/unit%23part?x=1"),
    "the text of an escape": ("/a/unit%41", b"", "/a/unit%2541"),
    "the text of the escapes this makes": ("/a/%3F%23%25", b"", "/a/%253F%2523%2525"),
    "a percent sign no escape follows": ("/a/100%zz", b"", "/a/100%25zz"),
    "all three": ("/a/50%?#", b"", "/a/50%25%3F%23"),
    "a slash, a space and colons": ("/a/unit/3 b:c", b"", "/a/unit/3 b:c"),
    "dot segments, left for a browser to read": ("/a/unit/../3", b"", "/a/unit/../3"),
    "a backslash, left for the sign-in to refuse": ("/a/unit\\part", b"", "/a/unit\\part"),
    "a line break, left for the sign-in to refuse": ("/a/unit\npart", b"", "/a/unit\npart"),
    "a number sign in a query as sent": ("/a/unit", b"x=1#y", "/a/unit?x=1%23y"),
}
"""A path as a server decodes it and a query as a request sends it, and the address made."""


@pytest.mark.parametrize("case", list(ADDRESSES))
def test_the_way_back_escapes_again_only_what_would_change_the_address(case: str) -> None:
    """A percent sign, a question mark and a number sign in a decoded path are escaped again,
    the percent sign first, so the text of an escape inside an id is not read as one. The
    rest of the path is as it was. The query is kept as a browser sends one, in its order;
    a byte a browser would have escaped, a number sign among them, is escaped."""
    path, query, address = ADDRESSES[case]

    assert way_back(path, query) == address


class Answer(NamedTuple):
    """What a server answered over a socket."""

    status: int
    location: str
    cookie: str
    text: str


def over_a_socket(
    port: int,
    method: str,
    target: str,
    *,
    form: dict[str, str] | None = None,
    cookie: str = "",
) -> Answer:
    """One request written to the server as it is, its target untouched, and the answer."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Accept": "text/html", "Origin": f"http://127.0.0.1:{port}"}
    if cookie:
        headers["Cookie"] = cookie
    body = None
    if form is not None:
        body = urlencode(form)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        connection.request(method, target, body=body, headers=headers)
        answer = connection.getresponse()
        text = answer.read().decode()
        kept = (answer.getheader("Set-Cookie") or "").partition(";")[0]
        return Answer(answer.status, answer.getheader("Location") or "", kept, text)
    finally:
        connection.close()


@pytest.mark.parametrize("case", list(OVER_A_SOCKET))
def test_over_a_real_server_the_sign_in_returns_to_the_same_id_and_query(
    case: str, tmp_path: pathlib.Path
) -> None:
    """The same conversation over a loopback socket, with a server that decodes a path once:
    the page asked for, the sign-in page, the sign-in, and the address it sends to, asked
    for as a browser asks, which sends nothing from a number sign on. What the page receives
    after the sign-in is the id and the query first asked for."""
    sent, item = OVER_A_SOCKET[case]
    asked: list[tuple[str, str]] = []
    with a_household(tmp_path, asked) as client, serving(client) as port:
        first = over_a_socket(port, "GET", f"{HER_PAGES}/{sent}?{QUERY}")
        sign_in = over_a_socket(port, "GET", first.location)
        came_in = over_a_socket(
            port, "POST", "/sign-in", form={"passphrase": HERS, **way_back_on(sign_in.text)}
        )
        before = list(asked)
        as_a_browser_asks = came_in.location.partition("#")[0]
        back = over_a_socket(port, "GET", as_a_browser_asks, cookie=came_in.cookie)

    assert (first.status, sign_in.status, came_in.status) == (303, 200, 303)
    assert came_in.cookie.startswith(f"{COOKIE}=")
    assert before == [], "the page is not reached before the sign-in"
    assert back.status == 200, (came_in.location, back.text[:300])
    assert asked == [(item, QUERY)]


@pytest.mark.parametrize(
    "sent",
    ["unit%5Cpart", "unit%09part", "unit%0Apart"],
    ids=["a backslash", "a tab", "a line break"],
)
def test_an_id_the_destination_rule_refuses_sends_her_home(
    sent: str, tmp_path: pathlib.Path
) -> None:
    """An id holding a backslash, a tab or a line break makes an address the sign-in does
    not send a browser to: the sign-in page carries no way back, and she is sent home. No
    other id's page is reached."""
    asked: list[tuple[str, str]] = []
    with a_household(tmp_path, asked) as client:
        sent_to = client.get(f"{HER_PAGES}/{sent}?{QUERY}", headers=PAGE)
        sign_in = client.get(sent_to.headers["location"], headers=PAGE)
        came_in = client.post("/sign-in", data={"passphrase": HERS, **way_back_on(sign_in.text)})
        home = client.get(came_in.headers["location"], headers=PAGE)

    assert way_back_on(sign_in.text) == {}
    assert came_in.headers["location"] == home_of(Principal.STUDENT)
    assert home.status_code == 200
    assert asked == []


def test_a_student_returned_to_a_parents_page_is_refused_there(tmp_path: pathlib.Path) -> None:
    """A parent's page asked for with no sign-in, then the student's passphrase: the sign-in
    sends her to that address, and the gate answers it 403 as it does any parent's page. The
    page is not reached."""
    asked: list[tuple[str, str]] = []
    target = f"{THEIR_PAGES}/unit%3Fpart?{QUERY}"
    with a_household(tmp_path, asked) as client:
        back_to = signed_in_from(client, target, HERS)
        refused = client.get(back_to, headers=PAGE)
        theirs = client.get(f"{THEIR_PAGES}/plain-id", headers=PAGE)

    assert back_to.startswith(f"{THEIR_PAGES}/")
    assert refused.status_code == 403
    assert theirs.status_code == 403
    assert "reached" not in refused.text
    assert asked == []


def test_a_form_sent_with_no_sign_in_is_not_sent_again(tmp_path: pathlib.Path) -> None:
    """A form posted with no sign-in leads to the sign-in page alone, with no way back, so
    the sign-in can't send it a second time, whatever its address holds."""
    asked: list[tuple[str, str]] = []
    with a_household(tmp_path, asked) as client:
        sent = client.post(f"{HER_PAGES}/unit%3Fpart?{QUERY}", data={"note": "hi"}, headers=PAGE)
        sign_in = client.get(sent.headers["location"], headers=PAGE)
        came_in = client.post("/sign-in", data={"passphrase": HERS, **way_back_on(sign_in.text)})

    assert sent.status_code == 303
    assert sent.headers["location"] == "/sign-in"
    assert way_back_on(sign_in.text) == {}
    assert came_in.headers["location"] == home_of(Principal.STUDENT)
    assert asked == []
