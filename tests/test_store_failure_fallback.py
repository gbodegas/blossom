# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A store failure that no page's own recovery catches: the fallback page.

Each page in the inventory answers a failure of the household's store with its own page, which
can keep what was typed and say what saved. A failure one of them lets through, on a route
marked for HTML, gets the application's fallback instead of the server's plain error: a page
that reads no store, 503 with a Try again link for a GET or HEAD, 500 for any other method,
marked no-store, and one warning that names the failure by its kind, its code and the line in
the routes nearest it, never by its words. A failure that isn't the store failing, and any
failure on a route not marked for HTML, still reaches the server as itself.

The routes the fallback answers here are added to the application by each test, since every
real page in the inventory catches its own failures.
"""

import html
import inspect
import logging
import pathlib
import re
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from typing import cast
from urllib.parse import urljoin

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from markupsafe import escape
from starlette.types import ASGIApp, Receive, Scope, Send

from blossom import app as app_module
from blossom.app import create_app
from blossom.dependencies import STATE_ATTRIBUTE
from blossom.routes import navigation, store_refusals
from blossom.routes import student as student_routes
from blossom.settings import SCREENSHOT_IMPORT_VARIABLE
from blossom.stores.drafts import StoreBusy, WriterBusy
from blossom.unreadable import TextNotUtf8
from tests.support import (
    PAGE_HEADERS,
    PLAN_DATE,
    SAME_ORIGIN,
    HangWatch,
    HeldByAnother,
    LockHeldByAnother,
    client_for,
    database_of,
    fixture_settings,
    household_client,
    sign_in_as,
    spy_on_stores,
    state_of,
    store_free_page,
    ways_back_of,
)

FAMILY_PROBE = "/parent/fallback-probe"
HER_PROBE = "/student/fallback-probe"
IMPORT_PROBE = "/parent/imports/{draft_id}/fallback-probe"
CALL_PROBE = "/parent/imports/{draft_id}/fallback-call"
WHERE_PROBE = "/student/fallback-where"
"""The routes each test adds: three marked for HTML, in each tree and on an import address; one
not marked, as a JSON route is; and one whose failure comes from a store under a real route."""

NOT_SHOWN = str(escape("This page can't be shown right now"))
NOT_USED = "Blossom couldn't use the household's records."
UNKNOWN_HEADING = str(escape("This can't be shown right now"))
UNKNOWN = "Blossom couldn't tell whether this went through."
WARNED = "a store failure reached no page's own recovery: "
MARK = "MARK her words"
"""Words a failure carries that must reach no log line and no page."""


def coded(kind: type[sqlite3.Error], name: str) -> sqlite3.Error:
    """An error of ``kind`` as SQLite raises one: the result code ``name`` names and that name
    set on it, with words no log may repeat."""
    error = kind(f"{MARK}: database is locked")
    error.sqlite_errorcode = getattr(sqlite3, name)
    error.sqlite_errorname = name
    return error


def decode_refusal() -> sqlite3.OperationalError:
    """The sqlite3 module's own refusal of stored text that isn't UTF-8, on a connection that
    keeps its default text factory: an ``OperationalError`` with no result code, whose words
    quote the stored text."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE notes (words TEXT)")
        stored = f"{MARK} \xff".encode("latin-1")
        connection.execute("INSERT INTO notes VALUES (CAST(? AS TEXT))", (stored,))
        try:
            connection.execute("SELECT words FROM notes").fetchall()
        except sqlite3.OperationalError as refusal:
            return refusal
    finally:
        connection.close()
    msg = "text that isn't UTF-8 was read"
    raise AssertionError(msg)


STORE_FAILURES: dict[str, Callable[[], BaseException]] = {
    "the drafts store's lock held": StoreBusy,
    "the file's writer held": WriterBusy,
    "busy": lambda: coded(sqlite3.OperationalError, "SQLITE_BUSY"),
    "busy, an extended code": lambda: coded(sqlite3.OperationalError, "SQLITE_BUSY_SNAPSHOT"),
    "no permission": lambda: coded(sqlite3.OperationalError, "SQLITE_PERM"),
    "read-only": lambda: coded(sqlite3.OperationalError, "SQLITE_READONLY"),
    "read-only, an extended code": lambda: coded(
        sqlite3.OperationalError, "SQLITE_READONLY_DBMOVED"
    ),
    "a disk error": lambda: coded(sqlite3.OperationalError, "SQLITE_IOERR"),
    "a short read": lambda: coded(sqlite3.OperationalError, "SQLITE_IOERR_SHORT_READ"),
    "damaged": lambda: coded(sqlite3.DatabaseError, "SQLITE_CORRUPT"),
    "the disk full": lambda: coded(sqlite3.OperationalError, "SQLITE_FULL"),
    "not opened": lambda: coded(sqlite3.OperationalError, "SQLITE_CANTOPEN"),
    "a locking protocol error": lambda: coded(sqlite3.OperationalError, "SQLITE_PROTOCOL"),
    "not a database": lambda: coded(sqlite3.DatabaseError, "SQLITE_NOTADB"),
    "a commit past its wait, with no code": lambda: sqlite3.OperationalError("database is locked"),
    "text that isn't UTF-8, by the module": decode_refusal,
    "text that isn't UTF-8, by the app": lambda: TextNotUtf8(MARK),
}
"""Each failure of the household's store the fallback answers, as it arrives."""

OTHER_FAILURES: dict[str, Callable[[], BaseException]] = {
    "a mistake in SQL": lambda: coded(sqlite3.OperationalError, "SQLITE_ERROR"),
    "a statement left open": lambda: coded(sqlite3.OperationalError, "SQLITE_LOCKED"),
    "a constraint, an extended code": lambda: coded(
        sqlite3.IntegrityError, "SQLITE_CONSTRAINT_UNIQUE"
    ),
    "a misuse": lambda: coded(sqlite3.InterfaceError, "SQLITE_MISUSE"),
    "a binding out of range": lambda: coded(sqlite3.InterfaceError, "SQLITE_RANGE"),
    "a value too big": lambda: coded(sqlite3.DataError, "SQLITE_TOOBIG"),
    "SQLite's own fault": lambda: coded(sqlite3.InternalError, "SQLITE_INTERNAL"),
    "a schema changed": lambda: coded(sqlite3.OperationalError, "SQLITE_SCHEMA"),
    "an interrupt": lambda: coded(sqlite3.OperationalError, "SQLITE_INTERRUPT"),
    "an abort": lambda: coded(sqlite3.OperationalError, "SQLITE_ABORT"),
    "an authorizer's refusal": lambda: coded(sqlite3.DatabaseError, "SQLITE_AUTH"),
    "a closed connection": lambda: sqlite3.ProgrammingError("Cannot operate on a closed database."),
    "a binding of the wrong type": lambda: sqlite3.InterfaceError(MARK),
    "a constraint with no code": lambda: sqlite3.IntegrityError(MARK),
    "a database error with no code": lambda: sqlite3.DatabaseError(MARK),
    "a wait that ran out": TimeoutError,
    "a mistake in the code": lambda: RuntimeError(MARK),
    "a value refused": lambda: ValueError(MARK),
}
"""Failures that are not the household's store failing: each still reaches the server."""

LEFT_TO_THE_SERVER = {
    ("GET", "/"),
    ("POST", "/sign-out"),
    ("POST", "/parent/plans"),
    ("GET", "/parent/plans/runs/{run_id}"),
    ("GET", "/parent/approvals"),
    ("GET", "/parent/approvals/{draft_id}"),
    ("POST", "/parent/approvals/{draft_id}"),
    ("GET", "/parent/checkpoint"),
    ("GET", "/parent/help-requests"),
    ("POST", "/parent/help-requests/{request_id}/accept"),
    ("POST", "/parent/help-requests/{request_id}/update"),
    ("POST", "/parent/help-requests/{request_id}/resolve"),
    ("GET", "/student/workload-signals"),
    ("POST", "/student/workload-signals"),
    ("DELETE", "/student/workload-signals/{signal_id}"),
    ("GET", "/student/help-requests"),
    ("POST", "/student/help-requests"),
    ("DELETE", "/student/help-requests/{request_id}"),
    ("GET", "/student/plans/today"),
    ("POST", "/student/plans"),
    ("GET", "/student/plans/runs/{run_id}"),
    ("GET", "/verifier/claims"),
}
"""The routes not marked for HTML: the JSON routes and the two that only redirect."""

IMPORT_PAGES = {
    ("POST", "/parent/imports"),
    ("POST", "/parent/imports/{draft_id}/images"),
    ("POST", "/parent/imports/{draft_id}/images/{image_id}/remove"),
    ("GET", "/parent/imports/{draft_id}/images/{image_id}"),
    ("GET", "/parent/imports/{draft_id}"),
    ("POST", "/parent/imports/{draft_id}/read"),
    ("POST", "/parent/imports/{draft_id}/review"),
    ("POST", "/parent/imports/{draft_id}/find"),
    ("POST", "/parent/imports/{draft_id}/set-aside"),
    ("POST", "/parent/imports/{draft_id}/finish"),
    ("POST", "/parent/imports/{draft_id}/discard"),
    ("POST", "/parent/imports/{draft_id}/not-hers"),
}
"""The import routes, marked for HTML by their router."""


def with_probes(
    client: TestClient, failure: Callable[[], BaseException], called: list[str] | None = None
) -> TestClient:
    """``client``'s application with this file's routes added, each raising what ``failure``
    makes, and noting its method in ``called`` when given."""
    app = cast("FastAPI", client.app)

    async def raised(request: Request) -> HTMLResponse:
        if called is not None:
            called.append(request.method)
        raise failure()

    def planned(request: Request) -> HTMLResponse:
        student_routes.todays_plan(getattr(request.app.state, STATE_ATTRIBUTE))
        return HTMLResponse("shown")

    for path, methods in (
        (FAMILY_PROBE, ["GET", "HEAD", "POST", "DELETE"]),
        (HER_PROBE, ["GET", "HEAD", "POST"]),
        (IMPORT_PROBE, ["GET", "POST"]),
        ("/parent", ["POST"]),
    ):
        app.add_api_route(path, raised, methods=methods, response_class=HTMLResponse)
    app.add_api_route(CALL_PROBE, raised, methods=["GET", "POST"])
    app.add_api_route(WHERE_PROBE, planned, methods=["GET"], response_class=HTMLResponse)
    return client


def open_client(tmp_path: pathlib.Path) -> TestClient:
    """The fixture week with the sign-in off and screenshot import on."""
    return household_client("open", tmp_path, screenshot_import=True)


def quietly(client: TestClient) -> TestClient:
    """The application ``client`` reaches, a failure nothing catches answered as a server
    answers it, a plain 500, rather than raised into the test."""
    return TestClient(
        client.app, follow_redirects=False, headers=SAME_ORIGIN, raise_server_exceptions=False
    )


def warned(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the application module warned about, in order."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == app_module.__name__ and record.levelno == logging.WARNING
    ]


def kind_of(error: BaseException) -> str:
    """How the warning names a failure: its class, and SQLite's name for its code if it has
    one."""
    name = getattr(error, "sqlite_errorname", None)
    return type(error).__name__ + (f" {name}" if isinstance(name, str) else "")


# ------------------------------------------------------------------ what counts as a failure


@pytest.mark.parametrize("case", list(STORE_FAILURES))
def test_a_failure_of_the_store_is_one_the_fallback_answers(case: str) -> None:
    assert store_refusals.store_failure(STORE_FAILURES[case]()) is True


@pytest.mark.parametrize("case", list(OTHER_FAILURES))
def test_a_failure_that_isnt_the_stores_is_not(case: str) -> None:
    assert store_refusals.store_failure(OTHER_FAILURES[case]()) is False


def test_a_code_counts_by_its_primary_code_whatever_the_class() -> None:
    """An extended code is read by its primary code, and a class SQLite gives a code doesn't
    decide: a damaged file arrives as a ``DatabaseError``, a constraint as an
    ``IntegrityError``."""
    short_read = coded(sqlite3.OperationalError, "SQLITE_IOERR_SHORT_READ")
    damaged = coded(sqlite3.DatabaseError, "SQLITE_CORRUPT")
    unique = coded(sqlite3.IntegrityError, "SQLITE_CONSTRAINT_UNIQUE")

    assert short_read.sqlite_errorcode != sqlite3.SQLITE_IOERR
    assert store_refusals.store_failure(short_read)
    assert not isinstance(damaged, sqlite3.OperationalError)
    assert store_refusals.store_failure(damaged)
    assert unique.sqlite_errorcode & 0xFF == sqlite3.SQLITE_CONSTRAINT
    assert not store_refusals.store_failure(unique)


# ------------------------------------------------------------------------------ the page


@pytest.mark.parametrize("case", list(STORE_FAILURES))
def test_a_get_the_store_fails_is_the_page_that_reads_no_store(
    case: str, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    failure = STORE_FAILURES[case]
    with with_probes(open_client(tmp_path), failure) as client:
        answer = client.get(f"{FAMILY_PROBE}?week=next&shown=", headers=PAGE_HEADERS)

    main = store_free_page(answer, status=503, heading=NOT_SHOWN, alert=f"{NOT_USED} Try again")
    assert 'href="/parent/fallback-probe?week=next&amp;shown="' in main
    assert ways_back_of(main) == [("/parent", "Back to Family review")]
    assert answer.headers.get_list("cache-control") == ["no-store"]
    assert warned(caplog) == [f"{WARNED}GET {FAMILY_PROBE}: {kind_of(failure())}"]
    assert MARK not in caplog.text
    assert MARK not in answer.text


@pytest.mark.parametrize("method", ["POST", "DELETE"])
@pytest.mark.parametrize("case", ["the drafts store's lock held", "a disk error"])
def test_any_other_method_the_store_fails_says_it_cant_tell_whether_it_went_through(
    case: str, method: str, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    failure = STORE_FAILURES[case]
    with with_probes(open_client(tmp_path), failure) as client:
        answer = client.request(method, FAMILY_PROBE, headers=PAGE_HEADERS)

    main = store_free_page(answer, status=500, heading=UNKNOWN_HEADING, alert=UNKNOWN)
    assert "Try again" not in main
    assert ways_back_of(main) == [("/parent", "Back to Family review")]
    assert answer.headers.get_list("cache-control") == ["no-store"]
    assert warned(caplog) == [f"{WARNED}{method} {FAMILY_PROBE}: {kind_of(failure())}"]


def test_a_head_the_store_fails_is_503_with_no_body(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    with with_probes(open_client(tmp_path), StoreBusy) as client:
        answer = client.head(FAMILY_PROBE, headers=PAGE_HEADERS)
        shown = client.get(FAMILY_PROBE, headers=PAGE_HEADERS)

    assert answer.status_code == 503
    assert answer.content == b""
    assert answer.headers.get_list("cache-control") == ["no-store"]
    assert answer.headers["content-type"] == shown.headers["content-type"]
    assert warned(caplog) == [
        f"{WARNED}HEAD {FAMILY_PROBE}: StoreBusy",
        f"{WARNED}GET {FAMILY_PROBE}: StoreBusy",
    ]


def test_the_family_page_itself_is_in_the_familys_tree(tmp_path: pathlib.Path) -> None:
    """A press at ``/parent`` itself goes back to Family review, a GET of the page the press
    was sent from."""
    with with_probes(open_client(tmp_path), StoreBusy) as client:
        answer = client.post("/parent", headers=PAGE_HEADERS)

    main = store_free_page(answer, status=500, heading=UNKNOWN_HEADING, alert=UNKNOWN)
    assert ways_back_of(main) == [("/parent", "Back to Family review")]


@pytest.mark.parametrize(
    ("reader", "back"),
    [
        ("open", "Back to my week"),
        ("her", "Back to my week"),
        ("parent", "Back to her week"),
    ],
)
def test_her_pages_go_back_to_her_week_said_for_whoever_reads(
    reader: str, back: str, tmp_path: pathlib.Path
) -> None:
    with with_probes(household_client(reader, tmp_path), StoreBusy) as client:
        sign_in_as(client, reader)
        shown = client.get(HER_PROBE, headers=PAGE_HEADERS)
        pressed = client.post(HER_PROBE, headers=PAGE_HEADERS)

    main = store_free_page(shown, status=503, heading=NOT_SHOWN, alert=f"{NOT_USED} Try again")
    assert ways_back_of(main) == [("/student/due-this-week", back)]
    main = store_free_page(pressed, status=500, heading=UNKNOWN_HEADING, alert=UNKNOWN)
    assert ways_back_of(main) == [("/student/due-this-week", back)]


def test_an_import_address_the_store_fails_is_the_page_marked_no_store(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    with with_probes(open_client(tmp_path), WriterBusy) as client:
        shown = client.get("/parent/imports/d1/fallback-probe", headers=PAGE_HEADERS)
        pressed = client.post("/parent/imports/d1/fallback-probe", headers=PAGE_HEADERS)

    store_free_page(shown, status=503, heading=NOT_SHOWN, alert=f"{NOT_USED} Try again")
    store_free_page(pressed, status=500, heading=UNKNOWN_HEADING, alert=UNKNOWN)
    assert shown.headers.get_list("cache-control") == ["no-store"]
    assert pressed.headers.get_list("cache-control") == ["no-store"]
    assert warned(caplog) == [
        f"{WARNED}GET {IMPORT_PROBE}: WriterBusy",
        f"{WARNED}POST {IMPORT_PROBE}: WriterBusy",
    ]


# ----------------------------------------------------------------- the sign-in and the marks


@pytest.mark.parametrize("reader", ["her", "parent"])
def test_with_the_sign_in_on_the_page_is_marked_no_store_and_signs_nobody_out(
    reader: str, tmp_path: pathlib.Path
) -> None:
    with with_probes(household_client(reader, tmp_path), StoreBusy) as client:
        sign_in_as(client, reader)
        answers = [
            client.request(method, HER_PROBE, headers=PAGE_HEADERS)
            for method in ("GET", "HEAD", "POST")
        ]
        after = client.get("/student/due-this-week", headers=PAGE_HEADERS)

    for answer in answers:
        assert answer.headers.get_list("cache-control") == ["no-store"]
        assert "set-cookie" not in answer.headers
        assert "location" not in answer.headers
    assert after.status_code == 200


def test_the_sign_in_answers_before_the_fallback(tmp_path: pathlib.Path) -> None:
    """Nobody signed in is sent to sign in, and she is refused a parent's address, both by the
    gate before any route runs: the route that would fail is never reached."""
    called: list[str] = []
    with with_probes(household_client("her", tmp_path), StoreBusy, called) as client:
        unsigned = client.get(FAMILY_PROBE, headers=PAGE_HEADERS)
        sign_in_as(client, "her")
        refused = client.get(FAMILY_PROBE, headers=PAGE_HEADERS)

    assert unsigned.status_code == 303
    assert unsigned.headers["location"].startswith("/sign-in?next=")
    assert refused.status_code == 403
    assert NOT_SHOWN not in refused.text
    assert called == []


# ------------------------------------------------------------------- what it reads and writes


def test_the_page_is_made_with_the_file_and_the_drafts_lock_still_held(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    with HangWatch(capsys) as watch, with_probes(open_client(tmp_path), StoreBusy) as client:
        spy_on_stores(monkeypatch, state_of(client), calls)
        with HeldByAnother(database_of(client)), LockHeldByAnother(state_of(client).drafts):
            shown, _ = watch.press(lambda: client.get(FAMILY_PROBE, headers=PAGE_HEADERS))
            pressed, _ = watch.press(lambda: client.post(FAMILY_PROBE, headers=PAGE_HEADERS))
        read_while_answering = list(calls)

    store_free_page(shown, status=503, heading=NOT_SHOWN, alert=f"{NOT_USED} Try again")
    store_free_page(pressed, status=500, heading=UNKNOWN_HEADING, alert=UNKNOWN)
    assert read_while_answering == []


# ----------------------------------------------------------------- what reaches the server


@pytest.mark.parametrize("case", list(OTHER_FAILURES))
def test_a_failure_that_isnt_the_stores_reaches_the_server_as_itself(
    case: str, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    failure = OTHER_FAILURES[case]()
    with with_probes(open_client(tmp_path), lambda: failure) as client:
        with pytest.raises(type(failure)) as caught:
            client.get(FAMILY_PROBE, headers=PAGE_HEADERS)
        plain = quietly(client).post(FAMILY_PROBE, headers=PAGE_HEADERS)

    assert caught.value is failure
    assert plain.status_code == 500
    assert plain.text == "Internal Server Error"
    assert plain.headers["content-type"].startswith("text/plain")
    assert warned(caplog) == []


@pytest.mark.parametrize("accept", [PAGE_HEADERS, {"Accept": "application/json"}])
@pytest.mark.parametrize("case", ["busy", "the drafts store's lock held", "a disk error"])
def test_a_store_failure_on_a_route_not_marked_for_html_reaches_the_server_as_itself(
    case: str,
    accept: dict[str, str],
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    failure = STORE_FAILURES[case]()
    with with_probes(open_client(tmp_path), lambda: failure) as client:
        with pytest.raises(type(failure)) as caught:
            client.get("/parent/imports/d1/fallback-call", headers=accept)
        plain = quietly(client).post("/parent/imports/d1/fallback-call", headers=accept)

    assert caught.value is failure
    assert plain.status_code == 500
    assert plain.text == "Internal Server Error"
    assert "cache-control" not in plain.headers
    assert warned(caplog) == []


JSON_CALLS = {
    "the family's help requests": ("GET", "/parent/help-requests", "help_requests", "listed"),
    "taking a request up": (
        "POST",
        "/parent/help-requests/h1/accept",
        "help_requests",
        "accept",
    ),
    "her request taken back": (
        "DELETE",
        "/student/help-requests/h1",
        "help_requests",
        "take_back",
    ),
    "today's plan": ("GET", "/student/plans/today", "drafts", "latest_for"),
}
"""JSON routes, each with the store call that fails under it."""


@pytest.mark.parametrize("accept", [PAGE_HEADERS, {"Accept": "application/json"}])
@pytest.mark.parametrize("case", list(JSON_CALLS))
def test_a_json_route_the_store_fails_answers_as_before(
    case: str,
    accept: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    method, path, store, call = JSON_CALLS[case]
    failure = StoreBusy() if store == "drafts" else coded(sqlite3.OperationalError, "SQLITE_BUSY")

    def refused(*_: object, **__: object) -> None:
        raise failure

    with client_for(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat())) as client:
        monkeypatch.setattr(getattr(state_of(client), store), call, refused)
        with pytest.raises(type(failure)) as caught:
            client.request(method, path, headers=accept)
        plain = quietly(client).request(method, path, headers=accept)

    assert caught.value is failure
    assert plain.status_code == 500
    assert plain.text == "Internal Server Error"
    assert plain.headers["content-type"].startswith("text/plain")
    assert "cache-control" not in plain.headers
    assert warned(caplog) == []


# -------------------------------------------------------------------------- the warning


def test_the_warning_names_the_line_in_the_routes_nearest_the_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    failure = coded(sqlite3.OperationalError, "SQLITE_IOERR_SHORT_READ")

    def refused(*_: object, **__: object) -> None:
        raise failure

    source, first = inspect.getsourcelines(student_routes.todays_plan_read)
    line = first + next(n for n, text in enumerate(source) if "state.drafts.latest_for" in text)
    with with_probes(open_client(tmp_path), StoreBusy) as client:
        monkeypatch.setattr(state_of(client).drafts, "latest_for", refused)
        answer = client.get(WHERE_PROBE, headers=PAGE_HEADERS)

    store_free_page(answer, status=503, heading=NOT_SHOWN, alert=f"{NOT_USED} Try again")
    assert warned(caplog) == [
        f"{WARNED}GET {WHERE_PROBE}: OperationalError SQLITE_IOERR_SHORT_READ"
        f" at routes/student.py:{line} in todays_plan_read"
    ]


def test_the_warning_names_no_place_when_no_route_line_is_found(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failure raised outside the routes has no line in them to name; the same failure
    raised again keeps no frame of the way it went the first time, a layer's included."""
    failure = coded(sqlite3.OperationalError, "SQLITE_FULL")
    with with_probes(open_client(tmp_path), lambda: failure) as client:
        with pytest.raises(sqlite3.OperationalError):
            client.post("/parent/imports/d1/fallback-call", headers=PAGE_HEADERS)
        answer = client.post("/parent/imports/d1/fallback-probe", headers=PAGE_HEADERS)

    store_free_page(answer, status=500, heading=UNKNOWN_HEADING, alert=UNKNOWN)
    assert warned(caplog) == [f"{WARNED}POST {IMPORT_PROBE}: OperationalError SQLITE_FULL"]


# --------------------------------------------------------------------- which routes it serves


def api_routes(routes: Iterable[object]) -> list[APIRoute]:
    """Every route of the application's own, in each router it includes."""
    found: list[APIRoute] = []
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            found += api_routes(included.routes)
        elif isinstance(route, APIRoute):
            found.append(route)
    return found


def test_only_the_json_routes_and_the_redirects_are_left_to_the_server() -> None:
    """A closed list: a new route not marked for HTML fails this until it is named here."""
    app = create_app(fixture_settings(**{SCREENSHOT_IMPORT_VARIABLE: "1"}))
    routes = api_routes(app.router.routes)
    left = {
        (method, route.path)
        for route in routes
        if not app_module.answers_a_page(route)
        for method in route.methods or ()
    }
    answered = {
        (method, route.path)
        for route in routes
        if app_module.answers_a_page(route)
        for method in route.methods or ()
    }

    assert left == LEFT_TO_THE_SERVER
    assert answered >= IMPORT_PAGES
    assert ("GET", "/parent") in answered
    assert len(answered) > len(IMPORT_PAGES) + len(LEFT_TO_THE_SERVER)


def test_a_store_failure_with_no_route_reaches_the_server_as_itself() -> None:
    """With no route matched there is no page to answer for."""
    assert app_module.answers_a_page(None) is False


# -------------------------------------------------------------------- the address asked for

ID_PROBE = "/student/fallback-ids/{item:path}"
IDS = {
    "an escaped question mark": ("unit%3Fpart", "unit?part"),
    "an escaped number sign": ("unit%23part", "unit#part"),
    "an escaped slash": ("unit%2F3", "unit/3"),
    "dots between escaped slashes": ("unit%2F..%2F3", "unit/../3"),
    "all of them": ("unit%2F3%20part%3Fb%23c", "unit/3 part?b#c"),
    "a plain id": ("plain-id", "plain-id"),
    "a space": ("a%20b", "a b"),
    "a letter past ASCII": ("caf%C3%A9", "café"),
    "an escaped percent sign": ("100%25", "100%"),
    "an escaped ampersand": ("a%26b", "a&b"),
}
"""Ids as a page's address carries them, and as the route receives them."""
QUERY = "choice=one&choice=two&blank="
"""A query with a repeated field and a blank one."""


def fails_once(asked: list[tuple[str, str]]) -> Callable[..., Awaitable[HTMLResponse]]:
    """A page of the assignment details' shape, noting each id and query it receives. Its first
    call fails as the store fails; later ones answer."""

    async def once(request: Request, item: str) -> HTMLResponse:
        asked.append((item, request.scope["query_string"].decode()))
        if len(asked) == 1:
            raise StoreBusy
        return HTMLResponse("shown")

    return once


def with_id_probe(client: TestClient, asked: list[tuple[str, str]]) -> TestClient:
    """``client``'s application with such a page added."""
    cast("FastAPI", client.app).add_api_route(
        ID_PROBE, fails_once(asked), methods=["GET", "HEAD"], response_class=HTMLResponse
    )
    return client


def without_raw_path(app: ASGIApp) -> ASGIApp:
    """``app`` under a server that gives no ``raw_path``, which ASGI leaves optional."""

    async def served(scope: Scope, receive: Receive, send: Send) -> None:
        await app({key: value for key, value in scope.items() if key != "raw_path"}, receive, send)

    return served


def try_again_of(page: str) -> str:
    """Where the page's Try again link goes, as a browser reads it from the page."""
    found = re.search(r'<a href="([^"]*)">Try again</a>', page)
    assert found is not None, page[:600]
    return html.unescape(found.group(1))


def followed(client: TestClient, sent: str, asked: list[tuple[str, str]]) -> str:
    """Ask for ``sent``, which fails once, then follow the Try again link the answer gives, as a
    browser resolves one, dot segments removed. The link, as the page wrote it."""
    first = client.get(f"/student/fallback-ids/{sent}?{QUERY}", headers=PAGE_HEADERS)
    store_free_page(first, status=503, heading=NOT_SHOWN, alert=f"{NOT_USED} Try again")
    assert len(asked) == 1, "the failed request isn't made again by the application"
    link = try_again_of(first.text)
    resolved = urljoin(f"http://testserver/student/fallback-ids/{sent}?{QUERY}", link)
    again = client.get(resolved, headers=PAGE_HEADERS)
    assert again.status_code == 200, (resolved, again.text[:300])
    return link


@pytest.mark.parametrize("case", list(IDS))
def test_try_again_asks_for_the_same_id_and_query(case: str, tmp_path: pathlib.Path) -> None:
    """The link is the address as it was sent, so what an id escapes stays part of the id."""
    sent, item = IDS[case]
    asked: list[tuple[str, str]] = []
    with with_id_probe(open_client(tmp_path), asked) as client:
        link = followed(client, sent, asked)

    assert link == f"/student/fallback-ids/{sent}?{QUERY}"
    assert asked == [(item, QUERY), (item, QUERY)]


@pytest.mark.parametrize("case", list(IDS))
def test_try_again_is_made_from_the_route_where_the_server_gives_no_raw_path(
    case: str, tmp_path: pathlib.Path
) -> None:
    """The decoded path alone has lost which slashes were the id's, so the matched route and its
    values make the address."""
    sent, item = IDS[case]
    asked: list[tuple[str, str]] = []
    probed = with_id_probe(open_client(tmp_path), asked)
    with TestClient(
        without_raw_path(probed.app), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        link = followed(client, sent, asked)

    assert link == f"/student/fallback-ids/{sent}?{QUERY}"
    assert asked == [(item, QUERY), (item, QUERY)]


def test_a_page_in_a_router_makes_its_whole_address_where_the_server_gives_no_raw_path(
    tmp_path: pathlib.Path,
) -> None:
    """The application's pages sit in routers with a prefix, and the route holds it."""
    asked: list[tuple[str, str]] = []
    router = APIRouter(prefix="/student/fallback-router-ids")
    router.add_api_route(
        "/{item:path}", fails_once(asked), methods=["GET"], response_class=HTMLResponse
    )
    application = open_client(tmp_path).app
    cast("FastAPI", application).include_router(router)
    with TestClient(
        without_raw_path(application), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        answer = client.get("/student/fallback-router-ids/unit%2F3", headers=PAGE_HEADERS)

    assert answer.status_code == 503
    assert try_again_of(answer.text) == "/student/fallback-router-ids/unit%2F3"
    assert asked == [("unit/3", "")]


def test_a_path_as_sent_keeps_its_escapes_and_escapes_what_an_address_cant_hold() -> None:
    assert navigation.asked_path(b"/a%2Fb%3Fc%23d/e-f_g.h~i") == "/a%2Fb%3Fc%23d/e-f_g.h~i"
    assert navigation.asked_path(b"/a!$&'()*+,;=:@b") == "/a!$&'()*+,;=:@b"
    assert navigation.asked_path(b"/a b") == "/a%20b"
    assert navigation.asked_path(b"/caf\xc3\xa9") == "/caf%C3%A9"
    assert navigation.asked_path(b"/a\\b") == "/a%5Cb"
    assert navigation.asked_path(b'/a"b<c>d') == "/a%22b%3Cc%3Ed"
    assert navigation.asked_path(b"/a?b#c") == "/a%3Fb%23c"


def test_a_head_on_such_an_address_is_503_with_no_body_marked_no_store(
    tmp_path: pathlib.Path,
) -> None:
    asked: list[tuple[str, str]] = []
    with with_id_probe(open_client(tmp_path), asked) as client:
        answer = client.head("/student/fallback-ids/unit%2F3", headers=PAGE_HEADERS)

    assert answer.status_code == 503
    assert answer.content == b""
    assert answer.headers.get_list("cache-control") == ["no-store"]
    assert asked == [("unit/3", "")]


def test_a_get_of_the_way_back_itself_offers_only_try_again(tmp_path: pathlib.Path) -> None:
    """Family review failing as a page: the way back would be the address Try again goes to."""

    async def raised() -> HTMLResponse:
        raise StoreBusy

    client = open_client(tmp_path)
    routes = cast("FastAPI", client.app).router.routes
    routes.insert(0, APIRoute("/parent", raised, methods=["GET"], response_class=HTMLResponse))
    with client:
        answer = client.get("/parent", headers=PAGE_HEADERS)

    main = store_free_page(answer, status=503, heading=NOT_SHOWN, alert=f"{NOT_USED} Try again")
    assert ways_back_of(main) == []
    assert try_again_of(main) == "/parent"
