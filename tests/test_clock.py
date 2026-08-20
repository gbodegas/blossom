"""Tests for the injectable clock and the window it drives.

The scaffold hardcoded ``date(2026, 8, 19)`` inside ``ProjectStateStore``, so
the "due this week" window had no relationship to the current date and the
route test could not fail no matter how much time passed. These tests pin the
clock explicitly and assert the window moves with it.
"""

import sqlite3
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.clock import FrozenClock, SystemClock, clock_from
from blossom.retrieval import RetrievalResult
from blossom.settings import TODAY_VARIABLE, Settings
from blossom.stores.project_state import (
    DUE_THIS_WEEK_KEY,
    Assignment,
    ProjectStateStore,
)

FIXTURE_ASSIGNMENTS = [
    Assignment(
        assignment_id="assignment-canal-essay",
        course="World History",
        title="Canal Era comparison essay",
        due_date=date(2026, 8, 21),
        dependencies=[],
        reported_submission_status="in_progress",
    ),
    Assignment(
        assignment_id="assignment-algebra-set",
        course="Algebra II",
        title="Quadratic modeling problem set",
        due_date=date(2026, 8, 24),
        dependencies=[],
        reported_submission_status="not_started",
    ),
]


def store_pinned_to(day: date) -> ProjectStateStore:
    """Build a store holding the fixture assignments with its clock pinned to ``day``."""
    clock = FrozenClock(datetime(day.year, day.month, day.day, tzinfo=UTC))
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    store = ProjectStateStore(connection, clock=clock)
    store.upsert_assignments(FIXTURE_ASSIGNMENTS)
    return store


def test_system_clock_reports_timezone_aware_utc() -> None:
    now = SystemClock().now()

    assert now.tzinfo is not None
    assert now.utcoffset() == UTC.utcoffset(None)


def test_clock_from_returns_system_clock_when_no_date_is_pinned() -> None:
    assert isinstance(clock_from(None), SystemClock)


def test_clock_from_pins_to_the_given_date() -> None:
    clock = clock_from(date(2026, 8, 19))

    assert clock.today() == date(2026, 8, 19)
    assert clock.now() == datetime(2026, 8, 19, tzinfo=UTC)


def test_window_includes_assignments_due_within_the_next_seven_days() -> None:
    store = store_pinned_to(date(2026, 8, 19))
    try:
        result = store.lookup(DUE_THIS_WEEK_KEY)
    finally:
        store.close()

    assert isinstance(result, RetrievalResult)
    titles = [item["title"] for item in result.payload["assignments"]]
    assert titles == ["Canal Era comparison essay", "Quadratic modeling problem set"]


def test_window_moves_with_the_clock() -> None:
    """The behaviour the hardcoded date made untestable."""
    store = store_pinned_to(date(2026, 8, 22))
    try:
        result = store.lookup(DUE_THIS_WEEK_KEY)
    finally:
        store.close()

    assert isinstance(result, RetrievalResult)
    titles = [item["title"] for item in result.payload["assignments"]]
    assert titles == ["Quadratic modeling problem set"]


def test_window_is_empty_once_the_fixture_week_has_passed() -> None:
    store = store_pinned_to(date(2026, 9, 30))
    try:
        result = store.lookup(DUE_THIS_WEEK_KEY)
    finally:
        store.close()

    assert isinstance(result, RetrievalResult)
    assert result.payload["assignments"] == []


def test_asserted_at_comes_from_the_clock_not_the_wall_time() -> None:
    store = store_pinned_to(date(2026, 8, 19))
    try:
        result = store.lookup(DUE_THIS_WEEK_KEY)
    finally:
        store.close()

    assert isinstance(result, RetrievalResult)
    assert result.asserted_at == datetime(2026, 8, 19, tzinfo=UTC)


def test_unknown_lookup_keys_still_return_nothing() -> None:
    store = store_pinned_to(date(2026, 8, 19))
    try:
        assert store.lookup("assignment:canal-essay") is None
    finally:
        store.close()


def test_settings_parse_a_pinned_date() -> None:
    settings = Settings.from_environment({TODAY_VARIABLE: "2026-08-19"})

    assert settings.today == date(2026, 8, 19)


def test_settings_default_to_an_unpinned_clock() -> None:
    assert Settings.from_environment({}).today is None


def test_settings_reject_an_unparseable_pinned_date() -> None:
    with pytest.raises(ValueError, match=TODAY_VARIABLE):
        Settings.from_environment({TODAY_VARIABLE: "next tuesday"})


def test_pinning_the_clock_changes_what_the_student_page_shows() -> None:
    """End to end: the environment variable reaches the rendered page."""
    late = Settings.from_environment({TODAY_VARIABLE: "2026-09-30"})

    with TestClient(create_app(late)) as client:
        response = client.get("/student/due-this-week")

    assert response.status_code == 200
    assert "Canal Era comparison essay" not in response.text
