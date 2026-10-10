# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The grade review and the save, for new results and a capture saved again.

A review says what a pasted report would do and writes nothing: her identity, the setup questions,
and each value's status. A save is one transaction that rechecks the page against the record,
then writes the parent's selection with an acceptance record, or writes nothing and says why.
"""

import dataclasses
import pathlib
import sqlite3
from collections.abc import Callable, Collection

import pytest

from blossom.grades.draft import GradeReportDraft, Presence, capture_key
from blossom.grades.identity import IdentityStatus, key_check, name_form, name_form_key
from blossom.grades.review import (
    CLASS_NAME_LIMIT,
    TERM_KEY,
    TERM_LIMIT,
    AlreadyRecorded,
    GradeAnswers,
    GradeReportSaved,
    GradeReview,
    IdentityAnswer,
    ItemStatus,
    MatchAnswer,
    NotHers,
    ReturnReason,
    ReviewPage,
    ReviewReturned,
    SaveOutcome,
    StillAsked,
    answers_asked,
    class_asked,
    first_month_asked,
    identity_asked,
    item_keys,
    labels_too_long,
    setup_asked,
    still_asked,
)
from blossom.grades.text_reader import (
    HeldBack,
    LinePlace,
    read_grade_report,
    reading_complete,
)
from blossom.stores.gradebook import GRADEBOOK_TABLES, SCOPE_COMPLETE, GradeReportNotSaved
from blossom.stores.project_state import ProjectStateStore
from tests.support import FIXTURES, as_stored, fixture_clock, grade_answers, save_grade

REPORT = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")
"""Wren's synthetic report: Biology, 2026-2027, T1, four categories and four results."""
WREN = "Bramble, Wren"
LINNET = "Bramble, Linnet"
KEY = name_form_key(b"5" * 64)
NEW_KEY = name_form_key(b"6" * 64)
SEVEN = "| Cell Diagram             | 7.0 "


def draft_of(text: str = REPORT) -> GradeReportDraft:
    reading = read_grade_report(text)
    assert reading.draft is not None, reading.not_read
    return reading.draft


WREN_DRAFT = draft_of()
LINNET_DRAFT = draft_of(REPORT.replace("**Bramble, Wren**", "**Bramble, Linnet**"))
MISSING_DRAFT = draft_of(REPORT.replace("**Bramble, Wren**", ""))
EIGHT_DRAFT = draft_of(REPORT.replace(SEVEN, SEVEN.replace("7.0", "8.0")))
"""Another capture of the same class and term: one score differs."""
CHEMISTRY_DRAFT = draft_of(
    REPORT.replace("07 BIO - C", "07 CHEM - A").replace("Biology", "Chemistry")
)
"""A report for another class in the same year and term."""
UNREADABLE_DRAFT = draft_of(REPORT.replace("| 7.0     | 10.0    |", "| 7,0     | 10.0    |"))
OLDER_DRAFT = draft_of(REPORT.replace("**2026-2027**", "**2025-2026**").replace("**T1**", "**T3**"))
"""Last year's report for the class, imported first."""


def in_memory() -> ProjectStateStore:
    return ProjectStateStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())


def review_of(
    store: ProjectStateStore, draft: GradeReportDraft = WREN_DRAFT, *, key: bytes = KEY
) -> GradeReview:
    return store.review_grade_report(draft, capture_key(draft), key=key)


answers_to = grade_answers


def settled_by(
    store: ProjectStateStore, draft: GradeReportDraft = WREN_DRAFT, *, key: bytes = KEY
) -> Callable[[tuple[MatchAnswer, ...]], GradeReview]:
    """How a returned page settles the review of ``draft`` with the matching answers it keeps."""

    def settle(matches: tuple[MatchAnswer, ...]) -> GradeReview:
        return store.review_grade_report(draft, capture_key(draft), key=key, matches=matches)

    return settle


def save(
    store: ProjectStateStore,
    draft: GradeReportDraft = WREN_DRAFT,
    review: GradeReview | None = None,
    *,
    key: bytes = KEY,
    answers: GradeAnswers | None = None,
    selection: Collection[str] | None = None,
    complete: bool = False,
) -> SaveOutcome:
    """A parent's save of Wren's report unless another is named, under the first key, its
    reading incomplete unless ``complete``."""
    return save_grade(
        store,
        draft,
        key=key,
        review=review,
        answers=answers,
        selection=selection,
        complete=complete,
    )


def gradebook_of(store: ProjectStateStore) -> dict[str, list[tuple[object, ...]]]:
    return {table: as_stored(store, table) for table in GRADEBOOK_TABLES}


def counted(store: ProjectStateStore) -> dict[str, int]:
    return {table: len(rows) for table, rows in gradebook_of(store).items()}


def statuses(review: GradeReview) -> list[ItemStatus]:
    return [item.status for item in review.items]


def one(store: ProjectStateStore, sql: str, *values: object) -> list[tuple[object, ...]]:
    return store._connection.execute(sql, values).fetchall()


def test_the_first_setup_confirms_the_year_and_term_current_now_not_the_report_s() -> None:
    """An older report imported first leaves the parent free to confirm the year and term that
    are current now; the report keeps its own, and the current context isn't moved by it."""
    store = in_memory()
    review = review_of(store, OLDER_DRAFT)
    answers = dataclasses.replace(answers_to(review), setup=("2026-2027", " T1 "))
    saved(save_grade(store, OLDER_DRAFT, key=KEY, review=review, answers=answers))

    assert review.setup == ("2025-2026", "T3")
    assert one(store, "SELECT year_label, term_label FROM grade_context") == [("2026-2027", "T1")]
    assert one(store, "SELECT year_label, label FROM grade_terms") == [("2025-2026", "T3")]


@pytest.mark.parametrize("setup", [("2026", "T1"), ("2026-2028", "T1"), ("2026-2027", "  ")])
def test_a_setup_that_is_not_a_school_year_and_a_term_returns_the_review(
    setup: tuple[str, str],
) -> None:
    store = in_memory()
    review = review_of(store)
    changes = store._connection.total_changes
    answers = dataclasses.replace(answers_to(review), setup=setup)
    returned(
        save_grade(store, WREN_DRAFT, key=KEY, review=review, answers=answers),
        ReturnReason.ANSWERS,
    )
    assert store._connection.total_changes == changes


def returned(outcome: SaveOutcome | GradeReview, why: ReturnReason) -> GradeReview:
    assert isinstance(outcome, ReviewReturned), outcome
    assert outcome.why is why
    return outcome.review


def saved(outcome: SaveOutcome) -> GradeReportSaved:
    assert isinstance(outcome, GradeReportSaved), outcome
    return outcome


# ------------------------------------------------------------- the review


def test_a_first_review_asks_every_setup_question_and_writes_nothing() -> None:
    store = in_memory()
    changes = store._connection.total_changes
    review = review_of(store)

    assert store._connection.total_changes == changes
    assert review.source_key == capture_key(WREN_DRAFT)
    assert review.identity.status is IdentityStatus.FIRST_USE
    assert review.identity.form == name_form(KEY, WREN)
    assert review.setup == ("2026-2027", "T1")
    assert review.first_month == "2026-2027"
    assert (review.class_question.matched, review.class_question.offered_name) == (
        None,
        "Biology",
    )
    assert review.class_question.existing == ()
    assert review.revision is None
    assert review.term is not None
    assert [len(review.categories), len(review.rows)] == [4, 4]
    assert set(statuses(review)) == {ItemStatus.NEW}
    assert review.ready == frozenset(item.key for item in review.items)
    assert review_of(store).acceptance_id != review.acceptance_id


# ------------------------------------------------------------- a first save, and its rest


def test_a_first_save_writes_exactly_the_expected_rows_and_raises_the_revision() -> None:
    store = in_memory()
    review = review_of(store)
    outcome = saved(save(store, review=review))

    assert (outcome.added, outcome.updated, outcome.already_saved, outcome.left) == (9, 0, 0, 0)
    assert outcome.acceptance_id == review.acceptance_id
    assert counted(store) == {
        "grade_student": 1,
        "grade_name_forms": 1,
        "grade_context": 1,
        "grade_years": 1,
        "grade_terms": 1,
        "grade_classes": 1,
        "grade_class_aliases": 1,
        "grade_reports": 1,
        "grade_term_observations": 1,
        "grade_category_observations": 4,
        "grade_results": 4,
        "grade_result_observations": 4,
        "grade_match_decisions": 4,
        "grade_scope_revisions": 1,
        "grade_acceptances": 1,
        "grade_current_actions": 0,
        "grade_corrections": 0,
        "grade_homework_classes": 0,
    }
    assert one(store, "SELECT year_label, term_label FROM grade_context") == [("2026-2027", "T1")]
    assert one(store, "SELECT label, first_month FROM grade_years") == [("2026-2027", 8)]
    assert one(store, "SELECT display_name FROM grade_classes") == [("Biology",)]
    assert one(store, "SELECT code, name FROM grade_class_aliases") == [("07 BIO - C", "Biology")]
    assert one(
        store, "SELECT source_key, acceptance_order, use, reader, result_rows FROM grade_reports"
    ) == [(capture_key(WREN_DRAFT), 1, "current", "text", 4)]
    assert one(store, "SELECT DISTINCT how FROM grade_match_decisions") == [("new",)]
    assert one(store, "SELECT revision FROM grade_scope_revisions") == [(1,)]
    assert one(
        store,
        "SELECT kind, source_key, report_id, identity_status, identity_answer, identity_form, "
        "added, updated, already_saved, left_to_check, role FROM grade_acceptances",
    ) == [
        (
            "grade_text",
            capture_key(WREN_DRAFT),
            outcome.report_id,
            "first_use",
            "hers",
            name_form(KEY, WREN),
            9,
            0,
            0,
            0,
            "parent",
        )
    ]
    again = review_of(store)
    assert again.identity.status is IdentityStatus.MATCHES
    assert (again.setup, again.first_month, again.revision) == (None, None, 1)
    assert again.class_question.matched is not None
    assert set(statuses(again)) == {ItemStatus.SAVED}
    assert again.ready == frozenset()
    assert {item.result_id for item in again.rows} == {
        row[0] for row in one(store, "SELECT result_id FROM grade_results")
    }


def test_a_partial_save_then_the_rest_joins_the_same_report() -> None:
    store = in_memory()
    first = review_of(store)
    chosen = {TERM_KEY, first.categories[0].key, first.rows[0].key}
    partial = saved(save(store, review=first, selection=chosen))
    middle = review_of(store)
    rest = saved(save(store, review=middle))

    assert [item.status for item in middle.items if item.key in chosen] == [ItemStatus.SAVED] * 3
    assert middle.ready == frozenset(item.key for item in first.items) - chosen
    assert (partial.added, partial.left) == (3, 6)
    assert (rest.added, rest.already_saved, rest.left) == (6, 3, 0)
    assert rest.report_id == partial.report_id
    assert one(store, "SELECT COUNT(*) FROM grade_reports") == [(1,)]
    assert one(store, "SELECT COUNT(*) FROM grade_results") == [(4,)]
    assert one(store, "SELECT COUNT(*) FROM grade_acceptances") == [(2,)]
    assert one(store, "SELECT revision FROM grade_scope_revisions") == [(2,)]
    assert set(statuses(review_of(store))) == {ItemStatus.SAVED}


def test_a_tab_separated_copy_saves_and_its_markdown_serialization_reads_saved() -> None:
    """The gradebook's own tab-separated copy feeds the review and the save unchanged: every
    value saves, a title keeps the space its cell held, and the same report as Markdown, with
    that space trimmed, is the same capture and reads Saved."""
    geometry = FIXTURES / "grade_clipboard" / "geometry-grade-report.txt"
    tabs = draft_of(geometry.read_bytes().decode("utf-8"))
    markdown = draft_of((geometry.parent / "markdown" / geometry.name).read_bytes().decode("utf-8"))
    store = in_memory()
    outcome = saved(save(store, tabs, review=review_of(store, tabs)))

    assert (outcome.added, outcome.updated, outcome.left) == (22, 0, 0)
    titles = [
        str(row[0]) for row in one(store, "SELECT assignment_text FROM grade_result_observations")
    ]
    assert len(titles) == 18
    assert sum(title.endswith("Use the examples ") for title in titles) == 1
    assert capture_key(markdown) == capture_key(tabs)
    assert set(statuses(review_of(store, markdown))) == {ItemStatus.SAVED}


STRAY = "Updated 10/06/2026"
"""A plain line no structure explains, of the kind a copy may carry."""
ABOVE = "Practice 2.2: classroom exercise\t8\t14\t63\tValid\t09/10\t0\t0\t\t1.0\t"
BELOW = "Practice 2.3: classroom exercise\t9\t15\t64\tValid\t09/17\t0\t0\t\t1.0\t"
SPACED = "Practice 2.5: classroom exercise"
"""The row whose Pts cell gets a space inside its number."""


def test_a_stray_line_holds_back_its_two_rows_and_the_other_rows_still_save() -> None:
    """Uncertainty stays local: a plain line between two tab rows holds both back, all three
    lines visible; a number with a space inside its cell is unreadable, its text kept, and never
    offered; every other row is offered and saves, and the acceptance records the reading as
    incomplete."""
    humanities = FIXTURES / "grade_clipboard" / "humanities-grade-report.txt"
    text = humanities.read_bytes().decode("utf-8")
    assert text.count(f"{ABOVE}\r\n{BELOW}") == 1
    text = text.replace(f"{ABOVE}\r\n{BELOW}", f"{ABOVE}\r\n{STRAY}\r\n{BELOW}")
    assert text.count(f"{SPACED}\t7\t") == 1
    text = text.replace(f"{SPACED}\t7\t", f"{SPACED}\t9 .0\t")
    reading = read_grade_report(text)
    draft = reading.draft
    assert draft is not None
    assert reading.unrecognized == (ABOVE, STRAY, BELOW)
    assert reading.places == (LinePlace.BEFORE_TERM,) * 3
    assert reading.held_back == (HeldBack(rows=(0, 2), stray=(1,)),)
    assert reading_complete(reading) is False
    rows = [row for category in draft.categories for row in category.rows]
    titles = [row.assignment.text for row in rows]
    assert len(rows) == 10
    assert not {ABOVE.split("\t")[0], BELOW.split("\t")[0]} & set(titles)
    points = rows[titles.index(SPACED)].points
    assert (points.presence, points.text) == (Presence.UNREADABLE, "9 .0")

    store = in_memory()
    review = review_of(store, draft)
    by_title = dict(zip(titles, review.rows, strict=True))
    spaced = by_title.pop(SPACED)
    assert spaced.status is ItemStatus.UNREADABLE
    assert spaced.key not in review.ready
    assert {item.status for item in by_title.values()} == {ItemStatus.NEW}
    assert {item.key for item in by_title.values()} <= review.ready
    outcome = saved(save(store, draft, review=review, complete=reading_complete(reading)))

    assert (outcome.added, outcome.updated) == (1 + 3 + 9, 0)
    stored = one(store, "SELECT assignment_text, points_text FROM grade_result_observations")
    assert sorted(stored) == sorted(
        (title, row.points.text) for title, row in zip(titles, rows, strict=True) if title != SPACED
    )
    assert one(store, "SELECT complete FROM grade_acceptances") == [(0,)]
    ((student, class_id, term),) = one(
        store, "SELECT student_id, class_id, term_label FROM grade_reports"
    )
    scope = {"student": student, "class": class_id, "term": term}
    assert [complete for _, complete in store._connection.execute(SCOPE_COMPLETE, scope)] == [0]


def test_a_restart_keeps_a_committed_save(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "blossom.sqlite3"
    store = ProjectStateStore.open(path, fixture_clock())
    saved(save(store))
    store.close()
    again = ProjectStateStore.open(path, fixture_clock())
    review = review_of(again)
    again.close()

    assert review.identity.status is IdentityStatus.MATCHES
    assert set(statuses(review)) == {ItemStatus.SAVED}


# ------------------------------------------------------------- his tenth-round cases


def test_identical_reports_for_two_siblings_ask_whose_report_it_is() -> None:
    store = in_memory()
    saved(save(store))
    before = gradebook_of(store)
    review = review_of(store, LINNET_DRAFT)

    assert capture_key(LINNET_DRAFT) == capture_key(WREN_DRAFT)
    assert review.identity.status is IdentityStatus.NOT_CONFIRMED
    assert review.identity.form == name_form(KEY, LINNET)
    assert set(statuses(review)) == {ItemStatus.SAVED}
    assert gradebook_of(store) == before


def test_identical_reports_for_two_siblings_not_hers_saves_nothing() -> None:
    store = in_memory()
    saved(save(store))
    before = gradebook_of(store)
    review = review_of(store, LINNET_DRAFT)

    outcome = save(store, LINNET_DRAFT, review, answers=answers_to(review, IdentityAnswer.NOT_HERS))

    assert isinstance(outcome, NotHers)
    assert gradebook_of(store) == before


def test_a_sibling_s_newer_report_answered_not_hers_records_no_presence_and_no_answer() -> None:
    """A sibling's report whose rows match Wren's results, with a changed score and a matching
    answer given: "Not hers" records no row as shown, keeps no answer and accepts nothing."""
    store = in_memory()
    saved(save(store))
    sibling = draft_of(
        REPORT.replace("**Bramble, Wren**", "**Bramble, Linnet**")
        .replace(SEVEN, SEVEN.replace("7.0", "8.0"))
        .replace("| Seed Germination Log |", "| Seed Germination Journal |")
    )
    review = review_of(store, sibling)
    asked = [item for item in review.rows if item.question is not None]
    before = gradebook_of(store)
    assert len(asked) == 1
    assert asked[0].question is not None
    answers = dataclasses.replace(
        answers_to(review, IdentityAnswer.NOT_HERS),
        matches=(MatchAnswer(asked[0].key, asked[0].question.ids, asked[0].question.ids[0]),),
    )

    outcome = save(store, sibling, review, answers=answers, selection=())

    assert isinstance(outcome, NotHers)
    assert gradebook_of(store) == before


def test_identical_reports_for_two_siblings_misread_saves_only_an_acceptance_record() -> None:
    store = in_memory()
    saved(save(store))
    before = gradebook_of(store)
    review = review_of(store, LINNET_DRAFT)

    outcome = saved(
        save(
            store,
            LINNET_DRAFT,
            review,
            answers=answers_to(review, IdentityAnswer.MISREAD),
            complete=True,
        )
    )

    after = gradebook_of(store)
    assert (outcome.added, outcome.already_saved, outcome.report_id) == (0, 9, None)
    assert {table for table in GRADEBOOK_TABLES if after[table] != before[table]} == {
        "grade_acceptances",
    }
    assert len(after["grade_acceptances"]) == 2
    assert after["grade_name_forms"] == before["grade_name_forms"]
    assert one(
        store,
        "SELECT identity_status, identity_answer, identity_form, report_id FROM grade_acceptances "
        "WHERE acceptance_id = ?",
        review.acceptance_id,
    ) == [("not_confirmed", "misread", name_form(KEY, LINNET), None)]
    assert review_of(store, LINNET_DRAFT).identity.status is IdentityStatus.NOT_CONFIRMED


def test_identical_reports_for_two_siblings_a_shown_match_answers_nothing() -> None:
    store = in_memory()
    saved(save(store))
    before = gradebook_of(store)
    review = review_of(store, LINNET_DRAFT)

    outcome = save(store, LINNET_DRAFT, review, answers=answers_to(review, IdentityAnswer.SHOWN))

    returned(outcome, ReturnReason.ANSWERS)
    assert gradebook_of(store) == before


def test_a_report_with_its_student_line_missing_needs_confirmation_and_adds_no_form() -> None:
    store = in_memory()
    review = review_of(store, MISSING_DRAFT)
    assert (review.identity.status, review.identity.form) == (IdentityStatus.MISSING, None)

    for unasked in (IdentityAnswer.SHOWN, IdentityAnswer.HERS, IdentityAnswer.MISREAD):
        outcome = save(store, MISSING_DRAFT, review, answers=answers_to(review, unasked))
        returned(outcome, ReturnReason.ANSWERS)
        assert counted(store)["grade_acceptances"] == 0
    confirmed = saved(save(store, MISSING_DRAFT, review))

    assert confirmed.added == 9
    assert one(store, "SELECT COUNT(*) FROM grade_name_forms") == [(0,)]
    assert one(store, "SELECT key_check FROM grade_student") == [(None,)]
    assert one(store, "SELECT identity_status, identity_answer FROM grade_acceptances") == [
        ("missing", "confirmed")
    ]


def test_the_student_line_changed_after_its_answer_returns_the_review() -> None:
    """Another paste into the same review: the same capture, another line, so the answer the
    page holds was made for a line that isn't read now."""
    store = in_memory()
    review = review_of(store)

    outcome = save(store, LINNET_DRAFT, review, answers=answers_to(review))

    again = returned(outcome, ReturnReason.ANSWERS)
    assert again.identity.form == name_form(KEY, LINNET)
    assert counted(store) == {
        table: 1 if table == "grade_student" else 0 for table in GRADEBOOK_TABLES
    }


def test_a_genuine_retry_writes_nothing_and_returns_the_recorded_outcome() -> None:
    store = in_memory()
    review = review_of(store)
    first = saved(save(store, review=review))
    before = gradebook_of(store)
    changes = store._connection.total_changes

    retry = save(store, review=review, answers=answers_to(review))

    assert retry == AlreadyRecorded(first, frozenset())
    assert store._connection.total_changes == changes
    assert gradebook_of(store) == before


def test_the_same_id_with_another_selection_returns_the_outcome_and_the_uncovered_rows() -> None:
    store = in_memory()
    review = review_of(store)
    first = saved(save(store, review=review, selection={TERM_KEY, review.rows[0].key}))
    before = gradebook_of(store)

    retry = save(store, review=review, selection={TERM_KEY, review.rows[1].key})

    assert retry == AlreadyRecorded(first, frozenset({review.rows[1].key}))
    assert gradebook_of(store) == before


def test_the_secret_replaced_asks_again_and_confirming_keeps_the_first_acceptance() -> None:
    store = in_memory()
    saved(save(store))
    first_acceptance = gradebook_of(store)["grade_acceptances"]
    review = review_of(store, key=NEW_KEY)

    assert review.source_key == capture_key(WREN_DRAFT)
    assert review.identity.status is IdentityStatus.CONFIRM_AGAIN
    assert review.identity.form == name_form(NEW_KEY, WREN)
    assert set(statuses(review)) == {ItemStatus.SAVED}
    outcome = saved(save(store, review=review, key=NEW_KEY))

    assert (outcome.added, outcome.already_saved) == (0, 9)
    assert one(store, "SELECT name_form FROM grade_name_forms") == [(name_form(NEW_KEY, WREN),)]
    assert one(store, "SELECT key_check FROM grade_student") == [(key_check(NEW_KEY),)]
    assert gradebook_of(store)["grade_acceptances"][:1] == first_acceptance
    after = review_of(store, key=NEW_KEY)
    assert after.identity.status is IdentityStatus.MATCHES
    assert set(statuses(after)) == {ItemStatus.SAVED}


def test_lookups_are_scoped_to_her_student_id() -> None:
    """An acceptance and a report kept under another student ID, for the same capture, resolve
    nothing of hers."""
    store = in_memory()
    other = in_memory()
    saved(save(other))
    for table in ("grade_acceptances", "grade_reports"):
        for row in one(other, f"SELECT * FROM {table}"):  # noqa: S608
            marks = ", ".join("?" * len(row))
            store._connection.execute(f"INSERT INTO {table} VALUES ({marks})", row)  # noqa: S608
    store._connection.commit()
    assert one(store, "SELECT COUNT(*) FROM grade_acceptances") == [(1,)]
    assert store.student_id() != other.student_id()

    review = review_of(store)

    assert set(statuses(review)) == {ItemStatus.NEW}
    assert saved(save(store, review=review)).added == 9


# ------------------------------------------------------------- what a save refuses


def test_a_stale_revision_returns_the_review_and_writes_nothing() -> None:
    store = in_memory()
    first = review_of(store)
    saved(save(store, review=first, selection={TERM_KEY}))
    one_tab, another_tab = review_of(store), review_of(store)
    saved(save(store, review=one_tab))
    before = gradebook_of(store)

    outcome = save(store, review=another_tab)

    again = returned(outcome, ReturnReason.REVISION)
    assert (another_tab.revision, again.revision) == (1, 2)
    assert set(statuses(again)) == {ItemStatus.SAVED}
    assert gradebook_of(store) == before


def test_a_no_op_that_completes_the_setup_returns_another_page_for_that_class() -> None:
    """A submission that only confirms her name, the year and term, the first month and a new
    class records nothing in the class and term, so no revision is raised. A second page open
    for that class meets the recheck of its answers instead: it returns the review, writing
    nothing, and its save then adds the values once, with one setup and one class."""
    store = in_memory()
    one_tab, another_tab = review_of(store), review_of(store)
    nothing = saved(save(store, review=one_tab, selection=()))

    assert (nothing.report_id, nothing.added, nothing.shown) == (None, 0, 0)
    setup = ("grade_student", "grade_name_forms", "grade_context", "grade_years", "grade_terms")
    made = (*setup, "grade_classes", "grade_class_aliases", "grade_acceptances")
    assert counted(store) == {table: int(table in made) for table in GRADEBOOK_TABLES}
    before = gradebook_of(store)
    outcome = save(store, review=another_tab)

    again = returned(outcome, ReturnReason.ANSWERS)
    assert gradebook_of(store) == before
    assert (again.setup, again.first_month, again.revision) == (None, None, None)
    assert again.identity.status is IdentityStatus.MATCHES
    (class_id,) = one(store, "SELECT class_id FROM grade_classes")
    assert again.class_question.matched == class_id[0]
    assert again.ready == frozenset(item.key for item in another_tab.items)
    rest = saved(save(store, review=again))

    assert rest.added == 9
    after = counted(store)
    assert {table: after[table] for table in made} == {
        table: 2 if table == "grade_acceptances" else 1 for table in made
    }
    assert (after["grade_results"], after["grade_reports"]) == (4, 1)
    assert one(store, "SELECT revision FROM grade_scope_revisions") == [(1,)]


def stale_identity(store: ProjectStateStore, review: GradeReview) -> GradeAnswers:
    """The page answered "Yes, this is her name"; another tab confirmed it meanwhile."""
    store.add_name_form(KEY, WREN, "parent")
    return answers_to(review)


def stale_setup(store: ProjectStateStore, review: GradeReview) -> GradeAnswers:
    """The page confirmed the year and term; another class's save set them meanwhile, and
    confirmed her line, so the line is answered as shown."""
    saved(save(store, CHEMISTRY_DRAFT))
    answers = answers_to(review, month=None)
    return GradeAnswers(
        identity=IdentityAnswer.SHOWN,
        identity_form=answers.identity_form,
        setup=answers.setup,
        new_class=answers.new_class,
    )


def stale_month(store: ProjectStateStore, review: GradeReview) -> GradeAnswers:
    """The page named the year's first month; another class's save confirmed the year
    meanwhile. The setup, which the review doesn't ask now, is left out."""
    saved(save(store, CHEMISTRY_DRAFT))
    answers = answers_to(review)
    return GradeAnswers(
        identity=IdentityAnswer.SHOWN,
        identity_form=answers.identity_form,
        first_month=answers.first_month,
        new_class=answers.new_class,
    )


def unknown_class(store: ProjectStateStore, review: GradeReview) -> GradeAnswers:
    """The page names, as the same class, one the year doesn't have."""
    answers = answers_to(review)
    return GradeAnswers(
        identity=answers.identity,
        identity_form=answers.identity_form,
        setup=answers.setup,
        first_month=answers.first_month,
        same_class="class-of-no-year",
    )


def unanswered_class(store: ProjectStateStore, review: GradeReview) -> GradeAnswers:
    """The page sends no answer to the class it asked about."""
    answers = answers_to(review)
    return GradeAnswers(
        identity=answers.identity,
        identity_form=answers.identity_form,
        setup=answers.setup,
        first_month=answers.first_month,
    )


@pytest.mark.parametrize(
    "stale", [stale_identity, stale_setup, stale_month, unknown_class, unanswered_class]
)
def test_every_stale_answer_returns_the_review_and_writes_nothing(
    stale: Callable[[ProjectStateStore, GradeReview], GradeAnswers],
) -> None:
    store = in_memory()
    review = review_of(store)
    answers = stale(store, review)
    before = gradebook_of(store)

    outcome = save(store, review=review, answers=answers)

    returned(outcome, ReturnReason.ANSWERS)
    assert gradebook_of(store) == before


def test_a_class_matched_since_the_page_was_made_returns_the_review() -> None:
    store = in_memory()
    review = review_of(store)
    saved(save(store, EIGHT_DRAFT))
    before = gradebook_of(store)

    outcome = save(store, review=review, answers=answers_to(review, IdentityAnswer.SHOWN))

    again = returned(outcome, ReturnReason.REVISION)
    assert again.class_question.matched is not None
    assert gradebook_of(store) == before


def test_the_same_class_answer_saves_into_that_class_with_an_alias() -> None:
    store = in_memory()
    saved(save(store, CHEMISTRY_DRAFT))
    review = review_of(store)
    (chemistry,) = review.class_question.existing
    answers = answers_to(review)

    outcome = save(
        store,
        review=review,
        answers=GradeAnswers(
            identity=answers.identity,
            identity_form=answers.identity_form,
            same_class=chemistry[0],
            same_class_revision=chemistry[2],
        ),
        selection=(),
    )

    assert saved(outcome).added == 0
    assert one(store, "SELECT COUNT(*) FROM grade_classes") == [(1,)]
    assert one(store, "SELECT code FROM grade_class_aliases ORDER BY code") == [
        ("07 BIO - C",),
        ("07 CHEM - A",),
    ]
    after = review_of(store)
    assert after.class_question.matched == chemistry[0]
    assert set(statuses(after)) == {ItemStatus.SAVED}


def test_a_page_for_another_reading_of_the_report_saves_nothing() -> None:
    """A page reviewed one capture; a save that brings another, a score changed, returns the
    review rather than saving values nobody saw."""
    store = in_memory()
    review = review_of(store)
    changes = store._connection.total_changes

    outcome = save_grade(store, EIGHT_DRAFT, key=KEY, review=review, answers=answers_to(review))

    assert store._connection.total_changes == changes
    assert returned(outcome, ReturnReason.SOURCE).source_key == capture_key(EIGHT_DRAFT)


def test_the_same_class_answer_is_held_to_the_revision_its_page_showed() -> None:
    """The class a page offered as the same is rechecked by its own scope revision: a save that
    changed it meanwhile returns the review."""
    store = in_memory()
    saved(save(store))
    other_code = draft_of(
        REPORT.replace("**T1**", "**T2**")
        .replace("07 BIO - C", "07 BIO - D")
        .replace("Biology", "Biology Lab")
    )
    review = review_of(store, other_code)
    (biology,) = review.class_question.existing
    saved(save(store, draft_of(REPORT.replace("**T1**", "**T2**"))))
    changes = store._connection.total_changes
    answers = dataclasses.replace(answers_to(review), new_class=None, same_class=biology[0])

    outcome = save_grade(store, other_code, key=KEY, review=review, answers=answers)

    assert store._connection.total_changes == changes
    returned(outcome, ReturnReason.REVISION)


def test_a_term_spaced_another_way_is_the_same_term() -> None:
    """The capture key folds a term's spaces, and so does every scope: a second paste of the
    same report with its term spaced another way saves into the same term."""
    store = in_memory()
    saved(save(store, draft_of(REPORT.replace("**T1**", "**Term 1**"))))
    again = draft_of(REPORT.replace("**T1**", "**Term  1**"))
    review = review_of(store, again)

    saved(
        save_grade(store, again, key=KEY, review=review, answers=answers_to(review), complete=True)
    )

    assert set(statuses(review)) == {ItemStatus.SAVED}
    assert one(store, "SELECT label FROM grade_terms") == [("Term 1",)]
    assert one(store, "SELECT term_label, revision FROM grade_scope_revisions") == [("Term 1", 1)]
    assert one(store, "SELECT COUNT(*) FROM grade_acceptances") == [(2,)]


def test_another_capture_s_saved_values_cannot_be_selected() -> None:
    """Another capture of the class and term: its values equal to the saved ones read Saved and
    can't be selected; only the changed score is offered. A save with nothing selected accepts
    no value, and records the four rows the report shows."""
    store = in_memory()
    saved(save(store))
    review = review_of(store, EIGHT_DRAFT)
    before = gradebook_of(store)

    assert statuses(review).count(ItemStatus.CHANGED) == 1
    assert set(statuses(review)) == {ItemStatus.SAVED, ItemStatus.CHANGED}
    assert review.ready == {review.rows[1].key}
    for chosen in (TERM_KEY, review.categories[0].key, review.rows[0].key):
        returned(save(store, EIGHT_DRAFT, review, selection={chosen}), ReturnReason.SELECTION)
        assert gradebook_of(store) == before
    nothing = saved(save(store, EIGHT_DRAFT, review, selection=()))
    assert (nothing.added, nothing.updated, nothing.left) == (0, 0, 1)
    assert (nothing.shown, nothing.answers_kept, nothing.accepted) == (4, 0, ())
    assert nothing.report_id is not None
    assert one(store, "SELECT COUNT(*) FROM grade_results") == [(4,)]
    after = gradebook_of(store)
    for table in ("grade_result_observations", "grade_term_observations", "grade_results"):
        assert after[table] == before[table]
    assert one(
        store,
        "SELECT how, COUNT(*) FROM grade_match_decisions WHERE report_id = ? GROUP BY how",
        nothing.report_id,
    ) == [("exact", 4)]


def test_unreadable_rows_cannot_be_selected() -> None:
    store = in_memory()
    review = review_of(store, UNREADABLE_DRAFT)
    cell_diagram = review.rows[1]

    assert cell_diagram.status is ItemStatus.UNREADABLE
    assert cell_diagram.key not in review.ready
    outcome = save(store, UNREADABLE_DRAFT, review, selection=review.ready | {cell_diagram.key})
    returned(outcome, ReturnReason.SELECTION)
    assert counted(store)["grade_acceptances"] == 0
    rest = saved(save(store, UNREADABLE_DRAFT, review))
    assert (rest.added, rest.left) == (8, 1)


def test_a_key_the_review_never_offered_returns_the_review() -> None:
    store = in_memory()
    review = review_of(store)

    outcome = save(store, review=review, selection={'["row", "made up"]'})

    returned(outcome, ReturnReason.SELECTION)
    assert counted(store)["grade_acceptances"] == 0


def test_her_own_role_saves_nothing() -> None:
    store = in_memory()
    review = review_of(store)

    with pytest.raises(ValueError, match="parent"):
        store.save_grade_report(
            WREN_DRAFT,
            capture_key(WREN_DRAFT),
            key=KEY,
            page=ReviewPage(review.acceptance_id, review.revision, review.source_key),
            answers=answers_to(review),
            selection=review.ready,
            role="student",  # type: ignore[arg-type]
            complete=True,
        )
    assert counted(store) == {
        table: 1 if table == "grade_student" else 0 for table in GRADEBOOK_TABLES
    }


def test_a_row_record_names_a_result_unless_it_is_a_remembered_different_answer() -> None:
    """A row record names its result and keeps no rejected candidates, except a remembered "A
    different assignment", which names no result and keeps the candidates it turned down."""
    store = in_memory()
    saved(save(store))
    (report_id, student_id) = store._connection.execute(
        "SELECT report_id, student_id FROM grade_reports"
    ).fetchone()

    def record(row: str, result: str | None, how: str, rejected: str | None) -> None:
        store._connection.execute(
            "INSERT INTO grade_match_decisions (report_id, row_key, student_id, evidence, "
            "occurrence, result_id, how, rejected, decided_by, decided_at) "
            "VALUES (?, ?, ?, '[]', 1, ?, ?, ?, 'parent', 'now')",
            (report_id, row, student_id, result, how, rejected),
        )

    record("kept", None, "different", '[["result-a", "[]"]]')
    for row, result, how, rejected in (
        ("no result", None, "answer", None),
        ("a result", "result-a", "different", '[["result-a", "[]"]]'),
        ("nothing turned down", None, "different", None),
        ("turned down too", "result-a", "chosen", '[["result-b", "[]"]]'),
        ("unknown how", "result-a", "guessed", None),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            record(row, result, how, rejected)
    for how in ("reused", "chosen"):
        record(how, "result-a", how, None)


def deny_the_acceptance(action: int, table: str | None, *_: object) -> int:
    """Refuse the acceptance record, the last write of a save, as a failing file refuses it."""
    refused = action == sqlite3.SQLITE_INSERT and table == "grade_acceptances"
    return sqlite3.SQLITE_DENY if refused else sqlite3.SQLITE_OK


def test_a_save_whose_acceptance_record_is_refused_saves_nothing() -> None:
    store = in_memory()
    review = review_of(store)
    store._connection.set_authorizer(deny_the_acceptance)

    with pytest.raises(GradeReportNotSaved):
        save(store, review=review)

    store._connection.set_authorizer(None)
    assert counted(store) == {
        table: 1 if table == "grade_student" else 0 for table in GRADEBOOK_TABLES
    }
    assert saved(save(store, review=review)).added == 9


# ------------------------------------------------------------- each question checked alone


NO_TERM_DRAFT = draft_of(
    REPORT.replace("| **Term Grade** | **81.9** | **B-** |   |   |   |   |   |   |   |\n", "")
)
"""The report with its Term Grade row left out of the copy."""


def test_each_question_is_checked_alone_and_answers_asked_is_all_of_them() -> None:
    store = in_memory()
    review = review_of(store)
    answers = answers_to(review)
    wrong = {
        identity_asked: dataclasses.replace(answers, identity_form="another-form"),
        setup_asked: dataclasses.replace(answers, setup=None),
        first_month_asked: dataclasses.replace(answers, first_month=("2026-2027", 13)),
        class_asked: dataclasses.replace(answers, new_class="   "),
    }

    assert answers_asked(review, answers)
    for asked, answer in wrong.items():
        assert not asked(review, answer), asked.__name__
        others = [other for other in wrong if other is not asked]
        assert all(other(review, answer) for other in others), asked.__name__
        assert not answers_asked(review, answer), asked.__name__


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"new_class": "B" * CLASS_NAME_LIMIT}, ()),
        ({"new_class": f"  {'B' * CLASS_NAME_LIMIT}  "}, ()),
        ({"new_class": "B" * (CLASS_NAME_LIMIT + 1)}, ("class_name",)),
        ({"setup": ("2026-2027", "T" * TERM_LIMIT)}, ()),
        ({"setup": ("2026-2027", "T" * (TERM_LIMIT + 1))}, ("term",)),
        (
            {"new_class": "B" * (CLASS_NAME_LIMIT + 1), "setup": ("2026-2027", "T" * 21)},
            ("class_name", "term"),
        ),
    ],
)
def test_a_label_over_its_limit_is_named_and_is_not_an_answer(
    change: dict[str, object], named: tuple[str, ...]
) -> None:
    store = in_memory()
    review = review_of(store)
    answers = dataclasses.replace(answers_to(review), **change)  # type: ignore[arg-type]

    assert labels_too_long(answers) == named
    assert answers_asked(review, answers) is (named == ())


def test_a_label_over_its_limit_saves_nothing_however_it_was_offered() -> None:
    store = in_memory()
    review = review_of(store)
    answers = dataclasses.replace(answers_to(review), new_class="B" * (CLASS_NAME_LIMIT + 1))
    before = gradebook_of(store)

    returned(save(store, review=review, answers=answers), ReturnReason.ANSWERS)
    assert gradebook_of(store) == before


def test_the_report_s_own_labels_are_inside_the_limits() -> None:
    store = in_memory()
    assert labels_too_long(answers_to(review_of(store))) == ()


@pytest.mark.parametrize("draft", [WREN_DRAFT, NO_TERM_DRAFT, UNREADABLE_DRAFT])
def test_each_position_names_the_key_the_review_gives_its_item(draft: GradeReportDraft) -> None:
    store = in_memory()
    review = review_of(store, draft)

    assert item_keys(draft) == tuple(item.key for item in review.items)


def test_two_readings_of_one_text_give_the_same_positions() -> None:
    crlf = draft_of(REPORT.replace("\n", "\r\n"))
    assert item_keys(crlf) == item_keys(WREN_DRAFT)


def test_a_draft_with_no_term_result_starts_at_its_first_category() -> None:
    store = in_memory()
    review = review_of(store, NO_TERM_DRAFT)

    assert review.term is None
    assert item_keys(NO_TERM_DRAFT)[0] == review.categories[0].key
    assert TERM_KEY not in item_keys(NO_TERM_DRAFT)
    assert item_keys(WREN_DRAFT)[0] == TERM_KEY


def everything_kept(review: GradeReview, answers: GradeAnswers) -> StillAsked:
    return StillAsked(
        identity=answers.identity,
        setup=answers.setup,
        first_month=answers.first_month,
        new_class=answers.new_class,
        same_class=answers.same_class,
        matches=answers.matches,
        use=answers.use,
        selection=review.ready,
    )


def test_still_asked_keeps_every_part_that_still_answers() -> None:
    store = in_memory()
    review = review_of(store)
    answers = answers_to(review)

    kept = still_asked(review, answers, review.ready, settled_by(store))

    assert kept == everything_kept(review, answers)


def test_still_asked_drops_the_identity_alone_when_the_line_s_form_changed() -> None:
    store = in_memory()
    review = review_of(store)
    answers = answers_to(review)
    again = review_of(store, key=NEW_KEY)

    kept = still_asked(again, answers, review.ready, settled_by(store, key=NEW_KEY))

    assert kept == dataclasses.replace(everything_kept(again, answers), identity=None)


def test_still_asked_drops_the_setup_and_month_alone_once_another_class_set_them() -> None:
    store = in_memory()
    review = review_of(store)
    answers = answers_to(review)
    saved(save(store, CHEMISTRY_DRAFT))
    again = review_of(store)
    shown = dataclasses.replace(answers, identity=IdentityAnswer.SHOWN)

    kept = still_asked(again, shown, review.ready, settled_by(store))

    assert (again.setup, again.first_month) == (None, None)
    assert kept == dataclasses.replace(everything_kept(again, shown), setup=None, first_month=None)


def test_still_asked_drops_the_class_alone_once_an_alias_matches_it() -> None:
    store = in_memory()
    review = review_of(store)
    answers = answers_to(review)
    saved(save(store, EIGHT_DRAFT))
    again = review_of(store)
    shown = dataclasses.replace(
        answers, identity=IdentityAnswer.SHOWN, setup=None, first_month=None
    )

    kept = still_asked(again, shown, review.ready, settled_by(store))

    assert again.class_question.matched is not None
    assert kept.new_class is None
    assert kept.identity is IdentityAnswer.SHOWN
    assert kept.selection == review.ready & again.ready


def test_still_asked_drops_a_use_no_choice_offers_and_a_tick_not_ready() -> None:
    store = in_memory()
    first = review_of(store)
    saved(save(store, review=first, selection=[first.rows[0].key]))
    review = review_of(store)
    answers = dataclasses.replace(answers_to(review), use="earlier")

    kept = still_asked(review, answers, {*first.ready, "not-a-key"}, settled_by(store))

    assert review.use is None
    assert kept.use is None
    assert kept.selection == review.ready


def test_still_asked_keeps_match_answers_alone_and_drops_a_second_for_the_same_row() -> None:
    store = in_memory()
    saved(save(store))
    renamed = draft_of(REPORT.replace(SEVEN, SEVEN.replace("Cell Diagram", "Cell Drawing")))
    review = review_of(store, renamed)
    (row,) = [item for item in review.rows if item.question is not None]
    assert row.question is not None
    different = MatchAnswer(row.key, row.question.ids, None)
    elsewhere = MatchAnswer("not-a-row", (), None)
    answers = dataclasses.replace(answers_to(review), matches=(elsewhere, different, different))

    kept = still_asked(review, answers, (), settled_by(store, renamed))

    assert kept.matches == (different,)


def test_still_asked_keeps_a_tick_its_kept_answer_makes_ready() -> None:
    """A tick on a row that asks is judged with the row's kept answer applied, as a save
    judges it: kept with the answer, dropped without one."""
    store = in_memory()
    saved(save(store))
    renamed = draft_of(REPORT.replace(SEVEN, SEVEN.replace("Cell Diagram", "Cell Drawing")))
    review = review_of(store, renamed)
    (row,) = [item for item in review.rows if item.question is not None]
    assert row.question is not None
    same = MatchAnswer(row.key, row.question.ids, row.question.ids[0])
    answers = dataclasses.replace(answers_to(review), matches=(same,))
    unanswered = dataclasses.replace(answers, matches=())
    settle = settled_by(store, renamed)

    assert row.key not in review.ready
    assert still_asked(review, answers, {row.key}, settle).selection == {row.key}
    assert still_asked(review, unanswered, {row.key}, settle).selection == frozenset()


# ------------------------------------------------------------- the check and the recorded save


def check(
    store: ProjectStateStore,
    review: GradeReview,
    draft: GradeReportDraft = WREN_DRAFT,
    *,
    answers: GradeAnswers | None = None,
    selection: Collection[str] | None = None,
) -> SaveOutcome | GradeReview:
    """A check of the page ``review`` made, with ``answers`` or an answer to every question."""
    return store.check_grade_answers(
        draft,
        capture_key(draft),
        key=KEY,
        complete=False,
        page=ReviewPage(review.acceptance_id, review.revision, review.source_key),
        answers=answers or answers_to(review),
        selection=frozenset(review.ready if selection is None else selection),
    )


def test_a_check_of_a_current_page_keeps_its_id_and_revision_and_writes_nothing() -> None:
    store = in_memory()
    saved(save(store))
    renamed = draft_of(REPORT.replace(SEVEN, SEVEN.replace("Cell Diagram", "Cell Drawing")))
    review = review_of(store, renamed)
    (row,) = [item for item in review.rows if item.question is not None]
    assert row.question is not None
    answers = dataclasses.replace(
        answers_to(review), matches=(MatchAnswer(row.key, row.question.ids, None),)
    )
    changes = store._connection.total_changes

    checked = check(store, review, renamed, answers=answers)

    assert isinstance(checked, GradeReview)
    assert (checked.acceptance_id, checked.revision) == (review.acceptance_id, review.revision)
    assert row.key in checked.ready
    assert row.key not in review.ready
    assert store._connection.total_changes == changes


def test_a_check_after_another_tab_s_save_returns_the_revision_and_the_next_save_too() -> None:
    store = in_memory()
    saved(save(store, selection={TERM_KEY}))
    one_tab, another_tab = review_of(store), review_of(store)
    saved(save(store, review=one_tab))
    before = gradebook_of(store)
    changes = store._connection.total_changes

    checked = check(store, another_tab)

    assert returned(checked, ReturnReason.REVISION).revision == 2
    assert store._connection.total_changes == changes
    assert gradebook_of(store) == before
    returned(save(store, review=another_tab), ReturnReason.REVISION)
    assert gradebook_of(store) == before


def test_a_check_answers_in_the_save_s_order_without_writing() -> None:
    store = in_memory()
    review = review_of(store)
    first = saved(save(store, review=review, selection={TERM_KEY}))
    fresh = review_of(store)
    changes = store._connection.total_changes

    recorded = check(store, review, selection={TERM_KEY, review.rows[0].key})
    other_text = check(store, fresh, EIGHT_DRAFT)
    not_hers = check(store, fresh, answers=answers_to(fresh, IdentityAnswer.NOT_HERS))
    stale = check(store, fresh, answers=answers_to(review))

    assert recorded == AlreadyRecorded(first, frozenset({review.rows[0].key}))
    returned(other_text, ReturnReason.SOURCE)
    assert not_hers == NotHers()
    returned(stale, ReturnReason.ANSWERS)
    assert store._connection.total_changes == changes


def test_a_check_refuses_the_ticks_a_save_refuses_and_names_those_on_rows_still_asking() -> None:
    """A tick on a value the save wouldn't take returns the review on Check as on Save, writing
    nothing; the refused ticks on rows whose question is open are named apart."""
    store = in_memory()
    saved(save(store))
    renamed = draft_of(REPORT.replace(SEVEN, SEVEN.replace("Cell Diagram", "Cell Drawing")))
    review = review_of(store, renamed)
    (row,) = [item for item in review.rows if item.question is not None]
    changes = store._connection.total_changes

    asking = frozenset({row.key})
    for chosen in (asking, frozenset({TERM_KEY, row.key})):
        for outcome in (
            check(store, review, renamed, selection=chosen),
            save(store, renamed, review, selection=chosen),
        ):
            assert isinstance(outcome, ReviewReturned), outcome
            assert outcome.why is ReturnReason.SELECTION
            assert (outcome.refused, outcome.still_asking) == (chosen, asking)
    assert store._connection.total_changes == changes


def test_the_recorded_save_names_its_class_term_and_answer_even_for_a_no_op() -> None:
    store = in_memory()
    review = review_of(store)
    nothing = saved(save(store, review=review, selection=()))

    recorded = store.recorded_save(review.acceptance_id)

    assert recorded is not None
    assert recorded.saved == nothing
    assert (recorded.class_name, recorded.year, recorded.term) == ("Biology", "2026-2027", "T1")
    assert (recorded.identity_status, recorded.identity_answer) == (
        IdentityStatus.FIRST_USE,
        IdentityAnswer.HERS,
    )
    assert recorded.context == ("2026-2027", "T1")


def test_the_recorded_save_is_none_for_an_unknown_another_student_s_or_a_deleted_id() -> None:
    store = in_memory()
    other = in_memory()
    other_review = review_of(other)
    saved(save(other, review=other_review))
    for row in one(other, "SELECT * FROM grade_acceptances"):
        marks = ", ".join("?" * len(row))
        store._connection.execute(f"INSERT INTO grade_acceptances VALUES ({marks})", row)  # noqa: S608
    store._connection.commit()
    review = review_of(store)
    saved(save(store, review=review))
    recorded = store.recorded_save(review.acceptance_id)
    assert recorded is not None
    changes = store._connection.total_changes

    assert store.recorded_save("acceptance-" + "0" * 32) is None
    assert store.recorded_save(other_review.acceptance_id) is None
    assert store._connection.total_changes == changes
    deleted = store.delete_class_term(
        recorded.class_id, "T1", revision=review_of(store).revision, role="parent"
    )
    assert deleted.__class__.__name__ == "ClassTermDeleted"
    assert store.recorded_save(review.acceptance_id) is None
