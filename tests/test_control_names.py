# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Every control says its own words. The name a screen reader or voice control uses holds the
words a button or link shows, as WCAG 2.5.3 asks, and Blossom's names start with them. Words
hidden from sight start with a space, since a browser sets them off with one, and a name that
goes on with punctuation is said whole in a label. The sign-in field and the family page's
notices use the shared field and wrapping rules."""

import pathlib
import re
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.hand_in import NEEDS_HAND_IN, TURNED_IN, HandInSaved, HandInState
from blossom.routes.navigation import TO_TURN_IN_PAGE, assignment_anchor, title_anchor
from blossom.routes.runs import plan_graphs
from blossom.settings import REPOSITORY_ROOT, TEMPLATE_PATH
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    DETAILS,
    ESSAY,
    ESSAY_ID,
    ESSAY_TITLE,
    HER_PAGE,
    NOW,
    PAGE_HEADERS,
    PLAN_DATE,
    THEIRS,
    accepting,
    browser,
    card_for,
    client_for,
    control_names,
    due,
    family_plan,
    field_names,
    fixture_week_plan,
    form_fields,
    household_client,
    main_of,
    names_not_led_by_their_words,
    names_without_their_words,
    report,
    school_missing,
    scripted_graphs,
    sign_in_as,
    signed_in,
    signed_in_household,
    store_of,
    whole_form,
)

HAND_IN = f"/student/actions/assignments/{ESSAY_ID}/hand-in"
STATES = (
    "week, new",
    "details, new",
    "week, change",
    "details, change",
    "week, saved",
    "details, saved",
    "details, hand-in history",
    "week, to check",
    "hand-in, not now",
    "hand-in, keep",
    "to turn in",
    "family",
    "family, hand-in",
    "week, help",
    "week, help, parent",
)
HELP_STATES = ("week, help", "week, help, parent")
"""Her week's help, where no control is repeated per assignment."""
REPEATED = (
    "Done",
    "Not yet",
    "Add a note (optional)",
    "Save update",
    "Keep it as it is",
    "Change",
    "Undo last update",
    "Update history",
    "Hand-in history",
    "Read instructions",
    "Not now",
    "Open Turning it in",
    "Review and update in Turning it in",
    "Looks good",
)


def with_an_instruction(client: TestClient, assignment_id: str) -> None:
    """One instruction of the school's kept for an assignment, which applies as a first one
    does, so its cards offer Read instructions."""
    store = store_of(client)
    item = store.one_assignment(assignment_id)
    assert item is not None
    store.put_on_record([item.model_copy(update={"note": "Bring the packet."})], {})


@pytest.fixture
def pages(tmp_path: pathlib.Path) -> dict[str, str]:
    """Her week, the details, To turn in and the family page, in every state that shows a
    control whose name adds context to its words, the essay's histories and its school
    report to check among them, and her week's help as she and a parent read it."""
    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [fixture_week_plan()], lambda: [accepting()]
        )
        with_an_instruction(client, ESSAY_ID)
        client.post("/parent/actions/plan", data=family_plan(client, PLAN_DATE.isoformat()))
        shown = {"family": client.get("/parent").text}
        shown["week, new"] = client.get(HER_PAGE).text
        shown["details, new"] = client.get(DETAILS).text
        report(client, ESSAY_ID, "done")
        shown["week, saved"] = client.get(HER_PAGE).text
        shown["details, saved"] = client.get(DETAILS).text
        shown["week, change"] = client.get(HER_PAGE, params={"change": ESSAY_ID}).text
        shown["details, change"] = client.get(DETAILS, params={"change": "1"}).text
        opened = client.get(DETAILS, params={"hand_in": "change"}).text
        shown["hand-in, not now"] = opened
        state = re.findall(r'name="state" value="([^"]+)"', opened)[0]
        client.post(
            HAND_IN, data={**whole_form(opened, HAND_IN), "state": state}, headers=PAGE_HEADERS
        )
        shown["hand-in, keep"] = client.get(DETAILS, params={"hand_in": "change"}).text
        shown["to turn in"] = client.get("/student/to-turn-in").text
        shown["family, hand-in"] = client.get("/parent").text
        client.post("/student/help-requests", json={"note": "Synthetic question"})
        made = client.post("/student/help-requests", json={"note": "Synthetic closed"})
        client.post(f"/parent/help-requests/{made.json()['request']['request_id']}/resolve")
        shown["week, help"] = client.get(HER_PAGE).text
        opened = client.get(DETAILS, params={"hand_in": "change"}).text
        state = re.findall(r'name="state" value="([^"]+)"', opened)[1]
        client.post(
            HAND_IN, data={**whole_form(opened, HAND_IN), "state": state}, headers=PAGE_HEADERS
        )
        shown["details, hand-in history"] = client.get(DETAILS).text
        store_of(client).record_status_reports(ESSAY_ID, [school_missing(PLAN_DATE)])
        shown["week, to check"] = client.get(HER_PAGE).text
    with client_for(signed_in_household(tmp_path)) as client:
        signed_in(client, THEIRS)
        shown["week, help, parent"] = client.get(HER_PAGE).text
    return shown


def test_the_states_show_the_controls_they_are_for(pages: dict[str, str]) -> None:
    assert ">Done</button>" in pages["week, new"]
    assert ">Not yet</button>" in pages["week, new"]
    assert ">Save update" in pages["week, change"]
    assert ">Keep it as it is" in pages["details, change"]
    assert ">Change<span" in pages["week, saved"]
    assert ">Undo last update<span" in pages["details, saved"]
    assert "<summary>Update history<span" in pages["details, saved"]
    assert "<summary>Hand-in history<span" in pages["details, hand-in history"]
    assert ": school report to check" in pages["week, to check"]
    assert ">Not now" in pages["hand-in, not now"]
    assert ">Keep it as it is" in pages["hand-in, keep"]
    assert 'aria-label="Turning it in: ' not in pages["to turn in"]
    assert ">Looks good" in pages["family"]
    assert ">Take it back</button>" in pages["week, help"]
    assert ">Ask again<" in pages["week, help"]
    assert ">Help updates (2)<" in pages["week, help"]
    assert ">Her help requests<" in pages["week, help, parent"]


def test_every_control_s_name_holds_the_words_it_shows(pages: dict[str, str]) -> None:
    assert set(pages) == set(STATES)
    failing = {state: names_without_their_words(page) for state, page in pages.items()}
    assert {state: found for state, found in failing.items() if found} == {}


def essay_part(page: str) -> str:
    """The essay's card on a page of several, after the line of school reports to check when
    the page has one, or else the whole page."""
    if f'id="{assignment_anchor(ESSAY_ID)}"' in page:
        line = re.search(
            r'<p class="confidence disagree(?: to-check)?" role="status">.*?</p>', page, re.S
        )
        return (line.group(0) if line else "") + card_for(page, ESSAY_ID)
    return page


def test_each_repeated_control_names_what_it_is_about(pages: dict[str, str]) -> None:
    """A control shown once per assignment names its title and course, and Looks good its plan."""
    about = f"{ESSAY_TITLE}, {ESSAY.course}"
    repeated = {
        state: [
            (words, name)
            for words, name in control_names(essay_part(page))
            if words in REPEATED or words == ESSAY_TITLE
        ]
        for state, page in pages.items()
        if state not in HELP_STATES
    }
    bare = {
        state: [
            (words, name)
            for words, name in found
            if about not in name and not name.startswith("Looks good: the plan for ")
        ]
        for state, found in repeated.items()
    }
    turning_in = [
        name for words, name in control_names(pages["family, hand-in"]) if words == ESSAY_TITLE
    ]

    assert [state for state, found in repeated.items() if not found] == []
    assert {state: found for state, found in bare.items() if found} == {}
    assert any(name.endswith("Turning it in") for name in turning_in)


PRACTICE_COURSES = ("History", "Science", "Art & <Design>")
NAMESAKE_NAMES = (
    ("week, new", "Done", "Done: Practice, {}"),
    ("week, new", "Not yet", "Not yet: Practice, {}"),
    ("week, new", "Add a note (optional)", "Add a note (optional) on Practice, {}"),
    ("details, new", "Add a note (optional)", "Add a note (optional) on Practice, {}"),
    ("week, change", "Save update", "Save update for Practice, {}"),
    ("week, new", "Read instructions", "Read instructions: Practice, {}"),
    ("week, change", "Read instructions", "Read instructions: Practice, {}"),
    ("details, new", "Save update", "Save update for Practice, {}"),
    ("week, change", "Keep it as it is", "Keep it as it is: your update on Practice, {}"),
    ("details, change", "Keep it as it is", "Keep it as it is: your update on Practice, {}"),
    ("update, returned", "Save update", "Save update for Practice, {}"),
    ("update, returned", "Keep it as it is", "Keep it as it is: your update on Practice, {}"),
    ("hand-in, not now", "Not now", "Not now: the hand-in status for Practice, {}"),
    ("hand-in, returned", "Not now", "Not now: the hand-in status for Practice, {}"),
    ("hand-in, keep", "Keep it as it is", "Keep it as it is: the hand-in status for Practice, {}"),
    ("week, saved", "Change", "Change your update on Practice, {}"),
    ("week, saved", "Undo last update", "Undo last update on Practice, {}"),
    ("week, saved", "Update history", "Update history for Practice, {}"),
    ("details, saved", "Change", "Change your update on Practice, {}"),
    ("details, saved", "Undo last update", "Undo last update on Practice, {}"),
    ("details, saved", "Update history", "Update history for Practice, {}"),
    ("details, hand-in history", "Hand-in history", "Hand-in history for Practice, {}"),
    ("week, to check", "Practice", "Practice, {}: school report to check"),
)


@pytest.fixture
def namesakes() -> dict[str, dict[str, str]]:
    """Three assignments called Practice, in three courses and due this week, each seen in
    every state that shows a control repeated per assignment: its card, its page, or its
    link among the school reports to check."""
    shown: dict[str, dict[str, str]] = {}
    with browser(key=True) as client:
        for course in PRACTICE_COURSES:
            kept = client.post(
                "/parent/inbox/keep",
                data={"course": course, "title": "Practice", "due_date": "2026-08-21"},
            )
            assert kept.status_code == 303, kept.text[:300]
        for item in store_of(client).all_assignments():
            if item.title != "Practice":
                continue
            key, details = item.assignment_id, f"/student/assignments/{item.assignment_id}"
            update = f"/student/actions/assignments/{key}/report"
            hand_in = f"/student/actions/assignments/{key}/hand-in"
            seen = shown[item.course] = {}
            with_an_instruction(client, key)
            seen["week, new"] = card_for(client.get(HER_PAGE).text, key)
            seen["details, new"] = client.get(details).text
            report(client, key, "done")
            seen["week, saved"] = card_for(client.get(HER_PAGE).text, key)
            seen["details, saved"] = client.get(details).text
            card = card_for(client.get(HER_PAGE, params={"change": key}).text, key)
            seen["week, change"] = card
            seen["details, change"] = client.get(details, params={"change": "1"}).text
            too_long = {**whole_form(card, update), "status": "done", "note": "x" * 501}
            returned = client.post(update, data=too_long, headers=PAGE_HEADERS)
            assert returned.status_code == 422, returned.text[:300]
            seen["update, returned"] = card_for(returned.text, key)
            opened = client.get(details, params={"hand_in": "change"}).text
            seen["hand-in, not now"] = opened
            state = re.findall(r'name="state" value="([^"]+)"', opened)[0]
            chosen = {**whole_form(opened, hand_in), "state": state}
            too_long = {**chosen, "note": "x" * 501}
            returned = client.post(hand_in, data=too_long, headers=PAGE_HEADERS)
            assert returned.status_code == 422, returned.text[:300]
            seen["hand-in, returned"] = returned.text
            client.post(hand_in, data=chosen, headers=PAGE_HEADERS)
            seen["hand-in, keep"] = client.get(details, params={"hand_in": "change"}).text
            other = re.findall(r'name="state" value="([^"]+)"', seen["hand-in, keep"])[1]
            again = {**whole_form(seen["hand-in, keep"], hand_in), "state": other}
            assert client.post(hand_in, data=again, headers=PAGE_HEADERS).status_code == 303
            seen["details, hand-in history"] = client.get(details).text
            store_of(client).record_status_reports(key, [school_missing(PLAN_DATE)])
            seen["week, to check"] = to_check_link(client.get(HER_PAGE).text, key)
    return shown


def to_check_link(page: str, assignment_id: str) -> str:
    """The one link in the line of school reports to check that brings this assignment into
    view."""
    line = re.search(r'<p class="confidence disagree to-check" role="status">.*?</p>', page, re.S)
    assert line is not None
    found: list[str] = [
        link
        for link in re.findall(r"<a\b[^>]*>.*?</a>", line.group(0), re.S)
        if f'#{title_anchor(assignment_id)}"' in link
    ]
    assert len(found) == 1, found
    return found[0]


@pytest.mark.parametrize(("state", "words", "name"), NAMESAKE_NAMES)
def test_same_titled_assignments_are_told_apart_by_course(
    namesakes: dict[str, dict[str, str]], state: str, words: str, name: str
) -> None:
    named = {
        course: [heard for shown, heard in control_names(seen[state]) if shown == words]
        for course, seen in namesakes.items()
    }

    assert named == {course: [name.format(course)] for course in PRACTICE_COURSES}


FIELDS = ("Your update", "Your note")


@pytest.mark.parametrize("state", ["week, new", "details, new", "week, change", "details, change"])
def test_her_update_s_group_and_note_name_the_title_and_course(
    namesakes: dict[str, dict[str, str]], state: str
) -> None:
    """A card on her week with no update offers buttons that name the assignment, and the
    note beside them; every whole form names its group too."""
    named = {
        course: [heard for shown, heard in field_names(seen[state]) if shown in FIELDS]
        for course, seen in namesakes.items()
    }
    group = [] if state == "week, new" else ["Your update on Practice, {}"]

    assert named == {
        course: [name.format(course) for name in [*group, "Your note on Practice, {}"]]
        for course in PRACTICE_COURSES
    }


def asked_on(item: dict[str, str]) -> str:
    """The day and time a request was asked, its own, in the household's zone, as a control's
    name says them: the year only when it is not the page's."""
    at = datetime.fromisoformat(item["asked_local"])
    time = f"{at.hour % 12 or 12}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'}"
    year = "" if at.year == PLAN_DATE.year else f", {at.year}"
    return f"{at:%A, %B} {at.day}{year} at {time}"


def test_take_it_back_names_her_request_by_its_time_and_day_in_one_label() -> None:
    """The visible words, then the request's own day and time, in a label that reads as
    written. The page's day is pinned to the evening she asked for, which is another day than
    the one the real clock stamped."""
    with browser() as client:
        client.post("/student/help-requests", json={"note": "Synthetic question"})
        asked = client.get("/student/help-requests").json()[0]
        page = client.get(HER_PAGE).text
    name = f"Take it back: your request from {asked_on(asked)}"

    assert date.fromisoformat(asked["evening"]) == PLAN_DATE
    assert [heard for shown, heard in control_names(page) if shown == "Take it back"] == [name]
    assert f'aria-label="{name}">Take it back</button>' in page


def test_the_family_pages_help_buttons_name_her_request_by_its_own_day_and_time() -> None:
    """I can help, Add an update and Close request say their words, then the request they
    move, by the day and time it was asked, never the evening's day beside the request's
    time."""
    with browser() as client:
        client.post("/student/help-requests", json={"note": "Synthetic question"})
        asked = client.get("/parent/help-requests").json()[0]
        waiting = client.get("/parent").text
        client.post(f"/parent/help-requests/{asked['request_id']}/accept", json={})
        taken = client.get("/parent").text
    when = asked_on(asked)
    moves = ("I can help", "Add an update", "Close request")

    assert [(shown, heard) for shown, heard in control_names(waiting) if shown in moves] == [
        ("I can help", f"I can help with the request from {when}"),
        ("Close request", f"Close request from {when}"),
    ]
    assert [(shown, heard) for shown, heard in control_names(taken) if shown in moves] == [
        ("Add an update", f"Add an update to the request from {when}"),
        ("Close request", f"Close request from {when}"),
    ]


LABELED = (
    ("week, change", "Keep it as it is", "your update on"),
    ("details, change", "Keep it as it is", "your update on"),
    ("hand-in, not now", "Not now", "the hand-in status for"),
    ("hand-in, keep", "Keep it as it is", "the hand-in status for"),
)


@pytest.mark.parametrize(("state", "words", "about"), LABELED)
def test_keep_it_as_it_is_and_not_now_say_their_whole_name_in_a_label(
    pages: dict[str, str], state: str, words: str, about: str
) -> None:
    """Hidden words that start with punctuation get a stray space before them in a browser's
    name, so these say theirs in a label that reads as written."""
    label = f'aria-label="{words}: {about} {ESSAY_TITLE}, {ESSAY.course}">{words}</a>'

    assert label in pages[state]
    assert f"{words}<span" not in pages[state]


def test_every_control_s_name_starts_with_the_words_it_shows(pages: dict[str, str]) -> None:
    failing = {state: names_not_led_by_their_words(page) for state, page in pages.items()}
    assert {state: found for state, found in failing.items() if found} == {}


HIDDEN_WORDS = re.compile(r'class="visually-hidden">(?!\s)([^<]{0,30})')
"""Hidden words that start with anything but a space, and what they say."""


def test_no_template_starts_its_hidden_words_with_punctuation() -> None:
    """A browser sets hidden words off with a space, so words that start with punctuation
    would read with a space before it: every hidden part of a name starts with a space."""
    found = {
        template.name: HIDDEN_WORDS.findall(template.read_text(encoding="utf-8"))
        for template in sorted(TEMPLATE_PATH.glob("**/*.html"))
    }

    assert len(found) > 20
    assert {name: hidden for name, hidden in found.items() if hidden} == {}


def test_no_page_starts_its_hidden_words_with_punctuation(pages: dict[str, str]) -> None:
    found = {state: HIDDEN_WORDS.findall(page) for state, page in pages.items()}
    assert {state: hidden for state, hidden in found.items() if hidden} == {}


def test_the_name_reader_sets_hidden_words_off_with_a_space_as_a_browser_does() -> None:
    """What these tests read as a name is what the browser computes: hidden words that start
    with a space read as written, and those that start with punctuation read with a space
    before it, which is why the controls here say such names in a label instead."""
    page = (
        '<a href="#one">Practice<span class="visually-hidden"> for History</span></a>'
        '<a href="#two">Practice<span class="visually-hidden">, History</span></a>'
        '<a href="#three" aria-label="Practice, History">Practice</a>'
    )

    assert control_names(page) == [
        ("Practice", "Practice for History"),
        ("Practice", "Practice , History"),
        ("Practice", "Practice, History"),
    ]


PRACTICE_KEYS = {
    "History": "assignment-practice-history",
    "Science": "assignment-practice-science",
    "Art & <Design>": "assignment-practice-art",
}
"""Three assignments called Practice, by course."""
TITLE_LINKS = sorted(f"Practice, {course}: Turning it in" for course in PRACTICE_KEYS)
"""The names of the three titles as links to their Turning it in."""
TO_THE_SECTION = {
    "refused": "Review and update in Turning it in",
    "undo refused": "Open Turning it in",
}
"""The words of the way to Turning it in beside a refused press and a refused Undo."""


def handed(store: ProjectStateStore, key: str, state: HandInState, note: str | None = None) -> None:
    """A hand-in update saved from another device, on the one that stands."""
    readable = store.hand_in_readings([key]).readable
    head = readable[key].head_id if key in readable else None
    kept = store.record_hand_in(
        key, state, None, note, expected_head=head, now=NOW, today=PLAN_DATE
    )
    assert isinstance(kept, HandInSaved), kept


def refused_undo(client: TestClient, key: str) -> str:
    """Her Undo beside the result of turning it in, refused because another device has said
    something newer since: the page that says so."""
    turn_in = f"/student/actions/assignments/{key}/hand-in"
    undo = f"/student/actions/assignments/{key}/undo-hand-in"
    page = client.get(TO_TURN_IN_PAGE).text
    row = page[page.index(f'id="to-turn-in-{key}"') :]
    pressed = client.post(turn_in, data=form_fields(row, turn_in), headers=PAGE_HEADERS)
    assert pressed.status_code == 303, pressed.text[:300]
    fields = form_fields(client.get(pressed.headers["location"]).text, undo)
    handed(store_of(client), key, TURNED_IN, "On the desk")
    refused = client.post(undo, data=fields, headers=PAGE_HEADERS)
    assert refused.status_code == 409, refused.text[:300]
    return refused.text


def refused_press(client: TestClient, key: str) -> str:
    """Her press on a row that another device has since said has nothing to turn in: the
    page that refuses it."""
    turn_in = f"/student/actions/assignments/{key}/hand-in"
    handed(store_of(client), key, NEEDS_HAND_IN)
    page = client.get(TO_TURN_IN_PAGE).text
    handed(store_of(client), key, "not_required")
    row = page[page.index(f'id="to-turn-in-{key}"') :]
    refused = client.post(turn_in, data=form_fields(row, turn_in), headers=PAGE_HEADERS)
    assert refused.status_code == 409, refused.text[:300]
    return refused.text


def turning_in_seen(reader: str, tmp_path: pathlib.Path) -> dict[str, str]:
    """What ``reader`` is shown of the three Practice assignments, each still to turn in:
    the rows of To turn in on its page and on her week, the family page's Turning work in
    where the reader opens it, the line that names them once their records cannot be read,
    and where she presses, what a refused press and a refused Undo say beside the refusal."""
    seen: dict[str, str] = {}
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        store = store_of(client)
        on = PLAN_DATE + timedelta(days=2)
        store.put_on_record(
            [
                due(key, "Practice", on).model_copy(update={"course": course})
                for course, key in PRACTICE_KEYS.items()
            ],
            {},
        )
        for key in PRACTICE_KEYS.values():
            handed(store, key, NEEDS_HAND_IN)
        seen["to turn in"] = client.get(TO_TURN_IN_PAGE).text
        seen["week, to turn in"] = client.get(HER_PAGE).text
        if reader != "her":
            seen["family, turning in"] = client.get("/parent").text
        if reader != "parent":
            for course, key in PRACTICE_KEYS.items():
                seen[f"undo refused, {course}"] = refused_undo(client, key)
                seen[f"refused, {course}"] = refused_press(client, key)
        store._connection.execute("UPDATE hand_in_events SET state = 'invalid-state'")
        store._connection.commit()
        seen["unreadable"] = client.get(TO_TURN_IN_PAGE).text
        seen["week, unreadable"] = client.get(HER_PAGE).text
    return seen


def about_the_refusal(page: str) -> str:
    """What a refusal says about the assignment it was about, beside the refusal."""
    start = page.index('id="to-turn-in-about"')
    return page[start : page.index("</div>", start)]


@pytest.mark.parametrize("reader", ["her", "parent", "open"])
def test_the_links_to_turning_it_in_say_their_whole_name_in_a_label(
    reader: str, tmp_path: pathlib.Path
) -> None:
    """A title that links to its Turning it in goes on with the course, and the way to the
    section beside a refusal with the title: each says its name in a label that reads as
    written, visible words first, and three assignments called Practice are told apart."""
    seen = turning_in_seen(reader, tmp_path)
    titles = {
        state: sorted(name for words, name in control_names(main_of(page)) if words == "Practice")
        for state, page in seen.items()
        if not state.startswith(("refused", "undo refused"))
    }
    to_the_section = {
        state: control_names(about_the_refusal(page))
        for state, page in seen.items()
        if state.startswith(("refused", "undo refused"))
    }
    expected = {
        f"{kind}, {course}": [(words, f"{words}: Practice, {course}")]
        for kind, words in TO_THE_SECTION.items()
        for course in PRACTICE_KEYS
    }

    assert titles == dict.fromkeys(titles, TITLE_LINKS)
    assert len(titles) == (4 if reader == "her" else 5)
    assert to_the_section == ({} if reader == "parent" else expected)
    for state, page in seen.items():
        assert 'Practice<span class="visually-hidden">' not in page, state
        assert "Turning it in<span" not in page, state
        if state in titles:
            for course in PRACTICE_KEYS:
                label = f'aria-label="Practice, {escape(course)}: Turning it in">Practice</a>'
                assert label in page, (state, course)
    for state, found in to_the_section.items():
        words = found[0][0]
        course = state.split(", ", 1)[1]
        label = f'aria-label="{words}: Practice, {escape(course)}">{words}</a>'
        assert label in seen[state], state


@pytest.mark.parametrize("reader", ["parent", "open"])
def test_looks_good_says_its_whole_name_in_a_label(reader: str, tmp_path: pathlib.Path) -> None:
    """The plan's evening goes on from the button's words with a colon, so the whole name is
    in a label that reads as written, for a parent signed in and with the sign-in off."""
    files = {
        "BLOSSOM_DATABASE_PATH": str(tmp_path / "blossom.sqlite3"),
        "BLOSSOM_CHECKPOINT_PATH": str(tmp_path / "checkpoints.sqlite3"),
        "BLOSSOM_TRACE_PATH": str(tmp_path / "traces.sqlite3"),
    }
    with browser(key=True, **files) as client:
        made = client.post("/parent/actions/plan", data=family_plan(client, PLAN_DATE.isoformat()))
        assert made.status_code == 303, made.text[:300]
        page = client.get("/parent").text
    if reader == "parent":
        with client_for(signed_in_household(tmp_path)) as client:
            signed_in(client, THEIRS)
            page = client.get("/parent").text
    name = "Looks good: the plan for Wednesday, August 19"

    assert [heard for shown, heard in control_names(page) if shown == "Looks good"] == [name]
    assert f'aria-label="{name}">Looks good</button>' in page
    assert "Looks good<span" not in page


def rules_naming(css: str, selector: str) -> list[tuple[str, str]]:
    """Every rule whose selector list holds ``selector`` itself, not scoped under another,
    as (selectors, declarations)."""
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [
        (head.strip(), inside)
        for head, inside in re.findall(r"([^{}]+)\{([^{}]*)\}", plain)
        if selector in (part.strip() for part in head.split(","))
    ]


def test_the_sign_in_field_takes_every_shared_text_field_rule() -> None:
    css = (REPOSITORY_ROOT / "blossom" / "static" / "blossom.css").read_text(encoding="utf-8")
    shared = rules_naming(css, 'input[type="text"]')

    assert len(shared) >= 2
    for selectors, _ in shared:
        assert 'input[type="password"]' in selectors, selectors


def test_the_family_page_s_notices_break_a_long_word() -> None:
    css = (REPOSITORY_ROOT / "blossom" / "static" / "blossom.css").read_text(encoding="utf-8")
    notices = rules_naming(css, ".confidence")

    assert any("overflow-wrap: anywhere;" in inside for _, inside in notices)
