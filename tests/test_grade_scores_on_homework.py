# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The school score on her homework pages: which assignment shows a result's line, where, and
the way from it to class details.

Each case saves a report through the gradebook's own save and reads her pages as a browser
does. The line is the one class details writes for the same result, the link goes to that
result in its own term on the reader's tree, and showing it stores nothing and moves nothing.
"""

import dataclasses
import difflib
import functools
import html
import json
import logging
import pathlib
import re
import sqlite3
import sys
import threading
import types
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import date, time
from time import monotonic
from typing import Any, Protocol

import pytest
from fastapi.testclient import TestClient

from blossom.grades.dates import due_date_of
from blossom.grades.draft import GradeReportDraft, GradeValue, Presence, capture_key, folded
from blossom.grades.identity import name_form_key
from blossom.grades.projection import MadeCurrent, original_of
from blossom.grades.review import (
    CurrentValue,
    GradeReportSaved,
    GradeValueUndecodable,
    HomeworkLinksConflict,
    MatchAnswer,
    ReportAt,
)
from blossom.grades.school_scores import (
    Homework,
    Link,
    Result,
    ScoreAt,
    Waiting,
    candidate_homework,
    school_scores,
    waiting_results,
)
from blossom.grades.text_reader import read_grade_report
from blossom.grades.wording import report_names, score_line, spoken_day
from blossom.hand_in import NEEDS_HAND_IN, TURNED_IN
from blossom.noticing import canonical_active_input, planning_digest, read_everything, week_from
from blossom.plans import PlanBlock
from blossom.reconciliation import SourceChannel
from blossom.routes.grades import result_said, row_shown
from blossom.routes.homework_scores import (
    HomeworkRow,
    PairStanding,
    ResultHomework,
    RuleValues,
    ScoreShown,
    Withheld,
    homework_of,
    read_grade_facts,
    read_score_facts,
    result_homework,
    results_read,
    rule_values,
    scores_shown,
)
from blossom.routes.navigation import (
    NEW_NOTE_PAGE,
    NOTE_ACTIONS,
    assignment_anchor,
    note_add_action,
    note_add_href,
)
from blossom.routes.runs import plan_graphs
from blossom.stores import gradebook
from blossom.stores.gradebook import (
    GRADEBOOK_TABLES,
    UNCONNECTED,
    ClassOfYear,
    DeletePreview,
    GradeReportNotSaved,
    GradeTransactionLost,
    HomeworkLinkWritten,
    LinkDecision,
    _cells,
    _copied,
    _month,
    _utf8,
    _whole,
    class_for,
    name_key,
)
from blossom.stores.project_state import Assignment, AssignmentKind, ProjectStateStore
from blossom.unreadable import text_or_refusal
from tests.support import (
    FIXTURES,
    HER_PAGE,
    PAGE_HEADERS,
    ZONE,
    accepting,
    as_stored,
    browser,
    capture_class,
    card_for,
    closed_world,
    confirm_current,
    correction_page,
    current_preview,
    database_of,
    every_row,
    fixture_week_plan,
    form_fields,
    grade_answers,
    household_client,
    page_of,
    plan_on,
    planned,
    record,
    reported,
    save_grade,
    school_said,
    scripted_graphs,
    sign_in_as,
    store_of,
    unreadable_claim,
    whole_form,
    without_minted_fields,
)

REPORT = (FIXTURES / "grade_report.md").read_text(encoding="utf-8")
KEY = name_form_key(b"5" * 64)
YEAR = "2026-2027"
BIOLOGY = "Biology"
LOG, DIAGRAM, PRACTICE, OSMOSIS = (
    "Seed Germination Log",
    "Cell Diagram",
    "Microscope Practice",
    "Osmosis with Potato Slices",
)
LOG_ID, DIAGRAM_ID, PRACTICE_ID, OSMOSIS_ID = (
    "assignment-seed-log",
    "assignment-cell-diagram",
    "assignment-microscope",
    "assignment-osmosis",
)
THURSDAY = date(2026, 8, 20)
MONDAY = date(2026, 8, 17)
IN_WEEK = (
    REPORT.replace("09/22", "08/20")
    .replace("09/26", "08/27")
    .replace("09/24", "08/12")
    .replace("10/02", "08/01")
)
"""The report with each result due around the pinned day: the log in its week, the diagram a
week later, the practice a week before and the osmosis lab more than two weeks before."""
LOG_ROW = "| Seed Germination Log | 18.0    | 20.0    | 90.0    |"
LINE = "School score: 18.0 / 20.0 · 90.0%"
DIAGRAM_LINE = "School score: 7.0 / 10.0 · 70.0%"
HEADING = "From the school's gradebook"
SCORE = re.compile(
    r'<p class="effect">(School score[^<]*)</p>\s*<p class="details-link">'
    r'<a class="assignment-link" href="([^"]*)" aria-label="([^"]*)">'
    r"Grade details</a></p>"
)
"""A score line with the link under it in a paragraph of its own, as each place writes them."""
Shown = list[tuple[str, str, str]]


def draft_of(text: str) -> GradeReportDraft:
    reading = read_grade_report(text)
    assert reading.draft is not None, reading.not_read
    return reading.draft


def homework(
    assignment_id: str = LOG_ID,
    title: str = LOG,
    on: date | None = THURSDAY,
    *,
    course: str = BIOLOGY,
    assigned: date | None = None,
    kind: AssignmentKind = AssignmentKind.HOMEWORK,
) -> Assignment:
    return Assignment(
        assignment_id=assignment_id,
        course=course,
        title=title,
        due_date=on,
        dependencies=[],
        reported_submission_status="not_started",
        assigned_on=assigned,
        kind=kind,
    )


def with_report(client: TestClient, text: str = IN_WEEK, *, complete: bool = False) -> str:
    """A parent's save of the report ``text``, and the class it went into."""
    store, draft = store_of(client), draft_of(text)
    outcome = save_grade(store, draft, key=KEY, complete=complete)
    assert isinstance(outcome, GradeReportSaved), outcome
    return capture_class(store, draft)


def scores(fragment: str) -> Shown:
    """Each score line in a piece of a page, with its link's address and accessible name."""
    return [
        (html.unescape(line), html.unescape(href), html.unescape(name))
        for line, href, name in SCORE.findall(fragment)
    ]


def on_card(client: TestClient, assignment_id: str = LOG_ID, **params: str) -> Shown:
    return scores(card_for(page_of(client, **params), assignment_id))


def details_of(client: TestClient, assignment_id: str = LOG_ID) -> str:
    return client.get(f"/student/assignments/{assignment_id}", headers=PAGE_HEADERS).text


def result_named(client: TestClient, class_id: str, title: str, term: str = "T1") -> str:
    (result_id,) = [
        result_id
        for result_id, value in store_of(client).class_term(class_id, term).current.results.items()
        if value.cells["assignment"][1] == title
    ]
    return result_id


def class_details(client: TestClient, class_id: str, n: int = 1, tree: str = "student") -> str:
    address = f"/{tree}/grades/classes/{class_id}/terms/{n}"
    return client.get(address, headers=PAGE_HEADERS).text


def line_on_class_details(page: str, result_id: str) -> str:
    start = page.index(f'<h3 class="card-title" id="{result_id}" tabindex="-1">')
    found = re.search(r'<p class="effect">(.*?)</p>', page[start:], re.S)
    assert found is not None
    return html.unescape(found.group(1))


def link_to(class_id: str, result_id: str, n: int = 1, tree: str = "student") -> str:
    return f"/{tree}/grades/classes/{class_id}/terms/{n}#{result_id}"


def earlier_row(page: str, title: str) -> str:
    found = re.search(
        rf'<li id="earlier-[^"]*" tabindex="-1">\s*<strong>{re.escape(title)}</strong>.*?</li>',
        page,
        re.S,
    )
    assert found is not None, title
    return found.group(0)


def above_details(row: str, title: str) -> list[str]:
    """The name of each score link in a row that sits in a paragraph of its own with the
    row's Details link next."""
    details = (
        r'\s*<a class="details-link assignment-link" href="[^"]*" '
        rf'aria-label="Details: {re.escape(title)}, {BIOLOGY}">Details</a>'
    )
    return [html.unescape(name) for _, _, name in re.findall(SCORE.pattern + details, row)]


def without_scores(page: str) -> str:
    """A page with each score line and its link taken out, the details' section whole, and
    the fresh ID of its Ask for help form blanked."""
    page = re.sub(
        rf'\n *<section>\s*<h2 class="update-heading">{HEADING}</h2>.*?</section>',
        "",
        page,
        flags=re.S,
    )
    page = re.sub(r"\n *" + SCORE.pattern, "", page)
    return re.sub(r'(name="request_id" value=")[0-9a-f]{32}', r"\1", page)


def test_a_card_shows_class_details_line_for_its_result_and_a_link_to_it() -> None:
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        nothing = on_card(client)
        class_id = with_report(client)
        shown = on_card(client)
        result_id = result_named(client, class_id, LOG)
        class_page = class_details(client, class_id)

    assert nothing == []
    assert shown == [(LINE, link_to(class_id, result_id), f"Grade details: {LOG}, {BIOLOGY}")]
    assert line_on_class_details(class_page, result_id) == LINE


def test_each_place_that_lists_an_assignment_shows_the_same_line_and_link() -> None:
    """An active card, a Done card, the card outside the week shown, the rows due later, active
    and done, the earlier-work rows, shown and folded, and the details."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [
                homework(),
                homework(DIAGRAM_ID, DIAGRAM, date(2026, 8, 27), assigned=date(2026, 8, 18)),
                homework(PRACTICE_ID, PRACTICE, date(2026, 8, 12)),
                homework(OSMOSIS_ID, OSMOSIS, date(2026, 8, 1)),
            ],
            {},
        )
        class_id = with_report(client)
        week = page_of(client)
        apart = page_of(client, week="2026-09-07", change=LOG_ID)
        details = details_of(client)
        reported(store, "done", LOG_ID)
        reported(store, "done", DIAGRAM_ID)
        done_week = page_of(client)
        ids = {title: result_named(client, class_id, title) for title in (LOG, DIAGRAM, PRACTICE)}
        osmosis = result_named(client, class_id, OSMOSIS)

    log = [(LINE, link_to(class_id, ids[LOG]), f"Grade details: {LOG}, {BIOLOGY}")]
    diagram = [
        (DIAGRAM_LINE, link_to(class_id, ids[DIAGRAM]), f"Grade details: {DIAGRAM}, {BIOLOGY}")
    ]
    later, done_later = (
        page[page.index('<section class="panel assigned">') :] for page in (week, done_week)
    )
    outside = apart[apart.index("<h2>Outside the week shown</h2>") :]
    fold = week[week.index('<details class="steps earlier-more"') :]
    assert scores(card_for(week, LOG_ID)) == log
    assert scores(card_for(outside, LOG_ID)) == log
    assert scores(card_for(later, DIAGRAM_ID)) == diagram
    assert LOG_ID in done_week.split('<details class="steps reported-done"')[1]
    assert scores(card_for(done_week, LOG_ID)) == log
    assert "reported-done" in done_later
    assert scores(card_for(done_later, DIAGRAM_ID)) == diagram
    assert scores(earlier_row(week, PRACTICE)) == [
        (
            "School score: 27.0 / 30.0 · 90.0%",
            link_to(class_id, ids[PRACTICE]),
            f"Grade details: {PRACTICE}, {BIOLOGY}",
        )
    ]
    assert scores(earlier_row(fold, OSMOSIS)) == [
        (
            "School score: 31.0 / 40.0 · 77.5%",
            link_to(class_id, osmosis),
            f"Grade details: {OSMOSIS}, {BIOLOGY}",
        )
    ]
    assert scores(details) == log
    heading = f'<h2 class="update-heading">{HEADING}</h2>'
    assert details.count(heading) == 1
    assert (
        details.index("detail-notes") < details.index(heading)
        if "detail-notes" in details
        else True
    )
    assert details.index(heading) < details.index('id="update-or-turn-in"')
    assert len(scores(week)) == 4


def test_a_row_writes_grade_details_in_a_paragraph_of_its_own_above_details() -> None:
    """The rows due later, active and done, and the earlier-work rows, shown and folded, write
    the link as a card does, in a paragraph of its own, with the row's Details link next. A
    row with no score is written as it is with no class on record."""
    poster_id, poster = "assignment-safety-poster", "Lab Safety Poster"
    notes = "Reading Notes"
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [
                homework(),
                homework(DIAGRAM_ID, DIAGRAM, date(2026, 8, 27), assigned=date(2026, 8, 18)),
                homework(poster_id, poster, date(2026, 8, 26), assigned=date(2026, 8, 18)),
                homework(PRACTICE_ID, PRACTICE, date(2026, 8, 12)),
                homework("assignment-reading-notes", notes, date(2026, 8, 11)),
                homework(OSMOSIS_ID, OSMOSIS, date(2026, 8, 1)),
            ],
            {},
        )
        bare = without_minted_fields(page_of(client))
        with_report(client)
        week = without_minted_fields(page_of(client))
        reported(store, "done", DIAGRAM_ID)
        done_week = page_of(client)

    section = '<section class="panel assigned">'
    bare_later, later, done_later = (
        page[page.index(section) :] for page in (bare, week, done_week)
    )
    fold = week[week.index('<details class="steps earlier-more"') :]
    assert above_details(card_for(later, DIAGRAM_ID), DIAGRAM) == [
        f"Grade details: {DIAGRAM}, {BIOLOGY}"
    ]
    assert DIAGRAM_ID in done_later.split('<details class="steps reported-done"')[1]
    assert above_details(card_for(done_later, DIAGRAM_ID), DIAGRAM) == [
        f"Grade details: {DIAGRAM}, {BIOLOGY}"
    ]
    assert above_details(earlier_row(week, PRACTICE), PRACTICE) == [
        f"Grade details: {PRACTICE}, {BIOLOGY}"
    ]
    assert above_details(earlier_row(fold, OSMOSIS), OSMOSIS) == [
        f"Grade details: {OSMOSIS}, {BIOLOGY}"
    ]
    for row, with_no_class in (
        (card_for(later, poster_id), card_for(bare_later, poster_id)),
        (earlier_row(week, notes), earlier_row(bare, notes)),
    ):
        assert row == with_no_class
        assert "School score" not in row
        assert '<p class="details-link">' not in row
        assert row.count('<a class="details-link assignment-link" href="') == 1


def test_the_to_turn_in_rows_and_the_plan_s_rows_keep_no_score() -> None:
    """An assignment with a score to show, still to turn in by her own hand-in form and set
    aside in today's plan: its card shows the line and the link, and neither the To turn in
    section nor the plan's rows show either."""
    action = f"/student/actions/assignments/{LOG_ID}/hand-in"
    plan = fixture_week_plan()
    block = PlanBlock(
        assignment_id=LOG_ID,
        starts_at=time(18, 30),
        ends_at=time(19, 0),
        rationale="the log while the seeds are fresh",
    )
    with browser(key=True) as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        result_id = result_named(client, class_id, LOG)
        opened = client.get(f"/student/assignments/{LOG_ID}", params={"hand_in": "change"}).text
        still = {"state": NEEDS_HAND_IN, "next_action": "", "note": ""}
        answer = client.post(
            action, data={**form_fields(opened, action), **still}, headers=PAGE_HEADERS
        )
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [plan.model_copy(update={"blocks": [*plan.blocks, block]})],
            lambda: [accepting()],
        )
        made = planned(client)
        page = page_of(client)

    assert answer.status_code == 303, answer.text
    start = page.index('<section class="panel to-turn-in"')
    to_turn_in = page[start : page.index("</section>", start)]
    rows = plan_on(page, made)
    assert LOG in to_turn_in
    assert LOG in rows
    for part in (to_turn_in, rows):
        assert "School score" not in part
        assert "Grade details" not in part
    assert scores(page) == [
        (LINE, link_to(class_id, result_id), f"Grade details: {LOG}, {BIOLOGY}")
    ]
    assert scores(card_for(page.replace(to_turn_in, ""), LOG_ID)) == scores(page)


@pytest.mark.parametrize(
    ("reader", "tree"), [("her", "student"), ("parent", "parent"), ("open", "student")]
)
def test_the_link_is_on_the_reader_s_own_tree(
    reader: str, tree: str, tmp_path: pathlib.Path
) -> None:
    """A parent reading her week gets the same line, with the way to a parent's class details."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        shown = on_card(client)
        detailed = scores(details_of(client))
        result_id = result_named(client, class_id, LOG)
        landed = class_details(client, class_id, tree=tree)

    expected = [(LINE, link_to(class_id, result_id, tree=tree), f"Grade details: {LOG}, {BIOLOGY}")]
    assert shown == expected
    assert detailed == expected
    assert line_on_class_details(landed, result_id) == LINE


def test_g3a_i3_with_no_class_on_record_her_week_and_details_show_no_score() -> None:
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        week, details = page_of(client), details_of(client)

    assert "School score" not in week + details
    assert "Grade details" not in week + details
    assert "gradebook" not in details


def test_g3a_i7_a_score_moves_nothing_and_changes_no_other_word_of_her_pages() -> None:
    """Her week and the details read the same with a score showing as with no class on record,
    the score's own line and link aside: every card, group, count and warning."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [homework(), homework(DIAGRAM_ID, DIAGRAM, date(2026, 8, 27), assigned=MONDAY)],
            {},
            {LOG_ID: [school_said("missing", SourceChannel.LMS, date(2026, 8, 18))]},
        )
        reported(store, "done", DIAGRAM_ID)
        before = [without_scores(without_minted_fields(page_of(client))), details_of(client)]
        with_report(client)
        after = [without_minted_fields(page_of(client)), details_of(client)]

    assert [len(scores(page)) for page in after] == [2, 1]
    assert "The school reports this missing" in html.unescape(card_for(after[0], LOG_ID))
    assert [
        list(difflib.unified_diff(was.split("\n"), without_scores(page).split("\n"), n=0))
        for was, page in zip(before, after, strict=True)
    ] == [[], []]
    assert "conflict" not in "".join(after).lower().replace("conflicting", "")


CELLS = [
    pytest.param("| 18.0    | 20.0    |         |", "average left blank", id="no average"),
    pytest.param("|         | 20.0    | 90.0    |", None, id="no points"),
    pytest.param("| 18.0    |         | 90.0    |", "maximum left blank", id="no maximum"),
    pytest.param("| 0.0     | 10.0    | 0.0     |", "0.0 / 10.0 · 0.0%", id="a zero"),
]


@pytest.mark.parametrize(("cells", "said"), CELLS)
def test_g3a_i6_her_pages_line_is_class_details_line_for_the_same_value(
    cells: str, said: str | None
) -> None:
    """The one exception among these: with no points reported class details still says the
    cells, and her pages say nothing."""
    text = IN_WEEK.replace(LOG_ROW, f"| Seed Germination Log {cells}")
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = with_report(client, text)
        shown = on_card(client)
        result_id = result_named(client, class_id, LOG)
        read = store.class_term(class_id, "T1")
        class_line = line_on_class_details(class_details(client, class_id), result_id)

    whole = score_line(read.current.results[result_id], report_names(read.reports, ZONE))
    assert class_line == whole
    if said is None:
        assert shown == []
        assert class_line.startswith("School score: blank / 20.0")
    else:
        assert [line for line, _, _ in shown] == [whole]
        assert said in whole


def test_a_result_a_complete_report_stops_showing_shows_no_score_until_one_shows_it_again() -> None:
    """Class details keeps the score with its notice; an incomplete copy, or a report of
    another term, withholds nothing; a later report that shows the result brings the line back,
    naming the report that supplied the score as class details does."""
    gone = "\n".join(line for line in IN_WEEK.split("\n") if LOG not in line)
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        result_id = result_named(client, class_id, LOG)
        with_report(client, gone.replace("| 7.0 ", "| 8.0 "))
        after_incomplete = on_card(client)
        with_report(client, gone.replace("**T1**", "**T2**"), complete=True)
        after_another_term = on_card(client)
        with_report(client, gone.replace("| 7.0 ", "| 9.0 "), complete=True)
        after_complete = on_card(client)
        class_page = class_details(client, class_id)
        with_report(client, IN_WEEK.replace("| 7.0 ", "| 6.0 "), complete=True)
        shown_again = on_card(client)
        again_page = class_details(client, class_id)

    assert [line for line, _, _ in after_incomplete] == [LINE]
    assert [line for line, _, _ in after_another_term] == [LINE]
    assert after_complete == []
    assert line_on_class_details(class_page, result_id).startswith(LINE)
    assert "Not shown in the" in class_page
    assert [line for line, _, _ in shown_again] == [line_on_class_details(again_page, result_id)]
    assert shown_again[0][0] == f"{LINE}, from the report added August 19"
    assert "Not shown in the" not in again_page


def test_a_first_month_corrected_so_the_dates_meet_shows_the_score_and_back_takes_it_away() -> None:
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(on=date(2027, 8, 20))], {})
        with_report(client)
        apart = on_card(client, week="2027-08-16")
        store.correct_first_month(YEAR, shown=8, month=9, role="parent")
        meets = on_card(client, week="2027-08-16")
        store.correct_first_month(YEAR, shown=9, month=8, role="parent")
        back = on_card(client, week="2027-08-16")

    assert apart == []
    assert [line for line, _, _ in meets] == [LINE]
    assert back == []


def test_a_parent_s_assertion_on_the_points_never_reaches_her_pages() -> None:
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = with_report(client)
        result_id = result_named(client, class_id, LOG)
        store.correct_value(
            class_id,
            "T1",
            page=correction_page(store, class_id, "result", result_id),
            field="points",
            how="parent_assertion",
            value=GradeValue(text="20.0", presence=Presence.REPORTED),
            role="parent",
        )
        asserted = store.class_term(class_id, "T1").current.results[result_id].asserted
        standing = on_card(client)
        store.correct_value(
            class_id,
            "T1",
            page=correction_page(store, class_id, "result", result_id),
            field="points",
            how="parent_assertion",
            value=None,
            role="parent",
        )
        withdrawn = on_card(client)

    assert "points" in asserted
    assert [line for line, _, _ in standing] == [LINE]
    assert [line for line, _, _ in withdrawn] == [LINE]


CLAIMS = [
    pytest.param([], True, id="a date only the family entered"),
    pytest.param([(SourceChannel.LMS, "2026-08-20")], True, id="a source that agrees"),
    pytest.param([(SourceChannel.LMS, "2026-08-21")], False, id="a source that disagrees"),
    pytest.param([(SourceChannel.EMAIL, "next Thursday")], False, id="a claim not read as a date"),
]


@pytest.mark.parametrize(("claims", "shows"), CLAIMS)
def test_the_score_shows_only_while_no_claim_leaves_the_homework_date_in_doubt(
    claims: list[tuple[SourceChannel, str]], shows: bool
) -> None:
    with browser() as client:
        said = [record(channel, value) for channel, value in claims]
        store_of(client).put_on_record([homework()], {LOG_ID: said} if said else {})
        with_report(client)
        shown = on_card(client)
        detailed = scores(details_of(client))

    assert [line for line, _, _ in shown] == ([LINE] if shows else [])
    assert detailed == shown


def test_homework_with_no_date_shows_no_score_and_takes_its_twin_s_away() -> None:
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        with_report(client)
        alone = on_card(client)
        store.put_on_record([homework("assignment-undated", on=None)], {})
        beside = on_card(client)
        undated = scores(details_of(client, "assignment-undated"))

    assert [line for line, _, _ in alone] == [LINE]
    assert beside == []
    assert undated == []


@pytest.mark.parametrize("status", ["done", "not_yet"])
def test_a_twin_she_updated_still_counts_so_neither_row_shows_a_score(status: str) -> None:
    """Every assignment on record takes part, whatever her update says."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(), homework("assignment-twin")], {})
        reported(store, status, "assignment-twin")
        with_report(client)
        page = page_of(client)

    assert scores(page) == []


def test_g3a_i15_the_term_in_use_changes_no_line_and_the_link_names_the_result_s_own_term() -> None:
    """A result of each term that holds a report is read: homework due in the first term's
    week links to place 1, homework due on the second term's day links to place 2, whatever
    term the household or a viewer has in use."""
    second = REPORT.replace("**T1**", "**T2**")
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [homework(), homework("assignment-second-log", on=date(2026, 9, 22))], {}
        )
        class_id = with_report(client)
        with_report(client, second)
        first_id = result_named(client, class_id, LOG)
        second_id = result_named(client, class_id, LOG, "T2")

        def read() -> tuple[Shown, Shown]:
            return on_card(client), scores(details_of(client, "assignment-second-log"))

        seen = [read()]
        current = store.grade_contexts().current
        assert current is not None
        other = (YEAR, "T1" if current[1] == "T2" else "T2")
        store.set_current_context(current, other, "parent")
        seen.append(read())
        store.choose_view("student", current)
        store.choose_view("anyone", current)
        seen.append(read())

    assert seen[0] == (
        [(LINE, link_to(class_id, first_id), f"Grade details: {LOG}, {BIOLOGY}")],
        [(LINE, link_to(class_id, second_id, n=2), f"Grade details: {LOG}, {BIOLOGY}")],
    )
    assert seen[1:] == [seen[0], seen[0]]


def test_a_twin_result_in_another_term_shows_nothing() -> None:
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        with_report(client)
        with_report(client, IN_WEEK.replace("**T1**", "**T2**"))
        shown = on_card(client)

    assert shown == []


GEOMETRY = IN_WEEK.replace("07 BIO - C", "08 GEO - A").replace("Biology", "08 GEOMETRY")
IMPORTED_ID, SAMPLE_ID, TYPED_ID = "assignment-imported", "assignment-sample", "assignment-typed"


def test_connecting_a_second_name_makes_twins_and_removing_it_brings_the_line_back() -> None:
    """ "Geometry" waits beside an imported "08 Geometry" row for the same work: the imported
    row shows the score. Connected, neither does, and both rows stay. The connection removed,
    the line returns, and only the mapping was written."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [
                homework(IMPORTED_ID, course="08 Geometry"),
                homework(SAMPLE_ID, course="Geometry"),
            ],
            {},
        )
        class_id = with_report(client, GEOMETRY)
        waiting = scores(page_of(client))
        homework_before = as_stored(store, "assignments")
        store.connect_homework_class(YEAR, "Geometry", shown=None, chosen=class_id, role="parent")
        connected_page = page_of(client)
        store.connect_homework_class(
            YEAR, "Geometry", shown=class_id, chosen=UNCONNECTED, role="parent"
        )
        removed = scores(page_of(client))
        homework_after = as_stored(store, "assignments")

    assert [name for _, _, name in waiting] == [f"Grade details: {LOG}, 08 Geometry"]
    assert scores(connected_page) == []
    assert card_for(connected_page, IMPORTED_ID)
    assert card_for(connected_page, SAMPLE_ID)
    assert removed == waiting
    assert homework_after == homework_before


def test_a_class_typed_as_grades_shows_it_is_a_twin_of_the_imported_row() -> None:
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(IMPORTED_ID, course="08 Geometry")], {})
        with_report(client, GEOMETRY)
        alone = scores(page_of(client))
        store.put_on_record([homework(TYPED_ID, course="08 GEOMETRY")], {})
        beside = scores(page_of(client))

    assert len(alone) == 1
    assert beside == []


def test_only_the_chosen_class_s_result_shows_and_the_line_follows_a_changed_answer() -> None:
    """Two classes with one official name and the same result: the name waits, shows the class
    a parent chooses, and follows the answer to the other class at the next read. The
    connection removed, the line goes, and no other grade table or homework row changed."""
    section_b = IN_WEEK.replace("07 BIO - C", "07 BIO - D").replace("| 18.0 ", "| 19.0 ")
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_a = with_report(client)
        class_b = with_report(client, section_b)
        waiting = on_card(client)
        store.connect_homework_class(YEAR, BIOLOGY, shown=None, chosen=class_a, role="parent")
        first = on_card(client)
        store.connect_homework_class(YEAR, BIOLOGY, shown=class_a, chosen=class_b, role="parent")
        second = on_card(client)
        others = [table for table in GRADEBOOK_TABLES if table != "grade_homework_classes"]
        before = {table: as_stored(store, table) for table in (*others, "assignments")}
        store.connect_homework_class(
            YEAR, BIOLOGY, shown=class_b, chosen=UNCONNECTED, role="parent"
        )
        removed = on_card(client)
        after = {table: as_stored(store, table) for table in (*others, "assignments")}
        result_a = result_named(client, class_a, LOG)
        result_b = result_named(client, class_b, LOG)

    assert class_a != class_b
    assert waiting == []
    assert [(line, href) for line, href, _ in first] == [(LINE, link_to(class_a, result_a))]
    assert [(line, href) for line, href, _ in second] == [
        ("School score: 19.0 / 20.0 · 90.0%", link_to(class_b, result_b))
    ]
    assert removed == []
    assert after == before


def test_two_spellings_that_fold_alike_connect_to_one_class() -> None:
    with browser() as client:
        store_of(client).put_on_record(
            [
                homework(course="biology"),
                homework(DIAGRAM_ID, DIAGRAM, date(2026, 8, 27), course=" BIOLOGY "),
            ],
            {},
        )
        with_report(client)
        first = on_card(client)
        second = scores(details_of(client, DIAGRAM_ID))

    assert [line for line, _, _ in first] == [LINE]
    assert [line for line, _, _ in second] == [DIAGRAM_LINE]


def test_g3a_i10_no_match_is_stored_and_a_deleted_class_and_term_takes_the_line_away() -> None:
    """Reading her pages writes nothing; a delete's preview counts no link; after the delete the
    line is gone and the mapping stands; a later import brings the line back."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = with_report(client)
        store.connect_homework_class(YEAR, BIOLOGY, shown=None, chosen=class_id, role="parent")
        rows = every_row(database_of(client))
        shown = on_card(client)
        details_of(client)
        after_reading = every_row(database_of(client))
        preview = store.delete_preview(class_id, "T1")
        assert isinstance(preview, DeletePreview)
        store.delete_class_term(class_id, "T1", revision=preview.revision, role="parent")
        mapping = as_stored(store, "grade_homework_classes")
        deleted = on_card(client)
        with_report(client)
        again = on_card(client)

    assert "grade_homework_classes" in GRADEBOOK_TABLES
    assert [line for line, _, _ in shown] == [LINE]
    assert after_reading == rows
    assert preview.links == 0
    assert len(mapping) == 1
    assert deleted == []
    assert [line for line, _, _ in again] == [LINE]


def test_g3a_i11_the_grade_details_address_carries_ids_and_a_place_alone() -> None:
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        with_report(client)
        ((_, href, _),) = on_card(client)

    assert re.fullmatch(
        r"/student/grades/classes/class-[0-9a-f]{32}/terms/[1-9][0-9]?#result-[0-9a-f]{32}", href
    )


def test_g3a_i14_one_read_gives_every_place_on_a_page_the_same_line() -> None:
    """The card outside the week shown and the earlier-work row of one assignment, on one page,
    read one mapping: the same line and the same link."""
    with browser() as client:
        store_of(client).put_on_record([homework(PRACTICE_ID, PRACTICE, date(2026, 8, 12))], {})
        with_report(client)
        page = page_of(client, change=PRACTICE_ID)

    row = scores(earlier_row(page, PRACTICE))
    assert len(row) == 1
    assert scores(page) == row * len(scores(page))
    assert len(scores(page)) == 2


def test_g3a_i2_the_planner_s_input_reads_the_same_with_a_score_showing_or_not() -> None:
    """The planner's input and its digest are the same with no class on record, with a score
    showing by the rules, and with two results linked by a parent and their count showing."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})

        def planner_input() -> tuple[str, list[dict[str, object]]]:
            week = week_from(read_everything(store, store), MONDAY)
            return planning_digest(week), canonical_active_input(week)

        before = planner_input()
        class_id = with_report(client)
        shown = on_card(client)
        after = planner_input()
        with_report(client, SECOND_TERM)
        for term in ("T1", "T2"):
            parent_links(client, class_id, result_named(client, class_id, LOG, term), term=term)
        linked = several(card_for(page_of(client), LOG_ID))
        with_links = planner_input()

    assert [line for line, _, _ in shown] == [LINE]
    assert LOG_ID in str(before[1])
    assert after == before
    assert [count for count, _, _ in linked] == ["2"]
    assert with_links == before


def test_a_newer_current_report_s_changed_score_is_the_one_her_card_shows() -> None:
    newer = IN_WEEK.replace(LOG_ROW, "| Seed Germination Log | 19.0    | 20.0    | 95.0    |")
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        with_report(client)
        first = on_card(client)
        with_report(client, newer)
        second = on_card(client)

    assert [line for line, _, _ in first] == [LINE]
    assert [line for line, _, _ in second] == ["School score: 19.0 / 20.0 · 95.0%"]


@pytest.mark.parametrize(("month", "shows"), [(8, True), (None, False)])
def test_a_name_that_connects_in_two_school_years_shows_the_result_its_date_equals(
    month: int | None, shows: bool
) -> None:
    """Another year's class with the same title leaves the count only by a fully resolved,
    different date: with that year's first month unconfirmed it competes, and nothing shows."""
    earlier = draft_of(IN_WEEK.replace("**2026-2027**", "**2025-2026**"))
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        review = store.review_grade_report(earlier, capture_key(earlier), key=KEY)
        answers = grade_answers(review, month=month)
        saved = save_grade(store, earlier, key=KEY, review=review, answers=answers)
        class_id = with_report(client)
        shown = on_card(client)
        result_id = result_named(client, class_id, LOG)
        years = {year for year, _ in store.grade_contexts().terms}
        earlier_class = capture_class(store, earlier)

    assert isinstance(saved, GradeReportSaved)
    assert years == {"2025-2026", YEAR}
    assert earlier_class != class_id
    assert [(line, href) for line, href, _ in shown] == (
        [(LINE, link_to(class_id, result_id))] if shows else []
    )


def test_each_result_on_class_details_carries_its_own_id_on_a_title_that_takes_the_focus() -> None:
    """Dated results and those under "Date not confirmed" alike."""
    with browser() as client:
        class_id = with_report(client, IN_WEEK.replace("| 08/27 ", "|       "))
        ids = list(store_of(client).class_term(class_id, "T1").current.results)
        page = class_details(client, class_id)

    assert len(ids) == 4
    assert "Date not confirmed" in page
    assert sorted(re.findall(r'<h3 class="card-title" id="([^"]*)" tabindex="-1">', page)) == (
        sorted(ids)
    )


DOUBTS = [
    pytest.param(THURSDAY, [], False, True, id="no claim"),
    pytest.param(THURSDAY, ["2026-08-20", " 2026-08-20 "], False, True, id="claims that agree"),
    pytest.param(THURSDAY, ["2026-08-20"], True, False, id="a claim that can't be read"),
    pytest.param(THURSDAY, ["2026-08-20", "2026-08-21"], False, False, id="a claim disagrees"),
    pytest.param(THURSDAY, ["Thursday"], False, False, id="a claim not read as a date"),
    pytest.param(None, [], False, False, id="no date"),
]


@pytest.mark.parametrize(("on", "said", "unreadable", "resolved"), DOUBTS)
def test_a_homework_date_is_fully_resolved_only_when_every_claim_reads_as_the_record_s_day(
    on: date | None, said: list[str], unreadable: bool, resolved: bool
) -> None:
    claims = [record(SourceChannel.LMS, value) for value in said]

    read = homework_of(homework(on=on), claims, unreadable)

    assert (read.assignment_id, read.title, read.day) == (LOG_ID, LOG, on)
    assert read.resolved is resolved


def test_only_classes_a_homework_name_connects_to_are_read_and_none_with_no_homework() -> None:
    """Every school year on record is asked for its classes; a class and term is read only
    for a connected class in a term that holds its report."""
    with browser() as client:
        store = store_of(client)
        class_id = with_report(client)
        with_report(client, GEOMETRY.replace("**T1**", "**T2**"))
        unnamed = read_grade_facts(store, [])
        other = read_grade_facts(store, ["Art"])
        named = read_grade_facts(store, [BIOLOGY, "Art"])

    assert unnamed == ()
    assert [(year.mapping.year, year.terms, dict(year.reads)) for year in other] == [
        (YEAR, ("T1", "T2"), {})
    ]
    assert [(year.terms, year.first_month, list(year.reads)) for year in named] == [
        (("T1", "T2"), 8, [(class_id, "T1")])
    ]


def kept_as_earlier(client: TestClient, text: str, *, complete: bool = False) -> None:
    """A parent's save of the report ``text`` as an earlier report."""
    store, draft = store_of(client), draft_of(text)
    review = store.review_grade_report(draft, capture_key(draft), key=KEY)
    answers = dataclasses.replace(grade_answers(review), use="earlier")
    outcome = save_grade(store, draft, key=KEY, review=review, answers=answers, complete=complete)
    assert isinstance(outcome, GradeReportSaved), outcome


def test_a_report_kept_as_earlier_supplies_no_score_and_withholds_none() -> None:
    """Alone it gives her card nothing; a current report's value shows; and a complete copy
    without the result, kept as earlier, leaves the line where it is."""
    changed = IN_WEEK.replace(LOG_ROW, "| Seed Germination Log | 19.0    | 20.0    | 95.0    |")
    gone = "\n".join(line for line in IN_WEEK.split("\n") if LOG not in line)
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        kept_as_earlier(client, changed)
        only_earlier = on_card(client)
        with_report(client)
        current = on_card(client)
        kept_as_earlier(client, gone.replace("| 7.0 ", "| 9.0 "), complete=True)
        absent_in_an_earlier_one = on_card(client)

    assert only_earlier == []
    assert [line for line, _, _ in current] == [LINE]
    assert absent_in_an_earlier_one == current


def test_a_changed_score_left_unticked_keeps_the_standing_score_with_its_source() -> None:
    """A newer current report shows the result with another score, and a parent leaves
    that value unticked. Her card keeps the standing score and names the report it comes from,
    character for character as class details writes it."""
    newer = draft_of(
        IN_WEEK.replace(LOG_ROW, "| Seed Germination Log | 19.0    | 20.0    | 95.0    |")
    )
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = with_report(client)
        result_id = result_named(client, class_id, LOG)
        review = store.review_grade_report(newer, capture_key(newer), key=KEY)
        (changed,) = [item.key for item in review.rows if item.result_id == result_id]
        saved = save_grade(store, newer, key=KEY, review=review, selection=review.ready - {changed})
        shown = on_card(client)
        detailed = scores(details_of(client))
        value = store.class_term(class_id, "T1").current.results[result_id]
        class_line = line_on_class_details(class_details(client, class_id), result_id)

    assert changed in review.ready
    assert isinstance(saved, GradeReportSaved)
    assert value.last_shown is not None
    assert value.last_shown.report_id == saved.report_id
    assert value.report_id != saved.report_id
    assert shown == [
        (
            f"{LINE}, from the report added August 19",
            link_to(class_id, result_id),
            f"Grade details: {LOG}, {BIOLOGY}",
        )
    ]
    assert class_line == shown[0][0]
    assert detailed == shown


def test_a_report_without_the_result_withholds_no_score_while_a_row_s_question_waits() -> None:
    """A complete newer report leaves the result out and asks about another row.
    With that question unanswered the line stays; answered, the same report takes it away."""
    gone = "\n".join(line for line in IN_WEEK.split("\n") if LOG not in line)
    asking = draft_of(
        gone.replace(
            "| Cell Diagram             | 7.0 ", "| Cell Diagram             | EX  "
        ).replace("| Missing    | 08/27   |", "| Missing    | 08/28   |")
    )
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = with_report(client)
        review = store.review_grade_report(asking, capture_key(asking), key=KEY)
        asked = [item.key for item in review.rows if item.question is not None]
        first = save_grade(store, asking, key=KEY, review=review, complete=True)
        unanswered = on_card(client)
        unanswered_page = class_details(client, class_id)
        later = store.review_grade_report(asking, capture_key(asking), key=KEY)
        ((row, question),) = [
            (item.key, item.question) for item in later.rows if item.question is not None
        ]
        same = MatchAnswer(row, question.ids, question.ids[0])
        answers = dataclasses.replace(grade_answers(later), matches=(same,))
        second = save_grade(
            store, asking, key=KEY, review=later, answers=answers, selection=(), complete=True
        )
        answered = on_card(client)
        answered_page = class_details(client, class_id)

    assert len(asked) == 1
    assert isinstance(first, GradeReportSaved)
    assert isinstance(second, GradeReportSaved)
    assert [line for line, _, _ in unanswered] == [LINE]
    assert "Not shown in the" not in unanswered_page
    assert answered == []
    assert "Not shown in the" in answered_page


def test_claims_the_record_can_t_read_show_no_score_and_still_count_against_a_twin() -> None:
    """A stored claim that can't be read leaves the homework date in doubt, whatever it says:
    the card and the details show no score, and the row stays in the count, so a twin with a
    settled date shows none either."""
    said = "A claim about this date cannot be read right now."
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        with_report(client)
        readable = on_card(client)
        unreadable_claim(store, LOG_ID, THURSDAY.isoformat())
        unread = store.read_claims([LOG_ID]).unreadable
        card = card_for(page_of(client), LOG_ID)
        details = details_of(client)
        store.put_on_record([homework("assignment-twin")], {})
        beside = scores(page_of(client))
        twin = scores(details_of(client, "assignment-twin"))

    assert [line for line, _, _ in readable] == [LINE]
    assert unread == frozenset({LOG_ID})
    assert said in card
    assert said in details
    assert scores(card) == []
    assert scores(details) == []
    assert beside == []
    assert twin == []


def test_a_twin_she_turned_in_still_counts_so_neither_row_shows_a_score() -> None:
    """Her hand-in update, saved from the details' own form, takes no row out of the count."""
    twin = "assignment-twin"
    action = f"/student/actions/assignments/{twin}/hand-in"
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(), homework(twin)], {})
        with_report(client)
        opened = client.get(f"/student/assignments/{twin}", params={"hand_in": "change"}).text
        sent = {**form_fields(opened, action), "state": TURNED_IN, "next_action": "", "note": ""}
        answer = client.post(action, data=sent, headers=PAGE_HEADERS)
        said = [event.state for event in store.hand_in_chains([twin]).get(twin, [])]
        page = page_of(client)
        detailed = [scores(details_of(client, one)) for one in (LOG_ID, twin)]

    assert answer.status_code == 303, answer.text
    assert said == [TURNED_IN]
    assert card_for(page, LOG_ID)
    assert card_for(page, twin)
    assert scores(page) == []
    assert detailed == [[], []]


IMPORTED = f"Homework for Wren\n- 08/20/2026 - Thursday\n08 Geometry - Due: {LOG}:\n"
"""The school's list as a parent pastes it: the log, in "08 Geometry", due on the pinned
Thursday."""
TYPED = "08 GEOMETRY"
KEEP = "/parent/inbox/keep"


def entered_by_a_parent(client: TestClient) -> None:
    """The parent's entry form with the class typed as a report writes it, reviewed and then
    saved from the review page's own form."""
    entry = {
        "course": TYPED,
        "title": LOG,
        "due_date": THURSDAY.isoformat(),
        "assigned_on": "",
        "kind": "HOMEWORK",
        "note": "",
    }
    review = client.post("/parent/inbox/enter", data=entry)
    assert review.status_code == 200, review.text
    kept = client.post(KEEP, data=whole_form(review.text, KEEP))
    assert kept.headers["location"] == "/parent?added=1&updated=0&unchanged=0"


def added_from_a_note(client: TestClient) -> None:
    """A homework note of hers with the class typed as a report writes it, then added to
    homework under the title from the note's own form."""
    fields = form_fields(client.get(NEW_NOTE_PAGE).text, NOTE_ACTIONS)
    words = {"text": "Seed log, from the board", "course": TYPED, "due_date": THURSDAY.isoformat()}
    noted = client.post(NOTE_ACTIONS, data={**fields, **words}, headers=PAGE_HEADERS)
    assert noted.status_code == 303, noted.text
    action = note_add_action(fields["capture_id"])
    form = whole_form(client.get(note_add_href(fields["capture_id"])).text, action)
    assert (form["course_other"], form["due_date"]) == (TYPED, THURSDAY.isoformat())
    added = client.post(action, data={**form, "title": LOG}, headers=PAGE_HEADERS)
    assert added.status_code == 303, added.text


@pytest.mark.parametrize("typed", [entered_by_a_parent, added_from_a_note])
def test_a_class_typed_as_a_report_writes_it_makes_a_twin_of_the_imported_row(
    typed: Callable[[TestClient], None],
) -> None:
    """The row a parent's paste imports shows the score alone. The typed one is a row with an
    ID of its own under the same folded name, so the two are twins and neither shows a score."""
    with browser() as client:
        store = store_of(client)
        review = client.post("/parent/inbox/read", data={"text": IMPORTED})
        pasted = client.post(KEEP, data=whole_form(review.text, KEEP))
        class_id = with_report(client, GEOMETRY)
        alone = scores(page_of(client))
        typed(client)
        twins = [item for item in store.all_assignments() if item.title == LOG]
        mapping = store.homework_classes(YEAR)
        beside = scores(page_of(client))
        detailed = [scores(details_of(client, item.assignment_id)) for item in twins]

    assert pasted.headers["location"] == "/parent?added=1&updated=0&unchanged=0"
    assert [(line, name) for line, _, name in alone] == [
        (LINE, f"Grade details: {LOG}, 08 Geometry")
    ]
    assert sorted(item.course for item in twins) == [TYPED, "08 Geometry"]
    assert len({item.assignment_id for item in twins}) == 2
    assert {name_key(item.course) for item in twins} == {name_key(TYPED)}
    assert [class_for(item.course, mapping) for item in twins] == [class_id, class_id]
    assert beside == []
    assert detailed == [[], []]


def test_a_task_takes_part_like_any_assignment_on_record() -> None:
    """A task is sized apart from homework and matched like it. Alone it shows its result's
    line and link on its card and on its details; beside homework with its class, title and
    day neither row shows a score."""
    twin = "assignment-twin"
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(kind=AssignmentKind.TASK)], {})
        class_id = with_report(client)
        result_id = result_named(client, class_id, LOG)
        kinds = [item.kind for item in store.all_assignments() if item.title == LOG]
        alone = [on_card(client), scores(details_of(client))]
        store.put_on_record([homework(twin)], {})
        beside = [
            scores(page_of(client)),
            *(scores(details_of(client, one)) for one in (LOG_ID, twin)),
        ]

    expected = [(LINE, link_to(class_id, result_id), f"Grade details: {LOG}, {BIOLOGY}")]
    assert kinds == [AssignmentKind.TASK]
    assert alone == [expected, expected]
    assert beside == [[], [], []]


TASK_DATES = [
    pytest.param(None, [], False, id="no date"),
    pytest.param(date(2026, 8, 21), ["2026-08-24"], False, id="a date in doubt"),
    pytest.param(date(2026, 8, 21), [], True, id="a settled date that differs"),
]


@pytest.mark.parametrize(("on", "said", "shows"), TASK_DATES)
def test_a_task_with_the_homework_s_title_competes_until_its_own_date_rules_it_out(
    on: date | None, said: list[str], shows: bool
) -> None:
    """A task of the same class and title with no date, or a date a source disputes, takes
    the homework row's score away and shows none itself. One whose settled date differs
    leaves the count, and the homework row shows its score."""
    task = "assignment-task"
    claims = {task: [record(SourceChannel.LMS, value) for value in said]} if said else {}
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        with_report(client)
        alone = on_card(client)
        store.put_on_record([homework(task, on=on, kind=AssignmentKind.TASK)], claims)
        kinds = {item.assignment_id: item.kind for item in store.all_assignments()}
        beside = [on_card(client), scores(details_of(client)), scores(details_of(client, task))]

    assert kinds[task] is AssignmentKind.TASK
    assert [line for line, _, _ in alone] == [LINE]
    assert [[line for line, _, _ in one] for one in beside] == [
        [LINE] if shows else [],
        [LINE] if shows else [],
        [],
    ]


def not_captured_due(text: str) -> str:
    """The report ``text`` with no Due column in any of its tables of results."""
    lines = []
    for line in text.split("\n"):
        cells = line.split("|")
        lines.append("|".join(cells[:6] + cells[7:]) if len(cells) == 13 else line)
    return "\n".join(lines)


def test_the_grade_date_is_the_due_date_shown_when_the_newest_report_captured_none() -> None:
    """A newer current report changes the score and holds no Due column, and a parent answers that
    each of its rows is the result on record. The result's own Due cell is then not captured, class
    details shows an earlier report's date, and her card shows the newer score under it."""
    newer = draft_of(
        not_captured_due(
            IN_WEEK.replace(LOG_ROW, "| Seed Germination Log | 19.0    | 20.0    | 95.0    |")
        )
    )
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = with_report(client)
        result_id = result_named(client, class_id, LOG)
        asked = store.review_grade_report(newer, capture_key(newer), key=KEY)
        same = tuple(
            MatchAnswer(item.key, item.question.ids, item.question.ids[0])
            for item in asked.rows
            if item.question is not None
        )
        review = store.review_grade_report(newer, capture_key(newer), key=KEY, matches=same)
        answers = dataclasses.replace(grade_answers(review), matches=same)
        saved = save_grade(store, newer, key=KEY, review=review, answers=answers)
        value = store.class_term(class_id, "T1").current.results[result_id]
        shown = on_card(client)
        detailed = scores(details_of(client))
        class_line = line_on_class_details(class_details(client, class_id), result_id)

    assert len(same) == 4
    assert isinstance(saved, GradeReportSaved)
    assert value.report_id == saved.report_id
    assert value.cells["due"][0] is Presence.NOT_CAPTURED
    assert value.due is not None
    assert value.due.cell == (Presence.REPORTED, "08/20")
    assert [line for line, _, _ in shown] == ["School score: 19.0 / 20.0 · 95.0%"]
    assert class_line == shown[0][0]
    assert detailed == shown


def test_the_link_names_a_term_by_its_place_in_the_year_not_among_the_class_s_reports() -> None:
    """A class whose only report is in the year's second term: the link goes to place 2,
    where class details holds the result."""
    geometry = "08 Geometry"
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(course=geometry)], {})
        with_report(client)
        class_id = with_report(client, GEOMETRY.replace("**T1**", "**T2**"))
        result_id = result_named(client, class_id, LOG, "T2")
        terms = store.terms_of_year(YEAR)
        reported_in = [
            term
            for term in terms
            for one in store.classes_in(YEAR, term)
            if one.class_id == class_id and one.reported
        ]
        shown = on_card(client)
        detailed = scores(details_of(client))
        landed = class_details(client, class_id, n=2)

    assert terms == ("T1", "T2")
    assert reported_in == ["T2"]
    assert shown == [(LINE, link_to(class_id, result_id, n=2), f"Grade details: {LOG}, {geometry}")]
    assert detailed == shown
    assert line_on_class_details(landed, result_id) == LINE


def test_each_school_year_s_results_resolve_under_that_year_s_own_first_month() -> None:
    """Two years hold a class of one name and a result due 08/20. The earlier starting in September
    and the later in August, both fall on one day and the homework due then shows no score. Both
    starting in August, the earlier year's falls a year before and the later's score shows."""
    past = "2025-2026"
    earlier = draft_of(IN_WEEK.replace(f"**{YEAR}**", f"**{past}**"))
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        review = store.review_grade_report(earlier, capture_key(earlier), key=KEY)
        saved = save_grade(
            store, earlier, key=KEY, review=review, answers=grade_answers(review, month=9)
        )
        class_id = with_report(client)
        result_id = result_named(client, class_id, LOG)
        months = [store.first_month_of(year) for year in (past, YEAR)]
        apart = [on_card(client), scores(details_of(client))]
        store.correct_first_month(past, shown=9, month=8, role="parent")
        alike = [on_card(client), scores(details_of(client))]

    expected = [(LINE, link_to(class_id, result_id), f"Grade details: {LOG}, {BIOLOGY}")]
    assert isinstance(saved, GradeReportSaved)
    assert months == [9, 8]
    assert apart == [[], []]
    assert alike == [expected, expected]


TWIN_DATES = [
    pytest.param(["2026-09-11"], False, id="its date in doubt"),
    pytest.param([], True, id="its date settled"),
]


@pytest.mark.parametrize(("said", "shows"), TWIN_DATES)
def test_a_twin_her_week_doesn_t_list_still_takes_the_line_away(
    said: list[str], shows: bool
) -> None:
    """Homework of the same class and title due three weeks later is on no part of her week.
    While a source disputes its date it competes, and the card her week shows has no score,
    there or on its details; with its date settled it leaves the count."""
    twin = "assignment-twin"
    claims = {twin: [record(SourceChannel.LMS, value) for value in said]} if said else {}
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(), homework(twin, on=date(2026, 9, 10))], claims)
        with_report(client)
        week = page_of(client)
        detailed = [scores(details_of(client, one)) for one in (LOG_ID, twin)]

    assert f'id="{assignment_anchor(twin)}"' not in week
    assert week.count(f">{LOG}<") == 1
    assert [line for line, _, _ in scores(week)] == ([LINE] if shows else [])
    assert [[line for line, _, _ in one] for one in detailed] == [[LINE] if shows else [], []]


SMUDGED = "smudged"
"""Text that is no presence, no whole number, no moment and no JSON, which no save writes."""
LEFT_OUT = "a homework page shows no school score: GradeValueUndecodable"
"""The one record a page read makes when it leaves its school scores out."""


def stored(store: ProjectStateStore, statement: str, *values: object) -> None:
    """One statement written straight into the household's file, as no save writes it."""
    with store._lock, store._connection:
        store._connection.execute(statement, values)


def points_presence(client: TestClient, result_id: str, presence: str) -> None:
    """The stored presence of a result's points, set in every report that holds the result."""
    stored(
        store_of(client),
        "UPDATE grade_result_observations SET points_presence = ? WHERE result_id = ?",
        presence,
        result_id,
    )


def recorded(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Each message the application logged, warnings and above."""
    return [
        one.getMessage()
        for one in caplog.records
        if one.name.startswith("blossom") and one.levelno >= logging.WARNING
    ]


def test_a_grade_cell_that_can_t_be_decoded_leaves_her_pages_as_with_no_class_on_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Her week and the details of the cell's assignment, of one whose result decodes and of one
    with none answer, show no score and read as with no class on record. Each read records the
    failure's type alone, the file stays as it was, and the lines return once the cell decodes."""
    notes_id = "assignment-reading-notes"
    addresses = [
        HER_PAGE,
        *(f"/student/assignments/{one}" for one in (LOG_ID, DIAGRAM_ID, notes_id)),
    ]
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [
                homework(),
                homework(DIAGRAM_ID, DIAGRAM, date(2026, 8, 27), assigned=MONDAY),
                homework(notes_id, "Reading Notes", date(2026, 8, 19)),
            ],
            {},
            {LOG_ID: [school_said("missing", SourceChannel.LMS, date(2026, 8, 18))]},
        )
        reported(store, "done", DIAGRAM_ID)

        def read() -> list[str]:
            answers = [client.get(address, headers=PAGE_HEADERS) for address in addresses]
            assert [answer.status_code for answer in answers] == [200, 200, 200, 200]
            return [without_minted_fields(answer.text) for answer in answers]

        bare = read()
        class_id = with_report(client)
        whole = read()
        result_id = result_named(client, class_id, LOG)
        points_presence(client, result_id, SMUDGED)
        before = every_row(database_of(client))
        said, logged = [], []
        with caplog.at_level(logging.WARNING, logger="blossom"):
            for address in addresses:
                caplog.clear()
                answer = client.get(address, headers=PAGE_HEADERS)
                said.append((answer.status_code, without_minted_fields(answer.text)))
                logged.append((recorded(caplog), caplog.text))
        after = every_row(database_of(client))
        points_presence(client, result_id, Presence.REPORTED.value)
        again = read()

    assert [len(scores(page)) for page in whole] == [2, 1, 1, 0]
    assert [status for status, _ in said] == [200, 200, 200, 200]
    for (_, page), with_no_class in zip(said, bare, strict=True):
        assert "School score" not in page
        assert "Grade details" not in page
        assert HEADING not in page
        assert not [said for said in ("no grades", "not grade") if said in page.lower()]
        assert without_scores(page) == without_scores(with_no_class)
    assert [records for records, _ in logged] == [[LEFT_OUT]] * 4
    for _, text in logged:
        assert not [word for word in (LOG, DIAGRAM, BIOLOGY, "18.0", SMUDGED) if word in text]
    assert after == before
    assert [len(scores(page)) for page in again] == [2, 1, 1, 0]


def test_an_undecodable_cell_in_a_result_no_card_shows_leaves_the_whole_page_without_scores(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cell is in a result of the connected class whose homework is due weeks later, off
    her week. Her week and each assignment's details show no score, though the result of the
    one card her week shows decodes."""
    later = REPORT.replace("09/22", "08/20")
    with browser() as client:
        store_of(client).put_on_record(
            [homework(), homework(OSMOSIS_ID, OSMOSIS, date(2026, 10, 2))], {}
        )
        class_id = with_report(client, later)

        def read() -> list[str]:
            return [page_of(client), details_of(client), details_of(client, OSMOSIS_ID)]

        whole = read()
        points_presence(client, result_named(client, class_id, OSMOSIS), SMUDGED)
        with caplog.at_level(logging.WARNING, logger="blossom"):
            left = read()

    assert OSMOSIS not in whole[0]
    assert [[line for line, _, _ in scores(page)] for page in whole] == [
        [LINE],
        [LINE],
        ["School score: 31.0 / 40.0 · 77.5%"],
    ]
    assert card_for(left[0], LOG_ID)
    assert not [page for page in left if "School score" in page or "Grade details" in page]
    assert recorded(caplog) == [LEFT_OUT] * 3


def test_a_twin_with_an_undecodable_cell_is_refused_and_the_other_does_not_show(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two terms hold a result with the log's title and day, so the log shows no score. With
    one of the two undecodable it still shows none: a count without that one would make the
    other look like the log's only result."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        with_report(client, IN_WEEK.replace("**T1**", "**T2**"))
        twins = scores(page_of(client))
        points_presence(client, result_named(client, class_id, LOG, "T2"), SMUDGED)
        with caplog.at_level(logging.WARNING, logger="blossom"):
            left = [page_of(client), details_of(client)]

    assert twins == []
    assert not [page for page in left if "School score" in page or "Grade details" in page]
    assert recorded(caplog) == [LEFT_OUT] * 2


def test_a_cell_the_school_left_unreadable_withholds_its_own_score_and_no_other(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stored presence of unreadable is one of the four: its result shows no score, as one
    with no points reported shows none, the page's other score shows, and no failure is
    recorded."""
    with browser() as client:
        store_of(client).put_on_record(
            [homework(), homework(DIAGRAM_ID, DIAGRAM, date(2026, 8, 27), assigned=MONDAY)], {}
        )
        class_id = with_report(client)
        points_presence(client, result_named(client, class_id, DIAGRAM), Presence.UNREADABLE.value)
        with caplog.at_level(logging.WARNING, logger="blossom"):
            week = page_of(client)
            detailed = [scores(details_of(client, one)) for one in (LOG_ID, DIAGRAM_ID)]

    assert [line for line, _, _ in scores(week)] == [LINE]
    assert [line for line, _, _ in scores(card_for(week, LOG_ID))] == [LINE]
    assert [[line for line, _, _ in one] for one in detailed] == [[LINE], []]
    assert recorded(caplog) == []


@pytest.mark.parametrize("address", [HER_PAGE, f"/student/assignments/{LOG_ID}"])
def test_a_database_error_from_the_grade_read_gets_the_page_s_own_answer(
    address: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Only a stored value that can't be decoded leaves the scores out: a file that can't be
    read is answered as her page answers any record it can't read."""

    def refused(class_id: str, term: str) -> None:
        msg = "database disk image is malformed"
        raise sqlite3.DatabaseError(msg)

    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        with_report(client)
        monkeypatch.setattr(store, "class_term", refused)
        with caplog.at_level(logging.WARNING, logger="blossom"):
            answer = client.get(address, headers=PAGE_HEADERS)

    assert answer.status_code == 503
    assert "School score" not in answer.text
    assert LEFT_OUT not in recorded(caplog)
    assert [said for said in recorded(caplog) if said.endswith(": DatabaseError")]


@pytest.mark.parametrize("address", ["/student/grades", "/student/grades/classes/{}/terms/1"])
def test_grades_and_class_details_answer_an_undecodable_cell_with_the_store_s_own_error(
    address: str,
) -> None:
    """Neither page leaves anything out: each fails with the error the read raises, a
    ``ValueError`` as the decoding's own error is, with that error as its cause and nothing
    stored in its message."""
    with browser() as client:
        class_id = with_report(client)
        points_presence(client, result_named(client, class_id, LOG), SMUDGED)
        with pytest.raises(GradeValueUndecodable) as raised:
            client.get(address.format(class_id), headers=PAGE_HEADERS)

    assert isinstance(raised.value, ValueError)
    assert type(raised.value.__cause__) is ValueError
    assert SMUDGED in str(raised.value.__cause__)
    assert str(raised.value) == "a stored grade value can't be decoded"


NO_DAY = "0001-01-01T00:00:00+00:00"
"""A stored moment that falls before the calendar's first day where the household is."""
ADDED_AT = "UPDATE grade_reports SET imported_at = ?"
ACTED_AT = "UPDATE grade_current_actions SET acted_at = ?"
COPIED = "UPDATE grade_current_actions SET copied = ?"
WRONG_SHAPES = {
    "a list": '[["result","one"]]',
    "nothing": "null",
    "an object with no observations": '{"rows":[]}',
    "observations that are an object": '{"observations":{},"rows":[]}',
    "observations that are a text": '{"observations":"result","rows":[]}',
    "an entry of one text": '{"observations":[["result"]],"rows":[]}',
    "an entry of three texts": '{"observations":[["result","one","two"]],"rows":[]}',
    "an entry that is a text of two letters": '{"observations":["ab"],"rows":[]}',
    "a target that is a number": '{"observations":[["result",7]],"rows":[]}',
    "a kind no action copies": '{"observations":[["smudged","one"]],"rows":[]}',
    "a kind that is a list": '{"observations":[[["result"],"one"]],"rows":[]}',
    "observations that are an empty text": '{"observations":"","rows":[]}',
    "an entry that is an object of two keys": (
        '{"observations":[{"result":1,"category":2}],"rows":[]}'
    ),
}
"""Copied lists that are JSON and not the object an action writes: its ``observations`` a list
of pairs, each a kind the action copies and a target, both texts."""
MALFORMED = [
    pytest.param(ADDED_AT, SMUDGED, id="the moment a report was added"),
    pytest.param(ACTED_AT, SMUDGED, id="the moment an action made a report"),
    pytest.param(ADDED_AT, NO_DAY, id="a report added on no day of the calendar"),
    pytest.param(ACTED_AT, NO_DAY, id="a report made on no day of the calendar"),
    pytest.param(COPIED, SMUDGED, id="a copied list that is no JSON"),
    *(
        pytest.param(COPIED, shape, id=f"a copied list that is {name}")
        for name, shape in WRONG_SHAPES.items()
    ),
]
"""A report's stored moment that is no moment or has no day where the household is, and an
action's stored list of what it copied that is not the list an action writes: each statement
with the value it stores."""
DECODED = [
    pytest.param(
        "UPDATE grade_result_observations SET due_presence = ?",
        SMUDGED,
        ValueError,
        "the read",
        id="a result's cell",
    ),
    pytest.param(
        "UPDATE grade_category_observations SET weight_presence = ?",
        SMUDGED,
        ValueError,
        "the read",
        id="a category's cell",
    ),
    pytest.param(
        "UPDATE grade_term_observations SET letter_presence = ?",
        SMUDGED,
        ValueError,
        "the read",
        id="the term's cell",
    ),
    pytest.param(
        "UPDATE grade_reports SET acceptance_order = ? WHERE acceptance_order = 1",
        SMUDGED,
        type(None),
        "the read",
        id="a report's order",
    ),
    pytest.param(
        "UPDATE grade_reports SET result_rows = ?",
        SMUDGED,
        type(None),
        "the read",
        id="a report's number of rows",
    ),
    pytest.param(ADDED_AT, SMUDGED, ValueError, "the names", id="the moment a report was added"),
    pytest.param(
        ACTED_AT, SMUDGED, ValueError, "the names", id="the moment an action made a report"
    ),
    pytest.param(
        ADDED_AT, NO_DAY, OverflowError, "the names", id="a report added on no day of the calendar"
    ),
    pytest.param(
        ACTED_AT, NO_DAY, OverflowError, "the names", id="a report made on no day of the calendar"
    ),
    pytest.param(
        COPIED, SMUDGED, json.JSONDecodeError, "the read", id="a copied list that is no JSON"
    ),
    *(
        pytest.param(COPIED, shape, type(None), "the read", id=f"a copied list that is {name}")
        for name, shape in WRONG_SHAPES.items()
    ),
]


def made_current_again(client: TestClient, text: str = IN_WEEK) -> str:
    """The report ``text`` saved, a newer one with the log's score changed, and the first made
    current again by the class-details action, which keeps its moment and what it copied.
    The class."""
    class_id = with_report(client, text)
    with_report(
        client, text.replace(LOG_ROW, "| Seed Germination Log | 19.0    | 20.0    | 95.0    |")
    )
    made = confirm_current(store_of(client), draft_of(text))
    assert isinstance(made, MadeCurrent), made
    return class_id


@pytest.mark.parametrize(("statement", "value", "cause", "at"), DECODED)
def test_each_of_these_stored_grade_values_raises_the_one_named_error(
    statement: str, value: str, cause: type, at: str
) -> None:
    """Each of these values raises it from each read of the class and term, a homework page's grade
    read included; a report's moment raises it where the report is named. Its cause is the
    decoding's own error, when one was raised, and its message holds nothing stored."""
    draft = draft_of(IN_WEEK)
    with browser() as client:
        store = store_of(client)
        class_id = made_current_again(client)
        sound = read_grade_facts(store, [BIOLOGY])
        stored(store, statement, value)
        reads: dict[str, tuple[Callable[[], object], ...]] = {
            "the read": (
                lambda: store.class_term(class_id, "T1"),
                lambda: store.current_values(class_id, "T1"),
                lambda: store.review_grade_report(draft, capture_key(draft), key=KEY),
                lambda: store.preview_current(class_id, "T1", capture_key(draft)),
                lambda: read_grade_facts(store, [BIOLOGY]),
            ),
            "the names": (
                lambda: report_names(store.class_term(class_id, "T1").reports, ZONE),
                lambda: report_names(
                    store.review_grade_report(draft, capture_key(draft), key=KEY).reports, ZONE
                ),
                lambda: scores_shown(
                    [homework()],
                    {},
                    (),
                    read_grade_facts(store, [BIOLOGY]),
                    zone=ZONE,
                    base="/student/grades",
                ),
            ),
        }
        raised = []
        for read in reads[at]:
            with pytest.raises(GradeValueUndecodable) as caught:
                read()
            raised.append(caught.value)

    assert [list(year.reads) for year in sound] == [[(class_id, "T1")]]
    assert [type(error) for error in raised] == [GradeValueUndecodable] * len(raised)
    assert [type(error.__cause__) for error in raised] == [cause] * len(raised)
    assert {str(error) for error in raised} == {"a stored grade value can't be decoded"}


@pytest.mark.parametrize(("statement", "value"), MALFORMED)
def test_each_of_these_moments_and_copied_lists_leaves_her_pages_as_with_no_class_on_record(
    statement: str, value: str, caplog: pytest.LogCaptureFixture
) -> None:
    """With one of these moments or copied lists stored, her week and the details of two assignments
    with a result and of one with none each answer, show no score at all, and read as with no class
    on record. Each read records the failure's type alone, and the file stays as it was."""
    notes_id = "assignment-reading-notes"
    addresses = [
        HER_PAGE,
        *(f"/student/assignments/{one}" for one in (LOG_ID, DIAGRAM_ID, notes_id)),
    ]
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [
                homework(),
                homework(DIAGRAM_ID, DIAGRAM, date(2026, 8, 27), assigned=MONDAY),
                homework(notes_id, "Reading Notes", date(2026, 8, 19)),
            ],
            {},
            {LOG_ID: [school_said("missing", SourceChannel.LMS, date(2026, 8, 18))]},
        )
        reported(store, "done", DIAGRAM_ID)

        def read() -> list[str]:
            answers = [client.get(address, headers=PAGE_HEADERS) for address in addresses]
            assert [answer.status_code for answer in answers] == [200, 200, 200, 200]
            return [without_minted_fields(answer.text) for answer in answers]

        bare = read()
        made_current_again(client)
        whole = read()
        stored(store, statement, value)
        before = every_row(database_of(client))
        said, logged = [], []
        with caplog.at_level(logging.WARNING, logger="blossom"):
            for address in addresses:
                caplog.clear()
                answer = client.get(address, headers=PAGE_HEADERS)
                said.append((answer.status_code, without_minted_fields(answer.text)))
                logged.append((recorded(caplog), caplog.text))
        after = every_row(database_of(client))

    assert [[line for line, _, _ in scores(page)] for page in whole] == [
        [LINE, DIAGRAM_LINE],
        [LINE],
        [DIAGRAM_LINE],
        [],
    ]
    assert [status for status, _ in said] == [200, 200, 200, 200]
    for (_, page), with_no_class in zip(said, bare, strict=True):
        assert "School score" not in page
        assert "Grade details" not in page
        assert HEADING not in page
        assert not [said for said in ("no grades", "not grade") if said in page.lower()]
        assert without_scores(page) == without_scores(with_no_class)
    assert [records for records, _ in logged] == [[LEFT_OUT]] * 4
    for _, text in logged:
        assert not [word for word in (LOG, DIAGRAM, BIOLOGY, "18.0", value) if word in text]
    assert after == before


OTHER_TERM = [
    pytest.param(ADDED_AT, SMUDGED, id="the moment a report was added"),
    pytest.param(ACTED_AT, SMUDGED, id="the moment an action made a report"),
    pytest.param(ADDED_AT, NO_DAY, id="a report added on no day of the calendar"),
    pytest.param(COPIED, SMUDGED, id="a copied list that is no JSON"),
    pytest.param(
        COPIED, WRONG_SHAPES["observations that are an object"], id="observations an object"
    ),
    pytest.param(COPIED, WRONG_SHAPES["an entry of one text"], id="an entry of one text"),
]


@pytest.mark.parametrize(("statement", "value"), OTHER_TERM)
def test_each_of_these_values_in_a_term_no_card_shows_leaves_the_whole_page_without_scores(
    statement: str, value: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The value is in the class's second term, whose results are no homework's on her week.
    Her week and the details show no score, though the first term's own values decode and
    its reports are named."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = with_report(client)
        made_current_again(client, REPORT.replace("**T1**", "**T2**"))

        def read() -> list[str]:
            return [page_of(client), details_of(client)]

        whole = read()
        stored(store, f"{statement} WHERE term_label = ?", value, folded("T2"))
        with caplog.at_level(logging.WARNING, logger="blossom"):
            left = read()
        first_term = store.class_term(class_id, "T1")
        named = report_names(first_term.reports, ZONE)

    assert [[line for line, _, _ in scores(page)] for page in whole] == [[LINE], [LINE]]
    assert card_for(left[0], LOG_ID)
    assert not [page for page in left if "School score" in page or "Grade details" in page]
    assert recorded(caplog) == [LEFT_OUT] * 2
    assert len(named) == len(first_term.reports) == 1


def test_what_the_class_details_action_writes_decodes_and_the_scores_show(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The action's record of what it copied is an object whose observations are pairs of a
    kind and a target. With it on record her card and its details show the line of the report
    the action made, and no failure is recorded."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        made_current_again(client)
        with caplog.at_level(logging.WARNING, logger="blossom"):
            shown = [on_card(client), scores(details_of(client))]
        with store._lock:
            ((copied,),) = store._connection.execute(
                "SELECT copied FROM grade_current_actions"
            ).fetchall()

    written = json.loads(copied)
    assert sorted(written) == ["observations", "rows"]
    assert all(
        isinstance(entry, list) and len(entry) == 2 and all(isinstance(one, str) for one in entry)
        for entry in written["observations"]
    )
    assert "result" in {kind for kind, _ in written["observations"]}
    assert {kind for kind, _ in written["observations"]} <= {"term", "category", "result"}
    assert [[line for line, _, _ in one] for one in shown] == [[LINE], [LINE]]
    assert recorded(caplog) == []


@pytest.mark.parametrize("address", ["/student/grades", "/student/grades/classes/{}/terms/1"])
@pytest.mark.parametrize(("statement", "value"), MALFORMED)
def test_grades_and_class_details_answer_these_moments_and_copied_lists_with_the_named_error(
    statement: str, value: str, address: str
) -> None:
    """With one of these moments or copied lists stored, neither page leaves anything out: each
    fails with the named error, a ``ValueError``, with nothing stored in its message."""
    with browser() as client:
        class_id = made_current_again(client)
        sound = client.get(address.format(class_id), headers=PAGE_HEADERS)
        stored(store_of(client), statement, value)
        with pytest.raises(GradeValueUndecodable) as raised:
            client.get(address.format(class_id), headers=PAGE_HEADERS)

    assert sound.status_code == 200
    assert isinstance(raised.value, ValueError)
    assert str(raised.value) == "a stored grade value can't be decoded"


ADDRESSES = [HER_PAGE, f"/student/assignments/{LOG_ID}"]


@pytest.mark.parametrize("address", ADDRESSES)
@pytest.mark.parametrize("where", ["the read", "the lines"])
@pytest.mark.parametrize("kind", [ValueError, RecursionError, MemoryError])
def test_no_other_error_is_taken_for_a_grade_value_that_can_t_be_decoded(
    kind: type[Exception], where: str, address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``ValueError`` of any other kind, a ``RecursionError`` or a ``MemoryError``, from the
    grade read or from making the lines, fails the page as it does with no score to leave out."""

    def failed(*_: object, **__: object) -> None:
        msg = "another failure"
        raise kind(msg)

    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        with_report(client)
        if where == "the read":
            monkeypatch.setattr(store, "class_term", failed)
        else:
            monkeypatch.setattr("blossom.routes.homework_scores.school_scores", failed)
        with pytest.raises(kind, match="another failure") as raised:
            client.get(address, headers=PAGE_HEADERS)

    assert type(raised.value) is kind


@pytest.mark.parametrize("address", ADDRESSES)
def test_a_memory_error_where_a_copied_list_is_read_fails_the_page_as_itself(
    address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``MemoryError`` from the JSON reader, raised for an action's copied list alone, is no
    stored value that can't be decoded: the page fails with it, where it showed the score."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        made_current_again(client)
        sound = client.get(address, headers=PAGE_HEADERS)
        with store._lock:
            ((copied,),) = store._connection.execute("SELECT copied FROM grade_current_actions")

        def loads(text: str) -> object:
            if text == copied:
                msg = "another failure"
                raise MemoryError(msg)
            return json.loads(text)

        reading = types.SimpleNamespace(loads=loads, dumps=json.dumps)
        monkeypatch.setattr("blossom.stores.gradebook.json", reading)
        with pytest.raises(MemoryError, match="another failure"):
            client.get(address, headers=PAGE_HEADERS)

    assert sound.status_code == 200
    assert LINE in html.unescape(sound.text)


ORDER = "UPDATE grade_reports SET acceptance_order = ? WHERE acceptance_order = 1"
ROWS = "UPDATE grade_reports SET result_rows = ?"
DEEP = "[" * 200_000
"""A copied list nested past the depth the JSON reader follows."""
NOT_UTF8 = b"\xff"
BOUND = 20.0
"""The seconds a read gets to return before its test fails."""
WALK_BOUND = 5.0
"""The seconds ``original_of`` alone gets to end its walk of a chain of sources."""
UNDECODABLE = "a stored grade value can't be decoded"
NO_CAUSE = type(None)
"""What a named error's cause is when its decoding raised no error of its own."""


@dataclasses.dataclass(frozen=True)
class Edit:
    """One stored value written as no save writes it: the statement, what it binds, which reads
    raise the named error for it, its decoding's own error, when one was raised, whether it loops a
    chain of sources, and whether a parent's assertion is on record for it."""

    statement: str
    binds: tuple[object, ...] = ()
    at: str = "the class"
    cause: type = NO_CAUSE
    loops: bool = False
    asserted: bool = False


def not_utf8(table: str, column: str) -> Edit:
    """Bytes that are no UTF-8, stored as the text of ``column`` in every row of ``table``."""
    statement = f"UPDATE {table} SET {column} = CAST(? AS TEXT)"  # noqa: S608
    return Edit(statement, (NOT_UTF8,), "the score read", UnicodeDecodeError)


def as_bytes(table: str, column: str, at: str = "the class", *, asserted: bool = False) -> Edit:
    """The text of ``column`` stored as bytes in every row of ``table`` that holds one."""
    statement = f"UPDATE {table} SET {column} = CAST({column} AS BLOB)"  # noqa: S608
    return Edit(statement, at=at, asserted=asserted)


DIGITS = "9" * 5000
"""A whole number of more digits than Python reads into an integer."""

EDITS = {
    "an order that is infinity": Edit(ORDER, (float("inf"),)),
    "an order that is a fraction": Edit(ORDER, (3.5,)),
    "an order stored as bytes": Edit(ORDER, (b"1",)),
    "an order with an underscore": Edit(ORDER, ("0_0",)),
    "an order with a sign and a digit of another script": Edit(ORDER, ("-٣",)),
    "an order that is no number": Edit(ORDER, (SMUDGED,)),
    "a number of rows that is infinity": Edit(ROWS, (float("inf"),)),
    "a number of rows that is a fraction": Edit(ROWS, (3.5,)),
    "a number of rows stored as bytes": Edit(ROWS, (b"4",)),
    "a number of rows that is no number": Edit(ROWS, (SMUDGED,)),
    "a copied list nested too deeply": Edit(COPIED, (DEEP,), cause=RecursionError),
    "a result's title stored as bytes": Edit(
        "UPDATE grade_result_observations SET assignment_text = ?", (LOG.encode(),)
    ),
    "a result's points stored as bytes": Edit(
        "UPDATE grade_result_observations SET points_text = ?", (b"18.0",)
    ),
    "a category's name stored as bytes": Edit(
        "UPDATE grade_category_observations SET name_text = ?", (b"Labs",)
    ),
    "the term's letter stored as bytes": Edit(
        "UPDATE grade_term_observations SET letter_text = ?", (b"A",)
    ),
    "a first month that is a fraction": Edit(
        "UPDATE grade_years SET first_month = ?", (8.5,), "the month"
    ),
    "a result's title that is not UTF-8": not_utf8("grade_result_observations", "assignment_text"),
    "a result's presence that is not UTF-8": not_utf8(
        "grade_result_observations", "points_presence"
    ),
    "a category's name that is not UTF-8": not_utf8("grade_category_observations", "name_text"),
    "the term's percent that is not UTF-8": not_utf8("grade_term_observations", "percent_text"),
    "a term's label that is not UTF-8": not_utf8("grade_terms", "label"),
    "a class's name that is not UTF-8": not_utf8("grade_classes", "display_name"),
    "an alias's code that is not UTF-8": not_utf8("grade_class_aliases", "code"),
    "the moment a report was added that is not UTF-8": not_utf8("grade_reports", "imported_at"),
    "a copied list that is not UTF-8": not_utf8("grade_current_actions", "copied"),
    "an action whose source is the report it made": Edit(
        "UPDATE grade_current_actions SET source_report = report_made", loops=True
    ),
    "a report in a term with no row among its year's terms": Edit(
        "DELETE FROM grade_terms", at="the score read"
    ),
    "a report whose term only folds to its term's label": Edit(
        "UPDATE grade_reports SET term_label = ' T1'", at="the score read"
    ),
    "a copied list whose target is a number of 5,000 digits": Edit(
        COPIED, (f'{{"observations":[["result",{DIGITS}]],"rows":[]}}',), cause=ValueError
    ),
    "a copied list with a number of 5,000 digits under a key of its own": Edit(
        COPIED, (f'{{"observations":[],"rows":[],"n":{DIGITS}}}',), cause=ValueError
    ),
    "a copied list that is a number of 5,000 digits alone": Edit(
        COPIED, (DIGITS,), cause=ValueError
    ),
    "her student ID stored as bytes": as_bytes("grade_student", "student_id"),
    "her key check stored as bytes": Edit("UPDATE grade_student SET key_check = ?", (b"check",)),
    "a name form stored as bytes": as_bytes("grade_name_forms", "name_form"),
    "the term in use stored as bytes": as_bytes("grade_context", "term_label", "the score read"),
    "the school year in use stored as bytes": as_bytes(
        "grade_context", "year_label", "the score read"
    ),
    "a term's school year stored as bytes": as_bytes("grade_terms", "year_label", "the score read"),
    "a term's label stored as bytes": as_bytes("grade_terms", "label", "the score read"),
    "the moment a term was made stored as bytes": as_bytes(
        "grade_terms", "made_at", "the score read"
    ),
    "a class's school year stored as bytes": as_bytes(
        "grade_classes", "year_label", "the score read"
    ),
    "a class's ID stored as bytes": as_bytes("grade_classes", "class_id", "the score read"),
    "a class's name stored as bytes": as_bytes("grade_classes", "display_name", "the score read"),
    "an alias's name stored as bytes": as_bytes("grade_class_aliases", "name", "the score read"),
    "an alias's class stored as bytes": as_bytes(
        "grade_class_aliases", "class_id", "the score read"
    ),
    "an alias's code stored as bytes": as_bytes("grade_class_aliases", "code", "the score read"),
    "the moment an alias was added stored as bytes": as_bytes(
        "grade_class_aliases", "added_at", "the score read"
    ),
    "a report's class stored as bytes": as_bytes("grade_reports", "class_id", "the score read"),
    "a report's ID stored as bytes": as_bytes("grade_reports", "report_id"),
    "the moment a report was added stored as bytes": as_bytes(
        "grade_reports", "imported_at", "the score read"
    ),
    "the moment an action made a report stored as bytes": as_bytes(
        "grade_current_actions", "acted_at", "the score read"
    ),
    "a category's key stored as bytes": as_bytes("grade_category_observations", "category_key"),
    "a result's ID stored as bytes": as_bytes("grade_result_observations", "result_id"),
    "a match's evidence stored as bytes": as_bytes("grade_match_decisions", "evidence"),
    "a match's result stored as bytes": as_bytes("grade_match_decisions", "result_id"),
    "an assertion's target stored as bytes": as_bytes("grade_corrections", "target", asserted=True),
    "an assertion's text stored as bytes": as_bytes(
        "grade_corrections", "corrected_text", asserted=True
    ),
    "the report an action made stored as bytes": as_bytes("grade_current_actions", "report_made"),
    "an action's source stored as bytes": as_bytes("grade_current_actions", "source_report"),
    "a copied list stored as bytes": as_bytes("grade_current_actions", "copied"),
}
"""Stored values no save writes, each with the file's own checks in force: a report's order
or number of rows that is no whole number at or above its bound, a copied list nested too
deeply or holding a number of more digits than an integer reads, a cell's text that is not
text, a first month that is no whole month, text that is not UTF-8, an action whose chain of
sources comes back to itself, a report whose term has no row among its year's terms or only
folds to one, and a fetched text column stored as bytes."""
EDITED = [pytest.param(edit, id=name) for name, edit in EDITS.items()]


def on_record_for(client: TestClient, edit: Edit) -> str:
    """What ``made_current_again`` leaves on record, with a parent's assertion on the log's
    points when ``edit`` changes one. The class."""
    class_id = made_current_again(client)
    if edit.asserted:
        store = store_of(client)
        store.correct_value(
            class_id,
            "T1",
            page=correction_page(store, class_id, "result", result_named(client, class_id, LOG)),
            field="points",
            how="parent_assertion",
            value=GradeValue(text="20.0", presence=Presence.REPORTED),
            role="parent",
        )
    return class_id


def within[Answer](seconds: float, read: Callable[[], Answer]) -> Answer:
    """What ``read`` answers or raises, from a thread of its own that gets ``seconds``: a read
    that never returns fails its test."""
    answers: list[Answer] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            answers.append(read())
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "the read did not return"
    if failures:
        raise failures[0]
    return answers[0]


def walk_within[Answer](seconds: float, walk: Callable[[], Answer]) -> Answer:
    """What the pure ``walk`` answers or raises, watched line by line in the caller's own
    thread: one still going after ``seconds`` is stopped where it stands with a
    ``TimeoutError``, so no thread is left running it."""
    deadline = monotonic() + seconds

    def watch(
        _frame: types.FrameType, _event: str, _arg: object
    ) -> Callable[[types.FrameType, str, Any], Any] | None:
        if monotonic() > deadline:
            msg = "the walk did not end"
            raise TimeoutError(msg)
        return watch

    kept = sys.gettrace()
    sys.settrace(watch)
    try:
        return walk()
    finally:
        sys.settrace(kept)


def the_stored_chains_are_refused(client: TestClient) -> None:
    """The stored chains of sources, read through a connection of its own and walked by
    ``original_of`` alone, are each refused in time. Made before a read of the store, a walk
    that never ends fails the test here, with the store's lock never held."""
    connection = sqlite3.connect(database_of(client))
    try:
        rows = connection.execute(
            "SELECT report_made, source_report, copied FROM grade_current_actions"
        ).fetchall()
    finally:
        connection.close()
    copies = {
        str(made): (
            str(source),
            frozenset(
                (str(kind), str(target)) for kind, target in json.loads(copied)["observations"]
            ),
        )
        for made, source, copied in rows
    }
    walks = [
        (kind, target, made)
        for made, (_, copied) in copies.items()
        for kind, target in sorted(copied)
    ]
    assert walks
    for kind, target, made in walks:
        with pytest.raises(GradeValueUndecodable):
            walk_within(WALK_BOUND, functools.partial(original_of, kind, target, made, copies))


@pytest.mark.parametrize("edit", EDITED)
def test_each_of_these_hand_edited_grade_values_leaves_her_pages_as_with_no_class_on_record(
    edit: Edit, caplog: pytest.LogCaptureFixture
) -> None:
    """With one of these values stored, her week and the details of three assignments answer in
    time, show no score and read as with no class on record. Each read records the failure's type
    alone; the gradebook's rows and the connection's decoding stay as they were."""
    notes_id = "assignment-reading-notes"
    addresses = [
        HER_PAGE,
        *(f"/student/assignments/{one}" for one in (LOG_ID, DIAGRAM_ID, notes_id)),
    ]
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [
                homework(),
                homework(DIAGRAM_ID, DIAGRAM, date(2026, 8, 27), assigned=MONDAY),
                homework(notes_id, "Reading Notes", date(2026, 8, 19)),
            ],
            {},
            {LOG_ID: [school_said("missing", SourceChannel.LMS, date(2026, 8, 18))]},
        )
        reported(store, "done", DIAGRAM_ID)

        def read() -> list[str]:
            answers = [client.get(address, headers=PAGE_HEADERS) for address in addresses]
            assert [answer.status_code for answer in answers] == [200, 200, 200, 200]
            return [without_minted_fields(answer.text) for answer in answers]

        bare = read()
        on_record_for(client, edit)
        whole = read()
        stored(store, edit.statement, *edit.binds)
        if edit.loops:
            the_stored_chains_are_refused(client)
        before = {table: as_stored(store, table) for table in GRADEBOOK_TABLES}
        said, logged = [], []
        with caplog.at_level(logging.WARNING, logger="blossom"):
            for address in addresses:
                caplog.clear()
                answer = within(BOUND, functools.partial(client.get, address, headers=PAGE_HEADERS))
                said.append((answer.status_code, without_minted_fields(answer.text)))
                logged.append((recorded(caplog), caplog.text))
        after = {table: as_stored(store, table) for table in GRADEBOOK_TABLES}
        decoding = store._connection.text_factory

    private = [LOG, DIAGRAM, BIOLOGY, "18.0", *(one for one in edit.binds if isinstance(one, str))]
    assert [[line for line, _, _ in scores(page)] for page in whole] == [
        [LINE, DIAGRAM_LINE],
        [LINE],
        [DIAGRAM_LINE],
        [],
    ]
    assert [status for status, _ in said] == [200, 200, 200, 200]
    for (_, page), with_no_class in zip(said, bare, strict=True):
        assert "School score" not in page
        assert "Grade details" not in page
        assert HEADING not in page
        assert not [said for said in ("no grades", "not grade") if said in page.lower()]
        assert without_scores(page) == without_scores(with_no_class)
    assert [records for records, _ in logged] == [[LEFT_OUT]] * 4
    for _, text in logged:
        assert not [word for word in private if word in text]
    assert after == before
    assert decoding is text_or_refusal


@pytest.mark.parametrize("edit", EDITED)
def test_each_of_these_hand_edited_grade_values_raises_the_one_named_error_where_it_is_read(
    edit: Edit,
) -> None:
    """Each of these values raises it from the reads its entry names: each read of the class and
    term, the first month's own, or a homework page's grade read. Each returns in time, with the
    decoding's own error as its cause, when one was raised, and nothing stored in its message."""
    draft = draft_of(IN_WEEK)
    with browser() as client:
        store = store_of(client)
        class_id = on_record_for(client, edit)
        sound = read_grade_facts(store, [BIOLOGY])
        stored(store, edit.statement, *edit.binds)
        if edit.loops:
            the_stored_chains_are_refused(client)
        reads: dict[str, tuple[Callable[[], object], ...]] = {
            "the class": (
                lambda: store.class_term(class_id, "T1"),
                lambda: store.current_values(class_id, "T1"),
                lambda: store.review_grade_report(draft, capture_key(draft), key=KEY),
                lambda: store.preview_current(class_id, "T1", capture_key(draft)),
                lambda: read_grade_facts(store, [BIOLOGY]),
            ),
            "the month": (
                lambda: store.first_month_of(YEAR),
                lambda: read_grade_facts(store, [BIOLOGY]),
            ),
            "the score read": (lambda: read_grade_facts(store, [BIOLOGY]),),
        }
        raised = []
        for read in reads[edit.at]:
            with pytest.raises(GradeValueUndecodable) as caught:
                within(BOUND, read)
            raised.append(caught.value)
        decoding = store._connection.text_factory

    assert [list(year.reads) for year in sound] == [[(class_id, "T1")]]
    assert [type(error) for error in raised] == [GradeValueUndecodable] * len(raised)
    assert [type(error.__cause__) for error in raised] == [edit.cause] * len(raised)
    assert {str(error) for error in raised} == {UNDECODABLE}
    assert decoding is text_or_refusal


@pytest.mark.parametrize("address", ["/student/grades", "/student/grades/classes/{}/terms/1"])
@pytest.mark.parametrize(
    "edit", [pytest.param(edit, id=name) for name, edit in EDITS.items() if edit.at == "the class"]
)
def test_grades_and_class_details_answer_each_of_these_hand_edited_values_with_the_named_error(
    edit: Edit, address: str
) -> None:
    """Neither page leaves anything out: each fails in time with the named error, a
    ``ValueError``, with nothing stored in its message."""
    with browser() as client:
        class_id = on_record_for(client, edit)
        sound = client.get(address.format(class_id), headers=PAGE_HEADERS)
        stored(store_of(client), edit.statement, *edit.binds)
        if edit.loops:
            the_stored_chains_are_refused(client)
        with pytest.raises(GradeValueUndecodable) as raised:
            within(BOUND, lambda: client.get(address.format(class_id), headers=PAGE_HEADERS))

    assert sound.status_code == 200
    assert isinstance(raised.value, ValueError)
    assert str(raised.value) == UNDECODABLE


def test_class_details_answers_a_first_month_that_is_no_whole_month_with_the_named_error() -> None:
    """Class details resolves each due date under the year's first month and fails on one
    that is a fraction; Grades, which reads no first month, still answers."""
    edit = EDITS["a first month that is a fraction"]
    with browser() as client:
        class_id = with_report(client)
        stored(store_of(client), edit.statement, *edit.binds)
        grades = client.get("/student/grades", headers=PAGE_HEADERS)
        with pytest.raises(GradeValueUndecodable) as raised:
            class_details(client, class_id)

    assert grades.status_code == 200
    assert str(raised.value) == UNDECODABLE


def twin_terms(client: TestClient) -> None:
    """Two terms of one class, each with a result of the log's title and day."""
    with_report(client)
    with_report(client, IN_WEEK.replace("**T1**", "**T2**"))


def twin_years(client: TestClient) -> None:
    """Two school years with a class of one name and a result due 08/20, the earlier year
    starting in September and the later in August, so both fall on the log's day."""
    earlier = draft_of(IN_WEEK.replace(f"**{YEAR}**", "**2025-2026**"))
    store = store_of(client)
    review = store.review_grade_report(earlier, capture_key(earlier), key=KEY)
    saved = save_grade(
        store, earlier, key=KEY, review=review, answers=grade_answers(review, month=9)
    )
    assert isinstance(saved, GradeReportSaved)
    with_report(client)


COMPETITORS = [
    pytest.param(
        twin_terms,
        "UPDATE grade_result_observations SET assignment_text = CAST(assignment_text AS BLOB) "
        "WHERE report_id IN (SELECT report_id FROM grade_reports WHERE term_label = 'T2')",
        id="a twin's title stored as bytes",
    ),
    pytest.param(
        twin_years,
        "UPDATE grade_years SET first_month = 8.5 WHERE label = '2025-2026'",
        id="a twin year's first month a fraction",
    ),
    pytest.param(
        twin_terms,
        "DELETE FROM grade_terms WHERE label = 'T2'",
        id="a twin's term with no row among its year's terms",
    ),
    pytest.param(
        twin_terms,
        "UPDATE grade_reports SET term_label = ' T2' WHERE term_label = 'T2'",
        id="a twin's report whose term only folds to its term's label",
    ),
]


@pytest.mark.parametrize(("arrange", "statement"), COMPETITORS)
def test_these_hand_edited_competitors_are_refused_and_the_other_does_not_show(
    arrange: Callable[[TestClient], None], statement: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Two results could each be the log's, so the log shows no score. With one of them stored in
    one of these ways, which would drop it out of the count, the log still shows none, on her week
    and on its details, and each read records the failure."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        arrange(client)
        twins = [scores(page_of(client)), scores(details_of(client))]
        stored(store, statement)
        with caplog.at_level(logging.WARNING, logger="blossom"):
            left = [page_of(client), details_of(client)]

    assert twins == [[], []]
    assert card_for(left[0], LOG_ID)
    assert not [page for page in left if "School score" in page or "Grade details" in page]
    assert recorded(caplog) == [LEFT_OUT] * 2


def test_a_report_outside_its_year_s_terms_of_a_class_no_homework_names_changes_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only a class one of the homework names connects to is read for the scores: a report of
    another class in a term with no row leaves the page's line as it is."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        with_report(client)
        with_report(client, GEOMETRY.replace("**T1**", "**T2**"))
        stored(store, "DELETE FROM grade_terms WHERE label = 'T2'")
        with caplog.at_level(logging.WARNING, logger="blossom"):
            shown = [on_card(client), scores(details_of(client))]

    assert [[line for line, _, _ in one] for one in shown] == [[LINE], [LINE]]
    assert recorded(caplog) == []


def test_a_score_read_asks_once_for_the_terms_that_hold_a_report() -> None:
    """One statement names every report's year, class and term, however many years, classes
    and terms are on record, and a record a save wrote holds none outside its year's terms."""
    with browser() as client:
        store = store_of(client)
        biology = with_report(client)
        geometry = with_report(client, GEOMETRY.replace("**T1**", "**T2**"))
        seen: list[str] = []
        store._connection.set_trace_callback(seen.append)
        try:
            read = read_grade_facts(store, [BIOLOGY, "08 GEOMETRY"])
        finally:
            store._connection.set_trace_callback(None)
        held = store.terms_reported()

    asked = [statement for statement in seen if "r.term_label FROM grade_reports AS r" in statement]
    assert len(asked) == 1
    assert sorted(held) == sorted([(YEAR, biology, "T1"), (YEAR, geometry, "T2")])
    assert [sorted(year.reads) for year in read] == [sorted([(biology, "T1"), (geometry, "T2")])]


WHOLE = [(1, 1, 1), (7, 1, 7), (0, 0, 0), ("7", 1, 7), ("007", 1, 7), ("0", 0, 0)]
"""A stored order or number of rows, its column's bound, and the number it reads as."""
NOT_WHOLE = [
    float("inf"),
    3.5,
    2.0,
    b"2",
    "+2",
    "-2",
    "2_0",
    " 2",
    "2 ",
    "٣",
    "²",
    "",
    "two",
    None,
    "9" * 5000,
    -1,
]
"""Stored values that are no integer and no text of ASCII digits alone, or read below 0."""


def test_a_stored_order_or_row_count_reads_only_as_a_whole_number_at_or_above_its_bound() -> None:
    read = [_whole(stored, least) for stored, least, _ in WHOLE]
    refused = []
    for least, values in ((1, [*NOT_WHOLE, 0, "0"]), (0, NOT_WHOLE)):
        for value in values:
            with pytest.raises(GradeValueUndecodable) as raised:
                _whole(value, least)
            refused.append(str(raised.value))

    assert read == [number for *_, number in WHOLE]
    assert set(refused) == {UNDECODABLE}


def test_a_cell_s_stored_text_reads_as_written_and_a_value_that_is_no_text_is_refused() -> None:
    reported_as, blank = Presence.REPORTED.value, Presence.BLANK.value
    read = _cells(("points", "note"), ("b'18.0'", reported_as, "", blank))
    refused = []
    for value in (b"18.0", 18, 18.0, None):
        with pytest.raises(GradeValueUndecodable) as raised:
            _cells(("points",), (value, reported_as))
        refused.append(str(raised.value))

    assert read == {"points": (Presence.REPORTED, "b'18.0'"), "note": (Presence.BLANK, "")}
    assert set(refused) == {UNDECODABLE}


def test_a_stored_first_month_reads_only_as_a_whole_number_from_1_to_12() -> None:
    read = [_month(stored) for stored in range(1, 13)]
    refused = []
    for value in (8.5, 8.0, float("inf"), "8", b"8", SMUDGED, 0, 13, -1):
        with pytest.raises(GradeValueUndecodable) as raised:
            _month(value)
        refused.append(str(raised.value))
    with browser() as client:
        store = store_of(client)
        with_report(client)
        on_record = [store.first_month_of(year) for year in (YEAR, "1999-2000")]

    assert read == list(range(1, 13))
    assert set(refused) == {UNDECODABLE}
    assert on_record == [8, None]


def test_a_chain_of_sources_reads_to_its_first_report_and_one_that_comes_back_is_refused() -> None:
    """A chain that ends reads to the report that copied nothing of the observation. One that
    returns to a report already on it, the report itself or one further back, is refused in
    time; a report whose action copied none of the observation is followed nowhere."""
    copied = frozenset({("result", "one")})
    ends = {"third": ("second", copied), "second": ("first", copied)}
    loops = [
        {"first": ("first", copied)},
        {"first": ("second", copied), "second": ("first", copied)},
        {"third": ("second", copied), "second": ("first", copied), "first": ("second", copied)},
    ]
    refused = []
    for loop in loops:
        with pytest.raises(GradeValueUndecodable) as raised:
            walk_within(
                WALK_BOUND, functools.partial(original_of, "result", "one", next(iter(loop)), loop)
            )
        refused.append(str(raised.value))

    assert original_of("result", "one", "third", ends) == "first"
    assert original_of("result", "other", "third", ends) == "third"
    assert original_of("result", "other", "first", loops[0]) == "first"
    assert set(refused) == {UNDECODABLE}


def test_each_decoder_turns_only_its_own_error_into_the_named_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error of another kind, raised where a decoder reads its stored value, is raised as
    itself: a presence, a copied list's JSON, an order's digits, a moment and a text column."""

    def failed(*_: object, **__: object) -> None:
        msg = "another failure"
        raise RuntimeError(msg)

    class Digits(str):
        __slots__ = ()
        __int__ = failed

    class Stored(bytes):
        __slots__ = ()

        def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
            failed(encoding, errors)
            return ""

    reading = types.SimpleNamespace(loads=failed, JSONDecodeError=json.JSONDecodeError)
    monkeypatch.setattr("blossom.stores.gradebook.Presence", failed)
    monkeypatch.setattr("blossom.stores.gradebook.json", reading)
    monkeypatch.setattr(
        "blossom.grades.wording.datetime", types.SimpleNamespace(fromisoformat=failed)
    )
    decodings: list[Callable[[], object]] = [
        lambda: _cells(("points",), ("18.0", "reported")),
        lambda: _copied("{}"),
        lambda: _whole(Digits("7"), 1),
        lambda: spoken_day("2026-08-20T10:00:00+00:00", ZONE),
        lambda: _utf8(Stored(b"text")),
    ]
    raised = []
    for decoding in decodings:
        with pytest.raises(RuntimeError, match="another failure") as caught:
            decoding()
        raised.append(type(caught.value))

    assert raised == [RuntimeError] * 5


def test_text_is_decoded_strictly_for_a_score_read_alone_and_the_connection_keeps_its_own() -> None:
    """A homework page's grade read refuses text that is not UTF-8 with the named error and
    leaves the connection its own decoding. The same read made for another page gets the
    database's own error, as do Grades and class details."""
    edit = EDITS["a result's title that is not UTF-8"]
    with browser() as client:
        store = store_of(client)
        class_id = with_report(client)
        sound = read_grade_facts(store, [BIOLOGY])
        stored(store, edit.statement, *edit.binds)
        with pytest.raises(GradeValueUndecodable) as refused:
            read_grade_facts(store, [BIOLOGY])
        decoding = store._connection.text_factory
        others = []
        for read in (
            lambda: store.class_term(class_id, "T1"),
            lambda: client.get("/student/grades", headers=PAGE_HEADERS),
            lambda: class_details(client, class_id),
        ):
            with pytest.raises(sqlite3.OperationalError) as plain:
                read()
            others.append(type(plain.value))

    assert [list(year.reads) for year in sound] == [[(class_id, "T1")]]
    assert type(refused.value.__cause__) is UnicodeDecodeError
    assert decoding is text_or_refusal
    assert others == [sqlite3.OperationalError] * 3


@pytest.mark.parametrize("address", ADDRESSES)
def test_a_database_error_inside_the_score_read_gets_the_page_s_own_answer(
    address: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A file SQLite can't read just then, met while text is decoded strictly, is raised as
    itself: the page answers as it answers any record it can't read, and the connection
    keeps its own decoding of text."""

    def locked(*_: object, **__: object) -> None:
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        with_report(client)
        monkeypatch.setattr(store, "_class_reports", locked)
        with caplog.at_level(logging.WARNING, logger="blossom"):
            answer = client.get(address, headers=PAGE_HEADERS)
        decoding = store._connection.text_factory

    assert answer.status_code == 503
    assert "School score" not in answer.text
    assert LEFT_OUT not in recorded(caplog)
    assert [said for said in recorded(caplog) if said.endswith(": OperationalError")]
    assert decoding is text_or_refusal


@pytest.mark.parametrize(
    "shape",
    ["observations that are an empty text", "an entry that is an object of two keys"],
)
def test_a_copied_list_that_is_refused_stops_the_action_and_a_correction_and_writes_nothing(
    shape: str,
) -> None:
    """With an action's copied list on record in a shape no action writes, "Use saved values
    from this report as current" and a parent's correction each fail with the named error,
    and the file's rows stay as they were."""
    draft = draft_of(IN_WEEK)
    with browser() as client:
        store = store_of(client)
        class_id = made_current_again(client)
        preview = current_preview(store, draft)
        page = correction_page(store, class_id, "result", result_named(client, class_id, LOG))
        stored(store, COPIED, WRONG_SHAPES[shape])
        before = every_row(database_of(client))
        with pytest.raises(GradeValueUndecodable) as acted:
            confirm_current(store, draft, preview)
        with pytest.raises(GradeValueUndecodable) as corrected:
            store.correct_value(
                class_id,
                "T1",
                page=page,
                field="points",
                how="parent_assertion",
                value=GradeValue(text="20.0", presence=Presence.REPORTED),
                role="parent",
            )
        after = every_row(database_of(client))

    assert {str(acted.value), str(corrected.value)} == {UNDECODABLE}
    assert after == before


BY_CODE = "07 BIO - C"
"""The report's code for Biology, which a homework class name may be."""


def earlier_as_bytes(table: str, column: str) -> str:
    """The statement that stores ``column`` as bytes in the earlier year's rows of ``table``."""
    return (
        f"UPDATE {table} SET {column} = CAST({column} AS BLOB) "  # noqa: S608
        "WHERE year_label = '2025-2026'"
    )


DROPPED = [
    pytest.param(
        twin_years,
        BY_CODE,
        (earlier_as_bytes("grade_class_aliases", "class_id"),),
        id="an alias's class, the homework named by the alias's code",
    ),
    pytest.param(
        twin_years,
        BIOLOGY,
        (
            earlier_as_bytes("grade_classes", "display_name"),
            earlier_as_bytes("grade_class_aliases", "name"),
        ),
        id="a class's name and its alias's name",
    ),
    pytest.param(
        twin_years,
        BY_CODE,
        (earlier_as_bytes("grade_class_aliases", "code"),),
        id="an alias's code, the homework named by that code",
    ),
    pytest.param(
        twin_terms,
        BIOLOGY,
        ("UPDATE grade_reports SET report_id = CAST(report_id AS BLOB) WHERE term_label = 'T2'",),
        id="a report's ID",
    ),
    pytest.param(
        twin_years,
        BIOLOGY,
        (earlier_as_bytes("grade_classes", "year_label"),),
        id="a class's school year",
    ),
    pytest.param(
        twin_years,
        BIOLOGY,
        (earlier_as_bytes("grade_classes", "class_id"),),
        id="a class's ID",
    ),
    pytest.param(
        twin_terms,
        BIOLOGY,
        ("UPDATE grade_reports SET class_id = CAST(class_id AS BLOB) WHERE term_label = 'T2'",),
        id="a report's class",
    ),
    pytest.param(
        twin_years,
        BIOLOGY,
        ("UPDATE grade_classes SET class_id = NULL WHERE year_label = '2025-2026'",),
        id="a class's ID set to null",
    ),
    pytest.param(
        twin_terms,
        BIOLOGY,
        ("UPDATE grade_reports SET report_id = NULL WHERE term_label = 'T2'",),
        id="a report's ID set to null",
    ),
]
"""Two results that could each be the log's, the homework's class name, and the statements
that store an identifier of one of the two as bytes, which equal no text, or as a null."""


@pytest.mark.parametrize(("arrange", "course", "statements"), DROPPED)
def test_each_of_these_identifiers_as_bytes_or_null_is_refused_and_the_other_does_not_show(
    arrange: Callable[[TestClient], None],
    course: str,
    statements: tuple[str, ...],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two results could each be the log's, so the log shows no score. With one of these identifiers
    of one stored as bytes, or as a null in a key SQLite lets be null, the log still shows none, on
    her week and on its details, and each read records the failure."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(course=course)], {})
        arrange(client)
        twins = [scores(page_of(client)), scores(details_of(client))]
        for statement in statements:
            stored(store, statement)
        with caplog.at_level(logging.WARNING, logger="blossom"):
            left = [page_of(client), details_of(client)]

    assert twins == [[], []]
    assert card_for(left[0], LOG_ID)
    assert not [page for page in left if "School score" in page or "Grade details" in page]
    assert recorded(caplog) == [LEFT_OUT] * 2


def test_a_removed_connection_whose_name_is_stored_as_bytes_does_not_connect_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A parent removed the connection of "Biology": the answer holds no class, and its
    homework shows no score. With the answer's name stored as bytes, her week and details
    still show none, and each read records the failure."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        with_report(client)
        connected = [scores(page_of(client)), scores(details_of(client))]
        store.connect_homework_class(YEAR, BIOLOGY, shown=None, chosen=UNCONNECTED, role="parent")
        with caplog.at_level(logging.WARNING, logger="blossom"):
            removed = [scores(page_of(client)), scores(details_of(client))]
        answers = dict(store.homework_classes(YEAR).answers)
        quiet = recorded(caplog)
        caplog.clear()
        stored(store, "UPDATE grade_homework_classes SET name_key = CAST(name_key AS BLOB)")
        with caplog.at_level(logging.WARNING, logger="blossom"):
            left = [page_of(client), details_of(client)]

    assert [[line for line, _, _ in one] for one in connected] == [[LINE], [LINE]]
    assert removed == [[], []]
    assert answers == {name_key(BIOLOGY): None}
    assert quiet == []
    assert not [page for page in left if "School score" in page or "Grade details" in page]
    assert recorded(caplog) == [LEFT_OUT] * 2


def test_a_chosen_answer_whose_class_is_stored_as_bytes_leaves_her_pages_without_scores(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A parent connected "Geometry" to the class a report made, and its homework shows the
    score. With the answer's class stored as bytes, her week and details show none, and each
    read records the failure."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(course="Geometry")], {})
        class_id = with_report(client, GEOMETRY)
        store.connect_homework_class(YEAR, "Geometry", shown=None, chosen=class_id, role="parent")
        connected = lines_shown(client)
        stored(store, "UPDATE grade_homework_classes SET class_id = CAST(class_id AS BLOB)")
        with caplog.at_level(logging.WARNING, logger="blossom"):
            left = [page_of(client), details_of(client)]

    assert connected == [[LINE], [LINE]]
    assert card_for(left[0], LOG_ID)
    assert not [page for page in left if "School score" in page or "Grade details" in page]
    assert recorded(caplog) == [LEFT_OUT] * 2


@pytest.mark.parametrize(
    ("table", "column", "mapped"),
    [
        ("grade_classes", "class_id", True),
        ("grade_classes", "display_name", True),
        ("grade_reports", "class_id", False),
    ],
)
def test_each_read_of_a_year_s_classes_refuses_an_id_or_a_name_stored_as_bytes(
    table: str, column: str, mapped: bool
) -> None:
    """The classes of a term, which Grades lists, and the year's mapping of homework class names
    each raise the named error for each of these columns it fetches: a score read makes both, and
    either meets a class's ID or name first."""
    with browser() as client:
        store = store_of(client)
        class_id = with_report(client)
        sound = store.classes_in(YEAR, "T1")
        named = [one.class_id for one in store.homework_classes(YEAR).classes]
        stored(store, as_bytes(table, column).statement)
        reads: list[Callable[[], object]] = [lambda: store.classes_in(YEAR, "T1")]
        if mapped:
            reads.append(lambda: store.homework_classes(YEAR))
        refused = []
        for read in reads:
            with pytest.raises(GradeValueUndecodable) as raised:
                read()
            refused.append(str(raised.value))

    assert [(one.class_id, one.name, one.reported) for one in sound] == [(class_id, BIOLOGY, True)]
    assert named == [class_id]
    assert set(refused) == {UNDECODABLE}


FETCHED = [
    ("grade_context", "year_label", "the terms on record"),
    ("grade_context", "term_label", "the terms on record"),
    ("grade_terms", "year_label", "the terms on record"),
    ("grade_terms", "label", "the terms on record"),
    ("grade_terms", "made_at", "the terms on record"),
    ("grade_classes", "year_label", "the terms reported"),
    ("grade_reports", "term_label", "the terms reported"),
    ("grade_reports", "report_id", "the class's reports"),
]
"""A text column, and the read that fetches it before any other read of a score read does."""


@pytest.mark.parametrize(("table", "column", "read"), FETCHED)
def test_each_read_of_the_terms_and_reports_refuses_each_of_these_columns_stored_as_bytes(
    table: str, column: str, read: str
) -> None:
    """The terms on record, the terms that hold a report and a class's reports each raise the named
    error for each of these columns it fetches, stored as bytes, before a later read meets it."""
    with browser() as client:
        store = store_of(client)
        class_id = with_report(client)
        reads: dict[str, Callable[[], object]] = {
            "the terms on record": store.grade_contexts,
            "the terms reported": store.terms_reported,
            "the class's reports": lambda: store._class_reports(store.student_id(), class_id, "T1"),
        }
        contexts, reported_in = store.grade_contexts(), store.terms_reported()
        kept = store._class_reports(store.student_id(), class_id, "T1")
        stored(store, as_bytes(table, column).statement)
        with pytest.raises(GradeValueUndecodable) as raised:
            reads[read]()

    assert (contexts.current, contexts.terms) == ((YEAR, "T1"), ((YEAR, "T1"),))
    assert reported_in == ((YEAR, class_id, "T1"),)
    assert [report.acted_at for report in kept] == [None]
    assert str(raised.value) == UNDECODABLE


EARLIER_REPORT = "WHERE acceptance_order = 1"
"""The first report of ``made_current_again``, whose capture's newest report is the one the
action made."""


def test_a_class_s_reports_refuse_the_id_of_a_capture_s_earlier_report_stored_as_bytes() -> None:
    """The report's capture has a newer report, so the check of the report's own ID alone meets the
    bytes: the newest report of its capture is another, and that ID is text."""
    with browser() as client:
        store = store_of(client)
        class_id = made_current_again(client)
        student_id = store.student_id()
        sound = store._class_reports(student_id, class_id, "T1")
        stored(
            store,
            f"UPDATE grade_reports SET report_id = CAST(report_id AS BLOB) {EARLIER_REPORT}",  # noqa: S608
        )
        with pytest.raises(GradeValueUndecodable) as raised:
            store._class_reports(student_id, class_id, "T1")

    assert [report.latest_of_capture == report.report_id for report in sound] == [False, True, True]
    assert str(raised.value) == UNDECODABLE


NULLED = [
    ("UPDATE grade_classes SET class_id = NULL", "the classes of a term"),
    ("UPDATE grade_classes SET class_id = NULL", "the homework class names"),
    (f"UPDATE grade_reports SET report_id = NULL {EARLIER_REPORT}", "the class's reports"),  # noqa: S608
    (f"UPDATE grade_reports SET report_id = NULL {EARLIER_REPORT}", "the current values"),  # noqa: S608
]
"""A key SQLite lets be null, set to null, and a read that fetches it."""


@pytest.mark.parametrize(("statement", "read"), NULLED)
def test_a_null_in_a_class_s_id_or_a_report_s_id_is_refused_by_each_of_these_reads(
    statement: str, read: str
) -> None:
    """No save writes a null in a class's ID or a report's ID, and each of these reads refuses one
    where it would keep a null in a column that may hold nothing. The report is its capture's
    earlier one, so the check of its own ID alone meets the null."""
    with browser() as client:
        store = store_of(client)
        class_id = made_current_again(client)
        student_id = store.student_id()
        reads: dict[str, Callable[[], object]] = {
            "the classes of a term": lambda: store.classes_in(YEAR, "T1"),
            "the homework class names": lambda: store.homework_classes(YEAR),
            "the class's reports": lambda: store._class_reports(student_id, class_id, "T1"),
            "the current values": lambda: store.current_values(class_id, "T1"),
        }
        reads[read]()
        stored(store, statement)
        with pytest.raises(GradeValueUndecodable) as raised:
            reads[read]()

    assert str(raised.value) == UNDECODABLE


def header_without(text: str, line: str) -> GradeReportDraft:
    """The report ``text`` as read, with ``line`` of its header left out."""
    draft = draft_of(text)
    return draft.model_copy(update={"header": draft.header.model_copy(update={line: None})})


def lines_shown(client: TestClient) -> list[list[str]]:
    """The score lines on her week's card for the log and on the log's details."""
    return [[line for line, _, _ in one] for one in (on_card(client), scores(details_of(client)))]


def test_a_report_with_no_class_name_keeps_an_alias_with_no_name_and_connects_by_its_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    unnamed = header_without(IN_WEEK, "class_name")
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(course=BY_CODE)], {})
        saved = save_grade(store, unnamed, key=KEY)
        with caplog.at_level(logging.WARNING, logger="blossom"):
            shown = lines_shown(client)
        aliases = [
            (alias.code, alias.name)
            for one in store.homework_classes(YEAR).classes
            for alias in one.aliases
        ]

    assert isinstance(saved, GradeReportSaved)
    assert aliases == [(BY_CODE, None)]
    assert shown == [[LINE], [LINE]]
    assert recorded(caplog) == []


def test_a_student_with_no_key_check_and_no_name_form_reads_as_one_and_the_score_shows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    unsigned = header_without(IN_WEEK, "student_line")
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        saved = save_grade(store, unsigned, key=KEY)
        with caplog.at_level(logging.WARNING, logger="blossom"):
            shown = lines_shown(client)
        _, check, forms = store._her_name_record()

    assert isinstance(saved, GradeReportSaved)
    assert (check, forms) == (None, [])
    assert shown == [[LINE], [LINE]]
    assert recorded(caplog) == []


def answered_different(client: TestClient) -> str:
    """The report, then a newer one that asks whether a changed row is one on record, saved
    with a parent's answer "A different assignment". The class."""
    asking = draft_of(
        IN_WEEK.replace(
            "| Cell Diagram             | 7.0 ", "| Cell Diagram             | EX  "
        ).replace("| Missing    | 08/27   |", "| Missing    | 08/28   |")
    )
    store = store_of(client)
    class_id = with_report(client)
    review = store.review_grade_report(asking, capture_key(asking), key=KEY)
    ((row, question),) = [
        (item.key, item.question) for item in review.rows if item.question is not None
    ]
    different = MatchAnswer(row, question.ids, None)
    answers = dataclasses.replace(grade_answers(review), matches=(different,))
    saved = save_grade(store, asking, key=KEY, review=review, answers=answers)
    assert isinstance(saved, GradeReportSaved), saved
    return class_id


SOURCED = f"{LINE}, from the report added August 19"
"""The log's line while a newer current report stands beside the one its score is from."""


def test_a_report_no_action_made_has_no_moment_of_one_and_a_match_may_hold_no_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After "A different assignment" the match holds no result, the other matches turned
    nothing down, and neither report has an action's moment. Each reads as none, and the
    log's score stands with its source."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = answered_different(client)
        with caplog.at_level(logging.WARNING, logger="blossom"):
            shown = lines_shown(client)
        read = store.class_term(class_id, "T1")
        held = store._scope_held(store.student_id(), class_id, "T1")

    assert [report.acted_at for report in read.reports] == [None, None]
    assert [result for _, _, result, _, _ in held.decided].count(None) == 1
    assert [rejected is None for _, _, _, _, rejected in held.decided].count(False) == 1
    assert None in [rejected for _, _, _, _, rejected in held.decided]
    assert shown == [[SOURCED]] * 2
    assert recorded(caplog) == []


def test_what_a_match_turned_down_stored_as_bytes_leaves_her_pages_without_scores(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one match that turned a result down keeps it as text. Stored as bytes, her week
    and details show no score, each read records the failure, and the class's own read
    raises the named error."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = answered_different(client)
        shown = lines_shown(client)
        stored(store, "UPDATE grade_match_decisions SET rejected = CAST(rejected AS BLOB)")
        with caplog.at_level(logging.WARNING, logger="blossom"):
            left = [page_of(client), details_of(client)]
        with pytest.raises(GradeValueUndecodable) as raised:
            store.class_term(class_id, "T1")

    assert shown == [[SOURCED]] * 2
    assert card_for(left[0], LOG_ID)
    assert not [page for page in left if "School score" in page or "Grade details" in page]
    assert recorded(caplog) == [LEFT_OUT] * 2
    assert str(raised.value) == UNDECODABLE


def test_a_year_with_no_first_month_and_a_file_with_no_term_in_use_read_as_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A save that leaves a year's first month unconfirmed keeps none, and its results stay
    in the count. A file whose term in use is gone has none, and the score still shows."""
    earlier = draft_of(IN_WEEK.replace(f"**{YEAR}**", "**2025-2026**"))
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        fresh = store.grade_contexts().current
        with caplog.at_level(logging.WARNING, logger="blossom"):
            before = lines_shown(client)
            with_report(client)
            stored(store, "DELETE FROM grade_context")
            alone = lines_shown(client)
            in_use = store.grade_contexts().current
            review = store.review_grade_report(earlier, capture_key(earlier), key=KEY)
            saved = save_grade(
                store, earlier, key=KEY, review=review, answers=grade_answers(review, month=None)
            )
            beside = lines_shown(client)
        months = [store.first_month_of(year) for year in ("2025-2026", YEAR)]

    assert isinstance(saved, GradeReportSaved)
    assert (fresh, in_use) == (None, None)
    assert months == [None, 8]
    assert [before, alone, beside] == [[[], []], [[LINE], [LINE]], [[], []]]
    assert recorded(caplog) == []


PRESENCES = frozenset(presence.value for presence in Presence)
SOUND_MOMENT = "2026-08-20T10:00:00+00:00"
SOUND_COPIED = {
    '{"observations":[["result","one"],["term","term"]],"rows":[]}': frozenset(
        {("result", "one"), ("term", "term")}
    ),
    '{"observations":[],"rows":[]}': frozenset(),
}
"""Copied lists as an action writes them, each with what it lists."""
CORPUS: tuple[object, ...] = (
    None,
    0,
    1,
    7,
    8,
    12,
    13,
    -1,
    2**63 - 1,
    0.0,
    2.0,
    3.5,
    8.5,
    1e300,
    float("inf"),
    float("-inf"),
    float("nan"),
    b"",
    b"7",
    b"reported",
    NOT_UTF8,
    "Biología".encode(),
    b'{"observations":[],"rows":[]}',
    "",
    " ",
    "7",
    "007",
    "0",
    "+7",
    "-7",
    "7_0",
    " 7",
    "٣",
    "²",
    "0x10",
    "1e3",
    "9" * 4300,
    "9" * 4301,
    DIGITS,
    *sorted(PRESENCES),
    "Reported",
    SOUND_MOMENT,
    NO_DAY,
    "9999-12-31T23:59:59+00:00",
    "9999-12-31T23:59:59-14:00",
    "2026-13-01T00:00:00+00:00",
    SMUDGED,
    "08/20",
    "02/30",
    YEAR,
    "2026-2028",
    DEEP,
    '{"observations":' * 100_000,
    f'{{"observations":[["result",{DIGITS}]],"rows":[]}}',
    f'{{"observations":[],"rows":[],"n":{DIGITS}}}',
    *WRONG_SHAPES.values(),
    *SOUND_COPIED,
    "NaN",
)
"""Stored values of every storage class, the hazards met so far among them: nothing, whole
numbers, floats with both infinities and no number, bytes that are and aren't UTF-8, a JSON
document as bytes, text that is empty, signed, underscored, in another script's digits or of
4,300, 4,301 and 5,000 digits, moments at the calendar's edges, documents nested past
reading or holding a number of 5,000 digits, each wrong shape of a copied list, and a
well-formed value of each decoder's kind."""


def is_anything(_: object) -> bool:
    return True


def is_text(value: object) -> bool:
    return type(value) is str


def is_bytes(value: object) -> bool:
    return type(value) is bytes


def is_utf8(value: object) -> bool:
    try:
        return isinstance(value, bytes) and value.decode("utf-8") is not None
    except UnicodeDecodeError:
        return False


def whole_of(value: object) -> int | None:
    """The number a stored order or row count is, as the test reads it: an integer, or ASCII
    digits of no more than 4,300; None for any other value."""
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,4300}", value):
        return int(value)
    return None


def at_least(least: int) -> Callable[[object], bool]:
    def sound(value: object) -> bool:
        number = whole_of(value)
        return number is not None and number >= least

    return sound


def is_month(value: object) -> bool:
    return type(value) is int and 1 <= value <= 12


def dates_of(text: str) -> list[date | None]:
    """``text`` as a due cell under each first month a year may hold, and as a year's label."""
    return [
        *(due_date_of((Presence.REPORTED, text), YEAR, month) for month in (None, *range(1, 13))),
        due_date_of((Presence.REPORTED, "08/20"), text, 8),
    ]


def first_of(report: str) -> str:
    """``original_of`` from ``report`` along a chain of two sources that ends, in time."""
    copied = frozenset({("result", "one")})
    copies = {report: ("a source", copied), "a source": ("the first", copied)}
    return walk_within(WALK_BOUND, functools.partial(original_of, "result", "one", report, copies))


@dataclasses.dataclass(frozen=True)
class Decoding:
    """A decoder as the store calls it: which stored values it can be handed, the call, what
    a value it returns for one must be, and which of them it must read."""

    handed: Callable[[object], bool]
    call: Callable[[Any], object]
    reads_as: Callable[[Any, Any], bool]
    sound: Callable[[object], bool]


DECODINGS = {
    "a text column": Decoding(
        is_anything,
        lambda value: gradebook._text(value),
        lambda value, read: read is value and type(read) is str,
        is_text,
    ),
    "a text column that may hold nothing": Decoding(
        is_anything,
        lambda value: gradebook._text_or_none(value),
        lambda value, read: read is value and (read is None or type(read) is str),
        lambda value: value is None or is_text(value),
    ),
    "a cell's text": Decoding(
        is_anything,
        lambda value: _cells(("points",), (value, Presence.REPORTED.value)),
        lambda value, read: type(value) is str and read == {"points": (Presence.REPORTED, value)},
        is_text,
    ),
    "a cell's presence": Decoding(
        is_anything,
        lambda value: _cells(("points",), ("18.0", value)),
        lambda value, read: value in PRESENCES and read == {"points": (Presence(value), "18.0")},
        lambda value: value in PRESENCES,
    ),
    "a report's order": Decoding(
        is_anything,
        lambda value: _whole(value, 1),
        lambda value, read: type(read) is int and read >= 1 and read == whole_of(value),
        at_least(1),
    ),
    "a report's number of rows": Decoding(
        is_anything,
        lambda value: _whole(value, 0),
        lambda value, read: type(read) is int and read >= 0 and read == whole_of(value),
        at_least(0),
    ),
    "a year's first month": Decoding(
        is_anything, _month, lambda value, read: is_month(value) and read == value, is_month
    ),
    "text decoded strictly": Decoding(
        is_bytes,
        _utf8,
        lambda value, read: type(read) is str and read.encode("utf-8") == value,
        is_utf8,
    ),
    "a report's moment": Decoding(
        is_text,
        lambda value: spoken_day(value, ZONE),
        lambda value, read: type(read) is str and (value != SOUND_MOMENT or read == "August 20"),
        lambda value: value == SOUND_MOMENT,
    ),
    "an action's copied list": Decoding(
        is_text,
        _copied,
        lambda value, read: value in SOUND_COPIED and read == SOUND_COPIED[value],
        lambda value: value in SOUND_COPIED,
    ),
    "a due date": Decoding(
        is_text,
        dates_of,
        lambda value, read: (
            all(one is None or type(one) is date for one in read)
            and (value != "08/20" or read[8] == THURSDAY)
        ),
        is_text,
    ),
    "a chain of sources": Decoding(is_text, first_of, lambda _, read: read == "the first", is_text),
}
"""Each decoder a score read runs, over the stored values it can be handed."""


@pytest.mark.parametrize("name", DECODINGS)
def test_each_decoder_reads_or_refuses_each_stored_value_of_one_fixed_set(name: str) -> None:
    """One fixed set of stored values through a decoder as the store calls it: each call returns a
    value of the decoder's kind or raises the named error, and each value marked well-formed is
    read. It shows these values alone and proves nothing of any other."""
    decoding = DECODINGS[name]
    read, misread, refused, said = [], [], [], set()
    for at, value in enumerate(CORPUS):
        if not decoding.handed(value):
            continue
        try:
            answer = decoding.call(value)
        except GradeValueUndecodable as error:
            said.add(str(error))
            refused.append(at)
            continue
        read.append(at)
        if not decoding.reads_as(value, answer):
            misread.append(at)

    assert read
    assert misread == []
    assert [at for at in refused if decoding.sound(CORPUS[at])] == []
    assert said <= {UNDECODABLE}


RESULTS_ID = "school-results"
"""The id of the details' gradebook heading when it lists several results, where a row's See
the results link lands."""
CONFLICT = "a homework page shows no school score: HomeworkLinksConflict"
"""The one record a page read makes when two confirmed links name one result."""
SEVERAL = re.compile(
    r'<p class="effect">School results \((\d+)\)</p>\s*<p class="details-link">'
    r'<a class="assignment-link" href="([^"]*)" aria-label="([^"]*)">See the results</a></p>'
)
"""The count of an assignment's results with the link under it, as each place writes them."""
EACH_RESULT = re.compile(
    r'<h3 class="result-title"><span class="authored-text">([^<]*)</span></h3>\s*'
    r'<p class="effect">(School score[^<]*)</p>\s*<p class="details-link">'
    r'<a class="assignment-link" href="([^"]*)" aria-label="([^"]*)">Grade details</a></p>'
)
"""One of several results on the details: its title, its line, and its own link."""
NOTEBOOK = "Lab Notebook"
"""A homework title no result has, so only a link can show a score on it."""
SECOND_TERM = IN_WEEK.replace("**T1**", "**T2**")
NEWER_LOG = "| Seed Germination Log | 19.0    | 20.0    | 95.0    |"
NEWER_LINE = "School score: 19.0 / 20.0 · 95.0%"
LINK_ROW = "INSERT INTO grade_homework_links VALUES (?, ?, ?, 'confirmed', 'parent', ?)"
LINKED_AT = "2026-08-19T12:00:00+00:00"
DROP_LINKS_INDEX = "DROP INDEX grade_homework_links_one_homework"
HER_PAGES = [HER_PAGE, f"/student/assignments/{LOG_ID}"]


def parent_links(
    client: TestClient,
    class_id: str,
    result_id: str,
    assignment_id: str = LOG_ID,
    *,
    term: str = "T1",
    decision: LinkDecision = "link",
) -> None:
    """A parent's decision about a result and a homework, made by the store's own write."""
    store = store_of(client)
    preview = store.delete_preview(class_id, term)
    assert isinstance(preview, DeletePreview)
    assert preview.revision is not None
    outcome = store.decide_homework_link(
        result_id, assignment_id, decision=decision, revision=preview.revision, role="parent"
    )
    assert isinstance(outcome, HomeworkLinkWritten), outcome


def several(fragment: str) -> Shown:
    """Each count of results in a piece of a page, with its link's address and name."""
    return [
        (count, html.unescape(href), html.unescape(name))
        for count, href, name in SEVERAL.findall(fragment)
    ]


def details_address(fragment: str) -> str:
    """The address of the Details link in a card or a row."""
    found = re.search(r'href="([^"]*)" aria-label="Details: ', fragment)
    assert found is not None
    return html.unescape(found.group(1))


def as_rendered(client: TestClient, address: str) -> str:
    """A page with the fields each render mints left blank, so two renders compare equal."""
    answer = client.get(address, headers=PAGE_HEADERS)
    assert answer.status_code == 200
    page = without_minted_fields(answer.text)
    return re.sub(r'(name="request_id" value=")[0-9a-f]{32}', r"\1", page)


def without_results(page: str) -> str:
    """A page with every school score block taken out: one result's line and link, the count
    of several with its link, and the details' section, whichever it holds."""
    page = re.sub(
        rf'\n *<section>\s*<h2 class="update-heading"[^>]*>{HEADING}</h2>.*?</section>',
        "",
        page,
        flags=re.S,
    )
    page = re.sub(r"\n *" + SEVERAL.pattern, "", page)
    return re.sub(r"\n *" + SCORE.pattern, "", page)


def order_on_class_details(page: str) -> list[str]:
    """Each result's ID on a class details page, in the order the page lists its cards."""
    return re.findall(r'<h3 class="card-title" id="(result-[0-9a-f]+)" tabindex="-1">', page)


def conflicting(client: TestClient, result_id: str) -> None:
    """The file changed by hand: the links index dropped, and two confirmed rows stored that
    name ``result_id``, one for the log's homework and one for homework not on record."""
    store = store_of(client)
    stored(store, DROP_LINKS_INDEX)
    for assignment_id in (LOG_ID, "assignment-not-on-record"):
        stored(store, LINK_ROW, store.student_id(), result_id, assignment_id, LINKED_AT)


def test_with_no_link_on_record_each_place_writes_its_line_and_link_in_these_exact_bytes() -> None:
    """A card, the card outside the week shown, the rows due later, active and done, the
    earlier-work rows and the details: each writes the text below, spaces and line breaks
    included, and the page without it is the page with no class on record."""
    rows = {LOG_ID: LOG, DIAGRAM_ID: DIAGRAM, PRACTICE_ID: PRACTICE, OSMOSIS_ID: OSMOSIS}
    lines = {
        LOG: LINE,
        DIAGRAM: DIAGRAM_LINE,
        PRACTICE: "School score: 27.0 / 30.0 · 90.0%",
        OSMOSIS: "School score: 31.0 / 40.0 · 77.5%",
    }
    apart = f"{HER_PAGE}?week=2026-09-07&change={LOG_ID}"
    addresses = [HER_PAGE, apart, f"/student/assignments/{LOG_ID}"]
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [
                homework(),
                homework(DIAGRAM_ID, DIAGRAM, date(2026, 8, 27), assigned=date(2026, 8, 18)),
                homework(PRACTICE_ID, PRACTICE, date(2026, 8, 12)),
                homework(OSMOSIS_ID, OSMOSIS, date(2026, 8, 1)),
            ],
            {},
        )
        bare = [as_rendered(client, address) for address in addresses]
        class_id = with_report(client)
        whole = [as_rendered(client, address) for address in addresses]
        ids = {title: result_named(client, class_id, title) for title in rows.values()}
        reported(store, "done", LOG_ID)
        reported(store, "done", DIAGRAM_ID)
        whole_done = as_rendered(client, HER_PAGE)
        links = as_stored(store, "grade_homework_links")
        preview = store.delete_preview(class_id, "T1")
        assert isinstance(preview, DeletePreview)
        store.delete_class_term(class_id, "T1", revision=preview.revision, role="parent")
        bare_done = as_rendered(client, HER_PAGE)

    def written(title: str, indent: int) -> str:
        pad = " " * indent
        return (
            f'\n{pad}<p class="effect">{lines[title]}</p>'
            f'\n{pad}<p class="details-link"><a class="assignment-link" '
            f'href="{link_to(class_id, ids[title])}" '
            f'aria-label="Grade details: {title}, {BIOLOGY}">Grade details</a></p>'
        )

    week = [written(LOG, 6), written(DIAGRAM, 12), written(PRACTICE, 2), written(OSMOSIS, 2)]
    done = [written(LOG, 6), written(DIAGRAM, 16), written(PRACTICE, 2), written(OSMOSIS, 2)]
    section = (
        f'\n    <section>\n      <h2 class="update-heading">{HEADING}</h2>{written(LOG, 6)}'
        "\n    </section>"
    )
    assert links == []
    for page, without, pieces in (
        (whole[0], bare[0], week),
        (whole[1], bare[1], [written(LOG, 6), written(PRACTICE, 2), written(OSMOSIS, 2)]),
        (whole[2], bare[2], [section]),
        (whole_done, bare_done, done),
    ):
        for piece in pieces:
            assert page.count(piece) == 1, piece
            page = page.replace(piece, "")
        assert page == without


def test_a_link_of_the_pair_the_rules_already_show_changes_no_byte_of_her_pages() -> None:
    """Her week and the details read the same with the one result standing by the rules and
    standing by a parent's link."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        by_the_rules = [as_rendered(client, address) for address in HER_PAGES]
        parent_links(client, class_id, result_named(client, class_id, LOG))
        rows = as_stored(store_of(client), "grade_homework_links")
        by_a_link = [as_rendered(client, address) for address in HER_PAGES]

    assert len(rows) == 1
    assert [len(scores(page)) for page in by_the_rules] == [1, 1]
    assert by_a_link == by_the_rules


def test_each_place_shows_the_count_of_an_assignment_s_results_and_one_way_to_them() -> None:
    """Seven places on her week, each for an assignment linked to a result in each of two terms,
    show the count and one link to the results on the row's own details. With the score blocks
    out, her week and each details page equal those with no class on record; reads write nothing."""
    rows = {LOG_ID: LOG, DIAGRAM_ID: DIAGRAM, PRACTICE_ID: PRACTICE, OSMOSIS_ID: OSMOSIS}
    addresses = [
        HER_PAGE,
        f"{HER_PAGE}?week=2026-09-07&change={LOG_ID}",
        *(f"/student/assignments/{one}" for one in rows),
    ]
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [
                homework(),
                homework(DIAGRAM_ID, DIAGRAM, date(2026, 8, 27), assigned=date(2026, 8, 18)),
                homework(PRACTICE_ID, PRACTICE, date(2026, 8, 12)),
                homework(OSMOSIS_ID, OSMOSIS, date(2026, 8, 1)),
            ],
            {},
        )
        bare = [as_rendered(client, address) for address in addresses]
        class_id = with_report(client)
        with_report(client, SECOND_TERM)
        unlinked = scores(page_of(client)) + several(page_of(client))
        for assignment_id, title in rows.items():
            for term in ("T1", "T2"):
                result_id = result_named(client, class_id, title, term)
                parent_links(client, class_id, result_id, assignment_id, term=term)
        on_record = every_row(database_of(client))
        linked = [as_rendered(client, address) for address in addresses]
        after_reading = every_row(database_of(client))
        week = page_of(client)
        apart = page_of(client, week="2026-09-07", change=LOG_ID)
        reported(store, "done", LOG_ID)
        reported(store, "done", DIAGRAM_ID)
        done_week = page_of(client)
        done_linked = as_rendered(client, HER_PAGE)
        for term in ("T1", "T2"):
            preview = store.delete_preview(class_id, term)
            assert isinstance(preview, DeletePreview)
            store.delete_class_term(class_id, term, revision=preview.revision, role="parent")
        done_bare = as_rendered(client, HER_PAGE)

    later, done_later = (
        page[page.index('<section class="panel assigned">') :] for page in (week, done_week)
    )
    outside = apart[apart.index("<h2>Outside the week shown</h2>") :]
    fold = week[week.index('<details class="steps earlier-more"') :]
    places = {
        "an active card": (card_for(week, LOG_ID), LOG),
        "the card outside the week shown": (card_for(outside, LOG_ID), LOG),
        "a Done card": (card_for(done_week, LOG_ID), LOG),
        "a row due later": (card_for(later, DIAGRAM_ID), DIAGRAM),
        "a done row due later": (card_for(done_later, DIAGRAM_ID), DIAGRAM),
        "an earlier-work row": (earlier_row(week, PRACTICE), PRACTICE),
        "a folded earlier-work row": (earlier_row(fold, OSMOSIS), OSMOSIS),
    }
    assert unlinked == []
    for place, (fragment, title) in places.items():
        landing = f"{details_address(fragment)}#{RESULTS_ID}"
        assert several(fragment) == [("2", landing, f"See the results: {title}, {BIOLOGY}")], place
        assert "School score" not in fragment, place
        assert "Grade details" not in fragment, place
    assert len(several(week)) == 4
    assert scores(week) == []
    assert [len(several(page)) for page in linked[:2]] == [4, 3]
    assert [len(scores(page)) for page in linked[2:]] == [2, 2, 2, 2]
    assert [without_results(page) for page in linked] == bare
    assert len(several(done_linked)) == 4
    assert without_results(done_linked) == done_bare
    assert after_reading == on_record


@pytest.mark.parametrize(
    ("reader", "tree"), [("her", "student"), ("parent", "parent"), ("open", "student")]
)
def test_the_details_list_each_result_with_its_title_its_line_and_its_own_link(
    reader: str, tree: str, tmp_path: pathlib.Path
) -> None:
    """Three results linked to one assignment, two in one term and one in the next: each has its
    title, class details' line and a link to its own term on the reader's tree. No word says who
    chose them, the section holds no form or button, and the newer of one term's two is first."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        store_of(client).put_on_record([homework(title=NOTEBOOK)], {})
        class_id = with_report(client)
        with_report(client, SECOND_TERM.replace(LOG_ROW, NEWER_LOG))
        first, diagram = (result_named(client, class_id, title) for title in (LOG, DIAGRAM))
        second = result_named(client, class_id, LOG, "T2")
        for result_id, term in ((second, "T2"), (first, "T1"), (diagram, "T1")):
            parent_links(client, class_id, result_id, term=term)
        details = details_of(client)
        card = card_for(page_of(client), LOG_ID)
        pages = [class_details(client, class_id, n, tree) for n in (1, 2)]

    heading = f'<h2 class="update-heading" id="{RESULTS_ID}" tabindex="-1">{HEADING}</h2>'
    section = details[details.index(heading) :]
    section = section[: section.index("</section>")]
    listed = [
        tuple(html.unescape(part) for part in found) for found in EACH_RESULT.findall(section)
    ]
    assert details.count(heading) == 1
    assert listed == [
        (
            DIAGRAM,
            DIAGRAM_LINE,
            link_to(class_id, diagram, 1, tree),
            f"Grade details: {DIAGRAM}, {BIOLOGY}",
        ),
        (LOG, LINE, link_to(class_id, first, 1, tree), f"Grade details: {LOG}, {BIOLOGY}"),
        (LOG, NEWER_LINE, link_to(class_id, second, 2, tree), f"Grade details: {LOG}, {BIOLOGY}"),
    ]
    assert pages[0].index(f'id="{diagram}"') < pages[0].index(f'id="{first}"')
    assert [line for _, line, _, _ in listed] == [
        line_on_class_details(pages[0], diagram),
        line_on_class_details(pages[0], first),
        line_on_class_details(pages[1], second),
    ]
    assert len(scores(section)) == 3
    assert [count for count, _, _ in several(card)] == ["3"]
    said = re.sub(r"<[^>]*>", " ", section).lower()
    assert not [word for word in ("link", "match", "parent", "chose") if word in said]
    assert "<form" not in section
    assert "<button" not in section


TIED = (
    IN_WEEK.replace("08/27", "08/20")
    .replace("| 08/12   |", "|         |")
    .replace("| 08/01   |", "|         |")
)
"""The report with the diagram due on the log's day, and the practice and the osmosis lab with
no due date."""


def test_several_results_come_term_by_term_in_class_details_order() -> None:
    """Eight results of two terms linked to one assignment, two due one day and two undated in
    each, the second term's day the later, and one first-term title's stored presence changed
    by hand to blank. The details list them term by term, as each term's class details does."""
    assert (TIED.count("08/20"), TIED.count("08/12"), TIED.count("08/01")) == (2, 0, 0)
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(title=NOTEBOOK)], {})
        class_id = with_report(client, TIED)
        with_report(client, TIED.replace("**T1**", "**T2**").replace("08/20", "08/27"))
        for term in ("T2", "T1"):
            for title in (OSMOSIS, LOG, PRACTICE, DIAGRAM):
                result_id = result_named(client, class_id, title, term)
                parent_links(client, class_id, result_id, term=term)
        stored(
            store,
            "UPDATE grade_result_observations SET assignment_presence = ? WHERE result_id = ?",
            Presence.BLANK.value,
            result_named(client, class_id, DIAGRAM),
        )
        details = details_of(client)
        pages = [class_details(client, class_id, n) for n in (1, 2)]

    on_class_details = [order_on_class_details(page) for page in pages]
    listed = scores(details)
    titles = [
        html.unescape(re.sub(r"<[^>]*>", "", title))
        for title in re.findall(r'<h3 class="result-title">(.*?)</h3>', details, re.S)
    ]
    assert [len(one) for one in on_class_details] == [4, 4]
    assert [href.rsplit("#", 1)[-1] for _, href, _ in listed] == [
        *on_class_details[0],
        *on_class_details[1],
    ]
    assert [href.rsplit("#", 1)[0].rsplit("/", 1)[-1] for _, href, _ in listed] == [
        *["1"] * 4,
        *["2"] * 4,
    ]
    assert titles == [
        LOG,
        "Title left blank",
        PRACTICE,
        OSMOSIS,
        DIAGRAM,
        LOG,
        PRACTICE,
        OSMOSIS,
    ]


def test_results_of_two_school_years_both_show_the_earlier_year_s_first() -> None:
    """One assignment linked to a result of last school year's second term and to one of this
    year's first term, in two classes of one name. The details list both, last year's first
    though its term's place is the later, each with the link to its own class and term."""
    last_year = IN_WEEK.replace(f"**{YEAR}**", "**2025-2026**")
    with browser() as client:
        store_of(client).put_on_record([homework(title=NOTEBOOK)], {})
        this_class = with_report(client)
        last_class = with_report(client, last_year)
        with_report(client, last_year.replace("**T1**", "**T2**").replace(LOG_ROW, NEWER_LOG))
        this_result = result_named(client, this_class, LOG)
        last_result = result_named(client, last_class, LOG, "T2")
        parent_links(client, this_class, this_result)
        parent_links(client, last_class, last_result, term="T2")
        details = details_of(client)
        card = several(card_for(page_of(client), LOG_ID))

    assert this_class != last_class
    assert scores(details) == [
        (NEWER_LINE, link_to(last_class, last_result, 2), f"Grade details: {LOG}, {BIOLOGY}"),
        (LINE, link_to(this_class, this_result), f"Grade details: {LOG}, {BIOLOGY}"),
    ]
    assert [count for count, _, _ in card] == ["2"]


def test_the_count_is_of_results_whose_line_shows_and_one_shows_as_one_result_does() -> None:
    """Two results linked to one assignment. While a newer report gives one of them no points
    her pages read as for the one result, and the link row stays as stored; points reported
    again bring the count back."""
    no_points = IN_WEEK.replace(LOG_ROW, "| Seed Germination Log |         | 20.0    | 90.0    |")
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(title=NOTEBOOK)], {})
        class_id = with_report(client)
        log, diagram = (result_named(client, class_id, title) for title in (LOG, DIAGRAM))
        parent_links(client, class_id, log)
        parent_links(client, class_id, diagram)
        rows = as_stored(store, "grade_homework_links")
        both = several(card_for(page_of(client), LOG_ID))
        with_report(client, no_points)
        one = [on_card(client), scores(details_of(client))]
        counted = several(page_of(client)) + several(details_of(client))
        titled = EACH_RESULT.findall(details_of(client))
        rows_then = as_stored(store, "grade_homework_links")
        class_page = class_details(client, class_id)
        with_report(client, IN_WEEK.replace(LOG_ROW, NEWER_LOG))
        again = several(card_for(page_of(client), LOG_ID))

    line = line_on_class_details(class_page, diagram)
    expected = [(line, link_to(class_id, diagram), f"Grade details: {NOTEBOOK}, {BIOLOGY}")]
    assert [count for count, _, _ in both] == ["2"]
    assert line.startswith(DIAGRAM_LINE)
    assert one == [expected, expected]
    assert counted == []
    assert titled == []
    assert rows_then == rows
    assert line_on_class_details(class_page, log).startswith("School score: blank / 20.0")
    assert [count for count, _, _ in again] == ["2"]


def test_a_linked_result_of_another_title_shows_on_undated_homework_and_follows_its_value() -> None:
    """Undated homework of another title, linked to the log's result, shows class details' line
    for it, a newer report's changed score included. A complete report that stops showing the
    result takes the line away, one that shows it again brings it back, and the link row stays."""
    gone = "\n".join(line for line in IN_WEEK.split("\n") if LOG not in line)
    newer = IN_WEEK.replace(LOG_ROW, NEWER_LOG)
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(title=NOTEBOOK, on=None)], {})
        class_id = with_report(client)
        result_id = result_named(client, class_id, LOG)
        unlinked = on_card(client)
        parent_links(client, class_id, result_id)
        rows = as_stored(store, "grade_homework_links")
        linked = [on_card(client), scores(details_of(client))]
        with_report(client, newer)
        changed = on_card(client)
        newer_class_line = line_on_class_details(class_details(client, class_id), result_id)
        with_report(client, gone, complete=True)
        not_shown = [on_card(client), scores(details_of(client))]
        still_on_class_details = class_details(client, class_id)
        with_report(client, newer.replace("| 7.0 ", "| 6.0 "), complete=True)
        shown_again = on_card(client)
        again_class_line = line_on_class_details(class_details(client, class_id), result_id)
        rows_after = as_stored(store, "grade_homework_links")

    expected = [(LINE, link_to(class_id, result_id), f"Grade details: {NOTEBOOK}, {BIOLOGY}")]
    assert unlinked == []
    assert linked == [expected, expected]
    assert [line for line, _, _ in changed] == [newer_class_line]
    assert newer_class_line.startswith(NEWER_LINE)
    assert not_shown == [[], []]
    assert f'id="{result_id}"' in still_on_class_details
    assert [line for line, _, _ in shown_again] == [again_class_line]
    assert rows_after == rows


def test_a_link_shows_only_while_the_homework_s_class_name_connects_to_the_result_s_class() -> None:
    """One homework row under the class's own name keeps the class read and its line by the
    rules; one under a name a parent connected is linked to the log's result. The connection
    removed, the linked row shows no score and its link row is as stored; connected, it is back."""
    both = (LOG_ID, DIAGRAM_ID)
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [
                homework(title=NOTEBOOK, course="Life Science"),
                homework(DIAGRAM_ID, DIAGRAM, date(2026, 8, 27), assigned=MONDAY),
            ],
            {},
        )
        class_id = with_report(client)
        log, diagram = (result_named(client, class_id, title) for title in (LOG, DIAGRAM))
        store.connect_homework_class(
            YEAR, "Life Science", shown=None, chosen=class_id, role="parent"
        )
        parent_links(client, class_id, log)
        rows = as_stored(store, "grade_homework_links")
        connected = [scores(details_of(client, one)) for one in both]
        store.connect_homework_class(
            YEAR, "Life Science", shown=class_id, chosen=UNCONNECTED, role="parent"
        )
        removed = [scores(details_of(client, one)) for one in both]
        removed_on_her_week = scores(page_of(client))
        rows_then = as_stored(store, "grade_homework_links")
        store.connect_homework_class(
            YEAR, "Life Science", shown=UNCONNECTED, chosen=class_id, role="parent"
        )
        again = [scores(details_of(client, one)) for one in both]

    linked = [(LINE, link_to(class_id, log), f"Grade details: {NOTEBOOK}, Life Science")]
    ruled = [(DIAGRAM_LINE, link_to(class_id, diagram), f"Grade details: {DIAGRAM}, {BIOLOGY}")]
    assert connected == [linked, ruled]
    assert removed == [[], ruled]
    assert removed_on_her_week == ruled
    assert rows_then == rows
    assert again == [linked, ruled]


def test_a_pair_kept_apart_shows_nothing_and_shows_again_once_no_decision_is_on_record() -> None:
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        result_id = result_named(client, class_id, LOG)
        ruled = on_card(client)
        parent_links(client, class_id, result_id, decision="apart")
        apart = [on_card(client), scores(details_of(client))]
        parent_links(client, class_id, result_id, decision="automatic")
        rows = as_stored(store_of(client), "grade_homework_links")
        again = on_card(client)

    assert [line for line, _, _ in ruled] == [LINE]
    assert apart == [[], []]
    assert rows == []
    assert again == ruled


def test_a_parent_s_assertion_on_a_linked_result_s_points_never_reaches_her_pages() -> None:
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(title=NOTEBOOK)], {})
        class_id = with_report(client)
        result_id = result_named(client, class_id, LOG)
        parent_links(client, class_id, result_id)
        store.correct_value(
            class_id,
            "T1",
            page=correction_page(store, class_id, "result", result_id),
            field="points",
            how="parent_assertion",
            value=GradeValue(text="20.0", presence=Presence.REPORTED),
            role="parent",
        )
        asserted = store.class_term(class_id, "T1").current.results[result_id].asserted
        pages = [as_rendered(client, address) for address in HER_PAGES]

    assert "points" in asserted
    assert [[line for line, _, _ in scores(page)] for page in pages] == [[LINE], [LINE]]
    assert not [page for page in pages if "20.0 / 20.0" in page]


def test_a_task_competes_with_its_twin_until_a_parent_links_it_and_then_shows_its_line() -> None:
    """A task and homework with the log's class, title and day: neither shows a score. Linked
    to the result, the task shows the line on its card and its details, and the homework none."""
    twin = "assignment-twin"
    with browser() as client:
        store_of(client).put_on_record([homework(kind=AssignmentKind.TASK), homework(twin)], {})
        class_id = with_report(client)
        result_id = result_named(client, class_id, LOG)
        competing = scores(page_of(client))
        parent_links(client, class_id, result_id)
        page = page_of(client)
        shown = [scores(card_for(page, LOG_ID)), scores(details_of(client))]
        beside = [scores(page), scores(details_of(client, twin))]

    expected = [(LINE, link_to(class_id, result_id), f"Grade details: {LOG}, {BIOLOGY}")]
    assert competing == []
    assert shown == [expected, expected]
    assert beside == [expected, []]


def test_homework_whose_one_linked_result_has_no_points_shows_no_score_at_all() -> None:
    """Homework the rules alone match to the diagram's result, linked to the log's result: it
    shows the log's line, and once a newer report gives the log no points it shows nothing on
    her week or its details, the diagram's line included."""
    no_points = IN_WEEK.replace(LOG_ROW, "| Seed Germination Log |         | 20.0    | 90.0    |")
    with browser() as client:
        store_of(client).put_on_record(
            [homework(DIAGRAM_ID, DIAGRAM, date(2026, 8, 27), assigned=MONDAY)], {}
        )
        class_id = with_report(client)
        ruled = scores(page_of(client))
        parent_links(client, class_id, result_named(client, class_id, LOG), DIAGRAM_ID)
        linked = scores(page_of(client))
        with_report(client, no_points)
        withheld = [scores(page_of(client)), scores(details_of(client, DIAGRAM_ID))]

    assert [line for line, _, _ in ruled] == [DIAGRAM_LINE]
    assert [line for line, _, _ in linked] == [LINE]
    assert withheld == [[], []]


def test_one_page_read_runs_the_score_rules_once_and_two_places_on_it_show_that_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card outside the week shown and the earlier-work row of one assignment linked to two
    results, on one page: the same count and the same link, from one run of the rules. A read
    of the details runs them once too."""
    runs: list[int] = []

    def counted(
        read: Iterable[Homework],
        results: Iterable[Result],
        classes: Mapping[str, Collection[str]],
        *,
        decided: Collection[Link] = (),
    ) -> dict[str, tuple[ScoreAt, ...]]:
        runs.append(1)
        return school_scores(read, results, classes, decided=decided)

    with browser() as client:
        store_of(client).put_on_record([homework(PRACTICE_ID, PRACTICE, date(2026, 8, 12))], {})
        class_id = with_report(client)
        with_report(client, SECOND_TERM)
        for term in ("T1", "T2"):
            result_id = result_named(client, class_id, PRACTICE, term)
            parent_links(client, class_id, result_id, PRACTICE_ID, term=term)
        monkeypatch.setattr("blossom.routes.homework_scores.school_scores", counted)
        page = page_of(client, change=PRACTICE_ID)
        on_her_week = len(runs)
        details = details_of(client, PRACTICE_ID)

    row = several(earlier_row(page, PRACTICE))
    assert len(row) == 1
    assert several(page) == row * 2
    assert on_her_week == 1
    assert len(runs) == 2
    assert len(scores(details)) == 2


AS_READ = "Cell  Diagram Mitochondriaandchloroplastsandribosomesandvacuoles"
"""A title as a copy can give it: two spaces together and one long string with no break."""
TITLE_STATES = [
    pytest.param(Presence.BLANK, DIAGRAM, "Title left blank", id="left blank"),
    pytest.param(
        Presence.UNREADABLE, AS_READ, f"Title couldn't be read: {AS_READ}", id="couldn't be read"
    ),
    pytest.param(Presence.NOT_CAPTURED, DIAGRAM, "Title not in the copy", id="not in the copy"),
]


@pytest.mark.parametrize(("presence", "text", "words"), TITLE_STATES)
def test_a_result_with_no_reported_title_is_listed_under_class_details_words_for_it(
    presence: Presence, text: str, words: str
) -> None:
    """Two results linked to one assignment, then one result's stored title and its presence
    changed by hand. The details title it as class details does, the same text in the same
    marking as the school's, spaces kept, and name its link by the same words."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(title=NOTEBOOK)], {})
        class_id = with_report(client)
        log, diagram = (result_named(client, class_id, title) for title in (LOG, DIAGRAM))
        parent_links(client, class_id, log)
        parent_links(client, class_id, diagram)
        stored(
            store,
            "UPDATE grade_result_observations SET assignment_text = ?, assignment_presence = ? "
            "WHERE result_id = ?",
            text,
            presence.value,
            diagram,
        )
        details = details_of(client)
        class_page = class_details(client, class_id)

    card = class_page[class_page.index(f'<h3 class="card-title" id="{diagram}" tabindex="-1">') :]
    on_class_details = re.search(r'<h3 class="card-title"[^>]*>(.*?)</h3>', card, re.S)
    assert on_class_details is not None
    titles = re.findall(r'<h3 class="result-title">(.*?)</h3>', details, re.S)
    assert html.unescape(on_class_details.group(1)) == (
        f'<span class="authored-text">{words}</span>'
    )
    assert titles == [on_class_details.group(1), f'<span class="authored-text">{LOG}</span>']
    assert scores(details) == [
        (DIAGRAM_LINE, link_to(class_id, diagram), f"Grade details: {words}, {BIOLOGY}"),
        (LINE, link_to(class_id, log), f"Grade details: {LOG}, {BIOLOGY}"),
    ]
    assert f"Grade details: {NOTEBOOK}" not in html.unescape(details)


def test_a_link_read_asks_two_statements_inside_the_hold_only_with_a_class_and_term_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every statement a read of her week or of the details asks, against the same read with the
    link read put aside: equal with no report or only an unconnected class's. With a connected
    class and term read: one additional link read, two SQL statements inside the existing hold."""
    her_record = " ".join(gradebook.HER_NAME_RECORD.split())

    def asked(client: TestClient, address: str) -> list[str]:
        seen: list[str] = []
        connection = store_of(client)._connection
        connection.set_trace_callback(seen.append)
        try:
            assert client.get(address, headers=PAGE_HEADERS).status_code == 200
        finally:
            connection.set_trace_callback(None)
        return [" ".join(statement.split()) for statement in seen]

    def both(client: TestClient) -> list[tuple[list[str], list[str]]]:
        """Each page's statements as it is read, and with the link read put aside."""
        read = [asked(client, address) for address in HER_PAGES]
        with monkeypatch.context() as aside:
            aside.setattr(store_of(client), "homework_links", tuple)
            return list(zip(read, [asked(client, address) for address in HER_PAGES], strict=True))

    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        with_none = both(client)
        with_report(client, GEOMETRY)
        unconnected = both(client)
        with_report(client)
        connected = both(client)
        her_links = gradebook.HOMEWORK_LINKS.replace("?", f"'{store_of(client).student_id()}'")

    for read, aside in (*with_none, *unconnected):
        assert read == aside
        assert not [one for one in read if "grade_homework_links" in one]
    for read, aside in connected:
        at = read.index(her_links)
        begun = max(place for place, one in enumerate(read[:at]) if one.startswith("BEGIN"))
        ended = min(place for place, one in enumerate(read) if place > at and one == "COMMIT")
        assert read.count(her_links) == 1
        assert read[at - 1] == her_record
        assert read[: at - 1] + read[at + 1 :] == aside
        assert not [one for one in read[begun + 1 : ended] if one in ("COMMIT", "ROLLBACK")]
        assert next(place for place, one in enumerate(read) if "grade_" in one) > begun
        assert "grade_" in read[at - 2]
        assert not [one for one in read[at + 1 :] if "grade_" in one]


def test_the_read_gives_the_grade_facts_and_her_links_and_the_mapping_says_how_each_stands() -> (
    None
):
    """One pair linked and one kept apart, by the store's own write. The read gives the grade
    facts with both links as plain values. The mapping made from them holds the linked result
    with its title and nothing for the pair kept apart; with no link, both as the rules stand."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(), homework(DIAGRAM_ID, DIAGRAM, date(2026, 8, 27))], {})
        nothing = read_score_facts(store, [BIOLOGY])
        class_id = with_report(client)
        log, diagram = (result_named(client, class_id, title) for title in (LOG, DIAGRAM))
        parent_links(client, class_id, log)
        parent_links(client, class_id, diagram, DIAGRAM_ID, decision="apart")
        years, links = read_score_facts(store, [BIOLOGY])
        alone = read_grade_facts(store, [BIOLOGY])
        everyone = store.all_assignments()
        base = "/student/grades"
        with_links = scores_shown(everyone, {}, (), years, links=links, zone=ZONE, base=base)
        by_the_rules = scores_shown(everyone, {}, (), years, zone=ZONE, base=base)

    assert not results_read(nothing[0])
    assert nothing[1] == ()
    assert years == alone
    assert sorted(links, key=lambda one: one.assignment_id) == [
        Link(DIAGRAM_ID, diagram, confirmed=False),
        Link(LOG_ID, log, confirmed=True),
    ]
    assert with_links == {
        LOG_ID: (ScoreShown(LINE, link_to(class_id, log), title=LOG, linked=True),)
    }
    assert by_the_rules == {
        LOG_ID: (ScoreShown(LINE, link_to(class_id, log), title=LOG, linked=False),),
        DIAGRAM_ID: (
            ScoreShown(DIAGRAM_LINE, link_to(class_id, diagram), title=DIAGRAM, linked=False),
        ),
    }


@pytest.mark.parametrize("reader", ["her", "parent"])
def test_two_confirmed_links_naming_one_result_leave_her_pages_as_with_no_class_on_record(
    reader: str, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The file is changed by hand: the links index dropped and two confirmed rows stored for
    the log's result. Her week, the details of the homework a row names and of homework no row
    names each answer 200 with no score and no way to one, and record the conflict's type alone."""
    addresses = [HER_PAGE, *(f"/student/assignments/{one}" for one in (LOG_ID, DIAGRAM_ID))]
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        store = store_of(client)
        store.put_on_record(
            [homework(), homework(DIAGRAM_ID, DIAGRAM, date(2026, 8, 27), assigned=MONDAY)], {}
        )
        bare = [as_rendered(client, address) for address in addresses]
        class_id = with_report(client)
        whole = [as_rendered(client, address) for address in addresses]
        conflicting(client, result_named(client, class_id, LOG))
        before = every_row(database_of(client))
        said, logged = [], []
        with caplog.at_level(logging.WARNING, logger="blossom"):
            for address in addresses * 2:
                caplog.clear()
                said.append(as_rendered(client, address))
                logged.append((recorded(caplog), caplog.text))
        after = every_row(database_of(client))

    assert [len(scores(page)) for page in whole] == [2, 1, 1]
    assert said == bare * 2
    for page in said:
        assert not [
            word
            for word in ("School score", "School results", "Grade details", "See the results")
            if word in page
        ]
        assert HEADING not in page
    assert [records for records, _ in logged] == [[CONFLICT]] * 6
    for _, text in logged:
        assert not [word for word in (LOG, DIAGRAM, BIOLOGY, "18.0", LOG_ID) if word in text]
    assert after == before


def test_a_conflict_is_met_only_when_her_links_are_read_and_whatever_result_it_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The file is changed by hand as above, the two rows naming a result of a class no homework
    name connects to. With only that class's report on record her week reads no link, shows no
    score and records nothing; with a connected class's report too it records the conflict."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        geometry = with_report(client, GEOMETRY)
        conflicting(client, result_named(client, geometry, LOG))
        with caplog.at_level(logging.WARNING, logger="blossom"):
            unread = as_rendered(client, HER_PAGE)
            first = recorded(caplog)
            with_report(client)
            caplog.clear()
            read = as_rendered(client, HER_PAGE)
            second = recorded(caplog)

    assert scores(unread) == []
    assert first == []
    assert scores(read) == []
    assert second == [CONFLICT]


LINK_EDITS = [
    pytest.param(as_bytes("grade_homework_links", "result_id"), id="the result's ID as bytes"),
    pytest.param(
        as_bytes("grade_homework_links", "assignment_id"), id="the homework's ID as bytes"
    ),
    pytest.param(not_utf8("grade_homework_links", "result_id"), id="the result's ID not UTF-8"),
    pytest.param(
        not_utf8("grade_homework_links", "assignment_id"), id="the homework's ID not UTF-8"
    ),
]
"""A link's result ID and homework ID, each stored as bytes and each stored as text that is
not UTF-8."""


@pytest.mark.parametrize("edit", LINK_EDITS)
def test_a_link_value_that_can_t_be_decoded_leaves_her_pages_as_with_no_class_on_record(
    edit: Edit, caplog: pytest.LogCaptureFixture
) -> None:
    """A link made by the store's own write, then one of its IDs changed by hand through SQL.
    Her week and the details read as with no class on record and log the failure's type alone,
    also with two confirmed rows for one result stored by hand beside it."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [homework(), homework(DIAGRAM_ID, NOTEBOOK, date(2026, 8, 27), assigned=MONDAY)], {}
        )
        bare = [as_rendered(client, address) for address in HER_PAGES]
        class_id = with_report(client)
        diagram = result_named(client, class_id, DIAGRAM)
        parent_links(client, class_id, diagram, DIAGRAM_ID)
        whole = [as_rendered(client, address) for address in HER_PAGES]
        stored(store, edit.statement, *edit.binds)
        said, logged = [], []
        with caplog.at_level(logging.WARNING, logger="blossom"):
            for with_a_conflict in (False, True):
                if with_a_conflict:
                    conflicting(client, result_named(client, class_id, LOG))
                for address in HER_PAGES:
                    caplog.clear()
                    said.append(as_rendered(client, address))
                    logged.append((recorded(caplog), caplog.text))
        decoding = store._connection.text_factory

    private = (LOG, DIAGRAM, NOTEBOOK, BIOLOGY, DIAGRAM_ID, "xff", "\ufffd")
    assert [len(scores(page)) for page in whole] == [2, 1]
    assert said == bare * 2
    for page in said:
        assert "School score" not in page
        assert "Grade details" not in page
    assert [records for records, _ in logged] == [[LEFT_OUT]] * 4
    for _, text in logged:
        assert not [word for word in private if word in text]
    assert decoding is text_or_refusal


def test_an_undecodable_cell_beside_a_link_or_a_conflict_leaves_no_score_and_one_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A link is on record, and the stored presence of another result's points is changed by
    hand to no presence. Her week shows no score and writes one record of the decoding failure's
    type, also with two confirmed rows for one result stored by hand: the facts are read first."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [homework(), homework(DIAGRAM_ID, NOTEBOOK, date(2026, 8, 27), assigned=MONDAY)], {}
        )
        class_id = with_report(client)
        parent_links(client, class_id, result_named(client, class_id, DIAGRAM), DIAGRAM_ID)
        whole = scores(page_of(client))
        log = result_named(client, class_id, LOG)
        points_presence(client, result_named(client, class_id, OSMOSIS), SMUDGED)
        with caplog.at_level(logging.WARNING, logger="blossom"):
            damaged = scores(page_of(client))
            first = recorded(caplog)
            caplog.clear()
            conflicting(client, log)
            both = scores(page_of(client))
            second = recorded(caplog)

    assert len(whole) == 2
    assert damaged == []
    assert first == [LEFT_OUT]
    assert both == []
    assert second == [LEFT_OUT]


THE_CLASS = ClassOfYear("class-biology", BIOLOGY, YEAR)
NOTEBOOK_ID = "assignment-notebook"
LINKED, APART = PairStanding.LINKED, PairStanding.APART


def school_result(
    result_id: str,
    title: str = LOG,
    *,
    term: str = "T1",
    class_id: str = THE_CLASS.class_id,
    scored: bool = True,
    shown: bool = True,
) -> Result:
    """A result as the score rules read it, due on the log's day."""
    return Result(class_id, term, result_id, title, THURSDAY, True, scored, shown)


def as_read(
    items: Sequence[Assignment],
    results: Sequence[Result],
    *,
    in_doubt: Collection[str] = (),
    unconnected: Collection[str] = (),
) -> RuleValues:
    """One page read as plain values: each assignment's name connected to the one class but
    for those ``unconnected``, and a date claim that can't be read for those ``in_doubt``."""
    return RuleValues(
        assignments=list(items),
        homework=[homework_of(item, (), item.assignment_id in in_doubt) for item in items],
        results=list(results),
        classes={
            item.assignment_id: [THE_CLASS.class_id]
            for item in items
            if item.assignment_id not in unconnected
        },
        found={},
    )


def value_with(points: Presence = Presence.REPORTED, *, shown: bool = True) -> CurrentValue:
    """A result's current value with its points cell in the presence ``points``, shown by its
    class and term or else not shown by a newer report."""
    text = {Presence.REPORTED: "18.0", Presence.UNREADABLE: "EX"}.get(points, "")
    return CurrentValue(
        cells={"assignment": (Presence.REPORTED, LOG), "points": (points, text)},
        report_id="report",
        order=1,
        not_shown=None if shown else ReportAt("report-2", 2),
    )


def viewed(
    result_id: str,
    read: RuleValues,
    links: Collection[Link] = (),
    *,
    term: str = "T1",
    value: CurrentValue | None = None,
    current_year: str | None = YEAR,
) -> ResultHomework:
    """The view of a result of the one class, its points reported unless ``value`` says so."""
    return result_homework(
        THE_CLASS,
        term,
        result_id,
        value_with() if value is None else value,
        read,
        links=links,
        current_year=current_year,
    )


def row(
    item: Assignment,
    standing: PairStanding = PairStanding.NONE,
    *,
    moves_link: bool = False,
    another_result: bool = False,
) -> HomeworkRow:
    """A row for homework on record, with its class name, title and due date as written."""
    return HomeworkRow(
        assignment_id=item.assignment_id,
        on_record=True,
        course=item.course,
        title=item.title,
        due=item.due_date,
        standing=standing,
        moves_link=moves_link,
        another_result=another_result,
    )


def off_record(assignment_id: str, standing: PairStanding) -> HomeworkRow:
    """A row for homework that is not on record: its ID and the decision alone."""
    return HomeworkRow(assignment_id, False, None, None, None, standing, False, False)


def confirmed(assignment_id: str, result_id: str) -> Link:
    return Link(assignment_id, result_id, confirmed=True)


def kept_apart(assignment_id: str, result_id: str) -> Link:
    return Link(assignment_id, result_id, confirmed=False)


def test_a_result_s_view_says_linked_matched_or_neither_and_lists_each_candidate() -> None:
    """Plain values: one result with homework of its title and day, and undated homework of
    another title. The view names the homework the rules stand it with, or the homework a link
    names, or neither, and every candidate keeps its row whatever decision stands."""
    log, twin = homework(), homework("assignment-twin")
    notebook = homework(NOTEBOOK_ID, NOTEBOOK, None)
    one = [school_result("result-log")]
    items = [log, notebook]
    alone = as_read(items, one)

    matched = viewed("result-log", alone)
    apart = viewed("result-log", alone, [kept_apart(LOG_ID, "result-log")])
    linked = viewed("result-log", alone, [confirmed(LOG_ID, "result-log")])
    elsewhere = viewed("result-log", alone, [confirmed(NOTEBOOK_ID, "result-log")])
    twins = viewed("result-log", as_read([log, twin], one))
    spelled = viewed("result-log", alone, term="  T1 ")
    unread = viewed("result-other", alone)

    assert matched == ResultHomework(
        class_id=THE_CLASS.class_id,
        term="T1",
        result_id="result-log",
        class_name=BIOLOGY,
        year=YEAR,
        linked=None,
        matched=row(log),
        candidates=(row(log),),
        apart=(),
        withheld=(),
        waiting=None,
    )
    assert apart == dataclasses.replace(
        matched, matched=None, candidates=(row(log, APART),), apart=(row(log, APART),)
    )
    assert linked == dataclasses.replace(
        matched, matched=None, linked=row(log, LINKED), candidates=(row(log, LINKED),)
    )
    assert elsewhere == dataclasses.replace(
        matched,
        matched=None,
        linked=row(notebook, LINKED),
        candidates=(row(log, moves_link=True),),
    )
    assert twins == dataclasses.replace(
        matched,
        matched=None,
        candidates=(row(log), row(twin)),
        waiting=Waiting(THE_CLASS.class_id, "T1", "result-log", 2, 0, False, False),
    )
    assert spelled == dataclasses.replace(matched, term="  T1 ")
    assert unread == dataclasses.replace(
        matched, result_id="result-other", matched=None, candidates=()
    )


def test_a_candidate_s_row_says_when_its_homework_already_has_another_linked_result() -> None:
    """Plain values: one homework and a result of its title in each of two terms, the first
    linked. The second waits with the homework as its candidate, and its row says the homework
    has another linked result; linked too, its own row still says so."""
    log = homework()
    results = [school_result("result-first"), school_result("result-second", term="T2")]
    read = as_read([log], results)
    one = [confirmed(LOG_ID, "result-first")]
    both = [*one, confirmed(LOG_ID, "result-second")]

    first = viewed("result-first", read, one)
    second = viewed("result-second", read, one, term="T2")
    second_linked = viewed("result-second", read, both, term="T2")

    assert first.candidates == (row(log, LINKED),)
    assert (first.linked, first.waiting) == (row(log, LINKED), None)
    assert second.candidates == (row(log, another_result=True),)
    assert (second.linked, second.matched) == (None, None)
    assert second.waiting == Waiting(THE_CLASS.class_id, "T2", "result-second", 0, 0, True, False)
    assert second_linked.linked == row(log, LINKED, another_result=True)
    assert second_linked.waiting is None


def test_pairs_kept_apart_and_a_link_are_listed_with_their_homework_on_record_or_not() -> None:
    """Plain values: a result kept apart from homework of its title, from homework of another
    title and from homework not among the assignments given, and linked to homework not among
    them. Rows for homework on record come first, in the order the assignments came."""
    log, notebook = homework(), homework(NOTEBOOK_ID, NOTEBOOK, None)
    items = [notebook, log]
    read = as_read(items, [school_result("result-log")])
    links = [
        kept_apart("assignment-gone", "result-log"),
        kept_apart(LOG_ID, "result-log"),
        confirmed("assignment-left", "result-log"),
        kept_apart(NOTEBOOK_ID, "result-log"),
        kept_apart(LOG_ID, "result-other"),
    ]

    view = viewed("result-log", read, links, value=value_with(Presence.BLANK))

    assert view.linked == off_record("assignment-left", LINKED)
    assert view.matched is None
    assert view.candidates == (row(log, APART, moves_link=True),)
    assert view.apart == (
        row(notebook, APART, moves_link=True),
        row(log, APART, moves_link=True),
        off_record("assignment-gone", APART),
    )
    assert view.withheld == ()
    assert view.waiting is None


@pytest.mark.parametrize(
    ("value", "connected", "current_year", "withheld"),
    [
        pytest.param(value_with(), True, YEAR, (), id="nothing withheld"),
        pytest.param(
            value_with(Presence.BLANK), True, YEAR, (Withheld.POINTS_BLANK,), id="points blank"
        ),
        pytest.param(
            value_with(Presence.UNREADABLE),
            True,
            YEAR,
            (Withheld.POINTS_UNREADABLE,),
            id="points unreadable",
        ),
        pytest.param(
            value_with(Presence.NOT_CAPTURED),
            True,
            YEAR,
            (Withheld.POINTS_NOT_IN_COPY,),
            id="points not in the copy",
        ),
        pytest.param(value_with(shown=False), True, YEAR, (Withheld.NOT_SHOWN,), id="not shown"),
        pytest.param(
            value_with(), False, YEAR, (Withheld.NAME_NOT_CONNECTED,), id="name not connected"
        ),
        pytest.param(
            value_with(),
            False,
            "2027-2028",
            (Withheld.NAME_NOT_CONNECTED_IN_ANOTHER_YEAR,),
            id="name not connected, a class of another school year",
        ),
        pytest.param(
            value_with(),
            False,
            None,
            (Withheld.NAME_NOT_CONNECTED_IN_ANOTHER_YEAR,),
            id="name not connected, no current school year",
        ),
        pytest.param(
            value_with(Presence.BLANK, shown=False),
            False,
            YEAR,
            (Withheld.POINTS_BLANK, Withheld.NOT_SHOWN, Withheld.NAME_NOT_CONNECTED),
            id="all three at once",
        ),
    ],
)
def test_a_linked_result_names_each_reason_its_score_stays_off_her_homework(
    value: CurrentValue, connected: bool, current_year: str | None, withheld: tuple[Withheld, ...]
) -> None:
    """Plain values: undated homework of another title linked to the result. The view names a
    reason exactly when the score rules show no score for the pair, never reads a parent's
    assertion, and names none with no link or with the linked homework not given."""
    notebook = homework(NOTEBOOK_ID, NOTEBOOK, None)
    scored = value.cells["points"][0] is Presence.REPORTED
    results = [school_result("result-log", scored=scored, shown=value.not_shown is None)]
    read = as_read([notebook], results, unconnected=() if connected else [NOTEBOOK_ID])
    links = [confirmed(NOTEBOOK_ID, "result-log")]

    view = viewed("result-log", read, links, value=value, current_year=current_year)
    unlinked = viewed("result-log", read, value=value, current_year=current_year)
    left = viewed("result-log", as_read([], results), links, value=value, current_year=current_year)

    assert view.withheld == withheld
    assert view.linked == row(notebook, LINKED)
    told = dataclasses.replace(value, asserted={"points": (Presence.REPORTED, "20.0")})
    assert viewed("result-log", read, links, value=told, current_year=current_year) == (view)
    shows = school_scores(read.homework, read.results, read.classes, decided=links)
    assert bool(shows) is (withheld == ())
    assert unlinked.withheld == ()
    assert (left.linked, left.withheld) == (off_record(NOTEBOOK_ID, LINKED), ())


def test_each_result_of_a_class_waits_in_its_view_exactly_as_the_score_rules_name_it() -> None:
    """Plain values: one class with a result of each kind that waits and of each that does not.
    Every result's view holds the rules' own answer for it, and none both waits and stands."""

    def rows(title: str, *assignment_ids: str) -> list[Assignment]:
        return [homework(one, title) for one in assignment_ids]

    items = [
        *rows("Twin Homework", "twin-1", "twin-2"),
        *rows("Twin Results", "shared"),
        *rows("In Doubt", "doubt"),
        *rows("No Points", "unscored"),
        *rows("Showing", "showing"),
        *rows("Kept Apart", "apart"),
        *rows("Twins No Points", "pointless-1", "pointless-2"),
        *rows("Linked", "linked"),
        *rows("Second Result", "second"),
        *rows("Both Ways", "first-row", "second-row"),
    ]
    results = [
        school_result("twin-homework", "Twin Homework"),
        school_result("twin-result-1", "Twin Results"),
        school_result("twin-result-2", "Twin Results", term="T2"),
        school_result("in-doubt", "In Doubt"),
        school_result("no-points", "No Points", scored=False),
        school_result("not-shown", "Showing", shown=False),
        school_result("kept-apart", "Kept Apart"),
        school_result("twins-no-points", "Twins No Points", scored=False),
        school_result("linked", "Linked"),
        school_result("first-linked", "Second Result"),
        school_result("second-waits", "Second Result"),
        school_result("row-linked", "Both Ways"),
        school_result("row-ruled", "Both Ways"),
        school_result("no-homework", "No Homework"),
        school_result("unconnected", "Twin Homework", class_id="class-other"),
    ]
    links = [
        kept_apart("apart", "kept-apart"),
        confirmed("linked", "linked"),
        confirmed("second", "first-linked"),
        confirmed("first-row", "row-linked"),
    ]
    read = as_read(items, results, in_doubt=["doubt"])

    named = {
        (one.class_id, one.term, one.result_id): one
        for one in waiting_results(read.homework, read.results, read.classes, decided=links)
    }
    views = {
        one.result_id: result_homework(
            ClassOfYear(one.class_id, BIOLOGY, YEAR),
            one.term,
            one.result_id,
            value_with(),
            read,
            links=links,
            current_year=YEAR,
        )
        for one in results
    }

    def waits(
        result_id: str,
        assignments: int = 0,
        results: int = 0,
        *,
        elsewhere: bool = False,
        unsettled: bool = False,
        term: str = "T1",
    ) -> Waiting:
        return Waiting(
            THE_CLASS.class_id, term, result_id, assignments, results, elsewhere, unsettled
        )

    for one in results:
        assert views[one.result_id].waiting == named.get((one.class_id, one.term, one.result_id))
    assert {view.waiting for view in views.values() if view.waiting is not None} == {
        waits("twin-homework", assignments=2),
        waits("twin-result-1", results=2),
        waits("twin-result-2", results=2, term="T2"),
        waits("in-doubt", unsettled=True),
        waits("twins-no-points", assignments=2),
        waits("second-waits", elsewhere=True),
    }
    assert {
        result_id: view.matched.assignment_id
        for result_id, view in views.items()
        if view.matched is not None
    } == {"no-points": "unscored", "not-shown": "showing", "row-ruled": "second-row"}
    assert {
        result_id: view.linked.assignment_id
        for result_id, view in views.items()
        if view.linked is not None
    } == {"linked": "linked", "first-linked": "second", "row-linked": "first-row"}
    by_id = {item.assignment_id: item for item in items}
    assert views["row-ruled"].candidates == (
        row(by_id["first-row"], another_result=True),
        row(by_id["second-row"]),
    )
    assert views["second-waits"].candidates == (row(by_id["second"], another_result=True),)
    assert views["kept-apart"].candidates == (row(by_id["apart"], APART),)
    assert [views[one].candidates for one in ("no-homework", "unconnected")] == [(), ()]
    for view in views.values():
        assert [view.linked, view.matched, view.waiting].count(None) >= 2


def test_a_result_s_view_is_refused_whole_on_two_confirmed_links_naming_one_result() -> None:
    """Plain values. The view raises the conflict error with no cause and no context, whether
    or not the two links name the result asked about."""
    log, twin = homework(), homework("assignment-twin")
    read = as_read([log, twin], [school_result("result-log"), school_result("result-2")])
    links = [confirmed(LOG_ID, "result-2"), confirmed("assignment-twin", "result-2")]

    for result_id in ("result-log", "result-2"):
        with pytest.raises(GradeValueUndecodable) as refused:
            viewed(result_id, read, links)

        assert type(refused.value) is HomeworkLinksConflict
        assert refused.value.__cause__ is None
        assert refused.value.__context__ is None


def test_a_result_s_view_takes_its_rows_and_rule_answers_from_the_reading_her_pages_use() -> None:
    """One reading: the log's homework, twin practice rows, and a notebook linked to the osmosis
    lab's result. Each result's view from it has the rules' candidates, match and waiting, and
    rows holding the reading's own records; her pages' mapping is the rules' answer on it."""
    twin = "assignment-practice-twin"
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [
                homework(),
                homework(PRACTICE_ID, PRACTICE, date(2026, 8, 12)),
                homework(twin, PRACTICE, date(2026, 8, 12)),
                homework(NOTEBOOK_ID, NOTEBOOK, None),
            ],
            {},
        )
        class_id = with_report(client)
        parent_links(client, class_id, result_named(client, class_id, OSMOSIS), NOTEBOOK_ID)
        of = store.class_named(class_id)
        assert of is not None
        years, links = read_score_facts(store, [BIOLOGY])
        everyone = store.all_assignments()
        read = rule_values(everyone, {}, (), years)
        values = store.class_term(class_id, "T1").current.results
        views = [
            result_homework(
                of,
                "T1",
                one.result_id,
                values[one.result_id],
                read,
                links=links,
                current_year=YEAR,
            )
            for one in read.results
        ]
        shown = scores_shown(
            everyone, {}, (), years, links=links, zone=ZONE, base="/student/grades"
        )
        page = class_details(client, class_id)

    assert read.assignments == everyone
    assert read.homework == [homework_of(item, (), False) for item in everyone]
    assert [one.result_id for one in read.results] == order_on_class_details(page)
    assert set(read.found) == {(class_id, "T1", one.result_id) for one in read.results}
    records = {item.assignment_id: item for item in read.assignments}
    could = {
        one.result_id: one
        for one in candidate_homework(read.homework, read.results, read.classes, decided=links)
    }
    waits = {
        one.result_id: one
        for one in waiting_results(read.homework, read.results, read.classes, decided=links)
    }
    ruled = school_scores(read.homework, read.results, read.classes, decided=links)
    by_the_rules = {at.result_id: one for one, all_at in ruled.items() for at in all_at}
    for view in views:
        held = could.get(view.result_id)
        assert [row.assignment_id for row in view.candidates] == (
            [] if held is None else list(held.assignments)
        )
        for row in (*view.candidates, *view.apart, view.linked, view.matched):
            if row is not None:
                item = records[row.assignment_id]
                assert (row.course, row.title, row.due) == (item.course, item.title, item.due_date)
        matched = None if view.matched is None else view.matched.assignment_id
        linked = None if view.linked is None else view.linked.assignment_id
        assert (linked or matched) == by_the_rules.get(view.result_id)
        assert view.waiting == waits.get(view.result_id)
    assert sorted(view.result_id for view in views if view.waiting) == sorted(
        result_named_in(read, title) for title in (PRACTICE,)
    )
    assert {view.matched.title for view in views if view.matched} == {LOG}
    assert {view.linked.title for view in views if view.linked} == {NOTEBOOK}
    assert list(shown) == list(ruled)


def result_named_in(read: RuleValues, title: str) -> str:
    """The ID of the one result of ``title`` among the values the rules take."""
    (result_id,) = [one.result_id for one in read.results if one.title == title]
    return result_id


def view_now(client: TestClient, class_id: str, result_id: str) -> tuple[ResultHomework, list[str]]:
    """A result's view from the store as it stands, read as her pages read, and the IDs of the
    assignments her pages' mapping shows a score on."""
    store = store_of(client)
    of = store.class_named(class_id)
    current = store.grade_contexts().current
    assert of is not None
    assert current is not None
    everyone = store.all_assignments()
    years, links = read_score_facts(store, [one.course for one in everyone])
    view = result_homework(
        of,
        "T1",
        result_id,
        store.class_term(class_id, "T1").current.results[result_id],
        rule_values(everyone, {}, (), years),
        links=links,
        current_year=current[0],
    )
    shown = scores_shown(everyone, {}, (), years, links=links, zone=ZONE, base="/student/grades")
    return view, sorted(shown)


def test_a_result_s_view_follows_each_decision_a_parent_makes_through_the_store() -> None:
    """The log's result, homework of its title and day, and a notebook under a name a parent
    connects; each decision by the store's own write, then the notebook's row deleted by hand,
    as no save does. A reason is named exactly when her pages show no score for the link."""
    name = "Life Science"
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(), homework(NOTEBOOK_ID, NOTEBOOK, None, course=name)], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)

        def now() -> tuple[ResultHomework, list[str]]:
            return view_now(client, class_id, log)

        on_record = {item.assignment_id: item for item in store.all_assignments()}
        by_the_rules = now()
        parent_links(client, class_id, log, decision="apart")
        apart = now()
        parent_links(client, class_id, log, decision="automatic")
        again = now()
        parent_links(client, class_id, log)
        linked = now()
        store.connect_homework_class(YEAR, name, shown=None, chosen=class_id, role="parent")
        parent_links(client, class_id, log, NOTEBOOK_ID)
        moved = now()
        store.connect_homework_class(YEAR, name, shown=class_id, chosen=UNCONNECTED, role="parent")
        disconnected = now()
        stored(store, "DELETE FROM assignments WHERE assignment_id = ?", NOTEBOOK_ID)
        left = now()

    mine, notebook = on_record[LOG_ID], on_record[NOTEBOOK_ID]
    plain = ResultHomework(
        class_id=class_id,
        term="T1",
        result_id=log,
        class_name=BIOLOGY,
        year=YEAR,
        linked=None,
        matched=row(mine),
        candidates=(row(mine),),
        apart=(),
        withheld=(),
        waiting=None,
    )
    kept = row(mine, APART)
    assert by_the_rules == (plain, [LOG_ID])
    assert apart == (
        dataclasses.replace(plain, matched=None, candidates=(kept,), apart=(kept,)),
        [],
    )
    assert again == by_the_rules
    assert linked == (
        dataclasses.replace(
            plain, matched=None, linked=row(mine, LINKED), candidates=(row(mine, LINKED),)
        ),
        [LOG_ID],
    )
    after_move = dataclasses.replace(
        plain,
        matched=None,
        linked=row(notebook, LINKED),
        candidates=(row(mine, APART, moves_link=True),),
        apart=(row(mine, APART, moves_link=True),),
    )
    assert moved == (after_move, [NOTEBOOK_ID])
    assert disconnected == (
        dataclasses.replace(after_move, withheld=(Withheld.NAME_NOT_CONNECTED,)),
        [],
    )
    assert left == (
        dataclasses.replace(after_move, linked=off_record(NOTEBOOK_ID, LINKED)),
        [],
    )


def test_a_linked_result_s_view_names_points_left_blank_and_a_report_that_does_not_show_it() -> (
    None
):
    """Undated homework of another title linked to the log's result by the store's own write.
    A newer report leaves the log's points blank; then a complete report doesn't show the log.
    Each reason is named while her pages' mapping holds no score, and the link row stays."""
    no_points = IN_WEEK.replace(LOG_ROW, "| Seed Germination Log |         | 20.0    | 90.0    |")
    gone = "\n".join(line for line in IN_WEEK.split("\n") if LOG not in line)
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(title=NOTEBOOK, on=None)], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        parent_links(client, class_id, log)
        rows = as_stored(store, "grade_homework_links")
        shows = view_now(client, class_id, log)
        with_report(client, no_points)
        blank = view_now(client, class_id, log)
        with_report(client, gone, complete=True)
        both = view_now(client, class_id, log)
        rows_after = as_stored(store, "grade_homework_links")

    assert (shows[0].withheld, shows[1]) == ((), [LOG_ID])
    assert (blank[0].withheld, blank[1]) == ((Withheld.POINTS_BLANK,), [])
    assert (both[0].withheld, both[1]) == ((Withheld.POINTS_BLANK, Withheld.NOT_SHOWN), [])
    assert {view.linked.assignment_id for view, _ in (shows, blank, both) if view.linked} == {
        LOG_ID
    }
    assert rows_after == rows


def test_twin_homework_waits_in_a_result_s_view_until_a_parent_links_one_through_the_store() -> (
    None
):
    """Two homework rows with the log's class, title and day. The log's result waits with both
    rows as its candidates; linked to one by the store's own write, it waits no more, both
    rows stay listed, and the other row's says a link would move."""
    twin = "assignment-twin"
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(), homework(twin)], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        on_record = {item.assignment_id: item for item in store.all_assignments()}
        waiting = view_now(client, class_id, log)
        parent_links(client, class_id, log, twin)
        linked = view_now(client, class_id, log)

    mine, other = on_record[LOG_ID], on_record[twin]
    assert waiting[0].candidates == (row(mine), row(other))
    assert waiting[0].waiting == Waiting(class_id, "T1", log, 2, 0, False, False)
    assert (waiting[0].linked, waiting[0].matched, waiting[1]) == (None, None, [])
    assert linked[0].candidates == (row(mine, moves_link=True), row(other, LINKED))
    assert (linked[0].linked, linked[0].waiting, linked[1]) == (row(other, LINKED), None, [twin])


DONE_LOG = homework().model_copy(update={"reported_submission_status": "done"})
"""The log's homework reported done: its status is not among what the rules read."""
TWIN_ROW = homework("assignment-twin")
NOTEBOOK_ROW = homework(NOTEBOOK_ID, NOTEBOOK, None)
UNDATED_LOG = homework(on=None)


@pytest.mark.parametrize(
    ("items", "results", "links", "expected"),
    [
        pytest.param(
            [homework()],
            [school_result("r1")],
            [],
            {"r1": ([LOG_ID], LOG_ID, None)},
            id="one pair by the rule",
        ),
        pytest.param(
            [homework()],
            [school_result("r1")],
            [kept_apart(LOG_ID, "r1")],
            {"r1": ([LOG_ID], None, None)},
            id="one pair kept apart",
        ),
        pytest.param(
            [NOTEBOOK_ROW],
            [school_result("r1")],
            [confirmed(NOTEBOOK_ID, "r1")],
            {"r1": ([], None, NOTEBOOK_ID)},
            id="titles differ, linked",
        ),
        pytest.param(
            [UNDATED_LOG],
            [school_result("r1")],
            [confirmed(LOG_ID, "r1")],
            {"r1": ([LOG_ID], None, LOG_ID)},
            id="homework undated, linked",
        ),
        pytest.param(
            [homework(), TWIN_ROW],
            [school_result("r1")],
            [],
            {"r1": ([LOG_ID, "assignment-twin"], None, None)},
            id="twin homework",
        ),
        pytest.param(
            [DONE_LOG, TWIN_ROW],
            [school_result("r1")],
            [],
            {"r1": ([LOG_ID, "assignment-twin"], None, None)},
            id="twin homework, one done",
        ),
        pytest.param(
            [homework(), TWIN_ROW],
            [school_result("r1")],
            [confirmed(LOG_ID, "r1")],
            {"r1": ([LOG_ID, "assignment-twin"], None, LOG_ID)},
            id="twin homework, one linked",
        ),
        pytest.param(
            [homework(), TWIN_ROW],
            [school_result("r1")],
            [kept_apart(LOG_ID, "r1")],
            {"r1": ([LOG_ID, "assignment-twin"], "assignment-twin", None)},
            id="twin homework, one kept apart",
        ),
        pytest.param(
            [homework()],
            [school_result("r1"), school_result("r2")],
            [],
            {"r1": ([LOG_ID], None, None), "r2": ([LOG_ID], None, None)},
            id="twin results",
        ),
        pytest.param(
            [homework()],
            [school_result("r1"), school_result("r2")],
            [confirmed(LOG_ID, "r1")],
            {"r1": ([LOG_ID], None, LOG_ID), "r2": ([LOG_ID], None, None)},
            id="twin results, one linked",
        ),
        pytest.param(
            [homework()],
            [school_result("r1"), school_result("r2")],
            [confirmed(LOG_ID, "r1"), confirmed(LOG_ID, "r2")],
            {"r1": ([LOG_ID], None, LOG_ID), "r2": ([LOG_ID], None, LOG_ID)},
            id="twin results, both linked",
        ),
        pytest.param(
            [homework()],
            [school_result("r1"), school_result("r2", term="T2")],
            [confirmed(LOG_ID, "r2")],
            {"r1": ([LOG_ID], None, None), "r2": ([LOG_ID], None, LOG_ID)},
            id="twin results in two terms, the later linked",
        ),
        pytest.param(
            [homework(), TWIN_ROW],
            [school_result("r1"), school_result("r2")],
            [confirmed(LOG_ID, "r1")],
            {
                "r1": ([LOG_ID, "assignment-twin"], None, LOG_ID),
                "r2": ([LOG_ID, "assignment-twin"], "assignment-twin", None),
            },
            id="twin homework and twin results, one pair linked",
        ),
        pytest.param(
            [homework(), NOTEBOOK_ROW],
            [school_result("r1")],
            [confirmed(NOTEBOOK_ID, "r1")],
            {"r1": ([LOG_ID], None, NOTEBOOK_ID)},
            id="the rule's homework beside a link to other homework",
        ),
        pytest.param(
            [homework()],
            [school_result("r1"), school_result("r2", NOTEBOOK, scored=False)],
            [confirmed(LOG_ID, "r2")],
            {"r1": ([LOG_ID], None, None), "r2": ([], None, LOG_ID)},
            id="linked to a result without points, beside the rule's own result",
        ),
    ],
)
def test_a_view_s_candidates_and_match_are_the_rules_own_for_twins_and_single_pairs(
    items: list[Assignment],
    results: list[Result],
    links: list[Link],
    expected: dict[str, tuple[list[str], str | None, str | None]],
) -> None:
    """Plain values. Each result's view lists the candidates, the homework the rules stand it
    with, and the linked homework given below, and the homework it is matched with is the one
    the score rules show it on."""
    read = as_read(items, results)

    views = {one.result_id: viewed(one.result_id, read, links, term=one.term) for one in results}

    assert {
        result_id: (
            [one.assignment_id for one in view.candidates],
            None if view.matched is None else view.matched.assignment_id,
            None if view.linked is None else view.linked.assignment_id,
        )
        for result_id, view in views.items()
    } == expected
    ruled = school_scores(read.homework, read.results, read.classes, decided=links)
    assert {
        at.result_id: assignment_id
        for assignment_id, shown in ruled.items()
        for at in shown
        if not at.linked
    } == {
        result_id: view.matched.assignment_id
        for result_id, view in views.items()
        if view.matched is not None
    }


@pytest.mark.parametrize(
    ("value", "withheld"),
    [
        pytest.param(value_with(), (), id="the score shows"),
        pytest.param(value_with(Presence.BLANK), (Withheld.POINTS_BLANK,), id="points blank"),
        pytest.param(
            value_with(Presence.UNREADABLE), (Withheld.POINTS_UNREADABLE,), id="points unreadable"
        ),
        pytest.param(
            value_with(Presence.NOT_CAPTURED),
            (Withheld.POINTS_NOT_IN_COPY,),
            id="points not in the copy",
        ),
        pytest.param(value_with(shown=False), (Withheld.NOT_SHOWN,), id="not shown"),
        pytest.param(
            value_with(Presence.UNREADABLE, shown=False),
            (Withheld.POINTS_UNREADABLE, Withheld.NOT_SHOWN),
            id="points unreadable and not shown",
        ),
    ],
)
def test_a_matched_result_names_each_reason_its_score_stays_off_her_homework(
    value: CurrentValue, withheld: tuple[Withheld, ...]
) -> None:
    """Plain values: one homework and one result the rules stand together, its name connected
    or not. The view names the points and showing reasons exactly when the score rules show
    no score, never the class connection, and the match, the candidates and waiting stay."""
    log = homework()
    scored = value.cells["points"][0] is Presence.REPORTED
    results = [school_result("result-log", scored=scored, shown=value.not_shown is None)]
    read = as_read([log], results)

    view = viewed("result-log", read, value=value)
    other_year = viewed("result-log", read, value=value, current_year="2027-2028")

    assert view.withheld == withheld
    assert (view.matched, view.linked, view.waiting) == (row(log), None, None)
    assert view.candidates == (row(log),)
    assert other_year == view
    shows = school_scores(read.homework, read.results, read.classes)
    assert bool(shows) is (withheld == ())


def test_a_result_that_waits_names_no_reason_beside_the_rules_answer() -> None:
    """Plain values: twin homework and a result whose points are blank and that a newer
    report doesn't show. It waits with both rows as candidates, and no reason is named."""
    log, twin = homework(), homework("assignment-twin")
    results = [school_result("result-log", scored=False, shown=False)]
    read = as_read([log, twin], results)

    view = viewed("result-log", read, value=value_with(Presence.BLANK, shown=False))

    assert view.withheld == ()
    assert view.waiting == Waiting(THE_CLASS.class_id, "T1", "result-log", 2, 0, False, False)
    assert (view.matched, view.linked) == (None, None)


def test_a_result_s_view_holds_closed_fields_and_closed_sets_of_values() -> None:
    assert [field.name for field in dataclasses.fields(ResultHomework)] == [
        "class_id",
        "term",
        "result_id",
        "class_name",
        "year",
        "linked",
        "matched",
        "candidates",
        "apart",
        "withheld",
        "waiting",
    ]
    assert [field.name for field in dataclasses.fields(HomeworkRow)] == [
        "assignment_id",
        "on_record",
        "course",
        "title",
        "due",
        "standing",
        "moves_link",
        "another_result",
    ]
    assert list(PairStanding) == ["none", "linked", "apart"]
    assert list(Withheld) == [
        "points_blank",
        "points_unreadable",
        "points_not_in_copy",
        "not_shown",
        "name_not_connected",
        "name_not_connected_in_another_year",
    ]


RESULT_PAGE = "/parent/grades/results/{result_id}/homework"
ON_THE_COMPUTER = "127.0.0.1:8781"


def computer(client: TestClient, *, quiet: bool = False) -> TestClient:
    """The same application reached from the computer it runs on, where a family grade page
    opens with the sign-in off; ``quiet`` answers a failure no route catches with a bare 500."""
    return TestClient(
        client.app,
        follow_redirects=False,
        headers={"Host": ON_THE_COMPUTER, "Origin": f"http://{ON_THE_COMPUTER}"},
        client=("127.0.0.1", 50000),
        raise_server_exceptions=not quiet,
    )


NO_POINTS = IN_WEEK.replace(LOG_ROW, "| Seed Germination Log |         | 20.0    | 90.0    |")
WITHOUT_LOG = "\n".join(line for line in IN_WEEK.split("\n") if LOG not in line)
LOG_DUE = "Due Thursday, August 20"
LOG_ROW_SAID = [BIOLOGY, LOG, LOG_DUE]
CHOOSE = ["Link to this homework", "Not this homework"]
ANOTHER = (
    "This homework already has another linked school result. You can link this result too or "
    "keep them apart."
)
SYNTHETIC_CAUSE = "synthetic-cause-text-for-the-boundary"


def result_page(client: TestClient, result_id: str) -> str:
    """A result's page as a parent opens it, which answers 200."""
    answer = computer(client).get(RESULT_PAGE.format(result_id=result_id), headers=PAGE_HEADERS)
    assert answer.status_code == 200, answer.status_code
    return answer.text


def card_of(page: str, result_id: str) -> str:
    """The card of the result ``result_id`` on a page."""
    at = page.index(f'id="{result_id}"')
    start = page.rindex('<section class="panel grade-card">', 0, at)
    return page[start : page.index("</section>", at) + len("</section>")]


def said_on(page: str) -> list[str]:
    """Each line a reader reads on a result's page after its result card, in order: the
    paragraphs, list items, homework headings and buttons, each as its text alone."""
    start = page.index('<h1 id="homework"')
    rest = page[page.index("</section>", start) + len("</section>") : page.index("</main>")]
    return [
        " ".join(html.unescape(re.sub(r"<[^>]+>", "", found[2])).split())
        for found in re.finditer(r"<(p|li|h2|button)\b[^>]*>(.*?)</\1>", rest, re.S)
    ]


def forms_on(page: str) -> list[tuple[str, str, str, str]]:
    """Each form on a page with what it sends: its action, homework, decision and revision."""
    found = re.findall(
        r'<form method="post" action="([^"]*)">\s*'
        r'<input type="hidden" name="homework" value="([^"]*)">\s*'
        r'<input type="hidden" name="decision" value="([^"]*)">\s*'
        r'<input type="hidden" name="revision" value="([^"]*)">',
        page,
    )
    return [
        (html.unescape(a), html.unescape(b), html.unescape(c), html.unescape(d))
        for a, b, c, d in found
    ]


def revision_of(client: TestClient, result_id: str) -> str:
    place = store_of(client).result_place(result_id)
    assert place is not None
    return str(place.revision)


def test_a_matched_result_s_page_draws_its_card_and_a_form_for_each_decision_on_its_homework() -> (
    None
):
    """The page shows the result's card as her class details draws it, says the rules matched it,
    and lists its homework with a form for each decision. Every address on the page holds the
    result's ID alone; the homework, the decision and the revision travel in the forms."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        page = result_page(client, log)
        details = class_details(client, class_id)
        revision = revision_of(client, log)

    address = RESULT_PAGE.format(result_id=log)
    assert said_on(page) == [
        f"Matched by class, title and due date to {LOG}.",
        *LOG_ROW_SAID,
        *CHOOSE,
    ]
    assert forms_on(page) == [
        (address, LOG_ID, "link", revision),
        (address, LOG_ID, "apart", revision),
    ]
    assert card_of(page, log).replace("h2 ", "h3 ").replace("</h2>", "</h3>") == card_of(
        details, log
    )
    assert '<h1 id="homework" tabindex="-1">Homework for this result</h1>' in page
    assert re.search(
        r'<p class="course">School result</p>\s*<section class="panel grade-card">', page
    )
    assert '<p>T1 · <span class="year-range">2026-2027</span></p>' in page
    assert (
        len(
            re.findall(
                r'<div class="actions">\s*(?:<form [^>]*>.*?</form>\s*){2}</div>', page, re.S
            )
        )
        == 1
    )
    assert ties_hold(page)
    back = f'<a href="/parent/grades/classes/{class_id}/terms/1">Back to '
    assert back + f'<span class="authored-text">{BIOLOGY}</span></a>' in page
    addresses = re.findall(r'(?:href|action)="([^"]*)"', page)
    assert LOG_ID not in " ".join(addresses)
    assert [one for one in addresses if "/results/" in one] == [address, address]


def test_a_result_s_page_says_what_each_decision_on_record_leaves() -> None:
    """Kept apart, then left to the rules again, then linked, each by the store's own write:
    the page says each state and offers the buttons that state leaves."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        parent_links(client, class_id, log, decision="apart")
        apart = said_on(result_page(client, log))
        parent_links(client, class_id, log, decision="automatic")
        again = said_on(result_page(client, log))
        parent_links(client, class_id, log)
        linked = said_on(result_page(client, log))

    assert apart == [
        "Not linked to homework.",
        *LOG_ROW_SAID,
        f"Kept apart from {LOG}. Blossom won't match these two by itself.",
        "Link to this homework",
        "Let Blossom match again",
    ]
    assert again == [f"Matched by class, title and due date to {LOG}.", *LOG_ROW_SAID, *CHOOSE]
    assert linked == [f"Linked to {LOG}.", *LOG_ROW_SAID, "Remove link"]


def test_a_result_s_page_says_why_twin_homework_leaves_it_waiting() -> None:
    twin = "assignment-twin"
    with browser() as client:
        store_of(client).put_on_record([homework(), homework(twin)], {})
        class_id = with_report(client)
        twins = said_on(result_page(client, result_named(client, class_id, LOG)))

    assert twins == [
        "Not linked to homework.",
        "Blossom doesn't match these by itself:",
        "2 assignments could be this result.",
        *LOG_ROW_SAID,
        *CHOOSE,
        *LOG_ROW_SAID,
        *CHOOSE,
    ]


def test_a_twin_result_s_page_says_why_it_waits_and_then_that_its_homework_has_a_link() -> None:
    """Twin results in two terms; then the first linked by the store's own write: the second's
    page says its homework already has another linked result, above and on the row."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        with_report(client, SECOND_TERM)
        first, second = (result_named(client, class_id, LOG, term) for term in ("T1", "T2"))
        both = [said_on(result_page(client, one)) for one in (first, second)]
        parent_links(client, class_id, first)
        after = said_on(result_page(client, second))

    assert (
        both
        == [
            [
                "Not linked to homework.",
                "Blossom doesn't match these by itself:",
                "2 results could be this homework.",
                *LOG_ROW_SAID,
                *CHOOSE,
            ]
        ]
        * 2
    )
    assert after == [
        "Not linked to homework.",
        "Blossom doesn't match these by itself:",
        ANOTHER,
        *LOG_ROW_SAID,
        ANOTHER,
        *CHOOSE,
    ]


def test_a_result_s_page_says_a_link_would_move_from_homework_on_record_or_not() -> None:
    """The log's result linked to a notebook under a connected name, by the store's write;
    then the notebook's row deleted from the file by hand, as no save does."""
    name = "Life Science"
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(), homework(NOTEBOOK_ID, NOTEBOOK, None, course=name)], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        store.connect_homework_class(YEAR, name, shown=None, chosen=class_id, role="parent")
        parent_links(client, class_id, log, NOTEBOOK_ID)
        on_record = said_on(result_page(client, log))
        stored(store, "DELETE FROM assignments WHERE assignment_id = ?", NOTEBOOK_ID)
        page = result_page(client, log)

    assert on_record == [
        f"Linked to {NOTEBOOK}.",
        name,
        NOTEBOOK,
        "Due date not recorded",
        "Remove link",
        *LOG_ROW_SAID,
        f"Linking moves this result's link from {NOTEBOOK}.",
        *CHOOSE,
    ]
    gone = "Linked to homework that isn't on record any more."
    assert said_on(page) == [
        gone,
        "Remove link",
        *LOG_ROW_SAID,
        "Linking moves this result's link from homework that isn't on record any more.",
        *CHOOSE,
    ]
    assert '<p id="state">Linked to homework that isn&#39;t on record any more.</p>' in page
    assert '<section class="panel grade-card" aria-labelledby="state">' in page
    assert ties_hold(page)
    assert [
        decision for _, homework_id, decision, _ in forms_on(page) if homework_id == NOTEBOOK_ID
    ] == ["apart"]


REASONS = "The score isn't shown on her homework:"
BLANK_SAID = "the points cell was left blank."
NOT_SHOWN_SAID = "the newest complete report doesn't show this result."


def test_a_linked_result_s_page_names_each_reason_its_score_is_withheld_in_order() -> None:
    """A notebook under a connected name linked to the log's result; then a newer report
    leaves the points blank, the name is disconnected, and a complete report stops showing
    the result, each by the store's own writes. Each reason is said, in that order."""
    name = "Life Science"
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(NOTEBOOK_ID, NOTEBOOK, None, course=name)], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        store.connect_homework_class(YEAR, name, shown=None, chosen=class_id, role="parent")
        parent_links(client, class_id, log, NOTEBOOK_ID)
        shows = said_on(result_page(client, log))
        with_report(client, NO_POINTS)
        blank = said_on(result_page(client, log))
        store.connect_homework_class(YEAR, name, shown=class_id, chosen=UNCONNECTED, role="parent")
        disconnected = result_page(client, log)
        with_report(client, WITHOUT_LOG, complete=True)
        all_three = said_on(result_page(client, log))

    linked = f"Linked to {NOTEBOOK}."
    notebook = [name, NOTEBOOK, "Due date not recorded", "Remove link"]
    unconnected = f"{name} isn't connected to {BIOLOGY} now."
    assert shows == [linked, *notebook]
    assert blank == [linked, REASONS, BLANK_SAID, *notebook]
    assert said_on(disconnected) == [
        linked,
        REASONS,
        BLANK_SAID,
        unconnected,
        "Homework class names",
        *notebook,
    ]
    assert '<a href="/parent/grades/homework-classes">Homework class names</a>' in disconnected
    assert all_three == [
        linked,
        REASONS,
        BLANK_SAID,
        NOT_SHOWN_SAID,
        unconnected,
        "Homework class names",
        *notebook,
    ]


def test_a_matched_result_s_page_names_the_points_and_showing_reasons_alone() -> None:
    """The log's homework, matched by the rules; then a newer report leaves the points blank,
    and a complete report stops showing the result. The match and its choices stay."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        with_report(client, NO_POINTS)
        blank = said_on(result_page(client, log))
        with_report(client, WITHOUT_LOG, complete=True)
        both = said_on(result_page(client, log))

    matched = f"Matched by class, title and due date to {LOG}."
    assert blank == [matched, REASONS, BLANK_SAID, *LOG_ROW_SAID, *CHOOSE]
    assert both == [matched, REASONS, BLANK_SAID, NOT_SHOWN_SAID, *LOG_ROW_SAID, *CHOOSE]


def test_the_words_for_each_reason_and_each_decision_left_on_a_row() -> None:
    """Plain values: each reason a score stays off her homework in its words, a class of
    another year's among them, and each row's words and buttons for each decision."""
    notebook = HomeworkRow(NOTEBOOK_ID, True, "Life Science", NOTEBOOK, None, LINKED, False, False)
    view = ResultHomework(
        class_id=THE_CLASS.class_id,
        term="T1",
        result_id="result-log",
        class_name=BIOLOGY,
        year="2025-2026",
        linked=notebook,
        matched=None,
        candidates=(),
        apart=(),
        withheld=tuple(Withheld),
        waiting=Waiting(THE_CLASS.class_id, "T1", "result-log", 2, 3, True, True),
    )
    state, withheld, waiting = result_said(view)
    log = HomeworkRow(LOG_ID, True, BIOLOGY, LOG, THURSDAY, PairStanding.NONE, True, True)

    assert state.text == f"Linked to {NOTEBOOK}."
    assert [one.text for one in withheld] == [
        "the points cell was left blank.",
        "the points couldn't be read.",
        "the points weren't in the copy.",
        "the newest complete report doesn't show this result.",
        f"Life Science isn't connected to {BIOLOGY} now.",
        f"Life Science isn't connected to {BIOLOGY}, a class of 2025-2026. Homework class names "
        "changes the current school year only.",
    ]
    assert [one.text for one in waiting] == [
        "2 assignments could be this result.",
        "3 results could be this homework.",
        ANOTHER,
        "a due date isn't settled.",
    ]
    rows = {
        standing: row_shown(1, dataclasses.replace(log, standing=standing), notebook)
        for standing in PairStanding
    }
    assert [said.text for said in rows[PairStanding.NONE].said] == [
        f"Linking moves this result's link from {NOTEBOOK}.",
        ANOTHER,
    ]
    assert rows[PairStanding.NONE].buttons == [("link", CHOOSE[0]), ("apart", CHOOSE[1])]
    assert rows[PairStanding.APART].buttons == [
        ("link", "Link to this homework"),
        ("automatic", "Let Blossom match again"),
    ]
    assert rows[PairStanding.LINKED].buttons == [("apart", "Remove link")]
    assert rows[PairStanding.LINKED].said == []
    gone = row_shown(2, off_record("assignment-gone", APART), notebook)
    assert [one.text for one in gone.said] == [
        "Kept apart from homework that isn't on record any more."
    ]
    assert gone.buttons == [("automatic", "Let Blossom match again")]


@pytest.mark.parametrize(
    "cause",
    [
        pytest.param("no such shape", id="an ID of another shape"),
        pytest.param("not on record", id="a result not on record"),
        pytest.param("no current value", id="a result with no current value"),
    ],
)
def test_a_result_s_page_answers_404_for_an_id_that_names_no_current_result(cause: str) -> None:
    """A result with no current value is made by marking its report kept as earlier in the
    file by hand, as no save does."""
    with browser() as client:
        store = store_of(client)
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        if cause == "no current value":
            stored(store, "UPDATE grade_reports SET use = 'earlier'")
        asked = {
            "no such shape": "result-not-a-result",
            "not on record": "result-" + "0" * 32,
            "no current value": log,
        }[cause]
        answer = computer(client).get(RESULT_PAGE.format(result_id=asked), headers=PAGE_HEADERS)

    assert answer.status_code == 404
    assert '<p class="problem" role="alert" id="problem">' in answer.text
    assert "Blossom has no current record of this result." in answer.text
    assert '<a href="/parent/grades">Back to Grades</a>' in answer.text
    assert "<form" not in answer.text


@pytest.mark.parametrize("named", [LOG, DIAGRAM])
@pytest.mark.parametrize("failure", ["a stored value", "two links"])
def test_a_result_s_page_stopped_by_a_grade_failure_answers_the_generic_500_and_one_record(
    failure: str, named: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A points presence that is no presence, whose decoding error holds a chosen text; or two
    confirmed links to the log's result in a file changed by hand. The page answers as an
    unanswered failure does, nothing is raised, and one record names the error's type alone."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log, asked = result_named(client, class_id, LOG), result_named(client, class_id, named)
        if failure == "a stored value":
            points_presence(client, log, SYNTHETIC_CAUSE)
        else:
            conflicting(client, log)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            answer = computer(client).get(RESULT_PAGE.format(result_id=asked), headers=PAGE_HEADERS)
            records = list(caplog.records)
        quiet = computer(client, quiet=True)
        unanswered = quiet.get("/parent/grades/nowhere-" + "x", headers=PAGE_HEADERS)
        points_presence(client, log, SYNTHETIC_CAUSE)
        failing = quiet.get(f"/student/grades/classes/{class_id}/terms/1", headers=PAGE_HEADERS)

    kind = "GradeValueUndecodable" if failure == "a stored value" else "HomeworkLinksConflict"
    assert (answer.status_code, answer.text) == (500, "Internal Server Error")
    assert (failing.status_code, failing.text) == (500, "Internal Server Error")
    assert answer.headers["content-type"] == failing.headers["content-type"]
    assert unanswered.status_code == 404
    application = [one for one in records if one.name.startswith("blossom")]
    assert [one.getMessage() for one in application] == [f"a grade page wasn't shown: {kind}"]
    assert [(one.name, one.levelno) for one in application] == [
        ("blossom.routes.grades", logging.WARNING)
    ]
    assert [one for one in records if one.exc_info or SYNTHETIC_CAUSE in one.getMessage()] == []
    assert SYNTHETIC_CAUSE not in answer.text


def test_an_unrelated_error_inside_a_result_s_page_is_not_caught(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A database error from her links' read leaves the route as itself, with no record."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)

        def refused() -> None:
            msg = "synthetic"
            raise sqlite3.DatabaseError(msg)

        monkeypatch.setattr(store_of(client), "homework_links", refused)
        with caplog.at_level(logging.DEBUG), pytest.raises(sqlite3.DatabaseError):
            computer(client).get(RESULT_PAGE.format(result_id=log), headers=PAGE_HEADERS)

    assert not [one for one in caplog.records if "wasn't shown" in one.getMessage()]


WAY_IN = re.compile(
    r'<p class="details-link"><a class="assignment-link" href="([^"]*)" '
    r'aria-label="([^"]*)">Homework for this result</a></p>'
)
"""A parent's way from a result's card on class details to its homework page."""


def ways_on(page: str) -> list[tuple[str, str]]:
    """Each way to a result's homework page on class details, as its address and its name."""
    return [(html.unescape(href), html.unescape(name)) for href, name in WAY_IN.findall(page)]


@pytest.mark.parametrize(
    ("reader", "trees"),
    [("parent", ("student", "parent")), ("her", ("student",))],
)
def test_class_details_gives_a_parent_each_result_s_way_in_and_its_linked_homework(
    reader: str, trees: tuple[str, ...], tmp_path: pathlib.Path
) -> None:
    """With the sign-in on, a parent reading class details on either tree gets a way to each
    result's homework page and, on a result a link names, its homework; she gets neither,
    and her page reads as it does with no link on record."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        store_of(client).put_on_record([homework(NOTEBOOK_ID, NOTEBOOK, None)], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        before = [class_details(client, class_id, tree=tree) for tree in trees]
        parent_links(client, class_id, log, NOTEBOOK_ID)
        pages = [class_details(client, class_id, tree=tree) for tree in trees]
        titles = {
            result_id: value.cells["assignment"][1]
            for result_id, value in store_of(client)
            .class_term(class_id, "T1")
            .current.results.items()
        }

    if reader == "her":
        assert pages == before
        assert ways_on(pages[0]) == []
        assert "Linked to homework" not in pages[0]
        return
    for page in pages:
        assert sorted(ways_on(page)) == sorted(
            (
                f"{RESULT_PAGE.format(result_id=result_id)}#homework",
                f"Homework for this result: {title}",
            )
            for result_id, title in titles.items()
        )
        card = card_of(page, log)
        assert f'<p><span class="authored-text">Linked to homework: {NOTEBOOK}.</span></p>' in card
        assert page.count("Linked to homework:") == 1


def test_class_details_as_she_reads_it_asks_the_same_statements_with_links_on_record() -> None:
    """With the sign-in off, her class details asks the same statements with a link on record as
    with none, and none of a link, a homework row or a hold; a parent's asks hers, then, inside
    one hold, her links and the linked homework's row."""
    her_record = " ".join(gradebook.HER_NAME_RECORD.split())

    def asked(client: TestClient, address: str) -> list[str]:
        seen: list[str] = []
        connection = store_of(client)._connection
        connection.set_trace_callback(seen.append)
        try:
            assert client.get(address, headers=PAGE_HEADERS).status_code == 200
        finally:
            connection.set_trace_callback(None)
        return [" ".join(statement.split()) for statement in seen]

    with browser() as client:
        store_of(client).put_on_record([homework(NOTEBOOK_ID, NOTEBOOK, None)], {})
        class_id = with_report(client)
        hers = f"/student/grades/classes/{class_id}/terms/1"
        parents = f"/parent/grades/classes/{class_id}/terms/1"
        with_none = asked(client, hers)
        parent_links(client, class_id, result_named(client, class_id, LOG), NOTEBOOK_ID)
        with_one = asked(client, hers)
        parent = asked(computer(client), parents)
        her_links = gradebook.HOMEWORK_LINKS.replace("?", f"'{store_of(client).student_id()}'")

    assert with_one == with_none
    assert not [one for one in with_one if "grade_homework_links" in one or "assignments" in one]
    assert not [one for one in with_one if one.startswith(("BEGIN", "COMMIT", "ROLLBACK"))]
    at = parent.index(her_links)
    assert parent[0].startswith("BEGIN")
    assert parent[1 : at - 1] == with_one
    assert parent[at - 1] == her_record
    (title_read,) = parent[at + 1 : -1]
    assert title_read.endswith(f"FROM assignments WHERE assignment_id = '{NOTEBOOK_ID}'")
    assert parent[-1] == "COMMIT"
    assert parent.count(her_links) == 1


@pytest.mark.parametrize("failure", ["two links", "a link value"])
def test_class_details_stops_a_parent_s_page_at_her_links_with_the_generic_500_and_one_record(
    failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Two confirmed links to the log's result, or a link row whose homework ID is text that
    is no UTF-8 holding a chosen text, each in a file changed by hand. A parent's page answers
    as an unanswered failure does, with one record of the type alone; hers opens."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        if failure == "two links":
            conflicting(client, log)
        else:
            parent_links(client, class_id, log)
            stored(
                store_of(client),
                "UPDATE grade_homework_links SET assignment_id = CAST(? AS TEXT)",
                SYNTHETIC_CAUSE.encode() + b"\xff",
            )
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            answer = computer(client).get(
                f"/parent/grades/classes/{class_id}/terms/1", headers=PAGE_HEADERS
            )
            records = list(caplog.records)
        hers = client.get(f"/student/grades/classes/{class_id}/terms/1", headers=PAGE_HEADERS)

    kind = "HomeworkLinksConflict" if failure == "two links" else "GradeValueUndecodable"
    assert (answer.status_code, answer.text) == (500, "Internal Server Error")
    assert answer.headers["content-type"] == "text/plain; charset=utf-8"
    application = [one for one in records if one.name.startswith("blossom")]
    assert [(one.name, one.levelno, one.getMessage()) for one in application] == [
        ("blossom.routes.grades", logging.WARNING, f"a grade page wasn't shown: {kind}")
    ]
    assert [one for one in records if one.exc_info or SYNTHETIC_CAUSE in one.getMessage()] == []
    assert hers.status_code == 200
    assert ways_on(hers.text) == []


def test_class_details_own_failure_comes_before_her_links_and_is_raised_as_today(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A points presence that is no presence in this class and two confirmed links, in a file
    changed by hand: a parent's page fails with the stored value's own error, her links are
    not asked for, and no record is written."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        conflicting(client, log)
        points_presence(client, log, SMUDGED)
        seen: list[str] = []
        connection = store_of(client)._connection
        connection.set_trace_callback(seen.append)
        with caplog.at_level(logging.DEBUG), pytest.raises(GradeValueUndecodable) as raised:
            computer(client).get(f"/parent/grades/classes/{class_id}/terms/1", headers=PAGE_HEADERS)
        connection.set_trace_callback(None)

    assert type(raised.value) is GradeValueUndecodable
    assert not [one for one in seen if "grade_homework_links" in one]
    assert not [one for one in caplog.records if "wasn't shown" in one.getMessage()]


def test_an_unrelated_error_from_her_links_on_class_details_is_raised_as_itself(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with browser() as client:
        class_id = with_report(client)

        def refused() -> None:
            msg = "synthetic"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store_of(client), "homework_links", refused)
        with caplog.at_level(logging.DEBUG), pytest.raises(sqlite3.OperationalError):
            computer(client).get(f"/parent/grades/classes/{class_id}/terms/1", headers=PAGE_HEADERS)

    assert not [one for one in caplog.records if "wasn't shown" in one.getMessage()]


def test_a_pair_kept_apart_from_homework_not_on_record_is_said_with_no_title() -> None:
    """The log's result kept apart from its homework by the store's write; then that homework's
    row deleted from the file by hand, as no save does."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        parent_links(client, class_id, log, decision="apart")
        stored(store_of(client), "DELETE FROM assignments WHERE assignment_id = ?", LOG_ID)
        page = result_page(client, log)

    assert said_on(page) == [
        "Not linked to homework.",
        "Kept apart from homework that isn't on record any more.",
        "Let Blossom match again",
    ]
    assert ties_hold(page)
    assert [decision for _, _, decision, _ in forms_on(page)] == ["automatic"]


LINKS = "grade_homework_links"
REVISIONS = "grade_scope_revisions"
LINKS_CHANGED = (
    "Grades or links for this class changed while you were choosing. Nothing was saved. "
    "Check this page again."
)
INDEX_UNAVAILABLE = (
    "a homework link wasn't written: the required homework-links index is unavailable"
)
WRONG_INDEXES = {
    "not unique": gradebook.HOMEWORK_LINKS_INDEX_DEFINITION.replace("UNIQUE ", ""),
    "other columns": gradebook.HOMEWORK_LINKS_INDEX_DEFINITION.replace(
        "(student_id, result_id)", "(student_id, assignment_id)"
    ),
    "another predicate": gradebook.HOMEWORK_LINKS_INDEX_DEFINITION.replace(
        "'confirmed'", "'rejected'"
    ),
    "no predicate": gradebook.HOMEWORK_LINKS_INDEX_DEFINITION.split("\nWHERE")[0],
    "the same meaning spelled apart": " ".join(
        gradebook.HOMEWORK_LINKS_INDEX_DEFINITION.lower().split()
    ),
}
"""Each index of the required name with a definition other than the one the application makes."""


class Answered(Protocol):
    """What a test reads of an answer: its status, its body and its headers."""

    @property
    def status_code(self) -> int: ...

    @property
    def text(self) -> str: ...

    @property
    def headers(self) -> Mapping[str, str]: ...


def pressed(
    client: TestClient,
    result_id: str,
    homework_id: str = LOG_ID,
    decision: str = "link",
    *,
    revision: str | None = None,
) -> Answered:
    """A decision posted from the computer the application runs on, as the page's form sends
    it, at the revision of the result's class and term unless ``revision`` says otherwise."""
    sent = {
        "homework": homework_id,
        "decision": decision,
        "revision": revision_of(client, result_id) if revision is None else revision,
    }
    return computer(client).post(
        RESULT_PAGE.format(result_id=result_id), data=sent, headers=PAGE_HEADERS
    )


def stored_links(client: TestClient) -> list[tuple[str, str]]:
    """Each of her links as its homework and its stored state, by the store's own read."""
    return sorted((one.assignment_id, one.state) for one in store_of(client).homework_links())


def everything_else(client: TestClient) -> dict[str, object]:
    """Every table of the household's file but the link table and the revisions."""
    return closed_world([database_of(client)], leaving_out={LINKS, REVISIONS})


def problem_on(page: str) -> str:
    found = re.search(r'<p class="problem" role="alert" id="problem">([^<]*)</p>', page)
    assert found is not None
    return html.unescape(found[1])


def test_each_decision_from_each_prior_state_writes_its_row_and_lands_on_the_heading() -> None:
    """Link, remove the link, link again, let the rules match again, then not this homework:
    each answers 303 to the result's heading, changes only the link row and raises the class
    and term's revision by one, and the page it lands on focuses the heading."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        others = everything_else(client)
        steps = []
        for decision in ("link", "apart", "link", "apart", "automatic", "apart"):
            before = int(revision_of(client, log))
            answer = pressed(client, log, decision=decision)
            steps.append((answer.status_code, answer.headers["location"], stored_links(client)))
            assert int(revision_of(client, log)) == before + 1
        landed = computer(client).get(steps[0][1], headers=PAGE_HEADERS)
        unchanged = everything_else(client)

    at = f"/parent/grades/results/{log}/homework#homework"
    assert steps == [
        (303, at, [(LOG_ID, "confirmed")]),
        (303, at, [(LOG_ID, "rejected")]),
        (303, at, [(LOG_ID, "confirmed")]),
        (303, at, [(LOG_ID, "rejected")]),
        (303, at, []),
        (303, at, [(LOG_ID, "rejected")]),
    ]
    assert unchanged == others
    assert '<h1 id="homework" tabindex="-1">Homework for this result</h1>' in landed.text


@pytest.mark.parametrize(
    ("setup", "decision"),
    [
        pytest.param("link", "link", id="linked, linked again"),
        pytest.param("apart", "apart", id="kept apart, kept apart again"),
        pytest.param(None, "automatic", id="no decision, left to the rules"),
    ],
)
def test_a_decision_already_on_record_answers_303_and_writes_nothing(
    setup: LinkDecision | None, decision: str
) -> None:
    """Pressed again, at the revision of its page or at an earlier one, the decision stands: 303
    to the heading, and the file is as it was."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        if setup is not None:
            parent_links(client, class_id, log, decision=setup)
        before = every_row(database_of(client))
        again = pressed(client, log, decision=decision)
        old = pressed(client, log, decision=decision, revision="1")
        after = every_row(database_of(client))

    at = f"/parent/grades/results/{log}/homework#homework"
    assert [(one.status_code, one.headers["location"]) for one in (again, old)] == [(303, at)] * 2
    assert after == before


def test_a_link_to_other_homework_moves_the_link_and_an_old_remove_link_stands() -> None:
    """Linked to homework A, then linked to homework B: A's pair is kept apart and B's linked
    in one write. An old page's "Remove link" for A then stands, writes nothing, and the
    page it lands on says the result is linked to B."""
    name = "Life Science"
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(), homework(NOTEBOOK_ID, NOTEBOOK, None, course=name)], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        store.connect_homework_class(YEAR, name, shown=None, chosen=class_id, role="parent")
        old_revision = revision_of(client, log)
        first = pressed(client, log)
        moved = pressed(client, log, NOTEBOOK_ID)
        rows = stored_links(client)
        before = every_row(database_of(client))
        old_remove = pressed(client, log, decision="apart", revision=old_revision)
        after = every_row(database_of(client))
        page = said_on(result_page(client, log))

    assert [first.status_code, moved.status_code, old_remove.status_code] == [303, 303, 303]
    assert rows == [(NOTEBOOK_ID, "confirmed"), (LOG_ID, "rejected")]
    assert after == before
    assert page[0] == f"Linked to {NOTEBOOK}."


@pytest.mark.parametrize(
    "stale",
    [
        "another link in the class and term",
        "a save of the class and term",
        "a link left to the rules again",
    ],
)
def test_a_form_from_an_old_page_answers_409_and_writes_nothing(stale: str) -> None:
    """A form read before something else was recorded in the class and term, or a current form
    that leaves a link to the rules: 409 with the page as it reads now, the file as it was."""
    with browser() as client:
        store_of(client).put_on_record(
            [homework(), homework(DIAGRAM_ID, DIAGRAM, date(2026, 8, 27))], {}
        )
        class_id = with_report(client)
        log, diagram = (result_named(client, class_id, title) for title in (LOG, DIAGRAM))
        revision = revision_of(client, log)
        if stale == "another link in the class and term":
            parent_links(client, class_id, diagram, DIAGRAM_ID)
        elif stale == "a save of the class and term":
            with_report(client, NO_POINTS)
        else:
            parent_links(client, class_id, log)
            revision = revision_of(client, log)
        before = every_row(database_of(client))
        decision = "automatic" if stale == "a link left to the rules again" else "link"
        answer = pressed(client, log, decision=decision, revision=revision)
        after = every_row(database_of(client))

    assert answer.status_code == 409
    assert problem_on(answer.text) == LINKS_CHANGED
    assert '<h1 id="homework" tabindex="-1">' in answer.text
    assert after == before


def test_a_link_to_homework_whose_name_is_connected_differently_answers_409() -> None:
    """The notebook's name connected to the class, the page read, then the connection removed:
    the link answers 409, writes nothing, and the page it answers with offers no notebook."""
    name = "Life Science"
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(NOTEBOOK_ID, LOG, THURSDAY, course=name)], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        store.connect_homework_class(YEAR, name, shown=None, chosen=class_id, role="parent")
        offered = [one[1] for one in forms_on(result_page(client, log))]
        revision = revision_of(client, log)
        store.connect_homework_class(YEAR, name, shown=class_id, chosen=UNCONNECTED, role="parent")
        before = every_row(database_of(client))
        answer = pressed(client, log, NOTEBOOK_ID, revision=revision)
        after = every_row(database_of(client))

    assert NOTEBOOK_ID in offered
    assert answer.status_code == 409
    assert problem_on(answer.text) == (
        "That homework's class name is connected differently now. Nothing was saved. "
        "Check this page again."
    )
    assert NOTEBOOK_ID not in [one[1] for one in forms_on(answer.text)]
    assert after == before


@pytest.mark.parametrize("decision", ["link", "apart"])
def test_a_decision_on_homework_not_on_record_answers_409_and_writes_nothing(
    decision: str,
) -> None:
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        before = every_row(database_of(client))
        answer = pressed(client, log, "assignment-not-on-record", decision)
        after = every_row(database_of(client))

    assert answer.status_code == 409
    assert problem_on(answer.text) == "That homework is not on record now. Nothing was saved."
    assert after == before


@pytest.mark.parametrize(
    "cause", ["no such shape", "not on record", "no current value", "deleted with its class"]
)
def test_a_decision_on_a_result_with_no_current_record_answers_404_and_writes_nothing(
    cause: str,
) -> None:
    """A result with no current value is made by marking its report kept as earlier in the
    file by hand, as no save does; a delete is the store's own."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        revision = revision_of(client, log)
        if cause == "no current value":
            stored(store, "UPDATE grade_reports SET use = 'earlier'")
        elif cause == "deleted with its class":
            preview = store.delete_preview(class_id, "T1")
            assert isinstance(preview, DeletePreview)
            store.delete_class_term(class_id, "T1", revision=preview.revision, role="parent")
        asked = {"no such shape": "result-not-a-result", "not on record": "result-" + "0" * 32}
        before = every_row(database_of(client))
        answer = pressed(client, asked.get(cause, log), revision=revision)
        after = every_row(database_of(client))

    assert answer.status_code == 404
    assert problem_on(answer.text) == "Blossom has no current record of this result."
    assert after == before


@pytest.mark.parametrize(
    "sent",
    [
        pytest.param([("homework", LOG_ID), ("decision", "link")], id="no revision"),
        pytest.param(
            [("homework", LOG_ID), ("homework", LOG_ID), ("decision", "link"), ("revision", "1")],
            id="a field twice",
        ),
        pytest.param(
            [("homework", LOG_ID), ("decision", "choose"), ("revision", "1")],
            id="an unknown decision",
        ),
        pytest.param([("homework", ""), ("decision", "link"), ("revision", "1")], id="no homework"),
        pytest.param(
            [("homework", LOG_ID), ("decision", "link"), ("revision", "one")],
            id="a revision that is no number",
        ),
        pytest.param(
            [("homework", LOG_ID), ("decision", "link"), ("revision", "1"), ("class", "x")],
            id="a field of another form",
        ),
    ],
)
@pytest.mark.parametrize("index", ["present", "absent"])
def test_a_form_that_is_not_whole_answers_422_with_the_page_and_writes_nothing(
    sent: list[tuple[str, str]], index: str
) -> None:
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        if index == "absent":
            stored(store_of(client), DROP_LINKS_INDEX)
        before = every_row(database_of(client))
        answer = computer(client).post(
            RESULT_PAGE.format(result_id=log),
            content="&".join(f"{key}={value}" for key, value in sent),
            headers={**PAGE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
        after = every_row(database_of(client))

    assert answer.status_code == 422
    assert problem_on(answer.text) == "Choose homework. Nothing was saved."
    assert '<h1 id="homework" tabindex="-1">' in answer.text
    assert after == before


@pytest.mark.parametrize("decision", ["link", "apart", "automatic"])
def test_a_decision_meeting_a_stored_value_it_can_t_decode_answers_500_and_one_record(
    decision: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A points presence that is no presence in the result's class and term, its decoding error
    holding a chosen text: the generic answer, one record of the type alone, nothing written."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        revision = revision_of(client, log)
        points_presence(client, log, SYNTHETIC_CAUSE)
        before = every_row(database_of(client))
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            answer = pressed(client, log, decision=decision, revision=revision)
            records = list(caplog.records)
        after = every_row(database_of(client))

    assert (answer.status_code, answer.text) == (500, "Internal Server Error")
    assert answer.headers["content-type"] == "text/plain; charset=utf-8"
    application = [one for one in records if one.name.startswith("blossom")]
    assert [(one.name, one.levelno, one.getMessage()) for one in application] == [
        (
            "blossom.routes.grades",
            logging.WARNING,
            "a homework link wasn't written: GradeValueUndecodable",
        )
    ]
    assert [one for one in records if one.exc_info or SYNTHETIC_CAUSE in one.getMessage()] == []
    assert SYNTHETIC_CAUSE not in answer.text
    assert after == before


def test_a_decision_meeting_two_links_to_one_result_answers_500_and_one_record(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Two confirmed links to the log's result in a file changed by hand, with the index check
    made to pass so the write reads them: the generic answer, one record naming the conflict,
    and nothing written."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log, diagram = (result_named(client, class_id, title) for title in (LOG, DIAGRAM))
        conflicting(client, log)
        monkeypatch.setattr(store_of(client), "_homework_links_index_available", lambda: True)
        before = every_row(database_of(client))
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            answers = [pressed(client, one, decision="apart") for one in (log, diagram)]
            records = list(caplog.records)
        after = every_row(database_of(client))

    assert [(one.status_code, one.text) for one in answers] == [(500, "Internal Server Error")] * 2
    assert [one.getMessage() for one in records if one.name.startswith("blossom")] == [
        "a homework link wasn't written: HomeworkLinksConflict"
    ] * 2
    assert after == before


@pytest.mark.parametrize("wrong", ["missing", *WRONG_INDEXES])
def test_while_the_required_index_is_unavailable_every_decision_answers_500_and_one_record(
    wrong: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The index dropped, or made again by hand with another definition: every decision, one
    that would stand and one for a result not on record included, gets the generic answer and
    the one fixed record, and nothing is written."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        parent_links(client, class_id, log, decision="apart")
        revision = revision_of(client, log)
        stored(store, DROP_LINKS_INDEX)
        if wrong != "missing":
            stored(store, WRONG_INDEXES[wrong])
        before = every_row(database_of(client))
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            answers = [
                pressed(client, log, decision=decision, revision=revision)
                for decision in ("link", "apart", "automatic")
            ]
            answers.append(pressed(client, "result-" + "0" * 32, revision=revision))
            records = list(caplog.records)
        after = every_row(database_of(client))

    assert [(one.status_code, one.text) for one in answers] == [(500, "Internal Server Error")] * 4
    application = [one.getMessage() for one in records if one.name.startswith("blossom")]
    assert application == [INDEX_UNAVAILABLE] * 4
    assert not [one for one in records if "conflict" in one.getMessage().lower()]
    assert after == before


@pytest.mark.parametrize(
    ("refusal", "said"),
    [
        (GradeReportNotSaved, "Blossom couldn't save this. Nothing was saved. Try again."),
        (
            GradeTransactionLost,
            "Blossom couldn't tell whether this was saved. Pressing the button again is safe.",
        ),
    ],
)
def test_a_write_the_file_refuses_or_loses_answers_500_with_its_sentence(
    refusal: type[Exception],
    said: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The store's write made to raise in the test, as when the file refuses it or the
    transaction is lost: 500 with the page and its sentence, no record, nothing written. A page
    that can't be read again answers the generic 500 with one record instead."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        revision = revision_of(client, log)

        def refused(*_: object, **__: object) -> None:
            msg = "synthetic"
            raise refusal(msg)

        monkeypatch.setattr(store_of(client), "decide_homework_link", refused)
        before = every_row(database_of(client))
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            answer = pressed(client, log, revision=revision)
            answered = [
                one.getMessage() for one in caplog.records if one.name.startswith("blossom")
            ]
        after = every_row(database_of(client))
        points_presence(client, log, SYNTHETIC_CAUSE)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            stopped = pressed(client, log, revision=revision)
            stopped_records = list(caplog.records)

    assert answer.status_code == 500
    assert problem_on(answer.text) == said
    assert '<h1 id="homework" tabindex="-1">' in answer.text
    assert after == before
    assert answered == []
    assert (stopped.status_code, stopped.text) == (500, "Internal Server Error")
    assert [one.getMessage() for one in stopped_records if one.name.startswith("blossom")] == [
        "a homework link wasn't written: GradeValueUndecodable"
    ]
    assert [one for one in stopped_records if SYNTHETIC_CAUSE in one.getMessage()] == []


def test_two_parents_deciding_at_once_one_lands_and_the_other_reads_again() -> None:
    """Both pages read at one revision: the same decision twice stands; a different decision
    on the same pair, or on another result of the class and term, meets the changed revision.
    Results in two terms linked to one homework both apply."""
    with browser() as client:
        store_of(client).put_on_record(
            [homework(), homework(DIAGRAM_ID, DIAGRAM, date(2026, 8, 27))], {}
        )
        class_id = with_report(client)
        with_report(client, SECOND_TERM)
        log, diagram = (result_named(client, class_id, title) for title in (LOG, DIAGRAM))
        later = result_named(client, class_id, LOG, "T2")
        shown = revision_of(client, log)
        same = [pressed(client, log, revision=shown) for _ in range(2)]
        other = pressed(client, log, decision="apart", revision=shown)
        another_result = pressed(client, diagram, DIAGRAM_ID, revision=shown)
        both_terms = pressed(client, later, LOG_ID)
        rows = stored_links(client)

    assert [one.status_code for one in same] == [303, 303]
    assert other.status_code == 409
    assert another_result.status_code == 409
    assert both_terms.status_code == 303
    assert rows == [(LOG_ID, "confirmed"), (LOG_ID, "confirmed")]


def test_a_pair_kept_apart_returns_to_the_rules_only_when_left_to_them_again() -> None:
    """Kept apart by a press: her week shows no score for it, and none after the same report is
    saved again; left to the rules by a press, the score is back."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        ruled = on_card(client)
        pressed(client, log, decision="apart")
        apart = on_card(client)
        with_report(client)
        saved_again = on_card(client)
        pressed(client, log, decision="automatic")
        again = on_card(client)

    assert [line for line, _, _ in ruled] == [LINE]
    assert apart == saved_again == []
    assert again == ruled


def test_a_form_that_is_not_whole_on_a_file_with_two_links_to_one_result_is_stopped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two confirmed links to the log's result in a file changed by hand: a press whose form is
    not whole renders no page, gets the generic answer and one record, and writes nothing."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        conflicting(client, log)
        before = every_row(database_of(client))
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            answer = pressed(client, log, "")
            records = list(caplog.records)
        after = every_row(database_of(client))

    assert (answer.status_code, answer.text) == (500, "Internal Server Error")
    assert [one.getMessage() for one in records if one.name.startswith("blossom")] == [
        "a homework link wasn't written: HomeworkLinksConflict"
    ]
    assert after == before


def test_only_the_index_in_the_file_when_pressed_decides_and_a_shapeless_id_is_404() -> None:
    """The index dropped by the test's own connection after the page was read: the press is
    refused, and the page still opens; made again by the test's own statement, the same press
    goes through. A result ID of no such shape answers 404 while the index is missing."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        revision = revision_of(client, log)
        stored(store, DROP_LINKS_INDEX)
        refused = pressed(client, log, revision=revision)
        shapeless = pressed(client, "result-not-a-result", revision=revision)
        refused_rows = stored_links(client)
        opened = computer(client).get(RESULT_PAGE.format(result_id=log), headers=PAGE_HEADERS)
        stored(store, gradebook.CREATE_HOMEWORK_LINKS_INDEX)
        landed = pressed(client, log, revision=revision)
        rows = stored_links(client)

    assert (refused.status_code, refused.text) == (500, "Internal Server Error")
    assert shapeless.status_code == 404
    assert refused_rows == []
    assert opened.status_code == 200
    assert landed.status_code == 303
    assert rows == [(LOG_ID, "confirmed")]


def ties_hold(page: str) -> bool:
    """Whether every element a page names a region or a control by is on the page."""
    named = [
        labels or descriptions
        for labels, descriptions in re.findall(
            r'aria-labelledby="([^"]*)"|aria-describedby="([^"]*)"', page
        )
    ]
    return bool(named) and all(f'id="{one}"' in page for each in named for one in each.split())


def test_a_school_year_range_on_a_result_s_page_is_kept_together_where_it_fits() -> None:
    """Homework of Biology linked to last year's log, the current year made this one, then the
    homework put on record under another class name, each by the store's own write: the page
    names last year's class and its year, the range in one inline box a line keeps whole."""
    then = draft_of(IN_WEEK.replace(f"**{YEAR}**", "**2025-2026**"))
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(NOTEBOOK_ID, NOTEBOOK, None)], {})
        review = store.review_grade_report(then, capture_key(then), key=KEY)
        saved = save_grade(
            store, then, key=KEY, review=review, answers=grade_answers(review, month=9)
        )
        assert isinstance(saved, GradeReportSaved)
        last_year = capture_class(store, then)
        with_report(client)
        store.set_current_context(("2025-2026", "T1"), (YEAR, "T1"), role="parent")
        log = result_named(client, last_year, LOG)
        parent_links(client, last_year, log, NOTEBOOK_ID)
        store.put_on_record([homework(NOTEBOOK_ID, NOTEBOOK, None, course="Earth Science")], {})
        page = result_page(client, log)

    reason = (
        f"Earth Science isn't connected to {BIOLOGY}, a class of 2025-2026. Homework class names "
        "changes the current school year only."
    )
    assert reason in said_on(page)
    assert (
        f'<li><span class="authored-text">Earth Science isn&#39;t connected to {BIOLOGY}, a class '
        'of <span class="year-range">2025-2026</span>. Homework class names changes the current '
        "school year only.</span></li>"
    ) in page
    assert '<p>T1 · <span class="year-range">2025-2026</span></p>' in page
