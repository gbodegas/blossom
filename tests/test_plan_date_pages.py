"""A saved plan's dates on both pages: the date each row was planned with, the label the
plan saved, and, on a plan still in force for its evening, what stands now.

The fixture week through the app, a pinned clock, scripted plans, and the record changed
between planning and reading, through the workflows that change it where there are any
and written directly where the state is one no workflow writes.
"""

import pathlib
import re
import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from html import unescape

import pytest
from fastapi.testclient import TestClient

from blossom import plan_dates
from blossom.agent.compose import compose
from blossom.app import create_app
from blossom.candidates import readings_for, row_reader
from blossom.captures import (
    STUDENT,
    CaptureChanged,
    CaptureDetails,
    CapturePromoted,
    CaptureUnlinked,
    candidate_basis,
)
from blossom.drafts import DraftStatus
from blossom.noticing import (
    canonical_active_input,
    expect_due_date,
    in_week,
    notice_due_date,
    planning_digest,
    read_everything,
    reconcile_dates,
    week_from,
)
from blossom.plan_checks import check_plan
from blossom.plan_reading import anchor_for
from blossom.plans import DailyPlan, Deferral, PlanBlock
from blossom.reconciliation import (
    Agreement,
    SourceChannel,
    SourceConfidence,
    SourceRecord,
    classify_confidence,
)
from blossom.routes.runs import plan_graphs
from blossom.stores.drafts import DraftRecord
from blossom.stores.project_state import Assignment, ProjectStateStore
from tests.support import (
    ESSAY_ID,
    HER_PAGE,
    HERS,
    PAGE_HEADERS,
    PLAN_DATE,
    QUIZ_ID,
    SAME_ORIGIN,
    SYLLABUS_ID,
    THEIRS,
    ZONE,
    accepting,
    browser,
    composed_plan,
    control_names,
    files_in,
    fixture_settings,
    lands_on,
    plan_on,
    planned,
    record,
    report,
    scripted_graphs,
    signed_in,
    state_of,
    store_of,
    two_sittings,
    waiting_note,
    walkthrough,
    walkthrough_plan,
    words,
)

LMS, EMAIL = SourceChannel.LMS, SourceChannel.EMAIL
FAMILY = SourceChannel.PARENT_ENTRY
A19, A20, A21, A22, A23 = (date(2026, 8, day) for day in (19, 20, 21, 22, 23))
TEXTBOOK_ID = "assignment-textbook-cover"
ALGEBRA_ID = "assignment-algebra-set"
SCIENCE_ID = "assignment-science-fair-proposal"
LINK = "See what is on record now"
X = "A claim about this date cannot be read right now, so what the sources say is not fully known."
U = "Some source information cannot be read as a date. Check the current record."
MATCH = "The readable sources now match the recorded due date."
UNSUPPORTED = "No readable source currently supports the recorded due date."
LIMIT = "This saved plan does not keep enough detail to compare every change in its sources."
HISTORY = "This is the saved plan. Assignment links open the current record."
CUED_A = f"Recorded due August 21, 2026. Check this date. {LINK}"
PLAIN_A = "Recorded due August 21, 2026."
UNDATED_A = f"No due date on record. Check this date. {LINK}"
PLANNED_CUE = [record(LMS, "2026-08-21"), record(FAMILY, "2026-08-22")]
NOTE_TIME = datetime(2026, 8, 19, 21, 0, tzinfo=UTC)


# ------------------------------------------------------------- reading a row back


def row_of(plan: str, dom_id: str) -> str:
    found = re.search(
        rf'<li class="plan-[a-z]+[^"]*" id="{re.escape(dom_id)}">(.*?)</li>', plan, re.S
    )
    assert found is not None, dom_id
    return found.group(1)


def lines(plan: str, dom_id: str) -> tuple[str, str | None]:
    """A row's saved date line and its current line, as words."""
    row = row_of(plan, dom_id)
    saved = re.search(r'<p class="plan-due">(.*?)</p>', row, re.S)
    now = re.search(r'<p class="plan-now">(.*?)</p>', row, re.S)
    assert saved is not None
    return words(saved.group(1)), None if now is None else words(now.group(1))


def block(record_: DraftRecord, index: int) -> str:
    return f"{anchor_for(record_.draft_id)}-block-{index}"


def deferral(record_: DraftRecord, index: int) -> str:
    return f"{anchor_for(record_.draft_id)}-deferral-{index}"


def essay_rows(page: str, record_: DraftRecord) -> list[tuple[str, str | None]]:
    """The essay's two sittings in the walkthrough plan."""
    plan = plan_on(page, record_)
    return [lines(plan, block(record_, 0)), lines(plan, block(record_, 2))]


def article_of(page: str, record_: DraftRecord) -> str:
    """The family page's article that holds one plan, decision line included."""
    at = page.index(f'id="{anchor_for(record_.draft_id)}"')
    start = page.rindex("<article", 0, at)
    return page[start : page.index("</article>", at)]


# ------------------------------------------------------------- the record between plan and page


def essay_is(
    client: TestClient,
    due: date | None,
    claims: Sequence[SourceRecord],
    *,
    unreadable: bool = False,
) -> None:
    """The essay's recorded date and its counting claims, written directly: the claims the
    school makes accumulate, so a set of them no workflow could leave is a constructed state.
    ``unreadable`` adds one more claim row and damages it."""
    store = store_of(client)
    item = store.one_assignment(ESSAY_ID)
    assert item is not None
    with store._lock, store._connection:
        store._connection.execute("DELETE FROM date_claims WHERE assignment_id=?", (ESSAY_ID,))
    store.put_on_record([item.model_copy(update={"due_date": due})], {ESSAY_ID: list(claims)})
    if unreadable:
        damaged_claim(store, ESSAY_ID)


def damaged_claim(store: ProjectStateStore, assignment_id: str) -> None:
    """One more claim row about the assignment, which then cannot be read."""
    store.record_claims(assignment_id, [record(LMS, "2026-08-21")])
    with store._lock, store._connection:
        store._connection.execute(
            "UPDATE date_claims SET active = 7 WHERE rowid = "
            "(SELECT MAX(rowid) FROM date_claims WHERE assignment_id = ?)",
            (assignment_id,),
        )


def unrelated_update(client: TestClient) -> None:
    """Her Not yet with a note on the quiz, which changes nothing about the essay's date."""
    report(client, QUIZ_ID, "not_yet", "starting it tomorrow")


def pages(client: TestClient) -> tuple[str, str]:
    hers = client.get(HER_PAGE, headers=PAGE_HEADERS)
    family = client.get("/parent", headers=PAGE_HEADERS)
    assert (hers.status_code, family.status_code) == (200, 200)
    return hers.text, family.text


def fingerprint_now(client: TestClient, evening: date) -> str:
    store = store_of(client)
    return planning_digest(week_from(read_everything(store, store), evening))


# ------------------------------------------------------------- the walkthrough, labeled by id


@pytest.mark.parametrize("reader", ["her, sign-in off", "a parent on her week", "family page"])
def test_the_walkthrough_plan_is_labeled_by_assignment_id_on_both_pages(
    reader: str, tmp_path: pathlib.Path
) -> None:
    """The essay's two blocks, the textbook, the syllabus and the quiz carry the label."""
    paths = files_in(tmp_path)
    with browser(key=True, **paths) as client:
        walkthrough(client)
        made = planned(client)
        page = client.get(
            "/parent" if reader == "family page" else HER_PAGE, headers=PAGE_HEADERS
        ).text
    if reader == "a parent on her week":
        settings = fixture_settings(
            BLOSSOM_TODAY=PLAN_DATE.isoformat(),
            BLOSSOM_STUDENT_PASSPHRASE=HERS,
            BLOSSOM_PARENT_PASSPHRASE=THEIRS,
            **paths,
        )
        with TestClient(
            create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
        ) as client:
            signed_in(client, THEIRS)
            page = client.get(HER_PAGE, headers=PAGE_HEADERS).text

    plan = plan_on(page, made)
    labeled = {
        "essay, first sitting": block(made, 0),
        "essay, second sitting": block(made, 2),
        "textbook": deferral(made, 1),
        "syllabus": deferral(made, 3),
        "quiz": deferral(made, 4),
    }
    plain = {
        "science fair": block(made, 1),
        "algebra": deferral(made, 0),
        "reading log": deferral(made, 2),
        "the English namesake": deferral(made, 5),
    }
    for name, dom_id in labeled.items():
        saved, now = lines(plan, dom_id)
        assert "Check this date." in saved, name
        assert saved.endswith(LINK), name
        assert now is None, name
    for name, dom_id in plain.items():
        saved, now = lines(plan, dom_id)
        assert "Check this date" not in saved, name
        assert LINK not in saved, name
        assert now is None, name
        assert saved.startswith(("Recorded due ", "No due date on record.")), name
    rows_shown = plan[: plan.index('<details class="steps plan-original">')]
    assert rows_shown.count("Dates needing clarification") == 1
    clarify = rows_shown[rows_shown.index("Dates needing clarification") :]
    assert clarify[: clarify.index("</ul>")].count(f"/student/assignments/{ESSAY_ID}?") == 1
    assert "Not in this evening's plan</h" in plan
    assert LIMIT not in plan
    assert "As things stand" not in page


# ------------------------------------------------------------- each case through both pages

PAGE_CASES = [
    pytest.param(
        A21,
        [record(FAMILY, "2026-08-20")],
        A21,
        [record(FAMILY, "2026-08-19")],
        False,
        False,
        CUED_A,
        "As things stand: The sources give August 19, 2026, not the recorded due date.",
        id="a-source-moves",
    ),
    pytest.param(
        None,
        [],
        None,
        [record(LMS, "2026-08-21")],
        False,
        False,
        UNDATED_A,
        "As things stand: The sources give August 21, 2026, but no due date is on record.",
        id="first-source-for-undated-work",
    ),
    pytest.param(
        A21,
        [record(FAMILY, "2026-08-20")],
        A21,
        [],
        False,
        False,
        CUED_A,
        f"As things stand: {UNSUPPORTED}",
        id="contradicting-source-removed",
    ),
    pytest.param(
        A21,
        [record(FAMILY, "2026-08-20")],
        A21,
        [record(FAMILY, "Friday")],
        False,
        False,
        CUED_A,
        f"As things stand: {UNSUPPORTED} {U}",
        id="source-now-unparseable-constructed",
    ),
    pytest.param(
        A21,
        PLANNED_CUE,
        A21,
        [record(LMS, "2026-08-21"), record(EMAIL, "2026-08-21")],
        False,
        False,
        CUED_A,
        f"As things stand: {MATCH}",
        id="positive-agreement",
    ),
    pytest.param(
        A21,
        PLANNED_CUE,
        A21,
        PLANNED_CUE,
        False,
        False,
        CUED_A,
        "As things stand: The sources give different dates: August 21 and August 22, 2026.",
        id="conflict-persists-fingerprint-changed",
    ),
    pytest.param(
        A21,
        PLANNED_CUE,
        A21,
        PLANNED_CUE,
        False,
        True,
        CUED_A,
        None,
        id="conflict-persists-fingerprint-unchanged",
    ),
    pytest.param(
        A21,
        PLANNED_CUE,
        A21,
        [record(LMS, "2026-08-21"), record(FAMILY, "2026-08-23")],
        False,
        False,
        CUED_A,
        "As things stand: The sources give different dates: August 21 and August 23, 2026.",
        id="conflict-persists-a-claimed-date-moved",
    ),
    pytest.param(
        A21,
        [record(LMS, "2026-08-21")],
        A21,
        [record(LMS, "2026-08-21"), record(LMS, "2026-08-22")],
        False,
        False,
        PLAIN_A,
        "As things stand: The sources give different dates: August 21 and August 22, 2026. "
        f"Check this date. {LINK}",
        id="new-conflict-same-recorded-date",
    ),
    pytest.param(
        A21,
        [record(FAMILY, "2026-08-22")],
        A22,
        [record(FAMILY, "2026-08-22")],
        False,
        False,
        CUED_A,
        "As things stand: Since this plan was made, the recorded due date changed to August 22, "
        f"2026. {MATCH}",
        id="date-and-evidence-change-together",
    ),
    pytest.param(
        A21,
        [record(LMS, "2026-08-21")],
        A22,
        [record(LMS, "2026-08-21")],
        False,
        False,
        PLAIN_A,
        "As things stand: Since this plan was made, the recorded due date changed to August 22, "
        "2026. The sources give August 21, 2026, not the recorded due date. Check this date. "
        f"{LINK}",
        id="date-changes-source-stays",
    ),
    pytest.param(
        A21,
        [],
        None,
        [],
        False,
        False,
        PLAIN_A,
        "As things stand: When this plan was made, a due date was recorded. There is no due "
        "date on record now. No readable source gives a due date. Check this date. "
        f"{LINK}",
        id="date-removed",
    ),
    pytest.param(
        None,
        [],
        A21,
        [],
        False,
        False,
        UNDATED_A,
        "As things stand: Since this plan was made, a due date was recorded: August 21, 2026. "
        f"{UNSUPPORTED}",
        id="date-added",
    ),
    pytest.param(
        A21,
        PLANNED_CUE,
        A21,
        [record(LMS, "2026-08-21")],
        True,
        False,
        CUED_A,
        f"As things stand: {X}",
        id="readable-support-a-claim-unreadable",
    ),
    pytest.param(
        A21,
        [record(LMS, "2026-08-21")],
        A21,
        [record(LMS, "2026-08-19")],
        True,
        False,
        PLAIN_A,
        "As things stand: The sources give August 19, 2026, not the recorded due date. "
        f"{X} Check this date. {LINK}",
        id="readable-conflict-a-claim-unreadable",
    ),
    pytest.param(
        A21,
        [record(LMS, "2026-08-21")],
        A22,
        [record(LMS, "2026-08-22")],
        True,
        False,
        PLAIN_A,
        "As things stand: Since this plan was made, the recorded due date changed to August 22, "
        f"2026. {X} {LINK}",
        id="date-changed-a-claim-unreadable",
    ),
    pytest.param(
        None,
        [],
        None,
        [],
        False,
        False,
        UNDATED_A,
        "As things stand: No readable source gives a due date.",
        id="absent-date-stays-unknown",
    ),
    pytest.param(
        A21,
        PLANNED_CUE,
        A21,
        PLANNED_CUE,
        True,
        True,
        CUED_A,
        f"As things stand: {X}",
        id="nothing-changed-but-a-claim-unreadable",
    ),
    pytest.param(
        A21,
        PLANNED_CUE,
        A21,
        [record(LMS, "2026-08-21"), record(EMAIL, "Friday")],
        False,
        False,
        CUED_A,
        f"As things stand: {U}",
        id="cued-match-beside-an-unparseable-value-constructed",
    ),
]


@pytest.mark.parametrize(
    ("saved_due", "then", "due", "claims", "unreadable", "equal", "line_a", "line_b"), PAGE_CASES
)
def test_each_case_reads_the_same_on_both_pages_and_changes_no_saved_plan(
    saved_due: date | None,
    then: list[SourceRecord],
    due: date | None,
    claims: list[SourceRecord],
    unreadable: bool,
    equal: bool,
    line_a: str,
    line_b: str | None,
) -> None:
    with browser(key=True) as client:
        walkthrough(client)
        essay_is(client, saved_due, then)
        made = planned(client)
        if equal:
            if unreadable:
                damaged_claim(store_of(client), ESSAY_ID)
        else:
            essay_is(client, due, claims, unreadable=unreadable)
            unrelated_update(client)
        assert (fingerprint_now(client, PLAN_DATE) == made.inputs_digest) is equal
        hers, family = pages(client)
        after = state_of(client).drafts.get(made.draft_id)

    for page in (hers, family):
        assert essay_rows(page, made) == [(line_a, line_b), (line_a, line_b)]
        assert plan_on(page, made).count(LIMIT) <= 1
        assert "match" not in (line_b or "") or not (unreadable or "Friday" in str(claims))
    assert after == made


def test_a_row_whose_assignment_is_gone_keeps_its_saved_line_on_both_pages() -> None:
    with browser(key=True) as client:
        walkthrough(client)
        made = planned(client)
        store = store_of(client)
        with store._lock, store._connection:
            store._connection.execute("DELETE FROM assignments WHERE assignment_id=?", (ESSAY_ID,))
        hers, family = pages(client)

    gone = (
        "Recorded due August 21, 2026. Check this date. This assignment is not on record now.",
        None,
    )
    for page in (hers, family):
        assert essay_rows(page, made) == [gone, gone]
        row = row_of(plan_on(page, made), block(made, 0))
        assert "<a " not in row.split('<p class="plan-due">', 1)[1]


# ------------------------------------------------------------- evidence from the note workflows


def note_joined(client: TestClient, target: str, day: date) -> tuple[str, int]:
    """A note of hers with a due date, joined to homework on record: her note's day counts
    as one more claim beside the school's. The note's id and revision."""
    store = store_of(client)
    item = store.one_assignment(target)
    assert item is not None
    name = waiting_note(
        store, course=item.course, title=item.title, text="Synthetic note", due_date=day
    )
    joined = store.link_capture(
        name,
        target=target,
        expected_revision=2,
        basis=candidate_basis(readings_for(store, [item])),
        shown=row_reader(store),
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=NOTE_TIME,
        today=PLAN_DATE,
    )
    assert isinstance(joined, CapturePromoted), joined
    return name, joined.capture.revision


def note_taken_off(client: TestClient, name: str, revision: int, target: str) -> int:
    store = store_of(client)
    left = store.unlink_capture(
        name,
        expected_revision=revision,
        leaving=target,
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=NOTE_TIME,
        today=PLAN_DATE,
    )
    assert isinstance(left, CaptureUnlinked), left
    return left.capture.revision


def note_joined_again(client: TestClient, name: str, revision: int, target: str, day: date) -> None:
    store = store_of(client)
    item = store.one_assignment(target)
    assert item is not None
    moved = store.clarify_capture(
        name,
        CaptureDetails(course=item.course, title=item.title, kind="HOMEWORK", due_date=day),
        expected_revision=revision,
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=NOTE_TIME,
        today=PLAN_DATE,
    )
    assert isinstance(moved, CaptureChanged), moved
    joined = store.link_capture(
        name,
        target=target,
        expected_revision=moved.capture.revision,
        basis=candidate_basis(readings_for(store, [item])),
        shown=row_reader(store),
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=NOTE_TIME,
        today=PLAN_DATE,
    )
    assert isinstance(joined, CapturePromoted), joined


@pytest.mark.parametrize("case", ["the note's day moves", "the note is taken off"])
def test_a_note_taken_off_or_joined_again_with_another_day_reads_as_present_evidence(
    case: str,
) -> None:
    """The science fair proposal, recorded Aug 20, with her note saying Aug 19."""
    with browser(key=True) as client:
        walkthrough(client)
        name, revision = note_joined(client, SCIENCE_ID, A19)
        made = planned(client)
        revision = note_taken_off(client, name, revision, SCIENCE_ID)
        if case.startswith("the note's day"):
            note_joined_again(client, name, revision, SCIENCE_ID, date(2026, 8, 18))
        hers, family = pages(client)

    expected = (
        "As things stand: The sources give August 18, 2026, not the recorded due date."
        if case.startswith("the note's day")
        else f"As things stand: {UNSUPPORTED}"
    )
    for page in (hers, family):
        plan = plan_on(page, made)
        assert lines(plan, block(made, 1)) == (
            f"Recorded due August 20, 2026. Check this date. {LINK}",
            expected,
        )
        assert plan.count(LIMIT) == 1


def test_a_dated_note_joined_to_undated_work_reads_as_its_first_source() -> None:
    """The syllabus had no date and no source; her note now gives one."""
    with browser(key=True) as client:
        walkthrough(client)
        made = planned(client)
        note_joined(client, SYLLABUS_ID, A21)
        hers, family = pages(client)

    for page in (hers, family):
        assert lines(plan_on(page, made), deferral(made, 3)) == (
            UNDATED_A,
            "As things stand: The sources give August 21, 2026, but no due date is on record.",
        )


# ------------------------------------------------------------- the six acceptance cases


def test_an_unreadable_claim_shows_although_the_fingerprint_is_equal() -> None:
    with browser(key=True) as client:
        walkthrough(client)
        made = planned(client)
        damaged_claim(store_of(client), ALGEBRA_ID)
        equal = fingerprint_now(client, PLAN_DATE) == made.inputs_digest
        hers, family = pages(client)

    assert equal
    for page in (hers, family):
        plan = plan_on(page, made)
        assert lines(plan, deferral(made, 0)) == (
            "Recorded due August 24, 2026.",
            f"As things stand: {X} {LINK}",
        )
        assert "match" not in words(plan)
        assert LIMIT not in plan
        assert lines(plan, block(made, 0))[1] is None


def test_an_unrelated_update_restates_present_evidence_and_says_nothing_changed() -> None:
    """Her Not yet on the quiz moves the fingerprint; no date and no source changed."""
    with browser(key=True) as client:
        walkthrough(client)
        made = planned(client)
        before = pages(client)
        unrelated_update(client)
        moved = fingerprint_now(client, PLAN_DATE) != made.inputs_digest
        after = pages(client)

    assert moved
    cued = {
        block(made, 0): "The sources give different dates: August 21 and August 22, 2026.",
        block(made, 2): "The sources give different dates: August 21 and August 22, 2026.",
        deferral(made, 1): "The sources give different dates: August 21 and August 22, 2026.",
        deferral(made, 3): "No readable source gives a due date.",
        deferral(made, 4): "The sources give August 21, 2026, not the recorded due date.",
    }
    silent = [block(made, 1), deferral(made, 0), deferral(made, 2), deferral(made, 5)]
    for then, now in zip(before, after, strict=True):
        plan, earlier = plan_on(now, made), plan_on(then, made)
        for dom_id, evidence in cued.items():
            assert lines(plan, dom_id) == (
                lines(earlier, dom_id)[0],
                f"As things stand: {evidence}",
            )
        for dom_id in silent:
            assert lines(plan, dom_id) == lines(earlier, dom_id)
        assert plan.count(LIMIT) == 1
        said = words(plan)
        assert "Since this plan was made" not in said
        assert " changed" not in said.replace("Its times have not changed", "")


@pytest.mark.parametrize("digest", ["none", "an earlier namespace"])
def test_a_missing_or_older_fingerprint_is_never_read_as_unchanged(digest: str) -> None:
    """Nothing about the week changed; the plan cannot show that it did not."""
    with browser(key=True) as client:
        walkthrough(client)
        made = planned(client)
        store = store_of(client)
        week = week_from(read_everything(store, store), PLAN_DATE)
        older = uuid.uuid5(
            uuid.UUID("7d1e6a34-0000-4000-8000-000000000000"),
            str(canonical_active_input(week)),
        ).hex
        drafts = state_of(client).drafts
        drafts._connection.execute(
            "UPDATE drafts SET inputs_digest=? WHERE draft_id=?",
            (None if digest == "none" else older, made.draft_id),
        )
        drafts._connection.commit()
        hers, family = pages(client)

    for page in (hers, family):
        plan = plan_on(page, made)
        assert lines(plan, block(made, 0))[1] == (
            "As things stand: The sources give different dates: August 21 and August 22, 2026."
        )
        assert plan.count(LIMIT) == 1
    stale = "Plan again." in family
    assert stale is (digest != "none")


def test_a_recorded_date_change_is_its_own_sentence_even_beside_an_equal_fingerprint() -> None:
    """A constructed state: the saved fingerprint made to match a week whose essay moved."""
    with browser(key=True) as client:
        walkthrough(client)
        made = planned(client)
        essay_is(client, A22, PLANNED_CUE)
        drafts = state_of(client).drafts
        drafts._connection.execute(
            "UPDATE drafts SET inputs_digest=? WHERE draft_id=?",
            (fingerprint_now(client, PLAN_DATE), made.draft_id),
        )
        drafts._connection.commit()
        hers, family = pages(client)

    for page in (hers, family):
        assert essay_rows(page, made)[0] == (
            CUED_A,
            "As things stand: Since this plan was made, the recorded due date changed to August "
            "22, 2026. Check this date.",
        )
        assert LIMIT not in plan_on(page, made)


def test_an_unparseable_value_beside_a_match_changes_only_what_the_page_says() -> None:
    """A constructed state: the page withholds agreement and the reconciliation is unchanged."""
    claims = [record(LMS, "2026-08-21"), record(EMAIL, "Friday")]
    essay = Assignment(
        assignment_id=ESSAY_ID,
        course="World History",
        title="Canal Era comparison essay",
        due_date=A21,
        dependencies=[],
        reported_submission_status="in_progress",
    )
    noticed = notice_due_date(expect_due_date(essay), claims)
    result = reconcile_dates(claims)

    assert isinstance(result, Agreement)
    assert [item.asserted_value for item in result.records] == ["2026-08-21"]
    assert classify_confidence(result) is SourceConfidence.SINGLE_SOURCE
    assert noticed.verdict.value == "CONFIRMED"
    assert noticed.observed_dates == (A21,)
    assert in_week(essay, noticed, PLAN_DATE)
    with browser(key=True) as client:
        walkthrough(client)
        essay_is(client, A21, PLANNED_CUE)
        made = planned(client)
        essay_is(client, A21, claims)
        store = store_of(client)
        hashed = canonical_active_input(week_from(read_everything(store, store), PLAN_DATE))
        drafts = state_of(client).drafts
        drafts._connection.execute(
            "UPDATE drafts SET inputs_digest=? WHERE draft_id=?",
            (fingerprint_now(client, PLAN_DATE), made.draft_id),
        )
        drafts._connection.commit()
        damaged_claim(store, ESSAY_ID)
        hers, family = pages(client)

    essay_input = next(row for row in hashed if row["id"] == ESSAY_ID)
    assert ["EMAIL", "Friday", ""] in essay_input["claims"]  # type: ignore[operator]
    for page in (hers, family):
        assert essay_rows(page, made)[0] == (CUED_A, f"As things stand: {X}")
        assert "match" not in words(plan_on(page, made))


# ------------------------------------------------------------- operative plans on the family page

SAME_MOMENT = datetime(2026, 8, 19, 4, 0, tzinfo=UTC)


def plan_for(client: TestClient, namesake: str, evening: date) -> DraftRecord:
    client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
        lambda: [walkthrough_plan(namesake).model_copy(update={"plan_date": evening})],
        lambda: [accepting()],
    )
    made = client.post("/parent/plans", json={"plan_date": evening.isoformat()})
    assert made.status_code == 201, made.text[:300]
    found = state_of(client).drafts.get(made.json()["draft_id"])
    assert found is not None
    return found


def stored_plan(client: TestClient, evening: date, tag: str, decision: str | None) -> DraftRecord:
    """A plan for a later evening saved and published through the drafts store, as a paused
    run leaves it, then decided there, at one instant with every other plan made this way."""
    drafts = state_of(client).drafts
    made = composed_plan(
        two_sittings().model_copy(update={"plan_date": evening}),
        draft_id=f"draft:plan:{evening}:{tag}",
    )
    drafts.record_waiting(
        made.draft.model_copy(update={"created_at": SAME_MOMENT}),
        thread_id=f"thread-{evening}-{tag}",
        plan_date=evening,
        outcome="accepted",
        plan_assignment_ids=made.snapshot.assignment_ids,
        plan_snapshot=made.snapshot,
    )
    drafts.publish(made.draft.draft_id)
    if decision is not None:
        drafts.record_decision(
            made.draft.draft_id,
            status=DraftStatus.APPROVED_FOR_MANUAL_SEND
            if decision == "approved"
            else DraftStatus.DRAFT,
            decision=decision,  # type: ignore[arg-type]
            reason="Start with the outline." if decision == "rejected" else None,
        )
    found = drafts.get(made.draft.draft_id)
    assert found is not None
    return found


def decide(client: TestClient, record_: DraftRecord, decision: str) -> None:
    done = client.post(f"/parent/actions/decide/{record_.draft_id}", data={"decision": decision})
    assert done.status_code == 303, done.text[:300]


MOVED = (
    "As things stand: Since this plan was made, the recorded due date changed to August 23, 2026."
)


def test_each_plan_still_in_force_shows_what_stands_and_the_rest_read_as_saved(
    tmp_path: pathlib.Path,
) -> None:
    """Each evening's plan in force shows current facts, whatever was decided about it."""
    paths = files_in(tmp_path)
    with browser(key=True, **paths) as client:
        namesake = walkthrough(client)
        today_first = planned(client)
        decide(client, today_first, "approve")
        today_second = planned(client)
        tomorrow_first = plan_for(client, namesake, A20)
        decide(client, tomorrow_first, "approve")
        tomorrow_second = plan_for(client, namesake, A20)
        refused = stored_plan(client, A21, "a", "rejected")
        approved = stored_plan(client, A22, "a", "approved")
        same_first = stored_plan(client, A23, "z", "approved")
        same_later = stored_plan(client, A23, "a", "approved")
        drafts = state_of(client).drafts
        before = {item.draft_id: item for item in (*drafts.waiting(), *drafts.decided())}
        snapshot = drafts.review_snapshot(PLAN_DATE)
        latest = {evening: drafts.latest_for(evening) for evening in (A19, A20, A21, A22, A23)}
        store = store_of(client)
        item = store.one_assignment(ESSAY_ID)
        assert item is not None
        store.upsert_assignments([item.model_copy(update={"due_date": A23})])
        family = client.get("/parent", headers=PAGE_HEADERS).text
        after = {item.draft_id: item for item in (*drafts.waiting(), *drafts.decided())}
    rolled = create_app(fixture_settings(BLOSSOM_TODAY="2026-08-20", **paths))
    with TestClient(rolled, follow_redirects=False, headers=SAME_ORIGIN) as client:
        next_day = client.get("/parent", headers=PAGE_HEADERS).text
        next_snapshot = state_of(client).drafts.review_snapshot(A20)

    live = [today_second, tomorrow_second, refused, approved, same_later]
    saved = [today_first, tomorrow_first, same_first]
    assert snapshot.operative == {found.draft_id for found in latest.values() if found}
    assert snapshot.operative == {plan.draft_id for plan in live}
    assert after == before
    for plan in live:
        found = plan_on(family, plan)
        essay = lines(found, block(plan, 0))
        assert (essay[1] or "").startswith(MOVED), plan.draft_id
    for plan in saved:
        found = plan_on(family, plan)
        assert lines(found, block(plan, 0))[1] is None, plan.draft_id
        assert "As things stand" not in found
        assert HISTORY in found
    rejected = article_of(family, refused)
    assert "<strong>Change asked.</strong>" in rejected
    assert "Reason: Start with the outline." in rejected
    assert "Looks good." not in rejected
    assert next_snapshot.operative == {plan.draft_id for plan in live[1:]}
    assert "As things stand" not in plan_on(next_day, today_second)
    assert HISTORY in plan_on(next_day, today_second)
    assert lines(plan_on(next_day, tomorrow_second), block(tomorrow_second, 0))[1] is not None


# ------------------------------------------------------------- a Done row, namesakes and links


def test_a_done_row_keeps_its_date_lines_above_its_mark() -> None:
    with browser(key=True) as client:
        walkthrough(client)
        made = planned(client)
        essay_is(client, A22, [record(LMS, "2026-08-21")])
        report(client, ESSAY_ID, "done")
        hers, family = pages(client)

    for page in (hers, family):
        row = row_of(plan_on(page, made), block(made, 0))
        order = re.findall(r'<p class="(plan-[a-z-]+)">', row)
        assert order == [
            "plan-when",
            "plan-what",
            "plan-due",
            "plan-now",
            "plan-mark",
            "plan-mark-words",
            "plan-why",
        ]
        assert '<span class="plan-why-label">Reason when planned:</span>' in row


@pytest.mark.parametrize("page", ["her week", "family page"])
def test_each_evidence_link_names_its_assignment_and_lands_on_what_the_sources_say(
    page: str,
) -> None:
    """Namesakes are told apart by course and date, and each link keeps the way back."""
    with browser(key=True) as client:
        namesake = walkthrough(client)
        made = planned(client)
        store_of(client).record_claims(namesake, [record(LMS, "2026-08-19")])
        shown = client.get(HER_PAGE if page == "her week" else "/parent", headers=PAGE_HEADERS).text
        plan = plan_on(shown, made)
        links = re.findall(
            r'<a class="assignment-link details-link" href="([^"]+)" '
            r'aria-label="([^"]+)">([^<]+)</a>',
            plan,
        )
        landed = {href: client.get(unescape(href), headers=PAGE_HEADERS) for href, _, _ in links}

    names = {unescape(label) for _, label, _ in links}
    assert f"{LINK} for Canal Era comparison essay, World History, due August 21, 2026" in names
    assert f"{LINK} for Canal Era comparison essay, English, due August 21, 2026" in names
    assert all(text == LINK for _, _, text in links)
    assert all(label.startswith(f"{LINK} for ") for _, label, _ in links)
    back = "return_to=today" if page == "her week" else "return_to=family&amp;plan_id="
    for href, answer in landed.items():
        assert back in href
        assert href.endswith("#evidence")
        assert answer.status_code == 200
        assert lands_on(answer.text, unescape(href)).startswith(
            '<section class="evidence" id="evidence"'
        )
    heard = [name for text, name in control_names(plan) if text == LINK]
    assert len(heard) == len(links)
    assert all(name.startswith(f"{LINK} for ") for name in heard)


# ------------------------------------------------------------- an older plan's words


def test_a_plan_composed_with_the_older_heading_keeps_its_words_and_takes_the_labels() -> None:
    """Its saved words and old heading stay, and its rows take labels by id."""
    with browser(key=True) as client:
        walkthrough(client)
        made = planned(client)
        drafts = state_of(client).drafts
        old_body = made.body.replace("Not in this evening's plan:", "Waiting for another day:")
        assert old_body != made.body
        drafts._connection.execute(
            "UPDATE drafts SET body=? WHERE draft_id=?", (old_body, made.draft_id)
        )
        drafts._connection.commit()
        unrelated_update(client)
        hers, family = pages(client)

    for page in (hers, family):
        plan = plan_on(page, made)
        assert "Waiting for another day:" in plan[plan.index('<pre class="plan-original-text">') :]
        assert "Not in this evening's plan</h" in plan
        assert "school portal (day header): 2026-08-21" in plan
        assert lines(plan, block(made, 0)) == (
            CUED_A,
            "As things stand: The sources give different dates: August 21 and August 22, 2026.",
        )


# ------------------------------------------------------------- what reading may not change


def test_showing_current_facts_writes_nothing_and_leaves_the_review_rules_as_they_were() -> None:
    with browser(key=True) as client:
        walkthrough(client)
        waiting = planned(client)
        essay_is(client, A22, [record(LMS, "2026-08-22")])
        hers, family = pages(client)
        drafts = state_of(client).drafts
        unchanged = drafts.get(waiting.draft_id)
        refused = client.post(f"/parent/approvals/{waiting.draft_id}", json={"approved": True})
        essay_is(client, A21, PLANNED_CUE)
        again = planned(client)
        decide(client, again, "approve")
        essay_is(client, A22, [record(LMS, "2026-08-22")])
        family_after = client.get("/parent", headers=PAGE_HEADERS).text

    assert unchanged == waiting
    assert refused.status_code == 409
    assert "Plan again." in family
    decided = article_of(family_after, again)
    assert "Plan again." not in decided
    assert MOVED.replace("23", "22") in words(decided)


# ------------------------------------------------------------- one reading, any size


def due_of(number: int) -> date:
    return date(2026, 8, 20) + timedelta(days=number % 5)


def row(number: int) -> Assignment:
    return Assignment(
        assignment_id=f"assignment-bulk-{number:03d}",
        course="Science",
        title=f"Practice set {number:03d}",
        due_date=due_of(number),
        dependencies=[],
        reported_submission_status="not_started",
    )


def traced(connection: sqlite3.Connection, seen: list[str]) -> None:
    connection.set_trace_callback(lambda statement: seen.append(" ".join(statement.split())))


def with_plans(client: TestClient, count: int) -> None:
    """``count`` assignments with two claims each, and ``count`` plans across two past
    evenings, today and four later ones, a third of them approved."""
    state = state_of(client)
    rows = [row(number) for number in range(count)]
    state.project_state.put_on_record(
        rows,
        {
            item.assignment_id: [
                record(LMS, due_of(number).isoformat()),
                record(FAMILY, (due_of(number) + timedelta(days=number % 2)).isoformat()),
            ]
            for number, item in enumerate(rows)
        },
    )
    for number in range(count):
        evening = PLAN_DATE + timedelta(days=((number + 2) % 7) - 2)
        first, second = rows[number], rows[(number + 1) % count]
        plan = DailyPlan(
            plan_date=evening,
            blocks=[
                PlanBlock(
                    assignment_id=first.assignment_id,
                    starts_at=time(16, 30),
                    ends_at=time(17, 0),
                    rationale="first",
                )
            ],
            deferred=[Deferral(assignment_id=second.assignment_id, reason="later")]
            if count > 1
            else [],
        )
        window = [first, second] if count > 1 else [first]
        made = compose(
            draft_id=f"draft:plan:{evening}:{number:04d}",
            plan=plan,
            assignments=window,
            verification=check_plan(
                plan, due_in_window=window, zone=ZONE, requested_evening=evening
            ),
            verdict=None,
            settled=True,
        )
        state.drafts.record_waiting(
            made.draft.model_copy(update={"created_at": SAME_MOMENT}),
            thread_id=f"thread-{number:04d}",
            plan_date=evening,
            outcome="accepted",
            plan_assignment_ids=made.snapshot.assignment_ids,
            plan_snapshot=made.snapshot,
        )
        state.drafts.publish(made.draft.draft_id)
        if number % 3 == 0:
            state.drafts.record_decision(
                made.draft.draft_id,
                status=DraftStatus.APPROVED_FOR_MANUAL_SEND,
                decision="approved",
                reason=None,
            )


@pytest.mark.parametrize("page", [HER_PAGE, "/parent"])
def test_current_facts_add_no_statement_at_any_size(page: str, tmp_path: pathlib.Path) -> None:
    costs = []
    for count in (1, 20, 200):
        folder = tmp_path / str(count)
        folder.mkdir()
        with browser(key=False, **files_in(folder)) as client:
            with_plans(client, count)
            state = state_of(client)
            on_record: list[str] = []
            on_drafts: list[str] = []
            traced(state.project_state._connection, on_record)
            traced(state.drafts._connection, on_drafts)
            try:
                shown = client.get(page, headers=PAGE_HEADERS)
            finally:
                state.project_state._connection.set_trace_callback(None)
                state.drafts._connection.set_trace_callback(None)
        assert shown.status_code == 200
        assert sum("FROM date_claims" in statement for statement in on_record) == 1
        costs.append((len(on_record), len(on_drafts)))

    assert costs == [(10, 1 if page == HER_PAGE else 2)] * 3


@pytest.mark.parametrize("page", [HER_PAGE, "/parent"])
def test_present_evidence_is_worked_out_once_for_each_assignment_of_the_live_plans(
    page: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    worked: list[str] = []
    real = plan_dates.date_now

    def counted(assignment: Assignment, *given: object, **named: object) -> object:
        worked.append(assignment.assignment_id)
        return real(assignment, *given, **named)  # type: ignore[arg-type]

    with browser() as client:
        with_plans(client, 20)
        drafts = state_of(client).drafts
        snapshot = drafts.review_snapshot(PLAN_DATE)
        live = [
            item
            for item in (*snapshot.waiting, *snapshot.decided)
            if item.draft_id in snapshot.operative
        ]
        if page == HER_PAGE:
            live = [item for item in live if item.plan_date == PLAN_DATE]
        monkeypatch.setattr(plan_dates, "date_now", counted)
        assert client.get(page, headers=PAGE_HEADERS).status_code == 200

    wanted = {name for item in live for name in item.plan_assignment_ids or ()}
    assert len(live) == (1 if page == HER_PAGE else 5)
    assert sorted(worked) == sorted(wanted)
