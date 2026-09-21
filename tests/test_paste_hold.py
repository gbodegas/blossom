"""A school paste that would touch homework made from a note is held, whole.

Until the school's version of such homework can be reviewed as the same work or
as separate work, a paste with a row of the same class and title writes
nothing: not that row, and not the rows beside it. The rows in the way are
named, the pasted text is kept, and a paste that touches no such homework goes
on as it always has. The comparison is made inside the write's own transaction,
so a note added to homework between the review and the save is met too.
"""

import html
import pathlib
import re
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from blossom.captures import (
    PARENT,
    STUDENT,
    CaptureCreated,
    CaptureDetails,
    CapturePromoted,
    candidate_basis,
    derived_assignment_id,
    new_capture_id,
)
from blossom.intake import Held, Kept, Reading, by_hand, keep
from blossom.reconciliation import SourceChannel
from blossom.stores.project_state import AssignmentKind, ProjectStateStore
from tests.support import (
    PAGE_HEADERS,
    PRACTICE,
    Answer,
    browser,
    fixture_clock,
    practice_store,
    state_of,
)

AT = datetime(2026, 9, 14, 21, 0, tzinfo=UTC)
MONDAY = date(2026, 9, 14)


def homework_from_a_note(
    store: ProjectStateStore,
    *,
    by: str = STUDENT,
    channel: SourceChannel = SourceChannel.STUDENT_REPORT,
    course: str = "Geometry",
    title: str = "Questions 4-8",
) -> str:
    name = new_capture_id()
    made = store.create_capture(
        name,
        "Geometry questions 4-8, heard from a classmate",
        None,
        None,
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=AT,
        today=MONDAY,
    )
    assert isinstance(made, CaptureCreated)
    given = CaptureDetails(course=course, title=title, kind="HOMEWORK")
    done = store.promote_capture(
        name,
        given,
        expected_revision=1,
        basis=candidate_basis(store.promotion_candidates(given)),
        choice="new",
        authored_by=by,  # type: ignore[arg-type]
        channel=channel,
        now=AT,
        today=MONDAY,
    )
    assert isinstance(done, CapturePromoted)
    return done.assignment_id


def entry(course: str, title: str, due: date | None = None) -> Reading:
    return by_hand(course, title, due, None, AssignmentKind.HOMEWORK, now=AT)


def rows(store: ProjectStateStore) -> tuple[list[object], list[object], list[object]]:
    connection = store._connection
    return (
        connection.execute("SELECT * FROM assignments ORDER BY assignment_id").fetchall(),
        connection.execute("SELECT * FROM date_claims ORDER BY rowid").fetchall(),
        connection.execute("SELECT * FROM status_reports ORDER BY rowid").fetchall(),
    )


@pytest.mark.parametrize(
    ("by", "channel"),
    [(STUDENT, SourceChannel.STUDENT_REPORT), (PARENT, SourceChannel.PARENT_ENTRY)],
)
def test_a_paste_with_a_row_about_homework_made_from_a_note_writes_none_of_its_rows(
    tmp_path: pathlib.Path, by: str, channel: SourceChannel
) -> None:
    """Whoever added the note to homework: the mark on the record is not what is asked,
    since a parent's press marks it a family entry and it is homework from a note all the same."""
    store = practice_store(tmp_path / "record.sqlite3")
    made = homework_from_a_note(store, by=by, channel=channel)
    before = rows(store)

    answer = keep(
        (
            entry("Geometry", "Questions   4-8", date(2026, 9, 18)),
            entry("History", "A worksheet no note is about", date(2026, 9, 18)),
        ),
        store,
    )

    assert isinstance(answer, Held)
    assert [(item.course, item.title) for item in answer.blocking] == [
        ("Geometry", "Questions 4-8")
    ]
    assert answer.assignments == (made,)
    assert rows(store) == before
    assert not store._connection.in_transaction


def test_a_paste_that_touches_no_such_homework_goes_on_as_it_did(tmp_path: pathlib.Path) -> None:
    """Homework made from a note is on record, and so is a Done of hers on school homework,
    which makes that homework no note's. Neither is in this paste's way."""
    store = practice_store(tmp_path / "record.sqlite3")
    homework_from_a_note(store)
    store.report_status(PRACTICE, "done", None, expected_head=None, now=AT, today=MONDAY)
    practice = next(item for item in store.all_assignments() if item.assignment_id == PRACTICE)

    answer = keep(
        (
            entry("History", "A worksheet no note is about", date(2026, 9, 18)),
            entry(practice.course, practice.title, practice.due_date),
            entry("geometry", "Questions 4-8", date(2026, 9, 18)),
        ),
        store,
    )

    assert isinstance(answer, Kept)
    assert answer.added == 2


def test_a_note_added_to_homework_after_the_review_is_met_inside_the_save(
    tmp_path: pathlib.Path,
) -> None:
    """Two connections. The review found nothing in the way; the note became homework
    through the other connection; the save reads that inside its own transaction."""
    path = tmp_path / "record.sqlite3"
    first = practice_store(path)
    second = ProjectStateStore.open(path, fixture_clock())
    batch = (entry("Geometry", "Questions 4-8", date(2026, 9, 18)),)
    assert first.held_by_notes([("Geometry", "Questions 4-8")]) == {}
    made = homework_from_a_note(second)
    before = rows(first)

    answer = keep(batch, first)

    assert isinstance(answer, Held)
    assert answer.assignments == (made,)
    assert made == derived_assignment_id(made_from(second, made))
    assert rows(first) == before


def made_from(store: ProjectStateStore, assignment_id: str) -> str:
    row = store._connection.execute(
        "SELECT capture_id FROM homework_captures WHERE assignment_id = ?", (assignment_id,)
    ).fetchone()
    return str(row[0])


def paste_entry(client: TestClient, course: str, title: str) -> tuple[dict[str, str], Answer]:
    draft = {"course": course, "title": title, "assigned_on": "", "due_date": "2026-08-21"}
    draft.update({"kind": "HOMEWORK", "note": ""})
    return draft, client.post("/parent/inbox/enter", data=draft, headers=PAGE_HEADERS)


def test_the_review_says_which_rows_are_in_the_way_keeps_the_entry_and_saves_nothing() -> None:
    with browser() as client:
        store = state_of(client).project_state
        homework_from_a_note(store, title="Questions <b>4-8</b>")
        before = rows(store)
        draft, review = paste_entry(client, "Geometry", "Questions <b>4-8</b>")
        found = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', review.text)
        carried = {name: html.unescape(value) for name, value in found}
        saved = client.post("/parent/inbox/keep", data=carried, headers=PAGE_HEADERS)
        after = rows(store)

    for answer in (review, saved):
        assert "homework she added from a note" in answer.text
        assert "Questions &lt;b&gt;4-8&lt;/b&gt;" in answer.text
        assert "<b>4-8</b>" not in answer.text
        assert "Nothing was saved" in answer.text or "Nothing will be saved" in answer.text
    assert review.status_code == 200
    assert saved.status_code == 409
    assert 'formaction="/parent/inbox/edit"' in saved.text
    assert after == before
