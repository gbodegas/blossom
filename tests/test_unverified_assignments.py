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
from blossom.noticing import monday_of
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


def student_page(fixture_root: pathlib.Path | None = None, week: str | None = None) -> str:
    environment = {"BLOSSOM_TODAY": PINNED_TODAY}
    if fixture_root is not None:
        environment["BLOSSOM_FIXTURE_PATH"] = str(fixture_root)
    settings = fixture_settings(**environment)
    params = {} if week is None else {"week": week}
    with TestClient(create_app(settings)) as client:
        response = client.get("/student/due-this-week", params=params)
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


def test_an_uncorroborated_assignment_is_shown_and_says_so_quietly() -> None:
    page = student_page()

    assert "Science fair topic proposal" in page
    assert "From the family's record only; nothing from the school confirms it yet." in page


def test_disagreeing_sources_are_still_listed_individually() -> None:
    """No channel is chosen as the winner; both claims stay visible."""
    page = student_page()

    assert "Sources disagree" in page
    assert "LMS: 2026-08-21" in page
    assert "PARENT_ENTRY: 2026-08-22" in page


def test_a_corroborated_assignment_names_the_channels_that_agree() -> None:
    """The algebra set is due the Monday after the fixture week."""
    page = student_page(week="2026-08-24")

    assert "Date confirmed by the school portal and what you reported." in page


def test_one_channel_saying_a_date_twice_is_still_one_source() -> None:
    twice = Reconciler().reconcile(
        [record(SourceChannel.LMS, "2026-08-21"), record(SourceChannel.LMS, "2026-08-21")]
    )
    two = Reconciler().reconcile(
        [record(SourceChannel.LMS, "2026-08-21"), record(SourceChannel.EMAIL, "2026-08-21")]
    )

    assert classify_confidence(twice) is SourceConfidence.SINGLE_SOURCE
    assert classify_confidence(two) is SourceConfidence.CORROBORATED


def test_every_assignment_in_the_window_reaches_the_page(tmp_path: pathlib.Path) -> None:
    """Nothing filters. With no sources, the week is the record's alone, and every
    item the record puts in the school week is on the page, each saying so."""
    fixtures = pathlib.Path("data/synthetic")
    assignments = json.loads((fixtures / "assignments.json").read_text(encoding="utf-8"))
    (tmp_path / "assignments.json").write_text(json.dumps(assignments), encoding="utf-8")
    (tmp_path / "deadline_sources.json").write_text("[]", encoding="utf-8")
    monday = monday_of(date.fromisoformat(PINNED_TODAY))
    in_window = [
        item
        for item in assignments
        if item["due_date"] is None
        or monday <= date.fromisoformat(item["due_date"]) <= monday + DUE_THIS_WEEK_SPAN
    ]
    assert len(in_window) == len(assignments) - 3

    page = student_page(tmp_path)

    for assignment in in_window:
        assert assignment["title"] in page, f"{assignment['title']} was dropped"
    assert page.count("From the family's record only") == len(in_window)


def test_the_view_no_longer_carries_a_fabricated_workload_count() -> None:
    """The view carries no workload count because no data source backs one."""
    from blossom.views import StudentAssignmentView

    assert "workload_signal_count" not in StudentAssignmentView.model_fields


@pytest.mark.parametrize(
    "said", ["From the family's record only", "Sources disagree", "Date from the family's record;"]
)
def test_where_a_date_came_from_is_always_stated_never_left_to_absence(said: str) -> None:
    """Every card says where its date came from, so silence never carries a meaning."""
    assert said in student_page()


def test_a_contradicted_card_names_the_record_as_the_date_shown() -> None:
    """The card shows the record's date, so the quiet line cannot attribute that
    date to a source that gave another; the banner carries what the source said."""
    page = student_page()
    card = page.split("Vocabulary quiz, unit one", 1)[1].split("</article>", 1)[0]
    line = " ".join(card.split())

    assert "Due Wednesday, August 26, 2026" in card
    assert "Date from the family's record; the school portal has a different one." in line
    assert "Date from the school portal." not in card
    assert "The school says otherwise." in card
    assert "LMS (day header): 2026-08-21" in card


def test_only_disagreement_and_contradiction_are_made_prominent() -> None:
    """A warning on the page means something because the rest is said quietly."""
    page = student_page()

    banners = page.count('class="confidence disagree"')
    assert banners == 3, "the essay's sources, the cover's two dates, and the quiz's record"
    assert 'class="confidence unverified"' not in page
    assert 'class="confidence corroborated"' not in page
