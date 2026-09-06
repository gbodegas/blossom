"""Tests for the application-scoped store lifecycle.

Three properties are checked here:

1. Stores are built once at startup and shared across requests.
2. One SQLite connection is safe to use from FastAPI's worker threads.
3. Routes take their stores from a dependency, so a test can substitute them.
"""

import pathlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from blossom import dependencies
from blossom.app import create_app
from blossom.clock import Clock
from blossom.dependencies import (
    STATE_ATTRIBUTE,
    ApplicationState,
    build_application_state,
    get_application_state,
)
from blossom.settings import TRACE_PATH_VARIABLE
from blossom.stores.checkpoints import UnsafeCheckpointPath
from blossom.stores.drafts import DraftsStore
from blossom.stores.project_state import Assignment, ProjectStateStore
from tests.support import fixture_clock, fixture_settings


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
