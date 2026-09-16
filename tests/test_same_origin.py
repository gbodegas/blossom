"""A request that would change something must come from this server's own pages.

A page on another site can make a signed-in browser send a form here, cookie
and all; what gives it away is the browser's own word for where the form came
from, the Origin header, or failing that the Referer. The gate reads one of
them against the address the request was sent to, for every method but GET,
HEAD, and OPTIONS, before the sign-in and before the open routes, and refuses
in plain text what does not match. The test client sends the header with every
request; the clients here leave it off so each request says exactly what it
carries.
"""

import pathlib

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.household import ELSEWHERE, read_authority, read_origin
from blossom.settings import Settings
from tests.support import ORIGIN, SAME_ORIGIN, fixture_settings

PAGE = {"Accept": "text/html"}
HERS = "the blue bicycle in the hallway"
THEIRS = "coffee before the school run"
Headers = dict[str, str] | list[tuple[str, str]]


def signed_in_household(tmp_path: pathlib.Path) -> Settings:
    return fixture_settings(
        BLOSSOM_TODAY="2026-08-19",
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
        BLOSSOM_STUDENT_PASSPHRASE=HERS,
        BLOSSOM_PARENT_PASSPHRASE=THEIRS,
    )


def is_the_refusal(response: object) -> bool:
    """The gate's own answer: 403, plain text, the one sentence, and not to be stored."""
    status = getattr(response, "status_code", None)
    headers = getattr(response, "headers", {})
    text = getattr(response, "text", "")
    return (
        status == 403
        and headers.get("content-type", "").startswith("text/plain")
        and text == ELSEWHERE
        and headers.get("cache-control") == "no-store"
    )


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        pytest.param({"Origin": ORIGIN}, True, id="origin"),
        pytest.param({"Origin": "HTTP://TestServer"}, True, id="origin-in-another-case"),
        pytest.param({"Origin": "http://testserver:80"}, True, id="origin-with-the-default-port"),
        pytest.param({"Referer": "http://testserver/parent"}, True, id="referer"),
        pytest.param(
            {"Referer": "http://testserver:80/student/due-this-week?week=2026-08-24#today"},
            True,
            id="referer-with-port-query-and-fragment",
        ),
        pytest.param({}, False, id="neither"),
        pytest.param({"Origin": "null"}, False, id="origin-null"),
        pytest.param({"Origin": ""}, False, id="origin-empty"),
        pytest.param({"Origin": "http://testserver/"}, False, id="origin-with-a-path"),
        pytest.param({"Origin": "http://testserver?week=1"}, False, id="origin-with-a-query"),
        pytest.param({"Origin": "https://testserver"}, False, id="origin-other-scheme"),
        pytest.param({"Origin": "http://testserver:8000"}, False, id="origin-other-port"),
        pytest.param({"Origin": "http://elsewhere"}, False, id="origin-other-host"),
        pytest.param({"Origin": "http://testserver.example"}, False, id="origin-longer-host"),
        pytest.param({"Origin": "http://someone@testserver"}, False, id="origin-with-a-user"),
        pytest.param({"Origin": "testserver"}, False, id="origin-without-a-scheme"),
        pytest.param({"Origin": "file://testserver"}, False, id="origin-file-scheme"),
        pytest.param({"Origin": "http://test server"}, False, id="origin-with-a-space"),
        pytest.param(
            {"Origin": "null", "Referer": "http://testserver/parent"},
            False,
            id="bad-origin-is-not-rescued-by-the-referer",
        ),
        pytest.param(
            {"Origin": "http://elsewhere", "Referer": "http://testserver/parent"},
            False,
            id="other-origin-is-not-rescued-by-the-referer",
        ),
        pytest.param({"Referer": "http://elsewhere/parent"}, False, id="referer-other-host"),
        pytest.param({"Referer": "https://testserver/parent"}, False, id="referer-other-scheme"),
        pytest.param({"Referer": "http://testserver:8000/"}, False, id="referer-other-port"),
        pytest.param({"Referer": "/parent"}, False, id="referer-relative"),
        pytest.param({"Referer": "not an address"}, False, id="referer-unreadable"),
        pytest.param({"Referer": ""}, False, id="referer-empty"),
        pytest.param([("Origin", ORIGIN), ("Origin", ORIGIN)], False, id="two-origins"),
        pytest.param(
            [("Referer", "http://testserver/a"), ("Referer", "http://testserver/b")],
            False,
            id="two-referers",
        ),
        pytest.param(
            [("Host", "testserver"), ("Host", "testserver"), ("Origin", ORIGIN)],
            False,
            id="two-hosts",
        ),
        pytest.param({"Host": "testserver:80:1", "Origin": ORIGIN}, False, id="host-unreadable"),
        pytest.param(
            {"Host": "testserver:port", "Origin": ORIGIN}, False, id="host-port-not-a-number"
        ),
        pytest.param({"Host": "testserver:0", "Origin": ORIGIN}, False, id="host-port-zero"),
        pytest.param({"Host": "test server", "Origin": ORIGIN}, False, id="host-with-a-space"),
        pytest.param({"Host": "testserver/x", "Origin": ORIGIN}, False, id="host-with-a-path"),
        pytest.param({"Host": "", "Origin": ORIGIN}, False, id="host-empty"),
        pytest.param({"Host": "[::1]:8000", "Origin": "http://[::1]:8000"}, True, id="ipv6"),
        pytest.param({"Host": "[::1]", "Origin": "http://[::1]:80"}, True, id="ipv6-default-port"),
        pytest.param({"Host": "[::1]:8000", "Origin": "http://[::1]"}, False, id="ipv6-other-port"),
        pytest.param({"Host": "[::1", "Origin": "http://[::1]"}, False, id="ipv6-unclosed"),
        pytest.param({"Host": "::1", "Origin": "http://[::1]"}, False, id="ipv6-unbracketed"),
    ],
)
def test_a_request_that_changes_something_is_judged_by_where_it_came_from(
    headers: Headers, allowed: bool
) -> None:
    """Sent to the JSON route that asks for help: allowed, it is answered 201 and the request
    is kept; refused, it is answered 403 in plain text, not to be stored, and nothing is kept."""
    with TestClient(create_app(fixture_settings())) as client:
        answer = client.post("/student/help-requests", json={"note": "hi"}, headers=headers)
        kept = client.get("/student/help-requests").json()

    if allowed:
        assert answer.status_code == 201
        assert len(kept) == 1
    else:
        assert is_the_refusal(answer), (answer.status_code, answer.text)
        assert kept == []


def test_reading_gets_no_check_and_every_other_method_does() -> None:
    """GET passes without a word about where it came from, and HEAD and OPTIONS reach the
    routes, which have no answer for them but 405; POST and DELETE are checked, the JSON
    routes and the forms alike, and the refusal names nothing about the route, so it reads
    the same for a route that exists and one that does not."""
    with TestClient(create_app(fixture_settings()), follow_redirects=False) as client:
        asked = client.post("/student/help-requests", headers=SAME_ORIGIN).json()
        request_id = asked["request"]["request_id"]
        page = client.get("/student/due-this-week", headers=PAGE)
        head = client.head("/student/due-this-week")
        options = client.options("/student/due-this-week")
        calls = client.get("/student/help-requests")
        deleted = client.delete(f"/student/help-requests/{request_id}")
        form = client.post("/student/actions/ask-for-help", data={"note": "hi"}, headers=PAGE)
        nowhere = client.post("/no/such/route")
        still_there = client.get("/student/help-requests").json()
        taken_back = client.delete(f"/student/help-requests/{request_id}", headers=SAME_ORIGIN)

    assert page.status_code == 200
    assert head.status_code == 405
    assert options.status_code == 405
    assert calls.status_code == 200
    assert is_the_refusal(deleted)
    assert is_the_refusal(form)
    assert is_the_refusal(nowhere)
    assert len(still_there) == 1
    assert taken_back.status_code == 204


def test_the_check_comes_before_the_sign_in_and_before_the_open_routes(
    tmp_path: pathlib.Path,
) -> None:
    """With the sign-in on, a browser with no sign-in that sends a form from elsewhere is
    refused, not sent to sign in; the sign-in and sign-out forms and the static files are
    checked too; and a signed-in student's form from elsewhere is refused with her cookie
    in hand, while the same form from her own page goes through."""
    with TestClient(create_app(signed_in_household(tmp_path)), follow_redirects=False) as client:
        anonymous_form = client.post("/student/help-requests", json={"note": "hi"}, headers=PAGE)
        anonymous_call = client.post("/student/help-requests", json={"note": "hi"})
        sign_in_from_elsewhere = client.post("/sign-in", data={"passphrase": HERS})
        sign_out_from_elsewhere = client.post("/sign-out")
        static_from_elsewhere = client.post("/static/blossom.css")
        static_from_here = client.post("/static/blossom.css", headers=SAME_ORIGIN)
        sign_in_page = client.get("/sign-in", headers=PAGE)
        came_in = client.post("/sign-in", data={"passphrase": HERS}, headers=SAME_ORIGIN)
        from_elsewhere = client.post("/student/help-requests", json={"note": "hi"})
        from_her_page = client.post(
            "/student/help-requests",
            json={"note": "hi"},
            headers={"Referer": "http://testserver/student/due-this-week"},
        )
        kept = client.get("/student/help-requests").json()
        signed_out = client.post("/sign-out", headers=SAME_ORIGIN)

    assert is_the_refusal(anonymous_form)
    assert is_the_refusal(anonymous_call)
    assert is_the_refusal(sign_in_from_elsewhere)
    assert is_the_refusal(sign_out_from_elsewhere)
    assert is_the_refusal(static_from_elsewhere)
    assert static_from_here.status_code == 405
    assert sign_in_page.status_code == 200
    assert came_in.status_code == 303
    assert is_the_refusal(from_elsewhere)
    assert from_her_page.status_code == 201
    assert len(kept) == 1
    assert signed_out.status_code == 303


@pytest.mark.parametrize(
    ("text", "scheme", "expected"),
    [
        ("testserver", "http", ("testserver", 80)),
        ("TestServer", "https", ("testserver", 443)),
        ("testserver:8000", "http", ("testserver", 8000)),
        ("testserver:65535", "http", ("testserver", 65535)),
        ("[::1]", "http", ("[::1]", 80)),
        ("[2001:DB8::1]:8000", "http", ("[2001:db8::1]", 8000)),
        ("testserver:65536", "http", None),
        ("testserver:0", "http", None),
        ("testserver:", "http", None),
        ("testserver:٨٠", "http", None),
        ("testserver:8000:1", "http", None),
        (":8000", "http", None),
        ("[]", "http", None),
        ("[::1]8000", "http", None),
        ("testserver", "ftp", None),
        ("", "http", None),
        ("test\tserver", "http", None),
        ("testserver#frag", "http", None),
    ],
)
def test_an_authority_is_read_whole_or_not_at_all(
    text: str, scheme: str, expected: tuple[str, int] | None
) -> None:
    assert read_authority(text, scheme) == expected


@pytest.mark.parametrize(
    ("value", "whole_address", "expected"),
    [
        ("http://testserver", False, ("http", "testserver", 80)),
        ("https://Testserver:8443", False, ("https", "testserver", 8443)),
        ("http://testserver/", False, None),
        ("http://testserver#", False, None),
        ("null", False, None),
        ("http://", False, None),
        ("http://[::1", False, None),
        ("http://testserver/parent?week=1#x", True, ("http", "testserver", 80)),
        ("http://testserver\tx/parent", True, None),
        ("ws://testserver/parent", True, None),
    ],
)
def test_an_origin_is_a_scheme_and_an_authority_and_a_referer_is_a_whole_address(
    value: str, whole_address: bool, expected: tuple[str, str, int] | None
) -> None:
    assert read_origin(value, whole_address=whole_address) == expected
