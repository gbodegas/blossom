# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A transaction no scope of a store owns, met by a press and by a page. The press is refused
where its store would join or commit the transaction: the page says nothing was saved and
gives back what was typed, the transaction stays open with what it holds, and a connection of
its own to the household's file reads what it read before the press. A page whose read block
meets the transaction says what cannot be shown. The fixture week through the app, with the
state forced: a transaction begun by hand on the store's own connection, holding one change.
"""

import logging
import pathlib
import sqlite3
from datetime import date
from typing import Final

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.routes.navigation import week_href
from tests.support import (
    DETAILS,
    ESSAY_ID,
    FIXTURE_WEEK,
    FIXTURES,
    HER_PAGE,
    HERS,
    MISSING_EMAIL,
    PAGE_HEADERS,
    PLAN_DATE,
    READING_LOG_ID,
    REPORT,
    action_of,
    card_for,
    database_of,
    every_row,
    family_page,
    form_fields,
    help_row,
    hidden,
    household_client,
    quiet_client,
    report,
    row_for,
    sign_in_as,
    signed_in,
    signed_in_household,
    state_of,
    store_free_page,
    store_of,
    ways_back_of,
    whole_form,
)

TYPED: Final = "Kept <b>words</b>\nand a second line"
HELD: Final = "held and never committed"
HELD_ID: Final = "f" * 32
TITLE_HELD: Final = "SELECT COUNT(*) FROM assignments WHERE title = ?"
ID_HELD: Final = "SELECT COUNT(*) FROM help_request_ids WHERE request_id = ?"
UPDATE_NOT_SAVED: Final = "Your update could not be saved. Your words are still here. Try again."
CHECK_NOT_SAVED: Final = "The check could not be saved. Nothing was written."
REPORT_NOT_SAVED: Final = "Blossom couldn't save this. Nothing was saved. Try again."
NOT_TAKEN_BACK: Final = "Your request could not be taken back, and nothing was changed. Try again."
ON_HELP_REQUESTS: Final = "A transaction was found left open on the help requests connection"
GRADE_REPORT: Final = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")
REVIEW: Final = "/parent/grades/add/review"
SAVE: Final = "/parent/grades/add/save"
ASKED: Final = "which part?"


def held_on_the_record(client: TestClient) -> sqlite3.Connection:
    """Begin a transaction by hand on the record's connection and leave it open, holding a
    new title for the reading log. The connection."""
    connection = store_of(client)._connection
    connection.execute("BEGIN")
    connection.execute(
        "UPDATE assignments SET title = ? WHERE assignment_id = ?", (HELD, READING_LOG_ID)
    )
    return connection


def held_on_help_requests(client: TestClient) -> sqlite3.Connection:
    """Begin a transaction by hand on the help requests connection and leave it open, holding
    one more used form id. The connection."""
    connection = state_of(client).help_requests._connection
    connection.execute("BEGIN")
    connection.execute("INSERT INTO help_request_ids (request_id) VALUES (?)", (HELD_ID,))
    return connection


def still_holds(connection: sqlite3.Connection, counted: str, value: str) -> bool:
    """Whether the transaction is still open on ``connection`` with its one change in it."""
    found = [tuple(row) for row in connection.execute(counted, (value,))]
    return connection.in_transaction and found == [(1,)]


def read_fresh(path: pathlib.Path, sql: str) -> list[object]:
    """The first column of every row ``sql`` reads, through a connection of its own."""
    connection = sqlite3.connect(path)
    try:
        return [row[0] for row in connection.execute(sql)]
    finally:
        connection.close()


def left_open_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    """Every line the log holds about a transaction left open, with its level."""
    return [
        (entry.levelno, entry.getMessage())
        for entry in caplog.records
        if "left open" in entry.getMessage()
    ]


# ------------------------------------------------------------- four presses in the state


def test_her_update_in_the_state_is_not_saved_and_her_choice_and_words_come_back(
    tmp_path: pathlib.Path,
) -> None:
    with household_client("her", tmp_path) as client:
        sign_in_as(client, "her")
        week = client.get(
            HER_PAGE, params={"week": FIXTURE_WEEK, "change": ESSAY_ID}, headers=PAGE_HEADERS
        ).text
        fields = form_fields(card_for(week, ESSAY_ID), REPORT)
        before = every_row(database_of(client))
        connection = held_on_the_record(client)
        answer = client.post(
            REPORT, data={**fields, "status": "done", "note": TYPED}, headers=PAGE_HEADERS
        )
        held = still_holds(connection, TITLE_HELD, HELD)
        after = every_row(database_of(client))

    main = store_free_page(answer, status=500, heading="Update not saved", alert=UPDATE_NOT_SAVED)
    assert "<p>Your choice: Done.</p>" in main
    assert ">Your note, as typed</label>" in main
    assert f"readonly>\n{escape(TYPED)}</textarea>" in main
    back = week_href(
        date.fromisoformat(FIXTURE_WEEK), ESSAY_ID, show=ESSAY_ID, in_place=fields.get("in_place")
    )
    assert ways_back_of(main) == [(str(escape(back)), "Back to the week")]
    assert held
    assert after == before


def test_a_family_check_in_the_state_is_not_saved_and_the_note_comes_back(
    tmp_path: pathlib.Path,
) -> None:
    with household_client("parent", tmp_path) as client:
        sign_in_as(client, "parent")
        told = client.post("/parent/inbox/keep", data={"text": MISSING_EMAIL})
        assert told.status_code == 303
        sign_in_as(client, "her")
        report(client, ESSAY_ID, "done", "Handed in Tuesday.")
        sign_in_as(client, "parent")
        row = row_for(family_page(client), ESSAY_ID)
        check = {
            "basis": hidden(row, "basis"),
            "expected_check_id": hidden(row, "expected_check_id"),
            "note": TYPED,
        }
        before = every_row(database_of(client))
        connection = held_on_the_record(client)
        answer = client.post(action_of(row, "mark"), data=check, headers=PAGE_HEADERS)
        held = still_holds(connection, TITLE_HELD, HELD)
        after = every_row(database_of(client))

    main = store_free_page(answer, status=500, heading="Family review", alert=CHECK_NOT_SAVED)
    assert ">Your note, as typed</label>" in main
    assert f"readonly>{escape(TYPED)}</textarea>" in main
    assert ways_back_of(main) == [("/parent", "Return to Family review")]
    assert held
    assert after == before


def test_a_pasted_school_result_in_the_state_is_not_saved_and_its_form_comes_back_whole(
    tmp_path: pathlib.Path,
) -> None:
    with household_client("parent", tmp_path) as client:
        sign_in_as(client, "parent")
        review = client.post(REVIEW, data={"report_text": GRADE_REPORT}, headers=PAGE_HEADERS)
        assert review.status_code == 200, review.text[:300]
        form = {**whole_form(review.text, SAVE), "identity": "hers"}
        before = every_row(database_of(client))
        connection = held_on_the_record(client)
        answer = client.post(SAVE, data=form, headers=PAGE_HEADERS)
        held = still_holds(connection, TITLE_HELD, HELD)
        after = every_row(database_of(client))

    assert answer.status_code == 500, (answer.status_code, answer.headers.get("location"))
    assert escape(REPORT_NOT_SAVED) in answer.text
    assert "location" not in answer.headers
    assert whole_form(answer.text, SAVE) == form
    assert held
    assert after == before


def test_taking_back_a_help_request_in_the_state_says_so_in_help_and_the_request_stays(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    with household_client("her", tmp_path) as client:
        sign_in_as(client, "her")
        asked = state_of(client).help_requests.ask(PLAN_DATE, ASKED).request_id
        before = every_row(database_of(client))
        connection = held_on_help_requests(client)
        answer = client.post(f"/student/actions/take-back-help/{asked}", headers=PAGE_HEADERS)
        held = still_holds(connection, ID_HELD, HELD_ID)
        left = read_fresh(database_of(client), "SELECT request_id FROM help_requests")
        after = every_row(database_of(client))

    assert answer.status_code == 500, (answer.status_code, answer.headers.get("location"))
    assert (
        '<p class="problem" role="alert" id="help-problem" tabindex="-1" autofocus>'
        f'{NOT_TAKEN_BACK} <a href="#help-{asked}">Go to the request.</a></p>'
    ) in answer.text
    assert answer.text.count("autofocus") == 1
    assert ASKED in help_row(answer.text, asked)
    assert held
    assert left == [asked]
    assert after == before
    assert left_open_lines(caplog) == [(logging.ERROR, ON_HELP_REQUESTS)]


def test_the_json_take_back_in_the_state_answers_500_and_leaves_everything_as_it_was(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    with quiet_client(signed_in_household(tmp_path)) as client:
        signed_in(client, HERS)
        asked = state_of(client).help_requests.ask(PLAN_DATE, ASKED).request_id
        before = every_row(database_of(client))
        connection = held_on_help_requests(client)
        answer = client.delete(f"/student/help-requests/{asked}")
        held = still_holds(connection, ID_HELD, HELD_ID)
        left = read_fresh(database_of(client), "SELECT request_id FROM help_requests")
        after = every_row(database_of(client))

    assert answer.status_code == 500
    assert answer.text == "Internal Server Error"
    assert held
    assert left == [asked]
    assert after == before
    assert left_open_lines(caplog) == [(logging.ERROR, ON_HELP_REQUESTS)]


# ------------------------------------------------------------- pages whose read block refuses

READ_PAGES: Final = {
    "her week": (
        "her",
        HER_PAGE,
        "This week cannot be shown right now",
        "The record cannot be read right now, so the week, its updates and current date "
        "information are not shown. Try again in a moment. Try again",
    ),
    "Family review": (
        "parent",
        "/parent",
        "Family review",
        "The record cannot be read right now, so reviews, updates and current date information "
        "are not shown. Try again in a moment. Try again",
    ),
    "an assignment's details": (
        "her",
        DETAILS,
        "Assignment",
        "This assignment can't be shown right now. Try again in a moment. Try again",
    ),
}
"""Three of the pages that read the record in a read block: who opens each, its address, and
the heading and words it has when the record can't be read. The other read blocks aren't
covered here."""


@pytest.mark.parametrize("page", list(READ_PAGES))
def test_a_page_whose_read_block_meets_the_state_says_what_cannot_be_shown(
    page: str, tmp_path: pathlib.Path
) -> None:
    reader, address, heading, alert = READ_PAGES[page]
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        before = every_row(database_of(client))
        connection = held_on_the_record(client)
        answer = client.get(address, headers=PAGE_HEADERS)
        held = still_holds(connection, TITLE_HELD, HELD)
        after = every_row(database_of(client))

    store_free_page(answer, status=503, heading=heading, alert=alert)
    assert held
    assert after == before
