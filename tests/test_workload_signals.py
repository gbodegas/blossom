"""The workload signal: one press, a visible result, a reduced plan, brief and hers to remove.

The design's third tier of verification is whether a plan is right for her,
which no check can answer; her signal overrides the plan directly. These tests
hold the store, the graph, the routes, and the page to that.
"""

import asyncio
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from blossom.agent.graph import CompiledPlanGraph, PlanState
from blossom.agent.runs import DURABILITY, run_config
from blossom.app import create_app
from blossom.clock import FrozenClock, spoken_time
from blossom.dependencies import STATE_ATTRIBUTE, ApplicationState
from blossom.plan_checks import PlanCheck
from blossom.plans import DailyPlan, Deferral, PlanBlock
from blossom.routes.runs import plan_graphs
from blossom.routes.student import templates
from blossom.settings import DEFAULT_EVENING_MINUTES
from blossom.stores.workload_signals import (
    DETAIL_MAX_LENGTH,
    SIGNAL_RETENTION_DAYS,
    WorkloadSignalsStore,
)
from blossom.views import StudentDueThisWeekView, WeekView, WorkloadSignalView
from tests.support import (
    FIXTURE_TIMEZONE,
    OBSERVED_AT,
    PLAN_DATE,
    Scripted,
    accepting,
    fixture_clock,
    fixture_settings,
    good_plan,
    graph_with,
    human_text,
    light_fixture_plan,
    ok,
    scripted_graphs,
    signals_in_memory,
)

ZONE = ZoneInfo(FIXTURE_TIMEZONE)


def store_in_memory(clock: FrozenClock | None = None) -> WorkloadSignalsStore:
    return WorkloadSignalsStore(
        sqlite3.connect(":memory:", check_same_thread=False), clock or fixture_clock()
    )


def run(graph: CompiledPlanGraph, thread: str = "plan:signaled") -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        return dict(
            await graph.ainvoke(
                PlanState(plan_date=PLAN_DATE, rounds=0),
                config=run_config(thread),
                durability=DURABILITY,
            )
        )

    return asyncio.run(go())


def long_plan() -> DailyPlan:
    """Ninety minutes: inside the usual evening, over the reduced one."""
    return DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            PlanBlock(
                assignment_id="assignment-canal-essay",
                starts_at=time(16, 30),
                ends_at=time(18, 0),
                rationale="the essay in one sitting",
            )
        ],
        deferred=[Deferral(assignment_id="assignment-algebra-set", reason="not due until Monday")],
    )


# ------------------------------------------------------------------ the store


def test_both_budgets_are_the_households_numbers() -> None:
    """What an evening holds, and what it holds once she has said too much, are
    settings handed to the graph, not rules of the system."""
    signals = signals_in_memory()
    signals.record(PLAN_DATE)
    critic = Scripted(ok(accepting()), ok(accepting()))

    signaled = run(
        graph_with(
            Scripted(ok(good_plan())),
            critic,
            signals=signals,
            evening_minutes=200,
            too_much_minutes=100,
        )
    )
    usual = run(
        graph_with(Scripted(ok(good_plan())), critic, evening_minutes=200, too_much_minutes=100),
        thread="plan:usual",
    )

    assert signaled["budget_minutes"] == 100
    assert signaled["steps"][0].found.endswith("so the budget is 100 minutes")
    assert usual["budget_minutes"] == 200


def test_a_press_is_kept_for_its_evening_and_can_be_taken_back() -> None:
    store = store_in_memory()

    signal = store.record(PLAN_DATE)

    assert [item.signal_id for item in store.for_evening(PLAN_DATE)] == [signal.signal_id]
    assert store.for_evening(PLAN_DATE + timedelta(days=1)) == []
    assert signal.detail is None
    assert signal.given_at == fixture_clock().now()
    assert store.withdraw(signal.signal_id) is True
    assert store.for_evening(PLAN_DATE) == []
    assert store.withdraw(signal.signal_id) is False


def test_words_she_adds_are_kept_but_never_required() -> None:
    store = store_in_memory()

    with_words = store.record(PLAN_DATE, "everything at once")
    without = store.record(PLAN_DATE)

    kept = {item.signal_id: item.detail for item in store.held()}
    assert kept == {with_words.signal_id: "everything at once", without.signal_id: None}


def test_everything_kept_is_listed_most_recent_first() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    store = WorkloadSignalsStore(connection, FrozenClock(OBSERVED_AT, ZONE))
    earlier = store.record(PLAN_DATE - timedelta(days=1))
    an_hour_on = WorkloadSignalsStore(
        connection, FrozenClock(OBSERVED_AT + timedelta(hours=1), ZONE)
    )
    later = an_hour_on.record(PLAN_DATE)

    assert [item.signal_id for item in store.held()] == [later.signal_id, earlier.signal_id]


def test_old_signals_are_swept_and_recent_ones_kept() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    then = WorkloadSignalsStore(connection, FrozenClock(OBSERVED_AT, ZONE))
    old = then.record(PLAN_DATE)
    later = OBSERVED_AT + timedelta(days=SIGNAL_RETENTION_DAYS + 1)
    now = WorkloadSignalsStore(connection, FrozenClock(later, ZONE))
    fresh = now.record(PLAN_DATE + timedelta(days=SIGNAL_RETENTION_DAYS + 1))

    removed = now.sweep()

    assert removed == 1
    assert [item.signal_id for item in now.held()] == [fresh.signal_id]
    assert now.withdraw(old.signal_id) is False


def test_the_store_offers_no_way_to_read_a_pattern() -> None:
    """One question for the planner, one list for her, and nothing grouped by day."""
    offered = {name for name in dir(WorkloadSignalsStore) if not name.startswith("_")}

    assert offered == {
        "record",
        "withdraw",
        "for_evening",
        "held",
        "sweep",
        "open",
        "close",
        "name",
        "retention_policy",
    }


# ------------------------------------------------------------------ the graph


def test_a_signal_cuts_the_budget_before_the_planner_is_asked() -> None:
    signals = signals_in_memory()
    signals.record(PLAN_DATE)
    planner = Scripted(ok(good_plan()))

    result = run(graph_with(planner, Scripted(ok(accepting())), signals=signals))

    brief = human_text(planner.briefs[0])
    assert result["too_much"] is True
    assert result["budget_minutes"] == 75
    assert "<budget_minutes>75</budget_minutes>" in brief
    assert "<too_much>" in brief
    assert "She said today is too much." in brief
    assert result["steps"][0].found.endswith(
        "she said today is too much, so the budget is 75 minutes"
    )


def test_without_a_signal_the_evening_is_the_usual_length() -> None:
    planner = Scripted(ok(good_plan()))

    result = run(graph_with(planner, Scripted(ok(accepting()))))

    assert result["too_much"] is False
    assert result["budget_minutes"] == DEFAULT_EVENING_MINUTES
    assert "<too_much>" not in human_text(planner.briefs[0])


def test_a_plan_that_fits_the_usual_evening_fails_the_reduced_one() -> None:
    signals = signals_in_memory()
    signals.record(PLAN_DATE)
    planner = Scripted(*[ok(long_plan())] * 3)

    result = run(graph_with(planner, Scripted(), signals=signals))

    assert result["outcome"] == "checks_failed"
    assert result["verification"].failed_checks == (PlanCheck.WITHIN_TIME_BUDGET,)
    assert result["feedback"] == ["the plan asks for 90 minutes and the evening allows 75"]


def test_the_same_plan_passes_when_she_has_not_signaled() -> None:
    result = run(graph_with(Scripted(ok(long_plan())), Scripted(ok(accepting()))))

    assert result["outcome"] == "accepted"


def test_the_critic_and_the_draft_are_told() -> None:
    signals = signals_in_memory()
    signals.record(PLAN_DATE)
    critic = Scripted(ok(accepting()))

    result = run(graph_with(Scripted(ok(good_plan())), critic, signals=signals))

    assert "<too_much>" in human_text(critic.briefs[0])
    body = result["__interrupt__"][0].value["body"]
    assert "You said today was too much, so this plan is kept to 75 minutes." in body


def test_a_signal_about_another_evening_changes_nothing_tonight() -> None:
    signals = signals_in_memory()
    signals.record(PLAN_DATE - timedelta(days=1))

    result = run(graph_with(Scripted(ok(good_plan())), Scripted(ok(accepting())), signals=signals))

    assert result["too_much"] is False
    assert result["budget_minutes"] == DEFAULT_EVENING_MINUTES


# ------------------------------------------------------- the routes and the page


def browser() -> TestClient:
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()))
    app.dependency_overrides[plan_graphs] = scripted_graphs(
        lambda: [light_fixture_plan()], lambda: [accepting()]
    )
    return TestClient(app, follow_redirects=False)


def test_a_press_is_answered_with_what_it_changed() -> None:
    with browser() as client:
        response = client.post("/student/workload-signals")
        listed = client.get("/student/workload-signals").json()

    assert response.status_code == 201
    body = response.json()
    assert body["principal"] == "STUDENT"
    assert body["signal"]["detail"] is None
    assert body["signal"]["evening"] == "2026-08-19"
    assert [item["signal_id"] for item in listed] == [body["signal"]["signal_id"]]


def test_a_signal_can_be_deleted_once_and_is_then_gone() -> None:
    with browser() as client:
        signal_id = client.post("/student/workload-signals").json()["signal"]["signal_id"]
        first = client.delete(f"/student/workload-signals/{signal_id}")
        second = client.delete(f"/student/workload-signals/{signal_id}")
        listed = client.get("/student/workload-signals").json()

    assert first.status_code == 204
    assert second.status_code == 404
    assert listed == []


def test_the_page_offers_the_control_and_then_shows_what_it_changed() -> None:
    with browser() as client:
        before = client.get("/student/due-this-week").text
        pressed = client.post("/student/actions/too-much")
        after = client.get("/student/due-this-week").text

    assert "Too much right now" in before
    assert "You said it was too much" not in before
    assert pressed.status_code == 303
    assert pressed.headers["location"] == "/student/due-this-week"
    assert "You said it was too much" in after
    assert "held to 75 minutes instead of 150" in after
    assert "Take it back" in after
    assert "What Blossom keeps about this" in after
    assert 'action="/student/actions/too-much"' not in after


def test_taking_it_back_from_the_page_restores_the_evening() -> None:
    with browser() as client:
        client.post("/student/actions/too-much")
        signal_id = client.get("/student/workload-signals").json()[0]["signal_id"]
        taken_back = client.post(f"/student/actions/take-back/{signal_id}")
        page = client.get("/student/due-this-week").text
        listed = client.get("/student/workload-signals").json()

    assert taken_back.status_code == 303
    assert "Too much right now" in page
    assert "You said it was too much" not in page
    assert listed == []


def test_a_press_on_her_page_reaches_the_parents_plan() -> None:
    with browser() as client:
        client.post("/student/actions/too-much")
        started = client.post("/parent/plans", json={}).json()
        page = client.get("/parent").text

    assert started["steps"][0]["found"].endswith(
        "she said today is too much, so the budget is 75 minutes"
    )
    assert "You said today was too much, so this plan is kept to 75 minutes." in page


def test_the_application_keeps_her_signals_in_the_drafts_file_swept_by_the_real_clock() -> None:
    with browser() as client:
        client.post("/student/workload-signals")
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        held = state.workload_signals.held()

    assert len(held) == 1
    assert held[0].evening == date(2026, 8, 19)
    assert held[0].given_at.date() > date(2026, 8, 19)


# ----------------------------------------------- a draft made for another evening


def test_a_press_after_a_draft_is_waiting_makes_it_stale() -> None:
    with browser() as client:
        started = client.post("/parent/plans", json={}).json()
        client.post("/student/actions/too-much")
        queue = client.get("/parent/approvals").json()["waiting"]
        page = client.get("/parent").text
        approve = client.post(f"/parent/approvals/{started['draft_id']}", json={"approved": True})

    assert queue[0]["stale"] == (
        "She has said today is too much, and this plan was made for the full evening. "
        "Plan again before approving."
    )
    assert "Plan again." in page
    assert 'value="approve"' not in page
    assert 'value="refuse"' in page
    assert approve.status_code == 409
    assert approve.json()["detail"] == queue[0]["stale"]


def test_a_stale_draft_can_still_be_refused() -> None:
    with browser() as client:
        started = client.post("/parent/plans", json={}).json()
        client.post("/student/actions/too-much")
        refused = client.post(f"/parent/approvals/{started['draft_id']}", json={"approved": False})

    assert refused.status_code == 200
    assert refused.json()["decision"] == "rejected"


def test_taking_the_signal_back_after_a_reduced_draft_makes_it_stale() -> None:
    with browser() as client:
        signal_id = client.post("/student/workload-signals").json()["signal"]["signal_id"]
        started = client.post("/parent/plans", json={}).json()
        client.delete(f"/student/workload-signals/{signal_id}")
        queue = client.get("/parent/approvals").json()["waiting"]
        approve = client.post(f"/parent/approvals/{started['draft_id']}", json={"approved": True})

    assert queue[0]["stale"] == (
        "Her signal for this evening is gone, taken back or past its week, and this plan "
        "was kept short for it. Plan again for the full evening."
    )
    assert approve.status_code == 409


def test_a_plan_made_after_the_press_fits_and_can_be_approved() -> None:
    with browser() as client:
        client.post("/student/actions/too-much")
        started = client.post("/parent/plans", json={}).json()
        queue = client.get("/parent/approvals").json()["waiting"]
        approve = client.post(f"/parent/approvals/{started['draft_id']}", json={"approved": True})

    assert queue[0]["stale"] is None
    assert started["steps"][0]["found"].endswith("so the budget is 75 minutes")
    assert approve.status_code == 200
    assert approve.json()["decision"] == "approved"


# ------------------------------------------------------------- retention on reads


def test_reads_leave_out_a_signal_past_its_week_before_any_sweep() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    then = WorkloadSignalsStore(connection, FrozenClock(OBSERVED_AT, ZONE))
    then.record(PLAN_DATE)
    later = OBSERVED_AT + timedelta(days=SIGNAL_RETENTION_DAYS + 1)
    now = WorkloadSignalsStore(connection, FrozenClock(later, ZONE))

    assert now.for_evening(PLAN_DATE) == []
    assert now.held() == []
    assert now.sweep() == 1


def test_a_decided_draft_is_not_measured_against_the_evening_again() -> None:
    with browser() as client:
        started = client.post("/parent/plans", json={}).json()
        client.post(f"/parent/approvals/{started['draft_id']}", json={"approved": True})
        client.post("/student/actions/too-much")
        decided = client.get(f"/parent/approvals/{started['draft_id']}").json()
        page = client.get("/parent").text

    assert decided["decision"] == "approved"
    assert decided["stale"] is None
    assert "Plan again." not in page


# ---------------------------------------------------- her words, and the lock


def test_her_words_are_shown_back_to_her_exactly_as_kept() -> None:
    with browser() as client:
        pressed = client.post("/student/workload-signals", json={"detail": "everything at once"})
        listed = client.get("/student/workload-signals").json()
        page = client.get("/student/due-this-week").text

    assert pressed.json()["signal"]["detail"] == "everything at once"
    assert [item["detail"] for item in listed] == ["everything at once"]
    assert page.count("You added <q>everything at once</q>.") == 2


def test_a_press_waits_while_a_decision_is_being_recorded() -> None:
    """A decision is checked against the evening as signaled, which holds still until it lands."""
    with browser() as client, ThreadPoolExecutor(max_workers=1) as pool:
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        portal = client.portal
        assert portal is not None
        portal.call(state.decision_lock.acquire)
        pressing = pool.submit(client.post, "/student/actions/too-much")
        _, still_waiting = wait([pressing], timeout=0.3)
        held_while_locked = state.workload_signals.held()
        portal.call(state.decision_lock.release)
        pressed = pressing.result(timeout=10)
        held_after = state.workload_signals.held()

    assert pressing in still_waiting
    assert held_while_locked == []
    assert pressed.status_code == 303
    assert len(held_after) == 1


# ------------------------------------------------ her words are capped, and named


def test_her_words_are_capped_at_the_boundary_and_in_the_store() -> None:
    with browser() as client:
        at_the_cap = client.post(
            "/student/workload-signals", json={"detail": "w" * DETAIL_MAX_LENGTH}
        )
        over = client.post(
            "/student/workload-signals", json={"detail": "w" * (DETAIL_MAX_LENGTH + 1)}
        )
        listed = client.get("/student/workload-signals").json()

    assert at_the_cap.status_code == 201
    assert over.status_code == 422
    assert len(listed) == 1
    with pytest.raises(ValidationError):
        store_in_memory().record(PLAN_DATE, "w" * (DETAIL_MAX_LENGTH + 1))


def test_each_remove_button_says_which_signal_it_removes() -> None:
    """Two kept signals, two controls, each named by its own date and time."""
    given = OBSERVED_AT.astimezone(ZONE)

    def kept(days_ago: int) -> WorkloadSignalView:
        moment = given - timedelta(days=days_ago)
        return WorkloadSignalView(
            signal_id=f"signal-{days_ago}",
            evening=moment.date(),
            given_at=moment,
            given_local=moment,
        )

    monday = date(2026, 8, 17)
    view = StudentDueThisWeekView(
        generated_at=OBSERVED_AT,
        today=date(2026, 8, 19),
        week=WeekView(
            start=monday,
            end=monday + timedelta(days=6),
            current=True,
            previous=monday - timedelta(days=7),
            following=monday + timedelta(days=7),
        ),
        assignments=[],
        plan_horizon_end=date(2026, 8, 25),
        full_budget_minutes=DEFAULT_EVENING_MINUTES,
        budget_minutes=DEFAULT_EVENING_MINUTES,
        signals=[kept(0), kept(1)],
    )

    page = templates.get_template("student_due_this_week.html").render(view=view)

    labels = re.findall(r'aria-label="(Remove the signal from [^"]+)"', page)
    assert len(labels) == 2
    assert len(set(labels)) == 2
    for label, signal in zip(labels, view.signals, strict=True):
        assert signal.evening.strftime("%B") in label
        assert str(signal.evening.day) in label
        assert spoken_time(signal.given_local) in label
