"""Her "Too much right now" signal is hers alone to give and to take back.

A parent signed in reads what she said, in words that name her, and meets no control that
records or withdraws a signal in her name. Every route that would do so answers a parent
403 before a body is read or a signal is looked up, and changes nothing: not the signals,
not her evening's budget, not a waiting plan. With the sign-in off nobody is known, and
the routes and the page answer as they do for her.
"""

import pathlib
import re
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Literal, Protocol

import pytest
from fastapi.testclient import TestClient
from starlette.routing import BaseRoute

from blossom.app import create_app
from blossom.plans import DailyPlan
from blossom.routes import student as student_routes
from blossom.routes.runs import plan_graphs
from blossom.stores.help_requests import NOTE_MAX_LENGTH
from blossom.stores.workload_signals import DETAIL_MAX_LENGTH
from tests.support import (
    HERS,
    PLAN_DATE,
    SAME_ORIGIN,
    THEIRS,
    Answer,
    accepting,
    fixture_settings,
    fixture_week_plan,
    light_fixture_plan,
    scripted_graphs,
    signed_in,
    signed_in_household,
    state_of,
)

PAGE = "/student/due-this-week"
TOO_MUCH = "/student/actions/too-much"
TAKE_BACK = "/student/actions/take-back/"
SIGNALS = "/student/workload-signals"
HELP = "/student/help-requests"
JSON_TYPE = {"Content-Type": "application/json"}
FORM_TYPE = {"Content-Type": "application/x-www-form-urlencoded"}
OVERLONG = b'{"detail": "' + b"x" * (DETAIL_MAX_LENGTH + 1) + b'"}'
ABSENT = "0" * 32
REFUSAL = "Sign in as the student to say today is too much or take it back."
NOT_HERS_TO_ASK = "Sign in as the student to ask for help or take a request back."

Reader = Literal["her", "parent", "open"]


class Response(Answer, Protocol):
    """A response, read as text or as JSON."""

    def json(self) -> dict[str, Any]: ...


# ------------------------------------------------------------------ the contract pinned
#
# The guard answers a parent in the route class, so what the framework describes for each
# route is its handler's alone: request body, statuses, operation id and the handler's own
# docstring, pinned whole.


def body_of(schema: str) -> dict[str, Any]:
    """An optional JSON body of one model, as the framework describes it."""
    return {
        "content": {
            "application/json": {
                "schema": {
                    "anyOf": [{"$ref": f"#/components/schemas/{schema}"}, {"type": "null"}],
                    "title": "Payload",
                }
            }
        }
    }


def answered_with(schema: str) -> dict[str, Any]:
    return {
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema}"}}},
        "description": "Successful Response",
    }


NOT_VALID = answered_with("HTTPValidationError") | {"description": "Validation Error"}

HELP_POST = {
    "description": (
        "Ask for help today. ``payload`` is optional so an empty POST works. With a\n"
        "``request_id`` the same id sent again is the request it made, 200, and never a "
        "second;\nwithout one every POST asks again, so a retry is not safe. ``HersToAsk`` "
        "answers a\nparent 403 before the body is read."
    ),
    "operationId": "ask_for_help_student_help_requests_post",
    "requestBody": body_of("HelpRequestBody"),
    "responses": {"201": answered_with("HelpRequestResponse"), "422": NOT_VALID},
    "summary": "Ask For Help",
    "tags": ["student"],
}
SIGNAL_POST = {
    "description": (
        "Record that today is too much. ``payload`` is optional so an empty POST works."
    ),
    "operationId": "register_workload_signal_student_workload_signals_post",
    "requestBody": body_of("WorkloadSignalRequest"),
    "responses": {"201": answered_with("WorkloadSignalResponse"), "422": NOT_VALID},
    "summary": "Register Workload Signal",
    "tags": ["student"],
}
SIGNAL_DELETE = {
    "description": "Take a signal back. It is gone, not marked; the record is hers to remove.",
    "operationId": "withdraw_workload_signal_student_workload_signals__signal_id__delete",
    "parameters": [
        {
            "in": "path",
            "name": "signal_id",
            "required": True,
            "schema": {"title": "Signal Id", "type": "string"},
        }
    ],
    "responses": {"204": {"description": "Successful Response"}, "422": NOT_VALID},
    "summary": "Withdraw Workload Signal",
    "tags": ["student"],
}


# ------------------------------------------------------------------ the household


@contextmanager
def household(
    tmp_path: pathlib.Path, reader: Reader, *, plan: Callable[[], DailyPlan] | None = None
) -> Iterator[TestClient]:
    """The pinned day, read by one reader. ``plan`` gives the household a key and a
    scripted planner that answers with it, so a plan can be made; without it planning is
    unavailable, as in a household with no key."""
    if reader == "open":
        environ = {"ANTHROPIC_API_KEY": "not-a-key-and-never-sent"} if plan else {}
        settings = fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **environ)
    else:
        settings = signed_in_household(tmp_path)
        if plan:
            settings = replace(settings, anthropic_api_key="not-a-key-and-never-sent")
    app = create_app(settings)
    if plan:
        planned = plan
        app.dependency_overrides[plan_graphs] = scripted_graphs(
            lambda: [planned()], lambda: [accepting()]
        )
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        if reader != "open":
            signed_in(client, HERS)
        if reader == "parent":
            as_a_parent(client)
        yield client


def as_a_parent(client: TestClient) -> None:
    client.post("/sign-out")
    signed_in(client, THEIRS)


def as_her(client: TestClient) -> None:
    client.post("/sign-out")
    signed_in(client, HERS)


def rows(client: TestClient) -> list[tuple[object, ...]]:
    """Every signal kept, whole, straight from the store's table."""
    connection = state_of(client).workload_signals._connection
    return [tuple(row) for row in connection.execute("SELECT * FROM workload_signals ORDER BY 1")]


def one_of_hers(client: TestClient, detail: str | None = None) -> str:
    """A signal of hers for today, kept straight in the store, whoever is signed in."""
    return state_of(client).workload_signals.record(PLAN_DATE, detail).signal_id


def never(*args: object, **kwargs: object) -> None:
    msg = "a parent's press reached the signal store"
    raise AssertionError(msg)


def nothing_reaches_the_store(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any record or withdrawal from here on fails the test: a refusal looks nothing up."""
    monkeypatch.setattr(student_routes, "record_signal", never)
    monkeypatch.setattr(student_routes, "withdraw_signal", never)
    monkeypatch.setattr(state_of(client).workload_signals, "record", never)
    monkeypatch.setattr(state_of(client).workload_signals, "withdraw", never)


def press(client: TestClient, which: str, signal_id: str = ABSENT) -> Response:
    """One of the four presses that would record or withdraw a signal."""
    match which:
        case "too much":
            return client.post(TOO_MUCH)
        case "take back":
            return client.post(TAKE_BACK + signal_id)
        case "create":
            return client.post(SIGNALS)
        case _:
            return client.delete(f"{SIGNALS}/{signal_id}")


def without_form_ids(page: str) -> str:
    """A page with the fresh id each render gives its forms taken out, so two readings of
    the same record compare equal."""
    return re.sub(r'value="[0-9a-f]{32}"', 'value=""', page)


# ------------------------------------------------------------ a parent is refused


@pytest.mark.parametrize(
    ("which", "whose"),
    [
        ("too much", None),
        ("take back", "hers"),
        ("take back", "absent"),
        ("create", None),
        ("delete", "hers"),
        ("delete", "absent"),
    ],
    ids=[
        "html too much",
        "html take back",
        "html take back absent",
        "json create",
        "json delete",
        "json delete absent",
    ],
)
def test_a_parent_is_refused_on_every_signal_route_and_nothing_changes(
    which: str, whose: str | None, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with household(tmp_path, "parent") as client:
        hers = one_of_hers(client)
        before = rows(client)
        nothing_reaches_the_store(client, monkeypatch)
        answer = press(client, which, hers if whose == "hers" else ABSENT)
        after = rows(client)

    assert answer.status_code == 403
    assert after == before
    if which in ("too much", "take back"):
        assert f'<p class="problem" role="alert">{REFUSAL}</p>' in answer.text
        assert "<h1>Student week</h1>" in answer.text
    else:
        assert answer.json() == {"detail": REFUSAL}


@pytest.mark.parametrize(
    ("body", "headers"),
    [
        (b"", {}),
        (b"null", JSON_TYPE),
        (b'{"detail": "from a parent"}', JSON_TYPE),
        (b"{not json", JSON_TYPE),
        (OVERLONG, JSON_TYPE),
        (b'{"status": "done"}', JSON_TYPE),
        (b'{"detail": "from a parent"}', {"Content-Type": "text/plain"}),
        (b'{"detail": "from a parent"}', {}),
        (b"\xff\xfe{", JSON_TYPE),
    ],
    ids=[
        "empty",
        "null",
        "json",
        "malformed",
        "overlong",
        "unknown field",
        "plain text",
        "no content type",
        "not utf-8",
    ],
)
def test_a_parents_json_body_is_never_read(
    body: bytes, headers: dict[str, str], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the body, a parent is answered 403: never the 422 or 400 reading it gives."""
    with household(tmp_path, "parent") as client:
        before = rows(client)
        nothing_reaches_the_store(client, monkeypatch)
        answer = client.post(SIGNALS, content=body, headers=headers)
        after = rows(client)

    assert answer.status_code == 403
    assert answer.json() == {"detail": REFUSAL}
    assert after == before


@pytest.mark.parametrize(
    ("which", "body"),
    [("too much", b"detail=from+a+parent"), ("take back", b"x=1&x=2"), ("too much", b"\xff")],
    ids=["too much with words", "take back with a junk form", "too much not utf-8"],
)
def test_a_parents_page_press_reads_no_form(
    which: str, body: bytes, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with household(tmp_path, "parent") as client:
        hers = one_of_hers(client)
        before = rows(client)
        nothing_reaches_the_store(client, monkeypatch)
        where = TOO_MUCH if which == "too much" else TAKE_BACK + hers
        answer = client.post(where, content=body, headers=FORM_TYPE)
        after = rows(client)

    assert answer.status_code == 403
    assert REFUSAL in answer.text
    assert after == before


# ---------------------------------------------------------- her, and nobody known


def answered(client: TestClient, which: str) -> tuple[int, object]:
    """One press and what it said: the status, and the JSON error's type and place."""
    hers = one_of_hers(client) if which in ("take back", "delete") else ABSENT
    match which:
        case "too much" | "take back" | "delete":
            answer = press(client, which, hers)
        case "take back absent":
            answer = press(client, "take back")
        case "delete absent":
            answer = press(client, "delete")
        case "create":
            answer = client.post(SIGNALS, json={"detail": "a long day"})
        case "malformed":
            answer = client.post(SIGNALS, content=b"{not json", headers=JSON_TYPE)
        case "overlong":
            answer = client.post(SIGNALS, content=OVERLONG, headers=JSON_TYPE)
        case "unknown field":
            answer = client.post(SIGNALS, content=b'{"status": "done"}', headers=JSON_TYPE)
        case "plain text":
            answer = client.post(
                SIGNALS, content=b'{"detail": "x"}', headers={"Content-Type": "text/plain"}
            )
        case _:
            answer = client.post(SIGNALS, content=b"\xff\xfe{", headers=JSON_TYPE)
    said: object = None
    if answer.status_code == 422:
        error = answer.json()["detail"][0]
        said = (error["type"], error["loc"])
    elif answer.status_code == 404:
        said = answer.json()["detail"]
    elif answer.status_code == 201:
        body = answer.json()
        said = (body["principal"], body["signal"]["detail"])
    elif answer.status_code == 303:
        said = answer.headers["location"]
    return answer.status_code, said


@pytest.mark.parametrize("reader", ["her", "open"])
@pytest.mark.parametrize(
    ("which", "expected", "kept"),
    [
        ("too much", (303, PAGE), 1),
        ("take back", (303, PAGE), 0),
        ("take back absent", (303, PAGE), 0),
        ("create", (201, ("STUDENT", "a long day")), 1),
        ("delete", (204, None), 0),
        ("delete absent", (404, f"no signal '{ABSENT}'"), 0),
        ("malformed", (422, ("json_invalid", ["body", 1])), 0),
        ("overlong", (422, ("string_too_long", ["body", "detail"])), 0),
        ("unknown field", (422, ("extra_forbidden", ["body", "status"])), 0),
        ("plain text", (422, ("model_attributes_type", ["body"])), 0),
        ("not utf-8", (400, None), 0),
    ],
)
def test_her_presses_and_the_open_households_keep_their_answers(
    reader: Reader,
    which: str,
    expected: tuple[int, object],
    kept: int,
    tmp_path: pathlib.Path,
) -> None:
    with household(tmp_path, reader) as client:
        got = answered(client, which)
        left = rows(client)

    assert got == expected
    assert len(left) == kept


@pytest.mark.parametrize("reader", ["her", "open"])
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"{not json", ("json_invalid", ["body", 1])),
        (
            b'{"note": "' + b"x" * (NOTE_MAX_LENGTH + 1) + b'"}',
            ("string_too_long", ["body", "note"]),
        ),
        (b'{"status": "done"}', ("extra_forbidden", ["body", "status"])),
    ],
    ids=["malformed", "overlong", "unknown field"],
)
def test_her_help_ask_is_checked_as_it_is_for_her(
    reader: Reader, body: bytes, expected: tuple[str, list[object]], tmp_path: pathlib.Path
) -> None:
    with household(tmp_path, reader) as client:
        answer = client.post(HELP, content=body, headers=JSON_TYPE)

    assert answer.status_code == 422
    error = answer.json()["detail"][0]
    assert (error["type"], error["loc"]) == expected


# ----------------------------------------------------------- a decision in progress


def test_a_parent_is_answered_at_once_while_a_decision_holds_the_lock(
    tmp_path: pathlib.Path,
) -> None:
    """Her presses wait for a decision to land; a parent's refusal waits for nothing."""
    with household(tmp_path, "parent") as client, ThreadPoolExecutor(max_workers=4) as pool:
        hers = one_of_hers(client)
        state = state_of(client)
        portal = client.portal
        assert portal is not None
        before = rows(client)
        portal.call(state.decision_lock.acquire)
        try:
            presses = [
                pool.submit(press, client, which, hers)
                for which in ("too much", "take back", "create", "delete")
            ]
            _, still_waiting = wait(presses, timeout=5)
        finally:
            portal.call(state.decision_lock.release)
        after = rows(client)

    assert still_waiting == set()
    assert [each.result().status_code for each in presses] == [403, 403, 403, 403]
    assert after == before


def test_her_presses_wait_while_a_decision_holds_the_lock(tmp_path: pathlib.Path) -> None:
    with household(tmp_path, "her") as client, ThreadPoolExecutor(max_workers=4) as pool:
        first, second = one_of_hers(client), one_of_hers(client)
        state = state_of(client)
        portal = client.portal
        assert portal is not None
        portal.call(state.decision_lock.acquire)
        try:
            presses: list[Future[Response]] = [
                pool.submit(press, client, "too much"),
                pool.submit(press, client, "take back", first),
                pool.submit(press, client, "create"),
                pool.submit(press, client, "delete", second),
            ]
            _, still_waiting = wait(presses, timeout=0.3)
            while_held = len(rows(client))
        finally:
            portal.call(state.decision_lock.release)
        statuses = [each.result(timeout=10).status_code for each in presses]
        after = len(rows(client))

    assert still_waiting == set(presses)
    assert while_held == 2
    assert statuses == [303, 303, 201, 204]
    assert after == 2


# ------------------------------------------------------------- a shared device


def test_her_page_left_open_when_a_parent_signs_in_is_refused(tmp_path: pathlib.Path) -> None:
    """On one device: her page open with its buttons, a parent signs in, and a press on
    the page she left is refused with the words that say what to do."""
    with household(tmp_path, "her") as client:
        left_open = client.get(PAGE).text
        hers = one_of_hers(client)
        left_open_with_one = client.get(PAGE).text
        as_a_parent(client)
        before = rows(client)
        too_much = client.post(TOO_MUCH)
        take_back = client.post(TAKE_BACK + hers)
        after = rows(client)

    assert f'action="{TOO_MUCH}"' in left_open
    assert f'action="{TAKE_BACK}{hers}"' in left_open_with_one
    assert too_much.status_code == 403
    assert take_back.status_code == 403
    assert f'<p class="problem" role="alert">{REFUSAL}</p>' in too_much.text
    assert f'<p class="problem" role="alert">{REFUSAL}</p>' in take_back.text
    assert after == before


# -------------------------------------------------------------- her evening


@pytest.mark.parametrize("which", ["too much", "take back", "create", "delete"])
def test_a_parents_refused_press_leaves_her_evening_as_it_was(
    which: str, tmp_path: pathlib.Path
) -> None:
    """A refused Too much would have held her next plan short; a refused take-back would
    have let her smaller plan's signal go. Neither moves her evening, her plan's notice or
    what the family page says."""
    signal_up = which in ("take back", "delete")
    plan = light_fixture_plan if signal_up else fixture_week_plan
    with household(tmp_path, "her", plan=plan) as client:
        hers = one_of_hers(client) if signal_up else ABSENT
        client.post("/student/actions/plan")
        today_before = client.get("/student/plans/today").json()
        as_a_parent(client)
        page_before = without_form_ids(client.get(PAGE).text)
        family_before = without_form_ids(client.get("/parent").text)
        refused = press(client, which, hers)
        page_after = without_form_ids(client.get(PAGE).text)
        family_after = without_form_ids(client.get("/parent").text)
        as_her(client)
        today_after = client.get("/student/plans/today").json()
        kept = rows(client)

    assert refused.status_code == 403
    assert len(kept) == (1 if signal_up else 0)
    assert today_after == today_before
    assert today_after["stale"] is None
    assert page_after == page_before
    assert family_after == family_before


# ------------------------------------------------------------------- the page


def test_a_parent_meets_no_signal_control_and_reads_what_she_said(
    tmp_path: pathlib.Path,
) -> None:
    with household(tmp_path, "parent") as client:
        none_yet = client.get(PAGE).text
        one_of_hers(client, "the essay and two quizzes")
        one_of_hers(client, "a late night")
        signaled = client.get(PAGE).text

    assert f'action="{TOO_MUCH}"' not in none_yet
    assert "Too much right now</button>" not in none_yet
    assert TAKE_BACK not in signaled
    assert "Take it back</button>" not in signaled
    assert "Remove</button>" not in signaled
    assert "<strong>She said it was too much</strong> at" in signaled
    assert "Her next plan for today is held to 75 minutes instead of 150." in signaled
    assert "Nothing has been planned yet; the next plan will be the shorter one." in signaled
    assert signaled.count("She added <q>a late night</q>.") == 2
    assert signaled.count("She added <q>the essay and two quizzes</q>.") == 1
    assert signaled.count("Too much on Wednesday, August 19, 2026,") == 2
    assert (
        '"Too much right now" takes her one press, no rating, no reason, and she can take it '
        "back." in signaled
    )
    for said_to_her in ("You said", "You added", "Your next plan", "the plan you make"):
        assert said_to_her not in signaled
    assert "you can take it back" not in signaled
    assert "<summary>What Blossom keeps about this</summary>" in signaled
    assert 'id="today"' in signaled
    assert "autofocus" not in none_yet
    assert "autofocus" not in signaled


@pytest.mark.parametrize("reader", ["her", "open"])
def test_she_and_the_open_household_keep_every_control_and_word(
    reader: Reader, tmp_path: pathlib.Path
) -> None:
    with household(tmp_path, reader) as client:
        none_yet = client.get(PAGE).text
        first = one_of_hers(client, "the essay and two quizzes")
        latest = one_of_hers(client, "a late night")
        signaled = client.get(PAGE).text

    assert f'action="{TOO_MUCH}"' in none_yet
    assert ">Too much right now</button>" in none_yet
    assert signaled.count(f'action="{TAKE_BACK}{latest}"') == 2
    assert signaled.count(f'action="{TAKE_BACK}{first}"') == 1
    assert signaled.count(">Take it back</button>") == 1
    assert signaled.count(">Remove</button>") == 2
    assert "<strong>You said it was too much</strong> at" in signaled
    assert "Your next plan for today is held to 75 minutes instead of 150." in signaled
    assert "Nothing has been planned yet; the plan you make will be the shorter one." in signaled
    assert signaled.count("You added <q>a late night</q>.") == 2
    assert signaled.count("You added <q>the essay and two quizzes</q>.") == 1
    assert (
        '"Too much right now" takes one press, no rating, no reason, and you can take it back.'
        in signaled
    )
    for said_to_a_parent in ("She said", "She added", "Her next plan", "she can take it back"):
        assert said_to_a_parent not in signaled
    assert 'id="today"' in signaled
    assert "autofocus" not in none_yet
    assert "autofocus" not in signaled


@pytest.mark.parametrize(
    ("reader", "sentence"),
    [
        ("parent", "Planning is unavailable right now. Everything else here still works.</p>"),
        (
            "her",
            'Planning is unavailable right now. Everything else here still works, and "Too '
            'much\n        right now" is still yours to press.',
        ),
        (
            "open",
            'Planning is unavailable right now. Everything else here still works, and "Too '
            'much\n        right now" is still yours to press.',
        ),
    ],
)
def test_planning_off_invites_only_her_to_press(
    reader: Reader, sentence: str, tmp_path: pathlib.Path
) -> None:
    with household(tmp_path, reader) as client:
        page = client.get(PAGE).text

    assert sentence in page
    if reader == "parent":
        assert "is still yours to press" not in page


# ------------------------------------------------------- the plan's notices


SIGNALED_TO_HER = (
    "You have said today is too much, and this plan was made for the full evening. "
    "Your current plan has not changed yet. Make a smaller plan when you are ready."
)
SIGNALED_TO_A_PARENT = (
    "She has said today is too much, and this plan was made for the full evening. "
    "Her current plan has not changed yet. Make a smaller plan when she is ready."
)
SIGNAL_ENDED = (
    "This plan was kept to the smaller evening for a signal that is not there now. "
    "It stays until a new one is made; plan again for the full evening."
)


def her_week_as(client: TestClient, reader: Reader) -> str:
    if reader == "parent":
        as_a_parent(client)
    return client.get(PAGE).text


@pytest.mark.parametrize(
    ("reader", "sentence"),
    [("her", SIGNALED_TO_HER), ("parent", SIGNALED_TO_A_PARENT), ("open", SIGNALED_TO_HER)],
)
def test_a_signal_after_the_plan_is_said_to_whoever_reads_her_page(
    reader: Reader, sentence: str, tmp_path: pathlib.Path
) -> None:
    signs_in: Reader = "open" if reader == "open" else "her"
    with household(tmp_path, signs_in, plan=fixture_week_plan) as client:
        client.post("/student/actions/plan")
        one_of_hers(client)
        page = her_week_as(client, reader)

    assert f"<strong>Make a smaller plan.</strong> {sentence}</p>" in page
    other = SIGNALED_TO_HER if sentence == SIGNALED_TO_A_PARENT else SIGNALED_TO_A_PARENT
    assert other not in page


@pytest.mark.parametrize("reader", ["her", "parent", "open"])
@pytest.mark.parametrize("gone", ["taken back", "past its week"])
def test_a_signal_gone_after_a_smaller_plan_reads_the_same_to_everyone(
    reader: Reader, gone: str, tmp_path: pathlib.Path
) -> None:
    """The notice names nobody and never says the signal was taken back: one that has
    passed its week reads the same as one withdrawn."""
    signs_in: Reader = "open" if reader == "open" else "her"
    with household(tmp_path, signs_in, plan=light_fixture_plan) as client:
        hers = one_of_hers(client)
        client.post("/student/actions/plan")
        signals = state_of(client).workload_signals
        if gone == "taken back":
            signals.withdraw(hers)
        else:
            signals._connection.execute(
                "UPDATE workload_signals SET evening = '2026-08-18' WHERE signal_id = ?",
                (hers,),
            )
        page = her_week_as(client, reader)

    assert f"<strong>Plan again.</strong> {SIGNAL_ENDED}</p>" in page
    assert "taken back" not in page
    assert "withdr" not in page


@pytest.mark.parametrize(
    ("reader", "sentence"),
    [
        ("her", "Kept to a shorter evening because you said today was too much."),
        ("parent", "Kept to a shorter evening because she said today was too much."),
        ("open", "Kept to a shorter evening because you said today was too much."),
    ],
)
def test_a_smaller_plan_says_whose_signal_shortened_it(
    reader: Reader, sentence: str, tmp_path: pathlib.Path
) -> None:
    signs_in: Reader = "open" if reader == "open" else "her"
    with household(tmp_path, signs_in, plan=light_fixture_plan) as client:
        one_of_hers(client)
        client.post("/student/actions/plan")
        page = her_week_as(client, reader)

    assert sentence in page


# ----------------------------------------------------------- the contract kept


def test_the_openapi_entries_are_pinned_whole(tmp_path: pathlib.Path) -> None:
    with household(tmp_path, "open") as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert paths[SIGNALS]["post"] == SIGNAL_POST
    assert paths[SIGNALS + "/{signal_id}"]["delete"] == SIGNAL_DELETE
    assert paths[HELP]["post"] == HELP_POST


@pytest.mark.parametrize("reader", ["her", "open"])
def test_the_created_signal_and_help_request_keep_their_fields(
    reader: Reader, tmp_path: pathlib.Path
) -> None:
    with household(tmp_path, reader) as client:
        signal = client.post(SIGNALS, json={"detail": "a long day"})
        asked = client.post(HELP, json={"note": "the essay"})

    assert signal.status_code == 201
    assert asked.status_code == 201
    assert set(signal.json()) == {"principal", "signal"}
    assert signal.json()["principal"] == "STUDENT"
    assert set(asked.json()) == {"principal", "request"}
    assert asked.json()["principal"] == "STUDENT"


@pytest.mark.parametrize(
    "body", [b"", b'{"note": "the essay"}', b"{not json"], ids=["empty", "valid", "malformed"]
)
def test_a_parents_help_ask_keeps_its_own_refusal(body: bytes, tmp_path: pathlib.Path) -> None:
    with household(tmp_path, "parent") as client:
        answer = client.post(HELP, content=body, headers=JSON_TYPE)
        held = state_of(client).help_requests.open_requests()

    assert answer.status_code == 403
    assert answer.json() == {"detail": NOT_HERS_TO_ASK}
    assert held == []


def every_route(routes: Iterable[BaseRoute]) -> Iterator[BaseRoute]:
    """Every route the application answers on, each included router opened up."""
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is None:
            yield route
        else:
            yield from every_route(included.routes)


def test_exactly_three_routes_are_hers_alone(tmp_path: pathlib.Path) -> None:
    with household(tmp_path, "open") as client:
        routes = list(every_route(client.app.routes))  # type: ignore[attr-defined]
        hers_alone = sorted(
            (route.path, sorted(route.methods or ()), type(route).__name__)
            for route in routes
            if isinstance(route, student_routes.HersAlone)
        )

    assert {TOO_MUCH, TAKE_BACK + "{signal_id}"} <= {getattr(route, "path", "") for route in routes}

    assert hers_alone == [
        (HELP, ["POST"], "HersToAsk"),
        (SIGNALS, ["POST"], "HersToSignal"),
        (SIGNALS + "/{signal_id}", ["DELETE"], "HersToSignal"),
    ]
