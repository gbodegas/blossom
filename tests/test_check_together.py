# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Check together: each homework's standing from her two accounts, what one read of the
record and the gradebook counts, her week's line, each counted result's explanation on her
cards, rows and assignment details, and Grades' reads and counts.

The standing is stated as plain values. Every other case saves Wren's synthetic report through
the gradebook's own save, decides links through its own write, keeps her updates and hand-ins
through the record's own writes, and reads both as a page does, in one hold, or reads her week
as a browser does. Where a test changes a stored row through its own SQL, its docstring says so.
"""

import dataclasses
import html
import itertools
import logging
import pathlib
import re
import sqlite3
from collections.abc import Callable, Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient

from blossom.grades.draft import GradeReportDraft, Presence, capture_key
from blossom.grades.identity import name_form_key
from blossom.grades.projection import MadeCurrent
from blossom.grades.review import GradeReportSaved, GradeValueUndecodable
from blossom.grades.text_reader import read_grade_report
from blossom.hand_in import NEEDS_HAND_IN, NOT_REQUIRED, TURNED_IN, UNKNOWN
from blossom.noticing import canonical_active_input, planning_digest, read_everything, week_from
from blossom.reconciliation import SourceChannel
from blossom.routes.grades import GradesRead, class_rows, grades_read
from blossom.routes.homework_scores import (
    CheckStanding,
    HerAccount,
    account_of,
    check_standing,
    read_score_facts,
    rule_values,
    to_check,
)
from blossom.stores.gradebook import (
    GRADEBOOK_TABLES,
    UNCONNECTED,
    ClassTermDeleted,
    ContextChanged,
    DeletePreview,
    GradeReportNotSaved,
    GradeTransactionLost,
    HomeworkLinkWritten,
    LinkDecision,
    TermNotOnRecord,
    ViewNotOnRecord,
)
from blossom.stores.project_state import Assignment, AssignmentKind, ProjectStateStore
from tests.support import (
    FIXTURES,
    HER_PAGE,
    NOT_UTF8,
    NOW,
    PAGE_HEADERS,
    PLAN_DATE,
    ZONE,
    Answer,
    Statements,
    capture_class,
    card_for,
    closed_world,
    confirm_current,
    family_page,
    fixture_clock,
    grade_answers,
    homework_named,
    household_client,
    mark,
    reported,
    save_grade,
    school_missing,
    school_said,
    sign_in_as,
    spoil,
    state_of,
    status_of,
    store_of,
    without_columns,
)

COUNTED, NOT_COUNTED, INDETERMINATE = (
    CheckStanding.COUNTED,
    CheckStanding.NOT_COUNTED,
    CheckStanding.INDETERMINATE,
)
KEY = name_form_key(b"5" * 64)
YEAR = "2026-2027"
BIOLOGY = "Biology"
DIAGRAM, LOG = "Cell Diagram", "Seed Germination Log"
DIAGRAM_ID, OTHER_ID = "assignment-cell-diagram", "assignment-lab-notebook"
REPORT = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")
DIAGRAM_ROW = "| Cell Diagram             | 7.0     | 10.0    | 70.0    | Missing    |"
LOG_ROW = "| Seed Germination Log | 18.0    | 20.0    | 90.0    | Valid      |"
VALID = REPORT.replace(DIAGRAM_ROW, DIAGRAM_ROW.replace("Missing", "Valid  "))
"""Wren's report with "Cell Diagram" reported Valid."""
VALID_NINE = REPORT.replace(
    DIAGRAM_ROW, "| Cell Diagram             | 9.0     | 10.0    | 90.0    | Valid      |"
)
"""Wren's report with "Cell Diagram" reported Valid, 9 of 10."""
WITHOUT_DIAGRAM = "\n".join(
    line
    for line in REPORT.replace(LOG_ROW, LOG_ROW.replace("18.0", "19.0")).split("\n")
    if DIAGRAM not in line
)
"""Wren's report without "Cell Diagram", and the log's score changed."""


def draft_of(text: str) -> GradeReportDraft:
    reading = read_grade_report(text)
    assert reading.draft is not None, reading.not_read
    return reading.draft


@pytest.fixture
def store(tmp_path: pathlib.Path) -> Iterator[ProjectStateStore]:
    """A file of its own, with "Cell Diagram" and a second Biology homework on record."""
    assert (REPORT.count(DIAGRAM_ROW), REPORT.count(LOG_ROW)) == (1, 1)
    opened = ProjectStateStore.open(tmp_path / "blossom.sqlite3", fixture_clock())
    try:
        opened.put_on_record(
            [
                homework_named(BIOLOGY, DIAGRAM_ID, DIAGRAM),
                homework_named(BIOLOGY, OTHER_ID, "Lab Notebook"),
            ],
            {},
        )
        yield opened
    finally:
        opened.close()


def saved(store: ProjectStateStore, text: str = REPORT, **how: object) -> str:
    """A parent's save of the report ``text``, and the class it went into."""
    draft = draft_of(text)
    outcome = save_grade(store, draft, key=KEY, **how)  # type: ignore[arg-type]
    assert isinstance(outcome, GradeReportSaved), outcome
    return capture_class(store, draft)


def kept_as_earlier(store: ProjectStateStore, text: str) -> None:
    """A parent's save of the report ``text`` as an earlier report."""
    draft = draft_of(text)
    review = store.review_grade_report(draft, capture_key(draft), key=KEY)
    answers = dataclasses.replace(grade_answers(review), use="earlier")
    outcome = save_grade(store, draft, key=KEY, review=review, answers=answers)
    assert isinstance(outcome, GradeReportSaved), outcome


def result_named(store: ProjectStateStore, class_id: str, title: str = DIAGRAM) -> str:
    (result_id,) = [
        result_id
        for result_id, value in store.class_term(class_id, "T1").current.results.items()
        if value.cells["assignment"][1] == title
    ]
    return result_id


def decided(
    store: ProjectStateStore,
    class_id: str,
    result_id: str,
    assignment_id: str = DIAGRAM_ID,
    decision: LinkDecision = "link",
) -> None:
    """A parent's decision about a result and a homework, made by the store's own write."""
    preview = store.delete_preview(class_id, "T1")
    assert isinstance(preview, DeletePreview)
    assert preview.revision is not None
    outcome = store.decide_homework_link(
        result_id, assignment_id, decision=decision, revision=preview.revision, role="parent"
    )
    assert isinstance(outcome, HomeworkLinkWritten), outcome


def linked_report(store: ProjectStateStore) -> tuple[str, str]:
    """Wren's report saved and "Cell Diagram", reported Missing, linked to its homework: the
    class and the result."""
    class_id = saved(store)
    result_id = result_named(store, class_id)
    decided(store, class_id, result_id)
    return class_id, result_id


def handed(store: ProjectStateStore, state: str, assignment_id: str = DIAGRAM_ID) -> None:
    """Her hand-in, saved over whatever stands."""
    readings = store.hand_in_readings([assignment_id]).readable
    head = readings[assignment_id].head_id if assignment_id in readings else None
    store.record_hand_in(
        assignment_id,
        state,  # type: ignore[arg-type]
        None,
        None,
        expected_head=head,
        now=NOW,
        today=PLAN_DATE,
    )


def checks_in(store: ProjectStateStore) -> dict[str, tuple[CheckStanding, list[str]]]:
    """Each homework with a counted result, as one page read gives it, with its standing and
    its counted results' IDs: the record, the grade facts and her links read in one hold."""
    with store.reading():
        everything = read_everything(store, store)
        years, links = read_score_facts(store, (item.course for item in everything.assignments))
    rules = rule_values(
        everything.assignments, everything.records, everything.claims_unavailable, years
    )
    return {
        assignment_id: (one.standing, [result.result_id for result in one.results])
        for assignment_id, one in to_check(rules, links, everything).items()
    }


# ------------------------------------------------------------- the standing, as plain values

READINGS = (True, False, None)
"""Each record of hers says it, says otherwise, or can't be read."""


@pytest.mark.parametrize(("done", "turned_in"), list(itertools.product(READINGS, READINGS)))
def test_done_or_turned_in_counts_whatever_the_other_says_and_unreadable_alone_is_unknown(
    done: bool | None, turned_in: bool | None
) -> None:
    """Counted when a readable record says Done or Turned in; not counted when both are
    readable and neither says so; otherwise unknown."""
    standing = check_standing(HerAccount(done=done, turned_in=turned_in))

    if done is True or turned_in is True:
        assert standing is COUNTED
    elif done is None or turned_in is None:
        assert standing is INDETERMINATE
    else:
        assert standing is NOT_COUNTED


# ------------------------------------------------------------- her account, through the store


def test_her_done_or_turned_in_beside_a_linked_missing_result_counts(
    store: ProjectStateStore,
) -> None:
    _, result_id = linked_report(store)
    nothing_said = checks_in(store)
    reported(store, "done", DIAGRAM_ID)
    done = checks_in(store)
    handed(store, NEEDS_HAND_IN)
    done_still_to_hand_in = checks_in(store)

    assert nothing_said == {DIAGRAM_ID: (NOT_COUNTED, [result_id])}
    assert done == {DIAGRAM_ID: (COUNTED, [result_id])}
    assert done_still_to_hand_in == {DIAGRAM_ID: (COUNTED, [result_id])}


def test_turned_in_alone_counts(store: ProjectStateStore) -> None:
    _, result_id = linked_report(store)
    handed(store, TURNED_IN)

    assert checks_in(store) == {DIAGRAM_ID: (COUNTED, [result_id])}
    assert account_of(read_everything(store, store), DIAGRAM_ID) == HerAccount(
        done=False, turned_in=True
    )


@pytest.mark.parametrize("hand_in", [None, UNKNOWN, NOT_REQUIRED, NEEDS_HAND_IN])
@pytest.mark.parametrize("update", [None, "not_yet"])
def test_neither_done_nor_turned_in_with_every_record_readable_is_not_counted(
    store: ProjectStateStore, update: str | None, hand_in: str | None
) -> None:
    _, result_id = linked_report(store)
    if update is not None:
        reported(store, update, DIAGRAM_ID)
    if hand_in is not None:
        handed(store, hand_in)

    assert checks_in(store) == {DIAGRAM_ID: (NOT_COUNTED, [result_id])}


def test_done_undone_or_changed_to_not_yet_ends_it_unless_turned_in_still_stands(
    store: ProjectStateStore,
) -> None:
    _, result_id = linked_report(store)
    reported(store, "done", DIAGRAM_ID)
    counted = checks_in(store)
    store.undo_report(
        DIAGRAM_ID, store.student_reports(DIAGRAM_ID)[-1].report_id, now=NOW, today=PLAN_DATE
    )
    undone = checks_in(store)
    reported(store, "done", DIAGRAM_ID)
    reported(store, "not_yet", DIAGRAM_ID)
    not_yet = checks_in(store)
    handed(store, TURNED_IN)
    turned_in_stands = checks_in(store)

    assert counted == {DIAGRAM_ID: (COUNTED, [result_id])}
    assert undone == {DIAGRAM_ID: (NOT_COUNTED, [result_id])}
    assert not_yet == {DIAGRAM_ID: (NOT_COUNTED, [result_id])}
    assert turned_in_stands == {DIAGRAM_ID: (COUNTED, [result_id])}


def test_turned_in_undone_ends_it_unless_done_still_stands(store: ProjectStateStore) -> None:
    _, result_id = linked_report(store)
    handed(store, TURNED_IN)
    counted = checks_in(store)
    head = store.hand_in_readings([DIAGRAM_ID]).readable[DIAGRAM_ID].head_id
    assert head is not None
    store.undo_hand_in(DIAGRAM_ID, head, now=NOW, today=PLAN_DATE)
    undone = checks_in(store)
    reported(store, "done", DIAGRAM_ID)
    handed(store, TURNED_IN)
    head = store.hand_in_readings([DIAGRAM_ID]).readable[DIAGRAM_ID].head_id
    assert head is not None
    store.undo_hand_in(DIAGRAM_ID, head, now=NOW, today=PLAN_DATE)
    done_stands = checks_in(store)

    assert counted == {DIAGRAM_ID: (COUNTED, [result_id])}
    assert undone == {DIAGRAM_ID: (NOT_COUNTED, [result_id])}
    assert done_stands == {DIAGRAM_ID: (COUNTED, [result_id])}


def test_a_record_of_hers_that_can_t_be_read_never_cancels_the_other_s_valid_evidence(
    store: ProjectStateStore,
) -> None:
    """Her hand-in note is changed to bytes that are not UTF-8, then her update's, each by the
    test's own SQL: Done beside an unreadable hand-in counts, and Turned in beside unreadable
    updates counts."""
    _, result_id = linked_report(store)
    reported(store, "done", DIAGRAM_ID)
    handed(store, NEEDS_HAND_IN)
    spoil(store, "hand_in_events", "note", NOT_UTF8, DIAGRAM_ID)
    done_beside_unreadable = checks_in(store)
    account = account_of(read_everything(store, store), DIAGRAM_ID)

    assert account == HerAccount(done=True, turned_in=None)
    assert done_beside_unreadable == {DIAGRAM_ID: (COUNTED, [result_id])}


def test_turned_in_beside_updates_that_can_t_be_read_counts(store: ProjectStateStore) -> None:
    """Her update's note is changed to bytes that are not UTF-8 by the test's own SQL."""
    _, result_id = linked_report(store)
    reported(store, "not_yet", DIAGRAM_ID)
    handed(store, TURNED_IN)
    spoil(store, "student_reports", "note", NOT_UTF8, DIAGRAM_ID)

    assert account_of(read_everything(store, store), DIAGRAM_ID) == HerAccount(
        done=None, turned_in=True
    )
    assert checks_in(store) == {DIAGRAM_ID: (COUNTED, [result_id])}


@pytest.mark.parametrize(
    ("update", "hand_in", "spoiled"),
    [
        pytest.param("done", TURNED_IN, ("student_reports", "hand_in_events"), id="both"),
        pytest.param("not_yet", TURNED_IN, ("hand_in_events",), id="hand-in, updates not yet"),
        pytest.param("done", NEEDS_HAND_IN, ("student_reports",), id="updates, to hand in"),
    ],
)
def test_neither_readable_record_saying_so_beside_one_that_can_t_be_read_is_unknown(
    store: ProjectStateStore, update: str, hand_in: str, spoiled: tuple[str, ...]
) -> None:
    """The named tables' notes are changed to bytes that are not UTF-8 by the test's own SQL,
    so what those records said is unknown and never read as Done or Turned in."""
    _, result_id = linked_report(store)
    reported(store, update, DIAGRAM_ID)
    handed(store, hand_in)
    for table in spoiled:
        spoil(store, table, "note", NOT_UTF8, DIAGRAM_ID)

    assert checks_in(store) == {DIAGRAM_ID: (INDETERMINATE, [result_id])}


# ------------------------------------------------------------- the gradebook, through the store


def test_newer_current_report_saying_valid_ends_it_and_one_kept_as_earlier_does_not(
    store: ProjectStateStore,
) -> None:
    _, result_id = linked_report(store)
    reported(store, "done", DIAGRAM_ID)
    kept_as_earlier(store, VALID_NINE)
    after_earlier = checks_in(store)
    saved(store, VALID)
    after_current = checks_in(store)

    assert after_earlier == {DIAGRAM_ID: (COUNTED, [result_id])}
    assert after_current == {}


def test_a_newer_report_left_unticked_keeps_the_status_and_ticked_supplies_its_own(
    store: ProjectStateStore,
) -> None:
    """Report B shows "Cell Diagram" Valid, 9 of 10. Unticked, the value and its Status stay
    report A's and B only shows it; ticked, B supplies Valid."""
    class_id, result_id = linked_report(store)
    reported(store, "done", DIAGRAM_ID)
    newer = draft_of(VALID_NINE)
    review = store.review_grade_report(newer, capture_key(newer), key=KEY)
    (changed,) = [item.key for item in review.rows if item.result_id == result_id]
    unticked = save_grade(store, newer, key=KEY, review=review, selection=review.ready - {changed})
    assert isinstance(unticked, GradeReportSaved)
    value = store.class_term(class_id, "T1").current.results[result_id]
    shown_only = checks_in(store)
    ticked = saved(store, VALID_NINE.replace("| 9.0 ", "| 8.0 ").replace("| 90.0 ", "| 80.0 "))
    after_ticked = checks_in(store)

    assert value.last_shown is not None
    assert value.last_shown.report_id == unticked.report_id
    assert value.report_id != unticked.report_id
    assert value.cells["status"][1] == "Missing"
    assert shown_only == {DIAGRAM_ID: (COUNTED, [result_id])}
    assert ticked == class_id
    assert after_ticked == {}


def test_a_newer_current_report_without_the_status_cell_ends_it(
    store: ProjectStateStore,
) -> None:
    """The newer copy has no Status column and another score, so it supplies the value."""
    class_id, result_id = linked_report(store)
    reported(store, "done", DIAGRAM_ID)
    counted = checks_in(store)
    rescored = REPORT.replace(DIAGRAM_ROW, DIAGRAM_ROW.replace("| 7.0 ", "| 8.0 "))
    saved(store, without_columns(rescored, "Homework / Practice", "Status"))
    value = store.class_term(class_id, "T1").current.results[result_id]

    assert counted == {DIAGRAM_ID: (COUNTED, [result_id])}
    assert value.cells["status"][0] is Presence.NOT_CAPTURED
    assert value.cells["points"][1] == "8.0"
    assert checks_in(store) == {}


def test_the_class_details_action_making_a_valid_report_current_ends_it(
    store: ProjectStateStore,
) -> None:
    class_id = saved(store, VALID)
    saved(store)
    result_id = result_named(store, class_id)
    decided(store, class_id, result_id)
    reported(store, "done", DIAGRAM_ID)
    counted = checks_in(store)
    made = confirm_current(store, draft_of(VALID))
    assert isinstance(made, MadeCurrent), made

    assert counted == {DIAGRAM_ID: (COUNTED, [result_id])}
    assert checks_in(store) == {}


@pytest.mark.parametrize(
    "decisions",
    [
        pytest.param(("apart",), id="Remove link or Not this homework"),
        pytest.param(("apart", "automatic"), id="Let Blossom match again"),
    ],
)
def test_a_parent_s_link_decision_ends_it(
    store: ProjectStateStore, decisions: tuple[LinkDecision, ...]
) -> None:
    class_id, result_id = linked_report(store)
    reported(store, "done", DIAGRAM_ID)
    counted = checks_in(store)
    for decision in decisions:
        decided(store, class_id, result_id, decision=decision)

    assert counted == {DIAGRAM_ID: (COUNTED, [result_id])}
    assert checks_in(store) == {}


def test_a_link_to_other_homework_moves_the_basis_to_that_homework(
    store: ProjectStateStore,
) -> None:
    class_id, result_id = linked_report(store)
    reported(store, "done", DIAGRAM_ID)
    decided(store, class_id, result_id, OTHER_ID)

    assert checks_in(store) == {OTHER_ID: (NOT_COUNTED, [result_id])}


def test_a_removed_class_connection_ends_it(store: ProjectStateStore) -> None:
    _, result_id = linked_report(store)
    reported(store, "done", DIAGRAM_ID)
    counted = checks_in(store)
    store.connect_homework_class(YEAR, BIOLOGY, shown=None, chosen=UNCONNECTED, role="parent")

    assert counted == {DIAGRAM_ID: (COUNTED, [result_id])}
    assert checks_in(store) == {}


def test_a_complete_newer_report_leaving_the_result_out_ends_it_and_a_partial_one_does_not(
    store: ProjectStateStore,
) -> None:
    _, result_id = linked_report(store)
    reported(store, "done", DIAGRAM_ID)
    saved(store, WITHOUT_DIAGRAM)
    after_partial = checks_in(store)
    saved(store, WITHOUT_DIAGRAM.replace("| 19.0 ", "| 17.0 "), complete=True)
    after_complete = checks_in(store)

    assert after_partial == {DIAGRAM_ID: (COUNTED, [result_id])}
    assert after_complete == {}


def test_a_delete_of_the_class_and_term_ends_it_and_leaves_her_record_as_it_was(
    store: ProjectStateStore, tmp_path: pathlib.Path
) -> None:
    class_id, result_id = linked_report(store)
    reported(store, "done", DIAGRAM_ID)
    handed(store, TURNED_IN)
    counted = checks_in(store)
    hers = closed_world([tmp_path / "blossom.sqlite3"], leaving_out=GRADEBOOK_TABLES)
    preview = store.delete_preview(class_id, "T1")
    assert isinstance(preview, DeletePreview)
    deleted = store.delete_class_term(class_id, "T1", revision=preview.revision, role="parent")

    assert isinstance(deleted, ClassTermDeleted), deleted
    assert counted == {DIAGRAM_ID: (COUNTED, [result_id])}
    assert checks_in(store) == {}
    assert closed_world([tmp_path / "blossom.sqlite3"], leaving_out=GRADEBOOK_TABLES) == hers


def test_a_link_naming_homework_not_on_record_counts_nothing(
    store: ProjectStateStore,
) -> None:
    """The homework row is deleted by the test's own SQL after its link is made, as no write of
    Blossom's deletes it."""
    _, result_id = linked_report(store)
    reported(store, "done", DIAGRAM_ID)
    counted = checks_in(store)
    store._connection.execute("DELETE FROM assignments WHERE assignment_id = ?", (DIAGRAM_ID,))
    store._connection.commit()

    assert counted == {DIAGRAM_ID: (COUNTED, [result_id])}
    assert checks_in(store) == {}


# ------------------------------------------------------------- invariants without a page


def test_g3c_i1_a_read_of_check_together_writes_nothing(
    week: TestClient, tmp_path: pathlib.Path
) -> None:
    """The household's three files, the record, the checkpoints and the traces, are the same
    before and after two reads of Check together through the application's own store."""
    store = store_of(week)
    linked_report(store)
    reported(store, "done", DIAGRAM_ID)
    handed(store, TURNED_IN)
    world = closed_world(household_files(tmp_path), leaving_out=())

    first, second = checks_in(store), checks_in(store)

    assert first == second
    assert first[DIAGRAM_ID][0] is COUNTED
    assert closed_world(household_files(tmp_path), leaving_out=()) == world


def test_g3c_i4_the_family_check_basis_is_the_same_with_and_without_a_grade_basis(
    store: ProjectStateStore,
) -> None:
    """Her Done beside the school's Missing email: the family check's basis, what it needs and
    its mark read the same before and after a counted grade basis, and a mark changes no grade
    basis."""
    store.record_status_reports(DIAGRAM_ID, [school_missing(date(2026, 9, 10))])
    reported(store, "done", DIAGRAM_ID)

    def family_check() -> tuple[object, ...]:
        status = status_of(store, DIAGRAM_ID)
        return status.check_basis, status.needs_a_check, status.check, status.checked

    without_grades = family_check()
    _, result_id = linked_report(store)
    with_grades = family_check()
    counted = checks_in(store)
    basis = status_of(store, DIAGRAM_ID).check_basis
    assert basis is not None
    store.mark_checked(
        DIAGRAM_ID,
        basis,
        None,
        expected_check=None,
        basis_now=lambda: status_of(store, DIAGRAM_ID).check_basis,
        now=NOW,
        today=PLAN_DATE,
    )
    marked = status_of(store, DIAGRAM_ID)

    assert without_grades[0] is not None
    assert without_grades[1] is True
    assert with_grades == without_grades
    assert counted == {DIAGRAM_ID: (COUNTED, [result_id])}
    assert marked.checked
    assert checks_in(store) == counted


def test_g3c_i6_the_planner_input_is_the_same_with_and_without_a_counted_check(
    store: ProjectStateStore,
) -> None:
    reported(store, "done", DIAGRAM_ID)
    handed(store, TURNED_IN)

    def planner_input() -> tuple[str, list[dict[str, object]]]:
        week = week_from(read_everything(store, store), date(2026, 9, 14))
        return planning_digest(week), canonical_active_input(week)

    before = planner_input()
    linked_report(store)
    counted = checks_in(store)
    after = planner_input()
    saved(store, VALID)
    ended = checks_in(store)
    after_ended = planner_input()

    assert counted[DIAGRAM_ID][0] is COUNTED
    assert ended == {}
    assert OTHER_ID in str(before[1])
    assert after == before
    assert after_ended == before


# ------------------------------------------------------------- her week's line

WEEK_DAY = date(2026, 8, 20)
LAB_ID = "assignment-lab-report"
CANT_WORK_OUT = "Check together can't be worked out right now."
CANT_WORK_OUT_LINE = f'<p class="problem">{CANT_WORK_OUT}</p>'
LINE = re.compile(r'<p class="confidence disagree to-check" role="status">.*?</p>', re.S)
LEFT_OUT = "a homework page shows no school score: "
LINK_ROW = "INSERT INTO grade_homework_links VALUES (?, ?, ?, 'confirmed', 'parent', ?)"
READERS = ["her", "parent", "open"]


def in_week(assignment_id: str, title: str, on: date = WEEK_DAY) -> Assignment:
    """Biology homework due in the fixture week."""
    return Assignment(
        assignment_id=assignment_id,
        course=BIOLOGY,
        title=title,
        due_date=on,
        dependencies=[],
        reported_submission_status="not_started",
        kind=AssignmentKind.HOMEWORK,
    )


@pytest.fixture
def week(tmp_path: pathlib.Path) -> Iterator[TestClient]:
    """The open household's fixture week, with "Cell Diagram" and "Lab Report" on record."""
    with household_client("open", tmp_path) as client:
        store_of(client).put_on_record(
            [in_week(LAB_ID, "Lab Report", date(2026, 8, 19)), in_week(DIAGRAM_ID, DIAGRAM)], {}
        )
        yield client


def her_week(client: TestClient) -> str:
    answer = client.get(HER_PAGE, headers=PAGE_HEADERS)
    assert answer.status_code == 200
    return answer.text


def lines_of(page: str) -> list[str]:
    """Each to-check line and each Check together problem on the page, as written, in order."""
    found = re.finditer(rf"{LINE.pattern}|{re.escape(CANT_WORK_OUT_LINE)}", page, re.S)
    return [one.group(0) for one in found]


def said(line: str) -> str:
    """A line as it reads, its tags dropped and its spaces folded."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", line)).split())


def names_in(line: str) -> list[str]:
    """Each link's accessible name in a line."""
    return [html.unescape(name) for name in re.findall(r'aria-label="([^"]*)"', line)]


def lab_missing_by_email(client: TestClient) -> None:
    """The school's email says "Lab Report" is missing, and she reports it Done."""
    store = store_of(client)
    store.record_status_reports(
        LAB_ID, [school_said("missing", SourceChannel.EMAIL, date(2026, 8, 18))]
    )
    reported(store, "done", LAB_ID)


def recorded(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        one.getMessage()
        for one in caplog.records
        if one.name.startswith("blossom") and one.levelno >= logging.WARNING
    ]


def undecodable(client: TestClient, result_id: str) -> None:
    """The result's points presence is set to text no save writes, by the test's own SQL."""
    store = store_of(client)
    with store._lock, store._connection:
        store._connection.execute(
            "UPDATE grade_result_observations SET points_presence = 'smudged' WHERE result_id = ?",
            (result_id,),
        )


def conflicting(client: TestClient, result_id: str) -> None:
    """The links index is dropped and a second confirmed link naming the result is stored for
    "Lab Report", by the test's own SQL."""
    store = store_of(client)
    with store._lock, store._connection:
        store._connection.execute("DROP INDEX grade_homework_links_one_homework")
        store._connection.execute(
            LINK_ROW, (store.student_id(), result_id, LAB_ID, "2026-08-19T12:00:00+00:00")
        )


FAILURES = {"undecodable": undecodable, "conflicting": conflicting}


@pytest.mark.parametrize("reader", READERS)
def test_her_done_beside_a_linked_missing_result_gives_one_assignment_to_check_together(
    reader: str, tmp_path: pathlib.Path
) -> None:
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        store_of(client).put_on_record([in_week(DIAGRAM_ID, DIAGRAM)], {})
        reported(store_of(client), "done", DIAGRAM_ID)
        before = lines_of(her_week(client))
        linked_report(store_of(client))
        page = her_week(client)

    (line,) = lines_of(page)
    assert before == []
    assert said(line) == f"1 assignment to check together: {DIAGRAM}."
    assert names_in(line) == [f"{DIAGRAM}, {BIOLOGY}: to check together"]
    (href,) = re.findall(r'href="([^"]*)"', line)
    assert html.unescape(href) == f"{HER_PAGE}?week=2026-08-17&show={DIAGRAM_ID}#title-{DIAGRAM_ID}"
    assert f'id="title-{DIAGRAM_ID}"' in page
    assert "<strong>1 assignment to check together:</strong>" in line
    assert CANT_WORK_OUT not in page


def test_turned_in_alone_beside_incomplete_counts_and_never_says_finished(
    week: TestClient,
) -> None:
    store = store_of(week)
    incomplete = REPORT.replace(DIAGRAM_ROW, DIAGRAM_ROW.replace("Missing   ", "Incomplete"))
    class_id = saved(store, incomplete)
    decided(store, class_id, result_named(store, class_id))
    handed(store, TURNED_IN)

    (line,) = lines_of(her_week(week))

    assert said(line) == f"1 assignment to check together: {DIAGRAM}."
    assert "finished" not in line


@pytest.mark.parametrize("hand_in", [None, UNKNOWN, NOT_REQUIRED])
def test_neither_done_nor_turned_in_with_every_record_readable_shows_no_line(
    week: TestClient, hand_in: str | None
) -> None:
    linked_report(store_of(week))
    reported(store_of(week), "not_yet", DIAGRAM_ID)
    if hand_in is not None:
        handed(store_of(week), hand_in)

    assert lines_of(her_week(week)) == []


def test_a_homework_on_both_bases_is_listed_once_and_mark_checked_leaves_it_listed(
    week: TestClient,
) -> None:
    """Her Done, the school's Missing email and a counted result on "Cell Diagram": one line
    lists it once; the family page lists it for the email; after Mark checked it stays."""
    store = store_of(week)
    store.record_status_reports(
        DIAGRAM_ID, [school_said("missing", SourceChannel.EMAIL, date(2026, 8, 18))]
    )
    reported(store, "done", DIAGRAM_ID)
    linked_report(store)
    before = lines_of(her_week(week))
    family = family_page(week)
    marked = mark(week, DIAGRAM_ID)
    after_page = her_week(week)
    after = lines_of(after_page)

    assert [said(line) for line in before] == [f"1 assignment to check together: {DIAGRAM}."]
    assert "Worth checking together" in family
    assert f'aria-label="Mark checked: {DIAGRAM}"' in family
    assert marked.status_code == 303
    assert status_of(store, DIAGRAM_ID).checked
    assert after == before
    assert explained(card_for(after_page, DIAGRAM_ID)) == [explanation(DIAGRAM, "Missing")]


def test_a_grade_basis_alone_offers_no_mark_checked_on_her_week_or_the_family_page(
    week: TestClient,
) -> None:
    reported(store_of(week), "done", DIAGRAM_ID)
    linked_report(store_of(week))

    page, family = her_week(week), family_page(week)

    assert [said(line) for line in lines_of(page)] == [
        f"1 assignment to check together: {DIAGRAM}."
    ]
    assert "Mark checked" not in page
    assert "Worth checking together" not in family
    assert "Mark checked" not in family


def test_an_email_basis_and_a_grade_basis_make_one_combined_line(week: TestClient) -> None:
    lab_missing_by_email(week)
    reported(store_of(week), "done", DIAGRAM_ID)
    linked_report(store_of(week))

    (line,) = lines_of(her_week(week))

    assert said(line) == "2 assignments to check together: Lab Report, Cell Diagram."
    assert names_in(line) == [
        "Lab Report, Biology: to check together",
        "Cell Diagram, Biology: to check together",
    ]
    assert "<strong>2 assignments to check together:</strong>" in line


def test_with_no_grade_basis_the_existing_line_is_unchanged(week: TestClient) -> None:
    """The email-basis line reads the same with no report on record and with a linked result
    the school reports Valid."""
    lab_missing_by_email(week)
    reported(store_of(week), "done", DIAGRAM_ID)
    without_grades = her_week(week)
    store = store_of(week)
    class_id = saved(store, VALID)
    decided(store, class_id, result_named(store, class_id))
    with_valid = her_week(week)

    assert lines_of(with_valid) == lines_of(without_grades)
    (line,) = lines_of(without_grades)
    assert said(line) == "1 finished assignment has a school report to check: Lab Report."
    assert names_in(line) == ["Lab Report, Biology: school report to check"]


@pytest.mark.parametrize("failure", FAILURES)
def test_a_covered_failure_with_done_in_reach_shows_no_count_and_says_so_once(
    week: TestClient, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    store = store_of(week)
    reported(store, "done", DIAGRAM_ID)
    _, result_id = linked_report(store)
    assert "School score" in her_week(week)
    FAILURES[failure](week, result_id)
    with caplog.at_level(logging.WARNING, logger="blossom"):
        page = her_week(week)

    assert lines_of(page) == [CANT_WORK_OUT_LINE]
    assert page.count(CANT_WORK_OUT) == 1
    assert "School score" not in page
    assert "Grade details" not in page
    (record,) = recorded(caplog)
    assert record.startswith(LEFT_OUT)
    assert record.removeprefix(LEFT_OUT) in {"GradeValueUndecodable", "HomeworkLinksConflict"}
    assert not [word for word in (DIAGRAM, BIOLOGY, "7.0", "Missing") if word in caplog.text]


@pytest.mark.parametrize("failure", FAILURES)
def test_a_covered_failure_with_nothing_in_reach_that_could_count_is_an_exact_zero(
    week: TestClient, failure: str
) -> None:
    _, result_id = linked_report(store_of(week))
    reported(store_of(week), "not_yet", DIAGRAM_ID)
    FAILURES[failure](week, result_id)

    page = her_week(week)

    assert lines_of(page) == []
    assert "School score" not in page


@pytest.mark.parametrize("failure", FAILURES)
def test_a_covered_failure_beside_an_email_basis_keeps_the_existing_line_then_says_so(
    week: TestClient, failure: str
) -> None:
    lab_missing_by_email(week)
    without_grades = lines_of(her_week(week))
    store = store_of(week)
    reported(store, "done", DIAGRAM_ID)
    _, result_id = linked_report(store)
    FAILURES[failure](week, result_id)

    page = her_week(week)

    assert lines_of(page) == [*without_grades, CANT_WORK_OUT_LINE]
    assert said(without_grades[0]).startswith("1 finished assignment has a school report")


@pytest.mark.parametrize(
    ("update", "hand_in", "spoiled"),
    [
        pytest.param("done", TURNED_IN, ("student_reports", "hand_in_events"), id="both"),
        pytest.param("not_yet", TURNED_IN, ("hand_in_events",), id="hand-in, updates not yet"),
        pytest.param("done", NEEDS_HAND_IN, ("student_reports",), id="updates, to hand in"),
    ],
)
def test_a_homework_left_unknown_by_a_record_of_hers_says_the_count_can_t_be_worked_out(
    week: TestClient, update: str, hand_in: str, spoiled: tuple[str, ...]
) -> None:
    """The named tables' notes are changed to bytes that are not UTF-8 by the test's own SQL."""
    store = store_of(week)
    linked_report(store)
    reported(store, update, DIAGRAM_ID)
    handed(store, hand_in)
    for table in spoiled:
        spoil(store, table, "note", NOT_UTF8, DIAGRAM_ID)

    page = her_week(week)

    assert lines_of(page) == [CANT_WORK_OUT_LINE]
    assert "(0)" not in page


def household_files(folder: pathlib.Path) -> list[pathlib.Path]:
    """The household's three files under ``folder``: the record, the checkpoints and the
    traces, each one there."""
    files = [folder / name for name in ("blossom.sqlite3", "checkpoints.sqlite3", "traces.sqlite3")]
    assert all(one.is_file() for one in files), files
    return files


def made_unreadable(client: TestClient, result_id: str) -> None:
    """Her hand-in set to still to turn in, then her update's note changed to bytes that are
    not UTF-8 by the test's own SQL: "Cell Diagram" is left unknown."""
    store = store_of(client)
    handed(store, NEEDS_HAND_IN)
    spoil(store, "student_reports", "note", NOT_UTF8, DIAGRAM_ID)


I1_STATES = {"counted": None, "covered failure": undecodable, "unknown": made_unreadable}


@pytest.mark.parametrize("state", I1_STATES)
@pytest.mark.parametrize("reader", READERS)
def test_g3c_i1_her_week_and_details_with_check_together_write_nothing(
    reader: str, state: str, tmp_path: pathlib.Path
) -> None:
    """Her week and the counted homework's details, read for her, a signed-in parent and the
    open household with the result counted, after a covered failure, and with the homework
    unknown: the record, the checkpoints and the traces are the same byte for byte."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        store = store_of(client)
        store.put_on_record(
            [in_week(LAB_ID, "Lab Report", date(2026, 8, 19)), in_week(DIAGRAM_ID, DIAGRAM)], {}
        )
        lab_missing_by_email(client)
        reported(store, "done", DIAGRAM_ID)
        _, result_id = linked_report(store)
        change = I1_STATES[state]
        if change is not None:
            change(client, result_id)
        world = closed_world(household_files(tmp_path), leaving_out=())
        week_page, details = her_week(client), details_of(client)
        after = closed_world(household_files(tmp_path), leaving_out=())

    shown = [said(line) for line in lines_of(week_page)]
    if state == "counted":
        assert shown == ["2 assignments to check together: Lab Report, Cell Diagram."]
        assert explained(details) == [explanation(DIAGRAM, "Missing")]
    else:
        assert shown[-1] == CANT_WORK_OUT
        assert explained(week_page + details) == []
    assert after == world


def masked(statements: list[str]) -> list[str]:
    """Statements as a read asks them, with each generated ID and stored moment masked: what
    two households seeded alike can't share."""
    masked_ids = [
        re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32}",
            "<id>",
            one,
        )
        for one in statements
    ]
    return [
        re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.]+(?:[+-]\d{2}:\d{2})?", "<moment>", one)
        for one in masked_ids
    ]


def asked_by(reader: str, folder: pathlib.Path, status: str) -> tuple[dict[str, list[str]], str]:
    """The statements her week and "Cell Diagram"'s details each ask in a household whose
    linked result the school reports with ``status``, and those two pages, joined."""
    folder.mkdir()
    with household_client(reader, folder) as client:
        sign_in_as(client, reader)
        store = store_of(client)
        store.put_on_record([in_week(DIAGRAM_ID, DIAGRAM)], {})
        reported(store, "done", DIAGRAM_ID)
        text = REPORT.replace(DIAGRAM_ROW, DIAGRAM_ROW.replace("Missing", status.ljust(7)))
        class_id = saved(store, text)
        decided(store, class_id, result_named(store, class_id))
        state = state_of(client)
        asked: dict[str, list[str]] = {}
        pages = []
        for name, read in (("her week", her_week), ("details", details_of)):
            read(client)
            with Statements(state) as seen:
                pages.append(read(client))
            asked[name] = masked(list(seen))
    return asked, "".join(pages)


@pytest.mark.parametrize("reader", READERS)
def test_her_week_and_details_ask_the_same_statements_with_check_together_shown_or_not(
    reader: str, tmp_path: pathlib.Path
) -> None:
    """Two households seeded alike but for the linked result's Status, Valid or Missing: her
    week and the details ask the same statements in the same order, generated IDs and moments
    aside, whether Check together shows or not."""
    without, plain = asked_by(reader, tmp_path / "valid", "Valid")
    shown, counted = asked_by(reader, tmp_path / "missing", "Missing")

    assert "to check together" not in plain
    assert explained(plain) == []
    assert "1 assignment to check together:" in counted
    assert explained(counted) == [explanation(DIAGRAM, "Missing")] * 2
    assert all(without.values())
    assert shown == without


# ------------------------------------------------------------- her cards, rows and details

DIAGRAM_SCORE = "School score: 7.0 / 10.0 · 70.0%"
LOG_SCORE = "School score: 18.0 / 20.0 · 90.0%"
EXPLANATION = re.compile(r'<p class="effect">Check together: .*?</p>', re.S)
PART = re.compile(
    r'<h3 class="result-title">(.*?)</h3>'
    r'|<p class="effect">((?:School score|School results|Check together).*?)</p>'
    r'|<a class="assignment-link" href="[^"]*" aria-label="([^"]*)">'
    r"(?:Grade details|See the results|Change or remove link)</a>",
    re.S,
)
BLANK_DIAGRAM = REPORT.replace(DIAGRAM_ROW, DIAGRAM_ROW.replace("| 7.0     |", "|         |"))
"""Wren's report with "Cell Diagram" reported Missing and its points left blank."""
SECOND_TERM = REPORT.replace("**T1**", "**T2**")
CANT_BE_READ = {
    "student_reports": "Your updates on this assignment can't be read right now.",
    "hand_in_events": "The hand-in record cannot be read right now.",
}
"""Each record's existing line on her card when it can't be read."""


def explanation(title: str | None, status: str, source: str | None = None) -> str:
    """An explanation as the proposal words it, for a result with ``title`` or none."""
    named = "a result" if title is None else title
    ending = "" if source is None else f", from the {source}"
    return f"Check together: the school's gradebook lists {named} as {status}{ending}."


def explained(fragment: str) -> list[str]:
    """Each Check together explanation in a piece of a page, as it reads."""
    return [said(one) for one in EXPLANATION.findall(fragment)]


def parts_of(fragment: str) -> list[str]:
    """The gradebook's parts of a piece of a page, in order: each result title, each quiet
    line as it reads, and each link to a result or results by its accessible name."""
    found = []
    for title, line, name in PART.findall(fragment):
        if title:
            found.append(f"title: {said(title)}")
        elif line:
            found.append(said(line))
        else:
            found.append(f"link: {html.unescape(name)}")
    return found


def gradebook_block(page: str) -> str:
    """Assignment details' gradebook block, from its heading to its section's end, or nothing."""
    start = page.find("From the school's gradebook</h2>")
    if start < 0:
        return ""
    head = page.rindex("<h2", 0, start)
    return page[head : page.index("</section>", start)]


def details_of(client: TestClient, assignment_id: str = DIAGRAM_ID) -> str:
    answer = client.get(f"/student/assignments/{assignment_id}", headers=PAGE_HEADERS)
    assert answer.status_code == 200
    return answer.text


def linked_from(store: ProjectStateStore, text: str, title: str, assignment_id: str) -> str:
    """The report ``text`` saved, and its result ``title`` linked to ``assignment_id``: the
    result."""
    class_id = saved(store, text)
    result_id = result_named_in(store, class_id, title, term_of(text))
    decided_in(store, class_id, result_id, assignment_id, term_of(text))
    return result_id


def term_of(text: str) -> str:
    return "T2" if "**T2**" in text else "T1"


def result_named_in(store: ProjectStateStore, class_id: str, title: str, term: str) -> str:
    (result_id,) = [
        result_id
        for result_id, value in store.class_term(class_id, term).current.results.items()
        if value.cells["assignment"][1] == title
    ]
    return result_id


def decided_in(
    store: ProjectStateStore, class_id: str, result_id: str, assignment_id: str, term: str
) -> None:
    preview = store.delete_preview(class_id, term)
    assert isinstance(preview, DeletePreview)
    assert preview.revision is not None
    outcome = store.decide_homework_link(
        result_id, assignment_id, decision="link", revision=preview.revision, role="parent"
    )
    assert isinstance(outcome, HomeworkLinkWritten), outcome


@pytest.mark.parametrize("reader", READERS)
def test_a_counted_result_with_a_score_is_explained_after_its_line_and_link(
    reader: str, tmp_path: pathlib.Path
) -> None:
    """Her card: the score line, its Grade details, then the explanation, with no link of its
    own. Details: the line, the explanation, then Grade details with the homework's title, and
    for a signed-in parent the way to change the link."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        store_of(client).put_on_record([in_week(DIAGRAM_ID, DIAGRAM)], {})
        reported(store_of(client), "done", DIAGRAM_ID)
        linked_report(store_of(client))
        card = card_for(her_week(client), DIAGRAM_ID)
        block = gradebook_block(details_of(client))

    name = f"{DIAGRAM}, {BIOLOGY}"
    assert parts_of(card) == [
        DIAGRAM_SCORE,
        f"link: Grade details: {name}",
        explanation(DIAGRAM, "Missing"),
    ]
    assert parts_of(block) == [
        DIAGRAM_SCORE,
        explanation(DIAGRAM, "Missing"),
        f"link: Grade details: {name}",
        *([f"link: Change or remove link: {name}"] if reader == "parent" else []),
    ]
    assert 'id="school-results"' not in block
    assert '<span class="authored-text">Cell Diagram</span>' in block
    assert '<span class="authored-text">Missing</span>' in block


def test_a_counted_result_with_blank_points_keeps_its_own_grade_details(week: TestClient) -> None:
    """ "Cell Diagram", Missing with its points blank, linked to "Lab Report": no score line; the
    card's explanation is followed by Grade details named for the result, and details name it
    for the homework, as with one score."""
    store = store_of(week)
    reported(store, "done", LAB_ID)
    linked_from(store, BLANK_DIAGRAM, DIAGRAM, LAB_ID)

    card = card_for(her_week(week), LAB_ID)
    block = gradebook_block(details_of(week, LAB_ID))

    assert "School score" not in card + block
    assert parts_of(card) == [
        explanation(DIAGRAM, "Missing"),
        f"link: Grade details: {DIAGRAM}, {BIOLOGY}",
    ]
    assert parts_of(block) == [
        explanation(DIAGRAM, "Missing"),
        f"link: Grade details: Lab Report, {BIOLOGY}",
    ]


def test_two_results_one_counted_are_explained_after_see_the_results(week: TestClient) -> None:
    """One homework linked to "Cell Diagram" in T1, Missing, and in T2, Valid."""
    store = store_of(week)
    reported(store, "done", DIAGRAM_ID)
    linked_from(store, REPORT, DIAGRAM, DIAGRAM_ID)
    linked_from(
        store,
        SECOND_TERM.replace(DIAGRAM_ROW, DIAGRAM_ROW.replace("Missing", "Valid  ")),
        DIAGRAM,
        DIAGRAM_ID,
    )

    card = card_for(her_week(week), DIAGRAM_ID)

    assert parts_of(card) == [
        "School results (2)",
        f"link: See the results: {DIAGRAM}, {BIOLOGY}",
        explanation(DIAGRAM, "Missing"),
    ]


def test_one_homework_with_two_counted_results_is_explained_twice_and_listed_once(
    week: TestClient,
) -> None:
    store = store_of(week)
    reported(store, "done", DIAGRAM_ID)
    linked_from(store, REPORT, DIAGRAM, DIAGRAM_ID)
    linked_from(store, SECOND_TERM, DIAGRAM, DIAGRAM_ID)

    page = her_week(week)

    assert [said(line) for line in lines_of(page)] == [
        f"1 assignment to check together: {DIAGRAM}."
    ]
    assert parts_of(card_for(page, DIAGRAM_ID)) == [
        "School results (2)",
        f"link: See the results: {DIAGRAM}, {BIOLOGY}",
        explanation(DIAGRAM, "Missing"),
        explanation(DIAGRAM, "Missing"),
    ]


def test_a_score_beside_a_counted_result_with_blank_points_keeps_both_links_apart(
    tmp_path: pathlib.Path,
) -> None:
    """ "Lab Report" linked to the log, Valid 18/20, and to "Cell Diagram", Missing with its
    points blank. Her card: the log's line and Grade details, then the explanation and its own
    Grade details. Details, in the rules' order, the diagram (due later) first, each titled, with
    the result's name on Grade details and, for a parent, the way to change each link."""
    with household_client("parent", tmp_path) as client:
        sign_in_as(client, "parent")
        store = store_of(client)
        store.put_on_record([in_week(LAB_ID, "Lab Report")], {})
        reported(store, "done", LAB_ID)
        linked_from(store, BLANK_DIAGRAM, LOG, LAB_ID)
        class_id = capture_class(store, draft_of(BLANK_DIAGRAM))
        decided_in(store, class_id, result_named_in(store, class_id, DIAGRAM, "T1"), LAB_ID, "T1")
        card = card_for(her_week(client), LAB_ID)
        block = gradebook_block(details_of(client, LAB_ID))

    assert parts_of(card) == [
        LOG_SCORE,
        f"link: Grade details: Lab Report, {BIOLOGY}",
        explanation(DIAGRAM, "Missing"),
        f"link: Grade details: {DIAGRAM}, {BIOLOGY}",
    ]
    assert 'id="school-results"' in block
    assert parts_of(block) == [
        f"title: {DIAGRAM}",
        explanation(DIAGRAM, "Missing"),
        f"link: Grade details: {DIAGRAM}, {BIOLOGY}",
        f"link: Change or remove link: {DIAGRAM}, {BIOLOGY}",
        f"title: {LOG}",
        LOG_SCORE,
        f"link: Grade details: {LOG}, {BIOLOGY}",
        f"link: Change or remove link: {LOG}, {BIOLOGY}",
    ]


def test_twins_with_blank_points_each_keep_their_own_grade_details(week: TestClient) -> None:
    """ "Cell Diagram" in T1 and in T2, each Missing with its points blank: two explanations,
    each followed by its own Grade details, their accessible names alike."""
    store = store_of(week)
    reported(store, "done", DIAGRAM_ID)
    linked_from(store, BLANK_DIAGRAM, DIAGRAM, DIAGRAM_ID)
    linked_from(store, BLANK_DIAGRAM.replace("**T1**", "**T2**"), DIAGRAM, DIAGRAM_ID)

    card = card_for(her_week(week), DIAGRAM_ID)
    hrefs = re.findall(r'href="([^"]*)" aria-label="Grade details', card)

    assert parts_of(card) == [
        explanation(DIAGRAM, "Missing"),
        f"link: Grade details: {DIAGRAM}, {BIOLOGY}",
        explanation(DIAGRAM, "Missing"),
        f"link: Grade details: {DIAGRAM}, {BIOLOGY}",
    ]
    assert len(set(hrefs)) == 2


def test_an_untitled_result_is_explained_as_a_result(week: TestClient) -> None:
    """The diagram's title cell is left blank in the report."""
    store = store_of(week)
    reported(store, "done", LAB_ID)
    untitled = REPORT.replace("| Cell Diagram             |", "|                          |")
    class_id = saved(store, untitled)
    (result_id,) = [
        one
        for one, value in store.class_term(class_id, "T1").current.results.items()
        if value.cells["status"][1] == "Missing"
    ]
    decided_in(store, class_id, result_id, LAB_ID, "T1")

    assert explained(card_for(her_week(week), LAB_ID)) == [explanation(None, "Missing")]


def test_the_report_that_supplied_the_status_is_named_on_the_line_and_the_explanation(
    week: TestClient,
) -> None:
    """Report A: Missing 7/10; a newer report B shows it Valid 9/10, left unticked."""
    store = store_of(week)
    reported(store, "done", DIAGRAM_ID)
    class_id, result_id = linked_report(store)
    newer = draft_of(VALID_NINE)
    review = store.review_grade_report(newer, capture_key(newer), key=KEY)
    (changed,) = [item.key for item in review.rows if item.result_id == result_id]
    unticked = save_grade(store, newer, key=KEY, review=review, selection=review.ready - {changed})
    assert isinstance(unticked, GradeReportSaved)

    page = her_week(week)
    block = gradebook_block(details_of(week))

    source = "report added August 19"
    assert parts_of(card_for(page, DIAGRAM_ID)) == [
        f"{DIAGRAM_SCORE}, from the {source}",
        f"link: Grade details: {DIAGRAM}, {BIOLOGY}",
        explanation(DIAGRAM, "Missing", source),
    ]
    assert explained(block) == [explanation(DIAGRAM, "Missing", source)]


def test_turned_in_earlier_work_is_explained_on_its_row_and_not_in_the_line(
    week: TestClient,
) -> None:
    store = store_of(week)
    store.put_on_record([in_week(OTHER_ID, "Lab Notebook", date(2026, 8, 12))], {})
    incomplete = REPORT.replace(DIAGRAM_ROW, DIAGRAM_ROW.replace("Missing   ", "Incomplete"))
    linked_from(store, incomplete, DIAGRAM, OTHER_ID)
    handed(store, TURNED_IN, OTHER_ID)

    page = her_week(week)
    row = re.search(
        r'<li id="earlier-[^"]*" tabindex="-1">\s*<strong>Lab Notebook</strong>.*?</li>',
        page,
        re.S,
    )

    assert row is not None
    assert explained(row.group(0)) == [explanation(DIAGRAM, "Incomplete")]
    assert lines_of(page) == []


@pytest.mark.parametrize("failure", FAILURES)
def test_a_covered_failure_leaves_no_explanation_anywhere(week: TestClient, failure: str) -> None:
    store = store_of(week)
    reported(store, "done", DIAGRAM_ID)
    _, result_id = linked_report(store)
    assert explained(her_week(week)) == [explanation(DIAGRAM, "Missing")]
    FAILURES[failure](week, result_id)

    assert explained(her_week(week)) == []
    assert explained(details_of(week)) == []
    assert gradebook_block(details_of(week)) == ""


@pytest.mark.parametrize(
    ("update", "hand_in", "spoiled"),
    [
        pytest.param("done", TURNED_IN, ("student_reports", "hand_in_events"), id="both"),
        pytest.param("not_yet", TURNED_IN, ("hand_in_events",), id="hand-in, updates not yet"),
        pytest.param("done", NEEDS_HAND_IN, ("student_reports",), id="updates, to hand in"),
    ],
)
def test_a_homework_left_unknown_keeps_its_can_t_be_read_line_and_no_explanation(
    week: TestClient, update: str, hand_in: str, spoiled: tuple[str, ...]
) -> None:
    """The named tables' notes are changed to bytes that are not UTF-8 by the test's own SQL."""
    store = store_of(week)
    linked_report(store)
    reported(store, update, DIAGRAM_ID)
    handed(store, hand_in)
    for table in spoiled:
        spoil(store, table, "note", NOT_UTF8, DIAGRAM_ID)

    card = card_for(her_week(week), DIAGRAM_ID)
    block = gradebook_block(details_of(week))

    for table in spoiled:
        assert CANT_BE_READ[table] in card
    assert explained(card) == []
    assert explained(block) == []
    assert parts_of(card)[:2] == [DIAGRAM_SCORE, f"link: Grade details: {DIAGRAM}, {BIOLOGY}"]


# ------------------------------------------------------------- Grades' reads and counts

TERM = (YEAR, "T1")
SECOND = (YEAR, "T2")
FOUR_MISSING = (
    REPORT.replace(LOG_ROW, LOG_ROW.replace("Valid  ", "Missing"))
    .replace(
        "| Microscope Practice                | 27.0    | 30.0    | 90.0    | Valid      |",
        "| Microscope Practice                | 27.0    | 30.0    | 90.0    | Missing    |",
    )
    .replace(
        "| Osmosis with Potato Slices         | 31.0    | 40.0    | 77.5    | Valid      |",
        "| Osmosis with Potato Slices         | 31.0    | 40.0    | 77.5    | Missing    |",
    )
)
"""Wren's report with each of its four Biology results reported Missing."""
CHECKS_LEFT_OUT = "Grades shows no Check together count: "


def on_grades(store: ProjectStateStore, term: tuple[str, str] = TERM) -> GradesRead:
    """Grades' reads for ``term``, as her tree reads them."""
    return grades_read(store, term, "/student/grades", ZONE)


def listed(read: GradesRead) -> dict[str, list[str]]:
    """Each class's homework to check together on Grades, by title, in the listed order."""
    return {class_id: [one.title for one in ones] for class_id, ones in read.to_check.lists.items()}


def records(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        one.getMessage()
        for one in caplog.records
        if one.name.startswith("blossom") and one.levelno >= logging.WARNING
    ]


def test_grades_lists_her_done_homework_linked_to_a_missing_result_under_its_class(
    store: ProjectStateStore,
) -> None:
    """Grades' class rows are as they read alone, and the counted homework is listed under the
    class of the open term its result is in."""
    reported(store, "done", DIAGRAM_ID)
    class_id, _ = linked_report(store)

    read = on_grades(store)

    assert read.rows == class_rows(store, TERM, "/student/grades", ZONE)
    assert read.to_check.unknown is False
    assert listed(read) == {class_id: [DIAGRAM]}
    (one,) = read.to_check.lists[class_id]
    assert (one.assignment_id, one.course, one.title, one.due_date) == (
        DIAGRAM_ID,
        BIOLOGY,
        DIAGRAM,
        date(2026, 9, 18),
    )


@pytest.mark.parametrize(
    ("update", "hand_in", "counted"),
    [
        pytest.param(None, TURNED_IN, True, id="turned in alone"),
        pytest.param("done", NEEDS_HAND_IN, True, id="done, still to hand in"),
        pytest.param("not_yet", None, False, id="not yet"),
        pytest.param(None, NOT_REQUIRED, False, id="nothing to turn in"),
        pytest.param(None, None, False, id="nothing said"),
    ],
)
def test_grades_counts_by_her_done_or_turned_in_read_literally(
    store: ProjectStateStore, update: str | None, hand_in: str | None, counted: bool
) -> None:
    class_id, _ = linked_report(store)
    if update is not None:
        reported(store, update, DIAGRAM_ID)
    if hand_in is not None:
        handed(store, hand_in)

    read = on_grades(store)

    assert read.to_check.unknown is False
    assert listed(read) == ({class_id: [DIAGRAM]} if counted else {})


def test_a_homework_counted_in_two_terms_is_listed_once_in_each_term_s_row(
    store: ProjectStateStore,
) -> None:
    """ "Cell Diagram" linked to its result in T1 and in T2, both Missing: one entry in the
    row of whichever term is open."""
    reported(store, "done", DIAGRAM_ID)
    class_id, _ = linked_report(store)
    second = saved(store, SECOND_TERM)
    decided_in(store, second, result_named_in(store, second, DIAGRAM, "T2"), DIAGRAM_ID, "T2")

    assert second == class_id
    assert listed(on_grades(store)) == {class_id: [DIAGRAM]}
    assert listed(on_grades(store, SECOND)) == {class_id: [DIAGRAM]}


def test_a_result_counted_in_another_term_is_not_in_the_open_term_s_lists(
    store: ProjectStateStore,
) -> None:
    """Only T2 holds the linked Missing result: T1 lists nothing, T2 lists it."""
    reported(store, "done", DIAGRAM_ID)
    saved(store, VALID)
    class_id = saved(store, SECOND_TERM)
    decided_in(store, class_id, result_named_in(store, class_id, DIAGRAM, "T2"), DIAGRAM_ID, "T2")

    assert listed(on_grades(store)) == {}
    assert listed(on_grades(store, SECOND)) == {class_id: [DIAGRAM]}


@pytest.mark.parametrize(
    ("update", "hand_in", "spoiled"),
    [
        pytest.param("done", TURNED_IN, ("student_reports", "hand_in_events"), id="both"),
        pytest.param("not_yet", TURNED_IN, ("hand_in_events",), id="hand-in, updates not yet"),
        pytest.param("done", NEEDS_HAND_IN, ("student_reports",), id="updates, to hand in"),
    ],
)
def test_a_homework_left_unknown_leaves_every_grades_count_unknown(
    store: ProjectStateStore, update: str, hand_in: str, spoiled: tuple[str, ...]
) -> None:
    """The named tables' notes are changed to bytes that are not UTF-8 by the test's own SQL.
    A counted homework beside it lists nothing either."""
    linked_report(store)
    reported(store, update, DIAGRAM_ID)
    handed(store, hand_in)
    for table in spoiled:
        spoil(store, table, "note", NOT_UTF8, DIAGRAM_ID)

    read = on_grades(store)

    assert read.to_check.unknown is True
    assert read.to_check.lists == {}


def test_grades_own_class_read_fails_as_itself_and_the_new_reads_never_run(
    store: ProjectStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A result's points presence set to text no save writes, by the test's own SQL, alone and
    with a second confirmed link naming the result stored the same way."""
    reported(store, "done", DIAGRAM_ID)
    _, result_id = linked_report(store)
    asked: list[str] = []

    def links() -> tuple[()]:
        asked.append("links")
        return ()

    monkeypatch.setattr(store, "homework_links", links)
    with store._lock, store._connection:
        store._connection.execute(
            "UPDATE grade_result_observations SET points_presence = 'smudged' WHERE result_id = ?",
            (result_id,),
        )

    with pytest.raises(GradeValueUndecodable):
        on_grades(store)
    with store._lock, store._connection:
        store._connection.execute("DROP INDEX grade_homework_links_one_homework")
        store._connection.execute(
            LINK_ROW, (store.student_id(), result_id, OTHER_ID, "2026-08-19T12:00:00+00:00")
        )
    with pytest.raises(GradeValueUndecodable) as raised:
        on_grades(store)

    assert type(raised.value) is GradeValueUndecodable
    assert asked == []


@pytest.mark.parametrize("failure", ["undecodable link", "conflicting confirmations"])
def test_a_covered_failure_in_the_new_reads_leaves_every_count_unknown_with_one_record(
    store: ProjectStateStore, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    """A link's homework ID stored as bytes that are not UTF-8, holding the word ZEBRA, or a
    second confirmed link naming the result with the index dropped, each by the test's own SQL.
    Grades' rows are as they read alone; no count, one record naming the type, no ZEBRA."""
    reported(store, "done", DIAGRAM_ID)
    _, result_id = linked_report(store)
    rows = class_rows(store, TERM, "/student/grades", ZONE)
    with store._lock, store._connection:
        if failure == "undecodable link":
            store._connection.execute(
                "UPDATE grade_homework_links SET assignment_id = CAST(X'5A45425241FF80' AS TEXT)"
            )
        else:
            store._connection.execute("DROP INDEX grade_homework_links_one_homework")
            store._connection.execute(
                LINK_ROW, (store.student_id(), result_id, OTHER_ID, "2026-08-19T12:00:00+00:00")
            )
    with caplog.at_level(logging.WARNING, logger="blossom"):
        read = on_grades(store)

    named = "GradeValueUndecodable" if failure == "undecodable link" else "HomeworkLinksConflict"
    assert read.rows == rows
    assert read.to_check.unknown is True
    assert read.to_check.lists == {}
    assert records(caplog) == [CHECKS_LEFT_OUT + named]
    assert "ZEBRA" not in caplog.text


def test_a_linked_homework_row_that_can_t_be_read_leaves_every_count_unknown(
    store: ProjectStateStore, caplog: pytest.LogCaptureFixture
) -> None:
    """ "Cell Diagram"'s title changed to bytes that are not UTF-8 by the test's own SQL: its
    class name and title are unknown, so the count is. The read names the row apart, and
    Grades writes no record of its own."""
    reported(store, "done", DIAGRAM_ID)
    linked_report(store)
    spoil(store, "assignments", "title", NOT_UTF8, DIAGRAM_ID)
    with caplog.at_level(logging.WARNING, logger="blossom"):
        read = on_grades(store)

    assert read.to_check.unknown is True
    assert read.to_check.lists == {}
    assert records(caplog) == [
        f"the homework record of {DIAGRAM_ID} cannot be read (UnicodeDecodeError)"
    ]


def test_an_unreadable_homework_row_no_link_names_is_never_read(
    store: ProjectStateStore, caplog: pytest.LogCaptureFixture
) -> None:
    """ "Lab Notebook"'s title changed to bytes that are not UTF-8 by the test's own SQL; no
    link names it. Grades counts as with no such row, and nothing is recorded."""
    reported(store, "done", DIAGRAM_ID)
    class_id, _ = linked_report(store)
    spoil(store, "assignments", "title", NOT_UTF8, OTHER_ID)
    with caplog.at_level(logging.WARNING, logger="blossom"):
        read = on_grades(store)

    assert listed(read) == {class_id: [DIAGRAM]}
    assert records(caplog) == []


def test_a_linked_row_that_can_t_be_read_beside_a_result_that_never_counts_changes_nothing(
    store: ProjectStateStore,
) -> None:
    """ "Cell Diagram"'s title changed to bytes that are not UTF-8 by the test's own SQL, its
    link naming a result the school reports Valid: no count could hold it."""
    reported(store, "done", DIAGRAM_ID)
    class_id = saved(store, VALID)
    decided(store, class_id, result_named(store, class_id))
    spoil(store, "assignments", "title", NOT_UTF8, DIAGRAM_ID)

    read = on_grades(store)

    assert read.to_check.unknown is False
    assert read.to_check.lists == {}


def spied(asked: list[str], name: str, real: Callable[..., object]) -> Callable[..., object]:
    """``real``, noting ``name`` in ``asked`` each time it is called."""

    def call(*args: object) -> object:
        asked.append(name)
        return real(*args)

    return call


def test_with_no_report_in_the_open_term_nothing_more_is_asked(
    store: ProjectStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report of T2 alone, read with T1 open: the class rows only. With T2 open, the new
    reads are asked, in order."""
    reported(store, "done", DIAGRAM_ID)
    saved(store, SECOND_TERM)
    asked: list[str] = []
    for name in ("homework_links", "homework_classes", "homework_readings", "hand_in_readings"):
        monkeypatch.setattr(store, name, spied(asked, name, getattr(store, name)))

    read = on_grades(store)

    assert read.rows == class_rows(store, TERM, "/student/grades", ZONE)
    assert read.to_check.unknown is False
    assert read.to_check.lists == {}
    assert asked == []
    assert on_grades(store, SECOND).rows
    assert asked == ["homework_links", "homework_classes", "homework_readings", "hand_in_readings"]


def test_g3c_i2_grades_never_counts_a_result_reported_valid_or_a_pair_kept_apart(
    store: ProjectStateStore,
) -> None:
    reported(store, "done", DIAGRAM_ID)
    class_id = saved(store, VALID)
    result_id = result_named(store, class_id)
    decided(store, class_id, result_id)
    with_valid = on_grades(store)
    saved(store)
    with_missing = on_grades(store)
    decided(store, class_id, result_id, decision="apart")
    kept_apart = on_grades(store)

    assert listed(with_valid) == {}
    assert listed(with_missing) == {class_id: [DIAGRAM]}
    assert listed(kept_apart) == {}
    assert not any(one.to_check.unknown for one in (with_valid, with_missing, kept_apart))


def test_grades_lists_counted_homework_in_the_record_s_order(store: ProjectStateStore) -> None:
    """Four counted homework in Biology: two due on one day under the class's name and its
    report code, one due earlier, one undated. Listed by due date, then class name, then
    title, the undated one last."""
    made = [
        (BIOLOGY, "assignment-zeta", "Zeta", date(2026, 9, 20)),
        ("07 BIO - C", "assignment-alpha", "Alpha", date(2026, 9, 20)),
        (BIOLOGY, "assignment-early", "Early", date(2026, 9, 10)),
        (BIOLOGY, "assignment-undated", "Undated", None),
    ]
    store.put_on_record(
        [
            homework_named(course, assignment_id, title).model_copy(update={"due_date": due})
            for course, assignment_id, title, due in made
        ],
        {},
    )
    class_id = saved(store, FOUR_MISSING)
    pairs = {
        "Seed Germination Log": "assignment-zeta",
        "Cell Diagram": "assignment-alpha",
        "Microscope Practice": "assignment-early",
        "Osmosis with Potato Slices": "assignment-undated",
    }
    for title, assignment_id in pairs.items():
        decided(store, class_id, result_named(store, class_id, title), assignment_id)
        reported(store, "done", assignment_id)

    assert listed(on_grades(store)) == {class_id: ["Early", "Alpha", "Zeta", "Undated"]}


def test_g3c_i1_grades_reads_write_nothing(week: TestClient, tmp_path: pathlib.Path) -> None:
    store = store_of(week)
    reported(store, "done", DIAGRAM_ID)
    linked_report(store)
    world = closed_world(household_files(tmp_path), leaving_out=())

    first, second = on_grades(store), on_grades(store)

    assert first == second
    assert first.to_check.lists
    assert closed_world(household_files(tmp_path), leaving_out=()) == world


@pytest.mark.parametrize(
    "error", [ValueError("synthetic"), sqlite3.OperationalError("disk I/O error")]
)
def test_g3c_i9_the_guard_catches_nothing_wider(
    store: ProjectStateStore, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """A failure of the homework read that is no grade value's raises as itself."""
    reported(store, "done", DIAGRAM_ID)
    linked_report(store)

    def failing(*_: object) -> None:
        raise error

    monkeypatch.setattr(store, "homework_readings", failing)

    with pytest.raises(type(error)):
        on_grades(store)


# ------------------------------------------------------------- Grades' page

GRADE_PAGES = {"her": "/student/grades", "parent": "/parent/grades"}
CLASS_ROW_OR_PROBLEM = re.compile(
    r'<section class="panel grade-card"[^>]*>(.*?)</section>|'
    r'<p class="problem">Check together can\'t be worked out right now\.</p>',
    re.S,
)
ROW_PART = re.compile(
    r"<h3[^>]*>(.*?)</h3>"
    r'|<p(?! class="details-link")[^>]*>(.*?)</p>'
    r"|<a\b([^>]*)>(.*?)</a>",
    re.S,
)
"""A part of a class row: its heading, a line, or a link, in the page's order."""
TERM_GRADE = "School-reported grade 81.9% · B-"
SUPPLIED = "Report added August 19"
NO_TERM_GRADE = "\n".join(line for line in REPORT.split("\n") if "**Term Grade**" not in line)
"""Wren's report with no term grade row, so its current report supplies no term grade."""


def grades_of(reader: str, client: TestClient) -> str:
    answer = client.get(GRADE_PAGES[reader], headers=PAGE_HEADERS)
    assert answer.status_code == 200
    return answer.text


def grade_rows(page: str) -> list[object]:
    """Grades' class rows in order, each as its parts: the name, each line as it reads and each
    link by its accessible name or words, with the line that the count can't be worked out
    where it stands."""
    found: list[object] = []
    for one in CLASS_ROW_OR_PROBLEM.finditer(page):
        row = one.group(1)
        if row is None:
            found.append(CANT_WORK_OUT)
            continue
        parts = []
        for heading, line, attributes, words in ROW_PART.findall(row):
            if heading:
                parts.append(said(heading))
            elif attributes or words:
                name = re.search(r'aria-label="([^"]*)"', attributes)
                parts.append(f"link: {html.unescape(name.group(1)) if name else said(words)}")
            else:
                parts.append(said(line))
        found.append(parts)
    return found


@pytest.mark.parametrize("reader", ["her", "parent"])
def test_grades_shows_check_together_with_its_list_after_the_term_grade_and_before_see(
    reader: str, tmp_path: pathlib.Path
) -> None:
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        store = store_of(client)
        store.put_on_record([in_week(DIAGRAM_ID, DIAGRAM)], {})
        reported(store, "done", DIAGRAM_ID)
        linked_report(store)
        page = grades_of(reader, client)
        (href,) = re.findall(r'href="([^"]*)" aria-label="[^"]*: to check together"', page)
        details = client.get(html.unescape(href), headers=PAGE_HEADERS)

    (row,) = grade_rows(page)
    assert row == [
        BIOLOGY,
        TERM_GRADE,
        SUPPLIED,
        "Check together (1)",
        f"link: {DIAGRAM}, {BIOLOGY}: to check together",
        f"link: See {BIOLOGY}",
    ]
    assert html.unescape(href) == f"/student/assignments/{DIAGRAM_ID}"
    assert details.status_code == 200
    back = "Back to family review" if reader == "parent" else "Back to the week"
    assert re.search(rf'<p class="return"><a href="[^"]*">{back}</a>', details.text)
    assert CANT_WORK_OUT not in page


def test_a_row_with_no_term_grade_shows_check_together_before_see(week: TestClient) -> None:
    """The open household's Grades, its one report holding no term grade row."""
    store = store_of(week)
    reported(store, "done", DIAGRAM_ID)
    class_id = saved(store, NO_TERM_GRADE)
    decided(store, class_id, result_named(store, class_id))

    (row,) = grade_rows(grades_of("her", week))

    assert row == [
        BIOLOGY,
        "Check together (1)",
        f"link: {DIAGRAM}, {BIOLOGY}: to check together",
        f"link: See {BIOLOGY}",
    ]


def test_grades_with_nothing_to_check_shows_no_count_and_no_problem(week: TestClient) -> None:
    store = store_of(week)
    reported(store, "not_yet", DIAGRAM_ID)
    linked_report(store)

    page = grades_of("her", week)

    assert "Check together" not in page
    assert grade_rows(page) == [[BIOLOGY, TERM_GRADE, SUPPLIED, f"link: See {BIOLOGY}"]]


def link_index_dropped(client: TestClient, result_id: str) -> None:
    """The links index is dropped and a second confirmed link naming the result is stored for
    "Lab Report", by the test's own SQL."""
    conflicting(client, result_id)


def link_not_utf8(client: TestClient, _: str) -> None:
    """Each link's homework ID stored as bytes that are not UTF-8, by the test's own SQL."""
    store = store_of(client)
    with store._lock, store._connection:
        store._connection.execute(
            "UPDATE grade_homework_links SET assignment_id = CAST(X'5A45425241FF80' AS TEXT)"
        )


def homework_row_not_utf8(client: TestClient, _: str) -> None:
    spoil(store_of(client), "assignments", "title", NOT_UTF8, DIAGRAM_ID)


GRADES_FAILURES = {
    "undecodable link": (link_not_utf8, "GradeValueUndecodable"),
    "conflicting confirmations": (link_index_dropped, "HomeworkLinksConflict"),
    "unreadable homework row": (homework_row_not_utf8, None),
    "unknown homework": (made_unreadable, None),
}


@pytest.mark.parametrize("failure", GRADES_FAILURES)
@pytest.mark.parametrize("reader", ["her", "parent"])
def test_grades_with_a_count_it_can_t_work_out_is_the_ordinary_page_with_c4_once(
    reader: str, failure: str, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Each failure is written by the test's own SQL, as its helper says. The page is Grades as
    with no link, but for the one line before the first class row; a covered failure writes one
    record naming its type, and nothing else is recorded but the homework read's own record."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        store = store_of(client)
        store.put_on_record(
            [in_week(LAB_ID, "Lab Report", date(2026, 8, 19)), in_week(DIAGRAM_ID, DIAGRAM)], {}
        )
        reported(store, "done", DIAGRAM_ID)
        class_id = saved(store)
        plain = grades_of(reader, client)
        result_id = result_named(store, class_id)
        decided(store, class_id, result_id)
        change, named = GRADES_FAILURES[failure]
        change(client, result_id)
        with caplog.at_level(logging.WARNING, logger="blossom"):
            page = grades_of(reader, client)

    rows = grade_rows(page)
    assert rows[0] == CANT_WORK_OUT
    assert rows[1:] == grade_rows(plain)
    assert page.count(CANT_WORK_OUT) == 1
    assert "Check together (" not in page
    assert page.replace(f'\n      <p class="problem">{CANT_WORK_OUT}</p>', "") == plain
    if named is not None:
        assert records(caplog) == [CHECKS_LEFT_OUT + named]
    elif failure == "unreadable homework row":
        assert records(caplog) == [
            f"the homework record of {DIAGRAM_ID} cannot be read (UnicodeDecodeError)"
        ]
    assert "ZEBRA" not in caplog.text


def view_refused(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Answer:
    return client.post("/student/grades/view", data={"view": "not a term"}, headers=PAGE_HEADERS)


def view_not_on_record(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Answer:
    def refused(*_: object) -> None:
        raise ViewNotOnRecord

    monkeypatch.setattr(store_of(client), "choose_view", refused)
    return client.post("/student/grades/view", data={"view": "2026-2027 T1"}, headers=PAGE_HEADERS)


def current_refused(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Answer:
    return client.post("/parent/grades/current-term", data={"shown": "x"}, headers=PAGE_HEADERS)


def current_failing(error: Exception) -> Callable[[TestClient, pytest.MonkeyPatch], Answer]:
    def answer(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Answer:
        def failing(*_: object) -> None:
            raise error

        monkeypatch.setattr(store_of(client), "set_current_context", failing)
        return client.post(
            "/parent/grades/current-term",
            data={"shown": "2026-2027 T1", "term": "2026-2027 T1"},
            headers=PAGE_HEADERS,
        )

    return answer


def current_changed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Answer:
    monkeypatch.setattr(
        store_of(client), "set_current_context", lambda *_: ContextChanged(("2026-2027", "T2"))
    )
    return client.post(
        "/parent/grades/current-term",
        data={"shown": "2026-2027 T1", "term": "2026-2027 T1"},
        headers=PAGE_HEADERS,
    )


CALLERS = {
    "her view refused": ("her", view_refused, 422),
    "her view not on record": ("her", view_not_on_record, 422),
    "current term refused": ("parent", current_refused, 422),
    "current term not on record": ("parent", current_failing(TermNotOnRecord()), 422),
    "current term not saved": ("parent", current_failing(GradeReportNotSaved("no")), 500),
    "current term lost": ("parent", current_failing(GradeTransactionLost("lost")), 500),
    "current term changed": ("parent", current_changed, 409),
}


@pytest.mark.parametrize("failure", ["undecodable link", "conflicting confirmations"])
@pytest.mark.parametrize("caller", CALLERS)
def test_every_answer_grades_gives_shows_what_its_get_shows(
    caller: str, failure: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each answer of the term forms, with a covered failure stored as its helper says: its
    class rows, and the line that the count can't be worked out, are those of that reader's
    own read of Grades."""
    reader, answered, code = CALLERS[caller]
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        store = store_of(client)
        store.put_on_record(
            [in_week(LAB_ID, "Lab Report", date(2026, 8, 19)), in_week(DIAGRAM_ID, DIAGRAM)], {}
        )
        reported(store, "done", DIAGRAM_ID)
        _, result_id = linked_report(store)
        GRADES_FAILURES[failure][0](client, result_id)
        read = grades_of(reader, client)
        answer = answered(client, monkeypatch)

    assert answer.status_code == code
    assert grade_rows(read)[0] == CANT_WORK_OUT
    assert grade_rows(answer.text) == grade_rows(read)
