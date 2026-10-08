# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The parent's page: the same three things as the JSON routes, as forms.

Driven with the test client as a browser would drive it: a form post, a
redirect back to the page, and the page read again. The models are scripted
through the route's builder dependency, over the real stores.
"""

import html
import logging
import pathlib
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from html import unescape
from time import monotonic
from typing import Annotated
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.agent.graph import CompiledPlanGraph, plan_graph_for
from blossom.agent.runs import GRAPH_VERSION
from blossom.agent.steps import StepRecord, describe_past_due
from blossom.app import create_app
from blossom.clock import FrozenClock, spoken_time
from blossom.dependencies import STATE_ATTRIBUTE, ApplicationState, get_application_state
from blossom.drafts import Draft
from blossom.heuristic_relevance import Criterion, CriterionFinding, CriticVerdict, Judgment
from blossom.intake import identity
from blossom.noticing import read_week
from blossom.plans import DailyPlan, Deferral, PlanBlock
from blossom.reconciliation import SourceChannel
from blossom.routes import parent as parent_routes
from blossom.routes import runs as runs_module
from blossom.routes.parent import ASSIGNMENTS_CHANGED, REASON_MAX_LENGTH
from blossom.routes.runs import NOTHING_TO_SCHEDULE, PlanGraphs, plan_graphs
from blossom.routes.student import ASSIGNMENTS_CHANGED as HER_ASSIGNMENTS_CHANGED
from blossom.settings import ANTHROPIC_API_KEY_VARIABLE, REPOSITORY_ROOT
from blossom.stores.drafts import ReviewSnapshot, RunState
from blossom.stores.help_requests import NOTE_MAX_LENGTH
from blossom.stores.project_state import Assignment, Saved, Undone
from tests import support
from tests.support import (
    FAMILY_ASK_AGAIN,
    FAMILY_DATE_NOT_READ,
    FAMILY_NEWER_PLAN_AUGUST_19,
    FAMILY_PLAN_ANOTHER_EVENING,
    FIXTURE_TIMEZONE,
    PAGE_HEADERS,
    SAME_ORIGIN,
    SHOWN_BELOW_WAITING,
    Scripted,
    ended_run,
    family_line,
    family_plan,
    fixture_settings,
    forgetful_fixture_plan,
    form_fields,
    fresh_plan_fields,
    help_group,
    help_reply,
    help_row,
    household_client,
    ok,
    plan_fold_open,
    record,
    runs_recorded,
    settled_run,
    sign_in_as,
    state_of,
    store_of,
    whole_form,
)

PLAN_DATE = date(2026, 8, 19)
CREATED = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)


def a_plan() -> DailyPlan:
    return DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            PlanBlock(
                assignment_id="assignment-canal-essay",
                starts_at=time(16, 30),
                ends_at=time(17, 30),
                rationale="the essay first, while she is fresh",
            ),
            PlanBlock(
                assignment_id="assignment-science-fair-proposal",
                starts_at=time(18, 0),
                ends_at=time(18, 30),
                rationale="nobody has confirmed this date, so it gets done tonight",
            ),
        ],
        deferred=[
            Deferral(assignment_id="assignment-algebra-set", reason="not due until Monday"),
            Deferral(
                assignment_id="assignment-textbook-cover", reason="five minutes on the weekend"
            ),
            Deferral(assignment_id="assignment-reading-log", reason="a page a night is on track"),
            Deferral(assignment_id="assignment-signed-syllabus", reason="ask what the date is"),
            Deferral(assignment_id="assignment-vocabulary-quiz", reason="the portal says Friday"),
        ],
    )


def accepting() -> CriticVerdict:
    return CriticVerdict(
        findings=[
            CriterionFinding(criterion=criterion, critique="reads well", judgment=Judgment.PASSES)
            for criterion in Criterion
        ]
    )


def undecided() -> CriticVerdict:
    return CriticVerdict(
        findings=[
            CriterionFinding(
                criterion=Criterion.SUPPORT_RULES,
                critique="no rules were given",
                judgment=Judgment.CANNOT_TELL,
            )
        ]
    )


def scripted_graphs(
    verdict: Callable[[], CriticVerdict] = accepting,
    plans: Callable[[], list[DailyPlan]] = lambda: [a_plan()],
) -> Callable[..., PlanGraphs]:
    """Scripted models with permission to start, over the app's own stores."""

    def override(
        state: Annotated[ApplicationState, Depends(get_application_state)],
    ) -> PlanGraphs:
        return PlanGraphs(
            build=lambda: plan_graph_for(
                state,
                planner=Scripted(*[ok(plan) for plan in plans()]),
                critic=Scripted(ok(verdict())),
            ),
            may_start=True,
        )

    return override


def browser(
    verdict: Callable[[], CriticVerdict] = accepting,
    plans: Callable[[], list[DailyPlan]] = lambda: [a_plan()],
    real_clock: FrozenClock | None = None,
) -> TestClient:
    """A client that does not follow redirects, so the redirect itself is visible.

    It carries a key so the page shows the plan form; the models are scripted,
    so nothing is ever sent with it. ``real_clock`` pins real time.
    """
    with_key = {ANTHROPIC_API_KEY_VARIABLE: "not-a-key-and-never-sent"}
    app = create_app(
        fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **with_key), real_clock=real_clock
    )
    app.dependency_overrides[plan_graphs] = scripted_graphs(verdict, plans)
    return TestClient(app, follow_redirects=False, headers=SAME_ORIGIN)


def waiting_draft_id(client: TestClient) -> str:
    """Start a run through a fresh page's form and return the draft it left waiting."""
    posted = client.post(
        "/parent/actions/plan", data=fresh_plan_fields(client, plan_date=PLAN_DATE.isoformat())
    )
    assert posted.status_code == 303
    queue = client.get("/parent/approvals").json()["waiting"]
    assert len(queue) == 1
    return str(queue[0]["draft_id"])


# ------------------------------------------------------------------ the page


def test_the_page_renders_with_nothing_waiting() -> None:
    with browser() as client:
        response = client.get("/parent")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<h1>Family review</h1>" in response.text
    assert "No plans need your review." in response.text
    assert "No earlier plans yet." in response.text
    assert 'value="2026-08-19"' in response.text


def test_the_page_is_not_part_of_the_api_schema() -> None:
    with browser() as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert "/parent" not in paths
    assert "/parent/actions/plan" not in paths
    assert "/parent/approvals" in paths


# -------------------------------------------------------------- planning


def test_the_plan_form_runs_the_graph_and_the_page_shows_the_draft() -> None:
    with browser() as client:
        posted = client.post(
            "/parent/actions/plan", data=family_plan(client, PLAN_DATE.isoformat())
        )
        page = client.get("/parent").text

    assert posted.status_code == 303
    assert posted.headers["location"] == "/parent"
    assert "Evening of 2026-08-19" in page
    assert "Wednesday, August 19, 2026" in page
    assert "Blossom's review:</strong> accepted." in page
    assert "Plan for Wednesday, August 19" in page
    assert "Canal Era comparison essay" in page
    assert 'name="decision" value="approve"' in page
    assert "Nothing leaves here on its own." in page


def test_a_blank_date_means_today() -> None:
    with browser() as client:
        posted = client.post("/parent/actions/plan", data=family_plan(client, ""))
        queue = client.get("/parent/approvals").json()["waiting"]

    assert posted.status_code == 303
    assert [item["plan_date"] for item in queue] == ["2026-08-19"]


def test_a_date_that_is_not_one_is_said_rather_than_guessed_at() -> None:
    with browser() as client:
        response = client.post("/parent/actions/plan", data=family_plan(client, "next tuesday"))

    assert response.status_code == 422
    assert "is not a date" in response.text
    assert "<h1>Family review</h1>" in response.text


def test_an_unsettled_plan_says_so_above_its_text() -> None:
    """The line above the plan says where the notes are: open under the plan, in the one fold
    of Blossom's review notes, which holds what the review could not settle."""
    with browser(verdict=undecided) as client:
        client.post("/parent/actions/plan", data=family_plan(client, PLAN_DATE.isoformat()))
        page = client.get("/parent").text

    assert (
        "could not settle every point. Its notes are open under the plan, in Blossom's review "
        "notes." in page
    )
    assert "at the end of the text" not in page
    notes = page[page.index('<details class="steps plan-review" open>') :]
    assert "support rules (could not assess)" in notes[: notes.index("</details>")]
    assert page.count("<summary>Blossom's review notes</summary>") == 1


def test_the_parents_work_with_her_comes_before_planning_for_her() -> None:
    """With a request and a waiting plan, the help action comes first, the review
    second, and the form that starts a plan folds away after both."""
    with browser() as client:
        client.post("/student/help-requests", json={"note": "the outline"})
        waiting_draft_id(client)
        page = client.get("/parent").text

    help_at = page.index("<h2>Help she asked for</h2>")
    review_at = page.index("<h2>Waiting for your review</h2>")
    plan_form_at = page.index('action="/parent/actions/plan"')
    earlier_at = page.index("<summary>Earlier plans</summary>")
    assert help_at < review_at < plan_form_at < earlier_at
    assert page.index('value="accept"') < plan_form_at
    assert "<summary>Help with a plan</summary>" in page
    assert ">I can help<" in page
    assert ">Close request<" in page
    assert ">Reply to your student (optional)</label>" in page
    assert "<strong>Blossom's review:</strong> accepted." in page
    assert '<p class="review-heading">Parent review</p>' in page


def test_the_help_buttons_accessible_names_begin_with_their_visible_words() -> None:
    """A button spoken by its visible words is found by them: the name starts with the label."""
    with browser() as client:
        client.post("/student/help-requests")
        page = client.get("/parent").text

    assert 'aria-label="I can help with the request from ' in page
    assert 'aria-label="Close request from ' in page
    assert "Mark resolved" not in page
    assert "What you say here appears on her page when she refreshes it." in page
    assert "as soon as it is taken" not in page


def test_the_labels_submit_the_same_values_as_before() -> None:
    with browser() as client:
        request_id = client.post("/student/help-requests").json()["request"]["request_id"]
        page = client.get("/parent").text
        helped = client.post(
            f"/parent/actions/help/{request_id}", data={"step": "accept", "response": "on it"}
        )
        hers = client.get("/student/due-this-week").text

    assert 'name="step" value="accept" class="primary"' in page
    assert 'name="step" value="resolve" class="secondary"' in page
    assert helped.status_code == 303
    assert "<strong>A parent is helping.</strong>" in hers
    assert help_reply(help_row(hers, request_id)) == "on it"


def asked_lines(item: dict[str, str]) -> list[tuple[str, str]]:
    """When she asked, as a request's group says it: the request's own day and time in the
    household's zone, from what the JSON route lists, and the evening it was asked for when
    that is another day, as the pinned day makes it here."""
    at = datetime.fromisoformat(item["asked_local"])
    year = "" if at.year == PLAN_DATE.year else f", {at.year}"
    lines = [("when", f"Requested: {at:%A, %B} {at.day}{year} at {spoken_time(at)}")]
    if at.date() != date.fromisoformat(item["evening"]):
        lines.append(("when", "For Wednesday, August 19"))
    return lines


def added_line(update: dict[str, str]) -> tuple[str, str]:
    """When a parent's update was added, as a request's group says it: its own day and time in
    the household's zone, from what the JSON route lists."""
    at = datetime.fromisoformat(update["written_local"])
    year = "" if at.year == PLAN_DATE.year else f", {at.year}"
    return ("when", f"Added: {at:%A, %B} {at.day}{year} at {spoken_time(at)}")


def test_each_request_on_the_family_page_is_one_group_with_a_short_reply_field() -> None:
    """The group her page shows, said to a parent: where it stands, her words under Student's
    request, when she asked, the latest update under Parent update with the day and time it
    was added, and no word about what her page says. The box is short, with the cap, and a
    one-time id rides with it. A waiting request's box is the reply I can help sends; a
    request already taken up calls it Add an update. Close request is its own button in
    both."""
    with browser() as client:
        made = {
            name: client.post("/student/help-requests", json={"note": f"Synthetic {name}"}).json()[
                "request"
            ]["request_id"]
            for name in ("waiting", "taken", "taken quietly", "closed")
        }
        taken, quietly, closed = made["taken"], made["taken quietly"], made["closed"]
        client.post(f"/parent/help-requests/{taken}/accept", json={"response": "Synthetic so far"})
        client.post(f"/parent/help-requests/{quietly}/accept")
        client.post(f"/parent/help-requests/{closed}/resolve", json={"response": "Synthetic last"})
        listed = {item["request_id"]: item for item in client.get("/parent/help-requests").json()}
        page = client.get("/parent").text

    part = page.split('<section id="help-she-asked-for"', 1)[1].split("</section>", 1)[0]
    theirs = ("label", "Student's request")
    reply = ("label", "Parent update")

    def card(name: str) -> str:
        at = part.index(f'action="/parent/actions/help/{made[name]}"')
        return part[part.rindex("<article", 0, at) : part.index("</article>", at)]

    def opening(name: str) -> list[tuple[str, str]]:
        return [theirs, ("words", f"Synthetic {name}"), *asked_lines(listed[made[name]])]

    waiting, helping = ("state", "Waiting for a parent."), ("state", "A parent is helping.")
    assert help_group(card("waiting")) == [waiting, *opening("waiting")]
    assert help_group(card("taken")) == [
        helping,
        *opening("taken"),
        reply,
        ("words", "Synthetic so far"),
        added_line(listed[taken]["updates"][0]),
    ]
    assert help_group(card("taken quietly")) == [helping, *opening("taken quietly")]
    history = part.split("<summary>Closed in the last two weeks</summary>", 1)[1]
    state, *rest = help_group(history)
    assert state[1].startswith("A parent closed this request on ")
    assert rest == [
        *opening("closed"),
        reply,
        ("words", "Synthetic last"),
        added_line(listed[closed]["updates"][0]),
    ]
    for name, label, moves in (
        ("waiting", "Reply to your student (optional)", ["I can help", "Close request"]),
        ("taken", "Add an update", ["Add an update", "Close request"]),
        ("taken quietly", "Add an update", ["Add an update", "Close request"]),
    ):
        shown, key = card(name), made[name]
        assert f'<label for="reply-{key}">{label}</label>' in shown
        field = f'<textarea id="reply-{key}" name="response" rows="2" maxlength="{NOTE_MAX_LENGTH}"'
        assert field in shown
        assert "aria-describedby" not in shown
        assert '<input type="text" name="response"' not in shown
        assert re.fullmatch(
            r"[0-9a-f]{32}", whole_form(shown, f"/parent/actions/help/{key}")["update_id"]
        )
        assert re.findall(r'<button type="submit" name="step"[^>]*>([^<]*)</button>', shown) == (
            moves
        )
    for never in (
        "She said",
        "Her page says",
        "Reply so far",
        "Resolved",
        "Not taken up",
        "Parent reply",
        "Reply when closing",
        "replaces the current reply",
        "Mark resolved",
    ):
        assert never not in part


def test_review_times_read_in_the_households_zone() -> None:
    """The stored stamp stays what it is; the page shows it in the family's own hours,
    including a day whose local date is not the UTC one."""
    with browser() as client:
        draft_id = waiting_draft_id(client)
        client.post(f"/parent/actions/decide/{draft_id}", data={"decision": "approve"})
        record = client.get(f"/parent/approvals/{draft_id}").json()
        page = client.get("/parent").text

    stamped = datetime.fromisoformat(record["decided_at"])
    local = stamped.astimezone(ZoneInfo(FIXTURE_TIMEZONE))
    shown = f"Reviewed {local:%B} {local.day}, {local.year}, {spoken_time(local)} {local:%Z}."
    assert shown in page
    assert "UTC." not in page


# --------------------------------------------------------------- deciding


def test_approving_from_the_page_moves_the_draft_to_decided() -> None:
    with browser() as client:
        draft_id = waiting_draft_id(client)
        posted = client.post(
            f"/parent/actions/decide/{draft_id}",
            data={"decision": "approve", "reason": "looks right"},
        )
        page = client.get("/parent").text
        record = client.get(f"/parent/approvals/{draft_id}").json()

    assert posted.status_code == 303
    assert posted.headers["location"] == "/parent"
    assert "No plans need your review." in page
    assert "<strong>Looks good.</strong>" in page
    assert "Said on her page" not in page
    assert "Note about this plan: looks right." in page
    assert ", 2026, " in page
    assert record["status"] == "APPROVED_FOR_MANUAL_SEND"
    assert record["decision"] == "approved"


def test_refusing_from_the_page_keeps_the_draft_a_draft() -> None:
    with browser() as client:
        draft_id = waiting_draft_id(client)
        client.post(
            f"/parent/actions/decide/{draft_id}",
            data={"decision": "refuse", "reason": "too late in the evening"},
        )
        page = client.get("/parent").text
        record = client.get(f"/parent/approvals/{draft_id}").json()

    assert "<strong>Change asked.</strong> The plan stays as it was until she plans again." in page
    assert "Note about this plan: too late in the evening." in page
    assert record["status"] == "DRAFT"
    assert record["decision"] == "rejected"


def test_a_blank_reason_is_no_reason() -> None:
    with browser() as client:
        draft_id = waiting_draft_id(client)
        client.post(
            f"/parent/actions/decide/{draft_id}", data={"decision": "approve", "reason": "   "}
        )
        record = client.get(f"/parent/approvals/{draft_id}").json()

    assert record["reason"] is None


def test_only_the_two_buttons_are_decisions_and_a_bad_one_is_a_page() -> None:
    """A tampered form value is answered as this page with the problem, not as
    the framework's JSON validation error."""
    with browser() as client:
        draft_id = waiting_draft_id(client)
        response = client.post(f"/parent/actions/decide/{draft_id}", data={"decision": "yes"})
        record = client.get(f"/parent/approvals/{draft_id}").json()

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert "<h1>Family review</h1>" in response.text
    assert "is not one of the two buttons" in response.text
    assert record["decision"] is None


def test_a_reason_over_the_cap_from_the_page_is_answered_as_the_page() -> None:
    """The form caps the field; a request around the form meets the same cap here."""
    with browser() as client:
        draft_id = waiting_draft_id(client)
        response = client.post(
            f"/parent/actions/decide/{draft_id}",
            data={"decision": "approve", "reason": "r" * (REASON_MAX_LENGTH + 1)},
        )
        record = client.get(f"/parent/approvals/{draft_id}").json()

    assert response.status_code == 422
    assert f"A reason is at most {REASON_MAX_LENGTH} characters; this one is 501." in response.text
    assert f'maxlength="{REASON_MAX_LENGTH}"' in response.text
    assert record["decision"] is None


def tomorrows_plan() -> DailyPlan:
    """The same plan, made for the evening after the fixture date."""
    return a_plan().model_copy(update={"plan_date": PLAN_DATE + timedelta(days=1)})


def test_each_decision_button_says_which_draft_it_decides() -> None:
    """One waiting draft is named by its evening; two evenings waiting get a position each."""
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()))
    app.dependency_overrides[plan_graphs] = scripted_graphs()
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post(
            "/parent/actions/plan", data=fresh_plan_fields(client, plan_date=PLAN_DATE.isoformat())
        )
        one_waiting = client.get("/parent").text
        app.dependency_overrides[plan_graphs] = scripted_graphs(plans=lambda: [tomorrows_plan()])
        client.post(
            "/parent/actions/plan",
            data=fresh_plan_fields(client, plan_date=(PLAN_DATE + timedelta(days=1)).isoformat()),
        )
        two_waiting = client.get("/parent").text

    named = (
        r'aria-label="Looks good: (the plan for [^"]+)">Looks good</button>'
        r'|aria-label="(Ask for a change to the plan for [^"]+)"'
    )

    def labels_of(page: str) -> list[str]:
        return ["".join(found) for found in re.findall(named, page)]

    assert labels_of(one_waiting) == [
        "the plan for Wednesday, August 19",
        "Ask for a change to the plan for Wednesday, August 19",
    ]
    labels = labels_of(two_waiting)
    assert len(labels) == 4
    assert len(set(labels)) == 4
    assert "the plan for Wednesday, August 19, 1 of 2" in labels
    assert "Ask for a change to the plan for Thursday, August 20, 2 of 2" in labels
    assert "She has this plan on her page already." in two_waiting
    assert "This plan is for Thursday, August 20. It reaches her page on that day" in two_waiting


NOTE_LABEL = "Note about this plan (optional)"
NOTE_EXAMPLE = "For example: Start with Geometry, then take a short break."
NOTE_HELPER = "Add encouragement or explain what should change."
WHAT_A_CHANGE_DOES = "Ask for a change records your note. It doesn't rewrite the saved plan."


def test_the_review_note_says_what_it_is_with_an_example_and_a_helper_that_stays() -> None:
    """Each waiting plan's note is labeled as a note about that plan, optional; the example is
    a placeholder and never a value; a helper that stays once the placeholder goes is tied to
    the field by its own id; and the form says that Ask for a change records the note without
    rewriting the plan, beside where the note shows for that evening."""
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()))
    app.dependency_overrides[plan_graphs] = scripted_graphs()
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post(
            "/parent/actions/plan", data=fresh_plan_fields(client, plan_date=PLAN_DATE.isoformat())
        )
        app.dependency_overrides[plan_graphs] = scripted_graphs(plans=lambda: [tomorrows_plan()])
        client.post(
            "/parent/actions/plan",
            data=fresh_plan_fields(client, plan_date=(PLAN_DATE + timedelta(days=1)).isoformat()),
        )
        page = client.get("/parent").text

    forms = re.findall(
        r'<form method="post" action="/parent/actions/decide/[^"]+" class="decision">(.*?)</form>',
        page,
        re.S,
    )
    helpers = []
    for form, evening in zip(
        forms, ("She has this plan", "This plan is for Thursday"), strict=True
    ):
        field = re.search(r'<label for="([^"]+)">([^<]*)</label>\s*<input ([^>]*)>', form)
        assert field is not None
        named, label, attributes = field.groups()
        assert label == NOTE_LABEL
        assert f'id="{named}"' in attributes
        assert 'name="reason"' in attributes
        assert f'placeholder="{NOTE_EXAMPLE}"' in attributes
        assert "value=" not in attributes
        described = re.search(r'aria-describedby="([^"]+)"', attributes)
        assert described is not None
        assert f'<p class="helper" id="{described.group(1)}">{NOTE_HELPER}</p>' in form
        helpers.append(described.group(1))
        note = re.search(r'<p class="note">(.*?)</p>', form, re.S)
        assert note is not None
        said = " ".join(note.group(1).split())
        assert said.startswith(f"{WHAT_A_CHANGE_DOES} {evening}")
        assert said.endswith("Nothing leaves here on its own.")
    assert len(set(helpers)) == 2
    assert "Reason (optional)" not in page
    assert "A sentence she would recognize" not in page


def test_a_past_evening_is_refused_by_the_form_and_a_past_draft_says_it_is_not_on_her_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The past plan is settled without a graph, so its review step is taken as one a review
    can resume, as a run paused at the gate leaves it."""
    monkeypatch.setattr(parent_routes, "unresumable_threads", lambda *args: frozenset())
    with browser() as client:
        refused = client.post("/parent/actions/plan", data=family_plan(client, "2026-08-18"))
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        settled_run(
            state.drafts,
            Draft(draft_id="draft:plan:past", body="Plan for Tuesday", created_at=CREATED),
            thread_id="plan:past",
            plan_date=PLAN_DATE - timedelta(days=1),
            outcome="accepted",
        )
        page = client.get("/parent").text

    assert refused.status_code == 422
    assert "The evening of 2026-08-18 has passed." in refused.text
    assert "This plan was for Tuesday, August 18, which has passed. It is not on her page" in page


def test_a_date_problem_for_a_later_evening_is_said_against_that_evening() -> None:
    """Planning the day after tomorrow, work due tomorrow comes before the evening but
    hasn't passed, and the family page doesn't say it has."""
    evening = PLAN_DATE + timedelta(days=2)
    found = describe_past_due(
        ["World History \u00b7 Canal Era comparison essay"], [PLAN_DATE + timedelta(days=1)]
    )
    with browser() as client:
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        ended_run(
            state.drafts,
            thread_id=f"plan:{evening.isoformat()}:later",
            plan_date=evening,
            outcome="date_problem",
            steps=[
                StepRecord(node="retrieve", round=0, expected="", found=found, recorded_at=CREATED)
            ],
        )
        page = client.get("/parent").text

    assert "No plan for Friday, August 21, 2026" in page
    assert "A due date on record comes before the evening being planned" in page
    assert "is due 2026-08-20, before the evening being planned" in page
    assert "already passed" not in page
    assert "before this evening" not in page


def test_a_family_press_that_ends_on_a_date_problem_keeps_the_plan_buttons_words() -> None:
    """Work whose only date is the day before the evening: the press returns to the family
    page, which says the date problem, and its plan button keeps its own words, since
    planning again can't fix a date."""
    quiz = Assignment(
        assignment_id="assignment-map-quiz",
        course="Geography",
        title="Map quiz",
        due_date=None,
        dependencies=[],
        reported_submission_status="not_started",
    )
    with browser() as client:
        state = state_of(client)
        state.project_state.upsert_assignments([quiz])
        state.project_state.record_claims(
            quiz.assignment_id, [record(SourceChannel.LMS, "2026-08-18")]
        )
        posted = client.post(
            "/parent/actions/plan", data=family_plan(client, PLAN_DATE.isoformat())
        )
        page = client.get("/parent").text
        ended = state.drafts.runs_without_a_draft()

    assert posted.status_code == 303
    assert [run.outcome for run in ended] == ["date_problem"]
    assert "A due date on record comes before the evening being planned" in page
    assert '<button type="submit" class="primary">Plan it</button>' in page
    assert "Try again" not in page


def test_a_waiting_draft_for_a_past_evening_is_never_told_to_plan_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reduced plan left unreviewed past its signal's week: no fresh plan could put it right.
    It is settled without a graph, so its review step is taken as one a review can resume."""
    monkeypatch.setattr(parent_routes, "unresumable_threads", lambda *args: frozenset())
    with browser() as client:
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        settled_run(
            state.drafts,
            Draft(draft_id="draft:plan:past", body="Plan for Tuesday", created_at=CREATED),
            thread_id="plan:past",
            plan_date=PLAN_DATE - timedelta(days=1),
            outcome="accepted",
            too_much=True,
        )
        record = client.get("/parent/approvals/draft:plan:past").json()
        page = client.get("/parent").text

    assert record["stale"] is None
    assert "Plan again." not in page
    assert 'value="approve"' in page


def test_planning_again_retires_the_plan_before_it_on_the_page() -> None:
    with browser() as client:
        first = waiting_draft_id(client)
        client.post("/parent/actions/plan", data=family_plan(client, PLAN_DATE.isoformat()))
        page = client.get("/parent").text
        queue = client.get("/parent/approvals").json()["waiting"]

    assert len(queue) == 1
    assert queue[0]["draft_id"] != first
    assert (
        "<strong>Superseded.</strong> A later plan for this evening took its place, "
        "and this one was never reviewed." in page
    )
    assert "a later plan for the evening took its place" not in page
    assert "<summary>The plan as it was</summary>" in page
    assert "The plan as it was reviewed" not in page
    assert "Closed " in page
    assert "Reviewed " not in page


def test_a_failure_on_the_way_is_said_on_the_page_and_the_queue_stays() -> None:
    """A planner that raises is not a refusal: the page says Blossom couldn't finish a
    reliable plan and that her updates are saved, with that outcome's status, and keeps
    its queue. The JSON route answers the run's record, ended interrupted, like any run
    that ended without a plan, never a bare error."""
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()))
    app.dependency_overrides[plan_graphs] = scripted_graphs()
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        draft_id = waiting_draft_id(client)
        app.dependency_overrides[plan_graphs] = scripted_graphs(plans=list)
        response = client.post(
            "/parent/actions/plan", data=fresh_plan_fields(client, plan_date=PLAN_DATE.isoformat())
        )
        queue = client.get("/parent/approvals").json()["waiting"]
        over_json = client.post("/parent/plans", json={"plan_date": PLAN_DATE.isoformat()})
        ended = state_of(client).drafts.latest_run()

    assert response.status_code == 409
    assert (
        "Blossom couldn&#39;t finish a reliable plan this time. Her homework updates are saved. "
        "Family review shows what happened."
    ) in problem_line(response.text)
    assert "went wrong" not in response.text
    assert "<h1>Family review</h1>" in response.text
    assert [item["draft_id"] for item in queue] == [draft_id]
    assert over_json.status_code == 201
    assert ended is not None
    assert (ended.status, ended.reason) == ("ended", "interrupted")
    body = over_json.json()
    assert (body["thread_id"], body["outcome"], body["draft_id"], body["waiting"]) == (
        ended.run_id,
        "interrupted",
        None,
        False,
    )


def test_a_waiting_draft_can_be_decided_from_the_page_without_a_key() -> None:
    """The page says deciding needs no key, so it must not."""
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()))
    app.dependency_overrides[plan_graphs] = scripted_graphs()
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        draft_id = waiting_draft_id(client)
        app.dependency_overrides.clear()

        page_before = client.get("/parent").text
        posted = client.post(f"/parent/actions/decide/{draft_id}", data={"decision": "approve"})
        record = client.get(f"/parent/approvals/{draft_id}").json()

    assert "No API key is configured" in page_before
    assert 'name="decision" value="approve"' in page_before
    assert posted.status_code == 303
    assert record["status"] == "APPROVED_FOR_MANUAL_SEND"


def test_an_unknown_draft_is_a_page_that_says_so() -> None:
    with browser() as client:
        response = client.post("/parent/actions/decide/draft:nobody", data={"decision": "approve"})

    assert response.status_code == 404
    assert "no draft" in response.text
    assert "<h1>Family review</h1>" in response.text


def test_deciding_twice_from_the_page_is_refused_with_the_first_standing() -> None:
    with browser() as client:
        draft_id = waiting_draft_id(client)
        client.post(f"/parent/actions/decide/{draft_id}", data={"decision": "approve"})
        again = client.post(f"/parent/actions/decide/{draft_id}", data={"decision": "refuse"})
        record = client.get(f"/parent/approvals/{draft_id}").json()

    assert again.status_code == 409
    assert "already approved" in again.text
    assert record["decision"] == "approved"


# --------------------------------------------------------------- without a key


def test_without_a_key_the_page_reads_and_the_plan_form_says_why_not() -> None:
    settings = fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat())
    assert settings.anthropic_api_key is None

    with TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN) as client:
        page = client.get("/parent")
        posted = client.post("/parent/actions/plan", data=fresh_plan_fields(client, plan_date=""))

    assert page.status_code == 200
    assert "No API key is configured" in page.text
    assert 'action="/parent/actions/plan"' not in page.text
    assert posted.status_code == 503
    assert ANTHROPIC_API_KEY_VARIABLE in posted.text


def test_dates_on_both_pages_carry_their_year() -> None:
    """Two evenings a year apart must never read the same. Her page frames a week,
    and the week's range carries the year for every card in it."""
    with browser() as client:
        student = client.get("/student/due-this-week").text

    assert "August 17 to" in student
    assert "August 23, 2026" in student
    assert "Recorded date Friday, August 21" in student, "the essay's sources disagree"


# ------------------------------------------------------------- the run's record


def test_the_page_shows_how_a_waiting_plan_was_made() -> None:
    with browser() as client:
        client.post("/parent/actions/plan", data=family_plan(client, PLAN_DATE.isoformat()))
        page = client.get("/parent").text

    assert "How this plan was made" in page
    assert '<span class="step-node">Read the week</span>' in page
    assert '<span class="step-node">First plan</span>' in page
    assert '<span class="step-node">Rules check</span>' in page
    assert '<span class="step-node">Reviewer</span>' in page
    assert "It kept all 8 rules." in page
    assert "The reviewer found nothing to change." in page
    assert "Expected:" not in page
    assert "Found:" not in page
    assert "round 1" not in page
    assert "Plans that couldn't be made" not in page


def test_a_decided_plan_keeps_the_record_of_how_it_was_made() -> None:
    with browser() as client:
        draft_id = waiting_draft_id(client)
        client.post(f"/parent/actions/decide/{draft_id}", data={"decision": "approve"})
        page = client.get("/parent").text

    assert page.count("How this plan was made") == 1
    assert "The reviewer found nothing to change." in page


def ended_fold_open(page: str) -> bool:
    """Whether the fold of plans that couldn't be made is open, read from that fold alone:
    the page's other folds share its class."""
    found = re.search(
        r"""<details class="steps panel-fold"( open)?>\s*<summary>Plans that couldn't be made""",
        page,
    )
    assert found is not None
    return found.group(1) is not None


def forgetful() -> DailyPlan:
    """Leaves the whole week but one item unmentioned, so tier one fails every round."""
    return DailyPlan(plan_date=PLAN_DATE, blocks=a_plan().blocks[:1])


def test_a_run_that_ended_without_a_plan_is_on_the_page_with_its_steps() -> None:
    with browser(plans=lambda: [forgetful()] * 3) as client:
        posted = client.post(
            "/parent/actions/plan", data=family_plan(client, PLAN_DATE.isoformat())
        )
        page = client.get("/parent").text

    assert posted.status_code == 303
    assert ended_fold_open(page)
    assert "<summary>Plans that couldn't be made</summary>" in page
    assert "No plan for Wednesday, August 19, 2026" in page
    assert (
        "Every version Blossom wrote broke one of its rules, so there&#39;s no plan to "
        "review. Planning again may work, since each try writes a fresh plan." in page
    )
    assert "In the last version, it broke 1 of 8 rules:" in page
    assert "ended with checks_failed" not in page
    assert "How this run went" in page
    for label in ("First plan", "Second plan", "Third plan"):
        assert f'<span class="step-node">{label}</span>' in page
    assert page.count('<span class="step-node">Rules check</span>') == 3
    assert re.search(
        r'<p class="note">Took [\d.]+ seconds? in all and 3 model requests\. By step: '
        r"Read the week, [\d.]+ seconds?; First plan, [\d.]+ seconds?; Rules check, ",
        page,
    )
    assert "No plans need your review." in page


def test_a_run_a_later_run_of_its_evening_followed_is_kept_closed() -> None:
    """The failed run is the record of an evening a later plan answered, so the page
    keeps it folded."""
    scripts = iter([[forgetful()] * 3, [a_plan()]])
    with browser(plans=lambda: next(scripts)) as client:
        client.post("/parent/actions/plan", data=family_plan(client, PLAN_DATE.isoformat()))
        client.post("/parent/actions/plan", data=family_plan(client, PLAN_DATE.isoformat()))
        page = client.get("/parent").text

    assert not ended_fold_open(page)
    assert "No plan for Wednesday, August 19, 2026" in page


def test_a_run_for_an_evening_already_past_is_kept_closed() -> None:
    with browser() as client:
        ended_run(
            state_of(client).drafts,
            thread_id="plan:2026-08-18:old",
            plan_date=PLAN_DATE - timedelta(days=1),
            outcome="checks_failed",
            steps=[],
        )
        page = client.get("/parent").text

    assert not ended_fold_open(page)
    assert "No plan for Tuesday, August 18, 2026" in page
    assert "In the last version" not in page


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_a_run_whose_last_check_is_blank_still_shows(blank: str) -> None:
    at = datetime(2026, 8, 19, 20, tzinfo=UTC)
    steps = [
        StepRecord(node="plan", round=1, expected="", found="1 block", recorded_at=at),
        StepRecord(node="verify", round=1, expected="", found=blank, recorded_at=at),
    ]
    with browser() as client:
        ended_run(
            state_of(client).drafts,
            thread_id="plan:2026-08-19:blank",
            plan_date=PLAN_DATE,
            outcome="checks_failed",
            steps=steps,
        )
        page = client.get("/parent")

    assert page.status_code == 200
    assert "No plan for Wednesday, August 19, 2026" in page.text
    assert "In the last version" not in page.text
    assert '<span class="step-node">Rules check</span>' in page.text
    assert re.search(r'<p class="step-line">\s*\.</p>', page.text) is None


def test_a_run_recorded_in_earlier_words_reads_as_sentences() -> None:
    """A run saved before the steps were written as sentences reads as sentences all the
    same, labeled by what each step did."""
    at = datetime(2026, 8, 19, 20, tzinfo=UTC)
    steps = [
        StepRecord(
            node="plan", round=1, expected="", found="1 block asking 60 minutes", recorded_at=at
        ),
        StepRecord(
            node="verify", round=1, expected="", found="all 8 checks passed", recorded_at=at
        ),
    ]
    with browser() as client:
        ended_run(
            state_of(client).drafts,
            thread_id="plan:2026-08-19:earlier",
            plan_date=PLAN_DATE,
            outcome="model_refused",
            steps=steps,
        )
        page = client.get("/parent").text

    assert '<span class="step-node">First plan</span>' in page
    assert '<p class="step-line">1 block asking 60 minutes.</p>' in page
    assert '<p class="step-line">All 8 checks passed.</p>' in page


# ------------------------------------------------------- the plan and the week


IN_THE_WINDOW = {
    "course": "Spanish",
    "title": "Vocabulary list, unit two",
    "due_date": (PLAN_DATE + timedelta(days=1)).isoformat(),
    "kind": "HOMEWORK",
}


def covering_the_new_work() -> DailyPlan:
    """The same plan with the entry above put off, so it mentions everything in the window."""
    new_work = Deferral(
        assignment_id=identity(IN_THE_WINDOW["course"], IN_THE_WINDOW["title"]),
        reason="a short list, later in the week",
    )
    return a_plan().model_copy(update={"deferred": [*a_plan().deferred, new_work]})


def test_work_added_in_the_plans_window_makes_the_waiting_plan_stale() -> None:
    """A plan is made from the week as it stood. Work saved into its window after that
    is said on both pages, the button to approve is gone, and approving is refused."""
    with browser() as client:
        draft_id = waiting_draft_id(client)
        before = client.get("/parent").text
        saved = client.post("/parent/inbox/keep", data=IN_THE_WINDOW)
        page = client.get("/parent").text
        hers = client.get("/student/due-this-week").text
        record = client.get(f"/parent/approvals/{draft_id}").json()
        refused = client.post(f"/parent/actions/decide/{draft_id}", data={"decision": "approve"})
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            plans=lambda: [covering_the_new_work()]
        )
        planned_again = client.post(
            "/parent/actions/plan", data=family_plan(client, PLAN_DATE.isoformat())
        )
        fresh = client.get("/parent").text

    assert 'value="approve"' in before
    assert saved.status_code == 303
    assert ASSIGNMENTS_CHANGED in page
    assert "<strong>Plan again.</strong>" in page
    assert 'value="approve"' not in page
    assert HER_ASSIGNMENTS_CHANGED in hers
    assert record["stale"] is not None
    assert refused.status_code == 409
    assert planned_again.status_code == 303
    assert ASSIGNMENTS_CHANGED not in fresh
    assert 'value="approve"' in fresh


def test_a_status_the_school_reports_makes_the_waiting_plan_stale() -> None:
    """The planner reads the reported status with every assignment, so a report the
    school makes after the plan changes what the plan was made from."""
    told = (
        "Assignments:\n"
        "09/09 World History - A: Homework: Canal Era comparison essay Grade: Missing\n"
    )
    with browser() as client:
        draft_id = waiting_draft_id(client)
        saved = client.post("/parent/inbox/keep", data={"text": told})
        page = client.get("/parent").text
        record = client.get(f"/parent/approvals/{draft_id}").json()

    assert saved.headers["location"] == "/parent?added=0&updated=1&unchanged=0"
    assert ASSIGNMENTS_CHANGED in page
    assert record["stale"] is not None


def test_a_type_corrected_on_a_saved_row_makes_the_waiting_plan_stale() -> None:
    """The type is part of what a plan is made from, so correcting it changes the week."""
    retyped = {"course": "World History", "title": "Canal Era comparison essay", "kind": "TASK"}
    with browser() as client:
        draft_id = waiting_draft_id(client)
        saved = client.post("/parent/inbox/keep", data=retyped)
        page = client.get("/parent").text
        record = client.get(f"/parent/approvals/{draft_id}").json()

    assert saved.headers["location"] == "/parent?added=0&updated=1&unchanged=0"
    assert ASSIGNMENTS_CHANGED in page
    assert record["stale"] is not None


def test_a_saving_that_changes_nothing_or_distant_work_leaves_the_plan_fresh() -> None:
    """Saving what is already on record, or work due well past the plan's week, changes
    nothing the plan was made from, so the plan stands."""
    already = {"course": "World History", "title": "Canal Era comparison essay"}
    distant = {**IN_THE_WINDOW, "due_date": (PLAN_DATE + timedelta(days=60)).isoformat()}
    with browser() as client:
        draft_id = waiting_draft_id(client)
        unchanged = client.post("/parent/inbox/keep", data=already)
        far_off = client.post("/parent/inbox/keep", data=distant)
        page = client.get("/parent").text
        record = client.get(f"/parent/approvals/{draft_id}").json()

    assert unchanged.headers["location"] == "/parent?added=0&updated=0&unchanged=1"
    assert far_off.headers["location"] == "/parent?added=1&updated=0&unchanged=0"
    assert ASSIGNMENTS_CHANGED not in page
    assert 'value="approve"' in page
    assert record["stale"] is None


def test_a_decided_plan_is_history_and_is_not_measured_against_the_week_again() -> None:
    with browser() as client:
        draft_id = waiting_draft_id(client)
        client.post(f"/parent/actions/decide/{draft_id}", data={"decision": "approve"})
        client.post("/parent/inbox/keep", data=IN_THE_WINDOW)
        page = client.get("/parent").text
        hers = client.get("/student/due-this-week").text
        record = client.get(f"/parent/approvals/{draft_id}").json()

    assert "<strong>Looks good.</strong>" in page
    assert ASSIGNMENTS_CHANGED not in page
    assert HER_ASSIGNMENTS_CHANGED not in hers
    assert "Looks good." in hers
    assert record["decision"] == "approved"


def test_her_report_that_work_is_done_makes_the_waiting_plan_stale_and_an_undo_unmakes_it() -> None:
    """What she reports about her part is part of what a plan is made from; taking the
    report back restores the week the plan was made from, and the plan stands again."""
    with browser() as client:
        draft_id = waiting_draft_id(client)
        state: ApplicationState = getattr(
            client.app.state,  # type: ignore[attr-defined]
            STATE_ATTRIBUTE,
        )
        saved = state.project_state.report_status(
            "assignment-canal-essay", "done", None, expected_head=None, now=CREATED, today=PLAN_DATE
        )
        assert isinstance(saved, Saved)
        page = client.get("/parent").text
        record = client.get(f"/parent/approvals/{draft_id}").json()
        undone = state.project_state.undo_report(
            "assignment-canal-essay", saved.report.report_id, now=CREATED, today=PLAN_DATE
        )
        fresh = client.get("/parent").text
        fresh_record = client.get(f"/parent/approvals/{draft_id}").json()

    assert ASSIGNMENTS_CHANGED in page
    assert 'value="approve"' not in page
    assert record["stale"] is not None
    assert isinstance(undone, Undone)
    assert ASSIGNMENTS_CHANGED not in fresh
    assert 'value="approve"' in fresh
    assert fresh_record["stale"] is None


def test_an_evening_with_nothing_left_to_do_is_refused_before_any_run() -> None:
    """Every assignment in the window reported done: the form and the JSON route answer
    409 with the one sentence, and no run is written."""
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
        posted = client.post(
            "/parent/actions/plan", data=family_plan(client, PLAN_DATE.isoformat())
        )
        over_json = client.post("/parent/plans", json={"plan_date": PLAN_DATE.isoformat()})
        ended = state.drafts.runs_without_a_draft()

    assert len(window.assignments) == 7
    assert posted.status_code == 409
    assert NOTHING_TO_SCHEDULE in posted.text
    assert over_json.status_code == 409
    assert over_json.json()["detail"] == NOTHING_TO_SCHEDULE
    assert ended == []


def test_an_evening_past_the_calendars_edge_is_refused_by_the_form_as_by_the_route() -> None:
    """The last day the date type can hold has no week after it to read: the form says so
    with 422, as the JSON route does, rather than fail on the way to reading the week."""
    with browser() as client:
        refused = client.post("/parent/actions/plan", data=family_plan(client, "9999-12-31"))
        over_json = client.post("/parent/plans", json={"plan_date": "9999-12-31"})
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        ended = state.drafts.runs_without_a_draft()

    assert refused.status_code == 422
    assert "The evening of 9999-12-31 is past the edge of the calendar." in refused.text
    assert over_json.status_code == 422
    assert over_json.json()["detail"].startswith("The evening of 9999-12-31 is past the edge")
    assert ended == []


FIXTURE_LEFT_OUT = (
    "Algebra II \u00b7 Quadratic modeling problem set",
    "Science \u00b7 Science fair topic proposal",
    "Science \u00b7 Cover the textbook",
    "English \u00b7 Reading log, week one",
    "English \u00b7 Syllabus, signed",
    "Spanish \u00b7 Vocabulary quiz, unit one",
)


def ended_runs(client: TestClient, plans: Callable[[], list[DailyPlan]]) -> str:
    """Plan the evening from Family review with ``plans`` and return the page from its
    fold of plans that couldn't be made on."""
    client.app.dependency_overrides[plan_graphs] = support.scripted_graphs(  # type: ignore[attr-defined]
        plans, list
    )
    posted = client.post("/parent/actions/plan", data=family_plan(client, PLAN_DATE.isoformat()))
    assert posted.status_code == 303
    page = client.get(posted.headers["location"]).text
    return page[page.index("<summary>Plans that couldn't be made</summary>") :]


def test_a_failed_plan_names_the_homework_it_left_out_by_course_and_title() -> None:
    with support.browser(key=True) as client:
        fold = ended_runs(client, lambda: [forgetful_fixture_plan()] * 3)

    last = re.search(
        r'<p class="note">In the last version, it broke 1 of 8 rules: ([^<]*)\.</p>', fold
    )
    assert last is not None
    assert sorted(last.group(1).split("; ")) == sorted(
        f"the plan leaves out {name}" for name in FIXTURE_LEFT_OUT
    )
    assert fold.count(f"It broke 1 of 8 rules: {last.group(1)}.") == 3
    assert "assignment-" not in fold


def test_homework_the_plan_made_up_is_never_shown_by_its_id() -> None:
    made_up = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            PlanBlock(
                assignment_id="<b>assignment-made-up</b>",
                starts_at=time(16, 0),
                ends_at=time(16, 30),
                rationale="first",
            ),
            *forgetful_fixture_plan().blocks,
        ],
    )
    with support.browser(key=True) as client:
        fold = ended_runs(client, lambda: [made_up] * 3)

    assert "the plan includes homework Blossom doesn&#39;t recognize;" in fold
    assert "made-up" not in fold
    assert "<b>" not in fold


def test_a_title_with_markup_is_shown_as_text_in_a_failed_plan() -> None:
    lab = Assignment(
        assignment_id="assignment-lab-notes",
        course="Science",
        title="<b>Lab</b> & notes",
        due_date=date(2026, 8, 20),
        dependencies=[],
        reported_submission_status="not_started",
    )
    with support.browser(key=True) as client:
        store_of(client).upsert_assignments([lab])
        fold = ended_runs(client, lambda: [forgetful_fixture_plan()] * 3)

    assert "the plan leaves out Science \u00b7 &lt;b&gt;Lab&lt;/b&gt; &amp; notes" in fold
    assert "<b>Lab</b>" not in fold


def test_a_failed_plan_keeps_the_names_the_run_read_after_the_homework_changes() -> None:
    with support.browser(key=True) as client:
        ended_runs(client, lambda: [forgetful_fixture_plan()] * 3)
        renamed = next(
            item
            for item in store_of(client).all_assignments()
            if item.assignment_id == "assignment-algebra-set"
        ).model_copy(update={"title": "Renamed problem set", "course": "Geometry"})
        store_of(client).upsert_assignments([renamed])
        page = client.get("/parent").text
        titles = {item.title for item in store_of(client).all_assignments()}

    fold = page[page.index("<summary>Plans that couldn't be made</summary>") :]
    assert "the plan leaves out Algebra II \u00b7 Quadratic modeling problem set" in fold
    assert "Renamed problem set" not in fold
    assert "Renamed problem set" in titles


LONG_TITLE = "PhotosynthesisAndCellularRespirationReview"


def long_twins() -> list[Assignment]:
    """Two Science assignments due the same day under one title that is a single long word."""
    return [
        Assignment(
            assignment_id=identity("Science", LONG_TITLE, occurrence),
            course="Science",
            title=LONG_TITLE,
            due_date=date(2026, 8, 24),
            dependencies=[],
            reported_submission_status="not_started",
        )
        for occurrence in ("first", "second")
    ]


def deferring_the_twins() -> DailyPlan:
    """The fixture week's passing plan with both twins put off, so it passes every check."""
    plan = support.fixture_week_plan()
    put_off = [
        Deferral(assignment_id=item.assignment_id, reason="due Monday") for item in long_twins()
    ]
    return plan.model_copy(update={"deferred": [*plan.deferred, *put_off]})


def faulting_the_reasons() -> CriticVerdict:
    return CriticVerdict(
        findings=[
            CriterionFinding(
                criterion=criterion,
                critique="the reasons repeat themselves",
                judgment=Judgment.FAILS if criterion is Criterion.RATIONALE else Judgment.PASSES,
            )
            for criterion in Criterion
        ]
    )


def rules_for(css: str, name: str) -> list[str]:
    """The declarations of every rule whose selectors use the class ``name``."""
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [
        inside
        for head, inside in re.findall(r"([^{}]+)\{([^{}]*)\}", plain)
        if re.search(rf"\.{re.escape(name)}(?![\w-])", head)
    ]


@pytest.mark.parametrize(
    ("card", "opening"),
    [
        ("failed run", '<article class="draft outcome-checks_failed">'),
        ("kept plan", '<article class="draft outcome-unsettled">'),
        ("reviewed kept plan", '<article class="draft decided-approved">'),
    ],
    ids=["failed run", "kept plan", "reviewed kept plan"],
)
def test_long_homework_labels_in_a_runs_explanation_stay_whole_and_wrap(
    card: str, opening: str
) -> None:
    labels = [
        f"Science \u00b7 {LONG_TITLE} (due Aug 24, {item.assignment_id})" for item in long_twins()
    ]
    with support.browser(key=True) as client:
        store_of(client).upsert_assignments(long_twins())
        if card == "failed run":
            ended_runs(client, lambda: [forgetful_fixture_plan()] * 3)
        else:
            client.app.dependency_overrides[plan_graphs] = support.scripted_graphs(  # type: ignore[attr-defined]
                lambda: [deferring_the_twins(), deferring_the_twins(), forgetful_fixture_plan()],
                lambda: [faulting_the_reasons()] * 2,
            )
            posted = client.post(
                "/parent/actions/plan", data=family_plan(client, PLAN_DATE.isoformat())
            )
            assert posted.status_code == 303
        if card == "reviewed kept plan":
            draft_id = client.get("/parent/approvals").json()["waiting"][0]["draft_id"]
            decided = client.post(
                f"/parent/actions/decide/{draft_id}", data={"decision": "approve", "reason": ""}
            )
            assert decided.status_code == 303
        page = client.get("/parent").text
    css = (REPOSITORY_ROOT / "blossom" / "static" / "blossom.css").read_text(encoding="utf-8")

    start = page.index(opening)
    article = page[start : page.index("</article>", start)]
    naming = [
        line
        for line in re.findall(r'<p class="step-line">([^<]*)</p>', article)
        if LONG_TITLE in line
    ]
    assert naming
    assert all(label in line for line in naming for label in labels)
    assert any("overflow-wrap: anywhere;" in rule for rule in rules_for(css, "step-line"))
    if card == "failed run":
        assert re.search(
            r"""<details class="steps panel-fold" open>\s*<summary>Plans that couldn't be made"""
            rf"""</summary>\s*<section>\s*{opening}""",
            page,
        )
        note = re.search(r'<p class="note">(In the last version[^<]*)</p>', article)
        assert note is not None
        assert all(label in note.group(1) for label in labels)
        assert ".steps .draft {\n  min-width: 0;\n  overflow-wrap: anywhere;\n}" in css
    for name in ("draft", "step-line", "note", "steps"):
        for rule in rules_for(css, name):
            for cut in ("text-overflow", "overflow:", "overflow-x", "nowrap"):
                assert cut not in rule
            assert "overflow-wrap" not in rule or "overflow-wrap: anywhere;" in rule


# ------------------------------------------------------- where a plan request stands

TOMORROW = PLAN_DATE + timedelta(days=1)
FAMILY_RUN = f"plan:{TOMORROW.isoformat()}:family"
UNSURE = f"plan:{PLAN_DATE.isoformat()}:unsure"
CANT_RESUME = (
    "This plan can&#39;t be approved or refused here, because Blossom can&#39;t finish its "
    "saved review step."
)
UNRESUMABLE_TODAY = (
    f"{CANT_RESUME} It stays on her page until a new plan replaces it, and closes as expired "
    "two weeks after its evening."
)
UNRESUMABLE_ANOTHER_EVENING = f"{CANT_RESUME} It closes as expired two weeks after its evening."
ON_THAT_REQUEST = "Check on that request."
ON_IT = "Check on it."
AGAIN = "Check again."


def family_link(run_id: str, label: str) -> str:
    """The link the family page gives to check a run again, as the page writes it."""
    return f'<a href="/parent?run={run_id.replace(":", "%3A")}">{label}</a>'


def run_line(page: str) -> str | None:
    """The line the family page gives a planning run, or ``None`` when it gives none."""
    marker = page.find('id="plan-run"')
    if marker < 0:
        return None
    start = page.rindex("<p", 0, marker)
    return page[start : page.index("</p>", start) + 4]


def problem_line(page: str) -> str:
    start = page.index('<p class="problem" role="alert" id="problem"')
    return page[start : page.index("</p>", start) + 4]


def clocked_browser(clock: support.FakeTime) -> TestClient:
    """The family page as ``browser`` makes it, its runs timed on ``clock``."""
    with_key = {ANTHROPIC_API_KEY_VARIABLE: "not-a-key-and-never-sent"}
    app = create_app(
        fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **with_key), monotonic=clock
    )
    app.dependency_overrides[plan_graphs] = scripted_graphs()
    return TestClient(app, follow_redirects=False, headers=SAME_ORIGIN)


def test_the_family_page_says_on_load_only_that_a_run_is_being_made_or_finished() -> None:
    """On load the family page reads the household's newest run. One being made is said
    with Check and is not among the plans that couldn't be made; past its deadline it is
    being finished, and is listed as timed out. Once the record ends it, the list says so
    and nothing more is said at the top."""
    clock = support.FakeTime()
    with clocked_browser(clock) as client:
        state = state_of(client)
        quiet = client.get("/parent").text
        admitted = state.drafts.admit_run(
            FAMILY_RUN, plan_date=TOMORROW, deadline_mono=clock() + 90
        )
        being_made = client.get("/parent").text
        clock.advance(91)
        finishing = client.get("/parent").text
        state.drafts.run_status(FAMILY_RUN)
        ended = client.get("/parent").text

    assert run_line(quiet) is None
    assert admitted is None
    line = run_line(being_made)
    assert line is not None
    assert "The plan request for Thursday, August 20 is still being finished." in line
    assert family_link(FAMILY_RUN, ON_IT) in line
    assert "Plans that couldn't be made" not in being_made
    line = run_line(finishing)
    assert line is not None
    assert "Blossom is finishing the last plan request." in line
    assert family_link(FAMILY_RUN, ON_IT) in line
    assert "Plans that couldn't be made" in finishing
    assert run_line(ended) is None
    assert "Plans that couldn't be made" in ended


def test_a_run_asked_for_by_its_id_is_said_on_the_family_page() -> None:
    """``?run=`` on the family page says where that run stands, in a parent's words."""
    with browser() as client:
        state = state_of(client)
        admitted = state.drafts.admit_run(
            FAMILY_RUN, plan_date=TOMORROW, deadline_mono=state.monotonic() + 90
        )
        running = client.get(f"/parent?run={FAMILY_RUN}").text
        state.drafts.end_run(FAMILY_RUN, reason="service_failed")
        ended = client.get(f"/parent?run={FAMILY_RUN}").text

    assert admitted is None
    line = run_line(running)
    assert line is not None
    assert "The plan request for Thursday, August 20 is still being finished." in line
    assert family_link(FAMILY_RUN, ON_IT) in line
    line = run_line(ended)
    assert line is not None
    assert (
        "Blossom couldn&#39;t get a plan from the planning service this time. Her homework "
        "updates are saved."
    ) in line
    assert "hasn&#39;t changed" not in line
    assert "Your" not in line
    assert "service_failed" not in line


def test_a_run_asked_for_that_adds_nothing_falls_back_on_the_family_page() -> None:
    """``?run=`` naming a run whose plan was published, or one never made, says what the
    family page says on load, so a newer run still being finished shows with its Check."""
    first = f"plan:{PLAN_DATE.isoformat()}:first"
    with browser() as client:
        state = state_of(client)
        settled_run(
            state.drafts,
            Draft(draft_id=f"draft:{first}", body="Plan for Wednesday", created_at=CREATED),
            thread_id=first,
            plan_date=PLAN_DATE,
        )
        admitted = state.drafts.admit_run(
            FAMILY_RUN, plan_date=TOMORROW, deadline_mono=state.monotonic() + 90
        )
        published = client.get(f"/parent?run={first}").text
        never = client.get("/parent?run=plan:2026-08-19:never").text

    assert admitted is None
    for page in (published, never):
        line = run_line(page)
        assert line is not None
        assert "The plan request for Thursday, August 20 is still being finished." in line
        assert family_link(FAMILY_RUN, ON_IT) in line


FAMILY_JSON_ANSWERS: dict[str, tuple[Callable[[], Exception], str]] = {
    "not saved": (
        runs_module.NotSaved,
        "Blossom made a plan but couldn't save it. Try again in a moment. Her homework "
        "updates are saved.",
    ),
    "not saved, her plan kept": (
        lambda: runs_module.NotSaved(kept=True),
        "Blossom made a plan but couldn't save it, so her current plan hasn't changed. "
        "Try again in a moment. Her homework updates are saved.",
    ),
    "could not start": (
        runs_module.CouldNotStart,
        "Blossom couldn't start a plan this time. Try again in a moment. Her homework "
        "updates are saved.",
    ),
}


@pytest.mark.parametrize("answer", FAMILY_JSON_ANSWERS)
def test_each_503_from_the_family_json_route_says_her_updates_are_saved(
    answer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan not saved and a run that couldn't start answer the family's JSON route with a
    503 that says her updates are saved, in a parent's words."""
    error, said = FAMILY_JSON_ANSWERS[answer]

    async def refused(*args: object, **kwargs: object) -> object:
        raise error()

    monkeypatch.setattr(parent_routes, "make_plan", refused)
    with browser() as client:
        response = client.post("/parent/plans", json={"plan_date": PLAN_DATE.isoformat()})

    assert response.status_code == 503
    assert response.json()["detail"] == said


FAMILY_ANSWERS: dict[str, tuple[Callable[[], Exception], int, str, str | None]] = {
    "already planning": (
        lambda: runs_module.AlreadyPlanning(
            RunState(
                run_id=FAMILY_RUN,
                plan_date=TOMORROW,
                status="running",
                reason="running",
                seconds_left=40.0,
                plan_unchanged=True,
            )
        ),
        409,
        "The last plan request, for Thursday, August 20, is still being finished. Try again "
        "in about 41 seconds. Her homework updates are saved.",
        family_link(FAMILY_RUN, ON_THAT_REQUEST),
    ),
    "not saved": (
        runs_module.NotSaved,
        503,
        "Blossom made a plan but couldn&#39;t save it. Try again in a moment. Her homework "
        "updates are saved.",
        None,
    ),
    "not saved, her plan kept": (
        lambda: runs_module.NotSaved(kept=True),
        503,
        "Blossom made a plan but couldn&#39;t save it, so her current plan hasn&#39;t changed. "
        "Try again in a moment. Her homework updates are saved.",
        None,
    ),
    "failed on the way": (
        lambda: RuntimeError("a node failed on the way"),
        409,
        "Blossom couldn&#39;t finish a reliable plan this time. Her homework updates are saved. "
        "Family review shows what happened.",
        None,
    ),
    "unconfirmed": (
        lambda: runs_module.Unconfirmed(UNSURE, PLAN_DATE),
        202,
        "Blossom couldn&#39;t confirm that the new plan was saved. Her homework updates are saved.",
        family_link(UNSURE, AGAIN),
    ),
    "could not start": (
        runs_module.CouldNotStart,
        503,
        "Blossom couldn&#39;t start a plan this time. Try again in a moment. Her homework "
        "updates are saved.",
        None,
    ),
}


@pytest.mark.parametrize("answer", FAMILY_ANSWERS)
def test_each_answer_to_the_family_plan_form_says_her_updates_are_saved(
    answer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each way the family's plan form can end without a plan is said at the top of the
    page with the JSON route's status, in a parent's words; a press refused while a run is
    being finished links to it, and an unconfirmed one offers to check again."""
    error, code, said, link = FAMILY_ANSWERS[answer]

    async def refused(*args: object, **kwargs: object) -> object:
        raise error()

    monkeypatch.setattr(parent_routes, "make_plan", refused)
    with browser() as client:
        response = client.post(
            "/parent/actions/plan", data=family_plan(client, PLAN_DATE.isoformat())
        )

    line = problem_line(response.text)
    assert response.status_code == code
    assert said in line
    if link is None:
        assert "?run=" not in line
    else:
        assert link in line
    assert "Your" not in line


@pytest.mark.parametrize("route", ["/parent/plans", "/parent/actions/plan"])
def test_a_week_that_cannot_be_read_before_a_family_press_is_a_plan_that_could_not_start(
    route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A family press whose read of the week fails before any run is admitted couldn't
    start: the JSON route and the plan form answer 503 with the try-again sentence and her
    updates saved, in a parent's words, and no run is written."""

    def unreadable(*args: object, **kwargs: object) -> object:
        msg = "disk I/O error"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(runs_module, "read_week", unreadable)
    evening = {"plan_date": PLAN_DATE.isoformat()}
    with browser() as client:
        if route == "/parent/plans":
            response = client.post(route, json=evening)
        else:
            response = client.post(route, data=family_plan(client, evening["plan_date"]))
        newest = state_of(client).drafts.latest_run()

    said = (
        "Blossom couldn't start a plan this time. Try again in a moment. Her homework "
        "updates are saved."
    )
    assert response.status_code == 503
    if route == "/parent/plans":
        assert response.json()["detail"] == said
    else:
        line = problem_line(response.text)
        assert said.replace("'", "&#39;") in line
        assert "Your" not in line
    assert newest is None


@pytest.mark.parametrize("route", ["/parent/plans", "/parent/actions/plan"])
def test_a_graph_that_cannot_be_built_before_a_family_press_is_a_plan_that_could_not_start(
    route: str,
) -> None:
    """A family press whose plan graph fails to build, before any run is admitted, couldn't
    start: the JSON route and the plan form answer 503 with the try-again sentence and her
    updates saved, in a parent's words, and no run is written."""

    def unbuildable() -> CompiledPlanGraph:
        msg = "the plan graph could not be built"
        raise RuntimeError(msg)

    def graphs() -> PlanGraphs:
        return PlanGraphs(build=unbuildable, may_start=True)

    evening = {"plan_date": PLAN_DATE.isoformat()}
    with browser() as client:
        client.app.dependency_overrides[plan_graphs] = graphs  # type: ignore[attr-defined]
        if route == "/parent/plans":
            response = client.post(route, json=evening)
        else:
            response = client.post(route, data=family_plan(client, evening["plan_date"]))
        newest = state_of(client).drafts.latest_run()

    said = (
        "Blossom couldn't start a plan this time. Try again in a moment. Her homework "
        "updates are saved."
    )
    assert response.status_code == 503
    if route == "/parent/plans":
        assert response.json()["detail"] == said
    else:
        line = problem_line(response.text)
        assert said.replace("'", "&#39;") in line
        assert "Your" not in line
    assert newest is None


def test_a_waiting_plan_no_review_can_resume_says_why_in_place_of_its_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A published waiting plan whose saved review step is missing, or was written by
    another version of the graph, says on the family page why it can't be approved or
    refused here, in place of its two buttons, and stays on her page; a decision pressed
    on it anyway is refused with the same words. A plan whose step can be resumed keeps
    its buttons, and so does one whose step couldn't be read."""
    lost = "plan:2026-08-19:lost"
    with browser() as client:
        state = state_of(client)
        settled_run(
            state.drafts,
            Draft(draft_id=f"draft:{lost}", body="Plan for Wednesday", created_at=CREATED),
            thread_id=lost,
            plan_date=PLAN_DATE,
        )
        missing = client.get("/parent").text
        her_week = client.get("/student/due-this-week").text
        pressed = client.post(f"/parent/actions/decide/draft:{lost}", data={"decision": "approve"})
    with browser() as client:
        state = state_of(client)
        draft_id = waiting_draft_id(client)
        resumable = client.get("/parent").text
        with monkeypatch.context() as patched:
            patched.setattr("blossom.agent.retention.GRAPH_VERSION", GRAPH_VERSION + 1)
            another_version = client.get("/parent").text

        async def unreadable(*args: object, **kwargs: object) -> object:
            msg = "disk I/O error"
            raise OSError(msg)

        with monkeypatch.context() as patched:
            patched.setattr(state.checkpointer, "aget_tuple", unreadable)
            unread = client.get("/parent").text

    form = 'action="/parent/actions/decide/{}"'
    assert UNRESUMABLE_TODAY in missing
    assert form.format(f"draft:{lost}") not in missing
    assert "A plan is ready." in her_week
    assert pressed.status_code == 409
    assert UNRESUMABLE_TODAY in pressed.text
    assert CANT_RESUME not in resumable
    assert form.format(draft_id) in resumable
    assert UNRESUMABLE_TODAY in another_version
    assert form.format(draft_id) not in another_version
    assert CANT_RESUME not in unread
    assert form.format(draft_id) in unread


@pytest.mark.parametrize("days", [1, -1], ids=["tomorrow", "yesterday"])
def test_a_plan_for_another_evening_no_review_can_resume_says_only_when_it_closes(
    days: int,
) -> None:
    """A waiting plan for another evening is not on her page today, so in place of its
    buttons the family page says only that it can't be approved or refused here and when
    it closes; a decision pressed on it is refused with the same words."""
    evening = PLAN_DATE + timedelta(days=days)
    lost = f"plan:{evening.isoformat()}:lost"
    with browser() as client:
        state = state_of(client)
        settled_run(
            state.drafts,
            Draft(draft_id=f"draft:{lost}", body="Plan for the evening", created_at=CREATED),
            thread_id=lost,
            plan_date=evening,
        )
        page = client.get("/parent").text
        pressed = client.post(f"/parent/actions/decide/draft:{lost}", data={"decision": "approve"})

    assert UNRESUMABLE_ANOTHER_EVENING in page
    assert "It stays on her page" not in page
    assert f'action="/parent/actions/decide/draft:{lost}"' not in page
    assert pressed.status_code == 409
    assert UNRESUMABLE_ANOTHER_EVENING in pressed.text
    assert "It stays on her page" not in pressed.text


# ------------------------------------------------------- one press, one run

W_3F = "This form came from an incomplete or outdated page, so no plan was started."
FOCUSED_LINE = '<p class="problem" role="alert" id="problem" tabindex="-1" autofocus>'
RESTING_LINE = '<p class="problem" role="alert" id="problem">'
RUN_CHECK = '<span class="run-check"><a href="/parent?run='
W_4F = "This form is from a page opened more than a week ago, so no plan was started."


def date_kept(page: str) -> str:
    """The value the fresh plan form's date field is filled with."""
    form = page[page.index('action="/parent/actions/plan"') :]
    found = re.search(r'name="plan_date" value="([^"]*)"', form[: form.index("</form>")])
    assert found is not None
    return found.group(1)


def test_the_family_form_sent_twice_makes_one_run() -> None:
    """The same form sent twice is one run, and the waiting plan isn't replaced."""
    with browser() as client:
        form = family_plan(client, PLAN_DATE.isoformat())
        first = client.post("/parent/actions/plan", data=form)
        again = client.post("/parent/actions/plan", data=form)
        queue = client.get("/parent/approvals").json()["waiting"]
        runs = runs_recorded(client)

    assert first.status_code == again.status_code == 303
    assert again.headers["location"] == "/parent"
    assert len(queue) == 1
    assert [run for run, _, _ in runs] == [form["run_id"]]


def test_a_blank_date_plans_the_evening_the_page_was_made_for() -> None:
    """A blank date means the page's own evening, never a later today."""
    later = PLAN_DATE + timedelta(days=1)
    with browser(plans=lambda: [a_plan().model_copy(update={"plan_date": later})]) as client:
        form = family_plan(client, "", evening=later.isoformat())
        posted = client.post("/parent/actions/plan", data=form)
        runs = runs_recorded(client)

    assert posted.status_code == 303
    assert [evening for _, evening, _ in runs] == [later.isoformat()]


def test_a_used_family_form_with_another_date_starts_nothing_and_keeps_the_date() -> None:
    """A form that already asked for one evening starts nothing for another: it names the
    evening it asked for and what came of it, and keeps the newly chosen date."""
    later = (PLAN_DATE + timedelta(days=1)).isoformat()
    with browser() as client:
        form = family_plan(client, PLAN_DATE.isoformat())
        client.post("/parent/actions/plan", data=form)
        again = client.post("/parent/actions/plan", data={**form, "plan_date": later})
        runs = runs_recorded(client)

    assert again.status_code == 409
    assert (
        "This form already asked for a plan for Wednesday, August 19, so nothing was started "
        "for Thursday, August 20. That plan is shown below."
    ) in again.text
    assert date_kept(again.text) == later
    assert FOCUSED_LINE in again.text
    assert len(runs) == 1


def test_a_used_family_form_with_an_unreadable_date_names_its_request() -> None:
    """A form whose date can't be read, sent again, names the evening it already asked for
    and what came of it, and says nothing was started, over the open form, which holds today:
    a date field can't hold what was typed."""
    with browser() as client:
        form = family_plan(client, PLAN_DATE.isoformat())
        client.post("/parent/actions/plan", data=form)
        again = client.post("/parent/actions/plan", data={**form, "plan_date": "next tuesday"})
        runs = runs_recorded(client)

    assert again.status_code == 409
    assert "This form already asked for a plan for Wednesday, August 19." in again.text
    assert family_line(again.text).endswith(f"{FAMILY_DATE_NOT_READ} {FAMILY_PLAN_ANOTHER_EVENING}")
    assert date_kept(again.text) == PLAN_DATE.isoformat()
    assert plan_fold_open(again.text) is True
    assert FOCUSED_LINE in again.text
    assert len(runs) == 1


def test_a_used_family_form_for_a_passed_evening_answers_what_it_did() -> None:
    """The form's own run is found before any date check, so a form sent again after its
    evening passed answers what it did, not that the evening has passed."""
    earlier = PLAN_DATE - timedelta(days=1)
    with browser() as client:
        form = family_plan(client, earlier.isoformat())
        ended_run(
            state_of(client).drafts,
            thread_id=form["run_id"],
            plan_date=earlier,
            outcome="interrupted",
        )
        again = client.post("/parent/actions/plan", data=form)
        runs = runs_recorded(client)

    assert again.status_code == 409
    assert "has passed" not in again.text
    assert str(escape(parent_routes.PLAN_INTERRUPTED)) in again.text
    assert len(runs) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "plan:2026-08-19:abc"},
        {"evening": "tonight"},
        {"issued_at": "a while ago"},
        {"newest_plan": "draft-never-made"},
    ],
)
def test_a_family_form_that_is_not_whole_starts_nothing_and_keeps_the_date(
    change: dict[str, str],
) -> None:
    later = (PLAN_DATE + timedelta(days=2)).isoformat()
    with browser() as client:
        form = {**family_plan(client, later), **change}
        posted = client.post("/parent/actions/plan", data=form)
        runs = runs_recorded(client)

    assert posted.status_code == 422
    assert W_3F in posted.text
    assert FOCUSED_LINE in posted.text
    assert date_kept(posted.text) == later
    assert runs == []


@pytest.mark.parametrize("extra", ["a field of another form", "a field sent twice"])
def test_a_family_form_with_a_stray_or_repeated_field_keeps_the_date(extra: str) -> None:
    """A form that isn't whole because of what it carries, not what it lacks, still keeps the
    evening chosen: the first date it sent is handed back to the page that refuses it."""
    later = (PLAN_DATE + timedelta(days=2)).isoformat()
    with browser() as client:
        form = family_plan(client, later)
        if extra == "a field of another form":
            pairs = [*form.items(), ("stray", "x")]
        else:
            pairs = [*form.items(), ("run_id", form["run_id"])]
        posted = client.post(
            "/parent/actions/plan",
            content=urlencode(pairs),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        runs = runs_recorded(client)

    assert posted.status_code == 422
    assert W_3F in posted.text
    assert date_kept(posted.text) == later
    assert runs == []


def test_an_old_family_form_starts_nothing_and_keeps_the_date() -> None:
    instant = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    later = (PLAN_DATE + timedelta(days=2)).isoformat()
    with browser(real_clock=FrozenClock(instant, ZoneInfo("America/New_York"))) as client:
        form = family_plan(client, later, issued_at=(instant - timedelta(days=7)).isoformat())
        posted = client.post("/parent/actions/plan", data=form)
        runs = runs_recorded(client)

    assert posted.status_code == 409
    assert W_4F in posted.text
    assert FOCUSED_LINE in posted.text
    assert date_kept(posted.text) == later
    assert runs == []


def test_a_family_form_whose_run_is_running_offers_only_a_check() -> None:
    """A family press whose run is still running starts nothing and offers to check on it,
    with no Plan it that could start another paid run."""
    with browser() as client:
        form = family_plan(client, "")
        blocking = state_of(client).drafts.admit_run(
            form["run_id"], plan_date=PLAN_DATE, deadline_mono=monotonic() + 60
        )
        pressed = client.post("/parent/actions/plan", data=form)
        runs = runs_recorded(client)

    assert blocking is None
    assert pressed.status_code == 202
    assert "Check on it." in pressed.text
    assert FOCUSED_LINE in pressed.text
    assert RUN_CHECK in pressed.text
    assert 'action="/parent/actions/plan"' not in pressed.text
    assert [run for run, _, _ in runs] == [form["run_id"]]


def test_a_family_answer_left_unconfirmed_offers_only_a_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A family press whose plan may have been published offers Check again and no Plan it
    that could start another paid run."""

    async def unconfirmed(*args: object, **kwargs: object) -> object:
        raise runs_module.Unconfirmed("a" * 32, PLAN_DATE)

    monkeypatch.setattr(parent_routes, "make_plan", unconfirmed)
    with browser() as client:
        pressed = client.post("/parent/actions/plan", data=family_plan(client, ""))

    assert pressed.status_code == 202
    assert "Check again." in pressed.text
    assert FOCUSED_LINE in pressed.text
    assert RUN_CHECK in pressed.text
    assert 'action="/parent/actions/plan"' not in pressed.text


def test_a_family_form_behind_a_newer_plan_starts_nothing_and_opens_that_plan() -> None:
    """A family page opened before a newer plan for the evening was made can't replace it:
    its press starts nothing, says where the newer plan is, and offers a fresh Plan it."""
    with browser() as client:
        stale = family_plan(client, "")
        assert client.post("/parent/actions/plan", data=family_plan(client, "")).is_redirect
        newer = state_of(client).drafts.newest_published()
        pressed = client.post("/parent/actions/plan", data=stale)
        runs = runs_recorded(client)

    assert pressed.status_code == 409
    said = " ".join(unescape(pressed.text).split())
    assert f"{FAMILY_NEWER_PLAN_AUGUST_19} {SHOWN_BELOW_WAITING} {FAMILY_ASK_AGAIN}" in said
    assert FOCUSED_LINE in pressed.text
    assert len(runs) == 1
    fresh = form_fields(pressed.text, "/parent/actions/plan")
    assert fresh["run_id"] not in {stale["run_id"], runs[0][0]}
    assert fresh["newest_plan"] == newer


def test_a_plan_published_while_the_family_page_is_read_leaves_its_form_behind_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The family page takes its form's newest plan from the one reading its plans come from,
    so a plan published after that reading is neither shown nor named by the form: its press
    starts nothing rather than replace a plan the page didn't show."""
    with browser() as client:
        assert client.post("/parent/actions/plan", data=family_plan(client, "")).is_redirect
        drafts = state_of(client).drafts
        shown = drafts.newest_published()
        read = drafts.review_snapshot
        between: list[str] = []

        def then_published(today: date) -> ReviewSnapshot:
            snapshot = read(today)
            if not between:
                between.append("plan:between")
                landed = Draft(
                    draft_id="draft:plan:between",
                    body="Plan for Wednesday, August 19",
                    created_at=datetime(2026, 8, 19, 22, 0, tzinfo=UTC),
                )
                settled_run(drafts, landed, thread_id="plan:between", plan_date=PLAN_DATE)
            return snapshot

        monkeypatch.setattr(drafts, "review_snapshot", then_published)
        page = client.get("/parent").text
        monkeypatch.undo()
        form = form_fields(page, "/parent/actions/plan")
        pressed = client.post("/parent/actions/plan", data=form)
        runs = runs_recorded(client)

    assert between == ["plan:between"]
    assert shown != ""
    assert form["newest_plan"] == shown
    assert "draft:plan:between" not in page
    assert pressed.status_code == 409
    said = " ".join(unescape(pressed.text).split())
    assert f"{FAMILY_NEWER_PLAN_AUGUST_19} {SHOWN_BELOW_WAITING} {FAMILY_ASK_AGAIN}" in said
    assert [run for run, _, _ in runs][1:] == ["plan:between"]


def test_a_refused_date_is_said_at_the_top_without_taking_the_focus() -> None:
    """Only an answer about the plan form itself takes the focus; an evening that has passed
    is said at the top as any refusal there is."""
    earlier = (PLAN_DATE - timedelta(days=1)).isoformat()
    with browser() as client:
        posted = client.post("/parent/actions/plan", data=family_plan(client, earlier))

    assert posted.status_code == 422
    assert RESTING_LINE in posted.text
    assert 'tabindex="-1" autofocus>' not in problem_line(posted.text)


def test_the_plan_form_and_its_answers_are_held_by_their_own_rules() -> None:
    """Every rule naming the plan form or the run check's place is the one checked in a
    browser: the evening field may shrink inside the form at large text, the check link is
    padded to 44 pixels in its sentence, and a focused line widens its edge as hers does."""
    css = (REPOSITORY_ROOT / "blossom" / "static" / "blossom.css").read_text(encoding="utf-8")
    folded = {
        name: [" ".join(rule.split()) for rule in rules_for(css, name)]
        for name in ("plan-form", "run-check")
    }
    focus = support.declared_for('#problem[tabindex="-1"]:focus')

    assert folded["plan-form"] == [
        "display: flex; flex-wrap: wrap; gap: 0.75rem 1rem; align-items: end;",
        "min-width: 0;",
    ]
    assert folded["run-check"] == ["display: inline-block; padding: 0.8rem 0; margin: -0.8rem 0;"]
    assert focus == support.declared_for(".week-problem:focus")
    assert len(focus) == 1


@pytest.mark.parametrize("who", ["her", "another origin", "signed out"])
def test_a_family_press_refused_at_the_door_reads_and_starts_no_run(
    who: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A family form sent by her, from another origin, or after the sign-out is refused
    before its run is looked up: no run is read and none is started."""
    with household_client("parent", tmp_path) as client:
        sign_in_as(client, "parent")
        form = fresh_plan_fields(client, plan_date="")
        headers = dict(PAGE_HEADERS)
        if who == "her":
            client.cookies.clear()
            sign_in_as(client, "her")
        elif who == "another origin":
            headers["Origin"] = "http://elsewhere.example"
        else:
            assert client.post("/sign-out", headers=PAGE_HEADERS).status_code < 400
        looked: list[str] = []
        monkeypatch.setattr(
            state_of(client).drafts, "run_status", lambda run_id, *_, **__: looked.append(run_id)
        )
        pressed = client.post("/parent/actions/plan", data=form, headers=headers)
        monkeypatch.undo()
        runs = runs_recorded(client)

    assert pressed.status_code in {303, 403}
    if who == "signed out":
        assert pressed.headers["location"] == "/sign-in"
    else:
        assert pressed.status_code == 403
    assert looked == []
    assert runs == []


# ------------------------------------------------------- a refused decision keeps its words

TYPED_REASON = 'Start with the essay & "then" a <short> break.'
DECISION_FORM = (
    r'<form method="post" action="/parent/actions/decide/([^"]+)" class="decision">(.*?)</form>'
)
KEPT_REASON = (
    r'<label for="kept-reason">Note about this plan \(optional\)</label>\s*'
    r'<textarea id="kept-reason" rows="3" readonly>(.*?)</textarea>'
)


def note_boxes(page: str) -> dict[str, tuple[str, str]]:
    """Each waiting plan's note box by its draft id: the box's id and its attributes."""
    boxes = {}
    for draft_id, form in re.findall(DECISION_FORM, page, re.S):
        box = re.search(r'<input id="(review-note-\d+)" ([^>]*)>', form)
        assert box is not None
        boxes[draft_id] = (box.group(1), box.group(2))
    return boxes


def value_of(attributes: str) -> str | None:
    """The value a box holds when the page arrives, read as a browser reads it, or none."""
    found = re.search(r' value="([^"]*)"', f" {attributes}")
    return None if found is None else html.unescape(found.group(1))


def kept_under_the_problem(page: str) -> str | None:
    """The reason shown to copy under the problem, read as a browser reads it, or none."""
    found = re.findall(KEPT_REASON, page, re.S)
    assert len(found) <= 1
    return html.unescape(found[0]) if found else None


def two_waiting_plans(client: TestClient) -> tuple[str, str]:
    """Today's plan and tomorrow's, both waiting: their draft ids, today's first."""
    today = waiting_draft_id(client)
    client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
        plans=lambda: [tomorrows_plan()]
    )
    posted = client.post("/parent/actions/plan", data=family_plan(client, TOMORROW.isoformat()))
    assert posted.status_code == 303
    queue = client.get("/parent/approvals").json()["waiting"]
    tomorrow = ({str(draft["draft_id"]) for draft in queue} - {today}).pop()
    return today, tomorrow


KEPT_IN_THE_BOX = {
    "a decision no button makes": (422, "is not one of the two buttons"),
    "a reason over the cap": (
        422,
        f"A reason is at most {REASON_MAX_LENGTH} characters; this one is {REASON_MAX_LENGTH + 1}.",
    ),
    "Looks good on a plan gone stale": (409, ASSIGNMENTS_CHANGED),
}


@pytest.mark.parametrize("case", list(KEPT_IN_THE_BOX))
def test_a_refused_decision_keeps_the_typed_reason_in_its_plans_box(
    case: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The family page a refusal answers with holds the words as typed in the box of the
    plan they were for, ready to send again, and in no other box. Words that are what was
    refused are marked and focused, with a way to the box; a Looks good on a stale plan
    links to the box too. The refusal saves nothing, and the words go into no address
    and no log line."""
    caplog.set_level(logging.DEBUG)
    words = "r" * (REASON_MAX_LENGTH + 1) if case == "a reason over the cap" else TYPED_REASON
    status, said = KEPT_IN_THE_BOX[case]
    with browser() as client:
        today, tomorrow = two_waiting_plans(client)
        pressed, other = (today, tomorrow) if case.endswith("stale") else (tomorrow, today)
        if case.endswith("stale"):
            assert client.post("/parent/inbox/keep", data=IN_THE_WINDOW).status_code == 303
        decision = "sideways" if case == "a decision no button makes" else "approve"
        refused = client.post(
            f"/parent/actions/decide/{pressed}", data={"decision": decision, "reason": words}
        )
        records = [client.get(f"/parent/approvals/{one}").json() for one in (today, tomorrow)]
        fresh = client.get("/parent").text

    assert refused.status_code == status
    assert "location" not in refused.headers
    assert said in refused.text
    boxes = note_boxes(refused.text)
    box_id, attributes = boxes[pressed]
    assert value_of(attributes) == words
    assert value_of(boxes[other][1]) is None
    assert kept_under_the_problem(refused.text) is None
    at_the_words = case == "a reason over the cap"
    assert ('aria-invalid="true"' in attributes) is at_the_words
    assert ("autofocus" in attributes) is at_the_words
    assert ('aria-describedby="problem ' in attributes) is at_the_words
    linked = at_the_words or case.endswith("stale")
    assert (f'<a href="#{box_id}">Go to the field.</a>' in refused.text) is linked
    assert refused.text.count("Go to the field.") == int(linked)
    assert [(record["decision"], record["reason"]) for record in records] == [(None, None)] * 2
    assert all(value_of(attributes) is None for _, attributes in note_boxes(fresh).values())
    assert words not in caplog.text


def test_a_refused_decision_on_a_plan_with_no_box_keeps_the_reason_under_the_problem(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A plan already decided and one not on record have no box for the words, so they are
    shown to copy under the problem, read-only, and put in no other plan's box. The first
    decision stands with its own words. A plan no review can resume keeps its box on its
    own refusal's page, where the refusal is the authority, so the words are in that box."""
    caplog.set_level(logging.DEBUG)
    lost = "plan:2026-08-19:lost"
    with browser() as client:
        decided, waiting = two_waiting_plans(client)
        first = {"decision": "approve", "reason": "First words."}
        assert client.post(f"/parent/actions/decide/{decided}", data=first).status_code == 303
        settled_run(
            state_of(client).drafts,
            Draft(draft_id=f"draft:{lost}", body="Plan for Wednesday", created_at=CREATED),
            thread_id=lost,
            plan_date=PLAN_DATE,
        )
        presses = {
            f"/parent/actions/decide/{decided}": 409,
            "/parent/actions/decide/draft:nobody": 404,
            f"/parent/actions/decide/draft:{lost}": 409,
        }
        answers = [
            (client.post(path, data={"decision": "refuse", "reason": TYPED_REASON}), status)
            for path, status in presses.items()
        ]
        record = client.get(f"/parent/approvals/{decided}").json()
        untouched = client.get(f"/parent/approvals/{waiting}").json()
        unresumed = client.get(f"/parent/approvals/draft:{lost}").json()

    *no_box, (on_the_lost_plan, _) = answers
    for answer, status in no_box:
        assert answer.status_code == status
        assert "location" not in answer.headers
        assert kept_under_the_problem(answer.text) == TYPED_REASON
        boxes = note_boxes(answer.text)
        assert decided not in boxes
        assert all(value_of(attributes) is None for _, attributes in boxes.values())
        assert "Go to the field." not in answer.text
    assert on_the_lost_plan.status_code == 409
    assert UNRESUMABLE_TODAY in on_the_lost_plan.text
    assert "Go to the field." not in on_the_lost_plan.text
    boxes = note_boxes(on_the_lost_plan.text)
    assert value_of(boxes[f"draft:{lost}"][1]) == TYPED_REASON
    assert value_of(boxes[waiting][1]) is None
    assert kept_under_the_problem(on_the_lost_plan.text) is None
    assert (record["decision"], record["reason"]) == ("approved", "First words.")
    for still in (untouched, unresumed):
        assert (still["decision"], still["reason"]) == (None, None)
    assert TYPED_REASON not in caplog.text


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "spaces"])
def test_a_refusal_keeps_no_reason_when_none_was_typed(blank: str) -> None:
    """A box left empty, or holding only spaces, is no reason: a refusal keeps nothing of it,
    in the box or under the problem."""
    with browser() as client:
        draft_id = waiting_draft_id(client)
        refused = client.post(
            f"/parent/actions/decide/{draft_id}", data={"decision": "sideways", "reason": blank}
        )
        gone = client.post(
            "/parent/actions/decide/draft:nobody", data={"decision": "refuse", "reason": blank}
        )

    assert refused.status_code == 422
    assert value_of(note_boxes(refused.text)[draft_id][1]) is None
    assert gone.status_code == 404
    for answer in (refused, gone):
        assert kept_under_the_problem(answer.text) is None
        assert 'id="kept-reason"' not in answer.text
