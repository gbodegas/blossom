"""Every control says its own words. The name a screen reader or voice control uses holds the
words a button or link shows, as WCAG 2.5.3 asks, and Blossom's names start with them. The
sign-in field and the family page's notices use the shared field and wrapping rules."""

import re

import pytest

from blossom.routes.runs import plan_graphs
from blossom.settings import REPOSITORY_ROOT
from tests.support import (
    ESSAY_ID,
    ESSAY_TITLE,
    HER_PAGE,
    PAGE_HEADERS,
    PLAN_DATE,
    accepting,
    browser,
    control_names,
    fixture_week_plan,
    names_not_led_by_their_words,
    names_without_their_words,
    report,
    scripted_graphs,
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
)
REPEATED = (
    "Save update",
    "Keep it as it is",
    "Not now",
    "Open Turning it in",
    "Review and update in Turning it in",
    "Looks good",
)


@pytest.fixture
def pages() -> dict[str, str]:
    """Her week, the details, To turn in and the family page, in every state that shows a
    control whose name adds context to its words."""
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
    return shown


def test_the_states_show_the_controls_they_are_for(pages: dict[str, str]) -> None:
    assert ">Save update" in pages["week, new"]
    assert ">Keep it as it is" in pages["details, change"]
    assert ">Not now" in pages["hand-in, not now"]
    assert ">Keep it as it is" in pages["hand-in, keep"]
    assert 'aria-label="Turning it in: ' not in pages["to turn in"]
    assert ">Looks good" in pages["family"]


def test_every_control_s_name_holds_the_words_it_shows(pages: dict[str, str]) -> None:
    assert set(pages) == set(STATES)
    failing = {state: names_without_their_words(page) for state, page in pages.items()}
    assert {state: found for state, found in failing.items() if found} == {}


def test_each_repeated_control_names_what_it_is_about(pages: dict[str, str]) -> None:
    """A control shown once per assignment or plan says which one in its name."""
    bare = {
        state: [
            (words, name)
            for words, name in control_names(page)
            if (words in REPEATED or words == ESSAY_TITLE) and len(name) <= len(words) + 2
        ]
        for state, page in pages.items()
    }
    turning_in = [
        name for words, name in control_names(pages["family, hand-in"]) if words == ESSAY_TITLE
    ]

    assert {state: found for state, found in bare.items() if found} == {}
    assert any(name.endswith("Turning it in") for name in turning_in)


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
