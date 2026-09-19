"""Her account of turning work in: a chain of events kept apart from her account of the work.

Three things are read from one chain and never stand in for one another: the
head, which a save is compared with; the event that began the state standing
now, which gives the day a page says and, later, a place in her list; and the
event her words came from. The store cases write through the store, read
back through another connection where that matters, and never through a page:
no page reads this table yet.
"""

import pathlib
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from blossom.authored_text import TextRefused
from blossom.hand_in import (
    NEEDS_HAND_IN,
    NOT_REQUIRED,
    TURNED_IN,
    UNKNOWN,
    BrokenChain,
    HandInAlreadySaved,
    HandInConflict,
    HandInEvent,
    HandInProjection,
    HandInSaved,
    HandInState,
    HandInUndone,
    already_undone,
    project,
)
from blossom.noticing import planning_digest, read_everything, week_from
from blossom.stores.project_state import (
    DONE,
    CouldNotSave,
    ProjectStateStore,
    UnknownAssignment,
    UnknownHandIn,
)
from tests.support import PRACTICE, PRACTICE_LOG, a_row, fixture_clock, practice_store

MONDAY = date(2026, 9, 14)
AT = datetime(2026, 9, 14, 21, 0, tzinfo=UTC)


def day(offset: int) -> date:
    return MONDAY + timedelta(days=offset)


def event(
    name: str,
    state: HandInState | None,
    *,
    on: int = 0,
    after: str | None = None,
    undoes: str | None = None,
    action: str | None = None,
    note: str | None = None,
) -> HandInEvent:
    return HandInEvent(
        event_id=name,
        assignment_id=PRACTICE,
        operation="report" if undoes is None else "undo",
        state=state,
        next_action=action,
        note=note,
        reported_at=AT + timedelta(days=on),
        reported_on=day(on),
        previous_event_id=after,
        undone_event_id=undoes,
    )


def said(
    store: ProjectStateStore,
    state: HandInState,
    *,
    head: str | None,
    on: int = 0,
    action: str | None = None,
    note: str | None = None,
    assignment_id: str = PRACTICE,
) -> HandInSaved | HandInAlreadySaved | HandInConflict:
    return store.record_hand_in(
        assignment_id,
        state,
        action,
        note,
        expected_head=head,
        now=AT + timedelta(days=on),
        today=day(on),
    )


def saved(outcome: object) -> HandInEvent:
    assert isinstance(outcome, HandInSaved), outcome
    return outcome.event


def standing(store: ProjectStateStore, assignment_id: str = PRACTICE) -> HandInProjection:
    return project(assignment_id, store.hand_in_chains([assignment_id]).get(assignment_id, []))


# ------------------------------------------------------------------ one event


def test_a_report_says_a_state_and_an_undo_names_what_it_follows() -> None:
    with pytest.raises(ValidationError, match="says no state"):
        event("a", None)
    with pytest.raises(ValidationError, match="names an event to take back"):
        HandInEvent(**{**event("a", TURNED_IN).model_dump(), "undone_event_id": "b"})
    with pytest.raises(ValidationError, match="must follow the event it takes back"):
        event("b", None, after="x", undoes="a")
    assert event("b", None, after="a", undoes="a").state is None


def test_a_next_action_goes_only_with_still_to_turn_in_and_a_note_with_any_state() -> None:
    kept = event("a", NEEDS_HAND_IN, action="  Put it in my folder ", note="for Tuesday\r\n")
    assert (kept.next_action, kept.note) == ("Put it in my folder", "for Tuesday")
    for state in (UNKNOWN, TURNED_IN, NOT_REQUIRED):
        assert event("a", state, note="I think so").note == "I think so"
        with pytest.raises(ValidationError, match="only with still to turn in"):
            event("a", state, action="Put it in my folder")
    with pytest.raises(ValidationError, match="no state for it to stand with"):
        event("b", None, after="a", undoes="a", note="left behind")


def test_an_event_holds_the_same_words_whoever_made_it() -> None:
    """A seed file or a direct caller is held to what a form is."""
    with pytest.raises(ValidationError):
        event("a", NEEDS_HAND_IN, action="one\ntwo")
    with pytest.raises(ValidationError):
        event("a", TURNED_IN, note="bell \x07")
    with pytest.raises(ValidationError):
        event("a", TURNED_IN, note="half \ud800")
    with pytest.raises(ValidationError):
        event("a", TURNED_IN, note="x" * 501)
    with pytest.raises(ValidationError):
        event("a", NEEDS_HAND_IN, action="x" * 201)


# ------------------------------------------------------------- what stands now


def test_nothing_said_is_not_a_report_of_not_sure() -> None:
    nothing = project(PRACTICE, [])
    assert (nothing.state, nothing.reported_on, nothing.head_id) == (None, None, None)

    not_sure = project(PRACTICE, [event("a", UNKNOWN, on=1, note="I forget")])
    assert (not_sure.state, not_sure.reported_on, not_sure.note) == (UNKNOWN, day(1), "I forget")
    assert not_sure.undo_event_id == "a"


def test_an_edit_in_the_same_state_keeps_the_day_she_entered_it() -> None:
    chain = [
        event("a", NEEDS_HAND_IN, on=0, action="Put it in my folder", note="first words"),
        event("b", NEEDS_HAND_IN, on=2, after="a", action="Hand it to her", note="first words"),
        event("c", NEEDS_HAND_IN, on=3, after="b", action="Hand it to her", note="new words"),
    ]

    after_the_action = project(PRACTICE, chain[:2])
    after_the_note = project(PRACTICE, chain)

    assert after_the_action.reported_on == day(0)
    assert after_the_action.next_action == "Hand it to her"
    assert after_the_action.note_updated_on is None
    assert after_the_note.reported_on == day(0)
    assert after_the_note.entered_by == "a"
    assert after_the_note.note_updated_on == day(3)
    assert after_the_note.head_id == "c"


def test_leaving_a_state_and_coming_back_begins_a_new_period() -> None:
    chain = [
        event("a", NEEDS_HAND_IN, on=0, note="first"),
        event("b", TURNED_IN, on=1, after="a", note="first"),
        event("c", NEEDS_HAND_IN, on=4, after="b", note="first"),
    ]

    again = project(PRACTICE, chain)

    assert (again.state, again.reported_on, again.entered_by) == (NEEDS_HAND_IN, day(4), "c")
    assert again.note_updated_on is None


def test_an_undo_restores_the_period_the_words_and_the_day_that_stood_before() -> None:
    chain = [
        event("a", NEEDS_HAND_IN, on=0, action="Put it in my folder", note="first"),
        event("b", NEEDS_HAND_IN, on=2, after="a", action="Put it in my folder", note="second"),
        event("c", TURNED_IN, on=3, after="b"),
        event(
            "d",
            NEEDS_HAND_IN,
            on=5,
            after="c",
            undoes="c",
            action="Put it in my folder",
            note="second",
        ),
    ]

    restored = project(PRACTICE, chain)

    assert (restored.state, restored.reported_on, restored.entered_by) == (
        NEEDS_HAND_IN,
        day(0),
        "a",
    )
    assert (restored.next_action, restored.note) == ("Put it in my folder", "second")
    assert restored.note_updated_on == day(2)
    assert restored.head_id == "d"
    assert restored.undo_event_id is None
    assert [row.event.event_id for row in restored.history] == ["a", "b", "c", "d"]


def test_undoing_the_first_report_leaves_no_report_and_no_day() -> None:
    chain = [event("a", UNKNOWN, on=1), event("b", None, on=2, after="a", undoes="a")]

    nothing = project(PRACTICE, chain)

    assert (nothing.state, nothing.reported_on, nothing.note_updated_on) == (None, None, None)
    assert nothing.head_id == "b"


def test_the_stored_order_decides_never_the_clock() -> None:
    chain = [
        event("a", NEEDS_HAND_IN, on=3),
        event("b", TURNED_IN, on=0, after="a"),
        event("c", TURNED_IN, on=0, after="b", note="same moment"),
    ]

    assert project(PRACTICE, chain).state == TURNED_IN
    assert project(PRACTICE, chain).reported_on == day(0)
    assert project(PRACTICE, chain).head_id == "c"


def test_a_chain_whose_links_do_not_follow_its_order_is_not_read_as_anything() -> None:
    forked = [event("a", NEEDS_HAND_IN), event("b", TURNED_IN), event("c", UNKNOWN, after="a")]
    ring = [event("a", NEEDS_HAND_IN, after="b"), event("b", TURNED_IN, after="a")]
    other = [event("a", NEEDS_HAND_IN).model_copy(update={"assignment_id": PRACTICE_LOG})]

    for chain in (forked, ring, other):
        with pytest.raises(BrokenChain):
            project(PRACTICE, chain)


# -------------------------------------------------------------------- the store


@pytest.fixture
def store(tmp_path: pathlib.Path) -> ProjectStateStore:
    return practice_store(tmp_path / "record.sqlite3")


def test_a_first_report_is_kept_and_found_again_after_a_restart(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "record.sqlite3"
    store = practice_store(path)

    first = saved(said(store, NEEDS_HAND_IN, head=None, action="Put it in my folder"))
    store.close()
    again = ProjectStateStore.open(path, fixture_clock())

    assert first.sequence is not None
    assert standing(again).head_id == first.event_id
    assert standing(again).next_action == "Put it in my folder"
    assert standing(again, PRACTICE_LOG).state is None


def test_a_file_from_before_gains_the_table_and_keeps_its_rows(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "record.sqlite3"
    before = practice_store(path)
    before.report_status(PRACTICE, DONE, "all of it", expected_head=None, now=AT, today=MONDAY)
    before._connection.execute("DROP TABLE hand_in_events")
    before._connection.commit()
    before.close()

    store = ProjectStateStore.open(path, fixture_clock())

    assert store.hand_in_chains() == {}
    assert {item.assignment_id for item in store.all_assignments()} == {PRACTICE, PRACTICE_LOG}
    assert len(store.student_reports(PRACTICE)) == 1
    saved(said(store, TURNED_IN, head=None))
    indexes = store._connection.execute("PRAGMA index_list(hand_in_events)").fetchall()
    assert "hand_in_events_by_assignment" in {str(row[1]) for row in indexes}


def test_the_same_words_again_are_already_saved_whatever_head_the_page_held(
    store: ProjectStateStore,
) -> None:
    first = saved(said(store, NEEDS_HAND_IN, head=None, action="Put it in my folder", note="a\nb"))

    lost_response = said(
        store, NEEDS_HAND_IN, head=None, action=" Put it in my folder", note="a\r\nb "
    )
    stale_page = said(
        store, NEEDS_HAND_IN, head=first.event_id, action="Put it in my folder", note="a\nb"
    )

    assert isinstance(lost_response, HandInAlreadySaved)
    assert isinstance(stale_page, HandInAlreadySaved)
    assert lost_response.head == first
    assert len(store.hand_in_chains([PRACTICE])[PRACTICE]) == 1


def test_an_action_left_in_the_form_is_not_kept_with_another_state(
    store: ProjectStateStore,
) -> None:
    """The form shows every field without scripts, so the old action can come along."""
    first = saved(said(store, NEEDS_HAND_IN, head=None, action="Put it in my folder"))

    turned_in = saved(said(store, TURNED_IN, head=first.event_id, action="Put it in my folder"))
    repeat = said(store, TURNED_IN, head=turned_in.event_id, action="Something else")

    assert turned_in.next_action is None
    assert isinstance(repeat, HandInAlreadySaved)


def test_a_page_whose_head_has_moved_on_is_refused_with_what_stands(
    store: ProjectStateStore,
) -> None:
    first = saved(said(store, NEEDS_HAND_IN, head=None))
    second = saved(said(store, TURNED_IN, head=first.event_id, on=1))

    stale = said(store, NOT_REQUIRED, head=first.event_id, on=2)
    no_head_named = said(store, NOT_REQUIRED, head=None, on=2)

    assert isinstance(stale, HandInConflict)
    assert stale.head == second
    assert isinstance(no_head_named, HandInConflict)
    assert len(store.hand_in_chains([PRACTICE])[PRACTICE]) == 2


def test_a_head_that_is_not_this_assignments_proves_nothing(store: ProjectStateStore) -> None:
    other = saved(said(store, TURNED_IN, head=None, assignment_id=PRACTICE_LOG))

    with pytest.raises(UnknownHandIn):
        said(store, TURNED_IN, head=other.event_id)
    with pytest.raises(UnknownHandIn):
        said(store, TURNED_IN, head="no-such-event")
    with pytest.raises(UnknownHandIn):
        store.undo_hand_in(PRACTICE, other.event_id, now=AT, today=MONDAY)
    with pytest.raises(UnknownAssignment):
        said(store, TURNED_IN, head=None, assignment_id="not-on-record")
    assert PRACTICE not in store.hand_in_chains()


def test_words_the_record_will_not_keep_are_refused_before_anything_is_read(
    store: ProjectStateStore,
) -> None:
    for action, note in (("x" * 201, None), ("one\ntwo", None), (None, "x" * 501), (None, "\x07")):
        with pytest.raises(TextRefused):
            said(store, NEEDS_HAND_IN, head=None, action=action, note=note)
    assert store.hand_in_chains() == {}


def test_undo_takes_back_the_head_and_a_repeat_is_refused_as_already_undone(
    store: ProjectStateStore,
) -> None:
    first = saved(said(store, NEEDS_HAND_IN, head=None, action="Put it in my folder", note="mine"))
    second = saved(said(store, TURNED_IN, head=first.event_id, on=1))

    undone = store.undo_hand_in(PRACTICE, second.event_id, now=AT, today=day(2))
    replay = store.undo_hand_in(PRACTICE, second.event_id, now=AT, today=day(2))
    stale = store.undo_hand_in(PRACTICE, first.event_id, now=AT, today=day(2))

    assert isinstance(undone, HandInUndone)
    assert (undone.event.state, undone.event.next_action, undone.event.note) == (
        NEEDS_HAND_IN,
        "Put it in my folder",
        "mine",
    )
    assert standing(store).reported_on == day(0)
    assert isinstance(replay, HandInConflict)
    assert already_undone(replay, second.event_id)
    assert isinstance(stale, HandInConflict)
    assert not already_undone(stale, first.event_id)
    assert len(store.hand_in_chains([PRACTICE])[PRACTICE]) == 3


def test_a_repeat_after_something_else_was_said_is_only_a_change(store: ProjectStateStore) -> None:
    first = saved(said(store, TURNED_IN, head=None))
    undone = store.undo_hand_in(PRACTICE, first.event_id, now=AT, today=day(1))
    assert isinstance(undone, HandInUndone)
    saved(said(store, NOT_REQUIRED, head=undone.event.event_id, on=2))

    replay = store.undo_hand_in(PRACTICE, first.event_id, now=AT, today=day(3))

    assert isinstance(replay, HandInConflict)
    assert not already_undone(replay, first.event_id)


def test_a_write_the_file_refuses_leaves_nothing_and_says_so(
    store: ProjectStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forked(*args: object) -> None:
        msg = "the chain forked"
        raise RuntimeError(msg)

    monkeypatch.setattr(store, "_confirm_hand_in_head_locked", forked)

    with pytest.raises(CouldNotSave):
        said(store, TURNED_IN, head=None)

    monkeypatch.undo()
    assert store.hand_in_chains() == {}
    assert not store._connection.in_transaction
    saved(said(store, TURNED_IN, head=None))


def test_the_writer_is_reserved_before_the_head_is_read(tmp_path: pathlib.Path) -> None:
    """Another connection holding the write lock makes the save fail whole, not read and race."""
    path = tmp_path / "record.sqlite3"
    store = practice_store(path)
    store._connection.execute("PRAGMA busy_timeout = 100")
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")

    with pytest.raises(CouldNotSave):
        said(store, TURNED_IN, head=None)

    other.execute("ROLLBACK")
    other.close()
    assert store.hand_in_chains() == {}
    saved(said(store, TURNED_IN, head=None))


def test_her_two_accounts_never_move_each_other_and_a_plan_never_sees_this_one(
    store: ProjectStateStore,
) -> None:
    before = planning_digest(week_from(read_everything(store, store), MONDAY))

    first = saved(said(store, NEEDS_HAND_IN, head=None, action="Put it in my folder", note="mine"))
    saved(said(store, TURNED_IN, head=first.event_id, on=1))
    after_hand_in = planning_digest(week_from(read_everything(store, store), MONDAY))
    store.report_status(PRACTICE, DONE, None, expected_head=None, now=AT, today=MONDAY)

    assert after_hand_in == before
    assert store.student_report_chains([PRACTICE])[PRACTICE][0].status == DONE
    assert standing(store).state == TURNED_IN
    assert len(store.hand_in_chains([PRACTICE])[PRACTICE]) == 2


@pytest.mark.parametrize("count", [1, 20, 200])
def test_every_chain_is_read_in_one_statement(count: int, tmp_path: pathlib.Path) -> None:
    store = ProjectStateStore.open(tmp_path / "record.sqlite3", fixture_clock())
    names = [f"math-{number:03d}" for number in range(count)]
    store.put_on_record([a_row(name, f"Set {name}") for name in names], {})
    for name in names[::2]:
        saved(said(store, NEEDS_HAND_IN, head=None, assignment_id=name))
    seen: list[str] = []
    store._connection.set_trace_callback(seen.append)

    chains = store.hand_in_chains(names)
    store._connection.set_trace_callback(None)

    assert list(chains) == names[::2]
    assert len(seen) == 1
    assert store.hand_in_chains([]) == {}
