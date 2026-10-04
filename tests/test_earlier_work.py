# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Earlier unfinished work: homework due before today that she has not reported done, listed
on every week shown, each with a choice for today's plan. A choice makes the work catch-up
work a plan may schedule tonight or put off, with its dates as given; it lasts the household's
day, and the next day starts with none and marks yesterday's. Today is Saturday, October 3,
2026, throughout."""

import dataclasses
import pathlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from blossom.agent.compose import CATCH_UP, CATCH_UP_PUT_OFF
from blossom.app import create_app
from blossom.noticing import read_week
from blossom.plans import DailyPlan, Deferral
from blossom.reconciliation import SourceChannel
from blossom.routes.runs import plan_graphs
from blossom.routes.student import (
    CHOICE_BAD_FORM,
    CHOICE_FROM_ANOTHER_DAY,
    NOT_EARLIER_NOW,
    NOT_HERS_TO_CHOOSE,
    earlier_action,
)
from blossom.settings import ANTHROPIC_API_KEY_VARIABLE as KEY_VARIABLE
from blossom.stores.catch_up import ChoiceMade, ChoiceStood
from blossom.stores.project_state import Assignment, AssignmentKind, ProjectStateStore
from tests.support import (
    HER_PAGE,
    PAGE_HEADERS,
    SAME_ORIGIN,
    THEIRS,
    Scripted,
    SetClock,
    accepting,
    client_for,
    due,
    fixture_clock,
    fixture_settings,
    form_fields,
    human_text,
    plan_block,
    record,
    scripted_graphs,
    signed_in,
    signed_in_household,
    state_of,
    store_of,
    with_clock,
    words,
)

TODAY = date(2026, 10, 3)
TOMORROW = date(2026, 10, 4)
NOON = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
OCT_2 = due("earlier-oct-2", "Fractions practice", date(2026, 10, 2))
OCT_3 = due("due-oct-3", "Reading log", date(2026, 10, 3))
OCT_9 = due("due-oct-9", "Poem draft", date(2026, 10, 9))
OCT_10 = due("due-oct-10", "Science poster", date(2026, 10, 10))
UNDATED = Assignment(
    assignment_id="undated-work",
    course="Art",
    title="Sketchbook page",
    due_date=None,
    dependencies=[],
    reported_submission_status="not_started",
    kind=AssignmentKind.HOMEWORK,
)
BOTH_PAST = due("both-past", "Map quiz review", date(2026, 10, 1))
"""Recorded October 1; the school portal says September 30. Every date is before today."""
PAST_AND_AHEAD = due("past-and-ahead", "Lab write-up", date(2026, 10, 5))
"""Recorded October 5; the school portal says October 1. One date has passed."""
SECTION = "Earlier unfinished work"


@contextmanager
def household(
    *assignments: Assignment,
    plans: list[DailyPlan] | None = None,
    briefs: list[Scripted[DailyPlan]] | None = None,
) -> Iterator[TestClient]:
    """Her page on October 3 with only ``assignments`` on record, the dates the school portal
    gives for two of them, and scripted models that answer with ``plans``."""
    settings = fixture_settings(
        BLOSSOM_TODAY=TODAY.isoformat(),
        BLOSSOM_FIXTURE_PATH="",
        **{KEY_VARIABLE: "not-a-key-and-never-sent"},
    )
    app = create_app(settings)
    app.dependency_overrides[plan_graphs] = scripted_graphs(
        lambda: list(plans or []), lambda: [accepting()] * 3, planners=briefs
    )
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        put_on_record(store_of(client), assignments)
        yield client


def put_on_record(store: ProjectStateStore, assignments: tuple[Assignment, ...]) -> None:
    claims = {
        BOTH_PAST.assignment_id: [record(SourceChannel.LMS, "2026-09-30")],
        PAST_AND_AHEAD.assignment_id: [record(SourceChannel.LMS, "2026-10-01")],
    }
    named = {item.assignment_id for item in assignments}
    store.put_on_record(
        list(assignments), {name: said for name, said in claims.items() if name in named}
    )


def page(client: TestClient, **params: str) -> str:
    return client.get(HER_PAGE, params=params, headers=PAGE_HEADERS).text


def section(html: str) -> str:
    """The Earlier unfinished work section, or nothing when the page has none."""
    start = html.find('id="earlier-work"')
    if start < 0:
        return ""
    return html[start : html.index("</section>", start)]


def item_of(html: str, assignment_id: str) -> str:
    listed = section(html)
    start = listed.index(f'id="earlier-{assignment_id}"')
    return listed[start : listed.index("</li>", start)]


def choose(client: TestClient, assignment_id: str, **params: str) -> str:
    """Press the item's button as the page shows it; the address it is answered with."""
    shown = page(client, **params)
    action = earlier_action(assignment_id)
    answer = client.post(action, data=form_fields(shown, action), headers=PAGE_HEADERS)
    assert answer.status_code == 303, answer.text
    return answer.headers["location"]


def planned_ids(client: TestClient, day: date = TODAY) -> list[str]:
    """What a plan for ``day`` would be made from, as the run reads it."""
    store = store_of(client)
    return [item.assignment_id for item in read_week(store, store, day).active()]


def kept_rows(client: TestClient) -> list[tuple[str, str]]:
    connection: sqlite3.Connection = store_of(client)._connection
    return sorted(connection.execute("SELECT * FROM catch_up_choices").fetchall())


# ------------------------------------------------------------------ what is earlier work


def test_work_due_yesterday_is_listed_and_planned_only_once_she_includes_it() -> None:
    with household(OCT_2, OCT_3) as client:
        before = page(client)
        planned_before = planned_ids(client)
        landed = choose(client, OCT_2.assignment_id)
        after = client.get(landed, headers=PAGE_HEADERS).text
        planned_after = planned_ids(client)
        week = read_week(store_of(client), store_of(client), TODAY)

    assert SECTION in section(before)
    assert "Was due Friday, October 2." in words(item_of(before, OCT_2.assignment_id))
    assert "Include in today's plan" in item_of(before, OCT_2.assignment_id)
    assert planned_before == [OCT_3.assignment_id]
    assert landed.endswith(f"#earlier-{OCT_2.assignment_id}")
    assert "Included in today's plan." in words(item_of(after, OCT_2.assignment_id))
    assert "Chosen for today" in item_of(after, OCT_2.assignment_id)
    assert "Remove from today's plan" in item_of(after, OCT_2.assignment_id)
    assert planned_after == [OCT_3.assignment_id, OCT_2.assignment_id]
    assert week.catch_up == {OCT_2.assignment_id}


def test_today_the_next_week_and_undated_work_are_not_earlier_work() -> None:
    with household(OCT_3, OCT_9, OCT_10, UNDATED) as client:
        shown = page(client)
        planned = planned_ids(client)

    assert section(shown) == ""
    assert planned == [OCT_3.assignment_id, OCT_9.assignment_id, UNDATED.assignment_id]
    assert OCT_10.assignment_id not in planned


def test_dates_that_differ_say_so_and_one_still_to_come_keeps_work_in_the_window() -> None:
    with household(BOTH_PAST, PAST_AND_AHEAD) as client:
        shown = page(client)
        planned = planned_ids(client)

    listed = item_of(shown, BOTH_PAST.assignment_id)
    assert "Was due Wednesday, September 30." in words(listed)
    assert "The dates given for it differ; Details shows each one." in words(listed)
    assert f'id="earlier-{PAST_AND_AHEAD.assignment_id}"' not in section(shown)
    assert planned == [PAST_AND_AHEAD.assignment_id]


def test_done_takes_chosen_work_out_of_planning_and_off_the_list_at_once() -> None:
    with household(OCT_2, OCT_3) as client:
        choose(client, OCT_2.assignment_id)
        store_of(client).report_status(
            OCT_2.assignment_id, "done", None, expected_head=None, now=NOON, today=TODAY
        )
        shown = page(client)
        planned = planned_ids(client)

    assert planned == [OCT_3.assignment_id]
    assert section(shown) == ""


# ------------------------------------------------------------------ the day a choice is for


def test_a_choice_lasts_the_day_and_the_next_day_marks_it_chosen_yesterday() -> None:
    with household(OCT_2, OCT_3) as client:
        before = store_of(client).all_assignments()
        choose(client, OCT_2.assignment_id)
        refreshed = page(client, refreshed="1")
        planned_today = planned_ids(client)
        with_clock(client, SetClock(TOMORROW, NOON))
        next_day = page(client)
        planned_tomorrow = planned_ids(client, TOMORROW)
        after = store_of(client).all_assignments()
        reports = store_of(client).student_reports(OCT_2.assignment_id)

    assert "Chosen for today" in item_of(refreshed, OCT_2.assignment_id)
    assert OCT_2.assignment_id in planned_today
    listed = item_of(next_day, OCT_2.assignment_id)
    assert "Chosen yesterday" in listed
    assert "You chose this for yesterday's plan. Choose it again to include it today." in words(
        listed
    )
    assert "Include in today's plan" in listed
    assert OCT_2.assignment_id not in planned_tomorrow
    assert after == before
    assert reports == []


def test_a_choice_made_while_viewing_another_week_is_for_today() -> None:
    with household(OCT_2, OCT_3) as client:
        other = page(client, week="2026-09-21")
        landed = choose(client, OCT_2.assignment_id, week="2026-09-21")
        planned = planned_ids(client)
        rows = kept_rows(client)

    assert "Include in today's plan" in item_of(other, OCT_2.assignment_id)
    assert "week=2026-09-21" in landed
    assert rows == [(TODAY.isoformat(), OCT_2.assignment_id)]
    assert planned == [OCT_3.assignment_id, OCT_2.assignment_id]


def test_a_parent_reads_her_choices_and_meets_no_control(tmp_path: pathlib.Path) -> None:
    settings = dataclasses.replace(signed_in_household(tmp_path), today=TODAY, fixture_path=None)
    with client_for(settings) as client:
        put_on_record(store_of(client), (OCT_2, OCT_3))
        store_of(client).choose_catch_up(OCT_2.assignment_id, TODAY, include=True)
        signed_in(client, THEIRS)
        shown = page(client)
        pressed = client.post(
            earlier_action(OCT_2.assignment_id),
            data={"choice": "remove", "made_with": "x", "week": ""},
            headers=PAGE_HEADERS,
        )
        rows = kept_rows(client)

    listed = section(shown)
    assert "Homework due before today that she hasn't reported done." in words(listed)
    assert "Chosen for today" in listed
    assert "<form" not in listed
    assert "you" not in words(listed).lower().split()
    assert pressed.status_code == 403
    assert NOT_HERS_TO_CHOOSE in words(pressed.text)
    assert rows == [(TODAY.isoformat(), OCT_2.assignment_id)]


# ------------------------------------------------------------------ stale and repeated presses


def test_a_page_from_another_day_is_refused_and_keeps_her_press() -> None:
    with household(OCT_2, OCT_3) as client:
        yesterday = page(client)
        with_clock(client, SetClock(TOMORROW, NOON))
        action = earlier_action(OCT_2.assignment_id)
        answer = client.post(action, data=form_fields(yesterday, action), headers=PAGE_HEADERS)
        rows = kept_rows(client)

    assert answer.status_code == 409
    listed = item_of(answer.text, OCT_2.assignment_id)
    assert CHOICE_FROM_ANOTHER_DAY in words(listed)
    assert "Include in today's plan" in listed
    assert rows == []


def test_the_same_press_twice_keeps_one_choice_and_says_so() -> None:
    with household(OCT_2, OCT_3) as client:
        shown = page(client)
        action = earlier_action(OCT_2.assignment_id)
        fields = form_fields(shown, action)
        first = client.post(action, data=fields, headers=PAGE_HEADERS)
        again = client.post(action, data=fields, headers=PAGE_HEADERS)
        landed = client.get(again.headers["location"], headers=PAGE_HEADERS).text
        rows = kept_rows(client)

    assert first.headers["location"].count("earlier_said=included") == 1
    assert "earlier_said=already_included" in again.headers["location"]
    assert "This was already in today's plan." in words(item_of(landed, OCT_2.assignment_id))
    assert rows == [(TODAY.isoformat(), OCT_2.assignment_id)]


@pytest.mark.parametrize(
    "over",
    [{"made_with": "2026-10-03.0000000000000000"}, {"choice": "remove"}, {"extra": "1"}],
    ids=["unsigned", "another choice than the page offered", "a field the page doesn't send"],
)
def test_a_form_no_page_made_changes_nothing(over: dict[str, str]) -> None:
    with household(OCT_2, OCT_3) as client:
        shown = page(client)
        action = earlier_action(OCT_2.assignment_id)
        answer = client.post(
            action, data={**form_fields(shown, action), **over}, headers=PAGE_HEADERS
        )
        rows = kept_rows(client)

    assert answer.status_code == 422
    assert CHOICE_BAD_FORM in words(answer.text)
    assert rows == []


def test_work_reported_done_since_the_page_was_made_cannot_be_chosen() -> None:
    with household(OCT_2, OCT_3) as client:
        shown = page(client)
        store_of(client).report_status(
            OCT_2.assignment_id, "done", None, expected_head=None, now=NOON, today=TODAY
        )
        action = earlier_action(OCT_2.assignment_id)
        answer = client.post(action, data=form_fields(shown, action), headers=PAGE_HEADERS)
        rows = kept_rows(client)

    assert answer.status_code == 409
    assert NOT_EARLIER_NOW in words(answer.text)
    assert rows == []


# ------------------------------------------------------------------ the store


def test_the_choices_table_is_made_once_and_choices_stand_or_are_taken_back() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    store = ProjectStateStore(connection, fixture_clock())
    store.put_on_record([OCT_2], {})
    again = ProjectStateStore(connection, fixture_clock())

    made = again.choose_catch_up(OCT_2.assignment_id, TODAY, include=True)
    stood = again.choose_catch_up(OCT_2.assignment_id, TODAY, include=True)
    chosen = again.catch_up_choices()
    taken_back = again.choose_catch_up(OCT_2.assignment_id, TODAY, include=False)
    left = again.catch_up_choices()

    assert isinstance(made, ChoiceMade)
    assert isinstance(stood, ChoiceStood)
    assert chosen == {TODAY: frozenset({OCT_2.assignment_id})}
    assert isinstance(taken_back, ChoiceMade)
    assert left == {}
    assert again.all_assignments() == [OCT_2]


# ------------------------------------------------------------------ planning with catch-up work


def two_tonight() -> DailyPlan:
    return DailyPlan(
        plan_date=TODAY,
        blocks=[
            plan_block(OCT_3.assignment_id, "16:30", "17:00"),
            plan_block(OCT_2.assignment_id, "17:15", "17:45", "it was due yesterday"),
        ],
    )


def one_put_off() -> DailyPlan:
    return DailyPlan(
        plan_date=TODAY,
        blocks=[plan_block(OCT_3.assignment_id, "16:30", "17:00")],
        deferred=[Deferral(assignment_id=OCT_2.assignment_id, reason="It doesn't fit tonight.")],
    )


def test_chosen_catch_up_work_makes_a_valid_plan_with_its_date_kept() -> None:
    briefs: list[Scripted[DailyPlan]] = []
    with household(OCT_2, OCT_3, plans=[two_tonight()], briefs=briefs) as client:
        choose(client, OCT_2.assignment_id)
        made = client.post("/student/actions/plan", headers=PAGE_HEADERS)
        shown = page(client)
        saved = state_of(client).drafts.latest_for(TODAY)
        due_now = next(
            item for item in store_of(client).all_assignments() if item == OCT_2
        ).due_date

    assert made.status_code == 303, made.text
    brief = human_text(briefs[0].briefs[0])
    assert f'id="{OCT_2.assignment_id}"' in brief
    assert 'catch_up="due before today"' in brief
    assert 'due="2026-10-02"' in brief
    assert saved is not None
    assert saved.outcome == "accepted"
    assert CATCH_UP in saved.body
    assert "Fractions practice (Geometry, due Oct 2)" in saved.body
    assert "Today's plan includes it." in item_of(shown, OCT_2.assignment_id)
    assert due_now == date(2026, 10, 2)


def test_a_plan_that_puts_off_chosen_work_says_so_and_what_she_can_do() -> None:
    with household(OCT_2, OCT_3, plans=[one_put_off()]) as client:
        choose(client, OCT_2.assignment_id)
        client.post("/student/actions/plan", headers=PAGE_HEADERS)
        shown = page(client)
        saved = state_of(client).drafts.latest_for(TODAY)

    assert saved is not None
    assert CATCH_UP_PUT_OFF in saved.body
    listed = words(item_of(shown, OCT_2.assignment_id))
    assert "Today's plan puts it off: It doesn't fit tonight." in listed
    assert "It stays on this list, so you can choose it again tomorrow, or ask for help." in listed


def only_tonight() -> DailyPlan:
    return DailyPlan(plan_date=TODAY, blocks=[plan_block(OCT_3.assignment_id, "16:30", "17:00")])


def test_choosing_after_a_plan_was_made_says_to_plan_again() -> None:
    with household(OCT_2, OCT_3, plans=[only_tonight()]) as client:
        client.post("/student/actions/plan", headers=PAGE_HEADERS)
        choose(client, OCT_2.assignment_id)
        shown = page(client)

    listed = words(item_of(shown, OCT_2.assignment_id))
    assert "Today's plan was made before you chose it. Plan again to include it." in listed
