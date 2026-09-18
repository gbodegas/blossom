"""The parent's page: the same three things as the JSON routes, as forms.

Driven with the test client as a browser would drive it: a form post, a
redirect back to the page, and the page read again. The models are scripted
through the route's builder dependency, over the real stores.
"""

import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends
from fastapi.testclient import TestClient

from blossom.agent.graph import plan_graph_for
from blossom.app import create_app
from blossom.clock import spoken_time
from blossom.dependencies import STATE_ATTRIBUTE, ApplicationState, get_application_state
from blossom.drafts import Draft
from blossom.heuristic_relevance import Criterion, CriterionFinding, CriticVerdict, Judgment
from blossom.intake import identity
from blossom.noticing import read_week
from blossom.plans import DailyPlan, Deferral, PlanBlock
from blossom.routes.parent import ASSIGNMENTS_CHANGED, REASON_MAX_LENGTH
from blossom.routes.runs import NOTHING_TO_SCHEDULE, PlanGraphs, plan_graphs
from blossom.routes.student import ASSIGNMENTS_CHANGED as HER_ASSIGNMENTS_CHANGED
from blossom.settings import ANTHROPIC_API_KEY_VARIABLE
from blossom.stores.project_state import Saved, Undone
from tests.support import FIXTURE_TIMEZONE, SAME_ORIGIN, Scripted, fixture_settings, ok

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
) -> TestClient:
    """A client that does not follow redirects, so the redirect itself is visible.

    It carries a key so the page shows the plan form; the models are scripted,
    so nothing is ever sent with it.
    """
    with_key = {ANTHROPIC_API_KEY_VARIABLE: "not-a-key-and-never-sent"}
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **with_key))
    app.dependency_overrides[plan_graphs] = scripted_graphs(verdict, plans)
    return TestClient(app, follow_redirects=False, headers=SAME_ORIGIN)


def waiting_draft_id(client: TestClient) -> str:
    """Start a run through the form and return the draft it left waiting."""
    posted = client.post("/parent/actions/plan", data={"plan_date": PLAN_DATE.isoformat()})
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
        posted = client.post("/parent/actions/plan", data={"plan_date": PLAN_DATE.isoformat()})
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
        posted = client.post("/parent/actions/plan", data={"plan_date": ""})
        queue = client.get("/parent/approvals").json()["waiting"]

    assert posted.status_code == 303
    assert [item["plan_date"] for item in queue] == ["2026-08-19"]


def test_a_date_that_is_not_one_is_said_rather_than_guessed_at() -> None:
    with browser() as client:
        response = client.post("/parent/actions/plan", data={"plan_date": "next tuesday"})

    assert response.status_code == 422
    assert "is not a date" in response.text
    assert "<h1>Family review</h1>" in response.text


def test_an_unsettled_plan_says_so_above_its_text() -> None:
    with browser(verdict=undecided) as client:
        client.post("/parent/actions/plan", data={"plan_date": PLAN_DATE.isoformat()})
        page = client.get("/parent").text

    assert "could not settle every point. Its notes are at the end of the text." in page
    assert "support rules (could not assess)" in page


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
    assert ">Mark resolved<" in page
    assert "Reply (optional)" in page
    assert "<strong>Blossom's review:</strong> accepted." in page
    assert '<p class="review-heading">Parent review</p>' in page


def test_the_help_buttons_accessible_names_begin_with_their_visible_words() -> None:
    """A button spoken by its visible words is found by them: the name starts with the label."""
    with browser() as client:
        client.post("/student/help-requests")
        page = client.get("/parent").text

    assert 'aria-label="I can help with the request from ' in page
    assert 'aria-label="Mark resolved: request from ' in page
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
    assert "<strong>A parent is on it.</strong> They said: <q>on it</q>" in hers


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
    assert "Reason: looks right." in page
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
    assert "Reason: too late in the evening." in page
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
        client.post("/parent/actions/plan", data={"plan_date": PLAN_DATE.isoformat()})
        one_waiting = client.get("/parent").text
        app.dependency_overrides[plan_graphs] = scripted_graphs(plans=lambda: [tomorrows_plan()])
        client.post(
            "/parent/actions/plan", data={"plan_date": (PLAN_DATE + timedelta(days=1)).isoformat()}
        )
        two_waiting = client.get("/parent").text

    named = r'aria-label="((?:Say|Ask for a change to) the plan for [^"]+)"'
    assert re.findall(named, one_waiting) == [
        "Say the plan for Wednesday, August 19 looks good",
        "Ask for a change to the plan for Wednesday, August 19",
    ]
    labels = re.findall(named, two_waiting)
    assert len(labels) == 4
    assert len(set(labels)) == 4
    assert "Say the plan for Wednesday, August 19, 1 of 2 looks good" in labels
    assert "Ask for a change to the plan for Thursday, August 20, 2 of 2" in labels
    assert "She has this plan on her page already." in two_waiting
    assert "This plan is for Thursday, August 20. It reaches her page on that day" in two_waiting


def test_a_past_evening_is_refused_by_the_form_and_a_past_draft_says_it_is_not_on_her_page() -> (
    None
):
    with browser() as client:
        refused = client.post("/parent/actions/plan", data={"plan_date": "2026-08-18"})
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        state.drafts.record_waiting(
            Draft(draft_id="draft:plan:past", body="Plan for Tuesday", created_at=CREATED),
            thread_id="plan:past",
            plan_date=PLAN_DATE - timedelta(days=1),
            outcome="accepted",
        )
        state.drafts.publish("draft:plan:past")
        page = client.get("/parent").text

    assert refused.status_code == 422
    assert "The evening of 2026-08-18 has passed." in refused.text
    assert "This plan was for Tuesday, August 18, which has passed. It is not on her page" in page


def test_a_waiting_draft_for_a_past_evening_is_never_told_to_plan_again() -> None:
    """A reduced plan left unreviewed past its signal's week: no fresh plan could put it right."""
    with browser() as client:
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        state.drafts.record_waiting(
            Draft(draft_id="draft:plan:past", body="Plan for Tuesday", created_at=CREATED),
            thread_id="plan:past",
            plan_date=PLAN_DATE - timedelta(days=1),
            outcome="accepted",
            too_much=True,
        )
        state.drafts.publish("draft:plan:past")
        record = client.get("/parent/approvals/draft:plan:past").json()
        page = client.get("/parent").text

    assert record["stale"] is None
    assert "Plan again." not in page
    assert 'value="approve"' in page


def test_planning_again_retires_the_plan_before_it_on_the_page() -> None:
    with browser() as client:
        first = waiting_draft_id(client)
        client.post("/parent/actions/plan", data={"plan_date": PLAN_DATE.isoformat()})
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
    """A planner that raises is not a refusal; the page still says so and keeps its queue."""
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()))
    app.dependency_overrides[plan_graphs] = scripted_graphs()
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        draft_id = waiting_draft_id(client)
        app.dependency_overrides[plan_graphs] = scripted_graphs(plans=list)
        response = client.post("/parent/actions/plan", data={"plan_date": PLAN_DATE.isoformat()})
        queue = client.get("/parent/approvals").json()["waiting"]

    assert response.status_code == 500
    assert (
        "The plan could not be made: something went wrong on the way. "
        "What is waiting below is unchanged."
    ) in response.text
    assert "<h1>Family review</h1>" in response.text
    assert [item["draft_id"] for item in queue] == [draft_id]


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
        posted = client.post("/parent/actions/plan", data={"plan_date": ""})

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
        client.post("/parent/actions/plan", data={"plan_date": PLAN_DATE.isoformat()})
        page = client.get("/parent").text

    assert "How this plan was made" in page
    assert '<span class="step-node">retrieve</span>' in page
    assert "Expected: the record&#39;s due dates hold against the school&#39;s sources." in page
    assert "Found: all 7 checks passed." in page
    assert "Found: accepted on every criterion." in page
    assert "Ended without a plan" not in page


def test_a_decided_plan_keeps_the_record_of_how_it_was_made() -> None:
    with browser() as client:
        draft_id = waiting_draft_id(client)
        client.post(f"/parent/actions/decide/{draft_id}", data={"decision": "approve"})
        page = client.get("/parent").text

    assert page.count("How this plan was made") == 1
    assert "Found: accepted on every criterion." in page


def forgetful() -> DailyPlan:
    """Leaves the whole week but one item unmentioned, so tier one fails every round."""
    return DailyPlan(plan_date=PLAN_DATE, blocks=a_plan().blocks[:1])


def test_a_run_that_ended_without_a_plan_is_on_the_page_with_its_steps() -> None:
    with browser(plans=lambda: [forgetful()] * 3) as client:
        posted = client.post("/parent/actions/plan", data={"plan_date": PLAN_DATE.isoformat()})
        page = client.get("/parent").text

    assert posted.status_code == 303
    assert "Ended without a plan" in page
    assert "The plan failed its checks after every revision." in page
    assert "How this run went" in page
    assert page.count('<span class="step-node">plan</span>') == 3
    assert "Found: 1 of 7 checks failed:" in page
    assert "No plans need your review." in page


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
            "/parent/actions/plan", data={"plan_date": PLAN_DATE.isoformat()}
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
        posted = client.post("/parent/actions/plan", data={"plan_date": PLAN_DATE.isoformat()})
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
        refused = client.post("/parent/actions/plan", data={"plan_date": "9999-12-31"})
        over_json = client.post("/parent/plans", json={"plan_date": "9999-12-31"})
        state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
        ended = state.drafts.runs_without_a_draft()

    assert refused.status_code == 422
    assert "The evening of 9999-12-31 is past the edge of the calendar." in refused.text
    assert over_json.status_code == 422
    assert over_json.json()["detail"].startswith("The evening of 9999-12-31 is past the edge")
    assert ended == []
