# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The record of a parent's homework links: its table, its one index, what a start does about
that index, and the read of her links.

No store method writes a link here, so every link row in these tests is stored by the test's
own SQL, and each test says what it stored. Such a test shows what the file, a start and the
read then do. It does not show which rows a write of Blossom's makes. The files are synthetic:
Wren's fixture report, saved through the gradebook's own save.
"""

import logging
import pathlib
import sqlite3
from collections.abc import Callable

import pytest

from blossom.grades.draft import GradeReportDraft
from blossom.grades.identity import name_form_key
from blossom.grades.review import GradeReportSaved, GradeValueUndecodable, HomeworkLinksConflict
from blossom.grades.text_reader import read_grade_report
from blossom.stores.gradebook import (
    CREATE_HOMEWORK_LINKS_INDEX,
    GRADEBOOK_TABLES,
    HOMEWORK_LINKS_INDEX_DEFINITION,
    VIEW_TABLES,
    HomeworkLink,
)
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    FIXTURES,
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
