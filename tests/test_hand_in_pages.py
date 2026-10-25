# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Turning it in, on the pages: her own account of delivery, kept apart from Done.

The fixture week through the app, a pinned clock, and forms read from the
page's own HTML, so a hidden field the page does not write is never sent.
Nothing here needs a script in the browser, and no model is asked.
"""

import asyncio
import pathlib
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.app import NOTHING_SAVED, create_app
from blossom.hand_in import NEEDS_HAND_IN, TURNED_IN, HandInSaved, HandInState
from blossom.noticing import planning_digest, read_everything, week_from
from blossom.plans import DailyPlan
from blossom.routes import hand_in as hand_in_routes
from blossom.routes import student as student_routes
from blossom.routes.hand_in import (
    CHANGED_WITHOUT_THE_PAGE,
    CHOOSE_A_STATE,
    HAND_IN_CANNOT_UNDO,
    HAND_IN_CHANGED,
    HAND_IN_NOT_SAVED,
    NEXT_ACTION_ONE_LINE,
    NEXT_ACTION_TOO_LONG,
    NOT_A_HAND_IN_OF_THIS,
    TURN_IN_BAD_FORM,
    UNKEPT_CHARACTER,
)
from blossom.routes.parent import ASSIGNMENTS_CHANGED as THEIR_ASSIGNMENTS_CHANGED
from blossom.routes.runs import plan_graphs
from blossom.routes.student import (
    ALREADY_UNDONE,
    ASSIGNMENTS_CHANGED,
    BAD_FORM,
    BAD_RETURN,
    CANNOT_UNDO,
    HAND_IN_ALREADY_SAVED,
    HAND_IN_SAVED,
    HAND_IN_UNDONE,
    NOT_HERS_TO_UPDATE,
    STEP_NOT_SAVED,
    TURN_IN_MEANS,
)
from blossom.stores.project_state import CouldNotSave, ProjectStateStore
from blossom.to_turn_in import TURNED_IN_EARLIER
from tests.support import (
    DETAILS,
    ESSAY_ID,
    ESSAY_TITLE,
    FIXTURE_WEEK,
    HER_PAGE,
    HERS,
    NOT_UTF8,
    PAGE_HEADERS,
    PLAN_DATE,
    QUIZ_ID,
    SAME_ORIGIN,
    THEIRS,
    Answer,
    Scripted,
    a_row,
    accepting,
    as_stored,
    browser,
    card_for,
    first_button_of,
    fixture_week_plan,
    form_fields,
    human_text,
    lands_on,
    plan_form,
    pressed_form,
    report,
    school_missing,
    scripted_graphs,
    signed_in_household,
    spoil,
    state_of,
    store_free_page,
    with_clock,
    words,
)

ACTIONS = f"/student/actions/assignments/{ESSAY_ID}"
FAMILY = "/parent"
NOT_RECORDED = "Hand-in status not recorded."
QUESTION = "Has this been turned in?"
STEP_LABEL = "Your next step (optional)"
STEP_HELP = "This appears on your To turn in list. For example: Put it in my blue folder."
STAYS = "This stays in Blossom. It does not send work to school or send an alert."
DISCREPANCY = "The school reports this missing, and {who} reported it turned in."


def section(page: str) -> str:
    """The Turning it in section of a details page, from the section around its heading."""
    start = page.rindex("<section", 0, page.index('id="turning-it-in"'))
    return page[start : page.index("</section>", start)]


def opened(client: TestClient, **query: str) -> str:
    """The details with the hand-in form open, as its own link opens it."""
    shown = client.get(DETAILS, params={"hand_in": "change", **query})
    assert shown.status_code == 200
    return shown.text


def save(
    client: TestClient,
    page: str,
    state: str | None,
    *,
    action: str = "",
    note: str = "",
    **over: str,
) -> Answer:
    """Send the hand-in form of a details page as the page wrote it, with her choices."""
    fields = form_fields(page, f"{ACTIONS}/hand-in")
    chosen = {} if state is None else {"state": state}
    return client.post(
        f"{ACTIONS}/hand-in",
        data={**fields, **chosen, "next_action": action, "note": note, **over},
        headers=PAGE_HEADERS,
    )


def landed(client: TestClient, answer: Answer) -> str:
    assert answer.status_code == 303, answer.text
    return client.get(answer.headers["location"]).text


def chain(client: TestClient) -> list[str]:
    store: ProjectStateStore = state_of(client).project_state
    return [event.event_id for event in store.hand_in_chains([ESSAY_ID]).get(ESSAY_ID, [])]


# ----------------------------------------------------------- what the pages show


def test_details_say_nothing_is_recorded_until_she_says_something_and_offer_the_form() -> None:
    with browser() as client:
        page = client.get(DETAILS).text
        turning = section(page)

        assert "<h2" in turning
        assert "Turning it in" in turning
        assert NOT_RECORDED in turning
        assert "Not sure" not in turning
        assert 'action="' + f"{ACTIONS}/hand-in" + '"' not in turning
        assert turning.count(">I turned it in</button>") == 1
        assert ">Other turn-in options</a>" in turning
        assert turning.count(TURN_IN_MEANS) == 1
        assert (
            page.index("Your update")
            < page.index('id="turning-it-in"')
            < page.index('id="evidence"')
        )

        form = section(opened(client))
        assert "checked" not in form
        for label in ("Still to turn in", "Turned in", "Nothing to turn in", "Not sure"):
            assert label in form
        assert "Save hand-in update" in form
        assert QUESTION in form
        assert STAYS in form
        assert chain(client) == []


def test_a_first_save_lands_on_its_result_with_the_statement_the_action_and_the_way_back() -> None:
    with browser() as client:
        page = opened(client, return_to="week", week=FIXTURE_WEEK)
        answer = save(
            client,
            page,
            NEEDS_HAND_IN,
            action="Put the essay in my return folder",
            note="by Friday",
        )

        assert answer.status_code == 303
        where = answer.headers["location"]
        assert where.endswith(f"#hand-in-result-{ESSAY_ID}")
        assert "return_to=week" in where
        shown = section(client.get(where).text)
        assert HAND_IN_SAVED in shown
        assert "Back to the week" in shown
        assert "Still to turn in" in shown
        assert "You reported Still to turn in on August 19, 2026." in shown
        assert "Put the essay in my return folder" in shown
        assert "by Friday" in shown
        assert "Shown in Blossom" in shown
        assert "Other turn-in options" in shown
        assert "Undo" in shown
        assert len(chain(client)) == 1


def test_with_no_next_action_the_fixed_one_is_shown_and_nothing_is_stored_for_it() -> None:
    with browser() as client:
        shown = section(landed(client, save(client, opened(client), NEEDS_HAND_IN)))

        assert "Turn it in." in shown
        store = state_of(client).project_state
        assert store.hand_in_chains([ESSAY_ID])[ESSAY_ID][0].next_action is None


@pytest.mark.parametrize(
    ("state", "sentence"),
    [
        (TURNED_IN, "You reported it turned in on August 19, 2026."),
        ("not_required", "You reported nothing to turn in on August 19, 2026."),
        ("unknown", "You reported this on August 19, 2026."),
    ],
)
def test_each_other_state_has_its_own_dated_sentence_and_keeps_a_note(
    state: str, sentence: str
) -> None:
    with browser() as client:
        shown = section(landed(client, save(client, opened(client), state, note="I think so")))

        assert sentence in shown
        assert "I think so" in shown
        assert NOT_RECORDED not in shown
        if state == "unknown":
            assert "Hand-in status: Not sure" in shown


def test_the_same_words_again_are_already_saved_with_what_stands_and_write_nothing() -> None:
    with browser() as client:
        first = opened(client)
        landed(client, save(client, first, TURNED_IN, note="on Monday"))

        again = save(client, first, TURNED_IN, note=" on Monday\r\n")

        shown = section(landed(client, again))
        assert HAND_IN_ALREADY_SAVED in shown
        assert "You reported it turned in on August 19, 2026." in shown
        assert len(chain(client)) == 1


def test_change_opens_the_form_with_what_stands_and_an_action_left_behind_is_not_kept() -> None:
    with browser() as client:
        landed(client, save(client, opened(client), NEEDS_HAND_IN, action="Put it in my folder"))

        form = section(opened(client))
        assert 'value="needs_hand_in" checked' in form
        assert "Put it in my folder" in form
        shown = section(landed(client, save(client, form, TURNED_IN, action="Put it in my folder")))

        assert "You reported it turned in on August 19, 2026." in shown
        assert "Put it in my folder" not in shown[: shown.index("Hand-in history")]
        assert "Put it in my folder" in shown[shown.index("Hand-in history") :]
        store = state_of(client).project_state
        assert store.hand_in_chains([ESSAY_ID])[ESSAY_ID][-1].next_action is None


def test_undo_restores_what_stood_and_a_repeat_says_it_was_already_undone() -> None:
    with browser() as client:
        landed(client, save(client, opened(client), NEEDS_HAND_IN, action="Put it in my folder"))
        turned_in = landed(client, save(client, opened(client), TURNED_IN))
        undo = form_fields(section(turned_in), f"{ACTIONS}/undo-hand-in")

        first = client.post(f"{ACTIONS}/undo-hand-in", data=undo, headers=PAGE_HEADERS)
        replay = client.post(f"{ACTIONS}/undo-hand-in", data=undo, headers=PAGE_HEADERS)

        shown = section(landed(client, first))
        assert HAND_IN_UNDONE in shown
        assert "You reported Still to turn in on August 19, 2026." in shown
        assert "Put it in my folder" in shown
        assert replay.status_code == 409
        assert escape(ALREADY_UNDONE) in replay.text
        assert "You reported Still to turn in" in section(replay.text)
        assert len(chain(client)) == 3


def test_an_undo_of_something_she_has_since_changed_is_only_a_change() -> None:
    with browser() as client:
        first = landed(client, save(client, opened(client), TURNED_IN))
        undo = form_fields(section(first), f"{ACTIONS}/undo-hand-in")
        landed(client, save(client, opened(client), "not_required"))

        stale = client.post(f"{ACTIONS}/undo-hand-in", data=undo, headers=PAGE_HEADERS)

        assert stale.status_code == 409
        assert escape(HAND_IN_CANNOT_UNDO) in stale.text
        assert escape(ALREADY_UNDONE) not in stale.text
        assert len(chain(client)) == 2


def test_a_save_from_a_page_that_is_behind_shows_what_stands_and_keeps_her_words() -> None:
    with browser() as client:
        behind = opened(client)
        landed(client, save(client, behind, TURNED_IN, note="from the other device"))

        refused = save(client, behind, NEEDS_HAND_IN, action="Hand it to her", note="my words")

        assert refused.status_code == 409
        assert escape(HAND_IN_CHANGED) in refused.text
        shown = section(refused.text)
        assert "You reported it turned in on August 19, 2026." in shown
        assert "from the other device" in shown
        assert "Your unsaved update" in shown
        assert 'value="needs_hand_in" checked' in shown
        assert "Hand it to her" in shown
        assert "my words" in shown
        assert len(chain(client)) == 1
        again = save(client, shown, NEEDS_HAND_IN, action="Hand it to her", note="my words")
        assert again.status_code == 303


# ----------------------------------------------------- one way in, and one question


def test_one_button_opens_the_form_and_opening_or_keeping_it_writes_nothing() -> None:
    with browser() as client:
        page = section(client.get(DETAILS, params={"return_to": "week", "week": FIXTURE_WEEK}).text)
        openers = re.findall(r'<form method="get"[^>]*>.*?</form>', page, re.S)
        from_a_saved_address = section(client.get(DETAILS, params={"hand_in": "remember"}).text)
        form = section(opened(client, return_to="week", week=FIXTURE_WEEK))
        keep = re.search(r'<a class="cancel" href="([^"]+)"', form)
        assert keep is not None
        kept = section(client.get(keep.group(1).replace("&amp;", "&")).text)

        assert lands_on(page, "#turning-it-in") == (
            '<h2 class="update-heading" id="turning-it-in" tabindex="-1">'
        )
        assert openers == []
        assert ">Other turn-in options</a>" in page
        assert "hand_in=change" in page
        assert "remember" not in page
        for shown in (form, from_a_saved_address):
            assert QUESTION in shown
            assert " checked" not in shown
            assert ">I turned it in</button>" not in shown
        assert QUESTION not in kept
        assert ">Other turn-in options</a>" in kept
        assert chain(client) == []


def test_the_form_asks_one_question_and_says_what_the_step_is_before_the_field() -> None:
    with browser() as client:
        form = section(opened(client))
        landed(client, save(client, form, NEEDS_HAND_IN, note="in the folder"))
        with_a_note = section(opened(client))

    legend = re.search(r"<legend>(.*?)</legend>", form, re.S)
    assert legend is not None
    assert words(legend.group(1)).startswith(QUESTION)
    answers = re.findall(r'name="state" value="(\w+)"', form)
    assert answers == [NEEDS_HAND_IN, TURNED_IN, "not_required", "unknown"]
    start = form.index('<div class="next-step">')
    step = form[start : form.index("</div>", start)]
    assert step.index(STEP_LABEL) < step.index(STEP_HELP) < step.index('name="next_action"')
    assert "A next step is saved only with Still to turn in." in step
    assert f'aria-describedby="next-action-hint-{ESSAY_ID} next-action-limit-{ESSAY_ID}"' in step
    note = form[form.index('<details class="steps note-fold">') :]
    assert note.index("<summary>Add a note (optional)") < note.index('name="note"')
    assert "Your parents can read this. It is not shared with the planning model." in note
    assert '<details class="steps note-fold" open>' in with_a_note
    assert form.index('name="note"') < form.index(STAYS) < form.index("Save hand-in update")


@pytest.mark.parametrize("state", [TURNED_IN, "not_required", "unknown"])
def test_a_step_typed_beside_another_answer_is_not_saved_and_the_result_says_so(
    state: str,
) -> None:
    with browser() as client:
        result = section(landed(client, save(client, opened(client), state, action="Blue folder")))
        again = section(landed(client, save(client, opened(client), state, action="Blue folder")))
        stored = state_of(client).project_state.hand_in_chains([ESSAY_ID])[ESSAY_ID]

    assert f"{HAND_IN_SAVED} {STEP_NOT_SAVED}" in result
    assert f"{HAND_IN_ALREADY_SAVED} {STEP_NOT_SAVED}" in again
    assert "Blue folder" not in result
    assert [event.next_action for event in stored] == [None]


@pytest.mark.parametrize(
    ("state", "action"),
    [(NEEDS_HAND_IN, "Blue folder"), (NEEDS_HAND_IN, ""), (TURNED_IN, ""), (TURNED_IN, "   ")],
    ids=["a step with its answer", "no step", "another answer, no step", "only spaces"],
)
def test_a_save_with_no_step_left_behind_says_nothing_of_one(state: str, action: str) -> None:
    with browser() as client:
        result = section(landed(client, save(client, opened(client), state, action=action)))

    assert HAND_IN_SAVED in result
    assert STEP_NOT_SAVED not in result


def test_a_step_typed_beside_another_answer_stays_in_the_form_when_the_save_is_refused() -> None:
    with browser() as client:
        answer = save(client, opened(client), TURNED_IN, action="Blue folder", note="x" * 501)

        assert answer.status_code == 422
        shown = section(answer.text)
        assert 'value="turned_in" checked' in shown
        assert 'name="next_action" value="Blue folder"' in shown
        assert '<details class="steps note-fold" open>' in shown
        assert chain(client) == []


@pytest.mark.parametrize("reader", ["her", "a parent"])
def test_turned_in_beside_a_school_missing_is_shown_as_the_two_reports(
    reader: str, tmp_path: pathlib.Path
) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        landed(client, save(client, opened(client), TURNED_IN))
        store = state_of(client).project_state
        store.record_status_reports(ESSAY_ID, [school_missing(PLAN_DATE)])
        if reader == "a parent":
            client.post("/sign-out")
            client.post("/sign-in", data={"passphrase": THEIRS})
        page = client.get(DETAILS).text
        stands = store.hand_in_chains([ESSAY_ID])[ESSAY_ID]

    turning = words(section(page))
    who = "you" if reader == "her" else "she"
    assert DISCREPANCY.format(who=who) in turning
    assert "Blossom keeps both as they are, so the family can check." in turning
    assert "The school reports this missing." in words(page[page.index('id="evidence"') :])
    assert [event.state for event in stands] == [TURNED_IN]


@pytest.mark.parametrize("work", [None, "not_yet", "done"])
@pytest.mark.parametrize("state", [NEEDS_HAND_IN, TURNED_IN, "not_required", "unknown"])
def test_each_answer_stands_beside_any_work_update_and_neither_save_changes_the_other(
    work: str | None, state: str
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        if work is not None:
            report(client, ESSAY_ID, work)
        works = store.student_report_chains([ESSAY_ID]).get(ESSAY_ID, [])
        landed(client, save(client, opened(client), state))
        hand_ins = chain(client)
        after_hand_in = store.student_report_chains([ESSAY_ID]).get(ESSAY_ID, [])
        report(client, ESSAY_ID, "not_yet" if work == "done" else "done")
        after_work = chain(client)
        page = section(client.get(DETAILS).text)

    assert after_hand_in == works
    assert after_work == hand_ins
    assert len(hand_ins) == 1
    assert {
        NEEDS_HAND_IN: "You reported Still to turn in",
        TURNED_IN: "You reported it turned in",
        "not_required": "You reported nothing to turn in",
        "unknown": "Hand-in status: Not sure",
    }[state] in page


# ------------------------------------------------------------- forms that are refused


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"state": None}, CHOOSE_A_STATE),
        ({"state": "handed_over"}, CHOOSE_A_STATE),
        ({"state": NEEDS_HAND_IN, "action": "x" * 201}, NEXT_ACTION_TOO_LONG),
        ({"state": NEEDS_HAND_IN, "action": "one\ntwo"}, NEXT_ACTION_ONE_LINE),
        ({"state": TURNED_IN, "note": "bell \x07 here"}, UNKEPT_CHARACTER),
        ({"state": TURNED_IN, "note": "x" * 501}, "Keep your note to 500 characters or fewer."),
    ],
    ids=["nothing chosen", "no such state", "long action", "two lines", "a control", "long note"],
)
def test_a_field_the_record_will_not_keep_is_said_beside_it_with_her_words_kept(
    fields: dict[str, str | None], problem: str
) -> None:
    with browser() as client:
        answer = save(
            client,
            opened(client),
            fields["state"],
            action=fields.get("action") or "",
            note=fields.get("note") or "kept words",
        )

        assert answer.status_code == 422
        assert escape(problem) in answer.text
        shown = section(answer.text)
        if problem != CHOOSE_A_STATE:
            assert 'aria-invalid="true"' in shown
        if fields.get("note") is None:
            assert "kept words" in shown
        assert chain(client) == []


def test_a_form_that_is_not_whole_or_names_another_way_back_writes_nothing() -> None:
    with browser() as client:
        page = opened(client)
        fields = form_fields(page, f"{ACTIONS}/hand-in")

        twice = client.post(
            f"{ACTIONS}/hand-in",
            content="state=turned_in&state=unknown&next_action=&note=&expected_hand_in_id=",
            headers={**PAGE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
        extra = save(client, page, TURNED_IN, role="parent")
        elsewhere = save(client, page, TURNED_IN, return_to="https://example.test/")
        foreign = save(client, page, TURNED_IN, expected_hand_in_id="hand-in-not-this-one")

        assert fields["expected_hand_in_id"] == ""
        assert (twice.status_code, extra.status_code) == (422, 422)
        assert escape(BAD_FORM) in twice.text
        assert elsewhere.status_code == 422
        assert escape(BAD_RETURN) in elsewhere.text
        assert foreign.status_code == 422
        assert escape(NOT_A_HAND_IN_OF_THIS) in foreign.text
        assert chain(client) == []


def test_an_assignment_that_is_not_on_record_is_said_plainly_and_nothing_is_written() -> None:
    with browser() as client:
        gone = client.post(
            "/student/actions/assignments/assignment-not-there/hand-in",
            data={"state": TURNED_IN, "next_action": "", "note": "mine", "expected_hand_in_id": ""},
            headers=PAGE_HEADERS,
        )

        assert gone.status_code == 404
        assert "This assignment is not on record now." in gone.text
        assert state_of(client).project_state.hand_in_chains() == {}


def test_a_write_the_file_refuses_keeps_her_words_and_says_nothing_of_a_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        page = opened(client)
        store = state_of(client).project_state

        def refuses(*args: object) -> None:
            msg = "the chain forked"
            raise RuntimeError(msg)

        monkeypatch.setattr(store, "_confirm_hand_in_head_locked", refuses)
        answer = save(client, page, NEEDS_HAND_IN, action="Hand it to her", note="my words")

        assert answer.status_code == 500
        assert escape(HAND_IN_NOT_SAVED) in answer.text
        assert HAND_IN_SAVED not in answer.text
        assert "my words" in answer.text
        assert "Hand it to her" in answer.text
        monkeypatch.undo()
        assert chain(client) == []


def test_a_chain_that_does_not_hold_is_said_to_be_unreadable_and_never_not_recorded() -> None:
    with browser() as client:
        landed(client, save(client, opened(client), TURNED_IN))
        store = state_of(client).project_state
        store._connection.execute("UPDATE hand_in_events SET previous_event_id = 'no-such-event'")
        store._connection.commit()

        details = client.get(DETAILS)
        week = client.get(HER_PAGE, params={"week": FIXTURE_WEEK})
        family = client.get(FAMILY)

        assert details.status_code == 200
        turning = section(details.text)
        assert "cannot be read" in turning
        assert NOT_RECORDED not in turning
        assert f"{ACTIONS}/hand-in" not in turning
        assert "cannot be read" in card_for(week.text, ESSAY_ID)
        assert NOT_RECORDED not in card_for(week.text, ESSAY_ID)
        assert family.status_code == 200
        assert "cannot be read" in family.text


# ------------------------------------------------------------------ who may write


def test_a_parent_reads_it_and_cannot_write_it_and_a_forged_role_changes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        page = opened(client)
        landed(client, save(client, page, NEEDS_HAND_IN, action="Put it in my folder", note="mine"))
        event_id = chain(client)[0]
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})

        read = section(client.get(DETAILS, params={"hand_in": "change"}).text)
        refused = save(client, page, TURNED_IN)
        undo = client.post(
            f"{ACTIONS}/undo-hand-in", data={"hand_in_id": event_id}, headers=PAGE_HEADERS
        )

        assert "She reported Still to turn in on August 19, 2026." in read
        assert "Put it in my folder" in read
        assert "Sign in as the student to update." in read
        assert f"{ACTIONS}/hand-in" not in read
        assert "<button" not in read
        assert QUESTION not in read
        assert (refused.status_code, undo.status_code) == (403, 403)
        assert escape(NOT_HERS_TO_UPDATE) in refused.text
        assert len(chain(client)) == 1


def test_a_device_with_no_sign_in_and_a_request_from_elsewhere_write_nothing(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(signed_in_household(tmp_path))
    data = {"state": TURNED_IN, "next_action": "", "note": "", "expected_hand_in_id": ""}
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as anonymous:
        unsigned = anonymous.post(f"{ACTIONS}/hand-in", data=data, headers=PAGE_HEADERS)
        unsigned_undo = anonymous.post(
            f"{ACTIONS}/undo-hand-in", data={"hand_in_id": "hand-in-any"}, headers=PAGE_HEADERS
        )
        for answer in (unsigned, unsigned_undo):
            assert answer.status_code == 303
            assert answer.headers["location"].startswith("/sign-in")
        assert state_of(anonymous).project_state.hand_in_chains() == {}
    with TestClient(app, follow_redirects=False) as elsewhere:
        refused = elsewhere.post(
            f"{ACTIONS}/hand-in",
            data=data,
            headers={**PAGE_HEADERS, "Origin": "https://example.test"},
        )
        assert refused.status_code == 403


# --------------------------------------------------- her week, the family page, the plan


def test_her_card_offers_i_turned_it_in_and_one_press_records_it() -> None:
    """The card says nothing is recorded and offers one press beside the way to the other
    choices. Done writes no hand-in report; the press writes one, turned in with no step and
    no note, and lands on the card's line that says so, with Undo for that report."""
    with browser() as client:
        page = client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text
        before = card_for(page, ESSAY_ID)
        where = report(client, ESSAY_ID, "done")
        after_done = card_for(client.get(where).text, ESSAY_ID)
        done_chain = chain(client)
        week = client.get(where).text
        action, sent = pressed_form(card_for(week, ESSAY_ID), "hand_in_head")
        answer = client.post(action, data=sent, headers=PAGE_HEADERS)
        result = landed(client, answer)
        store = state_of(client).project_state
        events = store.hand_in_chains([ESSAY_ID])[ESSAY_ID]
        reports = store.student_reports(ESSAY_ID)

    assert NOT_RECORDED in before
    assert ">I turned it in</button>" in before
    assert ">Other turn-in options</a>" in before
    assert before.index(">Your work<") < before.index(">Turning it in<")
    assert f'action="{ACTIONS}/hand-in"' not in before
    assert "Want to keep track of turning it in?" not in after_done
    assert "Your update is saved." in after_done
    assert ">I turned it in</button>" in after_done
    assert done_chain == []
    assert action == f"{ACTIONS}/turned-in"
    assert answer.headers["location"].endswith(f"#hand-in-result-{ESSAY_ID}")
    assert "turn_in_said=turned_in" in answer.headers["location"]
    assert [(event.state, event.next_action, event.note) for event in events] == [
        (TURNED_IN, None, None)
    ]
    assert f"turn_in_event={events[0].event_id}" in answer.headers["location"]
    assert [report.status for report in reports] == ["done"]
    card = card_for(result, ESSAY_ID)
    assert "You reported this turned in." in card
    assert "You reported it turned in on August 19." in card
    assert f'name="undo_hand_in" value="{events[0].event_id}"' in card
    assert ">I turned it in</button>" not in card
    assert ">Change</a>" in card


def test_after_done_the_section_asks_nothing_and_done_writes_no_delivery() -> None:
    """Done on the details is saved and that is all: no offer beside the result, and the
    section offers its one press and the way to the other choices, as before Done."""
    with browser() as client:
        page = client.get(DETAILS, params={"return_to": "week", "week": FIXTURE_WEEK}).text
        fields = form_fields(page, f"{ACTIONS}/report")
        answer = client.post(
            f"{ACTIONS}/report", data={**fields, "status": "done", "note": ""}, headers=PAGE_HEADERS
        )
        result = landed(client, answer)
        events = chain(client)

    assert events == []
    assert "Your update is saved." in result
    assert "Want to keep track of turning it in?" not in result
    assert NOT_RECORDED in section(result)
    assert section(result).count(">I turned it in</button>") == 1
    assert result.count(">Other turn-in options</a>") == 1
    assert "hand_in=change" not in result[: result.index('id="turning-it-in"')]


def test_the_family_page_lists_what_she_said_and_offers_no_way_to_say_it_for_her() -> None:
    with browser() as client:
        assert "Turning work in" not in client.get(FAMILY).text
        landed(client, save(client, opened(client), NEEDS_HAND_IN, action="Put it in my folder"))

        family = client.get(FAMILY).text

        start = family.index('id="turning-work-in"')
        listed = family[start : family.index("</section>", start)]
        assert ESSAY_TITLE in listed
        assert "She reported Still to turn in on August 19" in listed
        assert "Put it in my folder" in listed
        assert "return_to=family" in listed
        assert not [
            action for action in re.findall(r'action="([^"]+)"', family) if "hand-in" in action
        ]


def test_the_family_list_keeps_its_order_its_window_and_its_unreadable_rows_last() -> None:
    """Six assignments, said about on chosen days, read on the pinned day, August 19.

    Still to turn in comes first, the longest standing first, however old, and
    an edit inside that state does not move a row down. Whatever else she said
    follows while her latest word is inside fourteen days, the latest first by
    the order the file kept and never by the clock: August 6 is inside and
    August 5 is not. A record that cannot be read comes last.
    """
    names = ["old-wait", "new-wait", "edge-in", "recent", "edge-out", "broken"]
    with browser(BLOSSOM_FIXTURE_PATH="") as client:
        store = state_of(client).project_state
        store.put_on_record([a_row(name, f"Work {name}") for name in names], {})

        def said(name: str, state: HandInState, on: date, head: str | None = None) -> str:
            at = datetime(on.year, on.month, on.day, 21, 0, tzinfo=UTC)
            kept = store.record_hand_in(
                name, state, None, None, expected_head=head, now=at, today=on
            )
            assert isinstance(kept, HandInSaved)
            return kept.event.event_id

        first = said("old-wait", NEEDS_HAND_IN, date(2026, 7, 1))
        said("edge-out", TURNED_IN, date(2026, 8, 5))
        said("recent", "not_required", date(2026, 8, 18))
        said("edge-in", TURNED_IN, date(2026, 8, 6))
        said("new-wait", NEEDS_HAND_IN, date(2026, 8, 17))
        said("broken", TURNED_IN, date(2026, 8, 19))
        kept = store.record_hand_in(
            "old-wait",
            NEEDS_HAND_IN,
            "Put it in my folder",
            None,
            expected_head=first,
            now=datetime(2026, 8, 19, 21, 0, tzinfo=UTC),
            today=date(2026, 8, 19),
        )
        assert isinstance(kept, HandInSaved)
        store._connection.execute(
            "UPDATE hand_in_events SET state = 'invalid-state' WHERE assignment_id = 'broken'"
        )
        store._connection.commit()

        family = client.get(FAMILY).text

    start = family.index('id="turning-work-in"')
    listed = family[start : family.index("</section>", start)]
    order = re.findall(r'<article class="draft" id="(?:update|hand-in)-([^"]+)"', listed)
    assert order == ["old-wait", "new-wait", "edge-in", "recent", "broken"]
    assert "edge-out" not in listed
    assert "She reported Still to turn in on July 1, 2026." in listed
    assert "cannot be read" in listed[listed.index("-broken") :]


def test_her_two_accounts_stay_apart_on_the_page_and_a_plan_is_not_made_stale() -> None:
    with browser() as client:
        store = state_of(client).project_state
        before = planning_digest(week_from(read_everything(store, store), PLAN_DATE))
        report(client, ESSAY_ID, "not_yet")
        after_work = planning_digest(week_from(read_everything(store, store), PLAN_DATE))

        landed(client, save(client, opened(client), TURNED_IN, note="ZEBRA-FOLDER"))

        page = client.get(DETAILS).text
        assert "Not yet" in page
        assert "You reported it turned in on August 19, 2026." in section(page)
        assert planning_digest(week_from(read_everything(store, store), PLAN_DATE)) == after_work
        assert before != after_work
        assert store.student_report_chains([ESSAY_ID])[ESSAY_ID][-1].status == "not_yet"


def test_nothing_she_says_about_turning_work_in_reaches_a_planner_or_stales_a_plan() -> None:
    """Said before the plan is asked for and again while it waits: no brief holds a word of
    it, the plan is made from the same work, and the waiting plan is not called changed."""
    planners: list[Scripted[DailyPlan]] = []
    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [fixture_week_plan()], lambda: [accepting()], planners=planners
        )
        landed(
            client,
            save(client, opened(client), NEEDS_HAND_IN, action="ZEBRA-FOLDER", note="ZEBRA-NOTE"),
        )

        assert client.post("/student/actions/plan", data=plan_form(client)).status_code == 303
        waiting = state_of(client).drafts.latest_for(PLAN_DATE)
        landed(client, save(client, opened(client), TURNED_IN, note="ZEBRA-LATER"))
        week = client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text
        family = client.get(FAMILY).text

    assert waiting is not None
    assert [planner.calls for planner in planners] == [1]
    sent = " ".join(str(message.content) for brief in planners[0].briefs for message in brief)
    assert "ZEBRA" not in sent
    brief = human_text(planners[0].briefs[0]).lower()
    assert not [
        words for words in ("hand-in", "hand in", "still to turn in", "turned in") if words in brief
    ]
    assert ASSIGNMENTS_CHANGED not in week
    assert THEIR_ASSIGNMENTS_CHANGED not in family


def test_the_existing_undo_of_her_work_update_says_when_it_was_already_undone() -> None:
    with browser() as client:
        where = report(client, ESSAY_ID, "done")
        card = card_for(client.get(where).text, ESSAY_ID)
        undo = form_fields(card, f"{ACTIONS}/undo-report")

        first = client.post(f"{ACTIONS}/undo-report", data=undo, headers=PAGE_HEADERS)
        replay = client.post(f"{ACTIONS}/undo-report", data=undo, headers=PAGE_HEADERS)
        report(client, ESSAY_ID, "not_yet")
        later = client.post(f"{ACTIONS}/undo-report", data=undo, headers=PAGE_HEADERS)

        assert first.status_code == 303
        assert replay.status_code == 409
        assert escape(ALREADY_UNDONE) in replay.text
        assert later.status_code == 409
        assert escape(CANNOT_UNDO) in later.text
        assert escape(ALREADY_UNDONE) not in later.text


# ------------------------------------------- a record that cannot be read, and her words

MALFORMED = {
    "a state that is none of the four": ("state", "invalid-state"),
    "a day that is no day": ("reported_on", "not-a-day"),
    "a note past the limit": ("note", "x" * 501),
    "text that is not UTF-8": ("note", NOT_UTF8),
}


def damage(client: TestClient, column: str, value: str) -> list[tuple[object, ...]]:
    """Spoil the essay's one stored event with plain SQL, and hand back the table as it is."""
    return spoil(state_of(client).project_state, "hand_in_events", column, value)


def rows(client: TestClient) -> list[tuple[object, ...]]:
    return as_stored(state_of(client).project_state, "hand_in_events")


@pytest.mark.parametrize("where", [DETAILS, f"{HER_PAGE}?week={FIXTURE_WEEK}", FAMILY])
@pytest.mark.parametrize("spoiled", MALFORMED.values(), ids=MALFORMED.keys())
def test_a_row_that_cannot_be_decoded_makes_that_record_unreadable_and_breaks_no_page(
    spoiled: tuple[str, str], where: str
) -> None:
    with browser() as client:
        landed(client, save(client, opened(client), TURNED_IN, note="kept as written"))
        before = damage(client, *spoiled)

        shown = client.get(where)

        assert shown.status_code == 200
        assert "cannot be read" in shown.text
        if where == DETAILS:
            turning = section(shown.text)
            assert NOT_RECORDED not in turning
            assert f"{ACTIONS}/hand-in" not in turning
            assert "undo-hand-in" not in turning
        assert rows(client) == before


def test_beside_an_unreadable_record_another_assignment_reads_and_saves_as_usual() -> None:
    quiz = f"/student/assignments/{QUIZ_ID}"
    with browser() as client:
        landed(client, save(client, opened(client), TURNED_IN))
        damage(client, "state", "invalid-state")

        page = client.get(quiz, params={"hand_in": "change"}).text
        fields = form_fields(page, f"/student/actions/assignments/{QUIZ_ID}/hand-in")
        answer = client.post(
            f"/student/actions/assignments/{QUIZ_ID}/hand-in",
            data={**fields, "state": NEEDS_HAND_IN, "next_action": "", "note": ""},
            headers=PAGE_HEADERS,
        )
        week = client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text

        assert NOT_RECORDED in section(page)
        assert answer.status_code == 303
        assert "Still to turn in" in card_for(week, QUIZ_ID)
        assert "cannot be read" in card_for(week, ESSAY_ID)


@pytest.mark.parametrize("spoiled", MALFORMED.values(), ids=MALFORMED.keys())
def test_nothing_is_saved_or_undone_on_a_record_that_cannot_be_decoded(
    spoiled: tuple[str, str],
) -> None:
    with browser() as client:
        page = opened(client)
        first = landed(client, save(client, page, TURNED_IN))
        undo = form_fields(section(first), f"{ACTIONS}/undo-hand-in")
        behind = opened(client)
        before = damage(client, *spoiled)

        saved_over = save(client, behind, NEEDS_HAND_IN, note="mine")
        undone = client.post(f"{ACTIONS}/undo-hand-in", data=undo, headers=PAGE_HEADERS)

        assert (saved_over.status_code, undone.status_code) == (500, 500)
        assert HAND_IN_SAVED not in saved_over.text
        assert HAND_IN_UNDONE not in undone.text
        assert rows(client) == before


def test_a_plan_is_still_made_beside_a_record_that_cannot_be_decoded() -> None:
    planners: list[Scripted[DailyPlan]] = []
    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [fixture_week_plan()], lambda: [accepting()], planners=planners
        )
        landed(client, save(client, opened(client), TURNED_IN, note="ZEBRA-NOTE"))
        damage(client, "reported_on", "not-a-day")

        made = client.post("/student/actions/plan", data=plan_form(client))

    assert made.status_code == 303
    sent = " ".join(str(message.content) for brief in planners[0].briefs for message in brief)
    assert "ZEBRA" not in sent
    assert ESSAY_TITLE in sent


TYPED_STEP = "MY <b>UNSAVED</b> STEP \U0001f642"
TYPED_NOTE = "MY UNSAVED WORDS\nsecond line <script>alert(1)</script> \U0001f642"


@pytest.mark.parametrize(
    "spoiled",
    [("previous_event_id", "no-such-event"), ("state", "invalid-state")],
    ids=["a link that leads nowhere", "a row that cannot be decoded"],
)
def test_a_save_refused_over_an_unreadable_record_still_shows_every_word_she_sent(
    spoiled: tuple[str, str],
) -> None:
    """The reread succeeds and finds the record unreadable, so no form is there to hold them."""
    with browser() as client:
        landed(client, save(client, opened(client), TURNED_IN))
        behind = opened(client)
        before = damage(client, *spoiled)

        answer = save(client, behind, NEEDS_HAND_IN, action=TYPED_STEP, note=TYPED_NOTE)

        assert answer.status_code == 500
        turning = section(answer.text)
        assert escape(HAND_IN_NOT_SAVED) in answer.text
        assert "cannot be read" in turning
        assert "Your unsaved hand-in update" in turning
        assert "Still to turn in" in turning
        assert str(escape(TYPED_STEP)) in turning
        assert str(escape(TYPED_NOTE)) in turning
        assert "<script>alert(1)</script>" not in answer.text
        assert "readonly" in turning
        assert f"{ACTIONS}/hand-in" not in turning
        assert "undo-hand-in" not in turning
        assert rows(client) == before


# ------------------------------------------------ a form refused for something else


def malformed(client: TestClient, page: str, how: str) -> Answer:
    fields = form_fields(page, f"{ACTIONS}/hand-in")
    sent = {**fields, "state": TURNED_IN, "next_action": "kept step", "note": "first note"}
    url = f"{ACTIONS}/hand-in"
    if how == "a note sent twice":
        body = "&".join(f"{name}={value}" for name, value in sent.items()) + "&note=second+note"
        return client.post(
            url,
            content=body.replace(" ", "+"),
            headers={**PAGE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
    if how == "a field this page does not send":
        return client.post(url, data={**sent, "role": "parent"}, headers=PAGE_HEADERS)
    if how == "a hidden field left out":
        del sent["expected_hand_in_id"]
        return client.post(url, data=sent, headers=PAGE_HEADERS)
    del sent["note"]
    return client.post(
        url, data=sent, files={"note": ("note.txt", b"uploaded words")}, headers=PAGE_HEADERS
    )


@pytest.mark.parametrize(
    "how",
    [
        "a note sent twice",
        "a field this page does not send",
        "a hidden field left out",
        "a note sent as a file",
    ],
)
def test_a_form_refused_for_another_field_keeps_the_choice_she_made(how: str) -> None:
    """Showing what was readable is not accepting the form: nothing is written for it. A note
    sent as a file is a form that could not be read, so nothing typed beside it is kept."""
    with browser() as client:
        answer = malformed(client, opened(client), how)

        if how == "a note sent as a file":
            store_free_page(answer, status=400, heading="Nothing was saved", alert=NOTHING_SAVED)
            assert "kept step" not in answer.text
        else:
            assert answer.status_code == 422
            turning = section(answer.text)
            assert escape(BAD_FORM) in answer.text
            assert 'value="turned_in" checked' in turning
            assert "kept step" in turning
            assert "first note" in turning
            assert "second note" not in turning
        assert chain(client) == []


def test_a_state_that_is_none_of_the_four_leaves_every_choice_open() -> None:
    with browser() as client:
        answer = save(client, opened(client), "handed_over", note="kept words")

        assert answer.status_code == 422
        assert " checked" not in section(answer.text)
        assert "kept words" in section(answer.text)


# ------------------------------------------------------ the moment and its day


class TickingClock:
    """A clock that moves on two seconds every time it is read, in a zone of its own."""

    def __init__(self, start: datetime, zone: ZoneInfo) -> None:
        self.at = start
        self._zone = zone

    @property
    def zone(self) -> ZoneInfo:
        return self._zone

    def now(self) -> datetime:
        read, self.at = self.at, self.at + timedelta(seconds=2)
        return read

    def today(self) -> date:
        return self.now().astimezone(self._zone).date()


@pytest.mark.parametrize(
    ("start", "zone"),
    [
        (datetime(2026, 8, 20, 3, 59, 59, tzinfo=UTC), "America/New_York"),
        (datetime(2026, 11, 1, 5, 59, 59, tzinfo=UTC), "America/New_York"),
        (datetime(2026, 8, 20, 6, 59, 59, tzinfo=UTC), "America/Los_Angeles"),
    ],
    ids=["midnight in New York", "the hour that repeats", "midnight in another zone"],
)
def test_the_day_an_event_is_kept_under_is_the_day_of_its_own_moment(
    start: datetime, zone: str
) -> None:
    """One read of the clock for a save and one for an undo, the day drawn from each."""
    with browser() as client:
        where = ZoneInfo(zone)
        page = opened(client)
        with_clock(client, TickingClock(start, where))
        first = landed(client, save(client, page, TURNED_IN))
        undo = form_fields(section(first), f"{ACTIONS}/undo-hand-in")
        client.post(f"{ACTIONS}/undo-hand-in", data=undo, headers=PAGE_HEADERS)
        again = save(client, opened(client), TURNED_IN)
        events = state_of(client).project_state.hand_in_chains([ESSAY_ID])[ESSAY_ID]

    assert again.status_code == 303
    assert len(events) == 3
    for event in events:
        assert event.reported_on == event.reported_at.astimezone(where).date()


def test_the_pinned_clock_keeps_its_day() -> None:
    with browser() as client:
        landed(client, save(client, opened(client), TURNED_IN))
        event = state_of(client).project_state.hand_in_chains([ESSAY_ID])[ESSAY_ID][0]

    assert event.reported_on == PLAN_DATE


# ------------------------------------------------- finding a refusal, and the way back


def summary(page: str) -> str:
    """The leading problem summary of a details page, tag and all."""
    start = page.index('id="problem-summary"')
    begin = page.rindex("<p", 0, start)
    return page[begin : page.index("</p>", start)]


def test_a_refusal_about_no_one_field_is_said_first_and_takes_the_focus() -> None:
    with browser() as client:
        behind = opened(client)
        landed(client, save(client, behind, TURNED_IN, note="from the other device"))
        conflict = save(client, behind, NEEDS_HAND_IN, note="my words").text
        turned_in = landed(client, save(client, opened(client), "not_required"))
        undo = form_fields(section(turned_in), f"{ACTIONS}/undo-hand-in")
        landed(client, save(client, opened(client), "unknown"))
        stale_undo = client.post(f"{ACTIONS}/undo-hand-in", data=undo, headers=PAGE_HEADERS).text
        elsewhere = save(client, opened(client), TURNED_IN, return_to="https://example.test/").text

    for page, problem in (
        (conflict, HAND_IN_CHANGED),
        (stale_undo, HAND_IN_CANNOT_UNDO),
        (elsewhere, BAD_RETURN),
    ):
        first = summary(page)
        assert escape(problem) in first
        assert 'tabindex="-1"' in first
        assert " autofocus" in first
        assert f'href="#hand-in-problem-{ESSAY_ID}"' in first
        assert page.count(" autofocus") == 1
        assert page.index('id="problem-summary"') < page.index('id="turning-it-in"')
        assert f'id="hand-in-problem-{ESSAY_ID}" tabindex="-1"' in page
    assert "Save again" in section(conflict)
    assert "Save hand-in update" not in section(conflict)
    assert "Save again" not in section(elsewhere)


def test_a_refusal_about_one_field_is_said_first_with_a_link_to_that_field() -> None:
    with browser() as client:
        long_note = save(client, opened(client), TURNED_IN, note="x" * 501).text
        two_lines = save(client, opened(client), NEEDS_HAND_IN, action="one\ntwo").text
        nothing = save(client, opened(client), None).text

    for page, target in (
        (long_note, f"hand-in-note-{ESSAY_ID}"),
        (two_lines, f"next-action-{ESSAY_ID}"),
        (nothing, f"hand-in-state-{ESSAY_ID}"),
    ):
        first = summary(page)
        assert f'href="#{target}"' in first
        assert " autofocus" not in first
        assert page.count(" autofocus") == 1
        assert f'id="{target}"' in section(page)


def test_a_way_back_from_a_hand_in_row_lands_on_that_row_and_not_on_an_empty_section() -> None:
    with browser() as client:
        landed(client, save(client, opened(client), NEEDS_HAND_IN, action="Put it in my folder"))
        details = client.get(DETAILS, params={"return_to": "family"}).text
        found = re.search(r'<a href="([^"]+)">Back to family review</a>', details)
        assert found is not None
        family = client.get(found.group(1).replace("&amp;", "&")).text

        assert found.group(1).endswith(f"#update-{ESSAY_ID}")
        assert family.count(f'id="update-{ESSAY_ID}"') == 1
        start = family.index(f'id="update-{ESSAY_ID}"')
        assert "Put it in my folder" in family[start : family.index("</article>", start)]
        assert "No assignment updates to show." not in family


def test_a_row_in_both_sections_keeps_one_of_each_id_and_an_absent_one_keeps_its_fallback() -> None:
    with browser() as client:
        report(client, ESSAY_ID, "not_yet")
        landed(client, save(client, opened(client), NEEDS_HAND_IN))
        both = client.get(FAMILY, params={"focus": ESSAY_ID}).text
        absent = client.get(FAMILY, params={"focus": QUIZ_ID}).text

    assert both.count(f'id="update-{ESSAY_ID}"') == 1
    assert both.count(f'id="hand-in-{ESSAY_ID}"') == 1
    assert absent.count(f'id="update-{QUIZ_ID}"') == 1
    assert f'<span id="update-{QUIZ_ID}"' in absent


# ------------------------------------------------ offered once, and on every kind of row

ALGEBRA_ID = "assignment-algebra-set"
ALGEBRA = f"/student/assignments/{ALGEBRA_ID}"
ALGEBRA_ACTIONS = f"/student/actions/assignments/{ALGEBRA_ID}"


def back_to_the_week(client: TestClient, result: str) -> str:
    found = re.search(r'<a href="([^"]+)">Back to the week</a>', result)
    assert found is not None
    shown = client.get(found.group(1).replace("&amp;", "&"))
    assert shown.status_code == 200
    return shown.text


SAID = {
    "still to turn in": (NEEDS_HAND_IN, "You reported Still to turn in on August 19. Next:"),
    "turned in": (TURNED_IN, "You reported it turned in on August 19."),
    "nothing to turn in": ("not_required", "You reported nothing to turn in on August 19."),
    "not sure": ("unknown", "Hand-in status: Not sure. You reported this on August 19."),
    "a record that cannot be read": (TURNED_IN, "cannot be read"),
}


@pytest.mark.parametrize("work", ["not_yet", "done"])
@pytest.mark.parametrize("kind", SAID.keys())
def test_a_row_that_is_due_later_shows_what_she_said_about_turning_it_in(
    kind: str, work: str
) -> None:
    """Saved on the details, then her real way back: the line is in the row she returns to."""
    state, expected = SAID[kind]
    with browser() as client:
        report(client, ALGEBRA_ID, work)
        week = client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text
        assert "Assigned this week, due later" in week
        assert NOT_RECORDED in card_for(week, ALGEBRA_ID)
        page = client.get(
            ALGEBRA, params={"hand_in": "change", "return_to": "week", "week": FIXTURE_WEEK}
        ).text
        fields = form_fields(page, f"{ALGEBRA_ACTIONS}/hand-in")
        action = "Put it in the return folder" if work == "not_yet" else ""
        answer = client.post(
            f"{ALGEBRA_ACTIONS}/hand-in",
            data={**fields, "state": state, "next_action": action, "note": ""},
            headers=PAGE_HEADERS,
        )
        result = landed(client, answer)
        if kind == "a record that cannot be read":
            store = state_of(client).project_state
            store._connection.execute(
                "UPDATE hand_in_events SET state = 'invalid-state' WHERE assignment_id = ?",
                (ALGEBRA_ID,),
            )
            store._connection.commit()
        returned = back_to_the_week(client, result)

    # The last row due later runs to the page's end, where her To turn in list follows it.
    row = card_for(returned, ALGEBRA_ID).split('<section class="panel to-turn-in"', 1)[0]
    assert expected in row
    assert NOT_RECORDED not in row
    if kind == "still to turn in":
        assert ("Put it in the return folder" if work == "not_yet" else "Turn it in.") in row
    if kind != "a record that cannot be read":
        assert (
            'href="' + ALGEBRA + "?hand_in=change&amp;return_to=week&amp;week=2026-08-17"
            "#turning-it-in" + '"'
        ) in row
    assert f"{ALGEBRA_ACTIONS}/hand-in" not in row
    if work == "done":
        fold = returned[: returned.index(f'id="assignment-{ALGEBRA_ID}"')]
        assert fold.rindex('<details class="steps reported-done" open>') > fold.rindex("due later")


def test_an_earlier_week_shows_what_stands_now_on_its_due_later_row() -> None:
    with browser() as client:
        landed(client, save(client, opened(client), NEEDS_HAND_IN, action="Put it in my folder"))
        earlier = client.get(HER_PAGE, params={"week": "2026-08-10"}).text

    assert "Assigned this week, due later" in earlier
    row = card_for(earlier, ESSAY_ID)
    assert "You reported Still to turn in on August 19. Next:" in row
    assert "Put it in my folder" in row
    assert "week=2026-08-10#turning-it-in" in row


# ------------------------------------ a field refused on a record that cannot be read


@pytest.mark.parametrize(
    "sent",
    [
        {"state": TURNED_IN, "note": "x" * 501, "action": "kept step"},
        {"state": NEEDS_HAND_IN, "note": "kept words", "action": "y" * 201},
        {"state": None, "note": "kept words", "action": "kept step"},
    ],
    ids=["a note past the limit", "a next step past the limit", "nothing chosen"],
)
def test_a_field_refused_on_an_unreadable_record_points_at_what_is_on_the_page(
    sent: dict[str, str | None],
) -> None:
    """No form is shown there, so the summary cannot send her to a field of it."""
    with browser() as client:
        landed(client, save(client, opened(client), TURNED_IN))
        behind = opened(client)
        before = damage(client, "previous_event_id", "no-such-event")

        answer = save(
            client, behind, sent["state"], action=sent["action"] or "", note=sent["note"] or ""
        )

        assert answer.status_code == 422
        page = answer.text
        first = summary(page)
        targets = re.findall(r'href="#([^"]+)"', first)
        assert targets == [f"hand-in-problem-{ESSAY_ID}"]
        assert all(f'id="{target}"' in page for target in targets)
        assert page.count(" autofocus") == 1
        assert " autofocus" in first
        turning = section(page)
        assert "cannot be read" in turning
        assert "Your unsaved hand-in update" in turning
        for words in (sent["note"], sent["action"]):
            assert str(words) in turning
        assert f"{ACTIONS}/hand-in" not in turning
        assert "undo-hand-in" not in turning
        assert rows(client) == before


# ------------------------------------------------ I turned it in, from a card and the details

WORDS = "Wren's <b>reading</b> notes"


def week_card(client: TestClient, **query: str) -> tuple[str, str]:
    """Her week and the essay's card on it."""
    page = client.get(HER_PAGE, params={"week": FIXTURE_WEEK, **query}).text
    return page, card_for(page, ESSAY_ID)


def turn_in_press(
    client: TestClient, html: str, button: str = "hand_in_head", **typed: str
) -> Answer:
    """A press of one of the buttons about turning it in, sent as a browser sends it from
    the page: every field its form owns, the button's own value, and what she typed."""
    action, sent = pressed_form(html, button)
    return client.post(action, data={**sent, **typed}, headers=PAGE_HEADERS)


def test_a_second_press_is_already_saved_and_undo_takes_back_only_that_report() -> None:
    """Two presses of one card make one report: the second is already saved and names the
    report that stands. Undo beside it takes that report back, and pressed again it is
    refused as already undone, writing nothing."""
    with browser() as client:
        _, card = week_card(client)
        first = turn_in_press(client, card)
        second = turn_in_press(client, card)
        events = chain(client)
        shown = card_for(landed(client, second), ESSAY_ID)
        undo = turn_in_press(client, shown, "undo_hand_in")
        undone = card_for(landed(client, undo), ESSAY_ID)
        again = turn_in_press(client, shown, "undo_hand_in")
        after = chain(client)

    assert first.status_code == second.status_code == 303
    assert len(events) == 1
    assert (
        f"turn_in_said=same&turn_in_about={ESSAY_ID}&turn_in_event={events[0]}"
        in (second.headers["location"])
    )
    assert HAND_IN_ALREADY_SAVED in shown
    assert f'name="undo_hand_in" value="{events[0]}"' in shown
    assert HAND_IN_UNDONE in undone
    assert NOT_RECORDED in undone
    assert 'name="undo_hand_in"' not in undone
    assert ">I turned it in</button>" in undone
    assert again.status_code == 409
    assert escape(ALREADY_UNDONE) in card_for(again.text, ESSAY_ID)
    assert len(after) == 2


@pytest.mark.parametrize("state", [TURNED_IN, "not_required"])
def test_a_press_over_turned_in_with_a_note_or_nothing_to_turn_in_writes_nothing(
    state: HandInState,
) -> None:
    """The card offers no press there, and a form made by hand over that head is no form a
    card made: refused whole, the note and the state kept as they stand."""
    with browser() as client:
        landed(client, save(client, opened(client), state, note="in the blue tray"))
        _, card = week_card(client)
        head = chain(client)[-1]
        answer = client.post(
            f"{ACTIONS}/turned-in",
            data={"hand_in_head": head, "week": FIXTURE_WEEK, "in_place": ""},
            headers=PAGE_HEADERS,
        )
        after = chain(client)

    assert ">I turned it in</button>" not in card
    assert ">Change</a>" in card
    assert answer.status_code == 422
    assert escape(TURN_IN_BAD_FORM) in answer.text
    assert after == [head]


def test_a_press_from_a_card_that_fell_behind_is_refused_with_what_stands() -> None:
    with browser() as client:
        _, card = week_card(client)
        landed(client, save(client, opened(client), NEEDS_HAND_IN))
        answer = turn_in_press(client, card)
        events = chain(client)
        shown = card_for(answer.text, ESSAY_ID)

    assert answer.status_code == 409
    assert escape(CHANGED_WITHOUT_THE_PAGE) in shown
    assert HAND_IN_CHANGED not in answer.text
    assert "You reported Still to turn in on August 19." in shown
    assert ">I turned it in</button>" in shown
    assert len(events) == 1


@pytest.mark.parametrize("outcome", ["saved", "changed elsewhere"])
def test_words_in_a_folded_note_come_back_unsaved_after_i_turned_it_in(outcome: str) -> None:
    """She types a note in the folded one-press form and presses I turned it in. The words
    are sent, saved nowhere, and come back in the same form, opened, under the heading that
    says they are not saved, with the update the page had shown."""
    with browser() as client:
        _, card = week_card(client)
        token = re.search(r'name="expected_report_id" value="([^"]*)"', card)
        assert token is not None
        if outcome == "changed elsewhere":
            landed(client, save(client, opened(client), NEEDS_HAND_IN))
        answer = turn_in_press(client, card, note=WORDS)
        store = state_of(client).project_state
        reports = store.student_reports(ESSAY_ID)
        events = store.hand_in_chains([ESSAY_ID])[ESSAY_ID]
    shown = card_for(answer.text, ESSAY_ID)

    assert answer.status_code == (200 if outcome == "saved" else 409)
    assert reports == []
    assert [event.note for event in events] == [None]
    assert f">{escape(WORDS)}</textarea>" in shown
    assert '<details class="steps note-fold" open>' in shown
    assert '<h4 class="update-heading">Your unsaved update</h4>' in shown
    assert f'name="expected_report_id" value="{token.group(1)}"' in shown
    if outcome == "saved":
        assert "You reported this turned in." in shown
        assert f'id="hand-in-result-{ESSAY_ID}" tabindex="-1" autofocus' in shown
    else:
        assert escape(CHANGED_WITHOUT_THE_PAGE) in shown


def test_a_cleared_note_on_an_open_form_comes_back_cleared_with_its_choice() -> None:
    with browser() as client:
        report(client, ESSAY_ID, "not_yet", note="old words")
        _, card = week_card(client, change=ESSAY_ID)
        answer = turn_in_press(client, card, note="")
        reports = state_of(client).project_state.student_reports(ESSAY_ID)
    shown = card_for(answer.text, ESSAY_ID)

    assert answer.status_code == 200
    assert [(report.status, report.note) for report in reports] == [("not_yet", "old words")]
    assert 'value="not_yet" checked' in shown
    assert "></textarea>" in shown
    assert "old words</textarea>" not in shown


def test_words_in_the_open_form_on_the_details_come_back_unsaved() -> None:
    with browser() as client:
        page = client.get(DETAILS, params={"return_to": "week", "week": FIXTURE_WEEK}).text
        action, sent = pressed_form(page, "hand_in_head")
        answer = client.post(action, data={**sent, "note": WORDS}, headers=PAGE_HEADERS)
        reports = state_of(client).project_state.student_reports(ESSAY_ID)

    assert action == f"{ACTIONS}/turned-in"
    assert sent["report_view"] == "detail"
    assert answer.status_code == 200
    assert reports == []
    assert f">{escape(WORDS)}</textarea>" in answer.text
    assert "You reported this turned in." in section(answer.text)
    assert "Back to the week" in section(answer.text)


def test_each_button_joins_the_form_it_belongs_to_and_enter_still_saves_the_update() -> None:
    with browser() as client:
        page, card = week_card(client)
        not_yet_page = client.get(report(client, QUIZ_ID, "not_yet")).text

    form_id = f"update-form-{ESSAY_ID}"
    action, sent = pressed_form(card, "hand_in_head")
    assert action == f"{ACTIONS}/turned-in"
    assert f'form="{form_id}"' in card
    assert set(sent) == {"hand_in_head", "note", "expected_report_id", "week", "in_place"}
    assert first_button_of(page, form_id).get("name") == "status"
    quiz = card_for(not_yet_page, QUIZ_ID)
    assert f'id="update-form-{QUIZ_ID}"' not in quiz
    assert f'<form method="post" action="/student/actions/assignments/{QUIZ_ID}/turned-in"' in quiz


def test_the_older_explanation_goes_where_the_new_line_shows() -> None:
    older = "own report, kept on the day Blossom took it."
    with browser() as client:
        landed(client, save(client, opened(client), NEEDS_HAND_IN))
        waiting = section(client.get(DETAILS).text)
        week = client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text
        landed(client, save(client, opened(client), TURNED_IN))
        turned = section(client.get(DETAILS).text)

    assert waiting.count(TURN_IN_MEANS) == 1
    assert older not in waiting
    assert TURN_IN_MEANS not in turned
    assert older in turned
    assert week.count(TURN_IN_MEANS) == 1
    assert week.count('aria-describedby="turn-in-means"') == week.count(
        ">I turned it in</button>"
    ) - week.count('name="state" value="turned_in"')


def test_a_saved_press_whose_page_cannot_be_read_says_what_was_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable(*_: object, **__: object) -> None:
        msg = "disk I/O error"
        raise sqlite3.OperationalError(msg)

    with browser() as client:
        _, card = week_card(client)
        monkeypatch.setattr(hand_in_routes, "card_page", unreadable)
        answer = turn_in_press(client, card, note=WORDS)
        events = chain(client)

    assert answer.status_code == 503
    assert "<h1>Turning it in</h1>" in answer.text
    assert escape("You reported this turned in. Your week can't be shown right now.") in (
        answer.text
    )
    assert '<h2 class="update-heading">Your unsaved update</h2>' in answer.text
    assert f"readonly>{escape(WORDS)}</textarea>" in answer.text
    assert "<form" not in answer.text.split("<main", 1)[1]
    assert "Nothing was saved" not in answer.text
    assert len(events) == 1


def on_the_loop_while(made: Callable[..., object], seen: list[str], name: str) -> object:
    """``made``, noting under ``name`` whether an event loop runs where it is called."""

    def watched(*args: object, **kwargs: object) -> object:
        try:
            asyncio.get_running_loop()
            seen.append(f"{name} on the loop")
        except RuntimeError:
            seen.append(f"{name} off the loop")
        return made(*args, **kwargs)

    return watched


def test_the_turn_in_routes_build_their_pages_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every page the two routes answer her with is made where no event loop runs, a worker
    thread, and not on the loop that serves every request: a saved press with her words, a
    press made twice, a press refused, a write the file refuses, and homework gone."""
    seen: list[str] = []

    def refuses(assignment_id: str, *_: object, **__: object) -> object:
        raise CouldNotSave(assignment_id, sqlite3.OperationalError("disk I/O error"))

    with browser() as client:
        _, card = week_card(client)
        monkeypatch.setattr(
            hand_in_routes, "card_page", on_the_loop_while(hand_in_routes.card_page, seen, "page")
        )
        monkeypatch.setattr(
            hand_in_routes, "gone_page", on_the_loop_while(student_routes.gone_page, seen, "gone")
        )
        saved = turn_in_press(client, card, note=WORDS)
        again = turn_in_press(client, card, note=WORDS)
        landed(client, save(client, opened(client), NEEDS_HAND_IN))
        changed = turn_in_press(client, card, note=WORDS)
        store = state_of(client).project_state
        with monkeypatch.context() as failing:
            failing.setattr(store, "record_hand_in", refuses)
            failed = turn_in_press(client, card, note=WORDS)
        store._connection.execute("DELETE FROM assignments WHERE assignment_id = ?", (ESSAY_ID,))
        store._connection.commit()
        gone = turn_in_press(client, card, note=WORDS)

    assert [saved.status_code, again.status_code, changed.status_code] == [200, 200, 409]
    assert [failed.status_code, gone.status_code] == [500, 404]
    assert seen == [
        "page off the loop",
        "page off the loop",
        "page off the loop",
        "page off the loop",
        "gone off the loop",
    ]


def test_a_parents_refused_turn_in_page_is_built_off_the_event_loop(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    page = student_routes.templates.TemplateResponse
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": THEIRS})
        monkeypatch.setattr(
            student_routes.templates, "TemplateResponse", on_the_loop_while(page, seen, "page")
        )
        answers = [
            client.post(
                f"{ACTIONS}/{press}",
                data={button: "", "note": WORDS, "expected_report_id": "", "week": FIXTURE_WEEK},
                headers=PAGE_HEADERS,
            )
            for press, button in (("turned-in", "hand_in_head"), ("undo-turned-in", "undo_hand_in"))
        ]

    assert [answer.status_code for answer in answers] == [403, 403]
    assert seen == ["page off the loop", "page off the loop"]


def test_a_result_read_after_the_record_moved_on_is_said_as_earlier_with_no_undo() -> None:
    """The address names the report the press made. Once a newer report follows it, the
    card says it was done earlier, offers no Undo, and an Undo of that report from the page
    shown before is refused and writes nothing."""
    with browser() as client:
        _, card = week_card(client)
        where = turn_in_press(client, card).headers["location"]
        shown = card_for(client.get(where).text, ESSAY_ID)
        landed(client, save(client, opened(client), TURNED_IN, note="in the tray"))
        later = card_for(client.get(where).text, ESSAY_ID)
        before = chain(client)
        undo = turn_in_press(client, shown, "undo_hand_in")
        after = chain(client)

    assert "You reported this turned in." in shown
    assert TURNED_IN_EARLIER in later
    assert "You reported this turned in." not in later
    assert 'name="undo_hand_in"' not in later
    assert ">Change</a>" in later
    assert undo.status_code == 409
    assert escape(HAND_IN_CANNOT_UNDO) in undo.text
    assert after == before


def test_a_later_visit_to_a_turned_in_card_offers_change_and_no_undo() -> None:
    with browser() as client:
        _, card = week_card(client)
        landed(client, turn_in_press(client, card))
        _, later = week_card(client)

    assert "You reported it turned in on August 19." in later
    assert ">Change</a>" in later
    assert 'name="undo_hand_in"' not in later
    assert ">I turned it in</button>" not in later


def test_a_card_form_sent_to_the_hand_in_route_writes_nothing_and_keeps_no_note_of_hers() -> None:
    with browser() as client:
        _, card = week_card(client)
        _, sent = pressed_form(card, "hand_in_head")
        answer = client.post(
            f"{ACTIONS}/hand-in", data={**sent, "note": WORDS}, headers=PAGE_HEADERS
        )
        events = chain(client)

    assert answer.status_code == 422
    assert escape(BAD_FORM) in answer.text
    assert f">{escape(WORDS)}</textarea>" not in section(answer.text)
    assert events == []


def test_a_hand_in_form_sent_to_the_turn_in_route_writes_nothing() -> None:
    with browser() as client:
        page = opened(client, return_to="week", week=FIXTURE_WEEK)
        fields = form_fields(page, f"{ACTIONS}/hand-in")
        answer = client.post(
            f"{ACTIONS}/turned-in",
            data={**fields, "state": TURNED_IN, "next_action": "", "note": "in the blue tray"},
            headers=PAGE_HEADERS,
        )
        events = chain(client)

    assert answer.status_code == 422
    assert escape(TURN_IN_BAD_FORM) in answer.text
    assert "in the blue tray" not in answer.text
    assert events == []


@pytest.mark.parametrize("result", ["same", "undone"])
def test_a_saved_press_whose_page_cannot_be_read_says_what_it_was(
    result: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Already saved and undone are said as they are, beside the page that can't be shown;
    neither says nothing was saved."""

    def unreadable(*_: object, **__: object) -> None:
        msg = "disk I/O error"
        raise sqlite3.OperationalError(msg)

    with browser() as client:
        _, card = week_card(client)
        shown = card_for(landed(client, turn_in_press(client, card)), ESSAY_ID)
        monkeypatch.setattr(hand_in_routes, "card_page", unreadable)
        if result == "same":
            answer = turn_in_press(client, card, note=WORDS)
        else:
            answer = turn_in_press(client, shown, "undo_hand_in", note=WORDS)
        events = chain(client)

    said = HAND_IN_ALREADY_SAVED if result == "same" else HAND_IN_UNDONE
    assert answer.status_code == 503
    assert "<h1>Turning it in</h1>" in answer.text
    assert escape(f"{said} Your week can't be shown right now.") in answer.text
    assert f"readonly>{escape(WORDS)}</textarea>" in answer.text
    assert "Nothing was saved" not in answer.text
    assert len(events) == (1 if result == "same" else 2)


def test_words_sent_with_undo_come_back_unsaved_and_undo_writes_once() -> None:
    with browser() as client:
        _, card = week_card(client)
        shown = card_for(landed(client, turn_in_press(client, card)), ESSAY_ID)
        undone = turn_in_press(client, shown, "undo_hand_in", note=WORDS)
        again = turn_in_press(client, shown, "undo_hand_in", note=WORDS)
        events = chain(client)
        reports = state_of(client).project_state.student_reports(ESSAY_ID)

    assert undone.status_code == 200
    assert HAND_IN_UNDONE in card_for(undone.text, ESSAY_ID)
    assert f">{escape(WORDS)}</textarea>" in card_for(undone.text, ESSAY_ID)
    assert again.status_code == 409
    assert f">{escape(WORDS)}</textarea>" in card_for(again.text, ESSAY_ID)
    assert len(events) == 2
    assert reports == []


def test_a_press_for_homework_taken_off_the_record_keeps_her_words_to_copy() -> None:
    with browser() as client:
        _, card = week_card(client)
        store = state_of(client).project_state
        store._connection.execute("DELETE FROM assignments WHERE assignment_id = ?", (ESSAY_ID,))
        store._connection.commit()
        answer = turn_in_press(client, card, note=WORDS)

    assert answer.status_code == 404
    assert '<h2 class="update-heading">Your unsaved update</h2>' in answer.text
    assert f"readonly>{escape(WORDS)}</textarea>" in answer.text


def test_a_refused_press_whose_page_cannot_be_read_keeps_her_words_as_her_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable(*_: object, **__: object) -> None:
        msg = "disk I/O error"
        raise sqlite3.OperationalError(msg)

    with browser() as client:
        _, card = week_card(client)
        landed(client, save(client, opened(client), NEEDS_HAND_IN))
        monkeypatch.setattr(hand_in_routes, "card_page", unreadable)
        answer = turn_in_press(client, card, note=WORDS)

    assert answer.status_code == 409
    assert "<h1>Update not saved</h1>" in answer.text
    assert escape("That changed while you were away, so nothing was saved.") in answer.text
    assert '<h2 class="update-heading">Your unsaved update</h2>' in answer.text
    assert f"readonly>{escape(WORDS)}</textarea>" in answer.text
    assert "hand-in update" not in answer.text.split("<main", 1)[1].lower()


def test_a_parent_at_a_result_address_reads_what_stands_and_no_result(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        _, card = week_card(client)
        where = turn_in_press(client, card).headers["location"]
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        read = client.get(where).text
        details = client.get(
            DETAILS, params={"turn_in_said": "turned_in", "turn_in_event": chain(client)[0]}
        ).text

    shown = card_for(read, ESSAY_ID)
    assert "She reported it turned in on August 19." in shown
    assert "You reported this turned in." not in read
    assert "You reported this turned in." not in details
    assert "<button" not in section(details)
    assert 'name="undo_hand_in"' not in read


@pytest.mark.parametrize("token", [" report-padded ", "r" * 201])
def test_a_work_token_no_page_writes_comes_back_blank_and_meets_a_standing_update(
    token: str,
) -> None:
    """A blank token is safe: the next save meets any update that stands as made elsewhere."""
    with browser() as client:
        report(client, ESSAY_ID, "done")
        _, card = week_card(client, change=ESSAY_ID)
        answer = turn_in_press(client, card, note=WORDS, expected_report_id=token)
        shown = card_for(answer.text, ESSAY_ID)
        fields = dict(form_fields(shown, f"{ACTIONS}/report"))
        save_again = client.post(
            f"{ACTIONS}/report",
            data={**fields, "status": "not_yet", "note": WORDS},
            headers=PAGE_HEADERS,
        )
        reports = state_of(client).project_state.student_reports(ESSAY_ID)

    assert answer.status_code == 200
    assert fields["expected_report_id"] == ""
    assert save_again.status_code == 409
    assert [report.status for report in reports] == ["done"]


HAND_IN_KINDS: dict[str, HandInState | None] = {
    "no report": None,
    "not sure": "unknown",
    "still to turn in": NEEDS_HAND_IN,
    "turned in with a note": TURNED_IN,
    "nothing to turn in": "not_required",
    "a hand-in record that cannot be read": TURNED_IN,
}


@pytest.mark.parametrize("work", ["none", "done", "not_yet", "unreadable"])
@pytest.mark.parametrize("kind", HAND_IN_KINDS)
def test_each_hand_in_state_shows_its_controls_beside_any_work_state(kind: str, work: str) -> None:
    """The press is offered over no report, Not sure and Still to turn in, whatever her work
    says, an unreadable work record included; Change otherwise; nothing to press over a
    hand-in record that cannot be read. With no work form to join, beside a work record that
    cannot be read, the press is a form of its own and there is nothing typed to send."""
    state = HAND_IN_KINDS[kind]
    with browser() as client:
        store = state_of(client).project_state
        if work != "none":
            report(client, ESSAY_ID, "done" if work == "unreadable" else work)
        if work == "unreadable":
            spoil(store, "student_reports", "status", "invalid-status")
        if state is not None:
            note = "in the tray" if kind == "turned in with a note" else None
            store.record_hand_in(
                ESSAY_ID,
                state,
                None,
                note,
                expected_head=None,
                now=datetime(2026, 8, 19, 21, 0, tzinfo=UTC),
                today=PLAN_DATE,
            )
        if kind == "a hand-in record that cannot be read":
            spoil(store, "hand_in_events", "state", "invalid-state")
        _, card = week_card(client)
        offered = ">I turned it in</button>" in card
        pressed = turn_in_press(client, card) if offered else None
        events = [] if pressed is None else chain(client)

    assert offered is (kind in ("no report", "not sure", "still to turn in"))
    # No address names a result here, so no state, Nothing to turn in included, offers Undo.
    assert 'name="undo_hand_in"' not in card
    assert (f'aria-label="Change your hand-in update for {ESSAY_TITLE}' in card) is (
        kind in ("turned in with a note", "nothing to turn in")
    )
    if kind == "a hand-in record that cannot be read":
        assert "The hand-in record cannot be read right now." in card
        assert NOT_RECORDED not in card
    if work == "unreadable":
        assert f'id="update-form-{ESSAY_ID}"' not in card
        assert "<textarea" not in card
        if offered:
            assert f'<form method="post" action="{ACTIONS}/turned-in"' in card
    if pressed is not None:
        assert pressed.status_code == 303
        assert "turn_in_said=turned_in" in pressed.headers["location"]
        assert len(events) == (1 if state is None else 2)


@pytest.mark.parametrize("reader", ["a parent", "a record that cannot be read"])
def test_a_card_with_nothing_to_press_keeps_the_way_to_turning_it_in(
    reader: str, tmp_path: pathlib.Path
) -> None:
    """Where the group offers no control, its line still ends in the link to the section on
    the details, as it always has."""
    link = (
        f'<a class="assignment-link" href="{DETAILS}?return_to=week&amp;week={FIXTURE_WEEK}'
        f'#turning-it-in" aria-label="Turning it in: {ESSAY_TITLE}, World History">'
        "Turning it in</a>"
    )
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        if reader == "a parent":
            client.post("/sign-out")
            client.post("/sign-in", data={"passphrase": THEIRS})
        else:
            landed(client, save(client, opened(client), TURNED_IN))
            spoil(state_of(client).project_state, "hand_in_events", "state", "invalid-state")
        _, card = week_card(client)

    assert link in card
    assert ">I turned it in</button>" not in card
    assert ">Other turn-in options</a>" not in card


@pytest.mark.parametrize("refusal", ["changed elsewhere", "a way back no page makes"])
def test_a_changed_choice_and_a_typed_note_both_come_back_after_a_refused_press(
    refusal: str,
) -> None:
    """Her own press from the open form keeps her changed choice and her note through a
    refusal, 409 or 422: only a parent's refused press leaves the choice behind."""
    with browser() as client:
        report(client, ESSAY_ID, "done")
        _, card = week_card(client, change=ESSAY_ID)
        typed: dict[str, str] = {"status": "not_yet", "note": WORDS}
        if refusal == "changed elsewhere":
            landed(client, save(client, opened(client), NEEDS_HAND_IN))
        else:
            typed["week"] = "someday"
        answer = turn_in_press(client, card, **typed)
        reports = state_of(client).project_state.student_reports(ESSAY_ID)
    shown = card_for(answer.text, ESSAY_ID)

    assert answer.status_code == (409 if refusal == "changed elsewhere" else 422)
    assert 'value="not_yet" checked' in shown
    assert f">{escape(WORDS)}</textarea>" in shown
    assert [report.status for report in reports] == ["done"]
