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
from blossom.noticing import monday_of, reconcile_dates
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


LAB = {
    "assignment_id": "lab",
    "course": "Science",
    "title": "Lab write-up",
    "due_date": "2026-08-21",
    "dependencies": [],
    "reported_submission_status": "not_started",
    "assigned_on": "2026-08-17",
    "kind": "HOMEWORK",
}


def claim(value: str, channel: str = "LMS", seen_in: str | None = None) -> dict[str, object]:
    row: dict[str, object] = {
        "assignment_id": "lab",
        "channel": channel,
        "asserted_value": value,
        "observed_at": "2026-08-19T09:00:00-04:00",
        "confidence": 0.8,
    }
    if seen_in is not None:
        row["seen_in"] = seen_in
    return row


def lab_page(tmp_path: pathlib.Path, sources: list[dict[str, object]], **row: object) -> str:
    """Her page for one fixture assignment, the lab write-up, with the claims given."""
    (tmp_path / "assignments.json").write_text(json.dumps([{**LAB, **row}]), encoding="utf-8")
    (tmp_path / "deadline_sources.json").write_text(json.dumps(sources), encoding="utf-8")
    page = student_page(tmp_path)
    return page.split("Lab write-up", 1)[1].split("</article>", 1)[0]


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
    assert '<span class="source">Entered by the family</span>' in page


def test_disagreeing_sources_are_still_listed_individually() -> None:
    """No channel is chosen as the winner; both claims stay visible."""
    page = student_page()

    assert "Sources disagree" in page
    assert "school portal: 2026-08-21" in page
    assert "family entry: 2026-08-22" in page


def test_a_corroborated_assignment_names_the_channels_that_agree() -> None:
    """The algebra set is due the Monday after the fixture week."""
    page = student_page(week="2026-08-24")

    assert '<span class="source">School portal and your report</span>' in page


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
    assert page.count("Entered by the family") == len(in_window)


def test_the_view_no_longer_carries_a_fabricated_workload_count() -> None:
    """The view carries no workload count because no data source backs one."""
    from blossom.views import StudentAssignmentView

    assert "workload_signal_count" not in StudentAssignmentView.model_fields


@pytest.mark.parametrize(
    "said", ["Entered by the family", "Sources disagree", "Date from the family's record;"]
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

    assert "Recorded date Wednesday, August 26" in card
    assert "Due Wednesday, August 26" not in card
    assert "Date from the family's record; the school portal has a different one." in line
    assert "School portal</span>" not in card
    assert "The school says otherwise." in card
    assert "school portal (day header): 2026-08-21" in card


def test_disagreeing_sources_still_say_whose_date_is_on_the_card(tmp_path: pathlib.Path) -> None:
    dated = " ".join(
        lab_page(tmp_path, [claim("2026-08-21"), claim("2026-08-22", "PARENT_ENTRY")]).split()
    )
    undated = " ".join(
        lab_page(
            tmp_path, [claim("2026-08-21"), claim("2026-08-22", "PARENT_ENTRY")], due_date=None
        ).split()
    )

    assert "Date from the family's record; the sources below give different ones." in dated
    assert "No date on the family's record; the sources below give different ones." in undated
    assert "Sources disagree" in dated
    assert "Sources disagree" in undated


def test_a_value_that_is_not_a_date_does_not_confirm_the_record(tmp_path: pathlib.Path) -> None:
    """Two portal entries saying "Friday" agree with each other and read as nothing. The
    record's date is not from them, and the page does not say it is."""
    card = lab_page(tmp_path, [claim("Friday"), claim("Friday", seen_in="title")])
    line = " ".join(card.split())

    assert "Due Friday, August 21" in card
    assert (
        "Date from the family's record; a value from the school portal could not be read as a date."
        in line
    )
    assert "Not read as a date: school portal: Friday; school portal (title): Friday." in card
    assert "School portal</span>" not in card
    assert 'class="confidence disagree"' not in card


def test_a_date_the_record_lacks_is_said_to_be_the_sources(tmp_path: pathlib.Path) -> None:
    card = lab_page(tmp_path, [claim("2026-08-21")], due_date=None)
    line = " ".join(card.split())

    assert "Due date not recorded" in card
    assert "No date on the family's record; the school portal has one." in line
    assert "The school says otherwise. It gives a date where none is recorded." in line
    assert "from the one recorded" not in line
    assert "school portal: 2026-08-21" in card


def test_a_school_date_among_disagreeing_sources_is_one_banner_not_two(
    tmp_path: pathlib.Path,
) -> None:
    """Recorded the 21st, school the 22nd, family the 23rd: the sources disagree and
    none supports the record. The disagreement banner lists every claim once, so
    the school's contradiction adds no second banner and no second list."""
    card = lab_page(tmp_path, [claim("2026-08-22"), claim("2026-08-23", "PARENT_ENTRY")])

    assert "Recorded date Friday, August 21" in card
    assert card.count('class="confidence disagree"') == 1
    assert card.count("What the sources say") == 1
    assert "Sources disagree about it" in card
    assert "It gives a different date from the one recorded" not in card
    assert "What they gave" not in card
    listed = card.split("What the sources say", 1)[1]
    assert "school portal: 2026-08-22" in listed
    assert "family entry: 2026-08-23" in listed


def test_a_readable_date_that_matches_is_the_sources(tmp_path: pathlib.Path) -> None:
    one = lab_page(tmp_path, [claim("2026-08-21")])
    two = lab_page(tmp_path, [claim("2026-08-21"), claim("2026-08-21", "STUDENT_REPORT")])
    mixed = " ".join(
        lab_page(tmp_path, [claim("2026-08-21"), claim("2026-08-21", seen_in="title")]).split()
    )

    assert '<span class="source">School portal</span>' in one
    assert '<span class="source">School portal and your report</span>' in two
    assert '<span class="source">School portal</span>' in mixed, "one channel twice is one source"


def test_claims_are_reconciled_as_dates_and_only_when_they_read_as_dates() -> None:
    spaced = reconcile_dates(
        [
            record(SourceChannel.LMS, " 2026-08-21 "),
            record(SourceChannel.STUDENT_REPORT, "2026-08-21"),
        ]
    )
    beside = reconcile_dates(
        [record(SourceChannel.LMS, "2026-08-21"), record(SourceChannel.STUDENT_REPORT, "Friday")]
    )
    nothing = reconcile_dates([record(SourceChannel.LMS, "Friday")])

    assert classify_confidence(spaced) is SourceConfidence.CORROBORATED
    assert classify_confidence(beside) is SourceConfidence.SINGLE_SOURCE
    assert isinstance(nothing, NoSourceRecords)


def test_claims_compared_as_dates_are_kept_as_the_source_spelled_them(
    tmp_path: pathlib.Path,
) -> None:
    """The comparison reads the date; the record shown is the claim as asserted."""
    differing = reconcile_dates(
        [record(SourceChannel.LMS, "20260821"), record(SourceChannel.PARENT_ENTRY, "2026-08-22")]
    )
    agreeing = reconcile_dates(
        [
            record(SourceChannel.LMS, " 2026-08-21 "),
            record(SourceChannel.STUDENT_REPORT, "20260821"),
        ]
    )
    card = lab_page(tmp_path, [claim("20260821"), claim("2026-08-22", "PARENT_ENTRY")])

    assert isinstance(differing, Disagreement)
    assert [c.asserted_value for c in differing.conflicting_claims] == ["20260821", "2026-08-22"]
    assert isinstance(agreeing, Agreement)
    assert [c.asserted_value for c in agreeing.records] == [" 2026-08-21 ", "20260821"]
    assert "Sources disagree" in card
    assert "school portal: 20260821" in card


def test_a_space_around_a_date_is_not_a_disagreement(tmp_path: pathlib.Path) -> None:
    card = lab_page(tmp_path, [claim(" 2026-08-21 "), claim("2026-08-21", "STUDENT_REPORT")])

    assert '<span class="source">School portal and your report</span>' in card
    assert 'class="confidence disagree"' not in card


def test_a_weekday_beside_a_date_is_listed_apart_not_set_against_it(tmp_path: pathlib.Path) -> None:
    card = lab_page(tmp_path, [claim("2026-08-21"), claim("Friday", "STUDENT_REPORT")])

    assert '<span class="source">School portal</span>' in card
    assert "Not read as a date: your report: Friday." in card
    assert 'class="confidence disagree"' not in card


def test_a_contradiction_names_only_the_channels_that_gave_a_date(tmp_path: pathlib.Path) -> None:
    card = lab_page(tmp_path, [claim("2026-08-22"), claim("Friday", "STUDENT_REPORT")])
    line = " ".join(card.split())
    listed = card.split("What the sources say", 1)[1]

    assert "Date from the family's record; the school portal has a different one." in line
    assert "Not read as a date: your report: Friday." in card
    assert "school portal: 2026-08-22" in listed
    assert "Friday" not in listed


def test_only_the_schools_date_is_a_banner(tmp_path: pathlib.Path) -> None:
    """A parent's entry or her own report that differs from the record is said
    quietly, with what they gave; a school channel's is the banner."""
    parent = lab_page(tmp_path, [claim("2026-08-22", "PARENT_ENTRY")])
    hers = lab_page(tmp_path, [claim("2026-08-22", "STUDENT_REPORT")])
    school = lab_page(tmp_path, [claim("2026-08-22", "EMAIL")])

    assert "what a parent entered has a different one." in " ".join(parent.split())
    assert "What they gave: family entry: 2026-08-22." in parent
    assert "The school says otherwise." not in parent
    assert 'class="confidence disagree"' not in parent
    assert "what you reported has a different one." in " ".join(hers.split())
    assert 'class="confidence disagree"' not in hers
    assert "The school says otherwise." in school
    assert "school email: 2026-08-22" in school.split("What the sources say", 1)[1]


@pytest.mark.parametrize(
    ("sources", "named"),
    [
        ([("Friday", "STUDENT_REPORT")], "what you reported"),
        ([("Friday", "PARENT_ENTRY")], "what a parent entered"),
        (
            [("Friday", "LMS"), ("Friday", "STUDENT_REPORT")],
            "the school portal and what you reported",
        ),
    ],
)
def test_an_unreadable_value_is_said_in_words_that_fit_every_channel(
    tmp_path: pathlib.Path, sources: list[tuple[str, str]], named: str
) -> None:
    """The words for her own report and a parent's entry begin with "what", so the
    sentence is built around a value from them rather than what they said."""
    card = lab_page(tmp_path, [claim(value, channel) for value, channel in sources])
    line = " ".join(card.split())

    assert f"a value from {named} could not be read as a date." in line
    assert "what what" not in line


def test_an_undated_record_with_unreadable_claims_is_not_said_to_have_a_date(
    tmp_path: pathlib.Path,
) -> None:
    card = lab_page(tmp_path, [claim("Friday")], due_date=None)
    line = " ".join(card.split())

    assert "Due date not recorded" in card
    assert (
        "No date on the family's record; a value from the school portal could not be read" in line
    )
    assert "Date from the family's record" not in line
    assert "Not read as a date: school portal: Friday." in card


def test_a_record_in_another_year_shows_its_year_when_a_source_puts_it_in_this_week(
    tmp_path: pathlib.Path,
) -> None:
    """The range carries the week's year; a card repeats a year the range does not cover."""
    card = lab_page(tmp_path, [claim("2026-08-21")], due_date="2027-08-21")

    assert "Recorded date Saturday, August 21, 2027" in card
    assert "The school says otherwise." in card


def test_a_contested_date_is_named_as_recorded_and_the_card_says_to_check_it(
    tmp_path: pathlib.Path,
) -> None:
    """The record's date stays on the card as the recorded date; the sources' dates
    sit under the warning in readable channel names; nothing is chosen."""
    contradicted = lab_page(tmp_path, [claim("2026-08-22")])
    disagreeing = lab_page(tmp_path, [claim("2026-08-21"), claim("2026-08-22", "PARENT_ENTRY")])

    assert "Recorded date Friday, August 21" in contradicted
    assert "<strong>Check this date.</strong> The school says otherwise" in contradicted
    assert "school portal: 2026-08-22" in contradicted.split("What the sources say", 1)[1]
    assert "Recorded date Friday, August 21" in disagreeing
    assert "Due Friday, August 21" not in disagreeing
    assert "<strong>Check this date.</strong> Sources disagree about it" in disagreeing
    listed = disagreeing.split("What the sources say", 1)[1]
    assert "school portal: 2026-08-21" in listed
    assert "family entry: 2026-08-22" in listed
    assert "LMS" not in contradicted
    assert "LMS" not in disagreeing
    assert "PARENT_ENTRY" not in disagreeing


def test_every_card_carries_exactly_one_quiet_line_on_the_fixtures() -> None:
    for week in (None, "2026-08-24"):
        page = student_page(week=week)
        assert page.count('<article class="assignment') == page.count('class="source"')


def test_only_disagreement_and_contradiction_are_made_prominent() -> None:
    """A warning on the page means something because the rest is said quietly."""
    page = student_page()

    banners = page.count('class="confidence disagree"')
    assert banners == 3, "the essay's sources, the cover's two dates, and the quiz's record"
    assert 'class="confidence unverified"' not in page
    assert 'class="confidence corroborated"' not in page
