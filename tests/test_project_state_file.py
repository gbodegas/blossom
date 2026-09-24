"""Assignments and the claims about their dates live in the household's file.

They outlive a restart; a fixture is read only into a blank file and leaves a
kept record alone; a set that cannot be read leaves no file behind, and a first
start the process cut short is begun again; a household that names no fixture
starts with nothing on record and nothing synthetic; a claim is read back
exactly as it was made; and a batch that fails leaves nothing of itself.
"""

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from blossom.app import create_app
from blossom.dependencies import build_application_state
from blossom.reconciliation import SourceChannel, SourceRecord
from blossom.settings import REPOSITORY_ROOT, Settings
from blossom.sources import FixtureSource
from blossom.stores.paths import UnsafeCheckpointPath
from blossom.stores.project_state import (
    Assignment,
    AssignmentKind,
    ProjectStateStore,
    StatusReport,
)
from tests.support import FIXTURES, SAME_ORIGIN, fixture_clock, fixture_settings

PAGE = {"Accept": "text/html"}


def settings_in(tmp_path: pathlib.Path, **environ: str) -> Settings:
    """Settings with every state file under ``tmp_path``; ``environ`` wins."""
    return fixture_settings(
        **{
            "BLOSSOM_TODAY": "2026-08-19",
            "BLOSSOM_DATABASE_PATH": str(tmp_path / "blossom.sqlite3"),
            "BLOSSOM_CHECKPOINT_PATH": str(tmp_path / "checkpoints.sqlite3"),
            "BLOSSOM_TRACE_PATH": str(tmp_path / "traces.sqlite3"),
            **environ,
        }
    )


def test_assignments_and_their_claims_outlive_a_restart(tmp_path: pathlib.Path) -> None:
    seeded = build_application_state(settings_in(tmp_path), InMemorySaver())
    try:
        before = seeded.project_state.all_assignments()
        claims_before = {
            item.assignment_id: seeded.project_state.deadline_records(item.assignment_id)
            for item in before
        }
    finally:
        seeded.close()
    again = build_application_state(settings_in(tmp_path, BLOSSOM_FIXTURE_PATH=""), InMemorySaver())
    try:
        after = again.project_state.all_assignments()
        claims_after = {
            item.assignment_id: again.project_state.deadline_records(item.assignment_id)
            for item in after
        }
    finally:
        again.close()

    assert before
    assert after == before
    assert any(claims_before.values())
    assert claims_after == claims_before


def test_the_fixture_seeds_a_new_file_and_leaves_a_kept_record_alone(
    tmp_path: pathlib.Path,
) -> None:
    """An assignment entered by hand is still there after the next start, beside the
    fixture's, rather than written over by the fixture."""
    entered = Assignment(
        assignment_id="assignment-entered-by-hand",
        course="Spanish",
        title="Vocabulary list, unit two",
        due_date=date(2026, 8, 27),
        dependencies=[],
        reported_submission_status="not_started",
    )
    first = build_application_state(settings_in(tmp_path), InMemorySaver())
    try:
        first.project_state.upsert_assignments([entered])
    finally:
        first.close()
    second = build_application_state(settings_in(tmp_path), InMemorySaver())
    try:
        on_record = {item.assignment_id: item for item in second.project_state.all_assignments()}
    finally:
        second.close()

    assert on_record["assignment-entered-by-hand"] == entered
    assert "assignment-canal-essay" in on_record


def test_an_existing_file_is_the_record_and_a_named_fixture_leaves_it_alone(
    tmp_path: pathlib.Path,
) -> None:
    """A household file from before the record lived in it, with a fixture path left in
    .env from an earlier example: the file stays as it is, and only the planner's rules
    and notes come from the set."""
    before = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    before.close()
    state = build_application_state(settings_in(tmp_path), InMemorySaver())
    try:
        assert state.project_state.created is False
        assignments = state.project_state.all_assignments()
        rules = state.support_rules.list_all()
    finally:
        state.close()

    assert assignments == []
    assert rules


def test_a_set_that_cannot_be_read_leaves_no_file_behind(tmp_path: pathlib.Path) -> None:
    """The whole set is read before anything is written, and a start that fails removes
    the file it made, so the next start, with a set that reads, seeds afresh."""
    broken = tmp_path / "broken"
    broken.mkdir()
    shutil.copy(FIXTURES / "assignments.json", broken / "assignments.json")
    (broken / "deadline_sources.json").write_text("[{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        build_application_state(
            settings_in(tmp_path, BLOSSOM_FIXTURE_PATH=str(broken)), InMemorySaver()
        )
    assert not (tmp_path / "blossom.sqlite3").exists()

    state = build_application_state(settings_in(tmp_path), InMemorySaver())
    try:
        seeded = state.project_state.all_assignments()
    finally:
        state.close()

    assert seeded


def test_a_set_claiming_a_date_for_an_assignment_it_does_not_list_is_refused_by_name(
    tmp_path: pathlib.Path,
) -> None:
    """Every claim in a set is read and checked; one about an assignment the set does not
    list is a mistyped id, refused by name, and the start leaves no file behind."""
    mistyped = tmp_path / "mistyped"
    mistyped.mkdir()
    shutil.copy(FIXTURES / "assignments.json", mistyped / "assignments.json")
    claims = [
        {
            "assignment_id": "assignment-canal-essay",
            "channel": "LMS",
            "asserted_value": "2026-08-21",
            "observed_at": "2026-08-19T09:00:00-04:00",
            "confidence": 0.82,
        },
        {
            "assignment_id": "assignment-canal-esay",
            "channel": "PARENT_ENTRY",
            "asserted_value": "2026-08-22",
            "observed_at": "2026-08-19T10:00:00-04:00",
            "confidence": 0.7,
        },
    ]
    (mistyped / "deadline_sources.json").write_text(json.dumps(claims), encoding="utf-8")
    settings = settings_in(tmp_path, BLOSSOM_FIXTURE_PATH=str(mistyped))

    with pytest.raises(ValueError, match="assignment-canal-esay"):
        build_application_state(settings, InMemorySaver())
    assert not (tmp_path / "blossom.sqlite3").exists()


def test_a_household_with_no_fixture_starts_with_nothing_on_record(
    tmp_path: pathlib.Path,
) -> None:
    settings = settings_in(tmp_path, BLOSSOM_FIXTURE_PATH="")
    assert settings.fixture_path is None
    with TestClient(create_app(settings), headers=SAME_ORIGIN) as client:
        page = client.get("/student/due-this-week", headers=PAGE)
    state = build_application_state(settings, InMemorySaver())
    try:
        assignments = state.project_state.all_assignments()
        rules = state.support_rules.list_all()
        notes = state.reflections.list_all()
    finally:
        state.close()

    assert page.status_code == 200
    assert assignments == []
    assert rules == []
    assert notes == []


def test_a_claim_is_read_back_as_made_and_the_file_refuses_a_share(
    tmp_path: pathlib.Path,
) -> None:
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        assert store.created
        assert store.is_empty()
        when = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
        claims = [
            SourceRecord(
                channel=SourceChannel.LMS,
                asserted_value="2026-08-21",
                observed_at=when,
                confidence=0.9,
                seen_in="the day's header",
            ),
            SourceRecord(
                channel=SourceChannel.PARENT_ENTRY,
                asserted_value="2026-08-22",
                observed_at=when + timedelta(hours=1),
                confidence=0.7,
            ),
        ]
        store.record_claims("assignment-essay", claims)
        read_back = store.deadline_records("assignment-essay")
        unknown = store.deadline_records("assignment-unknown")
        empty = store.is_empty()
    finally:
        store.close()

    assert read_back == claims
    assert unknown == []
    assert empty is False
    with pytest.raises(UnsafeCheckpointPath):
        ProjectStateStore.open(pathlib.Path(r"\\server\share\blossom.sqlite3"), fixture_clock())


def test_a_first_start_the_process_cut_short_is_begun_again(tmp_path: pathlib.Path) -> None:
    """The tables and the seed are one transaction, so a process that ends between them
    leaves a file SQLite rolls back to blank, and the next start seeds it in full."""
    script = tmp_path / "cut_short.py"
    script.write_text(
        textwrap.dedent(
            """
            import os
            import pathlib
            import sys

            from blossom.clock import clock_from
            from blossom.sources import FixtureSource, read_whole
            from blossom.stores.project_state import ProjectStateStore

            whole = ProjectStateStore._record_claims_locked

            def cut_short(self, assignment_id, records):
                whole(self, assignment_id, records)
                os._exit(71)

            ProjectStateStore._record_claims_locked = cut_short
            ProjectStateStore.initialize(
                pathlib.Path(sys.argv[1]),
                clock_from(None, "America/New_York"),
                lambda: read_whole(FixtureSource(pathlib.Path(sys.argv[2]))),
            )
            """
        ),
        encoding="utf-8",
    )
    database = tmp_path / "blossom.sqlite3"
    ended = subprocess.run(  # noqa: S603
        [sys.executable, str(script), str(database), str(FIXTURES)],
        cwd=REPOSITORY_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPOSITORY_ROOT)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert ended.returncode == 71, ended.stderr

    state = build_application_state(settings_in(tmp_path), InMemorySaver())
    try:
        seeded = {item.assignment_id for item in state.project_state.all_assignments()}
        claims = state.project_state.deadline_records("assignment-canal-essay")
    finally:
        state.close()

    assert seeded == {item.assignment_id for item in FixtureSource(FIXTURES).assignments()}
    assert claims


def test_a_batch_that_fails_leaves_nothing_of_itself_and_frees_the_file(
    tmp_path: pathlib.Path,
) -> None:
    """A claim the database refuses, or a batch cut short by what it is given, is rolled
    back whole: nothing of it shows, a second writer is not kept waiting, and a later
    batch keeps nothing of it."""
    path = tmp_path / "blossom.sqlite3"
    when = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
    good = SourceRecord(
        channel=SourceChannel.LMS, asserted_value="2026-08-21", observed_at=when, confidence=0.9
    )
    with pytest.raises(ValueError, match="confidence"):
        SourceRecord(
            channel=SourceChannel.LMS,
            asserted_value="2026-08-22",
            observed_at=when,
            confidence=float("nan"),
        )
    unchecked = SourceRecord.model_construct(
        channel=SourceChannel.LMS,
        asserted_value="2026-08-22",
        observed_at=when,
        confidence=float("nan"),
        seen_in=None,
    )
    refused = SourceRecord(
        channel=SourceChannel.EMAIL, asserted_value="2026-08-23", observed_at=when, confidence=0.8
    )
    store = ProjectStateStore.open(path, fixture_clock())
    other = sqlite3.connect(path, timeout=0.2)
    try:
        other.execute(
            "CREATE TRIGGER refuse_email BEFORE INSERT ON date_claims "
            "WHEN NEW.channel = 'EMAIL' BEGIN SELECT RAISE(ABORT, 'refused by the test'); END"
        )
        other.commit()
        with pytest.raises(sqlite3.DatabaseError):
            store.record_claims("assignment-essay", [good, unchecked])
        with pytest.raises(sqlite3.DatabaseError):
            store.record_claims("assignment-essay", [good, refused])

        def cut_short() -> Iterator[Assignment]:
            yield Assignment(
                assignment_id="assignment-first",
                course="Spanish",
                title="Vocabulary list",
                due_date=date(2026, 8, 27),
                dependencies=[],
                reported_submission_status="not_started",
            )
            msg = "cut short by what it was given"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError):
            store.upsert_assignments(cut_short())
        other.execute(
            "INSERT INTO date_claims "
            "(assignment_id, channel, asserted_value, observed_at, confidence, seen_in) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("assignment-other", "LMS", "2026-08-24", when.isoformat(), 0.5, None),
        )
        other.commit()
        store.record_claims("assignment-essay", [good])
        essay = store.deadline_records("assignment-essay")
        on_record = store.all_assignments()
    finally:
        other.close()
        store.close()

    assert essay == [good]
    assert on_record == []


def test_a_file_from_before_is_brought_up_to_the_schema_and_keeps_its_rows(
    tmp_path: pathlib.Path,
) -> None:
    """A file written before the note column: the columns are added, no index is made on the
    claims, and every row stays, two observations of one value included."""
    path = tmp_path / "blossom.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE assignments (
            assignment_id TEXT PRIMARY KEY, course TEXT NOT NULL, title TEXT NOT NULL,
            due_date TEXT, dependencies TEXT NOT NULL, reported_submission_status TEXT NOT NULL,
            assigned_on TEXT, kind TEXT NOT NULL
        );
        CREATE TABLE date_claims (
            assignment_id TEXT NOT NULL, channel TEXT NOT NULL, asserted_value TEXT NOT NULL,
            observed_at TEXT NOT NULL, confidence REAL NOT NULL, seen_in TEXT
        );
        INSERT INTO assignments VALUES
            ('assignment-essay', 'World History', 'Canal Era comparison essay', '2026-08-21',
             '', 'in_progress', NULL, 'HOMEWORK');
        INSERT INTO date_claims VALUES
            ('assignment-essay', 'LMS', '2026-08-21', '2026-08-19T09:00:00+00:00', 0.9, NULL),
            ('assignment-essay', 'LMS', '2026-08-21', '2026-08-19T10:00:00+00:00', 0.9, NULL),
            ('assignment-essay', 'PARENT_ENTRY', '2026-08-22',
             '2026-08-19T11:00:00+00:00', 0.7, NULL);
        """
    )
    old.commit()
    old.close()

    store = ProjectStateStore.open(path, fixture_clock())
    try:
        rows = store.all_assignments()
        claims = store.deadline_records("assignment-essay")
        unique = {
            str(row[1])
            for row in store._connection.execute("PRAGMA index_list(date_claims)")
            if row[2]
        }
    finally:
        store.close()

    assert [row.assignment_id for row in rows] == ["assignment-essay"]
    assert rows[0].note is None
    assert [(said.channel.value, said.asserted_value) for said in claims] == [
        ("LMS", "2026-08-21"),
        ("LMS", "2026-08-21"),
        ("PARENT_ENTRY", "2026-08-22"),
    ]
    assert claims[0].observed_at.isoformat() == "2026-08-19T09:00:00+00:00"
    # The one unique index the table has is the one that makes a note's claim one claim;
    # the school's claims name no note, so it holds none of them to once.
    assert unique == {"date_claims_capture_once"}


def test_a_file_from_the_version_between_loses_its_index_and_keeps_every_observation(
    tmp_path: pathlib.Path,
) -> None:
    """A file from a version that held each claim once carries that index; opening it drops
    the index and nothing else, so two observations of one value are both kept."""
    path = tmp_path / "blossom.sqlite3"
    between = sqlite3.connect(path)
    between.executescript(
        """
        CREATE TABLE assignments (
            assignment_id TEXT PRIMARY KEY, course TEXT NOT NULL, title TEXT NOT NULL,
            due_date TEXT, dependencies TEXT NOT NULL, reported_submission_status TEXT NOT NULL,
            assigned_on TEXT, kind TEXT NOT NULL, note TEXT
        );
        CREATE TABLE date_claims (
            assignment_id TEXT NOT NULL, channel TEXT NOT NULL, asserted_value TEXT NOT NULL,
            observed_at TEXT NOT NULL, confidence REAL NOT NULL, seen_in TEXT
        );
        CREATE UNIQUE INDEX date_claims_once
            ON date_claims (assignment_id, channel, asserted_value, COALESCE(seen_in, ''));
        CREATE INDEX date_claims_by_assignment ON date_claims (assignment_id);
        INSERT INTO assignments VALUES
            ('assignment-essay', 'World History', 'Canal Era comparison essay', '2026-08-21',
             '', 'in_progress', NULL, 'HOMEWORK', NULL);
        INSERT INTO date_claims VALUES
            ('assignment-essay', 'LMS', '2026-08-21', '2026-08-19T09:00:00+00:00', 0.9, NULL);
        """
    )
    between.commit()
    between.close()

    store = ProjectStateStore.open(path, fixture_clock())
    try:
        indexes = {
            str(row[1]) for row in store._connection.execute("PRAGMA index_list(date_claims)")
        }
        store.put_on_record(
            [],
            {
                "assignment-essay": [
                    SourceRecord(
                        channel=SourceChannel.LMS,
                        asserted_value="2026-08-21",
                        observed_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
                        confidence=0.9,
                    ),
                    SourceRecord(
                        channel=SourceChannel.LMS,
                        asserted_value="2026-08-21",
                        observed_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
                        confidence=0.9,
                    ),
                ]
            },
        )
        claims = store.deadline_records("assignment-essay")
        rows = store.all_assignments()
    finally:
        store.close()

    assert "date_claims_once" not in indexes
    assert "date_claims_by_assignment" in indexes
    assert [said.observed_at.isoformat() for said in claims] == [
        "2026-08-19T09:00:00+00:00",
        "2026-08-20T09:00:00+00:00",
        "2026-08-21T09:00:00+00:00",
    ]
    assert [row.assignment_id for row in rows] == ["assignment-essay"]


def a_row(note: str | None, origins: dict[str, SourceChannel]) -> Assignment:
    return Assignment(
        assignment_id="assignment-essay",
        course="World History",
        title="Canal Era comparison essay",
        due_date=date(2026, 8, 21),
        dependencies=[],
        reported_submission_status="in_progress",
        assigned_on=None,
        kind=AssignmentKind.HOMEWORK,
        note=note,
        origins=origins,
    )


def a_report(day: date) -> StatusReport:
    return StatusReport(
        status="missing",
        channel=SourceChannel.EMAIL,
        reported_on=day,
        dated_by="the day it was pasted",
        observed_at=datetime(2026, 9, 12, 20, 0, tzinfo=UTC),
    )


def test_a_report_is_kept_once_per_channel_status_and_day_whoever_writes_it(
    tmp_path: pathlib.Path,
) -> None:
    """The file itself keeps a report once per channel, status, and day, so a caller that
    writes the same report twice, or two callers, leave one, and the row's reported status
    follows the latest report by its day, whoever wrote it; a file from a version that kept
    no such index folds its duplicates to the first and gains the index."""
    path = tmp_path / "blossom.sqlite3"
    store = ProjectStateStore.open(path, fixture_clock())
    try:
        store.put_on_record([a_row(None, {})], {})
        store.record_status_reports("assignment-essay", [a_report(date(2026, 9, 9))] * 2)
        store.record_status_reports("assignment-essay", [a_report(date(2026, 9, 9))])
        store.put_on_record([], {}, {"assignment-essay": [a_report(date(2026, 9, 10))]})
        reports = store.status_reports("assignment-essay")
        row_status = store.all_assignments()[0].reported_submission_status
        latest = store.latest_status_reports()["assignment-essay"].status
        store.record_status_reports(
            "assignment-essay",
            [a_report(date(2026, 9, 11)).model_copy(update={"status": "submitted"})],
        )
        after_a_later_day = store.all_assignments()[0].reported_submission_status
        store.record_status_reports("assignment-essay", [a_report(date(2026, 9, 8))])
        after_an_older_day = store.all_assignments()[0].reported_submission_status
    finally:
        store.close()

    before = tmp_path / "before.sqlite3"
    old = sqlite3.connect(before)
    old.executescript(
        """
        CREATE TABLE assignments (
            assignment_id TEXT PRIMARY KEY, course TEXT NOT NULL, title TEXT NOT NULL,
            due_date TEXT, dependencies TEXT NOT NULL, reported_submission_status TEXT NOT NULL,
            assigned_on TEXT, kind TEXT NOT NULL, note TEXT, origins TEXT
        );
        CREATE TABLE status_reports (
            assignment_id TEXT NOT NULL, status TEXT NOT NULL, channel TEXT NOT NULL,
            reported_on TEXT NOT NULL, dated_by TEXT NOT NULL, observed_at TEXT NOT NULL,
            source_date_text TEXT
        );
        INSERT INTO assignments VALUES
            ('assignment-essay', 'World History', 'Canal Era comparison essay', '2026-08-21',
             '', 'missing', NULL, 'HOMEWORK', NULL, NULL);
        INSERT INTO status_reports VALUES
            ('assignment-essay', 'missing', 'EMAIL', '2026-09-09', 'the day it was pasted',
             '2026-09-12T20:00:00+00:00', '09/09'),
            ('assignment-essay', 'missing', 'EMAIL', '2026-09-09', 'the day it was pasted',
             '2026-09-12T21:00:00+00:00', '09/09'),
            ('assignment-essay', 'missing', 'EMAIL', '2026-09-10', 'the day it was pasted',
             '2026-09-13T20:00:00+00:00', '09/09');
        """
    )
    old.commit()
    old.close()
    upgraded = ProjectStateStore.open(before, fixture_clock())
    try:
        kept = upgraded.status_reports("assignment-essay")
        unique = {
            str(row[1])
            for row in upgraded._connection.execute("PRAGMA index_list(status_reports)")
            if row[2]
        }
    finally:
        upgraded.close()

    assert [(report.reported_on, report.status) for report in reports] == [
        (date(2026, 9, 9), "missing"),
        (date(2026, 9, 10), "missing"),
    ]
    assert (row_status, latest) == ("missing", "missing")
    assert after_a_later_day == "submitted"
    assert after_an_older_day == "submitted"
    assert [report.observed_at.isoformat() for report in kept] == [
        "2026-09-12T20:00:00+00:00",
        "2026-09-13T20:00:00+00:00",
    ]
    assert unique == {"status_reports_once"}


def test_a_row_written_without_a_note_or_origins_keeps_the_saved_ones(
    tmp_path: pathlib.Path,
) -> None:
    """A row written over a saved one carries its note and its origins only when it has
    them; a row without them, as a set read from a file is, erases neither. A row with
    another note replaces the note."""
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        store.put_on_record(
            [
                a_row(
                    "Pencil only.",
                    {"record": SourceChannel.LMS, "note": SourceChannel.PARENT_ENTRY},
                )
            ],
            {},
        )
        store.put_on_record([a_row(None, {})], {})
        kept = store.all_assignments()[0]
        store.put_on_record(
            [a_row("Ink.", {"record": SourceChannel.LMS, "note": SourceChannel.PARENT_ENTRY})],
            {},
        )
        replaced = store.all_assignments()[0]
    finally:
        store.close()

    assert kept.note == "Pencil only."
    assert kept.origins == {"record": SourceChannel.LMS, "note": SourceChannel.PARENT_ENTRY}
    assert replaced.note == "Ink."
    assert replaced.origins["note"] is SourceChannel.PARENT_ENTRY


def test_a_record_write_that_fails_at_the_claims_keeps_no_assignment_either(
    tmp_path: pathlib.Path,
) -> None:
    """The assignments and the claims are one write: a claim the database refuses after
    the assignment is in rolls the assignment back with it, and the file is free."""
    path = tmp_path / "blossom.sqlite3"
    when = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
    entered = Assignment(
        assignment_id="assignment-essay",
        course="World History",
        title="Canal Era comparison essay",
        due_date=date(2026, 8, 21),
        dependencies=[],
        reported_submission_status="not_started",
    )
    said = SourceRecord(
        channel=SourceChannel.LMS, asserted_value="2026-08-21", observed_at=when, confidence=0.9
    )
    refused = SourceRecord(
        channel=SourceChannel.EMAIL, asserted_value="2026-08-22", observed_at=when, confidence=0.8
    )
    store = ProjectStateStore.open(path, fixture_clock())
    other = sqlite3.connect(path, timeout=0.2)
    try:
        other.execute(
            "CREATE TRIGGER refuse_email BEFORE INSERT ON date_claims "
            "WHEN NEW.channel = 'EMAIL' BEGIN SELECT RAISE(ABORT, 'refused by the test'); END"
        )
        other.commit()
        with pytest.raises(sqlite3.DatabaseError):
            store.put_on_record([entered], {"assignment-essay": [said, refused]})
        on_record = store.all_assignments()
        claims = store.deadline_records("assignment-essay")
        other.execute(
            "INSERT INTO date_claims "
            "(assignment_id, channel, asserted_value, observed_at, confidence, seen_in) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("assignment-other", "LMS", "2026-08-24", when.isoformat(), 0.5, None),
        )
        other.commit()
    finally:
        other.close()
        store.close()

    assert on_record == []
    assert claims == []
