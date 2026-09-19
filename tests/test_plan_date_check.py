"""A plan for another evening than the one asked for is sent back, never saved or repaired.

The evening a run plans is fixed when the run starts. A planner that answers
with another date, a day early or a day late, gets the same correction any
failed check gets, within the same bounds, and the store's own refusal of a
bundle that disagrees with itself stays the last word.
"""

import asyncio
import json
import pathlib
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

import pytest
from fastapi import Depends
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from blossom import plan_checks
from blossom.agent import steps as agent_steps
from blossom.agent.graph import (
    MAX_REVISIONS,
    CompiledPlanGraph,
    ModelAnswer,
    PlanState,
    plan_graph_for,
)
from blossom.agent.runs import DURABILITY, GRAPH_VERSION, run_config
from blossom.dependencies import ApplicationState, get_application_state
from blossom.heuristic_relevance import CriticVerdict
from blossom.noticing import planning_digest, read_week
from blossom.plan_checks import ONLY_WHAT_IS_LISTED, PlanCheck, check_plan
from blossom.plans import DailyPlan
from blossom.routes.parent import ASSIGNMENTS_CHANGED as THEIR_ASSIGNMENTS_CHANGED
from blossom.routes.runs import PlanGraphs, plan_graphs
from blossom.routes.student import ASSIGNMENTS_CHANGED
from blossom.stores.checkpoints import open_checkpointer
from blossom.stores.drafts import DraftsStore, IncoherentBundle
from blossom.stores.project_state import Saved
from tests.support import (
    ESSAY,
    ESSAY_ID,
    HER_PAGE,
    PAGE_HEADERS,
    PLAN_DATE,
    PROBLEM_SET,
    ZONE,
    ReportsWhileAsked,
    Scripted,
    SetClock,
    accepting,
    browser,
    composed_plan,
    drafts_in_memory,
    fixture_clock,
    fixture_week_plan,
    good_plan,
    graph_with,
    human_text,
    ok,
    scripted_graphs,
    state_of,
    work_listed,
)

DAY = timedelta(days=1)
SEVEN: tuple[PlanCheck, ...] = tuple(
    check for check in PlanCheck if check.value != "PLAN_DATE_MATCHES_REQUEST"
)


def dated(plan: DailyPlan, evening: date) -> DailyPlan:
    return plan.model_copy(update={"plan_date": evening})


def wrong_evening(returned: date, asked: date = PLAN_DATE) -> str:
    return f"the plan is for {returned}, not the evening asked for, {asked}: make it for {asked}"


def run_for(
    graph: CompiledPlanGraph, evening: date = PLAN_DATE, thread: str = "plan:dated"
) -> dict[str, Any]:
    """Drive one run for ``evening`` to its pause or its end."""

    async def go() -> dict[str, Any]:
        result = await graph.ainvoke(
            PlanState(plan_date=evening, rounds=0),
            config=run_config(thread),
            durability=DURABILITY,
        )
        return dict(result)

    return asyncio.run(go())


def verify_lines(result: dict[str, Any]) -> list[str]:
    return [item.found for item in result["steps"] if item.node == "verify"]


# ------------------------------------------------------------- the check by itself


@pytest.mark.parametrize("off_by", [-1, 1, -7, 366])
def test_a_plan_for_another_evening_fails_the_date_check_and_nothing_else(off_by: int) -> None:
    """The essay and the problem set, planned well. For the evening asked for, every check
    passes. For a day early, a day late, or a week early, the date check fails, alone, and
    says both dates; a year on it fails beside the deadlines that date runs past. The
    finding is the planner's to read, since it names no assignment."""
    window = [ESSAY, PROBLEM_SET]
    right = check_plan(good_plan(), due_in_window=window, zone=ZONE, requested_evening=PLAN_DATE)
    returned = PLAN_DATE + off_by * DAY
    wrong = check_plan(
        dated(good_plan(), returned),
        due_in_window=window,
        zone=ZONE,
        requested_evening=PLAN_DATE,
        reported_done=["assignment-somewhere-else"],
    )

    assert right.passed
    assert set(right.outcomes) == set(PlanCheck)
    assert not wrong.passed
    if off_by < 366:
        assert wrong.failed_checks == (PlanCheck.PLAN_DATE_MATCHES_REQUEST,)
    assert wrong.failed_checks[0] is PlanCheck.PLAN_DATE_MATCHES_REQUEST
    assert wrong.findings[PlanCheck.PLAN_DATE_MATCHES_REQUEST] == (wrong_evening(returned),)
    assert wrong_evening(returned) in wrong.as_feedback()
    assert ONLY_WHAT_IS_LISTED not in wrong.as_feedback()


def test_the_evening_asked_for_is_handed_in_and_never_read_from_the_plan() -> None:
    """The check has no default to fall back on: a caller that names no evening is refused,
    so the plan's own date can never stand in for the one that was asked for."""
    with pytest.raises(TypeError):
        check_plan(good_plan(), due_in_window=[ESSAY, PROBLEM_SET], zone=ZONE)  # type: ignore[call-arg]


# ------------------------------------------------------------- through the revision loop


def test_a_wrong_evening_is_sent_back_once_and_the_corrected_plan_is_published() -> None:
    """The first answer is a good plan dated a day early; the second is the same plan for
    the evening asked for. The planner is asked twice and told both dates, the reviewer
    once, about the second plan only, and the draft, its text, and its snapshot are all for
    the evening asked for."""
    drafts = drafts_in_memory()
    planner = Scripted(ok(dated(good_plan(), PLAN_DATE - DAY)), ok(good_plan()))
    critic = Scripted(ok(accepting()))

    result = run_for(graph_with(planner, critic, drafts=drafts))

    record = drafts.get(result["draft"].draft_id)
    assert (planner.calls, critic.calls) == (2, 1)
    assert result["outcome"] == "accepted"
    assert verify_lines(result) == [
        f"1 of 8 checks failed: {wrong_evening(PLAN_DATE - DAY)}",
        "all 8 checks passed",
    ]
    second = human_text(planner.briefs[1])
    assert '<feedback round="2">' in second
    assert wrong_evening(PLAN_DATE - DAY) in second
    assert work_listed(planner.briefs[1]) == work_listed(planner.briefs[0])
    assert "2026-08-18" not in human_text(critic.briefs[0])
    assert len(result["__interrupt__"]) == 1
    assert record is not None
    assert record.plan_date == PLAN_DATE
    assert record.body.startswith("Plan for Wednesday, August 19")
    assert record.plan_snapshot is not None
    assert json.loads(record.plan_snapshot)["plan"]["plan_date"] == PLAN_DATE.isoformat()
    assert result["plan"].plan_date == PLAN_DATE


def test_a_planner_that_never_names_the_evening_asked_for_ends_as_checks_failed() -> None:
    """Every answer allowed is for the wrong evening. The planner is asked as often as any
    failing plan allows and no more, the reviewer never, and the run ends where a plan that
    never passes its checks ends: recorded, with no draft and no gate."""
    drafts = drafts_in_memory()
    planner = Scripted(*[ok(dated(good_plan(), PLAN_DATE + DAY))] * (MAX_REVISIONS + 1))
    critic: Scripted[CriticVerdict] = Scripted()

    result = run_for(graph_with(planner, critic, drafts=drafts))

    assert result["outcome"] == "checks_failed"
    assert (planner.calls, critic.calls) == (MAX_REVISIONS + 1, 0)
    assert "__interrupt__" not in result
    assert "draft" not in result
    assert verify_lines(result) == [f"1 of 8 checks failed: {wrong_evening(PLAN_DATE + DAY)}"] * (
        MAX_REVISIONS + 1
    )
    assert drafts.waiting() == []
    (ended,) = drafts.runs_without_a_draft()
    assert ended.outcome == "checks_failed"
    assert ended.plan_date == PLAN_DATE


def test_the_wrong_evening_every_time_leaves_the_plan_already_there_alone() -> None:
    """Through her page: today's plan is made and waits. She plans again and every answer is
    dated tomorrow. Her page says the run ended with its checks failed, 409, never that
    something went wrong on the way; the earlier plan is still today's, still waiting, not
    displaced; the run is on record without a draft; and nothing is left in flight."""
    with browser(key=True) as client:
        assert client.post("/student/actions/plan").status_code == 303
        state = state_of(client)
        first = state.drafts.latest_for(PLAN_DATE)
        assert first is not None
        late = dated(fixture_week_plan(), PLAN_DATE + DAY)
        planners: list[Scripted[DailyPlan]] = []

        def planner() -> list[DailyPlan]:
            return [late] * (MAX_REVISIONS + 1)

        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            planner, list, planners=planners
        )
        again = client.post("/student/actions/plan", headers=PAGE_HEADERS)
        latest = state.drafts.latest_for(PLAN_DATE)
        ended = state.drafts.runs_without_a_draft()
        in_flight = set(state.in_flight)
        family = client.get("/parent", headers=PAGE_HEADERS).text

    assert again.status_code == 409
    assert "No plan was made this time: the run ended with checks_failed." in again.text
    assert "something went wrong on the way" not in again.text
    assert planners[-1].calls == MAX_REVISIONS + 1
    assert latest is not None
    assert latest.draft_id == first.draft_id
    assert latest.waiting
    assert latest.decision is None
    assert [run.outcome for run in ended] == ["checks_failed"]
    assert in_flight == set()
    assert "1 of 8 checks failed: the plan is for 2026-08-20" in family


# ------------------------------------------------------------- whose evening it is


def test_an_evening_a_parent_asks_for_is_the_one_the_plan_is_held_to() -> None:
    """A parent asks for tomorrow evening. A plan dated today, which is what the household's
    clock says, is sent back; the plan dated tomorrow is published for tomorrow."""
    tomorrow = PLAN_DATE + DAY
    with browser(key=True) as client:
        planners: list[Scripted[DailyPlan]] = []
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [fixture_week_plan(), dated(fixture_week_plan(), tomorrow)],
            lambda: [accepting()],
            planners=planners,
        )
        asked = client.post("/parent/plans", json={"plan_date": tomorrow.isoformat()})
        state = state_of(client)
        made = state.drafts.latest_for(tomorrow)
        todays = state.drafts.latest_for(PLAN_DATE)

    assert asked.status_code == 201, asked.text[:300]
    assert asked.json()["outcome"] == "accepted"
    assert planners[-1].calls == 2
    assert wrong_evening(PLAN_DATE, tomorrow) in human_text(planners[-1].briefs[1])
    assert todays is None
    assert made is not None
    assert made.plan_date == tomorrow
    assert made.body.startswith("Plan for Thursday, August 20")


class AnswersAfterMidnight:
    """A planner whose first answer comes only after the household's day has moved on, and is
    dated the new day, as a model that went by the clock would date it."""

    def __init__(self, clock: SetClock, *plans: DailyPlan) -> None:
        self.clock = clock
        self.plans = list(plans)
        self.briefs: list[list[BaseMessage]] = []

    async def __call__(self, messages: Sequence[BaseMessage]) -> ModelAnswer[DailyPlan]:
        self.briefs.append(list(messages))
        if len(self.briefs) == 1:
            self.clock.day = self.clock.day + DAY
            self.clock.at = self.clock.at + DAY
        return ok(self.plans.pop(0))


def test_a_day_that_turns_while_the_planner_is_asked_does_not_move_the_evening() -> None:
    """The run was asked for the 19th. The household's day becomes the 20th while the planner
    is held, and its answer is dated the 20th. The run still holds the plan to the 19th:
    that answer is sent back, and the plan for the 19th is the one saved."""
    clock = SetClock(PLAN_DATE, datetime(2026, 8, 20, 3, 58, tzinfo=UTC))
    drafts = drafts_in_memory()
    planner = AnswersAfterMidnight(clock, dated(good_plan(), PLAN_DATE + DAY), good_plan())
    critic = Scripted(ok(accepting()))

    result = run_for(graph_with(planner, critic, drafts=drafts, clock=clock))

    record = drafts.get(result["draft"].draft_id)
    assert clock.today() == PLAN_DATE + DAY
    assert (len(planner.briefs), critic.calls) == (2, 1)
    assert verify_lines(result)[0] == f"1 of 8 checks failed: {wrong_evening(PLAN_DATE + DAY)}"
    assert result["outcome"] == "accepted"
    assert record is not None
    assert record.plan_date == PLAN_DATE
    assert result["plan"].plan_date == PLAN_DATE


# ------------------------------------------------------------- what the run read stays put


def test_a_done_that_lands_before_the_correction_changes_nothing_the_run_works_from() -> None:
    """Her Done lands while the planner is first asked, and that answer is dated a day
    early. The correction is asked for from the same reading: the same work listed, the
    essay in it, the date finding and no word about finished work. The draft keeps the
    fingerprint of what the run read, so both pages read it as stale the moment it is
    published and approving it is refused."""
    planners: list[ReportsWhileAsked[DailyPlan]] = []
    critics: list[Scripted[CriticVerdict]] = []

    def override(
        state: Annotated[ApplicationState, Depends(get_application_state)],
    ) -> PlanGraphs:
        if not planners:
            early = dated(fixture_week_plan(), PLAN_DATE - DAY)
            planners.append(ReportsWhileAsked(state, early, fixture_week_plan()))
            critics.append(Scripted(ok(accepting())))
        return PlanGraphs(
            build=lambda: plan_graph_for(state, planner=planners[0], critic=critics[0]),
            may_start=True,
        )

    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = override  # type: ignore[attr-defined]
        state = state_of(client)
        as_read = planning_digest(read_week(state.project_state, state.project_state, PLAN_DATE))
        planned = client.post("/student/actions/plan")
        record = state.drafts.latest_for(PLAN_DATE)
        as_it_stands = planning_digest(
            read_week(state.project_state, state.project_state, PLAN_DATE)
        )
        hers = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text
        assert record is not None
        refused = client.post(
            f"/parent/actions/decide/{record.draft_id}", data={"decision": "approve"}
        )
        after = state.drafts.get(record.draft_id)

    first, second = (human_text(brief) for brief in planners[0].briefs)
    assert planned.status_code == 303
    assert isinstance(planners[0].saved, Saved)
    assert (len(planners[0].briefs), critics[0].calls) == (2, 1)
    assert work_listed(planners[0].briefs[1]) == work_listed(planners[0].briefs[0])
    assert f'id="{ESSAY_ID}"' in work_listed(planners[0].briefs[1])
    assert work_listed(critics[0].briefs[0]) == work_listed(planners[0].briefs[0])
    assert wrong_evening(PLAN_DATE - DAY) in second
    assert wrong_evening(PLAN_DATE - DAY) not in first
    assert ONLY_WHAT_IS_LISTED not in second
    assert "reported done" not in second
    assert record.plan_date == PLAN_DATE
    assert record.inputs_digest == as_read
    assert as_it_stands != as_read
    assert record.plan_assignment_ids is not None
    assert ESSAY_ID in record.plan_assignment_ids
    assert ASSIGNMENTS_CHANGED in hers
    assert THEIR_ASSIGNMENTS_CHANGED in family
    assert 'value="approve"' not in family
    assert refused.status_code == 409
    assert after is not None
    assert after.waiting


# ------------------------------------------------------------- runs from before the check


def test_a_run_paused_before_the_check_existed_resumes_and_keeps_its_own_record(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run made while there were seven checks pauses at the gate, in files. The process
    that resumes it knows eight. It resumes under the same graph version, the decision is
    recorded, the verification it carries still holds seven results, and its record still
    says what it said when it was made: nothing reads a saved result again, and no page
    turns a plan that passed seven checks into one that failed an eighth."""
    checkpoints = tmp_path / "checkpoints.sqlite3"
    drafts_path = tmp_path / "blossom.sqlite3"
    config = run_config("plan:2026-08-19:before")

    async def first_process() -> dict[str, Any]:
        drafts = DraftsStore.open(drafts_path, fixture_clock())
        try:
            async with open_checkpointer(checkpoints) as saver:
                assert isinstance(saver, AsyncSqliteSaver)
                graph = graph_with(
                    Scripted(ok(good_plan())),
                    Scripted(ok(accepting())),
                    checkpointer=saver,
                    drafts=drafts,
                )
                result = await graph.ainvoke(
                    PlanState(plan_date=PLAN_DATE, rounds=0), config=config, durability=DURABILITY
                )
                drafts.publish(result["draft"].draft_id)
                return dict(result)
        finally:
            drafts.close()

    async def second_process() -> tuple[dict[str, Any], dict[str, Any]]:
        drafts = DraftsStore.open(drafts_path, fixture_clock())
        try:
            async with open_checkpointer(checkpoints) as saver:
                graph = graph_with(
                    Scripted[DailyPlan](),
                    Scripted[CriticVerdict](),
                    checkpointer=saver,
                    drafts=drafts,
                )
                paused = await graph.aget_state(config)
                resumed = await graph.ainvoke(
                    Command(resume={"approved": True, "reason": "Looks right."}),
                    config=config,
                    durability=DURABILITY,
                )
                return dict(paused.values), dict(resumed)
        finally:
            drafts.close()

    with monkeypatch.context() as earlier:
        earlier.setattr(plan_checks, "ORDERED_PLAN_CHECKS", SEVEN)
        earlier.setattr(agent_steps, "ORDERED_PLAN_CHECKS", SEVEN)
        made = asyncio.run(first_process())
    paused, resumed = asyncio.run(second_process())

    drafts = DraftsStore.open(drafts_path, fixture_clock())
    try:
        record = drafts.get(made["draft"].draft_id)
    finally:
        drafts.close()
    assert len(plan_checks.ORDERED_PLAN_CHECKS) == 8
    assert GRAPH_VERSION == 1
    assert set(paused["verification"].outcomes) == set(SEVEN)
    assert set(resumed["verification"].outcomes) == set(SEVEN)
    assert record is not None
    assert record.decision == "approved"
    assert [item.found for item in record.steps if item.node == "verify"] == ["all 7 checks passed"]


def test_a_decided_plan_from_before_the_check_reads_on_the_family_page_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plan made and approved while there were seven checks is history when the eighth
    arrives. The family page shows its record as written, seven checks passed, and its
    decision as decided, and makes nothing of the check it never ran."""
    with browser(key=True) as client:
        with monkeypatch.context() as earlier:
            earlier.setattr(plan_checks, "ORDERED_PLAN_CHECKS", SEVEN)
            earlier.setattr(agent_steps, "ORDERED_PLAN_CHECKS", SEVEN)
            assert client.post("/student/actions/plan").status_code == 303
            record = state_of(client).drafts.latest_for(PLAN_DATE)
            assert record is not None
            decided = client.post(
                f"/parent/actions/decide/{record.draft_id}", data={"decision": "approve"}
            )
            assert decided.status_code == 303
        before = state_of(client).drafts.get(record.draft_id)
        family = client.get("/parent", headers=PAGE_HEADERS).text
        hers = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        after = state_of(client).drafts.get(record.draft_id)

    assert len(plan_checks.ORDERED_PLAN_CHECKS) == 8
    assert "Found: all 7 checks passed." in family
    assert "8 checks" not in family
    assert "<strong>Looks good.</strong>" in family
    assert "Looks good." in hers
    assert after == before


# ------------------------------------------------------------- the store's own word


def test_the_store_still_refuses_a_bundle_for_another_evening_whatever_the_checks_say() -> None:
    """A composition whose plan is for the 18th, handed to the store as the draft of the
    19th, is refused as a bundle that disagrees with itself, and nothing of it is kept. The
    check in front of it changes nothing about that."""
    drafts = drafts_in_memory()
    early = dated(composed_plan().snapshot.plan, PLAN_DATE - DAY)
    made = composed_plan(early)

    with pytest.raises(IncoherentBundle):
        drafts.record_waiting(
            made.draft,
            thread_id="plan:2026-08-19:abc12345",
            plan_date=PLAN_DATE,
            outcome="accepted",
            plan_assignment_ids=made.snapshot.assignment_ids,
            plan_snapshot=made.snapshot,
        )

    assert drafts.get(made.draft.draft_id) is None
    assert drafts.waiting() == []
