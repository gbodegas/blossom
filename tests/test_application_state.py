"""Tests for the application-scoped store lifecycle.

Three properties matter here, and none of them held before:

1. Stores are built once, not per request. The scaffold reopened SQLite and
   re-seeded every assignment on each call.
2. A connection shared across FastAPI's worker threads does not raise. This is
   the hazard the previous per-request connections happened to avoid.
3. Routes take their stores from a dependency, so a test can substitute them.
"""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.dependencies import (
    STATE_ATTRIBUTE,
    ApplicationState,
    build_application_state,
    get_application_state,
)
from blossom.settings import Settings
from blossom.stores.project_state import Assignment, ProjectStateStore


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
    """Synchronous handlers run in a thread pool, so the store is used from many threads.

    Without ``check_same_thread=False`` and the store's lock, this raises
    ``sqlite3.ProgrammingError``.
    """
    state = build_application_state(Settings.from_environment({}))
    try:

        def read() -> int:
            found = state.project_state.due_between(date(2026, 1, 1), date(2026, 12, 31))
            return len(found)

        with ThreadPoolExecutor(max_workers=8) as pool:
            counts = list(pool.map(lambda _: read(), range(64)))
    finally:
        state.close()

    assert counts and len(set(counts)) == 1


def test_concurrent_writes_are_serialised() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    store = ProjectStateStore(connection)
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
    empty_store = ProjectStateStore(connection)
    settings = Settings.from_environment({})
    substitute = build_application_state(settings)
    substitute_with_empty_store = ApplicationState(
        settings=settings,
        clock=substitute.clock,
        source=substitute.source,
        project_state=empty_store,
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
