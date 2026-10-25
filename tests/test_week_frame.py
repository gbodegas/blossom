# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Her page frames the school week, Monday to Sunday, and moves between weeks.

The planner keeps its own horizon, the seven days from the evening it plans,
and the page says through which day that reaches. Work assigned in the week
and due after it is listed under the week's cards, the way the school's own
page frames a week. The evening's minute budgets are the household's settings
and the page shows the numbers it was given.
"""

import html
import json
import pathlib
import re
from datetime import date
from typing import get_args

import pytest
from fastapi.testclient import TestClient

from blossom.agent.prompts import PLANNER_SYSTEM
from blossom.app import create_app
from blossom.drafts import Decision
from blossom.noticing import monday_of
from blossom.reconciliation import SourceConfidence
from blossom.routes.navigation import details_href
from blossom.settings import STATIC_PATH as STATIC
from blossom.settings import TEMPLATE_PATH as TEMPLATES
from tests.support import (
    SAME_ORIGIN,
    fixture_settings,
    fresh_signal_fields,
    outside_earlier_work,
    report,
    shown_heading,
)

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
    """Her page with the clock pinned to the fixture week unless ``environment`` says otherwise."""
    settings = fixture_settings(**{"BLOSSOM_TODAY": PINNED_TODAY, **environment})
    params = {} if week is None else {"week": week}
    with TestClient(create_app(settings), headers=SAME_ORIGIN) as client:
        response = client.get("/student/due-this-week", params=params)
    return response.status_code, response.text


def test_the_week_is_the_school_week_that_holds_today() -> None:
    status, shown = page()

    assert status == 200
    assert "<h1>My week</h1>" in shown
    assert "August 17 to" in shown
    assert "August 23, 2026" in shown
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
    assert "<strong>Quadratic modeling problem set</strong> &middot; Algebra II" in after
    assert "Due Monday, August 24" in after
    assert "<strong>Reading log, week one</strong> &middot; English" in after
    assert "Due Tuesday, August 25" in after


def test_the_next_week_is_a_page_of_its_own() -> None:
    status, shown = page(week="2026-08-24")

    assert status == 200
    assert "<h1>Week of August 24</h1>" in shown
    assert "August 24 to" in shown
    assert "August 30, 2026" in shown
    assert 'href="/student/due-this-week">This week</a>' in shown
    assert "Quadratic modeling problem set" in shown
    assert "Reading log, week one" in shown
    assert "Syllabus, signed" in shown, "undated work is in every week"
    assert "Canal Era comparison essay" not in shown
    assert "Assigned this week, due later" not in shown


def test_a_week_across_the_new_year_gives_both_years() -> None:
    _, shown = page(week="2026-12-30")
    line = " ".join(shown.split())

    assert "<h1>Week of Decem\u00adber 28</h1>" in shown
    assert "December 28, 2026 to January 3, 2027" in line


def test_any_day_names_its_whole_week() -> None:
    _, from_monday = page(week=MONDAY)
    _, from_thursday = page(week="2026-08-20")
    _, from_sunday = page(week="2026-08-23")

    for shown in (from_monday, from_thursday, from_sunday):
        assert "<h1>My week</h1>" in shown
        assert "August 17 to" in shown


@pytest.mark.parametrize("given", ["soon", "", "   ", "2026-08"])
def test_a_week_that_is_not_a_date_is_said_on_todays_week(given: str) -> None:
    """Only an absent ``week`` means today's week without a word; a blank one is not a date."""
    status, shown = page(week=given)

    assert status == 422
    assert "That is not a date, so this is the week that holds today." in shown
    assert "<h1>My week</h1>" in shown


@pytest.mark.parametrize("given", ["0001-01-01", "0001-01-05", "9999-12-31", "9999-12-27"])
def test_the_edges_of_the_calendar_are_said_not_crashed_into(given: str) -> None:
    """The first and last weeks the date type holds have no week on one side to link to."""
    status, shown = page(week=given)

    assert status == 422
    assert "That week is past the edge of the calendar" in shown
    assert "<h1>My week</h1>" in shown


def test_todays_week_at_the_edge_of_what_can_be_pinned_still_renders() -> None:
    """The first and last days a clock may be pinned to have a week either side."""
    _, first = page(BLOSSOM_TODAY="0001-01-08")
    _, last = page(BLOSSOM_TODAY="9999-12-24")

    assert "<h1>My week</h1>" in first
    assert 'href="/student/due-this-week?week=0001-01-01">Previous week</a>' in first
    assert "<h1>My week</h1>" in last
    assert 'href="/student/due-this-week?week=9999-12-27">Next week</a>' in last


@pytest.mark.parametrize(
    ("given", "heading"),
    [("0001-01-08", "Week of January 8"), ("9999-12-20", "Week of December 20")],
)
def test_the_weeks_just_inside_the_edges_are_shown(given: str, heading: str) -> None:
    status, shown = page(week=given)

    assert status == 200
    assert f"<h1>{shown_heading(heading)}</h1>" in shown


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

    assert "<strong>Due after the week</strong> &middot; Science" in shown
    assert "Due Thursday, August 27" in shown
    assert "Due before the week" not in outside_earlier_work(shown)
    assert "Due before the week" in week_before


def test_work_due_in_another_year_says_which(tmp_path: pathlib.Path) -> None:
    """The list can hold work due in any later year, so the year is part of the date."""
    row = dict(FIXTURE_ROW, assignment_id="next-year", title="Long project", due_date="2027-08-27")
    (tmp_path / "assignments.json").write_text(json.dumps([row]), encoding="utf-8")
    (tmp_path / "deadline_sources.json").write_text("[]", encoding="utf-8")

    _, shown = page(BLOSSOM_FIXTURE_PATH=str(tmp_path))

    assert "<strong>Long project</strong> &middot; Science" in shown
    assert "Due Friday, August 27, 2027" in shown


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

    assert "No assignments are recorded as due this week." in this_week
    assert "No assignments are recorded as due that week." in that_week


def test_the_page_keeps_the_horizon_explanation_out_of_the_way() -> None:
    """The short statement of how far a plan looks lives inside the plan disclosure,
    checked where a plan exists; nothing about it sits above the assignments."""
    _, shown = page()

    assert "looks at everything due through" not in shown
    assert "whichever week that falls in" not in shown


def test_the_page_shows_the_households_budgets() -> None:
    settings = fixture_settings(
        BLOSSOM_TODAY=PINNED_TODAY, BLOSSOM_EVENING_MINUTES="120", BLOSSOM_TOO_MUCH_MINUTES="40"
    )
    with TestClient(create_app(settings), headers=SAME_ORIGIN) as client:
        client.post(
            "/student/actions/too-much",
            data=fresh_signal_fields(client),
            follow_redirects=False,
        )
        shown = client.get("/student/due-this-week").text

    assert "Your next plan will use up to 40 minutes." in shown
    assert "75 minutes" not in shown


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


CARD_GIVES_WAY = (
    "   text at 200%, keeps the ordinary padding and margin. */\n"
    ".assignment.confidence-corroborated,\n"
    ".assignment.confidence-single_source,\n"
    ".assignment.confidence-sources_disagree,\n"
    ".assignment.confidence-unverified {\n"
    "  margin-inline: min(0px, 13vw - 2.25rem);\n"
    "  padding-inline: clamp(0px, 13vw - 1rem, 1.6rem);\n}\n\n"
    "@media (max-width: 30rem) {\n"
    "  .assignment.confidence-corroborated,\n"
    "  .assignment.confidence-single_source,\n"
    "  .assignment.confidence-sources_disagree,\n"
    "  .assignment.confidence-unverified {\n"
    "    padding-inline: clamp(0px, 13vw - 1rem, 1.2rem);\n  }\n}\n"
)
"""The rule that lets a homework card and an assignment's details give up side padding on a
narrow screen with large text, pinned from the last line of its comment through its narrow
block. Check her week and the details in a browser at 320 px and 1280 px before changing it."""


def test_the_homework_card_s_narrow_rule_is_pinned_and_names_every_date_confidence() -> None:
    """The pinned text occurs once, so no selector stands before, between or after the ones it
    lists: one for each confidence a date can have, in both blocks. No other rule names such a
    class, and the card's and the details' templates still write it."""
    styles = (STATIC / "blossom.css").read_text(encoding="utf-8")
    card = (TEMPLATES / "student_card.html").read_text(encoding="utf-8")
    details = (TEMPLATES / "student_assignment.html").read_text(encoding="utf-8")
    named = [f".assignment.confidence-{one.value.lower()}" for one in SourceConfidence]

    assert styles.count(CARD_GIVES_WAY) == 1
    assert styles.count(".assignment.confidence-") == CARD_GIVES_WAY.count(
        ".assignment.confidence-"
    )
    assert [
        CARD_GIVES_WAY.count(f"{one},") + CARD_GIVES_WAY.count(f"{one} {{") for one in named
    ] == [2] * len(SourceConfidence)
    assert CARD_GIVES_WAY.count(".assignment.confidence-") == 2 * len(SourceConfidence)
    assert 'class="assignment confidence-{{ assignment.deadline_confidence|lower }}"' in card
    assert (
        'class="assignment detail confidence-{{ assignment.deadline_confidence|lower }}"' in details
    )


PANELS_GIVE_WAY = (
    "   and margin. */\n"
    ".panel.apart,\n.panel.assigned {\n"
    "  margin-inline: min(0px, 13vw - 2.25rem);\n"
    "  padding-inline: clamp(0px, 13vw - 1rem, 1.6rem);\n}\n\n"
    "@media (max-width: 30rem) {\n"
    "  .panel.apart,\n  .panel.assigned {\n"
    "    padding-inline: clamp(0px, 13vw - 1rem, 1.2rem);\n  }\n}\n"
)
"""The rule that lets the panels of her week that hold homework give up side padding on a
narrow screen with large text, pinned from the last line of its comment through its narrow
block. Check her week in a browser at 320 px and 1280 px, with a card outside the week shown."""
CARD_KEEPS_TO_ITS_PANEL = (
    "   side margin for it, so the card keeps to the panel's own line. */\n"
    ".panel.apart .assignment {\n  margin-inline: 0;\n}\n"
)
"""The rule that keeps the card outside the week shown on its panel's own line, pinned from the
last line of its comment."""


def test_her_week_s_homework_panels_narrow_rule_is_pinned_and_only_her_week_writes_them() -> None:
    """Each pinned text occurs once, so no selector stands before, between or after the two
    panel classes the rule lists; and only her week's two templates write a panel with either."""
    styles = (STATIC / "blossom.css").read_text(encoding="utf-8")
    writers = {
        page.name: text
        for page in sorted(TEMPLATES.glob("*.html"))
        if 'class="panel apart' in (text := page.read_text(encoding="utf-8"))
        or 'class="panel assigned' in text
    }

    assert styles.count(PANELS_GIVE_WAY) == 1
    assert styles.count(CARD_KEEPS_TO_ITS_PANEL) == 1
    assert styles.count(".panel.assigned") == 2
    assert styles.count(".panel.apart") == 4
    assert sorted(writers) == ["student_due_this_week.html", "student_earlier_work.html"]
    assert writers["student_due_this_week.html"].count('<section class="panel apart">') == 1
    assert writers["student_due_this_week.html"].count('<section class="panel assigned">') == 1
    assert writers["student_earlier_work.html"].count('class="panel assigned earlier-work"') == 1


CARDS_GIVE_WAY = (
    "   and 640 pixels or wider with text at 200%. */\n"
    "main > section:not(.panel) > .draft,\n.panel-fold > section:not(.panel) > .draft {\n"
    "  margin-inline: min(0px, 13vw - 2.25rem);\n"
    "  max-width: none;\n"
    "  padding-inline: clamp(0px, 13vw - 1rem, 1.6rem);\n}\n\n"
    "@media (max-width: 30rem) {\n"
    "  main > section:not(.panel) > .draft,\n  .panel-fold > section:not(.panel) > .draft {\n"
    "    padding-inline: clamp(0px, 13vw - 1rem, 1.2rem);\n  }\n}\n"
)
"""The rule that lets every card of the family page give up side padding and reach into the
page's side margin on a narrow screen with large text, pinned from the last line of its
comment through its narrow block. Check the family page in a browser at 320 px and 1280 px."""


CARD_CUE_STAYS_ON_SCREEN = (
    "   with ordinary text and 400 pixels or wider with text at 200%. */\n"
    'main > section:not(.panel) > .draft[tabindex="-1"]:focus {\n'
    "  box-shadow:\n"
    "    0 0 0 clamp(0px, 13vw - 1.25rem, 0.375rem) var(--arrival-tint),\n"
    "    -0.25rem 0 0 clamp(0px, 13vw - 1.25rem, 0.375rem) var(--blue-action);\n}\n"
)
"""The arrival cue of a family card a link lands on, its spread narrowed to the margin left
beside the card, pinned from the last line of its comment."""
SECTIONS_REACH_WITH_THEIR_CARDS = (
    "   and its cue on every screen 200 or 400 pixels or wider. */\n"
    "#help-she-asked-for,\n#turning-work-in {\n"
    "  margin-inline: min(0px, 13vw - 2.25rem);\n"
    "  padding-inline: clamp(0px, 2.25rem - 13vw, 2.25rem);\n}\n\n"
    "section#help-she-asked-for:focus {\n"
    "  box-shadow:\n"
    "    0 0 0 clamp(0px, 13vw - 1.25rem, 0.375rem) var(--arrival-tint),\n"
    "    -0.25rem 0 0 clamp(0px, 13vw - 1.25rem, 0.375rem) var(--blue-action);\n}\n"
)
"""The two family sections a link lands on that hold cards, reaching with their cards and
padding their words back in, and the narrowed cue of the one that has a cue, pinned from the
last line of their comment. Check both landings in a browser at 320 px with text at 200%."""


def test_the_family_page_s_landing_cues_are_pinned_word_for_word() -> None:
    """Each pinned text occurs once, and the narrowed spread is written only in those two cues,
    so a cue put back to its full spread, or a section that stops reaching, fails here."""
    styles = (STATIC / "blossom.css").read_text(encoding="utf-8")

    assert styles.count(CARD_CUE_STAYS_ON_SCREEN) == 1
    assert styles.count(SECTIONS_REACH_WITH_THEIR_CARDS) == 1
    assert styles.count("clamp(0px, 13vw - 1.25rem, 0.375rem)") == 4
    assert styles.count("#turning-work-in") == 1


def test_the_family_page_s_cards_narrow_rule_is_pinned_and_no_decision_has_its_own() -> None:
    """The pinned text occurs once; the family page writes ten cards and the page of plans
    shown as saved one, inside its panel; and no rule names a plan's decision, of the four
    there are, so a card of any decision takes the shared rule."""
    styles = (STATIC / "blossom.css").read_text(encoding="utf-8")
    writers = {
        page.name: text
        for page in sorted(TEMPLATES.glob("*.html"))
        if 'class="draft' in (text := page.read_text(encoding="utf-8"))
    }

    assert styles.count(CARDS_GIVE_WAY) == 1
    assert styles.count("> section:not(.panel) > .draft") == 5
    assert sorted(writers) == ["parent_review.html", "plans_unavailable.html"]
    assert writers["parent_review.html"].count('<article class="draft') == 10
    assert writers["parent_review.html"].count('class="draft decided-{{ draft.decision }}"') == 2
    assert writers["plans_unavailable.html"].count('<article class="draft">') == 1
    assert writers["plans_unavailable.html"].count('<section class="panel saved-plans">') == 1
    assert get_args(Decision) == ("approved", "rejected", "expired", "superseded")
    for decision in get_args(Decision):
        assert f"decided-{decision}" not in styles


LATER_ROWS = {
    "agreed": "Agreed project",
    "disputed": "Disputed project",
    "unread": "Unread project",
    "contradicted": "Contradicted project",
}
"""Work assigned this week and due after it, one row for each way its date can stand."""
LATER_CLAIMS = [
    ("agreed", "LMS", "2026-08-27"),
    ("agreed", "EMAIL", "2026-08-27"),
    ("disputed", "LMS", "2026-08-27"),
    ("disputed", "EMAIL", "2026-08-28"),
    ("unread", "LMS", "the Thursday after next"),
    ("contradicted", "LMS", "2026-08-28"),
]
"""What the sources say of each row's date: two that agree, two that disagree, one that
can't be read as a date, and the school's alone against the record's."""


def later_rows(tmp_path: pathlib.Path, done: str | None = None) -> tuple[dict[str, str], str]:
    """Each row due later on her week, by its id, with ``done`` reported done, and the
    details page its evidence link names, for the disputed row."""
    rows = [
        dict(FIXTURE_ROW, assignment_id=name, title=title, due_date="2026-08-27")
        for name, title in LATER_ROWS.items()
    ]
    claims = [
        {
            "assignment_id": name,
            "channel": channel,
            "asserted_value": value,
            "observed_at": "2026-08-18T09:00:00-04:00",
            "confidence": 0.8,
        }
        for name, channel, value in LATER_CLAIMS
    ]
    (tmp_path / "assignments.json").write_text(json.dumps(rows), encoding="utf-8")
    (tmp_path / "deadline_sources.json").write_text(json.dumps(claims), encoding="utf-8")
    settings = fixture_settings(
        BLOSSOM_TODAY=PINNED_TODAY,
        BLOSSOM_FIXTURE_PATH=str(tmp_path),
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
    )
    with TestClient(create_app(settings), headers=SAME_ORIGIN, follow_redirects=False) as client:
        if done is not None:
            report(client, done, "done", week=MONDAY)
        shown = client.get("/student/due-this-week").text
        later = shown.partition("Assigned this week, due later")[2]
        found = {
            name: later.split(f"<strong>{title}</strong>", 1)[1].split("</li>", 1)[0]
            for name, title in LATER_ROWS.items()
        }
        evidence = html.unescape(re.findall(r'href="([^"]*#evidence)"', found["disputed"])[0])
        details = client.get(evidence).text
    return found, details


@pytest.mark.parametrize("done", [None, "disputed", "contradicted"])
def test_a_row_due_later_says_how_its_date_stands_and_links_to_what_the_sources_say(
    tmp_path: pathlib.Path, done: str | None
) -> None:
    """A row due later, to do or reported done, shows its date as a card does: the date and
    its source when sources agree, the recorded date with a banner when they disagree or
    the school says otherwise, and the claim that couldn't be read. Nothing is chosen; the
    banner links to the evidence on that assignment's details."""
    rows, details = later_rows(tmp_path, done)
    evidence = {
        name: details_href(name, fragment="evidence", return_to="week", week=MONDAY)
        for name in ("disputed", "contradicted")
    }

    assert "Due Thursday, August 27" in rows["agreed"]
    assert '<span class="source">' in rows["agreed"].split("</p>", 1)[0]
    assert "Check this date." not in rows["agreed"]
    assert "Recorded date Thursday, August 27" in rows["disputed"]
    assert "Sources disagree about it, and nothing has been chosen for you." in rows["disputed"]
    assert "could not be read as a date" in rows["unread"]
    assert "Recorded date Thursday, August 27" in rows["contradicted"]
    assert "The school says otherwise." in rows["contradicted"]
    for name, title in (("disputed", "Disputed project"), ("contradicted", "Contradicted project")):
        assert (
            f'<a href="{html.escape(evidence[name])}" aria-label="See what the sources say: '
            f'{title}, Science">See what the sources say</a>.'
        ) in rows[name]
    assert all('href="#evidence"' not in row for row in rows.values())
    assert "August 28" not in rows["disputed"].split("Check this date.", 1)[0]
    assert '<section class="evidence" id="evidence" tabindex="-1">' in details
    assert "Disputed project" in details
