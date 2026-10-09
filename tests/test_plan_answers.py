# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""What the two plan presses answer: her plan button and the family Plan it.

Every answer says a fact, adds a clause about the page only when the page it sends shows
what the clause names, keeps what the press carried that the page can show, and has words
of its own for the stand-in that reads no store. These tests drive each answer through the
app with synthetic state and scripted models, and hold it to words written out in
``tests/support.py``.
"""

import asyncio
import pathlib
import re
import sqlite3
from collections.abc import Callable
from concurrent.futures import Future
from datetime import UTC, date, datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from typing import Any, cast, get_args
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from blossom.agent import graph as graph_module
from blossom.agent.runs import RUN_DEADLINE_SECONDS, Unfinished
from blossom.drafts import DraftStatus
from blossom.plan_checks import past_deadlines
from blossom.reconciliation import SourceChannel
from blossom.routes import parent as parent_routes
from blossom.routes import student as student_routes
from blossom.routes.runs import (
    AlreadyPlanning,
    CouldNotStart,
    NotSaved,
    Unconfirmed,
    ended_sentences,
    plan_graphs,
)
from blossom.routes.student import place_key
from blossom.stores.drafts import RunState, StoreBusy, WriterBusy
from blossom.stores.project_state import Assignment
from blossom.views import PastDueView
from tests.support import (
    ENDED_REASONS,
    ESSAY_ID,
    FAMILY_ASKED_FOR_AUGUST_19,
    FAMILY_DATE_NOT_READ,
    FAMILY_FOR_A_NEW_PLAN,
    FAMILY_FORM_EXPIRED,
    FAMILY_FORM_NOT_WHOLE,
    FAMILY_LINE,
    FAMILY_NEWER_PLAN_AUGUST_19,
    FAMILY_NEWER_PLAN_AUGUST_20,
    FAMILY_NOT_SHOWN_LINE,
    FAMILY_PLAN_ACTION,
    FAMILY_PLAN_ALREADY_MADE_AUGUST_20,
    FAMILY_PLAN_ALREADY_MADE_TODAY,
    FAMILY_REVIEW_SHOWS,
    FAMILY_ROWS,
    HER_FOR_A_NEW_PLAN,
    HER_FOR_A_NEW_PLAN_TODAY,
    HER_FORM_EXPIRED,
    HER_FORM_FROM_AUGUST_18,
    HER_FORM_NOT_WHOLE,
    HER_NEWER_PLAN,
    HER_PAGE,
    HER_PLAN_ACTION,
    HER_PLAN_ALREADY_MADE,
    HER_ROWS,
    HER_TOP_LINE,
    HER_UPDATES_SAVED,
    HERS,
    NOTHING_FOR_AUGUST_20,
    PAGE_HEADERS,
    PLAN_DATE,
    SHOWN_BELOW_UNDER_EARLIER_PLANS,
    SHOWN_BELOW_UNDER_TODAYS_REVIEWED_PLAN,
    SHOWN_BELOW_WAITING,
    STAND_IN_LINE,
    THAT_PLAN_SHOWN_BELOW,
    THEIRS,
    YOUR_WEEK_NOT_SHOWN_LINE,
    AnswerRow,
    Scripted,
    accepting,
    browser,
    database_of,
    ended_run,
    family_line,
    family_line_focused,
    family_plan,
    first_sentence,
    fixture_week_plan,
    form_fields,
    fresh_plan_fields,
    her_line,
    household_client,
    opening_focused,
    plan_date_shown,
    plan_fold_open,
    plan_form,
    record,
    refusing,
    report,
    reported,
    runs_recorded,
    scripted_graphs,
    sign_in_as,
    state_of,
    the_answer_alert,
    words,
)

ISSUED_LONG_AGO = "20260101T000000Z"
LATER_EVENING = date(2026, 8, 20)
REPORTED = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)
STORE_FAILURE_KINDS = (StoreBusy, WriterBusy, sqlite3.OperationalError)


def overtaking(client: TestClient) -> None:
    """Have the next run that settles find a newer plan for its evening, published through a
    connection of its own as another process would publish one: a copy of the evening's
    newest plan under ids of its own."""
    drafts = state_of(client).drafts
    original = drafts.settle_run
    path = state_of(client).settings.database_path

    def settle(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                """
                INSERT INTO drafts (draft_id, thread_id, plan_date, status, outcome, body,
                                    created_at, published, published_order)
                SELECT 'draft:plan:outside', 'plan:outside', plan_date, 'DRAFT', outcome, body,
                       created_at, 1, (SELECT MAX(published_order) + 1 FROM drafts)
                FROM drafts WHERE published = 1 ORDER BY published_order DESC LIMIT 1
                """
            )
            connection.commit()
        finally:
            connection.close()
        return original(*args, **kwargs)

    drafts.settle_run = settle  # type: ignore[method-assign]


def failing_once(kind: type[Exception], original: Callable[..., object]) -> Callable[..., object]:
    """A store read that fails the first time it is called, ``kind`` raised as a busy file
    raises it, and reads as ``original`` after that."""
    calls: list[int] = []

    def read(*args: object, **kwargs: object) -> object:
        calls.append(1)
        if len(calls) == 1:
            msg = "database is locked"
            raise kind(msg)
        return original(*args, **kwargs)

    return read


# ------------------------------------------------- her plan button


@pytest.mark.parametrize(
    ("case", "fact", "status"),
    [
        ("newer plan", HER_NEWER_PLAN, 409),
        ("not whole", HER_FORM_NOT_WHOLE, 422),
        ("another evening", HER_FORM_FROM_AUGUST_18, 409),
        ("expired", HER_FORM_EXPIRED, 409),
    ],
    ids=["newer plan", "not whole", "another evening", "expired"],
)
def test_her_answer_on_the_stand_in_names_no_place_or_button(
    case: str, fact: str, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When her week can't be read, the answer is its fact alone: no plan shown below, and no
    button to press, since the stand-in has neither."""
    with browser(key=True) as client:
        form = plan_form(client)
        if case == "newer plan":
            assert client.post(
                HER_PLAN_ACTION, data=plan_form(client), headers=PAGE_HEADERS
            ).is_redirect
        elif case == "not whole":
            form = {**form, "evening": "tonight"}
        elif case == "another evening":
            form = {**form, "evening": "2026-08-18"}
        else:
            form = {**form, "issued_at": ISSUED_LONG_AGO}
        before = runs_recorded(client)
        monkeypatch.setattr(state_of(client).drafts, "latest_for", refusing())
        answer = client.post(HER_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
        monkeypatch.undo()
        assert runs_recorded(client) == before
    assert answer.status_code == status
    assert the_answer_alert(answer.text) == f"{fact} {YOUR_WEEK_NOT_SHOWN_LINE}"
    assert f'action="{HER_PLAN_ACTION}"' not in answer.text


@pytest.mark.parametrize("whole", [True, False], ids=["whole", "not whole"])
def test_her_kept_cards_stay_on_a_form_that_is_refused(whole: bool) -> None:
    """The cards her visit keeps in place stay in place on the page that refuses the form,
    whether or not the form was whole."""
    kept = f"a:{place_key(ESSAY_ID)}"
    with browser(key=True) as client:
        form = {**plan_form(client), "evening": "tonight", "in_place": kept}
        if not whole:
            form["stray"] = "1"
        answer = client.post(HER_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
    assert answer.status_code == 422
    assert her_line(answer.text) == f"{HER_FORM_NOT_WHOLE} {HER_FOR_A_NEW_PLAN_TODAY}"
    assert form_fields(answer.text, HER_PLAN_ACTION).get("in_place") == kept


@pytest.mark.parametrize("kind", STORE_FAILURE_KINDS, ids=lambda kind: kind.__name__)
def test_her_repeat_whose_replacement_check_fails_says_a_plan_was_made(
    kind: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A used form whose run made a plan, when what came after it can't be read, is answered
    409 with that plan made and nothing new started, never as if its plan were the latest."""
    with browser(key=True) as client:
        first = plan_form(client)
        assert client.post(HER_PLAN_ACTION, data=first, headers=PAGE_HEADERS).is_redirect
        assert client.post(
            HER_PLAN_ACTION, data=plan_form(client), headers=PAGE_HEADERS
        ).is_redirect
        before = runs_recorded(client)
        drafts = state_of(client).drafts
        monkeypatch.setattr(drafts, "latest_for", failing_once(kind, drafts.latest_for))
        answer = client.post(HER_PLAN_ACTION, data=first, headers=PAGE_HEADERS)
        monkeypatch.undo()
        assert runs_recorded(client) == before
    assert answer.status_code == 409, answer.headers.get("location")
    assert her_line(answer.text) == f"{HER_PLAN_ALREADY_MADE} {HER_FOR_A_NEW_PLAN}"


def test_her_first_press_overtaken_says_a_newer_plan_was_made() -> None:
    """A first press whose run is overtaken by a newer plan for today says so, 409, with that
    plan shown and a new one still asked for by a fresh press; it never lands silently."""
    with browser(key=True) as client:
        assert client.post(
            HER_PLAN_ACTION, data=plan_form(client), headers=PAGE_HEADERS
        ).is_redirect
        form = plan_form(client)
        overtaking(client)
        answer = client.post(HER_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
        ran = runs_recorded(client)
    assert (ran[-1][0], ran[-1][2]) == (form["run_id"], "ended")
    assert answer.status_code == 409, answer.headers.get("location")
    assert her_line(answer.text) == f"{HER_NEWER_PLAN} It is shown below. {HER_FOR_A_NEW_PLAN}"


def test_family_first_press_overtaken_says_a_newer_plan_was_made() -> None:
    """A first family press whose run is overtaken by a newer plan for its evening says so,
    409, with the evening kept in the open fold; it never lands silently."""
    with browser(key=True) as client:
        assert client.post(FAMILY_PLAN_ACTION, data=family_plan(client, "")).is_redirect
        form = family_plan(client, "")
        overtaking(client)
        answer = client.post(FAMILY_PLAN_ACTION, data=form)
        ran = runs_recorded(client)
    assert (ran[-1][0], ran[-1][2]) == (form["run_id"], "ended")
    assert answer.status_code == 409, answer.headers.get("location")
    assert family_line(answer.text) == (
        f"{FAMILY_NEWER_PLAN_AUGUST_19} {SHOWN_BELOW_WAITING} {FAMILY_FOR_A_NEW_PLAN}"
    )
    assert plan_fold_open(answer.text) is True
    assert plan_date_shown(answer.text) == PLAN_DATE.isoformat()
    assert family_line_focused(answer.text)


@pytest.mark.parametrize("who", ["another origin", "signed out"])
def test_her_used_form_is_refused_at_the_door_before_its_run_is_read(
    who: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A used form refused by the gate reads no run and says nothing about it."""
    with household_client("parent", tmp_path) as client:
        sign_in_as(client, "her")
        form = fresh_plan_fields(client)
        ended_run(
            state_of(client).drafts,
            thread_id=form["run_id"],
            plan_date=PLAN_DATE,
            outcome="timed_out",
        )
        headers = dict(PAGE_HEADERS)
        if who == "another origin":
            headers["Origin"] = "http://elsewhere.example"
        else:
            assert client.post("/sign-out", headers=PAGE_HEADERS).status_code < 400
        looked: list[str] = []
        monkeypatch.setattr(
            state_of(client).drafts, "run_status", lambda run_id, *_, **__: looked.append(run_id)
        )
        pressed = client.post(HER_PLAN_ACTION, data=form, headers=headers)
        monkeypatch.undo()
    assert looked == []
    assert pressed.status_code in {303, 403}
    assert ENDED_REASONS["timed_out"] not in pressed.text


# ------------------------------------------------- the family Plan it


@pytest.mark.parametrize("case", ["not whole", "expired"])
def test_family_refusal_with_an_unreadable_date_claims_nothing_kept(case: str) -> None:
    """A refused form whose date can't be read keeps no date: the open fold holds today, and
    the line names Plan it without saying any evening is kept."""
    with browser(key=True) as client:
        form = family_plan(client, "someday")
        form = (
            {**form, "stray": "1"}
            if case == "not whole"
            else {**form, "issued_at": ISSUED_LONG_AGO}
        )
        answer = client.post(FAMILY_PLAN_ACTION, data=form)
    fact = FAMILY_FORM_NOT_WHOLE if case == "not whole" else FAMILY_FORM_EXPIRED
    assert answer.status_code == (422 if case == "not whole" else 409)
    assert family_line(answer.text) == f"{fact} {FAMILY_FOR_A_NEW_PLAN}"
    assert plan_fold_open(answer.text) is True
    assert plan_date_shown(answer.text) == PLAN_DATE.isoformat()
    assert family_line_focused(answer.text)


def test_family_form_not_whole_with_a_blank_date_keeps_its_evening() -> None:
    """A blank date means the form's evening, so a form that isn't whole keeps that evening,
    not the server's today."""
    with browser(key=True) as client:
        form = {**family_plan(client, ""), "evening": "2026-08-18", "stray": "1"}
        answer = client.post(FAMILY_PLAN_ACTION, data=form)
    assert answer.status_code == 422
    assert family_line(answer.text) == f"{FAMILY_FORM_NOT_WHOLE} {FAMILY_FOR_A_NEW_PLAN}"
    assert plan_fold_open(answer.text) is True
    assert plan_date_shown(answer.text) == "2026-08-18"


def test_family_newer_plan_waiting_is_shown_below_the_line() -> None:
    """The line comes first on the family page, so a newer plan waiting for review is below."""
    with browser(key=True) as client:
        stale = family_plan(client, "")
        assert client.post(FAMILY_PLAN_ACTION, data=family_plan(client, "")).is_redirect
        before = runs_recorded(client)
        answer = client.post(FAMILY_PLAN_ACTION, data=stale)
        assert runs_recorded(client) == before
    assert answer.status_code == 409
    text = answer.text
    assert family_line(text) == (
        f"{FAMILY_NEWER_PLAN_AUGUST_19} {SHOWN_BELOW_WAITING} {FAMILY_FOR_A_NEW_PLAN}"
    )
    assert text.index('id="problem"') < text.index("<h2>Waiting for your review</h2>")


def test_family_newer_plan_decided_today_is_named_by_its_own_heading() -> None:
    """Today's decided plan is shown under its own heading, so the line names that heading."""
    with browser(key=True) as client:
        stale = family_plan(client, "")
        assert client.post(FAMILY_PLAN_ACTION, data=family_plan(client, "")).is_redirect
        newer = state_of(client).drafts.latest_for(PLAN_DATE)
        assert newer is not None
        state_of(client).drafts.record_decision(
            newer.draft_id,
            status=DraftStatus.APPROVED_FOR_MANUAL_SEND,
            decision="approved",
            reason=None,
        )
        answer = client.post(FAMILY_PLAN_ACTION, data=stale)
    assert answer.status_code == 409
    assert family_line(answer.text) == (
        f"{FAMILY_NEWER_PLAN_AUGUST_19} {SHOWN_BELOW_UNDER_TODAYS_REVIEWED_PLAN} "
        f"{FAMILY_FOR_A_NEW_PLAN}"
    )


@pytest.mark.parametrize("kind", STORE_FAILURE_KINDS, ids=lambda kind: kind.__name__)
def test_family_newer_plan_unread_is_refused_with_no_place(
    kind: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When where the newer plan is can't be read, the stale press is refused all the same,
    409 with no place named, whatever the store failed with."""
    with browser(key=True) as client:
        stale = family_plan(client, "")
        assert client.post(FAMILY_PLAN_ACTION, data=family_plan(client, "")).is_redirect
        before = runs_recorded(client)
        monkeypatch.setattr(state_of(client).drafts, "latest_for", refusing(kind))
        answer = client.post(FAMILY_PLAN_ACTION, data=stale)
        monkeypatch.undo()
        assert runs_recorded(client) == before
    assert answer.status_code == 409
    assert family_line(answer.text) == f"{FAMILY_NEWER_PLAN_AUGUST_19} {FAMILY_FOR_A_NEW_PLAN}"


@pytest.mark.parametrize("kind", STORE_FAILURE_KINDS, ids=lambda kind: kind.__name__)
def test_family_repeat_whose_replacement_check_fails_says_a_plan_was_made(
    kind: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A used family form whose run made a plan, when what came after it can't be read, is
    answered 409 with that plan made and nothing new started, never a silent landing."""
    with browser(key=True) as client:
        first = family_plan(client, "")
        assert client.post(FAMILY_PLAN_ACTION, data=first).is_redirect
        assert client.post(FAMILY_PLAN_ACTION, data=family_plan(client, "")).is_redirect
        before = runs_recorded(client)
        drafts = state_of(client).drafts
        monkeypatch.setattr(drafts, "latest_for", failing_once(kind, drafts.latest_for))
        answer = client.post(FAMILY_PLAN_ACTION, data=first)
        monkeypatch.undo()
        assert runs_recorded(client) == before
    assert answer.status_code == 409, answer.headers.get("location")
    assert family_line(answer.text) == f"{FAMILY_PLAN_ALREADY_MADE_TODAY} {FAMILY_FOR_A_NEW_PLAN}"
    assert plan_date_shown(answer.text) == PLAN_DATE.isoformat()
    assert plan_fold_open(answer.text) is True


def test_family_repeat_for_a_later_evening_whose_replacement_check_fails_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same answer for a used form whose plan was for an evening after today names that
    evening, where a form for today says today."""
    later_plan = fixture_week_plan().model_copy(update={"plan_date": LATER_EVENING})
    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [later_plan], lambda: [accepting()]
        )
        first = family_plan(client, LATER_EVENING.isoformat())
        published(client, FAMILY_PLAN_ACTION, first)
        before = runs_recorded(client)
        drafts = state_of(client).drafts
        monkeypatch.setattr(drafts, "latest_for", failing_once(StoreBusy, drafts.latest_for))
        answer = client.post(FAMILY_PLAN_ACTION, data=first)
        monkeypatch.undo()
        assert runs_recorded(client) == before
    assert answer.status_code == 409, answer.headers.get("location")
    assert family_line(answer.text) == (
        f"{FAMILY_PLAN_ALREADY_MADE_AUGUST_20} {FAMILY_FOR_A_NEW_PLAN}"
    )
    assert plan_date_shown(answer.text) == LATER_EVENING.isoformat()


def test_family_used_form_with_an_unreadable_date_claims_nothing_about_the_field() -> None:
    """A used form sent with a date that can't be read says so without pointing at the field,
    which shows today, in the open fold."""
    with browser(key=True) as client:
        first = family_plan(client, PLAN_DATE.isoformat())
        assert client.post(FAMILY_PLAN_ACTION, data=first).is_redirect
        answer = client.post(FAMILY_PLAN_ACTION, data={**first, "plan_date": "someday"})
    assert answer.status_code == 409
    assert family_line(answer.text) == (
        f"{FAMILY_ASKED_FOR_AUGUST_19} {THAT_PLAN_SHOWN_BELOW} {FAMILY_DATE_NOT_READ}"
    )
    assert plan_fold_open(answer.text) is True
    assert plan_date_shown(answer.text) == PLAN_DATE.isoformat()


@pytest.mark.parametrize("chosen", [LATER_EVENING.isoformat(), "someday"], ids=["W-5", "W-5u"])
def test_family_used_form_for_an_ended_run_says_why_and_that_nothing_started(chosen: str) -> None:
    """A used form whose run ended without a plan, sent with another evening or a date that
    can't be read, says why that run ended, that her updates are saved and that nothing was
    started, over the open plan form, with no direction to it."""
    with browser(key=True) as client:
        form = family_plan(client, PLAN_DATE.isoformat())
        ended_run(
            state_of(client).drafts,
            thread_id=form["run_id"],
            plan_date=PLAN_DATE,
            outcome="date_problem",
        )
        answer = client.post(FAMILY_PLAN_ACTION, data={**form, "plan_date": chosen})
    another = chosen == LATER_EVENING.isoformat()
    nothing = NOTHING_FOR_AUGUST_20 if another else FAMILY_DATE_NOT_READ
    assert answer.status_code == 409
    assert family_line(answer.text) == (
        f"{FAMILY_ASKED_FOR_AUGUST_19} {ENDED_REASONS['date_problem']} {HER_UPDATES_SAVED} "
        f"{nothing}"
    )
    assert f'action="{FAMILY_PLAN_ACTION}"' in answer.text
    assert plan_fold_open(answer.text) is True
    assert plan_date_shown(answer.text) == (chosen if another else PLAN_DATE.isoformat())
    assert family_line_focused(answer.text)


def test_family_ended_repeat_keeps_its_evening_with_the_focus_on_the_line() -> None:
    """A used form whose run ended without a plan keeps that run's evening in the open fold,
    and the line's first sentence takes the focus."""
    with browser(key=True) as client:
        form = family_plan(client, LATER_EVENING.isoformat())
        ended_run(
            state_of(client).drafts,
            thread_id=form["run_id"],
            plan_date=LATER_EVENING,
            outcome="timed_out",
        )
        answer = client.post(FAMILY_PLAN_ACTION, data=form)
    assert answer.status_code == 409
    assert plan_fold_open(answer.text) is True
    assert plan_date_shown(answer.text) == LATER_EVENING.isoformat()
    assert family_line_focused(answer.text)


@pytest.mark.parametrize("reason", ["timed_out", "service_failed", "date_problem", "checks_failed"])
def test_family_ended_repeat_on_the_stand_in_says_why_and_what_is_saved(
    reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the family page, the ended repeat says why and that her updates are saved,
    and never sends the reader to the page that can't be shown."""
    with browser(key=True) as client:
        form = family_plan(client, PLAN_DATE.isoformat())
        ended_run(
            state_of(client).drafts, thread_id=form["run_id"], plan_date=PLAN_DATE, outcome=reason
        )
        monkeypatch.setattr(state_of(client).drafts, "review_snapshot", refusing())
        answer = client.post(FAMILY_PLAN_ACTION, data=form)
        monkeypatch.undo()
    assert answer.status_code == 409
    alert = the_answer_alert(answer.text)
    assert alert == f"{ENDED_REASONS[reason]} {HER_UPDATES_SAVED} {FAMILY_NOT_SHOWN_LINE}"
    assert FAMILY_REVIEW_SHOWS not in alert


def cant_make_the_plan_for(named: str) -> tuple[str, str]:
    """A date problem for an evening that isn't today, as its opening and then the whole."""
    opening = f"Blossom can't make the plan for {named}."
    return opening, (
        f"{opening} Some work is due before that evening, so no plan can finish it on time."
    )


@pytest.mark.parametrize("shown", [True, False], ids=["page", "stand-in"])
@pytest.mark.parametrize(
    ("evening", "named"),
    [(LATER_EVENING, "Thursday, August 20"), (date(2026, 8, 18), "Tuesday, August 18")],
    ids=["later", "earlier"],
)
def test_family_ended_repeat_names_the_evening_of_its_date_problem(
    evening: date, named: str, shown: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A used form whose run, for an evening that isn't today, ended on a date problem says
    the date problem against that evening, on the page and on the stand-in, and its first
    sentence, naming the evening, takes the focus."""
    with browser(key=True) as client:
        form = family_plan(client, evening.isoformat())
        ended_run(
            state_of(client).drafts,
            thread_id=form["run_id"],
            plan_date=evening,
            outcome="date_problem",
        )
        if not shown:
            monkeypatch.setattr(state_of(client).drafts, "review_snapshot", refusing())
        answer = client.post(FAMILY_PLAN_ACTION, data=form)
        monkeypatch.undo()
    opening, said = cant_make_the_plan_for(named)
    assert answer.status_code == 409
    if shown:
        assert family_line(answer.text) == f"{said} {HER_UPDATES_SAVED} {FAMILY_REVIEW_SHOWS}"
        assert opening_focused(answer.text, FAMILY_LINE) == opening
    else:
        alert = the_answer_alert(answer.text)
        assert alert == f"{said} {HER_UPDATES_SAVED} {FAMILY_NOT_SHOWN_LINE}"
        assert opening_focused(answer.text, STAND_IN_LINE) == opening


@pytest.mark.parametrize("chosen", [PLAN_DATE.isoformat(), "someday"], ids=["W-5", "W-5u"])
def test_family_used_form_for_a_later_evenings_date_problem_names_that_evening(
    chosen: str,
) -> None:
    """A used form whose run, for a later evening, ended on a date problem, sent with another
    evening or a date that can't be read, says the date problem against the run's own
    evening, never as today's plan."""
    with browser(key=True) as client:
        form = family_plan(client, LATER_EVENING.isoformat())
        ended_run(
            state_of(client).drafts,
            thread_id=form["run_id"],
            plan_date=LATER_EVENING,
            outcome="date_problem",
        )
        answer = client.post(FAMILY_PLAN_ACTION, data={**form, "plan_date": chosen})
    _, said = cant_make_the_plan_for("Thursday, August 20")
    nothing = (
        "No plan was started for Wednesday, August 19."
        if chosen == PLAN_DATE.isoformat()
        else FAMILY_DATE_NOT_READ
    )
    assert answer.status_code == 409
    assert family_line(answer.text) == (
        f"This form was used for Thursday, August 20. {said} {HER_UPDATES_SAVED} {nothing}"
    )


@pytest.mark.parametrize("parent", [False, True], ids=["her", "parent"])
@pytest.mark.parametrize("count", [1, 2], ids=["one", "two"])
@pytest.mark.parametrize(
    ("evening", "named"),
    [(LATER_EVENING, "Thursday, August 20"), (date(2026, 8, 18), "Tuesday, August 18")],
    ids=["later", "earlier"],
)
def test_a_date_problem_for_another_evening_names_no_work_as_already_passed(
    evening: date, named: str, count: int, parent: bool
) -> None:
    """A date problem for an evening that isn't today says that evening's sentences even with
    past-due work from the record, which is due before that evening and may still be ahead,
    and never says today's plan or a date already passed."""
    work = [
        PastDueView(
            assignment_id=key, title=title, course=course, due_date=evening - timedelta(days=back)
        )
        for key, title, course, back in (
            ("assignment-map-quiz", "Map quiz", "Geography", 1),
            ("assignment-atlas-page", "Atlas page", "Art", 2),
        )
    ][:count]
    said = ended_sentences("date_problem", parent=parent, past_due=work, evening=evening)
    opening, whole = cant_make_the_plan_for(named)
    yours = "Your homework updates are saved."
    usual = (HER_UPDATES_SAVED, FAMILY_REVIEW_SHOWS) if parent else (yours,)
    assert said == (opening, whole.removeprefix(f"{opening} "), *usual)
    assert not any("today's plan" in part or "already passed" in part for part in said)


def test_family_check_page_for_an_ended_run_keeps_its_evening() -> None:
    """The page a run's check link opens, for the newest run of an evening still ahead that
    ended without a plan, holds that evening in the open fold, and says nothing about it."""
    run_id = uuid4().hex
    with browser(key=True) as client:
        ended_run(
            state_of(client).drafts, thread_id=run_id, plan_date=LATER_EVENING, outcome="timed_out"
        )
        page = client.get("/parent", params={"run": run_id}, headers=PAGE_HEADERS)
    assert page.status_code == 200
    assert plan_fold_open(page.text) is True
    assert plan_date_shown(page.text) == LATER_EVENING.isoformat()
    assert "kept below" not in page.text


def test_family_check_page_for_an_ended_run_starts_nothing_and_mints_a_new_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening the check page for an ended run prepares a new request and starts none: no run
    is admitted, the refilled form carries an id of its own, and only a press of that form
    plans the evening, under that new id."""
    run_id = uuid4().hex
    later_plan = fixture_week_plan().model_copy(update={"plan_date": LATER_EVENING})
    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [later_plan], lambda: [accepting()]
        )
        drafts = state_of(client).drafts
        ended_run(drafts, thread_id=run_id, plan_date=LATER_EVENING, outcome="timed_out")
        before = runs_recorded(client)
        admitted: list[str] = []
        admit = drafts.admit_run

        def admitting(run: str, *args: object, **kwargs: Any) -> object:  # noqa: ANN401
            admitted.append(run)
            return admit(run, *args, **kwargs)

        monkeypatch.setattr(drafts, "admit_run", admitting)
        page = client.get("/parent", params={"run": run_id}, headers=PAGE_HEADERS)
        loaded = (runs_recorded(client), list(admitted))
        refilled = form_fields(page.text, FAMILY_PLAN_ACTION)
        pressed = client.post(FAMILY_PLAN_ACTION, data={**refilled, "plan_date": LATER})
        ran = runs_recorded(client)
    assert page.status_code == 200
    assert loaded == (before, [])
    assert refilled["run_id"] not in {run_id, ""}
    assert pressed.is_redirect, family_line(pressed.text)
    assert admitted == [refilled["run_id"]]
    assert ran == [*before, (refilled["run_id"], LATER, "published")]


@pytest.mark.parametrize("case", ["overtaken", "not the newest", "passed", "published", "running"])
def test_family_check_page_refills_only_an_ended_run_still_ahead(case: str) -> None:
    """The check page refills its plan form only for a run that ended without a plan, wasn't
    overtaken, is the newest of its evening, and is for today or later. Otherwise the form
    holds today and Help with a plan stays closed."""
    run_id = uuid4().hex
    with browser(key=True) as client:
        drafts = state_of(client).drafts
        if case == "published":
            form = family_plan(client, "")
            published(client, FAMILY_PLAN_ACTION, form)
            run_id = form["run_id"]
        elif case == "running":
            drafts.admit_run(
                run_id, plan_date=LATER_EVENING, deadline_mono=monotonic() + RUN_DEADLINE_SECONDS
            )
        else:
            evening = date(2026, 8, 18) if case == "passed" else LATER_EVENING
            reason = "overtaken" if case == "overtaken" else "timed_out"
            ended_run(drafts, thread_id=run_id, plan_date=evening, outcome=reason)
            if case == "not the newest":
                ended_run(
                    drafts, thread_id=uuid4().hex, plan_date=LATER_EVENING, outcome="timed_out"
                )
        before = runs_recorded(client)
        page = client.get("/parent", params={"run": run_id}, headers=PAGE_HEADERS)
        after = runs_recorded(client)
    assert page.status_code == 200
    assert after == before
    assert plan_date_shown(page.text) == PLAN_DATE.isoformat()
    assert plan_fold_open(page.text) is False


# ------------------------------------------------- every row of both tables

LATER = LATER_EVENING.isoformat()
ROWS = (*HER_ROWS, *FAMILY_ROWS)
ANSWERED = tuple(row for row in ROWS if not row.lands)
"""The rows that answer on a page; the rest land on the page they planned for."""
FAMILY_KEPT: dict[str, str | None] = {
    "family-not-whole": LATER,
    "family-expired": LATER,
    "family-another-evening": LATER,
    "family-unreadable-date": None,
    "family-newer-plan": PLAN_DATE.isoformat(),
    "family-newer-plan-unread": PLAN_DATE.isoformat(),
    "family-plan-made": PLAN_DATE.isoformat(),
    "family-ended": PLAN_DATE.isoformat(),
    "family-not-a-date": None,
    "family-passed": "2026-08-18",
    "family-beyond": "9999-12-25",
    "family-already-planning": LATER,
    "family-not-saved": LATER,
    "family-could-not-start": LATER,
    "family-refused": LATER,
    "family-interrupted": LATER,
}
"""The date each family row's form holds, ``None`` for the page's own day; the running and
unconfirmed rows offer no form."""
CHOSEN = {
    "family-not-a-date": "someday",
    "family-passed": "2026-08-18",
    "family-beyond": "9999-12-25",
}
"""The date each family row refused for the date itself was chosen with."""
PLANNING_ANSWERS: dict[str, Callable[[], Exception]] = {
    "family-already-planning": lambda: AlreadyPlanning(
        RunState(
            run_id=f"plan:{LATER}:another",
            plan_date=LATER_EVENING,
            status="running",
            reason="running",
            seconds_left=40.0,
            plan_unchanged=True,
        )
    ),
    "family-not-saved": NotSaved,
    "family-could-not-start": CouldNotStart,
    "family-interrupted": lambda: RuntimeError("a step failed on the way"),
}
"""How planning answers each family row that comes back from it, as its own modules script it."""
NO_MODEL_ROWS = frozenset(
    {
        "family-not-whole",
        "family-expired",
        "family-ended",
        "family-not-a-date",
        "family-passed",
        "family-beyond",
    }
)
"""The family rows a press reaches without a model to plan with."""
NOTHING_TO_PLAN_ROWS = frozenset({"her-not-whole", "her-another-evening", "her-expired"})
"""Her rows a press reaches when nothing is left to plan."""
SIGNED_IN = {"BLOSSOM_STUDENT_PASSPHRASE": HERS, "BLOSSOM_PARENT_PASSPHRASE": THEIRS}


def too_slow(*_: object, **__: object) -> None:
    """A store read whose wait ended before it returned."""
    raise Unfinished(cast("asyncio.Future[Any]", Future()))


def nothing_left_to_do(client: TestClient) -> None:
    """Have every assignment reported done, so no evening has work left to plan."""
    project_state = state_of(client).project_state
    for item in project_state.all_assignments():
        project_state.report_status(
            item.assignment_id, "done", None, expected_head=None, now=REPORTED, today=PLAN_DATE
        )


def planning_answers(monkeypatch: pytest.MonkeyPatch, route: Any, answer: object) -> None:  # noqa: ANN401
    """Have ``route``'s planning answer with ``answer``: raised when it is an exception, or
    returned as the run's view, as its own modules script the planner's outcomes."""

    async def planned(*_: object, **__: object) -> object:
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(route, "make_plan", planned)


def prepared(
    client: TestClient, row: str, monkeypatch: pytest.MonkeyPatch, kept: str = ""
) -> tuple[str, dict[str, str]]:
    """The press that gives ``row``'s answer, with the state it needs, and the reads that row
    is about made to fail. Her forms carry ``kept`` as the cards kept in place."""
    drafts = state_of(client).drafts
    if row.startswith("her-"):
        return HER_PLAN_ACTION, her_press(client, row, monkeypatch, kept)
    fields = fresh_plan_fields(client)
    if row in {"family-not-whole", "family-expired"}:
        changed = {"stray": "1"} if row == "family-not-whole" else {"issued_at": ISSUED_LONG_AGO}
        return FAMILY_PLAN_ACTION, {**fields, "plan_date": LATER, **changed}
    if row in CHOSEN:
        return FAMILY_PLAN_ACTION, {**fields, "plan_date": CHOSEN[row]}
    if row == "family-refused":
        nothing_left_to_do(client)
        return FAMILY_PLAN_ACTION, {**fields, "plan_date": LATER}
    if row in PLANNING_ANSWERS:
        planning_answers(monkeypatch, parent_routes, PLANNING_ANSWERS[row]())
        return FAMILY_PLAN_ACTION, {**fields, "plan_date": LATER}
    form = {**fields, "plan_date": ""}
    if row in {"family-another-evening", "family-unreadable-date"}:
        published(client, FAMILY_PLAN_ACTION, form)
        chosen = LATER if row == "family-another-evening" else "someday"
        return FAMILY_PLAN_ACTION, {**form, "plan_date": chosen}
    if row == "family-running":
        drafts.admit_run(
            form["run_id"], plan_date=PLAN_DATE, deadline_mono=monotonic() + RUN_DEADLINE_SECONDS
        )
    elif row in {"family-newer-plan", "family-newer-plan-unread"}:
        published(client, FAMILY_PLAN_ACTION, fresh_plan_fields(client, plan_date=""))
        if row == "family-newer-plan-unread":
            monkeypatch.setattr(drafts, "latest_for", refusing(StoreBusy))
    elif row in {"family-plan-made", "family-plan-latest"}:
        published(client, FAMILY_PLAN_ACTION, form)
        if row == "family-plan-made":
            published(client, FAMILY_PLAN_ACTION, fresh_plan_fields(client, plan_date=""))
            monkeypatch.setattr(drafts, "latest_for", failing_once(StoreBusy, drafts.latest_for))
    elif row == "family-ended":
        ended_run(drafts, thread_id=form["run_id"], plan_date=PLAN_DATE, outcome="timed_out")
    elif row == "family-unconfirmed":
        planning_answers(monkeypatch, parent_routes, Unconfirmed(form["run_id"], PLAN_DATE))
    elif row == "family-ended-first":
        ended = SimpleNamespace(outcome="timed_out", draft_id=None)
        planning_answers(monkeypatch, parent_routes, SimpleNamespace(view=ended))
    return FAMILY_PLAN_ACTION, form


def her_press(
    client: TestClient, row: str, monkeypatch: pytest.MonkeyPatch, kept: str
) -> dict[str, str]:
    """Her press that gives ``row``'s answer, with the state it needs."""
    drafts = state_of(client).drafts
    form = {**fresh_plan_fields(client), "in_place": kept}
    if row == "her-not-whole":
        form["stray"] = "1"
    elif row == "her-another-evening":
        form["evening"] = "2026-08-18"
    elif row == "her-expired":
        form["issued_at"] = ISSUED_LONG_AGO
    elif row == "her-running":
        drafts.admit_run(
            form["run_id"], plan_date=PLAN_DATE, deadline_mono=monotonic() + RUN_DEADLINE_SECONDS
        )
    elif row == "her-newer-plan":
        published(client, HER_PLAN_ACTION, fresh_plan_fields(client))
    elif row in {"her-plan-made", "her-plan-latest"}:
        published(client, HER_PLAN_ACTION, form)
        if row == "her-plan-made":
            published(client, HER_PLAN_ACTION, fresh_plan_fields(client))
            monkeypatch.setattr(drafts, "latest_for", failing_once(StoreBusy, drafts.latest_for))
    elif row == "her-ended":
        ended_run(drafts, thread_id=form["run_id"], plan_date=PLAN_DATE, outcome="timed_out")
    elif row == "her-unconfirmed":
        planning_answers(monkeypatch, student_routes, Unconfirmed(form["run_id"], PLAN_DATE))
    elif row == "her-before":
        planning_answers(monkeypatch, student_routes, RuntimeError("a step failed on the way"))
    return form


def published(client: TestClient, action: str, form: dict[str, str]) -> None:
    """Press ``form`` and have it publish a plan."""
    answer = client.post(action, data=form, headers=PAGE_HEADERS)
    assert answer.is_redirect, (answer.status_code, line_of_page(answer.text))


def line_of_page(page: str) -> str:
    """The line either page answers with."""
    return her_line(page) or family_line(page)


def line_of(row: AnswerRow, page: str) -> str:
    """The line ``row``'s page answers with."""
    return her_line(page) if row.row.startswith("her-") else family_line(page)


def test_every_plan_row_is_in_a_table() -> None:
    """The rows the code names, answers and landings, are the rows the tables hold."""
    from blossom.routes.runs import PlanLanding, PlanRow

    assert {*get_args(PlanRow), *get_args(PlanLanding)} == {row.row for row in ROWS}


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row.row)
def test_each_press_is_answered_by_its_row(row: AnswerRow, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each press is answered by the row it names: an answer with that row's status and
    fact, whose first sentence takes the focus, and a plan form only when the row offers
    one, or a landing on its page."""
    from blossom.routes.runs import PlanAnswer

    answered: list[str] = []
    said = PlanAnswer.said

    def spied(answer: Any, shows: Callable[..., bool]) -> str:  # noqa: ANN401
        answered.append(answer.row)
        return said(answer, shows)

    def landing(name: str, response: Any) -> Any:  # noqa: ANN401
        answered.append(name)
        return response

    with browser(key=True) as client:
        action, form = prepared(client, row.row, monkeypatch)
        monkeypatch.setattr(PlanAnswer, "said", spied)
        for route in (student_routes, parent_routes):
            monkeypatch.setattr(route, "landed", landing)
        answer = client.post(action, data=form, headers=PAGE_HEADERS)
    assert answered == [row.row]
    assert answer.status_code == row.status
    if row.lands:
        assert answer.headers["location"].startswith(row.lands)
        return
    assert line_of(row, answer.text).startswith(row.fact)
    tag = HER_TOP_LINE if row.row.startswith("her-") else FAMILY_LINE
    assert opening_focused(answer.text, tag) == first_sentence(row.fact)
    assert (f'action="{action}"' in answer.text) is row.form


@pytest.mark.parametrize("failure", ["store error", "too slow"])
@pytest.mark.parametrize("row", ANSWERED, ids=lambda row: row.row)
def test_each_answer_on_the_stand_in_says_its_own_words(
    row: AnswerRow, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the page can't be read, or is read too late, the stand-in says the row's own
    words, its first sentence taking the focus, and nothing about a place, a date kept or a
    button."""
    failing = refusing() if failure == "store error" else too_slow
    with browser(key=True) as client:
        action, form = prepared(client, row.row, monkeypatch)
        page_read = "newest_published" if row.row.startswith("her-") else "review_snapshot"
        monkeypatch.setattr(state_of(client).drafts, page_read, failing)
        answer = client.post(action, data=form, headers=PAGE_HEADERS)
    line = YOUR_WEEK_NOT_SHOWN_LINE if row.row.startswith("her-") else FAMILY_NOT_SHOWN_LINE
    assert answer.status_code == row.status
    assert the_answer_alert(answer.text) == f"{row.stand_in} {line}"
    assert opening_focused(answer.text, STAND_IN_LINE) == first_sentence(row.stand_in)


TYPED = ["Dr. Tomorrow", "Read Ch. 3", "Q&A? Part 2!"]
"""Dates typed with a sentence's marks inside them, each followed by more words; the answer
quotes each one."""
TITLED = ["'Dr. Tomorrow'", "Read Ch. 3", "Q&A? Part 2!"]
"""Titles with a sentence's marks inside them: a closing quote, a period, a question mark and
an exclamation mark, each followed by more words."""


@pytest.mark.parametrize("failure", ["page", "store error", "too slow"])
@pytest.mark.parametrize("typed", TYPED)
def test_a_typed_date_with_marks_inside_keeps_the_whole_first_sentence_focused(
    typed: str, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A date typed with a sentence's marks inside it is quoted whole in the answer's first
    sentence, and that whole sentence takes the focus, on the family page and its stand-in."""
    opening = f"{typed!r} is not a date."
    with browser(key=True) as client:
        form = {**fresh_plan_fields(client), "plan_date": typed}
        if failure != "page":
            failing = refusing() if failure == "store error" else too_slow
            monkeypatch.setattr(state_of(client).drafts, "review_snapshot", failing)
        answer = client.post(FAMILY_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
    said = f"{opening} Use the form YYYY-MM-DD."
    assert answer.status_code == 422
    if failure == "page":
        assert family_line(answer.text) == said
        assert opening_focused(answer.text, FAMILY_LINE) == opening
    else:
        assert the_answer_alert(answer.text) == f"{said} {FAMILY_NOT_SHOWN_LINE}"
        assert opening_focused(answer.text, STAND_IN_LINE) == opening


@pytest.mark.parametrize("failure", ["page", "store error", "too slow"])
@pytest.mark.parametrize("title", TITLED)
def test_a_title_with_marks_inside_keeps_her_whole_first_sentence_focused(
    title: str, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that ended on a due date already passed opens with a short first sentence, and
    that first sentence alone takes the focus, however the work's title is punctuated: on her
    week, which names the work once, in its link, and on its stand-in, which has no link and
    names the work after the opening."""
    opening = "Blossom can't make today's plan."
    late = PastDueView(
        assignment_id=ESSAY_ID, title=title, course="Math", due_date=date(2026, 8, 18)
    )
    ended = SimpleNamespace(outcome="date_problem", draft_id=None, past_due=[late])
    with browser(key=True) as client:
        form = fresh_plan_fields(client)
        planning_answers(monkeypatch, student_routes, SimpleNamespace(view=ended))
        if failure != "page":
            failing = refusing() if failure == "store error" else too_slow
            monkeypatch.setattr(state_of(client).drafts, "newest_published", failing)
        answer = client.post(HER_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
    named = f"{title} (Math, due August 18)"
    saved = "Your homework updates are saved."
    assert answer.status_code == 409
    if failure == "page":
        assert her_line(answer.text) == (
            f"{ENDED_REASONS['date_problem']} {saved} Check the dates for {named}. See homework."
        )
        assert opening_focused(answer.text, HER_TOP_LINE) == opening
    else:
        assert the_answer_alert(answer.text) == (
            f"{opening} {named} has a due date that already passed, so no plan can finish it on "
            f"time. {saved} {YOUR_WEEK_NOT_SHOWN_LINE}"
        )
        assert opening_focused(answer.text, STAND_IN_LINE) == opening


PAST_DUE = [
    ("Read Ch. 3 and answer the review questions at the end", "Biology", date(2026, 8, 17)),
    ("Map quiz", "Geography", date(2026, 8, 18)),
    ("Canal Era timeline with five primary sources", "World History", date(2026, 8, 14)),
]
"""Work a run found due before today, as title, course and due date."""


@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("reader", ["her", "parent"])
def test_her_date_problem_names_each_assignment_once_in_its_link(
    reader: str, count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Her week's answer to a press that ended on due dates already passed says the problem
    once, naming no work, and names each assignment once, in its link to its dates, with its
    title, course and due date, to her and to a parent reading her page."""
    late = [
        PastDueView(assignment_id=f"assignment-late-{n}", title=title, course=course, due_date=day)
        for n, (title, course, day) in enumerate(PAST_DUE[:count])
    ]
    ended = SimpleNamespace(outcome="date_problem", draft_id=None, past_due=late)
    with browser(key=True, **SIGNED_IN) as client:
        sign_in_as(client, reader)
        form = fresh_plan_fields(client)
        planning_answers(monkeypatch, student_routes, SimpleNamespace(view=ended))
        answer = client.post(HER_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
    saved = (
        f"{HER_UPDATES_SAVED} {FAMILY_REVIEW_SHOWS}"
        if reader == "parent"
        else "Your homework updates are saved."
    )
    links = [
        f"Check the dates for {work.title} ({work.course}, due {work.due_date:%B} "
        f"{work.due_date.day})."
        for work in late
    ]
    line = her_line(answer.text)
    assert answer.status_code == 409
    assert line == f"{ENDED_REASONS['date_problem']} {saved} {' '.join(links)} See homework."
    assert [line.count(work.title) for work in late] == [1] * count
    for work, link in zip(late, links, strict=True):
        href = f"/student/assignments/{work.assignment_id}?return_to=week#evidence"
        assert f'<a href="{href}">{link}</a>' in answer.text
    assert opening_focused(answer.text, HER_TOP_LINE) == "Blossom can't make today's plan."


@pytest.mark.parametrize("row", [row for row in ANSWERED if row.form], ids=lambda row: row.row)
def test_each_answer_keeps_what_the_press_carried(
    row: AnswerRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every answer that offers a plan form keeps what the press carried that the page can
    show: her cards in place, or the family's date, in the open form under the line, which
    takes the focus, with no line claiming a date kept."""
    kept = f"a:{place_key(ESSAY_ID)}"
    with browser(key=True) as client:
        action, form = prepared(client, row.row, monkeypatch, kept)
        answer = client.post(action, data=form, headers=PAGE_HEADERS)
    assert answer.status_code == row.status
    if row.row.startswith("her-"):
        assert form_fields(answer.text, HER_PLAN_ACTION).get("in_place") == kept
        return
    held = FAMILY_KEPT[row.row]
    assert plan_date_shown(answer.text) == (PLAN_DATE.isoformat() if held is None else held)
    assert plan_fold_open(answer.text) is True
    assert family_line_focused(answer.text)
    assert "kept" not in family_line(answer.text)


@pytest.mark.parametrize(
    "row", [row for row in FAMILY_ROWS if row.row in NO_MODEL_ROWS], ids=lambda row: row.row
)
def test_family_answer_without_a_plan_form_names_none(
    row: AnswerRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no model to plan with, the family page shows no plan form, so no answer names
    Plan it or a date kept below."""
    with browser(key=False) as client:
        action, form = prepared(client, row.row, monkeypatch)
        answer = client.post(action, data=form, headers=PAGE_HEADERS)
    line = family_line(answer.text)
    assert answer.status_code == row.status
    assert line.startswith(row.fact)
    assert plan_date_shown(answer.text) is None
    assert "press Plan it" not in line
    assert "kept below" not in line
    assert "choose a date" not in line


@pytest.mark.parametrize(
    "row", [row for row in HER_ROWS if row.row in NOTHING_TO_PLAN_ROWS], ids=lambda row: row.row
)
def test_her_answer_with_nothing_to_plan_names_no_button(
    row: AnswerRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With everything reported done, her week shows no plan button, so no answer names one."""
    with browser(key=True) as client:
        for item in state_of(client).project_state.all_assignments():
            report(client, item.assignment_id, "done")
        action, form = prepared(client, row.row, monkeypatch)
        answer = client.post(action, data=form, headers=PAGE_HEADERS)
    assert answer.status_code == row.status
    assert f'action="{HER_PLAN_ACTION}"' not in answer.text
    assert her_line(answer.text) == row.fact


def test_family_newer_plan_decided_for_a_later_evening_is_under_earlier_plans() -> None:
    """A newer plan already decided for an evening after today is shown under Earlier plans,
    whose fold the answer opens."""
    later_plan = fixture_week_plan().model_copy(update={"plan_date": LATER_EVENING})
    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [later_plan], lambda: [accepting()]
        )
        stale = family_plan(client, LATER)
        published(client, FAMILY_PLAN_ACTION, family_plan(client, LATER))
        newer = state_of(client).drafts.latest_for(LATER_EVENING)
        assert newer is not None
        state_of(client).drafts.record_decision(
            newer.draft_id,
            status=DraftStatus.APPROVED_FOR_MANUAL_SEND,
            decision="approved",
            reason=None,
        )
        answer = client.post(FAMILY_PLAN_ACTION, data=stale)
    text = answer.text
    assert answer.status_code == 409
    assert family_line(text) == (
        f"{FAMILY_NEWER_PLAN_AUGUST_20} {SHOWN_BELOW_UNDER_EARLIER_PLANS} {FAMILY_FOR_A_NEW_PLAN}"
    )
    assert re.search(r'<details class="steps panel-fold" open>\s*<summary>Earlier plans', text)
    earlier = text.split("<summary>Earlier plans</summary>", 1)[1]
    assert '<article class="draft decided-approved">' in earlier
    assert plan_date_shown(text) == LATER


REFUSALS = {
    "her": ["another origin", "signed out"],
    "family": ["her", "another origin", "signed out"],
}
"""How the gate refuses each route's press: her on the family page, another origin, and a
reader who signed out."""


@pytest.mark.parametrize(
    ("row", "refusal"),
    [
        (row, refusal)
        for row in ROWS
        for refusal in REFUSALS["her" if row.row.startswith("her-") else "family"]
    ],
    ids=lambda value: value.row if isinstance(value, AnswerRow) else value,
)
def test_each_press_refused_at_the_door_reads_no_run(
    row: AnswerRow, refusal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A press the gate refuses, used form or not, reads no run, plans nothing and shows no
    outcome, whichever answer it would have had."""
    hers = row.row.startswith("her-")
    with browser(key=True, **SIGNED_IN) as client:
        sign_in_as(client, "her" if hers else "parent")
        action, form = prepared(client, row.row, monkeypatch)
        before = runs_recorded(client)
        read: list[str] = []
        drafts = state_of(client).drafts
        for name in ("run_status", "latest_for", "admit_run", "newest_published"):
            monkeypatch.setattr(drafts, name, lambda *_, name=name, **__: read.append(name))
        for route in (student_routes, parent_routes):
            planning_answers(monkeypatch, route, AssertionError("planned past the door"))
        headers = dict(PAGE_HEADERS)
        if refusal == "her":
            sign_in_as(client, "her")
        elif refusal == "another origin":
            headers["Origin"] = "http://elsewhere.example"
        else:
            assert client.post("/sign-out", headers=PAGE_HEADERS).status_code < 400
        answer = client.post(action, data=form, headers=headers)
        monkeypatch.undo()
        after = runs_recorded(client)
    assert read == []
    assert after == before
    assert answer.status_code in {303, 403}
    if answer.status_code == 303:
        assert answer.headers["location"].startswith("/sign-in")
    for said in (row.fact, "already asked", "newer plan", "still being finished"):
        assert not said or said not in answer.text


@pytest.mark.parametrize("route", ["her", "family"])
def test_the_same_form_twice_makes_one_run(route: str) -> None:
    """A form pressed twice plans once."""
    with browser(key=True) as client:
        if route == "her":
            form, action = plan_form(client), HER_PLAN_ACTION
        else:
            form, action = family_plan(client, ""), FAMILY_PLAN_ACTION
        first = client.post(action, data=form, headers=PAGE_HEADERS)
        second = client.post(action, data=form, headers=PAGE_HEADERS)
        ran = runs_recorded(client)
    assert first.status_code == second.status_code == 303
    assert [run for run, _, _ in ran] == [form["run_id"]]


# ------------------------------------------------- a run keeps the past-due work it names

LONG_TITLE = "Photosynthesisandcellularrespirationvocabularyreview"
LONG_COURSE = "Environmentalscienceandsustainability"
KEPT_WORK = {
    "one": [("assignment-map-quiz", "Map quiz", "Geography", "2026-08-18")],
    "three": [
        (
            "assignment-late-reading",
            "Read Ch. 3 and answer the review questions at the end",
            "Biology",
            "2026-08-17",
        ),
        ("assignment-map-quiz", "Map quiz", "Geography", "2026-08-18"),
        (
            "assignment-late-timeline",
            "Canal Era timeline with five primary sources",
            "World History",
            "2026-08-14",
        ),
    ],
    "long words": [("assignment-long-words", LONG_TITLE, LONG_COURSE, "2026-08-18")],
}
"""Work on record whose only date, the portal's, is before today's evening, as id, title,
course and that date."""
DATE_LINK = re.compile(r'<a href="([^"]*)">(Check the dates for [^<]*)</a>')
PLAN_RUN_NOTICE = re.compile(r'<p [^>]*id="plan-run">(.*?)</p>', re.S)


def past_due_on_record(
    client: TestClient, work: list[tuple[str, str, str, str]], planners: list[Scripted[Any]]
) -> None:
    """``work`` on record with only the portal's date, and graphs with scripted models, each
    planner built added to ``planners``."""
    state = state_of(client)
    state.project_state.upsert_assignments(
        [
            Assignment(
                assignment_id=name,
                course=course,
                title=title,
                due_date=None,
                dependencies=[],
                reported_submission_status="not_started",
            )
            for name, title, course, _ in work
        ]
    )
    for name, _, _, day in work:
        state.project_state.record_claims(name, [record(SourceChannel.LMS, day)])
    client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
        lambda: [fixture_week_plan()], lambda: [accepting()], planners=planners
    )


def date_links(html: str) -> list[tuple[str, str]]:
    """Every "Check the dates" link in ``html``, as href and words, in order."""
    return DATE_LINK.findall(html)


def plan_run_notice(page: str) -> str:
    """The HTML inside her week's notice about a planning run, or empty."""
    found = PLAN_RUN_NOTICE.search(page)
    return "" if found is None else found.group(1)


async def form_run_unread(*_: object, **__: object) -> None:
    """A read of the form's run that finds nothing, so admission decides."""
    return None


@pytest.mark.parametrize("case", list(KEPT_WORK))
@pytest.mark.parametrize("reader", ["her", "parent"])
def test_a_used_form_says_the_first_answers_line_and_date_links(
    reader: str, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The used form pressed again, and again when admission is what finds its run, says the
    first answer's line and its "Check the dates" links, the same ones in the same order, from
    the work the run kept: one run, and no model asked."""
    planners: list[Scripted[Any]] = []
    with browser(key=True, **SIGNED_IN) as client:
        sign_in_as(client, reader)
        past_due_on_record(client, KEPT_WORK[case], planners)
        form = plan_form(client)
        first = client.post(HER_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
        again = client.post(HER_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
        monkeypatch.setattr(student_routes, "run_of_the_form", form_run_unread)
        admitted = client.post(HER_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
        runs = runs_recorded(client)
    links = date_links(first.text)
    expected = [
        (
            f"/student/assignments/{name}?return_to=week#evidence",
            f"Check the dates for {title} ({course}, due August {int(day[-2:])}).",
        )
        for name, title, course, day in KEPT_WORK[case]
    ]
    assert sorted(links) == sorted(expected)
    for answer in (first, again, admitted):
        assert answer.status_code == 409
        assert her_line(answer.text) == her_line(first.text)
        assert date_links(answer.text) == links
        assert opening_focused(answer.text, HER_TOP_LINE) == "Blossom can't make today's plan."
    assert [run for run, _, _ in runs] == [form["run_id"]]
    # Admission builds a graph before it finds the run; the graph is never asked.
    assert [planner.calls for planner in planners] == [0, 0]


@pytest.mark.parametrize("case", list(KEPT_WORK))
@pytest.mark.parametrize("reader", ["her", "parent"])
def test_a_used_forms_stand_in_names_the_same_work_as_the_first(
    reader: str, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When her week can't be read, the stand-in of the first answer and of the used form
    pressed again say the same words, naming each past-due assignment once."""
    planners: list[Scripted[Any]] = []
    with browser(key=True, **SIGNED_IN) as client:
        sign_in_as(client, reader)
        past_due_on_record(client, KEPT_WORK[case], planners)
        form = plan_form(client)
        monkeypatch.setattr(state_of(client).drafts, "newest_published", refusing())
        first = client.post(HER_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
        again = client.post(HER_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
    said = the_answer_alert(first.text)
    assert first.status_code == again.status_code == 409
    assert said.startswith("Blossom can't make today's plan. ")
    assert the_answer_alert(again.text) == said
    assert [said.count(title) for _, title, _, _ in KEPT_WORK[case]] == [1] * len(KEPT_WORK[case])
    assert [planner.calls for planner in planners] == [0]


def test_a_run_that_runs_out_of_time_as_it_ends_links_no_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A date problem whose run's time has passed when the record ends it is recorded as timed
    out: the first answer and the used form pressed again both say the time ran out, with no
    "Check the dates" link."""
    planners: list[Scripted[Any]] = []
    found_late = past_deadlines
    with browser(key=True) as client:
        past_due_on_record(client, KEPT_WORK["one"], planners)
        drafts = state_of(client).drafts

        def late(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            # The store's clock passes the run's deadline while the week is read.
            monkeypatch.setattr(drafts, "_monotonic", lambda: 1e12)
            return found_late(*args, **kwargs)

        monkeypatch.setattr(graph_module, "past_deadlines", late)
        form = plan_form(client)
        first = client.post(HER_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
        again = client.post(HER_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
        outcomes = [run.outcome for run in drafts.runs_without_a_draft()]
    assert outcomes == ["timed_out"]
    for answer in (first, again):
        assert answer.status_code == 409
        assert her_line(answer.text).startswith(ENDED_REASONS["timed_out"])
        assert date_links(answer.text) == []
    assert her_line(again.text) == her_line(first.text)


@pytest.mark.parametrize("reader", ["her", "parent"])
def test_a_used_form_after_a_newer_plan_for_today_says_the_newer_plan(reader: str) -> None:
    """After a date problem, the work is reported done and a fresh press makes today's plan:
    the first, used form pressed again says a newer plan was made, and the address naming
    its run links no work."""
    planners: list[Scripted[Any]] = []
    with browser(key=True, **SIGNED_IN) as client:
        sign_in_as(client, reader)
        past_due_on_record(client, KEPT_WORK["one"], planners)
        used = plan_form(client)
        first = client.post(HER_PLAN_ACTION, data=used, headers=PAGE_HEADERS)
        reported(state_of(client).project_state, "done", "assignment-map-quiz")
        made = client.post(HER_PLAN_ACTION, data=plan_form(client), headers=PAGE_HEADERS)
        again = client.post(HER_PLAN_ACTION, data=used, headers=PAGE_HEADERS)
        asked = client.get(HER_PAGE, params={"run": used["run_id"]}, headers=PAGE_HEADERS)
    assert first.status_code == 409
    assert made.status_code == 303
    assert again.status_code == 409
    assert her_line(again.text).startswith(f"{HER_NEWER_PLAN} It is shown below.")
    assert date_links(again.text) == []
    assert date_links(asked.text) == []
    assert [planner.calls for planner in planners] == [0, 1]


@pytest.mark.parametrize("reader", ["her", "parent"])
def test_a_run_that_kept_no_work_says_the_general_sentence(reader: str) -> None:
    """A date-problem run whose record keeps no past-due work, as one from before runs kept
    it, is answered with the general sentence and no "Check the dates" link, though past-due
    work is on today's record."""
    planners: list[Scripted[Any]] = []
    with browser(key=True, **SIGNED_IN) as client:
        sign_in_as(client, reader)
        past_due_on_record(client, KEPT_WORK["one"], planners)
        form = plan_form(client)
        ended_run(
            state_of(client).drafts,
            thread_id=form["run_id"],
            plan_date=PLAN_DATE,
            outcome="date_problem",
        )
        again = client.post(HER_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
        loaded = client.get(HER_PAGE, headers=PAGE_HEADERS)
    assert again.status_code == 409
    assert her_line(again.text).startswith(f"{ENDED_REASONS['date_problem']} ")
    assert "Map quiz" not in her_line(again.text)
    assert date_links(again.text) == []
    assert ENDED_REASONS["date_problem"] in words(plan_run_notice(loaded.text))
    assert date_links(loaded.text) == []
    assert planners == []


def test_a_kept_list_that_cannot_be_read_is_said_as_none(caplog: pytest.LogCaptureFixture) -> None:
    """A run's kept work that can't be read is logged and read as none: her week loads, and the
    used form pressed again says the general sentence with no link."""
    planners: list[Scripted[Any]] = []
    with browser(key=True) as client:
        past_due_on_record(client, KEPT_WORK["one"], planners)
        form = plan_form(client)
        first = client.post(HER_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
        connection = sqlite3.connect(database_of(client))
        try:
            connection.execute(
                "UPDATE runs SET past_due=? WHERE thread_id=?", ('[{"title": 3', form["run_id"])
            )
            connection.commit()
        finally:
            connection.close()
        loaded = client.get(HER_PAGE, headers=PAGE_HEADERS)
        again = client.post(HER_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
    assert len(date_links(first.text)) == 1
    assert loaded.status_code == 200
    assert date_links(loaded.text) == []
    assert again.status_code == 409
    assert her_line(again.text).startswith(f"{ENDED_REASONS['date_problem']} ")
    assert date_links(again.text) == []
    assert f"the past-due work of run {form['run_id']} could not be read" in caplog.text


@pytest.mark.parametrize("reader", ["her", "parent"])
def test_her_weeks_notices_for_the_run_link_the_same_work(reader: str) -> None:
    """Her week on load, and with the address naming the run, says the run's notice in its own
    words and then the first answer's "Check the dates" links, the same ones in the same
    order; the family page's notice for the run links none."""
    planners: list[Scripted[Any]] = []
    with browser(key=True, **SIGNED_IN) as client:
        sign_in_as(client, reader)
        past_due_on_record(client, KEPT_WORK["three"], planners)
        form = plan_form(client)
        first = client.post(HER_PLAN_ACTION, data=form, headers=PAGE_HEADERS)
        loaded = client.get(HER_PAGE, headers=PAGE_HEADERS)
        asked = client.get(HER_PAGE, params={"run": form["run_id"]}, headers=PAGE_HEADERS)
        family = (
            client.get("/parent", params={"run": form["run_id"]}, headers=PAGE_HEADERS)
            if reader == "parent"
            else None
        )
    links = date_links(first.text)
    labels = " ".join(label for _, label in links)
    assert len(links) == 3
    for page in (loaded, asked):
        notice = plan_run_notice(page.text)
        assert words(notice).startswith(f"{ENDED_REASONS['date_problem']} ")
        assert words(notice).endswith(f" {labels}")
        assert date_links(notice) == links
        assert date_links(page.text) == links
    if family is not None:
        notice = plan_run_notice(family.text)
        assert "Some work has a due date that already passed" in words(notice)
        assert date_links(family.text) == []
