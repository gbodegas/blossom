"""Whose words an assignment's note is: the teacher's, a parent's, or hers.

Once she can add homework herself, a note on it may be her own words. The
planner and the critic are told which of the three a note is, the page says it
to whoever reads it, and the fingerprint a plan is checked against carries it,
so the same words from another hand are a change. With no note there is no
hand to name, and nothing about one can make a plan stale.
"""

import json
import uuid
from datetime import date

from blossom.agent.prompts import CRITIC_SYSTEM, PLANNER_SYSTEM, assignments_block
from blossom.noticing import (
    PLANNING_DIGEST,
    Week,
    canonical_active_input,
    planning_digest,
    read_everything,
    week_from,
)
from blossom.reconciliation import SourceChannel
from blossom.routes.parent import ASSIGNMENTS_CHANGED as THEIR_ASSIGNMENTS_CHANGED
from blossom.routes.runs import plan_graphs
from blossom.routes.student import ASSIGNMENTS_CHANGED
from blossom.stores.project_state import Assignment, AssignmentKind
from tests.support import (
    FIXTURE_WEEK,
    HER_PAGE,
    PLAN_DATE,
    Scripted,
    accepting,
    browser,
    fixture_week_plan,
    scripted_graphs,
    state_of,
)

MONDAY = date(2026, 8, 17)
BEFORE_THIS = uuid.UUID("3b9c1f52-8a47-4d06-b1e3-5c7a9d2f4e68")


def work(note: str | None, by: SourceChannel | None, name: str = "assignment-notes") -> Assignment:
    return Assignment(
        assignment_id=name,
        course="Geometry",
        title="Questions 4-8",
        due_date=date(2026, 8, 21),
        dependencies=[],
        reported_submission_status="not_started",
        kind=AssignmentKind.HOMEWORK,
        note=note,
        origins={} if by is None else {"note": by},
    )


def week_of(*assignments: Assignment) -> Week:
    return Week(assignments=list(assignments), records={}, noticings={}, statuses={})


def test_each_hand_has_a_name_and_a_note_with_no_mark_is_the_schools() -> None:
    assert work("Cite two sources.", SourceChannel.LMS).note_by == "teacher"
    assert work("Cite two sources.", SourceChannel.EMAIL).note_by == "teacher"
    assert work("Cite two sources.", None).note_by == "teacher"
    assert work("Signed on Sunday.", SourceChannel.PARENT_ENTRY).note_by == "parent"
    assert work("Heard in class.", SourceChannel.STUDENT_REPORT).note_by == "student"
    assert work(None, SourceChannel.STUDENT_REPORT).note_by is None
    assert work(None, None).note_by is None


def test_her_note_is_put_to_a_model_as_hers_and_never_as_the_teachers() -> None:
    hers = work("Heard in class, pages 12 to 14.", SourceChannel.STUDENT_REPORT)
    text = assignments_block(
        [
            hers,
            work("Cite two sources.", SourceChannel.LMS, "assignment-essay"),
            work("Signed on Sunday.", SourceChannel.PARENT_ENTRY, "assignment-syllabus"),
        ],
        {},
    )

    assert 'student_noted="Heard in class, pages 12 to 14."' in text
    assert text.count("student_noted=") == 1
    assert text.count("teacher_wrote=") == 1
    assert text.count("parent_wrote=") == 1
    assert "Heard in class" not in text.split("student_noted=")[0]
    for system in (PLANNER_SYSTEM, CRITIC_SYSTEM):
        assert "student_noted" in system


def test_the_fingerprint_carries_whose_words_a_note_is_and_nothing_about_a_note_not_there() -> None:
    """The same words from another hand are another input. With no note, a mark left on the
    record changes nothing, so metadata alone cannot make a plan stale."""
    words = "Bring the signed form."
    by_a_parent = week_of(work(words, SourceChannel.PARENT_ENTRY))
    by_her = week_of(work(words, SourceChannel.STUDENT_REPORT))
    unmarked = week_of(work(words, None))
    from_the_portal = week_of(work(words, SourceChannel.LMS))
    no_note = week_of(work(None, None))
    no_note_with_a_mark = week_of(work(None, SourceChannel.STUDENT_REPORT))

    assert planning_digest(by_a_parent) != planning_digest(by_her)
    assert planning_digest(by_her) != planning_digest(unmarked)
    assert planning_digest(unmarked) == planning_digest(from_the_portal)
    assert planning_digest(no_note) == planning_digest(no_note_with_a_mark)
    assert planning_digest(by_her) == planning_digest(
        week_of(work(words, SourceChannel.STUDENT_REPORT))
    )
    row = canonical_active_input(by_her)[0]
    assert row["note_by"] == "student"
    assert "note_by_a_parent" not in row
    assert canonical_active_input(no_note_with_a_mark)[0]["note_by"] is None


def test_the_fingerprint_has_a_namespace_of_its_own_for_this_shape() -> None:
    """A fixed constant, written down, and not the one from before: a plan fingerprinted
    under the earlier shape reads as changed once and is asked for again."""
    assert uuid.UUID("7d1e6a34-2c9b-4f58-a0d7-93b5e1c8f264") == PLANNING_DIGEST
    assert PLANNING_DIGEST != BEFORE_THIS


def test_the_page_says_whose_words_a_note_is_to_whoever_reads_it() -> None:
    with browser() as client:
        store = state_of(client).project_state
        rows = store.all_assignments()
        first, second, third = rows[0], rows[1], rows[2]
        store.upsert_assignments(
            [
                first.model_copy(
                    update={
                        "note": "Heard in class, pages 12 to 14.",
                        "origins": {**first.origins, "note": SourceChannel.STUDENT_REPORT},
                    }
                ),
                second.model_copy(
                    update={
                        "note": "Signed on Sunday.",
                        "origins": {**second.origins, "note": SourceChannel.PARENT_ENTRY},
                    }
                ),
                third.model_copy(
                    update={
                        "note": "Cite two sources.",
                        "origins": {**third.origins, "note": SourceChannel.LMS},
                    }
                ),
            ]
        )
        pages = {
            name: client.get(f"/student/assignments/{name}").text
            for name in (first.assignment_id, second.assignment_id, third.assignment_id)
        }

    assert "You wrote: <q>Heard in class, pages 12 to 14.</q>" in pages[first.assignment_id]
    assert "From the teacher" not in pages[first.assignment_id].split("Heard in class")[0][-80:]
    assert "A parent wrote: <q>Signed on Sunday.</q>" in pages[second.assignment_id]
    assert "From the teacher: <q>Cite two sources.</q>" in pages[third.assignment_id]


def as_fingerprinted_before(week: Week) -> str:
    """The week's fingerprint in the shape and the namespace from before this one."""
    rows = []
    for row in canonical_active_input(week):
        earlier = {name: value for name, value in row.items() if name != "note_by"}
        earlier["note_by_a_parent"] = row["note_by"] == "parent"
        rows.append(earlier)
    serialized = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return uuid.uuid5(BEFORE_THIS, serialized).hex


def test_a_plan_that_was_waiting_under_the_shape_before_reads_as_changed_and_asks_no_model() -> (
    None
):
    """The record did not change; the fingerprint's shape did. The waiting plan says it is
    behind on both pages, approving it is refused, and nothing calls a planner or a critic
    to make another: a new plan is made when someone asks for one."""
    planners: list[Scripted] = []  # type: ignore[type-arg]
    critics: list[Scripted] = []  # type: ignore[type-arg]
    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [fixture_week_plan()],
            lambda: [accepting()],
            planners=planners,
            critics=critics,
        )
        state = state_of(client)
        assert client.post("/student/actions/plan").status_code == 303
        waiting = state.drafts.latest_for(PLAN_DATE)
        assert waiting is not None
        fresh = [
            client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text,
            client.get("/parent").text,
        ]
        week = week_from(read_everything(state.project_state, state.project_state), PLAN_DATE)
        state.drafts._connection.execute(
            "UPDATE drafts SET inputs_digest = ? WHERE draft_id = ?",
            (as_fingerprinted_before(week), waiting.draft_id),
        )
        state.drafts._connection.commit()
        asked_before = sum(len(planner.briefs) for planner in planners)
        behind = [
            client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text,
            client.get("/parent").text,
        ]
        approved = client.post(f"/parent/approvals/{waiting.draft_id}", json={"approved": True})

    assert ASSIGNMENTS_CHANGED not in fresh[0]
    assert THEIR_ASSIGNMENTS_CHANGED not in fresh[1]
    assert ASSIGNMENTS_CHANGED in behind[0]
    assert THEIR_ASSIGNMENTS_CHANGED in behind[1]
    assert approved.status_code == 409
    # The refused approval may build a graph to look at the paused run; it sends it nothing.
    assert sum(len(planner.briefs) for planner in planners) == asked_before == 1
    assert sum(len(critic.briefs) for critic in critics) == 1
