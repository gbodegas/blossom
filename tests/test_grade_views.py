# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""What Grades and class details read, each viewer's remembered term, and a parent's choice of
the current term.

A remembered term is a viewer's own preference, kept apart from the grade records: choosing one
writes that viewer's row alone and no grade table, and the store accepts only a term that is the
current one or on record. The current term changes only by a parent's explicit choice, compared
and set alone, and leaves every viewer's remembered term as it was.
"""

import dataclasses
import pathlib
from typing import cast

import pytest

from blossom.grades.draft import GradeReportDraft, capture_key
from blossom.grades.identity import name_form_key
from blossom.grades.projection import MadeCurrent
from blossom.grades.review import GradeReportSaved
from blossom.grades.text_reader import read_grade_report
from blossom.stores import gradebook
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
    answers_in,
    as_stored,
    capture_class,
    closed_world,
    confirm_current,
    fixture_clock,
    grade_answers,
    homework_named,
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


# ------------------------------------------------------------- homework class names

GEOMETRY = draft_of(
    REPORT.replace("**07 BIO - C**", "**08 GEO - A**").replace("**Biology**", "**08 GEOMETRY**")
)
"""A second class of the year: 08 GEOMETRY, under the report code 08 GEO - A."""
OTHER_SECTION = draft_of(REPORT.replace("**07 BIO - C**", "**07 BIO - D**"))
"""Another section's report: a second class with the official name Biology."""
CODE_BOTANY = draft_of(
    REPORT.replace("**07 BIO - C**", "**Botany**").replace("**Biology**", "**Zoology**")
)
NAME_BOTANY = draft_of(
    REPORT.replace("**07 BIO - C**", "**07 BOT - A**").replace("**Biology**", "**Botany**")
)
MAPPING = "grade_homework_classes"
NO_CLASS = "class-" + "0" * 32


def with_homework(store: ProjectStateStore, *courses: str) -> None:
    """Homework on record under each of ``courses``, as written."""
    store.put_on_record(
        [homework_named(course, f"assignment-named-{n}") for n, course in enumerate(courses)], {}
    )


def saved_as(store: ProjectStateStore, draft: GradeReportDraft, name: str) -> str:
    """``draft`` saved as a new class with the official name ``name``; the class's ID."""
    review = store.review_grade_report(draft, capture_key(draft), key=KEY)
    answers = dataclasses.replace(grade_answers(review), new_class=name)
    outcome = save_grade(store, draft, key=KEY, review=review, answers=answers)
    assert isinstance(outcome, GradeReportSaved)
    return capture_class(store, draft)


def test_the_homework_class_table_is_a_gradebook_table_whose_class_may_be_none(
    tmp_path: pathlib.Path,
) -> None:
    store = opened(tmp_path)
    columns = {
        str(row[1]): (str(row[2]), int(row[3]))
        for row in store._connection.execute(f"PRAGMA table_info({MAPPING})")
    }
    store.close()

    assert MAPPING in GRADEBOOK_TABLES
    assert columns == {
        **dict.fromkeys(
            ("student_id", "year_label", "name_key", "answered_by", "answered_at"), ("TEXT", 1)
        ),
        "class_id": ("TEXT", 0),
    }


@pytest.mark.parametrize(
    ("label", "which"),
    [
        ("life  SCIENCE", "biology"),
        (" 07 bio - c", "biology"),
        ("biology", "biology"),
        ("08 Geometry", "geometry"),
        ("08 geo  - a", "geometry"),
    ],
)
def test_a_name_joins_the_one_class_it_equals_but_for_capitalization_and_spaces(
    label: str, which: str, tmp_path: pathlib.Path
) -> None:
    """The official name, the report's code and the report's name each connect a name."""
    store = opened(tmp_path)
    classes = {"biology": saved_as(store, WREN, "Life Science")}
    saved(store, GEOMETRY)
    classes["geometry"] = capture_class(store, GEOMETRY)
    mapping = store.homework_classes(YEAR)
    store.close()

    assert gradebook.class_for(label, mapping) == classes[which]


def test_a_name_that_equals_no_class_or_more_than_one_waits(tmp_path: pathlib.Path) -> None:
    store = opened(tmp_path)
    saved(store, WREN, OTHER_SECTION, CODE_BOTANY, NAME_BOTANY)
    second = capture_class(store, OTHER_SECTION)
    mapping = store.homework_classes(YEAR)
    store.close()

    assert gradebook.class_for("Geometry", mapping) is None
    assert gradebook.class_for("Biology", mapping) is None
    assert gradebook.class_for("botany", mapping) is None
    assert gradebook.class_for("07 BIO - D", mapping) == second


def test_a_parent_s_answer_comes_first_and_only_her_classes_of_the_year_are_read(
    tmp_path: pathlib.Path,
) -> None:
    store = opened(tmp_path)
    saved(store, WREN, GEOMETRY, EARLIER_YEAR)
    with_homework(store, "Biology", "Art")
    biology, geometry, earlier = (
        capture_class(store, draft) for draft in (WREN, GEOMETRY, EARLIER_YEAR)
    )
    store._connection.execute(
        "INSERT INTO grade_classes VALUES (?, 'student-other', ?, 'Art', 'parent', ?)",
        (NO_CLASS, YEAR, "2026-09-01T00:00:00+00:00"),
    )
    store._connection.execute(
        "INSERT INTO grade_homework_classes VALUES ('student-other', ?, 'art', ?, 'parent', ?)",
        (YEAR, NO_CLASS, "2026-09-01T00:00:00+00:00"),
    )
    store._connection.commit()
    connected = store.connect_homework_class(
        YEAR, "Biology", shown=None, chosen=geometry, role="parent"
    )
    mapping = store.homework_classes(YEAR)
    last_year = store.homework_classes("2025-2026")
    unknown_year = store.homework_classes("2024-2025")
    store.close()

    assert connected == gradebook.HomeworkClassConnected(geometry)
    assert gradebook.class_for("biology", mapping) == geometry
    assert gradebook.class_for("Art", mapping) is None
    assert {one.class_id for one in mapping.classes} == {biology, geometry}
    assert dict(mapping.answers) == {"biology": geometry}
    assert gradebook.class_for("Biology", last_year) == earlier
    assert (unknown_year.classes, dict(unknown_year.answers)) == ((), {})


def test_the_mapping_read_gives_each_class_its_aliases_earliest_first(
    tmp_path: pathlib.Path,
) -> None:
    """Two aliases added in one moment come in the order the table holds them."""
    store = opened(tmp_path)
    saved(store, WREN)
    second = draft_of(
        REPORT.replace("**07 BIO - C**", "**07 BIO - D**").replace("**T1**", "**T2**")
    )
    review = store.review_grade_report(second, capture_key(second), key=KEY)
    (existing,) = review.class_question.existing
    answers = dataclasses.replace(
        grade_answers(review),
        new_class=None,
        same_class=existing[0],
        same_class_revision=existing[2],
    )
    assert isinstance(
        save_grade(store, second, key=KEY, review=review, answers=answers), GradeReportSaved
    )
    (only,) = store.homework_classes(YEAR).classes
    store.close()

    assert only.name == "Biology"
    assert [(alias.code, alias.name) for alias in only.aliases] == [
        ("07 BIO - C", "Biology"),
        ("07 BIO - D", "Biology"),
    ]
    assert only.aliases[0].added_at == only.aliases[1].added_at


def test_connecting_a_name_compares_and_sets_and_a_page_sent_again_stands(
    tmp_path: pathlib.Path,
) -> None:
    store = opened(tmp_path)
    saved(store, WREN, GEOMETRY)
    with_homework(store, "Geometry", "Art")
    biology, geometry = capture_class(store, WREN), capture_class(store, GEOMETRY)

    def connect(name: str, shown: str | None, chosen: str, role: str = "parent") -> object:
        return store.connect_homework_class(
            YEAR, name, shown=shown, chosen=chosen, role=cast(gradebook.ConfirmedBy, role)
        )

    first = connect(" geometry", None, geometry, "household")
    recorded = as_stored(store, MAPPING)
    again = connect("Geometry", None, geometry)
    answered_meanwhile = connect("Geometry", None, biology)
    never_answered = connect("Art", biology, geometry)
    another_class = connect("Geometry", NO_CLASS, biology)
    unchanged = as_stored(store, MAPPING)
    changed = connect("GEOMETRY", geometry, biology)
    after = as_stored(store, MAPPING)
    with pytest.raises(ValueError, match="only a parent"):
        connect("Geometry", biology, geometry, "student")
    store.close()

    assert first == gradebook.HomeworkClassConnected(geometry)
    assert answers_in(recorded) == [(YEAR, "geometry", geometry, "household")]
    assert again == gradebook.HomeworkClassStood(geometry)
    assert answered_meanwhile == gradebook.HomeworkClassChanged(geometry)
    assert never_answered == gradebook.HomeworkClassChanged(None)
    assert another_class == gradebook.HomeworkClassChanged(geometry)
    assert unchanged == recorded
    assert changed == gradebook.HomeworkClassConnected(biology)
    assert answers_in(after) == [(YEAR, "geometry", biology, "parent")]


@pytest.mark.parametrize("unknown", ["class", "year", "another year's class", "name"])
def test_connecting_refuses_a_class_year_or_name_that_isn_t_on_record(
    unknown: str, tmp_path: pathlib.Path
) -> None:
    store = opened(tmp_path)
    saved(store, WREN, EARLIER_YEAR)
    with_homework(store, "Biology")
    biology, earlier = capture_class(store, WREN), capture_class(store, EARLIER_YEAR)
    year, name, chosen = {
        "class": (YEAR, "Biology", NO_CLASS),
        "year": ("2024-2025", "Biology", biology),
        "another year's class": (YEAR, "Biology", earlier),
        "name": (YEAR, "Geometry", biology),
    }[unknown]
    before = grade_tables(store)

    with pytest.raises(gradebook.HomeworkClassNotOnRecord):
        store.connect_homework_class(year, name, shown=None, chosen=chosen, role="parent")
    after = grade_tables(store)
    store.close()

    assert after == before
    assert after[MAPPING] == []


@pytest.mark.parametrize(
    "kind",
    [
        "a new connection",
        "another class",
        "a parent's connection removed",
        "a name matched by name removed",
        "the same answer sent again",
    ],
)
def test_an_answer_for_a_year_on_record_that_isn_t_current_writes_nothing_whatever_its_kind(
    kind: str, tmp_path: pathlib.Path
) -> None:
    """The write compares the year an answer is for with the current school year inside its
    own transaction, before anything else about the answer."""
    store = opened(tmp_path)
    saved(store, WREN, GEOMETRY, EARLIER_YEAR)
    with_homework(store, "Geometry", "08 Geometry", "Art")
    biology, geometry = capture_class(store, WREN), capture_class(store, GEOMETRY)
    connected = store.connect_homework_class(
        YEAR, "Geometry", shown=None, chosen=biology, role="parent"
    )
    name, shown, chosen = {
        "a new connection": ("Art", None, biology),
        "another class": ("Geometry", biology, geometry),
        "a parent's connection removed": ("Geometry", biology, gradebook.UNCONNECTED),
        "a name matched by name removed": ("08 Geometry", None, gradebook.UNCONNECTED),
        "the same answer sent again": ("Geometry", None, biology),
    }[kind]
    turned = store.set_current_context((YEAR, "T1"), ("2025-2026", "T3"), "parent")
    before = grade_tables(store)
    outcome = store.connect_homework_class(YEAR, name, shown=shown, chosen=chosen, role="parent")
    after = grade_tables(store)
    back = store.set_current_context(("2025-2026", "T3"), (YEAR, "T1"), "parent")
    while_current = store.connect_homework_class(
        YEAR, name, shown=shown, chosen=chosen, role="parent"
    )
    store.close()

    assert connected == gradebook.HomeworkClassConnected(biology)
    assert turned == ContextSet(("2025-2026", "T3"))
    assert after == before
    assert len(after[MAPPING]) == 1
    assert outcome == gradebook.HomeworkYearNotCurrent("2025-2026")
    assert back == ContextSet((YEAR, "T1"))
    assert (
        type(while_current)
        is {
            "a new connection": gradebook.HomeworkClassConnected,
            "another class": gradebook.HomeworkClassConnected,
            "a parent's connection removed": gradebook.HomeworkClassRemoved,
            "a name matched by name removed": gradebook.HomeworkClassRemoved,
            "the same answer sent again": gradebook.HomeworkClassStood,
        }[kind]
    )


def test_an_answer_with_no_current_year_is_refused_as_its_year_is_or_isn_t_on_record(
    tmp_path: pathlib.Path,
) -> None:
    """With no current year and term on record, an answer for a year with a term on record is
    one for a year that isn't current, and any other year isn't on record."""
    store = opened(tmp_path)
    saved(store, WREN)
    with_homework(store, "Biology")
    biology = capture_class(store, WREN)
    store._connection.execute("DELETE FROM grade_context")
    store._connection.commit()
    before = grade_tables(store)
    outcome = store.connect_homework_class(
        YEAR, "Biology", shown=None, chosen=biology, role="parent"
    )
    with pytest.raises(gradebook.HomeworkClassNotOnRecord):
        store.connect_homework_class(
            "2024-2025", "Biology", shown=None, chosen=biology, role="parent"
        )
    after = grade_tables(store)
    store.close()

    assert outcome == gradebook.HomeworkYearNotCurrent(None)
    assert after == before
    assert after[MAPPING] == []


def test_g3a_i1_connecting_a_name_changes_only_the_mapping_table(tmp_path: pathlib.Path) -> None:
    """Every other table, the stored class names and assignment IDs included, reads byte for
    byte the same after a name is connected, sent again, changed and sent from a stale page."""
    path = tmp_path / "blossom.sqlite3"
    store = opened(tmp_path)
    saved(store, WREN, GEOMETRY)
    with_homework(store, "Geometry", "geometry ", "08 Geometry")
    biology, geometry = capture_class(store, WREN), capture_class(store, GEOMETRY)
    before = closed_world([path], leaving_out=(MAPPING,))
    outcomes = [
        store.connect_homework_class(YEAR, "Geometry", shown=shown, chosen=chosen, role="parent")
        for shown, chosen in (
            (None, geometry),
            (None, geometry),
            (geometry, biology),
            (geometry, geometry),
        )
    ]
    after = closed_world([path], leaving_out=(MAPPING,))
    rows = as_stored(store, MAPPING)
    store.close()

    assert [type(outcome) for outcome in outcomes] == [
        gradebook.HomeworkClassConnected,
        gradebook.HomeworkClassStood,
        gradebook.HomeworkClassConnected,
        gradebook.HomeworkClassChanged,
    ]
    assert any(name.endswith("rows of assignments") for name in before)
    assert after == before
    assert len(rows) == 1


def test_removing_a_connection_returns_the_name_to_waiting_until_it_is_connected_again(
    tmp_path: pathlib.Path,
) -> None:
    """The answer on record is one of three, none, unconnected or a class, and each press is
    compared with it: a removal sent again stands, and a page from before it is refused."""
    store = opened(tmp_path)
    saved(store, WREN, GEOMETRY)
    with_homework(store, "Geometry")
    biology, geometry = capture_class(store, WREN), capture_class(store, GEOMETRY)

    def answer(shown: str | None, chosen: str) -> object:
        return store.connect_homework_class(
            YEAR, "Geometry", shown=shown, chosen=chosen, role="parent"
        )

    connected = answer(None, geometry)
    removed = answer(geometry, gradebook.UNCONNECTED)
    rows = as_stored(store, MAPPING)
    waiting = store.homework_classes(YEAR)
    again = answer(geometry, gradebook.UNCONNECTED)
    from_before = answer(geometry, biology)
    never_shown = answer(None, biology)
    unchanged = as_stored(store, MAPPING)
    back = answer(gradebook.UNCONNECTED, biology)
    after = store.homework_classes(YEAR)
    store.close()

    assert connected == gradebook.HomeworkClassConnected(geometry)
    assert removed == gradebook.HomeworkClassRemoved()
    assert [row[3] for row in rows] == [None]
    assert dict(waiting.answers) == {"geometry": None}
    assert gradebook.class_for("Geometry", waiting) is None
    assert again == gradebook.HomeworkClassStood(gradebook.UNCONNECTED)
    assert from_before == gradebook.HomeworkClassChanged(gradebook.UNCONNECTED)
    assert never_shown == gradebook.HomeworkClassChanged(gradebook.UNCONNECTED)
    assert unchanged == rows
    assert back == gradebook.HomeworkClassConnected(biology)
    assert gradebook.class_for("Geometry", after) == biology


def test_a_name_matched_by_name_stays_waiting_once_removed_though_it_equals_the_class(
    tmp_path: pathlib.Path,
) -> None:
    """The automatic rule never applies to a name that has a parent's answer."""
    store = opened(tmp_path)
    saved(store, WREN, GEOMETRY)
    with_homework(store, "08 Geometry")
    geometry = capture_class(store, GEOMETRY)
    matched = gradebook.class_for("08 Geometry", store.homework_classes(YEAR))
    removed = store.connect_homework_class(
        YEAR, "08 Geometry", shown=None, chosen=gradebook.UNCONNECTED, role="household"
    )
    rows = answers_in([(*row[:3], b"", *row[4:]) for row in as_stored(store, MAPPING)])
    waiting = store.homework_classes(YEAR)
    back = store.connect_homework_class(
        YEAR, "08 geometry", shown=gradebook.UNCONNECTED, chosen=geometry, role="parent"
    )
    after = store.homework_classes(YEAR)
    store.close()

    assert matched == geometry
    assert removed == gradebook.HomeworkClassRemoved()
    assert rows == [(YEAR, "08 geometry", "", "household")]
    assert gradebook.class_for("08 GEOMETRY", waiting) is None
    assert back == gradebook.HomeworkClassConnected(geometry)
    assert gradebook.class_for("08 Geometry", after) == geometry


@pytest.mark.parametrize("nothing", ["a name that waits", "a year not on record"])
def test_removing_refuses_where_there_is_no_connection_to_remove(
    nothing: str, tmp_path: pathlib.Path
) -> None:
    store = opened(tmp_path)
    saved(store, WREN)
    with_homework(store, "Geometry", "Biology")
    year, name = {
        "a name that waits": (YEAR, "Geometry"),
        "a year not on record": ("2024-2025", "Biology"),
    }[nothing]

    with pytest.raises(gradebook.HomeworkClassNotOnRecord):
        store.connect_homework_class(
            year, name, shown=None, chosen=gradebook.UNCONNECTED, role="parent"
        )
    rows = as_stored(store, MAPPING)
    store.close()

    assert rows == []


def test_g3a_i1_removing_a_connection_changes_only_the_mapping_table(
    tmp_path: pathlib.Path,
) -> None:
    """Homework, every other grade table and every history row read byte for byte the same
    after a connection is removed, the removal is sent again, and the name is connected again."""
    path = tmp_path / "blossom.sqlite3"
    store = opened(tmp_path)
    saved(store, WREN, GEOMETRY)
    with_homework(store, "Geometry", "08 Geometry")
    geometry = capture_class(store, GEOMETRY)
    before = closed_world([path], leaving_out=(MAPPING,))
    outcomes = [
        store.connect_homework_class(YEAR, name, shown=shown, chosen=chosen, role="parent")
        for name, shown, chosen in (
            ("Geometry", None, geometry),
            ("Geometry", geometry, gradebook.UNCONNECTED),
            ("Geometry", geometry, gradebook.UNCONNECTED),
            ("08 Geometry", None, gradebook.UNCONNECTED),
            ("Geometry", gradebook.UNCONNECTED, geometry),
        )
    ]
    after = closed_world([path], leaving_out=(MAPPING,))
    store.close()

    assert [type(outcome) for outcome in outcomes] == [
        gradebook.HomeworkClassConnected,
        gradebook.HomeworkClassRemoved,
        gradebook.HomeworkClassStood,
        gradebook.HomeworkClassRemoved,
        gradebook.HomeworkClassConnected,
    ]
    assert any(name.endswith("rows of assignments") for name in before)
    assert after == before
