# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A grade save is one write inside a caller's transaction too.

A caller that holds the store's writer, writes, saves a report and catches the save's refusal
keeps its own work and none of the save: every write statement of a save is refused in turn,
the release of the save's savepoint as well, and the file is compared whole after each. When
SQLite itself ends the caller's transaction, everything in it is gone, and the save says so
with an error that is not a refusal, so no caller commits the rest of its block alone.
"""

import pathlib
import sqlite3
from collections.abc import Callable, Iterator

import pytest

from blossom.grades.draft import GradeReportDraft, capture_key
from blossom.grades.identity import name_form_key
from blossom.grades.review import AlreadyRecorded, GradeReportSaved, GradeReview
from blossom.grades.text_reader import read_grade_report
from blossom.stores import gradebook
from blossom.stores.gradebook import GradeReportNotSaved
from blossom.stores.project_state import ProjectStateStore
from tests.support import FIXTURES, a_row, closed_world, fixture_clock, save_grade

REPORT = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")
KEY = name_form_key(b"5" * 64)
EARLIER_KEY = name_form_key(b"6" * 64)
"""The key her name was confirmed under before the household secret was replaced."""

Site = tuple[int, str]
FIRST_USE: tuple[Site, ...] = (
    (sqlite3.SQLITE_INSERT, "grade_name_forms"),
    (sqlite3.SQLITE_UPDATE, "grade_student"),
    (sqlite3.SQLITE_INSERT, "grade_context"),
    (sqlite3.SQLITE_INSERT, "grade_years"),
    (sqlite3.SQLITE_INSERT, "grade_terms"),
    (sqlite3.SQLITE_INSERT, "grade_classes"),
    (sqlite3.SQLITE_INSERT, "grade_class_aliases"),
    (sqlite3.SQLITE_INSERT, "grade_reports"),
    (sqlite3.SQLITE_INSERT, "grade_term_observations"),
    (sqlite3.SQLITE_INSERT, "grade_category_observations"),
    (sqlite3.SQLITE_INSERT, "grade_results"),
    (sqlite3.SQLITE_INSERT, "grade_result_observations"),
    (sqlite3.SQLITE_INSERT, "grade_match_decisions"),
    (sqlite3.SQLITE_INSERT, "grade_scope_revisions"),
    (sqlite3.SQLITE_INSERT, "grade_acceptances"),
)
"""Every write statement of a first save, in order: her name form and key check, the setup,
the class, the report and its values, the revision and the acceptance."""
CONFIRM_AGAIN: Site = (sqlite3.SQLITE_DELETE, "grade_name_forms")
"""The one statement only "Yes, this is her name" after a replaced secret makes."""
UPSERT_ARM: Site = (sqlite3.SQLITE_UPDATE, "grade_scope_revisions")
"""The revision's upsert, which SQLite authorizes as an update too, in the same statement."""
WRITES = frozenset({sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE})
SITES = [("first use", site) for site in FIRST_USE] + [("confirm again", CONFIRM_AGAIN)]
SAVEPOINT_RELEASED = (sqlite3.SQLITE_SAVEPOINT, "RELEASE", "grade_save")


def draft_of(text: str) -> GradeReportDraft:
    reading = read_grade_report(text)
    assert reading.draft is not None, reading.not_read
    return reading.draft


WREN = draft_of(REPORT)
OTHER_TERM = draft_of(REPORT.replace("**T1**", "**T2**"))


@pytest.fixture
def path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "blossom.sqlite3"


@pytest.fixture
def opened(path: pathlib.Path) -> Iterator[Callable[[str], ProjectStateStore]]:
    """The store of the file at ``path``, after the scenario's earlier saves; closed after."""
    stores: list[ProjectStateStore] = []

    def open_for(scenario: str) -> ProjectStateStore:
        store = ProjectStateStore.open(path, fixture_clock())
        stores.append(store)
        if scenario == "confirm again":
            assert isinstance(save_grade(store, OTHER_TERM, key=EARLIER_KEY), GradeReportSaved)
        return store

    yield open_for
    for store in stores:
        store.close()


def review_of(store: ProjectStateStore) -> GradeReview:
    return store.review_grade_report(WREN, capture_key(WREN), key=KEY)


def world(path: pathlib.Path) -> dict[str, object]:
    """The whole file but its assignments, read through connections of its own."""
    return closed_world([path], leaving_out=("assignments",))


def assignments(store: ProjectStateStore) -> int:
    return int(store._connection.execute("SELECT COUNT(*) FROM assignments").fetchone()[0])


def unrelated(store: ProjectStateStore, name: str) -> None:
    """The caller's own work in its transaction: one homework row, joined, never committed here."""
    store.put_on_record([a_row(f"unrelated-{name}", "Unrelated work")], {})


def refusing(*refused: tuple[object, ...]) -> Callable[..., int]:
    """An authorizer that refuses each statement whose action and first argument, and second
    when given, are one of ``refused``, as a failing file refuses it, and allows all else."""

    def authorize(action: int, first: str | None, second: str | None, *_: object) -> int:
        for item in refused:
            if (action, first, second)[: len(item)] == item:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorize


def test_a_full_save_writes_exactly_the_named_statements(
    opened: Callable[[str], ProjectStateStore],
) -> None:
    """The closed list: what the two identity scenarios write is every site the tests refuse."""
    seen: set[Site] = set()
    for scenario in ("first use", "confirm again"):
        store = opened(scenario)

        def note(action: int, table: str | None, *_: object) -> int:
            if action in WRITES and table is not None:
                seen.add((action, table))
            return sqlite3.SQLITE_OK

        store._connection.set_authorizer(note)
        assert isinstance(save_grade(store, WREN, key=KEY), GradeReportSaved)
        store._connection.set_authorizer(None)
        store.close()

    assert seen == {*FIRST_USE, CONFIRM_AGAIN, UPSERT_ARM}


ACTIONS = {
    sqlite3.SQLITE_INSERT: "insert",
    sqlite3.SQLITE_UPDATE: "update",
    sqlite3.SQLITE_DELETE: "delete",
}


@pytest.mark.parametrize(
    ("scenario", "site"), SITES, ids=[f"{ACTIONS[action]}-{table}" for _, (action, table) in SITES]
)
def test_a_refused_write_inside_a_callers_transaction_leaves_nothing_of_the_save(
    opened: Callable[[str], ProjectStateStore], path: pathlib.Path, scenario: str, site: Site
) -> None:
    """Refused three times in a row inside a caller's transaction, the save leaves the file as
    it was, with the caller's own writes kept; lifted, the same page saves once."""
    store = opened(scenario)
    review = review_of(store)
    before, rows = world(path), assignments(store)

    for attempt in range(3):
        store._connection.set_authorizer(refusing(site))
        with store.comparing_and_writing():
            unrelated(store, f"before-{attempt}")
            with pytest.raises(GradeReportNotSaved):
                save_grade(store, WREN, key=KEY, review=review)
            unrelated(store, f"after-{attempt}")
        store._connection.set_authorizer(None)

        assert not store._connection.in_transaction
        assert world(path) == before
        assert assignments(store) == rows + 2 * (attempt + 1)
    assert isinstance(save_grade(store, WREN, key=KEY, review=review), GradeReportSaved)
    assert isinstance(save_grade(store, WREN, key=KEY, review=review), AlreadyRecorded)


def test_a_refused_release_takes_back_the_whole_save(
    opened: Callable[[str], ProjectStateStore], path: pathlib.Path
) -> None:
    store = opened("first use")
    review = review_of(store)
    before, rows = world(path), assignments(store)

    store._connection.set_authorizer(refusing(SAVEPOINT_RELEASED))
    with store.comparing_and_writing():
        unrelated(store, "before")
        with pytest.raises(GradeReportNotSaved):
            save_grade(store, WREN, key=KEY, review=review)
        unrelated(store, "after")
    store._connection.set_authorizer(None)

    assert world(path) == before
    assert assignments(store) == rows + 2
    assert isinstance(save_grade(store, WREN, key=KEY, review=review), GradeReportSaved)


def test_a_transaction_sqlite_ends_is_lost_and_never_a_refusal(
    opened: Callable[[str], ProjectStateStore], path: pathlib.Path
) -> None:
    """An interrupt at the report's insert makes SQLite roll back the caller's whole
    transaction: nothing of the save remains, the caller's earlier write is gone too, and the
    caller hears that its transaction was lost, which a refusal's handler doesn't catch."""
    store = opened("first use")
    review = review_of(store)
    before, rows = world(path), assignments(store)
    armed: list[bool] = []

    def arm(action: int, table: str | None, *_: object) -> int:
        if (action, table) == (sqlite3.SQLITE_INSERT, "grade_reports") and not armed:
            armed.append(True)
        return sqlite3.SQLITE_OK

    def interrupt() -> int:
        if armed and armed[-1]:
            armed.append(False)
            return 1
        return 0

    def caller() -> None:
        with store.comparing_and_writing():
            unrelated(store, "before")
            save_grade(store, WREN, key=KEY, review=review)

    store._connection.set_authorizer(arm)
    store._connection.set_progress_handler(interrupt, 1)
    with pytest.raises(gradebook.GradeTransactionLost):
        caller()
    store._connection.set_progress_handler(None, 1)
    store._connection.set_authorizer(None)

    assert not issubclass(gradebook.GradeTransactionLost, GradeReportNotSaved)
    assert world(path) == before
    assert assignments(store) == rows
    assert isinstance(save_grade(store, WREN, key=KEY, review=review), GradeReportSaved)


def test_a_joined_saves_outcome_stands_only_once_the_callers_block_commits(
    opened: Callable[[str], ProjectStateStore], path: pathlib.Path
) -> None:
    store = opened("first use")
    review = review_of(store)
    before = world(path)
    commit_refused: list[bool] = []

    def refuse_commit(action: int, first: str | None, *_: object) -> int:
        if commit_refused and (action, first) == (sqlite3.SQLITE_TRANSACTION, "COMMIT"):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def caller() -> None:
        with store.comparing_and_writing():
            assert isinstance(save_grade(store, WREN, key=KEY, review=review), GradeReportSaved)
            commit_refused.append(True)

    store._connection.set_authorizer(refuse_commit)
    with pytest.raises(sqlite3.DatabaseError):
        caller()
    store._connection.set_authorizer(None)

    assert world(path) == before
    assert isinstance(save_grade(store, WREN, key=KEY, review=review), GradeReportSaved)


def test_any_other_exception_takes_back_the_save_and_is_raised_as_itself(
    opened: Callable[[str], ProjectStateStore],
    path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error that isn't the file's, after the setup was written, takes the setup back too."""
    store = opened("first use")
    review = review_of(store)
    before, rows = world(path), assignments(store)

    def broken(*_: object, **__: object) -> str:
        msg = "injected after the setup's writes"
        raise LookupError(msg)

    monkeypatch.setattr(store, "_report_of", broken)
    with store.comparing_and_writing():
        unrelated(store, "before")
        with pytest.raises(LookupError):
            save_grade(store, WREN, key=KEY, review=review)
        unrelated(store, "after")
    monkeypatch.undo()

    assert world(path) == before
    assert assignments(store) == rows + 2


def test_a_caller_that_fails_after_the_save_keeps_none_of_its_block(
    opened: Callable[[str], ProjectStateStore], path: pathlib.Path
) -> None:
    """The control: the save joins the caller's transaction, so the caller's failure takes back
    the save and both of its own writes."""
    store = opened("first use")
    review = review_of(store)
    before, rows = world(path), assignments(store)

    def caller() -> None:
        with store.comparing_and_writing():
            unrelated(store, "before")
            assert isinstance(save_grade(store, WREN, key=KEY, review=review), GradeReportSaved)
            unrelated(store, "after")
            msg = "the caller's own failure"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="the caller's own failure"):
        caller()

    assert world(path) == before
    assert assignments(store) == rows


@pytest.mark.parametrize("scenario", ["first use", "confirm again"])
def test_every_grade_write_happens_inside_the_saves_one_savepoint(
    opened: Callable[[str], ProjectStateStore], scenario: str
) -> None:
    """No begin, commit or plain rollback runs between the save's savepoint and its release,
    and no grade table is written outside them. SQLite's trace sees every statement as it runs;
    its authorizer sees a statement only when it is first prepared, so it can't tell order."""
    store = opened(scenario)
    ran: list[str] = []
    store._connection.set_trace_callback(ran.append)
    with store.comparing_and_writing():
        assert isinstance(save_grade(store, WREN, key=KEY), GradeReportSaved)
    store._connection.set_trace_callback(None)

    statements = [" ".join(text.split()).upper() for text in ran]
    opened_at = statements.index("SAVEPOINT GRADE_SAVE")
    released_at = max(at for at, text in enumerate(statements) if text == "RELEASE GRADE_SAVE")
    inside = statements[opened_at:released_at]
    ending = ("BEGIN", "COMMIT", "END")
    assert not [text for text in inside if text.startswith(ending) or text == "ROLLBACK"]
    written = [
        at
        for at, text in enumerate(statements)
        if text.startswith(("INSERT", "UPDATE", "DELETE")) and " GRADE_" in text.split("(")[0]
    ]
    assert written
    assert all(opened_at < at < released_at for at in written)


def test_a_grade_savepoint_never_opens_outside_a_transaction() -> None:
    connection = sqlite3.connect(":memory:")
    with (
        pytest.raises(RuntimeError, match="inside the writer's transaction"),
        gradebook.all_or_none(connection),
    ):
        pass
    assert not connection.in_transaction
