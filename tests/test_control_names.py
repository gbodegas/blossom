"""Every control says its own words. The name a screen reader or voice control uses holds the
words a button or link shows, as WCAG 2.5.3 asks, and Blossom's names start with them. The
sign-in field and the family page's notices use the shared field and wrapping rules."""

import pathlib
import re
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from blossom.routes.navigation import assignment_anchor
from blossom.routes.runs import plan_graphs
from blossom.settings import REPOSITORY_ROOT
from tests.support import (
    DETAILS,
    ESSAY,
    ESSAY_ID,
    ESSAY_TITLE,
    HER_PAGE,
    PAGE_HEADERS,
    PLAN_DATE,
    THEIRS,
    accepting,
    browser,
    card_for,
    client_for,
    control_names,
    field_names,
    fixture_week_plan,
    names_not_led_by_their_words,
    names_without_their_words,
    report,
    school_missing,
    scripted_graphs,
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
    "Update this homework",
    "Add a note (optional)",
    "Save update",
    "Keep it as it is",
    "Change",
    "Undo",
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
        client.post("/parent/actions/plan", data={"plan_date": PLAN_DATE.isoformat()})
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
    assert ">Save update" in pages["week, new"]
    assert "<summary>Update this homework<span" in pages["week, new"]
    assert ">Keep it as it is" in pages["details, change"]
    assert ">Change<span" in pages["week, saved"]
    assert ">Undo<span" in pages["details, saved"]
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
        line = re.search(r'<p class="confidence disagree" role="status">.*?</p>', page, re.S)
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
    ("week, new", "Update this homework", "Update this homework for Practice, {}"),
    ("week, new", "Add a note (optional)", "Add a note (optional) on Practice, {}"),
    ("details, new", "Add a note (optional)", "Add a note (optional) on Practice, {}"),
    ("week, new", "Save update", "Save update for Practice, {}"),
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
    ("week, saved", "Undo", "Undo your update on Practice, {}"),
    ("week, saved", "Update history", "Update history for Practice, {}"),
    ("details, saved", "Change", "Change your update on Practice, {}"),
    ("details, saved", "Undo", "Undo your update on Practice, {}"),
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
    line = re.search(r'<p class="confidence disagree" role="status">.*?</p>', page, re.S)
    assert line is not None
    found: list[str] = [
        link
        for link in re.findall(r"<a\b[^>]*>.*?</a>", line.group(0), re.S)
        if f'#{assignment_anchor(assignment_id)}"' in link
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
    named = {
        course: [heard for shown, heard in field_names(seen[state]) if shown in FIELDS]
        for course, seen in namesakes.items()
    }

    assert named == {
        course: [f"Your update on Practice, {course}", f"Your note on Practice, {course}"]
        for course in PRACTICE_COURSES
    }


def test_take_it_back_names_her_request_by_its_time_and_day_in_one_label() -> None:
    """The visible words, then the request's time and day, in a label that reads as written."""
    with browser() as client:
        client.post("/student/help-requests", json={"note": "Synthetic question"})
        asked = client.get("/student/help-requests").json()[0]
        page = client.get(HER_PAGE).text
    at = datetime.fromisoformat(asked["asked_local"])
    evening = date.fromisoformat(asked["evening"])
    time = f"{at.hour % 12 or 12}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'}"
    name = f"Take it back: your request from {time} on Wednesday, August 19"

    assert evening == PLAN_DATE
    assert [heard for shown, heard in control_names(page) if shown == "Take it back"] == [name]
    assert f'aria-label="{name}">Take it back</button>' in page


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
