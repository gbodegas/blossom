"""Turning it in, on the pages: her own account of delivery, kept apart from Done.

The fixture week through the app, a pinned clock, and forms read from the
page's own HTML, so a hidden field the page does not write is never sent.
Nothing here needs a script in the browser, and no model is asked.
"""

import pathlib
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.app import create_app
from blossom.hand_in import NEEDS_HAND_IN, TURNED_IN, HandInSaved, HandInState
from blossom.noticing import planning_digest, read_everything, week_from
from blossom.plans import DailyPlan
from blossom.routes.hand_in import (
    CHOOSE_A_STATE,
    HAND_IN_CANNOT_UNDO,
    HAND_IN_CHANGED,
    HAND_IN_NOT_SAVED,
    NEXT_ACTION_ONE_LINE,
    NEXT_ACTION_TOO_LONG,
    NOT_A_HAND_IN_OF_THIS,
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
)
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    ESSAY_ID,
    ESSAY_TITLE,
    FIXTURE_WEEK,
    HER_PAGE,
    HERS,
    PAGE_HEADERS,
    PLAN_DATE,
    SAME_ORIGIN,
    THEIRS,
    Answer,
    Scripted,
    a_row,
    accepting,
    browser,
    card_for,
    fixture_week_plan,
    form_fields,
    human_text,
    report,
    scripted_graphs,
    signed_in_household,
    state_of,
    with_clock,
)

DETAILS = f"/student/assignments/{ESSAY_ID}"
ACTIONS = f"/student/actions/assignments/{ESSAY_ID}"
FAMILY = "/parent"
NOT_RECORDED = "Hand-in status not recorded."


def section(page: str) -> str:
    """The Turning it in section of a details page."""
    start = page.index('id="turning-it-in"')
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
        assert "Update hand-in status" in turning
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
        assert "Done means you have finished your part. It does not turn work in." in form
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
        assert "Change" in shown
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


def test_her_card_shows_one_quiet_line_and_a_done_save_offers_the_hand_in_update() -> None:
    with browser() as client:
        before = card_for(client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text, ESSAY_ID)
        assert NOT_RECORDED in before
        assert f"{ACTIONS}/hand-in" not in before

        where = report(client, ESSAY_ID, "done")
        after_done = card_for(client.get(where).text, ESSAY_ID)
        assert "Your update is saved." in after_done
        assert "Update hand-in status" in after_done
        assert "hand_in=change" in after_done
        assert chain(client) == []

        landed(client, save(client, opened(client), NEEDS_HAND_IN, action="Put it in my folder"))
        card = card_for(client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text, ESSAY_ID)
        assert "Still to turn in" in card
        assert "Put it in my folder" in card
        assert "#turning-it-in" in card


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

        assert client.post("/student/actions/plan").status_code == 303
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

QUIZ_ID = "assignment-vocabulary-quiz"
MALFORMED = {
    "a state that is none of the four": ("state", "invalid-state"),
    "a day that is no day": ("reported_on", "not-a-day"),
    "a note past the limit": ("note", "x" * 501),
}


def damage(client: TestClient, column: str, value: str) -> list[tuple[object, ...]]:
    """Spoil the essay's one stored event with plain SQL, and hand back the table as it is."""
    store = state_of(client).project_state
    store._connection.execute(
        f"UPDATE hand_in_events SET {column} = ? WHERE assignment_id = ?",  # noqa: S608
        (value, ESSAY_ID),
    )
    store._connection.commit()
    return store._connection.execute("SELECT * FROM hand_in_events ORDER BY sequence").fetchall()


def rows(client: TestClient) -> list[tuple[object, ...]]:
    connection = state_of(client).project_state._connection
    return connection.execute("SELECT * FROM hand_in_events ORDER BY sequence").fetchall()


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

        made = client.post("/student/actions/plan")

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
    """Showing what was readable is not accepting the form: nothing is written for it."""
    with browser() as client:
        answer = malformed(client, opened(client), how)

        assert answer.status_code == 422
        turning = section(answer.text)
        assert escape(BAD_FORM) in answer.text
        assert 'value="turned_in" checked' in turning
        assert "kept step" in turning
        if how != "a note sent as a file":
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
