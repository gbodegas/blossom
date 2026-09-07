"""Her page frames the school week, Monday to Sunday, and moves between weeks.

The planner keeps its own horizon, the seven days from the evening it plans,
and the page says through which day that reaches. Work assigned in the week
and due after it is listed under the week's cards, the way the school's own
page frames a week. The evening's minute budgets are the household's settings
and the page shows the numbers it was given.
"""

import pathlib
from datetime import date

from fastapi.testclient import TestClient

from blossom.agent.prompts import PLANNER_SYSTEM
from blossom.app import create_app
from blossom.noticing import monday_of
from tests.support import fixture_settings

PINNED_TODAY = "2026-08-19"
MONDAY = "2026-08-17"


def page(week: str | None = None, **environment: str) -> tuple[int, str]:
    settings = fixture_settings(BLOSSOM_TODAY=PINNED_TODAY, **environment)
    params = {} if week is None else {"week": week}
    with TestClient(create_app(settings)) as client:
        response = client.get("/student/due-this-week", params=params)
    return response.status_code, response.text


def test_the_week_is_the_school_week_that_holds_today() -> None:
    status, shown = page()

    assert status == 200
    assert "<h1>Due this week</h1>" in shown
    assert "Monday, August 17 to" in shown
    assert "Sunday, August 23, 2026" in shown
    assert 'href="/student/due-this-week?week=2026-08-10">Previous week</a>' in shown
    assert 'href="/student/due-this-week?week=2026-08-24">Next week</a>' in shown
    assert ">This week</a>" not in shown


def test_work_due_after_the_week_is_listed_as_assigned_not_shown_as_due() -> None:
    _, shown = page()
    cards, _, after = shown.partition("Assigned this week, due later")

    assert "Canal Era comparison essay" in cards
    assert "Science fair topic proposal" in cards
    assert "Quadratic modeling problem set" not in cards
    assert "Reading log, week one" not in cards
    assert "Quadratic modeling problem set (Algebra II), due Monday, August 24." in after
    assert "Reading log, week one (English), due Tuesday, August 25." in after


def test_the_next_week_is_a_page_of_its_own() -> None:
    status, shown = page(week="2026-08-24")

    assert status == 200
    assert "<h1>Week of August 24</h1>" in shown
    assert "Monday, August 24 to" in shown
    assert "Sunday, August 30, 2026" in shown
    assert 'href="/student/due-this-week">This week</a>' in shown
    assert "Quadratic modeling problem set" in shown
    assert "Reading log, week one" in shown
    assert "Syllabus, signed" in shown, "undated work is in every week"
    assert "Canal Era comparison essay" not in shown
    assert "Assigned this week, due later" not in shown


def test_any_day_names_its_whole_week() -> None:
    _, from_monday = page(week=MONDAY)
    _, from_thursday = page(week="2026-08-20")
    _, from_sunday = page(week="2026-08-23")

    for shown in (from_monday, from_thursday, from_sunday):
        assert "<h1>Due this week</h1>" in shown
        assert "Monday, August 17 to" in shown


def test_a_week_that_is_not_a_date_is_said_on_todays_week() -> None:
    status, shown = page(week="soon")

    assert status == 422
    assert "That is not a date, so this is the week that holds today." in shown
    assert "<h1>Due this week</h1>" in shown


def test_an_empty_week_says_so_in_its_own_tense(tmp_path: pathlib.Path) -> None:
    (tmp_path / "assignments.json").write_text("[]", encoding="utf-8")
    (tmp_path / "deadline_sources.json").write_text("[]", encoding="utf-8")
    fixtures = {"BLOSSOM_FIXTURE_PATH": str(tmp_path)}

    _, this_week = page(**fixtures)
    _, that_week = page(week="2026-08-24", **fixtures)

    assert "Nothing is due this week." in this_week
    assert "Nothing is due that week." in that_week


def test_the_page_says_how_far_a_plan_looks() -> None:
    _, shown = page()

    assert "looks at everything due through" in shown
    assert "Tuesday, August 25" in shown


def test_the_page_shows_the_households_budgets() -> None:
    settings = fixture_settings(
        BLOSSOM_TODAY=PINNED_TODAY, BLOSSOM_EVENING_MINUTES="120", BLOSSOM_TOO_MUCH_MINUTES="40"
    )
    with TestClient(create_app(settings)) as client:
        client.post("/student/actions/too-much", follow_redirects=False)
        shown = client.get("/student/due-this-week").text

    assert "held to 40 minutes instead of 120" in shown


def test_the_planner_arranges_the_evening_and_does_not_teach() -> None:
    """A rationale may say how to begin. It never says how hard the work is or
    what the academic steps are; the school's entry does not, and the plan does
    not invent it."""
    assert "small steps" not in PLANNER_SYSTEM
    assert "It never\n  says how hard the work is" in PLANNER_SYSTEM


def test_the_week_starts_on_monday_whatever_the_day() -> None:
    assert monday_of(date(2026, 8, 17)) == date(2026, 8, 17)
    assert monday_of(date(2026, 8, 19)) == date(2026, 8, 17)
    assert monday_of(date(2026, 8, 23)) == date(2026, 8, 17)
    assert monday_of(date(2026, 8, 24)) == date(2026, 8, 24)
