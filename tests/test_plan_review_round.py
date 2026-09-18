"""Readings that must hold together: the family page's drafts read once, a snapshot held to
its whole shape, one household day for a page, and a way back that survives a failure.

The fixture week through the app with scripted plans, a second connection to
the same file where two writers are needed, and a clock that moves when a
page reads it twice.
"""

import dataclasses
import json
import pathlib
import re
import threading
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from markupsafe import escape
from pydantic import ValidationError

from blossom.dependencies import STATE_ATTRIBUTE
from blossom.drafts import Draft, DraftStatus
from blossom.plan_reading import anchor_for
from blossom.plan_snapshot import PlanSnapshot, read_snapshot
from blossom.routes import student as student_routes
from blossom.routes.student import BAD_RETURN, NOT_SAVED, NOT_UNDONE
from blossom.stores.drafts import DraftsStore
from tests.support import (
    ESSAY_ID,
    FIXTURE_TIMEZONE,
    FIXTURE_WEEK,
    HER_PAGE,
    PAGE_HEADERS,
    PLAN_DATE,
    browser,
    composed_plan,
    fixture_clock,
    plan_on,
    planned,
    report,
    state_of,
    walkthrough,
)

DETAILS = f"/student/assignments/{ESSAY_ID}"
ACTIONS = f"/student/actions/assignments/{ESSAY_ID}"
UNAVAILABLE = "The saved text is shown because this plan's structured view is unavailable."


# ------------------------------------------------------------- the family page's drafts, read once


@pytest.mark.parametrize("lands", ["a publication", "a decision"])
def test_the_family_page_shows_one_reading_of_its_drafts_whatever_lands_meanwhile(
    monkeypatch: pytest.MonkeyPatch, lands: str
) -> None:
    """Right after the page's first read of the drafts, another plan is published, or the
    waiting one is decided. The page answers 200 with a reading that agrees with itself:
    each draft once, the waiting plan still in the queue as it was read."""
    with browser(key=True) as client:
        walkthrough(client)
        first = planned(client)
        drafts = state_of(client).drafts
        later = Draft(
            draft_id="draft:review-later",
            body="Plan for Thursday, August 20, 2026",
            created_at=datetime(2026, 8, 19, 23, 0, tzinfo=UTC),
        )
        landed: list[str] = []

        def land() -> None:
            if landed:
                return
            landed.append(lands)
            if lands == "a publication":
                drafts.record_waiting(
                    later, thread_id="t-later", plan_date=date(2026, 8, 20), outcome="accepted"
                )
                drafts.publish(later.draft_id)
            else:
                drafts.record_decision(
                    first.draft_id,
                    status=DraftStatus.APPROVED_FOR_MANUAL_SEND,
                    decision="approved",
                    reason=None,
                )

        for name in ("waiting", "decided", "latest_for", "review_snapshot"):
            read = getattr(drafts, name, None)
            if read is None:
                continue

            def then_lands(*given: object, _read: object = read, **named: object) -> object:
                found = _read(*given, **named)  # type: ignore[operator]
                land()
                return found

            monkeypatch.setattr(drafts, name, then_lands)
        page = client.get("/parent", headers=PAGE_HEADERS)
        monkeypatch.undo()
        after = client.get("/parent", headers=PAGE_HEADERS)

    assert landed == [lands]
    assert page.status_code == 200
    assert page.text.count(f'id="{anchor_for(first.draft_id)}"') == 1
    assert "draft:review-later" not in page.text
    queue = page.text[page.text.index("<h2>Waiting for your review</h2>") :]
    assert anchor_for(first.draft_id) in queue[: queue.index("</section>")]
    assert after.status_code == 200
    assert after.text.count(f'id="{anchor_for(first.draft_id)}"') == 1


def test_the_family_page_holds_while_a_second_connection_publishes_and_decides(
    tmp_path: pathlib.Path,
) -> None:
    """Two connections to one file, let go together: one renders the family page again and
    again while the other publishes plans and decides them. Every page answers 200 and
    shows each draft at most once."""
    paths = {
        "BLOSSOM_DATABASE_PATH": str(tmp_path / "blossom.sqlite3"),
        "BLOSSOM_CHECKPOINT_PATH": str(tmp_path / "checkpoints.sqlite3"),
        "BLOSSOM_TRACE_PATH": str(tmp_path / "traces.sqlite3"),
    }
    together = threading.Barrier(2)
    pages: list[tuple[int, str]] = []
    ids = [f"draft:elsewhere-{n}" for n in range(12)]

    def elsewhere() -> None:
        other = DraftsStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
        try:
            together.wait(10)
            for n, draft_id in enumerate(ids):
                other.record_waiting(
                    Draft(
                        draft_id=draft_id,
                        body=f"Plan {n}",
                        created_at=datetime(2026, 8, 19, 22, n, tzinfo=UTC),
                    ),
                    thread_id=f"t-{n}",
                    plan_date=date(2026, 8, 20 + n % 3),
                    outcome="accepted",
                )
                other.publish(draft_id)
                if n % 2:
                    other.record_decision(
                        draft_id,
                        status=DraftStatus.APPROVED_FOR_MANUAL_SEND,
                        decision="approved",
                        reason=None,
                    )
        finally:
            other.close()

    with browser(key=True, **paths) as client:
        walkthrough(client)
        planned(client)
        writer = threading.Thread(target=elsewhere)
        writer.start()
        together.wait(10)
        while writer.is_alive() or len(pages) < 5:
            answer = client.get("/parent", headers=PAGE_HEADERS)
            pages.append((answer.status_code, answer.text))
            if len(pages) > 200:
                break
        writer.join(20)
        last = client.get("/parent", headers=PAGE_HEADERS).text

    assert not writer.is_alive()
    assert all(code == 200 for code, _ in pages)
    for _, text in pages:
        assert all(text.count(f'id="{anchor_for(draft_id)}"') <= 1 for draft_id in ids)
    assert all(last.count(f'id="{anchor_for(draft_id)}"') == 1 for draft_id in ids)


# ------------------------------------------------------------- a snapshot held to its whole shape


def test_version_one_is_every_key_it_writes_and_wall_clock_times() -> None:
    """An explicit null due date, empty lists, and a null review are version 1. A key left
    out is not: a due date, the introduction, the clarifications, the review, or either
    list inside a review. Block times are wall times in the household's zone, so a block
    that carries an offset, one or all of them, is not version 1 either."""
    made = composed_plan()
    whole = made.snapshot.model_dump(mode="json")
    names = list(whole["assignments"])

    def without(path: list[str]) -> dict[str, object]:
        copy = json.loads(json.dumps(whole))
        inside = copy
        for key in path[:-1]:
            inside = inside[key]
        del inside[path[-1]]
        return dict(copy)

    def with_times(which: list[int]) -> dict[str, object]:
        copy = json.loads(json.dumps(whole))
        for index in which:
            block = copy["plan"]["blocks"][index]
            block["starts_at"] += "+00:00"
            block["ends_at"] += "+00:00"
        return dict(copy)

    explicit = json.loads(json.dumps(whole))
    explicit["assignments"][names[0]]["due_date"] = None
    explicit["intro"], explicit["clarifications"], explicit["review"] = [], [], None

    assert PlanSnapshot.model_validate(explicit).review is None
    for path in (
        ["assignments", names[0], "due_date"],
        ["intro"],
        ["clarifications"],
        ["review"],
        ["review", "intro"],
        ["review", "findings"],
    ):
        with pytest.raises(ValidationError):
            PlanSnapshot.model_validate(without(path))
    for which in ([0], [0, 1, 2]):
        with pytest.raises(ValidationError, match="wall times"):
            PlanSnapshot.model_validate(with_times(which))
        reading = read_snapshot(
            "draft:x",
            json.dumps(with_times(which)),
            plan_date=PLAN_DATE,
            plan_assignment_ids=made.snapshot.assignment_ids,
        )
        assert (reading.snapshot, reading.unavailable) == (None, True)


@pytest.mark.parametrize("fault", ["mixed times", "every time aware", "a due date left out"])
def test_a_snapshot_that_is_not_whole_falls_back_to_the_saved_text_on_both_pages(
    fault: str,
) -> None:
    """Today's plan has a snapshot that is not version 1, beside an earlier plan that is.
    Both pages answer 200 with the saved text whole and the sentence that says why, no mark
    beside any row, the earlier plan still read by its rows, and nothing written."""
    with browser(key=True) as client:
        walkthrough(client)
        good = planned(client)
        bad = planned(client)
        drafts = state_of(client).drafts
        assert bad.plan_snapshot is not None
        saved = json.loads(bad.plan_snapshot)
        if fault == "a due date left out":
            del saved["assignments"][ESSAY_ID]["due_date"]
        else:
            for index, block in enumerate(saved["plan"]["blocks"]):
                if fault == "every time aware" or index == 0:
                    block["starts_at"] += "+00:00"
                    block["ends_at"] += "+00:00"
        drafts._connection.execute(
            "UPDATE drafts SET plan_snapshot=? WHERE draft_id=?", (json.dumps(saved), bad.draft_id)
        )
        drafts._connection.commit()
        report(client, ESSAY_ID, "done")
        before = (drafts.get(good.draft_id), drafts.get(bad.draft_id))
        hers = client.get(HER_PAGE, headers=PAGE_HEADERS)
        family = client.get("/parent", headers=PAGE_HEADERS)
        after = (drafts.get(good.draft_id), drafts.get(bad.draft_id))

    assert (hers.status_code, family.status_code) == (200, 200)
    for page in (hers.text, family.text):
        text = plan_on(page, bad)
        assert UNAVAILABLE in text
        assert "set aside for" in text
        assert "Reported done" not in text
    assert (
        "No due date on record" not in hers.text[hers.text.index(anchor_for(bad.draft_id)) :][:4000]
    )
    earlier = family.text[family.text.index(f'id="{anchor_for(good.draft_id)}"') :]
    assert '<ol class="plan-rows">' in earlier[: earlier.index("</pre>")]
    assert after == before


# ------------------------------------------------------------- one household day for a page


class MovingClock:
    """A clock whose household day moves on after it is first read, as a page rendered
    across midnight would find it."""

    def __init__(self) -> None:
        self.reads = 0
        self._zone = ZoneInfo(FIXTURE_TIMEZONE)

    @property
    def zone(self) -> ZoneInfo:
        return self._zone

    def now(self) -> datetime:
        return datetime(2026, 8, 20, 3, 59, tzinfo=UTC)

    def today(self) -> date:
        self.reads += 1
        return PLAN_DATE if self.reads == 1 else date(2026, 8, 20)


@pytest.mark.parametrize("path", [HER_PAGE, "/parent", f"{DETAILS}?return_to=today"])
def test_a_page_reads_the_household_day_once_and_uses_it_throughout(path: str) -> None:
    """The day moves on the moment a page has read it. The page still shows one day: today's
    plan with her update beside its rows, under a Today that is the same day, and the next
    page is the next day's, with no plan for it."""
    with browser(key=True) as client:
        walkthrough(client)
        record = planned(client)
        report(client, ESSAY_ID, "done")
        state = state_of(client)
        moving = MovingClock()
        setattr(client.app.state, STATE_ATTRIBUTE, dataclasses.replace(state, clock=moving))  # type: ignore[attr-defined]
        page = client.get(path, headers=PAGE_HEADERS)
        first_reads = moving.reads
        following = client.get(path, headers=PAGE_HEADERS)

    assert page.status_code == 200
    assert first_reads == 1
    if path == HER_PAGE:
        assert "Wednesday, August 19" in page.text
        assert page.text.count("You can skip this block.") == 2
        assert "This plan includes work you now report as Done." in page.text
        assert anchor_for(record.draft_id) not in following.text
    elif path == "/parent":
        assert page.text.count("She can skip this block.") == 2
        assert "She can skip this block." not in following.text
    else:
        assert f"#{anchor_for(record.draft_id)}" in page.text
        assert "No plan is saved for today now." in following.text


# ------------------------------------------------------------- a card's week is held to a real week


@pytest.mark.parametrize("week", ["not-a-day", "9999-12-31", "0001-01-01", "2026-13-40"])
@pytest.mark.parametrize("action", ["report", "undo-report"])
def test_a_card_form_that_names_no_real_week_writes_nothing(week: str, action: str) -> None:
    """A week that is not a day, or is past either edge of the calendar, is not a week her
    page made: both forms are refused, 422, on her current week, before anything is
    written, and a blank week is her current week as it always was."""
    with browser() as client:
        report(client, ESSAY_ID, "not_yet")
        store = state_of(client).project_state
        head = store.student_reports(ESSAY_ID)[-1].report_id
        fields = (
            {"status": "done", "note": "kept words", "expected_report_id": head}
            if action == "report"
            else {"report_id": head}
        )
        refused = client.post(f"{ACTIONS}/{action}", data={**fields, "week": week})
        blank = client.post(f"{ACTIONS}/{action}", data={**fields, "week": ""})
        events = store.student_reports(ESSAY_ID)

    assert refused.status_code == 422
    assert BAD_RETURN in refused.text
    assert "<h1>My week</h1>" in refused.text
    assert blank.status_code == 303
    assert len(events) == 2


# ------------------------------------------------------------- Change keeps the way back


@pytest.mark.parametrize("origin", ["today", "week", "family plan"])
def test_change_save_and_undo_on_the_details_keep_the_way_back_they_came_with(origin: str) -> None:
    """From a plan, a chosen week, or a plan on the family page: Change opens the form on
    the details with the same way back, the save returns there with it, and so does Undo."""
    with browser(key=True) as client:
        walkthrough(client)
        record = planned(client)
        report(client, ESSAY_ID, "not_yet")
        query = {
            "today": "return_to=today",
            "week": "return_to=week&week=2026-08-24",
            "family plan": f"return_to=family&plan_id={record.draft_id}",
        }[origin]
        page = client.get(f"{DETAILS}?{query}", headers=PAGE_HEADERS).text
        start = page.index(f'<form method="get" action="{DETAILS}"')
        change = dict(
            re.findall(
                r'<input type="hidden" name="([^"]+)" value="([^"]*)">',
                page[start : page.index("</form>", start)],
            )
        )
        editor = client.get(DETAILS, params=change, headers=PAGE_HEADERS).text
        form = editor[editor.index(f'action="{ACTIONS}/report"') :]
        hidden = dict(
            re.findall(
                r'<input type="hidden" name="([^"]+)" value="([^"]*)">',
                form[: form.index("</form>")],
            )
        )
        saved = client.post(f"{ACTIONS}/report", data={**hidden, "status": "done", "note": ""})
        after = client.get(saved.headers["location"], headers=PAGE_HEADERS).text
        undo = after[after.index(f'action="{ACTIONS}/undo-report"') :]
        undone = client.post(
            f"{ACTIONS}/undo-report",
            data=dict(
                re.findall(
                    r'<input type="hidden" name="([^"]+)" value="([^"]*)">',
                    undo[: undo.index("</form>")],
                )
            ),
        )

    expected = {
        "today": {"return_to": "today"},
        "week": {"return_to": "week", "week": "2026-08-24"},
        "family plan": {"return_to": "family", "plan_id": record.draft_id},
    }[origin]
    assert change == {**expected, "change": "1"}
    assert {name: value for name, value in hidden.items() if name in expected} == expected
    assert saved.status_code == 303
    assert undone.status_code == 303
    for location in (saved.headers["location"], undone.headers["location"]):
        assert location.startswith(f"{DETAILS}?")
        for name, value in expected.items():
            assert f"{name}={value.replace(':', '%3A')}" in location
    label = {"today": "today&#39;s plan", "week": "the week", "family plan": "family review"}
    assert after.count(f"Back to {label[origin]}</a>") == 2


# ------------------------------------------------------------- the failure page keeps the way back


@pytest.mark.parametrize(
    ("origin", "links"),
    [
        pytest.param(
            {"report_view": "detail", "return_to": "today", "week": "", "plan_id": ""},
            [
                (f"{DETAILS}?return_to=today", "Back to the assignment"),
                ("/student/due-this-week#today", "Back to Today"),
            ],
            id="from-todays-plan",
        ),
        pytest.param(
            {"report_view": "detail", "return_to": "week", "week": "2026-08-10", "plan_id": ""},
            [
                (f"{DETAILS}?return_to=week&amp;week=2026-08-10", "Back to the assignment"),
                (
                    f"/student/due-this-week?week=2026-08-10&amp;show={ESSAY_ID}"
                    f"#assignment-{ESSAY_ID}",
                    "Back to the week",
                ),
            ],
            id="from-an-earlier-week",
        ),
        pytest.param(
            {
                "report_view": "detail",
                "return_to": "family",
                "week": "",
                "plan_id": "draft:plan:2026-08-19:abc",
            },
            [
                (
                    f"{DETAILS}?return_to=family&amp;plan_id=draft%3Aplan%3A2026-08-19%3Aabc",
                    "Back to the assignment",
                ),
                (
                    "/parent?plan=draft%3Aplan%3A2026-08-19%3Aabc#plan-draft-plan-2026-08-19-abc",
                    "Back to family review",
                ),
            ],
            id="from-a-family-plan",
        ),
        pytest.param(
            {"week": FIXTURE_WEEK},
            [
                (
                    f"/student/due-this-week?week={FIXTURE_WEEK}&amp;show={ESSAY_ID}"
                    f"#assignment-{ESSAY_ID}",
                    "Back to the week",
                )
            ],
            id="from-a-card",
        ),
    ],
)
@pytest.mark.parametrize("action", ["report", "undo-report"])
def test_when_the_write_and_the_reread_both_fail_the_plain_page_keeps_the_way_back(
    monkeypatch: pytest.MonkeyPatch,
    origin: dict[str, str],
    links: list[tuple[str, str]],
    action: str,
) -> None:
    """The file refuses the write and the page cannot be read back. The plain page says the
    update was not saved, keeps her words, markup, line break, and a character outside the
    basic plane included, and offers the way back the form carried, made from checked
    values alone: no store is read for it, and nothing she typed is in an address."""
    words = "kept <b>words</b>\r\nsecond line \U0001f33c"
    with browser() as client:
        report(client, ESSAY_ID, "not_yet")
        state = state_of(client)
        store = state.project_state
        head = store.student_reports(ESSAY_ID)[-1].report_id
        store._connection.execute(
            "CREATE TRIGGER refuse_reports BEFORE INSERT ON student_reports "
            "BEGIN SELECT RAISE(ABORT, 'refused'); END"
        )
        store._connection.commit()

        def unreadable(*_: object, **__: object) -> None:
            msg = "the record could not be read"
            raise RuntimeError(msg)

        def no_store(*_: object, **__: object) -> None:
            msg = "the plain page read a store"
            raise AssertionError(msg)

        monkeypatch.setattr(student_routes, "detail_page", unreadable)
        monkeypatch.setattr(student_routes, "student_page", unreadable)
        monkeypatch.setattr(state.drafts, "latest_for", no_store)
        monkeypatch.setattr(state.drafts, "get", no_store)
        fields = (
            {"status": "done", "note": words, "expected_report_id": head}
            if action == "report"
            else {"report_id": head}
        )
        answer = client.post(f"{ACTIONS}/{action}", data={**fields, **origin})
        monkeypatch.undo()
        events = store.student_reports(ESSAY_ID)

    text = answer.text
    assert answer.status_code == 500
    assert "<h1>Update not saved</h1>" in text
    assert (NOT_SAVED if action == "report" else NOT_UNDONE) in text
    for href, label in links:
        assert f'<a href="{href}">{label}</a>' in text
    if action == "report":
        assert f"readonly>{escape(words)}</textarea>" in text
        assert "Your choice: Done." in text
    assert "kept" not in "".join(re.findall(r'href="([^"]*)"', text))
    assert len(events) == 1
