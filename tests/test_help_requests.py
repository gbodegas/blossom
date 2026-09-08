"""Asking for help: one press from her page, a state a parent moves, each step shown to her."""

import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from blossom.app import create_app
from blossom.clock import FrozenClock
from blossom.dependencies import STATE_ATTRIBUTE, ApplicationState
from blossom.stores.help_requests import (
    HELP_RETENTION_DAYS,
    NOTE_MAX_LENGTH,
    HelpRequestsStore,
    RequestClosed,
)
from tests.support import OBSERVED_AT, PLAN_DATE, ZONE, fixture_clock, fixture_settings

PAGE = "/student/due-this-week"


def store_in_memory(clock: FrozenClock | None = None) -> HelpRequestsStore:
    return HelpRequestsStore(
        sqlite3.connect(":memory:", check_same_thread=False), clock or fixture_clock()
    )


def browser() -> TestClient:
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()))
    return TestClient(app, follow_redirects=False)


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
        over_form = client.post(
            "/student/actions/ask-for-help", data={"note": "w" * (NOTE_MAX_LENGTH + 1)}
        )
        at_cap = client.post("/student/actions/ask-for-help", data={"note": "w" * NOTE_MAX_LENGTH})
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
        asked = client.post("/student/actions/ask-for-help", data={"note": "  the outline  "})
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
    assert "Nothing asked." in after
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
