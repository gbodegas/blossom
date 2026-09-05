"""The parent's routes, end to end: start a run, see the queue, decide.

The graph is substituted through the route's own dependency, so these tests
exercise the real application state, the real drafts table, and the real
saved-state store, with only the two model calls scripted.
"""

import asyncio
from collections.abc import Callable
from datetime import date, time
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from blossom.agent.graph import CompiledPlanGraph, PlanState, plan_graph_for
from blossom.agent.runs import DURABILITY, run_config
from blossom.app import create_app
from blossom.dependencies import ApplicationState, build_application_state, get_application_state
from blossom.drafts import DraftStatus
from blossom.heuristic_relevance import Criterion, CriterionFinding, CriticVerdict, Judgment
from blossom.plans import DailyPlan, Deferral, PlanBlock
from blossom.routes.parent import DecisionRequest, PlanGraphs, decide_draft, plan_graphs
from blossom.settings import ANTHROPIC_API_KEY_VARIABLE
from blossom.views import DecisionView
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
        deferred=[
            Deferral(assignment_id="assignment-algebra-set", reason="not due until Monday"),
            Deferral(
                assignment_id="assignment-textbook-cover", reason="five minutes on the weekend"
            ),
            Deferral(assignment_id="assignment-reading-log", reason="a page a night is on track"),
            Deferral(assignment_id="assignment-signed-syllabus", reason="ask what the date is"),
        ],
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
) -> Callable[..., PlanGraphs]:
    """A replacement for the route's graphs dependency, over the app's own stores.

    Scripted models, and permission to start, so a run can be driven in an
    application that has no key; the models are never asked for one.
    """

    def override(
        state: Annotated[ApplicationState, Depends(get_application_state)],
    ) -> PlanGraphs:
        return PlanGraphs(
            build=lambda: plan_graph_for(
                state,
                planner=Scripted(*[ok(plan) for plan in planner()]),
                critic=Scripted(*[ok(verdict) for verdict in critic()]),
            ),
            may_start=True,
        )

    return override


def app_with(
    planner: Callable[[], list[DailyPlan]] = lambda: [good_plan()],
    critic: Callable[[], list[CriticVerdict]] = lambda: [accepting()],
) -> TestClient:
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()))
    app.dependency_overrides[plan_graphs] = scripted(planner, critic)
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


def test_the_table_answers_before_a_model_is_needed() -> None:
    """A dependency resolves before the handler, so the graph is built inside it:
    without a key, an unknown draft is still a 404 and a decided one a 409."""
    with app_with() as client:
        started = client.post("/parent/plans", json={}).json()
        client.post(f"/parent/approvals/{started['draft_id']}", json={"approved": True})
        client.app.dependency_overrides.clear()  # type: ignore[attr-defined]

        unknown = client.post("/parent/approvals/draft:nobody", json={"approved": True})
        decided = client.post(f"/parent/approvals/{started['draft_id']}", json={"approved": False})
        fresh = client.post("/parent/plans", json={})

    assert unknown.status_code == 404
    assert decided.status_code == 409
    assert fresh.status_code == 503


def test_deciding_needs_no_key_because_nothing_past_the_gate_asks_a_model() -> None:
    """A thread paused on a machine with a key can be decided on one without."""
    with app_with() as client:
        started = client.post("/parent/plans", json={}).json()
        client.app.dependency_overrides.clear()  # type: ignore[attr-defined]

        decided = client.post(
            f"/parent/approvals/{started['draft_id']}",
            json={"approved": True, "reason": "decided without a key"},
        )
        record = client.get(f"/parent/approvals/{started['draft_id']}").json()

    assert decided.status_code == 200
    assert record["status"] == DraftStatus.APPROVED_FOR_MANUAL_SEND.value
    assert record["reason"] == "decided without a key"


def test_two_decisions_at_once_leave_one_winner_and_tell_the_other() -> None:
    """The route's whole sequence runs under one lock, so the second request sees
    the draft decided rather than racing the first into the table."""
    state = build_application_state(
        fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()), InMemorySaver()
    )
    try:

        def build() -> CompiledPlanGraph:
            return plan_graph_for(
                state, planner=Scripted(ok(good_plan())), critic=Scripted(ok(accepting()))
            )

        async def scenario() -> tuple[str, list[object]]:
            config = run_config("plan:2026-08-19:race")
            paused = await build().ainvoke(
                PlanState(plan_date=PLAN_DATE, rounds=0), config=config, durability=DURABILITY
            )
            draft_id = str(paused["draft"].draft_id)
            approve = decide_draft(
                state, build, draft_id, DecisionRequest(approved=True, reason="first")
            )
            reject = decide_draft(
                state, build, draft_id, DecisionRequest(approved=False, reason="second")
            )
            outcomes = await asyncio.gather(approve, reject, return_exceptions=True)
            return draft_id, list(outcomes)

        draft_id, outcomes = asyncio.run(scenario())
        record = state.drafts.get(draft_id)
    finally:
        state.close()

    winners = [outcome for outcome in outcomes if isinstance(outcome, DecisionView)]
    refusals = [outcome for outcome in outcomes if isinstance(outcome, HTTPException)]
    assert len(winners) == 1
    assert len(refusals) == 1
    assert refusals[0].status_code == 409
    assert record is not None
    assert record.decision == winners[0].decision
    assert record.reason == winners[0].reason


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
