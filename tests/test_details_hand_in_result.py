# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The hand-in result on an assignment's details belongs to one event.

A save, a repeated save, and an undo from the hand-in form each send her to
the details at an address that names the event the save returned. The page
says the result only when its own reading finds that event in this
assignment's history, of the kind the word says, whatever hand-in state it
left. While that event is the latest, the result is said as what stands, and
the section's Undo names that event. Once something newer follows it, the
result is said as done earlier, beside what stands now, with no Undo. Any
other address says nothing, and the section is as on a plain visit. A parent
reads what stands and no result.

A save that discards a next step, typed beside an answer other than Still to
turn in, is answered with the details themselves: the result, and apart from
it a line that this attempt's next step was not saved. That line belongs to
that one answer. No address carries it, so no later read says it. The result
takes the focus and names that line as its description.

The fixture week through the app on a pinned clock, forms read from the
page's own HTML, and no model asked.
"""

import pathlib
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from functools import partial
from html import unescape
from typing import Final
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.app import create_app
from blossom.dependencies import ApplicationState
from blossom.hand_in import (
    NEEDS_HAND_IN,
    TURNED_IN,
    HandInAlreadySaved,
    HandInEvent,
    HandInSaved,
    HandInState,
    HandInUndone,
)
from blossom.routes import hand_in as hand_in_routes
from blossom.routes.navigation import details_href, segment
from blossom.routes.student import HAND_IN_ALREADY_SAVED, HAND_IN_SAVED, HAND_IN_UNDONE
from blossom.stores.drafts import StoreBusy
from blossom.to_turn_in import RESULT_UNREADABLE
from tests.support import (
    DETAILS,
    ESSAY_ID,
    FIXTURE_WEEK,
    HERS,
    PAGE_HEADERS,
    PLAN_DATE,
    QUIZ_ID,
    SAME_ORIGIN,
    THEIRS,
    Answer,
    as_stored,
    browser,
    due,
    form_fields,
    main_of,
    pressed_form,
    refusing,
    signed_in_household,
    spoil,
    spy_on_stores,
    state_of,
    words,
)

SAVE: Final = f"/student/actions/assignments/{ESSAY_ID}/hand-in"
UNDO: Final = f"/student/actions/assignments/{ESSAY_ID}/undo-hand-in"
QUIZ_DETAILS: Final = f"/student/assignments/{QUIZ_ID}"
AT_NINE: Final = datetime(2026, 8, 19, 21, 0, tzinfo=UTC)
STATES: Final[tuple[HandInState, ...]] = (NEEDS_HAND_IN, TURNED_IN, "not_required", "unknown")
WORDS: Final = ("saved", "same", "undone", "saved_without_step", "same_without_step")
SAVED_EARLIER: Final = "Your hand-in update was saved earlier. This is what stands now."
UNDONE_EARLIER: Final = "Your hand-in update was undone earlier. This is what stands now."
STANDING: Final = (HAND_IN_SAVED, HAND_IN_ALREADY_SAVED, HAND_IN_UNDONE)
EVERY_SENTENCE: Final = (*STANDING, SAVED_EARLIER, UNDONE_EARLIER)
RESULT_LINE: Final = 'class="note update-result"'
SECTION_UNDO: Final = 'name="hand_in_id"'
TURN_IN_UNDO: Final = 'name="undo_hand_in"'
OTHER_ANSWERS: Final[tuple[HandInState, ...]] = (TURNED_IN, "not_required", "unknown")
"""The answers a next step does not go with."""
STEP_NOTICE: Final = (
    "The next step was not saved by this attempt: it goes only with Still to turn in."
)
NOTICE_ID: Final = f"hand-in-result-{ESSAY_ID}-step-not-saved"
NOTICE_LINE: Final = f'<p class="note" id="{NOTICE_ID}">{STEP_NOTICE}</p>'
DESCRIBED_BY_THE_NOTICE: Final = f'aria-describedby="{NOTICE_ID}"'
"""What the result line of a direct answer carries: the line about the step, an element of
its own after it, is named as its description, so whoever arrives on the result is told
both."""
ABOUT_A_STEP: Final = "next step was not saved"
"""Words any line about a discarded step holds, and nothing else on these pages does."""
NOT_SHOWN: Final = "This assignment can't be shown right now."
RECORD_UNREADABLE: Final = (
    "Your hand-in record for this assignment cannot be read right now, so it is not shown. "
    "Nothing can be saved here until it can be read."
)
"""The section's own line over a hand-in record that cannot be read."""


def section(page: str) -> str:
    """The Turning it in section of a details page, from the section around its heading."""
    start = page.rindex("<section", 0, page.index('id="turning-it-in"'))
    return page[start : page.index("</section>", start)]


def stands(turning: str) -> str:
    """The line of the section that says what stands now."""
    found = re.search(r'<p class="update-summary">.*?</p>', turning, re.DOTALL)
    assert found is not None
    return found.group(0)


def result_line(turning: str) -> str:
    """The one line of the section that says what a save or an undo did."""
    found = re.findall(r'<p class="note update-result"[^>]*>.*?</p>', turning, re.DOTALL)
    assert len(found) == 1, found
    return str(found[0])


def typed(
    client: TestClient, state: HandInState, *, action: str = "", **back: str
) -> dict[str, str]:
    """What the hand-in form of the details sends, opened by its own link, with her choice
    and a next step as she typed it."""
    page = client.get(DETAILS, params={"hand_in": "change", **back}).text
    return {**form_fields(page, SAVE), "state": state, "next_action": action, "note": ""}


def save(
    client: TestClient, state: HandInState, *, note: str = "", action: str = "", **back: str
) -> Answer:
    """The hand-in form of the details, opened by its own link and sent with her choice."""
    sent = {**typed(client, state, action=action, **back), "note": note}
    return client.post(SAVE, data=sent, headers=PAGE_HEADERS)


def undo(client: TestClient, page: str) -> Answer:
    """The section's own Undo, sent as ``page`` wrote it."""
    return client.post(UNDO, data=form_fields(section(page), UNDO), headers=PAGE_HEADERS)


def chain(client: TestClient) -> list[HandInEvent]:
    return state_of(client).project_state.hand_in_chains([ESSAY_ID]).get(ESSAY_ID, [])


def ids(client: TestClient) -> list[str]:
    return [event.event_id for event in chain(client)]


def asked(answer: Answer) -> dict[str, str]:
    """What the address a press was answered with carries, each field once."""
    assert answer.status_code == 303, answer.text[:300]
    fields = parse_qsl(urlsplit(answer.headers["location"]).query, keep_blank_values=True)
    carried = dict(fields)
    assert len(carried) == len(fields), fields
    return carried


def followed(client: TestClient, answer: Answer) -> str:
    """The section of the page a press sends her to."""
    assert answer.status_code == 303, answer.text[:300]
    return section(client.get(answer.headers["location"]).text)


def saved(client: TestClient, state: HandInState = TURNED_IN, **back: str) -> Answer:
    return save(client, state, **back)


def saved_again(client: TestClient, state: HandInState = TURNED_IN, **back: str) -> Answer:
    assert save(client, state, **back).status_code == 303
    return save(client, state, **back)


def undone(client: TestClient, state: HandInState = TURNED_IN, **back: str) -> Answer:
    first = save(client, state, **back)
    assert first.status_code == 303
    return undo(client, client.get(first.headers["location"]).text)


def saved_again_over_an_undo(client: TestClient, **back: str) -> Answer:
    """Still to turn in, then turned in, then that taken back: the head is an undo that put
    Still to turn in back, and the same answer again is already saved over it."""
    assert save(client, NEEDS_HAND_IN, **back).status_code == 303
    turned_in = save(client, TURNED_IN, **back)
    assert undo(client, client.get(turned_in.headers["location"]).text).status_code == 303
    return save(client, NEEDS_HAND_IN, **back)


MADE: Final[dict[str, Callable[..., Answer]]] = {
    "saved": saved,
    "same": saved_again,
    "undone": undone,
}
"""A real save, repeated save, and undo from the details, by the word its address says."""
NEWER: Final[dict[str, tuple[HandInState, str | None] | None]] = {
    "turned in with a note": (TURNED_IN, "in the tray"),
    "nothing to turn in": ("not_required", None),
    "not sure": ("unknown", None),
    "still to turn in": (NEEDS_HAND_IN, None),
    "an undo": None,
}
"""What follows the event a result is about: another report, or an Undo of the head."""


def reported(client: TestClient, state: HandInState, note: str | None = None) -> str:
    """One more report after the head, written through the store, and its id."""
    kept = state_of(client).project_state.record_hand_in(
        ESSAY_ID, state, None, note, expected_head=ids(client)[-1], now=AT_NINE, today=PLAN_DATE
    )
    assert isinstance(kept, HandInSaved)
    return kept.event.event_id


def newer_than(client: TestClient, kind: str) -> str:
    """One more event after the head, written through the store, and its id."""
    report = NEWER[kind]
    if report is None:
        store = state_of(client).project_state
        taken_back = store.undo_hand_in(ESSAY_ID, ids(client)[-1], now=AT_NINE, today=PLAN_DATE)
        assert isinstance(taken_back, HandInUndone)
        return taken_back.event.event_id
    return reported(client, *report)


# ------------------------------------------------------- the address names the event

REDIRECTS: Final[dict[str, tuple[Callable[..., Answer], str, str]]] = {
    "saved, still to turn in": (partial(saved, state=NEEDS_HAND_IN), "saved", "report"),
    "saved, turned in": (partial(saved, state=TURNED_IN), "saved", "report"),
    "saved, nothing to turn in": (partial(saved, state="not_required"), "saved", "report"),
    "saved, not sure": (partial(saved, state="unknown"), "saved", "report"),
    "already saved over a report": (saved_again, "same", "report"),
    "already saved over an undo": (saved_again_over_an_undo, "same", "undo"),
    "undone": (undone, "undone", "undo"),
}
"""Each press the details answer with a redirect: how it is made, the word its address
says, and the kind of event the operation returned."""


@pytest.mark.parametrize(
    "back", [{}, {"return_to": "week", "week": FIXTURE_WEEK}], ids=["no way back", "a way back"]
)
@pytest.mark.parametrize("press", REDIRECTS)
def test_each_details_hand_in_redirect_names_the_event_the_operation_returned(
    press: str, back: dict[str, str]
) -> None:
    """The event a save made, the head a repeated save found standing, and the undo an Undo
    made are each the latest event when the press is answered, and the address names it."""
    made, word, operation = REDIRECTS[press]
    with browser() as client:
        answer = made(client, **back)
        events = chain(client)

    where = asked(answer)
    assert answer.headers["location"].endswith(f"#hand-in-result-{ESSAY_ID}")
    assert where.get("hand_in") == word
    assert events[-1].operation == operation
    assert where.get("hand_in_event") == events[-1].event_id
    assert {name: where.get(name) for name in back} == back


# ------------------------------------------------------------- a result that stands


@pytest.mark.parametrize("state", STATES)
def test_a_real_save_repeat_and_undo_each_say_their_sentence_in_every_state(
    state: HandInState,
) -> None:
    with browser() as client:
        first = save(client, state)
        after_save = followed(client, first)
        after_repeat = followed(client, save(client, state))
        after_undo = followed(client, undo(client, client.get(first.headers["location"]).text))
        plain = section(client.get(DETAILS).text)

    for shown, sentence in (
        (after_save, HAND_IN_SAVED),
        (after_repeat, HAND_IN_ALREADY_SAVED),
        (after_undo, HAND_IN_UNDONE),
    ):
        assert shown.count(RESULT_LINE) == 1
        assert [said for said in EVERY_SENTENCE if said in shown] == [sentence]
    assert "Hand-in status not recorded." in after_undo
    assert RESULT_LINE not in plain


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("outcome", ["saved", "same"])
def test_the_undo_beside_a_standing_result_names_the_event_in_the_address(
    outcome: str, state: HandInState
) -> None:
    """A report that stands can be taken back from the page that says it was saved, and the
    one Undo there names the event the result is about."""
    with browser() as client:
        answer = MADE[outcome](client, state)
        shown = followed(client, answer)

    named = asked(answer).get("hand_in_event")
    assert named is not None
    assert re.findall(r'name="hand_in_id" value="([^"]*)"', shown) == [named]
    assert TURN_IN_UNDO not in shown


@pytest.mark.parametrize(
    "made", [undone, saved_again_over_an_undo], ids=["undone", "already saved over an undo"]
)
def test_a_standing_result_about_an_undo_offers_no_undo(made: Callable[..., Answer]) -> None:
    with browser() as client:
        answer = made(client)
        shown = followed(client, answer)
        head = chain(client)[-1]

    assert head.operation == "undo"
    assert asked(answer).get("hand_in_event") == head.event_id
    assert shown.count(RESULT_LINE) == 1
    assert SECTION_UNDO not in shown
    assert TURN_IN_UNDO not in shown


# ------------------------------------------------- an address the reading does not bear out

UNBORNE: Final[dict[str, dict[str, str]]] = {
    **{f"{word} with no event": {"hand_in": word} for word in WORDS},
    "a blank event": {"hand_in": "saved", "hand_in_event": ""},
    "an event no record holds": {"hand_in": "saved", "hand_in_event": "hand-in-made-up"},
    "another assignment's event": {"hand_in": "saved", "hand_in_event": "QUIZ"},
    "a report named as an undo": {"hand_in": "undone", "hand_in_event": "HEAD"},
    "an undo named as a save": {"hand_in": "saved", "hand_in_event": "UNDO"},
    "an undo that restored nothing named as already saved": {
        "hand_in": "same",
        "hand_in_event": "UNDO",
    },
    "an id longer than any": {"hand_in": "saved", "hand_in_event": "x" * 201},
    "a word no save says": {"hand_in": "made_up", "hand_in_event": "HEAD"},
}


@pytest.mark.parametrize("named", UNBORNE)
def test_an_address_the_reading_does_not_bear_out_leaves_the_section_as_on_any_visit(
    named: str,
) -> None:
    """Her history here is a report, its undo, which restored nothing, and a report that
    stands and can be taken back. The quiz has a report of its own."""
    with browser() as client:
        taken_back = followed(client, undone(client))
        assert "Hand-in status not recorded." in taken_back
        assert save(client, NEEDS_HAND_IN).status_code == 303
        _, the_undo, head = chain(client)
        store = state_of(client).project_state
        other = store.record_hand_in(
            QUIZ_ID, TURNED_IN, None, None, expected_head=None, now=AT_NINE, today=PLAN_DATE
        )
        assert isinstance(other, HandInSaved)
        events = {
            "HEAD": head.event_id,
            "UNDO": the_undo.event_id,
            "QUIZ": other.event.event_id,
        }
        query = {field: events.get(value, value) for field, value in UNBORNE[named].items()}
        plain = section(client.get(DETAILS).text)
        shown = section(client.get(DETAILS, params=query).text)

    assert (the_undo.operation, the_undo.state) == ("undo", None)
    assert f'name="hand_in_id" value="{head.event_id}"' in plain
    assert RESULT_LINE not in plain
    assert shown == plain


def test_an_undo_that_put_a_state_back_is_no_save_and_may_be_what_already_stood() -> None:
    """Turned in, then Not sure, then that taken back: the undo is the latest event and
    Turned in stands again. Named as a save it says nothing, since a save is a report, and
    the section is as on a plain visit. Named as a save of what already stood it is said, as
    any event that left a state standing may be, with no Undo beside it, since the latest
    event is no report."""
    with browser() as client:
        assert save(client, TURNED_IN).status_code == 303
        not_sure = save(client, "unknown")
        assert undo(client, client.get(not_sure.headers["location"]).text).status_code == 303
        the_undo = chain(client)[-1]
        named = {"hand_in_event": the_undo.event_id}
        plain = section(client.get(DETAILS).text)
        as_a_save = section(client.get(DETAILS, params={"hand_in": "saved", **named}).text)
        as_already = section(client.get(DETAILS, params={"hand_in": "same", **named}).text)

    assert (the_undo.operation, the_undo.state) == ("undo", TURNED_IN)
    assert RESULT_LINE not in plain
    assert SECTION_UNDO not in plain
    assert as_a_save == plain
    said = result_line(as_already)
    assert [sentence for sentence in EVERY_SENTENCE if sentence in said] == [HAND_IN_ALREADY_SAVED]
    assert SECTION_UNDO not in as_already
    assert TURN_IN_UNDO not in as_already


@pytest.mark.parametrize("word", WORDS)
def test_this_assignments_event_says_nothing_on_an_assignment_with_no_events(word: str) -> None:
    with browser() as client:
        answer = saved(client)
        event = asked(answer).get("hand_in_event") or ids(client)[-1]
        plain = section(client.get(QUIZ_DETAILS).text)
        alone = section(client.get(QUIZ_DETAILS, params={"hand_in": word}).text)
        with_the_essays = section(
            client.get(QUIZ_DETAILS, params={"hand_in": word, "hand_in_event": event}).text
        )

    assert "Hand-in status not recorded." in plain
    assert RESULT_LINE not in plain
    assert alone == plain
    assert with_the_essays == plain


# --------------------------------------------------------------- an older result

OLDER: Final = [
    (outcome, kind)
    for outcome in MADE
    for kind, report in NEWER.items()
    # An undo is the head only until a report follows it: nothing takes an undo back.
    if not (outcome == "undone" and report is None)
]


@pytest.mark.parametrize(("outcome", "kind"), OLDER)
def test_an_older_result_is_said_as_earlier_with_no_undo(outcome: str, kind: str) -> None:
    """Something newer follows the event the address names. The result is said as done
    earlier beside what stands now, and the section offers no Undo, which would take back
    the newer event. Everything else the section offers on a plain visit stays."""
    with browser() as client:
        answer = MADE[outcome](client)
        about = ids(client)[-1]
        newer = newer_than(client, kind)
        shown = section(client.get(answer.headers["location"]).text)
        plain = section(client.get(DETAILS).text)

    earlier = UNDONE_EARLIER if outcome == "undone" else SAVED_EARLIER
    assert newer != about
    assert shown.count(RESULT_LINE) == 1
    assert [said for said in EVERY_SENTENCE if said in shown] == [earlier]
    assert stands(shown) == stands(plain)
    assert SECTION_UNDO not in shown
    assert TURN_IN_UNDO not in shown
    assert (SECTION_UNDO in plain) is (NEWER[kind] is not None)
    for control in (">I turned it in</button>", ">Other turn-in options</a>", ">Change</button>"):
        assert shown.count(control) == plain.count(control)


def test_a_stale_result_page_cannot_take_back_a_newer_update() -> None:
    """The page that said a save was kept is asked for again once a newer report stands. It
    offers no Undo, and the Undo drawn while the save stood names that save, so it is refused
    and nothing is written."""
    with browser() as client:
        where = saved(client).headers["location"]
        drawn = form_fields(section(client.get(where).text), UNDO)
        newer = newer_than(client, "still to turn in")
        before = ids(client)
        stale = section(client.get(where).text)
        refused = client.post(UNDO, data=drawn, headers=PAGE_HEADERS)
        after = ids(client)

    assert drawn["hand_in_id"] == before[0]
    assert before[-1] == newer
    assert SECTION_UNDO not in stale
    assert refused.status_code == 409
    assert after == before


# --------------------------------------------------------------------- a parent


@pytest.mark.parametrize("kind", [None, "nothing to turn in"], ids=["standing", "older"])
@pytest.mark.parametrize("outcome", MADE)
def test_a_parent_at_a_hand_in_result_address_reads_what_stands_and_no_result(
    outcome: str, kind: str | None, tmp_path: pathlib.Path
) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        where = MADE[outcome](client).headers["location"]
        if kind is not None:
            newer_than(client, kind)
        hers = section(client.get(where).text)
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        shown = section(client.get(where).text)
        plain = section(client.get(DETAILS).text)

    assert hers.count(RESULT_LINE) == 1
    assert shown == plain
    assert RESULT_LINE not in shown
    assert not [said for said in EVERY_SENTENCE if said in shown]
    assert "<button" not in shown
    assert "Sign in as the student to update." in shown


# ------------------------------------------------- pages that answer a press themselves


def test_no_refusal_page_says_a_hand_in_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused save is answered with the details themselves, with no redirect: nothing
    chosen, a page that fell behind, and a write the file refuses. None says a result."""
    with browser() as client:
        behind = client.get(DETAILS, params={"hand_in": "change"}).text
        fields = form_fields(behind, SAVE)
        nothing_chosen = client.post(
            SAVE, data={**fields, "next_action": "", "note": ""}, headers=PAGE_HEADERS
        )
        assert saved(client).status_code == 303
        fell_behind = client.post(
            SAVE,
            data={**fields, "state": NEEDS_HAND_IN, "next_action": "", "note": ""},
            headers=PAGE_HEADERS,
        )
        store = state_of(client).project_state

        def refuses(*args: object) -> None:
            msg = "the chain forked"
            raise RuntimeError(msg)

        monkeypatch.setattr(store, "_confirm_hand_in_head_locked", refuses)
        not_written = save(client, "not_required")

    answers = (nothing_chosen, fell_behind, not_written)
    assert [answer.status_code for answer in answers] == [422, 409, 500]
    for answer in answers:
        assert RESULT_LINE not in answer.text
        assert not [said for said in EVERY_SENTENCE if said in answer.text]


# ------------------------------------------------------- a record that cannot be read


@pytest.mark.parametrize("outcome", MADE)
def test_a_result_address_over_an_unreadable_record_says_no_save(outcome: str) -> None:
    """The reading finds no event in a record it cannot read, so no save, repeat or undo is
    said. An address that names a result and an event is told that what was done cannot be
    shown; a word with no event says nothing. The section's own line and its lack of
    anything to press are as on a plain visit."""
    with browser() as client:
        answer = MADE[outcome](client)
        spoil(state_of(client).project_state, "hand_in_events", "state", "none-of-the-four")
        shown = section(client.get(answer.headers["location"]).text)
        plain = section(client.get(DETAILS).text)
        word_alone = section(client.get(DETAILS, params={"hand_in": asked(answer)["hand_in"]}).text)

    assert "cannot be read right now" in plain
    assert RESULT_LINE not in plain
    assert word_alone == plain
    assert not [said for said in EVERY_SENTENCE if said in shown]
    assert shown.count(RESULT_LINE) == 1
    assert str(escape(RESULT_UNREADABLE)) in shown
    assert "cannot be read right now, so it is not shown" in shown
    assert "<button" not in shown
    assert f'action="{SAVE}"' not in shown


# ----------------------------------------------------- beside a turn-in result


def test_a_turn_in_result_is_read_first_and_the_hand_in_words_only_when_there_is_none() -> None:
    with browser() as client:
        action, sent = pressed_form(client.get(DETAILS).text, "hand_in_head")
        pressed = client.post(action, data=sent, headers=PAGE_HEADERS)
        assert pressed.status_code == 303, pressed.text[:300]
        event = ids(client)[-1]
        turn_in = {"turn_in_said": "turned_in", "turn_in_event": event}
        alone = section(client.get(DETAILS, params=turn_in).text)
        both = section(
            client.get(DETAILS, params={**turn_in, "hand_in": "saved", "hand_in_event": event}).text
        )

    assert "You reported this turned in." in alone
    assert alone.count(RESULT_LINE) == 1
    assert both == alone


# ------------------------------------------------- a save that discards a next step


@pytest.mark.parametrize("state", OTHER_ANSWERS)
def test_a_save_that_discards_a_next_step_is_answered_with_the_details_themselves(
    state: HandInState,
) -> None:
    """No redirect: the page says the result, held to the event the save made, and after it,
    as an element of its own, that this attempt's next step was not saved. The step is kept
    nowhere and shown nowhere, and the section's Undo names the event the result is about."""
    with browser() as client:
        answer = save(client, state, action="Blue folder")
        events = chain(client)

    assert answer.status_code == 200, answer.headers.get("location")
    assert "location" not in answer.headers
    shown = section(answer.text)
    said = result_line(shown)
    assert [sentence for sentence in EVERY_SENTENCE if sentence in said] == [HAND_IN_SAVED]
    assert ABOUT_A_STEP not in said
    assert "autofocus" in said
    assert answer.text.count("autofocus") == 1
    assert shown.count(NOTICE_LINE) == 1
    assert shown.count(ABOUT_A_STEP) == 1
    assert shown.index(said) < shown.index(NOTICE_LINE)
    assert [(event.operation, event.state, event.next_action) for event in events] == [
        ("report", state, None)
    ]
    assert re.findall(r'name="hand_in_id" value="([^"]*)"', shown) == [events[0].event_id]
    assert "Blue folder" not in answer.text


def test_every_other_save_repeat_and_undo_is_answered_with_a_redirect() -> None:
    """A step saved with Still to turn in, a save with no step, a repeat with no step, a step
    of spaces only, and an Undo discard nothing, so each is answered with a redirect that
    says the plain word and names the latest event."""
    answered: list[tuple[Answer, str]] = []
    discarding_nothing: tuple[tuple[HandInState, str], ...] = (
        (NEEDS_HAND_IN, "Blue folder"),
        (TURNED_IN, ""),
        (TURNED_IN, ""),
        ("not_required", "   "),
    )
    with browser() as client:
        for state, action in discarding_nothing:
            answered.append((save(client, state, action=action), ids(client)[-1]))
        answered.append((undo(client, client.get(DETAILS).text), ids(client)[-1]))
        events = chain(client)

    assert [asked(answer).get("hand_in") for answer, _ in answered] == [
        "saved",
        "saved",
        "same",
        "saved",
        "undone",
    ]
    assert [asked(answer).get("hand_in_event") for answer, _ in answered] == [
        head for _, head in answered
    ]
    assert [event.next_action for event in events] == ["Blue folder", None, None, None]


@pytest.mark.parametrize("situation", ["a standing event", "an older event", "no event"])
@pytest.mark.parametrize("word", ["saved_without_step", "same_without_step"])
def test_a_word_about_a_next_step_says_nothing_on_a_later_read(word: str, situation: str) -> None:
    """No address carries the discarded step. A word about one is no word the page knows,
    with a real event of a save that discarded a step, with that event once something newer
    follows it, and with none."""
    with browser() as client:
        named = {}
        if situation != "no event":
            save(client, TURNED_IN, action="Blue folder")
            named = {"hand_in_event": ids(client)[-1]}
        if situation == "an older event":
            newer_than(client, "nothing to turn in")
        plain = section(client.get(DETAILS).text)
        shown = section(client.get(DETAILS, params={"hand_in": word, **named}).text)

    assert RESULT_LINE not in plain
    assert ABOUT_A_STEP not in plain
    assert shown == plain


def test_the_plain_word_with_the_event_says_the_result_and_nothing_of_a_step() -> None:
    with browser() as client:
        save(client, TURNED_IN, action="Blue folder")
        event = ids(client)[-1]
        shown = section(
            client.get(DETAILS, params={"hand_in": "saved", "hand_in_event": event}).text
        )

    assert HAND_IN_SAVED in result_line(shown)
    assert ABOUT_A_STEP not in shown
    assert "autofocus" not in shown


def test_the_same_form_sent_again_is_already_saved_with_the_notice_and_writes_nothing() -> None:
    """A reload of the answer sends the same fields again. Nothing is written: the page
    says it was already saved, about the event that stands, with the line about this
    attempt's step, and its Undo names that event."""
    with browser() as client:
        sent = typed(client, TURNED_IN, action="Blue folder")
        first = client.post(SAVE, data=sent, headers=PAGE_HEADERS)
        again = client.post(SAVE, data=sent, headers=PAGE_HEADERS)
        events = chain(client)

    assert (first.status_code, again.status_code) == (200, 200)
    shown = section(again.text)
    said = result_line(shown)
    assert [sentence for sentence in EVERY_SENTENCE if sentence in said] == [HAND_IN_ALREADY_SAVED]
    assert shown.count(NOTICE_LINE) == 1
    assert len(events) == 1
    assert re.findall(r'name="hand_in_id" value="([^"]*)"', shown) == [events[0].event_id]


def test_the_same_form_sent_again_after_a_newer_update_is_refused_and_writes_nothing() -> None:
    """Something newer and different stands when the same fields arrive again. The save is
    refused as any save from a page that fell behind, and so is an Undo drawn while the
    first save stood. Nothing is written, and no result or line about a step is said."""
    with browser() as client:
        sent = typed(client, TURNED_IN, action="Blue folder")
        client.post(SAVE, data=sent, headers=PAGE_HEADERS)
        drawn = form_fields(section(client.get(DETAILS).text), UNDO)
        newer = newer_than(client, "still to turn in")
        before = ids(client)
        again = client.post(SAVE, data=sent, headers=PAGE_HEADERS)
        refused = client.post(UNDO, data=drawn, headers=PAGE_HEADERS)
        after = ids(client)

    assert before == [drawn["hand_in_id"], newer]
    assert (again.status_code, refused.status_code) == (409, 409)
    assert after == before
    for answer in (again, refused):
        assert RESULT_LINE not in answer.text
        assert ABOUT_A_STEP not in answer.text


def after_the_save(
    patch: pytest.MonkeyPatch, then: Callable[[ApplicationState, HandInEvent], None]
) -> None:
    """Make ``then`` happen once the route's own save is kept, or found already standing,
    and before its page is read. The save is the store's own, and what it returned is
    handed on as it was."""
    kept = hand_in_routes.hand_in_now

    def and_then(
        state: ApplicationState,
        assignment_id: str,
        said: HandInState,
        action: str,
        note: str,
        *,
        expected_head: str | None,
        only_over: HandInState | None = None,
    ) -> object:
        made = kept(
            state,
            assignment_id,
            said,
            action,
            note,
            expected_head=expected_head,
            only_over=only_over,
        )
        assert isinstance(made, HandInSaved | HandInAlreadySaved)
        then(state, made.event if isinstance(made, HandInSaved) else made.head)
        return made

    patch.setattr(hand_in_routes, "hand_in_now", and_then)


def a_newer_report(state: ApplicationState, about: HandInEvent) -> None:
    """Still to turn in, reported through the store after the event a save is about."""
    newer = state.project_state.record_hand_in(
        about.assignment_id,
        NEEDS_HAND_IN,
        None,
        None,
        expected_head=about.event_id,
        now=AT_NINE,
        today=PLAN_DATE,
    )
    assert isinstance(newer, HandInSaved)


def off_the_record(state: ApplicationState, about: HandInEvent) -> None:
    """The assignment is taken off the record; her hand-in events stay in the file."""
    store = state.project_state
    store._connection.execute(
        "DELETE FROM assignments WHERE assignment_id = ?", (about.assignment_id,)
    )
    store._connection.commit()


def record_unreadable(state: ApplicationState, about: HandInEvent) -> None:
    """Her hand-in events for the assignment are damaged in the file, so none can be read."""
    spoil(state.project_state, "hand_in_events", "state", "none-of-the-four", about.assignment_id)


@pytest.mark.parametrize("outcome", ["saved", "same"])
def test_a_newer_event_before_the_page_is_read_gives_the_earlier_result_and_the_notice(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another report lands after the save is kept, or found already standing, and before
    its page is read: both writes are the store's own. The result is said as done earlier
    with no Undo, which would take back the newer report, and the line about this attempt's
    step is still said."""
    with browser() as client:
        sent = typed(client, TURNED_IN, action="Blue folder")
        if outcome == "same":
            assert client.post(SAVE, data=sent, headers=PAGE_HEADERS).status_code == 200
        after_the_save(monkeypatch, a_newer_report)
        answer = client.post(SAVE, data=sent, headers=PAGE_HEADERS)
        events = chain(client)
        plain = section(client.get(DETAILS).text)

    assert answer.status_code == 200, answer.headers.get("location")
    shown = section(answer.text)
    said = result_line(shown)
    assert [sentence for sentence in EVERY_SENTENCE if sentence in shown] == [SAVED_EARLIER]
    assert SAVED_EARLIER in said
    assert ABOUT_A_STEP not in said
    assert shown.count(NOTICE_LINE) == 1
    assert SECTION_UNDO not in shown
    assert TURN_IN_UNDO not in shown
    assert [(event.state, event.next_action) for event in events] == [
        (TURNED_IN, None),
        (NEEDS_HAND_IN, None),
    ]
    assert stands(shown) == stands(plain)
    assert SECTION_UNDO in plain


def kept_and_not_shown(answer: Answer, confirmed: str) -> str:
    """The answer to a kept save whose own page cannot be read, held to all it may say: the
    result the save confirmed, that this attempt's next step was not saved, which page
    cannot be shown, and the ways back. Nothing says or suggests the update was not saved,
    nothing of her choice or words is shown, and there is nothing to press. Its main part."""
    assert answer.status_code == 503, answer.headers.get("location")
    main = main_of(answer.text)
    alert = re.search(
        r'<p class="problem" role="alert" id="problem-summary" tabindex="-1" autofocus>(.*?)</p>',
        main,
        re.DOTALL,
    )
    assert alert is not None, main[:600]
    told = f"{confirmed} {STEP_NOTICE} {NOT_SHOWN}"
    assert words(alert.group(1)) == told
    assert "<h1>Turning it in</h1>" in main
    assert words(main).endswith(f"Turning it in {told} Back to the assignment Back to the week")
    assert answer.text.count("not saved") == 1
    assert answer.text.count("was not saved by this attempt") == 1
    for never in (
        "It was not saved",
        "Update not saved",
        "Nothing was saved",
        "Your choice for turning it in",
        "as typed",
        "Blue folder",
    ):
        assert never not in answer.text, never
    for control in ("<form", "<button", "<input", "<textarea", "<select"):
        assert control not in main, control
    assert answer.text.count("autofocus") == 1
    assert "set-cookie" not in answer.headers
    return main


@pytest.mark.parametrize("outcome", ["saved", "same"])
def test_a_kept_save_whose_page_cannot_be_read_says_what_was_kept_and_the_notice(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The save is kept, or was already, and the builder of its page fails as a store that
    cannot be read fails. The page builder is called once. The answer says the confirmed
    result, that this attempt's next step was not saved, and which page cannot be shown; it
    never says the update was not saved, shows none of her choice or words, and holds no
    form and nothing to press."""
    tried: list[str] = []

    def unreadable(*_: object, **__: object) -> None:
        tried.append("the details")
        msg = "disk I/O error"
        raise sqlite3.OperationalError(msg)

    with browser() as client:
        sent = typed(client, TURNED_IN, action="Blue folder")
        if outcome == "same":
            client.post(SAVE, data=sent, headers=PAGE_HEADERS)
        monkeypatch.setattr(hand_in_routes, "detail_page", unreadable)
        answer = client.post(SAVE, data=sent, headers=PAGE_HEADERS)
        events = chain(client)

    kept_and_not_shown(answer, HAND_IN_SAVED if outcome == "saved" else HAND_IN_ALREADY_SAVED)
    assert tried == ["the details"]
    assert [(event.operation, event.state, event.next_action) for event in events] == [
        ("report", TURNED_IN, None)
    ]


@pytest.mark.parametrize("fault", [sqlite3.OperationalError, StoreBusy])
@pytest.mark.parametrize("outcome", ["saved", "same"])
def test_a_reading_that_fails_after_the_save_is_tried_once_and_no_store_is_asked_again(
    outcome: str, fault: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The details are built by their own code, and the one reading of her hand-in events
    they make fails. Every public call of the four stores is marked. The save is made once,
    the reading is tried once, and no store is called after it fails; the answer is the page
    that says what was kept."""
    calls: list[str] = []
    with browser() as client:
        sent = typed(client, TURNED_IN, action="Blue folder")
        if outcome == "same":
            client.post(SAVE, data=sent, headers=PAGE_HEADERS)
        state = state_of(client)
        with monkeypatch.context() as patch:
            spy_on_stores(patch, state, calls)
            patch.setattr(state.project_state, "hand_in_readings", refusing(fault, calls))
            answer = client.post(SAVE, data=sent, headers=PAGE_HEADERS)
        events = chain(client)

    kept_and_not_shown(answer, HAND_IN_SAVED if outcome == "saved" else HAND_IN_ALREADY_SAVED)
    assert calls.count("FAILED HERE") == 1
    failed = calls.index("FAILED HERE")
    assert calls[:failed].count("project_state.record_hand_in") == 1
    assert calls[failed + 1 :] == []
    assert [(event.operation, event.state, event.next_action) for event in events] == [
        ("report", TURNED_IN, None)
    ]


# --------------------------------------------- the same answer in other circumstances


def test_the_same_form_sent_again_after_an_equal_newer_update_is_already_saved_over_it() -> None:
    """Turned in is saved with a step discarded, then Not sure and Turned in again are
    reported elsewhere, so what stands equals the first form though a newer event holds it.
    The first form sent again writes nothing: it is already saved, about the newer head,
    which the page's Undo names. The Undo the first answer drew names the first event, so
    it is refused and writes nothing."""
    with browser() as client:
        sent = typed(client, TURNED_IN, action="Blue folder")
        first = client.post(SAVE, data=sent, headers=PAGE_HEADERS)
        assert first.status_code == 200, first.headers.get("location")
        drawn = form_fields(section(first.text), UNDO)
        not_sure = reported(client, "unknown")
        equal = reported(client, TURNED_IN)
        before = ids(client)
        again = client.post(SAVE, data=sent, headers=PAGE_HEADERS)
        refused = client.post(UNDO, data=drawn, headers=PAGE_HEADERS)
        after = ids(client)

    assert before == [drawn["hand_in_id"], not_sure, equal]
    assert again.status_code == 200, again.headers.get("location")
    shown = section(again.text)
    said = result_line(shown)
    assert [sentence for sentence in EVERY_SENTENCE if sentence in said] == [HAND_IN_ALREADY_SAVED]
    assert shown.count(NOTICE_LINE) == 1
    assert re.findall(r'name="hand_in_id" value="([^"]*)"', shown) == [equal]
    assert refused.status_code == 409
    assert after == before


def test_the_signed_in_student_is_answered_with_the_details_themselves(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        answer = save(client, TURNED_IN, action="Blue folder")
        events = chain(client)

    assert answer.status_code == 200, answer.headers.get("location")
    shown = section(answer.text)
    assert HAND_IN_SAVED in result_line(shown)
    assert shown.count(NOTICE_LINE) == 1
    assert [(event.operation, event.state, event.next_action) for event in events] == [
        ("report", TURNED_IN, None)
    ]
    assert re.findall(r'name="hand_in_id" value="([^"]*)"', shown) == [events[0].event_id]


def test_the_direct_answer_and_its_repeat_set_no_cookie() -> None:
    """That a step was discarded is said by the answer and kept nowhere: neither the answer
    nor the same form sent again sets a cookie, and the client holds none after them."""
    with browser() as client:
        sent = typed(client, TURNED_IN, action="Blue folder")
        first = client.post(SAVE, data=sent, headers=PAGE_HEADERS)
        again = client.post(SAVE, data=sent, headers=PAGE_HEADERS)
        held = dict(client.cookies)

    assert (first.status_code, again.status_code) == (200, 200)
    assert "set-cookie" not in first.headers
    assert "set-cookie" not in again.headers
    assert held == {}


def test_an_assignment_off_the_record_before_the_page_is_read_is_said_as_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The save is kept, and the assignment leaves the record before its page is read. The
    answer is the page for an assignment that is not on record: it says nothing of the
    save, no line about a step, nothing that was not saved, and it holds no form. The
    event stays in the file."""
    with browser() as client:
        sent = typed(client, TURNED_IN, action="Blue folder")
        after_the_save(monkeypatch, off_the_record)
        answer = client.post(SAVE, data=sent, headers=PAGE_HEADERS)
        stored = as_stored(state_of(client).project_state, "hand_in_events")

    assert answer.status_code == 404
    main = main_of(answer.text)
    assert "This assignment is not on record now." in main
    assert not [sentence for sentence in EVERY_SENTENCE if sentence in answer.text]
    assert RESULT_LINE not in answer.text
    assert ABOUT_A_STEP not in answer.text
    assert "not saved" not in answer.text
    assert "Blue folder" not in answer.text
    for control in ("<form", "<button", "<input", "<textarea"):
        assert control not in main, control
    assert len(stored) == 1


@pytest.mark.parametrize("outcome", ["saved", "same"])
def test_a_record_unreadable_before_the_page_is_read_says_so_and_the_notice(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The save is kept, or was already, and her hand-in events cannot be read when the page
    is. The section says that what was done cannot be shown, then that this attempt's next
    step was not saved, then its own line that the record cannot be read, and nothing else:
    no form, nothing to press, and no other word of anything not saved."""
    with browser() as client:
        sent = typed(client, TURNED_IN, action="Blue folder")
        if outcome == "same":
            assert client.post(SAVE, data=sent, headers=PAGE_HEADERS).status_code == 200
        after_the_save(monkeypatch, record_unreadable)
        answer = client.post(SAVE, data=sent, headers=PAGE_HEADERS)
        stored = as_stored(state_of(client).project_state, "hand_in_events")

    assert answer.status_code == 200, answer.headers.get("location")
    shown = section(answer.text)
    assert words(shown) == (
        f"Turning it in {RESULT_UNREADABLE} Back to the week {STEP_NOTICE} {RECORD_UNREADABLE}"
    )
    assert str(escape(RESULT_UNREADABLE)) in result_line(shown)
    assert shown.count(NOTICE_LINE) == 1
    assert f'<p class="problem">{RECORD_UNREADABLE}</p>' in shown
    assert shown.count("not saved") == 1
    assert not [sentence for sentence in EVERY_SENTENCE if sentence in shown]
    for control in ("<form", "<button", "<input", "<textarea"):
        assert control not in shown, control
    assert len(stored) == 1


# ------------------------------------------- the result names the notice as its description


def opening_tag(element: str) -> str:
    """The opening tag of an element, with its attributes."""
    return element[: element.index(">") + 1]


DIRECT_ANSWERS: Final = (
    "saved, turned in",
    "saved, nothing to turn in",
    "saved, not sure",
    "already saved",
    "saved earlier",
    "already saved, then earlier",
    "a record that cannot be read",
)
"""Each direct answer that shows a result line above the line about the step."""


@pytest.mark.parametrize("case", DIRECT_ANSWERS)
def test_the_result_of_a_direct_answer_names_the_line_about_the_step_as_its_description(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The result line holds the focus, and the line about the step is a separate element
    after it. The result names that element as its description, by an id one element on the
    page has, so the step is said to whoever arrives on the result."""
    states: dict[str, HandInState] = {
        "saved, nothing to turn in": "not_required",
        "saved, not sure": "unknown",
    }
    with browser() as client:
        sent = typed(client, states.get(case, TURNED_IN), action="Blue folder")
        if case.startswith("already saved"):
            assert client.post(SAVE, data=sent, headers=PAGE_HEADERS).status_code == 200
        if case.endswith("earlier"):
            after_the_save(monkeypatch, a_newer_report)
        if case == "a record that cannot be read":
            after_the_save(monkeypatch, record_unreadable)
        answer = client.post(SAVE, data=sent, headers=PAGE_HEADERS)

    assert answer.status_code == 200, answer.headers.get("location")
    shown = section(answer.text)
    said = result_line(shown)
    result = opening_tag(said)
    assert DESCRIBED_BY_THE_NOTICE in result
    assert result.count("aria-describedby") == 1
    assert "autofocus" in result
    assert answer.text.count(f'id="{NOTICE_ID}"') == 1
    assert answer.text.count(NOTICE_ID) == 2
    assert re.search(re.escape(said) + r"\s*" + re.escape(NOTICE_LINE), shown) is not None
    assert ABOUT_A_STEP not in said


def test_no_page_without_the_line_about_a_step_describes_its_result_by_it() -> None:
    """A result read from an address, a plain visit and a refusal show no line about a step.
    None holds that line's id, and no result line names a description, so no reference
    points at an element the page lacks."""
    with browser() as client:
        behind = typed(client, NEEDS_HAND_IN)
        assert save(client, TURNED_IN, action="Blue folder").status_code == 200
        named = {"hand_in_event": ids(client)[-1]}
        pages = {
            "a plain visit": client.get(DETAILS),
            "saved, standing": client.get(DETAILS, params={"hand_in": "saved", **named}),
            "already saved, standing": client.get(DETAILS, params={"hand_in": "same", **named}),
            "a form that fell behind": client.post(SAVE, data=behind, headers=PAGE_HEADERS),
        }
        newer_than(client, "nothing to turn in")
        pages["saved, earlier"] = client.get(DETAILS, params={"hand_in": "saved", **named})
        spoil(state_of(client).project_state, "hand_in_events", "state", "none-of-the-four")
        pages["a record that cannot be read"] = client.get(
            DETAILS, params={"hand_in": "saved", **named}
        )

    assert {name: page.status_code for name, page in pages.items()} == {
        "a plain visit": 200,
        "saved, standing": 200,
        "already saved, standing": 200,
        "a form that fell behind": 409,
        "saved, earlier": 200,
        "a record that cannot be read": 200,
    }
    with_a_result = []
    for name, page in pages.items():
        assert NOTICE_ID not in page.text, name
        assert ABOUT_A_STEP not in page.text, name
        shown = section(page.text)
        if RESULT_LINE in shown:
            with_a_result.append(name)
            assert "aria-describedby" not in opening_tag(result_line(shown)), name
    assert with_a_result == [
        "saved, standing",
        "already saved, standing",
        "saved, earlier",
        "a record that cannot be read",
    ]


AWKWARD_IDS: Final = {
    "a space, a slash, an apostrophe, a hash, a question mark": "set/2 it's #1? \u00e9",
    "a space, quotes, angle brackets, an ampersand": 'a "quoted" <b> & c',
}
"""Assignment ids the record keeps as they are, each holding characters that are not part of
one id token on a page."""


@pytest.mark.parametrize("held", AWKWARD_IDS)
def test_the_description_is_one_id_token_whatever_the_assignments_id_holds(held: str) -> None:
    """The record keeps any assignment id but a dot or two. With one that holds a space and
    other characters an id token cannot, the direct answer's result line still names its
    description by exactly one token, and that token is the id of the line about the step,
    which one element has."""
    name = AWKWARD_IDS[held]
    details = details_href(name)
    action = f"/student/actions/assignments/{segment(name)}/hand-in"
    with browser() as client:
        store = state_of(client).project_state
        store.put_on_record([due(name, "Set two, question one", date(2026, 8, 21))], {})
        opened = client.get(details, params={"hand_in": "change"})
        assert opened.status_code == 200, opened.text[:300]
        sent = {
            **form_fields(opened.text, action),
            "state": TURNED_IN,
            "next_action": "Blue folder",
            "note": "",
        }
        answer = client.post(action, data=sent, headers=PAGE_HEADERS)
        events = store.hand_in_chains([name]).get(name, [])

    assert answer.status_code == 200, answer.headers.get("location")
    assert [(event.assignment_id, event.state) for event in events] == [(name, TURNED_IN)]
    shown = section(answer.text)
    described = re.findall(r'aria-describedby="([^"]*)"', opening_tag(result_line(shown)))
    assert len(described) == 1
    tokens = unescape(described[0]).split()
    assert len(tokens) == 1, tokens
    notices = re.findall(rf'<p class="note" id="([^"]*)">{re.escape(STEP_NOTICE)}</p>', shown)
    assert [unescape(found) for found in notices] == tokens
    assert answer.text.count(f'id="{notices[0]}"') == 1
