"""Tests that nothing is hidden from the student's weekly view.

An assignment whose date no channel corroborates stays on the page and is
labeled unverified. The design treats an absence of corroboration as a finding
to surface, not a reason to stay silent.

`Reconciler.reconcile` returns `NoSourceRecords` for an empty record list
rather than raising, so one source-less assignment cannot take the rest of the
page down with it.
"""

import json
import pathlib
from datetime import date

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.reconciliation import (
    Agreement,
    Disagreement,
    NoSourceRecords,
    Reconciler,
    SourceChannel,
    SourceConfidence,
    classify_confidence,
)
from blossom.stores.project_state import DUE_THIS_WEEK_SPAN
from tests.support import fixture_settings, record

PINNED_TODAY = "2026-08-19"


def student_page(fixture_root: pathlib.Path | None = None) -> str:
    environment = {"BLOSSOM_TODAY": PINNED_TODAY}
    if fixture_root is not None:
        environment["BLOSSOM_FIXTURE_PATH"] = str(fixture_root)
    settings = fixture_settings(**environment)
    with TestClient(create_app(settings)) as client:
        response = client.get("/student/due-this-week")
    assert response.status_code == 200
    return response.text


def test_reconciler_reports_absence_instead_of_raising() -> None:
    """An empty record list is a reportable outcome, not an error."""
    assert isinstance(Reconciler().reconcile([]), NoSourceRecords)


def test_confidence_classification_covers_every_reconciliation_outcome() -> None:
    lms = record(SourceChannel.LMS, "2026-08-21")
    parent = record(SourceChannel.PARENT_ENTRY, "2026-08-22")
    agreeing = record(SourceChannel.STUDENT_REPORT, "2026-08-21")

    assert classify_confidence(NoSourceRecords()) is SourceConfidence.UNVERIFIED
    assert (
        classify_confidence(Disagreement(conflicting_claims=[lms, parent]))
        is SourceConfidence.SOURCES_DISAGREE
    )
    assert (
        classify_confidence(Agreement(value="2026-08-21", records=[lms]))
        is SourceConfidence.SINGLE_SOURCE
    )
    assert (
        classify_confidence(Agreement(value="2026-08-21", records=[lms, agreeing]))
        is SourceConfidence.CORROBORATED
    )


def test_a_source_less_assignment_does_not_break_the_page() -> None:
    """One assignment with no sources leaves every other assignment on the page."""
    page = student_page()

    assert "Science fair topic proposal" in page
    assert "Canal Era comparison essay" in page


def test_an_uncorroborated_assignment_is_shown_and_labelled_unverified() -> None:
    page = student_page()

    assert "Science fair topic proposal" in page
    assert "Unverified due date" in page


def test_disagreeing_sources_are_still_listed_individually() -> None:
    """No channel is chosen as the winner; both claims stay visible."""
    page = student_page()

    assert "Sources disagree" in page
    assert "LMS: 2026-08-21" in page
    assert "PARENT_ENTRY: 2026-08-22" in page


def test_a_corroborated_assignment_names_the_channels_that_agree() -> None:
    page = student_page()

    assert "Confirmed by 2 sources" in page
    assert "LMS, STUDENT_REPORT" in page


def test_every_assignment_in_the_window_reaches_the_page(tmp_path: pathlib.Path) -> None:
    """Nothing filters. With no sources, the week is the record's alone, and every
    item the record puts in it is on the page, each marked unverified."""
    fixtures = pathlib.Path("data/synthetic")
    assignments = json.loads((fixtures / "assignments.json").read_text(encoding="utf-8"))
    (tmp_path / "assignments.json").write_text(json.dumps(assignments), encoding="utf-8")
    (tmp_path / "deadline_sources.json").write_text("[]", encoding="utf-8")
    start = date.fromisoformat(PINNED_TODAY)
    in_window = [
        item
        for item in assignments
        if item["due_date"] is None
        or start <= date.fromisoformat(item["due_date"]) <= start + DUE_THIS_WEEK_SPAN
    ]
    assert len(in_window) == len(assignments) - 1

    page = student_page(tmp_path)

    for assignment in in_window:
        assert assignment["title"] in page, f"{assignment['title']} was dropped"
    assert page.count("Unverified due date") == len(in_window)


def test_the_view_no_longer_carries_a_fabricated_workload_count() -> None:
    """The view carries no workload count because no data source backs one."""
    from blossom.views import StudentAssignmentView

    assert "workload_signal_count" not in StudentAssignmentView.model_fields


@pytest.mark.parametrize("banner", ["Unverified due date", "Sources disagree", "Confirmed by"])
def test_confidence_is_always_stated_never_left_to_absence(banner: str) -> None:
    """Every card carries a banner, so 'unverified' is not signaled by silence."""
    assert banner in student_page()
