"""Her page frames the school week, Monday to Sunday, and moves between weeks.

The planner keeps its own horizon, the seven days from the evening it plans,
and the page says through which day that reaches. Work assigned in the week
and due after it is listed under the week's cards, the way the school's own
page frames a week. The evening's minute budgets are the household's settings
and the page shows the numbers it was given.
"""

import json
import pathlib
from datetime import date

import pytest
from fastapi.testclient import TestClient

from blossom.agent.prompts import PLANNER_SYSTEM
from blossom.app import create_app
from blossom.noticing import monday_of
from blossom.settings import STATIC_PATH as STATIC
from blossom.settings import TEMPLATE_PATH as TEMPLATES
from tests.support import fixture_settings

PINNED_TODAY = "2026-08-19"
MONDAY = "2026-08-17"
FIXTURE_ROW: dict[str, object] = {
    "assignment_id": "row",
    "course": "Science",
    "title": "A row",
    "due_date": None,
    "dependencies": [],
    "reported_submission_status": "not_started",
    "assigned_on": "2026-08-18",
    "kind": "HOMEWORK",
}


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
    assert "Quadratic modeling problem set (Algebra II), due Monday, August 24, 2026." in after
    assert "Reading log, week one (English), due Tuesday, August 25, 2026." in after


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


def test_a_week_across_the_new_year_gives_both_years() -> None:
    _, shown = page(week="2026-12-30")
    line = " ".join(shown.split())

    assert "<h1>Week of December 28</h1>" in shown
    assert "Monday, December 28, 2026 to Sunday, January 3, 2027" in line


def test_any_day_names_its_whole_week() -> None:
    _, from_monday = page(week=MONDAY)
    _, from_thursday = page(week="2026-08-20")
    _, from_sunday = page(week="2026-08-23")

    for shown in (from_monday, from_thursday, from_sunday):
        assert "<h1>Due this week</h1>" in shown
        assert "Monday, August 17 to" in shown


@pytest.mark.parametrize("given", ["soon", "", "   ", "2026-08"])
def test_a_week_that_is_not_a_date_is_said_on_todays_week(given: str) -> None:
    """Only an absent ``week`` means today's week without a word; a blank one is not a date."""
    status, shown = page(week=given)

    assert status == 422
    assert "That is not a date, so this is the week that holds today." in shown
    assert "<h1>Due this week</h1>" in shown


@pytest.mark.parametrize("given", ["0001-01-01", "0001-01-05", "9999-12-31", "9999-12-27"])
def test_the_edges_of_the_calendar_are_said_not_crashed_into(given: str) -> None:
    """The first and last weeks the date type holds have no week on one side to link to."""
    status, shown = page(week=given)

    assert status == 422
    assert "That week is past the edge of the calendar" in shown
    assert "<h1>Due this week</h1>" in shown


@pytest.mark.parametrize(
    ("given", "heading"),
    [("0001-01-08", "Week of January 8"), ("9999-12-20", "Week of December 20")],
)
def test_the_weeks_just_inside_the_edges_are_shown(given: str, heading: str) -> None:
    status, shown = page(week=given)

    assert status == 200
    assert f"<h1>{heading}</h1>" in shown


def test_work_assigned_this_week_is_due_later_only_when_it_is(tmp_path: pathlib.Path) -> None:
    """Work given out this week and due before it is not "due later"; it belongs to the
    week it was due in."""
    later = dict(
        FIXTURE_ROW, assignment_id="later", title="Due after the week", due_date="2026-08-27"
    )
    before = dict(
        FIXTURE_ROW, assignment_id="before", title="Due before the week", due_date="2026-08-14"
    )
    (tmp_path / "assignments.json").write_text(json.dumps([later, before]), encoding="utf-8")
    (tmp_path / "deadline_sources.json").write_text("[]", encoding="utf-8")

    _, shown = page(BLOSSOM_FIXTURE_PATH=str(tmp_path))
    _, week_before = page(week="2026-08-10", BLOSSOM_FIXTURE_PATH=str(tmp_path))

    assert "Due after the week (Science), due Thursday, August 27, 2026." in shown
    assert "Due before the week" not in shown
    assert "Due before the week" in week_before


def test_work_due_in_another_year_says_which(tmp_path: pathlib.Path) -> None:
    """The list can hold work due in any later year, so the year is part of the date."""
    row = dict(FIXTURE_ROW, assignment_id="next-year", title="Long project", due_date="2027-08-27")
    (tmp_path / "assignments.json").write_text(json.dumps([row]), encoding="utf-8")
    (tmp_path / "deadline_sources.json").write_text("[]", encoding="utf-8")

    _, shown = page(BLOSSOM_FIXTURE_PATH=str(tmp_path))

    assert "Long project (Science), due Friday, August 27, 2027." in shown


def test_the_parents_accepted_review_keeps_its_sage_state() -> None:
    """Her page stopped emitting the class; the parent's page still says a reviewer
    accepted a plan with it, so the style stays."""
    parent = (TEMPLATES / "parent_review.html").read_text(encoding="utf-8")
    styles = (STATIC / "blossom.css").read_text(encoding="utf-8")

    assert 'class="confidence corroborated"' in parent
    assert ".confidence.corroborated {" in styles


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
