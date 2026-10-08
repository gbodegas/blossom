# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A grade write is one write inside a caller's transaction too.

A caller that holds the store's writer, writes, saves a report and catches the save's refusal
keeps its own work and none of the save: every write statement of a save is refused in turn,
the release of the save's savepoint as well, and the file is compared whole after each. The
same holds for the class-details action, a first month's correction and a class and term's
delete, and G-I20 runs every
write's statements in one loop. When SQLite itself ends the caller's transaction, everything in
it is gone, and the save says so with an error that is not a refusal, so no caller commits the
rest of its block alone.
"""

import dataclasses
import pathlib
import sqlite3
from collections.abc import Callable, Iterator

import pytest

from blossom.grades.draft import GradeReportDraft, capture_key
from blossom.grades.identity import name_form_key
from blossom.grades.projection import ActionRecorded, CurrentPreview, MadeCurrent
from blossom.grades.review import (
    AlreadyRecorded,
    GradeAnswers,
    GradeReportSaved,
    GradeReview,
    MatchAnswer,
)
from blossom.grades.text_reader import read_grade_report
from blossom.stores import gradebook
from blossom.stores.gradebook import (
    AlreadyDeleted,
    ClassTermDeleted,
    DeletePreview,
    FirstMonthCorrected,
    FirstMonthStood,
    GradeReportNotSaved,
)
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    FIXTURES,
    a_row,
    capture_class,
    closed_world,
    confirm_current,
    current_preview,
    fixture_clock,
    grade_answers,
    save_grade,
)

REPORT = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")
KEY = name_form_key(b"5" * 64)
YEAR = "2026-2027"
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


@pytest.mark.parametrize("joined", [True, False], ids=["joined", "standalone"])
def test_a_transaction_sqlite_ends_is_lost_and_never_a_refusal(
    opened: Callable[[str], ProjectStateStore], path: pathlib.Path, joined: bool
) -> None:
    """An interrupt at the report's insert makes SQLite roll back the whole transaction:
    nothing of the save remains, a caller's earlier write is gone too, and the caller hears
    that the transaction was lost, from the interrupt itself, which a refusal's handler
    doesn't catch. The same holds for a save of its own."""
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
        if not joined:
            save_grade(store, WREN, key=KEY, review=review)
            return
        with store.comparing_and_writing():
            unrelated(store, "before")
            save_grade(store, WREN, key=KEY, review=review)

    store._connection.set_authorizer(arm)
    store._connection.set_progress_handler(interrupt, 1)
    with pytest.raises(gradebook.GradeTransactionLost) as lost:
        caller()
    store._connection.set_progress_handler(None, 1)
    store._connection.set_authorizer(None)

    assert not issubclass(gradebook.GradeTransactionLost, GradeReportNotSaved)
    assert isinstance(lost.value.__cause__, sqlite3.OperationalError)
    assert "interrupt" in str(lost.value.__cause__)
    assert not store._connection.in_transaction
    assert world(path) == before
    assert assignments(store) == rows
    assert isinstance(save_grade(store, WREN, key=KEY, review=review), GradeReportSaved)


@pytest.mark.parametrize("joined", [True, False], ids=["joined", "standalone"])
def test_a_failed_cleanup_is_a_lost_transaction_never_a_refusal(
    opened: Callable[[str], ProjectStateStore], path: pathlib.Path, joined: bool
) -> None:
    """The acceptance is refused, and so is the rollback to the save's savepoint: the save
    can't say its writes were taken back, so it raises a lost transaction from the refusal,
    and a caller that abandons its block keeps nothing of it."""
    store = opened("first use")
    review = review_of(store)
    before, rows = world(path), assignments(store)

    def caller() -> None:
        if not joined:
            save_grade(store, WREN, key=KEY, review=review)
            return
        with store.comparing_and_writing():
            unrelated(store, "before")
            save_grade(store, WREN, key=KEY, review=review)

    store._connection.set_authorizer(
        refusing(
            (sqlite3.SQLITE_INSERT, "grade_acceptances"),
            (sqlite3.SQLITE_SAVEPOINT, "ROLLBACK", "grade_save"),
        )
    )
    with pytest.raises(gradebook.GradeTransactionLost) as lost:
        caller()
    store._connection.set_authorizer(None)

    assert isinstance(lost.value.__cause__, sqlite3.DatabaseError)
    assert not store._connection.in_transaction
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

    monkeypatch.setattr(store, "_new_report", broken)
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
    """No begin, commit or plain rollback runs inside a grade write's savepoint, and no grade
    table is written outside one, for a save or a first month's correction. SQLite's trace sees
    every statement as it runs; its authorizer sees a statement only when it is first prepared."""
    store = opened(scenario)
    ran: list[str] = []
    store._connection.set_trace_callback(ran.append)
    with store.comparing_and_writing():
        assert isinstance(save_grade(store, WREN, key=KEY), GradeReportSaved)
        corrected = store.correct_first_month(YEAR, shown=8, month=9, role="parent")
        class_id = capture_class(store, WREN)
        held = store.delete_preview(class_id, "T1")
        assert isinstance(held, DeletePreview)
        removed = store.delete_class_term(class_id, "T1", revision=held.revision, role="parent")
    store._connection.set_trace_callback(None)

    statements = [" ".join(text.split()).upper() for text in ran]
    depth = 0
    inside: list[str] = []
    written: list[bool] = []
    for text in statements:
        depth += (text == "SAVEPOINT GRADE_SAVE") - (text == "RELEASE GRADE_SAVE")
        if depth:
            inside.append(text)
        if text.startswith(("INSERT", "UPDATE", "DELETE")) and " GRADE_" in text.split("(")[0]:
            written.append(depth > 0)
    ending = ("BEGIN", "COMMIT", "END")
    assert corrected == FirstMonthCorrected(YEAR, 9)
    assert isinstance(removed, ClassTermDeleted)
    assert statements.count("SAVEPOINT GRADE_SAVE") >= 3
    assert depth == 0
    assert not [text for text in inside if text.startswith(ending) or text == "ROLLBACK"]
    assert [text for text in inside if text.startswith("UPDATE GRADE_YEARS")]
    assert [text for text in inside if text.startswith("DELETE FROM GRADE_REPORTS")]
    assert written
    assert all(written)


def test_a_grade_savepoint_never_opens_outside_a_transaction() -> None:
    connection = sqlite3.connect(":memory:")
    with (
        pytest.raises(RuntimeError, match="inside the writer's transaction"),
        gradebook.all_or_none(connection),
    ):
        pass
    assert not connection.in_transaction


EIGHT = draft_of(
    REPORT.replace("| Cell Diagram             | 7.0 ", "| Cell Diagram             | 8.0 ")
)
"""A newer capture: Cell Diagram changed, its other rows as saved."""
PRESENCE: tuple[Site, ...] = (
    (sqlite3.SQLITE_INSERT, "grade_terms"),
    (sqlite3.SQLITE_INSERT, "grade_reports"),
    (sqlite3.SQLITE_INSERT, "grade_match_decisions"),
    (sqlite3.SQLITE_INSERT, "grade_scope_revisions"),
    (sqlite3.SQLITE_INSERT, "grade_acceptances"),
)
"""Every write statement of a save of presence alone: the term, kept when on record, the report,
its row records, the revision and the acceptance."""


def presence_page(store: ProjectStateStore) -> GradeReview:
    """Wren's report saved, then the page of a newer capture whose rows all match."""
    assert isinstance(save_grade(store, WREN, key=KEY), GradeReportSaved)
    return store.review_grade_report(EIGHT, capture_key(EIGHT), key=KEY)


def test_a_save_of_presence_alone_writes_exactly_the_named_statements(
    opened: Callable[[str], ProjectStateStore],
) -> None:
    store = opened("first use")
    review = presence_page(store)
    seen: set[Site] = set()

    def note(action: int, table: str | None, *_: object) -> int:
        if action in WRITES and table is not None:
            seen.add((action, table))
        return sqlite3.SQLITE_OK

    store._connection.set_authorizer(note)
    outcome = save_grade(store, EIGHT, key=KEY, review=review, selection=())
    store._connection.set_authorizer(None)

    assert isinstance(outcome, GradeReportSaved)
    assert (outcome.added, outcome.updated, outcome.shown) == (0, 0, 4)
    assert seen == {*PRESENCE, UPSERT_ARM}


@pytest.mark.parametrize("site", PRESENCE, ids=[table for _, table in PRESENCE])
def test_a_refused_write_of_presence_alone_leaves_nothing_of_it(
    opened: Callable[[str], ProjectStateStore], path: pathlib.Path, site: Site
) -> None:
    """A save of presence alone, refused at each of its statements three times inside a
    caller's transaction, leaves the file as it was, with the caller's writes kept; lifted, the
    same page saves once."""
    store = opened("first use")
    review = presence_page(store)
    before, rows = world(path), assignments(store)

    for attempt in range(3):
        store._connection.set_authorizer(refusing(site))
        with store.comparing_and_writing():
            unrelated(store, f"before-{attempt}")
            with pytest.raises(GradeReportNotSaved):
                save_grade(store, EIGHT, key=KEY, review=review, selection=())
            unrelated(store, f"after-{attempt}")
        store._connection.set_authorizer(None)

        assert world(path) == before
        assert assignments(store) == rows + 2 * (attempt + 1)
    assert isinstance(
        save_grade(store, EIGHT, key=KEY, review=review, selection=()), GradeReportSaved
    )
    assert isinstance(
        save_grade(store, EIGHT, key=KEY, review=review, selection=()), AlreadyRecorded
    )


def test_a_later_row_record_refused_leaves_nothing_of_the_save(
    opened: Callable[[str], ProjectStateStore], path: pathlib.Path
) -> None:
    """The row record statement refused only at a later row, by a TEMP trigger keyed on that row,
    after the earlier rows' records ran: nothing of the save remains."""
    store = opened("first use")
    review = presence_page(store)
    later = review.rows[2].key
    before, rows = world(path), assignments(store)
    store._connection.execute(
        "CREATE TEMP TRIGGER refuse_later_record BEFORE INSERT ON grade_match_decisions "
        f"WHEN NEW.row_key = '{later}' BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )

    for attempt in range(3):
        with store.comparing_and_writing():
            unrelated(store, f"before-{attempt}")
            with pytest.raises(GradeReportNotSaved):
                save_grade(store, EIGHT, key=KEY, review=review, selection=())
            unrelated(store, f"after-{attempt}")

        assert world(path) == before
        assert assignments(store) == rows + 2 * (attempt + 1)
    store._connection.execute("DROP TRIGGER temp.refuse_later_record")
    saved = save_grade(store, EIGHT, key=KEY, review=review, selection=())
    assert isinstance(saved, GradeReportSaved)
    assert saved.shown == 4


RENAMED = draft_of(REPORT.replace("| Seed Germination Log |", "| Seed Germination Journal |"))
"""A newer capture: Seed Germination Log renamed, a question about its one candidate."""
RESOLVED: tuple[Site, ...] = (
    (sqlite3.SQLITE_INSERT, "grade_terms"),
    (sqlite3.SQLITE_UPDATE, "grade_match_decisions"),
    (sqlite3.SQLITE_INSERT, "grade_scope_revisions"),
    (sqlite3.SQLITE_INSERT, "grade_acceptances"),
)
"""Every write statement of a save that answers "Same assignment" for a row whose remembered
"A different assignment" its report keeps: the term, the record changed in place, the revision
and the acceptance."""


def renamed_page(store: ProjectStateStore, *, same: bool) -> tuple[GradeReview, GradeAnswers]:
    """The page of the renamed capture, with its row answered "A different assignment", or
    "Same assignment" when ``same``."""
    review = store.review_grade_report(RENAMED, capture_key(RENAMED), key=KEY)
    (asked,) = [item for item in review.rows if item.question is not None]
    assert asked.question is not None
    named = asked.question.ids[0] if same else None
    answer = MatchAnswer(asked.key, asked.question.ids, named)
    return review, dataclasses.replace(grade_answers(review), matches=(answer,))


def different_page(store: ProjectStateStore, *, kept: bool) -> tuple[GradeReview, GradeAnswers]:
    """Wren's report saved, then the renamed capture's page answering "A different assignment";
    when ``kept``, that answer saved first and the page answering "Same assignment"."""
    assert isinstance(save_grade(store, WREN, key=KEY), GradeReportSaved)
    if not kept:
        return renamed_page(store, same=False)
    review, answers = renamed_page(store, same=False)
    kept_first = save_grade(store, RENAMED, key=KEY, review=review, answers=answers, selection=())
    assert isinstance(kept_first, GradeReportSaved)
    return renamed_page(store, same=True)


@pytest.mark.parametrize("kept", [False, True], ids=["different", "resolved"])
def test_a_save_of_answers_alone_writes_exactly_the_named_statements(
    opened: Callable[[str], ProjectStateStore], kept: bool
) -> None:
    """A "different" answer kept writes a report's statements; resolving it later changes its
    record in place."""
    store = opened("first use")
    review, answers = different_page(store, kept=kept)
    seen: set[Site] = set()

    def note(action: int, table: str | None, *_: object) -> int:
        if action in WRITES and table is not None:
            seen.add((action, table))
        return sqlite3.SQLITE_OK

    store._connection.set_authorizer(note)
    outcome = save_grade(store, RENAMED, key=KEY, review=review, answers=answers, selection=())
    store._connection.set_authorizer(None)

    assert isinstance(outcome, GradeReportSaved)
    assert outcome.answers_kept == 1
    assert seen == {*(RESOLVED if kept else PRESENCE), UPSERT_ARM}


ANSWER_SITES = [(False, site) for site in PRESENCE] + [(True, site) for site in RESOLVED]


@pytest.mark.parametrize(
    ("kept", "site"),
    ANSWER_SITES,
    ids=[f"{'resolved' if kept else 'different'}-{table}" for kept, (_, table) in ANSWER_SITES],
)
def test_a_refused_write_of_answers_alone_leaves_nothing_of_it(
    opened: Callable[[str], ProjectStateStore], path: pathlib.Path, kept: bool, site: Site
) -> None:
    """A "different" answer kept, or later resolved in place, refused at each statement three
    times inside a caller's transaction, leaves the file as it was, with the caller's writes
    kept; lifted, the same page saves once."""
    store = opened("first use")
    review, answers = different_page(store, kept=kept)
    before, rows = world(path), assignments(store)

    def again() -> object:
        return save_grade(store, RENAMED, key=KEY, review=review, answers=answers, selection=())

    for attempt in range(3):
        store._connection.set_authorizer(refusing(site))
        with store.comparing_and_writing():
            unrelated(store, f"before-{attempt}")
            with pytest.raises(GradeReportNotSaved):
                again()
            unrelated(store, f"after-{attempt}")
        store._connection.set_authorizer(None)

        assert world(path) == before
        assert assignments(store) == rows + 2 * (attempt + 1)
    assert isinstance(again(), GradeReportSaved)
    assert isinstance(again(), AlreadyRecorded)


def test_a_refused_different_record_leaves_nothing_of_the_save(
    opened: Callable[[str], ProjectStateStore], path: pathlib.Path
) -> None:
    """The "different" record refused by a TEMP trigger keyed on it, after the rows shown were
    recorded: nothing of the save remains."""
    store = opened("first use")
    review, answers = different_page(store, kept=False)
    before, rows = world(path), assignments(store)
    store._connection.execute(
        "CREATE TEMP TRIGGER refuse_different BEFORE INSERT ON grade_match_decisions "
        "WHEN NEW.how = 'different' BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )

    for attempt in range(3):
        with store.comparing_and_writing():
            unrelated(store, f"before-{attempt}")
            with pytest.raises(GradeReportNotSaved):
                save_grade(store, RENAMED, key=KEY, review=review, answers=answers, selection=())
            unrelated(store, f"after-{attempt}")

        assert world(path) == before
        assert assignments(store) == rows + 2 * (attempt + 1)
    store._connection.execute("DROP TRIGGER temp.refuse_different")
    saved = save_grade(store, RENAMED, key=KEY, review=review, answers=answers, selection=())
    assert isinstance(saved, GradeReportSaved)
    assert (saved.shown, saved.answers_kept) == (3, 1)


NO_OP: tuple[Site, ...] = (
    (sqlite3.SQLITE_INSERT, "grade_terms"),
    (sqlite3.SQLITE_INSERT, "grade_acceptances"),
)
"""Every write statement of a submission that records nothing new: the term, kept when on
record, and the acceptance, for its retry. The revision stays."""
INCOMPLETE_NO_OP: tuple[Site, ...] = (
    (sqlite3.SQLITE_INSERT, "grade_terms"),
    (sqlite3.SQLITE_INSERT, "grade_scope_revisions"),
    (sqlite3.SQLITE_INSERT, "grade_acceptances"),
)
"""The same submission read incomplete, its capture's report on record: evidence against
that report's completeness, so the revision rises too."""
EXCUSED_MOVED = draft_of(
    REPORT.replace("| 7.0     | 10.0    |", "| EX      | 10.0    |").replace(
        "| Missing    | 09/26   |", "| Missing    | 09/29   |"
    )
)
"""A newer capture: Cell Diagram's score written "EX" and its due date moved."""


def no_op_page(store: ProjectStateStore) -> tuple[GradeReview, GradeAnswers, GradeReportDraft]:
    """Wren's report saved, then a fresh page of it: every value Saved, every row on record."""
    assert isinstance(save_grade(store, WREN, key=KEY), GradeReportSaved)
    review = review_of(store)
    return review, grade_answers(review), WREN


def unreadable_page(
    store: ProjectStateStore,
) -> tuple[GradeReview, GradeAnswers, GradeReportDraft]:
    """Wren's report saved, then the page of ``EXCUSED_MOVED`` answering "Same assignment" for
    the row whose score can't be read."""
    assert isinstance(save_grade(store, WREN, key=KEY), GradeReportSaved)
    review = store.review_grade_report(EXCUSED_MOVED, capture_key(EXCUSED_MOVED), key=KEY)
    (asked,) = [item for item in review.rows if item.question is not None]
    assert asked.question is not None
    answer = MatchAnswer(asked.key, asked.question.ids, asked.question.ids[0])
    return review, dataclasses.replace(grade_answers(review), matches=(answer,)), EXCUSED_MOVED


PAGES = {
    "no-op": (no_op_page, NO_OP, True),
    "incomplete no-op": (no_op_page, INCOMPLETE_NO_OP, False),
    "unreadable": (unreadable_page, PRESENCE, True),
}
"""Each submission with no value selected that this module checks site by site, with its
statements and whether its reading was complete."""


@pytest.mark.parametrize("scenario", list(PAGES))
def test_a_no_op_or_an_answered_unreadable_score_writes_exactly_the_named_statements(
    opened: Callable[[str], ProjectStateStore], scenario: str
) -> None:
    """A no-op writes its term and its acceptance alone, with no revision; an answer for a row
    whose score can't be read writes a save of presence alone."""
    store = opened("first use")
    page, sites, complete = PAGES[scenario]
    review, answers, draft = page(store)
    seen: set[Site] = set()

    def note(action: int, table: str | None, *_: object) -> int:
        if action in WRITES and table is not None:
            seen.add((action, table))
        return sqlite3.SQLITE_OK

    store._connection.set_authorizer(note)
    outcome = save_grade(
        store, draft, key=KEY, review=review, answers=answers, selection=(), complete=complete
    )
    store._connection.set_authorizer(None)

    assert isinstance(outcome, GradeReportSaved)
    assert (outcome.added, outcome.updated) == (0, 0)
    if scenario == "no-op":
        assert (outcome.report_id, outcome.shown) == (None, 0)
        assert seen == set(NO_OP)
    elif scenario == "incomplete no-op":
        assert (outcome.report_id, outcome.shown) == (None, 0)
        assert seen == {*INCOMPLETE_NO_OP, UPSERT_ARM}
    else:
        assert (outcome.shown, outcome.answers_kept) == (4, 1)
        assert seen == {*PRESENCE, UPSERT_ARM}


NEW_SITES = [(scenario, site) for scenario, (_, sites, _) in PAGES.items() for site in sites]


@pytest.mark.parametrize(
    ("scenario", "site"),
    NEW_SITES,
    ids=[f"{scenario}-{table}" for scenario, (_, table) in NEW_SITES],
)
def test_a_refused_no_op_or_answered_unreadable_score_leaves_nothing_of_it(
    opened: Callable[[str], ProjectStateStore], path: pathlib.Path, scenario: str, site: Site
) -> None:
    """Each refused at each of its statements three times inside a caller's transaction leaves
    the file as it was, with the caller's writes kept; lifted, the same page saves once."""
    store = opened("first use")
    page, _, complete = PAGES[scenario]
    review, answers, draft = page(store)
    before, rows = world(path), assignments(store)

    def again() -> object:
        return save_grade(
            store,
            draft,
            key=KEY,
            review=review,
            answers=answers,
            selection=(),
            complete=complete,
        )

    for attempt in range(3):
        store._connection.set_authorizer(refusing(site))
        with store.comparing_and_writing():
            unrelated(store, f"before-{attempt}")
            with pytest.raises(GradeReportNotSaved):
                again()
            unrelated(store, f"after-{attempt}")
        store._connection.set_authorizer(None)

        assert world(path) == before
        assert assignments(store) == rows + 2 * (attempt + 1)
    assert isinstance(again(), GradeReportSaved)
    assert isinstance(again(), AlreadyRecorded)


ACTION: tuple[Site, ...] = (
    (sqlite3.SQLITE_INSERT, "grade_reports"),
    (sqlite3.SQLITE_INSERT, "grade_term_observations"),
    (sqlite3.SQLITE_INSERT, "grade_category_observations"),
    (sqlite3.SQLITE_INSERT, "grade_result_observations"),
    (sqlite3.SQLITE_INSERT, "grade_match_decisions"),
    (sqlite3.SQLITE_INSERT, "grade_current_actions"),
    (sqlite3.SQLITE_INSERT, "grade_scope_revisions"),
)
"""Every write statement of the class-details action: the copied report, its observations and
row records, one statement per table, the action record and the revision."""


def action_preview(store: ProjectStateStore) -> CurrentPreview:
    """Wren's report saved, then a newer capture changing Cell Diagram: the preview of making
    the first capture's saved values current."""
    assert isinstance(save_grade(store, WREN, key=KEY), GradeReportSaved)
    assert isinstance(save_grade(store, EIGHT, key=KEY), GradeReportSaved)
    return current_preview(store, WREN)


def test_the_class_details_action_writes_exactly_the_named_statements(
    opened: Callable[[str], ProjectStateStore],
) -> None:
    store = opened("first use")
    preview = action_preview(store)
    seen: set[Site] = set()

    def note(action: int, table: str | None, *_: object) -> int:
        if action in WRITES and table is not None:
            seen.add((action, table))
        return sqlite3.SQLITE_OK

    store._connection.set_authorizer(note)
    outcome = confirm_current(store, WREN, preview)
    store._connection.set_authorizer(None)

    assert isinstance(outcome, MadeCurrent)
    assert seen == {*ACTION, UPSERT_ARM}


@pytest.mark.parametrize("site", ACTION, ids=[table for _, table in ACTION])
def test_a_refused_write_of_the_class_details_action_leaves_nothing_of_it(
    opened: Callable[[str], ProjectStateStore], path: pathlib.Path, site: Site
) -> None:
    """The action, refused at each of its statements three times inside a caller's
    transaction, leaves the file as it was, with the caller's writes kept; lifted, the same
    preview is confirmed once, and its retry writes nothing."""
    store = opened("first use")
    preview = action_preview(store)
    before, rows = world(path), assignments(store)

    for attempt in range(3):
        store._connection.set_authorizer(refusing(site))
        with store.comparing_and_writing():
            unrelated(store, f"before-{attempt}")
            with pytest.raises(GradeReportNotSaved):
                confirm_current(store, WREN, preview)
            unrelated(store, f"after-{attempt}")
        store._connection.set_authorizer(None)

        assert not store._connection.in_transaction
        assert world(path) == before
        assert assignments(store) == rows + 2 * (attempt + 1)
    outcome = confirm_current(store, WREN, preview)
    assert isinstance(outcome, MadeCurrent)
    assert confirm_current(store, WREN, preview) == ActionRecorded(outcome)


def test_a_later_copied_row_record_refused_leaves_nothing_of_the_action(
    opened: Callable[[str], ProjectStateStore], path: pathlib.Path
) -> None:
    """A TEMP trigger refuses only the copy of the last row record, after the report and its
    observations were copied in the same write: nothing of the action remains."""
    store = opened("first use")
    preview = action_preview(store)
    (last,) = store._connection.execute(
        "SELECT MAX(row_key) FROM grade_match_decisions WHERE report_id = ?", (preview.source,)
    ).fetchone()
    store._connection.execute(
        "CREATE TEMP TRIGGER refuse_last BEFORE INSERT ON grade_match_decisions "
        "WHEN NEW.row_key = " + "'" + str(last).replace("'", "''") + "' "
        "AND NEW.report_id <> " + "'" + preview.source + "' "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )
    before = world(path)

    with pytest.raises(GradeReportNotSaved):
        confirm_current(store, WREN, preview)
    store._connection.execute("DROP TRIGGER refuse_last")

    assert world(path) == before
    assert isinstance(confirm_current(store, WREN, preview), MadeCurrent)


FIRST_MONTH: tuple[Site, ...] = ((sqlite3.SQLITE_UPDATE, "grade_years"),)
"""The one write statement of a first month's correction: the year's month and who set it."""


def test_a_first_month_correction_writes_exactly_the_named_statements(
    opened: Callable[[str], ProjectStateStore],
) -> None:
    store = opened("first use")
    assert isinstance(save_grade(store, WREN, key=KEY), GradeReportSaved)
    seen: set[Site] = set()

    def note(action: int, table: str | None, *_: object) -> int:
        if action in WRITES and table is not None:
            seen.add((action, table))
        return sqlite3.SQLITE_OK

    store._connection.set_authorizer(note)
    outcome = store.correct_first_month(YEAR, shown=8, month=9, role="parent")
    store._connection.set_authorizer(None)

    assert outcome == FirstMonthCorrected(YEAR, 9)
    assert seen == set(FIRST_MONTH)


def test_a_refused_first_month_correction_leaves_nothing_of_it(
    opened: Callable[[str], ProjectStateStore], path: pathlib.Path
) -> None:
    """Refused three times inside a caller's transaction, the correction leaves the file as it
    was, with the caller's writes kept; lifted, the same page corrects the month once, and sent
    again it stands."""
    store = opened("first use")
    assert isinstance(save_grade(store, WREN, key=KEY), GradeReportSaved)
    before, rows = world(path), assignments(store)

    def again() -> object:
        return store.correct_first_month(YEAR, shown=8, month=9, role="parent")

    for attempt in range(3):
        store._connection.set_authorizer(refusing(*FIRST_MONTH))
        with store.comparing_and_writing():
            unrelated(store, f"before-{attempt}")
            with pytest.raises(GradeReportNotSaved, match="first month could not be corrected"):
                again()
            unrelated(store, f"after-{attempt}")
        store._connection.set_authorizer(None)

        assert not store._connection.in_transaction
        assert world(path) == before
        assert assignments(store) == rows + 2 * (attempt + 1)
    assert again() == FirstMonthCorrected(YEAR, 9)
    assert again() == FirstMonthStood(YEAR, 9)


DELETE: tuple[Site, ...] = (
    (sqlite3.SQLITE_DELETE, "grade_match_decisions"),
    (sqlite3.SQLITE_DELETE, "grade_term_observations"),
    (sqlite3.SQLITE_DELETE, "grade_category_observations"),
    (sqlite3.SQLITE_DELETE, "grade_result_observations"),
    (sqlite3.SQLITE_DELETE, "grade_acceptances"),
    (sqlite3.SQLITE_DELETE, "grade_current_actions"),
    (sqlite3.SQLITE_DELETE, "grade_reports"),
    (sqlite3.SQLITE_DELETE, "grade_results"),
    (sqlite3.SQLITE_INSERT, "grade_scope_revisions"),
    UPSERT_ARM,
)
"""Every write statement of a class and term's delete: one per scoped table, children first,
and the revision's upsert, both arms."""


def class_and_term_delete(store: ProjectStateStore) -> Callable[[], object]:
    """Wren's first term holding a report, a newer capture and a class-details action: the
    confirmation of deleting it, ready to send."""
    assert isinstance(confirm_current(store, WREN, action_preview(store)), MadeCurrent)
    class_id = capture_class(store, WREN)
    held = store.delete_preview(class_id, "T1")
    assert isinstance(held, DeletePreview)
    return lambda: store.delete_class_term(class_id, "T1", revision=held.revision, role="parent")


def test_a_delete_writes_exactly_the_named_statements(
    opened: Callable[[str], ProjectStateStore],
) -> None:
    store = opened("first use")
    delete = class_and_term_delete(store)
    seen: set[Site] = set()

    def note(action: int, table: str | None, *_: object) -> int:
        if action in WRITES and table is not None:
            seen.add((action, table))
        return sqlite3.SQLITE_OK

    store._connection.set_authorizer(note)
    outcome = delete()
    store._connection.set_authorizer(None)

    assert isinstance(outcome, ClassTermDeleted)
    assert seen == set(DELETE)


def test_a_refused_delete_leaves_nothing_of_it(
    opened: Callable[[str], ProjectStateStore], path: pathlib.Path
) -> None:
    """Refused at its last delete three times inside a caller's transaction, the delete leaves
    the file as it was, with the caller's writes kept; lifted, the same confirmation deletes
    once, and sent again it answers already deleted."""
    store = opened("first use")
    delete = class_and_term_delete(store)
    before, rows = world(path), assignments(store)

    for attempt in range(3):
        store._connection.set_authorizer(refusing((sqlite3.SQLITE_DELETE, "grade_results")))
        with store.comparing_and_writing():
            unrelated(store, f"before-{attempt}")
            with pytest.raises(GradeReportNotSaved, match="class and term could not be deleted"):
                delete()
            unrelated(store, f"after-{attempt}")
        store._connection.set_authorizer(None)

        assert not store._connection.in_transaction
        assert world(path) == before
        assert assignments(store) == rows + 2 * (attempt + 1)
    assert isinstance(delete(), ClassTermDeleted)
    assert isinstance(delete(), AlreadyDeleted)


Write = Callable[[ProjectStateStore], Callable[[], object]]
"""A grade write's setup on a fresh file: the write, ready to send, and to send again."""


def first_save(store: ProjectStateStore) -> Callable[[], object]:
    review = review_of(store)
    return lambda: save_grade(store, WREN, key=KEY, review=review)


def confirmed_again(store: ProjectStateStore) -> Callable[[], object]:
    assert isinstance(save_grade(store, OTHER_TERM, key=EARLIER_KEY), GradeReportSaved)
    return first_save(store)


def presence_alone(store: ProjectStateStore) -> Callable[[], object]:
    review = presence_page(store)
    return lambda: save_grade(store, EIGHT, key=KEY, review=review, selection=())


def answers_alone(*, kept: bool) -> Write:
    def setup(store: ProjectStateStore) -> Callable[[], object]:
        review, answers = different_page(store, kept=kept)
        return lambda: save_grade(
            store, RENAMED, key=KEY, review=review, answers=answers, selection=()
        )

    return setup


def submission(scenario: str) -> Write:
    def setup(store: ProjectStateStore) -> Callable[[], object]:
        page, _, complete = PAGES[scenario]
        review, answers, draft = page(store)
        return lambda: save_grade(
            store, draft, key=KEY, review=review, answers=answers, selection=(), complete=complete
        )

    return setup


def class_details_action(store: ProjectStateStore) -> Callable[[], object]:
    preview = action_preview(store)
    return lambda: confirm_current(store, WREN, preview)


def first_month_correction(store: ProjectStateStore) -> Callable[[], object]:
    assert isinstance(save_grade(store, WREN, key=KEY), GradeReportSaved)
    return lambda: store.correct_first_month(YEAR, shown=8, month=9, role="parent")


EVERY_WRITE: dict[str, tuple[Write, tuple[Site, ...], type]] = {
    "a first save": (first_save, FIRST_USE, GradeReportSaved),
    "confirmed again": (confirmed_again, (CONFIRM_AGAIN,), GradeReportSaved),
    "presence alone": (presence_alone, PRESENCE, GradeReportSaved),
    "a different answer kept": (answers_alone(kept=False), PRESENCE, GradeReportSaved),
    "a different answer resolved": (answers_alone(kept=True), RESOLVED, GradeReportSaved),
    **{
        scenario: (submission(scenario), sites, GradeReportSaved)
        for scenario, (_, sites, _) in PAGES.items()
    },
    "the class-details action": (class_details_action, ACTION, MadeCurrent),
    "a first-month correction": (first_month_correction, FIRST_MONTH, FirstMonthCorrected),
    "a class and term's delete": (class_and_term_delete, DELETE, ClassTermDeleted),
}
"""Every kind of grade write, with each write statement it makes and what it returns lifted."""


def test_g_i20_every_grade_write_is_all_or_nothing_at_each_site(tmp_path: pathlib.Path) -> None:
    """Each write statement of each kind of grade write, refused once inside a caller's
    transaction, leaves the file as it was with the caller's writes kept, as the write's own
    refusal; lifted, the same write goes through."""
    seen = {}
    for number, (kind, (setup, sites, lifted)) in enumerate(EVERY_WRITE.items()):
        for at, site in enumerate(sites):
            file = tmp_path / f"{number}-{at}.sqlite3"
            store = ProjectStateStore.open(file, fixture_clock())
            write = setup(store)
            before, rows = world(file), assignments(store)
            store._connection.set_authorizer(refusing(site))
            with store.comparing_and_writing():
                unrelated(store, "before")
                with pytest.raises(GradeReportNotSaved):
                    write()
                unrelated(store, "after")
            store._connection.set_authorizer(None)
            kept = (world(file) == before, assignments(store) == rows + 2)
            seen[kind, site] = (*kept, isinstance(write(), lifted))
            store.close()

    assert len(seen) == sum(len(sites) for _, sites, _ in EVERY_WRITE.values())
    assert seen == dict.fromkeys(seen, (True, True, True))
