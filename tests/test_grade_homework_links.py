# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The record of a parent's homework links: its table, its one index, what a start does about
that index, the read of her links, and the one write that decides a link.

Where a test stores a link row, drops the index or makes another index through its own SQL, its
docstring says so. Such a test shows what the file, a start, the read and the write then do. It
does not show that a write of Blossom's leaves a file that way. The files are synthetic: Wren's
fixture report, saved through the gradebook's own save, and homework rows made for the test.
"""

import itertools
import logging
import pathlib
import random
import sqlite3
from collections.abc import Callable
from datetime import UTC

import pytest

from blossom.grades.draft import GradeReportDraft
from blossom.grades.identity import name_form_key
from blossom.grades.review import GradeReportSaved, GradeValueUndecodable, HomeworkLinksConflict
from blossom.grades.text_reader import read_grade_report
from blossom.stores.gradebook import (
    CREATE_HOMEWORK_LINKS_INDEX,
    GRADEBOOK_TABLES,
    HOMEWORK_LINKS_INDEX_DEFINITION,
    UNCONNECTED,
    VIEW_TABLES,
    ClassTermDeleted,
    DeletePreview,
    HomeworkClassRemoved,
    HomeworkLink,
    HomeworkLinkChanged,
    HomeworkLinkClassNameChanged,
    HomeworkLinkHomeworkNotOnRecord,
    HomeworkLinkOutcome,
    HomeworkLinkResultNotOnRecord,
    HomeworkLinkStood,
    HomeworkLinkWritten,
    LinkDecision,
    RequiredIndexUnavailable,
)
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    FIXTURES,
    capture_class,
    closed_world,
    fixture_clock,
    homework_named,
    household_client,
    reported,
    save_grade,
)

KEY = name_form_key(b"5" * 64)
INDEX = "grade_homework_links_one_homework"
DEFINITION_AS_STORED = (
    "CREATE UNIQUE INDEX grade_homework_links_one_homework\n"
    "ON grade_homework_links (student_id, result_id)\n"
    "WHERE state = 'confirmed'"
)
"""This module's own copy of the index's definition, character for character."""
NOT_MADE = "the homework links index was not made: two confirmed links name one result"
"""This module's own copy of the one record a start writes when it goes on without the index."""
STARTS = "blossom.stores.project_state"
LINK = "INSERT INTO grade_homework_links VALUES (?, ?, ?, ?, 'parent', '2026-09-14T12:00:00+00:00')"
DROP_INDEX = f"DROP INDEX {INDEX}"
SOMEONE_ELSE = "student-someone-else"

Statement = tuple[str, tuple[object, ...]]


def draft_of(text: str) -> GradeReportDraft:
    reading = read_grade_report(text)
    assert reading.draft is not None, reading.not_read
    return reading.draft


WREN = draft_of((FIXTURES / "grade_report.md").read_text(encoding="utf-8"))


def with_a_report(path: pathlib.Path) -> tuple[str, list[str]]:
    """A file the store's open made, Wren's report saved in it and the file closed: her
    student ID and the IDs of her results."""
    store = ProjectStateStore.open(path, fixture_clock())
    assert isinstance(save_grade(store, WREN, key=KEY), GradeReportSaved)
    student = store.student_id()
    results = [
        str(result_id)
        for (result_id,) in store._connection.execute(
            "SELECT result_id FROM grade_results ORDER BY rowid"
        )
    ]
    store.close()
    assert len(results) >= 2
    return student, results


def by_hand(path: pathlib.Path, *statements: Statement, unchecked: bool = False) -> None:
    """Statements run on the closed file through a connection of the test's own, as no write of
    Blossom's runs them; ``unchecked`` turns the table's CHECK constraints off for them."""
    raw = sqlite3.connect(path)
    try:
        if unchecked:
            raw.execute("PRAGMA ignore_check_constraints = ON")
        with raw:
            for sql, values in statements:
                raw.execute(sql, values)
    finally:
        raw.close()


def confirmed(student: str, result: str, assignment: str) -> Statement:
    return LINK, (student, result, assignment, "confirmed")


def kept_apart(student: str, result: str, assignment: str) -> Statement:
    return LINK, (student, result, assignment, "rejected")


def damaged(path: pathlib.Path) -> tuple[str, list[str]]:
    """A file with a report, then changed by hand: the index dropped, and two confirmed rows
    stored that name her first result."""
    student, results = with_a_report(path)
    by_hand(
        path,
        (DROP_INDEX, ()),
        confirmed(student, results[0], "assignment-one"),
        confirmed(student, results[0], "assignment-two"),
    )
    return student, results


def index_stored(path: pathlib.Path) -> list[tuple[object, ...]]:
    """What the file's catalog holds for the index's name: each row's stored definition."""
    raw = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return raw.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (INDEX,)
        ).fetchall()
    finally:
        raw.close()


def tables_in(path: pathlib.Path) -> set[str]:
    raw = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return {str(name) for (name,) in raw.execute("SELECT name FROM sqlite_master")}
    finally:
        raw.close()


def link_rows(path: pathlib.Path) -> list[tuple[object, ...]]:
    """Every link row of the closed file, in the file's order, read by the test."""
    raw = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return raw.execute("SELECT * FROM grade_homework_links ORDER BY rowid").fetchall()
    finally:
        raw.close()


def start_records(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    """Each record the store's module wrote, as its level and its whole message."""
    return [(one.levelname, one.getMessage()) for one in caplog.records if one.name == STARTS]


def by_open(path: pathlib.Path) -> None:
    ProjectStateStore.open(path, fixture_clock()).close()


def by_initialize(path: pathlib.Path) -> None:
    ProjectStateStore.initialize(path, fixture_clock()).close()


def by_the_application(path: pathlib.Path) -> None:
    assert path.name == "blossom.sqlite3"
    with household_client("open", path.parent):
        pass


WAYS: dict[str, Callable[[pathlib.Path], None]] = {
    "the store's open": by_open,
    "a first start's one transaction": by_initialize,
    "the application": by_the_application,
}


# ------------------------------------------------------------- the index's definition


def test_the_index_definition_is_spelled_as_this_module_s_copy() -> None:
    """The constant equals the copy written out here, so a change to its spelling fails."""
    assert HOMEWORK_LINKS_INDEX_DEFINITION == DEFINITION_AS_STORED


def test_the_statement_a_start_runs_is_the_definition_with_if_not_exists_put_in() -> None:
    assert CREATE_HOMEWORK_LINKS_INDEX != HOMEWORK_LINKS_INDEX_DEFINITION
    assert CREATE_HOMEWORK_LINKS_INDEX.count(" IF NOT EXISTS") == 1
    assert CREATE_HOMEWORK_LINKS_INDEX.replace(" IF NOT EXISTS", "") == DEFINITION_AS_STORED
    assert CREATE_HOMEWORK_LINKS_INDEX.startswith("CREATE UNIQUE INDEX IF NOT EXISTS ")


@pytest.mark.parametrize("way", WAYS)
def test_a_new_file_stores_the_definition_whole_and_keeps_it_when_opened_again(
    way: str, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A blank file made each way holds the link table and, in its catalog, the constant's
    text; closed and opened again by the store and by the application, it holds the same."""
    path = tmp_path / "blossom.sqlite3"
    with caplog.at_level(logging.DEBUG, logger=STARTS):
        WAYS[way](path)
        made = index_stored(path)
        by_open(path)
        reopened = index_stored(path)
        by_the_application(path)
        started = index_stored(path)

    assert made == [(HOMEWORK_LINKS_INDEX_DEFINITION,)]
    assert reopened == made
    assert started == made
    assert "grade_homework_links" in tables_in(path)
    assert start_records(caplog) == []


def test_a_file_holding_link_rows_keeps_the_definition_when_opened_again(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file an earlier open made, with one confirmed and one kept-apart row stored by hand
    for one result: opened again, the catalog's text and the rows are as they were."""
    path = tmp_path / "blossom.sqlite3"
    student, results = with_a_report(path)
    by_hand(
        path,
        confirmed(student, results[0], "assignment-one"),
        kept_apart(student, results[0], "assignment-two"),
    )
    rows = link_rows(path)

    with caplog.at_level(logging.DEBUG, logger=STARTS):
        by_open(path)

    assert len(rows) == 2
    assert link_rows(path) == rows
    assert index_stored(path) == [(DEFINITION_AS_STORED,)]
    assert start_records(caplog) == []


def test_a_file_from_before_the_link_table_gains_the_table_and_the_index(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The link table is dropped by hand, its index going with it; the next open makes both
    and logs nothing."""
    path = tmp_path / "blossom.sqlite3"
    with_a_report(path)
    by_hand(path, ("DROP TABLE grade_homework_links", ()))
    assert index_stored(path) == []

    with caplog.at_level(logging.DEBUG, logger=STARTS):
        by_open(path)

    assert "grade_homework_links" in tables_in(path)
    assert index_stored(path) == [(DEFINITION_AS_STORED,)]
    assert link_rows(path) == []
    assert start_records(caplog) == []


def test_the_file_refuses_a_second_confirmed_row_for_one_result(tmp_path: pathlib.Path) -> None:
    """With the index a start made in place, a second confirmed row stored by hand for one
    result is refused by the file, and a kept-apart row beside the first is not."""
    path = tmp_path / "blossom.sqlite3"
    student, results = with_a_report(path)
    by_hand(path, confirmed(student, results[0], "assignment-one"))

    with pytest.raises(sqlite3.IntegrityError):
        by_hand(path, confirmed(student, results[0], "assignment-two"))
    by_hand(path, kept_apart(student, results[0], "assignment-two"))

    assert [row[2:4] for row in link_rows(path)] == [
        ("assignment-one", "confirmed"),
        ("assignment-two", "rejected"),
    ]


# ------------------------------------------------------------- the index at a start


@pytest.mark.parametrize("way", ["the store's open", "the application"])
def test_a_start_goes_on_without_the_index_when_two_confirmed_rows_name_one_result(
    way: str, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """On the file changed by hand the start succeeds, writes its one fixed record, leaves the
    link rows row for row, and leaves the file without the index."""
    path = tmp_path / "blossom.sqlite3"
    damaged(path)
    rows = link_rows(path)

    with caplog.at_level(logging.DEBUG, logger=STARTS):
        WAYS[way](path)

    written = [one for one in caplog.records if one.name == STARTS]
    assert start_records(caplog) == [("WARNING", NOT_MADE)]
    assert [(one.msg, one.args, one.exc_info, one.exc_text, one.stack_info) for one in written] == [
        (NOT_MADE, (), None, None, None)
    ]
    assert len(rows) == 2
    assert link_rows(path) == rows
    assert index_stored(path) == []


def test_each_start_on_that_file_writes_the_record_once_and_changes_nothing(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "blossom.sqlite3"
    damaged(path)
    rows = link_rows(path)
    seen = []

    for _ in range(2):
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger=STARTS):
            by_open(path)
        seen.append((start_records(caplog), link_rows(path) == rows, index_stored(path)))

    assert seen == [([("WARNING", NOT_MADE)], True, [])] * 2


def test_the_gradebook_s_tables_are_made_and_kept_though_the_index_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """Beside the two rows, two gradebook tables are dropped by hand before the start; after
    it every gradebook table is in the file and her student ID is the one from before."""
    path = tmp_path / "blossom.sqlite3"
    student, _ = damaged(path)
    by_hand(path, ("DROP TABLE grade_homework_classes", ()), ("DROP TABLE grade_view_choices", ()))

    store = ProjectStateStore.open(path, fixture_clock())
    kept = store.student_id()
    open_transaction = store._connection.in_transaction
    store.close()

    assert set(GRADEBOOK_TABLES) | set(VIEW_TABLES) <= tables_in(path)
    assert kept == student
    assert not open_transaction
    assert index_stored(path) == []


def test_the_store_reads_and_writes_after_a_start_without_the_index(
    tmp_path: pathlib.Path,
) -> None:
    """After that start homework is put on record, her update is stored and undone, and a
    second report is saved; the read of her links raises the conflict the two rows hold."""
    path = tmp_path / "blossom.sqlite3"
    damaged(path)
    rows = link_rows(path)
    homework = homework_named("Biology", "assignment-one")
    moment = fixture_clock().now()

    store = ProjectStateStore.open(path, fixture_clock())
    store.put_on_record([homework], {})
    reported(store, "done", homework.assignment_id)
    updates = store.student_reports(homework.assignment_id)
    store.undo_report(
        homework.assignment_id, updates[-1].report_id, now=moment, today=moment.date()
    )
    undone = store.student_reports(homework.assignment_id)
    second = draft_of(
        (FIXTURES / "grade_report.md").read_text(encoding="utf-8").replace("**T1**", "**T2**")
    )
    saved = save_grade(store, second, key=KEY)
    with pytest.raises(HomeworkLinksConflict):
        store.homework_links()
    on_record = [one.assignment_id for one in store.all_assignments()]
    store.close()

    assert on_record == [homework.assignment_id]
    assert (len(updates), len(undone)) == (1, 2)
    assert isinstance(saved, GradeReportSaved)
    assert link_rows(path) == rows


def test_the_first_start_after_a_repair_by_hand_makes_the_index(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One of the two rows is set to rejected by hand after a start without the index. The
    next start makes the index and logs nothing, the read returns both rows, and the file
    then refuses a second confirmed row stored by hand."""
    path = tmp_path / "blossom.sqlite3"
    student, results = damaged(path)
    by_open(path)
    by_hand(
        path,
        (
            "UPDATE grade_homework_links SET state = 'rejected' WHERE assignment_id = ?",
            ("assignment-two",),
        ),
    )
    before = index_stored(path)
    first = start_records(caplog)
    caplog.clear()

    with caplog.at_level(logging.DEBUG, logger=STARTS):
        store = ProjectStateStore.open(path, fixture_clock())
        links = store.homework_links()
        store.close()
    with pytest.raises(sqlite3.IntegrityError):
        by_hand(path, confirmed(student, results[0], "assignment-three"))

    assert first == [("WARNING", NOT_MADE)]
    assert before == []
    assert index_stored(path) == [(DEFINITION_AS_STORED,)]
    assert start_records(caplog) == []
    assert links == (
        HomeworkLink(results[0], "assignment-one", "confirmed"),
        HomeworkLink(results[0], "assignment-two", "rejected"),
    )


def test_a_start_goes_on_when_the_two_rows_are_under_another_student_id(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The index is dropped by hand and two confirmed rows for one result are stored under a
    student ID that is not hers: the start writes its one record, and her read returns none."""
    path = tmp_path / "blossom.sqlite3"
    _, results = with_a_report(path)
    by_hand(
        path,
        (DROP_INDEX, ()),
        confirmed(SOMEONE_ELSE, results[0], "assignment-one"),
        confirmed(SOMEONE_ELSE, results[0], "assignment-two"),
    )

    with caplog.at_level(logging.DEBUG, logger=STARTS):
        store = ProjectStateStore.open(path, fixture_clock())
        links = store.homework_links()
        store.close()

    assert start_records(caplog) == [("WARNING", NOT_MADE)]
    assert links == ()
    assert index_stored(path) == []
    assert len(link_rows(path)) == 2


def test_a_refusal_of_the_index_with_no_such_rows_stops_the_start(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The step that runs the index statement is replaced by one that raises an integrity
    error, on a file whose link rows hold no result confirmed twice: the open raises it."""
    path = tmp_path / "blossom.sqlite3"
    student, results = with_a_report(path)
    by_hand(path, confirmed(student, results[0], "assignment-one"))
    refusal = sqlite3.IntegrityError("a refusal made by the test")

    def refused(_: ProjectStateStore) -> None:
        raise refusal

    monkeypatch.setattr(ProjectStateStore, "_create_homework_links_index", refused)
    with caplog.at_level(logging.DEBUG, logger=STARTS), pytest.raises(sqlite3.Error) as stopped:
        ProjectStateStore.open(path, fixture_clock())

    assert stopped.value is refusal
    assert start_records(caplog) == []


def test_another_error_from_the_index_step_stops_the_start(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the file changed by hand, the step that runs the index statement is replaced by one
    that raises an operational error: the open raises it and no record is written."""
    path = tmp_path / "blossom.sqlite3"
    damaged(path)
    rows = link_rows(path)
    failure = sqlite3.OperationalError("a failure made by the test")

    def failed(_: ProjectStateStore) -> None:
        raise failure

    monkeypatch.setattr(ProjectStateStore, "_create_homework_links_index", failed)
    with caplog.at_level(logging.DEBUG, logger=STARTS), pytest.raises(sqlite3.Error) as stopped:
        ProjectStateStore.open(path, fixture_clock())

    assert stopped.value is failure
    assert start_records(caplog) == []
    assert link_rows(path) == rows


def test_an_integrity_error_from_another_step_stops_the_start(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the file changed by hand, the step that makes the gradebook's tables is replaced by
    one that raises an integrity error: the open raises it and no record is written."""
    path = tmp_path / "blossom.sqlite3"
    damaged(path)
    refusal = sqlite3.IntegrityError("a refusal made by the test")

    def refused(_: ProjectStateStore) -> None:
        raise refusal

    monkeypatch.setattr(ProjectStateStore, "_create_gradebook_tables", refused)
    with caplog.at_level(logging.DEBUG, logger=STARTS), pytest.raises(sqlite3.Error) as stopped:
        ProjectStateStore.open(path, fixture_clock())

    assert stopped.value is refusal
    assert start_records(caplog) == []


def test_a_failure_of_the_verifying_read_stops_the_start(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the file changed by hand, the read that looks for a result confirmed twice is
    replaced by one that raises: the open raises the read's error and no record is written."""
    path = tmp_path / "blossom.sqlite3"
    damaged(path)
    rows = link_rows(path)
    failure = sqlite3.OperationalError("a failure made by the test")

    def failed(_: ProjectStateStore) -> bool:
        raise failure

    monkeypatch.setattr(ProjectStateStore, "_links_twice_confirmed", failed)
    with caplog.at_level(logging.DEBUG, logger=STARTS), pytest.raises(sqlite3.Error) as stopped:
        ProjectStateStore.open(path, fixture_clock())

    assert stopped.value is failure
    assert start_records(caplog) == []
    assert link_rows(path) == rows
    assert index_stored(path) == []


# ------------------------------------------------------------- the conflict error


def test_the_conflict_error_is_a_grade_value_error_with_a_fixed_message_of_its_own() -> None:
    conflict = HomeworkLinksConflict()

    assert isinstance(conflict, GradeValueUndecodable)
    assert conflict.args == ("two confirmed homework links name one result",)
    assert GradeValueUndecodable().args == ("a stored grade value can't be decoded",)
    with pytest.raises(TypeError):
        HomeworkLinksConflict("anything")  # type: ignore[call-arg]


# ------------------------------------------------------------- the read of her links


def test_a_file_with_no_link_row_reads_as_no_links(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "blossom.sqlite3"
    with_a_report(path)

    store = ProjectStateStore.open(path, fixture_clock())
    links = store.homework_links()
    store.close()

    assert links == ()


def test_the_read_returns_every_row_under_her_student_id_and_no_other(
    tmp_path: pathlib.Path,
) -> None:
    """Rows stored by hand: hers for two results and for a result ID that is on no record, in
    both states, and one under another student ID. The read returns hers, as stored."""
    path = tmp_path / "blossom.sqlite3"
    student, results = with_a_report(path)
    by_hand(
        path,
        confirmed(student, results[0], "assignment-one"),
        kept_apart(student, results[0], "assignment-two"),
        confirmed(student, results[1], "assignment-two"),
        kept_apart(student, "result-on-no-record", "assignment-one"),
        confirmed(SOMEONE_ELSE, results[1], "assignment-three"),
    )

    store = ProjectStateStore.open(path, fixture_clock())
    links = store.homework_links()
    store.close()

    assert sorted(links, key=lambda link: (link.result_id, link.assignment_id)) == sorted(
        [
            HomeworkLink(results[0], "assignment-one", "confirmed"),
            HomeworkLink(results[0], "assignment-two", "rejected"),
            HomeworkLink(results[1], "assignment-two", "confirmed"),
            HomeworkLink("result-on-no-record", "assignment-one", "rejected"),
        ],
        key=lambda link: (link.result_id, link.assignment_id),
    )


def test_two_confirmed_rows_for_one_assignment_and_two_results_are_read(
    tmp_path: pathlib.Path,
) -> None:
    """Stored by hand with the index in place: one assignment confirmed for two results, and a
    result with one confirmed row and two kept-apart rows. The read returns all five."""
    path = tmp_path / "blossom.sqlite3"
    student, results = with_a_report(path)
    by_hand(
        path,
        confirmed(student, results[0], "assignment-one"),
        confirmed(student, results[1], "assignment-one"),
        kept_apart(student, results[0], "assignment-two"),
        kept_apart(student, results[0], "assignment-three"),
        kept_apart(student, results[1], "assignment-two"),
    )

    store = ProjectStateStore.open(path, fixture_clock())
    links = store.homework_links()
    store.close()

    assert len(links) == 5
    assert sorted(link.result_id for link in links if link.state == "confirmed") == sorted(
        results[:2]
    )


@pytest.mark.parametrize("rows", [2, 3])
@pytest.mark.parametrize("on_record", [True, False], ids=["on record", "on no record"])
def test_the_read_refuses_confirmed_rows_that_name_one_result(
    rows: int, on_record: bool, tmp_path: pathlib.Path
) -> None:
    """The index is dropped by hand and two or three confirmed rows are stored for one result
    ID, on record or not, beside a row for another. The read raises the conflict error, with
    nothing chained."""
    path = tmp_path / "blossom.sqlite3"
    student, results = with_a_report(path)
    named = results[0] if on_record else "result-on-no-record"
    by_hand(
        path,
        (DROP_INDEX, ()),
        confirmed(student, results[1], "assignment-other"),
        *(confirmed(student, named, f"assignment-{at}") for at in range(rows)),
    )

    store = ProjectStateStore.open(path, fixture_clock())
    with pytest.raises(GradeValueUndecodable) as refusal:
        store.homework_links()
    store.close()

    assert type(refusal.value) is HomeworkLinksConflict
    assert refusal.value.args == ("two confirmed homework links name one result",)
    assert refusal.value.__cause__ is None
    assert refusal.value.__context__ is None


@pytest.mark.parametrize("column", ["result_id", "assignment_id", "state"])
@pytest.mark.parametrize("conflict", [False, True], ids=["alone", "beside a conflict"])
def test_a_link_value_stored_as_bytes_stops_the_read_before_any_count(
    column: str, conflict: bool, tmp_path: pathlib.Path
) -> None:
    """One value of one row is rewritten by hand as bytes, alone and beside two confirmed rows
    for one result: the read raises the decoding error itself, never the conflict error."""
    path = tmp_path / "blossom.sqlite3"
    student, results = with_a_report(path)
    by_hand(
        path,
        (DROP_INDEX, ()),
        confirmed(student, results[0], "assignment-one"),
        (confirmed if conflict else kept_apart)(student, results[0], "assignment-two"),
        kept_apart(student, results[1], "assignment-bytes"),
        (
            f"UPDATE grade_homework_links SET {column} = CAST({column} AS BLOB) "  # noqa: S608
            "WHERE assignment_id = 'assignment-bytes'",
            (),
        ),
        unchecked=True,
    )

    store = ProjectStateStore.open(path, fixture_clock())
    with pytest.raises(GradeValueUndecodable) as refusal:
        store.homework_links()
    store.close()

    assert type(refusal.value) is GradeValueUndecodable


def test_text_that_is_not_utf_8_stops_a_strict_read_of_her_links(tmp_path: pathlib.Path) -> None:
    """One assignment ID is rewritten by hand as text holding a byte that is no UTF-8: read
    inside the store's strict decoding, her links raise the decoding error."""
    path = tmp_path / "blossom.sqlite3"
    student, results = with_a_report(path)
    by_hand(
        path,
        confirmed(student, results[0], "assignment-one"),
        ("UPDATE grade_homework_links SET assignment_id = CAST(X'ff' AS TEXT)", ()),
    )

    store = ProjectStateStore.open(path, fixture_clock())
    with pytest.raises(GradeValueUndecodable) as refusal, store.strict_text():
        store.homework_links()
    store.close()

    assert type(refusal.value) is GradeValueUndecodable


# ------------------------------------------------------------- the write that decides a link

BIOLOGY = ("assignment-one", "assignment-two", "assignment-three")
"""Three homework rows whose class name is her report's class."""
ART = "assignment-art"
"""Homework whose class name connects to no class of hers."""
NO_RESULT = "result-" + "0" * 32
DECISIONS: tuple[LinkDecision, ...] = ("link", "apart", "automatic")
YEAR = "2026-2027"
SECOND_TERM = draft_of(
    (FIXTURES / "grade_report.md").read_text(encoding="utf-8").replace("**T1**", "**T2**")
)
EARLIER: dict[str, tuple[LinkDecision, ...]] = {
    "no decision": (),
    "confirmed": ("link",),
    "kept apart": ("apart",),
}
"""The decisions that leave a pair in each state, made through the write."""


def homework_put(store: ProjectStateStore) -> None:
    rows = [homework_named("Biology", name) for name in BIOLOGY]
    store.put_on_record([*rows, homework_named("Art", ART)], {})


def linking(path: pathlib.Path) -> tuple[ProjectStateStore, list[str]]:
    """A file with Wren's report saved and the homework rows on record, open: the store and
    her result IDs."""
    _, results = with_a_report(path)
    store = ProjectStateStore.open(path, fixture_clock())
    homework_put(store)
    return store, results


def revision_of(store: ProjectStateStore, result: str) -> int:
    """The revision of the result's class and term, 0 for a result on no record."""
    held = store._connection.execute(
        "SELECT revision FROM grade_scope_revisions WHERE (class_id, term_label) IN "
        "(SELECT class_id, term_label FROM grade_results WHERE result_id = ?)",
        (result,),
    ).fetchone()
    return 0 if held is None else int(held[0])


def decide(
    store: ProjectStateStore,
    result: str,
    assignment: str,
    decision: LinkDecision,
    *,
    behind: int = 0,
) -> HomeworkLinkOutcome:
    """A parent's decision sent with the class and term's revision, or one ``behind`` it."""
    return store.decide_homework_link(
        result,
        assignment,
        decision=decision,
        revision=revision_of(store, result) - behind,
        role="parent",
    )


def states(store: ProjectStateStore) -> dict[tuple[str, str], str]:
    """Each link row's state by its result and assignment, read by the test's own SQL."""
    rows = store._connection.execute(
        "SELECT result_id, assignment_id, state FROM grade_homework_links"
    )
    return {(str(result), str(assignment)): str(state) for result, assignment, state in rows}


def whole_file(path: pathlib.Path) -> dict[str, object]:
    """Every table, index and row of the file, the catalog's text of each index included."""
    return closed_world([path], leaving_out=())


def all_but_links(path: pathlib.Path) -> dict[str, object]:
    """The file but the two tables a link write may change."""
    return closed_world([path], leaving_out=("grade_homework_links", "grade_scope_revisions"))


def every_press(results: list[str]) -> list[tuple[str, str, LinkDecision]]:
    """Each decision on each pair of her first two results and the Biology homework, and each
    decision on a result that is on no record."""
    on_record = list(itertools.product(results[:2], BIOLOGY, DECISIONS))
    return [*on_record, *((NO_RESULT, BIOLOGY[0], decision) for decision in DECISIONS)]


def answers(store: ProjectStateStore, results: list[str]) -> set[HomeworkLinkOutcome]:
    """What every press is answered, each sent with its class and term's revision."""
    return {decide(store, *press) for press in every_press(results)}


@pytest.mark.parametrize(
    ("earlier", "decision", "leaves"),
    [
        ("no decision", "link", "confirmed"),
        ("no decision", "apart", "rejected"),
        ("kept apart", "link", "confirmed"),
        ("kept apart", "automatic", None),
        ("confirmed", "apart", "rejected"),
    ],
)
def test_a_decision_that_changes_the_record_is_written_and_raises_the_revision(
    earlier: str, decision: LinkDecision, leaves: str | None, tmp_path: pathlib.Path
) -> None:
    """From each state, made through the write, the decision leaves the pair as expected,
    raises its class and term's revision by one, and changes no other table of the file."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    pair = (results[0], BIOLOGY[0])
    for one in EARLIER[earlier]:
        assert decide(store, *pair, one) == HomeworkLinkWritten()
    revision, others = revision_of(store, results[0]), all_but_links(path)

    outcome = decide(store, *pair, decision)
    after = (states(store).get(pair), revision_of(store, results[0]), all_but_links(path))
    store.close()

    assert outcome == HomeworkLinkWritten()
    assert after == (leaves, revision + 1, others)


@pytest.mark.parametrize(
    ("earlier", "decision"),
    [("confirmed", "link"), ("kept apart", "apart"), ("no decision", "automatic")],
)
@pytest.mark.parametrize("behind", [0, 1], ids=["the revision", "a revision behind"])
def test_a_decision_already_on_record_stands_whatever_revision_it_carries(
    earlier: str, decision: LinkDecision, behind: int, tmp_path: pathlib.Path
) -> None:
    """A decision the record already holds, sent again with the revision or one behind it,
    stands: nothing in the file changes, the revision included."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    pair = (results[0], BIOLOGY[0])
    for one in EARLIER[earlier]:
        assert decide(store, *pair, one) == HomeworkLinkWritten()
    assert decide(store, results[1], BIOLOGY[1], "link") == HomeworkLinkWritten()
    before = whole_file(path)

    outcome = decide(store, *pair, decision, behind=behind)
    store.close()

    assert outcome == HomeworkLinkStood()
    assert whole_file(path) == before


@pytest.mark.parametrize(
    ("result", "assignment", "decision"),
    [
        (0, 1, "link"),
        (0, 2, "apart"),
        (0, 0, "apart"),
        (1, 1, "automatic"),
        (1, 2, "link"),
    ],
    ids=[
        "a link to other homework",
        "a pair with no decision kept apart",
        "the link removed",
        "a pair kept apart left with no decision",
        "another result of the class and term linked",
    ],
)
def test_a_change_sent_with_a_revision_behind_is_refused_and_writes_nothing(
    result: int, assignment: int, decision: LinkDecision, tmp_path: pathlib.Path
) -> None:
    """With a link and a pair kept apart on record, each change that doesn't stand already,
    sent one revision behind its class and term's, is answered as changed; the file is as it
    was."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    assert decide(store, results[0], BIOLOGY[0], "link") == HomeworkLinkWritten()
    assert decide(store, results[1], BIOLOGY[1], "apart") == HomeworkLinkWritten()
    before = whole_file(path)

    outcome = decide(store, results[result], BIOLOGY[assignment], decision, behind=1)
    store.close()

    assert outcome == HomeworkLinkChanged()
    assert whole_file(path) == before


def test_a_class_and_term_with_no_revision_row_takes_no_change(tmp_path: pathlib.Path) -> None:
    """The revision rows are deleted by hand. A link sent with any revision is then answered as
    changed, and no link row is written."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    with store._lock, store._connection:
        store._connection.execute("DELETE FROM grade_scope_revisions")

    outcomes = {
        store.decide_homework_link(
            results[0], BIOLOGY[0], decision="link", revision=revision, role="parent"
        )
        for revision in (0, 1)
    }
    written = states(store)
    store.close()

    assert outcomes == {HomeworkLinkChanged()}
    assert written == {}


def test_a_link_is_not_left_with_no_decision_in_one_step(tmp_path: pathlib.Path) -> None:
    """A confirmed pair sent "automatic" at the current revision is answered as changed, and
    the file is as it was."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    assert decide(store, results[0], BIOLOGY[0], "link") == HomeworkLinkWritten()
    before = whole_file(path)

    outcome = decide(store, results[0], BIOLOGY[0], "automatic")
    store.close()

    assert outcome == HomeworkLinkChanged()
    assert whole_file(path) == before


@pytest.mark.parametrize("second", ["no decision", "kept apart"])
def test_a_link_to_other_homework_sets_the_first_pair_apart_in_the_same_write(
    second: str, tmp_path: pathlib.Path
) -> None:
    """A result linked to one homework is linked to another, a pair with no decision or one
    kept apart. With the index in place the file doesn't refuse it: the first pair is set
    apart, the second confirmed, and the revision rises once."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    first, other = (results[0], BIOLOGY[0]), (results[0], BIOLOGY[1])
    assert decide(store, *first, "link") == HomeworkLinkWritten()
    for one in EARLIER[second]:
        assert decide(store, *other, one) == HomeworkLinkWritten()
    revision = revision_of(store, results[0])

    outcome = decide(store, *other, "link")
    after = (states(store), revision_of(store, results[0]))
    store.close()

    assert index_stored(path) == [(DEFINITION_AS_STORED,)]
    assert outcome == HomeworkLinkWritten()
    assert after == ({first: "rejected", other: "confirmed"}, revision + 1)


def test_a_written_link_carries_who_answered_and_when(tmp_path: pathlib.Path) -> None:
    """A link by the household, then a link to other homework by a parent: each row the second
    write touches carries that write's role, and both carry the clock's moment in UTC."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    student = store.student_id()
    moment = fixture_clock().now().astimezone(UTC).isoformat()

    store.decide_homework_link(
        results[0],
        BIOLOGY[0],
        decision="link",
        revision=revision_of(store, results[0]),
        role="household",
    )
    by_the_household = link_rows_of(store)
    assert decide(store, results[0], BIOLOGY[1], "link") == HomeworkLinkWritten()
    by_a_parent = link_rows_of(store)
    store.close()

    assert by_the_household == [(student, results[0], BIOLOGY[0], "confirmed", "household", moment)]
    assert by_a_parent == [
        (student, results[0], BIOLOGY[0], "rejected", "parent", moment),
        (student, results[0], BIOLOGY[1], "confirmed", "parent", moment),
    ]


def link_rows_of(store: ProjectStateStore) -> list[tuple[object, ...]]:
    """Every link row of an open store, in the file's order, read by the test's own SQL."""
    return store._connection.execute("SELECT * FROM grade_homework_links ORDER BY rowid").fetchall()


def test_a_link_needs_the_homework_s_class_name_connected_to_the_result_s_class(
    tmp_path: pathlib.Path,
) -> None:
    """Homework of a class name that is no class of hers can't be linked and can be kept apart.
    Once a parent removes the Biology name's connection, Biology homework can't be linked."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    before = whole_file(path)

    unconnected = decide(store, results[0], ART, "link")
    untouched = whole_file(path) == before
    apart = decide(store, results[0], ART, "apart")
    removed = store.connect_homework_class(
        YEAR, "Biology", shown=None, chosen=UNCONNECTED, role="parent"
    )
    after_removal = decide(store, results[0], BIOLOGY[0], "link")
    written = states(store)
    store.close()

    assert (unconnected, untouched) == (HomeworkLinkClassNameChanged(), True)
    assert apart == HomeworkLinkWritten()
    assert removed == HomeworkClassRemoved()
    assert after_removal == HomeworkLinkClassNameChanged()
    assert written == {(results[0], ART): "rejected"}


def test_homework_on_no_record_is_neither_linked_nor_kept_apart(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    before = whole_file(path)

    outcomes = [decide(store, results[0], "assignment-on-no-record", one) for one in DECISIONS]
    store.close()

    assert outcomes == [
        HomeworkLinkHomeworkNotOnRecord(),
        HomeworkLinkHomeworkNotOnRecord(),
        HomeworkLinkStood(),
    ]
    assert whole_file(path) == before


def test_a_link_to_homework_that_left_the_record_is_removed_and_left_with_no_decision(
    tmp_path: pathlib.Path,
) -> None:
    """A linked homework's row is deleted by hand, as no write of Blossom's deletes one. The
    link still stands when sent again, is removed, can't be made again, and its pair is then
    left with no decision."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    pair = (results[0], BIOLOGY[0])
    assert decide(store, *pair, "link") == HomeworkLinkWritten()
    with store._lock, store._connection:
        store._connection.execute("DELETE FROM assignments WHERE assignment_id = ?", (BIOLOGY[0],))

    sent: tuple[LinkDecision, ...] = ("link", "apart", "link", "automatic")
    outcomes = [decide(store, *pair, one) for one in sent]
    written = states(store)
    store.close()

    assert outcomes == [
        HomeworkLinkStood(),
        HomeworkLinkWritten(),
        HomeworkLinkHomeworkNotOnRecord(),
        HomeworkLinkWritten(),
    ]
    assert written == {}


def test_a_result_on_no_record_or_with_no_current_value_takes_no_decision(
    tmp_path: pathlib.Path,
) -> None:
    """A result ID on no record, and then her own result once every report's use is set to
    earlier by hand so that it holds no current value: each decision is answered that the
    result isn't on record, and no link row is written."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)

    unknown = {decide(store, NO_RESULT, BIOLOGY[0], one) for one in DECISIONS}
    with store._lock, store._connection:
        store._connection.execute("UPDATE grade_reports SET use = 'earlier'")
    valueless = {decide(store, results[0], BIOLOGY[0], one) for one in DECISIONS}
    written = states(store)
    store.close()

    assert unknown == {HomeworkLinkResultNotOnRecord()}
    assert valueless == {HomeworkLinkResultNotOnRecord()}
    assert written == {}


@pytest.mark.parametrize("index", ["in place", "dropped by hand"])
def test_a_role_that_may_not_change_grade_records_is_refused_before_any_statement(
    index: str, tmp_path: pathlib.Path
) -> None:
    """With the index in place and with it dropped by hand, a decision sent as the student is
    refused as the store's other grade writes refuse that role, and the write asks nothing of
    the file. A decision of no known kind is refused the same way."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    if index == "dropped by hand":
        by_hand(path, (DROP_INDEX, ()))
    asked: list[str] = []
    store._connection.set_trace_callback(asked.append)

    with pytest.raises(ValueError, match="changes grade records"):
        store.decide_homework_link(
            results[0],
            BIOLOGY[0],
            decision="link",
            revision=1,
            role="student",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="link, apart or automatic"):
        store.decide_homework_link(
            results[0],
            BIOLOGY[0],
            decision="confirmed",  # type: ignore[arg-type]
            revision=1,
            role="parent",
        )
    store._connection.set_trace_callback(None)
    written = states(store)
    store.close()

    assert asked == []
    assert written == {}


def test_decisions_made_from_one_page_land_one_after_the_other(tmp_path: pathlib.Path) -> None:
    """Five presses all carry the revision one page showed. The first is written, the same
    decision again stands, and a different decision on the pair, another pair of the result,
    and another result of the class and term are each answered as changed."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    page = revision_of(store, results[0])
    presses: list[tuple[str, str, LinkDecision]] = [
        (results[0], BIOLOGY[0], "link"),
        (results[0], BIOLOGY[0], "link"),
        (results[0], BIOLOGY[0], "apart"),
        (results[0], BIOLOGY[1], "link"),
        (results[1], BIOLOGY[1], "link"),
    ]

    outcomes = [
        store.decide_homework_link(
            result, assignment, decision=decision, revision=page, role="parent"
        )
        for result, assignment, decision in presses
    ]
    written = states(store)
    store.close()

    assert outcomes == [
        HomeworkLinkWritten(),
        HomeworkLinkStood(),
        HomeworkLinkChanged(),
        HomeworkLinkChanged(),
        HomeworkLinkChanged(),
    ]
    assert written == {(results[0], BIOLOGY[0]): "confirmed"}


def test_results_of_two_terms_are_linked_to_one_homework_from_pages_of_the_same_time(
    tmp_path: pathlib.Path,
) -> None:
    """A result of each of two terms is linked to the same homework, each with the revision its
    own term had before either write. Both are written, and the read returns both links."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    assert isinstance(save_grade(store, SECOND_TERM, key=KEY), GradeReportSaved)
    (later,) = store._connection.execute(
        "SELECT result_id FROM grade_results WHERE term_label = 'T2' ORDER BY rowid LIMIT 1"
    ).fetchone()
    pages = {result: revision_of(store, result) for result in (results[0], str(later))}

    outcomes = [
        store.decide_homework_link(
            result, BIOLOGY[0], decision="link", revision=page, role="parent"
        )
        for result, page in pages.items()
    ]
    links = store.homework_links()
    store.close()

    assert outcomes == [HomeworkLinkWritten(), HomeworkLinkWritten()]
    assert sorted(links, key=lambda link: link.result_id) == sorted(
        (HomeworkLink(result, BIOLOGY[0], "confirmed") for result in pages),
        key=lambda link: link.result_id,
    )


def test_two_changes_leave_one_link_and_two_pairs_kept_apart_which_the_read_returns(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)

    outcomes = [decide(store, results[0], homework, "link") for homework in BIOLOGY]
    links = store.homework_links()
    store.close()

    assert outcomes == [HomeworkLinkWritten()] * 3
    assert {(link.assignment_id, link.state) for link in links} == {
        (BIOLOGY[0], "rejected"),
        (BIOLOGY[1], "rejected"),
        (BIOLOGY[2], "confirmed"),
    }


def test_no_sequence_of_decisions_is_refused_by_the_file_or_confirms_a_result_twice(
    tmp_path: pathlib.Path,
) -> None:
    """Three hundred decisions drawn from a fixed seed, over two results and three homework
    rows, most with the current revision. None raises, and after each the file holds at most
    one confirmed row for a result."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    drawn = random.Random(20261012)  # noqa: S311
    seen: set[HomeworkLinkOutcome] = set()
    most = 0

    for _ in range(300):
        press = (drawn.choice(results[:2]), drawn.choice(BIOLOGY), drawn.choice(DECISIONS))
        seen.add(decide(store, *press, behind=int(drawn.random() < 0.1)))
        (twice,) = store._connection.execute(
            "SELECT COALESCE(MAX(held), 0) FROM (SELECT COUNT(*) AS held "
            "FROM grade_homework_links WHERE state = 'confirmed' GROUP BY result_id)"
        ).fetchone()
        most = max(most, int(twice))
    store.close()

    assert seen == {HomeworkLinkWritten(), HomeworkLinkStood(), HomeworkLinkChanged()}
    assert most == 1


def test_a_delete_removes_links_the_write_made_and_a_new_import_starts_with_none(
    tmp_path: pathlib.Path,
) -> None:
    """A link and a pair kept apart, made through the write, are counted by the delete's
    preview and removed by the delete. The old result then takes no decision, and the report
    imported again has new result IDs and no link row."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    assert decide(store, results[0], BIOLOGY[0], "link") == HomeworkLinkWritten()
    assert decide(store, results[1], BIOLOGY[1], "apart") == HomeworkLinkWritten()
    class_id = capture_class(store, WREN)
    preview = store.delete_preview(class_id, "T1")
    assert isinstance(preview, DeletePreview)

    deleted = store.delete_class_term(class_id, "T1", revision=preview.revision, role="parent")
    left = states(store)
    gone = decide(store, results[0], BIOLOGY[0], "link")
    again = save_grade(store, WREN, key=KEY)
    new = [
        str(result_id)
        for (result_id,) in store._connection.execute("SELECT result_id FROM grade_results")
    ]
    after_import = states(store)
    store.close()

    assert (preview.links, preview.kept_apart) == (1, 1)
    assert isinstance(deleted, ClassTermDeleted)
    assert left == {}
    assert gone == HomeworkLinkResultNotOnRecord()
    assert isinstance(again, GradeReportSaved)
    assert new
    assert not set(new) & set(results)
    assert after_import == {}


def test_the_same_report_saved_again_keeps_each_result_s_id_and_its_link(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    assert decide(store, results[0], BIOLOGY[0], "link") == HomeworkLinkWritten()
    rows = link_rows_of(store)

    save_grade(store, WREN, key=KEY)
    kept = [
        str(result_id)
        for (result_id,) in store._connection.execute(
            "SELECT result_id FROM grade_results ORDER BY rowid"
        )
    ]
    after = link_rows_of(store)
    store.close()

    assert kept == results
    assert after == rows


# ------------------------------------------------------------- the write and the index


def test_with_the_index_a_start_made_a_write_is_decided_by_its_other_checks(
    tmp_path: pathlib.Path,
) -> None:
    """On a file the application made, whose catalog holds the definition whole: a link is
    written, the same link sent a revision behind stands, and a change sent a revision behind
    is answered as changed."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    stored = index_stored(path)

    outcomes = [
        decide(store, results[0], BIOLOGY[0], "link"),
        decide(store, results[0], BIOLOGY[0], "link", behind=1),
        decide(store, results[0], BIOLOGY[1], "link", behind=1),
        decide(store, results[1], BIOLOGY[1], "apart", behind=1),
    ]
    store.close()

    assert stored == [(DEFINITION_AS_STORED,)]
    assert outcomes == [
        HomeworkLinkWritten(),
        HomeworkLinkStood(),
        HomeworkLinkChanged(),
        HomeworkLinkChanged(),
    ]


def test_the_index_is_read_first_in_a_link_write_and_is_one_statement_of_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The statements one press asks are traced twice: as built, and with the store's check of
    the index replaced by the test with one that asks nothing. As built there is one more, the
    catalog's, and only the transaction's own opening comes before it."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    asked: list[str] = []
    store._connection.set_trace_callback(asked.append)

    decide(store, results[0], BIOLOGY[0], "automatic")
    built = [statement for statement in asked if "grade_scope_revisions WHERE (" not in statement]
    asked.clear()
    monkeypatch.setattr(ProjectStateStore, "_homework_links_index_available", lambda _: True)
    decide(store, results[0], BIOLOGY[0], "automatic")
    replaced = [
        statement for statement in asked if "grade_scope_revisions WHERE (" not in statement
    ]
    store._connection.set_trace_callback(None)
    store.close()

    catalog = [statement for statement in built if "sqlite_master" in statement]
    before_it = built[: built.index(catalog[0])]
    assert len(catalog) == 1
    assert [statement for statement in built if statement not in catalog] == replaced
    assert [statement.split()[0] for statement in before_it] == ["BEGIN", "SAVEPOINT"]


WRONG_INDEXES = {
    "not unique": (
        "CREATE INDEX grade_homework_links_one_homework\n"
        "ON grade_homework_links (student_id, result_id)\n"
        "WHERE state = 'confirmed'"
    ),
    "other columns": (
        "CREATE UNIQUE INDEX grade_homework_links_one_homework\n"
        "ON grade_homework_links (student_id, assignment_id)\n"
        "WHERE state = 'confirmed'"
    ),
    "another predicate": (
        "CREATE UNIQUE INDEX grade_homework_links_one_homework\n"
        "ON grade_homework_links (student_id, result_id)\n"
        "WHERE state = 'rejected'"
    ),
    "no predicate": (
        "CREATE UNIQUE INDEX grade_homework_links_one_homework\n"
        "ON grade_homework_links (student_id, result_id)"
    ),
    "the same meaning in another spelling": (
        "create unique index grade_homework_links_one_homework "
        "on grade_homework_links(student_id,result_id) where state='confirmed'"
    ),
}
"""Indexes of the required name that are not the required one, each made by a test's SQL."""


def with_decisions(path: pathlib.Path) -> tuple[ProjectStateStore, list[str]]:
    """The open store with a link for her first result and a pair of her second kept apart,
    both made through the write."""
    store, results = linking(path)
    assert decide(store, results[0], BIOLOGY[0], "link") == HomeworkLinkWritten()
    assert decide(store, results[1], BIOLOGY[1], "apart") == HomeworkLinkWritten()
    return store, results


def test_no_decision_is_written_while_the_index_is_missing(tmp_path: pathlib.Path) -> None:
    """The index is dropped through the test's SQL while the store is open. Every decision is
    then answered that the required index is unavailable, one that would stand and one for a
    result on no record included, and the file stays as the test left it, with no index."""
    path = tmp_path / "blossom.sqlite3"
    store, results = with_decisions(path)
    by_hand(path, (DROP_INDEX, ()))
    before = whole_file(path)

    outcomes = answers(store, results)
    store.close()

    assert outcomes == {RequiredIndexUnavailable()}
    assert whole_file(path) == before
    assert index_stored(path) == []


@pytest.mark.parametrize("wrong", WRONG_INDEXES)
def test_no_decision_is_written_while_an_index_of_that_name_is_defined_otherwise(
    wrong: str, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The test's SQL drops the index and makes another of its name. Every decision is then
    answered that the index is unavailable and the file stays as it was, also once the store
    is opened again, logging nothing. After the test drops it, the next open makes the index."""
    path = tmp_path / "blossom.sqlite3"
    store, results = with_decisions(path)
    by_hand(path, (DROP_INDEX, ()), (WRONG_INDEXES[wrong], ()))
    before = whole_file(path)

    first = answers(store, results)
    store.close()
    unchanged = whole_file(path) == before
    with caplog.at_level(logging.DEBUG, logger=STARTS):
        store = ProjectStateStore.open(path, fixture_clock())
    second = answers(store, results)
    store.close()
    restarted = (whole_file(path) == before, index_stored(path), start_records(caplog))
    by_hand(path, (DROP_INDEX, ()))
    store = ProjectStateStore.open(path, fixture_clock())
    written = decide(store, results[0], BIOLOGY[1], "link")
    store.close()

    assert index_stored(path) == [(DEFINITION_AS_STORED,)]
    assert (first, unchanged) == ({RequiredIndexUnavailable()}, True)
    assert second == {RequiredIndexUnavailable()}
    assert restarted[0]
    assert restarted[1] != [(DEFINITION_AS_STORED,)]
    assert len(restarted[1]) == 1
    assert restarted[2] == []
    assert written == HomeworkLinkWritten()


def test_only_the_catalog_decides_whether_a_link_is_written(tmp_path: pathlib.Path) -> None:
    """The test's SQL drops the index while the store is open, and a link is refused. The
    test's SQL then makes the index again with the start's statement, with no restart, and the
    same press is written. The file gains no table for it."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    tables = tables_in(path)
    by_hand(path, (DROP_INDEX, ()))
    refused = decide(store, results[0], BIOLOGY[0], "link")
    by_hand(path, (CREATE_HOMEWORK_LINKS_INDEX, ()))

    written = decide(store, results[0], BIOLOGY[0], "link")
    store.close()

    assert refused == RequiredIndexUnavailable()
    assert written == HomeworkLinkWritten()
    assert tables_in(path) == tables


def test_no_decision_is_written_from_a_start_without_the_index_until_a_start_makes_it(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """On the file changed by hand (the index dropped, two confirmed rows for one result), every
    decision is answered that the index is unavailable after a start, after a second, and after
    a row is set to rejected by hand. The next start makes the index, and a link is written."""
    path = tmp_path / "blossom.sqlite3"
    _, results = damaged(path)
    store = ProjectStateStore.open(path, fixture_clock())
    homework_put(store)
    rows = whole_file(path)

    first = answers(store, results)
    store.close()
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=STARTS):
        store = ProjectStateStore.open(path, fixture_clock())
    second = (answers(store, results), start_records(caplog), whole_file(path) == rows)
    by_hand(
        path,
        (
            "UPDATE grade_homework_links SET state = 'rejected' WHERE assignment_id = ?",
            (BIOLOGY[1],),
        ),
    )
    read = store.homework_links()
    repaired = (answers(store, results), index_stored(path))
    store.close()
    store = ProjectStateStore.open(path, fixture_clock())
    made = index_stored(path)
    decided = [
        decide(store, results[0], BIOLOGY[2], "link"),
        decide(store, results[0], BIOLOGY[2], "link", behind=1),
        decide(store, results[1], BIOLOGY[0], "apart", behind=1),
    ]
    after = states(store)
    store.close()

    assert first == {RequiredIndexUnavailable()}
    assert second == ({RequiredIndexUnavailable()}, [("WARNING", NOT_MADE)], True)
    assert len(read) == 2
    assert repaired == ({RequiredIndexUnavailable()}, [])
    assert made == [(DEFINITION_AS_STORED,)]
    assert decided == [HomeworkLinkWritten(), HomeworkLinkStood(), HomeworkLinkChanged()]
    assert after == {
        (results[0], BIOLOGY[0]): "rejected",
        (results[0], BIOLOGY[1]): "rejected",
        (results[0], BIOLOGY[2]): "confirmed",
    }


def test_no_decision_is_written_while_another_student_id_s_rows_keep_the_index_out(
    tmp_path: pathlib.Path,
) -> None:
    """The index is dropped by hand and two confirmed rows for one result stored under another
    student ID. After the start her read returns no link and every decision is answered that
    the index is unavailable; with those rows deleted by the test, the next start makes it."""
    path = tmp_path / "blossom.sqlite3"
    _, results = with_a_report(path)
    by_hand(
        path,
        (DROP_INDEX, ()),
        confirmed(SOMEONE_ELSE, results[0], BIOLOGY[0]),
        confirmed(SOMEONE_ELSE, results[0], BIOLOGY[1]),
    )
    store = ProjectStateStore.open(path, fixture_clock())
    homework_put(store)
    before = whole_file(path)

    read = store.homework_links()
    refused = answers(store, results)
    store.close()
    unchanged = whole_file(path) == before
    by_hand(path, ("DELETE FROM grade_homework_links WHERE student_id = ?", (SOMEONE_ELSE,)))
    store = ProjectStateStore.open(path, fixture_clock())
    written = decide(store, results[0], BIOLOGY[0], "link")
    store.close()

    assert read == ()
    assert (refused, unchanged) == ({RequiredIndexUnavailable()}, True)
    assert index_stored(path) == [(DEFINITION_AS_STORED,)]
    assert written == HomeworkLinkWritten()


def test_the_write_refuses_two_confirmed_rows_for_one_result_once_its_index_check_is_replaced(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is changed by hand and the store's index check is replaced by the test to answer
    yes. Each decision on a result on record then raises the conflict error, nothing chained,
    and writes nothing. This shows the write's refusal, not that a write stores such rows."""
    path = tmp_path / "blossom.sqlite3"
    _, results = damaged(path)
    store = ProjectStateStore.open(path, fixture_clock())
    homework_put(store)
    before = whole_file(path)
    monkeypatch.setattr(ProjectStateStore, "_homework_links_index_available", lambda _: True)
    raised = []
    answered = set()

    for result, assignment, decision in every_press(results):
        if result == NO_RESULT:
            answered.add(decide(store, result, assignment, decision))
            continue
        with pytest.raises(GradeValueUndecodable) as refusal:
            decide(store, result, assignment, decision)
        raised.append((type(refusal.value), refusal.value.__cause__, refusal.value.__context__))
    open_transaction = store._connection.in_transaction
    store.close()

    assert len(raised) == 18
    assert set(raised) == {(HomeworkLinksConflict, None, None)}
    assert answered == {HomeworkLinkResultNotOnRecord()}
    assert not open_transaction
    assert whole_file(path) == before


def test_a_link_to_other_homework_sets_the_first_pair_apart_first_on_a_file_with_no_index(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index is dropped by hand and the store's check of it is replaced by the test with
    one that answers yes. A linked result is then linked to other homework: the write updates
    the first pair before it inserts the second, and one confirmed row is left."""
    path = tmp_path / "blossom.sqlite3"
    store, results = linking(path)
    by_hand(path, (DROP_INDEX, ()))
    monkeypatch.setattr(ProjectStateStore, "_homework_links_index_available", lambda _: True)
    assert decide(store, results[0], BIOLOGY[0], "link") == HomeworkLinkWritten()
    asked: list[str] = []
    store._connection.set_trace_callback(asked.append)

    outcome = decide(store, results[0], BIOLOGY[1], "link")
    store._connection.set_trace_callback(None)
    written = states(store)
    store.close()

    wrote = [
        statement.split()[0]
        for statement in asked
        if "grade_homework_links" in statement and not statement.startswith("SELECT")
    ]
    assert outcome == HomeworkLinkWritten()
    assert wrote == ["UPDATE", "INSERT"]
    assert written == {(results[0], BIOLOGY[0]): "rejected", (results[0], BIOLOGY[1]): "confirmed"}
    assert index_stored(path) == []
