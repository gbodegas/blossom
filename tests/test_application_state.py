# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Tests for the application-scoped store lifecycle.

Three properties are checked here:

1. Stores are built once at startup and shared across requests.
2. One SQLite connection is safe to use from FastAPI's worker threads.
3. Routes take their stores from a dependency, so a test can substitute them.
"""

import asyncio
import hashlib
import json
import logging
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
from blossom.screenshots import homework_rows
from blossom.settings import (
    CHECKPOINT_PATH_VARIABLE,
    DATABASE_PATH_VARIABLE,
    TRACE_PATH_VARIABLE,
)
from blossom.stores import import_drafts
from blossom.stores.checkpoints import open_checkpointer
from blossom.stores.drafts import DraftsStore, WriterBusy
from blossom.stores.gradebook import ADD_ACCEPTANCE
from blossom.stores.import_drafts import (
    DRAFT_LIFETIME,
    DraftNotFound,
    DraftRow,
    ImportDraftsStore,
    KindNotWired,
    NewImage,
    ReadTiming,
    StoredReading,
    UploadedReading,
    instant,
    new_creation_token,
)
from blossom.stores.paths import UnsafeCheckpointPath
from blossom.stores.project_state import Assignment, ProjectStateStore
from blossom.stores.workload_signals import SIGNAL_RETENTION_DAYS, WorkloadSignalsStore
from tests.support import (
    OBSERVED_AT,
    PLAN_DATE,
    SAME_ORIGIN,
    fixture_clock,
    fixture_settings,
    homework_answer,
    homework_card,
    stored_homework,
)


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
        made = state.import_drafts.create(
            student,
            kind="homework_screenshot",
            made_by="parent",
            creation_token=new_creation_token(),
            images=[NewImage(png=b"synthetic", width=1, height=1)],
        )
        assert made is not None
        with pytest.raises(KindNotWired):
            state.import_drafts.create(
                student,
                kind="grade_screenshot",
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


# ------------------------------------------------------------------ drafts it can't work out

SENTINEL_ANSWER = homework_answer(
    active_student={"name": "Sentinel-Name-N1", "uncertain": "Sentinel-Note-N2"},
    cards=[homework_card(course="Sentinel-Course-C3", title="Sentinel-Title-T4")],
)
"""A reading whose name, note and school text are each a sentinel."""

BROKEN_ACCEPTANCE = "Sentinel-Accept-A5 is not a list of pairs"
"""An acceptance record's pairs that don't decode."""


def plant_read_draft(path: pathlib.Path, student_id: str, made_at: datetime, answer: object) -> str:
    """Make a homework draft at ``made_at`` with one screenshot and its stored reading."""
    planter = ImportDraftsStore.open(
        path,
        FrozenClock(made_at, fixture_clock().zone),
        lambda: 0.0,
        rows={"homework_screenshot": homework_rows},
    )
    try:
        png = f"synthetic-{made_at}".encode()
        added = planter.create(
            student_id,
            kind="homework_screenshot",
            made_by="parent",
            creation_token=new_creation_token(),
            images=[NewImage(png=png, width=4, height=3)],
        )
        assert added is not None
        attempt = planter.start_attempt(student_id, added.draft_id, added.added)
        assert attempt is not None
        stored = stored_homework("planted", answer)
        assert planter.store_reading(
            student_id,
            added.draft_id,
            attempt_id=attempt.attempt_id,
            image_id=added.added[0],
            sha256=hashlib.sha256(png).hexdigest(),
            reading=UploadedReading(
                stored.kind, stored.prompt_version, stored.schema_version, stored.body
            ),
            timing=ReadTiming(1.0, 10, 5),
        )
        return added.draft_id
    finally:
        planter.close()


def plant_acceptance(path: pathlib.Path, student_id: str, draft_id: str, accepted: str) -> None:
    """An acceptance record of the draft as a save writes it, its pairs as given."""
    with sqlite3.connect(path) as connection:
        connection.execute(
            ADD_ACCEPTANCE,
            (
                f"acceptance-{draft_id}",
                student_id,
                "homework_screenshot",
                draft_id,
                None,
                None,
                None,
                accepted,
                "matches",
                "shown",
                None,
                1,
                0,
                0,
                0,
                1,
                0,
                1,
                OBSERVED_AT.isoformat(),
                "parent",
            ),
        )
    connection.close()


def keys_of(path: pathlib.Path, draft_id: str) -> list[str]:
    """Every row key the homework rows function gives the draft's stored readings."""
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT sha256, reading_kind, prompt_version, schema_version, reading "
            "FROM import_draft_images WHERE draft_id = ?",
            (draft_id,),
        ).fetchall()
    connection.close()
    return [row.key for row in homework_rows([StoredReading(*row) for row in rows])]


def drafts_in_file(path: pathlib.Path) -> dict[str, tuple[object, int]]:
    """Each draft in the file: whether it is closed, and how many images it holds."""
    with sqlite3.connect(path) as connection:
        found = {
            str(draft_id): (closed_at, int(images))
            for draft_id, closed_at, images in connection.execute(
                "SELECT d.draft_id, d.closed_at, "
                "(SELECT count(*) FROM import_draft_images i WHERE i.draft_id = d.draft_id) "
                "FROM import_drafts d"
            )
        }
    connection.close()
    return found


def echoing_rows(readings: object) -> object:
    """A rows function that fails on a sentinel reading with an error quoting it, as a rows
    function failing on a reading's text could; any other reading it reads as the real one."""
    bodies = [reading.body for reading in readings]  # type: ignore[attr-defined]
    if any("Sentinel" in body for body in bodies):
        message = f"couldn't read {bodies[0]}"
        raise ValueError(message)
    return homework_rows(readings)  # type: ignore[arg-type]


def plant_three(path: pathlib.Path, student_id: str, source: str) -> dict[str, str]:
    """In the sweep's order: a draft that can't be worked out, then an expired one, then a
    finished one. Drafts expire in the order they are made, so the expired draft's times are
    set in the file to put it after the failing one."""
    failing = plant_read_draft(path, student_id, REAL_NOW - timedelta(hours=2), SENTINEL_ANSWER)
    if source == "acceptance":
        plant_acceptance(path, student_id, failing, BROKEN_ACCEPTANCE)
    expired = plant_draft(path, student_id, REAL_NOW - timedelta(minutes=30))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE import_drafts SET expires_at = ? WHERE draft_id = ?",
            (instant(REAL_NOW - timedelta(minutes=1)), expired),
        )
    connection.close()
    finished = plant_read_draft(
        path, student_id, REAL_NOW - timedelta(minutes=10), homework_answer()
    )
    pairs = [[key, f"assignment-{place}"] for place, key in enumerate(keys_of(path, finished))]
    plant_acceptance(path, student_id, finished, json.dumps(pairs))
    return {"failing": failing, "expired": expired, "finished": finished}


SOURCES = ["acceptance", "rows"]
KIND_OF = {"acceptance": "JSONDecodeError", "rows": "ValueError"}
PASSED_OVER = "the import draft sweep left a draft it couldn't work out open: {}"


def wired(monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    """For a failing rows function, the application's wiring takes the echoing one."""
    if source == "rows":
        monkeypatch.setattr(dependencies, "homework_rows", echoing_rows)


def assert_swept_around(path: pathlib.Path, drafts: dict[str, str]) -> None:
    found = drafts_in_file(path)
    assert drafts["expired"] not in found
    assert found[drafts["finished"]][0] is not None
    assert found[drafts["finished"]][1] == 0
    assert found[drafts["failing"]] == (None, 1)


@pytest.mark.parametrize("source", SOURCES)
def test_a_start_passes_over_a_draft_it_cant_work_out_and_sweeps_the_rest(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    source: str,
) -> None:
    """The draft after a failing one is still deleted at expiry, the finished one still
    closes, and the start goes on; the failure is logged by its kind alone."""
    settings = fixture_settings(**file_settings(tmp_path))
    first = build_application_state(settings, InMemorySaver(), real_clock=real_clock())
    student = first.project_state.student_id()
    first.close()
    drafts = plant_three(settings.database_path, student, source)
    wired(monkeypatch, source)

    with caplog.at_level(logging.DEBUG):
        state = build_application_state(settings, InMemorySaver(), real_clock=real_clock())
    state.close()

    assert_swept_around(settings.database_path, drafts)
    assert PASSED_OVER.format(KIND_OF[source]) in caplog.messages
    assert "Sentinel" not in caplog.text


@pytest.mark.parametrize("source", SOURCES)
def test_the_hourly_sweep_passes_over_a_draft_it_cant_work_out_and_sweeps_the_rest(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    source: str,
) -> None:
    """Through the schedule itself: nothing reaches its failure log, and no sentinel any log."""
    settings = fixture_settings(**file_settings(tmp_path))
    wired(monkeypatch, source)

    async def go() -> dict[str, str]:
        async with open_checkpointer(settings.checkpoint_path) as checkpointer:
            state = build_application_state(settings, checkpointer, real_clock=real_clock())
            try:
                student = state.project_state.student_id()
                planted = plant_three(settings.database_path, student, source)
                with caplog.at_level(logging.DEBUG):
                    task = asyncio.create_task(repeat(0.01, lambda: sweep_aged(state)))
                    await asyncio.sleep(0.2)
                    task.cancel()
                    await asyncio.wait([task])
                return planted
            finally:
                state.close()

    drafts = asyncio.run(go())

    assert_swept_around(settings.database_path, drafts)
    assert PASSED_OVER.format(KIND_OF[source]) in caplog.messages
    assert not [record for record in caplog.records if record.exc_info]
    assert "a scheduled sweep failed" not in caplog.text
    assert "Sentinel" not in caplog.text


def test_a_start_still_refuses_when_the_expiry_deletion_itself_fails(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing over a draft that can't be worked out never passes over a failed deletion."""
    settings = fixture_settings(**file_settings(tmp_path))
    first = build_application_state(settings, InMemorySaver(), real_clock=real_clock())
    student = first.project_state.student_id()
    first.close()
    plant_three(settings.database_path, student, "acceptance")
    failing_delete = (*import_drafts.DELETE_DRAFT, "DELETE FROM no_such_table WHERE ? IS NULL")
    monkeypatch.setattr(import_drafts, "DELETE_DRAFT", failing_delete)

    with pytest.raises(sqlite3.OperationalError):
        build_application_state(settings, InMemorySaver(), real_clock=real_clock())


def test_a_start_still_refuses_when_the_file_fails_while_a_draft_is_worked_out(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a rows function's or an acceptance record's failure is passed over."""
    settings = fixture_settings(**file_settings(tmp_path))
    first = build_application_state(settings, InMemorySaver(), real_clock=real_clock())
    student = first.project_state.student_id()
    first.close()
    plant_read_draft(
        settings.database_path, student, REAL_NOW - timedelta(hours=2), homework_answer()
    )

    def failing_file(*args: object, **kwargs: object) -> object:
        message = "synthetic"
        raise sqlite3.DatabaseError(message)

    monkeypatch.setattr(ImportDraftsStore, "_worked_out", failing_file)

    with pytest.raises(sqlite3.DatabaseError):
        build_application_state(settings, InMemorySaver(), real_clock=real_clock())


@pytest.mark.parametrize("column", ["acceptance", "reading"])
def test_a_start_passes_over_a_draft_whose_text_isnt_utf_8(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture, column: str
) -> None:
    """Bytes that aren't UTF-8 in an acceptance record or a stored reading are a decoding
    refusal, passed over by its kind like any other; the expired draft after it still goes."""
    settings = fixture_settings(**file_settings(tmp_path))
    first = build_application_state(settings, InMemorySaver(), real_clock=real_clock())
    student = first.project_state.student_id()
    first.close()
    path = settings.database_path
    failing = plant_read_draft(path, student, REAL_NOW - timedelta(hours=2), homework_answer())
    expired = plant_draft(path, student, REAL_NOW - timedelta(minutes=30))
    if column == "acceptance":
        plant_acceptance(path, student, failing, "[]")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE import_drafts SET expires_at = ? WHERE draft_id = ?",
            (instant(REAL_NOW - timedelta(minutes=1)), expired),
        )
        if column == "acceptance":
            connection.execute(
                "UPDATE grade_acceptances SET accepted = CAST(X'5B5B22FF22' AS TEXT) "
                "WHERE source_key = ?",
                (failing,),
            )
        else:
            connection.execute(
                "UPDATE import_draft_images SET reading = CAST(X'7BFF7D' AS TEXT) "
                "WHERE draft_id = ?",
                (failing,),
            )
    connection.close()

    with caplog.at_level(logging.DEBUG):
        state = build_application_state(settings, InMemorySaver(), real_clock=real_clock())
    state.close()

    found = drafts_in_file(path)
    assert expired not in found
    assert found[failing] == (None, 1)
    assert PASSED_OVER.format("TextNotUtf8") in caplog.messages
    assert not [record for record in caplog.records if record.exc_info]
