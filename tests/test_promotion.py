"""A homework note made into homework, or joined to homework already on record.

Details are added to a note by her or by a parent, each field saying who
supplied it, and never touch her words. Adding it to homework is one explicit
act that writes the assignment, the link, the claim about its date, and the
event together or not at all. Homework of the same class and title already on
record is a choice put to a person, never made for them, and a choice made on
a page that is behind is refused.
"""

import pathlib
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from blossom import intake
from blossom.authored_text import TextRefused
from blossom.captures import (
    CAPTURE_CLAIM_CONFIDENCE,
    CLARIFY,
    HOUSEHOLD,
    KINDS,
    LINK,
    PARENT,
    PROMOTE,
    STUDENT,
    CandidatesChanged,
    Capture,
    CaptureAlreadyPromoted,
    CaptureChanged,
    CaptureConflict,
    CaptureCreated,
    CaptureDetails,
    CaptureNotSaved,
    CapturePromoted,
    CaptureUnchanged,
    ChoiceNeeded,
    DetailsMissing,
    UnreadableCapture,
    candidate_basis,
    derived_assignment_id,
    new_capture_id,
)
from blossom.noticing import planning_digest, read_everything, week_from
from blossom.pairing import pair
from blossom.reconciliation import SourceChannel
from blossom.stores.project_state import Assignment, AssignmentKind, ProjectStateStore
from tests.support import fixture_clock, practice_store

MONDAY = date(2026, 9, 14)
AT = datetime(2026, 9, 14, 21, 0, tzinfo=UTC)
WORDS = "Geometry questions 4-8, heard from a classmate"
HERS = SourceChannel.STUDENT_REPORT
THEIRS = SourceChannel.PARENT_ENTRY
NO_ONE_YET = candidate_basis([])


@pytest.fixture
def store(tmp_path: pathlib.Path) -> ProjectStateStore:
    return practice_store(tmp_path / "record.sqlite3")


def note(store: ProjectStateStore, text: str = WORDS) -> str:
    name = new_capture_id()
    made = store.create_capture(
        name, text, None, None, authored_by=STUDENT, channel=HERS, now=AT, today=MONDAY
    )
    assert isinstance(made, CaptureCreated)
    return name


def details(
    course: str | None = "Geometry",
    title: str | None = "Questions 4-8",
    due: date | None = None,
    kind: str | None = "HOMEWORK",
    about: str | None = None,
) -> CaptureDetails:
    return CaptureDetails(
        course=course,
        title=title,
        due_date=due,
        kind=kind,  # type: ignore[arg-type]
        note=about,
    )


def clarify(
    store: ProjectStateStore,
    name: str,
    given: CaptureDetails,
    revision: int,
    *,
    by: str = STUDENT,
    channel: SourceChannel = HERS,
) -> object:
    return store.clarify_capture(
        name,
        given,
        expected_revision=revision,
        authored_by=by,  # type: ignore[arg-type]
        channel=channel,
        now=AT,
        today=MONDAY,
    )


def promote(
    store: ProjectStateStore,
    name: str,
    given: CaptureDetails,
    revision: int,
    *,
    basis: str = NO_ONE_YET,
    choice: str = "new",
    target: str | None = None,
    by: str = STUDENT,
    channel: SourceChannel = HERS,
) -> object:
    return store.promote_capture(
        name,
        given,
        expected_revision=revision,
        basis=basis,
        choice=choice,  # type: ignore[arg-type]
        target=target,
        authored_by=by,  # type: ignore[arg-type]
        channel=channel,
        now=AT + timedelta(hours=1),
        today=MONDAY,
    )


def everything(
    store: ProjectStateStore,
) -> tuple[list[object], list[object], list[object], list[object]]:
    connection = store._connection
    return (
        connection.execute("SELECT * FROM assignments ORDER BY assignment_id").fetchall(),
        connection.execute("SELECT * FROM date_claims ORDER BY rowid").fetchall(),
        connection.execute("SELECT * FROM homework_captures ORDER BY capture_id").fetchall(),
        connection.execute("SELECT * FROM capture_events ORDER BY sequence").fetchall(),
    )


def the_note(store: ProjectStateStore, name: str) -> Capture:
    held = store.capture(name)
    assert held is not None
    return held


# ------------------------------------------------------------------ the shared rules


def test_the_pairing_rule_is_the_one_the_school_paste_uses() -> None:
    assert intake.pair is pair
    assert pair("  Geometry ", "Questions   4-8") == ("Geometry", "Questions 4-8")
    assert pair("geometry", "Questions 4-8") != pair("Geometry", "Questions 4-8")


def test_the_kinds_a_note_may_hold_are_the_records_own() -> None:
    assert set(KINDS) == {kind.value for kind in AssignmentKind}


def test_the_confidence_a_notes_claim_carries_is_the_family_entrys_and_nothing_more() -> None:
    assert CAPTURE_CLAIM_CONFIDENCE == intake.FAMILY_CONFIDENCE == 0.8


def test_an_assignment_made_from_a_note_is_named_from_the_note_and_nothing_that_can_change() -> (
    None
):
    name = new_capture_id()
    assert derived_assignment_id(name) == derived_assignment_id(name)
    assert derived_assignment_id(name) != derived_assignment_id(new_capture_id())
    assert derived_assignment_id(name).startswith("assignment-from-note-")
    Assignment(
        assignment_id=derived_assignment_id(name),
        course="Geometry",
        title="Questions 4-8",
        due_date=None,
        dependencies=[],
        reported_submission_status="unknown",
    )


@pytest.mark.parametrize(
    ("given", "refused"),
    [
        ({"course": "c" * 61}, True),
        ({"course": "c" * 60}, False),
        ({"title": "t" * 201}, True),
        ({"title": "t" * 200}, False),
        ({"about": "n" * 501}, True),
        ({"about": "n" * 500}, False),
        ({"course": "Geo\nmetry"}, True),
        ({"title": "Questions\t4-8"}, True),
        ({"about": "two\nlines are fine"}, False),
        ({"title": "bell" + chr(7)}, True),
        ({"kind": "ESSAY"}, True),
        ({"kind": None}, False),
    ],
)
def test_details_are_held_to_the_limits_an_entry_is_held_to(
    given: dict[str, str | None], refused: bool
) -> None:
    if refused:
        with pytest.raises((TextRefused, ValueError)):
            details(**given)  # type: ignore[arg-type]
    else:
        assert details(**given) is not None  # type: ignore[arg-type]


# ------------------------------------------------------------------ adding details


def test_details_say_who_supplied_each_and_never_touch_her_words(store: ProjectStateStore) -> None:
    name = note(store)
    first = clarify(store, name, details(due=None, kind=None), 1)
    assert isinstance(first, CaptureChanged)
    second = clarify(
        store, name, details(due=date(2026, 9, 18), kind=None), 2, by=PARENT, channel=THEIRS
    )
    assert isinstance(second, CaptureChanged)
    held = the_note(store, name)

    assert (held.text, held.original_text) == (WORDS, WORDS)
    assert (held.course, held.title, held.due_date, held.kind) == (
        "Geometry",
        "Questions 4-8",
        date(2026, 9, 18),
        None,
    )
    assert {field: (by.authored_by, by.channel) for field, by in held.attribution.items()} == {
        "course": (STUDENT, HERS),
        "title": (STUDENT, HERS),
        "due_date": (PARENT, THEIRS),
    }
    assert [change.operation for change in store.capture_history(name)][1:] == [CLARIFY, CLARIFY]
    assert second.event.authored_by == PARENT
    assert held.revision == 3
    assert held.outstanding


def test_details_from_a_page_that_is_behind_change_nothing(store: ProjectStateStore) -> None:
    name = note(store)
    assert isinstance(clarify(store, name, details(), 1), CaptureChanged)
    before = everything(store)

    assert isinstance(clarify(store, name, details(title="Another title"), 1), CaptureConflict)
    assert isinstance(clarify(store, name, details(), 1), CaptureUnchanged)
    store.archive_capture(name, expected_revision=2, authored_by=STUDENT, now=AT, today=MONDAY)
    assert isinstance(clarify(store, name, details(title="Another title"), 3), CaptureConflict)
    assert everything(store)[:2] == before[:2]


def test_details_alone_are_no_homework_and_change_nothing_a_plan_is_made_from(
    store: ProjectStateStore,
) -> None:
    before = planning_digest(week_from(read_everything(store, store), MONDAY))
    rows = everything(store)[:2]
    name = note(store)
    clarify(store, name, details(due=date(2026, 9, 18), about="Show the working."), 1)

    assert planning_digest(week_from(read_everything(store, store), MONDAY)) == before
    assert everything(store)[:2] == rows


# ------------------------------------------------------------------ adding it to homework


def test_adding_it_writes_the_assignment_the_link_the_claim_and_the_event_together(
    store: ProjectStateStore,
) -> None:
    name = note(store)
    clarify(store, name, details(kind=None), 1)
    clarify(store, name, details(due=date(2026, 9, 18), kind=None), 2, by=PARENT, channel=THEIRS)
    before = planning_digest(week_from(read_everything(store, store), MONDAY))
    given = details(due=date(2026, 9, 18), kind="HOMEWORK", about="Show the working.")

    done = promote(store, name, given, 3)

    assert isinstance(done, CapturePromoted)
    made = derived_assignment_id(name)
    assert (done.assignment_id, done.created) == (made, True)
    row = next(item for item in store.all_assignments() if item.assignment_id == made)
    assert (row.course, row.title, row.due_date, row.kind, row.note) == (
        "Geometry",
        "Questions 4-8",
        date(2026, 9, 18),
        AssignmentKind.HOMEWORK,
        "Show the working.",
    )
    assert row.origins == {
        "record": HERS,
        "course": HERS,
        "title": HERS,
        "due_date": THEIRS,
        "kind": HERS,
        "note": HERS,
    }
    assert row.note_by == "student"
    assert row.reported_submission_status == "unknown"
    claims = store.claim_history(made)
    assert [(c.record.channel, c.record.asserted_value, c.record.confidence) for c in claims] == [
        (THEIRS, "2026-09-18", CAPTURE_CLAIM_CONFIDENCE)
    ]
    assert [(c.capture_id, c.capture_revision, c.active) for c in claims] == [(name, 4, True)]
    held = the_note(store, name)
    assert (held.assignment_id, held.revision, held.outstanding) == (made, 4, False)
    assert (held.text, held.original_text) == (WORDS, WORDS)
    assert name not in [item.capture_id for item in store.outstanding_captures().notes]
    assert store.capture_history(name)[-1].operation == PROMOTE
    assert store._connection.execute("SELECT COUNT(*) FROM status_reports").fetchone()[0] == 0
    assert planning_digest(week_from(read_everything(store, store), MONDAY)) != before
    assert store.sound_capture_history(name) is not None


def test_the_same_press_again_makes_no_second_assignment(store: ProjectStateStore) -> None:
    name = note(store)
    given = details(due=date(2026, 9, 18))
    assert isinstance(promote(store, name, given, 1), CapturePromoted)
    after = everything(store)

    again = promote(store, name, given, 1)

    assert isinstance(again, CaptureAlreadyPromoted)
    assert again.assignment_id == derived_assignment_id(name)
    assert everything(store) == after


def test_it_needs_a_class_and_a_title_and_nothing_is_made_up_for_them(
    store: ProjectStateStore,
) -> None:
    name = note(store)
    before = everything(store)

    for given in (details(course=None), details(title=None), details(course=None, title=None)):
        assert isinstance(promote(store, name, given, 1), DetailsMissing)
    assert everything(store) == before


def test_whoever_adds_it_the_fields_keep_the_hand_that_supplied_them(
    store: ProjectStateStore,
) -> None:
    """She named the class and the title; a parent presses the button and adds the day. The
    record is a family entry, and her fields stay hers."""
    name = note(store)
    clarify(store, name, details(kind=None), 1)
    given = details(due=date(2026, 9, 18), kind="TASK")

    done = promote(store, name, given, 2, by=PARENT, channel=THEIRS)

    assert isinstance(done, CapturePromoted)
    row = next(item for item in store.all_assignments() if item.assignment_id == done.assignment_id)
    assert row.origins == {
        "record": THEIRS,
        "course": HERS,
        "title": HERS,
        "due_date": THEIRS,
        "kind": THEIRS,
    }
    assert row.kind == AssignmentKind.TASK
    assert done.event.authored_by == PARENT


def test_with_the_sign_in_off_the_household_did_it_and_the_route_says_which_way_in(
    store: ProjectStateStore,
) -> None:
    name = note(store)

    done = promote(store, name, details(), 1, by=HOUSEHOLD, channel=HERS)

    assert isinstance(done, CapturePromoted)
    row = next(item for item in store.all_assignments() if item.assignment_id == done.assignment_id)
    assert row.origins["record"] == HERS
    assert done.event.authored_by == HOUSEHOLD
    assert the_note(store, name).attribution["title"].authored_by == HOUSEHOLD


def test_a_failure_part_way_leaves_the_note_as_it_was_and_no_part_of_an_assignment(
    store: ProjectStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = note(store)
    before = everything(store)

    def refuses(*args: object, **kwargs: object) -> None:
        msg = "the file refused"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(store, "_record_capture_claim_locked", refuses)
    with pytest.raises(CaptureNotSaved):
        promote(store, name, details(due=date(2026, 9, 18)), 1)
    monkeypatch.undo()

    assert everything(store) == before
    assert not store._connection.in_transaction
    assert the_note(store, name).outstanding


# ------------------------------------------------------------------ homework already on record


def on_record(
    store: ProjectStateStore, title: str = "Questions 4-8", due: date | None = None
) -> Assignment:
    row = Assignment(
        assignment_id=intake.identity("Geometry", title, None if due is None else due.isoformat()),
        course="Geometry",
        title=title,
        due_date=due,
        dependencies=[],
        reported_submission_status="not_started",
        origins={"record": SourceChannel.LMS},
    )
    store.put_on_record([row], {})
    return row


def test_homework_of_the_same_class_and_title_is_a_choice_and_never_made_for_anyone(
    store: ProjectStateStore,
) -> None:
    """Every one of the same class and title is a candidate, whatever its day. Another
    spelling or another case is not the same to the record, so it is no candidate."""
    early = on_record(store, due=date(2026, 9, 11))
    late = on_record(store, due=date(2026, 9, 25))
    on_record(store, title="questions 4-8")
    name = note(store)
    given = details(course="  Geometry ", title="Questions   4-8", due=date(2026, 9, 18))

    candidates = store.promotion_candidates(given)
    before = everything(store)
    unasked = promote(store, name, given, 1, basis=candidate_basis(candidates))

    assert sorted(item.assignment_id for item in candidates) == sorted(
        [early.assignment_id, late.assignment_id]
    )
    assert isinstance(unasked, ChoiceNeeded)
    assert sorted(item.assignment_id for item in unasked.candidates) == sorted(
        item.assignment_id for item in candidates
    )
    assert everything(store) == before


def test_same_homework_joins_the_note_and_overwrites_nothing(store: ProjectStateStore) -> None:
    target = on_record(store, due=date(2026, 9, 25))
    name = note(store)
    given = details(due=date(2026, 9, 18), about="Her own reminder.")
    basis = candidate_basis(store.promotion_candidates(given))
    assignments = everything(store)[0]

    done = promote(store, name, given, 1, basis=basis, choice="same", target=target.assignment_id)

    assert isinstance(done, CapturePromoted)
    assert (done.assignment_id, done.created) == (target.assignment_id, False)
    assert everything(store)[0] == assignments
    claims = store.claim_history(target.assignment_id)
    assert [(c.capture_id, c.record.asserted_value, c.record.channel) for c in claims] == [
        (name, "2026-09-18", HERS)
    ]
    assert store.capture_history(name)[-1].operation == LINK
    assert the_note(store, name).assignment_id == target.assignment_id
    assert derived_assignment_id(name) not in {
        item.assignment_id for item in store.all_assignments()
    }


def test_keep_separate_is_written_down_and_makes_the_notes_own_assignment_once(
    store: ProjectStateStore,
) -> None:
    other = on_record(store, due=date(2026, 9, 25))
    name = note(store)
    given = details()
    basis = candidate_basis(store.promotion_candidates(given))

    done = promote(store, name, given, 1, basis=basis, choice="separate")
    again = promote(store, name, given, 1, basis=basis, choice="separate")

    assert isinstance(done, CapturePromoted)
    assert (done.assignment_id, done.created) == (derived_assignment_id(name), True)
    assert isinstance(again, CaptureAlreadyPromoted)
    decision = done.event.decision
    assert decision is not None
    assert (decision.choice, decision.candidates, decision.basis) == (
        "separate",
        (other.assignment_id,),
        basis,
    )
    assert (
        len(
            [
                a
                for a in store.all_assignments()
                if pair(a.course, a.title) == pair("Geometry", "Questions 4-8")
            ]
        )
        == 2
    )


@pytest.mark.parametrize("choice", ["new", "separate", "same"])
def test_homework_that_arrived_after_the_page_was_made_is_put_to_her_again(
    tmp_path: pathlib.Path, choice: str
) -> None:
    """Two connections: the page showed no homework of that name, or one; the school's
    paste lands through the other connection; the press is answered with the choices as
    they stand, nothing is written, and no twin is made."""
    path = tmp_path / "record.sqlite3"
    first = practice_store(path)
    second = ProjectStateStore.open(path, fixture_clock())
    name = note(first)
    given = details()
    shown = on_record(first, due=date(2026, 9, 11)) if choice != "new" else None
    basis = candidate_basis(first.promotion_candidates(given))
    arrived = on_record(second, due=date(2026, 9, 25))
    before = everything(first)

    answer = promote(
        first,
        name,
        given,
        1,
        basis=basis,
        choice=choice,
        target=None if shown is None or choice != "same" else shown.assignment_id,
    )

    assert isinstance(answer, CandidatesChanged)
    assert arrived.assignment_id in {item.assignment_id for item in answer.candidates}
    assert everything(first) == before
    assert the_note(first, name).outstanding


def test_a_candidate_that_changed_since_the_page_was_made_is_put_to_her_again(
    store: ProjectStateStore,
) -> None:
    shown = on_record(store, due=date(2026, 9, 11))
    name = note(store)
    given = details()
    basis = candidate_basis(store.promotion_candidates(given))
    store.upsert_assignments([shown.model_copy(update={"due_date": date(2026, 9, 12)})])

    answer = promote(store, name, given, 1, basis=basis, choice="same", target=shown.assignment_id)

    assert isinstance(answer, CandidatesChanged)
    assert the_note(store, name).outstanding


def test_a_target_that_is_no_candidate_is_no_choice(store: ProjectStateStore) -> None:
    on_record(store, due=date(2026, 9, 11))
    elsewhere = on_record(store, title="Another worksheet")
    name = note(store)
    given = details()
    basis = candidate_basis(store.promotion_candidates(given))

    answer = promote(
        store, name, given, 1, basis=basis, choice="same", target=elsewhere.assignment_id
    )

    assert isinstance(answer, ChoiceNeeded)
    assert the_note(store, name).outstanding


# ------------------------------------------------------------------ what the file holds


@pytest.mark.parametrize(
    "damage",
    [
        "UPDATE homework_captures SET kind = 'ESSAY'",
        "UPDATE homework_captures SET title = 'Two' || char(10) || 'lines'",
        "UPDATE homework_captures SET title = ' padded '",
        "UPDATE homework_captures SET note = ''",
        "UPDATE homework_captures SET assignment_id = ''",
        "UPDATE homework_captures SET attribution = json_remove(attribution, '$.title')",
    ],
)
def test_details_held_any_other_way_than_the_store_writes_them_are_a_note_that_cannot_be_read(
    store: ProjectStateStore, damage: str
) -> None:
    name = note(store)
    clarify(store, name, details(about="Show the working."), 1)
    store._connection.execute(damage)
    store._connection.commit()

    with pytest.raises(UnreadableCapture):
        store.capture(name)
    assert store.outstanding_captures().unreadable == [name] or store.captures_named(
        [name]
    ).unreadable == [name]


def test_a_change_written_before_details_existed_is_still_read(store: ProjectStateStore) -> None:
    """A note saved before this kept four things of each moment. Its changes read as they
    did, and the note takes details and is added to homework like any other."""
    name = note(store)
    store._connection.execute(
        "UPDATE capture_events SET after = json_remove(after, '$.title', '$.kind', '$.note', "
        "'$.assignment_id')"
    )
    store._connection.commit()

    assert store.sound_capture_history(name) is not None
    assert isinstance(clarify(store, name, details(), 1), CaptureChanged)
    assert isinstance(promote(store, name, details(), 2), CapturePromoted)
    assert store.sound_capture_history(name) is not None
