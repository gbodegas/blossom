# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The gradebook's invariants, each with a test named for it.

G-I1: no gradebook write changes anything outside the gradebook's own tables, read as a closed
world from ``sqlite_master`` with the checkpoint and trace files beside it, so a table added later
is covered unless it is named a gradebook table. G-I8: a retry writes nothing and returns what
was recorded. G-I13: no gradebook table, and no log line, holds a student's name. G-I15: every
gradebook row carries her one student ID, and nothing is looked up under another. G-I17: no
grade save writes her own account or the school's submission status.
"""

import dataclasses
import logging
import pathlib
import sqlite3
from collections.abc import Callable

import pytest

from blossom.grades.draft import GradeReportDraft, capture_key
from blossom.grades.identity import IdentityStatus, name_form, name_form_key
from blossom.grades.review import (
    TERM_KEY,
    AlreadyRecorded,
    GradeReportSaved,
    IdentityAnswer,
    NotHers,
    ReviewReturned,
)
from blossom.grades.text_reader import read_grade_report
from blossom.stores.gradebook import GRADEBOOK_TABLES, AnswerNotAsked, NameFormAdded
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    FIXTURES,
    OBSERVED_AT,
    as_stored,
    closed_world,
    database_of,
    fixture_clock,
    grade_answers,
    household_client,
    save_grade,
    state_of,
)

WREN = "Bramble, Wren"
LINNET = "Bramble, Linnet"
NAMES = ("bramble", "wren", "linnet")
KEY = name_form_key(b"5" * 64)
NEW_KEY = name_form_key(b"6" * 64)
REPORT = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")


def draft_of(text: str) -> GradeReportDraft:
    reading = read_grade_report(text)
    assert reading.draft is not None
    return reading.draft


WREN_REPORT = draft_of(REPORT)
LINNET_REPORT = draft_of(REPORT.replace("**Bramble, Wren**", "**Bramble, Linnet**"))
ANOTHER_CAPTURE = draft_of(
    REPORT.replace("| Cell Diagram             | 7.0 ", "| Cell Diagram             | 8.0 ")
)


def expect(kind: type, outcome: object) -> object:
    assert isinstance(outcome, kind), outcome
    return outcome


def every_grade_write(store: ProjectStateStore) -> list[tuple[str, Callable[[], object]]]:
    """Each kind of grade save, and each save that writes nothing, in an order that reaches
    them all."""
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
            "a sibling's identical report, misread",
            lambda: expect(GradeReportSaved, answered(LINNET_REPORT, KEY, IdentityAnswer.MISREAD)),
        ),
        (
            "another capture, nothing selectable",
            lambda: expect(
                GradeReportSaved, save_grade(store, ANOTHER_CAPTURE, key=KEY, selection=())
            ),
        ),
        (
            "the secret replaced, confirmed again",
            lambda: expect(GradeReportSaved, answered(WREN_REPORT, NEW_KEY, IdentityAnswer.HERS)),
        ),
        (
            "not hers",
            lambda: expect(NotHers, answered(LINNET_REPORT, NEW_KEY, IdentityAnswer.NOT_HERS)),
        ),
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
    for table in GRADEBOOK_TABLES:
        raw.execute(f"DROP TABLE {table}")
    raw.commit()
    raw.close()
    before = closed_world([path], leaving_out=GRADEBOOK_TABLES)

    store = ProjectStateStore.open(path, fixture_clock())
    made = store.student_id()
    store.close()
    again = ProjectStateStore.open(path, fixture_clock())
    kept = again.student_id()
    again.close()

    assert closed_world([path], leaving_out=GRADEBOOK_TABLES) == before
    assert kept == made


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
    for _, write in every_name_write(store):
        write()
    for _, write in every_grade_write(store):
        write()
    store.add_name_form(NEW_KEY, "Wren Bramble", "parent")

    carried = {
        table: {
            row[0]
            for row in store._connection.execute(f"SELECT student_id FROM {table}")  # noqa: S608
        }
        for table in GRADEBOOK_TABLES
    }

    assert carried == {table: {store.student_id()} for table in GRADEBOOK_TABLES}


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
        before = closed_world(files, leaving_out=GRADEBOOK_TABLES)
        seen = []
        for label, write in every_grade_write(state.project_state):
            write()
            seen.append((label, closed_world(files, leaving_out=GRADEBOOK_TABLES) == before))
        accepted = as_stored(state.project_state, "grade_acceptances")

    assert any(name.endswith("assignments") for name in before)
    assert seen == [(label, True) for label, _ in seen]
    assert len(accepted) == 5


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
