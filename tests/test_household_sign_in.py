"""The household sign-in: two passphrases, two people, three devices on one home network.

With both passphrases set, every page and route asks who is there. Hers opens
her week; a parent's opens both pages. The sign-in is a signed cookie with a
secret kept beside the database, so a restart keeps everyone signed in and a
cookie made elsewhere is refused. Each person's cookies are signed with a key
drawn from the secret and their passphrase, so a changed passphrase signs that
person out; wrong passphrases are counted per device. With neither set,
nothing asks, which every other test relies on.
"""

import os
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.app import create_app
from blossom.household import (
    ATTEMPT_LIMIT,
    COOKIE,
    COOLDOWN_SECONDS,
    SECRET_NAME,
    SESSION_SECONDS,
    SKEW_SECONDS,
    SignInAttempts,
    UnreadableHouseholdSecret,
    issue,
    read_token,
)
from blossom.principals import Principal
from blossom.settings import PARENT_PASSPHRASE_VARIABLE, STUDENT_PASSPHRASE_VARIABLE, Settings
from tests.support import fixture_settings

HERS = "quiet mornings and loud music"
THEIRS = "the kitchen table at seven"
SHORT = "short one"
PAGE = {"Accept": "text/html"}


def household(tmp_path: pathlib.Path, **environ: str) -> Settings:
    """Settings with both passphrases set and state under ``tmp_path``; ``environ`` wins."""
    return fixture_settings(
        **{
            "BLOSSOM_TODAY": "2026-08-19",
            "BLOSSOM_DATABASE_PATH": str(tmp_path / "blossom.sqlite3"),
            "BLOSSOM_CHECKPOINT_PATH": str(tmp_path / "checkpoints.sqlite3"),
            "BLOSSOM_TRACE_PATH": str(tmp_path / "traces.sqlite3"),
            "BLOSSOM_STUDENT_PASSPHRASE": HERS,
            "BLOSSOM_PARENT_PASSPHRASE": THEIRS,
            **environ,
        }
    )


def test_a_passphrase_missing_alike_or_short_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match=PARENT_PASSPHRASE_VARIABLE):
        fixture_settings(BLOSSOM_STUDENT_PASSPHRASE=HERS)
    with pytest.raises(ValueError, match=STUDENT_PASSPHRASE_VARIABLE):
        fixture_settings(BLOSSOM_PARENT_PASSPHRASE=THEIRS)
    with pytest.raises(ValueError, match="must differ"):
        fixture_settings(BLOSSOM_STUDENT_PASSPHRASE=HERS, BLOSSOM_PARENT_PASSPHRASE=HERS)
    with pytest.raises(ValueError, match=f"{PARENT_PASSPHRASE_VARIABLE} must be at least 12"):
        fixture_settings(BLOSSOM_STUDENT_PASSPHRASE=HERS, BLOSSOM_PARENT_PASSPHRASE=SHORT)
    assert fixture_settings().household_sign_in is False


def test_without_a_sign_in_a_browser_is_sent_to_sign_in_and_a_call_is_told_401(
    tmp_path: pathlib.Path,
) -> None:
    with TestClient(create_app(household(tmp_path)), follow_redirects=False) as client:
        page = client.get("/student/due-this-week", headers=PAGE)
        call = client.get("/student/plans/today")
        form = client.post("/student/help-requests", data={"note": "hi"}, headers=PAGE)
        posted_call = client.post("/student/help-requests", data={"note": "hi"})
        theirs = client.get("/parent", headers=PAGE)
        stylesheet = client.get("/static/blossom.css")
        sign_in = client.get("/sign-in", headers=PAGE)

    assert page.status_code == 303
    assert page.headers["location"] == "/sign-in?next=%2Fstudent%2Fdue-this-week"
    assert call.status_code == 401
    assert form.status_code == 303
    assert form.headers["location"] == "/sign-in"
    assert posted_call.status_code == 401
    assert theirs.status_code == 303
    assert stylesheet.status_code == 200
    assert "cache-control" not in stylesheet.headers
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
        claims = client.get("/verifier/claims")
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
    assert claims.status_code == 403
    assert own_call.status_code == 200
    assert hers.headers["cache-control"] == "no-store"
    assert own_call.headers["cache-control"] == "no-store"


def test_a_parents_passphrase_opens_both_pages(tmp_path: pathlib.Path) -> None:
    with TestClient(create_app(household(tmp_path)), follow_redirects=False) as client:
        came_in = client.post("/sign-in", data={"passphrase": THEIRS})
        theirs = client.get("/parent", headers=PAGE)
        hers = client.get("/student/due-this-week", headers=PAGE)
        call = client.get("/parent/approvals")
        claims = client.get("/verifier/claims")

    assert came_in.headers["location"] == "/parent"
    assert theirs.status_code == 200
    assert "<h1>Family review</h1>" in theirs.text
    assert theirs.headers["cache-control"] == "no-store"
    assert hers.status_code == 200
    assert call.status_code == 200
    assert claims.status_code == 200
    assert claims.headers["cache-control"] == "no-store"


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


def test_a_cookie_made_elsewhere_aged_out_or_dated_ahead_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    now = datetime.now(UTC)
    app = create_app(household(tmp_path))
    with TestClient(app, follow_redirects=False) as client:
        keys: dict[Principal, bytes] = app.state.household_keys
        theirs = keys[Principal.PARENT]
        forged = issue(Principal.PARENT, b"someone else's secret", now)
        old = issue(Principal.PARENT, theirs, now - timedelta(seconds=SESSION_SECONDS + 1))
        ahead = issue(Principal.PARENT, theirs, now + timedelta(seconds=SKEW_SECONDS + 1))
        client.cookies.set(COOKIE, forged)
        with_forged = client.get("/parent", headers=PAGE)
        client.cookies.set(COOKIE, old)
        with_old = client.get("/parent", headers=PAGE)
        client.cookies.set(COOKIE, ahead)
        with_ahead = client.get("/parent", headers=PAGE)

    assert with_forged.status_code == 303
    assert with_old.status_code == 303
    assert with_ahead.status_code == 303
    hers = keys[Principal.STUDENT]
    assert read_token(issue(Principal.STUDENT, hers, now), keys, now) is Principal.STUDENT
    at_the_edge = issue(Principal.STUDENT, hers, now - timedelta(seconds=SESSION_SECONDS))
    assert read_token(at_the_edge, keys, now) is Principal.STUDENT
    a_little_ahead = issue(Principal.STUDENT, hers, now + timedelta(seconds=SKEW_SECONDS))
    assert read_token(a_little_ahead, keys, now) is Principal.STUDENT
    assert read_token(issue(Principal.STUDENT, theirs, now), keys, now) is None
    assert read_token("STUDENT:notanumber:abc", keys, now) is None
    assert read_token("VERIFIER:1:abc", keys, now) is None
    whole = issue(Principal.PARENT, theirs, now)
    assert read_token("PARENT:\u0661\u0662\u0663:abc", keys, now) is None
    assert read_token("P\u00c4RENT:1:abc", keys, now) is None
    assert read_token(whole[:-1] + "\u00e9", keys, now) is None


def test_changing_a_passphrase_signs_that_person_out_and_not_the_other(
    tmp_path: pathlib.Path,
) -> None:
    """A passphrase that may have been seen is changed in .env and the app restarted: that
    person's devices are signed out, the other person's stay signed in, the old
    passphrase is refused, and the new one works."""
    renewed = "the porch light at nine"
    with TestClient(create_app(household(tmp_path)), follow_redirects=False) as client:
        hers = client.post("/sign-in", data={"passphrase": HERS}).cookies[COOKIE]
        theirs = client.post("/sign-in", data={"passphrase": THEIRS}).cookies[COOKIE]
    after = household(tmp_path, BLOSSOM_PARENT_PASSPHRASE=renewed)
    with TestClient(create_app(after), follow_redirects=False) as client:
        client.cookies.set(COOKIE, theirs)
        old_parent = client.get("/parent", headers=PAGE)
        with_old_phrase = client.post("/sign-in", data={"passphrase": THEIRS})
        client.cookies.set(COOKIE, hers)
        still_hers = client.get("/student/due-this-week", headers=PAGE)
        renewed_parent = client.post("/sign-in", data={"passphrase": renewed})

    assert old_parent.status_code == 303
    assert with_old_phrase.status_code == 422
    assert still_hers.status_code == 200
    assert renewed_parent.status_code == 303
    assert renewed_parent.headers["location"] == "/parent"


def test_ten_wrong_passphrases_from_a_device_are_answered_with_a_wait(
    tmp_path: pathlib.Path,
) -> None:
    """Past the limit the device is told to wait, in the page and in the answer's headers,
    and a right passphrase is not read until then. The sign-in page itself still shows."""
    with TestClient(create_app(household(tmp_path)), follow_redirects=False) as client:
        answers = [
            client.post("/sign-in", data={"passphrase": "open sesame"}).status_code
            for _ in range(ATTEMPT_LIMIT)
        ]
        refused = client.post("/sign-in", data={"passphrase": THEIRS})
        page = client.get("/sign-in", headers=PAGE)

    assert answers == [422] * ATTEMPT_LIMIT
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == str(COOLDOWN_SECONDS)
    assert "Too many tries from this device." in refused.text
    assert COOKIE not in refused.cookies
    assert page.status_code == 200


def test_the_count_is_per_device_bounded_and_over_once_the_wait_is() -> None:
    """The count is kept with a clock handed in, so the wait, its end, a slow trickle of
    wrong tries, a right one, and the bound on devices remembered are all checked."""
    attempts = SignInAttempts(limit=3, cooldown=60, capacity=2)
    start = datetime(2026, 8, 19, 20, 0, tzinfo=UTC)

    def at(seconds: float) -> datetime:
        return start + timedelta(seconds=seconds)

    for _ in range(3):
        assert attempts.wait_for("tablet", at(0)) == 0
        attempts.failed("tablet", at(0))
    assert attempts.wait_for("tablet", at(0)) == 60
    assert attempts.wait_for("tablet", at(59.5)) == 1
    assert attempts.wait_for("laptop", at(1)) == 0
    assert attempts.wait_for("tablet", at(60)) == 0
    assert len(attempts) == 0
    attempts.failed("tablet", at(61))
    assert attempts.wait_for("tablet", at(61)) == 0
    attempts.cleared("tablet")
    assert len(attempts) == 0
    for seconds in (100, 101, 200):
        attempts.failed("phone", at(seconds))
    assert attempts.wait_for("phone", at(200)) == 0
    for device in ("a", "b", "c"):
        attempts.failed(device, at(300))
    assert len(attempts) == 2
    assert attempts.wait_for("phone", at(300)) == 0
    assert len(attempts) == 2


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


def test_the_secret_file_is_whole_or_the_start_stops_by_name(tmp_path: pathlib.Path) -> None:
    """A secret short of the one written is one a stranger could guess, and one with anything
    added is a changed file, so both are refused; the file is read as written. It is the
    owner's alone, and a part left by a start cut short is replaced, not reused."""
    settings = household(tmp_path)
    leftover = tmp_path / f"{SECRET_NAME}.part"
    leftover.write_text("left by a start cut short", encoding="utf-8")
    with TestClient(create_app(settings)):
        pass
    written = (tmp_path / SECRET_NAME).read_text(encoding="utf-8")

    assert len(written) == 64
    assert set(written) <= set("0123456789abcdef")
    assert not list(tmp_path.glob("*.part"))
    if os.name == "posix":
        assert (tmp_path / SECRET_NAME).stat().st_mode & 0o777 == 0o600
    for spoiled in ("", written[:40], written.upper(), written + "\n", " " + written):
        (tmp_path / SECRET_NAME).write_text(spoiled, encoding="utf-8")
        with (
            pytest.raises(UnreadableHouseholdSecret, match=SECRET_NAME),
            TestClient(create_app(settings)),
        ):
            pass


@pytest.mark.parametrize(
    "elsewhere",
    [
        "https://example.org/",
        "//example.org/",
        "///example.org/",
        "////example.org/path",
        "//[",
        "//\uff0fexample.org/",
        "/\\example.org/",
        "/student/due-this-week\r\nSet-Cookie: x=y",
        "javascript:alert(1)",
        "student/due-this-week",
    ],
)
def test_the_next_path_must_be_on_this_site(tmp_path: pathlib.Path, elsewhere: str) -> None:
    """A return address is read the way a browser reads it: anything that could leave
    the site, break the response, or trip the parser is dropped for her own page."""
    with TestClient(create_app(household(tmp_path)), follow_redirects=False) as client:
        came_in = client.post("/sign-in", data={"passphrase": HERS, "next": elsewhere})

    assert came_in.headers["location"] == "/student/due-this-week"


@pytest.mark.parametrize("unreadable", ["//[", "//\uff0fexample.org/"])
def test_a_return_address_the_parser_cannot_read_leaves_the_sign_in_whole(
    tmp_path: pathlib.Path, unreadable: str
) -> None:
    """The sign-in page, the wrong-passphrase answer, and the way in all stand: a return
    address that trips the parser is dropped, not raised, and never echoed into the form."""
    with TestClient(create_app(household(tmp_path)), follow_redirects=False) as client:
        asked = client.get("/sign-in", params={"next": unreadable}, headers=PAGE)
        wrong = client.post("/sign-in", data={"passphrase": "open sesame", "next": unreadable})
        came_in = client.post("/sign-in", data={"passphrase": THEIRS, "next": unreadable})

    assert asked.status_code == 200
    assert 'name="next"' not in asked.text
    assert wrong.status_code == 422
    assert "That passphrase is not one of ours." in wrong.text
    assert 'name="next"' not in wrong.text
    assert came_in.status_code == 303
    assert came_in.headers["location"] == "/parent"


def test_the_sign_in_brings_the_browser_back_to_the_whole_address(
    tmp_path: pathlib.Path,
) -> None:
    wanted = "/student/due-this-week?week=2026-08-24&show_plan=1"
    with TestClient(create_app(household(tmp_path)), follow_redirects=False) as client:
        sent = client.get(wanted, headers=PAGE)
        asked = client.get(sent.headers["location"], headers=PAGE)
        came_in = client.post("/sign-in", data={"passphrase": HERS, "next": wanted})

    assert sent.status_code == 303
    assert sent.headers["location"] == (
        "/sign-in?next=%2Fstudent%2Fdue-this-week%3Fweek%3D2026-08-24%26show_plan%3D1"
    )
    assert asked.status_code == 200
    assert f'name="next" value="{escape(wanted)}"' in asked.text
    assert came_in.headers["location"] == wanted


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
