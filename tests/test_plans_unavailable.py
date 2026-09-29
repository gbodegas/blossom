"""Her week and the family page when a GET cannot read the record: a page of their own that
says so, keeps any plan already read as saved, and reads nothing more.

The fixture week, a pinned clock, a scripted plan, and a read made to fail at the store
it is asked of. A POST that shows a page again is left as it is.
"""

import logging
import pathlib
import re
import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import date
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.drafts import DraftStatus
from blossom.plan_reading import anchor_for, read_plan
from blossom.reconciliation import SourceChannel
from blossom.routes import parent as parent_routes
from blossom.routes import student as student_routes
from blossom.routes.runs import plan_graphs
from blossom.stores.drafts import DraftRecord
from blossom.templating import page_templates
from tests.support import (
    ESSAY_ID,
    ESSAY_TITLE,
    HER_PAGE,
    HERS,
    PAGE_HEADERS,
    PLAN_DATE,
    SAME_ORIGIN,
    THEIRS,
    accepting,
    browser,
    composed_plan,
    drafts_in_memory,
    fixture_settings,
    plan_on,
    planned,
    record,
    report,
    scripted_graphs,
    signed_in,
    state_of,
    two_sittings,
    walkthrough,
    walkthrough_plan,
)

HER_ALERT = (
    "The record cannot be read right now, so the week, its updates and current date "
    "information are not shown. Try again in a moment."
)
FAMILY_ALERT = (
    "The record cannot be read right now, so reviews, updates and current date information "
    "are not shown. Try again in a moment."
)
DATES_UNREAD = "Current date information cannot be read right now."
HISTORY = "This is the saved plan. Assignment links open the current record."
NEVER = (
    "No plan",
    "no plan",
    "Nothing was saved",
    "Nothing was changed",
    "update-result",
    "Saved as",
    "Undone",
    "Sent",
    "As things stand",
    "Reported done",
    "not on record now",
    "Plan again",
    "does not keep enough detail",
    "When this plan was made",
    "Since this plan was made",
    "<form",
)
"""What a page that could not read the record never says or offers: that a save or a send
happened, that there is no plan, anything about the record as it stands, or a control."""
PATHS = ("BLOSSOM_DATABASE_PATH", "BLOSSOM_CHECKPOINT_PATH", "BLOSSOM_TRACE_PATH")


def main_of(page: str) -> str:
    return page.split('<main id="main">', 1)[1].split("</main>", 1)[0]


def words(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def failing(*_: object, **__: object) -> None:
    msg = "disk I/O error"
    raise sqlite3.OperationalError(msg)


def watched(
    monkeypatch: pytest.MonkeyPatch,
    target: object,
    name: str,
    calls: Counter[str],
    *,
    fails: bool = False,
) -> None:
    """Count every call of ``target.name``, and make it fail when ``fails`` says so."""
    real: Callable[..., object] = getattr(target, name)

    def counted(*given: object, **named: object) -> object:
        calls[name] += 1
        if fails:
            failing()
        return real(*given, **named)

    monkeypatch.setattr(target, name, counted)


def assert_recovery(page: str, alert: str, again: str) -> str:
    """The page's main part, after checking what every such page holds and never holds."""
    main = main_of(page)
    assert main.count("autofocus") == 1
    assert (
        f'<p class="problem" role="alert" id="problem-summary" tabindex="-1" autofocus>{alert} '
        f'<a href="{again}">Try again</a></p>'
    ) in main
    for never in NEVER:
        assert never not in main, never
    return main


def files_in(folder: pathlib.Path) -> dict[str, str]:
    return {name: str(folder / f"{name.lower()}.sqlite3") for name in PATHS}


# ------------------------------------------------------------- her week


@pytest.mark.parametrize("where", ["the record", "her signal"])
def test_her_week_keeps_todays_plan_as_saved_when_a_later_read_fails(
    where: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: Counter[str] = Counter()
    with browser(key=True) as client:
        walkthrough(client)
        made = planned(client)
        state = state_of(client)
        watched(monkeypatch, state.drafts, "latest_for", calls)
        watched(monkeypatch, student_routes, "read_everything", calls, fails=where == "the record")
        watched(
            monkeypatch,
            state.workload_signals,
            "for_evening",
            calls,
            fails=where == "her signal",
        )
        with caplog.at_level(logging.WARNING, logger="blossom.routes.student"):
            shown = client.get(HER_PAGE, headers=PAGE_HEADERS)

    assert shown.status_code == 503
    main = assert_recovery(shown.text, HER_ALERT, "/student/due-this-week")
    assert "<h1>This week cannot be shown right now</h1>" in main
    assert 'id="todays-plan" tabindex="-1"' in main
    today = main[main.index('id="todays-plan"') :]
    assert "<h2>Today's plan</h2>" in today
    plan = plan_on(today, made)
    assert plan.count(DATES_UNREAD) == 1
    assert HISTORY in plan
    assert "Recorded due August 21, 2026. <strong>Check this date.</strong>" in plan
    assert f'href="/student/assignments/{ESSAY_ID}?return_to=today#evidence"' in plan
    assert "A parent has not reviewed it yet." in today
    assert calls["latest_for"] == 1
    assert calls["read_everything"] == 1
    assert calls["for_evening"] == (1 if where == "her signal" else 0)
    logged = [item for item in caplog.records if item.name == "blossom.routes.student"]
    assert [item.getMessage() for item in logged] == [
        "her week could not be read: OperationalError"
    ]


def test_her_week_says_nothing_about_plans_when_none_was_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: Counter[str] = Counter()
    with browser(key=True) as client:
        walkthrough(client)
        planned(client)
        state = state_of(client)
        watched(monkeypatch, state.drafts, "latest_for", calls, fails=True)
        watched(monkeypatch, student_routes, "read_everything", calls)
        shown = client.get(f"{HER_PAGE}?show_plan=1", headers=PAGE_HEADERS)

    assert shown.status_code == 503
    main = assert_recovery(shown.text, HER_ALERT, "/student/due-this-week?show_plan=1")
    assert "todays-plan" not in main
    assert "plan-reading" not in main
    assert "Today" not in words(main)
    assert (calls["latest_for"], calls["read_everything"]) == (1, 0)


def test_her_week_with_no_plan_today_says_nothing_about_plans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser(key=True) as client:
        monkeypatch.setattr(student_routes, "read_everything", failing)
        shown = client.get(HER_PAGE, headers=PAGE_HEADERS)

    assert shown.status_code == 503
    main = assert_recovery(shown.text, HER_ALERT, "/student/due-this-week")
    assert "todays-plan" not in main


def test_a_saves_result_in_the_address_is_neither_said_nor_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a save's redirect, the failed page neither confirms nor denies the save."""
    with browser(key=True) as client:
        walkthrough(client)
        made = planned(client)
        back = report(client, ESSAY_ID, "done")
        monkeypatch.setattr(student_routes, "read_everything", failing)
        shown = client.get(back, headers=PAGE_HEADERS)

    assert "saved=" in back
    assert shown.status_code == 503
    again = back.split("#", 1)[0].replace("&", "&amp;")
    main = assert_recovery(shown.text, HER_ALERT, again)
    plan = plan_on(main, made)
    assert DATES_UNREAD in plan
    assert "reported-done" not in main


@pytest.mark.parametrize("who", ["her", "a parent"])
def test_each_signed_in_reader_meets_the_same_page_in_their_own_words(
    who: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = files_in(tmp_path)
    with browser(key=True, **paths) as client:
        walkthrough(client)
        made = planned(client)
        decided = client.post(
            f"/parent/actions/decide/{made.draft_id}",
            data={"decision": "refuse", "reason": "Start with the outline."},
        )
        assert decided.status_code == 303
    settings = fixture_settings(
        BLOSSOM_TODAY=PLAN_DATE.isoformat(),
        BLOSSOM_STUDENT_PASSPHRASE=HERS,
        BLOSSOM_PARENT_PASSPHRASE=THEIRS,
        **paths,
    )
    with TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN) as client:
        signed_in(client, HERS if who == "her" else THEIRS)
        monkeypatch.setattr(student_routes, "read_everything", failing)
        shown = client.get(HER_PAGE, headers=PAGE_HEADERS)

    assert shown.status_code == 503
    main = assert_recovery(shown.text, HER_ALERT, "/student/due-this-week")
    today = main[main.index('id="todays-plan"') :]
    if who == "her":
        assert "A change is asked for; plan again when you are ready." in today
        assert "<q>Start with the outline.</q>" in today
    else:
        assert "<strong>Change asked.</strong> Reason: Start with the outline." in today
    assert plan_on(today, made).count(DATES_UNREAD) == 1


def test_a_failed_help_read_on_her_week_stays_inside_her_week(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser(key=True) as client:
        walkthrough(client)
        planned(client)
        monkeypatch.setattr(state_of(client).help_requests, "retained", failing)
        shown = client.get(HER_PAGE, headers=PAGE_HEADERS)

    assert shown.status_code == 200
    assert HER_ALERT not in shown.text
    assert "This week cannot be shown right now" not in shown.text


def test_a_date_the_record_has_not_now_is_not_said_on_the_page_that_cannot_read_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A date gone from the record is said only by the page that can read the record."""
    with browser(key=True) as client:
        walkthrough(client)
        made = planned(client)
        store = state_of(client).project_state
        item = store.one_assignment(ESSAY_ID)
        assert item is not None
        store.upsert_assignments([item.model_copy(update={"due_date": None})])
        readable = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text
        monkeypatch.setattr(student_routes, "read_everything", failing)
        monkeypatch.setattr(parent_routes, "read_everything", failing)
        hers = client.get(HER_PAGE, headers=PAGE_HEADERS)
        theirs = client.get("/parent", headers=PAGE_HEADERS)

    removed = (
        "When this plan was made, a due date was recorded. There is no due date on record now."
    )
    assert removed in plan_on(readable, made)
    assert removed in plan_on(family, made)
    for page in (hers, theirs):
        assert page.status_code == 503
        plan = plan_on(main_of(page.text), made)
        assert "Recorded due August 21, 2026." in plan
        assert 'class="plan-now"' not in plan
        for sentence in (removed, "Since this plan was made", "The sources give", "No readable"):
            assert sentence not in plan


# ------------------------------------------------------------- the family page


def later_plan(
    client: TestClient, namesake: str, evening: str, decision: str | None
) -> DraftRecord:
    """A plan for a later evening, made through the family page and decided there."""
    day = date.fromisoformat(evening)
    client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
        lambda: [walkthrough_plan(namesake).model_copy(update={"plan_date": day})],
        lambda: [accepting()],
    )
    made = client.post("/parent/plans", json={"plan_date": evening})
    assert made.status_code == 201, made.text[:300]
    found = state_of(client).drafts.get(made.json()["draft_id"])
    assert found is not None
    if decision is not None:
        decided = client.post(
            f"/parent/actions/decide/{found.draft_id}",
            data={"decision": decision, "reason": "Fine."},
        )
        assert decided.status_code == 303
    return found


def stored_plan(client: TestClient, evening: date) -> DraftRecord:
    """An approved plan for a later evening, saved and published through the drafts store as
    a paused run leaves it."""
    drafts = state_of(client).drafts
    made = composed_plan(
        two_sittings().model_copy(update={"plan_date": evening}),
        draft_id=f"draft:plan:{evening}:stored",
    )
    drafts.record_waiting(
        made.draft,
        thread_id=f"thread-{evening}-stored",
        plan_date=evening,
        outcome="accepted",
        plan_assignment_ids=made.snapshot.assignment_ids,
        plan_snapshot=made.snapshot,
    )
    drafts.publish(made.draft.draft_id)
    drafts.record_decision(
        made.draft.draft_id,
        status=DraftStatus.APPROVED_FOR_MANUAL_SEND,
        decision="approved",
        reason=None,
    )
    found = drafts.get(made.draft.draft_id)
    assert found is not None
    return found


@pytest.mark.parametrize(
    ("where", "target"),
    [
        ("the record", "read_everything"),
        ("her signal", "for_evening"),
        ("her requests for help", "open_requests"),
    ],
)
def test_the_family_page_keeps_every_plan_in_force_as_saved_when_a_later_read_fails(
    where: str, target: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: Counter[str] = Counter()
    with browser(key=True) as client:
        namesake = walkthrough(client)
        replaced = planned(client)
        decided = client.post(
            f"/parent/actions/decide/{replaced.draft_id}", data={"decision": "approve"}
        )
        assert decided.status_code == 303
        today = planned(client)
        tomorrow = later_plan(client, namesake, "2026-08-20", "refuse")
        later = stored_plan(client, date(2026, 8, 21))
        state = state_of(client)
        watched(monkeypatch, state.drafts, "review_snapshot", calls)
        watched(
            monkeypatch, parent_routes, "read_everything", calls, fails=target == "read_everything"
        )
        watched(
            monkeypatch,
            state.workload_signals,
            "for_evening",
            calls,
            fails=target == "for_evening",
        )
        watched(
            monkeypatch,
            state.help_requests,
            "open_requests",
            calls,
            fails=target == "open_requests",
        )
        with caplog.at_level(logging.WARNING, logger="blossom.routes.parent"):
            shown = client.get("/parent?plan=x&refreshed=1", headers=PAGE_HEADERS)

    assert shown.status_code == 503
    main = assert_recovery(shown.text, FAMILY_ALERT, "/parent?refreshed=1&amp;plan=x")
    assert "<h1>Family review</h1>" in main
    assert "<h2>Plans for today and later evenings</h2>" in main
    assert anchor_for(replaced.draft_id) not in main
    order = [main.index(f'id="{anchor_for(plan.draft_id)}"') for plan in (today, tomorrow, later)]
    assert order == sorted(order)
    for plan, words_said in (
        (today, "Waiting for your review."),
        (tomorrow, "Change asked."),
        (later, "Looks good."),
    ):
        at = main.index(f'id="{anchor_for(plan.draft_id)}"')
        article = main[main.rindex("<article", 0, at) : main.index("</article>", at)]
        assert f"<strong>{words_said}</strong>" in article, where
        assert plan_on(article, plan).count(DATES_UNREAD) == 1
        assert "Recorded due" in article
        back = f"?return_to=family&amp;plan_id={quote(plan.draft_id, safe='')}"
        assert f'href="/student/assignments/{ESSAY_ID}{back}#evidence"' in article
    assert main.count(DATES_UNREAD) == 3
    assert calls["review_snapshot"] == 1
    assert calls[target] == 1
    assert calls["read_everything"] <= 1
    logged = [item for item in caplog.records if item.name == "blossom.routes.parent"]
    assert [item.getMessage() for item in logged] == [
        "the family page could not be read: OperationalError"
    ]
    for item in logged:
        assert ESSAY_TITLE not in item.getMessage()


def test_the_family_page_says_nothing_about_plans_when_none_was_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: Counter[str] = Counter()
    with browser(key=True) as client:
        walkthrough(client)
        planned(client)
        state = state_of(client)
        watched(monkeypatch, state.drafts, "review_snapshot", calls, fails=True)
        watched(monkeypatch, parent_routes, "read_everything", calls)
        shown = client.get("/parent", headers=PAGE_HEADERS)

    assert shown.status_code == 503
    main = assert_recovery(shown.text, FAMILY_ALERT, "/parent")
    assert "Plans for today" not in main
    assert "plan-reading" not in main
    assert (calls["review_snapshot"], calls["read_everything"]) == (1, 0)


# ------------------------------------------------------------- a POST is not this page's


def test_a_refused_post_that_shows_a_page_again_answers_as_it_did_when_the_record_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal shown again after a POST still answers 500 when the record fails."""
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()))
    with TestClient(
        app, headers=SAME_ORIGIN, raise_server_exceptions=False, follow_redirects=False
    ) as client:
        monkeypatch.setattr(student_routes, "read_everything", failing)
        monkeypatch.setattr(parent_routes, "read_everything", failing)
        hers = client.post("/student/actions/plan")
        theirs = client.post("/parent/actions/decide/draft:any", data={"decision": "sideways"})

    for answer in (hers, theirs):
        assert answer.status_code == 500
        assert HER_ALERT not in answer.text
        assert FAMILY_ALERT not in answer.text


def test_the_page_is_only_for_a_failed_read_of_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another kind of failure is not taken for an unreadable record."""
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat()))
    with TestClient(
        app, headers=SAME_ORIGIN, raise_server_exceptions=False, follow_redirects=False
    ) as client:

        def broken(*_: object, **__: object) -> None:
            msg = "not a read of the record"
            raise RuntimeError(msg)

        monkeypatch.setattr(student_routes, "read_everything", broken)
        shown = client.get(HER_PAGE, headers=PAGE_HEADERS)

    assert shown.status_code == 500
    assert HER_ALERT not in shown.text


def test_a_readable_record_still_shows_every_page_as_before() -> None:
    with browser(key=True) as client:
        walkthrough(client)
        planned(client)
        store = state_of(client).project_state
        store.record_claims(ESSAY_ID, [record(SourceChannel.LMS, "2026-08-19")])
        hers = client.get(HER_PAGE, headers=PAGE_HEADERS)
        family = client.get("/parent", headers=PAGE_HEADERS)

    for page in (hers, family):
        assert page.status_code == 200
        assert DATES_UNREAD not in page.text
        assert "autofocus" not in main_of(page.text)


# ------------------------------------------------------------- the saved reading on its own


def test_a_plan_shown_as_saved_says_once_that_dates_cannot_be_read_and_a_text_plan_does_not() -> (
    None
):
    made = composed_plan()
    store = drafts_in_memory()
    try:
        store.record_waiting(
            made.draft,
            thread_id="plan:2026-08-19:unread",
            plan_date=PLAN_DATE,
            outcome="unsettled",
            plan_assignment_ids=made.snapshot.assignment_ids,
            plan_snapshot=made.snapshot,
        )
        saved = store.get(made.draft.draft_id)
    finally:
        store.close()
    assert saved is not None
    partial = page_templates().get_template("plan_reading.html")
    shown = read_plan(saved, reader="student", dates_unread=True)
    text = read_plan(
        saved.model_copy(update={"plan_snapshot": None}), reader="student", dates_unread=True
    )
    html = partial.render(reading=shown)

    assert shown.dates_note == DATES_UNREAD
    assert html.count(DATES_UNREAD) == 1
    assert all(row.now is None for row in shown.blocks)
    assert all(row.now is None for row in shown.deferrals)
    assert not shown.live
    assert 'class="plan-now"' not in html
    assert text.dates_note is None
    assert DATES_UNREAD not in partial.render(reading=text)
