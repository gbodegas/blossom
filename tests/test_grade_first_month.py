# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A school year's first month, corrected by a parent, and the due dates read under it.

The month is the year's own state, compared and set in one write: a page applies only while the
month on record is the one it showed, and a page sent again finds its month there. Nothing stored
or compared depends on the month, so a correction leaves every revision, open review and preview
of the year as it was, and a due date resolves under the month on record whenever it is read.
"""

import dataclasses
import pathlib
from datetime import date

import pytest

from blossom.grades.dates import due_date_of
from blossom.grades.draft import GradeReportDraft, Presence, capture_key
from blossom.grades.identity import name_form_key
from blossom.grades.projection import MadeCurrent
from blossom.grades.review import GradeReportSaved
from blossom.grades.text_reader import read_grade_report
from blossom.stores.gradebook import (
    FirstMonthChanged,
    FirstMonthCorrected,
    FirstMonthOutcome,
    FirstMonthStood,
    YearNotOnRecord,
)
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    FIXTURES,
    capture_class,
    closed_world,
    confirm_current,
    current_preview,
    fixture_clock,
    grade_answers,
    save_grade,
)

REPORT = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")
KEY = name_form_key(b"5" * 64)
YEAR = "2026-2027"
SEVEN = "| Cell Diagram             | 7.0 "


def draft_of(text: str) -> GradeReportDraft:
    reading = read_grade_report(text)
    assert reading.draft is not None, reading.not_read
    return reading.draft


WREN = draft_of(REPORT)
EIGHT = draft_of(REPORT.replace(SEVEN, SEVEN.replace("7.0", "8.0")))
"""A newer capture of the first term: Cell Diagram changed."""
SECOND_TERM = draft_of(REPORT.replace("**T1**", "**T2**"))
SECOND_TERM_EIGHT = draft_of(
    REPORT.replace("**T1**", "**T2**").replace(SEVEN, SEVEN.replace("7.0", "8.0"))
)
"""A newer capture of the second term, which a page reviews while the month is corrected."""


def opened(tmp_path: pathlib.Path) -> tuple[ProjectStateStore, pathlib.Path]:
    path = tmp_path / "blossom.sqlite3"
    return ProjectStateStore.open(path, fixture_clock()), path


def year_saved(store: ProjectStateStore, month: int | None = 8) -> None:
    """Wren's report saved, its year's first month confirmed as ``month``, or left unconfirmed."""
    review = store.review_grade_report(WREN, capture_key(WREN), key=KEY)
    answers = grade_answers(review, month=month)
    outcome = save_grade(store, WREN, key=KEY, review=review, answers=answers)
    assert isinstance(outcome, GradeReportSaved), outcome


def corrected(store: ProjectStateStore, shown: int | None, month: int) -> FirstMonthOutcome:
    return store.correct_first_month(YEAR, shown=shown, month=month, role="parent")


def test_a_first_month_correction_changes_only_the_year(tmp_path: pathlib.Path) -> None:
    store, path = opened(tmp_path)
    year_saved(store)
    before = closed_world([path], leaving_out=())

    outcome = corrected(store, 8, 9)
    after = closed_world([path], leaving_out=())
    row = store._connection.execute(
        "SELECT first_month, first_month_by FROM grade_years WHERE label = ?", (YEAR,)
    ).fetchone()
    store.close()

    assert outcome == FirstMonthCorrected(YEAR, 9)
    assert after.keys() == before.keys()
    assert [name for name in before if after[name] != before[name]] == [
        f"{path.name} rows of grade_years"
    ]
    assert row == (9, "parent")


def test_a_stale_first_month_correction_returns_the_month_on_record(
    tmp_path: pathlib.Path,
) -> None:
    store, _ = opened(tmp_path)
    year_saved(store)
    first = corrected(store, 8, 9)
    changes = store._connection.total_changes

    stale = corrected(store, 8, 10)
    unchanged = store._connection.total_changes == changes
    month = store.first_month_of(YEAR)
    store.close()

    assert first == FirstMonthCorrected(YEAR, 9)
    assert stale == FirstMonthChanged(YEAR, 9)
    assert unchanged
    assert month == 9


def test_a_first_month_changed_and_changed_back_accepts_the_first_page(
    tmp_path: pathlib.Path,
) -> None:
    """A page showing August, held open while two others change the month and change it back,
    finds the month it showed, which is the year's whole state, and applies."""
    store, _ = opened(tmp_path)
    year_saved(store)

    away = corrected(store, 8, 9)
    back = corrected(store, 9, 8)
    held_open = corrected(store, 8, 10)
    month = store.first_month_of(YEAR)
    store.close()

    assert (away, back) == (FirstMonthCorrected(YEAR, 9), FirstMonthCorrected(YEAR, 8))
    assert held_open == FirstMonthCorrected(YEAR, 10)
    assert month == 10


def test_a_month_left_unconfirmed_is_confirmed_once_when_two_pages_race(
    tmp_path: pathlib.Path,
) -> None:
    store, _ = opened(tmp_path)
    year_saved(store, month=None)
    unconfirmed = store.first_month_of(YEAR)

    first = corrected(store, None, 9)
    changes = store._connection.total_changes
    second = corrected(store, None, 10)
    resent = corrected(store, None, 9)
    unchanged = store._connection.total_changes == changes
    row = store._connection.execute(
        "SELECT first_month, first_month_by FROM grade_years WHERE label = ?", (YEAR,)
    ).fetchone()
    store.close()

    assert unconfirmed is None
    assert first == FirstMonthCorrected(YEAR, 9)
    assert second == FirstMonthChanged(YEAR, 9)
    assert resent == FirstMonthStood(YEAR, 9)
    assert unchanged
    assert row == (9, "parent")


def test_a_resent_first_month_correction_stands_and_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    store, path = opened(tmp_path)
    year_saved(store)
    first = store.correct_first_month(YEAR, shown=8, month=9, role="household")
    whole = closed_world([path], leaving_out=())
    changes = store._connection.total_changes

    again = [corrected(store, 8, 9) for _ in range(2)]
    unchanged = store._connection.total_changes == changes
    store.close()

    assert first == FirstMonthCorrected(YEAR, 9)
    assert again == [FirstMonthStood(YEAR, 9)] * 2
    assert unchanged
    assert closed_world([path], leaving_out=()) == whole


def test_a_first_month_for_a_year_not_on_record_or_out_of_range_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    store, _ = opened(tmp_path)
    year_saved(store)
    changes = store._connection.total_changes

    elsewhere = store.correct_first_month("2025-2026", shown=None, month=9, role="parent")
    for month in (0, 13):
        with pytest.raises(ValueError, match="1 to 12"):
            corrected(store, 8, month)
    with pytest.raises(ValueError, match="only a parent"):
        store.correct_first_month(YEAR, shown=8, month=9, role="student")  # type: ignore[arg-type]
    unchanged = store._connection.total_changes == changes
    on_record = store.first_month_of(YEAR)
    store.close()

    assert elsewhere == YearNotOnRecord("2025-2026")
    assert unchanged
    assert on_record == 8


def test_a_first_month_correction_leaves_every_open_review_and_preview_of_the_year_valid(
    tmp_path: pathlib.Path,
) -> None:
    """A review open in one term and a preview open in another read the same after the month
    changes, down to the scope held and every revision, and both still go through. The month is
    in nothing they compare."""
    store, _ = opened(tmp_path)
    year_saved(store)
    for draft in (EIGHT, SECOND_TERM):
        assert isinstance(save_grade(store, draft, key=KEY), GradeReportSaved)
    class_id = capture_class(store, WREN)
    student_id = store.student_id()

    def as_held() -> tuple[object, ...]:
        return (
            store._scope_held(student_id, class_id, "T1"),
            store._scope_held(student_id, class_id, "T2"),
            store._connection.execute(
                "SELECT class_id, term_label, revision FROM grade_scope_revisions "
                "ORDER BY class_id, term_label"
            ).fetchall(),
        )

    review = store.review_grade_report(SECOND_TERM_EIGHT, capture_key(SECOND_TERM_EIGHT), key=KEY)
    preview = current_preview(store, WREN)
    held = as_held()

    outcome = corrected(store, 8, 1)
    review_now = store.review_grade_report(
        SECOND_TERM_EIGHT, capture_key(SECOND_TERM_EIGHT), key=KEY
    )
    preview_now = current_preview(store, WREN)
    held_now = as_held()
    made = confirm_current(store, WREN, preview)
    saved = save_grade(store, SECOND_TERM_EIGHT, key=KEY, review=review)
    store.close()

    assert outcome == FirstMonthCorrected(YEAR, 1)
    assert dataclasses.replace(review_now, acceptance_id=review.acceptance_id) == review
    assert dataclasses.replace(preview_now, action_id=preview.action_id) == preview
    assert held_now == held
    assert isinstance(made, MadeCurrent)
    assert isinstance(saved, GradeReportSaved)
    assert saved.updated == 1


RESOLVED = [
    ((Presence.REPORTED, "08/01"), YEAR, 8, date(2026, 8, 1)),
    ((Presence.REPORTED, "12/31"), YEAR, 8, date(2026, 12, 31)),
    ((Presence.REPORTED, "01/04"), YEAR, 8, date(2027, 1, 4)),
    ((Presence.REPORTED, "07/31"), YEAR, 8, date(2027, 7, 31)),
    ((Presence.REPORTED, "02/29"), "2027-2028", 8, date(2028, 2, 29)),
    ((Presence.REPORTED, "09/26"), YEAR, 1, date(2026, 9, 26)),
]
"""A reported MM/DD in the label's first year from the first month on, else in its second."""
UNRESOLVED = [
    ((Presence.REPORTED, "09/26"), YEAR, None),
    ((Presence.BLANK, ""), YEAR, 8),
    ((Presence.UNREADABLE, "9/26"), YEAR, 8),
    ((Presence.NOT_CAPTURED, ""), YEAR, 8),
    ((Presence.REPORTED, "Sept 26"), YEAR, 8),
    ((Presence.REPORTED, "09/26"), "2026-2028", 8),
    ((Presence.REPORTED, "09/26"), "2027-2026", 8),
    ((Presence.REPORTED, "09/26"), " 2026-2027", 8),
    ((Presence.REPORTED, "09/26"), "26-27", 8),
    ((Presence.REPORTED, "02/29"), YEAR, 8),
    ((Presence.REPORTED, "04/31"), YEAR, 8),
    ((Presence.REPORTED, "09/26"), YEAR, 0),
    ((Presence.REPORTED, "09/26"), YEAR, 13),
]
"""No month confirmed, another presence, text not MM/DD, another label, or a day the year lacks:
each stays as written."""


def test_a_due_date_resolves_under_the_first_month_on_read(tmp_path: pathlib.Path) -> None:
    store, _ = opened(tmp_path)
    year_saved(store)
    due = (Presence.REPORTED, "09/26")
    confirmed = due_date_of(due, YEAR, store.first_month_of(YEAR))
    corrected(store, 8, 10)
    after_correction = due_date_of(due, YEAR, store.first_month_of(YEAR))
    unknown_year = store.first_month_of("2025-2026")
    store.close()

    assert confirmed == date(2026, 9, 26)
    assert after_correction == date(2027, 9, 26)
    assert unknown_year is None
    assert [due_date_of(cell, year, month) for cell, year, month, _ in RESOLVED] == [
        expected for *_, expected in RESOLVED
    ]
    assert [due_date_of(cell, year, month) for cell, year, month in UNRESOLVED] == [None] * len(
        UNRESOLVED
    )
