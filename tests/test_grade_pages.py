# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The grade pages: adding a report by pasting it, its review, its save, and the outcome.

Every family grade route answers a signed-in parent with sign-in on, and, with sign-in off, only
the computer running Blossom, named as itself: a loopback client and a loopback Host. It refuses
everyone else before it reads a form or opens the store. A review writes nothing, and the paste
travels only in a form's body.
"""

import ast
import inspect
import logging
import os
import pathlib
import re
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from html import unescape
from typing import Final
from zoneinfo import ZoneInfo

import pytest
from fastapi.params import Form as FormParameter
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from markupsafe import escape
from starlette.requests import Request

from blossom import anthropic_client
from blossom.agent import graph as agent_graph
from blossom.app import create_app
from blossom.clock import FrozenClock
from blossom.grades import draft as grade_drafts
from blossom.grades import review as grade_review
from blossom.grades.draft import (
    GradeNumber,
    GradeReportDraft,
    GradeValue,
    Presence,
    TermResult,
    capture_key,
)
from blossom.grades.identity import name_form, name_form_key
from blossom.grades.projection import MadeCurrent
from blossom.grades.review import Cell, CurrentValue, GradeReportSaved, ItemStatus, ReviewItem
from blossom.grades.text_reader import read_grade_report
from blossom.household import secret_beside
from blossom.intake import TEXT_MAX_LENGTH
from blossom.routes import grades as grade_routes
from blossom.routes.forms import FormRoute, fields_of
from blossom.settings import Settings
from blossom.stores import gradebook
from blossom.stores.gradebook import (
    VIEW_TABLES,
    ClassReport,
    GradeReportNotSaved,
    GradeTransactionLost,
)
from blossom.stores.paths import SECRET_NAME, UnsafeCheckpointPath
from tests.support import (
    FIXTURE_TIMEZONE,
    FIXTURES,
    HERS,
    PLAN_DATE,
    THEIRS,
    Chain,
    as_a_browser_sends,
    as_stored,
    capture_class,
    closed_world,
    confirm_current,
    elements_of,
    every_route,
    files_in,
    fixture_settings,
    form_values,
    grade_answers,
    rules_reaching,
    save_grade,
    signed_in,
    signed_in_household,
    store_of,
    whole_form,
    without_columns,
    words,
)

REPORT = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")
"""Wren's synthetic report: Biology, 2026-2027, T1."""
NO_LINE = REPORT.replace("**Bramble, Wren**", "")
PAGE = {"Accept": "text/html"}
ADD = "/parent/grades/add"
REVIEW = "/parent/grades/add/review"
EDIT = "/parent/grades/add/edit"
CHECK = "/parent/grades/add/check"
SAVE = "/parent/grades/add/save"
SAVED = "/parent/grades/saved/{acceptance_id}"
GRADES = "/parent/grades"
HER_GRADES = "/student/grades"
VIEW = "/parent/grades/view"
HER_VIEW = "/student/grades/view"
CURRENT_TERM = "/parent/grades/current-term"
CLASS_AT = "/parent/grades/classes/{class_id}/terms/{n}"
HER_CLASS_AT = "/student/grades/classes/{class_id}/terms/{n}"
UNKNOWN = "acceptance-" + "0" * 32
FAMILY_GRADE_ROUTES = {
    ("GET", ADD),
    ("POST", REVIEW),
    ("POST", EDIT),
    ("POST", CHECK),
    ("POST", SAVE),
    ("GET", SAVED),
    ("GET", GRADES),
    ("POST", VIEW),
    ("POST", CURRENT_TERM),
    ("GET", CLASS_AT),
}
"""Every family grade route and method, named here so a route added without a row fails."""
HER_GRADE_ROUTES = {("GET", HER_GRADES), ("POST", HER_VIEW), ("GET", HER_CLASS_AT)}
"""Every grade route of her pages, which any viewer may open."""
LOOPBACK = "127.0.0.1:8781"


def open_household(tmp_path: pathlib.Path) -> Settings:
    """The pinned day with sign-in off, its files under ``tmp_path``."""
    return fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **files_in(tmp_path))


def database(settings: Settings) -> pathlib.Path:
    return pathlib.Path(settings.database_path)


@contextmanager
def at(
    settings: Settings, *, client: str = "127.0.0.1", host: str = LOOPBACK
) -> Iterator[TestClient]:
    """The app as a browser at ``client`` reaches it under the name ``host``, its Origin the
    same name, so the same-origin check passes and only the grade rule decides."""
    with TestClient(
        create_app(settings),
        follow_redirects=False,
        headers={"Host": host, "Origin": f"http://{host}"},
        client=(client, 50000),
    ) as browser:
        yield browser


def every_family_request(browser: TestClient, **headers: str) -> dict[tuple[str, str], int]:
    """Each family grade route pressed once, as a page would press it: the paste in each form,
    the review's own form checked and saved where the review gave one, and the outcome of that
    save, or of an ID on record nowhere."""
    asked = {**PAGE, **headers}
    statuses = {}
    sent: dict[str, str] = {"report_text": REPORT}
    for path in (ADD,):
        statuses[("GET", path)] = browser.get(path, headers=asked).status_code
    review = browser.post(REVIEW, data={"report_text": REPORT}, headers=asked)
    statuses[("POST", REVIEW)] = review.status_code
    if review.status_code == 200:
        sent = {**sent_from(review.text), "identity": "hers"}
    statuses[("POST", EDIT)] = browser.post(
        EDIT, data={"report_text": REPORT}, headers=asked
    ).status_code
    statuses[("POST", CHECK)] = browser.post(CHECK, data=sent, headers=asked).status_code
    saved = browser.post(SAVE, data=sent, headers=asked)
    statuses[("POST", SAVE)] = saved.status_code
    outcome = saved.headers.get("location", SAVED.format(acceptance_id=UNKNOWN))
    statuses[("GET", SAVED)] = browser.get(outcome, headers=asked).status_code
    grades = browser.get(GRADES, headers=asked)
    statuses[("GET", GRADES)] = grades.status_code
    term = {"view": "2026-2027 T1"}
    statuses[("POST", VIEW)] = browser.post(VIEW, data=term, headers=asked).status_code
    same = {"shown": "2026-2027 T1", "term": "2026-2027 T1"}
    statuses[("POST", CURRENT_TERM)] = browser.post(
        CURRENT_TERM, data=same, headers=asked
    ).status_code
    linked = re.search(r'href="(/parent/grades/classes/[^"]+/terms/1)"', grades.text)
    details = linked[1] if linked else CLASS_AT.format(class_id="class-unknown", n=1)
    statuses[("GET", CLASS_AT)] = browser.get(details, headers=asked).status_code
    return statuses


ALLOWED = {
    ("GET", ADD): 200,
    ("POST", REVIEW): 200,
    ("POST", EDIT): 200,
    ("POST", CHECK): 200,
    ("POST", SAVE): 303,
    ("GET", SAVED): 200,
    ("GET", GRADES): 200,
    ("POST", VIEW): 303,
    ("POST", CURRENT_TERM): 303,
    ("GET", CLASS_AT): 200,
}
"""What each route answers the household or a parent pressing it as a page would."""


def sent_from(page: str) -> dict[str, str]:
    """What the review's form sends with nothing changed, its text as a browser reads the
    textarea: the one line break after the opening tag dropped."""
    fields = whole_form(page, SAVE)
    fields["report_text"] = fields["report_text"].removeprefix("\n")
    return fields


# ------------------------------------------------------------- who may use what


def test_the_table_names_every_family_grade_route(tmp_path: pathlib.Path) -> None:
    app = create_app(open_household(tmp_path))
    served = {
        (method, route.path)
        for route in every_route(app.routes)
        if isinstance(route, APIRoute) and route.path.startswith((GRADES, HER_GRADES))
        for method in route.methods or ()
    }

    assert served == FAMILY_GRADE_ROUTES | HER_GRADE_ROUTES


def test_no_grade_route_reads_a_form_before_its_dependencies(tmp_path: pathlib.Path) -> None:
    app = create_app(open_household(tmp_path))
    grade_routes_served = [
        route
        for route in every_route(app.routes)
        if isinstance(route, APIRoute) and route.endpoint.__module__ == grade_routes.__name__
    ]

    assert len(grade_routes_served) == len(FAMILY_GRADE_ROUTES | HER_GRADE_ROUTES)
    for route in grade_routes_served:
        assert not isinstance(route, FormRoute), route.path
        for parameter in inspect.signature(route.endpoint).parameters.values():
            marks = [parameter.default, *getattr(parameter.annotation, "__metadata__", ())]
            assert not any(isinstance(mark, FormParameter) for mark in marks), route.path


def test_with_sign_in_on_her_sign_in_is_refused_and_nothing_is_written(
    tmp_path: pathlib.Path,
) -> None:
    settings = signed_in_household(tmp_path)
    with at(settings, client="192.0.2.10", host="testserver") as browser:
        signed_in(browser, HERS)
        before = closed_world([database(settings)], leaving_out=())
        statuses = every_family_request(browser)
        after = closed_world([database(settings)], leaving_out=())

    assert set(statuses.values()) == {403}
    assert after == before


def test_with_sign_in_on_no_one_signed_in_is_sent_to_sign_in(tmp_path: pathlib.Path) -> None:
    with at(signed_in_household(tmp_path), host="testserver") as browser:
        page = browser.get(ADD, headers=PAGE)
        pressed = browser.post(REVIEW, data={"report_text": REPORT}, headers=PAGE)
        called = browser.post(REVIEW, data={"report_text": REPORT})

    assert (page.status_code, page.headers["location"]) == (
        303,
        "/sign-in?next=%2Fparent%2Fgrades%2Fadd",
    )
    assert (pressed.status_code, pressed.headers["location"]) == (303, "/sign-in")
    assert called.status_code == 401


def test_with_sign_in_on_a_parent_off_the_server_computer_may_add_a_report(
    tmp_path: pathlib.Path,
) -> None:
    with at(signed_in_household(tmp_path), client="192.0.2.10", host="testserver") as browser:
        signed_in(browser, THEIRS)
        statuses = every_family_request(browser)

    assert statuses == ALLOWED


@pytest.mark.parametrize("client", ["192.0.2.10", "testclient", "10.0.0.2", "::2"])
def test_with_sign_in_off_a_device_off_the_server_computer_is_refused(
    client: str, tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    with at(settings, client=client) as browser:
        before = closed_world([database(settings)], leaving_out=())
        statuses = every_family_request(browser)
        refused = browser.get(ADD, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert set(statuses.values()) == {403}
    assert escape(grade_routes.ONLY_HERE) in refused.text
    assert 'name="report_text"' not in refused.text
    assert after == before
    assert not (tmp_path / SECRET_NAME).exists()


@pytest.mark.parametrize("host", ["blossom.example", "192.0.2.10:8781", "127.0.0.1.example"])
def test_with_sign_in_off_a_name_other_than_this_computer_s_is_refused(
    host: str, tmp_path: pathlib.Path
) -> None:
    with at(open_household(tmp_path), host=host) as browser:
        statuses = every_family_request(browser)

    assert set(statuses.values()) == {403}
    assert not (tmp_path / SECRET_NAME).exists()


@pytest.mark.parametrize(
    ("client", "host"),
    [
        ("127.0.0.1", LOOPBACK),
        ("127.0.0.1", "localhost:8781"),
        ("::1", "[::1]:8781"),
        ("::ffff:127.0.0.1", LOOPBACK),
        ("127.0.0.2", "localhost"),
    ],
)
def test_with_sign_in_off_the_server_computer_may_add_a_report(
    client: str, host: str, tmp_path: pathlib.Path
) -> None:
    with at(open_household(tmp_path), client=client, host=host) as browser:
        statuses = every_family_request(browser)

    assert statuses == ALLOWED


def a_request(scope_client: tuple[str, int] | None, hosts: list[bytes]) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": ADD,
        "headers": [(b"host", host) for host in hosts],
        "client": scope_client,
        "scheme": "http",
        "query_string": b"",
    }
    return Request(scope)


@pytest.mark.parametrize(
    ("scope_client", "hosts", "here"),
    [
        (None, [b"127.0.0.1:8781"], False),
        (("127.0.0.1", 1), [], False),
        (("127.0.0.1", 1), [b"127.0.0.1:8781", b"127.0.0.1:8781"], False),
        (("127.0.0.1", 1), [b"[::1"], False),
        (("127.0.0.1", 1), [b"127.0.0.1:8781"], True),
    ],
)
def test_a_missing_client_or_a_host_not_named_once_is_not_this_computer(
    scope_client: tuple[str, int] | None, hosts: list[bytes], here: bool
) -> None:
    assert grade_routes.from_this_computer(a_request(scope_client, hosts)) is here


def test_an_unreadable_body_off_the_server_computer_is_refused_unread(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path), client="192.0.2.10") as browser:
        answer = browser.post(
            REVIEW,
            content=b"\xff\xfe not a form",
            headers={**PAGE, "Content-Type": "multipart/form-data; boundary=x"},
        )

    assert answer.status_code == 403
    assert escape(grade_routes.ONLY_HERE) in answer.text


# ------------------------------------------------------------- the household secret


def test_with_sign_in_off_the_paste_page_makes_no_secret_and_the_first_review_does(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        page = browser.get(ADD, headers=PAGE)
        made_before_review = (tmp_path / SECRET_NAME).exists()
        first = browser.post(REVIEW, data={"report_text": REPORT}, headers=PAGE)
        key = browser.app.state.grade_name_key  # type: ignore[attr-defined]
        second = browser.post(REVIEW, data={"report_text": REPORT}, headers=PAGE)
        key_again = browser.app.state.grade_name_key  # type: ignore[attr-defined]

    secret = (tmp_path / SECRET_NAME).read_bytes()
    assert (page.status_code, made_before_review) == (200, False)
    assert (first.status_code, second.status_code) == (200, 200)
    assert key == key_again == name_form_key(secret)
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / SECRET_NAME).stat().st_mode) == 0o600


def test_with_sign_in_on_both_keys_come_from_one_read_of_the_secret(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[pathlib.Path] = []

    def counted(path: pathlib.Path) -> bytes:
        reads.append(path)
        return secret_beside(path)

    monkeypatch.setattr("blossom.dependencies.secret_beside", counted)
    with at(signed_in_household(tmp_path), host="testserver") as browser:
        signed_in(browser, THEIRS)
        reviewed = browser.post(REVIEW, data={"report_text": REPORT}, headers=PAGE)
        key = browser.app.state.grade_name_key  # type: ignore[attr-defined]

    assert reviewed.status_code == 200
    assert len(reads) == 1
    assert key == name_form_key((tmp_path / SECRET_NAME).read_bytes())


def damaged(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / SECRET_NAME).write_bytes(b"not a whole secret")


def unreadable(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(path: pathlib.Path) -> bytes:
        raise PermissionError(path)

    monkeypatch.setattr(grade_routes, "secret_beside", refuse)


def on_a_share(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(path: pathlib.Path) -> bytes:
        raise UnsafeCheckpointPath(str(path))

    monkeypatch.setattr(grade_routes, "secret_beside", refuse)


@pytest.mark.parametrize("trouble", [damaged, unreadable, on_a_share])
def test_a_secret_that_cant_be_read_keeps_the_text_and_the_paste_page_working(
    trouble: object, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = open_household(tmp_path)
    trouble(tmp_path, monkeypatch)  # type: ignore[operator]
    with at(settings) as browser:
        before = closed_world([database(settings)], leaving_out=())
        reviewed = browser.post(REVIEW, data={"report_text": REPORT}, headers=PAGE)
        page = browser.get(ADD, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert reviewed.status_code == 500
    assert escape(grade_routes.NO_NAME_CHECK) in reviewed.text
    assert "Bramble, Wren" in reviewed.text.split("<textarea", 1)[1]
    assert page.status_code == 200
    assert after == before


# ------------------------------------------------------------- the paste page


def test_the_paste_page_posts_its_text_to_the_review(tmp_path: pathlib.Path) -> None:
    with at(open_household(tmp_path)) as browser:
        page = browser.get(ADD, headers=PAGE).text

    assert f'action="{REVIEW}"' in page
    assert 'method="post"' in page
    assert '<textarea name="report_text" id="report-text"' in page
    assert escape(grade_routes.PASTE_HINT) in page


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("   \n  ", "NOTHING_PASTED"),
        ("x" * (TEXT_MAX_LENGTH + 1), "TOO_LONG"),
        ("Bramble, Wren\nsome notes, not a report", "NOT_A_REPORT"),
        (REPORT + "\n" + REPORT, "SEVERAL_REPORTS"),
    ],
    ids=["blank", "too-long", "not-a-report", "several-reports"],
)
def test_a_paste_that_gives_no_review_keeps_the_text_and_writes_nothing(
    text: str, said: str, tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(REVIEW, data={"report_text": text}, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 422
    assert escape(getattr(grade_routes, said)) in answer.text
    assert f'action="{REVIEW}"' in answer.text
    if text.strip():
        assert escape(text.splitlines()[0][:40]) in answer.text
    assert after == before
    assert not (tmp_path / SECRET_NAME).exists()


def test_a_report_that_came_through_email_isn_t_read_and_its_text_is_kept_exactly(
    tmp_path: pathlib.Path,
) -> None:
    text = (FIXTURES / "grade_email" / "plain-text-body.txt").read_bytes().decode("utf-8")
    sent = as_a_browser_sends({"report_text": text})["report_text"]
    settings = open_household(tmp_path)
    with at(settings) as browser:
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(REVIEW, data={"report_text": sent}, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    textarea = answer.text.split('id="report-text"', 1)[1].split(">", 1)[1]
    assert answer.status_code == 422
    assert problem_said(answer.text) == (
        "Blossom couldn't find a grade report in this text. Nothing was saved. Copy the report "
        "again from the school's gradebook. Your text is kept."
    )
    assert unescape(textarea.split("</textarea>", 1)[0]) == "\n" + sent
    assert after == before


def test_edit_this_text_returns_the_paste_page_holding_the_text(tmp_path: pathlib.Path) -> None:
    with at(open_household(tmp_path)) as browser:
        answer = browser.post(EDIT, data={"report_text": "\n" + REPORT}, headers=PAGE)

    assert answer.status_code == 200
    textarea = answer.text.split('<textarea name="report_text" id="report-text"', 1)[1]
    assert textarea.split(">", 1)[1].startswith("\n\n|   |")
    assert f'action="{REVIEW}"' in answer.text


# ------------------------------------------------------------- the review


def reviewed(tmp_path: pathlib.Path, text: str = REPORT) -> str:
    with at(open_household(tmp_path)) as browser:
        answer = browser.post(REVIEW, data={"report_text": text}, headers=PAGE)
    assert answer.status_code == 200, answer.text
    return answer.text


def edit_form(page: str) -> str:
    return page.split(f'action="{EDIT}"', 1)[1].split("</form>", 1)[0]


def test_the_review_writes_nothing_but_the_secret(tmp_path: pathlib.Path) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(REVIEW, data={"report_text": REPORT}, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 200
    assert after == before


def test_the_review_shows_the_line_as_read_and_preselects_no_answer(
    tmp_path: pathlib.Path,
) -> None:
    page = reviewed(tmp_path)
    about = page.split('id="about-this-report"', 1)[1].split("</fieldset>", 1)[0]

    assert "Bramble, Wren" in about
    for answer in ("hers", "misread", "not_hers"):
        assert f'name="identity" value="{answer}"' in about
    assert "checked" not in about


def test_a_report_with_no_student_line_offers_not_hers_and_preselects_nothing(
    tmp_path: pathlib.Path,
) -> None:
    page = reviewed(tmp_path, NO_LINE)
    about = page.split('id="about-this-report"', 1)[1].split("</fieldset>", 1)[0]

    assert escape(grade_routes.NO_STUDENT_LINE) in about
    assert 'name="identity" value="confirmed"' in about
    assert 'name="identity" value="not_hers"' in about
    assert 'value="hers"' not in about
    assert "checked" not in about


def test_edit_this_text_is_its_own_form_holding_one_copy_of_the_text(
    tmp_path: pathlib.Path,
) -> None:
    page = reviewed(tmp_path)
    form = edit_form(page)

    assert form.count('name="') == 1
    assert 'name="report_text"' in form
    assert "Edit this text" in form


def test_the_review_carries_the_text_whole_with_its_leading_line_break(
    tmp_path: pathlib.Path,
) -> None:
    page = reviewed(tmp_path, "\n" + REPORT)
    hidden = page.split('<textarea name="report_text" hidden', 1)[1].split(">", 1)[1]

    assert hidden.startswith("\n\n|   |")


def test_each_item_is_named_by_its_position(tmp_path: pathlib.Path) -> None:
    page = reviewed(tmp_path)

    names = set(re.findall(r'name="([^"]*)"', page.split("<main", 1)[1]))

    assert {f"select.{i}" for i in range(9)} <= names
    assert "select.9" not in names
    assert "Seed Germination Log" in page
    assert names <= positions_on(page).names, sorted(names)


def test_the_review_asks_the_setup_month_and_class_with_the_report_s_labels(
    tmp_path: pathlib.Path,
) -> None:
    page = reviewed(tmp_path)

    assert 'name="setup" value="report"' in page
    assert 'name="setup_term" maxlength="20"' in page
    assert 'name="first_month"' in page
    assert 'name="class_name" maxlength="60"' in page
    assert 'value="Biology"' in page
    assert 'name="class" value="new"' in page


def problem_said(page: str) -> str:
    """The words of the page's problem line."""
    found = re.search(r'<p class="problem"[^>]*>(.*?)</p>', page, re.DOTALL)
    assert found is not None
    return words(found[1])


def test_a_paste_over_the_size_limit_names_the_limit_and_keeps_the_text(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        answer = browser.post(
            REVIEW, data={"report_text": "x" * (TEXT_MAX_LENGTH + 1)}, headers=PAGE
        )

    assert answer.status_code == 422
    assert problem_said(answer.text) == (
        "This text exceeds the size limit. Nothing was saved. Paste one class's report at a "
        "time. Your text is kept."
    )


def test_a_secret_that_cant_be_read_says_hers_couldn_t_be_checked_at_the_review_and_the_save(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unchecked = (
        "Blossom couldn't check that this report is hers. Nothing was saved. See the household "
        "guide before trying again."
    )
    with at(open_household(tmp_path)) as browser:
        form = answered(review_page(browser))
        del browser.app.state.grade_name_key  # type: ignore[attr-defined]
        unreadable(tmp_path, monkeypatch)
        reviewed = browser.post(REVIEW, data={"report_text": REPORT}, headers=PAGE)
        saved = browser.post(SAVE, data=form, headers=PAGE)

    assert (reviewed.status_code, saved.status_code) == (500, 500)
    assert problem_said(reviewed.text) == f"{unchecked} Your text is kept."
    assert problem_said(saved.text) == f"{unchecked} Your text and answers are kept."


def test_a_report_with_no_student_line_says_none_was_found_in_this_copy(
    tmp_path: pathlib.Path,
) -> None:
    about = reviewed(tmp_path, NO_LINE).split('id="about-this-report"', 1)[1]

    assert "<legend>No student name was found in this copy.</legend>" in about


def test_the_name_was_misread_says_this_spelling_isn_t_remembered_and_promises_nothing(
    tmp_path: pathlib.Path,
) -> None:
    about = words(reviewed(tmp_path).split('id="about-this-report"', 1)[1].split("</fieldset>")[0])

    assert "The name was misread Blossom won't remember this spelling." in about
    assert "next report" not in about


def test_a_changed_text_keeps_the_text_and_asks_for_the_answers_again(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        form = answered(review_page(browser))
        changed = form["report_text"].replace("| 7.0 ", "| 8.0 ")
        answer = browser.post(SAVE, data={**form, "report_text": changed}, headers=PAGE)

    assert answer.status_code == 409
    assert problem_said(answer.text) == (
        "The report text changed. Nothing was saved. Review the text again. Your text is kept. "
        "Check the answers again."
    )


def test_each_cell_says_what_the_copy_held_under_its_own_field_s_name(
    tmp_path: pathlib.Path,
) -> None:
    text = (
        REPORT.replace(
            "| Seed Germination Log | 18.0    | 20.0    | 90.0    | Valid      | 09/22   |",
            "| Seed Germination Log |         | EX      | 90.0    |            | EX      |",
        )
        .replace("| Cell Diagram             | 7.0     |", "| Cell Diagram             | EX      |")
        .replace("**80.0**", "**EX**", 1)
        .replace("| **81.9** | **B-** |", "| **EX** |        |")
    )
    said = words(re.sub(r"</?span[^>]*>", "", reviewed(tmp_path, text)))

    assert (
        "Term grade on this report: percent couldn't be read: EX · letter grade left blank"
    ) in said
    assert "Homework / Practice: weight 15.0, average couldn't be read: EX" in said
    assert "Quizzes: weight 20.0, average left blank" in said
    assert (
        "Seed Germination Log School score: blank / maximum couldn't be read: EX · Status left "
        "blank · Due date shown: couldn't be read: EX"
    ) in said
    assert (
        "Cell Diagram School score: couldn't be read: EX / 10.0 · Missing · Due date shown: 09/26"
    ) in said


@pytest.mark.parametrize(
    ("value", "label", "said"),
    [
        (GradeNumber.read("0.0"), None, "0.0"),
        (GradeNumber.read(""), None, "blank"),
        (GradeNumber.read("EX"), None, "couldn't be read: EX"),
        (GradeNumber.not_captured(), None, "not in the copy"),
        (GradeNumber.read("20.0"), "maximum", "20.0"),
        (GradeNumber.read(""), "maximum", "maximum left blank"),
        (GradeNumber.read("EX"), "maximum", "maximum couldn't be read: EX"),
        (GradeNumber.not_captured(), "maximum", "maximum not in the copy"),
        (GradeValue.not_captured(), "Assignment", "Assignment not in the copy"),
    ],
)
def test_a_cell_is_described_short_after_its_label_and_whole_where_it_stands_alone(
    value: GradeValue, label: str | None, said: str
) -> None:
    assert grade_routes.cell(value, label) == said


def test_a_category_s_weight_and_average_name_themselves_reported_or_not() -> None:
    assert grade_routes.cell(GradeNumber.read("25.0"), "weight", named=True) == "weight 25.0"
    assert grade_routes.cell(GradeNumber.read(""), "average", named=True) == "average left blank"
    assert (
        grade_routes.cell(GradeNumber.not_captured(), "average", named=True)
        == "average not in the copy"
    )


@pytest.mark.parametrize(
    ("kind", "said"),
    [
        ("term", "No percent or letter grade in this copy"),
        ("category", "No weight or average in this copy"),
        ("row", "No score in this copy"),
    ],
)
def test_a_value_the_copy_didn_t_capture_is_named_by_its_own_fields(kind: str, said: str) -> None:
    item = ReviewItem(key="k", status=ItemStatus.VALUE_NOT_CAPTURED)

    assert grade_routes.status_word(grade_routes.Shown(0, item, kind)) == said


def test_a_saved_value_is_described_under_its_field_s_name() -> None:
    saved = {
        "points": (Presence.BLANK, ""),
        "max_points": (Presence.REPORTED, "20.0"),
        "average": (Presence.UNREADABLE, "EX"),
        "percent": (Presence.NOT_CAPTURED, ""),
        "letter": (Presence.REPORTED, "B"),
        "assignment": (Presence.REPORTED, "Cell Diagram"),
    }

    assert grade_routes.saved_now(saved) == [
        "school score left blank",
        "20.0",
        "average couldn't be read: EX",
        "percent not in the copy",
        "B",
    ]


RECORDED = without_columns(
    REPORT.replace(
        "| 09/22   | 0.0       | 0.0       |             | 1.0        |          |",
        "| 09/22   | 2.0       | 1.0       | 0.5         | 1.0        | Redo the labels |",
    ).replace("| 09/24   | 0.0       | 0.0       |", "| 09/24   |           | EX        |"),
    "Labs",
    "Penalty",
    "Note",
)
"""Wren's report with a curve, a bonus, a penalty and a note on Seed Germination Log, and on
Microscope Practice a blank curve, an unreadable bonus, and no Penalty or Note in the copy."""


def result_cards(page: str) -> list[str]:
    """Each result card of a review, as written."""
    results = page.split('id="values-heading"', 1)[1].split("</section>", 1)[0]
    return [card.split("</article>", 1)[0] for card in results.split("<article")[1:]]


def record_details(page: str, title: str) -> tuple[str, str, list[str]]:
    """The opening tag of the school record details on the review's row titled ``title``, its
    summary's words, and the words of each of its lines."""
    (card,) = [
        card
        for card in result_cards(page)
        if words(re.findall(r"<h3>.*?</h3>", card, flags=re.DOTALL)[0]) == title
    ]
    (details,) = re.findall(r"<details[^>]*>.*?</details>", card, flags=re.DOTALL)
    summary = re.search(r"<summary>(.*?)</summary>", details, flags=re.DOTALL)
    assert summary is not None
    lines = [words(line) for line in re.findall(r"<li>(.*?)</li>", details, flags=re.DOTALL)]
    return details.split(">", 1)[0] + ">", words(summary[1]), lines


def test_each_result_row_s_school_record_details_are_closed_and_say_each_cell_it_saves(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        page = review_page(browser, RECORDED)

    assert record_details(page, "Seed Germination Log") == (
        '<details class="steps">',
        "School record details: Adjusted, Teacher's note",
        [
            "Average: 90.0",
            "Curve: 2.0",
            "Bonus: 1.0",
            "Penalty: 0.5",
            "Weight: 1.0",
            "Note: Redo the labels",
        ],
    )
    assert record_details(page, "Microscope Practice") == (
        '<details class="steps">',
        "School record details",
        [
            "Average: 90.0",
            "Curve: left blank",
            "Bonus: couldn't be read: EX",
            "Penalty: not in the copy",
            "Weight: 1.0",
            "Note: not in the copy",
        ],
    )
    assert record_details(page, "Cell Diagram") == (
        '<details class="steps">',
        "School record details",
        [
            "Average: 70.0",
            "Curve: 0.0",
            "Bonus: 0.0",
            "Penalty: left blank",
            "Weight: 1.0",
            "Note: left blank",
        ],
    )


def test_only_result_rows_hold_school_record_details_and_none_holds_a_control(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        page = review_page(browser, RECORDED)

    cards = result_cards(page)
    held = [len(re.findall(r"<details\b", card)) for card in cards]
    assert held == [0, 0, 0, 0, 0, 1, 1, 1, 1]
    for details in re.findall(r"<details\b.*?</details>", page, flags=re.DOTALL):
        assert not re.search(r"<(input|select|textarea|button)\b", details)


def test_a_save_with_every_school_record_details_closed_saves_each_record_cell(
    tmp_path: pathlib.Path,
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        _, said = outcome_of(browser, answered(review_page(browser, RECORDED)))

    assert "Saved. 8 added. 1 is left to check." in said
    with sqlite3.connect(database(settings)) as connection:
        saved = connection.execute(
            "SELECT trim(assignment_text), trim(curve_text), trim(bonus_text), "
            "trim(penalty_text), penalty_presence, trim(note_text), note_presence "
            "FROM grade_result_observations WHERE trim(assignment_text) IN (?, ?) "
            "ORDER BY position",
            ("Seed Germination Log", "Osmosis with Potato Slices"),
        ).fetchall()
    assert saved == [
        ("Seed Germination Log", "2.0", "1.0", "0.5", "reported", "Redo the labels", "reported"),
        ("Osmosis with Potato Slices", "0.0", "0.0", "", "not_captured", "", "not_captured"),
    ]


@pytest.mark.parametrize(
    ("cells", "lines", "cues"),
    [
        (
            {"curve": "0", "bonus": "0.0", "penalty": "-0.0", "note": " "},
            [
                "Average: 90.0",
                "Curve: 0",
                "Bonus: 0.0",
                "Penalty: -0.0",
                "Weight: 1.0",
                "Note: left blank",
            ],
            [],
        ),
        (
            {"curve": "", "bonus": "", "penalty": "-1.5", "note": "Late"},
            [
                "Average: 90.0",
                "Curve: left blank",
                "Bonus: left blank",
                "Penalty: -1.5",
                "Weight: 1.0",
                "Note: Late",
            ],
            ["Adjusted", "Teacher's note"],
        ),
    ],
)
def test_a_row_s_school_record_says_zero_apart_from_blank_and_names_its_cues(
    cells: dict[str, str], lines: list[str], cues: list[str]
) -> None:
    category = grade_drafts.GradeCategory(
        name=GradeValue.read("Labs"),
        weight=GradeNumber.read("25.0"),
        average=GradeNumber.read("83.8"),
        rows=(),
    )
    written = {"weight": "1.0", **cells}
    row = grade_drafts.GradeRow(
        assignment=GradeValue.read("Microscope Practice"),
        points=GradeNumber.read("27.0"),
        max_points=GradeNumber.read("30.0"),
        average=GradeNumber.read("90.0"),
        status=GradeValue.read("Valid"),
        due=grade_drafts.DueText.read("09/24"),
        curve=GradeNumber.read(written["curve"]),
        bonus=GradeNumber.read(written["bonus"]),
        penalty=GradeNumber.read(written["penalty"]),
        weight=GradeNumber.read(written["weight"]),
        note=GradeValue.read(written["note"]),
        occurrence=1,
    )

    record = grade_routes.school_record(category, row)

    assert (record.lines, record.cues) == (lines, cues)


@pytest.mark.parametrize(
    ("average", "line"),
    [
        (GradeNumber.read("90.0"), "Average: 90.0"),
        (GradeNumber.read(""), "Average: left blank"),
        (GradeNumber.read("EX"), "Average: couldn't be read: EX"),
        (GradeNumber.not_captured(), "Average: not in the copy"),
    ],
)
def test_a_row_s_school_record_says_its_average_as_the_report_wrote_it_first(
    average: GradeNumber, line: str
) -> None:
    category = grade_drafts.GradeCategory(
        name=GradeValue.read("Labs"),
        weight=GradeNumber.read("25.0"),
        average=GradeNumber.read("83.8"),
        rows=(),
    )
    row = grade_drafts.GradeRow(
        assignment=GradeValue.read("Microscope Practice"),
        points=GradeNumber.read("27.0"),
        max_points=GradeNumber.read("30.0"),
        average=average,
        status=GradeValue.read("Valid"),
        due=grade_drafts.DueText.read("09/24"),
        curve=GradeNumber.read("0.0"),
        bonus=GradeNumber.read("0.0"),
        penalty=GradeNumber.read(""),
        weight=GradeNumber.read("1.0"),
        note=GradeValue.read(""),
        occurrence=1,
    )

    assert grade_routes.school_record(category, row).lines[:2] == [line, "Curve: 0.0"]


def test_no_family_grade_route_calls_a_model(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_model(*args: object, **kwargs: object) -> None:
        said = "a grade page called a model"
        raise AssertionError(said)

    monkeypatch.setattr(anthropic_client, "chat_model", no_model)
    monkeypatch.setattr(agent_graph, "chat_model", no_model)
    monkeypatch.setattr(anthropic_client.PinnedChatAnthropic, "invoke", no_model)
    monkeypatch.setattr(anthropic_client.PinnedChatAnthropic, "ainvoke", no_model)
    settings = fixture_settings(
        BLOSSOM_TODAY=PLAN_DATE.isoformat(),
        ANTHROPIC_API_KEY="not-a-key-and-never-sent",
        **files_in(tmp_path),
    )
    with at(settings) as browser:
        statuses = every_family_request(browser)
        no_line = browser.post(REVIEW, data={"report_text": NO_LINE}, headers=PAGE)

    assert statuses == ALLOWED
    assert no_line.status_code == 200


def test_no_paste_or_student_line_reaches_a_log_or_an_address(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    marked = REPORT.replace("Bramble, Wren", "Bramble, Sentinelwren").replace(
        "Seed Germination Log", "Sentinel Seed Log"
    )
    caplog.set_level(logging.DEBUG)
    with at(open_household(tmp_path)) as browser:
        answers = [
            browser.post(path, data={"report_text": marked}, headers=PAGE)
            for path in (REVIEW, EDIT)
        ]
        answers.append(browser.post(REVIEW, data={"report_text": "x" * 50_000}, headers=PAGE))

    assert "Sentinel" not in caplog.text
    assert all("location" not in answer.headers for answer in answers)


def test_a_line_that_matches_a_confirmed_form_is_shown_with_no_question(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        browser.post(REVIEW, data={"report_text": REPORT}, headers=PAGE)
        key = browser.app.state.grade_name_key  # type: ignore[attr-defined]
        draft = read_grade_report(REPORT).draft
        assert draft is not None
        save_grade(store_of(browser), draft, key=key)
        page = browser.post(REVIEW, data={"report_text": REPORT}, headers=PAGE).text
    about = page.split('id="about-this-report"', 1)[1].split("</fieldset>", 1)[0]

    assert "Bramble, Wren" in about
    assert '<input type="hidden" name="identity" value="shown">' in about
    assert 'type="radio"' not in about
    assert "not_hers" not in about


STYLESHEET = pathlib.Path(grade_routes.__file__).parents[1] / "static" / "blossom.css"
TEMPLATES = STYLESHEET.parents[1] / "templates"
GRADE_REVIEW_RULE = (
    ".grade-review .choice {\n  min-width: 0;\n"
    "  padding-inline: clamp(0px, 13vw - 1rem, 1rem);\n}\n"
)
"""The grade review's rule for its fieldsets, pinned whole: a fieldset takes the page's width,
and its side padding gives way on a narrow screen with large text."""
IDENTITY_RULES = (
    ".grade-review .choice.identity label {\n  flex-wrap: wrap;\n}\n\n"
    ".grade-review .choice.identity label > span {\n  flex: 1 1 8rem;\n}\n"
)
"""The grade review's rules for its "Is this her?" choices, pinned whole: a choice's words go
under its radio when less than 8rem is left beside it."""
CARD_RULES = (
    ".grade-review .panel,\n.grade-review article,\n.grade-outcome,\n.grade-card {\n"
    "  margin-inline: min(0px, 13vw - 2.25rem);\n"
    "  padding-inline: clamp(0px, 13vw - 1rem, 1.6rem);\n}\n\n"
    "@media (max-width: 30rem) {\n"
    "  .grade-review .panel,\n  .grade-review article,\n  .grade-outcome,\n  .grade-card {\n"
    "    padding-inline: clamp(0px, 13vw - 1rem, 1.2rem);\n  }\n}\n"
)
"""The grade review's cards, the saved report's outcome, and the cards of Grades and class
details, pinned whole: on a narrow screen with large text, a card's side padding gives way and
the card reaches into the page's side margin, so a word of a class, category or assignment name
breaks only where the line can't hold it."""
SHARED_CARD_RULES = (
    "main {\n  max-width: 46rem;\n  margin: 0 auto;\n  padding: 0.5rem 1.25rem 3rem;\n}\n",
    "article,\n.panel {\n  background: var(--surface);\n  border: 1px solid var(--edge);\n"
    "  border-radius: var(--radius);\n  padding: 1.35rem 1.6rem;\n  margin-block: 1.1rem;\n",
    "  article,\n  .panel {\n    padding: 1.1rem 1.2rem;\n  }\n",
)
"""The page's side margin and the card insets every page shares, pinned as they stand: the
cards' rules above give way from these values and reach into this margin."""
SHARED_CHOICE_RULE = (
    ".choice {\n  margin: 0.75rem 0;\n  padding: 0.75rem 1rem;\n"
    "  border: 1px solid var(--field-edge);\n  border-radius: var(--radius-small);\n}\n"
)
"""The fieldset rule every review shares, pinned as it stands, so the grade review's width
fix stays in its own rule."""


def test_the_grade_review_s_own_rule_is_pinned_and_only_its_form_carries_its_class() -> None:
    css = STYLESHEET.read_text(encoding="utf-8")
    carriers = sorted(
        page.name
        for page in TEMPLATES.glob("*.html")
        if "grade-review" in page.read_text(encoding="utf-8")
    )

    assert css.count("grade-review") == 8
    assert css.count(GRADE_REVIEW_RULE) == 1
    assert css.count(IDENTITY_RULES) == 1
    assert css.count(CARD_RULES) == 1
    assert css.count(SHARED_CHOICE_RULE) == 1
    assert carriers == ["grade_review.html"]


def test_the_outcome_card_s_rule_is_pinned_and_only_the_saved_page_carries_its_class() -> None:
    css = STYLESHEET.read_text(encoding="utf-8")
    carriers = sorted(
        page.name
        for page in TEMPLATES.glob("*.html")
        if "grade-outcome" in page.read_text(encoding="utf-8")
    )

    assert css.count("grade-outcome") == 2
    assert css.count(CARD_RULES) == 1
    for shared in SHARED_CARD_RULES:
        assert css.count(shared) == 1, shared
    assert carriers == ["grade_saved.html"]
    page = (TEMPLATES / "grade_saved.html").read_text(encoding="utf-8")
    outcome = '<section class="panel grade-outcome" aria-labelledby="outcome-heading">'
    assert page.count(outcome) == 1


def test_the_cards_of_grades_and_class_details_share_the_grade_card_rule() -> None:
    css = STYLESHEET.read_text(encoding="utf-8")
    carriers = sorted(
        page.name
        for page in TEMPLATES.glob("*.html")
        if "grade-card" in page.read_text(encoding="utf-8")
    )

    assert css.count("grade-card") == 2
    assert css.count(CARD_RULES) == 1
    for shared in SHARED_CARD_RULES:
        assert css.count(shared) == 1, shared
    assert carriers == ["grade_class.html", "grades.html"]
    grades = (TEMPLATES / "grades.html").read_text(encoding="utf-8")
    card = '<section class="panel grade-card" aria-labelledby="class-{{ loop.index }}">'
    assert grades.count(card) == 1
    assert grades.count("<section") == 1
    details = (TEMPLATES / "grade_class.html").read_text(encoding="utf-8")
    assert details.count('<section class="panel grade-card">') == 1
    assert details.count("<section") == 1


HEADING_RULE = ".grade-heading {\n  margin-inline: clamp(-0.95rem, 26vw - 4.5rem, 0px);\n}\n"
"""Class details' headings, pinned whole: on a narrow screen with large text, a heading reaches
into the page's side margin until it is 0.3rem from the screen's edge, so a word of a class's
name breaks only where the whole line can't hold it."""
SHARED_HEADING_RULES = (
    "h1 {\n  font-size: 2.1rem;\n  font-weight: 700;\n  color: var(--blue-action);\n"
    "  margin: 0.75rem 0 0.25rem;\n  letter-spacing: 0.01em;\n}\n",
    "h2 {\n  font-size: 1.3rem;\n  font-weight: 600;\n  color: var(--blue-action);\n"
    "  margin: 0 0 0.6rem;\n}\n",
    SHARED_CARD_RULES[0],
)
"""The headings' own rules and the page's side margin, pinned as they stand: the headings' rule
above replaces their side margins of nothing and reaches into this margin."""


def test_the_class_details_headings_rule_is_pinned_and_only_its_headings_carry_it() -> None:
    css = STYLESHEET.read_text(encoding="utf-8")
    carriers = sorted(
        page.name
        for page in TEMPLATES.glob("*.html")
        if "grade-heading" in page.read_text(encoding="utf-8")
    )

    assert css.count("grade-heading") == 1
    assert css.count(HEADING_RULE) == 1
    for shared in SHARED_HEADING_RULES:
        assert css.count(shared) == 1, shared
    assert carriers == ["grade_class.html"]
    page = (TEMPLATES / "grade_class.html").read_text(encoding="utf-8")
    named = '<h1 class="grade-heading"><span class="authored-text">{{ name }}</span></h1>'
    assert page.count(named) == 1
    assert page.count('<h2 class="grade-heading">') == 3
    assert page.count("<h2") == 3


MAKE_CURRENT_RULE = (
    "button.make-current {\n"
    "  padding-inline: clamp(0px, 13vw - 1rem, 1.35rem);\n"
    "  overflow-wrap: anywhere;\n"
    "}\n\n"
    "@media (max-width: 30rem) {\n"
    "  button.make-current {\n"
    "    flex: 1 1 100%;\n"
    "    border-radius: var(--radius);\n"
    "  }\n"
    "}\n"
)
"""The parent's choice of the household's current term, pinned whole: on a narrow screen the
button takes the whole line with a card's corners, with large text its side padding gives way,
and only a word wider than the whole line breaks."""
SHARED_BUTTON_RULES = (
    ".actions {\n  display: flex;\n  gap: 0.6rem;\n  flex-wrap: wrap;\n}\n",
    "button {\n  font: inherit;\n  font-family: var(--font-display);\n  font-weight: 700;\n"
    "  min-height: 2.75rem;\n  padding: 0.6rem 1.35rem;\n",
)
"""The row of actions and the button every page shares, pinned as they stand: the choice's rule
above gives way from their padding and keeps their 44 pixel height."""


def test_the_current_term_choice_s_rule_is_pinned_and_only_grades_carries_it() -> None:
    css = STYLESHEET.read_text(encoding="utf-8")
    carriers = sorted(
        page.name
        for page in TEMPLATES.glob("*.html")
        if "make-current" in page.read_text(encoding="utf-8")
    )

    assert css.count("make-current") == 2
    assert css.count(MAKE_CURRENT_RULE) == 1
    for shared in SHARED_BUTTON_RULES:
        assert css.count(shared) == 1, shared
    assert carriers == ["grades.html"]
    page = (TEMPLATES / "grades.html").read_text(encoding="utf-8")
    button = (
        '<button type="submit" class="secondary make-current">'
        "Use {{ make_current.named }} as the household's current term</button>"
    )
    assert page.count(button) == 1


GRADE_PANEL_RULE = (
    ".grade-panel .decision button {\n"
    "  padding-inline: clamp(0px, 13vw - 1rem, 1.35rem);\n"
    "  overflow-wrap: normal;\n"
    "}\n"
)
"""The paste and retry pages' one rule, pinned whole: their buttons' side padding gives way on
a narrow screen with large text, and their words, the page's own, stay whole."""


def test_the_grade_panel_rule_is_pinned_and_only_the_paste_and_retry_panels_carry_it() -> None:
    css = STYLESHEET.read_text(encoding="utf-8")
    carriers = sorted(
        page.name
        for page in TEMPLATES.glob("*.html")
        if "grade-panel" in page.read_text(encoding="utf-8")
    )

    assert css.count("grade-panel") == 1
    assert css.count(GRADE_PANEL_RULE) == 1
    assert carriers == ["grade_add.html", "grade_retry.html"]
    for carrier in carriers:
        page = (TEMPLATES / carrier).read_text(encoding="utf-8")
        assert page.count('<section class="panel grade-panel">') == 1


GRADE_SELECTS_RULE = (
    ".grade-review select {\n  appearance: base-select;\n  overflow-wrap: anywhere;\n}\n"
)
"""The grade review's rule for its selects, pinned whole: where the browser offers the
customizable select, the chosen value wraps, at every width."""
GRADE_SELECT_CHECK = (
    "A rule that can reach a select of the grade review changed or appeared. Check the review "
    "in a browser with its longest choice chosen, at 320, 375 and 414 px with 200% text and at "
    "1280 px, and in a browser without the customizable select: the whole chosen name shows, "
    "nothing scrolls sideways, the select is at least 44 px tall and its focus outline shows. "
    "Then copy the rules found into GRADE_SELECT_RULES."
)
GRADE_SELECT_RULES: tuple[str, ...] = (
    '@font-face { font-family: "Quicksand"; src: '
    'url("/static/fonts/Quicksand-Variable.ttf") format("truetype"); font-weight: 300 700; '
    "font-style: normal; font-display: swap }",
    '@font-face { font-family: "Outfit"; src: url("/static/fonts/Outfit-Variable.ttf") '
    'format("truetype"); font-weight: 300 700; font-style: normal; font-display: swap }',
    ":root { --canvas: #fdf5f7; --rule: rgba(155, 184, 211, 0.1); --blue-action: #4c7193; "
    "--text: #4a5c6f; --surface: rgba(255, 255, 255, 0.6); --surface-strong: rgba(255, "
    "255, 255, 0.82); --edge: rgba(155, 184, 211, 0.28); --shadow: 0 8px 24px rgba(155, "
    "184, 211, 0.15); --rose-ink: #96505f; --radius: 16px; --radius-small: 12px; "
    '--font-body: "Outfit", "Rubik", "Quicksand", system-ui, sans-serif }',
    "* { box-sizing: border-box }",
    "html { color-scheme: light }",
    "body { margin: 0; min-height: 100vh; font-family: var(--font-body); font-weight: 400; "
    "color: var(--text); line-height: 1.6; background: repeating-linear-gradient(to "
    "bottom, transparent 0 2rem, var(--rule) 2rem calc(2rem + 1px)), var(--canvas) }",
    "main { max-width: 46rem; margin: 0 auto; padding: 0.5rem 1.25rem 3rem }",
    "section { margin-block: 2rem }",
    "article, .panel { background: var(--surface); border: 1px solid var(--edge); "
    "border-radius: var(--radius); padding: 1.35rem 1.6rem; margin-block: 1.1rem; "
    "box-shadow: var(--shadow); backdrop-filter: blur(8px) }",
    "input:focus-visible, textarea:focus-visible, select:focus-visible, "
    "button:focus-visible, a:focus-visible, summary:focus-visible { outline: 3px solid "
    "var(--blue-action); outline-offset: 2px }",
    "@media (max-width: 30rem) / article, .panel { padding: 1.1rem 1.2rem }",
    "textarea, select { font: inherit; font-family: var(--font-body); color: var(--text); "
    "padding: 0.55rem 0.85rem; border: 1px solid var(--edge); border-radius: "
    "var(--radius-small); background: var(--surface-strong) }",
    "select { min-height: 2.75rem }",
    ":root { --field-edge: #6b7d90 }",
    'input[type="text"], input[type="date"], input[type="password"], textarea, select { '
    "border-color: var(--field-edge); min-width: 0; max-width: 100% }",
    'input[aria-invalid="true"], textarea[aria-invalid="true"], '
    'select[aria-invalid="true"] { border-color: var(--rose-ink); border-width: 2px }',
    ".choice { margin: 0.75rem 0; padding: 0.75rem 1rem; border: 1px solid "
    "var(--field-edge); border-radius: var(--radius-small) }",
    ".grade-review .choice { min-width: 0; padding-inline: clamp(0px, 13vw - 1rem, 1rem) }",
    ".grade-review select { appearance: base-select; overflow-wrap: anywhere }",
    ".grade-review .panel, .grade-review article, .grade-outcome, .grade-card { "
    "margin-inline: min(0px, 13vw - 2.25rem); padding-inline: clamp(0px, 13vw - 1rem, 1.6rem) }",
    "@media (max-width: 30rem) / .grade-review .panel, .grade-review article, "
    ".grade-outcome, .grade-card { padding-inline: clamp(0px, 13vw - 1rem, 1.2rem) }",
    "main.wide { max-width: 46rem }",
    "@media (min-width: 72rem) / main.wide { max-width: 72rem }",
    "@media (min-width: 72rem) / main.wide .review-form { display: grid; "
    "grid-template-columns: minmax(0, 46rem) minmax(16rem, 22rem); gap: 0 2rem; "
    "align-items: start }",
)
"""Every rule that can reach a select of the grade review or an element above it, with the
custom properties it uses and the font faces, as the stylesheet writes them: what the selects
were checked with in a browser."""


def select_chains(page: str) -> set[Chain]:
    """Each select of a page and every element above it, as tag, classes and id."""
    return {
        tuple(
            (one.tag, one.classes, one.attributes.get("id"))
            for one in [select, *select.ancestors()]
        )
        for select in elements_of(page)
        if select.tag == "select"
    }


def check_grade_select_rules(css: str, chains: set[Chain]) -> None:
    assert rules_reaching(css, *chains) == list(GRADE_SELECT_RULES), GRADE_SELECT_CHECK


def test_the_rules_that_reach_the_grade_review_s_selects_are_the_checked_ones(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every state of the review is styled by the one stylesheet and nothing inline, and the
    rules that can reach its selects are the ones they were checked with."""
    chains: set[Chain] = set()
    for state, page in every_review_state(tmp_path, monkeypatch).items():
        chains |= select_chains(page)
        assert "<style" not in page.lower(), state
        assert ' style="' not in page, state
    css = STYLESHEET.read_text(encoding="utf-8")

    assert {chain[0][2] for chain in chains} >= {"first-month", "choose-5", "choose-6"}
    assert css.count(GRADE_SELECTS_RULE) == 1
    check_grade_select_rules(css, chains)


def one_list(tmp_path: pathlib.Path) -> set[Chain]:
    """The selects of a review whose row offers saved results to choose from."""
    with at(open_household(tmp_path)) as browser:
        first_saved(browser)
        return select_chains(review_page(browser, UNRELATED))


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda css: css.replace(GRADE_SELECTS_RULE, ""), id="no-rule"),
        pytest.param(
            lambda css: css.replace(
                GRADE_SELECTS_RULE, GRADE_SELECTS_RULE.replace("base-select", "auto")
            ),
            id="native",
        ),
        pytest.param(
            lambda css: css.replace(
                GRADE_SELECTS_RULE, GRADE_SELECTS_RULE.replace("anywhere", "normal")
            ),
            id="long-word-kept",
        ),
        pytest.param(
            lambda css: css.replace(
                GRADE_SELECTS_RULE, "@media (max-width: 30rem) {\n" + GRADE_SELECTS_RULE + "}\n"
            ),
            id="narrow-only",
        ),
        pytest.param(
            lambda css: css + "\n.grade-review select { white-space: nowrap; }\n", id="nowrap"
        ),
        pytest.param(
            lambda css: css + "\n.grade-review .choice { white-space: nowrap; }\n",
            id="nowrap-above",
        ),
        pytest.param(lambda css: css + "\nselect { max-width: none; }\n", id="every-select"),
    ],
)
def test_a_rule_that_can_reach_a_grade_select_fails_the_check(
    tmp_path: pathlib.Path, edit: Callable[[str], str]
) -> None:
    chains = one_list(tmp_path)
    with pytest.raises(AssertionError):
        check_grade_select_rules(edit(STYLESHEET.read_text(encoding="utf-8")), chains)


def test_a_rule_for_another_page_s_select_passes_the_grade_select_check(
    tmp_path: pathlib.Path,
) -> None:
    css = STYLESHEET.read_text(encoding="utf-8") + "\n.adding-note select { color: red; }\n"
    check_grade_select_rules(css, one_list(tmp_path))


# ------------------------------------------------------------- the save, the check, the outcome


def review_page(browser: TestClient, text: str = REPORT) -> str:
    answer = browser.post(REVIEW, data={"report_text": text}, headers=PAGE)
    assert answer.status_code == 200, answer.text
    return answer.text


def answered(page: str, **more: str) -> dict[str, str]:
    """The review's form as sent with "Yes, this is her name" chosen, and ``more``."""
    return {**sent_from(page), "identity": "hers", **more}


def unticked(form: dict[str, str], *keep: str) -> dict[str, str]:
    """``form`` with every box unticked but the positions ``keep`` names."""
    return {
        name: value
        for name, value in form.items()
        if not name.startswith("select.") or name.removeprefix("select.") in keep
    }


def outcome_of(browser: TestClient, form: dict[str, str]) -> tuple[str, str]:
    """Where saving ``form`` lands, and the words of the outcome it lands on."""
    saved = browser.post(SAVE, data=form, headers=PAGE)
    assert saved.status_code == 303, saved.text
    location = saved.headers["location"]
    assert location.startswith("/parent/grades/saved/acceptance-")
    page = browser.get(location, headers=PAGE)
    assert page.status_code == 200, page.text
    return location, words(page.text)


def test_a_save_from_the_review_lands_on_its_outcome_by_its_acceptance_id(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        form = answered(review_page(browser))
        location, said = outcome_of(browser, form)

    assert location == SAVED.format(acceptance_id=form["acceptance_id"])
    assert "Saved. 9 added." in said
    assert "Biology" in said
    assert "T1 · 2026-2027" in said
    assert "Her name was confirmed with this report." in said
    assert "Nothing was saved" not in said


def test_an_outcome_links_its_class_details_and_grades(tmp_path: pathlib.Path) -> None:
    with at(open_household(tmp_path)) as browser:
        saved = browser.post(SAVE, data=answered(review_page(browser)), headers=PAGE)
        outcome = browser.get(saved.headers["location"], headers=PAGE)
        class_id = capture_class(store_of(browser), FIRST_TERM)
        details = browser.get(CLASS_AT.format(class_id=class_id, n=1), headers=PAGE)

    link = f'<a href="{CLASS_AT.format(class_id=class_id, n=1)}"><span class="authored-text">See'
    assert link in outcome.text
    assert "See Biology" in words(outcome.text)
    assert f'<a href="{GRADES}">Grades</a></p>' in outcome.text
    assert details.status_code == 200


def test_a_save_that_records_only_the_acceptance_never_says_nothing_was_saved(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        form = unticked(answered(review_page(browser)))
        _, said = outcome_of(browser, form)

    assert grade_routes.NO_VALUES_CHANGED in said
    assert "Saved." not in said
    assert "Nothing was saved" not in said
    assert "Biology" in said


def test_a_resent_save_returns_its_outcome_and_writes_nothing(tmp_path: pathlib.Path) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = answered(review_page(browser))
        first = browser.post(SAVE, data=form, headers=PAGE)
        before = closed_world([database(settings)], leaving_out=())
        again = browser.post(SAVE, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert again.status_code == 303
    assert again.headers["location"] == first.headers["location"]
    assert after == before


def test_a_resent_save_with_rows_that_save_left_out_says_so(tmp_path: pathlib.Path) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = answered(review_page(browser))
        browser.post(SAVE, data=unticked(form, "0"), headers=PAGE)
        before = closed_world([database(settings)], leaving_out=())
        again = browser.post(SAVE, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert again.status_code == 200
    assert escape(grade_routes.SAVED_ELSEWHERE) in again.text
    assert problem_said(again.text) == (
        "This report was saved from another page or tab. Your text is kept. Check the answers "
        "again. See what was saved"
    )
    assert escape(grade_routes.LEFT_OUT) in again.text
    assert 'name="select.1" value="1" checked' in again.text
    assert whole_form(again.text, SAVE)["acceptance_id"] != form["acceptance_id"]
    assert after == before


def test_a_save_with_no_answer_about_the_name_keeps_everything_and_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = {**sent_from(review_page(browser)), "class_name": "Bio Lab"}
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(SAVE, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 422
    assert escape(grade_routes.ANSWER_THE_NAME) in answer.text
    kept = whole_form(answer.text, SAVE)
    assert kept["acceptance_id"] == form["acceptance_id"]
    assert kept["class_name"] == "Bio Lab"
    assert "identity" not in kept
    assert after == before


def test_not_hers_ends_the_import_and_writes_nothing(tmp_path: pathlib.Path) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = answered(review_page(browser), identity="not_hers")
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(SAVE, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 200
    assert escape(grade_routes.NOT_HERS) in answer.text
    assert 'name="report_text"' not in answer.text
    assert after == before


@pytest.mark.parametrize(("field", "label"), [("class_name", "class name"), ("setup_term", "term")])
def test_a_label_over_its_limit_is_named_and_nothing_is_saved(
    field: str, label: str, tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    long = "B" * 61 if field == "class_name" else "T" * 21
    with at(settings) as browser:
        form = answered(review_page(browser), **{field: long})
        if field == "setup_term":
            form.update(setup="other", setup_year="2026-2027")
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(SAVE, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 422
    assert f"The {label} is longer than its limit" in words(answer.text)
    assert whole_form(answer.text, SAVE)[field] == long
    assert after == before


def test_a_changed_text_returns_the_review_and_writes_nothing(tmp_path: pathlib.Path) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = answered(review_page(browser))
        changed = form["report_text"].replace("| 7.0 ", "| 8.0 ")
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(SAVE, data={**form, "report_text": changed}, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert changed != form["report_text"]
    assert answer.status_code == 409
    assert escape(grade_routes.TEXT_CHANGED) in answer.text
    assert after == before


def two_tabs(browser: TestClient) -> tuple[dict[str, str], dict[str, str]]:
    """Two pages for the same class and term after a first save of its term grade alone."""
    browser.post(SAVE, data=unticked(answered(review_page(browser)), "0"), headers=PAGE)
    one_tab = {**sent_from(review_page(browser)), "identity": "shown"}
    another_tab = {**sent_from(review_page(browser)), "identity": "shown"}
    return one_tab, another_tab


def test_a_check_after_another_tab_s_save_says_so_and_leaves_the_next_save_protected(
    tmp_path: pathlib.Path,
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        one_tab, another_tab = two_tabs(browser)
        assert browser.post(SAVE, data=one_tab, headers=PAGE).status_code == 303
        before = closed_world([database(settings)], leaving_out=())
        checked = browser.post(CHECK, data=another_tab, headers=PAGE)
        between = closed_world([database(settings)], leaving_out=())
        saved = browser.post(SAVE, data=another_tab, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert checked.status_code == 409
    assert escape(grade_routes.CHANGED_WHILE_REVIEWING) in checked.text
    assert problem_said(checked.text) == (
        "These grades changed while you were reviewing. Nothing was saved. Check the review "
        "again. Your text is kept. Check the answers again."
    )
    assert "select." not in " ".join(sent_from(checked.text))
    assert between == before
    assert saved.status_code == 409
    assert escape(grade_routes.CHANGED_WHILE_REVIEWING) in saved.text
    assert after == before


def test_a_check_of_a_current_page_keeps_its_id_and_revision_and_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = answered(review_page(browser), class_name="Bio Lab")
        before = closed_world([database(settings)], leaving_out=())
        checked = browser.post(CHECK, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())
        kept = sent_from(checked.text)
        saved = browser.post(SAVE, data=kept, headers=PAGE)

    assert checked.status_code == 200
    assert (kept["acceptance_id"], kept["revision"]) == (form["acceptance_id"], form["revision"])
    assert (kept["identity"], kept["class_name"]) == ("hers", "Bio Lab")
    assert after == before
    assert saved.status_code == 303


def test_a_check_of_a_saved_page_says_it_was_saved_and_links_its_outcome(
    tmp_path: pathlib.Path,
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = answered(review_page(browser))
        saved = browser.post(SAVE, data=form, headers=PAGE)
        before = closed_world([database(settings)], leaving_out=())
        checked = browser.post(CHECK, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert checked.status_code == 200
    assert escape(grade_routes.SAVED_ELSEWHERE) in checked.text
    assert f'href="{saved.headers["location"]}"' in checked.text
    assert after == before


@pytest.mark.parametrize("acceptance_id", ["not-an-id", UNKNOWN, "acceptance-" + "g" * 32])
def test_an_outcome_not_on_record_says_so(acceptance_id: str, tmp_path: pathlib.Path) -> None:
    with at(open_household(tmp_path)) as browser:
        answer = browser.get(SAVED.format(acceptance_id=acceptance_id), headers=PAGE)

    assert answer.status_code == 404
    assert escape(grade_routes.NOT_ON_RECORD) in answer.text
    assert f'href="{ADD}"' in answer.text


def test_an_outcome_whose_class_and_term_were_deleted_is_not_on_record(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        saved = browser.post(SAVE, data=answered(review_page(browser)), headers=PAGE)
        store = store_of(browser)
        (class_id,) = store._connection.execute("SELECT class_id FROM grade_classes").fetchone()
        (revision,) = store._connection.execute(
            "SELECT revision FROM grade_scope_revisions"
        ).fetchone()
        store.delete_class_term(class_id, "T1", revision=revision, role="household")
        answer = browser.get(saved.headers["location"], headers=PAGE)

    assert answer.status_code == 404
    assert escape(grade_routes.NOT_ON_RECORD) in answer.text


def test_a_recorded_outcome_answers_only_where_a_fresh_request_would(
    tmp_path: pathlib.Path,
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = answered(review_page(browser))
        location = browser.post(SAVE, data=form, headers=PAGE).headers["location"]
    with at(settings, client="192.0.2.10") as elsewhere:
        read = elsewhere.get(location, headers=PAGE)
        resent = elsewhere.post(SAVE, data=form, headers=PAGE)
    with at(settings, host="blossom.example") as renamed:
        renamed_read = renamed.get(location, headers=PAGE)

    for answer in (read, resent, renamed_read):
        assert answer.status_code == 403
        assert "9 added" not in answer.text
        assert "location" not in answer.headers


def test_with_sign_in_on_her_sign_in_and_no_sign_in_get_no_recorded_outcome(
    tmp_path: pathlib.Path,
) -> None:
    settings = signed_in_household(tmp_path)
    with at(settings, host="testserver") as browser:
        signed_in(browser, THEIRS)
        form = answered(review_page(browser))
        location = browser.post(SAVE, data=form, headers=PAGE).headers["location"]
    with at(settings, host="testserver") as hers:
        signed_in(hers, HERS)
        her_read = hers.get(location, headers=PAGE)
    with at(settings, host="testserver") as nobody:
        nobody_read = nobody.get(location, headers=PAGE)

    assert her_read.status_code == 403
    assert "9 added" not in her_read.text
    assert nobody_read.status_code == 303
    assert nobody_read.headers["location"].startswith("/sign-in")


def test_a_save_the_store_refuses_keeps_every_field_for_a_retry(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*args: object, **kwargs: object) -> None:
        said = "refused"
        raise GradeReportNotSaved(said)

    def lost(*args: object, **kwargs: object) -> None:
        said = "lost"
        raise GradeTransactionLost(said)

    with at(open_household(tmp_path)) as browser:
        form = answered(review_page(browser))
        store = store_of(browser)
        monkeypatch.setattr(store, "save_grade_report", refused)
        not_saved = browser.post(SAVE, data=form, headers=PAGE)
        monkeypatch.setattr(store, "save_grade_report", lost)
        unknown = browser.post(SAVE, data=form, headers=PAGE)

    assert not_saved.status_code == unknown.status_code == 500
    assert escape(grade_routes.NOT_SAVED) in not_saved.text
    assert escape(grade_routes.OUTCOME_UNKNOWN) in unknown.text
    for answer in (not_saved, unknown):
        assert sent_from(answer.text) == form


@pytest.mark.parametrize(
    "name", ["science-grade-report.txt", "geometry-grade-report.txt", "spanish-grade-report.txt"]
)
def test_a_tab_separated_paste_round_trips_through_the_review_to_a_save(
    name: str, tmp_path: pathlib.Path
) -> None:
    text = (FIXTURES / "grade_clipboard" / name).read_bytes().decode("utf-8")
    pasted = as_a_browser_sends({"report_text": "\n" + text})["report_text"]
    with at(open_household(tmp_path)) as browser:
        form = as_a_browser_sends(answered(review_page(browser, pasted)))
        _, said = outcome_of(browser, form)
        again = review_page(browser, pasted)

    original = read_grade_report(text).draft
    assert original is not None
    assert form["source_key"] == capture_key(original)
    assert "Saved." in said
    assert "New" not in re.findall(r'<span class="pill">([^<]*)<', again)


def test_family_review_links_to_adding_a_grade_report(tmp_path: pathlib.Path) -> None:
    with at(open_household(tmp_path)) as browser:
        page = browser.get("/parent", headers=PAGE).text

    assert f'<a href="{ADD}">Add grade report</a>' in page


def test_answers_another_tab_settled_return_the_review_and_keep_what_still_applies(
    tmp_path: pathlib.Path,
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        one_tab = answered(review_page(browser))
        another_tab = answered(review_page(browser), class_name="Bio Lab")
        assert browser.post(SAVE, data=unticked(one_tab), headers=PAGE).status_code == 303
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(SAVE, data=another_tab, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 409
    assert escape(grade_routes.ANSWER_DOESNT_FIT) in answer.text
    assert problem_said(answer.text) == (
        "An answer doesn't fit this report now: another page saved it, or the name check "
        "changed. Nothing was saved. Your text is kept. Check the answers again."
    )
    kept = whole_form(answer.text, SAVE)
    assert kept["identity"] == "shown"
    assert "setup" not in kept
    assert "class" not in kept
    assert kept["acceptance_id"] != another_tab["acceptance_id"]
    assert after == before


def test_a_tick_on_a_result_not_offered_returns_the_review_and_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        browser.post(SAVE, data=answered(review_page(browser)), headers=PAGE)
        form = {**sent_from(review_page(browser)), "identity": "shown", "select.1": "1"}
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(SAVE, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 409
    assert escape(grade_routes.NOT_NEW) in answer.text
    assert problem_said(answer.text) == (
        "A ticked result isn't new any more. Nothing was saved. Check the ticks again. Your text "
        "is kept. Check the answers again."
    )
    assert "select.1" not in whole_form(answer.text, SAVE)
    assert after == before


def test_a_check_of_a_changed_text_says_so_and_writes_nothing(tmp_path: pathlib.Path) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = answered(review_page(browser))
        changed = form["report_text"].replace("| 7.0 ", "| 8.0 ")
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(CHECK, data={**form, "report_text": changed}, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 409
    assert escape(grade_routes.TEXT_CHANGED) in answer.text
    assert after == before


LINES = REPORT.split("\n")
STRAY = "\n".join([*LINES[:8], "A stray line", *LINES[8:]])
"""Wren's report with a line Blossom doesn't recognize between its header and its Term Grade
row, so its reading isn't complete."""
CHANGED_ALIKE = {
    "stray line left out": (STRAY, REPORT),
    "spaces in a cell": (
        REPORT,
        REPORT.replace("| Seed Germination Log | 18.0    |", "| Seed Germination Log |  18.0   |"),
    ),
    "blank line added": (REPORT, REPORT.replace("**PERCENT**\n", "**PERCENT**\n\n", 1)),
    "line added": (REPORT, REPORT + "Printed for the family\n"),
}
"""A text reviewed and the text sent back in its place, each read to the same capture key."""


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize(("shown", "sent"), list(CHANGED_ALIKE.values()), ids=list(CHANGED_ALIKE))
def test_a_text_changed_but_read_alike_says_so_and_writes_nothing(
    route: str, shown: str, sent: str, tmp_path: pathlib.Path
) -> None:
    """The form binds the text it carried, not only its reading: a reading the page showed as
    incomplete, sent back without its stray line, isn't saved as complete."""
    settings = open_household(tmp_path)
    with at(settings) as browser:
        page = review_page(browser, shown)
        form = answered(page)
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data={**form, "report_text": sent}, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())
    draft = read_grade_report(sent).draft
    assert draft is not None

    assert capture_key(draft) == form["source_key"]
    assert answer.status_code == 409, answer.text[:300]
    assert problem_said(answer.text) == (
        "The report text changed. Nothing was saved. Review the text again. Your text is kept. "
        "Check the answers again."
    )
    returned = sent_from(answer.text)
    assert returned["report_text"] == sent
    assert returned["acceptance_id"] != form["acceptance_id"]
    assert returned["identity"] == "hers"
    if shown == STRAY:
        assert "Lines Blossom didn" in page
        assert "Lines Blossom didn" not in answer.text
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("before_it", ["name unanswered", "not hers", "saved"])
def test_a_changed_text_is_answered_before_anything_else_the_form_says(
    route: str, before_it: str, tmp_path: pathlib.Path
) -> None:
    """The text is checked before the name's answer, "Not hers" or a recorded save is read."""
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = sent_from(review_page(browser, STRAY))
        if before_it == "not hers":
            form["identity"] = "not_hers"
        if before_it == "saved":
            form = answered(review_page(browser, STRAY))
            assert browser.post(SAVE, data=form, headers=PAGE).status_code == 303
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data={**form, "report_text": REPORT}, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 409, answer.text[:300]
    assert escape(grade_routes.TEXT_CHANGED) in answer.text
    assert sent_from(answer.text)["report_text"] == REPORT
    assert after == before


def test_a_changed_text_the_secret_kept_from_a_check_is_answered_when_sent_again(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry page sends back the fields no row's place decides as they came, so the key of
    the text its page carried still meets the changed text on the next save, and nothing a row
    sent rides along."""
    with at(open_household(tmp_path)) as browser:
        form = answered(review_page(browser, STRAY))
        del browser.app.state.grade_name_key  # type: ignore[attr-defined]
        unreadable(tmp_path, monkeypatch)
        retry = browser.post(SAVE, data={**form, "report_text": REPORT}, headers=PAGE)
        monkeypatch.undo()
        again = browser.post(SAVE, data=sent_from(retry.text), headers=PAGE)

    assert retry.status_code == 500
    sent = {**form, "report_text": REPORT}
    assert sent_from(retry.text) == {
        name: value for name, value in sent.items() if name in POSITION_FREE
    }
    assert again.status_code == 409
    assert escape(grade_routes.TEXT_CHANGED) in again.text


ROUND_TRIPS = {
    "line feeds": REPORT,
    "carriage returns": as_a_browser_sends({"text": REPORT})["text"],
    "a leading line break": as_a_browser_sends({"text": "\n" + REPORT})["text"],
    "trailing spaces": "\r\n".join(f"{line}  \t" for line in LINES),
    "letters, < and &": REPORT.replace("Seed Germination Log", "Señal & <Growth> Log &amp;"),
    "tabs": as_a_browser_sends(
        {
            "text": "\n"
            + (FIXTURES / "grade_clipboard" / "science-grade-report.txt").read_bytes().decode()
        }
    )["text"],
}
"""Texts as a review may receive them, each sent back by a browser with its line breaks as a
carriage return and a line feed."""


@pytest.mark.parametrize("text", list(ROUND_TRIPS.values()), ids=list(ROUND_TRIPS))
def test_a_text_sent_back_as_it_came_never_reads_as_changed(
    text: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every page that carries the text sends back the same text: the review, the page asking
    about the name, the check, and the retry page, each to Check and to Save."""

    def refused(*args: object, **kwargs: object) -> None:
        said = "refused"
        raise GradeReportNotSaved(said)

    def sent(page: str) -> dict[str, str]:
        return as_a_browser_sends(sent_from(page))

    with at(open_household(tmp_path)) as browser:
        review = review_page(browser, text)
        asked = browser.post(CHECK, data=sent(review), headers=PAGE)
        checked = browser.post(CHECK, data=as_a_browser_sends(answered(asked.text)), headers=PAGE)
        monkeypatch.setattr(store_of(browser), "save_grade_report", refused)
        retry = browser.post(SAVE, data=sent(checked.text), headers=PAGE)
        monkeypatch.undo()
        rechecked = browser.post(CHECK, data=sent(retry.text), headers=PAGE)
        saved = browser.post(SAVE, data=sent(retry.text), headers=PAGE)

    assert (asked.status_code, checked.status_code, retry.status_code) == (422, 200, 500)
    assert (rechecked.status_code, saved.status_code) == (200, 303), rechecked.text[:300]
    for answer in (asked, checked, retry, rechecked):
        assert escape(grade_routes.TEXT_CHANGED) not in answer.text
        assert sent_from(answer.text)["report_text"].replace("\r\n", "\n") == text.replace(
            "\r\n", "\n"
        )


def test_no_typed_answer_reaches_a_log_or_an_address(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    marked = REPORT.replace("Bramble, Wren", "Bramble, Sentinelwren")
    caplog.set_level(logging.DEBUG)
    with at(open_household(tmp_path)) as browser:
        form = answered(
            review_page(browser, marked),
            class_name="Sentinel Lab",
            setup="other",
            setup_year="2026-2027",
            setup_term="Sentinel",
        )
        checked = browser.post(CHECK, data=form, headers=PAGE)
        saved = browser.post(SAVE, data=form, headers=PAGE)
        outcome = browser.get(saved.headers["location"], headers=PAGE)

    assert (checked.status_code, saved.status_code, outcome.status_code) == (200, 303, 200)
    assert "Sentinel" not in caplog.text
    assert "Sentinel" not in saved.headers["location"]


def test_not_hers_ends_the_import_whatever_else_the_form_holds(tmp_path: pathlib.Path) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = answered(review_page(browser, NO_LINE), identity="not_hers", class_name="")
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(SAVE, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 200
    assert escape(grade_routes.NOT_HERS) in answer.text
    assert after == before


# ------------------------------------------------------------- Grades and class details

SEED_KEY = name_form_key(b"5" * 64)
"""The key the seeded reports' name forms are kept under; the pages read none."""


def draft_of(text: str) -> GradeReportDraft:
    reading = read_grade_report(text)
    assert reading.draft is not None, reading.not_read
    return reading.draft


FIRST_TERM = draft_of(REPORT)
SECOND_TERM = draft_of(REPORT.replace("**T1**", "**T2**"))


def seeded(browser: TestClient, *drafts: GradeReportDraft, month: int | None = 8) -> str:
    """Each report saved through the store, never the routes these tests read, and the class
    they went into."""
    store = store_of(browser)
    for draft in drafts:
        review = store.review_grade_report(draft, capture_key(draft), key=SEED_KEY)
        answers = grade_answers(review, month=month)
        outcome = save_grade(store, draft, key=SEED_KEY, review=review, answers=answers)
        assert isinstance(outcome, GradeReportSaved), outcome
    return capture_class(store, drafts[0])


def grade_tables(settings: Settings) -> dict[str, object]:
    return closed_world([database(settings)], leaving_out=VIEW_TABLES)


def nav_of(page: str) -> str:
    return page.split('<nav class="places"', 1)[1].split("</nav>", 1)[0]


def test_grades_before_any_report_say_so_in_each_voice(tmp_path: pathlib.Path) -> None:
    with at(open_household(tmp_path)) as browser:
        hers = browser.get(HER_GRADES, headers=PAGE)
        theirs = browser.get(GRADES, headers=PAGE)

    assert (hers.status_code, theirs.status_code) == (200, 200)
    assert "No grade reports yet." in words(hers.text)
    assert "Add one to start" not in hers.text
    assert f'href="{ADD}"' not in hers.text
    assert "No grade reports yet. Add one to start." in words(theirs.text)
    assert f'href="{ADD}"' in theirs.text


def test_grades_name_each_class_s_term_grade_and_the_report_that_supplied_it(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        class_id = seeded(browser, FIRST_TERM)
        hers = browser.get(HER_GRADES, headers=PAGE)
        theirs = browser.get(GRADES, headers=PAGE)

    for page, details in ((hers, HER_CLASS_AT), (theirs, CLASS_AT)):
        said = words(page.text)
        assert page.status_code == 200
        assert "Biology" in said
        assert "T1 · 2026-2027" in said
        assert "School-reported grade 81.9% · B-" in said
        assert re.search(r"Report added [A-Z][a-z]+ \d{1,2}\b", said), said
        assert f'href="{details.format(class_id=class_id, n=1)}"' in page.text
        assert not [name for name in ("bramble", "wren") if name in page.text.casefold()]


def test_grades_parent_controls_follow_the_reader_never_the_address(
    tmp_path: pathlib.Path,
) -> None:
    """A parent signed in reads a parent's controls on her address too; she never does. The
    sentence naming the household's current term reads the same for both."""
    with at(signed_in_household(tmp_path), host="testserver") as browser:
        seeded(browser, FIRST_TERM, SECOND_TERM)
        signed_in(browser, THEIRS)
        browser.post(VIEW, data={"view": "2026-2027 T2"}, headers=PAGE)
        theirs = browser.get(HER_GRADES, headers=PAGE)
        browser.post("/sign-out")
        signed_in(browser, HERS)
        browser.post(HER_VIEW, data={"view": "2026-2027 T2"}, headers=PAGE)
        hers = browser.get(HER_GRADES, headers=PAGE)

    showing = "Showing T2 · 2026-2027. Your household's current term in Blossom is T1 · 2026-2027."
    assert showing in words(theirs.text)
    assert showing in words(hers.text)
    assert f'href="{ADD}"' in theirs.text
    assert f'action="{CURRENT_TERM}"' in theirs.text
    assert f'action="{VIEW}"' in theirs.text
    assert "Use T2 · 2026-2027 as the household's current term" in words(theirs.text)
    assert f'href="{ADD}"' not in hers.text
    assert CURRENT_TERM not in hers.text
    assert "Use T2" not in words(hers.text)
    assert f'action="{HER_VIEW}"' in hers.text
    assert "/parent/grades" not in hers.text


def test_a_viewer_s_term_writes_their_choice_alone_and_grades_say_which_is_current(
    tmp_path: pathlib.Path,
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        seeded(browser, FIRST_TERM, SECOND_TERM)
        first = browser.get(HER_GRADES, headers=PAGE)
        chooser = whole_form(first.text, HER_VIEW)
        grades = grade_tables(settings)
        chose = browser.post(HER_VIEW, data={"view": "2026-2027 T2"}, headers=PAGE)
        grades_after = grade_tables(settings)
        showing = browser.get(HER_GRADES, headers=PAGE)
        followed = browser.post(HER_VIEW, data={"view": "current"}, headers=PAGE)
        current = browser.get(HER_GRADES, headers=PAGE)

    assert chooser == {"view": "current"}
    assert re.findall(r'<option value="([^"]*)"', first.text) == [
        "current",
        "2026-2027 T1",
        "2026-2027 T2",
    ]
    assert (chose.status_code, chose.headers["location"]) == (303, HER_GRADES)
    assert grades_after == grades
    assert (
        "Showing T2 · 2026-2027. Your household's current term in Blossom is T1 · 2026-2027."
        in words(showing.text)
    )
    assert '<input type="hidden" name="view" value="current">' in showing.text
    assert "Show the current term" in words(showing.text)
    assert (followed.status_code, followed.headers["location"]) == (303, HER_GRADES)
    assert "current term in Blossom is" not in words(current.text)


@pytest.mark.parametrize("view", ["2026-2027 T9", "2030-2031 T1", "T1", "", "2026-2027"])
def test_a_term_not_on_record_is_refused_and_nothing_is_written(
    view: str, tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        seeded(browser, FIRST_TERM)
        before = closed_world([database(settings)], leaving_out=())
        refused = browser.post(HER_VIEW, data={"view": view}, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert refused.status_code == 422
    assert escape(grade_routes.TERM_NOT_ON_RECORD) in refused.text
    assert after == before


def test_reading_grades_and_class_details_writes_nothing(tmp_path: pathlib.Path) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        class_id = seeded(browser, FIRST_TERM, SECOND_TERM)
        browser.post(HER_VIEW, data={"view": "2026-2027 T2"}, headers=PAGE)
        before = closed_world([database(settings)], leaving_out=())
        pages = [
            browser.get(address, headers=PAGE).status_code
            for address in (
                HER_GRADES,
                GRADES,
                HER_CLASS_AT.format(class_id=class_id, n=1),
                CLASS_AT.format(class_id=class_id, n=2),
            )
        ]
        after = closed_world([database(settings)], leaving_out=())

    assert pages == [200, 200, 200, 200]
    assert after == before


def test_a_parent_s_current_term_leaves_every_remembered_term_as_it_was(
    tmp_path: pathlib.Path,
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        seeded(browser, FIRST_TERM, SECOND_TERM)
        browser.post(VIEW, data={"view": "2026-2027 T2"}, headers=PAGE)
        remembered = as_stored(store_of(browser), "grade_view_choices")
        form = whole_form(browser.get(GRADES, headers=PAGE).text, CURRENT_TERM)
        chosen = browser.post(CURRENT_TERM, data=form, headers=PAGE)
        context = store_of(browser).grade_contexts().current
        remembered_after = as_stored(store_of(browser), "grade_view_choices")
        again = browser.post(CURRENT_TERM, data=form, headers=PAGE)
        before = closed_world([database(settings)], leaving_out=())
        stale = browser.post(
            CURRENT_TERM, data={"shown": "2026-2027 T1", "term": "2026-2027 T1"}, headers=PAGE
        )
        missing = browser.post(
            CURRENT_TERM, data={"shown": "2026-2027 T2", "term": "2026-2027 T9"}, headers=PAGE
        )
        after = closed_world([database(settings)], leaving_out=())

    assert form == {"shown": "2026-2027 T1", "term": "2026-2027 T2"}
    assert (chosen.status_code, chosen.headers["location"]) == (303, GRADES)
    assert context == ("2026-2027", "T2")
    assert remembered_after == remembered
    assert (again.status_code, again.headers["location"]) == (303, GRADES)
    assert stale.status_code == 409
    assert escape(grade_routes.CONTEXT_CHANGED) in stale.text
    assert missing.status_code == 422
    assert escape(grade_routes.TERM_NOT_ON_RECORD) in missing.text
    assert after == before


def test_a_typed_setup_term_with_no_report_shows_no_class_link(tmp_path: pathlib.Path) -> None:
    with at(open_household(tmp_path)) as browser:
        form = answered(
            review_page(browser), setup="other", setup_year="2026-2027", setup_term="Fall"
        )
        assert browser.post(SAVE, data=form, headers=PAGE).status_code == 303
        page = browser.get(GRADES, headers=PAGE)

    assert page.status_code == 200
    assert "No grade reports for Fall · 2026-2027." in words(page.text)
    assert "/terms/" not in page.text
    assert re.findall(r'<option value="([^"]*)"', page.text) == [
        "current",
        "2026-2027 T1",
        "2026-2027 Fall",
    ]


def test_class_details_list_results_by_due_date_with_scores_as_reported(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        class_id = seeded(browser, FIRST_TERM)
        page = browser.get(HER_CLASS_AT.format(class_id=class_id, n=1), headers=PAGE)

    said = words(page.text)
    newest_first = [
        "Osmosis with Potato Slices",
        "Cell Diagram",
        "Microscope Practice",
        "Seed Germination Log",
    ]
    assert page.status_code == 200
    assert "T1 · 2026-2027" in said
    assert "School-reported grade 81.9% · B-" in said
    places = [said.index(title) for title in newest_first]
    assert places == sorted(places)
    assert "School score: 18.0 / 20.0 · 90.0%" in said
    assert "Due September 22" in said
    assert "Gradebook status: Missing" in said
    assert "Date not confirmed" not in said
    assert "School record details" in said
    assert "Penalty: left blank" in said
    assert "Category details" in said
    assert "Labs: weight 25.0, average 83.8" in said
    assert "Quizzes: weight 20.0, average left blank" in said
    assert "no grade reported" not in said
    assert not [name for name in ("bramble", "wren") if name in page.text.casefold()]


def test_class_details_name_each_category_cell_by_its_stored_presence(
    tmp_path: pathlib.Path,
) -> None:
    """A category's weight and average read as written, or by the presence the save stored:
    left blank or not in the copy, each apart and never "no grade reported"."""
    labs = "**Labs** |   | **Weight = 25.0**"
    homework_average = "**Category Average**\n\n|   |\n| - |\n\n**80.0**\n\n"
    draft = draft_of(REPORT.replace(labs, labs.replace("25.0", "")).replace(homework_average, ""))
    tests_ = draft.categories[3].model_copy(update={"weight": GradeNumber.not_captured()})
    draft = draft.model_copy(update={"categories": (*draft.categories[:3], tests_)})
    with at(open_household(tmp_path)) as browser:
        class_id = seeded(browser, draft)
        pages = [
            browser.get(address.format(class_id=class_id, n=1), headers=PAGE)
            for address in (HER_CLASS_AT, CLASS_AT)
        ]

    for page in pages:
        said = words(page.text)
        assert page.status_code == 200
        assert "Homework / Practice: weight 15.0, average not in the copy" in said
        assert "Labs: weight left blank, average 83.8" in said
        assert "Quizzes: weight 20.0, average left blank" in said
        assert "Tests /Projects: weight not in the copy, average left blank" in said
        assert "no grade reported" not in said


@pytest.mark.parametrize(
    ("weight", "average", "line"),
    [
        ((Presence.REPORTED, "25.0"), (Presence.REPORTED, "83.8"), "weight 25.0, average 83.8"),
        ((Presence.REPORTED, "0.0"), (Presence.REPORTED, "0.0"), "weight 0.0, average 0.0"),
        ((Presence.BLANK, ""), (Presence.BLANK, ""), "weight left blank, average left blank"),
        (
            (Presence.UNREADABLE, "TBD"),
            (Presence.UNREADABLE, "EX"),
            "weight couldn't be read: TBD, average couldn't be read: EX",
        ),
        (
            (Presence.NOT_CAPTURED, ""),
            (Presence.NOT_CAPTURED, ""),
            "weight not in the copy, average not in the copy",
        ),
        ((Presence.BLANK, ""), (Presence.REPORTED, "0.0"), "weight left blank, average 0.0"),
    ],
)
def test_a_category_line_keeps_zero_blank_unreadable_and_uncaptured_apart(
    weight: Cell, average: Cell, line: str
) -> None:
    value = CurrentValue(
        cells={"name": (Presence.REPORTED, "Labs"), "weight": weight, "average": average},
        report_id="report",
        order=1,
    )

    assert grade_routes.category_line(value) == f"Labs: {line}"


@pytest.mark.parametrize(
    ("name", "said"),
    [
        ((Presence.BLANK, ""), "Category name left blank"),
        ((Presence.UNREADABLE, "##"), "Category name couldn't be read: ##"),
        ((Presence.NOT_CAPTURED, ""), "Category name not in the copy"),
    ],
)
def test_a_category_name_not_reported_reads_as_the_review_says_it(name: Cell, said: str) -> None:
    value = CurrentValue(
        cells={
            "name": name,
            "weight": (Presence.REPORTED, "25.0"),
            "average": (Presence.REPORTED, "83.8"),
        },
        report_id="report",
        order=1,
    )

    assert grade_routes.category_line(value) == f"{said}: weight 25.0, average 83.8"


def test_class_details_say_category_name_left_blank(tmp_path: pathlib.Path) -> None:
    labs = FIRST_TERM.categories[1].model_copy(update={"name": GradeValue.read("")})
    draft = FIRST_TERM.model_copy(
        update={"categories": (FIRST_TERM.categories[0], labs, *FIRST_TERM.categories[2:])}
    )
    with at(open_household(tmp_path)) as browser:
        class_id = seeded(browser, draft)
        pages = [
            browser.get(address.format(class_id=class_id, n=1), headers=PAGE)
            for address in (HER_CLASS_AT, CLASS_AT)
        ]

    for page in pages:
        said = words(page.text)
        assert page.status_code == 200
        assert "Category name left blank: weight 25.0, average 83.8" in said
        assert "Name left blank" not in said


TERM_CELLS: Final[dict[str, dict[Presence, tuple[Cell, str]]]] = {
    "percent": {
        Presence.REPORTED: ((Presence.REPORTED, "81.9"), "81.9%"),
        Presence.BLANK: ((Presence.BLANK, ""), "percent left blank"),
        Presence.UNREADABLE: ((Presence.UNREADABLE, "8l.9"), "percent couldn't be read: 8l.9"),
        Presence.NOT_CAPTURED: ((Presence.NOT_CAPTURED, ""), "percent not in the copy"),
    },
    "letter": {
        Presence.REPORTED: ((Presence.REPORTED, "B-"), "B-"),
        Presence.BLANK: ((Presence.BLANK, ""), "letter grade left blank"),
        Presence.UNREADABLE: ((Presence.UNREADABLE, "A?"), "letter grade couldn't be read: A?"),
        Presence.NOT_CAPTURED: ((Presence.NOT_CAPTURED, ""), "letter grade not in the copy"),
    },
}
"""Each term cell in each presence, and the words Grades and class details say for it."""
SCORE_CELLS: Final[dict[str, dict[Presence, tuple[Cell, str]]]] = {
    "points": {
        Presence.REPORTED: ((Presence.REPORTED, "18.0"), "18.0"),
        Presence.BLANK: ((Presence.BLANK, ""), "blank"),
        Presence.UNREADABLE: ((Presence.UNREADABLE, "EX"), "couldn't be read: EX"),
        Presence.NOT_CAPTURED: ((Presence.NOT_CAPTURED, ""), "not in the copy"),
    },
    "max_points": {
        Presence.REPORTED: ((Presence.REPORTED, "20.0"), "20.0"),
        Presence.BLANK: ((Presence.BLANK, ""), "maximum left blank"),
        Presence.UNREADABLE: ((Presence.UNREADABLE, "TBD"), "maximum couldn't be read: TBD"),
        Presence.NOT_CAPTURED: ((Presence.NOT_CAPTURED, ""), "maximum not in the copy"),
    },
    "average": {
        Presence.REPORTED: ((Presence.REPORTED, "90.0"), "90.0%"),
        Presence.BLANK: ((Presence.BLANK, ""), "average left blank"),
        Presence.UNREADABLE: ((Presence.UNREADABLE, "X9"), "average couldn't be read: X9"),
        Presence.NOT_CAPTURED: ((Presence.NOT_CAPTURED, ""), "average not in the copy"),
    },
}
"""Each score cell in each presence, and the words class details say for it."""
SAVED_PRESENCES: Final = (Presence.REPORTED, Presence.BLANK, Presence.NOT_CAPTURED)
"""The presences a saved cell holds: a value with a cell that couldn't be read is never offered
to a save."""


def term_said(percent: Presence, letter: Presence) -> str:
    """The term grade line for a percent and a letter in these presences."""
    if {percent, letter} == {Presence.BLANK}:
        return "Term grade left blank"
    if {percent, letter} == {Presence.NOT_CAPTURED}:
        return "Term grade not in the copy"
    shown = (TERM_CELLS["percent"][percent][1], TERM_CELLS["letter"][letter][1])
    return "School-reported grade " + " · ".join(shown)


def score_said(points: Presence, most: Presence, average: Presence) -> str:
    """The score line for points, a maximum and an average in these presences."""
    if {points, most, average} == {Presence.BLANK}:
        return "Score left blank"
    if {points, most, average} == {Presence.NOT_CAPTURED}:
        return "Score not in the copy"
    shown = (
        SCORE_CELLS[field][presence][1]
        for field, presence in (("points", points), ("max_points", most), ("average", average))
    )
    return "School score: {} / {} · {}".format(*shown)


def as_number(cell: Cell) -> GradeNumber:
    return GradeNumber(text=cell[1], presence=cell[0])


@pytest.mark.parametrize("percent", list(Presence))
@pytest.mark.parametrize("letter", list(Presence))
def test_a_term_grade_line_says_each_cell_as_written_or_by_its_presence(
    percent: Presence, letter: Presence
) -> None:
    """No cell of the term grade is dropped for another's presence: the percent and the letter
    each read as written, or as the review says a cell that wasn't reported."""
    value = CurrentValue(
        cells={
            "percent": TERM_CELLS["percent"][percent][0],
            "letter": TERM_CELLS["letter"][letter][0],
        },
        report_id="report",
        order=1,
    )

    assert grade_routes.term_grade(value) == term_said(percent, letter)


@pytest.mark.parametrize("points", list(Presence))
@pytest.mark.parametrize("most", list(Presence))
@pytest.mark.parametrize("average", list(Presence))
def test_a_score_line_says_each_cell_as_written_or_by_its_presence(
    points: Presence, most: Presence, average: Presence
) -> None:
    """No cell of a score is dropped for another's presence: the points, the maximum and the
    average each read as written, or as the review says a cell that wasn't reported."""
    value = CurrentValue(
        cells={
            "points": SCORE_CELLS["points"][points][0],
            "max_points": SCORE_CELLS["max_points"][most][0],
            "average": SCORE_CELLS["average"][average][0],
        },
        report_id="report",
        order=1,
    )

    assert grade_routes.score_of(value) == score_said(points, most, average)


@pytest.mark.parametrize(
    ("percent", "letter"),
    [
        (percent, letter)
        for percent in SAVED_PRESENCES
        for letter in SAVED_PRESENCES
        if {percent, letter} != {Presence.NOT_CAPTURED}
    ],
)
def test_grades_and_class_details_show_each_term_cell_the_save_kept(
    percent: Presence, letter: Presence, tmp_path: pathlib.Path
) -> None:
    (percent_cell, _), (letter_cell, _) = (
        TERM_CELLS["percent"][percent],
        TERM_CELLS["letter"][letter],
    )
    term = TermResult(
        percent=as_number(percent_cell),
        letter=GradeValue(text=letter_cell[1], presence=letter_cell[0]),
    )
    with at(open_household(tmp_path)) as browser:
        class_id = seeded(browser, FIRST_TERM.model_copy(update={"term": term}))
        grades = [
            words(browser.get(address, headers=PAGE).text) for address in (HER_GRADES, GRADES)
        ]
        details = [
            words(browser.get(address.format(class_id=class_id, n=1), headers=PAGE).text)
            for address in (HER_CLASS_AT, CLASS_AT)
        ]

    line = term_said(percent, letter)
    for said in grades:
        assert f"Biology {line} Report added" in said, said
    for said in details:
        assert f"T1 · 2026-2027 {line} Report added" in said, said


def test_class_details_show_each_score_cell_the_save_kept(tmp_path: pathlib.Path) -> None:
    states = [
        (points, most, average)
        for points in SAVED_PRESENCES
        for most in SAVED_PRESENCES
        for average in SAVED_PRESENCES
    ]
    homework = FIRST_TERM.categories[0]
    rows = tuple(
        homework.rows[0].model_copy(
            update={
                "assignment": GradeValue.read(f"Log {n:02}"),
                "points": as_number(SCORE_CELLS["points"][points][0]),
                "max_points": as_number(SCORE_CELLS["max_points"][most][0]),
                "average": as_number(SCORE_CELLS["average"][average][0]),
            }
        )
        for n, (points, most, average) in enumerate(states, start=1)
    )
    draft = FIRST_TERM.model_copy(
        update={
            "categories": (homework.model_copy(update={"rows": rows}), *FIRST_TERM.categories[1:])
        }
    )
    with at(open_household(tmp_path)) as browser:
        class_id = seeded(browser, draft)
        pages = [
            words(browser.get(address.format(class_id=class_id, n=1), headers=PAGE).text)
            for address in (HER_CLASS_AT, CLASS_AT)
        ]

    for said in pages:
        for n, (points, most, average) in enumerate(states, start=1):
            line = score_said(points, most, average)
            assert f"Log {n:02} Homework / Practice {line} Due September 22" in said, (n, said)


STATUS_CELLS: Final[dict[Presence, tuple[Cell, str]]] = {
    Presence.REPORTED: ((Presence.REPORTED, "Missing"), "Gradebook status: Missing"),
    Presence.BLANK: ((Presence.BLANK, ""), "Gradebook status left blank"),
    Presence.UNREADABLE: ((Presence.UNREADABLE, "M?"), "Gradebook status couldn't be read: M?"),
    Presence.NOT_CAPTURED: ((Presence.NOT_CAPTURED, ""), "Gradebook status not in the copy"),
}
"""The school's status for a result in each presence, and the line class details say for it."""


@pytest.mark.parametrize("presence", list(Presence))
def test_a_result_s_status_line_says_the_status_as_written_or_by_its_presence(
    presence: Presence,
) -> None:
    """Every result has a status line: the status as written, or as the review says a cell that
    wasn't reported."""
    cells: dict[str, Cell] = dict.fromkeys(grade_review.RESULT_FIELDS, (Presence.BLANK, ""))
    cells["assignment"] = (Presence.REPORTED, "Cell Diagram")
    cells["status"] = STATUS_CELLS[presence][0]
    value = CurrentValue(cells=cells, report_id="report", order=1)

    _, shown = grade_routes.result_shown(
        value, year="2026-2027", first_month=8, newest=None, names={}
    )

    assert shown.status == STATUS_CELLS[presence][1]


def test_class_details_say_a_status_left_blank_or_not_in_the_copy(tmp_path: pathlib.Path) -> None:
    """A Status cell left blank, and a copy whose Labs table has no Status column, each say so
    on the result's own card; a reported status reads as written."""
    blank = REPORT.replace("| Valid      | 09/22", "|            | 09/22", 1)
    text = without_columns(blank, "Labs", "Status")
    with at(open_household(tmp_path)) as browser:
        class_id = seeded(browser, draft_of(text))
        pages = [
            words(browser.get(address.format(class_id=class_id, n=1), headers=PAGE).text)
            for address in (HER_CLASS_AT, CLASS_AT)
        ]

    cards = [
        "Osmosis with Potato Slices Labs School score: 31.0 / 40.0 · 77.5% Due October 2"
        " Gradebook status not in the copy",
        "Cell Diagram Homework / Practice School score: 7.0 / 10.0 · 70.0% Due September 26"
        " Gradebook status: Missing",
        "Microscope Practice Labs School score: 27.0 / 30.0 · 90.0% Due September 24"
        " Gradebook status not in the copy",
        "Seed Germination Log Homework / Practice School score: 18.0 / 20.0 · 90.0%"
        " Due September 22 Gradebook status left blank",
    ]
    for said in pages:
        for card in cards:
            assert card in said, (card, said)


def test_a_newer_report_that_repeats_the_term_grade_leaves_it_named_by_its_supplier(
    tmp_path: pathlib.Path,
) -> None:
    """A newer capture that shows the same term grade records no new one, so the grade stays
    named by the report that supplied it, and no line claims the newer report lacked it."""
    seven = "| Cell Diagram             | 7.0 "
    newer = draft_of(REPORT.replace(seven, seven.replace("7.0", "8.0")))
    with at(open_household(tmp_path)) as browser:
        class_id = seeded(browser, FIRST_TERM, newer)
        grades = words(browser.get(HER_GRADES, headers=PAGE).text)
        details = words(browser.get(HER_CLASS_AT.format(class_id=class_id, n=1), headers=PAGE).text)

    for said in (grades, details):
        assert "School-reported grade 81.9% · B-" in said
        assert re.search(r"Report added [A-Z][a-z]+ \d{1,2} ", said), said
        assert "not shown" not in said.casefold()
    assert re.search(r"Second report added [A-Z][a-z]+ \d{1,2}: Current", details), details
    assert "School score: 8.0 / 10.0" in details


def saves_between_reads(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Each public call to the household's store followed, once it returns, by a real save of
    the same report with a new term grade; the term grades saved, in order, fill the list."""
    store = store_of(browser)
    saved: list[str] = []
    saving = False

    def after(method: Callable[..., object]) -> Callable[..., object]:
        def call(*args: object, **kwargs: object) -> object:
            nonlocal saving
            answer = method(*args, **kwargs)
            if not saving:
                saving = True
                try:
                    grade = f"{82 + len(saved)}.9"
                    draft = draft_of(REPORT.replace("**81.9**", f"**{grade}**"))
                    outcome = save_grade(store, draft, key=SEED_KEY)
                    assert isinstance(outcome, GradeReportSaved), outcome
                    saved.append(grade)
                finally:
                    saving = False
            return answer

        return call

    for name in dir(type(store)):
        if not name.startswith("_") and inspect.isfunction(getattr(type(store), name)):
            monkeypatch.setattr(store, name, after(getattr(store, name)))
    return saved


def supplier_line(grade: str, saved: list[str]) -> str:
    """The line naming the report that supplied ``grade``: the seeded report, or the save that
    wrote it, by its place among the reports added that day."""
    place = 0 if grade == "81.9" else saved.index(grade) + 1
    return f"{grade_routes.ordinal(place + 1) + ' ' if place else ''}report added".capitalize()


@pytest.mark.parametrize(("grades", "details"), [(HER_GRADES, HER_CLASS_AT), (GRADES, CLASS_AT)])
def test_a_save_between_a_page_s_reads_leaves_each_value_named_by_its_supplier(
    grades: str, details: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save that commits after any of the page's reads, between the reports and the values
    among them, never leaves a value named by a report the page didn't read with it."""
    pages = []
    for place, address in enumerate((grades, details)):
        (tmp_path / str(place)).mkdir()
        with at(open_household(tmp_path / str(place))) as browser:
            class_id = seeded(browser, FIRST_TERM)
            saved = saves_between_reads(browser, monkeypatch)
            page = browser.get(address.format(class_id=class_id, n=1), headers=PAGE)
            monkeypatch.undo()
        pages.append((page, saved))

    for page, saved in pages:
        said = words(page.text)
        shown = re.search(r"School-reported grade (\d+\.\d)% · B- ", said)
        assert page.status_code == 200
        assert saved, "no save landed during the page's reads"
        assert shown is not None, said
        line = supplier_line(shown[1], saved)
        assert f"School-reported grade {shown[1]}% · B- {line} " in said, said
        assert not re.search(r"\b(from|in) the report\b(?! added)", said), said
    details_said = words(pages[1][0].text)
    shown = re.search(r"School-reported grade (\d+\.\d)% · B- ", details_said)
    assert shown is not None
    line = supplier_line(shown[1], pages[1][1])
    assert re.search(rf"{line} [A-Z][a-z]+ \d{{1,2}}: Current", details_said), details_said


@pytest.mark.parametrize(
    ("count", "said"),
    [
        (1, "first"),
        (2, "second"),
        (10, "tenth"),
        (11, "eleventh"),
        (12, "twelfth"),
        (19, "nineteenth"),
        (20, "twentieth"),
        (21, "twenty-first"),
        (99, "ninety-ninth"),
        (100, "one hundredth"),
        (101, "one hundred first"),
        (111, "one hundred eleventh"),
    ],
)
def test_an_ordinal_is_spelled_for_any_positive_count(count: int, said: str) -> None:
    assert grade_routes.ordinal(count) == said


def test_an_ordinal_has_no_place_below_one() -> None:
    with pytest.raises(ValueError, match="positive"):
        grade_routes.ordinal(0)


SAME_DAY: Final = {10: "tenth", 11: "eleventh", 12: "twelfth", 21: "twenty-first"}
"""Counts of reports added the same day, and the place word of the last of them."""


def same_day_reports(count: int, *, acted_from: int | None = None) -> tuple[ClassReport, ...]:
    """``count`` reports imported on October 9, each its own capture, and with ``acted_from`` a
    report made current on October 10 from that report's capture, which is then its newest."""
    made = "report-made"
    reports = [
        ClassReport(
            report_id=f"report-{n}",
            order=n,
            use="current",
            imported_at="2026-10-09T16:00:00+00:00",
            acted_at=None,
            latest_of_capture=made if n == acted_from else f"report-{n}",
        )
        for n in range(1, count + 1)
    ]
    if acted_from is not None:
        reports.append(
            ClassReport(
                report_id=made,
                order=count + 1,
                use="current",
                imported_at="2026-10-10T16:00:00+00:00",
                acted_at="2026-10-10T16:00:00+00:00",
                latest_of_capture=made,
            )
        )
    return tuple(reports)


@pytest.mark.parametrize("count", list(SAME_DAY))
def test_each_report_added_the_same_day_is_named_by_its_spelled_place(count: int) -> None:
    names = grade_routes.report_names(same_day_reports(count), ZoneInfo(FIXTURE_TIMEZONE))

    assert names["report-1"] == "report added October 9"
    assert names["report-2"] == "second report added October 9"
    assert names[f"report-{count}"] == f"{SAME_DAY[count]} report added October 9"
    assert not any(re.search(r"\breport \d", name) for name in names.values()), names


def test_a_report_made_current_from_the_eleventh_capture_keeps_its_place_word() -> None:
    names = grade_routes.report_names(
        same_day_reports(12, acted_from=11), ZoneInfo(FIXTURE_TIMEZONE)
    )

    assert names["report-11"] == "eleventh report added October 9"
    assert names["report-made"] == "eleventh report added October 9, made current October 10"
    assert grade_routes.as_line(names["report-11"]) == "Eleventh report added October 9"
    assert grade_routes.as_line(names["report-made"]) == (
        "Eleventh report added October 9 · Made current October 10"
    )


def same_day_drafts(count: int) -> list[GradeReportDraft]:
    """``count`` copies of the report, each its own capture by its term grade."""
    return [draft_of(REPORT.replace("**81.9**", f"**{60 + n}.9**")) for n in range(1, count + 1)]


@pytest.mark.parametrize("count", list(SAME_DAY))
def test_class_details_name_the_last_of_many_same_day_reports_by_its_spelled_place(
    count: int, tmp_path: pathlib.Path
) -> None:
    with at(open_household(tmp_path)) as browser:
        class_id = seeded(browser, *same_day_drafts(count))
        page = browser.get(CLASS_AT.format(class_id=class_id, n=1), headers=PAGE)

    said = words(page.text)
    place = SAME_DAY[count].capitalize()
    assert page.status_code == 200
    assert f"School-reported grade {60 + count}.9% · B- {place} report added August 19" in said
    assert f"{place} report added August 19: Current" in said, said
    assert not re.search(r"\b[Rr]eport \d", said), said


def test_class_details_name_a_report_made_current_from_the_eleventh_capture(
    tmp_path: pathlib.Path,
) -> None:
    drafts = same_day_drafts(12)
    with at(open_household(tmp_path)) as browser:
        class_id = seeded(browser, *drafts)
        made = confirm_current(store_of(browser), drafts[10])
        assert isinstance(made, MadeCurrent), made
        page = browser.get(CLASS_AT.format(class_id=class_id, n=1), headers=PAGE)

    said = words(page.text)
    line = "Eleventh report added August 19 · Made current August 19"
    assert page.status_code == 200
    assert f"School-reported grade 71.9% · B- {line}" in said, said
    assert "Eleventh report added August 19: Current" in said, said
    assert not re.search(r"\b[Rr]eport \d", said), said


def test_class_details_without_a_confirmed_month_keep_dates_as_written(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        class_id = seeded(browser, FIRST_TERM, month=None)
        page = browser.get(CLASS_AT.format(class_id=class_id, n=1), headers=PAGE)

    said = words(page.text)
    assert page.status_code == 200
    assert "Date not confirmed" in said
    assert "Due date shown: 09/22" in said
    assert "Due September" not in said


def test_class_details_take_their_term_from_the_address_never_the_selection(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        class_id = seeded(browser, FIRST_TERM, SECOND_TERM)
        browser.post(HER_VIEW, data={"view": "2026-2027 T2"}, headers=PAGE)
        first = browser.get(HER_CLASS_AT.format(class_id=class_id, n=1), headers=PAGE)
        second = browser.get(HER_CLASS_AT.format(class_id=class_id, n=2), headers=PAGE)

    assert "T1 · 2026-2027" in words(first.text)
    assert "T2 · 2026-2027" not in words(first.text)
    assert "T2 · 2026-2027" in words(second.text)


@pytest.mark.parametrize(
    "address",
    [
        "/student/grades/classes/class-unknown/terms/1",
        "/student/grades/classes/{class_id}/terms/2",
        "/student/grades/classes/{class_id}/terms/0",
        "/student/grades/classes/{class_id}/terms/one",
        "/student/grades/classes/{class_id}/terms/01",
        "/parent/grades/classes/class-unknown/terms/1",
        "/parent/grades/classes/{class_id}/terms/2",
    ],
)
def test_a_class_or_term_position_not_on_record_is_not_found(
    address: str, tmp_path: pathlib.Path
) -> None:
    with at(open_household(tmp_path)) as browser:
        class_id = seeded(browser, FIRST_TERM)
        page = browser.get(address.format(class_id=class_id), headers=PAGE)

    assert page.status_code == 404
    assert "Blossom has no record for this class and term." in words(page.text)


def test_grades_is_in_the_masthead_for_whoever_reads(tmp_path: pathlib.Path) -> None:
    with at(open_household(tmp_path)) as browser:
        her_week = browser.get("/student/due-this-week", headers=PAGE)
        review = browser.get("/parent", headers=PAGE)
        hers = browser.get(HER_GRADES, headers=PAGE)
        theirs = browser.get(GRADES, headers=PAGE)
    (tmp_path / "on").mkdir()
    with at(signed_in_household(tmp_path / "on"), host="testserver") as browser:
        signed_in(browser, THEIRS)
        parent_on_her_week = browser.get("/student/due-this-week", headers=PAGE)

    assert '<a href="/student/grades">Grades</a>' in nav_of(her_week.text)
    assert '<a href="/parent/grades">Grades</a>' in nav_of(review.text)
    assert '<a href="/student/grades" aria-current="page">Grades</a>' in nav_of(hers.text)
    assert ">My week</a>" in nav_of(hers.text)
    assert '<a href="/parent/grades" aria-current="page">Grades</a>' in nav_of(theirs.text)
    assert ">Student week</a>" in nav_of(theirs.text)
    assert '<a href="/parent/grades">Grades</a>' in nav_of(parent_on_her_week.text)


def family_grade_pages(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Each family grade page as a parent reaches it: the paste, the review, a check, a save
    the store refuses, a save's outcome, Grades and class details."""
    pages = {"add": browser.get(ADD, headers=PAGE)}
    pages["review"] = browser.post(REVIEW, data={"report_text": REPORT}, headers=PAGE)
    form = answered(pages["review"].text)
    pages["check"] = browser.post(CHECK, data=form, headers=PAGE)

    def refused(*args: object, **kwargs: object) -> None:
        said = "refused"
        raise GradeReportNotSaved(said)

    monkeypatch.setattr(store_of(browser), "save_grade_report", refused)
    pages["retry"] = browser.post(SAVE, data=form, headers=PAGE)
    monkeypatch.undo()
    saved = browser.post(SAVE, data=form, headers=PAGE)
    assert saved.status_code == 303, saved.text
    pages["saved"] = browser.get(saved.headers["location"], headers=PAGE)
    pages["grades"] = browser.get(GRADES, headers=PAGE)
    class_id = capture_class(store_of(browser), FIRST_TERM)
    pages["class"] = browser.get(CLASS_AT.format(class_id=class_id, n=1), headers=PAGE)
    assert {name: page.status_code for name, page in pages.items()} == {
        "add": 200,
        "review": 200,
        "check": 200,
        "retry": 500,
        "saved": 200,
        "grades": 200,
        "class": 200,
    }
    return {name: page.text for name, page in pages.items()}


def marks_grades_in_a_parent_s_voice(page: str) -> bool:
    """Whether the masthead marks Grades alone as the current place, in a parent's voice."""
    nav = nav_of(page)
    return (
        '<a href="/parent/grades" aria-current="page">Grades</a>' in nav
        and '<a href="/parent">Family review</a>' in nav
        and nav.count("aria-current") == 1
        and ">Student week</a>" in nav
        and ">My week</a>" not in nav
    )


def test_every_family_grade_page_marks_grades_not_family_review_with_sign_in_off(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        pages = family_grade_pages(browser, monkeypatch)
    with at(settings, client="192.0.2.10") as browser:
        pages["refused"] = browser.get(ADD, headers=PAGE).text

    assert escape(grade_routes.ONLY_HERE) in pages["refused"]
    unmarked = [name for name, page in pages.items() if not marks_grades_in_a_parent_s_voice(page)]
    assert unmarked == []


def test_every_family_grade_page_marks_grades_not_family_review_for_a_parent_signed_in(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with at(signed_in_household(tmp_path), host="testserver") as browser:
        signed_in(browser, THEIRS)
        pages = family_grade_pages(browser, monkeypatch)

    unmarked = [name for name, page in pages.items() if not marks_grades_in_a_parent_s_voice(page)]
    assert unmarked == []


def test_her_grade_pages_mark_grades_in_each_sign_in_mode(tmp_path: pathlib.Path) -> None:
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    with at(open_household(tmp_path / "off")) as browser:
        class_id = seeded(browser, FIRST_TERM)
        off = [
            browser.get(address, headers=PAGE).text
            for address in (HER_GRADES, HER_CLASS_AT.format(class_id=class_id, n=1))
        ]
    with at(signed_in_household(tmp_path / "on"), host="testserver") as browser:
        class_id = seeded(browser, FIRST_TERM)
        signed_in(browser, HERS)
        on = [
            browser.get(address, headers=PAGE).text
            for address in (HER_GRADES, HER_CLASS_AT.format(class_id=class_id, n=1))
        ]

    for page in off:
        nav = nav_of(page)
        assert '<a href="/student/grades" aria-current="page">Grades</a>' in nav
        assert '<a href="/parent">Family review</a>' in nav
        assert nav.count("aria-current") == 1
        assert ">My week</a>" in nav
    for page in on:
        nav = nav_of(page)
        assert '<a href="/student/grades" aria-current="page">Grades</a>' in nav
        assert "Family review" not in nav
        assert nav.count("aria-current") == 1
        assert ">My week</a>" in nav


# ------------------------------------------------------------- the review form's grammar

OTHER_CLASS = REPORT.replace("**07 BIO - C**", "**07 CHEM - A**")
"""Wren's report under another class's name, so the review offers her saved class."""
RETITLED = REPORT.replace("| Seed Germination Log |", "| Zebra Field Notes |").replace(
    "| Cell Diagram             |", "| Yarrow Pressing          |"
)
"""Wren's report with its first two rows retitled: each asks whether it is a saved result."""
UNRELATED = REPORT.replace(
    "| Cell Diagram             | 7.0     | 10.0    | 70.0    | Missing    | 09/26   |",
    "| Yarrow Pressing          | 7.0     | 15.0    | 70.0    | Missing    | 10/09   |",
)
"""Wren's report with a row no saved result may be: it offers the free results alone."""
IXL = REPORT.replace(
    "| Seed Germination Log | 18.0    | 20.0    | 90.0    | Valid      | 09/22   |",
    "| IXL Practice         | 9.0     | 10.0    | 90.0    | Valid      | 09/22   |",
).replace(
    "| Cell Diagram             | 7.0     | 10.0    | 70.0    | Missing    | 09/26   |",
    "| IXL Practice             | 8.0     | 10.0    | 80.0    | Valid      | 09/22   |",
)
"""Wren's report with two practice rows that share a title and a due date."""
IXL_AWAY = IXL.replace("| IXL Practice         |", "| Zebra Field Notes    |").replace(
    "| IXL Practice             |", "| Yarrow Pressing          |"
)
"""The practice rows retitled away: each asks which of the two saved results it is."""
OTHER_DIGITS = (
    "\N{ARABIC-INDIC DIGIT THREE}",
    "\N{SUPERSCRIPT TWO}",
    "\N{FULLWIDTH DIGIT THREE}",
)
MAY_BE_EMPTY = frozenset({"first_month", "choose"})
"""The page fields whose grammar takes an empty value."""


def first_saved(browser: TestClient, text: str = REPORT) -> None:
    """``text`` saved whole, every result ticked."""
    saved = browser.post(SAVE, data=answered(review_page(browser, text)), headers=PAGE)
    assert saved.status_code == 303, saved.text


def as_sent(page: str) -> dict[str, str]:
    """A review's form as sent with nothing changed, its first answer about the name chosen
    where the page asks one."""
    form = sent_from(page)
    asked = [value for _, name, value in form_values(page, SAVE) if name == "identity"]
    form.setdefault("identity", asked[0])
    return form


def positions_on(page: str) -> grade_routes.Positions:
    """The positions of the draft a page's form carries."""
    draft = read_grade_report(sent_from(page)["report_text"]).draft
    assert draft is not None
    return grade_routes.positions_of(draft)


def marked(page: str) -> set[int]:
    """The rows a returned review marks for the parent to resolve."""
    rows = {int(found) for found in re.findall(r'id="item-(\d+)-problem"', page)}
    for row in rows:
        assert f'aria-describedby="item-{row}-problem"' in page
    return rows


def decision_of(settings: Settings, title: str) -> tuple[str, str | None]:
    """How the newest report's row titled ``title`` resolved, and the result it names."""
    with sqlite3.connect(f"file:{database(settings)}?mode=ro", uri=True) as db:
        found = db.execute(
            "SELECT d.how, d.result_id FROM grade_match_decisions AS d "
            "JOIN grade_reports AS r ON r.report_id = d.report_id "
            "WHERE d.row_key LIKE ? ORDER BY r.acceptance_order DESC LIMIT 1",
            (f"%{title}%",),
        ).fetchone()
    assert found is not None, title
    return str(found[0]), None if found[1] is None else str(found[1])


def results_on_record(settings: Settings) -> int:
    with sqlite3.connect(f"file:{database(settings)}?mode=ro", uri=True) as db:
        return int(db.execute("SELECT COUNT(*) FROM grade_results").fetchone()[0])


def every_review_state(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Each state the review's form renders in, by name, from two households of Wren's."""
    pages = {}
    with at(open_household(tmp_path / "one")) as browser:
        pages["first use"] = review_page(browser)
        pages["no student line"] = review_page(browser, NO_LINE)
        first_saved(browser)
        pages["class matched, holding data"] = review_page(browser)
        pages["another class"] = review_page(browser, OTHER_CLASS)
        pages["choices alone"] = review_page(browser, UNRELATED)
        renamed = review_page(browser, RETITLED)
        pages["renamed rows"] = renamed
        form = as_sent(renamed)
        checked = browser.post(CHECK, data={**form, "match.5": form["candidates.5"]}, headers=PAGE)
        assert checked.status_code == 200, checked.text
        pages["checked"] = checked.text
        refused = browser.post(CHECK, data={**form, "match.5": "choose"}, headers=PAGE)
        assert refused.status_code == 422, refused.text
        pages["refused"] = refused.text
        different = {**form, "match.5": form["candidates.5"], "match.6": "different"}
        assert browser.post(SAVE, data=different, headers=PAGE).status_code == 303
        pages["remembered different"] = review_page(browser, RETITLED)

        def refuse(*args: object, **kwargs: object) -> None:
            said = "refused"
            raise GradeReportNotSaved(said)

        monkeypatch.setattr(store_of(browser), "save_grade_report", refuse)
        retry = browser.post(SAVE, data=as_sent(review_page(browser, OTHER_CLASS)), headers=PAGE)
        assert retry.status_code == 500, retry.text
        pages["retry"] = retry.text
    with at(open_household(tmp_path / "two")) as browser:
        first_saved(browser, IXL)
        pages["which question"] = review_page(browser, IXL_AWAY)
    return pages


def test_every_control_of_every_review_state_has_its_row_of_the_grammar(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every name each state renders has one row of the form's table, each value a page-owned
    control may send fits its grammar alone and beside what the page sends, and exactly the
    text inputs and text areas are the parent's own."""
    rendered: set[str] = set()
    for state, page in every_review_state(tmp_path, monkeypatch).items():
        positions = positions_on(page)
        sent = as_sent(page)
        grade_routes.page_values(sent, positions)
        for kind, name, value in form_values(page, SAVE):
            field = grade_routes.FORM.get(name.split(".")[0])
            assert field is not None, (state, name)
            rendered.add(name.split(".")[0])
            if state != "retry":
                typed = kind in ("text", "textarea")
                assert (field.owner == "typed") == typed, (state, name, kind)
            if field.owner == "page":
                assert field.value is not None, name
                assert field.value.fullmatch(value), (state, name, value)
                grade_routes.page_values({**sent, name: value}, positions)

    assert rendered == set(grade_routes.FORM)


def test_every_name_the_form_may_send_comes_from_the_table(tmp_path: pathlib.Path) -> None:
    positions = positions_on(reviewed(tmp_path))

    always = {name for name, field in grade_routes.FORM.items() if field.where == "always"}
    assert always == {
        "report_text",
        "acceptance_id",
        "revision",
        "source_key",
        "text_key",
        "identity_form",
    }
    assert {name.split(".")[0] for name in positions.names} == set(grade_routes.FORM)
    assert "select.8" in positions.names
    assert "match.4" not in positions.names
    assert "match.5" in positions.names


def test_every_id_and_key_the_store_mints_fits_its_pattern(tmp_path: pathlib.Path) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        first_saved(browser)
        key = browser.app.state.grade_name_key  # type: ignore[attr-defined]
    with sqlite3.connect(f"file:{database(settings)}?mode=ro", uri=True) as db:
        classes = [found for (found,) in db.execute("SELECT class_id FROM grade_classes")]
        results = [found for (found,) in db.execute("SELECT result_id FROM grade_results")]
        accepted = [found for (found,) in db.execute("SELECT acceptance_id FROM grade_acceptances")]
    draft = read_grade_report(REPORT).draft
    assert draft is not None

    assert classes
    assert results
    assert accepted
    assert all(gradebook.CLASS_ID.fullmatch(found) for found in classes)
    assert all(gradebook.RESULT_ID.fullmatch(found) for found in results)
    assert all(gradebook.ACCEPTANCE_ID.fullmatch(found) for found in accepted)
    assert gradebook.ACCEPTANCE_ID.fullmatch(gradebook.new_acceptance_id())
    assert grade_drafts.HEX_KEY.fullmatch(capture_key(draft))
    assert grade_drafts.HEX_KEY.fullmatch(name_form(key, "Bramble, Wren"))
    assert grade_drafts.HEX_KEY.fullmatch(grade_routes.text_key(REPORT))
    assert vars(grade_routes)["ACCEPTANCE_ID"] is gradebook.ACCEPTANCE_ID


def test_the_text_key_reads_line_breaks_alike_and_every_other_character_as_sent() -> None:
    keyed = grade_routes.text_key

    assert keyed("a\r\nb\rc\n") == keyed("a\nb\nc\n") == keyed("a\r\nb\r\nc\r\n")
    assert keyed("\na") != keyed("a") != keyed("a ") != keyed("a\t")
    assert keyed("a\n\nb") != keyed("a\nb")
    assert keyed("&lt;") != keyed("<")
    assert keyed("a\x00b") == keyed("a\ufffdb")


def malformed(field: str, value: str) -> list[str | None]:
    """Values outside ``field``'s grammar, each made from ``value``, one the page rendered:
    another script's digit added or put in, one more character, its case, its prefix or
    separator changed, a leading zero, a list's space doubled or a tab, a token repeated, and
    empty. None stands for the field left out."""
    samples: list[str | None] = [value + digit for digit in OTHER_DIGITS]
    samples += [re.sub(r"[0-9]", OTHER_DIGITS[0], value, count=1), value + "a", value.upper()]
    samples += [value.replace("-", "_", 1), value.replace(":", ""), "0" + value]
    samples += [f"{value} {value}", f"{value}  {value}", f"{value}\t{value}"]
    if field not in MAY_BE_EMPTY:
        samples.append("")
    if field == "revision":
        samples += ["0", "9" * 19, "9" * 20, "9" * 5000]
    if field == "class":
        class_id = value.rpartition(":")[0]
        samples += [f"{class_id}:{digits}" for digits in ("0", "07", "9" * 19, "9" * 5000)]
        samples += [f"k{value}", f"{class_id}a:none"]
    if field in ("candidates", "choices"):
        samples.append(value.replace(" ", "  ") + " " + value.split()[0])
    if field == "choose":
        samples.append(None)
    return [sample for sample in dict.fromkeys(samples) if sample != value]


def matrix_bases(browser: TestClient) -> list[tuple[str, dict[str, str], str]]:
    """For each page field of the table, a form a review rendered that sends it, and the name
    it sends it under: a saved report's review for the fields every page sends and the rows,
    the first review for its answers, and another class's review for the class."""
    first = as_sent(review_page(browser))
    first.update(setup="report", first_month="9", use="current")
    first_saved(browser)
    renamed = as_sent(review_page(browser, RETITLED))
    candidate = renamed["candidates.5"]
    pick = renamed["choices.5"].split()[0]
    other_class = review_page(browser, OTHER_CLASS)
    another = as_sent(other_class)
    offered = [value for _, name, value in form_values(other_class, SAVE) if name == "class"]
    another["class"] = offered[-1]
    bases: list[tuple[str, dict[str, str], str]] = []
    for name, field in grade_routes.FORM.items():
        if field.owner != "page":
            continue
        if field.where in ("always", "answer", "item"):
            form = renamed if field.where == "always" else first
            form = another if name == "class" else form
            bases.append((name, form, "select.0" if field.where == "item" else name))
        elif name == "match":
            bases.append((name, {**renamed, "match.5": candidate}, "match.5"))
        elif name == "choose":
            bases.append((name, {**renamed, "match.5": "choose", "choose.5": pick}, "choose.5"))
            bases.append((name, {**renamed, "choose.5": pick}, "choose.5"))
        else:
            answered_row = {**renamed, "match.5": candidate, "choose.5": ""}
            if name == "choices":
                answered_row = {**renamed, "match.5": "choose", "choose.5": pick}
            bases.append((name, answered_row, f"{name}.5"))
            bases.append((name, answered_row, f"{name}.6"))
    return bases


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
def test_each_page_value_outside_its_grammar_is_refused_before_any_store_call(
    route: str, tmp_path: pathlib.Path
) -> None:
    """The refusals are made from the table, so a page field gets them when it is added: each
    is a form that isn't whole, its text kept and nothing written, and posted to an app that
    has never made the household secret, it still makes none."""
    settings = open_household(tmp_path / "household")
    fresh_folder = tmp_path / "fresh"
    fresh_folder.mkdir()
    not_refused: list[tuple[str, str]] = []
    with at(settings) as browser, at(open_household(fresh_folder)) as fresh:
        bases = matrix_bases(browser)
        before = closed_world([database(settings)], leaving_out=())
        positions = positions_on(review_page(browser, REPORT))
        for field, base, name in bases:
            grade_routes.page_values(base, positions)
            control = browser.post(CHECK, data=base, headers=PAGE)
            assert grade_routes.NOT_WHOLE not in unescape(control.text), (field, name)
            for sample in malformed(field, base[name]):
                form = {key: value for key, value in base.items() if key != name}
                if sample is not None:
                    form[name] = sample
                for client in (browser, fresh):
                    answer = client.post(route, data=form, headers=PAGE)
                    refused = answer.status_code == 422 and (
                        escape(grade_routes.NOT_WHOLE) in answer.text
                        and escape(REPORT.splitlines()[0]) in answer.text
                        and 'name="report_text"' in answer.text
                    )
                    if not refused:
                        not_refused.append((name, repr(sample)[:40]))
        after = closed_world([database(settings)], leaving_out=())
        key_made = getattr(fresh.app.state, "grade_name_key", None)  # type: ignore[attr-defined]

    assert not_refused == []
    assert after == before
    assert not (fresh_folder / SECRET_NAME).exists()
    assert key_made is None


@pytest.mark.parametrize(
    ("row_values", "rule"),
    [
        ({"candidates.5": None}, "a candidate with no candidates"),
        ({"match.5": "different", "candidates.5": None}, "different with no candidates"),
        ({"match.5": "choose", "candidates.5": None}, "choose with no candidates"),
        ({"match.5": "choose", "choices.5": None}, "choose with no choices"),
        ({"choices.5": None}, "a pick with no choices"),
        ({"match.5": "result-" + "f" * 32}, "a candidate not offered"),
        ({"match.5": "choose", "choose.5": "result-" + "f" * 32}, "a pick not offered"),
    ],
)
@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
def test_a_row_s_fields_that_disagree_with_its_own_lists_are_refused(
    route: str, row_values: dict[str, str | None], rule: str, tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        first_saved(browser)
        form = as_sent(review_page(browser, RETITLED))
        form["match.5"] = form["candidates.5"]
        if row_values.get("match.5") == "choose":
            form["choose.5"] = form["choices.5"].split()[0]
        for name, value in row_values.items():
            if value is None:
                form.pop(name)
            else:
                form[name] = value
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 422, rule
    assert escape(grade_routes.NOT_WHOLE) in answer.text
    assert after == before


def test_a_well_formed_form_posted_to_an_app_with_no_secret_makes_one(
    tmp_path: pathlib.Path,
) -> None:
    """The control for the refusals above: the same kind of app does make the secret for a
    form that is whole."""
    fresh_folder = tmp_path / "fresh"
    fresh_folder.mkdir()
    with at(open_household(tmp_path / "household")) as browser:
        form = answered(review_page(browser))
    with at(open_household(fresh_folder)) as fresh:
        answer = fresh.post(CHECK, data=form, headers=PAGE)

    assert grade_routes.NOT_WHOLE not in unescape(answer.text)
    assert (fresh_folder / SECRET_NAME).exists()


def test_a_well_formed_acceptance_id_never_minted_saves_exactly_as_a_minted_one(
    tmp_path: pathlib.Path,
) -> None:
    fresh_id = f"acceptance-{uuid.uuid4().hex}"
    with at(open_household(tmp_path)) as browser:
        form = {**answered(review_page(browser)), "acceptance_id": fresh_id}
        location, said = outcome_of(browser, form)

    assert location == SAVED.format(acceptance_id=fresh_id)
    assert "Saved. 9 added." in said


# ------------------------------------------------------------- a row's two answer controls


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("tick", [True, False], ids=["ticked", "unticked"])
def test_a_pick_with_no_radio_is_the_row_s_answer(
    route: str, tick: bool, tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        first_saved(browser)
        form = as_sent(review_page(browser, RETITLED))
        pick = form["candidates.5"]
        form["choose.5"] = pick
        if tick:
            form["select.5"] = "1"
        counted_before = results_on_record(settings)
        answer = browser.post(route, data=form, headers=PAGE)
        counted_after = results_on_record(settings)

    assert counted_after == counted_before
    if route == CHECK:
        assert answer.status_code == 200, answer.text
        assert f'value="{pick}" selected' in answer.text
        return
    assert answer.status_code == 303, answer.text
    assert decision_of(settings, "Zebra") == ("chosen", pick)


def remembered_review(browser: TestClient) -> dict[str, str]:
    """The retitled report's review again after a save answered its second row "A different
    assignment", which the page now checks by default."""
    first_saved(browser)
    form = as_sent(review_page(browser, RETITLED))
    different = {**unticked(form), "match.5": form["candidates.5"], "match.6": "different"}
    assert browser.post(SAVE, data=different, headers=PAGE).status_code == 303
    page = review_page(browser, RETITLED)
    assert 'name="match.6" value="different" checked' in page
    return as_sent(page)


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("case", ["remembered different", "another candidate", "different"])
def test_a_pick_that_contradicts_the_row_s_radio_keeps_everything_and_marks_the_row(
    route: str, case: str, tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        if case == "remembered different":
            form = remembered_review(browser)
            form.update({"choose.6": form["choices.6"].split()[0], "select.6": "1"})
            row = 6
        else:
            first_saved(browser)
            form = as_sent(review_page(browser, RETITLED))
            other = next(c for c in form["choices.5"].split() if c != form["candidates.5"])
            radio = form["candidates.5"] if case == "another candidate" else "different"
            form.update({"match.5": radio, "choose.5": other, "select.5": "1"})
            row = 5
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 422, answer.text
    assert problem_said(answer.text).startswith(
        f"{grade_routes.CHOOSE_WHICH} {grade_routes.CORRECT_THE_MARKED}"
    )
    assert marked(answer.text) == {row}
    assert sent_from(answer.text) == form
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
def test_a_candidate_radio_and_the_same_pick_agree(route: str, tmp_path: pathlib.Path) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        first_saved(browser)
        form = as_sent(review_page(browser, RETITLED))
        same = form["candidates.5"]
        form.update({"match.5": same, "choose.5": same})
        answer = browser.post(route, data=form, headers=PAGE)

    if route == CHECK:
        assert answer.status_code == 200, answer.text
        return
    assert answer.status_code == 303, answer.text
    assert decision_of(settings, "Zebra") == ("answer", same)


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
def test_choose_with_no_pick_asks_which_and_marks_the_row(
    route: str, tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        first_saved(browser)
        form = {**as_sent(review_page(browser, RETITLED)), "match.5": "choose"}
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 422
    assert escape(grade_routes.CHOOSE_WHICH) in answer.text
    assert marked(answer.text) == {5}
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("case", ["two picks", "a radio and a pick", "two radios"])
def test_two_rows_naming_one_assignment_keep_everything_and_mark_both(
    route: str, case: str, tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        if case == "two radios":
            first_saved(browser, IXL)
            form = as_sent(review_page(browser, IXL_AWAY))
            same = form["candidates.5"].split()[0]
            assert same in form["candidates.6"].split()
            form.update({"match.5": same, "match.6": same})
        else:
            first_saved(browser)
            form = as_sent(review_page(browser, RETITLED))
            same = form["candidates.5"]
            if case == "two picks":
                form.update({"choose.5": same, "choose.6": same})
            else:
                form.update({"match.5": same, "choose.6": same})
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 422, answer.text
    assert problem_said(answer.text).startswith(
        f"{grade_routes.CHOOSE_WHICH} {grade_routes.CORRECT_THE_MARKED}"
    )
    assert marked(answer.text) == {5, 6}
    assert sent_from(answer.text) == form
    assert after == before


def test_a_fresh_review_s_list_starts_at_its_empty_placeholder_with_nothing_selected(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        first_saved(browser)
        page = review_page(browser, RETITLED)

    for row in (5, 6):
        options = [value for _, name, value in form_values(page, SAVE) if name == f"choose.{row}"]
        assert options[0] == ""
        assert len(options) == 3
    assert " selected" not in page.split('id="choose-5"', 1)[1].split("</select>", 1)[0]
    assert as_sent(page)["choose.5"] == ""


def test_a_check_then_a_save_choosing_another_row_s_assignment_saves(
    tmp_path: pathlib.Path,
) -> None:
    """Choosing on one row and checking leaves the other row's list whole."""
    with at(open_household(tmp_path)) as browser:
        first_saved(browser)
        form = as_sent(review_page(browser, RETITLED))
        first, second = form["choices.5"].split()
        checked = browser.post(
            CHECK, data={**form, "match.5": "choose", "choose.5": first}, headers=PAGE
        )
        again = as_sent(checked.text)
        saved = browser.post(
            SAVE, data={**again, "match.6": "choose", "choose.6": second}, headers=PAGE
        )

    assert checked.status_code == 200
    assert again["choices.6"].split() == [first, second]
    assert saved.status_code == 303, saved.text


# ------------------------------------------------------------- the text first, then its rows


def rows_moved(text: str, first: str, second: str) -> str:
    """``text`` with the lines that start ``first`` and ``second`` in each other's places."""
    lines = text.split("\n")
    one, other = (
        next(at for at, line in enumerate(lines) if line.startswith(start))
        for start in (first, second)
    )
    lines[one], lines[other] = lines[other], lines[one]
    return "\n".join(lines)


def row_left_out(text: str, start: str) -> str:
    """``text`` without the line that starts ``start``."""
    return "\n".join(line for line in text.split("\n") if not line.startswith(start))


CELL_LINE = next(line for line in LINES if line.startswith("| Cell Diagram"))
LEAF_LINE = CELL_LINE.replace("Cell Diagram", "Leaf Rubbing").replace("09/26", "09/29")
CHANGED_ROWS = {
    "a row removed": (REPORT, row_left_out(REPORT, "| Osmosis")),
    "two rows swapped": (REPORT, rows_moved(REPORT, "| Seed", "| Cell")),
    "a row added": (REPORT, REPORT.replace(CELL_LINE, f"{CELL_LINE}\n{LEAF_LINE}")),
    "a stray line removed": (STRAY, REPORT),
    "spaces in a cell": CHANGED_ALIKE["spaces in a cell"],
}
"""A text reviewed and the text sent back in its place, its rows moved, added, or kept."""
RETITLED_NO_LINE = RETITLED.replace("**Bramble, Wren**", "")
"""The retitled report with no student line, so the page asks about the name."""
OTHER_SECTION = RETITLED_NO_LINE.replace("**07 BIO - C**", "**07 BIO - D**")
"""The retitled report under another section's class code, so the page asks which class."""
POSITION_FREE: Final = frozenset(
    {
        "report_text",
        "acceptance_id",
        "revision",
        "source_key",
        "text_key",
        "identity_form",
        "identity",
        "setup",
        "setup_year",
        "setup_term",
        "first_month",
        "class",
        "class_name",
        "use",
    }
)
"""The names a review form sends that no row's place decides."""


def by_assignment(page: str) -> dict[str, tuple[bool, str | None, str | None]]:
    """What a page's form sends for each row of its text, by the row's title: the tick, the
    radio answer and the list's pick."""
    form = sent_from(page)
    draft = read_grade_report(form["report_text"]).draft
    assert draft is not None
    titles = [row.assignment.text for category in draft.categories for row in category.rows]
    positions = grade_routes.positions_of(draft)
    return {
        title: (f"select.{at}" in form, form.get(f"match.{at}"), form.get(f"choose.{at}"))
        for at, title in zip(positions.rows, titles, strict=True)
    }


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("name", ["answered", "unanswered"])
@pytest.mark.parametrize(("shown", "sent"), list(CHANGED_ROWS.values()), ids=list(CHANGED_ROWS))
def test_a_changed_text_keeps_no_row_s_tick_and_ticks_what_a_fresh_review_does(
    route: str, name: str, shown: str, sent: str, tmp_path: pathlib.Path
) -> None:
    """A text that changed after its review is reviewed fresh before any field a row's place
    names is read: the answers no row decides stay, and every row shows what a fresh review
    shows."""
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = {**sent_from(review_page(browser, shown)), "class_name": "Bio Lab"}
        del form["select.5"]
        if name == "answered":
            form["identity"] = "hers"
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data={**form, "report_text": sent}, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())
        fresh = review_page(browser, sent)

    assert answer.status_code == 409, answer.text[:300]
    assert problem_said(answer.text) == (
        f"{grade_routes.TEXT_CHANGED} {grade_routes.TEXT_KEPT_ANSWER_AGAIN}"
    )
    returned = sent_from(answer.text)
    assert returned["report_text"] == sent
    assert returned["class_name"] == "Bio Lab"
    assert returned.get("identity") == ("hers" if name == "answered" else None)
    assert by_assignment(answer.text) == by_assignment(fresh)
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("name", ["answered", "unanswered"])
def test_rows_swapped_after_review_carry_no_tick_or_answer_to_each_other(
    route: str, name: str, tmp_path: pathlib.Path
) -> None:
    """Two rows that differ in tick and answer, sent back in each other's places: neither
    assignment takes the other's tick or answer."""
    settings = open_household(tmp_path)
    with at(settings) as browser:
        first_saved(browser)
        form = sent_from(review_page(browser, RETITLED_NO_LINE))
        pick = form["choices.5"].split()[0]
        form.update({"choose.5": pick, "select.5": "1", "match.6": "different"})
        form.pop("select.6", None)
        if name == "answered":
            form["identity"] = "confirmed"
        sent = rows_moved(RETITLED_NO_LINE, "| Zebra", "| Yarrow")
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data={**form, "report_text": sent}, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())
        fresh = review_page(browser, sent)

    assert answer.status_code == 409, answer.text[:300]
    rows = by_assignment(answer.text)
    assert rows["Yarrow Pressing"] == (False, None, "")
    assert rows["Zebra Field Notes"] == (False, None, "")
    assert rows == by_assignment(fresh)
    assert sent_from(answer.text).get("identity") == ("confirmed" if name == "answered" else None)
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize(
    "damaged",
    [
        {"match.8": "xyz"},
        {"select.5": "yes"},
        {"choose.6": "not a result"},
        {"candidates.7": "a  b"},
        {"select.x": "1"},
    ],
    ids=["match of a row gone", "tick", "pick", "candidates", "tick by no place"],
)
def test_a_changed_text_reads_no_row_s_field_so_a_damaged_one_changes_nothing(
    route: str, damaged: dict[str, str], tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    sent = row_left_out(REPORT, "| Osmosis")
    with at(settings) as browser:
        form = {**answered(review_page(browser)), **damaged, "report_text": sent}
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 409, answer.text[:300]
    assert escape(grade_routes.TEXT_CHANGED) in answer.text
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize(
    "damaged",
    [
        {"revision": "007"},
        {"acceptance_id": "acceptance-x"},
        {"identity": "maybe"},
        {"class": "klass-1:none"},
        {"first_month": "13"},
        {"use": "later"},
        {"color": "red"},
    ],
    ids=["revision", "acceptance", "identity", "class", "month", "use", "another form's"],
)
def test_a_changed_text_with_a_damaged_field_no_row_decides_is_refused(
    route: str, damaged: dict[str, str], tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    sent = row_left_out(REPORT, "| Osmosis")
    with at(settings) as browser:
        form = {**answered(review_page(browser)), **damaged, "report_text": sent}
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 422, answer.text[:300]
    assert escape(grade_routes.NOT_WHOLE) in answer.text
    assert escape(sent.splitlines()[0]) in answer.text
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("text", ["unchanged", "changed"])
@pytest.mark.parametrize(
    "case",
    ["text_key left out", "text_key twice", "text_key malformed", "report_text twice"],
)
def test_the_text_and_its_key_each_come_once_whether_or_not_the_text_changed(
    route: str, text: str, case: str, tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form: dict[str, str | list[str]] = {**answered(review_page(browser))}
        reviewed_text = str(form["report_text"])
        if text == "changed":
            form["report_text"] = row_left_out(reviewed_text, "| Osmosis")
        if case == "text_key left out":
            del form["text_key"]
        elif case == "text_key twice":
            form["text_key"] = [str(form["text_key"])] * 2
        elif case == "text_key malformed":
            form["text_key"] = str(form["text_key"]).upper()
        else:
            form["report_text"] = [str(form["report_text"]), reviewed_text]
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 422, answer.text[:300]
    assert escape(grade_routes.NOT_WHOLE) in answer.text
    assert after == before


def checked_in_her_class(browser: TestClient) -> dict[str, str]:
    """The other section's review checked with "It's the same class as" her saved class, the
    name answered and nothing ticked: the page it returns asks its rows' questions in that
    class."""
    first_saved(browser)
    page = review_page(browser, OTHER_SECTION)
    offered = [value for _, name, value in form_values(page, SAVE) if name == "class"]
    form = {**unticked(sent_from(page)), "identity": "confirmed", "class": offered[-1]}
    checked = browser.post(CHECK, data=form, headers=PAGE)
    assert checked.status_code == 200, checked.text[:300]
    again = sent_from(checked.text)
    assert again["class"] == offered[-1]
    assert {"candidates.5", "choices.6"} <= again.keys()
    return again


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("case", ["a conflict on one row", "the name unanswered"])
def test_a_review_returned_to_correct_an_answer_keeps_the_class_chosen(
    route: str, case: str, tmp_path: pathlib.Path
) -> None:
    """The page asking for a correction is the review in the class the page chose, with its
    rows' questions, choices and answers."""
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = checked_in_her_class(browser)
        pick = form["choices.6"].split()[0]
        form.update({"match.5": form["candidates.5"], "match.6": "different", "choose.6": pick})
        if case == "the name unanswered":
            del form["identity"]
            form["choose.6"] = ""
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 422, answer.text[:300]
    assert sent_from(answer.text) == form
    if case == "a conflict on one row":
        assert marked(answer.text) == {6}
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("name", ["answered", "unanswered"])
def test_a_changed_text_is_reviewed_in_the_class_chosen(
    route: str, name: str, tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = checked_in_her_class(browser)
        if name == "unanswered":
            del form["identity"]
        sent = OTHER_SECTION.replace("| 27.0    |", "|  27.0   |")
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data={**form, "report_text": sent}, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 409, answer.text[:300]
    returned = sent_from(answer.text)
    assert returned["class"] == form["class"]
    assert {"candidates.5", "choices.5", "candidates.6", "choices.6"} <= returned.keys()
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
def test_a_page_saved_before_is_shown_again_in_the_class_chosen(
    route: str, tmp_path: pathlib.Path
) -> None:
    """The save that recorded the page named its class code for her class, so the page shown
    again reads in that class with no question about it."""
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = {**checked_in_her_class(browser), "match.5": "different", "match.6": "different"}
        assert browser.post(SAVE, data=unticked(form), headers=PAGE).status_code == 303
        answer = browser.post(route, data={**form, "select.5": "1"}, headers=PAGE)

    assert answer.status_code == 200, answer.text[:300]
    assert escape(grade_routes.SAVED_ELSEWHERE) in answer.text
    assert 'name="class"' not in answer.text
    assert "Microscope Practice" in answer.text


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
def test_a_tick_on_a_row_still_asking_marks_the_row_and_keeps_everything(
    route: str, tmp_path: pathlib.Path
) -> None:
    """A tick on a row whose question is open is answered on Check as on Save: the row is marked
    with its question, and nothing is lost."""
    settings = open_household(tmp_path)
    with at(settings) as browser:
        first_saved(browser)
        form = {**as_sent(review_page(browser, RETITLED)), "select.5": "1"}
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 422, answer.text[:300]
    assert problem_said(answer.text).startswith(
        f"{grade_routes.CHOOSE_WHICH} {grade_routes.CORRECT_THE_MARKED}"
    )
    assert marked(answer.text) == {5}
    assert sent_from(answer.text) == form
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("ticks", [("1",), ("1", "5")], ids=["a saved category", "mixed"])
def test_a_tick_on_a_value_not_new_is_answered_on_check_as_on_save(
    route: str, ticks: tuple[str, ...], tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        first_saved(browser)
        form = as_sent(review_page(browser, RETITLED))
        form.update({f"select.{position}": "1" for position in ticks})
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 409, answer.text[:300]
    assert escape(grade_routes.NOT_NEW) in answer.text
    assert marked(answer.text) == set()
    assert after == before


# ------------------------------------------------------------- the ticks a returned page keeps

RESCORED = REPORT.replace(
    "| Microscope Practice                | 27.0    |",
    "| Microscope Practice                | 28.0    |",
)
"""Wren's report with one lab rescored, which another tab saves."""


def answered_and_ticked(browser: TestClient) -> dict[str, str]:
    """The retitled report's review after the report's first save, its first renamed row
    answered as the saved result it asks about and ticked: a save takes that tick only with the
    answer applied."""
    first_saved(browser)
    form = as_sent(review_page(browser, RETITLED))
    form.update({"match.5": form["candidates.5"], "select.5": "1"})
    return form


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
def test_a_tick_its_kept_answer_makes_ready_is_kept_when_another_tab_saved(
    route: str, tmp_path: pathlib.Path
) -> None:
    """The review returned after another tab's save keeps each tick a save could take with the
    answers it keeps, and says the answers are kept only when every answer and tick is."""
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = answered_and_ticked(browser)
        other = browser.post(SAVE, data=as_sent(review_page(browser, RESCORED)), headers=PAGE)
        assert other.status_code == 303, other.text[:300]
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())
        saved = browser.post(SAVE, data=sent_from(answer.text), headers=PAGE)

    assert answer.status_code == 409, answer.text[:300]
    assert escape(grade_routes.CHANGED_WHILE_REVIEWING) in answer.text
    returned = sent_from(answer.text)
    assert (returned["match.5"], returned["select.5"]) == (form["match.5"], "1")
    assert problem_said(answer.text) == (
        f"{grade_routes.CHANGED_WHILE_REVIEWING} {grade_routes.BOTH_KEPT}"
    )
    assert after == before
    assert saved.status_code == 303, saved.text[:300]
    assert decision_of(settings, "Zebra") == ("answer", form["candidates.5"])


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("why", ["its value not new", "its row's answer not fitting"])
def test_a_tick_dropped_from_a_returned_page_asks_for_the_answers_again(
    route: str, why: str, tmp_path: pathlib.Path
) -> None:
    """A tick a save can't take with the answers kept is dropped, and the page says to check
    the answers again: a value saved before, or a row another tab answered first."""
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = answered_and_ticked(browser)
        if why == "its value not new":
            form["select.1"] = "1"
        else:
            other = {**form, "match.5": "different"}
            assert browser.post(SAVE, data=other, headers=PAGE).status_code == 303
            form["acceptance_id"] = sent_from(review_page(browser, RETITLED))["acceptance_id"]
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 409, answer.text[:300]
    returned = sent_from(answer.text)
    if why == "its value not new":
        assert escape(grade_routes.NOT_NEW) in answer.text
        assert (returned["match.5"], returned["select.5"]) == (form["match.5"], "1")
        assert "select.1" not in returned
    else:
        assert "match.5" not in returned
        assert "select.5" not in returned
    assert problem_said(answer.text).endswith(grade_routes.TEXT_KEPT_ANSWER_AGAIN)
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("use", ["sent", "left to its default"])
def test_a_page_saved_before_keeps_a_tick_its_kept_answer_makes_ready(
    route: str, use: str, tmp_path: pathlib.Path
) -> None:
    """A page sent again under the acceptance ID a save recorded, now with a row answered and
    ticked that save left alone: the row's answer and its tick are offered again. The save
    joined the report, so a choice of its use answers nothing now."""
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = answered_and_ticked(browser)
        if use == "left to its default":
            del form["use"]
        left_alone = {
            name: value for name, value in form.items() if name not in {"match.5", "select.5"}
        }
        assert browser.post(SAVE, data=left_alone, headers=PAGE).status_code == 303
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert answer.status_code == 200, answer.text[:300]
    assert escape(grade_routes.SAVED_ELSEWHERE) in answer.text
    returned = sent_from(answer.text)
    assert (returned["match.5"], returned["select.5"]) == (form["match.5"], "1")
    assert "use" not in returned
    kept = (
        grade_routes.BOTH_KEPT
        if use == "left to its default"
        else grade_routes.TEXT_KEPT_ANSWER_AGAIN
    )
    assert problem_said(answer.text) == (
        f"{grade_routes.SAVED_ELSEWHERE} {kept} See what was saved"
    )
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
def test_a_changed_text_ticks_only_what_its_fresh_review_ticks(
    route: str, tmp_path: pathlib.Path
) -> None:
    """A page whose text changed after review keeps no row's answer or tick: its rows show
    what a fresh review shows, and the page asks for the answers again."""
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = answered_and_ticked(browser)
        sent = RETITLED.replace("| 27.0    |", "|  27.0   |")
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data={**form, "report_text": sent}, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())
        fresh = sent_from(review_page(browser, sent))

    assert answer.status_code == 409, answer.text[:300]
    returned = sent_from(answer.text)
    assert returned["identity"] == form["identity"]
    assert "match.5" not in returned
    ticked = {name for name in returned if name.startswith("select.")}
    assert ticked == {name for name in fresh if name.startswith("select.")}
    assert "select.5" not in ticked
    assert problem_said(answer.text) == (
        f"{grade_routes.TEXT_CHANGED} {grade_routes.TEXT_KEPT_ANSWER_AGAIN}"
    )
    assert after == before


def test_the_text_is_read_first_and_a_changed_one_reads_no_row_s_field(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The names each read of the form allows, pinned: the text and its key alone, then every
    name of the text's own review when it is the text reviewed, and only the names no row's
    place decides when it isn't; a changed text never reaches the reading of the rows."""
    reads: list[tuple[frozenset[str], dict[str, object]]] = []
    ran: list[str] = []
    real_fields_of = fields_of

    async def read(
        request: Request, allowed: frozenset[str], **options: object
    ) -> tuple[dict[str, str], bool]:
        reads.append((allowed, options))
        return await real_fields_of(request, allowed, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(grade_routes, "fields_of", read)
    for name in ("page_values", "row_values"):
        original = getattr(grade_routes, name)

        def watched(
            *args: object, _original: Callable[..., object] = original, _name: str = name
        ) -> object:
            ran.append(_name)
            return _original(*args)

        monkeypatch.setattr(grade_routes, name, watched)
    with at(open_household(tmp_path)) as browser:
        form = answered(review_page(browser))
        reads.clear()
        unchanged = browser.post(CHECK, data=form, headers=PAGE)
        unchanged_reads, unchanged_ran = list(reads), list(ran)
        reads.clear()
        ran.clear()
        sent = row_left_out(form["report_text"], "| Osmosis")
        changed = browser.post(CHECK, data={**form, "report_text": sent}, headers=PAGE)
    positions = positions_on(changed.text)
    text_only = frozenset({"report_text", "text_key"})

    assert unchanged.status_code == 200
    assert changed.status_code == 409
    whole = positions_on(unchanged.text).names
    assert [allowed for allowed, _ in unchanged_reads] == [text_only, whole]
    assert [allowed for allowed, _ in reads] == [text_only, POSITION_FREE]
    assert {name.split(".")[0] for name in positions.names} - POSITION_FREE == {
        "select",
        "candidates",
        "choices",
        "match",
        "choose",
    }
    assert "page_values" in unchanged_ran
    assert ran == []
    first_ignored = reads[0][1]["ignored"]
    rows_ignored = reads[1][1]["ignored"]
    assert callable(first_ignored)
    assert callable(rows_ignored)
    for name in ("identity", "select.5", "color", "class"):
        assert first_ignored(name), name
    for name in text_only:
        assert not first_ignored(name), name
    for name in ("select.5", "match.x", "choose", "candidates.12", "choices.0"):
        assert rows_ignored(name), name
    for name in (*POSITION_FREE, "color", "selects.1", "matched"):
        assert not rows_ignored(name), name
    assert reads[1][1]["may_be_absent"] == POSITION_FREE - grade_routes.ALWAYS


# ------------------------------------------------------------- each choice named by its assignment

SAME_SCORE = REPORT.replace(
    "| Cell Diagram             | 7.0     | 10.0    | 70.0    | Missing    | 09/26   |",
    "| IXL Practice             | 9.0     | 10.0    | 90.0    | Valid      | 09/22   |",
).replace(
    "| Microscope Practice                | 27.0    | 30.0    | 90.0    | Valid      | 09/24   |",
    "| IXL Practice                       | 9.0     | 10.0    | 90.0    | Valid      | 09/22   |",
)
"""Wren's report with a practice row in two categories, the same title, due date and score."""
SAME_SCORE_AWAY = SAME_SCORE.replace(
    "| IXL Practice             |", "| Yarrow Pressing          |"
).replace("| IXL Practice                       |", "| Zebra Field Notes                  |")
"""Both practice rows retitled away: each offers the two saved results."""
SAME_SCORE_LOWER = SAME_SCORE.replace(
    "| IXL Practice             | 9.0     | 10.0    | 90.0    |",
    "| IXL Practice             | 7.0     | 10.0    | 70.0    |",
)
"""A later copy of the report with the Homework / Practice row's score changed."""
TWINS = IXL.replace("| 8.0     | 10.0    | 80.0    |", "| 9.0     | 10.0    | 90.0    |")
"""Two practice rows in one category with the same title, due date and score."""
TWINS_AWAY = TWINS.replace("| IXL Practice         |", "| Zebra Field Notes    |").replace(
    "| IXL Practice             |", "| Yarrow Pressing          |"
)
"""The twin rows retitled away: each asks which of the saved results it is."""
ONE_IXL = REPORT.replace(
    "| Seed Germination Log | 18.0    | 20.0    | 90.0    | Valid      | 09/22   |",
    "| IXL Practice         | 9.0     | 10.0    | 90.0    | Valid      | 09/22   |",
)
"""Wren's report with one practice row, at row 1."""
PRACTICE: Final = "IXL Practice · due 09/22 · saved score 9.0 / 10.0 · Homework / Practice"


def named_choices(page: str, row: int) -> dict[str, str]:
    """Each result a row's list offers, by its value, with the words its option shows."""
    select = page.split(f'id="choose-{row}"', 1)[1].split("</select>", 1)[0]
    found = re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', select)
    return {value: unescape(text) for value, text in found if value}


def named_candidates(page: str, row: int) -> dict[str, str]:
    """Each result a row's question offers by radio, by its value, with the words beside it."""
    found = re.findall(
        rf'<input type="radio" name="match\.{row}" value="(result-[0-9a-f]{{32}})"[^>]*>'
        r" <span>(.*?)</span></label>",
        page,
    )
    return {value: words(text) for value, text in found}


def every_control_named(page: str) -> list[list[str]]:
    """The words of each list's options and of each question's result radios on a page."""
    rows = {int(row) for row in re.findall(r'name="(?:choose|match)\.(\d+)"', page)}
    listed = [row for row in rows if f'id="choose-{row}"' in page]
    named = [list(named_choices(page, row).values()) for row in listed]
    named.extend(list(named_candidates(page, row).values()) for row in rows)
    return [labels for labels in named if labels]


def test_choices_that_share_a_title_and_due_date_are_told_apart_by_the_saved_score(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        first_saved(browser, IXL)
        page = review_page(browser, IXL_AWAY)
        form = as_sent(page)

    ids = form["choices.5"].split()
    expected = {
        "IXL Practice · due 09/22 · saved score 9.0 / 10.0",
        "IXL Practice · due 09/22 · saved score 8.0 / 10.0",
    }
    for row in (5, 6):
        options = named_choices(page, row)
        assert list(options) == ids
        assert set(options.values()) == expected
        assert named_candidates(page, row) == options


def test_choices_that_share_a_title_due_date_and_score_are_told_apart_by_category(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        first_saved(browser, SAME_SCORE)
        page = review_page(browser, SAME_SCORE_AWAY)

    for row in (6, 7):
        assert sorted(named_choices(page, row).values()) == [
            "IXL Practice · due 09/22 · saved score 9.0 / 10.0 · Homework / Practice",
            "IXL Practice · due 09/22 · saved score 9.0 / 10.0 · Labs",
        ]


def test_a_later_copy_kept_as_earlier_leaves_the_saved_score_as_saved(
    tmp_path: pathlib.Path,
) -> None:
    """The saved score is the result's current value, not the newest copy's."""
    with at(open_household(tmp_path)) as browser:
        first_saved(browser, SAME_SCORE)
        lower = {**as_sent(review_page(browser, SAME_SCORE_LOWER)), "use": "earlier"}
        kept = browser.post(SAVE, data=lower, headers=PAGE)
        assert kept.status_code == 303, kept.text
        page = review_page(browser, SAME_SCORE_AWAY)

    assert sorted(named_choices(page, 6).values()) == [
        "IXL Practice · due 09/22 · saved score 9.0 / 10.0 · Homework / Practice",
        "IXL Practice · due 09/22 · saved score 9.0 / 10.0 · Labs",
    ]


def test_two_results_of_one_report_are_told_apart_by_row_on_the_day_it_was_added_there(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The day is the household's: 02:00 UTC on October 8 is October 7 in New York."""
    with at(open_household(tmp_path)) as browser:
        late = FrozenClock(datetime(2026, 10, 8, 2, 0, tzinfo=UTC), ZoneInfo(FIXTURE_TIMEZONE))
        monkeypatch.setattr(store_of(browser), "_clock", late)
        first_saved(browser, TWINS)
        page = review_page(browser, TWINS_AWAY)

    for row in (5, 6):
        assert sorted(named_choices(page, row).values()) == [
            f"{PRACTICE} · report added October 7 · row 1 of that report",
            f"{PRACTICE} · report added October 7 · row 2 of that report",
        ]
        assert named_candidates(page, row) == named_choices(page, row)


def test_the_same_row_of_two_reports_added_the_same_day_is_numbered_the_same_way_each_time(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        first_saved(browser, ONE_IXL)
        twins = as_sent(review_page(browser, TWINS))
        different = {"match.5": "different", "match.6": "different", "select.5": "1"}
        saved = browser.post(SAVE, data={**twins, **different, "select.6": "1"}, headers=PAGE)
        assert saved.status_code == 303, saved.text
        first = review_page(browser, TWINS_AWAY)
        second = review_page(browser, TWINS_AWAY)

    added = f"{PRACTICE} · report added August 19"
    options = named_choices(first, 5)
    assert sorted(options.values()) == [
        "Cell Diagram · due 09/26",
        f"{added} · row 1 of that report · saved entry 1 of 2",
        f"{added} · row 1 of that report · saved entry 2 of 2",
        f"{added} · row 2 of that report",
    ]
    assert named_choices(second, 5) == options
    assert named_choices(second, 6) == options
    assert named_candidates(first, 5) == options


def test_a_check_that_answers_every_open_row_still_names_every_choice(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        first_saved(browser)
        form = as_sent(review_page(browser, RETITLED))
        answers = {"match.5": form["candidates.5"], "match.6": form["candidates.6"]}
        checked = browser.post(CHECK, data={**form, **answers}, headers=PAGE)

    assert checked.status_code == 200, checked.text
    for row in (5, 6):
        assert sorted(named_choices(checked.text, row).values()) == [
            "Cell Diagram · due 09/26",
            "Seed Germination Log · due 09/22",
        ]
    assert "result-" not in words(checked.text)


def test_no_review_state_shows_an_id_and_each_control_names_its_choices_apart(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = every_review_state(tmp_path, monkeypatch)
    named = 0
    for state, page in pages.items():
        assert "result-" not in words(page), state
        for labels in every_control_named(page):
            named += 1
            assert len(set(labels)) == len(labels), (state, labels)
    assert named > 0


def facts(
    title: str = "IXL Practice",
    *,
    due: Cell | None = (Presence.REPORTED, "09/22"),
    points: str = "9.0",
    category: str = "Homework / Practice",
    added: date = PLAN_DATE,
    order: int = 1,
    position: int = 1,
) -> "grade_review.ChoiceFacts":
    return grade_review.ChoiceFacts(
        title=(Presence.REPORTED, title),
        category=(Presence.REPORTED, category),
        due=due,
        points=(Presence.REPORTED, points) if points else (Presence.BLANK, ""),
        max_points=(Presence.REPORTED, "10.0"),
        report_added=added,
        report_order=order,
        position=position,
    )


def test_a_choice_is_named_by_title_and_due_date_alone_when_those_tell_it_apart() -> None:
    labels = grade_routes.choice_labels(
        ["result-a", "result-b"], {"result-a": facts(), "result-b": facts("Cell Diagram")}
    )

    assert labels == {
        "result-a": "IXL Practice · due 09/22",
        "result-b": "Cell Diagram · due 09/22",
    }


def test_a_due_date_never_captured_is_said_and_never_filled_in() -> None:
    labels = grade_routes.choice_labels(
        ["result-a", "result-b", "result-c"],
        {
            "result-a": facts(due=None),
            "result-b": facts(due=(Presence.BLANK, ""), points=""),
            "result-c": facts(due=(Presence.BLANK, "")),
        },
    )

    assert labels == {
        "result-a": "IXL Practice · Due date not captured",
        "result-b": "IXL Practice · due left blank · saved score blank / 10.0",
        "result-c": "IXL Practice · due left blank · saved score 9.0 / 10.0",
    }


def test_the_last_tier_numbers_each_group_in_one_order_whatever_order_it_is_given() -> None:
    offered = {
        "result-c": facts(order=2),
        "result-a": facts(order=1, position=2),
        "result-b": facts(order=1),
    }

    labels = grade_routes.choice_labels(["result-c", "result-a", "result-b"], offered)

    tail = "report added August 19 · row 1 of that report"
    assert labels == {
        "result-b": f"{PRACTICE} · {tail} · saved entry 1 of 2",
        "result-c": f"{PRACTICE} · {tail} · saved entry 2 of 2",
        "result-a": f"{PRACTICE} · report added August 19 · row 2 of that report",
    }


def test_report_text_that_repeats_a_later_tier_still_gives_distinct_labels() -> None:
    """A category written like the tiers after it ties a numbered label; the whole list is then
    numbered in the last tier's order."""
    tail = "report added August 19 · row 1 of that report · saved entry 1 of 2"
    offered = {
        "result-a": facts(order=1),
        "result-b": facts(order=2),
        "result-e": facts(category=f"Homework / Practice · {tail}", order=3),
    }

    labels = grade_routes.choice_labels(list(offered), offered)

    assert len(set(labels.values())) == 3
    assert labels["result-a"].endswith("row 1 of that report · saved entry 1 of 3")
    assert labels["result-b"].endswith("row 1 of that report · saved entry 2 of 3")
    assert labels["result-e"] == f"{PRACTICE} · {tail} · saved entry 3 of 3"


# ------------------------------------------------------------- the script beside a row's list

SCRIPT = STYLESHEET.parent / "blossom.js"
REVIEW_LISTENER: Final = """  var review = document.querySelector("form.review-form");
  if (review) {
    review.addEventListener("change", function (event) {
      var name = event.target.name || "";
      /* An entry picked by hand from a grade row's list also answers the row with "Choose an
         existing assignment". A value the browser restores sends no change, and one a script
         sets isn't trusted, so neither moves the answer. */
      if (event.isTrusted && name.indexOf("choose.") === 0 && event.target.value) {
        var choose = review.querySelector(
          "input[type='radio'][name='match." + name.slice(7) + "'][value='choose']"
        );
        if (choose) {
          choose.checked = true;
        }
      }
      if (name.indexOf("kind-") !== 0 && name.indexOf("occurrence-") !== 0) {
        return;
      }
      if (name.indexOf("kind-") === 0) {
        var card = event.target.closest("article");
        var effect = card ? card.querySelector(".effect") : null;
        if (effect) {
          var base = effect.dataset.base === undefined ? effect.textContent : effect.dataset.base;
          var chosen = event.target.value === "TASK" ? "task" : "homework";
          /* A choice carried from a page before, marked beside the select,
             is the parent's whatever it is set to, the suggestion included. */
          var carried = card.querySelector("input[name='chosen-" + name.slice(5) + "']");
          effect.textContent = event.target.value === event.target.dataset.saved && !carried
            ? base
            : (base ? base + " " : "") + "The type becomes " + chosen + ", as chosen.";
        }
      }
      var button = review.querySelector("button.primary[disabled]");
      if (button) {
        button.disabled = false;
        button.removeAttribute("aria-disabled");
        button.classList.remove("done");
        button.textContent = button.dataset.changedLabel || button.textContent;
      }
    });
  }"""
"""The review forms' one listener, whole: only a trusted change of a row's list to an entry
selects that row's "Choose an existing assignment", before the inbox review's own branches."""


def test_only_an_entry_picked_by_hand_selects_choose_an_existing_assignment() -> None:
    """Nothing on load, on a page shown again or on a value restored moves a row's answer: the
    one listener is pinned whole, and no other line of the script names a row's answer or list."""
    script = SCRIPT.read_text(encoding="utf-8")

    assert script.count(REVIEW_LISTENER) == 1
    rest = script.replace(REVIEW_LISTENER, "")
    assert [line for line in rest.splitlines() if "choose." in line or "match." in line] == []
    assert script.count('addEventListener("change"') == 1


def test_a_returned_page_keeps_a_remembered_different_beside_a_pick_as_sent(
    tmp_path: pathlib.Path,
) -> None:
    """With scripts off, the conflict comes back with both answers as sent: the page itself
    never switches the row to "Choose an existing assignment"."""
    with at(open_household(tmp_path)) as browser:
        form = remembered_review(browser)
        form.update({"choose.6": form["choices.6"].split()[0], "select.6": "1"})
        answer = browser.post(CHECK, data=form, headers=PAGE)

    assert answer.status_code == 422, answer.text
    assert 'name="match.6" value="different" checked' in answer.text
    assert 'name="match.6" value="choose">' in answer.text
    assert f'value="{form["choose.6"]}" selected' in answer.text


# ------------------------------------------------------------- what a returned page keeps

Y2_REPORT = REPORT.replace("**2026-2027**", "**2025-2026**")
"""Wren's report for the year before, which another tab saves."""
Y2_NO_LINE = Y2_REPORT.replace("**Bramble, Wren**", "")
EDITED_LINE = REPORT.replace("**Bramble, Wren**", "**Bramble, W**")
"""Wren's report with its student line edited after review."""
POSITION_FREE_ANSWERS: Final = frozenset(
    {"identity", "setup", "setup_year", "setup_term", "first_month", "class", "class_name", "use"}
)
"""The answers a review form sends that no row's place decides."""


def answers_sent(form: dict[str, str]) -> dict[str, str]:
    """The answers among a form's fields: those no row's place decides, each row's radio, each
    row's nonempty pick and each tick, a class by its ID alone."""
    return {
        name: value.partition(":")[0] if name == "class" else value
        for name, value in form.items()
        if name in POSITION_FREE_ANSWERS
        or name.startswith(("match.", "select."))
        or (name.startswith("choose.") and value)
    }


def kept_as_said(posted: dict[str, str], answer: str, *, source: bool = False) -> bool:
    """Whether the returned page's form, sent unchanged, sends each answer posted as posted, and
    the page says its answers are kept exactly when it does, never after a changed text."""
    returned = answers_sent(sent_from(answer))
    every = all(returned.get(name) == value for name, value in answers_sent(posted).items())
    if 'id="problem"' in answer:
        said = problem_said(answer)
        assert (grade_routes.BOTH_KEPT in said) == (every and not source), said
        assert (grade_routes.TEXT_KEPT_ANSWER_AGAIN in said) == (not every or source), said
    return every


def month_selected(page: str) -> list[str]:
    """The month options a page marks selected, none where it asks no month."""
    found = re.search(
        r'<select id="first-month" name="first_month"[^>]*>(.*?)</select>', page, re.DOTALL
    )
    return [] if found is None else re.findall(r'<option value="([^"]*)" selected', found[1])


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("month", ["unsure", "8"])
def test_a_month_answer_is_kept_as_sent_when_another_tab_saved_another_year(
    route: str, month: str, tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = answered(review_page(browser), first_month=month)
        other = browser.post(SAVE, data=answered(review_page(browser, Y2_REPORT)), headers=PAGE)
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert other.status_code == 303
    assert answer.status_code == 409, answer.text[:300]
    assert problem_said(answer.text) == (
        f"{grade_routes.ANSWER_DOESNT_FIT} {grade_routes.TEXT_KEPT_ANSWER_AGAIN}"
    )
    assert month_selected(answer.text) == [month]
    assert sent_from(answer.text)["first_month"] == month
    assert "setup" not in sent_from(answer.text)
    assert not kept_as_said(form, answer.text)
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("month", ["unsure", "8"])
def test_a_changed_text_keeps_another_setup_as_typed_and_the_next_save_marks_it(
    route: str, month: str, tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        typed = {"setup": "other", "setup_year": "2026-2027", "setup_term": ""}
        form = answered(review_page(browser), **typed)
        changed = form["report_text"].replace("| 7.0 ", "| 8.0 ")
        before = closed_world([database(settings)], leaving_out=())
        returned = browser.post(route, data={**form, "report_text": changed}, headers=PAGE)
        again_form = answered(returned.text, first_month=month)
        again = browser.post(SAVE, data=again_form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert returned.status_code == 409, returned.text[:300]
    assert problem_said(returned.text) == (
        f"{grade_routes.TEXT_CHANGED} {grade_routes.TEXT_KEPT_ANSWER_AGAIN}"
    )
    sent_back = sent_from(returned.text)
    assert {name: sent_back.get(name) for name in typed} == typed
    kept_as_said({**form, "report_text": changed}, returned.text, source=True)
    assert again.status_code == 422, again.text[:300]
    assert problem_said(again.text) == (
        f"{grade_routes.TERM_BLANK} {grade_routes.CORRECT_THE_MARKED} {grade_routes.BOTH_KEPT}"
    )
    assert kept_as_said(again_form, again.text)
    assert month_selected(again.text) == [month]
    for page in (returned.text, again.text):
        assert escape(grade_routes.ANSWER_DOESNT_FIT) not in page
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
def test_an_edited_student_line_drops_the_answer_about_the_name(
    route: str, tmp_path: pathlib.Path
) -> None:
    """The answer about the name is bound to the line it answered: after an edited line the
    page asks it again, and saving that page confirms nothing."""
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = answered(review_page(browser))
        returned = browser.post(route, data={**form, "report_text": EDITED_LINE}, headers=PAGE)
        sent_back = sent_from(returned.text)
        before = closed_world([database(settings)], leaving_out=())
        saved = browser.post(SAVE, data=sent_back, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert returned.status_code == 409, returned.text[:300]
    assert sent_back["identity_form"] != form["identity_form"]
    assert "identity" not in sent_back
    assert saved.status_code == 422, saved.text[:300]
    assert escape(grade_routes.ANSWER_THE_NAME) in saved.text
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("identity", ["hers", "misread", "not_hers"])
def test_a_changed_text_with_the_same_line_keeps_the_answer_about_the_name(
    route: str, identity: str, tmp_path: pathlib.Path
) -> None:
    with at(open_household(tmp_path)) as browser:
        form = answered(review_page(browser), identity=identity)
        changed = form["report_text"].replace("| 7.0 ", "| 8.0 ")
        returned = browser.post(route, data={**form, "report_text": changed}, headers=PAGE)

    assert returned.status_code == 409, returned.text[:300]
    assert sent_from(returned.text)["identity"] == identity


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
def test_a_page_without_the_use_is_checked_and_saved_with_its_default(
    route: str, tmp_path: pathlib.Path
) -> None:
    with at(open_household(tmp_path)) as browser:
        form = answered(review_page(browser))
        del form["use"]
        answer = browser.post(route, data=form, headers=PAGE)

    assert answer.status_code == (200 if route == CHECK else 303), answer.text[:300]


def test_a_returned_page_that_newly_asks_the_use_saves_with_its_default(
    tmp_path: pathlib.Path,
) -> None:
    with at(open_household(tmp_path)) as browser:
        first_saved(browser)
        form = as_sent(review_page(browser))
        changed = form["report_text"].replace("| 7.0 ", "| 8.0 ")
        returned = browser.post(SAVE, data={**form, "report_text": changed}, headers=PAGE)
        saved = browser.post(SAVE, data=sent_from(returned.text), headers=PAGE)

    assert "use" not in form
    assert returned.status_code == 409
    assert 'name="use" value="current"' in returned.text
    assert "use" not in sent_from(returned.text)
    assert saved.status_code == 303, saved.text[:300]


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("month", ["unsure", "8"])
def test_a_page_returned_for_the_name_says_when_another_tab_s_save_dropped_its_answers(
    route: str, month: str, tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = {**sent_from(review_page(browser, NO_LINE)), "first_month": month}
        other = browser.post(SAVE, data=answered(review_page(browser, OTHER_CLASS)), headers=PAGE)
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert other.status_code == 303
    assert answer.status_code == 422, answer.text[:300]
    assert problem_said(answer.text) == (
        f"{grade_routes.NOTHING_SAVED} {grade_routes.ANSWER_THE_NAME} "
        f"{grade_routes.TEXT_KEPT_ANSWER_AGAIN}"
    )
    assert {"setup", "first_month"}.isdisjoint(sent_from(answer.text))
    assert not kept_as_said(form, answer.text)
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
def test_a_page_returned_for_the_name_drops_a_class_another_tab_saved_into(
    route: str, tmp_path: pathlib.Path
) -> None:
    settings = open_household(tmp_path)
    with at(settings) as browser:
        first_saved(browser)
        page = review_page(browser, OTHER_SECTION)
        offered = [value for _, name, value in form_values(page, SAVE) if name == "class"]
        form = {**unticked(sent_from(page)), "class": offered[-1]}
        other = browser.post(SAVE, data=as_sent(review_page(browser, RESCORED)), headers=PAGE)
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert other.status_code == 303
    assert answer.status_code == 422, answer.text[:300]
    now = [value for _, name, value in form_values(answer.text, SAVE) if name == "class"]
    assert offered[-1] not in now
    assert "class" not in sent_from(answer.text)
    assert problem_said(answer.text).endswith(grade_routes.TEXT_KEPT_ANSWER_AGAIN)
    assert not kept_as_said(form, answer.text)
    assert after == before


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize(
    ("case", "said"),
    [
        ("the setup left out", grade_routes.NOT_WHOLE),
        ("the class left out", grade_routes.CHOOSE_CLASS),
        ("the setup left out and the class left out", grade_routes.NOT_WHOLE),
        ("the setup changed and the class left out", grade_routes.ANSWER_DOESNT_FIT),
    ],
)
def test_an_answer_that_doesn_t_fit_says_why_in_the_save_s_order(
    route: str, case: str, said: str, tmp_path: pathlib.Path
) -> None:
    """Each check the answers fail is read in the save's order: a question that changed since
    the page says the answer doesn't fit, then a setup the page asked and sent unanswered is a
    damaged form, then a class the page asked and left unanswered is marked."""
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = {**sent_from(review_page(browser, NO_LINE)), "identity": "confirmed"}
        if "changed" in case:
            year_before = {**sent_from(review_page(browser, Y2_NO_LINE)), "identity": "confirmed"}
            assert browser.post(SAVE, data=year_before, headers=PAGE).status_code == 303
        if "setup left out" in case:
            del form["setup"]
        if "class left out" in case:
            del form["class"]
        before = closed_world([database(settings)], leaving_out=())
        answer = browser.post(route, data=form, headers=PAGE)
        after = closed_world([database(settings)], leaving_out=())

    assert escape(said) in answer.text, words(answer.text)[:600]
    if said == grade_routes.NOT_WHOLE:
        assert answer.status_code == 422
        assert escape(NO_LINE.splitlines()[0]) in answer.text
    elif said == grade_routes.CHOOSE_CLASS:
        assert answer.status_code == 422
        assert problem_said(answer.text) == (
            f"{grade_routes.CHOOSE_CLASS} {grade_routes.CORRECT_THE_MARKED} "
            f"{grade_routes.BOTH_KEPT}"
        )
        assert kept_as_said(form, answer.text)
    else:
        assert answer.status_code == 409
        assert escape(grade_routes.CHOOSE_CLASS) not in answer.text
        kept_as_said(form, answer.text)
    assert after == before


@pytest.mark.parametrize("text", ["unchanged", "changed"])
def test_the_retry_page_says_the_answers_are_kept_only_for_the_text_its_page_carried(
    text: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with at(open_household(tmp_path)) as browser:
        form = answered(review_page(browser, STRAY))
        if text == "changed":
            form["report_text"] = REPORT
        del browser.app.state.grade_name_key  # type: ignore[attr-defined]
        unreadable(tmp_path, monkeypatch)
        retry = browser.post(SAVE, data=form, headers=PAGE)

    assert retry.status_code == 500
    kept = grade_routes.BOTH_KEPT if text == "unchanged" else grade_routes.TEXT_KEPT_ANSWER_AGAIN
    assert problem_said(retry.text).endswith(kept)


RETURNS: Final = (
    "as posted",
    "another year saved",
    "her class saved",
    "text changed",
    "name unanswered",
    "saved before",
)
"""Each way a check or a save answers with the review again."""
ANSWERED: Final[dict[str, dict[str, str]]] = {
    "month untouched": {"first_month": ""},
    "month not sure": {"first_month": "unsure"},
    "month August": {"first_month": "8"},
    "setup another": {"setup": "other", "setup_year": "2026-2027", "setup_term": "T2"},
    "setup another partly typed": {"setup": "other", "setup_year": "2026-27", "setup_term": ""},
    "class named blank": {"class": "new", "class_name": ""},
    "use earlier": {"use": "earlier"},
    "name misread": {"identity": "misread"},
}
"""A first review's answers, each beside the page's defaults."""


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("answers", list(ANSWERED.values()), ids=list(ANSWERED))
@pytest.mark.parametrize("how", RETURNS)
def test_a_returned_page_sends_back_each_answer_as_sent_or_says_to_check_them_again(
    route: str, answers: dict[str, str], how: str, tmp_path: pathlib.Path
) -> None:
    """Whatever returns the review, each answer it sends back is the one posted, never one made
    from it, and it says the answers are kept exactly when every one is sent back."""
    if how == "as posted" and route == SAVE:
        return
    settings = open_household(tmp_path)
    with at(settings) as browser:
        form = answered(review_page(browser), **answers)
        if how == "another year saved":
            first_saved(browser, Y2_REPORT)
        elif how == "her class saved":
            first_saved(browser, RESCORED)
        elif how == "text changed":
            form["report_text"] = form["report_text"].replace("| 7.0 ", "| 8.0 ")
        elif how == "name unanswered":
            del form["identity"]
        elif how == "saved before":
            ready = browser.post(SAVE, data=unticked(answered(review_page(browser))), headers=PAGE)
            assert ready.status_code == 303
            form["acceptance_id"] = sent_from(review_page(browser))["acceptance_id"]
        answer = browser.post(route, data=form, headers=PAGE)

    if answer.status_code == 303 or f'action="{SAVE}"' not in answer.text:
        return
    returned = answers_sent(sent_from(answer.text))
    for name, value in answers_sent(form).items():
        if name in returned and (name, returned[name]) != ("identity", "shown"):
            assert returned[name] == value, (name, value)
    every = kept_as_said(form, answer.text, source=how == "text changed")
    assert every or answer.status_code != 200 or how != "as posted"


ROW_ANSWERS: Final = ("radio and tick", "pick alone", "a different assignment", "same class")
"""A row's answers on the retitled report, and the year's class chosen on another section's."""


@pytest.mark.parametrize("route", [CHECK, SAVE], ids=["check", "save"])
@pytest.mark.parametrize("answer", ROW_ANSWERS)
@pytest.mark.parametrize("how", [one for one in RETURNS if one != "another year saved"])
def test_a_returned_page_sends_back_each_row_and_class_answer_as_sent_or_says_so(
    route: str, answer: str, how: str, tmp_path: pathlib.Path
) -> None:
    if how == "as posted" and route == SAVE:
        return
    settings = open_household(tmp_path)
    with at(settings) as browser:
        if answer == "same class":
            form = checked_in_her_class(browser)
            text = OTHER_SECTION
        else:
            first_saved(browser)
            text = RETITLED
            form = as_sent(review_page(browser, RETITLED))
            if answer == "radio and tick":
                form.update({"match.5": form["candidates.5"], "select.5": "1"})
            elif answer == "pick alone":
                form["choose.5"] = form["choices.5"].split()[0]
            else:
                form["match.5"] = "different"
        if how == "her class saved":
            other = browser.post(SAVE, data=as_sent(review_page(browser, RESCORED)), headers=PAGE)
            assert other.status_code == 303, other.text[:300]
        elif how == "text changed":
            form["report_text"] = form["report_text"].replace("| 27.0    |", "|  27.0   |")
        elif how == "name unanswered":
            del form["identity"]
        elif how == "saved before":
            ready = browser.post(SAVE, data=unticked(form), headers=PAGE)
            assert ready.status_code in (303, 422), ready.text[:300]
            form["acceptance_id"] = sent_from(review_page(browser, text))["acceptance_id"]
        answer_page = browser.post(route, data=form, headers=PAGE)

    if answer_page.status_code == 303 or f'action="{SAVE}"' not in answer_page.text:
        return
    returned = answers_sent(sent_from(answer_page.text))
    for name, value in answers_sent(form).items():
        if name in returned and (name, returned[name]) != ("identity", "shown"):
            assert returned[name] == value, (name, value)
    every = kept_as_said(form, answer_page.text, source=how == "text changed")
    assert every or answer_page.status_code != 200 or how != "as posted"


def test_every_answer_a_returned_page_may_keep_has_its_one_rule_and_one_builder() -> None:
    """Every name the review form sends but those the review mints again has an entry in the
    table that decides whether a returned page keeps it, and every returned review's fields are
    the form's as sent or come through that one function."""
    minted = {name for name, field in grade_routes.FORM.items() if field.where == "always"}
    assert set(grade_routes.KEPT_BY) == set(grade_routes.FORM) - minted
    assert set(grade_routes.KEPT_BY) - grade_routes.ANSWER_NAMES == {"candidates", "choices"}
    assert set(grade_routes.KEPT_BY) >= grade_routes.ANSWER_NAMES
    tree = ast.parse(inspect.getsource(grade_routes))
    built: list[tuple[str, str]] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for call in ast.walk(function):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            if call.func.id == "review_page":
                given = {one.arg: ast.unparse(one.value) for one in call.keywords}
                built.append((function.name, given.get("fields", "")))
            elif call.func.id == "kept_on_return":
                built.append((function.name, "kept_on_return"))
    assert sorted(built) == sorted(
        [
            ("changed_text", "kept.fields"),
            ("changed_text", "kept_on_return"),
            ("refused_answers", "kept.fields"),
            ("refused_answers", "kept_on_return"),
            ("returned_page", "form.fields"),
            ("returned_page", "form.fields"),
            ("returned_page", "form.fields"),
            ("returned_page", "kept.fields"),
            ("returned_page", "kept.fields"),
            ("returned_page", "kept_on_return"),
            ("returned_page", "kept_on_return"),
        ]
    )
    assert not hasattr(grade_routes, "kept_fields")
    assert not hasattr(grade_routes, "all_kept")


def calls_in(source: str, name: str) -> list[str]:
    """The names each call in the function ``name`` of ``source`` calls, in the source's order."""
    found = next(
        one
        for one in ast.walk(ast.parse(source))
        if isinstance(one, ast.FunctionDef) and one.name == name
    )
    calls = [one for one in ast.walk(found) if isinstance(one, ast.Call)]
    calls.sort(key=lambda one: (one.lineno, one.col_offset))
    return [one.func.id for one in calls if isinstance(one.func, ast.Name)]


ASKED: Final = (
    "identity_asked",
    "setup_asked",
    "first_month_asked",
    "class_asked",
    "matches_asked",
    "use_asked",
)
"""Each check a save's answers must pass, in the save's order."""


def test_the_reason_an_answer_doesn_t_fit_reads_every_check_the_save_makes_in_its_order() -> None:
    """The save returns the answers over these checks and no other, and the reason a returned
    page gives reads each of them in the same order, so a check added to the save fails here
    before it can fall through to a reason no one chose."""
    checked = inspect.getsource(gradebook)
    save = [one for one in calls_in(checked, "_checked") if one.endswith("_asked")]
    assert save == ["answers_asked", "matches_asked", "use_asked"]
    composed = calls_in(inspect.getsource(grade_review), "answers_asked")
    assert composed == list(ASKED[:4])
    why = calls_in(inspect.getsource(grade_routes), "answers_why")
    assert [one for one in why if one in ASKED] == list(ASKED)
