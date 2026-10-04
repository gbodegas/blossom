# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Today's planning window, named by its days wherever a page speaks of it (spec v1.4 U-05,
T-U3). It is the household's day through six days later, the same on every week shown: under
Today's controls on this week, and under the link back to this week on another, where no
Today panel is added. A Not yet and a note added to homework say whether the work can be in
today's plan by the rule the planner reads: undated work is in, and a source's date inside
the window counts. A Not yet outside the window says whether its dates are all before today, all
after the window, or on both sides of it. Beside Plan today a sentence says what a plan is made
from. Another week says its updates are the latest saved, the details head their evidence as
what is on record now, and a parent reads the privacy fold about her."""

import pathlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.plans import DailyPlan
from blossom.reconciliation import SourceChannel
from blossom.routes.runs import plan_graphs
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    ESSAY_ID,
    FIXTURE_WEEK,
    HER_PAGE,
    HERS,
    KEY,
    PAGE_HEADERS,
    SAME_ORIGIN,
    THEIRS,
    Scripted,
    accepting,
    browser,
    card_for,
    client_for,
    due,
    files_in,
    fixture_settings,
    fixture_week_plan,
    hidden,
    record,
    report,
    reported,
    rules_named,
    scripted_graphs,
    signed_in,
    signed_in_household,
    state_of,
    store_of,
    words,
    work_listed,
)
from tests.support import QUIZ_ID as QUIZ
from tests.support import SYLLABUS_ID as SYLLABUS

WINDOW = "Today's planning window is August 19 to August 25, 2026."
OTHER_WEEK = "Updates show the latest saved information, even when you view a different week."
IN_THE_WINDOW = "Still unfinished. It can be included in today's plan."
LATER = "Saved as Not yet. This is due after August 25, so it isn't included in today's plan yet."
LATER_WITH_YEAR = (
    "Saved as Not yet. This is due after August 25, 2026, so it isn't included in today's plan yet."
)


def on_day(day: date, **environ: str) -> TestClient:
    """Her page with the household's day pinned to ``day``."""
    return TestClient(
        create_app(fixture_settings(BLOSSOM_TODAY=day.isoformat(), **environ)),
        follow_redirects=False,
        headers=SAME_ORIGIN,
    )


def poster(client: TestClient) -> str:
    """Homework a parent keeps from the inbox for September 10, well past the window."""
    kept = client.post(
        "/parent/inbox/keep", data={"course": "Art", "title": "Poster", "due_date": "2026-09-10"}
    )
    assert kept.status_code == 303
    return next(
        item.assignment_id
        for item in state_of(client).project_state.all_assignments()
        if item.title == "Poster"
    )


# ------------------------------------------------------------------ where the window is named


@pytest.mark.parametrize("finished", [False, True], ids=["work to plan", "nothing to plan"])
def test_this_week_names_the_window_once_under_todays_controls(finished: bool) -> None:
    with browser() as client:
        if finished:
            for item in state_of(client).project_state.all_assignments():
                report(client, item.assignment_id, "done")
        page = client.get(HER_PAGE, headers=PAGE_HEADERS).text
    today = page[page.index('id="today"') : page.index("</section>", page.index('id="today"'))]

    assert page.count(WINDOW) == 1
    assert today.index('<div class="actions">') < today.index(WINDOW)
    assert today.index(WINDOW) < today.index('class="support-links"')
    assert ("Nothing to schedule from the work in this planning window." in today) is finished


@pytest.mark.parametrize("week", ["2026-08-10", "2026-08-24", "2026-09-07"])
def test_another_week_names_todays_window_under_its_link_back_to_this_week(week: str) -> None:
    with browser() as client:
        page = client.get(HER_PAGE, params={"week": week}, headers=PAGE_HEADERS).text

    after_the_link = page.split("Return to this week and today's plan</a></p>")[1]

    assert page.count(WINDOW) == 1
    assert 'id="today"' not in page
    assert page.index(">This week</a>") < page.index(WINDOW)
    assert after_the_link.lstrip().startswith(f'<p class="note planning-window">{WINDOW}')
    assert page.index(WINDOW) < page.index('<h2 class="list-heading"')


def test_another_week_says_its_updates_are_the_latest_saved_above_its_cards() -> None:
    with browser() as client:
        later = client.get(HER_PAGE, params={"week": "2026-08-24"}, headers=PAGE_HEADERS).text
        this = client.get(HER_PAGE, headers=PAGE_HEADERS).text

    assert later.count(OTHER_WEEK) == 1
    assert later.index('<h2 class="list-heading"') < later.index(OTHER_WEEK)
    assert later.index(WINDOW) < later.index(OTHER_WEEK) < later.index('class="assignment')
    assert "Updates shown are the latest on record." not in later
    assert OTHER_WEEK not in this


@pytest.mark.parametrize(
    ("day", "window"),
    [
        (date(2026, 8, 28), "August 28 to September 3, 2026"),
        (date(2026, 12, 29), "December 29, 2026 to January 4, 2027"),
        (date(2027, 1, 1), "January 1 to January 7, 2027"),
    ],
    ids=["into the next month", "into the next year", "a new year's first day"],
)
def test_the_window_says_the_month_and_the_year_it_reaches(day: date, window: str) -> None:
    with on_day(day) as client:
        page = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        past = client.get(HER_PAGE, params={"week": "2026-08-17"}, headers=PAGE_HEADERS).text

    assert f"Today's planning window is {window}." in page
    assert f"Today's planning window is {window}." in past


# ------------------------------------------------------------------ what a Not yet says


@pytest.mark.parametrize(
    "assignment",
    [ESSAY_ID, SYLLABUS, QUIZ],
    ids=["recorded in the window", "undated", "a source date in the window"],
)
def test_a_not_yet_the_planner_would_take_can_be_included_in_todays_plan(assignment: str) -> None:
    with browser() as client:
        landed = client.get(report(client, assignment, "not_yet"), headers=PAGE_HEADERS).text
        details = client.get(f"/student/assignments/{assignment}", headers=PAGE_HEADERS).text

    assert IN_THE_WINDOW in card_for(landed, assignment)
    assert IN_THE_WINDOW in details
    assert "Saved as Not yet." not in card_for(landed, assignment)
    assert "Saved as Not yet." not in details


def test_a_not_yet_due_after_the_window_names_the_windows_last_day() -> None:
    with browser() as client:
        far = poster(client)
        page = client.get(HER_PAGE, params={"week": "2026-09-07"}, headers=PAGE_HEADERS).text
        answer = client.post(
            f"/student/actions/assignments/{far}/report",
            data={
                "status": "not_yet",
                "note": "",
                "expected_report_id": hidden(card_for(page, far), "expected_report_id"),
                "week": "2026-09-07",
            },
        )
        landed = client.get(answer.headers["location"], headers=PAGE_HEADERS).text
        details = client.get(f"/student/assignments/{far}", headers=PAGE_HEADERS).text

    assert LATER in card_for(landed, far)
    assert LATER_WITH_YEAR in details
    assert IN_THE_WINDOW not in card_for(landed, far)


# ------------------------------------------------------------------ the details and the fold


def test_the_details_head_their_evidence_as_what_is_on_record_now() -> None:
    with browser() as client:
        page = client.get(
            f"/student/assignments/{ESSAY_ID}?return_to=week&week={FIXTURE_WEEK}",
            headers=PAGE_HEADERS,
        ).text

    assert '<h2 class="update-heading">What is on record now</h2>' in page
    assert "Current assignment record." not in page
    assert "Updates shown are the latest on record." not in page


def test_a_parent_reads_the_privacy_fold_about_her(tmp_path: pathlib.Path) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        hers = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        theirs = client.get(HER_PAGE, headers=PAGE_HEADERS).text

    def fold(page: str) -> tuple[str, str]:
        summary = page.split("<summary>How Blossom uses ")[1].split("</summary>")[0]
        shared = page.split('<p id="what-is-shared">')[1].split("</p>")[0]
        return summary, " ".join(shared.split())

    her_summary, her_words = fold(hers)
    their_summary, their_words = fold(theirs)
    assert her_summary == "your information"
    assert their_summary == "her information"
    assert "It sends your week" in her_words
    assert "It sends her week" in their_words
    for word in (" you ", " your ", " you,", "You "):
        assert word not in f" {their_words} "
    assert "note about the work that she or a parent gave on that page" in their_words


# ------------------------------------------------------------------ a Saturday's window

SATURDAY = date(2026, 10, 3)
"""A Saturday, so her calendar week, September 28 to October 4, and today's planning window,
October 3 to October 9, are two different ranges."""
THIS_WEEK = "2026-09-28"
NEXT_WEEK = "2026-10-05"
SATURDAY_WINDOW = "Today's planning window is October 3 to October 9, 2026."
SCOPE = (
    "Makes a plan for today using unfinished homework due in the next 7 days and homework "
    "without a due date."
)
BEFORE = "Saved as Not yet. This was due before today. Earlier work isn't included in today's plan."
AFTER = "Saved as Not yet. This is due after October 9, so it isn't included in today's plan yet."
AFTER_WITH_YEAR = (
    "Saved as Not yet. This is due after October 9, 2026, so it isn't included in today's plan yet."
)
ASTRIDE = (
    "Saved as Not yet. Its dates disagree: one is before today and another is after October 9, "
    "so it isn't included in today's plan."
)
ASTRIDE_WITH_YEAR = (
    "Saved as Not yet. Its dates disagree: one is before today and another is after October 9, "
    "2026, so it isn't included in today's plan."
)
SATURDAY_OUTSIDE = (
    "Saved as Not yet. It is outside today's planning window (October 3 to October 9, 2026)."
)
EVERY_EFFECT = (
    IN_THE_WINDOW,
    BEFORE,
    AFTER,
    AFTER_WITH_YEAR,
    ASTRIDE,
    ASTRIDE_WITH_YEAR,
    SATURDAY_OUTSIDE,
)
HER_HELP = '<a href="#ask-for-help">Ask for help</a>'
HER_HELP_FROM_ELSEWHERE = '<a href="/student/due-this-week#ask-for-help">Ask for help</a>'

OCTOBER_2 = "assignment-due-october-2"
OCTOBER_3 = "assignment-due-october-3"
OCTOBER_9 = "assignment-due-october-9"
OCTOBER_10 = "assignment-due-october-10"
LAST_WEEK = "assignment-due-september-25"
EARLIER_BY_EVERY_DATE = "assignment-earlier-by-every-date"
IN_BY_A_SOURCE = "assignment-in-by-a-source"
DATES_ASTRIDE = "assignment-dates-astride"
DATE_UNREAD = "assignment-date-unread"


def saturday_work(store: ProjectStateStore) -> None:
    """Homework around the Saturday: due the day before, the day itself, the window's last
    day, the day after it, and the week before; and work whose sources give other dates."""
    store.upsert_assignments(
        [
            due(OCTOBER_2, "Proof practice", date(2026, 10, 2)),
            due(OCTOBER_3, "Angle worksheet", date(2026, 10, 3)),
            due(OCTOBER_9, "Chapter review", date(2026, 10, 9)),
            due(OCTOBER_10, "Test corrections", date(2026, 10, 10)),
            due(LAST_WEEK, "Ruler drawings", date(2026, 9, 25)),
            due(EARLIER_BY_EVERY_DATE, "Area problems", date(2026, 10, 1)),
            due(IN_BY_A_SOURCE, "Circle problems", date(2026, 10, 2)),
            due(DATES_ASTRIDE, "Triangle proofs", date(2026, 10, 2)),
            due(DATE_UNREAD, "Constructions", date(2026, 10, 2)),
        ]
    )
    store.record_claims(EARLIER_BY_EVERY_DATE, [record(SourceChannel.LMS, "2026-09-30")])
    store.record_claims(IN_BY_A_SOURCE, [record(SourceChannel.LMS, "2026-10-05")])
    store.record_claims(
        DATES_ASTRIDE,
        [record(SourceChannel.LMS, "2026-10-02"), record(SourceChannel.EMAIL, "2026-10-12")],
    )
    store.record_claims(DATE_UNREAD, [record(SourceChannel.EMAIL, "next Friday")])


@contextmanager
def on_saturday(**environ: str) -> Iterator[TestClient]:
    """Her page on the Saturday, with the work around it on record."""
    with on_day(SATURDAY, **environ) as client:
        saturday_work(store_of(client))
        yield client


def saturday_household(tmp_path: pathlib.Path, **environ: str) -> TestClient:
    """The Saturday with the sign-in on, its files under ``tmp_path``."""
    return client_for(
        fixture_settings(
            BLOSSOM_TODAY=SATURDAY.isoformat(),
            BLOSSOM_STUDENT_PASSPHRASE=HERS,
            BLOSSOM_PARENT_PASSPHRASE=THEIRS,
            **files_in(tmp_path),
            **environ,
        )
    )


def today_panel(page: str) -> str:
    """Today's panel on her week, whole."""
    start = page.index('id="today"')
    return page[start : page.index("</section>", start)]


def effects(piece: str) -> list[str]:
    """Which of the sentences about today's plan a piece of a page says."""
    return [said for said in EVERY_EFFECT if said in piece]


@pytest.mark.parametrize("reader", ["her", "a parent"])
def test_plan_today_says_what_a_plan_is_made_from_beside_it(
    reader: str, tmp_path: pathlib.Path
) -> None:
    with saturday_household(tmp_path, ANTHROPIC_API_KEY=KEY) as client:
        signed_in(client, HERS if reader == "her" else THEIRS)
        page = client.get(HER_PAGE, headers=PAGE_HEADERS).text
    today = today_panel(page)

    assert page.count(SCOPE) == 1
    assert f'<p class="note planning-window" id="plan-scope">{SCOPE} {SATURDAY_WINDOW}</p>' in today
    assert (
        '<button type="submit" class="primary" aria-describedby="plan-scope">Plan today</button>'
    ) in today
    assert today.index(">Plan today</button>") < today.index(SCOPE)
    assert today.index(SCOPE) < today.index('class="support-links"')


@pytest.mark.parametrize("where", ["no model", "another week", "nothing to plan"])
def test_the_scope_is_said_only_where_plan_today_is_offered(where: str) -> None:
    key = {} if where == "no model" else {"ANTHROPIC_API_KEY": KEY}
    with on_saturday(**key) as client:
        if where == "nothing to plan":
            store = store_of(client)
            for item in store.all_assignments():
                reported(store, "done", item.assignment_id)
        week = {"week": NEXT_WEEK} if where == "another week" else {}
        page = client.get(HER_PAGE, params=week, headers=PAGE_HEADERS).text

    assert 'action="/student/actions/plan"' not in page
    assert SCOPE not in page
    assert page.count(SATURDAY_WINDOW) == 1


@pytest.mark.parametrize(
    ("assignment", "week", "on_the_card", "on_the_details"),
    [
        (OCTOBER_2, THIS_WEEK, BEFORE, BEFORE),
        (LAST_WEEK, "2026-09-21", BEFORE, BEFORE),
        (EARLIER_BY_EVERY_DATE, THIS_WEEK, BEFORE, BEFORE),
        (OCTOBER_3, THIS_WEEK, IN_THE_WINDOW, IN_THE_WINDOW),
        (OCTOBER_9, NEXT_WEEK, IN_THE_WINDOW, IN_THE_WINDOW),
        (OCTOBER_10, NEXT_WEEK, AFTER, AFTER_WITH_YEAR),
        (SYLLABUS, THIS_WEEK, IN_THE_WINDOW, IN_THE_WINDOW),
        (IN_BY_A_SOURCE, THIS_WEEK, IN_THE_WINDOW, IN_THE_WINDOW),
        (DATES_ASTRIDE, THIS_WEEK, ASTRIDE, ASTRIDE_WITH_YEAR),
        (DATE_UNREAD, THIS_WEEK, SATURDAY_OUTSIDE, SATURDAY_OUTSIDE),
    ],
    ids=[
        "due the day before",
        "due the week before",
        "every date before today",
        "due today",
        "due on the window's last day",
        "due the day after the window",
        "undated",
        "a source date in the window",
        "dates before and after the window",
        "a date that cannot be read",
    ],
)
def test_on_a_saturday_a_not_yet_says_where_its_dates_put_it(
    assignment: str, week: str, on_the_card: str, on_the_details: str
) -> None:
    with on_saturday() as client:
        landed = client.get(report(client, assignment, "not_yet", week=week), headers=PAGE_HEADERS)
        details = client.get(f"/student/assignments/{assignment}", headers=PAGE_HEADERS).text

    assert effects(card_for(landed.text, assignment)) == [on_the_card]
    assert effects(details) == [on_the_details]


def test_earlier_work_is_said_to_be_left_out_of_todays_plan_with_her_way_to_help() -> None:
    with on_saturday() as client:
        this_week = client.get(report(client, OCTOBER_2, "not_yet", week=THIS_WEEK)).text
        last_week = client.get(report(client, LAST_WEEK, "not_yet", week="2026-09-21")).text
        details = client.get(f"/student/assignments/{OCTOBER_2}", headers=PAGE_HEADERS).text

    assert f"{BEFORE} {HER_HELP}" in card_for(this_week, OCTOBER_2)
    assert 'id="ask-for-help"' in this_week
    assert f"{BEFORE} {HER_HELP_FROM_ELSEWHERE}" in card_for(last_week, LAST_WEEK)
    assert 'id="ask-for-help"' not in last_week
    assert f"{BEFORE} {HER_HELP_FROM_ELSEWHERE}" in details


def effect_line(piece: str) -> str:
    """The line of her update that says what it means for today's plan."""
    start = piece.index('<div class="update">')
    start = piece.index('<p class="effect">', start)
    return piece[start : piece.index("</p>", start)]


@pytest.mark.parametrize("signed", [False, True])
def test_her_way_to_help_from_earlier_work_keeps_its_press_area_inside_its_own_line(
    signed: bool, tmp_path: pathlib.Path
) -> None:
    """Ask for help ends the line just above Change and Undo, so its line grows to hold it."""
    with saturday_household(tmp_path) if signed else on_day(SATURDAY) as client:
        saturday_work(store_of(client))
        if signed:
            signed_in(client, HERS)
        this_week = client.get(report(client, OCTOBER_2, "not_yet", week=THIS_WEEK)).text
        last_week = client.get(report(client, LAST_WEEK, "not_yet", week="2026-09-21")).text
        details = client.get(f"/student/assignments/{OCTOBER_2}", headers=PAGE_HEADERS).text

    assert HER_HELP in effect_line(card_for(this_week, OCTOBER_2))
    assert HER_HELP_FROM_ELSEWHERE in effect_line(card_for(last_week, LAST_WEEK))
    assert HER_HELP_FROM_ELSEWHERE in effect_line(details)
    declared = [
        {line.strip() for line in rule.split("\n") if line.strip()}
        for rule in rules_named(".update .effect a")
    ]
    assert declared == [{"display: inline-block;", "padding: 0.8rem 0;", "margin: 0;"}]


@pytest.mark.parametrize(("assignment", "week"), [(OCTOBER_2, THIS_WEEK), (OCTOBER_10, NEXT_WEEK)])
def test_done_on_either_side_of_the_window_is_out_of_work_to_plan(
    assignment: str, week: str
) -> None:
    with on_saturday() as client:
        landed = client.get(report(client, assignment, "done", week=week), headers=PAGE_HEADERS)
        details = client.get(f"/student/assignments/{assignment}", headers=PAGE_HEADERS).text

    for piece in (card_for(landed.text, assignment), details):
        assert "This is out of work to plan. Your school record is separate." in piece
        assert effects(piece) == []


def test_a_parent_reads_each_not_yet_in_the_same_words_without_her_way_to_help(
    tmp_path: pathlib.Path,
) -> None:
    weeks = {
        OCTOBER_2: THIS_WEEK,
        OCTOBER_3: THIS_WEEK,
        OCTOBER_10: NEXT_WEEK,
        DATES_ASTRIDE: THIS_WEEK,
        DATE_UNREAD: THIS_WEEK,
    }
    with saturday_household(tmp_path) as client:
        saturday_work(store_of(client))
        signed_in(client, HERS)
        for assignment, week in weeks.items():
            report(client, assignment, "not_yet", week=week)
        client.post("/sign-out")
        signed_in(client, THEIRS)
        pages = {
            week: client.get(HER_PAGE, params={"week": week}, headers=PAGE_HEADERS).text
            for week in (THIS_WEEK, NEXT_WEEK)
        }
        details = {
            assignment: client.get(f"/student/assignments/{assignment}").text
            for assignment in weeks
        }

    said = {
        assignment: effects(card_for(pages[week], assignment)) for assignment, week in weeks.items()
    }
    assert said == {
        OCTOBER_2: [BEFORE],
        OCTOBER_3: [IN_THE_WINDOW],
        OCTOBER_10: [AFTER],
        DATES_ASTRIDE: [ASTRIDE],
        DATE_UNREAD: [SATURDAY_OUTSIDE],
    }
    assert effects(details[OCTOBER_2]) == [BEFORE]
    assert effects(details[OCTOBER_10]) == [AFTER_WITH_YEAR]
    assert f"{BEFORE} </p>" in " ".join(card_for(pages[THIS_WEEK], OCTOBER_2).split())
    assert f"{BEFORE} </p>" in " ".join(details[OCTOBER_2].split())
    for page in (*pages.values(), *details.values()):
        assert f"{BEFORE} <a" not in page


@pytest.mark.parametrize("week", ["2026-09-21", THIS_WEEK, NEXT_WEEK, "2026-10-12"])
def test_browsing_another_week_keeps_todays_window_and_what_each_card_says(week: str) -> None:
    with on_saturday() as client:
        for assignment, shown in (
            (OCTOBER_2, THIS_WEEK),
            (DATES_ASTRIDE, THIS_WEEK),
            (OCTOBER_9, NEXT_WEEK),
            (OCTOBER_10, NEXT_WEEK),
        ):
            report(client, assignment, "not_yet", week=shown)
        page = client.get(HER_PAGE, params={"week": week}, headers=PAGE_HEADERS).text
        current = client.get(HER_PAGE, headers=PAGE_HEADERS).text

    assert page.count(SATURDAY_WINDOW) == 1
    assert words(current.split('<p class="range">')[1].split("</p>")[0]) == (
        "September 28 to October 4, 2026"
    )
    expected = {
        THIS_WEEK: {OCTOBER_2: BEFORE, DATES_ASTRIDE: ASTRIDE},
        NEXT_WEEK: {OCTOBER_9: IN_THE_WINDOW, OCTOBER_10: AFTER},
        "2026-10-12": {DATES_ASTRIDE: ASTRIDE},
    }.get(week, {})
    for assignment, said in expected.items():
        assert effects(card_for(page, assignment)) == [said]


def test_a_plan_asked_for_after_browsing_another_week_reads_todays_window() -> None:
    """Circle problems is in today's window by its portal date, October 5, while its record
    says October 2, which has passed: no plan can keep to it, so the press names it and asks
    no model. Once the record takes the portal's date, the planner is asked about today's
    window and nothing else."""
    planners: list[Scripted[DailyPlan]] = []
    with on_saturday(ANTHROPIC_API_KEY=KEY) as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [fixture_week_plan()] * 3, lambda: [accepting()] * 3, planners=planners
        )
        client.get(HER_PAGE, params={"week": "2026-10-12"}, headers=PAGE_HEADERS)
        today = today_panel(client.get(HER_PAGE, headers=PAGE_HEADERS).text)
        named = client.post("/student/actions/plan", headers=PAGE_HEADERS)
        store_of(client).upsert_assignments(
            [due(IN_BY_A_SOURCE, "Circle problems", date(2026, 10, 5))]
        )
        client.post("/student/actions/plan", headers=PAGE_HEADERS)

    form = today[today.index('action="/student/actions/plan"') :]
    assert "<input" not in form[: form.index("</form>")]
    assert named.status_code == 409
    assert "Circle problems" in named.text
    assert "has a due date that already passed" in named.text
    assert planners[0].calls == 0
    listed = work_listed(planners[1].briefs[0])
    for planned in (OCTOBER_3, OCTOBER_9, SYLLABUS, IN_BY_A_SOURCE):
        assert planned in listed
    for left_out in (OCTOBER_2, OCTOBER_10, LAST_WEEK, EARLIER_BY_EVERY_DATE, DATES_ASTRIDE):
        assert left_out not in listed
