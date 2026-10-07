# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Her student record and the name forms a parent confirmed, told apart without her name.

The record is one row, made at the first start and kept by every start after it. A report's
student line is compared as its keyed form, never as the name, and the key check on her record
says whether the forms were made under the key in hand. A key that doesn't match asks for her
name to be confirmed again; confirming again replaces the forms and the key check together.
"""

import dataclasses
import pathlib
import re
import sqlite3
from typing import Any

import pytest

from blossom.grades.identity import (
    KEY_CHECK_TEXT,
    NAME_FORM_LABEL,
    Identity,
    IdentityStatus,
    key_check,
    name_form,
    name_form_key,
)
from blossom.household import keys_for
from blossom.stores.gradebook import (
    GRADEBOOK_TABLES,
    AnswerNotAsked,
    NameConfirmedAgain,
    NameFormAdded,
    NameFormNotSaved,
    NameFormStood,
)
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    OBSERVED_AT,
    as_stored,
    closed_world,
    fixture_clock,
    practice_store,
    signed_in_household,
)

WREN = "Bramble, Wren"
LINNET = "Bramble, Linnet"
SPREAD = "  bramble,\t\tWREN \n"
"""Her student line with other spacing and case."""
OTHER_ORDER = "Wren Bramble"
SECRET = b"5" * 64
REPLACED = b"6" * 64
KEY = name_form_key(SECRET)
NEW_KEY = name_form_key(REPLACED)
AT = OBSERVED_AT.isoformat()


def in_memory() -> ProjectStateStore:
    return ProjectStateStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())


def check_of(store: ProjectStateStore) -> object:
    return store._connection.execute("SELECT key_check FROM grade_student").fetchone()[0]


def forms_of(store: ProjectStateStore) -> list[tuple[object, ...]]:
    return store._connection.execute(
        "SELECT student_id, name_form, confirmed_by, confirmed_at FROM grade_name_forms "
        "ORDER BY name_form"
    ).fetchall()


def gradebook_of(store: ProjectStateStore) -> dict[str, list[tuple[object, ...]]]:
    return {table: as_stored(store, table) for table in GRADEBOOK_TABLES}


def deny_the_key_check(action: int, table: str | None, *_: object) -> int:
    """Refuse the write of her key check, as a file that fails partway through refuses it."""
    refused = action == sqlite3.SQLITE_UPDATE and table == "grade_student"
    return sqlite3.SQLITE_DENY if refused else sqlite3.SQLITE_OK


# ------------------------------------------------------------- her student record


def test_the_first_start_makes_her_record_once_with_a_random_id(tmp_path: pathlib.Path) -> None:
    first = ProjectStateStore.initialize(tmp_path / "one.sqlite3", fixture_clock())
    made = first.student_id()
    record = first._connection.execute("SELECT * FROM grade_student").fetchall()
    first.close()
    again = ProjectStateStore.open(tmp_path / "one.sqlite3", fixture_clock())
    kept = again.student_id()
    count = again._connection.execute("SELECT COUNT(*) FROM grade_student").fetchone()[0]
    another = ProjectStateStore.open(tmp_path / "two.sqlite3", fixture_clock())

    assert re.fullmatch(r"student-[0-9a-f]{32}", made)
    assert record == [(1, made, None, AT)]
    assert kept == made
    assert count == 1
    assert another.student_id() != made


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO grade_student (only_row, student_id, made_at) VALUES (1, 'student-b', 'x')",
        "INSERT INTO grade_student (only_row, student_id, made_at) VALUES (2, 'student-b', 'x')",
        "INSERT INTO grade_student (student_id, made_at) VALUES ('student-b', 'x')",
    ],
)
def test_a_second_student_row_is_refused_by_the_schema(statement: str) -> None:
    store = in_memory()

    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(statement)
    assert store._connection.execute("SELECT COUNT(*) FROM grade_student").fetchone()[0] == 1


# ------------------------------------------------------------- the key and the forms


def test_the_name_form_ignores_spacing_and_case_and_holds_no_name() -> None:
    form = name_form(KEY, WREN)

    assert re.fullmatch(r"[0-9a-f]{64}", form)
    assert name_form(KEY, SPREAD) == form
    assert name_form(NEW_KEY, WREN) != form
    assert name_form(KEY, LINNET) != form
    assert name_form(KEY, OTHER_ORDER) != form
    assert not {"bramble", "wren"} & set(re.findall(r"[a-z]+", form))


def test_the_name_form_reads_an_accent_the_same_however_it_was_typed() -> None:
    composed, decomposed = "Bramble, Wrén", "Bramble, Wrén"
    assert composed != decomposed
    assert name_form(KEY, composed) == name_form(KEY, decomposed)
    assert name_form(KEY, composed.upper()) == name_form(KEY, decomposed)
    assert name_form(KEY, composed) != name_form(KEY, WREN)


def test_the_name_form_key_is_its_own_and_the_key_check_tells_keys_apart(
    tmp_path: pathlib.Path,
) -> None:
    sign_in_keys = keys_for(SECRET, signed_in_household(tmp_path)).values()

    assert len(KEY) == 32
    assert KEY not in sign_in_keys
    assert KEY not in (SECRET, NEW_KEY)
    assert name_form_key(SECRET) == KEY
    assert re.fullmatch(r"[0-9a-f]{64}", key_check(KEY))
    assert key_check(KEY) == key_check(name_form_key(SECRET))
    assert key_check(KEY) != key_check(NEW_KEY)


def test_no_passphrase_draws_the_name_form_key_and_no_line_makes_the_key_check(
    tmp_path: pathlib.Path,
) -> None:
    """A passphrase and a student line are text, so their bytes are valid UTF-8; the label and
    the key check's fixed text aren't, so no sign-in key is the name-form key and no line's
    form is the key check, whatever is typed."""
    typed_label = NAME_FORM_LABEL.decode("utf-8", errors="ignore")
    typed_check = KEY_CHECK_TEXT.decode("utf-8", errors="ignore")
    household = dataclasses.replace(signed_in_household(tmp_path), parent_passphrase=typed_label)

    for fixed in (NAME_FORM_LABEL, KEY_CHECK_TEXT):
        with pytest.raises(UnicodeDecodeError):
            fixed.decode("utf-8")
    assert typed_label == "grade name forms"
    assert KEY not in keys_for(SECRET, household).values()
    assert name_form(KEY, typed_check) != key_check(KEY)


# ------------------------------------------------------------- what a student line is


def test_a_line_reads_first_use_then_matches_once_confirmed() -> None:
    store = in_memory()
    asked = store.identity_of(KEY, WREN)
    store.add_name_form(KEY, WREN, "parent")

    assert asked == Identity(IdentityStatus.FIRST_USE, name_form(KEY, WREN))
    assert store.identity_of(KEY, WREN) == Identity(IdentityStatus.MATCHES, name_form(KEY, WREN))
    assert store.identity_of(KEY, SPREAD) == Identity(IdentityStatus.MATCHES, name_form(KEY, WREN))


def test_a_sibling_s_line_is_not_confirmed_and_a_line_left_out_is_missing() -> None:
    store = in_memory()
    store.add_name_form(KEY, WREN, "parent")

    assert store.identity_of(KEY, LINNET) == Identity(
        IdentityStatus.NOT_CONFIRMED, name_form(KEY, LINNET)
    )
    assert store.identity_of(KEY, None) == Identity(IdentityStatus.MISSING, None)
    assert store.identity_of(KEY, " \t ") == Identity(IdentityStatus.MISSING, None)


def test_a_replaced_key_asks_for_confirmation_again_and_changes_nothing() -> None:
    """Never another student: the key check alone says the secret changed, so her line and a
    sibling's are both to be confirmed again, and asking writes nothing."""
    store = in_memory()
    store.add_name_form(KEY, WREN, "parent")
    saved = gradebook_of(store)
    changes = store._connection.total_changes

    hers = store.identity_of(NEW_KEY, WREN)
    sibling = store.identity_of(NEW_KEY, LINNET)

    assert hers == Identity(IdentityStatus.CONFIRM_AGAIN, name_form(NEW_KEY, WREN))
    assert sibling == Identity(IdentityStatus.CONFIRM_AGAIN, name_form(NEW_KEY, LINNET))
    assert store.identity_of(NEW_KEY, None) == Identity(IdentityStatus.MISSING, None)
    assert gradebook_of(store) == saved
    assert store._connection.total_changes == changes


def test_a_line_is_told_in_one_read_and_no_write() -> None:
    store = in_memory()
    store.add_name_form(KEY, WREN, "parent")
    seen: list[str] = []
    store._connection.set_trace_callback(seen.append)
    try:
        store.identity_of(KEY, LINNET)
    finally:
        store._connection.set_trace_callback(None)

    assert len(seen) == 1, seen
    assert seen[0].lstrip().upper().startswith("SELECT")


# ------------------------------------------------------------- "Yes, this is her name"


def test_the_first_form_sets_the_key_check_with_it() -> None:
    store = in_memory()
    before = check_of(store)

    added = store.add_name_form(KEY, WREN, "parent")

    assert before is None
    assert added == NameFormAdded(name_form(KEY, WREN))
    assert check_of(store) == key_check(KEY)
    assert forms_of(store) == [(store.student_id(), name_form(KEY, WREN), "parent", AT)]


def test_a_form_already_confirmed_writes_nothing_and_another_form_keeps_the_check() -> None:
    store = in_memory()
    store.add_name_form(KEY, WREN, "household")
    changes = store._connection.total_changes

    repeated = store.add_name_form(KEY, SPREAD, "parent")
    unchanged = store._connection.total_changes
    other = store.add_name_form(KEY, OTHER_ORDER, "parent")

    assert repeated == NameFormStood(name_form(KEY, WREN))
    assert unchanged == changes
    assert other == NameFormAdded(name_form(KEY, OTHER_ORDER))
    assert check_of(store) == key_check(KEY)
    assert sorted(row[1:3] for row in forms_of(store)) == sorted(
        [(name_form(KEY, WREN), "household"), (name_form(KEY, OTHER_ORDER), "parent")]
    )


def test_adding_a_form_is_refused_while_the_key_check_differs() -> None:
    store = in_memory()
    store.add_name_form(KEY, WREN, "parent")
    saved = gradebook_of(store)

    with pytest.raises(AnswerNotAsked) as refused:
        store.add_name_form(NEW_KEY, WREN, "parent")

    assert refused.value.identity == Identity(
        IdentityStatus.CONFIRM_AGAIN, name_form(NEW_KEY, WREN)
    )
    assert gradebook_of(store) == saved


def test_no_form_is_added_for_a_missing_line_or_by_her() -> None:
    store = in_memory()
    her: Any = "student"

    with pytest.raises(AnswerNotAsked) as refused:
        store.add_name_form(KEY, "   ", "parent")
    with pytest.raises(ValueError, match="parent"):
        store.add_name_form(KEY, WREN, her)

    assert refused.value.identity == Identity(IdentityStatus.MISSING, None)
    assert forms_of(store) == []
    assert check_of(store) is None


def test_a_first_form_whose_key_check_is_refused_leaves_neither() -> None:
    store = in_memory()
    store._connection.set_authorizer(deny_the_key_check)
    try:
        with pytest.raises(NameFormNotSaved):
            store.add_name_form(KEY, WREN, "parent")
    finally:
        store._connection.set_authorizer(None)

    assert forms_of(store) == []
    assert check_of(store) is None
    assert store.identity_of(KEY, WREN).status is IdentityStatus.FIRST_USE


def test_a_restart_keeps_her_forms_and_the_key_check(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "blossom.sqlite3"
    store = ProjectStateStore.open(path, fixture_clock())
    made = store.student_id()
    store.add_name_form(KEY, WREN, "parent")
    store.close()

    again = ProjectStateStore.open(path, fixture_clock())

    assert again.student_id() == made
    assert again.identity_of(KEY, WREN).status is IdentityStatus.MATCHES
    assert check_of(again) == key_check(KEY)


# ------------------------------------------------------------- after the secret was replaced


def test_confirming_again_replaces_the_forms_and_the_key_check_together() -> None:
    store = in_memory()
    made = store.student_id()
    store.add_name_form(KEY, WREN, "parent")
    store.add_name_form(KEY, OTHER_ORDER, "parent")

    again = store.confirm_name_again(NEW_KEY, SPREAD, "household")

    assert again == NameConfirmedAgain(name_form(NEW_KEY, WREN))
    assert forms_of(store) == [(made, name_form(NEW_KEY, WREN), "household", AT)]
    assert check_of(store) == key_check(NEW_KEY)
    assert store.student_id() == made
    assert store.identity_of(NEW_KEY, WREN).status is IdentityStatus.MATCHES
    assert store.identity_of(NEW_KEY, OTHER_ORDER).status is IdentityStatus.NOT_CONFIRMED
    assert store.identity_of(KEY, WREN).status is IdentityStatus.CONFIRM_AGAIN


def test_confirming_again_once_more_writes_nothing() -> None:
    store = in_memory()
    store.add_name_form(KEY, WREN, "parent")
    store.confirm_name_again(NEW_KEY, WREN, "parent")
    changes = store._connection.total_changes

    repeated = store.confirm_name_again(NEW_KEY, SPREAD, "parent")

    assert repeated == NameFormStood(name_form(NEW_KEY, WREN))
    assert store._connection.total_changes == changes


def test_a_failure_between_the_two_writes_leaves_both_as_they_were() -> None:
    store = in_memory()
    store.add_name_form(KEY, WREN, "parent")
    store.add_name_form(KEY, OTHER_ORDER, "parent")
    saved = gradebook_of(store)
    seen: list[str] = []
    store._connection.set_authorizer(deny_the_key_check)
    store._connection.set_trace_callback(seen.append)
    try:
        with pytest.raises(NameFormNotSaved):
            store.confirm_name_again(NEW_KEY, WREN, "parent")
    finally:
        store._connection.set_authorizer(None)
        store._connection.set_trace_callback(None)
    ran = " ".join(seen)

    assert "DELETE FROM grade_name_forms" in ran
    assert "INSERT INTO grade_name_forms" in ran
    assert seen[-1].strip().upper() == "ROLLBACK"
    assert gradebook_of(store) == saved
    assert store.identity_of(KEY, WREN).status is IdentityStatus.MATCHES
    assert store.identity_of(NEW_KEY, WREN).status is IdentityStatus.CONFIRM_AGAIN


def test_a_confirmation_failing_inside_a_caller_s_transaction_takes_back_its_writes() -> None:
    """Joined into a save's transaction, a confirmation that fails midway takes back what it
    began, so a caller that goes on and commits keeps neither half of it."""
    store = in_memory()
    store.add_name_form(KEY, WREN, "parent")
    store.add_name_form(KEY, OTHER_ORDER, "parent")
    saved = gradebook_of(store)
    store._connection.set_authorizer(deny_the_key_check)
    try:
        with store.comparing_and_writing(), pytest.raises(NameFormNotSaved):
            store.confirm_name_again(NEW_KEY, WREN, "parent")
    finally:
        store._connection.set_authorizer(None)

    assert gradebook_of(store) == saved
    assert store.identity_of(KEY, WREN).status is IdentityStatus.MATCHES


def test_a_first_form_failing_inside_a_caller_s_transaction_leaves_no_form() -> None:
    store = in_memory()
    saved = gradebook_of(store)
    store._connection.set_authorizer(deny_the_key_check)
    try:
        with store.comparing_and_writing(), pytest.raises(NameFormNotSaved):
            store.add_name_form(KEY, WREN, "parent")
    finally:
        store._connection.set_authorizer(None)

    assert gradebook_of(store) == saved
    assert store.identity_of(KEY, WREN).status is IdentityStatus.FIRST_USE


def test_the_store_says_how_long_it_keeps_her_record_and_name_forms() -> None:
    policy = ProjectStateStore.retention_policy

    assert "student record" in policy
    assert "name forms" in policy
    assert "never the names" in policy


@pytest.mark.parametrize("line", [WREN, LINNET, "  "])
def test_confirming_again_is_refused_unless_the_key_check_differs(line: str) -> None:
    """Confirming again is only for a replaced key: a first use, a line not confirmed under the
    key in hand, and a missing line are other questions, and nothing is written."""
    fresh = in_memory()
    confirmed = in_memory()
    confirmed.add_name_form(KEY, OTHER_ORDER, "parent")
    saved = gradebook_of(confirmed)

    with pytest.raises(AnswerNotAsked) as first:
        fresh.confirm_name_again(KEY, line, "parent")
    with pytest.raises(AnswerNotAsked) as later:
        confirmed.confirm_name_again(KEY, line, "parent")

    assert first.value.identity == fresh.identity_of(KEY, line)
    assert later.value.identity == confirmed.identity_of(KEY, line)
    assert first.value.identity.status is not IdentityStatus.CONFIRM_AGAIN
    assert forms_of(fresh) == []
    assert check_of(fresh) is None
    assert gradebook_of(confirmed) == saved


def test_confirming_again_touches_no_row_outside_its_two_tables(tmp_path: pathlib.Path) -> None:
    """Acceptance history and everything else in the file stay as they were: confirming again
    writes her name forms and her key check, and nothing more."""
    path = tmp_path / "blossom.sqlite3"
    store = practice_store(path)
    store.add_name_form(KEY, WREN, "parent")
    before = closed_world([path], leaving_out=GRADEBOOK_TABLES)

    store.confirm_name_again(NEW_KEY, WREN, "parent")

    assert closed_world([path], leaving_out=GRADEBOOK_TABLES) == before
