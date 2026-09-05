"""The clock, the household's zone, and the "due this week" window they drive.

An instant is UTC and a date is local, and the difference is not academic: for
most of an American evening the UTC date is already tomorrow. These tests pin
the clock to known moments, including one late evening and both daylight-saving
nights inside the school year, and assert the window and the rendered page
follow the household rather than UTC.
"""

import sqlite3
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from blossom.app import create_app
from blossom.clock import (
    FrozenClock,
    SystemClock,
    TimeZoneUnavailable,
    clock_from,
    household_zone,
)
from blossom.drafts import Draft
from blossom.retrieval import RetrievalResult
from blossom.settings import TIMEZONE_VARIABLE, TODAY_VARIABLE, Settings
from blossom.stores.project_state import (
    DUE_THIS_WEEK_KEY,
    Assignment,
    ProjectStateStore,
)
from blossom.stores.reflections import Reflection, ReflectionSubject
from blossom.stores.support_rules import SupportRule
from tests.support import FIXTURE_TIMEZONE, NAIVE_INSTANTS, fixture_settings

ZONE = ZoneInfo(FIXTURE_TIMEZONE)

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


def local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """A wall-clock moment in the household's zone."""
    return datetime(year, month, day, hour, minute, tzinfo=ZONE)


def store_pinned_to(instant: datetime) -> ProjectStateStore:
    """A store holding the fixture assignments, its clock pinned to ``instant``."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    store = ProjectStateStore(connection, clock=FrozenClock(instant, ZONE))
    store.upsert_assignments(FIXTURE_ASSIGNMENTS)
    return store


# ------------------------------------------------------------------ the zone


def test_a_missing_zone_is_refused_by_name() -> None:
    """There is no default, and the message says which variable to set."""
    for missing in (None, "", "   "):
        with pytest.raises(TimeZoneUnavailable, match=TIMEZONE_VARIABLE):
            household_zone(missing)


def test_an_unresolvable_zone_names_the_key_and_the_database() -> None:
    with pytest.raises(TimeZoneUnavailable, match="tzdata"):
        household_zone("Mars/Olympus_Mons")


def test_a_real_key_resolves() -> None:
    assert household_zone(" America/New_York ") == ZONE


# ----------------------------------------------------------------- the clock


def test_system_clock_reports_timezone_aware_utc() -> None:
    now = SystemClock(ZONE).now()

    assert now.tzinfo is not None
    assert now.utcoffset() == UTC.utcoffset(None)


def test_today_is_the_household_date_not_the_utc_one() -> None:
    """At half past eight on a September evening the UTC date is already tomorrow."""
    evening = local(2026, 9, 2, 20, 30)
    clock = FrozenClock(evening, ZONE)

    assert clock.now().date() == date(2026, 9, 3)
    assert clock.today() == date(2026, 9, 2)


@pytest.mark.parametrize(
    ("day", "offset_hours"),
    [(date(2026, 11, 1), 4), (date(2027, 3, 14), 5)],
    ids=["clocks-go-back", "clocks-go-forward"],
)
def test_the_daylight_saving_nights_still_read_as_their_own_day(
    day: date, offset_hours: int
) -> None:
    """Both transitions fall inside the school year. Local midnight is still
    that day, and it sits at a different UTC hour on either side."""
    clock = clock_from(day, FIXTURE_TIMEZONE)

    assert clock.today() == day
    assert clock.now() == datetime(day.year, day.month, day.day, offset_hours, tzinfo=UTC)


@pytest.mark.parametrize("naive", NAIVE_INSTANTS, ids=["no-zone", "zone-without-an-offset"])
def test_every_hand_written_guard_uses_pythons_rule_for_aware(naive: datetime) -> None:
    """Aware means a zone that answers with an offset, not merely a zone.

    A guard testing ``tzinfo is not None`` admits the second shape, and
    ``astimezone`` then reads it as the running machine's local time, so the
    same value means a different moment on every machine. The pydantic-typed
    fields already refuse both; these are the guards written by hand.
    """
    with pytest.raises(ValueError, match="aware"):
        FrozenClock(naive, ZONE)
    with pytest.raises(ValueError, match="aware"):
        Reflection(
            reflection_id="r1",
            subject=ReflectionSubject.SYSTEM,
            observation="the plan was rebuilt twice",
            observed_at=naive,
        )
    with pytest.raises(ValueError, match="aware"):
        SupportRule(rule_id="s1", instruction="Break long tasks up.", asserted_at=naive)


@pytest.mark.parametrize("naive", NAIVE_INSTANTS, ids=["no-zone", "zone-without-an-offset"])
def test_a_pydantic_instant_refuses_both_shapes_too(naive: datetime) -> None:
    with pytest.raises(ValidationError):
        Draft(body="Could she have until Friday?", created_at=naive)


def test_clock_from_returns_system_clock_when_no_date_is_pinned() -> None:
    assert isinstance(clock_from(None, FIXTURE_TIMEZONE), SystemClock)


def test_clock_from_pins_to_local_midnight() -> None:
    clock = clock_from(date(2026, 8, 19), FIXTURE_TIMEZONE)

    assert clock.today() == date(2026, 8, 19)
    assert clock.now() == local(2026, 8, 19).astimezone(UTC)


def test_clock_from_refuses_to_build_without_a_zone() -> None:
    with pytest.raises(TimeZoneUnavailable, match=TIMEZONE_VARIABLE):
        clock_from(date(2026, 8, 19), None)


# ---------------------------------------------------------------- the window


def test_window_includes_assignments_due_within_the_next_seven_days() -> None:
    store = store_pinned_to(local(2026, 8, 19))
    try:
        result = store.lookup(DUE_THIS_WEEK_KEY)
    finally:
        store.close()

    assert isinstance(result, RetrievalResult)
    titles = [item["title"] for item in result.payload["assignments"]]
    assert titles == ["Canal Era comparison essay", "Quadratic modeling problem set"]


def test_a_late_evening_does_not_move_the_window_to_tomorrow() -> None:
    """The same week, seen at 21:00, when the UTC date has already rolled over."""
    store = store_pinned_to(local(2026, 8, 19, 21, 0))
    try:
        result = store.lookup(DUE_THIS_WEEK_KEY)
    finally:
        store.close()

    assert isinstance(result, RetrievalResult)
    titles = [item["title"] for item in result.payload["assignments"]]
    assert titles == ["Canal Era comparison essay", "Quadratic modeling problem set"]


def test_window_moves_with_the_clock() -> None:
    store = store_pinned_to(local(2026, 8, 22))
    try:
        result = store.lookup(DUE_THIS_WEEK_KEY)
    finally:
        store.close()

    assert isinstance(result, RetrievalResult)
    titles = [item["title"] for item in result.payload["assignments"]]
    assert titles == ["Quadratic modeling problem set"]


def test_window_is_empty_once_the_fixture_week_has_passed() -> None:
    store = store_pinned_to(local(2026, 9, 30))
    try:
        result = store.lookup(DUE_THIS_WEEK_KEY)
    finally:
        store.close()

    assert isinstance(result, RetrievalResult)
    assert result.payload["assignments"] == []


def test_asserted_at_comes_from_the_clock_not_the_wall_time() -> None:
    store = store_pinned_to(local(2026, 8, 19))
    try:
        result = store.lookup(DUE_THIS_WEEK_KEY)
    finally:
        store.close()

    assert isinstance(result, RetrievalResult)
    assert result.asserted_at == local(2026, 8, 19).astimezone(UTC)


def test_unknown_lookup_keys_still_return_nothing() -> None:
    store = store_pinned_to(local(2026, 8, 19))
    try:
        assert store.lookup("assignment:canal-essay") is None
    finally:
        store.close()


# --------------------------------------------------------------- the settings


def test_settings_parse_a_pinned_date() -> None:
    settings = Settings.from_environment({TODAY_VARIABLE: "2026-08-19"})

    assert settings.today == date(2026, 8, 19)


def test_settings_default_to_an_unpinned_clock() -> None:
    assert Settings.from_environment({}).today is None


def test_settings_hold_the_zone_key_and_leave_it_unset_when_absent() -> None:
    """Settings carry the key, not a resolved zone: reading the system's time
    zone database is work the clock does, at startup, where it can say so."""
    assert Settings.from_environment({TIMEZONE_VARIABLE: " America/New_York "}).timezone_key == (
        "America/New_York"
    )
    assert Settings.from_environment({TIMEZONE_VARIABLE: "  "}).timezone_key is None
    assert Settings.from_environment({}).timezone_key is None


def test_settings_reject_an_unparseable_pinned_date() -> None:
    with pytest.raises(ValueError, match=TODAY_VARIABLE):
        Settings.from_environment({TODAY_VARIABLE: "next tuesday"})


# ------------------------------------------------------------ through the app


def test_pinning_the_clock_changes_what_the_student_page_shows() -> None:
    """End to end: the environment variable reaches the rendered page."""
    late = fixture_settings(**{TODAY_VARIABLE: "2026-09-30"})

    with TestClient(create_app(late)) as client:
        response = client.get("/student/due-this-week")

    assert response.status_code == 200
    assert "Canal Era comparison essay" not in response.text


def test_the_application_refuses_to_start_without_a_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong zone moves the school week silently, so an absent one stops startup."""
    monkeypatch.delenv(TIMEZONE_VARIABLE, raising=False)
    settings = Settings.from_environment({TODAY_VARIABLE: "2026-08-19"})

    with (
        pytest.raises(TimeZoneUnavailable, match=TIMEZONE_VARIABLE),
        TestClient(create_app(settings)),
    ):
        pass
