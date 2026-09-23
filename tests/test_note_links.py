"""Finding and correcting a note's link to homework: the store and the line of changes.

A note joins homework found by search through an explicit choice of one row,
held to that row as it was shown and to the note's revision. A note joined to
homework that was on record before it can be moved to other homework or
unlinked, each checked the same way; an unlink withdraws only that note's
claims on the old homework and changes nothing else about it. A note that made
its own assignment stays where it is. The history keeps every link and unlink.
"""

import pathlib
from datetime import UTC, date, datetime, timedelta

import pytest

from blossom import intake
from blossom.candidates import readings_for, row_reader
from blossom.captures import (
    LINK,
    STUDENT,
    UNLINK,
    CandidateDecision,
    CandidatesChanged,
    Capture,
    CaptureAlreadyPromoted,
    CaptureAlreadyUnlinked,
    CaptureConflict,
    CaptureCreated,
    CaptureDetails,
    CaptureEvent,
    CaptureNotJoined,
    CaptureNotSaved,
    CapturePromoted,
    CaptureSnapshot,
    CaptureUnlinked,
    HomeworkGone,
    UnsoundCaptureHistory,
    accepted_press,
    candidate_basis,
    derived_assignment_id,
    new_capture_id,
    sound_history,
    what_remains,
)
from blossom.pairing import pair
from blossom.reconciliation import SourceChannel, SourceRecord
from blossom.stores.project_state import Assignment, ProjectStateStore, StatusReport
from tests.support import practice_store

MONDAY = date(2026, 9, 14)
AT = datetime(2026, 9, 14, 21, 0, tzinfo=UTC)
LATER = AT + timedelta(hours=1)
WORDS = "Reading log, the one Ms. Ortiz mentioned"
HERS = SourceChannel.STUDENT_REPORT


@pytest.fixture
def store(tmp_path: pathlib.Path) -> ProjectStateStore:
    return practice_store(tmp_path / "record.sqlite3")


def note(store: ProjectStateStore, text: str = WORDS, due: date | None = None) -> str:
    name = new_capture_id()
    made = store.create_capture(
        name, text, None, due, authored_by=STUDENT, channel=HERS, now=AT, today=MONDAY
    )
    assert isinstance(made, CaptureCreated)
    return name


def details(**given: object) -> CaptureDetails:
    return CaptureDetails(
        **{"course": "Geometry", "title": "Questions 4-8", "kind": "HOMEWORK", **given}  # type: ignore[arg-type]
    )


def on_record(
    store: ProjectStateStore,
    course: str = "Humanities",
    title: str = "Summer reading log",
    due: date | None = date(2026, 9, 25),
) -> Assignment:
    row = Assignment(
        assignment_id=intake.identity(course, title, None if due is None else due.isoformat()),
        course=course,
        title=title,
        due_date=due,
        dependencies=[],
        reported_submission_status="not_started",
        origins={"record": SourceChannel.LMS},
    )
    store.put_on_record([row], {})
    return row


def basis_of(store: ProjectStateStore, target: Assignment) -> str:
    return candidate_basis(readings_for(store, [target]))


def link(
    store: ProjectStateStore,
    name: str,
    target: Assignment,
    revision: int,
    *,
    basis: str | None = None,
    leaving: str | None = None,
    now: datetime = LATER,
) -> object:
    return store.link_capture(
        name,
        target=target.assignment_id,
        expected_revision=revision,
        basis=basis_of(store, target) if basis is None else basis,
        leaving=leaving,
        shown=row_reader(store),
        authored_by=STUDENT,
        channel=HERS,
        now=now,
        today=MONDAY,
    )


def unlink(
    store: ProjectStateStore, name: str, revision: int, leaving: str, now: datetime = LATER
) -> object:
    return store.unlink_capture(
        name,
        expected_revision=revision,
        leaving=leaving,
        authored_by=STUDENT,
        now=now,
        today=MONDAY,
    )


def the_note(store: ProjectStateStore, name: str) -> Capture:
    held = store.capture(name)
    assert held is not None
    return held


def tables(store: ProjectStateStore) -> dict[str, list[object]]:
    connection = store._connection
    names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    ]
    return {
        name: connection.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()  # noqa: S608
        for name in names
        if name != "sqlite_sequence"
    }


def claims(store: ProjectStateStore, assignment_id: str) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in store._connection.execute(
            "SELECT asserted_value, capture_id, active, withdrawn_at IS NOT NULL FROM date_claims "
            "WHERE assignment_id = ? ORDER BY rowid",
            (assignment_id,),
        ).fetchall()
    ]


def joined(
    store: ProjectStateStore, target: Assignment, due: date | None = date(2026, 9, 18)
) -> str:
    name = note(store, due=due)
    assert isinstance(link(store, name, target, 1), CapturePromoted)
    return name


# ------------------------------------------------------------------ joining homework found


@pytest.mark.parametrize("day", [date(2026, 9, 18), None])
def test_a_note_joins_homework_found_by_search_and_changes_nothing_on_it(
    store: ProjectStateStore, day: date | None
) -> None:
    """The note names the homework, its event keeps the choice of that one row and the row
    as shown, a day the note gives is one more claim beside the school's, and the homework's
    own row is as it was. The note needs no details of its own for this."""
    target = on_record(store)
    store.record_claims(
        target.assignment_id,
        [
            SourceRecord(
                channel=SourceChannel.LMS,
                asserted_value="2026-09-25",
                observed_at=AT,
                confidence=0.9,
                seen_in="day header",
            )
        ],
    )
    name = note(store, due=day)
    before = tables(store)

    made = link(store, name, target, 1)

    assert isinstance(made, CapturePromoted)
    assert (made.assignment_id, made.created) == (target.assignment_id, False)
    held = the_note(store, name)
    assert (held.assignment_id, held.revision, held.outstanding) == (target.assignment_id, 2, False)
    assert (held.course, held.title, held.kind) == (None, None, None)
    event = store.capture_history(name)[-1]
    assert event.operation == LINK
    assert event.decision == CandidateDecision(
        choice="found", candidates=(target.assignment_id,), basis=basis_of(store, target)
    )
    after = tables(store)
    assert after["assignments"] == before["assignments"]
    expected: list[tuple[object, ...]] = [("2026-09-25", None, 1, 0)]
    if day is not None:
        expected.append((day.isoformat(), name, 1, 0))
    assert claims(store, target.assignment_id) == expected
    assert store.one_assignment(target.assignment_id) == target


def test_the_same_link_by_search_again_writes_nothing(store: ProjectStateStore) -> None:
    target = on_record(store)
    name = joined(store, target)
    before = tables(store)

    again = link(store, name, target, 1)
    other = link(store, name, on_record(store, title="Other work"), 1)

    assert isinstance(again, CaptureAlreadyPromoted)
    assert again.assignment_id == target.assignment_id
    assert isinstance(other, CaptureConflict)
    assert {name: rows for name, rows in tables(store).items() if name != "assignments"} == {
        name: rows for name, rows in before.items() if name != "assignments"
    }


def test_a_link_by_search_is_held_to_the_row_as_it_was_shown(store: ProjectStateStore) -> None:
    """The row's fingerprint travels with the choice. Homework that changed in anything the
    row showed since is put to the person again with the row as it stands, and nothing is
    written."""
    target = on_record(store)
    name = note(store)
    shown = basis_of(store, target)
    store.record_status_reports(
        target.assignment_id,
        [
            StatusReport(
                status="missing",
                channel=SourceChannel.LMS,
                reported_on=MONDAY,
                dated_by="the day it was pasted",
                observed_at=AT,
            )
        ],
    )
    before = tables(store)

    refused = link(store, name, target, 1, basis=shown)

    assert isinstance(refused, CandidatesChanged)
    assert [item.assignment_id for item in refused.candidates] == [target.assignment_id]
    assert candidate_basis(refused.candidates) != shown
    assert tables(store) == before
    assert the_note(store, name).assignment_id is None


def test_a_link_to_homework_that_left_the_record_is_refused(store: ProjectStateStore) -> None:
    target = on_record(store)
    name = note(store)
    shown = basis_of(store, target)
    store._connection.execute(
        "DELETE FROM assignments WHERE assignment_id = ?", (target.assignment_id,)
    )
    store._connection.commit()
    before = tables(store)

    refused = link(store, name, target, 1, basis=shown)

    assert isinstance(refused, HomeworkGone)
    assert refused.assignment_id == target.assignment_id
    assert tables(store) == before


def test_a_link_from_a_page_that_is_behind_or_for_a_note_put_away_is_refused(
    store: ProjectStateStore,
) -> None:
    target = on_record(store)
    name = note(store)
    stale = link(store, name, target, 2)
    store.archive_capture(name, expected_revision=1, authored_by=STUDENT, now=LATER, today=MONDAY)
    put_away = link(store, name, target, 2)

    assert isinstance(stale, CaptureConflict)
    assert isinstance(put_away, CaptureConflict)
    assert the_note(store, name).assignment_id is None


# ------------------------------------------------------------------ unlinking


def test_unlinking_withdraws_the_notes_claims_and_nothing_else(
    store: ProjectStateStore,
) -> None:
    """The note's claims on that homework count no more and stay as history with the moment;
    the school's equal claim and another note's claim are untouched; the note waits again
    with its details kept; the homework's row, its reports, and everything else on record
    are as they were; the history keeps the link and the unlink."""
    target = on_record(store)
    store.record_claims(
        target.assignment_id,
        [
            SourceRecord(
                channel=SourceChannel.LMS,
                asserted_value="2026-09-18",
                observed_at=AT,
                confidence=0.9,
                seen_in="day header",
            )
        ],
    )
    other = joined(store, target, due=date(2026, 9, 19))
    name = joined(store, target)
    before = tables(store)

    left = unlink(store, name, 2, target.assignment_id)

    assert isinstance(left, CaptureUnlinked)
    assert (left.assignment_id, left.event.operation) == (target.assignment_id, UNLINK)
    held = the_note(store, name)
    assert (held.assignment_id, held.revision, held.outstanding, held.due_date) == (
        None,
        3,
        True,
        date(2026, 9, 18),
    )
    assert claims(store, target.assignment_id) == [
        ("2026-09-18", None, 1, 0),
        ("2026-09-19", other, 1, 0),
        ("2026-09-18", name, 0, 1),
    ]
    assert [event.operation for event in store.capture_history(name)] == ["create", LINK, UNLINK]
    after = tables(store)
    assert {
        n: rows
        for n, rows in after.items()
        if n not in ("date_claims", "homework_captures", "capture_events")
    } == {
        n: rows
        for n, rows in before.items()
        if n not in ("date_claims", "homework_captures", "capture_events")
    }
    assert [said.asserted_value for said in store.deadline_records(target.assignment_id)] == [
        "2026-09-18",
        "2026-09-19",
    ]
    assert [item.capture_id for item in store.outstanding_captures().notes] == [name]


def test_the_same_unlink_again_writes_nothing(store: ProjectStateStore) -> None:
    target = on_record(store)
    name = joined(store, target)
    assert isinstance(unlink(store, name, 2, target.assignment_id), CaptureUnlinked)
    before = tables(store)

    again = unlink(store, name, 2, target.assignment_id)

    assert isinstance(again, CaptureAlreadyUnlinked)
    assert again.assignment_id == target.assignment_id
    assert again.head.operation == UNLINK
    assert tables(store) == before


def test_an_unlink_is_held_to_the_homework_shown_and_the_revision(
    store: ProjectStateStore,
) -> None:
    target = on_record(store)
    elsewhere = on_record(store, title="Other work")
    name = joined(store, target)
    before = tables(store)

    other_homework = unlink(store, name, 2, elsewhere.assignment_id)
    behind = unlink(store, name, 1, target.assignment_id)

    assert isinstance(other_homework, CaptureConflict)
    assert isinstance(behind, CaptureConflict)
    assert tables(store) == before


def test_a_note_that_made_its_own_assignment_stays_where_it_is(store: ProjectStateStore) -> None:
    """A promoted note is neither unlinked nor moved: its link is evidence of the assignment
    it made. A note still waiting has nothing to unlink either."""
    name = note(store)
    assert isinstance(
        store.promote_capture(
            name,
            details(),
            expected_revision=1,
            basis=candidate_basis([]),
            choice="new",
            candidates=lambda given, rows: readings_for(
                store, store.promotion_candidates(given, among=rows)
            ),
            authored_by=STUDENT,
            channel=HERS,
            now=LATER,
            today=MONDAY,
        ),
        CapturePromoted,
    )
    own = derived_assignment_id(name)
    waiting = note(store)
    elsewhere = on_record(store)
    before = tables(store)

    assert isinstance(unlink(store, name, 2, own), CaptureNotJoined)
    assert isinstance(link(store, name, elsewhere, 2, leaving=own), CaptureNotJoined)
    assert isinstance(unlink(store, waiting, 1, elsewhere.assignment_id), CaptureNotJoined)
    assert isinstance(link(store, waiting, elsewhere, 1, leaving=own), CaptureNotJoined)
    assert tables(store) == before


# ------------------------------------------------------------------ changing a link


def test_changing_a_link_unlinks_and_links_in_one_commit(store: ProjectStateStore) -> None:
    """The old homework loses the note's claims, the new one gains one, the note names the
    new one, and the history holds the unlink and the link, two revisions on. The old
    homework's row is as it was."""
    old = on_record(store)
    new = on_record(store, course="Spanish", title="Vocabulary list, unit two")
    name = joined(store, old)
    before = tables(store)

    moved = link(store, name, new, 2, leaving=old.assignment_id)

    assert isinstance(moved, CapturePromoted)
    assert (moved.assignment_id, moved.created) == (new.assignment_id, False)
    held = the_note(store, name)
    assert (held.assignment_id, held.revision) == (new.assignment_id, 4)
    assert [event.operation for event in store.capture_history(name)] == [
        "create",
        LINK,
        UNLINK,
        LINK,
    ]
    assert claims(store, old.assignment_id) == [("2026-09-18", name, 0, 1)]
    assert claims(store, new.assignment_id) == [("2026-09-18", name, 1, 0)]
    assert tables(store)["assignments"] == before["assignments"]
    assert accepted_press(store.capture_history(name)) is not None
    assert accepted_press(store.capture_history(name)).after.assignment_id == new.assignment_id  # type: ignore[union-attr]


def test_a_change_of_link_that_fails_part_way_leaves_the_note_as_it_was(
    store: ProjectStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = on_record(store)
    new = on_record(store, course="Spanish", title="Vocabulary list, unit two")
    name = joined(store, old)
    before = tables(store)

    def refuses(*args: object, **kwargs: object) -> None:
        msg = "the file refused the claim"
        raise RuntimeError(msg)

    monkeypatch.setattr(store, "_record_capture_claim_locked", refuses)
    with pytest.raises(CaptureNotSaved):
        link(store, name, new, 2, leaving=old.assignment_id)

    assert tables(store) == before
    assert not store._connection.in_transaction
    assert the_note(store, name).assignment_id == old.assignment_id


def test_a_change_of_link_is_held_to_the_homework_shown_as_the_old_one(
    store: ProjectStateStore,
) -> None:
    old = on_record(store)
    new = on_record(store, course="Spanish", title="Vocabulary list, unit two")
    elsewhere = on_record(store, title="Other work")
    name = joined(store, old)
    before = tables(store)

    refused = link(store, name, new, 2, leaving=elsewhere.assignment_id)
    changed = link(store, name, new, 2, leaving=old.assignment_id, basis="not what was shown")

    assert isinstance(refused, CaptureConflict)
    assert isinstance(changed, CandidatesChanged)
    assert tables(store) == before


def test_the_same_change_of_link_again_writes_nothing(store: ProjectStateStore) -> None:
    old = on_record(store)
    new = on_record(store, course="Spanish", title="Vocabulary list, unit two")
    name = joined(store, old)
    assert isinstance(link(store, name, new, 2, leaving=old.assignment_id), CapturePromoted)
    before = tables(store)

    again = link(store, name, new, 2, leaving=old.assignment_id)

    assert isinstance(again, CaptureAlreadyPromoted)
    assert again.assignment_id == new.assignment_id
    assert tables(store) == before


# ------------------------------------------------------------------ after an unlink


def test_after_an_unlink_the_note_waits_with_what_it_needs_and_can_be_joined_again(
    store: ProjectStateStore,
) -> None:
    target = on_record(store)
    name = joined(store, target)
    assert isinstance(unlink(store, name, 2, target.assignment_id), CaptureUnlinked)
    held = the_note(store, name)
    on_record_pairs = {pair(item.course, item.title) for item in store.all_assignments()}

    assert what_remains([held], on_record_pairs) == {name: "needs"}
    assert accepted_press(store.capture_history(name)) is None
    again = link(store, name, target, 3)
    assert isinstance(again, CapturePromoted)
    assert claims(store, target.assignment_id) == [
        ("2026-09-18", name, 0, 1),
        ("2026-09-18", name, 1, 0),
    ]


# ------------------------------------------------------------------ the line of changes


def test_the_line_of_changes_holds_an_unlink_to_a_note_that_was_joined(
    store: ProjectStateStore,
) -> None:
    """An unlink leaves a joined note waiting, its words and details as they were, not put
    away, with no choice on it; on a note that made its own assignment, on a note that waits,
    or with anything else changed, it is no unlink."""
    target = on_record(store)
    name = joined(store, target)
    assert isinstance(unlink(store, name, 2, target.assignment_id), CaptureUnlinked)
    held = the_note(store, name)
    events = store.capture_history(name)
    assert sound_history(held, events) is not None
    create, made, left = events

    def unsound(last: CaptureEvent, note_as: Capture | None = None) -> None:
        with pytest.raises(UnsoundCaptureHistory):
            sound_history(note_as or held, [create, made, last])

    reworded = held.model_copy(update={"text": "Other words"})
    unsound(left.model_copy(update={"after": CaptureSnapshot.of(reworded)}), reworded)
    put_away = held.model_copy(update={"archived": True})
    unsound(left.model_copy(update={"after": CaptureSnapshot.of(put_away)}), put_away)
    unsound(
        left.model_copy(
            update={"decision": CandidateDecision(choice="found", candidates=(), basis="x")}
        )
    )
    with pytest.raises(UnsoundCaptureHistory, match="was not joined"):
        sound_history(
            held, [create, left.model_copy(update={"revision": 2, "before": create.after})]
        )
    promoted = made.model_copy(
        update={
            "operation": "promote",
            "decision": CandidateDecision(choice="new", candidates=(), basis=candidate_basis([])),
            "after": made.after.model_copy(update={"assignment_id": derived_assignment_id(name)}),
        }
    )
    with pytest.raises(UnsoundCaptureHistory):
        sound_history(
            held,
            [
                create,
                promoted,
                left.model_copy(update={"before": promoted.after}),
            ],
        )


def test_a_link_to_homework_found_by_search_is_held_to_the_row_it_names(
    store: ProjectStateStore,
) -> None:
    target = on_record(store)
    name = joined(store, target)
    held = the_note(store, name)
    create, made = store.capture_history(name)
    assert sound_history(held, [create, made]) is not None

    elsewhere = made.model_copy(
        update={
            "decision": CandidateDecision(
                choice="found", candidates=("assignment-other",), basis="x"
            )
        }
    )
    with pytest.raises(UnsoundCaptureHistory):
        sound_history(held, [create, elsewhere])


def test_the_accepted_press_stops_at_an_unlink(store: ProjectStateStore) -> None:
    target = on_record(store)
    name = joined(store, target)
    events = store.capture_history(name)
    assert accepted_press(events) == events[-1]
    assert isinstance(unlink(store, name, 2, target.assignment_id), CaptureUnlinked)
    assert accepted_press(store.capture_history(name)) is None


def test_choosing_the_homework_the_note_is_joined_to_now_writes_nothing(
    store: ProjectStateStore,
) -> None:
    """A move to the homework the note is joined to already is no move: nothing is
    withdrawn, no unlink and no link are written, and the press is answered as the one
    that stands."""
    target = on_record(store)
    name = joined(store, target)
    before = tables(store)

    again = link(store, name, target, 2, leaving=target.assignment_id)

    assert isinstance(again, CaptureAlreadyPromoted)
    assert again.assignment_id == target.assignment_id
    assert tables(store) == before
