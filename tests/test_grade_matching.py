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
import hashlib
import json
import sqlite3
from collections.abc import Callable, Collection
from typing import cast

import pytest

from blossom.grades.draft import GradeReportDraft, Presence, capture_key
from blossom.grades.identity import Identity, IdentityStatus, name_form_key
from blossom.grades.projection import (
    NEW,
    ActionRecorded,
    CurrentPreview,
    MadeCurrent,
    NothingToChange,
    PreviewRevised,
    ReportNotSaved,
    ScopeHeld,
    SourceOf,
    preview_of,
)
from blossom.grades.review import (
    RESULT_FIELDS,
    TERM_KEY,
    AlreadyRecorded,
    ClassRecord,
    CurrentValue,
    CurrentValues,
    GradeReportSaved,
    GradeReview,
    ItemStatus,
    MatchAnswer,
    OnRecord,
    QuestionKind,
    ReportAt,
    ReportUse,
    ReturnReason,
    ReviewItem,
    ReviewReturned,
    SaveOutcome,
    UseChoice,
    cells_of,
    review_from,
)
from blossom.grades.text_reader import read_grade_report, reading_complete
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    FIXTURES,
    OBSERVED_AT,
    confirm_current,
    current_preview,
    fixture_clock,
    grade_answers,
    save_grade,
    without_the_due_column,
)

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


def whole(*changes: tuple[str, str], text: str = REPORT) -> bool:
    """Whether the reading of ``text`` with ``changes`` is complete."""
    for old, new in changes:
        text = text.replace(old, new)
    return reading_complete(read_grade_report(text))


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
    complete: bool = False,
    use: str | None = None,
) -> SaveOutcome:
    """A parent's save with every setup question answered, ``matches`` as the matching answers,
    ``use`` as the report-level choice (its default when None), and the ready values plus the
    answered rows selected unless ``selection`` says otherwise; its reading incomplete unless
    ``complete``."""
    review = review or review_of(store, draft)
    answers = dataclasses.replace(
        grade_answers(review), matches=tuple(matches), use=cast("ReportUse | None", use)
    )
    if selection is None:
        selection = review.ready | {answer.row_key for answer in matches}
    return save_grade(
        store,
        draft,
        key=KEY,
        review=review,
        answers=answers,
        selection=selection,
        complete=complete,
    )


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
    offered = review_of(store, B)
    assert offered.use == UseChoice("current", ())
    newer = saved(save(store, B, offered, use="earlier"))
    assert use_of(store, newer.report_id) == "earlier"
    class_id = class_of(store)
    before = store.current_values(class_id, "T1")
    review = review_of(store, B)
    saved(save(store, B, review))

    assert {value.report_id for value in before.results.values()} == {first.report_id}
    # B's 8.0 was never current, and B's own capture accepted it: Saved, never offered again.
    assert row(review, "Cell Diagram").status is ItemStatus.SAVED
    assert review.ready == frozenset()
    assert review.use is None
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
    first = saved(save(store, A, complete=whole()))
    partial = variant(CELL_SCORE, text=CLIPPED)
    review = review_of(store, partial)
    assert review.ready == {row(review, "Cell Diagram").key}
    assert whole(CELL_SCORE, text=CLIPPED) is False
    later = saved(save(store, partial, review, complete=whole(CELL_SCORE, text=CLIPPED)))
    current = store.current_values(class_of(store), "T1")

    assert current.term is not None
    assert (current.term.report_id, current.term.order) == (first.report_id, 1)
    assert text_of(current.term, "percent") == "81.9"
    cell = row(review, "Cell Diagram").result_id or ""
    assert (current.results[cell].report_id, current.results[cell].order) == (later.report_id, 2)
    complete = store._connection.execute(
        "SELECT complete FROM grade_acceptances WHERE report_id = ?", (later.report_id,)
    ).fetchall()
    assert complete == [(0,)]
    assert not any(value.not_shown for value in current.results.values())


def test_a_result_missing_from_a_newer_complete_report_is_not_shown_in_it() -> None:
    """A complete, fully resolved current report without Osmosis supports "Not shown in this
    report"; Osmosis keeps its score from A and was last shown in A. The rows the newer report
    repeats unchanged were last shown in it, their scores still A's."""
    store = in_memory()
    first = saved(save(store, A, complete=whole()))
    osmosis = row(review_of(store, A), "Osmosis with Potato Slices").result_id or ""
    without = variant(CELL_SCORE, (f"{OSMOSIS}\n", ""))
    newer = saved(save(store, without, complete=whole(CELL_SCORE, (f"{OSMOSIS}\n", ""))))
    current = store.current_values(class_of(store), "T1")

    cell = row(review_of(store, A), "Cell Diagram").result_id or ""
    by_a = ReportAt(first.report_id or "", 1)
    by_newer = ReportAt(newer.report_id or "", 2)
    value = current.results[osmosis]
    assert (value.report_id, value.last_shown, value.not_shown) == (first.report_id, by_a, by_newer)
    assert current.results[cell].report_id == newer.report_id
    others = [key for key in current.results if key not in (cell, osmosis)]
    assert {current.results[key].report_id for key in others} == {first.report_id}
    assert {current.results[key].last_shown for key in (*others, cell)} == {by_newer}
    assert [value.not_shown for key, value in current.results.items() if key != osmosis] == [
        None
    ] * 3


CELL_NINE = ("| Cell Diagram             | 7.0 ", "| Cell Diagram             | 9.0 ")
"""Cell Diagram's score changed from 7.0 to 9.0."""


def test_a_result_shown_again_by_the_newest_complete_report_is_shown_there() -> None:
    """Osmosis is left out of complete report B, then shown unchanged by a newer complete report:
    it was last shown in that one, B's absence is older than that showing, and its value stays
    A's."""
    store = in_memory()
    first = saved(save(store, A, complete=True))
    osmosis = row(review_of(store, A), "Osmosis with Potato Slices").result_id or ""
    saved(save(store, variant(CELL_SCORE, (f"{OSMOSIS}\n", "")), complete=True))
    newest = saved(save(store, variant(CELL_NINE), complete=True))
    current = store.current_values(class_of(store), "T1")

    assert current.results[osmosis].not_shown is None
    assert current.results[osmosis].last_shown == ReportAt(newest.report_id or "", 3)
    assert current.results[osmosis].report_id == first.report_id
    cell = row(review_of(store, A), "Cell Diagram").result_id or ""
    assert current.results[cell].report_id == newest.report_id


def test_a_result_shown_again_by_a_newer_partial_report_is_shown_there() -> None:
    """Osmosis is left out of complete report B, then shown unchanged by a newer copy without a
    term grade. A partial report doesn't say what is missing, but it does say what is there."""
    store = in_memory()
    saved(save(store, A, complete=True))
    osmosis = row(review_of(store, A), "Osmosis with Potato Slices").result_id or ""
    saved(save(store, variant(CELL_SCORE, (f"{OSMOSIS}\n", "")), complete=True))
    no_term = REPORT[: REPORT.index("| **Term Grade**")]
    assert whole(text=no_term) is False
    partial = saved(save(store, variant(CELL_NINE, text=no_term), complete=False))
    current = store.current_values(class_of(store), "T1")

    complete = store._connection.execute(
        "SELECT complete FROM grade_acceptances WHERE report_id = ?", (partial.report_id,)
    ).fetchall()
    assert complete == [(0,)]
    assert current.results[osmosis].not_shown is None
    assert current.results[osmosis].last_shown == ReportAt(partial.report_id or "", 3)


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


# ------------------------------------------------------------- presence and provenance


def records(store: ProjectStateStore, report_id: str | None) -> dict[str, tuple[str | None, str]]:
    """Each row record of a report: its row key, with the result it names and how."""
    found = store._connection.execute(
        "SELECT row_key, result_id, how FROM grade_match_decisions WHERE report_id = ?",
        (report_id,),
    )
    return {str(key): (result, str(how)) for key, result, how in found}


def observed(store: ProjectStateStore, report_id: str | None) -> int:
    (count,) = store._connection.execute(
        "SELECT COUNT(*) FROM grade_result_observations WHERE report_id = ?", (report_id,)
    ).fetchone()
    return int(count)


OSMOSIS_GONE = (f"{OSMOSIS}\n", "")
"""Osmosis left out of a copy."""


def test_unchanged_and_unselected_changed_rows_are_recorded_as_shown_and_accept_nothing() -> None:
    """A newer report's unchanged rows and its Changed row left unselected are recorded as
    shown, each with its result and how it matched; no value is accepted, the outcome says no
    grade value changed, and the Changed score stays A's."""
    store = in_memory()
    saved(save(store, A, complete=True))
    review_b = review_of(store, B)
    cell = row(review_b, "Cell Diagram")
    presence = saved(save(store, B, review_b, selection=()))

    assert (presence.added, presence.updated, presence.accepted) == (0, 0, ())
    assert (presence.shown, presence.answers_kept) == (4, 0)
    assert records(store, presence.report_id) == {
        item.key: (item.result_id, "exact") for item in review_b.rows
    }
    assert observed(store, presence.report_id) == 0
    current = store.current_values(class_of(store), "T1")
    by_b = ReportAt(presence.report_id or "", 2)
    assert text_of(current.results[cell.result_id or ""], "points") == "7.0"
    assert {value.order for value in current.results.values()} == {1}
    assert {value.last_shown for value in current.results.values()} == {by_b}
    assert current.term is not None
    assert current.term.order == 1


def test_a_row_shown_first_is_still_changed_and_accepted_later_keeps_its_one_record() -> None:
    """Rule 0 replays the shown row for its identity only: it stays Changed and selectable, and
    accepting it adds its observation to the same report, under the record it already has."""
    store = in_memory()
    saved(save(store, A, complete=True))
    presence = saved(save(store, B, selection=()))
    again = review_of(store, B)
    cell = row(again, "Cell Diagram")

    assert (cell.status, cell.how) == (ItemStatus.CHANGED, "same_capture")
    assert again.ready == {cell.key}
    later = saved(save(store, B, again))

    assert later.report_id == presence.report_id
    assert (later.added, later.updated, later.shown) == (0, 1, 0)
    assert records(store, presence.report_id)[cell.key] == (cell.result_id, "exact")
    assert len(records(store, presence.report_id)) == 4
    current = store.current_values(class_of(store), "T1")
    assert text_of(current.results[cell.result_id or ""], "points") == "8.0"
    assert current.results[cell.result_id or ""].report_id == presence.report_id


def test_a_supplies_b_repeats_and_c_omits_keep_source_last_shown_and_absence_apart() -> None:
    """His example: the score came from A, the assignment was last shown in B, and complete,
    fully resolved C says it doesn't show it."""
    store = in_memory()
    first = saved(save(store, A, complete=True))
    osmosis = row(review_of(store, A), "Osmosis with Potato Slices").result_id or ""
    b = saved(save(store, variant(CELL_SCORE), selection=(), complete=True))
    c = saved(save(store, variant(CELL_NINE, OSMOSIS_GONE), selection=(), complete=True))
    value = store.current_values(class_of(store), "T1").results[osmosis]

    assert value.report_id == first.report_id
    assert value.last_shown == ReportAt(b.report_id or "", 2)
    assert value.not_shown == ReportAt(c.report_id or "", 3)


def test_absence_needs_a_complete_reading_and_every_row_resolved() -> None:
    """An incomplete reading, or a row left unresolved, keeps a report from saying what it
    doesn't show."""
    for complete, change in ((False, ()), (True, (RENAMED,))):
        store = in_memory()
        saved(save(store, A, complete=True))
        osmosis = row(review_of(store, A), "Osmosis with Potato Slices").result_id or ""
        without = variant(OSMOSIS_GONE, *change)
        newer = saved(save(store, without, selection=(), complete=complete))
        value = store.current_values(class_of(store), "T1").results[osmosis]

        assert newer.report_id is not None
        assert value.not_shown is None
        assert value.last_shown == ReportAt(value.report_id, 1)


def test_a_couldnt_read_row_records_nothing_and_blocks_absence() -> None:
    """A row whose due date can't be read has no reliable identity: it records no presence, and
    its report isn't fully resolved."""
    store = in_memory()
    saved(save(store, A, complete=True))
    osmosis = row(review_of(store, A), "Osmosis with Potato Slices").result_id or ""
    draft = variant(OSMOSIS_GONE, ("| Missing    | 09/26   |", "| Missing    | 09/2x   |"))
    review = review_of(store, draft)
    unread = [item for item in review.rows if item.status is ItemStatus.UNREADABLE]
    newer = saved(save(store, draft, review, selection=(), complete=True))

    assert [item.result_id for item in unread] == [None]
    assert unread[0].key not in records(store, newer.report_id)
    assert newer.shown == 2
    value = store.current_values(class_of(store), "T1").results[osmosis]
    assert value.not_shown is None


def test_a_row_with_an_unreadable_score_and_readable_identity_is_shown_not_accepted() -> None:
    """His twelfth round: "EX" as a score is never read as anything, the row stays Couldn't
    read, and its match records the assignment as shown while the score stays A's."""
    store = in_memory()
    saved(save(store, A, complete=True))
    excused = variant(("| Cell Diagram             | 7.0 ", "| Cell Diagram             | EX  "))
    review = review_of(store, excused)
    cell = row(review, "Cell Diagram")

    assert (cell.status, cell.how) == (ItemStatus.UNREADABLE, "exact")
    assert cell.key not in review.ready
    newer = saved(save(store, excused, review, selection=(), complete=True))
    assert records(store, newer.report_id)[cell.key] == (cell.result_id, "exact")
    value = store.current_values(class_of(store), "T1").results[cell.result_id or ""]
    assert text_of(value, "points") == "7.0"
    assert value.order == 1
    assert value.last_shown == ReportAt(newer.report_id or "", 2)


# ------------------------------------------------------------- a score that can't be read, asked


CELL_EX = ("| Cell Diagram             | 7.0 ", "| Cell Diagram             | EX  ")
"""Cell Diagram's score written "EX", which no supported format reads."""
SEED_SCORE = ("| Seed Germination Log | 18.0 ", "| Seed Germination Log | 19.0 ")


def answered_review(
    store: ProjectStateStore, draft: GradeReportDraft, *answers: MatchAnswer
) -> GradeReview:
    """The review a save computes again with ``answers`` applied, as the save holds it."""
    with store._lock:
        source = capture_key(draft)
        return store._review_locked(
            draft, source, KEY, same_class=None, matches=answers, complete=False
        )


def test_a_row_whose_score_cant_be_read_asks_and_its_answer_records_it_shown() -> None:
    """His thirteenth round, 6: the row asks "Same assignment, due date changed?" beside its
    warning; answered, it is recorded as shown, still Couldn't read, never selectable, and "EX"
    is kept nowhere."""
    store = in_memory()
    first = saved(save(store, A, complete=True))
    cell_id = row(review_of(store, A), "Cell Diagram").result_id
    draft = variant(CELL_EX, CELL_DUE)
    review = review_of(store, draft)
    cell = row(review, "Cell Diagram")

    assert (cell.status, cell.result_id, cell.how) == (ItemStatus.UNREADABLE, None, None)
    assert cell.question is not None
    assert (cell.question.kind, cell.question.ids) == (QuestionKind.DUE_CHANGED, (cell_id,))
    assert cell.key not in review.ready
    answer = same(cell)
    answered = row(answered_review(store, draft, answer), "Cell Diagram")
    assert (answered.status, answered.result_id, answered.how) == (
        ItemStatus.UNREADABLE,
        cell_id,
        "answer",
    )
    refused = save(store, draft, review, matches=[answer], selection={cell.key})
    assert isinstance(refused, ReviewReturned)
    assert refused.why is ReturnReason.SELECTION
    stale = MatchAnswer(cell.key, (*cell.question.ids, "result-of-no-one"), cell_id)
    unbound = save(store, draft, review, matches=[stale], selection=())
    assert isinstance(unbound, ReviewReturned)
    assert unbound.why is ReturnReason.ANSWERS
    kept = saved(save(store, draft, review, matches=[answer], selection=(), complete=True))

    assert (kept.added, kept.updated, kept.accepted) == (0, 0, ())
    assert (kept.shown, kept.answers_kept) == (4, 1)
    assert records(store, kept.report_id)[cell.key] == (cell_id, "answer")
    assert observed(store, kept.report_id) == 0
    assert len(results(store)) == 4
    value = store.current_values(class_of(store), "T1").results[cell_id or ""]
    assert (text_of(value, "points"), text_of(value, "due")) == ("7.0", "09/26")
    assert value.report_id == first.report_id
    assert value.last_shown == ReportAt(kept.report_id or "", 2)
    held = store._connection.execute(
        "SELECT COUNT(*) FROM grade_result_observations WHERE points_text = 'EX'"
    ).fetchone()
    assert held == (0,)
    again = row(review_of(store, draft), "Cell Diagram")
    assert (again.status, again.result_id, again.how, again.question) == (
        ItemStatus.UNREADABLE,
        cell_id,
        "same_capture",
        None,
    )
    before = store._connection.total_changes
    assert isinstance(save(store, draft, review, matches=[answer], selection=()), AlreadyRecorded)
    assert store._connection.total_changes == before


def test_an_unreadable_score_left_unanswered_records_nothing_and_proves_no_absence() -> None:
    """The question may stay unanswered: the other eligible values save, the row records
    nothing, and its report proves no absence until a later submission answers it."""
    store = in_memory()
    saved(save(store, A, complete=True))
    osmosis = row(review_of(store, A), "Osmosis with Potato Slices").result_id or ""
    draft = variant(CELL_EX, CELL_DUE, OSMOSIS_GONE, SEED_SCORE)
    review = review_of(store, draft)
    cell, seed = row(review, "Cell Diagram"), row(review, "Seed Germination Log")

    assert (cell.status, cell.question is not None) == (ItemStatus.UNREADABLE, True)
    assert review.ready == {seed.key}
    newer = saved(save(store, draft, review, selection={seed.key}, complete=True))

    assert (newer.updated, newer.shown, newer.answers_kept) == (1, 1, 0)
    assert cell.key not in records(store, newer.report_id)
    current = store.current_values(class_of(store), "T1").results
    assert text_of(current[seed.result_id or ""], "points") == "19.0"
    assert current[osmosis].not_shown is None
    later = review_of(store, draft)
    asked = row(later, "Cell Diagram")
    assert asked.question is not None
    answered = saved(save(store, draft, later, matches=[same(asked)], selection=(), complete=True))
    assert answered.report_id == newer.report_id
    shown = store.current_values(class_of(store), "T1").results[osmosis].not_shown
    assert shown == ReportAt(newer.report_id or "", 2)


LAB_EX = (
    OSMOSIS,
    OSMOSIS.replace("Osmosis with Potato Slices", "Potato Lab                ")
    .replace("| 31.0    |", "| EX      |")
    .replace("| 40.0    |", "| 50.0    |")
    .replace("10/02", "10/03"),
)
"""Osmosis's title, due date and max points all changed, and its score written "EX"."""


def test_a_row_whose_score_cant_be_read_first_classed_new_can_choose_an_existing_one() -> None:
    """Rule 7: "Choose an existing assignment" is open for it; the choice records presence as
    the parent's, and the row never becomes a new result or a value."""
    store = in_memory()
    saved(save(store, A, complete=True))
    osmosis = row(review_of(store, A), "Osmosis with Potato Slices").result_id or ""
    draft = variant(LAB_EX)
    review = review_of(store, draft)
    lab = row(review, "Potato Lab")

    assert (lab.status, lab.question, lab.choices) == (ItemStatus.UNREADABLE, None, (osmosis,))
    assert lab.key not in review.ready
    refused = save(store, draft, review, matches=[chosen(lab, osmosis)], selection={lab.key})
    assert isinstance(refused, ReviewReturned)
    assert refused.why is ReturnReason.SELECTION
    outcome = saved(save(store, draft, review, matches=[chosen(lab, osmosis)], selection=()))

    assert (outcome.added, outcome.updated, outcome.answers_kept) == (0, 0, 1)
    assert records(store, outcome.report_id)[lab.key] == (osmosis, "chosen")
    assert len(results(store)) == 4
    value = store.current_values(class_of(store), "T1").results[osmosis]
    assert text_of(value, "assignment") == "Osmosis with Potato Slices"


def test_a_row_whose_identity_and_score_cant_be_read_asks_nothing() -> None:
    """A row whose due date can't be read either is unchanged: no question, no choice."""
    store = in_memory()
    saved(save(store, A, complete=True))
    draft = variant(CELL_EX, ("| Missing    | 09/26   |", "| Missing    | 09/2x   |"))
    review = review_of(store, draft)
    cell = row(review, "Cell Diagram")
    newer = saved(save(store, draft, review, selection=()))

    assert (cell.status, cell.result_id, cell.question, cell.choices) == (
        ItemStatus.UNREADABLE,
        None,
        None,
        (),
    )
    assert cell.key not in records(store, newer.report_id)


def test_an_older_capture_s_rest_after_a_newer_report_showed_the_result_is_not_offered() -> None:
    """A saves Cell at 7.0; K saves only Osmosis; L, the newest, shows Cell at 7.0. K's rest
    can't put its 8.0 over the newer showing: it isn't offered."""
    osmosis_32 = ("| 31.0    | 40.0    |", "| 32.0    | 40.0    |")
    osmosis_33 = ("| 31.0    | 40.0    |", "| 33.0    | 40.0    |")
    store = in_memory()
    saved(save(store, A, complete=True))
    k = variant(CELL_SCORE, osmosis_32)
    first = review_of(store, k)
    osmosis = row(first, "Osmosis with Potato Slices")
    saved(save(store, k, first, selection={osmosis.key}))
    saved(save(store, variant(osmosis_33), selection=()))
    rest = review_of(store, k)
    cell = row(rest, "Cell Diagram")

    assert (cell.status, cell.covered) == (ItemStatus.COVERED, True)
    assert cell.key not in rest.ready
    assert rest.use is None
    value = store.current_values(class_of(store), "T1").results[cell.result_id or ""]
    assert (text_of(value, "points"), value.order) == ("7.0", 1)
    assert value.last_shown is not None
    assert value.last_shown.order == 3


def test_one_capture_read_complete_once_and_incomplete_once_proves_no_absence() -> None:
    """Two submissions of one capture join one report; one incomplete reading keeps it from
    proving absence."""
    store = in_memory()
    saved(save(store, A, complete=True))
    osmosis = row(review_of(store, A), "Osmosis with Potato Slices").result_id or ""
    without = variant(CELL_SCORE, OSMOSIS_GONE)
    cell = row(review_of(store, without), "Cell Diagram")
    first = saved(save(store, without, selection=(), complete=True))
    assert store.current_values(class_of(store), "T1").results[osmosis].not_shown is not None
    second = saved(save(store, without, selection={cell.key}, complete=False))

    assert second.report_id == first.report_id
    assert store.current_values(class_of(store), "T1").results[osmosis].not_shown is None


def test_a_submission_that_records_nothing_new_makes_no_report_and_its_retry_is_a_no_op() -> None:
    store = in_memory()
    saved(save(store, A, complete=True))
    saved(save(store, B, selection=()))
    reports = store._connection.execute("SELECT COUNT(*) FROM grade_reports").fetchone()
    review = review_of(store, B)
    nothing = saved(save(store, B, review, selection=()))

    assert (nothing.report_id, nothing.shown, nothing.added, nothing.updated) == (None, 0, 0, 0)
    assert store._connection.execute("SELECT COUNT(*) FROM grade_reports").fetchone() == reports
    before = store._connection.total_changes
    assert isinstance(save(store, B, review, selection=()), AlreadyRecorded)
    assert store._connection.total_changes == before


def revision(store: ProjectStateStore) -> int:
    (found,) = store._connection.execute("SELECT revision FROM grade_scope_revisions").fetchone()
    return int(found)


def test_a_no_op_leaves_the_revision_and_another_open_page_still_saves() -> None:
    """His thirteenth round, 7: a submission that records nothing new writes only its
    acceptance, so a page built before it still saves."""
    store = in_memory()
    saved(save(store, A, complete=True))
    saved(save(store, B, selection=()))
    other_page = review_of(store, B)
    before = revision(store)
    nothing = saved(save(store, B, selection=()))

    assert nothing.report_id is None
    assert revision(store) == before
    cell = row(other_page, "Cell Diagram")
    later = saved(save(store, B, other_page, selection={cell.key}))
    assert later.updated == 1
    assert revision(store) == before + 1


def presence_alone(store: ProjectStateStore) -> SaveOutcome:
    return save(store, B, selection=())


def an_answer_alone(store: ProjectStateStore) -> SaveOutcome:
    renamed = variant(RENAMED)
    review = review_of(store, renamed)
    answer = same(row(review, "Seed Germination Journal"))
    return save(store, renamed, review, matches=[answer], selection=())


def a_different_answer_alone(store: ProjectStateStore) -> SaveOutcome:
    renamed = variant(RENAMED)
    review = review_of(store, renamed)
    answer = different(row(review, "Seed Germination Journal"))
    return save(store, renamed, review, matches=[answer], selection=())


def an_unreadable_score_answered(store: ProjectStateStore) -> SaveOutcome:
    draft = variant(CELL_EX, CELL_DUE)
    review = review_of(store, draft)
    return save(store, draft, review, matches=[same(row(review, "Cell Diagram"))], selection=())


@pytest.mark.parametrize(
    "submission",
    [presence_alone, an_answer_alone, a_different_answer_alone, an_unreadable_score_answered],
)
def test_presence_or_an_answer_alone_raises_the_revision_and_returns_another_open_page(
    submission: Callable[[ProjectStateStore], SaveOutcome],
) -> None:
    """New presence or a matching answer is a change with no grade value: the revision rises,
    and a page built before it comes back for review, writing nothing."""
    store = in_memory()
    saved(save(store, A, complete=True))
    other_page = review_of(store, variant(CELL_SCORE, OSMOSIS_GONE))
    before = revision(store)
    outcome = saved(submission(store))

    assert (outcome.added, outcome.updated) == (0, 0)
    assert outcome.report_id is not None
    assert revision(store) == before + 1
    changes = store._connection.total_changes
    returned = save(store, variant(CELL_SCORE, OSMOSIS_GONE), other_page)
    assert isinstance(returned, ReviewReturned)
    assert returned.why is ReturnReason.REVISION
    assert store._connection.total_changes == changes


def test_an_answer_kept_without_a_value_is_recorded_and_replayed() -> None:
    """Saving only a matching answer keeps it as shown and says no grade value changed; a retry
    writes nothing; the capture again resolves the row to that result through its record."""
    store = in_memory()
    saved(save(store, A, complete=True))
    renamed = variant(RENAMED, CELL_SCORE)
    review = review_of(store, renamed)
    seed = row(review, "Seed Germination Journal")
    answer = same(seed)
    kept = saved(save(store, renamed, review, matches=[answer], selection=()))

    assert (kept.added, kept.updated, kept.answers_kept) == (0, 0, 1)
    assert records(store, kept.report_id)[seed.key] == (answer.result_id, "answer")
    before = store._connection.total_changes
    retry = save(store, renamed, review, matches=[answer], selection=())
    assert isinstance(retry, AlreadyRecorded)
    assert retry.saved == kept
    assert store._connection.total_changes == before
    again = row(review_of(store, renamed), "Seed Germination Journal")
    assert (again.result_id, again.how, again.question) == (answer.result_id, "same_capture", None)


# ------------------------------------------------------------- reusing explicit answers


SEED = REPORT[REPORT.index("| Seed Germination Log |") : REPORT.index("\n", REPORT.index("| Seed"))]
"""Seed Germination Log's row as the fixture writes it."""


def seed_rows(*titles: str) -> GradeReportDraft:
    """A capture whose Seed Germination Log row is replaced by rows with ``titles``, alike in
    every other cell."""
    rows = "\n".join(SEED.replace("Seed Germination Log", title, 1) for title in titles)
    return variant((SEED, rows), CELL_NINE)


def kept_answer(store: ProjectStateStore, title: str) -> str:
    """Seed Germination Log renamed ``title`` in a capture of its own, answered "Same
    assignment" and saved with no value selected: the result the answer names."""
    renamed = variant((SEED, SEED.replace("Seed Germination Log", title, 1)))
    review = review_of(store, renamed)
    answer = same(row(review, title))
    saved(save(store, renamed, review, matches=[answer], selection=()))
    return answer.result_id or ""


def test_an_answer_kept_without_a_value_is_reused_for_the_same_evidence() -> None:
    """The same normalized evidence returns in another capture: the answer is reused without
    asking, and recorded as automatic."""
    store = in_memory()
    saved(save(store, A, complete=True))
    seed = kept_answer(store, "Seed Germination Journal")
    later = seed_rows("Seed Germination Journal")
    review = review_of(store, later)
    journal = row(review, "Seed Germination Journal")

    assert (journal.result_id, journal.how, journal.question) == (seed, "reused", None)
    c = saved(save(store, later, review, selection=()))
    assert records(store, c.report_id)[journal.key] == (seed, "reused")
    assert c.answers_kept == 0


def test_two_rows_that_would_reuse_one_result_both_ask() -> None:
    store = in_memory()
    saved(save(store, A, complete=True))
    seed = kept_answer(store, "Seed Germination Journal")
    assert kept_answer(store, "Seed Germination Diary") == seed
    review = review_of(store, seed_rows("Seed Germination Journal", "Seed Germination Diary"))

    for title in ("Seed Germination Journal", "Seed Germination Diary"):
        item = row(review, title)
        assert (item.status, item.how) == (ItemStatus.NEEDS_ANSWER, None)
        assert item.question is not None
        assert item.question.ids == (seed,)


def test_an_exact_match_comes_before_a_reused_answer() -> None:
    """The result the unchanged row matches exactly isn't reused for the renamed one."""
    store = in_memory()
    saved(save(store, A, complete=True))
    seed = kept_answer(store, "Seed Germination Journal")
    review = review_of(store, seed_rows("Seed Germination Log", "Seed Germination Journal"))
    log, journal = row(review, "Seed Germination Log"), row(review, "Seed Germination Journal")

    assert (log.result_id, log.how) == (seed, "exact")
    assert (journal.status, journal.result_id, journal.how) == (ItemStatus.NEW, None, None)


def test_a_reuse_waits_while_an_unresolved_row_may_be_that_result() -> None:
    """Another row of the report, unresolved, has the answered result among its candidates: the
    reuse waits, and both rows ask."""
    store = in_memory()
    saved(save(store, A, complete=True))
    seed = kept_answer(store, "Seed Germination Journal")
    review = review_of(store, seed_rows("Seed Germination Journal", "Germination Notes"))
    journal, notes = row(review, "Seed Germination Journal"), row(review, "Germination Notes")

    assert (journal.status, journal.how) == (ItemStatus.NEEDS_ANSWER, None)
    assert notes.question is not None
    assert seed in notes.question.ids


def test_a_stored_automatic_decision_for_another_result_blocks_the_reuse() -> None:
    """An exact record for the same evidence naming another result conflicts with the answer:
    the row asks."""
    store = in_memory()
    saved(save(store, A, complete=True))
    seed = kept_answer(store, "Seed Germination Journal")
    later = seed_rows("Seed Germination Journal")
    journal = row(review_of(store, later), "Seed Germination Journal")
    (evidence,) = store._connection.execute(
        "SELECT evidence FROM grade_match_decisions WHERE row_key = ? LIMIT 1", (journal.key,)
    ).fetchone()
    cell = row(review_of(store, A), "Cell Diagram").result_id
    (report_id,) = store._connection.execute(
        "SELECT report_id FROM grade_reports LIMIT 1"
    ).fetchone()
    store._connection.execute(
        "INSERT INTO grade_match_decisions (report_id, row_key, student_id, evidence, occurrence, "
        "result_id, how, decided_by, decided_at) SELECT ?, 'another-row', student_id, ?, 1, ?, "
        "'exact', 'parent', '2026-10-07T00:00:00+00:00' FROM grade_student",
        (report_id, evidence, cell),
    )
    asked = row(review_of(store, later), "Seed Germination Journal")

    assert seed != cell
    assert (asked.status, asked.how) == (ItemStatus.NEEDS_ANSWER, None)


# ------------------------------------------------------------- remembered answers and choices


def chosen(item: ReviewItem, result_id: str | None) -> MatchAnswer:
    """ "Choose an existing assignment" for ``item``, naming ``result_id`` among its choices."""
    return MatchAnswer(item.key, item.choices, result_id, chosen=True)


def different_kept(store: ProjectStateStore) -> tuple[str, GradeReportSaved]:
    """A saved complete; Seed Germination Log renamed Journal in a capture of its own, answered
    "A different assignment" and saved with no value selected: the rejected result, and the
    outcome."""
    saved(save(store, A, complete=True))
    seed = row(review_of(store, A), "Seed Germination Log").result_id or ""
    renamed = variant(RENAMED)
    review = review_of(store, renamed)
    journal = row(review, "Seed Germination Journal")
    outcome = save(
        store, renamed, review, matches=[different(journal)], selection=(), complete=True
    )
    return seed, saved(outcome)


def test_a_different_answer_kept_without_a_value_is_remembered_and_changeable() -> None:
    """His twelfth round: the answer is kept with the candidate it turned down and its evidence;
    it creates nothing, shows nothing and resolves nothing, and the same evidence with the same
    candidate is not asked again, though the answer stays open to change."""
    store = in_memory()
    seed, kept = different_kept(store)
    journal_key = row(review_of(store, variant(RENAMED)), "Seed Germination Journal").key

    assert (kept.added, kept.updated, kept.shown, kept.answers_kept) == (0, 0, 3, 1)
    assert records(store, kept.report_id)[journal_key] == (None, "different")
    (rejected,) = store._connection.execute(
        "SELECT rejected FROM grade_match_decisions WHERE row_key = ?", (journal_key,)
    ).fetchone()
    evidence = [["reported", "Homework / Practice"], ["reported", "Seed Germination Log"]]
    assert json.loads(rejected) == [[seed, [*evidence, ["reported", "09/22"]]]]
    assert len(results(store)) == 4
    assert store.current_values(class_of(store), "T1").results[seed].not_shown is None
    for draft in (variant(RENAMED), variant(RENAMED, CELL_SCORE)):
        item = row(review_of(store, draft), "Seed Germination Journal")

        assert (item.status, item.result_id, item.remembered) == (ItemStatus.NEW, None, True)
        assert item.question is not None
        assert item.question.ids == (seed,)
        assert seed in item.choices


def test_a_remembered_different_asks_again_for_twins_or_another_candidate() -> None:
    """Indistinguishable rows, or a candidate the answer never saw, ask again."""
    store = in_memory()
    seed, _ = different_kept(store)
    twins = review_of(store, seed_rows("Seed Germination Journal", "Seed Germination Journal"))
    for occurrence in (1, 2):
        item = row(twins, "Seed Germination Journal", occurrence)
        assert (item.status, item.remembered) == (ItemStatus.NEEDS_ANSWER, False)
    saved(save(store, WITH_LEAF))
    item = row(review_of(store, variant(RENAMED)), "Seed Germination Journal")

    assert (item.status, item.remembered) == (ItemStatus.NEEDS_ANSWER, False)
    assert item.question is not None
    assert seed in item.question.ids
    assert len(item.question.ids) == 2


LEAF = SEED.replace("Seed Germination Log", "Leaf Sketch", 1).replace("09/22", "09/30")
WITH_LEAF = variant((SEED, f"{SEED}\n{LEAF}"))
"""A capture adding Leaf Sketch to Homework / Practice, with Seed Germination Log's max points."""


def test_a_different_answered_again_for_another_candidate_keeps_one_record() -> None:
    """In the report that kept it, "A different assignment" answered again once a new candidate
    appeared turns down both in the row's one record, and is remembered for both."""
    store = in_memory()
    seed, kept = different_kept(store)
    saved(save(store, WITH_LEAF))
    renamed = variant(RENAMED)
    review = review_of(store, renamed)
    journal = row(review, "Seed Germination Journal")
    assert journal.question is not None
    again = saved(save(store, renamed, review, matches=[different(journal)], selection=()))

    assert again.report_id == kept.report_id
    assert (again.shown, again.answers_kept) == (0, 1)
    assert records(store, kept.report_id)[journal.key] == (None, "different")
    (rejected,) = store._connection.execute(
        "SELECT rejected FROM grade_match_decisions WHERE row_key = ?", (journal.key,)
    ).fetchone()
    assert {one[0] for one in json.loads(rejected)} == {seed, *journal.question.ids}
    assert len(journal.question.ids) == 2
    item = row(review_of(store, renamed), "Seed Germination Journal")
    assert (item.status, item.remembered) == (ItemStatus.NEW, True)


def test_a_row_answered_different_that_later_resolves_keeps_its_one_record() -> None:
    """Answered "Same assignment", chosen, or selected as new, the row's remembered answer in
    its report becomes the record of the result it resolved to, in place."""
    for way in ("same", "chosen", "new"):
        store = in_memory()
        seed, kept = different_kept(store)
        renamed = variant(RENAMED)
        review = review_of(store, renamed)
        journal = row(review, "Seed Germination Journal")
        assert journal.choices == (seed,)
        matches = {"same": [same(journal)], "chosen": [chosen(journal, seed)], "new": []}[way]
        selection = {journal.key} if way == "new" else set()
        later = saved(save(store, renamed, review, matches=matches, selection=selection))

        assert later.report_id == kept.report_id, way
        held = records(store, kept.report_id)
        assert len(held) == 4
        result, how = held[journal.key]
        assert how == {"same": "answer", "chosen": "chosen", "new": "answer"}[way]
        assert (result == seed) is (way != "new")
        assert len(results(store)) == (5 if way == "new" else 4)
        assert (later.shown, later.answers_kept) == ((0, 0) if way == "new" else (1, 1))


TAKEN_OVER = (
    OSMOSIS,
    OSMOSIS.replace("Osmosis with Potato Slices", "Potato Lab                ")
    .replace("| 40.0    |", "| 50.0    |")
    .replace("10/02", "10/03"),
)
"""Osmosis's title, due date and max points all changed: a New row."""


def test_choose_an_existing_assignment_keeps_the_result_for_a_new_row() -> None:
    """His eleventh round, 3: a row with no candidate offers her results no other row resolved
    to; the choice keeps the result's ID, is recorded as the parent's, and is checked again."""
    store = in_memory()
    saved(save(store, A, complete=True))
    osmosis = row(review_of(store, A), "Osmosis with Potato Slices").result_id or ""
    draft = variant(TAKEN_OVER)
    review = review_of(store, draft)
    lab = row(review, "Potato Lab")

    assert (lab.status, lab.question, lab.choices) == (ItemStatus.NEW, None, (osmosis,))
    stale = MatchAnswer(lab.key, (*lab.choices, "result-of-no-one"), osmosis, chosen=True)
    taken = chosen(lab, row(review, "Cell Diagram").result_id)
    for wrong in (stale, taken):
        returned = save(store, draft, review, matches=[wrong])
        assert isinstance(returned, ReviewReturned)
        assert returned.why is ReturnReason.ANSWERS
    outcome = saved(save(store, draft, review, matches=[chosen(lab, osmosis)]))

    assert (outcome.added, outcome.updated) == (0, 1)
    assert records(store, outcome.report_id)[lab.key] == (osmosis, "chosen")
    assert len(results(store)) == 4
    value = store.current_values(class_of(store), "T1").results[osmosis]
    assert (text_of(value, "assignment"), text_of(value, "due")) == ("Potato Lab", "10/03")


def test_two_rows_choosing_one_result_return_the_review() -> None:
    store = in_memory()
    saved(save(store, A, complete=True))
    osmosis = row(review_of(store, A), "Osmosis with Potato Slices").result_id or ""
    other = TAKEN_OVER[1].replace("Potato Lab", "Potato Test")
    draft = variant((OSMOSIS, f"{TAKEN_OVER[1]}\n{other}"))
    review = review_of(store, draft)
    lab, test = row(review, "Potato Lab"), row(review, "Potato Test")
    returned = save(store, draft, review, matches=[chosen(lab, osmosis), chosen(test, osmosis)])

    assert isinstance(returned, ReviewReturned)
    assert returned.why is ReturnReason.ANSWERS
    assert len(results(store)) == 4


def test_reordered_identical_rows_ask_and_conflicting_answers_never_save() -> None:
    """His case list: IXL twins in another order ask which; two answers naming one result
    return the review; crossed answers keep both results, replay through their records, and
    leave the evidence's answers in conflict, so a third capture asks again."""
    store = in_memory()
    saved(save(store, variant(text=IXL_REPORT)))
    again = review_of(store, variant(text=IXL_REPORT))
    first, second = row(again, "IXL", 1).result_id, row(again, "IXL", 2).result_id
    eight = IXL.replace("9.0", "8.0", 1)
    swapped = variant(text=IXL_REPORT.replace(f"{IXL}\n{IXL}", f"{eight}\n{IXL}"))
    review = review_of(store, swapped)
    one, two = row(review, "IXL", 1), row(review, "IXL", 2)

    for item in (one, two):
        assert (item.status, item.how, item.remembered) == (ItemStatus.NEEDS_ANSWER, None, False)
    assert one.question is not None
    assert two.question is not None
    clash = [
        MatchAnswer(one.key, one.question.ids, first),
        MatchAnswer(two.key, two.question.ids, first),
    ]
    returned = save(store, swapped, review, matches=clash)
    assert isinstance(returned, ReviewReturned)
    assert returned.why is ReturnReason.ANSWERS
    crossed = [
        MatchAnswer(one.key, one.question.ids, second),
        MatchAnswer(two.key, two.question.ids, first),
    ]
    outcome = saved(save(store, swapped, review, matches=crossed, selection=()))

    assert records(store, outcome.report_id)[one.key] == (second, "answer")
    assert records(store, outcome.report_id)[two.key] == (first, "answer")
    assert len(results(store)) == 6
    replay = review_of(store, swapped)
    assert (row(replay, "IXL", 1).result_id, row(replay, "IXL", 1).how) == (second, "same_capture")
    seven = IXL.replace("9.0", "7.0", 1)
    third = review_of(store, variant(text=IXL_REPORT.replace(f"{IXL}\n{IXL}", f"{seven}\n{IXL}")))
    assert {row(third, "IXL", n).status for n in (1, 2)} == {ItemStatus.NEEDS_ANSWER}


# ------------------------------------------------------------- the report-level choice (rule 5)


def use_of(store: ProjectStateStore, report_id: str | None) -> str:
    (use,) = store._connection.execute(
        "SELECT use FROM grade_reports WHERE report_id = ?", (report_id,)
    ).fetchone()
    return str(use)


def reports_of(store: ProjectStateStore) -> int:
    (count,) = store._connection.execute("SELECT COUNT(*) FROM grade_reports").fetchone()
    return int(count)


AGAIN = variant(text=IXL_REPORT)
"""Another capture of A with two IXL rows added: its Cell Diagram reads 7.0, as A's did."""
UNREAD_DUES = tuple(
    (f"| {due}   |", f"| {due[:-1]}x   |") for due in ("09/22", "09/26", "09/24", "10/02")
)
"""Every row's due date made unreadable: no row's identity can be read."""


def test_a_new_report_starts_on_current_and_is_saved_as_current() -> None:
    store = in_memory()
    first = review_of(store, A)
    assert first.use == UseChoice("current", ())
    one = saved(save(store, A, first))
    newer = review_of(store, B)
    assert newer.use == UseChoice("current", ())
    two = saved(save(store, B, newer))

    assert (use_of(store, one.report_id), use_of(store, two.report_id)) == ("current", "current")
    cell = row(newer, "Cell Diagram").result_id or ""
    value = store.current_values(class_of(store), "T1").results[cell]
    assert (text_of(value, "points"), value.report_id) == ("8.0", two.report_id)


def test_a_s_rest_in_another_capture_after_newer_b_starts_on_earlier_and_b_stays_current() -> None:
    """A saved without its IXL rows; B, newer, changes Cell Diagram; a clipped copy of A brings
    the IXL rows back with A's 7.0. It repeats a value B replaced, so it starts on "Keep as an
    earlier report", and saved so it supplies nothing current: B stays current."""
    store = in_memory()
    full = review_of(store, AGAIN)
    ixl = {row(full, "IXL", n).key for n in (1, 2)}
    saved(save(store, AGAIN, full, selection=full.ready - ixl))
    newer = saved(save(store, B))
    before = store.current_values(class_of(store), "T1")
    rest = variant(text=IXL_REPORT[: IXL_REPORT.index("|          |   |                   |")])
    review = review_of(store, rest)
    cell = row(review, "Cell Diagram")

    assert review.use == UseChoice("earlier", (cell.key,))
    assert cell.status is ItemStatus.MATCHES_EARLIER
    assert review.ready == ixl
    kept = saved(save(store, rest, review))
    assert kept.added == 2
    assert use_of(store, kept.report_id) == "earlier"
    after = store.current_values(class_of(store), "T1")
    assert after == before
    assert after.results[cell.result_id or ""].report_id == newer.report_id
    assert len(results(store)) == 6


def test_the_parent_may_still_choose_current_and_bring_a_value_back() -> None:
    """A teacher's 7.0, then 8.0, then 7.0 again in another capture: the review starts on
    earlier, the value reads Matches an earlier saved value with 8.0 as its "from", and only the
    parent's choice of current brings it back. A retry of that choice writes nothing."""
    store = in_memory()
    saved(save(store, A))
    replaced = saved(save(store, B))
    review = review_of(store, AGAIN)
    cell = row(review, "Cell Diagram")

    assert review.use == UseChoice("earlier", (cell.key,))
    assert cell.current is not None
    assert (cell.status, text_of(cell.current, "points")) == (ItemStatus.MATCHES_EARLIER, "8.0")
    assert cell.key in review.back_to
    assert cell.key not in review.ready
    for use in (None, "earlier"):
        refused = save(store, AGAIN, review, selection={cell.key}, use=use)
        assert isinstance(refused, ReviewReturned)
        assert refused.why is ReturnReason.SELECTION
    back = saved(save(store, AGAIN, review, selection={cell.key}, use="current"))
    current = store.current_values(class_of(store), "T1")
    value = current.results[cell.result_id or ""]

    assert (back.added, back.updated) == (0, 1)
    assert use_of(store, back.report_id) == "current"
    assert (text_of(value, "points"), value.report_id, value.order) == ("7.0", back.report_id, 3)
    held = store._connection.execute(
        "SELECT report_id FROM grade_result_observations WHERE result_id = ?", (cell.result_id,)
    ).fetchall()
    assert len(held) == 3
    assert (replaced.report_id,) in held
    count = reports_of(store)
    retry = save(store, AGAIN, review, selection={cell.key}, use="current")
    assert isinstance(retry, AlreadyRecorded)
    assert retry.saved == back
    assert reports_of(store) == count
    assert store.current_values(class_of(store), "T1") == current


def test_a_value_the_page_never_showed_as_matching_an_earlier_one_never_goes_back() -> None:
    """G-I8: B moved Cell Diagram and changed its score; another capture with A's very cells asks
    first and, answered, matches the value B replaced. It can't be brought back in that save,
    since the page started on current without saying why not."""
    store = in_memory()
    saved(save(store, A))
    moved = variant(CELL_SCORE, CELL_DUE)
    first = review_of(store, moved)
    saved(save(store, moved, first, matches=[same(row(first, "Cell Diagram"))]))
    review = review_of(store, AGAIN)
    cell = row(review, "Cell Diagram")
    assert (cell.status, review.use) == (ItemStatus.NEEDS_ANSWER, UseChoice("current", ()))
    assert cell.question is not None
    answer = [same(cell)]
    returned = save(store, AGAIN, review, matches=answer, selection={cell.key}, use="current")

    assert isinstance(returned, ReviewReturned)
    assert returned.why is ReturnReason.SELECTION
    assert reports_of(store) == 2
    shown = saved(save(store, AGAIN, review, matches=answer, selection=(), use="current"))
    value = store.current_values(class_of(store), "T1").results[cell.question.ids[0]]
    assert (text_of(value, "points"), value.order, value.last_shown) == (
        "8.0",
        2,
        ReportAt(shown.report_id or "", 3),
    )


def test_the_choice_is_not_offered_when_it_would_change_nothing() -> None:
    """A copy whose term and categories are saved and whose rows can't be identified would change
    no current value, last showing or absence: no choice, and an answer to one returns the
    review."""
    store = in_memory()
    saved(save(store, A, complete=True))
    before = store.current_values(class_of(store), "T1")
    draft = variant(*UNREAD_DUES)
    review = review_of(store, draft)

    assert {item.status for item in review.rows} == {ItemStatus.UNREADABLE}
    assert review.use is None
    for use in ("current", "earlier", "now"):
        returned = save(store, draft, review, use=use)
        assert isinstance(returned, ReviewReturned)
        assert returned.why is ReturnReason.ANSWERS
    outcome = saved(save(store, draft, review, complete=True))
    assert outcome.report_id is None
    assert store.current_values(class_of(store), "T1") == before


ROW_LINES = tuple(
    line
    for line in REPORT.splitlines()
    if line.startswith(("| Seed", "| Cell", "| Microscope", "| Osmosis"))
)
NO_ROWS = tuple((f"{line}\n", "") for line in ROW_LINES)
"""Every result row left out of a copy: its term and categories stay."""
LAB_LOG = "| Lab Safety Log | 9.0 | 10.0 | 90.0 | Valid | 09/30 | 0.0 | 0.0 | | 1.0 | |"
LAB_LOG_REPORT = REPORT.replace(CELL, f"{CELL}\n{LAB_LOG}")
"""Another capture with one more homework row, Lab Safety Log."""


def test_a_capture_with_no_rows_offers_no_choice_complete_or_not() -> None:
    """A copy whose term and categories are saved and that has no result rows makes no report, so
    it can't show an assignment as absent: no choice, complete or not, and an answer to one
    returns the review."""
    store = in_memory()
    saved(save(store, A, complete=True))
    draft = variant(*NO_ROWS)

    assert whole(*NO_ROWS)
    assert review_of(store, draft).use is None
    review = store.review_grade_report(draft, capture_key(draft), key=KEY, complete=True)
    assert (review.rows, {item.status for item in review.items}) == ((), {ItemStatus.SAVED})
    assert review.use is None
    returned = save(store, draft, review, use="current", complete=True)
    assert isinstance(returned, ReviewReturned)
    assert returned.why is ReturnReason.ANSWERS
    assert saved(save(store, draft, review, complete=True)).report_id is None


def test_absence_alone_offers_the_choice_only_for_a_complete_reading() -> None:
    """Its one row names Lab Safety Log, which only a report kept as earlier holds, so no current
    value or last showing could change: only a complete reading could show A's results as
    absent, and only then is the choice offered."""
    store = in_memory()
    saved(save(store, A, complete=True))
    saved(save(store, variant(text=LAB_LOG_REPORT), use="earlier", complete=True))
    excused = (*NO_ROWS, (LAB_LOG, LAB_LOG.replace(" 9.0 ", " EX ")))
    draft = variant(*excused, text=LAB_LOG_REPORT)
    log = row(review_of(store, draft), "Lab Safety Log")

    assert whole(*excused, text=LAB_LOG_REPORT)
    assert (log.status, log.how) == (ItemStatus.UNREADABLE, "exact")
    assert log.result_id not in store.current_values(class_of(store), "T1").results
    assert review_of(store, draft).use is None
    review = store.review_grade_report(draft, capture_key(draft), key=KEY, complete=True)
    assert review.use == UseChoice("current", ())


def test_an_answer_not_among_the_choices_returns_the_review() -> None:
    store = in_memory()
    saved(save(store, A))
    review = review_of(store, B)
    returned = save(store, B, review, use="now")

    assert isinstance(returned, ReviewReturned)
    assert returned.why is ReturnReason.ANSWERS
    assert reports_of(store) == 1


def test_a_presence_only_report_takes_the_default_and_kept_as_earlier_shows_nothing() -> None:
    """4.3a: a submission of presence alone makes a report with rule 5's default use. Kept as
    earlier, it moves no last showing and proves no absence."""
    store = in_memory()
    saved(save(store, A, complete=True))
    saved(save(store, B, complete=True))
    before = store.current_values(class_of(store), "T1")
    review = review_of(store, AGAIN)
    kept = saved(save(store, AGAIN, review, selection=(), complete=True))

    assert (kept.shown, kept.report_id is not None) == (4, True)
    assert use_of(store, kept.report_id) == "earlier"
    assert store.current_values(class_of(store), "T1") == before


def test_a_partial_report_kept_as_earlier_changes_nothing_current() -> None:
    """His case list: a clipped copy with a changed score starts on current; kept as earlier, it
    supplies nothing, and its own rest joins it with no choice offered."""
    store = in_memory()
    saved(save(store, A, complete=True))
    before = store.current_values(class_of(store), "T1")
    partial = variant(CELL_SCORE, text=CLIPPED)
    review = review_of(store, partial)

    assert review.use == UseChoice("current", ())
    kept = saved(save(store, partial, review, use="earlier"))
    assert kept.updated == 1
    assert use_of(store, kept.report_id) == "earlier"
    assert store.current_values(class_of(store), "T1") == before
    assert review_of(store, partial).use is None


def test_a_capture_s_rest_joins_its_latest_report_kept_as_earlier() -> None:
    """Rule 4: the rest of a capture kept as earlier joins that report, still earlier, with no
    choice offered, and changes nothing current."""
    store = in_memory()
    saved(save(store, A))
    saved(save(store, B))
    review = review_of(store, AGAIN)
    kept = saved(save(store, AGAIN, review, selection={row(review, "IXL", 1).key}))
    assert use_of(store, kept.report_id) == "earlier"
    before = store.current_values(class_of(store), "T1")
    count = reports_of(store)
    rest = review_of(store, AGAIN)
    second = row(rest, "IXL", 2)

    assert rest.use is None
    assert second.key in rest.ready
    later = saved(save(store, AGAIN, rest, selection={second.key}))
    assert later.report_id == kept.report_id
    assert reports_of(store) == count
    assert use_of(store, kept.report_id) == "earlier"
    assert store.current_values(class_of(store), "T1") == before


def test_a_title_date_and_max_change_chosen_keeps_the_id_and_its_history() -> None:
    """His case list: Osmosis renamed, moved and given another max, chosen as the same
    assignment, keeps its result ID with both observations; A again resolves its row to that ID
    through its own records and reads Matches an earlier saved value."""
    store = in_memory()
    saved(save(store, A, complete=True))
    osmosis = row(review_of(store, A), "Osmosis with Potato Slices").result_id or ""
    draft = variant(TAKEN_OVER)
    review = review_of(store, draft)
    saved(save(store, draft, review, matches=[chosen(row(review, "Potato Lab"), osmosis)]))
    held = store._connection.execute(
        "SELECT COUNT(*) FROM grade_result_observations WHERE result_id = ?", (osmosis,)
    ).fetchone()
    back = review_of(store, A)
    item = row(back, "Osmosis with Potato Slices")

    assert held == (2,)
    assert (item.result_id, item.how) == (osmosis, "same_capture")
    assert item.status is ItemStatus.MATCHES_EARLIER
    assert back.use is None


def test_an_explicit_answer_kept_in_an_earlier_report_still_counts_for_matching() -> None:
    """Matching isn't use: a "Same assignment" answer saved in a report kept as earlier is
    reused for the same evidence in another capture."""
    store = in_memory()
    saved(save(store, A))
    draft = variant(MOVED)
    review = review_of(store, draft)
    moved = row(review, "Microscope Practice")
    assert moved.question is not None
    kept = saved(save(store, draft, review, matches=[same(moved)], selection=(), use="earlier"))
    assert use_of(store, kept.report_id) == "earlier"
    other = row(review_of(store, variant(MOVED, CELL_SCORE)), "Microscope Practice")

    assert (other.result_id, other.how) == (moved.question.ids[0], "reused")


SEED_19 = ("| Seed Germination Log | 18.0 ", "| Seed Germination Log | 19.0 ")
SEED_20 = ("| Seed Germination Log | 18.0 ", "| Seed Germination Log | 20.0 ")
CELL_9 = ("| Cell Diagram             | 7.0 ", "| Cell Diagram             | 9.0 ")
MICROSCOPE_28 = (
    "| Microscope Practice                | 27.0 ",
    "| Microscope Practice                | 28.0 ",
)


def test_a_value_only_an_earlier_report_holds_is_changed_and_offered_in_another_capture() -> None:
    """His thirteenth round, 5: B's 8.0 was saved only as history, so nothing replaced it. In
    another capture it reads Changed from A's 7.0, is offered, the report starts on current, and
    saving it makes 8.0 current."""
    store = in_memory()
    saved(save(store, A))
    saved(save(store, B, use="earlier"))
    other = variant(CELL_SCORE, SEED_19)
    review = review_of(store, other)
    cell = row(review, "Cell Diagram")

    assert cell.status is ItemStatus.CHANGED
    assert cell.key in review.ready
    assert review.use == UseChoice("current", ())
    saved(save(store, other, review))
    value = store.current_values(class_of(store), "T1").results[cell.result_id or ""]
    assert text_of(value, "points") == "8.0"


def test_a_result_only_an_earlier_report_holds_offers_its_first_current_value() -> None:
    """Lab Safety Log was saved only in a report kept as earlier, so it has no current value. In
    another capture its row matches that result and offers its first current value."""
    store = in_memory()
    saved(save(store, A))
    saved(save(store, variant(text=LAB_LOG_REPORT), use="earlier"))
    other = variant(SEED_19, text=LAB_LOG_REPORT)
    review = review_of(store, other)
    log = row(review, "Lab Safety Log")

    assert (log.status, log.how, log.current) == (ItemStatus.NEW, "exact", None)
    assert log.result_id is not None
    assert log.key in review.ready
    saved(save(store, other, review))
    value = store.current_values(class_of(store), "T1").results[log.result_id]
    assert text_of(value, "points") == "9.0"
    assert len(results(store)) == 5


def test_a_replaced_value_still_starts_the_report_on_earlier_beside_an_earlier_only_one() -> None:
    """Seed's 18.0 was current until 19.0 replaced it, and Cell's 8.0 was only ever history. A
    capture repeating both offers Cell as Changed, and Seed's repeat still starts it on earlier."""
    store = in_memory()
    saved(save(store, A))
    saved(save(store, variant(SEED_19)))
    saved(save(store, B, use="earlier"))
    other = variant(CELL_SCORE, MICROSCOPE_28)
    review = review_of(store, other)
    seed = row(review, "Seed Germination Log")

    assert seed.status is ItemStatus.MATCHES_EARLIER
    assert row(review, "Cell Diagram").status is ItemStatus.CHANGED
    assert row(review, "Cell Diagram").key in review.ready
    assert review.use == UseChoice("earlier", (seed.key,))


def test_a_capture_s_rest_matching_an_earlier_value_reads_shown_in_a_newer_report() -> None:
    """His scheduled review on #148 (4210429315): K saves only Seed, then M makes Cell 8.0
    current and N replaces it with 9.0. K's rest, Cell 8.0, matches a value M held, but N is newer
    than K's report and covers it: Shown in a newer report, and never offered."""
    store = in_memory()
    saved(save(store, A))
    k = variant(CELL_SCORE, SEED_19)
    first = review_of(store, k)
    saved(save(store, k, first, selection={row(first, "Seed Germination Log").key}))
    saved(save(store, variant(CELL_SCORE, SEED_20)))
    saved(save(store, variant(CELL_9, SEED_20)))
    again = review_of(store, k)
    cell = row(again, "Cell Diagram")

    assert (cell.status, cell.covered) == (ItemStatus.COVERED, True)
    assert cell.key not in again.ready | again.back_to


LEAF_CHANGES = (CELL_SCORE, (SEED, f"{SEED}\n{LEAF}"))
B_LEAF = variant(*LEAF_CHANGES)
"""A newer capture: Cell Diagram's score changed from 7.0 to 8.0, and Leaf Sketch added."""
NINE_LEAF = variant(CELL_NINE, (SEED, f"{SEED}\n{LEAF}"))
"""A capture newer still: Cell Diagram at 9.0, with Leaf Sketch."""
OBSERVED_LATER = OBSERVED_AT.replace(day=28)
LATER = fixture_clock(OBSERVED_LATER)
"""A clock nine days on, for the moment a parent makes saved values current."""


def made(outcome: object) -> MadeCurrent:
    assert isinstance(outcome, MadeCurrent), outcome
    return outcome


def revision_of(store: ProjectStateStore) -> int:
    (revision,) = store._connection.execute("SELECT revision FROM grade_scope_revisions").fetchone()
    return int(revision)


def result_of(store: ProjectStateStore, draft: GradeReportDraft, title: str) -> str:
    result_id = row(review_of(store, draft), title).result_id
    assert result_id is not None
    return result_id


def test_class_details_brings_a_replaced_value_back_from_the_capture_s_latest_report() -> None:
    """A's 7.0, then B's 8.0 with Leaf Sketch: the preview of A's capture lists 7.0 back from
    8.0, each of A's results last shown in the new report, and Leaf Sketch not shown there; the
    confirmation makes exactly that current ("back to" from class details)."""
    store = in_memory()
    first = saved(save(store, A, complete=True))
    newer = saved(save(store, B_LEAF, complete=whole(*LEAF_CHANGES)))
    cell, leaf = result_of(store, A, "Cell Diagram"), result_of(store, B_LEAF, "Leaf Sketch")
    preview = current_preview(store, A)

    assert (preview.source, preview.acceptances, preview.made_by) == (
        first.report_id,
        (first.acceptance_id,),
        None,
    )
    assert preview.complete is True
    ((effect),) = preview.values
    assert (effect.kind, effect.target, effect.after.report_id) == ("result", cell, NEW)
    assert effect.before is not None
    assert (effect.before.report_id, text_of(effect.before, "points")) == (newer.report_id, "8.0")
    assert text_of(effect.after, "points") == "7.0"
    a_results = {result_id for _, result_id in first.accepted if result_id is not None}
    assert {(one.result, one.before, one.after) for one in preview.last_shown} == {
        (result_id, newer.report_id, NEW) for result_id in a_results
    }
    assert [(one.result, one.before, one.after) for one in preview.not_shown] == [(leaf, None, NEW)]
    body = json.loads(preview.canonical)
    assert set(body) == {
        "scope",
        "revision",
        "source",
        "complete",
        "values",
        "last_shown",
        "not_shown",
    }
    assert body["values"][0][3][0] == NEW
    assert hashlib.sha256(preview.canonical.encode()).hexdigest() == preview.digest
    assert preview.action_id not in preview.canonical
    assert "2026-" not in preview.canonical
    revision = revision_of(store)

    outcome = made(confirm_current(store, A, preview))
    current = store.current_values(class_of(store), "T1")
    value = current.results[cell]

    assert (outcome.source, outcome.digest) == (first.report_id, preview.digest)
    assert (text_of(value, "points"), value.report_id, value.order) == ("7.0", outcome.report_id, 3)
    assert value.last_shown == ReportAt(outcome.report_id, 3)
    assert current.results[leaf].report_id == newer.report_id
    assert current.results[leaf].not_shown == ReportAt(outcome.report_id, 3)
    assert use_of(store, outcome.report_id) == "current"
    assert revision_of(store) == revision + 1


def test_the_new_report_keeps_its_source_s_import_and_the_action_records_its_copies() -> None:
    """The new report carries A's source key, reader, import time, as-of time and number of
    result rows; its row records are ``same_capture`` at the action's role and time; the action
    record names its source, the acceptances into it and every copy; no acceptance or name form
    is written."""
    store = in_memory()
    first = saved(save(store, A, complete=True))
    saved(save(store, B_LEAF, complete=True))
    preview = current_preview(store, A)
    forms = store._connection.execute("SELECT * FROM grade_name_forms").fetchall()
    store._clock = LATER

    outcome = made(confirm_current(store, A, preview))

    def report(report_id: str) -> tuple[object, ...]:
        return tuple(
            store._connection.execute(
                "SELECT source_key, reader, imported_at, as_of, result_rows FROM grade_reports "
                "WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        )

    assert report(outcome.report_id) == report(str(first.report_id))
    copied_rows = store._connection.execute(
        "SELECT row_key, result_id, how, rejected, decided_by, decided_at "
        "FROM grade_match_decisions WHERE report_id = ? ORDER BY row_key",
        (outcome.report_id,),
    ).fetchall()
    source_rows = store._connection.execute(
        "SELECT row_key, result_id FROM grade_match_decisions WHERE report_id = ? ORDER BY row_key",
        (first.report_id,),
    ).fetchall()
    later = OBSERVED_LATER.isoformat()
    assert copied_rows == [
        (row_key, result_id, "same_capture", None, "parent", later)
        for row_key, result_id in source_rows
    ]
    (action,) = store._connection.execute(
        "SELECT action_id, source_report, source_acceptances, source_action, report_made, "
        "copied, complete_from_source, digest, acted_at, role FROM grade_current_actions"
    ).fetchall()
    assert action[:5] == (
        preview.action_id,
        first.report_id,
        json.dumps([first.acceptance_id]),
        None,
        outcome.report_id,
    )
    copied = json.loads(action[5])
    assert copied["rows"] == [[row_key, result_id] for row_key, result_id in source_rows]
    assert sorted(map(tuple, copied["observations"])) == sorted(
        [("term", TERM_KEY)]
        + [
            ("category", key)
            for key, result_id in first.accepted
            if result_id is None and key != TERM_KEY
        ]
        + [("result", result_id) for _, result_id in first.accepted if result_id is not None]
    )
    assert action[6:] == (1, preview.digest, later, "parent")
    assert report(outcome.report_id)[2] != later
    assert store._connection.execute("SELECT COUNT(*) FROM grade_acceptances").fetchone() == (2,)
    assert store._connection.execute("SELECT * FROM grade_name_forms").fetchall() == forms


def test_a_retry_returns_its_outcome_and_a_second_confirmation_has_nothing_to_change() -> None:
    """After the action, its retry returns the recorded outcome and writes nothing; the preview
    of the capture is empty, from its new latest report, and confirming it writes nothing and
    leaves the revision. A retry posting another digest also gets a fresh preview."""
    store = in_memory()
    saved(save(store, A, complete=True))
    saved(save(store, B_LEAF, complete=True))
    preview = current_preview(store, A)
    outcome = made(confirm_current(store, A, preview))
    changes, revision = store._connection.total_changes, revision_of(store)

    assert confirm_current(store, A, preview) == ActionRecorded(outcome)
    again = current_preview(store, A)
    assert again.empty
    assert (again.source, again.acceptances, again.made_by) == (
        outcome.report_id,
        (),
        outcome.action_id,
    )
    assert isinstance(confirm_current(store, A, again), NothingToChange)
    other = confirm_current(store, A, preview, digest="0" * 64)
    assert isinstance(other, ActionRecorded)
    assert other.made == outcome
    assert other.fresh is not None
    assert not isinstance(other.fresh, ReportNotSaved)
    assert other.fresh.empty
    assert (store._connection.total_changes, revision_of(store)) == (changes, revision)


def test_a_changed_revision_source_or_preview_returns_the_revised_preview() -> None:
    """A saved without Osmosis, then B, clipped, with Cell Diagram at 8.0. A's rest saved after
    the preview joins A's report: the stale confirmation returns the revised preview, with both
    acceptances, under a fresh action ID; a digest not of that preview returns it again; only
    the revised preview's own confirmation writes."""
    store = in_memory()
    full = review_of(store, A)
    osmosis = row(full, "Osmosis with Potato Slices").key
    first = saved(save(store, A, full, selection=full.ready - {osmosis}, complete=True))
    saved(save(store, variant(CELL_SCORE, text=CLIPPED)))
    preview = current_preview(store, A)
    rest = saved(save(store, A, complete=True))
    assert rest.report_id == first.report_id
    count = reports_of(store)

    revised = confirm_current(store, A, preview)
    assert isinstance(revised, PreviewRevised)
    fresh = revised.preview
    assert fresh.action_id != preview.action_id
    assert fresh.revision == preview.revision + 1
    assert fresh.acceptances == tuple(sorted((first.acceptance_id, rest.acceptance_id)))
    tampered = confirm_current(store, A, fresh, digest=preview.digest)
    assert isinstance(tampered, PreviewRevised)
    assert reports_of(store) == count
    outcome = made(confirm_current(store, A, fresh))
    assert reports_of(store) == count + 1
    assert outcome.digest == fresh.digest


def test_a_row_the_source_only_showed_gives_no_score() -> None:
    """B saved with nothing selected shows Cell Diagram at 8.0 without accepting it; made
    current after a newer report, B moves last showings only, and 7.0 stays current."""
    store = in_memory()
    first = saved(save(store, A, complete=True))
    shown = saved(save(store, B, selection=()))
    saved(save(store, WITH_LEAF))
    cell = result_of(store, A, "Cell Diagram")
    preview = current_preview(store, B)

    assert preview.source == shown.report_id
    assert preview.values == ()
    assert {one.after for one in preview.last_shown} == {NEW}
    outcome = made(confirm_current(store, B, preview))
    value = store.current_values(class_of(store), "T1").results[cell]
    assert (text_of(value, "points"), value.report_id) == ("7.0", first.report_id)
    assert value.last_shown == ReportAt(outcome.report_id, 4)
    (copied,) = store._connection.execute("SELECT copied FROM grade_current_actions").fetchone()
    assert json.loads(copied)["observations"] == []


def test_a_copy_of_an_incomplete_report_stays_incomplete_through_a_copy_of_a_copy() -> None:
    """A read incomplete: its copy claims no absence, and a copy of that copy, which has no
    acceptance of its own, takes its making action's flag, never completeness from no
    readings."""
    store = in_memory()
    saved(save(store, A, complete=False))
    saved(save(store, B_LEAF, complete=True))
    leaf = result_of(store, B_LEAF, "Leaf Sketch")
    preview = current_preview(store, A)
    assert (preview.complete, preview.not_shown) == (False, ())
    copy = made(confirm_current(store, A, preview))
    saved(save(store, NINE_LEAF, complete=True))

    again = current_preview(store, A)
    assert (again.source, again.acceptances, again.made_by) == (
        copy.report_id,
        (),
        copy.action_id,
    )
    assert again.complete is False
    made(confirm_current(store, A, again))
    assert store.current_values(class_of(store), "T1").results[leaf].not_shown is None
    (flags,) = zip(
        *store._connection.execute("SELECT complete_from_source FROM grade_current_actions"),
        strict=True,
    )
    assert flags == (0, 0)


@pytest.mark.parametrize("rest_complete", [True, False])
def test_the_capture_s_rest_joins_the_new_report_over_a_newer_one(rest_complete: bool) -> None:
    """A saved without Cell Diagram, then B with 8.0: A's Cell Diagram is shown in a newer
    report. Once A's capture is made current, it is Changed and selectable, its save joins the
    new report, and that report claims Leaf Sketch's absence only if the rest read complete."""
    store = in_memory()
    full = review_of(store, A)
    cell_key = row(full, "Cell Diagram").key
    saved(save(store, A, full, selection=full.ready - {cell_key}, complete=True))
    saved(save(store, B_LEAF, complete=True))
    leaf = result_of(store, B_LEAF, "Leaf Sketch")
    assert row(review_of(store, A), "Cell Diagram").status is ItemStatus.COVERED

    outcome = made(confirm_current(store, A))
    assert store.current_values(class_of(store), "T1").results[leaf].not_shown is None
    review = review_of(store, A)
    cell = row(review, "Cell Diagram")
    assert cell.status is ItemStatus.CHANGED
    assert review.ready == {cell.key}
    rest = saved(save(store, A, review, complete=rest_complete))
    current = store.current_values(class_of(store), "T1")

    assert rest.report_id == outcome.report_id
    assert text_of(current.results[cell.result_id or ""], "points") == "7.0"
    expected = ReportAt(outcome.report_id, 3) if rest_complete else None
    assert current.results[leaf].not_shown == expected


def test_a_same_text_source_change_alone_is_an_effect() -> None:
    """The projection over two reports with the same term grade: making the first one the source
    lists the term's source change alone; the newer one, already the source, has nothing to
    change."""
    cells = {"percent": (Presence.REPORTED, "81.9"), "letter": (Presence.REPORTED, "B-")}
    held = ScopeHeld(
        reports={"report-a": (1, "current", 0), "report-b": (2, "current", 0)},
        complete={"report-a": True},
        observed=(("term", TERM_KEY, "report-a", cells), ("term", TERM_KEY, "report-b", cells)),
        decided=(),
    )

    def preview(source: str) -> CurrentPreview:
        return preview_of(held, SourceOf("s", "c", "T1", 2, source, (), None), "action-x")

    first, top = preview("report-a"), preview("report-b")
    (effect,) = first.values
    assert effect.before is not None
    assert (effect.target, effect.before.report_id, effect.after.report_id) == (
        TERM_KEY,
        "report-b",
        NEW,
    )
    assert effect.before.cells == effect.after.cells
    assert (first.last_shown, first.not_shown, first.complete) == ((), (), True)
    assert top.empty
    assert top.complete is False


def test_a_report_not_saved_offers_no_preview_and_a_confirmation_naming_it_writes_nothing() -> None:
    """A capture with no report in the class and term has no preview, and a confirmation naming
    a report that isn't saved there writes nothing."""
    store = in_memory()
    saved(save(store, A, complete=True))
    saved(save(store, B, complete=True))
    preview = current_preview(store, A)
    changes = store._connection.total_changes

    missing = store.preview_current(class_of(store), "T1", capture_key(WITH_LEAF))
    gone = confirm_current(store, A, dataclasses.replace(preview, source="report-gone"))

    assert isinstance(missing, ReportNotSaved)
    assert isinstance(gone, ReportNotSaved)
    assert store._connection.total_changes == changes


def test_a_different_answer_for_an_unreadable_score_is_remembered_with_its_warning() -> None:
    """His fourteenth round, 6: kept only by a review submission, bound to the row's
    evidence and the candidate it turned down; it creates no assignment and no presence, the row
    stays Couldn't read, and the same evidence with the same candidate is remembered."""
    store = in_memory()
    first = saved(save(store, A, complete=True))
    cell_id = row(review_of(store, A), "Cell Diagram").result_id or ""
    draft = variant(CELL_EX, CELL_DUE)
    before = store._connection.total_changes
    review = review_of(store, draft)
    cell = row(review, "Cell Diagram")

    assert store._connection.total_changes == before
    assert (cell.status, cell.result_id, cell.how) == (ItemStatus.UNREADABLE, None, None)
    answered = row(answered_review(store, draft, different(cell)), "Cell Diagram")
    assert (answered.status, answered.result_id, answered.how) == (
        ItemStatus.UNREADABLE,
        None,
        "answer",
    )
    assert answered.key not in answered_review(store, draft, different(cell)).ready
    kept = saved(save(store, draft, review, matches=[different(cell)], selection=(), complete=True))

    assert (kept.added, kept.updated, kept.accepted) == (0, 0, ())
    assert (kept.shown, kept.answers_kept) == (3, 1)
    assert records(store, kept.report_id)[cell.key] == (None, "different")
    (rejected,) = store._connection.execute(
        "SELECT rejected FROM grade_match_decisions WHERE row_key = ?", (cell.key,)
    ).fetchone()
    evidence = [["reported", "Homework / Practice"], ["reported", "Cell Diagram"]]
    assert json.loads(rejected) == [[cell_id, [*evidence, ["reported", "09/26"]]]]
    assert observed(store, kept.report_id) == 0
    assert len(results(store)) == 4
    value = store.current_values(class_of(store), "T1").results[cell_id]
    assert (text_of(value, "points"), value.report_id) == ("7.0", first.report_id)
    assert value.last_shown == ReportAt(first.report_id or "", 1)
    changes = store._connection.total_changes
    assert isinstance(save(store, draft, review, matches=[different(cell)]), AlreadyRecorded)
    assert store._connection.total_changes == changes
    for again in (draft, variant(CELL_EX, CELL_DUE, SEED_SCORE)):
        later = review_of(store, again)
        item = row(later, "Cell Diagram")

        assert (item.status, item.result_id, item.how, item.remembered) == (
            ItemStatus.UNREADABLE,
            None,
            "answer",
            True,
        )
        assert item.question is not None
        assert item.question.ids == (cell_id,)
        assert cell_id in item.choices
        assert item.key not in later.ready


# ------------------------------------------------------------- a due date that wasn't captured


NO_DUE = without_the_due_column(REPORT, "Homework / Practice")
"""Wren's report with the Homework / Practice table's Due column left out of the copy."""
LEAF_SKETCH = (
    "| Cell Diagram             | 7.0     | 10.0 ",
    "| Leaf Sketch              | 7.0     | 15.0 ",
)
"""Cell Diagram's row as a title and max points no result has."""


def undated(*changes: tuple[str, str]) -> GradeReportDraft:
    return variant(*changes, text=NO_DUE)


def test_a_row_whose_due_wasnt_captured_asks_and_an_answer_records_only_which_it_is() -> None:
    """His fourteenth and sixteenth rounds: the row never matches by itself, asks which
    assignment it is and offers a choice; the parent's answer records it as shown, its date kept
    not captured, and its equal score then reads Saved beside "Due date not captured", so it
    isn't offered. The reading is incomplete, so no absence follows; the same capture replays
    the answer, and another capture lacking dates asks again."""
    store = in_memory()
    first = saved(save(store, A, complete=True))
    held = review_of(store, A)
    seed_id = row(held, "Seed Germination Log").result_id or ""
    osmosis = row(held, "Osmosis with Potato Slices").result_id or ""
    draft = undated(OSMOSIS_GONE)
    review = review_of(store, draft)
    seed, cell = row(review, "Seed Germination Log"), row(review, "Cell Diagram")

    for item in (seed, cell):
        assert (item.status, item.result_id, item.how, item.due_not_captured) == (
            ItemStatus.DUE_NOT_CAPTURED,
            None,
            None,
            True,
        )
        assert item.key not in review.ready
    assert seed.question is not None
    assert (seed.question.kind, seed.question.ids) == (QuestionKind.WHICH, (seed_id,))
    assert seed_id in seed.choices
    answers = [same(seed), same(cell)]
    settled = answered_review(store, draft, *answers)
    answered = row(settled, "Seed Germination Log")
    assert (answered.status, answered.result_id, answered.how, answered.due_not_captured) == (
        ItemStatus.SAVED,
        seed_id,
        "answer",
        True,
    )
    assert answered.key not in settled.ready
    refused = save(store, draft, review, matches=answers, selection={seed.key})
    assert isinstance(refused, ReviewReturned)
    assert refused.why is ReturnReason.SELECTION
    complete = whole(OSMOSIS_GONE, text=NO_DUE)
    kept = saved(save(store, draft, review, matches=answers, selection=(), complete=complete))

    assert complete is False
    assert (kept.added, kept.updated, kept.accepted) == (0, 0, ())
    assert (kept.shown, kept.answers_kept) == (3, 2)
    assert records(store, kept.report_id)[seed.key] == (seed_id, "answer")
    (evidence,) = store._connection.execute(
        "SELECT evidence FROM grade_match_decisions WHERE row_key = ?", (seed.key,)
    ).fetchone()
    assert json.loads(evidence)[2] == ["not_captured", ""]
    assert observed(store, kept.report_id) == 0
    assert len(results(store)) == 4
    current = store.current_values(class_of(store), "T1").results
    assert (text_of(current[seed_id], "due"), current[seed_id].report_id) == (
        "09/22",
        first.report_id,
    )
    assert current[seed_id].last_shown == ReportAt(kept.report_id or "", 2)
    assert current[osmosis].not_shown is None
    changes = store._connection.total_changes
    assert isinstance(save(store, draft, review, matches=answers, selection=()), AlreadyRecorded)
    assert store._connection.total_changes == changes
    replay = review_of(store, draft)
    again = row(replay, "Seed Germination Log")
    assert (again.status, again.result_id, again.how, again.question) == (
        ItemStatus.SAVED,
        seed_id,
        "same_capture",
        None,
    )
    assert again.due_not_captured
    assert again.key not in replay.ready
    other = review_of(store, undated(OSMOSIS_GONE, CELL_SCORE))
    asked = row(other, "Seed Germination Log")
    assert (asked.status, asked.result_id, asked.how) == (
        ItemStatus.DUE_NOT_CAPTURED,
        None,
        None,
    )
    assert asked.question is not None
    assert asked.question.ids == (seed_id,)
    assert asked.key not in other.ready


def test_a_row_whose_due_wasnt_captured_and_no_result_fits_is_offered_a_choice_only() -> None:
    """With no candidate, the row is no proposed new assignment: it can't be selected before a
    choice and records nothing unanswered. "Choose an existing assignment" records which
    assignment it is, and its value then reads Changed beside "Due date not captured" and can be
    ticked, in this review and when the same capture replays the choice."""
    store = in_memory()
    saved(save(store, A, complete=True))
    cell_id = row(review_of(store, A), "Cell Diagram").result_id or ""
    draft = undated(LEAF_SKETCH)
    review = review_of(store, draft)
    leaf, seed = row(review, "Leaf Sketch"), row(review, "Seed Germination Log")

    assert (leaf.status, leaf.question, leaf.result_id) == (
        ItemStatus.DUE_NOT_CAPTURED,
        None,
        None,
    )
    assert cell_id in leaf.choices
    assert leaf.key not in review.ready
    refused = save(store, draft, review, selection={leaf.key})
    assert isinstance(refused, ReviewReturned)
    assert refused.why is ReturnReason.SELECTION
    settled = answered_review(store, draft, chosen(leaf, cell_id))
    choice = row(settled, "Leaf Sketch")
    assert (choice.status, choice.result_id, choice.how, choice.due_not_captured) == (
        ItemStatus.CHANGED,
        cell_id,
        "chosen",
        True,
    )
    assert choice.key in settled.ready
    outcome = saved(save(store, draft, review, matches=[chosen(leaf, cell_id)], selection=()))

    assert (outcome.added, outcome.updated, outcome.answers_kept) == (0, 0, 1)
    assert records(store, outcome.report_id)[leaf.key] == (cell_id, "chosen")
    assert seed.key not in records(store, outcome.report_id)
    assert observed(store, outcome.report_id) == 0
    assert len(results(store)) == 4
    replay = review_of(store, draft)
    again = row(replay, "Leaf Sketch")
    assert (again.status, again.result_id, again.how) == (
        ItemStatus.CHANGED,
        cell_id,
        "same_capture",
    )
    assert again.key in replay.ready


def test_a_row_missing_its_due_date_with_a_score_that_cant_be_read_keeps_couldnt_read() -> None:
    """Both rules at once: the row asks, its answer records which assignment it is, and the
    score warning stays: the value can't be selected, before or after the match."""
    store = in_memory()
    saved(save(store, A, complete=True))
    cell_id = row(review_of(store, A), "Cell Diagram").result_id or ""
    draft = undated(CELL_EX)
    review = review_of(store, draft)
    cell = row(review, "Cell Diagram")

    assert (cell.status, cell.result_id, cell.due_not_captured) == (
        ItemStatus.UNREADABLE,
        None,
        True,
    )
    assert cell.question is not None
    assert cell.question.ids == (cell_id,)
    assert cell_id in cell.choices
    settled = answered_review(store, draft, same(cell))
    answered = row(settled, "Cell Diagram")
    assert (answered.status, answered.result_id, answered.how) == (
        ItemStatus.UNREADABLE,
        cell_id,
        "answer",
    )
    assert answered.key not in settled.ready
    refused = save(store, draft, review, matches=[same(cell)], selection={cell.key})
    assert isinstance(refused, ReviewReturned)
    assert refused.why is ReturnReason.SELECTION
    kept = saved(save(store, draft, review, matches=[same(cell)], selection=()))

    assert records(store, kept.report_id)[cell.key] == (cell_id, "answer")
    again = row(review_of(store, draft), "Cell Diagram")
    assert (again.status, again.result_id, again.how) == (
        ItemStatus.UNREADABLE,
        cell_id,
        "same_capture",
    )


def test_a_missing_due_date_is_never_matching_evidence_and_never_equals_another() -> None:
    """Rule 2: a result whose own due date wasn't captured is no candidate by its date for a row
    missing its date, and equal evidence with the date missing never matches without asking."""
    draft = undated(LEAF_SKETCH)
    poster = (LEAF_SKETCH[0], "| Poster                   | 7.0     | 12.0 ")
    leaf = ("| Seed Germination Log | 18.0    | 20.0 ", "| Leaf Sketch          | 18.0    | 15.0 ")
    (homework, *_) = undated(leaf, poster).categories
    seen = {
        f"result-{index}": CurrentValue(
            cells_of(RESULT_FIELDS, (homework.name, *one.cells())), "report-1", 1
        )
        for index, one in enumerate(homework.rows)
    }
    on_record = OnRecord(
        identity=Identity(IdentityStatus.MATCHES, "form"),
        context=True,
        year_known=True,
        matched="class-1",
        existing=(),
        revision=1,
        held=ClassRecord(CurrentValues(None, {}, {}), {}, seen, {}, {}),
        saved={},
    )
    review = review_from(draft, capture_key(draft), "acceptance-1", on_record, complete=False)
    sketch, seed = row(review, "Leaf Sketch"), row(review, "Seed Germination Log")

    assert (sketch.status, sketch.result_id, sketch.how) == (
        ItemStatus.DUE_NOT_CAPTURED,
        None,
        None,
    )
    assert sketch.question is not None
    assert sketch.question.ids == ("result-0",)
    assert seed.question is None
    assert seed.choices == ("result-0", "result-1")


def undated_different_kept(store: ProjectStateStore) -> tuple[str, str, GradeReportSaved]:
    """A saved complete; Seed Germination Log renamed Journal in a copy missing the Homework /
    Practice Due column, answered "A different assignment" and saved with no value selected:
    the rejected result, A's report, and the outcome."""
    first = saved(save(store, A, complete=True))
    seed = row(review_of(store, A), "Seed Germination Log").result_id or ""
    draft = undated(RENAMED)
    review = review_of(store, draft)
    journal = row(review, "Seed Germination Journal")
    outcome = save(store, draft, review, matches=[different(journal)], selection=())
    return seed, first.report_id or "", saved(outcome)


def test_a_different_answer_for_a_row_whose_due_wasnt_captured_replays_in_its_capture() -> None:
    """His fifteenth round: kept by the submission for the same capture and row, against the
    candidate it turned down; it creates no assignment, presence or value, the row stays Due date
    not captured and unselectable, a retry writes nothing, and the answer stays open to change.
    Once an existing assignment is chosen, the same capture replays the choice and its value
    reads Changed."""
    store = in_memory()
    first = saved(save(store, A, complete=True)).report_id or ""
    seed = row(review_of(store, A), "Seed Germination Log").result_id or ""
    draft = undated(RENAMED)
    review = review_of(store, draft)
    journal_key = row(review, "Seed Germination Journal").key
    answer = different(row(review, "Seed Germination Journal"))
    kept = saved(save(store, draft, review, matches=[answer], selection=()))

    assert (kept.added, kept.updated, kept.accepted) == (0, 0, ())
    assert (kept.shown, kept.answers_kept) == (2, 1)
    assert records(store, kept.report_id)[journal_key] == (None, "different")
    (rejected,) = store._connection.execute(
        "SELECT rejected FROM grade_match_decisions WHERE row_key = ?", (journal_key,)
    ).fetchone()
    evidence = [["reported", "Homework / Practice"], ["reported", "Seed Germination Log"]]
    assert json.loads(rejected) == [[seed, [*evidence, ["reported", "09/22"]]]]
    assert observed(store, kept.report_id) == 0
    assert len(results(store)) == 4
    value = store.current_values(class_of(store), "T1").results[seed]
    assert (value.report_id, value.last_shown) == (first, ReportAt(first, 1))
    changes = store._connection.total_changes
    assert isinstance(save(store, draft, review, matches=[answer], selection=()), AlreadyRecorded)
    assert store._connection.total_changes == changes
    replay = review_of(store, draft)
    item = row(replay, "Seed Germination Journal")

    assert (item.status, item.result_id, item.how, item.remembered) == (
        ItemStatus.DUE_NOT_CAPTURED,
        None,
        "answer",
        True,
    )
    assert item.question is not None
    assert item.question.ids == (seed,)
    assert seed in item.choices
    assert item.key not in replay.ready
    before = revision(store)
    nothing = saved(save(store, draft, replay, selection=()))
    assert (nothing.report_id, nothing.shown, nothing.answers_kept) == (None, 0, 0)
    assert revision(store) == before
    page = review_of(store, draft)
    journal = row(page, "Seed Germination Journal")
    later = saved(save(store, draft, page, matches=[chosen(journal, seed)], selection=()))

    assert later.report_id == kept.report_id
    assert records(store, kept.report_id)[journal_key] == (seed, "chosen")
    assert observed(store, kept.report_id) == 0
    assert len(results(store)) == 4
    again = row(review_of(store, draft), "Seed Germination Journal")
    assert (again.status, again.result_id, again.how, again.due_not_captured) == (
        ItemStatus.CHANGED,
        seed,
        "same_capture",
        True,
    )


def test_a_remembered_different_for_a_row_whose_due_wasnt_captured_stays_in_its_capture() -> None:
    """His fifteenth round: another capture whose row has the same title and its date missing
    too asks again, and so does the same capture once a new candidate appears; "Choose an
    existing assignment" stays open in both."""
    store = in_memory()
    seed, _, _ = undated_different_kept(store)
    other = review_of(store, undated(RENAMED, CELL_SCORE))
    item = row(other, "Seed Germination Journal")

    assert (item.status, item.result_id, item.how, item.remembered) == (
        ItemStatus.DUE_NOT_CAPTURED,
        None,
        None,
        False,
    )
    assert item.question is not None
    assert item.question.ids == (seed,)
    assert seed in item.choices
    assert item.key not in other.ready
    saved(save(store, WITH_LEAF))
    leaf = row(review_of(store, WITH_LEAF), "Leaf Sketch").result_id or ""
    same_capture = review_of(store, undated(RENAMED))
    item = row(same_capture, "Seed Germination Journal")

    assert (item.status, item.result_id, item.how, item.remembered) == (
        ItemStatus.DUE_NOT_CAPTURED,
        None,
        None,
        False,
    )
    assert item.question is not None
    assert set(item.question.ids) == {seed, leaf}
    assert {seed, leaf} <= set(item.choices)
    assert item.key not in same_capture.ready


def test_a_different_answer_for_an_undated_row_whose_score_cant_be_read_keeps_its_warning() -> None:
    """Both rules at once: remembered for its own capture only, with Couldn't read kept."""
    store = in_memory()
    saved(save(store, A, complete=True))
    cell_id = row(review_of(store, A), "Cell Diagram").result_id or ""
    draft = undated(CELL_EX)
    review = review_of(store, draft)
    cell = row(review, "Cell Diagram")
    kept = saved(save(store, draft, review, matches=[different(cell)], selection=()))

    assert records(store, kept.report_id)[cell.key] == (None, "different")
    assert observed(store, kept.report_id) == 0
    for again, remembered in ((draft, True), (undated(CELL_EX, SEED_SCORE), False)):
        later = review_of(store, again)
        item = row(later, "Cell Diagram")

        assert (item.status, item.result_id, item.remembered) == (
            ItemStatus.UNREADABLE,
            None,
            remembered,
        )
        assert item.how == ("answer" if remembered else None)
        assert item.question is not None
        assert item.question.ids == (cell_id,)
        assert cell_id in item.choices
        assert item.key not in later.ready


# ------------------------------------------------------------- a matched row missing its due date


CELL_NINE = ("| Cell Diagram             | 7.0 ", "| Cell Diagram             | 9.0 ")


def due_saved(store: ProjectStateStore, report_id: str | None) -> list[tuple[str, str, str, str]]:
    """Each result observation of a report: its result, points, and due text and presence."""
    found = store._connection.execute(
        "SELECT result_id, points_text, due_text, due_presence FROM grade_result_observations "
        "WHERE report_id = ?",
        (report_id,),
    )
    return [(str(one[0]), str(one[1]), str(one[2]), str(one[3])) for one in found]


def test_a_matched_row_missing_its_due_date_can_be_ticked_and_keeps_it_not_captured() -> None:
    """His sixteenth round: before the match the row can't be selected; the parent's answer
    makes its changed score Changed beside "Due date not captured", and ticked, it is saved as an
    observation of the matched result whose due date stays not captured, never the result's.
    A retry writes nothing, and the same capture replays it as Saved."""
    store = in_memory()
    saved(save(store, A, complete=True))
    cell_id = row(review_of(store, A), "Cell Diagram").result_id or ""
    draft = undated(CELL_SCORE)
    review = review_of(store, draft)
    cell = row(review, "Cell Diagram")

    assert (cell.status, cell.result_id, cell.due_not_captured) == (
        ItemStatus.DUE_NOT_CAPTURED,
        None,
        True,
    )
    refused = save(store, draft, review, selection={cell.key})
    assert isinstance(refused, ReviewReturned)
    assert refused.why is ReturnReason.SELECTION
    settled = answered_review(store, draft, same(cell))
    answered = row(settled, "Cell Diagram")
    assert (answered.status, answered.result_id, answered.how, answered.due_not_captured) == (
        ItemStatus.CHANGED,
        cell_id,
        "answer",
        True,
    )
    assert answered.current is not None
    assert text_of(answered.current, "points") == "7.0"
    assert answered.key in settled.ready
    outcome = saved(save(store, draft, review, matches=[same(cell)], selection={cell.key}))

    assert (outcome.added, outcome.updated) == (0, 1)
    assert outcome.accepted == ((cell.key, cell_id),)
    assert records(store, outcome.report_id)[cell.key] == (cell_id, "answer")
    assert due_saved(store, outcome.report_id) == [(cell_id, "8.0", "", "not_captured")]
    assert len(results(store)) == 4
    current = store.current_values(class_of(store), "T1").results[cell_id]
    assert current.report_id == outcome.report_id
    assert (current.cells["points"], current.cells["due"]) == (
        (Presence.REPORTED, "8.0"),
        (Presence.NOT_CAPTURED, ""),
    )
    assert current.not_shown is None
    changes = store._connection.total_changes
    retried = save(store, draft, review, matches=[same(cell)], selection={cell.key})
    assert isinstance(retried, AlreadyRecorded)
    assert store._connection.total_changes == changes
    replay = review_of(store, draft)
    again = row(replay, "Cell Diagram")
    assert (again.status, again.result_id, again.how, again.due_not_captured) == (
        ItemStatus.SAVED,
        cell_id,
        "same_capture",
        True,
    )
    assert again.key not in replay.ready


def test_a_matched_row_missing_its_due_date_left_unticked_saves_only_which_it_is() -> None:
    """His sixteenth round: matching without ticking saves only the row record, no value; the
    same capture replays the match with its value still Changed and selectable, and ticking it
    then accepts the value into the capture's report, keeping the row's one record."""
    store = in_memory()
    first = saved(save(store, A, complete=True)).report_id
    cell_id = row(review_of(store, A), "Cell Diagram").result_id or ""
    draft = undated(CELL_SCORE)
    review = review_of(store, draft)
    cell = row(review, "Cell Diagram")
    kept = saved(save(store, draft, review, matches=[same(cell)], selection=()))

    assert (kept.added, kept.updated, kept.accepted) == (0, 0, ())
    assert (kept.shown, kept.answers_kept) == (3, 1)
    assert records(store, kept.report_id)[cell.key] == (cell_id, "answer")
    assert observed(store, kept.report_id) == 0
    current = store.current_values(class_of(store), "T1").results[cell_id]
    assert (current.report_id, text_of(current, "points"), text_of(current, "due")) == (
        first,
        "7.0",
        "09/26",
    )
    replay = review_of(store, draft)
    again = row(replay, "Cell Diagram")
    assert (again.status, again.result_id, again.how, again.question) == (
        ItemStatus.CHANGED,
        cell_id,
        "same_capture",
        None,
    )
    assert again.due_not_captured
    assert again.key in replay.ready
    later = saved(save(store, draft, replay, selection={again.key}))

    assert later.report_id == kept.report_id
    assert (later.updated, later.accepted) == (1, ((cell.key, cell_id),))
    assert records(store, kept.report_id)[cell.key] == (cell_id, "answer")
    (count,) = store._connection.execute(
        "SELECT COUNT(*) FROM grade_match_decisions WHERE row_key = ?", (cell.key,)
    ).fetchone()
    assert count == 1
    assert due_saved(store, kept.report_id) == [(cell_id, "8.0", "", "not_captured")]


def test_a_matched_row_missing_its_due_date_reads_matches_earlier_or_shown_in_a_newer_report() -> (
    None
):
    """Its status is the ordinary one against the matched result, the due date left out of the
    comparison on both sides: A's score after newer B replaced it matches an earlier saved value,
    and a changed score in a capture whose report comes before B's is shown in a newer report.
    Neither is offered."""
    store = in_memory()
    saved(save(store, A, complete=True))
    cell_id = row(review_of(store, A), "Cell Diagram").result_id or ""
    before_b = undated(CELL_NINE)
    first_look = review_of(store, before_b)
    seed = row(first_look, "Seed Germination Log")
    saved(save(store, before_b, first_look, matches=[same(seed)], selection=()))
    newer = saved(save(store, B))
    assert dict(newer.accepted).get(row(review_of(store, B), "Cell Diagram").key) == cell_id

    for draft, status, covered in (
        (undated(), ItemStatus.MATCHES_EARLIER, False),
        (before_b, ItemStatus.COVERED, True),
    ):
        asked = row(review_of(store, draft), "Cell Diagram")
        settled = answered_review(store, draft, same(asked))
        item = row(settled, "Cell Diagram")

        assert (item.status, item.result_id, item.covered, item.due_not_captured) == (
            status,
            cell_id,
            covered,
            True,
        )
        assert item.key not in settled.ready


def test_after_a_value_saved_from_a_copy_missing_its_dates_the_next_dated_capture_asks() -> None:
    """Rev 3k, 9.3 point 2, as built: a result's evidence and shown due date come from its
    observation in the highest acceptance order, so after a value saved from a copy missing its
    dates the result shows its due date as not captured, and the next dated capture of it asks
    "Same assignment?" rather than matching by itself. Answered, its value reads Changed by the
    due date it now supplies."""
    store = in_memory()
    saved(save(store, A, complete=True))
    cell_id = row(review_of(store, A), "Cell Diagram").result_id or ""
    draft = undated(CELL_SCORE)
    cell = row(review_of(store, draft), "Cell Diagram")
    saved(save(store, draft, matches=[same(cell)], selection={cell.key}))
    current = store.current_values(class_of(store), "T1").results[cell_id]
    assert current.cells["due"] == (Presence.NOT_CAPTURED, "")
    review = review_of(store, B)
    asked, seed = row(review, "Cell Diagram"), row(review, "Seed Germination Log")

    assert (asked.status, asked.result_id, asked.how) == (ItemStatus.NEEDS_ANSWER, None, None)
    assert asked.question is not None
    assert (asked.question.kind, asked.question.ids) == (QuestionKind.WHICH, (cell_id,))
    assert asked.key not in review.ready
    assert (seed.status, seed.how) == (ItemStatus.SAVED, "exact")
    answered = row(answered_review(store, B, same(asked)), "Cell Diagram")
    assert (answered.status, answered.result_id, answered.due_not_captured) == (
        ItemStatus.CHANGED,
        cell_id,
        False,
    )
