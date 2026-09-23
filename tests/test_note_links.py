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
    SearchPress,
    UnsoundCaptureHistory,
    accepted_press,
    accepted_search_press,
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
    store: ProjectStateStore,
    name: str,
    revision: int,
    leaving: str,
    now: datetime = LATER,
    channel: SourceChannel = HERS,
) -> object:
    return store.unlink_capture(
        name,
        expected_revision=revision,
        leaving=leaving,
        authored_by=STUDENT,
        channel=channel,
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
        choice="found",
        candidates=(target.assignment_id,),
        basis=basis_of(store, target),
        search_press=SearchPress(expected_revision=1, leaving=None),
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


def edited(store: ProjectStateStore, name: str, revision: int, **given: object) -> None:
    held = the_note(store, name)
    changed = store.edit_capture(
        name,
        str(given.get("text", held.text)),
        given.get("course", held.course),  # type: ignore[arg-type]
        given.get("due", held.due_date),  # type: ignore[arg-type]
        expected_revision=revision,
        authored_by=STUDENT,
        channel=HERS,
        now=LATER,
        today=MONDAY,
    )
    assert type(changed).__name__ == "CaptureChanged", changed


# ------------------------------------------------------------------ the accepted press


@pytest.mark.parametrize("later", ["words", "day", "put away"])
def test_the_accepted_link_by_search_is_that_press_again_whatever_the_note_became(
    store: ProjectStateStore, later: str
) -> None:
    """The press that joined the note is recognized by what it carried: the homework, the
    row as shown, and the revision the page showed. The note's words, day, or being put
    away since change nothing about that, and nothing is written for the press again."""
    target = on_record(store)
    name = note(store, due=date(2026, 9, 18))
    basis = basis_of(store, target)
    assert isinstance(link(store, name, target, 1, basis=basis), CapturePromoted)
    if later == "words":
        edited(store, name, 2, text="Other words")
    elif later == "day":
        edited(store, name, 2, due=date(2026, 9, 19))
    else:
        store.archive_capture(
            name, expected_revision=2, authored_by=STUDENT, now=LATER, today=MONDAY
        )
    before = tables(store)

    again = link(store, name, target, 1, basis=basis)
    other_row = link(store, name, target, 1, basis="not what was shown")
    other_page = link(store, name, target, 2, basis=basis)

    assert isinstance(again, CaptureAlreadyPromoted)
    assert again.assignment_id == target.assignment_id
    assert isinstance(other_row, CaptureConflict)
    assert isinstance(other_page, CaptureConflict)
    assert tables(store) == before


@pytest.mark.parametrize("later", ["words", "put away"])
def test_the_accepted_move_is_that_press_again_whatever_the_note_became(
    store: ProjectStateStore, later: str
) -> None:
    old = on_record(store)
    new = on_record(store, course="Spanish", title="Vocabulary list, unit two")
    elsewhere = on_record(store, title="Other work")
    name = joined(store, old)
    basis = basis_of(store, new)
    assert isinstance(
        link(store, name, new, 2, basis=basis, leaving=old.assignment_id), CapturePromoted
    )
    if later == "words":
        edited(store, name, 4, text="Other words")
    else:
        store.archive_capture(
            name, expected_revision=4, authored_by=STUDENT, now=LATER, today=MONDAY
        )
    before = tables(store)

    again = link(store, name, new, 2, basis=basis, leaving=old.assignment_id)
    other_old = link(store, name, new, 2, basis=basis, leaving=elsewhere.assignment_id)
    other_new = link(
        store, name, elsewhere, 2, basis=basis_of(store, elsewhere), leaving=old.assignment_id
    )
    first_press = link(store, name, new, 1, basis=basis)

    assert isinstance(again, CaptureAlreadyPromoted)
    assert isinstance(other_old, CaptureConflict)
    assert isinstance(other_new, CaptureConflict)
    assert isinstance(first_press, CaptureConflict)
    assert tables(store) == before


def test_an_unlink_or_another_link_since_ends_the_accepted_press(
    store: ProjectStateStore,
) -> None:
    """An old press is never revived: after an unlink the note waits and the press is behind
    it; after a link to other homework the old move is a different press."""
    old = on_record(store)
    new = on_record(store, course="Spanish", title="Vocabulary list, unit two")
    third = on_record(store, title="Other work")
    name = note(store, due=date(2026, 9, 18))
    first = basis_of(store, old)
    assert isinstance(link(store, name, old, 1, basis=first), CapturePromoted)
    assert isinstance(unlink(store, name, 2, old.assignment_id), CaptureUnlinked)
    after_unlink = tables(store)
    replayed_link = link(store, name, old, 1, basis=first)
    assert isinstance(replayed_link, CaptureConflict)
    assert tables(store) == after_unlink
    assert accepted_search_press(store.capture_history(name)) is None

    assert isinstance(link(store, name, new, 3, basis=basis_of(store, new)), CapturePromoted)
    move = basis_of(store, third)
    assert isinstance(
        link(store, name, third, 4, basis=move, leaving=new.assignment_id), CapturePromoted
    )
    press = accepted_search_press(store.capture_history(name))
    assert press is not None
    assert (press.target, press.left, press.revision_before, press.basis) == (
        third.assignment_id,
        new.assignment_id,
        4,
        move,
    )
    before = tables(store)
    old_move = link(store, name, new, 3, basis=basis_of(store, new), leaving=old.assignment_id)
    assert isinstance(old_move, CaptureConflict)
    assert tables(store) == before


def test_a_link_by_search_names_exactly_the_one_row_chosen(store: ProjectStateStore) -> None:
    target = on_record(store)
    name = joined(store, target)
    held = the_note(store, name)
    create, made = store.capture_history(name)
    assert made.decision is not None
    widened = made.model_copy(
        update={
            "decision": made.decision.model_copy(
                update={"candidates": (target.assignment_id, "assignment-other")}
            )
        }
    )
    with pytest.raises(UnsoundCaptureHistory):
        sound_history(held, [create, widened])


# ------------------------------------------------------------------ the press kept with the link


def test_a_link_after_a_separate_unlink_is_its_own_press_and_no_old_move(
    store: ProjectStateStore,
) -> None:
    """Unlinking from A and then linking to B are two presses, and the link keeps what it
    asked: no homework left, and the revision its page showed. That link sent again is
    that press again; the move from A to B that was opened before the unlink and never
    accepted is a different press, refused, whatever the events around it look like."""
    old = on_record(store)
    new = on_record(store, course="Spanish", title="Vocabulary list, unit two")
    name = joined(store, old)
    move_basis = basis_of(store, new)
    assert isinstance(unlink(store, name, 2, old.assignment_id), CaptureUnlinked)
    assert isinstance(link(store, name, new, 3, basis=move_basis), CapturePromoted)
    press = accepted_search_press(store.capture_history(name))
    before = tables(store)

    again = link(store, name, new, 3, basis=move_basis)
    old_move = link(store, name, new, 2, basis=move_basis, leaving=old.assignment_id)

    assert press is not None
    assert (press.target, press.left, press.revision_before) == (new.assignment_id, None, 3)
    assert isinstance(again, CaptureAlreadyPromoted)
    assert isinstance(old_move, CaptureConflict)
    assert tables(store) == before
    made = store.capture_history(name)[-1]
    assert made.decision is not None
    assert made.decision.search_press == SearchPress(expected_revision=3, leaving=None)


def test_a_move_keeps_what_it_asked_with_its_link(store: ProjectStateStore) -> None:
    old = on_record(store)
    new = on_record(store, course="Spanish", title="Vocabulary list, unit two")
    name = joined(store, old)
    assert isinstance(
        link(store, name, new, 2, basis=basis_of(store, new), leaving=old.assignment_id),
        CapturePromoted,
    )
    left, made = store.capture_history(name)[-2:]

    assert (left.operation, made.operation) == (UNLINK, LINK)
    assert made.decision is not None
    assert made.decision.search_press == SearchPress(expected_revision=2, leaving=old.assignment_id)


PRESS_NOT_ITS_OWN = {
    "a link that names a revision not the one before it": SearchPress(
        expected_revision=5, leaving=None
    ),
    "a link that says it left homework with no unlink before it": SearchPress(
        expected_revision=1, leaving="assignment-elsewhere"
    ),
}


@pytest.mark.parametrize("damage", sorted(PRESS_NOT_ITS_OWN))
def test_a_press_kept_with_a_link_that_the_events_do_not_bear_out_is_unreadable(
    store: ProjectStateStore, damage: str
) -> None:
    target = on_record(store)
    name = joined(store, target)
    held = the_note(store, name)
    create, made = store.capture_history(name)
    assert made.decision is not None
    widened = made.model_copy(
        update={
            "decision": made.decision.model_copy(update={"search_press": PRESS_NOT_ITS_OWN[damage]})
        }
    )
    with pytest.raises(UnsoundCaptureHistory):
        sound_history(held, [create, widened])


def test_a_link_kept_without_its_press_is_readable_and_no_press_is_proven(
    store: ProjectStateStore,
) -> None:
    """A link written before the press was kept with it reads as it did. No press is proven
    from it, so a link sent again is a conflict rather than a guess; the events are not
    rewritten."""
    target = on_record(store)
    name = joined(store, target)
    basis = basis_of(store, target)
    store._connection.execute(
        "UPDATE capture_events SET decision = json_remove(decision, '$.search_press') "
        "WHERE capture_id = ? AND operation = 'link'",
        (name,),
    )
    store._connection.commit()
    before = tables(store)

    events = store.capture_history(name)
    assert store.sound_capture_history(name) is not None
    assert events[-1].decision is not None
    assert events[-1].decision.search_press is None
    assert accepted_search_press(events) is None
    assert isinstance(link(store, name, target, 1, basis=basis), CaptureConflict)
    assert tables(store) == before


def test_a_link_and_an_unlink_keep_the_way_the_press_came_through(
    store: ProjectStateStore,
) -> None:
    """The tree a press came through is kept with its event, apart from who pressed and
    from the note's own attribution; an event written before that was kept reads as it did,
    with none."""
    target = on_record(store)
    name = note(store, due=date(2026, 9, 18))
    assert isinstance(link(store, name, target, 1), CapturePromoted)
    assert isinstance(
        unlink(store, name, 2, target.assignment_id, channel=SourceChannel.PARENT_ENTRY),
        CaptureUnlinked,
    )
    store._connection.execute(
        "UPDATE capture_events SET channel = NULL WHERE capture_id = ? AND revision = 1", (name,)
    )
    store._connection.commit()

    create, made, left = store.capture_history(name)
    assert create.channel is None
    assert made.channel is SourceChannel.STUDENT_REPORT
    assert left.channel is SourceChannel.PARENT_ENTRY
    assert the_note(store, name).attribution["due_date"].channel is SourceChannel.STUDENT_REPORT


# ------------------------------------------------------------------ the way in held to its kinds


@pytest.mark.parametrize("operation", ["create", "edit"])
def test_a_way_in_kept_on_a_change_that_is_no_search_or_unlink_is_unreadable(
    store: ProjectStateStore, operation: str
) -> None:
    """The tree a press came through belongs to a link by search and to an unlink; a first
    save or an edit that carries one is not one line, in the reading and in the file."""
    target = on_record(store)
    name = joined(store, target)
    edited(store, name, 2, text="Later words")
    held = the_note(store, name)
    events = list(store.capture_history(name))
    place = next(index for index, event in enumerate(events) if event.operation == operation)
    events[place] = events[place].model_copy(update={"channel": SourceChannel.STUDENT_REPORT})
    with pytest.raises(UnsoundCaptureHistory):
        sound_history(held, events)

    store._connection.execute(
        "UPDATE capture_events SET channel = ? WHERE capture_id = ? AND operation = ?",
        (SourceChannel.STUDENT_REPORT.value, name, operation),
    )
    store._connection.commit()
    with pytest.raises(UnsoundCaptureHistory):
        store.sound_capture_history(name)


# ------------------------------------------------------------------ the way in is a person's tree


@pytest.mark.parametrize("school", [SourceChannel.LMS, SourceChannel.EMAIL])
def test_a_way_in_that_is_the_schools_is_never_written_and_unreadable_if_found(
    store: ProjectStateStore, school: SourceChannel
) -> None:
    """The tree a press came through is hers or the family's; the school's channels name
    no tree. A link or an unlink asked to keep one is refused before anything is written,
    and a row that holds one, however it got there, is not one line."""
    target = on_record(store)
    name = joined(store, target)
    other = on_record(store, course="Spanish", title="Vocabulary list, unit two")
    before = tables(store)

    with pytest.raises(CaptureNotSaved):
        store.link_capture(
            name,
            target=other.assignment_id,
            expected_revision=2,
            basis=basis_of(store, other),
            leaving=target.assignment_id,
            shown=row_reader(store),
            authored_by=STUDENT,
            channel=school,
            now=LATER,
            today=MONDAY,
        )
    with pytest.raises(CaptureNotSaved):
        unlink(store, name, 2, target.assignment_id, channel=school)
    assert tables(store) == before

    held = the_note(store, name)
    create, made = store.capture_history(name)
    with pytest.raises(UnsoundCaptureHistory):
        sound_history(held, [create, made.model_copy(update={"channel": school})])
    store._connection.execute(
        "UPDATE capture_events SET channel = ? WHERE capture_id = ? AND operation = 'link'",
        (school.value, name),
    )
    store._connection.commit()
    with pytest.raises(UnsoundCaptureHistory):
        store.sound_capture_history(name)


# ------------------------------------------------------------------ a move is one press


MOVE_PAIR_DAMAGE: dict[str, dict[str, object]] = {
    "another author": {"authored_by": "parent"},
    "another way in": {"channel": SourceChannel.PARENT_ENTRY},
    "another moment": {"occurred_at": LATER + timedelta(minutes=5)},
    "another day": {"occurred_on": MONDAY + timedelta(days=1)},
}


@pytest.mark.parametrize("damage", sorted(MOVE_PAIR_DAMAGE))
def test_a_move_whose_unlink_and_link_are_not_one_press_is_unreadable(
    store: ProjectStateStore, damage: str
) -> None:
    """A move is one press: its unlink and its link are written together, by one person,
    through one tree, at one moment. A line that pairs an unlink with a link differing in
    any of these is not one line, in the reading and from the file."""
    old = on_record(store)
    new = on_record(store, course="Spanish", title="Vocabulary list, unit two")
    name = joined(store, old)
    assert isinstance(
        link(store, name, new, 2, basis=basis_of(store, new), leaving=old.assignment_id),
        CapturePromoted,
    )
    held = the_note(store, name)
    events = list(store.capture_history(name))
    assert store.sound_capture_history(name) is not None
    events[-2] = events[-2].model_copy(update=MOVE_PAIR_DAMAGE[damage])
    with pytest.raises(UnsoundCaptureHistory):
        sound_history(held, events)

    column, value = {
        "another author": ("authored_by", "parent"),
        "another way in": ("channel", SourceChannel.PARENT_ENTRY.value),
        "another moment": ("occurred_at_utc", (LATER + timedelta(minutes=5)).isoformat()),
        "another day": ("occurred_on", (MONDAY + timedelta(days=1)).isoformat()),
    }[damage]
    store._connection.execute(
        f"UPDATE capture_events SET {column} = ? "  # noqa: S608
        "WHERE capture_id = ? AND operation = 'unlink'",
        (value, name),
    )
    store._connection.commit()
    with pytest.raises(UnsoundCaptureHistory):
        store.sound_capture_history(name)
