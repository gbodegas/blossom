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
from collections.abc import Callable
from datetime import date, time
from time import monotonic
from typing import Any

import pytest
from fastapi.testclient import TestClient

from blossom.grades.draft import GradeReportDraft, GradeValue, Presence, capture_key, folded
from blossom.grades.identity import name_form_key
from blossom.grades.projection import MadeCurrent, original_of
from blossom.grades.review import GradeReportSaved, GradeValueUndecodable, MatchAnswer
from blossom.grades.text_reader import read_grade_report
from blossom.grades.wording import report_names, score_line, spoken_day
from blossom.hand_in import NEEDS_HAND_IN, TURNED_IN
from blossom.noticing import canonical_active_input, planning_digest, read_everything, week_from
from blossom.plans import PlanBlock
from blossom.reconciliation import SourceChannel
from blossom.routes.homework_scores import homework_of, read_grade_facts, scores_shown
from blossom.routes.navigation import (
    NEW_NOTE_PAGE,
    NOTE_ACTIONS,
    assignment_anchor,
    note_add_action,
    note_add_href,
)
from blossom.routes.runs import plan_graphs
from blossom.stores.gradebook import (
    GRADEBOOK_TABLES,
    UNCONNECTED,
    DeletePreview,
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
    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})

        def planner_input() -> tuple[str, list[dict[str, object]]]:
            week = week_from(read_everything(store, store), MONDAY)
            return planning_digest(week), canonical_active_input(week)

        before = planner_input()
        with_report(client)
        shown = on_card(client)
        after = planner_input()

    assert [line for line, _, _ in shown] == [LINE]
    assert LOG_ID in str(before[1])
    assert after == before


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
    """A newer current report changes the score and holds no Due column, and a parent answers
    that each of its rows is the result on record. The result's own Due cell is then not
    captured, class details shows the date an earlier report gave, and her card shows the
    newer score under that date."""
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
    """Two school years hold a class of one name and a result due 08/20. With the earlier
    year starting in September and the later one in August both fall on the same day, so the
    homework due that day shows no score. With both starting in August the earlier year's
    falls a year before, and the later year's score shows."""
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
    """Her week, and the details of the assignment whose result holds the cell, of one whose
    own result decodes and of one with no result: each answers, shows no score at all, and
    reads as it does with no class on record. Each read records the failure's type alone, the
    file stays as it was, and once the cell decodes the lines are back."""
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


def test_an_undecodable_twin_is_never_left_out_of_the_count_to_let_the_other_show(
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
def test_each_stored_grade_value_that_can_t_be_decoded_raises_the_one_named_error(
    statement: str, value: str, cause: type, at: str
) -> None:
    """A value the gradebook decodes raises it from each read of the class and term, a homework
    page's grade read included; a report's moment raises it where the report is named. Its
    cause is the decoding's own error, when one was raised, and its message holds nothing stored."""
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
def test_a_malformed_moment_or_copied_list_leaves_her_pages_as_with_no_class_on_record(
    statement: str, value: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Her week, and the details of two assignments with a result and of one with none: each
    answers, shows no score at all, and reads as it does with no class on record. Each read
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
def test_a_malformed_value_in_a_term_no_card_shows_leaves_the_whole_page_without_scores(
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
def test_grades_and_class_details_answer_a_malformed_moment_or_copied_list_with_the_named_error(
    statement: str, value: str, address: str
) -> None:
    """Neither page leaves anything out: each fails with the named error, a ``ValueError``,
    with nothing stored in its message."""
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
def test_no_other_error_is_taken_for_a_grade_value_that_can_t_be_decoded(
    where: str, address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``ValueError`` of any other kind, from the grade read or from making the lines, fails
    the page as it does with no score to leave out."""

    def failed(*_: object, **__: object) -> None:
        msg = "another failure"
        raise ValueError(msg)

    with browser() as client:
        store = store_of(client)
        store.put_on_record([homework()], {})
        with_report(client)
        if where == "the read":
            monkeypatch.setattr(store, "class_term", failed)
        else:
            monkeypatch.setattr("blossom.routes.homework_scores.school_scores", failed)
        with pytest.raises(ValueError, match="another failure") as raised:
            client.get(address, headers=PAGE_HEADERS)

    assert type(raised.value) is ValueError


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
    """One stored value written as no save writes it: the statement, what it binds, which
    reads raise the named error for it, the error its decoding raised, when one was, and
    whether it makes a chain of sources come back to itself."""

    statement: str
    binds: tuple[object, ...] = ()
    at: str = "the class"
    cause: type = NO_CAUSE
    loops: bool = False


def not_utf8(table: str, column: str) -> Edit:
    """Bytes that are no UTF-8, stored as the text of ``column`` in every row of ``table``."""
    statement = f"UPDATE {table} SET {column} = CAST(? AS TEXT)"  # noqa: S608
    return Edit(statement, (NOT_UTF8,), "the score read", UnicodeDecodeError)


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
}
"""Stored values no save writes, each with the file's own checks in force: a report's order
or number of rows that is no whole number at or above its bound, a copied list nested too
deeply, a cell's text that is not text, a first month that is no whole month, text that is
not UTF-8, an action whose chain of sources comes back to itself, and a report whose term
has no row among its year's terms."""
EDITED = [pytest.param(edit, id=name) for name, edit in EDITS.items()]


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
def test_a_hand_edited_grade_value_leaves_her_pages_as_with_no_class_on_record(
    edit: Edit, caplog: pytest.LogCaptureFixture
) -> None:
    """Her week, and the details of two assignments with a result and of one with none: each
    answers in time, shows no score at all, and reads as it does with no class on record.
    Each read records the failure's type alone, the gradebook's rows stay as they were, and
    the connection keeps its own decoding of text."""
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
def test_each_hand_edited_grade_value_raises_the_one_named_error_where_it_is_read(
    edit: Edit,
) -> None:
    """A report's order or rows, a cell's text, a copied list and a chain of sources raise it
    from each read of the class and term; a first month from its own read; text that is not
    UTF-8 and a report outside its year's terms from a homework page's grade read. Each read
    returns in time, its cause is the decoding's own error, when one was raised, and its
    message holds nothing stored."""
    draft = draft_of(IN_WEEK)
    with browser() as client:
        store = store_of(client)
        class_id = made_current_again(client)
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
def test_grades_and_class_details_answer_a_hand_edited_value_with_the_named_error(
    edit: Edit, address: str
) -> None:
    """Neither page leaves anything out: each fails in time with the named error, a
    ``ValueError``, with nothing stored in its message."""
    with browser() as client:
        class_id = made_current_again(client)
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
]


@pytest.mark.parametrize(("arrange", "statement"), COMPETITORS)
def test_a_hand_edited_competitor_is_never_read_past_to_let_the_other_show(
    arrange: Callable[[TestClient], None], statement: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Two results could each be the log's, so the log shows no score. With one of them
    stored so it would drop out of the count, the log still shows none, on her week and on
    its details, and each read records the failure."""
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
