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
    AlreadyRecorded,
    GradeReportSaved,
    GradeReview,
    ItemStatus,
    MatchAnswer,
    QuestionKind,
    ReportAt,
    ReturnReason,
    ReviewItem,
    ReviewReturned,
    SaveOutcome,
)
from blossom.grades.text_reader import read_grade_report, reading_complete
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
) -> SaveOutcome:
    """A parent's save with every setup question answered, ``matches`` as the matching answers,
    and the ready values plus the answered rows selected unless ``selection`` says otherwise; its
    reading incomplete unless ``complete``."""
    review = review or review_of(store, draft)
    answers = dataclasses.replace(grade_answers(review), matches=tuple(matches))
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

    assert (cell.status, cell.covered) == (ItemStatus.CHANGED, True)
    assert cell.key not in rest.ready
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
