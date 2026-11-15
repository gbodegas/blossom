# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Every answer the application makes through the household gate, but a static file, carries
``Cache-Control: no-store``, with the sign-in on or off.

The mark is a consistent HTTP cache header. It asks that the answer not be stored. It does not
make a browser ask again on Back or Forward, remove a copy a browser already holds, or keep text
typed and not sent.
"""

import dataclasses
import pathlib
import re
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.routes import student as student_routes
from tests.support import (
    ESSAY_ID,
    FIXTURE_WEEK,
    HER_PAGE,
    HERS,
    PAGE_HEADERS,
    PLAN_DATE,
    SAME_ORIGIN,
    card_for,
    client_for,
    every_route,
    fixture_settings,
    refusing,
    report,
    save,
    signed_in,
    signed_in_household,
)

MODES = ("the sign-in off", "the sign-in on")
DETAILS = f"/student/assignments/{ESSAY_ID}"
IMPORTS = "/parent/imports"


@contextmanager
def her_pages(mode: str, tmp_path: pathlib.Path) -> Iterator[TestClient]:
    """Her pages on the pinned day: with the sign-in off, or with it on and her signed in."""
    if mode == "the sign-in off":
        settings = fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat())
    else:
        settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        if mode == "the sign-in on":
            signed_in(client, HERS)
        yield client


@pytest.mark.parametrize("mode", MODES)
def test_a_page_two_calls_a_redirect_and_a_refusal_are_marked_and_a_static_file_is_not(
    mode: str, tmp_path: pathlib.Path
) -> None:
    """One answer of each kind the gate lets through: a page that reads her week, a JSON
    call that succeeds and one that is refused, a redirect and a page that refuses an
    assignment that isn't on record. Each is marked, with the sign-in on or off. A static
    file is marked in neither."""
    with her_pages(mode, tmp_path) as client:
        page = client.get(HER_PAGE, headers=PAGE_HEADERS)
        call = client.post("/student/help-requests", json={"note": "A synthetic question"})
        no_plan = client.get("/student/plans/today")
        redirect = client.get("/", headers=PAGE_HEADERS)
        refusal = client.get("/student/assignments/assignment-not-here", headers=PAGE_HEADERS)
        stylesheet = client.get("/static/blossom.css")

    assert page.status_code == 200
    assert call.status_code == 201
    assert no_plan.status_code == 404
    for json_answer in (call, no_plan):
        assert json_answer.headers["content-type"].startswith("application/json")
    assert redirect.status_code in (302, 303, 307)
    assert refusal.status_code == 404
    assert stylesheet.status_code == 200
    for answer in (page, call, no_plan, redirect, refusal):
        assert answer.headers["cache-control"] == "no-store"
    assert "cache-control" not in stylesheet.headers


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("landing", ["no landing", "a Done landing"])
def test_her_week_that_can_t_be_read_is_marked_as_her_week_is(
    mode: str, landing: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Her week marks itself after a Done; the page that stands in for it when the record
    can't be read did not, so with the sign-in off it carried no mark. Asked for plainly, and
    at the address a save that made a card Done lands on."""
    with her_pages(mode, tmp_path) as client:
        address = HER_PAGE
        if landing == "a Done landing":
            opened = client.get(
                HER_PAGE, params={"week": FIXTURE_WEEK, "change": ESSAY_ID}, headers=PAGE_HEADERS
            )
            address = save(client, card_for(opened.text, ESSAY_ID), "done").headers["location"]
        monkeypatch.setattr(student_routes, "student_page", refusing())
        answer = client.get(address, headers=PAGE_HEADERS)

    assert ("landing=" in address) == (landing == "a Done landing")
    assert answer.status_code == 503
    assert answer.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("landing", ["no landing", "a Done landing"])
def test_the_details_that_can_t_be_read_are_marked_as_the_details_are(
    mode: str, landing: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same for an assignment's details: asked for plainly, and at the address a save
    from the details that made the assignment Done lands on."""
    with her_pages(mode, tmp_path) as client:
        address = DETAILS
        if landing == "a Done landing":
            address = report(client, ESSAY_ID, "done", week="", report_view="detail")
        monkeypatch.setattr(student_routes, "detail_page", refusing())
        answer = client.get(address, headers=PAGE_HEADERS)

    assert address.startswith(DETAILS)
    assert ("landing=" in address) == (landing == "a Done landing")
    assert answer.status_code == 503
    assert answer.headers["cache-control"] == "no-store"


def test_every_route_in_the_table_answers_with_the_mark_with_the_sign_in_off() -> None:
    """Each route in the application's own table, screenshot import's routes among them,
    is asked once, with the sign-in off, a synthetic id in each path parameter and nothing
    in the body, and whatever answers is marked. No route is skipped but the static files'
    mount.

    This shows the mark doesn't depend on which route answers. It is not a test of each
    route's successful answer: most of these requests are refused for a missing form or an
    id that isn't on record. The pages named in the contract are asked for by name above and
    in the modules that reach them."""
    settings = dataclasses.replace(
        fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()), screenshot_import=True
    )
    asked: list[tuple[str, str]] = []
    unmarked: list[tuple[str, str, int]] = []
    left_out: list[str] = []
    with TestClient(
        create_app(settings),
        follow_redirects=False,
        headers=SAME_ORIGIN,
        raise_server_exceptions=False,
    ) as client:
        for route in every_route(client.app.routes):  # type: ignore[attr-defined]
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path is None or methods is None:
                left_out.append(str(getattr(route, "name", route)))
                continue
            address = re.sub(r"\{[^}]+\}", "nothing-here", path)
            for method in sorted(methods - {"HEAD"}):
                answer = client.request(method, address, headers=PAGE_HEADERS)
                asked.append((method, path))
                if answer.headers.get("cache-control") != "no-store":
                    unmarked.append((method, path, answer.status_code))

    assert left_out == ["static"]
    assert len(asked) == len(set(asked))
    assert len(asked) > 100
    assert any(path.startswith(IMPORTS) for _, path in asked)
    assert unmarked == []
