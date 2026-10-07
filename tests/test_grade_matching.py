# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Matching a report's rows to her results across captures, each value's status against what is
current, and the current values per target.

A capture saved before resolves its rows through its own acceptance records first. Another
capture's row matches a result without asking only on equal, unique evidence; otherwise a parent
answers. A value equal to the current one is Saved, one equal to a value a newer report replaced
matches an earlier saved value, and only a New or Changed value can be selected.
"""

import dataclasses
import sqlite3
from collections.abc import Collection

from blossom.grades.draft import GradeReportDraft, Presence, capture_key
from blossom.grades.identity import name_form_key
from blossom.grades.review import (
    TERM_KEY,
    GradeReportSaved,
    GradeReview,
    ItemStatus,
    MatchAnswer,
    QuestionKind,
    ReturnReason,
    ReviewItem,
    ReviewReturned,
    SaveOutcome,
)
from blossom.grades.text_reader import read_grade_report
from blossom.stores.project_state import ProjectStateStore
from tests.support import FIXTURES, fixture_clock, grade_answers, save_grade

REPORT = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")
"""Wren's synthetic report: Biology, 2026-2027, T1, four categories and four results."""
KEY = name_form_key(b"5" * 64)
CELL = (
    "| Cell Diagram             | 7.0     | 10.0    | 70.0    | Missing    | 09/26   "
    "| 0.0       | 0.0       |             | 1.0        |          |"
)
OSMOSIS = (
    "| Osmosis with Potato Slices         | 31.0    | 40.0    | 77.5    | Valid      | 10/02   "
    "| 0.0       | 0.0       |             | 1.0        |          |"
)
IXL = "| IXL | 9.0 | 10.0 | 90.0 | Valid | | 0.0 | 0.0 | | 1.0 | |"
CELL_SCORE = ("| Cell Diagram             | 7.0 ", "| Cell Diagram             | 8.0 ")
RENAMED = ("| Seed Germination Log |", "| Seed Germination Journal |")
MOVED = (
    "| 30.0    | 90.0    | Valid      | 09/24   |",
    "| 30.0    | 90.0    | Valid      | 09/25   |",
)
CELL_DUE = ("| Missing    | 09/26   |", "| Missing    | 09/29   |")


def variant(*changes: tuple[str, str], text: str = REPORT) -> GradeReportDraft:
    for old, new in changes:
        assert old in text
        text = text.replace(old, new)
    reading = read_grade_report(text)
    assert reading.draft is not None, reading.not_read
    return reading.draft


A = variant()
B = variant(CELL_SCORE)
"""A newer report: Cell Diagram's score changed from 7.0 to 8.0."""
CLIPPED = REPORT[: REPORT.index("|          |   |                   |")]
"""A copy of the report that stops after its first category: no term grade."""
IXL_REPORT = REPORT.replace(CELL, f"{CELL}\n{IXL}\n{IXL}")
"""Two IXL rows alike in every cell of their evidence: two results."""
IXL_OTHER = IXL_REPORT.replace(f"{IXL}\n{IXL}", f"{IXL}\n{IXL.replace('9.0', '8.0', 1)}")
"""Another capture of it: the second IXL row's score changed from 9.0 to 8.0."""


def in_memory() -> ProjectStateStore:
    return ProjectStateStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())


def review_of(store: ProjectStateStore, draft: GradeReportDraft) -> GradeReview:
    return store.review_grade_report(draft, capture_key(draft), key=KEY)


def save(
    store: ProjectStateStore,
    draft: GradeReportDraft,
    review: GradeReview | None = None,
    *,
    matches: Collection[MatchAnswer] = (),
    selection: Collection[str] | None = None,
) -> SaveOutcome:
    """A parent's save with every setup question answered, ``matches`` as the matching answers,
    and the ready values plus the answered rows selected unless ``selection`` says otherwise."""
    review = review or review_of(store, draft)
    answers = dataclasses.replace(grade_answers(review), matches=tuple(matches))
    if selection is None:
        selection = review.ready | {answer.row_key for answer in matches}
    return save_grade(store, draft, key=KEY, review=review, answers=answers, selection=selection)


def saved(outcome: SaveOutcome) -> GradeReportSaved:
    assert isinstance(outcome, GradeReportSaved), outcome
    return outcome


def row(review: GradeReview, title: str, occurrence: int = 1) -> ReviewItem:
    (found,) = [
        item
        for item in review.rows
        if f'"{title}"' in item.key and item.key.endswith(f",{occurrence}]")
    ]
    return found


def same(item: ReviewItem, choice: int = 0) -> MatchAnswer:
    """The answer "Same assignment" to ``item``'s question, naming its ``choice``-th candidate."""
    assert item.question is not None
    return MatchAnswer(item.key, item.question.ids, item.question.ids[choice])


def different(item: ReviewItem) -> MatchAnswer:
    """The answer "A different assignment" to ``item``'s question."""
    assert item.question is not None
    return MatchAnswer(item.key, item.question.ids, None)


def class_of(store: ProjectStateStore) -> str:
    (class_id,) = store._connection.execute("SELECT class_id FROM grade_classes").fetchone()
    return str(class_id)


def results(store: ProjectStateStore) -> set[str]:
    return {str(one[0]) for one in store._connection.execute("SELECT result_id FROM grade_results")}


def text_of(value: object, field: str) -> str:
    cells = value.cells  # type: ignore[attr-defined]
    presence, text = cells[field]
    assert isinstance(presence, Presence)
    return str(text)


# ------------------------------------------------------------- A, then newer B, then A again


def test_a_then_newer_b_then_a_again_offers_nothing_and_b_stays_current() -> None:
    store = in_memory()
    first = saved(save(store, A))
    cell = row(review_of(store, A), "Cell Diagram").result_id or ""
    review_b = review_of(store, B)

    assert row(review_b, "Cell Diagram").status is ItemStatus.CHANGED
    assert row(review_b, "Cell Diagram").result_id == cell
    assert review_b.ready == {row(review_b, "Cell Diagram").key}
    newer = saved(save(store, B, review_b))
    again = review_of(store, A)

    assert (newer.added, newer.updated, newer.already_saved) == (0, 1, 8)
    assert row(again, "Cell Diagram").status is ItemStatus.MATCHES_EARLIER
    assert {item.status for item in again.items} == {ItemStatus.SAVED, ItemStatus.MATCHES_EARLIER}
    assert again.ready == frozenset()
    assert [item.result_id for item in again.rows] == [item.result_id for item in review_b.rows]
    assert len(results(store)) == 4
    current = store.current_values(class_of(store), "T1")
    assert text_of(current.results[cell], "points") == "8.0"
    assert current.results[cell].report_id == newer.report_id
    assert current.results[cell].order == 2
    assert current.term is not None
    assert current.term.report_id == first.report_id
    assert first.report_id != newer.report_id


def test_a_again_after_b_renamed_a_row_and_moved_a_date_matches_its_own_results() -> None:
    store = in_memory()
    saved(save(store, A))
    own = {item.key: item.result_id for item in review_of(store, A).rows}
    renaming = variant(RENAMED, MOVED, CELL_SCORE)
    review_b = review_of(store, renaming)
    seed = row(review_b, "Seed Germination Journal")
    microscope = row(review_b, "Microscope Practice")

    assert seed.status is ItemStatus.NEEDS_ANSWER
    assert seed.question is not None
    assert seed.question.kind is QuestionKind.RENAMED
    assert microscope.question is not None
    assert microscope.question.kind is QuestionKind.DUE_CHANGED
    assert seed.key not in review_b.ready
    assert microscope.key not in review_b.ready
    newer = saved(save(store, renaming, review_b, matches=[same(seed), same(microscope)]))
    again = review_of(store, A)

    assert (newer.added, newer.updated) == (0, 3)
    assert len(results(store)) == 4
    assert {item.key: item.result_id for item in again.rows} == own
    assert all(item.question is None for item in again.items)
    assert again.ready == frozenset()
    assert {
        row(again, title).status for title in ("Seed Germination Log", "Microscope Practice")
    } == {ItemStatus.MATCHES_EARLIER}
    current = store.current_values(class_of(store), "T1")
    seed_result = row(again, "Seed Germination Log").result_id or ""
    assert text_of(current.results[seed_result], "assignment") == "Seed Germination Journal"


# ------------------------------------------------------------- matching across captures


def test_ixl_twins_keep_their_results_across_a_re_import_and_ask_which_in_another() -> None:
    store = in_memory()
    twins = variant(text=IXL_REPORT)
    saved(save(store, twins))
    again = review_of(store, twins)
    first, second = row(again, "IXL", 1), row(again, "IXL", 2)

    assert (first.status, second.status) == (ItemStatus.SAVED, ItemStatus.SAVED)
    assert first.result_id != second.result_id
    other = variant(text=IXL_OTHER)
    review = review_of(store, other)
    one, two = row(review, "IXL", 1), row(review, "IXL", 2)

    for item in (one, two):
        assert item.status is ItemStatus.NEEDS_ANSWER
        assert item.question is not None
        assert item.question.kind is QuestionKind.WHICH
        assert set(item.question.ids) == {first.result_id, second.result_id}
        shown = {text_of(candidate.last, "points") for candidate in item.question.candidates}
        assert shown == {"9.0"}
        assert {text_of(c.last, "status") for c in item.question.candidates} == {"Valid"}
    keep_first = MatchAnswer(one.key, one.question.ids, first.result_id)  # type: ignore[union-attr]
    keep_second = MatchAnswer(two.key, two.question.ids, second.result_id)  # type: ignore[union-attr]
    outcome = saved(
        save(store, other, review, matches=[keep_first, keep_second], selection={two.key})
    )

    assert (outcome.added, outcome.updated) == (0, 1)
    assert len(results(store)) == 6
    current = store.current_values(class_of(store), "T1")
    assert text_of(current.results[second.result_id or ""], "points") == "8.0"
    assert text_of(current.results[first.result_id or ""], "points") == "9.0"


def test_a_due_date_changed_and_answered_same_keeps_the_result_id() -> None:
    store = in_memory()
    saved(save(store, A))
    cell = row(review_of(store, A), "Cell Diagram").result_id
    moved = variant(CELL_DUE)
    review = review_of(store, moved)
    item = row(review, "Cell Diagram")

    assert item.question is not None
    assert item.question.kind is QuestionKind.DUE_CHANGED
    (candidate,) = item.question.candidates
    assert candidate.result_id == cell
    assert [text_of(candidate.last, field) for field in ("points", "status", "due")] == [
        "7.0",
        "Missing",
        "09/26",
    ]
    outcome = saved(save(store, moved, review, matches=[same(item)]))

    assert (outcome.added, outcome.updated) == (0, 1)
    assert dict(outcome.accepted)[item.key] == cell
    assert len(results(store)) == 4
    decided = store._connection.execute(
        "SELECT row_key, result_id, how FROM grade_match_decisions WHERE report_id = ?",
        (outcome.report_id,),
    ).fetchall()
    assert (item.key, cell, "answer") in decided
    assert sorted(how for _, _, how in decided) == ["answer", "exact", "exact", "exact"]
    observed = store._connection.execute(
        "SELECT result_id FROM grade_result_observations WHERE report_id = ?", (outcome.report_id,)
    ).fetchall()
    assert observed == [(cell,)]
    current = store.current_values(class_of(store), "T1")
    assert text_of(current.results[cell or ""], "due") == "09/29"


def test_a_rename_answered_same_keeps_the_result_id_and_a_different_one_adds_a_result() -> None:
    store = in_memory()
    saved(save(store, A))
    osmosis = row(review_of(store, A), "Osmosis with Potato Slices").result_id
    renamed = variant((OSMOSIS[:38], "| Osmosis Lab                        |"))
    review = review_of(store, renamed)
    item = row(review, "Osmosis Lab")

    assert item.question is not None
    assert item.question.kind is QuestionKind.RENAMED
    assert item.question.ids == (osmosis,)
    assert saved(save(store, renamed, review, matches=[same(item)])).updated == 1
    assert len(results(store)) == 4
    elsewhere = in_memory()
    saved(save(elsewhere, A))
    other = review_of(elsewhere, renamed)
    outcome = saved(save(elsewhere, renamed, other, matches=[different(row(other, "Osmosis Lab"))]))
    assert (outcome.added, outcome.updated) == (1, 0)
    assert len(results(elsewhere)) == 5


def test_a_stored_decision_disagreeing_with_an_exact_match_asks() -> None:
    """Cell Diagram moves to 09/29 and is answered the same; a copy back at 09/26 is answered a
    different assignment. A fourth copy at 09/26 equals that one's evidence exactly, but the
    first save decided the same evidence for the other result, so a parent is asked."""
    store = in_memory()
    saved(save(store, A))
    first_results = results(store)
    cell = row(review_of(store, A), "Cell Diagram").result_id
    moved = variant(CELL_DUE, CELL_SCORE)
    review = review_of(store, moved)
    saved(save(store, moved, review, matches=[same(row(review, "Cell Diagram"))]))
    back = variant(("| Cell Diagram             | 7.0 ", "| Cell Diagram             | 6.0 "))
    review = review_of(store, back)
    saved(save(store, back, review, matches=[different(row(review, "Cell Diagram"))]))
    (new_cell,) = results(store) - first_results

    fourth = variant(("| Cell Diagram             | 7.0 ", "| Cell Diagram             | 5.0 "))
    item = row(review_of(store, fourth), "Cell Diagram")

    assert item.status is ItemStatus.NEEDS_ANSWER
    assert item.question is not None
    assert set(item.question.ids) == {cell, new_cell}


def test_a_renamed_row_asks_about_a_result_no_row_has_resolved_to() -> None:
    """Cell Diagram moves to 09/29, and Cell Drawing comes due 09/26 in its category. Either
    row may be the Cell Diagram result until one resolves to it, so both ask, and no two
    answers can name it."""
    store = in_memory()
    saved(save(store, A))
    cell = row(review_of(store, A), "Cell Diagram").result_id
    moved_cell = CELL.replace("| 09/26   |", "| 09/29   |")
    drawing = CELL.replace("Cell Diagram", "Cell Drawing")
    both = variant(text=REPORT.replace(CELL, f"{moved_cell}\n{drawing}"))
    review = review_of(store, both)
    moved, renamed = row(review, "Cell Diagram"), row(review, "Cell Drawing")

    assert moved.question is not None
    assert moved.question.kind is QuestionKind.DUE_CHANGED
    assert renamed.status is ItemStatus.NEEDS_ANSWER
    assert renamed.question is not None
    assert renamed.question.kind is QuestionKind.RENAMED
    assert renamed.question.ids == (cell,)
    twice = save(store, both, review, matches=[same(moved), same(renamed)])
    assert isinstance(twice, ReviewReturned)
    assert twice.why is ReturnReason.ANSWERS
    outcome = saved(save(store, both, review, matches=[different(moved), same(renamed)]))
    assert dict(outcome.accepted)[renamed.key] == cell
    assert len(results(store)) == 5


# ------------------------------------------------------------- current values per target


def test_a_history_only_repeat_changes_nothing_current() -> None:
    """B kept as an earlier report, as the current choice keeps one, supplies no current value,
    and importing it again offers nothing and leaves every current value as it was."""
    store = in_memory()
    first = saved(save(store, A))
    newer = saved(save(store, B))
    store._connection.execute(
        "UPDATE grade_reports SET use = 'earlier' WHERE report_id = ?", (newer.report_id,)
    )
    store._connection.commit()
    class_id = class_of(store)
    before = store.current_values(class_id, "T1")
    review = review_of(store, B)
    saved(save(store, B, review))

    assert {value.report_id for value in before.results.values()} == {first.report_id}
    assert row(review, "Cell Diagram").status is ItemStatus.MATCHES_EARLIER
    assert review.ready == frozenset()
    assert store.current_values(class_id, "T1") == before


def test_a_clipped_copy_of_a_is_all_saved() -> None:
    store = in_memory()
    saved(save(store, A))
    review = review_of(store, variant(text=CLIPPED))

    assert review.term is None
    assert {item.status for item in review.items} == {ItemStatus.SAVED}
    assert review.ready == frozenset()


def test_a_partial_report_without_a_term_total_leaves_the_earlier_total_current() -> None:
    store = in_memory()
    first = saved(save(store, A))
    partial = variant(CELL_SCORE, text=CLIPPED)
    review = review_of(store, partial)
    assert review.ready == {row(review, "Cell Diagram").key}
    later = saved(save(store, partial, review))
    current = store.current_values(class_of(store), "T1")

    assert current.term is not None
    assert (current.term.report_id, current.term.order) == (first.report_id, 1)
    assert text_of(current.term, "percent") == "81.9"
    cell = row(review, "Cell Diagram").result_id or ""
    assert (current.results[cell].report_id, current.results[cell].order) == (later.report_id, 2)
    coverage = store._connection.execute(
        "SELECT coverage FROM grade_reports WHERE report_id = ?", (later.report_id,)
    ).fetchall()
    assert coverage == [("partial",)]
    assert not any(value.last_seen for value in current.results.values())


def test_a_result_missing_from_a_newer_full_report_is_last_seen_in_its_own() -> None:
    store = in_memory()
    first = saved(save(store, A))
    osmosis = row(review_of(store, A), "Osmosis with Potato Slices").result_id or ""
    without = variant(CELL_SCORE, (f"{OSMOSIS}\n", ""))
    newer = saved(save(store, without))
    current = store.current_values(class_of(store), "T1")

    cell = row(review_of(store, A), "Cell Diagram").result_id or ""
    assert (current.results[osmosis].report_id, current.results[osmosis].last_seen) == (
        first.report_id,
        True,
    )
    assert current.results[cell].report_id == newer.report_id
    assert {
        current.results[key].report_id for key in current.results if key not in (cell, osmosis)
    } == {first.report_id}
    assert [value.last_seen for key, value in current.results.items() if key != osmosis] == [
        False
    ] * 3


CELL_NINE = ("| Cell Diagram             | 7.0 ", "| Cell Diagram             | 9.0 ")
"""Cell Diagram's score changed from 7.0 to 9.0."""


def test_a_result_shown_again_by_the_newest_full_report_is_not_last_seen() -> None:
    """Osmosis is left out of full report B, then shown unchanged by a newer full report: that
    report saw it last, so it isn't last seen in A, and its value stays A's."""
    store = in_memory()
    first = saved(save(store, A))
    osmosis = row(review_of(store, A), "Osmosis with Potato Slices").result_id or ""
    saved(save(store, variant(CELL_SCORE, (f"{OSMOSIS}\n", ""))))
    newest = saved(save(store, variant(CELL_NINE)))
    current = store.current_values(class_of(store), "T1")

    assert current.results[osmosis].last_seen is False
    assert current.results[osmosis].report_id == first.report_id
    cell = row(review_of(store, A), "Cell Diagram").result_id or ""
    assert current.results[cell].report_id == newest.report_id


def test_a_result_shown_again_by_a_newer_partial_report_is_not_last_seen() -> None:
    """Osmosis is left out of full report B, then shown unchanged by a newer copy without a
    term grade. A partial report doesn't say what is missing, but it does say what is there."""
    store = in_memory()
    saved(save(store, A))
    osmosis = row(review_of(store, A), "Osmosis with Potato Slices").result_id or ""
    saved(save(store, variant(CELL_SCORE, (f"{OSMOSIS}\n", ""))))
    no_term = REPORT[: REPORT.index("| **Term Grade**")]
    partial = saved(save(store, variant(CELL_NINE, text=no_term)))
    current = store.current_values(class_of(store), "T1")

    coverage = store._connection.execute(
        "SELECT coverage FROM grade_reports WHERE report_id = ?", (partial.report_id,)
    ).fetchall()
    assert coverage == [("partial",)]
    assert current.results[osmosis].last_seen is False


# ------------------------------------------------------------- answers bound to their questions


def test_matching_answers_bound_to_no_question_asked_now_return_the_review() -> None:
    store = in_memory()
    saved(save(store, A))
    moved = variant(CELL_DUE, RENAMED)
    review = review_of(store, moved)
    cell, seed = row(review, "Cell Diagram"), row(review, "Seed Germination Journal")
    assert cell.question is not None
    assert seed.question is not None
    stale = MatchAnswer(cell.key, (*cell.question.ids, "result-of-no-one"), cell.question.ids[0])
    unasked = MatchAnswer(row(review, "Microscope Practice").key, cell.question.ids, None)
    twice = MatchAnswer(seed.key, seed.question.ids, cell.question.ids[0])
    before = store._connection.total_changes

    for matches in ([stale], [unasked], [same(cell), twice]):
        outcome = save(store, moved, review, matches=matches)
        assert isinstance(outcome, ReviewReturned), outcome
        assert outcome.why is ReturnReason.ANSWERS
    unanswered = save(store, moved, review, selection={cell.key})
    assert isinstance(unanswered, ReviewReturned)
    assert unanswered.why is ReturnReason.SELECTION
    assert store._connection.total_changes == before
    assert TERM_KEY not in review.ready
