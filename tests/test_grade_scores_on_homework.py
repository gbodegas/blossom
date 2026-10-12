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
import html
import pathlib
import re
from collections.abc import Callable
from datetime import date

import pytest
from fastapi.testclient import TestClient

from blossom.grades.draft import GradeReportDraft, GradeValue, Presence, capture_key
from blossom.grades.identity import name_form_key
from blossom.grades.review import GradeReportSaved, MatchAnswer
from blossom.grades.text_reader import read_grade_report
from blossom.grades.wording import report_names, score_line
from blossom.hand_in import TURNED_IN
from blossom.noticing import canonical_active_input, planning_digest, read_everything, week_from
from blossom.reconciliation import SourceChannel
from blossom.routes.homework_scores import homework_of, read_grade_facts
from blossom.routes.navigation import NEW_NOTE_PAGE, NOTE_ACTIONS, note_add_action, note_add_href
from blossom.stores.gradebook import (
    GRADEBOOK_TABLES,
    UNCONNECTED,
    DeletePreview,
    class_for,
    name_key,
)
from blossom.stores.project_state import Assignment, AssignmentKind
from tests.support import (
    FIXTURES,
    PAGE_HEADERS,
    ZONE,
    as_stored,
    browser,
    capture_class,
    card_for,
    correction_page,
    database_of,
    every_row,
    form_fields,
    grade_answers,
    household_client,
    page_of,
    record,
    reported,
    save_grade,
    school_said,
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
) -> Assignment:
    return Assignment(
        assignment_id=assignment_id,
        course=course,
        title=title,
        due_date=on,
        dependencies=[],
        reported_submission_status="not_started",
        assigned_on=assigned,
        kind=AssignmentKind.HOMEWORK,
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
    with browser() as client:
        store_of(client).put_on_record([homework()], {})
        with_report(client)
        reported(store_of(client), "done", LOG_ID)
        page = page_of(client)

    assert len(scores(page)) == 1
    assert scores(card_for(page, LOG_ID)) != []


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
    pytest.param("| 18.0    | 20.0    |         |", "average left blank", id="S2 no average"),
    pytest.param("|         | 20.0    | 90.0    |", None, id="S4 no points"),
    pytest.param("| 18.0    |         | 90.0    |", "maximum left blank", id="S3 no maximum"),
    pytest.param("| 0.0     | 10.0    | 0.0     |", "0.0 / 10.0 · 0.0%", id="S9 a zero"),
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
    """S7: class details keeps the score with its notice; an incomplete copy, or a report of
    another term, withholds nothing; a later report that shows the result brings the line back,
    naming the report that supplied the score as class details does (S6)."""
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
    """S6: a newer current report shows the result with another score, and a parent leaves
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
    """S7's limit: a complete newer report leaves the result out and asks about another row.
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
