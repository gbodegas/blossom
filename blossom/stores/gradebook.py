# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The part of the record's store that keeps her grade reports: her student record, the name
forms a parent confirmed as hers, and the reports a parent accepted, with what each save did.

Mixed into the store of the record, which supplies the connection, the lock, the clock, and the
transaction that reserves the writer before it reads. Her record is one row, the schema allows
no second: a random ID, made at the first start that opens the file with these tables and never
changed, and the key check, empty until the first confirmed form. A confirmed form is a keyed
hash of a student line, never the line. Every row carries her student ID, and nothing is read
under another. Nothing here changes any other table.

A save of a report is one transaction: a recorded acceptance ID returns what it recorded, a
changed class and term or an answer to a question the review doesn't ask now returns the
review, and otherwise the answers, the selected values and an acceptance record are written
together. A selected row matched to one of her results adds its observation to that result; any
other makes a new result. Current values are read, never stored: each target's comes from the
current report with the highest acceptance order that supplied it.
"""

import json
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Collection, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC
from typing import Final, Literal, cast, get_args

from blossom.clock import Clock
from blossom.grades.draft import (
    GradeReportDraft,
    GradeRow,
    GradeValue,
    Presence,
    ReportHeader,
    folded,
    row_evidence,
)
from blossom.grades.identity import Identity, IdentityStatus, identity_among, key_check
from blossom.grades.review import (
    CATEGORY_FIELDS,
    NOTHING_HELD,
    RESULT_FIELDS,
    TERM_FIELDS,
    TERM_KEY,
    AlreadyRecorded,
    Cell,
    ClassRecord,
    Compared,
    CurrentValue,
    CurrentValues,
    GradeAnswers,
    GradeReportSaved,
    GradeReview,
    IdentityAnswer,
    ItemStatus,
    MatchAnswer,
    NotHers,
    OnRecord,
    ReturnReason,
    ReviewPage,
    ReviewReturned,
    SaveOutcome,
    answers_asked,
    category_keys,
    compared,
    evidence_text,
    matches_asked,
    review_from,
    row_key,
)

GRADEBOOK_TABLES: Final = (
    "grade_student",
    "grade_name_forms",
    "grade_context",
    "grade_years",
    "grade_terms",
    "grade_classes",
    "grade_class_aliases",
    "grade_reports",
    "grade_term_observations",
    "grade_category_observations",
    "grade_results",
    "grade_result_observations",
    "grade_match_decisions",
    "grade_scope_revisions",
    "grade_acceptances",
)
"""Every table a grade write may change. Every other table of the file, and the checkpoint and
trace files, are a closed world no grade write touches."""
TEXT_KIND: Final = "grade_text"
"""The kind of an acceptance of a pasted report, whose source key is its capture key."""

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

WHO: Final = "IN ('parent', 'household')"
CREATE_REPORT_TABLES: Final = (
    f"""
CREATE TABLE IF NOT EXISTS grade_context (
    student_id TEXT PRIMARY KEY,
    year_label TEXT NOT NULL,
    term_label TEXT NOT NULL,
    set_by TEXT NOT NULL CHECK (set_by {WHO}),
    set_at TEXT NOT NULL
)
""",
    f"""
CREATE TABLE IF NOT EXISTS grade_years (
    student_id TEXT NOT NULL,
    label TEXT NOT NULL,
    first_month INTEGER CHECK (first_month BETWEEN 1 AND 12),
    first_month_by TEXT CHECK (first_month_by {WHO}),
    made_at TEXT NOT NULL,
    PRIMARY KEY (student_id, label),
    CHECK ((first_month IS NULL) = (first_month_by IS NULL))
)
""",
    """
CREATE TABLE IF NOT EXISTS grade_terms (
    student_id TEXT NOT NULL,
    year_label TEXT NOT NULL,
    label TEXT NOT NULL,
    made_at TEXT NOT NULL,
    PRIMARY KEY (student_id, year_label, label)
)
""",
    f"""
CREATE TABLE IF NOT EXISTS grade_classes (
    class_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    year_label TEXT NOT NULL,
    display_name TEXT NOT NULL,
    made_by TEXT NOT NULL CHECK (made_by {WHO}),
    made_at TEXT NOT NULL
)
""",
    f"""
CREATE TABLE IF NOT EXISTS grade_class_aliases (
    student_id TEXT NOT NULL,
    year_label TEXT NOT NULL,
    matched_as TEXT NOT NULL,
    class_id TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    added_by TEXT NOT NULL CHECK (added_by {WHO}),
    added_at TEXT NOT NULL,
    PRIMARY KEY (student_id, year_label, matched_as)
)
""",
    """
CREATE TABLE IF NOT EXISTS grade_reports (
    report_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    class_id TEXT NOT NULL,
    term_label TEXT NOT NULL,
    source_key TEXT NOT NULL,
    acceptance_order INTEGER NOT NULL CHECK (acceptance_order >= 1),
    use TEXT NOT NULL CHECK (use IN ('current', 'earlier')),
    reader TEXT NOT NULL CHECK (reader IN ('text', 'screenshot')),
    imported_at TEXT NOT NULL,
    as_of TEXT,
    coverage TEXT NOT NULL CHECK (coverage IN ('full', 'partial')),
    UNIQUE (student_id, class_id, term_label, acceptance_order)
)
""",
    """
CREATE TABLE IF NOT EXISTS grade_term_observations (
    report_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    percent_text TEXT NOT NULL,
    percent_presence TEXT NOT NULL,
    letter_text TEXT NOT NULL,
    letter_presence TEXT NOT NULL
)
""",
    """
CREATE TABLE IF NOT EXISTS grade_category_observations (
    report_id TEXT NOT NULL,
    category_key TEXT NOT NULL,
    student_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    name_text TEXT NOT NULL,
    name_presence TEXT NOT NULL,
    weight_text TEXT NOT NULL,
    weight_presence TEXT NOT NULL,
    average_text TEXT NOT NULL,
    average_presence TEXT NOT NULL,
    PRIMARY KEY (report_id, category_key)
)
""",
    """
CREATE TABLE IF NOT EXISTS grade_results (
    result_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    class_id TEXT NOT NULL,
    term_label TEXT NOT NULL,
    made_at TEXT NOT NULL
)
""",
    """
CREATE TABLE IF NOT EXISTS grade_result_observations (
    report_id TEXT NOT NULL,
    result_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    category_text TEXT NOT NULL,
    category_presence TEXT NOT NULL,
    assignment_text TEXT NOT NULL,
    assignment_presence TEXT NOT NULL,
    points_text TEXT NOT NULL,
    points_presence TEXT NOT NULL,
    max_points_text TEXT NOT NULL,
    max_points_presence TEXT NOT NULL,
    average_text TEXT NOT NULL,
    average_presence TEXT NOT NULL,
    status_text TEXT NOT NULL,
    status_presence TEXT NOT NULL,
    due_text TEXT NOT NULL,
    due_presence TEXT NOT NULL,
    curve_text TEXT NOT NULL,
    curve_presence TEXT NOT NULL,
    bonus_text TEXT NOT NULL,
    bonus_presence TEXT NOT NULL,
    penalty_text TEXT NOT NULL,
    penalty_presence TEXT NOT NULL,
    weight_text TEXT NOT NULL,
    weight_presence TEXT NOT NULL,
    note_text TEXT NOT NULL,
    note_presence TEXT NOT NULL,
    PRIMARY KEY (report_id, result_id)
)
""",
    f"""
CREATE TABLE IF NOT EXISTS grade_match_decisions (
    report_id TEXT NOT NULL,
    row_key TEXT NOT NULL,
    student_id TEXT NOT NULL,
    evidence TEXT NOT NULL,
    occurrence INTEGER NOT NULL CHECK (occurrence >= 1),
    result_id TEXT NOT NULL,
    how TEXT NOT NULL CHECK (how IN ('new', 'same_capture', 'exact', 'answer')),
    decided_by TEXT NOT NULL CHECK (decided_by {WHO}),
    decided_at TEXT NOT NULL,
    PRIMARY KEY (report_id, row_key)
)
""",
    """
CREATE TABLE IF NOT EXISTS grade_scope_revisions (
    student_id TEXT NOT NULL,
    class_id TEXT NOT NULL,
    term_label TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    PRIMARY KEY (student_id, class_id, term_label)
)
""",
    f"""
CREATE TABLE IF NOT EXISTS grade_acceptances (
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
    accepted_at TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role {WHO})
)
""",
)
"""The tables a save of a report writes, each row carrying her student ID."""

CONTEXT_ON_RECORD: Final = "SELECT 1 FROM grade_context WHERE student_id = ?"
YEAR_ON_RECORD: Final = "SELECT 1 FROM grade_years WHERE student_id = ? AND label = ?"
ALIAS_MATCHED: Final = (
    "SELECT class_id FROM grade_class_aliases "
    "WHERE student_id = ? AND year_label = ? AND matched_as = ?"
)
CLASSES_OF_YEAR: Final = (
    "SELECT class_id, display_name FROM grade_classes WHERE student_id = ? AND year_label = ? "
    "ORDER BY display_name, class_id"
)
REVISION_OF: Final = (
    "SELECT revision FROM grade_scope_revisions "
    "WHERE student_id = ? AND class_id = ? AND term_label = ?"
)
SCOPE_REPORTS: Final = (
    "SELECT report_id, acceptance_order, use, coverage FROM grade_reports "
    "WHERE student_id = ? AND class_id = ? AND term_label = ?"
)
TERMS_OBSERVED: Final = (
    "SELECT o.report_id, o.percent_text, o.percent_presence, o.letter_text, o.letter_presence "
    "FROM grade_term_observations AS o JOIN grade_reports AS r "
    "ON r.report_id = o.report_id AND r.student_id = o.student_id "
    "WHERE r.student_id = ? AND r.class_id = ? AND r.term_label = ?"
)
CATEGORIES_OBSERVED: Final = (
    "SELECT o.report_id, o.category_key, o.name_text, o.name_presence, o.weight_text, "
    "o.weight_presence, o.average_text, o.average_presence "
    "FROM grade_category_observations AS o JOIN grade_reports AS r "
    "ON r.report_id = o.report_id AND r.student_id = o.student_id "
    "WHERE r.student_id = ? AND r.class_id = ? AND r.term_label = ? ORDER BY o.position"
)
RESULTS_OBSERVED: Final = (
    "SELECT o.report_id, o.result_id, o.category_text, o.category_presence, o.assignment_text, "
    "o.assignment_presence, o.points_text, o.points_presence, o.max_points_text, "
    "o.max_points_presence, o.average_text, o.average_presence, o.status_text, "
    "o.status_presence, o.due_text, o.due_presence, o.curve_text, o.curve_presence, "
    "o.bonus_text, o.bonus_presence, o.penalty_text, o.penalty_presence, o.weight_text, "
    "o.weight_presence, o.note_text, o.note_presence "
    "FROM grade_result_observations AS o JOIN grade_reports AS r "
    "ON r.report_id = o.report_id AND r.student_id = o.student_id "
    "WHERE r.student_id = ? AND r.class_id = ? AND r.term_label = ? ORDER BY o.position"
)
DECIDED: Final = (
    "SELECT o.report_id, o.evidence, o.result_id FROM grade_match_decisions AS o "
    "JOIN grade_reports AS r ON r.report_id = o.report_id AND r.student_id = o.student_id "
    "WHERE r.student_id = ? AND r.class_id = ? AND r.term_label = ?"
)
ROW_DECIDED: Final = (
    "SELECT 1 FROM grade_match_decisions WHERE student_id = ? AND report_id = ? AND row_key = ?"
)
ACCEPTED_FROM_SOURCE: Final = (
    "SELECT accepted FROM grade_acceptances WHERE student_id = ? AND kind = ? AND source_key = ?"
)
RECORDED: Final = (
    "SELECT source_key, report_id, accepted, added, updated, already_saved, left_to_check "
    "FROM grade_acceptances WHERE student_id = ? AND acceptance_id = ?"
)
SET_CONTEXT: Final = (
    "INSERT INTO grade_context (student_id, year_label, term_label, set_by, set_at) "
    "VALUES (?, ?, ?, ?, ?)"
)
ADD_YEAR: Final = (
    "INSERT INTO grade_years (student_id, label, first_month, first_month_by, made_at) "
    "VALUES (?, ?, ?, ?, ?)"
)
ADD_TERM: Final = (
    "INSERT OR IGNORE INTO grade_terms (student_id, year_label, label, made_at) VALUES (?, ?, ?, ?)"
)
ADD_CLASS: Final = (
    "INSERT INTO grade_classes (class_id, student_id, year_label, display_name, made_by, made_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
ADD_ALIAS: Final = (
    "INSERT INTO grade_class_aliases "
    "(student_id, year_label, matched_as, class_id, code, name, added_by, added_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
LATEST_OF_CAPTURE: Final = (
    "SELECT report_id FROM grade_reports "
    "WHERE student_id = ? AND class_id = ? AND term_label = ? AND source_key = ? "
    "ORDER BY acceptance_order DESC LIMIT 1"
)
NEXT_ORDER: Final = (
    "SELECT COALESCE(MAX(acceptance_order), 0) + 1 FROM grade_reports "
    "WHERE student_id = ? AND class_id = ? AND term_label = ?"
)
ADD_REPORT: Final = (
    "INSERT INTO grade_reports (report_id, student_id, class_id, term_label, source_key, "
    "acceptance_order, use, reader, imported_at, as_of, coverage) "
    "VALUES (?, ?, ?, ?, ?, ?, 'current', 'text', ?, NULL, ?)"
)
ADD_TERM_OBSERVATION: Final = (
    "INSERT INTO grade_term_observations (report_id, student_id, percent_text, "
    "percent_presence, letter_text, letter_presence) VALUES (?, ?, ?, ?, ?, ?)"
)
ADD_CATEGORY_OBSERVATION: Final = (
    "INSERT INTO grade_category_observations (report_id, category_key, student_id, position, "
    "name_text, name_presence, weight_text, weight_presence, average_text, average_presence) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
ADD_RESULT: Final = (
    "INSERT INTO grade_results (result_id, student_id, class_id, term_label, made_at) "
    "VALUES (?, ?, ?, ?, ?)"
)
ADD_RESULT_OBSERVATION: Final = (
    "INSERT INTO grade_result_observations (report_id, result_id, student_id, position, "
    "category_text, category_presence, assignment_text, assignment_presence, points_text, "
    "points_presence, max_points_text, max_points_presence, average_text, average_presence, "
    "status_text, status_presence, due_text, due_presence, curve_text, curve_presence, "
    "bonus_text, bonus_presence, penalty_text, penalty_presence, weight_text, weight_presence, "
    "note_text, note_presence) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
ADD_MATCH_DECISION: Final = (
    "INSERT INTO grade_match_decisions (report_id, row_key, student_id, evidence, occurrence, "
    "result_id, how, decided_by, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
RAISE_REVISION: Final = (
    "INSERT INTO grade_scope_revisions (student_id, class_id, term_label, revision) "
    "VALUES (?, ?, ?, 1) "
    "ON CONFLICT (student_id, class_id, term_label) DO UPDATE SET revision = revision + 1"
)
ADD_ACCEPTANCE: Final = (
    "INSERT INTO grade_acceptances (acceptance_id, student_id, kind, source_key, report_id, "
    "accepted, identity_status, identity_answer, identity_form, added, updated, already_saved, "
    "left_to_check, accepted_at, role) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def new_acceptance_id() -> str:
    """A one-time ID for a review page, which a save of that page is recorded under."""
    return f"acceptance-{uuid.uuid4().hex}"


def matched_as(header: ReportHeader) -> str:
    """How a class alias is matched: the class code and name as written, with spaces and case
    folded."""

    def fold(text: str | None) -> str | None:
        return None if text is None else folded(text).casefold()

    return json.dumps([fold(header.class_code), fold(header.class_name)], ensure_ascii=False)


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


class GradeReportNotSaved(RuntimeError):
    """The file refused a save of a report; the whole save was rolled back with it."""


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

    def comparing_and_writing(self) -> AbstractContextManager[None]:
        """The store's lock and its reserved writer, which the store of the record supplies."""
        raise NotImplementedError

    def _create_gradebook_tables(self) -> None:
        """The tables, and her record on a file without one, in the caller's transaction."""
        self._connection.execute(CREATE_STUDENT)
        self._connection.execute(CREATE_NAME_FORMS)
        for statement in CREATE_REPORT_TABLES:
            self._connection.execute(statement)
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

    def review_grade_report(
        self, draft: GradeReportDraft, source_key: str, *, key: bytes
    ) -> GradeReview:
        """What saving ``draft`` would do, under a fresh acceptance ID: the identity of its
        line, its setup questions, the scope revision, and each value's status. A read alone."""
        with self._lock:
            return self._review_locked(draft, source_key, key, same_class=None)

    def current_values(self, class_id: str, term: str) -> CurrentValues:
        """Each target's current value in her class and term: from the current report with the
        highest acceptance order that supplied it, with that report and its order. A read alone."""
        with self._lock:
            student_id = self._her_name_record()[0]
            return self._class_record(student_id, class_id, term).current

    def save_grade_report(
        self,
        draft: GradeReportDraft,
        source_key: str,
        *,
        key: bytes,
        page: ReviewPage,
        answers: GradeAnswers,
        selection: Collection[str],
        role: ConfirmedBy,
    ) -> SaveOutcome:
        """The parent's save of the values ``selection`` names, in one transaction. A recorded
        acceptance ID returns its outcome; a changed revision, an answer to no question asked
        now, or a value neither New nor Changed returns the review; each of these writes
        nothing."""
        confirmer = _confirmer(role)
        chosen = frozenset(selection)
        try:
            with self.comparing_and_writing():
                student_id = self._her_name_record()[0]
                recorded = self._recorded(student_id, page.acceptance_id)
                if recorded is not None:
                    outcome, recorded_source = recorded
                    covered = {item_key for item_key, _ in outcome.accepted}
                    if recorded_source != source_key:
                        covered = set()
                    return AlreadyRecorded(outcome, chosen - covered)
                if answers.identity is IdentityAnswer.NOT_HERS:
                    return NotHers()
                review = self._review_locked(draft, source_key, key, same_class=None)
                into = review
                if answers.same_class is not None:
                    into = self._review_locked(
                        draft, source_key, key, same_class=answers.same_class
                    )
                if review.revision != page.revision:
                    return ReviewReturned(into, ReturnReason.REVISION)
                if not answers_asked(review, answers) or not matches_asked(into, answers.matches):
                    return ReviewReturned(into, ReturnReason.ANSWERS)
                settled = into
                if answers.matches:
                    settled = self._review_locked(
                        draft,
                        source_key,
                        key,
                        same_class=answers.same_class,
                        matches=answers.matches,
                    )
                if not chosen <= settled.ready:
                    return ReviewReturned(into, ReturnReason.SELECTION)
                return self._write_save(
                    draft,
                    settled,
                    answers,
                    chosen,
                    key=key,
                    by=confirmer,
                    student_id=student_id,
                    acceptance_id=page.acceptance_id,
                )
        except (sqlite3.Error, NameFormNotSaved) as error:
            msg = f"the grade report could not be saved: {type(error).__name__}"
            raise GradeReportNotSaved(msg) from error

    def _review_locked(
        self,
        draft: GradeReportDraft,
        source_key: str,
        key: bytes,
        *,
        same_class: str | None,
        matches: tuple[MatchAnswer, ...] = (),
    ) -> GradeReview:
        """The review, the values' statuses read in the class an alias matched, or else in
        ``same_class`` when it is one of the year's classes, with ``matches`` applied."""
        header = draft.header
        student_id, check, forms = self._her_name_record()
        identity = identity_among(key, header.student_line, check=check, forms=forms)
        year, term = header.year_label, header.term_label
        alias = self._connection.execute(
            ALIAS_MATCHED, (student_id, year, matched_as(header))
        ).fetchone()
        matched = None if alias is None else str(alias[0])
        existing = tuple(
            (str(row[0]), str(row[1]))
            for row in self._connection.execute(CLASSES_OF_YEAR, (student_id, year))
        )
        revision = None
        if matched is not None:
            held = self._connection.execute(REVISION_OF, (student_id, matched, term)).fetchone()
            revision = None if held is None else int(held[0])
        reviewed = matched
        if reviewed is None and same_class in {class_id for class_id, _ in existing}:
            reviewed = same_class
        held = NOTHING_HELD
        if reviewed is not None:
            held = self._class_record(student_id, reviewed, term)
        saved: dict[str, str | None] = {}
        for (accepted,) in self._connection.execute(
            ACCEPTED_FROM_SOURCE, (student_id, TEXT_KIND, source_key)
        ):
            for item_key, result_id in json.loads(accepted):
                saved[str(item_key)] = None if result_id is None else str(result_id)
        on_record = OnRecord(
            identity=identity,
            context=self._connection.execute(CONTEXT_ON_RECORD, (student_id,)).fetchone()
            is not None,
            year_known=self._connection.execute(YEAR_ON_RECORD, (student_id, year)).fetchone()
            is not None,
            matched=matched,
            existing=existing,
            revision=revision,
            held=held,
            saved=saved,
        )
        return review_from(draft, source_key, new_acceptance_id(), on_record, matches)

    def _class_record(self, student_id: str, class_id: str, term: str) -> ClassRecord:
        """What her class and term hold, read under her student ID: each target's current value,
        every accepted value of each target, each result's latest observation, and the results
        stored decisions gave each row evidence."""
        scope = (student_id, class_id, term)
        reports = {
            str(report_id): (int(order), str(use), str(coverage))
            for report_id, order, use, coverage in self._connection.execute(SCOPE_REPORTS, scope)
        }
        observed: list[tuple[str, str, str, dict[str, Cell]]] = []
        for row in self._connection.execute(TERMS_OBSERVED, scope):
            observed.append(("term", TERM_KEY, str(row[0]), _cells(TERM_FIELDS, row[1:])))
        for row in self._connection.execute(CATEGORIES_OBSERVED, scope):
            observed.append(
                ("category", str(row[1]), str(row[0]), _cells(CATEGORY_FIELDS, row[2:]))
            )
        for row in self._connection.execute(RESULTS_OBSERVED, scope):
            observed.append(("result", str(row[1]), str(row[0]), _cells(RESULT_FIELDS, row[2:])))
        observed.sort(key=lambda one: reports[one[2]][0])
        current: dict[str, dict[str, CurrentValue]] = {"term": {}, "category": {}, "result": {}}
        accepted: dict[str, set[Compared]] = {}
        latest: dict[str, CurrentValue] = {}
        held_by: dict[str, set[str]] = {}
        for kind, target, report_id, cells in observed:
            order, use, _ = reports[report_id]
            value = CurrentValue(cells, report_id, order)
            accepted.setdefault(target, set()).add(compared(cells))
            if kind == "result":
                latest[target] = value
                held_by.setdefault(report_id, set()).add(target)
            if use == "current":
                current[kind][target] = value
        decided: dict[str, set[str]] = {}
        for report_id, evidence, result in self._connection.execute(DECIDED, scope):
            decided.setdefault(str(evidence), set()).add(str(result))
            held_by.setdefault(str(report_id), set()).add(str(result))
        newer_full = [
            (order, report_id)
            for report_id, (order, use, coverage) in reports.items()
            if use == "current" and coverage == "full"
        ]
        for result, value in current["result"].items():
            if any(
                order > value.order and result not in held_by.get(report_id, set())
                for order, report_id in newer_full
            ):
                current["result"][result] = replace(value, last_seen=True)
        return ClassRecord(
            current=CurrentValues(
                term=current["term"].get(TERM_KEY),
                categories=current["category"],
                results=current["result"],
            ),
            accepted={target: frozenset(values) for target, values in accepted.items()},
            latest=latest,
            decided={evidence: frozenset(results) for evidence, results in decided.items()},
        )

    def _recorded(self, student_id: str, acceptance_id: str) -> tuple[GradeReportSaved, str] | None:
        """The outcome recorded under ``acceptance_id`` for her, with its source key."""
        row = self._connection.execute(RECORDED, (student_id, acceptance_id)).fetchone()
        if row is None:
            return None
        source_key, report_id, accepted, added, updated, already_saved, left = row
        outcome = GradeReportSaved(
            acceptance_id=acceptance_id,
            report_id=report_id,
            added=added,
            updated=updated,
            already_saved=already_saved,
            left=left,
            accepted=tuple(
                (str(item_key), None if result_id is None else str(result_id))
                for item_key, result_id in json.loads(accepted)
            ),
        )
        return outcome, str(source_key)

    def _write_save(
        self,
        draft: GradeReportDraft,
        review: GradeReview,
        answers: GradeAnswers,
        chosen: frozenset[str],
        *,
        key: bytes,
        by: ConfirmedBy,
        student_id: str,
        acceptance_id: str,
    ) -> GradeReportSaved:
        """Every write of a save that passed its checks, in the caller's transaction: the
        identity answer, the setup, the selected values, the revision and the acceptance."""
        header = draft.header
        now = self._stamp()
        line = header.student_line
        if answers.identity is IdentityAnswer.HERS and line is not None:
            if review.identity.status is IdentityStatus.CONFIRM_AGAIN:
                self.confirm_name_again(key, line, by)
            else:
                self.add_name_form(key, line, by)
        year, term = header.year_label, header.term_label
        if answers.setup is not None:
            current_year, current_term = answers.setup
            self._connection.execute(
                SET_CONTEXT, (student_id, current_year, folded(current_term), by, now)
            )
        if review.first_month is not None:
            month = None if answers.first_month is None else answers.first_month[1]
            self._connection.execute(
                ADD_YEAR, (student_id, year, month, None if month is None else by, now)
            )
        self._connection.execute(ADD_TERM, (student_id, year, term, now))
        class_id = self._class_of(header, review, answers, student_id=student_id, by=by, now=now)
        report_id = None
        accepted: list[tuple[str, str | None]] = []
        if chosen:
            report_id = self._report_of(draft, class_id, review.source_key, student_id, now)
            accepted = self._observe(
                draft, review, chosen, report_id, class_id, student_id=student_id, by=by, now=now
            )
        self._connection.execute(RAISE_REVISION, (student_id, class_id, term))
        already = sum(1 for item in review.items if item.status in ALREADY)
        changed = sum(
            1 for item in review.items if item.key in chosen and item.status is ItemStatus.CHANGED
        )
        outcome = GradeReportSaved(
            acceptance_id=acceptance_id,
            report_id=report_id,
            added=len(chosen) - changed,
            updated=changed,
            already_saved=already,
            left=len(review.items) - len(chosen) - already,
            accepted=tuple(sorted(accepted, key=lambda pair: pair[0])),
        )
        self._connection.execute(
            ADD_ACCEPTANCE,
            (
                acceptance_id,
                student_id,
                TEXT_KIND,
                review.source_key,
                report_id,
                json.dumps([list(pair) for pair in outcome.accepted], ensure_ascii=False),
                review.identity.status.value,
                answers.identity.value,
                review.identity.form,
                outcome.added,
                outcome.updated,
                outcome.already_saved,
                outcome.left,
                now,
                by,
            ),
        )
        return outcome

    def _class_of(
        self,
        header: ReportHeader,
        review: GradeReview,
        answers: GradeAnswers,
        *,
        student_id: str,
        by: ConfirmedBy,
        now: str,
    ) -> str:
        """The class an alias matched; or the class the answer names, the report's code and name
        kept as its alias, a new class made for a display name."""
        if review.class_question.matched is not None:
            return review.class_question.matched
        if answers.same_class is not None:
            class_id = answers.same_class
        else:
            class_id = f"class-{uuid.uuid4().hex}"
            name = folded(answers.new_class or "")
            self._connection.execute(
                ADD_CLASS, (class_id, student_id, header.year_label, name, by, now)
            )
        self._connection.execute(
            ADD_ALIAS,
            (
                student_id,
                header.year_label,
                matched_as(header),
                class_id,
                header.class_code,
                header.class_name,
                by,
                now,
            ),
        )
        return class_id

    def _report_of(
        self, draft: GradeReportDraft, class_id: str, source_key: str, student_id: str, now: str
    ) -> str:
        """The capture's latest report in the class and term, which its rest joins; or a new
        report, next in acceptance order, current."""
        term = draft.header.term_label
        latest = self._connection.execute(
            LATEST_OF_CAPTURE, (student_id, class_id, term, source_key)
        ).fetchone()
        if latest is not None:
            return str(latest[0])
        (order,) = self._connection.execute(NEXT_ORDER, (student_id, class_id, term)).fetchone()
        report_id = f"report-{uuid.uuid4().hex}"
        coverage = "partial" if draft.term.percent.presence is Presence.NOT_CAPTURED else "full"
        self._connection.execute(
            ADD_REPORT, (report_id, student_id, class_id, term, source_key, order, now, coverage)
        )
        return report_id

    def _observe(
        self,
        draft: GradeReportDraft,
        review: GradeReview,
        chosen: frozenset[str],
        report_id: str,
        class_id: str,
        *,
        student_id: str,
        by: ConfirmedBy,
        now: str,
    ) -> list[tuple[str, str | None]]:
        """The selected values as observations of ``report_id``; each row an observation of the
        result it resolved to, or of a new one, with its match decision: each accepted key with
        the result it resolved to."""
        resolved = {item.key: item for item in review.rows}
        accepted: list[tuple[str, str | None]] = []
        if TERM_KEY in chosen:
            percent, letter = draft.term.percent, draft.term.letter
            self._connection.execute(
                ADD_TERM_OBSERVATION,
                (report_id, student_id, *_cell(percent), *_cell(letter)),
            )
            accepted.append((TERM_KEY, None))
        for position, (item_key, category) in enumerate(
            zip(category_keys(draft), draft.categories, strict=True), start=1
        ):
            if item_key in chosen:
                self._connection.execute(
                    ADD_CATEGORY_OBSERVATION,
                    (
                        report_id,
                        item_key,
                        student_id,
                        position,
                        *_cell(category.name),
                        *_cell(category.weight),
                        *_cell(category.average),
                    ),
                )
                accepted.append((item_key, None))
        rows = [(category, row) for category in draft.categories for row in category.rows]
        for position, (category, row) in enumerate(rows, start=1):
            item_key = row_key(category.name, row)
            item = resolved[item_key]
            decision = (report_id, item_key, student_id, category.name, row)
            if item_key not in chosen:
                shown, how = item.result_id, item.how
                repeated = item.status in ALREADY and not self._decided(*decision[:3])
                if repeated and shown is not None and how is not None:
                    self._decide(*decision, shown, how, by=by, now=now)
                continue
            result_id = item.result_id
            if result_id is None:
                result_id = f"result-{uuid.uuid4().hex}"
                term = draft.header.term_label
                self._connection.execute(ADD_RESULT, (result_id, student_id, class_id, term, now))
            cells = [text for value in (category.name, *row.cells()) for text in _cell(value)]
            self._connection.execute(
                ADD_RESULT_OBSERVATION, (report_id, result_id, student_id, position, *cells)
            )
            self._decide(*decision, result_id, item.how or "new", by=by, now=now)
            accepted.append((item_key, result_id))
        return accepted

    def _decided(self, report_id: str, item_key: str, student_id: str) -> bool:
        """Whether the row already has its match decision in ``report_id``."""
        held = self._connection.execute(ROW_DECIDED, (student_id, report_id, item_key))
        return held.fetchone() is not None

    def _decide(
        self,
        report_id: str,
        item_key: str,
        student_id: str,
        category_name: GradeValue,
        row: GradeRow,
        result_id: str,
        how: str,
        *,
        by: ConfirmedBy,
        now: str,
    ) -> None:
        """A row's match decision in ``report_id``: its evidence and occurrence, the result it
        resolved to, and how. A row the report repeats unchanged gets one without an observation,
        so the report counts as showing that result."""
        evidence = evidence_text(row_evidence(category_name, row))
        self._connection.execute(
            ADD_MATCH_DECISION,
            (report_id, item_key, student_id, evidence, row.occurrence, result_id, how, by, now),
        )

    def _stamp(self) -> str:
        """Now, in UTC, as the record writes a moment."""
        return self._clock.now().astimezone(UTC).isoformat()


def _cell(value: GradeValue) -> tuple[str, str]:
    """A value as an observation keeps it: its text as written, and its presence."""
    return value.text, value.presence.value


def _cells(fields: tuple[str, ...], row: tuple[object, ...]) -> dict[str, Cell]:
    """An observation's cells by field, from its text and presence columns in turn."""
    return {
        field: (Presence(str(row[2 * at + 1])), str(row[2 * at])) for at, field in enumerate(fields)
    }


ALREADY: Final = frozenset({ItemStatus.SAVED, ItemStatus.MATCHES_EARLIER})
"""The statuses an outcome counts as already saved."""


def _confirmer(role: str) -> ConfirmedBy:
    """``role`` when it may confirm her name, or ``ValueError``."""
    if role not in get_args(ConfirmedBy):
        msg = "only a parent, or the household with the sign-in off, confirms her name"
        raise ValueError(msg)
    return cast(ConfirmedBy, role)
