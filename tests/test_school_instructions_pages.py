"""The school's instructions on the pages: her week's card and an assignment's details show
the school's words apart from anyone's note, those that apply in the one order, and the rest
folded, in the voice of whoever reads them.
"""

import json
import pathlib
import sqlite3
from datetime import UTC, date, datetime

import pytest
from markupsafe import escape

from blossom.reconciliation import SourceChannel
from blossom.routes.navigation import details_href, instructions_review_href
from blossom.school_instructions import InstructionChoice, InstructionSeen
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    ESSAY_ID,
    HER_PAGE,
    HERS,
    PAGE_HEADERS,
    THEIRS,
    card_for,
    client_for,
    school_missing,
    signed_in,
    signed_in_household,
    store_of,
)

A = "Outline three causes before drafting."
B = "Compare two canals in the conclusion."
C = "Bring the map handout."
NOW = datetime(2026, 8, 19, 20, 0, tzinfo=UTC)
TODAY = date(2026, 8, 19)
TYPED = "Ask about the library pass."
FOLD = "Earlier instructions from the school"
SAID = "These are the school's words. Which of them apply is chosen by the family"


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
    assert review.status_code == 500


# ------------------------------------------------------------- everywhere the assignment is listed


def awaiting(store: ProjectStateStore, words: str) -> None:
    """A school note found in the old note field after the upgrade, as the startup rule
    keeps it beside the essay's instructions."""
    revision = store.school_instruction_readings([ESSAY_ID]).readable[ESSAY_ID].revision
    with store._lock, store._writing():
        store._insert_instruction_locked(
            ESSAY_ID,
            InstructionSeen(words, SourceChannel.EMAIL),
            "awaiting",
            revision + 1,
            imported_by=None,
            settled_by=None,
            now=None,
            today=None,
        )


def with_a_parents_note(store: ProjectStateStore) -> None:
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


def the_record(store: ProjectStateStore) -> dict[str, list[tuple[object, ...]]]:
    return {
        name: store._connection.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()  # noqa: S608
        for name in ("assignments", "school_instructions")
    }


@pytest.mark.parametrize("standing", ["applies-with-earlier", "none-applies", "unreadable"])
def test_the_family_rows_show_the_schools_words_as_her_pages_do(
    tmp_path: pathlib.Path, standing: str
) -> None:
    """The essay is on the family page for what the school reports. Its row shows the
    school's words that apply, the rest folded, a parent's note apart, and the family's
    review; or that none applies; or that they cannot be read, with no review. Opening the
    page writes nothing."""
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        signed_in(client, THEIRS)
        store = store_of(client)
        kept(store, A, B, applies=frozenset({A}))
        awaiting(store, C)
        with_a_parents_note(store)
        if standing == "none-applies":
            store.settle_school_instructions(
                ESSAY_ID,
                [],
                InstructionChoice(3, (A, B, C), frozenset(), True),
                authored_by="parent",
                now=NOW,
                today=TODAY,
            )
        if standing == "unreadable":
            store._connection.execute(
                "UPDATE school_instructions SET state = 'bent' WHERE text = ?", (B,)
            )
            store._connection.commit()
        store.record_status_reports(ESSAY_ID, [school_missing(TODAY)])
        before = the_record(store)
        page = client.get("/parent", headers=PAGE_HEADERS).text
        after = the_record(store)

    row = page.split(f'id="update-{ESSAY_ID}"', 1)[1].split('id="update-', 1)[0]
    review = f'<a href="{instructions_review_href(ESSAY_ID)}">Review school instructions</a>'
    assert after == before
    assert f"A parent wrote: <q>{escape(TYPED)}</q>" in row
    if standing == "applies-with-earlier":
        assert f'From the school: <q class="authored-text">{escape(A)}</q>' in row
        folded = row.split(
            "<summary>Instructions from the school waiting for review, and earlier ones</summary>",
            1,
        )[1]
        assert str(escape(B)) in folded
        assert str(escape(C)) in folded
        assert "nor her own updates" in row
        assert review in row
    if standing == "none-applies":
        assert "From the school:" not in row
        assert "No instruction from the school applies now." in row
        assert f"<summary>{FOLD}</summary>" in row
        assert review in row
    if standing == "unreadable":
        assert "cannot be read right now" in row
        assert str(escape(A)) not in row
        assert "Review school instructions" not in row


@pytest.mark.parametrize("standing", ["kept", "unreadable"])
@pytest.mark.parametrize("done", [False, True], ids=["to-do", "reported-done"])
@pytest.mark.parametrize("week", ["this-week", "a-week-before"])
def test_work_due_later_shows_the_schools_words_in_both_lists(
    tmp_path: pathlib.Path, week: str, done: bool, standing: str
) -> None:
    """The essay is assigned in the week shown and due after it, to do or reported done, in
    the week of today or one looked back on. Its row shows the school's words, those that
    apply and the rest folded, or that they cannot be read."""
    assigned, due, looked_at = (
        (date(2026, 8, 18), date(2026, 9, 1), None)
        if week == "this-week"
        else (date(2026, 8, 11), date(2026, 8, 25), "2026-08-10")
    )
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        signed_in(client, HERS)
        store = store_of(client)
        kept(store, A, B, applies=frozenset({A}))
        awaiting(store, C)
        essay = store.one_assignment(ESSAY_ID)
        assert essay is not None
        store.upsert_assignments(
            [essay.model_copy(update={"assigned_on": assigned, "due_date": due})]
        )
        store._connection.execute("DELETE FROM date_claims WHERE assignment_id = ?", (ESSAY_ID,))
        store._connection.commit()
        if done:
            store.report_status(ESSAY_ID, "done", None, expected_head=None, now=NOW, today=TODAY)
        if standing == "unreadable":
            store._connection.execute(
                "UPDATE school_instructions SET state = 'bent' WHERE text = ?", (C,)
            )
            store._connection.commit()
        page = client.get(
            HER_PAGE, params={"week": looked_at} if looked_at else None, headers=PAGE_HEADERS
        ).text

    later = page.split("<h2>Assigned this week, due later</h2>", 1)[1]
    row = card_for(later, ESSAY_ID)
    if standing == "kept":
        assert f'From the school: <q class="authored-text">{escape(A)}</q>' in row
        folded = row.split(
            "<summary>Instructions from the school waiting for review, and earlier ones</summary>",
            1,
        )[1]
        assert str(escape(B)) in folded
        assert str(escape(C)) in folded
        assert "nor your own updates" in row
    else:
        assert "cannot be read right now" in row
        assert str(escape(A)) not in row
    assert "Review school instructions" not in row


def test_a_school_note_no_one_chose_is_shown_apart_and_never_as_applying(
    tmp_path: pathlib.Path,
) -> None:
    """A kept instruction cannot be read, so a school note found in the old note field
    stays there at the start; the page says it apart, as not yet reviewed, and never among
    the words that apply."""
    legacy = "Unreviewed words from before."
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        kept(store_of(client), A, applies=frozenset({A}))
    connection = sqlite3.connect(settings.database_path)
    with connection:
        connection.execute("UPDATE school_instructions SET state = 'bent'")
        connection.execute(
            "UPDATE assignments SET note = ?, origins = ? WHERE assignment_id = ?",
            (legacy, json.dumps({"note": "LMS"}), ESSAY_ID),
        )
    connection.close()
    with client_for(settings) as client:
        signed_in(client, HERS)
        page = client.get(details_href(ESSAY_ID), headers=PAGE_HEADERS).text

    found = f'<q class="authored-text">{escape(legacy)}</q>'
    assert f"Found in the old note field, not yet reviewed: {found}" in page
    assert "it does not apply until a parent reviews it" in page
    assert f'From the school: <q class="authored-text">{escape(legacy)}</q>' not in page
