# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The gradebook's invariants, each with a test named for it.

G-I1: no gradebook write changes anything outside the gradebook's own tables, read as a closed
world from ``sqlite_master`` with the checkpoint and trace files beside it, so a table added later
is covered unless it is named a gradebook table. G-I13: no gradebook table, and no log line,
holds a student's name. G-I15: every gradebook row carries her one student ID, and nothing is
looked up under another.
"""

import logging
import pathlib
import sqlite3
from collections.abc import Callable

import pytest

from blossom.grades.identity import IdentityStatus, name_form, name_form_key
from blossom.stores.gradebook import GRADEBOOK_TABLES, AnswerNotAsked, NameFormAdded
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    OBSERVED_AT,
    as_stored,
    closed_world,
    database_of,
    fixture_clock,
    household_client,
    state_of,
)

WREN = "Bramble, Wren"
LINNET = "Bramble, Linnet"
NAMES = ("bramble", "wren", "linnet")
KEY = name_form_key(b"5" * 64)
NEW_KEY = name_form_key(b"6" * 64)


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
