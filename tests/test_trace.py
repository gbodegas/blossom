"""The local tracer: the framework's run tree, redacted, in a file of its own, swept in time.

The tracer is attached to a scripted graph run, so the tree it receives is
the real one the framework builds, with no model and no network.
"""

import asyncio
import pathlib
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import BaseMessage
from langchain_core.tracers.schemas import Run
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from blossom.agent.graph import ModelAnswer, PlanState
from blossom.agent.runs import DURABILITY, run_config
from blossom.agent.trace import LocalRunTracer, Redactor, as_json, traced, unredacted
from blossom.app import create_app
from blossom.clock import FrozenClock
from blossom.dependencies import STATE_ATTRIBUTE, ApplicationState, build_application_state
from blossom.routes.parent import plan_graphs
from blossom.settings import TRACE_PATH_VARIABLE
from blossom.stores.checkpoints import UnsafeCheckpointPath
from blossom.stores.project_state import Assignment
from blossom.stores.traces import TRACE_RETENTION_DAYS, TracedRun, TraceStore
from tests.support import (
    FIXTURE_TIMEZONE,
    OBSERVED_AT,
    PLAN_DATE,
    Scripted,
    accepting,
    fixture_clock,
    fixture_settings,
    fixture_week_plan,
    good_plan,
    graph_with,
    ok,
    scripted_graphs,
)

ZONE = ZoneInfo(FIXTURE_TIMEZONE)


def traced_run(
    graph_kwargs: dict[str, Any] | None = None,
    *,
    thread: str = "plan:traced",
    redact: Redactor = unredacted,
    store: TraceStore | None = None,
) -> tuple[TraceStore, dict[str, Any]]:
    """Run the scripted graph with the tracer attached and return the store and the result."""
    store = store or TraceStore(
        sqlite3.connect(":memory:", check_same_thread=False), fixture_clock()
    )
    tracer = LocalRunTracer(store, redact=redact)
    graph = graph_with(Scripted(ok(good_plan())), Scripted(ok(accepting())), **(graph_kwargs or {}))

    async def go() -> dict[str, Any]:
        return dict(
            await graph.ainvoke(
                PlanState(plan_date=PLAN_DATE, rounds=0),
                config=run_config(thread, callbacks=[tracer]),
                durability=DURABILITY,
            )
        )

    return store, asyncio.run(go())


def test_a_run_leaves_the_whole_tree_under_its_thread() -> None:
    store, result = traced_run()

    rows = store.runs_for_thread("plan:traced")
    roots = [row for row in rows if row.parent_run_id is None]
    names = {row.name for row in rows}
    assert result["outcome"] == "accepted"
    assert len(roots) == 1
    assert {row.trace_id for row in rows} == {roots[0].run_id}
    assert names >= {"retrieve", "plan", "verify", "critique", "compose", "require_human_approval"}
    assert all(row.run_type == "chain" for row in rows)
    assert all(row.started_at.tzinfo is not None for row in rows)


def test_what_went_in_and_came_out_is_kept_as_text() -> None:
    store, _ = traced_run()

    rows = {row.name: row for row in store.runs_for_thread("plan:traced")}
    assert '"plan_date": "2026-08-19"' in rows["retrieve"].inputs
    assert rows["compose"].outputs is not None
    assert "Canal Era comparison essay" in rows["compose"].outputs
    assert rows["require_human_approval"].error is not None
    assert "GraphInterrupt" in rows["require_human_approval"].error


def test_the_redaction_hook_sees_every_input_output_and_error() -> None:
    store, _ = traced_run(redact=lambda text: text.replace("Canal Era", "[a title]"))

    rows = store.runs_for_thread("plan:traced")
    everything = "\n".join(
        part for row in rows for part in (row.inputs, row.outputs or "", row.error or "")
    )
    assert "Canal Era" not in everything
    assert "[a title]" in everything


def test_a_resumed_thread_adds_a_second_tree_to_the_same_thread() -> None:
    store = TraceStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())
    tracer = LocalRunTracer(store)
    graph = graph_with(Scripted(ok(good_plan())), Scripted(ok(accepting())))
    config = run_config("plan:resumed", callbacks=[tracer])

    async def go() -> None:
        await graph.ainvoke(
            PlanState(plan_date=PLAN_DATE, rounds=0), config=config, durability=DURABILITY
        )
        resume: Command[Any] = Command(resume={"approved": True, "reason": None})
        await graph.ainvoke(resume, config=config, durability=DURABILITY)

    asyncio.run(go())

    rows = store.runs_for_thread("plan:resumed")
    roots = [row for row in rows if row.parent_run_id is None]
    assert len(roots) == 2
    assert "record_decision" in {row.name for row in rows}


def test_old_traces_are_swept_and_recent_ones_kept(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "traces.sqlite3"
    then = TraceStore.open(path, FrozenClock(OBSERVED_AT, ZONE))
    traced_run(store=then, thread="plan:old")
    old_rows = then.count()
    then.close()
    later = OBSERVED_AT + timedelta(days=TRACE_RETENTION_DAYS + 1)
    now = TraceStore.open(path, FrozenClock(later, ZONE))

    removed = now.sweep()
    traced_run(store=now, thread="plan:new")

    assert old_rows > 0
    assert removed == old_rows
    assert now.runs_for_thread("plan:old") == []
    assert now.runs_for_thread("plan:new") != []
    now.close()


def test_recording_a_run_sweeps_what_has_aged_out(tmp_path: pathlib.Path) -> None:
    """The sweep rides on every recorded run, so a long-lived process needs no timer."""
    path = tmp_path / "traces.sqlite3"
    then = TraceStore.open(path, FrozenClock(OBSERVED_AT, ZONE))
    traced_run(store=then, thread="plan:old")
    then.close()
    later = OBSERVED_AT + timedelta(days=TRACE_RETENTION_DAYS + 1)
    now = TraceStore.open(path, FrozenClock(later, ZONE))

    traced_run(store=now, thread="plan:new")

    assert now.runs_for_thread("plan:old") == []
    assert now.count() == len(now.runs_for_thread("plan:new"))
    now.close()


def test_a_trace_within_the_window_survives_a_sweep() -> None:
    store, _ = traced_run()

    assert store.sweep() == 0
    assert store.runs_for_thread("plan:traced") != []


def test_recording_a_tree_again_leaves_one_copy() -> None:
    store = TraceStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())
    run = TracedRun(
        run_id="r1",
        trace_id="r1",
        parent_run_id=None,
        thread_id="plan:x",
        name="LangGraph",
        run_type="chain",
        started_at=datetime(2026, 8, 19, 22, 0, tzinfo=UTC),
        ended_at=None,
        inputs="{}",
        outputs=None,
        error=None,
        children=(
            TracedRun(
                run_id="r2",
                trace_id="r1",
                parent_run_id="r1",
                thread_id="plan:x",
                name="retrieve",
                run_type="chain",
                started_at=datetime(2026, 8, 19, 22, 0, 1, tzinfo=UTC),
                ended_at=datetime(2026, 8, 19, 22, 0, 2, tzinfo=UTC),
                inputs="{}",
                outputs="{}",
                error=None,
            ),
        ),
    )

    store.record(run)
    store.record(run)

    assert [row.run_id for row in store.runs_for_thread("plan:x")] == ["r1", "r2"]
    assert store.count() == 2


def test_the_file_is_refused_where_the_saved_state_store_refuses_it() -> None:
    with pytest.raises(UnsafeCheckpointPath):
        TraceStore.open(pathlib.Path(":memory:"), fixture_clock())


def test_pydantic_values_are_written_by_their_fields() -> None:
    assignment = Assignment(
        assignment_id="a",
        course="Science",
        title="Lab report",
        due_date=PLAN_DATE,
        dependencies=[],
        reported_submission_status="not_started",
    )

    text = as_json({"assignments": [assignment], "when": PLAN_DATE})

    assert '"title": "Lab report"' in text
    assert '"when": "2026-08-19"' in text


def test_the_application_traces_the_runs_its_routes_start(tmp_path: pathlib.Path) -> None:
    trace_file = tmp_path / "traces.sqlite3"
    settings = fixture_settings(
        BLOSSOM_TODAY=PLAN_DATE.isoformat(), **{TRACE_PATH_VARIABLE: str(trace_file)}
    )
    app = create_app(settings)
    app.dependency_overrides[plan_graphs] = scripted_graphs(
        lambda: [fixture_week_plan()], lambda: [accepting()]
    )

    with TestClient(app) as running:
        started = running.post("/parent/plans", json={}).json()
        state: ApplicationState = getattr(app.state, STATE_ATTRIBUTE)
        rows = state.traces.runs_for_thread(started["thread_id"])

    assert [row.name for row in rows if row.parent_run_id is None] == ["LangGraph"]
    assert "compose" in {row.name for row in rows}
    assert trace_file.is_file()


def test_the_thread_id_comes_from_the_metadata_the_run_configuration_states() -> None:
    """The framework also copies it there on its own; the configuration does not rely on that."""
    config = run_config("plan:stated")
    stated = Run(
        id=uuid4(),
        name="LangGraph",
        run_type="chain",
        inputs={},
        start_time=datetime(2026, 8, 19, 22, 0, tzinfo=UTC),
        extra={"metadata": dict(config["metadata"])},
    )
    unstated = Run(
        id=uuid4(),
        name="LangGraph",
        run_type="chain",
        inputs={},
        start_time=datetime(2026, 8, 19, 22, 0, tzinfo=UTC),
        extra={},
    )

    assert traced(stated, unredacted).thread_id == "plan:stated"
    assert traced(unstated, unredacted).thread_id is None


class ThroughAModel:
    """A planner or critic that goes through a framework chat model, as the real seam does.

    The fake model answers with a fixed line; the parsed value comes from the
    script. What matters is that the framework sees a model call inside the
    node.
    """

    def __init__(self, parsed: object) -> None:
        self.model = FakeListChatModel(responses=["{}"])
        self.parsed = parsed

    async def __call__(self, messages: Sequence[BaseMessage]) -> ModelAnswer[Any]:
        await self.model.ainvoke(list(messages))
        return ModelAnswer(parsed=self.parsed, stop_reason="end_turn", parsing_error=None)


def test_a_model_call_inside_a_node_is_a_run_beneath_that_node() -> None:
    store = TraceStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())
    tracer = LocalRunTracer(store)
    graph = graph_with(ThroughAModel(good_plan()), ThroughAModel(accepting()))

    async def go() -> None:
        await graph.ainvoke(
            PlanState(plan_date=PLAN_DATE, rounds=0),
            config=run_config("plan:modeled", callbacks=[tracer]),
            durability=DURABILITY,
        )

    asyncio.run(go())

    rows = store.runs_for_thread("plan:modeled")
    by_id = {row.run_id: row for row in rows}
    model_runs = [row for row in rows if row.run_type == "llm"]
    assert [by_id[str(row.parent_run_id)].name for row in model_runs] == ["plan", "critique"]
    assert all(row.outputs is not None and "generations" in row.outputs for row in model_runs)
    assert all(row.inputs for row in model_runs)


def test_a_persisted_tree_is_forgotten_by_the_tracer() -> None:
    store = TraceStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())
    tracer = LocalRunTracer(store)
    graph = graph_with(
        Scripted(ok(good_plan()), ok(good_plan())), Scripted(ok(accepting()), ok(accepting()))
    )

    async def go() -> None:
        for thread in ("plan:one", "plan:two"):
            await graph.ainvoke(
                PlanState(plan_date=PLAN_DATE, rounds=0),
                config=run_config(thread, callbacks=[tracer]),
                durability=DURABILITY,
            )

    asyncio.run(go())

    assert store.count() > 0
    assert tracer.order_map == {}
    assert tracer.run_map == {}


def test_the_redactor_sees_names_as_written_not_as_escapes() -> None:
    run = Run(
        id=uuid4(),
        name="plan",
        run_type="chain",
        inputs={"title": "Ensayo de José"},
        outputs={"note": "para José"},
        start_time=datetime(2026, 8, 19, 22, 0, tzinfo=UTC),
        extra={},
    )

    recorded = traced(run, lambda text: text.replace("José", "[name]"))

    assert "José" not in recorded.inputs
    assert "José" not in (recorded.outputs or "")
    assert "[name]" in recorded.inputs
    assert as_json({"title": "José"}) == '{"title": "José"}'


def test_traces_are_stamped_by_the_real_clock_even_when_the_household_clock_is_pinned() -> None:
    settings = fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat())
    state = build_application_state(settings, InMemorySaver())
    try:
        graph = graph_with(Scripted(ok(good_plan())), Scripted(ok(accepting())))

        async def go() -> None:
            await graph.ainvoke(
                PlanState(plan_date=PLAN_DATE, rounds=0),
                config=run_config("plan:pinned", callbacks=[state.tracer]),
                durability=DURABILITY,
            )

        asyncio.run(go())
        rows = state.traces.runs_for_thread("plan:pinned")
    finally:
        state.close()

    assert state.clock.today() == PLAN_DATE
    assert rows
    assert all(row.recorded_at is not None for row in rows)
    assert all(row.recorded_at.date() > PLAN_DATE for row in rows if row.recorded_at is not None)
