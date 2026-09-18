"""One saved plan as a page reads it: rows by assignment id, marks only on today's
working plan, and the saved text whole when the snapshot cannot be used.

No page and no model: a composition saved to a drafts table in memory and
read back, then handed to the presenter with what a view would hand it.
"""

import json
from datetime import date

from blossom.agent.compose import Composition
from blossom.plan_reading import DoneMark, anchor_for, read_plan
from blossom.plans import DailyPlan
from blossom.routes.navigation import details_href, segment
from blossom.stores.drafts import DraftRecord
from blossom.stores.project_state import Assignment
from tests.support import (
    ESSAY,
    NAMESAKE_ESSAY,
    PLAN_DATE,
    PROBLEM_SET,
    SYLLABUS,
    composed_plan,
    drafts_in_memory,
    plan_block,
)


def saved(made: Composition, **columns: str | None) -> DraftRecord:
    """The record of a composition as the drafts table keeps it, with any column a test
    wants to read differently written over afterward."""
    store = drafts_in_memory()
    try:
        store.record_waiting(
            made.draft,
            thread_id="plan:2026-08-19:abc12345",
            plan_date=PLAN_DATE,
            outcome="unsettled",
            plan_assignment_ids=made.snapshot.assignment_ids,
            plan_snapshot=made.snapshot,
        )
        for name, value in columns.items():
            store._connection.execute(f"UPDATE drafts SET {name}=?", (value,))  # noqa: S608
        store._connection.commit()
        record = store.get(made.draft.draft_id)
    finally:
        store.close()
    assert record is not None
    return record


def to_details(assignment_id: str) -> str:
    return details_href(assignment_id, return_to="today")


def test_a_structured_reading_keeps_every_row_where_it_was_and_names_it_by_id() -> None:
    """Blocks in time order, each with an id of its own from its place in the saved plan, so
    two blocks for one assignment are two rows with one link; what was put off in its saved
    order; the dates to clarify; the reviewer's notes; every reason on one line; and the
    text as composed beside it, untouched."""
    made = composed_plan()
    reading = read_plan(saved(made), reader="student", current=True, link_for=to_details)
    anchor = anchor_for(made.draft.draft_id)

    assert reading.structured
    assert not reading.unavailable
    assert reading.anchor == anchor == "plan-draft-plan-2026-08-19-abc12345"
    assert reading.title == "Plan for Wednesday, August 19, 2026"
    assert reading.intro == made.snapshot.intro
    assert [(row.dom_id, row.span, row.work.assignment_id) for row in reading.blocks] == [
        (f"{anchor}-block-1", "4:30 PM to 5:00 PM", ESSAY.assignment_id),
        (f"{anchor}-block-2", "5:15 PM to 5:45 PM", NAMESAKE_ESSAY.assignment_id),
        (f"{anchor}-block-0", "6:00 PM to 6:30 PM", ESSAY.assignment_id),
    ]
    assert reading.blocks[2].rationale == "the second half after a break"
    assert reading.blocks[0].work == reading.blocks[2].work
    assert (
        reading.blocks[0].work.href == "/student/assignments/assignment-canal-essay?return_to=today"
    )
    assert [(row.dom_id, row.work.assignment_id, row.reason) for row in reading.deferrals] == [
        (f"{anchor}-deferral-0", PROBLEM_SET.assignment_id, "not due until Monday"),
        (f"{anchor}-deferral-1", SYLLABUS.assignment_id, "ask what the date is"),
    ]
    assert reading.deferrals[1].work.due_date is None
    assert [row.work.assignment_id for row in reading.clarifications] == [
        ESSAY.assignment_id,
        PROBLEM_SET.assignment_id,
        SYLLABUS.assignment_id,
    ]
    assert reading.review is not None
    assert reading.review.findings[0].label == "order (does not pass)"
    assert reading.body == made.draft.body
    assert not reading.annotated
    assert reading.done_work == []


def test_two_assignments_with_one_title_are_two_rows_told_apart_by_more_than_their_words() -> None:
    """The essay and its namesake in another course read differently as they are, course and
    all, and each link says its own course. Two assignments alike in title, course, and due
    date each show their id as well, and each link says it."""
    twin = Assignment(
        assignment_id="assignment-canal-essay-second",
        course=ESSAY.course,
        title=ESSAY.title,
        due_date=ESSAY.due_date,
        dependencies=[],
        reported_submission_status="not_started",
    )
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            plan_block(ESSAY.assignment_id, "16:30", "17:00"),
            plan_block(twin.assignment_id, "17:00", "17:30"),
            plan_block(NAMESAKE_ESSAY.assignment_id, "17:30", "18:00"),
        ],
    )
    made = composed_plan(
        plan, assignments=[ESSAY, twin, NAMESAKE_ESSAY], noticings=[], confidence={}
    )
    reading = read_plan(saved(made), reader="family", current=False, link_for=to_details)
    first, second, namesake = (row.work for row in reading.blocks)

    assert (first.title, first.course) == (second.title, second.course)
    assert (first.reference, second.reference) == (ESSAY.assignment_id, twin.assignment_id)
    assert namesake.reference is None
    assert first.link_name == (
        "Canal Era comparison essay, World History, due August 21, 2026, assignment-canal-essay"
    )
    assert second.link_name.endswith(", assignment-canal-essay-second")
    assert namesake.link_name == "Canal Era comparison essay, English"
    assert len({first.href, second.href, namesake.href}) == 3


def test_marks_go_beside_every_row_of_the_assignment_on_todays_plan_and_on_no_other() -> None:
    """The essay is marked: both of its blocks carry the mark and its namesake does not. A
    deferral can carry one too. The same record read as history carries none, whatever is
    handed in, and the saved rows read the same either way."""
    record = saved(composed_plan())
    marks = {
        ESSAY.assignment_id: DoneMark(date(2026, 8, 19), restored_on=date(2026, 8, 20)),
        SYLLABUS.assignment_id: DoneMark(date(2026, 8, 19)),
        "assignment-not-in-the-plan": DoneMark(date(2026, 8, 19)),
    }
    current = read_plan(record, reader="student", current=True, link_for=to_details, done=marks)
    history = read_plan(record, reader="student", current=False, link_for=to_details, done=marks)

    assert [row.done is not None for row in current.blocks] == [True, False, True]
    assert current.blocks[0].done == current.blocks[2].done == marks[ESSAY.assignment_id]
    assert [row.done is not None for row in current.deferrals] == [False, True]
    assert current.annotated
    assert [work.assignment_id for work in current.done_work] == [
        ESSAY.assignment_id,
        SYLLABUS.assignment_id,
    ]
    assert not history.annotated
    assert history.done_work == []
    assert [(row.span, row.work, row.rationale) for row in history.blocks] == [
        (row.span, row.work, row.rationale) for row in current.blocks
    ]
    assert current.blocks[0].work.href is not None


def test_a_row_whose_assignment_is_gone_keeps_its_saved_label_and_gets_no_link() -> None:
    record = saved(composed_plan())
    on_record = {ESSAY.assignment_id, PROBLEM_SET.assignment_id, SYLLABUS.assignment_id}
    reading = read_plan(
        record, reader="student", current=True, link_for=to_details, on_record=on_record
    )
    gone = reading.blocks[1].work

    assert gone.assignment_id == NAMESAKE_ESSAY.assignment_id
    assert (gone.title, gone.course) == (NAMESAKE_ESSAY.title, NAMESAKE_ESSAY.course)
    assert gone.href is None
    assert gone.unavailable
    assert all(row.work.href for row in (reading.blocks[0], reading.blocks[2]))
    assert not reading.blocks[0].work.unavailable


def test_a_plan_without_a_usable_snapshot_is_read_as_its_saved_text_with_no_links_or_marks() -> (
    None
):
    """No snapshot is an earlier plan; a snapshot of another version, one that is not JSON,
    and one that lists other assignments than its draft are unavailable. Each is the saved
    text whole, and marks handed in for today's plan go nowhere."""
    made = composed_plan()
    other_version = json.dumps({**made.snapshot.model_dump(mode="json"), "version": 2})
    marks = {ESSAY.assignment_id: DoneMark(date(2026, 8, 19))}
    readings = {
        name: read_plan(record, reader="student", current=True, link_for=to_details, done=marks)
        for name, record in {
            "earlier": saved(made, plan_snapshot=None),
            "version": saved(made, plan_snapshot=other_version),
            "torn": saved(made, plan_snapshot="{not json"),
            "other ids": saved(made, plan_assignment_ids=json.dumps([ESSAY.assignment_id])),
            "no ids": saved(made, plan_assignment_ids=None),
        }.items()
    }

    assert not any(reading.structured for reading in readings.values())
    assert {name: reading.unavailable for name, reading in readings.items()} == {
        "earlier": False,
        "version": True,
        "torn": True,
        "other ids": True,
        "no ids": True,
    }
    assert all(reading.body == made.draft.body for reading in readings.values())
    assert all(reading.blocks == [] and not reading.annotated for reading in readings.values())


def test_an_address_is_made_from_values_each_escaped_where_it_goes() -> None:
    assert segment("assignment-canal-essay") == "assignment-canal-essay"
    assert segment("a/b?c#d e") == "a%2Fb%3Fc%23d%20e"
    assert segment("é") == "%C3%A9"
    made = details_href(
        "a b/c", return_to="family", plan_id="draft:plan:2026-08-19:x&y=z", week=None
    )
    assert made == (
        "/student/assignments/a%20b%2Fc"
        "?return_to=family&plan_id=draft%3Aplan%3A2026-08-19%3Ax%26y%3Dz"
    )
    assert details_href("assignment-x") == "/student/assignments/assignment-x"
