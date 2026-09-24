"""The school's instructions on the pages: her week's card and an assignment's details show
the school's words apart from anyone's note, those that apply in the one order, and the rest
folded, in the voice of whoever reads them.
"""

import pathlib
import sqlite3
from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.app import create_app
from blossom.reconciliation import SourceChannel
from blossom.routes.navigation import details_href, instructions_review_href
from blossom.school_instructions import InstructionChoice, InstructionSeen
from blossom.settings import Settings
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    ESSAY_ID,
    HER_PAGE,
    HERS,
    PAGE_HEADERS,
    SAME_ORIGIN,
    THEIRS,
    card_for,
    signed_in_household,
    state_of,
)

A = "Outline three causes before drafting."
B = "Compare two canals in the conclusion."
C = "Bring the map handout."
NOW = datetime(2026, 8, 19, 20, 0, tzinfo=UTC)
TODAY = date(2026, 8, 19)
TYPED = "Ask about the library pass."
FOLD = "Earlier instructions from the school"
SAID = "These are the school's words. Which of them apply is chosen by the family"


def client_for(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN)


def signed_in(client: TestClient, passphrase: str) -> None:
    came_in = client.post("/sign-in", data={"passphrase": passphrase})
    assert came_in.status_code == 303, came_in.text


def store_of(client: TestClient) -> ProjectStateStore:
    return state_of(client).project_state


def kept(store: ProjectStateStore, *texts: str, applies: frozenset[str]) -> None:
    """Keep these instructions for the essay, the first alone and the rest by a choice."""
    first, *rest = texts
    store.settle_school_instructions(
        ESSAY_ID,
        [InstructionSeen(first, SourceChannel.LMS)],
        None,
        authored_by="parent",
        now=NOW,
        today=TODAY,
    )
    if rest:
        revision = store.school_instruction_readings([ESSAY_ID]).readable[ESSAY_ID].revision
        store.settle_school_instructions(
            ESSAY_ID,
            [InstructionSeen(text, SourceChannel.LMS) for text in rest],
            InstructionChoice(revision, texts, applies),
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )


def test_her_card_shows_what_applies_and_folds_the_earlier_in_her_voice(
    tmp_path: pathlib.Path,
) -> None:
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        signed_in(client, HERS)
        kept(store_of(client), A, B, applies=frozenset({A}))
        page = client.get(HER_PAGE, headers=PAGE_HEADERS).text

    card = card_for(page, ESSAY_ID)
    assert f'From the school: <q class="authored-text">{escape(A)}</q>' in card
    shown, folded = card.split(f"<summary>{FOLD}</summary>", 1)
    assert str(escape(B)) not in shown
    assert str(escape(B)) in folded.split("</details>", 1)[0]
    assert SAID in card
    assert "nor your own updates" in card
    assert "Review school instructions" not in card


def test_a_parent_reading_her_details_is_told_about_her_and_offered_the_review(
    tmp_path: pathlib.Path,
) -> None:
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        signed_in(client, THEIRS)
        kept(store_of(client), A, B, applies=frozenset({A}))
        page = client.get(details_href(ESSAY_ID, return_to="week"), headers=PAGE_HEADERS).text

    assert "nor her own updates" in page
    assert f'<a href="{instructions_review_href(ESSAY_ID)}">Review school instructions</a>' in page


def test_the_instructions_that_apply_read_in_one_order_whichever_came_first(
    tmp_path: pathlib.Path,
) -> None:
    both = frozenset({A, C})
    one, other = tmp_path / "one", tmp_path / "other"
    pages = []
    for place, order in ((one, (A, C)), (other, (C, A))):
        place.mkdir()
        settings = signed_in_household(place)
        with client_for(settings) as client:
            signed_in(client, HERS)
            kept(store_of(client), *order, applies=both)
            pages.append(client.get(details_href(ESSAY_ID), headers=PAGE_HEADERS).text)

    for page in pages:
        first, second = page.index(str(escape(C))), page.index(str(escape(A)))
        assert first < second
    assert FOLD not in pages[0]


def test_a_parents_note_and_the_schools_words_are_shown_apart(tmp_path: pathlib.Path) -> None:
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        signed_in(client, HERS)
        store = store_of(client)
        kept(store, A, applies=frozenset({A}))
        essay = store.one_assignment(ESSAY_ID)
        assert essay is not None
        store.upsert_assignments(
            [
                essay.model_copy(
                    update={
                        "note": TYPED,
                        "origins": {**essay.origins, "note": SourceChannel.PARENT_ENTRY},
                    }
                )
            ]
        )
        page = client.get(details_href(ESSAY_ID), headers=PAGE_HEADERS).text
        found = store.school_instruction_readings([ESSAY_ID]).readable[ESSAY_ID]

    assert f'From the school: <q class="authored-text">{escape(A)}</q>' in page
    assert f"A parent wrote: <q>{escape(TYPED)}</q>" in page
    assert found.texts == (A,)


def test_an_instruction_waiting_for_review_is_folded_and_applies_to_nothing(
    tmp_path: pathlib.Path,
) -> None:
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        kept(store_of(client), A, applies=frozenset({A}))
    connection = sqlite3.connect(settings.database_path)
    with connection:
        connection.execute(
            "UPDATE assignments SET note = ?, origins = ? WHERE assignment_id = ?",
            (C, '{"note": "EMAIL"}', ESSAY_ID),
        )
    connection.close()
    with client_for(settings) as client:
        signed_in(client, HERS)
        page = client.get(details_href(ESSAY_ID), headers=PAGE_HEADERS).text

    shown, folded = page.split(
        "<summary>Instructions from the school waiting for review, and earlier ones</summary>", 1
    )
    assert f'From the school: <q class="authored-text">{escape(A)}</q>' in shown
    assert str(escape(C)) not in shown
    assert "Waiting for a parent's review" in folded
    assert str(escape(C)) in folded.split("</details>", 1)[0]


def test_an_instruction_that_cannot_be_read_is_said_and_offers_no_review(
    tmp_path: pathlib.Path,
) -> None:
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        kept(store_of(client), A, applies=frozenset({A}))
    connection = sqlite3.connect(settings.database_path)
    with connection:
        connection.execute(
            "UPDATE school_instructions SET state = 'bent' WHERE assignment_id = ?", (ESSAY_ID,)
        )
    connection.close()
    with client_for(settings) as client:
        signed_in(client, THEIRS)
        details = client.get(details_href(ESSAY_ID, return_to="family"), headers=PAGE_HEADERS)
        week = client.get(HER_PAGE, headers=PAGE_HEADERS)
        review = client.get(instructions_review_href(ESSAY_ID), headers=PAGE_HEADERS)

    assert details.status_code == 200
    assert "cannot be read right now" in details.text
    assert str(escape(A)) not in details.text
    assert "Review school instructions</a>" not in details.text
    assert week.status_code == 200
    assert "cannot be read right now" in card_for(week.text, ESSAY_ID)
    assert review.status_code == 503
