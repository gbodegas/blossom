"""The shapes a school portal shows, and what the code does with each.

An item with no due date, an item with an assigned date as well as a due date,
a task that is minutes rather than a sitting, and a source that gives two
different dates in two places. None of these are in the fixtures yet; they are
built here so the model, the store, the checks, the prompts, the draft, and the
page are each shown to carry them.
"""

import json
import pathlib
import sqlite3
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from blossom.agent.compose import compose_draft
from blossom.agent.prompts import assignments_block, critic_brief
from blossom.app import create_app
from blossom.heuristic_relevance import CriticVerdict
from blossom.plan_checks import PlanCheck, check_plan
from blossom.plans import DailyPlan, Deferral, PlanBlock
from blossom.reconciliation import Reconciler, SourceChannel, SourceConfidence, SourceRecord
from blossom.stores.project_state import Assignment, AssignmentKind, ProjectStateStore
from blossom.views import StudentAssignmentView
from tests.support import FIXTURE_TIMEZONE, fixture_clock, fixture_settings

ZONE = ZoneInfo(FIXTURE_TIMEZONE)
PLAN_DATE = date(2026, 8, 19)
OBSERVED = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)

ESSAY = Assignment(
    assignment_id="assignment-canal-essay",
    course="World History",
    title="Canal Era comparison essay",
    due_date=date(2026, 8, 21),
    dependencies=[],
    reported_submission_status="in_progress",
    assigned_on=date(2026, 8, 14),
)
SYLLABUS = Assignment(
    assignment_id="assignment-signed-syllabus",
    course="Geometry",
    title="Syllabus, signed",
    due_date=None,
    dependencies=[],
    reported_submission_status="not_started",
    kind=AssignmentKind.TASK,
)


def block(
    assignment: str, start: str, end: str, why: str = "first while she is fresh"
) -> PlanBlock:
    return PlanBlock(
        assignment_id=assignment,
        starts_at=time.fromisoformat(start),
        ends_at=time.fromisoformat(end),
        rationale=why,
    )


# -------------------------------------------------------------------- the model


def test_an_assignment_may_have_no_due_date_and_defaults_to_homework() -> None:
    plain = Assignment(
        assignment_id="a",
        course="Science",
        title="Lab report",
        due_date=None,
        dependencies=[],
        reported_submission_status="not_started",
    )

    assert plain.due_date is None
    assert plain.assigned_on is None
    assert plain.kind is AssignmentKind.HOMEWORK


def test_the_due_date_must_be_stated_even_when_it_is_absent() -> None:
    """A source says ``null`` rather than leaving the key out, so an omission is a
    schema error and not a silently undated item."""
    with_key_missing = {
        "assignment_id": "a",
        "course": "Science",
        "title": "Lab report",
        "dependencies": [],
        "reported_submission_status": "not_started",
    }

    with pytest.raises(ValidationError, match="due_date"):
        Assignment.model_validate(with_key_missing)


def test_the_view_carries_the_kind_and_defaults_to_homework() -> None:
    view = StudentAssignmentView(
        assignment_id="a",
        course="Science",
        title="Lab report",
        due_date=None,
        submission_status="not_started",
        deadline_confidence=SourceConfidence.UNVERIFIED,
        source_channels=[],
        disagreement=[],
    )

    assert view.kind is AssignmentKind.HOMEWORK


# -------------------------------------------------------------------- the store


def store_with(*assignments: Assignment) -> ProjectStateStore:
    store = ProjectStateStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())
    store.upsert_assignments(list(assignments))
    return store


def test_the_store_keeps_every_new_field_and_an_absent_date() -> None:
    store = store_with(ESSAY, SYLLABUS)
    try:
        dated = store.due_between(PLAN_DATE, date(2026, 8, 25))
        undated = store.undated()
    finally:
        store.close()

    assert dated == [ESSAY]
    assert dated[0].assigned_on == date(2026, 8, 14)
    assert undated == [SYLLABUS]
    assert undated[0].due_date is None
    assert undated[0].kind is AssignmentKind.TASK


def test_the_week_lists_dated_work_first_and_undated_work_after() -> None:
    """An undated item cannot be placed in any week, so it is in every week."""
    store = store_with(SYLLABUS, ESSAY)
    try:
        result = store.lookup("due_this_week")
    finally:
        store.close()

    assert result is not None
    listed = [item["assignment_id"] for item in result.payload["assignments"]]
    assert listed == ["assignment-canal-essay", "assignment-signed-syllabus"]
    assert result.payload["assignments"][1]["due_date"] is None


# -------------------------------------------------------------------- the checks


def test_an_undated_assignment_must_still_be_accounted_for() -> None:
    forgotten = DailyPlan(
        plan_date=PLAN_DATE, blocks=[block("assignment-canal-essay", "16:30", "17:30")]
    )

    result = check_plan(forgotten, due_in_window=[ESSAY, SYLLABUS], zone=ZONE)

    assert result.failed_checks == (PlanCheck.NOTHING_OMITTED,)
    assert "assignment-signed-syllabus" in result.as_findings()[0]


def test_a_block_on_an_undated_assignment_has_no_deadline_to_miss_and_is_flagged() -> None:
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            block("assignment-canal-essay", "16:30", "17:30"),
            block("assignment-signed-syllabus", "17:30", "17:40", "five minutes with a pen"),
        ],
    )

    result = check_plan(plan, due_in_window=[ESSAY, SYLLABUS], zone=ZONE)

    assert result.passed
    assert result.undated == ("assignment-signed-syllabus",)


# ------------------------------------------------------------------- the prompts


def test_the_planner_is_told_the_kind_the_assigned_date_and_an_unknown_due_date() -> None:
    text = assignments_block([ESSAY, SYLLABUS], {})

    assert 'kind="HOMEWORK"' in text
    assert 'assigned="2026-08-14"' in text
    assert 'due="2026-08-21"' in text
    assert 'kind="TASK"' in text
    assert 'due="unknown"' in text


def test_the_critic_is_told_which_assignments_have_no_date() -> None:
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[block("assignment-canal-essay", "16:30", "17:30")],
        deferred=[Deferral(assignment_id="assignment-signed-syllabus", reason="ask for the date")],
    )
    verification = check_plan(plan, due_in_window=[ESSAY, SYLLABUS], zone=ZONE)

    brief = critic_brief(
        plan_date=PLAN_DATE,
        zone=ZONE.key,
        budget_minutes=150,
        assignments=[ESSAY, SYLLABUS],
        confidence={},
        support_rules=[],
        reflections=[],
        plan=plan,
        verification=verification,
    )

    assert "<undated>assignment-signed-syllabus</undated>" in str(brief[1].content)


# --------------------------------------------------------------------- the draft


def test_the_draft_says_when_there_is_no_due_date() -> None:
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[block("assignment-canal-essay", "16:30", "17:30")],
        deferred=[Deferral(assignment_id="assignment-signed-syllabus", reason="ask for the date")],
    )
    verification = check_plan(plan, due_in_window=[ESSAY, SYLLABUS], zone=ZONE)

    draft = compose_draft(
        draft_id="draft:test",
        plan=plan,
        assignments=[ESSAY, SYLLABUS],
        verification=verification,
        verdict=CriticVerdict(findings=[]),
        settled=False,
    )

    assert "Syllabus, signed (Geometry, no due date on record): ask for the date" in draft.body
    assert "No due date on record; worth asking:" in draft.body


# -------------------------------------------------------------------- the claims


def record(channel: SourceChannel, value: str, seen_in: str | None = None) -> SourceRecord:
    return SourceRecord(
        channel=channel, asserted_value=value, observed_at=OBSERVED, confidence=0.8, seen_in=seen_in
    )


def test_two_claims_from_one_channel_are_told_apart_by_where_they_were_seen() -> None:
    header = record(SourceChannel.LMS, "2026-09-09", "day header")
    inline = record(SourceChannel.LMS, "2026-09-10", "title")

    outcome = Reconciler().reconcile([header, inline])

    assert [claim.describe() for claim in outcome.conflicting_claims] == [  # type: ignore[union-attr]
        "LMS (day header): 2026-09-09",
        "LMS (title): 2026-09-10",
    ]
    assert record(SourceChannel.PARENT_ENTRY, "2026-09-09").describe() == (
        "PARENT_ENTRY: 2026-09-09"
    )


# ---------------------------------------------------------------------- the page


def test_the_page_shows_an_undated_task_and_a_self_disagreeing_source(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "assignments.json").write_text(
        json.dumps([ESSAY.model_dump(mode="json"), SYLLABUS.model_dump(mode="json")]),
        encoding="utf-8",
    )
    (tmp_path / "deadline_sources.json").write_text(
        json.dumps(
            [
                {
                    "assignment_id": "assignment-canal-essay",
                    "channel": "LMS",
                    "asserted_value": "2026-08-21",
                    "observed_at": "2026-08-18T09:00:00+00:00",
                    "confidence": 0.8,
                    "seen_in": "day header",
                },
                {
                    "assignment_id": "assignment-canal-essay",
                    "channel": "LMS",
                    "asserted_value": "2026-08-22",
                    "observed_at": "2026-08-18T09:00:00+00:00",
                    "confidence": 0.8,
                    "seen_in": "title",
                },
            ]
        ),
        encoding="utf-8",
    )
    settings = fixture_settings(
        BLOSSOM_TODAY=PLAN_DATE.isoformat(), BLOSSOM_FIXTURE_PATH=str(tmp_path)
    )

    with TestClient(create_app(settings)) as client:
        page = client.get("/student/due-this-week").text

    assert "Syllabus, signed" in page
    assert "No due date on record" in page
    assert "a task, not a sitting" in page
    assert "LMS (day header): 2026-08-21" in page
    assert "LMS (title): 2026-08-22" in page
