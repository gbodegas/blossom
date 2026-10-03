# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Her week folds the form of a card with no update under Update this homework, and opens it
when a response is about that form.

An ordinary visit leaves every fold closed, so the week reads as its homework and not as a
stack of forms. A card's facts, its links, its hand-in line and any line a save or a refusal
left stay outside the fold, which holds the form alone. An address that asks to change a
card, a refusal of her update, and a save refused because the card changed elsewhere open
that card's fold on the server, with what she chose and typed. A card with an update keeps
its summary and its Change, the details keep their form unfolded, and a parent reads no form.

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
class Fold:
    """One update fold: the card or row it sits in, whether it arrives open, its summary, and
    what it holds after the summary."""

    owner: str
    open: bool
    summary: str
    inside: str


UPDATE_FOLD: Final = re.compile(r'<details class="steps update-fold"( open)?>')
ITEM: Final = re.compile(r'<(?:article|li)\b[^>]*\sid="(assignment-[^"]*)"')
OWNER: Final = re.compile(r'(?<![\w-])id="(assignment-[^"]*)"')
FORM_ONLY: Final = re.compile(
    r'\s*(?:<h3 class="update-heading">Your unsaved update</h3>\s*)?'
    r'<form method="post" action="[^"]*/report" class="report">.*</form>\s*',
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


def update_folds(page: str) -> list[Fold]:
    """Every update fold on the page, in order."""
    found = []
    for start in UPDATE_FOLD.finditer(page):
        summary, _, inside = page[start.end() : closing(page, start.start())].partition(
            "</summary>"
        )
        owner = OWNER.findall(page, 0, start.start())[-1]
        found.append(Fold(owner, bool(start.group(1)), summary.strip(), inside))
    return found


def open_folds(page: str) -> list[str]:
    """The card or row of each fold that arrives open."""
    return [fold.owner for fold in update_folds(page) if fold.open]


def items(page: str) -> dict[str, str]:
    """Each card and row due later on the page, by its id: from its opening tag to the next
    one's."""
    places = [(found.start(), found.group(1)) for found in ITEM.finditer(page)]
    ends = [start for start, _ in places[1:]] + [len(page)]
    return {name: page[start:end] for (start, name), end in zip(places, ends, strict=True)}


def without_the_fold(item: str) -> str:
    """A card or row with its update fold taken out, or as it is when it has none."""
    start = UPDATE_FOLD.search(item)
    if start is None:
        return item
    return item[: start.start()] + item[closing(item, start.start()) :]


def autofocused(page: str) -> list[str]:
    """The opening tag of each element the page asks to take the focus as it arrives."""
    return re.findall(r"<[a-zA-Z][^<>]*\sautofocus(?=[\s>])[^<>]*>", page)


def check_the_folds(page: str) -> None:
    """Every form of a card with no update is in its fold, alone; a card with one has no fold;
    and at most one element asks for the focus, never one in a closed fold."""
    for name, item in items(page).items():
        folds = update_folds(item)
        has_form = 'class="report"' in item
        standing = '<p class="update-summary">' in item
        assert len(folds) == (1 if has_form and not standing else 0), name
        for fold in folds:
            assert fold.owner == name
            assert FORM_ONLY.fullmatch(fold.inside), (name, fold.inside[:300])
            assert fold.inside.count("<form") == 1
    focus = autofocused(page)
    assert len(focus) <= 1, focus
    for fold in update_folds(page):
        assert fold.open or not any(tag in fold.inside for tag in focus)


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


# case: (make the response, its status, the assignment whose fold it opens, or none)
RESPONSES: Final[dict[str, tuple[Callable[[TestClient], Answer], int, str | None]]] = {
    "arrival": (arrival, 200, None),
    "show=": (shown, 200, None),
    "a school report to check": (to_check, 200, None),
    "Keep it as it is": (kept_as_it_is, 200, None),
    "edit intent, a card": (asks_to_change(ESSAY_ID), 200, ESSAY_ID),
    "edit intent, a row due later": (asks_to_change(READING_LOG_ID), 200, READING_LOG_ID),
    "edit intent, a card shown apart": (asks_to_change(ESSAY_ID, week=LATER_WEEK), 200, ESSAY_ID),
    "edit intent, a card with an update": (edit_saved, 200, None),
    "no choice": (no_choice, 422, ESSAY_ID),
    "note too long": (note_too_long, 422, ESSAY_ID),
    "form not whole": (malformed, 422, ESSAY_ID),
    "bad return": (bad_return, 422, ESSAY_ID),
    "not this card's": (not_this_cards, 422, ESSAY_ID),
    "failed write": (failed_write, 500, ESSAY_ID),
    "conflict, saved elsewhere": (conflict, 409, None),
    "conflict, undone elsewhere": (conflict_undone, 409, ESSAY_ID),
    "a save": (then_shown(saved), 200, None),
    "the same save again": (then_shown(saved_again), 200, None),
    "an undo to no update": (then_shown(undone), 200, None),
    "a stale undo": (stale_undo, 409, None),
    "an undo already made": (already_undone, 409, None),
    "a failed undo": (failed_undo, 500, None),
}


# ------------------------------------------------------------------ which fold opens (T-U6)


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
@pytest.mark.parametrize("case", list(RESPONSES))
def test_only_the_fold_a_response_is_about_opens(
    case: str, reader: str, tmp_path: pathlib.Path
) -> None:
    make, status, about = RESPONSES[case]
    with reading(reader, tmp_path) as client:
        answer = make(client)
    page = main_of(answer.text)

    assert answer.status_code == status, answer.text[:300]
    assert open_folds(page) == ([] if about is None else [assignment_anchor(about)])
    assert update_folds(page)
    check_the_folds(page)


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
def test_an_ordinary_visit_folds_every_form_of_the_week_and_opens_none(
    reader: str, tmp_path: pathlib.Path
) -> None:
    with reading(reader, tmp_path) as client:
        page = main_of(page_of(client))

    folds = update_folds(page)
    assert [fold.owner for fold in folds] == [assignment_anchor(name) for name in UNREPORTED]
    assert not any(fold.open for fold in folds)
    assert folds[2].summary == (
        f'<summary>Update this homework<span class="visually-hidden"> for {ESSAY_TITLE}, '
        f"{ESSAY.course}</span>"
    )
    assert autofocused(page) == []
    check_the_folds(page)


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

    (fold,) = [fold for fold in update_folds(unchosen) if fold.open]
    assert radio in fold.inside
    assert ">kept</textarea>" in fold.inside
    assert autofocused(unchosen) == [radio]
    (fold,) = [fold for fold in update_folds(general) if fold.open]
    assert 'value="not_yet" checked>' in fold.inside
    assert ">kept</textarea>" in fold.inside
    assert autofocused(general) == [line]
    assert line in without_the_fold(card_for(general, ESSAY_ID))


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
def test_a_save_refused_after_an_undo_elsewhere_opens_the_fold_under_her_unsaved_update(
    reader: str, tmp_path: pathlib.Path
) -> None:
    with reading(reader, tmp_path) as client:
        answer = conflict_undone(client)
    card = card_for(main_of(answer.text), ESSAY_ID)

    (fold,) = update_folds(card)
    assert fold.open
    assert re.match(
        r'\s*<h3 class="update-heading">Your unsaved update</h3>\s*<form method="post"',
        fold.inside,
    )
    assert 'value="done" checked>' in fold.inside
    assert ">Mine.</textarea>" in fold.inside
    assert '<p class="update-summary">' not in card
    assert f'id="update-problem-{ESSAY_ID}"' in without_the_fold(card)


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
def test_the_lines_a_save_or_an_undo_leaves_stay_outside_a_closed_fold(
    reader: str, tmp_path: pathlib.Path
) -> None:
    with reading(reader, tmp_path) as client:
        restored = card_for(main_of(after(client, undone(client)).text), ESSAY_ID)
        stale = card_for(main_of(already_undone(client).text), ESSAY_ID)

    for card, line in (
        (restored, f'<p class="note update-result" role="status" id="update-result-{ESSAY_ID}"'),
        (stale, f'<p class="problem" role="alert" id="update-problem-{ESSAY_ID}"'),
    ):
        (fold,) = update_folds(card)
        assert not fold.open
        assert line in without_the_fold(card)
        assert line not in fold.inside


# ------------------------------------------------------------------ what stays outside the fold


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


# week: (arrange it, how many forms it folds)
WEEKS: Final[dict[str, tuple[Callable[[TestClient], None], int]]] = {
    "quiet": (lambda client: None, 7),
    "crowded": (a_crowded_week, 20),
    "all done": (every_card_done, 0),
    "long, several and waiting instructions": (instructions, 7),
}


@pytest.mark.parametrize("reader", ["her", "sign-in off"])
@pytest.mark.parametrize("week", list(WEEKS))
def test_a_cards_facts_links_and_lines_stay_outside_its_fold(
    week: str, reader: str, tmp_path: pathlib.Path
) -> None:
    """The fold holds the form alone: the title, the course, the due line, Details, Read
    instructions, the hand-in line and the school's words that apply are outside it."""
    arrange, count = WEEKS[week]
    with reading(reader, tmp_path) as client:
        arrange(client)
        page = main_of(page_of(client))
        edited = main_of(page_of(client, change=ESSAY_ID))
        records = {
            assignment_anchor(item.assignment_id): item
            for item in store_of(client).all_assignments()
        }

    check_the_folds(page)
    check_the_folds(edited)
    folded = {name: item for name, item in items(page).items() if update_folds(item)}
    assert len(folded) == count
    for name, item in folded.items():
        outside, record = without_the_fold(item), records[name]
        assert re.search(rf"<(h2|strong)>{re.escape(record.title)}</\1>", outside), name
        assert record.course in outside
        assert re.search(r'<p class="due">|<br>\s*Due \w+day, ', outside), name
        assert 'aria-label="Details: ' in outside
        assert '<p class="note hand-in-line">' in outside
    if week.startswith("long"):
        essay = without_the_fold(items(page)[assignment_anchor(ESSAY_ID)])
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
    check_the_folds(page)


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
def test_a_card_with_an_update_keeps_its_summary_change_and_undo_with_no_fold(
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
        assert update_folds(each) == []
        assert '<span class="pill">Your update: Not yet</span>' in each
    assert ">Change<span" in card
    assert ">Change<span" not in changing
    assert 'value="not_yet" checked>' in changing
    assert ">Keep it as it is</a>" in changing
    assert ">Change<span" in back
    assert undone_.status_code == 303
    assert [event.operation for event in left] == ["report", "undo"]


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
def test_edit_intent_opens_one_fold_for_an_id_of_up_to_two_hundred_characters(
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
    assert open_folds(page) == ([] if about is None else [assignment_anchor(about)])
    assert (about == ESSAY_ID) == ('<section class="panel apart">' in page)
    assert autofocused(page) == []
    assert written == []
    check_the_folds(page)


def test_a_parent_reads_no_fold_and_no_form_whatever_the_address_asks(
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
    assert update_folds(card_for(page, name)) == []
    assert autofocused(page) == []
    landing = lands_on(row, f"#update-choice-{READING_LOG_ID}")
    assert landing == f'<fieldset class="choice" id="update-choice-{READING_LOG_ID}" tabindex="-1">'
    (fold,) = [fold for fold in update_folds(row) if fold.open]
    assert landing in fold.inside
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
