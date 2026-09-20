"""Her To turn in list: everything she reports as still to turn in, found from her main page.

Outside the fold of finished work and outside any week, however old, in the
order she took each on. One press says a thing was turned in, with the head
the row showed, and can be undone from where she pressed. The fixture week
through the app, a pinned clock, and forms read from the page's own HTML.
"""

import pathlib
import re
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.app import create_app
from blossom.hand_in import NEEDS_HAND_IN, TURNED_IN, HandInSaved, HandInState
from blossom.reconciliation import SourceChannel
from blossom.routes.hand_in import (
    GONE_FROM_THE_LIST,
    LIST_ALREADY_UNDONE,
    LIST_BAD_FORM,
    LIST_CHANGED,
    LIST_NOT_A_HAND_IN_OF_THIS,
    LIST_NOT_SAVED,
)
from blossom.routes.navigation import TO_TURN_IN_PAGE
from blossom.routes.student import (
    BAD_RETURN,
    HAND_IN_ALREADY_SAVED,
    HAND_IN_UNDONE,
    NOT_HERS_TO_UPDATE,
)
from blossom.stores.project_state import ProjectStateStore
from blossom.to_turn_in import TURNED_IN_FROM_THE_LIST
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
    browser,
    card_for,
    form_fields,
    report,
    school_said,
    signed_in_household,
    state_of,
)

QUIZ_ID = "assignment-vocabulary-quiz"
ALGEBRA_ID = "assignment-algebra-set"
EMPTY = "Nothing is on your To turn in list."
PRESS = "I turned it in"


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

    assert page.status_code == 200
    assert listed(page.text) == [ESSAY_ID]
    assert "cannot be read" in page.text
    assert "Vocabulary quiz" in page.text[page.text.index("cannot be read") - 400 :]


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
