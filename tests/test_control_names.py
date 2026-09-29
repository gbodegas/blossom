"""Every control says its own words. The name a screen reader or voice control uses holds the
words a button or link shows, as WCAG 2.5.3 asks, and Blossom's names start with them. The
sign-in field and the family page's notices use the shared field and wrapping rules."""

import pathlib
import re

import pytest

from blossom.routes.navigation import assignment_anchor
from blossom.routes.runs import plan_graphs
from blossom.settings import REPOSITORY_ROOT
from tests.support import (
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
    fixture_week_plan,
    names_not_led_by_their_words,
    names_without_their_words,
    report,
    scripted_graphs,
    signed_in,
    signed_in_household,
    store_of,
    whole_form,
)

DETAILS = f"/student/assignments/{ESSAY_ID}"
HAND_IN = f"/student/actions/assignments/{ESSAY_ID}/hand-in"
STATES = (
    "week, new",
    "details, new",
    "week, change",
    "details, change",
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
    "Save update",
    "Keep it as it is",
    "Not now",
    "Open Turning it in",
    "Review and update in Turning it in",
    "Looks good",
)


@pytest.fixture
def pages(tmp_path: pathlib.Path) -> dict[str, str]:
    """Her week, the details, To turn in and the family page, in every state that shows a
    control whose name adds context to its words, and her week's help as she and a parent
    read it."""
    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [fixture_week_plan()], lambda: [accepting()]
        )
        client.post("/parent/actions/plan", data={"plan_date": PLAN_DATE.isoformat()})
        shown = {"family": client.get("/parent").text}
        shown["week, new"] = client.get(HER_PAGE).text
        shown["details, new"] = client.get(DETAILS).text
        report(client, ESSAY_ID, "done")
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
    with client_for(signed_in_household(tmp_path)) as client:
        signed_in(client, THEIRS)
        shown["week, help, parent"] = client.get(HER_PAGE).text
    return shown


def test_the_states_show_the_controls_they_are_for(pages: dict[str, str]) -> None:
    assert ">Save update" in pages["week, new"]
    assert ">Keep it as it is" in pages["details, change"]
    assert ">Not now" in pages["hand-in, not now"]
    assert ">Keep it as it is" in pages["hand-in, keep"]
    assert 'aria-label="Turning it in: ' not in pages["to turn in"]
    assert ">Looks good" in pages["family"]
    assert ">Take it back<span" in pages["week, help"]
    assert ">Ask again<" in pages["week, help"]
    assert ">Help updates (2)<" in pages["week, help"]
    assert ">Her help requests<" in pages["week, help, parent"]


def test_every_control_s_name_holds_the_words_it_shows(pages: dict[str, str]) -> None:
    assert set(pages) == set(STATES)
    failing = {state: names_without_their_words(page) for state, page in pages.items()}
    assert {state: found for state, found in failing.items() if found} == {}


def essay_part(page: str) -> str:
    """The essay's card on a page of several, or else the whole page."""
    if f'id="{assignment_anchor(ESSAY_ID)}"' in page:
        return card_for(page, ESSAY_ID)
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
    ("week, new", "Save update", "Save update for Practice, {}"),
    ("details, new", "Save update", "Save update for Practice, {}"),
    ("week, change", "Keep it as it is", "Keep it as it is: your update on Practice, {}"),
    ("details, change", "Keep it as it is", "Keep it as it is: your update on Practice, {}"),
    ("update, returned", "Save update", "Save update for Practice, {}"),
    ("update, returned", "Keep it as it is", "Keep it as it is: your update on Practice, {}"),
    ("hand-in, not now", "Not now", "Not now: the hand-in status for Practice, {}"),
    ("hand-in, returned", "Not now", "Not now: the hand-in status for Practice, {}"),
    ("hand-in, keep", "Keep it as it is", "Keep it as it is: the hand-in status for Practice, {}"),
)


@pytest.fixture
def namesakes() -> dict[str, dict[str, str]]:
    """Three assignments called Practice, in three courses and due this week, each seen in
    every state that shows a control repeated per assignment: its card, or its page."""
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
            seen["week, new"] = card_for(client.get(HER_PAGE).text, key)
            seen["details, new"] = client.get(details).text
            report(client, key, "done")
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
    return shown


@pytest.mark.parametrize(("state", "words", "name"), NAMESAKE_NAMES)
def test_same_titled_assignments_are_told_apart_by_course(
    namesakes: dict[str, dict[str, str]], state: str, words: str, name: str
) -> None:
    named = {
        course: [heard for shown, heard in control_names(seen[state]) if shown == words]
        for course, seen in namesakes.items()
    }

    assert named == {course: [name.format(course)] for course in PRACTICE_COURSES}


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
