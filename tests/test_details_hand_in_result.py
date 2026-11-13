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

The fixture week through the app on a pinned clock, forms read from the
page's own HTML, and no model asked.
"""

import pathlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Final
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.app import create_app
from blossom.hand_in import (
    NEEDS_HAND_IN,
    TURNED_IN,
    HandInEvent,
    HandInSaved,
    HandInState,
    HandInUndone,
)
from blossom.routes.student import HAND_IN_ALREADY_SAVED, HAND_IN_SAVED, HAND_IN_UNDONE
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
    browser,
    form_fields,
    pressed_form,
    signed_in_household,
    spoil,
    state_of,
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


def section(page: str) -> str:
    """The Turning it in section of a details page, from the section around its heading."""
    start = page.rindex("<section", 0, page.index('id="turning-it-in"'))
    return page[start : page.index("</section>", start)]


def stands(turning: str) -> str:
    """The line of the section that says what stands now."""
    found = re.search(r'<p class="update-summary">.*?</p>', turning, re.DOTALL)
    assert found is not None
    return found.group(0)


def save(client: TestClient, state: HandInState, *, note: str = "", **back: str) -> Answer:
    """The hand-in form of the details, opened by its own link and sent with her choice."""
    page = client.get(DETAILS, params={"hand_in": "change", **back}).text
    fields = form_fields(page, SAVE)
    return client.post(
        SAVE,
        data={**fields, "state": state, "next_action": "", "note": note},
        headers=PAGE_HEADERS,
    )


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


def newer_than(client: TestClient, kind: str) -> str:
    """One more event after the head, written through the store, and its id."""
    store = state_of(client).project_state
    head = ids(client)[-1]
    report = NEWER[kind]
    if report is None:
        taken_back = store.undo_hand_in(ESSAY_ID, head, now=AT_NINE, today=PLAN_DATE)
        assert isinstance(taken_back, HandInUndone)
        return taken_back.event.event_id
    state, note = report
    kept = store.record_hand_in(
        ESSAY_ID, state, None, note, expected_head=head, now=AT_NINE, today=PLAN_DATE
    )
    assert isinstance(kept, HandInSaved)
    return kept.event.event_id


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
