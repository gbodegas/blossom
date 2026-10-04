# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Her week offers Done and Not yet on each card she has not reported done, and shows the
whole form only when a response or Change is about it.

On an ordinary visit a card with no update has a Done and a Not yet button, each of which
saves that report through the update route, with an optional note folded beside them; a
card left Not yet has Done beside its Change and Undo, and Done keeps her note. A card's
facts, its links, its hand-in line and any line a save or a refusal left stay outside these
forms. An address that asks to change a card, a refusal of her update, and a save refused
because the card changed elsewhere show that card's whole form on the server, with what she
chose and typed. The details keep their form, and a parent reads no form and no button.

The fixture week through the app, a pinned day, synthetic words, and forms read from the
pages' own HTML. No model is asked.
"""

import pathlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Final

import pytest
from fastapi.testclient import TestClient

from blossom.reconciliation import SourceChannel
from blossom.routes.navigation import assignment_anchor
from blossom.school_instructions import InstructionChoice, InstructionSeen
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    DETAILS,
    ESCAPED,
    ESSAY,
    ESSAY_ID,
    ESSAY_TITLE,
    FIXTURE_WEEK,
    HER_PAGE,
    LATER_WEEK,
    NONE_APPLIES,
    NOW,
    PAGE_HEADERS,
    PLAN_DATE,
    QUIZ_ID,
    READING_LOG_ID,
    REPORT,
    SYLLABUS_ID,
    UNDO,
    Answer,
    after,
    already_undone,
    bad_return,
    browser,
    card_for,
    conflict,
    control_names,
    due,
    failed_undo,
    failed_write,
    form_fields,
    lands_on,
    main_of,
    malformed,
    no_choice,
    not_this_cards,
    note_too_long,
    page_of,
    reading,
    refuse_writes,
    reported,
    rules_named,
    save,
    saved,
    saved_again,
    school_missing,
    stale_undo,
    store_of,
    undone,
    week_card,
    whole_form,
    words,
)

UNREPORTED: Final = (
    "assignment-science-fair-proposal",
    "assignment-textbook-cover",
    ESSAY_ID,
    QUIZ_ID,
    SYLLABUS_ID,
    "assignment-algebra-set",
    READING_LOG_ID,
)
"""Every assignment of the fixture week with no update of hers, in page order: five cards,
then two rows due later."""
DONE_MEANS: Final = (
    '<p class="note" id="done-means">Done means you have finished your part. It does not turn '
    "work in.</p>"
)
KEPT_NOTE: Final = "Ask about question 3\nand the map"
APPLIES: Final = "Name the three canals the chapter compares."
EARLIER: Final = "Bring a ruler for the timeline."
WAITING: Final = "Answer in full sentences."
LONG_INSTRUCTION: Final = (
    "Read every page of the chapter and answer each question in full. " * 500
).strip()


# ------------------------------------------------------------------ the record


def her_reports(store: ProjectStateStore) -> list[tuple[object, ...]]:
    """Every update of hers the record holds, in the order kept."""
    rows: list[tuple[object, ...]] = store._connection.execute(
        "SELECT * FROM student_reports ORDER BY rowid"
    ).fetchall()
    return rows


# ------------------------------------------------------------------ reading a page


@dataclass(frozen=True)
class Form:
    """One update form on her week: the card or row it sits in, and the form whole."""

    owner: str
    html: str


WHOLE_FORM: Final = re.compile(r'<form method="post" action="[^"]*/report" class="report">')
QUICK_FORM: Final = re.compile(r'<form method="post" action="[^"]*/report" class="quick">')
ITEM: Final = re.compile(r'<(?:article|li)\b[^>]*\sid="(assignment-[^"]*)"')
OWNER: Final = re.compile(r'(?<![\w-])id="(assignment-[^"]*)"')
QUICK_BUTTON: Final = re.compile(
    r'<button type="submit" name="status" value="(done|not_yet)" class="(?:primary|secondary)"'
    r' aria-label="([^"]*)"(?: aria-describedby="done-means")?>'
)
"""A button that saves one report on its own. Done is described by the page's one line that
says what Done means."""
QUICK_ONLY: Final = re.compile(
    r'<form method="post" action="[^"]*/report" class="quick">\s*'
    r'(?:<details class="steps note-fold">.*</details>\s*)?'
    r'(?:<input type="hidden" name="[^"]+" value="[^"]*">\s*)+'
    r'(?:<div class="actions">\s*(?:<button [^>]*>[^<]*</button>\s*){2}</div>'
    r"|<button [^>]*>[^<]*</button>)\s*</form>",
    re.S,
)


def closing(page: str, start: int) -> int:
    """Where the fold that opens at ``start`` ends: the start of its own ``</details>``."""
    depth = 0
    for tag in re.finditer(r"<details\b|</details>", page[start:]):
        depth += 1 if tag.group(0) == "<details" else -1
        if depth == 0:
            return start + tag.start()
    raise AssertionError(page[start : start + 80])


def forms_of(page: str, pattern: re.Pattern[str]) -> list[Form]:
    """Every update form of one kind on the page, in order, each with its owner."""
    return [
        Form(
            OWNER.findall(page, 0, start.start())[-1],
            page[start.start() : page.index("</form>", start.start()) + len("</form>")],
        )
        for start in pattern.finditer(page)
    ]


def whole_forms(page: str) -> list[str]:
    """The card or row of each whole update form on the page: the one with Done and Not yet
    as a choice, a note, and Save update."""
    return [form.owner for form in forms_of(page, WHOLE_FORM)]


def quick_forms(page: str) -> list[Form]:
    """Each form of Done and Not yet buttons on the page."""
    return forms_of(page, QUICK_FORM)


def quick_buttons(html: str) -> list[tuple[str, str]]:
    """Each Done or Not yet button that saves on its own, as (the status it saves, its
    label)."""
    return QUICK_BUTTON.findall(html)


def in_a_fold(html: str, at: int) -> bool:
    """Whether the place ``at`` is inside a disclosure that is not open."""
    depth = 0
    for tag in re.finditer(r"<details\b(?![^>]*\sopen)[^>]*>|</details>", html[:at]):
        depth += -1 if tag.group(0) == "</details>" else 1
    return depth > 0


def items(page: str) -> dict[str, str]:
    """Each card and row due later on the page, by its id: from its opening tag to the next
    one's."""
    places = [(found.start(), found.group(1)) for found in ITEM.finditer(page)]
    ends = [start for start, _ in places[1:]] + [len(page)]
    return {name: page[start:end] for (start, name), end in zip(places, ends, strict=True)}


def without_the_form(item: str) -> str:
    """A card or row with its update form taken out, whichever kind it has, or as it is when
    it has none."""
    for form in forms_of(item, WHOLE_FORM) + forms_of(item, QUICK_FORM):
        item = item.replace(form.html, "")
    return item


def autofocused(page: str) -> list[str]:
    """The opening tag of each element the page asks to take the focus as it arrives."""
    return re.findall(r"<[a-zA-Z][^<>]*\sautofocus(?=[\s>])[^<>]*>", page)


def check_the_cards(page: str) -> None:
    """No card or row folds its form. One with no update shows its whole form or its Done and
    Not yet buttons, never both; one left Not yet has Done beside Change; one reported done
    has neither; each set of buttons holds a folded note and hidden fields alone, outside any
    closed fold; and at most one element asks for the focus."""
    assert "update-fold" not in page
    for name, item in items(page).items():
        whole, quick = forms_of(item, WHOLE_FORM), forms_of(item, QUICK_FORM)
        standing = re.search(r'<span class="pill">Your update: (Done|Not yet)</span>', item)
        assert len(whole) + len(quick) <= 1, name
        if whole or 'class="actions"' not in item:
            # The whole form is shown, or the reader has nothing to press here.
            assert quick_buttons(item) == [], name
            continue
        expected = (
            ["done", "not_yet"] if not standing else ["done"] if standing[1] == "Not yet" else []
        )
        assert [status for status, _ in quick_buttons(item)] == expected, name
        for form in quick:
            assert form.owner == name
            assert QUICK_ONLY.fullmatch(form.html), (name, form.html[:400])
            assert not in_a_fold(item, item.index(form.html)), name
    assert len(autofocused(page)) <= 1, autofocused(page)


# ------------------------------------------------------------------ the responses


def then_shown(press: Callable[[TestClient], Answer]) -> Callable[[TestClient], Answer]:
    """A press answered by a redirect, as the page the redirect opens."""
    return lambda client: after(client, press(client))


def arrival(client: TestClient) -> Answer:
    return client.get(HER_PAGE, headers=PAGE_HEADERS)


def shown(client: TestClient) -> Answer:
    return client.get(HER_PAGE, params={"show": ESSAY_ID}, headers=PAGE_HEADERS)


def followed(client: TestClient, page: str, pattern: str) -> Answer:
    """The page a link on ``page`` opens, the first whose opening tag matches ``pattern``."""
    link = re.search(pattern, page, re.S)
    assert link is not None, pattern
    return client.get(link.group(1).replace("&amp;", "&"), headers=PAGE_HEADERS)


def to_check(client: TestClient) -> Answer:
    reported(store_of(client), "done", QUIZ_ID)
    store_of(client).record_status_reports(QUIZ_ID, [school_missing(PLAN_DATE)])
    return followed(
        client,
        page_of(client),
        r'<p class="confidence disagree to-check" role="status">.*?<a href="([^"]+)"',
    )


def kept_as_it_is(client: TestClient) -> Answer:
    reported(store_of(client), "not_yet")
    return followed(client, week_card(client), r'<a class="cancel" href="([^"]+)"')


def asks_to_change(assignment_id: str, **params: str) -> Callable[[TestClient], Answer]:
    def asked(client: TestClient) -> Answer:
        return client.get(
            HER_PAGE, params={"change": assignment_id, **params}, headers=PAGE_HEADERS
        )

    return asked


def edit_saved(client: TestClient) -> Answer:
    reported(store_of(client), "not_yet")
    return asks_to_change(ESSAY_ID)(client)


def conflict_undone(client: TestClient) -> Answer:
    store = store_of(client)
    reported(store, "not_yet")
    card = week_card(client)
    head = store.student_reports(ESSAY_ID)[-1].report_id
    store.undo_report(ESSAY_ID, head, now=NOW, today=PLAN_DATE)
    return save(client, card, "done", "Mine.")


# case: (make the response, its status, the assignment whose whole form it shows, or none)
RESPONSES: Final[dict[str, tuple[Callable[[TestClient], Answer], int, str | None]]] = {
    "arrival": (arrival, 200, None),
    "show=": (shown, 200, None),
    "a school report to check": (to_check, 200, None),
    "Keep it as it is": (kept_as_it_is, 200, None),
    "edit intent, a card": (asks_to_change(ESSAY_ID), 200, ESSAY_ID),
    "edit intent, a row due later": (asks_to_change(READING_LOG_ID), 200, READING_LOG_ID),
    "edit intent, a card shown apart": (asks_to_change(ESSAY_ID, week=LATER_WEEK), 200, ESSAY_ID),
    "edit intent, a card with an update": (edit_saved, 200, ESSAY_ID),
    "no choice": (no_choice, 422, ESSAY_ID),
    "note too long": (note_too_long, 422, ESSAY_ID),
    "form not whole": (malformed, 422, ESSAY_ID),
    "bad return": (bad_return, 422, ESSAY_ID),
    "not this card's": (not_this_cards, 422, ESSAY_ID),
    "failed write": (failed_write, 500, ESSAY_ID),
    "conflict, saved elsewhere": (conflict, 409, ESSAY_ID),
    "conflict, undone elsewhere": (conflict_undone, 409, ESSAY_ID),
    "a save": (then_shown(saved), 200, None),
    "the same save again": (then_shown(saved_again), 200, None),
    "an undo to no update": (then_shown(undone), 200, None),
    "a stale undo": (stale_undo, 409, None),
    "an undo already made": (already_undone, 409, None),
    "a failed undo": (failed_undo, 500, None),
}


# ------------------------------------------------------------------ which form a response shows


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
@pytest.mark.parametrize("case", list(RESPONSES))
def test_only_the_card_a_response_is_about_shows_its_whole_form(
    case: str, reader: str, tmp_path: pathlib.Path
) -> None:
    make, status, about = RESPONSES[case]
    with reading(reader, tmp_path) as client:
        answer = make(client)
    page = main_of(answer.text)

    assert answer.status_code == status, answer.text[:300]
    assert whole_forms(page) == ([] if about is None else [assignment_anchor(about)])
    assert quick_forms(page)
    check_the_cards(page)


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
def test_an_ordinary_visit_offers_done_and_not_yet_on_every_card_with_no_update(
    reader: str, tmp_path: pathlib.Path
) -> None:
    """Each button is outside any fold, names its assignment, and is described by the one
    line that says what Done means; the note waits folded in the same form."""
    with reading(reader, tmp_path) as client:
        page = main_of(page_of(client))

    quick = quick_forms(page)
    assert [form.owner for form in quick] == [assignment_anchor(name) for name in UNREPORTED]
    assert whole_forms(page) == []
    assert quick_buttons(quick[2].html) == [
        ("done", f"Done: {ESSAY_TITLE}, {ESSAY.course}"),
        ("not_yet", f"Not yet: {ESSAY_TITLE}, {ESSAY.course}"),
    ]
    assert '<details class="steps note-fold">' in quick[2].html
    assert 'type="radio"' not in "".join(form.html for form in quick)
    assert page.count(DONE_MEANS) == 1
    assert not any("Done means" in card_for(page, name) for name in UNREPORTED[:5])
    assert autofocused(page) == []
    check_the_cards(page)


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
def test_a_refused_form_opens_with_what_she_chose_and_typed(
    reader: str, tmp_path: pathlib.Path
) -> None:
    """A refusal about a field puts the focus on it inside the open fold; one about the
    form is said on the card's line, outside the fold, which takes the focus."""
    with reading(reader, tmp_path) as client:
        unchosen = main_of(no_choice(client).text)
        general = main_of(failed_write(client).text)
    radio = '<input type="radio" name="status" value="done" autofocus>'
    line = (
        f'<p class="problem" role="alert" id="update-problem-{ESSAY_ID}" tabindex="-1" autofocus>'
    )

    (form,) = forms_of(unchosen, WHOLE_FORM)
    assert form.owner == assignment_anchor(ESSAY_ID)
    assert radio in form.html
    assert ">kept</textarea>" in form.html
    assert autofocused(unchosen) == [radio]
    (form,) = forms_of(general, WHOLE_FORM)
    assert 'value="not_yet" checked>' in form.html
    assert ">kept</textarea>" in form.html
    assert autofocused(general) == [line]
    assert line in without_the_form(card_for(general, ESSAY_ID))
    assert "update-result" not in card_for(general, ESSAY_ID)


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
def test_a_save_refused_after_an_undo_elsewhere_shows_the_form_under_her_unsaved_update(
    reader: str, tmp_path: pathlib.Path
) -> None:
    with reading(reader, tmp_path) as client:
        answer = conflict_undone(client)
    card = card_for(main_of(answer.text), ESSAY_ID)

    (form,) = forms_of(card, WHOLE_FORM)
    assert re.search(
        r'<h3 class="update-heading">Your unsaved update</h3>\s*<form method="post"', card
    )
    assert 'value="done" checked>' in form.html
    assert ">Mine.</textarea>" in form.html
    assert quick_forms(card) == []
    assert '<p class="update-summary">' not in card
    assert f'id="update-problem-{ESSAY_ID}"' in without_the_form(card)


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
def test_the_lines_a_save_or_an_undo_leaves_stay_outside_the_buttons_it_offers_again(
    reader: str, tmp_path: pathlib.Path
) -> None:
    with reading(reader, tmp_path) as client:
        restored = card_for(main_of(after(client, undone(client)).text), ESSAY_ID)
        stale = card_for(main_of(already_undone(client).text), ESSAY_ID)

    for card, line in (
        (restored, f'<p class="note update-result" role="status" id="update-result-{ESSAY_ID}"'),
        (stale, f'<p class="problem" role="alert" id="update-problem-{ESSAY_ID}"'),
    ):
        (form,) = quick_forms(card)
        assert [status for status, _ in quick_buttons(form.html)] == ["done", "not_yet"]
        assert line in without_the_form(card)
        assert line not in form.html


# ------------------------------------------------------------------ what stays outside the form


def a_crowded_week(client: TestClient) -> None:
    store = store_of(client)
    store.put_on_record(
        [due(f"crowd-{n}", f"Crowd set {n}", date(2026, 8, 20 + n % 3)) for n in range(14)], {}
    )
    reported(store, "done", "assignment-science-fair-proposal")


def every_card_done(client: TestClient) -> None:
    for item in store_of(client).all_assignments():
        reported(store_of(client), "done", item.assignment_id)


def waiting_for_review(store: ProjectStateStore, assignment_id: str, text: str) -> None:
    """A school note kept waiting for a parent's review, as the startup rule keeps one found
    in the old note field."""
    held = store.school_instruction_readings([assignment_id]).readable[assignment_id]
    with store._lock, store._writing():
        store._insert_instruction_locked(
            assignment_id,
            InstructionSeen(text, SourceChannel.EMAIL),
            "awaiting",
            held.revision + 1,
            imported_by=None,
            settled_by=None,
            now=None,
            today=None,
        )


def settled(
    store: ProjectStateStore, assignment_id: str, seen: list[str], choice: InstructionChoice | None
) -> None:
    store.settle_school_instructions(
        assignment_id,
        [InstructionSeen(text, SourceChannel.LMS) for text in seen],
        choice,
        authored_by="parent",
        now=NOW,
        today=PLAN_DATE,
    )


def instructions(client: TestClient) -> None:
    """The essay with a long instruction and a short one that apply, an earlier one and one
    waiting for review; the quiz with one that applies; the syllabus with an earlier one
    alone."""
    store = store_of(client)
    for name, first in ((ESSAY_ID, LONG_INSTRUCTION), (QUIZ_ID, APPLIES), (SYLLABUS_ID, EARLIER)):
        settled(store, name, [first], None)
    revisions = store.school_instruction_readings([ESSAY_ID, SYLLABUS_ID]).readable
    shown_ = (LONG_INSTRUCTION, APPLIES, EARLIER)
    applies = frozenset({LONG_INSTRUCTION, APPLIES})
    choice = InstructionChoice(revisions[ESSAY_ID].revision, shown_, applies)
    settled(store, ESSAY_ID, [APPLIES, EARLIER], choice)
    waiting_for_review(store, ESSAY_ID, WAITING)
    none = InstructionChoice(revisions[SYLLABUS_ID].revision, (EARLIER,), frozenset(), True)
    settled(store, SYLLABUS_ID, [], none)


# week: (arrange it, how many cards and rows offer Done and Not yet)
WEEKS: Final[dict[str, tuple[Callable[[TestClient], None], int]]] = {
    "quiet": (lambda client: None, 7),
    "crowded": (a_crowded_week, 20),
    "all done": (every_card_done, 0),
    "long, several and waiting instructions": (instructions, 7),
}


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
@pytest.mark.parametrize("week", list(WEEKS))
def test_a_cards_facts_links_and_lines_stay_outside_its_buttons(
    week: str, reader: str, tmp_path: pathlib.Path
) -> None:
    """The form of Done and Not yet holds its folded note and the buttons alone: the title,
    the course, the due line, Details, Read instructions, the hand-in line and the school's
    words that apply are outside it."""
    arrange, count = WEEKS[week]
    with reading(reader, tmp_path) as client:
        arrange(client)
        page = main_of(page_of(client))
        edited = main_of(page_of(client, change=ESSAY_ID))
        records = {
            assignment_anchor(item.assignment_id): item
            for item in store_of(client).all_assignments()
        }

    check_the_cards(page)
    check_the_cards(edited)
    offered = {name: item for name, item in items(page).items() if quick_forms(item)}
    assert len(offered) == count
    for name, item in offered.items():
        outside, record = without_the_form(item), records[name]
        assert re.search(rf"<(h2|strong)>{re.escape(record.title)}</\1>", outside), name
        assert record.course in outside
        assert re.search(r'<p class="due">|<span class="due">Due \w+day, ', outside), name
        assert 'aria-label="Details: ' in outside
        assert '<p class="note hand-in-line">' in outside
    if week.startswith("long"):
        essay = without_the_form(items(page)[assignment_anchor(ESSAY_ID)])
        assert 'aria-label="Read instructions: ' in essay
        assert f'From the school: <q class="authored-text">{LONG_INSTRUCTION}</q>' in essay
        assert f'From the school: <q class="authored-text">{APPLIES}</q>' in essay
        assert '<details class="earlier-instructions">' in essay


# ------------------------------------------------------------------ the other folds, apart


def reported_done(page: str) -> list[tuple[bool, list[str]]]:
    """Each Reported done fold: whether it arrives open, and the cards or rows inside it."""
    return [
        (bool(fold.group(1)), ITEM.findall(page[fold.start() : closing(page, fold.start())]))
        for fold in re.finditer(r'<details class="steps reported-done"( open)?>', page)
    ]


def done_result(client: TestClient) -> Answer:
    return after(client, save(client, week_card(client, QUIZ_ID), "done", "", QUIZ_ID))


def done_problem(client: TestClient) -> Answer:
    card = week_card(client, QUIZ_ID)
    reported(store_of(client), "not_yet", QUIZ_ID)
    reported(store_of(client), "done", QUIZ_ID)
    return save(client, card, "done", "Mine.", QUIZ_ID)


# case: (make the response, whether it names the card in the first Reported done fold)
DONE_RESPONSES: Final[dict[str, tuple[Callable[[TestClient], Answer], bool]]] = {
    "arrival": (arrival, False),
    "show=": (lambda client: client.get(HER_PAGE, params={"show": QUIZ_ID}), True),
    "change=": (asks_to_change(QUIZ_ID), True),
    "a school report to check": (to_check, True),
    "a result": (done_result, True),
    "a problem": (done_problem, True),
}


@pytest.mark.parametrize("case", list(DONE_RESPONSES))
def test_reported_done_holds_the_done_work_and_opens_for_what_a_response_names(
    case: str,
) -> None:
    make, names_it = DONE_RESPONSES[case]
    with browser() as client:
        reported(store_of(client), "done", QUIZ_ID)
        reported(store_of(client), "done", READING_LOG_ID)
        page = main_of(make(client).text)

    assert reported_done(page) == [
        (names_it, [assignment_anchor(QUIZ_ID)]),
        (False, [assignment_anchor(READING_LOG_ID)]),
    ]
    check_the_cards(page)


def earlier_fold(item: str) -> str:
    start = item.index('<details class="earlier-instructions">')
    return item[start : closing(item, start) + len("</details>")]


EARLIER_RESPONSES: Final[dict[str, Callable[[TestClient], Answer]]] = {
    "arrival": arrival,
    "edit intent": asks_to_change(ESSAY_ID),
    "a refusal": no_choice,
    "a conflict": conflict,
    "a save": then_shown(saved),
}


@pytest.mark.parametrize("case", list(EARLIER_RESPONSES))
def test_earlier_instructions_hold_only_the_schools_earlier_and_waiting_words(
    case: str,
) -> None:
    with browser() as client:
        instructions(client)
        page = main_of(EARLIER_RESPONSES[case](client).text)
    essay, syllabus = card_for(page, ESSAY_ID), card_for(page, SYLLABUS_ID)

    assert " ".join(earlier_fold(essay).split()) == (
        '<details class="earlier-instructions"> <summary>Instructions from the school waiting '
        f'for review, and earlier ones</summary> <ul> <li><q class="authored-text">{WAITING}</q> '
        '<span class="source">Waiting for a parent\'s review; it does not apply until '
        f'then.</span></li> <li><q class="authored-text">{EARLIER}</q> <span class="source">'
        "Earlier; does not apply now.</span></li> </ul> </details>"
    )
    assert words(earlier_fold(syllabus)) == (
        f"Earlier instructions from the school {EARLIER} Earlier; does not apply now."
    )
    assert APPLIES in essay.replace(earlier_fold(essay), "")
    assert NONE_APPLIES in syllabus.replace(earlier_fold(syllabus), "")
    assert '<details class="earlier-instructions" open' not in page


# ------------------------------------------------------------------ a card with an update


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
def test_a_card_left_not_yet_keeps_its_summary_change_and_undo_and_offers_done(
    reader: str, tmp_path: pathlib.Path
) -> None:
    with reading(reader, tmp_path) as client:
        store = store_of(client)
        reported(store, "not_yet")
        card = card_for(page_of(client), ESSAY_ID)
        changing = card_for(page_of(client, week=FIXTURE_WEEK, change=ESSAY_ID), ESSAY_ID)
        back = card_for(
            followed(client, changing, r'<a class="cancel" href="([^"]+)"').text, ESSAY_ID
        )
        undone_ = client.post(UNDO, data=form_fields(card, UNDO), headers=PAGE_HEADERS)
        left = store.student_reports(ESSAY_ID)

    for each in (card, changing, back):
        assert "update-fold" not in each
        assert '<span class="pill">Your update: Not yet</span>' in each
    assert ">Change<span" in card
    assert [status for status, _ in quick_buttons(card)] == ["done"]
    assert [status for status, _ in quick_buttons(back)] == ["done"]
    assert quick_buttons(changing) == []
    assert ">Change<span" not in changing
    assert 'value="not_yet" checked>' in changing
    assert ">Keep it as it is</a>" in changing
    assert ">Change<span" in back
    assert undone_.status_code == 303
    assert [event.operation for event in left] == ["report", "undo"]


# ------------------------------------------------------------------ Done and Not yet on the card


def pressed(client: TestClient, card: str, status: str, **typed: str) -> Answer:
    """One of the card's buttons pressed, sending what the card's form holds as a browser
    would, the pressed button's status among it, and any note she typed in the fold."""
    return client.post(
        REPORT, data={**whole_form(card, REPORT), "status": status, **typed}, headers=PAGE_HEADERS
    )


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
@pytest.mark.parametrize(("status", "said"), [("done", "Done"), ("not_yet", "Not yet")])
def test_one_press_on_a_card_saves_that_report_through_the_update_route(
    status: str, said: str, reader: str, tmp_path: pathlib.Path
) -> None:
    with reading(reader, tmp_path) as client:
        card = card_for(main_of(page_of(client)), ESSAY_ID)
        answer = pressed(client, card, status)
        shown = after(client, answer)
        events = store_of(client).student_reports(ESSAY_ID)

    assert answer.status_code == 303, answer.text[:300]
    assert [(event.operation, event.status, event.note) for event in events] == [
        ("report", status, None)
    ]
    assert f'<span class="pill">Your update: {said}</span>' in card_for(shown.text, ESSAY_ID)


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
def test_a_note_typed_in_the_fold_is_saved_with_the_button_pressed(
    reader: str, tmp_path: pathlib.Path
) -> None:
    with reading(reader, tmp_path) as client:
        card = card_for(main_of(page_of(client)), ESSAY_ID)
        answer = pressed(client, card, "not_yet", note="Halfway through the outline")
        events = store_of(client).student_reports(ESSAY_ID)

    assert answer.status_code == 303, answer.text[:300]
    assert [(event.status, event.note) for event in events] == [
        ("not_yet", "Halfway through the outline")
    ]


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
def test_done_on_a_card_left_not_yet_keeps_her_note(reader: str, tmp_path: pathlib.Path) -> None:
    """The note stands until she changes or clears it through Change; one press of Done
    carries it as it stands."""
    with reading(reader, tmp_path) as client:
        store = store_of(client)
        store.report_status(
            ESSAY_ID, "not_yet", KEPT_NOTE, expected_head=None, now=NOW, today=PLAN_DATE
        )
        card = card_for(main_of(page_of(client)), ESSAY_ID)
        answer = pressed(client, card, "done")
        events = store.student_reports(ESSAY_ID)

    assert quick_buttons(card) == [("done", f"Done: {ESSAY_TITLE}, {ESSAY.course}")]
    assert answer.status_code == 303, answer.text[:300]
    assert [(event.status, event.note) for event in events] == [
        ("not_yet", KEPT_NOTE),
        ("done", KEPT_NOTE),
    ]


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
def test_a_second_press_of_the_same_button_saves_once(reader: str, tmp_path: pathlib.Path) -> None:
    with reading(reader, tmp_path) as client:
        card = card_for(main_of(page_of(client)), ESSAY_ID)
        first = pressed(client, card, "done")
        second = pressed(client, card, "done")
        events = store_of(client).student_reports(ESSAY_ID)

    assert (first.status_code, second.status_code) == (303, 303)
    assert [event.status for event in events] == ["done"]


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
def test_a_press_on_a_card_another_device_has_updated_is_refused_with_her_choice_kept(
    reader: str, tmp_path: pathlib.Path
) -> None:
    with reading(reader, tmp_path) as client:
        card = card_for(main_of(page_of(client)), ESSAY_ID)
        reported(store_of(client), "not_yet")
        answer = pressed(client, card, "done")
        events = store_of(client).student_reports(ESSAY_ID)
    shown = card_for(main_of(answer.text), ESSAY_ID)

    assert answer.status_code == 409, answer.text[:300]
    assert [event.status for event in events] == ["not_yet"]
    assert '<h3 class="update-heading">Your unsaved update</h3>' in shown
    (form,) = forms_of(shown, WHOLE_FORM)
    assert 'value="done" checked>' in form.html
    assert "update-result" not in shown


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
def test_a_press_the_file_refuses_never_reads_as_saved(reader: str, tmp_path: pathlib.Path) -> None:
    with reading(reader, tmp_path) as client:
        card = card_for(main_of(page_of(client)), ESSAY_ID)
        refuse_writes(client)
        answer = pressed(client, card, "done", note="kept")
        events = store_of(client).student_reports(ESSAY_ID)
    page = main_of(answer.text)
    shown = card_for(page, ESSAY_ID)

    assert answer.status_code == 500, answer.text[:300]
    assert events == []
    assert "update-result" not in page
    assert "Your update: Done" not in shown
    assert f'<p class="problem" role="alert" id="update-problem-{ESSAY_ID}"' in shown
    (form,) = forms_of(shown, WHOLE_FORM)
    assert 'value="done" checked>' in form.html
    assert ">kept</textarea>" in form.html


def test_a_card_reported_done_offers_change_and_undo_and_no_button() -> None:
    with browser() as client:
        reported(store_of(client), "done")
        card = card_for(main_of(page_of(client, show=ESSAY_ID)), ESSAY_ID)

    assert quick_buttons(card) == []
    assert 'class="quick"' not in card
    assert ">Change<span" in card
    assert ">Undo<span" in card


def test_every_button_names_its_assignment_and_the_page_says_what_done_means_once() -> None:
    with browser() as client:
        page = main_of(page_of(client))
    named = [name for shown, name in control_names(page) if shown in ("Done", "Not yet")]

    assert len(named) == 2 * len(UNREPORTED)
    assert f"Done: {ESSAY_TITLE}, {ESSAY.course}" in named
    assert f"Not yet: {ESSAY_TITLE}, {ESSAY.course}" in named
    assert len(set(named)) == len(named)
    assert page.count(DONE_MEANS) == 1
    assert page.count('value="done" class="primary"') == len(UNREPORTED)
    assert page.count('aria-describedby="done-means"') == len(UNREPORTED)


# ------------------------------------------------------------------ edit intent


LONGEST: Final = "a" * 200
TOO_LONG: Final = "b" * 201


@pytest.mark.parametrize(
    ("asked", "about"),
    [
        ({"change": LONGEST}, LONGEST),
        ({"change": TOO_LONG}, None),
        ({"change": "assignment-nowhere"}, None),
        ({"change": ""}, None),
        ({"change": ESSAY_ID, "week": LATER_WEEK}, ESSAY_ID),
    ],
)
def test_edit_intent_shows_one_whole_form_for_an_id_of_up_to_two_hundred_characters(
    asked: dict[str, str], about: str | None
) -> None:
    with browser() as client:
        store = store_of(client)
        store.put_on_record(
            [
                due(LONGEST, "Longest id", date(2026, 8, 20)),
                due(TOO_LONG, "Too long", date(2026, 8, 20)),
            ],
            {},
        )
        answer = client.get(HER_PAGE, params=asked, headers=PAGE_HEADERS)
        written = her_reports(store)
    page = main_of(answer.text)

    assert answer.status_code == 200
    assert whole_forms(page) == ([] if about is None else [assignment_anchor(about)])
    assert (about == ESSAY_ID) == ('<section class="panel apart">' in page)
    assert autofocused(page) == []
    assert written == []
    check_the_cards(page)


def test_a_parent_reads_no_form_and_no_button_whatever_the_address_asks(
    tmp_path: pathlib.Path,
) -> None:
    with reading("a parent", tmp_path) as client:
        pages = [
            page_of(client),
            page_of(client, change=ESSAY_ID),
            page_of(client, change=READING_LOG_ID),
            page_of(client, week=LATER_WEEK, change=ESSAY_ID),
            client.post(REPORT, data={"status": "done"}, headers=PAGE_HEADERS).text,
            client.get(DETAILS, params={"change": "1"}, headers=PAGE_HEADERS).text,
        ]
        written = her_reports(store_of(client))

    for page in pages:
        assert "update-fold" not in page
        assert 'class="report"' not in page
        assert 'class="quick"' not in page
        assert quick_buttons(page) == []
        assert 'id="done-means"' not in page
    assert written == []


# ------------------------------------------------------------------ where Change lands


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
@pytest.mark.parametrize(
    ("name", "group"),
    [
        (ESSAY_ID, "update-choice-assignment-canal-essay"),
        (ESCAPED, "update-choice-set%2F2%20it%27s%20%231%3F%20%C3%A9"),
    ],
)
def test_change_lands_on_the_group_of_the_form_it_opens_and_asks_for_no_focus(
    name: str, group: str, reader: str, tmp_path: pathlib.Path
) -> None:
    with reading(reader, tmp_path) as client:
        store = store_of(client)
        store.put_on_record([due(ESCAPED, "Set two, question one", date(2026, 8, 21))], {})
        reported(store, "done", name)
        card = card_for(page_of(client), name)
        form = re.search(r'<form method="get" action="([^"]+)" class="action">', card)
        assert form is not None
        answer = client.get(form.group(1), params=form_fields(card, form.group(1)))
        page = main_of(answer.text)
        row = main_of(page_of(client, change=READING_LOG_ID))

    assert form.group(1) == f"/student/due-this-week#{group}"
    assert lands_on(page, form.group(1)) == f'<fieldset class="choice" id="{group}" tabindex="-1">'
    assert "update-fold" not in card_for(page, name)
    assert autofocused(page) == []
    landing = lands_on(row, f"#update-choice-{READING_LOG_ID}")
    assert landing == f'<fieldset class="choice" id="update-choice-{READING_LOG_ID}" tabindex="-1">'
    (whole,) = forms_of(row, WHOLE_FORM)
    assert whole.owner == assignment_anchor(READING_LOG_ID)
    assert landing in whole.html
    assert autofocused(row) == []


def test_the_group_a_change_lands_on_has_the_apps_outline() -> None:
    assert any(
        "outline: 2px solid var(--blue-action);" in inside
        for inside in rules_named(".update .choice:focus")
    )


# ------------------------------------------------------------------ the details


def details_refused(client: TestClient) -> Answer:
    page = client.get(DETAILS, params={"change": "1"}, headers=PAGE_HEADERS).text
    return client.post(REPORT, data={**form_fields(page, REPORT), "note": ""})


def details_saved_elsewhere(client: TestClient) -> Answer:
    page = client.get(DETAILS, params={"change": "1"}, headers=PAGE_HEADERS).text
    reported(store_of(client), "not_yet")
    return client.post(
        REPORT, data={**form_fields(page, REPORT), "status": "done", "note": "Mine."}
    )


DETAILS_RESPONSES: Final[dict[str, Callable[[TestClient], Answer]]] = {
    "arrival": lambda client: client.get(DETAILS, headers=PAGE_HEADERS),
    "Change": lambda client: client.get(DETAILS, params={"change": "1"}, headers=PAGE_HEADERS),
    "a refusal": details_refused,
    "a conflict": details_saved_elsewhere,
}


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
@pytest.mark.parametrize("case", list(DETAILS_RESPONSES))
def test_the_details_keep_the_form_unfolded_and_change_lands_where_it_did(
    case: str, reader: str, tmp_path: pathlib.Path
) -> None:
    with reading(reader, tmp_path) as client:
        answer = DETAILS_RESPONSES[case](client)
        reported(store_of(client), "done")
        saved_details = client.get(DETAILS, headers=PAGE_HEADERS).text

    assert "update-fold" not in answer.text
    assert 'class="report"' in answer.text
    assert f'<form method="get" action="{DETAILS}#update-or-turn-in" class="action">' in (
        saved_details
    )
