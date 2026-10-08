# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A parent's delete of one class's grades for one term, and the class and term every
acceptance carries so the delete finds exactly its own.

The delete is one revision-checked grade write: it removes the class and term's reports,
observations, row records, results, acceptances and class-details actions, one statement per
table, keeps her identity, years, terms, classes and aliases, and raises the revision. Its retry
reads the class and term as they are: already deleted when they hold nothing, or a fresh
preview, deleting nothing, after a newer import.

A file whose acceptance table has no class and term gets them at its first start: each row is
tied exactly to its report's class and term, or its capture's when it names no report, and a
homework screenshot's stays empty. A row nothing ties stops the start, every gradebook table
left as it was, and nothing is ever dropped or guessed. The files here are synthetic: rows a save
writes, reshaped into the table without the two columns, then changed by hand for each case.
"""

import pathlib
import sqlite3

import pytest

from blossom.grades.draft import GradeReportDraft, capture_key
from blossom.grades.identity import name_form_key
from blossom.grades.projection import CurrentPage, MadeCurrent, ReportNotSaved
from blossom.grades.review import GradeReportSaved, ReturnReason, ReviewReturned
from blossom.grades.text_reader import read_grade_report
from blossom.stores.gradebook import (
    GRADEBOOK_TABLES,
    AcceptancesNotTied,
    AlreadyDeleted,
    ClassTermDeleted,
    DeleteOutcome,
    DeletePreview,
    DeleteReturned,
    NothingToDelete,
)
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    FIXTURES,
    as_stored,
    capture_class,
    closed_world,
    confirm_current,
    current_preview,
    fixture_clock,
    save_grade,
)

REPORT = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")
KEY = name_form_key(b"5" * 64)


def draft_of(text: str) -> GradeReportDraft:
    reading = read_grade_report(text)
    assert reading.draft is not None, reading.not_read
    return reading.draft


WREN = draft_of(REPORT)
SECOND_TERM = draft_of(REPORT.replace("**T1**", "**T2**"))

UNTIED_ACCEPTANCES = """CREATE TABLE grade_acceptances (
    acceptance_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    kind TEXT NOT NULL
        CHECK (kind IN ('grade_text', 'grade_screenshot', 'homework_screenshot')),
    source_key TEXT NOT NULL,
    report_id TEXT,
    accepted TEXT NOT NULL,
    identity_status TEXT NOT NULL,
    identity_answer TEXT NOT NULL,
    identity_form TEXT,
    added INTEGER NOT NULL,
    updated INTEGER NOT NULL,
    already_saved INTEGER NOT NULL,
    left_to_check INTEGER NOT NULL,
    shown INTEGER NOT NULL CHECK (shown >= 0),
    answers_kept INTEGER NOT NULL CHECK (answers_kept >= 0),
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
    accepted_at TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('parent', 'household'))
)"""
"""The acceptance table of a file made before acceptances carried their class and term."""
UNTIED_COLUMNS = (
    "acceptance_id, student_id, kind, source_key, report_id, accepted, identity_status, "
    "identity_answer, identity_form, added, updated, already_saved, left_to_check, shown, "
    "answers_kept, complete, accepted_at, role"
)
HAND_MADE = (
    "INSERT INTO grade_acceptances SELECT ?, student_id, ?, ?, ?, '[]', identity_status, "
    "identity_answer, identity_form, 0, 0, 0, 0, 0, 0, 1, accepted_at, role "
    "FROM grade_acceptances LIMIT 1"
)
"""One acceptance made by hand beside the saved ones: its ID, kind, source key and report."""


def untied_file(path: pathlib.Path) -> dict[str, tuple[str | None, str | None]]:
    """A synthetic file without the two columns, holding every kind of row the rebuild ties,
    and the class and term each must get: a report's acceptance, a report-less one whose capture
    has two reports (one a copy), one in a second term, a screenshot's report, and a homework
    screenshot's."""
    store = ProjectStateStore.open(path, fixture_clock())
    for draft in (WREN, SECOND_TERM):
        assert isinstance(save_grade(store, draft, key=KEY), GradeReportSaved)
        nothing_new = save_grade(store, draft, key=KEY, selection=())
        assert isinstance(nothing_new, GradeReportSaved), nothing_new
        assert nothing_new.report_id is None
    store.close()
    raw = sqlite3.connect(path)
    with raw:
        raw.execute("ALTER TABLE grade_acceptances RENAME TO made")
        raw.execute(UNTIED_ACCEPTANCES)
        raw.execute(
            f"INSERT INTO grade_acceptances SELECT {UNTIED_COLUMNS} FROM made"  # noqa: S608
        )
        raw.execute("DROP TABLE made")
        (first,) = raw.execute(
            "SELECT report_id FROM grade_reports WHERE term_label = 'T1'"
        ).fetchone()
        raw.execute(
            "INSERT INTO grade_reports SELECT 'copy-of-first', student_id, class_id, term_label, "
            "source_key, acceptance_order + 1, use, reader, imported_at, as_of, result_rows "
            "FROM grade_reports WHERE report_id = ?",
            (first,),
        )
        raw.execute(HAND_MADE, ("a screenshot", "grade_screenshot", "a picture", first))
        raw.execute(HAND_MADE, ("a homework picture", "homework_screenshot", "a page", None))
    tied: dict[str, tuple[str | None, str | None]] = {}
    for acceptance_id, kind, key, report_id in raw.execute(
        "SELECT acceptance_id, kind, source_key, report_id FROM grade_acceptances"
    ):
        if kind == "homework_screenshot":
            tied[acceptance_id] = (None, None)
            continue
        scopes = raw.execute(
            "SELECT DISTINCT class_id, term_label FROM grade_reports "
            "WHERE report_id = ? OR (? IS NULL AND source_key = ?)",
            (report_id, report_id, key),
        ).fetchall()
        assert len(scopes) == 1
        tied[acceptance_id] = scopes[0]
    raw.close()
    return tied


def changed_by_hand(path: pathlib.Path, *statements: tuple[str, tuple[object, ...]]) -> None:
    raw = sqlite3.connect(path)
    with raw:
        for sql, values in statements:
            raw.execute(sql, values)
    raw.close()


def gradebook_world(path: pathlib.Path) -> dict[str, object]:
    """Every gradebook table's schema and rows, read through a connection of its own."""
    everything = closed_world([path], leaving_out=())
    return {
        name: held
        for name, held in everything.items()
        if any(name.endswith((f" {table}", f" of {table}")) for table in GRADEBOOK_TABLES)
    }


def class_and_term(path: pathlib.Path) -> dict[str, tuple[str | None, str | None]]:
    raw = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return {
            str(row[0]): (row[1], row[2])
            for row in raw.execute(
                "SELECT acceptance_id, class_id, term_label FROM grade_acceptances"
            )
        }
    finally:
        raw.close()


def test_the_acceptance_rebuild_ties_every_row_or_refuses_the_start(
    tmp_path: pathlib.Path,
) -> None:
    """Every kind of row it ties gets exactly its scope, every other column kept, and a second
    start changes nothing."""
    path = tmp_path / "blossom.sqlite3"
    tied = untied_file(path)
    before = closed_world([path], leaving_out=("grade_acceptances",))
    raw = sqlite3.connect(path)
    kept = sorted(raw.execute(f"SELECT {UNTIED_COLUMNS} FROM grade_acceptances"))  # noqa: S608
    raw.close()

    ProjectStateStore.open(path, fixture_clock()).close()
    rebuilt = closed_world([path], leaving_out=())
    ProjectStateStore.open(path, fixture_clock()).close()

    raw = sqlite3.connect(path)
    after = sorted(raw.execute(f"SELECT {UNTIED_COLUMNS} FROM grade_acceptances"))  # noqa: S608
    raw.close()
    assert len(tied) == 6
    assert {scope[1] for scope in tied.values()} == {"T1", "T2", None}
    assert class_and_term(path) == tied
    assert after == kept
    assert closed_world([path], leaving_out=("grade_acceptances",)) == before
    assert closed_world([path], leaving_out=()) == rebuilt


REFUSED: dict[str, tuple[tuple[str, tuple[object, ...]], ...]] = {
    "a report-less row whose capture has no report": (
        (
            "UPDATE grade_acceptances SET source_key = 'nothing saved' WHERE acceptance_id = ("
            "SELECT acceptance_id FROM grade_acceptances WHERE report_id IS NULL "
            "AND kind = 'grade_text' ORDER BY acceptance_id LIMIT 1)",
            (),
        ),
    ),
    "a report that isn't there": (
        ("UPDATE grade_acceptances SET report_id = 'gone' WHERE kind = 'grade_screenshot'", ()),
    ),
    "a screenshot naming no report": (
        ("UPDATE grade_acceptances SET report_id = NULL WHERE kind = 'grade_screenshot'", ()),
    ),
    "a capture whose reports disagree": (
        ("UPDATE grade_reports SET term_label = 'T9' WHERE report_id = 'copy-of-first'", ()),
    ),
    "a homework row naming a report": (
        (
            "UPDATE grade_acceptances SET report_id = 'copy-of-first' "
            "WHERE kind = 'homework_screenshot'",
            (),
        ),
    ),
}


@pytest.mark.parametrize("case", REFUSED)
def test_a_row_the_rebuild_cannot_tie_refuses_the_start_and_changes_nothing(
    tmp_path: pathlib.Path, case: str
) -> None:
    """The refusal counts the rows of each case, never their content, says nothing was lost,
    and every gradebook table stays as it was, the file's other tables too."""
    path = tmp_path / "blossom.sqlite3"
    untied_file(path)
    changed_by_hand(path, *REFUSED[case])
    before = closed_world([path], leaving_out=())

    with pytest.raises(AcceptancesNotTied) as refusal:
        ProjectStateStore.open(path, fixture_clock())

    message = str(refusal.value)
    assert closed_world([path], leaving_out=()) == before
    assert "1 " in message
    assert "Nothing in the file was changed" in message
    assert not [
        held for held in ("gone", "copy-of-first", "nothing saved", "T9") if held in message
    ]


SITES: dict[str, tuple[int, str, str | None]] = {
    "the old table set aside": (sqlite3.SQLITE_ALTER_TABLE, "main", "grade_acceptances"),
    "the new table": (sqlite3.SQLITE_CREATE_TABLE, "grade_acceptances", None),
    "a row copied": (sqlite3.SQLITE_INSERT, "grade_acceptances", None),
    "the old table dropped": (sqlite3.SQLITE_DROP_TABLE, "grade_acceptances_before", None),
}


@pytest.mark.parametrize("site", SITES)
def test_a_refused_rebuild_step_leaves_the_old_table_byte_identical(
    tmp_path: pathlib.Path, site: str
) -> None:
    """Each step of the rebuild refused as a failing file refuses it stops the start with the
    old table and its rows as they were; the next start rebuilds it."""
    path = tmp_path / "blossom.sqlite3"
    tied = untied_file(path)
    before = gradebook_world(path)
    refused = SITES[site]

    begun: list[bool] = []

    def authorize(action: int, first: str | None, second: str | None, *_: object) -> int:
        if (action, first, second) == SITES["the old table set aside"]:
            begun.append(True)
        if begun and (action, first, second) == refused:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection = sqlite3.connect(path, check_same_thread=False)
    connection.set_authorizer(authorize)
    with pytest.raises(sqlite3.DatabaseError):
        ProjectStateStore(connection, fixture_clock())
    connection.close()

    assert begun
    assert gradebook_world(path) == before
    ProjectStateStore.open(path, fixture_clock()).close()
    assert class_and_term(path) == tied


def test_the_rebuilt_table_matches_a_fresh_file_and_nothing_else_changes(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "blossom.sqlite3"
    untied_file(path)
    before = closed_world([path], leaving_out=("grade_acceptances",))
    ProjectStateStore.open(path, fixture_clock()).close()
    fresh = tmp_path / "fresh.sqlite3"
    ProjectStateStore.open(fresh, fixture_clock()).close()

    def schema(file: pathlib.Path) -> list[tuple[str, str, str]]:
        raw = sqlite3.connect(file)
        try:
            return raw.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name LIKE 'grade_acceptances%' "
                "OR tbl_name LIKE 'grade_acceptances%' ORDER BY type, name"
            ).fetchall()
        finally:
            raw.close()

    def names(file: pathlib.Path) -> set[str]:
        raw = sqlite3.connect(file)
        try:
            return {str(row[0]) for row in raw.execute("SELECT name FROM sqlite_master")}
        finally:
            raw.close()

    assert schema(path) == schema(fresh)
    assert len(schema(path)) == 2
    assert names(path) == names(fresh)
    assert closed_world([path], leaving_out=("grade_acceptances",)) == before


def test_a_term_saved_after_the_rebuild_keeps_both_scopes_apart(tmp_path: pathlib.Path) -> None:
    """A save on a rebuilt file writes its class and term with its acceptance, as on a fresh
    one."""
    path = tmp_path / "blossom.sqlite3"
    untied_file(path)
    store = ProjectStateStore.open(path, fixture_clock())
    again = save_grade(store, SECOND_TERM, key=KEY, selection=())
    (scope,) = store._connection.execute(
        "SELECT DISTINCT class_id, term_label FROM grade_reports WHERE term_label = 'T2'"
    ).fetchall()
    store.close()

    assert isinstance(again, GradeReportSaved)
    assert again.report_id is None
    assert class_and_term(path)[again.acceptance_id] == scope


# The delete of one class's grades for one term.

ZYGOTE = draft_of(REPORT.replace("Cell Diagram", "Zygote Sketch"))
"""Wren's first-term report with an assignment no other report names, to find in the file."""
EIGHT = draft_of(
    REPORT.replace("| Cell Diagram             | 7.0 ", "| Cell Diagram             | 8.0 ")
)
"""A newer capture of the first term: Cell Diagram changed."""


def two_terms(path: pathlib.Path) -> tuple[ProjectStateStore, str]:
    """Wren's first and second terms saved, and the first term's class."""
    store = ProjectStateStore.open(path, fixture_clock())
    for draft in (WREN, SECOND_TERM):
        assert isinstance(save_grade(store, draft, key=KEY), GradeReportSaved)
    return store, capture_class(store, WREN)


def previewed(store: ProjectStateStore, class_id: str, term: str = "T1") -> DeletePreview:
    preview = store.delete_preview(class_id, term)
    assert isinstance(preview, DeletePreview), preview
    return preview


def deleted(
    store: ProjectStateStore, class_id: str, revision: int | None, term: str = "T1"
) -> DeleteOutcome:
    return store.delete_class_term(class_id, term, revision=revision, role="parent")


def held_in(store: ProjectStateStore, class_id: str, term: str) -> dict[str, int]:
    """How many rows of each scoped table belong to the class and term."""
    scope = (store.student_id(), class_id, term)
    by_report = (
        "student_id = ? AND report_id IN (SELECT report_id FROM grade_reports "
        "WHERE student_id = ? AND class_id = ? AND term_label = ?)"
    )
    counted = {}
    for table in (
        "grade_match_decisions",
        "grade_term_observations",
        "grade_category_observations",
        "grade_result_observations",
    ):
        sql = f"SELECT COUNT(*) FROM {table} WHERE {by_report}"  # noqa: S608
        counted[table] = store._connection.execute(sql, (scope[0], *scope)).fetchone()[0]
    for table in ("grade_acceptances", "grade_current_actions", "grade_reports", "grade_results"):
        sql = (
            f"SELECT COUNT(*) FROM {table} "  # noqa: S608
            "WHERE student_id = ? AND class_id = ? AND term_label = ?"
        )
        counted[table] = store._connection.execute(sql, scope).fetchone()[0]
    return counted


@pytest.mark.parametrize("fresh", ["another capture", "the same capture"])
def test_a_delete_then_a_fresh_import_then_the_old_delete_s_retry_deletes_nothing(
    tmp_path: pathlib.Path, fresh: str
) -> None:
    """His case 2: the old confirmation's revision is never current again, so its retry
    returns a fresh preview and deletes nothing newly imported; only that fresh preview's
    revision deletes."""
    path = tmp_path / "blossom.sqlite3"
    store, class_id = two_terms(path)
    old = previewed(store, class_id)
    assert isinstance(deleted(store, class_id, old.revision), ClassTermDeleted)
    imported = save_grade(store, EIGHT if fresh == "another capture" else WREN, key=KEY)
    assert isinstance(imported, GradeReportSaved)
    before = gradebook_world(path)

    retried = deleted(store, class_id, old.revision)
    after_retry = gradebook_world(path)
    fresh_page = previewed(store, class_id)
    again = deleted(store, class_id, retried.preview.revision)  # type: ignore[union-attr]
    left = held_in(store, class_id, "T1")
    store.close()

    assert isinstance(retried, DeleteReturned)
    assert retried.preview.revision is not None
    assert retried.preview.revision > (old.revision or 0) + 1
    assert retried.preview.imported_reports == 1
    assert retried.preview == fresh_page
    assert after_retry == before
    assert isinstance(again, ClassTermDeleted)
    assert left == dict.fromkeys(left, 0)


def test_a_retried_delete_answers_already_deleted(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "blossom.sqlite3"
    store, class_id = two_terms(path)
    preview = previewed(store, class_id)
    first = deleted(store, class_id, preview.revision)
    after = gradebook_world(path)
    resent = deleted(store, class_id, preview.revision)
    store.close()

    assert isinstance(first, ClassTermDeleted)
    assert first.removed == preview
    assert resent == AlreadyDeleted(class_id, "T1")
    assert gradebook_world(path) == after


def test_a_delete_leaves_every_other_class_and_term_and_her_identity(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "blossom.sqlite3"
    store, class_id = two_terms(path)
    kept = (
        "grade_student",
        "grade_name_forms",
        "grade_context",
        "grade_years",
        "grade_terms",
        "grade_classes",
        "grade_class_aliases",
    )
    before = {table: as_stored(store, table) for table in kept}
    second = held_in(store, class_id, "T2")
    other = store.current_values(class_id, "T2")
    assert isinstance(
        deleted(store, class_id, previewed(store, class_id).revision), ClassTermDeleted
    )

    assert {table: as_stored(store, table) for table in kept} == before
    assert held_in(store, class_id, "T2") == second
    assert store.current_values(class_id, "T2") == other
    assert store.delete_preview(class_id, "T1") == NothingToDelete(class_id, "T1")
    store.close()


def test_a_save_page_and_its_retry_from_before_a_delete_save_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """A page reviewed before the delete, and a retry of a save whose acceptance the delete
    removed, meet the raised revision and write nothing."""
    path = tmp_path / "blossom.sqlite3"
    store, class_id = two_terms(path)
    retry_page = store.review_grade_report(EIGHT, capture_key(EIGHT), key=KEY)
    saved = save_grade(store, EIGHT, key=KEY, review=retry_page)
    assert isinstance(saved, GradeReportSaved)
    page = store.review_grade_report(EIGHT, capture_key(EIGHT), key=KEY)
    assert isinstance(
        deleted(store, class_id, previewed(store, class_id).revision), ClassTermDeleted
    )
    after = gradebook_world(path)

    stale = save_grade(store, EIGHT, key=KEY, review=page)
    retried = save_grade(store, EIGHT, key=KEY, review=retry_page)
    store.close()

    assert isinstance(stale, ReviewReturned)
    assert stale.why is ReturnReason.REVISION
    assert isinstance(retried, ReviewReturned)
    assert retried.why is ReturnReason.REVISION
    assert gradebook_world(path) == after


def test_a_class_details_action_after_a_delete_names_no_deleted_report(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "blossom.sqlite3"
    store, class_id = two_terms(path)
    assert isinstance(save_grade(store, EIGHT, key=KEY), GradeReportSaved)
    preview = current_preview(store, WREN)
    assert isinstance(confirm_current(store, WREN, preview), MadeCurrent)
    assert isinstance(
        deleted(store, class_id, previewed(store, class_id).revision), ClassTermDeleted
    )
    after = gradebook_world(path)

    retried = store.use_capture_as_current(
        class_id,
        "T1",
        page=CurrentPage(preview.action_id, preview.source, preview.digest),
        role="parent",
    )
    store.close()

    assert isinstance(retried, ReportNotSaved)
    assert gradebook_world(path) == after


def test_a_delete_removes_every_acceptance_of_its_class_and_term(tmp_path: pathlib.Path) -> None:
    """The report-less acceptance of a submission that saved nothing goes with its class and
    term, so a complete reading of that capture saved again reads complete."""
    path = tmp_path / "blossom.sqlite3"
    store, class_id = two_terms(path)
    nothing_new = save_grade(store, WREN, key=KEY, selection=())
    assert isinstance(nothing_new, GradeReportSaved)
    assert nothing_new.report_id is None
    assert isinstance(
        deleted(store, class_id, previewed(store, class_id).revision), ClassTermDeleted
    )
    gone = held_in(store, class_id, "T1")["grade_acceptances"]
    again = save_grade(store, WREN, key=KEY, complete=True)
    assert isinstance(again, GradeReportSaved)
    complete = store._scope_held(store.student_id(), class_id, "T1").complete
    store.close()

    assert gone == 0
    assert complete == {again.report_id: True}


def test_a_class_and_term_holding_only_submissions_that_saved_nothing_is_deleted(
    tmp_path: pathlib.Path,
) -> None:
    """No report and no revision row: the preview offers it, the delete removes them and makes
    the revision row, and a page from before saves nothing."""
    path = tmp_path / "blossom.sqlite3"
    store = ProjectStateStore.open(path, fixture_clock())
    page = store.review_grade_report(WREN, capture_key(WREN), key=KEY)
    nothing = save_grade(store, WREN, key=KEY, selection=())
    assert isinstance(nothing, GradeReportSaved)
    assert nothing.report_id is None
    class_id = str(
        store._connection.execute("SELECT class_id FROM grade_acceptances").fetchone()[0]
    )
    preview = previewed(store, class_id)
    outcome = deleted(store, class_id, preview.revision)
    revision = store._connection.execute(
        "SELECT revision FROM grade_scope_revisions WHERE class_id = ?", (class_id,)
    ).fetchall()
    stale = save_grade(store, WREN, key=KEY, review=page)
    store.close()

    assert preview == DeletePreview(None, 0, 0, 0, 0, 1)
    assert isinstance(outcome, ClassTermDeleted)
    assert revision == [(1,)]
    assert isinstance(stale, ReviewReturned)
    assert stale.why is ReturnReason.REVISION


def test_a_no_op_between_preview_and_confirmation_is_deleted_too(tmp_path: pathlib.Path) -> None:
    """A submission that records nothing new leaves the revision as it was, so the confirmation
    built before it still matches and removes it too; the preview counted one fewer."""
    path = tmp_path / "blossom.sqlite3"
    store, class_id = two_terms(path)
    preview = previewed(store, class_id)
    nothing_new = save_grade(store, WREN, key=KEY, selection=(), complete=True)
    assert isinstance(nothing_new, GradeReportSaved)
    assert nothing_new.report_id is None
    assert previewed(store, class_id).revision == preview.revision
    outcome = deleted(store, class_id, preview.revision)
    left = held_in(store, class_id, "T1")
    store.close()

    assert preview.submissions == 0
    assert isinstance(outcome, ClassTermDeleted)
    assert left == dict.fromkeys(left, 0)


def test_a_deleted_value_is_in_no_byte_of_the_file(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "blossom.sqlite3"
    store = ProjectStateStore.open(path, fixture_clock())
    assert isinstance(save_grade(store, ZYGOTE, key=KEY), GradeReportSaved)
    class_id = capture_class(store, ZYGOTE)
    store.close()
    assert b"zygote" in path.read_bytes().lower()
    store = ProjectStateStore.open(path, fixture_clock())
    assert isinstance(
        deleted(store, class_id, previewed(store, class_id).revision), ClassTermDeleted
    )
    store.close()

    assert b"zygote" not in path.read_bytes().lower()


def test_a_replay_checks_access_and_ownership_first(tmp_path: pathlib.Path) -> None:
    """Her own role is refused before any statement runs, and the delete's answers are read
    from the class and term as they are, under her student ID."""
    path = tmp_path / "blossom.sqlite3"
    store, class_id = two_terms(path)
    preview = previewed(store, class_id)
    ran: list[str] = []
    store._connection.set_trace_callback(ran.append)
    with pytest.raises(ValueError, match="only a parent"):
        store.delete_class_term(class_id, "T1", revision=preview.revision, role="student")  # type: ignore[arg-type]
    store._connection.set_trace_callback(None)
    assert isinstance(deleted(store, class_id, preview.revision), ClassTermDeleted)
    store._connection.execute(
        "UPDATE grade_scope_revisions SET student_id = 'someone else' WHERE class_id = ? "
        "AND term_label = 'T1'",
        (class_id,),
    )
    elsewhere = deleted(store, class_id, preview.revision)
    store._connection.rollback()
    store.close()

    assert ran == []
    assert elsewhere == NothingToDelete(class_id, "T1")
