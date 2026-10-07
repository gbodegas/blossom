# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The gradebook's invariants, each with a test named for it.

G-I1: no gradebook write changes anything outside the gradebook's own tables, read as a closed
world from ``sqlite_master`` with the checkpoint and trace files beside it, so a table added later
is covered unless it is named a gradebook table. G-I7: current values are per target, and a
report kept as earlier changes none. G-I8: a retry writes nothing and returns what was recorded,
and a value a newer report replaced comes back only under the parent's choice of current.
G-I13: no gradebook table, and no log line, holds a student's name. G-I15: every gradebook row
carries her one student ID, and nothing is looked up under another. G-I16: a result is its
stable ID, and an ambiguous match never saves without a parent's answer. G-I17: no grade save
writes her own account or the school's submission status.
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
    MatchAnswer,
    NotHers,
    ReturnReason,
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
MOVED = draft_of(REPORT.replace("| Missing    | 09/26   |", "| Missing    | 09/29   |"))
"""Another capture: Cell Diagram's due date moved, a question a parent answers."""
MOVED_AGAIN = draft_of(REPORT.replace("| Missing    | 09/26   |", "| Missing    | 09/30   |"))
SEED = "| Seed Germination Log | 18.0 "
KEPT = draft_of(REPORT.replace(SEED, "| Seed Germination Log | 19.0 "))
"""Another capture with A's Cell Diagram after a newer 8.0, and Seed changed: kept as earlier."""
BACK = draft_of(REPORT.replace(SEED, "| Seed Germination Log | 17.0 "))
"""A third such capture, its Cell Diagram brought back under the parent's choice of current."""


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
    assert len(accepted) == 12


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
    report kept as earlier, as the current choice keeps one, supplies none."""
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
