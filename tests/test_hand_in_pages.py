"""Turning it in, on the pages: her own account of delivery, kept apart from Done.

The fixture week through the app, a pinned clock, and forms read from the
page's own HTML, so a hidden field the page does not write is never sent.
Nothing here needs a script in the browser, and no model is asked.
"""

import pathlib
import re

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.app import create_app
from blossom.hand_in import NEEDS_HAND_IN, TURNED_IN
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
        assert unsigned.status_code == 303
        assert unsigned.headers["location"].startswith("/sign-in")
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
