# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

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
from blossom.stores.checkpoints import open_checkpointer
from blossom.stores.drafts import DraftsStore, WriterBusy
from blossom.stores.import_drafts import (
    DRAFT_LIFETIME,
    DraftNotFound,
    DraftRow,
    ImportDraftsStore,
    KindNotWired,
    NewImage,
    new_creation_token,
)
from blossom.stores.paths import UnsafeCheckpointPath
from blossom.stores.project_state import Assignment, ProjectStateStore
from blossom.stores.workload_signals import SIGNAL_RETENTION_DAYS, WorkloadSignalsStore
from tests.support import OBSERVED_AT, PLAN_DATE, SAME_ORIGIN, fixture_clock, fixture_settings


def test_stores_are_built_once_and_shared_across_requests() -> None:
    """Two requests must see the same store instance, not two freshly seeded ones."""
    observed: list[ApplicationState] = []
    app = create_app()

    with TestClient(app, headers=SAME_ORIGIN) as client:
        for _ in range(2):
            response = client.get("/student/due-this-week")
            assert response.status_code == 200
            observed.append(getattr(app.state, STATE_ATTRIBUTE))

    assert observed[0] is observed[1]


def test_state_is_closed_when_the_application_shuts_down() -> None:
    app = create_app()

    with TestClient(app, headers=SAME_ORIGIN) as client:
        client.get("/student/due-this-week")
        state: ApplicationState = getattr(app.state, STATE_ATTRIBUTE)

    with pytest.raises(sqlite3.ProgrammingError):
        state.project_state.due_between(date(2026, 1, 1), date(2026, 12, 31))


def test_using_the_app_without_its_lifespan_fails_with_a_useful_message() -> None:
    """``TestClient(app)`` without ``with`` skips startup, which is easy to do by accident."""
    client = TestClient(create_app(), headers=SAME_ORIGIN)

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
        project_state=empty_store,
        support_rules=substitute.support_rules,
        reflections=substitute.reflections,
        drafts=substitute.drafts,
        checkpointer=substitute.checkpointer,
        traces=substitute.traces,
        tracer=substitute.tracer,
        workload_signals=substitute.workload_signals,
        help_requests=substitute.help_requests,
        import_drafts=substitute.import_drafts,
        real_clock=substitute.real_clock,
    )
    app.dependency_overrides[get_application_state] = lambda: substitute_with_empty_store

    try:
        with TestClient(app, headers=SAME_ORIGIN) as client:
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

    def remembering_open(
        path: pathlib.Path, clock: Clock, monotonic: Callable[[], float] = time.monotonic
    ) -> DraftsStore:
        store = real_open(path, clock, monotonic)
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

    def remembering_open(
        path: pathlib.Path, clock: Clock, monotonic: Callable[[], float] = time.monotonic
    ) -> DraftsStore:
        store = real_open(path, clock, monotonic)
        opened.append(store)
        return store

    async def failing_sweep(*args: object, **kwargs: object) -> None:
        msg = "the saved state could not be read"
        raise RuntimeError(msg)

    monkeypatch.setattr(DraftsStore, "open", staticmethod(remembering_open))
    monkeypatch.setattr(dependencies, "sweep_saved_state", failing_sweep)

    with (
        pytest.raises(RuntimeError, match="could not be read"),
        TestClient(create_app(), headers=SAME_ORIGIN),
    ):
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

    with TestClient(app, headers=SAME_ORIGIN) as client:
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


# ------------------------------------------------------- the import drafts store


def no_rows(readings: object) -> list[DraftRow]:
    """A rows function for planting drafts; the application wires none."""
    return []


def plant_draft(path: pathlib.Path, student_id: str, made_at: datetime) -> str:
    """Make one draft in the file as a process at ``made_at`` would, with a Read attempt
    left open on that process's monotonic clock."""
    planter = ImportDraftsStore.open(
        path,
        FrozenClock(made_at, fixture_clock().zone),
        lambda: 0.0,
        rows={"homework_screenshot": no_rows},
    )
    try:
        added = planter.create(
            student_id,
            kind="homework_screenshot",
            made_by="parent",
            creation_token=new_creation_token(),
            images=[NewImage(png=f"synthetic-{made_at}".encode(), width=4, height=3)],
        )
        assert added is not None
        assert planter.start_attempt(student_id, added.draft_id, added.added) is not None
        return added.draft_id
    finally:
        planter.close()


def attempts_in_file(path: pathlib.Path) -> list[object]:
    """Every attempt id a draft or an image of one holds in the file."""
    with sqlite3.connect(path) as connection:
        held = connection.execute(
            "SELECT attempt_id FROM import_drafts UNION ALL "
            "SELECT attempt_id FROM import_draft_images"
        ).fetchall()
    return [attempt for (attempt,) in held]


REAL_NOW = OBSERVED_AT
"""The real clock's instant in these tests, whatever the household date is pinned to."""


def real_clock() -> Clock:
    return FrozenClock(REAL_NOW, fixture_clock().zone)


def test_the_import_drafts_store_opens_with_the_state_and_closes_with_it(
    tmp_path: pathlib.Path,
) -> None:
    settings = fixture_settings(**file_settings(tmp_path))
    app = create_app(settings)

    with TestClient(app, headers=SAME_ORIGIN) as client:
        client.get("/student/due-this-week")
        state: ApplicationState = getattr(app.state, STATE_ATTRIBUTE)
        student = state.project_state.student_id()
        for kind in ("homework_screenshot", "grade_screenshot"):
            with pytest.raises(KindNotWired):
                state.import_drafts.create(
                    student,
                    kind=kind,
                    made_by="parent",
                    creation_token=new_creation_token(),
                    images=[NewImage(png=b"synthetic", width=1, height=1)],
                )

    with pytest.raises(sqlite3.ProgrammingError):
        state.import_drafts.snapshot(student, "0" * 32)


def test_a_start_clears_every_attempt_and_sweeps_expired_drafts(tmp_path: pathlib.Path) -> None:
    settings = fixture_settings(**file_settings(tmp_path))
    first = build_application_state(settings, InMemorySaver(), real_clock=real_clock())
    student = first.project_state.student_id()
    first.close()
    expired = plant_draft(settings.database_path, student, REAL_NOW - DRAFT_LIFETIME)
    open_draft = plant_draft(settings.database_path, student, REAL_NOW - timedelta(hours=1))
    assert len([held for held in attempts_in_file(settings.database_path) if held]) == 4

    state = build_application_state(settings, InMemorySaver(), real_clock=real_clock())
    try:
        assert attempts_in_file(settings.database_path) == [None, None]
        with pytest.raises(DraftNotFound):
            state.import_drafts.snapshot(student, expired)
        kept = state.import_drafts.snapshot(student, open_draft)
    finally:
        state.close()

    assert (len(kept.images), kept.attempt_ends) == (1, None)


def test_the_scheduled_sweep_removes_expired_drafts(tmp_path: pathlib.Path) -> None:
    settings = fixture_settings(**file_settings(tmp_path))

    async def go() -> tuple[str, str]:
        async with open_checkpointer(settings.checkpoint_path) as checkpointer:
            state = build_application_state(settings, checkpointer, real_clock=real_clock())
            try:
                student = state.project_state.student_id()
                expired = plant_draft(settings.database_path, student, REAL_NOW - DRAFT_LIFETIME)
                kept = plant_draft(settings.database_path, student, REAL_NOW - timedelta(hours=1))
                await sweep_aged(state)
                with pytest.raises(DraftNotFound):
                    state.import_drafts.snapshot(student, expired)
                return kept, state.import_drafts.snapshot(student, kept).draft_id
            finally:
                state.close()

    planted, found = asyncio.run(go())
    assert found == planted


@pytest.mark.parametrize(
    ("step", "fault"),
    [
        ("clear_attempts", WriterBusy()),
        ("sweep", WriterBusy()),
        ("sweep", sqlite3.DatabaseError("synthetic")),
    ],
)
def test_a_start_whose_import_drafts_store_fails_closes_what_it_opened(
    monkeypatch: pytest.MonkeyPatch, step: str, fault: Exception
) -> None:
    """A busy or failed draft store stops the start, as the other stores' sweeps do."""
    opened: list[DraftsStore | ImportDraftsStore] = []
    real_drafts_open = DraftsStore.open
    real_imports_open = ImportDraftsStore.open

    def remembering_drafts(
        path: pathlib.Path, clock: Clock, monotonic: Callable[[], float] = time.monotonic
    ) -> DraftsStore:
        store = real_drafts_open(path, clock, monotonic)
        opened.append(store)
        return store

    def remembering_imports(*args: object, **kwargs: object) -> ImportDraftsStore:
        store = real_imports_open(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(store)
        return store

    def failing(*args: object, **kwargs: object) -> object:
        raise fault

    monkeypatch.setattr(DraftsStore, "open", staticmethod(remembering_drafts))
    monkeypatch.setattr(ImportDraftsStore, "open", staticmethod(remembering_imports))
    monkeypatch.setattr(ImportDraftsStore, step, failing)

    with pytest.raises(type(fault)):
        build_application_state(fixture_settings(), InMemorySaver())

    assert [type(store) for store in opened] == [DraftsStore, ImportDraftsStore]
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].waiting()  # type: ignore[union-attr]
    with pytest.raises(sqlite3.ProgrammingError):
        opened[1].snapshot("student", "0" * 32)  # type: ignore[union-attr]


def test_a_scheduled_sweep_whose_draft_store_is_busy_still_runs_the_others(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The draft sweep runs last, and its failure reaches the schedule, which logs it."""
    settings = fixture_settings(**file_settings(tmp_path))

    def busy(*args: object, **kwargs: object) -> object:
        raise WriterBusy

    async def go() -> tuple[int, int]:
        async with open_checkpointer(settings.checkpoint_path) as checkpointer:
            state = build_application_state(settings, checkpointer)
            try:
                age_a_signal_into(settings.database_path)
                before = signals_in_file(settings.database_path)
                monkeypatch.setattr(state.import_drafts, "sweep", busy)
                with pytest.raises(WriterBusy):
                    await sweep_aged(state)
                return before, signals_in_file(settings.database_path)
            finally:
                state.close()

    assert asyncio.run(go()) == (1, 0)
