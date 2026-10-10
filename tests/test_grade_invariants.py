# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The gradebook's invariants, each with a test named for it.

G-I1: no gradebook write changes anything outside the gradebook's own tables, read as a closed
world from ``sqlite_master`` with the checkpoint and trace files beside it, so a table added later
is covered unless it is named a gradebook table. G-I7: current values are per target, and a
report kept as earlier changes none, its due dates included. G-I8: a retry writes nothing and
returns what was recorded, and a value a newer report replaced comes back only under the parent's
choice of current. G-I13: no gradebook table, and no log line, holds a student's name. G-I15:
every gradebook row carries her one student ID, and nothing is looked up under another. G-I16: a
result is its stable ID, and an ambiguous match never saves without a parent's answer. G-I17: no
grade save writes her own account or the school's submission status. G-I2: what a plan is made
from reads the same across every grade write. G-I5: nothing saves for another student or for an
identity no one confirmed. G-I12: the grade modules import nothing that reaches a model. G-I18:
no grade write changes the current context a parent chose, but a parent's explicit choice of the
current term. G-I11: a delete removes exactly its class and term and keeps the revision, raised.
G-I6: saved reports, observations and corrections never change, and each correction keeps the
value as read. A remembered term is a viewer's own, outside the gradebook's tables, so G-I1's
closed world holds it.
"""

import ast
import dataclasses
import json
import logging
import pathlib
import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import date

import pytest

from blossom.grades.draft import GradeReportDraft, GradeValue, Presence, capture_key
from blossom.grades.identity import IdentityStatus, name_form, name_form_key
from blossom.grades.projection import (
    ActionRecorded,
    CurrentPreview,
    MadeCurrent,
    NothingToChange,
    PreviewRevised,
)
from blossom.grades.review import (
    TERM_KEY,
    AlreadyRecorded,
    GradeReportSaved,
    IdentityAnswer,
    MatchAnswer,
    NotHers,
    ReturnReason,
    ReviewReturned,
)
from blossom.grades.text_reader import read_grade_report
from blossom.noticing import canonical_active_input, planning_digest, read_everything, week_from
from blossom.settings import PACKAGE_ROOT
from blossom.stores.gradebook import (
    GRADEBOOK_TABLES,
    UNCONNECTED,
    VIEW_TABLES,
    AlreadyDeleted,
    AnswerNotAsked,
    ClassTermDeleted,
    ContextChanged,
    ContextSet,
    ContextStood,
    CorrectionPage,
    CorrectionRecorded,
    CorrectionReturned,
    DeletePreview,
    FirstMonthCorrected,
    FirstMonthStood,
    HomeworkClassConnected,
    HomeworkClassNotOnRecord,
    HomeworkClassRemoved,
    HomeworkClassStood,
    NameFormAdded,
    ValueCorrected,
)
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    FIXTURES,
    OBSERVED_AT,
    as_stored,
    capture_class,
    closed_world,
    confirm_current,
    correction_page,
    current_preview,
    database_of,
    fixture_clock,
    grade_answers,
    homework_named,
    household_client,
    practice_store,
    reported,
    save_grade,
    state_of,
)

WREN = "Bramble, Wren"
LINNET = "Bramble, Linnet"
NAMES = ("bramble", "wren", "linnet")
KEY = name_form_key(b"5" * 64)
NEW_KEY = name_form_key(b"6" * 64)
REPORT = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")
YEAR = "2026-2027"
MONDAY = date(2026, 9, 14)
"""The week the practice store's two assignments are due in."""


def draft_of(text: str) -> GradeReportDraft:
    reading = read_grade_report(text)
    assert reading.draft is not None
    return reading.draft


WREN_REPORT = draft_of(REPORT)
LINNET_REPORT = draft_of(REPORT.replace("**Bramble, Wren**", "**Bramble, Linnet**"))
ANOTHER_CAPTURE = draft_of(
    REPORT.replace("| Cell Diagram             | 7.0 ", "| Cell Diagram             | 8.0 ")
)
MOVED = draft_of(REPORT.replace("| Missing    | 09/26   |", "| Missing    | 09/29   |"))
"""Another capture: Cell Diagram's due date moved, a question a parent answers."""
MOVED_AGAIN = draft_of(REPORT.replace("| Missing    | 09/26   |", "| Missing    | 09/30   |"))
SEED = "| Seed Germination Log | 18.0 "
KEPT = draft_of(REPORT.replace(SEED, "| Seed Germination Log | 19.0 "))
"""Another capture with A's Cell Diagram after a newer 8.0, and Seed changed: kept as earlier."""
BACK = draft_of(REPORT.replace(SEED, "| Seed Germination Log | 17.0 "))
"""A third such capture, its Cell Diagram brought back under the parent's choice of current."""
SECOND_TERM = draft_of(REPORT.replace("**T1**", "**T2**"))
"""Her second term, deleted and imported again, so the first stays."""


def chosen_current(store: ProjectStateStore, draft: GradeReportDraft) -> object:
    """A parent's save of ``draft`` under the choice of current, selecting every value it offers,
    those that go back to a value a newer report replaced included."""
    review = store.review_grade_report(draft, capture_key(draft), key=KEY)
    answers = dataclasses.replace(grade_answers(review), use="current")
    selection = review.ready | review.back_to
    return save_grade(store, draft, key=KEY, review=review, answers=answers, selection=selection)


EXCUSED_MOVED = draft_of(
    REPORT.replace("| 7.0     | 10.0    |", "| EX      | 10.0    |").replace(
        "| Missing    | 09/26   |", "| Missing    | 10/01   |"
    )
)
"""Another capture: Cell Diagram's score written "EX" and its due date moved, a question a parent
answers though the score can't be read."""


def saved_report(outcome: object) -> GradeReportSaved:
    assert isinstance(outcome, GradeReportSaved), outcome
    return outcome


def answered_matches(store: ProjectStateStore, draft: GradeReportDraft, *, same: bool) -> object:
    """A parent's save of ``draft`` answering every matching question with its first candidate,
    or with "A different assignment", and selecting what is ready and what was answered."""
    review = store.review_grade_report(draft, capture_key(draft), key=KEY)
    matches = []
    for item in review.rows:
        if item.question is not None:
            chosen = item.question.ids[0] if same else None
            matches.append(MatchAnswer(item.key, item.question.ids, chosen))
    answers = dataclasses.replace(grade_answers(review), matches=tuple(matches))
    selection = review.ready | {answer.row_key for answer in matches}
    return save_grade(store, draft, key=KEY, review=review, answers=answers, selection=selection)


def answered_unreadable(store: ProjectStateStore) -> object:
    """A parent's save of ``EXCUSED_MOVED`` answering its question with the first candidate and
    selecting nothing: presence alone."""
    review = store.review_grade_report(EXCUSED_MOVED, capture_key(EXCUSED_MOVED), key=KEY)
    matches = []
    for item in review.rows:
        if item.question is not None:
            matches.append(MatchAnswer(item.key, item.question.ids, item.question.ids[0]))
    assert len(matches) == 1
    answers = dataclasses.replace(grade_answers(review), matches=tuple(matches))
    return save_grade(store, EXCUSED_MOVED, key=KEY, review=review, answers=answers, selection=())


def records_nothing_new(store: ProjectStateStore) -> object:
    """``EXCUSED_MOVED`` again under a fresh page, nothing selected: every row is on record, so
    only the acceptance is written."""
    outcome = save_grade(store, EXCUSED_MOVED, key=KEY, selection=())
    assert isinstance(outcome, GradeReportSaved), outcome
    assert (outcome.report_id, outcome.shown, outcome.answers_kept) == (None, 0, 0)
    return outcome


def expect(kind: type, outcome: object) -> object:
    assert isinstance(outcome, kind), outcome
    return outcome


def class_details_actions(store: ProjectStateStore) -> list[tuple[str, Callable[[], object]]]:
    """The class-details action on the capture kept as earlier, its retry, a confirmation with
    nothing to change, and one posting a digest not of its preview."""
    previews: list[CurrentPreview] = []

    def act() -> object:
        previews.append(current_preview(store, KEPT))
        return expect(MadeCurrent, confirm_current(store, KEPT, previews[0]))

    return [
        ("the class-details action", act),
        ("its retry", lambda: expect(ActionRecorded, confirm_current(store, KEPT, previews[0]))),
        ("nothing to change", lambda: expect(NothingToChange, confirm_current(store, KEPT))),
        (
            "a digest not of its preview",
            lambda: expect(
                PreviewRevised,
                confirm_current(
                    store, ANOTHER_CAPTURE, current_preview(store, ANOTHER_CAPTURE), digest="0"
                ),
            ),
        ),
    ]


def second_term_delete(store: ProjectStateStore) -> list[tuple[str, Callable[[], object]]]:
    """Her second term saved and deleted, the delete's retry, a save page from before it, and the
    term imported again."""
    pages = []
    held: list[tuple[str, int | None]] = []

    def save_second() -> object:
        outcome = expect(GradeReportSaved, save_grade(store, SECOND_TERM, key=KEY))
        pages.append(store.review_grade_report(SECOND_TERM, capture_key(SECOND_TERM), key=KEY))
        return outcome

    def delete() -> object:
        class_id = capture_class(store, SECOND_TERM)
        preview = store.delete_preview(class_id, "T2")
        assert isinstance(preview, DeletePreview), preview
        held.append((class_id, preview.revision))
        return removed()

    def removed() -> object:
        class_id, revision = held[0]
        return store.delete_class_term(class_id, "T2", revision=revision, role="parent")

    return [
        ("her second term", save_second),
        ("a delete of her second term", lambda: expect(ClassTermDeleted, delete())),
        ("its retry", lambda: expect(AlreadyDeleted, removed())),
        (
            "a save page from before it",
            lambda: expect(
                ReviewReturned, save_grade(store, SECOND_TERM, key=KEY, review=pages[0])
            ),
        ),
        (
            "her second term imported again",
            lambda: expect(GradeReportSaved, save_grade(store, SECOND_TERM, key=KEY)),
        ),
    ]


FIRST_TERM = (YEAR, "T1")
NEXT_TERM = (YEAR, "T2")
CURRENT_TERM_CHOSEN = ("the current term chosen", "the first term chosen again")
"""The steps of ``every_grade_write`` that change the current context: a parent's explicit
choice of the current term, each one."""


def current_term_choices(store: ProjectStateStore) -> list[tuple[str, Callable[[], object]]]:
    """A parent's choice of the current term, its retry, a page from before it, and the first
    term chosen again, which leaves the context as the first setup made it."""

    def chosen(shown: tuple[str, str], term: tuple[str, str]) -> object:
        return store.set_current_context(shown, term, "parent")

    return [
        (CURRENT_TERM_CHOSEN[0], lambda: expect(ContextSet, chosen(FIRST_TERM, NEXT_TERM))),
        ("its retry", lambda: expect(ContextStood, chosen(FIRST_TERM, NEXT_TERM))),
        ("a page from before it", lambda: expect(ContextChanged, chosen(FIRST_TERM, FIRST_TERM))),
        (CURRENT_TERM_CHOSEN[1], lambda: expect(ContextSet, chosen(NEXT_TERM, FIRST_TERM))),
    ]


def assertions(store: ProjectStateStore) -> list[tuple[str, Callable[[], object]]]:
    """A Status "Missing" asserted on Seed through the class-details action's copy, its retry, a
    stale page, its withdrawal, and a letter asserted on the term."""
    pages: list[CorrectionPage] = []

    def page(kind: str) -> CorrectionPage:
        class_id = capture_class(store, WREN_REPORT)
        if kind == "term":
            return correction_page(store, class_id, kind, TERM_KEY)
        (seed,) = (
            result
            for result, value in store.current_values(class_id, "T1").results.items()
            if value.cells["assignment"][1] == "Seed Germination Log"
        )
        pages.append(correction_page(store, class_id, kind, seed))
        return pages[-1]

    def asserted(on: CorrectionPage, field: str, text: str | None) -> object:
        value = None if text is None else GradeValue(text=text, presence=Presence.REPORTED)
        return store.correct_value(
            capture_class(store, WREN_REPORT),
            "T1",
            page=on,
            field=field,
            how="parent_assertion",
            value=value,
            role="parent",
        )

    return [
        (
            "a Status asserted through a copy",
            lambda: expect(ValueCorrected, asserted(page("result"), "status", "Missing")),
        ),
        (
            "its retry",
            lambda: expect(CorrectionRecorded, asserted(pages[0], "status", "Missing")),
        ),
        (
            "a stale assertion page",
            lambda: expect(
                CorrectionReturned,
                asserted(dataclasses.replace(pages[0], correction_id="stale"), "status", "Valid"),
            ),
        ),
        (
            "its withdrawal",
            lambda: expect(ValueCorrected, asserted(page("result"), "status", None)),
        ),
        (
            "a letter asserted",
            lambda: expect(ValueCorrected, asserted(page("term"), "letter", "A-")),
        ),
    ]


def homework_class_connected(store: ProjectStateStore, *, removed: bool = False) -> object:
    """A parent's answer for the first homework class name on record, or the removal of that
    connection, each of which stands when it is sent again. With no homework on record the
    write is refused, and that is what it does."""
    class_id = capture_class(store, WREN_REPORT)
    names = [assignment.course for assignment in store.all_assignments()]
    shown, chosen = (class_id, UNCONNECTED) if removed else (None, class_id)
    if not names:
        with pytest.raises(HomeworkClassNotOnRecord):
            store.connect_homework_class(YEAR, "Biology", shown=shown, chosen=chosen, role="parent")
        return None
    outcome = store.connect_homework_class(
        YEAR, names[0], shown=shown, chosen=chosen, role="parent"
    )
    kinds = HomeworkClassConnected | HomeworkClassRemoved | HomeworkClassStood
    assert isinstance(outcome, kinds), outcome
    return outcome


def every_grade_write(store: ProjectStateStore) -> list[tuple[str, Callable[[], object]]]:
    """Each kind of grade write, and each that writes nothing, in an order that reaches them
    all."""
    first = store.review_grade_report(WREN_REPORT, capture_key(WREN_REPORT), key=KEY)

    def answered(draft: GradeReportDraft, key: bytes, answer: IdentityAnswer) -> object:
        review = store.review_grade_report(draft, capture_key(draft), key=key)
        answers = grade_answers(review, answer)
        return save_grade(store, draft, key=key, review=review, answers=answers)

    return [
        ("a review", lambda: first),
        (
            "a first save, in part",
            lambda: expect(
                GradeReportSaved,
                save_grade(store, WREN_REPORT, key=KEY, review=first, selection={TERM_KEY}),
            ),
        ),
        (
            "its retry",
            lambda: expect(
                AlreadyRecorded,
                save_grade(store, WREN_REPORT, key=KEY, review=first, selection={TERM_KEY}),
            ),
        ),
        ("the rest", lambda: expect(GradeReportSaved, save_grade(store, WREN_REPORT, key=KEY))),
        ("a homework class name connected", lambda: homework_class_connected(store)),
        ("its resend", lambda: homework_class_connected(store)),
        ("the connection removed", lambda: homework_class_connected(store, removed=True)),
        ("the removal sent again", lambda: homework_class_connected(store, removed=True)),
        (
            "a stale page",
            lambda: expect(
                ReviewReturned,
                save_grade(
                    store,
                    WREN_REPORT,
                    key=KEY,
                    review=dataclasses.replace(first, acceptance_id="a page from before"),
                ),
            ),
        ),
        (
            "a first-month correction",
            lambda: expect(
                FirstMonthCorrected,
                store.correct_first_month(YEAR, shown=8, month=9, role="parent"),
            ),
        ),
        (
            "its retry",
            lambda: expect(
                FirstMonthStood, store.correct_first_month(YEAR, shown=8, month=9, role="parent")
            ),
        ),
        (
            "a sibling's identical report, misread",
            lambda: expect(GradeReportSaved, answered(LINNET_REPORT, KEY, IdentityAnswer.MISREAD)),
        ),
        (
            "another capture, nothing selected",
            lambda: expect(
                GradeReportSaved, save_grade(store, ANOTHER_CAPTURE, key=KEY, selection=())
            ),
        ),
        (
            "another capture's changed value",
            lambda: expect(GradeReportSaved, save_grade(store, ANOTHER_CAPTURE, key=KEY)),
        ),
        (
            "a capture kept as an earlier report by its default",
            lambda: expect(GradeReportSaved, save_grade(store, KEPT, key=KEY)),
        ),
        (
            "a value back under the choice of current",
            lambda: expect(GradeReportSaved, chosen_current(store, BACK)),
        ),
        (
            "a moved due date answered the same",
            lambda: expect(GradeReportSaved, answered_matches(store, MOVED, same=True)),
        ),
        (
            "a moved due date answered a different assignment",
            lambda: expect(GradeReportSaved, answered_matches(store, MOVED_AGAIN, same=False)),
        ),
        (
            "a score that can't be read, its question answered",
            lambda: expect(GradeReportSaved, answered_unreadable(store)),
        ),
        (
            "a submission that records nothing new",
            lambda: records_nothing_new(store),
        ),
        *second_term_delete(store),
        *current_term_choices(store),
        (
            "the secret replaced, confirmed again",
            lambda: expect(GradeReportSaved, answered(WREN_REPORT, NEW_KEY, IdentityAnswer.HERS)),
        ),
        (
            "not hers",
            lambda: expect(NotHers, answered(LINNET_REPORT, NEW_KEY, IdentityAnswer.NOT_HERS)),
        ),
        *class_details_actions(store),
        *assertions(store),
    ]


def refused(write: Callable[[], object]) -> Callable[[], object]:
    """A write the record refuses, as a step: the refusal is what it does."""

    def step() -> object:
        with pytest.raises(AnswerNotAsked):
            write()
        return None

    return step


def every_name_write(store: ProjectStateStore) -> list[tuple[str, Callable[[], object]]]:
    """Each write this part of the record makes, and each it refuses, in an order that reaches
    them all."""
    return [
        ("her line asked about", lambda: store.identity_of(KEY, WREN)),
        ("the first form", lambda: store.add_name_form(KEY, WREN, "household")),
        ("the same form again", lambda: store.add_name_form(KEY, " bramble,  WREN", "parent")),
        ("another form", lambda: store.add_name_form(KEY, "Wren Bramble", "parent")),
        ("a sibling's line asked about", lambda: store.identity_of(KEY, LINNET)),
        ("a replaced key", refused(lambda: store.add_name_form(NEW_KEY, WREN, "parent"))),
        ("confirmed again", lambda: store.confirm_name_again(NEW_KEY, WREN, "parent")),
        ("confirmed again once more", lambda: store.confirm_name_again(NEW_KEY, WREN, "parent")),
        ("a missing line", refused(lambda: store.add_name_form(NEW_KEY, " ", "parent"))),
    ]


def test_g_i1_no_name_form_write_changes_anything_outside_the_gradebook(
    tmp_path: pathlib.Path,
) -> None:
    with household_client("open", tmp_path) as client:
        state = state_of(client)
        files = [
            pathlib.Path(state.settings.database_path),
            pathlib.Path(state.settings.checkpoint_path),
            pathlib.Path(state.settings.trace_path),
        ]
        before = closed_world(files, leaving_out=GRADEBOOK_TABLES)
        seen = []
        for label, write in every_name_write(state.project_state):
            write()
            seen.append((label, closed_world(files, leaving_out=GRADEBOOK_TABLES) == before))
        confirmed = state.project_state.identity_of(NEW_KEY, WREN).status

    assert any(name.endswith("assignments") for name in before)
    assert seen == [(label, True) for label, _ in seen]
    assert confirmed is IdentityStatus.MATCHES


def test_g_i1_her_record_arrives_on_a_file_from_before_and_changes_nothing_else(
    tmp_path: pathlib.Path,
) -> None:
    with household_client("open", tmp_path) as client:
        path = database_of(client)
    raw = sqlite3.connect(path)
    for table in (*GRADEBOOK_TABLES, *VIEW_TABLES):
        raw.execute(f"DROP TABLE {table}")
    raw.commit()
    raw.close()
    before = closed_world([path], leaving_out=(*GRADEBOOK_TABLES, *VIEW_TABLES))

    store = ProjectStateStore.open(path, fixture_clock())
    made = store.student_id()
    store.close()
    again = ProjectStateStore.open(path, fixture_clock())
    kept = again.student_id()
    remembered = as_stored(again, "grade_view_choices")
    again.close()

    assert closed_world([path], leaving_out=(*GRADEBOOK_TABLES, *VIEW_TABLES)) == before
    assert kept == made
    assert remembered == []


def test_g_i13_no_gradebook_table_or_log_line_holds_a_name(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The file holds nothing but the record's empty tables and the gradebook, so no byte of it
    may spell a name, in any case, freed pages included."""
    path = tmp_path / "blossom.sqlite3"
    store = ProjectStateStore.open(path, fixture_clock())
    with caplog.at_level(logging.DEBUG):
        for _, write in every_name_write(store):
            write()
        for _, write in every_grade_write(store):
            write()
    held = [
        value.decode("utf-8") if isinstance(value, bytes) else str(value)
        for table in GRADEBOOK_TABLES
        for row in as_stored(store, table)
        for value in row
    ]
    store.close()
    whole_file = path.read_bytes().decode("latin-1").casefold()

    assert held
    assert not [value for value in held for name in NAMES if name in value.casefold()]
    assert not [name for name in NAMES if name in whole_file]
    assert not [line for line in caplog.messages for name in NAMES if name in line.casefold()]


def test_g_i15_every_gradebook_row_carries_her_one_student_id(tmp_path: pathlib.Path) -> None:
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    store.put_on_record([homework_named("Biology", "assignment-named")], {})
    for _, write in every_name_write(store):
        write()
    for _, write in every_grade_write(store):
        write()
    store.add_name_form(NEW_KEY, "Wren Bramble", "parent")
    store.choose_view("student", NEXT_TERM)
    store.choose_view("parent", FIRST_TERM)
    tables = (*GRADEBOOK_TABLES, *VIEW_TABLES)

    carried = {
        table: {
            row[0]
            for row in store._connection.execute(f"SELECT student_id FROM {table}")  # noqa: S608
        }
        for table in tables
    }

    assert carried == {table: {store.student_id()} for table in tables}


def test_g_i15_a_form_kept_under_another_student_id_matches_nothing() -> None:
    store = ProjectStateStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())
    store._connection.execute(
        "INSERT INTO grade_name_forms (student_id, name_form, confirmed_by, confirmed_at) "
        "VALUES ('student-someone-else', ?, 'parent', ?)",
        (name_form(KEY, WREN), OBSERVED_AT.isoformat()),
    )
    store._connection.commit()

    assert store.identity_of(KEY, WREN).status is IdentityStatus.FIRST_USE
    assert store.add_name_form(KEY, WREN, "parent") == NameFormAdded(name_form(KEY, WREN))
    assert store.identity_of(KEY, WREN).status is IdentityStatus.MATCHES


def test_g_i1_no_grade_save_changes_anything_outside_the_gradebook(
    tmp_path: pathlib.Path,
) -> None:
    with household_client("open", tmp_path) as client:
        state = state_of(client)
        files = [
            pathlib.Path(state.settings.database_path),
            pathlib.Path(state.settings.checkpoint_path),
            pathlib.Path(state.settings.trace_path),
        ]
        remembered(state.project_state)
        before = closed_world(files, leaving_out=GRADEBOOK_TABLES)
        seen = []
        for label, write in every_grade_write(state.project_state):
            write()
            seen.append((label, closed_world(files, leaving_out=GRADEBOOK_TABLES) == before))
        accepted = as_stored(state.project_state, "grade_acceptances")

    assert any(name.endswith("assignments") for name in before)
    assert len(before[f"{files[0].name} rows of grade_view_choices"]) == 3  # type: ignore[arg-type]
    assert seen == [(label, True) for label, _ in seen]
    assert len(accepted) == 13


def remembered(store: ProjectStateStore) -> None:
    """A remembered term for each viewer, written as the record keeps it, so the closed world
    every grade write is checked against holds one row per viewer."""
    student_id = store.student_id()
    store._connection.executemany(
        "INSERT INTO grade_view_choices (student_id, viewer, year_label, term_label) "
        "VALUES (?, ?, ?, ?)",
        [(student_id, viewer, YEAR, "T1") for viewer in ("student", "parent", "anyone")],
    )
    store._connection.commit()


def test_a_viewer_s_choice_changes_no_grade_table_and_no_other_viewer_s_row(
    tmp_path: pathlib.Path,
) -> None:
    """Choosing, choosing again, and following the current term each write that viewer's row
    alone: every grade table, the other viewers' rows and the closed world beyond read the
    same."""
    with household_client("open", tmp_path) as client:
        state = state_of(client)
        store = state.project_state
        files = [
            pathlib.Path(state.settings.database_path),
            pathlib.Path(state.settings.checkpoint_path),
            pathlib.Path(state.settings.trace_path),
        ]
        for _, write in every_grade_write(store):
            write()
        remembered(store)
        grades = {table: as_stored(store, table) for table in GRADEBOOK_TABLES}
        world = closed_world(files, leaving_out=(*GRADEBOOK_TABLES, *VIEW_TABLES))
        others = [row for row in as_stored(store, "grade_view_choices") if row[1] != b"student"]
        seen = []
        for view in (NEXT_TERM, FIRST_TERM, None):
            store.choose_view("student", view)
            seen.append(
                (
                    {table: as_stored(store, table) for table in GRADEBOOK_TABLES} == grades,
                    closed_world(files, leaving_out=(*GRADEBOOK_TABLES, *VIEW_TABLES)) == world,
                    [r for r in as_stored(store, "grade_view_choices") if r[1] != b"student"]
                    == others,
                    store.view_of("student"),
                )
            )

    assert seen == [
        (True, True, True, NEXT_TERM),
        (True, True, True, FIRST_TERM),
        (True, True, True, None),
    ]


def test_g_i8_a_retry_writes_nothing_and_returns_the_recorded_outcome(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "blossom.sqlite3"
    store = ProjectStateStore.open(path, fixture_clock())
    review = store.review_grade_report(WREN_REPORT, capture_key(WREN_REPORT), key=KEY)
    first = save_grade(store, WREN_REPORT, key=KEY, review=review)
    whole = closed_world([path], leaving_out=())
    changes = store._connection.total_changes

    retries = [save_grade(store, WREN_REPORT, key=KEY, review=review) for _ in range(2)]
    elsewhere = save_grade(store, LINNET_REPORT, key=KEY, review=review)

    assert isinstance(first, GradeReportSaved)
    assert retries == [AlreadyRecorded(first, frozenset())] * 2
    assert elsewhere == AlreadyRecorded(first, frozenset())
    assert store._connection.total_changes == changes
    store.close()
    assert closed_world([path], leaving_out=()) == whole


def test_g_i17_no_grade_save_writes_her_account_or_the_school_status(
    tmp_path: pathlib.Path,
) -> None:
    """Her Not yet and Done, her Turned in, and the school's Missing email stay as they were,
    named here besides G-I1's closed world."""
    facts = ("student_reports", "hand_in_events", "status_reports")
    with household_client("open", tmp_path) as client:
        store = state_of(client).project_state

        def apart() -> tuple[list[tuple[object, ...]], list[list[tuple[object, ...]]]]:
            school = store._connection.execute(
                "SELECT assignment_id, reported_submission_status FROM assignments "
                "ORDER BY assignment_id"
            ).fetchall()
            return school, [as_stored(store, table) for table in facts]

        before = apart()
        for _, write in every_grade_write(store):
            write()
        after = apart()

    assert before[0]
    assert after == before


def test_g_i7_current_values_are_per_target_and_an_earlier_report_supplies_none(
    tmp_path: pathlib.Path,
) -> None:
    """Each target's current value comes from the newest current report that supplied it. A
    report kept as earlier, as the current choice keeps one, supplies none, and no due date."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    first = saved_report(save_grade(store, WREN_REPORT, key=KEY))
    newer = saved_report(save_grade(store, ANOTHER_CAPTURE, key=KEY))
    (class_id,) = store._connection.execute("SELECT class_id FROM grade_classes").fetchone()
    current = store.current_values(class_id, "T1")
    supplied = sorted(value.report_id for value in current.results.values())

    assert supplied == sorted([str(first.report_id)] * 3 + [str(newer.report_id)])
    assert current.term is not None
    assert current.term.report_id == first.report_id
    store.close()
    other = ProjectStateStore.open(tmp_path / "earlier.sqlite3", fixture_clock())
    first = saved_report(save_grade(other, WREN_REPORT, key=KEY))
    review = other.review_grade_report(ANOTHER_CAPTURE, capture_key(ANOTHER_CAPTURE), key=KEY)
    answers = dataclasses.replace(grade_answers(review), use="earlier")
    saved_report(save_grade(other, ANOTHER_CAPTURE, key=KEY, review=review, answers=answers))
    (class_id,) = other._connection.execute("SELECT class_id FROM grade_classes").fetchone()
    kept = other.current_values(class_id, "T1")
    other.close()

    assert {value.report_id for value in kept.results.values()} == {first.report_id}
    assert {
        None if value.due is None else value.due.report.report_id for value in kept.results.values()
    } == {first.report_id}


def test_g_i8_values_become_current_only_through_the_deliberate_current_choice(
    tmp_path: pathlib.Path,
) -> None:
    """A value a newer report replaced comes back only under the parent's choice of current:
    never by a default, a retry or a re-import, which leave every current value as it was."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    saved_report(save_grade(store, WREN_REPORT, key=KEY))
    saved_report(save_grade(store, ANOTHER_CAPTURE, key=KEY))
    (class_id,) = store._connection.execute("SELECT class_id FROM grade_classes").fetchone()
    replaced = store.current_values(class_id, "T1")
    review = store.review_grade_report(KEPT, capture_key(KEPT), key=KEY)
    everything = review.ready | review.back_to

    assert review.use is not None
    assert review.use.default == "earlier"
    by_default = save_grade(store, KEPT, key=KEY, review=review, selection=everything)
    assert isinstance(by_default, ReviewReturned)
    assert by_default.why is ReturnReason.SELECTION
    kept = saved_report(save_grade(store, KEPT, key=KEY, review=review))
    assert isinstance(save_grade(store, KEPT, key=KEY, review=review), AlreadyRecorded)
    again = store.review_grade_report(KEPT, capture_key(KEPT), key=KEY)
    assert (again.use, again.back_to) == (None, frozenset())
    saved_report(save_grade(store, KEPT, key=KEY, review=again))
    assert store.current_values(class_id, "T1") == replaced
    back = saved_report(chosen_current(store, BACK))
    now = store.current_values(class_id, "T1")
    store.close()

    cell = next(item for item in review.rows if '"Cell Diagram"' in item.key)
    value = now.results[cell.result_id or ""]
    assert kept.report_id != back.report_id
    assert (value.report_id, value.cells["points"][1]) == (back.report_id, "7.0")


def test_g_i16_an_ambiguous_match_saves_only_with_the_parent_s_answer_and_keeps_the_id(
    tmp_path: pathlib.Path,
) -> None:
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    saved_report(save_grade(store, WREN_REPORT, key=KEY))
    results = store._connection.execute("SELECT result_id FROM grade_results").fetchall()
    review = store.review_grade_report(MOVED, capture_key(MOVED), key=KEY)
    (asked,) = [item for item in review.rows if item.question is not None]
    changes = store._connection.total_changes

    refused = save_grade(store, MOVED, key=KEY, review=review, selection={asked.key})
    unchanged = store._connection.total_changes == changes
    outcome = saved_report(answered_matches(store, MOVED, same=True))
    kept = store._connection.execute("SELECT result_id FROM grade_results").fetchall()
    store.close()

    assert isinstance(refused, ReviewReturned)
    assert refused.why is ReturnReason.SELECTION
    assert unchanged
    assert asked.question is not None
    assert dict(outcome.accepted)[asked.key] == asked.question.ids[0]
    assert kept == results


def test_g_i19_presence_comes_only_from_a_deliberate_submission_and_accepts_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """A review records nothing, and "Not hers" nothing. A submission of presence alone records
    each reliably matched row with how it matched, accepts no value, leaves every current value
    and report use as it was, says no grade value changed, and its retry writes nothing."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    saved_report(save_grade(store, WREN_REPORT, key=KEY))
    (class_id,) = store._connection.execute("SELECT class_id FROM grade_classes").fetchone()
    before = store.current_values(class_id, "T1")
    uses = store._connection.execute("SELECT report_id, use FROM grade_reports").fetchall()
    changes = store._connection.total_changes
    review = store.review_grade_report(ANOTHER_CAPTURE, capture_key(ANOTHER_CAPTURE), key=KEY)
    not_hers = grade_answers(review, IdentityAnswer.NOT_HERS)
    refused = save_grade(store, ANOTHER_CAPTURE, key=KEY, review=review, answers=not_hers)
    untouched = store._connection.total_changes == changes

    shown = saved_report(save_grade(store, ANOTHER_CAPTURE, key=KEY, review=review, selection=()))
    after = store.current_values(class_id, "T1")
    retried = store._connection.total_changes
    retry = save_grade(store, ANOTHER_CAPTURE, key=KEY, review=review, selection=())
    hows = store._connection.execute(
        "SELECT how FROM grade_match_decisions WHERE report_id = ?", (shown.report_id,)
    ).fetchall()
    kept_uses = store._connection.execute(
        "SELECT report_id, use FROM grade_reports WHERE report_id != ?", (shown.report_id,)
    ).fetchall()
    unchanged_after_retry = store._connection.total_changes == retried
    store.close()

    assert isinstance(refused, NotHers)
    assert untouched
    assert (shown.added, shown.updated, shown.accepted, shown.shown) == (0, 0, (), 4)
    assert sorted(hows) == [("exact",)] * 4
    assert {key: value.cells for key, value in after.results.items()} == {
        key: value.cells for key, value in before.results.items()
    }
    assert after.term == before.term
    assert kept_uses == uses
    assert isinstance(retry, AlreadyRecorded)
    assert unchanged_after_retry


def test_g_i21_every_observation_and_row_record_traces_to_one_acceptance_or_one_action(
    tmp_path: pathlib.Path,
) -> None:
    """After every kind of grade write, each observation is named by exactly one acceptance
    into its report or by the action that made its report, each row record is listed by that
    action or was written with an acceptance into its report, and each correction names one
    observation an acceptance wrote, entered on it or on a copy of it."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    for _, write in every_grade_write(store):
        write()
    connection = store._connection
    named: Counter[tuple[str, str, str]] = Counter()
    accepted_at: dict[str, set[str]] = {}
    for report_id, accepted, at in connection.execute(
        "SELECT report_id, accepted, accepted_at FROM grade_acceptances WHERE report_id IS NOT NULL"
    ):
        accepted_at.setdefault(report_id, set()).add(at)
        for item_key, result_id in json.loads(accepted):
            if result_id is not None:
                named[report_id, "result", result_id] += 1
            elif item_key == TERM_KEY:
                named[report_id, "term", TERM_KEY] += 1
            else:
                named[report_id, "category", item_key] += 1
    listed: Counter[tuple[str, str, str]] = Counter()
    rows_listed: Counter[tuple[str, str, str]] = Counter()
    for report_id, copied in connection.execute(
        "SELECT report_made, copied FROM grade_current_actions"
    ):
        body = json.loads(copied)
        for kind, target in body["observations"]:
            listed[report_id, kind, target] += 1
        for row_key, result_id in body["rows"]:
            rows_listed[report_id, row_key, result_id] += 1
    observations = [
        *(
            (str(report_id), "term", TERM_KEY)
            for (report_id,) in connection.execute("SELECT report_id FROM grade_term_observations")
        ),
        *(
            (str(report_id), "category", str(key))
            for report_id, key in connection.execute(
                "SELECT report_id, category_key FROM grade_category_observations"
            )
        ),
        *(
            (str(report_id), "result", str(result))
            for report_id, result in connection.execute(
                "SELECT report_id, result_id FROM grade_result_observations"
            )
        ),
    ]
    records = connection.execute(
        "SELECT report_id, row_key, result_id, decided_at FROM grade_match_decisions"
    ).fetchall()
    traced = {
        (report_id, row_key): rows_listed[report_id, row_key, result_id]
        + (
            (report_id, row_key, result_id) not in rows_listed
            and decided_at in accepted_at.get(report_id, set())
        )
        for report_id, row_key, result_id, decided_at in records
    }
    source = {
        str(made): str(source)
        for made, source in connection.execute(
            "SELECT report_made, source_report FROM grade_current_actions"
        )
    }

    def copies_of(report_id: str) -> set[str]:
        return (
            {report_id}
            | {made for made, of in source.items() if of == report_id}
            | {
                further
                for made, of in source.items()
                if of == report_id
                for further in copies_of(made)
            }
        )

    corrections = [
        ((str(report_id), str(kind), str(target)), str(entered_on))
        for report_id, kind, target, entered_on in connection.execute(
            "SELECT report_id, kind, target, entered_on FROM grade_corrections"
        )
    ]
    store.close()

    assert listed
    assert rows_listed
    assert {one: named[one] + listed[one] for one in observations} == dict.fromkeys(observations, 1)
    assert (named + listed).total() == len(observations)
    assert traced == dict.fromkeys(traced, 1)
    assert rows_listed.total() == sum(
        1
        for report_id, row_key, result_id, _ in records
        if (report_id, row_key, result_id) in rows_listed
    )
    assert corrections
    assert [(named[original], listed[original]) for original, _ in corrections] == [
        (1, 0) for _ in corrections
    ]
    assert all(entered_on in copies_of(original[0]) for original, entered_on in corrections)
    assert any(entered_on != original[0] for original, entered_on in corrections)


def test_g_i2_the_planner_input_is_byte_identical_across_every_grade_write(
    tmp_path: pathlib.Path,
) -> None:
    """Over a week with homework to plan, what a plan is made from and its fingerprint read the
    same after each grade write as before the first."""
    store = practice_store(tmp_path / "blossom.sqlite3")

    def planner_input() -> tuple[str, list[dict[str, object]]]:
        week = week_from(read_everything(store, store), MONDAY)
        return planning_digest(week), canonical_active_input(week)

    before = planner_input()
    seen = []
    for label, write in every_grade_write(store):
        write()
        seen.append((label, planner_input() == before))
    store.close()

    assert len(before[1]) == 2
    assert seen == [(label, True) for label, _ in seen]


LINE_MISSING = draft_of(REPORT.replace("**Bramble, Wren**", ""))
IdentityCase = tuple[str, bool, GradeReportDraft, GradeReportDraft, bytes, IdentityAnswer]
IDENTITY_CASES: list[IdentityCase] = [
    ("first use, shown as hers", False, WREN_REPORT, WREN_REPORT, KEY, IdentityAnswer.SHOWN),
    ("first use, misread", False, WREN_REPORT, WREN_REPORT, KEY, IdentityAnswer.MISREAD),
    ("a missing line, as hers", False, LINE_MISSING, LINE_MISSING, KEY, IdentityAnswer.HERS),
    ("a missing line, confirmed", False, LINE_MISSING, LINE_MISSING, KEY, IdentityAnswer.CONFIRMED),
    ("a missing line, not hers", False, LINE_MISSING, LINE_MISSING, KEY, IdentityAnswer.NOT_HERS),
    ("not hers", True, LINNET_REPORT, LINNET_REPORT, KEY, IdentityAnswer.NOT_HERS),
    ("a sibling's identical report", True, LINNET_REPORT, LINNET_REPORT, KEY, IdentityAnswer.SHOWN),
    ("a sibling's report misread", True, LINNET_REPORT, LINNET_REPORT, KEY, IdentityAnswer.MISREAD),
    ("a changed student line", True, WREN_REPORT, LINNET_REPORT, KEY, IdentityAnswer.SHOWN),
    ("a replaced secret", True, WREN_REPORT, WREN_REPORT, NEW_KEY, IdentityAnswer.SHOWN),
]
"""Each case: whether her line was confirmed and her report saved first, the report the page
reviewed, the report posted, the key, and the answer the page sends."""
IDENTITY_OUTCOMES = {
    "first use, shown as hers": ReviewReturned,
    "first use, misread": GradeReportSaved,
    "a missing line, as hers": ReviewReturned,
    "a missing line, confirmed": GradeReportSaved,
    "a missing line, not hers": NotHers,
    "not hers": NotHers,
    "a sibling's identical report": ReviewReturned,
    "a sibling's report misread": GradeReportSaved,
    "a changed student line": ReviewReturned,
    "a replaced secret": ReviewReturned,
}


def test_g_i5_nothing_saves_for_another_student_or_an_unconfirmed_identity() -> None:
    """Every import, an already-saved capture included, gets its own identity check, and an
    answer counts only for the line it answered: no answer here adds a name form, and each one
    the check refuses, or "Not hers", writes nothing."""
    seen = {}
    for label, confirmed, reviewed, posted, key, answer in IDENTITY_CASES:
        store = ProjectStateStore(
            sqlite3.connect(":memory:", check_same_thread=False), fixture_clock()
        )
        if confirmed:
            saved_report(save_grade(store, WREN_REPORT, key=KEY))
        forms = as_stored(store, "grade_name_forms")
        review = store.review_grade_report(reviewed, capture_key(reviewed), key=key)
        changes = store._connection.total_changes
        outcome = save_grade(
            store, posted, key=key, review=review, answers=grade_answers(review, answer)
        )
        wrote = store._connection.total_changes != changes
        seen[label] = (type(outcome), wrote, as_stored(store, "grade_name_forms") == forms)
        store.close()

    assert seen == {
        label: (kind, kind is GradeReportSaved, True) for label, kind in IDENTITY_OUTCOMES.items()
    }


GRADE_IMPORTS = {
    "blossom.clock",
    "blossom.grades.draft",
    "blossom.grades.identity",
    "blossom.grades.projection",
    "blossom.grades.review",
    "collections",
    "collections.abc",
    "contextlib",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "hashlib",
    "hmac",
    "json",
    "pydantic",
    "re",
    "secrets",
    "sqlite3",
    "threading",
    "typing",
    "unicodedata",
    "uuid",
}
"""Everything the grade modules and the gradebook import: the standard library, pydantic, the
clock and one another. No model client, no graph, no network, no logging."""


def test_g_i12_the_grade_modules_import_nothing_that_reaches_a_model() -> None:
    files = [*sorted((PACKAGE_ROOT / "grades").glob("*.py")), PACKAGE_ROOT / "stores/gradebook.py"]
    imported: set[str] = set()
    for file in files:
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")

    assert (PACKAGE_ROOT / "grades" / "dates.py") in files
    assert imported == GRADE_IMPORTS


SECOND_TERM = draft_of(REPORT.replace("**T1**", "**T2**"))
EARLIER_YEAR = draft_of(
    REPORT.replace("**2026-2027**", "**2025-2026**").replace("**T1**", "**T3**")
)


def test_g_i18_no_grade_write_changes_the_current_context(tmp_path: pathlib.Path) -> None:
    """After the first setup confirms the year and term, every grade write, imports for another
    term and another year included, leaves the current context as the parent chose it: only a
    parent's explicit choice of the current term changes it."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    steps = [
        *every_grade_write(store),
        ("another term", lambda: saved_report(save_grade(store, SECOND_TERM, key=NEW_KEY))),
        ("another year", lambda: saved_report(save_grade(store, EARLIER_YEAR, key=NEW_KEY))),
    ]
    first: list[tuple[object, ...]] = []
    chosen: list[tuple[object, ...]] = []
    changed_by = []
    for label, write in steps:
        write()
        now = as_stored(store, "grade_context")
        if not first:
            first = chosen = now
        elif now != chosen:
            changed_by.append((label, [row[1:4] for row in now]))
            chosen = now
    years = store._connection.execute("SELECT label FROM grade_years ORDER BY label").fetchall()
    store.close()

    assert [row[1:4] for row in first] == [(YEAR.encode(), b"T1", b"parent")]
    assert changed_by == [
        (CURRENT_TERM_CHOSEN[0], [(YEAR.encode(), b"T2", b"parent")]),
        (CURRENT_TERM_CHOSEN[1], [(YEAR.encode(), b"T1", b"parent")]),
    ]
    assert len(steps) > len(changed_by) + 2
    assert years == [("2025-2026",), (YEAR,)]


def test_every_grade_acceptance_carries_its_report_s_class_and_term(
    tmp_path: pathlib.Path,
) -> None:
    """After every kind of grade write, each acceptance into a report has that report's class
    and term, and each grade acceptance has both, a report-less one included: the delete finds
    them by that exact pair."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    seen = []
    for label, write in every_grade_write(store):
        write()
        astray = store._connection.execute(
            "SELECT COUNT(*) FROM grade_acceptances AS a WHERE a.class_id IS NULL "
            "OR a.term_label IS NULL OR (a.report_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM grade_reports AS r WHERE r.report_id = a.report_id "
            "AND r.student_id = a.student_id AND r.class_id = a.class_id "
            "AND r.term_label = a.term_label))"
        ).fetchone()[0]
        seen.append((label, astray))
    report_less = store._connection.execute(
        "SELECT COUNT(*) FROM grade_acceptances WHERE report_id IS NULL"
    ).fetchone()[0]
    store.close()

    assert report_less
    assert seen == [(label, 0) for label, _ in seen]


def test_a_homework_screenshot_acceptance_carries_no_class_or_term() -> None:
    """The schema keeps a class and term exactly on grade acceptances: a homework screenshot's
    with either, or a grade acceptance missing either, is refused."""
    store = ProjectStateStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())
    student_id = store.student_id()

    def accepted(kind: str, class_id: str | None, term: str | None) -> bool:
        try:
            with store._connection:
                store._connection.execute(
                    "INSERT INTO grade_acceptances (acceptance_id, student_id, kind, source_key, "
                    "report_id, class_id, term_label, accepted, identity_status, "
                    "identity_answer, identity_form, added, updated, already_saved, "
                    "left_to_check, shown, answers_kept, complete, accepted_at, role) "
                    "VALUES (?, ?, ?, 'a capture', NULL, ?, ?, '[]', 'matches', 'shown', NULL, "
                    "0, 0, 0, 0, 0, 0, 1, ?, 'parent')",
                    (f"{kind}-{class_id}-{term}", student_id, kind, class_id, term, "now"),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    cases = {
        (kind, class_id, term): accepted(kind, class_id, term)
        for kind in ("grade_text", "grade_screenshot", "homework_screenshot")
        for class_id in (None, "a class")
        for term in (None, "T1")
    }

    assert cases == {
        (kind, class_id, term): (class_id, term) == (None, None)
        if kind == "homework_screenshot"
        else None not in (class_id, term)
        for kind, class_id, term in cases
    }


KEPT_BY_A_DELETE = (
    "grade_student",
    "grade_name_forms",
    "grade_context",
    "grade_years",
    "grade_terms",
    "grade_classes",
    "grade_class_aliases",
    "grade_homework_classes",
)
SCOPED_BY_REPORT = (
    "grade_corrections",
    "grade_term_observations",
    "grade_category_observations",
    "grade_result_observations",
    "grade_match_decisions",
)
SCOPED_BY_CLASS_AND_TERM = (
    "grade_reports",
    "grade_results",
    "grade_acceptances",
    "grade_current_actions",
)
RAISED_BY_A_DELETE = ("grade_scope_revisions",)


def revised(store: ProjectStateStore) -> list[tuple[tuple[str, str], int]]:
    return [
        ((str(class_id), str(term)), int(revision))
        for class_id, term, revision in store._connection.execute(
            "SELECT class_id, term_label, revision FROM grade_scope_revisions"
        )
    ]


def test_g_i11_a_delete_removes_exactly_its_class_and_term_and_keeps_the_revision(
    tmp_path: pathlib.Path,
) -> None:
    """Every gradebook table is classified kept, scoped or raised, and an unclassified one fails;
    after every kind of grade write, a delete of the first term removes exactly that scope's
    rows, leaves every other row as stored, raises its revision by one, and a page from before
    saves nothing."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    for _, write in every_grade_write(store):
        write()
    class_id = capture_class(store, WREN_REPORT)
    scope = (store.student_id(), class_id, "T1")
    page = store.review_grade_report(MOVED, capture_key(MOVED), key=KEY)
    by_report = (
        "student_id = ? AND report_id IN (SELECT report_id FROM grade_reports "
        "WHERE student_id = ? AND class_id = ? AND term_label = ?)",
        (scope[0], *scope),
    )
    by_scope = ("student_id = ? AND class_id = ? AND term_label = ?", scope)
    where = {
        **dict.fromkeys(SCOPED_BY_REPORT, by_report),
        **dict.fromkeys(SCOPED_BY_CLASS_AND_TERM, by_scope),
    }

    def rows(table: str, sql: str = "1", values: tuple[str, ...] = ()) -> list[str]:
        query = f"SELECT * FROM {table} WHERE {sql}"  # noqa: S608
        return sorted(repr(row) for row in store._connection.execute(query, values))

    inside = {table: len(rows(table, *where[table])) for table in where}
    outside = {table: rows(table, f"NOT ({where[table][0]})", where[table][1]) for table in where}
    kept = {table: rows(table) for table in KEPT_BY_A_DELETE}
    revisions = dict(revised(store))
    preview = store.delete_preview(class_id, "T1")
    assert isinstance(preview, DeletePreview)
    outcome = store.delete_class_term(class_id, "T1", revision=preview.revision, role="parent")
    after = {table: rows(table) for table in where}
    kept_after = {table: rows(table) for table in KEPT_BY_A_DELETE}
    raised = dict(revised(store))
    stale = save_grade(store, MOVED, key=KEY, review=page)
    store.close()

    classified = (
        *KEPT_BY_A_DELETE,
        *SCOPED_BY_REPORT,
        *SCOPED_BY_CLASS_AND_TERM,
        *RAISED_BY_A_DELETE,
    )
    assert sorted(classified) == sorted(GRADEBOOK_TABLES)
    assert isinstance(outcome, ClassTermDeleted)
    assert all(inside.values()), inside
    assert after == outside
    assert kept_after == kept
    assert raised == {**revisions, (class_id, "T1"): revisions[class_id, "T1"] + 1}
    assert isinstance(stale, ReviewReturned)
    assert stale.why is ReturnReason.REVISION


OBSERVATION_TABLES = (
    "grade_reports",
    "grade_term_observations",
    "grade_category_observations",
    "grade_result_observations",
)


def test_g_i6_saved_observations_never_change_and_records_keep_the_reading(
    tmp_path: pathlib.Path,
) -> None:
    """Across every kind of grade write, a stored report, observation or correction row is
    never changed, and only the delete removes any; each correction keeps its original's cell
    as read."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())

    def rows() -> set[tuple[str, tuple[object, ...]]]:
        return {
            (table, row)
            for table in (*OBSERVATION_TABLES, "grade_corrections")
            for row in as_stored(store, table)
        }

    seen = []
    before = rows()
    for label, write in every_grade_write(store):
        write()
        after = rows()
        gone = {table for table, _ in before - after}
        seen.append((label, gone))
        before = after
    columns = {
        "term": "grade_term_observations WHERE report_id = :report",
        "category": "grade_category_observations "
        "WHERE report_id = :report AND category_key = :target",
        "result": "grade_result_observations WHERE report_id = :report AND result_id = :target",
    }
    kept = []
    for report_id, kind, target, field, text, presence in store._connection.execute(
        "SELECT report_id, kind, target, field, read_text, read_presence FROM grade_corrections"
    ).fetchall():
        query = f"SELECT {field}_text, {field}_presence FROM {columns[kind]}"  # noqa: S608
        stored = store._connection.execute(query, {"report": report_id, "target": target})
        kept.append(stored.fetchone() == (text, presence))
    store.close()

    deleting = "a delete of her second term"
    assert [label for label, gone in seen if gone] == [deleting]
    assert dict(seen)[deleting] <= set(OBSERVATION_TABLES)
    assert kept
    assert all(kept)


def test_only_gradebook_names_the_corrections_table() -> None:
    """Every module that names the corrections table is the gradebook's store, so nothing else
    reads an assertion into a check, a plan or her account."""
    naming = sorted(
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".html", ".js", ".sql"}
        and "grade_corrections" in path.read_text(encoding="utf-8")
    )

    assert naming == ["stores/gradebook.py"]


def test_g3a_i13_no_homework_write_changes_a_gradebook_table(tmp_path: pathlib.Path) -> None:
    """The reverse boundary, the mapping included: homework put on record, the school's status,
    her update and its undo, and the homework that carried a connected name leaving the record
    each leave every gradebook table byte for byte, so an answer outlives its homework."""
    with household_client("open", tmp_path) as client:
        store = state_of(client).project_state
        path = database_of(client)
        expect(GradeReportSaved, save_grade(store, WREN_REPORT, key=KEY))
        class_id = capture_class(store, WREN_REPORT)
        first = store.all_assignments()[0]
        expect(
            HomeworkClassConnected,
            store.connect_homework_class(
                YEAR, first.course, shown=None, chosen=class_id, role="parent"
            ),
        )

        def gradebook_tables() -> dict[str, object]:
            world = closed_world([path], leaving_out=())
            return {
                name: value
                for name, value in world.items()
                if name.rsplit(" ", 1)[-1] in GRADEBOOK_TABLES
            }

        def left_the_record() -> None:
            store._connection.execute("DELETE FROM assignments WHERE course = ?", (first.course,))
            store._connection.commit()

        before = gradebook_tables()
        writes: list[tuple[str, Callable[[], object]]] = [
            (
                "homework put on record",
                lambda: store.put_on_record([homework_named("Art", "assignment-art")], {}),
            ),
            (
                "the same name in another spelling",
                lambda: store.put_on_record(
                    [homework_named(first.course.upper(), "assignment-again")], {}
                ),
            ),
            ("her update", lambda: reported(store, "done", first.assignment_id)),
            (
                "its undo",
                lambda: store.undo_report(
                    first.assignment_id,
                    store.student_reports(first.assignment_id)[-1].report_id,
                    now=OBSERVED_AT,
                    today=MONDAY,
                ),
            ),
            ("the homework that carried the name leaving", left_the_record),
        ]
        seen = []
        for label, write in writes:
            write()
            seen.append((label, gradebook_tables() == before))

    assert any(name.endswith("rows of grade_homework_classes") for name in before)
    assert before[f"{path.name} rows of grade_homework_classes"] != []
    assert seen == [(label, True) for label, _ in seen]
