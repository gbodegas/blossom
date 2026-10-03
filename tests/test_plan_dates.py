# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A saved plan's dates set against the record as it stands, with no page and no store.

The planner's rule is run on a composition, the composition is saved to a drafts table in
memory, and the reading a page would hold now is made by hand, so each case says exactly
what was planned and what stands. The rows are rendered through the plan's own partial
and read back as words.
"""

import re
from collections.abc import Sequence
from datetime import date
from typing import cast

import pytest

from blossom.agent.compose import compose, wording
from blossom.assignment_status import AssignmentStatus
from blossom.noticing import (
    Everything,
    expect_due_date,
    notice_due_date,
    planning_digest,
    reconcile_dates,
    week_from,
)
from blossom.plan_checks import check_plan
from blossom.plan_dates import (
    NOT_RECORDED_NOW,
    Doubt,
    Evidence,
    date_now,
    dates_now,
    dates_said,
    doubt_of,
    row_now,
)
from blossom.plan_reading import PlanReading, read_plan
from blossom.plans import DailyPlan, Deferral
from blossom.reconciliation import SourceChannel, SourceRecord, classify_confidence
from blossom.routes.navigation import details_href
from blossom.stores.drafts import DraftRecord
from blossom.stores.project_state import Assignment
from blossom.templating import page_templates
from tests.support import (
    ESSAY,
    PLAN_DATE,
    ZONE,
    drafts_in_memory,
    plan_block,
    record,
    to_details,
    words,
)

LMS, EMAIL = SourceChannel.LMS, SourceChannel.EMAIL
FAMILY, HERS = SourceChannel.PARENT_ENTRY, SourceChannel.STUDENT_REPORT
A19, A20, A21, A22, A23 = (date(2026, 8, day) for day in (19, 20, 21, 22, 23))
ESSAY_ID = ESSAY.assignment_id
OTHER_DIGEST = "0" * 32
LINK = "See what is on record now"


def essay(due: date | None) -> Assignment:
    return ESSAY.model_copy(update={"due_date": due})


def planner_says(due: date | None, claims: Sequence[SourceRecord]) -> str | None:
    """The clarification the composer writes for the essay, or ``None``."""
    item = essay(due)
    said = wording(
        assignments=[item],
        verdict=None,
        settled=True,
        noticings=[notice_due_date(expect_due_date(item), list(claims))],
        confidence={ESSAY_ID: classify_confidence(reconcile_dates(list(claims)))},
        too_much=False,
        budget_minutes=None,
    )
    return said.clarifications[0].text if said.clarifications else None


def standing(*items: Assignment) -> dict[str, AssignmentStatus]:
    return {
        item.assignment_id: AssignmentStatus(
            assignment_id=item.assignment_id, head=None, asserted=None, school={}
        )
        for item in items
    }


def reading_now(
    due: date | None, claims: Sequence[SourceRecord], *, unreadable: bool = False
) -> Everything:
    """The record as a page would read it now, with the essay on it."""
    item = essay(due)
    return Everything(
        assignments=[item],
        records={ESSAY_ID: list(claims)},
        statuses=standing(item),
        claims_unavailable=frozenset({ESSAY_ID}) if unreadable else frozenset(),
    )


def nothing_on_record() -> Everything:
    return Everything(assignments=[], records={}, statuses={})


def saved_plan(
    due: date | None,
    claims: Sequence[SourceRecord],
    *,
    digest: str | None = OTHER_DIGEST,
    deferred: bool = False,
    blocks: int = 1,
) -> DraftRecord:
    """The essay planned by the planner's own rule against ``claims``, saved as a draft."""
    item = essay(due)
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[]
        if deferred
        else [
            plan_block(ESSAY_ID, f"{16 + number}:30", f"{17 + number}:00")
            for number in range(blocks)
        ],
        deferred=[Deferral(assignment_id=ESSAY_ID, reason="later this week")] if deferred else [],
    )
    made = compose(
        draft_id="draft:plan:2026-08-19:dates",
        plan=plan,
        assignments=[item],
        verification=check_plan(plan, due_in_window=[item], zone=ZONE, requested_evening=PLAN_DATE),
        verdict=None,
        settled=True,
        noticings=[notice_due_date(expect_due_date(item), list(claims))],
        confidence={ESSAY_ID: classify_confidence(reconcile_dates(list(claims)))},
    )
    store = drafts_in_memory()
    try:
        store.record_waiting(
            made.draft,
            thread_id="plan:2026-08-19:dates",
            plan_date=PLAN_DATE,
            outcome="accepted",
            inputs_digest=digest,
            plan_assignment_ids=made.snapshot.assignment_ids,
            plan_snapshot=made.snapshot,
        )
        found = store.get(made.draft.draft_id)
    finally:
        store.close()
    assert found is not None
    return found


def to_evidence(assignment_id: str) -> str:
    return details_href(assignment_id, fragment="evidence", return_to="today")


def live_reading(record: DraftRecord, now: Everything) -> PlanReading:
    return read_plan(
        record,
        reader="student",
        current=True,
        link_for=to_details,
        evidence_for=to_evidence,
        on_record=now.ids,
        now=dates_now(now, record.plan_assignment_ids or ()),
    )


def history_reading(record: DraftRecord, now: Everything) -> PlanReading:
    return read_plan(
        record,
        reader="family",
        current=False,
        link_for=to_details,
        evidence_for=to_evidence,
        on_record=now.ids,
    )


def rendered(reading: PlanReading) -> str:
    return page_templates().get_template("plan_reading.html").render(reading=reading)


def row_lines(html: str, dom_id: str) -> tuple[str, str | None, str]:
    """A row's saved date line and current line as words, and the row whole."""
    row = re.search(rf'<li class="plan-[a-z]+[^"]*" id="{dom_id}">(.*?)</li>', html, re.S)
    assert row is not None, dom_id
    saved = re.search(r'<p class="plan-due">(.*?)</p>', row.group(1), re.S)
    now = re.search(r'<p class="plan-now">(.*?)</p>', row.group(1), re.S)
    assert saved is not None
    return words(saved.group(1)), None if now is None else words(now.group(1)), row.group(1)


# ------------------------------------------------------------- one rule for doubt

RULE_TABLE = [
    pytest.param(A21, [], None, id="dated-no-claim"),
    pytest.param(A21, [record(LMS, "2026-08-21")], None, id="one-source-agrees"),
    pytest.param(
        A21, [record(LMS, "2026-08-21"), record(EMAIL, "2026-08-21")], None, id="two-agree"
    ),
    pytest.param(
        A21,
        [record(LMS, "2026-08-20")],
        "recorded as due Aug 21, but the sources say school portal: 2026-08-20",
        id="contradicted",
    ),
    pytest.param(
        A21,
        [record(LMS, "2026-08-21"), record(FAMILY, "2026-08-22")],
        "the sources give different dates: school portal: 2026-08-21; family entry: 2026-08-22",
        id="two-channels-disagree",
    ),
    pytest.param(
        A21,
        [record(HERS, "2026-08-22")],
        "recorded as due Aug 21, but the sources say your report: 2026-08-22",
        id="her-report-contradicts",
    ),
    pytest.param(A21, [record(LMS, "Friday")], None, id="unparseable-only"),
    pytest.param(
        A21,
        [record(LMS, "2026-08-21"), record(EMAIL, "Friday")],
        None,
        id="unparseable-beside-match",
    ),
    pytest.param(A21, [record(LMS, " 2026-08-21 ")], None, id="padded"),
    pytest.param(None, [], "the date needs asking about", id="undated"),
    pytest.param(
        None,
        [record(LMS, "2026-08-21")],
        "the date needs asking about; the sources say school portal: 2026-08-21",
        id="undated-claimed",
    ),
    pytest.param(
        None,
        [record(LMS, "2026-08-21"), record(FAMILY, "2026-08-22")],
        "the date needs asking about; the sources say school portal: 2026-08-21; "
        "family entry: 2026-08-22",
        id="undated-two-claims",
    ),
    pytest.param(
        None, [record(LMS, "soon")], "the date needs asking about", id="undated-unparseable"
    ),
]


@pytest.mark.parametrize(("due", "claims", "said"), RULE_TABLE)
def test_the_composer_and_the_page_read_doubt_by_one_rule(
    due: date | None, claims: list[SourceRecord], said: str | None
) -> None:
    item = essay(due)
    noticed = notice_due_date(expect_due_date(item), claims)
    doubt = doubt_of(due, noticed, classify_confidence(reconcile_dates(claims)))
    now = date_now(item, noticed, claims, unreadable=False)

    assert planner_says(due, claims) == said
    assert (doubt is not None) is (said is not None)
    assert now.in_doubt is (said is not None)
    if said is not None:
        expected = {
            "the date needs asking about;": Doubt.UNDATED_CLAIMED,
            "the date needs asking about": Doubt.UNDATED,
            "recorded as due": Doubt.CONTRADICTED,
            "the sources give different": Doubt.DISAGREE,
        }
        assert doubt is next(kind for lead, kind in expected.items() if said.startswith(lead))


def test_a_missing_noticing_or_confidence_is_read_as_nothing_said() -> None:
    assert doubt_of(A21, None, None) is None
    assert doubt_of(None, None, None) is Doubt.UNDATED


# ------------------------------------------------------------- present evidence, typed


@pytest.mark.parametrize(
    ("due", "claims", "evidence", "readable", "unparseable"),
    [
        (
            A21,
            [record(LMS, "2026-08-21"), record(EMAIL, "2026-08-21")],
            Evidence.SUPPORT,
            (A21,),
            0,
        ),
        (A21, [], Evidence.NONE, (), 0),
        (A21, [record(FAMILY, "Friday")], Evidence.NONE, (), 1),
        (A21, [record(LMS, "2026-08-19")], Evidence.CONTRADICTED, (A19,), 0),
        (
            A21,
            [record(LMS, "2026-08-21"), record(FAMILY, "2026-08-22")],
            Evidence.DISAGREE,
            (A21, A22),
            0,
        ),
        (None, [], Evidence.UNKNOWN_NONE, (), 0),
        (None, [record(LMS, "soon")], Evidence.UNKNOWN_NONE, (), 1),
        (None, [record(LMS, "2026-08-21")], Evidence.UNKNOWN_CLAIMS, (A21,), 0),
        (A21, [record(LMS, "2026-08-21"), record(EMAIL, "Friday")], Evidence.SUPPORT, (A21,), 1),
    ],
)
def test_present_evidence_is_one_of_six_kinds(
    due: date | None,
    claims: list[SourceRecord],
    evidence: Evidence,
    readable: tuple[date, ...],
    unparseable: int,
) -> None:
    item = essay(due)
    found = date_now(item, notice_due_date(expect_due_date(item), claims), claims, unreadable=True)

    assert (found.recorded, found.evidence, found.readable) == (due, evidence, readable)
    assert found.unparseable == unparseable
    assert found.unreadable


def test_dates_are_said_with_the_year_once_unless_they_span_years() -> None:
    assert dates_said([A19]) == "August 19, 2026"
    assert dates_said([A21, A22]) == "August 21 and August 22, 2026"
    assert dates_said([A21, A22, A23]) == "August 21, August 22 and August 23, 2026"
    assert (
        dates_said([date(2026, 12, 31), date(2027, 1, 2)])
        == "December 31, 2026 and January 2, 2027"
    )


# ------------------------------------------------------------- every case, row by row

PLANNED_CUE = [record(LMS, "2026-08-21"), record(FAMILY, "2026-08-22")]
CUED_A = f"Recorded due August 21, 2026. Check this date. {LINK}"
PLAIN_A = "Recorded due August 21, 2026."
UNDATED_A = f"No due date on record. Check this date. {LINK}"
X = "A claim about this date cannot be read right now, so what the sources say is not fully known."
U = "Some source information cannot be read as a date. Check the current record."
MATCH = "The readable sources now match the recorded due date."
UNSUPPORTED = "No readable source currently supports the recorded due date."
LIMIT = "This saved plan does not keep enough detail to compare every change in its sources."

CASES = [
    # row, saved due, claims when planned, due now, claims now, unreadable, digest equal,
    # line A, line B, P1
    pytest.param(
        A21,
        [record(FAMILY, "2026-08-20")],
        A21,
        [record(FAMILY, "2026-08-19")],
        False,
        False,
        CUED_A,
        "As things stand: The sources give August 19, 2026, not the recorded due date.",
        True,
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
        True,
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
        True,
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
        True,
        id="source-now-unparseable",
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
        True,
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
        True,
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
        False,
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
        True,
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
        True,
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
        True,
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
        True,
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
        True,
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
        True,
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
        False,
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
        True,
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
        False,
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
        True,
        id="absent-date-stays-unknown",
    ),
    pytest.param(
        A21,
        [record(LMS, "2026-08-21")],
        A21,
        [record(LMS, "2026-08-21")],
        False,
        False,
        PLAIN_A,
        None,
        False,
        id="routine-single-source",
    ),
    pytest.param(
        A20,
        [],
        A20,
        [],
        False,
        False,
        "Recorded due August 20, 2026.",
        None,
        False,
        id="record-only-due-the-next-day",
    ),
    pytest.param(
        A20,
        [record(LMS, "2026-08-19")],
        A20,
        [record(LMS, "2026-08-19")],
        False,
        False,
        f"Recorded due August 20, 2026. Check this date. {LINK}",
        "As things stand: The sources give August 19, 2026, not the recorded due date.",
        True,
        id="real-contradiction-due-the-next-day-fingerprint-changed",
    ),
    pytest.param(
        A20,
        [record(LMS, "2026-08-19")],
        A20,
        [record(LMS, "2026-08-19")],
        False,
        True,
        f"Recorded due August 20, 2026. Check this date. {LINK}",
        None,
        False,
        id="real-contradiction-due-the-next-day-fingerprint-unchanged",
    ),
    pytest.param(
        A21,
        [record(LMS, "2026-08-21")],
        A21,
        [record(LMS, "2026-08-21"), record(EMAIL, "Friday")],
        False,
        False,
        PLAIN_A,
        f"As things stand: {U} {LINK}",
        True,
        id="routine-an-unparseable-value-now",
    ),
    pytest.param(
        A21,
        [record(LMS, "2026-08-21")],
        A21,
        [record(LMS, "2026-08-21")],
        True,
        False,
        PLAIN_A,
        f"As things stand: {X} {LINK}",
        False,
        id="routine-an-unreadable-claim",
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
        False,
        id="nothing-changed-but-a-claim-unreadable",
    ),
    pytest.param(
        A21,
        [record(LMS, "2026-08-21")],
        A21,
        [record(LMS, "2026-08-21")],
        False,
        True,
        PLAIN_A,
        None,
        False,
        id="nothing-changed",
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
        True,
        id="cued-match-beside-an-unparseable-value",
    ),
]


@pytest.mark.parametrize(
    ("saved_due", "then", "due", "claims", "unreadable", "equal", "line_a", "line_b", "limit"),
    CASES,
)
def test_each_case_shows_its_saved_line_and_its_current_line(
    saved_due: date | None,
    then: list[SourceRecord],
    due: date | None,
    claims: list[SourceRecord],
    unreadable: bool,
    equal: bool,
    line_a: str,
    line_b: str | None,
    limit: bool,
) -> None:
    now = reading_now(due, claims, unreadable=unreadable)
    digest = planning_digest(week_from(now, PLAN_DATE)) if equal else OTHER_DIGEST
    reading = live_reading(saved_plan(saved_due, then, digest=digest), now)
    html = rendered(reading)
    saved, current, row = row_lines(html, reading.blocks[0].dom_id)

    assert (saved, current) == (line_a, line_b)
    assert (LIMIT in html) is limit
    assert html.count(LIMIT) <= 1
    assert row.count("#evidence") == (1 if LINK in f"{line_a} {line_b}" else 0)
    assert "match" not in (current or "") or not (unreadable or "Friday" in str(claims))


def test_a_row_whose_assignment_is_gone_keeps_its_saved_line_and_says_so() -> None:
    reading = live_reading(saved_plan(A21, PLANNED_CUE), nothing_on_record())
    saved, current, row = row_lines(rendered(reading), reading.blocks[0].dom_id)

    assert (
        saved
        == "Recorded due August 21, 2026. Check this date. This assignment is not on record now."
    )
    assert current is None
    assert "<a " not in row
    assert (reading.blocks[0].work.href, reading.blocks[0].work.evidence) == (None, None)
    assert reading.blocks[0].now is None
    assert LIMIT not in rendered(reading)


def test_a_date_absent_from_the_record_is_said_only_from_a_record_that_reads_so() -> None:
    """A readable record without the planned date says so; a missing assignment says nothing."""
    undated = live_reading(saved_plan(A21, []), reading_now(None, []))
    gone = live_reading(saved_plan(A21, []), nothing_on_record())
    removed = (
        "When this plan was made, a due date was recorded. There is no due date on record now."
    )

    assert removed == NOT_RECORDED_NOW
    assert undated.blocks[0].now is not None
    assert undated.blocks[0].now.words[0] == removed
    assert gone.blocks[0].now is None
    assert removed not in rendered(gone)


def test_the_labels_are_strong_and_the_link_names_its_assignment() -> None:
    now = reading_now(A22, [record(LMS, "2026-08-21")])
    reading = live_reading(saved_plan(A21, [record(LMS, "2026-08-21")]), now)
    _, _, row = row_lines(rendered(reading), reading.blocks[0].dom_id)

    assert '<p class="plan-now"><strong>As things stand:</strong> Since' in row
    assert "<strong>Check this date.</strong>" in row
    assert (
        '<a class="assignment-link" '
        'href="/student/assignments/assignment-canal-essay?return_to=today" '
        'aria-label="Canal Era comparison essay, World History">'
    ) in row
    assert (
        '<a class="assignment-link details-link" '
        'href="/student/assignments/assignment-canal-essay?return_to=today#evidence" '
        'aria-label="See what is on record now for Canal Era comparison essay, World History">'
        "See what is on record now</a>"
    ) in row


# ------------------------------------------------------------- the parts and their gates


@pytest.mark.parametrize("changed", [True, False])
@pytest.mark.parametrize("cued", [True, False])
@pytest.mark.parametrize("moved", [True, False])
@pytest.mark.parametrize("doubt", [True, False])
def test_present_evidence_shows_only_when_the_fingerprint_moved_and_the_row_calls_for_it(
    changed: bool, cued: bool, moved: bool, doubt: bool
) -> None:
    """fingerprint_changed AND (in_doubt_now OR saved_cue OR recorded_date_changed)."""
    due = A21
    claims = [record(LMS, "2026-08-19" if doubt else "2026-08-21")]
    saved_due = A20 if moved else A21
    item = essay(due)
    now = date_now(item, notice_due_date(expect_due_date(item), claims), claims, unreadable=False)
    found = row_now(saved_due, cued, now, inputs_changed=changed)
    said = [] if found is None else list(found.words)
    evidence = [
        line for line in said if line.startswith(("The sources", "The readable", "No readable"))
    ]
    recorded = [line for line in said if line.startswith("Since this plan was made")]

    assert bool(evidence) is (changed and (doubt or cued or moved))
    assert bool(recorded) is moved
    assert found is None or found.restates is bool(evidence)
    if found is not None:
        assert found.check is (doubt and (not cued or moved))


@pytest.mark.parametrize("changed", [True, False])
def test_what_cannot_be_read_shows_whatever_the_fingerprint_says(changed: bool) -> None:
    claims = [record(LMS, "2026-08-21"), record(EMAIL, "Friday")]
    item = essay(A21)
    now = date_now(item, notice_due_date(expect_due_date(item), claims), claims, unreadable=True)
    found = row_now(A21, True, now, inputs_changed=changed)

    assert found is not None
    assert X in found.words
    assert (U in found.words) is changed
    assert MATCH not in found.words


def test_a_recorded_date_change_is_said_the_same_whatever_the_fingerprint() -> None:
    item = essay(A22)
    now = date_now(item, notice_due_date(expect_due_date(item), []), [], unreadable=False)
    changed = row_now(A21, False, now, inputs_changed=True)
    same = row_now(A21, False, now, inputs_changed=False)

    assert changed is not None
    assert same is not None
    moved = "Since this plan was made, the recorded due date changed to August 22, 2026."
    assert changed.words == (moved, UNSUPPORTED)
    assert same.words == (moved,)


def test_no_line_says_a_date_was_resolved_confirmed_or_left_alone() -> None:
    lines: list[str] = []
    for case in CASES:
        saved_due, then, due, claims, unreadable, equal = cast(
            "tuple[date | None, list[SourceRecord], date | None, list[SourceRecord], bool, bool]",
            case.values[:6],
        )
        now = reading_now(due, claims, unreadable=unreadable)
        digest = planning_digest(week_from(now, PLAN_DATE)) if equal else OTHER_DIGEST
        lines.append(rendered(live_reading(saved_plan(saved_due, then, digest=digest), now)))
    said = words(" ".join(lines)).lower()

    for never in ("resolved", "confirmed", "no source gives another", "settled", "verified"):
        assert never not in said


# ------------------------------------------------------------- repeats, deferrals, history


def test_repeated_blocks_of_one_assignment_show_one_label_and_one_line_each() -> None:
    now = reading_now(A21, [record(FAMILY, "2026-08-19")])
    reading = live_reading(saved_plan(A21, [record(FAMILY, "2026-08-20")], blocks=2), now)
    html = rendered(reading)
    first = row_lines(html, reading.blocks[0].dom_id)
    second = row_lines(html, reading.blocks[1].dom_id)

    assert (
        first[:2]
        == second[:2]
        == (
            CUED_A,
            "As things stand: The sources give August 19, 2026, not the recorded due date.",
        )
    )
    rows_shown = html[: html.index('<details class="steps plan-original">')]
    assert reading.blocks[0].now is reading.blocks[1].now
    assert rows_shown.count("Dates needing clarification") == 1
    assert rows_shown.count("but the sources say family entry: 2026-08-20") == 1
    assert html.count(LIMIT) == 1


def test_a_deferral_takes_the_same_lines_under_the_new_heading() -> None:
    now = reading_now(A21, [record(FAMILY, "2026-08-19")])
    reading = live_reading(saved_plan(A21, [record(FAMILY, "2026-08-20")], deferred=True), now)
    html = rendered(reading)

    assert row_lines(html, reading.deferrals[0].dom_id)[:2] == (
        CUED_A,
        "As things stand: The sources give August 19, 2026, not the recorded due date.",
    )
    assert (
        "Not in this evening&#39;s plan</h3>" in html or "Not in this evening's plan</h3>" in html
    )
    assert "Saved for another day" not in html


def test_a_plan_shown_as_history_carries_its_labels_and_no_current_line() -> None:
    now = reading_now(A22, [record(LMS, "2026-08-19")], unreadable=True)
    reading = history_reading(saved_plan(A21, PLANNED_CUE), now)
    html = rendered(reading)

    assert not reading.live
    assert row_lines(html, reading.blocks[0].dom_id)[:2] == (CUED_A, None)
    assert "As things stand" not in html
    assert LIMIT not in html
    assert "This is the saved plan." in html


def test_a_text_reading_has_no_label_date_line_or_current_line() -> None:
    record_ = saved_plan(A21, PLANNED_CUE)
    text_only = record_.model_copy(update={"plan_snapshot": None})
    reading = live_reading(text_only, reading_now(A22, []))
    html = rendered(reading)

    assert not reading.structured
    for absent in ("Check this date", "Recorded due", "As things stand", LIMIT, "#evidence"):
        assert absent not in html


def test_a_plan_with_no_fingerprint_is_never_read_as_unchanged() -> None:
    now = reading_now(A21, PLANNED_CUE)
    no_digest = live_reading(saved_plan(A21, PLANNED_CUE, digest=None), now)
    equal = live_reading(
        saved_plan(A21, PLANNED_CUE, digest=planning_digest(week_from(now, PLAN_DATE))), now
    )

    assert no_digest.blocks[0].now is not None
    assert equal.blocks[0].now is None
