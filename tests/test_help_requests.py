"""Asking for help: one press from her page, a state a parent moves, each step shown to her."""

import pathlib
import sqlite3
from datetime import timedelta
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from pydantic import ValidationError

from blossom.app import create_app
from blossom.clock import FrozenClock
from blossom.dependencies import STATE_ATTRIBUTE, ApplicationState
from blossom.routes import student as student_routes
from blossom.stores.help_requests import (
    HELP_RETENTION_DAYS,
    NOTE_MAX_LENGTH,
    HelpRequestsStore,
    RequestClosed,
)
from tests.support import (
    HERS,
    OBSERVED_AT,
    PLAN_DATE,
    SAME_ORIGIN,
    THEIRS,
    ZONE,
    Answer,
    fixture_clock,
    fixture_settings,
    signed_in_household,
    whole_form,
)

PAGE = "/student/due-this-week"
ASK = "/student/actions/ask-for-help"
FORM_TYPE = "application/x-www-form-urlencoded"


def store_in_memory(clock: FrozenClock | None = None) -> HelpRequestsStore:
    return HelpRequestsStore(
        sqlite3.connect(":memory:", check_same_thread=False), clock or fixture_clock()
    )


def browser() -> TestClient:
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()))
    return TestClient(app, follow_redirects=False, headers=SAME_ORIGIN)


def ask_form(client: TestClient) -> dict[str, str]:
    """The fields her page's Ask for help form sends, a fresh id among them."""
    return whole_form(client.get(PAGE).text, ASK)


# ------------------------------------------------------------------ the store


def test_a_request_starts_as_requested_and_can_be_taken_back_until_a_parent_takes_it_up() -> None:
    store = store_in_memory()

    asked = store.ask(PLAN_DATE, "the essay outline")

    assert asked.state == "requested"
    assert asked.note == "the essay outline"
    assert asked.asked_at == fixture_clock().now()
    assert [item.request_id for item in store.open_requests()] == [asked.request_id]
    assert store.take_back(asked.request_id) is True
    assert store.open_requests() == []
    assert store.take_back(asked.request_id) is False


def test_taking_up_then_resolving_moves_the_state_and_keeps_the_words_back() -> None:
    store = store_in_memory()
    asked = store.ask(PLAN_DATE)

    taken_up = store.accept(asked.request_id, "on my way")
    again = store.accept(asked.request_id, "ignored")
    kept = store.get(asked.request_id)
    assert kept is not None
    assert kept.response == "on my way"
    resolved = store.resolve(asked.request_id, "we did the outline together")

    assert taken_up.state == "accepted"
    assert taken_up.accepted_at == fixture_clock().now()
    assert taken_up.response == "on my way"
    assert again == taken_up
    assert resolved.state == "resolved"
    assert resolved.resolved_at == fixture_clock().now()
    assert resolved.response == "we did the outline together"
    assert store.open_requests() == []
    assert [item.request_id for item in store.recently_resolved()] == [asked.request_id]


def test_a_request_taken_up_cannot_be_taken_back_and_a_resolved_one_cannot_move() -> None:
    store = store_in_memory()
    asked = store.ask(PLAN_DATE)
    store.accept(asked.request_id)

    with pytest.raises(RequestClosed, match="taken back"):
        store.take_back(asked.request_id)
    resolved = store.resolve(asked.request_id)
    with pytest.raises(RequestClosed, match="taken up"):
        store.accept(asked.request_id)
    with pytest.raises(RequestClosed, match="resolved again"):
        store.resolve(asked.request_id)
    with pytest.raises(KeyError):
        store.accept("nobody")

    assert resolved.accepted_at is not None


def test_resolving_straight_from_requested_stamps_both_steps() -> None:
    store = store_in_memory()
    asked = store.ask(PLAN_DATE)

    resolved = store.resolve(asked.request_id, "sorted")

    assert resolved.accepted_at == resolved.resolved_at == fixture_clock().now()


def test_a_resolved_request_is_kept_two_weeks_and_an_open_one_indefinitely() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    then = HelpRequestsStore(connection, FrozenClock(OBSERVED_AT, ZONE))
    resolved = then.ask(PLAN_DATE)
    then.resolve(resolved.request_id, "done")
    still_open = then.ask(PLAN_DATE, "never answered")
    later = OBSERVED_AT + timedelta(days=HELP_RETENTION_DAYS + 1)
    now = HelpRequestsStore(connection, FrozenClock(later, ZONE))

    before_sweep = now.recently_resolved()
    gone_before_sweep = now.get(resolved.request_id)
    with pytest.raises(KeyError):
        now.accept(resolved.request_id)
    swept = now.sweep()

    assert before_sweep == []
    assert gone_before_sweep is None
    assert swept == 1
    assert now.get(resolved.request_id) is None
    assert [item.request_id for item in now.open_requests()] == [still_open.request_id]


def test_her_words_are_capped_in_the_store() -> None:
    with pytest.raises(ValidationError):
        store_in_memory().ask(PLAN_DATE, "w" * (NOTE_MAX_LENGTH + 1))


def test_the_store_offers_no_way_to_read_a_pattern() -> None:
    offered = {name for name in dir(HelpRequestsStore) if not name.startswith("_")}

    assert offered == {
        "ask",
        "ask_once",
        "already_asked",
        "take_back",
        "accept",
        "resolve",
        "get",
        "open_requests",
        "recently_resolved",
        "sweep",
        "open",
        "close",
        "name",
        "retention_policy",
    }


# ------------------------------------------------------------- the routes


def test_asking_over_json_is_answered_with_the_request_as_kept() -> None:
    with browser() as client:
        response = client.post("/student/help-requests", json={"note": "the essay outline"})
        hers = client.get("/student/help-requests").json()
        parents = client.get("/parent/help-requests").json()

    assert response.status_code == 201
    body = response.json()
    assert body["principal"] == "STUDENT"
    assert body["request"]["state"] == "requested"
    assert body["request"]["note"] == "the essay outline"
    assert body["request"]["evening"] == "2026-08-19"
    assert [item["request_id"] for item in hers] == [body["request"]["request_id"]]
    assert parents == hers


def test_a_parent_takes_it_up_and_resolves_it_and_she_sees_each_step() -> None:
    with browser() as client:
        request_id = client.post("/student/help-requests").json()["request"]["request_id"]
        not_seen = client.get(PAGE).text
        taken_up = client.post(
            f"/parent/help-requests/{request_id}/accept", json={"response": "coming"}
        )
        on_it = client.get(PAGE).text
        resolved = client.post(
            f"/parent/help-requests/{request_id}/resolve", json={"response": "all sorted"}
        )
        after = client.get(PAGE).text
        again = client.post(f"/parent/help-requests/{request_id}/resolve")

    assert "Waiting for a parent to respond." in not_seen
    assert taken_up.status_code == 200
    assert taken_up.json()["state"] == "accepted"
    assert "<strong>A parent is on it.</strong> They said: <q>coming</q>" in on_it
    assert resolved.json()["state"] == "resolved"
    assert "<strong>Resolved.</strong> They said: <q>all sorted</q>" in after
    assert "A resolved request stays here for two weeks" in after
    assert again.status_code == 409


def test_she_can_take_a_request_back_only_while_nobody_has_taken_it_up() -> None:
    with browser() as client:
        first = client.post("/student/help-requests").json()["request"]["request_id"]
        second = client.post("/student/help-requests").json()["request"]["request_id"]
        client.post(f"/parent/help-requests/{second}/accept")
        taken_back = client.delete(f"/student/help-requests/{first}")
        refused = client.delete(f"/student/help-requests/{second}")
        gone = client.delete(f"/student/help-requests/{first}")
        listed = client.get("/student/help-requests").json()

    assert taken_back.status_code == 204
    assert refused.status_code == 409
    assert gone.status_code == 404
    assert [item["request_id"] for item in listed] == [second]


def test_an_unknown_request_and_a_move_that_is_not_one_of_the_two_are_refused() -> None:
    with browser() as client:
        unknown = client.post("/parent/help-requests/nobody/accept")
        request_id = client.post("/student/help-requests").json()["request"]["request_id"]
        odd_move = client.post(f"/parent/actions/help/{request_id}", data={"step": "shrug"})

    assert unknown.status_code == 404
    assert odd_move.status_code == 422
    assert "is not one of the two moves" in odd_move.text


def test_her_note_is_capped_at_the_boundary() -> None:
    with browser() as client:
        over_json = client.post(
            "/student/help-requests", json={"note": "w" * (NOTE_MAX_LENGTH + 1)}
        )
        over_form = client.post(ASK, data={**ask_form(client), "note": "w" * (NOTE_MAX_LENGTH + 1)})
        at_cap = client.post(ASK, data={**ask_form(client), "note": "w" * NOTE_MAX_LENGTH})
        word_back = client.post(
            "/parent/help-requests/nobody/resolve", json={"response": "w" * (NOTE_MAX_LENGTH + 1)}
        )

    assert over_json.status_code == 422
    assert over_form.status_code == 422
    assert f"A note is at most {NOTE_MAX_LENGTH} characters" in over_form.text
    assert at_cap.status_code == 303
    assert word_back.status_code == 422


def test_a_parents_word_back_is_capped_on_the_form_too() -> None:
    with browser() as client:
        request_id = client.post("/student/help-requests").json()["request"]["request_id"]
        over = client.post(
            f"/parent/actions/help/{request_id}",
            data={"step": "accept", "response": "w" * (NOTE_MAX_LENGTH + 1)},
        )
        still_requested = client.get("/parent/help-requests").json()[0]["state"]
        at_cap = client.post(
            f"/parent/actions/help/{request_id}",
            data={"step": "accept", "response": "w" * NOTE_MAX_LENGTH},
        )

    assert over.status_code == 422
    assert f"A reply is at most {NOTE_MAX_LENGTH} characters" in over.text
    assert "<h1>Family review</h1>" in over.text
    assert still_requested == "requested"
    assert at_cap.status_code == 303


# --------------------------------------------------------------- the pages


def test_her_page_offers_the_press_and_then_lists_the_request_with_a_way_back() -> None:
    with browser() as client:
        before = client.get(PAGE).text
        asked = client.post(ASK, data={**ask_form(client), "note": "  the outline  "})
        after = client.get(PAGE).text
        request_id = client.get("/student/help-requests").json()[0]["request_id"]
        taken_back = client.post(f"/student/actions/take-back-help/{request_id}")
        again = client.get(PAGE).text

    assert 'action="/student/actions/ask-for-help"' in before
    assert "You asked for help" not in before
    assert asked.status_code == 303
    assert asked.headers["location"] == PAGE
    assert "<strong>You asked for help</strong>" in after
    assert "<q>the outline</q>" in after
    assert "Waiting for a parent to respond." in after
    assert f'action="/student/actions/take-back-help/{request_id}"' in after
    assert '>Take it back<span class="visually-hidden">: your request from ' in after
    assert 'aria-label="Take back' not in after
    assert taken_back.status_code == 303
    assert "You asked for help" not in again


def test_the_parents_page_lists_what_is_open_and_moves_it_with_two_buttons() -> None:
    with browser() as client:
        client.post("/student/help-requests", json={"note": "the outline"})
        request_id = client.get("/parent/help-requests").json()[0]["request_id"]
        listed = client.get("/parent").text
        taken_up = client.post(
            f"/parent/actions/help/{request_id}", data={"step": "accept", "response": "coming"}
        )
        on_it = client.get("/parent").text
        resolved = client.post(
            f"/parent/actions/help/{request_id}", data={"step": "resolve", "response": "sorted"}
        )
        after = client.get("/parent").text
        hers = client.get(PAGE).text

    assert "<h2>Help she asked for</h2>" in listed
    assert "She said: <q>the outline</q>" in listed
    assert "Not taken up yet. Her page says it is waiting for a parent to respond." in listed
    assert 'value="accept"' in listed
    assert taken_up.status_code == 303
    assert (
        "<strong>Taken up.</strong> Her page says a parent is on it. "
        "Reply so far: <q>coming</q>" in on_it
    )
    assert 'value="accept"' not in on_it
    assert resolved.status_code == 303
    assert "No open help requests." in after
    assert "Resolved in the last two weeks" in after
    assert "Resolved with <q>sorted</q>" in after
    assert "<strong>Resolved.</strong> They said: <q>sorted</q>" in hers


def test_taking_back_a_request_that_is_not_there_is_said_on_her_page() -> None:
    with browser() as client:
        response = client.post("/student/actions/take-back-help/nobody")

    assert response.status_code == 404
    assert "That request is not here any more; nothing was changed." in response.text
    assert "<h1>My week</h1>" in response.text


def test_taking_back_a_request_a_parent_has_taken_up_is_said_on_her_page() -> None:
    with browser() as client:
        request_id = client.post("/student/help-requests").json()["request"]["request_id"]
        client.post(f"/parent/help-requests/{request_id}/accept")
        response = client.post(f"/student/actions/take-back-help/{request_id}")

    assert response.status_code == 409
    assert "cannot be taken back" in response.text
    assert "<h1>My week</h1>" in response.text


def test_requests_live_in_the_drafts_file_stamped_by_the_real_clock() -> None:
    with browser() as client:
        client.post("/student/help-requests")
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        held = state.help_requests.open_requests()

    assert len(held) == 1
    assert held[0].evening == PLAN_DATE
    assert held[0].asked_at.date() > PLAN_DATE


# ------------------------------------------------------------------ hers to ask, once per form

ALREADY_SENT = "That request was already sent."
FORM_USED = "This form was already used. Open a new help form to ask again."
FORM_SENT_OTHER_WORDS = "This form already sent a request"


def requests_held(client: TestClient) -> list[tuple[object, ...]]:
    state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
    found = state.help_requests._connection.execute("SELECT * FROM help_requests ORDER BY 1")
    return [tuple(row) for row in found.fetchall()]


def signed_in(tmp_path: pathlib.Path) -> TestClient:
    client = TestClient(
        create_app(signed_in_household(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    )
    client.__enter__()
    client.post("/sign-in", data={"passphrase": HERS})
    return client


def as_a_parent(client: TestClient) -> None:
    client.post("/sign-out")
    client.post("/sign-in", data={"passphrase": THEIRS})


def posted(client: TestClient, fields: list[tuple[str, str]]) -> Answer:
    return client.post(ASK, content=urlencode(fields), headers={"Content-Type": FORM_TYPE})


def test_a_parent_can_not_ask_or_take_back_in_her_name(tmp_path: pathlib.Path) -> None:
    client = signed_in(tmp_path)
    try:
        form = ask_form(client)
        hers = client.post(ASK, data={**ask_form(client), "note": "hers"})
        request_id = client.get("/student/help-requests").json()[0]["request_id"]
        as_a_parent(client)
        before = requests_held(client)
        page = client.get(PAGE).text
        asked = client.post(ASK, data={**form, "note": "from a parent"})
        asked_json = client.post("/student/help-requests", json={"note": "from a parent"})
        taken_back = client.post(f"/student/actions/take-back-help/{request_id}")
        taken_back_json = client.delete(f"/student/help-requests/{request_id}")
        after = requests_held(client)
    finally:
        client.__exit__(None, None, None)

    assert hers.status_code == 303
    for refused in (asked, asked_json, taken_back, taken_back_json):
        assert refused.status_code == 403
    assert after == before
    assert f'action="{ASK}"' not in page
    assert "/student/actions/take-back-help/" not in page
    assert "<q>hers</q>" in page
    assert 'href="/parent#help-she-asked-for"' in page


HELP_JSON = "/student/help-requests"
JSON_TYPE = {"Content-Type": "application/json"}
BROKEN_FORM = {"Content-Type": "multipart/form-data; boundary=synthetic"}
BROKEN_BODY = b"--synthetic\r\nContent-Disposition: form-data\r\n\r\nno end"
OVERLONG = b'{"note": "' + b"x" * (NOTE_MAX_LENGTH + 1) + b'"}'


async def unread(*args: object, **kwargs: object) -> None:
    msg = "the body was read before the reader was known"
    raise AssertionError(msg)


@pytest.mark.parametrize(
    ("where", "body", "headers"),
    [
        (ASK, BROKEN_BODY, BROKEN_FORM),
        (ASK, b"note=from+a+parent&note=twice", {"Content-Type": FORM_TYPE}),
        (ASK, b"", {"Content-Type": FORM_TYPE}),
        (HELP_JSON, b"{not json", JSON_TYPE),
        (HELP_JSON, OVERLONG, JSON_TYPE),
        (HELP_JSON, b'{"request_id": "not-an-id"}', JSON_TYPE),
        (HELP_JSON, b'{"status": "done"}', JSON_TYPE),
        (HELP_JSON, b'{"note": "from a parent"}', {"Content-Type": "text/plain"}),
        (HELP_JSON, b"", {}),
        (HELP_JSON, b"\xff\xfe{", JSON_TYPE),
    ],
    ids=[
        "form broken multipart",
        "form note twice",
        "form empty",
        "json malformed",
        "json overlong",
        "json bad id",
        "json unknown field",
        "json as plain text",
        "json empty",
        "json not utf-8",
    ],
)
def test_a_parent_is_refused_before_the_body_is_read(
    where: str,
    body: bytes,
    headers: dict[str, str],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = signed_in(tmp_path)
    try:
        as_a_parent(client)
        before = requests_held(client)
        monkeypatch.setattr(student_routes, "fields_of", unread)
        refused = client.post(where, content=body, headers=headers)
        monkeypatch.undo()
        after = requests_held(client)
    finally:
        client.__exit__(None, None, None)

    assert refused.status_code == 403
    assert "Sign in as the student to ask for help or take a request back." in refused.text
    assert after == before


@pytest.mark.parametrize(
    ("body", "headers", "status_code", "problem"),
    [
        (b"", {}, 201, None),
        (b"null", JSON_TYPE, 201, None),
        (b'{"note": "hers"}', JSON_TYPE, 201, None),
        (b'{"note": "hers"}', {"Content-Type": "application/json; charset=utf-8"}, 201, None),
        (b'{"note": "hers"}', {"Content-Type": "application/problem+json"}, 201, None),
        (b'{"note": "hers"}', {}, 422, ("model_attributes_type", ["body"])),
        (b"{not json", JSON_TYPE, 422, ("json_invalid", ["body", 1])),
        (OVERLONG, JSON_TYPE, 422, ("string_too_long", ["body", "note"])),
        (b'{"status": "done"}', JSON_TYPE, 422, ("extra_forbidden", ["body", "status"])),
        (
            b'{"request_id": "not-an-id"}',
            JSON_TYPE,
            422,
            ("string_pattern_mismatch", ["body", "request_id"]),
        ),
        (
            b'{"note": "hers"}',
            {"Content-Type": "text/plain"},
            422,
            ("model_attributes_type", ["body"]),
        ),
        (b"\xff\xfe{", JSON_TYPE, 400, None),
    ],
    ids=[
        "empty",
        "null",
        "json",
        "json with charset",
        "a json subtype",
        "no content type",
        "malformed",
        "overlong",
        "unknown field",
        "bad id",
        "plain text",
        "not utf-8",
    ],
)
def test_her_json_ask_reads_its_body_as_the_framework_does(
    body: bytes,
    headers: dict[str, str],
    status_code: int,
    problem: tuple[str, list[str | int]] | None,
    tmp_path: pathlib.Path,
) -> None:
    client = signed_in(tmp_path)
    try:
        answer = client.post(HELP_JSON, content=body, headers=headers)
        held = requests_held(client)
    finally:
        client.__exit__(None, None, None)

    assert answer.status_code == status_code
    assert len(held) == (1 if status_code == 201 else 0)
    if problem is not None:
        first = answer.json()["detail"][0]
        assert (first["type"], first["loc"]) == problem


def test_she_can_not_take_up_or_resolve_her_own_request(tmp_path: pathlib.Path) -> None:
    client = signed_in(tmp_path)
    try:
        client.post(ASK, data={**ask_form(client), "note": "hers"})
        request_id = client.get("/student/help-requests").json()[0]["request_id"]
        before = requests_held(client)
        answers = [
            client.post(f"/parent/actions/help/{request_id}", data={"step": "accept"}),
            client.post(f"/parent/actions/help/{request_id}", data={"step": "resolve"}),
            client.post(f"/parent/help-requests/{request_id}/accept", json={}),
            client.post(f"/parent/help-requests/{request_id}/resolve", json={}),
        ]
        after = requests_held(client)
    finally:
        client.__exit__(None, None, None)

    assert all(answer.status_code == 403 for answer in answers)
    assert after == before


FIRST = ("note", "First synthetic words")
NOT_WHOLE_SAID = (
    "That form carried a field twice, left one out, or had one this page doesn't send, "
    "so nothing was sent."
)


@pytest.mark.parametrize(
    "shape",
    ["note twice", "no id", "id twice", "bad id", "unknown field"],
)
def test_an_ask_form_that_is_not_whole_sends_nothing_and_keeps_her_first_words(
    shape: str,
) -> None:
    with browser() as client:
        form_id = ask_form(client)["request_id"]
        fields = {
            "note twice": [FIRST, ("note", "Second synthetic words"), ("request_id", form_id)],
            "no id": [FIRST],
            "id twice": [FIRST, ("request_id", form_id), ("request_id", form_id)],
            "bad id": [FIRST, ("request_id", "not-an-id")],
            "unknown field": [FIRST, ("request_id", form_id), ("channel", "LMS")],
        }[shape]
        answer = posted(client, fields)
        again = whole_form(answer.text, ASK)
        held = requests_held(client)

    assert answer.status_code == 422
    assert held == []
    assert str(escape(NOT_WHOLE_SAID)) in answer.text
    assert again["note"] == "First synthetic words"
    assert "Second synthetic words" not in answer.text
    assert again["request_id"] not in ("", "not-an-id", form_id)


def test_a_note_sent_as_a_file_sends_nothing_and_shows_none_of_it() -> None:
    with browser() as client:
        form = ask_form(client)
        answer = client.post(
            ASK,
            data={"request_id": form["request_id"]},
            files={"note": ("note.txt", b"file words", "text/plain")},
        )
        held = requests_held(client)

    assert answer.status_code == 422
    assert "file words" not in answer.text
    assert str(escape(NOT_WHOLE_SAID)) in answer.text
    assert held == []


def test_an_overlong_note_keeps_her_words_by_the_field() -> None:
    words = "Zebra quartz " * 40 + "violin"
    with browser() as client:
        answer = client.post(ASK, data={**ask_form(client), "note": words})
        held = requests_held(client)
    start = answer.text.index('id="help-note"')
    field = answer.text[answer.text.rindex("<input", 0, start) : answer.text.index(">", start)]

    assert answer.status_code == 422
    assert held == []
    assert f'value="{words}"' in field
    assert 'aria-invalid="true"' in field
    assert "help-problem" in field
    assert "autofocus" in field
    assert f"A note is at most {NOTE_MAX_LENGTH} characters" in answer.text


def test_a_send_the_file_refuses_keeps_her_words(monkeypatch: pytest.MonkeyPatch) -> None:
    with browser() as client:
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]

        def refuses(*_: object, **__: object) -> None:
            msg = "disk I/O error"
            raise sqlite3.OperationalError(msg)

        form = ask_form(client)
        monkeypatch.setattr(state.help_requests, "ask_once", refuses)
        answer = client.post(ASK, data={**form, "note": "Zebra quartz violin"})
        monkeypatch.undo()
        held = requests_held(client)

    assert answer.status_code == 500
    assert 'value="Zebra quartz violin"' in answer.text
    assert "could not be sent" in answer.text
    assert held == []


def test_the_same_form_sent_again_makes_one_request() -> None:
    with browser() as client:
        form = {**ask_form(client), "note": "the outline"}
        first = client.post(ASK, data=form)
        again = client.post(ASK, data=form)
        landed = client.get(again.headers["location"]).text
        held = requests_held(client)

    assert first.status_code == 303
    assert again.status_code == 303
    assert len(held) == 1
    assert landed.count(ALREADY_SENT) == 1


def test_the_same_form_after_a_parent_took_it_up_shows_it_as_it_stands() -> None:
    with browser() as client:
        form = {**ask_form(client), "note": "the outline"}
        client.post(ASK, data=form)
        request_id = client.get("/student/help-requests").json()[0]["request_id"]
        client.post(f"/parent/help-requests/{request_id}/accept", json={"response": "After dinner"})
        before = requests_held(client)
        again = client.post(ASK, data=form)
        landed = client.get(again.headers["location"]).text
        after = requests_held(client)

    assert again.status_code == 303
    assert after == before
    assert ALREADY_SENT in landed
    assert "A parent is on it." in landed


def test_the_same_form_with_other_words_is_refused_and_keeps_them() -> None:
    with browser() as client:
        form = ask_form(client)
        client.post(ASK, data={**form, "note": "the outline"})
        refused = client.post(ASK, data={**form, "note": "the conclusion too"})
        again = whole_form(refused.text, ASK)
        held = requests_held(client)

    assert refused.status_code == 409
    assert FORM_SENT_OTHER_WORDS in refused.text
    assert again["note"] == "the conclusion too"
    assert again["request_id"] != form["request_id"]
    assert len(held) == 1


def test_a_form_whose_request_was_taken_back_asks_nothing_again() -> None:
    with browser() as client:
        form = {**ask_form(client), "note": "the outline"}
        client.post(ASK, data=form)
        request_id = client.get("/student/help-requests").json()[0]["request_id"]
        client.post(f"/student/actions/take-back-help/{request_id}")
        replayed = client.post(ASK, data=form)
        again = whole_form(replayed.text, ASK)
        held = requests_held(client)

    assert replayed.status_code == 409
    assert FORM_USED in replayed.text
    assert again["note"] == "the outline"
    assert again["request_id"] != form["request_id"]
    assert held == []


def test_a_fresh_form_with_the_same_words_asks_again() -> None:
    with browser() as client:
        client.post(ASK, data={**ask_form(client), "note": "the outline"})
        client.post(ASK, data={**ask_form(client), "note": "the outline"})
        held = requests_held(client)

    assert len(held) == 2


def test_json_with_an_id_asks_once_and_without_one_asks_every_time() -> None:
    with browser() as client:
        form_id = ask_form(client)["request_id"]
        made = client.post("/student/help-requests", json={"note": "json", "request_id": form_id})
        same = client.post("/student/help-requests", json={"note": "json", "request_id": form_id})
        other = client.post("/student/help-requests", json={"note": "else", "request_id": form_id})
        malformed = client.post("/student/help-requests", json={"request_id": "not-an-id"})
        client.delete(f"/student/help-requests/{form_id}")
        spent = client.post("/student/help-requests", json={"note": "json", "request_id": form_id})
        plain = [client.post("/student/help-requests", json={"note": "again"}) for _ in range(2)]
        held = requests_held(client)

    assert (made.status_code, same.status_code) == (201, 200)
    assert same.json()["request"]["request_id"] == form_id
    assert (other.status_code, malformed.status_code, spent.status_code) == (409, 422, 409)
    assert [answer.status_code for answer in plain] == [201, 201]
    assert len(held) == 2
