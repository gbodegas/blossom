# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The workload signal: one press, a visible result, a reduced plan, brief and hers to remove.

The design's third tier of verification is whether a plan is right for her,
which no check can answer; her signal overrides the plan directly. These tests
hold the store, the graph, the routes, and the page to that.
"""

import asyncio
import pathlib
import re
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, time, timedelta
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from blossom.agent.graph import Ask, CompiledPlanGraph, PlanState
from blossom.agent.runs import DURABILITY, RUN_DEADLINE_SECONDS, run_config
from blossom.app import create_app
from blossom.clock import FrozenClock, spoken_time
from blossom.dependencies import STATE_ATTRIBUTE, ApplicationState
from blossom.heuristic_relevance import CriticVerdict
from blossom.plan_checks import PlanCheck
from blossom.plans import DailyPlan, Deferral, PlanBlock
from blossom.routes import student as student_routes
from blossom.routes.runs import plan_graphs
from blossom.routes.student import SIGNAL_REMOVED, landing_cookie, templates
from blossom.settings import (
    ANTHROPIC_API_KEY_VARIABLE,
    DEFAULT_EVENING_MINUTES,
    DEFAULT_TOO_MUCH_MINUTES,
)
from blossom.stores.drafts import DraftsStore
from blossom.stores.workload_signals import (
    DETAIL_MAX_LENGTH,
    SIGNAL_RETENTION_DAYS,
    WorkloadSignalsStore,
)
from blossom.views import StudentDueThisWeekView, WeekView, WorkloadSignalView
from tests.support import (
    FIXTURE_TIMEZONE,
    HERS,
    OBSERVED_AT,
    PLAN_DATE,
    SAME_ORIGIN,
    THEIRS,
    Scripted,
    accepting,
    changed_by_hand,
    drafts_in_memory,
    fixture_clock,
    fixture_settings,
    good_plan,
    graph_with,
    human_text,
    landing_in,
    light_fixture_plan,
    ok,
    refusing,
    scripted_graphs,
    signals_in_memory,
    signed_in,
    signed_in_household,
)

ZONE = ZoneInfo(FIXTURE_TIMEZONE)


def store_in_memory(clock: FrozenClock | None = None) -> WorkloadSignalsStore:
    return WorkloadSignalsStore(
        sqlite3.connect(":memory:", check_same_thread=False), clock or fixture_clock()
    )


def graph_and_store(
    planner: Ask[DailyPlan],
    critic: Ask[CriticVerdict],
    *,
    signals: WorkloadSignalsStore | None = None,
    evening_minutes: int = DEFAULT_EVENING_MINUTES,
    too_much_minutes: int = DEFAULT_TOO_MUCH_MINUTES,
) -> tuple[CompiledPlanGraph, DraftsStore]:
    """The graph over in-memory stores, and the drafts store its run is admitted to."""
    drafts = drafts_in_memory()
    graph = graph_with(
        planner,
        critic,
        drafts=drafts,
        signals=signals,
        evening_minutes=evening_minutes,
        too_much_minutes=too_much_minutes,
    )
    return graph, drafts


def run(
    graph: CompiledPlanGraph, drafts: DraftsStore, thread: str = "plan:signaled"
) -> dict[str, Any]:
    """Admit one run on ``thread`` and drive it to its pause or its end."""
    blocking = drafts.admit_run(
        thread, plan_date=PLAN_DATE, deadline_mono=monotonic() + RUN_DEADLINE_SECONDS
    )
    assert blocking is None

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
        *graph_and_store(
            Scripted(ok(good_plan())),
            critic,
            signals=signals,
            evening_minutes=200,
            too_much_minutes=100,
        )
    )
    usual = run(
        *graph_and_store(
            Scripted(ok(good_plan())), critic, evening_minutes=200, too_much_minutes=100
        ),
        thread="plan:usual",
    )

    assert signaled["budget_minutes"] == 100
    assert signaled["steps"][0].found.endswith("so the evening allows 100 minutes.")
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

    result = run(*graph_and_store(planner, Scripted(ok(accepting())), signals=signals))

    brief = human_text(planner.briefs[0])
    assert result["too_much"] is True
    assert result["budget_minutes"] == 75
    assert "<budget_minutes>75</budget_minutes>" in brief
    assert "<too_much>" in brief
    assert "She said today is too much." in brief
    assert result["steps"][0].found.endswith(
        "She said today is too much, so the evening allows 75 minutes."
    )


def test_without_a_signal_the_evening_is_the_usual_length() -> None:
    planner = Scripted(ok(good_plan()))

    result = run(*graph_and_store(planner, Scripted(ok(accepting()))))

    assert result["too_much"] is False
    assert result["budget_minutes"] == DEFAULT_EVENING_MINUTES
    assert "<too_much>" not in human_text(planner.briefs[0])


def test_a_plan_that_fits_the_usual_evening_fails_the_reduced_one() -> None:
    signals = signals_in_memory()
    signals.record(PLAN_DATE)
    planner = Scripted(*[ok(long_plan())] * 3)

    result = run(*graph_and_store(planner, Scripted(), signals=signals))

    assert result["outcome"] == "checks_failed"
    assert result["verification"].failed_checks == (PlanCheck.WITHIN_TIME_BUDGET,)
    assert result["feedback"] == ["the plan asks for 90 minutes and the evening allows 75"]


def test_the_same_plan_passes_when_she_has_not_signaled() -> None:
    result = run(*graph_and_store(Scripted(ok(long_plan())), Scripted(ok(accepting()))))

    assert result["outcome"] == "accepted"


def test_the_critic_and_the_draft_are_told() -> None:
    signals = signals_in_memory()
    signals.record(PLAN_DATE)
    critic = Scripted(ok(accepting()))

    result = run(*graph_and_store(Scripted(ok(good_plan())), critic, signals=signals))

    assert "<too_much>" in human_text(critic.briefs[0])
    body = result["__interrupt__"][0].value["body"]
    assert "You said today was too much, so this plan is kept to 75 minutes." in body


def test_a_signal_about_another_evening_changes_nothing_tonight() -> None:
    signals = signals_in_memory()
    signals.record(PLAN_DATE - timedelta(days=1))

    result = run(
        *graph_and_store(Scripted(ok(good_plan())), Scripted(ok(accepting())), signals=signals)
    )

    assert result["too_much"] is False
    assert result["budget_minutes"] == DEFAULT_EVENING_MINUTES


# ------------------------------------------------------- the routes and the page


def browser(*, key: bool = False, planners: list[Scripted[DailyPlan]] | None = None) -> TestClient:
    """Her page with scripted models. ``key`` lets the page offer its plan button, and
    ``planners`` collects every planner a run builds, so a test can count what was asked."""
    environ = {ANTHROPIC_API_KEY_VARIABLE: "not-a-key-and-never-sent"} if key else {}
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **environ))
    app.dependency_overrides[plan_graphs] = scripted_graphs(
        lambda: [light_fixture_plan()], lambda: [accepting()], planners=planners
    )
    return TestClient(app, follow_redirects=False, headers=SAME_ORIGIN)


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
    assert REQUESTED not in before
    assert pressed.status_code == 303
    assert pressed.headers["location"] == "/student/due-this-week"
    assert REQUESTED in after
    assert NEXT_PLAN in after
    assert f">{UNDO}</button>" in after
    assert "What Blossom keeps about this" in after
    assert 'action="/student/actions/too-much"' not in after


def test_taking_it_back_from_the_page_restores_the_evening() -> None:
    with browser() as client:
        client.post("/student/actions/too-much")
        signal_id = client.get("/student/workload-signals").json()[0]["signal_id"]
        taken_back = client.post(f"/student/actions/take-back/{signal_id}")
        page = client.get(taken_back.headers["location"]).text
        listed = client.get("/student/workload-signals").json()

    assert taken_back.status_code == 303
    assert taken_back.headers["location"] == landed_at(taken_back.headers["location"])
    assert "Too much right now" in page
    assert REQUESTED not in page
    assert REMOVED in page
    assert listed == []


# ------------------------------------------- what her page says, and Undo 'Too much right now'

UNDO = "Undo 'Too much right now'"
REQUESTED = "<strong>A shorter plan is requested for today.</strong>"
NEXT_PLAN = "Your next plan will use up to 75 minutes."
SAVED_PLAN = "Your saved plan already uses the 75-minute limit."
REMOVED = "You removed your request."
UNCHANGED = "Your saved plan has not changed."
STILL = "A shorter plan is still requested for today."
PAGE = "/student/due-this-week"
STATE = re.compile(
    r'<div class="too-much">\s*<p class="note" id="too-much-state"( role="status")?>(.*?)</p>'
    r'\s*<div class="actions">(.*?)</div>\s*</div>',
    re.S,
)
"""The state line and the Undo beside it: whether it is said as news, its words, its control."""


def today_of(page: str) -> str:
    """Today's panel, from its heading to the week's homework."""
    start = page.index('<section class="panel today" id="today"')
    return page[start : page.index('<h2 class="list-heading"')]


def said(html: str) -> str:
    """Words as a reader meets them, the spaces between them folded."""
    return " ".join(html.split())


def tonight(client: TestClient) -> list[str]:
    """Her signals kept for today, the oldest first."""
    state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
    return [item.signal_id for item in state.workload_signals.for_evening(PLAN_DATE)]


def todays_draft(client: TestClient) -> object:
    """Today's saved plan whole, as the drafts store keeps it."""
    state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
    return state.drafts.latest_for(PLAN_DATE)


def landed_at(location: str) -> str:
    """Her week at the landing an Undo's redirect names, which is new for every press."""
    return f"{PAGE}?landing={landing_in(location)}"


def undo(client: TestClient, signal_id: str) -> str:
    """Press Undo for one signal, as her page sends it, and read the page it lands on, which
    the mark the press left in that landing's cookie is for."""
    pressed = client.post(f"/student/actions/take-back/{signal_id}")
    assert pressed.status_code == 303
    location = pressed.headers["location"]
    assert location == landed_at(location)
    assert pressed.headers["set-cookie"].startswith(
        f"{landing_cookie(landing_in(location))}={SIGNAL_REMOVED};"
    )
    return client.get(location).text


def asked(planners: list[Scripted[DailyPlan]]) -> tuple[int, int]:
    """How many planners runs built, and how many times they were asked."""
    return len(planners), sum(planner.calls for planner in planners)


def test_with_no_signal_today_offers_the_press_and_says_nothing_about_a_shorter_plan() -> None:
    with browser(key=True) as client:
        today = today_of(client.get(PAGE).text)

    assert ">Too much right now</button>" in today
    assert UNDO not in today
    assert STATE.search(today) is None
    for words in ("shorter plan", REMOVED, "use up to", "-minute limit"):
        assert words not in today, words


@pytest.mark.parametrize("plan", ["no plan", "a full plan", "a smaller plan"])
def test_a_signal_says_what_it_asks_for_beside_its_undo(plan: str) -> None:
    """From the signals kept for today and the household's minutes: a shorter plan is asked
    for, and her next plan will be held to it, or the plan saved under it already is. Undo
    sits under those words and is described by them, and nothing offers to press Too much
    again. Pressing plans nothing."""
    planners: list[Scripted[DailyPlan]] = []
    with browser(key=True, planners=planners) as client:
        if plan == "a full plan":
            client.post("/student/actions/plan")
        before = asked(planners)
        client.post("/student/actions/too-much")
        after = asked(planners)
        if plan == "a smaller plan":
            client.post("/student/actions/plan")
        page = client.get(PAGE).text
        held = tonight(client)

    today = today_of(page)
    state = STATE.search(today)
    assert state is not None
    news, words, control = state.groups()
    assert news is None
    assert said(words) == f"{REQUESTED} {SAVED_PLAN if plan == 'a smaller plan' else NEXT_PLAN}"
    assert f'action="/student/actions/take-back/{held[-1]}"' in control
    assert f'aria-describedby="too-much-state">{UNDO}</button>' in control
    assert today.count(UNDO) == 1
    assert "Too much right now</button>" not in page
    assert ("Make a smaller plan</button>" in today) is (plan != "a smaller plan")
    assert "smaller budget" not in today
    assert after == before


@pytest.mark.parametrize("plan", [False, True], ids=["no plan", "a smaller plan"])
def test_undo_removes_her_request_says_so_and_leaves_the_plan_as_saved(plan: bool) -> None:
    """One signal, one Undo: the row is gone, the page says she removed her request, and that
    her saved plan has not changed when one is saved; Too much right now is offered again.
    No model is asked, and the saved plan is as it was."""
    planners: list[Scripted[DailyPlan]] = []
    with browser(key=True, planners=planners) as client:
        client.post("/student/actions/too-much")
        if plan:
            client.post("/student/actions/plan")
        saved = todays_draft(client)
        before = asked(planners)
        page = undo(client, tonight(client)[0])
        after = asked(planners)
        left = tonight(client)
        kept = todays_draft(client)

    today = today_of(page)
    line = re.search(r'<p class="note" id="too-much-state" role="status">(.*?)</p>', today, re.S)
    assert line is not None
    assert said(line.group(1)) == (f"{REMOVED} {UNCHANGED}" if plan else REMOVED)
    assert left == []
    assert after == before
    assert kept == saved
    assert (saved is not None) is plan
    assert STATE.search(today) is None
    assert ">Too much right now</button>" in today
    assert UNDO not in page
    for never in ("taken back", "withdr", "150 minutes", "usual"):
        assert never not in today, never
    assert "taken back" not in page
    assert "withdr" not in page


def test_with_two_requests_each_undo_says_what_still_stands() -> None:
    """Each press keeps a row, and the evening stays shorter while any is left. Undo removes
    the latest: the page says she removed her request and that a shorter plan is still
    requested, with what it does, and its Undo is for the one left. That removes the last,
    and only then is nothing said to be requested."""
    with browser(key=True) as client:
        client.post("/student/actions/too-much")
        client.post("/student/actions/too-much")
        first, latest = tonight(client)
        two = today_of(client.get(PAGE).text)
        one_left = today_of(undo(client, latest))
        left = tonight(client)
        none_left = today_of(undo(client, first))
        gone = tonight(client)

    state = STATE.search(two)
    assert state is not None
    assert f"take-back/{latest}" in state.group(3)
    assert two.count(UNDO) == 1
    assert left == [first]
    state = STATE.search(one_left)
    assert state is not None
    news, words, control = state.groups()
    assert news == ' role="status"'
    assert said(words) == f"{REMOVED} {STILL} {NEXT_PLAN}"
    assert f'action="/student/actions/take-back/{first}"' in control
    assert one_left.count(UNDO) == 1
    assert REQUESTED not in one_left
    assert gone == []
    assert STATE.search(none_left) is None
    assert REMOVED in none_left
    assert STILL not in none_left
    assert "shorter plan" not in none_left
    assert ">Too much right now</button>" in none_left


def test_an_undo_sent_again_changes_nothing_and_never_says_the_evening_is_back() -> None:
    """Undo pressed twice, as a double press or a page left open sends it: the second finds
    its request gone, changes nothing, and lands on the same words. With an earlier request
    still kept, the page says so, and nothing says the usual evening is back."""
    with browser(key=True) as client:
        client.post("/student/actions/too-much")
        client.post("/student/actions/too-much")
        first, latest = tonight(client)
        once = today_of(undo(client, latest))
        twice = today_of(undo(client, latest))
        left = tonight(client)

    assert left == [first]
    assert twice == once
    state = STATE.search(twice)
    assert state is not None
    assert said(state.group(2)) == f"{REMOVED} {STILL} {NEXT_PLAN}"
    assert f"take-back/{first}" in state.group(3)
    for never in ("150 minutes", "usual", "full evening"):
        assert never not in twice, never


def test_only_the_page_her_undo_lands_on_says_she_removed_her_request() -> None:
    """The Undo's redirect names a new landing and leaves a mark for that page alone, which
    reads it once, clears it and is kept by no cache. A refresh or a return says what stands."""
    with browser(key=True) as client:
        client.post("/student/actions/too-much")
        client.post("/student/actions/too-much")
        first, latest = tonight(client)
        pressed = client.post(f"/student/actions/take-back/{latest}")
        landed = client.get(pressed.headers["location"])
        refreshed = client.get(pressed.headers["location"])
        returned = client.get(PAGE)

    news = STATE.search(today_of(landed.text))
    assert news is not None
    assert news.group(1) == ' role="status"'
    assert said(news.group(2)) == f"{REMOVED} {STILL} {NEXT_PLAN}"
    for later in (refreshed.text, returned.text):
        assert REMOVED not in later
        state = STATE.search(today_of(later))
        assert state is not None
        assert state.group(1) is None
        assert said(state.group(2)) == f"{REQUESTED} {NEXT_PLAN}"
        assert f"take-back/{first}" in state.group(3)
    landing = landing_in(pressed.headers["location"])
    left = pressed.headers["set-cookie"]
    assert left.startswith(f"{landing_cookie(landing)}={SIGNAL_REMOVED};")
    for part in ("HttpOnly", "Max-Age=60", f"Path={PAGE}", "SameSite=lax"):
        assert part in left, part
    assert landed.headers["cache-control"] == "no-store"
    assert landed.headers["set-cookie"].startswith(f'{landing_cookie(landing)}=""; ')
    assert "cache-control" not in refreshed.headers


@pytest.mark.parametrize("kept", [0, 1], ids=["nothing kept", "a request kept"])
@pytest.mark.parametrize(
    "query",
    ["signal=removed", f"landing={'0' * 16}", f"signal=removed&landing={'0' * 16}"],
    ids=["a removal named", "a landing with no mark", "both"],
)
def test_an_address_alone_never_says_she_removed_her_request(kept: int, query: str) -> None:
    """Opened from a bookmark, a link or by hand, an address says nothing of a removal: Today
    says what stands, as on any visit."""
    with browser(key=True) as client:
        for _ in range(kept):
            client.post("/student/actions/too-much")
        page = client.get(f"{PAGE}?{query}")

    today = today_of(page.text)
    assert REMOVED not in today
    state = STATE.search(today)
    if kept:
        assert state is not None
        assert state.group(1) is None
        assert said(state.group(2)) == f"{REQUESTED} {NEXT_PLAN}"
    else:
        assert state is None
        assert ">Too much right now</button>" in today
    assert "cache-control" not in page.headers


def test_try_again_after_an_unreadable_landing_says_the_removal_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Her week unreadable where an Undo lands says nothing of the removal and leaves its mark,
    so Try again, the same address while the mark waits, says it once."""
    real = student_routes.student_page
    with browser(key=True) as client:
        client.post("/student/actions/too-much")
        pressed = client.post(f"/student/actions/take-back/{tonight(client)[0]}")
        monkeypatch.setattr(student_routes, "student_page", refusing())
        failed = client.get(pressed.headers["location"])
        monkeypatch.setattr(student_routes, "student_page", real)
        again = re.search(r'<a href="([^"]+)">Try again</a>', failed.text)
        assert again is not None
        tried = client.get(again.group(1))
        refreshed = client.get(again.group(1))

    assert failed.status_code == 503
    assert REMOVED not in failed.text
    assert "set-cookie" not in failed.headers
    assert again.group(1) == pressed.headers["location"]
    assert REMOVED in today_of(tried.text)
    assert tried.headers["cache-control"] == "no-store"
    assert REMOVED not in refreshed.text


def test_a_press_on_her_page_reaches_the_parents_plan() -> None:
    with browser() as client:
        client.post("/student/actions/too-much")
        started = client.post("/parent/plans", json={}).json()
        page = client.get("/parent").text

    assert started["steps"][0]["found"].endswith(
        "She said today is too much, so the evening allows 75 minutes."
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
    assert started["steps"][0]["found"].endswith("so the evening allows 75 minutes.")
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


# --------------------------------------------- a saved plan against the limit set now


@contextmanager
def started(
    folder: pathlib.Path, minutes: int, planners: list[Scripted[DailyPlan]] | None = None
) -> Iterator[TestClient]:
    """Her household on the files under ``folder``, its Too much limit set to ``minutes``, her
    signed in. A later start on the same files reads the limit as set at that start."""
    settings = replace(
        signed_in_household(folder),
        anthropic_api_key="not-a-key-and-never-sent",
        too_much_minutes=minutes,
    )
    app = create_app(settings)
    app.dependency_overrides[plan_graphs] = scripted_graphs(
        lambda: [light_fixture_plan()], lambda: [accepting()], planners=planners
    )
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        signed_in(client, HERS)
        yield client


def as_a_parent(client: TestClient) -> str:
    """Today's panel of her week as a parent signed in on this client reads it."""
    client.post("/sign-out")
    signed_in(client, THEIRS)
    return today_of(client.get(PAGE).text)


def a_shorter_plan_saved(folder: pathlib.Path, *, presses: int = 1) -> object:
    """Too much pressed ``presses`` times, then a plan made under the usual 75 minutes: one
    block, 60 minutes long. The plan as saved."""
    with started(folder, DEFAULT_TOO_MUCH_MINUTES) as client:
        for _ in range(presses):
            client.post("/student/actions/too-much")
        client.post("/student/actions/plan")
        return todays_draft(client)


@pytest.mark.parametrize(
    ("now", "fits"),
    [
        pytest.param(40, False, id="lowered to 40"),
        pytest.param(75, True, id="kept at 75"),
        pytest.param(100, True, id="raised to 100"),
    ],
)
def test_a_saved_plan_is_said_to_use_the_limit_only_when_its_blocks_fit_the_limit_set_now(
    now: int, fits: bool, tmp_path: pathlib.Path
) -> None:
    """Read after a start with the limit set to ``now``: her line, her plan button and a
    parent's sentence say the saved plan uses the limit only when its blocks fit it. The
    plan keeps its own words, and nothing is planned."""
    saved = a_shorter_plan_saved(tmp_path)
    planners: list[Scripted[DailyPlan]] = []
    with started(tmp_path, now, planners) as client:
        hers = today_of(client.get(PAGE).text)
        theirs = as_a_parent(client)
        kept = todays_draft(client)

    state = STATE.search(hers)
    assert state is not None
    if fits:
        assert said(state.group(2)) == (
            f"{REQUESTED} Your saved plan already uses the {now}-minute limit."
        )
        assert "Plan again</button>" in hers
        assert f"This plan already uses the smaller budget, {now} minutes." in said(theirs)
    else:
        assert said(state.group(2)) == f"{REQUESTED} Your next plan will use up to {now} minutes."
        assert "Make a smaller plan</button>" in hers
        assert "already uses" not in theirs
        assert f"held to {now} minutes instead of {DEFAULT_EVENING_MINUTES}." in said(theirs)
    assert ("Make a smaller plan" in hers) is not fits
    assert "kept to 75 minutes" in hers
    assert kept == saved
    assert asked(planners) == (0, 0)


@pytest.mark.parametrize(
    ("now", "fits"), [pytest.param(40, False, id="lowered"), pytest.param(75, True, id="kept")]
)
def test_an_undo_that_leaves_a_request_measures_the_saved_plan_against_the_limit_set_now(
    now: int, fits: bool, tmp_path: pathlib.Path
) -> None:
    a_shorter_plan_saved(tmp_path, presses=2)
    with started(tmp_path, now) as client:
        landing = today_of(undo(client, tonight(client)[-1]))

    state = STATE.search(landing)
    assert state is not None
    plan = (
        f"Your saved plan already uses the {now}-minute limit."
        if fits
        else f"Your next plan will use up to {now} minutes."
    )
    assert said(state.group(2)) == f"{REMOVED} {STILL} {plan}"
    assert ("Make a smaller plan</button>" in landing) is not fits


def test_a_saved_plan_whose_blocks_cant_be_read_is_said_neither_to_fit_nor_to_be_over(
    tmp_path: pathlib.Path,
) -> None:
    """Saved as text alone, its blocks can't be measured: her line says what her next plan
    will use, the button offers a plan again, and a parent reads no claim about the limit."""
    a_shorter_plan_saved(tmp_path)
    with started(tmp_path, DEFAULT_TOO_MUCH_MINUTES) as client:
        state_now: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        changed_by_hand(state_now.drafts, "UPDATE drafts SET plan_snapshot = NULL")
        hers = today_of(client.get(PAGE).text)
        theirs = as_a_parent(client)

    state = STATE.search(hers)
    assert state is not None
    assert said(state.group(2)) == f"{REQUESTED} {NEXT_PLAN}"
    assert "Plan again</button>" in hers
    assert "Make a smaller plan" not in hers
    assert "already uses" not in theirs


@pytest.mark.parametrize(
    ("now", "label"),
    [
        pytest.param(40, "Make a smaller plan", id="lowered"),
        pytest.param(75, "Plan again", id="kept"),
    ],
)
def test_a_saved_plan_read_as_changed_offers_what_the_limit_set_now_calls_for(
    now: int, label: str, tmp_path: pathlib.Path
) -> None:
    """The work it was made from reads differently now, so the plan's notice leads with what
    the plan button offers, which follows the limit set now."""
    a_shorter_plan_saved(tmp_path)
    with started(tmp_path, DEFAULT_TOO_MUCH_MINUTES) as client:
        state_now: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        changed_by_hand(state_now.drafts, "UPDATE drafts SET inputs_digest = ?", ("0" * 32,))
    with started(tmp_path, now) as client:
        hers = today_of(client.get(PAGE).text)
        theirs = as_a_parent(client)

    assert f"<strong>{label}.</strong>" in hers
    assert "already uses" not in theirs
