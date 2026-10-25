# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The homework screenshot review's rules: lists, defaults, units, validation, the preview,
offers, new-slot ids, pieces from readings, and what a change of readings clears.

Rows and pieces are synthetic; pieces from readings are read from synthetic screenshots of
Wren's homework list.
"""

import dataclasses
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import cast

import pytest

from blossom import intake, screenshot_page
from blossom import screenshot_review as review
from blossom.screenshot_review import EarlierPick, Piece, Refusal, SavedRow
from blossom.screenshot_save import confirm_fields
from blossom.screenshots import CardPlace, ScreenshotRow, homework_assembly
from blossom.stores.import_drafts import Carry, StoredReading
from blossom.stores.project_state import AssignmentKind
from tests.support import OBSERVED_AT, homework_answer, homework_card, stored_homework

C, Q, T = "English 7", "Vocab Quiz", "Vocab Test"
MON, TUE, WED, THU, FRI = (date(2026, 10, d) for d in (12, 13, 14, 15, 16))
W1, W2, W3 = "Study list one.", "Study list two.", "Study list three."


def row(
    rid: str,
    due: date | None,
    words: Sequence[str] = (),
    assigned: date | None = None,
    title: str = Q,
    kind: str | None = None,
) -> SavedRow:
    return SavedRow(rid, C, title, due, assigned, frozenset(words), kind=kind)


def piece(
    name: str,
    due: date | None,
    words: Sequence[str] = (),
    assigned: date | None = None,
    key: str | None = None,
    split: bool = False,
    title: str = Q,
) -> Piece:
    return Piece(name, key or name, C, title, due, assigned, frozenset(words), split)


def rows_of(*rows: SavedRow) -> dict[str, SavedRow]:
    return {r.assignment_id: r for r in rows}


def nothing_writes(piece: Piece, assignment_id: str) -> bool:
    return True


def groups_of(*pieces: Piece) -> dict[tuple[str, str], list[Piece]]:
    out: dict[tuple[str, str], list[Piece]] = {}
    for p in pieces:
        out.setdefault(p.pair, []).append(p)
    return out


def checker(
    pieces: Sequence[Piece], rows: Mapping[str, SavedRow]
) -> Callable[[Mapping[str, review.Destination]], review.Validation]:
    def check(mapping: Mapping[str, review.Destination]) -> review.Validation:
        return review.validate(pieces, mapping, rows, [], {}, {})

    return check


# ------------------------------------------------------------------ the reference


def test_a_reference_is_four_characters_when_unique() -> None:
    assert (
        review.reference("assignment-vocab-quiz-4a1c9d2e", ["assignment-vocab-quiz-0000beef"])
        == "ref 9d2e"
    )


def test_a_reference_extends_two_at_a_time_while_two_agree() -> None:
    a, b = "assignment-vocab-quiz-11119d2e", "assignment-vocab-quiz-22229d2e"
    assert review.reference(a, [b]) == "ref 119d2e"
    assert review.reference(b, [a]) == "ref 229d2e"


def test_a_reference_covers_other_id_forms() -> None:
    a = "assignment-from-note-0123456789abcdef"
    b = "assignment-from-note-fedcba9876abcdef"
    assert review.reference(a, [b]) == "ref 89abcdef"
    assert review.reference("assignment-xyz", []) == "ref xyz"


def test_a_reference_falls_back_to_the_bare_whole_id() -> None:
    a, b = "assignment-a-9d2e", "assignment-b-9d2e"
    assert review.reference(a, [b]) == a


# ------------------------------------------------------------------ the destination list


def test_the_window_is_seven_days_either_side() -> None:
    assert review.in_window(TUE, date(2026, 10, 20))
    assert not review.in_window(TUE, date(2026, 10, 21))
    assert review.in_window(TUE, date(2026, 10, 6))
    assert review.in_window(None, None)
    assert not review.in_window(None, TUE)
    assert not review.in_window(TUE, None)


def test_listed_rows_are_same_name_rows_in_the_window_at_any_due_date() -> None:
    rows = rows_of(
        row("x", TUE),
        row("y", date(2026, 10, 19)),
        row("far", date(2026, 10, 30)),
        row("t", TUE, title=T),
    )
    assert [r.assignment_id for r in review.listed(piece("p", FRI), rows.values())] == ["x", "y"]


def test_linked_rows_are_distinct_newest_first_and_of_another_name() -> None:
    rows = rows_of(row("t1", WED, title=T), row("t2", THU, title=T))
    picks = [EarlierPick(C, Q, "t1", 1), EarlierPick(C, Q, "t2", 2), EarlierPick(C, Q, "t1", 3)]
    assert [r.assignment_id for r in review.linked(piece("p", TUE), picks, rows)] == ["t1", "t2"]


def test_entries_come_in_order_and_slots_keep_their_numbers_while_used() -> None:
    rows = rows_of(row("x", TUE), row("t", WED, title=T))
    a, b = piece("a", TUE), piece("b", TUE)
    found = review.entries(b, [a, b], rows, [EarlierPick(C, Q, "t", 1)], {"a": {"new": 3}})
    assert [e.kind for e in found] == [
        "listed",
        "linked",
        "find",
        "new_slot",
        "new_slot",
        "new_slot",
    ]
    assert [e.slot for e in found if e.kind == "new_slot"] == [3, 4, 5]


def test_a_row_found_for_another_piece_is_listed_with_its_value() -> None:
    rows = rows_of(row("other", MON, title="Reading Log"))
    a, b = piece("a", TUE), piece("b", TUE)
    found = {"found": "other", "offer": "basis"}
    listed = review.entries(b, [a, b], rows, [], {"a": found})
    [entry] = [e for e in listed if e.kind == "found_for_another"]
    assert (entry.stands_for, entry.value) == ("other", review.value_json(found))


def test_a_stored_destination_is_offerable_only_while_its_list_could_show_it() -> None:
    rows = rows_of(row("x", TUE), row("far", date(2026, 10, 30)), row("t", WED, title=T))
    p = piece("p", TUE)
    assert review.offerable(p, {"existing": "x"}, rows, [])
    assert not review.offerable(p, {"existing": "gone"}, rows, [])
    assert not review.offerable(p, {"existing": "far"}, rows, [])
    assert review.offerable(p, {"found": "far"}, rows, [])
    assert review.offerable(p, {"linked": "t"}, rows, [EarlierPick(C, Q, "t", 1)])
    assert not review.offerable(p, {"linked": "t"}, rows, [])


# ------------------------------------------------------------------ facts


def test_facts_compare_instruction_words_and_name_other_dates() -> None:
    rows = rows_of(row("x", TUE, [W1]), row("r", WED, [W1, W2]))
    p = piece("p", TUE, [W1, W2])
    beside_x = review.facts(p, rows["x"], rows)
    assert beside_x[0].kind == "lacks_words"
    assert any(f.kind == "also_saved_in" and f.about == "r" for f in beside_x)
    beside_r = review.facts(p, rows["r"], rows)
    assert beside_r[0].kind == "has_words"
    assert any(f.kind == "due_differs" for f in beside_r)


def test_also_saved_in_names_only_the_pieces_list() -> None:
    p = piece("p", WED, [W1])
    far = row("assignment-vocab-quiz-far1", date(2026, 10, 26), [W1])
    near = row("assignment-vocab-quiz-near", MON, [W1])
    other = row("assignment-word-study-l1", THU, [W1], title="Word Study")
    dest = row("assignment-vocab-quiz-dest", WED)
    rows = rows_of(far, near, other, dest)
    picks = [EarlierPick(C, Q, other.assignment_id, 1)]
    about = {f.about for f in review.facts(p, dest, rows, picks) if f.kind == "also_saved_in"}
    assert about == {near.assignment_id, other.assignment_id}
    alone = {f.about for f in review.facts(p, dest, rows) if f.kind == "also_saved_in"}
    assert alone == {near.assignment_id}


# ------------------------------------------------------------------ defaults


def test_already_saved_needs_one_exact_match_that_writes_nothing() -> None:
    rows = rows_of(row("x", TUE, [W1], MON), row("y", WED, [W1], MON))
    p = piece("p", TUE, [W1], MON)
    assert review.defaults([p], groups_of(p), rows, [], {}, nothing_writes) == {
        "p": {"existing": "x"}
    }
    assert review.defaults([p], groups_of(p), rows, [], {}, lambda q, rid: False) == {}


def test_no_already_saved_for_two_exact_matches_or_a_date_difference() -> None:
    p = piece("p", TUE, [W1], MON)
    two = rows_of(row("x", TUE, [W1], MON), row("x2", TUE, [W1], MON))
    assert review.defaults([p], groups_of(p), two, [], {}, nothing_writes) == {}
    moved = rows_of(row("x", WED, [W1], MON))
    assert review.defaults([p], groups_of(p), moved, [], {}, nothing_writes) == {}


def test_no_already_saved_for_two_pieces_onto_one_row() -> None:
    rows = rows_of(row("x", TUE, [W1], MON))
    a, b = piece("a", TUE, [W1], MON), piece("b", TUE, [W1], MON)
    assert review.defaults([a, b], groups_of(a, b), rows, [], {}, nothing_writes) == {}
    assert (
        review.defaults([a], groups_of(a), rows, [], {"b": {"existing": "x"}}, nothing_writes) == {}
    )


def test_a_lone_piece_with_nothing_offered_starts_on_a_new_slot() -> None:
    p = piece("p", TUE, [W1])
    assert review.defaults([p], groups_of(p), {}, [], {}, nothing_writes) == {"p": {"new": 1}}
    a, b = piece("a", TUE), piece("b", TUE)
    assert review.defaults([a, b], groups_of(a, b), {}, [], {}, nothing_writes) == {}
    linked = rows_of(row("t", WED, title=T))
    picks = [EarlierPick(C, Q, "t", 1)]
    assert review.defaults([p], groups_of(p), linked, picks, {}, nothing_writes) == {}


# ------------------------------------------------------------------ units


def test_units_close_over_one_destination_and_one_rows_pieces() -> None:
    a = piece("k|mon", MON, key="k", split=True)
    b = piece("k|tue", TUE, key="k", split=True)
    c, d = piece("c", TUE), piece("d", TUE)
    found = review.units([a, b, c, d], {"c": {"existing": "x"}, "k|mon": {"existing": "x"}})
    assert ("c", "k|mon", "k|tue") in found
    assert ("d",) in found


def test_a_rows_due_piece_shares_its_unit_and_may_share_a_days_destination() -> None:
    mon = piece("k|2026-10-12", WED, [W1], assigned=MON, key="k", split=True)
    tue = piece("k|2026-10-13", WED, [W2], assigned=TUE, key="k", split=True)
    due = piece("k|due", WED, [W1], key="k")
    assert review.units([mon, tue, due], {}) == [("k|2026-10-12", "k|2026-10-13", "k|due")]
    shared = {"k|2026-10-12": {"new": 1}, "k|due": {"new": 1}, "k|2026-10-13": {"new": 2}}
    assert [u.refusals for u in review.validate([mon, tue, due], shared, {}, [], {}, {}).units] == [
        ()
    ]
    both = {"k|2026-10-12": {"new": 1}, "k|2026-10-13": {"new": 1}, "k|due": {"new": 1}}
    [unit] = review.validate([mon, tue, due], both, {}, [], {}, {}).units
    assert [(r.kind, r.pieces) for r in unit.refusals] == [
        ("split_siblings", ("k|2026-10-12", "k|2026-10-13"))
    ]


# ------------------------------------------------------------------ validation


def test_two_split_pieces_in_one_destination_are_refused() -> None:
    a = piece("k|mon", MON, key="k", split=True)
    b = piece("k|tue", TUE, key="k", split=True)
    [unit] = review.validate(
        [a, b], {"k|mon": {"new": 1}, "k|tue": {"new": 1}}, {}, [], {}, {}
    ).units
    assert [r.kind for r in unit.refusals] == ["split_siblings"]


def test_copying_words_another_assignment_holds_on_the_destinations_date_is_refused() -> None:
    rows = rows_of(row("z", WED), row("r", WED, [W1]))
    c = piece("c", WED, [W1])
    [unit] = review.validate([c], {"c": {"existing": "z"}}, rows, [], {}, {}).units
    assert unit.refusals == (Refusal("copy", ("c",), ("z", "r")),)


def test_a_holder_at_another_date_refuses_nothing() -> None:
    rows = rows_of(row("R", WED, [W1, W2]), row("X", TUE))
    c, d = piece("c", TUE, [W1]), piece("d", WED, [W2])
    found = review.validate(
        [c, d], {"c": {"existing": "X"}, "d": {"existing": "R"}}, rows, [], {}, {}
    )
    assert not any(u.refusals for u in found.units)


def test_the_same_date_variant_is_refused() -> None:
    rows = rows_of(row("R", WED, [W1, W2]), row("Z", WED))
    c, d = piece("c", TUE, [W1]), piece("d", WED, [W2])
    found = review.validate(
        [c, d], {"c": {"existing": "Z"}, "d": {"existing": "R"}}, rows, [], {}, {}
    )
    assert [r for u in found.units for r in u.refusals] == [Refusal("copy", ("c",), ("Z", "R"))]


def test_a_copy_is_checked_at_the_date_the_save_will_write() -> None:
    rows = rows_of(row("R", WED, [W1]), row("Z", THU))
    c = piece("c", WED, [W1])
    mapping = {"c": {"existing": "Z"}}
    first = review.validate([c], mapping, rows, [], {}, {})
    [question] = [q for u in first.units for q in u.questions if q.kind == "due"]
    assert not any(u.refusals for u in first.units)
    assert not first.units[0].ready
    kept = review.validate([c], mapping, rows, [], {}, {("due", question.subject): THU.isoformat()})
    assert not any(u.refusals for u in kept.units)
    assert kept.units[0].ready
    moved = review.validate(
        [c], mapping, rows, [], {}, {("due", question.subject): WED.isoformat()}
    )
    assert [r.named for u in moved.units for r in u.refusals] == [("Z", "R")]


def test_saving_in_parts_or_together_refuses_alike() -> None:
    x0, y0 = row("X", TUE), row("Y", THU)
    a, b = piece("a", TUE, [W2]), piece("b", THU, [W2])
    mapping = {"a": {"existing": "X"}, "b": {"existing": "Y"}}
    together = review.validate([a, b], mapping, rows_of(x0, y0), [], {}, {})
    assert not any(u.refusals for u in together.units)
    after = review.validate([a, b], mapping, rows_of(row("X", TUE, [W2]), y0), [], {"a": "X"}, {})
    assert not any(u.refusals for u in after.units)


def test_a_new_slot_is_never_refused_as_a_copy() -> None:
    rows = rows_of(row("r", TUE, [W1]), row("s", TUE, [W2]))
    a, b = piece("a", TUE, [W1]), piece("b", TUE, [W2])
    found = review.validate([a, b], {"a": {"new": 1}, "b": {"new": 1}}, rows, [], {}, {})
    assert not any(u.refusals for u in found.units)


def test_a_due_date_question_holds_the_unit_until_answered() -> None:
    rows = rows_of(row("x", WED))
    p = piece("p", TUE)
    [unit] = review.validate([p], {"p": {"existing": "x"}}, rows, [], {}, {}).units
    assert not unit.ready
    [question] = unit.questions
    assert question.kind == "due"
    answered = {("due", question.subject): WED.isoformat()}
    assert review.validate([p], {"p": {"existing": "x"}}, rows, [], {}, answered).units[0].ready


def test_an_assigned_date_question_asks_where_the_saved_date_differs() -> None:
    rows = rows_of(row("x", TUE, assigned=MON))
    p = piece("p", TUE, assigned=date(2026, 10, 11))
    [unit] = review.validate([p], {"p": {"existing": "x"}}, rows, [], {}, {}).units
    assert [q.kind for q in unit.questions] == ["assigned"]
    assert not unit.ready


def test_a_destination_its_list_cannot_offer_asks_again() -> None:
    [unit] = review.validate([piece("p", TUE)], {"p": {"existing": "gone"}}, {}, [], {}, {}).units
    assert unit.stale == ("p",)
    assert not unit.ready


def test_saved_pieces_are_not_checked() -> None:
    found = review.validate([piece("p", TUE)], {"p": {"existing": "gone"}}, {}, [], {"p": "x"}, {})
    assert found.units == ()


def test_a_piece_with_no_destination_is_not_ready() -> None:
    [unit] = review.validate([piece("p", TUE)], {}, {}, [], {}, {}).units
    assert unit.unanswered == ("p",)
    assert not unit.ready


# ------------------------------------------------------------------ the kind question


def test_a_kind_question_when_kinds_differ_or_differ_from_the_saved_one() -> None:
    a, b = piece("a", WED), piece("b", WED)
    saved = row("assignment-vocab-quiz-r1", WED, kind="HOMEWORK")
    dest = ("id", saved.assignment_id)
    assert review.kind_question(dest, [a, b], saved, {}) is None
    assert review.kind_question(dest, [a, b], saved, {"a": "HOMEWORK"}) is None
    asked = review.kind_question(dest, [a, b], saved, {"a": "TASK"})
    assert asked is not None
    assert (asked.pieces, asked.subject) == (("a", "b"), review.subject_for(dest, ["a", "b"]))
    slot = ("new", C, Q, "1")
    assert review.kind_question(slot, [a, b], None, {"a": "TASK"}) is None
    assert review.kind_question(slot, [a, b], None, {"a": "TASK", "b": "HOMEWORK"}) is not None
    assert review.destination_kind(slot, [a, b], None, {"a": "TASK"}, {}) == ("TASK", True)


def test_the_kind_question_holds_the_unit_without_refusing_and_reaches_the_preview() -> None:
    p = piece("a", WED, [W1])
    saved = row("assignment-vocab-quiz-r1", WED, [W1], kind="HOMEWORK")
    rows = rows_of(saved)
    mapping = {"a": {"existing": saved.assignment_id}}
    kinds = {"a": "TASK"}
    first = review.validate([p], mapping, rows, [], {}, {}, kinds=kinds)
    [unit] = first.units
    [asked] = unit.kind_questions
    assert not unit.ready
    assert not unit.refusals
    [before] = review.preview(first, [p], mapping, rows, {}, (), {}, kinds=kinds)
    assert before.open_questions == ("kind",)
    answers = {("kind", asked.subject): "TASK"}
    answered = review.validate([p], mapping, rows, [], {}, answers, kinds=kinds)
    assert answered.units[0].ready
    [after] = review.preview(answered, [p], mapping, rows, answers, (), {}, kinds=kinds)
    assert not after.nothing_new
    assert after.open_questions == ()
    assert after.digest != before.digest
    assert (after.written_kind, after.kind_from) == ("TASK", "HOMEWORK")
    same = {("kind", asked.subject): "HOMEWORK"}
    [kept] = review.preview(answered, [p], mapping, rows, same, (), {}, kinds=kinds)
    assert kept.nothing_new
    assert kept.digest != after.digest


# ------------------------------------------------------------------ offers


def test_the_move_to_new_slots_clears_two_conflicts_in_one_unit() -> None:
    rows = rows_of(row("X", TUE), row("Y", TUE, [W3]))
    first = piece("k|mon", TUE, key="k", split=True)
    second = piece("k|tue", TUE, key="k", split=True)
    other = piece("s", TUE, [W3])
    pieces = [first, second, other]
    mapping = {"k|mon": {"existing": "X"}, "k|tue": {"existing": "X"}, "s": {"existing": "X"}}
    check = checker(pieces, rows)
    [unit] = [u for u in check(mapping).units if u.refusals]
    assert not any(u.refusals for u in check(review.closure(pieces, mapping, unit, check)).units)
    values = {
        p.name: [e.value for e in review.entries(p, pieces, rows, [], mapping) if e.value]
        for p in pieces
    }
    found = review.offers(pieces, mapping, unit, values, check)
    assert found
    assert len(found) <= review.MOST_OFFERS
    for offer in found:
        changed = dict(mapping)
        for name, value in offer.changes:
            changed[name] = review.value_of(value)
        assert not any(u.refusals for u in check(changed).units)
        assert all(old is not None for _, old in offer.moved_from)


def test_offers_change_nothing_until_posted() -> None:
    rows = rows_of(row("Z", WED), row("R", WED, [W1]))
    c = piece("c", WED, [W1])
    mapping = {"c": {"existing": "Z"}}
    check = checker([c], rows)
    [unit] = check(mapping).units
    before = {k: dict(v) for k, v in mapping.items()}
    values = {"c": [e.value for e in review.entries(c, [c], rows, [], mapping) if e.value]}
    review.offers([c], mapping, unit, values, check)
    assert mapping == before


# ------------------------------------------------------------------ new-slot ids


def test_slot_ids_salt_past_ids_on_record_and_never_depend_on_order() -> None:
    first = review.slot_id(C, Q, ["b", "a"], [])
    assert first == review.slot_id(C, Q, ["a", "b"], [])
    second = review.slot_id(C, Q, ["a", "b"], [first])
    assert second != first
    assert second == review.slot_id(C, Q, ["a", "b"], [first])
    assert review.slot_id(C, Q, ["a"], []) != first
    tag = review.slot_occurrence(C, Q, ["a", "b"], [first])
    assert intake.identity(C, Q, tag) == second


# ------------------------------------------------------------------ the group preview


def test_the_digest_follows_the_destination_and_a_defaults_list() -> None:
    p = piece("p", TUE, [W1], MON)
    mapping = {"p": {"existing": "x"}}
    plain = rows_of(row("x", TUE, [W1], MON))
    more = rows_of(row("x", TUE, [W1, W2], MON))
    [first] = review.preview(
        review.validate([p], mapping, plain, [], {}, {}),
        [p],
        mapping,
        plain,
        {},
        ["p"],
        {"p": ["x"]},
    )
    assert first.nothing_new
    assert first.defaulted_entries == (("p", ("x",)),)
    [changed] = review.preview(
        review.validate([p], mapping, more, [], {}, {}), [p], mapping, more, {}, ["p"], {"p": ["x"]}
    )
    assert changed.digest != first.digest
    [longer] = review.preview(
        review.validate([p], mapping, plain, [], {}, {}),
        [p],
        mapping,
        plain,
        {},
        ["p"],
        {"p": ["x", "y"]},
    )
    assert longer.digest != first.digest


def test_a_new_slots_preview_names_the_words_and_the_overlap() -> None:
    rows = rows_of(row("r", TUE, [W1]))
    a = piece("a", TUE, [W1])
    mapping = {"a": {"new": 1}}
    [entry] = review.preview(
        review.validate([a], mapping, rows, [], {}, {}), [a], mapping, rows, {}, [], {}
    )
    assert (entry.kind, entry.overlap, entry.adds) == ("new", ("r",), (W1,))


def test_the_preview_overlap_names_only_the_pieces_list() -> None:
    p = piece("a", WED, [W1])
    rows = rows_of(
        row("assignment-vocab-quiz-far1", date(2026, 10, 26), [W1]),
        row("assignment-vocab-quiz-near", MON, [W1]),
    )
    mapping = {"a": {"new": 1}}
    [entry] = review.preview(
        review.validate([p], mapping, rows, [], {}, {}), [p], mapping, rows, {}, (), {}
    )
    assert entry.overlap == ("assignment-vocab-quiz-near",)


def one_piece_preview(
    p: Piece,
    saved: SavedRow,
    answers: Mapping[tuple[str, str], str] | None = None,
    kinds: Mapping[str, str] | None = None,
    writes_nothing: Callable[[Piece, str], bool] | None = None,
) -> review.PreviewEntry:
    rows = rows_of(saved)
    mapping = {p.name: {"existing": saved.assignment_id}}
    found = review.validate([p], mapping, rows, [], {}, answers or {}, kinds=kinds)
    [entry] = review.preview(
        found,
        [p],
        mapping,
        rows,
        answers or {},
        (),
        {},
        kinds=kinds,
        writes_nothing=writes_nothing or nothing_writes,
    )
    return entry


def test_the_preview_digest_holds_what_its_lines_read_of_the_saved_row() -> None:
    """A preview's digest changes with each thing its lines read of the saved row a card goes
    to: its kind, class and title, and whether saving writes nothing there; the same inputs give
    the same digest. The row is one found under another name, so a new name keeps it offered."""

    def found(saved: SavedRow, words: Sequence[str], writes: bool) -> review.PreviewEntry:
        p = piece("a", WED, words)
        rows = rows_of(saved)
        mapping = {"a": {"found": saved.assignment_id, "offer": "basis"}}
        [entry] = review.preview(
            review.validate([p], mapping, rows, [], {}, {}),
            [p],
            mapping,
            rows,
            {},
            (),
            {},
            writes_nothing=lambda piece, target: writes,
        )
        return entry

    saved = row("assignment-word-study-r1", WED, [W1], title="Word Study", kind="HOMEWORK")
    plain = found(saved, [W1, W2], True).digest
    changed = [
        dataclasses.replace(saved, kind="TASK"),
        dataclasses.replace(saved, title="Word Study Guide"),
        dataclasses.replace(saved, course="English 9"),
    ]
    same, other = found(saved, [W1], True), found(saved, [W1], False)

    assert found(saved, [W1, W2], True).digest == plain
    assert [found(c, [W1, W2], True).digest != plain for c in changed] == [True] * 3
    assert (same.nothing_new, other.nothing_new) == (True, False)
    assert same.digest != other.digest


def test_a_filled_assigned_date_is_not_nothing_new() -> None:
    entry = one_piece_preview(
        piece("a", WED, [W1], assigned=MON), row("assignment-vocab-quiz-r1", WED, [W1])
    )
    assert entry.assigned == MON
    assert not entry.nothing_new


def test_a_piece_that_fails_the_writes_nothing_test_is_not_nothing_new() -> None:
    saved = row("assignment-vocab-quiz-r1", WED, [W1])
    assert one_piece_preview(
        piece("a", WED, [W1]), saved, writes_nothing=lambda p, t: True
    ).nothing_new
    assert not one_piece_preview(
        piece("a", WED, [W1]), saved, writes_nothing=lambda p, t: False
    ).nothing_new


def test_an_open_question_is_not_nothing_new() -> None:
    entry = one_piece_preview(piece("a", WED, [W1]), row("assignment-vocab-quiz-r1", TUE, [W1]))
    assert entry.open_questions == ("due",)
    assert entry.due == TUE
    assert not entry.nothing_new


def test_the_preview_carries_the_written_kind_and_the_saved_one() -> None:
    saved = row("assignment-vocab-quiz-r1", WED, [W1], kind="TASK")
    subject = review.subject_for(("id", saved.assignment_id), ["a"])
    entry = one_piece_preview(
        piece("a", WED, [W1]), saved, {("kind", subject): "HOMEWORK"}, {"a": "HOMEWORK"}
    )
    assert (entry.written_kind, entry.kind_from) == ("HOMEWORK", "TASK")
    assert not entry.nothing_new


def test_an_open_instruction_question_is_not_nothing_new() -> None:
    p = piece("a", WED, [W1])
    rows = rows_of(row("assignment-vocab-quiz-r1", WED, [W1]))
    mapping = {"a": {"existing": "assignment-vocab-quiz-r1"}}
    found = review.validate([p], mapping, rows, [], {}, {})
    [asked] = review.preview(found, [p], mapping, rows, {}, (), {}, lambda d, m: (3, None))
    [chosen] = review.preview(found, [p], mapping, rows, {}, (), {}, lambda d, m: (3, (W1,)))
    assert not asked.nothing_new
    assert chosen.nothing_new
    assert asked.digest != chosen.digest


# ------------------------------------------------------------------ pieces from readings


def shot(label: str, heading: str, *cards: dict[str, object]) -> StoredReading:
    answer = homework_answer(
        headings=[{"text": heading, "y": 150, "uncertain": None}], cards=list(cards)
    )
    return stored_homework(label, answer)


def assigned_card(words: str, top: int = 200) -> dict[str, object]:
    return homework_card(
        badge="ASSIGNED",
        course=C,
        title=Q,
        instruction=words,
        footer="Due (10/14/2026)",
        y_top=top,
        y_bottom=top + 80,
    )


def due_card(words: str, top: int = 450) -> dict[str, object]:
    return homework_card(
        badge="DUE", course=C, title=Q, instruction=words, footer=None, y_top=top, y_bottom=top + 80
    )


def split_readings() -> list[StoredReading]:
    """An assigned card under Monday and the due card under Wednesday in one screenshot, and the
    same assignment's card under Tuesday in another: one row whose assigned dates disagree."""
    both = homework_answer(
        headings=[
            {"text": "Monday 10/12/2026", "y": 150, "uncertain": None},
            {"text": "Wednesday 10/14/2026", "y": 400, "uncertain": None},
        ],
        cards=[assigned_card(W1), due_card(W1)],
    )
    return [stored_homework("mon-wed", both), shot("tue", "Tuesday 10/13/2026", assigned_card(W2))]


def test_a_row_asks_the_split_question_until_answered() -> None:
    pieces, asking, rows = review.pieces_from(split_readings(), {}, frozenset(), OBSERVED_AT)
    assert pieces == []
    [row_] = asking
    assert [d.day for d in row_.assigned] == [MON, TUE]
    assert rows == asking


def test_a_split_row_gives_a_piece_per_day_and_one_for_the_due_card_dated_by_its_own_cards() -> (
    None
):
    readings = split_readings()
    [row_] = homework_assembly(readings, now=OBSERVED_AT).rows
    pieces, asking, _ = review.pieces_from(readings, {row_.key: "split"}, frozenset(), OBSERVED_AT)
    assert asking == []
    by_name = {p.name.split("|")[1]: p for p in pieces}
    assert set(by_name) == {"2026-10-12", "2026-10-13", "due"}
    assert (by_name["2026-10-12"].assigned, by_name["2026-10-13"].assigned) == (MON, TUE)
    assert by_name["due"].assigned is None
    assert by_name["2026-10-12"].split
    assert not by_name["due"].split
    assert by_name["2026-10-13"].words == frozenset({W2})


def test_one_assignment_takes_the_chosen_assigned_day() -> None:
    readings = split_readings()
    [row_] = homework_assembly(readings, now=OBSERVED_AT).rows
    [p], _, _ = review.pieces_from(
        readings, {row_.key: ["one", TUE.isoformat()]}, frozenset(), OBSERVED_AT
    )
    assert (p.name, p.assigned) == (row_.key, TUE)


def test_a_set_aside_row_gives_no_piece() -> None:
    readings = [shot("wed", "Wednesday 10/14/2026", due_card(W1, top=200))]
    [row_] = homework_assembly(readings, now=OBSERVED_AT).rows
    assert review.pieces_from(readings, {}, frozenset({row_.key}), OBSERVED_AT)[0] == []


def test_typed_corrections_reach_the_piece() -> None:
    readings = [shot("wed", "Wednesday 10/14/2026", due_card("Study list 0ne.", top=200))]
    [row_] = homework_assembly(readings, now=OBSERVED_AT).rows
    fixes = {
        row_.key: {"instruction": W1, "title": "Vocab Quiz 2", "heading": "Thursday 10/15/2026"}
    }
    [p], _, _ = review.pieces_from(readings, {}, frozenset(), OBSERVED_AT, fixes)
    assert (p.title, p.words, p.due) == ("Vocab Quiz 2", frozenset({W1}), THU)
    assert p.reading is not None
    assert {c.asserted_value for c in p.reading.claims} == {THU.isoformat()}


def test_a_typed_date_reads_iso_or_the_portals_form_and_nothing_else() -> None:
    assert review.typed_day("2026-10-15") == THU
    assert review.typed_day("Due (10/15/2026)") == THU
    assert review.typed_day("Thursday 10/15/2026") == THU
    assert review.typed_day("soon") is None
    assert review.typed_day("13/40/2026") is None


def test_the_draft_store_gets_each_rows_piece_names() -> None:
    readings = split_readings()
    [row_] = homework_assembly(readings, now=OBSERVED_AT).rows
    assert review.piece_names(readings, {}) == {}
    names = review.piece_names(readings, {row_.key: json.dumps("split")})
    assert names == {
        row_.key: frozenset({f"{row_.key}|2026-10-12", f"{row_.key}|2026-10-13", f"{row_.key}|due"})
    }


# ------------------------------------------------------------------ a change of readings


@dataclasses.dataclass(frozen=True)
class FakeReading:
    title: str
    occurrence: str | None = None


@dataclasses.dataclass(frozen=True)
class FakeRow:
    key: str
    reading: FakeReading
    sources: tuple[CardPlace, ...]
    assigned: tuple[object, ...] = ()


def as_rows(rows: Sequence[FakeRow]) -> Sequence[ScreenshotRow]:
    """Rows with only the fields the clearing rules read, typed as the assembly's rows."""
    return cast("Sequence[ScreenshotRow]", rows)


def place(sha: str, at: int) -> CardPlace:
    return CardPlace(sha, at)


def test_a_touched_copy_group_clears_its_destinations_and_their_date_answers() -> None:
    a = FakeReading(Q)
    old = [
        FakeRow("k1", a, (place("s1", 0),)),
        FakeRow("k2", a, (place("s1", 1),)),
        FakeRow("k3", FakeReading(T), (place("s1", 2),)),
        FakeRow("kx", FakeReading("Gone"), (place("s9", 0),)),
    ]
    new = [
        FakeRow("k1", a, (place("s1", 0), place("s2", 0))),
        FakeRow("k2", a, (place("s1", 1),)),
        FakeRow("k3", FakeReading(T), (place("s1", 2),)),
    ]
    due = review.subject_for(("new", C, Q, "1"), ["k1", "k3"])
    other_due = review.subject_for(("new", C, T, "1"), ["k3"])
    answers = {
        ("destination", "k1"): "{}",
        ("destination", "k2"): "{}",
        ("destination", "k3"): "{}",
        ("due", due): "2026-10-14",
        ("due", other_due): "2026-10-14",
        ("destination", "kx|2026-10-12"): "{}",
        ("kind", "k3"): "TASK",
    }
    result = review.cleared(as_rows(old), as_rows(new), answers, frozenset())
    assert result.delete == {
        ("destination", "k1"),
        ("destination", "k2"),
        ("due", due),
        ("destination", "kx|2026-10-12"),
    }
    assert result.marks == {"k1", "k2", "kx|2026-10-12"}
    assert not result.aside_insert
    assert not result.aside_delete


def test_a_rows_set_aside_cards_carry_to_the_row_they_make() -> None:
    old = [FakeRow("k1", FakeReading(Q), (place("s1", 0),))]
    new = [FakeRow("k1b", FakeReading(Q, "x"), (place("s1", 0),))]
    result = review.cleared(as_rows(old), as_rows(new), {}, frozenset({"k1"}))
    assert result.aside_insert == {"k1b"}
    assert result.notes == (("k1b", "aside_carried"),)


def test_a_destinations_kind_answer_clears_with_its_piece_and_a_rows_own_stays() -> None:
    old = [FakeRow("k1", FakeReading(Q), (place("s1", 0),))]
    subject = review.subject_for(("id", "assignment-vocab-quiz-r1"), ["k1"])
    answers = {("kind", subject): "TASK", ("kind", "k1"): "TASK"}
    result = review.cleared(as_rows(old), [], answers, frozenset())
    assert result.delete == {("kind", subject)}


def test_the_draft_stores_carry_reads_the_readings_before_and_after() -> None:
    first = shot("wed", "Wednesday 10/14/2026", due_card(W1, top=200))
    second = shot(
        "thu",
        "Thursday 10/15/2026",
        homework_card(
            badge="DUE", course=C, title=T, instruction=W2, footer=None, y_top=200, y_bottom=280
        ),
    )
    [kept_row] = homework_assembly([first], now=OBSERVED_AT).rows
    [gone_row] = homework_assembly([second], now=OBSERVED_AT).rows
    answers = {("destination", kept_row.key): "{}", ("destination", gone_row.key): "{}"}
    result = review.carry([first, second], [first], answers, frozenset())
    assert result == Carry(frozenset({("destination", gone_row.key)}), frozenset({gone_row.key}))


# ------------------------------------------------------------------ which date a correction sets

TYPED_DAY = date(2026, 10, 6)


def faint(day: str, y: int = 150) -> dict[str, object]:
    return {"text": day, "y": y, "uncertain": "faint"}


def routed_shapes() -> dict[str, tuple[list[StoredReading], bool, str, frozenset[str]]]:
    """Each piece shape, as readings whose day headings the reader wasn't sure of, whether its
    row is answered "These are different assignments", which date a day heading's correction
    in saved progress of shape 3 sets, and the unsure fields its own cards ask about: the due date
    (``footer``) for a heading over a due card, the assigned date (``heading``) over an
    assigned card."""
    due_only = stored_homework(
        "due", homework_answer(headings=[faint("Wednesday 10/14/2026")], cards=[due_card(W1, 200)])
    )
    assigned_only = stored_homework(
        "assigned",
        homework_answer(headings=[faint("Monday 10/12/2026")], cards=[assigned_card(W1)]),
    )
    mixed = stored_homework(
        "mixed",
        homework_answer(
            headings=[faint("Monday 10/12/2026"), faint("Wednesday 10/14/2026", 400)],
            cards=[assigned_card(W1), due_card(W1)],
        ),
    )
    other_day = stored_homework(
        "tue", homework_answer(headings=[faint("Tuesday 10/13/2026")], cards=[assigned_card(W2)])
    )
    return {
        "a due card": ([due_only], False, "due", frozenset({"footer"})),
        "an assigned card": ([assigned_only], False, "assigned", frozenset({"heading"})),
        "a mixed row": ([mixed], False, "assigned", frozenset({"footer", "heading"})),
        "a split piece": ([mixed, other_day], True, "assigned", frozenset({"heading"})),
        "the undated piece of a split row": (
            [mixed, other_day],
            True,
            "due",
            frozenset({"footer"}),
        ),
    }


def routed_piece(
    shape: str,
    corrections: Mapping[str, Mapping[str, str | None]] | None = None,
) -> Piece:
    readings, split, _, _ = routed_shapes()[shape]
    rows = homework_assembly(readings, now=OBSERVED_AT).rows
    answers = {r.key: "split" for r in rows} if split else {}
    pieces, asking, _ = review.pieces_from(
        readings,
        answers,
        frozenset(),
        OBSERVED_AT,
        {k: dict(v) for k, v in (corrections or {}).items()},
    )
    assert asking == []
    if shape == "a split piece":
        [found] = [p for p in pieces if p.name.endswith("|2026-10-12")]
    elif shape == "the undated piece of a split row":
        [found] = [p for p in pieces if p.name.endswith("|due")]
    else:
        [found] = pieces
    return found


@pytest.mark.parametrize("shape", list(routed_shapes()))
def test_one_routing_decides_which_date_each_correction_sets(shape: str) -> None:
    """In saved progress of shape 3, a day heading's correction sets the date ``date_target``
    names; a piece's own boxes set exactly the date they name; and an unsure day heading asks
    about the date of the card it is over."""
    _, _, expected, unsure = routed_shapes()[shape]
    piece = routed_piece(shape)
    typed = {piece.key: {"heading": TYPED_DAY.isoformat()}}
    moved = routed_piece(shape, typed)
    own_due = routed_piece(shape, {review.dates_key(piece.name): {"due": TYPED_DAY.isoformat()}})
    own_assigned = routed_piece(
        shape, {review.dates_key(piece.name): {"assigned": TYPED_DAY.isoformat()}}
    )

    assert review.date_target(piece.reading, "heading") == expected
    assert review.date_target(piece.reading, "footer") == "due"
    if expected == "due":
        assert (moved.due, moved.assigned) == (TYPED_DAY, piece.assigned)
    else:
        assert (moved.due, moved.assigned) == (piece.due, TYPED_DAY)
    assert (own_due.due, own_due.assigned) == (TYPED_DAY, piece.assigned)
    assert (own_assigned.due, own_assigned.assigned) == (piece.due, TYPED_DAY)
    assert review.settles_due(piece, typed) == (expected == "due")
    assert review.settles_due(piece, {piece.key: {"footer": TYPED_DAY.isoformat()}})
    assert review.settles_due(piece, {review.dates_key(piece.name): {"due": "2026-10-06"}})
    assert not review.settles_due(piece, {piece.key: {"footer": "10/14"}})
    assert confirm_fields(piece) == unsure


# ------------------------------------------------------- her homework confirmed across a change


def unnamed_shot(label: str, title: str, name: str | None = None) -> StoredReading:
    """A screenshot showing one card of this title and this student name, or none."""
    answer = homework_answer(
        active_student={"name": name, "uncertain": None}, cards=[homework_card(title=title)]
    )
    return stored_homework(label, answer)


NAME_CHANGES: dict[str, tuple[list[StoredReading], list[StoredReading], bool]] = {
    "a screenshot with no name added": (
        [unnamed_shot("one", "Sentence Composing")],
        [unnamed_shot("one", "Sentence Composing"), unnamed_shot("two", "Lab Report")],
        True,
    ),
    "a screenshot with no name read again with other words": (
        [unnamed_shot("one", "Sentence Composing")],
        [unnamed_shot("one", "Lab Report")],
        True,
    ),
    "a named screenshot read again with no name": (
        [unnamed_shot("one", "Sentence Composing"), unnamed_shot("two", "Lab Report", "Wren")],
        [unnamed_shot("one", "Sentence Composing"), unnamed_shot("two", "Lab Report")],
        True,
    ),
    "a named screenshot added": (
        [unnamed_shot("one", "Sentence Composing")],
        [unnamed_shot("one", "Sentence Composing"), unnamed_shot("two", "Lab Report", "Wren")],
        False,
    ),
    "a screenshot with no name removed": (
        [unnamed_shot("one", "Sentence Composing"), unnamed_shot("two", "Lab Report")],
        [unnamed_shot("one", "Sentence Composing")],
        False,
    ),
    "the same screenshots in another order": (
        [unnamed_shot("one", "Sentence Composing"), unnamed_shot("two", "Lab Report")],
        [unnamed_shot("two", "Lab Report"), unnamed_shot("one", "Sentence Composing")],
        False,
    ),
}


@pytest.mark.parametrize("change", list(NAME_CHANGES))
def test_a_change_clears_the_missing_name_confirmation_only_for_a_new_screenshot_with_no_name(
    change: str,
) -> None:
    before, after, asks = NAME_CHANGES[change]
    answers = {("name", "missing"): "confirmed", ("name", "keyed-form-of-wren"): "hers"}
    result = review.carry(before, after, answers, frozenset())
    told = review.change_told(before, after, answers, frozenset())
    unanswered = review.change_told(before, after, {}, frozenset())

    assert (("name", "missing") in result.delete) == asks
    assert ("name", "keyed-form-of-wren") not in result.delete
    assert told.name_asked == asks
    assert (screenshot_page.CHANGE_SAID["name"] in screenshot_page.change_said(told)) == asks
    assert not unanswered.name_asked


# ------------------------------------------------------- corrections, each of the field it names


def mon_wed_row(*cards: dict[str, object]) -> list[StoredReading]:
    """One screenshot of an assigned card under Monday, due Wednesday, and the due card under
    Wednesday: one row, assigned Monday and due Wednesday."""
    return [
        stored_homework(
            "mon-wed",
            homework_answer(
                headings=[
                    {"text": "Monday 10/12/2026", "y": 150, "uncertain": None},
                    {"text": "Wednesday 10/14/2026", "y": 400, "uncertain": None},
                ],
                cards=list(cards) or [assigned_card(W1), due_card(W2)],
            ),
        )
    ]


def one_piece(
    readings: Sequence[StoredReading], corrections: review.Corrections | None = None
) -> Piece:
    [found], _, _ = review.pieces_from(readings, {}, frozenset(), OBSERVED_AT, corrections)
    return found


def test_an_assigned_date_edit_on_a_split_piece_moves_only_that_piece() -> None:
    """The probe of a split row: its Monday piece's assigned date moved to Sunday moves nothing
    of its Tuesday piece or of the due card's piece, whose due date stays Wednesday."""
    readings = split_readings()
    [row_] = homework_assembly(readings, now=OBSERVED_AT).rows
    split = {row_.key: "split"}
    before, _, _ = review.pieces_from(readings, split, frozenset(), OBSERVED_AT)
    monday = f"{row_.key}|2026-10-12"
    sunday = date(2026, 10, 11)
    typed = {review.dates_key(monday): {"assigned": sunday.isoformat()}}
    after, _, _ = review.pieces_from(readings, split, frozenset(), OBSERVED_AT, typed)

    moved = {p.name: (p.due, p.assigned) for p in after}
    kept = {p.name: (p.due, p.assigned) for p in before}
    assert moved[monday] == (WED, sunday)
    assert {n: d for n, d in moved.items() if n != monday} == {
        n: d for n, d in kept.items() if n != monday
    }
    assert moved[f"{row_.key}|due"] == (WED, None)


def test_a_due_card_s_due_box_moves_its_due_date_and_claims() -> None:
    """The probe of a due card joined to its assigned card, inverted: its due box moves the due
    date, and the claims that asserted the date as read, and leaves the assigned date."""
    readings = mon_wed_row()
    read = one_piece(readings)
    moved = one_piece(readings, {review.dates_key(read.name): {"due": THU.isoformat()}})

    assert (read.due, read.assigned) == (WED, MON)
    assert (moved.due, moved.assigned) == (THU, MON)
    assert read.reading is not None
    assert moved.reading is not None
    assert WED.isoformat() in {c.asserted_value for c in read.reading.claims}
    assert WED.isoformat() not in {c.asserted_value for c in moved.reading.claims}
    assert THU.isoformat() in {c.asserted_value for c in moved.reading.claims}
    assert moved.as_read is not None
    assert (moved.as_read.due, moved.as_read.assigned) == (WED, MON)


def test_an_assigned_date_can_be_set_where_none_was_read() -> None:
    readings = [shot("wed", "Wednesday 10/14/2026", due_card(W1, top=200))]
    read = one_piece(readings)
    set_ = one_piece(readings, {review.dates_key(read.name): {"assigned": TUE.isoformat()}})

    assert (read.due, read.assigned) == (WED, None)
    assert (set_.due, set_.assigned) == (WED, TUE)


def test_no_assigned_date_leaves_the_due_date_and_its_claims() -> None:
    """ "No assigned date was given" sets the piece's assigned date to none and changes nothing
    else: its due date, its claims and its words are as read."""
    readings = mon_wed_row()
    read = one_piece(readings)
    none = one_piece(readings, {review.dates_key(read.name): {"assigned": None}})

    assert none.assigned is None
    assert (none.due, none.words, none.title, none.course) == (
        read.due,
        read.words,
        read.title,
        read.course,
    )
    assert none.reading is not None
    assert read.reading is not None
    assert none.reading.claims == read.reading.claims
    assert none.reading.due_date == read.reading.due_date


def test_the_kind_follows_the_corrected_title() -> None:
    """The probe of a title edit: the reading's kind is the corrected title's suggestion, as a
    new assignment saves it."""
    readings = [shot("wed", "Wednesday 10/14/2026", due_card(W1, top=200))]
    read = one_piece(readings)
    slip = one_piece(readings, {read.key: {"title": "Permission slip"}})
    back = one_piece(readings, {read.key: {"title": Q}})

    assert read.reading is not None
    assert slip.reading is not None
    assert back.reading is not None
    assert read.reading.kind == intake.kind_of(Q)
    assert slip.reading.kind == intake.kind_of("Permission slip") == AssignmentKind.TASK
    assert back.reading.kind == read.reading.kind


def test_each_instruction_text_has_its_own_index_and_removing_one_keeps_another() -> None:
    """A row's instruction texts are indexed once each, in reading order; removing text 0 keeps
    text 1's correction, and an untouched text stays as read."""
    readings = [
        *mon_wed_row(assigned_card(W1), due_card(W2)),
        shot("copy", "Monday 10/12/2026", assigned_card(W1)),
    ]
    read = one_piece(readings)
    assert read.as_read is not None
    assert read.as_read.texts == (W1, W2)
    revised = "Study list two, revised."
    both = one_piece(readings, {read.key: {"instruction:0": None, "instruction:1": revised}})
    first = one_piece(readings, {read.key: {"instruction:0": None}})
    second = one_piece(readings, {read.key: {"instruction:1": revised}})

    assert both.words == frozenset({revised})
    assert first.words == frozenset({W2})
    assert second.words == frozenset({W1, revised})


def test_an_old_instruction_correction_replaces_every_text() -> None:
    """Saved progress of shape 3 keeps one instruction correction for the row, which replaces
    every text, as it always has."""
    readings = mon_wed_row()
    read = one_piece(readings)
    replaced = one_piece(readings, {read.key: {"instruction": W3}})

    assert read.words == frozenset({W1, W2})
    assert replaced.words == frozenset({W3})


def wordless_due_card(top: int = 450) -> dict[str, object]:
    """The due card read with no words, the reader unsure of its instructions."""
    card = due_card("", top)
    card.update(instruction=None, uncertain=[{"field": "instruction", "reason": "faint"}])
    return card


def test_words_typed_for_a_card_read_without_any_are_added_to_its_piece_once() -> None:
    """A card read with no words whose instructions the reader wasn't sure of has a field of its
    own, its screenshot and place: words typed there are added to its piece once, stripped,
    beside the row's other words. A text index, the words cleared, a card read with words or
    another screenshot's card adds nothing."""
    readings = mon_wed_row(assigned_card(W1), wordless_due_card())
    read = one_piece(readings)
    assert read.as_read is not None
    [card] = read.as_read.wordless
    field = review.wordless_field(card)
    added = one_piece(readings, {read.key: {field: f"  {W2}  "}})
    twice = one_piece(readings, {read.key: {field: W1}})
    elsewhere = review.wordless_field(CardPlace("0" * 64, card.place))
    others: list[dict[str, str | None]] = [{"instruction:1": W2}, {field: None}, {elsewhere: W2}]
    if card.place != 0:
        others.append({review.wordless_field(CardPlace(card.sha256, 0)): W2})

    assert read.as_read.texts == (W1,)
    assert review.wordless_card(field) == card
    assert added.words == frozenset({W1, W2})
    assert added.reading is not None
    assert [i.text for i in added.reading.instructions] == [W1, W2]
    assert twice.words == frozenset({W1})
    assert [one_piece(readings, {read.key: other}).words for other in others] == [
        frozenset({W1})
    ] * len(others)


def test_already_saved_sees_the_corrected_none() -> None:
    """With "No assigned date was given", "Already saved" matches a saved assignment with no
    assigned date and not one that has one, as for a reading that had none."""
    readings = mon_wed_row(assigned_card(W1))
    read = one_piece(readings)
    none = one_piece(readings, {review.dates_key(read.name): {"assigned": None}})
    plain = row("assignment-vocab-quiz-p1", read.due, sorted(read.words))
    dated = row("assignment-vocab-quiz-d1", read.due, sorted(read.words), assigned=MON)

    def chosen(p: Piece, saved: SavedRow) -> dict[str, dict[str, object]]:
        rows = rows_of(saved)
        return review.defaults([p], groups_of(p), rows, [], {}, nothing_writes)

    assert chosen(none, plain) == {none.name: {"existing": plain.assignment_id}}
    assert chosen(read, plain) == {}
    assert chosen(none, dated) == {}
    assert chosen(read, dated) == {read.name: {"existing": dated.assignment_id}}


def test_a_date_answer_not_offered_is_named_still_to_answer() -> None:
    """The probe of a stale date answer: one whose date the piece's corrected dates don't offer
    leaves its unit open and is named among the questions still open."""
    p = piece("a", WED, [W1])
    saved = row("assignment-vocab-quiz-r1", THU, [W1])
    dest = ("id", saved.assignment_id)
    subject = review.subject_for(dest, ["a"])
    offered = one_piece_preview(p, saved, {("due", subject): WED.isoformat()})
    stale = one_piece_preview(p, saved, {("due", subject): FRI.isoformat()})
    found = review.validate(
        [p],
        {"a": {"existing": saved.assignment_id}},
        rows_of(saved),
        [],
        {},
        {("due", subject): FRI.isoformat()},
    )

    assert offered.open_questions == ()
    assert stale.open_questions == ("due",)
    assert not found.units[0].ready


def test_a_new_slot_s_answers_are_named_by_its_slot_and_old_subjects_read_alike() -> None:
    """A new slot's date and kind answers are named by the slot and its pieces, never its class
    and title, and one stored under its class and title reads as the same subject."""
    names = ["b", "a"]
    dest = ("new", C, Q, "1")
    other = ("new", C, T, "1")
    old = json.dumps([list(dest), sorted(names)], separators=(",", ":"))
    saved = review.subject_for(("id", "assignment-x"), names)

    assert review.subject_for(dest, names) == review.subject_for(other, names)
    assert review.subject_for(dest, names) == '[["new","1"],["a","b"]]'
    assert review.subject_now(old) == review.subject_for(dest, names)
    assert review.subject_now(saved) == saved
    assert review.subject_now("not json") == "not json"
