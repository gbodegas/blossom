# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Family review around what needs a parent, with a bounded list of recent activity.

Made-up households read through the app on the pinned day, Wednesday,
August 19, 2026, most of them starting from an empty record. Each test names
the part of the page it pins: what needs a parent and the links to each item,
the ways to add or plan, the last seven household days of activity with what
stays open beside them, and the history below. Every date edge is read with a
pinned or set clock, and no model is asked.
"""

import pathlib
import re
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from html import unescape
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from blossom.drafts import Draft
from blossom.hand_in import NEEDS_HAND_IN, TURNED_IN, HandInSaved
from blossom.routes.navigation import (
    ADDED_NOTES_PAGE,
    ARCHIVED_NOTES_PAGE,
    details_href,
    segment,
)
from blossom.routes.parent import span_said
from blossom.stores.project_state import Checked, ProjectStateStore, Saved
from tests.support import (
    ESSAY_ID,
    PAGE_HEADERS,
    PLAN_DATE,
    THEIRS,
    ZONE,
    SetClock,
    a_row,
    browser,
    client_for,
    ended_run,
    lands_on,
    mark,
    school_missing,
    settled_run,
    signed_in,
    signed_in_household,
    spoil,
    state_of,
    store_of,
    waiting_note,
    with_clock,
    without_minted_fields,
    words,
)
from tests.test_help_placement import T0, asked, pinned_help
from tests.test_instructions_first import unreadable as instruction_bent
from tests.test_school_instructions_review import A, C, a_current_b_earlier, with_awaiting

FAMILY = "/parent"
NOTHING_NEEDS_YOU = "Nothing needs you right now."
FIRST_DAY = PLAN_DATE - timedelta(days=6)
"""The first of the last seven household days, today included."""
ADD_ASSIGNMENTS = "/parent?add=1#add-assignments"
ADD_A_GRADE_REPORT = "/parent/grades/add"
HELP_WITH_A_PLAN = "/parent?planning=1#help-with-a-plan"
CHECK_ME = "check-me"
PLAN_ID = "draft:plan:wren"
"""A waiting plan's id, with characters a link escapes."""
CHECKED_TODAY = (
    "A parent marked this checked with her on August 19. The note with it: Seen together."
)
EARLIER = {"activity": "earlier"}


# ------------------------------------------------------------------ reading the page


def page_at(client: TestClient, **query: str) -> str:
    shown = client.get(FAMILY, params=query, headers=PAGE_HEADERS)
    assert shown.status_code == 200, shown.text[:400]
    return shown.text


OPENING = re.compile(r'<(\w+)\b[^>]*?\sid="([^"]*)"[^>]*>')
ROW_ID = re.compile(r'\sid="(?:update|hand-in)-([^"]+)"')
LINK = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S)


def element(page: str, ident: str) -> str:
    """The element whose id is ``ident``, whole: its opening tag to the end tag closing it."""
    found = next((tag for tag in OPENING.finditer(page) if unescape(tag.group(2)) == ident), None)
    assert found is not None, ident
    name = found.group(1)
    depth = 0
    for tag in re.finditer(rf"<(/?){name}\b[^>]*>", page[found.start() :]):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return page[found.start() : found.start() + tag.end()]
    raise AssertionError(ident)


def without(fragment: str, *idents: str) -> str:
    """A piece of a page with the elements of these ids left out, where it holds them."""
    for ident in idents:
        if f'id="{ident}"' in fragment:
            fragment = fragment.replace(element(fragment, ident), "", 1)
    return fragment


def window(page: str) -> list[str]:
    """The assignments of the entries Recent activity lists for its days, in order: the
    section without Still open and Earlier activity."""
    listed = without(element(page, "recent-activity"), "still-open", "earlier-activity")
    return [unescape(found) for found in ROW_ID.findall(listed)]


def earlier(page: str) -> list[str]:
    """The assignments of the entries Earlier activity lists, in order."""
    return [unescape(found) for found in ROW_ID.findall(element(page, "earlier-activity"))]


def links(fragment: str) -> list[tuple[str, str]]:
    """Each link in a piece of a page: its address with entities undone, and its words."""
    made = []
    for attributes, inside in LINK.findall(fragment):
        href = re.search(r'\shref="([^"]*)"', attributes)
        if href is not None:
            made.append((unescape(href.group(1)), words(inside)))
    return made


def link_words(fragment: str, query: str, landing: str) -> list[str]:
    """The words of each link in a piece of a page to the family page with ``query``, landing
    on ``landing``, written from the root or from the page itself."""
    return [
        said
        for href, said in links(fragment)
        if href in (f"{FAMILY}?{query}#{landing}", f"?{query}#{landing}")
    ]


def needs_you_targets(page: str) -> list[str]:
    """Where each link in Needs you goes on the same page, as the fragment is written."""
    return [href for href, _ in links(element(page, "needs-you")) if href.startswith("#")]


def heading(fragment: str, level: str) -> str:
    found = re.search(rf"<{level}\b[^>]*>(.*?)</{level}>", fragment, re.S)
    assert found is not None, level
    return words(found.group(1))


def ids_of(page: str) -> Counter[str]:
    return Counter(unescape(found) for found in re.findall(r'\sid="([^"]*)"', page))


SPAN = f"{FIRST_DAY:%B} {FIRST_DAY.day} to {PLAN_DATE.day}"
"""The seven days as the page says them: in one month, the second day without its month."""


def bounds(shown: str) -> str:
    """The sentence above the list, as a pattern, ending with what it says it shows."""
    return (
        rf"{re.escape(SPAN)}, the last 7 days, by the day Blossom recorded each\. "
        rf"{re.escape(shown)}"
    )


def comparable(page: str) -> str:
    """A render with the ids and issue times its forms mint left blank."""
    return without_minted_fields(page)


# ------------------------------------------------------------------ made-up households


def at_noon(day: date) -> datetime:
    """Noon in the household on ``day``, as a moment."""
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=ZONE).astimezone(UTC)


def on_record(client: TestClient, *names: str) -> ProjectStateStore:
    """Rows on the record, each titled Work and its name."""
    store = state_of(client).project_state
    store.put_on_record([a_row(name, f"Work {name}") for name in names], {})
    return store


def handed_in(
    store: ProjectStateStore,
    name: str,
    moment: datetime,
    *,
    state: str = TURNED_IN,
    step: str | None = None,
) -> None:
    """Her word on turning one assignment in, kept at ``moment`` under its household day."""
    kept = store.record_hand_in(
        name,
        state,  # type: ignore[arg-type]
        step,
        None,
        expected_head=None,
        now=moment,
        today=moment.astimezone(ZONE).date(),
    )
    assert isinstance(kept, HandInSaved)


def said_done(store: ProjectStateStore, name: str, moment: datetime) -> None:
    saved = store.report_status(
        name, "done", None, expected_head=None, now=moment, today=moment.astimezone(ZONE).date()
    )
    assert isinstance(saved, Saved)


def worth_checking(store: ProjectStateStore, name: str, day: date) -> None:
    """Her Done beside the school's current missing, both from ``day``."""
    said_done(store, name, at_noon(day))
    store.record_status_reports(name, [school_missing(day)])


def help_asked(client: TestClient) -> str:
    asked = state_of(client).help_requests.ask(PLAN_DATE, "Wren asks how to start the essay.")
    return asked.request_id


def note_waiting(store: ProjectStateStore) -> str:
    return waiting_note(store, course="Math", title="Fraction drill", text="Fraction drill, Friday")


def plan_waiting(client: TestClient) -> str:
    settled_run(
        state_of(client).drafts,
        Draft(draft_id=PLAN_ID, body="Plan for Wren's evening", created_at=at_noon(PLAN_DATE)),
        thread_id="plan:wren",
        plan_date=PLAN_DATE,
    )
    return PLAN_ID


SETTLED = [f"handed-{number:02}" for number in range(12)]
OLDER = [f"older-{number}" for number in range(3)]


def crowded(client: TestClient) -> dict[str, str]:
    """A help request, a homework note and a plan waiting, a row worth checking together,
    twelve hand-in reports in the last seven days and three older ones."""
    store = on_record(client, CHECK_ME, *SETTLED, *OLDER)
    for number, name in enumerate(OLDER):
        handed_in(store, name, at_noon(PLAN_DATE - timedelta(days=12 - number)))
    for number, name in enumerate(SETTLED):
        handed_in(store, name, at_noon(FIRST_DAY) + timedelta(hours=number * 8))
    worth_checking(store, CHECK_ME, PLAN_DATE - timedelta(days=1))
    return {"help": help_asked(client), "note": note_waiting(store), "plan": plan_waiting(client)}


# ------------------------------------------------------------------ the order


def test_on_a_crowded_page_what_needs_you_and_the_ways_to_add_come_before_any_activity() -> None:
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        made = crowded(client)
        page = page_at(client)

    placed = [
        page.index('id="needs-you"'),
        page.index('id="add-or-do"'),
        page.index(f'id="help-request-{made["help"]}"'),
        page.index(f'href="{ADD_ASSIGNMENTS}"'),
        page.index(f'id="plan-to-review-{made["plan"]}"'),
    ]
    assert max(placed) < page.index('id="recent-activity"')
    assert max(placed) < page.index("She reported it turned in on")
    assert len(window(page)) == 10


def test_the_sections_follow_one_order_from_needs_you_to_history() -> None:
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        made = crowded(client)
        page = page_at(client)

    order = [
        'id="needs-you"',
        'id="add-or-do"',
        'id="help-she-asked-for"',
        f'id="homework-note-{made["note"]}"',
        'id="worth-checking-together"',
        f'id="plan-to-review-{made["plan"]}"',
        'id="recent-activity"',
        'id="add-assignments"',
        'id="help-with-a-plan"',
        'id="history"',
    ]
    positions = [page.index(ident) for ident in order]
    assert positions == sorted(positions)
    for ident, level, said in (
        ("needs-you", "h2", "Needs you"),
        ("add-or-do", "h2", "Add or do"),
        ("recent-activity", "h2", "Recent activity"),
        ("history", "h2", "History"),
    ):
        assert heading(element(page, ident), level).startswith(said)


# ------------------------------------------------------------------ Needs you


def test_needs_you_has_one_row_per_waiting_item_whatever_its_age_each_linking_to_its_card() -> None:
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = on_record(client, CHECK_ME)
        worth_checking(store, CHECK_ME, PLAN_DATE - timedelta(days=60))
        cards = {
            "help-request-": help_asked(client),
            "homework-note-": note_waiting(store),
            "update-": CHECK_ME,
            "plan-to-review-": plan_waiting(client),
        }
        page = page_at(client)

    section = element(page, "needs-you")
    assert re.fullmatch(r"Needs you\D*4\D*", heading(section, "h2"))
    assert sorted(needs_you_targets(page)) == sorted(
        f"#{prefix}{segment(name)}" for prefix, name in cards.items()
    )
    for prefix, name in cards.items():
        assert page.count(f'id="{prefix}{name}"') == 1
    assert f"#plan-to-review-{PLAN_ID}" not in needs_you_targets(page)
    assert NOTHING_NEEDS_YOU not in page


def test_an_instruction_waiting_for_review_has_its_row_and_its_card(
    tmp_path: pathlib.Path,
) -> None:
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        a_current_b_earlier(store_of(client))
    with_awaiting(settings, C)
    with client_for(settings) as client:
        signed_in(client, THEIRS)
        page = client.get(FAMILY, headers=PAGE_HEADERS).text

    card = f"instructions-to-review-{ESSAY_ID}"
    assert f"#instructions-to-review-{segment(ESSAY_ID)}" in needs_you_targets(page)
    assert page.count(f'id="{card}"') == 1
    assert C in words(element(page, card))
    [(_, _, said)] = needs_rows(page, "School instructions to review")
    found_in = ", found in the old note field"
    course = re.fullmatch(rf'<span class="authored-text">([^<]+)</span>{found_in}', said)
    assert course is not None, said
    assert f'<span class="authored-text">{course.group(1)}</span> &middot;' in element(page, card)


def test_with_nothing_waiting_needs_you_says_so() -> None:
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        page = page_at(client)

    section = element(page, "needs-you")
    assert NOTHING_NEEDS_YOU in words(section)
    assert heading(section, "h2") == "Needs you"
    assert 'class="pill"' not in section
    assert needs_you_targets(page) == []


def help_set_apart(client: TestClient, store: ProjectStateStore) -> None:
    help_asked(client)
    connection = state_of(client).help_requests._connection
    connection.execute("UPDATE help_requests SET evening = 'not a date'")
    connection.commit()


def a_note_unreadable(client: TestClient, store: ProjectStateStore) -> None:
    damaged = note_waiting(store)
    store._connection.execute(
        "UPDATE homework_captures SET attribution = 'no json' WHERE capture_id = ?", (damaged,)
    )
    store._connection.commit()


def her_updates_unreadable(client: TestClient, store: ProjectStateStore) -> None:
    said_done(store, CHECK_ME, at_noon(PLAN_DATE - timedelta(days=1)))
    spoil(store, "student_reports", "status", "finished", CHECK_ME)


def checks_unreadable(client: TestClient, store: ProjectStateStore) -> None:
    worth_checking(store, CHECK_ME, PLAN_DATE - timedelta(days=1))
    assert mark(client, CHECK_ME).status_code == 303
    spoil(store, "family_checks", "operation", "erased", CHECK_ME)


def instructions_unreadable(client: TestClient, store: ProjectStateStore) -> None:
    """The essay with A kept and B as history, one kept instruction's row then bent."""
    store.put_on_record([a_row(CHECK_ME, f"Work {CHECK_ME}"), a_row(ESSAY_ID, "Work essay")], {})
    a_current_b_earlier(store)
    instruction_bent(store, A)


UNREADABLE: dict[str, Callable[[TestClient, ProjectStateStore], None]] = {
    "help requests set apart": help_set_apart,
    "a homework note that can't be read": a_note_unreadable,
    "her updates that can't be read": her_updates_unreadable,
    "family checks that can't be read": checks_unreadable,
    "school instructions that can't be read": instructions_unreadable,
}


@pytest.mark.parametrize("unreadable", UNREADABLE.values(), ids=UNREADABLE.keys())
def test_nothing_needs_you_is_never_said_while_a_source_of_it_cannot_be_read(
    unreadable: Callable[[TestClient, ProjectStateStore], None],
) -> None:
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        unreadable(client, on_record(client, CHECK_ME))
        page = page_at(client)

    assert NOTHING_NEEDS_YOU not in page
    targets = needs_you_targets(page)
    assert targets
    for target in targets:
        assert lands_on(page, target)
    assert max(ids_of(page).values()) == 1


def test_school_instructions_that_cannot_be_read_have_a_row_and_a_line_linking_each_details_page(
    tmp_path: pathlib.Path,
) -> None:
    """The signed-in household with C waiting for review beside A and B, and A's row bent: no
    instruction can be read, so none is listed to review, and Needs you says so instead."""
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        a_current_b_earlier(store_of(client))
    with_awaiting(settings, C)
    with client_for(settings) as client:
        signed_in(client, THEIRS)
        instruction_bent(store_of(client), A)
        page = client.get(FAMILY, headers=PAGE_HEADERS).text

    assert NOTHING_NEEDS_YOU not in page
    assert needs_rows(page, "School instructions to review") == [
        ("#instructions-unreadable", "School instructions that can't be read", "")
    ]
    assert page.count('id="instructions-unreadable"') == 1
    line = element(page, "instructions-unreadable")
    assert line in element(page, "school-instructions-to-review")
    assert [urlsplit(href).path for href, _ in links(line)] == [details_href(ESSAY_ID)]
    assert max(ids_of(page).values()) == 1


# ------------------------------------------------------------------ Add or do


def test_add_or_do_holds_three_links_in_place_of_the_lead_line_and_no_form() -> None:
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        page = page_at(client)

    section = element(page, "add-or-do")
    assert heading(section, "h2") == "Add or do"
    assert links(section) == [
        (ADD_ASSIGNMENTS, "Add assignments"),
        (ADD_A_GRADE_REPORT, "Add a grade report"),
        (HELP_WITH_A_PLAN, "Help with a plan"),
    ]
    assert "<form" not in section
    assert 'class="lead-action"' not in page


def test_planning_opens_help_with_a_plan_and_changes_nothing_else() -> None:
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        plain = page_at(client)
        again = page_at(client)
        planning = page_at(client, planning="1")

    fold = lands_on(planning, "#help-with-a-plan")
    assert fold.startswith("<details")
    assert " open" in fold
    assert " open" not in lands_on(plain, "#help-with-a-plan")
    assert comparable(again) == comparable(plain)
    assert comparable(planning.replace(fold, fold.replace(" open", ""), 1)) == comparable(plain)


def test_add_opens_add_assignments_as_it_does() -> None:
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        plain = page_at(client)
        adding = page_at(client, add="1")

    assert " open" in lands_on(adding, "#add-assignments")
    assert " open" not in lands_on(plain, "#add-assignments")


# ------------------------------------------------------------------ Recent activity


EDGES = {
    "minus-seven": PLAN_DATE - timedelta(days=7),
    "minus-six": PLAN_DATE - timedelta(days=6),
    "today": PLAN_DATE,
    "a-later-day": PLAN_DATE + timedelta(days=1),
}


def test_the_days_are_the_last_seven_household_days_today_and_a_later_day_included() -> None:
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = on_record(client, *EDGES)
        for name, day in EDGES.items():
            handed_in(store, name, at_noon(day))
        page = page_at(client)
        older = page_at(client, activity="earlier")

    recent = element(page, "recent-activity")
    assert window(page) == ["a-later-day", "today", "minus-six"]
    assert re.search(bounds("Showing all 3."), words(recent))
    assert link_words(recent, "activity=earlier", "earlier-activity") == ["Earlier activity (1)"]
    assert link_words(recent, "activity=all", "recent-activity") == []
    assert earlier(older) == ["minus-seven"]


def test_a_moment_near_midnight_counts_by_the_household_day_and_not_the_utc_day() -> None:
    """Read at 23:30 on August 19 in New York, 03:30 on August 20 in UTC. A report kept at
    23:00 on August 12 there, August 13 in UTC, is a day before the seven; one kept at 22:00
    on August 19 there, August 20 in UTC, is today's."""
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = on_record(client, "late-on-the-twelfth", "late-tonight")
        handed_in(store, "late-on-the-twelfth", datetime(2026, 8, 13, 3, 0, tzinfo=UTC))
        handed_in(store, "late-tonight", datetime(2026, 8, 20, 2, 0, tzinfo=UTC))
        with_clock(client, SetClock(PLAN_DATE, datetime(2026, 8, 20, 3, 30, tzinfo=UTC)))
        page = page_at(client)
        older = page_at(client, activity="earlier")

    assert window(page) == ["late-tonight"]
    assert re.search(bounds("Showing all 1."), words(element(page, "recent-activity")))
    assert earlier(older) == ["late-on-the-twelfth"]


@pytest.mark.parametrize("count", [10, 11])
def test_the_overview_shows_the_ten_newest_and_says_how_many_the_days_hold(count: int) -> None:
    names = [f"handed-{number:02}" for number in range(count)]
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = on_record(client, *names)
        for number, name in enumerate(names):
            handed_in(store, name, at_noon(FIRST_DAY) + timedelta(hours=number * 12))
        page = page_at(client)
        whole = page_at(client, activity="all")

    newest = list(reversed(names))
    recent = element(page, "recent-activity")
    show_all = link_words(recent, "activity=all", "recent-activity")
    assert window(page) == newest[:10]
    if count == 10:
        assert re.search(bounds("Showing all 10."), words(recent))
        assert show_all == []
    else:
        assert re.search(bounds("Showing the 10 newest of 11."), words(recent))
        assert show_all == ["Show all 11 from these days"]
    assert window(whole) == newest
    assert max(ids_of(whole).values()) == 1


@pytest.mark.parametrize(
    ("first", "last", "said"),
    [
        (date(2026, 8, 13), date(2026, 8, 19), "August 13 to 19"),
        (date(2026, 11, 28), date(2026, 12, 4), "November 28 to December 4"),
        (date(2026, 12, 29), date(2027, 1, 4), "December 29, 2026 to January 4, 2027"),
    ],
    ids=["one-month", "two-months", "two-years"],
)
def test_the_days_are_said_with_one_month_two_months_or_two_years(
    first: date, last: date, said: str
) -> None:
    assert span_said(first, last) == said


def test_empty_days_say_so_with_their_dates() -> None:
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        page = page_at(client)

    recent = element(page, "recent-activity")
    assert f"No updates, checks or hand-in reports from {SPAN}." in words(recent)
    assert window(page) == []
    assert link_words(recent, "activity=earlier", "earlier-activity") == []


AGES = {"twenty-days": 20, "nine-days": 9, "eight-days": 8, "today": 0}


def test_earlier_activity_counts_what_is_older_and_lists_it_newest_first() -> None:
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = on_record(client, *AGES)
        for name, age in AGES.items():
            handed_in(store, name, at_noon(PLAN_DATE - timedelta(days=age)))
        page = page_at(client)
        older = page_at(client, activity="earlier")
        unknown = page_at(client, activity="zebra")

    recent = element(page, "recent-activity")
    assert window(page) == ["today"]
    assert link_words(recent, "activity=earlier", "earlier-activity") == ["Earlier activity (3)"]
    assert element(older, "earlier-activity") in element(older, "recent-activity")
    assert heading(element(older, "earlier-activity"), "h3").startswith("Earlier activity")
    assert earlier(older) == ["eight-days", "nine-days", "twenty-days"]
    assert comparable(unknown) == comparable(page)


def test_what_stays_open_is_shown_before_the_list_whatever_its_age_and_never_counted() -> None:
    names = [f"handed-{number:02}" for number in range(11)]
    long_ago = PLAN_DATE - timedelta(days=90)
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = on_record(client, *names, "to-turn-in", "school-missing", "unreadable")
        handed_in(
            store,
            "to-turn-in",
            at_noon(long_ago),
            state=NEEDS_HAND_IN,
            step="Find it in the blue folder",
        )
        store.record_status_reports(
            "school-missing", [school_missing(PLAN_DATE - timedelta(days=60))]
        )
        said_done(store, "unreadable", at_noon(PLAN_DATE - timedelta(days=2)))
        spoil(store, "student_reports", "status", "finished", "unreadable")
        for number, name in enumerate(names):
            handed_in(store, name, at_noon(FIRST_DAY) + timedelta(hours=number * 12))
        page = page_at(client)

    recent = element(page, "recent-activity")
    still = element(page, "still-open")
    assert still in recent
    assert heading(still, "h3") == "Still open"
    assert {unescape(found) for found in ROW_ID.findall(still)} == {
        "to-turn-in",
        "school-missing",
        "unreadable",
    }
    first_entry = recent.index(f'-{window(page)[0]}"')
    assert recent.index('id="still-open"') < first_entry
    said = words(still)
    assert f"She reported Still to turn in on {long_ago:%B} {long_ago.day}, {long_ago.year}." in (
        said
    )
    assert "Find it in the blue folder" in said
    assert "the school reports it missing" in said.lower()
    assert "Her updates on this assignment can't be read right now" in said
    assert len(window(page)) == 10
    assert re.search(bounds("Showing the 10 newest of 11."), words(recent))


NEWER = [f"handed-{number:02}" for number in range(11)]
"""Eleven turned-in reports, every twelve hours from noon on the first of the days."""


def morning(day: date) -> datetime:
    """Six in the morning in the household on ``day``, before every report of ``NEWER``."""
    return datetime(day.year, day.month, day.day, 6, 0, tzinfo=ZONE).astimezone(UTC)


def eleven_newer(store: ProjectStateStore) -> None:
    for number, name in enumerate(NEWER):
        handed_in(store, name, at_noon(FIRST_DAY) + timedelta(hours=number * 12))


def still_open_under(page: str, said: str) -> list[str]:
    """The assignments Still open lists under its heading ``said``, in order."""
    for part in element(page, "still-open").split('<h4 class="update-heading">')[1:]:
        title, _, rows = part.partition("</h4>")
        if words(title) == said:
            return [unescape(found) for found in ROW_ID.findall(rows)]
    return []


def ten_of_eleven(page: str) -> None:
    """The overview lists exactly the ten newest of ``NEWER`` and counts those eleven only."""
    assert window(page) == list(reversed(NEWER))[:10]
    assert re.search(
        bounds("Showing the 10 newest of 11."), words(element(page, "recent-activity"))
    )


def test_a_row_the_school_reports_missing_stays_open_past_ten_beside_her_update_of_the_days() -> (
    None
):
    """Her Not yet at 6:00 AM on August 13 beside the school's missing from June 20, with eleven
    newer reports: the row is under Still open, as one with no update of hers is."""
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = on_record(client, "sm-recent", "sm-control", *NEWER)
        for name in ("sm-recent", "sm-control"):
            store.record_status_reports(name, [school_missing(PLAN_DATE - timedelta(days=60))])
        saved = store.report_status(
            "sm-recent",
            "not_yet",
            None,
            expected_head=None,
            now=morning(FIRST_DAY),
            today=FIRST_DAY,
        )
        assert isinstance(saved, Saved)
        eleven_newer(store)
        page = page_at(client)

    assert sorted(still_open_under(page, "School reports")) == ["sm-control", "sm-recent"]
    assert page.count('id="update-sm-recent"') == 1
    said = words(element(page, "update-sm-recent"))
    assert "the school reports it missing" in said
    assert f"She reported it not yet done on {FIRST_DAY:%B} {FIRST_DAY.day}." in said
    ten_of_eleven(page)
    assert max(ids_of(page).values()) == 1


def test_a_row_the_school_reports_missing_stays_open_past_ten_with_its_check_of_the_days() -> None:
    """Her Done and the school's missing from July 20, marked checked at 6:00 AM on August 13,
    with eleven newer reports: the row is under Still open with its check and Check again."""
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = on_record(client, "sm-checked", *NEWER)
        worth_checking(store, "sm-checked", PLAN_DATE - timedelta(days=30))
        with_clock(client, SetClock(FIRST_DAY, morning(FIRST_DAY)))
        assert mark(client, "sm-checked", "Seen.").status_code == 303
        with_clock(client, SetClock(PLAN_DATE, at_noon(PLAN_DATE) + timedelta(hours=6)))
        eleven_newer(store)
        page = page_at(client)

    assert still_open_under(page, "School reports") == ["sm-checked"]
    assert page.count('id="update-sm-checked"') == 1
    row = element(page, "update-sm-checked")
    assert 'aria-label="Check again: Work sm-checked"' in row
    assert (
        f"A parent marked this checked with her on {FIRST_DAY:%B} {FIRST_DAY.day}. "
        "The note with it: Seen."
    ) in words(row)
    assert "the school reports it missing" in words(row)
    ten_of_eleven(page)
    assert max(ids_of(page).values()) == 1


def updates_unreadable_in_the_days(store: ProjectStateStore, name: str) -> None:
    """Her Done at 6:00 AM on August 13, its row then bent."""
    said_done(store, name, morning(FIRST_DAY))
    spoil(store, "student_reports", "status", "finished", name)


def checks_unreadable_in_the_days(store: ProjectStateStore, name: str) -> None:
    """Her Done at 6:00 AM on August 13 and a check of the same morning, written by the
    store's own writer on a made-up basis, the check's row then bent. A check made from the
    page rests on a school missing, which lists the row under School reports first."""
    said_done(store, name, morning(FIRST_DAY))
    basis = f"{name}|made-up"
    kept = store.mark_checked(
        name,
        basis,
        None,
        expected_check=None,
        basis_now=lambda: basis,
        now=morning(FIRST_DAY) + timedelta(minutes=5),
        today=FIRST_DAY,
    )
    assert isinstance(kept, Checked)
    spoil(store, "family_checks", "operation", "erased", name)


UNREADABLE_IN_THE_DAYS: dict[str, Callable[[ProjectStateStore, str], None]] = {
    "her updates": updates_unreadable_in_the_days,
    "the family checks": checks_unreadable_in_the_days,
}


@pytest.mark.parametrize(
    "unreadable", UNREADABLE_IN_THE_DAYS.values(), ids=UNREADABLE_IN_THE_DAYS.keys()
)
def test_a_row_whose_records_cannot_be_read_stays_open_past_ten_and_the_ten_stay_ten(
    unreadable: Callable[[ProjectStateStore, str], None],
) -> None:
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = on_record(client, "bent", *NEWER)
        unreadable(store, "bent")
        eleven_newer(store)
        page = page_at(client)

    assert still_open_under(page, "Records that can't be read") == ["bent"]
    assert page.count('id="update-bent"') == 1
    ten_of_eleven(page)
    assert max(ids_of(page).values()) == 1


def test_recent_activity_keeps_the_words_that_tell_the_four_accounts_apart() -> None:
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = on_record(client, "her-update", "her-hand-in", CHECK_ME)
        said_done(store, "her-update", at_noon(PLAN_DATE - timedelta(days=1)))
        handed_in(store, "her-hand-in", at_noon(PLAN_DATE - timedelta(days=1)))
        worth_checking(store, CHECK_ME, PLAN_DATE - timedelta(days=2))
        assert mark(client, CHECK_ME, "Seen together.").status_code == 303
        page = page_at(client)

    listed = without(element(page, "recent-activity"), "still-open", "earlier-activity")
    said = words(listed)
    assert set(window(page)) == {"her-update", "her-hand-in"}
    assert "it does not turn work in" in said
    assert "Nothing here is sent to the school." in said
    assert still_open_under(page, "School reports") == [CHECK_ME]
    checked = element(page, f"update-{CHECK_ME}")
    assert CHECKED_TODAY in words(checked)
    assert f'aria-label="Check again: Work {CHECK_ME}"' in checked
    assert "the school reports it missing" in words(checked)


def test_her_update_and_her_hand_in_on_one_assignment_are_two_entries_newest_first() -> None:
    """Nothing is merged: the update keeps the row's id and the hand-in report has its own."""
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = on_record(client, "both")
        handed_in(store, "both", at_noon(PLAN_DATE - timedelta(days=2)))
        said_done(store, "both", at_noon(PLAN_DATE - timedelta(days=1)))
        page = page_at(client)

    recent = element(page, "recent-activity")
    listed = without(recent, "still-open", "earlier-activity")
    assert re.findall(r'\sid="((?:update|hand-in)-both)"', listed) == [
        "update-both",
        "hand-in-both",
    ]
    assert re.search(bounds("Showing all 2."), words(recent))
    assert max(ids_of(page).values()) == 1


def test_a_row_the_address_names_is_on_the_page_past_the_ten_or_older_than_the_days() -> None:
    names = [f"handed-{number:02}" for number in range(11)]
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = on_record(client, "long-ago", *names)
        handed_in(store, "long-ago", at_noon(PLAN_DATE - timedelta(days=20)))
        for number, name in enumerate(names):
            handed_in(store, name, at_noon(FIRST_DAY) + timedelta(hours=number * 12))
        past_the_ten = page_at(client, focus=names[0])
        older = page_at(client, focus="long-ago")

    for page, name in ((past_the_ten, names[0]), (older, "long-ago")):
        assert page.count(f'id="update-{name}"') == 1
        assert f"Work {name}" in words(element(page, f"update-{name}"))
        assert max(ids_of(page).values()) == 1
    assert window(past_the_ten) == list(reversed(names))
    assert earlier(older) == ["long-ago"]


def test_every_id_on_the_crowded_page_and_its_two_longer_lists_is_said_once() -> None:
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        crowded(client)
        pages = [page_at(client, **query) for query in ({}, {"activity": "all"}, EARLIER)]

    for page in pages:
        assert 'id="recent-activity"' in page
        assert max(ids_of(page).values()) == 1
    assert len(window(pages[1])) == len(SETTLED)
    assert earlier(pages[2]) == list(reversed(OLDER))


MIDDLE_DOT = unescape("&middot;")
"""The dot between a row's day and its class, as the page's entity reads."""
ONE_LIST = '<ul class="compact-rows">'
ENTRY_ROW = re.compile(
    r'<li class="compact-row entry-[^"]*" id="(?:update|hand-in)-[^"]*" tabindex="-1">\s*'
    r'<p class="course">([^<]*)</p>\s*'
    r'<p class="decision-line"><strong><a [^>]*>([^<]*)</a></strong></p>\s*'
    r"<p>([^<]+)"
)
NEEDS_ROW = re.compile(
    r'<li class="compact-row">\s*<p class="course">([^<]*)</p>\s*'
    r'<p class="decision-line"><strong><a href="(#[^"]*)">(.*?)</a></strong></p>\s*'
    r"(?:<p>(.*?)</p>\s*)?</li>",
    re.S,
)


def test_the_entries_of_the_days_and_the_needs_you_rows_are_compact_rows_of_one_list_each() -> None:
    """Each row is a list item with its kind line, its link, and its words after them."""
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        crowded(client)
        page = page_at(client)

    listed = without(element(page, "recent-activity"), "still-open", "earlier-activity")
    assert listed.count(ONE_LIST) == 1
    entries = ENTRY_ROW.findall(listed)
    assert len(entries) == len(window(page)) == 10
    for kind, title, said in entries:
        assert re.fullmatch(rf"[A-Z][a-z]+ \d+ {MIDDLE_DOT} \S.*", unescape(kind).strip()), kind
        assert title.startswith("Work ")
        assert said.strip()

    section = element(page, "needs-you")
    assert section.count(ONE_LIST) == 1
    rows = NEEDS_ROW.findall(section)
    assert len(rows) == section.count("<li") == 4
    assert [words(kind) for kind, *_ in rows] == [
        "Help she asked for",
        "Homework she added",
        "Worth checking together",
        "Waiting for your review",
    ]
    assert re.fullmatch(r"Request from \w+, \w+ \d+ at \d+:\d\d [AP]M", words(rows[0][2]))
    assert rows[0][3] == ""
    for _, _, item, said in rows[1:3]:
        assert words(item)
        assert words(said)
    assert words(rows[3][2]) == f"Plan for {PLAN_DATE:%A, %B} {PLAN_DATE.day}"


def needs_rows(page: str, kind: str) -> list[tuple[str, str, str]]:
    """The Needs you rows of one kind: where each links, its link's words, and its words line
    as written, empty where the row has none."""
    return [
        (unescape(href), words(item), said)
        for said_kind, href, item, said in NEEDS_ROW.findall(element(page, "needs-you"))
        if words(said_kind) == kind
    ]


def test_two_waiting_requests_are_told_apart_by_the_day_and_time_their_links_say() -> None:
    """Asked at 5:00 PM and 5:30 PM on August 19 in the household, each request's link says
    when she asked it, and its row has no words line."""
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store, clock = pinned_help(client)
        first = asked(store, "Wren asks how to start the essay.")
        clock.at = T0 + timedelta(minutes=30)
        second = asked(store, "Wren asks where the map is.")
        page = page_at(client)

    said = "Request from Wednesday, August 19 at"
    assert sorted(needs_rows(page, "Help she asked for")) == sorted(
        [
            (f"#help-request-{segment(first)}", f"{said} 5:00 PM", ""),
            (f"#help-request-{segment(second)}", f"{said} 5:30 PM", ""),
        ]
    )


# ------------------------------------------------------------------ History


def test_history_holds_the_earlier_plans_the_plans_that_could_not_be_made_and_the_notes_line() -> (
    None
):
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        ended_run(
            state_of(client).drafts,
            thread_id="plan:ended",
            plan_date=PLAN_DATE,
            outcome="checks_failed",
        )
        page = page_at(client)

    history = element(page, "history")
    assert heading(history, "h2") == "History"
    assert "<summary>Earlier plans</summary>" in history
    assert "<summary>Plans that couldn't be made</summary>" in history
    for address, said in (
        (ADDED_NOTES_PAGE, "Notes added to homework"),
        (ARCHIVED_NOTES_PAGE, "Archived homework notes"),
    ):
        assert (address, said) in links(history)
        assert page.count(f'href="{address}"') == 1
