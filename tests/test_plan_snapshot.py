"""The plan saved as data beside its text: composed once, written whole, read one record
at a time.

Synthetic assignments and no model: what a composition keeps, that the text is
the text it always was, that a bundle is written whole or not at all and never
written over once it is on the pages, that a file from before gains the column
and keeps its drafts as they were, and what a reader makes of a snapshot it
cannot use.
"""

import json
import logging
import pathlib
import sqlite3
from datetime import UTC, date, datetime, time

import pytest
from pydantic import ValidationError

from blossom.agent.compose import Composition, MissingPlanMetadata, compose, compose_draft
from blossom.agent.steps import StepRecord
from blossom.drafts import Draft, DraftStatus
from blossom.heuristic_relevance import Criterion, CriticVerdict, Judgment
from blossom.noticing import Noticing, Verdict
from blossom.plan_checks import check_plan
from blossom.plan_snapshot import (
    SNAPSHOT_VERSION,
    PlanSnapshot,
    SavedAssignment,
    SnapshotReading,
    read_snapshot,
)
from blossom.plans import DailyPlan, Deferral, PlanBlock
from blossom.reconciliation import SourceConfidence
from blossom.stores.drafts import DraftsStore, IncoherentBundle, IncompatibleReplay
from blossom.stores.project_state import Assignment
from tests.support import (
    ESSAY,
    PLAN_DATE,
    PROBLEM_SET,
    ZONE,
    drafts_in_memory,
    finding,
    fixture_clock,
)

CREATED = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)
SYLLABUS = Assignment(
    assignment_id="assignment-signed-syllabus",
    course="Geometry",
    title="Syllabus, signed",
    due_date=None,
    dependencies=[],
    reported_submission_status="not_started",
)
OTHER_ESSAY = Assignment(
    assignment_id="assignment-canal-essay-english",
    course="English",
    title="Canal Era comparison essay",
    due_date=date(2026, 8, 21),
    dependencies=[],
    reported_submission_status="not_started",
)


def a_block(assignment_id: str, start: str, end: str, why: str = "while it is fresh") -> PlanBlock:
    return PlanBlock(
        assignment_id=assignment_id,
        starts_at=time.fromisoformat(start),
        ends_at=time.fromisoformat(end),
        rationale=why,
    )


def two_sittings() -> DailyPlan:
    """The essay in two sittings, its namesake in one, the problem set put off, and the
    undated syllabus put off too."""
    return DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            a_block(ESSAY.assignment_id, "18:00", "18:30", "the second half\nafter a break"),
            a_block(ESSAY.assignment_id, "16:30", "17:00", "the outline first"),
            a_block(OTHER_ESSAY.assignment_id, "17:15", "17:45"),
        ],
        deferred=[
            Deferral(assignment_id=PROBLEM_SET.assignment_id, reason="not due until Monday"),
            Deferral(assignment_id=SYLLABUS.assignment_id, reason="ask what the date is"),
        ],
    )


WINDOW = [ESSAY, OTHER_ESSAY, PROBLEM_SET, SYLLABUS]


def contested() -> list[Noticing]:
    """The portal gives the problem set another date than the record's."""
    return [
        Noticing(
            assignment_id=PROBLEM_SET.assignment_id,
            expected=PROBLEM_SET.due_date,
            observed=("LMS says 2026-08-26",),
            spoken=("the school portal says August 26",),
            observed_dates=(date(2026, 8, 26),),
            verdict=Verdict.CONTRADICTED,
        )
    ]


def dissent() -> CriticVerdict:
    """One criterion judged and failed, the rest not considered."""
    return CriticVerdict(
        findings=[finding(Judgment.FAILS, Criterion.ORDER, "the hard one\tcomes  late")]
    )


def composed(plan: DailyPlan | None = None, **over: object) -> Composition:
    plan = plan or two_sittings()
    given: dict[str, object] = {
        "draft_id": "draft:plan:2026-08-19:abc12345",
        "plan": plan,
        "assignments": WINDOW,
        "verification": check_plan(plan, due_in_window=WINDOW, zone=ZONE),
        "verdict": dissent(),
        "settled": False,
        "noticings": contested(),
        "confidence": {ESSAY.assignment_id: SourceConfidence.SOURCES_DISAGREE},
        "too_much": True,
        "budget_minutes": 45,
    }
    given.update(over)
    return compose(**given)  # type: ignore[arg-type]


# ------------------------------------------------------------- one composition, two things


def test_a_composition_keeps_the_plan_the_assignments_it_names_and_every_sentence() -> None:
    """Repeated blocks, a namesake, a deferral, undated work, a contested date, a shorter
    evening, a review that did not settle, and criteria the reviewer skipped: the snapshot
    keeps the plan in its own order with every field, the frozen title, course, and due
    date of exactly the assignments it speaks about, and the sentences as the text says
    them; it goes to JSON and comes back the same."""
    made = composed()
    snapshot = made.snapshot
    back = PlanSnapshot.model_validate_json(snapshot.model_dump_json())

    assert back == snapshot
    assert snapshot.version == SNAPSHOT_VERSION
    assert snapshot.plan == two_sittings()
    assert [block.starts_at for block in snapshot.plan.blocks] == [
        time(18, 0),
        time(16, 30),
        time(17, 15),
    ]
    assert snapshot.assignment_ids == sorted(item.assignment_id for item in WINDOW)
    assert list(snapshot.assignments) == snapshot.assignment_ids
    assert snapshot.assignments[ESSAY.assignment_id] == SavedAssignment(
        title="Canal Era comparison essay", course="World History", due_date=date(2026, 8, 21)
    )
    assert snapshot.assignments[SYLLABUS.assignment_id].due_date is None
    assert snapshot.intro == [
        "You said today was too much, so this plan is kept to 45 minutes.",
        "The reviewer did not settle on this plan. Its notes are at the end.",
    ]
    assert [(item.assignment_id, item.text) for item in snapshot.clarifications] == [
        (ESSAY.assignment_id, "the sources give different dates: "),
        (
            PROBLEM_SET.assignment_id,
            "recorded as due Aug 24, but the sources say the school portal says August 26",
        ),
        (SYLLABUS.assignment_id, "the date needs asking about"),
    ]
    assert snapshot.review is not None
    assert len(snapshot.review.intro) == 1
    assert snapshot.review.intro[0].startswith("The reviewer did not consider: ")
    assert [(item.label, item.text) for item in snapshot.review.findings] == [
        ("order (does not pass)", "the hard one comes late")
    ]


def test_the_text_is_the_text_it_always_was_and_says_what_the_snapshot_keeps() -> None:
    """The text of a composition is the text alone composed from the same inputs, line for
    line, and every sentence the snapshot keeps is a line of it."""
    made = composed()
    plan = two_sittings()
    alone = compose_draft(
        draft_id=made.draft.draft_id,
        plan=plan,
        assignments=WINDOW,
        verification=check_plan(plan, due_in_window=WINDOW, zone=ZONE),
        verdict=dissent(),
        settled=False,
        noticings=contested(),
        confidence={ESSAY.assignment_id: SourceConfidence.SOURCES_DISAGREE},
        too_much=True,
        budget_minutes=45,
    )
    lines = made.draft.body.split("\n")

    assert made.draft.body == alone.body
    assert made.draft.draft_id == "draft:plan:2026-08-19:abc12345"
    assert lines[0] == "Plan for Wednesday, August 19, 2026"
    assert lines[1:3] == made.snapshot.intro
    assert lines.index(
        "4:30 PM to 5:00 PM, set aside for Canal Era comparison essay (World History, due Aug 21)"
    ) < lines.index(
        "6:00 PM to 6:30 PM, set aside for Canal Era comparison essay (World History, due Aug 21)"
    )
    assert "    the second half after a break" in lines
    assert "- Syllabus, signed (Geometry, no due date on record): ask what the date is" in lines
    assert (
        "- Syllabus, signed (Geometry, no due date on record): the date needs asking about" in lines
    )
    assert "- order (does not pass): the hard one comes late" in lines
    assert made.snapshot.review is not None
    assert f"- {made.snapshot.review.intro[0]}" in lines


def test_an_evening_with_nothing_scheduled_and_no_review_is_a_snapshot_too() -> None:
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        deferred=[
            Deferral(assignment_id=ESSAY.assignment_id, reason="tomorrow"),
            Deferral(assignment_id=PROBLEM_SET.assignment_id, reason="Monday"),
        ],
    )
    made = compose(
        draft_id="draft:quiet",
        plan=plan,
        assignments=[ESSAY, PROBLEM_SET],
        verification=check_plan(plan, due_in_window=[ESSAY, PROBLEM_SET], zone=ZONE),
        verdict=None,
        settled=True,
    )

    assert "Nothing is scheduled tonight." in made.draft.body
    assert made.snapshot.plan.blocks == []
    assert (made.snapshot.intro, made.snapshot.clarifications, made.snapshot.review) == (
        [],
        [],
        None,
    )
    assert made.snapshot.assignment_ids == sorted([ESSAY.assignment_id, PROBLEM_SET.assignment_id])


def test_a_plan_that_names_work_the_run_did_not_read_composes_nothing() -> None:
    """There is no title to save beside such an assignment, so no draft comes of it; the text
    alone, for a caller with nothing to save, still names it by its id."""
    plan = DailyPlan(plan_date=PLAN_DATE, blocks=[a_block("assignment-nowhere", "16:30", "17:00")])
    checked = check_plan(plan, due_in_window=[ESSAY], zone=ZONE)

    with pytest.raises(MissingPlanMetadata, match="assignment-nowhere"):
        compose(
            draft_id="draft:x",
            plan=plan,
            assignments=[ESSAY],
            verification=checked,
            verdict=None,
            settled=True,
        )
    alone = compose_draft(
        draft_id="draft:x",
        plan=plan,
        assignments=[],
        verification=checked,
        verdict=None,
        settled=True,
    )

    assert "set aside for assignment-nowhere" in alone.body
    assert not checked.passed


def test_a_snapshot_that_does_not_agree_with_itself_is_not_one() -> None:
    whole = composed().snapshot.model_dump(mode="json")

    def broken(**over: object) -> dict[str, object]:
        return {**whole, **over}

    for version in (2, True, "1", 1.0, None):
        with pytest.raises(ValidationError):
            PlanSnapshot.model_validate(broken(version=version))
    fewer = {k: v for k, v in whole["assignments"].items() if k != SYLLABUS.assignment_id}
    with pytest.raises(ValidationError, match="not the ones the plan speaks about"):
        PlanSnapshot.model_validate(broken(assignments=fewer))
    more = {**whole["assignments"], "assignment-extra": {"title": "x", "course": "y"}}
    with pytest.raises(ValidationError, match="not the ones the plan speaks about"):
        PlanSnapshot.model_validate(broken(assignments=more))
    with pytest.raises(ValidationError, match="does not speak about"):
        PlanSnapshot.model_validate(
            broken(clarifications=[{"assignment_id": "assignment-extra", "text": "ask"}])
        )
    with pytest.raises(ValidationError):
        PlanSnapshot.model_validate(broken(stray="field"))


# ------------------------------------------------------------- written whole, or not at all


def save(store: DraftsStore, made: Composition, **over: object) -> None:
    given: dict[str, object] = {
        "thread_id": "plan:2026-08-19:abc12345",
        "plan_date": PLAN_DATE,
        "outcome": "unsettled",
        "too_much": True,
        "inputs_digest": "digest-1",
        "plan_assignment_ids": made.snapshot.assignment_ids,
        "plan_snapshot": made.snapshot,
    }
    given.update(over)
    store.record_waiting(made.draft, **given)  # type: ignore[arg-type]


def test_a_bundle_is_saved_together_and_read_back_after_a_restart(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "blossom.sqlite3"
    made = composed()
    store = DraftsStore.open(path, fixture_clock())
    try:
        save(store, made)
        store.publish(made.draft.draft_id)
    finally:
        store.close()
    again = DraftsStore.open(path, fixture_clock())
    try:
        record = again.get(made.draft.draft_id)
    finally:
        again.close()

    assert record is not None
    assert record.body == made.draft.body
    assert record.plan_assignment_ids == made.snapshot.assignment_ids
    assert record.plan_snapshot == made.snapshot.model_dump_json()
    reading = read_snapshot(
        record.draft_id,
        record.plan_snapshot,
        plan_date=record.plan_date,
        plan_assignment_ids=record.plan_assignment_ids,
    )
    assert reading.snapshot == made.snapshot
    assert not reading.unavailable


def test_a_bundle_whose_parts_disagree_is_refused_before_anything_is_written() -> None:
    made = composed()
    store = drafts_in_memory()
    try:
        with pytest.raises(IncoherentBundle, match="another|for 2026"):
            save(store, made, plan_date=date(2026, 8, 20))
        with pytest.raises(IncoherentBundle, match="other assignments"):
            save(store, made, plan_assignment_ids=[ESSAY.assignment_id])
        with pytest.raises(IncoherentBundle, match="other assignments"):
            save(store, made, plan_assignment_ids=None)
        nothing = store.get(made.draft.draft_id)
        runs = store.runs_without_a_draft()
    finally:
        store.close()

    assert nothing is None
    assert runs == []


@pytest.mark.parametrize("fault", ["insert", "steps", "commit"])
def test_a_save_that_fails_leaves_no_part_of_the_bundle_and_displaces_nothing(
    tmp_path: pathlib.Path, fault: str
) -> None:
    """The draft's insert is refused, the run's steps are refused after the draft went in, or
    the commit itself is refused by a reader on a second connection: each time nothing of
    the draft, its snapshot, or its run is kept, no transaction is left open, and the plan
    already on the pages is still the evening's. A save after the fault lands whole."""
    path = tmp_path / "blossom.sqlite3"
    store = DraftsStore.open(path, fixture_clock())
    earlier = Draft(draft_id="draft:earlier", body="the plan before", created_at=CREATED)
    made = composed()
    reader: sqlite3.Connection | None = None
    try:
        store.record_waiting(
            earlier, thread_id="t-earlier", plan_date=PLAN_DATE, outcome="accepted"
        )
        store.publish(earlier.draft_id)
        if fault == "commit":
            store._connection.execute("PRAGMA busy_timeout = 25")
            reader = sqlite3.connect(path)
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM drafts").fetchone()
        else:
            table = "drafts" if fault == "insert" else "steps"
            store._connection.execute(
                f"CREATE TRIGGER refuse BEFORE INSERT ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )
            store._connection.commit()
        with pytest.raises(sqlite3.Error):
            save(
                store,
                made,
                steps=[_step("compose")],
            )
        left_open = store._connection.in_transaction
        if reader is not None:
            reader.rollback()
            reader.close()
            reader = None
        else:
            store._connection.execute("DROP TRIGGER refuse")
            store._connection.commit()
        kept = store.get(made.draft.draft_id)
        runs = [run.thread_id for run in store.runs_without_a_draft()]
        latest = store.latest_for(PLAN_DATE)
        save(store, made, steps=[_step("compose")])
        landed = store.get(made.draft.draft_id)
    finally:
        if reader is not None:
            reader.close()
        store.close()

    assert not left_open
    assert kept is None
    assert runs == []
    assert latest is not None
    assert latest.draft_id == earlier.draft_id
    assert landed is not None
    assert landed.plan_snapshot == made.snapshot.model_dump_json()
    assert [item.node for item in landed.steps] == ["compose"]


def _step(node: str) -> StepRecord:
    return StepRecord(node=node, round=1, expected="a draft", found="a draft", recorded_at=CREATED)


def test_a_replay_takes_the_whole_bundle_before_publication_and_nothing_after() -> None:
    """Before its run has paused a draft saved again is replaced whole, snapshot with text,
    and keeps the time it was first made; a save that would drop the snapshot it has, or
    that names another thread, is refused. Once published, the same composition changes
    nothing, a decision and its steps included, and a different one is refused."""
    first = composed()
    revised_plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[a_block(ESSAY.assignment_id, "16:30", "17:30", "one sitting")],
        deferred=[
            Deferral(assignment_id=OTHER_ESSAY.assignment_id, reason="tomorrow"),
            Deferral(assignment_id=PROBLEM_SET.assignment_id, reason="Monday"),
            Deferral(assignment_id=SYLLABUS.assignment_id, reason="ask"),
        ],
    )
    second = composed(revised_plan)
    later = second.draft.model_copy(update={"created_at": CREATED.replace(hour=23)})
    store = drafts_in_memory()
    try:
        save(store, first)
        with pytest.raises(IncompatibleReplay, match="drop the snapshot"):
            store.record_waiting(
                first.draft,
                thread_id="plan:2026-08-19:abc12345",
                plan_date=PLAN_DATE,
                outcome="unsettled",
            )
        with pytest.raises(IncompatibleReplay, match="another thread"):
            save(store, first, thread_id="plan:2026-08-19:another")
        save(store, Composition(draft=later, snapshot=second.snapshot))
        replaced = store.get(first.draft.draft_id)
        store.publish(first.draft.draft_id)
        store.record_decision(
            first.draft.draft_id,
            status=DraftStatus.APPROVED_FOR_MANUAL_SEND,
            decision="approved",
            reason="looks right",
        )
        decided = store.get(first.draft.draft_id)
        save(store, Composition(draft=later, snapshot=second.snapshot), steps=[_step("x")])
        the_same = store.get(first.draft.draft_id)
        with pytest.raises(IncompatibleReplay, match="on the pages"):
            save(store, first)
        with pytest.raises(IncompatibleReplay, match="on the pages"):
            save(store, second, inputs_digest="digest-2")
        after = store.get(first.draft.draft_id)
    finally:
        store.close()

    assert replaced is not None
    assert replaced.body == second.draft.body
    assert replaced.plan_snapshot == second.snapshot.model_dump_json()
    assert replaced.created_at == first.draft.created_at
    assert decided is not None
    assert decided.decision == "approved"
    assert the_same == decided
    assert after == decided
    assert after.steps == []


def test_a_file_from_before_gains_the_column_twice_over_and_keeps_its_drafts(
    tmp_path: pathlib.Path,
) -> None:
    """A drafts table without the snapshot column, holding a decided draft: opened, and
    opened again, it has the column once, the draft reads as it did with no snapshot, and a
    new draft beside it keeps its own."""
    path = tmp_path / "before.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE drafts (
            draft_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE, plan_date TEXT NOT NULL,
            status TEXT NOT NULL, outcome TEXT NOT NULL, body TEXT NOT NULL,
            created_at TEXT NOT NULL, decided_at TEXT, decision TEXT, reason TEXT,
            too_much INTEGER NOT NULL DEFAULT 0, superseded_by TEXT,
            published INTEGER NOT NULL DEFAULT 0, published_order INTEGER,
            inputs_digest TEXT, plan_assignment_ids TEXT
        );
        INSERT INTO drafts VALUES
            ('draft:old', 't-old', '2026-08-18', 'APPROVED_FOR_MANUAL_SEND', 'accepted',
             'Plan for Tuesday, August 18, 2026', '2026-08-18T22:00:00+00:00',
             '2026-08-18T23:00:00+00:00', 'approved', 'fine', 0, NULL, 1, 1, 'digest-0',
             '["assignment-canal-essay"]');
        """
    )
    old.commit()
    old.close()
    made = composed()
    for _ in range(2):
        store = DraftsStore.open(path, fixture_clock())
        try:
            columns = [
                str(row["name"]) for row in store._connection.execute("PRAGMA table_info(drafts)")
            ]
            kept = store.get("draft:old")
            if store.get(made.draft.draft_id) is None:
                save(store, made)
            new = store.get(made.draft.draft_id)
        finally:
            store.close()

        assert columns.count("plan_snapshot") == 1
        assert kept is not None
        assert (kept.body, kept.decision, kept.reason) == (
            "Plan for Tuesday, August 18, 2026",
            "approved",
            "fine",
        )
        assert kept.plan_snapshot is None
        assert kept.plan_assignment_ids == ["assignment-canal-essay"]
        assert new is not None
        assert new.plan_snapshot == made.snapshot.model_dump_json()


# ------------------------------------------------------------- read one record at a time


def test_a_reader_uses_a_snapshot_only_when_it_is_whole_and_its_drafts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No snapshot is an earlier plan and no failure. Text that is not JSON, another
    version, a missing field, metadata that does not match, another evening, and another
    list of assignments are each unavailable, logged with the draft's id and where it
    failed, and never with what the plan says."""
    snapshot = composed().snapshot
    whole = snapshot.model_dump(mode="json")
    ids = snapshot.assignment_ids

    def read(saved: str | None, **over: object) -> SnapshotReading:
        given: dict[str, object] = {"plan_date": PLAN_DATE, "plan_assignment_ids": ids}
        given.update(over)
        return read_snapshot("draft:read", saved, **given)  # type: ignore[arg-type]

    fewer = {k: v for k, v in whole["assignments"].items() if k != SYLLABUS.assignment_id}
    with caplog.at_level(logging.WARNING, logger="blossom.plan_snapshot"):
        good = read(json.dumps(whole))
        legacy = read(None)
        unusable = [
            read("not json {"),
            read("[1, 2]"),
            read(json.dumps({**whole, "version": 2})),
            read(json.dumps({**whole, "version": True})),
            read(json.dumps({k: v for k, v in whole.items() if k != "plan"})),
            read(json.dumps({**whole, "assignments": fewer})),
            read(json.dumps(whole), plan_date=date(2026, 8, 20)),
            read(json.dumps(whole), plan_assignment_ids=ids[:-1]),
            read(json.dumps(whole), plan_assignment_ids=None),
        ]

    assert good.snapshot == snapshot
    assert not good.unavailable
    assert (legacy.snapshot, legacy.unavailable) == (None, False)
    assert all(item.snapshot is None and item.unavailable for item in unusable)
    assert len(caplog.records) == len(unusable)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert logged.count("draft:read") == len(unusable)
    assert "Canal Era" not in logged
    assert "while it is fresh" not in logged
    assert all(len(record.getMessage()) < 450 for record in caplog.records)
