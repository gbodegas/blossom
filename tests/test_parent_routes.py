"""The parent's routes, end to end: start a run, see the queue, decide.

The graph is substituted through the route's own dependency, so these tests
exercise the real application state, the real drafts table, and the real
saved-state store, with only the two model calls scripted.
"""

from collections.abc import Callable
from datetime import date, time
from typing import Annotated

from fastapi import Depends
from fastapi.testclient import TestClient

from blossom.agent.graph import CompiledPlanGraph, plan_graph_for
from blossom.app import create_app
from blossom.dependencies import ApplicationState, get_application_state
from blossom.drafts import DraftStatus
from blossom.heuristic_relevance import Criterion, CriterionFinding, CriticVerdict, Judgment
from blossom.plans import DailyPlan, Deferral, PlanBlock
from blossom.routes.parent import get_plan_graph
from blossom.settings import ANTHROPIC_API_KEY_VARIABLE
from tests.support import Scripted, fixture_settings, ok

PLAN_DATE = date(2026, 8, 19)


def good_plan() -> DailyPlan:
    """A plan that passes every check against the fixture week."""
    return DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            PlanBlock(
                assignment_id="assignment-canal-essay",
                starts_at=time(16, 30),
                ends_at=time(17, 30),
                rationale="the essay first, while she is fresh",
            ),
            PlanBlock(
                assignment_id="assignment-science-fair-proposal",
                starts_at=time(18, 0),
                ends_at=time(18, 30),
                rationale="nobody has confirmed this date, so it gets done tonight",
            ),
        ],
        deferred=[Deferral(assignment_id="assignment-algebra-set", reason="not due until Monday")],
    )


def accepting() -> CriticVerdict:
    return CriticVerdict(
        findings=[
            CriterionFinding(criterion=criterion, critique="reads well", judgment=Judgment.PASSES)
            for criterion in Criterion
        ]
    )


def forgetful_plan() -> DailyPlan:
    """Leaves two assignments unmentioned, so tier one fails every round."""
    return DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            PlanBlock(
                assignment_id="assignment-canal-essay",
                starts_at=time(16, 30),
                ends_at=time(17, 30),
                rationale="the essay first",
            )
        ],
    )


def scripted(
    planner: Callable[[], list[DailyPlan]], critic: Callable[[], list[CriticVerdict]]
) -> Callable[..., CompiledPlanGraph]:
    """A replacement for the route's graph dependency, over the app's own stores."""

    def override(
        state: Annotated[ApplicationState, Depends(get_application_state)],
    ) -> CompiledPlanGraph:
        return plan_graph_for(
            state,
            planner=Scripted(*[ok(plan) for plan in planner()]),
            critic=Scripted(*[ok(verdict) for verdict in critic()]),
        )

    return override


def app_with(
    planner: Callable[[], list[DailyPlan]] = lambda: [good_plan()],
    critic: Callable[[], list[CriticVerdict]] = lambda: [accepting()],
) -> TestClient:
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()))
    app.dependency_overrides[get_plan_graph] = scripted(planner, critic)
    return TestClient(app)


# -------------------------------------------------------------- starting a run


def test_a_run_pauses_at_the_gate_and_reports_the_draft() -> None:
    with app_with() as client:
        response = client.post("/parent/plans", json={})

    assert response.status_code == 201
    body = response.json()
    assert body["plan_date"] == "2026-08-19"
    assert body["outcome"] == "accepted"
    assert body["waiting"] is True
    assert body["thread_id"].startswith("plan:2026-08-19:")
    assert body["draft_id"] == f"draft:{body['thread_id']}"


def test_the_evening_defaults_to_today_in_the_household_zone_and_can_be_named() -> None:
    with app_with() as client:
        today = client.post("/parent/plans", json={}).json()
        named = client.post("/parent/plans", json={"plan_date": "2026-08-20"}).json()

    assert today["plan_date"] == "2026-08-19"
    assert named["plan_date"] == "2026-08-20"


def test_a_request_with_an_unknown_field_is_refused() -> None:
    with app_with() as client:
        response = client.post("/parent/plans", json={"plan_date": "2026-08-19", "send": True})

    assert response.status_code == 422


def test_a_run_that_never_passes_the_checks_queues_nothing() -> None:
    with app_with(planner=lambda: [forgetful_plan()] * 3, critic=list) as client:
        response = client.post("/parent/plans", json={})
        queue = client.get("/parent/approvals").json()

    body = response.json()
    assert response.status_code == 201
    assert body["outcome"] == "checks_failed"
    assert body["waiting"] is False
    assert body["draft_id"] is None
    assert queue["waiting"] == []


# ------------------------------------------------------------------ the queue


def test_the_queue_shows_the_waiting_draft_with_its_text() -> None:
    with app_with() as client:
        started = client.post("/parent/plans", json={}).json()
        queue = client.get("/parent/approvals").json()
        detail = client.get(f"/parent/approvals/{started['draft_id']}").json()

    assert [item["draft_id"] for item in queue["waiting"]] == [started["draft_id"]]
    assert detail["status"] == "DRAFT"
    assert detail["outcome"] == "accepted"
    assert detail["decision"] is None
    assert detail["body"].startswith("Plan for Wednesday, August 19")
    assert "Canal Era comparison essay" in detail["body"]
    assert "Waiting for another day:" in detail["body"]
    assert "thread_id" not in detail


def test_two_runs_wait_in_the_order_they_were_started() -> None:
    with app_with() as client:
        first = client.post("/parent/plans", json={}).json()
        second = client.post("/parent/plans", json={}).json()
        queue = client.get("/parent/approvals").json()

    assert [item["draft_id"] for item in queue["waiting"]] == [
        first["draft_id"],
        second["draft_id"],
    ]


def test_an_unknown_draft_is_not_found() -> None:
    with app_with() as client:
        assert client.get("/parent/approvals/draft:nobody").status_code == 404
        response = client.post("/parent/approvals/draft:nobody", json={"approved": True})

    assert response.status_code == 404


# ---------------------------------------------------------------- deciding


def test_approving_marks_the_draft_for_manual_send_and_clears_the_queue() -> None:
    with app_with() as client:
        started = client.post("/parent/plans", json={}).json()
        decided = client.post(
            f"/parent/approvals/{started['draft_id']}",
            json={"approved": True, "reason": "looks right"},
        )
        queue = client.get("/parent/approvals").json()
        detail = client.get(f"/parent/approvals/{started['draft_id']}").json()

    assert decided.status_code == 200
    body = decided.json()
    assert body["status"] == DraftStatus.APPROVED_FOR_MANUAL_SEND.value
    assert body["decision"] == "approved"
    assert body["reason"] == "looks right"
    assert body["decided_at"] is not None
    assert queue["waiting"] == []
    assert detail["decision"] == "approved"
    assert detail["status"] == DraftStatus.APPROVED_FOR_MANUAL_SEND.value


def test_refusing_leaves_the_draft_a_draft_and_records_why() -> None:
    with app_with() as client:
        started = client.post("/parent/plans", json={}).json()
        decided = client.post(
            f"/parent/approvals/{started['draft_id']}",
            json={"approved": False, "reason": "too late in the evening"},
        ).json()

    assert decided["status"] == DraftStatus.DRAFT.value
    assert decided["decision"] == "rejected"
    assert decided["reason"] == "too late in the evening"


def test_only_a_literal_true_approves() -> None:
    """The gate reads exactly ``True``; the request model makes anything else a 422."""
    with app_with() as client:
        started = client.post("/parent/plans", json={}).json()
        response = client.post(f"/parent/approvals/{started['draft_id']}", json={"approved": "yes"})

    assert response.status_code == 422


def test_a_draft_cannot_be_decided_twice() -> None:
    with app_with() as client:
        started = client.post("/parent/plans", json={}).json()
        client.post(f"/parent/approvals/{started['draft_id']}", json={"approved": True})
        again = client.post(f"/parent/approvals/{started['draft_id']}", json={"approved": False})

    assert again.status_code == 409
    assert "already approved" in again.json()["detail"]


# --------------------------------------------------------------- without a key


def test_the_queue_reads_without_a_model_and_a_run_says_why_it_cannot_start() -> None:
    """Reading what waits needs no key. Starting or deciding does, and says so."""
    settings = fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat())
    assert settings.anthropic_api_key is None, ANTHROPIC_API_KEY_VARIABLE

    with TestClient(create_app(settings)) as client:
        queue = client.get("/parent/approvals")
        started = client.post("/parent/plans", json={})

    assert queue.status_code == 200
    assert queue.json()["waiting"] == []
    assert started.status_code == 503
    assert ANTHROPIC_API_KEY_VARIABLE in started.json()["detail"]
