"""Her To turn in list: everything she reports as still to turn in, found from her main page.

Outside the fold of finished work and outside any week, however old, in the
order she took each on. One press says a thing was turned in, with the head
the row showed, and can be undone from where she pressed. The fixture week
through the app, a pinned clock, and forms read from the page's own HTML.
"""

import html
import pathlib
import re
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.app import create_app
from blossom.hand_in import NEEDS_HAND_IN, TURNED_IN, HandInSaved, HandInState
from blossom.reconciliation import SourceChannel
from blossom.routes.hand_in import (
    GONE_FROM_THE_LIST,
    HAND_IN_CANNOT_UNDO,
    HAND_IN_NOT_UNDONE,
    LIST_ALREADY_UNDONE,
    LIST_BAD_FORM,
    LIST_CHANGED,
    LIST_NOT_A_HAND_IN_OF_THIS,
    LIST_NOT_SAVED,
    LIST_TURN_IN_FAILED,
    LIST_UNDO_FAILED,
)
from blossom.routes.navigation import (
    TO_TURN_IN_PAGE,
    assignment_anchor,
    details_href,
    result_anchor,
)
from blossom.routes.runs import NOTHING_TO_SCHEDULE
from blossom.routes.student import (
    BAD_RETURN,
    HAND_IN_ALREADY_SAVED,
    HAND_IN_UNDONE,
    NOT_HERS_TO_UPDATE,
    hand_in_actions,
    report_actions,
)
from blossom.stores.project_state import ProjectStateStore
from blossom.to_turn_in import (
    RESULT_UNREADABLE,
    SAME_EARLIER,
    TURNED_IN_EARLIER,
    TURNED_IN_FROM_THE_LIST,
    UNDONE_EARLIER,
)
from tests.support import (
    ESSAY_ID,
    ESSAY_TITLE,
    FIXTURE_WEEK,
    HER_PAGE,
    HERS,
    PAGE_HEADERS,
    SAME_ORIGIN,
    THEIRS,
    Answer,
    a_row,
    as_served,
    browser,
    card_for,
    fixture_clock,
    form_fields,
    report,
    school_said,
    signed_in_household,
    state_of,
    with_clock,
)

QUIZ_ID = "assignment-vocabulary-quiz"
ALGEBRA_ID = "assignment-algebra-set"
EMPTY = "Nothing is on your To turn in list."
PRESS = "I turned it in"
PLAIN = {"hand-in": LIST_TURN_IN_FAILED, "undo-hand-in": LIST_UNDO_FAILED}


def said(
    store: ProjectStateStore,
    name: str,
    state: HandInState,
    on: date,
    *,
    head: str | None = None,
    action: str | None = None,
    note: str | None = None,
) -> str:
    at = datetime(on.year, on.month, on.day, 21, 0, tzinfo=UTC)
    kept = store.record_hand_in(name, state, action, note, expected_head=head, now=at, today=on)
    assert isinstance(kept, HandInSaved), kept
    return kept.event.event_id


def listed(page: str) -> list[str]:
    """The assignments in the list on a page, in the order shown."""
    return re.findall(r'<li class="to-turn-in-row" id="to-turn-in-([^"]+)"', page)


def section(page: str) -> str:
    start = page.index('id="to-turn-in"')
    return page[start : page.index("</section>", start)]


def press(client: TestClient, page: str, assignment_id: str, **changed: str) -> Answer:
    """Press the row's button, sending the fields the page wrote for it. ``changed`` is
    what a request made up by hand sends in place of them."""
    action = f"/student/actions/assignments/{assignment_id}/hand-in"
    row = page[page.index(f'id="to-turn-in-{assignment_id}"') :]
    return client.post(action, data={**form_fields(row, action), **changed}, headers=PAGE_HEADERS)


def said_first(page: str, sentence: str) -> bool:
    """Whether a refusal is said once, in the list's own place for it, ahead of the rows,
    as the one thing on the page given the focus."""
    place = page.index('id="to-turn-in-problem"')
    rows = page.find('<ul class="to-turn-in-rows">')
    return (
        page.count(str(escape(sentence))) == 1
        and page.index(str(escape(sentence))) > place
        and (rows == -1 or place < rows)
        and page.count(" autofocus") == 1
        and "autofocus" in page[place : page.index(">", place)]
    )


def follow(client: TestClient, answer: Answer) -> str:
    assert answer.status_code == 303, answer.text
    return client.get(answer.headers["location"]).text


# ------------------------------------------------------------------ finding the list


def test_with_nothing_to_turn_in_the_link_is_there_and_the_list_says_so() -> None:
    with browser() as client:
        week = client.get(HER_PAGE).text
        page = client.get(TO_TURN_IN_PAGE)

    assert f'href="{TO_TURN_IN_PAGE}"' in week
    assert ">To turn in</a>" in week
    assert "To turn in (0)" not in week
    assert 'id="to-turn-in"' not in week
    assert page.status_code == 200
    assert EMPTY in page.text
    assert "submitted" not in page.text.lower()
    assert listed(page.text) == []


def test_the_list_holds_what_is_still_to_turn_in_whatever_the_work_or_the_week() -> None:
    """Done work, work due long after the week, and nothing else."""
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = state_of(client).project_state
        names = ["done-work", "far-off", "turned", "none-needed", "unsure", "silent"]
        rows = [
            a_row(name, f"Work {name}").model_copy(update={"due_date": date(2026, 8, 20)})
            for name in names
        ]
        rows[1] = rows[1].model_copy(update={"due_date": date(2027, 1, 15)})
        store.put_on_record(rows, {})
        store.report_status(
            "done-work",
            "done",
            None,
            expected_head=None,
            now=datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
            today=date(2026, 8, 19),
        )
        said(store, "done-work", NEEDS_HAND_IN, date(2026, 8, 18))
        said(store, "far-off", NEEDS_HAND_IN, date(2026, 8, 19))
        said(store, "turned", TURNED_IN, date(2026, 8, 19))
        said(store, "none-needed", "not_required", date(2026, 8, 19))
        said(store, "unsure", "unknown", date(2026, 8, 19))

        week = client.get(HER_PAGE).text
        page = client.get(TO_TURN_IN_PAGE).text

    assert listed(page) == ["done-work", "far-off"]
    assert listed(section(week)) == ["done-work", "far-off"]
    assert "To turn in (2)" in week
    assert week.index('id="to-turn-in"') < week.index("Reported done (1)")


def test_with_every_assignment_done_her_week_still_leads_to_what_is_left_to_turn_in() -> None:
    """No plan can be asked for and none is offered; the list is another matter, and the
    Today panel's link and the section are both there, outside the fold of finished work."""
    with browser(key=True) as client:
        store = state_of(client).project_state
        for item in store.all_assignments():
            report(client, item.assignment_id, "done")
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))

        week = client.get(HER_PAGE).text

    today = week[week.index('id="today"') : week.index('id="to-turn-in"')]
    assert NOTHING_TO_SCHEDULE in today
    assert 'action="/student/actions/plan"' not in week
    assert f'<a href="{TO_TURN_IN_PAGE}">To turn in (1)</a>' in today
    assert listed(section(week)) == [ESSAY_ID]
    assert PRESS in section(week)
    assert week.index('id="to-turn-in"') < week.index("Reported done (")
    assert "<details" not in section(week)


def test_the_order_is_the_order_she_took_each_on_and_an_edit_does_not_move_a_row() -> None:
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = state_of(client).project_state
        names = ["first", "second", "third", "fourth"]
        store.put_on_record([a_row(name, f"Work {name}") for name in names], {})
        one = said(store, "first", NEEDS_HAND_IN, date(2026, 7, 1))
        two = said(store, "second", NEEDS_HAND_IN, date(2026, 8, 1))
        said(store, "third", NEEDS_HAND_IN, date(2026, 8, 10))
        said(store, "fourth", NEEDS_HAND_IN, date(2026, 8, 11))
        said(store, "first", NEEDS_HAND_IN, date(2026, 8, 19), head=one, action="In my folder")
        left = said(store, "second", TURNED_IN, date(2026, 8, 19), head=two)
        store.undo_hand_in(
            "second", left, now=datetime(2026, 8, 19, 22, 0, tzinfo=UTC), today=date(2026, 8, 19)
        )
        third = store.hand_in_chains(["third"])["third"][-1].event_id
        back = said(store, "third", TURNED_IN, date(2026, 8, 19), head=third)
        said(store, "third", NEEDS_HAND_IN, date(2026, 8, 19), head=back)

        page = client.get(TO_TURN_IN_PAGE).text
        week = client.get(HER_PAGE).text

    assert listed(page) == ["first", "second", "fourth", "third"]
    assert "You reported Still to turn in on July 1, 2026." in page
    assert listed(section(week)) == ["first", "second", "fourth"]
    assert "View all 4 to turn in" in section(week)
    assert f'href="{TO_TURN_IN_PAGE}"' in section(week)


def test_a_row_says_what_it_is_the_next_step_her_report_and_what_the_school_says() -> None:
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19), action="In my folder")
        said(store, QUIZ_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        store.record_status_reports(
            ESSAY_ID, [school_said("missing", SourceChannel.EMAIL, date(2026, 8, 19))]
        )

        page = client.get(TO_TURN_IN_PAGE).text

    essay = page[
        page.index(f'id="to-turn-in-{ESSAY_ID}"') : page.index(f'id="to-turn-in-{QUIZ_ID}"')
    ]
    assert ESSAY_TITLE in essay
    assert "World History" in essay
    assert "In my folder" in essay
    assert "You reported Still to turn in on August 19, 2026." in essay
    assert "The school reports this missing" in essay
    assert "return_to=to_turn_in" in essay
    assert "Turn it in." in page[page.index(f'id="to-turn-in-{QUIZ_ID}"') :]
    assert PRESS in essay


# --------------------------------------------------------------------- one press


def test_one_press_says_it_was_turned_in_and_the_result_offers_undo_where_she_pressed() -> None:
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19), action="In my folder", note="mine")
        said(store, QUIZ_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        page = client.get(TO_TURN_IN_PAGE).text

        answer = press(client, page, ESSAY_ID)

        assert answer.status_code == 303
        where = answer.headers["location"]
        assert where.startswith(TO_TURN_IN_PAGE)
        assert where.endswith("#to-turn-in-result")
        after = client.get(where).text
        head = store.hand_in_chains([ESSAY_ID])[ESSAY_ID][-1]
        assert (head.state, head.next_action, head.note) == (TURNED_IN, None, None)
        assert escape(TURNED_IN_FROM_THE_LIST) in after
        assert ESSAY_TITLE in after[after.index('id="to-turn-in-result"') :]
        assert listed(after) == [QUIZ_ID]

        undo_action = f"/student/actions/assignments/{ESSAY_ID}/undo-hand-in"
        undone = follow(
            client,
            client.post(undo_action, data=form_fields(after, undo_action), headers=PAGE_HEADERS),
        )
        restored = store.hand_in_chains([ESSAY_ID])[ESSAY_ID][-1]

    assert escape(HAND_IN_UNDONE) in undone
    assert listed(undone) == [ESSAY_ID, QUIZ_ID]
    assert "In my folder" in undone
    assert (restored.state, restored.note) == (NEEDS_HAND_IN, "mine")


def test_a_press_on_her_week_comes_back_to_her_week_even_when_the_list_empties() -> None:
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        week = client.get(HER_PAGE).text

        answer = press(client, section(week), ESSAY_ID)

        where = answer.headers["location"]
        assert where.startswith(HER_PAGE)
        assert where.endswith("#to-turn-in-result")
        after = client.get(where).text

    assert 'id="to-turn-in-result"' in after
    assert escape(TURNED_IN_FROM_THE_LIST) in after
    assert listed(after) == []
    assert "undo-hand-in" in section(after)


def test_a_second_press_is_already_saved_and_a_row_that_moved_on_is_refused() -> None:
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        page = client.get(TO_TURN_IN_PAGE).text
        follow(client, press(client, page, ESSAY_ID))

        again = press(client, page, ESSAY_ID)

        assert escape(HAND_IN_ALREADY_SAVED) in follow(client, again)
        assert len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID]) == 2

        said(store, QUIZ_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        behind = client.get(TO_TURN_IN_PAGE).text
        head = store.hand_in_chains([QUIZ_ID])[QUIZ_ID][-1].event_id
        said(store, QUIZ_ID, NEEDS_HAND_IN, date(2026, 8, 19), head=head, action="Hand it to her")
        refused = press(client, behind, QUIZ_ID)
        kept = len(store.hand_in_chains([QUIZ_ID])[QUIZ_ID])

    assert refused.status_code == 409
    assert said_first(refused.text, LIST_CHANGED)
    assert "Hand it to her" in refused.text
    assert listed(refused.text) == [QUIZ_ID]
    assert kept == 2


@pytest.mark.parametrize("origin", ["list", "week"])
def test_a_press_the_list_refuses_is_said_first_where_she_pressed_and_writes_nothing(
    origin: str,
) -> None:
    """Each refusal a press can get, in the list's own words, on the page it was made from."""
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19), note="mine")
        page = client.get(where).text
        action = f"/student/actions/assignments/{ESSAY_ID}/hand-in"

        foreign = press(client, page, ESSAY_ID, expected_hand_in_id="hand-in-not-this-one")
        extra = press(client, page, ESSAY_ID, role="parent")
        twice = client.post(
            action,
            content=f"state=turned_in&state=unknown&next_action=&note=&hand_in_view={origin}",
            headers={**PAGE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
        gone = client.post(
            "/student/actions/assignments/assignment-not-there/hand-in",
            data={**form_fields(page[page.index(f'id="to-turn-in-{ESSAY_ID}"') :], action)},
            headers=PAGE_HEADERS,
        )
        kept = store.hand_in_chains()

    assert (foreign.status_code, extra.status_code, twice.status_code) == (422, 422, 422)
    assert said_first(foreign.text, LIST_NOT_A_HAND_IN_OF_THIS)
    assert said_first(extra.text, LIST_BAD_FORM)
    assert said_first(twice.text, LIST_BAD_FORM)
    assert gone.status_code == 404
    assert said_first(gone.text, GONE_FROM_THE_LIST)
    for answer in (foreign, extra, twice, gone):
        assert listed(answer.text) == [ESSAY_ID]
        assert ("<h1>To turn in" in answer.text) == (origin == "list")
        assert "Your words are still here" not in answer.text
    assert list(kept) == [ESSAY_ID]
    assert len(kept[ESSAY_ID]) == 1


def test_a_place_to_show_the_result_that_these_pages_do_not_make_is_refused() -> None:
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        page = client.get(TO_TURN_IN_PAGE).text

        forged = press(client, page, ESSAY_ID, hand_in_view="https://example.test/")
        kept = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])

    assert forged.status_code == 422
    assert escape(BAD_RETURN) in forged.text
    assert "example.test" not in forged.text
    assert kept == 1


@pytest.mark.parametrize("origin", ["list", "week"])
def test_a_press_over_a_record_that_cannot_be_read_says_so_with_no_word_of_a_save(
    origin: str,
) -> None:
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        page = client.get(where).text
        store._connection.execute("UPDATE hand_in_events SET previous_event_id = 'no-such-event'")
        store._connection.commit()

        answer = press(client, page, ESSAY_ID)
        rows = store._connection.execute("SELECT COUNT(*) FROM hand_in_events").fetchone()[0]

    assert answer.status_code == 500
    assert said_first(answer.text, LIST_NOT_SAVED)
    assert escape(TURNED_IN_FROM_THE_LIST) not in answer.text
    assert listed(answer.text) == []
    assert "cannot be read" in answer.text
    assert rows == 1


@pytest.mark.parametrize("origin", ["list", "week"])
def test_an_undo_pressed_twice_on_the_list_is_said_to_be_already_undone(origin: str) -> None:
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    undo = f"/student/actions/assignments/{ESSAY_ID}/undo-hand-in"
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        after = follow(client, press(client, client.get(where).text, ESSAY_ID))
        fields = form_fields(after, undo)
        follow(client, client.post(undo, data=fields, headers=PAGE_HEADERS))

        again = client.post(undo, data=fields, headers=PAGE_HEADERS)
        kept = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])

    assert fields["hand_in_view"] == origin
    assert again.status_code == 409
    assert said_first(again.text, LIST_ALREADY_UNDONE)
    assert listed(again.text) == [ESSAY_ID]
    assert kept == 3


def test_reading_the_list_writes_nothing() -> None:
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        before = store._connection.execute("SELECT * FROM hand_in_events").fetchall()
        for _ in range(2):
            client.get(TO_TURN_IN_PAGE)
            client.get(HER_PAGE)
            client.head(TO_TURN_IN_PAGE)
        after = store._connection.execute("SELECT * FROM hand_in_events").fetchall()

    assert after == before


def test_a_record_that_cannot_be_read_is_said_under_the_list_and_never_dropped() -> None:
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        said(store, QUIZ_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        store._connection.execute(
            "UPDATE hand_in_events SET state = 'invalid-state' WHERE assignment_id = ?", (QUIZ_ID,)
        )
        store._connection.commit()

        page = client.get(TO_TURN_IN_PAGE)
        week = client.get(HER_PAGE)

    assert (page.status_code, week.status_code) == (200, 200)
    for shown in (page.text, section(week.text)):
        assert listed(shown) == [ESSAY_ID]
        named = re.search(r'<p class="problem">Your hand-in record cannot be read.*?</p>', shown)
        assert named is not None
        assert "Vocabulary quiz" in named.group()
        assert f"/student/assignments/{QUIZ_ID}" in named.group()
        assert "It may belong on this list." in named.group()
    assert "To turn in (1)" in week.text


def test_a_parent_reads_the_list_and_cannot_press(tmp_path: pathlib.Path) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        hers = client.get(TO_TURN_IN_PAGE).text
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})

        theirs = client.get(TO_TURN_IN_PAGE).text
        refused = press(client, hers, ESSAY_ID)
        kept = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])

    assert PRESS in hers
    assert ESSAY_TITLE in theirs
    assert PRESS not in theirs
    assert "She reported Still to turn in on August 19, 2026." in theirs
    assert "Sign in as the student to update." in theirs
    assert refused.status_code == 403
    assert escape(NOT_HERS_TO_UPDATE) in refused.text
    assert kept == 1


# --------------------------------------------------- help remembering, and the way back


def test_help_remembering_is_offered_after_done_and_after_not_sure_and_writes_nothing() -> None:
    details = f"/student/assignments/{ESSAY_ID}"
    offer = "Help me remember to turn this in"
    with browser() as client:
        store = state_of(client).project_state
        where = report(client, ESSAY_ID, "done")
        card = card_for(client.get(where).text, ESSAY_ID)
        assert offer in card
        assert "hand_in=remember" in card

        opened = client.get(details, params={"hand_in": "remember"}).text
        assert 'value="needs_hand_in" checked' in opened
        assert store.hand_in_chains() == {}

        said(store, ESSAY_ID, "unknown", date(2026, 8, 19))
        unsure = client.get(details).text
        assert offer in unsure

        head = store.hand_in_chains([ESSAY_ID])[ESSAY_ID][-1].event_id
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19), head=head)
        waiting = client.get(details).text
        kept = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])

    assert offer not in waiting
    assert kept == 2


def test_details_opened_from_the_list_lead_back_to_it_and_a_forged_way_back_is_refused() -> None:
    details = f"/student/assignments/{ESSAY_ID}"
    with browser() as client:
        page = client.get(details, params={"return_to": "to_turn_in"}).text
        forged = client.get(details, params={"return_to": "to_turn_in", "week": FIXTURE_WEEK}).text
        fields = form_fields(
            client.get(details, params={"return_to": "to_turn_in", "hand_in": "change"}).text,
            f"/student/actions/assignments/{ESSAY_ID}/hand-in",
        )

    assert f'<a href="{TO_TURN_IN_PAGE}#to-turn-in">Back to To turn in</a>' in page
    assert "Back to To turn in" not in forged
    assert fields["return_to"] == "to_turn_in"


@pytest.mark.parametrize("where", [HER_PAGE, TO_TURN_IN_PAGE])
def test_a_long_list_costs_no_more_reads_than_a_short_one(
    where: str, tmp_path: pathlib.Path
) -> None:
    costs = []
    for count in (1, 20, 200):
        record = str(tmp_path / f"record-{count}.sqlite3")
        with browser(BLOSSOM_FIXTURE_PATH="", BLOSSOM_DATABASE_PATH=record) as client:
            store = state_of(client).project_state
            names = [f"math-{number:03d}" for number in range(count)]
            store.put_on_record([a_row(name, f"Set {name}") for name in names], {})
            for name in names:
                said(store, name, NEEDS_HAND_IN, date(2026, 8, 19))
            seen: list[str] = []
            store._connection.set_trace_callback(seen.append)
            shown = client.get(where)
            store._connection.set_trace_callback(None)
        assert shown.status_code == 200
        assert f"({count})" in shown.text
        costs.append(len(seen))

    assert costs == [8, 8, 8]


# ------------------------------------------- a result belongs to the event that made it


def result_of(page: str) -> str:
    """The place that says what a press did, through to the end of what it holds."""
    start = page.index('id="to-turn-in-result"')
    rows = page.find('<ul class="to-turn-in-rows">', start)
    return page[start : rows if rows != -1 else page.index("</section>", start)]


def undo_of(store: ProjectStateStore, name: str, event_id: str) -> None:
    store.undo_hand_in(
        name, event_id, now=datetime(2026, 8, 19, 22, 0, tzinfo=UTC), today=date(2026, 8, 19)
    )


@pytest.mark.parametrize("origin", ["list", "week"])
@pytest.mark.parametrize("between", ["a note", "back on the list", "an undo"])
def test_a_result_read_after_the_record_moved_on_says_what_stands_and_offers_no_undo(
    origin: str, between: str
) -> None:
    """Another device writes between her press and the page that answers it. The result is
    about the event her press made, never about whatever is newest."""
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 18), action="In my folder", note="mine")
        answer = press(client, client.get(where).text, ESSAY_ID)
        held = answer.headers["location"]
        accepted = store.hand_in_chains([ESSAY_ID])[ESSAY_ID][-1].event_id
        if between == "a note":
            said(store, ESSAY_ID, TURNED_IN, date(2026, 8, 19), head=accepted, note="at the office")
        elif between == "back on the list":
            said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19), head=accepted, action="Ask her")
        else:
            undo_of(store, ESSAY_ID, accepted)
        rows = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])

        first = client.get(held).text
        reloaded = client.get(held).text
        kept = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])

    assert f"hand_in_event={accepted}" in held
    for page in (first, reloaded):
        result = result_of(page)
        assert escape(TURNED_IN_EARLIER) in result
        assert escape(TURNED_IN_FROM_THE_LIST) not in page
        assert 'name="hand_in_id"' not in page
        assert ESSAY_TITLE in result
        assert "World History" in result
        assert f"/student/assignments/{ESSAY_ID}" in result
        if between == "a note":
            assert "You reported it turned in on August 19, 2026." in result
            assert "at the office" in result
            assert listed(page) == []
        elif between == "back on the list":
            assert "You reported Still to turn in on August 19, 2026." in result
            assert "Ask her" in result
            assert listed(page) == [ESSAY_ID]
        else:
            assert "You reported Still to turn in on August 18, 2026." in result
            assert "In my folder" in result
            assert listed(page) == [ESSAY_ID]
    assert kept == rows


@pytest.mark.parametrize("origin", ["list", "week"])
def test_an_undo_result_read_after_the_record_moved_on_is_said_as_something_earlier(
    origin: str,
) -> None:
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    undo = f"/student/actions/assignments/{ESSAY_ID}/undo-hand-in"
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 18))
        after = follow(client, press(client, client.get(where).text, ESSAY_ID))
        undone = client.post(undo, data=form_fields(after, undo), headers=PAGE_HEADERS)
        held = undone.headers["location"]
        head = store.hand_in_chains([ESSAY_ID])[ESSAY_ID][-1]
        said(store, ESSAY_ID, "not_required", date(2026, 8, 19), head=head.event_id, note="online")

        page = client.get(held).text

    assert head.operation == "undo"
    assert f"hand_in_event={head.event_id}" in held
    assert escape(UNDONE_EARLIER) in result_of(page)
    assert escape(HAND_IN_UNDONE) not in page
    assert "You reported nothing to turn in on August 19, 2026." in result_of(page)
    assert "online" in result_of(page)
    assert 'name="hand_in_id"' not in page


@pytest.mark.parametrize("where", [TO_TURN_IN_PAGE, HER_PAGE])
def test_an_address_cannot_make_up_a_result_or_an_undo(where: str) -> None:
    """The event named must be one of that assignment's, of the kind the address says."""
    with browser() as client:
        store = state_of(client).project_state
        waiting = said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        said(store, QUIZ_ID, NEEDS_HAND_IN, date(2026, 8, 18))
        quiz = store.hand_in_chains([QUIZ_ID])[QUIZ_ID][-1].event_id
        turned = said(store, QUIZ_ID, TURNED_IN, date(2026, 8, 19), head=quiz)
        made_up = [
            {"hand_in_said": "turned_in", "about": ESSAY_ID, "hand_in_event": turned},
            {"hand_in_said": "turned_in", "about": ESSAY_ID, "hand_in_event": waiting},
            {"hand_in_said": "undone", "about": QUIZ_ID, "hand_in_event": turned},
            {"hand_in_said": "turned_in", "about": ESSAY_ID, "hand_in_event": "no-such-event"},
            {"hand_in_said": "turned_in", "about": ESSAY_ID, "hand_in_event": "x" * 201},
            {"hand_in_said": "turned_in", "about": ESSAY_ID},
            {"hand_in_said": "turned_in", "hand_in_event": turned},
            {"hand_in_said": "everything", "about": QUIZ_ID, "hand_in_event": turned},
        ]
        pages = [client.get(where, params=params) for params in made_up]
        real = client.get(
            where, params={"hand_in_said": "turned_in", "about": QUIZ_ID, "hand_in_event": turned}
        ).text
        rows = store._connection.execute("SELECT COUNT(*) FROM hand_in_events").fetchone()[0]

    for page in pages:
        assert page.status_code == 200
        assert escape(TURNED_IN_FROM_THE_LIST) not in page.text
        assert escape(HAND_IN_UNDONE) not in page.text
        assert escape(TURNED_IN_EARLIER) not in page.text
        assert 'name="hand_in_id"' not in page.text
        assert listed(page.text) == [ESSAY_ID]
    assert escape(TURNED_IN_FROM_THE_LIST) in real
    assert f'name="hand_in_id" value="{turned}"' in real
    assert rows == 3


@pytest.mark.parametrize("origin", ["list", "week"])
def test_a_result_whose_record_cannot_be_read_now_keeps_its_place_and_says_so(origin: str) -> None:
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        held = press(client, client.get(where).text, ESSAY_ID).headers["location"]
        store._connection.execute("UPDATE hand_in_events SET state = 'invalid-state'")
        store._connection.commit()

        page = client.get(held)

    assert page.status_code == 200
    assert escape(RESULT_UNREADABLE) in result_of(page.text)
    assert ESSAY_TITLE in result_of(page.text)
    assert escape(TURNED_IN_FROM_THE_LIST) not in page.text
    assert 'name="hand_in_id"' not in page.text


@pytest.mark.parametrize("origin", ["list", "week"])
def test_an_undo_shown_with_a_result_is_refused_once_the_record_moves_on(origin: str) -> None:
    """The Undo beside a result names the event the press made. When something newer stands
    it is answered 409, says which update it was about, and is aimed at nothing else."""
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    undo = f"/student/actions/assignments/{ESSAY_ID}/undo-hand-in"
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 18))
        after = follow(client, press(client, client.get(where).text, ESSAY_ID))
        fields = form_fields(after, undo)
        said(store, ESSAY_ID, TURNED_IN, date(2026, 8, 19), head=fields["hand_in_id"], note="desk")

        refused = client.post(undo, data=fields, headers=PAGE_HEADERS)
        kept = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])

    assert refused.status_code == 409
    assert said_first(refused.text, HAND_IN_CANNOT_UNDO)
    about = about_of(refused.text)
    assert ESSAY_TITLE in about
    assert "You asked to undo: Turned in, from August 19, 2026." in about
    assert "You reported it turned in on August 19, 2026." in about
    assert "desk" in about
    assert 'name="hand_in_id"' not in refused.text
    assert escape(HAND_IN_UNDONE) not in refused.text
    assert kept == 3


# ------------------------------------------------ the one press sends one thing only


def about_of(page: str) -> str:
    """What a refusal says about the assignment it was about, beside the refusal."""
    start = page.index('id="to-turn-in-about"')
    return page[start : page.index("</div>", start)]


@pytest.mark.parametrize("origin", ["list", "week"])
@pytest.mark.parametrize(
    "changed",
    [
        {"state": "unknown"},
        {"state": "needs_hand_in"},
        {"state": "not_required"},
        {"note": "said by hand"},
        {"next_action": "said by hand"},
        {"return_to": "week"},
        {"week": FIXTURE_WEEK},
    ],
    ids=["not sure", "still to turn in", "nothing to turn in", "a note", "a step", "a way", "week"],
)
def test_a_press_that_is_not_the_one_the_row_makes_is_refused_and_writes_nothing(
    origin: str, changed: dict[str, str]
) -> None:
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19), note="mine")
        page = client.get(where).text

        refused = press(client, page, ESSAY_ID, **changed)
        kept = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])
        worked = press(client, page, ESSAY_ID)
        again = press(client, page, ESSAY_ID)
        rows = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])

    assert refused.status_code == 422
    assert said_first(refused.text, LIST_BAD_FORM)
    assert escape(TURNED_IN_FROM_THE_LIST) not in refused.text
    assert "said by hand" not in refused.text
    assert kept == 1
    assert (worked.status_code, again.status_code) == (303, 303)
    assert "hand_in_said=turned_in" in worked.headers["location"]
    assert "hand_in_said=same" in again.headers["location"]
    assert rows == 2


@pytest.mark.parametrize("origin", ["list", "week"])
def test_a_press_with_a_file_or_a_field_twice_writes_nothing(origin: str) -> None:
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    action = f"/student/actions/assignments/{ESSAY_ID}/hand-in"
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        fields = form_fields(section(client.get(where).text), action)
        without_note = {name: value for name, value in fields.items() if name != "note"}

        filed = client.post(
            action,
            data=without_note,
            files={"note": ("note.txt", b"said by hand", "text/plain")},
            headers=PAGE_HEADERS,
        )
        twice = client.post(
            action,
            content="&".join(
                ["state=turned_in", *(f"{name}={value}" for name, value in fields.items())]
            ),
            headers={**PAGE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
        kept = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])

    assert (filed.status_code, twice.status_code) == (422, 422)
    assert said_first(filed.text, LIST_BAD_FORM)
    assert said_first(twice.text, LIST_BAD_FORM)
    assert kept == 1


@pytest.mark.parametrize("origin", ["list", "week"])
@pytest.mark.parametrize("extra", ["return_to", "week", "plan_id"])
def test_an_undo_from_the_list_sends_its_two_fields_and_nothing_else(
    origin: str, extra: str
) -> None:
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    undo = f"/student/actions/assignments/{ESSAY_ID}/undo-hand-in"
    sent = {"return_to": "week", "week": FIXTURE_WEEK, "plan_id": "draft:plan:2026-08-19:x"}
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        after = follow(client, press(client, client.get(where).text, ESSAY_ID))
        fields = form_fields(after, undo)

        refused = client.post(undo, data={**fields, extra: sent[extra]}, headers=PAGE_HEADERS)
        kept = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])
        worked = client.post(undo, data=fields, headers=PAGE_HEADERS)

    assert set(fields) == {"hand_in_id", "hand_in_view"}
    assert refused.status_code == 422
    assert said_first(refused.text, LIST_BAD_FORM)
    assert kept == 2
    assert worked.status_code == 303


# -------------------------------------- a refusal names what it was about, as it stands


@pytest.mark.parametrize("origin", ["list", "week"])
@pytest.mark.parametrize(
    ("state", "sentence"),
    [
        ("unknown", "Hand-in status: Not sure. You reported this on August 19, 2026."),
        ("not_required", "You reported nothing to turn in on August 19, 2026."),
        (TURNED_IN, "You reported it turned in on August 19, 2026."),
    ],
)
def test_a_press_on_something_that_left_the_list_is_refused_with_what_stands_beside_it(
    origin: str, state: HandInState, sentence: str
) -> None:
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    with browser() as client:
        store = state_of(client).project_state
        first = said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 18))
        page = client.get(where).text
        said(store, ESSAY_ID, state, date(2026, 8, 19), head=first, note="said on the other one")

        refused = press(client, page, ESSAY_ID)
        kept = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])

    assert refused.status_code == 409
    assert said_first(refused.text, LIST_CHANGED)
    about = about_of(refused.text)
    assert ESSAY_TITLE in about
    assert "World History" in about
    assert sentence in about
    assert "said on the other one" in about
    back = "to_turn_in" if origin == "list" else "week"
    assert f"/student/assignments/{ESSAY_ID}?" in about
    assert f"return_to={back}" in about
    assert "hand_in=change" in about
    assert "Review and update in Turning it in" in about
    assert 'name="expected_hand_in_id"' not in about
    assert listed(refused.text) == []
    assert "To turn in (" not in refused.text
    assert escape(TURNED_IN_FROM_THE_LIST) not in refused.text
    assert kept == 2


def test_a_row_pushed_past_her_weeks_three_is_still_named_and_can_be_said_again() -> None:
    """Her page showed it third. An Undo elsewhere put another ahead of it and its next step
    was edited, so the press is behind and the row is past the three her week shows."""
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = state_of(client).project_state
        names = ["first", "second", "third", "fourth"]
        store.put_on_record(
            [
                a_row(name, f"Work {name}").model_copy(update={"due_date": date(2026, 8, 20)})
                for name in names
            ],
            {},
        )
        heads = {name: said(store, name, NEEDS_HAND_IN, date(2026, 8, 10)) for name in names}
        left = said(store, "second", TURNED_IN, date(2026, 8, 11), head=heads["second"])
        week = client.get(HER_PAGE).text
        undo_of(store, "second", left)
        said(store, "fourth", NEEDS_HAND_IN, date(2026, 8, 19), head=heads["fourth"], action="Ask")

        refused = press(client, section(week), "fourth")
        shown = about_of(refused.text)
        action = "/student/actions/assignments/fourth/hand-in"
        newer = store.hand_in_chains(["fourth"])["fourth"][-1].event_id
        said(store, "fourth", NEEDS_HAND_IN, date(2026, 8, 19), head=newer, note="after that")
        behind = client.post(action, data=form_fields(shown, action), headers=PAGE_HEADERS)
        retry = form_fields(about_of(behind.text), action)
        worked = client.post(action, data=retry, headers=PAGE_HEADERS)
        chain = store.hand_in_chains(["fourth"])["fourth"]

    assert listed(section(week)) == ["first", "third", "fourth"]
    assert refused.status_code == 409
    assert listed(section(refused.text)) == ["first", "second", "third"]
    assert "To turn in (4)" in refused.text
    assert "Work fourth" in shown
    assert "Ask" in shown
    assert form_fields(shown, action)["expected_hand_in_id"] == newer
    assert behind.status_code == 409
    assert "after that" in about_of(behind.text)
    assert retry["state"] == "turned_in"
    assert worked.status_code == 303
    assert chain[-1].state == TURNED_IN
    assert len(chain) == 4


@pytest.mark.parametrize("origin", ["list", "week"])
def test_a_refusal_about_a_record_that_is_gone_or_cannot_be_read_says_which(origin: str) -> None:
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    action = f"/student/actions/assignments/{ESSAY_ID}/hand-in"
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        page = client.get(where).text
        gone = client.post(
            "/student/actions/assignments/assignment-not-there/hand-in",
            data=form_fields(section(page), action),
            headers=PAGE_HEADERS,
        )
        store._connection.execute("UPDATE hand_in_events SET state = 'invalid-state'")
        store._connection.commit()
        unreadable = press(client, page, ESSAY_ID)

    assert gone.status_code == 404
    assert said_first(gone.text, GONE_FROM_THE_LIST)
    assert 'id="to-turn-in-about"' not in gone.text
    assert unreadable.status_code == 500
    assert said_first(unreadable.text, LIST_NOT_SAVED)
    about = about_of(unreadable.text)
    assert ESSAY_TITLE in about
    assert "Your hand-in record for this assignment cannot be read right now." in about
    assert 'name="expected_hand_in_id"' not in about


# ------------------------------------------------- a failed write, and a failed reread


@pytest.mark.parametrize("origin", ["list", "week"])
@pytest.mark.parametrize("route", ["hand-in", "undo-hand-in"])
def test_a_write_the_file_refuses_is_said_even_when_the_list_cannot_be_read_back(
    origin: str, route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First the write fails and the list can still be read; then the reread fails too, and
    the plain page answers from what the request already held, reading nothing."""
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    action = f"/student/actions/assignments/{ESSAY_ID}/{route}"
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        page = client.get(where).text
        if route == "undo-hand-in":
            page = follow(client, press(client, page, ESSAY_ID))
        fields = form_fields(section(page), action)
        before = store._connection.execute("SELECT * FROM hand_in_events").fetchall()
        tried: list[str] = []

        def refuses(*args: object) -> None:
            tried.append("write")
            msg = "the chain forked"
            raise RuntimeError(msg)

        monkeypatch.setattr(store, "_confirm_hand_in_head_locked", refuses)
        readable = client.post(action, data=fields, headers=PAGE_HEADERS)

        seen: list[str] = []
        began: list[int] = []

        def unread(*args: object) -> None:
            began.append(len(seen))
            msg = "the file cannot be read"
            raise RuntimeError(msg)

        monkeypatch.setattr(store, "all_assignments", unread)
        store._connection.set_trace_callback(seen.append)
        plain = client.post(action, data=fields, headers=PAGE_HEADERS)
        store._connection.set_trace_callback(None)
        monkeypatch.undo()
        after = store._connection.execute("SELECT * FROM hand_in_events").fetchall()

    assert readable.status_code == 500
    assert said_first(readable.text, LIST_NOT_SAVED if route == "hand-in" else HAND_IN_NOT_UNDONE)
    assert ESSAY_TITLE in about_of(readable.text)
    assert plain.status_code == 500
    assert "<h1>" in plain.text
    assert escape(PLAIN[route]) in plain.text
    alert = re.search(r'<p class="problem"[^>]*role="alert"[^>]*>(.*?)</p>', plain.text, re.S)
    assert alert is not None
    assert str(escape(PLAIN[route])) in alert.group(1)
    assert 'tabindex="-1"' in alert.group(0)
    assert " autofocus" in alert.group(0).split(">", 1)[0]
    assert plain.text.count(" autofocus") == 1
    assert "Internal Server Error" not in plain.text
    assert escape(TURNED_IN_FROM_THE_LIST) not in plain.text
    assert escape(HAND_IN_UNDONE) not in plain.text
    back = f"{TO_TURN_IN_PAGE}#to-turn-in" if origin == "list" else HER_PAGE
    assert f'href="{back}"' in plain.text
    assert f'href="/student/assignments/{ESSAY_ID}' in plain.text
    assert tried == ["write", "write"]
    assert len(began) == 1
    assert [line for line in seen[began[0] :] if line.strip().upper() != "ROLLBACK"] == []
    assert after == before


# ----------------------------------------------------- what a row and the page say


def test_a_note_written_on_a_later_day_says_its_own_day_beside_the_day_she_took_it_on(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        store = state_of(client).project_state
        first = said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 7, 1), note="first words")
        said(store, QUIZ_ID, NEEDS_HAND_IN, date(2026, 7, 2), note="same day")
        same = client.get(TO_TURN_IN_PAGE).text
        edit = said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19), head=first, note="new words")

        hers = [client.get(TO_TURN_IN_PAGE).text, section(client.get(HER_PAGE).text)]
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        theirs = [client.get(TO_TURN_IN_PAGE).text, section(client.get(HER_PAGE).text)]
        undo_of(store, ESSAY_ID, edit)
        restored = client.get(TO_TURN_IN_PAGE).text

    assert "Note updated" not in same
    for page, who in ((hers[0], "You"), (hers[1], "You"), (theirs[0], "She"), (theirs[1], "She")):
        assert listed(page) == [ESSAY_ID, QUIZ_ID]
        essay = page[page.index(f'id="to-turn-in-{ESSAY_ID}"') : page.index(f'-{QUIZ_ID}"')]
        assert f"{who} reported Still to turn in on July 1, 2026." in essay
        assert "new words" in essay
        assert "Note updated August 19, 2026." in essay
        assert "Note updated" not in page[page.index(f'id="to-turn-in-{QUIZ_ID}"') :]
    assert "first words" in restored
    assert "Note updated" not in restored
    assert "She reported Still to turn in on July 1, 2026." in restored


def test_with_the_sign_in_off_a_later_note_says_its_day_and_the_row_keeps_its_place() -> None:
    with browser() as client:
        store = state_of(client).project_state
        first = said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 7, 1), note="first words")
        said(store, QUIZ_ID, NEEDS_HAND_IN, date(2026, 7, 2))
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19), head=first, note="new words")

        pages = [client.get(TO_TURN_IN_PAGE).text, section(client.get(HER_PAGE).text)]

    for page in pages:
        assert listed(page) == [ESSAY_ID, QUIZ_ID]
        assert "You reported Still to turn in on July 1, 2026." in page
        assert "Note updated August 19, 2026." in page
        assert page.count("Note updated") == 1


def test_a_parent_reads_the_page_as_hers_and_never_as_their_own(tmp_path: pathlib.Path) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        store = state_of(client).project_state
        hers_empty = client.get(TO_TURN_IN_PAGE).text
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        empty = client.get(TO_TURN_IN_PAGE).text
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        held = client.get(TO_TURN_IN_PAGE).text
        turned = said(
            store,
            ESSAY_ID,
            TURNED_IN,
            date(2026, 8, 19),
            head=store.hand_in_chains([ESSAY_ID])[ESSAY_ID][-1].event_id,
        )
        receipt = client.get(
            TO_TURN_IN_PAGE,
            params={"hand_in_said": "turned_in", "about": ESSAY_ID, "hand_in_event": turned},
        ).text

    assert "Back to my week" in hers_empty
    assert EMPTY in hers_empty
    for page in (empty, held, receipt):
        assert "Back to her week" in page
        assert "Back to my week" not in page
        assert "What she said is still to turn in." in page
        assert "What you said" not in page
        assert "your" not in page[page.index("<main") : page.index("</main>")].lower()
    assert "Nothing is on her To turn in list." in empty
    assert EMPTY not in empty
    assert escape(TURNED_IN_FROM_THE_LIST) not in receipt
    assert 'name="hand_in_id"' not in receipt


@pytest.mark.parametrize("origin", ["list", "week"])
def test_nothing_is_said_to_be_on_the_list_only_when_nothing_may_belong_on_it(origin: str) -> None:
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        held = press(client, client.get(where).text, ESSAY_ID).headers["location"]
        emptied = client.get(held).text
        store._connection.execute("UPDATE hand_in_events SET state = 'invalid-state'")
        store._connection.commit()

        unreadable = client.get(held).text

    assert EMPTY in emptied
    assert "cannot be read" in section(unreadable)
    assert "It may belong on this list." in section(unreadable)
    assert EMPTY not in unreadable


# ------------------------------------------ what the list sends is sent as it was written


def the_result(page: str) -> str:
    """The one paragraph that says what a press did."""
    found = re.search(
        r'<p class="note update-result"[^>]*id="to-turn-in-result"[^>]*>.*?</p>', page, re.S
    )
    assert found is not None
    return found.group()


@pytest.mark.parametrize("origin", ["list", "week"])
@pytest.mark.parametrize("padded", [" week ", "list ", " ", "\tlist"])
def test_a_place_for_the_result_with_space_around_it_is_not_one_these_pages_send(
    origin: str, padded: str
) -> None:
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    undo = f"/student/actions/assignments/{ESSAY_ID}/undo-hand-in"
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        page = client.get(where).text

        pressed = press(client, page, ESSAY_ID, hand_in_view=padded)
        kept = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])
        after = follow(client, press(client, page, ESSAY_ID))
        undone = client.post(
            undo, data={**form_fields(after, undo), "hand_in_view": padded}, headers=PAGE_HEADERS
        )
        rows = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])

    assert pressed.status_code == 422
    assert escape(BAD_RETURN) in pressed.text
    assert kept == 1
    assert undone.status_code == 422
    assert escape(BAD_RETURN) in undone.text
    assert rows == 2


@pytest.mark.parametrize("origin", ["list", "week"])
def test_a_list_form_whose_values_carry_space_around_them_is_refused(origin: str) -> None:
    """The list's forms send the head, the state, and the update to take back exactly as the
    page wrote them; the same value with space around it is a form made by hand."""
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    undo = f"/student/actions/assignments/{ESSAY_ID}/undo-hand-in"
    with browser() as client:
        store = state_of(client).project_state
        head = said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19))
        page = client.get(where).text

        refused = [
            press(client, page, ESSAY_ID, expected_hand_in_id=f" {head}"),
            press(client, page, ESSAY_ID, expected_hand_in_id=f"{head} "),
            press(client, page, ESSAY_ID, state=" turned_in"),
            press(client, page, ESSAY_ID, state="turned_in\n"),
        ]
        kept = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])
        after = follow(client, press(client, page, ESSAY_ID))
        fields = form_fields(after, undo)
        named = fields["hand_in_id"]
        for made_up in (f" {named}", f"{named} ", f"\t{named}\n"):
            refused.append(
                client.post(undo, data={**fields, "hand_in_id": made_up}, headers=PAGE_HEADERS)
            )
        rows = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])
        worked = client.post(undo, data=fields, headers=PAGE_HEADERS)

    assert kept == 1
    for answer in refused:
        assert answer.status_code == 422
        assert said_first(answer.text, LIST_BAD_FORM)
    assert rows == 2
    assert worked.status_code == 303


def test_an_assignment_whose_name_holds_a_slash_is_reached_by_every_address_made_for_it() -> None:
    """The row's link and both of its forms, followed as the page wrote them, and from the
    details her work update's form too."""
    slashed = "unit/3 part?b#c"
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = state_of(client).project_state
        store.put_on_record(
            [a_row(slashed, "Slashed set").model_copy(update={"due_date": date(2026, 8, 20)})], {}
        )
        said(store, slashed, NEEDS_HAND_IN, date(2026, 8, 19))
        save, undo = hand_in_actions(slashed)
        pages = {}
        for where in (TO_TURN_IN_PAGE, HER_PAGE):
            page = client.get(where).text
            row = page[page.index(f'id="to-turn-in-{slashed}"') :]
            link = re.search(r'<a class="assignment-link" href="([^"]+)"', row)
            assert link is not None
            details = client.get(link.group(1).replace("&amp;", "&"))
            pressed = client.post(save, data=form_fields(row, save), headers=PAGE_HEADERS)
            after = follow(client, pressed)
            undone = client.post(undo, data=form_fields(after, undo), headers=PAGE_HEADERS)
            pages[where] = (details, pressed, after, undone, follow(client, undone))
        report_action = "/student/actions/assignments/unit%2F3%20part%3Fb%23c/report"
        opened = client.get("/student/assignments/unit%2F3%20part%3Fb%23c", params={"change": "1"})
        reported = client.post(
            report_action,
            data={**form_fields(opened.text, report_action), "status": "done", "note": ""},
            headers=PAGE_HEADERS,
        )
        chain = store.hand_in_chains([slashed])[slashed]

    assert save == "/student/actions/assignments/unit%2F3%20part%3Fb%23c/hand-in"
    for details, pressed, after, undone, back in pages.values():
        assert details.status_code == 200
        assert "Slashed set" in details.text
        assert 'id="turning-it-in"' in details.text
        assert pressed.status_code == 303
        assert escape(TURNED_IN_FROM_THE_LIST) in after
        assert f'action="{undo}"' in after
        assert undone.status_code == 303
        assert listed(back) == [slashed]
    assert opened.status_code == 200
    assert reported.status_code == 303
    assert [event.operation for event in chain] == ["report", "report", "undo", "report", "undo"]


# ------------------------------- already saved, when what stands was put back by an undo


@pytest.mark.parametrize("origin", ["list", "week"])
@pytest.mark.parametrize("followed", [False, True], ids=["as it stands", "after another report"])
def test_a_press_already_saved_by_an_undo_that_put_turned_in_back_says_so(
    origin: str, followed: bool
) -> None:
    """Turned in, then still to turn in, then that taken back: what stands is turned in again,
    and the latest event is the undo. A press from a page that still showed the row writes
    nothing and its result names that undo."""
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    with browser() as client:
        store = state_of(client).project_state
        first = said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 17))
        turned = said(store, ESSAY_ID, TURNED_IN, date(2026, 8, 18), head=first)
        back = said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19), head=turned)
        held = client.get(where).text
        undo_of(store, ESSAY_ID, back)
        put_back = store.hand_in_chains([ESSAY_ID])[ESSAY_ID][-1]

        answer = press(client, held, ESSAY_ID)
        rows = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])
        address = answer.headers["location"]
        if followed:
            said(store, ESSAY_ID, "unknown", date(2026, 8, 19), head=put_back.event_id, note="hm")
        seen: list[str] = []
        store._connection.set_trace_callback(seen.append)
        page = client.get(address).text
        store._connection.set_trace_callback(None)
        again = client.get(address).text

    assert put_back.operation == "undo"
    assert answer.status_code == 303
    assert "hand_in_said=same" in address
    assert f"hand_in_event={put_back.event_id}" in address
    assert rows == 4
    assert len(seen) == 8
    assert the_result(page) == the_result(again)
    assert 'name="hand_in_id"' not in page
    if followed:
        assert escape(SAME_EARLIER) in the_result(page)
        assert "August 18" not in the_result(page)
        assert "Hand-in status: Not sure. You reported this on August 19, 2026." in about_of(page)
        assert "hm" in about_of(page)
    else:
        assert escape(HAND_IN_ALREADY_SAVED) in the_result(page)
        assert ESSAY_TITLE in the_result(page)
        assert "You reported it turned in on August 18, 2026." in the_result(page)
        assert listed(page) == []


@pytest.mark.parametrize("where", [TO_TURN_IN_PAGE, HER_PAGE])
def test_an_already_saved_result_is_only_for_an_event_that_left_turned_in_standing(
    where: str,
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        first = said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 17))
        turned = said(store, ESSAY_ID, TURNED_IN, date(2026, 8, 18), head=first)
        undo_of(store, ESSAY_ID, turned)
        to_waiting = store.hand_in_chains([ESSAY_ID])[ESSAY_ID][-1].event_id
        only = said(store, QUIZ_ID, TURNED_IN, date(2026, 8, 18))
        undo_of(store, QUIZ_ID, only)
        to_nothing = store.hand_in_chains([QUIZ_ID])[QUIZ_ID][-1].event_id
        unsure = said(store, QUIZ_ID, "unknown", date(2026, 8, 19), head=to_nothing)
        none_needed = said(store, ALGEBRA_ID, "not_required", date(2026, 8, 19))
        made_up = [
            ("same", ESSAY_ID, to_waiting),
            ("same", ESSAY_ID, first),
            ("same", QUIZ_ID, to_nothing),
            ("same", QUIZ_ID, unsure),
            ("same", ALGEBRA_ID, none_needed),
            ("same", ALGEBRA_ID, turned),
            ("same", ESSAY_ID, "no-such-event"),
            ("turned_in", ESSAY_ID, to_waiting),
        ]
        pages = [
            client.get(where, params={"hand_in_said": a, "about": b, "hand_in_event": c}).text
            for a, b, c in made_up
        ]
        true_one = client.get(
            where, params={"hand_in_said": "same", "about": ESSAY_ID, "hand_in_event": turned}
        ).text

    for page in pages:
        assert 'id="to-turn-in-result"' not in page
        assert escape(HAND_IN_ALREADY_SAVED) not in page
        assert 'name="hand_in_id"' not in page
    assert escape(SAME_EARLIER) in the_result(true_one)


# --------------------------------------------- a result says what stands, with its day


@pytest.mark.parametrize("origin", ["list", "week"])
def test_a_result_says_what_stands_with_the_day_she_said_it_whatever_day_it_is_read(
    origin: str,
) -> None:
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    undo = f"/student/actions/assignments/{ESSAY_ID}/undo-hand-in"
    with browser() as client:
        store = state_of(client).project_state
        said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 17), action="In my folder")
        held = client.get(where).text
        saved = follow(client, press(client, held, ESSAY_ID))
        with_clock(client, fixture_clock(datetime(2026, 8, 20, 20, 0, tzinfo=UTC)))
        same = follow(client, press(client, held, ESSAY_ID))
        rows = len(store.hand_in_chains([ESSAY_ID])[ESSAY_ID])
        undone = follow(
            client, client.post(undo, data=form_fields(same, undo), headers=PAGE_HEADERS)
        )

    assert escape(TURNED_IN_FROM_THE_LIST) in the_result(saved)
    assert "You reported it turned in on August 19, 2026." in the_result(saved)
    assert escape(HAND_IN_ALREADY_SAVED) in the_result(same)
    assert ESSAY_TITLE in the_result(same)
    assert "You reported it turned in on August 19, 2026." in the_result(same)
    assert "August 20" not in the_result(same)
    assert rows == 2
    assert escape(HAND_IN_UNDONE) in the_result(undone)
    assert "You reported Still to turn in on August 17, 2026." in the_result(undone)
    assert "In my folder" in the_result(undone)
    for page in (saved, same, undone):
        assert 'id="to-turn-in-result" tabindex="-1"' in the_result(page)


# ------------------------------ an address made for any id lands on the place it names

SLASHED = "unit/3 part?b#c"
SLASHED_UNICODE = "unit/" + chr(0xE9) + "/\U0001f469" + chr(0x200D) + "\U0001f52c"


def lands_on(page: str, address: str) -> str:
    """The opening tag of the element a browser lands on for an address: the element whose
    id is the fragment as written, or failing that the fragment with its escapes undone,
    which is the order a browser tries them in. Empty when the fragment names nothing."""
    fragment = urlsplit(address).fragment
    tags = {
        html.unescape(found.group(1)): found.group(0)
        for found in re.finditer(r'<\w+\b[^>]*?\sid="([^"]*)"[^>]*>', page)
    }
    return tags.get(fragment) or tags.get(unquote(fragment)) or ""


def special_record(client: TestClient, name: str) -> ProjectStateStore:
    store = state_of(client).project_state
    store.put_on_record(
        [a_row(name, "Special set").model_copy(update={"due_date": date(2026, 8, 20)})], {}
    )
    said(store, name, NEEDS_HAND_IN, date(2026, 8, 19))
    return store


@pytest.mark.parametrize("name", [SLASHED, SLASHED_UNICODE], ids=["ascii", "unicode"])
@pytest.mark.parametrize("origin", ["list", "week"])
def test_her_work_update_on_the_details_lands_on_its_result_for_any_id(
    origin: str, name: str
) -> None:
    """The details are reached by the row's own link. A save, the same save again, and an
    undo each answer with an address whose fragment names the result that is on the page."""
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    save, undo = report_actions(name)
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = special_record(client, name)
        page = client.get(where).text
        row = page[page.index(f'id="to-turn-in-{name}"') :]
        link = re.search(r'<a class="assignment-link" href="([^"]+)"', row)
        assert link is not None
        details = client.get(html.unescape(link.group(1)))
        fields = {**form_fields(details.text, save), "status": "done", "note": ""}

        saved = client.post(save, data=fields, headers=PAGE_HEADERS)
        after_save = client.get(saved.headers["location"]).text
        again = client.post(save, data=fields, headers=PAGE_HEADERS)
        after_again = client.get(again.headers["location"]).text
        undone = client.post(undo, data=form_fields(after_save, undo), headers=PAGE_HEADERS)
        after_undo = client.get(undone.headers["location"]).text
        reports = store.student_reports(name)

    assert details.status_code == 200
    assert (saved.status_code, again.status_code, undone.status_code) == (303, 303, 303)
    for answer, shown in ((saved, after_save), (again, after_again), (undone, after_undo)):
        landed = lands_on(shown, answer.headers["location"])
        assert 'class="note update-result"' in landed, answer.headers["location"]
        assert f'id="{escape(result_anchor(name))}"' in landed
    back = "Back to To turn in" if origin == "list" else "Back to the week"
    assert back in after_save
    assert len(reports) == 2


@pytest.mark.parametrize("name", [SLASHED, SLASHED_UNICODE], ids=["ascii", "unicode"])
def test_her_hand_in_update_on_the_details_lands_on_its_result_for_any_id(name: str) -> None:
    save, undo = hand_in_actions(name)
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        special_record(client, name)
        opened = client.get(details_href(name, hand_in="change", return_to="to_turn_in")).text
        fields = {**form_fields(opened, save), "state": "not_required", "next_action": ""}
        fields["note"] = "online"

        saved = client.post(save, data=fields, headers=PAGE_HEADERS)
        after_save = client.get(saved.headers["location"]).text
        undone = client.post(undo, data=form_fields(after_save, undo), headers=PAGE_HEADERS)
        after_undo = client.get(undone.headers["location"]).text

    assert (saved.status_code, undone.status_code) == (303, 303)
    for answer, shown in ((saved, after_save), (undone, after_undo)):
        landed = lands_on(shown, answer.headers["location"])
        assert 'class="note update-result"' in landed, answer.headers["location"]
        assert 'id="hand-in-result-' in landed


@pytest.mark.parametrize("name", [SLASHED, SLASHED_UNICODE], ids=["ascii", "unicode"])
def test_a_card_on_her_week_and_the_way_back_to_it_land_on_the_card_for_any_id(name: str) -> None:
    """A save and an undo from the card answer with her week, the card named in the query
    whole and in the fragment; the details' way back to the week lands on the card too, and
    so does the link her week writes for something worth checking."""
    save, undo = report_actions(name)
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = special_record(client, name)
        store.record_status_reports(
            name, [school_said("missing", SourceChannel.EMAIL, date(2026, 8, 19))]
        )
        week = client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text
        card = card_for(week, name)
        fields = {**form_fields(card, save), "status": "done", "note": ""}

        saved = client.post(save, data=fields, headers=PAGE_HEADERS)
        after_save = client.get(saved.headers["location"]).text
        to_check = re.search(r'to check:</strong>\s*<a href="([^"]+)"', after_save)
        assert to_check is not None
        checked = client.get(html.unescape(to_check.group(1))).text
        undone = client.post(
            undo, data=form_fields(card_for(after_save, name), undo), headers=PAGE_HEADERS
        )
        after_undo = client.get(undone.headers["location"]).text
        details = client.get(details_href(name, return_to="week", week=FIXTURE_WEEK)).text
        way_back = re.search(r'<p class="return"><a href="([^"]+)">Back to the week</a>', details)
        assert way_back is not None
        returned = client.get(html.unescape(way_back.group(1))).text

    assert (saved.status_code, undone.status_code) == (303, 303)
    for answer, shown, word in ((saved, after_save, "saved"), (undone, after_undo, "undone")):
        address = answer.headers["location"]
        assert parse_qs(urlsplit(address).query)[word] == [name]
        assert f'id="{assignment_anchor(name)}"' in lands_on(shown, address)
        assert 'class="note update-result"' in card_for(shown, name)
    assert f'id="{assignment_anchor(name)}"' in lands_on(returned, html.unescape(way_back.group(1)))
    assert parse_qs(urlsplit(html.unescape(way_back.group(1))).query)["show"] == [name]
    assert f'id="{assignment_anchor(name)}"' in lands_on(checked, html.unescape(to_check.group(1)))
    assert parse_qs(urlsplit(html.unescape(to_check.group(1))).query)["show"] == [name]


# ------------------------- a list form is held to what stands, where the write is decided

READING_LOG_ID = "assignment-reading-log"


def list_press(origin: str, head: str) -> dict[str, str]:
    """The row's form, made by hand: every field, as the list would write it."""
    return {
        "state": "turned_in",
        "next_action": "",
        "note": "",
        "expected_hand_in_id": head,
        "hand_in_view": origin,
    }


@pytest.mark.parametrize("origin", ["list", "week"])
def test_a_list_shaped_press_over_anything_but_still_to_turn_in_writes_nothing(origin: str) -> None:
    """No row is drawn for these, so no list form could name them: nothing said yet, not
    sure, nothing to turn in, and turned in with a note the press would have dropped."""
    with browser() as client:
        store = state_of(client).project_state
        heads = {
            ESSAY_ID: said(store, ESSAY_ID, "unknown", date(2026, 8, 19)),
            QUIZ_ID: said(store, QUIZ_ID, "not_required", date(2026, 8, 19)),
            ALGEBRA_ID: said(store, ALGEBRA_ID, TURNED_IN, date(2026, 8, 19), note="at the office"),
            READING_LOG_ID: "",
        }
        before = store._connection.execute("SELECT * FROM hand_in_events").fetchall()
        answers = {
            name: client.post(
                hand_in_actions(name)[0], data=list_press(origin, head), headers=PAGE_HEADERS
            )
            for name, head in heads.items()
        }
        after = store._connection.execute("SELECT * FROM hand_in_events").fetchall()

    for name, answer in answers.items():
        assert answer.status_code == 422, name
        assert said_first(answer.text, LIST_BAD_FORM), name
        assert escape(TURNED_IN_FROM_THE_LIST) not in answer.text
        assert 'name="expected_hand_in_id"' not in about_of(answer.text)
    assert "at the office" in about_of(answers[ALGEBRA_ID].text)
    assert "Hand-in status not recorded." in about_of(answers[READING_LOG_ID].text)
    assert after == before


@pytest.mark.parametrize("origin", ["list", "week"])
def test_a_list_shaped_undo_of_anything_but_her_turned_in_report_writes_nothing(
    origin: str,
) -> None:
    """The list offers Undo beside one thing, her report that it was turned in. The head a
    row carries is a report too, of still to turn in, and is not the list's to take back."""
    where = TO_TURN_IN_PAGE if origin == "list" else HER_PAGE
    with browser() as client:
        store = state_of(client).project_state
        waiting = said(store, ESSAY_ID, NEEDS_HAND_IN, date(2026, 8, 19), note="mine")
        unsure = said(store, QUIZ_ID, "unknown", date(2026, 8, 19))
        before = store._connection.execute("SELECT * FROM hand_in_events").fetchall()
        answers = [
            client.post(
                hand_in_actions(name)[1],
                data={"hand_in_id": head, "hand_in_view": origin},
                headers=PAGE_HEADERS,
            )
            for name, head in ((ESSAY_ID, waiting), (QUIZ_ID, unsure))
        ]
        after = store._connection.execute("SELECT * FROM hand_in_events").fetchall()
        undo = hand_in_actions(ESSAY_ID)[1]
        pressed = follow(client, press(client, client.get(where).text, ESSAY_ID))
        worked = client.post(undo, data=form_fields(pressed, undo), headers=PAGE_HEADERS)
        restored = store.hand_in_chains([ESSAY_ID])[ESSAY_ID][-1]

    for answer in answers:
        assert answer.status_code == 422
        assert said_first(answer.text, LIST_BAD_FORM)
        assert escape(HAND_IN_UNDONE) not in answer.text
    assert "mine" in about_of(answers[0].text)
    assert after == before
    assert worked.status_code == 303
    assert (restored.operation, restored.state, restored.note) == ("undo", NEEDS_HAND_IN, "mine")


# ------------------------------------- two ids that differ only by an escape, side by side

PAIR = {"unit/3": "Slash set", "unit%2F3": "Escaped set"}


def landed(page: str, address: str) -> str:
    """The card or row a browser lands on for an address, whole: from the element the
    fragment names to the next card's. Empty when the fragment names nothing."""
    tag = lands_on(page, address)
    if not tag:
        return ""
    start = page.index(tag)
    following = page.find('id="assignment-', start + len(tag))
    return page[start:] if following < 0 else page[start:following]


def in_an_open_fold(page: str, address: str) -> bool:
    """Whether the element an address lands on sits inside a Reported done fold that is
    open, which a fold the server did not open would hide from her."""
    start = page.index(lands_on(page, address))
    opened = page.rfind('<details class="steps reported-done"', 0, start)
    if opened <= page.rfind("</details>", 0, start):
        return False
    return " open" in page[opened : page.index(">", opened)]


@pytest.mark.parametrize("placement", ["due this week", "due later"])
@pytest.mark.parametrize("target", list(PAIR))
def test_every_way_to_a_card_lands_on_that_card_when_another_id_differs_only_by_an_escape(
    target: str, placement: str
) -> None:
    """Both assignments are on her week together, as cards or as rows due later. From the
    way back, a refusal's link, a save, the same save again, the link to something worth
    checking, Change, keeping it as it is, and an undo, the place reached is the one
    assignment's, active or under Reported done with that fold open, and the other's
    record is as it was. The paths are decoded as a server decodes them, once, which for
    an id that holds an escaped percent sign is not what the test client does by itself."""
    other = next(name for name in PAIR if name != target)
    save, undo = report_actions(target)
    with as_served(browser(BLOSSOM_FIXTURE_PATH="")) as client:
        store = state_of(client).project_state
        dates = (
            {"due_date": date(2026, 8, 20)}
            if placement == "due this week"
            else {"assigned_on": date(2026, 8, 18), "due_date": date(2026, 8, 28)}
        )
        store.put_on_record(
            [a_row(name, title).model_copy(update=dates) for name, title in PAIR.items()], {}
        )
        store.record_status_reports(
            target, [school_said("missing", SourceChannel.EMAIL, date(2026, 8, 19))]
        )
        week = client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text
        fields = form_fields(card_for(week, target), save)
        reached: dict[str, tuple[str, str]] = {}

        details = client.get(details_href(target, return_to="week", week=FIXTURE_WEEK)).text
        found = re.search(r'<p class="return"><a href="([^"]+)">Back to the week</a>', details)
        assert found is not None
        way_back = html.unescape(found.group(1))
        reached["the way back"] = (client.get(way_back).text, way_back)

        refused = client.post(save, data={**fields, "note": ""}, headers=PAGE_HEADERS)
        found = re.search(r'<a href="(#[^"]+)">Go to the assignment.</a>', refused.text)
        assert found is not None
        reached["the refusal's link"] = (refused.text, html.unescape(found.group(1)))

        chosen = {**fields, "status": "done", "note": ""}
        saved = client.post(save, data=chosen, headers=PAGE_HEADERS)
        after_save = client.get(saved.headers["location"]).text
        reached["a save"] = (after_save, saved.headers["location"])
        again = client.post(save, data=chosen, headers=PAGE_HEADERS)
        reached["the same save"] = (
            client.get(again.headers["location"]).text,
            again.headers["location"],
        )

        found = re.search(r'to check:</strong>\s*<a href="([^"]+)"', after_save)
        assert found is not None
        to_check = html.unescape(found.group(1))
        reached["worth checking"] = (client.get(to_check).text, to_check)

        mine = landed(after_save, saved.headers["location"])
        found = re.search(r'<form method="get" action="([^"]+)" class="action">', mine)
        assert found is not None
        change = html.unescape(found.group(1))
        opened = client.get(
            urlsplit(change).path, params=form_fields(mine, found.group(1)), headers=PAGE_HEADERS
        ).text
        reached["Change"] = (opened, change)
        found = re.search(r'<a class="cancel" href="([^"]+)"', landed(opened, change))
        assert found is not None
        keep = html.unescape(found.group(1))
        reached["keeping it"] = (client.get(keep).text, keep)

        undone = client.post(undo, data=form_fields(mine, undo), headers=PAGE_HEADERS)
        reached["an undo"] = (
            client.get(undone.headers["location"]).text,
            undone.headers["location"],
        )
        reports = {name: len(store.student_reports(name)) for name in PAIR}

    assert (refused.status_code, saved.status_code, again.status_code) == (422, 303, 303)
    assert undone.status_code == 303
    for how, (page, address) in reached.items():
        place = landed(page, address)
        assert f'id="{assignment_anchor(target)}"' in lands_on(page, address), how
        assert PAIR[target] in place, how
        assert PAIR[other] not in place, how
        done_now = how not in ("the way back", "the refusal's link", "an undo")
        assert in_an_open_fold(page, address) == done_now, how
    for how in ("a save", "the same save", "an undo"):
        assert 'class="note update-result"' in landed(*reached[how]), how
    assert parse_qs(urlsplit(reached["a save"][1]).query)["saved"] == [target]
    assert parse_qs(urlsplit(way_back).query)["show"] == [target]
    assert reports == {target: 2, other: 0}
