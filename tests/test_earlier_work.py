# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Earlier homework to check: homework due before today that she has not reported done, listed
on every week shown, each with a choice for today's plan. Ten from the last fourteen days show,
newest first, with her choices for today and yesterday; the rest are in a fold that says how
many. A choice makes the work catch-up
work a plan may schedule tonight or put off, with its dates as given; it lasts the household's
day, and the next day starts with none and marks yesterday's. Today is Saturday, October 3,
2026, throughout."""

import dataclasses
import json
import pathlib
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from blossom.agent.compose import CATCH_UP, CATCH_UP_PUT_OFF
from blossom.app import create_app
from blossom.assignment_status import statuses_for
from blossom.noticing import PLANNING_DIGEST, canonical_active_input, planning_digest, read_week
from blossom.plans import DailyPlan, Deferral
from blossom.reconciliation import SourceChannel
from blossom.routes.runs import plan_graphs
from blossom.routes.student import (
    BEYOND_THE_CALENDAR,
    CHOICE_BAD_FORM,
    CHOICE_FAILED,
    CHOICE_FROM_ANOTHER_DAY,
    NOT_A_WEEK,
    NOT_EARLIER_NOW,
    NOT_HERS_TO_CHOOSE,
    NOT_ON_RECORD,
    earlier_action,
    earlier_anchor,
    earlier_made_with,
    place_key,
)
from blossom.settings import ANTHROPIC_API_KEY_VARIABLE as KEY_VARIABLE
from blossom.stores.catch_up import ChoiceMade, ChoiceNotSaved, ChoiceStood
from blossom.stores.project_state import Assignment, AssignmentKind, ProjectStateStore
from tests.support import (
    HER_PAGE,
    HERS,
    NOT_UTF8,
    PAGE_HEADERS,
    SAME_ORIGIN,
    THEIRS,
    Scripted,
    SetClock,
    accepting,
    card_for,
    client_for,
    due,
    fixture_clock,
    fixture_settings,
    form_fields,
    human_text,
    plan_block,
    record,
    report,
    save,
    scripted_graphs,
    signed_in,
    signed_in_household,
    spoil,
    state_of,
    store_of,
    week_card,
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
PAST_AND_LATER = (
    due("later-oct-20", "Book report", date(2026, 10, 20)),
    due("later-oct-21", "Spelling list", date(2026, 10, 21)),
    due("past-oct-2", "Science notes", date(2026, 10, 2)),
)
"""Recorded October 20, October 21, and October 2; the school portal says October 1 for the
first two and October 20 for the last. One date has passed and one is after the window."""
SECTION = "Earlier homework to check"


@contextmanager
def household(
    *assignments: Assignment,
    plans: list[DailyPlan] | None = None,
    briefs: list[Scripted[DailyPlan]] | None = None,
) -> Iterator[TestClient]:
    """Her page on October 3 with only ``assignments`` on record, the dates the school portal
    gives for some of them, and scripted models that answer with ``plans``."""
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
        PAST_AND_LATER[0].assignment_id: [record(SourceChannel.LMS, "2026-10-01")],
        PAST_AND_LATER[1].assignment_id: [record(SourceChannel.LMS, "2026-10-01")],
        PAST_AND_LATER[2].assignment_id: [record(SourceChannel.LMS, "2026-10-20")],
    }
    named = {item.assignment_id for item in assignments}
    store.put_on_record(
        list(assignments), {name: said for name, said in claims.items() if name in named}
    )


def page(client: TestClient, **params: str) -> str:
    return client.get(HER_PAGE, params=params, headers=PAGE_HEADERS).text


def section(html: str) -> str:
    """The Earlier homework to check section, or nothing when the page has none."""
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
    assert "Added to your choices for today." in words(item_of(after, OCT_2.assignment_id))
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
    assert "Homework due before today with no Done update from her." in words(listed)
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


def pressed_across_midnight(
    client: TestClient,
    clock: SetClock,
    assignment_id: str,
    shown: str,
    meanwhile: Callable[[], object] = lambda: None,
) -> tuple[int, str]:
    """Press the item's button on ``shown`` while a decision holds the lock, and let the
    household day turn to October 4, and ``meanwhile`` happen, while the press waits for it."""
    state = state_of(client)
    portal = client.portal
    assert portal is not None
    action = earlier_action(assignment_id)
    fields = form_fields(shown, action)
    portal.call(state.decision_lock.acquire)
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            pressed = pool.submit(client.post, action, data=fields, headers=PAGE_HEADERS)
            deadline = time.monotonic() + 5
            while not portal.call(lambda: bool(state.decision_lock._waiters)):
                assert time.monotonic() < deadline, "the press never waited for the lock"
                time.sleep(0.01)
            clock.day = TOMORROW
            meanwhile()
        finally:
            portal.call(state.decision_lock.release)
        answer = pressed.result(timeout=10)
    return answer.status_code, answer.text


@pytest.mark.parametrize("pressed", ["include", "remove"])
def test_a_press_that_waits_for_the_lock_past_midnight_is_refused_and_changes_nothing(
    pressed: str,
) -> None:
    with household(OCT_2, OCT_3) as client:
        clock = SetClock(TODAY, NOON)
        with_clock(client, clock)
        if pressed == "remove":
            choose(client, OCT_2.assignment_id)
        before = kept_rows(client)
        code, text = pressed_across_midnight(client, clock, OCT_2.assignment_id, page(client))
        after = kept_rows(client)
        not_planned = planned_ids(client, TOMORROW)
        landing = page(client)
        fresh = choose(client, OCT_2.assignment_id)
        planned = planned_ids(client, TOMORROW)

    assert code == 409
    assert CHOICE_FROM_ANOTHER_DAY in words(item_of(text, OCT_2.assignment_id))
    assert "Include in today's plan" in item_of(text, OCT_2.assignment_id)
    assert after == before
    assert OCT_2.assignment_id not in not_planned
    assert "Include in today's plan" in item_of(landing, OCT_2.assignment_id)
    assert said_in(fresh).startswith("included.")
    assert OCT_2.assignment_id in planned


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
    assert "This was already in your choices for today." in words(
        item_of(landed, OCT_2.assignment_id)
    )
    assert rows == [(TODAY.isoformat(), OCT_2.assignment_id)]


@pytest.mark.parametrize(
    "over",
    [
        {"made_with": "2026-10-03.0000000000000000"},
        {"choice": "remove"},
        {"extra": "1"},
        {"place": "elsewhere"},
    ],
    ids=[
        "unsigned",
        "another choice than the page offered",
        "a field the page doesn't send",
        "a place the page doesn't name",
    ],
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


# ------------------------------------------------------------------ how much of it shows


def days_ago(count: int) -> date:
    return date.fromordinal(TODAY.toordinal() - count)


def due_days_ago(*counts: int) -> tuple[Assignment, ...]:
    """Geometry homework due ``count`` days before today, one for each count."""
    return tuple(
        due(f"ago-{count:02d}", f"Set from {count} days ago", days_ago(count)) for count in counts
    )


def shown_ids(html: str) -> list[str]:
    """The items of the section above its fold, in the order shown."""
    listed = section(html)
    fold = listed.find("<details")
    return re.findall(r'<li id="earlier-([^"]+)"', listed if fold < 0 else listed[:fold])


def folded_ids(html: str) -> list[str]:
    """The items inside the section's fold, in the order shown."""
    listed = section(html)
    fold = listed.find("<details")
    return [] if fold < 0 else re.findall(r'<li id="earlier-([^"]+)"', listed[fold:])


def the_fold(html: str) -> str:
    """The fold's opening tag."""
    listed = section(html)
    start = listed.index("<details")
    return listed[start : listed.index(">", start) + 1]


def test_day_fourteen_shows_and_day_fifteen_is_folded_with_its_count() -> None:
    with household(*due_days_ago(1, 14, 15)) as client:
        shown = page(client)

    assert shown_ids(shown) == ["ago-01", "ago-14"]
    assert folded_ids(shown) == ["ago-15"]
    assert "<summary>More earlier homework (1)</summary>" in section(shown)
    assert " open" not in the_fold(shown)


def test_the_tenth_item_shows_and_the_eleventh_is_folded_newest_first() -> None:
    with household(*due_days_ago(*range(1, 12))) as client:
        shown = page(client)

    assert shown_ids(shown) == [f"ago-{count:02d}" for count in range(1, 11)]
    assert folded_ids(shown) == ["ago-11"]
    assert "<summary>More earlier homework (1)</summary>" in section(shown)


def test_ten_or_fewer_from_the_last_fourteen_days_need_no_fold() -> None:
    with household(*due_days_ago(*range(1, 11))) as client:
        shown = page(client)

    assert len(shown_ids(shown)) == 10
    assert "<details" not in section(shown)


def test_choices_for_today_and_yesterday_show_once_whatever_their_age() -> None:
    recent = due_days_ago(*range(1, 12))
    old = due_days_ago(30, 40, 50)
    with household(*recent, *old) as client:
        store = store_of(client)
        store.choose_catch_up("ago-30", TODAY, include=True)
        store.choose_catch_up("ago-40", date(2026, 10, 2), include=True)
        store.choose_catch_up("ago-02", TODAY, include=True)
        store.choose_catch_up("ago-11", date(2026, 10, 2), include=True)
        shown = page(client)

    assert shown_ids(shown) == [
        *[f"ago-{count:02d}" for count in range(1, 12)],
        "ago-30",
        "ago-40",
    ]
    assert folded_ids(shown) == ["ago-50"]
    assert "<summary>More earlier homework (1)</summary>" in section(shown)
    every = shown_ids(shown) + folded_ids(shown)
    assert len(every) == len(set(every)) == 14
    assert "Chosen yesterday" in item_of(shown, "ago-40")
    assert "Include in today's plan" in item_of(shown, "ago-40")


def test_a_press_in_the_fold_lands_on_its_item_with_the_fold_open() -> None:
    with household(*due_days_ago(*range(1, 12)), *due_days_ago(30)) as client:
        before = page(client)
        landed = choose(client, "ago-30")
        after = client.get(landed, headers=PAGE_HEADERS).text
        later = page(client)
        removed = choose(client, "ago-30")
        after_removing = client.get(removed, headers=PAGE_HEADERS).text

    assert "ago-30" in folded_ids(before)
    assert landed.endswith("#earlier-ago-30")
    assert "ago-30" in folded_ids(after)
    assert the_fold(after).endswith(" open>")
    assert "Added to your choices for today." in words(item_of(after, "ago-30"))
    assert "ago-30" in shown_ids(later)
    assert "ago-30" in shown_ids(after_removing)
    assert "Removed from your choices for today." in words(item_of(after_removing, "ago-30"))


def test_a_refused_press_in_the_fold_is_said_there_with_the_fold_open() -> None:
    with household(*due_days_ago(*range(1, 12)), *due_days_ago(30)) as client:
        yesterday = page(client)
        with_clock(client, SetClock(TOMORROW, NOON))
        action = earlier_action("ago-30")
        answer = client.post(action, data=form_fields(yesterday, action), headers=PAGE_HEADERS)

    assert answer.status_code == 409
    assert "ago-30" in folded_ids(answer.text)
    assert the_fold(answer.text).endswith(" open>")
    assert CHOICE_FROM_ANOTHER_DAY in words(item_of(answer.text, "ago-30"))


def test_a_press_that_waits_past_midnight_is_refused_for_its_day_before_anything_else() -> None:
    with household(OCT_2, OCT_3) as client:
        clock = SetClock(TODAY, NOON)
        with_clock(client, clock)
        code, text = pressed_across_midnight(
            client,
            clock,
            OCT_2.assignment_id,
            page(client),
            lambda: store_of(client).report_status(
                OCT_2.assignment_id, "done", None, expected_head=None, now=NOON, today=TOMORROW
            ),
        )
        rows = kept_rows(client)

    assert code == 409
    assert CHOICE_FROM_ANOTHER_DAY in words(text)
    assert NOT_EARLIER_NOW not in words(text)
    assert rows == []


def test_a_press_in_the_fold_that_waits_past_midnight_is_refused_there_with_the_fold_open() -> None:
    with household(*due_days_ago(*range(1, 12)), *due_days_ago(30)) as client:
        clock = SetClock(TODAY, NOON)
        with_clock(client, clock)
        code, text = pressed_across_midnight(client, clock, "ago-30", page(client))
        rows = kept_rows(client)

    assert code == 409
    assert "ago-30" in folded_ids(text)
    assert the_fold(text).endswith(" open>")
    assert CHOICE_FROM_ANOTHER_DAY in words(item_of(text, "ago-30"))
    assert rows == []


def test_the_fold_is_the_pages_own_and_reading_it_writes_nothing() -> None:
    with household(*due_days_ago(*range(1, 12)), *due_days_ago(30)) as client:
        store = store_of(client)
        store.choose_catch_up("ago-02", TODAY, include=True)
        before = (kept_rows(client), store.all_assignments(), store.student_reports("ago-30"))
        shown = page(client)
        opened = page(client, earlier="ago-30", earlier_said="included")
        after = (kept_rows(client), store.all_assignments(), store.student_reports("ago-30"))

    fold = section(shown)[section(shown).index("<details") :]
    summary = fold[: fold.index("</summary>")]
    assert "<form" not in summary
    assert "<a " not in summary
    assert the_fold(opened).endswith(" open>")
    assert after == before


def test_each_item_says_not_yet_or_no_update_yet_under_its_heading() -> None:
    with household(OCT_2, BOTH_PAST) as client:
        store_of(client).report_status(
            OCT_2.assignment_id, "not_yet", None, expected_head=None, now=NOON, today=TODAY
        )
        shown = page(client)

    assert "Earlier homework to check</h2>" in section(shown)
    assert "Earlier unfinished work" not in shown
    assert '<span class="pill">Not yet</span>' in item_of(shown, OCT_2.assignment_id)
    assert "No update yet" not in item_of(shown, OCT_2.assignment_id)
    assert '<span class="pill">No update yet</span>' in item_of(shown, BOTH_PAST.assignment_id)


def test_a_parent_reads_the_fold_and_the_labels_with_no_control(tmp_path: pathlib.Path) -> None:
    settings = dataclasses.replace(signed_in_household(tmp_path), today=TODAY, fixture_path=None)
    with client_for(settings) as client:
        put_on_record(store_of(client), due_days_ago(*range(1, 12)))
        signed_in(client, THEIRS)
        shown = page(client, earlier="ago-11", earlier_said="included")

    listed = section(shown)
    assert folded_ids(shown) == ["ago-11"]
    assert "<summary>More earlier homework (1)</summary>" in listed
    assert '<span class="pill">No update yet</span>' in item_of(shown, "ago-11")
    assert "<form" not in listed
    assert "Added to your choices for today." not in words(listed)


def test_her_place_holds_through_presses_until_her_next_visit() -> None:
    with household(*due_days_ago(*range(1, 12)), *due_days_ago(30)) as client:
        landed = client.get(choose(client, "ago-30"), headers=PAGE_HEADERS).text
        action = earlier_action("ago-30")
        answer = client.post(action, data=form_fields(landed, action), headers=PAGE_HEADERS)
        removed = client.get(answer.headers["location"], headers=PAGE_HEADERS).text
        chosen_again = choose(client, "ago-30")
        from_the_list = choose(client, "ago-02")
        listed = client.get(from_the_list, headers=PAGE_HEADERS).text
        rows = kept_rows(client)

    assert "earlier_place=fold" in answer.headers["location"]
    assert "ago-30" in folded_ids(removed)
    assert the_fold(removed).endswith(" open>")
    assert "Removed from your choices for today." in words(item_of(removed, "ago-30"))
    assert "earlier_place=fold" in chosen_again
    assert "earlier_place=list" in from_the_list
    assert "ago-02" in shown_ids(listed)
    assert " open" not in the_fold(listed)
    assert rows == [(TODAY.isoformat(), "ago-02"), (TODAY.isoformat(), "ago-30")]


# ------------------------------------------------------------------ dates on both sides of today


@pytest.mark.parametrize(
    "work", PAST_AND_LATER, ids=["recorded Oct 20", "recorded Oct 21", "recorded Oct 2"]
)
def test_work_with_a_date_after_the_window_is_not_earlier_work(work: Assignment) -> None:
    """One date has passed and another is after today's window: it is listed in a later
    week, never here, and can't be chosen or planned as catch-up work."""
    with household(OCT_3, work) as client:
        shown = page(client)
        action = earlier_action(work.assignment_id)
        made_with = earlier_made_with(
            state_of(client).result_key, TODAY, work.assignment_id, "include"
        )
        fields = {"choice": "include", "made_with": made_with, "week": "", "place": "list"}
        answer = client.post(action, data=fields, headers=PAGE_HEADERS)
        rows = kept_rows(client)
        store_of(client).choose_catch_up(work.assignment_id, TODAY, include=True)
        planned = planned_ids(client)

    assert f'id="earlier-{work.assignment_id}"' not in section(shown)
    assert answer.status_code == 409
    assert NOT_EARLIER_NOW in words(answer.text)
    assert rows == []
    assert planned == [OCT_3.assignment_id]


# ------------------------------------------------------------------ the plan's fingerprint

INSTRUCTIONS_SHAPE = uuid.UUID("6f1ef033-6648-4683-b6e9-1dd419f420b5")
"""The namespace of the shape before catch-up work was marked."""


def test_the_fingerprint_has_a_namespace_of_its_own_for_catch_up_work() -> None:
    assert uuid.UUID("4fcf0d3f-3042-4144-8b89-5b608d49e134") == PLANNING_DIGEST
    assert PLANNING_DIGEST not in (
        INSTRUCTIONS_SHAPE,
        uuid.UUID("7d1e6a34-2c9b-4f58-a0d7-93b5e1c8f264"),
    )


def test_a_plan_waiting_from_before_catch_up_work_reads_as_changed_once() -> None:
    with household(OCT_2, OCT_3, plans=[only_tonight(), only_tonight()]) as client:
        client.post("/student/actions/plan", headers=PAGE_HEADERS)
        made = state_of(client).drafts.latest_for(TODAY)
        assert made is not None
        store = store_of(client)
        week = read_week(store, store, TODAY)
        shape = json.dumps(
            canonical_active_input(week), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        before = uuid.uuid5(INSTRUCTIONS_SHAPE, shape).hex
        fresh = client.get("/parent", headers=PAGE_HEADERS).text
        drafts = state_of(client).drafts
        drafts._connection.execute(
            "UPDATE drafts SET inputs_digest=? WHERE draft_id=?", (before, made.draft_id)
        )
        drafts._connection.commit()
        behind = client.get("/parent", headers=PAGE_HEADERS).text
        client.post("/student/actions/plan", headers=PAGE_HEADERS)
        again = client.get("/parent", headers=PAGE_HEADERS).text

    assert week.catch_up == frozenset()
    assert before != planning_digest(week)
    assert "<strong>Plan again.</strong>" not in fresh
    assert "<strong>Plan again.</strong>" in behind
    assert "<strong>Plan again.</strong>" not in again


# ------------------------------------------------------------------ what a press says

SAID = (
    "Added to your choices for today.",
    "Removed from your choices for today.",
    "This was already in your choices for today.",
    "This was already out of your choices for today.",
)


def said_in(location: str) -> str:
    found = re.search("earlier_said=([^&#]*)", location)
    assert found is not None, location
    return found.group(1)


def receipts(html: str, assignment_id: str) -> list[str]:
    """What the page says a press did, beside the item."""
    listed = section(html)
    start = listed.index(f'id="{earlier_anchor(assignment_id)}"')
    item = words(listed[start : listed.index("</li>", start)])
    return [said for said in SAID if said in item]


@pytest.mark.parametrize("word", ["included", "removed", "already_included", "already_removed"])
def test_an_address_no_press_made_says_nothing_about_a_choice(word: str) -> None:
    with household(OCT_2, OCT_3) as client:
        shown = page(client, earlier=OCT_2.assignment_id, earlier_said=word)
        rows = kept_rows(client)

    assert receipts(shown, OCT_2.assignment_id) == []
    assert "Include in today's plan" in item_of(shown, OCT_2.assignment_id)
    assert rows == []


def test_a_press_says_what_it_did_only_while_its_choice_still_holds() -> None:
    with household(OCT_2, OCT_3) as client:
        included = choose(client, OCT_2.assignment_id)
        right_after = client.get(included, headers=PAGE_HEADERS).text
        action = earlier_action(OCT_2.assignment_id)
        fields = form_fields(right_after, action)
        removed = client.post(action, data=fields, headers=PAGE_HEADERS).headers["location"]
        after_removal = client.get(removed, headers=PAGE_HEADERS).text
        twice = client.post(action, data=fields, headers=PAGE_HEADERS).headers["location"]
        removed_twice = client.get(twice, headers=PAGE_HEADERS).text
        inclusion_again = client.get(included, headers=PAGE_HEADERS).text
        choose(client, OCT_2.assignment_id)
        removal_again = client.get(removed, headers=PAGE_HEADERS).text

    assert receipts(right_after, OCT_2.assignment_id) == ["Added to your choices for today."]
    assert receipts(after_removal, OCT_2.assignment_id) == ["Removed from your choices for today."]
    assert receipts(removed_twice, OCT_2.assignment_id) == [
        "This was already out of your choices for today."
    ]
    assert receipts(inclusion_again, OCT_2.assignment_id) == []
    assert receipts(removal_again, OCT_2.assignment_id) == []


@pytest.mark.parametrize(
    "change",
    [
        "another item",
        "another word",
        "no signature",
        "a signature one short",
        "a signature one long",
        "the form's signature",
    ],
)
def test_a_receipt_changed_in_any_way_says_nothing(change: str) -> None:
    with household(OCT_2, BOTH_PAST, OCT_3) as client:
        store_of(client).choose_catch_up(BOTH_PAST.assignment_id, TODAY, include=True)
        given = said_in(choose(client, OCT_2.assignment_id))
        word, _, signature = given.partition(".")
        form = earlier_made_with(state_of(client).result_key, TODAY, OCT_2.assignment_id, "include")
        about, said = {
            "another item": (BOTH_PAST.assignment_id, given),
            "another word": (OCT_2.assignment_id, f"already_included.{signature}"),
            "no signature": (OCT_2.assignment_id, word),
            "a signature one short": (OCT_2.assignment_id, given[:-1]),
            "a signature one long": (OCT_2.assignment_id, f"{given}0"),
            "the form's signature": (OCT_2.assignment_id, form),
        }[change]
        shown = page(client, earlier=about, earlier_said=said)
        genuine = page(client, earlier=OCT_2.assignment_id, earlier_said=given)

    assert word == "included"
    assert receipts(shown, about) == []
    assert receipts(genuine, OCT_2.assignment_id) == ["Added to your choices for today."]


def test_a_receipt_from_another_day_says_nothing() -> None:
    with household(OCT_2, OCT_3) as client:
        landed = choose(client, OCT_2.assignment_id)
        with_clock(client, SetClock(TOMORROW, NOON))
        store_of(client).choose_catch_up(OCT_2.assignment_id, TOMORROW, include=True)
        shown = client.get(landed, headers=PAGE_HEADERS).text

    assert "Chosen for today" in item_of(shown, OCT_2.assignment_id)
    assert receipts(shown, OCT_2.assignment_id) == []


class TurnsAfterOneRead(SetClock):
    """A household clock that reads ``day`` once and ``then`` after that, as when midnight
    falls between two reads in one request."""

    def __init__(self, day: date, then: date) -> None:
        super().__init__(day, NOON)
        self.then = then

    def today(self) -> date:
        day, self.day = self.day, self.then
        return day


@pytest.mark.parametrize("week", [None, "2026-09-21"])
@pytest.mark.parametrize(
    ("presses", "said"),
    [(1, "Added to your choices for today."), (2, "Removed from your choices for today.")],
)
def test_a_receipt_and_the_page_it_is_on_are_about_one_day(
    presses: int, said: str, week: str | None
) -> None:
    with household(OCT_2, OCT_3) as client:
        for _ in range(presses):
            landed = choose(client, OCT_2.assignment_id, **({} if week is None else {"week": week}))
        store = store_of(client)
        if presses == 1:
            store.choose_catch_up(OCT_2.assignment_id, TOMORROW, include=True)
        before = (kept_rows(client), store.all_assignments())
        with_clock(client, TurnsAfterOneRead(TODAY, TOMORROW))
        shown = client.get(landed, headers=PAGE_HEADERS).text
        after = (kept_rows(client), store.all_assignments())

    assert ("week=" in landed) == (week is not None)
    assert ("Today, Saturday, October 3" in shown) == (week is None)
    assert earlier_anchor(OCT_3.assignment_id) not in section(shown)
    assert receipts(shown, OCT_2.assignment_id) == [said]
    assert after == before


def signed_days(html: str) -> set[str]:
    """The days the page's earlier homework forms were made for."""
    return set(re.findall(r'name="made_with" value="(\d{4}-\d{2}-\d{2})\.', html))


def about_today_only(html: str, week: str | None = None) -> None:
    """The page is about October 3 alone, the day the request read first."""
    assert signed_days(html) == {TODAY.isoformat()}
    if week is None:
        assert "Today, Saturday, October 3" in html
    assert "Sunday, October 4" not in html


@pytest.mark.parametrize(
    ("week", "problem"),
    [
        ("not-a-week", NOT_A_WEEK),
        ("2026-10-33", NOT_A_WEEK),
        ("0001-01-01", BEYOND_THE_CALENDAR),
        ("9999-12-31", BEYOND_THE_CALENDAR),
    ],
    ids=["not a date", "no such day", "the first week", "the last week"],
)
@pytest.mark.parametrize("receipt", [False, True], ids=["plain", "with a receipt"])
def test_a_week_the_page_cannot_show_is_answered_for_the_day_the_request_read(
    week: str, problem: str, receipt: bool
) -> None:
    with household(OCT_2, OCT_3, BOTH_PAST) as client:
        asked = f"{HER_PAGE}?week={week}"
        if receipt:
            landed = choose(client, OCT_2.assignment_id).partition("#")[0]
            asked = f"{landed}&week={week}"
        with_clock(client, TurnsAfterOneRead(TODAY, TOMORROW))
        answer = client.get(asked, headers=PAGE_HEADERS)

    assert answer.status_code == 422
    assert problem in words(answer.text)
    about_today_only(answer.text)


def refused_choice(client: TestClient, shown: str, refusal: str) -> tuple[int, str]:
    """Press Include or Remove on ``shown`` with the one change that ``refusal`` names,
    while the household day turns to October 4 after its first read."""
    key = state_of(client).result_key
    store = store_of(client)
    about, choice = OCT_2.assignment_id, "include"
    fields = form_fields(shown, earlier_action(about))
    if refusal == "made on another day":
        fields["made_with"] = earlier_made_with(key, date(2026, 10, 2), about, choice)
    elif refusal == "reported done since":
        store.report_status(about, "done", None, expected_head=None, now=NOON, today=TODAY)
    elif refusal in ("include work not on record", "remove work not on record"):
        about, choice = "gone-oct-1", refusal.split()[0]
        fields |= {"choice": choice, "made_with": earlier_made_with(key, TODAY, about, choice)}
    elif refusal == "a malformed form":
        fields["place"] = "elsewhere"
    else:
        error: Exception = sqlite3.OperationalError("disk I/O error")
        if refusal == "a write the file refuses":
            error = ChoiceNotSaved("synthetic")

        def fails(*_: object, **__: object) -> ChoiceMade:
            raise error

        store.choose_catch_up = fails  # type: ignore[method-assign]
    with_clock(client, TurnsAfterOneRead(TODAY, TOMORROW))
    answer = client.post(earlier_action(about), data=fields, headers=PAGE_HEADERS)
    return answer.status_code, answer.text


@pytest.mark.parametrize("week", [None, "2026-09-21"], ids=["today's week", "another week"])
@pytest.mark.parametrize(
    ("refusal", "code", "problem"),
    [
        ("made on another day", 409, CHOICE_FROM_ANOTHER_DAY),
        ("reported done since", 409, NOT_EARLIER_NOW),
        ("include work not on record", 404, NOT_ON_RECORD),
        ("remove work not on record", 404, NOT_ON_RECORD),
        ("a store that cannot be read", 500, CHOICE_FAILED),
        ("a write the file refuses", 500, CHOICE_FAILED),
        ("a malformed form", 422, CHOICE_BAD_FORM),
    ],
    ids=lambda value: value if isinstance(value, str) and len(value) < 30 else "",
)
def test_a_refused_choice_is_shown_for_the_day_it_was_checked_against(
    refusal: str, code: int, problem: str, week: str | None
) -> None:
    with household(OCT_2, OCT_3, BOTH_PAST) as client:
        shown = page(client, **({} if week is None else {"week": week}))
        before = kept_rows(client)
        answer, text = refused_choice(client, shown, refusal)
        after = kept_rows(client)

    assert answer == code
    assert problem in words(text)
    about_today_only(text, week)
    assert after == before


@pytest.mark.parametrize("assignment_id", ["it's-due", "two words", "café-5", "a&b=c", "100%"])
def test_a_press_on_work_with_any_id_says_what_it_did(assignment_id: str) -> None:
    odd = due(assignment_id, "Fractions practice", date(2026, 10, 2))
    with household(odd, OCT_3) as client:
        landed = choose(client, assignment_id)
        shown = client.get(landed, headers=PAGE_HEADERS).text
        rows = kept_rows(client)

    assert receipts(shown, assignment_id) == ["Added to your choices for today."]
    assert rows == [(TODAY.isoformat(), assignment_id)]


# ------------------------------------------------------------------ after her Not yet, on the card

THIS_WEEK = "2026-09-28"


def test_her_not_yet_on_earlier_work_offers_include_and_lands_back_on_the_card() -> None:
    with household(OCT_2, OCT_3) as client:
        landed = client.get(
            report(client, OCT_2.assignment_id, "not_yet", week=THIS_WEEK), headers=PAGE_HEADERS
        ).text
        card = card_for(landed, OCT_2.assignment_id)
        action = earlier_action(OCT_2.assignment_id)
        answer = client.post(action, data=form_fields(card, action), headers=PAGE_HEADERS)
        after = client.get(answer.headers["location"], headers=PAGE_HEADERS).text
        rows = kept_rows(client)

    assert "Saved as Not yet. This was due before today." in words(card)
    assert "Include in today's plan" in card
    assert answer.status_code == 303
    assert answer.headers["location"].endswith(f"#earlier-card-{OCT_2.assignment_id}")
    assert "week=" not in answer.headers["location"]
    chosen = words(card_for(after, OCT_2.assignment_id))
    assert "Saved as Not yet. This was due before today. You chose it for today's plan." in chosen
    assert "Added to your choices for today." in chosen
    assert f'id="earlier-card-{OCT_2.assignment_id}"' in card_for(after, OCT_2.assignment_id)
    assert "Include in today's plan" not in card_for(after, OCT_2.assignment_id)
    assert "Still unfinished" not in chosen
    assert "Added to your choices for today." not in words(item_of(after, OCT_2.assignment_id))
    assert "Chosen for today" in item_of(after, OCT_2.assignment_id)
    assert rows == [(TODAY.isoformat(), OCT_2.assignment_id)]


def test_the_details_say_whether_she_chose_earlier_work_and_link_to_the_list() -> None:
    details = f"/student/assignments/{OCT_2.assignment_id}"
    with household(OCT_2, OCT_3) as client:
        report(client, OCT_2.assignment_id, "not_yet", week=THIS_WEEK)
        before = client.get(details, headers=PAGE_HEADERS).text
        linked = client.get(f"{HER_PAGE}?earlier={OCT_2.assignment_id}", headers=PAGE_HEADERS).text
        store_of(client).choose_catch_up(OCT_2.assignment_id, TODAY, include=True)
        after = client.get(details, headers=PAGE_HEADERS).text

    assert (
        "Saved as Not yet. This was due before today. Include it in today's plan from Earlier "
        "homework to check."
    ) in words(before)
    assert f'href="/student/due-this-week?earlier={OCT_2.assignment_id}#earlier-' in before
    assert "Include in today's plan" in item_of(linked, OCT_2.assignment_id)
    assert ("Saved as Not yet. This was due before today. You chose it for today's plan.") in words(
        after
    )
    assert "Include it in today's plan" not in after


# ------------------------------------------------------------------ cards kept in place

REPORTED_DONE = '<details class="steps reported-done"'


def done_from_the_card(client: TestClient, assignment_id: str) -> str:
    """Her Done saved from a card of this week, as its form sends it; the page it lands on."""
    answer = save(client, week_card(client, assignment_id), "done", assignment_id=assignment_id)
    assert answer.status_code == 303, answer.status_code
    return client.get(answer.headers["location"], headers=PAGE_HEADERS).text


@pytest.mark.parametrize("where", ["list", "card"])
def test_an_include_keeps_the_cards_this_visit_keeps_in_place(where: str) -> None:
    with household(OCT_2, OCT_3) as client:
        if where == "card":
            report(client, OCT_2.assignment_id, "not_yet", week=THIS_WEEK)
        kept = done_from_the_card(client, OCT_3.assignment_id)
        action = earlier_action(OCT_2.assignment_id)
        pressed = card_for(kept, OCT_2.assignment_id) if where == "card" else section(kept)
        fields = form_fields(pressed, action)
        answer = client.post(action, data=fields, headers=PAGE_HEADERS)
        landed = client.get(answer.headers["location"], headers=PAGE_HEADERS).text
        fresh = page(client)

    assert REPORTED_DONE not in kept
    assert f"a:{place_key(OCT_3.assignment_id)}" in fields["in_place"].split("|")
    assert "landing=" in answer.headers["location"]
    assert REPORTED_DONE not in landed
    assert "Added to your choices for today." in words(landed)
    assert REPORTED_DONE in fresh


# ------------------------------------------------------------------ what today's plan still holds


def decided(client: TestClient, decision: str) -> None:
    """Approve today's waiting plan through the family's form, or leave it waiting."""
    if decision == "waiting":
        return
    saved = state_of(client).drafts.latest_for(TODAY)
    assert saved is not None
    answer = client.post(
        f"/parent/actions/decide/{saved.draft_id}",
        data={"decision": decision},
        headers=PAGE_HEADERS,
    )
    assert answer.status_code == 303, answer.text


@pytest.mark.parametrize("decision", ["waiting", "approve"])
def test_removing_work_todays_plan_schedules_says_the_plan_still_has_it_until_she_plans_again(
    decision: str,
) -> None:
    with household(OCT_2, OCT_3, plans=[two_tonight(), only_tonight()]) as client:
        choose(client, OCT_2.assignment_id)
        client.post("/student/actions/plan", headers=PAGE_HEADERS)
        decided(client, decision)
        published = state_of(client).drafts.latest_for(TODAY)
        shown = page(client)
        action = earlier_action(OCT_2.assignment_id)
        fields = form_fields(shown, action)
        first = client.post(action, data=fields, headers=PAGE_HEADERS)
        again = client.post(action, data=fields, headers=PAGE_HEADERS)
        removed = client.get(first.headers["location"], headers=PAGE_HEADERS).text
        removed_twice = client.get(again.headers["location"], headers=PAGE_HEADERS).text
        assert published is not None
        kept = state_of(client).drafts.get(published.draft_id)
        replanned = client.post("/student/actions/plan", headers=PAGE_HEADERS)
        fresh = page(client)
        latest = state_of(client).drafts.latest_for(TODAY)
        due_now = next(
            item for item in store_of(client).all_assignments() if item == OCT_2
        ).due_date
        status = statuses_for(store_of(client), [OCT_2.assignment_id])[OCT_2.assignment_id]
        planned = planned_ids(client)
        rows = kept_rows(client)

    assert fields["choice"] == "remove"
    for landed, said in [
        (removed, "Removed from your choices for today."),
        (removed_twice, "This was already out of your choices for today."),
    ]:
        listed = words(item_of(landed, OCT_2.assignment_id))
        assert said in listed
        assert "Today's plan still includes it. Plan again to leave it out." in listed
        assert "Removed from today's plan" not in listed
        assert "Chosen for today" not in listed
    assert kept is not None
    assert (kept.body, kept.status) == (published.body, published.status)
    assert "Fractions practice" in kept.body
    assert replanned.status_code == 303, replanned.text
    assert latest is not None
    assert latest.draft_id != published.draft_id
    assert "Fractions practice" not in latest.body
    assert "Today's plan" not in words(item_of(fresh, OCT_2.assignment_id))
    assert planned == [OCT_3.assignment_id]
    assert due_now == date(2026, 10, 2)
    assert status.status is None
    assert rows == []


def test_removing_work_todays_plan_puts_off_keeps_the_reason_and_says_the_next_plan_leaves_it() -> (
    None
):
    with household(OCT_2, OCT_3, plans=[one_put_off()]) as client:
        choose(client, OCT_2.assignment_id)
        client.post("/student/actions/plan", headers=PAGE_HEADERS)
        removed = client.get(choose(client, OCT_2.assignment_id), headers=PAGE_HEADERS).text

    listed = words(item_of(removed, OCT_2.assignment_id))
    assert (
        "Today's plan puts it off: It doesn't fit tonight. Your next plan leaves it out." in listed
    )
    assert "choose it again tomorrow" not in listed


@pytest.mark.parametrize("snapshot", [None, "not a snapshot"], ids=["absent", "unreadable"])
def test_a_plan_whose_rows_cannot_be_read_names_no_place_for_earlier_work(
    snapshot: str | None,
) -> None:
    with household(OCT_2, OCT_3, plans=[one_put_off()]) as client:
        choose(client, OCT_2.assignment_id)
        client.post("/student/actions/plan", headers=PAGE_HEADERS)
        drafts = state_of(client).drafts
        saved = drafts.latest_for(TODAY)
        assert saved is not None
        drafts._connection.execute(
            "UPDATE drafts SET plan_snapshot = ? WHERE draft_id = ?", (snapshot, saved.draft_id)
        )
        drafts._connection.commit()
        chosen = page(client)
        removed = client.get(choose(client, OCT_2.assignment_id), headers=PAGE_HEADERS).text

    listed = words(item_of(chosen, OCT_2.assignment_id))
    assert "Today's plan was made with it; the plan says where it goes." in listed
    assert "Today's plan includes it." not in listed
    assert "puts it off" not in listed
    taken_out = words(item_of(removed, OCT_2.assignment_id))
    assert "Today's plan was made with it. Plan again to leave it out." in taken_out


@pytest.mark.parametrize(
    ("planned", "readable", "said"),
    [
        (two_tonight, True, "Today's plan still includes it; her next plan leaves it out."),
        (
            one_put_off,
            True,
            "Today's plan puts it off: It doesn't fit tonight. Her next plan leaves it out.",
        ),
        (two_tonight, False, "Today's plan was made with it; her next plan leaves it out."),
    ],
    ids=["scheduled", "put off", "text only"],
)
def test_a_parent_reads_what_todays_plan_still_holds_and_updates_that_cannot_be_read(
    tmp_path: pathlib.Path, planned: Callable[[], DailyPlan], readable: bool, said: str
) -> None:
    settings = dataclasses.replace(signed_in_household(tmp_path), today=TODAY, fixture_path=None)
    app = create_app(settings)
    app.dependency_overrides[plan_graphs] = scripted_graphs(
        lambda: [planned()], lambda: [accepting()] * 3
    )
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        store = store_of(client)
        put_on_record(store, (OCT_2, OCT_3, BOTH_PAST))
        store.report_status(
            BOTH_PAST.assignment_id, "not_yet", None, expected_head=None, now=NOON, today=TODAY
        )
        spoil(store, "student_reports", "status", "finished", BOTH_PAST.assignment_id)
        signed_in(client, HERS)
        choose(client, OCT_2.assignment_id)
        made = client.post("/student/actions/plan", headers=PAGE_HEADERS)
        if not readable:
            drafts = state_of(client).drafts
            drafts._connection.execute("UPDATE drafts SET plan_snapshot = NULL")
            drafts._connection.commit()
        choose(client, OCT_2.assignment_id)
        signed_in(client, THEIRS)
        shown = page(client)

    assert made.status_code == 303, made.text
    listed = words(item_of(shown, OCT_2.assignment_id))
    assert said in listed
    assert "Plan again" not in listed
    assert "Your next plan" not in listed
    unread = words(item_of(shown, BOTH_PAST.assignment_id))
    assert "Her updates on this assignment can't be read right now." in unread
    assert "No update yet" not in unread
    intro = "Homework due before today with no Done update from her, or whose updates can't be read"
    assert intro in words(section(shown))


@pytest.mark.parametrize(
    ("column", "value"),
    [("status", "finished"), ("note", NOT_UTF8)],
    ids=["a status that is neither", "text that is not UTF-8"],
)
def test_earlier_work_whose_updates_cannot_be_read_says_so_and_stays_on_the_list(
    column: str, value: str
) -> None:
    with household(OCT_2, BOTH_PAST, OCT_3) as client:
        store = store_of(client)
        store.report_status(
            OCT_2.assignment_id,
            "not_yet",
            "starting soon",
            expected_head=None,
            now=NOON,
            today=TODAY,
        )
        healthy = page(client)
        spoil(store, "student_reports", column, value, OCT_2.assignment_id)
        shown = page(client)
        choose(client, OCT_2.assignment_id)
        planned = planned_ids(client)

    unread = item_of(shown, OCT_2.assignment_id)
    assert "Your updates on this assignment can't be read right now." in words(unread)
    assert "No update yet" not in unread
    assert "Not yet" not in unread
    assert '<span class="pill">No update yet</span>' in item_of(shown, BOTH_PAST.assignment_id)
    assert "or whose updates can't be read right now" in words(section(shown))
    assert "can't be read" not in words(section(healthy))
    assert OCT_2.assignment_id in planned


def unreadable_claim(store: ProjectStateStore, assignment_id: str) -> None:
    """One more date claim about the assignment, which then cannot be read."""
    store.record_claims(assignment_id, [record(SourceChannel.LMS, "2026-10-06")])
    with store._lock, store._connection:
        store._connection.execute(
            "UPDATE date_claims SET active = 7 WHERE rowid = "
            "(SELECT MAX(rowid) FROM date_claims WHERE assignment_id = ?)",
            (assignment_id,),
        )


def test_earlier_work_with_a_date_claim_that_cannot_be_read_is_not_listed_chosen_or_planned() -> (
    None
):
    """A claim that can't be read could put the work anywhere, so the readable dates alone
    never make it earlier work."""
    with household(OCT_2, BOTH_PAST, OCT_3) as client:
        store = store_of(client)
        store.choose_catch_up(OCT_2.assignment_id, TODAY, include=True)
        before = page(client)
        unreadable_claim(store, OCT_2.assignment_id)
        shown = page(client)
        planned = planned_ids(client)
        store.choose_catch_up(OCT_2.assignment_id, TODAY, include=False)
        made_with = earlier_made_with(
            state_of(client).result_key, TODAY, OCT_2.assignment_id, "include"
        )
        fields = {"choice": "include", "made_with": made_with, "week": "", "place": "list"}
        answer = client.post(earlier_action(OCT_2.assignment_id), data=fields, headers=PAGE_HEADERS)
        rows = kept_rows(client)

    assert f'id="earlier-{OCT_2.assignment_id}"' in section(before)
    assert f'id="earlier-{OCT_2.assignment_id}"' not in section(shown)
    assert f'id="earlier-{BOTH_PAST.assignment_id}"' in section(shown)
    assert planned == [OCT_3.assignment_id]
    assert answer.status_code == 409
    assert NOT_EARLIER_NOW in words(answer.text)
    assert rows == []


def test_old_work_todays_plan_still_holds_shows_above_the_fold_after_she_takes_it_out() -> None:
    old, older = due_days_ago(30, 31)
    plan = DailyPlan(plan_date=TODAY, blocks=[plan_block(old.assignment_id, "16:30", "17:00")])
    with household(old, older, plans=[plan]) as client:
        choose(client, old.assignment_id)
        client.post("/student/actions/plan", headers=PAGE_HEADERS)
        choose(client, old.assignment_id)
        fresh = page(client)

    assert shown_ids(fresh) == [old.assignment_id]
    assert folded_ids(fresh) == [older.assignment_id]
    listed = words(item_of(fresh, old.assignment_id))
    assert "Today's plan still includes it. Plan again to leave it out." in listed


def test_work_chosen_yesterday_that_todays_plan_still_holds_is_not_offered_again() -> None:
    with household(OCT_2, OCT_3, plans=[two_tonight()]) as client:
        store_of(client).choose_catch_up(OCT_2.assignment_id, days_ago(1), include=True)
        choose(client, OCT_2.assignment_id)
        client.post("/student/actions/plan", headers=PAGE_HEADERS)
        removed = client.get(choose(client, OCT_2.assignment_id), headers=PAGE_HEADERS).text

    listed = words(item_of(removed, OCT_2.assignment_id))
    assert "You chose this for yesterday's plan." in listed
    assert "Choose it again to include it today." not in listed
    assert "Today's plan still includes it. Plan again to leave it out." in listed


def test_including_after_a_plan_was_made_says_it_was_added_to_her_choices() -> None:
    with household(OCT_2, OCT_3, plans=[only_tonight()]) as client:
        client.post("/student/actions/plan", headers=PAGE_HEADERS)
        landed = client.get(choose(client, OCT_2.assignment_id), headers=PAGE_HEADERS).text

    listed = words(item_of(landed, OCT_2.assignment_id))
    assert "Added to your choices for today." in listed
    assert "Today's plan was made before you chose it. Plan again to include it." in listed
    assert "Included in today's plan" not in listed


@pytest.mark.parametrize(
    ("reason", "said"),
    [
        ("not enough time tonight", ": not enough time tonight."),
        ("Too long for tonight!", ": Too long for tonight!"),
        ("Could it wait?", ": Could it wait?"),
        ("It can wait (due Monday)", ": It can wait (due Monday)."),
        ('She said "it can wait."', ': She said "it can wait."'),
        ("It can wait\u2026", ": It can wait\u2026"),
        ("\u4eca\u591c\u306f\u7121\u7406\u3002", ": \u4eca\u591c\u306f\u7121\u7406\u3002"),
        ("\u200b", "."),
    ],
)
def test_a_reason_the_plan_gives_ends_its_sentence_before_the_next_one(
    reason: str, said: str
) -> None:
    put_off = DailyPlan(
        plan_date=TODAY,
        blocks=[plan_block(OCT_3.assignment_id, "16:30", "17:00")],
        deferred=[Deferral(assignment_id=OCT_2.assignment_id, reason=reason)],
    )
    with household(OCT_2, OCT_3, plans=[put_off]) as client:
        choose(client, OCT_2.assignment_id)
        client.post("/student/actions/plan", headers=PAGE_HEADERS)
        chosen = page(client)
        removed = client.get(choose(client, OCT_2.assignment_id), headers=PAGE_HEADERS).text

    assert f"Today's plan puts it off{said} It stays on this list" in words(
        item_of(chosen, OCT_2.assignment_id)
    )
    assert f"Today's plan puts it off{said} Your next plan leaves it out." in words(
        item_of(removed, OCT_2.assignment_id)
    )


# ------------------------------------------------------------------ dates no plan can keep to


ONLY_PAST = UNDATED.model_copy(update={"assignment_id": "undated-only-past", "title": "Atlas page"})
"""Nothing on record for its date; the school portal says October 1, which has passed."""


def tonight_with_the_lab_put_off(*catch_up: Assignment) -> DailyPlan:
    return DailyPlan(
        plan_date=TODAY,
        blocks=[
            plan_block(OCT_3.assignment_id, "16:30", "17:00"),
            *(
                plan_block(item.assignment_id, "17:15", "17:45", "it was due earlier")
                for item in catch_up
            ),
        ],
        deferred=[Deferral(assignment_id=PAST_AND_AHEAD.assignment_id, reason="Due Monday.")],
    )


def test_earlier_work_and_a_passed_date_still_held_to_a_later_one_are_planned_with_the_model() -> (
    None
):
    """On October 3, before she chooses it, the October 2 work stays out of what the planner
    is given; once chosen it is planned as catch-up work. The lab whose portal date passed is
    held to its record date, October 5, and the planner is told its date passed. Each press
    asks the model and neither run ends as a date problem."""
    briefs: list[Scripted[DailyPlan]] = []
    plans = iter([[tonight_with_the_lab_put_off()], [tonight_with_the_lab_put_off(OCT_2)]])
    with household(OCT_2, OCT_3, PAST_AND_AHEAD, briefs=briefs) as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: next(plans), lambda: [accepting()] * 3, planners=briefs
        )
        before = client.post("/student/actions/plan", headers=PAGE_HEADERS)
        choose(client, OCT_2.assignment_id)
        after = client.post("/student/actions/plan", headers=PAGE_HEADERS)
        ended = state_of(client).drafts.runs_without_a_draft()
        saved = state_of(client).drafts.latest_for(TODAY)

    assert (before.status_code, after.status_code) == (303, 303)
    assert [planner.calls for planner in briefs] == [1, 1]
    first, second = (human_text(planner.briefs[0]) for planner in briefs)
    assert f'id="{OCT_2.assignment_id}"' not in first
    assert f'id="{OCT_2.assignment_id}"' in second
    assert 'catch_up="due before today"' in second
    for brief in (first, second):
        assert 'date_passed="2026-10-01"' in brief
    assert ended == []
    assert saved is not None
    assert saved.outcome == "accepted"


def test_undated_work_whose_only_school_date_passed_ends_the_press_before_any_model() -> None:
    """With no date on record, the work is always in today's window and never earlier work
    to choose; its only date, the portal's October 1, has passed and no date is still to
    come, so every plan would fail the deadline check over it. The press names it and its
    day, links to its dates, and asks no model."""
    briefs: list[Scripted[DailyPlan]] = []
    with household(OCT_3, ONLY_PAST, briefs=briefs) as client:
        store_of(client).record_claims(
            ONLY_PAST.assignment_id, [record(SourceChannel.LMS, "2026-10-01")]
        )
        shown = page(client)
        answer = client.post("/student/actions/plan", headers=PAGE_HEADERS)
        ended = state_of(client).drafts.runs_without_a_draft()

    assert section(shown) == "" or ONLY_PAST.assignment_id not in section(shown)
    assert answer.status_code == 409
    assert (
        "Blossom can&#39;t make today&#39;s plan: Atlas page (Art, due October 1) has a due "
        "date that already passed, so no plan can finish it on time."
    ) in answer.text
    assert "Check the dates for Atlas page." in answer.text
    assert [planner.calls for planner in briefs] == [0]
    assert [run.outcome for run in ended] == ["date_problem"]
