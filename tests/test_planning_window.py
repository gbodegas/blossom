"""Today's planning window, named by its days wherever a page speaks of it (spec v1.4 U-05,
T-U3). It is the household's day through six days later, the same on every week shown: under
Today's controls on this week, and under the link back to this week on another, where no
Today panel is added. A Not yet and a note added to homework say whether the work can be in
today's plan by the rule the planner reads: undated work is in, and a source's date inside
the window counts. Another week says its updates are the latest saved, the details head their
evidence as what is on record now, and a parent reads the privacy fold about her."""

import pathlib
from datetime import date

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from tests.support import (
    ESSAY_ID,
    FIXTURE_WEEK,
    HER_PAGE,
    HERS,
    PAGE_HEADERS,
    SAME_ORIGIN,
    THEIRS,
    browser,
    card_for,
    fixture_settings,
    hidden,
    report,
    signed_in_household,
    state_of,
)
from tests.support import QUIZ_ID as QUIZ
from tests.support import SYLLABUS_ID as SYLLABUS

WINDOW = "Today's planning window is August 19 to August 25, 2026."
OTHER_WEEK = "Updates show the latest saved information, even when you view a different week."
IN_THE_WINDOW = "Still unfinished. It can be included in today's plan."
OUTSIDE = "Saved as Not yet. It is outside today's planning window (August 19 to August 25, 2026)."


def on_day(day: date) -> TestClient:
    """Her page with the household's day pinned to ``day``."""
    return TestClient(
        create_app(fixture_settings(BLOSSOM_TODAY=day.isoformat())),
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
    assert "outside today's planning window" not in card_for(landed, assignment)


def test_a_not_yet_outside_the_window_names_the_window_by_its_days() -> None:
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

    assert OUTSIDE in card_for(landed, far)
    assert OUTSIDE in details
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
