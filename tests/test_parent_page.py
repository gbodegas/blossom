"""The parent's page: the same three things as the JSON routes, as forms.

Driven with the test client as a browser would drive it: a form post, a
redirect back to the page, and the page read again. The models are scripted
through the route's builder dependency, over the real stores.
"""

from collections.abc import Callable
from datetime import date, time
from typing import Annotated

from fastapi import Depends
from fastapi.testclient import TestClient

from blossom.agent.graph import plan_graph_for
from blossom.app import create_app
from blossom.dependencies import ApplicationState, get_application_state
from blossom.heuristic_relevance import Criterion, CriterionFinding, CriticVerdict, Judgment
from blossom.plans import DailyPlan, Deferral, PlanBlock
from blossom.routes.parent import PlanGraphs, plan_graphs
from blossom.settings import ANTHROPIC_API_KEY_VARIABLE
from tests.support import Scripted, fixture_settings, ok

PLAN_DATE = date(2026, 8, 19)


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
        deferred=[Deferral(assignment_id="assignment-algebra-set", reason="not due until Monday")],
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
) -> Callable[..., PlanGraphs]:
    """Scripted models with permission to start, over the app's own stores."""

    def override(
        state: Annotated[ApplicationState, Depends(get_application_state)],
    ) -> PlanGraphs:
        return PlanGraphs(
            build=lambda: plan_graph_for(
                state, planner=Scripted(ok(a_plan())), critic=Scripted(ok(verdict()))
            ),
            may_start=True,
        )

    return override


def browser(verdict: Callable[[], CriticVerdict] = accepting) -> TestClient:
    """A client that does not follow redirects, so the redirect itself is visible.

    It carries a key so the page shows the plan form; the models are scripted,
    so nothing is ever sent with it.
    """
    with_key = {ANTHROPIC_API_KEY_VARIABLE: "not-a-key-and-never-sent"}
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **with_key))
    app.dependency_overrides[plan_graphs] = scripted_graphs(verdict)
    return TestClient(app, follow_redirects=False)


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
    assert "<h1>Review</h1>" in response.text
    assert "Nothing is waiting." in response.text
    assert "Nothing has been decided yet." in response.text
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
    assert "The reviewer accepted this plan." in page
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
    assert "<h1>Review</h1>" in response.text


def test_an_unsettled_plan_says_so_above_its_text() -> None:
    with browser(verdict=undecided) as client:
        client.post("/parent/actions/plan", data={"plan_date": PLAN_DATE.isoformat()})
        page = client.get("/parent").text

    assert "The reviewer did not settle on this plan." in page
    assert "support rules (CANNOT_TELL)" in page


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
    assert "Nothing is waiting." in page
    assert "<strong>Approved.</strong> Marked for you to send by hand." in page
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

    assert "<strong>Refused.</strong> Kept as a draft." in page
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
    assert "<h1>Review</h1>" in response.text
    assert "is not one of the two buttons" in response.text
    assert record["decision"] is None


def test_a_waiting_draft_can_be_decided_from_the_page_without_a_key() -> None:
    """The page says deciding needs no key, so it must not."""
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()))
    app.dependency_overrides[plan_graphs] = scripted_graphs()
    with TestClient(app, follow_redirects=False) as client:
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
    assert "<h1>Review</h1>" in response.text


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

    with TestClient(create_app(settings), follow_redirects=False) as client:
        page = client.get("/parent")
        posted = client.post("/parent/actions/plan", data={"plan_date": ""})

    assert page.status_code == 200
    assert "No API key is configured" in page.text
    assert 'action="/parent/actions/plan"' not in page.text
    assert posted.status_code == 503
    assert ANTHROPIC_API_KEY_VARIABLE in posted.text


def test_dates_on_both_pages_carry_their_year() -> None:
    """Two evenings a year apart must never read the same."""
    with browser() as client:
        student = client.get("/student/due-this-week").text

    assert "Due Friday, August 21, 2026" in student
