"""Tests that nothing is hidden from the student's weekly view.

Two failures motivated this module, and the second was worse than the first.

An assignment whose date no channel corroborates was dropped from the view by
an `if verification.passed` filter. That alone contradicts the design, which
treats an absence of corroboration as a finding to surface rather than a reason
to stay silent.

Underneath that, `Reconciler.reconcile` raised `ValueError` on an empty record
list, and it ran before the filter. So a single source-less assignment did not
merely hide itself -- it returned HTTP 500 and took every other assignment on
the page down with it. The regression test for that is
`test_a_source_less_assignment_does_not_break_the_page`.
"""

import json
import pathlib

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
from blossom.settings import Settings
from tests.support import record

PINNED_TODAY = "2026-08-19"


def student_page(fixture_root: pathlib.Path | None = None) -> str:
    environment = {"BLOSSOM_TODAY": PINNED_TODAY}
    if fixture_root is not None:
        environment["BLOSSOM_FIXTURE_PATH"] = str(fixture_root)
    settings = Settings.from_environment(environment)
    with TestClient(create_app(settings)) as client:
        response = client.get("/student/due-this-week")
    assert response.status_code == 200
    return response.text


def test_reconciler_reports_absence_instead_of_raising() -> None:
    """It used to raise, which made the absence unreportable."""
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
    """The regression guard. This returned HTTP 500 before, hiding everything."""
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
    """Nothing filters. The count on the page matches the count in the store."""
    fixtures = pathlib.Path("data/synthetic")
    assignments = json.loads((fixtures / "assignments.json").read_text(encoding="utf-8"))
    (tmp_path / "assignments.json").write_text(json.dumps(assignments), encoding="utf-8")
    (tmp_path / "deadline_sources.json").write_text("[]", encoding="utf-8")

    page = student_page(tmp_path)

    for assignment in assignments:
        assert assignment["title"] in page, f"{assignment['title']} was dropped"
    assert page.count("Unverified due date") == len(assignments)


def test_the_view_no_longer_carries_a_fabricated_workload_count() -> None:
    """It was hardcoded to 1 for every assignment, which was simply untrue."""
    from blossom.views import StudentAssignmentView

    assert "workload_signal_count" not in StudentAssignmentView.model_fields


@pytest.mark.parametrize("banner", ["Unverified due date", "Sources disagree", "Confirmed by"])
def test_confidence_is_always_stated_never_left_to_absence(banner: str) -> None:
    """Every card carries a banner, so 'unverified' is not signalled by silence."""
    assert banner in student_page()
