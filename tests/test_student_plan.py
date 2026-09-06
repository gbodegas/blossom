"""Today's plan is hers: asked for from her page, shown the moment it is made, reviewed after.

The parent's review is shown under the plan and never stands between her and
it. These tests hold her page, her JSON routes, and the parent's review to
that, with scripted models so nothing is ever sent.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.drafts import Draft
from blossom.plans import DailyPlan
from blossom.routes.runs import plan_graphs
from blossom.settings import ANTHROPIC_API_KEY_VARIABLE
from blossom.stores.drafts import DraftsStore
from tests.support import (
    PLAN_DATE,
    accepting,
    drafts_in_memory,
    fixture_settings,
    fixture_week_plan,
    forgetful_fixture_plan,
    light_fixture_plan,
    scripted_graphs,
)

PAGE = "/student/due-this-week"
CREATED = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)


def draft(draft_id: str, body: str, *, hour: int = 22) -> Draft:
    return Draft(draft_id=draft_id, body=body, created_at=CREATED.replace(hour=hour))


def browser(*, key: bool = True, plan: Callable[[], DailyPlan] = fixture_week_plan) -> TestClient:
    """Her page with scripted models. ``key`` false is a household without an API key."""
    environ = {ANTHROPIC_API_KEY_VARIABLE: "not-a-key-and-never-sent"} if key else {}
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **environ))
    if key:
        app.dependency_overrides[plan_graphs] = scripted_graphs(
            lambda: [plan()], lambda: [accepting()]
        )
    return TestClient(app, follow_redirects=False)


# --------------------------------------------------------------- the store


def test_the_latest_plan_for_an_evening_is_read_whatever_was_said_about_it() -> None:
    store: DraftsStore = drafts_in_memory()
    first = draft("draft:plan:a", "First plan")
    second = draft("draft:plan:b", "Second plan", hour=23)
    store.record_waiting(first, thread_id="plan:a", plan_date=PLAN_DATE, outcome="accepted")
    store.record_waiting(second, thread_id="plan:b", plan_date=PLAN_DATE, outcome="accepted")
    store.record_decision(
        "draft:plan:b",
        status=first.status,
        decision="rejected",
        reason="try again",
    )

    latest = store.latest_for(PLAN_DATE)

    assert latest is not None
    assert latest.draft_id == "draft:plan:b"
    assert latest.decision == "rejected"
    assert store.latest_for(date(2026, 8, 20)) is None


# ---------------------------------------------------------------- her page


def test_her_page_offers_to_plan_today_and_says_why_it_cannot_without_a_key() -> None:
    with browser() as client:
        with_key = client.get(PAGE).text
    with browser(key=False) as client:
        without = client.get(PAGE).text

    assert "Plan today" in with_key
    assert 'action="/student/actions/plan"' in with_key
    assert "No plan for today yet." in with_key
    assert "no API key is configured" in without
    assert 'action="/student/actions/plan"' not in without
    assert "Too much right now" in without


def test_the_plan_is_on_her_page_the_moment_it_is_made() -> None:
    with browser() as client:
        planned = client.post("/student/actions/plan")
        page = client.get(PAGE).text
        today = client.get("/student/plans/today").json()
        queue = client.get("/parent/approvals").json()["waiting"]

    assert planned.status_code == 303
    assert planned.headers["location"] == PAGE
    assert "Plan for Wednesday, August 19, 2026" in page
    assert "A parent has not looked at this yet. You can start anyway." in page
    assert ">Plan again<" in page
    assert today["decision"] is None
    assert today["stale"] is None
    assert today["too_much"] is False
    assert [item["draft_id"] for item in queue] == [today["draft_id"]]


def test_asking_over_json_answers_with_the_plan() -> None:
    with browser() as client:
        made = client.post("/student/plans")
        today = client.get("/student/plans/today").json()

    assert made.status_code == 201
    assert made.json()["draft_id"] == today["draft_id"]
    assert made.json()["body"].startswith("Plan for Wednesday, August 19, 2026")


def test_without_a_plan_today_is_a_404() -> None:
    with browser() as client:
        response = client.get("/student/plans/today")

    assert response.status_code == 404


def test_without_a_key_the_page_says_so_and_keeps_working() -> None:
    with browser(key=False) as client:
        response = client.post("/student/actions/plan")
        over_json = client.post("/student/plans")

    assert response.status_code == 503
    assert "Blossom could not make a plan:" in response.text
    assert "<h1>Due this week</h1>" in response.text
    assert over_json.status_code == 503


def test_a_run_that_ends_without_a_plan_is_said_and_the_page_keeps_its_plan() -> None:
    app = create_app(
        fixture_settings(
            BLOSSOM_TODAY=PLAN_DATE.isoformat(), ANTHROPIC_API_KEY="not-a-key-and-never-sent"
        )
    )
    app.dependency_overrides[plan_graphs] = scripted_graphs(
        lambda: [forgetful_fixture_plan()] * 3, lambda: [accepting()]
    )
    with TestClient(app, follow_redirects=False) as client:
        response = client.post("/student/actions/plan")
        over_json = client.post("/student/plans")

    assert response.status_code == 409
    assert "No plan was made this time: the run ended with checks_failed." in response.text
    assert "No plan for today yet." in response.text
    assert over_json.status_code == 409


def test_a_failure_on_the_way_is_said_on_the_page_and_the_plan_stays() -> None:
    """A planner that raises is not a refusal; the page still says so and keeps its plan."""
    app = create_app(
        fixture_settings(
            BLOSSOM_TODAY=PLAN_DATE.isoformat(), ANTHROPIC_API_KEY="not-a-key-and-never-sent"
        )
    )
    app.dependency_overrides[plan_graphs] = scripted_graphs(
        lambda: [fixture_week_plan()], lambda: [accepting()]
    )
    with TestClient(app, follow_redirects=False) as client:
        client.post("/student/actions/plan")
        app.dependency_overrides[plan_graphs] = scripted_graphs(list, lambda: [accepting()])
        response = client.post("/student/actions/plan")
        today = client.get("/student/plans/today").json()

    assert response.status_code == 500
    assert (
        "Blossom could not make a plan: something went wrong on the way. "
        "The plan already here, if any, is unchanged."
    ) in response.text
    assert "Plan for Wednesday, August 19, 2026" in response.text
    assert today["decision"] is None


def test_planning_again_replaces_the_plan_on_both_pages() -> None:
    with browser() as client:
        client.post("/student/actions/plan")
        first = client.get("/student/plans/today").json()["draft_id"]
        client.post("/student/actions/plan")
        today = client.get("/student/plans/today").json()
        queue = client.get("/parent/approvals").json()["waiting"]
        earlier = client.get(f"/parent/approvals/{first}").json()

    assert today["draft_id"] != first
    assert [item["draft_id"] for item in queue] == [today["draft_id"]]
    assert earlier["decision"] == "superseded"


# ------------------------------------------------------- the parent's review


def test_a_parents_review_shows_under_her_plan_and_never_blocks_it() -> None:
    with browser() as client:
        client.post("/student/actions/plan")
        draft_id = client.get("/student/plans/today").json()["draft_id"]
        before = client.get(PAGE).text
        client.post(
            f"/parent/approvals/{draft_id}", json={"approved": True, "reason": "good pacing"}
        )
        after = client.get(PAGE).text
        today = client.get("/student/plans/today").json()

    assert "A parent has not looked at this yet." in before
    assert "A parent looked at this plan and said it looks good." in after
    assert "They said: <q>good pacing</q>" in after
    assert today["decision"] == "approved"


def test_a_change_asked_for_is_said_in_her_words() -> None:
    with browser() as client:
        client.post("/student/actions/plan")
        draft_id = client.get("/student/plans/today").json()["draft_id"]
        client.post(
            f"/parent/approvals/{draft_id}",
            json={"approved": False, "reason": "the essay needs two sittings"},
        )
        page = client.get(PAGE).text

    assert "<strong>A parent asked for a change.</strong> Plan again when you are ready." in page
    assert "They said: <q>the essay needs two sittings</q>" in page
    assert "Plan for Wednesday, August 19, 2026" in page


# --------------------------------------------------- the evening changing


def test_a_press_after_the_plan_tells_her_to_plan_again_in_her_words() -> None:
    with browser() as client:
        client.post("/student/actions/plan")
        client.post("/student/actions/too-much")
        page = client.get(PAGE).text
        today = client.get("/student/plans/today").json()

    assert today["stale"] == (
        "You said today is too much after this plan was made. Plan again to make it smaller."
    )
    assert (
        "<strong>Plan again.</strong> You said today is too much after this plan was made." in page
    )
    assert "Your next plan for today is held to 75 minutes instead of 150." in page


def test_her_page_measures_the_plan_against_the_evening_whatever_a_parent_said() -> None:
    """A parent's review is not a reason to keep a plan that has stopped fitting the evening."""
    with browser() as client:
        client.post("/student/actions/plan")
        draft_id = client.get("/student/plans/today").json()["draft_id"]
        client.post(f"/parent/approvals/{draft_id}", json={"approved": True})
        client.post("/student/actions/too-much")
        today = client.get("/student/plans/today").json()
        reviewed = client.get(f"/parent/approvals/{draft_id}").json()

    assert today["decision"] == "approved"
    assert today["stale"] == (
        "You said today is too much after this plan was made. Plan again to make it smaller."
    )
    assert reviewed["stale"] is None


def test_taking_the_signal_back_after_a_reduced_plan_says_so_in_her_words() -> None:
    with browser(plan=light_fixture_plan) as client:
        signal_id = client.post("/student/workload-signals").json()["signal"]["signal_id"]
        client.post("/student/actions/plan")
        reduced = client.get("/student/plans/today").json()
        client.delete(f"/student/workload-signals/{signal_id}")
        today = client.get("/student/plans/today").json()
        page = client.get(PAGE).text

    assert reduced["too_much"] is True
    assert reduced["stale"] is None
    assert today["stale"] == (
        "Your signal ended after this plan was made, and the plan was kept short for it. "
        "Plan again for the full evening."
    )
    assert "because you said today was too much" in page
