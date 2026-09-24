"""The school's instructions that apply, put to the planner and the critic, and carried by the
fingerprint a plan is checked against. Those said before and any waiting for review are
neither: a plan is made from what applies, and only a change in what applies is a change.
"""

import uuid
from datetime import UTC, date, datetime

from blossom.agent.prompts import (
    CRITIC_SYSTEM,
    PLANNER_SYSTEM,
    assignments_block,
    critic_brief,
    planner_brief,
)
from blossom.noticing import (
    PLANNING_DIGEST,
    Week,
    canonical_active_input,
    planning_digest,
    read_everything,
    week_from,
)
from blossom.plan_checks import PlanVerification
from blossom.reconciliation import SourceChannel
from blossom.routes.parent import ASSIGNMENTS_CHANGED as THEIR_ASSIGNMENTS_CHANGED
from blossom.routes.runs import plan_graphs
from blossom.routes.student import ASSIGNMENTS_CHANGED
from blossom.school_instructions import (
    InstructionChoice,
    InstructionSeen,
    InstructionState,
    SchoolInstruction,
    standing_of,
)
from blossom.stores.project_state import Assignment, AssignmentKind
from tests.support import (
    ESSAY_ID,
    FIXTURE_WEEK,
    HER_PAGE,
    PLAN_DATE,
    Scripted,
    accepting,
    browser,
    fixture_week_plan,
    human_text,
    scripted_graphs,
    state_of,
)

A = "Outline three causes before drafting."
B = "Compare two canals in the conclusion."
C = "Bring the map handout."
TYPED = "Ask about the library pass."
NAME = "assignment-instructions"
NOW = datetime(2026, 8, 19, 20, 0, tzinfo=UTC)
TODAY = date(2026, 8, 19)
SHAPE_BEFORE = uuid.UUID("7d1e6a34-2c9b-4f58-a0d7-93b5e1c8f264")


def work(note: str | None = None, by: SourceChannel | None = None) -> Assignment:
    return Assignment(
        assignment_id=NAME,
        course="World History",
        title="Canal essay",
        due_date=date(2026, 8, 21),
        dependencies=[],
        reported_submission_status="not_started",
        kind=AssignmentKind.HOMEWORK,
        note=note,
        origins={} if by is None else {"note": by},
    )


def instruction(
    sequence: int, text: str, state: InstructionState, revision: int = 1
) -> SchoolInstruction:
    return SchoolInstruction(
        sequence=sequence,
        assignment_id=NAME,
        text=text,
        channel=SourceChannel.LMS,
        card="assigned",
        card_day=TODAY,
        first_seen_at=NOW,
        first_seen_on=TODAY,
        imported_by="parent",
        state=state,
        settled_by=None,
        settled_at=None,
        settled_on=None,
        revision=revision,
    )


def week_with(*kept: SchoolInstruction, row: Assignment | None = None) -> Week:
    item = row or work()
    return Week(
        assignments=[item],
        records={},
        noticings={},
        statuses={},
        instructions={NAME: standing_of(kept)} if kept else {},
    )


def briefs(week: Week) -> list[str]:
    """What the planner and the critic are handed about the week's work."""
    applying = {name: standing.texts for name, standing in week.instructions.items()}
    planner = planner_brief(
        plan_date=PLAN_DATE,
        zone="America/New_York",
        budget_minutes=90,
        assignments=week.active(),
        confidence={},
        support_rules=[],
        reflections=[],
        feedback=[],
        round_number=1,
        school_instructions=applying,
    )
    critic = critic_brief(
        plan_date=PLAN_DATE,
        zone="America/New_York",
        budget_minutes=90,
        assignments=week.active(),
        confidence={},
        support_rules=[],
        reflections=[],
        plan=fixture_week_plan(),
        verification=PlanVerification(outcomes={}),
        school_instructions=applying,
    )
    return [human_text(planner), human_text(critic)]


def test_each_instruction_that_applies_is_put_once_in_the_one_order_and_nothing_else_is() -> None:
    week = week_with(
        instruction(1, C, "current"),
        instruction(2, B, "history", 2),
        instruction(3, A, "current", 2),
        instruction(4, "Waiting words.", "awaiting", 3),
        row=work(TYPED, SourceChannel.PARENT_ENTRY),
    )

    first, second = sorted([A, C])
    for text in briefs(week):
        assert text.count(f'teacher_wrote="{first}"') == 1
        assert text.count(f'teacher_wrote_2="{second}"') == 1
        assert text.count(A) == text.count(C) == 1
        assert B not in text
        assert "Waiting words." not in text
        assert text.count(f'parent_wrote="{TYPED}"') == 1


def test_one_instruction_reads_as_a_moved_note_did() -> None:
    """A single instruction that applies is put as the teacher's note always was, so a
    record moved at the upgrade gives the planner the same words under the same name."""
    moved = assignments_block([work()], {}, school_instructions={NAME: (A,)})
    before = assignments_block([work(A, SourceChannel.LMS)], {})

    assert moved == before


def test_the_system_prompts_say_there_can_be_several_and_their_order_means_nothing() -> None:
    for system in (PLANNER_SYSTEM, CRITIC_SYSTEM):
        assert "teacher_wrote_2" in system
        assert "order means nothing" in system


def test_only_a_change_in_what_applies_changes_the_fingerprint() -> None:
    applies = week_with(instruction(1, A, "current"))
    with_history = week_with(instruction(1, A, "current"), instruction(2, B, "history", 2))
    with_waiting = week_with(instruction(1, A, "current"), instruction(2, B, "awaiting", 2))
    another = week_with(instruction(1, A, "history", 2), instruction(2, B, "current", 2))
    reordered = week_with(instruction(1, C, "current"), instruction(2, A, "current", 2))
    same_set = week_with(instruction(1, A, "current"), instruction(2, C, "current", 2))

    assert planning_digest(applies) == planning_digest(with_history)
    assert planning_digest(applies) == planning_digest(with_waiting)
    assert planning_digest(applies) != planning_digest(another)
    assert planning_digest(reordered) == planning_digest(same_set)
    row = canonical_active_input(same_set)[0]
    assert row["school_instructions"] == sorted([A, C])
    assert canonical_active_input(week_with())[0]["school_instructions"] == []


def test_the_fingerprint_has_a_namespace_of_its_own_for_this_shape() -> None:
    assert uuid.UUID("6f1ef033-6648-4683-b6e9-1dd419f420b5") == PLANNING_DIGEST
    assert PLANNING_DIGEST != SHAPE_BEFORE


def test_a_change_in_what_applies_makes_a_waiting_plan_behind_and_asks_no_model() -> None:
    """A plan waits; a parent chooses another instruction to apply. The plan says it is
    behind on both pages, approving it is refused, and no planner or critic is asked."""
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
        store = state.project_state
        store.settle_school_instructions(
            ESSAY_ID,
            [InstructionSeen(A, SourceChannel.LMS)],
            None,
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )
        assert client.post("/student/actions/plan").status_code == 303
        waiting = state.drafts.latest_for(PLAN_DATE)
        assert waiting is not None
        brief = planners[0].briefs[0]
        store.settle_school_instructions(
            ESSAY_ID,
            [InstructionSeen(B, SourceChannel.LMS)],
            InstructionChoice(1, (A, B), frozenset({A})),
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )
        kept_as_history = [
            client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text,
            client.get("/parent").text,
        ]
        store.settle_school_instructions(
            ESSAY_ID,
            [],
            InstructionChoice(2, (A, B), frozenset({B})),
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )
        behind = [
            client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text,
            client.get("/parent").text,
        ]
        approved = client.post(f"/parent/approvals/{waiting.draft_id}", json={"approved": True})
        week = week_from(read_everything(store, store), PLAN_DATE)

    assert f'teacher_wrote="{A}"' in human_text(brief)
    assert ASSIGNMENTS_CHANGED not in kept_as_history[0]
    assert THEIR_ASSIGNMENTS_CHANGED not in kept_as_history[1]
    assert ASSIGNMENTS_CHANGED in behind[0]
    assert THEIR_ASSIGNMENTS_CHANGED in behind[1]
    assert approved.status_code == 409
    assert week.instructions[ESSAY_ID].texts == (B,)
    assert sum(len(planner.briefs) for planner in planners) == 1
    assert sum(len(critic.briefs) for critic in critics) == 1


def test_a_run_tells_both_models_only_what_applies() -> None:
    """A plan made through the graph: the essay carries one instruction that applies, one
    said before, and one waiting for review. Both models are told the first, and only it."""
    planners: list[Scripted] = []  # type: ignore[type-arg]
    critics: list[Scripted] = []  # type: ignore[type-arg]
    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [fixture_week_plan()],
            lambda: [accepting()],
            planners=planners,
            critics=critics,
        )
        store = state_of(client).project_state
        store.settle_school_instructions(
            ESSAY_ID,
            [InstructionSeen(A, SourceChannel.LMS), InstructionSeen(B, SourceChannel.LMS)],
            InstructionChoice(0, (A, B), frozenset({A})),
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )
        # A school note found in the old field after the upgrade, as the startup rule keeps it.
        with store._lock, store._writing():
            store._insert_instruction_locked(
                ESSAY_ID,
                InstructionSeen(C, SourceChannel.EMAIL),
                "awaiting",
                2,
                imported_by=None,
                settled_by=None,
                now=None,
                today=None,
            )
        assert client.post("/student/actions/plan").status_code == 303

    told = [human_text(planners[0].briefs[0]), human_text(critics[0].briefs[0])]
    for text in told:
        assert text.count(f'teacher_wrote="{A}"') == 1
        assert B not in text
        assert C not in text
        assert "teacher_wrote_2" not in text
