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
import threading
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from blossom import intake
from blossom.authored_text import TextRefused
from blossom.candidates import candidate_readings, reader
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
from blossom.stores.project_state import (
    Assignment,
    AssignmentKind,
    ProjectStateStore,
    Saved,
    StatusReport,
    Undone,
)
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
        candidates=reader(store),
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


OTHERWISE: dict[str, dict[str, Any]] = {
    "course": {"course": "Physics"},
    "title": {"title": "Different work"},
    "due": {"due": date(2026, 9, 19)},
    "no due": {"due": None},
    "kind": {"kind": "TASK"},
    "about": {"about": "Different instructions"},
}


@pytest.mark.parametrize("changed", sorted(OTHERWISE))
def test_a_second_press_with_anything_else_in_it_is_not_the_same_press(
    store: ProjectStateStore, changed: str
) -> None:
    """The assignment's id is the note's, whatever the press holds, so the id proves which
    note and never that the press is the one that was accepted."""
    name = note(store)
    accepted: dict[str, Any] = {"due": date(2026, 9, 18), "about": "Show the working."}
    assert isinstance(promote(store, name, details(**accepted), 1), CapturePromoted)
    after = everything(store)

    other = promote(store, name, details(**{**accepted, **OTHERWISE[changed]}), 1)
    again = promote(store, name, details(**accepted), 1)

    assert isinstance(other, CaptureConflict)
    assert isinstance(again, CaptureAlreadyPromoted)
    assert everything(store) == after


def test_joining_again_with_another_note_about_the_work_is_not_the_same_press(
    store: ProjectStateStore,
) -> None:
    target = on_record(store, due=date(2026, 9, 25))
    name = note(store)
    given = details(about="Her own reminder.")
    basis = candidate_basis(candidate_readings(store, given))
    joined = promote(store, name, given, 1, basis=basis, choice="same", target=target.assignment_id)
    after = everything(store)

    other = promote(
        store,
        name,
        details(about="A different second-device instruction"),
        1,
        basis=basis,
        choice="same",
        target=target.assignment_id,
    )
    again = promote(store, name, given, 1, basis=basis, choice="same", target=target.assignment_id)

    assert isinstance(joined, CapturePromoted)
    assert isinstance(other, CaptureConflict)
    assert isinstance(again, CaptureAlreadyPromoted)
    assert everything(store) == after


def test_a_separate_assignment_made_on_purpose_is_held_to_the_press_that_made_it(
    store: ProjectStateStore,
) -> None:
    on_record(store, due=date(2026, 9, 25))
    name = note(store)
    given = details(about="Kept apart on purpose.")
    basis = candidate_basis(candidate_readings(store, given))
    made = promote(store, name, given, 1, basis=basis, choice="separate")
    after = everything(store)
    now = candidate_basis(candidate_readings(store, given))

    other = promote(store, name, details(about="Other words"), 1, basis=now, choice="separate")
    as_new = promote(store, name, given, 1, basis=now, choice="new")
    again = promote(store, name, given, 1, basis=basis, choice="separate")

    assert isinstance(made, CapturePromoted)
    assert isinstance(other, CaptureConflict)
    assert isinstance(as_new, CaptureConflict)
    assert isinstance(again, CaptureAlreadyPromoted)
    assert everything(store) == after


def test_another_kind_of_press_that_names_the_same_assignment_is_not_the_same_press(
    store: ProjectStateStore,
) -> None:
    """Made as the note's own assignment, then asked for as a join to that very assignment,
    and as a separate one beside homework that was never shown: the id matches each time and
    the press does not."""
    name = note(store)
    given = details()
    assert isinstance(promote(store, name, given, 1), CapturePromoted)
    own = derived_assignment_id(name)
    basis = candidate_basis(candidate_readings(store, given))
    after = everything(store)

    as_a_join = promote(store, name, given, 1, basis=basis, choice="same", target=own)
    as_separate = promote(store, name, given, 1, basis=basis, choice="separate")

    assert isinstance(as_a_join, CaptureConflict)
    assert isinstance(as_separate, CaptureConflict)
    assert everything(store) == after


@pytest.mark.parametrize("since", ["edited", "archived"])
def test_the_press_that_was_accepted_is_what_a_later_press_is_held_to(
    store: ProjectStateStore, since: str
) -> None:
    """Her words, class, and day can change after the note is in homework, and the assignment
    does not follow. A press is compared with the one that was accepted, never with the note
    as it has since become."""
    name = note(store)
    accepted = details(due=date(2026, 9, 18))
    assert isinstance(promote(store, name, accepted, 1), CapturePromoted)
    if since == "edited":
        moved = store.edit_capture(
            name,
            "Changed words",
            "Physics",
            date(2026, 9, 30),
            expected_revision=2,
            authored_by=STUDENT,
            channel=HERS,
            now=AT + timedelta(hours=2),
            today=MONDAY,
        )
    else:
        moved = store.archive_capture(
            name,
            expected_revision=2,
            authored_by=STUDENT,
            now=AT + timedelta(hours=2),
            today=MONDAY,
        )
    assert isinstance(moved, CaptureChanged)
    after = everything(store)

    original = promote(store, name, accepted, 1)
    as_it_reads_now = promote(
        store,
        name,
        details(course="Physics", due=date(2026, 9, 30)),
        the_note(store, name).revision,
    )

    assert isinstance(original, CaptureAlreadyPromoted)
    assert isinstance(as_it_reads_now, CaptureConflict)
    assert everything(store) == after


def test_a_second_device_with_other_details_finds_the_first_ones_accepted(
    tmp_path: pathlib.Path,
) -> None:
    """Two connections to the file, the second reading after the first has committed."""
    path = tmp_path / "record.sqlite3"
    first = practice_store(path)
    second = ProjectStateStore.open(path, fixture_clock())
    name = note(first)
    assert isinstance(promote(first, name, details(about="The first device's"), 1), CapturePromoted)
    after = everything(first)

    other = promote(
        second, name, details(about="The second device's"), 1, by=PARENT, channel=THEIRS
    )

    assert isinstance(other, CaptureConflict)
    assert everything(first) == after == everything(second)


def test_two_devices_adding_one_note_at_once_make_one_assignment(tmp_path: pathlib.Path) -> None:
    """She and a parent press at the same moment, through two connections to the file: one
    press adds the note, the other finds it added, and there is one assignment, one claim,
    and one change."""
    path = tmp_path / "record.sqlite3"
    first = practice_store(path)
    second = ProjectStateStore.open(path, fixture_clock())
    name = note(first)
    given = details(due=date(2026, 9, 18))
    before = everything(first)
    outcomes: list[object] = []
    ready = threading.Barrier(2)

    def she_adds_it() -> None:
        ready.wait(timeout=10)
        outcomes.append(promote(first, name, given, 1))

    def a_parent_adds_it() -> None:
        ready.wait(timeout=10)
        outcomes.append(promote(second, name, given, 1, by=PARENT, channel=THEIRS))

    threads = [threading.Thread(target=she_adds_it), threading.Thread(target=a_parent_adds_it)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(type(outcome).__name__ for outcome in outcomes) == [
        "CaptureAlreadyPromoted",
        "CapturePromoted",
    ]
    made = [
        item
        for item in first.all_assignments()
        if item.assignment_id == derived_assignment_id(name)
    ]
    assert len(made) == 1
    assert len(first.claim_history(derived_assignment_id(name))) == 1
    assert [event.operation for event in first.capture_history(name)] == ["create", "promote"]
    assert everything(first) != before


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

    candidates = candidate_readings(store, given)
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


def school_says(store: ProjectStateStore, target: Assignment, status: str, day: date) -> None:
    store.record_status_reports(
        target.assignment_id,
        [
            StatusReport(
                status=status,
                channel=SourceChannel.LMS,
                reported_on=day,
                dated_by="the day it was pasted",
                observed_at=AT,
            )
        ],
    )


def she_says(store: ProjectStateStore, target: Assignment, status: str) -> str:
    saved = store.report_status(
        target.assignment_id,
        status,  # type: ignore[arg-type]
        None,
        expected_head=None,
        now=AT,
        today=MONDAY,
    )
    assert isinstance(saved, Saved)
    return saved.report.report_id


def test_a_candidate_is_read_with_what_she_and_the_school_currently_say(
    store: ProjectStateStore,
) -> None:
    """Her account and the school's are read apart, and no report of hers reads as none:
    what the record's own status column holds is never offered in its place."""
    target = on_record(store, due=date(2026, 9, 25))
    given = details()
    unreported = candidate_readings(store, given)
    school_says(store, target, "missing", MONDAY)
    done = she_says(store, target, "done")
    standing = candidate_readings(store, given)
    undone = store.undo_report(target.assignment_id, done, now=AT, today=MONDAY)
    taken_back = candidate_readings(store, given)

    assert [(item.work_state, item.work_reported_on, item.school) for item in unreported] == [
        (None, None, ())
    ]
    assert unreported[0].recorded_status == "not_started"
    assert unreported[0].record_source is SourceChannel.LMS
    assert [(item.work_state, item.work_reported_on) for item in standing] == [("done", MONDAY)]
    assert [(word.channel, word.status, word.reported_on) for word in standing[0].school] == [
        (SourceChannel.LMS, "missing", MONDAY)
    ]
    assert isinstance(undone, Undone)
    assert [(item.work_state, item.work_reported_on) for item in taken_back] == [(None, None)]


SINCE_THE_PAGE = ("done", "not yet", "undone", "school missing", "record source")


@pytest.mark.parametrize("choice", ["same", "separate"])
@pytest.mark.parametrize("since", SINCE_THE_PAGE)
def test_anything_shown_about_a_candidate_that_changed_since_is_put_to_her_again(
    store: ProjectStateStore, since: str, choice: str
) -> None:
    target = on_record(store, due=date(2026, 9, 25))
    before_the_page = she_says(store, target, "not_yet") if since == "undone" else None
    name = note(store)
    given = details()
    basis = candidate_basis(candidate_readings(store, given))
    if since == "done":
        she_says(store, target, "done")
    elif since == "not yet":
        she_says(store, target, "not_yet")
    elif since == "undone":
        assert before_the_page is not None
        store.undo_report(target.assignment_id, before_the_page, now=AT, today=MONDAY)
    elif since == "school missing":
        school_says(store, target, "missing", MONDAY)
    else:
        moved = target.model_copy(update={"origins": {"record": SourceChannel.PARENT_ENTRY}})
        store.upsert_assignments([moved])
    before = everything(store)

    answer = promote(
        store,
        name,
        given,
        1,
        basis=basis,
        choice=choice,
        target=target.assignment_id if choice == "same" else None,
    )

    assert isinstance(answer, CandidatesChanged)
    assert [item.assignment_id for item in answer.candidates] == [target.assignment_id]
    assert candidate_basis(answer.candidates) != basis
    assert everything(store) == before


def test_a_school_statement_that_changes_nothing_shown_asks_nothing_again(
    store: ProjectStateStore,
) -> None:
    """The same statement pasted again, and a statement of an earlier day that is not the
    channel's current word, leave every row as it was shown."""
    target = on_record(store, due=date(2026, 9, 25))
    school_says(store, target, "missing", MONDAY)
    name = note(store)
    given = details()
    basis = candidate_basis(candidate_readings(store, given))
    school_says(store, target, "missing", MONDAY)
    school_says(store, target, "not_started", MONDAY - timedelta(days=3))

    answer = promote(store, name, given, 1, basis=basis, choice="same", target=target.assignment_id)

    assert candidate_basis(candidate_readings(store, given)) == basis
    assert isinstance(answer, CapturePromoted)


@pytest.mark.parametrize("size", [1, 20, 200])
def test_the_reads_a_choice_costs_do_not_grow_with_the_candidates(
    store: ProjectStateStore, size: int
) -> None:
    """Showing the candidates and checking them again inside the save each read the record
    in a fixed number of statements, however many candidates there are."""
    targets = [
        on_record(store, due=date(2026, 9, 1) + timedelta(days=index)) for index in range(size)
    ]
    for target in targets[::7]:
        she_says(store, target, "done")
    name = note(store)
    given = details()
    statements: list[str] = []

    store._connection.set_trace_callback(statements.append)
    shown = candidate_readings(store, given)
    to_show = [made for made in statements if made.lstrip().upper().startswith("SELECT")]
    statements.clear()
    answer = promote(
        store,
        name,
        given,
        1,
        basis=candidate_basis(shown),
        choice="same",
        target=targets[0].assignment_id,
    )
    store._connection.set_trace_callback(None)
    to_save = [made for made in statements if made.lstrip().upper().startswith("SELECT")]

    assert len(shown) == size
    assert isinstance(answer, CapturePromoted)
    assert len(to_show) == 4
    assert len(to_save) <= 8


def test_same_homework_joins_the_note_and_overwrites_nothing(store: ProjectStateStore) -> None:
    target = on_record(store, due=date(2026, 9, 25))
    name = note(store)
    given = details(due=date(2026, 9, 18), about="Her own reminder.")
    basis = candidate_basis(candidate_readings(store, given))
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
    basis = candidate_basis(candidate_readings(store, given))

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
    basis = candidate_basis(candidate_readings(first, given))
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
    basis = candidate_basis(candidate_readings(store, given))
    store.upsert_assignments([shown.model_copy(update={"due_date": date(2026, 9, 12)})])

    answer = promote(store, name, given, 1, basis=basis, choice="same", target=shown.assignment_id)

    assert isinstance(answer, CandidatesChanged)
    assert the_note(store, name).outstanding


def test_a_target_that_is_no_candidate_is_no_choice(store: ProjectStateStore) -> None:
    on_record(store, due=date(2026, 9, 11))
    elsewhere = on_record(store, title="Another worksheet")
    name = note(store)
    given = details()
    basis = candidate_basis(candidate_readings(store, given))

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
