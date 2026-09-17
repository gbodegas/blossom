"""Her own update on an assignment, from her page: Done or Not yet, with a note if she wants.

Synthetic fixtures, a pinned clock, and forms alone: what a card offers, what a
save does and says, what a save from a page that has moved on meets, what an
undo restores, who may make an update, what it means for the plan, and what
the family page makes of her word beside the school's.
"""

import pathlib
import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import Depends
from fastapi.testclient import TestClient
from langchain_core.messages import BaseMessage
from markupsafe import escape

from blossom.agent.graph import ModelAnswer, plan_graph_for
from blossom.app import create_app
from blossom.assignment_status import statuses_for
from blossom.dependencies import STATE_ATTRIBUTE, ApplicationState, get_application_state
from blossom.noticing import planning_digest, read_week
from blossom.plans import DailyPlan
from blossom.routes.parent import ASSIGNMENTS_CHANGED as THEIR_ASSIGNMENTS_CHANGED
from blossom.routes.parent import PLAN_INCLUDES_DONE as SHE_REPORTS
from blossom.routes.runs import NOTHING_TO_SCHEDULE, PlanGraphs, plan_graphs
from blossom.routes.student import (
    ASSIGNMENTS_CHANGED,
    CHOOSE_ONE,
    NOT_HERS_TO_UPDATE,
    NOTE_TOO_LONG,
    PLAN_INCLUDES_DONE,
    PLAN_WINDOW_DONE,
    SAVED_ELSEWHERE,
    UPDATE_ALREADY_SAVED,
    UPDATE_SAVED,
    UPDATE_UNDONE,
)
from blossom.settings import ANTHROPIC_API_KEY_VARIABLE, Settings
from blossom.stores.project_state import (
    Assignment,
    AssignmentKind,
    ProjectStateStore,
    Saved,
)
from tests.support import (
    PLAN_DATE,
    SAME_ORIGIN,
    Scripted,
    accepting,
    fixture_clock,
    fixture_settings,
    fixture_week_plan,
    ok,
    scripted_graphs,
)

PAGE = "/student/due-this-week"
ESSAY = "assignment-canal-essay"
ESSAY_TITLE = "Canal Era comparison essay"
LOG = "assignment-reading-log"
QUIZ = "assignment-vocabulary-quiz"
WEEK = "2026-08-17"
"""The Monday of the fixture week her page shows on the pinned day."""
HERS = "the blue bicycle in the hallway"
THEIRS = "coffee before the school run"
PAGE_HEADERS = {"Accept": "text/html"}
MISSING_EMAIL = (
    "Assignments:\n08/19 World History - A: Homework: Canal Era comparison essay Grade: Missing\n"
)


def browser(*, key: bool = False, **environ: str) -> TestClient:
    """Her page on the pinned day, with scripted models when ``key`` is set."""
    with_key = {ANTHROPIC_API_KEY_VARIABLE: "not-a-key-and-never-sent"} if key else {}
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **with_key, **environ))
    if key:
        app.dependency_overrides[plan_graphs] = scripted_graphs(
            lambda: [fixture_week_plan()], lambda: [accepting()]
        )
    return TestClient(app, follow_redirects=False, headers=SAME_ORIGIN)


def signed_in_household(tmp_path: pathlib.Path) -> Settings:
    return fixture_settings(
        BLOSSOM_TODAY=PLAN_DATE.isoformat(),
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
        BLOSSOM_STUDENT_PASSPHRASE=HERS,
        BLOSSOM_PARENT_PASSPHRASE=THEIRS,
    )


def state_of(client: TestClient) -> ApplicationState:
    state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
    return state


def card_for(page: str, assignment_id: str) -> str:
    """One card or list entry, from its id to the end of its update block."""
    start = page.index(f'id="assignment-{assignment_id}"')
    end = page.index("</div>\n", page.index('<div class="update">', start))
    return page[start:end]


def hidden(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match is not None, name
    return match.group(1)


def report(client: TestClient, assignment_id: str, status: str, note: str = "", **more: str) -> str:
    """Send her update from the card as it stands and return the address it goes back to."""
    page = client.get(
        PAGE,
        params={"week": more.pop("week", WEEK), "change": assignment_id},
        headers=PAGE_HEADERS,
    ).text
    head = hidden(card_for(page, assignment_id), "expected_report_id")
    answer = client.post(
        f"/student/actions/assignments/{assignment_id}/report",
        data={"status": status, "note": note, "expected_report_id": head, "week": WEEK, **more},
    )
    assert answer.status_code == 303, (answer.status_code, answer.text[:400])
    return answer.headers["location"]


def test_a_card_offers_her_update_and_a_done_folds_it_under_the_active_cards() -> None:
    """The form is Done or Not yet, nothing chosen, a note behind a fold, and the line that
    says what Done means. Saved, the card says so, shows the update with its day and what
    it means, offers Change and Undo, and folds under the active cards with a count."""
    with browser() as client:
        before = client.get(PAGE, headers=PAGE_HEADERS).text
        card = card_for(before, ESSAY)
        location = report(client, ESSAY, "done", "Turned in on paper.\r\nTwo pages.")
        after = client.get(location, headers=PAGE_HEADERS).text
        history = state_of(client).project_state.student_reports(ESSAY)

    assert "<legend>Your update</legend>" in card
    assert 'type="radio" name="status" value="done">' in card
    assert 'type="radio" name="status" value="not_yet">' in card
    assert "checked" not in card
    assert "Done means you have finished your part. It does not turn work in." in card
    assert "<summary>Add a note (optional)</summary>" in card
    assert "Your parents can read this. Notes on work being planned are shared" in card
    assert hidden(card, "expected_report_id") == ""
    assert hidden(card, "week") == WEEK
    assert location == f"{PAGE}?week={WEEK}&saved={ESSAY}#assignment-{ESSAY}"
    active, _, folded = after.partition('<details class="steps reported-done" open>')
    assert f'id="assignment-{ESSAY}"' not in active
    assert "<summary>Reported done (1)</summary>" in folded
    saved = card_for(folded, ESSAY)
    assert UPDATE_SAVED in saved
    assert '<span class="pill">Your update: Done</span>' in saved
    assert "Reported August 19" in saved
    assert "You wrote: <q>Turned in on paper.\nTwo pages.</q>" in saved
    assert "This is out of work to plan. Your school record is separate." in saved
    assert ">Change</button>" in saved
    assert f'action="/student/actions/assignments/{ESSAY}/undo-report"' in saved
    assert "<legend>Your update</legend>" not in saved
    assert [(item.status, item.note) for item in history] == [
        ("done", "Turned in on paper.\nTwo pages.")
    ]
    assert history[0].reported_on == PLAN_DATE


def test_the_same_update_is_already_saved_and_a_changed_note_is_a_new_one() -> None:
    with browser() as client:
        first = report(client, ESSAY, "done", "Finished.")
        same = report(client, ESSAY, "done", "  Finished.\r\n")
        page = client.get(same, headers=PAGE_HEADERS).text
        changed = report(client, ESSAY, "done", "Finished, all of it.")
        history = state_of(client).project_state.student_reports(ESSAY)

    assert first.endswith(f"&saved={ESSAY}#assignment-{ESSAY}")
    assert same.endswith(f"&same={ESSAY}#assignment-{ESSAY}")
    assert UPDATE_ALREADY_SAVED in card_for(page, ESSAY)
    assert changed.endswith(f"&saved={ESSAY}#assignment-{ESSAY}")
    assert [item.note for item in history] == ["Finished.", "Finished, all of it."]


def test_not_yet_says_what_it_means_inside_and_outside_todays_window() -> None:
    """Inside the window a Not yet stays work to plan; on a later week's page, an update on
    work outside the window says so, and gets today's day. That page says its updates are
    the latest on record."""
    with browser() as client:
        entered = client.post(
            "/parent/inbox/keep",
            data={"course": "Art", "title": "Poster", "due_date": "2026-09-10"},
        )
        assert entered.status_code == 303
        poster = next(
            item.assignment_id
            for item in state_of(client).project_state.all_assignments()
            if item.title == "Poster"
        )
        here = client.get(report(client, ESSAY, "not_yet"), headers=PAGE_HEADERS).text
        later_page = client.get(PAGE, params={"week": "2026-09-07"}, headers=PAGE_HEADERS).text
        head = hidden(card_for(later_page, poster), "expected_report_id")
        answer = client.post(
            f"/student/actions/assignments/{poster}/report",
            data={"status": "not_yet", "expected_report_id": head, "week": "2026-09-07"},
        )
        later = client.get(answer.headers["location"], headers=PAGE_HEADERS).text
        history = state_of(client).project_state.student_reports(poster)

    assert "This stays in work to plan when it is in the planning window." in card_for(here, ESSAY)
    assert "Updates shown are the latest on record." not in here
    assert answer.status_code == 303
    assert answer.headers["location"].startswith(f"{PAGE}?week=2026-09-07&saved={poster}")
    assert "Updates shown are the latest on record." in later
    assert "Saved as Not yet. This assignment is outside today's planning window." in card_for(
        later, poster
    )
    assert "Reported August 19" in card_for(later, poster)
    assert [item.reported_on for item in history] == [PLAN_DATE]


def test_a_missing_choice_or_a_long_note_returns_the_card_with_her_words_kept() -> None:
    with browser() as client:
        unchosen = client.post(
            f"/student/actions/assignments/{ESSAY}/report",
            data={"status": "", "note": "kept words", "expected_report_id": "", "week": WEEK},
        )
        too_long = client.post(
            f"/student/actions/assignments/{ESSAY}/report",
            data={"status": "done", "note": "x" * 501, "expected_report_id": "", "week": WEEK},
        )
        nothing = state_of(client).project_state.student_reports(ESSAY)

    assert unchosen.status_code == 422
    card = card_for(unchosen.text, ESSAY)
    assert CHOOSE_ONE in card
    assert "checked" not in card
    assert ">kept words</textarea>" in card
    assert '<details class="steps note-fold" open>' in card
    assert too_long.status_code == 422
    card = card_for(too_long.text, ESSAY)
    assert NOTE_TOO_LONG in card
    assert 'value="done" checked>' in card
    assert ">" + "x" * 501 + "</textarea>" in card
    assert nothing == []


def test_a_save_from_a_page_that_has_moved_on_is_refused_with_the_newer_update_shown() -> None:
    """Two devices open the same blank card. One saves Done; the other's Not yet is
    refused, 409, with the Done above its form and its own words kept, and its form now
    carries the newer update, so saving again lands. Two identical blank saves are one
    save and one already saved."""
    with browser() as client:
        first_page = client.get(PAGE, headers=PAGE_HEADERS).text
        stale_head = hidden(card_for(first_page, ESSAY), "expected_report_id")
        report(client, ESSAY, "done")
        refused = client.post(
            f"/student/actions/assignments/{ESSAY}/report",
            data={
                "status": "not_yet",
                "note": "still the last page",
                "expected_report_id": stale_head,
                "week": WEEK,
            },
        )
        card = card_for(refused.text, ESSAY)
        again = client.post(
            f"/student/actions/assignments/{ESSAY}/report",
            data={
                "status": "not_yet",
                "note": "still the last page",
                "expected_report_id": hidden(card, "expected_report_id"),
                "week": WEEK,
            },
        )
        blank_twice = client.post(
            f"/student/actions/assignments/{LOG}/report",
            data={"status": "done", "expected_report_id": "", "week": WEEK},
        )
        blank_again = client.post(
            f"/student/actions/assignments/{LOG}/report",
            data={"status": "done", "expected_report_id": "", "week": WEEK},
        )
        history = state_of(client).project_state.student_reports(ESSAY)

    assert refused.status_code == 409
    assert SAVED_ELSEWHERE in card
    assert '<span class="pill">Your update: Done</span>' in card
    assert 'value="not_yet" checked>' in card
    assert ">still the last page</textarea>" in card
    assert hidden(card, "expected_report_id") == history[0].report_id
    assert again.status_code == 303
    assert again.headers["location"].endswith(f"&saved={ESSAY}#assignment-{ESSAY}")
    assert [item.status for item in history] == ["done", "not_yet"]
    assert blank_twice.headers["location"].endswith(f"&saved={LOG}#assignment-{LOG}")
    assert blank_again.headers["location"].endswith(f"&same={LOG}#assignment-{LOG}")


def test_undo_restores_what_stood_before_and_a_stale_undo_is_refused() -> None:
    """Undoing a Done over a Not yet brings the Not yet back with its note and day, and
    offers no second undo; undoing the only update leaves the form; an undo naming an
    older update is refused with the card as it stands."""
    with browser() as client:
        report(client, ESSAY, "not_yet", "Half left.")
        page = client.get(report(client, ESSAY, "done"), headers=PAGE_HEADERS).text
        card = card_for(page, ESSAY)
        done_id = hidden(card, "report_id")
        undone = client.post(
            f"/student/actions/assignments/{ESSAY}/undo-report",
            data={"report_id": done_id, "week": WEEK},
        )
        restored_page = client.get(undone.headers["location"], headers=PAGE_HEADERS).text
        restored = card_for(restored_page, ESSAY)
        stale = client.post(
            f"/student/actions/assignments/{ESSAY}/undo-report",
            data={"report_id": done_id, "week": WEEK},
        )
        only = client.get(report(client, LOG, "done"), headers=PAGE_HEADERS).text
        to_nothing = client.post(
            f"/student/actions/assignments/{LOG}/undo-report",
            data={"report_id": hidden(card_for(only, LOG), "report_id"), "week": WEEK},
        )
        blank_again = card_for(
            client.get(to_nothing.headers["location"], headers=PAGE_HEADERS).text, LOG
        )
        statuses = statuses_for(state_of(client).project_state, [ESSAY, LOG])

    assert undone.status_code == 303
    assert undone.headers["location"] == f"{PAGE}?week={WEEK}&undone={ESSAY}#assignment-{ESSAY}"
    assert UPDATE_UNDONE in restored
    assert '<span class="pill">Your update: Not yet</span>' in restored
    assert "Reported August 19, restored August 19" in restored
    assert "You wrote: <q>Half left.</q>" in restored
    assert 'name="report_id"' not in restored
    assert ">Change</button>" in restored
    assert stale.status_code == 409
    assert SAVED_ELSEWHERE in card_for(stale.text, ESSAY)
    assert to_nothing.status_code == 303
    assert UPDATE_UNDONE in blank_again
    assert "<legend>Your update</legend>" in blank_again
    assert hidden(blank_again, "expected_report_id") == statuses[LOG].head_id
    assert (statuses[ESSAY].work_state, statuses[LOG].work_state) == ("not_yet", "unreported")


def test_the_change_button_opens_the_form_with_the_update_as_it_stands() -> None:
    with browser() as client:
        report(client, ESSAY, "not_yet", "Half left.")
        opened = client.get(PAGE, params={"week": WEEK, "change": ESSAY}, headers=PAGE_HEADERS).text
        card = card_for(opened, ESSAY)

    assert 'value="not_yet" checked>' in card
    assert ">Half left.</textarea>" in card
    assert '<span class="pill">Your update: Not yet</span>' in card
    assert "Keep it as it is</a>" in card
    assert ">Change</button>" not in card


def test_a_parent_signed_in_reads_her_update_and_cannot_make_one(tmp_path: pathlib.Path) -> None:
    """With the sign-in on, a parent's device sees her update as hers, is told to sign in as
    the student to change it, and is answered 403 when it tries; her device gets the form.
    A device with no sign-in is sent to sign in, as for any form."""
    with TestClient(
        create_app(signed_in_household(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        anonymous = client.post(
            f"/student/actions/assignments/{ESSAY}/report",
            data={"status": "done", "expected_report_id": "", "week": WEEK},
            headers=PAGE_HEADERS,
        )
        client.post("/sign-in", data={"passphrase": THEIRS})
        as_parent = client.get(PAGE, headers=PAGE_HEADERS).text
        parent_card = card_for(as_parent, ESSAY)
        refused = client.post(
            f"/student/actions/assignments/{ESSAY}/report",
            data={"status": "done", "expected_report_id": "", "week": WEEK},
            headers=PAGE_HEADERS,
        )
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": HERS})
        as_her = client.get(PAGE, headers=PAGE_HEADERS).text
        saved = client.get(report(client, ESSAY, "done", "On paper."), headers=PAGE_HEADERS).text
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        parent_after = card_for(client.get(PAGE, headers=PAGE_HEADERS).text, ESSAY)
        undo_refused = client.post(
            f"/student/actions/assignments/{ESSAY}/undo-report",
            data={"report_id": "report-x", "week": WEEK},
            headers=PAGE_HEADERS,
        )
        history = state_of(client).project_state.student_reports(ESSAY)

    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == "/sign-in"
    assert "No student update yet. Sign in as the student to update." in parent_card
    assert "<legend>Your update</legend>" not in parent_card
    assert refused.status_code == 403
    assert NOT_HERS_TO_UPDATE in refused.text
    assert "<legend>Your update</legend>" in card_for(as_her, ESSAY)
    assert '<span class="pill">Your update: Done</span>' in card_for(saved, ESSAY)
    assert '<span class="pill">Student update: Done</span>' in parent_after
    assert "She wrote: <q>On paper.</q>" in parent_after
    assert "Sign in as the student to update." in parent_after
    assert ">Change</button>" not in parent_after
    assert "undo-report" not in parent_after
    assert undo_refused.status_code == 403
    assert len(history) == 1


def test_a_plan_that_speaks_about_work_she_has_since_finished_says_so_on_both_pages() -> None:
    """The notice names the work on her page and the family page, stays whatever a parent
    decided, and a plan from before plans carried their ids gets the general notice."""
    with browser(key=True) as client:
        planned = client.post("/student/actions/plan")
        assert planned.status_code == 303
        quiet = client.get(PAGE, headers=PAGE_HEADERS).text
        report(client, ESSAY, "done")
        hers = client.get(PAGE, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text
        over_json = client.get("/student/plans/today").json()
        draft_id = client.get("/parent/approvals").json()["waiting"][0]["draft_id"]
        state = state_of(client)
        state.drafts._connection.execute("UPDATE drafts SET plan_assignment_ids=NULL")
        state.drafts._connection.commit()
        legacy = client.get(PAGE, headers=PAGE_HEADERS).text
        legacy_family = client.get("/parent", headers=PAGE_HEADERS).text

    assert "Reported done since." not in quiet
    assert PLAN_INCLUDES_DONE.format(ESSAY_TITLE) in hers
    assert SHE_REPORTS.format(ESSAY_TITLE) in family
    assert over_json["reported_done"] == PLAN_INCLUDES_DONE.format(ESSAY_TITLE)
    assert draft_id
    assert str(escape(PLAN_WINDOW_DONE)) in legacy
    assert str(escape("Some work in this plan's window is now reported Done.")) in legacy_family


def test_with_everything_in_the_window_reported_done_her_page_offers_no_plan_button() -> None:
    """The panel says nothing is left to schedule and the button is gone; the family page
    keeps its form, which the route refuses."""
    with browser(key=True) as client:
        state = state_of(client)
        for item in state.project_state.all_assignments():
            report(client, item.assignment_id, "done")
        hers = client.get(PAGE, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text
        posted = client.post("/parent/actions/plan", data={"plan_date": ""})

    assert NOTHING_TO_SCHEDULE in hers
    assert 'action="/student/actions/plan"' not in hers
    assert "Planning uses the model provider." not in hers
    assert 'action="/parent/actions/plan"' in family
    assert posted.status_code == 409
    assert NOTHING_TO_SCHEDULE in posted.text


def test_her_done_beside_the_schools_missing_is_something_to_check_on_both_pages() -> None:
    """Her page says one finished assignment has a school report to check and links to the
    card, which opens folded group; the family page lists it as worth checking together,
    with both statements and their days, and not under the school's reports. A later
    Missing from the school leaves her Done standing."""
    with browser() as client:
        told = client.post("/parent/inbox/keep", data={"text": MISSING_EMAIL})
        assert told.status_code == 303
        report(client, ESSAY, "done", "Handed in Tuesday.")
        hers = client.get(PAGE, headers=PAGE_HEADERS).text
        link = re.search(r'<a href="([^"]+)">Canal Era comparison essay</a>', hers)
        assert link is not None
        followed = client.get(link.group(1).replace("&amp;", "&"), headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text
        told_again = client.post("/parent/inbox/keep", data={"text": MISSING_EMAIL})
        still = statuses_for(state_of(client).project_state, [ESSAY])[ESSAY]

    assert "<strong>1 finished assignment\n      has a school report to check:</strong>" in hers
    assert f"show={ESSAY}#assignment-{ESSAY}" in link.group(1)
    assert '<details class="steps reported-done" open>' in followed
    assert "<strong>The school reports this missing.</strong>" in card_for(followed, ESSAY)
    checking, _, rest = family.partition("<h3>School reports</h3>")
    assert "<h3>Worth checking together</h3>" in checking
    assert "She reported it done on August 19. She wrote: <q>Handed in Tuesday.</q>" in checking
    assert "The school reports it missing. From the school email, pasted" in checking
    assert "<strong>Check the school record.</strong>" in checking
    assert "Blossom is not scheduling more homework for this assignment." in checking
    assert "<summary>Recent updates (1)</summary>" in checking
    assert ESSAY_TITLE not in rest
    assert told_again.status_code == 303
    assert (still.work_state, still.check_the_school_record) == ("done", True)


def test_the_assigned_later_list_takes_her_update_the_same_way() -> None:
    with browser() as client:
        before = client.get(PAGE, headers=PAGE_HEADERS).text
        location = report(client, LOG, "done")
        after = client.get(location, headers=PAGE_HEADERS).text

    _, _, later_before = before.partition("Assigned this week, due later")
    assert "<legend>Your update</legend>" in card_for(later_before, LOG)
    _, _, later_after = after.partition("Assigned this week, due later")
    assert "<summary>Reported done (1)</summary>" in later_after
    assert '<span class="pill">Your update: Done</span>' in card_for(later_after, LOG)


def test_reading_the_statuses_takes_the_same_few_reads_whatever_the_number_of_rows(
    tmp_path: pathlib.Path,
) -> None:
    def rows(count: int) -> list[Assignment]:
        return [
            Assignment(
                assignment_id=f"assignment-{n}",
                course="Math",
                title=f"Sheet {n}",
                due_date=date(2026, 8, 21),
                dependencies=[],
                reported_submission_status="not_started",
                kind=AssignmentKind.HOMEWORK,
            )
            for n in range(count)
        ]

    def reads(count: int) -> int:
        store = ProjectStateStore.open(tmp_path / f"{count}.sqlite3", fixture_clock())
        try:
            store.put_on_record(rows(count), {})
            now = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)
            for n in range(0, count, 2):
                store.report_status(
                    f"assignment-{n}", "done", None, expected_head=None, now=now, today=PLAN_DATE
                )
            statements: list[str] = []
            store._connection.set_trace_callback(statements.append)
            statuses_for(store, [f"assignment-{n}" for n in range(count)])
            store._connection.set_trace_callback(None)
        finally:
            store.close()
        return len([s for s in statements if not s.startswith(("BEGIN", "COMMIT"))])

    few, many = reads(2), reads(40)

    assert few == many
    assert many <= 4
    assert isinstance(sqlite3.connect(":memory:"), sqlite3.Connection)


class ReportsWhileAsked:
    """A planner whose answer comes only after her Done has landed, as a report does that
    arrives while the model call is pending. It takes the decision lock for the save, as
    her page's route does, so a run that held the lock through the call would never
    answer."""

    def __init__(self, state: ApplicationState) -> None:
        self.state = state
        self.lock_was_free = False
        self.saved: object = None

    async def __call__(self, messages: Sequence[BaseMessage]) -> ModelAnswer[DailyPlan]:
        self.lock_was_free = not self.state.decision_lock.locked()
        async with self.state.decision_lock:
            self.saved = self.state.project_state.report_status(
                ESSAY,
                "done",
                None,
                expected_head=None,
                now=self.state.clock.now(),
                today=self.state.clock.today(),
            )
        return ok(fixture_week_plan())


def test_a_done_saved_while_the_planner_is_asked_leaves_the_plan_stale_and_named() -> None:
    """Her Done lands while the model call is pending. The save is not kept waiting for the
    model; the run works from what it read, so the plan passes its checks and its draft
    keeps the fingerprint and the ids of that reading; and the moment it is published it
    reads as stale on both pages, its notice names the essay, and approving is refused."""
    asked: list[ReportsWhileAsked] = []

    def override(
        state: Annotated[ApplicationState, Depends(get_application_state)],
    ) -> PlanGraphs:
        if not asked:
            asked.append(ReportsWhileAsked(state))
        planner = asked[0]
        return PlanGraphs(
            build=lambda: plan_graph_for(state, planner=planner, critic=Scripted(ok(accepting()))),
            may_start=True,
        )

    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = override  # type: ignore[attr-defined]
        state = state_of(client)
        as_read = planning_digest(read_week(state.project_state, state.project_state, PLAN_DATE))
        planned = client.post("/student/actions/plan")
        record = state.drafts.latest_for(PLAN_DATE)
        as_it_stands = planning_digest(
            read_week(state.project_state, state.project_state, PLAN_DATE)
        )
        hers = client.get(PAGE, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text
        assert record is not None
        refused = client.post(
            f"/parent/actions/decide/{record.draft_id}", data={"decision": "approve"}
        )
        after = state.drafts.get(record.draft_id)

    assert len(asked) == 1
    assert asked[0].lock_was_free
    assert isinstance(asked[0].saved, Saved)
    assert planned.status_code == 303
    assert record.waiting
    assert record.inputs_digest == as_read
    assert as_it_stands != as_read
    assert record.plan_assignment_ids is not None
    assert ESSAY in record.plan_assignment_ids
    assert ASSIGNMENTS_CHANGED in hers
    assert PLAN_INCLUDES_DONE.format(ESSAY_TITLE) in hers
    assert THEIR_ASSIGNMENTS_CHANGED in family
    assert SHE_REPORTS.format(ESSAY_TITLE) in family
    assert 'value="approve"' not in family
    assert refused.status_code == 409
    assert after is not None
    assert after.waiting
