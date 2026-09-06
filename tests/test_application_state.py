"""Tests for the application-scoped store lifecycle.

Three properties are checked here:

1. Stores are built once at startup and shared across requests.
2. One SQLite connection is safe to use from FastAPI's worker threads.
3. Routes take their stores from a dependency, so a test can substitute them.
"""

import asyncio
import pathlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from blossom import dependencies
from blossom.app import create_app
from blossom.clock import Clock, FrozenClock
from blossom.dependencies import (
    STATE_ATTRIBUTE,
    ApplicationState,
    build_application_state,
    get_application_state,
    repeat,
    sweep_aged,
)
from blossom.settings import (
    CHECKPOINT_PATH_VARIABLE,
    DATABASE_PATH_VARIABLE,
    TRACE_PATH_VARIABLE,
)
from blossom.stores.checkpoints import UnsafeCheckpointPath, open_checkpointer
from blossom.stores.drafts import DraftsStore
from blossom.stores.project_state import Assignment, ProjectStateStore
from blossom.stores.workload_signals import SIGNAL_RETENTION_DAYS, WorkloadSignalsStore
from tests.support import OBSERVED_AT, PLAN_DATE, fixture_clock, fixture_settings


def test_stores_are_built_once_and_shared_across_requests() -> None:
    """Two requests must see the same store instance, not two freshly seeded ones."""
    observed: list[ApplicationState] = []
    app = create_app()

    with TestClient(app) as client:
        for _ in range(2):
            response = client.get("/student/due-this-week")
            assert response.status_code == 200
            observed.append(getattr(app.state, STATE_ATTRIBUTE))

    assert observed[0] is observed[1]


def test_state_is_closed_when_the_application_shuts_down() -> None:
    app = create_app()

    with TestClient(app) as client:
        client.get("/student/due-this-week")
        state: ApplicationState = getattr(app.state, STATE_ATTRIBUTE)

    with pytest.raises(sqlite3.ProgrammingError):
        state.project_state.due_between(date(2026, 1, 1), date(2026, 12, 31))


def test_using_the_app_without_its_lifespan_fails_with_a_useful_message() -> None:
    """``TestClient(app)`` without ``with`` skips startup, which is easy to do by accident."""
    client = TestClient(create_app())

    with pytest.raises(RuntimeError, match="lifespan"):
        client.get("/student/due-this-week")


def test_shared_connection_survives_concurrent_reads() -> None:
    """Reads from many threads are safe: ``check_same_thread=False`` plus the store's lock."""
    state = build_application_state(fixture_settings(), InMemorySaver())
    try:

        def read() -> int:
            found = state.project_state.due_between(date(2026, 1, 1), date(2026, 12, 31))
            return len(found)

        with ThreadPoolExecutor(max_workers=8) as pool:
            counts = list(pool.map(lambda _: read(), range(64)))
    finally:
        state.close()

    assert counts
    assert len(set(counts)) == 1


def test_concurrent_writes_are_serialised() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    store = ProjectStateStore(connection, fixture_clock())
    barrier = threading.Barrier(8)

    def write(index: int) -> None:
        barrier.wait()
        store.upsert_assignments(
            [
                Assignment(
                    assignment_id=f"assignment-{index}",
                    course="Algebra II",
                    title=f"Problem set {index}",
                    due_date=date(2026, 8, 21),
                    dependencies=[],
                    reported_submission_status="not_started",
                )
            ]
        )

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(8)))
        stored = store.due_between(date(2026, 8, 21), date(2026, 8, 21))
    finally:
        store.close()

    assert len(stored) == 8


def test_dependency_can_be_overridden_to_substitute_stores() -> None:
    """The seam that makes future route tests cheap: no filesystem, no environment."""
    app = create_app()
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    empty_store = ProjectStateStore(connection, fixture_clock())
    settings = fixture_settings()
    substitute = build_application_state(settings, InMemorySaver())
    substitute_with_empty_store = ApplicationState(
        settings=settings,
        clock=substitute.clock,
        source=substitute.source,
        project_state=empty_store,
        support_rules=substitute.support_rules,
        reflections=substitute.reflections,
        drafts=substitute.drafts,
        checkpointer=substitute.checkpointer,
        traces=substitute.traces,
        tracer=substitute.tracer,
        workload_signals=substitute.workload_signals,
    )
    app.dependency_overrides[get_application_state] = lambda: substitute_with_empty_store

    try:
        with TestClient(app) as client:
            response = client.get("/student/due-this-week")
    finally:
        app.dependency_overrides.clear()
        substitute.close()
        empty_store.close()

    assert response.status_code == 200
    assert "Canal Era comparison essay" not in response.text


def test_a_startup_that_fails_late_closes_what_it_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A refused trace path must not leave the drafts file open behind it."""
    opened: list[DraftsStore] = []
    real_open = DraftsStore.open

    def remembering_open(path: pathlib.Path, clock: Clock) -> DraftsStore:
        store = real_open(path, clock)
        opened.append(store)
        return store

    monkeypatch.setattr(DraftsStore, "open", staticmethod(remembering_open))
    synced = tmp_path / "OneDrive" / "traces.sqlite3"
    settings = fixture_settings(**{TRACE_PATH_VARIABLE: str(synced)})

    with pytest.raises(UnsafeCheckpointPath):
        build_application_state(settings, InMemorySaver())

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].waiting()


def test_a_startup_whose_sweep_fails_still_closes_the_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep runs inside the block that owns the state, so a failure still closes it."""
    opened: list[DraftsStore] = []
    real_open = DraftsStore.open

    def remembering_open(path: pathlib.Path, clock: Clock) -> DraftsStore:
        store = real_open(path, clock)
        opened.append(store)
        return store

    async def failing_sweep(*args: object, **kwargs: object) -> None:
        msg = "the saved state could not be read"
        raise RuntimeError(msg)

    monkeypatch.setattr(DraftsStore, "open", staticmethod(remembering_open))
    monkeypatch.setattr(dependencies, "sweep_saved_state", failing_sweep)

    with pytest.raises(RuntimeError, match="could not be read"), TestClient(create_app()):
        pass

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].waiting()


# ------------------------------------------------------- retention on a schedule


def signals_in_file(path: pathlib.Path) -> int:
    """How many signal rows the file physically holds, whatever the reads leave out."""
    with sqlite3.connect(path) as connection:
        (count,) = connection.execute("SELECT count(*) FROM workload_signals").fetchone()
    return int(count)


def age_a_signal_into(path: pathlib.Path) -> None:
    """Write one signal stamped well past its week, as a long-running process would hold."""
    long_ago = FrozenClock(
        OBSERVED_AT - timedelta(days=SIGNAL_RETENTION_DAYS + 1), fixture_clock().zone
    )
    aged = WorkloadSignalsStore(sqlite3.connect(path, check_same_thread=False), long_ago)
    aged.record(PLAN_DATE)
    aged.close()


def file_settings(tmp_path: pathlib.Path) -> dict[str, str]:
    return {
        DATABASE_PATH_VARIABLE: str(tmp_path / "blossom.sqlite3"),
        CHECKPOINT_PATH_VARIABLE: str(tmp_path / "checkpoints.sqlite3"),
        TRACE_PATH_VARIABLE: str(tmp_path / "traces.sqlite3"),
    }


def test_the_schedule_keeps_ticking_past_a_failure_until_canceled() -> None:
    async def go() -> int:
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            ticks += 1
            if ticks == 1:
                msg = "the first sweep fails"
                raise RuntimeError(msg)

        task = asyncio.create_task(repeat(0.01, tick))
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.wait([task])
        return ticks

    assert asyncio.run(go()) >= 3


def test_one_sweep_deletes_what_the_reads_already_leave_out(tmp_path: pathlib.Path) -> None:
    settings = fixture_settings(**file_settings(tmp_path))

    async def go() -> tuple[int, int]:
        async with open_checkpointer(settings.checkpoint_path) as checkpointer:
            state = build_application_state(settings, checkpointer)
            try:
                age_a_signal_into(settings.database_path)
                before = signals_in_file(settings.database_path)
                await sweep_aged(state)
                return before, signals_in_file(settings.database_path)
            finally:
                state.close()

    assert asyncio.run(go()) == (1, 0)


def test_a_running_process_sweeps_on_its_own(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signal past its week goes without a restart; the interval is shortened to show it."""
    monkeypatch.setattr(dependencies, "SWEEP_INTERVAL_SECONDS", 0.02)
    settings = fixture_settings(**file_settings(tmp_path))
    app = create_app(settings)

    with TestClient(app) as client:
        client.get("/student/due-this-week")
        age_a_signal_into(settings.database_path)
        held_by_the_page = client.get("/student/workload-signals").json()
        for _ in range(100):
            if signals_in_file(settings.database_path) == 0:
                break
            time.sleep(0.02)
        remaining = signals_in_file(settings.database_path)

    assert held_by_the_page == []
    assert remaining == 0
