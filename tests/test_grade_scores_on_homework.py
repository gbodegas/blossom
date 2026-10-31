# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The school score on her homework pages: which assignment shows a result's line, where, and
the way from it to class details.

Each case saves a report through the gradebook's own save and reads her pages as a browser
does. The line is the one class details writes for the same result, the link goes to that
result in its own term on the reader's tree, and showing it stores nothing and moves nothing.
"""

import contextlib
import dataclasses
import difflib
import functools
import html
import inspect
import json
import logging
import pathlib
import re
import sqlite3
import sys
import threading
import types
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from datetime import date, time
from time import monotonic
from typing import Any, Protocol

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from starlette.requests import Request
from starlette.responses import Response

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
from blossom.homework_search import PAGE_SIZE, QUERY_MAX_LENGTH, ResultsPage
from blossom.noticing import canonical_active_input, planning_digest, read_everything, week_from
from blossom.plans import PlanBlock
from blossom.reconciliation import SourceChannel
from blossom.routes import grades
from blossom.routes.grades import OtherShown, other_shown, result_page_read, result_said, row_shown
from blossom.routes.homework_scores import (
    FoundRow,
    FoundState,
    HomeworkFound,
    HomeworkRow,
    NoList,
    OtherResult,
    PairStanding,
    ResultHomework,
    RuleValues,
    ScoreShown,
    Withheld,
    YearRead,
    homework_found,
    homework_of,
    other_results,
    read_grade_facts,
    read_links,
    read_score_facts,
    result_homework,
    results_read,
    rule_values,
    scores_shown,
    title_of,
    waiting_in,
)
from blossom.routes.navigation import (
    NEW_NOTE_PAGE,
    NOTE_ACTIONS,
    assignment_anchor,
    note_add_action,
    note_add_href,
)
from blossom.routes.runs import plan_graphs
from blossom.routes.student import templates
from blossom.stores import gradebook
from blossom.stores.gradebook import (
    GRADEBOOK_TABLES,
    UNCONNECTED,
    ClassOfYear,
    DeletePreview,
    GradeReportNotSaved,
    GradeTransactionLost,
    HomeworkClasses,
    HomeworkLinkWritten,
    LinkDecision,
    NamedClass,
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
    Statements,
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
    main_of,
    page_of,
    plan_on,
    planned,
    record,
    refusing,
    refusing_coded,
    reported,
    save_grade,
    school_said,
    scripted_graphs,
    sign_in_as,
    state_of,
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


def way_in_to(result_id: str) -> str:
    """The address of a result's homework page, a parent's, landing on its heading."""
    return f"/parent/grades/results/{result_id}/homework#homework"


def ways_in_on(page: str) -> list[tuple[str, str, str]]:
    """Each link on a page to a result's homework page: its address, its words, and its
    accessible name when it has one of its own."""
    found = re.findall(
        r'<a class="assignment-link" href="(/parent/grades/results/[^"]*)"'
        r'(?: aria-label="([^"]*)")?>([^<]*)</a>',
        page,
    )
    return [(html.unescape(href), words, html.unescape(name)) for href, name, words in found]


def due_later(page: str) -> str:
    """The section of rows assigned in the week and due later, whole."""
    start = page.index('<section class="panel assigned">')
    return page[start : page.index("</section>", start)]


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
    later, done_later = (due_later(page) for page in (week, done_week))
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

    bare_later, later, done_later = (due_later(page) for page in (bare, week, done_week))
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


@pytest.mark.parametrize("reader", ["her", "parent", "open"])
def test_a_signed_in_parent_finds_beside_each_result_on_assignment_details_its_page(
    reader: str, tmp_path: pathlib.Path
) -> None:
    """The log matched by the rules, a notebook linked to the diagram's result and to the
    osmosis lab's, and a sketch linked to the practice's alone. On assignment details a
    signed-in parent finds, after each result's Grade details, "Confirm or reject this match"
    for the match and "Change or remove link" for each link, one or several, each to that
    result's page; she, and anyone with the sign-in off, finds none."""
    sketch = "assignment-sketch"
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        store_of(client).put_on_record(
            [homework(), homework(NOTEBOOK_ID, NOTEBOOK, None), homework(sketch, "Field sketch")],
            {},
        )
        class_id = with_report(client)
        log, diagram, osmosis, practice = (
            result_named(client, class_id, title) for title in (LOG, DIAGRAM, OSMOSIS, PRACTICE)
        )
        parent_links(client, class_id, diagram, NOTEBOOK_ID)
        parent_links(client, class_id, osmosis, NOTEBOOK_ID)
        parent_links(client, class_id, practice, sketch)
        matched = details_of(client)
        linked = details_of(client, NOTEBOOK_ID)
        linked_once = details_of(client, sketch)

    assert [len(scores(matched)), len(EACH_RESULT.findall(linked))] == [1, 2]
    assert len(scores(linked_once)) == 1
    if reader != "parent":
        assert ways_in_on(matched) == ways_in_on(linked) == ways_in_on(linked_once) == []
        return
    match_way, link_way = "Confirm or reject this match", "Change or remove link"
    assert ways_in_on(matched) == [(way_in_to(log), match_way, f"{match_way}: {LOG}, {BIOLOGY}")]
    assert ways_in_on(linked) == [
        (way_in_to(diagram), link_way, f"{link_way}: {DIAGRAM}, {BIOLOGY}"),
        (way_in_to(osmosis), link_way, f"{link_way}: {OSMOSIS}, {BIOLOGY}"),
    ]
    beside = (
        'Grade details</a></p>\n      <p class="details-link"><a class="assignment-link" '
        f'href="{way_in_to(log)}" aria-label="{match_way}: {LOG}, {BIOLOGY}">{match_way}</a></p>'
        "\n    </section>"
    )
    assert matched.count(beside) == 1
    assert linked.count('Grade details</a></p>\n      <p class="details-link">') == 2
    assert ways_in_on(linked_once) == [
        (way_in_to(practice), link_way, f"{link_way}: Field sketch, {BIOLOGY}")
    ]
    assert (
        linked_once.count(
            f'aria-label="{link_way}: Field sketch, {BIOLOGY}">{link_way}</a></p>\n    </section>'
        )
        == 1
    )


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
CHECK_UNKNOWN = '\n    <p class="problem">Check together can\'t be worked out right now.</p>\n  '
"""Her week's one line that Check together's count can't be worked out, as the page writes it."""


def without_check_unknown(page: str, address: str) -> str:
    """A page read after a covered failure, with her week's one Check together line taken out:
    her week holds it once, since her Done homework is in its reach, and details never do."""
    assert page.count(CHECK_UNKNOWN) == (1 if address == HER_PAGE else 0), address
    return page.replace(CHECK_UNKNOWN, "")


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
    with none answer, show no score and read as with no class on record, but for her week's one
    line that Check together can't be worked out. Each read records the failure's type alone,
    the file stays as it was, and the lines return once the cell decodes."""
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
    for address, (_, page), with_no_class in zip(addresses, said, bare, strict=True):
        assert "School score" not in page
        assert "Grade details" not in page
        assert HEADING not in page
        assert not [said for said in ("no grades", "not grade") if said in page.lower()]
        assert without_scores(without_check_unknown(page, address)) == without_scores(with_no_class)
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
    on record, but for her week's one line that Check together can't be worked out. Each read
    records the failure's type alone, and the file stays as it was."""
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
    for address, (_, page), with_no_class in zip(addresses, said, bare, strict=True):
        assert "School score" not in page
        assert "Grade details" not in page
        assert HEADING not in page
        assert not [said for said in ("no grades", "not grade") if said in page.lower()]
        assert without_scores(without_check_unknown(page, address)) == without_scores(with_no_class)
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
    time, show no score and read as with no class on record, but for her week's one line that
    Check together can't be worked out. Each read records the failure's type alone; the
    gradebook's rows and the connection's decoding stay as they were."""
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
    for address, (_, page), with_no_class in zip(addresses, said, bare, strict=True):
        assert "School score" not in page
        assert "Grade details" not in page
        assert HEADING not in page
        assert not [said for said in ("no grades", "not grade") if said in page.lower()]
        assert without_scores(without_check_unknown(page, address)) == without_scores(with_no_class)
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
    database's own error."""
    edit = EDITS["a result's title that is not UTF-8"]
    with browser() as client:
        store = store_of(client)
        class_id = with_report(client)
        sound = read_grade_facts(store, [BIOLOGY])
        stored(store, edit.statement, *edit.binds)
        with pytest.raises(GradeValueUndecodable) as refused:
            read_grade_facts(store, [BIOLOGY])
        decoding = store._connection.text_factory
        with pytest.raises(sqlite3.OperationalError) as plain:
            store.class_term(class_id, "T1")

    assert [list(year.reads) for year in sound] == [[(class_id, "T1")]]
    assert type(refused.value.__cause__) is UnicodeDecodeError
    assert decoding is text_or_refusal
    assert type(plain.value) is sqlite3.OperationalError


def cannot_be_shown(
    answer: "Answered", heading: str, sentence: str, again: str, ways_back: list[tuple[str, str]]
) -> None:
    """The page that reads no store, answering a grade page whose record can't be read: 503,
    headed as the page, its one sentence with "Try again" at ``again``, the ways back, Grades
    marked in its tree's menu, and no form."""
    assert answer.status_code == 503, (answer.status_code, answer.text[:600])
    main = main_of(answer.text)
    (shown,) = re.findall(r"<h1>(.*?)</h1>", main, re.S)
    assert (
        " ".join(html.unescape(re.sub(r"<[^>]+>", " ", shown)).split()).replace("\xad", "")
        == heading
    )
    assert (
        '<p class="problem" role="alert" id="problem-summary" tabindex="-1" autofocus>'
        f'{escape(sentence)} <a href="{again}">Try again</a></p>'
    ) in main
    ways = re.findall(r'<p class="return"><a href="([^"]*)">([^<]*)</a></p>', main)
    assert ways == ways_back
    tree = "/parent" if again.startswith("/parent/") else "/student"
    assert f'<a href="{tree}/grades" aria-current="page">Grades</a>' in answer.text
    assert "<form" not in main
    assert "Internal Server Error" not in answer.text


def logged_by_kind_alone(records: list[logging.LogRecord], said: str) -> None:
    """The application's one record of a page that can't be shown, a warning that names the
    error's kind alone: no record holds the error's words, an authored title or a traceback,
    and none says a grade page wasn't shown."""
    assert [
        (one.name, one.levelno, one.getMessage())
        for one in records
        if one.name.startswith("blossom")
    ] == [("blossom.routes.student", logging.WARNING, said)]
    for one in records:
        message = one.getMessage()
        assert not one.exc_info, message
        for unsaid in ("synthetic", LOG, BIOLOGY, "wasn't shown"):
            assert unsaid not in message, message


def test_grades_and_class_details_over_text_that_isn_t_utf_8_answer_that_they_can_t_be_shown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With a result's title stored as bytes that are not UTF-8, Grades and class details meet
    the database's own error and answer the page that can't be shown, which names the error's
    kind alone. Asking again only repeats the read, and the connection keeps its own decoding."""
    edit = EDITS["a result's title that is not UTF-8"]
    with browser() as client:
        store = store_of(client)
        class_id = with_report(client)
        stored(store, edit.statement, *edit.binds)
        details = f"/student/grades/classes/{class_id}/terms/1"
        with caplog.at_level(logging.DEBUG):
            answers = [
                client.get(address, headers=PAGE_HEADERS)
                for address in ("/student/grades", details)
            ]
            records = list(caplog.records)
        again = client.get(details, headers=PAGE_HEADERS)
        decoding = store._connection.text_factory

    cannot_be_shown(answers[0], "Grades", "Grades can't be shown right now.", "/student/grades", [])
    for answer in (answers[1], again):
        cannot_be_shown(
            answer,
            "Class details",
            "Class details can't be shown right now.",
            details,
            [("/student/grades", "Back to Grades")],
        )
    assert [
        (one.name, one.levelno, one.getMessage())
        for one in records
        if one.name.startswith("blossom")
    ] == [
        ("blossom.routes.student", logging.WARNING, "Grades could not be read: OperationalError"),
        (
            "blossom.routes.student",
            logging.WARNING,
            "Class details could not be read: OperationalError",
        ),
    ]
    assert not [one for one in records if one.exc_info or "UTF-8" in one.getMessage()]
    assert not [one for one in records if BIOLOGY in one.getMessage()]
    assert not [one for one in records if "wasn't shown" in one.getMessage()]
    assert decoding is text_or_refusal


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
TOGETHER_LINE = r'\n    <p class="confidence disagree to-check" role="status">.*?</p>\n  '
"""Her week's line of homework to check together, as the page writes it."""
EXPLANATION = r'\n<p class="effect">Check together: .*?</p>'
"""The explanation of one counted result on a card or row, as the page writes it."""


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
    out, her week and each details page equal those with no class on record, but for the line
    that lists her Done homework linked to a result the school reports Missing and the
    explanation of each such result on its row; reads write nothing."""
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

    later, done_later = (due_later(page) for page in (week, done_week))
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
    together = re.findall(TOGETHER_LINE, done_linked, re.S)
    assert [" ".join(re.sub(r"<[^>]+>", "", line).split()) for line in together] == [
        f"1 assignment to check together: {DIAGRAM}."
    ]
    explained = re.findall(EXPLANATION, done_linked, re.S)
    assert [" ".join(re.sub(r"<[^>]+>", "", line).split()) for line in explained] == [
        f"Check together: the school's gradebook lists {DIAGRAM} as Missing."
    ] * 2
    plain = re.sub(EXPLANATION, "", re.sub(TOGETHER_LINE, "", done_linked, flags=re.S), flags=re.S)
    assert without_results(plain) == done_bare
    assert after_reading == on_record


@pytest.mark.parametrize(
    ("reader", "tree"), [("her", "student"), ("parent", "parent"), ("open", "student")]
)
def test_the_details_list_each_result_with_its_title_its_line_and_its_own_link(
    reader: str, tree: str, tmp_path: pathlib.Path
) -> None:
    """Three results linked to one assignment, two in one term and one in the next: each has its
    title, class details' line and a link to its own term on the reader's tree. No word says who
    chose them, but for a signed-in parent's way to each result's page; the section holds no
    form or button, and the newer of one term's two is first."""
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
    link_way = "Change or remove link"
    ways = [
        (way_in_to(one), link_way, f"{link_way}: {title}, {BIOLOGY}")
        for one, title in ((diagram, DIAGRAM), (first, LOG), (second, LOG))
    ]
    assert ways_in_on(section) == (ways if reader == "parent" else [])
    for address, words, name in ways:
        section = section.replace(
            f'<p class="details-link"><a class="assignment-link" href="{address}" '
            f'aria-label="{name}">{words}</a></p>',
            "",
        )
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
    log_page, diagram_page = (way_in_to(one) for one in (log, diagram))
    assert with_links == {LOG_ID: (ScoreShown(LINE, link_to(class_id, log), LOG, True, log_page),)}
    assert by_the_rules == {
        LOG_ID: (ScoreShown(LINE, link_to(class_id, log), LOG, False, log_page),),
        DIAGRAM_ID: (
            ScoreShown(DIAGRAM_LINE, link_to(class_id, diagram), DIAGRAM, False, diagram_page),
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
    assert [len(ways_in_on(page)) for page in whole[1:]] == (
        [1, 1] if reader == "parent" else [0, 0]
    )
    assert said == bare * 2
    for page in said:
        assert not [
            word
            for word in ("School score", "School results", "Grade details", "See the results")
            if word in page
        ]
        assert ways_in_on(page) == []
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
    """Plain values: one homework and one result the rules stand together, with this school
    year current or another. The view names the points and showing reasons exactly when the
    rules show no score, never the class connection; the match, candidates and waiting stay."""
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


SET = "Practice set"
"""A homework title no result has, shared by the rows a search pages through."""
TWENTY_ONE = [homework(f"assignment-set-{n:02}", SET, None) for n in range(21, 0, -1)]
"""Twenty-one undated assignments of one class name and one title, which no word tells apart,
in the reverse of the search's order."""
CODE = "07 BIO - C"
"""The Biology class's report code, a second name homework connects to it by."""


def searched(
    words: str,
    read: RuleValues,
    links: Collection[Link] = (),
    *,
    page: str = "",
    result_id: str = "result-log",
) -> HomeworkFound | NoList:
    """A search of the one class's result ``result_id`` for ``words`` with the page field
    ``page``, from plain values."""
    return homework_found(words, page, THE_CLASS.class_id, result_id, read, links=links)


def found_ids(answer: HomeworkFound | NoList) -> list[str]:
    """The assignment ID of each row a search lists, in its order."""
    assert isinstance(answer, HomeworkFound), answer
    return [row.item.assignment_id for row in answer.rows]


def test_a_search_lists_only_the_class_s_homework_each_word_finds_in_its_name_or_title() -> None:
    """Plain values: homework of the class under its name and under its report code, a task
    among it, and homework of the log's title under a name of another class or of none. Every
    word must be in the class name or the title, and rows come in the search's one order."""
    log, notebook = homework(), homework(NOTEBOOK_ID, NOTEBOOK, None)
    task = homework("assignment-task", "Germination reading", None, kind=AssignmentKind.TASK)
    coded = homework("assignment-coded", "Germination photos", course=CODE)
    other = homework("assignment-other", LOG, course="08 GEOMETRY")
    waits = homework("assignment-waits", LOG, course="Life Science")
    plain = as_read(
        [other, log, waits, notebook, task, coded],
        [school_result("result-log")],
        unconnected=["assignment-waits"],
    )
    read = plain._replace(classes={**plain.classes, "assignment-other": ["class-geometry"]})

    answer = searched("germination", read)

    assert answer == HomeworkFound(
        ResultsPage(1, 1, 3, (coded, task, log)),
        (
            FoundRow(coded, FoundState.NONE),
            FoundRow(task, FoundState.NONE),
            FoundRow(log, FoundState.NONE),
        ),
    )
    assert found_ids(searched("seed", read)) == [LOG_ID]
    assert found_ids(searched("  GERMINATION   Log ", read)) == ["assignment-task", LOG_ID]
    assert found_ids(searched("notebook", read)) == [NOTEBOOK_ID]
    assert found_ids(searched("07", read)) == ["assignment-coded"]
    assert found_ids(searched("biology", read)) == ["assignment-task", NOTEBOOK_ID, LOG_ID]
    for words in ("geometry", "life science", "germination notebook"):
        assert searched(words, read) == HomeworkFound(ResultsPage(1, 1, 0, ()), ())


def test_each_found_row_s_state_is_the_first_of_linked_here_kept_apart_or_linked_elsewhere() -> (
    None
):
    """Plain values: homework linked to the result and to another, kept apart from it, linked
    to another result, kept apart from it and linked to another, kept apart from another result
    only, and with no decision. A row's state is the first that holds, as the page's rows say."""
    names = ["here", "apart", "elsewhere", "apart-and-elsewhere", "apart-from-other", "none"]
    items = [homework(f"assignment-{name}", SET, None) for name in names]
    links = [
        confirmed("assignment-here", "result-log"),
        confirmed("assignment-here", "result-2"),
        kept_apart("assignment-apart", "result-log"),
        confirmed("assignment-elsewhere", "result-3"),
        kept_apart("assignment-apart-and-elsewhere", "result-log"),
        confirmed("assignment-apart-and-elsewhere", "result-4"),
        kept_apart("assignment-apart-from-other", "result-2"),
    ]
    read = as_read(items, [school_result("result-log", SET)])

    answer = searched("practice", read, links)
    view = viewed("result-log", read, links)

    assert isinstance(answer, HomeworkFound)
    states = {row.item.assignment_id: row.state for row in answer.rows}
    assert states == {
        "assignment-here": FoundState.LINKED,
        "assignment-apart": FoundState.APART,
        "assignment-elsewhere": FoundState.ANOTHER_RESULT,
        "assignment-apart-and-elsewhere": FoundState.APART,
        "assignment-apart-from-other": FoundState.NONE,
        "assignment-none": FoundState.NONE,
    }
    first = {LINKED: FoundState.LINKED, APART: FoundState.APART}
    assert len(view.candidates) == len(items)
    for one in view.candidates:
        alone = FoundState.ANOTHER_RESULT if one.another_result else FoundState.NONE
        assert states[one.assignment_id] == first.get(one.standing, alone)


def test_a_search_comes_twenty_to_a_page_and_every_row_one_title_shares_can_be_reached() -> None:
    """Plain values: twenty-one assignments that no word tells apart. The page field empty or
    "1" gives the first twenty in the search's order with a next page; "2" gives the twenty-
    first with a previous page; together they hold every one."""
    read = as_read(TWENTY_ONE, [])
    order = sorted(TWENTY_ONE, key=lambda item: item.assignment_id)

    first, again, second = (searched("practice set", read, page=page) for page in ("", "1", "2"))

    assert isinstance(first, HomeworkFound)
    assert isinstance(second, HomeworkFound)
    assert again == first
    assert first.page == ResultsPage(1, 2, 21, tuple(order[:PAGE_SIZE]))
    assert (first.page.previous, first.page.next) == (None, 2)
    assert second.page == ResultsPage(2, 2, 21, tuple(order[PAGE_SIZE:]))
    assert (second.page.previous, second.page.next) == (1, None)
    assert [row.item for row in (*first.rows, *second.rows)] == order
    assert {row.state for row in (*first.rows, *second.rows)} == {FoundState.NONE}


@pytest.mark.parametrize(
    ("words", "page", "nothing"),
    [
        pytest.param("", "", NoList.NO_WORDS, id="no words"),
        pytest.param("   ", "", NoList.NO_WORDS, id="only spaces"),
        pytest.param("", "two", NoList.NO_WORDS, id="no words and a page in words"),
        pytest.param("  ", "3", NoList.NO_WORDS, id="only spaces and a page past the last"),
        pytest.param("practice\nset", "", NoList.WORDS_REFUSED, id="two lines"),
        pytest.param("s" * 201, "", NoList.WORDS_REFUSED, id="201 characters"),
        pytest.param(" s" * 101, "", NoList.WORDS_REFUSED, id="201 once spaces collapse"),
        pytest.param("practice\nset", "two", NoList.WORDS_REFUSED, id="two lines, a page in words"),
        pytest.param(SET, "0", NoList.NO_SUCH_PAGE, id="page 0"),
        pytest.param(SET, "3", NoList.NO_SUCH_PAGE, id="a page past the last"),
        pytest.param(SET, "02", NoList.NO_SUCH_PAGE, id="a leading zero"),
        pytest.param(SET, "two", NoList.NO_SUCH_PAGE, id="a page in words"),
        pytest.param(SET, "1234567", NoList.NO_SUCH_PAGE, id="seven digits"),
        pytest.param(SET, "+2", NoList.NO_SUCH_PAGE, id="a sign"),
        pytest.param(SET, " 2", NoList.NO_SUCH_PAGE, id="a space"),
        pytest.param(SET, "\uff12", NoList.NO_SUCH_PAGE, id="a digit that is not ASCII"),
        pytest.param("geometry", "2", NoList.NO_SUCH_PAGE, id="a second page of nothing found"),
    ],
)
def test_a_search_lists_nothing_for_no_words_refused_words_or_a_page_its_rows_lack(
    words: str, page: str, nothing: NoList
) -> None:
    """Plain values: twenty-one rows of one title. With no words any page field is a search with
    no words; refused words are refused whatever the page field; then a field that is no page
    number, or a number no page of the rows found has, names no page."""
    assert searched(words, as_read(TWENTY_ONE, []), page=page) is nothing


def test_a_search_past_the_last_page_gives_the_first_page_again_with_the_page_field_empty() -> None:
    """Plain values: "3" names no page of twenty-one rows; the same words with the page field
    empty, as the search's own button sends it, give the first page of twenty."""
    read = as_read(TWENTY_ONE, [])

    past = searched(SET, read, page="3")
    again = searched(SET, read, page="")

    assert past is NoList.NO_SUCH_PAGE
    assert isinstance(again, HomeworkFound)
    assert (again.page.number, again.page.total, len(again.rows)) == (1, 21, PAGE_SIZE)


def test_words_over_the_limit_as_typed_and_at_it_once_their_spaces_collapse_are_searched() -> None:
    """Plain values: words of 248 characters as typed and 200 once their spaces collapse find
    the rows that hold every one of them."""
    words = "  ".join([*["set"] * 48, "practice"])

    answer = searched(words, as_read(TWENTY_ONE, []))

    assert (len(words), len(" ".join(words.split()))) == (248, QUERY_MAX_LENGTH)
    assert isinstance(answer, HomeworkFound)
    assert answer.page.total == 21


@pytest.mark.parametrize(
    "words", ["practice", "", "two\nlines"], ids=["words", "no words", "refused words"]
)
def test_a_search_is_refused_whole_on_two_confirmed_links_naming_one_result(words: str) -> None:
    """Plain values. Two confirmed links that name one result raise the conflict error with no
    cause and no context, whatever the words, and whether or not that result is the one
    searched for."""
    read = as_read(TWENTY_ONE, [])
    links = [confirmed("assignment-set-01", "result-2"), confirmed("assignment-set-02", "result-2")]

    for result_id in ("result-log", "result-2"):
        with pytest.raises(GradeValueUndecodable) as refused:
            searched(words, read, links, result_id=result_id)

        assert type(refused.value) is HomeworkLinksConflict
        assert refused.value.__cause__ is None
        assert refused.value.__context__ is None


def test_a_search_s_answer_holds_closed_fields_and_closed_sets_of_values() -> None:
    assert [field.name for field in dataclasses.fields(HomeworkFound)] == ["page", "rows"]
    assert [field.name for field in dataclasses.fields(FoundRow)] == ["item", "state"]
    assert list(FoundState) == ["linked", "apart", "another_result", "none"]
    assert list(NoList) == ["no_words", "words_refused", "no_such_page"]


def found_now(
    client: TestClient, result_id: str, words: str, page: str = ""
) -> HomeworkFound | NoList:
    """What a search finds from a result's page read, as the store stands."""
    shown = result_page_read(state_of(client), result_id, search=(words, page))
    assert shown is not None
    answer = shown["found"]
    assert isinstance(answer, HomeworkFound | NoList), answer
    return answer


def test_a_search_makes_the_result_s_page_read_and_asks_no_statement_and_writes_nothing() -> None:
    """The log's page read with no search, then with a search that finds homework, with no
    words, with refused words, and with a page past the last. Each asks the page's statements
    in the page's order and no other, gives the page the same values, and writes nothing."""
    searches = [("germination", ""), ("", ""), ("two\nlines", ""), ("germination", "2")]
    with browser() as client:
        store_of(client).put_on_record([homework(), homework(NOTEBOOK_ID, NOTEBOOK, None)], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        state = state_of(client)
        before = closed_world([database_of(client)], leaving_out=())
        with Statements(state) as seen:
            plain = result_page_read(state, log)
        asked = list(seen)
        answers: list[tuple[dict[str, object] | None, list[str]]] = []
        for search in searches:
            with Statements(state) as seen:
                answers.append((result_page_read(state, log, search=search), list(seen)))
        after = closed_world([database_of(client)], leaving_out=())

    assert plain is not None
    assert plain["found"] is None
    assert asked
    found = []
    for shown, statements in answers:
        assert statements == asked
        assert shown is not None
        assert {**shown, "found": None, "search": None} == plain
        found.append(shown["found"])
    assert found[1:] == [NoList.NO_WORDS, NoList.WORDS_REFUSED, NoList.NO_SUCH_PAGE]
    assert isinstance(found[0], HomeworkFound)
    assert found_ids(found[0]) == [LOG_ID]
    assert after == before


def test_a_search_through_the_store_finds_homework_whose_name_the_year_makes_the_class() -> None:
    """Homework under the class's name and its report code, a task among it, and homework under
    another class's name and under a name that waits, all with one word in the title. A search
    on the log's page finds the homework whose name the year's answers make the log's class."""
    on_record = [
        homework(),
        homework("assignment-task", "Germination reading", None, kind=AssignmentKind.TASK),
        homework("assignment-coded", "Germination photos", course=CODE),
        homework("assignment-other", "Germination angles", course="08 GEOMETRY"),
        homework("assignment-waits", "Germination survey", course="Life Science"),
    ]
    with browser() as client:
        store = store_of(client)
        store.put_on_record(on_record, {})
        class_id = with_report(client)
        geometry = with_report(client, GEOMETRY)
        log = result_named(client, class_id, LOG)
        every_word = found_now(client, log, "germination")
        by_code = found_now(client, log, "07")
        by_name = found_now(client, log, "biology")
        mapping = store.homework_classes(YEAR)

    classes = {item.assignment_id: class_for(item.course, mapping) for item in on_record}
    assert classes == {
        LOG_ID: class_id,
        "assignment-task": class_id,
        "assignment-coded": class_id,
        "assignment-other": geometry,
        "assignment-waits": None,
    }
    assert found_ids(every_word) == ["assignment-coded", "assignment-task", LOG_ID]
    assert found_ids(by_code) == ["assignment-coded"]
    assert found_ids(by_name) == ["assignment-task", LOG_ID]


def test_a_search_through_the_store_reads_a_class_name_by_the_answers_of_the_class_s_year() -> None:
    """Last school year's report and this year's, in two classes of one name, and this year's
    answer that Biology is no class. Homework under Biology is found on last year's log page
    alone; homework under the report code, on both."""
    then = draft_of(IN_WEEK.replace(f"**{YEAR}**", "**2025-2026**"))
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [homework(), homework("assignment-coded", "Germination photos", course=CODE)], {}
        )
        review = store.review_grade_report(then, capture_key(then), key=KEY)
        saved = save_grade(
            store, then, key=KEY, review=review, answers=grade_answers(review, month=9)
        )
        assert isinstance(saved, GradeReportSaved)
        last_year = capture_class(store, then)
        this_year = with_report(client)
        store.set_current_context(("2025-2026", "T1"), (YEAR, "T1"), role="parent")
        store.connect_homework_class(YEAR, BIOLOGY, shown=None, chosen=UNCONNECTED, role="parent")
        now = found_now(client, result_named(client, this_year, LOG), "germination")
        earlier = found_now(client, result_named(client, last_year, LOG), "germination")
        connected = [
            class_for(BIOLOGY, store.homework_classes(year)) for year in (YEAR, "2025-2026")
        ]

    assert connected == [None, last_year]
    assert found_ids(now) == ["assignment-coded"]
    assert found_ids(earlier) == ["assignment-coded", LOG_ID]


def states_now(client: TestClient, result_id: str) -> dict[str, FoundState]:
    """Each Biology homework a search finds from a result's page read, with its state."""
    answer = found_now(client, result_id, "biology")
    assert isinstance(answer, HomeworkFound)
    return {row.item.assignment_id: row.state for row in answer.rows}


def test_each_found_row_s_state_follows_the_decisions_made_through_the_store() -> None:
    """The log's and the diagram's results, and four homework rows of the class. By the store's
    own write: a notebook linked to the log, the log's homework kept apart from it, practice
    linked to the diagram; then practice kept apart from the log; then the notebook linked to
    the diagram too. Each found row's state is the first that holds."""
    spare = "assignment-spare"
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [
                homework(),
                homework(NOTEBOOK_ID, NOTEBOOK, None),
                homework(PRACTICE_ID, PRACTICE, date(2026, 8, 12)),
                homework(spare, "Field sketch", None),
            ],
            {},
        )
        class_id = with_report(client)
        log, diagram = (result_named(client, class_id, title) for title in (LOG, DIAGRAM))
        untouched = states_now(client, log)
        parent_links(client, class_id, log, NOTEBOOK_ID)
        parent_links(client, class_id, log, decision="apart")
        parent_links(client, class_id, diagram, PRACTICE_ID)
        first = states_now(client, log)
        parent_links(client, class_id, log, PRACTICE_ID, decision="apart")
        second = states_now(client, log)
        links_second = read_links(store)
        parent_links(client, class_id, diagram, NOTEBOOK_ID)
        third = states_now(client, log)
        links_third = read_links(store)

    none, linked, apart = FoundState.NONE, FoundState.LINKED, FoundState.APART
    assert list(untouched) == [spare, NOTEBOOK_ID, PRACTICE_ID, LOG_ID]
    assert untouched == dict.fromkeys(untouched, none)
    assert first == {
        spare: none,
        NOTEBOOK_ID: linked,
        PRACTICE_ID: FoundState.ANOTHER_RESULT,
        LOG_ID: apart,
    }
    assert second == {**first, PRACTICE_ID: apart}
    assert third == second
    assert confirmed(PRACTICE_ID, diagram) in links_second
    assert confirmed(NOTEBOOK_ID, diagram) in links_third
    assert confirmed(NOTEBOOK_ID, log) in links_third


THEN_CLASS = ClassOfYear("class-biology-then", BIOLOGY, "2025-2026")
"""Last school year's class of the same name, another class."""


def year_of(year: str, *classes: ClassOfYear) -> YearRead:
    """A school year as plain values: its classes by ID and name, and nothing read."""
    named = tuple(NamedClass(one.class_id, one.name, ()) for one in classes)
    return YearRead(HomeworkClasses(year, named, {}), None, (), {})


def a_result(
    result_id: str,
    day: date | None,
    *,
    title: str = LOG,
    term: str = "T1",
    class_id: str = THE_CLASS.class_id,
) -> Result:
    """A result as the rules read it, its day resolved when it has one."""
    return Result(class_id, term, result_id, title, day, day is not None, True, True)


def with_values(read: RuleValues) -> RuleValues:
    """The values with each result's current value, as a page's read holds them."""
    found = {(one.class_id, one.term, one.result_id): (value_with(), 1) for one in read.results}
    return read._replace(found=found)


def others_of(read: RuleValues, result_id: str = "result-log", term: str = "T1") -> list[str]:
    """The IDs of the other results listed beside one of the one class's results."""
    years = (year_of("2025-2026", THEN_CLASS), year_of(YEAR, THE_CLASS))
    return [
        one.result_id for one in other_results(THE_CLASS.class_id, term, result_id, read, years)
    ]


def test_other_results_share_a_candidate_with_the_result_each_once_and_go_no_further() -> None:
    """Plain values: the log's result and six more. One shares both its candidates, two agree
    with each other in title, class, term and day and share both, one in another class shares
    one, one has another title, and one shares a candidate only with a result that shares one
    with the log's. The first four are listed, each once, in the order the results came; the
    log's own result and the others are not."""
    dated, twin = homework(), homework("assignment-twin")
    later = homework("assignment-later", on=date(2026, 8, 27))
    results = [
        a_result("result-none", THURSDAY, title=DIAGRAM),
        a_result("result-twice", None),
        a_result("result-log", THURSDAY),
        a_result("result-alike-1", THURSDAY),
        a_result("result-alike-2", THURSDAY),
        a_result("result-further", date(2026, 8, 27)),
        a_result("result-once", THURSDAY, class_id=THEN_CLASS.class_id),
    ]
    plain = as_read([dated, twin, later], results)
    classes = {**plain.classes, LOG_ID: [THE_CLASS.class_id, THEN_CLASS.class_id]}
    read = with_values(plain._replace(classes=classes))

    assert others_of(read) == ["result-twice", "result-alike-1", "result-alike-2", "result-once"]
    assert others_of(read, "result-alike-1") == [
        "result-twice",
        "result-log",
        "result-alike-2",
        "result-once",
    ]
    assert others_of(read, "result-further") == ["result-twice"]
    assert others_of(read, "result-none") == []


def test_other_results_come_from_every_term_and_school_year_read_in_the_results_order() -> None:
    """Plain values: the log's result, its twin in the next term, and last year's in a class of
    the same name, which the log's homework name connects to as well. Each other result is
    listed with its own class's name and year and its own term, in the order the results came."""
    results = [
        a_result("result-then", THURSDAY, class_id=THEN_CLASS.class_id),
        a_result("result-log", THURSDAY),
        a_result("result-next", THURSDAY, term="T2"),
    ]
    plain = as_read([homework()], results)
    read = with_values(plain._replace(classes={LOG_ID: [THE_CLASS.class_id, THEN_CLASS.class_id]}))
    years = (year_of("2025-2026", THEN_CLASS), year_of(YEAR, THE_CLASS))

    listed = other_results(THE_CLASS.class_id, "T1", "result-log", read, years)
    from_next = other_results(THE_CLASS.class_id, " T2 ", "result-next", read, years)

    assert [(one.result_id, one.class_name, one.year, one.term) for one in listed] == [
        ("result-then", BIOLOGY, "2025-2026", "T1"),
        ("result-next", BIOLOGY, YEAR, "T2"),
    ]
    assert listed[0] == OtherResult(
        class_id=THEN_CLASS.class_id,
        class_name=BIOLOGY,
        year="2025-2026",
        term="T1",
        result_id="result-then",
        value=value_with(),
        day=THURSDAY,
    )
    assert [one.result_id for one in from_next] == ["result-then", "result-log"]


def test_other_results_are_read_before_any_decision_and_never_through_a_link_alone() -> None:
    """Plain values: the log's result and its twin in the next term share the log's homework;
    a notebook of another title is linked to the log's result. The list takes no decisions, so
    the notebook adds nothing, and nothing a parent decides can change it."""
    results = [a_result("result-log", THURSDAY), a_result("result-next", THURSDAY, term="T2")]
    read = with_values(as_read([homework(), homework(NOTEBOOK_ID, NOTEBOOK, None)], results))

    assert others_of(read) == ["result-next"]
    assert "links" not in inspect.signature(other_results).parameters


def test_identical_looking_twins_are_listed_apart_with_nothing_made_up_to_tell_them_apart() -> None:
    """Plain values: two other results whose title, class, due line, term and year all agree.
    Each is its own item, with the address of its own page and nothing else that differs."""
    twins = [
        OtherResult(THE_CLASS.class_id, BIOLOGY, YEAR, "T1", one, value_with(), THURSDAY)
        for one in ("result-first", "result-second")
    ]

    shown = [other_shown(one, {}) for one in twins]

    assert shown == [
        OtherShown(
            address=f"/parent/grades/results/{one}/homework#homework",
            title=LOG,
            class_name=BIOLOGY,
            due="Due August 20",
            term="T1",
            year=YEAR,
        )
        for one in ("result-first", "result-second")
    ]


BOTH_CLASSES = {LOG_ID: [THE_CLASS.class_id, THEN_CLASS.class_id]}
"""The log's homework name connected to this year's class and to last year's."""


@pytest.mark.parametrize(
    ("items", "results", "links", "in_doubt", "classes", "waiting"),
    [
        pytest.param(
            [homework(), TWIN_ROW], [school_result("r1")], [], [], {}, {"r1"}, id="twin homework"
        ),
        pytest.param(
            [homework()],
            [school_result("r1"), school_result("r2")],
            [],
            [],
            {},
            {"r1", "r2"},
            id="twin results on the page",
        ),
        pytest.param(
            [homework()],
            [school_result("r1"), school_result("r2", term="T2")],
            [],
            [],
            {},
            {"r1"},
            id="a twin result in another term",
        ),
        pytest.param(
            [homework()],
            [school_result("r1"), school_result("r2", class_id=THEN_CLASS.class_id)],
            [],
            [],
            BOTH_CLASSES,
            {"r1"},
            id="a twin result in another class",
        ),
        pytest.param(
            [homework()],
            [school_result("r1"), a_result("r2", None, class_id=THEN_CLASS.class_id)],
            [],
            [],
            BOTH_CLASSES,
            {"r1"},
            id="a twin result in another year's class, its day unresolved",
        ),
        pytest.param(
            [homework()], [school_result("r1")], [], [LOG_ID], {}, {"r1"}, id="homework in doubt"
        ),
        pytest.param(
            [UNDATED_LOG], [school_result("r1")], [], [], {}, {"r1"}, id="homework with no date"
        ),
        pytest.param(
            [homework()],
            [a_result("r1", None)],
            [],
            [],
            {},
            {"r1"},
            id="the result's day unresolved",
        ),
        pytest.param([homework()], [school_result("r1")], [], [], {}, set(), id="the rule's pair"),
        pytest.param(
            [homework()],
            [school_result("r1", scored=False)],
            [],
            [],
            {},
            set(),
            id="the rule's pair, points not reported",
        ),
        pytest.param(
            [homework()],
            [school_result("r1", shown=False)],
            [],
            [],
            {},
            set(),
            id="the rule's pair, not shown",
        ),
        pytest.param(
            [homework(), TWIN_ROW],
            [school_result("r1")],
            [confirmed(LOG_ID, "r1")],
            [],
            {},
            set(),
            id="twin homework, one linked",
        ),
        pytest.param(
            [homework(), TWIN_ROW],
            [school_result("r1", scored=False)],
            [confirmed(LOG_ID, "r1")],
            [],
            {},
            set(),
            id="twin homework, one linked, points not reported",
        ),
        pytest.param(
            [homework(), TWIN_ROW],
            [school_result("r1", shown=False)],
            [confirmed(LOG_ID, "r1")],
            [],
            {},
            set(),
            id="twin homework, one linked, not shown",
        ),
        pytest.param(
            [homework(), TWIN_ROW],
            [school_result("r1")],
            [confirmed(LOG_ID, "r1")],
            [],
            {LOG_ID: []},
            set(),
            id="twin homework, one linked, its name not connected",
        ),
        pytest.param(
            [homework(), TWIN_ROW],
            [school_result("r1")],
            [kept_apart(LOG_ID, "r1")],
            [],
            {},
            set(),
            id="twin homework, one kept apart, the other's day settled",
        ),
        pytest.param(
            [homework(), TWIN_ROW],
            [school_result("r1")],
            [kept_apart(LOG_ID, "r1")],
            ["assignment-twin"],
            {},
            {"r1"},
            id="twin homework, one kept apart, the other in doubt",
        ),
        pytest.param(
            [NOTEBOOK_ROW], [school_result("r1")], [], [], {}, set(), id="no homework of its title"
        ),
        pytest.param(
            [homework()],
            [school_result("r1")],
            [],
            [],
            {LOG_ID: ["class-other"]},
            set(),
            id="no homework name connects to its class",
        ),
        pytest.param(
            [homework()],
            [school_result("r1"), school_result("r2", term="T2")],
            [confirmed(LOG_ID, "r2")],
            [],
            {},
            {"r1"},
            id="its one homework linked to another result",
        ),
        pytest.param(
            [homework()],
            [school_result("r1"), school_result("r2", term="T2")],
            [confirmed(LOG_ID, "r2"), confirmed(LOG_ID, "r1")],
            [],
            {},
            set(),
            id="its one homework linked to another result, then to it",
        ),
        pytest.param(
            [homework()],
            [school_result("r1"), school_result("r2", term="T2")],
            [confirmed(LOG_ID, "r2"), kept_apart(LOG_ID, "r1")],
            [],
            {},
            set(),
            id="its one homework linked to another result, then kept apart from it",
        ),
        pytest.param(
            [homework(), NOTEBOOK_ROW],
            [school_result("r1"), school_result("r2", term="T2")],
            [confirmed(LOG_ID, "r2"), confirmed(NOTEBOOK_ID, "r1")],
            [],
            {},
            set(),
            id="its one homework linked to another result, it linked to other homework",
        ),
        pytest.param(
            [homework(), TWIN_ROW],
            [school_result("r1", scored=False)],
            [],
            [],
            {},
            {"r1"},
            id="twin homework, points not reported",
        ),
        pytest.param(
            [homework(), TWIN_ROW],
            [school_result("r1"), school_result("r2")],
            [confirmed(LOG_ID, "r1")],
            [],
            {},
            set(),
            id="two rows and two results, one pair linked",
        ),
        pytest.param(
            [homework(), TWIN_ROW],
            [school_result("r1"), school_result("r2", scored=False)],
            [confirmed(LOG_ID, "r1")],
            [],
            {},
            set(),
            id="two rows and two results, one pair linked, the other's points not reported",
        ),
    ],
)
def test_the_results_waiting_on_a_class_and_term_are_those_whose_own_page_says_why(
    items: list[Assignment],
    results: list[Result],
    links: list[Link],
    in_doubt: list[str],
    classes: dict[str, list[str]],
    waiting: set[str],
) -> None:
    """Plain values: the results of the one class's first term that wait are exactly those whose
    own page gives a reason the rules leave them waiting, with terms compared folded. None of
    them is one the rules stand with homework, so none shows a score on her homework by them."""
    plain = as_read(items, results, in_doubt=in_doubt)
    read = plain._replace(classes={**plain.classes, **classes})

    here = waiting_in(THE_CLASS.class_id, "T1", read, links=links)
    spelled = waiting_in(THE_CLASS.class_id, " T1 ", read, links=links)

    assert here == spelled == waiting
    on_page = [one for one in results if (one.class_id, one.term) == (THE_CLASS.class_id, "T1")]
    for one in on_page:
        _, _, said = result_said(viewed(one.result_id, read, links))
        assert bool(said) is (one.result_id in here)
    stood = {
        one.result_id
        for one in candidate_homework(read.homework, read.results, read.classes, decided=links)
        if one.stood is not None
    }
    ruled = school_scores(read.homework, read.results, read.classes, decided=links)
    by_the_rules = {at.result_id for shown in ruled.values() for at in shown if not at.linked}
    assert not here & (stood | by_the_rules)


def test_a_result_the_rules_stand_with_one_row_carries_no_wait_while_another_row_stays_open() -> (
    None
):
    """Plain values: two rows and two results of one title, class and day, the first row linked
    to the first result. The second result's page shows its match with the second row and keeps
    the first row as a candidate already linked elsewhere; it waits on no class and term, with
    its points reported or not."""
    log = homework()
    links = [confirmed(LOG_ID, "r1")]
    for scored in (True, False):
        results = [school_result("r1"), school_result("r2", scored=scored)]
        read = as_read([log, TWIN_ROW], results)

        view = viewed("r2", read, links)
        here = waiting_in(THE_CLASS.class_id, "T1", read, links=links)

        assert here == frozenset()
        assert view.matched == row(TWIN_ROW)
        assert view.candidates == (row(log, another_result=True), row(TWIN_ROW))
        assert view.waiting is None


def test_the_results_waiting_on_a_class_and_term_are_refused_whole_on_conflicting_links() -> None:
    """Plain values. Two confirmed links that name one result raise the conflict error with no
    cause and no context, whichever class and term is asked about."""
    read = as_read([homework(), TWIN_ROW], [school_result("r1"), school_result("r2", term="T2")])
    links = [confirmed(LOG_ID, "r2"), confirmed("assignment-twin", "r2")]

    for term in ("T1", "T2"):
        with pytest.raises(GradeValueUndecodable) as refused:
            waiting_in(THE_CLASS.class_id, term, read, links=links)

        assert type(refused.value) is HomeworkLinksConflict
        assert refused.value.__cause__ is None
        assert refused.value.__context__ is None


WAITS_LINE = '<p class="source">Blossom doesn&#39;t match this result by itself.</p>'
"""The quiet line on a card of class details whose result waits for a parent's choice."""


def parent_class_page(client: TestClient, class_id: str, term: str = "T1") -> str:
    """Class details of a class and term as a parent reads it, on the computer Blossom runs on
    with the sign-in off."""
    store = store_of(client)
    named = store.class_named(class_id)
    assert named is not None
    n = store.terms_of_year(named.year).index(term) + 1
    address = f"/parent/grades/classes/{class_id}/terms/{n}"
    answer = computer(client).get(address, headers=PAGE_HEADERS)
    assert answer.status_code == 200
    return answer.text


def cued_on(page: str) -> frozenset[str]:
    """The results whose card on a class details page says they wait for a parent's choice,
    each the same as those whose way in has the same words at the end of its accessible name."""
    cued = frozenset(
        one for one in order_on_class_details(page) if WAITS_LINE in card_of(page, one)
    )
    named = frozenset(
        address.split("/")[4]
        for address, label in ways_on(page)
        if label.endswith(f". {grades.NOT_MATCHED_ALONE}")
    )
    assert named == cued
    return cued


Waits = tuple[frozenset[str], frozenset[str], frozenset[str]]
"""Who waits on a class and term: by the function, by each result's own page, on class details."""


def waiting_now(client: TestClient, class_id: str, term: str = "T1") -> Waits:
    """The results of a class and term that wait for a parent's choice, from the store as it
    stands, read in one hold as the result's page reads it; those of the class and term whose
    own page, read on its own, says why the rules leave them waiting; and those whose card on
    class details, as a parent reads it, says they wait."""
    state = state_of(client)
    store = state.project_state
    with store.reading():
        everyone = store.all_assignments()
        years = read_grade_facts(store, (one.course for one in everyone))
        links = read_links(store)
        claims = (
            store.read_claims([one.assignment_id for one in everyone])
            if results_read(years)
            else None
        )
        read = rule_values(
            everyone,
            {} if claims is None else claims.records,
            () if claims is None else claims.unreadable,
            years,
        )
        listed = list(store.class_term(class_id, term).current.results)
    said: set[str] = set()
    for result_id in listed:
        page = result_page_read(state, result_id)
        assert page is not None
        if page["waiting"]:
            said.add(result_id)
    cued = cued_on(parent_class_page(client, class_id, term))
    return waiting_in(class_id, term, read, links=links), frozenset(said), cued


def results_titled(
    client: TestClient, class_id: str, term: str, titles: Collection[str]
) -> set[str]:
    """The IDs of a class and term's results whose title is one of ``titles``."""
    values = store_of(client).class_term(class_id, term).current.results
    return {one for one, value in values.items() if value.cells["assignment"][1] in titles}


Pages = list[tuple[str, str, set[str]]]
"""Each class and term read, by class ID and term, with the titles of its results that wait."""


def twin_homework_on_record(client: TestClient) -> Pages:
    store_of(client).put_on_record([homework(), TWIN_ROW], {})
    return [(with_report(client), "T1", {LOG})]


def twin_homework_without_points(client: TestClient) -> Pages:
    store_of(client).put_on_record([homework(), TWIN_ROW], {})
    return [(with_report(client, NO_POINTS), "T1", {LOG})]


def twin_results_on_the_page(client: TestClient) -> Pages:
    store_of(client).put_on_record([homework()], {})
    return [(with_report(client, TWIN_LOGS), "T1", {LOG})]


def twin_results_in_two_terms(client: TestClient) -> Pages:
    store_of(client).put_on_record([homework()], {})
    class_id = with_report(client)
    with_report(client, SECOND_TERM)
    return [(class_id, "T1", {LOG}), (class_id, "T2", {LOG})]


def a_twin_result_last_year_its_first_month_unconfirmed(client: TestClient) -> Pages:
    store = store_of(client)
    store.put_on_record([homework()], {})
    class_id = with_report(client)
    then = draft_of(IN_WEEK.replace(f"**{YEAR}**", "**2025-2026**"))
    review = store.review_grade_report(then, capture_key(then), key=KEY)
    outcome = save_grade(
        store, then, key=KEY, review=review, answers=grade_answers(review, month=None)
    )
    assert isinstance(outcome, GradeReportSaved), outcome
    return [(class_id, "T1", {LOG}), (capture_class(store, then), "T1", {LOG})]


def homework_in_doubt(client: TestClient) -> Pages:
    store = store_of(client)
    store.put_on_record([homework()], {})
    unreadable_claim(store, LOG_ID)
    return [(with_report(client), "T1", {LOG})]


def homework_with_no_date(client: TestClient) -> Pages:
    store_of(client).put_on_record([UNDATED_LOG], {})
    return [(with_report(client), "T1", {LOG})]


def the_rule_s_pair(client: TestClient) -> Pages:
    store_of(client).put_on_record([homework()], {})
    class_id = with_report(client)
    assert [line for line, _, _ in on_card(client)] == [LINE]
    return [(class_id, "T1", set())]


def the_rule_s_pair_without_points(client: TestClient) -> Pages:
    store_of(client).put_on_record([homework()], {})
    return [(with_report(client, NO_POINTS), "T1", set())]


def the_rule_s_pair_not_shown(client: TestClient) -> Pages:
    store_of(client).put_on_record([homework()], {})
    class_id = with_report(client)
    with_report(client, WITHOUT_LOG, complete=True)
    return [(class_id, "T1", set())]


def a_name_connected_to_no_class(client: TestClient) -> Pages:
    store_of(client).put_on_record([homework(course="Chemistry")], {})
    return [(with_report(client), "T1", set())]


@pytest.mark.parametrize(
    "setup",
    [
        twin_homework_on_record,
        twin_homework_without_points,
        twin_results_on_the_page,
        twin_results_in_two_terms,
        a_twin_result_last_year_its_first_month_unconfirmed,
        homework_in_doubt,
        homework_with_no_date,
        the_rule_s_pair,
        the_rule_s_pair_without_points,
        the_rule_s_pair_not_shown,
        a_name_connected_to_no_class,
    ],
)
def test_the_results_waiting_on_a_class_and_term_through_the_store_are_those_whose_page_says_why(
    setup: Callable[[TestClient], Pages],
) -> None:
    """Each record below saved through the store's own writes, then read as the result's page
    reads it. On each class and term the results that wait are the ones listed, exactly the
    ones whose own page says why the rules leave them waiting, and exactly the ones class details
    shows a parent as waiting; results of other titles never wait."""
    with browser() as client:
        pages = setup(client)
        found = [
            (
                waiting_now(client, class_id, term),
                results_titled(client, class_id, term, titles),
            )
            for class_id, term, titles in pages
        ]

    for (waiting, said, cued), expected in found:
        assert waiting == said == cued == expected


def test_a_result_waits_while_its_homework_is_linked_elsewhere_until_a_decision_of_its_own() -> (
    None
):
    """The log's homework and a notebook, and the log's result in two terms, the first linked
    to the log's homework by the store's own write. The second waits until it is linked to that
    homework, kept apart from it, or linked to the notebook, and waits again once returned to
    the rules; linked, its card says which homework instead. A read of who waits, class details
    as a parent reads it among them, leaves every table as it was."""
    with browser() as client:
        store_of(client).put_on_record([homework(), NOTEBOOK_ROW], {})
        class_id = with_report(client)
        with_report(client, SECOND_TERM)
        first = result_named(client, class_id, LOG)
        second = result_named(client, class_id, LOG, "T2")

        def now() -> Waits:
            return waiting_now(client, class_id, "T2")

        def decide(decision: LinkDecision, assignment_id: str = LOG_ID) -> None:
            parent_links(client, class_id, second, assignment_id, term="T2", decision=decision)

        both = now()
        parent_links(client, class_id, first)
        world = closed_world([database_of(client)], leaving_out=())
        elsewhere = now()
        read_alone = closed_world([database_of(client)], leaving_out=())
        decide("link")
        linked = now()
        linked_card = card_of(parent_class_page(client, class_id, "T2"), second)
        decide("apart")
        apart = now()
        decide("automatic")
        returned = now()
        decide("link", NOTEBOOK_ID)
        linked_to_other = now()

    waits = frozenset({second})
    assert both == elsewhere == returned == (waits, waits, waits)
    assert read_alone == world
    assert linked == apart == linked_to_other == (frozenset(), frozenset(), frozenset())
    assert f'<span class="authored-text">Linked to homework: {LOG}.</span>' in linked_card
    assert WAITS_LINE not in linked_card


def test_a_row_kept_apart_leaves_its_twin_to_the_rules_unless_its_twin_s_day_is_in_doubt() -> None:
    """Twin homework of the log's result, the log's row kept apart by the store's own write. The
    twin's row then shows the log's score by the rules and nothing waits; with a date claim about
    the twin that can't be read, the result waits and no score shows."""
    twin = TWIN_ROW.assignment_id
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(), TWIN_ROW], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        parent_links(client, class_id, log, decision="apart")
        settled = waiting_now(client, class_id), on_card(client, twin)
        unreadable_claim(store, twin)
        in_doubt = waiting_now(client, class_id), on_card(client, twin)

    waits = frozenset({log})
    assert settled[0] == (frozenset(), frozenset(), frozenset())
    assert [line for line, _, _ in settled[1]] == [LINE]
    assert in_doubt == ((waits, waits, waits), [])


def test_a_result_the_rules_show_on_one_row_waits_nowhere_and_its_read_stores_nothing() -> None:
    """Two rows of the log's homework and the log's result in two terms, the first row linked to
    the first term's result by the store's own write. The second term's result shows on the
    second row by the rules, keeps the first row as a candidate, and waits nowhere; with its
    points left blank by a newer report it still waits nowhere. Reading who waits leaves every
    table as it was."""
    twin = TWIN_ROW.assignment_id
    with browser() as client:
        store_of(client).put_on_record([homework(), TWIN_ROW], {})
        class_id = with_report(client)
        with_report(client, SECOND_TERM)
        first = result_named(client, class_id, LOG)
        second = result_named(client, class_id, LOG, "T2")
        parent_links(client, class_id, first)
        world = closed_world([database_of(client)], leaving_out=())
        shown = waiting_now(client, class_id, "T2"), on_card(client, twin)
        read_alone = closed_world([database_of(client)], leaving_out=())
        page = result_page_read(state_of(client), second)
        with_report(client, NO_POINTS.replace("**T1**", "**T2**"))
        blank = waiting_now(client, class_id, "T2"), on_card(client, twin)

    nothing: Waits = (frozenset(), frozenset(), frozenset())
    assert shown[0] == blank[0] == nothing
    assert [line for line, _, _ in shown[1]] == [LINE]
    assert blank[1] == []
    assert read_alone == world
    assert page is not None
    assert page["state"] == grades.Said(grades.MATCHED_TO.format(title=LOG), authored=True)
    assert page["waiting"] == []
    rows = page["rows"]
    assert isinstance(rows, list)
    (linked_elsewhere,) = [one for one in rows if one.assignment_id == LOG_ID]
    assert [said.text for said in linked_elsewhere.said] == [grades.ANOTHER_RESULT]
    assert linked_elsewhere.buttons == [grades.LINK_HOMEWORK, grades.NOT_THIS_HOMEWORK]


LOG_LINE = next(line for line in IN_WEEK.split("\n") if line.startswith(LOG_ROW))
TWIN_LOGS = IN_WEEK.replace(
    LOG_LINE,
    LOG_LINE + "\n" + LOG_LINE.replace("18.0    | 20.0    | 90.0", "17.0    | 20.0    | 85.0"),
)
"""The report with a second row of the log's title and due date, scored apart: two results."""


def test_identical_looking_twins_on_a_result_s_page_are_two_links_to_their_own_pages() -> None:
    """Two results of the log's title and due date in the first term, and the log's in the
    second. The second term's page lists both twins with the same words, in class details'
    order, each a link to its own page."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client, TWIN_LOGS)
        with_report(client, SECOND_TERM)
        values = store_of(client).class_term(class_id, "T1").current.results
        twins = [one for one, value in values.items() if value.cells["assignment"][1] == LOG]
        page = result_page(client, result_named(client, class_id, LOG, "T2"))
        order = order_on_class_details(class_details(client, class_id))

    assert len(twins) == 2
    assert others_on(page) == [
        "Other results that could be the same homework:",
        *[LOG, BIOLOGY, "Due August 20", "T1 · 2026-2027"] * 2,
    ]
    assert other_links(page) == [(way_in_to(one), LOG) for one in order if one in twins]


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
SEARCH_HEADING_AT = '<h2 id="search-heading">'
"""Where a result's page's search begins, after its homework rows and any other results."""
OTHERS_AT = '<h2 id="other-results">Other results that could be the same homework:</h2>'
"""The heading over the other results that could be the same homework, before the search."""


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
    """Each line a reader reads on a result's page after its result card and before its other
    results and its search, in order: the paragraphs, list items, homework headings and
    buttons, each as its text."""
    start = page.index('<h1 id="homework"')
    end = page.index(OTHERS_AT, start) if OTHERS_AT in page else page.index(SEARCH_HEADING_AT)
    return lines_of(page[page.index("</section>", start) + len("</section>") : end])


def lines_of(part: str) -> list[str]:
    """Each paragraph, list item, heading, label and button in a piece of a page, in order,
    each as its text alone."""
    return [
        " ".join(html.unescape(re.sub(r"<[^>]+>", "", found[2])).split())
        for found in re.finditer(r"<(p|li|h2|h3|label|button)\b[^>]*>(.*?)</\1>", part, re.S)
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
    ask = (
        '\n    <p class="details-link"><a class="assignment-link" '
        f'href="/student/grades/results/{log}/ask" aria-label="Ask a parent about this result: '
        f'{LOG}">Ask a parent about this result</a></p>'
    )
    assert card_of(details, log).count(ask) == 1
    assert card_of(page, log).replace("h2 ", "h3 ").replace("</h2>", "</h3>") == card_of(
        details, log
    ).replace(ask, "")
    assert '<h1 id="homework" tabindex="-1">Home&shy;work for this result</h1>' in page
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
    assert "autofocus" not in page
    back = f'<a href="/parent/grades/classes/{class_id}/terms/1">Back to '
    assert back + f'<span class="authored-text">{BIOLOGY}</span></a>' in page
    addresses = re.findall(r'(?:href|action)="([^"]*)"', page)
    assert LOG_ID not in " ".join(addresses)
    assert [one for one in addresses if "/results/" in one] == [
        address,
        address,
        f"{address}/search#found",
    ]


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
    another year's among them with its range marked by its place, and each row's words and
    buttons for each decision."""
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
        f"Life Science isn't connected to {BIOLOGY}, a class of 2025-2026. The Homework class "
        "names page changes connections for the current school year only.",
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
    assert [said.text for said in rows[PairStanding.APART].said] == [
        f"Kept apart from {LOG}. Blossom won't match these two by itself.",
        f"Linking moves this result's link from {NOTEBOOK}.",
        ANOTHER,
    ]
    assert rows[PairStanding.APART].buttons == [
        ("link", "Link to this homework"),
        ("automatic", "Let Blossom match again"),
    ]
    assert rows[PairStanding.LINKED].buttons == [("apart", "Remove link")]
    assert rows[PairStanding.LINKED].said == []
    year_named = result_said(dataclasses.replace(view, class_name="Biology 2025-2026"))[1][-1]
    assert year_named.keep == "2025-2026"
    assert year_named.text[year_named.keep_at :].startswith(
        "2025-2026. The Homework class names page"
    )
    assert year_named.keep_at > year_named.text.index("2025-2026")
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
    assert focuses_its_answer(answer.text)
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


def test_a_database_error_from_her_links_on_a_result_s_page_answers_that_it_can_t_be_shown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A database error from her links' read, which is not the named error, answers the page
    that can't be shown, and its one record names the error's kind alone."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)

        def refused() -> None:
            msg = "synthetic"
            raise sqlite3.DatabaseError(msg)

        monkeypatch.setattr(store_of(client), "homework_links", refused)
        with caplog.at_level(logging.DEBUG):
            answer = computer(client).get(RESULT_PAGE.format(result_id=log), headers=PAGE_HEADERS)
            records = list(caplog.records)

    cannot_be_shown(
        answer,
        "Homework for this result",
        "This result's homework can't be shown right now.",
        RESULT_PAGE.format(result_id=log),
        [("/parent/grades", "Back to Grades")],
    )
    logged_by_kind_alone(records, "Homework for this result could not be read: DatabaseError")


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
    """With the sign-in off, her class details asks the same statements, and reads the same
    page, with a link and a result that waits on record as with neither, and none of a link, a
    homework row or a hold. A parent's asks hers, then, inside one hold, her links, the linked
    homework's row, and last the reads the rules take to say who waits, with no link statement
    among them."""
    her_record = " ".join(gradebook.HER_NAME_RECORD.split())

    def asked(client: TestClient, address: str) -> tuple[list[str], str]:
        seen: list[str] = []
        connection = store_of(client)._connection
        connection.set_trace_callback(seen.append)
        try:
            answer = client.get(address, headers=PAGE_HEADERS)
        finally:
            connection.set_trace_callback(None)
        assert answer.status_code == 200
        return [" ".join(statement.split()) for statement in seen], answer.text

    with browser() as client:
        store = store_of(client)
        class_id = with_report(client)
        hers = f"/student/grades/classes/{class_id}/terms/1"
        parents = f"/parent/grades/classes/{class_id}/terms/1"
        with_none, page_with_none = asked(client, hers)
        twin = homework("assignment-practice-twin", PRACTICE, date(2026, 8, 12))
        practice = homework(PRACTICE_ID, PRACTICE, date(2026, 8, 12))
        store.put_on_record([homework(NOTEBOOK_ID, NOTEBOOK, None), practice, twin], {})
        parent_links(client, class_id, result_named(client, class_id, LOG), NOTEBOOK_ID)
        with_one, page_with_one = asked(client, hers)
        parent, parent_page = asked(computer(client), parents)
        her_links = gradebook.HOMEWORK_LINKS.replace("?", f"'{store.student_id()}'")
        seen: list[str] = []
        store._connection.set_trace_callback(seen.append)
        everyone = store.all_assignments()
        years = read_grade_facts(store, (one.course for one in everyone))
        store.read_claims([one.assignment_id for one in everyone])
        store._connection.set_trace_callback(None)
        practice_result = result_named(client, class_id, PRACTICE)

    rules_read = [" ".join(statement.split()) for statement in seen]
    assert results_read(years)
    assert with_one == with_none
    assert page_with_one == page_with_none
    assert not [one for one in with_one if "grade_homework_links" in one or "assignments" in one]
    assert not [one for one in with_one if one.startswith(("BEGIN", "COMMIT", "ROLLBACK"))]
    at = parent.index(her_links)
    assert parent[0].startswith("BEGIN")
    assert parent[1 : at - 1] == with_one
    assert parent[at - 1] == her_record
    assert parent[at + 1].endswith(f"FROM assignments WHERE assignment_id = '{NOTEBOOK_ID}'")
    assert parent[at + 2 : -1] == rules_read
    assert not [one for one in rules_read if "grade_homework_links" in one]
    assert parent[-1] == "COMMIT"
    assert parent.count(her_links) == 1
    assert cued_on(parent_page) == {practice_result}
    assert "match this result" not in page_with_one


def test_a_waiting_result_s_card_says_so_before_its_way_in_whose_name_ends_the_same() -> None:
    """Twin homework of the log's result. On class details as a parent reads it with the
    sign-in off, the log's card carries the quiet line directly before its way in, whose
    accessible name is the way's own followed by the same words; no other card does, and her
    page says nothing of it."""
    with browser() as client:
        store_of(client).put_on_record([homework(), TWIN_ROW], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        page = parent_class_page(client, class_id)
        hers = class_details(client, class_id)

    way = (
        f'<p class="details-link"><a class="assignment-link" href="{way_in_to(log)}" '
        f'aria-label="Homework for this result: {LOG}. Blossom doesn&#39;t match this result by '
        'itself.">Homework for this result</a></p>'
    )
    after_score = f'<p class="effect">{LINE}</p>\n    \n    {WAITS_LINE}\n    {way}\n    <details '
    assert after_score in card_of(page, log)
    assert page.count(WAITS_LINE) == 1
    assert cued_on(page) == {log}
    assert "match this result" not in hers


@pytest.mark.parametrize(
    ("reader", "trees"),
    [("parent", ("student", "parent")), ("her", ("student",))],
)
def test_only_a_parent_reading_class_details_sees_which_results_wait_on_either_tree(
    reader: str, trees: tuple[str, ...], tmp_path: pathlib.Path
) -> None:
    """With the sign-in on, a parent reading class details on either tree sees the log's result
    wait while twin homework could be it; she sees nothing of it, and her page reads as it does
    with no homework on record."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        class_id = with_report(client)
        before = [class_details(client, class_id, tree=tree) for tree in trees]
        store_of(client).put_on_record([homework(), TWIN_ROW], {})
        log = result_named(client, class_id, LOG)
        pages = [class_details(client, class_id, tree=tree) for tree in trees]

    if reader == "her":
        assert pages == before
        assert "match this result" not in pages[0]
        return
    assert [cued_on(page) for page in pages] == [{log}, {log}]


@pytest.mark.parametrize("cause", [False, True])
def test_a_conflict_only_the_waiting_read_meets_stops_a_parent_s_class_details_with_one_record(
    cause: bool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Her links as the page reads them made to hold two confirmed links that name the log's
    result, the store's own check left out, so only the rules' own check meets them; and, with
    her links as stored, the refusal raised by the rules with a cause holding a chosen text. A
    parent's page answers as an unanswered failure does, with one record of the type alone and
    no card; nothing is raised and hers opens."""
    with browser() as client:
        store_of(client).put_on_record([homework(), TWIN_ROW], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        two = (confirmed(LOG_ID, log), confirmed(TWIN_ROW.assignment_id, log))
        if not cause:
            monkeypatch.setattr(grades, "read_links", lambda _: two)
        else:

            def refused(*_: object, **__: object) -> frozenset[str]:
                try:
                    raise ValueError(SYNTHETIC_CAUSE)
                except ValueError as error:
                    raise HomeworkLinksConflict from error

            monkeypatch.setattr(grades, "waiting_in", refused)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            answer = computer(client).get(
                f"/parent/grades/classes/{class_id}/terms/1", headers=PAGE_HEADERS
            )
            records = list(caplog.records)
        hers = client.get(f"/student/grades/classes/{class_id}/terms/1", headers=PAGE_HEADERS)

    assert (answer.status_code, answer.text) == (500, "Internal Server Error")
    assert answer.headers["content-type"] == "text/plain; charset=utf-8"
    application = [one for one in records if one.name.startswith("blossom")]
    assert [(one.name, one.levelno, one.getMessage()) for one in application] == [
        (
            "blossom.routes.grades",
            logging.WARNING,
            "a grade page wasn't shown: HomeworkLinksConflict",
        )
    ]
    assert [one for one in records if SYNTHETIC_CAUSE in one.getMessage()] == []
    assert with_details(records) == []
    assert hers.status_code == 200


def test_her_links_in_conflict_stop_a_parent_s_class_details_before_who_waits_is_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two confirmed links that name the log's result, in a file changed by hand, with twin
    homework that would make it wait. A parent's class details of the log's class and term, and
    of a second class with results neither link names, each stop at her links with one record
    naming the conflict, and ask no homework statement."""
    with browser() as client:
        store_of(client).put_on_record([homework(), TWIN_ROW], {})
        class_id = with_report(client)
        second_class = with_report(client, CHEMISTRY)
        conflicting(client, result_named(client, class_id, LOG))
        answers = []
        for one in (class_id, second_class):
            seen: list[str] = []
            connection = store_of(client)._connection
            connection.set_trace_callback(seen.append)
            caplog.clear()
            with caplog.at_level(logging.DEBUG):
                answer = computer(client).get(
                    f"/parent/grades/classes/{one}/terms/1", headers=PAGE_HEADERS
                )
                kept = list(caplog.records)
            connection.set_trace_callback(None)
            answers.append((answer.status_code, answer.text, kept, seen))

    assert second_class != class_id
    for status, text, kept, seen in answers:
        assert (status, text) == (500, "Internal Server Error")
        messages = [one.getMessage() for one in kept if one.name.startswith("blossom")]
        assert messages == ["a grade page wasn't shown: HomeworkLinksConflict"]
        assert with_details(kept) == []
        assert not [one for one in seen if "assignments" in one]


def test_a_linked_result_s_card_names_its_homework_and_never_says_it_waits_when_withheld() -> None:
    """Twin homework under a name a parent connects, the log's result waiting until linked to
    one row by the store's own write. Linked, its card on class details names that homework and
    says nothing of waiting, with its score showing, with its points left blank by a newer
    report, with a complete report that doesn't show it, and with the name disconnected."""
    name = "Life Science"
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [homework(course=name), homework(TWIN_ROW.assignment_id, course=name)], {}
        )
        class_id = with_report(client)
        store.connect_homework_class(YEAR, name, shown=None, chosen=class_id, role="parent")
        log = result_named(client, class_id, LOG)
        before = cued_on(parent_class_page(client, class_id))
        parent_links(client, class_id, log)
        cards = [card_of(parent_class_page(client, class_id), log)]
        with_report(client, NO_POINTS)
        cards.append(card_of(parent_class_page(client, class_id), log))
        with_report(client, WITHOUT_LOG, complete=True)
        cards.append(card_of(parent_class_page(client, class_id), log))
        store.connect_homework_class(YEAR, name, shown=class_id, chosen=UNCONNECTED, role="parent")
        cards.append(card_of(parent_class_page(client, class_id), log))

    assert before == {log}
    for card in cards:
        assert f'<p><span class="authored-text">Linked to homework: {LOG}.</span></p>' in card
        assert WAITS_LINE not in card
        assert "match this result" not in card


def test_her_links_in_conflict_stop_a_signed_in_parent_s_class_details_on_either_tree(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """With the sign-in on, two confirmed links that name the log's result, in a file changed by
    hand, and twin homework that would make it wait: a parent's class details on her tree and on
    the family tree each stop at her links, with one record naming the conflict."""
    with household_client("parent", tmp_path) as client:
        sign_in_as(client, "parent")
        store_of(client).put_on_record([homework(), TWIN_ROW], {})
        class_id = with_report(client)
        conflicting(client, result_named(client, class_id, LOG))
        answers = []
        details: list[logging.LogRecord] = []
        for tree in ("student", "parent"):
            caplog.clear()
            with caplog.at_level(logging.DEBUG):
                answer = client.get(
                    f"/{tree}/grades/classes/{class_id}/terms/1", headers=PAGE_HEADERS
                )
                kept = list(caplog.records)
            messages = [one.getMessage() for one in kept if one.name.startswith("blossom")]
            answers.append((answer.status_code, answer.text, messages))
            details.extend(with_details(kept))

    stopped = (500, "Internal Server Error", ["a grade page wasn't shown: HomeworkLinksConflict"])
    assert answers == [stopped, stopped]
    assert details == []


CHEMISTRY = IN_WEEK.replace("07 BIO - C", "08 CHE - A").replace(
    "| **Biology** |", "| **Chemistry** |"
)
"""The report of a second class, Chemistry, with the first's results."""
CHEMISTRY_ROW = homework("assignment-safety", "Lab Safety Quiz", None, course="Chemistry")
"""Homework whose class name connects the second class."""
NOT_CHECKED_LINE = (
    '<p class="problem">Blossom couldn&#39;t check which results here need your choice. '
    "Some may.</p>"
)
"""The one sentence of class details whose results' waiting couldn't be worked out."""
NOT_CHECKED_NOTE = "Blossom couldn't check whether this result needs your choice."
NO_CUE = "class details shows no cue: GradeValueUndecodable"


def two_classes(client: TestClient) -> tuple[str, str]:
    """The first class with twin homework of its log, a notebook linked to its diagram by the
    store's own write, and a second class a homework name connects to. Both classes."""
    store_of(client).put_on_record([homework(), TWIN_ROW, NOTEBOOK_ROW, CHEMISTRY_ROW], {})
    class_id = with_report(client)
    other = with_report(client, CHEMISTRY)
    parent_links(client, class_id, result_named(client, class_id, DIAGRAM), NOTEBOOK_ID)
    return class_id, other


def damaged(client: TestClient, damage: str, other: str) -> None:
    """A stored value only the read of who waits meets, in the second class."""
    if damage == "a cell":
        points_presence(client, result_named(client, other, LOG), SMUDGED)
    else:
        stored(
            store_of(client), "UPDATE grade_reports SET term_label = 'T9' WHERE class_id = ?", other
        )


def with_details(records: Iterable[logging.LogRecord]) -> list[logging.LogRecord]:
    """The records among ``records`` that carry an exception, its text or a stack."""
    return [one for one in records if one.exc_info or one.exc_text or one.stack_info]


def blossom_records(caplog: pytest.LogCaptureFixture) -> list[tuple[str, int, str]]:
    """Each record Blossom's own loggers wrote, by logger, level and message."""
    return [
        (one.name, one.levelno, one.getMessage())
        for one in caplog.records
        if one.name.startswith("blossom")
    ]


@pytest.mark.parametrize("damage", ["a cell", "a term its year does not list"])
def test_when_only_who_waits_can_t_be_read_a_parent_gets_the_page_with_one_sentence_and_notes(
    damage: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A value only the read of who waits meets, in a second connected class: a cell that can't
    be decoded, or a report in a term its year does not list. A parent's class details of the
    first class answers 200 with every card, every way in and the linked homework's line, no
    line saying a result waits, the one sentence before the results, the note at the end of every
    way in's name, and one record of the type alone each time it is read. Her links are asked
    once, before that read. With nothing damaged there is no sentence and no record; hers is
    the same page either way."""
    her_links_at = "FROM grade_homework_links"
    with browser() as client:
        class_id, other = two_classes(client)
        log = result_named(client, class_id, LOG)
        diagram = result_named(client, class_id, DIAGRAM)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            sound = parent_class_page(client, class_id)
            sound_records = blossom_records(caplog)
        hers_before = class_details(client, class_id)
        damaged(client, damage, other)
        seen: list[str] = []
        store_of(client)._connection.set_trace_callback(seen.append)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            page = parent_class_page(client, class_id)
            once = blossom_records(caplog)
            again = parent_class_page(client, class_id)
            twice = blossom_records(caplog)
            parents_kept = list(caplog.records)
        store_of(client)._connection.set_trace_callback(None)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            hers = class_details(client, class_id)
            her_records = blossom_records(caplog)
            hers_kept = list(caplog.records)
        titles = {
            result_id: title_of(value)
            for result_id, value in store_of(client)
            .class_term(class_id, "T1")
            .current.results.items()
        }

    assert cued_on(sound) == {log}
    assert NOT_CHECKED_LINE not in sound
    assert sound_records == []
    assert page == again
    assert page.count(NOT_CHECKED_LINE) == 1
    assert page.index(NOT_CHECKED_LINE) < page.index('<h2 class="grade-heading">')
    assert page.index("</h1>") < page.index(NOT_CHECKED_LINE)
    assert cued_on(page) == frozenset()
    assert "match this result" not in page
    assert order_on_class_details(page) == order_on_class_details(sound)
    assert sorted(ways_on(page)) == sorted(
        (way_in_to(result_id), f"Homework for this result: {title}. {NOT_CHECKED_NOTE}")
        for result_id, title in titles.items()
    )
    assert f'<span class="authored-text">Linked to homework: {NOTEBOOK}.</span>' in card_of(
        page, diagram
    )
    record = ("blossom.routes.homework_scores", logging.WARNING, NO_CUE)
    assert once == [record]
    assert twice == [record, record]
    assert with_details(parents_kept) == []
    assert with_details(hers_kept) == []
    reads = " ".join(" ".join(one.split()) for one in seen).split("BEGIN")[1:]
    assert len(reads) == 2
    for read in reads:
        assert read.count(her_links_at) == 1
        assert read.index(her_links_at) < read.index("FROM assignments ORDER BY")
    assert hers == hers_before
    assert her_records == []
    assert NOT_CHECKED_NOTE not in hers
    assert "couldn&#39;t check" not in hers


@pytest.mark.parametrize("failure", ["two links", "a link value", "a link value and two links"])
def test_her_links_stop_the_page_before_who_waits_is_read_also_when_that_read_would_fail(
    failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Two confirmed links that name the log's result, a link row whose homework ID is no
    UTF-8, or both, each in a file changed by hand, with a cell only the read of who waits
    meets that can't be decoded. A parent's class details stops at her links with the generic
    500 and one record naming what her links met, never the page with the sentence and never
    its record; hers opens."""
    with browser() as client:
        class_id, other = two_classes(client)
        damaged(client, "a cell", other)
        if "two links" in failure:
            conflicting(client, result_named(client, class_id, LOG))
        if "a link value" in failure:
            stored(
                store_of(client),
                "UPDATE grade_homework_links SET assignment_id = CAST(? AS TEXT) "
                "WHERE assignment_id = ?",
                SYNTHETIC_CAUSE.encode() + b"\xff",
                NOTEBOOK_ID,
            )
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            answer = computer(client).get(
                f"/parent/grades/classes/{class_id}/terms/1", headers=PAGE_HEADERS
            )
            records = blossom_records(caplog)
            kept = list(caplog.records)
        hers = client.get(f"/student/grades/classes/{class_id}/terms/1", headers=PAGE_HEADERS)

    kind = "HomeworkLinksConflict" if failure == "two links" else "GradeValueUndecodable"
    assert (answer.status_code, answer.text) == (500, "Internal Server Error")
    assert records == [
        ("blossom.routes.grades", logging.WARNING, f"a grade page wasn't shown: {kind}")
    ]
    assert with_details(kept) == []
    assert [one for one in kept if SYNTHETIC_CAUSE in one.getMessage()] == []
    assert hers.status_code == 200


def test_a_conflict_the_store_s_check_misses_stops_the_page_though_who_waits_can_t_be_read(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Her links as the page reads them made to hold two confirmed links that name the log's
    result, the store's own check left out, with a cell only the read of who waits meets that
    can't be decoded. A parent's class details stops with the generic 500 and one record naming
    the conflict, never the page with the sentence and never its record; nothing is raised."""
    with browser() as client:
        class_id, other = two_classes(client)
        log = result_named(client, class_id, LOG)
        two = (confirmed(LOG_ID, log), confirmed(TWIN_ROW.assignment_id, log))
        monkeypatch.setattr(grades, "read_links", lambda _: two)
        damaged(client, "a cell", other)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            answer = computer(client).get(
                f"/parent/grades/classes/{class_id}/terms/1", headers=PAGE_HEADERS
            )
            records = blossom_records(caplog)
            kept = list(caplog.records)

    assert (answer.status_code, answer.text) == (500, "Internal Server Error")
    assert records == [
        (
            "blossom.routes.grades",
            logging.WARNING,
            "a grade page wasn't shown: HomeworkLinksConflict",
        )
    ]
    assert with_details(kept) == []


BEFORE_THE_RESULTS = (
    '<p class="note">Report added August 19</p>\n    \n    \n'
    '      <h2 class="grade-heading">Assignments</h2>'
)
"""The bytes between the term's summary and the first results heading of class details, as the
page writes them with no sentence to say: two lines of spaces, nothing else."""


def test_class_details_writes_nothing_before_its_results_when_who_waits_is_not_in_doubt() -> None:
    """Her class details, a parent's of the class with a result that waits, and a parent's of
    the second class, where none waits, write exactly the same bytes between the term's summary
    and the first results heading; only the page that couldn't work out who waits has its
    sentence there."""
    with browser() as client:
        class_id, other = two_classes(client)
        hers = class_details(client, class_id)
        parents_waiting = parent_class_page(client, class_id)
        parents_none_waiting = parent_class_page(client, other)
        damaged(client, "a cell", other)
        unchecked = parent_class_page(client, class_id)

    assert cued_on(parents_waiting) != frozenset()
    assert cued_on(parents_none_waiting) == frozenset()
    for page in (hers, parents_waiting, parents_none_waiting):
        assert page.count(BEFORE_THE_RESULTS) == 1
    assert (
        '<p class="note">Report added August 19</p>\n    \n'
        f"      {NOT_CHECKED_LINE}\n    \n"
        '      <h2 class="grade-heading">Assignments</h2>'
    ) in unchecked


OWN_FAILURES = {
    "a cell in this class and term": (
        "UPDATE grade_result_observations SET points_presence = ?",
        (SMUDGED,),
    ),
    "a term row": ("UPDATE grade_terms SET made_at = CAST(made_at AS BLOB)", ()),
    "a report's moment that is no moment": ("UPDATE grade_reports SET imported_at = ?", (SMUDGED,)),
}
"""Values the page's own read or its lines meet, each written as no save writes it."""


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("failure", list(OWN_FAILURES))
def test_class_details_own_failure_is_raised_as_today_before_her_links_and_who_waits(
    failure: str, conflict: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """A value of the page's own data that can't be decoded, alone and beside two confirmed
    links that name one result, with a cell only the read of who waits meets damaged too. A
    parent's page and hers fail with the stored value's own error, raised as today; no link or
    homework statement is asked and no record is written."""
    with browser() as client:
        class_id, other = two_classes(client)
        damaged(client, "a cell", other)
        if conflict:
            conflicting(client, result_named(client, class_id, LOG))
        statement, binds = OWN_FAILURES[failure]
        stored(store_of(client), statement, *binds)
        raised = []
        seen: list[str] = []
        store_of(client)._connection.set_trace_callback(seen.append)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            for reader, address in (
                (computer(client), f"/parent/grades/classes/{class_id}/terms/1"),
                (client, f"/student/grades/classes/{class_id}/terms/1"),
            ):
                with pytest.raises(GradeValueUndecodable) as error:
                    reader.get(address, headers=PAGE_HEADERS)
                raised.append(type(error.value))
            records = blossom_records(caplog)
        store_of(client)._connection.set_trace_callback(None)

    assert raised == [GradeValueUndecodable, GradeValueUndecodable]
    assert records == []
    assert not [one for one in seen if "grade_homework_links" in one or "assignments" in one]


@pytest.mark.parametrize("row", ["met only by who waits", "named by a link"])
def test_a_homework_row_that_can_t_be_read_fails_a_parent_s_class_details_with_its_own_error(
    row: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A homework row whose kind is none Blossom knows, in a file changed by hand: one no link
    names, which only the read of who waits meets, or the notebook a link names. A parent's page
    fails with that read's own error and no record; hers asks no homework statement and opens."""
    with browser() as client:
        class_id, _ = two_classes(client)
        assignment_id = TWIN_ROW.assignment_id if row == "met only by who waits" else NOTEBOOK_ID
        stored(
            store_of(client),
            "UPDATE assignments SET kind = ? WHERE assignment_id = ?",
            SMUDGED,
            assignment_id,
        )
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(Exception) as raised:  # noqa: PT011
                computer(client).get(
                    f"/parent/grades/classes/{class_id}/terms/1", headers=PAGE_HEADERS
                )
            hers = client.get(f"/student/grades/classes/{class_id}/terms/1", headers=PAGE_HEADERS)
            records = blossom_records(caplog)

    assert not isinstance(raised.value, GradeValueUndecodable)
    assert records == []
    assert hers.status_code == 200


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


def test_a_database_error_from_her_links_on_class_details_answers_that_it_can_t_be_shown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with browser() as client:
        class_id = with_report(client)

        def refused() -> None:
            msg = "synthetic"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store_of(client), "homework_links", refused)
        details = f"/parent/grades/classes/{class_id}/terms/1"
        with caplog.at_level(logging.DEBUG):
            answer = computer(client).get(details, headers=PAGE_HEADERS)
            records = list(caplog.records)

    cannot_be_shown(
        answer,
        "Class details",
        "Class details can't be shown right now.",
        details,
        [("/parent/grades", "Back to Grades")],
    )
    logged_by_kind_alone(records, "Class details could not be read: OperationalError")


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


ANSWER_IN_FOCUS = '<p class="problem" role="alert" id="problem" tabindex="-1" autofocus>'
"""The answer of a page that says why, which takes the focus when the page opens."""


def focuses_its_answer(page: str) -> bool:
    """Whether a page gives the focus to its answer when it opens, and to nothing else."""
    return page.count(ANSWER_IN_FOCUS) == 1 and page.count("autofocus") == 1


def problem_on(page: str) -> str:
    found = re.search(r'<p class="problem" role="alert" id="problem"[^>]*>([^<]*)</p>', page)
    assert found is not None
    return html.unescape(found[1])


def test_each_decision_from_each_prior_state_writes_its_row_and_lands_on_the_heading() -> None:
    """Link, remove the link, link again, let the rules match again, then not this homework:
    each answers 303 to the heading, changes only the link row, raises the class and term's
    revision by one, and lands on a page holding that heading, which can take the focus."""
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
    assert '<h1 id="homework" tabindex="-1">Home&shy;work for this result</h1>' in landed.text
    assert "autofocus" not in landed.text


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
    assert focuses_its_answer(answer.text)
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
    assert focuses_its_answer(answer.text)
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
    assert focuses_its_answer(answer.text)
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
    assert focuses_its_answer(answer.text)
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
    assert focuses_its_answer(answer.text)
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
    assert focuses_its_answer(answer.text)
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


@pytest.mark.parametrize("name", ["Earth Science", "Earth Science 2025-2026"])
def test_a_school_year_range_on_a_result_s_page_is_marked_as_one_piece(name: str) -> None:
    """Homework linked to last year's log, then put on record under another class name, by the
    store's writes: the page names last year's class and its year, only the sentence's year slot
    and the term's range marked, a name's own copy of the range not, and offers no link."""
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
        store.put_on_record([homework(NOTEBOOK_ID, NOTEBOOK, None, course=name)], {})
        page = result_page(client, log)

    reason = (
        f"{name} isn't connected to {BIOLOGY}, a class of 2025-2026. The Homework class names "
        "page changes connections for the current school year only."
    )
    assert reason in said_on(page)
    (item,) = [one for one in re.findall(r"<li>(.*?)</li>", page, re.S) if "a class of" in one]
    assert re.findall(r'<span class="year-range">([^<]*)</span>', item) == ["2025-2026"]
    before = item.split('<span class="year-range">')[0]
    assert html.unescape(re.sub(r"<[^>]+>", "", before)).endswith("a class of ")
    assert (
        f'<li><span class="authored-text">{name} isn&#39;t connected to {BIOLOGY}, a class '
        'of <span class="year-range">2025-2026</span>. The Homework class names page changes '
        "connections for the current school year only.</span></li>"
    ) in page
    assert '<p>T1 · <span class="year-range">2025-2026</span></p>' in page
    assert '<a href="/parent/grades/homework-classes">' not in page


@pytest.mark.parametrize(
    ("failing", "named"),
    [
        pytest.param("another term", "HomeworkLinksConflict", id="a value of another term"),
        pytest.param("its own term", "GradeValueUndecodable", id="a value of its own term"),
    ],
)
def test_a_result_s_page_reads_its_own_term_then_her_links_then_the_rest(
    failing: str, named: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Two confirmed links to the first term's log, and a points presence that is no presence
    in the second term's log or in the first term's diagram, each in a file changed by hand:
    the one record names the failure the page's read order meets first."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        with_report(client, SECOND_TERM)
        first = result_named(client, class_id, LOG)
        conflicting(client, first)
        if failing == "another term":
            points_presence(client, result_named(client, class_id, LOG, "T2"), SMUDGED)
        else:
            points_presence(client, result_named(client, class_id, DIAGRAM), SMUDGED)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            answer = computer(client).get(RESULT_PAGE.format(result_id=first), headers=PAGE_HEADERS)
            records = [one.getMessage() for one in caplog.records if one.name.startswith("blossom")]

    assert (answer.status_code, answer.text) == (500, "Internal Server Error")
    assert records == [f"a grade page wasn't shown: {named}"]


def test_a_date_claim_that_can_t_be_read_leaves_the_result_waiting_as_on_her_week() -> None:
    """The log's homework with one more date claim that can't be read, as a file changed by
    hand holds it: her week shows no score for it, and the result's page says a due date
    isn't settled rather than that the rules matched it."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        unreadable_claim(store, LOG_ID, THURSDAY.isoformat())
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        page = result_page(client, log)
        card = on_card(client)

    assert card == []
    assert said_on(page) == [
        "Not linked to homework.",
        "Blossom doesn't match these by itself:",
        "a due date isn't settled.",
        *LOG_ROW_SAID,
        *CHOOSE,
    ]


def test_an_unrelated_error_from_the_write_leaves_the_decision_route_as_itself(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The store's write made to raise a ValueError in the test: the decision route raises it
    as itself, with no record of a link not written."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        revision = revision_of(client, log)

        def refused(*_: object, **__: object) -> None:
            msg = "synthetic"
            raise ValueError(msg)

        monkeypatch.setattr(store_of(client), "decide_homework_link", refused)
        with caplog.at_level(logging.DEBUG), pytest.raises(ValueError, match="synthetic"):
            pressed(client, log, revision=revision)

    assert not [one for one in caplog.records if "wasn't written" in one.getMessage()]


def test_a_result_whose_term_is_not_among_its_year_s_terms_answers_the_generic_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The year's term row deleted from the file by hand, as no save does: the result's page
    answers the plain generic 500 with one record of the failure's type alone."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        stored(store, "DELETE FROM grade_terms WHERE label = 'T1'")
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            answer = computer(client).get(RESULT_PAGE.format(result_id=log), headers=PAGE_HEADERS)
            records = [one.getMessage() for one in caplog.records if one.name.startswith("blossom")]

    assert (answer.status_code, answer.text) == (500, "Internal Server Error")
    assert records == ["a grade page wasn't shown: GradeValueUndecodable"]


@pytest.mark.parametrize("decision", ["link", "apart", "automatic"])
def test_a_press_for_a_result_whose_term_is_not_among_its_year_s_terms_writes_nothing(
    decision: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The page read, then the year's term row deleted from the file by hand, as no save does:
    each decision, one that would stand included, answers the plain generic 500 with one record
    of the failure's type alone, and the file is as it was."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        revision = forms_on(result_page(client, log))[0][3]
        stored(store, "DELETE FROM grade_terms WHERE label = 'T1'")
        before = every_row(database_of(client))
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            answer = pressed(client, log, decision=decision, revision=revision)
            records = [one.getMessage() for one in caplog.records if one.name.startswith("blossom")]
        after = every_row(database_of(client))

    assert (answer.status_code, answer.text) == (500, "Internal Server Error")
    assert records == ["a homework link wasn't written: GradeValueUndecodable"]
    assert after == before


def test_class_details_and_a_result_s_page_are_rendered_after_every_hold_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each page these routes render is built with no hold of the store open: class details
    and its page for a class or place not on record, as a parent and as she reads them, a
    result's page and its answers to a press."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        held = [0]
        reading = store.reading

        @contextlib.contextmanager
        def counted() -> Iterator[None]:
            held[0] += 1
            try:
                with reading():
                    yield
            finally:
                held[0] -= 1

        built: list[tuple[str, int]] = []
        response = templates.TemplateResponse

        def recorded(
            request: Request, name: str, context: dict[str, object], status_code: int = 200
        ) -> Response:
            built.append((name, held[0]))
            return response(request, name, context, status_code=status_code)

        monkeypatch.setattr(store, "reading", counted)
        monkeypatch.setattr(templates, "TemplateResponse", recorded)
        answers = [
            computer(client).get(
                f"/parent/grades/classes/{class_id}/terms/9", headers=PAGE_HEADERS
            ),
            computer(client).get(
                f"/parent/grades/classes/{class_id}/terms/1", headers=PAGE_HEADERS
            ),
            client.get(f"/student/grades/classes/{class_id}/terms/9", headers=PAGE_HEADERS),
            computer(client).get(RESULT_PAGE.format(result_id=log), headers=PAGE_HEADERS),
            pressed(client, log, revision="999999"),
        ]

    assert [one.status_code for one in answers] == [404, 200, 404, 200, 409]
    assert built == [
        ("grade_class.html", 0),
        ("grade_class.html", 0),
        ("grade_class.html", 0),
        ("grade_result_homework.html", 0),
        ("grade_result_homework.html", 0),
    ]


def test_a_revision_below_its_column_s_bound_stops_a_result_s_page_and_every_press(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The class and term's revision set to 0 by hand, the table's checks turned off for that
    one statement, as no write does: the page and a press of each decision answer the plain
    generic 500 with one record of the failure's type alone, and the file is as it was."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        stored(store, "PRAGMA ignore_check_constraints = ON")
        stored(store, "UPDATE grade_scope_revisions SET revision = 0")
        stored(store, "PRAGMA ignore_check_constraints = OFF")
        before = every_row(database_of(client))
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            page = computer(client).get(RESULT_PAGE.format(result_id=log), headers=PAGE_HEADERS)
            shown = [one.getMessage() for one in caplog.records if one.name.startswith("blossom")]
        presses = []
        for decision in ("link", "apart", "automatic"):
            caplog.clear()
            with caplog.at_level(logging.DEBUG):
                answer = pressed(client, log, decision=decision, revision="0")
                written = [
                    one.getMessage() for one in caplog.records if one.name.startswith("blossom")
                ]
            presses.append((answer.status_code, answer.text, written))
        after = every_row(database_of(client))

    assert (page.status_code, page.text) == (500, "Internal Server Error")
    assert shown == ["a grade page wasn't shown: GradeValueUndecodable"]
    assert (
        presses
        == [
            (
                500,
                "Internal Server Error",
                ["a homework link wasn't written: GradeValueUndecodable"],
            )
        ]
        * 3
    )
    assert after == before


SEARCH_PAGE = RESULT_PAGE + "/search"
FOUND_AT = '<h2 class="list-heading" id="found" tabindex="-1">Homework found</h2>'
"""The heading over what a search found, which a search's answer lands on."""
SEARCH_LINES = [
    f"Homework in {BIOLOGY}",
    "Search homework",
    "Type a word or two from the title or the class name.",
    "Search",
]
"""The search's heading, its field's label and hint, and its button, as a parent reads them."""
WORDS_REFUSED = (
    "Search words are one line of up to 200 characters. Nothing was searched, and nothing was "
    "changed."
)
NO_PAGE = "There is no such page of results."
REVIEW_WORDS = '<p class="jump"><a href="#search-words">Review your search words</a></p>'
"""What follows the explanation of refused words: the way to the field that holds them."""
LINK = "Link to this homework"
SKETCH_ID = "assignment-sketch"
Sent = Mapping[str, str | list[str]]


def search_posted(client: TestClient, result_id: str, sent: Sent) -> Answered:
    """A search's form posted from the computer the application runs on."""
    return computer(client).post(
        SEARCH_PAGE.format(result_id=result_id), data=sent, headers=PAGE_HEADERS
    )


def search(client: TestClient, result_id: str, words: str, page: str = "") -> Answered:
    """A search posted as the page's own form sends it."""
    return search_posted(client, result_id, {"words": words, "page": page})


def search_said_on(page: str) -> list[str]:
    """Each line a reader reads in a result's page's search, in order, each as its text."""
    return lines_of(page[page.index(SEARCH_HEADING_AT) : page.index("</main>")])


def found_on(page: str) -> str:
    """What a search's answer shows from the heading over what it found."""
    return page[page.index(FOUND_AT) :]


def field_of(page: str) -> str:
    """The words the search's field holds."""
    found = re.search(r'<input id="search-words" type="text" name="words" value="([^"]*)"', page)
    assert found is not None
    return html.unescape(found[1])


def page_presses(page: str) -> list[tuple[str, str, str, str]]:
    """Each page control: its form's action, the words and the page number it carries, and its
    button's words."""
    found = re.findall(
        r'<form method="post" action="([^"]*)">\s*'
        r'<input type="hidden" name="words" value="([^"]*)">\s*'
        r'<input type="hidden" name="page" value="([^"]*)">\s*'
        r'<button type="submit" class="secondary">([^<]*)</button>',
        page,
    )
    return [(html.unescape(a), html.unescape(b), c, d) for a, b, c, d in found]


def link_names(page: str) -> list[str]:
    """The accessible name of each found row's Link button."""
    return [
        html.unescape(one) for one in re.findall(rf'aria-label="([^"]*)">{LINK}</button>', page)
    ]


def addresses_on(page: str) -> list[str]:
    """Every link's and every form's address on a page."""
    return [html.unescape(one) for one in re.findall(r'(?:href|action)="([^"]*)"', page)]


def test_a_result_s_page_offers_a_search_whose_words_and_page_travel_in_the_form_s_body() -> None:
    """The page ends with the search under its heading: one empty field and one button, in a
    form that posts the words and an empty page number to the search's address. The address
    holds the result's ID and lands on the heading over what is found, which isn't there yet."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        page = result_page(client, log)

    address = SEARCH_PAGE.format(result_id=log)
    search_part = page[page.index(SEARCH_HEADING_AT) :]
    assert search_said_on(page) == SEARCH_LINES
    assert (
        f'<h2 id="search-heading"><span class="authored-text">Homework in {BIOLOGY}</span>' in page
    )
    assert f'<form method="post" action="{address}#found" class="decision">' in search_part
    assert (
        '<input id="search-words" type="text" name="words" value="" autocomplete="off" '
        'aria-describedby="search-hint">'
    ) in search_part
    assert search_part.count('<input type="hidden" name="page" value="">') == 1
    assert search_part.count("<form") == 1
    assert 'id="found"' not in page
    assert "autofocus" not in page


def test_a_search_answers_the_result_s_page_with_each_row_found_its_state_and_its_form() -> None:
    """Four homework rows of the class, a task among them: one linked to the log's result, one
    kept apart from it, one linked to the diagram's, and one with no decision. A search finds
    all four, in one order, each with at most one line of state; every row the result isn't
    linked to has a form that posts the one write, and a button named for its homework."""
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [
                homework(),
                homework(NOTEBOOK_ID, NOTEBOOK, None),
                homework(PRACTICE_ID, PRACTICE, date(2026, 8, 12)),
                homework(SKETCH_ID, "Field sketch", None, course=CODE, kind=AssignmentKind.TASK),
            ],
            {},
        )
        class_id = with_report(client)
        log, diagram = (result_named(client, class_id, title) for title in (LOG, DIAGRAM))
        parent_links(client, class_id, log, NOTEBOOK_ID)
        parent_links(client, class_id, log, decision="apart")
        parent_links(client, class_id, diagram, PRACTICE_ID)
        before = closed_world([database_of(client)], leaving_out=())
        answer = search(client, log, "bio")
        after = closed_world([database_of(client)], leaving_out=())
        revision = revision_of(client, log)

    page, address = answer.text, RESULT_PAGE.format(result_id=log)
    assert answer.status_code == 200
    assert search_said_on(page) == [
        *SEARCH_LINES,
        "Homework found",
        "Showing 1 to 4 of 4.",
        f"Linking moves this result's link from {NOTEBOOK}.",
        *(CODE, "Field sketch", "Due date not recorded", LINK),
        *(BIOLOGY, NOTEBOOK, "Due date not recorded", "Linked to this result."),
        *(BIOLOGY, PRACTICE, "Due Wednesday, August 12", ANOTHER, LINK),
        *(BIOLOGY, LOG, LOG_DUE, "Kept apart from this result.", LINK),
    ]
    assert forms_on(found_on(page)) == [
        (address, SKETCH_ID, "link", revision),
        (address, PRACTICE_ID, "link", revision),
        (address, LOG_ID, "link", revision),
    ]
    assert link_names(page) == [
        f"{LINK}: Field sketch, {CODE}, no due date",
        f"{LINK}: {PRACTICE}, {BIOLOGY}, due Wednesday, August 12",
        f"{LINK}: {LOG}, {BIOLOGY}, due Thursday, August 20",
    ]
    assert field_of(page) == "bio"
    assert page.count(FOUND_AT) == 1
    assert "autofocus" not in page
    assert page_presses(page) == []
    assert ties_hold(page)
    assert not [one for one in addresses_on(page) if "bio" in one.casefold()]
    assert after == before


def test_a_found_row_s_link_press_is_the_one_write_whatever_stands_and_lands_on_the_heading() -> (
    None
):
    """A found row's form posted as it stands: on homework with no decision, on homework linked
    to nothing while the result is linked elsewhere, then on homework kept apart. Each is the
    decision route's one write: 303 to the result's heading, and the link moves."""
    with browser() as client:
        store_of(client).put_on_record([homework(), homework(SKETCH_ID, "Field sketch", None)], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        presses, links = [], []
        for words in ("sketch", "seed", "sketch"):
            (form,) = forms_on(found_on(search(client, log, words).text))
            action, homework_id, decision, revision = form
            presses.append(
                computer(client).post(
                    action,
                    data={"homework": homework_id, "decision": decision, "revision": revision},
                    headers=PAGE_HEADERS,
                )
            )
            links.append(stored_links(client))

    landing = f"{RESULT_PAGE.format(result_id=log)}#homework"
    assert [(one.status_code, one.headers["location"]) for one in presses] == [(303, landing)] * 3
    confirmed_state, apart_state = gradebook.LINK_CONFIRMED, gradebook.LINK_APART
    assert links == [
        [(SKETCH_ID, confirmed_state)],
        [(LOG_ID, confirmed_state), (SKETCH_ID, apart_state)],
        [(LOG_ID, apart_state), (SKETCH_ID, confirmed_state)],
    ]


@pytest.mark.parametrize(
    ("stale", "said"),
    [
        pytest.param("another link", LINKS_CHANGED, id="another link in the class and term"),
        pytest.param(
            "name disconnected",
            "That homework's class name is connected differently now. Nothing was saved. "
            "Check this page again.",
            id="the class name disconnected",
        ),
        pytest.param(
            "homework removed",
            "That homework is not on record now. Nothing was saved.",
            id="the homework removed",
        ),
    ],
)
def test_a_link_press_from_an_earlier_search_is_checked_by_the_write_and_writes_nothing(
    stale: str, said: str
) -> None:
    """A found row's form from a search made before another link in the class and term, before
    the homework's class name is disconnected, or before the homework is removed by hand.
    The write answers 409 with the page as it reads now, without the search's list."""
    name = "Life Science"
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework(), homework(NOTEBOOK_ID, NOTEBOOK, None, course=name)], {})
        class_id = with_report(client)
        log, diagram = (result_named(client, class_id, title) for title in (LOG, DIAGRAM))
        store.connect_homework_class(YEAR, name, shown=None, chosen=class_id, role="parent")
        (form,) = forms_on(found_on(search(client, log, "notebook").text))
        if stale == "another link":
            parent_links(client, class_id, diagram)
        elif stale == "name disconnected":
            store.connect_homework_class(
                YEAR, name, shown=class_id, chosen=UNCONNECTED, role="parent"
            )
        else:
            stored(store, "DELETE FROM assignments WHERE assignment_id = ?", NOTEBOOK_ID)
        links = stored_links(client)
        action, homework_id, decision, revision = form
        answer = computer(client).post(
            action,
            data={"homework": homework_id, "decision": decision, "revision": revision},
            headers=PAGE_HEADERS,
        )
        links_after = stored_links(client)

    assert answer.status_code == 409
    assert problem_on(answer.text) == said
    assert focuses_its_answer(answer.text)
    assert FOUND_AT not in answer.text
    assert field_of(answer.text) == ""
    assert links_after == links


SET_ROWS = [homework(f"assignment-set-{n:02}", SET, None) for n in range(1, 22)]
"""Twenty-one undated homework rows of the class, one title among them all."""


def test_a_search_comes_twenty_to_a_page_and_its_page_controls_are_forms_that_reach_each_row() -> (
    None
):
    """Twenty-one rows no word tells apart. The first page lists twenty with "Next page", a
    form carrying the words and page 2; that page lists the last with "Previous page", whose
    form gives the first page again. No address holds the words or a page number."""
    with browser() as client:
        store_of(client).put_on_record(SET_ROWS, {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        first = search(client, log, "practice set")
        ((action, words, number, _),) = page_presses(first.text)
        second = search_posted(client, log, {"words": words, "page": number})
        ((back_action, back_words, back, _),) = page_presses(second.text)
        again = search_posted(client, log, {"words": back_words, "page": back})
        one = search(client, log, "practice set", "1")

    searching = f"{SEARCH_PAGE.format(result_id=log)}#found"
    assert [one.status_code for one in (first, second, again)] == [200] * 3
    assert page_presses(first.text) == [(searching, "practice set", "2", "Next page")]
    assert page_presses(second.text) == [(searching, "practice set", "1", "Previous page")]
    assert (action, back_action) == (searching, searching)
    assert search_said_on(first.text)[4:6] == ["Homework found", "Showing 1 to 20 of 21."]
    assert search_said_on(second.text)[4:6] == ["Homework found", "Showing 21 to 21 of 21."]
    assert search_said_on(again.text) == search_said_on(first.text) == search_said_on(one.text)
    reached = [row for page in (first, second) for _, row, _, _ in forms_on(found_on(page.text))]
    assert reached == [item.assignment_id for item in SET_ROWS]
    for page in (first, second):
        assert field_of(page.text) == "practice set"
        assert page.text.count(FOUND_AT) == 1
        assert not [one for one in addresses_on(page.text) if "practice" in one or "page=" in one]


@pytest.mark.parametrize(
    ("words", "said"),
    [
        pytest.param("", "Type a word or two to search. Nothing was searched.", id="no words"),
        pytest.param("   ", "Type a word or two to search. Nothing was searched.", id="spaces"),
        pytest.param(
            "geometry",
            f"No homework in {BIOLOGY} matches every word. Try fewer words, or other ones.",
            id="nothing found",
        ),
    ],
)
def test_a_search_with_no_words_or_words_that_find_nothing_says_so_under_its_heading(
    words: str, said: str
) -> None:
    """The answer is 200 and lands on the heading over what was found, which says why nothing
    is listed; the words stay in the field, and nothing is listed."""
    with browser() as client:
        store_of(client).put_on_record(SET_ROWS, {})
        class_id = with_report(client)
        answer = search(client, result_named(client, class_id, LOG), words)

    assert answer.status_code == 200
    assert search_said_on(answer.text) == [*SEARCH_LINES, "Homework found", said]
    assert field_of(answer.text) == words
    assert forms_on(found_on(answer.text)) == []
    assert page_presses(answer.text) == []
    assert "autofocus" not in answer.text


REFUSED_SEARCHES: list[tuple[str, Sent, str]] = [
    ("two lines", {"words": "practice\nset", "page": ""}, WORDS_REFUSED),
    ("201 characters", {"words": "s" * 201, "page": ""}, WORDS_REFUSED),
    ("201 once collapsed", {"words": " s" * 101, "page": ""}, WORDS_REFUSED),
    ("words twice", {"words": ["practice", "set"], "page": ""}, WORDS_REFUSED),
    ("another form's field", {"words": "practice", "page": "", "homework": LOG_ID}, WORDS_REFUSED),
    ("words left out", {"page": ""}, WORDS_REFUSED),
    ("page left out", {"words": "practice"}, WORDS_REFUSED),
    ("page twice", {"words": "practice", "page": ["", "2"]}, WORDS_REFUSED),
    ("page 0", {"words": "practice", "page": "0"}, NO_PAGE),
    ("page 3 of 2", {"words": "practice", "page": "3"}, NO_PAGE),
    ("a leading zero", {"words": "practice", "page": "02"}, NO_PAGE),
    ("a page in words", {"words": "practice", "page": "two"}, NO_PAGE),
    ("seven digits", {"words": "practice", "page": "1234567"}, NO_PAGE),
]
"""Each search answered 422, with the explanation it gets."""


def test_a_search_refused_or_naming_no_page_answers_422_with_its_explanation_and_no_list() -> None:
    """Each form of ``REFUSED_SEARCHES`` on a result with twenty-one rows to find: 422, its
    explanation taking the focus, no heading over what was found, no list and no page controls,
    the words posted kept in the field. Refused words alone mark the field and end the
    explanation with a link to it. Nothing written."""
    with browser() as client:
        store_of(client).put_on_record(SET_ROWS, {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        before = closed_world([database_of(client)], leaving_out=())
        answers = [search_posted(client, log, sent) for _, sent, _ in REFUSED_SEARCHES]
        after = closed_world([database_of(client)], leaving_out=())

    for (case, sent, said), answer in zip(REFUSED_SEARCHES, answers, strict=True):
        words = sent.get("words", "")
        kept = words[0] if isinstance(words, list) else words
        assert answer.status_code == 422, case
        assert problem_on(answer.text) == said, case
        assert focuses_its_answer(answer.text), case
        assert FOUND_AT not in answer.text, case
        assert forms_on(answer.text[answer.text.index(SEARCH_HEADING_AT) :]) == [], case
        assert page_presses(answer.text) == [], case
        assert html.unescape(re.sub(r"\s", "", field_of(answer.text))) == re.sub(r"\s", "", kept)
        refused = 'aria-describedby="search-hint problem" aria-invalid="true"' in answer.text
        assert refused is (said == WORDS_REFUSED), case
        explained = re.escape(f"{ANSWER_IN_FOCUS}{html.escape(said)}</p>")
        assert re.search(explained, answer.text), case
        followed = re.search(explained + r"\s*" + re.escape(REVIEW_WORDS), answer.text)
        assert (followed is not None) is (said == WORDS_REFUSED), case
        assert answer.text.count('href="#search-words"') == (1 if said == WORDS_REFUSED else 0)
    assert after == before


def test_a_search_s_words_are_kept_on_no_such_page_and_search_gives_the_first_page_again() -> None:
    """Twenty-one rows found and page 3 asked: 422 with no list and no page controls, the
    words in the field. The page's own search form, posted as it stands, gives the first page."""
    with browser() as client:
        store_of(client).put_on_record(SET_ROWS, {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        past = search(client, log, "practice set", "3")
        again = search(client, log, field_of(past.text), "")

    assert past.status_code == 422
    assert problem_on(past.text) == NO_PAGE
    assert field_of(past.text) == "practice set"
    assert page_presses(past.text) == []
    assert '<input type="hidden" name="page" value="">' in past.text
    assert again.status_code == 200
    assert search_said_on(again.text)[4:6] == ["Homework found", "Showing 1 to 20 of 21."]


@pytest.mark.parametrize(
    "asked", ["result-not-a-result", "result-" + "0" * 32], ids=["no such shape", "none"]
)
def test_a_search_for_no_current_result_answers_404_as_the_page_does(asked: str) -> None:
    """A search, and a form that is not whole, for an ID of no such shape or of no result."""
    with browser() as client:
        answers = [
            search(client, asked, "log"),
            search_posted(client, asked, {"words": "log"}),
        ]

    for answer in answers:
        assert answer.status_code == 404
        assert focuses_its_answer(answer.text)
        assert "Blossom has no current record of this result." in answer.text
        assert "<form" not in answer.text


@pytest.mark.parametrize("asked", [LOG, DIAGRAM])
@pytest.mark.parametrize("failure", ["a stored value", "two links"])
def test_a_search_stopped_by_a_grade_failure_answers_the_generic_500_and_one_record(
    failure: str, asked: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A points presence of the log's result that is no presence, whose decoding error holds a
    chosen text; or two confirmed links to the log's result in a file changed by hand. On the
    log's page and on the diagram's, which the links don't name, a search, a page control's
    press, refused words and a form that is not whole each answer the generic 500, raise
    nothing, write nothing, and leave one record naming the error's type alone."""
    sent: list[Sent] = [
        {"words": "practice", "page": ""},
        {"words": "practice", "page": "2"},
        {"words": "two\nlines", "page": ""},
        {"page": ""},
    ]
    with browser() as client:
        store_of(client).put_on_record([homework(), *SET_ROWS], {})
        class_id = with_report(client)
        log, searched_on = (result_named(client, class_id, title) for title in (LOG, asked))
        if failure == "a stored value":
            points_presence(client, log, SYNTHETIC_CAUSE)
        else:
            conflicting(client, log)
        before = closed_world([database_of(client)], leaving_out=())
        answers, records = [], []
        for one in sent:
            caplog.clear()
            with caplog.at_level(logging.DEBUG):
                answers.append(search_posted(client, searched_on, one))
                records.append(list(caplog.records))
        after = closed_world([database_of(client)], leaving_out=())

    kind = "GradeValueUndecodable" if failure == "a stored value" else "HomeworkLinksConflict"
    for answer, seen in zip(answers, records, strict=True):
        assert (answer.status_code, answer.text) == (500, "Internal Server Error")
        assert answer.headers["content-type"] == "text/plain; charset=utf-8"
        application = [one for one in seen if one.name.startswith("blossom")]
        assert [one.getMessage() for one in application] == [f"a grade page wasn't shown: {kind}"]
        assert [(one.name, one.levelno) for one in application] == [
            ("blossom.routes.grades", logging.WARNING)
        ]
        assert [one for one in seen if one.exc_info or SYNTHETIC_CAUSE in one.getMessage()] == []
    assert after == before


def test_a_value_error_inside_a_search_is_not_caught(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A value error that is not the named error, from the search's filtering, leaves the
    search's route as itself, with no record."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)

        def refused(*_: object, **__: object) -> None:
            msg = "synthetic"
            raise ValueError(msg)

        monkeypatch.setattr(grades, "homework_found", refused)
        with caplog.at_level(logging.DEBUG), pytest.raises(ValueError, match="synthetic"):
            search(client, log, "log")

    assert not [one for one in caplog.records if "wasn't shown" in one.getMessage()]


def test_a_database_error_from_her_links_inside_a_search_answers_that_it_can_t_be_shown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A database error from her links' read inside a search, which is not the named error,
    answers the result's page that can't be shown, with no search form, and its one record
    names the error's kind alone."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)

        def refused(*_: object, **__: object) -> None:
            msg = "synthetic"
            raise sqlite3.DatabaseError(msg)

        monkeypatch.setattr(store_of(client), "homework_links", refused)
        with caplog.at_level(logging.DEBUG):
            answer = search(client, log, "log")
            records = list(caplog.records)

    cannot_be_shown(
        answer,
        "Homework for this result",
        "This result's homework can't be shown right now.",
        RESULT_PAGE.format(result_id=log),
        [("/parent/grades", "Back to Grades")],
    )
    logged_by_kind_alone(records, "Homework for this result could not be read: DatabaseError")


def test_no_record_holds_a_search_s_words(caplog: pytest.LogCaptureFixture) -> None:
    """A search that finds homework, a page control's press, refused words and a page that
    names no page: no record of any logger holds the words searched."""
    words = "sketch-words-for-the-log-check"
    with browser() as client:
        store_of(client).put_on_record([homework(SKETCH_ID, f"Field {words}", None)], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        with caplog.at_level(logging.DEBUG):
            answers = [
                search(client, log, words),
                search(client, log, words, "1"),
                search(client, log, f"{words}\nmore"),
                search(client, log, words, "2"),
            ]
            records = list(caplog.records)

    assert [one.status_code for one in answers] == [200, 200, 422, 422]
    assert [one for one in records if words in one.getMessage() or one.exc_info] == []


def others_on(page: str) -> list[str]:
    """Each line a reader reads in a result's page's other results, before its search: the
    heading and each paragraph, each as its text."""
    if OTHERS_AT not in page:
        return []
    part = page[page.index(OTHERS_AT) : page.index(SEARCH_HEADING_AT)]
    return [
        " ".join(html.unescape(re.sub(r"<[^>]+>", "", found[2])).split())
        for found in re.finditer(r"<(p|h2)\b[^>]*>(.*?)</\1>", part, re.S)
    ]


def other_links(page: str) -> list[tuple[str, str]]:
    """Each other result's link: its address and its words."""
    if OTHERS_AT not in page:
        return []
    part = page[page.index(OTHERS_AT) : page.index(SEARCH_HEADING_AT)]
    return [
        (html.unescape(href), html.unescape(re.sub(r"<[^>]+>", "", words)))
        for href, words in re.findall(r'<a href="([^"]*)">(.*?)</a>', part)
    ]


def three_logs(client: TestClient) -> tuple[str, str, str]:
    """The log's homework, and a result of its title in this year's first and second terms
    and in last year's first term, in a class of the same name whose first month waits. The
    IDs of this year's first and second, then last year's."""
    store = store_of(client)
    store.put_on_record([homework(), *SET_ROWS], {})
    then = draft_of(IN_WEEK.replace(f"**{YEAR}**", "**2025-2026**"))
    review = store.review_grade_report(then, capture_key(then), key=KEY)
    saved = save_grade(
        store, then, key=KEY, review=review, answers=grade_answers(review, month=None)
    )
    assert isinstance(saved, GradeReportSaved)
    last_year = capture_class(store, then)
    class_id = with_report(client)
    with_report(client, SECOND_TERM)
    return (
        result_named(client, class_id, LOG),
        result_named(client, class_id, LOG, "T2"),
        result_named(client, last_year, LOG),
    )


def test_a_result_s_page_lists_each_other_result_of_its_homework_before_the_search() -> None:
    """The log's results in two terms of this year and in last year's class of the same name.
    The first term's page lists the other two after its homework rows and before the search,
    last year's first: each a link to its own page by its title, then its class name, its due
    line, and its term and year. The diagram's page, which shares no homework, has no list."""
    with browser() as client:
        first, second, then = three_logs(client)
        place = store_of(client).result_place(first)
        assert place is not None
        page = result_page(client, first)
        diagram = result_page(client, result_named(client, place.class_id, DIAGRAM))

    assert others_on(page) == [
        "Other results that could be the same homework:",
        LOG,
        BIOLOGY,
        "Due date shown: 08/20",
        "T1 · 2025-2026",
        LOG,
        BIOLOGY,
        "Due August 20",
        "T2 · 2026-2027",
    ]
    assert other_links(page) == [(way_in_to(then), LOG), (way_in_to(second), LOG)]
    for one in (then, second):
        item = f'<p class="jump"><a href="{way_in_to(one)}"><span class="authored-text">{LOG}'
        assert page.count(f"{item}</span></a></p>") == 1
    assert page.index(OTHERS_AT) < page.index(SEARCH_HEADING_AT)
    assert page.rindex("</section>", 0, page.index(OTHERS_AT)) > page.index('id="homework-1"')
    assert OTHERS_AT not in diagram
    assert "Other results" not in diagram


def test_the_other_results_stay_the_same_after_decisions_and_on_every_page_of_a_search() -> None:
    """The three logs' results: the first term's page before any decision, after its homework
    is linked to it and kept apart from the second term's, on a decision's 409, and on each
    page of a search that finds twenty-one rows, lists the same other results."""
    with browser() as client:
        first, second, then = three_logs(client)
        place = store_of(client).result_place(first)
        assert place is not None
        before = others_on(result_page(client, first))
        parent_links(client, place.class_id, first)
        parent_links(client, place.class_id, second, term="T2", decision="apart")
        after = others_on(result_page(client, first))
        stale = pressed(client, first, decision="apart", revision="1")
        pages = [search(client, first, "practice set", page) for page in ("", "2")]

    assert len(before) == 9
    assert after == before
    assert stale.status_code == 409
    assert others_on(stale.text) == before
    assert [one.status_code for one in pages] == [200, 200]
    assert [others_on(one.text) for one in pages] == [before, before]
    assert other_links(pages[1].text) == [(way_in_to(then), LOG), (way_in_to(second), LOG)]


RECORDS_UNREAD = "Blossom couldn't read the household's records."
RESULT_HOMEWORK_UNSHOWN = "This result's homework can't be shown right now."


def a_decision_s_page(
    answer: Answered, result_id: str, *, status: int, said: str, link: bool = True
) -> None:
    """The page a decision about a result's homework answers when the result's page can't be
    read: ``said`` and its status, then what can't be shown, "Try again" for the result's page
    unless ``said`` already says to try again, "Back to Grades", and no form."""
    assert answer.status_code == status, (answer.status_code, answer.text[:600])
    main = main_of(answer.text)
    (shown,) = re.findall(r"<h1>(.*?)</h1>", main, re.S)
    assert html.unescape(re.sub(r"<[^>]+>", "", shown)).replace("\xad", "").strip() == (
        "Homework for this result"
    )
    again = RESULT_PAGE.format(result_id=result_id)
    opening = '<p class="problem" role="alert" id="problem-summary" tabindex="-1" autofocus>'
    sentence = escape(f"{said} {RESULT_HOMEWORK_UNSHOWN}" if said != RECORDS_UNREAD else said)
    if link:
        assert f'{opening}{sentence} <a href="{again}">Try again</a></p>' in main, main[:900]
    else:
        assert f"{opening}{sentence}</p>" in main, main[:900]
        assert ">Try again</a>" not in main
    ways = re.findall(r'<p class="return"><a href="([^"]*)">([^<]*)</a></p>', main)
    assert ways == [("/parent/grades", "Back to Grades")]
    assert '<a href="/parent/grades" aria-current="page">Grades</a>' in answer.text
    assert "<form" not in main


def test_a_decision_whose_own_read_before_its_write_is_refused_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The press's read of the result, before its write, refused: the records sentence at
    503, nothing written, and the way to the result's page by a read."""
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        revision = revision_of(client, log)
        before = stored_links(client)
        monkeypatch.setattr(grades, "result_own", refusing_coded("SQLITE_BUSY"))
        answer = pressed(client, log, revision=revision)
        after = stored_links(client)

    a_decision_s_page(answer, log, status=503, said=RECORDS_UNREAD)
    assert after == before


def test_a_decision_whose_own_read_fails_another_way_is_still_the_server_s_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        revision = revision_of(client, log)
        monkeypatch.setattr(grades, "result_own", refusing(RuntimeError))
        with pytest.raises(RuntimeError):
            pressed(client, log, revision=revision)


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        ("no homework chosen", 422),
        ("an outcome unknown", 500),
        ("not saved", 500),
        ("links changed", 409),
    ],
)
def test_a_decision_s_answer_whose_page_can_t_be_read_keeps_its_sentence_and_status(
    monkeypatch: pytest.MonkeyPatch, outcome: str, status: int
) -> None:
    """Each designed answer of the press, its result's page then refused: the answer's own
    sentence and status, then what can't be shown."""
    raised: dict[str, type[Exception]] = {
        "an outcome unknown": GradeTransactionLost,
        "not saved": GradeReportNotSaved,
    }
    said = {
        "no homework chosen": grades.CHOOSE_HOMEWORK,
        "an outcome unknown": grades.PRESS_UNKNOWN,
        "not saved": grades.NOT_SAVED,
        "links changed": grades.LINKS_CHANGED,
    }[outcome]
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        class_id = with_report(client)
        log = result_named(client, class_id, LOG)
        revision = revision_of(client, log)
        original: Callable[..., object] = store.decide_homework_link

        def written(*args: object, **kwargs: object) -> object:
            monkeypatch.setattr(grades, "result_own", refusing_coded("SQLITE_BUSY"))
            if outcome in raised:
                msg = "synthetic"
                raise raised[outcome](msg)
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "decide_homework_link", written)
        if outcome == "no homework chosen":
            monkeypatch.setattr(grades, "result_own", refusing_coded("SQLITE_BUSY"))
            answer = pressed(client, log, homework_id="", revision=revision)
        elif outcome == "links changed":
            answer = pressed(client, log, revision=str(int(revision) + 1))
        else:
            answer = pressed(client, log, revision=revision)

    a_decision_s_page(answer, log, status=status, said=said, link="Try again." not in said)
