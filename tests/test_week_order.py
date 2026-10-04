# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Her week reads in the order the work is done in, and a press lands where its answer is.

Today comes first, with today's plan or, when there is none and work is left, a link down
to the homework. Then the week's homework, what she has reported done, and what was given
out this week for later; after all of it, her To turn in list, her homework notes and
Help. On another week the homework stands alone.

A save or an Undo from a card lands on the card's result. A refusal about one card is said
on that card, as the one alert, and the card's line takes the focus unless a field does;
the top of the page repeats it, with a link to the card, as a plain line. A refusal with no
card line on the page is said at the top, as the alert, and takes the focus there. A press
her To turn in list refuses is said on the list's own line, on her week and on the list's
own page, which is the alert and takes the focus. An ordinary visit asks for no focus, and
no response asks for more than one.

The fixture week through the app, a pinned day, synthetic words, and forms read from the
pages' own HTML. A plan is made by scripted graphs; no model is asked.
"""

import pathlib
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import date
from html import unescape
from typing import Final
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from blossom.hand_in import NEEDS_HAND_IN, TURNED_IN
from blossom.plans import DailyPlan
from blossom.routes.hand_in import GONE_FROM_THE_LIST
from blossom.routes.runs import PlanGraphs
from blossom.settings import REPOSITORY_ROOT
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    DETAILS,
    ESCAPED,
    ESSAY_ID,
    FIXTURE_WEEK,
    HER_PAGE,
    LATER_WEEK,
    NOW,
    PAGE_HEADERS,
    PLAN_DATE,
    REPORT,
    THEIRS,
    UNDO,
    Answer,
    accepting,
    after,
    already_undone,
    bad_return,
    card_for,
    conflict,
    due,
    failed_undo,
    failed_write,
    fixture_week_plan,
    forgetful_fixture_plan,
    form_fields,
    lands_on,
    main_of,
    malformed,
    no_choice,
    not_this_cards,
    note_too_long,
    page_of,
    reading,
    report,
    reported,
    save,
    saved,
    saved_again,
    school_missing,
    scripted_graphs,
    signed_in,
    stale_undo,
    state_of,
    store_of,
    undone,
    waiting_note,
    week_card,
    whole_form,
    words,
)
from tests.support import QUIZ_ID as QUIZ
from tests.support import READING_LOG_ID as LOG
from tests.support import SYLLABUS_ID as SYLLABUS

READERS: Final = ("her", "a parent", "sign-in off")
ALGEBRA: Final = "assignment-algebra-set"
FAIR: Final = "assignment-science-fair-proposal"
COVER: Final = "assignment-textbook-cover"
FAR: Final = "far-set"
NOWHERE: Final = "assignment-nowhere"
FAR_WEEK: Final = "2026-10-05"
ACTIONS: Final = f"/student/actions/assignments/{ESSAY_ID}"
HAND_IN: Final = f"{ACTIONS}/hand-in"
UNDO_HAND_IN: Final = f"{ACTIONS}/undo-hand-in"
TO_TURN_IN_PAGE: Final = "/student/to-turn-in"
TOO_MUCH: Final = "/student/actions/too-much"
TAKE_BACK: Final = "/student/actions/take-back/"
PLAN: Final = "/student/actions/plan"
CSS: Final = REPOSITORY_ROOT / "blossom" / "static" / "blossom.css"

HEADING: Final = '<h2 class="list-heading" id="homework" tabindex="-1">Due this week</h2>'
THAT_WEEK: Final = '<h2 class="list-heading" id="homework" tabindex="-1">Due that week</h2>'
TOP_FOCUSED: Final = '<p class="problem week-problem" role="alert" tabindex="-1" autofocus>'
TOP_PLAIN_ALERT: Final = '<p class="problem week-problem" role="alert">'
TOP_BESIDE_A_CARD: Final = '<p class="problem week-problem">'
OTHER_WEEK_SENTENCE: Final = (
    '<p class="note">Updates show the latest saved information, even when you view a '
    "different week.</p>"
)
NO_PLAN_YET: Final = "No plan for today yet."
NOTHING_TO_SCHEDULE: Final = (
    '<p class="note" role="status">Nothing to schedule from the work in this planning window.</p>'
)
EMPTY_WEEK: Final = "No assignments are recorded as due this week."
NOT_HERS: Final = "Sign in as the student to update."
NOT_HERS_TO_SIGNAL: Final = "Sign in as the student to say today is too much or take it back."
NOT_ON_RECORD: Final = "That assignment is not on record, so nothing was changed."
SAVED_ELSEWHERE: Final = "An update was saved on another device. Review it before saving yours."


# ------------------------------------------------------------------ the household


def as_a_parent(client: TestClient) -> None:
    """A parent signs in on the device she was using."""
    client.post("/sign-out")
    signed_in(client, THEIRS)


def planner(plan: Callable[[], DailyPlan]) -> Callable[..., PlanGraphs]:
    """Scripted graphs whose planner answers with this plan every time it is asked."""
    return scripted_graphs(lambda: [plan()] * 3, lambda: [accepting()])


def to_turn_in(
    store: ProjectStateStore, assignment_id: str = ESSAY_ID, step: str = "Print it"
) -> None:
    """Her word that the work is still to turn in, with this next step, over whatever she
    said before."""
    readings = store.hand_in_readings([assignment_id]).readable
    head = readings[assignment_id].head_id if assignment_id in readings else None
    store.record_hand_in(
        assignment_id, NEEDS_HAND_IN, step, None, expected_head=head, now=NOW, today=PLAN_DATE
    )


def turned_in(store: ProjectStateStore, assignment_id: str = ESSAY_ID) -> None:
    readings = store.hand_in_readings([assignment_id]).readable
    head = readings[assignment_id].head_id if assignment_id in readings else None
    store.record_hand_in(
        assignment_id, TURNED_IN, None, None, expected_head=head, now=NOW, today=PLAN_DATE
    )


def a_note(store: ProjectStateStore) -> None:
    waiting_note(store, course="Geometry", title="Questions 4-8", text="Questions 4-8, from class")


def off_the_record(store: ProjectStateStore, *names: str) -> None:
    for name in names:
        store._connection.execute("DELETE FROM assignments WHERE assignment_id = ?", (name,))
    store._connection.commit()


def everything_done(store: ProjectStateStore) -> None:
    for item in store.all_assignments():
        reported(store, "done", item.assignment_id)


def crowd(store: ProjectStateStore) -> None:
    """Fourteen more assignments due this week, a Done one among the fixture's, what is
    left to turn in, three homework notes and two requests for help: the week at its
    fullest."""
    store.put_on_record(
        [due(f"crowd-{n}", f"Crowd set {n}", date(2026, 8, 20 + n % 3)) for n in range(14)], {}
    )
    reported(store, "done", "assignment-science-fair-proposal")
    to_turn_in(store)
    to_turn_in(store, "assignment-science-fair-proposal")
    for _ in range(3):
        a_note(store)


def asked_for_help(client: TestClient) -> None:
    state_of(client).help_requests.ask(PLAN_DATE, "Question 3")
    state_of(client).help_requests.ask(PLAN_DATE, None)


def everything_kept(client: TestClient) -> dict[str, list[str]]:
    """Every row of every table in the household's files, read through a connection of its
    own."""
    settings = state_of(client).settings
    kept: dict[str, list[str]] = {}
    for path in (settings.database_path, settings.trace_path):
        with closing(sqlite3.connect(path)) as connection:
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            ]
            for table in tables:
                rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()  # noqa: S608
                kept[f"{path.name}:{table}"] = sorted(repr(row) for row in rows)
    return kept


# ------------------------------------------------------------------ reading a page


def tags(page: str) -> list[str]:
    """Every opening tag on the page, in order."""
    return re.findall(r"<[a-zA-Z][^<>]*>", page)


def focused_on_arrival(page: str) -> list[str]:
    """The opening tag of each element the page asks to take the focus as it arrives."""
    return [tag for tag in tags(page) if re.search(r"\sautofocus(?=[\s>])", tag)]


def tag_with_id(page: str, name: str) -> str:
    found = [tag for tag in tags(page) if f' id="{name}"' in tag]
    assert len(found) <= 1, (name, found)
    return found[0] if found else ""


def top_line(page: str) -> str:
    """The top of her week's problem line, whole, or empty when there is none."""
    found = re.search(r'<p class="problem week-problem"[^>]*>.*?</p>', page, re.S)
    return found.group(0) if found else ""


def at(page: str, marker: str) -> int:
    assert page.count(marker) == 1, (marker, page.count(marker))
    return page.index(marker)


def folds_open_around(page: str, tag: str) -> bool:
    """Whether every fold that holds the element starting with this tag is open."""
    place = page.index(tag)
    opened: list[str] = []
    for found in re.finditer(r"<details\b[^>]*>|</details>", page[:place]):
        if found.group(0).startswith("</"):
            opened.pop()
        else:
            opened.append(found.group(0))
    return all(re.search(r"\sopen(?=[\s>])", fold) for fold in opened)


TAB_STOP: Final = re.compile(
    r"<(?:a\b[^>]*\shref=|button\b|select\b|textarea\b|summary\b"
    r'|input\b(?![^>]*type="hidden"))'
)


def next_tab_stop(page: str, after: int) -> int:
    """Where the next Tab goes from a place on the page: the first control after it. A
    closed fold's summary counts and everything else inside that fold is skipped."""
    closed = 0
    for found in re.finditer(r"<[^<>]+>", page[after:]):
        tag = found.group(0)
        if tag.startswith("<details"):
            closed += 1 if closed or not re.search(r"\sopen(?=[\s>])", tag) else 0
        elif tag == "</details>":
            closed = max(closed - 1, 0)
        elif closed > 1 or (closed and not tag.startswith("<summary")):
            continue
        elif TAB_STOP.match(tag):
            return after + found.start()
    return -1


def item_of(page: str, title: str) -> tuple[int, int]:
    """Where the card or the row due later with this title starts and ends on the page."""
    for opening, name, ending in (
        ("<article ", f"<h2>{title}</h2>", "</article>"),
        ("<li id=", f"<strong>{title}</strong>", "</li>"),
    ):
        if name in page:
            place = page.index(name)
            return page.rindex(opening, 0, place), page.index(ending, place)
    raise AssertionError(title)


def outlined() -> set[str]:
    """Every selector of a rule that draws an outline."""
    plain = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    return {
        part.strip()
        for head, inside in re.findall(r"([^{}]+)\{([^{}]*)\}", plain)
        if re.search(r"(?<![-\w])outline:\s*\d", inside)
        for part in head.split(",")
    }


def in_sentence_rule() -> set[str]:
    """The selectors of the rule that gives a link inside a sentence 44 pixels to press."""
    plain = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    return {
        part.strip()
        for head, inside in re.findall(r"([^{}]+)\{([^{}]*)\}", plain)
        if "padding: 0.8rem 0;" in inside and "margin: -0.8rem 0;" in inside
        for part in head.split(",")
    }


def in_an_update(page: str, place: int) -> bool:
    """Whether the element at ``place`` is inside an update component: the last one opened
    before it is still open there, every element opened inside it since then closed."""
    start = page.rfind('<div class="update">', 0, place)
    depth = 0
    for found in re.finditer(r"<div\b|</div>", page[max(start, 0) : place]):
        depth += -1 if found.group(0) == "</div>" else 1
        if depth == 0:
            return False
    return start >= 0 and depth > 0


def outline_reaches(page: str, tag: str) -> bool:
    """Whether a rule that draws an outline on focus names this element: the top line, a
    card's problem line in an update, the To turn in list's problem line, a result line, the
    homework heading, or a field."""
    selectors = outlined()
    place = page.index(tag)
    checks = [
        ('class="problem week-problem"' in tag, ".week-problem:focus"),
        (
            tag.startswith('<p class="problem"')
            and 'tabindex="-1"' in tag
            and in_an_update(page, place),
            '.update .problem[tabindex="-1"]:focus',
        ),
        ('id="to-turn-in-problem"' in tag, "#to-turn-in-problem:focus"),
        ("update-result" in tag, ".update-result:focus"),
        ('class="list-heading"' in tag, ".list-heading:focus"),
        (tag.startswith("<input"), "input:focus-visible"),
        (tag.startswith("<textarea"), "textarea:focus-visible"),
    ]
    return any(matches and selector in selectors for matches, selector in checks)


def one_focus(answer: Answer) -> str:
    """The one element a response asks to take the focus, checked: no positive tabindex,
    at most one autofocus, and an outline that reaches it."""
    page = answer.text
    assert re.search(r'tabindex="[1-9]', page) is None
    asked = focused_on_arrival(page)
    assert len(asked) == 1, asked
    assert outline_reaches(page, asked[0]), asked[0]
    assert folds_open_around(page, asked[0]), asked[0]
    return asked[0]


# ------------------------------------------------------------------ the report helper
#
# The shared helper is checked on two weeks before anything here relies on it.


def test_the_report_helper_sends_the_week_it_read_and_lands_on_that_week() -> None:
    """Her update sent from her week and from the week after, where the essay is shown
    apart: each answer names the week its form came from."""
    with reading("sign-in off", pathlib.Path()) as client:
        this_week = report(client, ESSAY_ID, "not_yet")
        next_week = report(client, ESSAY_ID, "done", week=LATER_WEEK)
        landed = client.get(next_week, headers=PAGE_HEADERS).text

    assert parse_qs(urlsplit(this_week).query)["week"] == [FIXTURE_WEEK]
    assert parse_qs(urlsplit(next_week).query)["week"] == [LATER_WEEK]
    assert "<h1>Week of August 24</h1>" in landed
    assert "<h2>Outside the week shown</h2>" in landed
    assert "Your update is saved." in card_for(landed, ESSAY_ID)


# ------------------------------------------------------------------ the order (U-01, T-U6)

TODAY: Final = '<section class="panel today" id="today" tabindex="-1">'
APART: Final = '<section class="panel apart">'
ASSIGNED: Final = '<section class="panel assigned">'
TO_TURN_IN: Final = '<section class="panel to-turn-in" id="to-turn-in" tabindex="-1">'
NOTES: Final = '<section class="panel homework-notes" id="homework-notes" tabindex="-1">'
HELP: Final = '<section class="panel help-panel" aria-labelledby="help">'
PRIVACY: Final = "<summary>How Blossom uses"
PLACES: Final = (
    "Today",
    "apart",
    "homework",
    "cards",
    "Reported done",
    "given out for later",
    "To turn in",
    "homework notes",
    "Help",
    "how Blossom uses her information",
)
"""The places of her current week, in the order they are to come."""


def inside_a_fold(page: str, place: int) -> bool:
    before = page[:place]
    return len(re.findall(r"<details\b", before)) > len(re.findall(r"</details>", before))


def places_of(page: str) -> dict[str, int]:
    """Where each place of her week starts, for the places the page has. The cards are the
    first card of the week's own that is in no fold, and Reported done the fold of the
    week's own Done cards, before the work given out for later."""
    found: dict[str, int] = {}
    for name, marker in (
        ("Today", TODAY),
        ("apart", APART),
        ("homework", HEADING),
        ("given out for later", ASSIGNED),
        ("To turn in", TO_TURN_IN),
        ("homework notes", NOTES),
        ("Help", HELP),
        ("how Blossom uses her information", PRIVACY),
    ):
        if marker in page:
            found[name] = at(page, marker)
    if "homework" in found:
        start = found["homework"]
        end = found.get("given out for later", len(page))
        cards = [
            start + card.start()
            for card in re.finditer(r'<article class="assignment', page[start:end])
            if not inside_a_fold(page, start + card.start())
        ]
        if cards:
            found["cards"] = cards[0]
        done = page.find('<details class="steps reported-done"', start, end)
        if done >= 0:
            found["Reported done"] = done
    return found


def quiet(client: TestClient) -> dict[str, str]:
    """The fixture week with one thing to turn in and one homework note."""
    to_turn_in(store_of(client))
    a_note(store_of(client))
    return {}


def crowded(client: TestClient) -> dict[str, str]:
    crowd(store_of(client))
    asked_for_help(client)
    return {}


def all_done(client: TestClient) -> dict[str, str]:
    everything_done(store_of(client))
    to_turn_in(store_of(client))
    a_note(store_of(client))
    return {}


def with_a_card_apart(client: TestClient) -> dict[str, str]:
    """Crowded, with a card of another week shown apart beside a Done card of this week."""
    store = store_of(client)
    store.put_on_record([due("far-poster", "Poster", date(2026, 9, 10))], {})
    crowd(store)
    asked_for_help(client)
    return {"show": "far-poster"}


# state: (make it, the places it has, how many cards are active)
STATES: Final[dict[str, tuple[Callable[[TestClient], dict[str, str]], frozenset[str], int]]] = {
    "quiet": (quiet, frozenset(PLACES) - {"apart", "Reported done"}, 5),
    "crowded": (crowded, frozenset(PLACES) - {"apart"}, 18),
    "all Done": (all_done, frozenset(PLACES) - {"apart", "cards"}, 0),
    "a card apart": (with_a_card_apart, frozenset(PLACES), 18),
}


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("state", list(STATES))
def test_her_week_reads_today_then_the_homework_then_the_lists_and_help(
    state: str, reader: str, tmp_path: pathlib.Path
) -> None:
    """Whoever reads: Today, any card shown apart, the homework heading, the active cards,
    Reported done, the work given out for later, and only then To turn in, the homework
    notes, Help and how Blossom uses her information."""
    make, expected, active = STATES[state]
    with reading(reader, tmp_path) as client:
        params = make(client)
        page = main_of(page_of(client, **params))

    found = places_of(page)
    assert set(found) == expected
    assert sorted(found, key=found.__getitem__) == [name for name in PLACES if name in expected]
    homework = page[found["homework"] : found["given out for later"]]
    cards = [
        card
        for card in re.finditer(r'<article class="assignment', homework)
        if not inside_a_fold(page, found["homework"] + card.start())
    ]
    assert len(cards) == active
    today = page[found["Today"] : page.index("</section>", found["Today"])]
    assert '<p class="support-links"><a href="/student/to-turn-in">To turn in (' in today
    assert '<a href="/student/homework-notes">Homework notes (' in today
    assert ('<p class="support-links help-updates-link">' in today) == (
        state in ("crowded", "a card apart")
    )
    assert "autofocus" not in page


@pytest.mark.parametrize("reader", READERS)
def test_another_week_shows_its_homework_and_none_of_today_or_the_lists(
    reader: str, tmp_path: pathlib.Path
) -> None:
    with reading(reader, tmp_path) as client:
        crowded(client)
        page = main_of(page_of(client, week=LATER_WEEK))

    assert THAT_WEEK in page
    for absent in (TODAY, APART, TO_TURN_IN, NOTES, HELP, PRIVACY):
        assert absent not in page, absent
    assert page.index('<p class="return">') < page.index(THAT_WEEK)
    assert "See homework" not in page
    assert "autofocus" not in page


@pytest.mark.parametrize("reader", READERS)
def test_to_turn_in_needs_no_done_fold_when_every_assignment_is_done(
    reader: str, tmp_path: pathlib.Path
) -> None:
    """With all the work Done the list is its own panel, after Reported done and inside no
    fold, and Today still links to it."""
    with reading(reader, tmp_path) as client:
        all_done(client)
        page = main_of(page_of(client))

    panel_at = at(page, TO_TURN_IN)
    panel = page[panel_at : page.index("</section>", panel_at)]
    assert not inside_a_fold(page, panel_at)
    assert "<details" not in panel
    assert "To turn in (1)" in panel
    assert page.index("<summary>Reported done (") < panel_at
    assert 'action="/student/actions/plan"' not in page


# ------------------------------------------------------------------ See homework (U7-2)


def no_plan_one_card_left(client: TestClient) -> None:
    everything_done(store_of(client))
    reported(store_of(client), "not_yet", "assignment-textbook-cover")


def no_plan_one_later_row_left(client: TestClient) -> None:
    everything_done(store_of(client))
    reported(store_of(client), "not_yet", LOG)


def see_homework(name: str) -> str:
    """See homework as it links to the card or row with this id."""
    return f'<p><a class="to-help" href="#assignment-{name}">See homework</a></p>'


SHOWN: Final[dict[str, tuple[Callable[[TestClient], None], str]]] = {
    "the fixture week": (lambda client: None, FAIR),
    "one unfinished card": (no_plan_one_card_left, COVER),
    "only an unfinished row due later": (no_plan_one_later_row_left, LOG),
}


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("case", list(SHOWN))
def test_with_no_plan_and_work_left_see_homework_links_to_the_homework(
    case: str, reader: str, tmp_path: pathlib.Path
) -> None:
    """In the plan's place, a plain link to the first card or row still to do. It has no id
    of its own and asks for no focus, and the homework's heading keeps its id."""
    make, first = SHOWN[case]
    with reading(reader, tmp_path) as client:
        make(client)
        page = main_of(page_of(client))
        asked = main_of(page_of(client, show_plan="1"))

    today = page[at(page, TODAY) : page.index("</section>", at(page, TODAY))]
    assert page.count(see_homework(first)) == 1
    assert today.rstrip().endswith(see_homework(first))
    landing = lands_on(page, f"#assignment-{first}")
    assert landing.endswith(f'id="assignment-{first}" tabindex="-1">')
    assert lands_on(page, "#homework") == HEADING.removesuffix("Due this week</h2>")
    assert NO_PLAN_YET in today
    assert "autofocus" not in page
    assert 'id="todays-plan"' not in page
    no_plan = '<p class="note" id="todays-plan" tabindex="-1" role="status">'
    said = asked.index(f"{no_plan}No plan is saved for today now.</p>")
    assert said < asked.index(see_homework(first))


def finished_but_missing(store: ProjectStateStore, name: str) -> None:
    """Her Done on this work, beside the school's word that it is missing."""
    reported(store, "done", name)
    store.record_status_reports(name, [school_missing(PLAN_DATE)])


def the_first_card_done(client: TestClient) -> dict[str, str]:
    reported(store_of(client), "done", FAIR)
    return {}


def a_school_report_above_the_cards(client: TestClient) -> dict[str, str]:
    finished_but_missing(store_of(client), ESSAY_ID)
    return {}


def a_school_report_and_one_card_left(client: TestClient) -> dict[str, str]:
    no_plan_one_card_left(client)
    finished_but_missing(store_of(client), ESSAY_ID)
    return {}


def reported_done_above_the_row_left(client: TestClient) -> dict[str, str]:
    no_plan_one_later_row_left(client)
    return {}


def both_above_the_row_left(client: TestClient) -> dict[str, str]:
    no_plan_one_later_row_left(client)
    finished_but_missing(store_of(client), ESSAY_ID)
    return {}


def a_school_report_on_a_row_due_later(client: TestClient) -> dict[str, str]:
    no_plan_one_later_row_left(client)
    finished_but_missing(store_of(client), ALGEBRA)
    return {}


def only_rows_due_later(client: TestClient) -> dict[str, str]:
    off_the_record(store_of(client), FAIR, COVER, ESSAY_ID, QUIZ, SYLLABUS)
    return {}


def an_id_a_browser_escapes(client: TestClient) -> dict[str, str]:
    store = store_of(client)
    everything_done(store)
    store.put_on_record([due(ESCAPED, "Set two, question one", date(2026, 8, 21))], {})
    return {}


def a_card_shown_apart(client: TestClient) -> dict[str, str]:
    store_of(client).put_on_record([due(FAR, "Far set", date(2026, 10, 7))], {})
    return {"show": FAR}


WORK_LEFT: Final[dict[str, tuple[Callable[[TestClient], dict[str, str]], str]]] = {
    "the fixture week": (lambda client: {}, FAIR),
    "the first card Done": (the_first_card_done, COVER),
    "a school report to check above the cards": (a_school_report_above_the_cards, FAIR),
    "a school report to check and one card left": (a_school_report_and_one_card_left, COVER),
    "Reported done above the only row left": (reported_done_above_the_row_left, LOG),
    "a school report and Reported done above the row left": (both_above_the_row_left, LOG),
    "a school report on a row due later": (a_school_report_on_a_row_due_later, LOG),
    "nothing due this week, rows due later": (only_rows_due_later, ALGEBRA),
    "an id a browser escapes": (an_id_a_browser_escapes, ESCAPED),
    "a card shown apart": (a_card_shown_apart, FAIR),
}


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("case", list(WORK_LEFT))
def test_after_see_homework_the_next_tab_is_inside_the_first_work_left(
    case: str, reader: str, tmp_path: pathlib.Path
) -> None:
    """See homework lands on the first card or row still to do, outside every fold, so the
    next Tab is inside it whatever sits above it under the heading, which stays in place."""
    make, first = WORK_LEFT[case]
    with reading(reader, tmp_path) as client:
        params = make(client)
        titles = {item.assignment_id: item.title for item in store_of(client).all_assignments()}
        page = main_of(page_of(client, **params))

    links = re.findall(r'<a class="to-help" href="([^"]*)">See homework</a>', page)
    assert len(links) == 1, links
    landing = lands_on(page, unescape(links[0]))
    assert landing, links[0]
    start, end = item_of(page, titles[first])
    stop = next_tab_stop(page, page.index(landing) + len(landing))
    assert start < stop < end, page[stop : stop + 160]
    assert page.index(landing) == start
    assert landing.endswith(' tabindex="-1">')
    assert folds_open_around(page, landing)
    assert '<details class="steps reported-done" open' not in page
    heading = at(page, HEADING)
    assert lands_on(page, "#homework") == HEADING.removesuffix("Due this week</h2>")
    if "school report" in case:
        assert heading < page.index("to check:</strong>") < start
    if "Reported done" in case:
        assert heading < page.index("<summary>Reported done (5)</summary>") < start


def later_work_keeps_the_plan_button(client: TestClient) -> None:
    """Everything shown this week Done, and work in today's planning window that this week
    shows nowhere: due next week, given out before it."""
    store = store_of(client)
    store.put_on_record([due("next-week-set", "Next week's set", date(2026, 8, 25))], {})
    for item in store.all_assignments():
        if item.assignment_id != "next-week-set":
            reported(store, "done", item.assignment_id)


def an_empty_week(client: TestClient) -> None:
    store = store_of(client)
    off_the_record(store, *[item.assignment_id for item in store.all_assignments()])


OMITTED: Final[dict[str, tuple[Callable[[TestClient], None], bool, tuple[str, ...]]]] = {
    "every assignment Done": (
        lambda client: everything_done(store_of(client)),
        False,
        (NO_PLAN_YET, NOTHING_TO_SCHEDULE, "<summary>Reported done (5)</summary>"),
    ),
    "an empty week": (an_empty_week, False, (NO_PLAN_YET, NOTHING_TO_SCHEDULE, EMPTY_WEEK)),
    "this week Done, later work planned": (
        later_work_keeps_the_plan_button,
        False,
        (NO_PLAN_YET, "<summary>Reported done (5)</summary>"),
    ),
    "a plan today": (
        lambda client: None,
        True,
        ("A plan is ready.", "<summary>Today's saved plan"),
    ),
}


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("case", list(OMITTED))
def test_see_homework_is_left_out_with_a_plan_or_nothing_left_and_the_page_says_what_stands(
    case: str, reader: str, tmp_path: pathlib.Path
) -> None:
    make, with_a_plan, lines = OMITTED[case]
    graphs = planner(fixture_week_plan) if with_a_plan or case.startswith("this week") else None
    with reading(reader, tmp_path, graphs=graphs) as client:
        make(client)
        if with_a_plan:
            assert client.post(PLAN).status_code == 303
        page = main_of(page_of(client))

    assert "See homework" not in page
    for line in lines:
        assert line in page, line
    today = page[at(page, TODAY) : page.index("</section>", at(page, TODAY))]
    assert '<a href="/student/to-turn-in">To turn in</a>' in today
    assert '<a href="/student/homework-notes">Homework notes</a>' in today
    assert ("Write down homework</a>" in today) == (reader != "a parent")
    plan_button = 'action="/student/actions/plan"' in page
    assert plan_button == (case in ("this week Done, later work planned", "a plan today"))
    if case == "this week Done, later work planned":
        assert "Nothing to schedule from the work in this planning window." not in page
    assert "autofocus" not in page


# ------------------------------------------------------------------ anchors


@pytest.mark.parametrize("reader", READERS)
def test_every_place_the_week_links_to_is_there_to_land_on(
    reader: str, tmp_path: pathlib.Path
) -> None:
    with reading(reader, tmp_path, graphs=planner(fixture_week_plan)) as client:
        quiet(client)
        asked_for_help(client)
        assert client.post(PLAN).status_code == 303
        page = main_of(page_of(client, show_plan="1"))
        other = main_of(page_of(client, week=LATER_WEEK))

    assert lands_on(page, "#homework") == HEADING.removesuffix("Due this week</h2>")
    assert lands_on(other, "#homework") == THAT_WEEK.removesuffix("Due that week</h2>")
    for fragment in ("today", "to-turn-in", "homework-notes", "help", "ask-for-help"):
        assert 'tabindex="-1"' in lands_on(page, f"#{fragment}"), fragment
    assert lands_on(page, "#todays-plan") == '<div id="todays-plan" tabindex="-1">'
    assert page.count('id="homework"') == 1
    assert "See homework" not in page


def test_the_homework_heading_is_an_outlined_place_to_land() -> None:
    """The heading, a card or a row due later, the top line, a card's problem line, the To
    turn in list's problem line and a result line each show an outline when they take the
    focus, and the top line's link is as tall as a control."""
    selectors = outlined()
    for selector in (
        ".list-heading:focus",
        '.assignment[tabindex="-1"]:focus',
        '.assigned li[tabindex="-1"]:focus',
        ".week-problem:focus",
        '.update .problem[tabindex="-1"]:focus',
        "#to-turn-in-problem:focus",
        ".update-result:focus",
    ):
        assert selector in selectors, selector
    assert ".week-problem a" in in_sentence_rule()


# ------------------------------------------------------------------ the sentence on another week


def test_another_week_says_updates_are_the_latest_whenever_it_shows_a_card() -> None:
    """A week with its own homework, and a week whose only card is one shown apart, both say
    that updates are the latest saved; the current week and a week with nothing do not."""
    with reading("sign-in off", pathlib.Path()) as client:
        off_the_record(store_of(client), SYLLABUS)
        normal = main_of(page_of(client, week=LATER_WEEK))
        apart_only = main_of(page_of(client, week=FAR_WEEK, show=ESSAY_ID))
        empty = main_of(page_of(client, week=FAR_WEEK))
        current = main_of(page_of(client))

    assert OTHER_WEEK_SENTENCE in normal
    assert OTHER_WEEK_SENTENCE in apart_only
    assert "<h2>Outside the week shown</h2>" in apart_only
    assert "No assignments are recorded as due that week." in apart_only
    assert apart_only.index("<h2>Outside the week shown</h2>") < apart_only.index(THAT_WEEK)
    assert apart_only.index(THAT_WEEK) < apart_only.index(OTHER_WEEK_SENTENCE)
    assert OTHER_WEEK_SENTENCE not in empty
    assert "No assignments are recorded as due that week." in empty
    assert OTHER_WEEK_SENTENCE not in current


# ------------------------------------------------------------------ a card's answer (E3)


def conflict_on_done(client: TestClient) -> Answer:
    reported(store_of(client), "done")
    card = week_card(client)
    reported(store_of(client), "not_yet")
    reported(store_of(client), "done")
    return save(client, card, "done", "Mine.")


def conflict_apart(client: TestClient) -> Answer:
    card = week_card(client)
    store = store_of(client)
    store._connection.execute(
        "UPDATE assignments SET due_date = ? WHERE assignment_id = ?", ("2026-08-28", ESSAY_ID)
    )
    store._connection.execute("DELETE FROM date_claims WHERE assignment_id = ?", (ESSAY_ID,))
    store._connection.commit()
    reported(store, "not_yet")
    return save(client, card, "done", "Mine.")


def conflict_due_later(client: TestClient) -> Answer:
    card = week_card(client, LOG)
    reported(store_of(client), "not_yet", LOG)
    return save(client, card, "done", "Mine.", LOG)


def conflict_due_later_done(client: TestClient) -> Answer:
    reported(store_of(client), "done", LOG)
    card = week_card(client, LOG)
    reported(store_of(client), "not_yet", LOG)
    reported(store_of(client), "done", LOG)
    return save(client, card, "done", "Mine.", LOG)


def saved_apart(client: TestClient) -> Answer:
    card = week_card(client, week=LATER_WEEK)
    return save(client, card, "not_yet")


CARD_LINE: Final = f'<p class="problem" role="alert" id="update-problem-{ESSAY_ID}" tabindex="-1"'
LOG_LINE: Final = f'<p class="problem" role="alert" id="update-problem-{LOG}" tabindex="-1"'
RADIO: Final = '<input type="radio" name="status" value="done" autofocus>'
NOTE_FIELD: Final = f'<textarea id="update-note-{ESSAY_ID}" name="note" rows="2"'
# case: (make the response, status, the tag that takes the focus, the problem's words,
# the assignment it is about)
REFUSALS: Final[dict[str, tuple[Callable[[TestClient], Answer], int, str, str, str]]] = {
    "conflict": (conflict, 409, CARD_LINE, SAVED_ELSEWHERE, ESSAY_ID),
    "conflict on a Done card": (conflict_on_done, 409, CARD_LINE, SAVED_ELSEWHERE, ESSAY_ID),
    "conflict on the card shown apart": (conflict_apart, 409, CARD_LINE, SAVED_ELSEWHERE, ESSAY_ID),
    "conflict on a row due later": (conflict_due_later, 409, LOG_LINE, SAVED_ELSEWHERE, LOG),
    "conflict on a Done row due later": (
        conflict_due_later_done,
        409,
        LOG_LINE,
        SAVED_ELSEWHERE,
        LOG,
    ),
    "stale undo": (
        stale_undo,
        409,
        CARD_LINE,
        "Your update has changed, so it cannot be undone from that page.",
        ESSAY_ID,
    ),
    "already undone": (
        already_undone,
        409,
        CARD_LINE,
        "That update was already undone. The card shows what stands now.",
        ESSAY_ID,
    ),
    "form not whole": (malformed, 422, CARD_LINE, "That form carried a field twice", ESSAY_ID),
    "bad return": (
        bad_return,
        422,
        CARD_LINE,
        "The form named a page to go back to that these pages do not make.",
        ESSAY_ID,
    ),
    "not this card's": (
        not_this_cards,
        422,
        CARD_LINE,
        "That form names an update this assignment does not have",
        ESSAY_ID,
    ),
    "failed write": (
        failed_write,
        500,
        CARD_LINE,
        "Your update could not be saved. Your words are still here. Try again.",
        ESSAY_ID,
    ),
    "failed undo": (
        failed_undo,
        500,
        CARD_LINE,
        "Your update could not be undone, and nothing was changed. Try again.",
        ESSAY_ID,
    ),
    "no choice": (no_choice, 422, RADIO, "Choose Done or Not yet.", ESSAY_ID),
    "note too long": (note_too_long, 422, NOTE_FIELD, "Keep your note to 500", ESSAY_ID),
}


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
@pytest.mark.parametrize("case", list(REFUSALS))
def test_a_refusal_about_a_card_is_said_on_the_card_which_takes_the_focus(
    case: str, reader: str, tmp_path: pathlib.Path
) -> None:
    """The card's line is the one alert and the one focus, or its field is; the top of the
    page says it again as a plain line with a link to the card. A fold that holds the card
    is open."""
    make, status, focus, said, about = REFUSALS[case]
    with reading(reader, tmp_path) as client:
        answer = make(client)

    page = main_of(answer.text)
    assert answer.status_code == status, answer.text[:300]
    asked = one_focus(answer)
    assert asked.startswith(focus), asked
    assert page.count('role="alert"') == 1
    line = tag_with_id(page, f"update-problem-{about}")
    assert line.startswith(f'<p class="problem" role="alert" id="update-problem-{about}"')
    assert 'tabindex="-1"' in line
    assert ("autofocus" in line) == (focus in (CARD_LINE, LOG_LINE))
    assert top_line(page).startswith(TOP_BESIDE_A_CARD)
    assert said in words(top_line(page))
    assert f'<a href="#assignment-{about}">Go to the assignment.</a></p>' in top_line(page)
    assert "tabindex" not in top_line(page).split(">", 1)[0]
    assert said in words(page[page.index(line) :])
    assert lands_on(page, f"#assignment-{about}")
    if "apart" in case:
        assert page.index('<section class="panel apart">') < page.index(line)
    if "Done" in case:
        assert re.search(r'<details class="steps reported-done" open>', page)


SUCCESSES: Final[dict[str, tuple[Callable[[TestClient], Answer], str, str]]] = {
    "a save": (saved, "saved", "Your update is saved."),
    "the same save again": (saved_again, "same", "Your update is already saved."),
    "an undo": (undone, "undone", "Your update is undone."),
    "a save on a card shown apart": (saved_apart, "saved", "Your update is saved."),
}


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
@pytest.mark.parametrize("case", list(SUCCESSES))
def test_a_save_or_an_undo_lands_on_the_cards_result_and_asks_for_no_focus(
    case: str, reader: str, tmp_path: pathlib.Path
) -> None:
    make, word, said = SUCCESSES[case]
    with reading(reader, tmp_path) as client:
        answer = make(client)
        landed = after(client, answer)

    location = answer.headers["location"]
    assert urlsplit(location).fragment == f"update-result-{ESSAY_ID}"
    assert parse_qs(urlsplit(location).query)[word] == [ESSAY_ID]
    page = main_of(landed.text)
    target = lands_on(page, location)
    assert target == (
        f'<p class="note update-result" role="status" id="update-result-{ESSAY_ID}" tabindex="-1">'
    )
    assert said in words(page[page.index(target) :])
    assert page.index(target) > page.index(f'id="assignment-{ESSAY_ID}"')
    assert folds_open_around(page, target)
    assert outline_reaches(page, target)
    assert focused_on_arrival(landed.text) == []
    assert 'role="alert"' not in page
    if "apart" in case:
        assert page.index('<section class="panel apart">') < page.index(target)


# ------------------------------------------------------------------ the details keep their own


DETAILS_SUMMARY: Final = '<p class="problem" role="alert" id="problem-summary" tabindex="-1"'


def details_conflict(client: TestClient, **back: str) -> Answer:
    page = client.get(DETAILS, params={"return_to": "week", **back}, headers=PAGE_HEADERS).text
    fields = form_fields(page, REPORT)
    assert client.post(REPORT, data={**fields, "status": "not_yet", "note": ""}).status_code == 303
    return client.post(
        REPORT, data={**fields, "status": "done", "note": "Mine."}, headers=PAGE_HEADERS
    )


def details_no_choice(client: TestClient) -> Answer:
    page = client.get(DETAILS, params={"return_to": "week"}, headers=PAGE_HEADERS).text
    return client.post(REPORT, data={**form_fields(page, REPORT), "note": ""}, headers=PAGE_HEADERS)


@pytest.mark.parametrize(
    ("case", "make", "focus"),
    [
        ("conflict", details_conflict, DETAILS_SUMMARY),
        (
            "conflict, back to a week the work is not in",
            lambda client: details_conflict(client, week="2026-09-14"),
            DETAILS_SUMMARY,
        ),
        ("no choice", details_no_choice, RADIO),
    ],
)
def test_on_the_details_the_component_asks_for_no_focus_of_its_own(
    case: str, make: Callable[[TestClient], Answer], focus: str
) -> None:
    """The same component on the details: its problem line stays plain text the summary
    links to, and the one focus is the summary's or the field's."""
    with reading("sign-in off", pathlib.Path()) as client:
        answer = make(client)

    page = main_of(answer.text)
    asked = focused_on_arrival(answer.text)
    assert len(asked) == 1
    assert asked[0].startswith(focus), case
    assert page.count('role="alert"') == 1
    assert tag_with_id(page, f"update-problem-{ESSAY_ID}") == (
        f'<p class="problem" id="update-problem-{ESSAY_ID}" tabindex="-1">'
    )
    assert "week-problem" not in page


# ------------------------------------------------------------------ no card line


def stale_pages(client: TestClient) -> dict[str, tuple[str, list[tuple[str, str]]]]:
    """Her pages as she left them, each press's action and the form it would send: her
    week, the essay's details, and her To turn in list, with an update, a hand-in update,
    her signal for today and a press on the list standing, so every Undo is offered."""
    store = store_of(client)
    reported(store, "done")
    to_turn_in(store)
    to_turn_in(store, "assignment-science-fair-proposal")
    state_of(client).workload_signals.record(PLAN_DATE, None)
    week = page_of(client)
    card = card_for(week, ESSAY_ID)
    listed = client.get(TO_TURN_IN_PAGE, headers=PAGE_HEADERS).text
    row = listed[listed.index('id="to-turn-in-assignment-science-fair-proposal"') :]
    science = "/student/actions/assignments/assignment-science-fair-proposal"
    pressed = client.post(
        f"{science}/hand-in", data=form_fields(row, f"{science}/hand-in"), headers=PAGE_HEADERS
    )
    result = client.get(pressed.headers["location"], headers=PAGE_HEADERS).text
    details = client.get(DETAILS, params={"return_to": "week"}, headers=PAGE_HEADERS).text
    editing = client.get(
        DETAILS, params={"return_to": "week", "change": "1"}, headers=PAGE_HEADERS
    ).text
    opened = client.get(
        DETAILS, params={"return_to": "week", "hand_in": "change"}, headers=PAGE_HEADERS
    ).text
    signal = re.search(r'action="(/student/actions/take-back/[^"]+)"', week)
    assert signal is not None
    week_row = week[week.index(f'id="to-turn-in-{ESSAY_ID}"') :]
    change = week_card(client)
    chosen = [("status", "not_yet"), ("note", "Wren's words")]
    return {
        "a save from her week": (REPORT, [*form_fields(change, REPORT).items(), *chosen]),
        "an Undo from her week": (UNDO, list(form_fields(card, UNDO).items())),
        "a save from the details": (REPORT, [*form_fields(editing, REPORT).items(), *chosen]),
        "an Undo from the details": (UNDO, list(form_fields(details, UNDO).items())),
        "a hand-in from the details": (HAND_IN, list(whole_form(opened, HAND_IN).items())),
        "a hand-in Undo from the details": (
            UNDO_HAND_IN,
            list(form_fields(details, UNDO_HAND_IN).items()),
        ),
        "I turned it in on her week's list": (
            HAND_IN,
            list(form_fields(week_row, HAND_IN).items()),
        ),
        "an Undo on the To turn in page": (
            f"{science}/undo-hand-in",
            list(form_fields(result, f"{science}/undo-hand-in").items()),
        ),
        "Too much right now": (TOO_MUCH, []),
        "Take it back": (signal.group(1), []),
        "Remove, in what Blossom keeps": (signal.group(1), []),
    }


STALE: Final = (
    "a save from her week",
    "an Undo from her week",
    "a save from the details",
    "an Undo from the details",
    "a hand-in from the details",
    "a hand-in Undo from the details",
    "I turned it in on her week's list",
    "an Undo on the To turn in page",
    "Too much right now",
    "Take it back",
    "Remove, in what Blossom keeps",
)


def check_top_focus(answer: Answer, said: str) -> None:
    """The refusal said once, at the top, as the alert and the one focus, with no card link."""
    page = main_of(answer.text)
    asked = one_focus(answer)
    assert asked == TOP_FOCUSED, asked
    assert top_line(page) == f"{TOP_FOCUSED}{said}</p>"
    assert page.count('role="alert"') == 1
    assert "Go to the assignment." not in page
    assert page.index(TOP_FOCUSED) < page.index('<section class="panel today"')


@pytest.mark.parametrize("press", STALE)
def test_a_page_of_hers_pressed_after_a_parent_signs_in_is_refused_at_the_top_with_the_focus(
    press: str, tmp_path: pathlib.Path
) -> None:
    """Her week, details and list left open on the device, a parent signs in, and a press
    on the page she left answers with her current week, 403, the refusal at the top taking
    the focus. Nothing is written."""
    with reading("her", tmp_path) as client:
        forms = stale_pages(client)
        as_a_parent(client)
        before = everything_kept(client)
        action, form = forms[press]
        answer = client.post(action, data=dict(form), headers=PAGE_HEADERS)
        after_it = everything_kept(client)

    assert answer.status_code == 403
    assert after_it == before
    assert "<h1>Student week</h1>" in answer.text
    said = NOT_HERS_TO_SIGNAL if press in STALE[-3:] else NOT_HERS
    check_top_focus(answer, said)


def gone_undo(client: TestClient) -> Answer:
    reported(store_of(client), "done")
    card = card_for(page_of(client), ESSAY_ID)
    off_the_record(store_of(client), ESSAY_ID)
    return client.post(UNDO, data=form_fields(card, UNDO), headers=PAGE_HEADERS)


def not_on_record(client: TestClient) -> Answer:
    """A form made by hand for an id that is not on record, refused before any lookup: no
    card holds it on the page."""
    return client.post(
        f"/student/actions/assignments/{NOWHERE}/report",
        data={"status": "done", "note": ["one", "two"], "expected_report_id": ""},
        headers=PAGE_HEADERS,
    )


def plan_without_a_key(client: TestClient) -> Answer:
    return client.post(PLAN, headers=PAGE_HEADERS)


PLAN_REFUSED: Final = {
    "no model to ask": (
        None,
        503,
        "Blossom could not make a plan: no model can be constructed: ANTHROPIC_API_KEY is not set",
    ),
    "no plan fits": (
        planner(forgetful_fixture_plan),
        409,
        "Blossom couldn&#39;t make a plan that fits this evening.",
    ),
    "a failure on the way": (
        scripted_graphs(list, lambda: [accepting()]),
        500,
        "Blossom could not make a plan: something went wrong on the way.",
    ),
}


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
def test_an_undo_on_work_gone_from_the_record_is_refused_at_the_top_with_the_focus(
    reader: str, tmp_path: pathlib.Path
) -> None:
    with reading(reader, tmp_path) as client:
        answer = gone_undo(client)

    assert answer.status_code == 404
    check_top_focus(answer, NOT_ON_RECORD)


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
def test_a_refusal_about_work_no_card_shows_is_said_at_the_top_with_the_focus(
    reader: str, tmp_path: pathlib.Path
) -> None:
    """With no card on the page to say it on, a card's refusal is the top line's: the alert
    and the focus, with no link to a card that is not there."""
    with reading(reader, tmp_path) as client:
        answer = not_on_record(client)

    assert answer.status_code == 422
    assert f'id="assignment-{NOWHERE}"' not in answer.text
    check_top_focus(
        answer,
        "That form carried a field twice, or one this page does not send, so nothing was "
        "saved. Choose and save again.",
    )


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
@pytest.mark.parametrize("case", list(PLAN_REFUSED))
def test_a_plan_the_button_could_not_make_is_said_at_the_top_with_the_focus(
    case: str, reader: str, tmp_path: pathlib.Path
) -> None:
    """The plan button's three refusals, from scripted graphs: the top line takes the
    focus, and See homework is there below, since work is left and no plan was made."""
    graphs, status, said = PLAN_REFUSED[case]
    with reading(reader, tmp_path, graphs=graphs) as client:
        answer = client.post(PLAN, headers=PAGE_HEADERS)

    assert answer.status_code == status
    page = main_of(answer.text)
    asked = one_focus(answer)
    assert asked == TOP_FOCUSED
    assert top_line(page).startswith(TOP_FOCUSED + said), top_line(page)
    assert page.count('role="alert"') == 1
    assert see_homework(FAIR) in page


@pytest.mark.parametrize("week", ["not a day", "", "0001-01-01"])
def test_a_week_address_refused_on_a_visit_is_said_at_the_top_and_asks_for_no_focus(
    week: str,
) -> None:
    with reading("sign-in off", pathlib.Path()) as client:
        answer = client.get(HER_PAGE, params={"week": week}, headers=PAGE_HEADERS)

    assert answer.status_code == 422
    page = main_of(answer.text)
    assert top_line(page).startswith(TOP_PLAIN_ALERT)
    assert focused_on_arrival(answer.text) == []
    assert page.count('role="alert"') == 1


@pytest.mark.parametrize("address", ["", "?show=assignment-canal-essay", "?refreshed=1"])
@pytest.mark.parametrize("reader", READERS)
def test_an_ordinary_visit_asks_for_no_focus(
    address: str, reader: str, tmp_path: pathlib.Path
) -> None:
    with reading(reader, tmp_path) as client:
        crowded(client)
        answer = client.get(HER_PAGE + address, headers=PAGE_HEADERS)

    assert answer.status_code == 200
    assert focused_on_arrival(answer.text) == []
    assert "week-problem" not in answer.text


def test_a_parents_refused_request_for_help_keeps_its_own_place_and_focus(
    tmp_path: pathlib.Path,
) -> None:
    """Help's refusals are said in Help: nothing at the top of the week takes the focus."""
    with reading("her", tmp_path) as client:
        week = page_of(client)
        as_a_parent(client)
        answer = client.post(
            "/student/actions/ask-for-help",
            data=form_fields(week, "/student/actions/ask-for-help"),
            headers=PAGE_HEADERS,
        )

    assert answer.status_code == 403
    asked = focused_on_arrival(answer.text)
    assert len(asked) == 1
    assert asked[0].startswith('<p class="problem" role="alert" id="help-problem"')
    assert "week-problem" not in answer.text


def test_a_save_from_her_week_on_work_gone_meanwhile_keeps_the_small_page_and_its_focus() -> None:
    with reading("sign-in off", pathlib.Path()) as client:
        card = week_card(client)
        off_the_record(store_of(client), ESSAY_ID)
        answer = save(client, card, "done", "Typed before it went.")

    assert answer.status_code == 404
    assert focused_on_arrival(answer.text) == [
        '<p class="problem" role="alert" id="problem-summary" tabindex="-1" autofocus>'
    ]
    assert "week-problem" not in answer.text


LIST_LINE: Final = (
    '<p class="problem" role="alert" id="to-turn-in-problem" tabindex="-1" autofocus>'
)
LISTS: Final = {"her week": HER_PAGE, "the To turn in page": TO_TURN_IN_PAGE}


def test_a_refused_press_on_her_weeks_list_keeps_the_lists_own_place_and_focus() -> None:
    """A press on her week's To turn in list that the list refuses, the row having moved on
    in another tab, is said in the list, after the homework, and takes the focus there
    alone."""
    with reading("sign-in off", pathlib.Path()) as client:
        to_turn_in(store_of(client))
        week = page_of(client)
        row = week[week.index(f'id="to-turn-in-{ESSAY_ID}"') :]
        to_turn_in(store_of(client), step="Hand it to the teacher")
        answer = client.post(HAND_IN, data=form_fields(row, HAND_IN), headers=PAGE_HEADERS)

    assert answer.status_code == 409
    page = main_of(answer.text)
    assert one_focus(answer) == LIST_LINE
    assert "week-problem" not in page
    assert page.index(HEADING) < page.index(LIST_LINE)


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
@pytest.mark.parametrize("press", ["I turned it in", "Undo"])
@pytest.mark.parametrize("where", list(LISTS))
def test_a_press_on_the_list_for_homework_gone_is_said_on_the_lists_own_line_with_the_focus(
    where: str, press: str, reader: str, tmp_path: pathlib.Path
) -> None:
    """The list's press or its Undo, sent after the homework left the record in another tab,
    on her week and on the list's own page: the list's own line says so, as the one alert and
    the one focus, outlined. No line at the top says it again, and nothing links to the row
    or the card that is gone."""
    with reading(reader, tmp_path) as client:
        store = store_of(client)
        to_turn_in(store)
        page = client.get(LISTS[where], headers=PAGE_HEADERS).text
        row = page[page.index(f'id="to-turn-in-{ESSAY_ID}"') :]
        action, fields = HAND_IN, form_fields(row, HAND_IN)
        if press == "Undo":
            pressed = client.post(action, data=fields, headers=PAGE_HEADERS)
            result = client.get(pressed.headers["location"], headers=PAGE_HEADERS).text
            action, fields = UNDO_HAND_IN, form_fields(result, UNDO_HAND_IN)
        off_the_record(store, ESSAY_ID)
        answer = client.post(action, data=fields, headers=PAGE_HEADERS)

    assert answer.status_code == 404
    page = main_of(answer.text)
    assert one_focus(answer) == LIST_LINE
    assert words(page[page.index(LIST_LINE) : page.index("</p>", page.index(LIST_LINE))]) == (
        GONE_FROM_THE_LIST
    )
    assert page.count('role="alert"') == 1
    assert "week-problem" not in page
    assert "Go to the assignment." not in page
    assert re.search(rf'href="[^"]*{ESSAY_ID}', page) is None
    assert f'id="to-turn-in-{ESSAY_ID}"' not in page
    if where == "her week":
        assert page.index(HEADING) < page.index(LIST_LINE)
