# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A request for help about one homework note or one school result: the reference, and what
becomes of it.

The help store has a connection of its own to the record's file. A request that names a
note or a result is checked and written in one transaction on that connection, begun before
the note or result is looked for, and nothing else about asking for help changes.
"""

import pathlib
import sqlite3
from datetime import UTC, date, datetime

import pytest

from blossom.captures import STUDENT, CaptureChanged, CaptureCreated, NotACaptureId, new_capture_id
from blossom.grades.draft import capture_key
from blossom.grades.identity import name_form_key
from blossom.grades.review import GradeReportSaved
from blossom.grades.text_reader import read_grade_report
from blossom.reconciliation import SourceChannel
from blossom.stores.gradebook import DeletePreview
from blossom.stores.help_requests import (
    HelpAlreadyAsked,
    HelpAsked,
    HelpFormChanged,
    HelpFormUsed,
    HelpRequestsStore,
    NotAResultId,
    UnknownCaptureReference,
    UnknownResultReference,
    new_request_id,
)
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    FIXTURES,
    PLAN_DATE,
    fixture_clock,
    grade_answers,
    practice_store,
    save_grade,
)

AT = datetime(2026, 9, 14, 21, 0, tzinfo=UTC)
MONDAY = date(2026, 9, 14)


def note_in(store: ProjectStateStore, text: str = "Geometry questions 4-8") -> str:
    name = new_capture_id()
    outcome = store.create_capture(
        name,
        text,
        None,
        None,
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=AT,
        today=MONDAY,
    )
    assert isinstance(outcome, CaptureCreated)
    return name


def rows_of(help_store: HelpRequestsStore) -> list[tuple[object, ...]]:
    found = help_store._connection.execute("SELECT * FROM help_requests ORDER BY request_id")
    return [tuple(row) for row in found.fetchall()]


@pytest.fixture
def stores(tmp_path: pathlib.Path) -> tuple[ProjectStateStore, HelpRequestsStore]:
    path = tmp_path / "record.sqlite3"
    return practice_store(path), HelpRequestsStore.open(path, fixture_clock())


# ------------------------------------------------------------------ the file


def test_a_file_from_before_gains_the_column_and_keeps_every_request(
    tmp_path: pathlib.Path,
) -> None:
    """Additive, and safe to meet again: an old table gets one nullable column, every request
    in it reads as about no note, and a second start changes nothing."""
    path = tmp_path / "record.sqlite3"
    old = sqlite3.connect(path)
    old.execute(
        """
        CREATE TABLE help_requests (
            request_id TEXT PRIMARY KEY,
            evening TEXT NOT NULL,
            asked_at TEXT NOT NULL,
            note TEXT,
            state TEXT NOT NULL,
            accepted_at TEXT,
            resolved_at TEXT,
            response TEXT
        )
        """
    )
    old.execute(
        "INSERT INTO help_requests (request_id, evening, asked_at, note, state) "
        "VALUES ('from-before', ?, ?, 'the essay outline', 'requested')",
        (PLAN_DATE.isoformat(), fixture_clock().now().isoformat()),
    )
    old.commit()
    old.close()

    first = HelpRequestsStore.open(path, fixture_clock())
    kept = first.open_requests()
    first.close()
    second = HelpRequestsStore.open(path, fixture_clock())
    columns = [
        str(row[1]) for row in second._connection.execute("PRAGMA table_info(help_requests)")
    ]

    assert [(item.request_id, item.note, item.capture_id) for item in kept] == [
        ("from-before", "the essay outline", None)
    ]
    assert columns.count("capture_id") == 1
    assert second.open_requests() == kept
    assert second.ask(PLAN_DATE, "another").capture_id is None


# ------------------------------------------------------------- the reference


def test_a_request_about_a_note_names_it_and_copies_none_of_its_words(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    record, help_store = stores
    name = note_in(record, "Geometry questions 4-8, heard from a classmate")

    asked = help_store.ask(PLAN_DATE, None, capture_id=name)
    with_words = help_store.ask(PLAN_DATE, "which questions?", capture_id=name)

    assert (asked.capture_id, asked.note) == (name, None)
    assert (with_words.capture_id, with_words.note) == (name, "which questions?")
    assert [item.capture_id for item in help_store.open_requests()] == [name, name]
    assert help_store.get(asked.request_id) == asked


def test_a_name_that_is_no_note_of_this_record_writes_no_request(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    record, help_store = stores
    note_in(record)
    before = rows_of(help_store)

    with pytest.raises(UnknownCaptureReference):
        help_store.ask(PLAN_DATE, "about nothing", capture_id=new_capture_id())
    with pytest.raises(NotACaptureId):
        help_store.ask(PLAN_DATE, "about nothing", capture_id="assignment-canal-essay")
    with pytest.raises(NotACaptureId):
        help_store.ask(PLAN_DATE, "about nothing", capture_id="")

    assert rows_of(help_store) == before
    assert not help_store._connection.in_transaction
    assert help_store.ask(PLAN_DATE, "and then an ordinary one").capture_id is None


def test_a_file_with_no_notes_in_it_has_no_note_to_name() -> None:
    alone = HelpRequestsStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())

    with pytest.raises(UnknownCaptureReference):
        alone.ask(PLAN_DATE, None, capture_id=new_capture_id())

    assert alone.open_requests() == []
    assert not alone._connection.in_transaction


def test_the_note_is_looked_for_inside_the_transaction_that_writes_the_request(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    """The writer is reserved first, on the help store's own connection, then the form's id,
    then the note is read, then the request is written, and only then is it all committed. A
    connection's own scope would begin nothing until the insert, leaving the read outside."""
    record, help_store = stores
    name = note_in(record)
    seen: list[str] = []
    help_store._connection.set_trace_callback(seen.append)

    help_store.ask(PLAN_DATE, "which questions?", capture_id=name)
    help_store._connection.set_trace_callback(None)

    said = [" ".join(statement.split()) for statement in seen]
    assert said[0] == "BEGIN IMMEDIATE"
    assert said[-1] == "COMMIT"
    read = next(
        index for index, statement in enumerate(said) if "FROM homework_captures" in statement
    )
    reserved = next(
        index for index, statement in enumerate(said) if "INTO help_request_ids" in statement
    )
    wrote = next(
        index
        for index, statement in enumerate(said)
        if statement.startswith("INSERT INTO help_requests")
    )
    assert 0 < reserved < read < wrote < len(said) - 1
    assert not help_store._connection.in_transaction


def test_a_request_refused_by_the_file_leaves_nothing_and_no_transaction_open(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    record, help_store = stores
    name = note_in(record)
    help_store._connection.execute(
        "CREATE TRIGGER refuse_requests BEFORE INSERT ON help_requests "
        "BEGIN SELECT RAISE(ABORT, 'the file refused'); END"
    )
    help_store._connection.commit()

    with pytest.raises(sqlite3.DatabaseError):
        help_store.ask(PLAN_DATE, None, capture_id=name)

    assert rows_of(help_store) == []
    assert not help_store._connection.in_transaction


def test_putting_a_note_away_keeps_the_reference_and_a_damaged_note_loses_no_request(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    record, help_store = stores
    name = note_in(record)
    asked = help_store.ask(PLAN_DATE, "stuck on 6", capture_id=name)

    archived = record.archive_capture(
        name, expected_revision=1, authored_by=STUDENT, now=AT, today=MONDAY
    )
    about_an_archived_note = help_store.ask(PLAN_DATE, None, capture_id=name)
    record._connection.execute("UPDATE homework_captures SET due_date = 'next week'")
    record._connection.commit()

    assert isinstance(archived, CaptureChanged)
    assert about_an_archived_note.capture_id == name
    assert sorted(item.request_id for item in help_store.open_requests()) == sorted(
        [asked.request_id, about_an_archived_note.request_id]
    )
    assert help_store.accept(asked.request_id, "on it").capture_id == name
    assert help_store.resolve(asked.request_id).capture_id == name


@pytest.mark.parametrize(
    "held",
    [b"INJECTED reference", "INJECTED words", "CAPITALS", 7, 1.5],
)
def test_a_reference_that_is_no_id_as_the_store_writes_it_is_kept_as_unreadable_and_not_as_text(
    tmp_path: pathlib.Path, held: object
) -> None:
    """The store writes a note's id one way. Anything else in that column is no reference it
    wrote: the request is still read, it says its reference cannot be read, and nothing of
    what the column holds leaves the store."""
    path = tmp_path / "record.sqlite3"
    record = practice_store(path)
    name = new_capture_id()
    record.create_capture(
        name,
        "Geometry questions 4-8",
        None,
        None,
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=AT,
        today=MONDAY,
    )
    requests = HelpRequestsStore(sqlite3.connect(path, check_same_thread=False), fixture_clock())
    asked = requests.ask(MONDAY, "which part?", capture_id=name)
    value = name.upper() if held == "CAPITALS" else held
    requests._connection.execute("UPDATE help_requests SET capture_id = ?", (value,))
    requests._connection.commit()

    read = requests.get(asked.request_id)
    listed = requests.open_requests()

    assert read is not None
    assert (read.capture_id, read.capture_reference_unreadable) == (None, True)
    assert read.note == "which part?"
    assert [item.request_id for item in listed] == [asked.request_id]
    assert "INJECTED" not in read.model_dump_json()
    assert name.upper() not in read.model_dump_json()
    sound = requests.ask(MONDAY, None, capture_id=name)
    assert (sound.capture_id, sound.capture_reference_unreadable) == (name, False)
    plain = requests.ask(MONDAY, "about no note")
    assert (plain.capture_id, plain.capture_reference_unreadable) == (None, False)


# ------------------------------------------------------------------ what a request leaves behind


def named_by_requests(help_store: HelpRequestsStore) -> list[str]:
    found = help_store._connection.execute(
        "SELECT capture_id FROM notes_named_by_requests ORDER BY capture_id"
    )
    return [str(row[0]) for row in found.fetchall()]


def test_a_note_a_request_named_stays_named_after_the_request_goes(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    store, help_store = stores
    taken_back, swept, never = note_in(store), note_in(store, "Bring the form"), note_in(store)
    first = help_store.ask(PLAN_DATE, "which questions?", capture_id=taken_back)
    assert help_store.take_back(first.request_id)
    second = help_store.ask(PLAN_DATE, None, capture_id=swept)
    help_store.resolve(second.request_id, "Questions 4-8")
    help_store._connection.execute(
        "UPDATE help_requests SET resolved_at = '2020-01-01T00:00:00+00:00'"
    )
    help_store._connection.commit()
    help_store.ask(PLAN_DATE, "about nothing in particular")

    assert help_store.sweep() == 1
    assert sorted(named_by_requests(help_store)) == sorted([taken_back, swept])
    columns = help_store._connection.execute("PRAGMA table_info(notes_named_by_requests)")
    assert [str(row[1]) for row in columns] == ["capture_id"]
    assert store.capture_use(taken_back) == "asked"
    assert store.capture_use(swept) == "asked"
    assert store.capture_use(never) is None


def test_a_request_refused_names_no_note(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    _, help_store = stores

    with pytest.raises(UnknownCaptureReference):
        help_store.ask(PLAN_DATE, None, capture_id=new_capture_id())

    assert named_by_requests(help_store) == []


def test_a_file_from_before_names_every_note_its_requests_named(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "record.sqlite3"
    store = practice_store(path)
    name = note_in(store)
    help_store = HelpRequestsStore.open(path, fixture_clock())
    help_store.ask(PLAN_DATE, None, capture_id=name)
    help_store.ask(PLAN_DATE, "no note")
    help_store._connection.execute("DROP TABLE notes_named_by_requests")
    help_store._connection.commit()
    help_store.close()

    again = HelpRequestsStore.open(path, fixture_clock())
    once_more = HelpRequestsStore.open(path, fixture_clock())

    assert named_by_requests(again) == [name]
    assert named_by_requests(once_more) == [name]


def test_a_request_is_use_even_before_the_help_store_has_kept_its_note(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    """A file whose help store has not started since the ids were kept still shows the
    request itself, which is enough to keep the note."""
    store, help_store = stores
    name = note_in(store)
    help_store.ask(PLAN_DATE, None, capture_id=name)
    help_store._connection.execute("DROP TABLE notes_named_by_requests")
    help_store._connection.commit()

    assert store.capture_use(name) == "asked"


# ------------------------------------------------------------------ a result


GRADE_REPORT = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")
"""Wren's synthetic report: Biology, 2026-2027, T1."""
GRADE_KEY = name_form_key(b"5" * 64)


def result_in(store: ProjectStateStore) -> str:
    """One of her results, saved from the synthetic report through the store."""
    draft = read_grade_report(GRADE_REPORT).draft
    assert draft is not None
    review = store.review_grade_report(draft, capture_key(draft), key=GRADE_KEY)
    outcome = save_grade(store, draft, key=GRADE_KEY, review=review, answers=grade_answers(review))
    assert isinstance(outcome, GradeReportSaved), outcome
    (result_id,) = store._connection.execute(
        "SELECT result_id FROM grade_results ORDER BY result_id LIMIT 1"
    ).fetchone()
    return str(result_id)


def deleted(store: ProjectStateStore, result_id: str) -> None:
    """Her result's class and term deleted through the gradebook's own delete."""
    place = store.result_place(result_id)
    assert place is not None
    preview = store.delete_preview(place.class_id, place.term)
    assert isinstance(preview, DeletePreview)
    store.delete_class_term(place.class_id, place.term, revision=preview.revision, role="parent")


def help_tables(help_store: HelpRequestsStore) -> dict[str, list[tuple[object, ...]]]:
    return {
        table: [tuple(row) for row in help_store._connection.execute(f"SELECT * FROM {table}")]  # noqa: S608
        for table in ("help_requests", "help_request_ids", "notes_named_by_requests")
    }


def test_a_request_about_a_result_keeps_its_id_and_nothing_of_the_result(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    record, help_store = stores
    result_id = result_in(record)
    form = new_request_id()

    outcome = help_store.ask_once(
        form, MONDAY, "what does missing mean?", grade_result_id=result_id
    )

    assert isinstance(outcome, HelpAsked)
    kept = help_store.get(form)
    assert kept is not None
    assert (kept.grade_result_id, kept.note, kept.capture_id) == (
        result_id,
        "what does missing mean?",
        None,
    )
    assert kept.result_reference_unreadable is False
    held = " ".join(repr(row) for rows in help_tables(help_store).values() for row in rows)
    assert "Biology" not in held
    assert "Osmosis" not in held


@pytest.mark.parametrize("which", ["unknown", "another student", "deleted"])
def test_a_result_that_is_no_result_of_hers_writes_nothing_and_leaves_the_form_unused(
    stores: tuple[ProjectStateStore, HelpRequestsStore], which: str
) -> None:
    record, help_store = stores
    result_id = result_in(record)
    if which == "unknown":
        named = f"result-{new_request_id()}"
    elif which == "another student":
        named = f"result-{new_request_id()}"
        record._connection.execute(
            "INSERT INTO grade_results (result_id, student_id, class_id, term_label, made_at) "
            "SELECT ?, 'student-someone-else', class_id, term_label, made_at FROM grade_results "
            "WHERE result_id = ?",
            (named, result_id),
        )
        record._connection.commit()
    else:
        named = result_id
        deleted(record, result_id)
    before = help_tables(help_store)
    form = new_request_id()

    with pytest.raises(UnknownResultReference):
        help_store.ask_once(form, MONDAY, "which one?", grade_result_id=named)

    assert help_tables(help_store) == before
    assert not help_store._connection.in_transaction
    assert help_store.already_asked(form, "which one?", grade_result_id=named) is None
    assert isinstance(help_store.ask_once(form, MONDAY, "which one?"), HelpAsked)


@pytest.mark.parametrize(
    "named", ["", "result-", "RESULT-" + "a" * 32, "result-" + "A" * 32, "class-" + "a" * 32]
)
def test_a_malformed_result_id_is_refused_before_anything_is_read(
    stores: tuple[ProjectStateStore, HelpRequestsStore], named: str
) -> None:
    _, help_store = stores
    seen: list[str] = []
    help_store._connection.set_trace_callback(seen.append)
    form = new_request_id()

    with pytest.raises(NotAResultId):
        help_store.ask_once(form, MONDAY, None, grade_result_id=named)
    with pytest.raises(NotAResultId):
        help_store.already_asked(form, None, grade_result_id=named)
    help_store._connection.set_trace_callback(None)

    assert seen == []
    assert help_store.already_asked(form) is None


def test_a_request_names_a_note_or_a_result_never_both(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    record, help_store = stores
    note, result_id = note_in(record), result_in(record)
    seen: list[str] = []
    help_store._connection.set_trace_callback(seen.append)
    form = new_request_id()

    with pytest.raises(ValueError, match="note or a result"):
        help_store.ask_once(form, MONDAY, None, capture_id=note, grade_result_id=result_id)
    help_store._connection.set_trace_callback(None)

    assert seen == []
    assert help_store.already_asked(form) is None


def test_the_result_is_looked_for_inside_the_transaction_that_writes_the_request(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    record, help_store = stores
    result_id = result_in(record)
    seen: list[str] = []
    help_store._connection.set_trace_callback(seen.append)

    help_store.ask_once(new_request_id(), MONDAY, None, grade_result_id=result_id)
    help_store._connection.set_trace_callback(None)

    said = [" ".join(statement.split()) for statement in seen]
    assert said[0] == "BEGIN IMMEDIATE"
    assert said[-1] == "COMMIT"
    reserved = next(i for i, one in enumerate(said) if "INTO help_request_ids" in one)
    read = next(i for i, one in enumerate(said) if "FROM grade_results" in one)
    wrote = next(i for i, one in enumerate(said) if one.startswith("INSERT INTO help_requests"))
    assert 0 < reserved < read < wrote < len(said) - 1


def test_the_same_form_about_a_result_is_a_replay_and_other_words_or_results_changed(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    record, help_store = stores
    result_id = result_in(record)
    other = f"result-{new_request_id()}"
    form = new_request_id()
    first = help_store.ask_once(form, MONDAY, "which part?", grade_result_id=result_id)
    assert isinstance(first, HelpAsked)

    again = help_store.ask_once(form, MONDAY, "which  part?", grade_result_id=result_id)
    looked = help_store.already_asked(form, "which part?", grade_result_id=result_id)
    other_words = help_store.ask_once(form, MONDAY, "another", grade_result_id=result_id)
    other_result = help_store.already_asked(form, "which part?", grade_result_id=other)
    no_result = help_store.ask_once(form, MONDAY, "which part?")

    assert isinstance(again, HelpAlreadyAsked)
    assert again.request == first.request
    assert isinstance(looked, HelpAlreadyAsked)
    assert isinstance(other_words, HelpFormChanged)
    assert isinstance(other_result, HelpFormChanged)
    assert isinstance(no_result, HelpFormChanged)
    assert len(help_store.open_requests()) == 1
    assert help_store.take_back(form)
    assert isinstance(
        help_store.ask_once(form, MONDAY, "which part?", grade_result_id=result_id), HelpFormUsed
    )


def test_delete_then_ask_writes_nothing_and_ask_then_delete_keeps_the_request(
    stores: tuple[ProjectStateStore, HelpRequestsStore],
) -> None:
    record, help_store = stores
    result_id = result_in(record)
    asked = help_store.ask(MONDAY, "is this late?", grade_result_id=result_id)
    before = help_tables(help_store)

    deleted(record, result_id)

    assert help_tables(help_store) == before
    kept = help_store.get(asked.request_id)
    assert kept is not None
    assert (kept.grade_result_id, kept.note) == (result_id, "is this late?")
    with pytest.raises(UnknownResultReference):
        help_store.ask(MONDAY, "and this one?", grade_result_id=result_id)
    assert help_tables(help_store) == before
    assert help_store.resolve(asked.request_id, "It was turned in.").grade_result_id == result_id


def test_a_file_from_before_gains_the_result_column_once(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "record.sqlite3"
    first = HelpRequestsStore.open(path, fixture_clock())
    asked = first.ask(PLAN_DATE, "the essay outline")
    first._connection.execute("ALTER TABLE help_requests DROP COLUMN grade_result_id")
    first._connection.commit()
    first.close()

    second = HelpRequestsStore.open(path, fixture_clock())
    second.close()
    third = HelpRequestsStore.open(path, fixture_clock())
    columns = [str(row[1]) for row in third._connection.execute("PRAGMA table_info(help_requests)")]

    assert columns.count("grade_result_id") == 1
    kept = third.get(asked.request_id)
    assert kept is not None
    assert (kept.grade_result_id, kept.result_reference_unreadable) == (None, False)
    assert kept.note == "the essay outline"


@pytest.mark.parametrize("held", [b"INJECTED reference", "INJECTED words", "CAPITALS", 7, 1.5])
def test_a_result_reference_that_is_no_id_is_kept_as_unreadable_and_not_as_text(
    stores: tuple[ProjectStateStore, HelpRequestsStore], held: object
) -> None:
    record, help_store = stores
    result_id = result_in(record)
    asked = help_store.ask(MONDAY, "which part?", grade_result_id=result_id)
    value = result_id.upper() if held == "CAPITALS" else held
    help_store._connection.execute("UPDATE help_requests SET grade_result_id = ?", (value,))
    help_store._connection.commit()

    read = help_store.get(asked.request_id)

    assert read is not None
    assert (read.grade_result_id, read.result_reference_unreadable) == (None, True)
    assert (read.capture_id, read.capture_reference_unreadable) == (None, False)
    assert read.note == "which part?"
    assert [item.request_id for item in help_store.open_requests()] == [asked.request_id]
    assert "INJECTED" not in read.model_dump_json()
    assert result_id.upper() not in read.model_dump_json()
    replay = help_store.already_asked(asked.request_id, "which part?", grade_result_id=result_id)
    assert isinstance(replay, HelpFormChanged)


def test_an_unreadable_result_reference_is_no_request_about_no_result() -> None:
    """A request whose result reference can't be read is about a result all the same, so the
    same form sent again with the same words and no result is a changed form, not a replay."""
    alone = HelpRequestsStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())
    form = new_request_id()
    alone.ask_once(form, MONDAY, "which part?")
    alone._connection.execute("UPDATE help_requests SET grade_result_id = 'INJECTED'")
    alone._connection.commit()

    assert isinstance(alone.already_asked(form, "which part?"), HelpFormChanged)
    assert isinstance(alone.ask_once(form, MONDAY, "which part?"), HelpFormChanged)


def test_a_file_with_no_gradebook_in_it_has_no_result_to_name() -> None:
    alone = HelpRequestsStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())
    form = new_request_id()

    with pytest.raises(UnknownResultReference):
        alone.ask_once(form, MONDAY, None, grade_result_id=f"result-{new_request_id()}")

    assert alone.open_requests() == []
    assert alone.already_asked(form) is None
    assert not alone._connection.in_transaction
