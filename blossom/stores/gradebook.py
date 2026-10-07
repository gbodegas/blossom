# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The part of the record's store that keeps her student record for grade reports and the name
forms a parent confirmed as hers.

Mixed into the store of the record, which supplies the connection, the lock, the clock, and the
transaction that reserves the writer before it reads. Her record is one row, the schema allows
no second: a random ID, made at the first start that opens the file with these tables and never
changed, and the key check, empty until the first confirmed form. A confirmed form is a keyed
hash of a student line, never the line. Every row carries her student ID, and nothing is read
under another. Nothing here changes any other table.
"""

import secrets
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC
from typing import Final, Literal, cast, get_args

from blossom.clock import Clock
from blossom.grades.identity import Identity, IdentityStatus, identity_among, key_check

GRADEBOOK_TABLES: Final = ("grade_student", "grade_name_forms")
"""Every table a grade write may change. Every other table of the file, and the checkpoint and
trace files, are a closed world no grade write touches."""

ConfirmedBy = Literal["parent", "household"]
"""Who confirmed a name: a parent, or the household while the sign-in is off, when a page can't
say which person pressed. She never confirms her own name."""

CREATE_STUDENT: Final = """
CREATE TABLE IF NOT EXISTS grade_student (
    only_row INTEGER PRIMARY KEY CHECK (only_row = 1),
    student_id TEXT NOT NULL,
    key_check TEXT,
    made_at TEXT NOT NULL
)
"""
CREATE_NAME_FORMS: Final = """
CREATE TABLE IF NOT EXISTS grade_name_forms (
    student_id TEXT NOT NULL,
    name_form TEXT NOT NULL,
    confirmed_by TEXT NOT NULL CHECK (confirmed_by IN ('parent', 'household')),
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (student_id, name_form)
)
"""
STUDENT_ON_RECORD: Final = "SELECT 1 FROM grade_student"
MAKE_STUDENT: Final = (
    "INSERT INTO grade_student (only_row, student_id, key_check, made_at) VALUES (1, ?, NULL, ?)"
)
HER_NAME_RECORD: Final = """
SELECT student.student_id, student.key_check, forms.name_form
FROM grade_student AS student
LEFT JOIN grade_name_forms AS forms ON forms.student_id = student.student_id
"""
ADD_FORM: Final = (
    "INSERT INTO grade_name_forms (student_id, name_form, confirmed_by, confirmed_at) "
    "VALUES (?, ?, ?, ?)"
)
DROP_HER_FORMS: Final = "DELETE FROM grade_name_forms WHERE student_id = ?"
SET_KEY_CHECK: Final = "UPDATE grade_student SET key_check = ? WHERE student_id = ?"


@contextmanager
def all_or_none(connection: sqlite3.Connection) -> Iterator[None]:
    """The writes in the block land together or not at all, inside a caller's transaction too: a
    failure takes back what the block began before it goes on, so no caller commits half."""
    connection.execute("SAVEPOINT name_forms")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK TO name_forms")
        connection.execute("RELEASE name_forms")
        raise
    connection.execute("RELEASE name_forms")


def new_student_id() -> str:
    """A random ID for her student record, drawn from nothing about her."""
    return f"student-{secrets.token_hex(16)}"


class NoStudentRecord(LookupError):
    """The file has no student record: a start makes one, so this file was changed by hand."""


class AnswerNotAsked(ValueError):
    """The answer is not one the record asks about this line now; nothing was written.
    ``identity`` is what the line is now."""

    def __init__(self, identity: Identity) -> None:
        super().__init__(f"the record asks about this line as {identity.status.value}")
        self.identity = identity


class NameFormNotSaved(RuntimeError):
    """The file refused a write of her name forms; whatever was begun was rolled back with it."""


@dataclass(frozen=True)
class NameFormAdded:
    """The form was confirmed now, and the key check set with it when it was the first."""

    form: str


@dataclass(frozen=True)
class NameConfirmedAgain:
    """Her forms were replaced by this one, and the key check by one under the key in hand."""

    form: str


@dataclass(frozen=True)
class NameFormStood:
    """The form was already confirmed under the key in hand; nothing was written."""

    form: str


class GradebookRecords:
    """The part of the record's store that keeps her student record and her name forms."""

    _connection: sqlite3.Connection
    _lock: "threading.RLock"
    _clock: Clock

    def _writing(self) -> AbstractContextManager[None]:
        raise NotImplementedError

    def _create_gradebook_tables(self) -> None:
        """The tables, and her record on a file without one, in the caller's transaction."""
        self._connection.execute(CREATE_STUDENT)
        self._connection.execute(CREATE_NAME_FORMS)
        if self._connection.execute(STUDENT_ON_RECORD).fetchone() is None:
            self._connection.execute(MAKE_STUDENT, (new_student_id(), self._stamp()))

    def _her_name_record(self) -> tuple[str, str | None, list[str]]:
        """Her student ID, her key check and her confirmed forms, in one statement."""
        rows = self._connection.execute(HER_NAME_RECORD).fetchall()
        if not rows:
            msg = "the file has no student record"
            raise NoStudentRecord(msg)
        student_id, check = rows[0][0], rows[0][1]
        return student_id, check, [row[2] for row in rows if row[2] is not None]

    def student_id(self) -> str:
        """Her stable student ID, which every gradebook record carries."""
        with self._lock:
            return self._her_name_record()[0]

    def identity_of(self, key: bytes, student_line: str | None) -> Identity:
        """What ``student_line`` is against her record under ``key``, in one read and no write,
        with the keyed form of the line asked about."""
        with self._lock:
            _, check, forms = self._her_name_record()
        return identity_among(key, student_line, check=check, forms=forms)

    def add_name_form(
        self, key: bytes, student_line: str, role: ConfirmedBy
    ) -> NameFormAdded | NameFormStood:
        """The answer "Yes, this is her name" to a first use or a line not confirmed: the form
        is kept, and the first form sets the key check. A form already confirmed writes nothing;
        any other question about the line, a replaced key's included, is ``AnswerNotAsked``."""
        confirmer = _confirmer(role)
        try:
            with self._lock, self._writing():
                student_id, check, forms = self._her_name_record()
                identity = identity_among(key, student_line, check=check, forms=forms)
                if identity.form is None:
                    raise AnswerNotAsked(identity)
                if identity.status is IdentityStatus.MATCHES:
                    return NameFormStood(identity.form)
                if identity.status not in (IdentityStatus.FIRST_USE, IdentityStatus.NOT_CONFIRMED):
                    raise AnswerNotAsked(identity)
                with all_or_none(self._connection):
                    self._connection.execute(
                        ADD_FORM, (student_id, identity.form, confirmer, self._stamp())
                    )
                    if check is None:
                        self._connection.execute(SET_KEY_CHECK, (key_check(key), student_id))
                return NameFormAdded(identity.form)
        except sqlite3.Error as error:
            msg = f"the name form could not be saved: {type(error).__name__}"
            raise NameFormNotSaved(msg) from error

    def confirm_name_again(
        self, key: bytes, student_line: str, role: ConfirmedBy
    ) -> NameConfirmedAgain | NameFormStood:
        """The answer "Yes, this is her name" after the secret was replaced: her forms are
        replaced by this line's, and the key check by one under ``key``, together. A line that
        already matches writes nothing; any other question is ``AnswerNotAsked``."""
        confirmer = _confirmer(role)
        try:
            with self._lock, self._writing():
                student_id, check, forms = self._her_name_record()
                identity = identity_among(key, student_line, check=check, forms=forms)
                if identity.form is None:
                    raise AnswerNotAsked(identity)
                if identity.status is IdentityStatus.MATCHES:
                    return NameFormStood(identity.form)
                if identity.status is not IdentityStatus.CONFIRM_AGAIN:
                    raise AnswerNotAsked(identity)
                with all_or_none(self._connection):
                    self._connection.execute(DROP_HER_FORMS, (student_id,))
                    self._connection.execute(
                        ADD_FORM, (student_id, identity.form, confirmer, self._stamp())
                    )
                    self._connection.execute(SET_KEY_CHECK, (key_check(key), student_id))
                return NameConfirmedAgain(identity.form)
        except sqlite3.Error as error:
            msg = f"her name could not be confirmed again: {type(error).__name__}"
            raise NameFormNotSaved(msg) from error

    def _stamp(self) -> str:
        """Now, in UTC, as the record writes a moment."""
        return self._clock.now().astimezone(UTC).isoformat()


def _confirmer(role: str) -> ConfirmedBy:
    """``role`` when it may confirm her name, or ``ValueError``."""
    if role not in get_args(ConfirmedBy):
        msg = "only a parent, or the household with the sign-in off, confirms her name"
        raise ValueError(msg)
    return cast(ConfirmedBy, role)
