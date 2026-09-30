"""A result is hers: what her save, Undo, press or delete did is said to her alone.

A press of hers sends her to an address that names what it did, and the page there says so
where she acted. A parent signed in who opens such an address, her page left open on a
shared device and a parent signing in over it, reads what stands in the parent's words and
no result, as her To turn in list shows a parent: on her week, on a card shown apart and a
row due later, on an assignment's details for her update and her hand-in update, on her To
turn in list on her week and on its own page, and on her homework notes after a delete.
Nothing asks for the focus on such a visit, and the address's fragment names nothing on a
parent's page, so the page opens at its top. With the sign-in off the result is said, as it
is to her.

The fixture week through the app, a pinned day, synthetic words, and no model.
"""

import pathlib
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

import pytest
from fastapi.testclient import TestClient

from blossom.hand_in import NEEDS_HAND_IN
from blossom.routes.captures import NOTE_ALREADY_DELETED, NOTE_DELETED
from blossom.routes.navigation import TO_TURN_IN_PAGE, note_action, note_delete_href
from blossom.routes.student import (
    HAND_IN_ALREADY_SAVED,
    HAND_IN_SAVED,
    HAND_IN_UNDONE,
    UPDATE_ALREADY_SAVED,
    UPDATE_SAVED,
    UPDATE_UNDONE,
)
from blossom.to_turn_in import BACK_ON_THE_LIST, TURNED_IN_FROM_THE_LIST
from tests.support import (
    DETAILS,
    ESSAY_ID,
    HER_PAGE,
    HERS,
    LATER_WEEK,
    PAGE_HEADERS,
    REPORT,
    THEIRS,
    UNDO,
    browser,
    card_for,
    client_for,
    form_fields,
    lands_on,
    main_of,
    report,
    signed_in,
    signed_in_household,
    store_of,
    waiting_note,
    went_to,
    whole_form,
    words,
)
from tests.support import READING_LOG_ID as LOG

READERS: Final = ("her", "a parent", "sign-in off")
ACTIONS: Final = f"/student/actions/assignments/{ESSAY_ID}"
HAND_IN: Final = f"{ACTIONS}/hand-in"
UNDO_HAND_IN: Final = f"{ACTIONS}/undo-hand-in"
DONE: Final = "Student update: Done"
NOT_YET: Final = "Student update: Not yet"
NONE_YET: Final = "No student update yet. Sign in as the student to update."
TURNED_IN_ON: Final = "She reported it turned in on August 19, 2026."
STILL_TO_TURN_IN_ON: Final = "She reported Still to turn in on August 19, 2026."


@contextmanager
def household(reader: str, tmp_path: pathlib.Path) -> Iterator[TestClient]:
    """Her device signed in as her, or the household with the sign-in off. Every press
    made here is hers."""
    if reader == "sign-in off":
        with browser() as client:
            yield client
        return
    with client_for(signed_in_household(tmp_path)) as client:
        signed_in(client, HERS)
        yield client


def read_by(client: TestClient, reader: str) -> None:
    """Who opens the address: she stays, or a parent signs in on her device."""
    if reader == "a parent":
        client.post("/sign-out")
        signed_in(client, THEIRS)


@dataclass(frozen=True)
class Left:
    """An address a press of hers left, the result it names, and words a parent reads there
    instead, from the record as it stands: in the card or row ``about`` names, or anywhere
    in the page's main part."""

    address: str
    said: str
    theirs: str
    about: str | None = None


def details(client: TestClient, **query: str) -> str:
    return client.get(DETAILS, params={"return_to": "week", **query}, headers=PAGE_HEADERS).text


def saved_on_her_week(client: TestClient) -> Left:
    return Left(report(client, ESSAY_ID, "done"), UPDATE_SAVED, DONE, ESSAY_ID)


def saved_again_on_her_week(client: TestClient) -> Left:
    report(client, ESSAY_ID, "not_yet")
    return Left(report(client, ESSAY_ID, "not_yet"), UPDATE_ALREADY_SAVED, NOT_YET, ESSAY_ID)


def undone_on_her_week(client: TestClient) -> Left:
    week = client.get(report(client, ESSAY_ID, "done"), headers=PAGE_HEADERS).text
    fields = form_fields(card_for(week, ESSAY_ID), UNDO)
    undone = client.post(UNDO, data=fields, headers=PAGE_HEADERS)
    return Left(went_to(undone), UPDATE_UNDONE, NONE_YET, ESSAY_ID)


def saved_apart(client: TestClient) -> Left:
    """Her save from the week after, where the essay is shown apart."""
    return Left(report(client, ESSAY_ID, "done", week=LATER_WEEK), UPDATE_SAVED, DONE, ESSAY_ID)


def saved_on_a_row_due_later(client: TestClient) -> Left:
    return Left(report(client, LOG, "done"), UPDATE_SAVED, DONE, LOG)


def saved_on_the_details(client: TestClient) -> Left:
    fields = form_fields(details(client, change="1"), REPORT)
    saved = client.post(REPORT, data={**fields, "status": "done", "note": ""}, headers=PAGE_HEADERS)
    return Left(went_to(saved), UPDATE_SAVED, DONE)


def undone_on_the_details(client: TestClient) -> Left:
    saved_on_the_details(client)
    undone = client.post(UNDO, data=form_fields(details(client), UNDO), headers=PAGE_HEADERS)
    return Left(went_to(undone), UPDATE_UNDONE, NONE_YET)


def handed_in(client: TestClient, state: str = "turned_in") -> str:
    """Her hand-in update from the details' form, as she chose it and with no words."""
    fields = whole_form(details(client, hand_in="change"), HAND_IN)
    return went_to(client.post(HAND_IN, data={**fields, "state": state}, headers=PAGE_HEADERS))


def hand_in_saved_on_the_details(client: TestClient) -> Left:
    return Left(handed_in(client), HAND_IN_SAVED, TURNED_IN_ON)


def hand_in_saved_again_on_the_details(client: TestClient) -> Left:
    handed_in(client)
    return Left(handed_in(client), HAND_IN_ALREADY_SAVED, TURNED_IN_ON)


def hand_in_undone_on_the_details(client: TestClient) -> Left:
    handed_in(client)
    fields = form_fields(details(client), UNDO_HAND_IN)
    undone = client.post(UNDO_HAND_IN, data=fields, headers=PAGE_HEADERS)
    return Left(went_to(undone), HAND_IN_UNDONE, "Hand-in status not recorded.")


def turned_in_on_the_list(client: TestClient, where: str) -> str:
    """The press "I turned it in" on the essay's row of her list at ``where``, once she said
    on the details that it is still to turn in."""
    handed_in(client, NEEDS_HAND_IN)
    page = client.get(where, headers=PAGE_HEADERS).text
    row = page[page.index(f'id="to-turn-in-{ESSAY_ID}"') :]
    return went_to(client.post(HAND_IN, data=form_fields(row, HAND_IN), headers=PAGE_HEADERS))


def turned_in_on_her_week(client: TestClient) -> Left:
    return Left(
        turned_in_on_the_list(client, HER_PAGE),
        TURNED_IN_FROM_THE_LIST,
        "Reported turned in.",
        ESSAY_ID,
    )


def turned_in_on_the_to_turn_in_page(client: TestClient) -> Left:
    return Left(
        turned_in_on_the_list(client, TO_TURN_IN_PAGE),
        TURNED_IN_FROM_THE_LIST,
        "Nothing is on her To turn in list.",
    )


def undone_on_the_to_turn_in_page(client: TestClient) -> Left:
    result = client.get(turned_in_on_the_list(client, TO_TURN_IN_PAGE), headers=PAGE_HEADERS)
    fields = form_fields(result.text, UNDO_HAND_IN)
    undone = client.post(UNDO_HAND_IN, data=fields, headers=PAGE_HEADERS)
    return Left(went_to(undone), BACK_ON_THE_LIST, STILL_TO_TURN_IN_ON)


def deleted(client: TestClient, times: int) -> str:
    """A note of hers that was never used, deleted from its own page, the press sent
    ``times`` times; the address the last one went to."""
    name = waiting_note(
        store_of(client), course="Geometry", title="Questions 4-8", text="Questions 4-8"
    )
    action = note_action(name, "delete")
    fields = whole_form(client.get(note_delete_href(name), headers=PAGE_HEADERS).text, action)
    sent = [client.post(action, data=fields, headers=PAGE_HEADERS) for _ in range(times)]
    return went_to(sent[-1])


def note_deleted(client: TestClient) -> Left:
    return Left(deleted(client, 1), NOTE_DELETED, "What she wrote down to remember.")


def note_deleted_again(client: TestClient) -> Left:
    return Left(deleted(client, 2), NOTE_ALREADY_DELETED, "What she wrote down to remember.")


SURFACES: Final[dict[str, Callable[[TestClient], Left]]] = {
    "her week, a save": saved_on_her_week,
    "her week, the same save again": saved_again_on_her_week,
    "her week, an Undo": undone_on_her_week,
    "her week, a card shown apart": saved_apart,
    "her week, a row due later": saved_on_a_row_due_later,
    "the details, a save": saved_on_the_details,
    "the details, an Undo": undone_on_the_details,
    "the details, a hand-in update": hand_in_saved_on_the_details,
    "the details, the same hand-in update again": hand_in_saved_again_on_the_details,
    "the details, a hand-in Undo": hand_in_undone_on_the_details,
    "her week's To turn in list, I turned it in": turned_in_on_her_week,
    "her To turn in page, I turned it in": turned_in_on_the_to_turn_in_page,
    "her To turn in page, an Undo": undone_on_the_to_turn_in_page,
    "her homework notes, a delete": note_deleted,
    "her homework notes, a delete sent again": note_deleted_again,
}


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("surface", list(SURFACES))
def test_a_result_is_said_to_her_and_a_parent_reads_what_stands(
    surface: str, reader: str, tmp_path: pathlib.Path
) -> None:
    """Her press, then the address it left opened by her, by a parent who signed in over
    her page, and with the sign-in off. Only a parent is shown no result."""
    with household(reader, tmp_path) as client:
        left = SURFACES[surface](client)
        read_by(client, reader)
        answer = client.get(left.address, headers=PAGE_HEADERS)

    assert answer.status_code == 200
    assert re.search(r"\sautofocus(?=[\s>])", answer.text) is None
    page = main_of(answer.text)
    landed = lands_on(page, left.address)
    if reader == "a parent":
        assert landed == ""
        assert "update-result" not in page
        assert left.said not in words(page)
        assert left.theirs in words(page if left.about is None else card_for(page, left.about))
    else:
        assert landed.startswith('<p class="note update-result" role="status" id="'), landed
        start = page.index(landed)
        assert words(page[start : page.index("</p>", start)]).startswith(left.said)
