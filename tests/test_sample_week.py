"""The sample week: a synthetic scenario for showing Blossom, kept apart from the family's state.

Four ordinary assignments in the school week of September 7, 2026, each with one
date from the school portal and nothing disputing it, so the opening view is a
plain week rather than a page of exceptions. The launch file points every state
file under its own folder, and a flag marks both pages "Sample week", because a
pinned clock on its own says nothing about whose data is shown.
"""

import json
import pathlib
from datetime import date

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from blossom.app import create_app
from blossom.dependencies import build_application_state
from blossom.noticing import Verdict, expect_due_date, notice_due_date, reconcile_dates
from blossom.reconciliation import SourceChannel, SourceConfidence, classify_confidence
from blossom.routes.student import build_student_due_this_week_view
from blossom.settings import REPOSITORY_ROOT, SAMPLE_VARIABLE, Settings
from blossom.sources import FixtureSource
from tests.support import fixture_settings

SAMPLE = REPOSITORY_ROOT / "data" / "sample"
SAMPLE_DAY = "2026-09-07"


def launch_file() -> dict[str, str]:
    """The variables the sample launch file sets, comments and blanks left out."""
    lines = (SAMPLE / "sample.env").read_text(encoding="utf-8").splitlines()
    pairs = (line.split("=", 1) for line in lines if line and not line.startswith("#"))
    return dict(pairs)


def sample_settings(**environment: str) -> Settings:
    return fixture_settings(
        BLOSSOM_FIXTURE_PATH=str(SAMPLE), BLOSSOM_TODAY=SAMPLE_DAY, **environment
    )


def test_the_sample_holds_four_ordinary_items_each_with_one_portal_date() -> None:
    source = FixtureSource(SAMPLE)
    assignments = source.assignments()

    assert [item.assignment_id for item in assignments] == [
        "sample-geometry-exercises",
        "sample-syllabus-signed",
        "sample-binder-check",
        "sample-reading-log",
    ]
    assert [item.due_date for item in assignments] == [
        date(2026, 9, 8),
        date(2026, 9, 9),
        date(2026, 9, 11),
        date(2026, 9, 14),
    ]
    for item in assignments:
        records = source.deadline_records(item.assignment_id)
        noticed = notice_due_date(expect_due_date(item), records)
        assert [record.channel for record in records] == [SourceChannel.LMS]
        assert noticed.verdict is Verdict.CONFIRMED
        assert classify_confidence(reconcile_dates(records)) is SourceConfidence.SINGLE_SOURCE
    assert source.reflections() == []


def test_the_sample_page_is_a_plain_week_with_the_reading_log_assigned_for_later() -> None:
    with TestClient(create_app(sample_settings())) as client:
        this_week = client.get("/student/due-this-week").text
        next_week = client.get("/student/due-this-week", params={"week": "2026-09-14"}).text

    cards, _, later = this_week.partition("Assigned this week, due later")
    assert "<h1>My week</h1>" in this_week
    assert "September 7 to" in this_week
    for title in ("HW U1.1 Pg 11 #16, 17, 22, 23", "Syllabus, signed", "Binder and dividers check"):
        assert title in cards
    assert "Reading log" not in cards
    assert "<strong>Reading log</strong> &middot; Humanities" in later
    assert "Due Monday, September 14" in later
    assert this_week.count('<span class="source">School portal</span>') == 3
    assert 'class="confidence disagree"' not in this_week
    assert "Reading log" in next_week
    assert "Assigned this week, due later" not in next_week


def test_the_reading_log_keeps_its_id_across_the_two_weeks() -> None:
    state = build_application_state(sample_settings(), InMemorySaver())
    try:
        this_week = build_student_due_this_week_view(state)
        next_week = build_student_due_this_week_view(state, date(2026, 9, 14))
    finally:
        state.close()

    assert [item.assignment_id for item in this_week.assigned_this_week] == ["sample-reading-log"]
    assert [item.assignment_id for item in next_week.assignments] == ["sample-reading-log"]
    assert this_week.assigned_this_week[0].submission_status == "not_started"
    assert next_week.assignments[0].submission_status == "not_started"


def test_the_week_before_the_sample_is_empty_and_says_so() -> None:
    with TestClient(create_app(sample_settings())) as client:
        before = client.get("/student/due-this-week", params={"week": "2026-08-31"}).text

    assert "No assignments are recorded as due that week." in before


def test_the_sample_label_shows_on_both_pages_only_when_the_flag_is_set() -> None:
    with TestClient(create_app(sample_settings(BLOSSOM_SAMPLE="1"))) as client:
        labeled = (client.get("/student/due-this-week").text, client.get("/parent").text)
    with TestClient(create_app(sample_settings())) as client:
        plain = (client.get("/student/due-this-week").text, client.get("/parent").text)

    for page in labeled:
        assert '<p class="sample-label">Sample week</p>' in page
    for page in plain:
        assert "Sample week" not in page


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("1", True),
        ("true", True),
        ("Yes", True),
        ("on", True),
        ("0", False),
        ("no", False),
        ("", False),
    ],
)
def test_the_sample_flag_reads_yes_and_no(given: str, expected: bool) -> None:
    assert fixture_settings(BLOSSOM_SAMPLE=given).sample is expected


def test_the_sample_flag_refuses_anything_else_by_name() -> None:
    with pytest.raises(ValueError, match=SAMPLE_VARIABLE):
        fixture_settings(BLOSSOM_SAMPLE="maybe")


def test_the_launch_file_isolates_the_state_and_sets_the_flag() -> None:
    """The household's time zone and the model key come from .env, not from here."""
    variables = launch_file()

    assert variables["BLOSSOM_FIXTURE_PATH"] == "data/sample"
    assert variables["BLOSSOM_TODAY"] == SAMPLE_DAY
    assert variables["BLOSSOM_SAMPLE"] == "1"
    for name in ("BLOSSOM_DATABASE_PATH", "BLOSSOM_CHECKPOINT_PATH", "BLOSSOM_TRACE_PATH"):
        assert variables[name].startswith(".local/sample/")
    assert "BLOSSOM_TIMEZONE" not in variables
    assert "ANTHROPIC_API_KEY" not in variables

    settings = fixture_settings(**variables)
    assert settings.fixture_path == SAMPLE
    assert settings.today == date(2026, 9, 7)
    assert settings.sample is True
    assert settings.database_path.parent == REPOSITORY_ROOT / ".local" / "sample"
    assert settings.checkpoint_path.parent == REPOSITORY_ROOT / ".local" / "sample"
    assert settings.trace_path.parent == REPOSITORY_ROOT / ".local" / "sample"


def test_the_sample_state_folder_is_created_on_first_launch(tmp_path: pathlib.Path) -> None:
    folder = tmp_path / "sample"
    settings = sample_settings(
        BLOSSOM_DATABASE_PATH=str(folder / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(folder / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(folder / "traces.sqlite3"),
    )

    with TestClient(create_app(settings)) as client:
        status = client.get("/student/due-this-week").status_code

    assert status == 200
    assert (folder / "blossom.sqlite3").is_file()


def test_the_sample_rules_ask_the_planner_to_invent_nothing() -> None:
    """No rule asks for stages, steps, estimates, or a finish; they reserve time and
    name what she already knows she needs."""
    rules = json.loads((SAMPLE / "support_rules.json").read_text(encoding="utf-8"))

    assert len(rules) == 3
    for rule in rules:
        text = rule["instruction"].lower()
        for word in ("stage", "step", "finish", "estimate", "ten minutes"):
            assert word not in text, f"{rule['rule_id']} says {word!r}"


def test_the_prepared_sample_plan_is_labeled_and_matches_the_sample() -> None:
    text = (SAMPLE / "prepared_plan.md").read_text(encoding="utf-8")

    assert text.startswith("# Prepared sample plan")
    assert "Plan for Monday, September 7, 2026" in text
    for title in (
        "HW U1.1 Pg 11 #16, 17, 22, 23",
        "Syllabus, signed",
        "Binder and dividers check",
        "Reading log",
    ):
        assert title in text
    assert "The reviewer's notes" not in text
    assert "on track" not in text.split("```")[1]
