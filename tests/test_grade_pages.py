# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The grade pages: adding a report by pasting it, its review, its save, and the outcome.

Every family grade route answers a signed-in parent with sign-in on, and, with sign-in off, only
the computer running Blossom, named as itself: a loopback client and a loopback Host. It refuses
everyone else before it reads a form or opens the store. A review writes nothing, and the paste
travels only in a form's body.
"""

import inspect
import logging
import os
import pathlib
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.params import Form as FormParameter
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from markupsafe import escape
from starlette.requests import Request

from blossom import anthropic_client
from blossom.agent import graph as agent_graph
from blossom.app import create_app
from blossom.grades.draft import GradeReportDraft, capture_key
from blossom.grades.identity import name_form_key
from blossom.grades.review import GradeReportSaved
from blossom.grades.text_reader import read_grade_report
from blossom.household import secret_beside
from blossom.intake import TEXT_MAX_LENGTH
from blossom.routes import grades as grade_routes
from blossom.routes.forms import FormRoute
from blossom.settings import Settings
from blossom.stores.gradebook import VIEW_TABLES, GradeReportNotSaved, GradeTransactionLost
from blossom.stores.paths import SECRET_NAME, UnsafeCheckpointPath
from tests.support import (
    FIXTURES,
    HERS,
    PLAN_DATE,
    THEIRS,
    as_a_browser_sends,
    as_stored,
    capture_class,
    closed_world,
    every_route,
    files_in,
    fixture_settings,
    grade_answers,
    save_grade,
    signed_in,
    signed_in_household,
    store_of,
    whole_form,
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
GRAMMAR = re.compile(
    r"report_text|acceptance_id|revision|source_key|identity_form|identity|setup|setup_year"
    r"|setup_term|first_month|class|class_name|use|(select|match|candidates|choose|choices)\.\d+"
)
"""The names a review's form may send: fixed words, and each item's names by its position."""


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
    assert all(GRAMMAR.fullmatch(name) for name in names), sorted(names)


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
GRADE_REVIEW_RULE = ".grade-review .choice {\n  min-width: 0;\n}\n"
"""The one rule of the grade review's own, pinned whole."""
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

    assert css.count("grade-review") == 1
    assert css.count(GRADE_REVIEW_RULE) == 1
    assert css.count(SHARED_CHOICE_RULE) == 1
    assert carriers == ["grade_review.html"]


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
    assert grade_routes.NAME_CONFIRMED in said
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
    """A parent signed in reads a parent's controls on her address too; she never does."""
    with at(signed_in_household(tmp_path), host="testserver") as browser:
        seeded(browser, FIRST_TERM, SECOND_TERM)
        signed_in(browser, THEIRS)
        browser.post(VIEW, data={"view": "2026-2027 T2"}, headers=PAGE)
        theirs = browser.get(HER_GRADES, headers=PAGE)
        browser.post("/sign-out")
        signed_in(browser, HERS)
        browser.post(HER_VIEW, data={"view": "2026-2027 T2"}, headers=PAGE)
        hers = browser.get(HER_GRADES, headers=PAGE)

    assert f'href="{ADD}"' in theirs.text
    assert f'action="{CURRENT_TERM}"' in theirs.text
    assert f'action="{VIEW}"' in theirs.text
    assert "Use T2 · 2026-2027 as the current term" in words(theirs.text)
    assert f'href="{ADD}"' not in hers.text
    assert CURRENT_TERM not in hers.text
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
    assert "Showing T2 · 2026-2027. The current term is T1 · 2026-2027." in words(showing.text)
    assert '<input type="hidden" name="view" value="current">' in showing.text
    assert "Show the current term" in words(showing.text)
    assert (followed.status_code, followed.headers["location"]) == (303, HER_GRADES)
    assert "The current term is" not in words(current.text)


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
    assert "Quizzes: weight 20.0, no grade reported" in said
    assert not [name for name in ("bramble", "wren") if name in page.text.casefold()]


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
    ],
)
def test_a_class_or_term_position_not_on_record_is_not_found(
    address: str, tmp_path: pathlib.Path
) -> None:
    with at(open_household(tmp_path)) as browser:
        class_id = seeded(browser, FIRST_TERM)
        page = browser.get(address.format(class_id=class_id), headers=PAGE)

    assert page.status_code == 404
    assert escape(grade_routes.CLASS_NOT_ON_RECORD) in page.text


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
