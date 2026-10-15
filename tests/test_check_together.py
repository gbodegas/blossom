# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Check together: each homework's standing from her two accounts, what one read of the
record and the gradebook counts, and her week's line.

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
from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient

from blossom.grades.draft import GradeReportDraft, Presence, capture_key
from blossom.grades.identity import name_form_key
from blossom.grades.projection import MadeCurrent
from blossom.grades.review import GradeReportSaved
from blossom.grades.text_reader import read_grade_report
from blossom.hand_in import NEEDS_HAND_IN, NOT_REQUIRED, TURNED_IN, UNKNOWN
from blossom.noticing import canonical_active_input, planning_digest, read_everything, week_from
from blossom.reconciliation import SourceChannel
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
    DeletePreview,
    HomeworkLinkWritten,
    LinkDecision,
)
from blossom.stores.project_state import Assignment, AssignmentKind, ProjectStateStore
from tests.support import (
    FIXTURES,
    HER_PAGE,
    NOT_UTF8,
    NOW,
    PAGE_HEADERS,
    PLAN_DATE,
    capture_class,
    closed_world,
    confirm_current,
    database_of,
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
    store: ProjectStateStore, tmp_path: pathlib.Path
) -> None:
    linked_report(store)
    reported(store, "done", DIAGRAM_ID)
    handed(store, TURNED_IN)
    world = closed_world([tmp_path / "blossom.sqlite3"], leaving_out=())

    first, second = checks_in(store), checks_in(store)

    assert first == second
    assert first[DIAGRAM_ID][0] is COUNTED
    assert closed_world([tmp_path / "blossom.sqlite3"], leaving_out=()) == world


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
    after = lines_of(her_week(week))

    assert [said(line) for line in before] == [f"1 assignment to check together: {DIAGRAM}."]
    assert "Worth checking together" in family
    assert f'aria-label="Mark checked: {DIAGRAM}"' in family
    assert marked.status_code == 303
    assert status_of(store, DIAGRAM_ID).checked
    assert after == before


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


def test_g3c_i1_her_week_with_the_line_or_the_problem_writes_nothing(week: TestClient) -> None:
    store = store_of(week)
    lab_missing_by_email(week)
    reported(store, "done", DIAGRAM_ID)
    _, result_id = linked_report(store)
    world = closed_world([database_of(week)], leaving_out=())
    counted = lines_of(her_week(week))
    after_counted = closed_world([database_of(week)], leaving_out=())
    undecodable(week, result_id)
    failed_world = closed_world([database_of(week)], leaving_out=())
    unknown = lines_of(her_week(week))

    assert [said(line) for line in counted] == [
        "2 assignments to check together: Lab Report, Cell Diagram."
    ]
    assert after_counted == world
    assert unknown[-1] == CANT_WORK_OUT_LINE
    assert closed_world([database_of(week)], leaving_out=()) == failed_world
