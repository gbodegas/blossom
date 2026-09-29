"""Help on her week: one section below the homework, a link and a count in Today, closed
requests shown for seven days and kept for fourteen, and a place to land for every help link,
result and refusal. Synthetic words only. The household day stays pinned to the fixture's
Wednesday while the help store runs by a clock each test moves, since the store stamps and
ages requests by the real clock."""

import dataclasses
import html
import logging
import pathlib
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.dependencies import STATE_ATTRIBUTE
from blossom.routes import student as student_routes
from blossom.routes.navigation import note_action, note_help_href
from blossom.settings import REPOSITORY_ROOT
from blossom.stores import help_requests as help_store
from blossom.stores.help_requests import HelpRequest, HelpRequestsStore, RequestClosed
from tests.support import (
    HERS,
    PAGE_HEADERS,
    PLAN_DATE,
    THEIRS,
    SetClock,
    browser,
    client_for,
    lands_on,
    signed_in,
    signed_in_household,
    state_of,
    store_of,
    waiting_note,
    whole_form,
)

PAGE = "/student/due-this-week"
ASK = "/student/actions/ask-for-help"
TAKE_BACK = "/student/actions/take-back-help/"
HELP_JSON = "/student/help-requests"
FAMILY_JSON = "/parent/help-requests"
T0 = datetime(2026, 8, 19, 21, 0, tzinfo=UTC)
"""Five in the afternoon in New York on the fixture's Wednesday."""
MICRO = timedelta(microseconds=1)
SEVEN = timedelta(days=7)
FOURTEEN = timedelta(days=14)
NOBODY = "0" * 32
"""An id of the right shape that no form made."""

SECTION = '<section class="panel help-panel" id="help" tabindex="-1">'
RESULT = '<p class="note update-result" role="status" id="help-result" tabindex="-1">'
PROBLEM = '<p class="problem" role="alert" id="help-problem" tabindex="-1" autofocus>'
SENT = "Sent. Your parents can see the request."
ALREADY_SENT = "That request was already sent."
NOT_ON_THIS_PAGE = "That request is not on this page now."
CANNOT_CHECK = "That request can't be checked right now."
RESPONDING = (
    "A parent is already responding to this request, so it cannot be taken back. "
    "Nothing was changed."
)
CLOSED = "This request is already closed, so it cannot be taken back. Nothing was changed."
NOT_HERE = "That request is not here any more; nothing was changed."
NOT_HERS = "Sign in as the student to ask for help or take a request back."
ASK_AGAIN = '<a class="ask-again" href="#ask-for-help">Ask again</a>'
HER_LINK = '<a class="to-help" href="#ask-for-help">Ask for help</a>'
PARENT_LINK = '<a class="to-help" href="#help">Her help requests</a>'
FOLD_OPEN = '<details class="steps resolved" id="help-older" open>'

Reader = Literal["her", "parent", "open"]


# ------------------------------------------------------------------ helpers


@contextmanager
def household(tmp_path: pathlib.Path, reader: Reader, *, key: bool = False) -> Iterator[TestClient]:
    """Her week for one reader: signed in as her, signed in as a parent, or with the sign-in
    off. Requests are made through the store, so the reader never has to change."""
    client = browser(key=key) if reader == "open" else client_for(signed_in_household(tmp_path))
    with client:
        if reader != "open":
            signed_in(client, HERS if reader == "her" else THEIRS)
        yield client


def pinned_help(client: TestClient, at: datetime = T0) -> tuple[HelpRequestsStore, SetClock]:
    """The help store on the same file, run by a clock the test moves, in the store's place."""
    state = state_of(client)
    clock = SetClock(PLAN_DATE, at)
    store = HelpRequestsStore(state.help_requests._connection, clock)
    replaced = dataclasses.replace(state, help_requests=store)
    setattr(client.app.state, STATE_ATTRIBUTE, replaced)  # type: ignore[attr-defined]
    return store, clock


def asked(store: HelpRequestsStore, words: str | None, capture_id: str | None = None) -> str:
    return store.ask(PLAN_DATE, words, capture_id=capture_id).request_id


def closed(store: HelpRequestsStore, words: str, reply: str | None = None) -> str:
    request_id = asked(store, words)
    store.resolve(request_id, reply)
    return request_id


def element(page: str, tag: str, ident: str) -> str:
    """The element with this id, whole, from its opening tag to its own closing tag."""
    opening = re.search(rf'<{tag}\b[^>]*\sid="{re.escape(ident)}"[^>]*>', page)
    assert opening is not None, ident
    depth = 1
    for found in re.compile(rf"<(/?){tag}\b[^>]*>").finditer(page, opening.end()):
        depth += -1 if found.group(1) else 1
        if depth == 0:
            return page[opening.start() : found.end()]
    raise AssertionError(ident)


def section(page: str) -> str:
    return element(page, "section", "help")


def today(page: str) -> str:
    return element(page, "section", "today")


def row(page: str, request_id: str) -> str:
    return element(page, "li", f"help-{request_id}")


def updates(page: str) -> str:
    return element(page, "div", "help-updates") if 'id="help-updates"' in page else ""


def fold(page: str) -> str:
    return element(page, "details", "help-older") if 'id="help-older"' in page else ""


def words(fragment: str) -> str:
    """What a fragment says, its tags left out and its spaces as a reader meets them."""
    return " ".join(html.unescape(re.sub(r"<[^>]*>", "", fragment)).split())


def help_tables(client: TestClient) -> dict[str, list[tuple[object, ...]]]:
    """Every help table as the file holds it, and the file's list of tables."""
    connection = state_of(client).help_requests._connection
    statements = {
        "help_requests": "SELECT * FROM help_requests ORDER BY request_id",
        "help_request_ids": "SELECT * FROM help_request_ids ORDER BY request_id",
        "notes_named_by_requests": "SELECT * FROM notes_named_by_requests ORDER BY capture_id",
        "sqlite_master": "SELECT * FROM sqlite_master ORDER BY name",
    }
    return {
        name: [tuple(found) for found in connection.execute(sql)]
        for name, sql in statements.items()
    }


@contextmanager
def statements_on_help(client: TestClient) -> Iterator[list[str]]:
    """Every statement the help store's connection runs while the block runs."""
    seen: list[str] = []
    connection = state_of(client).help_requests._connection
    connection.set_trace_callback(seen.append)
    try:
        yield seen
    finally:
        connection.set_trace_callback(None)


class Refusing:
    """The help store's connection with every read of the requests refused, as a file that
    cannot be read refuses it, each try counted. Anything else goes through."""

    def __init__(self, connection: sqlite3.Connection, fault: type[Exception]) -> None:
        self.connection = connection
        self.fault = fault
        self.tries = 0

    def execute(self, statement: str, given: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if statement.lstrip().startswith("SELECT") and "FROM help_requests" in statement:
            self.tries += 1
            msg = "the file cannot be read"
            raise self.fault(msg)
        return self.connection.execute(statement, given)

    def __getattr__(self, name: str) -> object:
        return getattr(self.connection, name)


def outline_rule(css: str) -> list[str]:
    """Every selector of the rules that give a focused help target its outline."""
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    found: list[str] = []
    for head, inside in re.findall(r"([^{}]+)\{([^{}]*)\}", plain):
        if "outline: 2px solid var(--blue-action);" in inside:
            found.extend(part.strip() for part in head.split(","))
    return found


def wrapping(css: str) -> list[str]:
    """Every selector of the rules that break a long word rather than widen the page."""
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    found: list[str] = []
    for head, inside in re.findall(r"([^{}]+)\{([^{}]*)\}", plain):
        if "overflow-wrap: anywhere;" in inside:
            found.extend(part.strip() for part in head.split(","))
    return found


def in_sentence_rule(css: str) -> list[str]:
    """The selectors of the rule that gives a link inside a sentence 44 pixels to press."""
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for head, inside in re.findall(r"([^{}]+)\{([^{}]*)\}", plain):
        parts = [part.strip() for part in head.split(",")]
        if ".notice a" in parts and "padding: 0.8rem 0;" in inside:
            return parts
    msg = "no in-sentence link rule"
    raise AssertionError(msg)


CSS = (REPOSITORY_ROOT / "blossom" / "static" / "blossom.css").read_text(encoding="utf-8")


# ------------------------------------------------------------------ the one read and its groups


def test_the_one_read_holds_every_kept_request_and_the_instant_its_cutoff_used() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    clock = SetClock(PLAN_DATE, T0)
    store = HelpRequestsStore(connection, clock)
    gone = closed(store, "Synthetic gone")
    clock.at = T0 + timedelta(days=10)
    kept = closed(store, "Synthetic kept")
    waiting = asked(store, "Synthetic waiting")
    clock.at = T0 + FOURTEEN + MICRO
    seen: list[str] = []
    connection.set_trace_callback(seen.append)
    held = store.retained()
    connection.set_trace_callback(None)

    assert held.now == T0 + FOURTEEN + MICRO
    assert sorted(request.request_id for request in held.requests) == sorted([kept, waiting])
    assert gone not in {request.request_id for request in held.requests}
    assert len(seen) == 1
    assert help_store.HELP_RECENT_DAYS == 7


@pytest.mark.parametrize(
    ("after", "where"),
    [
        (SEVEN - MICRO, "recent"),
        (SEVEN, "earlier"),
        (SEVEN + MICRO, "earlier"),
        (FOURTEEN, "earlier"),
        (FOURTEEN + MICRO, "gone"),
    ],
    ids=["seven days less a microsecond", "seven days", "and a microsecond", "fourteen", "gone"],
)
def test_a_closure_is_recent_for_seven_days_then_older_until_it_goes(
    after: timedelta, where: str
) -> None:
    clock = SetClock(PLAN_DATE, T0)
    store = HelpRequestsStore(sqlite3.connect(":memory:", check_same_thread=False), clock)
    request_id = closed(store, "Synthetic boundary")
    clock.at = T0 + after
    groups = student_routes.help_groups(store.retained())

    placed = {
        "recent": [request.request_id for request in groups.recent],
        "earlier": [request.request_id for request in groups.earlier],
    }
    assert groups.open == []
    assert placed == {name: [request_id] if name == where else [] for name in placed}


def test_the_groups_keep_open_requests_oldest_first_and_closures_most_recent_first() -> None:
    clock = SetClock(PLAN_DATE, T0)
    store = HelpRequestsStore(sqlite3.connect(":memory:", check_same_thread=False), clock)
    made = []
    for minute in range(6):
        clock.at = T0 + timedelta(minutes=minute)
        made.append(asked(store, f"Synthetic {minute}"))
    for when, request_id in (
        (T0 + timedelta(hours=1), made[1]),
        (T0 + timedelta(hours=2), made[3]),
        (T0 + timedelta(days=8, hours=3), made[0]),
        (T0 + timedelta(days=8, hours=4), made[5]),
    ):
        clock.at = when
        store.resolve(request_id)
    store.accept(made[4])
    clock.at = T0 + timedelta(days=9)
    groups = student_routes.help_groups(store.retained())

    assert [request.request_id for request in groups.open] == [made[2], made[4]]
    assert [request.request_id for request in groups.recent] == [made[5], made[0]]
    assert [request.request_id for request in groups.earlier] == [made[3], made[1]]


def test_a_closure_at_the_same_instant_is_ordered_by_its_id() -> None:
    at = T0 + timedelta(hours=1)
    requests = [
        HelpRequest(
            request_id=name * 32,
            evening=PLAN_DATE,
            asked_at=T0,
            state="resolved",
            accepted_at=at,
            resolved_at=at,
        )
        for name in "ca"
    ]
    groups = student_routes.help_groups(help_store.HelpHeld(tuple(requests), at + SEVEN))

    assert [request.request_id for request in groups.earlier] == ["a" * 32, "c" * 32]


def test_the_sweep_keeps_a_closure_at_fourteen_days_and_takes_it_a_microsecond_later() -> None:
    clock = SetClock(PLAN_DATE, T0)
    store = HelpRequestsStore(sqlite3.connect(":memory:", check_same_thread=False), clock)
    closed(store, "Synthetic swept")
    clock.at = T0 + FOURTEEN
    kept = store.sweep()
    clock.at = T0 + FOURTEEN + MICRO

    assert kept == 0
    assert store.sweep() == 1


def test_taking_back_a_closure_past_retention_before_the_sweep_finds_nothing() -> None:
    clock = SetClock(PLAN_DATE, T0)
    store = HelpRequestsStore(sqlite3.connect(":memory:", check_same_thread=False), clock)
    request_id = closed(store, "Synthetic expired")
    clock.at = T0 + FOURTEEN

    with pytest.raises(RequestClosed):
        store.take_back(request_id)
    clock.at = T0 + FOURTEEN + MICRO
    assert store.take_back(request_id) is False


# ------------------------------------------------------------------ where help sits


def test_help_is_one_section_after_the_homework_and_before_the_privacy_fold() -> None:
    with browser() as client:
        store, clock = pinned_help(client)
        closed(store, "Synthetic earlier question", "Synthetic earlier reply")
        clock.at = T0 + timedelta(days=8)
        asked(store, "Synthetic open question")
        page = client.get(PAGE, params={"refreshed": "1"}).text
        elsewhere = client.get(PAGE, params={"week": "2026-08-24"}).text

    assert page.count(SECTION) == 1
    at = page.index(SECTION)
    assert page.index('<h2 class="list-heading">') < page.index('<section class="panel assigned">')
    assert page.index('<section class="panel assigned">') < at
    assert at < page.index("<summary>How Blossom uses your information</summary>")
    part = section(page)
    marks = [
        "<h2>Help</h2>",
        "Refreshed at",
        'id="ask-for-help"',
        'id="help-updates"',
        '<p class="note refresh">',
        'id="help-older"',
    ]
    assert [part.index(mark) for mark in marks] == sorted(part.index(mark) for mark in marks)
    assert "Refreshed at" not in today(page)
    assert 'id="help"' not in elsewhere
    assert 'id="ask-for-help"' not in elsewhere


@pytest.mark.parametrize("how_many", [1, 36])
def test_today_holds_a_link_and_a_count_and_no_words_of_any_request(how_many: int) -> None:
    with browser() as client:
        store, clock = pinned_help(client)
        made = [asked(store, f"Synthetic question {number}") for number in range(how_many)]
        if how_many == 36:
            for request_id in made[:12]:
                store.resolve(request_id, "Synthetic reply, earlier")
            clock.at = T0 + timedelta(days=8)
            for request_id in made[12:24]:
                store.resolve(request_id, "Synthetic reply, recent")
        page = client.get(PAGE).text

    shown = today(page)
    counted = 1 if how_many == 1 else 24
    line = (
        '<p class="support-links help-updates-link">'
        f'<a href="#help-updates">Help updates ({counted})</a></p>'
    )
    assert line in shown
    assert shown.index(line) > shown.index('<p class="support-links"><a href="/student/to-turn-in"')
    assert HER_LINK in shown
    assert "Synthetic" not in shown
    assert 'action="/student/actions/ask-for-help"' not in shown
    assert words(updates(page)).count("You asked for help") == counted
    if how_many == 36:
        assert "<summary>Older closed requests (12)</summary>" in fold(page)
        assert words(fold(page)).count("You asked for help") == 12


@pytest.mark.parametrize("reader", ["her", "parent", "open"])
@pytest.mark.parametrize("held", ["none", "only earlier"])
def test_with_nothing_open_or_recent_today_has_no_count_and_help_no_updates(
    reader: Reader, held: str, tmp_path: pathlib.Path
) -> None:
    with household(tmp_path, reader) as client:
        store, clock = pinned_help(client)
        if held == "only earlier":
            closed(store, "Synthetic earlier question")
            clock.at = T0 + SEVEN
        page = client.get(PAGE).text

    assert "help-updates-link" not in page
    assert 'id="help-updates"' not in page
    assert (fold(page) != "") is (held == "only earlier")
    if reader == "parent":
        note = element(page, "p", "ask-for-help")
        assert PARENT_LINK in today(page)
        assert HER_LINK not in page
        assert note.startswith('<p class="note" id="ask-for-help" tabindex="-1">')
        assert '<a href="/parent#help-she-asked-for">Family review</a>' in note
        assert words(note) == (
            "Her requests for help are below. You can answer open ones on Family review."
            if held == "only earlier"
            else "No requests for help to show. When she asks, you can answer on Family review."
        )
        assert lands_on(page, "#help") == SECTION
    else:
        assert HER_LINK in today(page)
        assert "Her help requests" not in page


# ------------------------------------------------------------------ seven and fourteen days


@pytest.mark.parametrize(
    ("after", "where"),
    [
        (SEVEN - MICRO, "recent"),
        (SEVEN, "earlier"),
        (SEVEN + MICRO, "earlier"),
        (FOURTEEN, "earlier"),
        (FOURTEEN + MICRO, "gone"),
    ],
    ids=["seven days less a microsecond", "seven days", "and a microsecond", "fourteen", "gone"],
)
def test_her_page_moves_a_closure_at_seven_days_and_drops_it_after_fourteen(
    after: timedelta, where: str
) -> None:
    with browser() as client:
        store, clock = pinned_help(client)
        request_id = closed(store, "Synthetic boundary question", "Synthetic boundary reply")
        clock.at = T0 + after
        page = client.get(PAGE).text

    assert "Today, Wednesday, August 19" in page
    if where == "gone":
        assert "Synthetic boundary" not in page
        assert 'id="help-older"' not in page
        assert "help-updates-link" not in page
        return
    shown = row(page, request_id)
    assert "They said: <q>Synthetic boundary reply</q>" in shown
    assert (f'id="help-{request_id}"' in updates(page)) is (where == "recent")
    assert (f'id="help-{request_id}"' in fold(page)) is (where == "earlier")
    assert ("Help updates (1)" in today(page)) is (where == "recent")
    assert (ASK_AGAIN in shown) is (where == "recent")
    if where == "earlier":
        assert '<details class="steps resolved" id="help-older">' in page
        assert words(fold(page)).endswith(
            "A closed request moves here seven days after it was closed and goes two weeks "
            "after it was closed."
        )


# ------------------------------------------------------------------ what a closed request says


@pytest.mark.parametrize("reply", [None, "Synthetic reply about the outline"])
def test_a_closed_request_says_a_parent_closed_it_on_its_own_day(reply: str | None) -> None:
    with browser() as client:
        store, clock = pinned_help(client, datetime(2026, 8, 21, 3, 30, tzinfo=UTC))
        request_id = closed(store, "Synthetic question", reply)
        open_id = asked(store, "Synthetic open question")
        clock.at += timedelta(hours=1)
        page = client.get(PAGE).text
        hers = client.get(HELP_JSON).json()
        theirs = client.get(FAMILY_JSON).json()

    shown = row(page, request_id)
    assert "<strong>A parent closed this request on Thursday, August 20.</strong>" in shown
    assert ("They said:" in shown) is (reply is not None)
    if reply is not None:
        assert "They said: <q>Synthetic reply about the outline</q>" in shown
    assert "Resolved" not in section(page)
    for listed in (hers, theirs):
        by_id = {item["request_id"]: item for item in listed}
        assert by_id[request_id]["resolved_local"] == "2026-08-20T23:30:00-04:00"
        assert by_id[open_id]["resolved_local"] is None


def test_a_closure_in_another_year_says_its_year() -> None:
    with browser() as client:
        store, clock = pinned_help(client, datetime(2025, 12, 31, 20, 0, tzinfo=UTC))
        request_id = closed(store, "Synthetic question from last year")
        clock.at = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
        page = client.get(PAGE).text

    assert "A parent closed this request on Wednesday, December 31, 2025." in row(page, request_id)


def test_several_requests_keep_their_own_rows_and_a_later_one_hides_no_earlier_reply() -> None:
    with browser() as client:
        store, clock = pinned_help(client)
        note = waiting_note(
            store_of(client), course="Geometry", title="Questions 4-8", text="Synthetic note words"
        )
        made = []
        for minute in range(5):
            clock.at = T0 + timedelta(minutes=minute)
            about = note if minute == 1 else None
            made.append(asked(store, f"Synthetic question {minute}", about))
        clock.at = T0 + timedelta(hours=1)
        store.resolve(made[3], "Synthetic reply three")
        clock.at = T0 + timedelta(hours=2)
        store.resolve(made[4])
        store.accept(made[2], "Synthetic reply two")
        clock.at = T0 + timedelta(hours=3)
        later = asked(store, "Synthetic later question")
        page = client.get(PAGE).text

    listed = re.findall(r'<li class="help-request" id="help-([0-9a-f]{32})" tabindex="-1">', page)
    assert listed == [made[0], made[1], made[2], later, made[4], made[3]]
    assert all(f'id="help-{request_id}"' in updates(page) for request_id in listed)
    assert "Help updates (6)" in today(page)
    assert "Synthetic note words" in row(page, made[1])
    assert "Synthetic note words" not in row(page, made[0])
    assert "Synthetic question 1" in row(page, made[1])
    assert "They said: <q>Synthetic reply three</q>" in row(page, made[3])
    assert "<strong>A parent is on it.</strong> They said: <q>Synthetic reply two</q>" in row(
        page, made[2]
    )
    assert "They said" not in row(page, made[4])


# ------------------------------------------------------------------ each reader


@pytest.mark.parametrize("reader", ["her", "parent", "open"])
def test_each_reader_reads_help_in_their_own_words_and_meets_only_their_controls(
    reader: Reader, tmp_path: pathlib.Path
) -> None:
    with household(tmp_path, reader) as client:
        store, clock = pinned_help(client)
        waiting = asked(store, "Synthetic waiting")
        taken = asked(store, "Synthetic taken")
        store.accept(taken, "Synthetic on it")
        old = closed(store, "Synthetic old", "Synthetic old reply")
        clock.at = T0 + timedelta(days=8)
        recent = closed(store, "Synthetic recent", "Synthetic recent reply")
        page = client.get(PAGE).text

    hers = reader != "parent"
    part = section(page)
    for request_id in (waiting, taken, old, recent):
        shown = words(row(page, request_id))
        assert shown.startswith("You asked for help" if hers else "She asked for help")
    for request_id in (taken, old, recent):
        assert TAKE_BACK not in row(page, request_id)
    for request_id in (waiting, taken, old):
        assert "Ask again" not in row(page, request_id)
    assert (f'action="{TAKE_BACK}{waiting}"' in row(page, waiting)) is hers
    assert (ASK_AGAIN in row(page, recent)) is hers
    assert (f'action="{ASK}"' in part) is hers
    assert (HER_LINK in today(page)) is hers
    assert (PARENT_LINK in today(page)) is not hers
    assert '<a href="#help-updates">Help updates (3)</a>' in today(page)
    assert lands_on(page, "#help-updates") == '<div id="help-updates" tabindex="-1">'
    refresh = re.search(r'<p class="note refresh">(.*?)</p>', part, re.S)
    assert refresh is not None
    assert words(refresh.group(1)) == (
        "Refresh replies. Refresh to see updates. Save or send your note first."
        if hers
        else "Refresh replies. Refresh to see updates."
    )
    assert '<a href="/student/due-this-week?refreshed=1#help">Refresh replies</a>' in part
    last = "and you can take it back." if hers else "and she can take it back."
    assert last in page
    if not hers:
        about_her = words(part).replace("You can answer", "")
        assert re.search(r"\byour?\b", about_her, re.IGNORECASE) is None
        assert '<a href="/parent#help-she-asked-for">Family review</a>' in part


# ------------------------------------------------------------------ where a sent form lands


@pytest.mark.parametrize("reader", ["her", "open"])
def test_a_fresh_ask_lands_on_its_own_row_saying_it_was_sent(
    reader: Reader, tmp_path: pathlib.Path
) -> None:
    with household(tmp_path, reader) as client:
        form = {**whole_form(client.get(PAGE).text, ASK), "note": "Synthetic fresh question"}
        sent = client.post(ASK, data=form)
        landed = client.get(sent.headers["location"]).text

    request_id = form["request_id"]
    assert sent.status_code == 303
    assert sent.headers["location"] == f"{PAGE}?asked={request_id}#help-result"
    assert landed.count('id="help-result"') == 1
    assert lands_on(landed, sent.headers["location"]) == RESULT
    assert f"{RESULT}{SENT}</p>" in row(landed, request_id)
    assert "autofocus" not in landed
    assert "Help updates (1)" in today(landed)
    assert whole_form(landed, ASK)["request_id"] != request_id


@pytest.mark.parametrize("then", ["accepted", "closed recently", "closed a week ago"])
def test_the_same_form_sent_again_lands_on_the_request_as_it_stands(then: str) -> None:
    with browser() as client:
        store, clock = pinned_help(client)
        earlier = closed(store, "Synthetic earlier question")
        clock.at = T0 + SEVEN
        form = {**whole_form(client.get(PAGE).text, ASK), "note": "Synthetic question"}
        assert client.post(ASK, data=form).status_code == 303
        request_id = form["request_id"]
        if then == "accepted":
            store.accept(request_id, "Synthetic reply")
        else:
            store.resolve(request_id, "Synthetic reply")
        if then == "closed a week ago":
            clock.at = T0 + FOURTEEN
        before = help_tables(client)
        again = client.post(ASK, data=form)
        landed = client.get(again.headers["location"]).text
        typed = client.get(PAGE, params={"asked": request_id}).text
        after = help_tables(client)

    assert again.status_code == 303
    assert again.headers["location"] == f"{PAGE}?asked_again={request_id}#help-result"
    for page in (landed, typed):
        shown = row(page, request_id)
        assert page.count('id="help-result"') == 1
        assert f"{RESULT}{ALREADY_SENT}</p>" in shown
        assert "Sent. Your parents" not in page
        if then == "accepted":
            assert "<strong>A parent is on it.</strong> They said: <q>Synthetic reply</q>" in shown
        else:
            assert "A parent closed this request on " in shown
            assert "They said: <q>Synthetic reply</q>" in shown
        assert f'id="help-{earlier}"' in fold(page)
        assert (FOLD_OPEN in page) is (then == "closed a week ago")
        assert (f'id="help-{request_id}"' in fold(page)) is (then == "closed a week ago")
    assert after == before


def not_on_this_page(page: str) -> None:
    """One result line, at the top of Help, saying the request is not here, and no other."""
    part = section(page)
    assert page.count('id="help-result"') == 1
    assert f"{RESULT}{NOT_ON_THIS_PAGE}</p>" in part
    assert part.index('id="help-result"') < part.index('id="ask-for-help"')
    assert "Sent. Your parents" not in page
    assert ALREADY_SENT not in page


@pytest.mark.parametrize("gone", ["taken back", "expired"])
def test_a_form_whose_request_is_gone_sends_nothing_and_its_markers_find_no_row(
    gone: str,
) -> None:
    with browser() as client:
        store, clock = pinned_help(client)
        form = {**whole_form(client.get(PAGE).text, ASK), "note": "Synthetic question"}
        client.post(ASK, data=form)
        request_id = form["request_id"]
        if gone == "taken back":
            assert client.post(f"{TAKE_BACK}{request_id}").status_code == 303
        else:
            store.resolve(request_id, "Synthetic reply")
            clock.at = T0 + FOURTEEN + MICRO
        before = help_tables(client)
        replayed = client.post(ASK, data=form)
        pages = [
            client.get(PAGE, params={kind: request_id}).text for kind in ("asked", "asked_again")
        ]
        after = help_tables(client)

    assert replayed.status_code == 409
    assert "This form was already used. Open a new help form to ask again." in words(replayed.text)
    assert after == before
    for page in pages:
        not_on_this_page(page)


@pytest.mark.parametrize("kind", ["asked", "asked_again"])
@pytest.mark.parametrize(
    "value",
    [NOBODY, "not-an-id", "", "F" * 32, "a" * 33],
    ids=["unknown", "words", "blank", "capitals", "long"],
)
def test_a_marker_that_names_no_request_says_so_at_the_top_of_help(kind: str, value: str) -> None:
    with browser() as client:
        store, _ = pinned_help(client)
        asked(store, "Synthetic open question")
        page = client.get(PAGE, params={kind: value}).text

    not_on_this_page(page)


def test_asked_is_read_before_asked_again() -> None:
    with browser() as client:
        store, _ = pinned_help(client)
        first = asked(store, "Synthetic first")
        second = asked(store, "Synthetic second")
        page = client.get(PAGE, params={"asked_again": first, "asked": second}).text

    assert page.count('id="help-result"') == 1
    assert f"{RESULT}{SENT}</p>" in row(page, second)


@pytest.mark.parametrize("kind", ["asked", "asked_again"])
def test_a_parent_reading_a_marker_reads_already_sent_and_meets_no_control(
    kind: str, tmp_path: pathlib.Path
) -> None:
    with household(tmp_path, "parent") as client:
        store, _ = pinned_help(client)
        request_id = asked(store, "Synthetic question")
        before = help_tables(client)
        page = client.get(PAGE, params={kind: request_id}).text
        after = help_tables(client)

    assert page.count('id="help-result"') == 1
    assert f"{RESULT}{ALREADY_SENT}</p>" in row(page, request_id)
    assert "Sent. Your parents" not in page
    assert 'action="/student/actions/' not in section(page)
    assert "Ask again" not in page
    assert after == before


def test_a_marker_typed_by_hand_writes_nothing_and_opens_no_control() -> None:
    with browser() as client:
        store, _ = pinned_help(client)
        mine = asked(store, "Synthetic open question")
        taken = asked(store, "Synthetic taken question")
        store.accept(taken)
        before = help_tables(client)
        typed = client.get(PAGE, params={"asked": mine}).text
        typed_taken = client.get(PAGE, params={"asked": taken}).text
        read = help_tables(client)
        withdrawn = client.post(f"{TAKE_BACK}{mine}", params={"asked": taken})
        after = help_tables(client)

    assert read == before
    assert f"{RESULT}{SENT}</p>" in row(typed, mine)
    assert f"{RESULT}{ALREADY_SENT}</p>" in row(typed_taken, taken)
    assert "Sent. Your parents" not in typed_taken
    assert whole_form(typed, ASK)["request_id"] not in (mine, taken)
    assert re.findall(r'action="(/student/actions/take-back-help/[^"]*)"', typed) == [
        f"{TAKE_BACK}{mine}"
    ]
    assert withdrawn.status_code == 303
    assert withdrawn.headers["location"] == f"{PAGE}#help"
    assert [found[0] for found in after["help_requests"]] == [taken]
    assert after["help_request_ids"] == before["help_request_ids"]


# ------------------------------------------------------------------ a read that fails


def good_and_failed(
    client: TestClient, fault: str, monkeypatch: pytest.MonkeyPatch, **query: str
) -> tuple[str, str, int, int]:
    """Her week read well, then with the help read failing: both pages, how many reads of
    the requests the failing one tried, and how many reads of their notes it made."""
    state = state_of(client)
    good = client.get(PAGE).text
    connection = state.help_requests._connection
    refusing = Refusing(connection, sqlite3.OperationalError)
    if fault == "the file":
        monkeypatch.setattr(state.help_requests, "_connection", refusing)
    else:
        connection.execute("UPDATE help_requests SET evening = 'not a date'")
        connection.commit()
    looked: list[object] = []
    named = state.project_state.captures_named

    def counted(*args: object) -> object:
        looked.append(args)
        return named(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(state.project_state, "captures_named", counted)
    seen: list[str] = []
    connection.set_trace_callback(seen.append)
    failed = client.get(PAGE, params=query)
    connection.set_trace_callback(None)
    monkeypatch.undo()
    assert failed.status_code == 200
    tries = refusing.tries if fault == "the file" else len(seen)
    return good, failed.text, tries, len(looked)


@pytest.mark.parametrize("fault", ["the file", "a row"])
def test_a_help_read_that_fails_leaves_the_rest_of_her_week_as_it_was(
    fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser(key=True) as client:
        client.post("/student/actions/plan")
        waiting_note(store_of(client), course="Geometry", title="Questions 4-8", text="Synthetic")
        store = state_of(client).help_requests
        note = waiting_note(store_of(client), course="Art", title="Sketch", text="Synthetic about")
        asked(store, "Synthetic question", note)
        good, failed, tries, notes_read = good_and_failed(client, fault, monkeypatch)

    count = (
        '<p class="support-links help-updates-link">'
        '<a href="#help-updates">Help updates (1)</a></p>'
    )
    rest = {
        name: " ".join(page.replace(section(page), "").replace(count, "").split())
        for name, page in (("good", good), ("failed", failed))
    }
    assert count in good
    assert rest["good"] == rest["failed"]
    assert 'id="todays-plan"' in failed
    assert '<section class="panel homework-notes"' in failed
    part = section(failed)
    assert (
        "Your requests for help can't be read right now. Refresh replies to check them before "
        "you ask again."
    ) in words(part)
    assert "Synthetic question" not in failed
    assert 'id="help-updates"' not in failed
    assert "Help updates" not in failed
    assert re.fullmatch(r"[0-9a-f]{32}", whole_form(failed, ASK)["request_id"])
    assert HER_LINK in today(failed)
    assert tries == 1
    assert notes_read == 0
    assert "autofocus" not in failed


@pytest.mark.parametrize("fault", ["the file", "a row"])
@pytest.mark.parametrize("kind", ["asked", "asked_again"])
def test_a_marker_on_a_failed_read_can_not_be_checked_and_never_says_sent(
    fault: str, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser() as client:
        request_id = asked(state_of(client).help_requests, "Synthetic question")
        _, failed, tries, _ = good_and_failed(client, fault, monkeypatch, **{kind: request_id})

    part = section(failed)
    assert failed.count('id="help-result"') == 1
    assert f"{RESULT}{escape(CANNOT_CHECK)}</p>" in part
    assert part.index('id="help-result"') < part.index('id="ask-for-help"')
    assert "Sent." not in failed
    assert ALREADY_SENT not in failed
    assert tries == 1


@pytest.mark.parametrize("fault", ["the file", "a row"])
def test_a_parent_on_a_failed_read_keeps_the_link_and_reads_about_her(
    fault: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with household(tmp_path, "parent") as client:
        asked(state_of(client).help_requests, "Synthetic question")
        _, failed, _, _ = good_and_failed(client, fault, monkeypatch)

    part = section(failed)
    assert PARENT_LINK in today(failed)
    assert "Help updates" not in failed
    assert f'action="{ASK}"' not in failed
    assert '<a href="/parent#help-she-asked-for">Family review</a>' in part
    assert "Her requests for help can't be read right now. Refresh replies to try again." in words(
        part
    )
    assert "Your requests" not in part


def test_a_row_that_can_not_be_read_is_logged_once_without_its_words(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "Synthetic private words " * 30
    with browser() as client:
        connection = state_of(client).help_requests._connection
        asked(state_of(client).help_requests, "Synthetic question")
        connection.execute("UPDATE help_requests SET note = ?", (secret,))
        connection.commit()
        with caplog.at_level(logging.WARNING):
            page = client.get(PAGE)

    said = [record for record in caplog.records if record.name == "blossom.routes.student"]
    assert page.status_code == 200
    assert len(said) == 1
    assert said[0].exc_info is None
    assert "Synthetic private" not in said[0].getMessage()
    assert "Synthetic private" not in page.text


def test_any_other_failure_of_the_read_is_not_taken_for_an_unreadable_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        store = state_of(client).help_requests
        monkeypatch.setattr(store, "_connection", Refusing(store._connection, RuntimeError))
        with pytest.raises(RuntimeError, match="the file cannot be read"):
            client.get(PAGE)


# ------------------------------------------------------------------ a refused take-back


@pytest.mark.parametrize(
    ("case", "status", "said", "linked"),
    [
        ("accepted", 409, RESPONDING, True),
        ("closed from requested", 409, CLOSED, True),
        ("closed after accept", 409, CLOSED, True),
        ("closed a week ago", 409, CLOSED, True),
        ("missing", 404, NOT_HERE, False),
        ("expired before the sweep", 404, NOT_HERE, False),
    ],
)
def test_a_refused_take_back_says_why_in_help_and_takes_the_focus(
    case: str, status: int, said: str, linked: bool
) -> None:
    with browser() as client:
        store, clock = pinned_help(client)
        request_id = NOBODY if case == "missing" else asked(store, "Synthetic question")
        if case in ("accepted", "closed after accept"):
            store.accept(request_id, "Synthetic reply")
        if case.startswith(("closed", "expired")):
            store.resolve(request_id)
        if case == "closed a week ago":
            clock.at = T0 + SEVEN
        if case.startswith("expired"):
            clock.at = T0 + FOURTEEN + MICRO
        before = help_tables(client)
        page = client.post(f"{TAKE_BACK}{request_id}")
        answer = client.delete(f"{HELP_JSON}/{request_id}")
        after = help_tables(client)

    problem = element(page.text, "p", "help-problem")
    assert page.status_code == status
    assert problem.startswith(PROBLEM)
    assert words(problem) == (f"{said} Go to the request." if linked else said)
    assert page.text.count("autofocus") == 1
    assert request_id not in words(page.text)
    assert '<p class="problem" role="alert">' not in page.text
    assert problem in section(page.text)
    assert answer.status_code == status
    if status == 409:
        state = "accepted" if case == "accepted" else "resolved"
        assert answer.json() == {
            "detail": f"request {request_id!r} is {state}, so it cannot be taken back"
        }
    else:
        assert answer.json() == {"detail": f"no help request {request_id!r}"}
    if linked:
        assert f'<a href="#help-{request_id}">Go to the request.</a>' in problem
        assert lands_on(page.text, f"#help-{request_id}").startswith('<li class="help-request"')
    assert (FOLD_OPEN in page.text) is (case == "closed a week ago")
    assert after == before


def test_a_refused_take_back_offers_no_way_to_a_row_the_page_could_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        store = state_of(client).help_requests
        request_id = asked(store, "Synthetic question")
        store.accept(request_id)
        taken = store.get(request_id)
        assert taken is not None

        def refuses(_: str) -> bool:
            raise RequestClosed(taken, "taken back")

        monkeypatch.setattr(store, "take_back", refuses)
        refusing = Refusing(store._connection, sqlite3.OperationalError)
        monkeypatch.setattr(store, "_connection", refusing)
        page = client.post(f"{TAKE_BACK}{request_id}")

    problem = element(page.text, "p", "help-problem")
    assert page.status_code == 409
    assert words(problem) == RESPONDING
    assert "Go to the request." not in page.text
    assert page.text.count("autofocus") == 1
    assert refusing.tries == 1


@pytest.mark.parametrize(
    ("state", "wanted", "said"),
    [("accepted", "resolved again", RESPONDING), ("resolved", "accepted", CLOSED)],
)
def test_a_refused_take_back_is_worded_by_the_state_found_and_not_by_the_message(
    state: str, wanted: str, said: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser() as client:
        store = state_of(client).help_requests
        request_id = asked(store, "Synthetic question")
        (store.accept if state == "accepted" else store.resolve)(request_id)
        found = store.get(request_id)
        assert found is not None

        def refuses(_: str) -> bool:
            raise RequestClosed(found, wanted)

        monkeypatch.setattr(store, "take_back", refuses)
        page = client.post(f"{TAKE_BACK}{request_id}")

    assert words(element(page.text, "p", "help-problem")) == f"{said} Go to the request."


@pytest.mark.parametrize("press", ["ask", "take back"])
def test_a_parents_press_from_a_page_left_open_is_refused_in_help_with_the_focus(
    press: str, tmp_path: pathlib.Path
) -> None:
    with household(tmp_path, "her") as client:
        store, _ = pinned_help(client)
        request_id = asked(store, "Synthetic question")
        left_open = client.get(PAGE).text
        client.post("/sign-out")
        signed_in(client, THEIRS)
        before = help_tables(client)
        if press == "ask":
            answer = client.post(ASK, data={**whole_form(left_open, ASK), "note": "Synthetic"})
        else:
            answer = client.post(f"{TAKE_BACK}{request_id}")
        after = help_tables(client)

    problem = element(answer.text, "p", "help-problem")
    assert answer.status_code == 403
    assert problem == f"{PROBLEM}{NOT_HERS}</p>"
    assert problem in section(answer.text)
    assert answer.text.count(NOT_HERS) == 1
    assert answer.text.count("autofocus") == 1
    assert PARENT_LINK in today(answer.text)
    assert f'action="{ASK}"' not in answer.text
    assert after == before


# ------------------------------------------------------------------ where each help link lands


def lands_on_a_focus_target(page: str, address: str) -> str:
    """The element an address lands on, which must take the focus and be outlined when it
    does; its opening tag."""
    target = lands_on(page, address)
    assert target, address
    assert 'tabindex="-1"' in target, target
    targets = outline_rule(CSS)
    if target.startswith("<section"):
        assert ".help-panel:focus" in targets or "#help-she-asked-for:focus" in targets
    elif target.startswith("<form"):
        assert 'class="ask"' in target
        assert ".ask:focus" in targets
    else:
        assert '.help-panel [tabindex="-1"]:focus' in targets
    return target


def test_every_help_link_and_redirect_lands_on_a_target_that_takes_the_focus(
    tmp_path: pathlib.Path,
) -> None:
    with browser() as client:
        store, clock = pinned_help(client)
        waiting = asked(store, "Synthetic waiting")
        taken = asked(store, "Synthetic taken")
        store.accept(taken)
        closed(store, "Synthetic earlier")
        clock.at = T0 + SEVEN
        recent = closed(store, "Synthetic recent")
        page = client.get(PAGE).text
        form = {**whole_form(page, ASK), "note": "Synthetic fresh"}
        fresh = client.post(ASK, data=form).headers["location"]
        again = client.post(ASK, data=form).headers["location"]
        taken_back = client.post(f"{TAKE_BACK}{waiting}").headers["location"]
        refused = client.post(f"{TAKE_BACK}{taken}").text
        note = waiting_note(store_of(client), course="Art", title="Sketch", text="Synthetic note")
        note_form = whole_form(
            client.get(note_help_href(note)).text, note_action(note, "ask-for-help")
        )
        client.post(note_action(note, "ask-for-help"), data=note_form, headers=PAGE_HEADERS)
        note_again = client.post(
            note_action(note, "ask-for-help"), data=note_form, headers=PAGE_HEADERS
        )
        landings = {
            where: client.get(where).text
            for where in (fresh, again, taken_back, note_again.headers["location"])
        }

    refresh = re.search(r'<a href="([^"]+)">Refresh replies</a>', page)
    assert refresh is not None
    assert refresh.group(1) == f"{PAGE}?refreshed=1#help"
    for href in ("#ask-for-help", "#help-updates"):
        assert f'href="{href}"' in today(page)
    assert lands_on_a_focus_target(page, "#ask-for-help").startswith('<form method="post"')
    assert lands_on_a_focus_target(page, "#help-updates").startswith('<div id="help-updates"')
    assert ASK_AGAIN in row(page, recent)
    assert lands_on_a_focus_target(page, refresh.group(1)) == SECTION
    assert f'<a href="#help-{taken}">Go to the request.</a>' in refused
    assert lands_on_a_focus_target(refused, f"#help-{taken}").startswith('<li class="help-request"')
    assert taken_back == f"{PAGE}#help"
    assert lands_on_a_focus_target(landings[taken_back], taken_back) == SECTION
    for where in (fresh, again, note_again.headers["location"]):
        assert where.endswith("#help-result")
        assert lands_on_a_focus_target(landings[where], where) == RESULT
    assert ".update-result:focus" in outline_rule(CSS)

    with household(tmp_path, "parent") as client:
        asked(state_of(client).help_requests, "Synthetic question")
        theirs = client.get(PAGE).text
        family = client.get("/parent").text

    assert lands_on_a_focus_target(theirs, "#help") == SECTION
    assert lands_on_a_focus_target(theirs, "#ask-for-help").startswith('<p class="note"')
    assert lands_on_a_focus_target(family, "/parent#help-she-asked-for") == (
        '<section id="help-she-asked-for" tabindex="-1">'
    )


def test_the_help_section_is_outlined_on_focus_its_links_are_tall_and_its_words_wrap() -> None:
    targets = outline_rule(CSS)
    tall = in_sentence_rule(CSS)

    for selector in (
        ".help-panel:focus",
        '.help-panel [tabindex="-1"]:focus',
        "#help-she-asked-for:focus",
    ):
        assert selector in targets
    assert ".help-panel .help-request a.ask-again" in tall
    assert ".help-panel .problem a" in tall
    assert ".help-panel" in wrapping(CSS)


# ------------------------------------------------------------------ one read, no writes


@pytest.mark.parametrize("how_many", [1, 36])
@pytest.mark.parametrize("marker", [False, True], ids=["no marker", "a marker"])
def test_her_week_reads_help_in_one_statement_and_writes_nothing(
    how_many: int, marker: bool
) -> None:
    with browser() as client:
        store, clock = pinned_help(client)
        made = [asked(store, f"Synthetic {number}") for number in range(how_many)]
        for request_id in made[: how_many // 3]:
            store.resolve(request_id, "Synthetic reply")
        clock.at = T0 + timedelta(days=8)
        for request_id in made[how_many // 3 : 2 * how_many // 3]:
            store.resolve(request_id)
        before = help_tables(client)
        with statements_on_help(client) as seen:
            page = client.get(PAGE, params={"asked_again": made[0]} if marker else None)
        client.get(PAGE)
        after = help_tables(client)

    assert page.status_code == 200
    assert len(seen) == 1
    assert "help_requests" in seen[0]
    assert after == before
