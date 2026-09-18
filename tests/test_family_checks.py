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
from blossom.routes.parent import ASSIGNMENTS_CHANGED as THEIR_ASSIGNMENTS_CHANGED
from blossom.routes.parent import (
    BAD_CHECK_FORM,
    CHECK_ALREADY,
    CHECK_FACTS_CHANGED,
    CHECK_MOVED_ON,
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
    PLAN_DATE,
    SAME_ORIGIN,
    accepting,
    fixture_clock,
    fixture_week_plan,
    scripted_graphs,
)
from tests.test_her_update import (
    ESSAY,
    ESSAY_TITLE,
    HERS,
    MISSING_EMAIL,
    PAGE,
    PAGE_HEADERS,
    THEIRS,
    WEEK,
    Answer,
    browser,
    card_for,
    hidden,
    report,
    school_said,
    signed_in_household,
    state_of,
)
from tests.test_student_reports import LOG, NOW, PRACTICE, TODAY, a_store, missing, status_of

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
        reported_at=NOW,
        reported_on=TODAY,
        previous_report_id=previous,
        undoes_report_id=undoes,
    )


def a_check(**over: object) -> FamilyCheck:
    fields: dict[str, object] = {
        "check_id": "check-1",
        "assignment_id": PRACTICE,
        "operation": "checked",
        "basis": f"{PRACTICE}|report-1|EMAIL:missing:2026-09-10",
        "checked_at": NOW,
        "checked_on": TODAY,
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
    store = a_store(tmp_path / "blossom.sqlite3")
    try:
        store.record_status_reports(PRACTICE, [missing(date(2026, 9, 10))])
        without_done = status_of(store, PRACTICE)
        saved = store.report_status(
            PRACTICE, "done", None, expected_head=None, now=NOW, today=TODAY
        )
        assert isinstance(saved, Saved)
        one = status_of(store, PRACTICE)
        store.record_status_reports(
            PRACTICE, [school_said("missing", SourceChannel.LMS, date(2026, 9, 12))]
        )
        two = status_of(store, PRACTICE)
        store.record_status_reports(PRACTICE, [missing(date(2026, 9, 1))])
        late = status_of(store, PRACTICE)
        store.record_status_reports(
            PRACTICE, [school_said("turned_in", SourceChannel.EMAIL, date(2026, 9, 14))]
        )
        cleared = status_of(store, PRACTICE)
        nothing = status_of(store, LOG)
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
    store = a_store(path)
    try:
        store.record_status_reports(PRACTICE, [missing(date(2026, 9, 10))])
        done = store.report_status(PRACTICE, "done", None, expected_head=None, now=NOW, today=TODAY)
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
            now=NOW,
            today=TODAY,
        )
        assert isinstance(marked, Checked)
        again = store.mark_checked(
            PRACTICE,
            basis,
            "Teacher has it.",
            expected_check=None,
            basis_now=basis_now,
            now=NOW,
            today=TODAY,
        )
        other_note = store.mark_checked(
            PRACTICE,
            basis,
            "Something else.",
            expected_check=marked.check.check_id,
            basis_now=basis_now,
            now=NOW,
            today=TODAY,
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
    assert isinstance(other_note, AlreadyChecked)
    assert again.check == other_note.check == marked.check
    assert chain == [marked.check]
    assert marked.check.note == "Teacher has it.\nSaid so Tuesday."
    assert (marked.check.operation, marked.check.basis) == ("checked", basis)
    assert (marked.check.checked_at, marked.check.checked_on) == (NOW, TODAY)
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
            now=NOW,
            today=TODAY,
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
    store = a_store(tmp_path / "blossom.sqlite3")
    try:
        store.record_status_reports(PRACTICE, [missing(date(2026, 9, 10))])
        done = store.report_status(PRACTICE, "done", None, expected_head=None, now=NOW, today=TODAY)
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
                now=NOW,
                today=TODAY,
            )

        not_yet = store.report_status(
            PRACTICE, "not_yet", None, expected_head=done.report.report_id, now=NOW, today=TODAY
        )
        assert isinstance(not_yet, Saved)
        gone = mark(first, None, None)
        done_again = store.report_status(
            PRACTICE, "done", None, expected_head=not_yet.report.report_id, now=NOW, today=TODAY
        )
        assert isinstance(done_again, Saved)
        renewed = basis_now()
        assert renewed is not None
        stale = mark(first, None, None)
        marked = mark(renewed, None, None)
        assert isinstance(marked, Checked)
        behind = mark(renewed, "Late note.", None)
        reopened = store.check_again(PRACTICE, marked.check.check_id, now=NOW, today=TODAY)
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
            now=NOW,
            today=TODAY,
        )
        assert isinstance(noted, Saved)
        after_the_note = status_of(store, PRACTICE).check
        store.record_status_reports(PRACTICE, [missing(date(2026, 9, 10))])
        after_the_same_paste = status_of(store, PRACTICE).check
        undone = store.undo_report(PRACTICE, noted.report.report_id, now=NOW, today=TODAY)
        assert isinstance(undone, Undone)
        after_the_undo = status_of(store, PRACTICE).check
        store.record_status_reports(
            PRACTICE, [school_said("missing", SourceChannel.LMS, date(2026, 9, 12))]
        )
        moved = status_of(store, PRACTICE)
        reopened_twice = store.check_again(PRACTICE, fresh.check.check_id, now=NOW, today=TODAY)
        once_more = store.check_again(PRACTICE, fresh.check.check_id, now=NOW, today=TODAY)
        with pytest.raises(UnknownCheck):
            mark(renewed, None, "check-nowhere")
        with pytest.raises(UnknownCheck):
            store.check_again(PRACTICE, "check-nowhere", now=NOW, today=TODAY)
        with pytest.raises(UnknownCheck):
            store.check_again(LOG, fresh.check.check_id, now=NOW, today=TODAY)
        with pytest.raises(UnknownAssignment):
            store.mark_checked(
                "assignment-nowhere",
                renewed,
                None,
                expected_check=None,
                basis_now=basis_now,
                now=NOW,
                today=TODAY,
            )
        with pytest.raises(NoteTooLong):
            mark(renewed, "x" * 501, None)
        chain = store.family_checks(PRACTICE)
        log_chain = store.family_checks(LOG)
    finally:
        store.close()

    assert isinstance(gone, CheckConflict)
    assert gone.head is None
    assert renewed != first
    assert basis_parts(renewed)[1] == done_again.report.report_id
    assert isinstance(stale, CheckConflict)
    assert isinstance(behind, AlreadyChecked)
    assert behind.check == marked.check
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
    report(client, ESSAY, "done", "Handed in Tuesday.")


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
        data={"check_id": hidden(row, "check_id")},
        headers=PAGE_HEADERS,
    )


def undo(client: TestClient, assignment_id: str) -> None:
    """Take back her latest update from her card."""
    page = client.get(PAGE, params={"week": WEEK, "show": assignment_id}, headers=PAGE_HEADERS).text
    head = hidden(card_for(page, assignment_id), "report_id")
    answer = client.post(
        f"/student/actions/assignments/{assignment_id}/undo-report",
        data={"report_id": head, "week": WEEK},
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
        row = row_for(before, ESSAY)
        marked = mark(client, ESSAY, "Teacher has it on paper.\r\nSaid so Tuesday.")
        after = client.get(marked.headers["location"], headers=PAGE_HEADERS).text
        hers = card_for(client.get(PAGE, headers=PAGE_HEADERS).text, ESSAY)
        chain = state_of(client).project_state.family_checks(ESSAY)

    assert "<h3>Worth checking together</h3>" in before
    assert f'<article class="draft needs-review" id="update-{ESSAY}" tabindex="-1">' in before
    assert f'action="/parent/actions/checks/{ESSAY}/mark"' in row
    assert HELPER in row
    assert f'<label for="check-note-{ESSAY}">Note for her card (optional)</label>' in row
    assert "Up to 500 characters. She reads this on her card." in row
    assert hidden(row, "expected_check_id") == ""
    assert hidden(row, "basis").startswith(f"{ESSAY}|report-")
    assert hidden(row, "basis").endswith("|EMAIL:missing:2026-08-19")
    assert f'aria-label="Mark checked: {ESSAY_TITLE}"' in row
    assert "maxlength" not in row
    assert "data-pending" not in row
    assert "<script" not in row
    assert marked.status_code == 303
    assert marked.headers["location"] == f"/parent?checked={ESSAY}#update-{ESSAY}"
    assert "<h3>Worth checking together</h3>" not in after
    assert '<details class="steps checked-recently" open>' in after
    assert "<summary>Checked recently (1)</summary>" in after
    checked = row_for(after, ESSAY)
    assert where(after, ESSAY) == "Checked recently"
    assert RECORDED in checked
    assert (
        "A parent marked this checked with her on August 19. The note with it: "
        "<q>Teacher has it on paper.\nSaid so Tuesday.</q>"
    ) in checked
    assert "She reported it done on August 19. She wrote: <q>Handed in Tuesday.</q>" in checked
    assert "The school reports it missing." in checked
    assert f'action="/parent/actions/checks/{ESSAY}/again"' in checked
    assert f'aria-label="Check again: {ESSAY_TITLE}"' in checked
    assert hidden(checked, "check_id") == chain[0].check_id
    assert f'action="/parent/actions/checks/{ESSAY}/mark"' not in after
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
        report(client, ESSAY, "done")
        hers = client.post(
            f"/parent/actions/checks/{ESSAY}/mark",
            data={"basis": "x", "expected_check_id": "", "note": ""},
            headers=PAGE_HEADERS,
        )
        hers_again = client.post(
            f"/parent/actions/checks/{ESSAY}/again",
            data={"check_id": "x"},
            headers=PAGE_HEADERS,
        )
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        marked = mark(client, ESSAY, "On paper.")
        chain = state_of(client).project_state.family_checks(ESSAY)
    with TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": THEIRS})
        family = family_page(client)
        as_parent = card_for(client.get(PAGE, headers=PAGE_HEADERS).text, ESSAY)
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": HERS})
        as_her = card_for(client.get(PAGE, headers=PAGE_HEADERS).text, ESSAY)

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
        first = row_for(family_page(client), ESSAY)
        store.record_status_reports(
            ESSAY, [school_said("missing", SourceChannel.LMS, date(2026, 8, 18))]
        )
        moved = client.post(
            f"/parent/actions/checks/{ESSAY}/mark",
            data={
                "basis": hidden(first, "basis"),
                "expected_check_id": hidden(first, "expected_check_id"),
                "note": "Typed before the portal spoke.",
            },
            headers=PAGE_HEADERS,
        )
        second = row_for(family_page(client), ESSAY)
        marked = mark(client, ESSAY, "Checked both.")
        from_before = client.post(
            f"/parent/actions/checks/{ESSAY}/mark",
            data={
                "basis": hidden(second, "basis"),
                "expected_check_id": hidden(second, "expected_check_id"),
                "note": "A second parent's words.",
            },
            headers=PAGE_HEADERS,
        )
        already = client.get(from_before.headers["location"], headers=PAGE_HEADERS).text
        checked = row_for(family_page(client), ESSAY)
        reopened = check_again(client, ESSAY)
        behind = client.post(
            f"/parent/actions/checks/{ESSAY}/mark",
            data={
                "basis": hidden(second, "basis"),
                "expected_check_id": hidden(second, "expected_check_id"),
                "note": "",
            },
            headers=PAGE_HEADERS,
        )
        twice = client.post(
            f"/parent/actions/checks/{ESSAY}/again",
            data={"check_id": hidden(checked, "check_id")},
            headers=PAGE_HEADERS,
        )
        third = row_for(family_page(client), ESSAY)
        report(client, ESSAY, "not_yet", "Found a page left.")
        gone = client.post(
            f"/parent/actions/checks/{ESSAY}/mark",
            data={
                "basis": hidden(third, "basis"),
                "expected_check_id": hidden(third, "expected_check_id"),
                "note": "Typed before her change.",
            },
            headers=PAGE_HEADERS,
        )
        chain = store.family_checks(ESSAY)

    assert moved.status_code == 409
    assert FACTS_CHANGED in moved.text
    assert f'<a href="#update-{ESSAY}">Go to the row.</a>' in moved.text
    moved_row = row_for(moved.text, ESSAY)
    assert f'id="check-problem-{ESSAY}">{FACTS_CHANGED}</p>' in moved_row
    assert "Typed before the portal spoke.</textarea>" in moved_row
    assert "school portal" in moved_row
    assert "school email" in moved_row
    assert hidden(moved_row, "basis").endswith("|EMAIL:missing:2026-08-19|LMS:missing:2026-08-18")
    assert marked.status_code == 303
    assert from_before.status_code == 303
    assert from_before.headers["location"] == f"/parent?checked_already={ESSAY}#update-{ESSAY}"
    assert CHECK_ALREADY in row_for(already, ESSAY)
    assert "<q>Checked both.</q>" in row_for(already, ESSAY)
    assert "A second parent's words." not in already
    assert reopened.status_code == 303
    assert reopened.headers["location"] == f"/parent?reopened={ESSAY}#update-{ESSAY}"
    assert behind.status_code == 409
    assert CHECK_MOVED_ON in row_for(behind.text, ESSAY)
    assert where(behind.text, ESSAY) == "Worth checking together"
    assert twice.status_code == 409
    assert CHECK_MOVED_ON in row_for(twice.text, ESSAY)
    assert gone.status_code == 409
    assert where(gone.text, ESSAY) == "Recent updates"
    gone_row = row_for(gone.text, ESSAY)
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
        assert mark(client, ESSAY, "Seen.").status_code == 303
        assert client.post("/parent/inbox/keep", data={"text": MISSING_EMAIL}).status_code == 303
        same_paste = family_page(client)
        report(client, ESSAY, "done", "Handed in Tuesday, both parts.")
        note_only = family_page(client)
        store.record_status_reports(
            ESSAY, [school_said("missing", SourceChannel.EMAIL, date(2026, 8, 10))]
        )
        late = family_page(client)
        report(client, ESSAY, "not_yet", "Found a page left.")
        out = family_page(client)
        undo(client, ESSAY)
        restored = family_page(client)
        hers_restored = card_for(client.get(PAGE, headers=PAGE_HEADERS).text, ESSAY)
        report(client, ESSAY, "not_yet")
        report(client, ESSAY, "done")
        renewed = family_page(client)
        hers_renewed = card_for(client.get(PAGE, headers=PAGE_HEADERS).text, ESSAY)
        assert mark(client, ESSAY).status_code == 303
        store.record_status_reports(
            ESSAY, [school_said("missing", SourceChannel.LMS, date(2026, 8, 18))]
        )
        newly = family_page(client)
        chain = store.family_checks(ESSAY)
        reports = store.student_reports(ESSAY)

    assert where(same_paste, ESSAY) == "Checked recently"
    assert where(note_only, ESSAY) == "Checked recently"
    assert "<q>Handed in Tuesday, both parts.</q>" in row_for(note_only, ESSAY)
    assert where(late, ESSAY) == "Checked recently"
    assert where(out, ESSAY) == "Recent updates"
    assert "A parent marked this checked" not in row_for(out, ESSAY)
    assert where(restored, ESSAY) == "Checked recently"
    assert "restored August 19" in row_for(restored, ESSAY)
    assert (
        "A parent marked this checked with you on August 19. The note with it: <q>Seen.</q>"
        in hers_restored
    )
    assert where(renewed, ESSAY) == "Worth checking together"
    renewed_row = row_for(renewed, ESSAY)
    assert (
        "A parent marked this checked on August 19, and what it rests on differs now: her "
        "Done is a new one.</p>"
    ) in renewed_row
    assert "A parent marked this checked with" not in hers_renewed
    assert where(newly, ESSAY) == "Worth checking together"
    newly_row = row_for(newly, ESSAY)
    assert (
        "A parent marked this checked on August 19, and what it rests on differs now: the "
        "school's report listed is not the one checked then. A report dated by the day it "
        "was pasted carries that day, which is not proof of a new warning from the school.</p>"
    ) in newly_row
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
        update={"blocks": [block for block in whole.blocks if block.assignment_id != ESSAY]}
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
                store.student_reports(ESSAY),
                store.status_reports_by_assignment(),
                planning_digest(week),
                None if latest is None else (latest.draft_id, latest.plan_assignment_ids),
                [item.assignment_id for item in week.active()],
            )

        before = snapshot()
        assert mark(client, ESSAY, "Seen.").status_code == 303
        assert check_again(client, ESSAY).status_code == 303
        assert mark(client, ESSAY, "Seen twice.").status_code == 303
        after = snapshot()
        hers = client.get(PAGE, headers=PAGE_HEADERS).text
        family = family_page(client)
        chain = store.family_checks(ESSAY)

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
        row = row_for(family_page(client), ESSAY)
        basis, head = hidden(row, "basis"), hidden(row, "expected_check_id")
        mark_at = f"/parent/actions/checks/{ESSAY}/mark"
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
            data={"basis": basis, "expected_check_id": head, "note": "", "week": WEEK},
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
        again_blank = client.post(
            f"/parent/actions/checks/{ESSAY}/again", data={"check_id": ""}, headers=PAGE_HEADERS
        )
        again_doubled = client.post(
            f"/parent/actions/checks/{ESSAY}/again",
            data={"check_id": ["a", "b"]},
            headers=PAGE_HEADERS,
        )
        chain = state_of(client).project_state.family_checks(ESSAY)

    assert long.status_code == 422
    assert f'{CHECK_NOTE_TOO_LONG} <a href="#update-{ESSAY}">Go to the row.</a>' in long.text
    long_row = row_for(long.text, ESSAY)
    assert f'id="check-problem-{ESSAY}">{CHECK_NOTE_TOO_LONG}</p>' in long_row
    assert (
        f'aria-describedby="check-problem-{ESSAY} check-hint-{ESSAY}" aria-invalid="true" '
        f"autofocus>{'x' * 501}</textarea>"
    ) in long_row
    assert doubled.status_code == 422
    assert BAD_CHECK_FORM in row_for(doubled.text, ESSAY)
    assert "Twice." not in doubled.text
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


def test_a_check_older_than_the_window_keeps_its_line_and_its_button_in_the_rows_group() -> None:
    """A check made fifteen household days ago stands, so its row is not worth checking; it
    is past the fold's window, so it is under the school's reports with the line that says
    it was checked and the way to check again."""
    with browser() as client:
        a_discrepancy(client)
        store = state_of(client).project_state
        assert mark(client, ESSAY, "Seen.").status_code == 303
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
        reopened = check_again(client, ESSAY)
        after = client.get(reopened.headers["location"], headers=PAGE_HEADERS).text

    assert where(family, ESSAY) == "School reports"
    row = row_for(family, ESSAY)
    assert (
        "A parent marked this checked with her on August 4. The note with it: <q>Seen.</q>" in row
    )
    assert f'aria-label="Check again: {ESSAY_TITLE}"' in row
    assert "Checked recently" not in family
    assert reopened.status_code == 303
    assert where(after, ESSAY) == "Worth checking together"
    assert CHECK_REOPENED in row_for(after, ESSAY)
