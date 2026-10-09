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

A save of a report is one write: a recorded acceptance ID returns what it recorded, a
changed class and term or an answer to a question the review doesn't ask now returns the
review, and otherwise the answers, the selected values, a row record for each row shown with a
reliable match, and an acceptance record are written together. A selected row matched to one of
her results adds its observation to that result; any other makes a new result. A new report
takes the parent's report-level choice, or its default, as its use; a capture's rest joins its
latest report with that report's use. Current values are read, never stored: each target's comes
from the current report with the highest acceptance order that supplied it, and a result's also
says the newest current report that showed it.

Every grade write goes through one entry: the store's lock, the writer's transaction (its own,
or a caller's it joins), and one savepoint around the whole write. A failure leaves none of the
write behind, inside a caller's transaction too, and the caller's other work there stays. When
SQLite itself ends a caller's transaction, everything in it is gone, and the caller hears that
its transaction was lost, never a refusal it might go on from.

A parent deletes one class's grades for one term in one write checked against the revision the
confirmation showed: its reports, observations, row records, results, acceptances and
class-details actions go, one statement per table, and the revision is raised, so a page from
before saves nothing. A retry is answered from the class and term as they are: already deleted
when they hold nothing, or a fresh preview, deleting nothing, after a newer import.

A school year's first month is corrected by comparing and setting the month itself, the year's
whole state. Nothing stored or compared depends on it, so no revision moves; due dates resolve
under the month on record when they are read.

A parent may assert a saved value, in a closed form only, or withdraw that assertion. Each is an
added record on the original reading, never a change to it: the observation keeps the school's
value as read, and the record keeps that cell, who and when. The assertion shows beside the
original and every copy, never in the review or any comparison, and the class and term's delete
removes its history with the rest.
"""

import json
import re
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Callable, Collection, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Final, Literal, cast, get_args

from blossom.clock import Clock
from blossom.grades.draft import (
    NUMBER,
    GradeReportDraft,
    GradeRow,
    GradeValue,
    Presence,
    ReportHeader,
    folded,
    row_evidence,
)
from blossom.grades.identity import Identity, IdentityStatus, identity_among, key_check
from blossom.grades.projection import (
    EXPLICIT,
    ActionOutcome,
    ActionRecorded,
    Copies,
    CurrentPage,
    CurrentPreview,
    Decided,
    MadeCurrent,
    NothingToChange,
    Observed,
    PreviewRevised,
    ReportNotSaved,
    ScopeHeld,
    SourceOf,
    apply_corrections,
    original_of,
    preview_of,
    project,
)
from blossom.grades.review import (
    CATEGORY_FIELDS,
    NOTHING_HELD,
    RESULT_FIELDS,
    TERM_FIELDS,
    TERM_KEY,
    AlreadyRecorded,
    Cell,
    ClassRecord,
    CurrentValues,
    GradeAnswers,
    GradeReportSaved,
    GradeReview,
    IdentityAnswer,
    ItemStatus,
    MatchAnswer,
    NotHers,
    OnRecord,
    RecordedSave,
    ReportUse,
    ReturnReason,
    ReviewPage,
    ReviewReturned,
    SaveOutcome,
    answers_asked,
    category_keys,
    evidence_text,
    matches_asked,
    rejected_text,
    review_from,
    row_key,
    rows_still_asking,
    selectable,
    use_asked,
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
    "grade_current_actions",
    "grade_corrections",
)
"""Every table a grade write may change. Every other table of the file, and the checkpoint and
trace files, are a closed world no grade write touches."""
VIEW_TABLES: Final = ("grade_view_choices",)
"""The tables only a viewer's own page writes: each viewer's remembered term on Grades. They sit
outside ``GRADEBOOK_TABLES``, in the closed world no grade write touches."""
Viewer = Literal["student", "parent", "anyone"]
"""Who a remembered term belongs to: her, a signed-in parent, or anyone with the sign-in off."""
CREATE_VIEW_CHOICES: Final = """
CREATE TABLE IF NOT EXISTS grade_view_choices (
    student_id TEXT NOT NULL,
    viewer TEXT NOT NULL CHECK (viewer IN ('student', 'parent', 'anyone')),
    year_label TEXT NOT NULL,
    term_label TEXT NOT NULL,
    PRIMARY KEY (student_id, viewer)
)
"""
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
    result_rows INTEGER NOT NULL CHECK (result_rows >= 0),
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
    result_id TEXT,
    how TEXT NOT NULL CHECK (
        how IN ('new', 'same_capture', 'exact', 'reused', 'answer', 'chosen', 'different')
    ),
    rejected TEXT,
    decided_by TEXT NOT NULL CHECK (decided_by {WHO}),
    decided_at TEXT NOT NULL,
    PRIMARY KEY (report_id, row_key),
    CHECK ((how = 'different') = (result_id IS NULL)),
    CHECK ((how = 'different') = (rejected IS NOT NULL))
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
    class_id TEXT,
    term_label TEXT,
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
    role TEXT NOT NULL CHECK (role {WHO}),
    CHECK ((kind = 'homework_screenshot') = (class_id IS NULL)),
    CHECK ((kind = 'homework_screenshot') = (term_label IS NULL))
)
""",
    f"""
CREATE TABLE IF NOT EXISTS grade_current_actions (
    action_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    class_id TEXT NOT NULL,
    term_label TEXT NOT NULL,
    source_report TEXT NOT NULL,
    source_acceptances TEXT NOT NULL,
    source_action TEXT,
    report_made TEXT NOT NULL UNIQUE,
    copied TEXT NOT NULL,
    complete_from_source INTEGER NOT NULL CHECK (complete_from_source IN (0, 1)),
    digest TEXT NOT NULL,
    acted_at TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role {WHO})
)
""",
    f"""
CREATE TABLE IF NOT EXISTS grade_corrections (
    correction_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    report_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('term', 'category', 'result')),
    target TEXT NOT NULL,
    field TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    read_text TEXT NOT NULL,
    read_presence TEXT NOT NULL CHECK (read_presence IN ('reported', 'blank', 'unreadable')),
    corrected_text TEXT NOT NULL,
    corrected_presence TEXT NOT NULL CHECK (corrected_presence IN ('reported', 'withdrawn')),
    correction_kind TEXT NOT NULL CHECK (correction_kind IN ('transcription', 'parent_assertion')),
    entered_on TEXT NOT NULL,
    entered_with TEXT,
    corrected_by TEXT NOT NULL CHECK (corrected_by {WHO}),
    corrected_at TEXT NOT NULL,
    UNIQUE (student_id, report_id, kind, target, field, sequence),
    CHECK ((corrected_presence = 'withdrawn') = (corrected_text = '')),
    CHECK (
        (kind = 'term' AND field IN ('percent', 'letter'))
        OR (kind = 'category' AND field IN ('weight', 'average'))
        OR (kind = 'result' AND field IN
            ('points', 'max_points', 'average', 'status', 'curve', 'bonus', 'penalty', 'weight'))
    ),
    CHECK (
        correction_kind = 'parent_assertion'
        OR (read_presence = 'unreadable' AND field NOT IN ('letter', 'status'))
    )
)
""",
)
"""The tables a save of a report, or the class-details action, writes, each row carrying her
student ID.

A report keeps its number of result rows. A row record names the result its row resolved to and
how: automatically (``same_capture``, ``exact``, ``reused``), by the parent's answer (``answer``,
or ``chosen`` from her assignments), or ``new``. A remembered "A different assignment" is
``different``: it names no result, and ``rejected`` keeps the candidates it turned down, each
with its matching evidence. An acceptance keeps its counts of rows recorded as shown and answers
kept, whether its reading was complete, and the class and term it covers, which a homework
screenshot's acceptance has none of. The class-details action keeps its source report,
the acceptances whose report that is and the action that made it, the report it made, what it
copied, the source's completeness as ``complete_from_source``, the digest of the preview it
confirmed, the role and its time. A report is complete when it has at least one acceptance or a
making action, and each of them was.

A correction is added only, on one value field of the original reading (``report_id``, ``kind``,
``target``): its order among that field's records, the cell as read, the value recorded or a
withdrawal, its kind, the report the parent acted on (``entered_on``), the save it came with
(``entered_with``), who and when."""

ACCEPTANCES_TABLE: Final = next(
    statement for statement in CREATE_REPORT_TABLES if "EXISTS grade_acceptances (" in statement
)
"""The acceptance table's one definition, which the rebuild of a file without its class and
term makes again."""
CONTEXT_ON_RECORD: Final = "SELECT 1 FROM grade_context WHERE student_id = ?"
YEAR_ON_RECORD: Final = "SELECT 1 FROM grade_years WHERE student_id = ? AND label = ?"
FIRST_MONTH_OF: Final = "SELECT first_month FROM grade_years WHERE student_id = ? AND label = ?"
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
    "SELECT report_id, acceptance_order, use, result_rows FROM grade_reports "
    "WHERE student_id = ? AND class_id = ? AND term_label = ?"
)
SCOPE_COMPLETE: Final = (
    "SELECT report_id, MIN(complete) FROM ("
    "SELECT r.report_id AS report_id, a.complete AS complete FROM grade_reports AS r "
    "JOIN grade_acceptances AS a ON a.student_id = r.student_id AND (a.report_id = r.report_id "
    "OR (a.report_id IS NULL AND r.reader = 'text' AND a.kind = 'grade_text' "
    "AND a.source_key = r.source_key)) "
    "WHERE r.student_id = :student AND r.class_id = :class AND r.term_label = :term "
    "UNION ALL "
    "SELECT c.report_made, c.complete_from_source FROM grade_current_actions AS c "
    "JOIN grade_reports AS r ON r.report_id = c.report_made AND r.student_id = c.student_id "
    "WHERE r.student_id = :student AND r.class_id = :class AND r.term_label = :term"
    ") GROUP BY report_id"
)
"""Each report's completeness, the one function for every report: the least of every accepted
reading of its capture, those that wrote into it and those that recorded nothing new, before
or after it was made, and its making action's ``complete_from_source``. A report with neither
is missing, which reads incomplete."""
SCOPE_REPORT: Final = (
    "SELECT source_key FROM grade_reports "
    "WHERE student_id = ? AND class_id = ? AND term_label = ? AND report_id = ?"
)
ACCEPTED_INTO: Final = (
    "SELECT acceptance_id FROM grade_acceptances WHERE student_id = ? AND report_id = ? "
    "ORDER BY acceptance_id"
)
MADE_BY: Final = (
    "SELECT action_id FROM grade_current_actions WHERE student_id = ? AND report_made = ?"
)
ACTION_RECORDED: Final = (
    "SELECT source_report, report_made, digest FROM grade_current_actions "
    "WHERE student_id = ? AND action_id = ?"
)
COPIED_OBSERVATIONS: Final = (
    "SELECT 'term', ? FROM grade_term_observations WHERE student_id = ? AND report_id = ? "
    "UNION ALL SELECT 'category', category_key FROM grade_category_observations "
    "WHERE student_id = ? AND report_id = ? "
    "UNION ALL SELECT 'result', result_id FROM grade_result_observations "
    "WHERE student_id = ? AND report_id = ?"
)
COPIED_ROWS: Final = (
    "SELECT row_key, result_id FROM grade_match_decisions "
    "WHERE student_id = ? AND report_id = ? AND result_id IS NOT NULL ORDER BY row_key"
)
COPY_REPORT: Final = (
    "INSERT INTO grade_reports (report_id, student_id, class_id, term_label, source_key, "
    "acceptance_order, use, reader, imported_at, as_of, result_rows) "
    "SELECT ?, student_id, class_id, term_label, source_key, ?, 'current', reader, imported_at, "
    "as_of, result_rows FROM grade_reports WHERE student_id = ? AND report_id = ?"
)
COPY_TERM: Final = (
    "INSERT INTO grade_term_observations (report_id, student_id, percent_text, "
    "percent_presence, letter_text, letter_presence) "
    "SELECT ?, student_id, percent_text, percent_presence, letter_text, letter_presence "
    "FROM grade_term_observations WHERE student_id = ? AND report_id = ?"
)
COPY_CATEGORIES: Final = (
    "INSERT INTO grade_category_observations (report_id, category_key, student_id, position, "
    "name_text, name_presence, weight_text, weight_presence, average_text, average_presence) "
    "SELECT ?, category_key, student_id, position, name_text, name_presence, weight_text, "
    "weight_presence, average_text, average_presence "
    "FROM grade_category_observations WHERE student_id = ? AND report_id = ?"
)
COPY_RESULTS: Final = (
    "INSERT INTO grade_result_observations (report_id, result_id, student_id, position, "
    "category_text, category_presence, assignment_text, assignment_presence, points_text, "
    "points_presence, max_points_text, max_points_presence, average_text, average_presence, "
    "status_text, status_presence, due_text, due_presence, curve_text, curve_presence, "
    "bonus_text, bonus_presence, penalty_text, penalty_presence, weight_text, weight_presence, "
    "note_text, note_presence) "
    "SELECT ?, result_id, student_id, position, "
    "category_text, category_presence, assignment_text, assignment_presence, points_text, "
    "points_presence, max_points_text, max_points_presence, average_text, average_presence, "
    "status_text, status_presence, due_text, due_presence, curve_text, curve_presence, "
    "bonus_text, bonus_presence, penalty_text, penalty_presence, weight_text, weight_presence, "
    "note_text, note_presence "
    "FROM grade_result_observations WHERE student_id = ? AND report_id = ?"
)
COPY_ROW_RECORDS: Final = (
    "INSERT INTO grade_match_decisions (report_id, row_key, student_id, evidence, occurrence, "
    "result_id, how, rejected, decided_by, decided_at) "
    "SELECT ?, row_key, student_id, evidence, occurrence, result_id, 'same_capture', NULL, ?, ? "
    "FROM grade_match_decisions WHERE student_id = ? AND report_id = ? AND result_id IS NOT NULL"
)
ADD_ACTION: Final = (
    "INSERT INTO grade_current_actions (action_id, student_id, class_id, term_label, "
    "source_report, source_acceptances, source_action, report_made, copied, "
    "complete_from_source, digest, acted_at, role) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
CAPTURE_SHOWN: Final = (
    "SELECT d.row_key, d.result_id FROM grade_match_decisions AS d JOIN grade_reports AS r "
    "ON r.report_id = d.report_id AND r.student_id = d.student_id "
    "WHERE r.student_id = ? AND r.reader = 'text' AND r.source_key = ? "
    "AND d.result_id IS NOT NULL"
)
CAPTURE_TURNED_DOWN: Final = (
    "SELECT d.row_key, d.rejected FROM grade_match_decisions AS d JOIN grade_reports AS r "
    "ON r.report_id = d.report_id AND r.student_id = d.student_id "
    "WHERE r.student_id = ? AND r.class_id = ? AND r.term_label = ? AND r.reader = 'text' "
    "AND r.source_key = ? AND d.how = 'different'"
)
CAPTURE_JOINS: Final = (
    "SELECT MAX(acceptance_order) FROM grade_reports "
    "WHERE student_id = ? AND class_id = ? AND term_label = ? AND source_key = ?"
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
LATEST_PLACES: Final = (
    "SELECT o.report_id, o.result_id, o.position, r.imported_at "
    "FROM grade_result_observations AS o JOIN grade_reports AS r "
    "ON r.report_id = o.report_id AND r.student_id = o.student_id "
    "WHERE r.student_id = ? AND r.class_id = ? AND r.term_label = ?"
)
DECIDED: Final = (
    "SELECT o.report_id, o.evidence, o.result_id, o.how, o.rejected "
    "FROM grade_match_decisions AS o "
    "JOIN grade_reports AS r ON r.report_id = o.report_id AND r.student_id = o.student_id "
    "WHERE r.student_id = ? AND r.class_id = ? AND r.term_label = ?"
)
ROW_DECIDED: Final = (
    "SELECT result_id, rejected FROM grade_match_decisions "
    "WHERE student_id = ? AND report_id = ? AND row_key = ?"
)
ACCEPTED_FROM_SOURCE: Final = (
    "SELECT accepted FROM grade_acceptances WHERE student_id = ? AND kind = ? AND source_key = ?"
)
RECORDED: Final = (
    "SELECT source_key, report_id, accepted, added, updated, already_saved, left_to_check, "
    "shown, answers_kept FROM grade_acceptances WHERE student_id = ? AND acceptance_id = ?"
)
RECORDED_SAVE: Final = (
    "SELECT a.identity_status, a.identity_answer, a.class_id, c.display_name, c.year_label, "
    "a.term_label FROM grade_acceptances AS a JOIN grade_classes AS c "
    "ON c.class_id = a.class_id AND c.student_id = a.student_id "
    "WHERE a.student_id = ? AND a.acceptance_id = ? AND a.kind = ?"
)
CURRENT_CONTEXT: Final = "SELECT year_label, term_label FROM grade_context WHERE student_id = ?"
CHANGE_CONTEXT: Final = (
    "UPDATE grade_context SET year_label = ?, term_label = ?, set_by = ?, set_at = ? "
    "WHERE student_id = ? AND year_label = ? AND term_label = ?"
)
TERMS_ON_RECORD: Final = "SELECT year_label, label, made_at FROM grade_terms WHERE student_id = ?"
TERM_ON_RECORD: Final = (
    "SELECT 1 FROM grade_terms WHERE student_id = ? AND year_label = ? AND label = ?"
)
CLASSES_REPORTED: Final = (
    "SELECT DISTINCT class_id FROM grade_reports WHERE student_id = ? AND term_label = ?"
)
CLASS_REPORTS: Final = (
    "SELECT r.report_id, r.acceptance_order, r.use, r.imported_at, a.acted_at, ("
    "SELECT latest.report_id FROM grade_reports AS latest "
    "WHERE latest.student_id = r.student_id AND latest.class_id = r.class_id "
    "AND latest.term_label = r.term_label AND latest.source_key = r.source_key "
    "ORDER BY latest.acceptance_order DESC LIMIT 1) "
    "FROM grade_reports AS r LEFT JOIN grade_current_actions AS a "
    "ON a.report_made = r.report_id AND a.student_id = r.student_id "
    "WHERE r.student_id = ? AND r.class_id = ? AND r.term_label = ? "
    "ORDER BY r.acceptance_order"
)
CLASS_NAMED: Final = (
    "SELECT year_label, display_name FROM grade_classes WHERE student_id = ? AND class_id = ?"
)
REPORT_SCOPE: Final = (
    "SELECT class_id, term_label, source_key FROM grade_reports "
    "WHERE student_id = ? AND report_id = ?"
)
VIEW_OF: Final = (
    "SELECT year_label, term_label FROM grade_view_choices WHERE student_id = ? AND viewer = ?"
)
CHOOSE_VIEW: Final = (
    "INSERT INTO grade_view_choices (student_id, viewer, year_label, term_label) "
    "VALUES (?, ?, ?, ?) ON CONFLICT (student_id, viewer) DO UPDATE SET "
    "year_label = excluded.year_label, term_label = excluded.term_label"
)
FOLLOW_CURRENT: Final = "DELETE FROM grade_view_choices WHERE student_id = ? AND viewer = ?"
SET_CONTEXT: Final = (
    "INSERT INTO grade_context (student_id, year_label, term_label, set_by, set_at) "
    "VALUES (?, ?, ?, ?, ?)"
)
ADD_YEAR: Final = (
    "INSERT INTO grade_years (student_id, label, first_month, first_month_by, made_at) "
    "VALUES (?, ?, ?, ?, ?)"
)
CORRECT_FIRST_MONTH: Final = (
    "UPDATE grade_years SET first_month = ?, first_month_by = ? "
    "WHERE student_id = ? AND label = ? AND first_month IS ?"
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
    "acceptance_order, use, reader, imported_at, as_of, result_rows) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, 'text', ?, NULL, ?)"
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
    "result_id, how, rejected, decided_by, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
DECIDE_DIFFERENT_AGAIN: Final = (
    "UPDATE grade_match_decisions SET result_id = ?, how = ?, rejected = ?, decided_by = ?, "
    "decided_at = ? WHERE student_id = ? AND report_id = ? AND row_key = ? AND how = 'different'"
)
RAISE_REVISION: Final = (
    "INSERT INTO grade_scope_revisions (student_id, class_id, term_label, revision) "
    "VALUES (?, ?, ?, 1) "
    "ON CONFLICT (student_id, class_id, term_label) DO UPDATE SET revision = revision + 1"
)
ADD_ACCEPTANCE: Final = (
    "INSERT INTO grade_acceptances (acceptance_id, student_id, kind, source_key, report_id, "
    "class_id, term_label, accepted, identity_status, identity_answer, identity_form, added, "
    "updated, already_saved, left_to_check, shown, answers_kept, complete, accepted_at, role) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
ACCEPTANCE_COLUMNS: Final = "PRAGMA table_info(grade_acceptances)"
UNTIED: Final = (
    "SELECT "
    "COALESCE(SUM(a.kind = 'homework_screenshot' AND a.report_id IS NOT NULL), 0), "
    "COALESCE(SUM(a.kind <> 'homework_screenshot' AND a.report_id IS NOT NULL AND NOT EXISTS ("
    "SELECT 1 FROM grade_reports AS r "
    "WHERE r.report_id = a.report_id AND r.student_id = a.student_id)), 0), "
    "COALESCE(SUM(a.kind = 'grade_screenshot' AND a.report_id IS NULL), 0), "
    "COALESCE(SUM(a.kind = 'grade_text' AND a.report_id IS NULL AND NOT EXISTS ("
    "SELECT 1 FROM grade_reports AS r WHERE r.student_id = a.student_id AND r.reader = 'text' "
    "AND r.source_key = a.source_key)), 0), "
    "COALESCE(SUM(a.kind = 'grade_text' AND a.report_id IS NULL AND EXISTS ("
    "SELECT 1 FROM grade_reports AS r JOIN grade_reports AS s "
    "ON s.student_id = r.student_id AND s.reader = 'text' AND s.source_key = r.source_key "
    "WHERE r.student_id = a.student_id AND r.reader = 'text' AND r.source_key = a.source_key "
    "AND (s.class_id <> r.class_id OR s.term_label <> r.term_label))), 0) "
    "FROM grade_acceptances AS a"
)
"""The acceptances of a file without their class and term that nothing ties to one, counted by
case, in the order of ``UNTIED_CASES``."""
UNTIED_CASES: Final = (
    "homework imports linked to a grade report",
    "imports linked to a missing grade report",
    "grade screenshots with no report link",
    "pasted grade reports with no matching saved report",
    "pasted grade reports matching different classes or terms",
)
"""How the refused start names each case of ``UNTIED``, in its order."""
START_GUIDE: Final = ("docs/development.md", "Blossom couldn't start: saved import records")
"""The guide's file in the Blossom folder and its heading, readable while Blossom can't
start."""
SET_ACCEPTANCES_ASIDE: Final = "ALTER TABLE grade_acceptances RENAME TO grade_acceptances_before"
TIE_ACCEPTANCES: Final = (
    "INSERT INTO grade_acceptances (acceptance_id, student_id, kind, source_key, report_id, "
    "class_id, term_label, accepted, identity_status, identity_answer, identity_form, added, "
    "updated, already_saved, left_to_check, shown, answers_kept, complete, accepted_at, role) "
    "SELECT a.acceptance_id, a.student_id, a.kind, a.source_key, a.report_id, "
    "CASE WHEN a.kind = 'homework_screenshot' THEN NULL ELSE ("
    "SELECT r.class_id FROM grade_reports AS r WHERE r.student_id = a.student_id "
    "AND (r.report_id = a.report_id OR (a.report_id IS NULL AND r.reader = 'text' "
    "AND r.source_key = a.source_key)) ORDER BY r.acceptance_order LIMIT 1) END, "
    "CASE WHEN a.kind = 'homework_screenshot' THEN NULL ELSE ("
    "SELECT r.term_label FROM grade_reports AS r WHERE r.student_id = a.student_id "
    "AND (r.report_id = a.report_id OR (a.report_id IS NULL AND r.reader = 'text' "
    "AND r.source_key = a.source_key)) ORDER BY r.acceptance_order LIMIT 1) END, "
    "a.accepted, a.identity_status, a.identity_answer, a.identity_form, a.added, a.updated, "
    "a.already_saved, a.left_to_check, a.shown, a.answers_kept, a.complete, a.accepted_at, "
    "a.role FROM grade_acceptances_before AS a"
)
"""Each acceptance with its report's class and term, or, naming none, its capture's, read
with a scalar subquery so a capture of several reports gives each row once."""
DROP_ACCEPTANCES_SET_ASIDE: Final = "DROP TABLE grade_acceptances_before"
SCOPE_HOLDS: Final = (
    "SELECT (SELECT COUNT(*) FROM grade_reports "
    "WHERE student_id = :student AND class_id = :class AND term_label = :term "
    "AND report_id NOT IN (SELECT report_made FROM grade_current_actions "
    "WHERE student_id = :student)), "
    "(SELECT COUNT(*) FROM grade_reports "
    "WHERE student_id = :student AND class_id = :class AND term_label = :term "
    "AND report_id IN (SELECT report_made FROM grade_current_actions "
    "WHERE student_id = :student)), "
    "(SELECT COUNT(*) FROM grade_results "
    "WHERE student_id = :student AND class_id = :class AND term_label = :term), "
    "(SELECT COUNT(*) FROM grade_acceptances "
    "WHERE student_id = :student AND class_id = :class AND term_label = :term "
    "AND report_id IS NULL), "
    "(SELECT COUNT(*) FROM grade_acceptances "
    "WHERE student_id = :student AND class_id = :class AND term_label = :term), "
    "(SELECT COUNT(*) FROM grade_current_actions "
    "WHERE student_id = :student AND class_id = :class AND term_label = :term)"
)
"""What her class and term hold: reports imported, reports an action made, results,
submissions that saved nothing, every acceptance, and every class-details action."""
DELETE_SCOPE: Final = (
    "DELETE FROM grade_corrections WHERE student_id = :student AND report_id IN ("
    "SELECT report_id FROM grade_reports "
    "WHERE student_id = :student AND class_id = :class AND term_label = :term)",
    "DELETE FROM grade_match_decisions WHERE student_id = :student AND report_id IN ("
    "SELECT report_id FROM grade_reports "
    "WHERE student_id = :student AND class_id = :class AND term_label = :term)",
    "DELETE FROM grade_term_observations WHERE student_id = :student AND report_id IN ("
    "SELECT report_id FROM grade_reports "
    "WHERE student_id = :student AND class_id = :class AND term_label = :term)",
    "DELETE FROM grade_category_observations WHERE student_id = :student AND report_id IN ("
    "SELECT report_id FROM grade_reports "
    "WHERE student_id = :student AND class_id = :class AND term_label = :term)",
    "DELETE FROM grade_result_observations WHERE student_id = :student AND report_id IN ("
    "SELECT report_id FROM grade_reports "
    "WHERE student_id = :student AND class_id = :class AND term_label = :term)",
    "DELETE FROM grade_acceptances "
    "WHERE student_id = :student AND class_id = :class AND term_label = :term",
    "DELETE FROM grade_current_actions "
    "WHERE student_id = :student AND class_id = :class AND term_label = :term",
    "DELETE FROM grade_reports "
    "WHERE student_id = :student AND class_id = :class AND term_label = :term",
    "DELETE FROM grade_results "
    "WHERE student_id = :student AND class_id = :class AND term_label = :term",
)
"""A class and term's delete, one statement per table, the reports' children before them: an
acceptance and an action are found by their own class and term, report-less ones included."""
SCOPE_COPIES: Final = (
    "SELECT report_made, source_report, copied FROM grade_current_actions "
    "WHERE student_id = ? AND class_id = ? AND term_label = ?"
)
SCOPE_CORRECTIONS: Final = (
    "SELECT c.report_id, c.kind, c.target, c.field, c.corrected_text, c.corrected_presence, "
    "c.correction_kind FROM grade_corrections AS c JOIN grade_reports AS r "
    "ON r.report_id = c.report_id AND r.student_id = c.student_id "
    "WHERE r.student_id = ? AND r.class_id = ? AND r.term_label = ? ORDER BY c.sequence"
)
CORRECTION_RECORDED: Final = (
    "SELECT c.report_id, c.kind, c.target, c.field, c.correction_kind, c.corrected_text, "
    "c.corrected_presence FROM grade_corrections AS c JOIN grade_reports AS r "
    "ON r.report_id = c.report_id AND r.student_id = c.student_id "
    "WHERE c.student_id = ? AND c.correction_id = ? AND r.class_id = ? AND r.term_label = ?"
)
NEXT_SEQUENCE: Final = (
    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM grade_corrections "
    "WHERE student_id = ? AND report_id = ? AND kind = ? AND target = ? AND field = ?"
)
ADD_CORRECTION: Final = (
    "INSERT INTO grade_corrections (correction_id, student_id, report_id, kind, target, field, "
    "sequence, read_text, read_presence, corrected_text, corrected_presence, correction_kind, "
    "entered_on, entered_with, corrected_by, corrected_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)"
)
HISTORY: Final = (
    "SELECT c.report_id, c.kind, c.target, c.field, c.sequence, c.read_text, c.read_presence, "
    "c.corrected_text, c.corrected_presence, c.correction_kind, c.entered_on, c.corrected_by, "
    "c.corrected_at FROM grade_corrections AS c JOIN grade_reports AS r "
    "ON r.report_id = c.report_id AND r.student_id = c.student_id "
    "WHERE c.student_id = ? AND r.class_id = ? AND r.term_label = ? AND c.kind = ? "
    "AND c.target = ? ORDER BY r.acceptance_order, c.field, c.sequence"
)
VALUE_FIELDS: Final = {
    "term": frozenset({"percent", "letter"}),
    "category": frozenset({"weight", "average"}),
    "result": frozenset(
        {"points", "max_points", "average", "status", "curve", "bonus", "penalty", "weight"}
    ),
}
"""The cells a correction may name, by kind: value cells only, never a Note or a cell that says
which target a value is."""
LETTER: Final = re.compile(r"[A-F][+-]?")
"""A letter grade, as a parent may assert one."""


ACCEPTANCE_ID: Final = re.compile(r"acceptance-[0-9a-f]{32}")
"""The shape of the acceptance IDs ``new_acceptance_id`` mints."""
CLASS_ID: Final = re.compile(r"class-[0-9a-f]{32}")
"""The shape of the class IDs a save mints."""
RESULT_ID: Final = re.compile(r"result-[0-9a-f]{32}")
"""The shape of the result IDs a save mints."""
REVISION_MAX: Final = 2**63 - 1
"""The largest scope revision the store's integer column holds."""


def new_acceptance_id() -> str:
    """A one-time ID for a review page, which a save of that page is recorded under."""
    return f"acceptance-{uuid.uuid4().hex}"


def new_action_id() -> str:
    """A one-time ID for a preview of the class-details action, its confirmation's retry key."""
    return f"action-{uuid.uuid4().hex}"


def new_correction_id() -> str:
    """A one-time ID for a page correcting a saved value, its record's retry key."""
    return f"correction-{uuid.uuid4().hex}"


def matched_as(header: ReportHeader) -> str:
    """How a class alias is matched: the class code and name as written, with spaces and case
    folded."""

    def fold(text: str | None) -> str | None:
        return None if text is None else folded(text).casefold()

    return json.dumps([fold(header.class_code), fold(header.class_name)], ensure_ascii=False)


OPEN_SAVEPOINT: Final = "SAVEPOINT grade_save"
"""The savepoint every grade write runs inside, written out whole, as are the two statements
that end it. A write nested in another opens its own under the same name, and SQLite takes the
latest."""
RELEASE_SAVEPOINT: Final = "RELEASE grade_save"
UNDO_SAVEPOINT: Final = "ROLLBACK TO grade_save"


@dataclass
class Boundary:
    """What became of one savepoint's write after a failure: the failure itself, and whether the
    write was taken back with the transaction still active."""

    taken_back: bool = True
    failure: BaseException | None = None


@contextmanager
def all_or_none(connection: sqlite3.Connection) -> Iterator[Boundary]:
    """The writes in the block land together or not at all, inside a caller's transaction too.

    It opens only inside a transaction, so the writer's ``BEGIN IMMEDIATE`` stays the only begin
    and its commit the only commit. A failure, the savepoint's own release included, takes back
    what the block began before it goes on. ``taken_back`` says whether that worked: it can't
    when SQLite has ended the transaction, or refuses the rollback."""
    if not connection.in_transaction:
        msg = "a grade write's savepoint opens only inside the writer's transaction"
        raise RuntimeError(msg)
    boundary = Boundary()
    connection.execute(OPEN_SAVEPOINT)
    try:
        yield boundary
        connection.execute(RELEASE_SAVEPOINT)
    except BaseException as error:
        boundary.failure = error
        boundary.taken_back = connection.in_transaction and _rolled_back(connection)
        raise


def _rolled_back(connection: sqlite3.Connection) -> bool:
    """Whether the latest savepoint's writes were taken back. A release refused after that leaves
    an empty savepoint, which holds nothing."""
    try:
        connection.execute(UNDO_SAVEPOINT)
    except sqlite3.Error:
        return False
    with suppress(sqlite3.Error):
        connection.execute(RELEASE_SAVEPOINT)
    return True


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
    """The file refused a write of her name forms. Nothing of the write remains, and a caller's
    transaction it joined is still open, with the caller's other work in it."""


class GradeReportNotSaved(RuntimeError):
    """The file refused a grade write: a report's save, the class-details action or a first
    month's correction. Nothing of the write remains, and a caller's transaction it joined is
    still open, with the caller's other work in it."""


class GradeTransactionLost(RuntimeError):
    """A grade write's transaction can't be trusted, and the operation must be abandoned: SQLite
    ended it, which rolled back all its uncommitted work, a caller's earlier work included, or
    the cleanup after a failure itself failed. Not a refusal, and no claim that a caller's work
    was kept. The original error is its cause."""


class AcceptancesNotTied(RuntimeError):
    """A start stopped because acceptances in the file can't each be tied to one class and
    term; it counts them by case, names nothing in them, and says where the guide is. Every
    gradebook table is left as it was."""

    def __init__(self, counts: tuple[int, ...]) -> None:
        cases = "; ".join(
            f"{case}: {count}" for count, case in zip(counts, UNTIED_CASES, strict=True) if count
        )
        path, heading = START_GUIDE
        super().__init__(
            "Blossom couldn't start. Some saved import records have missing or inconsistent "
            f"report links. Affected records: {sum(counts)} ({cases}). This startup attempt did "
            "not change or delete any grade records. Leave this file and any files beside it "
            "that start with the same name untouched, and see the household guide before "
            f"trying again. The guide is {path} in the Blossom "
            f'folder, under "{heading}".'
        )


def _report_refused(error: BaseException) -> Exception:
    return GradeReportNotSaved(f"the grade report could not be saved: {type(error).__name__}")


def _form_refused(error: BaseException) -> Exception:
    return NameFormNotSaved(f"the name form could not be saved: {type(error).__name__}")


def _action_refused(error: BaseException) -> Exception:
    return GradeReportNotSaved(
        f"the saved values could not be made current: {type(error).__name__}"
    )


def _confirmation_refused(error: BaseException) -> Exception:
    return NameFormNotSaved(f"her name could not be confirmed again: {type(error).__name__}")


def _delete_refused(error: BaseException) -> Exception:
    return GradeReportNotSaved(f"the class and term could not be deleted: {type(error).__name__}")


def _first_month_refused(error: BaseException) -> Exception:
    return GradeReportNotSaved(f"the first month could not be corrected: {type(error).__name__}")


def _context_refused(error: BaseException) -> Exception:
    return GradeReportNotSaved(f"the current term could not be set: {type(error).__name__}")


def _correction_refused(error: BaseException) -> Exception:
    return GradeReportNotSaved(f"the value could not be corrected: {type(error).__name__}")


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


@dataclass(frozen=True)
class FirstMonthCorrected:
    """The year's first month is ``month`` now, set by this correction."""

    year: str
    month: int


@dataclass(frozen=True)
class FirstMonthStood:
    """The year's first month was ``month`` already, as a page sent again finds it; nothing was
    written."""

    year: str
    month: int


@dataclass(frozen=True)
class FirstMonthChanged:
    """The month on record isn't the one the page showed: ``month``, None when unconfirmed.
    Nothing was written."""

    year: str
    month: int | None


@dataclass(frozen=True)
class YearNotOnRecord:
    """Her record holds no school year with this label; nothing was written."""

    year: str


FirstMonthOutcome = FirstMonthCorrected | FirstMonthStood | FirstMonthChanged | YearNotOnRecord


@dataclass(frozen=True)
class DeletePreview:
    """What a delete of her class and term would remove, and the revision it was read at, None
    when the class and term have none: the confirmation posts it back."""

    revision: int | None
    imported_reports: int
    made_current_reports: int
    results: int
    links: int
    submissions: int
    """Submissions that saved nothing: acceptances naming no report."""


@dataclass(frozen=True)
class NothingToDelete:
    """The class and term hold nothing to delete, and a delete answered so finds them never
    written; nothing was written now."""

    class_id: str
    term: str


@dataclass(frozen=True)
class AlreadyDeleted:
    """The class and term hold nothing, as a delete left them; nothing was written now."""

    class_id: str
    term: str


@dataclass(frozen=True)
class DeleteReturned:
    """The class and term changed since the confirmation was shown, and nothing was deleted;
    ``preview`` is what a new confirmation would delete now."""

    preview: DeletePreview


@dataclass(frozen=True)
class ClassTermDeleted:
    """The class and term's grades were deleted: ``removed`` is what the confirmation showed,
    and the revision was raised."""

    class_id: str
    term: str
    removed: DeletePreview


DeleteOutcome = ClassTermDeleted | AlreadyDeleted | DeleteReturned | NothingToDelete

Term = tuple[str, str]
"""A school year and a term within it, as the record keeps them: ``("2026-2027", "T1")``."""


class ViewNotOnRecord(ValueError):
    """A remembered term that is neither the current one nor a term on record for her; nothing
    was written."""


class TermNotOnRecord(ValueError):
    """A term chosen as current that isn't on record for her; nothing was written."""


@dataclass(frozen=True)
class GradeContexts:
    """The current year and term, None before the first setup, and every term on record, by
    school year, then when each term was made, then its label."""

    current: Term | None
    terms: tuple[Term, ...]


@dataclass(frozen=True)
class ClassInTerm:
    """A class of the year, and whether the term holds a report for it."""

    class_id: str
    name: str
    reported: bool


@dataclass(frozen=True)
class ClassOfYear:
    """A class by its ID, with its display name and its school year."""

    class_id: str
    name: str
    year: str


@dataclass(frozen=True)
class ClassReport:
    """A report of a class and term: its acceptance order and use, when it was imported, when
    an action made it (None for an import), and the newest report of its capture."""

    report_id: str
    order: int
    use: ReportUse
    imported_at: str
    acted_at: str | None
    latest_of_capture: str


@dataclass(frozen=True)
class ClassTermRead:
    """Her class and term's reports by acceptance order and each target's current value, read
    together, so every report a value names is among the reports."""

    reports: tuple[ClassReport, ...]
    current: CurrentValues


@dataclass(frozen=True)
class ReportScope:
    """A report's class, term and source key."""

    class_id: str
    term: str
    source_key: str


@dataclass(frozen=True)
class ContextSet:
    """The current year and term are ``context`` now, set by this choice."""

    context: Term


@dataclass(frozen=True)
class ContextStood:
    """The current year and term were ``context`` already, as a page sent again finds them;
    nothing was written."""

    context: Term


@dataclass(frozen=True)
class ContextChanged:
    """The current year and term aren't the ones the page showed: ``context`` is on record.
    Nothing was written."""

    context: Term


@dataclass(frozen=True)
class ContextNotOnRecord:
    """No year and term are on record yet; nothing was written."""


ContextOutcome = ContextSet | ContextStood | ContextChanged | ContextNotOnRecord

CorrectionKind = Literal["parent_assertion", "transcription"]


@dataclass(frozen=True)
class CorrectionPage:
    """A page for one saved value: its one-time correction ID, the class and term's revision it
    was read at, and the observation it shows, by report, kind and target. The report may be a
    copy."""

    correction_id: str
    revision: int
    report_id: str
    kind: str
    target: str


@dataclass(frozen=True)
class FieldNow:
    """One field of a saved value as it reads now, under a fresh page: the school's cell as read
    and the parent's assertion standing on it, if any."""

    page: CorrectionPage
    field: str
    read: Cell
    asserted: Cell | None


@dataclass(frozen=True)
class ValueCorrected:
    """A record was added for ``field`` under ``correction_id``: ``value``, or None for a
    withdrawal; the revision was raised."""

    correction_id: str
    field: str
    how: CorrectionKind
    value: str | None


@dataclass(frozen=True)
class CorrectionRecorded:
    """The page's correction ID was recorded already, and nothing was written now. ``fresh`` is
    the field posted as it reads now, when the post differs from the record."""

    recorded: ValueCorrected
    fresh: FieldNow | None = None


@dataclass(frozen=True)
class CorrectionReturned:
    """The class and term changed since the page was shown, and nothing was written; ``now`` is
    the field as it reads now."""

    now: FieldNow


@dataclass(frozen=True)
class NothingToCorrect:
    """The field already reads what was posted, and nothing was written."""

    field: str


@dataclass(frozen=True)
class ObservationNotSaved:
    """The page's value isn't saved in her class and term, as after a delete; nothing was
    written."""


CorrectionOutcome = (
    ValueCorrected
    | CorrectionRecorded
    | CorrectionReturned
    | NothingToCorrect
    | ObservationNotSaved
)


@dataclass(frozen=True)
class CorrectionRecord:
    """One record of a field's history: the original reading's report, kind and target; the
    field; the record's order among the field's records; the cell as read; the value recorded,
    None for a withdrawal; its kind; the report the parent acted on; who and when."""

    report_id: str
    kind: str
    target: str
    field: str
    sequence: int
    read: Cell
    value: str | None
    how: CorrectionKind
    entered_on: str
    by: ConfirmedBy
    at: str


class GradebookRecords:
    """The part of the record's store that keeps her student record and her name forms."""

    _connection: sqlite3.Connection
    _lock: "threading.RLock"
    _clock: Clock
    _grade_depth: int = 0
    """How many grade writes are open, nested, under the lock; only the outermost answers."""

    def _writing(self) -> AbstractContextManager[None]:
        raise NotImplementedError

    def comparing_and_writing(self) -> AbstractContextManager[None]:
        """The store's lock and its reserved writer, which the store of the record supplies. A
        writer reserved by ``BEGIN IMMEDIATE`` holds out other connections; a caller's deferred
        transaction is joined without that, and a write can then meet a busy file."""
        raise NotImplementedError

    @contextmanager
    def _grade_write(self, refused: Callable[[BaseException], Exception]) -> Iterator[None]:
        """Every grade write's one entry: the store's lock, the writer's transaction (its own,
        or a caller's it joins), and one savepoint around the whole write (``all_or_none``).

        Entries nest, and only the outermost decides what its caller hears when the write fails,
        from the transaction's actual state, never the error's code: SQLite's errors may end a
        transaction, and don't always. When the rollback to the savepoint worked with the
        transaction still active, nothing of the write remains and a caller's earlier work
        stays: a refusal of the file is ``refused(error)``, and any other exception is raised as
        itself. When SQLite ended the transaction, or a cleanup failed, it raises
        ``GradeTransactionLost`` from the original error: the caller abandons the operation,
        never treating it as a refusal. A joined write's outcome stands only once the caller's
        own block commits."""
        with self._lock:
            outermost = self._grade_depth == 0
            joined = self._connection.in_transaction
            boundary: Boundary | None = None
            self._grade_depth += 1
            try:
                with self._writing(), all_or_none(self._connection) as boundary:
                    yield
            except BaseException as error:
                if not outermost:
                    raise
                original = (
                    error if boundary is None or boundary.failure is None else boundary.failure
                )
                cleaned = boundary is None or boundary.taken_back
                if not cleaned or (not joined and self._connection.in_transaction):
                    msg = (
                        f"the transaction holding a grade write was lost: {type(original).__name__}"
                    )
                    raise GradeTransactionLost(msg) from original
                if isinstance(error, sqlite3.Error):
                    raise refused(error) from error
                raise
            finally:
                self._grade_depth -= 1

    def _create_gradebook_tables(self) -> None:
        """The tables, the remembered terms' included, and her record on a file without one, in
        the caller's transaction."""
        self._connection.execute(CREATE_STUDENT)
        self._connection.execute(CREATE_NAME_FORMS)
        for statement in CREATE_REPORT_TABLES:
            self._connection.execute(statement)
        self._connection.execute(CREATE_VIEW_CHOICES)
        self._tie_acceptances()
        if self._connection.execute(STUDENT_ON_RECORD).fetchone() is None:
            self._connection.execute(MAKE_STUDENT, (new_student_id(), self._stamp()))

    def _tie_acceptances(self) -> None:
        """Give a file's acceptances their class and term, once, in the caller's transaction:
        every row tied exactly, or ``AcceptancesNotTied`` before the rebuild writes anything,
        and the caller's transaction takes back the step. The table is made again from its one
        definition, so it reads as a fresh file's."""
        columns = {str(row[1]) for row in self._connection.execute(ACCEPTANCE_COLUMNS)}
        if "class_id" in columns:
            return
        counts = tuple(int(count) for count in self._connection.execute(UNTIED).fetchone())
        if any(counts):
            raise AcceptancesNotTied(counts)
        self._connection.execute(SET_ACCEPTANCES_ASIDE)
        self._connection.execute(ACCEPTANCES_TABLE)
        self._connection.execute(TIE_ACCEPTANCES)
        self._connection.execute(DROP_ACCEPTANCES_SET_ASIDE)

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
        with self._grade_write(_form_refused):
            student_id, check, forms = self._her_name_record()
            identity = identity_among(key, student_line, check=check, forms=forms)
            if identity.form is None:
                raise AnswerNotAsked(identity)
            if identity.status is IdentityStatus.MATCHES:
                return NameFormStood(identity.form)
            if identity.status not in (IdentityStatus.FIRST_USE, IdentityStatus.NOT_CONFIRMED):
                raise AnswerNotAsked(identity)
            self._connection.execute(
                ADD_FORM, (student_id, identity.form, confirmer, self._stamp())
            )
            if check is None:
                self._connection.execute(SET_KEY_CHECK, (key_check(key), student_id))
            return NameFormAdded(identity.form)

    def confirm_name_again(
        self, key: bytes, student_line: str, role: ConfirmedBy
    ) -> NameConfirmedAgain | NameFormStood:
        """The answer "Yes, this is her name" after the secret was replaced: her forms are
        replaced by this line's, and the key check by one under ``key``, together. A line that
        already matches writes nothing; any other question is ``AnswerNotAsked``."""
        confirmer = _confirmer(role)
        with self._grade_write(_confirmation_refused):
            student_id, check, forms = self._her_name_record()
            identity = identity_among(key, student_line, check=check, forms=forms)
            if identity.form is None:
                raise AnswerNotAsked(identity)
            if identity.status is IdentityStatus.MATCHES:
                return NameFormStood(identity.form)
            if identity.status is not IdentityStatus.CONFIRM_AGAIN:
                raise AnswerNotAsked(identity)
            self._connection.execute(DROP_HER_FORMS, (student_id,))
            self._connection.execute(
                ADD_FORM, (student_id, identity.form, confirmer, self._stamp())
            )
            self._connection.execute(SET_KEY_CHECK, (key_check(key), student_id))
            return NameConfirmedAgain(identity.form)

    def review_grade_report(
        self,
        draft: GradeReportDraft,
        source_key: str,
        *,
        key: bytes,
        complete: bool = False,
        same_class: str | None = None,
        matches: tuple[MatchAnswer, ...] = (),
    ) -> GradeReview:
        """What saving ``draft`` would do, under a fresh acceptance ID: the identity of its
        line, its setup questions, the scope revision, each value's status, and the
        report-level choice, the statuses read in ``same_class`` when no alias matched and it
        is one of the year's classes, with ``matches`` applied as a save applies them.
        ``complete`` is ``reading_complete`` of the reading the draft came from; a reading not
        known complete never offers the choice for absence alone. A read alone."""
        with self._lock:
            return self._review_locked(
                draft, source_key, key, same_class=same_class, matches=matches, complete=complete
            )

    def current_values(self, class_id: str, term: str) -> CurrentValues:
        """Each target's current value in her class and term, the term however its label is
        spaced: from the current report with the highest acceptance order that supplied it, with
        that report and its order. A read alone."""
        with self._lock:
            student_id = self._her_name_record()[0]
            return self._class_record(student_id, class_id, folded(term)).current

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
        complete: bool,
    ) -> SaveOutcome:
        """The parent's save of the values ``selection`` names, with the rows the report shows
        and the answers given, as one grade write: inside its own transaction, or a caller's it
        joins, where a failure leaves none of it (see ``_grade_write``). ``complete`` is
        ``reading_complete`` of the reading the draft came from. A recorded acceptance ID returns
        its outcome; a changed revision, an answer to no question asked now, or a value not
        offered returns the review; each of these writes nothing."""
        confirmer = _confirmer(role)
        chosen = frozenset(selection)
        with self._grade_write(_report_refused):
            student_id = self._her_name_record()[0]
            checked = self._checked(
                student_id, draft, source_key, key, page, answers, chosen, complete=complete
            )
            if not isinstance(checked, tuple):
                return checked
            into, settled = checked
            refused = _selection_refused(into, settled, answers, chosen)
            if refused is not None:
                return refused
            use: ReportUse = answers.use or (
                "current" if settled.use is None else settled.use.default
            )
            return self._write_save(
                draft,
                settled,
                answers,
                chosen,
                key=key,
                by=confirmer,
                student_id=student_id,
                acceptance_id=page.acceptance_id,
                complete=complete,
                use=use,
            )

    def check_grade_answers(
        self,
        draft: GradeReportDraft,
        source_key: str,
        *,
        key: bytes,
        complete: bool,
        page: ReviewPage,
        answers: GradeAnswers,
        selection: Collection[str],
    ) -> SaveOutcome | GradeReview:
        """The save's own checks of a page, in its order, writing nothing: a recorded acceptance
        ID's outcome, "Not hers", or the review returned and why, a tick the save can't take
        among them; otherwise the review with the answers applied, each row keeping the choices
        it offered, under the page's own acceptance ID and revision. A read alone."""
        with self._lock:
            student_id = self._her_name_record()[0]
            checked = self._checked(
                student_id,
                draft,
                source_key,
                key,
                page,
                answers,
                frozenset(selection),
                complete=complete,
            )
        if not isinstance(checked, tuple):
            return checked
        into, settled = checked
        refused = _selection_refused(into, settled, answers, frozenset(selection))
        if refused is not None:
            return refused
        offered = {item.key: item.choices for item in into.rows}
        rows = tuple(
            item if item.choices else replace(item, choices=offered.get(item.key, ()))
            for item in settled.rows
        )
        return replace(settled, rows=rows, acceptance_id=page.acceptance_id, revision=page.revision)

    def recorded_save(self, acceptance_id: str) -> RecordedSave | None:
        """Her save of a pasted report recorded under ``acceptance_id``, with its class and term
        and the current year and term; None when no such save of hers is on record. A read
        alone."""
        with self._lock:
            student_id = self._her_name_record()[0]
            recorded = self._recorded(student_id, acceptance_id)
            row = self._connection.execute(
                RECORDED_SAVE, (student_id, acceptance_id, TEXT_KIND)
            ).fetchone()
            context = self._connection.execute(CURRENT_CONTEXT, (student_id,)).fetchone()
        if recorded is None or row is None:
            return None
        status, answer, class_id, class_name, year, term = row
        return RecordedSave(
            saved=recorded[0],
            identity_status=IdentityStatus(status),
            identity_answer=IdentityAnswer(answer),
            class_id=str(class_id),
            class_name=str(class_name),
            year=str(year),
            term=str(term),
            context=None if context is None else (str(context[0]), str(context[1])),
        )

    def _checked(
        self,
        student_id: str,
        draft: GradeReportDraft,
        source_key: str,
        key: bytes,
        page: ReviewPage,
        answers: GradeAnswers,
        chosen: frozenset[str],
        *,
        complete: bool,
    ) -> SaveOutcome | tuple[GradeReview, GradeReview]:
        """A page against the record, in the save's order, under the caller's lock: a recorded
        acceptance ID, "Not hers", a changed source, a changed revision, answers to no question
        asked now; otherwise the review in the class the page chose, and with its answers."""
        recorded = self._recorded(student_id, page.acceptance_id)
        if recorded is not None:
            outcome, recorded_source = recorded
            covered = {item_key for item_key, _ in outcome.accepted}
            if recorded_source != source_key:
                covered = set()
            return AlreadyRecorded(outcome, chosen - covered)
        if answers.identity is IdentityAnswer.NOT_HERS:
            return NotHers()
        review = self._review_locked(draft, source_key, key, same_class=None, complete=complete)
        into = review
        if answers.same_class is not None:
            into = self._review_locked(
                draft, source_key, key, same_class=answers.same_class, complete=complete
            )
        if page.source_key != source_key:
            return ReviewReturned(into, ReturnReason.SOURCE)
        if review.revision != page.revision or (
            answers.same_class is not None
            and self._revision_of(student_id, answers.same_class, draft)
            != answers.same_class_revision
        ):
            return ReviewReturned(into, ReturnReason.REVISION)
        if (
            not answers_asked(review, answers)
            or not matches_asked(into, answers.matches)
            or not use_asked(into, answers.use)
        ):
            return ReviewReturned(into, ReturnReason.ANSWERS)
        settled = into
        if answers.matches:
            settled = self._review_locked(
                draft,
                source_key,
                key,
                same_class=answers.same_class,
                matches=answers.matches,
                complete=complete,
            )
        return into, settled

    def _review_locked(
        self,
        draft: GradeReportDraft,
        source_key: str,
        key: bytes,
        *,
        same_class: str | None,
        matches: tuple[MatchAnswer, ...] = (),
        complete: bool,
    ) -> GradeReview:
        """The review, the values' statuses read in the class an alias matched, or else in
        ``same_class`` when it is one of the year's classes, with ``matches`` applied, for a
        reading complete or not."""
        header = draft.header
        student_id, check, forms = self._her_name_record()
        identity = identity_among(key, header.student_line, check=check, forms=forms)
        year, term = header.year_label, folded(header.term_label)
        alias = self._connection.execute(
            ALIAS_MATCHED, (student_id, year, matched_as(header))
        ).fetchone()
        matched = None if alias is None else str(alias[0])
        existing = tuple(
            (str(row[0]), str(row[1]), self._revision_of(student_id, str(row[0]), draft))
            for row in self._connection.execute(CLASSES_OF_YEAR, (student_id, year))
        )
        revision = None if matched is None else self._revision_of(student_id, matched, draft)
        reviewed = matched
        if reviewed is None and same_class in {class_id for class_id, _, _ in existing}:
            reviewed = same_class
        held = NOTHING_HELD
        joins = None
        turned_down: dict[str, set[str]] = {}
        places: dict[tuple[str, str], tuple[int, date]] = {}
        if reviewed is not None:
            held = self._class_record(student_id, reviewed, term)
            zone = self._clock.zone
            for report_id, result_id, position, imported_at in self._connection.execute(
                LATEST_PLACES, (student_id, reviewed, term)
            ):
                added = datetime.fromisoformat(str(imported_at)).astimezone(zone).date()
                places[(str(report_id), str(result_id))] = (int(position), added)
            (joins,) = self._connection.execute(
                CAPTURE_JOINS, (student_id, reviewed, term, source_key)
            ).fetchone()
            for item_key, rejected in self._connection.execute(
                CAPTURE_TURNED_DOWN, (student_id, reviewed, term, source_key)
            ):
                turned_down.setdefault(str(item_key), set()).add(str(rejected))
        saved: dict[str, str | None] = {}
        for (accepted,) in self._connection.execute(
            ACCEPTED_FROM_SOURCE, (student_id, TEXT_KIND, source_key)
        ):
            for item_key, result_id in json.loads(accepted):
                saved[str(item_key)] = None if result_id is None else str(result_id)
        shown = {
            str(row_key): str(result_id)
            for row_key, result_id in self._connection.execute(
                CAPTURE_SHOWN, (student_id, source_key)
            )
            if row_key not in saved
        }
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
            shown=shown,
            joins=None if joins is None else int(joins),
            turned_down={key: frozenset(texts) for key, texts in turned_down.items()},
            places=places,
        )
        return review_from(
            draft, source_key, new_acceptance_id(), on_record, matches, complete=complete
        )

    def _class_record(self, student_id: str, class_id: str, term: str) -> ClassRecord:
        """What her class and term hold, read under her student ID and projected by
        ``project``."""
        return project(self._scope_held(student_id, class_id, term))

    def _scope_held(self, student_id: str, class_id: str, term: str) -> ScopeHeld:
        """Her class and term's reports, their completeness, observations and row records, as
        stored, and the parent's assertions standing on each observation."""
        scope = (student_id, class_id, term)
        reports = {
            str(report_id): (int(order), str(use), int(rows))
            for report_id, order, use, rows in self._connection.execute(SCOPE_REPORTS, scope)
        }
        named = {"student": student_id, "class": class_id, "term": term}
        complete = {
            str(report_id): bool(read_complete)
            for report_id, read_complete in self._connection.execute(SCOPE_COMPLETE, named)
        }
        observed: list[Observed] = []
        for row in self._connection.execute(TERMS_OBSERVED, scope):
            observed.append(("term", TERM_KEY, str(row[0]), _cells(TERM_FIELDS, row[1:])))
        for row in self._connection.execute(CATEGORIES_OBSERVED, scope):
            observed.append(
                ("category", str(row[1]), str(row[0]), _cells(CATEGORY_FIELDS, row[2:]))
            )
        for row in self._connection.execute(RESULTS_OBSERVED, scope):
            observed.append(("result", str(row[1]), str(row[0]), _cells(RESULT_FIELDS, row[2:])))
        decided: list[Decided] = [
            (
                str(report_id),
                str(evidence),
                None if result is None else str(result),
                str(how),
                None if rejected is None else str(rejected),
            )
            for report_id, evidence, result, how, rejected in self._connection.execute(
                DECIDED, scope
            )
        ]
        corrections = tuple(
            (str(report_id), str(kind), str(target), str(named), str(text), str(presence), str(how))
            for report_id, kind, target, named, text, presence, how in self._connection.execute(
                SCOPE_CORRECTIONS, scope
            )
        )
        marks = apply_corrections(tuple(observed), corrections, self._copies(scope=scope))
        return ScopeHeld(reports, complete, tuple(observed), tuple(decided), marks)

    def preview_current(
        self, class_id: str, term: str, source_key: str
    ) -> CurrentPreview | ReportNotSaved:
        """What "Use saved values from this report as current" would do for the capture
        ``source_key`` in her class and term, under a fresh action ID: its source is the
        capture's latest report. A read alone; ``ReportNotSaved`` when the capture has none."""
        with self._lock:
            student_id = self._her_name_record()[0]
            return self._preview_locked(student_id, class_id, folded(term), source_key)

    def use_capture_as_current(
        self, class_id: str, term: str, *, page: CurrentPage, role: ConfirmedBy
    ) -> ActionOutcome:
        """The parent's confirmation of the preview ``page`` names, as one grade write: a
        recorded action ID returns its outcome; a missing source, a changed revision, source
        acceptances or preview, or nothing to change write nothing; otherwise the copy of the
        source as the next current report, the action record and the raised revision."""
        by = _confirmer(role)
        term = folded(term)
        with self._grade_write(_action_refused):
            student_id = self._her_name_record()[0]
            source = self._connection.execute(
                SCOPE_REPORT, (student_id, class_id, term, page.source)
            ).fetchone()
            recorded = self._connection.execute(
                ACTION_RECORDED, (student_id, page.action_id)
            ).fetchone()
            if recorded is not None:
                made = MadeCurrent(
                    page.action_id, str(recorded[0]), str(recorded[1]), str(recorded[2])
                )
                if (made.source, made.digest) == (page.source, page.digest):
                    return ActionRecorded(made)
                fresh: CurrentPreview | ReportNotSaved = ReportNotSaved()
                if source is not None:
                    fresh = self._preview_locked(student_id, class_id, term, str(source[0]))
                return ActionRecorded(made, fresh)
            if source is None:
                return ReportNotSaved()
            preview = self._preview_locked(student_id, class_id, term, str(source[0]))
            if isinstance(preview, ReportNotSaved):
                return preview
            if preview.empty:
                return NothingToChange()
            if (preview.source, preview.digest) != (page.source, page.digest):
                return PreviewRevised(preview)
            return self._copy_as_current(
                preview, page.action_id, class_id, term, student_id=student_id, by=by
            )

    def _preview_locked(
        self, student_id: str, class_id: str, term: str, source_key: str
    ) -> CurrentPreview | ReportNotSaved:
        """The preview for the capture's latest report in the class and term, from the same
        projection a confirmation recomputes."""
        latest = self._connection.execute(
            LATEST_OF_CAPTURE, (student_id, class_id, term, source_key)
        ).fetchone()
        revision = self._connection.execute(REVISION_OF, (student_id, class_id, term)).fetchone()
        if latest is None or revision is None:
            return ReportNotSaved()
        report_id = str(latest[0])
        acceptances = tuple(
            str(row[0]) for row in self._connection.execute(ACCEPTED_INTO, (student_id, report_id))
        )
        made_by = self._connection.execute(MADE_BY, (student_id, report_id)).fetchone()
        source = SourceOf(
            student_id=student_id,
            class_id=class_id,
            term=term,
            revision=int(revision[0]),
            report_id=report_id,
            acceptances=acceptances,
            made_by=None if made_by is None else str(made_by[0]),
        )
        held = self._scope_held(student_id, class_id, term)
        return preview_of(held, source, new_action_id())

    def _copy_as_current(
        self,
        preview: CurrentPreview,
        action_id: str,
        class_id: str,
        term: str,
        *,
        student_id: str,
        by: ConfirmedBy,
    ) -> MadeCurrent:
        """The writes of a confirmed action: the source copied as the next current report, one
        statement per table under her student ID, its row records naming a result as
        ``same_capture`` at the action's time and role; the action record; the revision."""
        now = self._stamp()
        source = preview.source
        (order,) = self._connection.execute(NEXT_ORDER, (student_id, class_id, term)).fetchone()
        report_id = f"report-{uuid.uuid4().hex}"
        observations = [
            [str(kind), str(target)]
            for kind, target in self._connection.execute(
                COPIED_OBSERVATIONS,
                (TERM_KEY, student_id, source, student_id, source, student_id, source),
            )
        ]
        rows = [
            [str(row_key), str(result_id)]
            for row_key, result_id in self._connection.execute(COPIED_ROWS, (student_id, source))
        ]
        copies = (
            (COPY_REPORT, (report_id, order, student_id, source), 1),
            (COPY_TERM, (report_id, student_id, source), None),
            (COPY_CATEGORIES, (report_id, student_id, source), None),
            (COPY_RESULTS, (report_id, student_id, source), None),
            (COPY_ROW_RECORDS, (report_id, by, now, student_id, source), len(rows)),
        )
        written = 0
        for statement, parameters, expected in copies:
            count = self._connection.execute(statement, parameters).rowcount
            if expected is not None and count != expected:
                msg = "the action copies only her source report, and all of it"
                raise RuntimeError(msg)
            if statement is not COPY_REPORT and statement is not COPY_ROW_RECORDS:
                written += count
        if written != len(observations):
            msg = "the action copies every observation its record lists"
            raise RuntimeError(msg)
        copied = json.dumps(
            {"observations": sorted(observations), "rows": rows},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self._connection.execute(
            ADD_ACTION,
            (
                action_id,
                student_id,
                class_id,
                term,
                source,
                json.dumps(list(preview.acceptances)),
                preview.made_by,
                report_id,
                copied,
                int(preview.complete),
                preview.digest,
                now,
                by,
            ),
        )
        self._connection.execute(RAISE_REVISION, (student_id, class_id, term))
        return MadeCurrent(action_id, source, report_id, preview.digest)

    def first_month_of(self, year: str) -> int | None:
        """The first month on record for her school year ``year``, None when the year isn't on
        record or its month is unconfirmed: what ``due_date_of`` resolves under. A read alone."""
        with self._lock:
            student_id = self._her_name_record()[0]
            row = self._connection.execute(FIRST_MONTH_OF, (student_id, year)).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def correct_first_month(
        self, year: str, *, shown: int | None, month: int, role: ConfirmedBy
    ) -> FirstMonthOutcome:
        """A parent's ``month`` for her school year ``year``, one grade write that compares and
        sets the month: it applies only while the month on record is ``shown``, the page's (None
        when unconfirmed); one already ``month`` stands, and any other is ``FirstMonthChanged``."""
        by = _confirmer(role)
        if month not in range(1, 13):
            msg = "a first month is 1 to 12"
            raise ValueError(msg)
        with self._grade_write(_first_month_refused):
            student_id = self._her_name_record()[0]
            row = self._connection.execute(FIRST_MONTH_OF, (student_id, year)).fetchone()
            if row is None:
                return YearNotOnRecord(year)
            stored = None if row[0] is None else int(row[0])
            if stored == month:
                return FirstMonthStood(year, month)
            if stored != shown:
                return FirstMonthChanged(year, stored)
            self._connection.execute(CORRECT_FIRST_MONTH, (month, by, student_id, year, stored))
            return FirstMonthCorrected(year, month)

    def grade_contexts(self) -> GradeContexts:
        """The current year and term, a typed setup term with no report included, and every term
        on record, by school year, then when it was made, then its label. A read alone."""
        with self._lock:
            student_id = self._her_name_record()[0]
            current = self._connection.execute(CURRENT_CONTEXT, (student_id,)).fetchone()
            rows = self._connection.execute(TERMS_ON_RECORD, (student_id,)).fetchall()
        ordered = sorted(rows, key=lambda row: (str(row[0]), str(row[2]), folded(str(row[1]))))
        return GradeContexts(
            current=None if current is None else (str(current[0]), str(current[1])),
            terms=tuple((str(year), str(label)) for year, label, _ in ordered),
        )

    def terms_of_year(self, year: str) -> tuple[str, ...]:
        """Her terms of ``year`` on record, by when each was made, then its label: a term's
        position here, from 1, is how an address names it. A read alone."""
        return tuple(term for at, term in self.grade_contexts().terms if at == year)

    def classes_in(self, year: str, term: str) -> tuple[ClassInTerm, ...]:
        """Each of her classes of ``year``, by name, then ID, and whether ``term`` holds a report
        for it. A read alone."""
        with self._lock:
            student_id = self._her_name_record()[0]
            classes = self._connection.execute(CLASSES_OF_YEAR, (student_id, year)).fetchall()
            reported = {
                str(row[0])
                for row in self._connection.execute(CLASSES_REPORTED, (student_id, folded(term)))
            }
        ordered = sorted(classes, key=lambda row: (folded(str(row[1])), str(row[0])))
        return tuple(
            ClassInTerm(str(class_id), str(name), str(class_id) in reported)
            for class_id, name in ordered
        )

    def class_named(self, class_id: str) -> ClassOfYear | None:
        """Her class ``class_id``, with its name and year; None when no such class of hers is
        on record. A read alone."""
        with self._lock:
            student_id = self._her_name_record()[0]
            row = self._connection.execute(CLASS_NAMED, (student_id, class_id)).fetchone()
        return None if row is None else ClassOfYear(class_id, str(row[1]), str(row[0]))

    def class_term(self, class_id: str, term: str) -> ClassTermRead:
        """Her class and term's reports by acceptance order, each with its use, its import time,
        its making action's time and its capture's newest report, with each target's current
        value, under one hold of the store's lock: no grade write lands between them. A read
        alone."""
        with self._lock:
            student_id = self._her_name_record()[0]
            rows = self._connection.execute(
                CLASS_REPORTS, (student_id, class_id, folded(term))
            ).fetchall()
            current = self._class_record(student_id, class_id, folded(term)).current
        reports = tuple(
            ClassReport(
                report_id=str(report_id),
                order=int(order),
                use=cast(ReportUse, str(use)),
                imported_at=str(imported_at),
                acted_at=None if acted_at is None else str(acted_at),
                latest_of_capture=str(latest),
            )
            for report_id, order, use, imported_at, acted_at, latest in rows
        )
        return ClassTermRead(reports=reports, current=current)

    def report_scope(self, report_id: str) -> ReportScope | None:
        """Her report's class, term and source key; None when no such report of hers is on
        record. A read alone."""
        with self._lock:
            student_id = self._her_name_record()[0]
            row = self._connection.execute(REPORT_SCOPE, (student_id, report_id)).fetchone()
        return None if row is None else ReportScope(str(row[0]), str(row[1]), str(row[2]))

    def view_of(self, viewer: Viewer) -> Term | None:
        """The term ``viewer`` chose to see on Grades; None when they follow the current one.
        A read alone."""
        with self._lock:
            student_id = self._her_name_record()[0]
            row = self._connection.execute(VIEW_OF, (student_id, viewer)).fetchone()
        return None if row is None else (str(row[0]), str(row[1]))

    def choose_view(self, viewer: Viewer, view: Term | None) -> None:
        """``viewer``'s choice of the term Grades opens on, kept as their row alone: a term that
        is the current one or on record for her, or None to follow the current one. Any other is
        ``ViewNotOnRecord``, and nothing is written. Never a grade write."""
        if viewer not in get_args(Viewer):
            msg = "a remembered term belongs to her, a parent, or anyone"
            raise ValueError(msg)
        with self._lock, self._writing():
            student_id = self._her_name_record()[0]
            if view is None:
                self._connection.execute(FOLLOW_CURRENT, (student_id, viewer))
                return
            year, term = view
            current = self._connection.execute(CURRENT_CONTEXT, (student_id,)).fetchone()
            on_record = self._connection.execute(TERM_ON_RECORD, (student_id, year, term))
            if (current is None or (str(current[0]), str(current[1])) != view) and (
                on_record.fetchone() is None
            ):
                raise ViewNotOnRecord(view)
            self._connection.execute(CHOOSE_VIEW, (student_id, viewer, year, term))

    def set_current_context(self, shown: Term, chosen: Term, role: ConfirmedBy) -> ContextOutcome:
        """A parent's choice of ``chosen`` as the current year and term, one grade write that
        compares and sets the context alone: it applies only while ``shown`` is on record, a
        context already ``chosen`` stands, and a term not on record is ``TermNotOnRecord``.
        Everyone's remembered term stays as it was."""
        by = _confirmer(role)
        with self._grade_write(_context_refused):
            student_id = self._her_name_record()[0]
            row = self._connection.execute(CURRENT_CONTEXT, (student_id,)).fetchone()
            if row is None:
                return ContextNotOnRecord()
            stored = (str(row[0]), str(row[1]))
            if stored == chosen:
                return ContextStood(stored)
            if stored != shown:
                return ContextChanged(stored)
            on_record = self._connection.execute(TERM_ON_RECORD, (student_id, *chosen))
            if on_record.fetchone() is None:
                raise TermNotOnRecord(chosen)
            self._connection.execute(
                CHANGE_CONTEXT, (*chosen, by, self._stamp(), student_id, *stored)
            )
            return ContextSet(chosen)

    def correct_value(
        self,
        class_id: str,
        term: str,
        *,
        page: CorrectionPage,
        field: str,
        how: CorrectionKind,
        value: GradeValue | None,
        confirmed: bool = False,
        role: ConfirmedBy,
    ) -> CorrectionOutcome:
        """A parent's assertion of ``value`` on one field of the saved value ``page`` shows, or
        with None its withdrawal, as one grade write recorded on the original reading. A
        transcription correction isn't taken here. A recorded ID returns its outcome while its
        reading is saved; a missing reading, a changed revision or nothing to change write
        nothing; a value outside its closed form is refused."""
        by = _confirmer(role)
        if how != "parent_assertion":
            msg = "a saved value takes a parent's assertion, not a transcription correction"
            raise ValueError(msg)
        if field not in VALUE_FIELDS.get(page.kind, ()):
            msg = "only a value cell takes an assertion"
            raise ValueError(msg)
        term = folded(term)
        posted = None if value is None else value.text
        with self._grade_write(_correction_refused):
            student_id = self._her_name_record()[0]
            scope = (student_id, class_id, term)
            held = self._scope_held(*scope)
            cells = {(kind, target, report): one for kind, target, report, one in held.observed}
            recorded = self._connection.execute(
                CORRECTION_RECORDED, (student_id, page.correction_id, class_id, term)
            ).fetchone()
            if recorded is not None:
                original, kind, target, named, made_how, text, presence = map(str, recorded)
                if (kind, target, original) not in cells:
                    return ObservationNotSaved()
                made = ValueCorrected(
                    page.correction_id,
                    named,
                    cast(CorrectionKind, made_how),
                    None if presence == "withdrawn" else text,
                )
                if (made.field, made.value, kind, target) == (
                    field,
                    posted,
                    page.kind,
                    page.target,
                ):
                    return CorrectionRecorded(made)
                return CorrectionRecorded(made, self._field_now(held, page, field, scope))
            on_page = cells.get((page.kind, page.target, page.report_id))
            if on_page is None:
                return ObservationNotSaved()
            stored = self._connection.execute(REVISION_OF, scope).fetchone()
            if stored is None or int(stored[0]) != page.revision:
                now = self._field_now(held, page, field, scope)
                return CorrectionReturned(now) if now is not None else ObservationNotSaved()
            standing = held.marks.get((page.kind, page.target, page.report_id), {}).get(field)
            _assertion_allowed(field, on_page[field], value, standing, held)
            if value is not None and standing == (Presence.REPORTED, value.text):
                return NothingToCorrect(field)
            copies = self._copies(scope=scope)
            original = original_of(page.kind, page.target, page.report_id, copies)
            read_presence, read_text = cells[page.kind, page.target, original][field]
            (sequence,) = self._connection.execute(
                NEXT_SEQUENCE, (student_id, original, page.kind, page.target, field)
            ).fetchone()
            self._connection.execute(
                ADD_CORRECTION,
                (
                    page.correction_id,
                    student_id,
                    original,
                    page.kind,
                    page.target,
                    field,
                    int(sequence),
                    read_text,
                    read_presence.value,
                    posted or "",
                    "withdrawn" if value is None else "reported",
                    how,
                    page.report_id,
                    by,
                    self._stamp(),
                ),
            )
            self._connection.execute(RAISE_REVISION, scope)
            return ValueCorrected(page.correction_id, field, how, posted)

    def _field_now(
        self, held: ScopeHeld, page: CorrectionPage, field: str, scope: tuple[str, str, str]
    ) -> FieldNow | None:
        """``field`` of the page's reading as it reads now, under a fresh page, or None when the
        reading isn't saved in the class and term."""
        cells = next(
            (
                one
                for kind, target, report, one in held.observed
                if (kind, target, report) == (page.kind, page.target, page.report_id)
            ),
            None,
        )
        stored = self._connection.execute(REVISION_OF, scope).fetchone()
        if cells is None or field not in cells or stored is None:
            return None
        fresh = CorrectionPage(
            new_correction_id(), int(stored[0]), page.report_id, page.kind, page.target
        )
        asserted = held.marks.get((page.kind, page.target, page.report_id), {}).get(field)
        return FieldNow(fresh, field, cells[field], asserted)

    def _copies(self, *, scope: tuple[str, str, str]) -> Copies:
        """By each report an action made in the class and term, its source and what it
        copied."""
        copies: dict[str, tuple[str, frozenset[tuple[str, str]]]] = {}
        for made, source, copied in self._connection.execute(SCOPE_COPIES, scope):
            listed = json.loads(str(copied))["observations"]
            copies[str(made)] = (
                str(source),
                frozenset((str(kind), str(target)) for kind, target in listed),
            )
        return copies

    def correction_history(
        self, class_id: str, term: str, kind: str, target: str
    ) -> tuple[CorrectionRecord, ...]:
        """Every record on ``target``'s readings in her class and term, withdrawals included, by
        report and field in their order; a read alone."""
        term = folded(term)
        with self._lock:
            student_id = self._her_name_record()[0]
            rows = self._connection.execute(
                HISTORY, (student_id, class_id, term, kind, target)
            ).fetchall()
        return tuple(
            CorrectionRecord(
                report_id=str(report_id),
                kind=str(of_kind),
                target=str(of_target),
                field=str(named),
                sequence=int(sequence),
                read=(Presence(str(read_presence)), str(read_text)),
                value=None if corrected_presence == "withdrawn" else str(corrected_text),
                how=cast(CorrectionKind, str(how)),
                entered_on=str(entered_on),
                by=cast(ConfirmedBy, str(by)),
                at=str(at),
            )
            for (
                report_id,
                of_kind,
                of_target,
                named,
                sequence,
                read_text,
                read_presence,
                corrected_text,
                corrected_presence,
                how,
                entered_on,
                by,
                at,
            ) in rows
        )

    def delete_preview(self, class_id: str, term: str) -> DeletePreview | NothingToDelete:
        """What a delete of her class and term would remove, with the revision it was read at;
        a read alone."""
        term = folded(term)
        with self._lock:
            return self._delete_preview(self._her_name_record()[0], class_id, term)

    def delete_class_term(
        self, class_id: str, term: str, *, revision: int | None, role: ConfirmedBy
    ) -> DeleteOutcome:
        """A parent's delete of her class and term, as one grade write checked against the
        revision the confirmation showed: every answer is read from the class and term as they
        are, and only a confirmation of the current revision deletes, raising it."""
        _confirmer(role)
        term = folded(term)
        with self._grade_write(_delete_refused):
            student_id = self._her_name_record()[0]
            held = self._delete_preview(student_id, class_id, term)
            stored = self._connection.execute(REVISION_OF, (student_id, class_id, term)).fetchone()
            current = None if stored is None else int(stored[0])
            if isinstance(held, NothingToDelete):
                return held if current is None else AlreadyDeleted(class_id, term)
            if current != revision:
                return DeleteReturned(held)
            named = {"student": student_id, "class": class_id, "term": term}
            for statement in DELETE_SCOPE:
                self._connection.execute(statement, named)
            self._connection.execute(RAISE_REVISION, (student_id, class_id, term))
            return ClassTermDeleted(class_id, term, held)

    def _delete_preview(
        self, student_id: str, class_id: str, term: str
    ) -> DeletePreview | NothingToDelete:
        """Her class and term's counts at its revision, or ``NothingToDelete`` when they hold
        no report, result, acceptance or action."""
        named = {"student": student_id, "class": class_id, "term": term}
        imported, made, results, submissions, accepted, actions = (
            int(count) for count in self._connection.execute(SCOPE_HOLDS, named).fetchone()
        )
        if not (imported or made or results or accepted or actions):
            return NothingToDelete(class_id, term)
        stored = self._connection.execute(REVISION_OF, (student_id, class_id, term)).fetchone()
        current = None if stored is None else int(stored[0])
        return DeletePreview(current, imported, made, results, 0, submissions)

    def _revision_of(self, student_id: str, class_id: str, draft: GradeReportDraft) -> int | None:
        """The scope revision of ``class_id`` in the report's term, None when it holds nothing."""
        term = folded(draft.header.term_label)
        held = self._connection.execute(REVISION_OF, (student_id, class_id, term)).fetchone()
        return None if held is None else int(held[0])

    def _recorded(self, student_id: str, acceptance_id: str) -> tuple[GradeReportSaved, str] | None:
        """The outcome recorded under ``acceptance_id`` for her, with its source key."""
        row = self._connection.execute(RECORDED, (student_id, acceptance_id)).fetchone()
        if row is None:
            return None
        source_key, report_id, accepted, added, updated, already_saved, left, shown, kept = row
        outcome = GradeReportSaved(
            acceptance_id=acceptance_id,
            report_id=report_id,
            added=added,
            updated=updated,
            already_saved=already_saved,
            left=left,
            shown=shown,
            answers_kept=kept,
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
        complete: bool,
        use: ReportUse,
    ) -> GradeReportSaved:
        """Every write of a save that passed its checks, inside the save's one grade write: the
        identity answer, the setup, the selected values, the rows shown and the "different"
        answers kept, in the report they join, with its use, or make with ``use``, and the
        revision, when anything is new; and the acceptance."""
        header = draft.header
        now = self._stamp()
        line = header.student_line
        if answers.identity is IdentityAnswer.HERS and line is not None:
            if review.identity.status is IdentityStatus.CONFIRM_AGAIN:
                self.confirm_name_again(key, line, by)
            else:
                self.add_name_form(key, line, by)
        year, term = header.year_label, folded(header.term_label)
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
        latest = self._connection.execute(
            LATEST_OF_CAPTURE, (student_id, class_id, term, review.source_key)
        ).fetchone()
        joined = None if latest is None else str(latest[0])
        # Rows not selected: each one shown with a reliable match gets its record, and each one
        # answered "A different assignment" now keeps that answer. A remembered "different" in
        # the report joined becomes the record of what the row resolved to now.
        shown: list[tuple[str, str, str, bool]] = []
        different: list[tuple[str, str, bool]] = []
        for item in review.rows:
            if item.key in chosen or item.status is ItemStatus.NEEDS_ANSWER:
                continue
            record = None if joined is None else self._decided(joined, item.key, student_id)
            if record is not None and record[0] is not None:
                continue
            in_place = record is not None
            if item.result_id is not None and item.how is not None:
                shown.append((item.key, item.result_id, item.how, in_place))
            elif item.how == "answer" and item.question is not None and not item.remembered:
                rejected = rejected_text(item.question)
                if record is None or record[1] != rejected:
                    different.append((item.key, rejected, in_place))
        report_id = None
        accepted: list[tuple[str, str | None]] = []
        if chosen or shown or different:
            report_id = joined or self._new_report(
                draft, class_id, review.source_key, student_id, now, use=use
            )
            accepted = self._observe(
                draft, review, chosen, report_id, class_id, student_id=student_id, by=by, now=now
            )
            evidence = _rows_by_key(draft)
            for item_key, result_id, how, in_place in shown:
                self._decide(
                    report_id,
                    item_key,
                    student_id,
                    *evidence[item_key],
                    result_id,
                    how,
                    in_place=in_place,
                    by=by,
                    now=now,
                )
            for item_key, rejected, in_place in different:
                self._decide(
                    report_id,
                    item_key,
                    student_id,
                    *evidence[item_key],
                    None,
                    "different",
                    rejected=rejected,
                    in_place=in_place,
                    by=by,
                    now=now,
                )
        # Only a save that records something in the class and term raises its revision: a no-op
        # writes its acceptance alone, for its retry, and other open pages stay valid. An
        # incomplete reading of a capture already on record is evidence against its report's
        # completeness, so it raises the revision too. Setup and identity answers are held by
        # the recheck of answers instead.
        if report_id is not None or (joined is not None and not complete):
            self._connection.execute(RAISE_REVISION, (student_id, class_id, term))
        already = sum(
            1 for item in review.items if item.status in ALREADY and item.key not in chosen
        )
        changed = sum(1 for item in review.items if item.key in chosen and item.status in UPDATES)
        outcome = GradeReportSaved(
            acceptance_id=acceptance_id,
            report_id=report_id,
            added=len(chosen) - changed,
            updated=changed,
            already_saved=already,
            left=len(review.items) - len(chosen) - already,
            accepted=tuple(sorted(accepted, key=lambda pair: pair[0])),
            shown=len(shown),
            answers_kept=sum(1 for _, _, how, _ in shown if how in EXPLICIT) + len(different),
        )
        self._connection.execute(
            ADD_ACCEPTANCE,
            (
                acceptance_id,
                student_id,
                TEXT_KIND,
                review.source_key,
                report_id,
                class_id,
                term,
                json.dumps([list(pair) for pair in outcome.accepted], ensure_ascii=False),
                review.identity.status.value,
                answers.identity.value,
                review.identity.form,
                outcome.added,
                outcome.updated,
                outcome.already_saved,
                outcome.left,
                outcome.shown,
                outcome.answers_kept,
                int(complete),
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

    def _new_report(
        self,
        draft: GradeReportDraft,
        class_id: str,
        source_key: str,
        student_id: str,
        now: str,
        *,
        use: ReportUse,
    ) -> str:
        """A new report of the capture, next in acceptance order, with its use and its number of
        result rows."""
        term = folded(draft.header.term_label)
        (order,) = self._connection.execute(NEXT_ORDER, (student_id, class_id, term)).fetchone()
        report_id = f"report-{uuid.uuid4().hex}"
        rows = sum(len(category.rows) for category in draft.categories)
        self._connection.execute(
            ADD_REPORT, (report_id, student_id, class_id, term, source_key, order, use, now, rows)
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
        result it resolved to, or of a new one, with its match decision unless the report showed
        the row first: each accepted key with the result it resolved to."""
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
            if item_key not in chosen:
                continue
            record = self._decided(report_id, item_key, student_id)
            recorded = None if record is None else record[0]
            if recorded is not None and recorded != item.result_id:
                msg = "a row shown first is accepted only as the result its record names"
                raise RuntimeError(msg)
            result_id = item.result_id
            if result_id is None:
                result_id = f"result-{uuid.uuid4().hex}"
                term = folded(draft.header.term_label)
                self._connection.execute(ADD_RESULT, (result_id, student_id, class_id, term, now))
            cells = [text for value in (category.name, *row.cells()) for text in _cell(value)]
            self._connection.execute(
                ADD_RESULT_OBSERVATION, (report_id, result_id, student_id, position, *cells)
            )
            if recorded is None:
                self._decide(
                    report_id,
                    item_key,
                    student_id,
                    category.name,
                    row,
                    result_id,
                    item.how or "new",
                    in_place=record is not None,
                    by=by,
                    now=now,
                )
            accepted.append((item_key, result_id))
        return accepted

    def _decided(
        self, report_id: str, item_key: str, student_id: str
    ) -> tuple[str | None, str | None] | None:
        """The row's record in ``report_id``: the result it names, or None for a remembered "A
        different assignment" with the candidates it turned down; None when it has no record."""
        held = self._connection.execute(ROW_DECIDED, (student_id, report_id, item_key))
        found = held.fetchone()
        if found is None:
            return None
        result_id, rejected = found
        return (
            None if result_id is None else str(result_id),
            None if rejected is None else str(rejected),
        )

    def _decide(
        self,
        report_id: str,
        item_key: str,
        student_id: str,
        category_name: GradeValue,
        row: GradeRow,
        result_id: str | None,
        how: str,
        *,
        rejected: str | None = None,
        in_place: bool = False,
        by: ConfirmedBy,
        now: str,
    ) -> None:
        """A row's match decision in ``report_id``: its evidence and occurrence, the result it
        resolved to, or none with the candidates "different" turned down, and how. With
        ``in_place``, the row's remembered "different" there becomes this decision."""
        if in_place:
            again = (result_id, how, rejected, by, now, student_id, report_id, item_key)
            if self._connection.execute(DECIDE_DIFFERENT_AGAIN, again).rowcount != 1:
                msg = "only a remembered different assignment is decided again in place"
                raise RuntimeError(msg)
            return
        evidence = evidence_text(row_evidence(category_name, row))
        self._connection.execute(
            ADD_MATCH_DECISION,
            (
                report_id,
                item_key,
                student_id,
                evidence,
                row.occurrence,
                result_id,
                how,
                rejected,
                by,
                now,
            ),
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
"""The statuses an outcome counts as already saved, unless selected."""
UPDATES: Final = frozenset({ItemStatus.CHANGED, ItemStatus.MATCHES_EARLIER})
"""The statuses of selected values an outcome counts as updated: changed, or back to a value a
newer report replaced."""


def _rows_by_key(draft: GradeReportDraft) -> dict[str, tuple[GradeValue, GradeRow]]:
    """Each result row of ``draft`` by its key, with its category's name."""
    return {
        row_key(category.name, row): (category.name, row)
        for category in draft.categories
        for row in category.rows
    }


def _assertion_allowed(
    field: str, read: Cell, value: GradeValue | None, standing: Cell | None, held: ScopeHeld
) -> None:
    """Nothing when a parent may record ``value`` on ``field`` of a reading that read ``read``,
    or ``ValueError``: a cell the reading captured; a reported value in its closed form, a
    number in the field's form, a letter A to F with + or -, or a status her class and term
    already hold; a withdrawal only of an assertion that stands."""
    if read[0] is Presence.NOT_CAPTURED:
        msg = "the reading captured no such cell"
        raise ValueError(msg)
    if value is None:
        if standing is None:
            msg = "no assertion stands on this field"
            raise ValueError(msg)
        return
    text = value.text
    if field == "status":
        statuses = {
            cells["status"][1]
            for kind, _, _, cells in held.observed
            if kind == "result" and cells["status"][0] is Presence.REPORTED
        }
        allowed = text in statuses
    elif field == "letter":
        allowed = LETTER.fullmatch(text) is not None
    else:
        allowed = NUMBER.fullmatch(text) is not None
    if value.presence is not Presence.REPORTED or not allowed:
        msg = "an assertion takes only its closed form"
        raise ValueError(msg)


def _confirmer(role: str) -> ConfirmedBy:
    """``role`` when it may change her grade records, her name's confirmation included, or
    ``ValueError``."""
    if role not in get_args(ConfirmedBy):
        msg = "only a parent, or the household with the sign-in off, changes grade records"
        raise ValueError(msg)
    return cast(ConfirmedBy, role)


def _selection_refused(
    into: GradeReview, settled: GradeReview, answers: GradeAnswers, chosen: frozenset[str]
) -> ReviewReturned | None:
    """The review returned when ``chosen`` ticks a value a save can't take, naming the ticks
    refused and those of them on rows still asking; None when it takes them all."""
    refused = chosen - selectable(into, settled, answers.use)
    if not refused:
        return None
    asking = refused & rows_still_asking(settled)
    return ReviewReturned(into, ReturnReason.SELECTION, refused, asking)
