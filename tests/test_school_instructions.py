"""The school's instructions for an assignment, kept apart from anyone's own note.

The rule is the store's, for every writer: a text kept once for its
assignment, the first one standing, and any other new text waiting on an
explicit choice of which apply, made against the revision it was shown. The
move of school notes out of the old note field runs once, in one transaction,
and a school note found there later is kept for review, never decided.
"""

import json
import pathlib
import sqlite3
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from blossom.reconciliation import SourceChannel
from blossom.school_instructions import (
    INSTRUCTION_MAX_LENGTH,
    WIRE_MAX_LENGTH,
    Card,
    InstructionChoice,
    InstructionChoiceStale,
    InstructionSeen,
    InstructionsNeedAChoice,
    InstructionsSettled,
    InstructionsStanding,
    InstructionState,
    InstructionsUnchanged,
    SchoolInstruction,
    carried_state,
    from_wire,
    settle,
    standing_of,
    to_wire,
    travels_in_words,
)
from blossom.stores.project_state import Assignment, ProjectStateStore, Seed
from blossom.stores.school_instructions import (
    INSTRUCTIONS_OF,
    InstructionsForNoAssignment,
    SchoolInstructionsNeedAChoice,
    UnreadableInstruction,
    instruction_from,
)
from tests.support import fixture_clock, practice_store

NOW = datetime(2026, 9, 23, 22, 0, tzinfo=UTC)
TODAY = date(2026, 9, 23)
LATER = datetime(2026, 9, 24, 22, 0, tzinfo=UTC)
A = "Patterns, if-then statements, first proofs."
B = "Patterns and if-then statements only."
C = "Bring a calculator."


def seen(text: str, card: Card = "assigned", day: date | None = TODAY) -> InstructionSeen:
    return InstructionSeen(text=text, channel=SourceChannel.LMS, card=card, card_day=day)


def kept(
    text: str, state: InstructionState = "current", revision: int = 1, sequence: int = 1
) -> SchoolInstruction:
    return SchoolInstruction(
        sequence=sequence,
        assignment_id="assignment-q1-check-3",
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


def choose(
    revision: int, shown: tuple[str, ...], *applies: str, none: bool = False
) -> InstructionChoice:
    return InstructionChoice(
        shown_revision=revision, shown=shown, applies=frozenset(applies), none_applies=none
    )


# ------------------------------------------------------------------ the rule, on its own


def test_the_first_instruction_stands_without_a_choice() -> None:
    outcome = settle([], [seen(A)], None)

    assert isinstance(outcome, InstructionsSettled)
    assert [(item.text, state) for item, state in outcome.inserted] == [(A, "current")]
    assert outcome.changed == ()
    assert outcome.revision == 1


@pytest.mark.parametrize("state", ["current", "history", "awaiting"])
def test_a_text_already_kept_is_nothing_new_in_any_state_from_any_card(
    state: InstructionState,
) -> None:
    for card in ("assigned", "due"):
        assert isinstance(settle([kept(A, state)], [seen(A, card)], None), InstructionsUnchanged)


def test_no_instruction_changes_nothing() -> None:
    assert isinstance(settle([kept(A)], [], None), InstructionsUnchanged)
    assert isinstance(settle([], [], None), InstructionsUnchanged)


def test_two_different_first_instructions_need_a_choice_in_either_card_order() -> None:
    one = settle([], [seen(A, "assigned"), seen(B, "due")], None)
    other = settle([], [seen(B, "due"), seen(A, "assigned")], None)

    for outcome in (one, other):
        assert isinstance(outcome, InstructionsNeedAChoice)
        assert {item.text for item in outcome.new} == {A, B}
        assert outcome.revision == 0


def test_a_new_text_beside_a_kept_one_needs_a_choice_and_is_never_relegated() -> None:
    outcome = settle([kept(A)], [seen(B)], None)

    assert isinstance(outcome, InstructionsNeedAChoice)
    assert [item.text for item in outcome.kept] == [A]
    assert [item.text for item in outcome.new] == [B]


def test_keeping_the_current_one_still_keeps_the_new_one_as_history() -> None:
    outcome = settle([kept(A)], [seen(B)], choose(1, (A, B), A))

    assert isinstance(outcome, InstructionsSettled)
    assert [(item.text, state) for item, state in outcome.inserted] == [(B, "history")]
    assert outcome.changed == ()
    assert outcome.revision == 2


def test_a_choice_that_already_stands_is_nothing_new_whatever_revision_it_names() -> None:
    standing = [kept(A, "current", 2, 1), kept(B, "history", 2, 2)]

    assert isinstance(settle(standing, [seen(B)], choose(1, (A, B), A)), InstructionsUnchanged)
    assert isinstance(settle(standing, [], choose(7, (A, B), A)), InstructionsUnchanged)


def test_a_different_choice_from_an_earlier_revision_is_stale() -> None:
    """A current, then B chosen, then A chosen again: the texts and states are what the first
    form showed, and its revision is not, so its request for B changes nothing."""
    after_a_again = [kept(A, "current", 3, 1), kept(B, "history", 3, 2)]

    outcome = settle(after_a_again, [], choose(1, (A, B), B))

    assert isinstance(outcome, InstructionChoiceStale)
    assert outcome.revision == 3


def test_a_choice_made_against_another_set_is_stale() -> None:
    outcome = settle([kept(A)], [seen(B)], choose(1, (A, C), A))

    assert isinstance(outcome, InstructionChoiceStale)


def test_none_applies_keeps_everything_as_history() -> None:
    outcome = settle([kept(A)], [seen(B)], choose(1, (A, B), none=True))

    assert isinstance(outcome, InstructionsSettled)
    assert [(item.text, state) for item, state in outcome.inserted] == [(B, "history")]
    assert [(item.text, state) for item, state in outcome.changed] == [(A, "history")]


def test_a_choice_must_answer_and_must_not_contradict_itself() -> None:
    with pytest.raises(ValueError, match="answer"):
        choose(1, (A, B))
    with pytest.raises(ValueError, match="contradict"):
        choose(1, (A, B), A, none=True)
    with pytest.raises(ValueError, match="shown"):
        choose(1, (A,), B)


@pytest.mark.parametrize(
    ("already", "expected"), [([], "current"), ([A], "nothing"), ([B], "awaiting")]
)
def test_a_school_note_found_in_the_old_field_is_placed_by_the_startup_rule(
    already: list[str], expected: str
) -> None:
    assert carried_state([kept(text) for text in already], A) == expected


def test_the_current_set_is_ordered_by_text_whatever_came_first() -> None:
    one = standing_of([kept(C, sequence=1), kept(A, sequence=2), kept(B, "history", sequence=3)])
    other = standing_of([kept(A, sequence=1), kept(C, sequence=2), kept(B, "history", sequence=3)])

    assert one.texts == other.texts == tuple(sorted([A, C]))
    assert [item.text for item in one.history] == [B]
    assert one.revision == 1


# ------------------------------------------------------------------ the store


def target(store: ProjectStateStore) -> str:
    return store.all_assignments()[0].assignment_id


def readings(store: ProjectStateStore, name: str) -> InstructionsStanding:
    found = store.school_instruction_readings([name])
    return found.readable[name]


def test_the_store_keeps_the_first_and_asks_for_the_rest(tmp_path: pathlib.Path) -> None:
    store = practice_store(tmp_path / "record.sqlite3")
    name = target(store)

    first = store.settle_school_instructions(
        name, [seen(A)], None, authored_by="parent", now=NOW, today=TODAY
    )
    again = store.settle_school_instructions(
        name, [seen(A, "due")], None, authored_by="parent", now=LATER, today=TODAY
    )
    asked = store.settle_school_instructions(
        name, [seen(B)], None, authored_by="parent", now=LATER, today=TODAY
    )
    standing = readings(store, name)

    assert isinstance(first, InstructionsSettled)
    assert isinstance(again, InstructionsUnchanged)
    assert isinstance(asked, InstructionsNeedAChoice)
    assert standing.texts == (A,)
    assert standing.revision == 1
    row = standing.current[0]
    assert (row.channel, row.card, row.card_day, row.imported_by) == (
        SourceChannel.LMS,
        "assigned",
        TODAY,
        "parent",
    )
    assert (row.first_seen_at, row.first_seen_on, row.settled_by) == (NOW, TODAY, None)


def test_the_revision_survives_a_change_back_and_refuses_the_old_form(
    tmp_path: pathlib.Path,
) -> None:
    store = practice_store(tmp_path / "record.sqlite3")
    name = target(store)
    store.settle_school_instructions(
        name, [seen(A)], None, authored_by="parent", now=NOW, today=TODAY
    )
    kept_b = store.settle_school_instructions(
        name, [seen(B)], choose(1, (A, B), A), authored_by="parent", now=NOW, today=TODAY
    )
    old_form = choose(2, (A, B), B)
    to_b = store.settle_school_instructions(
        name, [], choose(2, (A, B), B), authored_by="parent", now=NOW, today=TODAY
    )
    to_a = store.settle_school_instructions(
        name, [], choose(3, (A, B), A), authored_by="household", now=LATER, today=TODAY
    )
    before = readings(store, name)

    late = store.settle_school_instructions(
        name, [], old_form, authored_by="parent", now=LATER, today=TODAY
    )

    assert isinstance(kept_b, InstructionsSettled)
    assert isinstance(to_b, InstructionsSettled)
    assert isinstance(to_a, InstructionsSettled)
    assert before.revision == 4
    assert before.texts == (A,)
    assert isinstance(late, InstructionChoiceStale)
    assert readings(store, name) == before
    assert before.current[0].settled_by == "household"


def test_a_choice_for_an_assignment_not_on_record_writes_nothing(tmp_path: pathlib.Path) -> None:
    store = practice_store(tmp_path / "record.sqlite3")

    outcome = store.settle_school_instructions(
        "assignment-nowhere", [seen(A)], None, authored_by="parent", now=NOW, today=TODAY
    )

    assert isinstance(outcome, InstructionsForNoAssignment)
    assert store.school_instruction_readings(["assignment-nowhere"]).readable == {}


def a_row(name: str, note: str | None, mark: SourceChannel | None) -> Assignment:
    origins: dict[str, SourceChannel] = {"record": SourceChannel.LMS}
    if mark is not None:
        origins["note"] = mark
    return Assignment(
        assignment_id=name,
        course="07 Algebra",
        title=name,
        due_date=date(2026, 10, 1),
        dependencies=[],
        reported_submission_status="not_started",
        note=note,
        origins=origins,
    )


def table_rows(store: ProjectStateStore, table: str) -> list[tuple[object, ...]]:
    return store._connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()  # noqa: S608


def test_a_direct_caller_meets_the_same_rule(tmp_path: pathlib.Path) -> None:
    """A school note written with an assignment, unmarked or marked by a school channel, is
    the school's instruction: the first stands, the same one is nothing, and a different one
    is refused whole, rows and claims with it, as a named outcome."""
    store = practice_store(tmp_path / "record.sqlite3")
    store.put_on_record([a_row("check-3", A, None)], {})
    store.put_on_record([a_row("check-3", A, SourceChannel.LMS)], {})
    before = {
        name: table_rows(store, name)
        for name in ("assignments", "date_claims", "school_instructions")
    }

    with pytest.raises(SchoolInstructionsNeedAChoice) as refused:
        store.put_on_record(
            [a_row("check-3", B, None), a_row("other", None, None)],
            {"other": []},
        )

    assert refused.value.outcome.new[0].text == B
    assert {name: table_rows(store, name) for name in before} == before
    row = store.one_assignment("check-3")
    assert row is not None
    assert (row.note, row.origins.get("note")) == (None, None)
    standing = readings(store, "check-3")
    assert standing.texts == (A,)
    assert standing.current[0].channel is None


@pytest.mark.parametrize("mark", [SourceChannel.STUDENT_REPORT, SourceChannel.PARENT_ENTRY])
def test_her_note_and_a_parents_note_stay_in_the_note_field(
    tmp_path: pathlib.Path, mark: SourceChannel
) -> None:
    store = practice_store(tmp_path / "record.sqlite3")
    store.put_on_record([a_row("mine", "My own words about it.", mark)], {})

    row = store.one_assignment("mine")

    assert row is not None
    assert (row.note, row.origins.get("note")) == ("My own words about it.", mark)
    assert store.school_instruction_readings(["mine"]).readable == {}


@pytest.mark.parametrize("writer", ["put_on_record", "upsert_assignments"])
@pytest.mark.parametrize(
    "mark", [["LMS"], {"LMS": 1}, 5, "PORTAL"], ids=["list", "dict", "number", "no-channel"]
)
def test_a_note_mark_that_names_no_channel_is_read_as_none(
    tmp_path: pathlib.Path, mark: object, writer: str
) -> None:
    """A school note whose mark is not text, or names no channel, is kept as the school's
    instruction with no channel, as a note with no mark is."""
    store = practice_store(tmp_path / "record.sqlite3")
    row = a_row("check-3", A, None)
    bent = row.model_copy(update={"origins": {**row.origins, "note": mark}})

    if writer == "put_on_record":
        store.put_on_record([bent], {})
    else:
        store.upsert_assignments([bent])
    standing = readings(store, "check-3")
    saved = store.one_assignment("check-3")

    assert standing.texts == (A,)
    assert standing.current[0].channel is None
    assert saved is not None
    assert (saved.note, saved.origins.get("note")) == (None, None)


def test_a_school_note_written_over_her_note_keeps_hers_and_its_mark(
    tmp_path: pathlib.Path,
) -> None:
    store = practice_store(tmp_path / "record.sqlite3")
    store.put_on_record([a_row("mine", "My own words.", SourceChannel.STUDENT_REPORT)], {})

    store.put_on_record([a_row("mine", A, SourceChannel.LMS)], {})
    row = store.one_assignment("mine")

    assert row is not None
    assert (row.note, row.origins.get("note")) == ("My own words.", SourceChannel.STUDENT_REPORT)
    assert readings(store, "mine").texts == (A,)


def test_the_bulk_reading_is_one_statement_and_says_what_cannot_be_read(
    tmp_path: pathlib.Path,
) -> None:
    store = practice_store(tmp_path / "record.sqlite3")
    store.put_on_record(
        [a_row("one", A, None), a_row("two", C, None), a_row("three", None, None)], {}
    )
    store._connection.execute(
        "UPDATE school_instructions SET state = 'guessed' WHERE assignment_id = 'two'"
    )
    store._connection.commit()
    statements: list[str] = []
    store._connection.set_trace_callback(statements.append)

    found = store.school_instruction_readings(["one", "two", "three"])

    store._connection.set_trace_callback(None)
    assert len([text for text in statements if text.lstrip().upper().startswith("SELECT")]) == 1
    assert found.readable["one"].texts == (A,)
    assert "three" not in found.readable
    assert found.unreadable == frozenset({"two"})


# ------------------------------------------------------------------ the move out of the old field


def old_file(
    tmp_path: pathlib.Path, rows: list[tuple[str, str | None, str | None]]
) -> pathlib.Path:
    """A file as a version before school instructions left it: assignments with notes and
    marks in the old field, and no instruction table."""
    path = tmp_path / "old.sqlite3"
    store = ProjectStateStore.open(path, fixture_clock())
    for name, note, mark in rows:
        store.put_on_record([a_row(name, None, None)], {})
        origins = {"record": "LMS", "course": "LMS"}
        if mark is not None:
            origins["note"] = mark
        store._connection.execute(
            "UPDATE assignments SET note = ?, origins = ? WHERE assignment_id = ?",
            (note, json.dumps(origins), name),
        )
    store._connection.execute("DROP TABLE school_instructions")
    store._connection.commit()
    store.close()
    return path


LEGACY = [
    ("marked", A, "LMS"),
    ("unmarked", C, None),
    ("hers", "Her own words.", "STUDENT_REPORT"),
    ("theirs", "A parent's words.", "PARENT_ENTRY"),
    ("none", None, None),
]


def test_the_move_keeps_every_school_note_once_and_clears_only_those(
    tmp_path: pathlib.Path,
) -> None:
    path = old_file(tmp_path, LEGACY)

    store = ProjectStateStore.open(path, fixture_clock())
    rows = {row.assignment_id: row for row in store.all_assignments()}
    found = store.school_instruction_readings(list(rows)).readable

    assert found["marked"].texts == (A,)
    assert found["marked"].current[0].channel is SourceChannel.LMS
    assert found["unmarked"].texts == (C,)
    assert found["unmarked"].current[0].channel is None
    for name in ("marked", "unmarked"):
        carried = found[name].current[0]
        assert (carried.card, carried.card_day, carried.first_seen_at, carried.first_seen_on) == (
            None,
            None,
            None,
            None,
        )
        assert (carried.imported_by, carried.settled_by, carried.revision) == (None, None, 1)
        assert rows[name].note is None
        assert rows[name].origins == {"record": SourceChannel.LMS, "course": SourceChannel.LMS}
    assert (rows["hers"].note, rows["hers"].origins["note"]) == (
        "Her own words.",
        SourceChannel.STUDENT_REPORT,
    )
    assert (rows["theirs"].note, rows["theirs"].origins["note"]) == (
        "A parent's words.",
        SourceChannel.PARENT_ENTRY,
    )
    assert "hers" not in found
    assert "theirs" not in found
    assert "none" not in found


def test_a_second_start_moves_nothing_and_changes_nothing(tmp_path: pathlib.Path) -> None:
    path = old_file(tmp_path, LEGACY)
    first = ProjectStateStore.open(path, fixture_clock())
    before = {name: table_rows(first, name) for name in ("assignments", "school_instructions")}
    first.close()

    second = ProjectStateStore.open(path, fixture_clock())

    assert {name: table_rows(second, name) for name in before} == before


def test_a_move_cut_short_leaves_the_file_as_it_was_and_the_next_start_completes_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blossom.stores import school_instructions as module

    path = old_file(tmp_path, LEGACY)

    def refuse(*args: object, **kwargs: object) -> None:
        msg = "the file refused"
        raise RuntimeError(msg)

    monkeypatch.setattr(module.SchoolInstructionRecords, "_clear_moved_notes", refuse)
    with pytest.raises(RuntimeError, match="refused"):
        ProjectStateStore.open(path, fixture_clock())
    monkeypatch.undo()
    raw = sqlite3.connect(path)
    tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    notes = raw.execute(
        "SELECT assignment_id, note FROM assignments ORDER BY assignment_id"
    ).fetchall()
    raw.close()

    store = ProjectStateStore.open(path, fixture_clock())

    assert "school_instructions" not in tables
    assert ("marked", A) in notes
    assert ("unmarked", C) in notes
    assert store.school_instruction_readings(["marked"]).readable["marked"].texts == (A,)
    assert len(table_rows(store, "school_instructions")) == 2


def test_a_note_the_move_did_not_keep_is_never_cleared(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The move checks that every note it is about to clear is kept, exactly once, before it
    clears any: a keep that wrote nothing stops the start, and every note stays where it
    was."""
    from blossom.stores import school_instructions as module

    path = old_file(tmp_path, LEGACY)

    def keep_nothing(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(module.SchoolInstructionRecords, "_insert_instruction_locked", keep_nothing)
    with pytest.raises(RuntimeError, match="not kept once"):
        ProjectStateStore.open(path, fixture_clock())
    monkeypatch.undo()
    raw = sqlite3.connect(path)
    notes = raw.execute(
        "SELECT assignment_id, note FROM assignments ORDER BY assignment_id"
    ).fetchall()
    raw.close()

    assert ("marked", A) in notes
    assert ("unmarked", C) in notes


@pytest.mark.parametrize(
    ("before", "found", "expected_current", "expected_awaiting"),
    [
        (None, A, (A,), ()),
        (A, A, (A,), ()),
        (A, B, (A,), (B,)),
    ],
)
def test_a_school_note_found_in_the_old_field_after_the_move(
    tmp_path: pathlib.Path,
    before: str | None,
    found: str,
    expected_current: tuple[str, ...],
    expected_awaiting: tuple[str, ...],
) -> None:
    """Whatever wrote it, a school note in the old field after the move is placed by the
    startup rule: it stands when nothing is kept, is nothing when kept already, and waits
    for a parent's review beside what stands otherwise."""
    path = tmp_path / "record.sqlite3"
    store = ProjectStateStore.open(path, fixture_clock())
    store.put_on_record([a_row("check-3", before, None)], {})
    store._connection.execute(
        "UPDATE assignments SET note = ?, origins = ? WHERE assignment_id = 'check-3'",
        (found, json.dumps({"record": "LMS", "note": "LMS"})),
    )
    store._connection.commit()
    store.close()

    reopened = ProjectStateStore.open(path, fixture_clock())
    standing = reopened.school_instruction_readings(["check-3"]).readable["check-3"]
    row = reopened.one_assignment("check-3")

    assert standing.texts == expected_current
    assert tuple(item.text for item in standing.awaiting) == expected_awaiting
    assert row is not None
    assert (row.note, row.origins.get("note")) == (None, None)


def test_a_seed_note_is_the_schools_first_instruction(tmp_path: pathlib.Path) -> None:
    seed = Seed(
        assignments=[
            a_row("seeded", A, None),
            a_row("hers", "Her words.", SourceChannel.STUDENT_REPORT),
        ],
        claims={},
        student_reports=[],
    )

    store = ProjectStateStore.initialize(tmp_path / "blank.sqlite3", fixture_clock(), lambda: seed)
    seeded = store.one_assignment("seeded")
    hers = store.one_assignment("hers")

    assert seeded is not None
    assert hers is not None
    assert seeded.note is None
    assert store.school_instruction_readings(["seeded"]).readable["seeded"].texts == (A,)
    assert hers.note == "Her words."


def test_a_note_beside_an_instruction_that_cannot_be_read_stays_and_the_rest_move(
    tmp_path: pathlib.Path,
) -> None:
    """A start never fails over a kept instruction it cannot read: the school note of that
    assignment stays in the old field, where the pages still show it, and every other school
    note moves as it would."""
    path = tmp_path / "record.sqlite3"
    store = ProjectStateStore.open(path, fixture_clock())
    store.put_on_record([a_row("bent", A, SourceChannel.LMS), a_row("plain", None, None)], {})
    store._connection.execute("UPDATE school_instructions SET state = 'bent'")
    for name, note in (("bent", C), ("plain", B)):
        store._connection.execute(
            "UPDATE assignments SET note = ?, origins = ? WHERE assignment_id = ?",
            (note, json.dumps({"record": "LMS", "note": "EMAIL"}), name),
        )
    store._connection.commit()
    store.close()

    reopened = ProjectStateStore.open(path, fixture_clock())
    bent = reopened.one_assignment("bent")
    plain = reopened.one_assignment("plain")
    found = reopened.school_instruction_readings(["bent", "plain"])

    assert bent is not None
    assert plain is not None
    assert (bent.note, bent.origins.get("note")) == (C, SourceChannel.EMAIL)
    assert found.unreadable == frozenset({"bent"})
    assert (plain.note, plain.origins.get("note")) == (None, None)
    assert found.readable["plain"].texts == (B,)
    assert found.readable["plain"].current[0].channel == SourceChannel.EMAIL


# ------------------------------------------------------------------ the words on the wire


@pytest.mark.parametrize(
    "words",
    [
        "One line.",
        "First paragraph.\nSecond paragraph.",
        "A line as a browser sent it.\r\nAnd the next.",
        'Say "only the odd ones" and it\'s done.',
        "Draw the \U0001f33c by <b>Friday</b> & bring it.",
    ],
)
def test_the_words_travel_on_the_wire_exactly(words: str) -> None:
    """Carried as one JSON string in ASCII, the words hold no line break a browser could
    rewrite, and come back as they left, every character included."""
    carried = to_wire(words)

    assert carried.isascii()
    assert "\n" not in carried
    assert "\r" not in carried
    assert from_wire(carried) == words
    assert from_wire(carried.replace("\n", "\r\n")) == words


@pytest.mark.parametrize(
    "value",
    [
        "plain words",
        "123",
        '["a list"]',
        '""',
        '"   "',
        '"\\ud800"',
        '"a" and more',
        ' "leading space"',
        '"' + "a" * (INSTRUCTION_MAX_LENGTH + 1) + '"',
        '"' + "\\u0061" * (WIRE_MAX_LENGTH // 6) + '"',
    ],
    ids=[
        "no-json",
        "a-number",
        "a-list",
        "empty",
        "blank",
        "half-a-pair",
        "text-after",
        "space-before",
        "too-many-words",
        "too-long-on-the-wire",
    ],
)
def test_a_value_the_page_did_not_write_is_no_words(value: str) -> None:
    assert from_wire(value) is None


# ------------------------------------------------------------------ a choice of words not there


def test_a_choice_of_words_neither_kept_nor_new_is_refused_and_a_true_retry_still_stands() -> None:
    """With every instruction retired, a choice of words never kept cannot stand, so it is
    never taken for a result that already stands; a choice whose whole result stands is
    still nothing new, whatever revision it names."""
    retired = [kept(A, "history", 3, 1), kept(B, "history", 3, 2)]

    unknown = settle(retired, [], choose(3, (A, B, "Never kept."), "Never kept."))
    retried = settle(retired, [], choose(1, (A, B), none=True))

    assert isinstance(unknown, InstructionChoiceStale)
    assert retried == InstructionsUnchanged(3)


# ------------------------------------------------------------------ what the store writes and reads


@pytest.mark.parametrize(
    ("text", "channel"),
    [
        ("", SourceChannel.LMS),
        ("   ", SourceChannel.LMS),
        ("\ud800", SourceChannel.LMS),
        ("Fine words.", SourceChannel.STUDENT_REPORT),
        ("Fine words.", SourceChannel.PARENT_ENTRY),
    ],
    ids=["empty", "blank", "half-a-pair", "her-report", "a-parents-entry"],
)
def test_an_instruction_is_the_schools_words_or_nothing(text: str, channel: SourceChannel) -> None:
    with pytest.raises(ValueError, match="instruction"):
        InstructionSeen(text, channel)


def test_the_store_refuses_before_it_writes_what_it_could_not_read_back(
    tmp_path: pathlib.Path,
) -> None:
    """A moment without its offset, a day that is a moment, or someone the store does not
    know is refused before the transaction begins, for a new row and for a change to a kept
    one alike, and the file is left as it was with nothing held open."""
    store = practice_store(tmp_path / "record.sqlite3")
    name = target(store)
    before = table_rows(store, "school_instructions")
    naive = datetime(2026, 9, 23, 22, 0)  # noqa: DTZ001 - a moment without its offset
    for moment, day, who in (
        (naive, TODAY, "parent"),
        (NOW, NOW, "parent"),
        (NOW, TODAY, "teacher"),
    ):
        with pytest.raises(ValueError, match="school instruction"):
            store.settle_school_instructions(
                name,
                [seen(A)],
                None,
                authored_by=who,  # type: ignore[arg-type]
                now=moment,
                today=day,
            )
        assert not store._connection.in_transaction
    assert table_rows(store, "school_instructions") == before

    store.settle_school_instructions(
        name, [seen(A)], None, authored_by="parent", now=NOW, today=TODAY
    )
    kept_rows = table_rows(store, "school_instructions")
    with pytest.raises(ValueError, match="school instruction"):
        store.settle_school_instructions(
            name,
            [seen(B, "due")],
            choose(1, (A, B), B),
            authored_by="parent",
            now=naive,
            today=TODAY,
        )

    assert table_rows(store, "school_instructions") == kept_rows
    assert not store._connection.in_transaction


def test_a_moment_is_written_in_utc_and_read_back_as_the_same_moment(
    tmp_path: pathlib.Path,
) -> None:
    store = practice_store(tmp_path / "record.sqlite3")
    name = target(store)
    evening = datetime(2026, 9, 23, 15, 0, tzinfo=timezone(timedelta(hours=-7)))

    store.settle_school_instructions(
        name, [seen(A)], None, authored_by="parent", now=evening, today=TODAY
    )
    written = store._connection.execute(
        "SELECT first_seen_at_utc FROM school_instructions"
    ).fetchone()[0]

    assert written == "2026-09-23T22:00:00+00:00"
    assert readings(store, name).current[0].first_seen_at == evening


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("sequence", 0),
        ("sequence", -1),
        ("revision", 1.5),
        ("revision", 0),
        ("revision", -1),
        ("text", ""),
        ("text", "   "),
        ("text", b"\xff"),
        ("channel", "STUDENT_REPORT"),
        ("channel", "PARENT_ENTRY"),
        ("channel", b"LMS"),
        ("card", "late"),
        ("card_day", "2026-9-1"),
        ("card_day", "20260901"),
        ("first_seen_at_utc", "2026-09-23T22:00:00"),
        ("first_seen_at_utc", "2026-09-23T23:00:00+01:00"),
        ("settled_at_utc", "2026-09-23T17:00:00-05:00"),
        ("first_seen_on", 20260923),
        ("state", b"current"),
        ("imported_by", "teacher"),
        ("settled_by", "school"),
    ],
)
def test_a_row_the_store_never_writes_makes_only_its_assignment_unavailable(
    tmp_path: pathlib.Path, column: str, value: object
) -> None:
    """Every value is read as the type the store wrote it, and nothing is made to fit: the
    damaged row's assignment is unavailable, the other reads as it was, and what is said of
    the damage never holds the school's words."""
    store = practice_store(tmp_path / "record.sqlite3")
    first, second = sorted(row.assignment_id for row in store.all_assignments())
    for name, words in ((first, A), (second, B)):
        store.settle_school_instructions(
            name, [seen(words)], None, authored_by="parent", now=NOW, today=TODAY
        )
    store._connection.execute(
        f"UPDATE school_instructions SET {column} = ? WHERE assignment_id = ?",  # noqa: S608
        (value, first),
    )
    store._connection.commit()

    found = store.school_instruction_readings([first, second])
    row = store._connection.execute(INSTRUCTIONS_OF, (first,)).fetchone()
    with pytest.raises(UnreadableInstruction) as refused:
        instruction_from(row)

    assert found.unreadable == frozenset({first})
    assert found.readable[second].texts == (B,)
    assert A not in str(refused.value)


def test_the_bulk_reading_asks_the_file_only_for_the_assignments_named(
    tmp_path: pathlib.Path,
) -> None:
    store = practice_store(tmp_path / "record.sqlite3")
    first, second = sorted(row.assignment_id for row in store.all_assignments())
    for name, words in ((first, A), (second, B)):
        store.settle_school_instructions(
            name, [seen(words)], None, authored_by="parent", now=NOW, today=TODAY
        )
    statements: list[str] = []
    store._connection.set_trace_callback(statements.append)

    found = store.school_instruction_readings([first])

    store._connection.set_trace_callback(None)
    asked = [text for text in statements if "FROM school_instructions" in text]
    assert len(asked) == 1
    assert f"json_each('[\"{first}\"]')" in asked[0]
    assert set(found.readable) == {first}


# ------------------------------------------------------------------ blank notes from before


def test_a_blank_school_note_is_no_instruction_and_never_stops_a_start(
    tmp_path: pathlib.Path,
) -> None:
    """A blank school note says nothing: it stays in the field as it was, a start and every
    start after it go on, and the school notes with words move once, line breaks and all."""
    path = old_file(
        tmp_path,
        [
            ("empty", "", None),
            ("spaces", "   ", "LMS"),
            ("words", "Line one.\nLine two.", "EMAIL"),
            ("hers", "Her own words.", "STUDENT_REPORT"),
            ("none", None, None),
        ],
    )
    first = ProjectStateStore.open(path, fixture_clock())
    after_first = {name: table_rows(first, name) for name in ("assignments", "school_instructions")}
    first.close()

    second = ProjectStateStore.open(path, fixture_clock())
    rows = {row.assignment_id: row for row in second.all_assignments()}
    found = second.school_instruction_readings(list(rows))
    after_second = {
        name: table_rows(second, name) for name in ("assignments", "school_instructions")
    }

    assert after_second == after_first
    assert (rows["empty"].note, rows["spaces"].note) == ("", "   ")
    assert rows["words"].note is None
    assert rows["hers"].note == "Her own words."
    assert set(found.readable) == {"words"}
    assert found.readable["words"].texts == ("Line one.\nLine two.",)
    assert found.readable["words"].current[0].channel is SourceChannel.EMAIL


def test_a_blank_seed_note_is_no_instruction(tmp_path: pathlib.Path) -> None:
    seed = Seed(
        assignments=[
            a_row("empty", "", None),
            a_row("spaces", "  ", SourceChannel.LMS),
            a_row("words", A, None),
        ],
        claims={},
        student_reports=[],
    )

    store = ProjectStateStore.initialize(tmp_path / "blank.sqlite3", fixture_clock(), lambda: seed)
    rows = {row.assignment_id: row for row in store.all_assignments()}
    found = store.school_instruction_readings(list(rows))

    assert set(found.readable) == {"words"}
    assert (rows["empty"].note, rows["spaces"].note) == ("", "  ")


# ------------------------------------------------------------------ marks the store cannot read


@pytest.mark.parametrize("mark", [[], {"LMS": 1}, 7], ids=["a-list", "an-object", "a-number"])
def test_a_note_mark_that_is_no_text_leaves_its_note_and_never_stops_a_start(
    tmp_path: pathlib.Path, mark: object
) -> None:
    """The origins can be read and their note mark cannot: the note stays where it is, with its
    origins as they were, and the start goes on to move every other school note."""
    path = old_file(tmp_path, [("bent", A, None), ("plain", B, "EMAIL")])
    bent = json.dumps({"record": "LMS", "note": mark})
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(
            "UPDATE assignments SET origins = ? WHERE assignment_id = 'bent'", (bent,)
        )
    connection.close()

    store = ProjectStateStore.open(path, fixture_clock())
    left = store._connection.execute(
        "SELECT note, origins FROM assignments WHERE assignment_id = 'bent'"
    ).fetchone()
    found = store.school_instruction_readings(["bent", "plain"])

    assert left == (A, bent)
    assert found.readable.keys() == {"plain"}
    assert found.readable["plain"].texts == (B,)


def test_a_school_note_written_over_a_note_mark_that_is_no_text_is_kept(
    tmp_path: pathlib.Path,
) -> None:
    store = practice_store(tmp_path / "record.sqlite3")
    name = target(store)
    store._connection.execute(
        "UPDATE assignments SET note = ?, origins = ? WHERE assignment_id = ?",
        ("Old words.", json.dumps({"note": []}), name),
    )
    store._connection.commit()

    store.put_on_record([a_row(name, C, SourceChannel.LMS)], {})

    assert readings(store, name).texts == (C,)


# ------------------------------------------------------------------ notes longer than a paste

LONGEST_PASTE = [INSTRUCTION_MAX_LENGTH, INSTRUCTION_MAX_LENGTH + 1]


@pytest.mark.parametrize("length", LONGEST_PASTE, ids=["as-long-as-a-paste", "longer"])
def test_a_school_note_longer_than_a_paste_moves_whole_once_beside_an_ordinary_one(
    tmp_path: pathlib.Path, length: int
) -> None:
    """A note had no length of its own before the school's instructions were kept apart. One
    as long as a paste, or longer, moves whole with its channel, beside an ordinary one; the
    field is cleared once it is kept, and a second start adds nothing."""
    words = "x" * (length - 1) + "."
    path = old_file(tmp_path, [("long", words, "LMS"), ("plain", B, None)])
    first = ProjectStateStore.open(path, fixture_clock())
    found = first.school_instruction_readings(["long", "plain"]).readable
    moved = first.one_assignment("long")
    after_first = {name: table_rows(first, name) for name in ("assignments", "school_instructions")}
    first.close()

    second = ProjectStateStore.open(path, fixture_clock())
    after_second = {
        name: table_rows(second, name) for name in ("assignments", "school_instructions")
    }

    assert found["long"].texts == (words,)
    assert found["long"].current[0].channel is SourceChannel.LMS
    assert found["plain"].texts == (B,)
    assert moved is not None
    assert moved.note is None
    assert after_second == after_first


@pytest.mark.parametrize("length", LONGEST_PASTE, ids=["as-long-as-a-paste", "longer"])
def test_a_seed_note_longer_than_a_paste_is_kept_whole(tmp_path: pathlib.Path, length: int) -> None:
    words = "y" * length
    seed = Seed(
        assignments=[a_row("long", words, SourceChannel.EMAIL), a_row("plain", A, None)],
        claims={},
        student_reports=[],
    )

    store = ProjectStateStore.initialize(tmp_path / "seed.sqlite3", fixture_clock(), lambda: seed)
    found = store.school_instruction_readings(["long", "plain"]).readable

    assert found["long"].texts == (words,)
    assert found["long"].current[0].channel is SourceChannel.EMAIL
    assert found["plain"].texts == (A,)


def test_only_words_a_paste_can_bring_travel_in_a_form_in_words() -> None:
    """An instruction is the school's words however long a note from before held them; a form
    carries in words only those that fit in a paste, and never longer ones."""
    longer = "z" * (INSTRUCTION_MAX_LENGTH + 1)

    assert InstructionSeen(longer, SourceChannel.LMS).text == longer
    assert travels_in_words("z" * INSTRUCTION_MAX_LENGTH)
    assert not travels_in_words(longer)
    assert from_wire(to_wire(longer)) is None


# ------------------------------------------------------------------ nothing new, named in full


def test_a_choice_that_stands_is_nothing_new_only_when_it_names_what_is_kept() -> None:
    """A retry that names every kept instruction and asks for what stands is nothing new,
    whatever revision it names; one that names words never kept, or leaves a kept one out,
    was made against other facts, and is refused as stale."""
    rows = [kept(A, "current", 2, 1), kept(B, "history", 2, 2)]

    assert settle(rows, [], choose(9, (A, B), A)) == InstructionsUnchanged(2)
    assert isinstance(settle(rows, [], choose(9, (A, B, C), A)), InstructionChoiceStale)
    assert isinstance(settle(rows, [], choose(2, (A, B, C), A)), InstructionChoiceStale)
    assert isinstance(settle(rows, [], choose(2, (A,), A)), InstructionChoiceStale)
