"""The family's check of her Done beside a school Missing: marked on the family page,
reopened there, shown on her card.

Synthetic fixtures, a pinned clock, and forms alone: what a check is made
against, who may make one, what a check meets when the facts or the record
moved since the page was made, what reopens a checked row and what does
not, and that none of it touches her account, the school's, a plan, or the
digest.
"""

import pathlib
import sqlite3
import threading
from datetime import date

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from pydantic import ValidationError

from blossom.app import create_app
from blossom.assignment_status import (
    Standing,
    basis_of,
    basis_parts,
    standing_after_each,
    states_after_each,
)
from blossom.noticing import planning_digest, read_week
from blossom.reconciliation import SourceChannel
from blossom.routes import parent as parent_routes
from blossom.routes.parent import ASSIGNMENTS_CHANGED as THEIR_ASSIGNMENTS_CHANGED
from blossom.routes.parent import (
    BAD_CHECK_FORM,
    CHECK_ALREADY,
    CHECK_FACTS_CHANGED,
    CHECK_MOVED_ON,
    CHECK_NOT_REOPENED,
    CHECK_NOT_SAVED,
    CHECK_NOTE_TOO_LONG,
    CHECK_RECORDED,
    CHECK_REOPENED,
    NOT_ON_RECORD,
    NOT_THIS_ROWS,
)
from blossom.routes.runs import plan_graphs
from blossom.routes.student import ASSIGNMENTS_CHANGED
from blossom.stores.project_state import (
    AlreadyChecked,
    CheckConflict,
    Checked,
    FamilyCheck,
    NoteTooLong,
    ProjectStateStore,
    Reopened,
    Saved,
    StudentReport,
    Undone,
    UnknownAssignment,
    UnknownCheck,
)
from tests.support import (
    ESSAY_ID,
    ESSAY_TITLE,
    FIXTURE_WEEK,
    HER_PAGE,
    HERS,
    MISSING_EMAIL,
    PAGE_HEADERS,
    PLAN_DATE,
    PRACTICE,
    PRACTICE_LOG,
    SAID_AT,
    SAID_ON,
    SAME_ORIGIN,
    THEIRS,
    Answer,
    accepting,
    browser,
    card_for,
    fixture_clock,
    fixture_week_plan,
    hidden,
    practice_store,
    report,
    school_missing,
    school_said,
    scripted_graphs,
    signed_in_household,
    state_of,
    status_of,
)

HELPER = "This records your check here. It does not change her update or the school's report."
HEADINGS = ("Worth checking together", "Checked recently", "Recent updates", "School reports")
RECORDED = str(escape(CHECK_RECORDED))
FACTS_CHANGED = str(escape(CHECK_FACTS_CHANGED))
NOT_THIS = str(escape(NOT_THIS_ROWS))
"""The sentences as the pages write them, an apostrophe escaped."""


def event(
    report_id: str,
    operation: str,
    status: str | None,
    note: str | None = None,
    *,
    previous: str | None = None,
    undoes: str | None = None,
) -> StudentReport:
    return StudentReport(
        report_id=report_id,
        assignment_id=PRACTICE,
        operation=operation,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        note=note,
        reported_at=SAID_AT,
        reported_on=SAID_ON,
        previous_report_id=previous,
        undoes_report_id=undoes,
    )


def a_check(**over: object) -> FamilyCheck:
    fields: dict[str, object] = {
        "check_id": "check-1",
        "assignment_id": PRACTICE,
        "operation": "checked",
        "basis": f"{PRACTICE}|report-1|EMAIL:missing:2026-09-10",
        "checked_at": SAID_AT,
        "checked_on": SAID_ON,
    }
    fields.update(over)
    return FamilyCheck(**fields)  # type: ignore[arg-type]


# ------------------------------------------------------------- the Done period and the basis


def test_a_done_period_begins_where_her_account_enters_done_and_a_note_change_keeps_it() -> None:
    """Not yet, then Done, then Done with a note, then Not yet, then Done: two periods, the
    first begun by the second report and kept through the note, the second begun by the
    last report. The words that stand are read in the same pass."""
    chain = [
        event("r1", "report", "not_yet"),
        event("r2", "report", "done", previous="r1"),
        event("r3", "report", "done", "On paper.", previous="r2"),
        event("r4", "report", "not_yet", previous="r3"),
        event("r5", "report", "done", previous="r4"),
    ]
    states = states_after_each(chain)

    assert [state.done_since for state in states] == [None, "r2", "r2", None, "r5"]
    assert [state.report for state in states] == standing_after_each(chain)
    assert [state.report.report_id for state in states if state.report] == [
        "r1",
        "r2",
        "r3",
        "r4",
        "r5",
    ]
    assert states_after_each([]) == []


def test_an_undo_restores_the_period_that_stood_and_is_never_a_period_of_its_own() -> None:
    """Taking back a note change restores the first Done and its period; taking back a Not
    yet restores the Done before it, period included; taking back the only Done leaves no
    period, and a Done after that begins a new one. An undo that leads nowhere restores
    nothing."""
    chain = [
        event("r1", "report", "done"),
        event("r2", "report", "done", "Both parts.", previous="r1"),
        event("u2", "undo", "done", previous="r2", undoes="r2"),
        event("r3", "report", "not_yet", previous="u2"),
        event("u3", "undo", "done", previous="r3", undoes="r3"),
    ]
    first_only = [
        event("r1", "report", "done"),
        event("u1", "undo", None, previous="r1", undoes="r1"),
        event("r2", "report", "done", previous="u1"),
    ]
    astray = [event("u9", "undo", "done", previous="r9", undoes="r9")]

    assert [s.done_since for s in states_after_each(chain)] == ["r1", "r1", "r1", None, "r1"]
    assert [s.done_since for s in states_after_each(first_only)] == ["r1", None, "r2"]
    assert states_after_each(astray) == [Standing(None, None)]


def test_the_basis_names_the_assignment_the_period_and_each_current_missing_in_one_order(
    tmp_path: pathlib.Path,
) -> None:
    """No basis without her Done, none without a school missing. With both, the line is the
    assignment, the report that began the Done, and each channel's current missing, sorted;
    a second channel adds one, an older statement arriving late adds nothing, and a channel
    that says something else drops out."""
    store = practice_store(tmp_path / "blossom.sqlite3")
    try:
        store.record_status_reports(PRACTICE, [school_missing(date(2026, 9, 10))])
        without_done = status_of(store, PRACTICE)
        saved = store.report_status(
            PRACTICE, "done", None, expected_head=None, now=SAID_AT, today=SAID_ON
        )
        assert isinstance(saved, Saved)
        one = status_of(store, PRACTICE)
        store.record_status_reports(
            PRACTICE, [school_said("missing", SourceChannel.LMS, date(2026, 9, 12))]
        )
        two = status_of(store, PRACTICE)
        store.record_status_reports(PRACTICE, [school_missing(date(2026, 9, 1))])
        late = status_of(store, PRACTICE)
        store.record_status_reports(
            PRACTICE, [school_said("turned_in", SourceChannel.EMAIL, date(2026, 9, 14))]
        )
        cleared = status_of(store, PRACTICE)
        nothing = status_of(store, PRACTICE_LOG)
    finally:
        store.close()

    since = saved.report.report_id
    assert without_done.check_basis is None
    assert not without_done.needs_a_check
    assert one.done_since == since
    assert one.check_basis == f"{PRACTICE}|{since}|EMAIL:missing:2026-09-10"
    assert one.check_basis == basis_of(PRACTICE, since, one.missing_reports)
    assert (one.needs_a_check, one.checked, one.check, one.check_head_id) == (
        True,
        False,
        None,
        None,
    )
    assert two.check_basis == f"{PRACTICE}|{since}|EMAIL:missing:2026-09-10|LMS:missing:2026-09-12"
    assert late.check_basis == two.check_basis
    assert cleared.check_basis == f"{PRACTICE}|{since}|LMS:missing:2026-09-12"
    assert basis_parts(two.check_basis) == (
        PRACTICE,
        since,
        ("EMAIL:missing:2026-09-10", "LMS:missing:2026-09-12"),
    )
    assert basis_parts("lone") == ("lone", "", ())
    assert (nothing.done_since, nothing.check_basis) == (None, None)


# ------------------------------------------------------------- the store


def test_a_check_is_appended_once_read_back_after_a_restart_and_a_file_from_before_gains_its_table(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "blossom.sqlite3"
    store = practice_store(path)
    try:
        store.record_status_reports(PRACTICE, [school_missing(date(2026, 9, 10))])
        done = store.report_status(
            PRACTICE, "done", None, expected_head=None, now=SAID_AT, today=SAID_ON
        )
        assert isinstance(done, Saved)
        basis = status_of(store, PRACTICE).check_basis
        assert basis is not None

        def basis_now() -> str | None:
            return status_of(store, PRACTICE).check_basis

        marked = store.mark_checked(
            PRACTICE,
            basis,
            "  Teacher has it.\r\nSaid so Tuesday.  ",
            expected_check=None,
            basis_now=basis_now,
            now=SAID_AT,
            today=SAID_ON,
        )
        assert isinstance(marked, Checked)
        again = store.mark_checked(
            PRACTICE,
            basis,
            "\r\nTeacher has it.\r\nSaid so Tuesday.\n ",
            expected_check=None,
            basis_now=basis_now,
            now=SAID_AT,
            today=SAID_ON,
        )
        other_note = store.mark_checked(
            PRACTICE,
            basis,
            "Something else.",
            expected_check=marked.check.check_id,
            basis_now=basis_now,
            now=SAID_AT,
            today=SAID_ON,
        )
    finally:
        store.close()
    reopened = ProjectStateStore.open(path, fixture_clock())
    try:
        status = status_of(reopened, PRACTICE)
        chain = reopened.family_checks(PRACTICE)
    finally:
        reopened.close()

    assert isinstance(again, AlreadyChecked)
    assert again.check == marked.check
    assert isinstance(other_note, CheckConflict)
    assert other_note.head == marked.check
    assert chain == [marked.check]
    assert marked.check.note == "Teacher has it.\nSaid so Tuesday."
    assert (marked.check.operation, marked.check.basis) == ("checked", basis)
    assert (marked.check.checked_at, marked.check.checked_on) == (SAID_AT, SAID_ON)
    assert marked.check.previous_check_id is None
    assert marked.check.check_id.startswith("check-")
    assert status.checked
    assert status.check == marked.check
    assert not status.needs_a_check
    assert status.check_head_id == marked.check.check_id

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
        CREATE TABLE student_reports (
            report_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL, operation TEXT NOT NULL,
            status TEXT, note TEXT, reported_at TEXT NOT NULL, reported_on TEXT NOT NULL,
            previous_report_id TEXT, undoes_report_id TEXT
        );
        INSERT INTO assignments VALUES
            ('assignment-essay', 'World History', 'Canal Era comparison essay', '2026-08-21',
             '', 'missing', NULL, 'HOMEWORK', NULL, NULL);
        INSERT INTO status_reports VALUES
            ('assignment-essay', 'missing', 'EMAIL', '2026-09-09', 'the day it was pasted',
             '2026-09-12T20:00:00+00:00', '09/09');
        INSERT INTO student_reports VALUES
            ('report-1', 'assignment-essay', 'report', 'done', NULL,
             '2026-09-12T21:00:00+00:00', '2026-09-12', NULL, NULL);
        """
    )
    old.commit()
    old.close()
    upgraded = ProjectStateStore.open(before, fixture_clock())
    try:
        tables = {
            str(row[0])
            for row in upgraded._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        kept = status_of(upgraded, "assignment-essay")
        marked_after = upgraded.mark_checked(
            "assignment-essay",
            kept.check_basis or "",
            None,
            expected_check=None,
            basis_now=lambda: status_of(upgraded, "assignment-essay").check_basis,
            now=SAID_AT,
            today=SAID_ON,
        )
    finally:
        upgraded.close()

    assert "family_checks" in tables
    assert kept.check_basis == "assignment-essay|report-1|EMAIL:missing:2026-09-09"
    assert kept.checks == ()
    assert isinstance(marked_after, Checked)


def test_a_check_meets_the_basis_and_the_record_as_they_stand_when_it_is_written(
    tmp_path: pathlib.Path,
) -> None:
    """A basis the facts moved past is a conflict, a Not yet since or a Done begun again; a
    record that moved past the page, reopened since, is a conflict too; the same check
    standing is already made. A note change, the same paste, and an undo that restores the
    period keep the check; a missing the school had not reported before opens the row
    again with the check still in the record."""
    store = practice_store(tmp_path / "blossom.sqlite3")
    try:
        store.record_status_reports(PRACTICE, [school_missing(date(2026, 9, 10))])
        done = store.report_status(
            PRACTICE, "done", None, expected_head=None, now=SAID_AT, today=SAID_ON
        )
        assert isinstance(done, Saved)
        first = status_of(store, PRACTICE).check_basis
        assert first is not None

        def basis_now() -> str | None:
            return status_of(store, PRACTICE).check_basis

        def mark(basis: str, note: str | None, expected: str | None) -> object:
            return store.mark_checked(
                PRACTICE,
                basis,
                note,
                expected_check=expected,
                basis_now=basis_now,
                now=SAID_AT,
                today=SAID_ON,
            )

        not_yet = store.report_status(
            PRACTICE,
            "not_yet",
            None,
            expected_head=done.report.report_id,
            now=SAID_AT,
            today=SAID_ON,
        )
        assert isinstance(not_yet, Saved)
        gone = mark(first, None, None)
        done_again = store.report_status(
            PRACTICE,
            "done",
            None,
            expected_head=not_yet.report.report_id,
            now=SAID_AT,
            today=SAID_ON,
        )
        assert isinstance(done_again, Saved)
        renewed = basis_now()
        assert renewed is not None
        stale = mark(first, None, None)
        marked = mark(renewed, None, None)
        assert isinstance(marked, Checked)
        behind = mark(renewed, "Late note.", None)
        the_same = mark(renewed, None, None)

        def reopen(check_id: str, shown: str | None, *, of: str = PRACTICE) -> object:
            return store.check_again(
                of,
                check_id,
                expected_basis=shown,
                basis_now=basis_now,
                now=SAID_AT,
                today=SAID_ON,
            )

        reopened = reopen(marked.check.check_id, renewed)
        assert isinstance(reopened, Reopened)
        after_reopening = status_of(store, PRACTICE)
        from_the_old_page = mark(renewed, None, marked.check.check_id)
        fresh = mark(renewed, "Seen.", reopened.check.check_id)
        assert isinstance(fresh, Checked)
        noted = store.report_status(
            PRACTICE,
            "done",
            "Both parts.",
            expected_head=done_again.report.report_id,
            now=SAID_AT,
            today=SAID_ON,
        )
        assert isinstance(noted, Saved)
        after_the_note = status_of(store, PRACTICE).check
        store.record_status_reports(PRACTICE, [school_missing(date(2026, 9, 10))])
        after_the_same_paste = status_of(store, PRACTICE).check
        undone = store.undo_report(PRACTICE, noted.report.report_id, now=SAID_AT, today=SAID_ON)
        assert isinstance(undone, Undone)
        after_the_undo = status_of(store, PRACTICE).check
        store.record_status_reports(
            PRACTICE, [school_said("missing", SourceChannel.LMS, date(2026, 9, 12))]
        )
        moved = status_of(store, PRACTICE)
        from_before_the_portal = reopen(fresh.check.check_id, renewed)
        reopened_twice = reopen(fresh.check.check_id, moved.check_basis)
        once_more = reopen(fresh.check.check_id, moved.check_basis)
        with pytest.raises(UnknownCheck):
            mark(renewed, None, "check-nowhere")
        with pytest.raises(UnknownCheck):
            reopen("check-nowhere", moved.check_basis)
        with pytest.raises(UnknownCheck):
            reopen(fresh.check.check_id, None, of=PRACTICE_LOG)
        with pytest.raises(UnknownAssignment):
            store.mark_checked(
                "assignment-nowhere",
                renewed,
                None,
                expected_check=None,
                basis_now=basis_now,
                now=SAID_AT,
                today=SAID_ON,
            )
        with pytest.raises(NoteTooLong):
            mark(renewed, "x" * 501, None)
        chain = store.family_checks(PRACTICE)
        log_chain = store.family_checks(PRACTICE_LOG)
    finally:
        store.close()

    assert isinstance(gone, CheckConflict)
    assert gone.head is None
    assert renewed != first
    assert basis_parts(renewed)[1] == done_again.report.report_id
    assert isinstance(stale, CheckConflict)
    assert isinstance(behind, CheckConflict)
    assert behind.head == marked.check
    assert isinstance(the_same, AlreadyChecked)
    assert the_same.check == marked.check
    assert isinstance(from_before_the_portal, CheckConflict)
    assert from_before_the_portal.head == fresh.check
    assert (reopened.check.operation, reopened.check.basis, reopened.check.note) == (
        "reopened",
        renewed,
        None,
    )
    assert reopened.check.previous_check_id == marked.check.check_id
    assert after_reopening.needs_a_check
    assert after_reopening.check is None
    assert after_reopening.check_head == reopened.check
    assert isinstance(from_the_old_page, CheckConflict)
    assert from_the_old_page.head == reopened.check
    assert fresh.check.previous_check_id == reopened.check.check_id
    assert after_the_note == fresh.check
    assert after_the_same_paste == fresh.check
    assert after_the_undo == fresh.check
    assert moved.needs_a_check
    assert moved.check is None
    assert moved.check_head == fresh.check
    assert isinstance(reopened_twice, Reopened)
    assert isinstance(once_more, CheckConflict)
    assert once_more.head == reopened_twice.check
    assert [item.operation for item in chain] == ["checked", "reopened", "checked", "reopened"]
    assert log_chain == []


def test_a_check_event_has_one_of_two_shapes() -> None:
    with pytest.raises(ValidationError, match="names no basis"):
        a_check(basis="   ")
    with pytest.raises(ValidationError, match="carries a note"):
        a_check(operation="reopened", note="x", previous_check_id="check-0")
    with pytest.raises(ValidationError, match="must follow"):
        a_check(operation="reopened")
    with pytest.raises(ValidationError, match="at most 500"):
        a_check(note="x" * 501)
    with pytest.raises(ValidationError):
        a_check(operation="undone", previous_check_id="check-0")

    kept = a_check(note="  A line.\r\nTwo.  ")
    blank = a_check(note="   ")
    reopening = a_check(operation="reopened", previous_check_id="check-0")

    assert kept.note == "A line.\nTwo."
    assert blank.note is None
    assert reopening.note is None


# ------------------------------------------------------------- the pages


def row_for(page: str, assignment_id: str) -> str:
    """One row of the assignment updates, whole: from its id to the next row's, or the
    section's end."""
    start = page.index(f'id="update-{assignment_id}"')
    ends = [
        found
        for found in (page.find('id="update-', start + 1), page.find("</section>", start))
        if found >= 0
    ]
    return page[start : min(ends)] if ends else page[start:]


def where(page: str, assignment_id: str) -> str:
    """The group heading a row is under."""
    start = page.index(f'id="update-{assignment_id}"')
    return max(HEADINGS, key=lambda heading: page.rfind(heading, 0, start))


def family_page(client: TestClient) -> str:
    return client.get("/parent", headers=PAGE_HEADERS).text


def a_discrepancy(client: TestClient) -> None:
    """The school's email says the essay is missing; she reports it done."""
    told = client.post("/parent/inbox/keep", data={"text": MISSING_EMAIL})
    assert told.status_code == 303
    report(client, ESSAY_ID, "done", "Handed in Tuesday.")


def mark(client: TestClient, assignment_id: str, note: str = "") -> Answer:
    """Mark the row checked from the family page as it stands, with the fields it carries."""
    row = row_for(family_page(client), assignment_id)
    return client.post(
        f"/parent/actions/checks/{assignment_id}/mark",
        data={
            "basis": hidden(row, "basis"),
            "expected_check_id": hidden(row, "expected_check_id"),
            "note": note,
        },
        headers=PAGE_HEADERS,
    )


def check_again(client: TestClient, assignment_id: str) -> Answer:
    """Reopen the check from the family page as it stands."""
    row = row_for(family_page(client), assignment_id)
    return client.post(
        f"/parent/actions/checks/{assignment_id}/again",
        data={"check_id": hidden(row, "check_id"), "basis": hidden(row, "basis")},
        headers=PAGE_HEADERS,
    )


def undo(client: TestClient, assignment_id: str) -> None:
    """Take back her latest update from her card."""
    page = client.get(
        HER_PAGE, params={"week": FIXTURE_WEEK, "show": assignment_id}, headers=PAGE_HEADERS
    ).text
    head = hidden(card_for(page, assignment_id), "report_id")
    answer = client.post(
        f"/student/actions/assignments/{assignment_id}/undo-report",
        data={"report_id": head, "week": FIXTURE_WEEK},
    )
    assert answer.status_code == 303, answer.text[:300]


def test_the_family_page_offers_mark_checked_and_a_check_shows_on_both_pages() -> None:
    """The row worth checking carries the form: a note for her card, the basis, the check
    the row showed, the helper words, and one button, a form alone. Marked, the page comes
    back at the row, folded under Checked recently with the day, the note, both accounts,
    and Check again; her card says a parent checked it with her, with the note."""
    with browser() as client:
        a_discrepancy(client)
        before = family_page(client)
        row = row_for(before, ESSAY_ID)
        marked = mark(client, ESSAY_ID, "Teacher has it on paper.\r\nSaid so Tuesday.")
        after = client.get(marked.headers["location"], headers=PAGE_HEADERS).text
        hers = card_for(client.get(HER_PAGE, headers=PAGE_HEADERS).text, ESSAY_ID)
        chain = state_of(client).project_state.family_checks(ESSAY_ID)

    assert "<h3>Worth checking together</h3>" in before
    assert f'<article class="draft needs-review" id="update-{ESSAY_ID}" tabindex="-1">' in before
    assert f'action="/parent/actions/checks/{ESSAY_ID}/mark"' in row
    assert HELPER in row
    assert (
        f'<label for="check-note-{ESSAY_ID}">Note for her card (optional)<span '
        f'class="visually-hidden"> for {ESSAY_TITLE} (World History)</span></label>'
    ) in row
    assert row.index("<textarea") < row.index("Mark checked</button>")
    assert "Up to 500 characters. She reads this on her card." in row
    assert hidden(row, "expected_check_id") == ""
    assert hidden(row, "basis").startswith(f"{ESSAY_ID}|report-")
    assert hidden(row, "basis").endswith("|EMAIL:missing:2026-08-19")
    assert f'aria-label="Mark checked: {ESSAY_TITLE}"' in row
    assert "maxlength" not in row
    assert "data-pending" not in row
    assert "<script" not in row
    assert marked.status_code == 303
    assert marked.headers["location"] == f"/parent?checked={ESSAY_ID}#update-{ESSAY_ID}"
    assert "<h3>Worth checking together</h3>" not in after
    assert '<details class="steps checked-recently" open>' in after
    assert "<summary>Checked recently (1)</summary>" in after
    checked = row_for(after, ESSAY_ID)
    assert where(after, ESSAY_ID) == "Checked recently"
    assert RECORDED in checked
    assert (
        "A parent marked this checked with her on August 19. The note with it: "
        "<q>Teacher has it on paper.\nSaid so Tuesday.</q>"
    ) in checked
    assert "She reported it done on August 19. She wrote: <q>Handed in Tuesday.</q>" in checked
    assert "The school reports it missing." in checked
    assert f'action="/parent/actions/checks/{ESSAY_ID}/again"' in checked
    assert f'aria-label="Check again: {ESSAY_TITLE}"' in checked
    assert hidden(checked, "check_id") == chain[0].check_id
    assert f'action="/parent/actions/checks/{ESSAY_ID}/mark"' not in after
    assert "Check again reopens it here." in after
    assert (
        "A parent marked this checked with you on August 19. The note with it: "
        "<q>Teacher has it on paper.\nSaid so Tuesday.</q>"
    ) in hers
    assert '<span class="pill">Your update: Done</span>' in hers
    assert [(item.operation, item.note) for item in chain] == [
        ("checked", "Teacher has it on paper.\nSaid so Tuesday.")
    ]


def test_only_a_parents_device_marks_a_check_and_the_check_holds_through_a_restart(
    tmp_path: pathlib.Path,
) -> None:
    """With the sign-in on, her device is answered 403 at both check paths and nothing is
    written; a parent's device marks the check. Started again on the same file, the family
    page and her card still show it, and her card as a parent reads it says "with her"."""
    settings = signed_in_household(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": THEIRS})
        assert client.post("/parent/inbox/keep", data={"text": MISSING_EMAIL}).status_code == 303
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": HERS})
        report(client, ESSAY_ID, "done")
        hers = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/mark",
            data={"basis": "x", "expected_check_id": "", "note": ""},
            headers=PAGE_HEADERS,
        )
        hers_again = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/again",
            data={"check_id": "x"},
            headers=PAGE_HEADERS,
        )
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        marked = mark(client, ESSAY_ID, "On paper.")
        chain = state_of(client).project_state.family_checks(ESSAY_ID)
    with TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": THEIRS})
        family = family_page(client)
        as_parent = card_for(client.get(HER_PAGE, headers=PAGE_HEADERS).text, ESSAY_ID)
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": HERS})
        as_her = card_for(client.get(HER_PAGE, headers=PAGE_HEADERS).text, ESSAY_ID)

    assert (hers.status_code, hers_again.status_code) == (403, 403)
    assert marked.status_code == 303
    assert len(chain) == 1
    assert "<summary>Checked recently (1)</summary>" in family
    assert (
        "A parent marked this checked with her on August 19. The note with it: <q>On paper.</q>"
        in as_parent
    )
    assert (
        "A parent marked this checked with you on August 19. The note with it: <q>On paper.</q>"
        in as_her
    )


def test_a_check_from_a_page_the_facts_or_the_record_moved_past_is_refused_with_what_stands() -> (
    None
):
    """The school's portal says missing too since the page was made: 409, the row shows both
    statements, the note typed is kept in the field. The same check from a page made before
    it was marked is already made. A check from a page made before the row was reopened,
    and a reopening of a check that was reopened already, are 409. A Not yet of hers since
    the page was made is 409 with no form to keep the note in, so the note is said."""
    with browser() as client:
        a_discrepancy(client)
        store = state_of(client).project_state
        first = row_for(family_page(client), ESSAY_ID)
        store.record_status_reports(
            ESSAY_ID, [school_said("missing", SourceChannel.LMS, date(2026, 8, 18))]
        )
        moved = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/mark",
            data={
                "basis": hidden(first, "basis"),
                "expected_check_id": hidden(first, "expected_check_id"),
                "note": "Typed before the portal spoke.",
            },
            headers=PAGE_HEADERS,
        )
        second = row_for(family_page(client), ESSAY_ID)
        marked = mark(client, ESSAY_ID, "Checked both.")
        from_before = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/mark",
            data={
                "basis": hidden(second, "basis"),
                "expected_check_id": hidden(second, "expected_check_id"),
                "note": "Words from the second parent.",
            },
            headers=PAGE_HEADERS,
        )
        retried = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/mark",
            data={
                "basis": hidden(second, "basis"),
                "expected_check_id": hidden(second, "expected_check_id"),
                "note": "  Checked both.\r\n",
            },
            headers=PAGE_HEADERS,
        )
        already = client.get(retried.headers["location"], headers=PAGE_HEADERS).text
        checked = row_for(family_page(client), ESSAY_ID)
        reopened = check_again(client, ESSAY_ID)
        behind = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/mark",
            data={
                "basis": hidden(second, "basis"),
                "expected_check_id": hidden(second, "expected_check_id"),
                "note": "",
            },
            headers=PAGE_HEADERS,
        )
        twice = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/again",
            data={"check_id": hidden(checked, "check_id"), "basis": hidden(checked, "basis")},
            headers=PAGE_HEADERS,
        )
        third = row_for(family_page(client), ESSAY_ID)
        report(client, ESSAY_ID, "not_yet", "Found a page left.")
        gone = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/mark",
            data={
                "basis": hidden(third, "basis"),
                "expected_check_id": hidden(third, "expected_check_id"),
                "note": "Typed before her change.",
            },
            headers=PAGE_HEADERS,
        )
        chain = store.family_checks(ESSAY_ID)

    assert moved.status_code == 409
    assert FACTS_CHANGED in moved.text
    assert f'<a href="#update-{ESSAY_ID}">Go to the row.</a>' in moved.text
    moved_row = row_for(moved.text, ESSAY_ID)
    assert f'id="check-problem-{ESSAY_ID}">{FACTS_CHANGED}</p>' in moved_row
    assert "Typed before the portal spoke.</textarea>" in moved_row
    assert "school portal" in moved_row
    assert "school email" in moved_row
    assert hidden(moved_row, "basis").endswith("|EMAIL:missing:2026-08-19|LMS:missing:2026-08-18")
    assert marked.status_code == 303
    assert from_before.status_code == 409
    second_row = row_for(from_before.text, ESSAY_ID)
    assert where(from_before.text, ESSAY_ID) == "Checked recently"
    assert CHECK_MOVED_ON in second_row
    assert "The note with it: <q>Checked both.</q>" in second_row
    assert (
        "The note typed with it was not saved: <q>Words from the second parent.</q>"
    ) in second_row
    assert retried.status_code == 303
    assert retried.headers["location"] == f"/parent?checked_already={ESSAY_ID}#update-{ESSAY_ID}"
    assert CHECK_ALREADY in row_for(already, ESSAY_ID)
    assert "<q>Checked both.</q>" in row_for(already, ESSAY_ID)
    assert "Words from the second parent." not in already
    assert reopened.status_code == 303
    assert reopened.headers["location"] == f"/parent?reopened={ESSAY_ID}#update-{ESSAY_ID}"
    assert behind.status_code == 409
    assert CHECK_MOVED_ON in row_for(behind.text, ESSAY_ID)
    assert where(behind.text, ESSAY_ID) == "Worth checking together"
    assert twice.status_code == 409
    assert CHECK_MOVED_ON in row_for(twice.text, ESSAY_ID)
    assert gone.status_code == 409
    assert where(gone.text, ESSAY_ID) == "Recent updates"
    gone_row = row_for(gone.text, ESSAY_ID)
    assert FACTS_CHANGED in gone_row
    assert "The note typed with it was not saved: <q>Typed before her change.</q>" in gone_row
    assert "<textarea" not in gone_row
    assert [item.operation for item in chain] == ["checked", "reopened"]


def test_what_reopens_a_checked_row_and_what_does_not() -> None:
    """The same email pasted again, a change to her note alone, and an older statement
    arriving late leave the check standing. A Not yet takes the row out of the checked
    ones; taking that back restores the Done, its period, and the check. A Not yet and
    then a Done is a new basis, worth checking again, and the row says the Done is new; a
    missing the school had not reported before opens it again too, and the row says the
    report differs and what a pasted day means."""
    with browser() as client:
        a_discrepancy(client)
        store = state_of(client).project_state
        assert mark(client, ESSAY_ID, "Seen.").status_code == 303
        assert client.post("/parent/inbox/keep", data={"text": MISSING_EMAIL}).status_code == 303
        same_paste = family_page(client)
        report(client, ESSAY_ID, "done", "Handed in Tuesday, both parts.")
        note_only = family_page(client)
        store.record_status_reports(
            ESSAY_ID, [school_said("missing", SourceChannel.EMAIL, date(2026, 8, 10))]
        )
        late = family_page(client)
        report(client, ESSAY_ID, "not_yet", "Found a page left.")
        out = family_page(client)
        undo(client, ESSAY_ID)
        restored = family_page(client)
        hers_restored = card_for(client.get(HER_PAGE, headers=PAGE_HEADERS).text, ESSAY_ID)
        report(client, ESSAY_ID, "not_yet")
        report(client, ESSAY_ID, "done")
        renewed = family_page(client)
        hers_renewed = card_for(client.get(HER_PAGE, headers=PAGE_HEADERS).text, ESSAY_ID)
        assert mark(client, ESSAY_ID).status_code == 303
        store.record_status_reports(
            ESSAY_ID, [school_said("missing", SourceChannel.LMS, date(2026, 8, 18))]
        )
        newly = family_page(client)
        chain = store.family_checks(ESSAY_ID)
        reports = store.student_reports(ESSAY_ID)

    assert where(same_paste, ESSAY_ID) == "Checked recently"
    assert where(note_only, ESSAY_ID) == "Checked recently"
    assert "<q>Handed in Tuesday, both parts.</q>" in row_for(note_only, ESSAY_ID)
    assert where(late, ESSAY_ID) == "Checked recently"
    assert where(out, ESSAY_ID) == "Recent updates"
    out_row = row_for(out, ESSAY_ID)
    assert (
        "A parent marked this checked on August 19, and what it rests on differs now: her "
        "update is not Done. The note with it: <q>Seen.</q></p>"
    ) in out_row
    assert "A parent marked this checked with her" not in out_row
    assert f'aria-label="Check again: {ESSAY_TITLE}"' in out_row
    assert f'action="/parent/actions/checks/{ESSAY_ID}/mark"' not in out_row
    assert where(restored, ESSAY_ID) == "Checked recently"
    assert "restored August 19" in row_for(restored, ESSAY_ID)
    assert (
        "A parent marked this checked with you on August 19. The note with it: <q>Seen.</q>"
        in hers_restored
    )
    assert where(renewed, ESSAY_ID) == "Worth checking together"
    renewed_row = row_for(renewed, ESSAY_ID)
    assert (
        "A parent marked this checked on August 19, and what it rests on differs now: her "
        "Done is a new one. The note with it: <q>Seen.</q></p>"
    ) in renewed_row
    assert "A parent marked this checked with" not in hers_renewed
    assert where(newly, ESSAY_ID) == "Worth checking together"
    newly_row = row_for(newly, ESSAY_ID)
    said = escape(
        "A parent marked this checked on August 19, and what it rests on differs now: the "
        "school's report listed is not the one checked then. A report dated by the day it "
        "was pasted carries that day, which is not proof of a new warning from the school."
    )
    assert f"{said}</p>" in newly_row
    assert "school portal" in newly_row
    assert [item.operation for item in chain] == ["checked", "checked"]
    assert chain[0].basis != chain[1].basis
    assert [item.status for item in reports] == [
        "done",
        "done",
        "not_yet",
        "done",
        "not_yet",
        "done",
    ]


def test_checks_leave_her_account_the_schools_the_plan_and_the_digest_as_they_were() -> None:
    """Marking, reopening, and marking again write only the family's checks: her events, the
    school's reports, the plan she has, and the digest a plan is measured against are the
    same before and after, and neither page says the assignments changed."""
    whole = fixture_week_plan()
    without_the_essay = whole.model_copy(
        update={"blocks": [block for block in whole.blocks if block.assignment_id != ESSAY_ID]}
    )
    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [without_the_essay], lambda: [accepting()]
        )
        a_discrepancy(client)
        state = state_of(client)
        store = state.project_state
        assert client.post("/student/actions/plan").status_code == 303
        record = state.drafts.latest_for(PLAN_DATE)
        assert record is not None

        def snapshot() -> tuple[object, ...]:
            week = read_week(store, store, PLAN_DATE)
            latest = state.drafts.latest_for(PLAN_DATE)
            return (
                store.student_reports(ESSAY_ID),
                store.status_reports_by_assignment(),
                planning_digest(week),
                None if latest is None else (latest.draft_id, latest.plan_assignment_ids),
                [item.assignment_id for item in week.active()],
            )

        before = snapshot()
        assert mark(client, ESSAY_ID, "Seen.").status_code == 303
        assert check_again(client, ESSAY_ID).status_code == 303
        assert mark(client, ESSAY_ID, "Seen twice.").status_code == 303
        after = snapshot()
        hers = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        family = family_page(client)
        chain = store.family_checks(ESSAY_ID)

    assert before == after
    assert record.inputs_digest == before[2]
    assert ASSIGNMENTS_CHANGED not in hers
    assert THEIR_ASSIGNMENTS_CHANGED not in family
    assert [item.operation for item in chain] == ["checked", "reopened", "checked"]
    assert chain[2].previous_check_id == chain[1].check_id


def test_the_check_form_is_read_whole_and_comes_back_with_the_words_kept() -> None:
    """A note past the limit is 422 with the words kept in the field, the field marked and
    tied to the problem, and the cursor put there; a field twice, or a field of another
    form's, is 422 with nothing written; a check that is not this row's, a blank basis, and
    an assignment not on record are refused as such. Nothing is written by any of them."""
    with browser() as client:
        a_discrepancy(client)
        row = row_for(family_page(client), ESSAY_ID)
        basis, head = hidden(row, "basis"), hidden(row, "expected_check_id")
        mark_at = f"/parent/actions/checks/{ESSAY_ID}/mark"
        long = client.post(
            mark_at,
            data={"basis": basis, "expected_check_id": head, "note": "x" * 501},
            headers=PAGE_HEADERS,
        )
        doubled = client.post(
            mark_at,
            data={"basis": [basis, basis], "expected_check_id": head, "note": "Twice."},
            headers=PAGE_HEADERS,
        )
        stranger = client.post(
            mark_at,
            data={
                "basis": basis,
                "expected_check_id": head,
                "note": "Kept beside a stray field.",
                "week": FIXTURE_WEEK,
            },
            headers=PAGE_HEADERS,
        )
        elsewhere = client.post(
            mark_at,
            data={"basis": basis, "expected_check_id": "check-nowhere", "note": ""},
            headers=PAGE_HEADERS,
        )
        blank = client.post(
            mark_at,
            data={"basis": "", "expected_check_id": "", "note": ""},
            headers=PAGE_HEADERS,
        )
        nowhere = client.post(
            "/parent/actions/checks/assignment-nowhere/mark",
            data={"basis": basis, "expected_check_id": "", "note": ""},
            headers=PAGE_HEADERS,
        )
        two_notes = client.post(
            mark_at,
            data={"basis": basis, "expected_check_id": head, "note": ["First.", "Second."]},
            headers=PAGE_HEADERS,
        )
        uploaded = client.post(
            mark_at,
            data={"basis": basis, "expected_check_id": head},
            files={"note": ("note.txt", b"words from a file", "text/plain")},
            headers=PAGE_HEADERS,
        )
        again_blank = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/again",
            data={"check_id": "", "basis": ""},
            headers=PAGE_HEADERS,
        )
        again_doubled = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/again",
            data={"check_id": ["a", "b"], "basis": ""},
            headers=PAGE_HEADERS,
        )
        chain = state_of(client).project_state.family_checks(ESSAY_ID)

    assert long.status_code == 422
    assert f'{CHECK_NOTE_TOO_LONG} <a href="#update-{ESSAY_ID}">Go to the row.</a>' in long.text
    long_row = row_for(long.text, ESSAY_ID)
    assert f'id="check-problem-{ESSAY_ID}">{CHECK_NOTE_TOO_LONG}</p>' in long_row
    assert (
        f'aria-describedby="check-problem-{ESSAY_ID} check-hint-{ESSAY_ID}" aria-invalid="true" '
        f"autofocus>{'x' * 501}</textarea>"
    ) in long_row
    assert doubled.status_code == 422
    assert BAD_CHECK_FORM in row_for(doubled.text, ESSAY_ID)
    doubled_row = row_for(doubled.text, ESSAY_ID)
    assert "Twice.</textarea>" in doubled_row
    assert (hidden(doubled_row, "basis"), hidden(doubled_row, "expected_check_id")) == (basis, head)
    assert "Kept beside a stray field.</textarea>" in row_for(stranger.text, ESSAY_ID)
    assert two_notes.status_code == 422
    assert BAD_CHECK_FORM in two_notes.text
    assert "First.</textarea>" in row_for(two_notes.text, ESSAY_ID)
    assert "Second." not in two_notes.text
    assert uploaded.status_code == 422
    assert BAD_CHECK_FORM in uploaded.text
    assert "words from a file" not in uploaded.text
    assert stranger.status_code == 422
    assert BAD_CHECK_FORM in stranger.text
    assert elsewhere.status_code == 422
    assert NOT_THIS in elsewhere.text
    assert blank.status_code == 422
    assert NOT_THIS in blank.text
    assert nowhere.status_code == 404
    assert NOT_ON_RECORD in nowhere.text
    assert again_blank.status_code == 422
    assert NOT_THIS in again_blank.text
    assert again_doubled.status_code == 422
    assert BAD_CHECK_FORM in again_doubled.text
    assert chain == []


@pytest.mark.parametrize(
    ("path", "sent"),
    [
        pytest.param("mark", {"basis": "b", "expected_check_id": ""}, id="no-note"),
        pytest.param("mark", {"basis": "b", "note": "Kept words."}, id="no-check-shown"),
        pytest.param("mark", {"expected_check_id": "", "note": "Kept words."}, id="no-basis"),
        pytest.param("again", {"basis": ""}, id="no-check-named"),
        pytest.param("again", {"check_id": "check-x"}, id="no-basis-shown"),
    ],
)
def test_a_check_form_with_a_field_left_out_writes_nothing(path: str, sent: dict[str, str]) -> None:
    """The page's forms send every field they have, blank or not; one that leaves a field
    out is not the page's form, whatever the fields it does send say, and the row with no
    check on it stays so."""
    with browser() as client:
        a_discrepancy(client)
        row = row_for(family_page(client), ESSAY_ID)
        fields = {
            name: hidden(row, "basis") if value == "b" else value for name, value in sent.items()
        }
        answer = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/{path}", data=fields, headers=PAGE_HEADERS
        )
        chain = state_of(client).project_state.family_checks(ESSAY_ID)

    assert answer.status_code == 422
    assert BAD_CHECK_FORM in row_for(answer.text, ESSAY_ID)
    assert ("Kept words.</textarea>" in row_for(answer.text, ESSAY_ID)) == ("note" in sent)
    assert chain == []


def test_a_check_whose_facts_moved_stays_on_its_row_and_can_be_reopened() -> None:
    """The school's email says something else after the check: the row is not worth checking,
    and it still says a parent marked it checked, on what day, with what note, and that no
    school channel reports it missing now, with Check again beside it. Her Not yet on top
    of that is said too. Her card says nothing of a check that does not stand. Reopened,
    the line goes and both events are in the record."""
    with browser() as client:
        a_discrepancy(client)
        store = state_of(client).project_state
        assert mark(client, ESSAY_ID, "Seen.").status_code == 303
        store.record_status_reports(
            ESSAY_ID, [school_said("turned_in", SourceChannel.EMAIL, PLAN_DATE)]
        )
        cleared = family_page(client)
        hers = card_for(client.get(HER_PAGE, headers=PAGE_HEADERS).text, ESSAY_ID)
        report(client, ESSAY_ID, "not_yet", "Found a page left.")
        both = family_page(client)
        reopened = check_again(client, ESSAY_ID)
        after = client.get(reopened.headers["location"], headers=PAGE_HEADERS).text
        chain = store.family_checks(ESSAY_ID)

    assert where(cleared, ESSAY_ID) == "Recent updates"
    cleared_row = row_for(cleared, ESSAY_ID)
    assert (
        "A parent marked this checked on August 19, and what it rests on differs now: no "
        "school channel reports it missing. The note with it: <q>Seen.</q></p>"
    ) in cleared_row
    assert f'aria-label="Check again: {ESSAY_TITLE}"' in cleared_row
    assert hidden(cleared_row, "check_id") == chain[0].check_id
    assert "The school reports it turned in." in cleared_row
    assert "Worth checking together" not in cleared
    assert "Checked recently" not in cleared
    assert "A parent marked this checked" not in hers
    assert (
        "differs now: her update is not Done, and no school channel reports it missing."
    ) in row_for(both, ESSAY_ID)
    assert reopened.status_code == 303
    after_row = row_for(after, ESSAY_ID)
    assert CHECK_REOPENED in after_row
    assert "A parent marked this checked" not in after_row
    assert "Check again" not in after_row
    assert [item.operation for item in chain] == ["checked", "reopened"]


def test_reads_by_name_take_any_number_of_names_as_one_bound_value(
    tmp_path: pathlib.Path,
) -> None:
    """Two thousand names, two of them on record: her events and the family's checks come
    back for those two, each statement written once with the names bound as one value."""
    store = practice_store(tmp_path / "blossom.sqlite3")
    try:
        store.record_status_reports(PRACTICE, [school_missing(date(2026, 9, 10))])
        done = store.report_status(
            PRACTICE, "done", None, expected_head=None, now=SAID_AT, today=SAID_ON
        )
        assert isinstance(done, Saved)
        basis = status_of(store, PRACTICE).check_basis
        assert basis is not None
        marked = store.mark_checked(
            PRACTICE,
            basis,
            None,
            expected_check=None,
            basis_now=lambda: status_of(store, PRACTICE).check_basis,
            now=SAID_AT,
            today=SAID_ON,
        )
        assert isinstance(marked, Checked)
        names = [f"assignment-{n}" for n in range(2000)] + [PRACTICE, PRACTICE_LOG]
        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)
        reports = store.student_report_chains(names)
        checks = store.family_check_chains(names)
        store._connection.set_trace_callback(None)
        odd = store.family_check_chains(["it's", 'a "name"', "x'); DROP TABLE family_checks; --"])
        still = store.family_checks(PRACTICE)
    finally:
        store.close()

    assert reports == {PRACTICE: [done.report]}
    assert checks == {PRACTICE: [marked.check]}
    assert len(statements) == 2
    assert all("json_each" in statement for statement in statements)
    assert odd == {}
    assert still == [marked.check]


def test_a_check_older_than_the_window_keeps_its_line_and_its_button_in_the_rows_group() -> None:
    """A check made fifteen household days ago stands, so its row is not worth checking; it
    is past the fold's window, so it is under the school's reports with the line that says
    it was checked and the way to check again."""
    with browser() as client:
        a_discrepancy(client)
        store = state_of(client).project_state
        assert mark(client, ESSAY_ID, "Seen.").status_code == 303
        store._connection.execute(
            "UPDATE family_checks SET checked_on = ?",
            ((PLAN_DATE - date.resolution * 15).isoformat(),),
        )
        store._connection.execute(
            "UPDATE student_reports SET reported_on = ?",
            ((PLAN_DATE - date.resolution * 20).isoformat(),),
        )
        store._connection.commit()
        family = family_page(client)
        reopened = check_again(client, ESSAY_ID)
        after = client.get(reopened.headers["location"], headers=PAGE_HEADERS).text

    assert where(family, ESSAY_ID) == "School reports"
    row = row_for(family, ESSAY_ID)
    assert (
        "A parent marked this checked with her on August 4. The note with it: <q>Seen.</q>" in row
    )
    assert f'aria-label="Check again: {ESSAY_TITLE}"' in row
    assert "Checked recently" not in family
    assert reopened.status_code == 303
    assert where(after, ESSAY_ID) == "Worth checking together"
    assert CHECK_REOPENED in row_for(after, ESSAY_ID)


def test_two_parents_with_different_notes_get_one_check_and_one_refusal_on_two_connections(
    tmp_path: pathlib.Path,
) -> None:
    """Two connections to one file, each with the unmarked row's basis and no check shown,
    each with its own words, let go together: one check is kept, the other is a conflict
    that names the check kept, whichever came first. The words of the one refused are its
    caller's to show; nothing of them is kept."""
    path = tmp_path / "blossom.sqlite3"
    first = practice_store(path)
    first.record_status_reports(PRACTICE, [school_missing(date(2026, 9, 10))])
    done = first.report_status(
        PRACTICE, "done", None, expected_head=None, now=SAID_AT, today=SAID_ON
    )
    assert isinstance(done, Saved)
    basis = status_of(first, PRACTICE).check_basis
    assert basis is not None
    second = ProjectStateStore.open(path, fixture_clock())
    together = threading.Barrier(2)
    results: dict[str, object] = {}

    def marking(store: ProjectStateStore, words: str) -> None:
        together.wait(5)
        results[words] = store.mark_checked(
            PRACTICE,
            basis,
            words,
            expected_check=None,
            basis_now=lambda: status_of(store, PRACTICE).check_basis,
            now=SAID_AT,
            today=SAID_ON,
        )

    threads = [
        threading.Thread(target=marking, args=(first, "One parent's words.")),
        threading.Thread(target=marking, args=(second, "The other parent's words.")),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
        chain = first.family_checks(PRACTICE)
    finally:
        first.close()
        second.close()

    kept = [result for result in results.values() if isinstance(result, Checked)]
    refused = [result for result in results.values() if isinstance(result, CheckConflict)]
    assert len(results) == 2
    assert (len(kept), len(refused)) == (1, 1)
    assert chain == [kept[0].check]
    assert refused[0].head == kept[0].check
    assert results[kept[0].check.note or ""] is kept[0]


def test_check_again_is_held_to_the_facts_its_page_showed() -> None:
    """The same email pasted again, a change to her note alone, and a statement with an
    earlier day arriving late leave a Check again form good. A missing the school had not
    reported, a Not yet, and a Done begun again since the page was made each refuse it,
    409, with nothing written. A row whose facts moved offers a form with a blank basis,
    which reopens the check that was made, once."""
    with browser() as client:
        a_discrepancy(client)
        store = state_of(client).project_state

        def press(row: str) -> Answer:
            return client.post(
                f"/parent/actions/checks/{ESSAY_ID}/again",
                data={"check_id": hidden(row, "check_id"), "basis": hidden(row, "basis")},
                headers=PAGE_HEADERS,
            )

        assert mark(client, ESSAY_ID, "Seen.").status_code == 303
        shown = row_for(family_page(client), ESSAY_ID)
        assert client.post("/parent/inbox/keep", data={"text": MISSING_EMAIL}).status_code == 303
        report(client, ESSAY_ID, "done", "Handed in Tuesday, both parts.")
        store.record_status_reports(
            ESSAY_ID, [school_said("missing", SourceChannel.EMAIL, date(2026, 8, 10))]
        )
        harmless = press(shown)

        assert mark(client, ESSAY_ID).status_code == 303
        shown = row_for(family_page(client), ESSAY_ID)
        store.record_status_reports(
            ESSAY_ID, [school_said("missing", SourceChannel.LMS, date(2026, 8, 18))]
        )
        after_the_portal = press(shown)
        after_the_portal_chain = len(store.family_checks(ESSAY_ID))

        assert mark(client, ESSAY_ID).status_code == 303
        shown = row_for(family_page(client), ESSAY_ID)
        report(client, ESSAY_ID, "not_yet", "Found a page left.")
        after_not_yet = press(shown)
        after_not_yet_chain = len(store.family_checks(ESSAY_ID))

        moved = row_for(family_page(client), ESSAY_ID)
        on_purpose = press(moved)
        consumed = press(moved)

        report(client, ESSAY_ID, "done")
        assert mark(client, ESSAY_ID).status_code == 303
        shown = row_for(family_page(client), ESSAY_ID)
        report(client, ESSAY_ID, "not_yet")
        report(client, ESSAY_ID, "done")
        after_a_new_done = press(shown)
        chain = store.family_checks(ESSAY_ID)

    assert harmless.status_code == 303
    assert after_the_portal.status_code == 409
    assert FACTS_CHANGED in row_for(after_the_portal.text, ESSAY_ID)
    assert after_the_portal_chain == 3
    assert after_not_yet.status_code == 409
    assert FACTS_CHANGED in row_for(after_not_yet.text, ESSAY_ID)
    assert after_not_yet_chain == 4
    assert hidden(moved, "basis") == ""
    assert "differs now: her update is not Done." in moved
    assert on_purpose.status_code == 303
    assert consumed.status_code == 409
    assert CHECK_MOVED_ON in row_for(consumed.text, ESSAY_ID)
    assert after_a_new_done.status_code == 409
    assert FACTS_CHANGED in row_for(after_a_new_done.text, ESSAY_ID)
    assert [item.operation for item in chain] == [
        "checked",
        "reopened",
        "checked",
        "checked",
        "reopened",
        "checked",
    ]


def test_a_check_the_file_refuses_keeps_the_note_even_when_the_page_cannot_be_read_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The insert is refused: the family page comes back, 500, with the note in its field
    and no word of a check. The page cannot be read back either: a plain page, 500, with
    the whole note, markup, line break, and a character outside the basic plane included,
    and no word of a check. The same for a reopening. With the faults gone the page reads
    the record as it is, and nothing was tried again."""
    words = "Kept <b>words</b>\r\nand a second line \U0001f33c"
    with browser() as client:
        a_discrepancy(client)
        store = state_of(client).project_state
        row = row_for(family_page(client), ESSAY_ID)
        fields = {
            "basis": hidden(row, "basis"),
            "expected_check_id": hidden(row, "expected_check_id"),
            "note": words,
        }
        store._connection.execute(
            "CREATE TRIGGER refuse_checks BEFORE INSERT ON family_checks "
            "BEGIN SELECT RAISE(ABORT, 'refused'); END"
        )
        store._connection.commit()
        refused = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/mark", data=fields, headers=PAGE_HEADERS
        )

        def unreadable(*_: object, **__: object) -> None:
            msg = "the record could not be read"
            raise RuntimeError(msg)

        monkeypatch.setattr(parent_routes, "assignment_updates", unreadable)
        unread = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/mark", data=fields, headers=PAGE_HEADERS
        )
        monkeypatch.undo()
        store._connection.execute("DROP TRIGGER refuse_checks")
        store._connection.commit()
        after = family_page(client)
        nothing = store.family_checks(ESSAY_ID)

        assert mark(client, ESSAY_ID, "Seen.").status_code == 303
        checked = row_for(family_page(client), ESSAY_ID)
        again = {"check_id": hidden(checked, "check_id"), "basis": hidden(checked, "basis")}
        store._connection.execute(
            "CREATE TRIGGER refuse_checks BEFORE INSERT ON family_checks "
            "BEGIN SELECT RAISE(ABORT, 'refused'); END"
        )
        store._connection.commit()
        not_reopened = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/again", data=again, headers=PAGE_HEADERS
        )
        monkeypatch.setattr(parent_routes, "assignment_updates", unreadable)
        not_reopened_unread = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/again", data=again, headers=PAGE_HEADERS
        )
        monkeypatch.undo()
        chain = store.family_checks(ESSAY_ID)

    assert refused.status_code == 500
    refused_row = row_for(refused.text, ESSAY_ID)
    assert CHECK_NOT_SAVED in refused_row
    assert f">{escape(words)}</textarea>" in refused_row
    assert RECORDED not in refused.text
    assert unread.status_code == 500
    assert "<h1>Family review</h1>" in unread.text
    assert CHECK_NOT_SAVED in unread.text
    assert f"readonly>{escape(words)}</textarea>" in unread.text
    assert '<a href="/parent">Return to Family review</a>' in unread.text
    assert RECORDED not in unread.text
    assert "Assignment updates" not in unread.text
    assert nothing == []
    assert where(after, ESSAY_ID) == "Worth checking together"
    assert not_reopened.status_code == 500
    assert CHECK_NOT_REOPENED in row_for(not_reopened.text, ESSAY_ID)
    assert not_reopened_unread.status_code == 500
    assert CHECK_NOT_REOPENED in not_reopened_unread.text
    assert "<textarea" not in not_reopened_unread.text
    assert CHECK_REOPENED not in not_reopened_unread.text
    assert [item.operation for item in chain] == ["checked"]


def test_each_note_field_names_its_assignment_for_a_reader_who_hears_the_page() -> None:
    """Two rows worth checking: each note field's label carries its own assignment and
    course, out of sight, so the two fields do not read the same; the hint and the button
    stay each row's own, and the note comes before its button."""
    log = "assignment-reading-log"
    with browser() as client:
        a_discrepancy(client)
        store = state_of(client).project_state
        store.record_status_reports(log, [school_said("missing", SourceChannel.EMAIL, PLAN_DATE)])
        report(client, log, "done")
        item = next(row for row in store.all_assignments() if row.assignment_id == log)
        family = family_page(client)

    essay_row, log_row = row_for(family, ESSAY_ID), row_for(family, log)
    assert (
        f'<label for="check-note-{log}">Note for her card (optional)<span '
        f'class="visually-hidden"> for {item.title} ({item.course})</span></label>'
    ) in log_row
    assert f"for {ESSAY_TITLE} (World History)</span></label>" in essay_row
    assert f'<textarea id="check-note-{log}" name="note"' in log_row
    assert f'aria-describedby="check-hint-{log}"' in log_row
    assert f'aria-label="Mark checked: {item.title}"' in log_row
    assert log_row.index("<textarea") < log_row.index("Mark checked</button>")
    assert family.count("Note for her card (optional)") == 2
