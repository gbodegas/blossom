"""The fixture set: a fictional student's week as a school portal would show it.

These tests hold the fixtures to the shapes the model was given: an undated
task, assigned dates beside due dates, a source that gives one item two dates,
and every confidence state on one page. They also hold the two corpora to what
the planner reads from them.
"""

import json
import pathlib

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from blossom.app import create_app
from blossom.dependencies import build_application_state
from blossom.reconciliation import Reconciler, SourceConfidence, classify_confidence
from blossom.settings import REPOSITORY_ROOT
from blossom.sources import FixtureSource
from blossom.stores.project_state import AssignmentKind
from blossom.stores.reflections import ReflectionSubject
from tests.support import fixture_settings

FIXTURES = REPOSITORY_ROOT / "data" / "synthetic"


def test_the_fixture_set_carries_every_shape_the_portal_shows() -> None:
    assignments = FixtureSource(FIXTURES).assignments()
    by_id = {item.assignment_id: item for item in assignments}

    assert len(assignments) == 6
    assert len(by_id) == 6
    assert all(item.assigned_on is not None for item in assignments)
    undated = [item for item in assignments if item.due_date is None]
    assert [item.assignment_id for item in undated] == ["assignment-signed-syllabus"]
    assert undated[0].kind is AssignmentKind.TASK
    assert by_id["assignment-textbook-cover"].kind is AssignmentKind.TASK
    assert by_id["assignment-textbook-cover"].due_date is not None
    assert sum(item.kind is AssignmentKind.HOMEWORK for item in assignments) == 4


def test_the_sources_produce_every_confidence_state() -> None:
    source = FixtureSource(FIXTURES)
    reconciler = Reconciler()

    labels = {
        item.assignment_id: classify_confidence(
            reconciler.reconcile(source.deadline_records(item.assignment_id))
        )
        for item in source.assignments()
    }

    assert labels["assignment-algebra-set"] is SourceConfidence.CORROBORATED
    assert labels["assignment-reading-log"] is SourceConfidence.SINGLE_SOURCE
    assert labels["assignment-canal-essay"] is SourceConfidence.SOURCES_DISAGREE
    assert labels["assignment-textbook-cover"] is SourceConfidence.SOURCES_DISAGREE
    assert labels["assignment-science-fair-proposal"] is SourceConfidence.UNVERIFIED
    assert labels["assignment-signed-syllabus"] is SourceConfidence.UNVERIFIED
    assert set(labels.values()) == set(SourceConfidence)


def test_one_source_gives_the_textbook_cover_two_dates_in_two_places() -> None:
    records = FixtureSource(FIXTURES).deadline_records("assignment-textbook-cover")

    assert [record.describe() for record in records] == [
        "LMS (day header): 2026-08-21",
        "LMS (title): 2026-08-22",
    ]


def test_the_rules_are_single_instructions_and_the_note_is_about_the_system() -> None:
    source = FixtureSource(FIXTURES)

    rules = source.support_rules()
    notes = source.reflections()

    assert len(rules) == 4
    assert all("\n\n" not in rule.instruction for rule in rules)
    assert all(rule.asserted_at.utcoffset() is not None for rule in rules)
    assert len(notes) == 1
    assert notes[0].subject is ReflectionSubject.SYSTEM
    assert notes[0].observed_at.utcoffset() is not None


def test_the_application_seeds_both_stores_from_the_fixtures() -> None:
    state = build_application_state(fixture_settings(), InMemorySaver())
    try:
        rules = state.support_rules.list_all()
        notes = state.reflections.list_all()
    finally:
        state.close()

    assert [rule.rule_id for rule in rules] == [
        "rule-stages",
        "rule-first-ten-minutes",
        "rule-stop-time",
        "rule-read-aloud",
    ]
    assert [note.reflection_id for note in notes] == ["reflection-first-ten-minutes"]


def test_a_fixture_set_without_the_corpora_has_none(tmp_path: pathlib.Path) -> None:
    """A set written to exercise the page alone need not carry rules or notes."""
    (tmp_path / "assignments.json").write_text("[]", encoding="utf-8")
    (tmp_path / "deadline_sources.json").write_text("[]", encoding="utf-8")

    source = FixtureSource(tmp_path)

    assert source.support_rules() == []
    assert source.reflections() == []


def test_the_page_shows_the_whole_week_with_every_state_named() -> None:
    settings = fixture_settings(BLOSSOM_TODAY="2026-08-19")

    with TestClient(create_app(settings)) as client:
        page = client.get("/student/due-this-week").text

    for title in json.loads((FIXTURES / "assignments.json").read_text(encoding="utf-8")):
        assert title["title"] in page
    assert "Confirmed by 2 sources" in page
    assert "One source only" in page
    assert "Sources disagree" in page
    assert "Unverified due date" in page
    assert "LMS (day header): 2026-08-21" in page
    assert "LMS (title): 2026-08-22" in page
    assert "No due date on record" in page
    assert "a task, not a sitting" in page
