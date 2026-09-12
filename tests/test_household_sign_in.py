"""The household sign-in: two passphrases, two people, three devices on one home network.

With both passphrases set, every page and route asks who is there. Hers opens
her week; a parent's opens both pages. The sign-in is a signed cookie with a
secret kept beside the database, so a restart keeps everyone signed in and a
cookie made elsewhere is refused. With neither set, nothing asks, which every
other test relies on.
"""

import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.household import COOKIE, SECRET_NAME, SESSION_SECONDS, issue, read_token
from blossom.principals import Principal
from blossom.settings import PARENT_PASSPHRASE_VARIABLE, STUDENT_PASSPHRASE_VARIABLE, Settings
from tests.support import fixture_settings

HERS = "quiet mornings and loud music"
THEIRS = "the kitchen table at seven"
PAGE = {"Accept": "text/html"}


def household(tmp_path: pathlib.Path, **environ: str) -> Settings:
    return fixture_settings(
        BLOSSOM_TODAY="2026-08-19",
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
        BLOSSOM_STUDENT_PASSPHRASE=HERS,
        BLOSSOM_PARENT_PASSPHRASE=THEIRS,
        **environ,
    )


def test_one_passphrase_without_the_other_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match=PARENT_PASSPHRASE_VARIABLE):
        fixture_settings(BLOSSOM_STUDENT_PASSPHRASE=HERS)
    with pytest.raises(ValueError, match=STUDENT_PASSPHRASE_VARIABLE):
        fixture_settings(BLOSSOM_PARENT_PASSPHRASE=THEIRS)
    with pytest.raises(ValueError, match="must differ"):
        fixture_settings(BLOSSOM_STUDENT_PASSPHRASE=HERS, BLOSSOM_PARENT_PASSPHRASE=HERS)
    assert fixture_settings().household_sign_in is False


def test_without_a_sign_in_a_browser_is_sent_to_sign_in_and_a_call_is_told_401(
    tmp_path: pathlib.Path,
) -> None:
    with TestClient(create_app(household(tmp_path)), follow_redirects=False) as client:
        page = client.get("/student/due-this-week", headers=PAGE)
        call = client.get("/student/plans/today")
        theirs = client.get("/parent", headers=PAGE)
        stylesheet = client.get("/static/blossom.css")
        sign_in = client.get("/sign-in", headers=PAGE)

    assert page.status_code == 303
    assert page.headers["location"] == "/sign-in?next=/student/due-this-week"
    assert call.status_code == 401
    assert theirs.status_code == 303
    assert stylesheet.status_code == 200
    assert sign_in.status_code == 200
    assert "<h1>Who is this?</h1>" in sign_in.text


def test_her_passphrase_opens_her_week_and_not_the_family_review(tmp_path: pathlib.Path) -> None:
    with TestClient(create_app(household(tmp_path)), follow_redirects=False) as client:
        came_in = client.post(
            "/sign-in", data={"passphrase": HERS, "next": "/student/due-this-week"}
        )
        hers = client.get("/student/due-this-week", headers=PAGE)
        theirs = client.get("/parent", headers=PAGE)
        call = client.get("/parent/approvals")
        own_call = client.get("/student/help-requests")

    assert came_in.status_code == 303
    assert came_in.headers["location"] == "/student/due-this-week"
    assert COOKIE in came_in.cookies
    assert hers.status_code == 200
    assert "<h1>My week</h1>" in hers.text
    assert ">Sign out<" in hers.text
    assert "Family review" not in hers.text.split("</nav>", 1)[0]
    assert theirs.status_code == 403
    assert "<h1>This page is for a parent</h1>" in theirs.text
    assert call.status_code == 403
    assert own_call.status_code == 200


def test_a_parents_passphrase_opens_both_pages(tmp_path: pathlib.Path) -> None:
    with TestClient(create_app(household(tmp_path)), follow_redirects=False) as client:
        came_in = client.post("/sign-in", data={"passphrase": THEIRS})
        theirs = client.get("/parent", headers=PAGE)
        hers = client.get("/student/due-this-week", headers=PAGE)
        call = client.get("/parent/approvals")

    assert came_in.headers["location"] == "/parent"
    assert theirs.status_code == 200
    assert "<h1>Family review</h1>" in theirs.text
    assert hers.status_code == 200
    assert call.status_code == 200


def test_the_wrong_passphrase_is_said_and_nothing_is_remembered(tmp_path: pathlib.Path) -> None:
    with TestClient(create_app(household(tmp_path)), follow_redirects=False) as client:
        wrong = client.post("/sign-in", data={"passphrase": "open sesame"})
        still_out = client.get("/student/due-this-week", headers=PAGE)

    assert wrong.status_code == 422
    assert "That passphrase is not one of ours." in wrong.text
    assert COOKIE not in wrong.cookies
    assert still_out.status_code == 303


def test_signing_out_forgets_this_device(tmp_path: pathlib.Path) -> None:
    with TestClient(create_app(household(tmp_path)), follow_redirects=False) as client:
        client.post("/sign-in", data={"passphrase": THEIRS})
        out = client.post("/sign-out")
        after = client.get("/parent", headers=PAGE)

    assert out.status_code == 303
    assert out.headers["location"] == "/sign-in"
    assert after.status_code == 303


def test_a_cookie_made_elsewhere_or_aged_out_is_refused(tmp_path: pathlib.Path) -> None:
    now = datetime.now(UTC)
    app = create_app(household(tmp_path))
    with TestClient(app, follow_redirects=False) as client:
        secret: bytes = app.state.household_secret
        forged = issue(Principal.PARENT, b"someone else's secret", now)
        old = issue(Principal.PARENT, secret, now - timedelta(seconds=SESSION_SECONDS + 1))
        client.cookies.set(COOKIE, forged)
        with_forged = client.get("/parent", headers=PAGE)
        client.cookies.set(COOKIE, old)
        with_old = client.get("/parent", headers=PAGE)

    assert with_forged.status_code == 303
    assert with_old.status_code == 303
    assert read_token(issue(Principal.STUDENT, secret, now), secret, now) is Principal.STUDENT
    assert read_token("STUDENT:notanumber:abc", secret, now) is None
    assert read_token("VERIFIER:1:abc", secret, now) is None


def test_a_restart_keeps_everyone_signed_in(tmp_path: pathlib.Path) -> None:
    settings = household(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        came_in = client.post("/sign-in", data={"passphrase": THEIRS})
        token = came_in.cookies[COOKIE]
    with TestClient(create_app(settings), follow_redirects=False) as client:
        client.cookies.set(COOKIE, token)
        after = client.get("/parent", headers=PAGE)

    assert after.status_code == 200
    assert (tmp_path / SECRET_NAME).is_file()


def test_the_next_path_must_be_on_this_site(tmp_path: pathlib.Path) -> None:
    with TestClient(create_app(household(tmp_path)), follow_redirects=False) as client:
        elsewhere = client.post(
            "/sign-in", data={"passphrase": HERS, "next": "https://example.org/"}
        )
        doubled = client.post("/sign-in", data={"passphrase": HERS, "next": "//example.org/"})

    assert elsewhere.headers["location"] == "/student/due-this-week"
    assert doubled.headers["location"] == "/student/due-this-week"


def test_with_no_passphrases_nothing_asks(tmp_path: pathlib.Path) -> None:
    open_settings = fixture_settings(
        BLOSSOM_TODAY="2026-08-19",
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
    )
    with TestClient(create_app(open_settings), follow_redirects=False) as client:
        hers = client.get("/student/due-this-week", headers=PAGE)
        theirs = client.get("/parent", headers=PAGE)
        sign_in = client.get("/sign-in", headers=PAGE)

    assert hers.status_code == 200
    assert theirs.status_code == 200
    assert ">Sign out<" not in hers.text
    assert sign_in.status_code == 303
    assert not (tmp_path / SECRET_NAME).exists()
