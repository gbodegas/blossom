# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Today's plan is hers: asked for from her page, shown the moment it is made, reviewed after.

The parent's review is shown under the plan and never stands between her and
it. These tests hold her page, her JSON routes, and the parent's review to
that, with scripted models so nothing is ever sent.
"""

import asyncio
import pathlib
import re
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from time import monotonic
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.agent.graph import MAX_REVISIONS, Ask, CompiledPlanGraph, ModelAnswer
from blossom.agent.runs import MODEL_RETRIES, RUN_DEADLINE_SECONDS
from blossom.anthropic_client import ServiceBusy, ServiceFailed
from blossom.app import create_app
from blossom.clock import FrozenClock, spoken_time
from blossom.dependencies import STATE_ATTRIBUTE, ApplicationState
from blossom.drafts import Draft
from blossom.heuristic_relevance import Criterion, CriterionFinding, CriticVerdict, Judgment
from blossom.noticing import read_week
from blossom.plan_reading import Reader, anchor_for
from blossom.plan_snapshot import read_snapshot
from blossom.plan_text import present_plan
from blossom.plans import DailyPlan
from blossom.reconciliation import SourceChannel
from blossom.routes import runs as run_routes
from blossom.routes import student as student_routes
from blossom.routes.runs import (
    NOTHING_TO_SCHEDULE,
    AlreadyPlanning,
    CouldNotStart,
    NotSaved,
    PlanGraphs,
    Unconfirmed,
    already_planning,
    ended_sentences,
    ended_without_a_plan,
    plan_graphs,
)
from blossom.settings import ANTHROPIC_API_KEY_VARIABLE
from blossom.stores.drafts import DraftRecord, DraftsStore, RunState, StoreBusy
from blossom.stores.project_state import Assignment
from blossom.views import PastDueView
from tests.support import (
    ESSAY_ID,
    FIXTURE_TIMEZONE,
    HER_FOR_A_NEW_PLAN,
    HER_FORM_EXPIRED,
    HER_FORM_FROM_AUGUST_18,
    HER_FORM_FROM_AUGUST_20,
    HER_FORM_NOT_WHOLE,
    HER_NEWER_PLAN,
    HER_TOP_LINE,
    PLAN_DATE,
    SAME_ORIGIN,
    THEIRS,
    FakeTime,
    Scripted,
    Spending,
    accepting,
    changed_by_hand,
    client_for,
    drafts_in_memory,
    ended_run,
    fixture_settings,
    fixture_week_plan,
    forgetful_fixture_plan,
    form_fields,
    fresh_plan_fields,
    light_fixture_plan,
    model_graphs,
    ok,
    opening_focused,
    plan_form,
    plan_on,
    record,
    refusing,
    report,
    runs_recorded,
    scripted_graphs,
    settled_run,
    signed_in,
    signed_in_household,
    state_of,
    status_of,
    unwrapped,
    words,
)

PAGE = "/student/due-this-week"
CREATED = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)


def draft(draft_id: str, body: str, *, hour: int = 22) -> Draft:
    return Draft(draft_id=draft_id, body=body, created_at=CREATED.replace(hour=hour))


def undecided() -> CriticVerdict:
    """A reviewer that could not settle: one criterion it could not tell about."""
    return CriticVerdict(
        findings=[
            CriterionFinding(
                criterion=Criterion.SUPPORT_RULES,
                critique="no rules were given",
                judgment=Judgment.CANNOT_TELL,
            )
        ]
    )


def browser(
    *,
    key: bool = True,
    plan: Callable[[], DailyPlan] = fixture_week_plan,
    critic: Callable[[], CriticVerdict] = accepting,
    clock: Callable[[], float] = monotonic,
    real_clock: FrozenClock | None = None,
) -> TestClient:
    """Her page with scripted models. ``key`` false is a household without an API key;
    ``clock`` is the monotonic clock its runs are timed on, and ``real_clock`` pins real time."""
    environ = {ANTHROPIC_API_KEY_VARIABLE: "not-a-key-and-never-sent"} if key else {}
    app = create_app(
        fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **environ),
        monotonic=clock,
        real_clock=real_clock,
    )
    if key:
        app.dependency_overrides[plan_graphs] = scripted_graphs(
            lambda: [plan()], lambda: [critic()]
        )
    return TestClient(app, follow_redirects=False, headers=SAME_ORIGIN)


SHOWN = f"{PAGE}?show_plan=1"


def whole(record: DraftRecord, page: str) -> bool:
    """Whether a saved plan is on the page once, read by its rows: each block's time,
    assignment and reason, and each assignment put off with its reason, in a row of its own,
    with every date to clarify and every note of the review, and no copy of the text beside
    them."""
    snapshot = read_snapshot(
        record.draft_id,
        record.plan_snapshot,
        plan_date=record.plan_date,
        plan_assignment_ids=record.plan_assignment_ids,
    ).snapshot
    assert snapshot is not None
    plan = plan_on(page, record)
    anchor = anchor_for(record.draft_id)
    named = snapshot.assignments

    def row(dom_id: str, *parts: str) -> bool:
        found = re.search(rf'<li class="plan-[a-z]+[^"]*" id="{dom_id}">(.*?)</li>', plan, re.S)
        return found is not None and all(str(escape(part)) in found.group(1) for part in parts)

    blocks = [
        row(
            f"{anchor}-block-{index}",
            f"{spoken_time(block.starts_at)} to {spoken_time(block.ends_at)}",
            named[block.assignment_id].title,
            named[block.assignment_id].course,
            block.rationale,
        )
        for index, block in enumerate(snapshot.plan.blocks)
    ]
    deferrals = [
        row(
            f"{anchor}-deferral-{index}",
            named[item.assignment_id].title,
            named[item.assignment_id].course,
            item.reason,
        )
        for index, item in enumerate(snapshot.plan.deferred)
    ]
    review = [] if snapshot.review is None else snapshot.review.findings
    notes = [str(escape(f"{finding.label}:")) for finding in review]
    notes += [str(escape(finding.text)) for finding in review]
    notes += [str(escape(item.text)) for item in snapshot.clarifications]
    rows = len(re.findall(r'<li class="plan-(?:block|deferral)', plan))
    return (
        all(blocks + deferrals)
        and rows == len(blocks) + len(deferrals)
        and all(note in plan for note in notes)
        and "<pre" not in plan
        and "Original saved text" not in plan
    )


# --------------------------------------------------------------- the store


def test_the_latest_plan_for_an_evening_is_read_whatever_was_said_about_it() -> None:
    store: DraftsStore = drafts_in_memory()
    first = draft("draft:plan:a", "First plan")
    second = draft("draft:plan:b", "Second plan", hour=23)
    settled_run(store, first, thread_id="plan:a", plan_date=PLAN_DATE)
    settled_run(store, second, thread_id="plan:b", plan_date=PLAN_DATE)
    store.record_decision(
        "draft:plan:b",
        status=first.status,
        decision="rejected",
        reason="try again",
    )

    latest = store.latest_for(PLAN_DATE)

    assert latest is not None
    assert latest.draft_id == "draft:plan:b"
    assert latest.decision == "rejected"
    assert store.latest_for(date(2026, 8, 20)) is None


# ---------------------------------------------------------------- her page


def test_her_page_offers_to_plan_today_and_says_why_it_cannot_without_a_key() -> None:
    with browser() as client:
        with_key = client.get(PAGE).text
    with browser(key=False) as client:
        without = client.get(PAGE).text

    assert "Plan today" in with_key
    assert 'action="/student/actions/plan"' in with_key
    assert "No plan for today yet." in with_key
    assert "Planning is unavailable right now" in without
    assert 'action="/student/actions/plan"' not in without
    assert "Too much right now" in without


def test_the_plan_is_on_her_page_the_moment_it_is_made() -> None:
    with browser() as client:
        planned = client.post("/student/actions/plan", data=plan_form(client))
        page = client.get(PAGE).text
        today = client.get("/student/plans/today").json()
        queue = client.get("/parent/approvals").json()["waiting"]

    assert planned.status_code == 303
    assert planned.headers["location"] == SHOWN
    assert "Plan for Wednesday, August 19, 2026" in page
    assert "A plan is ready." in page
    assert "A parent has not reviewed it yet." in page
    assert ">Plan again<" in page
    assert today["decision"] is None
    assert today["stale"] is None
    assert today["too_much"] is False
    assert [item["draft_id"] for item in queue] == [today["draft_id"]]


def test_asking_over_json_answers_with_the_plan() -> None:
    with browser() as client:
        made = client.post("/student/plans")
        today = client.get("/student/plans/today").json()

    assert made.status_code == 201
    assert made.json()["draft_id"] == today["draft_id"]
    assert made.json()["body"].startswith("Plan for Wednesday, August 19, 2026")


def test_without_a_plan_today_is_a_404() -> None:
    with browser() as client:
        response = client.get("/student/plans/today")

    assert response.status_code == 404


def test_without_a_key_the_page_says_so_and_keeps_working() -> None:
    with browser(key=False) as client:
        response = client.post("/student/actions/plan", data=fresh_plan_fields(client))
        over_json = client.post("/student/plans")

    assert response.status_code == 503
    assert "Blossom could not make a plan:" in response.text
    assert "<h1>My week</h1>" in response.text
    assert over_json.status_code == 503


def test_why_there_is_no_plan_is_said_in_plain_words_to_whoever_reads_it() -> None:
    """Her page never shows the run's code, and never says her homework is too much. Each
    way a run can fail is told apart: an answer that couldn't be used, time running out,
    the service, and a date already passed, named only from the record. Her updates are
    always said to be saved; a parent reading her page is sent to the family page, where
    the run's record is."""
    invalid = "Blossom couldn't finish a reliable plan this time."
    for outcome in ("checks_failed", "model_truncated", "model_refused", "model_unparseable"):
        assert ended_without_a_plan(outcome, parent=False) == (
            f"{invalid} Your homework updates are saved."
        )
        assert ended_without_a_plan(outcome, parent=True) == (
            f"{invalid} Her homework updates are saved. Family review shows what happened."
        )
    assert ended_without_a_plan("timed_out", parent=False) == (
        "Planning took too long, so Blossom stopped. Your homework updates are saved."
    )
    assert ended_without_a_plan("service_failed", parent=False) == (
        "Blossom couldn't get a plan from the planning service this time. Your homework updates "
        "are saved."
    )
    assert ended_without_a_plan("service_failed", parent=True) == (
        "Blossom couldn't get a plan from the planning service this time. Her homework updates "
        "are saved. Family review shows what happened."
    )
    quiz = PastDueView(
        assignment_id="assignment-map-quiz",
        title="Map quiz",
        course="Geography",
        due_date=date(2026, 8, 18),
    )
    assert ended_without_a_plan("date_problem", parent=False, past_due=[quiz]) == (
        "Blossom can't make today's plan. Map quiz (Geography, due August 18) has a due date "
        "that already passed, so no plan can finish it on time. Your homework updates are saved."
    )
    atlas = PastDueView(
        assignment_id="assignment-atlas-page",
        title="Atlas page",
        course="Art",
        due_date=date(2026, 8, 17),
    )
    assert ended_without_a_plan("date_problem", parent=True, past_due=[quiz, atlas]) == (
        "Blossom can't make today's plan. Map quiz (Geography, due August 18) and Atlas page "
        "(Art, due August 17) have due dates that already passed, so no plan can finish them "
        "on time. Her homework updates are saved. Family review shows what happened."
    )
    assert ended_without_a_plan("date_problem", parent=False) == (
        "Blossom can't make today's plan. Some work has a due date that already passed, so no "
        "plan can finish it on time. Your homework updates are saved."
    ), "nothing is named that the record does not show"
    assert ended_without_a_plan("date_problem", parent=False, evening=date(2026, 8, 20)) == (
        "Blossom can't make the plan for Thursday, August 20. Some work is due before that "
        "evening, so no plan can finish it on time. Your homework updates are saved."
    )
    openings = [
        ended_sentences("date_problem", parent=False, past_due=named)[0]
        for named in ([quiz], [quiz, atlas], [])
    ]
    assert openings == ["Blossom can't make today's plan."] * 3, "the work follows the opening"
    assert ended_sentences("date_problem", parent=True, evening=date(2026, 8, 20))[0] == (
        "Blossom can't make the plan for Thursday, August 20."
    )
    for outcome in ("checks_failed", "timed_out", "service_failed", "date_problem"):
        assert "fits this evening" not in ended_without_a_plan(outcome, parent=False)
    assert ended_without_a_plan("nothing_to_schedule", parent=False) == NOTHING_TO_SCHEDULE
    assert ended_without_a_plan("nothing_to_schedule", parent=True) == NOTHING_TO_SCHEDULE


def test_a_parent_who_plans_from_her_page_is_told_where_the_record_is(
    tmp_path: pathlib.Path,
) -> None:
    client = client_for(signed_in_household(tmp_path))
    client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
        lambda: [forgetful_fixture_plan()] * 3, lambda: [accepting()]
    )
    with client:
        signed_in(client, THEIRS)
        response = client.post("/student/actions/plan", data=fresh_plan_fields(client))

    assert response.status_code == 409
    assert (
        "Blossom couldn&#39;t finish a reliable plan this time. Her homework updates are saved. "
        "Family review shows what happened."
    ) in unwrapped(response.text)
    assert "Your homework updates" not in response.text


def test_a_run_that_ends_without_a_plan_is_said_and_the_page_keeps_its_plan() -> None:
    app = create_app(
        fixture_settings(
            BLOSSOM_TODAY=PLAN_DATE.isoformat(), ANTHROPIC_API_KEY="not-a-key-and-never-sent"
        )
    )
    app.dependency_overrides[plan_graphs] = scripted_graphs(
        lambda: [forgetful_fixture_plan()] * 3, lambda: [accepting()]
    )
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        response = client.post("/student/actions/plan", data=plan_form(client))
        over_json = client.post("/student/plans")

    assert response.status_code == 409
    assert (
        "Blossom couldn&#39;t finish a reliable plan this time. Your homework updates are "
        "saved." in unwrapped(response.text)
    )
    assert "checks_failed" not in response.text
    assert "No plan for today yet." in response.text
    assert over_json.status_code == 409
    assert over_json.json()["detail"] == (
        "Blossom couldn't finish a reliable plan this time. Your homework updates are saved."
    )


def test_a_failure_on_the_way_is_said_on_the_page_and_the_plan_stays() -> None:
    """A planner that raises is not a refusal: the run ends interrupted, both answers say
    Blossom couldn't finish a reliable plan and that her updates are saved, with the status
    of that outcome, and the page keeps its plan."""
    app = create_app(
        fixture_settings(
            BLOSSOM_TODAY=PLAN_DATE.isoformat(), ANTHROPIC_API_KEY="not-a-key-and-never-sent"
        )
    )
    app.dependency_overrides[plan_graphs] = scripted_graphs(
        lambda: [fixture_week_plan()], lambda: [accepting()]
    )
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/student/actions/plan", data=plan_form(client))
        app.dependency_overrides[plan_graphs] = scripted_graphs(list, lambda: [accepting()])
        response = client.post("/student/actions/plan", data=plan_form(client))
        over_json = client.post("/student/plans")
        today = client.get("/student/plans/today").json()
        ended = state_of(client).drafts.latest_run()

    assert response.status_code == 409
    assert (
        "Blossom couldn&#39;t finish a reliable plan this time. Your homework updates are "
        "saved." in the_line(response.text)
    )
    assert "went wrong" not in response.text
    assert TRY_AGAIN in response.text
    assert "Plan for Wednesday, August 19, 2026" in response.text
    assert over_json.status_code == 409
    assert over_json.json()["detail"] == (
        "Blossom couldn't finish a reliable plan this time. Your homework updates are saved."
    )
    assert ended is not None
    assert (ended.status, ended.reason) == ("ended", "interrupted")
    assert today["decision"] is None


def test_a_held_review_that_cannot_be_read_is_said_as_a_plan_not_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run whose held reviews can't be read before its settle ends interrupted, publishes
    nothing, and both of her answers say Blossom couldn't finish a reliable plan, with the
    saved sentence, never a bare error."""

    async def unreadable(*args: object, **kwargs: object) -> object:
        msg = "the saved review could not be read"
        raise RuntimeError(msg)

    monkeypatch.setattr(run_routes, "finish_held_reviews", unreadable)
    with browser() as client:
        page = client.post("/student/actions/plan", data=plan_form(client))
        over_json = client.post("/student/plans")
        state = state_of(client)
        ended = state.drafts.latest_run()
        latest = state.drafts.latest_for(PLAN_DATE)

    assert page.status_code == 409
    assert (
        "Blossom couldn&#39;t finish a reliable plan this time. Your homework updates are "
        "saved." in the_line(page.text)
    )
    assert over_json.status_code == 409
    assert over_json.json()["detail"] == (
        "Blossom couldn't finish a reliable plan this time. Your homework updates are saved."
    )
    assert ended is not None
    assert (ended.status, ended.reason) == ("ended", "interrupted")
    assert latest is None


def test_planning_again_replaces_the_plan_on_both_pages() -> None:
    with browser() as client:
        client.post("/student/actions/plan", data=plan_form(client))
        first = client.get("/student/plans/today").json()["draft_id"]
        client.post("/student/actions/plan", data=plan_form(client))
        today = client.get("/student/plans/today").json()
        queue = client.get("/parent/approvals").json()["waiting"]
        earlier = client.get(f"/parent/approvals/{first}").json()

    assert today["draft_id"] != first
    assert [item["draft_id"] for item in queue] == [today["draft_id"]]
    assert earlier["decision"] == "superseded"


# ------------------------------------------------------- the parent's review


def test_a_parents_review_shows_under_her_plan_and_never_blocks_it() -> None:
    with browser() as client:
        client.post("/student/actions/plan", data=plan_form(client))
        draft_id = client.get("/student/plans/today").json()["draft_id"]
        before = client.get(PAGE).text
        client.post(
            f"/parent/approvals/{draft_id}", json={"approved": True, "reason": "good pacing"}
        )
        after = client.get(PAGE).text
        today = client.get("/student/plans/today").json()

    assert "A parent has not reviewed it yet." in before
    assert "From your parents" not in before
    assert "From your parents" in after
    assert "Looks good." in after
    assert "<q>good pacing</q>" in after
    assert today["decision"] == "approved"


def test_a_change_asked_for_is_said_in_her_words() -> None:
    with browser() as client:
        client.post("/student/actions/plan", data=plan_form(client))
        draft_id = client.get("/student/plans/today").json()["draft_id"]
        client.post(
            f"/parent/approvals/{draft_id}",
            json={"approved": False, "reason": "the essay needs two sittings"},
        )
        page = client.get(PAGE).text

    assert "From your parents" in page
    assert "A change is asked for; plan again when you are ready." in page
    assert "<q>the essay needs two sittings</q>" in page
    assert "Plan for Wednesday, August 19, 2026" in page


# --------------------------------------------------------- the disclosure


def test_the_plan_is_set_out_for_reading_and_nothing_is_lost() -> None:
    """The time range in bold, the assignment under it as a link to its details by id, its
    due date, the reason, the work put off under a heading, and the reviewer's notes behind
    one fold, folded on both pages for a plan the review accepted; every part of the saved
    plan once, and no second copy of it as text."""
    with browser() as client:
        client.post("/student/actions/plan", data=plan_form(client))
        body = client.get("/student/plans/today").json()["body"]
        saved = state_of(client).drafts.latest_for(PLAN_DATE)
        page = client.get(PAGE).text
        theirs = client.get("/parent").text

    assert saved is not None
    assert saved.outcome == "accepted"
    text = present_plan(body)
    assert text.blocks[0].span == "4:30 PM to 5:30 PM"
    assert '<p class="plan-when"><strong>4:30 PM to 5:30 PM</strong></p>' in page
    assert (
        '<span class="plan-item">World History &middot; <a class="assignment-link" '
        'href="/student/assignments/assignment-canal-essay?return_to=today" '
        'aria-label="Canal Era comparison essay, World History">Canal Era comparison essay</a>'
    ) in page
    assert (
        '<p class="plan-due">Recorded due August 21, 2026. <strong>Check this date.</strong> '
        '<a class="assignment-link details-link" '
        'href="/student/assignments/assignment-canal-essay?return_to=today#evidence"'
    ) in page
    assert f"return_to=family&amp;plan_id={quote(saved.draft_id, safe='')}" in theirs
    assert '<p class="plan-why">' in page
    assert '<h3 class="plan-heading">Not in this evening\'s plan</h3>' in page
    assert '<h4 class="plan-heading">Not in this evening\'s plan</h4>' in theirs
    for shown in (page, theirs):
        assert "Original saved text" not in shown
        assert 'class="plan-original' not in shown
        assert shown.count("<summary>Blossom's review notes</summary>") == 1
        assert '<details class="steps plan-review">' in shown, "folded for an accepted plan"
        assert '<details class="steps plan-review" open>' not in shown
        assert whole(saved, shown)


def test_times_read_as_she_reads_a_clock() -> None:
    with browser() as client:
        client.post("/student/actions/too-much", follow_redirects=False)
        page = client.get(PAGE).text

    assert re.search(r"said at \d{1,2}:\d{2} [AP]M\.", page)
    assert not re.search(r"\b[01]\d:\d{2}\b(?! [AP]M)", page.split('<main id="main">', 1)[1]), (
        "no 24-hour time anywhere on the page"
    )
    assert "Your next plan will use up to 75 minutes." in page


def test_a_refresh_says_when() -> None:
    with browser() as client:
        hers = client.get(PAGE, params={"refreshed": "1"}).text
        theirs = client.get("/parent", params={"refreshed": "1"}).text
        plain = client.get(PAGE).text

    now = datetime.now(ZoneInfo(FIXTURE_TIMEZONE))
    minute_ago = now - timedelta(minutes=1)
    accepted = {f"Refreshed at {spoken_time(moment)}." for moment in (now, minute_ago)}
    assert any(stamp in hers for stamp in accepted), "the real clock, not the pinned one"
    assert any(stamp in theirs for stamp in accepted)
    assert "Refreshed at" not in plain
    assert 'href="/student/due-this-week?refreshed=1#help">Refresh replies</a>' in plain


def test_what_is_shared_points_inside_its_fold() -> None:
    with browser() as client:
        page = client.get(PAGE).text

    fold = page.split("<summary>How Blossom uses your information</summary>", 1)[1]
    assert 'id="what-is-shared"' in fold.split("</details>", 1)[0]
    assert 'href="#what-is-shared"' in page


def test_todays_saved_plan_is_unfolded_on_every_visit_and_help_is_one_link_away() -> None:
    """Right after it is made, on an ordinary visit, and on a refresh, the saved plan is on
    the page unfolded, a plan read by its rows and one read as text alike, so her next step
    takes no remembered action. It sits before everything about help, and a link beside
    the controls goes to the help form, so a long plan never puts help out of reach. No
    plan, no fold; and opening the page asks no model."""
    with browser() as client:
        before = client.get(PAGE).text
        planned = client.post("/student/actions/plan", data=plan_form(client))
        shown = client.get(planned.headers["location"]).text
        revisit = client.get(PAGE).text
        refreshed = client.get(PAGE, params={"refreshed": "1"}).text
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        saved = state.drafts.latest_for(PLAN_DATE)
        changed_by_hand(state.drafts, "UPDATE drafts SET plan_snapshot=NULL")
        as_text = client.get(PAGE).text

    assert '<details class="plan"' not in before
    assert "No plan for today yet." in before
    assert '<a class="to-help" href="#ask-for-help">Ask for help</a>' in before
    for page in (shown, revisit, refreshed, as_text):
        assert '<div id="todays-plan" tabindex="-1">' in page
        assert '<summary>Today\'s saved plan <span class="summary-meta">made at ' in page
        assert page.index('<div id="todays-plan" tabindex="-1">') < page.index('id="ask-for-help"')
        assert page.index('href="#ask-for-help"') < page.index(
            '<div id="todays-plan" tabindex="-1">'
        )
        assert '<span id="ask-for-help" tabindex="-1">Ask a parent for help</span>' in page
    assert saved is not None
    assert whole(saved, revisit), "the saved plan, read by its rows, all of it"
    assert "This plan uses the earlier text format." in as_text
    assert "set aside for" in as_text
    assert "Looks ahead through Tuesday, August 25." in revisit


def test_the_plan_unfolds_on_todays_week_however_the_week_was_named() -> None:
    """``show_plan`` is a presentation parameter and works beside ``week`` too."""
    with browser() as client:
        client.post("/student/actions/plan", data=plan_form(client))
        named = client.get(PAGE, params={"week": PLAN_DATE.isoformat(), "show_plan": "1"}).text
        other = client.get(PAGE, params={"week": "2026-08-24", "show_plan": "1"}).text

    assert '<div id="todays-plan" tabindex="-1">' in named
    assert '<details class="plan"' not in other, "another week has no today panel"


def test_the_plan_stays_unfolded_when_the_week_asked_for_cannot_be_shown() -> None:
    """The fallback renders today's week; the presentation flag rides along."""
    with browser() as client:
        client.post("/student/actions/plan", data=plan_form(client))
        bad = client.get(PAGE, params={"week": "bad", "show_plan": "1"})
        edge = client.get(PAGE, params={"week": "0001-01-01", "show_plan": "1"})

    assert bad.status_code == 422
    assert '<div id="todays-plan" tabindex="-1">' in bad.text
    assert edge.status_code == 422
    assert '<div id="todays-plan" tabindex="-1">' in edge.text


def test_asking_for_the_plan_unfolded_makes_no_plan() -> None:
    with browser() as client:
        page = client.get(PAGE, params={"show_plan": "1"}).text
        today = client.get("/student/plans/today")

    assert "No plan for today yet." in page
    assert '<details class="plan"' not in page
    assert today.status_code == 404


def test_a_review_the_reviewer_could_not_finish_is_said_outside_the_plan() -> None:
    """Not a check that failed: the reviewer could not tell. A parent's approval
    does not make that go away; the notes are in the plan, open."""
    warning = "Blossom's review could not settle every point. Open the plan to read its notes."
    with browser(critic=undecided) as client:
        client.post("/student/actions/plan", data=plan_form(client))
        draft_id = client.get("/student/plans/today").json()["draft_id"]
        before = client.get(PAGE).text
        client.post(f"/parent/approvals/{draft_id}", json={"approved": True, "reason": "fine"})
        after = client.get(PAGE).text

    assert warning in before
    assert "did not settle" in before, "the notes are in the saved text"
    for page in (before, after):
        assert '<details class="steps plan-review" open>' in page, "open whatever was decided"
    assert warning in after
    assert "From your parents" in after
    assert "check" not in warning


def test_the_page_puts_the_week_ahead_of_the_report_and_keeps_help_at_hand() -> None:
    with browser() as client:
        client.post("/student/actions/plan", data=plan_form(client))
        client.post("/student/help-requests", json={"note": "the essay outline"})
        request_id = client.get("/student/help-requests").json()[0]["request_id"]
        client.post(f"/parent/help-requests/{request_id}/resolve", json={"response": "done"})
        client.post("/student/help-requests")
        page = client.get(PAGE).text

    panel, _, rest = page.partition('<h2 class="list-heading"')
    help_at = rest.index('<section class="panel help-panel" aria-labelledby="help">')
    help_part = rest[help_at : rest.index("<summary>How Blossom uses your information</summary>")]
    assert "Today, Wednesday, August 19" in panel
    assert '<a class="to-help" href="#ask-for-help">Ask for help</a>' in panel
    assert '<a href="#help-updates">Help updates (2)</a>' in panel
    assert 'action="/student/actions/ask-for-help"' not in panel
    assert "the essay outline" not in panel
    assert rest.index("Canal Era comparison essay") < help_at
    assert 'action="/student/actions/ask-for-help"' in help_part
    assert "What would you like help with? (optional)" in help_part
    assert help_part.index("Waiting for a parent.") < help_part.index("the essay outline")
    assert "A parent closed this request on " in help_part
    assert "Planning uses the model provider." in panel
    assert 'href="#what-is-shared"' in panel
    assert 'id="what-is-shared"' in rest
    assert "Planning can take up to about a minute and a half." in rest
    assert "Planning can take up to about a minute and a half." not in panel
    assert "Canal Era comparison essay" in rest


def test_another_week_shows_its_work_and_a_way_back_instead_of_today() -> None:
    with browser() as client:
        client.post("/student/actions/plan", data=plan_form(client))
        other = client.get(PAGE, params={"week": "2026-08-24"}).text

    assert "<h1>Week of August 24</h1>" in other
    assert "Return to this week and today's plan" in other
    assert 'class="panel today"' not in other
    assert 'action="/student/actions/plan"' not in other
    assert 'action="/student/actions/ask-for-help"' not in other
    assert "Plan for Wednesday, August 19, 2026" not in other
    assert "Quadratic modeling problem set" in other


# ------------------------------------------------- what the plan button offers


def test_the_plan_button_follows_the_evening_as_it_stands() -> None:
    """The label and the words beside it come from the plan and the signal; nothing here plans."""
    with browser(plan=light_fixture_plan) as client:
        nothing_yet = client.get(PAGE).text
        client.post("/student/actions/too-much")
        signaled_no_plan = client.get(PAGE).text
        client.post("/student/actions/plan", data=plan_form(client))
        small_plan_signaled = client.get(PAGE).text
        signal_id = client.get("/student/workload-signals").json()[0]["signal_id"]
        client.delete(f"/student/workload-signals/{signal_id}")
        small_plan_no_signal = client.get(PAGE).text

    assert ">Plan today<" in nothing_yet
    assert "Make a smaller plan" not in nothing_yet

    assert ">Make a smaller plan<" in signaled_no_plan
    assert "Your next plan will use up to 75 minutes." in signaled_no_plan

    assert ">Plan again<" in small_plan_signaled
    assert "Your saved plan fits within today's 75-minute limit." in small_plan_signaled
    assert "Your next plan will use up to" not in small_plan_signaled
    assert "Make a smaller plan" not in small_plan_signaled

    assert ">Plan again<" in small_plan_no_signal
    assert (
        "It stays until a new one is made; plan again for the full evening." in small_plan_no_signal
    )


def test_a_full_plan_under_a_signal_offers_a_smaller_one_and_keeps_the_plan() -> None:
    with browser() as client:
        client.post("/student/actions/plan", data=plan_form(client))
        body = client.get("/student/plans/today").json()["body"]
        saved = state_of(client).drafts.latest_for(PLAN_DATE)
        client.post("/student/actions/too-much")
        page = client.get(PAGE).text
        after = client.get("/student/plans/today").json()["body"]

    assert saved is not None
    assert ">Make a smaller plan<" in page
    assert "Your current plan has not changed yet. Make a smaller plan when you are ready." in page
    assert whole(saved, page)
    assert after == body, "pressing the signal does not plan"


def test_the_planning_forms_carry_the_pending_words_and_the_others_do_not() -> None:
    """The enhancement is scoped by a data attribute; decision and help buttons keep their names."""
    with browser() as client:
        client.post("/student/actions/plan", data=plan_form(client))
        client.post("/student/help-requests")
        hers = client.get(PAGE).text
        theirs = client.get("/parent").text
        script = client.get("/static/blossom.js")

    assert re.search(r'<script src="/static/blossom\.js\?v=[0-9a-f]{12}" defer></script>', hers)
    assert 'data-pending="Making your plan"' in hers
    assert 'data-pending="Making the plan"' in theirs
    assert hers.count('data-pending="') == 1
    assert (
        'data-pending-later="Still working. The planner reads your week and writes the plan, '
        'which can take up to about a minute and a half"'
    ) in hers
    assert (
        'data-pending-later="Still working. The planner reads her week and writes the plan, '
        'which can take up to about a minute and a half"'
    ) in theirs
    assert "minute or two" not in hers + theirs, "no wait is said longer than a run may take"
    assert theirs.count('data-pending="') == 1
    assert 'name="decision" value="approve"' in theirs
    assert 'name="step" value="accept"' in theirs
    assert script.status_code == 200
    assert "form[data-pending]" in script.text
    assert "pageshow" in script.text


def test_refresh_is_a_link_on_both_pages_and_a_visit_marks_nothing() -> None:
    with browser() as client:
        client.post("/student/help-requests")
        hers = client.get(PAGE).text
        client.get("/parent")
        theirs = client.get("/parent").text
        state = client.get("/student/help-requests").json()[0]["state"]
        hers_after = client.get(PAGE).text

    assert '<a href="/student/due-this-week?refreshed=1#help">Refresh replies</a>' in hers
    assert "Refresh to see updates. Save or send your note first." in hers
    assert '<a href="/parent?refreshed=1#help-she-asked-for">Refresh requests</a>' in theirs
    assert state == "requested"
    assert "Waiting for a parent." in hers_after


# --------------------------------------------------- the evening changing


def test_a_press_after_the_plan_tells_her_to_plan_again_in_her_words() -> None:
    with browser() as client:
        client.post("/student/actions/plan", data=plan_form(client))
        client.post("/student/actions/too-much")
        page = client.get(PAGE).text
        today = client.get("/student/plans/today").json()

    assert today["stale"] == (
        "You have said today is too much, and this plan was made for the full evening. "
        "Your current plan has not changed yet. Make a smaller plan when you are ready."
    )
    assert "<strong>Make a smaller plan.</strong> You have said today is too much" in page
    assert ">Make a smaller plan<" in page
    assert "Your next plan will use up to 75 minutes." in page


def test_her_page_measures_the_plan_against_the_evening_whatever_a_parent_said() -> None:
    """A parent's review is not a reason to keep a plan that has stopped fitting the evening."""
    with browser() as client:
        client.post("/student/actions/plan", data=plan_form(client))
        draft_id = client.get("/student/plans/today").json()["draft_id"]
        client.post(f"/parent/approvals/{draft_id}", json={"approved": True})
        client.post("/student/actions/too-much")
        today = client.get("/student/plans/today").json()
        reviewed = client.get(f"/parent/approvals/{draft_id}").json()

    assert today["decision"] == "approved"
    assert today["stale"] == (
        "You have said today is too much, and this plan was made for the full evening. "
        "Your current plan has not changed yet. Make a smaller plan when you are ready."
    )
    assert reviewed["stale"] is None


def test_taking_the_signal_back_after_a_reduced_plan_says_so_in_her_words() -> None:
    with browser(plan=light_fixture_plan) as client:
        signal_id = client.post("/student/workload-signals").json()["signal"]["signal_id"]
        client.post("/student/actions/plan", data=plan_form(client))
        reduced = client.get("/student/plans/today").json()
        client.delete(f"/student/workload-signals/{signal_id}")
        today = client.get("/student/plans/today").json()
        page = client.get(PAGE).text

    assert reduced["too_much"] is True
    assert reduced["stale"] is None
    assert today["stale"] == (
        "This plan was kept to the smaller evening for a signal that is not there now. "
        "It stays until a new one is made; plan again for the full evening."
    )
    assert "because you said today was too much" in page


def test_with_nothing_left_to_plan_her_routes_refuse_before_any_run() -> None:
    """Every assignment in today's window reported done: the JSON route and the button
    answer 409 with the one sentence, no run is written, and no model is asked."""
    with browser() as client:
        state: ApplicationState = getattr(
            client.app.state,  # type: ignore[attr-defined]
            STATE_ATTRIBUTE,
        )
        window = read_week(state.project_state, state.project_state, PLAN_DATE)
        for item in window.assignments:
            state.project_state.report_status(
                item.assignment_id, "done", None, expected_head=None, now=CREATED, today=PLAN_DATE
            )
        over_json = client.post("/student/plans")
        from_the_page = client.post("/student/actions/plan", data=fresh_plan_fields(client))
        ended = state.drafts.runs_without_a_draft()

    assert over_json.status_code == 409
    assert over_json.json()["detail"] == NOTHING_TO_SCHEDULE
    assert from_the_page.status_code == 409
    assert NOTHING_TO_SCHEDULE in from_the_page.text
    assert ended == []


# ------------------------------------------------------- a run that ends without a plan

MAP_QUIZ = Assignment(
    assignment_id="assignment-map-quiz",
    course="Geography",
    title="Map quiz",
    due_date=None,
    dependencies=[],
    reported_submission_status="not_started",
)
"""Work with no date on record, so always in the window, whose only date, the portal's, is
the day before the evening: no date is still to come, so no plan for the evening can
schedule it or put it off in time."""


TRY_AGAIN = '<button type="submit" class="primary" aria-describedby="plan-scope">Try again</button>'
"""The plan button after a press that made no plan and that another press may put right,
described by the line that says what a plan is made from."""


def answer(
    parsed: DailyPlan | None = None,
    *,
    stop_reason: str = "end_turn",
    parsing_error: str | None = None,
) -> ModelAnswer[DailyPlan]:
    """An answer as the service can send one: whole, cut off, or unreadable."""
    return ModelAnswer(parsed=parsed, stop_reason=stop_reason, parsing_error=parsing_error)


def ended_page(client: TestClient) -> str:
    """Her plan press, as a page: the top line and the plan button."""
    response = client.post("/student/actions/plan", data=plan_form(client))
    assert response.status_code == 409
    return response.text


def the_line(page: str) -> str:
    """Her week's top line as written, its opening read as part of it."""
    start = page.index('<p class="problem week-problem"')
    return unwrapped(page[start : page.index("</p>", start) + 4])


def test_each_way_a_run_ends_without_a_plan_is_said_with_a_way_forward() -> None:
    """Wrong-date answers, a plan cut off, an answer that couldn't be read, time running
    out, and a service still busy after its retries: each is said in its own words, says
    her updates are saved, gives the focus to its first sentence, links to her homework, and
    the plan button offers to try again. None says the evening is too full, and none asks
    for a parent."""
    clock = FakeTime()
    tomorrow = fixture_week_plan().model_copy(update={"plan_date": date(2026, 8, 20)})
    cases: dict[str, tuple[Callable[[], Ask[DailyPlan]], str]] = {
        "wrong date": (
            lambda: Scripted(*[ok(tomorrow)] * (MAX_REVISIONS + 1)),
            "Blossom couldn&#39;t finish a reliable plan this time.",
        ),
        "truncated": (
            lambda: Scripted(answer(parsed=fixture_week_plan(), stop_reason="max_tokens")),
            "Blossom couldn&#39;t finish a reliable plan this time.",
        ),
        "malformed": (
            lambda: Scripted(answer(parsing_error="not valid JSON")),
            "Blossom couldn&#39;t finish a reliable plan this time.",
        ),
        "timeout": (
            lambda: Spending(clock, (RUN_DEADLINE_SECONDS, ok(forgetful_fixture_plan()))),
            "Planning took too long, so Blossom stopped.",
        ),
        "service": (
            lambda: Spending(clock, *[(1, ServiceBusy("overloaded"))] * (MODEL_RETRIES + 1)),
            "Blossom couldn&#39;t get a plan from the planning service this time.",
        ),
        "refused": (
            lambda: Spending(clock, (1, ServiceFailed("401 the key was refused"))),
            "Blossom couldn&#39;t get a plan from the planning service this time.",
        ),
    }
    for name, (planner, said) in cases.items():
        with browser() as client:
            client.app.dependency_overrides[plan_graphs] = model_graphs(  # type: ignore[attr-defined]
                planner, Scripted, budget=clock.budget
            )
            page = ended_page(client)

        line = the_line(page)
        assert f"{said} Your homework updates are saved." in line, name
        assert line.startswith(f"{HER_TOP_LINE}{said} "), name
        assert opening_focused(page, HER_TOP_LINE) == words(said), name
        assert '<a href="#title-assignment-science-fair-proposal">See homework.</a>' in line, name
        assert TRY_AGAIN in page, name
        assert "fits this evening" not in page, name
        assert "parent" not in line, name
        assert "No plan for today yet." in page, name


def test_work_dated_before_today_is_named_with_a_link_to_its_dates_and_no_model_is_asked() -> None:
    """The work is named once, in a link to its dates with its course and due date, no model
    is asked, and the plan button keeps its own words, since planning again can't fix a
    date."""
    planners: list[Scripted[DailyPlan]] = []
    with browser() as client:
        state = state_of(client)
        state.project_state.upsert_assignments([MAP_QUIZ])
        state.project_state.record_claims(
            MAP_QUIZ.assignment_id, [record(SourceChannel.LMS, "2026-08-18")]
        )
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            list, list, planners=planners
        )
        page = ended_page(client)
        ended = state.drafts.runs_without_a_draft()

    line = the_line(page)
    assert (
        "Blossom can&#39;t make today&#39;s plan. Some work has a due date that already passed, "
        "so no plan can finish it on time. Your homework updates are saved."
    ) in line
    assert (
        '<a href="/student/assignments/assignment-map-quiz?return_to=week#evidence">'
        "Check the dates for Map quiz (Geography, due August 18).</a>"
    ) in line
    assert words(line).count("Map quiz") == 1
    assert '<a href="#title-assignment-science-fair-proposal">See homework.</a>' in line
    assert TRY_AGAIN not in page
    assert (
        '<button type="submit" class="primary" aria-describedby="plan-scope">Plan today</button>'
        in page
    )
    assert planners[-1].calls == 0
    assert [run.outcome for run in ended] == ["date_problem"]


def test_a_failed_run_keeps_her_updates_and_the_plan_already_there() -> None:
    """She has a plan, saves Done on the essay, and plans again; the run times out. Her
    update stands, today's plan is the one she had, dated and marked as made before her
    update, and nothing waits in its place."""
    clock = FakeTime()
    with browser() as client:
        assert client.post("/student/actions/plan", data=plan_form(client)).status_code == 303
        state = state_of(client)
        first = state.drafts.latest_for(PLAN_DATE)
        assert first is not None
        report(client, ESSAY_ID, "done")
        client.app.dependency_overrides[plan_graphs] = model_graphs(  # type: ignore[attr-defined]
            lambda: Spending(clock, (RUN_DEADLINE_SECONDS, ok(fixture_week_plan()))),
            Scripted,
            budget=clock.budget,
        )
        page = ended_page(client)
        latest = state.drafts.latest_for(PLAN_DATE)
        waiting = [item.draft_id for item in state.drafts.waiting()]
        essay = status_of(state.project_state, ESSAY_ID)

    assert (
        "Planning took too long, so Blossom stopped. Your homework updates are saved."
        in unwrapped(page)
    )
    assert latest is not None
    assert latest.draft_id == first.draft_id
    assert waiting == [first.draft_id]
    assert essay.asserted is not None
    assert essay.asserted.status == "done"
    assert "Plan for Wednesday, August 19, 2026" in page
    assert "A plan is ready." in page
    assert (
        "Canal Era comparison essay"
        in page.split('class="confidence disagree" role="status"', 1)[1]
    )


def test_one_press_is_one_run() -> None:
    """A press makes one run and one record, whether it ends with a plan or without, and
    the button that makes it is the one form that holds itself busy after a press."""
    planners: list[Scripted[DailyPlan]] = []
    with browser() as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [forgetful_fixture_plan()] * (MAX_REVISIONS + 1), list, planners=planners
        )
        client.post("/student/actions/plan", data=plan_form(client))
        page = client.get(PAGE).text
        runs = state_of(client).drafts.runs_without_a_draft()

    assert len(planners) == 1
    assert len(runs) == 1
    assert page.count('action="/student/actions/plan"') == 1
    assert page.count("data-pending=") == 1


def test_a_press_while_a_plan_for_today_is_being_made_starts_nothing() -> None:
    """The run in flight is the one plan for today being made: a second press starts no run,
    asks no model, and says in the failure line which evening is still being finished and
    how long to wait, with the way to her homework."""
    planners: list[Scripted[DailyPlan]] = []
    clock = FakeTime()
    inflight = f"plan:{PLAN_DATE.isoformat()}:inflight"
    with browser(clock=clock) as client:
        state = state_of(client)
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [fixture_week_plan()], lambda: [accepting()], planners=planners
        )
        blocked = state.drafts.admit_run(
            inflight, plan_date=PLAN_DATE, deadline_mono=state.monotonic() + RUN_DEADLINE_SECONDS
        )
        page = ended_page(client)
        over_json = client.post("/student/plans")
        runs = state.drafts.runs_without_a_draft()
        running = state.drafts.running_threads()
        latest = state.drafts.latest_for(PLAN_DATE)
        blocking = state.drafts.latest_run()

    finishing = "A plan for Wednesday, August 19 is being made. Try again in about 91 seconds."
    line = the_line(page)
    assert blocked is None
    assert f"{finishing} Your homework updates are saved." in line
    assert '<a href="#title-assignment-science-fair-proposal">See homework.</a>' in line
    assert all(planner.calls == 0 for planner in planners)
    assert runs == []
    assert running == {inflight}
    assert latest is None
    assert over_json.status_code == 409
    assert over_json.json()["detail"] == {
        "message": f"{finishing} Your homework updates are saved.",
        "run_id": inflight,
        "plan_date": PLAN_DATE.isoformat(),
        "seconds_left": 91,
    }
    assert blocking is not None
    assert blocking.run_id == inflight
    assert already_planning(blocking, parent=True) == (
        f"{finishing} Her homework updates are saved."
    )
    assert run_link(inflight, ON_THAT_REQUEST) in line


# ------------------------------------------------------- where a plan request stands


ON_THAT_REQUEST = "Check on that request."
ON_IT = "Check on it."
AGAIN = "Check again."


def run_link(run_id: str, label: str) -> str:
    """The link her week gives to check a run again, as the page writes it."""
    return f'<a href="{PAGE}?run={run_id.replace(":", "%3A")}">{label}</a>'


def run_line(page: str) -> str | None:
    """The line her week gives a planning run, or ``None`` when it gives none."""
    marker = page.find('id="plan-run"')
    if marker < 0:
        return None
    start = page.rindex("<p", 0, marker)
    return page[start : page.index("</p>", start) + 4]


UNSURE = f"plan:{PLAN_DATE.isoformat()}:unsure"
INFLIGHT = f"plan:{PLAN_DATE.isoformat()}:inflight"
STILL_RUNNING = RunState(
    run_id=INFLIGHT,
    plan_date=PLAN_DATE,
    status="running",
    reason="running",
    seconds_left=40.0,
    plan_unchanged=True,
)
PRESS_ANSWERS: dict[str, tuple[Callable[[], Exception], int, str, str | None]] = {
    "already planning": (
        lambda: AlreadyPlanning(STILL_RUNNING),
        409,
        "A plan for Wednesday, August 19 is being made. Try again in about 41 seconds. {Your} "
        "homework updates are saved.",
        run_link(INFLIGHT, ON_THAT_REQUEST),
    ),
    "not saved": (
        NotSaved,
        503,
        "Blossom made a plan but couldn&#39;t save it. Try again in a moment. {Your} homework "
        "updates are saved.",
        None,
    ),
    "not saved, her plan kept": (
        lambda: NotSaved(kept=True),
        503,
        "Blossom made a plan but couldn&#39;t save it, so {your} current plan hasn&#39;t "
        "changed. Try again in a moment. {Your} homework updates are saved.",
        None,
    ),
    "failed on the way": (
        lambda: RuntimeError("a node failed on the way"),
        409,
        "Blossom couldn&#39;t finish a reliable plan this time. {Your} homework updates are saved.",
        None,
    ),
    "unconfirmed": (
        lambda: Unconfirmed(UNSURE, PLAN_DATE),
        202,
        "Blossom couldn&#39;t confirm that the new plan was saved. {Your} homework updates are "
        "saved.",
        run_link(UNSURE, AGAIN),
    ),
    "could not start": (
        CouldNotStart,
        503,
        "Blossom couldn&#39;t start a plan this time. Try again in a moment. {Your} homework "
        "updates are saved.",
        None,
    ),
}


@pytest.mark.parametrize("answer", PRESS_ANSWERS)
@pytest.mark.parametrize("reader", ["her", "a parent"])
def test_each_answer_to_a_press_says_her_updates_are_saved_in_the_readers_words(
    answer: str, reader: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A press refused while a run is being finished, a plan made but not saved, one whose
    saving couldn't be confirmed, a run that couldn't start, and one that failed on the way
    are each said on her week with the status the JSON route answers, in the reader's words.
    The first links to where that run stands, and an unconfirmed one offers to check again.
    Her plan is said unchanged only when that is known."""
    error, code, said, link = PRESS_ANSWERS[answer]

    async def refused(*args: object, **kwargs: object) -> object:
        raise error()

    monkeypatch.setattr(student_routes, "make_plan", refused)
    parent = reader == "a parent"
    client = client_for(signed_in_household(tmp_path)) if parent else browser()
    client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
        lambda: [fixture_week_plan()], lambda: [accepting()]
    )
    with client:
        if parent:
            signed_in(client, THEIRS)
        response = client.post(
            "/student/actions/plan", data=fresh_plan_fields(client) if parent else plan_form(client)
        )

    line = the_line(response.text)
    assert response.status_code == code
    assert said.format(Your="Her" if parent else "Your", your="her" if parent else "your") in line
    if link is None:
        assert "?run=" not in line
    else:
        assert link in line
    assert ("Your " in line or "your " in line) is not parent
    assert ("hasn&#39;t changed" in line) is (answer == "not saved, her plan kept")
    assert "went wrong" not in line


JSON_ANSWERS: dict[str, tuple[Callable[[], Exception], int, str]] = {
    "not saved": (
        NotSaved,
        503,
        "Blossom made a plan but couldn't save it. Try again in a moment. {Your} homework "
        "updates are saved.",
    ),
    "not saved, her plan kept": (
        lambda: NotSaved(kept=True),
        503,
        "Blossom made a plan but couldn't save it, so {your} current plan hasn't changed. "
        "Try again in a moment. {Your} homework updates are saved.",
    ),
    "could not start": (
        CouldNotStart,
        503,
        "Blossom couldn't start a plan this time. Try again in a moment. {Your} homework "
        "updates are saved.",
    ),
    "failed on the way": (
        lambda: RuntimeError("a node failed on the way"),
        409,
        "Blossom couldn't finish a reliable plan this time. {Your} homework updates are saved.",
    ),
}


@pytest.mark.parametrize("answer", JSON_ANSWERS)
@pytest.mark.parametrize("reader", ["her", "a parent"])
def test_each_json_answer_to_a_press_says_her_updates_are_saved_in_the_readers_words(
    answer: str, reader: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Her JSON route answers a plan not saved and a run that couldn't start with a 503, and
    a run that failed on the way as one Blossom couldn't finish, 409: each says her updates
    are saved in the words of whoever is signed in, never a bare error."""
    error, code, said = JSON_ANSWERS[answer]

    async def refused(*args: object, **kwargs: object) -> object:
        raise error()

    monkeypatch.setattr(student_routes, "make_plan", refused)
    parent = reader == "a parent"
    client = client_for(signed_in_household(tmp_path)) if parent else browser()
    client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
        lambda: [fixture_week_plan()], lambda: [accepting()]
    )
    with client:
        if parent:
            signed_in(client, THEIRS)
        response = client.post("/student/plans")

    detail = response.json()["detail"]
    assert response.status_code == code
    assert detail.startswith(
        said.format(Your="Her" if parent else "Your", your="her" if parent else "your")
    )
    assert ("Your " in detail or "your " in detail) is not parent


@pytest.mark.parametrize("route", ["/student/plans", "/student/actions/plan"])
@pytest.mark.parametrize("reader", ["her", "a parent"])
def test_a_week_that_cannot_be_read_before_a_press_is_a_plan_that_could_not_start(
    route: str, reader: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A press whose read of the week fails before any run is admitted couldn't start: her
    JSON route and her page answer 503 with the try-again sentence and her updates saved,
    in the reader's words, and no run is written."""

    def unreadable(*args: object, **kwargs: object) -> object:
        msg = "disk I/O error"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(run_routes, "read_week", unreadable)
    parent = reader == "a parent"
    client = client_for(signed_in_household(tmp_path)) if parent else browser()
    client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
        lambda: [fixture_week_plan()], lambda: [accepting()]
    )
    with client:
        if parent:
            signed_in(client, THEIRS)
        form = fresh_plan_fields(client) if parent else plan_form(client)
        response = client.post(route, data=form if "actions" in route else None)
        newest = state_of(client).drafts.latest_run()

    said = (
        "Blossom couldn't start a plan this time. Try again in a moment. "
        f"{'Her' if parent else 'Your'} homework updates are saved."
    )
    assert response.status_code == 503
    if route == "/student/plans":
        assert response.json()["detail"] == said
    else:
        line = the_line(response.text)
        assert str(escape(said)) in line
        assert ("Your " in line or "your " in line) is not parent
    assert newest is None


@pytest.mark.parametrize("route", ["/student/plans", "/student/actions/plan"])
@pytest.mark.parametrize("reader", ["her", "a parent"])
def test_a_graph_that_cannot_be_built_before_a_press_is_a_plan_that_could_not_start(
    route: str, reader: str, tmp_path: pathlib.Path
) -> None:
    """A press whose plan graph fails to build, before any run is admitted, couldn't start:
    her JSON route and her page answer 503 with the try-again sentence and her updates
    saved, in the reader's words, and no run is written."""

    def unbuildable() -> CompiledPlanGraph:
        msg = "the plan graph could not be built"
        raise RuntimeError(msg)

    def graphs() -> PlanGraphs:
        return PlanGraphs(build=unbuildable, may_start=True)

    parent = reader == "a parent"
    client = client_for(signed_in_household(tmp_path)) if parent else browser()
    client.app.dependency_overrides[plan_graphs] = graphs  # type: ignore[attr-defined]
    with client:
        if parent:
            signed_in(client, THEIRS)
        form = fresh_plan_fields(client) if parent else plan_form(client)
        response = client.post(route, data=form if "actions" in route else None)
        newest = state_of(client).drafts.latest_run()

    said = (
        "Blossom couldn't start a plan this time. Try again in a moment. "
        f"{'Her' if parent else 'Your'} homework updates are saved."
    )
    assert response.status_code == 503
    if route == "/student/plans":
        assert response.json()["detail"] == said
    else:
        line = the_line(response.text)
        assert str(escape(said)) in line
        assert ("Your " in line or "your " in line) is not parent
    assert newest is None


def test_an_unconfirmed_answer_offers_only_a_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answer whose plan may have been published offers to check again, never to try
    again, and no plan button that could start another paid run. A refusal while a run is
    being finished still offers Try again."""
    answers: dict[str, Callable[[], Exception]] = {
        "unconfirmed": lambda: Unconfirmed(UNSURE, PLAN_DATE),
        "already planning": lambda: AlreadyPlanning(STILL_RUNNING),
    }
    pages: dict[str, str] = {}
    for name, error in answers.items():

        async def refused(
            *args: object, raising: Callable[[], Exception] = error, **kwargs: object
        ) -> object:
            raise raising()

        monkeypatch.setattr(student_routes, "make_plan", refused)
        with browser() as client:
            pages[name] = client.post("/student/actions/plan", data=plan_form(client)).text

    assert 'action="/student/actions/plan"' not in pages["unconfirmed"]
    assert TRY_AGAIN not in pages["unconfirmed"]
    assert run_link(UNSURE, AGAIN) in the_line(pages["unconfirmed"])
    assert TRY_AGAIN in pages["already planning"]


def test_the_status_of_a_run_whose_record_cannot_be_read_is_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking where a run stands when reading its record fails, not only when the read
    doesn't finish in time, answers 503 unconfirmed on both routes, never a bare error."""

    def failing(
        self: DraftsStore, run_id: str, wait: float = 5.0, *, reconcile: bool = True
    ) -> None:
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(DraftsStore, "run_status", failing)
    with browser() as client:
        hers = client.get(f"/student/plans/runs/{UNSURE}")
        family = client.get(f"/parent/plans/runs/{UNSURE}")

    for answer in (hers, family):
        assert answer.status_code == 503
        assert answer.json()["detail"] == "Blossom couldn't confirm that the new plan was saved."


def test_the_api_schema_names_the_body_of_each_answer_to_a_press_and_to_a_runs_status() -> None:
    """Both JSON plan routes and both run status routes declare the body of every answer
    they give: her 201 is her plan or its published run, and a 409 says why in a sentence or
    names the run still running."""
    with browser() as client:
        api = client.get("/openapi.json").json()

    def named(schema: dict[str, Any]) -> list[str]:
        return [part["$ref"].rsplit("/", 1)[-1] for part in schema.get("anyOf", [schema])]

    def bodies(path: str, method: str) -> dict[str, list[str]]:
        answers = api["paths"][path][method]["responses"]
        return {
            code: named(answer["content"]["application/json"]["schema"])
            for code, answer in answers.items()
        }

    assert bodies("/student/plans", "post") == {
        "201": ["StudentPlanView", "PublishedRunView"],
        "202": ["UnconfirmedRunView"],
        "409": ["PlanConflictView"],
        "503": ["ProblemView"],
    }
    assert bodies("/parent/plans", "post") == {
        "201": ["PlanRunView"],
        "202": ["UnconfirmedRunView"],
        "409": ["PlanConflictView"],
        "422": ["HTTPValidationError"],
        "503": ["ProblemView"],
    }
    for path in ("/student/plans/runs/{run_id}", "/parent/plans/runs/{run_id}"):
        assert bodies(path, "get") == {
            "200": ["RunStatusView"],
            "404": ["ProblemView"],
            "422": ["HTTPValidationError"],
            "503": ["ProblemView"],
        }
    schemas = api["components"]["schemas"]
    conflict = schemas["PlanConflictView"]["properties"]["detail"]["anyOf"]
    assert conflict[0] == {"type": "string"}
    assert named(conflict[1]) == ["AlreadyPlanningView"]
    assert set(schemas["AlreadyPlanningView"]["required"]) == set(
        AlreadyPlanning(STILL_RUNNING).detail
    )
    assert schemas["ProblemView"]["required"] == ["detail"]


def test_her_plan_is_answered_by_its_run_when_reading_it_back_does_not_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Her JSON answer reads the published plan on a worker thread. When that reading
    doesn't finish within its wait, the plan stands published and the answer is still 201,
    with the run, its evening, and that it was published."""
    reading = student_routes.plan_view
    release = threading.Event()
    off_the_loop: list[bool] = []

    def slow(state: ApplicationState, record: DraftRecord, reader: Reader = "student") -> object:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            off_the_loop.append(True)
        else:
            off_the_loop.append(False)
        assert release.wait(5)
        return reading(state, record, reader)

    monkeypatch.setattr(student_routes, "plan_view", slow)
    monkeypatch.setattr(student_routes, "STORE_WAIT_SECONDS", 0.2)
    with browser() as client:
        try:
            answer = client.post("/student/plans")
        finally:
            release.set()
        state = state_of(client)
        run = state.drafts.latest_run()
        latest = state.drafts.latest_for(PLAN_DATE)

    assert run is not None
    assert answer.status_code == 201
    assert answer.json() == {
        "run_id": run.run_id,
        "plan_date": PLAN_DATE.isoformat(),
        "status": "published",
    }
    assert run.status == "published"
    assert latest is not None
    assert latest.thread_id == run.run_id
    assert off_the_loop == [True]


def test_a_run_asked_for_by_its_id_is_said_on_her_week(monkeypatch: pytest.MonkeyPatch) -> None:
    """``?run=`` says where that run stands: still being finished, with Check; ended, with
    why, and never that her plan hasn't changed while the evening has none; nothing more for
    a run whose plan was published or one never made; and that it couldn't be confirmed,
    with Check again, when the record can't be read in time."""
    first = f"plan:{PLAN_DATE.isoformat()}:first"
    newer = f"plan:{PLAN_DATE.isoformat()}:newer"

    def busy(self: DraftsStore, run_id: str, wait: float = 5.0, *, reconcile: bool = True) -> None:
        raise StoreBusy

    with browser() as client:
        state = state_of(client)
        admitted = state.drafts.admit_run(
            first, plan_date=PLAN_DATE, deadline_mono=state.monotonic() + RUN_DEADLINE_SECONDS
        )
        running = client.get(f"{PAGE}?run={first}").text
        state.drafts.end_run(first, reason="timed_out")
        ended = client.get(f"{PAGE}?run={first}").text
        settled_run(
            state.drafts,
            draft(f"draft:{newer}", "Plan for Wednesday"),
            thread_id=newer,
            plan_date=PLAN_DATE,
        )
        published = client.get(f"{PAGE}?run={newer}").text
        ended_before_it = client.get(f"{PAGE}?run={first}").text
        never = client.get(f"{PAGE}?run=plan:2026-08-19:never").text
        with monkeypatch.context() as patched:
            patched.setattr(DraftsStore, "run_status", busy)
            unsure = client.get(f"{PAGE}?run={first}")

    timed_out = "Planning took too long, so Blossom stopped."
    assert admitted is None
    line = run_line(running)
    assert line is not None
    assert 'class="note" role="status"' in line
    assert "The plan request for Wednesday, August 19 is still being finished." in line
    assert run_link(first, ON_IT) in line
    line = run_line(ended)
    assert line is not None
    assert f"{timed_out} Your homework updates are saved." in line
    assert "hasn&#39;t changed" not in line
    assert "?run=" not in line
    line = run_line(ended_before_it)
    assert line is not None
    assert f"{timed_out} Your homework updates are saved." in line
    assert run_line(published) is None
    assert "A plan is ready." in published
    assert run_line(never) is None
    assert unsure.status_code == 200
    line = run_line(unsure.text)
    assert line is not None
    assert (
        "Blossom couldn&#39;t confirm that the new plan was saved. Your homework updates are saved."
    ) in line
    assert run_link(first, AGAIN) in line


def test_a_parent_reading_her_week_is_told_where_a_run_stands_in_her_words(
    tmp_path: pathlib.Path,
) -> None:
    """A parent signed in reads her week: a run being made is said the same way, her JSON
    route's refusal while it runs says her updates are saved, and a run that ended is said
    with her plan and her updates, and where its record is."""
    asked = f"plan:{PLAN_DATE.isoformat()}:asked"
    client = client_for(signed_in_household(tmp_path))
    client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
        lambda: [fixture_week_plan()], lambda: [accepting()]
    )
    with client:
        signed_in(client, THEIRS)
        state = state_of(client)
        admitted = state.drafts.admit_run(
            asked, plan_date=PLAN_DATE, deadline_mono=state.monotonic() + RUN_DEADLINE_SECONDS
        )
        running = client.get(f"{PAGE}?run={asked}").text
        refused = client.post("/student/plans")
        state.drafts.end_run(asked, reason="timed_out")
        ended = client.get(f"{PAGE}?run={asked}").text
        found = client.get(PAGE).text

    assert admitted is None
    line = run_line(running)
    assert line is not None
    assert "The plan request for Wednesday, August 19 is still being finished." in line
    assert refused.status_code == 409
    assert refused.json()["detail"]["message"].endswith("Her homework updates are saved.")
    line = run_line(ended)
    assert line is not None
    assert (
        "Planning took too long, so Blossom stopped. Her homework updates are saved. "
        "Family review shows what happened."
    ) in line
    assert "hasn&#39;t changed" not in line
    assert "Your" not in line
    assert "your" not in line
    assert run_line(found) == line


def test_her_week_says_on_load_what_became_of_the_last_plan_request() -> None:
    """With no run named, her week reads the household's newest run: one within its deadline
    is said as still being finished, with Check, and one past it as Blossom finishing the
    last plan request, never as timed out before the record says so; one for today that
    ended with no newer plan is said with why, without a plan to call unchanged. A run for
    another evening, and a run whose plan was published, add nothing."""
    clock = FakeTime()
    asked = f"plan:{PLAN_DATE.isoformat()}:asked"
    tomorrow = PLAN_DATE + timedelta(days=1)
    family = f"plan:{tomorrow.isoformat()}:family"
    newer = f"plan:{PLAN_DATE.isoformat()}:newer"
    with browser(clock=clock) as client:
        state = state_of(client)
        quiet = client.get(PAGE).text
        admitted = state.drafts.admit_run(
            asked, plan_date=PLAN_DATE, deadline_mono=clock() + RUN_DEADLINE_SECONDS
        )
        being_made = client.get(PAGE).text
        clock.advance(RUN_DEADLINE_SECONDS + 1)
        finishing = client.get(PAGE).text
        reconciled = state.drafts.run_status(asked)
        after = client.get(PAGE).text
        ended_run(
            state.drafts,
            thread_id=family,
            plan_date=tomorrow,
            outcome="service_failed",
            now=clock,
        )
        another_evening = client.get(PAGE).text
        settled_run(
            state.drafts,
            draft(f"draft:{newer}", "Plan for Wednesday"),
            thread_id=newer,
            plan_date=PLAN_DATE,
            now=clock,
        )
        published = client.get(PAGE).text

    assert run_line(quiet) is None
    assert admitted is None
    line = run_line(being_made)
    assert line is not None
    assert "The plan request for Wednesday, August 19 is still being finished." in line
    assert run_link(asked, ON_IT) in line
    line = run_line(finishing)
    assert line is not None
    assert "Blossom is finishing the last plan request." in line
    assert run_link(asked, ON_IT) in line
    assert "took too long" not in finishing
    assert reconciled is not None
    assert (reconciled.status, reconciled.reason) == ("ended", "timed_out")
    line = run_line(after)
    assert line is not None
    assert "Planning took too long, so Blossom stopped. Your homework updates are saved." in line
    assert "hasn&#39;t changed" not in line
    assert "?run=" not in line
    assert run_line(another_evening) is None
    assert run_line(published) is None
    assert "A plan is ready." in published


def test_a_date_problem_found_on_load_is_said_as_one_and_names_its_evening() -> None:
    """A run for today that ended on a date problem is said on load as one: work whose due
    date passed, which no plan can finish on time, never as a plan that wasn't reliable;
    another way of ending keeps its own words. Asked for by its id, another evening's date
    problem names that evening, never today's plan."""
    clock = FakeTime()
    tomorrow = PLAN_DATE + timedelta(days=1)
    later = f"plan:{tomorrow.isoformat()}:later"
    with browser(clock=clock) as client:
        state = state_of(client)
        ended_run(
            state.drafts,
            thread_id=f"plan:{PLAN_DATE.isoformat()}:dated",
            plan_date=PLAN_DATE,
            outcome="date_problem",
            now=clock,
        )
        date_problem = client.get(PAGE).text
        ended_run(
            state.drafts,
            thread_id=f"plan:{PLAN_DATE.isoformat()}:checked",
            plan_date=PLAN_DATE,
            outcome="checks_failed",
            now=clock,
        )
        unreliable = client.get(PAGE).text
        ended_run(
            state.drafts, thread_id=later, plan_date=tomorrow, outcome="date_problem", now=clock
        )
        another_evening = client.get(f"{PAGE}?run={later}").text

    line = run_line(date_problem)
    assert line is not None
    assert (
        "Blossom can&#39;t make today&#39;s plan. Some work has a due date that already "
        "passed, so no plan can finish it on time. Your homework updates are saved."
    ) in line
    assert "reliable" not in line
    line = run_line(unreliable)
    assert line is not None
    assert "Blossom couldn&#39;t finish a reliable plan this time." in line
    assert "due date" not in line
    line = run_line(another_evening)
    assert line is not None
    assert (
        "Blossom can&#39;t make the plan for Thursday, August 20. Some work is due before "
        "that evening, so no plan can finish it on time."
    ) in line
    assert "today" not in line


def test_her_plan_is_said_unchanged_only_when_her_evening_has_one() -> None:
    """A run for today that ended while her evening had a plan, and still has it, says her
    current plan hasn't changed, on load and asked for by its id."""
    clock = FakeTime()
    first = f"plan:{PLAN_DATE.isoformat()}:first"
    later = f"plan:{PLAN_DATE.isoformat()}:later"
    with browser(clock=clock) as client:
        state = state_of(client)
        settled_run(
            state.drafts,
            draft(f"draft:{first}", "Plan for Wednesday"),
            thread_id=first,
            plan_date=PLAN_DATE,
            now=clock,
        )
        admitted = state.drafts.admit_run(
            later, plan_date=PLAN_DATE, deadline_mono=clock() + RUN_DEADLINE_SECONDS
        )
        state.drafts.end_run(later, reason="timed_out")
        on_load = client.get(PAGE).text
        asked = client.get(f"{PAGE}?run={later}").text

    assert admitted is None
    for page in (on_load, asked):
        line = run_line(page)
        assert line is not None
        assert (
            "Planning took too long, so Blossom stopped. Your current plan hasn&#39;t changed. "
            "Your homework updates are saved."
        ) in line
        assert "A plan is ready." in page


def test_a_run_asked_for_that_adds_nothing_falls_back_to_the_newest_run() -> None:
    """``?run=`` naming a run whose plan was published, or one never made, says what the page
    says on load, so a newer run still being finished shows with its own Check."""
    clock = FakeTime()
    first = f"plan:{PLAN_DATE.isoformat()}:first"
    newer = f"plan:{PLAN_DATE.isoformat()}:newer"
    with browser(clock=clock) as client:
        state = state_of(client)
        settled_run(
            state.drafts,
            draft(f"draft:{first}", "Plan for Wednesday"),
            thread_id=first,
            plan_date=PLAN_DATE,
            now=clock,
        )
        admitted = state.drafts.admit_run(
            newer, plan_date=PLAN_DATE, deadline_mono=clock() + RUN_DEADLINE_SECONDS
        )
        published = client.get(f"{PAGE}?run={first}").text
        never = client.get(f"{PAGE}?run=plan:2026-08-19:never").text

    assert admitted is None
    for page in (published, never):
        line = run_line(page)
        assert line is not None
        assert "The plan request for Wednesday, August 19 is still being finished." in line
        assert run_link(newer, ON_IT) in line
        assert run_link(first, ON_IT) not in line


def test_a_parent_making_her_plan_reads_it_in_her_words(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Her JSON 201 reads the plan for whoever is signed in: a parent gets her plan as the
    family reads it, and she gets it as hers."""
    reading = student_routes.plan_view
    readers: list[str] = []

    def watched(state: ApplicationState, record: DraftRecord, reader: Reader = "student") -> object:
        readers.append(reader)
        return reading(state, record, reader)

    monkeypatch.setattr(student_routes, "plan_view", watched)
    client = client_for(signed_in_household(tmp_path))
    client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
        lambda: [fixture_week_plan()], lambda: [accepting()]
    )
    with client:
        signed_in(client, THEIRS)
        theirs = client.post("/student/plans")
    with browser() as client:
        hers = client.post("/student/plans")

    assert (theirs.status_code, hers.status_code) == (201, 201)
    assert readers == ["family", "student"]


# ------------------------------------------------------- one press, one run

FORM_AGE = timedelta(days=7)


def test_her_plan_form_sent_twice_makes_one_run() -> None:
    """The same form sent twice is one run: the second press lands where the first did and
    replaces nothing."""
    with browser() as client:
        form = plan_form(client)
        first = client.post("/student/actions/plan", data=form)
        made = client.get("/student/plans/today").json()["draft_id"]
        again = client.post("/student/actions/plan", data=form)
        today = client.get("/student/plans/today").json()["draft_id"]
        queue = client.get("/parent/approvals").json()["waiting"]
        runs = runs_recorded(client)

    assert first.status_code == again.status_code == 303
    assert again.headers["location"] == SHOWN
    assert today == made
    assert [item["draft_id"] for item in queue] == [made]
    assert [run for run, _, _ in runs] == [form["run_id"]]


def test_a_stale_plan_button_starts_nothing_and_shows_the_newer_plan() -> None:
    """A page opened before a newer plan was made can't replace it: its press starts nothing,
    says so, and shows the newer plan with a fresh button for a deliberate new request."""
    with browser() as client:
        stale = plan_form(client)
        client.post("/student/actions/plan", data=plan_form(client))
        newer = client.get("/student/plans/today").json()["draft_id"]
        pressed = client.post("/student/actions/plan", data=stale)
        today = client.get("/student/plans/today").json()
        runs = runs_recorded(client)

    assert pressed.status_code == 409
    assert HER_NEWER_PLAN in the_line(pressed.text)
    assert f"{HER_FOR_A_NEW_PLAN}</p>" in the_line(pressed.text)
    assert today["draft_id"] == newer
    assert today["decision"] is None
    assert len(runs) == 1
    fresh = form_fields(pressed.text, "/student/actions/plan")
    assert fresh["run_id"] not in {stale["run_id"], runs[0][0]}
    assert fresh["newest_plan"] == newer


def without(name: str) -> Callable[[dict[str, str]], dict[str, str]]:
    return lambda form: {key: value for key, value in form.items() if key != name}


def with_value(name: str, value: str) -> Callable[[dict[str, str]], dict[str, str]]:
    return lambda form: {**form, name: value}


NOT_WHOLE: dict[str, Callable[[dict[str, str]], Any]] = {
    "no id": without("run_id"),
    "an id of another shape": with_value("run_id", "plan:2026-08-19:abc"),
    "an id in capitals": lambda form: {**form, "run_id": form["run_id"].upper()},
    "no evening": without("evening"),
    "an evening that is no date": with_value("evening", "tonight"),
    "no issue time": without("issued_at"),
    "an issue time that is no time": with_value("issued_at", "a while ago"),
    "an issue time far ahead": lambda form: {
        **form,
        "issued_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    },
    "no basis": without("newest_plan"),
    "a basis naming no plan": with_value("newest_plan", "draft-never-made"),
    "an id sent twice": lambda form: [*form.items(), ("run_id", form["run_id"])],
}


@pytest.mark.parametrize("change", NOT_WHOLE)
def test_a_plan_form_that_is_not_whole_starts_nothing(change: str) -> None:
    """A form from an incomplete or outdated page, or one made by hand, plans nothing, and the
    page offers a fresh button."""
    with browser() as client:
        form = NOT_WHOLE[change](plan_form(client))
        pressed = client.post("/student/actions/plan", data=form)
        runs = runs_recorded(client)

    assert pressed.status_code == 422
    assert str(escape(HER_FORM_NOT_WHOLE)) in the_line(pressed.text)
    assert runs == []
    assert form_fields(pressed.text, "/student/actions/plan")["run_id"]


def test_her_plan_form_from_another_evening_starts_nothing() -> None:
    """A press is never moved to another evening: a form from yesterday's page plans
    nothing today and says which page it came from."""
    with browser() as client:
        form = plan_form(client, evening=(PLAN_DATE - timedelta(days=1)).isoformat())
        pressed = client.post("/student/actions/plan", data=form)
        runs = runs_recorded(client)

    assert pressed.status_code == 409
    assert HER_FORM_FROM_AUGUST_18 in the_line(pressed.text)
    assert runs == []


def test_her_used_form_names_its_runs_evening_when_that_is_not_today() -> None:
    """An id already used for another evening answers with that run's evening and starts
    nothing; her page never answers with the family form's conflict."""
    with browser() as client:
        form = plan_form(client)
        ended_run(
            state_of(client).drafts,
            thread_id=form["run_id"],
            plan_date=PLAN_DATE + timedelta(days=1),
            outcome="interrupted",
        )
        pressed = client.post("/student/actions/plan", data=form)
        runs = runs_recorded(client)

    assert pressed.status_code == 409
    assert HER_FORM_FROM_AUGUST_20 in the_line(pressed.text)
    assert "already asked" not in pressed.text
    assert len(runs) == 1


def test_an_old_plan_form_starts_nothing() -> None:
    """A form issued seven real days ago or more is expired: it plans nothing, even when
    the household's day is pinned to the evening it names."""
    instant = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    with browser(real_clock=FrozenClock(instant, ZoneInfo(FIXTURE_TIMEZONE))) as client:
        form = plan_form(client, issued_at=(instant - FORM_AGE).isoformat())
        pressed = client.post("/student/actions/plan", data=form)
        runs = runs_recorded(client)

    assert pressed.status_code == 409
    assert HER_FORM_EXPIRED in the_line(pressed.text)
    assert runs == []


def test_a_form_whose_run_is_running_offers_only_a_check() -> None:
    """A press whose run is still running starts nothing and offers to check on it, with no
    plan button that could start another paid run."""
    with browser() as client:
        form = plan_form(client)
        blocking = state_of(client).drafts.admit_run(
            form["run_id"], plan_date=PLAN_DATE, deadline_mono=monotonic() + 60
        )
        pressed = client.post("/student/actions/plan", data=form)
        runs = runs_recorded(client)

    assert blocking is None
    assert pressed.status_code == 202
    line = the_line(pressed.text)
    assert "The plan request for Wednesday, August 19 is still being finished." in line
    assert "Check on it." in line
    assert 'action="/student/actions/plan"' not in pressed.text
    assert len(runs) == 1


@pytest.mark.parametrize("reason", ["interrupted", "timed_out"])
def test_a_form_whose_run_ended_answers_as_its_first_press_did(reason: str) -> None:
    """A repeat of a run that ended without a plan says why, as the first press did, with
    Try again on a fresh form, and starts nothing by itself."""
    with browser() as client:
        form = plan_form(client)
        ended_run(
            state_of(client).drafts, thread_id=form["run_id"], plan_date=PLAN_DATE, outcome=reason
        )
        pressed = client.post("/student/actions/plan", data=form)
        runs = runs_recorded(client)

    assert pressed.status_code == 409
    assert str(escape(ended_without_a_plan(reason, parent=False))) in the_line(pressed.text)
    assert TRY_AGAIN in pressed.text
    assert form_fields(pressed.text, "/student/actions/plan")["run_id"] != form["run_id"]
    assert len(runs) == 1


def test_a_press_whose_run_cannot_be_looked_up_is_decided_by_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the first read of a form's run fails, admission decides in its own transaction:
    the form makes its plan once, and the same form sent again starts nothing."""
    with browser() as client:
        form = plan_form(client)
        monkeypatch.setattr(state_of(client).drafts, "run_status", refusing())
        first = client.post("/student/actions/plan", data=form)
        again = client.post("/student/actions/plan", data=form)
        monkeypatch.undo()
        runs = runs_recorded(client)

    assert first.status_code == again.status_code == 303
    assert [(run, status) for run, _, status in runs] == [(form["run_id"], "published")]


def test_a_plan_published_while_her_page_is_read_leaves_its_button_behind_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Her page reads the newest plan's id before the plan it shows, so a plan published
    between the two reads leaves the button naming the earlier one: its press starts
    nothing rather than replace a plan the page may not have shown."""
    with browser() as client:
        drafts = state_of(client).drafts
        before = drafts.newest_published()
        read = drafts.newest_published
        between: list[str] = []

        def then_published() -> str:
            known = read()
            if not between:
                between.append("plan:between")
                landed = Draft(
                    draft_id="draft:plan:between",
                    body="Plan for Wednesday, August 19",
                    created_at=datetime(2026, 8, 19, 22, 0, tzinfo=UTC),
                )
                settled_run(drafts, landed, thread_id="plan:between", plan_date=PLAN_DATE)
            return known

        monkeypatch.setattr(drafts, "newest_published", then_published)
        form = plan_form(client)
        monkeypatch.undo()
        pressed = client.post("/student/actions/plan", data=form)
        runs = runs_recorded(client)

    assert between == ["plan:between"]
    assert form["newest_plan"] == before
    assert pressed.status_code == 409
    assert HER_NEWER_PLAN in the_line(pressed.text)
    assert [run for run, _, _ in runs] == ["plan:between"]
