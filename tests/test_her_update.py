# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Her own update on an assignment, from her page: Done or Not yet, with a note if she wants.

Synthetic fixtures, a pinned clock, and forms alone: what a card offers, what a
save does and says, what a save from a page that has moved on meets, what an
undo restores, who may make an update, what it means for the plan, and what
the family page makes of her word beside the school's.
"""

import json
import pathlib
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Final
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi import Depends, Response
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.agent.graph import plan_graph_for
from blossom.app import create_app
from blossom.assignment_status import statuses_for
from blossom.dependencies import ApplicationState, get_application_state
from blossom.heuristic_relevance import CriticVerdict
from blossom.intake import PASTE_DAY
from blossom.noticing import planning_digest, read_week
from blossom.plans import DailyPlan
from blossom.reconciliation import SourceChannel
from blossom.routes import parent as parent_routes
from blossom.routes import student as student_routes
from blossom.routes.navigation import segment, week_href
from blossom.routes.parent import ASSIGNMENTS_CHANGED as THEIR_ASSIGNMENTS_CHANGED
from blossom.routes.parent import PLAN_INCLUDES_DONE as SHE_REPORTS
from blossom.routes.runs import NOTHING_TO_SCHEDULE, PlanGraphs, plan_graphs
from blossom.routes.student import (
    ALREADY_UNDONE,
    ASSIGNMENTS_CHANGED,
    BAD_FORM,
    CANNOT_UNDO,
    CHOOSE_ONE,
    GONE,
    IN_PLACE_COOKIE,
    IN_PLACE_MAX,
    NOT_HERS_TO_UPDATE,
    NOT_SAVED,
    NOT_THIS_CARDS,
    NOT_UNDONE,
    NOTE_TOO_LONG,
    PLAN_INCLUDES_DONE,
    PLAN_WINDOW_DONE,
    SAVED_ELSEWHERE,
    UPDATE_ALREADY_SAVED,
    UPDATE_SAVED,
    UPDATE_UNDONE,
    InPlace,
    landing_cookie,
    leave_in_place,
    place_key,
)
from blossom.settings import ANTHROPIC_API_KEY_VARIABLE, REPOSITORY_ROOT
from blossom.stores.project_state import (
    Assignment,
    AssignmentKind,
    ProjectStateStore,
    Saved,
    StatusReport,
    UnknownAssignment,
)
from tests.support import (
    ESCAPED,
    ESSAY_TITLE,
    HERS,
    MISSING_EMAIL,
    PAGE_HEADERS,
    PLAN_DATE,
    SAME_ORIGIN,
    THEIRS,
    Answer,
    ReportsWhileAsked,
    Scripted,
    a_row,
    accepting,
    browser,
    card_for,
    fixture_clock,
    fixture_settings,
    fixture_week_plan,
    form_fields,
    hidden,
    human_text,
    landing_in,
    main_of,
    ok,
    refusing,
    report,
    reported,
    save,
    school_said,
    signed_in_household,
    state_of,
    store_of,
    week_card,
    whole_form,
)
from tests.support import ESSAY_ID as ESSAY
from tests.support import FIXTURE_WEEK as WEEK
from tests.support import HER_PAGE as PAGE
from tests.support import QUIZ_ID as QUIZ
from tests.support import READING_LOG_ID as LOG
from tests.support import SYLLABUS_ID as SYLLABUS
from tests.support import after as redirected

OFFERED = 'name="status" value="done"'
"""Done offered on a card or a form: a button on her week, a choice on the details."""
NAMED_BY_ITS_ROW = f'aria-label="{ESSAY_TITLE}, World History">{ESSAY_TITLE}</a> (World History).'
"""How a notice names the essay on a plan read by its rows: the saved title, a link to the
assignment's details, and the course; never the id."""


FOLD: Final = '<details class="steps reported-done"'
UNDO_ESSAY: Final = f"/student/actions/assignments/{ESSAY}/undo-report"
CHANGE_ESSAY: Final = f"/student/due-this-week#update-choice-{ESSAY}"


def placed(page: str) -> tuple[list[str], list[str]]:
    """Her week's cards in page order, the active ones and the ones in Reported done: each
    card's id as the card writes it, split where the week's Reported done fold starts."""
    main = main_of(page)
    later = main.find('<section class="panel assigned">')
    cards = main if later < 0 else main[:later]
    fold = cards.find(FOLD)
    found = [
        (card.start(), card.group(1))
        for card in re.finditer(r'<article class="assignment[^"]*" id="assignment-([^"]+)"', cards)
    ]
    active = [name for at, name in found if fold < 0 or at < fold]
    return active, [name for at, name in found if fold >= 0 and at > fold]


def left_for_the_page(answer: Answer) -> str:
    """The cookie a press leaves for the page that answers it, as the response sets it."""
    line = answer.headers.get("set-cookie", "")
    assert line.startswith(IN_PLACE_COOKIE), line
    return line


def link_named(page: str, label: str) -> str:
    """The address of a page's link with these words, as a browser follows it."""
    found = re.search(rf'<a href="([^"]+)">{re.escape(label)}</a>', page)
    assert found is not None, label
    return found.group(1).replace("&amp;", "&")


def cards_in(address: str) -> list[str]:
    """The cards an address carries for the page to keep in place, as the page reads them."""
    return parse_qs(urlsplit(address).query, keep_blank_values=True).get("in_place", [])


def minute_passes(client: TestClient) -> None:
    """The cookies presses left, dropped as a browser drops them once their minute is up."""
    for name in {cookie.name for cookie in client.cookies.jar}:
        if name.startswith(IN_PLACE_COOKIE):
            client.cookies.delete(name)
    assert not [cookie for cookie in client.cookies.jar if cookie.name.startswith(IN_PLACE_COOKIE)]


@contextmanager
def her_device(tmp_path: pathlib.Path, signed_in: bool) -> Iterator[TestClient]:
    """Her device on the fixture week, with the sign-in off or signed in as her."""
    if not signed_in:
        with browser() as client:
            yield client
        return
    settings = signed_in_household(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        yield client


def test_a_card_offers_her_update_and_a_done_stays_in_place_until_her_next_visit() -> None:
    """The card offers Done and Not yet as buttons, a note behind a fold, and the page says
    once what Done means. Saved, the card says so where it was, shows the update with its day
    and what it means, offers Change and Undo, and keeps its place among the active cards for
    the rest of the visit. A refresh or a return folds it under the active cards with a count."""
    with browser() as client:
        before = client.get(PAGE, headers=PAGE_HEADERS).text
        card = card_for(before, ESSAY)
        answer = save(client, week_card(client, ESSAY), "done", "Turned in on paper.\r\nTwo pages.")
        landed = redirected(client, answer)
        landing = landing_in(answer.headers["location"])
        left = client.cookies.get(landing_cookie(landing), path=PAGE)
        refreshed = client.get(answer.headers["location"], headers=PAGE_HEADERS).text
        returned = client.get(PAGE, headers=PAGE_HEADERS).text
        history = state_of(client).project_state.student_reports(ESSAY)
        week = read_week(state_of(client).project_state, state_of(client).project_state, PLAN_DATE)

    assert 'type="submit" name="status" value="done"' in card
    assert 'type="submit" name="status" value="not_yet"' in card
    assert 'type="radio"' not in card
    assert "Done means you have finished your part. It does not turn work in." in before
    assert "<summary>Add a note (optional)<span" in card
    assert "Up to 500 characters. Your parents can read this. Notes on work being" in card
    assert "maxlength" not in card
    assert (
        '<span class="visually-hidden"> on Canal Era comparison essay, World History</span>' in card
    )
    assert hidden(card, "expected_report_id") == ""
    assert hidden(card, "week") == WEEK
    assert hidden(card, "in_place") == f"a:{place_key(ESSAY)}"
    assert re.fullmatch(r"[0-9a-f]{16}", landing)
    assert answer.headers["location"] == (
        f"{PAGE}?week={WEEK}&saved={ESSAY}&landing={landing}#update-result-{ESSAY}"
    )
    cookie = left_for_the_page(answer)
    assert cookie.startswith(f"{landing_cookie(landing)}=a:{place_key(ESSAY)};")
    for part in ("HttpOnly", "Max-Age=60", f"Path={PAGE}", "SameSite=lax"):
        assert part in cookie, part
    assert placed(landed.text) == placed(before)
    assert FOLD not in main_of(landed.text)
    saved = card_for(landed.text, ESSAY)
    assert (
        f'<p class="note update-result" role="status" id="update-result-{ESSAY}" '
        f'tabindex="-1">{UPDATE_SAVED}</p>'
    ) in saved
    assert '<span class="pill">Your update: Done</span>' in saved
    assert "Reported August 19" in saved
    assert "You wrote: <q>Turned in on paper.\nTwo pages.</q>" in saved
    assert "This is out of work to plan. Your school record is separate." in saved
    assert ">Change<span" in saved
    assert f'action="{UNDO_ESSAY}"' in saved
    assert "<legend>Your update<span" not in saved
    assert left is None
    assert left_for_the_page(landed).startswith(f'{landing_cookie(landing)}=""; ')
    assert ESSAY not in [item.assignment_id for item in week.active()]
    active, done = placed(refreshed)
    assert ESSAY not in active
    assert done == [ESSAY]
    assert '<details class="steps reported-done" open>' in refreshed
    assert "<summary>Reported done (1)</summary>" in refreshed
    assert UPDATE_SAVED in card_for(refreshed, ESSAY)
    assert placed(returned)[1] == [ESSAY]
    assert '<details class="steps reported-done">' in returned
    assert OFFERED not in saved
    assert [(item.status, item.note) for item in history] == [
        ("done", "Turned in on paper.\nTwo pages.")
    ]
    assert history[0].reported_on == PLAN_DATE


def test_saving_another_card_in_the_same_visit_keeps_the_first_where_it_was() -> None:
    """Each form on the page carries the cards the visit keeps, its own among them, so a
    second save keeps the first card where it was; a return groups both by their updates."""
    with browser() as client:
        before = client.get(PAGE, headers=PAGE_HEADERS).text
        first = redirected(client, save(client, week_card(client, ESSAY), "done")).text
        other = card_for(first, SYLLABUS)
        second = redirected(client, save(client, other, "done", assignment_id=SYLLABUS)).text
        returned = client.get(PAGE, headers=PAGE_HEADERS).text

    assert hidden(other, "in_place") == f"a:{place_key(ESSAY)}|a:{place_key(SYLLABUS)}"
    assert placed(second) == placed(before)
    assert UPDATE_SAVED in card_for(second, SYLLABUS)
    assert UPDATE_SAVED not in card_for(second, ESSAY)
    for name in (ESSAY, SYLLABUS):
        assert '<span class="pill">Your update: Done</span>' in card_for(second, name)
    assert sorted(placed(returned)[1]) == sorted([ESSAY, SYLLABUS])


def quick_press(client: TestClient, page: str, name: str, status: str) -> Answer:
    """A press of a card's own Done or Not yet button, sending the card's quick form as a
    browser does: its hidden fields, the button's status, and the note as it stands."""
    action = f"/student/actions/assignments/{name}/report"
    start = page.index(f'<form method="post" action="{action}" class="quick">')
    fields = form_fields(page[start:], action)
    typed = {} if "note" in fields else {"note": ""}
    return client.post(action, data={**fields, **typed, "status": status}, headers=PAGE_HEADERS)


def test_a_cards_own_buttons_keep_the_cards_in_place_until_her_next_visit() -> None:
    """Done and Not yet on a card, Done on a card saved as Not yet, and Undo each carry the
    cards the visit keeps, their own among them, so no card moves until a refresh or a
    return groups the week by its updates."""
    undo = f"/student/actions/assignments/{SYLLABUS}/undo-report"
    with browser() as client:
        before = client.get(PAGE, headers=PAGE_HEADERS).text
        done = redirected(client, quick_press(client, before, ESSAY, "done")).text
        not_yet = redirected(client, quick_press(client, done, SYLLABUS, "not_yet")).text
        then_done = redirected(client, quick_press(client, not_yet, SYLLABUS, "done")).text
        fields = form_fields(card_for(then_done, SYLLABUS), undo)
        undone = client.post(undo, data=fields, headers=PAGE_HEADERS)
        undone_page = redirected(client, undone).text
        refreshed = client.get(undone.headers["location"], headers=PAGE_HEADERS).text
        returned = client.get(PAGE, headers=PAGE_HEADERS).text
        history = state_of(client).project_state.student_reports(SYLLABUS)

    assert 'class="quick"' in card_for(before, ESSAY)
    assert hidden(card_for(before, ESSAY), "in_place") == f"a:{place_key(ESSAY)}"
    assert hidden(card_for(done, SYLLABUS), "in_place") == (
        f"a:{place_key(ESSAY)}|a:{place_key(SYLLABUS)}"
    )
    assert fields["in_place"] == f"a:{place_key(ESSAY)}|a:{place_key(SYLLABUS)}"
    for page in (done, not_yet, then_done, undone_page):
        assert placed(page) == placed(before)
        assert FOLD not in main_of(page)
    assert '<span class="pill">Your update: Done</span>' in card_for(done, ESSAY)
    assert '<span class="pill">Your update: Not yet</span>' in card_for(not_yet, SYLLABUS)
    assert '<span class="pill">Your update: Done</span>' in card_for(then_done, SYLLABUS)
    assert UPDATE_UNDONE in card_for(undone_page, SYLLABUS)
    assert '<span class="pill">Your update: Not yet</span>' in card_for(undone_page, SYLLABUS)
    assert [event.operation for event in history] == ["report", "report", "undo"]
    assert placed(refreshed)[1] == [ESSAY]
    assert SYLLABUS in placed(refreshed)[0]
    assert placed(returned)[1] == [ESSAY]


@pytest.mark.parametrize("signed_in", [False, True], ids=["sign-in off", "signed in"])
def test_a_visit_from_another_tab_before_a_save_lands_leaves_its_cards_alone(
    tmp_path: pathlib.Path, signed_in: bool
) -> None:
    """A fresh visit to her week between a save's redirect and its landing groups the week by
    its updates and neither reads nor clears the cards the save keeps for its own landing."""
    with her_device(tmp_path, signed_in) as client:
        before = client.get(PAGE, headers=PAGE_HEADERS).text
        answer = save(client, card_for(before, ESSAY), "done")
        other = client.get(PAGE, headers=PAGE_HEADERS)
        landed = redirected(client, answer)
        refreshed = client.get(answer.headers["location"], headers=PAGE_HEADERS).text

    landing = landing_in(answer.headers["location"])
    assert placed(other.text)[1] == [ESSAY]
    assert IN_PLACE_COOKIE not in other.headers.get("set-cookie", "")
    assert placed(landed.text) == placed(before)
    assert left_for_the_page(landed).startswith(f'{landing_cookie(landing)}=""; ')
    assert placed(refreshed)[1] == [ESSAY]


@pytest.mark.parametrize("signed_in", [False, True], ids=["sign-in off", "signed in"])
@pytest.mark.parametrize("first", [ESSAY, SYLLABUS], ids=["essay lands first", "other first"])
def test_two_saves_before_either_lands_each_keep_their_own_card_in_place(
    tmp_path: pathlib.Path, signed_in: bool, first: str
) -> None:
    """Two forms from one visit saved before either redirect is followed: each landing keeps
    its own card where it was, whichever lands first, and a return groups both."""
    with her_device(tmp_path, signed_in) as client:
        before = client.get(PAGE, headers=PAGE_HEADERS).text
        answers = {
            name: save(client, card_for(before, name), "done", assignment_id=name)
            for name in (ESSAY, SYLLABUS)
        }
        order = [first, *[name for name in answers if name != first]]
        landed = {name: redirected(client, answers[name]) for name in order}
        returned = client.get(PAGE, headers=PAGE_HEADERS).text

    landings = {name: landing_in(answer.headers["location"]) for name, answer in answers.items()}
    assert landings[ESSAY] != landings[SYLLABUS]
    for name, other in ((ESSAY, SYLLABUS), (SYLLABUS, ESSAY)):
        cookie = landing_cookie(landings[name])
        assert left_for_the_page(answers[name]).startswith(f"{cookie}=a:{place_key(name)};")
        active, done = placed(landed[name].text)
        assert name in active
        assert done == [other]
        assert left_for_the_page(landed[name]).startswith(f'{cookie}=""; ')
    assert sorted(placed(returned)[1]) == sorted([ESSAY, SYLLABUS])


@pytest.mark.parametrize(
    "named",
    ["", "{l}{l}", "{l}&landing={l}", "{u}", "{l}0", "{s}", " {l}", "{l} ", "%20{l}", "x{s}"],
)
def test_her_week_reads_only_the_cards_of_the_landing_its_address_names(named: str) -> None:
    """Only an address that names a landing once, as these pages write it, reads the cards
    left for it; any other value neither reads nor clears them, and the landing still finds
    them after."""
    with browser() as client:
        before = client.get(PAGE, headers=PAGE_HEADERS).text
        answer = save(client, card_for(before, ESSAY), "done")
        landing = landing_in(answer.headers["location"])
        value = named.format(l=landing, u=landing.upper(), s=landing[:-1])
        asked = client.get(f"{PAGE}?landing={value}", headers=PAGE_HEADERS)
        landed = redirected(client, answer)

    assert placed(asked.text)[1] == [ESSAY]
    assert IN_PLACE_COOKIE not in asked.headers.get("set-cookie", "")
    assert placed(landed.text) == placed(before)


def test_a_landing_reads_its_own_cards_and_leaves_another_landings_alone() -> None:
    """Two saves' landings each read and clear their own cookie and no other."""
    with browser() as client:
        before = client.get(PAGE, headers=PAGE_HEADERS).text
        essay = save(client, card_for(before, ESSAY), "done")
        other = save(client, card_for(before, SYLLABUS), "done", assignment_id=SYLLABUS)
        landed = redirected(client, essay)
        names = {cookie.name for cookie in client.cookies.jar}

    assert left_for_the_page(landed).count(IN_PLACE_COOKIE) == 1
    assert landing_cookie(landing_in(essay.headers["location"])) not in names
    assert landing_cookie(landing_in(other.headers["location"])) in names


def test_try_again_after_try_again_keeps_one_cookie_for_the_visit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each Try again is sent to a new landing and clears the cookie of the landing it came
    from, so a browser never holds more than one for a visit that keeps failing."""
    held = "|".join(f"a:{place_key(f'card-{n}')}" for n in range(IN_PLACE_MAX))
    with browser() as client:
        monkeypatch.setattr(student_routes, "student_page", refusing())
        moved = client.get(f"{PAGE}?{urlencode({'in_place': held})}", headers=PAGE_HEADERS)
        tries, held_cookies = [], []
        for _ in range(3):
            failure = redirected(client, moved)
            moved = client.get(link_named(failure.text, "Try again"), headers=PAGE_HEADERS)
            tries.append(moved)
            held_cookies.append(
                [cookie.name for cookie in client.cookies.jar if IN_PLACE_COOKIE in cookie.name]
            )

    landings = [landing_in(answer.headers["location"]) for answer in tries]
    assert len(set(landings)) == 3
    for names, landing in zip(held_cookies, landings, strict=True):
        assert names == [landing_cookie(landing)]
    for answer, before in zip(tries[1:], landings, strict=False):
        assert f'{landing_cookie(before)}=""; ' in answer.headers["set-cookie"]


@pytest.mark.parametrize("repeated", ["same card", "two cards"])
def test_cards_named_twice_in_her_weeks_address_keep_nothing_in_place(repeated: str) -> None:
    """An address that names its cards twice is answered without them, a landing, or a cookie,
    never with the last of them."""
    first, other = f"a:{place_key(ESSAY)}", f"a:{place_key(SYLLABUS)}"
    second = first if repeated == "same card" else other
    with browser() as client:
        moved = client.get(
            f"{PAGE}?{urlencode([('in_place', first), ('in_place', second)])}",
            headers=PAGE_HEADERS,
        )

    assert moved.status_code == 303
    assert moved.headers["location"] == PAGE
    assert IN_PLACE_COOKIE not in moved.headers.get("set-cookie", "")


@pytest.mark.parametrize(
    "named", ["", "{s}", "{l}0", "{u}", "x{s}", "{l}&landing={l}"], ids=lambda named: named
)
def test_change_with_a_landing_it_does_not_write_clears_no_cookie(named: str) -> None:
    """Change from an address whose landing is not one these pages write, or is named twice,
    clears no cookie and sets only its own new landing's."""
    landing = "0123456789abcdef"
    value = named.format(l=landing, u=landing.upper(), s=landing[:-1])
    kept = f"a:{place_key(ESSAY)}"
    query = urlencode({"week": WEEK, "change": ESSAY, "in_place": kept})
    with browser() as client:
        moved = client.get(f"{PAGE}?{query}&landing={value}", headers=PAGE_HEADERS)
        nothing = client.get(
            f"{PAGE}?week={WEEK}&in_place=&landing={landing}", headers=PAGE_HEADERS
        )

    new = landing_in(moved.headers["location"])
    assert moved.headers["set-cookie"].count(IN_PLACE_COOKIE) == 1
    assert moved.headers["set-cookie"].startswith(f"{landing_cookie(new)}={kept};")
    assert nothing.headers["location"] == f"{PAGE}?week={WEEK}"
    assert nothing.headers["set-cookie"].startswith(f'{landing_cookie(landing)}=""; ')


def test_change_from_a_landing_is_sent_to_a_new_landing_of_its_own() -> None:
    """Change from an address that names a landing, once or twice, is sent to a new landing
    named once, and the address is answered without the old one."""
    with browser() as client:
        before = client.get(PAGE, headers=PAGE_HEADERS).text
        answer = save(client, card_for(before, ESSAY), "done")
        old = landing_in(answer.headers["location"])
        kept = f"a:{place_key(ESSAY)}"
        query = urlencode({"week": WEEK, "change": ESSAY, "landing": old, "in_place": kept})
        moved = client.get(f"{PAGE}?{query}&landing={old}", headers=PAGE_HEADERS)

    new = landing_in(moved.headers["location"])
    assert new != old
    assert moved.headers["location"] == (
        f"{PAGE}?week={WEEK}&change={ESSAY}&landing={new}#update-choice-{ESSAY}"
    )
    assert left_for_the_page(moved).startswith(f"{landing_cookie(new)}={kept};")


def test_change_undo_and_keep_it_as_it_is_keep_the_card_where_it_was() -> None:
    """Change and Keep it as it is carry the cards in their address, which is answered with
    the same address without them and the cookie; a save from the opened form and an Undo
    keep the card among the active cards too."""
    with browser() as client:
        before = client.get(PAGE, headers=PAGE_HEADERS).text
        first = redirected(client, save(client, week_card(client, ESSAY), "done")).text
        fields = form_fields(card_for(first, ESSAY), CHANGE_ESSAY)
        change = client.get(PAGE, params=fields, headers=PAGE_HEADERS)
        opened = redirected(client, change).text
        cancel = re.search(r'<a class="cancel" href="([^"]+)"', card_for(opened, ESSAY))
        assert cancel is not None
        keep = client.get(cancel.group(1).replace("&amp;", "&"), headers=PAGE_HEADERS)
        kept = redirected(client, keep).text
        reopened = redirected(client, client.get(PAGE, params=fields, headers=PAGE_HEADERS)).text
        changed = redirected(client, save(client, card_for(reopened, ESSAY), "not_yet")).text
        undo = form_fields(card_for(changed, ESSAY), UNDO_ESSAY)
        undone = redirected(client, client.post(UNDO_ESSAY, data=undo, headers=PAGE_HEADERS)).text

    assert fields == {"week": WEEK, "change": ESSAY, "in_place": f"a:{place_key(ESSAY)}"}
    assert change.status_code == 303
    to_change, to_keep = (landing_in(answer.headers["location"]) for answer in (change, keep))
    assert to_change != to_keep
    assert change.headers["location"] == (
        f"{PAGE}?week={WEEK}&change={ESSAY}&landing={to_change}#update-choice-{ESSAY}"
    )
    assert left_for_the_page(change).startswith(
        f"{landing_cookie(to_change)}=a:{place_key(ESSAY)};"
    )
    assert keep.headers["location"] == (
        f"{PAGE}?week={WEEK}&show={ESSAY}&landing={to_keep}#title-{ESSAY}"
    )
    for page in (opened, kept, changed, undone):
        assert placed(page) == placed(before)
        assert FOLD not in main_of(page)
    assert "<legend>Your update<span" in card_for(opened, ESSAY)
    assert "<legend>Your update<span" not in card_for(kept, ESSAY)
    assert '<span class="pill">Your update: Not yet</span>' in card_for(changed, ESSAY)
    assert undo["in_place"] == f"a:{place_key(ESSAY)}"
    assert UPDATE_UNDONE in card_for(undone, ESSAY)
    assert '<span class="pill">Your update: Done</span>' in card_for(undone, ESSAY)


def test_a_card_changed_from_done_stays_in_reported_done_for_the_visit() -> None:
    """A card in Reported done that she changes to Not yet stays in that fold, open around
    it, for the rest of the visit, and is among the active cards on her next visit."""
    with browser() as client:
        reported(store_of(client), "done", ESSAY)
        card = week_card(client, ESSAY)
        landed = redirected(client, save(client, card, "not_yet")).text
        returned = client.get(PAGE, headers=PAGE_HEADERS).text

    assert hidden(card, "in_place") == f"d:{place_key(ESSAY)}"
    assert placed(landed)[1] == [ESSAY]
    assert '<details class="steps reported-done" open>' in landed
    assert "Still unfinished. It can be included in today's plan." in card_for(landed, ESSAY)
    assert ESSAY in placed(returned)[0]
    assert FOLD not in main_of(returned)


def test_a_refused_save_keeps_the_cards_the_visit_holds_in_place() -> None:
    """A field error and a save from a page that has moved on are answered on the page with
    the cards the form carried still where they were."""
    with browser() as client:
        first = redirected(client, save(client, week_card(client, ESSAY), "done")).text
        stale = card_for(first, SYLLABUS)
        unchosen = save(client, stale, None, assignment_id=SYLLABUS)
        reported(store_of(client), "done", SYLLABUS)
        conflict = save(client, stale, "not_yet", assignment_id=SYLLABUS)

    assert unchosen.status_code == 422
    assert conflict.status_code == 409
    for answer in (unchosen, conflict):
        active, done = placed(answer.text)
        assert ESSAY in active, answer.status_code
        assert SYLLABUS in active, answer.status_code
        assert done == [], answer.status_code
        assert IN_PLACE_COOKIE not in answer.headers.get("set-cookie", "")
    assert CHOOSE_ONE in card_for(unchosen.text, SYLLABUS)
    assert SAVED_ELSEWHERE in card_for(conflict.text, SYLLABUS)


def test_what_a_visit_keeps_in_place_is_read_only_as_the_pages_write_it() -> None:
    """Each card is carried by a short key of its id; parts these pages never write are
    passed over, and a card named twice keeps its first place."""
    written = InPlace().keeping(ESCAPED, False).keeping(ESSAY, True)
    said = written.said()
    key = place_key(ESSAY)

    assert said == f"a:{place_key(ESCAPED)}|d:{key}"
    assert re.fullmatch(r"[0-9a-f]{16}", key)
    assert InPlace.read(said) == written
    assert InPlace.read(None) == InPlace()
    for junk in (
        "",
        "x:" + key,
        "a:",
        "a:" + key[:-1],
        "a:" + key + "0",
        "a:" + "A" * 16,
        "a:" + "g" * 16,
        "a: " + key,
        "a:" + ESSAY,
        "a",
    ):
        assert InPlace.read(junk) == InPlace(), junk
    assert InPlace.read(f"d:{key}|a:{key}").cards == ((key, True),)
    many = InPlace.read("|".join(f"a:{place_key(f'card-{n}')}" for n in range(60))).cards
    assert [key for key, _ in many] == [place_key(f"card-{n}") for n in range(20, 60)]
    keys = [place_key(f"card-{n}") for n in range(IN_PLACE_MAX)]
    over = InPlace.read("|".join(f"a:{key}" for key in [*keys, keys[0]])).cards
    assert [key for key, _ in over] == [*keys[1:], keys[0]]
    assert written.shown_done(ESSAY, False) is True
    assert written.shown_done(QUIZ, True) is True
    assert written.keeping(ESSAY, False) == written


def test_ids_that_differ_only_in_case_or_spacing_are_kept_apart() -> None:
    kept = InPlace().keeping("Essay", True)

    assert kept.shown_done("Essay", False) is True
    for other in ("essay", "Essay ", " Essay", "Es say"):
        assert kept.shown_done(other, False) is False, other
        assert place_key(other) != place_key("Essay"), other


LONG_IDS: Final = [f"{n:02d}" + "\U0001f4d8" * 198 for n in range(IN_PLACE_MAX + 1)]
"""Ids of the longest length a form takes, each character four bytes once encoded."""


def test_the_cookie_holds_every_card_a_visit_keeps_within_a_browsers_limit() -> None:
    """Forty cards with the longest ids fit the 4,096 bytes a browser keeps for one cookie;
    one more keeps the latest forty."""
    full = InPlace()
    for name in LONG_IDS[:IN_PLACE_MAX]:
        full = full.keeping(name, False)
    over = full.keeping(LONG_IDS[-1], True)
    landing = "f" * 16
    lines = [
        leave_in_place(Response(), kept, landing).headers["set-cookie"] for kept in (full, over)
    ]

    assert len(full.cards) == len(over.cards) == IN_PLACE_MAX
    for line in lines:
        assert len(line.encode()) <= 4096, len(line.encode())
    assert InPlace.read(full.said()) == full
    assert InPlace.read(over.said()) == over
    assert over.shown_done(LONG_IDS[0], True) is True
    assert over.shown_done(LONG_IDS[1], True) is False
    assert over.shown_done(LONG_IDS[-1], False) is True


def test_cards_with_long_ids_keep_their_place_through_each_save() -> None:
    """Three cards with the longest ids, saved Done one after another, each leave a cookie
    a browser keeps, and all three stay among the active cards."""
    names = LONG_IDS[:3]
    with browser() as client:
        store_of(client).upsert_assignments(
            [
                a_row(name, f"Set {n}").model_copy(update={"due_date": PLAN_DATE})
                for n, name in enumerate(names)
            ]
        )
        before = client.get(PAGE, headers=PAGE_HEADERS).text
        page, lines = before, []
        for name in names:
            action = student_routes.report_actions(name)[0]
            fields = form_fields(card_for(page, name), action)
            answer = client.post(
                action, data={**fields, "status": "done", "note": ""}, headers=PAGE_HEADERS
            )
            lines.append(left_for_the_page(answer))
            page = redirected(client, answer).text
            assert placed(page) == placed(before), name

    for line in lines:
        assert len(line.encode()) <= 4096, len(line.encode())
    assert all(segment(name) in placed(page)[0] for name in names)


@pytest.mark.parametrize("signed_in", [False, True], ids=["sign-in off", "signed in"])
def test_a_week_that_cannot_be_read_keeps_the_cards_in_place_for_a_try_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, signed_in: bool
) -> None:
    """The page that says her week can't be read leaves the cards in place again, so a try
    again shows them where they were; a return after it groups them by their updates."""
    real = student_routes.student_page
    with her_device(tmp_path, signed_in) as client:
        before = client.get(PAGE, headers=PAGE_HEADERS).text
        page, failures, retries = before, [], []
        for name, tries in ((ESSAY, 2), (SYLLABUS, 1)):
            answer = save(client, card_for(page, name), "done", assignment_id=name)
            for _ in range(tries):
                monkeypatch.setattr(student_routes, "student_page", refusing())
                failures.append(client.get(answer.headers["location"], headers=PAGE_HEADERS))
                monkeypatch.setattr(student_routes, "student_page", real)
            retries.append(client.get(answer.headers["location"], headers=PAGE_HEADERS))
            page = retries[-1].text
        returned = client.get(PAGE, headers=PAGE_HEADERS).text

    first, both = f"a:{place_key(ESSAY)}", f"a:{place_key(ESSAY)}|a:{place_key(SYLLABUS)}"
    for failure, kept in zip(failures, (first, first, both), strict=True):
        assert failure.status_code == 503
        line = left_for_the_page(failure)
        cookie = landing_cookie(landing_in(str(failure.request.url)))
        assert line.startswith(f"{cookie}={kept};"), line
        assert "Max-Age=60" in line
    for retry in retries:
        assert placed(retry.text) == placed(before)
        cookie = landing_cookie(landing_in(str(retry.request.url)))
        assert left_for_the_page(retry).startswith(f'{cookie}=""; ')
    assert sorted(placed(returned)[1]) == sorted([ESSAY, SYLLABUS])


@pytest.mark.parametrize("signed_in", [False, True], ids=["sign-in off", "signed in"])
@pytest.mark.parametrize("failures", [1, 2], ids=["unavailable once", "unavailable twice"])
def test_a_try_again_after_the_cookie_is_gone_keeps_the_cards_in_place(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, signed_in: bool, failures: int
) -> None:
    """Try again carries the cards in its address, so a try made after the cookie is gone,
    with the page failing again before it or not, shows them where they were. The saves are
    on record and out of the plan's work from the start, and a refresh or a return groups
    the week by its updates."""
    real = student_routes.student_page
    with her_device(tmp_path, signed_in) as client:
        before = client.get(PAGE, headers=PAGE_HEADERS).text
        page, links, tries, landings, while_failing = before, [], [], [], []
        for name in (ESSAY, SYLLABUS):
            answer = save(client, card_for(page, name), "done", assignment_id=name)
            monkeypatch.setattr(student_routes, "student_page", refusing())
            failure: Answer = client.get(answer.headers["location"], headers=PAGE_HEADERS)
            for _ in range(failures - 1):
                minute_passes(client)
                links.append(link_named(failure.text, "Try again"))
                failure = redirected(client, client.get(links[-1], headers=PAGE_HEADERS))
            assert failure.status_code == 503
            store = state_of(client).project_state
            while_failing.append(
                (
                    store.student_reports(name)[-1].status,
                    [item.assignment_id for item in read_week(store, store, PLAN_DATE).active()],
                )
            )
            monkeypatch.setattr(student_routes, "student_page", real)
            minute_passes(client)
            links.append(link_named(failure.text, "Try again"))
            tries.append(client.get(links[-1], headers=PAGE_HEADERS))
            landings.append(redirected(client, tries[-1]))
            page = landings[-1].text
        refreshed = client.get(tries[-1].headers["location"], headers=PAGE_HEADERS).text
        returned = client.get(PAGE, headers=PAGE_HEADERS).text

    first, both = f"a:{place_key(ESSAY)}", f"a:{place_key(ESSAY)}|a:{place_key(SYLLABUS)}"
    assert [cards_in(link) for link in links] == [[first]] * failures + [[both]] * failures
    for name, (status, active) in zip((ESSAY, SYLLABUS), while_failing, strict=True):
        assert status == "done"
        assert name not in active
    tried = links[failures - 1 :: failures]
    for answer, kept, link in zip(tries, (first, both), tried, strict=True):
        assert answer.status_code == 303
        assert "in_place" not in answer.headers["location"]
        landing = landing_in(answer.headers["location"])
        assert landing != landing_in(link)
        assert left_for_the_page(answer).startswith(f"{landing_cookie(landing)}={kept};")
    for answer, landed in zip(tries, landings, strict=True):
        assert landed.status_code == 200
        assert placed(landed.text) == placed(before)
        assert FOLD not in main_of(landed.text)
        cookie = landing_cookie(landing_in(answer.headers["location"]))
        assert left_for_the_page(landed).startswith(f'{cookie}=""; ')
    for later in (refreshed, returned):
        assert sorted(placed(later)[1]) == sorted([ESSAY, SYLLABUS])


def test_try_again_adds_the_cards_the_cookie_held_to_the_address_as_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Try again keeps the address as it was asked, blank and repeated values included, and
    adds the latest forty cards the cookie held; with no cookie it adds nothing."""
    held = "|".join(f"a:{place_key(f'card-{n}')}" for n in range(IN_PLACE_MAX + 1))
    latest = InPlace.read(held).said()
    asked = f"week={WEEK}&saved={ESSAY}&saved=&show="
    with browser() as client:
        monkeypatch.setattr(student_routes, "student_page", refusing())
        plain = client.get(f"{PAGE}?{asked}", headers=PAGE_HEADERS)
        bare = client.get(PAGE, headers=PAGE_HEADERS)
        pages, landings = [], []
        for address in (f"{PAGE}?{asked}&", f"{PAGE}?"):
            moved = client.get(f"{address}{urlencode({'in_place': held})}", headers=PAGE_HEADERS)
            landings.append(landing_in(moved.headers["location"]))
            pages.append(redirected(client, moved))

    assert len(InPlace.read(held).cards) == IN_PLACE_MAX
    assert link_named(plain.text, "Try again") == f"{PAGE}?{asked}"
    assert link_named(bare.text, "Try again") == PAGE
    assert [link_named(page.text, "Try again") for page in pages] == [
        f"{PAGE}?{asked}&landing={landings[0]}&{urlencode({'in_place': latest})}",
        f"{PAGE}?landing={landings[1]}&{urlencode({'in_place': latest})}",
    ]
    assert [cards_in(link_named(page.text, "Try again")) for page in pages] == [[latest]] * 2


@pytest.mark.parametrize("signed_in", [False, True], ids=["sign-in off", "signed in"])
@pytest.mark.parametrize(
    "press",
    ["refused", "not saved", "save of a card gone", "undo refused", "undo of a card gone"],
)
def test_a_press_whose_page_cannot_be_read_keeps_the_cards_in_place(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, signed_in: bool, press: str
) -> None:
    """A press from her week answered on the page that reads no store leaves no cookie for
    another visit to take: its way back carries the cards its form kept in place, so the way
    back shows them where they were, and a fresh visit groups the week by its updates."""
    with her_device(tmp_path, signed_in) as client:
        before = client.get(PAGE, headers=PAGE_HEADERS).text
        first = redirected(client, save(client, week_card(client, ESSAY), "done")).text
        other = card_for(first, SYLLABUS)
        store = store_of(client)
        if press == "not saved":
            store._connection.execute(
                "CREATE TRIGGER refuse_reports BEFORE INSERT ON student_reports "
                "BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )
            store._connection.commit()
        monkeypatch.setattr(student_routes, "student_page", refusing())
        if press.startswith("undo"):
            form = form_fields(card_for(first, ESSAY), UNDO_ESSAY)
            if press == "undo refused":
                form["report_id"] = "not-a-report"
            else:
                monkeypatch.setattr(store, "undo_report", refusing(UnknownAssignment))
            answer: Answer = client.post(UNDO_ESSAY, data=form, headers=PAGE_HEADERS)
            kept = InPlace.read(form["in_place"])
        else:
            if press == "save of a card gone":
                monkeypatch.setattr(store, "report_status", refusing(UnknownAssignment))
            status = None if press == "refused" else "not_yet"
            answer = save(client, other, status, assignment_id=SYLLABUS)
            kept = InPlace.read(hidden(other, "in_place"))
        monkeypatch.undo()
        back = client.get(f"{PAGE}?week={WEEK}", headers=PAGE_HEADERS)
        minute_passes(client)
        way_back = link_named(answer.text, "Back to the week")
        followed = client.get(way_back, headers=PAGE_HEADERS)
        later = redirected(client, followed)

    assert answer.status_code in (404, 422, 500)
    assert kept.cards
    assert IN_PLACE_COOKIE not in answer.headers.get("set-cookie", "")
    assert placed(back.text)[1] == [ESSAY]
    assert IN_PLACE_COOKIE not in back.headers.get("set-cookie", "")
    assert cards_in(way_back) == [kept.said()]
    landing = landing_in(followed.headers["location"])
    assert left_for_the_page(followed).startswith(f"{landing_cookie(landing)}={kept.said()};")
    assert "in_place" not in followed.headers["location"]
    assert placed(later.text) == placed(before)


def test_a_press_from_the_details_keeps_no_card_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """The details are not her week: a press there leaves no cookie, whatever its form
    names, when it saves and when its page can't be read."""
    action = student_routes.report_actions(ESSAY)[0]
    named = {"in_place": f"a:{place_key(QUIZ)}"}
    with browser() as client:
        form = whole_form(
            client.get(f"/student/assignments/{ESSAY}", headers=PAGE_HEADERS).text, action
        )
        monkeypatch.setattr(student_routes, "detail_page", refusing())
        refused = client.post(action, data={**form, **named}, headers=PAGE_HEADERS)
        undo = {name: form[name] for name in form if name not in ("status", "note")}
        not_undone = client.post(
            student_routes.report_actions(ESSAY)[1],
            data={**undo, "report_id": "not-a-report", **named},
            headers=PAGE_HEADERS,
        )
        monkeypatch.undo()
        saved = client.post(action, data={**form, **named, "status": "done"}, headers=PAGE_HEADERS)

    assert refused.status_code == not_undone.status_code == 422
    assert saved.status_code == 303
    for answer in (refused, not_undone, saved):
        assert IN_PLACE_COOKIE not in answer.headers.get("set-cookie", "")
    assert "landing=" not in saved.headers["location"]
    for answer in (refused, not_undone):
        assert "in_place=" not in answer.text


def test_the_same_update_is_already_saved_and_a_changed_note_is_a_new_one() -> None:
    with browser() as client:
        first = report(client, ESSAY, "done", "Finished.")
        same = report(client, ESSAY, "done", "  Finished.\r\n")
        page = client.get(same, headers=PAGE_HEADERS).text
        changed = report(client, ESSAY, "done", "Finished, all of it.")
        history = state_of(client).project_state.student_reports(ESSAY)

    assert first.endswith(f"&saved={ESSAY}#update-result-{ESSAY}")
    assert same.endswith(f"&same={ESSAY}#update-result-{ESSAY}")
    assert UPDATE_ALREADY_SAVED in card_for(page, ESSAY)
    assert changed.endswith(f"&saved={ESSAY}#update-result-{ESSAY}")
    assert [item.note for item in history] == ["Finished.", "Finished, all of it."]


def test_not_yet_says_what_it_means_inside_and_outside_todays_window() -> None:
    """Inside the window a Not yet stays work to plan; on a later week's page, an update on
    work outside the window says so, and gets today's day. That page says its updates are
    the latest on record."""
    with browser() as client:
        entered = client.post(
            "/parent/inbox/keep",
            data={"course": "Art", "title": "Poster", "due_date": "2026-09-10"},
        )
        assert entered.status_code == 303
        poster = next(
            item.assignment_id
            for item in state_of(client).project_state.all_assignments()
            if item.title == "Poster"
        )
        here = client.get(report(client, ESSAY, "not_yet"), headers=PAGE_HEADERS).text
        later_page = client.get(PAGE, params={"week": "2026-09-07"}, headers=PAGE_HEADERS).text
        head = hidden(card_for(later_page, poster), "expected_report_id")
        answer = client.post(
            f"/student/actions/assignments/{poster}/report",
            data={
                "status": "not_yet",
                "note": "",
                "expected_report_id": head,
                "week": "2026-09-07",
            },
        )
        later = client.get(answer.headers["location"], headers=PAGE_HEADERS).text
        history = state_of(client).project_state.student_reports(poster)

    assert "Still unfinished. It can be included in today's plan." in card_for(here, ESSAY)
    assert "Updates show the latest saved information" not in here
    assert answer.status_code == 303
    assert answer.headers["location"].startswith(f"{PAGE}?week=2026-09-07&saved={poster}")
    assert (
        "Updates show the latest saved information, even when you view a different week." in later
    )
    assert (
        "Saved as Not yet. This is due after August 25, so it isn't included in today's plan yet."
        in card_for(later, poster)
    )
    assert "Reported August 19" in card_for(later, poster)
    assert [item.reported_on for item in history] == [PLAN_DATE]


def test_a_missing_choice_or_a_long_note_returns_the_card_with_her_words_kept() -> None:
    with browser() as client:
        unchosen = client.post(
            f"/student/actions/assignments/{ESSAY}/report",
            data={"status": "", "note": "kept words", "expected_report_id": "", "week": WEEK},
        )
        too_long = client.post(
            f"/student/actions/assignments/{ESSAY}/report",
            data={"status": "done", "note": "x" * 501, "expected_report_id": "", "week": WEEK},
        )
        nothing = state_of(client).project_state.student_reports(ESSAY)

    assert unchosen.status_code == 422
    card = card_for(unchosen.text, ESSAY)
    assert CHOOSE_ONE in card
    assert "checked" not in card
    assert ">kept words</textarea>" in card
    assert '<details class="steps note-fold" open>' in card
    assert too_long.status_code == 422
    card = card_for(too_long.text, ESSAY)
    assert NOTE_TOO_LONG in card
    assert 'value="done" checked>' in card
    assert ">" + "x" * 501 + "</textarea>" in card
    assert nothing == []


def test_a_save_from_a_page_that_has_moved_on_is_refused_with_the_newer_update_shown() -> None:
    """Two devices open the same blank card. One saves Done; the other's Not yet is
    refused, 409, with the Done above its form and its own words kept, and its form now
    carries the newer update, so saving again lands. Two identical blank saves are one
    save and one already saved."""
    with browser() as client:
        first_page = client.get(PAGE, headers=PAGE_HEADERS).text
        stale_head = hidden(card_for(first_page, ESSAY), "expected_report_id")
        report(client, ESSAY, "done")
        refused = client.post(
            f"/student/actions/assignments/{ESSAY}/report",
            data={
                "status": "not_yet",
                "note": "still the last page",
                "expected_report_id": stale_head,
                "week": WEEK,
            },
        )
        card = card_for(refused.text, ESSAY)
        again = client.post(
            f"/student/actions/assignments/{ESSAY}/report",
            data={
                "status": "not_yet",
                "note": "still the last page",
                "expected_report_id": hidden(card, "expected_report_id"),
                "week": WEEK,
            },
        )
        blank_twice = client.post(
            f"/student/actions/assignments/{LOG}/report",
            data={"status": "done", "note": "", "expected_report_id": "", "week": WEEK},
        )
        blank_again = client.post(
            f"/student/actions/assignments/{LOG}/report",
            data={"status": "done", "note": "", "expected_report_id": "", "week": WEEK},
        )
        history = state_of(client).project_state.student_reports(ESSAY)

    assert refused.status_code == 409
    assert SAVED_ELSEWHERE in card
    assert '<span class="pill">Your update: Done</span>' in card
    assert 'value="not_yet" checked>' in card
    assert ">still the last page</textarea>" in card
    assert hidden(card, "expected_report_id") == history[0].report_id
    assert again.status_code == 303
    assert again.headers["location"].endswith(f"&saved={ESSAY}#update-result-{ESSAY}")
    assert [item.status for item in history] == ["done", "not_yet"]
    assert blank_twice.headers["location"].endswith(f"&saved={LOG}#update-result-{LOG}")
    assert blank_again.headers["location"].endswith(f"&same={LOG}#update-result-{LOG}")


def test_undo_restores_what_stood_before_and_a_stale_undo_is_refused() -> None:
    """Undoing a Done over a Not yet brings the Not yet back with its note and day, and
    offers no second undo; undoing the only update leaves the form; an undo naming an
    older update is refused with the card as it stands."""
    with browser() as client:
        report(client, ESSAY, "not_yet", "Half left.")
        page = client.get(report(client, ESSAY, "done"), headers=PAGE_HEADERS).text
        card = card_for(page, ESSAY)
        done_id = hidden(card, "report_id")
        undone = client.post(
            f"/student/actions/assignments/{ESSAY}/undo-report",
            data={"report_id": done_id, "week": WEEK},
        )
        restored_page = client.get(undone.headers["location"], headers=PAGE_HEADERS).text
        restored = card_for(restored_page, ESSAY)
        stale = client.post(
            f"/student/actions/assignments/{ESSAY}/undo-report",
            data={"report_id": done_id, "week": WEEK},
        )
        only = client.get(report(client, LOG, "done"), headers=PAGE_HEADERS).text
        to_nothing = client.post(
            f"/student/actions/assignments/{LOG}/undo-report",
            data={"report_id": hidden(card_for(only, LOG), "report_id"), "week": WEEK},
        )
        blank_again = card_for(
            client.get(to_nothing.headers["location"], headers=PAGE_HEADERS).text, LOG
        )
        statuses = statuses_for(state_of(client).project_state, [ESSAY, LOG])

    assert undone.status_code == 303
    assert undone.headers["location"] == (
        f"{PAGE}?week={WEEK}&undone={ESSAY}#update-result-{ESSAY}"
    )
    assert UPDATE_UNDONE in restored
    assert '<span class="pill">Your update: Not yet</span>' in restored
    assert "Reported August 19, restored August 19" in restored
    assert "You wrote: <q>Half left.</q>" in restored
    assert 'name="report_id"' not in restored
    assert ">Change<span" in restored
    assert stale.status_code == 409
    assert ALREADY_UNDONE in card_for(stale.text, ESSAY)
    assert f'href="#title-{ESSAY}"' in stale.text
    assert to_nothing.status_code == 303
    assert UPDATE_UNDONE in blank_again
    assert OFFERED in blank_again
    assert hidden(blank_again, "expected_report_id") == statuses[LOG].head_id
    assert (statuses[ESSAY].work_state, statuses[LOG].work_state) == ("not_yet", "unreported")


def test_the_change_button_opens_the_form_with_the_update_as_it_stands() -> None:
    with browser() as client:
        report(client, ESSAY, "not_yet", "Half left.")
        opened = client.get(PAGE, params={"week": WEEK, "change": ESSAY}, headers=PAGE_HEADERS).text
        card = card_for(opened, ESSAY)

    assert 'value="not_yet" checked>' in card
    assert ">Half left.</textarea>" in card
    assert '<span class="pill">Your update: Not yet</span>' in card
    assert (
        'aria-label="Keep it as it is: your update on Canal Era comparison essay, World '
        'History">Keep it as it is</a>'
    ) in card
    assert ">Change<span" not in card


def test_a_parent_signed_in_reads_her_update_and_cannot_make_one(tmp_path: pathlib.Path) -> None:
    """With the sign-in on, a parent's device sees her update as hers, is told to sign in as
    the student to change it, and is answered 403 when it tries; her device gets the form.
    A device with no sign-in is sent to sign in, as for any form."""
    with TestClient(
        create_app(signed_in_household(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        anonymous = client.post(
            f"/student/actions/assignments/{ESSAY}/report",
            data={"status": "done", "note": "", "expected_report_id": "", "week": WEEK},
            headers=PAGE_HEADERS,
        )
        client.post("/sign-in", data={"passphrase": THEIRS})
        as_parent = client.get(PAGE, headers=PAGE_HEADERS).text
        parent_card = card_for(as_parent, ESSAY)
        refused = client.post(
            f"/student/actions/assignments/{ESSAY}/report",
            data={"status": "done", "note": "", "expected_report_id": "", "week": WEEK},
            headers=PAGE_HEADERS,
        )
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": HERS})
        as_her = client.get(PAGE, headers=PAGE_HEADERS).text
        saved = client.get(report(client, ESSAY, "done", "On paper."), headers=PAGE_HEADERS).text
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        parent_after = card_for(client.get(PAGE, headers=PAGE_HEADERS).text, ESSAY)
        undo_refused = client.post(
            f"/student/actions/assignments/{ESSAY}/undo-report",
            data={"report_id": "report-x", "week": WEEK},
            headers=PAGE_HEADERS,
        )
        history = state_of(client).project_state.student_reports(ESSAY)

    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == "/sign-in"
    assert "No student update yet. Sign in as the student to update." in parent_card
    assert OFFERED not in parent_card
    assert refused.status_code == 403
    assert NOT_HERS_TO_UPDATE in refused.text
    assert OFFERED in card_for(as_her, ESSAY)
    assert '<span class="pill">Your update: Done</span>' in card_for(saved, ESSAY)
    assert '<span class="pill">Student update: Done</span>' in parent_after
    assert "She wrote: <q>On paper.</q>" in parent_after
    assert "This is out of work to plan. Her school record is separate." in parent_after
    assert "This is out of work to plan. Your school record is separate." in card_for(saved, ESSAY)
    assert "Sign in as the student to update." in parent_after
    assert ">Change<span" not in parent_after
    assert "undo-report" not in parent_after
    assert undo_refused.status_code == 403
    assert len(history) == 1


def test_a_plan_that_speaks_about_work_she_has_since_finished_says_so_on_both_pages() -> None:
    """The notice names the work on her page and the family page, stays whatever a parent
    decided, and a plan from before plans carried their ids gets the general notice."""
    with browser(key=True) as client:
        planned = client.post("/student/actions/plan")
        assert planned.status_code == 303
        quiet = client.get(PAGE, headers=PAGE_HEADERS).text
        report(client, ESSAY, "done")
        hers = client.get(PAGE, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text
        over_json = client.get("/student/plans/today").json()
        draft_id = client.get("/parent/approvals").json()["waiting"][0]["draft_id"]
        state = state_of(client)
        state.drafts._connection.execute("UPDATE drafts SET plan_assignment_ids=NULL")
        state.drafts._connection.commit()
        legacy = client.get(PAGE, headers=PAGE_HEADERS).text
        legacy_family = client.get("/parent", headers=PAGE_HEADERS).text

    assert "<strong>Your updates.</strong>" not in quiet
    assert PLAN_INCLUDES_DONE == "This plan includes work you now report as Done."
    assert PLAN_INCLUDES_DONE in hers
    assert (
        f'In it: <a class="assignment-link" href="/student/assignments/{ESSAY}?return_to=today" '
        f'aria-label="{ESSAY_TITLE}, World History">{ESSAY_TITLE}</a> (World History).'
    ) in hers
    assert "A new plan will leave it out." in hers
    assert (
        f'<strong>Student updates.</strong> {SHE_REPORTS} In it: <a class="assignment-link" href='
    ) in family
    assert NAMED_BY_ITS_ROW in family
    assert f"({ESSAY})" not in hers
    assert over_json["reported_done"] == PLAN_INCLUDES_DONE
    assert over_json["reported_done_work"] == [{"assignment_id": ESSAY, "title": ESSAY_TITLE}]
    assert "Reported done since" not in hers + family
    assert draft_id
    assert str(escape(PLAN_WINDOW_DONE)) in legacy
    assert str(escape("Some work in this plan's window is now reported Done.")) in legacy_family


def test_with_everything_in_the_window_reported_done_her_page_offers_no_plan_button() -> None:
    """The panel says nothing is left to schedule and the button is gone; the family page
    keeps its form, which the route refuses."""
    with browser(key=True) as client:
        state = state_of(client)
        for item in state.project_state.all_assignments():
            report(client, item.assignment_id, "done")
        hers = client.get(PAGE, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text
        posted = client.post("/parent/actions/plan", data={"plan_date": ""})

    assert NOTHING_TO_SCHEDULE in hers
    assert 'action="/student/actions/plan"' not in hers
    assert "Planning uses the model provider." not in hers
    assert 'action="/parent/actions/plan"' in family
    assert posted.status_code == 409
    assert NOTHING_TO_SCHEDULE in posted.text


def test_her_done_beside_the_schools_missing_is_something_to_check_on_both_pages() -> None:
    """Her page says one finished assignment has a school report to check and links to the
    card, which opens folded group; the family page lists it as worth checking together,
    with both statements and their days, and not under the school's reports. A later
    Missing from the school leaves her Done standing."""
    with browser() as client:
        told = client.post("/parent/inbox/keep", data={"text": MISSING_EMAIL})
        assert told.status_code == 303
        report(client, ESSAY, "done", "Handed in Tuesday.")
        hers = client.get(PAGE, headers=PAGE_HEADERS).text
        link = re.search(
            r'<a href="([^"]+)" aria-label="Canal Era comparison essay, World History: '
            r'school report to check">Canal Era comparison essay</a>',
            hers,
        )
        assert link is not None
        followed = client.get(link.group(1).replace("&amp;", "&"), headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text
        told_again = client.post("/parent/inbox/keep", data={"text": MISSING_EMAIL})
        still = statuses_for(state_of(client).project_state, [ESSAY])[ESSAY]

    assert "<strong>1 finished assignment\n      has a school report to check:</strong>" in hers
    assert f"show={ESSAY}#title-{ESSAY}" in link.group(1)
    assert '<details class="steps reported-done" open>' in followed
    assert "<strong>The school reports this missing.</strong>" in card_for(followed, ESSAY)
    checking, _, rest = family.partition("<h3>School reports</h3>")
    assert "<h3>Worth checking together</h3>" in checking
    assert "She reported it done on August 19. She wrote: <q>Handed in Tuesday.</q>" in checking
    assert "The school reports it missing. From the school email, pasted" in checking
    assert "<strong>Check the school record.</strong>" in checking
    assert "Blossom is not scheduling more homework for this assignment." in checking
    assert family.count(f'id="update-{ESSAY}"') == 1
    assert "Recent updates" not in family
    assert ESSAY_TITLE not in rest
    assert told_again.status_code == 303
    assert (still.work_state, still.check_the_school_record) == ("done", True)


def test_the_assigned_later_list_takes_her_update_the_same_way() -> None:
    with browser() as client:
        before = client.get(PAGE, headers=PAGE_HEADERS).text
        location = report(client, LOG, "done")
        after = client.get(location, headers=PAGE_HEADERS).text

    _, _, later_before = before.partition("Assigned this week, due later")
    assert OFFERED in card_for(later_before, LOG)
    _, _, later_after = after.partition("Assigned this week, due later")
    assert "<summary>Reported done (1)</summary>" in later_after
    assert '<span class="pill">Your update: Done</span>' in card_for(later_after, LOG)


def test_reading_the_statuses_takes_the_same_few_reads_whatever_the_number_of_rows(
    tmp_path: pathlib.Path,
) -> None:
    def rows(count: int) -> list[Assignment]:
        return [
            Assignment(
                assignment_id=f"assignment-{n}",
                course="Math",
                title=f"Sheet {n}",
                due_date=date(2026, 8, 21),
                dependencies=[],
                reported_submission_status="not_started",
                kind=AssignmentKind.HOMEWORK,
            )
            for n in range(count)
        ]

    def reads(count: int) -> int:
        store = ProjectStateStore.open(tmp_path / f"{count}.sqlite3", fixture_clock())
        try:
            store.put_on_record(rows(count), {})
            now = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)
            for n in range(0, count, 2):
                first = store.report_status(
                    f"assignment-{n}", "not_yet", None, expected_head=None, now=now, today=PLAN_DATE
                )
                assert isinstance(first, Saved)
                second = store.report_status(
                    f"assignment-{n}",
                    "done",
                    None,
                    expected_head=first.report.report_id,
                    now=now,
                    today=PLAN_DATE,
                )
                assert isinstance(second, Saved)
                store.undo_report(
                    f"assignment-{n}", second.report.report_id, now=now, today=PLAN_DATE
                )
            statements: list[str] = []
            store._connection.set_trace_callback(statements.append)
            statuses_for(store, [f"assignment-{n}" for n in range(count)])
            store._connection.set_trace_callback(None)
        finally:
            store.close()
        return len([s for s in statements if not s.startswith(("BEGIN", "COMMIT"))])

    few, many = reads(2), reads(40)

    assert few == many
    assert many == 3
    assert isinstance(sqlite3.connect(":memory:"), sqlite3.Connection)


@pytest.mark.parametrize("during", ["the planner", "the reviewer"])
def test_a_done_saved_while_a_model_is_asked_leaves_the_plan_stale_and_named(during: str) -> None:
    """Her Done lands while a model call is pending, the planner's or the reviewer's, and
    the run does the same either way. The save is not kept waiting for the model; each
    model is asked once; the draft keeps the fingerprint and the ids of what the run
    read; and the moment it is published it reads as stale on both pages, its notice
    names the essay, no approval is offered, and approving is refused."""
    planners: list[ReportsWhileAsked[DailyPlan] | Scripted[DailyPlan]] = []
    critics: list[ReportsWhileAsked[CriticVerdict] | Scripted[CriticVerdict]] = []

    def override(
        state: Annotated[ApplicationState, Depends(get_application_state)],
    ) -> PlanGraphs:
        if not planners:
            if during == "the planner":
                planners.append(ReportsWhileAsked(state, fixture_week_plan()))
                critics.append(Scripted(ok(accepting())))
            else:
                planners.append(Scripted(ok(fixture_week_plan())))
                critics.append(ReportsWhileAsked(state, accepting()))
        return PlanGraphs(
            build=lambda: plan_graph_for(state, planner=planners[0], critic=critics[0]),
            may_start=True,
        )

    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = override  # type: ignore[attr-defined]
        state = state_of(client)
        as_read = planning_digest(read_week(state.project_state, state.project_state, PLAN_DATE))
        planned = client.post("/student/actions/plan")
        record = state.drafts.latest_for(PLAN_DATE)
        as_it_stands = planning_digest(
            read_week(state.project_state, state.project_state, PLAN_DATE)
        )
        hers = client.get(PAGE, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text
        assert record is not None
        refused = client.post(
            f"/parent/actions/decide/{record.draft_id}", data={"decision": "approve"}
        )
        after = state.drafts.get(record.draft_id)

    held = planners[0] if during == "the planner" else critics[0]
    assert isinstance(held, ReportsWhileAsked)
    assert held.lock_was_free
    assert isinstance(held.saved, Saved)
    assert (len(planners[0].briefs), len(critics[0].briefs)) == (1, 1)
    assert f'id="{ESSAY}"' in human_text(planners[0].briefs[0])
    assert f'id="{ESSAY}"' in human_text(critics[0].briefs[0])
    assert planned.status_code == 303
    assert record.waiting
    assert record.inputs_digest == as_read
    assert as_it_stands != as_read
    assert record.plan_assignment_ids is not None
    assert ESSAY in record.plan_assignment_ids
    assert ASSIGNMENTS_CHANGED in hers
    assert PLAN_INCLUDES_DONE in hers
    assert NAMED_BY_ITS_ROW in hers
    assert THEIR_ASSIGNMENTS_CHANGED in family
    assert SHE_REPORTS in family
    assert NAMED_BY_ITS_ROW in family
    assert 'value="approve"' not in family
    assert refused.status_code == 409
    assert after is not None
    assert after.waiting


def test_work_finished_between_the_routes_question_and_the_runs_reading_ends_the_run_plainly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Everything is reported done after the route has asked whether there is work and
    before the run reads the week. The run ends at its first node: no model is asked, no
    draft is made, the plan already there stays hers, nothing is left in flight, and the
    family page says why, after a restart too, and never that the run was interrupted."""
    settings = fixture_settings(
        BLOSSOM_TODAY=PLAN_DATE.isoformat(),
        **{ANTHROPIC_API_KEY_VARIABLE: "not-a-key-and-never-sent"},
    )
    planners: list[Scripted[DailyPlan]] = []

    def override(
        state: Annotated[ApplicationState, Depends(get_application_state)],
    ) -> PlanGraphs:
        planner = Scripted(ok(fixture_week_plan()))
        planners.append(planner)
        return PlanGraphs(
            build=lambda: plan_graph_for(state, planner=planner, critic=Scripted(ok(accepting()))),
            may_start=True,
        )

    app = create_app(settings)
    app.dependency_overrides[plan_graphs] = override
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        state = state_of(client)
        first = client.post("/student/plans")
        kept = state.drafts.latest_for(PLAN_DATE)
        for item in state.project_state.all_assignments():
            report(client, item.assignment_id, "done")
        asked_before = sum(planner.calls for planner in planners)
        monkeypatch.setattr(student_routes, "require_work", lambda *_: None)
        late = client.post("/student/plans")
        asked_after = sum(planner.calls for planner in planners)
        ended = state.drafts.runs_without_a_draft()
        still = state.drafts.latest_for(PLAN_DATE)
        in_flight = set(state.in_flight)
    with TestClient(create_app(settings), headers=SAME_ORIGIN) as again:
        family = again.get("/parent", headers=PAGE_HEADERS).text

    assert first.status_code == 201
    assert late.status_code == 409
    assert late.json()["detail"] == NOTHING_TO_SCHEDULE
    assert (asked_before, asked_after) == (1, 1)
    assert [(run.outcome, [step.node for step in run.steps]) for run in ended] == [
        ("nothing_to_schedule", ["retrieve"])
    ]
    assert kept is not None
    assert still is not None
    assert still.draft_id == kept.draft_id
    assert in_flight == set()
    assert "The run ended because nothing was left to schedule." in family
    assert "interrupted" not in family


def test_the_missing_a_check_rests_on_is_shown_whatever_the_latest_report_says() -> None:
    """The school's email says Missing, then the portal says Submitted, and she reports
    Done. The check rests on the email's word, so her card and the family page show that
    report beside the portal's later one, and nothing the check points at is hidden."""
    submitted = StatusReport(
        status="submitted",
        channel=SourceChannel.LMS,
        reported_on=PLAN_DATE,
        dated_by=PASTE_DAY,
        observed_at=datetime(2026, 8, 19, 22, 30, tzinfo=UTC),
    )
    with browser() as client:
        client.post("/parent/inbox/keep", data={"text": MISSING_EMAIL})
        state_of(client).project_state.record_status_reports(ESSAY, [submitted])
        report(client, ESSAY, "done")
        hers = client.get(PAGE, params={"week": WEEK, "show": ESSAY}, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text
        status = statuses_for(state_of(client).project_state, [ESSAY])[ESSAY]

    card = hers[hers.index(f'id="assignment-{ESSAY}"') :]
    card = card[: card.index("</article>")]
    checking, _, _ = family.partition("<h3>School reports</h3>")
    assert status.check_the_school_record
    assert [item.channel for item in status.missing_reports] == [SourceChannel.EMAIL]
    assert "has a school report to check" in hers
    assert "<strong>The school reports this submitted.</strong>" in card
    assert "From the school portal, pasted" in card
    assert "<strong>The school reports this missing.</strong>" in card
    assert "From the school email, pasted" in card
    assert "<h3>Worth checking together</h3>" in checking
    assert "The school reports it missing. From the school email, pasted" in checking
    assert "The school reports it submitted. From the school portal, pasted" in checking


# ------------------------------------------------------------- the form, held to what it sends


def post_report(client: TestClient, assignment_id: str, **fields: str | list[str]) -> Answer:
    return client.post(
        f"/student/actions/assignments/{assignment_id}/report",
        data=fields,
        headers=PAGE_HEADERS,
    )


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"status": ["done", "not_yet"]}, id="two-choices"),
        pytest.param({"status": "done", "note": ["one", "two"]}, id="two-notes"),
        pytest.param({"status": "done", "expected_report_id": ["", ""]}, id="two-tokens"),
        pytest.param({"status": "done", "week": [WEEK, WEEK]}, id="two-weeks"),
        pytest.param({"status": "done", "channel": "LMS"}, id="a-channel"),
        pytest.param({"status": "done", "reported_on": "2026-01-01"}, id="a-day"),
    ],
)
def test_a_form_that_is_not_whole_writes_nothing_and_says_so(
    fields: dict[str, str | list[str]],
) -> None:
    sent: dict[str, str | list[str]] = {
        "note": "kept words",
        "expected_report_id": "",
        "week": WEEK,
        **fields,
    }
    with browser() as client:
        answer = post_report(client, ESSAY, **sent)
        nothing = state_of(client).project_state.student_reports(ESSAY)

    assert answer.status_code == 422
    assert BAD_FORM in card_for(answer.text, ESSAY)
    assert nothing == []


@pytest.mark.parametrize("left_out", ["note", "expected_report_id", "week"])
def test_a_form_with_a_field_left_out_writes_nothing_and_no_choice_is_still_asked_for(
    left_out: str,
) -> None:
    """Her browser sends the note, the update the card showed, and the week whether or not
    anything is in them, so a form without one is not the card's and is refused whole. Two
    radio buttons with none chosen send nothing, so a form without a status is the card's
    own, and the card asks her to choose."""
    sent = {"status": "done", "note": "", "expected_report_id": "", "week": WEEK}
    del sent[left_out]
    with browser() as client:
        answer = post_report(client, ESSAY, **sent)
        unchosen = post_report(client, ESSAY, note="kept words", expected_report_id="", week=WEEK)
        nothing = state_of(client).project_state.student_reports(ESSAY)

    assert answer.status_code == 422
    assert BAD_FORM in answer.text
    assert unchosen.status_code == 422
    assert CHOOSE_ONE in card_for(unchosen.text, ESSAY)
    assert "kept words</textarea>" in card_for(unchosen.text, ESSAY)
    assert nothing == []


def test_an_undo_form_that_is_not_whole_changes_nothing() -> None:
    with browser() as client:
        page = client.get(report(client, ESSAY, "done"), headers=PAGE_HEADERS).text
        named = hidden(card_for(page, ESSAY), "report_id")
        twice = client.post(
            f"/student/actions/assignments/{ESSAY}/undo-report",
            data={"report_id": [named, named], "week": WEEK},
        )
        extra = client.post(
            f"/student/actions/assignments/{ESSAY}/undo-report",
            data={"report_id": named, "week": WEEK, "status": "not_yet"},
        )
        history = state_of(client).project_state.student_reports(ESSAY)

    assert (twice.status_code, extra.status_code) == (422, 422)
    assert BAD_FORM in card_for(twice.text, ESSAY)
    assert [event.operation for event in history] == ["report"]


def test_a_form_must_name_an_update_of_its_own_card_whatever_else_it_says() -> None:
    """Forms read from the pages, then their token swapped: a made-up name, a name that is
    no id at all, and another card's real update are refused with the same update as the
    one standing and with another, and nothing is written. The card's own stale token
    with the same update is already saved. An undo is held to the same."""
    with browser() as client:
        report(client, LOG, "done")
        page = client.get(report(client, ESSAY, "done", "Finished."), headers=PAGE_HEADERS).text
        own = hidden(card_for(page, ESSAY), "report_id")
        other = hidden(card_for(page, LOG), "report_id")
        refusals = [
            post_report(
                client,
                ESSAY,
                status=status,
                note=note,
                expected_report_id=token,
                week=WEEK,
            )
            for token in ("report-000000000000", "' OR 1=1 --", other, "x" * 400)
            for status, note in (("done", "Finished."), ("not_yet", ""))
        ]
        report(client, ESSAY, "not_yet")
        stale_and_same = post_report(
            client, ESSAY, status="not_yet", note="", expected_report_id=own, week=WEEK
        )
        undo_unknown = client.post(
            f"/student/actions/assignments/{ESSAY}/undo-report",
            data={"report_id": "report-000000000000", "week": WEEK},
        )
        undo_another = client.post(
            f"/student/actions/assignments/{ESSAY}/undo-report",
            data={"report_id": other, "week": WEEK},
        )
        undo_stale = client.post(
            f"/student/actions/assignments/{ESSAY}/undo-report",
            data={"report_id": own, "week": WEEK},
        )
        history = state_of(client).project_state.student_reports(ESSAY)

    assert [answer.status_code for answer in refusals] == [422] * 8
    assert all(NOT_THIS_CARDS in card_for(a.text, ESSAY) for a in refusals)
    assert stale_and_same.status_code == 303
    assert stale_and_same.headers["location"].endswith(f"&same={ESSAY}#update-result-{ESSAY}")
    assert (undo_unknown.status_code, undo_another.status_code) == (422, 422)
    assert undo_stale.status_code == 409
    assert CANNOT_UNDO in card_for(undo_stale.text, ESSAY)
    assert [event.status for event in history] == ["done", "not_yet"]


# ------------------------------------------------------------- a save the file refuses


def test_a_save_the_file_refuses_keeps_her_words_and_says_nothing_of_a_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The insert is refused, then the write fails after the insert: each time the page
    comes back with her choice and her words, no redirect, no word of a save, and no
    event. With the fault gone, the same form saves. An undo the file refuses says so
    and leaves her update standing."""
    with browser() as client:
        store = state_of(client).project_state
        store._connection.execute(
            "CREATE TRIGGER refuse_reports BEFORE INSERT ON student_reports "
            "BEGIN SELECT RAISE(ABORT, 'refused'); END"
        )
        store._connection.commit()
        refused = post_report(
            client, ESSAY, status="done", note="kept <words>", expected_report_id="", week=WEEK
        )
        store._connection.execute("DROP TRIGGER refuse_reports")
        store._connection.commit()
        after_refusal = store.student_reports(ESSAY)

        def fail(_: object) -> None:
            msg = "the head could not be read back"
            raise RuntimeError(msg)

        monkeypatch.setattr(store, "_confirm_head_locked", fail)
        failed_late = post_report(
            client, ESSAY, status="done", note="kept <words>", expected_report_id="", week=WEEK
        )
        monkeypatch.undo()
        after_late_failure = store.student_reports(ESSAY)
        card = card_for(failed_late.text, ESSAY)
        retried = post_report(
            client,
            ESSAY,
            status="done",
            note="kept <words>",
            expected_report_id=hidden(card, "expected_report_id"),
            week=WEEK,
        )
        saved_page = client.get(retried.headers["location"], headers=PAGE_HEADERS).text
        store._connection.execute(
            "CREATE TRIGGER refuse_reports BEFORE INSERT ON student_reports "
            "BEGIN SELECT RAISE(ABORT, 'refused'); END"
        )
        store._connection.commit()
        undo_refused = client.post(
            f"/student/actions/assignments/{ESSAY}/undo-report",
            data={"report_id": hidden(card_for(saved_page, ESSAY), "report_id"), "week": WEEK},
        )
        store._connection.execute("DROP TRIGGER refuse_reports")
        store._connection.commit()
        standing = statuses_for(store, [ESSAY])[ESSAY]

    for answer in (refused, failed_late):
        body = card_for(answer.text, ESSAY)
        assert answer.status_code == 500
        assert NOT_SAVED in body
        assert UPDATE_SAVED not in answer.text
        assert 'value="done" checked' in body
        assert ">kept &lt;words&gt;</textarea>" in body
    assert after_refusal == []
    assert after_late_failure == []
    assert retried.status_code == 303
    assert undo_refused.status_code == 500
    assert NOT_UNDONE in card_for(undo_refused.text, ESSAY)
    assert UPDATE_UNDONE not in undo_refused.text
    assert (standing.status, standing.note) == ("done", "kept <words>")


def test_when_her_week_cannot_be_read_back_either_her_words_still_come_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        store._connection.execute(
            "CREATE TRIGGER refuse_reports BEFORE INSERT ON student_reports "
            "BEGIN SELECT RAISE(ABORT, 'refused'); END"
        )
        store._connection.commit()

        def unreadable(*_: object, **__: object) -> None:
            msg = "the week could not be read"
            raise RuntimeError(msg)

        monkeypatch.setattr(student_routes, "student_page", unreadable)
        answer = post_report(
            client, ESSAY, status="not_yet", note="kept <words>", expected_report_id="", week=WEEK
        )

    assert answer.status_code == 500
    text = answer.text
    assert NOT_SAVED in text
    assert "Your choice: Not yet." in text
    assert "readonly>kept &lt;words&gt;</textarea>" in text


# ------------------------------------------------------------- a card whose dates moved


def test_a_card_whose_dates_took_it_out_of_the_week_is_still_shown_with_the_result() -> None:
    """Two forms from one page; one saves, then the assignment's date moves out of the
    week. The other form's save is refused, 409, and the page still shows the card, apart,
    with the newer update, her choice and words, and the head for another try; a long
    note is refused the same way; and the save that then lands shows its confirmation on
    the card, apart."""
    with browser() as client:
        entered = client.post(
            "/parent/inbox/keep",
            data={"course": "Art", "title": "Poster", "due_date": "2026-08-20"},
        )
        assert entered.status_code == 303
        store = state_of(client).project_state
        poster = next(
            item.assignment_id for item in store.all_assignments() if item.title == "Poster"
        )
        first_page = client.get(PAGE, headers=PAGE_HEADERS).text
        stale_head = hidden(card_for(first_page, poster), "expected_report_id")
        report(client, poster, "done")
        store._connection.execute(
            "UPDATE assignments SET due_date='2026-10-08' WHERE assignment_id=?", (poster,)
        )
        store._connection.execute("DELETE FROM date_claims WHERE assignment_id=?", (poster,))
        store._connection.commit()
        plain = client.get(PAGE, params={"week": WEEK}, headers=PAGE_HEADERS).text
        refused = post_report(
            client,
            poster,
            status="not_yet",
            note="still the last corner",
            expected_report_id=stale_head,
            week=WEEK,
        )
        apart = card_for(refused.text, poster)
        too_long = post_report(
            client,
            poster,
            status="not_yet",
            note="x" * 501,
            expected_report_id=hidden(apart, "expected_report_id"),
            week=WEEK,
        )
        landed = post_report(
            client,
            poster,
            status="not_yet",
            note="still the last corner",
            expected_report_id=hidden(apart, "expected_report_id"),
            week=WEEK,
        )
        after = client.get(landed.headers["location"], headers=PAGE_HEADERS).text

    assert f'id="assignment-{poster}"' not in plain
    assert refused.status_code == 409
    assert "<h2>Outside the week shown</h2>" in refused.text
    assert SAVED_ELSEWHERE in apart
    assert '<span class="pill">Your update: Done</span>' in apart
    assert 'value="not_yet" checked' in apart
    assert ">still the last corner</textarea>" in apart
    assert "Due Thursday, October 8" in apart
    assert too_long.status_code == 422
    assert NOTE_TOO_LONG in card_for(too_long.text, poster)
    assert landed.status_code == 303
    assert "<h2>Outside the week shown</h2>" in after
    assert UPDATE_SAVED in card_for(after, poster)
    assert '<span class="pill">Your update: Not yet</span>' in card_for(after, poster)


# ------------------------------------------------- where the page lands, and what it says


def test_change_and_errors_land_on_the_card_and_name_the_field() -> None:
    """Change goes to the group of the card's form and the way back opens the fold it sits in;
    a missing choice puts the cursor on the first choice and ties the words to the group;
    a long note marks the field, ties the words and the hint to it, and takes the cursor;
    a refusal about neither is the card's alert and takes the focus there, and the top of
    the page repeats it with a link to the card."""
    with browser() as client:
        saved = client.get(report(client, ESSAY, "done", "Finished."), headers=PAGE_HEADERS).text
        changing = client.get(
            PAGE, params={"week": WEEK, "change": ESSAY}, headers=PAGE_HEADERS
        ).text
        unchosen = post_report(client, LOG, status="", note="", expected_report_id="", week=WEEK)
        too_long = post_report(
            client, LOG, status="done", note="x" * 501, expected_report_id="", week=WEEK
        )
        report(client, QUIZ, "done")
        conflict = post_report(
            client, QUIZ, status="not_yet", note="", expected_report_id="", week=WEEK
        )

    card = card_for(saved, ESSAY)
    assert f'action="/student/due-this-week#update-choice-{ESSAY}"' in card
    back = card_for(changing, ESSAY)
    assert f"week={WEEK}&amp;show={ESSAY}&amp;in_place=d%3A{place_key(ESSAY)}#title-{ESSAY}" in back
    assert '<details class="steps reported-done" open>' in changing
    group = card_for(unchosen.text, LOG)
    assert (
        f'<fieldset class="choice" id="update-choice-{LOG}" tabindex="-1" '
        f'aria-describedby="update-problem-{LOG}">'
    ) in group
    assert 'value="done" autofocus>' in group
    assert 'aria-invalid="true"' not in group
    field = card_for(too_long.text, LOG)
    assert (
        f'aria-describedby="update-problem-{LOG} update-hint-{LOG}" aria-invalid="true" autofocus>'
        in field
    )
    assert '<details class="steps note-fold" open>' in field
    assert 'value="done" checked>' in field
    top = conflict.text
    assert (
        f'<p class="problem week-problem">{SAVED_ELSEWHERE} '
        f'<a href="#title-{QUIZ}">Go to the assignment.</a></p>' in top
    )
    assert (
        f'<p class="problem" role="alert" id="update-problem-{QUIZ}" tabindex="-1" '
        f"autofocus>{SAVED_ELSEWHERE}</p>" in card_for(top, QUIZ)
    )
    assert top.count(" autofocus") == 1
    assert top.count('role="alert"') == 1


def test_the_note_limit_is_five_hundred_characters_as_the_server_counts_them() -> None:
    """Five hundred characters from beyond the basic plane, a thousand units as a browser's
    own limit would count them, are saved whole; one more is refused with every one kept;
    and edges and line endings are not counted against her."""
    exactly = "\U0001f33c" * 500
    with browser() as client:
        saved = post_report(
            client, ESSAY, status="done", note=f"  {exactly}\r\n", expected_report_id="", week=WEEK
        )
        refused = post_report(
            client,
            LOG,
            status="done",
            note=exactly + "\U0001f33c",
            expected_report_id="",
            week=WEEK,
        )
        kept = state_of(client).project_state.student_reports(ESSAY)
        nothing = state_of(client).project_state.student_reports(LOG)

    assert saved.status_code == 303
    assert [event.note for event in kept] == [exactly]
    assert refused.status_code == 422
    assert exactly + "\U0001f33c</textarea>" in card_for(refused.text, LOG)
    assert nothing == []


# ------------------------------------------------------------- her history, and the school's, apart


def test_the_history_fold_lists_her_updates_and_corrections_and_the_schools_reports_apart(
    tmp_path: pathlib.Path,
) -> None:
    """Not yet with a note, then Done with another, then the Done taken back: the fold lists
    all three with their days, the first note included, and says what the correction put
    back; the school's reports are listed apart under their own label; a card with
    nothing more to say than it shows has no fold; and a parent reads the same fold with
    no way to change anything."""
    with TestClient(
        create_app(signed_in_household(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        client.post("/sign-in", data={"passphrase": THEIRS})
        client.post("/parent/inbox/keep", data={"text": MISSING_EMAIL})
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": HERS})
        report(client, ESSAY, "not_yet", "The first note.")
        page = client.get(
            report(client, ESSAY, "done", "The second note."), headers=PAGE_HEADERS
        ).text
        client.post(
            f"/student/actions/assignments/{ESSAY}/undo-report",
            data={"report_id": hidden(card_for(page, ESSAY), "report_id"), "week": WEEK},
        )
        hers = client.get(PAGE, headers=PAGE_HEADERS).text
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        theirs = client.get(PAGE, headers=PAGE_HEADERS).text

    card = card_for(hers, ESSAY)
    assert "<summary>Update history" in card
    fold = card[card.index("<summary>Update history") :]
    assert '<p class="history-label">Your updates</p>' in fold
    assert "Not yet. <q>The first note.</q>" in fold
    assert "Done. <q>The second note.</q>" in fold
    assert "Correction. The update before it was taken back, which restored Not yet" in fold
    assert "from August 19" in fold
    assert (
        fold.index("The first note.") < fold.index("The second note.") < fold.index("Correction.")
    )
    assert '<p class="history-label">From the school</p>' in fold
    assert "Missing. From the school email, pasted" in fold
    assert "<summary>Update history" not in card_for(hers, LOG)
    parent_card = card_for(theirs, ESSAY)
    assert '<p class="history-label">Student updates</p>' in parent_card
    assert "The second note." in parent_card
    assert "undo-report" not in parent_card
    assert ">Change<span" not in parent_card


# ------------------------------------------------------------- the family page's groups


def test_each_assignment_is_in_one_family_group_with_every_fact_it_was_grouped_by() -> None:
    """Done beside Missing is worth checking; a recent Not yet beside Missing is a recent
    update, with the school's word in its row; a recent Done the school has said nothing
    about is a recent update; and a school report on work she has said nothing about is
    under the school's reports. No assignment is in two groups."""
    science = "assignment-science-fair-proposal"
    cover = "assignment-textbook-cover"
    with browser() as client:
        store = state_of(client).project_state
        for name in (ESSAY, LOG, cover):
            store.record_status_reports(
                name, [school_said("missing", SourceChannel.EMAIL, date(2026, 8, 18))]
            )
        report(client, ESSAY, "done")
        report(client, LOG, "not_yet", "Two chapters left.")
        report(client, science, "done")
        family = client.get("/parent", headers=PAGE_HEADERS).text

    section = family[family.index("<h2>Assignment updates</h2>") :]
    section = section[: section.index("<h2>Waiting for your review</h2>")]
    checking, _, rest = section.partition("<summary>Recent updates (2)</summary>")
    recent, _, school = rest.partition("<h3>School reports</h3>")
    titles = {
        ESSAY: ESSAY_TITLE,
        LOG: "Reading log, week one",
        science: "Science fair topic proposal",
        cover: "Cover the textbook",
    }
    assert [name for name in titles if titles[name] in checking] == [ESSAY]
    assert [name for name in titles if titles[name] in recent] == [LOG, science]
    assert [name for name in titles if titles[name] in school] == [cover]
    assert (
        "She reported it not yet done on August 19. She wrote: <q>Two chapters left.</q>" in recent
    )
    assert (
        "The school reports it missing. From the school email, pasted Tuesday, August 18" in recent
    )
    assert "the school reports it missing.</strong>" in school


@pytest.mark.parametrize(
    ("said", "checks", "shown"),
    [
        pytest.param(
            [("missing", SourceChannel.EMAIL, 18), ("submitted", SourceChannel.LMS, 19)],
            True,
            [
                "missing. From the school email, pasted Tuesday, August 18",
                "submitted. From the school portal, pasted Wednesday, August 19",
            ],
            id="email-missing-then-portal-submitted",
        ),
        pytest.param(
            [("missing", SourceChannel.LMS, 18), ("submitted", SourceChannel.EMAIL, 19)],
            True,
            [
                "missing. From the school portal, pasted Tuesday, August 18",
                "submitted. From the school email, pasted Wednesday, August 19",
            ],
            id="portal-missing-then-email-submitted",
        ),
        pytest.param(
            [("missing", SourceChannel.EMAIL, 17), ("missing", SourceChannel.LMS, 19)],
            True,
            [
                "missing. From the school email, pasted Monday, August 17",
                "missing. From the school portal, pasted Wednesday, August 19",
            ],
            id="two-channels-missing-on-different-days",
        ),
        pytest.param(
            [("missing", SourceChannel.EMAIL, 18), ("submitted", SourceChannel.EMAIL, 19)],
            False,
            ["submitted. From the school email, pasted Wednesday, August 19"],
            id="one-channel-missing-then-submitted",
        ),
    ],
)
def test_both_pages_show_what_each_school_channel_says_now_with_its_day(
    said: list[tuple[str, SourceChannel, int]], checks: bool, shown: list[str]
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        for status, channel, day in said:
            store.record_status_reports(ESSAY, [school_said(status, channel, date(2026, 8, day))])
        report(client, ESSAY, "done")
        hers = client.get(PAGE, params={"week": WEEK, "show": ESSAY}, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text

    card = card_for(hers, ESSAY)
    banners = card[: card.index('<div class="update">')]
    section = family[family.index("<h2>Assignment updates</h2>") :]
    for fact in shown:
        assert (
            f"The school reports this {fact}".replace(". From", ".</strong>\n          From")
            in banners
        )
        assert f"The school reports it {fact}" in section
    assert banners.count("<strong>The school reports this") == len(shown)
    assert ("has a school report to check" in hers) is checks
    assert ("<h3>Worth checking together</h3>" in section) is checks


def test_her_words_wrap_and_keep_their_lines_on_both_pages() -> None:
    """The rules the pages rely on for a long unbroken note: it wraps rather than widening
    the page, and the line breaks she typed are kept, in her card and the family's rows."""
    css = (REPOSITORY_ROOT / "blossom" / "static" / "blossom.css").read_text(encoding="utf-8")

    assert ".assignment-updates,\n.update,\n.history {\n  overflow-wrap: anywhere;\n}" in css
    assert ".assignment-updates q,\n.update q,\n.history q {\n  white-space: pre-line;\n}" in css
    assert ".visually-hidden {" in css


# ------------------------------------------------- the Today panel with nothing left to plan


def today_panel(page: str) -> str:
    start = page.index('<section class="panel today" id="today"')
    return page[start : page.index('<h2 class="list-heading"')]


def finish_everything(client: TestClient) -> None:
    for item in state_of(client).project_state.all_assignments():
        report(client, item.assignment_id, "done")


def asks_for_nothing(panel: str) -> bool:
    """Whether the panel points her at no plan she cannot ask for."""
    return not any(
        words in panel
        for words in (
            'action="/student/actions/plan"',
            "Plan again",
            "plan again",
            "Make a smaller plan",
            "Make a new plan",
            "You can start anyway",
            "A new plan will leave it out",
            "A plan is ready.",
        )
    )


@pytest.mark.parametrize("decision", ["waiting", "refuse", "approve"])
def test_with_everything_done_the_today_panel_informs_and_asks_for_nothing(decision: str) -> None:
    """A plan is made, left waiting or decided, and then she reports everything done. The
    panel keeps the saved plan, what a parent said, the exact notice with the work named,
    and that the plan differs from the record, and nothing in it sends her to a plan she
    cannot ask for."""
    with browser(key=True) as client:
        assert client.post("/student/actions/plan").status_code == 303
        draft_id = client.get("/parent/approvals").json()["waiting"][0]["draft_id"]
        if decision != "waiting":
            decided = client.post(
                f"/parent/actions/decide/{draft_id}",
                data={"decision": decision, "reason": "Start with the essay."},
            )
            assert decided.status_code == 303
        finish_everything(client)
        page = client.get(PAGE, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text

    panel = today_panel(page)
    assert asks_for_nothing(panel), panel
    assert "Your saved plan is below." in panel
    assert NOTHING_TO_SCHEDULE in panel
    assert PLAN_INCLUDES_DONE in panel
    assert "In it: " in panel
    assert f"{ESSAY_TITLE}</a> (World History)," in panel
    assert "View today's plan" in panel
    assert 'action="/parent/actions/plan"' in family
    if decision == "waiting":
        assert "A parent has not reviewed it yet." in panel
        assert "<strong>Saved plan.</strong>" in panel
        assert "The work or updates on record differ from what this plan used." in panel
    elif decision == "refuse":
        assert "A change was asked for on this saved plan." in panel
        assert "<q>Start with the essay.</q>" in panel
    else:
        assert "Looks good." in panel
        assert "<q>Start with the essay.</q>" in panel


def test_a_plan_from_before_ids_and_a_smaller_evening_ask_for_nothing_either() -> None:
    """The plan carries no ids and she has said today is too much: the panel gives the
    general notice about the plan's window, names no work, and offers no smaller plan."""
    with browser(key=True) as client:
        assert client.post("/student/actions/plan").status_code == 303
        state = state_of(client)
        state.drafts._connection.execute("UPDATE drafts SET plan_assignment_ids=NULL")
        state.drafts._connection.commit()
        assert client.post("/student/actions/too-much").status_code == 303
        finish_everything(client)
        page = client.get(PAGE, headers=PAGE_HEADERS).text

    panel = today_panel(page)
    assert asks_for_nothing(panel), panel
    assert PLAN_WINDOW_DONE == "Some work in this plan's window is now reported Done."
    assert str(escape(PLAN_WINDOW_DONE)) in panel
    assert "In it:" not in panel
    assert NOTHING_TO_SCHEDULE in panel
    assert "You said it was too much" in panel
    assert "<strong>Saved plan.</strong>" in panel


def test_a_not_yet_in_the_window_brings_the_plan_button_back_and_one_outside_does_not() -> None:
    with browser(key=True) as client:
        assert client.post("/student/actions/plan").status_code == 303
        entered = client.post(
            "/parent/inbox/keep",
            data={"course": "Art", "title": "Poster", "due_date": "2026-09-10"},
        )
        assert entered.status_code == 303
        poster = next(
            item.assignment_id
            for item in state_of(client).project_state.all_assignments()
            if item.title == "Poster"
        )
        finish_everything(client)
        far_off = client.get(PAGE, params={"week": "2026-09-07", "change": poster}).text
        outside = client.post(
            f"/student/actions/assignments/{poster}/report",
            data={
                "status": "not_yet",
                "note": "",
                "expected_report_id": hidden(card_for(far_off, poster), "expected_report_id"),
                "week": "2026-09-07",
            },
        )
        still_nothing = today_panel(client.get(PAGE, headers=PAGE_HEADERS).text)
        later_week = client.get(outside.headers["location"], headers=PAGE_HEADERS).text
        report(client, ESSAY, "not_yet")
        back = today_panel(client.get(PAGE, headers=PAGE_HEADERS).text)

    assert outside.status_code == 303
    assert asks_for_nothing(still_nothing), still_nothing
    assert "This is due after August 25, so it isn't included in today's plan yet." in card_for(
        later_week, poster
    )
    assert 'action="/student/actions/plan"' in back
    assert ">Plan again</button>" in back
    assert "A plan is ready." in back
    assert ASSIGNMENTS_CHANGED in back
    assert "A new plan will leave it out." in back
    assert NOTHING_TO_SCHEDULE not in back


def test_an_old_update_put_back_today_is_recent_by_its_correction_and_dated_as_it_is() -> None:
    """A Not yet from the first of the month, then a Done the same day, and today the Done
    taken back. Her latest event is today's correction, so the assignment is a recent
    update, sorted ahead of a Done made earlier today, and it reads with the old update's
    own day and the day it was restored. An old update left as it is stays out of the
    fold, and reachable from its week."""
    early = datetime(2026, 8, 1, 22, 0, tzinfo=UTC)
    on = date(2026, 8, 1)
    with browser() as client:
        store = state_of(client).project_state
        first = store.report_status(
            ESSAY, "not_yet", "Two parts left.", expected_head=None, now=early, today=on
        )
        assert isinstance(first, Saved)
        done = store.report_status(
            ESSAY, "done", None, expected_head=first.report.report_id, now=early, today=on
        )
        assert isinstance(done, Saved)
        left = store.report_status(QUIZ, "done", None, expected_head=None, now=early, today=on)
        assert isinstance(left, Saved)
        report(client, LOG, "done")
        taken_back = client.post(
            f"/student/actions/assignments/{ESSAY}/undo-report",
            data={"report_id": done.report.report_id, "week": WEEK},
        )
        family = client.get("/parent", headers=PAGE_HEADERS).text

    assert taken_back.status_code == 303
    section = family[family.index("<h2>Assignment updates</h2>") :]
    section = section[: section.index("<h2>Waiting for your review</h2>")]
    assert "<summary>Recent updates (2)</summary>" in section
    fold = section[section.index("<summary>Recent updates (2)</summary>") :]
    assert fold.index(ESSAY_TITLE) < fold.index("Reading log, week one")
    assert (
        "She reported it not yet done on August 1, restored August 19. "
        "She wrote: <q>Two parts left.</q>"
    ) in fold
    assert "Vocabulary quiz, unit one" not in section


def test_taking_back_her_only_update_is_recent_activity_that_says_no_update_stands() -> None:
    """A Done on the essay, then taken back: her latest event is today's correction, so the
    essay is a recent update, saying that she took the update back and none stands; her
    card offers the form again."""
    with browser() as client:
        page = client.get(report(client, ESSAY, "done"), headers=PAGE_HEADERS).text
        taken_back = client.post(
            f"/student/actions/assignments/{ESSAY}/undo-report",
            data={"report_id": hidden(card_for(page, ESSAY), "report_id"), "week": WEEK},
        )
        family = client.get("/parent", headers=PAGE_HEADERS).text
        hers = client.get(PAGE, headers=PAGE_HEADERS).text

    assert taken_back.status_code == 303
    section = family[family.index("<h2>Assignment updates</h2>") :]
    section = section[: section.index("<h2>Waiting for your review</h2>")]
    assert "<summary>Recent updates (1)</summary>" in section
    assert ESSAY_TITLE in section
    assert "She took back her update on August 19; no update stands." in section
    assert "She reported it" not in section
    assert OFFERED in card_for(hers, ESSAY)


def test_the_familys_planning_routes_refuse_a_run_that_found_nothing_left_to_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Everything is reported done between the family route's question and the run's
    reading. The JSON route answers 409 with the one sentence rather than 201 for a plan
    it did not make; the form comes back to the page with the sentence rather than
    redirecting as if a plan were made; no model is asked; and a run that reached a model
    and ended without a plan is still answered with its record, as before."""
    planners: list[Scripted[DailyPlan]] = []
    plans: list[DailyPlan] = [fixture_week_plan()]

    def override(
        state: Annotated[ApplicationState, Depends(get_application_state)],
    ) -> PlanGraphs:
        planner = Scripted(*[ok(plan) for plan in plans])
        planners.append(planner)
        return PlanGraphs(
            build=lambda: plan_graph_for(state, planner=planner, critic=Scripted(ok(accepting()))),
            may_start=True,
        )

    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = override  # type: ignore[attr-defined]
        state = state_of(client)
        finish_everything(client)
        monkeypatch.setattr(parent_routes, "require_work", lambda *_: None)
        over_json = client.post("/parent/plans", json={"plan_date": PLAN_DATE.isoformat()})
        from_the_form = client.post(
            "/parent/actions/plan", data={"plan_date": PLAN_DATE.isoformat()}
        )
        ended = state.drafts.runs_without_a_draft()
        asked = sum(planner.calls for planner in planners)
        monkeypatch.undo()
        report(client, ESSAY, "not_yet")
        plans[:] = [
            DailyPlan(plan_date=PLAN_DATE, blocks=[], deferred=[]),
            DailyPlan(plan_date=PLAN_DATE, blocks=[], deferred=[]),
            DailyPlan(plan_date=PLAN_DATE, blocks=[], deferred=[]),
        ]
        checks_failed = client.post("/parent/plans", json={"plan_date": PLAN_DATE.isoformat()})

    assert over_json.status_code == 409
    assert over_json.json()["detail"] == NOTHING_TO_SCHEDULE
    assert from_the_form.status_code == 409
    assert NOTHING_TO_SCHEDULE in from_the_form.text
    assert [run.outcome for run in ended] == ["nothing_to_schedule", "nothing_to_schedule"]
    assert asked == 0
    assert checks_failed.status_code == 201
    assert checks_failed.json()["draft_id"] is None
    assert checks_failed.json()["outcome"] == "checks_failed"


def test_two_assignments_with_one_title_are_told_apart_in_the_notice_by_their_ids() -> None:
    """Two posters, one for Art and one for Music, both in the plan and both reported done:
    the notice on each page names each with its id beside the title."""
    with browser(key=True) as client:
        for course in ("Art", "Music"):
            entered = client.post(
                "/parent/inbox/keep",
                data={"course": course, "title": "Poster", "due_date": "2026-09-10"},
            )
            assert entered.status_code == 303
        state = state_of(client)
        posters = sorted(
            item.assignment_id
            for item in state.project_state.all_assignments()
            if item.title == "Poster"
        )
        assert client.post("/student/actions/plan").status_code == 303
        state.drafts._connection.execute(
            "UPDATE drafts SET plan_assignment_ids=?", (json.dumps(posters),)
        )
        state.drafts._connection.commit()
        for poster in posters:
            report(client, poster, "done", week="2026-09-07")
        hers = client.get(PAGE, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text
        over_json = client.get("/student/plans/today").json()

    assert len(posters) == 2
    named = ", ".join(f"Poster ({poster})" for poster in posters)
    assert f"In it: {named}." in hers
    assert f"In it: {named}." in family
    assert over_json["reported_done_work"] == [
        {"assignment_id": poster, "title": "Poster"} for poster in posters
    ]


UNSAVED_HEADING = '<h3 class="update-heading">Your unsaved update</h3>'
REPORT = "/student/actions/assignments/{}/report"


@pytest.mark.parametrize("how", ["deleted", "refused by the store"])
def test_a_save_from_the_week_for_homework_no_longer_on_record_keeps_her_choice_and_words(
    monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    with browser() as client:
        page = client.get(PAGE, headers=PAGE_HEADERS).text
        fields = whole_form(page, REPORT.format(ESSAY))
        store = state_of(client).project_state
        if how == "deleted":
            store._connection.execute("DELETE FROM assignments WHERE assignment_id=?", (ESSAY,))
            store._connection.commit()
        else:

            def gone(*_: object, **__: object) -> None:
                raise UnknownAssignment(ESSAY)

            monkeypatch.setattr(store, "report_status", gone)
        answer = client.post(
            REPORT.format(ESSAY),
            data={**fields, "status": "done", "note": "Typed before it went."},
            headers=PAGE_HEADERS,
        )
        monkeypatch.undo()
        reports = store.student_reports(ESSAY)

    assert answer.status_code == 404
    assert GONE in answer.text
    assert "Your choice: Done. It was not saved." in answer.text
    assert "readonly>Typed before it went.</textarea>" in answer.text
    assert 'href="/student/due-this-week' in answer.text
    assert reports == []


@pytest.mark.parametrize("where", ["week", "details", "assigned later"])
def test_a_save_refused_for_a_newer_update_heads_her_words_as_unsaved(where: str) -> None:
    """The update saved elsewhere is shown, then her own choice and words, headed unsaved."""
    name = LOG if where == "assigned later" else ESSAY
    opened = f"/student/assignments/{name}" if where == "details" else PAGE
    with browser() as client:
        first = client.get(opened, headers=PAGE_HEADERS).text
        fields = whole_form(first, REPORT.format(name))
        report(client, name, "done")
        refused = client.post(
            REPORT.format(name),
            data={**fields, "status": "not_yet", "note": "still the last page"},
            headers=PAGE_HEADERS,
        )
        change = "1" if where == "details" else name
        changing = client.get(f"{opened}?change={change}", headers=PAGE_HEADERS).text
        too_long = client.post(
            REPORT.format(name),
            data={**whole_form(changing, REPORT.format(name)), "note": "x" * 501},
            headers=PAGE_HEADERS,
        )
    shown = refused.text if where == "details" else card_for(refused.text, name)

    assert refused.status_code == 409
    assert SAVED_ELSEWHERE in shown
    assert shown.count(UNSAVED_HEADING) == 1
    saved = shown.index('<span class="pill">Your update: Done</span>')
    assert saved < shown.index(UNSAVED_HEADING) < shown.index('value="not_yet" checked')
    assert ">still the last page</textarea>" in shown
    assert too_long.status_code == 422
    assert UNSAVED_HEADING not in too_long.text
    assert UNSAVED_HEADING not in changing


UNDO = "/student/actions/assignments/{}/undo-report"


@pytest.mark.parametrize("where", ["this week", "another week", "details from another week"])
def test_a_save_for_homework_gone_meanwhile_goes_back_to_the_week_the_form_was_on(
    where: str,
) -> None:
    later = date(2026, 9, 7)
    with browser() as client:
        entered = client.post(
            "/parent/inbox/keep",
            data={"course": "Art", "title": "Poster", "due_date": "2026-09-10"},
        )
        assert entered.status_code == 303
        poster = next(
            item.assignment_id
            for item in state_of(client).project_state.all_assignments()
            if item.title == "Poster"
        )
        name = ESSAY if where == "this week" else poster
        if where == "details from another week":
            opened = f"/student/assignments/{name}?return_to=week&week={later.isoformat()}"
            form = whole_form(client.get(opened, headers=PAGE_HEADERS).text, REPORT.format(name))
        else:
            params = {"week": later.isoformat()} if where == "another week" else {}
            page = client.get(PAGE, params=params, headers=PAGE_HEADERS).text
            form = whole_form(card_for(page, name), REPORT.format(name))
        week = later if where != "this week" else date.fromisoformat(form["week"])
        store = state_of(client).project_state
        store._connection.execute("DELETE FROM assignments WHERE assignment_id=?", (name,))
        store._connection.commit()
        answer = client.post(
            REPORT.format(name),
            data={**form, "status": "done", "note": "Typed before it went."},
            headers=PAGE_HEADERS,
        )

    kept = None if where == "details from another week" else form["in_place"]
    assert answer.status_code == 404
    assert GONE in answer.text
    assert f'href="{escape(week_href(week, name, show=name, in_place=kept))}"' in answer.text
    assert "Back to the week" in answer.text


@pytest.mark.parametrize("where", ["week", "details"])
@pytest.mark.parametrize("meanwhile", ["undone", "changed"])
def test_a_stale_undo_heads_nothing_as_her_unsaved_update(where: str, meanwhile: str) -> None:
    opened = f"/student/assignments/{ESSAY}" if where == "details" else PAGE
    with browser() as client:
        report(client, ESSAY, "done")
        undo = whole_form(client.get(opened, headers=PAGE_HEADERS).text, UNDO.format(ESSAY))
        if meanwhile == "undone":
            assert client.post(UNDO.format(ESSAY), data=undo).status_code == 303
        else:
            report(client, ESSAY, "not_yet")
        stale = client.post(UNDO.format(ESSAY), data=undo, headers=PAGE_HEADERS)
    shown = stale.text if where == "details" else card_for(stale.text, ESSAY)

    assert stale.status_code == 409
    assert (ALREADY_UNDONE if meanwhile == "undone" else CANNOT_UNDO) in stale.text
    assert UNSAVED_HEADING not in stale.text
    # With no update standing she is offered one; a card on her week left Not yet offers Done.
    assert (OFFERED in shown) is (meanwhile == "undone" or where == "week")


@pytest.mark.parametrize("where", ["week", "details"])
def test_a_save_refused_after_an_undo_elsewhere_heads_her_words_as_unsaved(where: str) -> None:
    opened = f"/student/assignments/{ESSAY}" if where == "details" else PAGE
    change = "1" if where == "details" else ESSAY
    with browser() as client:
        report(client, ESSAY, "done")
        changing = client.get(f"{opened}?change={change}", headers=PAGE_HEADERS).text
        fields = whole_form(changing, REPORT.format(ESSAY))
        undo = whole_form(client.get(opened, headers=PAGE_HEADERS).text, UNDO.format(ESSAY))
        assert client.post(UNDO.format(ESSAY), data=undo).status_code == 303
        refused = client.post(
            REPORT.format(ESSAY),
            data={**fields, "status": "not_yet", "note": "kept from the old page"},
            headers=PAGE_HEADERS,
        )
    shown = refused.text if where == "details" else card_for(refused.text, ESSAY)

    assert refused.status_code == 409
    assert SAVED_ELSEWHERE in shown
    assert shown.count(UNSAVED_HEADING) == 1
    assert shown.index(UNSAVED_HEADING) < shown.index('value="not_yet" checked')
    assert ">kept from the old page</textarea>" in shown
