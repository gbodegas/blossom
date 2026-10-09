# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A parent's assertion on a saved grade value, and its withdrawal.

An assertion is added beside the reading it names, never in place of it: the observation stays as
read, the school value stays current, and the assertion is kept apart, on the original reading
and every copy of it. It is never compared, matched or shown in the import review, and a
withdrawal is one more record, so the history stays until the class and term are deleted.
"""

import dataclasses
import pathlib
from datetime import date

import pytest

from blossom.grades.draft import GradeNumber, GradeReportDraft, GradeValue, Presence, capture_key
from blossom.grades.identity import name_form_key
from blossom.grades.projection import MadeCurrent, PreviewRevised, SourceOf, preview_of, project
from blossom.grades.review import (
    TERM_KEY,
    GradeReportSaved,
    ItemStatus,
    ReturnReason,
    ReviewReturned,
)
from blossom.grades.text_reader import read_grade_report
from blossom.noticing import canonical_active_input, planning_digest, read_everything, week_from
from blossom.stores.gradebook import (
    ClassTermDeleted,
    CorrectionPage,
    CorrectionRecorded,
    CorrectionReturned,
    DeletePreview,
    NothingToCorrect,
    ObservationNotSaved,
    ValueCorrected,
)
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    FIXTURES,
    as_stored,
    capture_class,
    confirm_current,
    correction_page,
    current_preview,
    fixture_clock,
    grade_answers,
    household_client,
    practice_store,
    save_grade,
    state_of,
    without_columns,
)

REPORT = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")
KEY = name_form_key(b"5" * 64)
SEED = "| Seed Germination Log | 18.0 "
CELL = "| Cell Diagram             | 7.0 "
MONDAY = date(2026, 9, 14)


def draft_of(text: str) -> GradeReportDraft:
    draft = read_grade_report(text).draft
    assert draft is not None
    return draft


WREN = draft_of(REPORT)
NINETEEN = draft_of(REPORT.replace(SEED, "| Seed Germination Log | 19.0 "))
"""Another capture: Seed changed to 19.0."""
EIGHT = draft_of(REPORT.replace(CELL, "| Cell Diagram             | 8.0 "))
"""Another capture: Cell Diagram changed to 8.0."""
SEVENTEEN = draft_of(REPORT.replace(SEED, "| Seed Germination Log | 17.0 "))
"""Another capture: Seed changed to 17.0."""
OBSERVATIONS = (
    "grade_reports",
    "grade_term_observations",
    "grade_category_observations",
    "grade_result_observations",
)


def reported(text: str) -> GradeValue:
    return GradeValue(text=text, presence=Presence.REPORTED)


def saved(path: pathlib.Path) -> tuple[ProjectStateStore, str]:
    store = ProjectStateStore.open(path / "blossom.sqlite3", fixture_clock())
    assert isinstance(save_grade(store, WREN, key=KEY), GradeReportSaved)
    return store, capture_class(store, WREN)


def result_named(store: ProjectStateStore, class_id: str, title: str) -> str:
    (found,) = (
        result
        for result, value in store.current_values(class_id, "T1").results.items()
        if value.cells["assignment"][1] == title
    )
    return found


def assert_value(
    store: ProjectStateStore,
    class_id: str,
    page: CorrectionPage,
    field: str,
    value: GradeValue | None,
    *,
    role: str = "parent",
) -> object:
    return store.correct_value(
        class_id,
        "T1",
        page=page,
        field=field,
        how="parent_assertion",
        value=value,
        role=role,  # type: ignore[arg-type]
    )


def stored(store: ProjectStateStore, *tables: str) -> list[list[tuple[object, ...]]]:
    return [as_stored(store, table) for table in tables]


def test_an_assertion_keeps_the_reading_and_the_school_value(tmp_path: pathlib.Path) -> None:
    store, class_id = saved(tmp_path)
    cell = result_named(store, class_id, "Cell Diagram")
    before = stored(store, *OBSERVATIONS)
    page = correction_page(store, class_id, "result", cell)

    outcome = assert_value(store, class_id, page, "points", reported("9.0"))
    current = store.current_values(class_id, "T1").results[cell]
    history = store.correction_history(class_id, "T1", "result", cell)
    after = stored(store, *OBSERVATIONS)
    store.close()

    assert outcome == ValueCorrected(page.correction_id, "points", "parent_assertion", "9.0")
    assert after == before
    assert current.cells["points"] == (Presence.REPORTED, "7.0")
    assert current.asserted == {"points": (Presence.REPORTED, "9.0")}
    assert [
        (one.field, one.read, one.value, one.how, one.by, one.entered_on, one.report_id)
        for one in history
    ] == [
        (
            "points",
            (Presence.REPORTED, "7.0"),
            "9.0",
            "parent_assertion",
            "parent",
            page.report_id,
            page.report_id,
        )
    ]


def earlier_then_copied(store: ProjectStateStore) -> tuple[str, str]:
    """``NINETEEN`` saved as earlier, then made current by the class-details action: its report
    and the copy."""
    review = store.review_grade_report(NINETEEN, capture_key(NINETEEN), key=KEY)
    answers = dataclasses.replace(grade_answers(review), use="earlier")
    outcome = save_grade(store, NINETEEN, key=KEY, review=review, answers=answers)
    assert isinstance(outcome, GradeReportSaved), outcome
    assert outcome.report_id is not None
    made = confirm_current(store, NINETEEN)
    assert isinstance(made, MadeCurrent), made
    return outcome.report_id, made.report_id


def test_an_assertion_entered_through_a_copy_is_kept_on_the_original_and_its_copies(
    tmp_path: pathlib.Path,
) -> None:
    """Entered on the copy C of Seed's reading A, the assertion is stored on A, shows on A and C,
    not on an independent newer reading, and on a later copy D of C."""
    store, class_id = saved(tmp_path)
    original, copy = earlier_then_copied(store)
    seed = result_named(store, class_id, "Seed Germination Log")
    page = correction_page(store, class_id, "result", seed)
    assert page.report_id == copy

    outcome = assert_value(store, class_id, page, "points", reported("20.0"))
    replaced = save_grade(store, SEVENTEEN, key=KEY)
    assert isinstance(replaced, GradeReportSaved), replaced
    newer = store.current_values(class_id, "T1").results[seed]
    later = confirm_current(store, NINETEEN)
    assert isinstance(later, MadeCurrent), later
    shown = store.current_values(class_id, "T1").results[seed]
    held = store._scope_held(store.student_id(), class_id, "T1")
    (record,) = store.correction_history(class_id, "T1", "result", seed)
    store.close()

    asserted = {"points": (Presence.REPORTED, "20.0")}
    assert isinstance(outcome, ValueCorrected)
    assert (record.report_id, record.entered_on) == (original, copy)
    assert (newer.report_id, newer.asserted) == (replaced.report_id, {})
    assert (later.source, shown.report_id, shown.asserted) == (copy, later.report_id, asserted)
    for report_id in (original, copy, later.report_id):
        assert held.marks["result", seed, report_id] == asserted
    assert ("result", seed, replaced.report_id) not in held.marks


def test_an_assertion_stays_in_history_when_an_independent_newer_reading_supplies_the_value(
    tmp_path: pathlib.Path,
) -> None:
    store, class_id = saved(tmp_path)
    cell = result_named(store, class_id, "Cell Diagram")
    page = correction_page(store, class_id, "result", cell)
    assert isinstance(
        assert_value(store, class_id, page, "points", reported("9.0")), ValueCorrected
    )

    assert isinstance(save_grade(store, EIGHT, key=KEY), GradeReportSaved)
    current = store.current_values(class_id, "T1").results[cell]
    history = store.correction_history(class_id, "T1", "result", cell)
    store.close()

    assert current.cells["points"] == (Presence.REPORTED, "8.0")
    assert current.asserted == {}
    assert [(one.report_id, one.value) for one in history] == [(page.report_id, "9.0")]


def test_a_school_value_equal_to_an_earlier_parent_assertion_is_recorded_as_school_reported(
    tmp_path: pathlib.Path,
) -> None:
    store, class_id = saved(tmp_path)
    cell = result_named(store, class_id, "Cell Diagram")
    page = correction_page(store, class_id, "result", cell)
    assert isinstance(
        assert_value(store, class_id, page, "points", reported("8.0")), ValueCorrected
    )

    review = store.review_grade_report(EIGHT, capture_key(EIGHT), key=KEY)
    (item,) = (one for one in review.rows if one.result_id == cell)
    outcome = save_grade(store, EIGHT, key=KEY, review=review)
    current = store.current_values(class_id, "T1").results[cell]
    store.close()

    assert item.status is ItemStatus.CHANGED
    assert item.current is not None
    assert item.current.cells["points"] == (Presence.REPORTED, "7.0")
    assert isinstance(outcome, GradeReportSaved)
    assert (current.report_id, current.cells["points"]) == (
        outcome.report_id,
        (Presence.REPORTED, "8.0"),
    )
    assert current.asserted == {}


def test_an_assertion_is_never_compared_or_shown_as_school_reported(
    tmp_path: pathlib.Path,
) -> None:
    store, class_id = saved(tmp_path)
    seed = result_named(store, class_id, "Seed Germination Log")
    page = correction_page(store, class_id, "result", seed)
    assert isinstance(
        assert_value(store, class_id, page, "points", reported("19.0")), ValueCorrected
    )

    held = store._scope_held(store.student_id(), class_id, "T1")
    review = store.review_grade_report(NINETEEN, capture_key(NINETEEN), key=KEY)
    (item,) = (one for one in review.rows if one.result_id == seed)
    store.close()

    unmarked = dataclasses.replace(held, marks={})
    record, plain = project(held), project(unmarked)
    source = SourceOf("s", class_id, "T1", 1, page.report_id, (), None)
    assert record.once_current == plain.once_current
    assert record.current.results[seed].cells == plain.current.results[seed].cells
    assert record.current.results[seed].asserted == {"points": (Presence.REPORTED, "19.0")}
    assert preview_of(held, source, "a").digest == preview_of(unmarked, source, "a").digest
    assert item.status is ItemStatus.CHANGED


def test_a_status_assertion_feeds_no_check_planning_or_her_account(
    tmp_path: pathlib.Path,
) -> None:
    """A Status "Missing" asserted beside the school's Valid leaves her Not yet and Done, her
    Turned in, the school's Missing email, and the planner input as they were."""
    facts = ("student_reports", "hand_in_events", "status_reports", "assignments")
    planner = practice_store(tmp_path / "planner.sqlite3")
    with household_client("open", tmp_path) as client:
        store = state_of(client).project_state
        for one in (planner, store):
            assert isinstance(save_grade(one, WREN, key=KEY), GradeReportSaved)

        def planned() -> tuple[str, list[dict[str, object]]]:
            week = week_from(read_everything(planner, planner), MONDAY)
            return planning_digest(week), canonical_active_input(week)

        before = (stored(store, *facts), planned())
        for one in (store, planner):
            class_id = capture_class(one, WREN)
            seed = result_named(one, class_id, "Seed Germination Log")
            page = correction_page(one, class_id, "result", seed)
            asserted = assert_value(one, class_id, page, "status", reported("Missing"))
            assert isinstance(asserted, ValueCorrected), asserted
        after = (stored(store, *facts), planned())
    planner.close()

    assert before[0][3]
    assert len(before[1][1]) == 2
    assert after == before


def test_a_withdrawn_assertion_keeps_its_history_and_the_school_value(
    tmp_path: pathlib.Path,
) -> None:
    store, class_id = saved(tmp_path)
    cell = result_named(store, class_id, "Cell Diagram")
    first = correction_page(store, class_id, "result", cell)
    assert isinstance(
        assert_value(store, class_id, first, "points", reported("9.0")), ValueCorrected
    )
    before = stored(store, *OBSERVATIONS)
    second = correction_page(store, class_id, "result", cell)

    outcome = assert_value(store, class_id, second, "points", None)
    current = store.current_values(class_id, "T1").results[cell]
    history = store.correction_history(class_id, "T1", "result", cell)
    third = correction_page(store, class_id, "result", cell)
    with pytest.raises(ValueError, match="no assertion"):
        assert_value(store, class_id, third, "points", None)
    after = stored(store, *OBSERVATIONS)
    store.close()

    assert outcome == ValueCorrected(second.correction_id, "points", "parent_assertion", None)
    assert current.asserted == {}
    assert current.cells["points"] == (Presence.REPORTED, "7.0")
    assert [(one.value, one.sequence) for one in history] == [("9.0", 1), (None, 2)]
    assert after == before


@pytest.mark.parametrize(
    ("kind", "field", "value", "allowed"),
    [
        ("result", "points", reported("9.5"), True),
        ("result", "points", reported("-1"), True),
        ("result", "points", reported("9.5 points"), False),
        ("result", "points", GradeValue(text="", presence=Presence.BLANK), False),
        ("result", "points", GradeNumber(text="9.O", presence=Presence.UNREADABLE), False),
        ("result", "status", reported("Missing"), True),
        ("result", "status", reported("Valid"), True),
        ("result", "status", reported("Late"), False),
        ("result", "status", reported("missing"), False),
        ("result", "note", reported("Turned in late"), False),
        ("result", "assignment", reported("Cell Drawing"), False),
        ("result", "due", reported("09/27"), False),
        ("result", "category", reported("Labs"), False),
        ("term", "letter", reported("A-"), True),
        ("term", "letter", reported("F"), True),
        ("term", "letter", reported("E+"), True),
        ("term", "letter", reported("G"), False),
        ("term", "letter", reported("A--"), False),
        ("term", "letter", reported("a"), False),
        ("term", "percent", reported("88.5"), True),
        ("term", "percent", reported("B"), False),
        ("category", "average", reported("81.0"), True),
        ("category", "weight", reported("heavy"), False),
        ("category", "name", reported("Labs"), False),
    ],
)
def test_assertions_take_only_their_closed_forms(
    tmp_path: pathlib.Path, kind: str, field: str, value: GradeValue, allowed: bool
) -> None:
    """A number in the field's form, a letter A to F with + or -, or a status the class and term
    already hold; free text, a Note, an identity cell and a blank are refused in the store."""
    store, class_id = saved(tmp_path)
    target = {
        "term": TERM_KEY,
        "category": next(iter(store.current_values(class_id, "T1").categories)),
        "result": result_named(store, class_id, "Cell Diagram"),
    }[kind]
    page = correction_page(store, class_id, kind, target)
    before = stored(store, "grade_corrections", "grade_scope_revisions")

    if allowed:
        assert isinstance(assert_value(store, class_id, page, field, value), ValueCorrected)
    else:
        with pytest.raises(ValueError, match="closed form|value cell"):
            assert_value(store, class_id, page, field, value)
        assert stored(store, "grade_corrections", "grade_scope_revisions") == before
    store.close()


def test_a_saved_value_refuses_a_transcription_correction_in_4b(tmp_path: pathlib.Path) -> None:
    store, class_id = saved(tmp_path)
    cell = result_named(store, class_id, "Cell Diagram")
    page = correction_page(store, class_id, "result", cell)
    before = stored(store, "grade_corrections", "grade_scope_revisions")

    with pytest.raises(ValueError, match="transcription"):
        store.correct_value(
            class_id,
            "T1",
            page=page,
            field="points",
            how="transcription",
            value=reported("7.0"),
            confirmed=True,
            role="parent",
        )
    after = stored(store, "grade_corrections", "grade_scope_revisions")
    store.close()

    assert after == before


def test_an_assertion_retry_returns_its_outcome_and_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    store, class_id = saved(tmp_path)
    cell = result_named(store, class_id, "Cell Diagram")
    page = correction_page(store, class_id, "result", cell)
    first = assert_value(store, class_id, page, "points", reported("9.0"))
    before = stored(store, "grade_corrections", "grade_scope_revisions")

    again = assert_value(store, class_id, page, "points", reported("9.0"))
    other = assert_value(store, class_id, page, "average", reported("95.0"))
    after = stored(store, "grade_corrections", "grade_scope_revisions")
    store.close()

    assert isinstance(first, ValueCorrected)
    assert again == CorrectionRecorded(first)
    assert isinstance(other, CorrectionRecorded)
    assert other.recorded == first
    assert other.fresh is not None
    assert other.fresh.page.correction_id != page.correction_id
    assert other.fresh.page.revision == page.revision + 1
    assert (other.fresh.field, other.fresh.read) == ("average", (Presence.REPORTED, "70.0"))
    assert after == before


def test_a_stale_assertion_page_returns_and_an_unchanged_one_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    store, class_id = saved(tmp_path)
    cell = result_named(store, class_id, "Cell Diagram")
    stale = correction_page(store, class_id, "result", cell)
    assert isinstance(
        assert_value(
            store,
            class_id,
            correction_page(store, class_id, "result", cell),
            "points",
            reported("9.0"),
        ),
        ValueCorrected,
    )
    before = stored(store, "grade_corrections", "grade_scope_revisions")

    returned = assert_value(store, class_id, stale, "points", reported("9.5"))
    unchanged = assert_value(
        store, class_id, correction_page(store, class_id, "result", cell), "points", reported("9.0")
    )
    after = stored(store, "grade_corrections", "grade_scope_revisions")
    store.close()

    assert isinstance(returned, CorrectionReturned)
    assert returned.now.page.correction_id != stale.correction_id
    assert returned.now.page.revision == stale.revision + 1
    assert returned.now.asserted == (Presence.REPORTED, "9.0")
    assert isinstance(unchanged, NothingToCorrect)
    assert after == before


def test_an_assertion_raises_the_revision_so_open_reviews_and_previews_return(
    tmp_path: pathlib.Path,
) -> None:
    store, class_id = saved(tmp_path)
    review = store.review_grade_report(EIGHT, capture_key(EIGHT), key=KEY)
    answers = dataclasses.replace(
        grade_answers(store.review_grade_report(NINETEEN, capture_key(NINETEEN), key=KEY)),
        use="earlier",
    )
    assert isinstance(save_grade(store, NINETEEN, key=KEY, answers=answers), GradeReportSaved)
    preview = current_preview(store, NINETEEN)
    cell = result_named(store, class_id, "Cell Diagram")
    page = correction_page(store, class_id, "result", cell)
    assert isinstance(
        assert_value(store, class_id, page, "points", reported("9.0")), ValueCorrected
    )

    stale = save_grade(store, EIGHT, key=KEY, review=review)
    confirmed = confirm_current(store, NINETEEN, preview)
    (revision,) = store._connection.execute(
        "SELECT revision FROM grade_scope_revisions WHERE class_id = ?", (class_id,)
    ).fetchone()
    store.close()

    assert isinstance(stale, ReviewReturned)
    assert stale.why is ReturnReason.REVISION
    assert isinstance(confirmed, PreviewRevised)
    assert revision == page.revision + 1


def deleted(store: ProjectStateStore, class_id: str) -> None:
    preview = store.delete_preview(class_id, "T1")
    assert isinstance(preview, DeletePreview)
    removed = store.delete_class_term(class_id, "T1", revision=preview.revision, role="parent")
    assert isinstance(removed, ClassTermDeleted)


def test_an_assertion_then_a_delete_then_the_same_capture_re_imported_carries_nothing(
    tmp_path: pathlib.Path,
) -> None:
    store, class_id = saved(tmp_path)
    cell = result_named(store, class_id, "Cell Diagram")
    page = correction_page(store, class_id, "result", cell)
    assert isinstance(
        assert_value(store, class_id, page, "points", reported("9.0")), ValueCorrected
    )

    deleted(store, class_id)
    corrections = stored(store, "grade_corrections")
    assert isinstance(save_grade(store, WREN, key=KEY), GradeReportSaved)
    again = result_named(store, class_id, "Cell Diagram")
    current = store.current_values(class_id, "T1").results[again]
    history = store.correction_history(class_id, "T1", "result", again)
    store.close()

    assert corrections == [[]]
    assert current.asserted == {}
    assert history == ()


def test_an_assertion_page_from_before_a_delete_writes_nothing(tmp_path: pathlib.Path) -> None:
    store, class_id = saved(tmp_path)
    cell = result_named(store, class_id, "Cell Diagram")
    used = correction_page(store, class_id, "result", cell)
    assert isinstance(
        assert_value(store, class_id, used, "points", reported("9.0")), ValueCorrected
    )
    unused = correction_page(store, class_id, "result", cell)

    deleted(store, class_id)
    before = stored(store, "grade_corrections", "grade_scope_revisions")
    retried = assert_value(store, class_id, used, "points", reported("9.0"))
    fresh = assert_value(store, class_id, unused, "points", reported("9.5"))
    after = stored(store, "grade_corrections", "grade_scope_revisions")
    store.close()

    assert retried == ObservationNotSaved()
    assert fresh == ObservationNotSaved()
    assert after == before


def test_assertions_are_not_in_the_review(tmp_path: pathlib.Path) -> None:
    """No review item's current value carries an assertion; the class's current values do."""
    store, class_id = saved(tmp_path)
    cell = result_named(store, class_id, "Cell Diagram")
    for kind, target, field, text in (
        ("result", cell, "points", "9.0"),
        ("term", TERM_KEY, "letter", "A-"),
    ):
        page = correction_page(store, class_id, kind, target)
        assert isinstance(
            assert_value(store, class_id, page, field, reported(text)), ValueCorrected
        )

    reviews = [
        store.review_grade_report(draft, capture_key(draft), key=KEY) for draft in (WREN, EIGHT)
    ]
    current = store.current_values(class_id, "T1")
    store.close()

    items = [
        item
        for review in reviews
        for item in (*((review.term,) if review.term else ()), *review.categories, *review.rows)
    ]
    assert [item for item in items if item.current is not None]
    assert all(item.current.asserted == {} for item in items if item.current is not None)
    assert current.results[cell].asserted == {"points": (Presence.REPORTED, "9.0")}
    assert current.term is not None
    assert current.term.asserted == {"letter": (Presence.REPORTED, "A-")}


def test_a_rest_saved_into_the_action_s_report_is_its_own_original(
    tmp_path: pathlib.Path,
) -> None:
    """A value a capture's rest saved into the report an action made isn't in the action's copy
    list, so an assertion on it is kept on that report itself."""
    store, class_id = saved(tmp_path)
    review = store.review_grade_report(NINETEEN, capture_key(NINETEEN), key=KEY)
    answers = dataclasses.replace(grade_answers(review), use="earlier")
    seed = result_named(store, class_id, "Seed Germination Log")
    (seed_key,) = (item.key for item in review.rows if item.result_id == seed)
    first = save_grade(
        store,
        NINETEEN,
        key=KEY,
        review=review,
        answers=answers,
        selection=review.ready - {seed_key},
    )
    assert isinstance(first, GradeReportSaved), first
    made = confirm_current(store, NINETEEN)
    assert isinstance(made, MadeCurrent), made
    rest = save_grade(store, NINETEEN, key=KEY)
    assert isinstance(rest, GradeReportSaved), rest
    assert rest.report_id == made.report_id
    page = correction_page(store, class_id, "result", seed)
    assert page.report_id == made.report_id

    outcome = assert_value(store, class_id, page, "points", reported("20.0"))
    (record,) = store.correction_history(class_id, "T1", "result", seed)
    store.close()

    assert isinstance(outcome, ValueCorrected)
    assert (record.report_id, record.entered_on) == (made.report_id, made.report_id)


def test_a_not_captured_cell_takes_no_assertion_or_correction(tmp_path: pathlib.Path) -> None:
    store = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    draft = draft_of(without_columns(REPORT, "Homework / Practice", "Avg"))
    assert isinstance(save_grade(store, draft, key=KEY), GradeReportSaved)
    class_id = capture_class(store, draft)
    cell = result_named(store, class_id, "Cell Diagram")
    page = correction_page(store, class_id, "result", cell)
    before = stored(store, "grade_corrections", "grade_scope_revisions")

    with pytest.raises(ValueError, match="captured"):
        assert_value(store, class_id, page, "average", reported("70.0"))
    after = stored(store, "grade_corrections", "grade_scope_revisions")
    store.close()

    assert after == before


def test_a_replay_checks_access_and_ownership_first(tmp_path: pathlib.Path) -> None:
    """A student role is refused before any lookup, a recorded ID included; a recorded ID whose
    observation a delete removed returns ``ObservationNotSaved``, never its old outcome."""
    store, class_id = saved(tmp_path)
    cell = result_named(store, class_id, "Cell Diagram")
    page = correction_page(store, class_id, "result", cell)
    assert isinstance(
        assert_value(store, class_id, page, "points", reported("9.0")), ValueCorrected
    )
    before = stored(store, "grade_corrections", "grade_scope_revisions")

    ran: list[str] = []
    store._connection.set_trace_callback(ran.append)
    with pytest.raises(ValueError, match="only a parent"):
        assert_value(store, class_id, page, "points", reported("9.0"), role="student")
    store._connection.set_trace_callback(None)
    elsewhere = assert_value(store, "another-class", page, "points", reported("9.0"))
    deleted(store, class_id)
    replayed = assert_value(store, class_id, page, "points", reported("9.0"))
    after = stored(store, "grade_corrections", "grade_scope_revisions")
    store.close()

    assert ran == []
    assert elsewhere == ObservationNotSaved()
    assert replayed == ObservationNotSaved()
    assert after[0] == []
    assert before[0]
