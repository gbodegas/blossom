"""Her own account of her work, kept as a chain of events in the household's file.

Synthetic assignments, a pinned clock, and the store alone: what she says
is appended once, compared before it is written, undone by restoring what
stood before, and read back as what stands now.
"""

import pathlib
import sqlite3
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from blossom.assignment_status import (
    NOTE_MAX_LENGTH,
    AssignmentStatus,
    normalize_note,
    standing_report,
    statuses_for,
)
from blossom.noticing import planning_digest, read_week
from blossom.reconciliation import SourceChannel
from blossom.sources import FixtureSource, read_whole
from blossom.stores.project_state import (
    AlreadySaved,
    Assignment,
    AssignmentKind,
    Conflict,
    CouldNotSave,
    ProjectStateStore,
    Saved,
    Seed,
    StatusReport,
    StudentReport,
    Undone,
    UnknownAssignment,
    UnknownReport,
)
from tests.support import FIXTURES, fixture_clock

NOW = datetime(2026, 9, 16, 23, 30, tzinfo=UTC)
TODAY = date(2026, 9, 16)
"""Half past four in the afternoon in the fixtures' zone, on the day the report is made."""
PRACTICE = "assignment-practice"
LOG = "assignment-log"


def a_row(assignment_id: str, title: str) -> Assignment:
    return Assignment(
        assignment_id=assignment_id,
        course="Math",
        title=title,
        due_date=date(2026, 9, 18),
        dependencies=[],
        reported_submission_status="not_started",
        kind=AssignmentKind.HOMEWORK,
    )


def a_store(path: pathlib.Path) -> ProjectStateStore:
    store = ProjectStateStore.open(path, fixture_clock())
    store.put_on_record([a_row(PRACTICE, "Weekly practice"), a_row(LOG, "Reading log")], {})
    return store


def missing(day: date) -> StatusReport:
    return StatusReport(
        status="missing",
        channel=SourceChannel.EMAIL,
        reported_on=day,
        dated_by="the day it was pasted",
        observed_at=NOW,
    )


def status_of(store: ProjectStateStore, assignment_id: str) -> AssignmentStatus:
    return statuses_for(store, [assignment_id])[assignment_id]


def test_a_first_report_is_one_event_with_its_day_and_changes_nothing_else(
    tmp_path: pathlib.Path,
) -> None:
    store = a_store(tmp_path / "blossom.sqlite3")
    try:
        store.record_status_reports(PRACTICE, [missing(date(2026, 9, 15))])
        before = store.all_assignments()
        saved = store.report_status(
            PRACTICE, "done", None, expected_head=None, now=NOW, today=TODAY
        )
        history = store.student_reports(PRACTICE)
        after = store.all_assignments()
        reports = store.status_reports(PRACTICE)
        status = status_of(store, PRACTICE)
        other = status_of(store, LOG)
    finally:
        store.close()

    assert isinstance(saved, Saved)
    assert saved.report.operation == "report"
    assert (saved.report.status, saved.report.note) == ("done", None)
    assert (saved.report.reported_at, saved.report.reported_on) == (NOW, TODAY)
    assert saved.report.previous_report_id is None
    assert history == [saved.report]
    assert after == before
    assert [report.status for report in reports] == ["missing"]
    assert (status.work_state, status.needs_homework) == ("done", False)
    assert status.head_id == saved.report.report_id
    assert status.reported_on == TODAY
    assert status.check_the_school_record
    assert (other.work_state, other.needs_homework, other.head_id) == ("unreported", True, None)
    assert not other.check_the_school_record


def test_three_reports_on_one_day_are_three_events_and_the_last_stands(
    tmp_path: pathlib.Path,
) -> None:
    store = a_store(tmp_path / "blossom.sqlite3")
    try:
        first = store.report_status(
            PRACTICE, "done", None, expected_head=None, now=NOW, today=TODAY
        )
        assert isinstance(first, Saved)
        second = store.report_status(
            PRACTICE,
            "not_yet",
            "Question four is left.",
            expected_head=first.report.report_id,
            now=NOW,
            today=TODAY,
        )
        assert isinstance(second, Saved)
        third = store.report_status(
            PRACTICE, "done", None, expected_head=second.report.report_id, now=NOW, today=TODAY
        )
        assert isinstance(third, Saved)
        history = store.student_reports(PRACTICE)
        status = status_of(store, PRACTICE)
    finally:
        store.close()

    assert [event.status for event in history] == ["done", "not_yet", "done"]
    assert [event.previous_report_id for event in history] == [
        None,
        first.report.report_id,
        second.report.report_id,
    ]
    assert len({event.report_id for event in history}) == 3
    assert status.head_id == third.report.report_id
    assert (status.status, status.note) == ("done", None)


def test_the_same_status_and_note_are_already_saved_whatever_page_they_come_from(
    tmp_path: pathlib.Path,
) -> None:
    """A repeat adds nothing and changes no date, a stale but matching page included; a
    changed note is a new report; notes that differ only in line endings or edges are one."""
    store = a_store(tmp_path / "blossom.sqlite3")
    try:
        first = store.report_status(
            PRACTICE,
            "done",
            normalize_note("Finished.\r\n"),
            expected_head=None,
            now=NOW,
            today=TODAY,
        )
        assert isinstance(first, Saved)
        later = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
        same = store.report_status(
            PRACTICE,
            "done",
            normalize_note("  Finished.\n"),
            expected_head=first.report.report_id,
            now=later,
            today=date(2026, 9, 17),
        )
        stale_but_same = store.report_status(
            PRACTICE, "done", "Finished.", expected_head=None, now=later, today=date(2026, 9, 17)
        )
        changed = store.report_status(
            PRACTICE,
            "done",
            "Finished, all of it.",
            expected_head=first.report.report_id,
            now=later,
            today=date(2026, 9, 17),
        )
        history = store.student_reports(PRACTICE)
    finally:
        store.close()

    assert isinstance(same, AlreadySaved)
    assert same.head == first.report
    assert isinstance(stale_but_same, AlreadySaved)
    assert isinstance(changed, Saved)
    assert [(event.note, event.reported_on) for event in history] == [
        ("Finished.", TODAY),
        ("Finished, all of it.", date(2026, 9, 17)),
    ]


def test_normalizing_a_note_keeps_her_words_and_their_breaks() -> None:
    assert normalize_note(None) is None
    assert normalize_note("   ") is None
    assert normalize_note("one\r\ntwo\rthree\n") == "one\ntwo\nthree"
    assert normalize_note("  keep  the   spaces  ") == "keep  the   spaces"
    assert len(normalize_note("x" * NOTE_MAX_LENGTH) or "") == NOTE_MAX_LENGTH


def test_a_page_whose_head_has_moved_on_conflicts_and_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """Two blank pages saving different first reports: one is saved, the other conflicts.
    An old done retried after a not yet conflicts. A page made before an undo to no report
    conflicts too, since the chain has a head even when nothing stands."""
    store = a_store(tmp_path / "blossom.sqlite3")
    try:
        first = store.report_status(
            PRACTICE, "done", None, expected_head=None, now=NOW, today=TODAY
        )
        assert isinstance(first, Saved)
        other_blank = store.report_status(
            PRACTICE, "not_yet", None, expected_head=None, now=NOW, today=TODAY
        )
        moved = store.report_status(
            PRACTICE, "not_yet", None, expected_head=first.report.report_id, now=NOW, today=TODAY
        )
        assert isinstance(moved, Saved)
        old_done_again = store.report_status(
            PRACTICE, "done", None, expected_head=first.report.report_id, now=NOW, today=TODAY
        )
        undone = store.undo_report(PRACTICE, moved.report.report_id, now=NOW, today=TODAY)
        assert isinstance(undone, Undone)
        with pytest.raises(UnknownReport):
            store.undo_report(LOG, "report-000000000000", now=NOW, today=TODAY)
        fresh_log = store.report_status(LOG, "done", None, expected_head=None, now=NOW, today=TODAY)
        assert isinstance(fresh_log, Saved)
        log_undone = store.undo_report(LOG, fresh_log.report.report_id, now=NOW, today=TODAY)
        assert isinstance(log_undone, Undone)
        blank_after_undo = store.report_status(
            LOG, "not_yet", None, expected_head=None, now=NOW, today=TODAY
        )
        history = store.student_reports(PRACTICE)
    finally:
        store.close()

    assert isinstance(other_blank, Conflict)
    assert other_blank.head == first.report
    assert isinstance(old_done_again, Conflict)
    assert old_done_again.head == moved.report
    assert isinstance(blank_after_undo, Conflict)
    assert blank_after_undo.head == log_undone.report
    assert [event.operation for event in history] == ["report", "report", "undo"]


def test_undo_restores_what_stood_before_and_keeps_the_correction(
    tmp_path: pathlib.Path,
) -> None:
    """Undoing a first report leaves a head that restores no report; undoing a later one
    restores the earlier report, its note, and its day, while the undo has a day of its
    own. An old target, an undo, or another assignment's report cannot be undone."""
    store = a_store(tmp_path / "blossom.sqlite3")
    try:
        first = store.report_status(
            PRACTICE, "not_yet", "Half left.", expected_head=None, now=NOW, today=TODAY
        )
        assert isinstance(first, Saved)
        next_day = datetime(2026, 9, 17, 22, 0, tzinfo=UTC)
        second = store.report_status(
            PRACTICE,
            "done",
            None,
            expected_head=first.report.report_id,
            now=next_day,
            today=date(2026, 9, 17),
        )
        assert isinstance(second, Saved)
        stale = store.undo_report(
            PRACTICE, first.report.report_id, now=next_day, today=date(2026, 9, 17)
        )
        undone = store.undo_report(
            PRACTICE, second.report.report_id, now=next_day, today=date(2026, 9, 17)
        )
        assert isinstance(undone, Undone)
        restored = status_of(store, PRACTICE)
        twice = store.undo_report(
            PRACTICE, undone.report.report_id, now=next_day, today=date(2026, 9, 17)
        )
        again = store.undo_report(
            PRACTICE, second.report.report_id, now=next_day, today=date(2026, 9, 17)
        )
        only = store.report_status(LOG, "done", None, expected_head=None, now=NOW, today=TODAY)
        assert isinstance(only, Saved)
        with pytest.raises(UnknownReport):
            store.undo_report(PRACTICE, only.report.report_id, now=NOW, today=TODAY)
        to_nothing = store.undo_report(LOG, only.report.report_id, now=NOW, today=TODAY)
        assert isinstance(to_nothing, Undone)
        nothing = status_of(store, LOG)
        after_all = store.report_status(
            LOG, "done", None, expected_head=to_nothing.report.report_id, now=NOW, today=TODAY
        )
    finally:
        store.close()

    assert isinstance(stale, Conflict)
    assert (undone.report.operation, undone.report.undoes_report_id) == (
        "undo",
        second.report.report_id,
    )
    assert (undone.report.status, undone.report.note) == ("not_yet", "Half left.")
    assert restored.head == undone.report
    assert restored.asserted == first.report
    assert (restored.status, restored.note, restored.work_state) == (
        "not_yet",
        "Half left.",
        "not_yet",
    )
    assert (restored.reported_on, restored.restored_on) == (TODAY, date(2026, 9, 17))
    assert isinstance(twice, Conflict)
    assert isinstance(again, Conflict)
    assert (to_nothing.report.status, to_nothing.report.note) == (None, None)
    assert (nothing.head, nothing.asserted, nothing.work_state) == (
        to_nothing.report,
        None,
        "unreported",
    )
    assert nothing.head_id == to_nothing.report.report_id
    assert isinstance(after_all, Saved)
    assert after_all.report.previous_report_id == to_nothing.report.report_id


def test_an_unknown_assignment_is_refused_by_name(tmp_path: pathlib.Path) -> None:
    store = a_store(tmp_path / "blossom.sqlite3")
    try:
        with pytest.raises(UnknownAssignment, match="assignment-nobody"):
            store.report_status(
                "assignment-nobody", "done", None, expected_head=None, now=NOW, today=TODAY
            )
        with pytest.raises(UnknownAssignment, match="assignment-nobody"):
            store.undo_report("assignment-nobody", "report-x", now=NOW, today=TODAY)
        heads = store.student_report_heads()
    finally:
        store.close()

    assert heads == {}


def test_a_write_that_fails_after_the_insert_leaves_no_event(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The event and the check that it is the head are one transaction: a failure after
    the insert rolls the event back, and the store is whole for the next write."""
    store = a_store(tmp_path / "blossom.sqlite3")
    try:

        def refuse(report: StudentReport) -> None:
            msg = "the head could not be read back"
            raise RuntimeError(msg)

        monkeypatch.setattr(store, "_confirm_head_locked", refuse)
        with pytest.raises(RuntimeError, match="read back"):
            store.report_status(PRACTICE, "done", None, expected_head=None, now=NOW, today=TODAY)
        nothing = store.student_reports(PRACTICE)
        monkeypatch.undo()
        saved = store.report_status(
            PRACTICE, "done", None, expected_head=None, now=NOW, today=TODAY
        )
        one = store.student_reports(PRACTICE)
    finally:
        store.close()

    assert nothing == []
    assert isinstance(saved, Saved)
    assert one == [saved.report]


def test_another_writer_holding_the_file_makes_the_save_wait_or_fail_whole(
    tmp_path: pathlib.Path,
) -> None:
    """A second connection holding the writer's lock keeps a save from starting; the save
    either lands whole after the lock is released or fails whole, never in part."""
    path = tmp_path / "blossom.sqlite3"
    store = a_store(path)
    other = sqlite3.connect(path, timeout=0.2)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute(
            "INSERT INTO date_claims VALUES ('assignment-log', 'LMS', '2026-09-18', "
            "'2026-09-16T00:00:00+00:00', 0.9, NULL)"
        )
        store._connection.execute("PRAGMA busy_timeout = 200")
        with pytest.raises(CouldNotSave, match="locked"):
            store.report_status(PRACTICE, "done", None, expected_head=None, now=NOW, today=TODAY)
        other.rollback()
        nothing = store.student_reports(PRACTICE)
        saved = store.report_status(
            PRACTICE, "done", None, expected_head=None, now=NOW, today=TODAY
        )
    finally:
        other.close()
        store.close()

    assert nothing == []
    assert isinstance(saved, Saved)


def test_reports_outlive_a_restart_and_a_file_from_before_gains_the_table(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "blossom.sqlite3"
    store = a_store(path)
    try:
        saved = store.report_status(
            PRACTICE, "done", "Turned in on paper.", expected_head=None, now=NOW, today=TODAY
        )
        assert isinstance(saved, Saved)
    finally:
        store.close()
    again = ProjectStateStore.open(path, fixture_clock())
    try:
        status = status_of(again, PRACTICE)
        history = again.student_reports(PRACTICE)
    finally:
        again.close()

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
             '2026-09-12T20:00:00+00:00', '09/09');
        """
    )
    old.commit()
    old.close()
    upgraded = ProjectStateStore.open(before, fixture_clock())
    try:
        essay = status_of(upgraded, "assignment-essay")
        school = upgraded.status_reports("assignment-essay")
    finally:
        upgraded.close()
    twice = ProjectStateStore.open(before, fixture_clock())
    try:
        tables = {
            str(row[0])
            for row in twice._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        twice.close()

    assert (status.status, status.note, status.reported_on) == (
        "done",
        "Turned in on paper.",
        TODAY,
    )
    assert history == [saved.report]
    assert (essay.work_state, essay.head) == ("unreported", None)
    assert essay.school[SourceChannel.EMAIL].status == "missing"
    assert not essay.check_the_school_record
    assert len(school) == 1
    assert "student_reports" in tables


def test_the_sample_set_seeds_her_report_once_and_the_synthetic_set_seeds_none(
    tmp_path: pathlib.Path,
) -> None:
    """The sample carries one report of hers, seeded with the rest into a blank file and
    never again; the synthetic set the suite runs against carries none."""
    sample = read_whole(FixtureSource(FIXTURES.parent / "sample"))
    synthetic = read_whole(FixtureSource(FIXTURES))
    path = tmp_path / "sample.sqlite3"
    store = ProjectStateStore.initialize(
        path, fixture_clock(), lambda: read_whole(FixtureSource(FIXTURES.parent / "sample"))
    )
    try:
        seeded = store.student_report_heads()
        undone = store.undo_report(
            sample.student_reports[0].assignment_id,
            sample.student_reports[0].report_id,
            now=NOW,
            today=TODAY,
        )
        assert isinstance(undone, Undone)
    finally:
        store.close()
    again = ProjectStateStore.initialize(
        path, fixture_clock(), lambda: read_whole(FixtureSource(FIXTURES.parent / "sample"))
    )
    try:
        after_restart = again.student_report_heads()
    finally:
        again.close()

    assert len(sample.student_reports) == 1
    assert sample.student_reports[0].status == "done"
    assert sample.student_reports[0].assignment_id in {
        item.assignment_id for item in sample.assignments
    }
    assert synthetic.student_reports == []
    assert list(seeded) == [sample.student_reports[0].assignment_id]
    assert after_restart[sample.student_reports[0].assignment_id] == undone.report


def test_what_she_reports_is_part_of_what_a_plan_is_made_from_within_its_bounds(
    tmp_path: pathlib.Path,
) -> None:
    """A "not yet", the words with it, and a "done" each change the week's fingerprint; the
    words with a "done" do not, since finished work is out of what a plan is built on; an
    undo that restores the week's input restores its fingerprint; and the work reported
    done is out of the week's active set while it stands."""
    store = a_store(tmp_path / "blossom.sqlite3")

    def week() -> tuple[str, list[str], list[str]]:
        read = read_week(store, store, TODAY)
        return (
            planning_digest(read),
            [item.assignment_id for item in read.active()],
            read.done_ids(),
        )

    try:
        before = week()
        not_yet = store.report_status(
            PRACTICE, "not_yet", None, expected_head=None, now=NOW, today=TODAY
        )
        assert isinstance(not_yet, Saved)
        said_not_yet = week()
        with_words = store.report_status(
            PRACTICE,
            "not_yet",
            "Half left.",
            expected_head=not_yet.report.report_id,
            now=NOW,
            today=TODAY,
        )
        assert isinstance(with_words, Saved)
        said_more = week()
        done = store.report_status(
            PRACTICE,
            "done",
            "All of it.",
            expected_head=with_words.report.report_id,
            now=NOW,
            today=TODAY,
        )
        assert isinstance(done, Saved)
        said_done = week()
        other_words = store.report_status(
            PRACTICE,
            "done",
            "Every bit.",
            expected_head=done.report.report_id,
            now=NOW,
            today=TODAY,
        )
        assert isinstance(other_words, Saved)
        said_done_otherwise = week()
        undone = store.undo_report(PRACTICE, other_words.report.report_id, now=NOW, today=TODAY)
        assert isinstance(undone, Undone)
        restored_done = week()
        back_to_words = store.report_status(
            PRACTICE,
            "not_yet",
            "Half left.",
            expected_head=undone.report.report_id,
            now=NOW,
            today=TODAY,
        )
        assert isinstance(back_to_words, Saved)
        restored_words = week()
    finally:
        store.close()

    assert before[1:] == ([LOG, PRACTICE], [])
    assert said_not_yet[1:] == ([LOG, PRACTICE], [])
    assert said_done[1:] == ([LOG], [PRACTICE])
    assert len({before[0], said_not_yet[0], said_more[0], said_done[0]}) == 4
    assert said_done_otherwise[0] == said_done[0]
    assert restored_done == said_done
    assert restored_words == said_more


def test_a_report_restored_twice_over_keeps_its_own_day(tmp_path: pathlib.Path) -> None:
    """Not yet on the 14th, done on the 15th and undone, done again on the 16th and undone:
    what stands is the first report, with the 14th as its day and the last undo's day as
    the day it was restored, however many undos lie between."""
    store = a_store(tmp_path / "blossom.sqlite3")

    def at(day: int) -> tuple[datetime, date]:
        return datetime(2026, 9, day, 23, 0, tzinfo=UTC), date(2026, 9, day)

    try:
        first = store.report_status(
            PRACTICE, "not_yet", "Half left.", expected_head=None, now=at(14)[0], today=at(14)[1]
        )
        assert isinstance(first, Saved)
        head = first.report.report_id
        for day in (15, 16, 17):
            now, today = at(day)
            done = store.report_status(
                PRACTICE, "done", None, expected_head=head, now=now, today=today
            )
            assert isinstance(done, Saved)
            undone = store.undo_report(PRACTICE, done.report.report_id, now=now, today=today)
            assert isinstance(undone, Undone)
            head = undone.report.report_id
        status = status_of(store, PRACTICE)
        chain = store.student_reports(PRACTICE)
    finally:
        store.close()

    assert [event.operation for event in chain] == [
        "report",
        "report",
        "undo",
        "report",
        "undo",
        "report",
        "undo",
    ]
    assert status.asserted == first.report
    assert standing_report(chain) == first.report
    assert standing_report(chain[:3]) == first.report
    assert standing_report(chain[:2]) == chain[1]
    assert standing_report([]) is None
    assert (status.status, status.note) == ("not_yet", "Half left.")
    assert (status.reported_on, status.restored_on) == (date(2026, 9, 14), date(2026, 9, 17))


def an_event(
    report_id: str, operation: str, status: str | None, **more: str | None
) -> dict[str, object]:
    return {
        "report_id": report_id,
        "assignment_id": PRACTICE,
        "operation": operation,
        "status": status,
        "note": more.get("note"),
        "reported_at": NOW.isoformat(),
        "reported_on": TODAY.isoformat(),
        "previous_report_id": more.get("previous"),
        "undoes_report_id": more.get("undoes"),
    }


@pytest.mark.parametrize(
    ("shape", "said"),
    [
        (an_event("u", "undo", None), "must follow the report it takes back"),
        (an_event("u", "undo", None, previous="a"), "must follow the report it takes back"),
        (an_event("u", "undo", None, previous="a", undoes="b"), "must follow the report"),
        (an_event("r", "report", None), "says neither done nor not yet"),
        (an_event("r", "report", "done", previous="a", undoes="a"), "as only an undo does"),
        (an_event("u", "undo", None, note="words", previous="a", undoes="a"), "with no status"),
    ],
)
def test_an_event_that_is_not_whole_is_refused_where_it_is_read(
    shape: dict[str, object], said: str
) -> None:
    with pytest.raises(ValidationError, match=said):
        StudentReport.model_validate(shape)


@pytest.mark.parametrize(
    ("events", "said"),
    [
        (
            [
                an_event("a", "report", "not_yet", note="Half left."),
                an_event("b", "report", "done", previous="a"),
                an_event("u", "undo", "done", previous="b", undoes="b"),
            ],
            "does not restore what stood before",
        ),
        (
            [
                an_event("a", "report", "done"),
                an_event("u", "undo", "not_yet", previous="a", undoes="a"),
            ],
            "does not restore what stood before",
        ),
        (
            [
                an_event("a", "report", "done"),
                an_event("u", "undo", None, previous="a", undoes="a"),
                an_event("v", "undo", None, previous="u", undoes="u"),
            ],
            "does not take back a report at the head",
        ),
        (
            [
                an_event("a", "report", "done"),
                an_event("b", "report", "not_yet", previous="a"),
                an_event("u", "undo", None, previous="a", undoes="a"),
            ],
            "does not follow the head",
        ),
    ],
)
def test_a_seeded_chain_is_held_to_the_same_rules_as_her_own_saves(
    tmp_path: pathlib.Path, events: list[dict[str, object]], said: str
) -> None:
    """A seed whose undo restores something other than what stood before, takes back an
    undo, or names an event that is not the head is refused by name, and the file the
    start would have made is not left behind."""
    path = tmp_path / "blossom.sqlite3"
    seed = Seed(
        [a_row(PRACTICE, "Weekly practice")],
        {},
        [StudentReport.model_validate(event) for event in events],
    )

    with pytest.raises(ValueError, match=said):
        ProjectStateStore.initialize(path, fixture_clock(), lambda: seed)

    assert not path.exists()


def test_a_well_formed_seeded_chain_is_kept_and_read_like_her_own(tmp_path: pathlib.Path) -> None:
    events = [
        an_event("a", "report", "not_yet", note="Half left."),
        an_event("b", "report", "done", previous="a"),
        an_event("u", "undo", "not_yet", note="Half left.", previous="b", undoes="b"),
    ]
    seed = Seed(
        [a_row(PRACTICE, "Weekly practice")],
        {},
        [StudentReport.model_validate(event) for event in events],
    )
    store = ProjectStateStore.initialize(
        tmp_path / "blossom.sqlite3", fixture_clock(), lambda: seed
    )
    try:
        status = status_of(store, PRACTICE)
    finally:
        store.close()

    assert status.head is not None
    assert status.head.report_id == "u"
    assert status.asserted is not None
    assert status.asserted.report_id == "a"
    assert (status.status, status.note) == ("not_yet", "Half left.")


def test_a_form_must_name_one_of_the_assignments_own_updates_before_anything_else(
    tmp_path: pathlib.Path,
) -> None:
    """A name that is no event, or another assignment's event, is refused whatever the form
    says, the same update as the one standing included, and nothing is written. A real
    but stale name with the same update is already saved; with another it conflicts; and
    no name at all is the first save's."""
    store = a_store(tmp_path / "blossom.sqlite3")
    try:
        first = store.report_status(
            PRACTICE, "done", None, expected_head=None, now=NOW, today=TODAY
        )
        assert isinstance(first, Saved)
        other = store.report_status(LOG, "done", None, expected_head=None, now=NOW, today=TODAY)
        assert isinstance(other, Saved)
        second = store.report_status(
            PRACTICE, "not_yet", None, expected_head=first.report.report_id, now=NOW, today=TODAY
        )
        assert isinstance(second, Saved)
        for name in ("report-000000000000", "not an id", other.report.report_id):
            for status in ("not_yet", "done"):
                with pytest.raises(UnknownReport):
                    store.report_status(
                        PRACTICE, status, None, expected_head=name, now=NOW, today=TODAY
                    )
        stale_and_same = store.report_status(
            PRACTICE, "not_yet", None, expected_head=first.report.report_id, now=NOW, today=TODAY
        )
        stale_and_other = store.report_status(
            PRACTICE, "done", None, expected_head=first.report.report_id, now=NOW, today=TODAY
        )
        history = store.student_reports(PRACTICE)
    finally:
        store.close()

    assert isinstance(stale_and_same, AlreadySaved)
    assert isinstance(stale_and_other, Conflict)
    assert [event.status for event in history] == ["done", "not_yet"]


def test_the_words_that_stand_keep_their_day_through_a_restart_and_clocks_out_of_order(
    tmp_path: pathlib.Path,
) -> None:
    """Not yet on the 10th, done on the 11th, undone on the 12th, done on the 13th, undone
    on the 14th: what stands is the 10th's report, restored on the 14th, by the order the
    events were kept in and not by their clocks, which here run backward, and the same
    after the file is opened again."""
    path = tmp_path / "blossom.sqlite3"
    store = a_store(path)

    def at(day: int, hour: int) -> tuple[datetime, date]:
        return datetime(2026, 9, day, hour, 0, tzinfo=UTC), date(2026, 9, day)

    try:
        first = store.report_status(
            PRACTICE,
            "not_yet",
            "Half left.",
            expected_head=None,
            now=at(10, 23)[0],
            today=at(10, 23)[1],
        )
        assert isinstance(first, Saved)
        done = store.report_status(
            PRACTICE,
            "done",
            None,
            expected_head=first.report.report_id,
            now=at(11, 9)[0],
            today=at(11, 9)[1],
        )
        assert isinstance(done, Saved)
        undone = store.undo_report(
            PRACTICE, done.report.report_id, now=at(12, 8)[0], today=at(12, 8)[1]
        )
        assert isinstance(undone, Undone)
        again = store.report_status(
            PRACTICE,
            "done",
            None,
            expected_head=undone.report.report_id,
            now=at(13, 7)[0],
            today=at(13, 7)[1],
        )
        assert isinstance(again, Saved)
        late = datetime(2026, 9, 9, 6, 0, tzinfo=UTC)
        last = store.undo_report(
            PRACTICE, again.report.report_id, now=late, today=date(2026, 9, 14)
        )
        assert isinstance(last, Undone)
        before_restart = status_of(store, PRACTICE)
    finally:
        store.close()
    reopened = ProjectStateStore.open(path, fixture_clock())
    try:
        after_restart = status_of(reopened, PRACTICE)
    finally:
        reopened.close()

    for status in (before_restart, after_restart):
        assert status.asserted == first.report
        assert status.head == last.report
        assert (status.status, status.note) == ("not_yet", "Half left.")
        assert (status.reported_on, status.restored_on) == (date(2026, 9, 10), date(2026, 9, 14))
        rows = status.history_rows
        assert [row.event.operation for row in rows] == [
            "report",
            "report",
            "undo",
            "report",
            "undo",
        ]
        assert [None if row.restored is None else row.restored.reported_on for row in rows] == [
            None,
            None,
            date(2026, 9, 10),
            None,
            date(2026, 9, 10),
        ]


def test_a_chain_whose_links_lead_nowhere_or_round_gives_no_report_a_borrowed_day() -> None:
    """Links the store would never write, made here by hand: the walk ends with no report
    rather than put a correction's day on her words."""

    def made(report_id: str, operation: str, **links: str | None) -> StudentReport:
        return StudentReport.model_construct(
            report_id=report_id,
            assignment_id=PRACTICE,
            operation=operation,
            status="not_yet",
            note=None,
            reported_at=NOW,
            reported_on=TODAY,
            previous_report_id=links.get("previous"),
            undoes_report_id=links.get("undoes"),
        )

    nowhere = [made("u", "undo", previous="gone", undoes="gone")]
    ring = [
        made("a", "undo", previous="b", undoes="b"),
        made("b", "undo", previous="a", undoes="a"),
    ]

    assert standing_report(nowhere) is None
    assert standing_report(ring) is None
