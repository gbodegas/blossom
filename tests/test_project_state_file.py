"""Assignments and the claims about their dates live in the household's file.

They outlive a restart; a fixture seeds only an empty file and leaves a kept
record alone; a household that names no fixture starts with nothing on record
and nothing synthetic; and a claim is read back exactly as it was made.
"""

import pathlib
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from blossom.app import create_app
from blossom.dependencies import build_application_state
from blossom.reconciliation import SourceChannel, SourceRecord
from blossom.settings import Settings
from blossom.stores.paths import UnsafeCheckpointPath
from blossom.stores.project_state import Assignment, ProjectStateStore
from tests.support import fixture_clock, fixture_settings

PAGE = {"Accept": "text/html"}


def settings_in(tmp_path: pathlib.Path, **environ: str) -> Settings:
    """Settings with every state file under ``tmp_path``; ``environ`` wins."""
    return fixture_settings(
        **{
            "BLOSSOM_TODAY": "2026-08-19",
            "BLOSSOM_DATABASE_PATH": str(tmp_path / "blossom.sqlite3"),
            "BLOSSOM_CHECKPOINT_PATH": str(tmp_path / "checkpoints.sqlite3"),
            "BLOSSOM_TRACE_PATH": str(tmp_path / "traces.sqlite3"),
            **environ,
        }
    )


def test_assignments_and_their_claims_outlive_a_restart(tmp_path: pathlib.Path) -> None:
    seeded = build_application_state(settings_in(tmp_path), InMemorySaver())
    try:
        before = seeded.project_state.all_assignments()
        claims_before = {
            item.assignment_id: seeded.project_state.deadline_records(item.assignment_id)
            for item in before
        }
    finally:
        seeded.close()
    again = build_application_state(settings_in(tmp_path, BLOSSOM_FIXTURE_PATH=""), InMemorySaver())
    try:
        after = again.project_state.all_assignments()
        claims_after = {
            item.assignment_id: again.project_state.deadline_records(item.assignment_id)
            for item in after
        }
    finally:
        again.close()

    assert before
    assert after == before
    assert any(claims_before.values())
    assert claims_after == claims_before


def test_the_fixture_seeds_an_empty_file_and_leaves_a_kept_record_alone(
    tmp_path: pathlib.Path,
) -> None:
    """An assignment entered by hand is still there after the next start, beside the
    fixture's, rather than written over by the fixture."""
    entered = Assignment(
        assignment_id="assignment-entered-by-hand",
        course="Spanish",
        title="Vocabulary list, unit two",
        due_date=date(2026, 8, 27),
        dependencies=[],
        reported_submission_status="not_started",
    )
    first = build_application_state(settings_in(tmp_path), InMemorySaver())
    try:
        first.project_state.upsert_assignments([entered])
    finally:
        first.close()
    second = build_application_state(settings_in(tmp_path), InMemorySaver())
    try:
        on_record = {item.assignment_id: item for item in second.project_state.all_assignments()}
    finally:
        second.close()

    assert on_record["assignment-entered-by-hand"] == entered
    assert "assignment-canal-essay" in on_record


def test_a_household_with_no_fixture_starts_with_nothing_on_record(
    tmp_path: pathlib.Path,
) -> None:
    settings = settings_in(tmp_path, BLOSSOM_FIXTURE_PATH="")
    assert settings.fixture_path is None
    with TestClient(create_app(settings)) as client:
        page = client.get("/student/due-this-week", headers=PAGE)
    state = build_application_state(settings, InMemorySaver())
    try:
        assignments = state.project_state.all_assignments()
        rules = state.support_rules.list_all()
        notes = state.reflections.list_all()
    finally:
        state.close()

    assert page.status_code == 200
    assert assignments == []
    assert rules == []
    assert notes == []


def test_a_claim_is_read_back_as_made_and_the_file_refuses_a_share(
    tmp_path: pathlib.Path,
) -> None:
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        assert store.is_empty()
        when = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
        claims = [
            SourceRecord(
                channel=SourceChannel.LMS,
                asserted_value="2026-08-21",
                observed_at=when,
                confidence=0.9,
                seen_in="the day's header",
            ),
            SourceRecord(
                channel=SourceChannel.PARENT_ENTRY,
                asserted_value="2026-08-22",
                observed_at=when + timedelta(hours=1),
                confidence=0.7,
            ),
        ]
        store.record_claims("assignment-essay", claims)
        read_back = store.deadline_records("assignment-essay")
        unknown = store.deadline_records("assignment-unknown")
        empty = store.is_empty()
    finally:
        store.close()

    assert read_back == claims
    assert unknown == []
    assert empty is False
    with pytest.raises(UnsafeCheckpointPath):
        ProjectStateStore.open(pathlib.Path(r"\\server\share\blossom.sqlite3"), fixture_clock())
