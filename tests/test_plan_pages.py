"""The saved plan on both pages as her work changes: read by its rows, marked only where
it is today's working plan, and never written by being read.

The fixture week through the app, a pinned clock, a scripted plan with the
essay in two sittings, a namesake put off, and forms alone. No model is asked
beyond the scripted answers, and nothing here needs a script in the browser.
"""

import pathlib
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.noticing import planning_digest, read_week
from blossom.plan_reading import anchor_for
from blossom.reconciliation import SourceChannel
from blossom.routes.parent import ASSIGNMENTS_CHANGED as THEIR_ASSIGNMENTS_CHANGED
from blossom.routes.runs import NOTHING_TO_SCHEDULE, plan_graphs
from blossom.stores.drafts import DraftRecord
from tests.support import (
    ESSAY_ID,
    ESSAY_TITLE,
    HER_PAGE,
    PAGE_HEADERS,
    PLAN_DATE,
    SAME_ORIGIN,
    accepting,
    browser,
    fixture_settings,
    fixture_week_plan,
    report,
    school_said,
    scripted_graphs,
    state_of,
    walkthrough,
    walkthrough_plan,
)

YOU_SKIP = "You report this as Done. You can skip this block."
SHE_SKIPS = "She reports this as Done. She can skip this block."
YOU_OUT = "You now report this as Done. It is out of work to plan."
SHE_OUT = "She now reports this as Done. It is out of work to plan."
YOURS_BESIDE = "Your updates are shown beside the saved plan. Its times have not changed."
HERS_BESIDE = "Her updates are shown beside the saved plan. Its times have not changed."
HISTORY = "This is the saved plan. Assignment links open the current record."
EARLIER_FORMAT = "This plan uses the earlier text format."
UNAVAILABLE = "The saved text is shown because this plan's structured view is unavailable."
WHEN_PLANNED = '<span class="plan-why-label">Reason when planned:</span>'


def planned(client: TestClient) -> DraftRecord:
    """Make today's plan from her page and return its record."""
    made = client.post("/student/actions/plan")
    assert made.status_code == 303, made.text[:300]
    record = state_of(client).drafts.latest_for(PLAN_DATE)
    assert record is not None
    return record


def plan_on(page: str, record: DraftRecord) -> str:
    """One plan's container on a page, whole: from its anchor to the end of its original
    text fold, or to the end of its text reading."""
    start = page.index(f'id="{anchor_for(record.draft_id)}"')
    ends = [
        found
        for found in (page.find(mark, start) for mark in ("</pre>", "</section>", "</article>"))
        if found >= 0
    ]
    return page[start : min(ends)] if ends else page[start:]


def rows(plan: str) -> list[str]:
    """Each saved row of a plan, a block or a deferral, as its own piece of the page."""
    return re.findall(r'<li class="plan-(?:block|deferral)[^"]*" id="[^"]+">.*?</li>', plan, re.S)


def saved_facts(client: TestClient, record: DraftRecord) -> tuple[object, ...]:
    """Everything a reading or an update must leave as it was: the draft whole, the week's
    fingerprint aside, and the count of drafts and runs."""
    state = state_of(client)
    again = state.drafts.get(record.draft_id)
    assert again is not None
    return (
        again,
        len(state.drafts.waiting()) + len(state.drafts.decided()),
        len(state.drafts.runs_without_a_draft()),
    )


# ------------------------------------------------------------- her updates beside the rows


def test_both_essay_blocks_are_marked_and_nothing_else_once_she_reports_the_essay_done() -> None:
    """The example the plan was built for. She follows the essay's link from the plan, saves
    Done on its details, and goes back: both essay blocks say she can skip them, with the
    day of the report and their reasons labeled as the reasons when planned; the science
    block, the namesake put off, and every other row read as they did; the times have not
    moved; the notice names the essay by its saved title with a link; and the family page
    says the same in its own words. Nothing about the draft was written."""
    with browser(key=True) as client:
        namesake = walkthrough(client)
        record = planned(client)
        before = client.get(f"{HER_PAGE}?show_plan=1", headers=PAGE_HEADERS).text
        facts = saved_facts(client, record)
        link = re.search(
            rf'<a href="(/student/assignments/{ESSAY_ID}\?return_to=today)"',
            plan_on(before, record),
        )
        assert link is not None
        details = client.get(link.group(1), headers=PAGE_HEADERS).text
        back = re.search(
            r'<p class="return"><a href="([^"]+)">Back to today&#39;s plan</a>', details
        )
        assert back is not None
        report(client, ESSAY_ID, "done", "Both parts.")
        hers = client.get(back.group(1).replace("&amp;", "&"), headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text
        after = saved_facts(client, record)

    unmarked = rows(plan_on(before, record))
    marked = rows(plan_on(hers, record))
    theirs = rows(plan_on(family, record))
    assert "Reported done" not in plan_on(before, record)
    assert YOURS_BESIDE not in before
    assert len(unmarked) == len(marked) == len(theirs) == 9
    essay_rows = [index for index, row in enumerate(marked) if f"/{ESSAY_ID}?" in row]
    assert essay_rows == [0, 2]
    for index in essay_rows:
        assert 'class="plan-block reported-done"' in marked[index]
        assert '<span class="pill">Reported done</span> Reported August 19, 2026' in marked[index]
        assert YOU_SKIP in marked[index]
        assert WHEN_PLANNED in marked[index]
        assert SHE_SKIPS in theirs[index]
        assert f'href="/student/assignments/{ESSAY_ID}?return_to=today"' in marked[index]
    assert "<strong>4:30 PM to 5:00 PM</strong>" in marked[0]
    assert "<strong>6:00 PM to 6:30 PM</strong>" in marked[2]
    assert "the outline first, while she is fresh" in marked[0]
    for index, row in enumerate(marked):
        if index not in essay_rows:
            assert row == unmarked[index]
            assert "Reported done" not in row
    namesake_row = next(row for row in marked if f"/{namesake}?" in row)
    assert ESSAY_TITLE in namesake_row
    assert "Reported done" not in namesake_row
    assert YOURS_BESIDE in hers
    assert HERS_BESIDE in plan_on(family, record)
    assert '<details class="plan" open>' in hers
    assert (
        f'In it: <a href="/student/assignments/{ESSAY_ID}?return_to=today" '
        f'aria-label="{ESSAY_TITLE}, World History, due August 21, 2026">{ESSAY_TITLE}</a> '
        "(World History, due August 21, 2026)."
    ) in hers
    assert f'<a href="/student/due-this-week?show_plan=1#{anchor_for(record.draft_id)}">' in hers
    assert "Both parts." not in plan_on(hers, record)
    assert after == facts


def test_a_deferral_she_reports_done_says_it_is_out_of_work_to_plan_and_never_to_skip() -> None:
    with browser(key=True) as client:
        namesake = walkthrough(client)
        record = planned(client)
        report(client, namesake, "done")
        hers = rows(plan_on(client.get(HER_PAGE, headers=PAGE_HEADERS).text, record))
        theirs = rows(plan_on(client.get("/parent", headers=PAGE_HEADERS).text, record))

    mine = next(row for row in hers if f"/{namesake}?" in row)
    family = next(row for row in theirs if f"/{namesake}?" in row)
    assert 'class="plan-deferral reported-done"' in mine
    assert YOU_OUT in mine
    assert YOU_SKIP not in mine
    assert WHEN_PLANNED in mine
    assert "the other essay comes first" in mine
    assert SHE_OUT in family
    assert sum("Reported done" in row for row in hers) == 1


def test_the_marks_follow_her_update_as_it_stands_and_nothing_else() -> None:
    """A change to her note keeps the mark. A Not yet removes it and leaves the rows where
    they were; taking the Not yet back restores the mark, dated by the report restored and
    the day it was put back. A parent's check and a new school Missing change no mark."""
    with browser(key=True) as client:
        walkthrough(client)
        record = planned(client)
        store = state_of(client).project_state

        def essay_rows() -> list[str]:
            page = client.get(HER_PAGE, headers=PAGE_HEADERS).text
            return [row for row in rows(plan_on(page, record)) if f"/{ESSAY_ID}?" in row]

        report(client, ESSAY_ID, "done")
        done = essay_rows()
        report(client, ESSAY_ID, "done", "With a note now.")
        noted = essay_rows()
        store.record_status_reports(
            ESSAY_ID, [school_said("missing", SourceChannel.EMAIL, PLAN_DATE)]
        )
        family = client.get("/parent", headers=PAGE_HEADERS).text
        basis = re.search(r'name="basis" value="([^"]+)"', family)
        assert basis is not None
        checked = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/mark",
            data={"basis": basis.group(1), "expected_check_id": "", "note": "Seen."},
        )
        assert checked.status_code == 303
        with_a_check = essay_rows()
        report(client, ESSAY_ID, "not_yet")
        not_yet = essay_rows()
        card = client.get(f"{HER_PAGE}?show={ESSAY_ID}", headers=PAGE_HEADERS).text
        head = re.search(r'name="report_id" value="([^"]+)"', card)
        assert head is not None
        undone = client.post(
            f"/student/actions/assignments/{ESSAY_ID}/undo-report",
            data={"report_id": head.group(1), "week": "2026-08-17"},
        )
        assert undone.status_code == 303
        restored = essay_rows()

    assert all("Reported done" in row for row in done)
    assert noted == done
    assert with_a_check == done
    assert all("Reported done" not in row and WHEN_PLANNED not in row for row in not_yet)
    assert [re.sub(r"\s+", " ", row) for row in not_yet] != []
    assert all("<strong>" in row and "plan-why" in row for row in not_yet)
    assert all(
        "Reported August 19, 2026, restored August 19, 2026" in row and YOU_SKIP in row
        for row in restored
    )


def test_work_that_was_not_in_the_plan_gets_no_row_when_it_comes_back() -> None:
    """The essay was reported done before the plan was made, so the plan says nothing about
    it. She changes it to Not yet: the plan gets no new row, and the pages say the work on
    record differs from what the plan used, as they always did."""
    with browser(key=True) as client:
        report(client, ESSAY_ID, "done")
        whole = walkthrough_without_the_essay(client)
        record = planned(client)
        report(client, ESSAY_ID, "not_yet")
        hers = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text

    assert whole
    assert f"/{ESSAY_ID}?" not in plan_on(hers, record)
    assert THEIR_ASSIGNMENTS_CHANGED in family
    assert 'value="approve"' not in family


def walkthrough_without_the_essay(client: TestClient) -> bool:
    whole = fixture_week_plan()
    without = whole.model_copy(
        update={"blocks": [block for block in whole.blocks if block.assignment_id != ESSAY_ID]}
    )
    client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
        lambda: [without], lambda: [accepting()]
    )
    return True


# ------------------------------------------------------------- current, and history


@pytest.mark.parametrize("decision", ["waiting", "approve", "refuse"])
def test_todays_latest_plan_is_marked_on_both_pages_whatever_was_decided(decision: str) -> None:
    """Waiting, the plan is in the review queue. Decided, it is under a heading of its own
    after the queue, once, and not among the earlier plans. Either way both pages show her
    update beside its rows, and a stale plan still offers no approval."""
    with browser(key=True) as client:
        walkthrough(client)
        record = planned(client)
        if decision != "waiting":
            decided = client.post(
                f"/parent/actions/decide/{record.draft_id}",
                data={"decision": decision, "reason": "Start with the outline."},
            )
            assert decided.status_code == 303
        report(client, ESSAY_ID, "done")
        hers = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text

    assert plan_on(hers, record).count(YOU_SKIP) == 2
    assert family.count(f'id="{anchor_for(record.draft_id)}"') == 1
    assert plan_on(family, record).count(SHE_SKIPS) == 2
    assert HISTORY not in plan_on(family, record)
    queue, _, rest = family.partition("<h2>Today's reviewed plan</h2>")
    if decision == "waiting":
        assert rest == ""
        assert anchor_for(record.draft_id) in queue
        assert 'value="approve"' not in family
        assert THEIR_ASSIGNMENTS_CHANGED in family
    else:
        earlier = rest[rest.index("<summary>Earlier plans</summary>") :]
        assert anchor_for(record.draft_id) not in queue
        assert anchor_for(record.draft_id) in rest
        assert anchor_for(record.draft_id) not in earlier
        assert "Reason: Start with the outline.." in rest
        assert "No earlier plans yet." in earlier


def test_an_earlier_plan_for_today_a_future_plan_and_the_original_text_carry_no_marks() -> None:
    """Two plans for today: the first is history the moment the second is published, whatever
    was said about it, and only the second is marked. A plan waiting for tomorrow is read by
    its rows and carries no mark. The text as composed never does."""
    with browser(key=True) as client:
        namesake = walkthrough(client)
        first = planned(client)
        approved = client.post(
            f"/parent/actions/decide/{first.draft_id}", data={"decision": "approve"}
        )
        assert approved.status_code == 303
        second = planned(client)
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [
                walkthrough_plan(namesake).model_copy(update={"plan_date": date(2026, 8, 20)})
            ],
            lambda: [accepting()],
        )
        tomorrow = client.post("/parent/plans", json={"plan_date": "2026-08-20"})
        report(client, ESSAY_ID, "done")
        hers = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text
        store = state_of(client).drafts
        later = [item for item in store.waiting() if item.plan_date == date(2026, 8, 20)]

    assert first.draft_id != second.draft_id
    assert plan_on(hers, second).count(YOU_SKIP) == 2
    assert anchor_for(first.draft_id) not in hers
    assert plan_on(family, second).count(SHE_SKIPS) == 2
    old = plan_on(family, first)
    assert "Reported done" not in old
    assert HISTORY in old
    assert f"/student/assignments/{ESSAY_ID}?return_to=family" in old
    assert family.index("<summary>Earlier plans</summary>") < family.index(
        f'id="{anchor_for(first.draft_id)}"'
    )
    assert "Today's reviewed plan" not in family
    original = family[family.index('<pre class="plan-original-text">') :]
    assert "Reported done" not in original[: original.index("</pre>")]
    assert tomorrow.status_code == 201
    assert len(later) == 1
    for plan in later:
        assert "Reported done" not in plan_on(family, plan)
        assert HISTORY in plan_on(family, plan)


def test_a_new_household_day_makes_yesterdays_plan_history(tmp_path: pathlib.Path) -> None:
    """The same file opened on the next household day: yesterday's plan is on no page of hers,
    and the family page reads it as history with no marks, although her Done still stands."""
    paths = {
        "BLOSSOM_DATABASE_PATH": str(tmp_path / "blossom.sqlite3"),
        "BLOSSOM_CHECKPOINT_PATH": str(tmp_path / "checkpoints.sqlite3"),
        "BLOSSOM_TRACE_PATH": str(tmp_path / "traces.sqlite3"),
    }
    with browser(key=True, **paths) as client:
        walkthrough(client)
        record = planned(client)
        report(client, ESSAY_ID, "done")
        today = client.get("/parent", headers=PAGE_HEADERS).text
    tomorrow = create_app(fixture_settings(BLOSSOM_TODAY="2026-08-20", **paths))
    with TestClient(tomorrow, follow_redirects=False, headers=SAME_ORIGIN) as client:
        hers = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text

    assert plan_on(today, record).count(SHE_SKIPS) == 2
    assert anchor_for(record.draft_id) not in hers
    assert "Reported done" not in plan_on(family, record)
    assert HISTORY in plan_on(family, record)


# ------------------------------------------------------------- plans without a usable snapshot


@pytest.mark.parametrize(
    ("column", "value", "sentence"),
    [
        pytest.param("plan_snapshot", None, EARLIER_FORMAT, id="from-before-snapshots"),
        pytest.param("plan_snapshot", '{"version": 2}', UNAVAILABLE, id="another-version"),
        pytest.param("plan_snapshot", "{torn", UNAVAILABLE, id="not-json"),
        pytest.param(
            "plan_assignment_ids", '["assignment-canal-essay"]', UNAVAILABLE, id="other-ids"
        ),
    ],
)
def test_a_plan_without_a_usable_snapshot_reads_as_its_saved_text_and_keeps_its_notice(
    column: str, value: str | None, sentence: str
) -> None:
    """The saved text whole, a sentence that says why, no links, no marks beside rows, and the
    notice the draft's own id list still supports. A plan beside it that can be read by its
    rows still is, and the page answers 200."""
    with browser(key=True) as client:
        walkthrough(client)
        broken = planned(client)
        store = state_of(client).drafts
        store._connection.execute(
            f"UPDATE drafts SET {column}=? WHERE draft_id=?",  # noqa: S608
            (value, broken.draft_id),
        )
        store._connection.commit()
        report(client, ESSAY_ID, "done")
        hers = client.get(HER_PAGE, headers=PAGE_HEADERS)
        family = client.get("/parent", headers=PAGE_HEADERS)
        over_json = client.get("/student/plans/today").json()

    assert (hers.status_code, family.status_code) == (200, 200)
    text = plan_on(hers.text, broken)
    assert sentence in text
    assert "set aside for" in text
    assert "Reported done" not in text
    assert "/student/assignments/" not in text
    assert "Original saved text" not in text
    assert "This plan includes work you now report as Done." in hers.text
    assert sentence in plan_on(family.text, broken)
    assert set(over_json) >= {"draft_id", "body", "reported_done", "reported_done_work"}
    assert "plan_snapshot" not in over_json


def test_a_plan_paused_from_before_snapshots_is_decided_as_it_was_and_stays_text() -> None:
    """A run paused at the gate whose draft has no snapshot, as one made before plans carried
    them: a parent's decision resumes and finishes the run as it always did, no model is
    asked again, and the draft keeps its text, its decision, and no snapshot."""
    with browser(key=True) as client:
        walkthrough(client)
        record = planned(client)
        drafts = state_of(client).drafts
        drafts._connection.execute(
            "UPDATE drafts SET plan_snapshot=NULL WHERE draft_id=?", (record.draft_id,)
        )
        drafts._connection.commit()
        decided = client.post(
            f"/parent/actions/decide/{record.draft_id}", data={"decision": "approve"}
        )
        after = drafts.get(record.draft_id)
        family = client.get("/parent", headers=PAGE_HEADERS).text
        hers = client.get(f"{HER_PAGE}?show_plan=1", headers=PAGE_HEADERS).text

    assert decided.status_code == 303
    assert after is not None
    assert (after.decision, after.plan_snapshot, after.body) == ("approved", None, record.body)
    assert EARLIER_FORMAT in plan_on(family, record)
    assert EARLIER_FORMAT in plan_on(hers, record)
    assert "set aside for" in plan_on(hers, record)


# ------------------------------------------------------------- nothing left; reading writes nothing


def test_every_saved_block_done_is_not_nothing_to_schedule_and_all_done_offers_no_plan() -> None:
    """With the two blocked assignments done and other work still active, her page still
    offers a plan. With everything done it keeps the saved plan and its marks, says there is
    nothing to schedule, and offers her no plan; the family's form stays."""
    with browser(key=True) as client:
        walkthrough(client)
        record = planned(client)
        report(client, ESSAY_ID, "done")
        report(client, "assignment-science-fair-proposal", "done")
        some = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        for item in state_of(client).project_state.all_assignments():
            report(client, item.assignment_id, "done")
        everything = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS).text

    assert plan_on(some, record).count(YOU_SKIP) == 3
    assert NOTHING_TO_SCHEDULE not in some
    assert 'action="/student/actions/plan"' in some
    assert NOTHING_TO_SCHEDULE in everything
    assert 'action="/student/actions/plan"' not in everything
    assert "Plan again" not in everything
    assert plan_on(everything, record).count(YOU_SKIP) == 3
    assert 'action="/parent/actions/plan"' in family


def test_reading_plans_and_details_writes_nothing_and_reads_her_updates_in_batches() -> None:
    """Both pages and an assignment's details are opened again and again: the draft, its
    decision, her events, the checks, and the week's fingerprint are as they were, and no
    model was asked. Her updates are read in a number of statements that does not grow with
    the rows of a plan or with the plans shown as history."""
    with browser(key=True) as client:
        walkthrough(client)
        first = planned(client)
        second = planned(client)
        report(client, ESSAY_ID, "done")
        state = state_of(client)
        store = state.project_state
        before = (
            state.drafts.get(first.draft_id),
            state.drafts.get(second.draft_id),
            store.student_reports(ESSAY_ID),
            store.family_checks(ESSAY_ID),
            planning_digest(read_week(store, store, PLAN_DATE)),
        )
        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)
        client.get("/parent", headers=PAGE_HEADERS)
        store._connection.set_trace_callback(None)
        family_reads = [text for text in statements if "student_reports" in text]
        for _ in range(3):
            client.get(HER_PAGE, headers=PAGE_HEADERS)
            client.get("/parent", headers=PAGE_HEADERS)
            client.get(f"/student/assignments/{ESSAY_ID}?return_to=today", headers=PAGE_HEADERS)
        after = (
            state.drafts.get(first.draft_id),
            state.drafts.get(second.draft_id),
            store.student_reports(ESSAY_ID),
            store.family_checks(ESSAY_ID),
            planning_digest(read_week(store, store, PLAN_DATE)),
        )

    assert after == before
    assert first.draft_id != second.draft_id
    assert 0 < len(family_reads) <= 6
    assert all("json_each" in text or "ORDER BY rowid" in text for text in family_reads)
