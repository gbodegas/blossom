# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""What Grades and class details read, each viewer's remembered term, and a parent's choice of
the current term.

A remembered term is a viewer's own preference, kept apart from the grade records: choosing one
writes that viewer's row alone and no grade table, and the store accepts only a term that is the
current one or on record. The current term changes only by a parent's explicit choice, compared
and set alone, and leaves every viewer's remembered term as it was.
"""

import pathlib

import pytest

from blossom.grades.draft import GradeReportDraft
from blossom.grades.identity import name_form_key
from blossom.grades.projection import MadeCurrent
from blossom.grades.review import GradeReportSaved
from blossom.grades.text_reader import read_grade_report
from blossom.stores.gradebook import (
    GRADEBOOK_TABLES,
    VIEW_TABLES,
    ClassInTerm,
    ClassOfYear,
    ContextChanged,
    ContextNotOnRecord,
    ContextSet,
    ContextStood,
    GradeContexts,
    ReportScope,
    TermNotOnRecord,
    Viewer,
    ViewNotOnRecord,
)
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    FIXTURES,
    as_stored,
    capture_class,
    confirm_current,
    fixture_clock,
    save_grade,
)

REPORT = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")
KEY = name_form_key(b"5" * 64)
YEAR = "2026-2027"
SEVEN = "| Cell Diagram             | 7.0 "
VIEWERS: tuple[Viewer, ...] = ("student", "parent", "anyone")


def draft_of(text: str) -> GradeReportDraft:
    reading = read_grade_report(text)
    assert reading.draft is not None, reading.not_read
    return reading.draft


WREN = draft_of(REPORT)
EIGHT = draft_of(REPORT.replace(SEVEN, SEVEN.replace("7.0", "8.0")))
"""A newer capture of the first term: Cell Diagram changed."""
SECOND_TERM = draft_of(REPORT.replace("**T1**", "**T2**"))
EARLIER_YEAR = draft_of(
    REPORT.replace("**2026-2027**", "**2025-2026**").replace("**T1**", "**T3**")
)


def opened(tmp_path: pathlib.Path) -> ProjectStateStore:
    return ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())


def saved(store: ProjectStateStore, *drafts: GradeReportDraft) -> None:
    for draft in drafts:
        assert isinstance(save_grade(store, draft, key=KEY), GradeReportSaved)


def grade_tables(store: ProjectStateStore) -> dict[str, list[tuple[object, ...]]]:
    return {table: as_stored(store, table) for table in GRADEBOOK_TABLES}


def views(store: ProjectStateStore) -> list[tuple[object, ...]]:
    return as_stored(store, "grade_view_choices")


def test_the_remembered_terms_are_kept_outside_every_grade_table() -> None:
    assert VIEW_TABLES == ("grade_view_choices",)
    assert not set(VIEW_TABLES) & set(GRADEBOOK_TABLES)


def test_before_any_report_there_is_no_context_and_no_term(tmp_path: pathlib.Path) -> None:
    store = opened(tmp_path)
    contexts = store.grade_contexts()
    store.close()

    assert contexts == GradeContexts(current=None, terms=())


def test_terms_are_listed_by_year_then_when_made_then_label_on_a_pinned_clock(
    tmp_path: pathlib.Path,
) -> None:
    """On a pinned clock two terms tie on when they were made, so their labels order them,
    whichever arrived first."""
    store = opened(tmp_path)
    saved(store, SECOND_TERM, WREN, EARLIER_YEAR)
    contexts = store.grade_contexts()
    of_year = store.terms_of_year(YEAR)
    of_earlier = store.terms_of_year("2025-2026")
    of_none = store.terms_of_year("2030-2031")
    store.close()

    assert contexts.current == (YEAR, "T2")
    assert contexts.terms == (("2025-2026", "T3"), (YEAR, "T1"), (YEAR, "T2"))
    assert of_year == ("T1", "T2")
    assert of_earlier == ("T3",)
    assert of_none == ()


def test_the_year_s_classes_say_which_the_term_reports(tmp_path: pathlib.Path) -> None:
    store = opened(tmp_path)
    saved(store, WREN)
    class_id = capture_class(store, WREN)
    (name,) = store._connection.execute(
        "SELECT display_name FROM grade_classes WHERE class_id = ?", (class_id,)
    ).fetchone()
    in_first = store.classes_in(YEAR, "T1")
    in_second = store.classes_in(YEAR, " T2 ")
    in_other_year = store.classes_in("2025-2026", "T1")
    named = store.class_named(class_id)
    not_named = store.class_named("class-not-on-record")
    store.close()

    assert named == ClassOfYear(class_id, name, YEAR)
    assert not_named is None
    assert in_first == (ClassInTerm(class_id, name, reported=True),)
    assert in_second == (ClassInTerm(class_id, name, reported=False),)
    assert in_other_year == ()


def test_a_class_s_reports_follow_acceptance_order_with_the_action_that_made_one(
    tmp_path: pathlib.Path,
) -> None:
    store = opened(tmp_path)
    saved(store, WREN, EIGHT)
    made = confirm_current(store, WREN)
    class_id = capture_class(store, WREN)
    read = store.class_term(class_id, "T1")
    reports = read.reports
    (acted_at,) = store._connection.execute("SELECT acted_at FROM grade_current_actions").fetchone()
    scope = store.report_scope(reports[0].report_id)
    unknown = store.report_scope("report-not-on-record")
    none_in_second = store.class_term(class_id, "T2")
    store.close()

    assert isinstance(made, MadeCurrent)
    assert read.current.results
    suppliers = [value.report_id for value in read.current.results.values()]
    assert set(suppliers) <= {report.report_id for report in reports}
    assert [report.order for report in reports] == [1, 2, 3]
    assert [report.use for report in reports] == ["current", "current", "current"]
    assert [report.acted_at for report in reports] == [None, None, acted_at]
    assert reports[2].report_id == made.report_id
    assert [report.latest_of_capture for report in reports] == [
        made.report_id,
        reports[1].report_id,
        made.report_id,
    ]
    assert scope is not None
    assert scope == ReportScope(class_id, "T1", scope.source_key)
    assert unknown is None
    assert none_in_second.reports == ()
    assert not none_in_second.current.results


def test_a_viewer_s_choice_writes_their_row_alone_and_no_grade_table(
    tmp_path: pathlib.Path,
) -> None:
    store = opened(tmp_path)
    saved(store, WREN, SECOND_TERM)
    store.choose_view("parent", (YEAR, "T2"))
    grades = grade_tables(store)
    others = views(store)

    store.choose_view("student", (YEAR, "T2"))
    chosen = views(store)
    store.choose_view("student", (YEAR, "T1"))
    chosen_again = views(store)
    store.choose_view("student", None)
    followed = views(store)
    seen = {viewer: store.view_of(viewer) for viewer in VIEWERS}
    grades_after = grade_tables(store)
    student_id = store.student_id()
    store.close()

    parent_row = (student_id.encode(), b"parent", YEAR.encode(), b"T2")
    assert others == [parent_row]
    assert chosen == [parent_row, (student_id.encode(), b"student", YEAR.encode(), b"T2")]
    assert chosen_again == [parent_row, (student_id.encode(), b"student", YEAR.encode(), b"T1")]
    assert followed == [parent_row]
    assert seen == {"student": None, "parent": (YEAR, "T2"), "anyone": None}
    assert grades_after == grades


@pytest.mark.parametrize(
    "view",
    [(YEAR, "T9"), ("2030-2031", "T1"), (YEAR, "t1"), (YEAR, " T1")],
    ids=["another term", "another year", "another spelling", "padded"],
)
def test_a_choice_not_on_record_is_refused_and_writes_nothing(
    view: tuple[str, str], tmp_path: pathlib.Path
) -> None:
    store = opened(tmp_path)
    saved(store, WREN)
    store.choose_view("anyone", (YEAR, "T1"))
    before = (grade_tables(store), views(store))

    with pytest.raises(ViewNotOnRecord):
        store.choose_view("anyone", view)
    after = (grade_tables(store), views(store))
    store.close()

    assert after == before


def test_a_choice_needs_a_viewer_the_record_knows(tmp_path: pathlib.Path) -> None:
    store = opened(tmp_path)
    saved(store, WREN)

    with pytest.raises(ValueError, match="belongs to"):
        store.choose_view("teacher", (YEAR, "T1"))  # type: ignore[arg-type]
    after = views(store)
    store.close()

    assert after == []


def test_before_any_report_only_following_the_current_term_is_a_choice(
    tmp_path: pathlib.Path,
) -> None:
    store = opened(tmp_path)
    store.choose_view("student", None)
    with pytest.raises(ViewNotOnRecord):
        store.choose_view("student", (YEAR, "T1"))
    after = views(store)
    store.close()

    assert after == []


def test_a_parent_s_choice_of_the_current_term_compares_and_sets_the_context_alone(
    tmp_path: pathlib.Path,
) -> None:
    store = opened(tmp_path)
    saved(store, WREN, SECOND_TERM)
    store.choose_view("student", (YEAR, "T1"))
    store.choose_view("parent", (YEAR, "T2"))
    grades = grade_tables(store)
    kept = views(store)

    outcome = store.set_current_context((YEAR, "T1"), (YEAR, "T2"), "parent")
    changed = {table for table, rows in grade_tables(store).items() if rows != grades[table]}
    context = as_stored(store, "grade_context")
    kept_after = views(store)
    retried = store.set_current_context((YEAR, "T1"), (YEAR, "T2"), "household")
    stale = store.set_current_context((YEAR, "T1"), (YEAR, "T1"), "parent")
    after_refusals = grade_tables(store)
    store.close()

    assert outcome == ContextSet((YEAR, "T2"))
    assert changed == {"grade_context"}
    assert [row[1:4] for row in context] == [(YEAR.encode(), b"T2", b"parent")]
    assert kept_after == kept
    assert retried == ContextStood((YEAR, "T2"))
    assert stale == ContextChanged((YEAR, "T2"))
    assert after_refusals["grade_context"] == context


def test_a_current_term_not_on_record_is_refused_and_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    store = opened(tmp_path)
    saved(store, WREN)
    before = (grade_tables(store), views(store))

    with pytest.raises(TermNotOnRecord):
        store.set_current_context((YEAR, "T1"), (YEAR, "T2"), "parent")
    with pytest.raises(ValueError, match="only a parent"):
        store.set_current_context((YEAR, "T1"), (YEAR, "T1"), "student")  # type: ignore[arg-type]
    after = (grade_tables(store), views(store))
    store.close()

    assert after == before


def test_with_no_context_the_current_term_can_t_be_chosen(tmp_path: pathlib.Path) -> None:
    store = opened(tmp_path)
    outcome = store.set_current_context((YEAR, "T1"), (YEAR, "T1"), "parent")
    context = as_stored(store, "grade_context")
    store.close()

    assert outcome == ContextNotOnRecord()
    assert context == []


def test_showing_the_current_term_changes_only_the_requesting_viewer_s_row(
    tmp_path: pathlib.Path,
) -> None:
    """A parent's new current term leaves a viewer on the term they chose; "Show the current
    term" then follows it for that viewer alone."""
    store = opened(tmp_path)
    saved(store, WREN, SECOND_TERM)
    for viewer in VIEWERS:
        store.choose_view(viewer, (YEAR, "T1"))
    moved = store.set_current_context((YEAR, "T1"), (YEAR, "T2"), "parent")
    still = {viewer: store.view_of(viewer) for viewer in VIEWERS}
    store.choose_view("student", None)
    after = {viewer: store.view_of(viewer) for viewer in VIEWERS}
    current = store.grade_contexts().current
    store.close()

    assert moved == ContextSet((YEAR, "T2"))
    assert still == dict.fromkeys(VIEWERS, (YEAR, "T1"))
    assert after == {"student": None, "parent": (YEAR, "T1"), "anyone": (YEAR, "T1")}
    assert current == (YEAR, "T2")
